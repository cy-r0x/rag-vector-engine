import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.websockets import WebSocketDisconnect

from api.handlers.chat import CHAT_TICKET_USE, CLOSE_FORBIDDEN, CLOSE_UNAUTHORIZED
from api.main import app
from api.middleware.auth import TOKEN_USE_CLAIM
from api.rag import NO_CONTEXT_ANSWER, SYSTEM_PROMPT
from common.auth import get_jwt_codec
from db import get_sessionmaker
from db.models import Document
from infra.embeddings import get_embedder
from infra.llm import ChatModelError, get_chat_model
from infra.storage.vector_db import ChunkHit, get_vector_store
from tests.api.conftest import DATABASE_URL, run_db
from tests.workers.fakes import FakeEmbedder


class SearchStore:
    def __init__(self) -> None:
        self.hits: list[ChunkHit] = []
        self.calls: list[dict] = []

    async def search(self, *, business_id, vector, limit=5, min_score=None, doc_ids=None):
        self.calls.append({"business_id": business_id, "limit": limit, "min_score": min_score})
        return self.hits

    async def close(self):
        pass


class FakeChatModel:
    provider, model = "fake", "fake-chat"

    def __init__(self) -> None:
        self.parts = ["Revenue grew ", "12% [1]."]
        self.error: Exception | None = None
        self.calls: list[dict] = []

    async def stream(self, *, system, prompt):
        self.calls.append({"system": system, "prompt": prompt})
        for part in self.parts:
            yield part
        if self.error is not None:
            raise self.error

    async def close(self):
        pass


@pytest.fixture
def chat(client: TestClient):
    """Registers a business with one document and wires fakes into the chat socket."""
    response = client.post(
        "/v1/auth/register",
        json={"business_name": "Acme", "email": f"{uuid.uuid4().hex[:12]}@acme.com", "password": "s3cret-passw0rd"},
    )
    assert response.status_code == 201
    data = response.json()
    business_id = uuid.UUID(data["business_id"])
    doc_id = uuid.uuid7()

    async def create(session):
        session.add(
            Document(
                id=doc_id,
                business_id=business_id,
                filename="q1-report.pdf",
                detected_mime="application/pdf",
                size_bytes=10,
                storage_key=f"{business_id}/{doc_id}/source.pdf",
            )
        )
        await session.commit()

    run_db(create)

    engine = create_async_engine(DATABASE_URL, poolclass=pool.NullPool)
    store, chat_model = SearchStore(), FakeChatModel()
    app.dependency_overrides[get_sessionmaker] = lambda: async_sessionmaker(engine, expire_on_commit=False)
    app.dependency_overrides[get_embedder] = FakeEmbedder
    app.dependency_overrides[get_vector_store] = lambda: store
    app.dependency_overrides[get_chat_model] = lambda: chat_model
    try:
        yield {
            "client": client,
            "business_id": business_id,
            "doc_id": doc_id,
            "access_token": data["access_token"],
            "user_id": data["user_id"],
            "store": store,
            "chat_model": chat_model,
        }
    finally:
        for dependency in (get_sessionmaker, get_embedder, get_vector_store, get_chat_model):
            app.dependency_overrides.pop(dependency, None)


def _ticket(chat) -> str:
    response = chat["client"].post(
        "/v1/chat/ticket", headers={"Authorization": f"Bearer {chat['access_token']}"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["business_id"] == str(chat["business_id"])
    assert body["ws_path"] == f"/v1/chat/ws/{chat['business_id']}"
    return body["ticket"]


def _url(business_id, token: str | None) -> str:
    return f"/v1/chat/ws/{business_id}" + (f"?token={token}" if token else "")


def _close_code(client: TestClient, url: str) -> int:
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(url) as socket:
            socket.receive_json()
    return closed.value.code


def _hit(doc_id, *, index=0, text="Revenue grew 12% year over year.", score=0.81) -> ChunkHit:
    return ChunkHit(doc_id=doc_id, chunk_index=index, text=text, start=0, end=len(text), score=score)


def test_socket_requires_a_token(chat):
    assert _close_code(chat["client"], _url(chat["business_id"], None)) == CLOSE_UNAUTHORIZED


def test_socket_rejects_an_invalid_token(chat):
    assert _close_code(chat["client"], _url(chat["business_id"], "not-a-jwt")) == CLOSE_UNAUTHORIZED


def test_socket_rejects_a_token_for_another_business(chat):
    assert _close_code(chat["client"], _url(uuid.uuid7(), _ticket(chat))) == CLOSE_FORBIDDEN


def test_ticket_is_not_an_api_access_token(chat):
    ticket = _ticket(chat)
    response = chat["client"].get("/v1/documents", headers={"Authorization": f"Bearer {ticket}"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Token is not valid for this API"
    assert get_jwt_codec().decode(ticket)[TOKEN_USE_CLAIM] == CHAT_TICKET_USE


def test_answer_streams_with_sources_for_the_tokens_business(chat):
    chat["store"].hits = [_hit(chat["doc_id"]), _hit(uuid.uuid7(), text="deleted document chunk", score=0.7)]

    with chat["client"].websocket_connect(_url(chat["business_id"], _ticket(chat))) as socket:
        socket.send_json({"query": "  How much did revenue grow?  ", "session_id": "s-1"})
        frames = [socket.receive_json() for _ in range(3)]

    assert frames[:2] == [
        {"type": "delta", "content": "Revenue grew ", "session_id": "s-1"},
        {"type": "delta", "content": "12% [1].", "session_id": "s-1"},
    ]
    assert frames[2] == {
        "type": "done",
        "session_id": "s-1",
        "sources": [
            {
                "number": 1,
                "doc_id": str(chat["doc_id"]),
                "filename": "q1-report.pdf",
                "chunk_index": 0,
                "score": 0.81,
                "excerpt": "Revenue grew 12% year over year.",
            }
        ],
    }
    assert chat["store"].calls == [{"business_id": chat["business_id"], "limit": 5, "min_score": 0.65}]
    (call,) = chat["chat_model"].calls
    assert call["system"] == SYSTEM_PROMPT
    assert '<excerpt number="1" document="q1-report.pdf">\nRevenue grew 12% year over year.\n</excerpt>' in call["prompt"]
    assert "deleted document chunk" not in call["prompt"]
    assert call["prompt"].endswith("Question: How much did revenue grow?")


def test_access_tokens_can_open_the_socket_too(chat):
    with chat["client"].websocket_connect(_url(chat["business_id"], chat["access_token"])) as socket:
        socket.send_json({"query": "anything?"})
        assert socket.receive_json() == {"type": "delta", "content": NO_CONTEXT_ANSWER}


def test_no_relevant_chunks_answers_without_calling_the_model(chat):
    with chat["client"].websocket_connect(_url(chat["business_id"], _ticket(chat))) as socket:
        socket.send_json({"query": "What is the cafeteria menu?"})
        frames = [socket.receive_json() for _ in range(2)]

    assert frames == [{"type": "delta", "content": NO_CONTEXT_ANSWER}, {"type": "done", "sources": []}]
    assert chat["chat_model"].calls == []


@pytest.mark.parametrize(
    ("raw", "detail"),
    [
        ("not json", "Messages must be JSON"),
        ('["query"]', "Messages must be JSON objects"),
        ('{"query": "   "}', "Ask a question"),
        ('{"query": "' + "x" * 2001 + '"}', "limited to 2000 characters"),
        ('{"query": "hi", "session_id": 5}', "'session_id' must be a string"),
    ],
)
def test_invalid_messages_get_an_error_and_keep_the_socket_open(chat, raw: str, detail: str):
    with chat["client"].websocket_connect(_url(chat["business_id"], _ticket(chat))) as socket:
        socket.send_text(raw)
        error = socket.receive_json()
        socket.send_json({"query": "still there?"})
        follow_up = socket.receive_json()

    assert error["type"] == "error" and detail in error["detail"]
    assert follow_up["type"] == "delta"


def test_model_failure_is_reported_as_an_error_frame(chat):
    chat["store"].hits = [_hit(chat["doc_id"])]
    chat["chat_model"].parts = []
    chat["chat_model"].error = ChatModelError("boom")

    with chat["client"].websocket_connect(_url(chat["business_id"], _ticket(chat))) as socket:
        socket.send_json({"query": "How much did revenue grow?"})
        frame = socket.receive_json()

    assert frame == {"type": "error", "detail": "The answer could not be generated. Please try again."}


def test_socket_closes_once_the_login_session_has_expired(chat):
    ticket = get_jwt_codec().encode(
        sub=chat["user_id"],
        business_id=str(chat["business_id"]),
        extra_claims={TOKEN_USE_CLAIM: CHAT_TICKET_USE, "session_exp": int(time.time()) - 1},
    )
    with chat["client"].websocket_connect(_url(chat["business_id"], ticket)) as socket:
        socket.send_json({"query": "How much did revenue grow?"})
        assert socket.receive_json()["detail"].startswith("Your session has expired")
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == CLOSE_UNAUTHORIZED
