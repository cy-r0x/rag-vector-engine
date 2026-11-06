import json

import httpx
import pytest

from common.config import Settings
from infra.llm import PROVIDERS, ChatModelConfigError, ChatModelError, create_chat_model
from infra.llm.ollama import DEFAULT_MODEL, OllamaChatModel


class FakeStream:
    def __init__(self, lines: list[str], status_code: int = 200, body: str = "") -> None:
        self._lines = lines
        self.status_code = status_code
        self._body = body.encode()
        self.request = httpx.Request("POST", "http://ollama/api/chat")

    async def __aenter__(self) -> "FakeStream":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=self.request,
                response=httpx.Response(self.status_code, content=self._body, request=self.request),
            )

    async def aread(self) -> bytes:
        return self._body

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class FakeClient:
    def __init__(self, stream: FakeStream | None = None, error: Exception | None = None) -> None:
        self.stream_response = stream
        self.error = error
        self.payloads: list[dict] = []
        self.closed = False

    def stream(self, method: str, url: str, json: dict) -> FakeStream:
        self.payloads.append(json)
        if self.error is not None:
            raise self.error
        assert self.stream_response is not None
        return self.stream_response

    async def aclose(self) -> None:
        self.closed = True


def _model(client: FakeClient, **kwargs) -> OllamaChatModel:
    return OllamaChatModel(base_url="http://127.0.0.1:11434", temperature=0.2, num_ctx=8192, client=client, **kwargs)


async def test_stream_sends_system_and_user_messages_without_thinking():
    lines = [
        json.dumps({"message": {"role": "assistant", "content": "Hello"}}),
        json.dumps({"message": {"role": "assistant", "content": " there"}, "done": True}),
    ]
    client = FakeClient(FakeStream(lines))

    parts = [part async for part in _model(client).stream(system="Use the excerpts.", prompt="What is the fee?")]

    assert parts == ["Hello", " there"]
    payload = client.payloads[0]
    assert payload["model"] == DEFAULT_MODEL
    assert payload["think"] is False
    assert payload["messages"] == [
        {"role": "system", "content": "Use the excerpts."},
        {"role": "user", "content": "What is the fee?"},
    ]
    assert payload["options"] == {"temperature": 0.2, "num_ctx": 8192}


async def test_ollama_errors_are_wrapped():
    client = FakeClient(error=httpx.ConnectError("refused"))

    with pytest.raises(ChatModelError, match="unreachable"):
        [part async for part in _model(client).stream(system="s", prompt="p")]


def test_factory_builds_ollama_and_rejects_unknown_providers():
    assert Settings.model_fields["llm_provider"].default == "ollama"
    model = create_chat_model(Settings(llm_model="", ollama_base_url="http://127.0.0.1:11434"))
    assert isinstance(model, OllamaChatModel) and model.model == DEFAULT_MODEL
    with pytest.raises(ChatModelConfigError, match="Unknown LLM_PROVIDER 'acme'.*ollama"):
        create_chat_model(Settings(llm_provider="acme"))
    assert PROVIDERS.keys() == {"ollama"}


def test_from_settings_uses_qwen3_4b():
    model = OllamaChatModel.from_settings(
        Settings(llm_model="", llm_temperature=0.1, ollama_base_url="http://127.0.0.1:11434", ollama_num_ctx=4096)
    )
    assert (model.provider, model.model) == ("ollama", DEFAULT_MODEL)
