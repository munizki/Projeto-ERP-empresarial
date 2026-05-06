from __future__ import annotations

from contextvars import ContextVar

from fastapi import Request
from starlette.types import ASGIApp, Receive, Scope, Send


_current_request: ContextVar[Request | None] = ContextVar("current_request", default=None)


def get_current_request() -> Request | None:
    return _current_request.get()


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        token = _current_request.set(request)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_request.reset(token)
