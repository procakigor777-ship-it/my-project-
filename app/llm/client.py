"""Обёртка над Anthropic SDK.

Вынесена за интерфейс `LLMClient`, чтобы весь конвейер прогонялся в
тестах на `FakeLLM` без сетевых вызовов и без ключа.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol, Sequence, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    def parse(
        self, *, model: str, system: str, user: str, output_format: type[T], max_tokens: int = 4000
    ) -> T:
        """Один вызов со структурированным выводом."""

    def parse_batch(
        self,
        *,
        model: str,
        system: str,
        items: Sequence[tuple[str, str]],
        output_format: type[T],
        max_tokens: int = 4000,
    ) -> str:
        """Отправляет пачку в Batch API, возвращает id батча."""

    def collect_batch(self, batch_id: str, output_format: type[T]) -> dict[str, T | Exception]:
        """Забирает результаты батча. Ключ — custom_id."""


class AnthropicLLM:
    """Боевая реализация.

    Системный промпт помечается `cache_control`: он одинаков для всех
    отзывов в цикле, и кэш срезает заметную часть стоимости входа.
    Переменная часть (текст отзыва) идёт в user-сообщение — то есть
    после последней точки кэширования.
    """

    def __init__(self, api_key: str | None = None):
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    @staticmethod
    def _system_blocks(system: str) -> list[dict]:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def parse(
        self, *, model: str, system: str, user: str, output_format: type[T], max_tokens: int = 4000
    ) -> T:
        try:
            response = self._client.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=self._system_blocks(system),
                messages=[{"role": "user", "content": user}],
                output_format=output_format,
            )
        except Exception as exc:  # noqa: BLE001 — наверх уходит одна доменная ошибка
            raise LLMError(f"Ошибка вызова модели {model}: {exc}") from exc

        if response.parsed_output is None:
            raise LLMError(f"Модель {model} не вернула структурированный ответ")
        return response.parsed_output

    def parse_batch(
        self,
        *,
        model: str,
        system: str,
        items: Sequence[tuple[str, str]],
        output_format: type[T],
        max_tokens: int = 4000,
    ) -> str:
        from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
        from anthropic.types.messages.batch_create_params import Request

        schema = _json_schema(output_format)
        requests = [
            Request(
                custom_id=custom_id,
                params=MessageCreateParamsNonStreaming(
                    model=model,
                    max_tokens=max_tokens,
                    system=self._system_blocks(system),
                    messages=[{"role": "user", "content": user}],
                    output_config={"format": schema},
                ),
            )
            for custom_id, user in items
        ]
        try:
            batch = self._client.messages.batches.create(requests=requests)
        except Exception as exc:  # noqa: BLE001
            raise LLMError(f"Не удалось создать батч: {exc}") from exc
        return batch.id

    def batch_status(self, batch_id: str) -> str:
        return self._client.messages.batches.retrieve(batch_id).processing_status

    def collect_batch(self, batch_id: str, output_format: type[T]) -> dict[str, T | Exception]:
        """Разбирает результаты батча.

        Результаты приходят в произвольном порядке — матчим строго по
        custom_id, никогда по позиции.
        """
        out: dict[str, T | Exception] = {}
        for result in self._client.messages.batches.results(batch_id):
            kind = result.result.type
            if kind == "succeeded":
                message = result.result.message
                text = next((b.text for b in message.content if b.type == "text"), "")
                try:
                    out[result.custom_id] = output_format.model_validate_json(text)
                except Exception as exc:  # noqa: BLE001
                    out[result.custom_id] = LLMError(f"Невалидный JSON в батче: {exc}")
            else:
                out[result.custom_id] = LLMError(f"Запрос батча завершился как {kind}")
        return out


def _json_schema(model: type[BaseModel]) -> dict:
    """JSON-схема для output_config.format.

    Batch API принимает сырую схему, а не pydantic-класс, поэтому
    схему разворачиваем сами: $defs инлайнить не нужно, но
    additionalProperties обязан быть false на каждом объекте.
    """
    schema = model.model_json_schema()
    _forbid_extra(schema)
    return {"type": "json_schema", "schema": schema}


def _forbid_extra(node: object) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        for value in node.values():
            _forbid_extra(value)
    elif isinstance(node, list):
        for value in node:
            _forbid_extra(value)


def build_llm(api_key: str | None = None) -> LLMClient:
    return AnthropicLLM(api_key)


def dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
