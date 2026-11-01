import logging

from common.logger.logger import RedactTokensFilter


def _record(msg: str, *args) -> logging.LogRecord:
    return logging.LogRecord("uvicorn.error", logging.INFO, __file__, 1, msg, args, None)


def test_token_query_values_are_redacted():
    record = _record('%s - "WebSocket %s" [accepted]', "127.0.0.1:1", "/v1/chat/ws/biz?token=eyJ.abc.def&x=1")

    assert RedactTokensFilter().filter(record) is True
    assert record.getMessage() == '127.0.0.1:1 - "WebSocket /v1/chat/ws/biz?token=[redacted]&x=1" [accepted]'


def test_messages_without_tokens_are_untouched():
    record = _record("Chat socket opened for business %s", "biz")

    RedactTokensFilter().filter(record)

    assert (record.msg, record.args) == ("Chat socket opened for business %s", ("biz",))
