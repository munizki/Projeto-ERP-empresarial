import os

from fastapi import Request, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy import inspect
from starlette.datastructures import MutableHeaders
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.database import SessionLocal, engine
from app.models import UserRole, Usuario
from app.security import (
    CSRF_COOKIE_NAME,
    csrf_token_valido,
    get_allowed_hosts,
    hash_senha,
    is_production_env,
    obter_ou_gerar_csrf_token,
    runtime_security_errors,
    validar_politica_senha,
)


SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "False").strip().lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "strict").strip().lower() or "strict"
FORCE_HTTPS = os.getenv("FORCE_HTTPS", "False").strip().lower() == "true"
CSRF_COOKIE_MAX_AGE = int(os.getenv("CSRF_COOKIE_MAX_AGE", "28800"))
SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))


def _should_validate_csrf(request: Request) -> bool:
    if request.method.upper() in SAFE_METHODS:
        return False
    if request.url.path.startswith("/static/"):
        return False
    return True


def _request_is_secure(request: Request) -> bool:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded_proto == "https"


def _build_receive_from_body(body: bytes, disconnect_message: Message | None = None) -> Receive:
    sent = False
    disconnected = False

    async def receive() -> Message:
        nonlocal sent, disconnected
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        if disconnect_message and not disconnected:
            disconnected = True
            return disconnect_message
        return {"type": "http.disconnect"}

    return receive


def _build_disconnect_receive(disconnect_message: Message | None = None) -> Receive:
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if disconnect_message and not sent:
            sent = True
            return disconnect_message
        return {"type": "http.disconnect"}

    return receive


async def _csrf_token_from_request(request: Request) -> str | None:
    header_token = (request.headers.get("x-csrf-token") or "").strip()
    if header_token:
        return header_token

    content_type = (request.headers.get("content-type") or "").lower()
    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        try:
            form = await request.form()
        except Exception:
            return None
        token = form.get("csrf_token")
        return str(token).strip() if token else None
    return None


def _csrf_failure_response(request: Request, detail: str) -> Response:
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept:
        from app.ui import templates

        return templates.TemplateResponse(
            "shared/erro.html",
            {"request": request, "status_code": 403, "detalhe": detail},
            status_code=403,
        )
    return PlainTextResponse(detail, status_code=403)


async def enforce_csrf(request: Request) -> Response | None:
    if not _should_validate_csrf(request):
        return None

    cookie_token = (request.cookies.get(CSRF_COOKIE_NAME) or "").strip()
    request_token = await _csrf_token_from_request(request)
    detail = "Sessao de formulario invalida. Atualize a pagina e tente novamente."
    if not cookie_token or not request_token:
        return _csrf_failure_response(request, detail)
    if not csrf_token_valido(cookie_token) or cookie_token != request_token:
        return _csrf_failure_response(request, detail)
    return None


def apply_security_headers(request: Request, response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
    )
    if _request_is_secure(request):
        response.headers.setdefault("Strict-Transport-Security", f"max-age={SECURE_HSTS_SECONDS}; includeSubDomains")
    if request.url.path.startswith("/auth/"):
        response.headers.setdefault("Cache-Control", "no-store")


def ensure_csrf_cookie(request: Request, response: Response) -> None:
    token = obter_ou_gerar_csrf_token(request.cookies.get(CSRF_COOKIE_NAME))
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,
        secure=COOKIE_SECURE,
        samesite=COOKIE_SAMESITE,
        path="/",
        max_age=CSRF_COOKIE_MAX_AGE,
    )


def _merge_runtime_headers(request: Request, headers: MutableHeaders) -> None:
    response = Response()
    ensure_csrf_cookie(request, response)
    apply_security_headers(request, response)
    for key_bytes, value_bytes in response.raw_headers:
        key = key_bytes.decode("latin-1")
        value = value_bytes.decode("latin-1")
        if key.lower() == "set-cookie":
            headers.append(key, value)
            continue
        if key not in headers:
            headers[key] = value


def validate_startup_security() -> None:
    errors = runtime_security_errors()
    inspector = inspect(engine)
    if is_production_env() and "usuarios" in inspector.get_table_names():
        with SessionLocal() as db:
            active_admin_count = db.query(Usuario).filter(
                Usuario.role == UserRole.ADMIN,
                Usuario.ativo == True,
            ).count()
            if active_admin_count == 0:
                bootstrap_email = (os.getenv("BOOTSTRAP_ADMIN_EMAIL") or "").strip().lower()
                bootstrap_password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or ""
                bootstrap_name = (os.getenv("BOOTSTRAP_ADMIN_NAME") or "Administrador").strip()
                if bootstrap_email and bootstrap_password:
                    senha_erro = validar_politica_senha(
                        bootstrap_password,
                        nome=bootstrap_name,
                        email=bootstrap_email,
                    )
                    if senha_erro:
                        errors.append(f"BOOTSTRAP_ADMIN_PASSWORD invalida: {senha_erro}")
                    else:
                        admin = Usuario(
                            nome=bootstrap_name,
                            email=bootstrap_email,
                            senha_hash=hash_senha(bootstrap_password),
                            role=UserRole.ADMIN,
                            ativo=True,
                        )
                        db.add(admin)
                        db.flush()
                        from app.services.auditoria import registrar_auditoria

                        registrar_auditoria(
                            db=db,
                            acao="BOOTSTRAP_ADMIN",
                            usuario_id=admin.id,
                            tabela="usuarios",
                            registro_id=admin.id,
                            descricao="Administrador inicial criado por variavel de ambiente",
                            dados_depois={"email": admin.email, "role": admin.role.value},
                        )
                        db.commit()
                        active_admin_count = 1
                else:
                    errors.append(
                        "Nenhum ADMIN ativo encontrado. Configure BOOTSTRAP_ADMIN_EMAIL e BOOTSTRAP_ADMIN_PASSWORD para o primeiro start."
                    )
        if active_admin_count != 1:
            errors.append("A producao precisa ter exatamente 1 administrador ativo.")

    if errors:
        raise RuntimeError("Configuracao de producao insegura: " + " | ".join(errors))


class RuntimeHardeningMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        body_chunks: list[bytes] = []
        disconnect_message: Message | None = None
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnect_message = message
                break
            if message["type"] != "http.request":
                continue
            body_chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break

        body = b"".join(body_chunks)
        csrf_request = Request(scope, receive=_build_receive_from_body(body, disconnect_message))
        csrf_response = await enforce_csrf(csrf_request)
        if csrf_response is not None:
            ensure_csrf_cookie(csrf_request, csrf_response)
            apply_security_headers(csrf_request, csrf_response)
            await csrf_response(scope, _build_disconnect_receive(disconnect_message), send)
            return

        header_request = Request(scope, receive=_build_disconnect_receive(disconnect_message))

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                _merge_runtime_headers(header_request, headers)
            await send(message)

        await self.app(scope, _build_receive_from_body(body, disconnect_message), send_wrapper)


def install_runtime_hardening(app) -> None:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_allowed_hosts())
    if FORCE_HTTPS:
        app.add_middleware(HTTPSRedirectMiddleware)
    app.add_middleware(RuntimeHardeningMiddleware)
