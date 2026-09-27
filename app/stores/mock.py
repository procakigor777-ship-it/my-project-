"""Мок-адаптер: весь конвейер гоняется локально без единого реального ключа.

Отзывы читаются из fixtures/<store>.json, опубликованные ответы
складываются в .runtime/published_<store>.json — туда же можно
заглянуть глазами после прогона.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models import Store
from app.stores.base import FetchedReview, ReviewGone, StoreClient, StoreError
from app.stores.limits import REPLY_CHAR_LIMITS

FIXTURES_DIR = Path("fixtures")
RUNTIME_DIR = Path(".runtime")


#: Даты в фикстурах можно задавать относительно текущего момента:
#: "-3h", "-2d". Иначе демо-данные устаревают и правила алертов,
#: которые смотрят на последние сутки, перестают срабатывать.
RELATIVE_RE = re.compile(r"^-(\d+)([hd])$")


def parse_created(value: str) -> datetime:
    match = RELATIVE_RE.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        delta = timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        return datetime.now(timezone.utc) - delta

    created = datetime.fromisoformat(value)
    return created if created.tzinfo else created.replace(tzinfo=timezone.utc)


class MockStoreClient(StoreClient):
    def __init__(
        self,
        store: Store,
        fixtures_dir: Path | None = None,
        runtime_dir: Path | None = None,
    ):
        self.store = store
        self.reply_char_limit = REPLY_CHAR_LIMITS[store]
        self._fixtures = (fixtures_dir or FIXTURES_DIR) / f"{store.value}.json"
        self._runtime = runtime_dir or RUNTIME_DIR
        self._published_path = self._runtime / f"published_{store.value}.json"

    def fetch_reviews(self, since: datetime | None = None) -> list[FetchedReview]:
        if not self._fixtures.exists():
            return []
        items = json.loads(self._fixtures.read_text(encoding="utf-8"))
        out = []
        for item in items:
            created = parse_created(item["created_at"])
            if since and created <= since:
                continue
            out.append(
                FetchedReview(
                    store=self.store,
                    store_review_id=item["id"],
                    rating=int(item["rating"]),
                    title=item.get("title"),
                    body=item.get("body", ""),
                    author=item.get("author"),
                    created_at=created,
                    locale=item.get("locale"),
                    territory=item.get("territory"),
                    app_version=item.get("app_version"),
                    device=item.get("device"),
                    raw=item,
                )
            )
        return out

    def publish_reply(self, store_review_id: str, text: str) -> str:
        if len(text) > self.reply_char_limit:
            raise StoreError(
                f"mock/{self.store.value}: ответ длиннее {self.reply_char_limit} символов",
                retryable=False,
            )
        known = {r.store_review_id for r in self.fetch_reviews()}
        if store_review_id not in known:
            raise ReviewGone(f"mock/{self.store.value}: отзыв {store_review_id} исчез")

        self._runtime.mkdir(parents=True, exist_ok=True)
        published = {}
        if self._published_path.exists():
            published = json.loads(self._published_path.read_text(encoding="utf-8"))
        published[store_review_id] = {
            "text": text,
            "published_at": datetime.now(timezone.utc).isoformat(),
        }
        self._published_path.write_text(
            json.dumps(published, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return f"mock-response-{store_review_id}"
