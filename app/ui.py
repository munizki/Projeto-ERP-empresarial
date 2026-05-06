import os
from urllib.parse import urlparse

from fastapi.templating import Jinja2Templates

from app.security import CSRF_COOKIE_NAME, SESSION_IDLE_TIMEOUT_SECONDS
from app.services.contexto_acoes import (
    caixa_action_bundle,
    can_use_advanced_mode,
    estoque_action_bundle,
    get_caixa_hidrometros,
    get_instalador_hidrometros,
    hidrometro_action_bundle,
    humanize_status,
    is_admin,
    solicitacao_action_bundle,
)
from app.services.regras_caixa import quantidade_esperada_caixa
from app.services.manutencao_hidrometros import manutencoes_caixa
from app.utils import APP_TIMEZONE, format_datetime, summarize_user_agent


def safe_back_href(request, fallback: str = "/dashboard") -> str:
    if request is None:
        return fallback

    referer = (request.headers.get("referer") or "").strip()
    if not referer:
        return fallback

    parsed_referer = urlparse(referer)
    parsed_current = urlparse(str(request.url))
    if parsed_referer.netloc and parsed_current.netloc and parsed_referer.netloc != parsed_current.netloc:
        return fallback

    destino = parsed_referer.path or fallback
    if destino == request.url.path:
        return fallback
    if parsed_referer.query:
        destino = f"{destino}?{parsed_referer.query}"
    return destino


templates = Jinja2Templates(directory="app/templates")
templates.env.filters["datetime_br"] = lambda value: format_datetime(value, with_seconds=False)
templates.env.filters["datetime_br_seconds"] = lambda value: format_datetime(value, with_seconds=True)
templates.env.filters["browser_label"] = summarize_user_agent
templates.env.globals["ASSET_VERSION"] = os.getenv("APP_ASSET_VERSION", "20260506-confirmacoes")
templates.env.globals["CSRF_COOKIE_NAME"] = CSRF_COOKIE_NAME
templates.env.globals["SESSION_IDLE_TIMEOUT_SECONDS"] = SESSION_IDLE_TIMEOUT_SECONDS
templates.env.globals["APP_TIMEZONE"] = APP_TIMEZONE
templates.env.globals["safe_back_href"] = safe_back_href
templates.env.globals["humanize_status"] = humanize_status
templates.env.globals["caixa_action_bundle"] = caixa_action_bundle
templates.env.globals["hidrometro_action_bundle"] = hidrometro_action_bundle
templates.env.globals["solicitacao_action_bundle"] = solicitacao_action_bundle
templates.env.globals["estoque_action_bundle"] = estoque_action_bundle
templates.env.globals["can_use_advanced_mode"] = can_use_advanced_mode
templates.env.globals["is_admin"] = is_admin
templates.env.globals["get_caixa_hidrometros"] = get_caixa_hidrometros
templates.env.globals["get_instalador_hidrometros"] = get_instalador_hidrometros
templates.env.globals["quantidade_esperada_caixa"] = quantidade_esperada_caixa
templates.env.globals["manutencoes_caixa"] = manutencoes_caixa
