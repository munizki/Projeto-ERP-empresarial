import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Usuario
from app.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    SESSION_IDLE_TIMEOUT_SECONDS,
    atualizar_hash_se_necessario,
    buscar_usuario_por_token,
    criar_token,
    hash_senha,
    limpar_login_falhas,
    login_bloqueado,
    login_rate_limit_key,
    registrar_login_falha,
    requer_autenticacao,
    validar_politica_senha,
    verificar_senha,
)
from app.services.auditoria import registrar_auditoria
from app.ui import templates
from app.utils import get_request_ip, normalize_text, utc_now


router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE_NAME = "access_token"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "False").strip().lower() == "true"
COOKIE_SAMESITE = os.getenv("COOKIE_SAMESITE", "strict").strip().lower() or "strict"


def _set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path="/",
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


def _clear_auth_cookie(response: RedirectResponse) -> None:
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, expirou: int = 0, desconectado_admin: int = 0, limite_sessao: int = 0, sessao_invalidada: int = 0):
    mensagem = None
    if expirou:
        mensagem = "Sessao encerrada por inatividade. Entre novamente para continuar."
    elif desconectado_admin:
        mensagem = "O administrador desconectou sua sessao para manutencao programada do sistema. Entre novamente quando a operacao for liberada."
    elif limite_sessao:
        mensagem = "Limite de conta conectada excedido: sua conta foi aberta em outro computador ou celular. Por seguranca, esta sessao foi encerrada."
    elif sessao_invalidada:
        mensagem = "Sua sessao anterior foi encerrada por uma atualizacao de seguranca. Entre novamente para continuar."
    return templates.TemplateResponse("auth/login.html", {"request": request, "mensagem": mensagem})


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    db: Session = Depends(get_db),
):
    email_limpo = normalize_text(email).lower()
    client_ip = get_request_ip(request)
    rate_key = login_rate_limit_key(email_limpo, client_ip)
    bloqueio_restante = login_bloqueado(rate_key)
    if bloqueio_restante > 0:
        registrar_auditoria(
            db=db,
            acao="LOGIN_BLOQUEADO",
            tabela="usuarios",
            descricao="Tentativa de login bloqueada por excesso de falhas",
            dados_depois={"email": email_limpo, "segundos_restantes": bloqueio_restante},
            ip=client_ip,
        )
        db.commit()
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "erro": f"Acesso temporariamente bloqueado. Aguarde {bloqueio_restante} segundo(s)."},
            status_code=429,
        )

    usuario = db.query(Usuario).filter(Usuario.email == email_limpo).first()

    if not usuario or not verificar_senha(senha, usuario.senha_hash) or not usuario.ativo:
        bloqueio_restante = registrar_login_falha(rate_key)
        registrar_auditoria(
            db=db,
            acao="LOGIN_USUARIO_INATIVO" if usuario and not usuario.ativo else "LOGIN_FALHA",
            usuario_id=usuario.id if usuario else None,
            tabela="usuarios",
            registro_id=usuario.id if usuario else None,
            descricao="Tentativa de login invalida" if not usuario or usuario.ativo else "Tentativa de login com usuario inativo",
            dados_depois={"email": email_limpo, "bloqueio_restante": bloqueio_restante},
            ip=client_ip,
        )
        db.commit()
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "erro": "Email ou senha invalidos."},
            status_code=401,
        )

    limpar_login_falhas(rate_key)
    novo_hash = atualizar_hash_se_necessario(senha, usuario.senha_hash)
    if novo_hash:
        usuario.senha_hash = novo_hash
    role = getattr(usuario.role, "value", usuario.role)
    sessao_unica_aplicada = role != "admin"
    if sessao_unica_aplicada:
        usuario.token_version = int(usuario.token_version or 0) + 1
        usuario.session_notice_code = "SESSION_LIMIT_EXCEEDED"
        usuario.session_notice_message = "Limite de conta conectada excedido. Esta conta foi aberta em outro dispositivo."
        usuario.session_notice_at = utc_now()
    else:
        usuario.session_notice_code = None
        usuario.session_notice_message = None
        usuario.session_notice_at = None
    usuario.ultimo_acesso = utc_now()
    registrar_auditoria(
        db=db,
        acao="LOGIN_SUCESSO",
        usuario_id=usuario.id,
        tabela="usuarios",
        registro_id=usuario.id,
        descricao="Login realizado com sucesso",
        dados_depois={
            "sessao_unica_aplicada": sessao_unica_aplicada,
            "token_version": int(usuario.token_version or 0),
        },
        ip=client_ip,
    )
    if sessao_unica_aplicada:
        registrar_auditoria(
            db=db,
            acao="SESSAO_UNICA_LOGIN",
            usuario_id=usuario.id,
            tabela="usuarios",
            registro_id=usuario.id,
            descricao="Novo login invalidou sessoes anteriores deste usuario.",
            dados_depois={"token_version": int(usuario.token_version or 0), "role": role},
            ip=client_ip,
            categoria="SEGURANCA",
        )
    db.commit()

    destino = "/instalador/entregas" if role == "instalador" else "/dashboard"
    response = RedirectResponse(url=destino, status_code=302)
    _set_auth_cookie(response, criar_token({"sub": str(usuario.id), "ver": int(usuario.token_version or 0)}))
    return response


@router.get("/session-ping")
async def session_ping(
    request: Request,
    usuario=Depends(requer_autenticacao),
):
    role = getattr(usuario.role, "value", usuario.role)
    response = JSONResponse(
        {
            "authenticated": True,
            "role": role,
            "idle_timeout_seconds": None if role == "admin" else SESSION_IDLE_TIMEOUT_SECONDS,
        }
    )
    if role != "admin":
        _set_auth_cookie(response, criar_token({"sub": str(usuario.id), "ver": int(usuario.token_version or 0)}))
    return response


@router.get("/trocar-senha", response_class=HTMLResponse)
async def trocar_senha_page(
    request: Request,
    usuario=Depends(requer_autenticacao),
):
    return templates.TemplateResponse(
        "auth/trocar_senha.html",
        {"request": request, "usuario": usuario},
    )


@router.post("/trocar-senha", response_class=HTMLResponse)
async def trocar_senha(
    request: Request,
    senha_atual: str = Form(...),
    nova_senha: str = Form(...),
    confirmar_senha: str = Form(...),
    usuario=Depends(requer_autenticacao),
    db: Session = Depends(get_db),
):
    if not verificar_senha(senha_atual, usuario.senha_hash):
        return templates.TemplateResponse(
            "auth/trocar_senha.html",
            {"request": request, "usuario": usuario, "erro": "A senha atual esta incorreta."},
            status_code=400,
        )

    if nova_senha != confirmar_senha:
        return templates.TemplateResponse(
            "auth/trocar_senha.html",
            {"request": request, "usuario": usuario, "erro": "A confirmacao da nova senha nao confere."},
            status_code=400,
        )

    if verificar_senha(nova_senha, usuario.senha_hash):
        return templates.TemplateResponse(
            "auth/trocar_senha.html",
            {"request": request, "usuario": usuario, "erro": "A nova senha precisa ser diferente da atual."},
            status_code=400,
        )

    erro_politica = validar_politica_senha(nova_senha, nome=usuario.nome, email=usuario.email)
    if erro_politica:
        return templates.TemplateResponse(
            "auth/trocar_senha.html",
            {"request": request, "usuario": usuario, "erro": erro_politica},
            status_code=400,
        )

    usuario.senha_hash = hash_senha(nova_senha)
    usuario.token_version = int(usuario.token_version or 0) + 1
    usuario.session_notice_code = "SECURITY_UPDATE"
    usuario.session_notice_message = "Sessao encerrada por atualizacao de seguranca da conta."
    usuario.session_notice_at = utc_now()
    registrar_auditoria(
        db=db,
        acao="TROCA_SENHA",
        usuario_id=usuario.id,
        tabela="usuarios",
        registro_id=usuario.id,
        descricao="Senha alterada pelo proprio usuario",
        ip=get_request_ip(request),
    )
    db.commit()

    response = templates.TemplateResponse(
        "auth/trocar_senha.html",
        {"request": request, "usuario": usuario, "sucesso": "Senha atualizada com sucesso."},
    )
    _set_auth_cookie(response, criar_token({"sub": str(usuario.id), "ver": int(usuario.token_version or 0)}))
    return response


@router.get("/logout")
@router.post("/logout")
async def logout(
    request: Request,
    db: Session = Depends(get_db),
):
    usuario = await buscar_usuario_por_token(
        request,
        db,
        verificar_inatividade=False,
        atualizar_atividade=False,
    )
    motivo = (request.query_params.get("motivo") or "").strip().lower()
    inatividade = motivo == "inatividade"
    registrar_auditoria(
        db=db,
        acao="LOGOUT_INATIVIDADE" if inatividade else "LOGOUT",
        usuario_id=usuario.id if usuario else None,
        tabela="usuarios",
        registro_id=usuario.id if usuario else None,
        descricao="Logout automatico por inatividade" if inatividade else "Logout realizado",
        ip=get_request_ip(request),
    )
    db.commit()

    response = RedirectResponse(url="/auth/login?expirou=1" if inatividade else "/auth/login", status_code=302)
    _clear_auth_cookie(response)
    return response
