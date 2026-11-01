import logging
import re

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

_TOKEN_IN_URL = re.compile(r"([?&]token=)[^&\s\"']+")


class RedactTokensFilter(logging.Filter):
    """Masks `token=` query values (e.g. in uvicorn's WebSocket handshake lines) so credentials never reach logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_IN_URL.sub(r"\1[redacted]", message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT)
    logging.getLogger().setLevel(level.upper())
    # Requests are logged by RequestLoggingMiddleware; avoid a second line per request.
    logging.getLogger("uvicorn.access").disabled = True
    # HTTP clients of Qdrant and Gemini log every request at INFO.
    for name in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLogger().level))
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactTokensFilter) for f in logger.filters):
            logger.addFilter(RedactTokensFilter())
