from collections.abc import AsyncIterator
from typing import Protocol


class ChatModelError(Exception):
    """The provider could not generate a response; `str(exc)` is safe to show to the tenant."""


class ChatModelConfigError(ChatModelError):
    """The chat model is misconfigured (unknown provider, missing API key, ...)."""


class ChatModel(Protocol):
    """A text generation provider. Callers depend only on this interface, never on a provider SDK."""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    def stream(self, *, system: str, prompt: str) -> AsyncIterator[str]:
        """Yield the response text incrementally."""
        ...

    async def close(self) -> None: ...
