"""Chat model provider registry (same pattern as `infra.embeddings`).

To add a provider: implement `ChatModel` in its own module and register a builder below. Builders import their
SDK lazily, so only the configured provider's dependencies are needed.
"""

from collections.abc import Callable
from functools import lru_cache

from common.config import Settings, get_settings
from infra.llm.base import ChatModel, ChatModelConfigError


def _ollama(settings: Settings) -> ChatModel:
    from infra.llm.ollama import OllamaChatModel

    return OllamaChatModel.from_settings(settings)


PROVIDERS: dict[str, Callable[[Settings], ChatModel]] = {
    "ollama": _ollama,
}


def create_chat_model(settings: Settings) -> ChatModel:
    """Build the chat model selected by LLM_PROVIDER."""
    name = settings.llm_provider.strip().lower()
    builder = PROVIDERS.get(name)
    if builder is None:
        known = ", ".join(sorted(PROVIDERS))
        raise ChatModelConfigError(f"Unknown LLM_PROVIDER {settings.llm_provider!r}; expected one of: {known}")
    return builder(settings)


@lru_cache
def get_chat_model() -> ChatModel:
    return create_chat_model(get_settings())
