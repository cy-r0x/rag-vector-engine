from collections.abc import AsyncIterator
from typing import Any

from google import genai
from google.genai import errors, types

from common.config import Settings
from infra.llm.base import ChatModelConfigError, ChatModelError

DEFAULT_MODEL = "gemini-3.5-flash-lite"

_TIMEOUT_MS = 120_000
_RETRY = types.HttpRetryOptions(
    attempts=3, initial_delay=1.0, max_delay=10.0, http_status_codes=[408, 429, 500, 502, 503, 504]
)


class GeminiChatModel:
    def __init__(
        self, *, api_key: str | None, model: str = DEFAULT_MODEL, temperature: float = 0.2, client: Any = None
    ) -> None:
        if client is None:
            if not api_key:
                raise ChatModelConfigError("GEMINI_API_KEY is not set")
            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=_TIMEOUT_MS, retry_options=_RETRY))
        self._client = client
        self._model = model
        self._temperature = temperature

    @classmethod
    def from_settings(cls, settings: Settings) -> "GeminiChatModel":
        return cls(
            api_key=settings.gemini_api_key,
            model=settings.llm_model or DEFAULT_MODEL,
            temperature=settings.llm_temperature,
        )

    @property
    def provider(self) -> str:
        return "gemini"

    @property
    def model(self) -> str:
        return self._model

    async def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        try:
            response = await self._client.aio.models.generate_content_stream(
                model=self._model,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=system, temperature=self._temperature),
            )
            async for chunk in response:
                if chunk.text:
                    yield chunk.text
        except errors.APIError as exc:
            raise ChatModelError(f"The answer could not be generated ({exc.code} {exc.status})") from exc
        except ChatModelError:
            raise
        except Exception as exc:
            raise ChatModelError("The answer could not be generated") from exc

    async def close(self) -> None:
        await self._client.aio.aclose()
