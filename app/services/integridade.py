from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    AuditoriaLog,
    CaixaHidrometro,
    EstoquePeca,
    Hidrometro,
    InstaladorPeca,
    MovimentacaoMaterial,
    MovimentacaoPeca,
    Solicitacao,
    SolicitacaoStatus,
    TipoPeca,
    UserRole,
    Usuario,
)
from app.services.regras_caixa import quantidade_esperada_caixa
from app.utils import utc_now


CAIXA_HIDROMETROS_ATTR = getattr(CaixaHidrometro, "hidrômetros")


def _limit(items: list[Any], limit: int = 20) -> list[Any]:
    return items[:limit]


def diagnostico_operacional(db: Session, *, pendente_horas: int = 48) -> dict[str, Any]:
    agora = utc_now()
    desde_24h = agora - timedelta(hours=24)
    limite_pendente = agora - timedelta(hours=pendente_horas)

    caixas = db.query(CaixaHidrometro).options(joinedload(CAIXA_HIDROMETROS_ATTR)).filter(
        CaixaHidrometro.ativo == True
    ).all()
    caixas_incompletas = [
        caixa for caixa in caixas
        if len(getattr(caixa, CAIXA_HIDROMETROS_ATTR.key) or []) != quantidade_esperada_caixa(caixa)
    ]

    hidrometros_sem_caixa = db.query(Hidrometro).filter(
        Hidrometro.caixa_id == None,
        Hidrometro.em_manutencao == False,
        Hidrometro.descartado_tecnico == False,
        Hidrometro.prioridade_reutilizacao == False,
        Hidrometro.retornou_manutencao == False,
    ).all()
    hidrometros_duplicados = db.query(
        Hidrometro.numero_serie,
        func.count(Hidrometro.id).label("total"),
    ).group_by(Hidrometro.numero_serie).having(func.count(Hidrometro.id) > 1).all()

    estoques_negativos = db.query(EstoquePeca).options(joinedload(EstoquePeca.tipo_peca)).filter(
        EstoquePeca.quantidade_atual < 0
    ).all()
    estoques_acima_limite = db.query(EstoquePeca).options(joinedload(EstoquePeca.tipo_peca)).filter(
        EstoquePeca.quantidade_atual > EstoquePeca.quantidade_maxima
    ).all()
    posse_negativa = db.query(InstaladorPeca).options(
        joinedload(InstaladorPeca.instalador),
        joinedload(InstaladorPeca.tipo_peca),
    ).filter(InstaladorPeca.quantidade < 0).all()

    solicitacoes_pendentes_antigas = db.query(Solicitacao).options(joinedload(Solicitacao.instalador)).filter(
        Solicitacao.status == SolicitacaoStatus.PENDENTE,
        Solicitacao.criado_em < limite_pendente,
    ).order_by(Solicitacao.criado_em.asc()).all()

    movimentacoes_material_sem_usuario = db.query(MovimentacaoMaterial).filter(
        MovimentacaoMaterial.registrado_por_id == None
    ).count()
    movimentacoes_peca_sem_usuario = db.query(MovimentacaoPeca).filter(
        MovimentacaoPeca.registrado_por_id == None
    ).count()

    admin_ativos = db.query(Usuario).filter(Usuario.role == UserRole.ADMIN, Usuario.ativo == True).count()
    usuarios_ativos = db.query(Usuario).filter(Usuario.ativo == True).count()

    erros_criticos_24h = db.query(AuditoriaLog).filter(
        AuditoriaLog.criado_em >= desde_24h,
        AuditoriaLog.severidade == "CRITICO",
    ).count()
    eventos_suspeitos_24h = db.query(AuditoriaLog).filter(
        AuditoriaLog.criado_em >= desde_24h,
        AuditoriaLog.severidade == "SUSPEITO",
    ).count()
    tentativas_bloqueadas_24h = db.query(AuditoriaLog).filter(
        AuditoriaLog.criado_em >= desde_24h,
        AuditoriaLog.acao.in_(["VALIDACAO_OPERACIONAL_BLOQUEADA", "REQUISICAO_BLOQUEADA"]),
    ).count()
    tentativas_login_invalidas_24h = db.query(AuditoriaLog).filter(
        AuditoriaLog.criado_em >= desde_24h,
        AuditoriaLog.acao.in_(["LOGIN_FALHA", "LOGIN_BLOQUEADO", "LOGIN_USUARIO_INATIVO"]),
    ).count()
    alteracoes_admin_recentes = db.query(AuditoriaLog).filter(
        AuditoriaLog.criado_em >= desde_24h,
        AuditoriaLog.acao.like("ADMIN_%"),
    ).order_by(AuditoriaLog.criado_em.desc(), AuditoriaLog.id.desc()).limit(15).all()
    eventos_suspeitos_recentes = db.query(AuditoriaLog).filter(
        AuditoriaLog.criado_em >= desde_24h,
        AuditoriaLog.severidade.in_(["SUSPEITO", "CRITICO"]),
    ).order_by(AuditoriaLog.criado_em.desc(), AuditoriaLog.id.desc()).limit(20).all()

    bloqueadores_criticos: list[str] = []
    if admin_ativos != 1:
        bloqueadores_criticos.append("Quantidade de administradores ativos diferente de 1.")
    if estoques_negativos:
        bloqueadores_criticos.append("Existe estoque de peca com saldo negativo.")
    if posse_negativa:
        bloqueadores_criticos.append("Existe saldo negativo de peca com instalador.")
    if hidrometros_sem_caixa:
        bloqueadores_criticos.append("Existe hidrometro sem caixa vinculada.")
    if hidrometros_duplicados:
        bloqueadores_criticos.append("Existe numero de hidrometro duplicado.")
    if movimentacoes_material_sem_usuario or movimentacoes_peca_sem_usuario:
        bloqueadores_criticos.append("Existe movimentacao operacional sem usuario responsavel.")

    return {
        "gerado_em": agora,
        "caixas_incompletas": caixas_incompletas,
        "hidrometros_sem_caixa": hidrometros_sem_caixa,
        "hidrometros_duplicados": hidrometros_duplicados,
        "estoques_negativos": estoques_negativos,
        "estoques_acima_limite": estoques_acima_limite,
        "posse_negativa": posse_negativa,
        "solicitacoes_pendentes_antigas": solicitacoes_pendentes_antigas,
        "movimentacoes_sem_usuario": movimentacoes_material_sem_usuario + movimentacoes_peca_sem_usuario,
        "alteracoes_admin_recentes": alteracoes_admin_recentes,
        "eventos_suspeitos_recentes": eventos_suspeitos_recentes,
        "erros_criticos_24h": erros_criticos_24h,
        "eventos_suspeitos_24h": eventos_suspeitos_24h,
        "tentativas_bloqueadas_24h": tentativas_bloqueadas_24h,
        "tentativas_login_invalidas_24h": tentativas_login_invalidas_24h,
        "usuarios_ativos": usuarios_ativos,
        "admin_ativos": admin_ativos,
        "bloqueadores_criticos": bloqueadores_criticos,
        "resumo": {
            "caixas_incompletas": len(caixas_incompletas),
            "hidrometros_sem_caixa": len(hidrometros_sem_caixa),
            "hidrometros_duplicados": len(hidrometros_duplicados),
            "estoques_negativos": len(estoques_negativos),
            "estoques_acima_limite": len(estoques_acima_limite),
            "posse_negativa": len(posse_negativa),
            "solicitacoes_pendentes_antigas": len(solicitacoes_pendentes_antigas),
            "movimentacoes_sem_usuario": movimentacoes_material_sem_usuario + movimentacoes_peca_sem_usuario,
            "alteracoes_admin_24h": len(alteracoes_admin_recentes),
            "eventos_suspeitos_24h": eventos_suspeitos_24h,
            "tentativas_bloqueadas_24h": tentativas_bloqueadas_24h,
        },
        "amostras": {
            "caixas_incompletas": _limit(caixas_incompletas),
            "hidrometros_sem_caixa": _limit(hidrometros_sem_caixa),
            "estoques_negativos": _limit(estoques_negativos),
            "solicitacoes_pendentes_antigas": _limit(solicitacoes_pendentes_antigas),
            "eventos_suspeitos_recentes": _limit(eventos_suspeitos_recentes),
        },
    }
