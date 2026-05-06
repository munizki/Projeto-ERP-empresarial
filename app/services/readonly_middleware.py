from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.database import SessionLocal
from app.services.operational_mode import is_operational_mutation_path, modo_leitura_status


MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class OperationalReadOnlyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        if method not in MUTATING_METHODS or not is_operational_mutation_path(path):
            await self.app(scope, receive, send)
            return

        try:
            with SessionLocal() as db:
                status = modo_leitura_status(db)
        except Exception:
            await self.app(scope, receive, send)
            return

        if not status["ativo"]:
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        motivo = status.get("motivo") or "Operacao pausada para protecao dos dados."
        response = HTMLResponse(
            content=(
                "<!DOCTYPE html><html lang='pt-BR'><head><meta charset='UTF-8'>"
                "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
                "<link rel='stylesheet' href='/static/css/style.css'>"
                "<title>Modo leitura</title></head>"
                "<body style='background:var(--gray-100);display:flex;align-items:center;justify-content:center;min-height:100vh'>"
                "<main class='card' style='max-width:520px;padding:32px;text-align:center'>"
                "<div style='font-size:54px;margin-bottom:12px'>&#128274;</div>"
                "<h1>Modo leitura ativo</h1>"
                f"<p class='text-muted' style='margin:12px 0 22px'>{motivo}</p>"
                "<a href='/dashboard' class='btn btn-primary'>Voltar ao painel</a>"
                "</main></body></html>"
            ),
            status_code=423,
        )
        await response(scope, request.receive, send)
