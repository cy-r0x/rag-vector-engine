from types import SimpleNamespace

import pytest
from google.genai import errors, types

from common.config import Settings
from infra.llm import PROVIDERS, ChatModelConfigError, ChatModelError, create_chat_model
from infra.llm.gemini import DEFAULT_MODEL, GeminiChatModel


class FakeModels:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.parts = ["Hello", "", " world"]
        self.error: Exception | None = None

    async def generate_content_stream(self, *, model, contents, config):
        self.calls.append(
            {"model": model, "contents": contents, "system": config.system_instruction, "temperature": config.temperature}
        )
        if self.error is not None:
            raise self.error

        async def chunks():
            for part in self.parts:
                yield types.GenerateContentResponse(
                    candidates=[types.Candidate(content=types.Content(role="model", parts=[types.Part(text=part)]))]
                )

        return chunks()


def _model() -> tuple[GeminiChatModel, FakeModels]:
    models = FakeModels()

    async def aclose() -> None:
        pass

    client = SimpleNamespace(aio=SimpleNamespace(models=models, aclose=aclose))
    return GeminiChatModel(api_key=None, temperature=0.1, client=client), models


async def test_stream_yields_text_and_passes_the_system_prompt():
    model, fake = _model()

    parts = [part async for part in model.stream(system="Answer from excerpts.", prompt="Question: hi")]

    assert parts == ["Hello", " world"]
    assert fake.calls == [
        {"model": DEFAULT_MODEL, "contents": "Question: hi", "system": "Answer from excerpts.", "temperature": 0.1}
    ]


async def test_api_errors_are_wrapped():
    model, fake = _model()
    fake.error = errors.ServerError(503, {"error": {"code": 503, "message": "Overloaded", "status": "UNAVAILABLE"}})

    with pytest.raises(ChatModelError, match="503 UNAVAILABLE"):
        [part async for part in model.stream(system="s", prompt="p")]


def test_missing_api_key_is_a_config_error():
    with pytest.raises(ChatModelConfigError, match="GEMINI_API_KEY is not set"):
        GeminiChatModel(api_key=None)


def test_factory_builds_gemini_by_default_and_rejects_unknown_providers():
    model = create_chat_model(Settings(gemini_api_key="key", llm_model=""))
    assert isinstance(model, GeminiChatModel) and model.model == DEFAULT_MODEL
    with pytest.raises(ChatModelConfigError, match="Unknown LLM_PROVIDER 'acme'.*gemini"):
        create_chat_model(Settings(llm_provider="acme"))
    assert "gemini" in PROVIDERS
