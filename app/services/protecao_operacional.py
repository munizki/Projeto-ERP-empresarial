from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import defaultdict, deque
from typing import Deque

from fastapi import Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.database import SessionLocal
from app.models import AuditoriaLog
from app.security import decodificar_token
from app.services.auditoria import registrar_auditoria
from app.services.operational_mode import is_operational_mutation_path
from app.utils import get_request_ip


MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SUSPICIOUS_WINDOW_SECONDS = int(os.getenv("SUSPICIOUS_OPERATION_WINDOW_SECONDS", "60"))
SUSPICIOUS_MAX_PER_WINDOW = int(os.getenv("SUSPICIOUS_OPERATION_MAX_PER_WINDOW", "35"))
HARD_RATE_MAX_PER_WINDOW = int(os.getenv("HARD_OPERATION_MAX_PER_WINDOW", "80"))

_operation_lock = threading.Lock()
_operation_windows: dict[str, Deque[float]] = defaultdict(deque)
_last_suspicious_log: dict[str, float] = {}


def _hash_value(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _extract_user_id_from_cookie(scope: Scope) -> int | None:
    headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
    cookie_header = headers.get("cookie") or ""
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "access_token" and value:
            payload = decodificar_token(value)
            if not payload:
                return None
            try:
                return int(payload.get("sub"))
            except (TypeError, ValueError):
                return None
    return None


def _rate_key(scope: Scope, user_id: int | None) -> str:
    client = scope.get("client")
    ip = client[0] if client else "-"
    return f"user:{user_id}" if user_id else f"ip:{_hash_value(ip)}"


def _record_operation_attempt(scope: Scope, user_id: int | None) -> tuple[bool, bool, int]:
    now = time.time()
    key = _rate_key(scope, user_id)
    with _operation_lock:
        window = _operation_windows[key]
        while window and now - window[0] > SUSPICIOUS_WINDOW_SECONDS:
            window.popleft()
        window.append(now)
        count = len(window)
        hard_block = count > HARD_RATE_MAX_PER_WINDOW
        suspicious = count > SUSPICIOUS_MAX_PER_WINDOW
    return hard_block, suspicious, count


def registrar_bloqueio_operacional(
    *,
    usuario_id: int | None,
    path: str,
    metodo: str,
    motivo: str,
    ip: str | None = None,
    status_code: int | None = None,
    tabela: str | None = None,
    registro_id: int | None = None,
    severidade: str = "SUSPEITO",
) -> None:
    try:
        with SessionLocal() as db:
            registrar_auditoria(
                db=db,
                acao="VALIDACAO_OPERACIONAL_BLOQUEADA",
                usuario_id=usuario_id,
                tabela=tabela,
                registro_id=registro_id,
                descricao=motivo,
                dados_depois={
                    "path": path,
                    "metodo": metodo,
                    "status_code": status_code,
                    "motivo": motivo,
                },
                ip=ip,
                severidade=severidade,
                categoria="SEGURANCA",
            )
            db.commit()
    except Exception:
        return


def _registrar_requisicao_bloqueada(
    *,
    scope: Scope,
    user_id: int | None,
    status_code: int,
    motivo: str,
) -> None:
    client = scope.get("client")
    ip = client[0] if client else None
    try:
        with SessionLocal() as db:
            registrar_auditoria(
                db=db,
                acao="REQUISICAO_BLOQUEADA",
                usuario_id=user_id,
                tabela="http",
                descricao=motivo,
                dados_depois={
                    "path": scope.get("path"),
                    "metodo": scope.get("method"),
                    "status_code": status_code,
                },
                ip=ip,
                severidade="SUSPEITO",
                categoria="SEGURANCA",
            )
            db.commit()
    except Exception:
        return


def _registrar_operacao_suspeita(scope: Scope, user_id: int | None, count: int) -> None:
    key = _rate_key(scope, user_id)
    now = time.time()
    with _operation_lock:
        last = _last_suspicious_log.get(key, 0)
        if now - last < SUSPICIOUS_WINDOW_SECONDS:
            return
        _last_suspicious_log[key] = now

    client = scope.get("client")
    ip = client[0] if client else None
    try:
        with SessionLocal() as db:
            registrar_auditoria(
                db=db,
                acao="OPERACAO_SUSPEITA_SEQUENCIA",
                usuario_id=user_id,
                tabela="http",
                descricao="Muitas operacoes em sequencia foram detectadas.",
                dados_depois={
                    "path": scope.get("path"),
                    "metodo": scope.get("method"),
                    "janela_segundos": SUSPICIOUS_WINDOW_SECONDS,
                    "total": count,
                },
                ip=ip,
                severidade="SUSPEITO",
                categoria="SEGURANCA",
            )
            db.commit()
    except Exception:
        return


def _html_rate_limit() -> HTMLResponse:
    return HTMLResponse(
        content=(
            "<!DOCTYPE html><html lang='pt-BR'><head><meta charset='UTF-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
            "<link rel='stylesheet' href='/static/css/style.css'>"
            "<title>Protecao ativa</title></head>"
            "<body style='background:var(--gray-100);display:flex;align-items:center;justify-content:center;min-height:100vh'>"
            "<main class='card' style='max-width:560px;padding:32px;text-align:center'>"
            "<div style='font-size:54px;margin-bottom:12px'>&#128737;</div>"
            "<h1>Protecao operacional ativa</h1>"
            "<p class='text-muted' style='margin:12px 0 22px'>Muitas operacoes foram enviadas em pouco tempo. Aguarde alguns instantes e tente novamente.</p>"
            "<a href='/dashboard' class='btn btn-primary'>Voltar ao painel</a>"
            "</main></body></html>"
        ),
        status_code=429,
    )


class OperationalProtectionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        should_track = method in MUTATING_METHODS and (
            is_operational_mutation_path(path) or path.startswith("/admin")
        )
        if not should_track:
            await self.app(scope, receive, send)
            return

        user_id = _extract_user_id_from_cookie(scope)
        hard_block, suspicious, count = _record_operation_attempt(scope, user_id)
        if suspicious:
            _registrar_operacao_suspeita(scope, user_id, count)
        if hard_block:
            _registrar_requisicao_bloqueada(
                scope=scope,
                user_id=user_id,
                status_code=429,
                motivo="Requisicao bloqueada por excesso de operacoes em sequencia.",
            )
            request = Request(scope, receive=receive)
            response = _html_rate_limit()
            await response(scope, request.receive, send)
            return

        status_holder = {"status": 200}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message.get("status", 200))
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                if "X-Operational-Protection" not in headers:
                    headers["X-Operational-Protection"] = "active"
            await send(message)

        await self.app(scope, receive, send_wrapper)

        status_code = int(status_holder.get("status", 200))
        if status_code >= 400:
            _registrar_requisicao_bloqueada(
                scope=scope,
                user_id=user_id,
                status_code=status_code,
                motivo="Requisicao mutavel bloqueada por validacao ou permissao.",
            )
