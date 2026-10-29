import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("api.request")


class RequestLoggingMiddleware:
    """Logs one line per HTTP request: method, path, status, duration, and business_id when authenticated.

    Add it last so it is the outermost middleware and also sees requests rejected by auth.
    The query string is never logged, since it may carry tokens.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            self._log(scope, 500, start, exc_info=True)
            raise
        self._log(scope, status_code, start)

    @staticmethod
    def _log(scope: Scope, status_code: int, start: float, exc_info: bool = False) -> None:
        duration_ms = (time.perf_counter() - start) * 1000
        payload = scope.get("state", {}).get("token_payload") or {}
        client = scope.get("client")
        level = logging.ERROR if status_code >= 500 else logging.WARNING if status_code >= 400 else logging.INFO
        logger.log(
            level,
            "%s %s -> %d (%.1f ms) client=%s business_id=%s",
            scope["method"],
            scope["path"],
            status_code,
            duration_ms,
            client[0] if client else "-",
            payload.get("business_id", "-"),
            exc_info=exc_info,
        )
