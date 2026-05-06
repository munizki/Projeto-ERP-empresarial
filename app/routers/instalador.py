from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.database import get_db
from app.models import (
    CarcacaMovimentacao,
    CarcacaTipoMovimento,
    CaixaHidrometro,
    InstalacaoHidrometro,
    Instalador,
    Solicitacao,
    SolicitacaoItemCaixa,
    SolicitacaoItemPeca,
    SolicitacaoStatus,
)
from app.security import requer_role
from app.services.auditoria import registrar_auditoria
from app.ui import templates
from app.utils import get_request_ip, normalize_text, utc_now


router = APIRouter(prefix="/instalador", tags=["instalador"])
instalador_dep = Depends(requer_role("instalador", "admin"))
CAIXA_HIDROMETROS_ATTR = getattr(CaixaHidrometro, "hidrômetros")


def _role_value(usuario) -> str:
    return getattr(getattr(usuario, "role", None), "value", getattr(usuario, "role", ""))


def _is_admin(usuario) -> bool:
    return _role_value(usuario) == "admin"


def _admin_instalador_id(request: Request) -> int | None:
    raw = normalize_text(request.query_params.get("instalador_id"))
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="Instalador informado invalido.")


def _portal_query(usuario, instalador: Instalador) -> str:
    return f"?instalador_id={instalador.id}" if _is_admin(usuario) else ""


def _portal_redirect(path: str, usuario, instalador: Instalador, flag: str | None = None) -> str:
    query = _portal_query(usuario, instalador)
    if not flag:
        return f"{path}{query}"
    separator = "&" if query else "?"
    return f"{path}{query}{separator}{flag}=1"


def _portal_context(request: Request, usuario, instalador: Instalador, **extra) -> dict:
    context = {
        "request": request,
        "usuario": usuario,
        "instalador": instalador,
        "admin_preview": _is_admin(usuario),
        "portal_query": _portal_query(usuario, instalador),
    }
    context.update(extra)
    return context


def _instalador_do_usuario(db: Session, request: Request, usuario) -> Instalador:
    if _is_admin(usuario):
        instalador_id = _admin_instalador_id(request)
        if not instalador_id:
            raise HTTPException(status_code=400, detail="Selecione um instalador para visualizar o portal.")
        instalador = db.query(Instalador).filter(
            Instalador.id == instalador_id,
            Instalador.ativo == True,
        ).first()
        if not instalador:
            raise HTTPException(status_code=404, detail="Instalador nao encontrado.")
        return instalador

    instalador = db.query(Instalador).filter(
        Instalador.usuario_id == usuario.id,
        Instalador.ativo == True,
    ).first()
    if instalador:
        return instalador

    registrar_auditoria(
        db=db,
        acao="INSTALADOR_SEM_VINCULO",
        usuario_id=usuario.id,
        tabela="instaladores",
        descricao="Usuario instalador tentou acessar portal sem vinculo operacional ativo.",
        dados_depois={"path": request.url.path},
        ip=get_request_ip(request),
        severidade="CRITICO",
        categoria="SEGURANCA",
        resultado="BLOQUEADO",
    )
    db.commit()
    raise HTTPException(status_code=403, detail="Usuario instalador sem vinculo operacional ativo.")


def _bloquear_acesso_registro(db: Session, request: Request, usuario, instalador: Instalador, solicitacao_id: int) -> None:
    registrar_auditoria(
        db=db,
        acao="INSTALADOR_ACESSO_NEGADO_REGISTRO",
        usuario_id=usuario.id,
        tabela=Solicitacao.__tablename__,
        registro_id=solicitacao_id,
        descricao="Instalador tentou acessar solicitacao de outro instalador.",
        dados_depois={
            "path": request.url.path,
            "instalador_usuario_id": instalador.id,
            "solicitacao_id": solicitacao_id,
        },
        ip=get_request_ip(request),
        severidade="SUSPEITO",
        categoria="SEGURANCA",
        resultado="BLOQUEADO",
    )
    db.commit()


def _carregar_solicitacao_do_instalador(
    db: Session,
    request: Request,
    usuario,
    instalador: Instalador,
    solicitacao_id: int,
) -> Solicitacao:
    solicitacao = db.query(Solicitacao).options(
        joinedload(Solicitacao.instalador),
        joinedload(Solicitacao.criado_por),
        joinedload(Solicitacao.entregue_por),
        selectinload(Solicitacao.itens_caixa).selectinload(SolicitacaoItemCaixa.caixa).selectinload(CAIXA_HIDROMETROS_ATTR),
        selectinload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca),
    ).filter(Solicitacao.id == solicitacao_id).first()
    if not solicitacao:
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")
    if solicitacao.instalador_id != instalador.id:
        _bloquear_acesso_registro(db, request, usuario, instalador, solicitacao_id)
        raise HTTPException(status_code=403, detail="Acesso negado a solicitacao de outro instalador.")
    return solicitacao


def _query_solicitacoes_instalador(db: Session, instalador_id: int):
    return db.query(Solicitacao).options(
        joinedload(Solicitacao.instalador),
        joinedload(Solicitacao.criado_por),
        joinedload(Solicitacao.entregue_por),
        selectinload(Solicitacao.itens_caixa).selectinload(SolicitacaoItemCaixa.caixa).selectinload(CAIXA_HIDROMETROS_ATTR),
        selectinload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca),
    ).filter(Solicitacao.instalador_id == instalador_id)


def _query_solicitacoes_resumo_instalador(db: Session, instalador_id: int):
    return db.query(Solicitacao).options(
        joinedload(Solicitacao.criado_por),
        joinedload(Solicitacao.entregue_por),
        selectinload(Solicitacao.itens_caixa),
        selectinload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca),
    ).filter(Solicitacao.instalador_id == instalador_id)


@router.get("/", response_class=HTMLResponse)
def instalador_home(request: Request, usuario=instalador_dep):
    if _is_admin(usuario):
        return RedirectResponse(url="/admin/portal-instaladores", status_code=302)
    return RedirectResponse(url="/instalador/entregas", status_code=302)


@router.get("/entregas", response_class=HTMLResponse)
def minhas_entregas(
    request: Request,
    usuario=instalador_dep,
    db: Session = Depends(get_db),
):
    if _is_admin(usuario) and _admin_instalador_id(request) is None:
        return RedirectResponse(url="/admin/portal-instaladores", status_code=302)
    instalador = _instalador_do_usuario(db, request, usuario)
    entregas = _query_solicitacoes_instalador(db, instalador.id).filter(
        Solicitacao.status == SolicitacaoStatus.ENTREGUE
    ).order_by(Solicitacao.entregue_em.desc(), Solicitacao.id.desc()).all()
    return templates.TemplateResponse(
        "instalador/entregas.html",
        _portal_context(request, usuario, instalador, entregas=entregas),
    )


@router.get("/entregas/{solicitacao_id}", response_class=HTMLResponse)
def detalhe_entrega(
    request: Request,
    solicitacao_id: int,
    usuario=instalador_dep,
    db: Session = Depends(get_db),
):
    instalador = _instalador_do_usuario(db, request, usuario)
    solicitacao = _carregar_solicitacao_do_instalador(db, request, usuario, instalador, solicitacao_id)
    return templates.TemplateResponse(
        "instalador/entrega_detalhe.html",
        _portal_context(request, usuario, instalador, solicitacao=solicitacao),
    )


@router.post("/entregas/{solicitacao_id}/confirmar")
def confirmar_recebimento(
    request: Request,
    solicitacao_id: int,
    usuario=instalador_dep,
    db: Session = Depends(get_db),
):
    instalador = _instalador_do_usuario(db, request, usuario)
    solicitacao = _carregar_solicitacao_do_instalador(db, request, usuario, instalador, solicitacao_id)
    if solicitacao.status != SolicitacaoStatus.ENTREGUE:
        raise HTTPException(status_code=400, detail="Somente entregas concluidas pelo almoxarifado podem ser confirmadas.")
    if solicitacao.recebimento_instalador_status == "divergencia":
        raise HTTPException(status_code=400, detail="Esta entrega possui divergencia apontada e precisa de tratamento administrativo.")
    if solicitacao.recebimento_instalador_status == "confirmado":
        return RedirectResponse(
            url=_portal_redirect(f"/instalador/entregas/{solicitacao.id}", usuario, instalador, "confirmado"),
            status_code=302,
        )

    momento = utc_now()
    solicitacao.recebimento_instalador_status = "confirmado"
    solicitacao.confirmacao_instalador_em = momento
    solicitacao.usuario_confirmacao_instalador_id = usuario.id
    solicitacao.motivo_divergencia_instalador = None

    registrar_auditoria(
        db=db,
        acao="ADMIN_CONFIRMA_RECEBIMENTO_PORTAL_INSTALADOR" if _is_admin(usuario) else "INSTALADOR_CONFIRMA_RECEBIMENTO",
        usuario_id=usuario.id,
        tabela=Solicitacao.__tablename__,
        registro_id=solicitacao.id,
        descricao=f"Instalador confirmou recebimento da solicitacao #{solicitacao.id}",
        dados_depois={
            "instalador_id": instalador.id,
            "admin_preview": _is_admin(usuario),
            "confirmado_em": momento,
            "status_recebimento": solicitacao.recebimento_instalador_status,
        },
        ip=get_request_ip(request),
        severidade="NORMAL",
        categoria="OPERACIONAL",
        resultado="SUCESSO",
    )
    db.commit()
    return RedirectResponse(
        url=_portal_redirect(f"/instalador/entregas/{solicitacao.id}", usuario, instalador, "confirmado"),
        status_code=302,
    )


@router.post("/entregas/{solicitacao_id}/divergencia")
def apontar_divergencia(
    request: Request,
    solicitacao_id: int,
    motivo: str = Form(""),
    usuario=instalador_dep,
    db: Session = Depends(get_db),
):
    instalador = _instalador_do_usuario(db, request, usuario)
    solicitacao = _carregar_solicitacao_do_instalador(db, request, usuario, instalador, solicitacao_id)
    motivo_limpo = normalize_text(motivo)
    if not motivo_limpo:
        return templates.TemplateResponse(
            "instalador/entrega_detalhe.html",
            _portal_context(
                request,
                usuario,
                instalador,
                solicitacao=solicitacao,
                erro="Informe o motivo da divergencia.",
            ),
            status_code=400,
        )
    if solicitacao.status != SolicitacaoStatus.ENTREGUE:
        raise HTTPException(status_code=400, detail="Somente entregas concluidas pelo almoxarifado podem receber divergencia.")
    if solicitacao.recebimento_instalador_status == "confirmado":
        raise HTTPException(status_code=400, detail="Entrega ja confirmada pelo instalador.")

    momento = utc_now()
    solicitacao.recebimento_instalador_status = "divergencia"
    solicitacao.confirmacao_instalador_em = momento
    solicitacao.usuario_confirmacao_instalador_id = usuario.id
    solicitacao.motivo_divergencia_instalador = motivo_limpo

    registrar_auditoria(
        db=db,
        acao="ADMIN_DIVERGENCIA_PORTAL_INSTALADOR" if _is_admin(usuario) else "INSTALADOR_DIVERGENCIA_RECEBIMENTO",
        usuario_id=usuario.id,
        tabela=Solicitacao.__tablename__,
        registro_id=solicitacao.id,
        descricao=f"Instalador apontou divergencia na solicitacao #{solicitacao.id}",
        dados_depois={
            "instalador_id": instalador.id,
            "admin_preview": _is_admin(usuario),
            "divergencia_em": momento,
            "motivo": motivo_limpo,
            "status_recebimento": solicitacao.recebimento_instalador_status,
        },
        ip=get_request_ip(request),
        severidade="NORMAL",
        categoria="OPERACIONAL",
        resultado="DIVERGENCIA",
    )
    db.commit()
    return RedirectResponse(
        url=_portal_redirect(f"/instalador/entregas/{solicitacao.id}", usuario, instalador, "divergencia"),
        status_code=302,
    )


@router.get("/solicitacoes", response_class=HTMLResponse)
def minhas_solicitacoes(
    request: Request,
    usuario=instalador_dep,
    db: Session = Depends(get_db),
):
    instalador = _instalador_do_usuario(db, request, usuario)
    solicitacoes = _query_solicitacoes_resumo_instalador(db, instalador.id).order_by(
        Solicitacao.criado_em.desc(), Solicitacao.id.desc()
    ).limit(100).all()
    return templates.TemplateResponse(
        "instalador/solicitacoes.html",
        _portal_context(request, usuario, instalador, solicitacoes=solicitacoes),
    )


@router.get("/carcacas", response_class=HTMLResponse)
def minhas_carcacas(
    request: Request,
    usuario=instalador_dep,
    db: Session = Depends(get_db),
):
    instalador = _instalador_do_usuario(db, request, usuario)
    movimentos = db.query(CarcacaMovimentacao).options(
        joinedload(CarcacaMovimentacao.usuario),
    ).filter(
        CarcacaMovimentacao.instalador_id == instalador.id,
        CarcacaMovimentacao.tipo_movimento == CarcacaTipoMovimento.DEVOLUCAO_INSTALADOR,
    ).order_by(CarcacaMovimentacao.data_movimento.desc(), CarcacaMovimentacao.id.desc()).all()
    total = sum(item.quantidade for item in movimentos)
    return templates.TemplateResponse(
        "instalador/carcacas.html",
        _portal_context(request, usuario, instalador, movimentos=movimentos, total=total),
    )


@router.get("/produtividade", response_class=HTMLResponse)
def minha_produtividade(
    request: Request,
    usuario=instalador_dep,
    db: Session = Depends(get_db),
):
    instalador = _instalador_do_usuario(db, request, usuario)
    total = db.query(InstalacaoHidrometro).filter(
        InstalacaoHidrometro.instalador_id == instalador.id
    ).count()
    por_dia = db.query(
        func.date(InstalacaoHidrometro.data_instalacao).label("dia"),
        func.count(InstalacaoHidrometro.id).label("total"),
    ).filter(
        InstalacaoHidrometro.instalador_id == instalador.id
    ).group_by(
        func.date(InstalacaoHidrometro.data_instalacao)
    ).order_by(
        func.date(InstalacaoHidrometro.data_instalacao).desc()
    ).limit(60).all()
    historico = db.query(InstalacaoHidrometro).options(
        joinedload(InstalacaoHidrometro.hidrometro),
        joinedload(InstalacaoHidrometro.caixa),
        joinedload(InstalacaoHidrometro.usuario_registro),
    ).filter(
        InstalacaoHidrometro.instalador_id == instalador.id
    ).order_by(InstalacaoHidrometro.data_instalacao.desc(), InstalacaoHidrometro.id.desc()).limit(100).all()
    return templates.TemplateResponse(
        "instalador/produtividade.html",
        _portal_context(
            request,
            usuario,
            instalador,
            total=total,
            por_dia=por_dia,
            historico=historico,
        ),
    )
