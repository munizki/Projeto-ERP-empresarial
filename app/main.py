import logging
import os
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import Base, engine
from app.routers import admin, almoxarifado, auth, dashboard, instalador, manipulador
from app.security import is_production_env
from app.services.logging_config import configure_logging
from app.services.protecao_operacional import OperationalProtectionMiddleware
from app.services.request_context import RequestContextMiddleware
from app.services.readonly_middleware import OperationalReadOnlyMiddleware
from app.services.runtime_hardening import install_runtime_hardening, validate_startup_security
from app.services.schema import ensure_runtime_schema
from app.ui import templates


configure_logging()
logger = logging.getLogger("app.main")

# Em producao, o schema deve ser aplicado somente por Alembic.
if not is_production_env():
    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema()
validate_startup_security()

app = FastAPI(
    title=os.getenv("APP_NAME", "Sistema de Controle de Hidrometros"),
    version=os.getenv("APP_VERSION", "1.0.0"),
    docs_url="/docs" if os.getenv("DEBUG", "False") == "True" else None,
    redoc_url=None,
)
install_runtime_hardening(app)
app.add_middleware(RequestContextMiddleware)
app.add_middleware(OperationalReadOnlyMiddleware)
app.add_middleware(OperationalProtectionMiddleware)


@app.middleware("http")
async def clear_idle_expired_session_cookie(request: Request, call_next):
    response = await call_next(request)
    if getattr(request.state, "session_expired_by_idle", False):
        location = response.headers.get("location")
        if location == "/auth/login":
            response.headers["location"] = "/auth/login?expirou=1"
        response.delete_cookie(
            key="access_token",
            path="/",
            samesite=os.getenv("COOKIE_SAMESITE", "strict").strip().lower() or "strict",
            secure=os.getenv("COOKIE_SECURE", "False").strip().lower() == "true",
        )
    return response


app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(dashboard.router)
app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(almoxarifado.router)
app.include_router(manipulador.router)
app.include_router(instalador.router)


@app.get("/healthz")
async def healthcheck():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Healthcheck falhou")
        return JSONResponse({"status": "error", "database": "unavailable"}, status_code=503)
    return {"status": "ok", "database": "ok"}


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 303:
        from fastapi.responses import RedirectResponse

        response = RedirectResponse(url=exc.headers.get("Location", "/auth/login"), status_code=303)
        if getattr(request.state, "session_expired_by_idle", False):
            response.delete_cookie(
                key="access_token",
                path="/",
                samesite=os.getenv("COOKIE_SAMESITE", "strict").strip().lower() or "strict",
                secure=os.getenv("COOKIE_SECURE", "False").strip().lower() == "true",
            )
        return response

    return templates.TemplateResponse(
        "shared/erro.html",
        {"request": request, "status_code": exc.status_code, "detalhe": exc.detail},
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    error_id = uuid.uuid4().hex[:12].upper()
    logger.exception("Erro nao tratado %s em %s %s", error_id, request.method, request.url.path)
    try:
        from app.database import SessionLocal
        from app.security import decodificar_token
        from app.services.auditoria import registrar_auditoria

        usuario_id = None
        token = request.cookies.get("access_token")
        payload = decodificar_token(token) if token else None
        if payload and payload.get("sub"):
            try:
                usuario_id = int(payload.get("sub"))
            except (TypeError, ValueError):
                usuario_id = None
        with SessionLocal() as db:
            registrar_auditoria(
                db=db,
                acao="ERRO_CRITICO_PAGINA",
                usuario_id=usuario_id,
                tabela="http",
                descricao="Erro nao tratado capturado pelo handler global",
                dados_depois={
                    "error_id": error_id,
                    "path": request.url.path,
                    "metodo": request.method,
                    "erro": exc.__class__.__name__,
                },
                request=request,
                severidade="CRITICO",
                categoria="SISTEMA",
                resultado="FALHA",
            )
            db.commit()
    except Exception:
        logger.exception("Falha ao registrar auditoria do erro critico %s", error_id)
    return templates.TemplateResponse(
        "shared/erro.html",
        {
            "request": request,
            "status_code": 500,
            "detalhe": "Erro interno do servidor. A equipe tecnica ja tem o identificador deste erro.",
            "error_id": error_id,
        },
        status_code=500,
    )
