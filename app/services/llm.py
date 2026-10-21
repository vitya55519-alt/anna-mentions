"""Тонкий клиент для любого OpenAI-совместимого эндпоинта.

Подходит Ollama (`http://localhost:11434/v1`), OpenRouter, OpenAI, vLLM.
Если LLM не настроен, анализ работает на правилах из rubrics/*.json.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def extract_json(content: str) -> dict | None:
    cleaned = _FENCE_RE.sub("", content).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


class LLMClient:
    def __init__(self, settings: Settings) -> None:
        self._url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        self._key = settings.llm_api_key
        self._model = settings.llm_model
        self._timeout = settings.llm_timeout_seconds

    async def complete_json(self, system: str, user: str) -> dict | None:
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        body = {
            "model": self._model,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for strict in (True, False):
                payload = {**body, "response_format": {"type": "json_object"}} if strict else body
                try:
                    response = await client.post(self._url, json=payload, headers=headers)
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if strict and exc.response.status_code in {400, 404, 422}:
                        continue  # часть локальных серверов не принимает response_format
                    logger.warning("LLM вернул ошибку: %s", exc)
                    return None
                except httpx.HTTPError as exc:
                    logger.warning("LLM недоступен: %s", exc)
                    return None

                try:
                    content = response.json()["choices"][0]["message"]["content"]
                except (KeyError, IndexError, ValueError):
                    logger.warning("Неожиданный ответ LLM: %s", response.text[:300])
                    return None
                return extract_json(content or "")

        return None
