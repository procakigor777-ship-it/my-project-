"""Адаптер App Store Connect API.

Авторизация — JWT (ES256), подписанный приватным ключом из
App Store Connect → Users and Access → Integrations. Токен живёт
не дольше 20 минут, поэтому пересоздаётся по необходимости.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import httpx2 as httpx
import jwt

from app.config import Settings
from app.models import Store
from app.stores.base import FetchedReview, ReviewGone, StoreClient, StoreError
from app.stores.limits import APPSTORE_REPLY_CHAR_LIMIT

API_ROOT = "https://api.appstoreconnect.apple.com"
AUDIENCE = "appstoreconnect-v1"
TOKEN_TTL_SECONDS = 15 * 60
PAGE_LIMIT = 200


class AppStoreClient(StoreClient):
    store = Store.APP_STORE
    reply_char_limit = APPSTORE_REPLY_CHAR_LIMIT

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        missing = [
            name
            for name, value in (
                ("APPSTORE_ISSUER_ID", settings.appstore_issuer_id),
                ("APPSTORE_KEY_ID", settings.appstore_key_id),
                ("APPSTORE_PRIVATE_KEY_PATH", settings.appstore_private_key_path),
                ("APPSTORE_APP_ID", settings.appstore_app_id),
            )
            if not value
        ]
        if missing:
            raise StoreError(f"App Store: не заданы {', '.join(missing)}")

        self.settings = settings
        self.app_id = settings.appstore_app_id
        self._private_key = settings.appstore_private_key_path.read_text()
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._http = http or httpx.Client(timeout=30.0)

    # --- авторизация -------------------------------------------------

    def _auth_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token

        payload = {
            "iss": self.settings.appstore_issuer_id,
            "iat": int(now),
            "exp": int(now + TOKEN_TTL_SECONDS),
            "aud": AUDIENCE,
        }
        self._token = jwt.encode(
            payload,
            self._private_key,
            algorithm="ES256",
            headers={"kid": self.settings.appstore_key_id, "typ": "JWT"},
        )
        self._token_expires_at = now + TOKEN_TTL_SECONDS
        return self._token

    def _request(self, method: str, url: str, **kwargs) -> dict:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._auth_token()}"
        try:
            response = self._http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise StoreError(f"App Store: сетевая ошибка: {exc}", retryable=True) from exc

        if response.status_code == 404:
            raise ReviewGone("App Store: объект не найден")
        if response.status_code == 429 or response.status_code >= 500:
            raise StoreError(
                f"App Store: {response.status_code} {response.text[:300]}",
                retryable=True,
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise StoreError(
                f"App Store: {response.status_code} {response.text[:300]}",
                retryable=False,
                status=response.status_code,
            )
        return response.json() if response.content else {}

    # --- чтение ------------------------------------------------------

    def fetch_reviews(self, since: datetime | None = None) -> list[FetchedReview]:
        """Инкрементально забирает отзывы.

        Сортировка по дате убыв. позволяет остановиться, как только дошли
        до уже известного периода, и не вычитывать всю историю.

        Если в конфиге перечислены территории, обходим каждую отдельно:
        Apple отдаёт выдачу по территориям, и без явного перебора часть
        отзывов может не попасть в первые страницы.
        """
        territories = self.settings.appstore_territory_list or [None]
        collected: list[FetchedReview] = []
        for territory in territories:
            collected.extend(self._fetch_territory(territory, since))
        return collected

    def _fetch_territory(
        self, territory: str | None, since: datetime | None
    ) -> list[FetchedReview]:
        params: dict[str, str | int] = {"limit": PAGE_LIMIT, "sort": "-createdDate"}
        if territory:
            params["filter[territory]"] = territory

        url = f"{API_ROOT}/v1/apps/{self.app_id}/customerReviews"
        out: list[FetchedReview] = []
        while url:
            payload = self._request("GET", url, params=params)
            params = {}  # ссылка next уже содержит параметры
            for item in payload.get("data", []):
                review = self._parse(item, territory)
                if since and review.created_at <= since:
                    return out  # выдача отсортирована — дальше только старое
                out.append(review)
            url = payload.get("links", {}).get("next", "")
        return out

    def _parse(self, item: dict, territory: str | None) -> FetchedReview:
        attrs = item.get("attributes", {})
        created_raw = attrs.get("createdDate")
        created = (
            datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
            if created_raw
            else datetime.now(timezone.utc)
        )
        return FetchedReview(
            store=Store.APP_STORE,
            store_review_id=item["id"],
            rating=int(attrs.get("rating") or 0),
            title=attrs.get("title"),
            body=attrs.get("body") or "",
            author=attrs.get("reviewerNickname"),
            created_at=created,
            territory=attrs.get("territory") or territory,
            raw=item,
        )

    # --- запись ------------------------------------------------------

    def publish_reply(self, store_review_id: str, text: str) -> str:
        """Публикует ответ.

        Повторный POST на тот же отзыв заменяет предыдущий ответ —
        отдельный «update» не нужен.
        """
        if len(text) > self.reply_char_limit:
            raise StoreError(
                f"App Store: ответ длиннее {self.reply_char_limit} символов", retryable=False
            )

        body = {
            "data": {
                "type": "customerReviewResponses",
                "attributes": {"responseBody": text},
                "relationships": {
                    "review": {"data": {"type": "customerReviews", "id": store_review_id}}
                },
            }
        }
        payload = self._request(
            "POST",
            f"{API_ROOT}/v1/customerReviewResponses",
            json=body,
            headers={"Content-Type": "application/json"},
        )
        return payload.get("data", {}).get("id", "")

    def delete_reply(self, response_id: str) -> None:
        self._request("DELETE", f"{API_ROOT}/v1/customerReviewResponses/{response_id}")

    @staticmethod
    def token_lifetime() -> timedelta:
        return timedelta(seconds=TOKEN_TTL_SECONDS)
