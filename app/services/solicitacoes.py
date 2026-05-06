from __future__ import annotations

from app.models import Solicitacao, SolicitacaoStatus
from app.utils import normalize_text


def cancelar_solicitacao(
    solicitacao: Solicitacao,
    *,
    motivo: str = "",
) -> dict[str, object]:
    if solicitacao.status == SolicitacaoStatus.CANCELADA:
        raise ValueError("Esta solicitacao ja foi cancelada.")

    if solicitacao.status == SolicitacaoStatus.ENTREGUE:
        raise ValueError("Solicitacoes entregues nao podem ser canceladas. Consulte o historico em vez de excluir.")

    caixas_liberadas: list[dict[str, object]] = []
    if solicitacao.status == SolicitacaoStatus.SEPARADA:
        for item in solicitacao.itens_caixa:
            if item.caixa is not None:
                caixas_liberadas.append(
                    {
                        "caixa_id": item.caixa.id,
                        "numero_interno": item.caixa.numero_interno,
                    }
                )
            item.caixa = None
            item.caixa_id = None

    solicitacao.status = SolicitacaoStatus.CANCELADA

    return {
        "motivo": normalize_text(motivo) or None,
        "caixas_liberadas": caixas_liberadas,
    }
