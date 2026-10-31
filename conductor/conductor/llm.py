"""Клиент DeepSeek (совместим с OpenAI Chat Completions, с вызовом инструментов)."""
from __future__ import annotations

import json
import logging

import httpx

log = logging.getLogger("conductor.llm")


class LLMError(RuntimeError):
    pass


class DeepSeek:
    def __init__(self, api_key: str, model: str = "deepseek-chat", base_url: str = "https://api.deepseek.com",
                 http: httpx.Client | None = None):
        self.api_key = api_key
        self.model = model
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.http = http or httpx.Client(timeout=httpx.Timeout(90.0, connect=15.0))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, temperature: float = 0.2) -> dict:
        """Возвращает message ответа: {"content": ..., "tool_calls": [...]}"""
        if not self.enabled:
            raise LLMError("DeepSeek не настроен")
        body = {"model": self.model, "messages": messages, "temperature": temperature}
        if tools:
            body["tools"] = tools
        try:
            r = self.http.post(self.url, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as e:
            raise LLMError(f"DeepSeek недоступен: {type(e).__name__}") from None
        if r.status_code != 200:
            raise LLMError(f"DeepSeek ответил {r.status_code}")
        try:
            return r.json()["choices"][0]["message"]
        except (KeyError, IndexError, json.JSONDecodeError):
            raise LLMError("DeepSeek вернул непонятный ответ") from None
