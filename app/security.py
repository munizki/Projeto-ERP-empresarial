import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Usuario
from app.utils import utc_now


SECRET_KEY = os.getenv("SECRET_KEY", "chave-padrao-troque-em-producao")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "480"))
SESSION_IDLE_TIMEOUT_MINUTES = int(os.getenv("SESSION_IDLE_TIMEOUT_MINUTES", "30"))
SESSION_IDLE_TIMEOUT_SECONDS = max(SESSION_IDLE_TIMEOUT_MINUTES, 1) * 60
SESSION_ACTIVITY_UPDATE_SECONDS = int(os.getenv("SESSION_ACTIVITY_UPDATE_SECONDS", "60"))
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "12"))
CSRF_COOKIE_NAME = os.getenv("CSRF_COOKIE_NAME", "csrf_token")
FAILED_LOGIN_WINDOW_SECONDS = int(os.getenv("FAILED_LOGIN_WINDOW_SECONDS", "900"))
FAILED_LOGIN_MAX_ATTEMPTS = int(os.getenv("FAILED_LOGIN_MAX_ATTEMPTS", "5"))
FAILED_LOGIN_LOCKOUT_SECONDS = int(os.getenv("FAILED_LOGIN_LOCKOUT_SECONDS", "900"))

INSECURE_SECRET_VALUES = {
    "",
    "chave-padrao-troque-em-producao",
    "mude-esta-chave-em-producao",
    "mude-esta-chave-secreta-em-producao-use-openssl-rand-hex-32",
}

pwd_context = CryptContext(
    schemes=["argon2", "bcrypt"],
    deprecated="auto",
    bcrypt__rounds=12,
)

_login_attempts_lock = threading.Lock()
_login_attempts: dict[str, dict[str, float]] = {}
_redis_client = None
_redis_checked = False


def is_production_env() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() in {"prod", "production"}


def get_allowed_hosts() -> list[str]:
    raw = os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").strip()
    hosts = [item.strip() for item in raw.split(",") if item.strip()]
    return hosts or ["localhost", "127.0.0.1", "testserver"]


def runtime_security_errors() -> list[str]:
    errors: list[str] = []
    if not is_production_env():
        return errors

    if SECRET_KEY in INSECURE_SECRET_VALUES or len(SECRET_KEY) < 32:
        errors.append("SECRET_KEY insegura para producao.")

    if os.getenv("COOKIE_SECURE", "False").strip().lower() != "true":
        errors.append("COOKIE_SECURE precisa estar como True em producao.")

    raw_allowed_hosts = os.getenv("ALLOWED_HOSTS", "").strip()
    if not raw_allowed_hosts:
        errors.append("ALLOWED_HOSTS precisa ser configurado em producao.")
    elif "*" in {item.strip() for item in raw_allowed_hosts.split(",") if item.strip()}:
        errors.append("ALLOWED_HOSTS nao pode usar '*' em producao.")

    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if not redis_url:
        errors.append("REDIS_URL precisa ser configurado em producao para rate limit compartilhado.")
    elif _get_redis_client() is None:
        errors.append("Redis de rate limit indisponivel ou biblioteca redis nao instalada.")

    return errors


def _redis_key(prefix: str, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"gmf:{prefix}:{digest}"


def _get_redis_client():
    global _redis_client, _redis_checked
    redis_url = (os.getenv("REDIS_URL") or "").strip()
    if not redis_url:
        return None
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
        client.ping()
        _redis_client = client
    except Exception:
        _redis_client = None
    return _redis_client


def _redis_login_bloqueado(key: str) -> Optional[int]:
    client = _get_redis_client()
    if client is None:
        return None
    ttl = client.ttl(_redis_key("login:block", key))
    if ttl is None or int(ttl) <= 0:
        return 0
    return int(ttl)


def _redis_registrar_login_falha(key: str) -> Optional[int]:
    client = _get_redis_client()
    if client is None:
        return None
    block_key = _redis_key("login:block", key)
    ttl = client.ttl(block_key)
    if ttl is not None and int(ttl) > 0:
        return int(ttl)

    fail_key = _redis_key("login:fail", key)
    count = int(client.incr(fail_key))
    if count == 1:
        client.expire(fail_key, FAILED_LOGIN_WINDOW_SECONDS)
    if count >= FAILED_LOGIN_MAX_ATTEMPTS:
        client.setex(block_key, FAILED_LOGIN_LOCKOUT_SECONDS, "1")
        client.delete(fail_key)
        return FAILED_LOGIN_LOCKOUT_SECONDS
    return 0


def _redis_limpar_login_falhas(key: str) -> bool:
    client = _get_redis_client()
    if client is None:
        return False
    client.delete(_redis_key("login:block", key), _redis_key("login:fail", key))
    return True


def normalizar_senha_legacy(senha: str) -> str:
    return hashlib.sha256((senha or "").encode("utf-8")).hexdigest()


def validar_politica_senha(senha: str, *, nome: str = "", email: str = "") -> Optional[str]:
    senha = senha or ""
    if len(senha) < PASSWORD_MIN_LENGTH:
        return f"A senha deve ter pelo menos {PASSWORD_MIN_LENGTH} caracteres."
    if not re.search(r"[a-z]", senha):
        return "A senha precisa ter ao menos uma letra minuscula."
    if not re.search(r"[A-Z]", senha):
        return "A senha precisa ter ao menos uma letra maiuscula."
    if not re.search(r"\d", senha):
        return "A senha precisa ter ao menos um numero."
    if not re.search(r"[^A-Za-z0-9]", senha):
        return "A senha precisa ter ao menos um caractere especial."

    senha_lower = senha.lower()
    nome_limpo = re.sub(r"\s+", "", (nome or "").lower())
    email_local = (email or "").split("@", 1)[0].lower()
    for trecho in {nome_limpo, email_local}:
        if trecho and len(trecho) >= 4 and trecho in senha_lower:
            return "A senha nao pode conter partes evidentes do nome ou do email."

    return None


def hash_senha(senha: str) -> str:
    return pwd_context.hash(senha or "")


def verificar_senha(senha_plain: str, senha_hash: str) -> bool:
    if not senha_hash:
        return False

    if senha_hash.startswith("$argon2"):
        try:
            return pwd_context.verify(senha_plain or "", senha_hash)
        except Exception:
            return False

    senha_normalizada = normalizar_senha_legacy(senha_plain or "")
    try:
        if pwd_context.verify(senha_normalizada, senha_hash):
            return True
    except Exception:
        pass

    try:
        if pwd_context.verify((senha_plain or "")[:72], senha_hash):
            return True
    except Exception:
        pass

    return False


def precisa_atualizar_hash(senha_hash: str) -> bool:
    if not senha_hash:
        return True
    if not senha_hash.startswith("$argon2"):
        return True
    return pwd_context.needs_update(senha_hash)


def atualizar_hash_se_necessario(senha_plain: str, senha_hash: str) -> Optional[str]:
    if not verificar_senha(senha_plain, senha_hash):
        return None
    if precisa_atualizar_hash(senha_hash):
        return hash_senha(senha_plain)
    return None


def criar_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire, "iat": datetime.utcnow(), "type": "access"})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def decodificar_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        return None


def _usuario_is_admin(usuario: Usuario) -> bool:
    return getattr(usuario.role, "value", usuario.role) == "admin"


def _login_redirect_location(request: Request) -> str:
    if getattr(request.state, "session_expired_by_idle", False):
        return "/auth/login?expirou=1"
    invalid_reason = getattr(request.state, "session_invalidated_reason", "")
    if invalid_reason == "admin_disconnect":
        return "/auth/login?desconectado_admin=1"
    if invalid_reason == "session_limit":
        return "/auth/login?limite_sessao=1"
    if invalid_reason == "generic":
        return "/auth/login?sessao_invalidada=1"
    return "/auth/login"


def login_redirect_location(request: Request) -> str:
    return _login_redirect_location(request)


def _atividade_expirada(usuario: Usuario, agora: datetime) -> bool:
    if _usuario_is_admin(usuario):
        return False
    if not usuario.ultimo_acesso:
        return False
    return (agora - usuario.ultimo_acesso).total_seconds() > SESSION_IDLE_TIMEOUT_SECONDS


def _deve_atualizar_atividade(usuario: Usuario, agora: datetime) -> bool:
    if _usuario_is_admin(usuario):
        return False
    if not usuario.ultimo_acesso:
        return True
    return (agora - usuario.ultimo_acesso).total_seconds() >= max(SESSION_ACTIVITY_UPDATE_SECONDS, 1)


def _auditar_sessao_expirada(db: Session, request: Request, usuario: Usuario, agora: datetime) -> None:
    try:
        from app.services.auditoria import registrar_auditoria
        from app.utils import get_request_ip

        registrar_auditoria(
            db=db,
            acao="SESSAO_EXPIRADA_INATIVIDADE",
            usuario_id=usuario.id,
            tabela="usuarios",
            registro_id=usuario.id,
            descricao="Sessao encerrada automaticamente por inatividade.",
            dados_depois={
                "path": request.url.path,
                "limite_minutos": SESSION_IDLE_TIMEOUT_MINUTES,
                "ultimo_acesso": usuario.ultimo_acesso.isoformat() if usuario.ultimo_acesso else None,
                "detectado_em": agora.isoformat(),
            },
            ip=get_request_ip(request),
            severidade="NORMAL",
            categoria="SEGURANCA",
        )
        db.commit()
    except Exception:
        db.rollback()


def _session_invalidada_reason(usuario: Usuario) -> str:
    code = str(getattr(usuario, "session_notice_code", "") or "").strip().upper()
    if code == "ADMIN_DISCONNECT":
        return "admin_disconnect"
    if code == "SESSION_LIMIT_EXCEEDED":
        return "session_limit"
    return "generic"


def _auditar_sessao_invalidada(db: Session, request: Request, usuario: Usuario, payload_version: int, reason: str) -> None:
    try:
        from app.services.auditoria import registrar_auditoria
        from app.utils import get_request_ip

        registrar_auditoria(
            db=db,
            acao="SESSAO_INVALIDADA_ADMIN" if reason == "admin_disconnect" else "SESSAO_INVALIDADA_LOGIN_POSTERIOR",
            usuario_id=usuario.id,
            tabela="usuarios",
            registro_id=usuario.id,
            descricao=(
                "Token bloqueado porque o administrador desconectou o usuario para manutencao."
                if reason == "admin_disconnect"
                else "Token bloqueado porque houve login posterior em outro navegador ou computador."
            ),
            dados_depois={
                "path": request.url.path,
                "token_version_cookie": payload_version,
                "token_version_atual": int(getattr(usuario, "token_version", 0) or 0),
                "session_notice_code": getattr(usuario, "session_notice_code", None),
            },
            ip=get_request_ip(request),
            severidade="SUSPEITO",
            categoria="SEGURANCA",
            resultado="BLOQUEADO",
        )
        db.commit()
    except Exception:
        db.rollback()


async def buscar_usuario_por_token(
    request: Request,
    db: Session,
    *,
    verificar_inatividade: bool = True,
    atualizar_atividade: bool = True,
) -> Optional[Usuario]:
    token = request.cookies.get("access_token")
    if not token:
        return None

    payload = decodificar_token(token)
    if not payload:
        return None
    if payload.get("type") != "access":
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None

    usuario = db.query(Usuario).filter(Usuario.id == user_id, Usuario.ativo == True).first()
    if not usuario:
        return None

    payload_version = payload.get("ver", 0)
    try:
        payload_version = int(payload_version)
    except (TypeError, ValueError):
        return None

    if payload_version != int(getattr(usuario, "token_version", 0) or 0):
        reason = _session_invalidada_reason(usuario)
        request.state.session_invalidated_reason = reason
        _auditar_sessao_invalidada(db, request, usuario, payload_version, reason)
        return None

    agora = utc_now()
    if verificar_inatividade and _atividade_expirada(usuario, agora):
        request.state.session_expired_by_idle = True
        _auditar_sessao_expirada(db, request, usuario, agora)
        return None

    if atualizar_atividade and _deve_atualizar_atividade(usuario, agora):
        try:
            usuario.ultimo_acesso = agora
            db.commit()
        except Exception:
            db.rollback()

    return usuario


def gerar_csrf_token() -> str:
    raw_token = secrets.token_urlsafe(32)
    signature = hmac.new(SECRET_KEY.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{raw_token}.{signature}"


def csrf_token_valido(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    raw_token, signature = token.rsplit(".", 1)
    expected = hmac.new(SECRET_KEY.encode("utf-8"), raw_token.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def obter_ou_gerar_csrf_token(token_atual: str | None) -> str:
    if csrf_token_valido(token_atual):
        return str(token_atual)
    return gerar_csrf_token()


def login_rate_limit_key(email: str, ip: str | None) -> str:
    return f"{(email or '').strip().lower()}|{ip or '-'}"


def login_bloqueado(key: str) -> int:
    redis_value = _redis_login_bloqueado(key)
    if redis_value is not None:
        return redis_value

    agora = time.time()
    with _login_attempts_lock:
        state = _login_attempts.get(key)
        if not state:
            return 0
        blocked_until = float(state.get("blocked_until", 0))
        if blocked_until <= agora:
            if agora - float(state.get("first_attempt", agora)) > FAILED_LOGIN_WINDOW_SECONDS:
                _login_attempts.pop(key, None)
                return 0
            return 0
        return max(int(blocked_until - agora), 1)


def registrar_login_falha(key: str) -> int:
    redis_value = _redis_registrar_login_falha(key)
    if redis_value is not None:
        return redis_value

    agora = time.time()
    with _login_attempts_lock:
        state = _login_attempts.get(key)
        if not state or agora - float(state.get("first_attempt", agora)) > FAILED_LOGIN_WINDOW_SECONDS:
            state = {"count": 0, "first_attempt": agora, "blocked_until": 0}

        if float(state.get("blocked_until", 0)) > agora:
            _login_attempts[key] = state
            return max(int(float(state["blocked_until"]) - agora), 1)

        state["count"] = int(state.get("count", 0)) + 1
        state["first_attempt"] = float(state.get("first_attempt", agora))
        if int(state["count"]) >= FAILED_LOGIN_MAX_ATTEMPTS:
            state["blocked_until"] = agora + FAILED_LOGIN_LOCKOUT_SECONDS
        _login_attempts[key] = state
        return max(int(float(state.get("blocked_until", 0)) - agora), 0)


def limpar_login_falhas(key: str) -> None:
    if _redis_limpar_login_falhas(key):
        return
    with _login_attempts_lock:
        _login_attempts.pop(key, None)


async def get_usuario_atual(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[Usuario]:
    return await buscar_usuario_por_token(request, db)


async def requer_autenticacao(
    request: Request,
    db: Session = Depends(get_db),
) -> Usuario:
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": _login_redirect_location(request)},
        )
    return usuario


def requer_role(*roles):
    async def verificar_role(
        request: Request,
        db: Session = Depends(get_db),
    ) -> Usuario:
        usuario = await get_usuario_atual(request, db)
        if not usuario:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": _login_redirect_location(request)},
            )

        user_role = getattr(usuario.role, "value", usuario.role)
        if user_role not in roles:
            try:
                from app.services.auditoria import registrar_auditoria
                from app.utils import get_request_ip

                registrar_auditoria(
                    db=db,
                    acao="ACESSO_NEGADO",
                    usuario_id=usuario.id,
                    tabela="usuarios",
                    registro_id=usuario.id,
                    descricao="Tentativa de acesso a area sem permissao.",
                    dados_depois={
                        "path": request.url.path,
                        "role": user_role,
                        "roles_permitidos": list(roles),
                    },
                    ip=get_request_ip(request),
                    severidade="SUSPEITO",
                    categoria="SEGURANCA",
                )
                db.commit()
            except Exception:
                db.rollback()
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso negado")
        return usuario

    return verificar_role
