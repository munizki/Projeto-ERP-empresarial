from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.models import (
    CaixaHidrometro,
    Solicitacao,
    SolicitacaoItemCaixa,
    SolicitacaoItemPeca,
    SolicitacaoStatus,
)
from app.services.contexto_acoes import get_caixa_hidrometros
from app.utils import format_datetime, normalize_text


CAIXA_HIDROMETROS_ATTR = getattr(CaixaHidrometro, "hidrômetros")
STATUS_RECEBIMENTO_VALIDOS = {"pendente", "confirmado", "divergencia"}


def status_recebimento_instalador(solicitacao: Solicitacao) -> str:
    status = normalize_text(solicitacao.recebimento_instalador_status).lower()
    if status in STATUS_RECEBIMENTO_VALIDOS:
        return status
    if solicitacao.status == SolicitacaoStatus.ENTREGUE:
        return "pendente"
    return "-"


def query_confirmacoes_instalador(
    db: Session,
    *,
    status_recebimento: str = "",
    instalador_id: int | None = None,
    data_inicio=None,
    data_fim=None,
):
    query = db.query(Solicitacao).options(
        joinedload(Solicitacao.instalador),
        joinedload(Solicitacao.criado_por),
        joinedload(Solicitacao.entregue_por),
        joinedload(Solicitacao.usuario_confirmacao_instalador),
        joinedload(Solicitacao.itens_caixa).joinedload(SolicitacaoItemCaixa.caixa).joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca),
    ).filter(Solicitacao.status == SolicitacaoStatus.ENTREGUE)

    status_limpo = normalize_text(status_recebimento).lower()
    if status_limpo == "pendente":
        query = query.filter(
            or_(
                Solicitacao.recebimento_instalador_status == None,
                Solicitacao.recebimento_instalador_status == "",
                Solicitacao.recebimento_instalador_status == "pendente",
            )
        )
    elif status_limpo in {"confirmado", "divergencia"}:
        query = query.filter(Solicitacao.recebimento_instalador_status == status_limpo)

    if instalador_id:
        query = query.filter(Solicitacao.instalador_id == instalador_id)
    if data_inicio:
        query = query.filter(Solicitacao.entregue_em >= data_inicio)
    if data_fim:
        query = query.filter(Solicitacao.entregue_em < data_fim)

    return query.order_by(
        Solicitacao.confirmacao_instalador_em.desc(),
        Solicitacao.entregue_em.desc(),
        Solicitacao.id.desc(),
    )


def resumo_confirmacoes_instalador(solicitacoes: list[Solicitacao]) -> dict[str, int]:
    resumo = {"total": len(solicitacoes), "pendente": 0, "confirmado": 0, "divergencia": 0}
    for solicitacao in solicitacoes:
        status = status_recebimento_instalador(solicitacao)
        if status in resumo:
            resumo[status] += 1
    return resumo


def _total_hidrometros_solicitacao(solicitacao: Solicitacao) -> int:
    total = 0
    for item in solicitacao.itens_caixa:
        total += len(get_caixa_hidrometros(item.caixa)) if item.caixa else 0
    return total


def linhas_exportacao_confirmacoes_instalador(solicitacoes: list[Solicitacao]) -> list[list[object]]:
    rows: list[list[object]] = []
    for solicitacao in solicitacoes:
        pecas = ", ".join(
            f"{item.tipo_peca.nome}: {item.quantidade_solicitada}"
            for item in solicitacao.itens_peca
            if item.tipo_peca
        )
        rows.append(
            [
                solicitacao.id,
                status_recebimento_instalador(solicitacao),
                solicitacao.instalador.nome if solicitacao.instalador else "",
                solicitacao.instalador.matricula if solicitacao.instalador else "",
                format_datetime(solicitacao.criado_em, with_seconds=True),
                format_datetime(solicitacao.entregue_em, with_seconds=True) if solicitacao.entregue_em else "",
                format_datetime(solicitacao.confirmacao_instalador_em, with_seconds=True)
                if solicitacao.confirmacao_instalador_em else "",
                solicitacao.criado_por.nome if solicitacao.criado_por else "",
                solicitacao.entregue_por.nome if solicitacao.entregue_por else "",
                solicitacao.usuario_confirmacao_instalador.nome if solicitacao.usuario_confirmacao_instalador else "",
                len(solicitacao.itens_caixa),
                _total_hidrometros_solicitacao(solicitacao),
                pecas,
                solicitacao.motivo_divergencia_instalador or "",
                solicitacao.observacoes or "",
            ]
        )
    return rows
