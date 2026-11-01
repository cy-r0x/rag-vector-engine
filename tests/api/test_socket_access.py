import hashlib
import uuid

import pytest
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from api.handlers.chat import CLOSE_FORBIDDEN, CLOSE_UNAUTHORIZED
from api.rag import NO_CONTEXT_ANSWER
from db.models import SocketToken
from tests.api.conftest import run_db
from tests.api.test_chat import chat  # noqa: F401  (fixture)


def _auth(chat) -> dict[str, str]:
    return {"Authorization": f"Bearer {chat['access_token']}"}


def _issue(chat) -> dict:
    response = chat["client"].post("/v1/socket/token", headers=_auth(chat))
    assert response.status_code == 200, response.text
    return response.json()


def _url(business_id) -> str:
    return f"/v1/chat/ws/{business_id}"


def _assert_answers(socket) -> None:
    socket.send_json({"query": "anything?"})
    assert socket.receive_json() == {"type": "delta", "content": NO_CONTEXT_ANSWER}
    assert socket.receive_json() == {"type": "done", "sources": []}


def _close_code(client, url: str, **kwargs) -> tuple[int, str]:
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(url, **kwargs) as socket:
            socket.receive_json()
    return closed.value.code, closed.value.reason


def test_socket_info_before_any_token(chat):
    response = chat["client"].get("/v1/socket", headers=_auth(chat))

    assert response.status_code == 200
    assert response.json() == {
        "business_id": str(chat["business_id"]),
        "ws_url": f"ws://localhost:8000/v1/chat/ws/{chat['business_id']}",
        "token": None,
    }


def test_issued_token_is_returned_once_and_stored_only_as_a_hash(chat):
    issued = _issue(chat)
    token = issued["token"]

    assert token.startswith("rve_sk_") and len(token) > 40
    assert issued["prefix"] == token[:13]
    assert issued["ws_url"] == f"ws://localhost:8000/v1/chat/ws/{chat['business_id']}"

    info = chat["client"].get("/v1/socket", headers=_auth(chat)).json()
    assert info["token"]["prefix"] == issued["prefix"]
    assert "token" not in info["token"]

    row = run_db(lambda s: s.scalar(select(SocketToken).where(SocketToken.business_id == chat["business_id"])))
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in (row.token_hash, row.token_prefix)


def test_socket_token_works_in_the_authorization_header_and_the_query(chat):
    token = _issue(chat)["token"]

    with chat["client"].websocket_connect(
        _url(chat["business_id"]), headers={"Authorization": f"Bearer {token}"}
    ) as socket:
        _assert_answers(socket)
    with chat["client"].websocket_connect(f"{_url(chat['business_id'])}?token={token}") as socket:
        _assert_answers(socket)


def test_rotation_revokes_the_previous_token_immediately(chat):
    old = _issue(chat)["token"]
    new = _issue(chat)["token"]
    assert new != old

    code, reason = _close_code(chat["client"], _url(chat["business_id"]), headers={"Authorization": f"Bearer {old}"})
    assert (code, reason) == (CLOSE_UNAUTHORIZED, "Invalid or rotated socket token")
    with chat["client"].websocket_connect(_url(chat["business_id"]), headers={"Authorization": f"Bearer {new}"}) as socket:
        _assert_answers(socket)
    assert run_db(lambda s: s.scalar(select(SocketToken.id).where(SocketToken.business_id == chat["business_id"])))


def test_open_socket_is_closed_on_its_next_message_after_rotation(chat):
    old = _issue(chat)["token"]

    with chat["client"].websocket_connect(_url(chat["business_id"]), headers={"Authorization": f"Bearer {old}"}) as socket:
        _assert_answers(socket)
        _issue(chat)

        socket.send_json({"query": "still allowed?"})
        assert socket.receive_json() == {
            "type": "error",
            "detail": "This socket token was rotated. Connect again with the new token.",
        }
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == CLOSE_UNAUTHORIZED


def test_socket_token_cannot_open_another_business_socket(chat):
    token = _issue(chat)["token"]
    code, _ = _close_code(chat["client"], _url(uuid.uuid7()), headers={"Authorization": f"Bearer {token}"})
    assert code == CLOSE_FORBIDDEN


def test_unknown_socket_token_is_rejected(chat):
    code, reason = _close_code(chat["client"], f"{_url(chat['business_id'])}?token=rve_sk_madeup")
    assert (code, reason) == (CLOSE_UNAUTHORIZED, "Invalid or rotated socket token")


def test_socket_token_is_not_an_api_access_token(chat):
    token = _issue(chat)["token"]
    assert chat["client"].get("/v1/documents", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize(("method", "path"), [("get", "/v1/socket"), ("post", "/v1/socket/token")])
def test_socket_management_requires_login(chat, method: str, path: str):
    assert getattr(chat["client"], method)(path).status_code == 401
