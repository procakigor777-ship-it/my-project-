"""Адаптер Google Play Developer API (androidpublisher v3).

Главное ограничение, вокруг которого построен весь ingest: `reviews.list`
отдаёт только отзывы за последние ~7 дней и только те, где есть текст.
Если воркер не отработал неделю — эти отзывы потеряны безвозвратно,
их неоткуда догрузить. Отсюда алерт на «ingest молчит N часов».
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx2 as httpx
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import service_account

from app.config import Settings
from app.models import Store
from app.stores.base import FetchedReview, ReviewGone, StoreClient, StoreError
from app.stores.limits import GOOGLEPLAY_REPLY_CHAR_LIMIT

API_ROOT = "https://androidpublisher.googleapis.com/androidpublisher/v3"
SCOPES = ["https://www.googleapis.com/auth/androidpublisher"]
PAGE_SIZE = 100


class GooglePlayClient(StoreClient):
    store = Store.GOOGLE_PLAY
    reply_char_limit = GOOGLEPLAY_REPLY_CHAR_LIMIT

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        if not settings.googleplay_service_account_path:
            raise StoreError("Google Play: не задан GOOGLEPLAY_SERVICE_ACCOUNT_PATH")
        if not settings.googleplay_package_name:
            raise StoreError("Google Play: не задан GOOGLEPLAY_PACKAGE_NAME")

        self.package_name = settings.googleplay_package_name
        self._credentials = service_account.Credentials.from_service_account_file(
            str(settings.googleplay_service_account_path), scopes=SCOPES
        )
        self._http = http or httpx.Client(timeout=30.0)

    def _access_token(self) -> str:
        if not self._credentials.valid:
            self._credentials.refresh(GoogleAuthRequest())
        return self._credentials.token

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token()}"
        try:
            response = self._http.request(method, f"{API_ROOT}{path}", headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise StoreError(f"Google Play: сетевая ошибка: {exc}", retryable=True) from exc

        if response.status_code == 404:
            raise ReviewGone("Google Play: отзыв не найден")
        if response.status_code == 429 or response.status_code >= 500:
            raise StoreError(
                f"Google Play: {response.status_code} {response.text[:300]}",
                retryable=True,
                status=response.status_code,
            )
        if response.status_code >= 400:
            raise StoreError(
                f"Google Play: {response.status_code} {response.text[:300]}",
                retryable=False,
                status=response.status_code,
            )
        return response.json() if response.content else {}

    # --- чтение ------------------------------------------------------

    def fetch_reviews(self, since: datetime | None = None) -> list[FetchedReview]:
        """Забирает всё доступное окно и фильтрует по `since` на нашей стороне.

        Фильтра по дате в API нет, а окно и так всего неделя, поэтому
        полный обход дешевле любой хитрости. Отредактированные отзывы
        приходят с новым `lastModified` — их надо пропустить дальше по
        конвейеру, поэтому сравнение нестрогое к телу: решение о
        переанализе принимает ingest по хешу текста.
        """
        params: dict[str, str | int] = {"maxResults": PAGE_SIZE}
        out: list[FetchedReview] = []
        while True:
            payload = self._request("GET", f"/applications/{self.package_name}/reviews", params=params)
            for item in payload.get("reviews", []):
                review = self._parse(item)
                if review is None:
                    continue
                if since and review.created_at <= since:
                    continue
                out.append(review)

            token = payload.get("tokenPagination", {}).get("nextPageToken")
            if not token:
                return out
            params = {"maxResults": PAGE_SIZE, "token": token}

    def _parse(self, item: dict) -> FetchedReview | None:
        comments = item.get("comments") or []
        user_comment = next(
            (c["userComment"] for c in comments if "userComment" in c), None
        )
        if not user_comment:
            return None  # остался только наш собственный ответ

        seconds = int((user_comment.get("lastModified") or {}).get("seconds") or 0)
        created = datetime.fromtimestamp(seconds, tz=timezone.utc)

        return FetchedReview(
            store=Store.GOOGLE_PLAY,
            store_review_id=item["reviewId"],
            rating=int(user_comment.get("starRating") or 0),
            title=None,  # в Google Play у отзыва нет заголовка
            body=user_comment.get("text") or "",
            author=item.get("authorName"),
            created_at=created,
            locale=user_comment.get("reviewerLanguage"),
            app_version=user_comment.get("appVersionName"),
            device=user_comment.get("device"),
            raw=item,
        )

    # --- запись ------------------------------------------------------

    def publish_reply(self, store_review_id: str, text: str) -> str:
        """Публикует ответ.

        У отзыва может быть только один ответ разработчика: повторный
        вызов перезаписывает предыдущий.
        """
        if len(text) > self.reply_char_limit:
            raise StoreError(
                f"Google Play: ответ длиннее {self.reply_char_limit} символов", retryable=False
            )

        self._request(
            "POST",
            f"/applications/{self.package_name}/reviews/{store_review_id}:reply",
            json={"replyText": text},
            headers={"Content-Type": "application/json"},
        )
        # Отдельного идентификатора ответа в Google Play нет — ответ
        # принадлежит отзыву, поэтому возвращаем его же id.
        return store_review_id
