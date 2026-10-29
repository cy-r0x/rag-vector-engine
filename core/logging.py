import logging

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(level=level.upper(), format=LOG_FORMAT)
    logging.getLogger().setLevel(level.upper())
    # Requests are logged by RequestLoggingMiddleware; avoid a second line per request.
    logging.getLogger("uvicorn.access").disabled = True
