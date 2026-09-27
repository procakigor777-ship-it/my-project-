"""Профиль бренда: описание продукта, тон ответов, примеры.

Это самая влиятельная часть промпта — сильнее модели и температуры.
Держится в отдельном файле, чтобы его правил маркетинг, а не разработчик.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class BrandProfile:
    product_name: str = "Наше приложение"
    product_description: str = ""
    features: dict[str, str] = field(default_factory=dict)
    tone_rules: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    signature: str = ""
    support_email: str = ""
    support_url: str = ""
    examples: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "BrandProfile":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def render(self) -> str:
        parts = [f"Продукт: {self.product_name}"]
        if self.product_description:
            parts.append(self.product_description)
        if self.features:
            glossary = "\n".join(f"- {name}: {desc}" for name, desc in self.features.items())
            parts.append(f"Глоссарий функций (используй эти названия, не выдумывай свои):\n{glossary}")
        if self.tone_rules:
            parts.append("Тон ответов:\n" + "\n".join(f"- {r}" for r in self.tone_rules))
        if self.forbidden:
            parts.append("Запрещено:\n" + "\n".join(f"- {r}" for r in self.forbidden))
        contacts = []
        if self.support_email:
            contacts.append(f"почта поддержки: {self.support_email}")
        if self.support_url:
            contacts.append(f"чат поддержки: {self.support_url}")
        if contacts:
            parts.append("Контакты поддержки — " + ", ".join(contacts))
        if self.signature:
            parts.append(f"Подпись в конце ответа: {self.signature}")
        if self.examples:
            rendered = "\n\n".join(
                f"Отзыв: {e.get('review', '')}\nХороший ответ: {e.get('reply', '')}"
                for e in self.examples
            )
            parts.append(f"Примеры ответов в нужном тоне:\n\n{rendered}")
        return "\n\n".join(parts)
