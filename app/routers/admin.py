import os
import logging
from datetime import timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    AuditoriaLog,
    BoxRuleConfig,
    CaixaHidrometro,
    ConferenciaInstaladorPeca,
    ConferenciaItem,
    ConferenciaPecas,
    EstoquePeca,
    FeedbackAnexo,
    FeedbackOperacional,
    FeedbackStatus,
    Hidrometro,
    HidrometroManutencao,
    HidrometroStatus,
    Instalador,
    InstaladorPeca,
    MovimentacaoMaterial,
    MovimentacaoPeca,
    MovimentacaoTipo,
    Solicitacao,
    SolicitacaoItemCaixa,
    SolicitacaoItemPeca,
    SolicitacaoStatus,
    TipoPeca,
    UserRole,
    Usuario,
)
from app.security import SESSION_IDLE_TIMEOUT_SECONDS, hash_senha, requer_role, validar_politica_senha
from app.services.admin_cleanup import (
    CleanupBlockedError,
    build_caixa_cleanup_preview,
    build_instalador_cleanup_preview,
    build_solicitacao_cleanup_preview,
    execute_caixa_cleanup,
    execute_instalador_cleanup,
    execute_solicitacao_cleanup,
)
from app.services.auditoria import registrar_auditoria
from app.services.backup import backup_operacional_status, criar_backup_banco, restore_validation_status
from app.services.estoque import (
    carregar_solicitacao_operacional,
    mensagem_reversao_entrega_solicitacao,
    reverter_entrega_solicitacao,
)
from app.services.exportacao import resposta_xlsx
from app.services.exportacao_periodica import gerar_pacote_exportacao_admin
from app.services.feedback_attachments import caminho_anexo, excluir_arquivos_feedback
from app.services.integridade import diagnostico_operacional
from app.services.manutencao_hidrometros import (
    REVERSAO_DESTINO_CAIXA_ORIGEM,
    REVERSAO_DESTINO_ESTOQUE_SOLTO,
    caixa_origem_disponivel_para_retorno,
    reverter_manutencao_hidrometro,
    resumo_manutencao_hidrometros,
)
from app.services.operational_mode import alterar_modo_leitura, modo_leitura_status
from app.services.hidrometros import (
    aplicar_baixa_hidrometro,
    buscar_movimentacao_baixa,
    resolver_contexto_retorno_baixa,
    reverter_baixa_hidrometro,
)
from app.services.regras_caixa import alterar_regra_caixa, obter_regra_caixa_ativa, quantidade_esperada_caixa
from app.ui import templates
from app.utils import (
    format_datetime,
    normalize_digits,
    normalize_text,
    parse_bool_form,
    parse_date_end,
    parse_date_start,
    summarize_user_agent,
    utc_now,
)


router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("app.admin")
admin_dep = Depends(requer_role("admin"))
CAIXA_HIDROMETROS_ATTR = getattr(CaixaHidrometro, "hidrômetros")
INSTALADOR_HIDROMETROS_ATTR = getattr(Instalador, "hidrômetros")


def _render_usuario_form(
    request: Request,
    usuario,
    editando=None,
    erro: str | None = None,
    status_code: int = 200,
    form_data: dict | None = None,
):
    return templates.TemplateResponse(
        "admin/usuario_form.html",
        {"request": request, "usuario": usuario, "editando": editando, "erro": erro, "form_data": form_data or {}},
        status_code=status_code,
    )


def _render_instalador_form(request: Request, usuario, editando=None, erro: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        "admin/instalador_form.html",
        {"request": request, "usuario": usuario, "editando": editando, "erro": erro},
        status_code=status_code,
    )


def _render_peca_form(request: Request, usuario, editando=None, erro: str | None = None, status_code: int = 200):
    return templates.TemplateResponse(
        "admin/peca_form.html",
        {"request": request, "usuario": usuario, "editando": editando, "erro": erro},
        status_code=status_code,
    )


def _usuario_existente_por_email(db: Session, email: str, ignorar_id: int | None = None) -> Usuario | None:
    query = db.query(Usuario).filter(Usuario.email == email)
    if ignorar_id is not None:
        query = query.filter(Usuario.id != ignorar_id)
    return query.first()


def _usuario_existente_por_matricula(db: Session, matricula: str, ignorar_id: int | None = None) -> Usuario | None:
    matricula_limpa = normalize_text(matricula)
    if not matricula_limpa:
        return None
    query = db.query(Usuario).filter(Usuario.matricula.ilike(matricula_limpa))
    if ignorar_id is not None:
        query = query.filter(Usuario.id != ignorar_id)
    return query.first()


def _buscar_instalador_duplicado(db: Session, cpf_limpo: str, matricula: str, ignorar_id: int | None = None) -> str | None:
    matricula_limpa = normalize_text(matricula)
    if not matricula_limpa:
        return "Matricula e obrigatoria para identificar o instalador."
    if cpf_limpo and len(cpf_limpo) != 11:
        return "CPF do instalador deve conter 11 digitos quando informado."

    query = db.query(Instalador)
    if ignorar_id is not None:
        query = query.filter(Instalador.id != ignorar_id)
    criterios = [Instalador.matricula.ilike(matricula_limpa)]
    if cpf_limpo:
        criterios.append(Instalador.cpf == cpf_limpo)
    existente = query.filter(or_(*criterios)).first()
    if not existente:
        return None
    if cpf_limpo and existente.cpf == cpf_limpo:
        return "CPF ja cadastrado no sistema."
    return "Matricula ja cadastrada no sistema."


def _resolver_instalador_para_usuario(
    db: Session,
    *,
    nome: str,
    cpf: str,
    matricula: str,
    usuario_alvo: Usuario | None = None,
    usuario_id_permitido: int | None = None,
) -> tuple[Instalador | None, str | None, bool]:
    cpf_limpo = normalize_digits(cpf)
    matricula_limpa = normalize_text(matricula)
    if not matricula_limpa:
        return None, "Matricula e obrigatoria para usuario instalador.", False
    if cpf_limpo and len(cpf_limpo) != 11:
        return None, "CPF do instalador deve conter 11 digitos quando informado.", False

    instalador_por_cpf = db.query(Instalador).filter(Instalador.cpf == cpf_limpo).first() if cpf_limpo else None
    instalador_por_matricula = db.query(Instalador).filter(Instalador.matricula.ilike(matricula_limpa)).first()
    if instalador_por_cpf and instalador_por_matricula and instalador_por_cpf.id != instalador_por_matricula.id:
        return None, "CPF e matricula pertencem a instaladores diferentes.", False

    instalador = instalador_por_cpf or instalador_por_matricula
    usuario_id_alvo = usuario_alvo.id if usuario_alvo and usuario_alvo.id else usuario_id_permitido
    if instalador:
        if instalador_por_cpf and normalize_text(instalador.matricula).lower() != matricula_limpa.lower():
            return None, "CPF ja cadastrado com outra matricula de instalador.", False
        if instalador_por_matricula and cpf_limpo and instalador.cpf and instalador.cpf != cpf_limpo:
            return None, "Matricula ja cadastrada com outro CPF de instalador.", False
        if instalador.usuario_id and instalador.usuario_id != usuario_id_alvo:
            return None, "Instalador ja esta vinculado a outro usuario.", False
        if usuario_alvo:
            if usuario_alvo.instalador and usuario_alvo.instalador.id != instalador.id:
                usuario_alvo.instalador.usuario_id = None
            instalador.usuario_id = usuario_alvo.id
            instalador.nome = normalize_text(nome) or instalador.nome
            instalador.cpf = cpf_limpo or None
            instalador.matricula = matricula_limpa
            instalador.ativo = True
            usuario_alvo.matricula = matricula_limpa
        return instalador, None, False

    if not usuario_alvo:
        return None, None, False

    if usuario_alvo.instalador:
        usuario_alvo.instalador.usuario_id = None

    novo_instalador = Instalador(
        nome=normalize_text(nome),
        cpf=cpf_limpo or None,
        matricula=matricula_limpa,
        ativo=True,
        usuario_id=usuario_alvo.id,
    )
    usuario_alvo.matricula = matricula_limpa
    db.add(novo_instalador)
    db.flush()
    return novo_instalador, None, True


def _buscar_tipo_peca_duplicado(db: Session, nome: str, ignorar_id: int | None = None) -> TipoPeca | None:
    query = db.query(TipoPeca).filter(TipoPeca.nome.ilike(nome))
    if ignorar_id is not None:
        query = query.filter(TipoPeca.id != ignorar_id)
    return query.first()


def _active_admin_count(db: Session, ignorar_id: int | None = None) -> int:
    query = db.query(Usuario).filter(Usuario.role == UserRole.ADMIN, Usuario.ativo == True)
    if ignorar_id is not None:
        query = query.filter(Usuario.id != ignorar_id)
    return query.count()


def _erro_regra_admin_unico(
    db: Session,
    *,
    alvo: Usuario | None = None,
    novo_role: UserRole,
    novo_ativo: bool,
) -> str | None:
    alvo_id = alvo.id if alvo else None
    outros_admins_ativos = _active_admin_count(db, ignorar_id=alvo_id)
    if novo_role == UserRole.ADMIN and novo_ativo and outros_admins_ativos > 0:
        return "Ja existe um administrador ativo. A regra atual permite somente 1 ADMIN ativo."
    if alvo and alvo.role == UserRole.ADMIN and alvo.ativo and (novo_role != UserRole.ADMIN or not novo_ativo):
        if outros_admins_ativos == 0:
            return "O sistema precisa manter exatamente 1 ADMIN ativo."
    return None


def _sanitize_next_path(next_path: str | None, fallback: str) -> str:
    target = (next_path or "").strip()
    if not target.startswith("/") or target.startswith("//"):
        return fallback
    return target


def _append_query_flag(url: str, flag: str) -> str:
    return f"{url}?{flag}" if "?" not in url else f"{url}&{flag}"


def _format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def _latest_audit_by_user(db: Session, user_ids: list[int]) -> dict[int, AuditoriaLog]:
    if not user_ids:
        return {}
    logs = db.query(AuditoriaLog).filter(
        AuditoriaLog.usuario_id.in_(user_ids)
    ).order_by(AuditoriaLog.criado_em.desc(), AuditoriaLog.id.desc()).limit(1000).all()
    latest: dict[int, AuditoriaLog] = {}
    for log in logs:
        if log.usuario_id and log.usuario_id not in latest:
            latest[log.usuario_id] = log
    return latest


def _session_rows_admin(db: Session, *, agora=None) -> list[dict[str, object]]:
    agora = agora or utc_now()
    online_threshold = agora - timedelta(minutes=5)
    idle_threshold = agora - timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS)
    usuarios = db.query(Usuario).filter(Usuario.ativo == True).order_by(
        Usuario.ultimo_acesso.desc().nullslast(),
        Usuario.nome.asc(),
    ).all()
    latest_logs = _latest_audit_by_user(db, [u.id for u in usuarios])
    rows: list[dict[str, object]] = []
    for user in usuarios:
        last_access = user.ultimo_acesso
        last_log = latest_logs.get(user.id)
        role = getattr(user.role, "value", user.role)
        online = bool(last_access and last_access >= online_threshold)
        sessao_provavel = bool(role == "admin" or (last_access and last_access >= idle_threshold))
        idle_seconds = int((agora - last_access).total_seconds()) if last_access else None
        rows.append(
            {
                "usuario": user,
                "role": role,
                "online": online,
                "sessao_provavel": sessao_provavel,
                "idle_seconds": idle_seconds,
                "idle_minutes": int(idle_seconds // 60) if idle_seconds is not None else None,
                "last_log": last_log,
                "ip": (last_log.ip_cliente or last_log.ip) if last_log else "",
                "browser": summarize_user_agent(last_log.user_agent) if last_log and last_log.user_agent else "",
                "pode_desconectar": role != "admin",
            }
        )
    return rows


def _monitoramento_alertas(diagnostico: dict, resumo_manutencao: dict, estoques_criticos, caixas_inconsistentes) -> list[dict[str, object]]:
    alertas: list[dict[str, object]] = []
    if diagnostico["erros_criticos_24h"]:
        alertas.append({
            "titulo": "Erros criticos nas ultimas 24h",
            "valor": diagnostico["erros_criticos_24h"],
            "tom": "red",
            "href": "/admin/auditoria?severidade=CRITICO",
            "detalhe": "Abrir eventos criticos",
        })
    if diagnostico["eventos_suspeitos_24h"]:
        alertas.append({
            "titulo": "Eventos suspeitos nas ultimas 24h",
            "valor": diagnostico["eventos_suspeitos_24h"],
            "tom": "amber",
            "href": "/admin/auditoria?severidade=SUSPEITO",
            "detalhe": "Abrir eventos suspeitos",
        })
    if diagnostico["tentativas_login_invalidas_24h"]:
        alertas.append({
            "titulo": "Tentativas de login invalidas",
            "valor": diagnostico["tentativas_login_invalidas_24h"],
            "tom": "amber",
            "href": "/admin/auditoria?acao=LOGIN",
            "detalhe": "Ver logins bloqueados/falhos",
        })
    if estoques_criticos:
        alertas.append({
            "titulo": "Itens de estoque abaixo do minimo",
            "valor": len(estoques_criticos),
            "tom": "red",
            "href": "/almoxarifado/estoque-inteligente?status=CRITICO",
            "detalhe": "Abrir estoque inteligente",
        })
    if caixas_inconsistentes:
        alertas.append({
            "titulo": "Caixas inconsistentes",
            "valor": len(caixas_inconsistentes),
            "tom": "red",
            "href": "#caixas-inconsistentes",
            "detalhe": "Ver caixas com diferenca",
        })
    if resumo_manutencao.get("parados_alerta"):
        alertas.append({
            "titulo": "Retornados da assistencia parados",
            "valor": resumo_manutencao.get("parados_alerta"),
            "tom": "amber",
            "href": "/almoxarifado/manutencao",
            "detalhe": "Abrir manutencao",
        })
    return alertas


def _sanitize_caixa_cleanup_next_path(next_path: str | None, caixa_id: int) -> str:
    fallback = "/almoxarifado/caixas"
    target = _sanitize_next_path(next_path, fallback)
    detalhe_path = f"/almoxarifado/caixas/{caixa_id}"
    if target == detalhe_path or target.startswith(f"{detalhe_path}?"):
        return fallback
    return target


def _carregar_caixa_limpeza(db: Session, caixa_id: int) -> CaixaHidrometro | None:
    return db.query(CaixaHidrometro).options(
        joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(CaixaHidrometro.movimentacoes),
        joinedload(CaixaHidrometro.instalador),
    ).filter(CaixaHidrometro.id == caixa_id).first()


def _carregar_solicitacao_limpeza(db: Session, solicitacao_id: int) -> Solicitacao | None:
    return db.query(Solicitacao).options(
        joinedload(Solicitacao.instalador),
        joinedload(Solicitacao.itens_caixa).joinedload(SolicitacaoItemCaixa.caixa).joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(Solicitacao.itens_caixa).joinedload(SolicitacaoItemCaixa.caixa).joinedload(CaixaHidrometro.movimentacoes),
        joinedload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca).joinedload(TipoPeca.estoque),
    ).filter(Solicitacao.id == solicitacao_id).first()


def _carregar_instalador_limpeza(db: Session, instalador_id: int) -> Instalador | None:
    return db.query(Instalador).options(
        joinedload(INSTALADOR_HIDROMETROS_ATTR),
        joinedload(Instalador.pecas_posse).joinedload(InstaladorPeca.tipo_peca),
    ).filter(Instalador.id == instalador_id).first()


def _carregar_usuario_limpeza(db: Session, usuario_id: int) -> Usuario | None:
    return db.query(Usuario).filter(Usuario.id == usuario_id).first()


def _carregar_hidrometro_admin(db: Session, hidrometro_id: int) -> Hidrometro | None:
    return db.query(Hidrometro).options(
        joinedload(Hidrometro.caixa).joinedload(CaixaHidrometro.instalador),
        joinedload(Hidrometro.caixa).joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(Hidrometro.instalador_atual),
        joinedload(Hidrometro.baixado_por),
        joinedload(Hidrometro.instalador_baixa),
    ).filter(Hidrometro.id == hidrometro_id).first()


def _carregar_manutencao_admin(db: Session, manutencao_id: int) -> HidrometroManutencao | None:
    return db.query(HidrometroManutencao).options(
        joinedload(HidrometroManutencao.hidrometro),
        joinedload(HidrometroManutencao.caixa_origem).joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(HidrometroManutencao.instalador_origem),
        joinedload(HidrometroManutencao.criado_por_usuario),
        joinedload(HidrometroManutencao.revertida_por_usuario),
    ).filter(HidrometroManutencao.id == manutencao_id).first()


def _carregar_movimentacao_entrada_peca_admin(db: Session, movimento_id: int) -> MovimentacaoPeca | None:
    return db.query(MovimentacaoPeca).options(
        joinedload(MovimentacaoPeca.tipo_peca).joinedload(TipoPeca.estoque),
        joinedload(MovimentacaoPeca.registrado_por),
    ).filter(MovimentacaoPeca.id == movimento_id).first()


def _carregar_movimentacao_material_admin(db: Session, movimento_id: int) -> MovimentacaoMaterial | None:
    return db.query(MovimentacaoMaterial).options(
        joinedload(MovimentacaoMaterial.caixa),
        joinedload(MovimentacaoMaterial.hidrometro),
        joinedload(MovimentacaoMaterial.instalador),
        joinedload(MovimentacaoMaterial.registrado_por),
    ).filter(MovimentacaoMaterial.id == movimento_id).first()


def _carregar_conferencia_admin(db: Session, conferencia_id: int) -> ConferenciaPecas | None:
    return db.query(ConferenciaPecas).options(
        joinedload(ConferenciaPecas.instalador),
        joinedload(ConferenciaPecas.responsavel),
        joinedload(ConferenciaPecas.itens).joinedload(ConferenciaItem.tipo_peca),
    ).filter(ConferenciaPecas.id == conferencia_id).first()


def _carregar_feedback_admin(db: Session, feedback_id: int) -> FeedbackOperacional | None:
    return db.query(FeedbackOperacional).options(
        joinedload(FeedbackOperacional.usuario),
        joinedload(FeedbackOperacional.resolvido_por),
        joinedload(FeedbackOperacional.anexos),
    ).filter(FeedbackOperacional.id == feedback_id).first()


def _contagens_exclusao_usuario(db: Session, usuario_id: int) -> dict[str, int]:
    return {
        "auditorias": db.query(AuditoriaLog).filter(AuditoriaLog.usuario_id == usuario_id).count(),
        "solicitacoes_criadas": db.query(Solicitacao).filter(Solicitacao.criado_por_id == usuario_id).count(),
        "solicitacoes_entregues": db.query(Solicitacao).filter(Solicitacao.entregue_por_id == usuario_id).count(),
        "movimentacoes_material": db.query(MovimentacaoMaterial).filter(MovimentacaoMaterial.registrado_por_id == usuario_id).count(),
        "movimentacoes_pecas": db.query(MovimentacaoPeca).filter(MovimentacaoPeca.registrado_por_id == usuario_id).count(),
        "conferencias": db.query(ConferenciaPecas).filter(ConferenciaPecas.responsavel_id == usuario_id).count(),
        "baixas_hidrometros": db.query(Hidrometro).filter(Hidrometro.baixado_por_id == usuario_id).count(),
    }


def _build_usuario_cleanup_preview(db: Session, alvo: Usuario, admin_atual: Usuario) -> dict[str, object]:
    contagens = _contagens_exclusao_usuario(db, alvo.id)
    active_admins_restantes = db.query(Usuario).filter(
        Usuario.id != alvo.id,
        Usuario.role == UserRole.ADMIN,
        Usuario.ativo == True,
    ).count()

    blockers: list[str] = []
    if alvo.id == admin_atual.id:
        blockers.append("O administrador atual nao pode excluir o proprio usuario logado.")
    if alvo.role == UserRole.ADMIN and active_admins_restantes == 0:
        blockers.append("Este usuario e o ultimo administrador ativo do sistema.")

    labels = {
        "auditorias": "registro(s) de auditoria",
        "solicitacoes_criadas": "solicitacao(oes) criada(s)",
        "solicitacoes_entregues": "solicitacao(oes) entregue(s)",
        "movimentacoes_material": "movimentacao(oes) de material",
        "movimentacoes_pecas": "movimentacao(oes) de peca",
        "conferencias": "conferencia(s) registrada(s)",
        "baixas_hidrometros": "baixa(s) de hidrometro registrada(s)",
    }
    for key, total in contagens.items():
        if total > 0:
            blockers.append(f"Possui {total} {labels[key]} vinculada(s) ao historico.")

    return {
        "entity_kind": "Usuario",
        "entity_title": alvo.nome,
        "entity_subtitle": alvo.email,
        "confirm_value": alvo.email.upper(),
        "details": [
            ("Email", alvo.email),
            ("Perfil", alvo.role.value),
            ("Status", "Ativo" if alvo.ativo else "Inativo"),
            ("Ultimo acesso", alvo.ultimo_acesso.strftime("%d/%m/%Y %H:%M") if alvo.ultimo_acesso else "Nunca"),
        ],
        "impact_items": [
            "Remove o acesso definitivo do usuario ao sistema.",
            "Mantem o log da exclusao administrativa na auditoria.",
        ],
        "warnings": [
            "A exclusao so fica liberada quando nao houver historico operacional vinculado.",
        ],
        "blockers": blockers,
        "allowed": not blockers,
    }


@router.get("/usuarios", response_class=HTMLResponse)
async def listar_usuarios(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    usuarios = db.query(Usuario).order_by(Usuario.nome).all()
    return templates.TemplateResponse(
        "admin/usuarios.html",
        {"request": request, "usuario": usuario, "usuarios": usuarios},
    )


@router.get("/portal-instaladores", response_class=HTMLResponse)
async def portal_instaladores_admin(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    instaladores = db.query(Instalador).options(
        joinedload(Instalador.usuario),
    ).filter(Instalador.ativo == True).order_by(Instalador.nome).all()
    entregas_pendentes = {
        instalador_id: total
        for instalador_id, total in db.query(Solicitacao.instalador_id, func.count(Solicitacao.id)).filter(
            Solicitacao.status == SolicitacaoStatus.ENTREGUE,
            or_(
                Solicitacao.recebimento_instalador_status == None,
                Solicitacao.recebimento_instalador_status == "",
                Solicitacao.recebimento_instalador_status == "pendente",
            ),
        ).group_by(Solicitacao.instalador_id).all()
    }
    return templates.TemplateResponse(
        "admin/portal_instaladores.html",
        {
            "request": request,
            "usuario": usuario,
            "instaladores": instaladores,
            "entregas_pendentes": entregas_pendentes,
        },
    )


@router.get("/feedbacks", response_class=HTMLResponse)
async def listar_feedbacks(
    request: Request,
    status: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    status_limpo = normalize_text(status)
    status_map = {
        FeedbackStatus.NOVO.value: FeedbackStatus.NOVO,
        FeedbackStatus.EM_ANALISE.value: FeedbackStatus.EM_ANALISE,
        FeedbackStatus.RESOLVIDO.value: FeedbackStatus.RESOLVIDO,
    }
    status_enum = status_map.get(status_limpo) if status_limpo else None
    if status_limpo and status_enum is None:
        raise HTTPException(status_code=400, detail="Filtro de status invalido.")

    feedbacks_query = db.query(FeedbackOperacional).options(
        joinedload(FeedbackOperacional.usuario),
        joinedload(FeedbackOperacional.resolvido_por),
        joinedload(FeedbackOperacional.anexos),
    )
    if status_enum is not None:
        feedbacks_query = feedbacks_query.filter(FeedbackOperacional.status == status_enum)

    feedbacks = feedbacks_query.order_by(
        FeedbackOperacional.urgente.desc(),
        FeedbackOperacional.criado_em.desc(),
        FeedbackOperacional.id.desc(),
    ).limit(200).all()

    return templates.TemplateResponse(
        "admin/feedbacks.html",
        {
            "request": request,
            "usuario": usuario,
            "feedbacks": feedbacks,
            "status_atual": status_limpo,
            "feedback_counts": {
                "novo": db.query(FeedbackOperacional).filter(FeedbackOperacional.status == FeedbackStatus.NOVO).count(),
                "em_analise": db.query(FeedbackOperacional).filter(
                    FeedbackOperacional.status == FeedbackStatus.EM_ANALISE
                ).count(),
                "resolvido": db.query(FeedbackOperacional).filter(
                    FeedbackOperacional.status == FeedbackStatus.RESOLVIDO
                ).count(),
                "urgente": db.query(FeedbackOperacional).filter(FeedbackOperacional.urgente == True).count(),
            },
        },
    )


@router.post("/feedbacks/{feedback_id}/status")
async def atualizar_status_feedback(
    feedback_id: int,
    status: str = Form(...),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    feedback = _carregar_feedback_admin(db, feedback_id)
    if not feedback:
        raise HTTPException(status_code=404, detail="Feedback nao encontrado.")

    status_limpo = normalize_text(status)
    status_map = {
        FeedbackStatus.NOVO.value: FeedbackStatus.NOVO,
        FeedbackStatus.EM_ANALISE.value: FeedbackStatus.EM_ANALISE,
        FeedbackStatus.RESOLVIDO.value: FeedbackStatus.RESOLVIDO,
    }
    novo_status = status_map.get(status_limpo)
    if novo_status is None:
        raise HTTPException(status_code=400, detail="Status de feedback invalido.")

    status_anterior = feedback.status.value
    feedback.status = novo_status
    feedback.atualizado_em = utc_now()
    if novo_status == FeedbackStatus.RESOLVIDO:
        feedback.resolvido_em = feedback.atualizado_em
        feedback.resolvido_por_id = usuario.id
    else:
        feedback.resolvido_em = None
        feedback.resolvido_por_id = None

    registrar_auditoria(
        db=db,
        acao="ADMIN_UPDATE_FEEDBACK",
        usuario_id=usuario.id,
        tabela=FeedbackOperacional.__tablename__,
        registro_id=feedback.id,
        descricao=f"Status do feedback #{feedback.id} atualizado",
        dados_antes={"status": status_anterior},
        dados_depois={"status": feedback.status.value},
    )
    db.commit()
    return RedirectResponse(url=f"/admin/feedbacks?status={feedback.status.value}", status_code=302)


@router.get("/feedbacks/anexos/{anexo_id}")
async def baixar_anexo_feedback(
    anexo_id: int,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    anexo = db.query(FeedbackAnexo).filter(FeedbackAnexo.id == anexo_id).first()
    if not anexo:
        raise HTTPException(status_code=404, detail="Anexo nao encontrado.")
    path = caminho_anexo(anexo)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo do anexo nao encontrado no servidor.")
    return FileResponse(
        path,
        media_type=anexo.content_type or "application/octet-stream",
        filename=anexo.nome_original,
        content_disposition_type="inline",
    )


@router.post("/feedbacks/limpar-resolvidos")
async def limpar_feedbacks_resolvidos(
    dias_minimos: int = Form(30),
    confirmacao: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    dias = min(max(int(dias_minimos or 30), 1), 3650)
    if normalize_text(confirmacao, upper=True) != "LIMPAR":
        return RedirectResponse(url="/admin/feedbacks?erro_limpeza=confirmacao", status_code=302)

    limite = utc_now() - timedelta(days=dias)
    feedbacks = db.query(FeedbackOperacional).options(
        joinedload(FeedbackOperacional.anexos),
    ).filter(
        FeedbackOperacional.status == FeedbackStatus.RESOLVIDO,
        FeedbackOperacional.resolvido_em.isnot(None),
        FeedbackOperacional.resolvido_em <= limite,
    ).all()
    total = len(feedbacks)
    anexos = sum(len(feedback.anexos or []) for feedback in feedbacks)
    ids = [feedback.id for feedback in feedbacks]

    for feedback in feedbacks:
        excluir_arquivos_feedback(feedback)
        db.delete(feedback)

    registrar_auditoria(
        db=db,
        acao="ADMIN_LIMPAR_FEEDBACKS_RESOLVIDOS",
        usuario_id=usuario.id,
        tabela=FeedbackOperacional.__tablename__,
        descricao=f"Feedbacks resolvidos antigos removidos: {total}",
        dados_depois={
            "dias_minimos": dias,
            "total": total,
            "anexos": anexos,
            "ids": ids[:100],
        },
    )
    db.commit()
    return RedirectResponse(url=f"/admin/feedbacks?limpeza={total}", status_code=302)


@router.get("/regras-caixa", response_class=HTMLResponse)
async def regras_caixa_page(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    regra_ativa = obter_regra_caixa_ativa(db, criar_padrao=True)
    historico = db.query(BoxRuleConfig).options(
        joinedload(BoxRuleConfig.criado_por),
    ).order_by(
        BoxRuleConfig.vigente_desde.desc(),
        BoxRuleConfig.id.desc(),
    ).all()
    return templates.TemplateResponse(
        "admin/regras_caixa.html",
        {
            "request": request,
            "usuario": usuario,
            "regra_ativa": regra_ativa,
            "historico": historico,
        },
    )


@router.post("/regras-caixa")
async def alterar_regras_caixa_admin(
    request: Request,
    quantidade_hidrometros: int = Form(...),
    justificativa: str = Form(""),
    confirmacao: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    regra_ativa = obter_regra_caixa_ativa(db, criar_padrao=True)
    historico = db.query(BoxRuleConfig).options(joinedload(BoxRuleConfig.criado_por)).order_by(
        BoxRuleConfig.vigente_desde.desc(),
        BoxRuleConfig.id.desc(),
    ).all()
    justificativa_limpa = normalize_text(justificativa)
    confirmacao_limpa = normalize_text(confirmacao, upper=True)

    def render_error(message: str, status_code: int = 400):
        return templates.TemplateResponse(
            "admin/regras_caixa.html",
            {
                "request": request,
                "usuario": usuario,
                "regra_ativa": regra_ativa,
                "historico": historico,
                "erro": message,
            },
            status_code=status_code,
        )

    if not justificativa_limpa:
        return render_error("Informe uma justificativa para alterar a regra da caixa.")
    if confirmacao_limpa != "ALTERAR":
        return render_error("Digite ALTERAR para confirmar a mudanca da regra.")

    try:
        nova_regra = alterar_regra_caixa(
            db,
            quantidade_hidrometros=quantidade_hidrometros,
            usuario_id=usuario.id,
            justificativa=justificativa_limpa,
        )
    except ValueError as exc:
        return render_error(str(exc))

    db.commit()
    return RedirectResponse(url=f"/admin/regras-caixa?sucesso={nova_regra.id}", status_code=302)


@router.get("/monitoramento", response_class=HTMLResponse)
async def monitoramento_admin_page(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    status_backup = backup_operacional_status()
    ultimo_backup = status_backup["ultimo_backup"]
    status_restore = restore_validation_status()
    modo_leitura = modo_leitura_status(db)
    diagnostico = modo_leitura["diagnostico"]
    estoques = db.query(EstoquePeca).options(joinedload(EstoquePeca.tipo_peca)).all()
    caixas = db.query(CaixaHidrometro).options(joinedload(CAIXA_HIDROMETROS_ATTR)).filter(
        CaixaHidrometro.ativo == True
    ).order_by(CaixaHidrometro.criado_em.desc()).limit(300).all()
    caixas_inconsistentes = [
        caixa for caixa in caixas
        if len(getattr(caixa, CAIXA_HIDROMETROS_ATTR.key) or []) != quantidade_esperada_caixa(caixa)
    ]
    login_alertas = db.query(AuditoriaLog).filter(
        AuditoriaLog.acao.in_(["LOGIN_FALHA", "LOGIN_BLOQUEADO", "LOGIN_USUARIO_INATIVO"])
    ).order_by(AuditoriaLog.criado_em.desc(), AuditoriaLog.id.desc()).limit(10).all()
    acoes_criticas = db.query(AuditoriaLog).filter(
        AuditoriaLog.severidade == "CRITICO"
    ).order_by(AuditoriaLog.criado_em.desc(), AuditoriaLog.id.desc()).limit(10).all()
    eventos_suspeitos = db.query(AuditoriaLog).filter(
        AuditoriaLog.severidade == "SUSPEITO"
    ).order_by(AuditoriaLog.criado_em.desc(), AuditoriaLog.id.desc()).limit(10).all()
    regras_recentes = db.query(BoxRuleConfig).options(joinedload(BoxRuleConfig.criado_por)).order_by(
        BoxRuleConfig.vigente_desde.desc(),
        BoxRuleConfig.id.desc(),
    ).limit(5).all()
    estoques_criticos = [estoque for estoque in estoques if estoque.abaixo_minimo]
    resumo_manutencao = resumo_manutencao_hidrometros(db)
    usuarios_sessao = _session_rows_admin(db)
    alertas_interativos = _monitoramento_alertas(
        diagnostico,
        resumo_manutencao,
        estoques_criticos,
        caixas_inconsistentes,
    )

    return templates.TemplateResponse(
        "admin/monitoramento.html",
        {
            "request": request,
            "usuario": usuario,
            "estoques_criticos": estoques_criticos,
            "caixas_inconsistentes": caixas_inconsistentes[:20],
            "login_alertas": login_alertas,
            "acoes_criticas": acoes_criticas,
            "eventos_suspeitos": eventos_suspeitos,
            "regras_recentes": regras_recentes,
            "resumo_manutencao": resumo_manutencao,
            "alertas_interativos": alertas_interativos,
            "usuarios_sessao": usuarios_sessao[:8],
            "usuarios_online": sum(1 for row in usuarios_sessao if row["online"]),
            "usuarios_operacionais_ativos": sum(1 for row in usuarios_sessao if row["role"] != "admin" and row["sessao_provavel"]),
            "solicitacoes_pendentes": db.query(Solicitacao).filter(Solicitacao.status == SolicitacaoStatus.PENDENTE).count(),
            "backup_status": (
                f"{format_datetime(ultimo_backup.created_at)} - {_format_file_size(ultimo_backup.size_bytes)}"
                if ultimo_backup
                else normalize_text(os.getenv("BACKUP_STATUS", "Nao configurado"))
            ),
            "ultimo_backup": ultimo_backup,
            "backup_retention_days": status_backup["retencao_dias"],
            "backup_dir": status_backup["diretorio"],
            "backup_engine_status": status_backup,
            "restore_status": status_restore,
            "modo_leitura": modo_leitura,
            "diagnostico": diagnostico,
            "checklist_admin": [
                {
                    "titulo": "Backup",
                    "valor": "OK" if ultimo_backup else "Pendente",
                    "tom": "green" if ultimo_backup else "amber",
                    "detalhe": _format_file_size(ultimo_backup.size_bytes) if ultimo_backup else "Sem backup local",
                },
                {
                    "titulo": "Restore",
                    "valor": "OK" if status_restore and status_restore.get("status") == "ok" else "Pendente",
                    "tom": "green" if status_restore and status_restore.get("status") == "ok" else "amber",
                    "detalhe": (status_restore or {}).get("validado_em", "Nao validado"),
                },
                {
                    "titulo": "Erros 24h",
                    "valor": diagnostico["erros_criticos_24h"],
                    "tom": "red" if diagnostico["erros_criticos_24h"] else "green",
                    "detalhe": "Criticos",
                },
                {
                    "titulo": "Logins",
                    "valor": diagnostico["tentativas_login_invalidas_24h"],
                    "tom": "amber" if diagnostico["tentativas_login_invalidas_24h"] else "green",
                    "detalhe": "Invalidos 24h",
                },
                {
                    "titulo": "Integridade",
                    "valor": len(diagnostico["bloqueadores_criticos"]),
                    "tom": "red" if diagnostico["bloqueadores_criticos"] else "green",
                    "detalhe": "Bloqueadores",
                },
                {
                    "titulo": "Tentativas",
                    "valor": diagnostico["tentativas_bloqueadas_24h"],
                    "tom": "amber" if diagnostico["tentativas_bloqueadas_24h"] else "green",
                    "detalhe": "Bloqueadas 24h",
                },
                {
                    "titulo": "Usuarios",
                    "valor": sum(1 for row in usuarios_sessao if row["sessao_provavel"]),
                    "tom": "blue",
                    "detalhe": "Sessoes provaveis",
                },
            ],
        },
    )


@router.post("/monitoramento/backup")
async def baixar_backup_admin(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    try:
        resultado = criar_backup_banco()
    except RuntimeError as exc:
        logger.exception("Falha ao gerar backup manual pelo monitoramento")
        registrar_auditoria(
            db=db,
            acao="ADMIN_BACKUP_FALHA",
            usuario_id=usuario.id,
            tabela="backup",
            descricao="Falha ao gerar backup manual pelo monitoramento",
            dados_depois={"erro": str(exc)},
            request=request,
            severidade="CRITICO",
            categoria="BACKUP",
            resultado="FALHA",
        )
        db.commit()
        return RedirectResponse(
            url=f"/admin/monitoramento?erro_backup={quote(str(exc))}",
            status_code=302,
        )

    registrar_auditoria(
        db=db,
        acao="ADMIN_BACKUP_DOWNLOAD",
        usuario_id=usuario.id,
        tabela="backup",
        descricao="Backup manual gerado para download pelo administrador",
        dados_depois={
            "arquivo": resultado.filename,
            "engine": resultado.engine,
            "tamanho_bytes": resultado.size_bytes,
            "criado_em": resultado.created_at,
        },
        request=request,
        categoria="BACKUP",
        resultado="SUCESSO",
    )
    db.commit()
    media_type = "application/octet-stream"
    return FileResponse(path=resultado.path, filename=resultado.filename, media_type=media_type)


@router.post("/monitoramento/modo-leitura")
async def alterar_modo_leitura_admin(
    acao: str = Form(...),
    motivo: str = Form(""),
    confirmacao: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    acao_limpa = normalize_text(acao, upper=True)
    confirmacao_limpa = normalize_text(confirmacao, upper=True)
    ativar = acao_limpa == "ATIVAR"
    liberar = acao_limpa == "LIBERAR"
    if not ativar and not liberar:
        raise HTTPException(status_code=400, detail="Acao de modo leitura invalida.")
    if confirmacao_limpa != acao_limpa:
        raise HTTPException(status_code=400, detail=f"Digite {acao_limpa} para confirmar.")
    try:
        alterar_modo_leitura(db, ativo=ativar, usuario_id=usuario.id, motivo=motivo)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    db.commit()
    return RedirectResponse(url="/admin/monitoramento?modo_leitura=1", status_code=302)


@router.get("/usuarios-ativos", response_class=HTMLResponse)
async def usuarios_ativos_admin_page(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    rows = _session_rows_admin(db)
    return templates.TemplateResponse(
        "admin/usuarios_ativos.html",
        {
            "request": request,
            "usuario": usuario,
            "usuarios_sessao": rows,
            "kpis": {
                "online": sum(1 for row in rows if row["online"]),
                "sessao_provavel": sum(1 for row in rows if row["sessao_provavel"]),
                "operacionais": sum(1 for row in rows if row["role"] != "admin" and row["sessao_provavel"]),
                "inativos": sum(1 for row in rows if not row["sessao_provavel"]),
            },
            "idle_timeout_minutes": int(SESSION_IDLE_TIMEOUT_SECONDS // 60),
        },
    )


@router.post("/usuarios/{usuario_id}/desconectar")
async def desconectar_usuario_admin(
    request: Request,
    usuario_id: int,
    justificativa: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    alvo = db.query(Usuario).filter(Usuario.id == usuario_id, Usuario.ativo == True).first()
    if not alvo:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")
    if alvo.id == usuario.id:
        raise HTTPException(status_code=400, detail="O admin atual nao pode desconectar a propria sessao por esta tela.")
    if getattr(alvo.role, "value", alvo.role) == "admin":
        raise HTTPException(status_code=400, detail="Sessao admin nao deve ser derrubada por este fluxo operacional.")
    justificativa_limpa = normalize_text(justificativa)
    if not justificativa_limpa:
        raise HTTPException(status_code=400, detail="Informe a justificativa da desconexao.")

    versao_anterior = int(alvo.token_version or 0)
    alvo.token_version = versao_anterior + 1
    alvo.session_notice_code = "ADMIN_DISCONNECT"
    alvo.session_notice_message = "O administrador desconectou sua sessao para manutencao programada do sistema."
    alvo.session_notice_at = utc_now()
    registrar_auditoria(
        db=db,
        acao="ADMIN_DESCONECTA_USUARIO",
        usuario_id=usuario.id,
        tabela=Usuario.__tablename__,
        registro_id=alvo.id,
        descricao=f"Usuario {alvo.email} desconectado pelo administrador.",
        dados_antes={"token_version": versao_anterior, "ultimo_acesso": alvo.ultimo_acesso},
        dados_depois={
            "token_version": alvo.token_version,
            "usuario_alvo": alvo.email,
            "perfil": getattr(alvo.role, "value", alvo.role),
            "justificativa": justificativa_limpa,
        },
        request=request,
        severidade="CRITICO",
        categoria="SEGURANCA",
    )
    db.commit()
    return RedirectResponse(url="/admin/usuarios-ativos?desconectado=1", status_code=302)


@router.post("/usuarios-ativos/desconectar-operacionais")
async def desconectar_usuarios_operacionais_admin(
    request: Request,
    justificativa: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    justificativa_limpa = normalize_text(justificativa)
    if not justificativa_limpa:
        raise HTTPException(status_code=400, detail="Informe a justificativa da desconexao em lote.")

    limite = utc_now() - timedelta(seconds=SESSION_IDLE_TIMEOUT_SECONDS)
    alvos = db.query(Usuario).filter(
        Usuario.ativo == True,
        Usuario.role != UserRole.ADMIN,
        Usuario.ultimo_acesso.isnot(None),
        Usuario.ultimo_acesso >= limite,
    ).all()
    dados_antes = [
        {"id": alvo.id, "email": alvo.email, "token_version": int(alvo.token_version or 0)}
        for alvo in alvos
    ]
    for alvo in alvos:
        alvo.token_version = int(alvo.token_version or 0) + 1
        alvo.session_notice_code = "ADMIN_DISCONNECT"
        alvo.session_notice_message = "O administrador desconectou sua sessao para manutencao programada do sistema."
        alvo.session_notice_at = utc_now()

    registrar_auditoria(
        db=db,
        acao="ADMIN_DESCONECTA_USUARIOS_OPERACIONAIS",
        usuario_id=usuario.id,
        tabela=Usuario.__tablename__,
        descricao="Administrador desconectou usuarios operacionais ativos para manutencao programada.",
        dados_antes={"usuarios": dados_antes},
        dados_depois={
            "quantidade": len(alvos),
            "justificativa": justificativa_limpa,
            "janela_minutos": int(SESSION_IDLE_TIMEOUT_SECONDS // 60),
        },
        request=request,
        severidade="CRITICO",
        categoria="SEGURANCA",
    )
    db.commit()
    return RedirectResponse(url=f"/admin/usuarios-ativos?desconectados={len(alvos)}", status_code=302)


@router.get("/diagnostico", response_class=HTMLResponse)
async def diagnostico_admin_page(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    diagnostico = diagnostico_operacional(db)
    return templates.TemplateResponse(
        "admin/diagnostico.html",
        {
            "request": request,
            "usuario": usuario,
            "diagnostico": diagnostico,
        },
    )


@router.get("/diagnostico/exportar.xlsx")
async def diagnostico_admin_xlsx(
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    diagnostico = diagnostico_operacional(db)
    rows: list[list[object]] = []
    for chave, valor in diagnostico["resumo"].items():
        rows.append(["RESUMO", chave, valor, "CRITICO" if valor and "negativo" in chave else "NORMAL"])
    for item in diagnostico["bloqueadores_criticos"]:
        rows.append(["BLOQUEADOR", item, "", "CRITICO"])
    for log in diagnostico["eventos_suspeitos_recentes"]:
        rows.append([
            "EVENTO",
            log.acao,
            log.descricao or "",
            log.severidade,
            log.categoria,
            log.ip_cliente or log.ip or "",
            log.ip_conexao or "",
            summarize_user_agent(log.user_agent),
            log.criado_em,
        ])

    registrar_auditoria(
        db=db,
        acao="ADMIN_EXPORTAR_DIAGNOSTICO",
        usuario_id=usuario.id,
        tabela="auditoria_logs",
        descricao="Relatorio de erro operacional exportado pelo administrador",
        dados_depois={"linhas": len(rows)},
    )
    db.commit()
    return resposta_xlsx(
        filename="diagnostico_operacional.xlsx",
        sheet_name="Diagnostico",
        headers=["Tipo", "Item", "Informacao", "Severidade", "Categoria", "IP cliente", "IP conexao/proxy", "Navegador", "Data"],
        rows=rows,
    )


@router.get("/exportacao-periodica.zip")
async def exportacao_periodica_admin(
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    filename, pacote = gerar_pacote_exportacao_admin(db)
    registrar_auditoria(
        db=db,
        acao="ADMIN_EXPORTACAO_PERIODICA",
        usuario_id=usuario.id,
        tabela="exportacao",
        descricao="Pacote administrativo periodico exportado",
        dados_depois={"arquivo": filename},
    )
    db.commit()
    return Response(
        content=pacote.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reversoes", response_class=HTMLResponse)
async def central_reversoes_page(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    solicitacoes_entregues = db.query(Solicitacao).options(
        joinedload(Solicitacao.instalador),
        joinedload(Solicitacao.entregue_por),
        joinedload(Solicitacao.itens_caixa).joinedload(SolicitacaoItemCaixa.caixa),
        joinedload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca),
    ).filter(
        Solicitacao.status == SolicitacaoStatus.ENTREGUE
    ).order_by(
        Solicitacao.entregue_em.desc(),
        Solicitacao.id.desc(),
    ).limit(20).all()

    hidrometros_instalados = db.query(Hidrometro).options(
        joinedload(Hidrometro.caixa),
        joinedload(Hidrometro.instalador_baixa),
        joinedload(Hidrometro.baixado_por),
    ).filter(
        Hidrometro.status == HidrometroStatus.INSTALADO
    ).order_by(
        Hidrometro.instalado_em.desc(),
        Hidrometro.id.desc(),
    ).limit(20).all()

    entradas_manuais = db.query(MovimentacaoPeca).options(
        joinedload(MovimentacaoPeca.tipo_peca),
        joinedload(MovimentacaoPeca.registrado_por),
    ).filter(
        MovimentacaoPeca.tipo == MovimentacaoTipo.ENTRADA,
        MovimentacaoPeca.instalador_id == None,
        MovimentacaoPeca.solicitacao_id == None,
    ).order_by(
        MovimentacaoPeca.criado_em.desc(),
        MovimentacaoPeca.id.desc(),
    ).limit(20).all()

    conferencias = db.query(ConferenciaPecas).options(
        joinedload(ConferenciaPecas.instalador),
        joinedload(ConferenciaPecas.responsavel),
    ).order_by(
        ConferenciaPecas.data_conferencia.desc(),
        ConferenciaPecas.id.desc(),
    ).limit(20).all()

    manutencoes = db.query(HidrometroManutencao).options(
        joinedload(HidrometroManutencao.hidrometro),
        joinedload(HidrometroManutencao.caixa_origem).joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(HidrometroManutencao.criado_por_usuario),
    ).filter(
        HidrometroManutencao.revertida == False,
        HidrometroManutencao.status != "DESCARTADO_TECNICO",
    ).order_by(
        HidrometroManutencao.updated_at.desc(),
        HidrometroManutencao.id.desc(),
    ).limit(20).all()

    return templates.TemplateResponse(
        "admin/reversoes.html",
        {
            "request": request,
            "usuario": usuario,
            "solicitacoes_entregues": solicitacoes_entregues,
            "hidrometros_instalados": hidrometros_instalados,
            "entradas_manuais": entradas_manuais,
            "conferencias": conferencias,
            "manutencoes": manutencoes,
        },
    )


@router.get("/manutencoes/{manutencao_id}/reverter", response_class=HTMLResponse)
async def reverter_manutencao_page(
    request: Request,
    manutencao_id: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    manutencao = _carregar_manutencao_admin(db, manutencao_id)
    if not manutencao:
        raise HTTPException(status_code=404, detail="Manutencao nao encontrada.")

    return templates.TemplateResponse(
        "admin/reverter_manutencao_hidrometro.html",
        {
            "request": request,
            "usuario": usuario,
            "manutencao": manutencao,
            "next_path": _sanitize_next_path(next_path, "/admin/reversoes"),
            "pode_voltar_caixa": caixa_origem_disponivel_para_retorno(manutencao),
            "REVERSAO_DESTINO_ESTOQUE_SOLTO": REVERSAO_DESTINO_ESTOQUE_SOLTO,
            "REVERSAO_DESTINO_CAIXA_ORIGEM": REVERSAO_DESTINO_CAIXA_ORIGEM,
        },
    )


@router.post("/manutencoes/{manutencao_id}/reverter")
async def reverter_manutencao_admin(
    request: Request,
    manutencao_id: int,
    justificativa: str = Form(""),
    confirmacao_serial: str = Form(""),
    confirmacao_reversao: str = Form(""),
    destino: str = Form(REVERSAO_DESTINO_ESTOQUE_SOLTO),
    next_path: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    manutencao = _carregar_manutencao_admin(db, manutencao_id)
    if not manutencao:
        raise HTTPException(status_code=404, detail="Manutencao nao encontrada.")

    next_safe = _sanitize_next_path(next_path, "/admin/reversoes")
    hidrometro = manutencao.hidrometro
    serial_esperado = hidrometro.numero_serie if hidrometro else ""
    justificativa_limpa = normalize_text(justificativa)
    serial_confirmado = normalize_text(confirmacao_serial, upper=True)
    confirmou_reversao = parse_bool_form(confirmacao_reversao, default=False)

    context = {
        "request": request,
        "usuario": usuario,
        "manutencao": manutencao,
        "next_path": next_safe,
        "pode_voltar_caixa": caixa_origem_disponivel_para_retorno(manutencao),
        "REVERSAO_DESTINO_ESTOQUE_SOLTO": REVERSAO_DESTINO_ESTOQUE_SOLTO,
        "REVERSAO_DESTINO_CAIXA_ORIGEM": REVERSAO_DESTINO_CAIXA_ORIGEM,
    }

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/reverter_manutencao_hidrometro.html",
            {**context, "erro": "Informe a justificativa obrigatoria para reverter esta manutencao."},
            status_code=400,
        )

    if serial_confirmado != serial_esperado:
        return templates.TemplateResponse(
            "admin/reverter_manutencao_hidrometro.html",
            {**context, "erro": "A confirmacao do serial nao confere com o hidrometro selecionado."},
            status_code=400,
        )

    if not confirmou_reversao:
        return templates.TemplateResponse(
            "admin/reverter_manutencao_hidrometro.html",
            {**context, "erro": "Confirme explicitamente que deseja reverter esta manutencao."},
            status_code=400,
        )

    try:
        reverter_manutencao_hidrometro(
            db,
            manutencao,
            usuario_id=usuario.id,
            justificativa=justificativa_limpa,
            destino=destino,
            request=request,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin/reverter_manutencao_hidrometro.html",
            {**context, "erro": str(exc)},
            status_code=409,
        )

    db.commit()
    return RedirectResponse(url=_append_query_flag(next_safe, "reversao_manutencao=1"), status_code=302)


@router.get("/movimentacoes/material/{movimento_id}/reverter")
async def redirecionar_reversao_movimentacao_material(
    movimento_id: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    movimento = _carregar_movimentacao_material_admin(db, movimento_id)
    if not movimento:
        raise HTTPException(status_code=404, detail="Movimentacao de material nao encontrada.")

    next_safe = _sanitize_next_path(next_path, "/admin/reversoes")
    if movimento.tipo == MovimentacaoTipo.BAIXA and movimento.hidrometro_id:
        return RedirectResponse(
            url=f"/admin/hidrometros/{movimento.hidrometro_id}/reverter-baixa?next_path={quote(next_safe, safe='')}",
            status_code=302,
        )

    if movimento.tipo == MovimentacaoTipo.SAIDA and movimento.solicitacao_id:
        return RedirectResponse(
            url=f"/admin/solicitacoes/{movimento.solicitacao_id}/reverter-entrega?next_path={quote(next_safe, safe='')}",
            status_code=302,
        )

    raise HTTPException(status_code=409, detail="Esta movimentacao nao possui reversao administrativa disponivel.")


@router.get("/usuarios/novo", response_class=HTMLResponse)
async def novo_usuario_page(request: Request, usuario=admin_dep):
    return _render_usuario_form(request, usuario)


@router.post("/usuarios/novo")
async def criar_usuario(
    request: Request,
    nome: str = Form(...),
    email: str = Form(...),
    senha: str = Form(...),
    role: str = Form(...),
    matricula: str = Form(""),
    cpf: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    form_data = {"nome": nome, "email": email, "role": role, "cpf": cpf, "matricula": matricula}
    email_limpo = normalize_text(email).lower()
    matricula_limpa = normalize_text(matricula)
    try:
        novo_role = UserRole(role)
    except ValueError:
        return _render_usuario_form(request, usuario, erro="Perfil de acesso invalido.", status_code=400, form_data=form_data)

    erro_admin_unico = _erro_regra_admin_unico(db, novo_role=novo_role, novo_ativo=True)
    if erro_admin_unico:
        return _render_usuario_form(request, usuario, erro=erro_admin_unico, status_code=400, form_data=form_data)

    if _usuario_existente_por_email(db, email_limpo):
        return _render_usuario_form(request, usuario, erro="Email ja cadastrado no sistema.", status_code=400, form_data=form_data)
    if matricula_limpa and _usuario_existente_por_matricula(db, matricula_limpa):
        return _render_usuario_form(request, usuario, erro="Matricula ja vinculada a outro usuario.", status_code=400, form_data=form_data)

    erro_senha = validar_politica_senha(senha, nome=nome, email=email_limpo)
    if erro_senha:
        return _render_usuario_form(request, usuario, erro=erro_senha, status_code=400, form_data=form_data)

    if novo_role == UserRole.INSTALADOR:
        _, erro_instalador, _ = _resolver_instalador_para_usuario(
            db,
            nome=nome,
            cpf=cpf,
            matricula=matricula,
        )
        if erro_instalador:
            return _render_usuario_form(request, usuario, erro=erro_instalador, status_code=400, form_data=form_data)

    novo = Usuario(
        nome=normalize_text(nome),
        email=email_limpo,
        matricula=matricula_limpa or None,
        senha_hash=hash_senha(senha),
        role=novo_role,
        ativo=True,
    )
    db.add(novo)
    db.flush()

    instalador_vinculado = None
    instalador_criado = False
    if novo_role == UserRole.INSTALADOR:
        instalador_vinculado, erro_instalador, instalador_criado = _resolver_instalador_para_usuario(
            db,
            nome=nome,
            cpf=cpf,
            matricula=matricula,
            usuario_alvo=novo,
        )
        if erro_instalador:
            db.rollback()
            return _render_usuario_form(request, usuario, erro=erro_instalador, status_code=400, form_data=form_data)

        registrar_auditoria(
            db=db,
            acao="CREATE_INSTALADOR" if instalador_criado else "VINCULAR_INSTALADOR_USUARIO",
            usuario_id=usuario.id,
            tabela="instaladores",
            registro_id=instalador_vinculado.id if instalador_vinculado else None,
            descricao=(
                f"Instalador criado pelo cadastro de usuario: {instalador_vinculado.nome}"
                if instalador_criado and instalador_vinculado
                else f"Instalador vinculado ao usuario: {novo.email}"
            ),
            dados_depois={
                "usuario_id": novo.id,
                "instalador_id": instalador_vinculado.id if instalador_vinculado else None,
                "cpf_informado": bool(instalador_vinculado.cpf if instalador_vinculado else normalize_digits(cpf)),
                "matricula": instalador_vinculado.matricula if instalador_vinculado else normalize_text(matricula),
                "origem": "cadastro_usuario",
            },
        )

    registrar_auditoria(
        db=db,
        acao="CREATE_USUARIO",
        usuario_id=usuario.id,
        tabela="usuarios",
        registro_id=novo.id,
        descricao=f"Usuario criado: {novo.email}",
        dados_depois={
            "nome": novo.nome,
            "email": novo.email,
            "matricula": novo.matricula,
            "role": novo.role.value,
            "ativo": novo.ativo,
            "instalador_id": instalador_vinculado.id if instalador_vinculado else None,
        },
    )
    db.commit()
    return RedirectResponse(url="/admin/usuarios?sucesso=1", status_code=302)


@router.get("/usuarios/{uid}/editar", response_class=HTMLResponse)
async def editar_usuario_page(
    request: Request,
    uid: int,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    editando = db.query(Usuario).filter(Usuario.id == uid).first()
    if not editando:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")
    return _render_usuario_form(request, usuario, editando=editando)


@router.post("/usuarios/{uid}/editar")
async def atualizar_usuario(
    request: Request,
    uid: int,
    nome: str = Form(...),
    email: str = Form(...),
    role: str = Form(...),
    matricula: str = Form(""),
    cpf: str = Form(""),
    ativo: str = Form("1"),
    nova_senha: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    editando = db.query(Usuario).filter(Usuario.id == uid).first()
    if not editando:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")

    form_data = {"nome": nome, "email": email, "role": role, "cpf": cpf, "matricula": matricula, "ativo": ativo}
    email_limpo = normalize_text(email).lower()
    matricula_limpa = normalize_text(matricula)
    try:
        novo_role = UserRole(role)
    except ValueError:
        return _render_usuario_form(
            request,
            usuario,
            editando=editando,
            erro="Perfil de acesso invalido.",
            status_code=400,
            form_data=form_data,
        )

    if _usuario_existente_por_email(db, email_limpo, ignorar_id=uid):
        editando.nome = normalize_text(nome)
        editando.email = email_limpo
        editando.role = novo_role
        editando.ativo = parse_bool_form(ativo, default=True)
        return _render_usuario_form(
            request,
            usuario,
            editando=editando,
            erro="Email ja cadastrado no sistema.",
            status_code=400,
            form_data=form_data,
        )
    if matricula_limpa and _usuario_existente_por_matricula(db, matricula_limpa, ignorar_id=uid):
        return _render_usuario_form(
            request,
            usuario,
            editando=editando,
            erro="Matricula ja vinculada a outro usuario.",
            status_code=400,
            form_data=form_data,
        )

    novo_status = parse_bool_form(ativo, default=True)
    erro_admin_unico = _erro_regra_admin_unico(db, alvo=editando, novo_role=novo_role, novo_ativo=novo_status)
    if erro_admin_unico:
        return _render_usuario_form(
            request,
            usuario,
            editando=editando,
            erro=erro_admin_unico,
            status_code=400,
            form_data=form_data,
        )

    if uid == usuario.id and not novo_status:
        return _render_usuario_form(
            request,
            usuario,
            editando=editando,
            erro="Voce nao pode desativar o proprio usuario.",
            status_code=400,
            form_data=form_data,
        )

    if novo_role == UserRole.INSTALADOR:
        _, erro_instalador, _ = _resolver_instalador_para_usuario(
            db,
            nome=nome,
            cpf=cpf,
            matricula=matricula,
            usuario_id_permitido=uid,
        )
        if erro_instalador:
            return _render_usuario_form(
                request,
                usuario,
                editando=editando,
                erro=erro_instalador,
                status_code=400,
                form_data=form_data,
            )

    dados_antes = {
        "nome": editando.nome,
        "email": editando.email,
        "matricula": editando.matricula,
        "role": editando.role.value,
        "ativo": editando.ativo,
        "instalador_id": editando.instalador.id if editando.instalador else None,
    }

    editando.nome = normalize_text(nome)
    editando.email = email_limpo
    editando.matricula = matricula_limpa or None
    editando.role = novo_role
    editando.ativo = novo_status
    if nova_senha.strip():
        erro_senha = validar_politica_senha(nova_senha, nome=editando.nome, email=email_limpo)
        if erro_senha:
            return _render_usuario_form(
                request,
                usuario,
                editando=editando,
                erro=erro_senha,
                status_code=400,
                form_data=form_data,
            )
        editando.senha_hash = hash_senha(nova_senha)
        editando.token_version = int(editando.token_version or 0) + 1

    instalador_vinculado = None
    instalador_criado = False
    instalador_desvinculado = None
    if novo_role == UserRole.INSTALADOR:
        instalador_vinculado, erro_instalador, instalador_criado = _resolver_instalador_para_usuario(
            db,
            nome=nome,
            cpf=cpf,
            matricula=matricula,
            usuario_alvo=editando,
        )
        if erro_instalador:
            db.rollback()
            return _render_usuario_form(
                request,
                usuario,
                editando=editando,
                erro=erro_instalador,
                status_code=400,
                form_data=form_data,
            )
        registrar_auditoria(
            db=db,
            acao="CREATE_INSTALADOR" if instalador_criado else "VINCULAR_INSTALADOR_USUARIO",
            usuario_id=usuario.id,
            tabela="instaladores",
            registro_id=instalador_vinculado.id if instalador_vinculado else None,
            descricao=(
                f"Instalador criado pelo cadastro de usuario: {instalador_vinculado.nome}"
                if instalador_criado and instalador_vinculado
                else f"Instalador vinculado ao usuario: {editando.email}"
            ),
            dados_depois={
                "usuario_id": editando.id,
                "instalador_id": instalador_vinculado.id if instalador_vinculado else None,
                "cpf_informado": bool(instalador_vinculado.cpf if instalador_vinculado else normalize_digits(cpf)),
                "matricula": instalador_vinculado.matricula if instalador_vinculado else normalize_text(matricula),
                "origem": "edicao_usuario",
            },
        )
    elif editando.instalador:
        instalador_desvinculado = editando.instalador
        instalador_desvinculado.usuario_id = None
        registrar_auditoria(
            db=db,
            acao="DESVINCULAR_INSTALADOR_USUARIO",
            usuario_id=usuario.id,
            tabela="instaladores",
            registro_id=instalador_desvinculado.id,
            descricao=f"Instalador desvinculado do usuario: {editando.email}",
            dados_depois={
                "usuario_id": editando.id,
                "instalador_id": instalador_desvinculado.id,
                "novo_role": editando.role.value,
            },
        )

    registrar_auditoria(
        db=db,
        acao="UPDATE_USUARIO",
        usuario_id=usuario.id,
        tabela="usuarios",
        registro_id=editando.id,
        descricao=f"Usuario editado: {editando.email}",
        dados_antes=dados_antes,
        dados_depois={
            "nome": editando.nome,
            "email": editando.email,
            "matricula": editando.matricula,
            "role": editando.role.value,
            "ativo": editando.ativo,
            "senha_alterada": bool(nova_senha.strip()),
            "instalador_id": instalador_vinculado.id if instalador_vinculado else None,
            "instalador_desvinculado_id": instalador_desvinculado.id if instalador_desvinculado else None,
        },
    )
    db.commit()
    return RedirectResponse(url="/admin/usuarios?sucesso=1", status_code=302)


@router.get("/limpeza/usuarios/{uid}", response_class=HTMLResponse)
async def confirmar_exclusao_usuario_page(
    request: Request,
    uid: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    alvo = _carregar_usuario_limpeza(db, uid)
    if not alvo:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")

    fallback = "/admin/usuarios"
    return templates.TemplateResponse(
        "admin/exclusao_confirmar.html",
        {
            "request": request,
            "usuario": usuario,
            "preview": _build_usuario_cleanup_preview(db, alvo, usuario),
            "submit_url": f"/admin/limpeza/usuarios/{alvo.id}",
            "next_path": _sanitize_next_path(next_path, fallback),
            "back_href": fallback,
        },
    )


@router.post("/limpeza/usuarios/{uid}")
async def excluir_usuario_admin(
    request: Request,
    uid: int,
    justificativa: str = Form(""),
    confirmacao: str = Form(""),
    confirmacao_final: str = Form(""),
    next_path: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    alvo = _carregar_usuario_limpeza(db, uid)
    if not alvo:
        raise HTTPException(status_code=404, detail="Usuario nao encontrado.")

    fallback = "/admin/usuarios"
    next_safe = _sanitize_next_path(next_path, fallback)
    preview = _build_usuario_cleanup_preview(db, alvo, usuario)
    justificativa_limpa = normalize_text(justificativa)

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/usuarios/{alvo.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Informe a justificativa da exclusao administrativa.",
            },
            status_code=400,
        )

    if normalize_text(confirmacao, upper=True) != normalize_text(preview["confirm_value"], upper=True):
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/usuarios/{alvo.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "A confirmacao digitada nao confere com o usuario selecionado.",
            },
            status_code=400,
        )

    if not parse_bool_form(confirmacao_final):
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/usuarios/{alvo.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Confirme que deseja apagar definitivamente este usuario.",
            },
            status_code=400,
        )

    if not preview["allowed"]:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/usuarios/{alvo.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Este usuario ainda nao pode ser excluido definitivamente.",
            },
            status_code=409,
        )

    dados_antes = {
        "nome": alvo.nome,
        "email": alvo.email,
        "role": alvo.role.value,
        "ativo": alvo.ativo,
    }

    try:
        db.delete(alvo)
        registrar_auditoria(
            db=db,
            acao="ADMIN_DELETE_USUARIO",
            usuario_id=usuario.id,
            tabela="usuarios",
            registro_id=uid,
            descricao=f"Usuario excluido definitivamente: {dados_antes['email']}",
            dados_antes=dados_antes,
            dados_depois={"justificativa": justificativa_limpa},
        )
        db.commit()
    except Exception:
        db.rollback()
        alvo = _carregar_usuario_limpeza(db, uid)
        preview = _build_usuario_cleanup_preview(db, alvo, usuario) if alvo else preview
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/usuarios/{uid}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Nao foi possivel excluir este usuario porque ele possui vinculos que ainda precisam ser preservados.",
            },
            status_code=409,
        )

    return RedirectResponse(url=_append_query_flag(next_safe, "limpeza=usuario_excluido"), status_code=302)


@router.get("/instaladores", response_class=HTMLResponse)
async def listar_instaladores(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    instaladores = db.query(Instalador).options(joinedload(Instalador.usuario)).order_by(Instalador.nome).all()
    return templates.TemplateResponse(
        "admin/instaladores.html",
        {"request": request, "usuario": usuario, "instaladores": instaladores},
    )


@router.get("/instaladores/novo", response_class=HTMLResponse)
async def novo_instalador_page(request: Request, usuario=admin_dep):
    return _render_instalador_form(request, usuario)


@router.post("/instaladores/novo")
async def criar_instalador(
    request: Request,
    nome: str = Form(...),
    cpf: str = Form(""),
    matricula: str = Form(...),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    cpf_limpo = normalize_digits(cpf)
    matricula_limpa = normalize_text(matricula)
    erro = _buscar_instalador_duplicado(db, cpf_limpo, matricula_limpa)
    if erro:
        return _render_instalador_form(request, usuario, erro=erro, status_code=400)

    novo = Instalador(
        nome=normalize_text(nome),
        cpf=cpf_limpo or None,
        data_nascimento=None,
        matricula=matricula_limpa,
        ativo=True,
    )
    db.add(novo)
    db.flush()

    registrar_auditoria(
        db=db,
        acao="CREATE_INSTALADOR",
        usuario_id=usuario.id,
        tabela="instaladores",
        registro_id=novo.id,
        descricao=f"Instalador criado: {novo.nome}",
        dados_depois={
            "nome": novo.nome,
            "cpf_informado": bool(novo.cpf),
            "matricula": novo.matricula,
            "ativo": novo.ativo,
        },
    )
    db.commit()
    return RedirectResponse(url="/admin/instaladores?sucesso=1", status_code=302)


@router.get("/instaladores/{iid}/editar", response_class=HTMLResponse)
async def editar_instalador_page(
    request: Request,
    iid: int,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    editando = db.query(Instalador).filter(Instalador.id == iid).first()
    if not editando:
        raise HTTPException(status_code=404, detail="Instalador nao encontrado.")
    return _render_instalador_form(request, usuario, editando=editando)


@router.post("/instaladores/{iid}/editar")
async def atualizar_instalador(
    request: Request,
    iid: int,
    nome: str = Form(...),
    cpf: str = Form(""),
    matricula: str = Form(...),
    ativo: str = Form("1"),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    editando = db.query(Instalador).filter(Instalador.id == iid).first()
    if not editando:
        raise HTTPException(status_code=404, detail="Instalador nao encontrado.")

    cpf_limpo = normalize_digits(cpf)
    matricula_limpa = normalize_text(matricula)
    erro = _buscar_instalador_duplicado(db, cpf_limpo, matricula_limpa, ignorar_id=iid)
    if erro:
        editando.nome = normalize_text(nome)
        editando.cpf = cpf_limpo or None
        editando.data_nascimento = None
        editando.matricula = matricula_limpa
        editando.ativo = parse_bool_form(ativo, default=True)
        return _render_instalador_form(request, usuario, editando=editando, erro=erro, status_code=400)

    dados_antes = {
        "nome": editando.nome,
        "cpf_informado": bool(editando.cpf),
        "matricula": editando.matricula,
        "ativo": editando.ativo,
    }

    editando.nome = normalize_text(nome)
    editando.cpf = cpf_limpo or None
    editando.data_nascimento = None
    editando.matricula = matricula_limpa
    editando.ativo = parse_bool_form(ativo, default=True)

    registrar_auditoria(
        db=db,
        acao="UPDATE_INSTALADOR",
        usuario_id=usuario.id,
        tabela="instaladores",
        registro_id=editando.id,
        descricao=f"Instalador editado: {editando.nome}",
        dados_antes=dados_antes,
        dados_depois={
            "nome": editando.nome,
            "cpf_informado": bool(editando.cpf),
            "matricula": editando.matricula,
            "ativo": editando.ativo,
        },
    )
    db.commit()
    return RedirectResponse(url="/admin/instaladores?sucesso=1", status_code=302)


@router.get("/pecas", response_class=HTMLResponse)
async def listar_pecas(
    request: Request,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    tipos = db.query(TipoPeca).options(joinedload(TipoPeca.estoque)).order_by(TipoPeca.nome).all()
    return templates.TemplateResponse(
        "admin/pecas.html",
        {"request": request, "usuario": usuario, "tipos": tipos},
    )


@router.get("/pecas/novo", response_class=HTMLResponse)
async def nova_peca_page(request: Request, usuario=admin_dep):
    return _render_peca_form(request, usuario)


@router.post("/pecas/novo")
async def criar_peca(
    request: Request,
    nome: str = Form(...),
    descricao: str = Form(""),
    unidade_medida: str = Form("unidade"),
    estoque_minimo_percentual: float = Form(20),
    quantidade_maxima: int = Form(100),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    nome_limpo = normalize_text(nome, upper=True)
    if quantidade_maxima <= 0:
        return _render_peca_form(request, usuario, erro="A quantidade maxima deve ser maior que zero.", status_code=400)
    if not 0 <= estoque_minimo_percentual <= 100:
        return _render_peca_form(request, usuario, erro="O estoque minimo deve ficar entre 0 e 100%.", status_code=400)
    if _buscar_tipo_peca_duplicado(db, nome_limpo):
        return _render_peca_form(request, usuario, erro="Ja existe um tipo de peca com esse nome.", status_code=400)

    tipo = TipoPeca(
        nome=nome_limpo,
        descricao=normalize_text(descricao) or None,
        unidade_medida=normalize_text(unidade_medida).lower() or "unidade",
        estoque_minimo_percentual=estoque_minimo_percentual,
        ativo=True,
    )
    db.add(tipo)
    db.flush()

    estoque = EstoquePeca(tipo_peca_id=tipo.id, quantidade_atual=0, quantidade_maxima=quantidade_maxima)
    db.add(estoque)
    db.flush()

    registrar_auditoria(
        db=db,
        acao="CREATE_TIPO_PECA",
        usuario_id=usuario.id,
        tabela="tipos_pecas",
        registro_id=tipo.id,
        descricao=f"Tipo de peca criado: {tipo.nome}",
        dados_depois={
            "nome": tipo.nome,
            "unidade_medida": tipo.unidade_medida,
            "estoque_minimo_percentual": tipo.estoque_minimo_percentual,
            "quantidade_maxima": estoque.quantidade_maxima,
        },
    )
    db.commit()
    return RedirectResponse(url="/admin/pecas?sucesso=1", status_code=302)


@router.get("/pecas/{tid}/editar", response_class=HTMLResponse)
async def editar_peca_page(
    request: Request,
    tid: int,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    editando = db.query(TipoPeca).options(joinedload(TipoPeca.estoque)).filter(TipoPeca.id == tid).first()
    if not editando:
        raise HTTPException(status_code=404, detail="Tipo de peca nao encontrado.")
    return _render_peca_form(request, usuario, editando=editando)


@router.post("/pecas/{tid}/editar")
async def atualizar_peca(
    request: Request,
    tid: int,
    nome: str = Form(...),
    descricao: str = Form(""),
    unidade_medida: str = Form("unidade"),
    estoque_minimo_percentual: float = Form(20),
    quantidade_maxima: int = Form(100),
    ativo: str = Form("1"),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    editando = db.query(TipoPeca).options(joinedload(TipoPeca.estoque)).filter(TipoPeca.id == tid).first()
    if not editando:
        raise HTTPException(status_code=404, detail="Tipo de peca nao encontrado.")

    nome_limpo = normalize_text(nome, upper=True)
    if quantidade_maxima <= 0:
        return _render_peca_form(request, usuario, editando=editando, erro="A quantidade maxima deve ser maior que zero.", status_code=400)
    if not 0 <= estoque_minimo_percentual <= 100:
        return _render_peca_form(request, usuario, editando=editando, erro="O estoque minimo deve ficar entre 0 e 100%.", status_code=400)
    if _buscar_tipo_peca_duplicado(db, nome_limpo, ignorar_id=tid):
        return _render_peca_form(request, usuario, editando=editando, erro="Ja existe um tipo de peca com esse nome.", status_code=400)

    if not editando.estoque:
        editando.estoque = EstoquePeca(tipo_peca_id=editando.id, quantidade_atual=0, quantidade_maxima=quantidade_maxima)
        db.add(editando.estoque)
        db.flush()

    dados_antes = {
        "nome": editando.nome,
        "descricao": editando.descricao,
        "unidade_medida": editando.unidade_medida,
        "estoque_minimo_percentual": editando.estoque_minimo_percentual,
        "quantidade_maxima": editando.estoque.quantidade_maxima,
        "ativo": editando.ativo,
    }

    editando.nome = nome_limpo
    editando.descricao = normalize_text(descricao) or None
    editando.unidade_medida = normalize_text(unidade_medida).lower() or "unidade"
    editando.estoque_minimo_percentual = estoque_minimo_percentual
    editando.ativo = parse_bool_form(ativo, default=True)
    editando.estoque.quantidade_maxima = quantidade_maxima

    registrar_auditoria(
        db=db,
        acao="UPDATE_TIPO_PECA",
        usuario_id=usuario.id,
        tabela="tipos_pecas",
        registro_id=editando.id,
        descricao=f"Tipo de peca editado: {editando.nome}",
        dados_antes=dados_antes,
        dados_depois={
            "nome": editando.nome,
            "descricao": editando.descricao,
            "unidade_medida": editando.unidade_medida,
            "estoque_minimo_percentual": editando.estoque_minimo_percentual,
            "quantidade_maxima": editando.estoque.quantidade_maxima,
            "ativo": editando.ativo,
        },
    )
    db.commit()
    return RedirectResponse(url="/admin/pecas?sucesso=1", status_code=302)


@router.get("/limpeza/caixas/{caixa_id}", response_class=HTMLResponse)
async def confirmar_exclusao_caixa_page(
    request: Request,
    caixa_id: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    caixa = _carregar_caixa_limpeza(db, caixa_id)
    if not caixa:
        raise HTTPException(status_code=404, detail="Caixa nao encontrada.")

    fallback = f"/almoxarifado/caixas/{caixa.id}"
    next_safe = _sanitize_caixa_cleanup_next_path(next_path, caixa.id)
    preview = build_caixa_cleanup_preview(db, caixa)
    return templates.TemplateResponse(
        "admin/exclusao_confirmar.html",
        {
            "request": request,
            "usuario": usuario,
            "preview": preview,
            "submit_url": f"/admin/limpeza/caixas/{caixa.id}",
            "next_path": next_safe,
            "back_href": fallback,
        },
    )


@router.post("/limpeza/caixas/{caixa_id}")
async def excluir_caixa_admin(
    request: Request,
    caixa_id: int,
    justificativa: str = Form(""),
    confirmacao: str = Form(""),
    confirmacao_final: str = Form(""),
    next_path: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    caixa = _carregar_caixa_limpeza(db, caixa_id)
    if not caixa:
        raise HTTPException(status_code=404, detail="Caixa nao encontrada.")

    preview = build_caixa_cleanup_preview(db, caixa)
    fallback = f"/almoxarifado/caixas/{caixa.id}"
    next_safe = _sanitize_caixa_cleanup_next_path(next_path, caixa.id)
    justificativa_limpa = normalize_text(justificativa)

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/caixas/{caixa.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Informe a justificativa da exclusao administrativa.",
            },
            status_code=400,
        )

    if normalize_text(confirmacao, upper=True) != normalize_text(preview["confirm_value"], upper=True):
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/caixas/{caixa.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "A confirmacao digitada nao confere com a caixa selecionada.",
            },
            status_code=400,
        )

    if not parse_bool_form(confirmacao_final):
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/caixas/{caixa.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Confirme que deseja apagar definitivamente esta caixa.",
            },
            status_code=400,
        )

    if not preview["allowed"]:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/caixas/{caixa.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Esta caixa ainda nao pode ser excluida definitivamente.",
            },
            status_code=409,
        )

    try:
        resultado = execute_caixa_cleanup(db, caixa)
    except CleanupBlockedError as exc:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": build_caixa_cleanup_preview(db, caixa),
                "submit_url": f"/admin/limpeza/caixas/{caixa.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": str(exc),
            },
            status_code=409,
        )

    registrar_auditoria(
        db=db,
        acao="ADMIN_DELETE_CAIXA",
        usuario_id=usuario.id,
        tabela="caixas_hidrometros",
        registro_id=caixa_id,
        descricao=f"Caixa excluida definitivamente: {preview['entity_title']}",
        dados_antes={
            "numero_interno": caixa.numero_interno,
            "serial_number": caixa.serial_number,
            "ativo": caixa.ativo,
        },
        dados_depois={
            "justificativa": justificativa_limpa,
            **resultado,
        },
    )
    db.commit()
    return RedirectResponse(url=_append_query_flag(next_safe, "limpeza=caixa_excluida"), status_code=302)


@router.get("/limpeza/solicitacoes/{solicitacao_id}", response_class=HTMLResponse)
async def confirmar_exclusao_solicitacao_page(
    request: Request,
    solicitacao_id: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    solicitacao = _carregar_solicitacao_limpeza(db, solicitacao_id)
    if not solicitacao:
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")

    fallback = "/manipulador/solicitacoes"
    preview = build_solicitacao_cleanup_preview(db, solicitacao)
    return templates.TemplateResponse(
        "admin/exclusao_confirmar.html",
        {
            "request": request,
            "usuario": usuario,
            "preview": preview,
            "submit_url": f"/admin/limpeza/solicitacoes/{solicitacao.id}",
            "next_path": _sanitize_next_path(next_path, fallback),
            "back_href": fallback,
        },
    )


@router.post("/limpeza/solicitacoes/{solicitacao_id}")
async def excluir_solicitacao_admin(
    request: Request,
    solicitacao_id: int,
    justificativa: str = Form(""),
    confirmacao: str = Form(""),
    confirmacao_final: str = Form(""),
    next_path: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    solicitacao = _carregar_solicitacao_limpeza(db, solicitacao_id)
    if not solicitacao:
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")

    preview = build_solicitacao_cleanup_preview(db, solicitacao)
    fallback = "/manipulador/solicitacoes"
    next_safe = _sanitize_next_path(next_path, fallback)
    justificativa_limpa = normalize_text(justificativa)

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/solicitacoes/{solicitacao.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Informe a justificativa da exclusao administrativa.",
            },
            status_code=400,
        )

    if normalize_text(confirmacao, upper=True) != normalize_text(preview["confirm_value"], upper=True):
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/solicitacoes/{solicitacao.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "A confirmacao digitada nao confere com a solicitacao selecionada.",
            },
            status_code=400,
        )

    if not parse_bool_form(confirmacao_final):
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/solicitacoes/{solicitacao.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Confirme que deseja apagar definitivamente esta solicitacao.",
            },
            status_code=400,
        )

    if not preview["allowed"]:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/solicitacoes/{solicitacao.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Esta solicitacao ainda nao pode ser excluida definitivamente.",
            },
            status_code=409,
        )

    try:
        resultado = execute_solicitacao_cleanup(db, solicitacao)
    except CleanupBlockedError as exc:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": build_solicitacao_cleanup_preview(db, solicitacao),
                "submit_url": f"/admin/limpeza/solicitacoes/{solicitacao.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": str(exc),
            },
            status_code=409,
        )

    registrar_auditoria(
        db=db,
        acao="ADMIN_DELETE_SOLICITACAO",
        usuario_id=usuario.id,
        tabela="solicitacoes",
        registro_id=solicitacao_id,
        descricao=f"Solicitacao excluida definitivamente: #{solicitacao_id}",
        dados_antes={
            "solicitacao_id": solicitacao_id,
            "status": preview["details"][0][1],
            "instalador": preview["entity_subtitle"],
        },
        dados_depois={
            "justificativa": justificativa_limpa,
            **resultado,
        },
    )
    db.commit()
    return RedirectResponse(url=_append_query_flag(next_safe, "limpeza=solicitacao_excluida"), status_code=302)


@router.get("/limpeza/instaladores/{instalador_id}", response_class=HTMLResponse)
async def confirmar_exclusao_instalador_page(
    request: Request,
    instalador_id: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    instalador = _carregar_instalador_limpeza(db, instalador_id)
    if not instalador:
        raise HTTPException(status_code=404, detail="Instalador nao encontrado.")

    fallback = "/admin/instaladores"
    preview = build_instalador_cleanup_preview(db, instalador)
    return templates.TemplateResponse(
        "admin/exclusao_confirmar.html",
        {
            "request": request,
            "usuario": usuario,
            "preview": preview,
            "submit_url": f"/admin/limpeza/instaladores/{instalador.id}",
            "next_path": _sanitize_next_path(next_path, fallback),
            "back_href": fallback,
        },
    )


@router.post("/limpeza/instaladores/{instalador_id}")
async def excluir_instalador_admin(
    request: Request,
    instalador_id: int,
    justificativa: str = Form(""),
    confirmacao: str = Form(""),
    confirmacao_final: str = Form(""),
    next_path: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    instalador = _carregar_instalador_limpeza(db, instalador_id)
    if not instalador:
        raise HTTPException(status_code=404, detail="Instalador nao encontrado.")

    preview = build_instalador_cleanup_preview(db, instalador)
    fallback = "/admin/instaladores"
    next_safe = _sanitize_next_path(next_path, fallback)
    justificativa_limpa = normalize_text(justificativa)

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/instaladores/{instalador.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Informe a justificativa da exclusao administrativa.",
            },
            status_code=400,
        )

    if normalize_text(confirmacao, upper=True) != normalize_text(preview["confirm_value"], upper=True):
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/instaladores/{instalador.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "A confirmacao digitada nao confere com o instalador selecionado.",
            },
            status_code=400,
        )

    if not parse_bool_form(confirmacao_final):
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/instaladores/{instalador.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Confirme que deseja apagar definitivamente este instalador.",
            },
            status_code=400,
        )

    if not preview["allowed"]:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": preview,
                "submit_url": f"/admin/limpeza/instaladores/{instalador.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": "Este instalador ainda nao pode ser excluido definitivamente.",
            },
            status_code=409,
        )

    try:
        resultado = execute_instalador_cleanup(db, instalador)
    except CleanupBlockedError as exc:
        return templates.TemplateResponse(
            "admin/exclusao_confirmar.html",
            {
                "request": request,
                "usuario": usuario,
                "preview": build_instalador_cleanup_preview(db, instalador),
                "submit_url": f"/admin/limpeza/instaladores/{instalador.id}",
                "next_path": next_safe,
                "back_href": fallback,
                "erro": str(exc),
            },
            status_code=409,
        )

    registrar_auditoria(
        db=db,
        acao="ADMIN_DELETE_INSTALADOR",
        usuario_id=usuario.id,
        tabela="instaladores",
        registro_id=instalador_id,
        descricao=f"Instalador excluido definitivamente: {preview['entity_title']}",
        dados_antes={
            "nome": instalador.nome,
            "matricula": instalador.matricula,
            "ativo": instalador.ativo,
        },
        dados_depois={
            "justificativa": justificativa_limpa,
            **resultado,
        },
    )
    db.commit()
    return RedirectResponse(url=_append_query_flag(next_safe, "limpeza=instalador_excluido"), status_code=302)


@router.get("/hidrometros/{hidrometro_id}/override-baixa", response_class=HTMLResponse)
async def override_baixa_hidrometro_page(
    request: Request,
    hidrometro_id: int,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    hidrometro = _carregar_hidrometro_admin(db, hidrometro_id)
    if not hidrometro:
        raise HTTPException(status_code=404, detail="Hidrometro nao encontrado.")

    return templates.TemplateResponse(
        "admin/override_hidrometro.html",
        {"request": request, "usuario": usuario, "hidrometro": hidrometro},
    )


@router.post("/hidrometros/{hidrometro_id}/override-baixa")
async def override_baixa_hidrometro(
    request: Request,
    hidrometro_id: int,
    justificativa: str = Form(...),
    confirmacao_serial: str = Form(...),
    confirmacao_override: str = Form(""),
    observacoes: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    hidrometro = _carregar_hidrometro_admin(db, hidrometro_id)
    if not hidrometro:
        raise HTTPException(status_code=404, detail="Hidrometro nao encontrado.")

    justificativa_limpa = normalize_text(justificativa)
    serial_confirmado = normalize_text(confirmacao_serial, upper=True)
    confirmou_override = parse_bool_form(confirmacao_override, default=False)

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/override_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "erro": "Informe a justificativa obrigatoria para a baixa administrativa.",
            },
            status_code=400,
        )

    if serial_confirmado != hidrometro.numero_serie:
        return templates.TemplateResponse(
            "admin/override_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "erro": "A confirmacao do serial nao confere com o hidrometro selecionado.",
            },
            status_code=400,
        )

    if not confirmou_override:
        return templates.TemplateResponse(
            "admin/override_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "erro": "Confirme explicitamente que deseja executar a baixa administrativa.",
            },
            status_code=400,
        )

    try:
        resultado = aplicar_baixa_hidrometro(
            db,
            hidrometro,
            usuario.id,
            observacoes=observacoes,
            override=True,
            justificativa_override=justificativa_limpa,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin/override_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "erro": str(exc),
            },
            status_code=400,
        )

    registrar_auditoria(
        db=db,
        acao="OVERRIDE_BAIXA_HIDROMETRO",
        usuario_id=usuario.id,
        tabela=Hidrometro.__tablename__,
        registro_id=hidrometro.id,
        descricao=f"Baixa administrativa do hidrometro {hidrometro.numero_serie}",
        dados_depois={
            "numero_serie": hidrometro.numero_serie,
            "caixa_id": hidrometro.caixa_id,
            "instalador_id_original": resultado["instalador_id"],
            "instalado_em": resultado["momento"],
            "justificativa_override": resultado["justificativa_override"],
            "observacoes": resultado["observacoes"],
        },
    )
    if resultado.get("caixa_finalizada") and hidrometro.caixa:
        registrar_auditoria(
            db=db,
            acao="ADMIN_FINALIZACAO_CAIXA",
            usuario_id=usuario.id,
            tabela=CaixaHidrometro.__tablename__,
            registro_id=hidrometro.caixa_id,
            descricao=f"Caixa finalizada por baixa administrativa: {hidrometro.caixa.numero_interno}",
            dados_depois={
                "numero_interno": hidrometro.caixa.numero_interno,
                "status": hidrometro.caixa.status.value,
                "solicitacao_id": resultado.get("solicitacao_id"),
                "justificativa_override": resultado["justificativa_override"],
            },
        )
    db.commit()
    return RedirectResponse(
        url=f"/manipulador/rastrear?numero_serie={quote(hidrometro.numero_serie)}&override=1",
        status_code=302,
    )


@router.get("/solicitacoes/{solicitacao_id}/reverter-entrega", response_class=HTMLResponse)
async def reverter_entrega_solicitacao_page(
    request: Request,
    solicitacao_id: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    solicitacao = carregar_solicitacao_operacional(db, solicitacao_id)
    if not solicitacao:
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")

    fallback = "/admin/reversoes"
    return templates.TemplateResponse(
        "admin/reverter_entrega_solicitacao.html",
        {
            "request": request,
            "usuario": usuario,
            "solicitacao": solicitacao,
            "next_path": _sanitize_next_path(next_path, fallback),
            "erro_contexto": mensagem_reversao_entrega_solicitacao(db, solicitacao),
        },
    )


@router.post("/solicitacoes/{solicitacao_id}/reverter-entrega")
async def reverter_entrega_solicitacao_admin(
    request: Request,
    solicitacao_id: int,
    justificativa: str = Form(""),
    confirmacao: str = Form(""),
    confirmacao_final: str = Form(""),
    next_path: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    solicitacao = carregar_solicitacao_operacional(db, solicitacao_id)
    if not solicitacao:
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")

    fallback = "/admin/reversoes"
    next_safe = _sanitize_next_path(next_path, fallback)
    justificativa_limpa = normalize_text(justificativa)
    confirmacao_esperada = str(solicitacao.id)
    confirmou_reversao = parse_bool_form(confirmacao_final, default=False)
    erro_contexto = mensagem_reversao_entrega_solicitacao(db, solicitacao)

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/reverter_entrega_solicitacao.html",
            {
                "request": request,
                "usuario": usuario,
                "solicitacao": solicitacao,
                "next_path": next_safe,
                "erro_contexto": erro_contexto,
                "erro": "Informe a justificativa obrigatoria para reverter esta entrega.",
            },
            status_code=400,
        )

    if normalize_text(confirmacao) != confirmacao_esperada:
        return templates.TemplateResponse(
            "admin/reverter_entrega_solicitacao.html",
            {
                "request": request,
                "usuario": usuario,
                "solicitacao": solicitacao,
                "next_path": next_safe,
                "erro_contexto": erro_contexto,
                "erro": "A confirmacao digitada nao confere com a solicitacao selecionada.",
            },
            status_code=400,
        )

    if not confirmou_reversao:
        return templates.TemplateResponse(
            "admin/reverter_entrega_solicitacao.html",
            {
                "request": request,
                "usuario": usuario,
                "solicitacao": solicitacao,
                "next_path": next_safe,
                "erro_contexto": erro_contexto,
                "erro": "Confirme explicitamente que deseja reverter esta entrega.",
            },
            status_code=400,
        )

    dados_antes = {
        "solicitacao_id": solicitacao.id,
        "status": solicitacao.status.value,
        "instalador_id": solicitacao.instalador_id,
        "entregue_em": solicitacao.entregue_em,
        "entregue_por_id": solicitacao.entregue_por_id,
        "caixas": [item.caixa.numero_interno if item.caixa else None for item in solicitacao.itens_caixa],
        "pecas": [
            {
                "tipo_peca_id": item.tipo_peca_id,
                "tipo_peca": item.tipo_peca.nome if item.tipo_peca else None,
                "quantidade": item.quantidade_solicitada,
            }
            for item in solicitacao.itens_peca
        ],
    }

    try:
        resultado = reverter_entrega_solicitacao(
            db,
            solicitacao,
            usuario.id,
            justificativa=justificativa_limpa,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin/reverter_entrega_solicitacao.html",
            {
                "request": request,
                "usuario": usuario,
                "solicitacao": solicitacao,
                "next_path": next_safe,
                "erro_contexto": erro_contexto,
                "erro": str(exc),
            },
            status_code=409,
        )

    registrar_auditoria(
        db=db,
        acao="ADMIN_REVERSE_ENTREGA_SOLICITACAO",
        usuario_id=usuario.id,
        tabela=Solicitacao.__tablename__,
        registro_id=solicitacao.id,
        descricao=f"Entrega revertida administrativamente da solicitacao #{solicitacao.id}",
        dados_antes=dados_antes,
        dados_depois={
            "status_restaurado": resultado["status_restaurado"],
            "caixas_revertidas": resultado["caixas_revertidas"],
            "pecas_revertidas": resultado["pecas_revertidas"],
            "justificativa": justificativa_limpa,
        },
    )
    db.commit()
    return RedirectResponse(url=_append_query_flag(next_safe, "reversao_entrega=1"), status_code=302)


@router.get("/hidrometros/{hidrometro_id}/reverter-baixa", response_class=HTMLResponse)
async def reverter_baixa_hidrometro_page(
    request: Request,
    hidrometro_id: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    hidrometro = _carregar_hidrometro_admin(db, hidrometro_id)
    if not hidrometro:
        raise HTTPException(status_code=404, detail="Hidrometro nao encontrado.")

    movimento_baixa = buscar_movimentacao_baixa(db, hidrometro)
    contexto_retorno = resolver_contexto_retorno_baixa(hidrometro, movimento_baixa)
    instalador_retorno = hidrometro.instalador_baixa or (movimento_baixa.instalador if movimento_baixa else None)
    if instalador_retorno is None and contexto_retorno["instalador_id"]:
        instalador_retorno = db.query(Instalador).filter(Instalador.id == contexto_retorno["instalador_id"]).first()

    return templates.TemplateResponse(
        "admin/reverter_baixa_hidrometro.html",
        {
            "request": request,
            "usuario": usuario,
            "hidrometro": hidrometro,
            "movimento_baixa": movimento_baixa,
            "instalador_retorno": instalador_retorno,
            "contexto_retorno": contexto_retorno,
            "next_path": _sanitize_next_path(next_path, "/admin/reversoes"),
        },
    )


@router.post("/hidrometros/{hidrometro_id}/reverter-baixa")
async def reverter_baixa_hidrometro_admin(
    request: Request,
    hidrometro_id: int,
    justificativa: str = Form(""),
    confirmacao_serial: str = Form(""),
    confirmacao_reversao: str = Form(""),
    next_path: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    hidrometro = _carregar_hidrometro_admin(db, hidrometro_id)
    if not hidrometro:
        raise HTTPException(status_code=404, detail="Hidrometro nao encontrado.")

    movimento_baixa = buscar_movimentacao_baixa(db, hidrometro)
    contexto_retorno = resolver_contexto_retorno_baixa(hidrometro, movimento_baixa)
    instalador_retorno = hidrometro.instalador_baixa or (movimento_baixa.instalador if movimento_baixa else None)
    if instalador_retorno is None and contexto_retorno["instalador_id"]:
        instalador_retorno = db.query(Instalador).filter(Instalador.id == contexto_retorno["instalador_id"]).first()

    justificativa_limpa = normalize_text(justificativa)
    serial_confirmado = normalize_text(confirmacao_serial, upper=True)
    confirmou_reversao = parse_bool_form(confirmacao_reversao, default=False)
    next_safe = _sanitize_next_path(next_path, "/admin/reversoes")

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/reverter_baixa_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "movimento_baixa": movimento_baixa,
                "instalador_retorno": instalador_retorno,
                "contexto_retorno": contexto_retorno,
                "next_path": next_safe,
                "erro": "Informe a justificativa obrigatoria para remover esta baixa.",
            },
            status_code=400,
        )

    if serial_confirmado != hidrometro.numero_serie:
        return templates.TemplateResponse(
            "admin/reverter_baixa_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "movimento_baixa": movimento_baixa,
                "instalador_retorno": instalador_retorno,
                "contexto_retorno": contexto_retorno,
                "next_path": next_safe,
                "erro": "A confirmacao do serial nao confere com o hidrometro selecionado.",
            },
            status_code=400,
        )

    if not confirmou_reversao:
        return templates.TemplateResponse(
            "admin/reverter_baixa_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "movimento_baixa": movimento_baixa,
                "instalador_retorno": instalador_retorno,
                "contexto_retorno": contexto_retorno,
                "next_path": next_safe,
                "erro": "Confirme explicitamente que deseja remover esta baixa instalada.",
            },
            status_code=400,
        )

    dados_antes = {
        "numero_serie": hidrometro.numero_serie,
        "status": hidrometro.status.value,
        "caixa_id": hidrometro.caixa_id,
        "instalado_em": hidrometro.instalado_em,
        "baixado_por_id": hidrometro.baixado_por_id,
        "instalador_baixa_id": hidrometro.instalador_baixa_id,
        "movimentacao_baixa_id": movimento_baixa.id if movimento_baixa else None,
    }

    try:
        resultado = reverter_baixa_hidrometro(db, hidrometro)
    except ValueError as exc:
        return templates.TemplateResponse(
            "admin/reverter_baixa_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "movimento_baixa": movimento_baixa,
                "instalador_retorno": instalador_retorno,
                "contexto_retorno": contexto_retorno,
                "next_path": next_safe,
                "erro": str(exc),
            },
            status_code=400,
        )

    registrar_auditoria(
        db=db,
        acao="ADMIN_REVERSE_BAIXA_HIDROMETRO",
        usuario_id=usuario.id,
        tabela=Hidrometro.__tablename__,
        registro_id=hidrometro.id,
        descricao=f"Baixa removida administrativamente do hidrometro {hidrometro.numero_serie}",
        dados_antes=dados_antes,
        dados_depois={
            "numero_serie": hidrometro.numero_serie,
            "status_restaurado": hidrometro.status.value,
            "instalador_id_restaurado": resultado["instalador_id_restaurado"],
            "contexto_retorno": resultado["contexto_retorno"],
            "movimentacao_baixa_removida_id": resultado["movimentacao_id"],
            "justificativa": justificativa_limpa,
        },
    )
    db.commit()
    if next_path:
        return RedirectResponse(url=_append_query_flag(next_safe, "reversao_baixa=1"), status_code=302)
    return RedirectResponse(
        url=f"/manipulador/rastrear?numero_serie={quote(hidrometro.numero_serie)}&reversao=1",
        status_code=302,
    )


@router.get("/pecas/movimentacoes/{movimento_id}/estornar-entrada", response_class=HTMLResponse)
async def estornar_entrada_peca_page(
    request: Request,
    movimento_id: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    movimento = _carregar_movimentacao_entrada_peca_admin(db, movimento_id)
    if not movimento:
        raise HTTPException(status_code=404, detail="Movimentacao de peca nao encontrada.")

    return templates.TemplateResponse(
        "admin/estornar_entrada_peca.html",
        {
            "request": request,
            "usuario": usuario,
            "movimento": movimento,
            "next_path": _sanitize_next_path(next_path, "/admin/reversoes"),
        },
    )


@router.post("/pecas/movimentacoes/{movimento_id}/estornar-entrada")
async def estornar_entrada_peca_admin(
    request: Request,
    movimento_id: int,
    justificativa: str = Form(""),
    confirmacao: str = Form(""),
    confirmacao_final: str = Form(""),
    next_path: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    movimento = _carregar_movimentacao_entrada_peca_admin(db, movimento_id)
    if not movimento:
        raise HTTPException(status_code=404, detail="Movimentacao de peca nao encontrada.")

    tipo = movimento.tipo_peca
    estoque = tipo.estoque if tipo else None
    justificativa_limpa = normalize_text(justificativa)
    confirmacao_esperada = str(movimento.id)
    next_safe = _sanitize_next_path(next_path, "/admin/reversoes")

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/estornar_entrada_peca.html",
            {
                "request": request,
                "usuario": usuario,
                "movimento": movimento,
                "next_path": next_safe,
                "erro": "Informe a justificativa obrigatoria para estornar esta entrada.",
            },
            status_code=400,
        )

    if normalize_text(confirmacao) != confirmacao_esperada:
        return templates.TemplateResponse(
            "admin/estornar_entrada_peca.html",
            {
                "request": request,
                "usuario": usuario,
                "movimento": movimento,
                "next_path": next_safe,
                "erro": "A confirmacao digitada nao confere com a movimentacao selecionada.",
            },
            status_code=400,
        )

    if not parse_bool_form(confirmacao_final):
        return templates.TemplateResponse(
            "admin/estornar_entrada_peca.html",
            {
                "request": request,
                "usuario": usuario,
                "movimento": movimento,
                "next_path": next_safe,
                "erro": "Confirme que deseja estornar esta entrada manual.",
            },
            status_code=400,
        )

    if movimento.tipo != MovimentacaoTipo.ENTRADA or movimento.instalador_id or movimento.solicitacao_id:
        return templates.TemplateResponse(
            "admin/estornar_entrada_peca.html",
            {
                "request": request,
                "usuario": usuario,
                "movimento": movimento,
                "next_path": next_safe,
                "erro": "Somente entradas manuais de estoque podem ser estornadas por este fluxo.",
            },
            status_code=409,
        )

    if not tipo or not estoque:
        return templates.TemplateResponse(
            "admin/estornar_entrada_peca.html",
            {
                "request": request,
                "usuario": usuario,
                "movimento": movimento,
                "next_path": next_safe,
                "erro": "O tipo de peca desta entrada nao esta mais disponivel para estorno seguro.",
            },
            status_code=409,
        )

    if estoque.quantidade_atual < movimento.quantidade:
        return templates.TemplateResponse(
            "admin/estornar_entrada_peca.html",
            {
                "request": request,
                "usuario": usuario,
                "movimento": movimento,
                "next_path": next_safe,
                "erro": "O estoque atual ja foi consumido parcialmente e nao comporta o estorno integral desta entrada.",
            },
            status_code=409,
        )

    momento = utc_now()
    dados_antes = {
        "movimentacao_id": movimento.id,
        "tipo_peca": tipo.nome,
        "quantidade": movimento.quantidade,
        "estoque_atual": estoque.quantidade_atual,
        "observacoes": movimento.observacoes,
    }

    estoque.quantidade_atual -= movimento.quantidade
    estoque.atualizado_em = momento
    db.add(
        MovimentacaoPeca(
            tipo=MovimentacaoTipo.SAIDA,
            tipo_peca_id=tipo.id,
            quantidade=movimento.quantidade,
            instalador_id=None,
            solicitacao_id=None,
            observacoes=f"Estorno admin da entrada #{movimento.id} | motivo: {justificativa_limpa}",
            registrado_por_id=usuario.id,
            criado_em=momento,
        )
    )

    registrar_auditoria(
        db=db,
        acao="ADMIN_REVERSE_ENTRADA_PECA",
        usuario_id=usuario.id,
        tabela="movimentacoes_pecas",
        registro_id=movimento.id,
        descricao=f"Entrada de peca estornada: {tipo.nome}",
        dados_antes=dados_antes,
        dados_depois={
            "tipo_peca": tipo.nome,
            "quantidade_estornada": movimento.quantidade,
            "estoque_resultante": estoque.quantidade_atual,
            "justificativa": justificativa_limpa,
        },
    )
    db.commit()
    if next_path:
        return RedirectResponse(url=_append_query_flag(next_safe, "estorno=1"), status_code=302)
    return RedirectResponse(url="/almoxarifado/estoque?estorno=1", status_code=302)


@router.get("/conferencias/{conferencia_id}/reverter", response_class=HTMLResponse)
async def reverter_conferencia_peca_page(
    request: Request,
    conferencia_id: int,
    next_path: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    conferencia = _carregar_conferencia_admin(db, conferencia_id)
    if not conferencia:
        raise HTTPException(status_code=404, detail="Conferencia nao encontrada.")

    return templates.TemplateResponse(
        "admin/reverter_conferencia_peca.html",
        {
            "request": request,
            "usuario": usuario,
            "conferencia": conferencia,
            "next_path": _sanitize_next_path(next_path, "/admin/reversoes"),
        },
    )


@router.post("/conferencias/{conferencia_id}/reverter")
async def reverter_conferencia_peca_admin(
    request: Request,
    conferencia_id: int,
    justificativa: str = Form(""),
    confirmacao: str = Form(""),
    confirmacao_final: str = Form(""),
    next_path: str = Form(""),
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    conferencia = _carregar_conferencia_admin(db, conferencia_id)
    if not conferencia:
        raise HTTPException(status_code=404, detail="Conferencia nao encontrada.")

    justificativa_limpa = normalize_text(justificativa)
    confirmacao_esperada = str(conferencia.id)
    confirmou_reversao = parse_bool_form(confirmacao_final, default=False)
    next_safe = _sanitize_next_path(next_path, "/admin/reversoes")

    if not justificativa_limpa:
        return templates.TemplateResponse(
            "admin/reverter_conferencia_peca.html",
            {
                "request": request,
                "usuario": usuario,
                "conferencia": conferencia,
                "next_path": next_safe,
                "erro": "Informe a justificativa obrigatoria para remover esta conferencia.",
            },
            status_code=400,
        )

    if normalize_text(confirmacao) != confirmacao_esperada:
        return templates.TemplateResponse(
            "admin/reverter_conferencia_peca.html",
            {
                "request": request,
                "usuario": usuario,
                "conferencia": conferencia,
                "next_path": next_safe,
                "erro": "A confirmacao digitada nao confere com a conferencia selecionada.",
            },
            status_code=400,
        )

    if not confirmou_reversao:
        return templates.TemplateResponse(
            "admin/reverter_conferencia_peca.html",
            {
                "request": request,
                "usuario": usuario,
                "conferencia": conferencia,
                "next_path": next_safe,
                "erro": "Confirme explicitamente que deseja reverter esta conferencia.",
            },
            status_code=400,
        )

    momento = utc_now()
    dados_antes = {
        "conferencia_id": conferencia.id,
        "instalador": conferencia.instalador.nome if conferencia.instalador else None,
        "responsavel": conferencia.responsavel.nome if conferencia.responsavel else None,
        "data_conferencia": conferencia.data_conferencia,
        "tem_divergencia": conferencia.tem_divergencia,
        "itens": [
            {
                "tipo_peca_id": item.tipo_peca_id,
                "tipo_peca": item.tipo_peca.nome if item.tipo_peca else None,
                "quantidade_sistema": item.quantidade_sistema,
                "quantidade_real": item.quantidade_real,
                "diferenca": item.diferenca,
            }
            for item in conferencia.itens
        ],
    }

    modos_reversao: dict[int, str] = {}
    for item in conferencia.itens:
        posse = db.query(InstaladorPeca).filter(
            InstaladorPeca.instalador_id == conferencia.instalador_id,
            InstaladorPeca.tipo_peca_id == item.tipo_peca_id,
        ).first()
        quantidade_atual = posse.quantidade if posse else 0
        if quantidade_atual == item.quantidade_real:
            modos_reversao[item.id] = "restaurar_saldo"
        elif quantidade_atual == item.quantidade_sistema:
            modos_reversao[item.id] = "informativo_sem_saldo"
        else:
            return templates.TemplateResponse(
                "admin/reverter_conferencia_peca.html",
                {
                    "request": request,
                    "usuario": usuario,
                    "conferencia": conferencia,
                    "next_path": next_safe,
                    "erro": (
                        f"A peca {item.tipo_peca.nome if item.tipo_peca else item.tipo_peca_id} "
                        "ja sofreu movimentacoes posteriores. Revise o saldo atual antes de reverter."
                    ),
                },
                status_code=409,
            )

    movimentos_reversao = 0
    for item in conferencia.itens:
        if modos_reversao.get(item.id) == "informativo_sem_saldo":
            continue

        posse = db.query(InstaladorPeca).filter(
            InstaladorPeca.instalador_id == conferencia.instalador_id,
            InstaladorPeca.tipo_peca_id == item.tipo_peca_id,
        ).first()
        if not posse and item.quantidade_sistema > 0:
            posse = InstaladorPeca(
                instalador_id=conferencia.instalador_id,
                tipo_peca_id=item.tipo_peca_id,
                quantidade=0,
            )
            db.add(posse)
            db.flush()

        if posse:
            posse.quantidade = item.quantidade_sistema
            posse.atualizado_em = momento

        if item.diferenca != 0:
            db.add(
                MovimentacaoPeca(
                    tipo=MovimentacaoTipo.SAIDA if item.diferenca > 0 else MovimentacaoTipo.ENTRADA,
                    tipo_peca_id=item.tipo_peca_id,
                    quantidade=abs(item.diferenca),
                    instalador_id=conferencia.instalador_id,
                    solicitacao_id=None,
                    observacoes=(
                        f"Reversao admin da conferencia #{conferencia.id} | "
                        f"motivo: {justificativa_limpa}"
                    ),
                    registrado_por_id=usuario.id,
                    criado_em=momento,
                )
            )
            movimentos_reversao += 1

    registros_informativos = db.query(ConferenciaInstaladorPeca).filter(
        ConferenciaInstaladorPeca.conferencia_id == conferencia.id
    ).all()
    for registro in registros_informativos:
        db.delete(registro)

    for item in list(conferencia.itens):
        db.delete(item)
    db.delete(conferencia)

    registrar_auditoria(
        db=db,
        acao="ADMIN_REVERSE_CONFERENCIA_PECA",
        usuario_id=usuario.id,
        tabela="conferencias_pecas",
        registro_id=conferencia_id,
        descricao=f"Conferencia revertida administrativamente: #{conferencia_id}",
        dados_antes=dados_antes,
        dados_depois={
            "conferencia_id": conferencia_id,
            "movimentos_compensacao": movimentos_reversao,
            "registros_informativos_removidos": len(registros_informativos),
            "justificativa": justificativa_limpa,
            "instalador_id": conferencia.instalador_id,
        },
    )
    db.commit()
    if next_path:
        return RedirectResponse(url=_append_query_flag(next_safe, "reversao_conferencia=1"), status_code=302)
    return RedirectResponse(url="/manipulador/conferencia?reversao=1", status_code=302)


@router.get("/auditoria", response_class=HTMLResponse)
async def ver_auditoria(
    request: Request,
    pagina: int = 1,
    usuario_filtro: str = "",
    acao: str = "",
    severidade: str = "",
    categoria: str = "",
    resultado: str = "",
    tabela: str = "",
    data_inicio: str = "",
    data_fim: str = "",
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    query = db.query(AuditoriaLog).outerjoin(Usuario, AuditoriaLog.usuario_id == Usuario.id).options(
        joinedload(AuditoriaLog.usuario)
    )

    if usuario_filtro.strip():
        termo = f"%{usuario_filtro.strip()}%"
        query = query.filter(or_(Usuario.nome.ilike(termo), Usuario.email.ilike(termo)))

    if acao.strip():
        query = query.filter(AuditoriaLog.acao.ilike(f"%{acao.strip()}%"))

    severidade_limpa = normalize_text(severidade, upper=True)
    if severidade_limpa:
        query = query.filter(AuditoriaLog.severidade == severidade_limpa)

    categoria_limpa = normalize_text(categoria, upper=True)
    if categoria_limpa:
        query = query.filter(AuditoriaLog.categoria == categoria_limpa)

    resultado_limpo = normalize_text(resultado, upper=True)
    if resultado_limpo:
        query = query.filter(AuditoriaLog.resultado == resultado_limpo)

    if tabela.strip():
        query = query.filter(AuditoriaLog.tabela.ilike(f"%{tabela.strip()}%"))

    dt_inicio = parse_date_start(data_inicio)
    if dt_inicio:
        query = query.filter(AuditoriaLog.criado_em >= dt_inicio)

    dt_fim = parse_date_end(data_fim)
    if dt_fim:
        query = query.filter(AuditoriaLog.criado_em < dt_fim)

    query = query.order_by(AuditoriaLog.criado_em.desc(), AuditoriaLog.id.desc())

    por_pagina = 50
    offset = max(pagina - 1, 0) * por_pagina
    total = query.count()
    registros = query.offset(offset).limit(por_pagina).all()
    total_paginas = max((total + por_pagina - 1) // por_pagina, 1)

    return templates.TemplateResponse(
        "admin/auditoria.html",
        {
            "request": request,
            "usuario": usuario,
            "registros": registros,
            "pagina": pagina,
            "total_paginas": total_paginas,
            "total": total,
            "filtros": {
                "usuario": usuario_filtro,
                "acao": acao,
                "severidade": severidade_limpa,
                "categoria": categoria_limpa,
                "resultado": resultado_limpo,
                "tabela": tabela,
                "data_inicio": data_inicio,
                "data_fim": data_fim,
            },
        },
    )
