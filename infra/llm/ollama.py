"""Ollama chat. Qwen3 thinking is turned off so a grounded answer is the only text streamed."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from common.config import Settings
from infra.llm.base import ChatModelConfigError, ChatModelError

DEFAULT_MODEL = "qwen3:4b"


class OllamaChatModel:
    def __init__(
        self,
        *,
        base_url: str,
        model: str = DEFAULT_MODEL,
        temperature: float = 0.2,
        num_ctx: int = 8192,
        client: Any = None,
    ) -> None:
        if not base_url.strip():
            raise ChatModelConfigError("OLLAMA_BASE_URL is not set")
        if num_ctx < 1:
            raise ChatModelConfigError("OLLAMA_NUM_CTX must be positive")
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._temperature = temperature
        self._num_ctx = num_ctx
        self._client = client if client is not None else httpx.AsyncClient(base_url=self._base_url, timeout=120.0)
        self._owns_client = client is None

    @classmethod
    def from_settings(cls, settings: Settings) -> "OllamaChatModel":
        return cls(
            base_url=settings.ollama_base_url,
            model=settings.llm_model or DEFAULT_MODEL,
            temperature=settings.llm_temperature,
            num_ctx=settings.ollama_num_ctx,
        )

    @property
    def provider(self) -> str:
        return "ollama"

    @property
    def model(self) -> str:
        return self._model

    async def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "stream": True,
            "think": False,
            "options": {"temperature": self._temperature, "num_ctx": self._num_ctx},
        }
        try:
            async with self._client.stream("POST", "/api/chat", json=payload) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    body = (await exc.response.aread()).decode(errors="replace").strip()
                    raise ChatModelError(f"The answer could not be generated ({exc.response.status_code}): {body}") from exc
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ChatModelError("The answer could not be generated") from exc
                    if error := event.get("error"):
                        raise ChatModelError(f"The answer could not be generated: {error}")
                    content = (event.get("message") or {}).get("content") or ""
                    if content:
                        yield content
        except ChatModelError:
            raise
        except httpx.RequestError as exc:
            raise ChatModelError(f"The answer could not be generated (Ollama is unreachable at {self._base_url})") from exc
        except Exception as exc:
            raise ChatModelError("The answer could not be generated") from exc

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
