from __future__ import annotations

from app.models import (
    CaixaStatus,
    Hidrometro,
    HidrometroStatus,
    InstalacaoHidrometro,
    MovimentacaoMaterial,
    MovimentacaoTipo,
)
from app.utils import normalize_text, utc_now


def mensagem_baixa_hidrometro(hidrometro: Hidrometro | None, *, override: bool = False) -> str:
    if not hidrometro:
        return "Hidrometro nao encontrado."

    if getattr(hidrometro, "descartado_tecnico", False) or hidrometro.status == HidrometroStatus.DESCARTADO_TECNICO:
        return "Este hidrometro foi descartado tecnicamente e nunca pode ser baixado."

    if (
        getattr(hidrometro, "em_manutencao", False)
        or getattr(hidrometro, "bloqueado_por_manutencao", False)
        or hidrometro.status in {
            HidrometroStatus.EM_MANUTENCAO,
            HidrometroStatus.ENVIADO_ASSISTENCIA,
            HidrometroStatus.RETORNADO_MANUTENCAO,
        }
    ):
        return "Este hidrometro esta bloqueado por manutencao e nao pode ser baixado."

    if hidrometro.status == HidrometroStatus.INSTALADO:
        return "Este hidrometro ja foi instalado anteriormente. Consulte o historico para auditoria."

    if hidrometro.status == HidrometroStatus.EM_ESTOQUE and not override:
        return "Este hidrometro nao pode ser instalado pois ainda esta em estoque. Envie a caixa para um instalador antes."

    if hidrometro.status != HidrometroStatus.COM_INSTALADOR and not override:
        return "Este hidrometro nao esta em contexto valido de instalacao. Revise a caixa e o instalador antes de registrar a baixa."

    if hidrometro.instalador_id is None and not override:
        return "Este hidrometro esta sem instalador vinculado. Corrija o contexto antes de registrar a baixa."

    if (
        hidrometro.caixa is not None
        and hidrometro.caixa.status != CaixaStatus.ENTREGUE
        and not override
    ):
        return "Este hidrometro nao pertence a uma caixa entregue ao instalador. A baixa so pode ser feita apos a entrega."

    return ""


def mensagem_reversao_baixa_hidrometro(hidrometro: Hidrometro | None) -> str:
    if not hidrometro:
        return "Hidrometro nao encontrado."
    if hidrometro.status != HidrometroStatus.INSTALADO:
        return "A reversao administrativa so esta disponivel para hidrometros que ja estao instalados."
    return ""


def buscar_movimentacao_baixa(db, hidrometro: Hidrometro) -> MovimentacaoMaterial | None:
    if not hidrometro.caixa_id:
        return None

    query = db.query(MovimentacaoMaterial).filter(
        MovimentacaoMaterial.tipo == MovimentacaoTipo.BAIXA,
        MovimentacaoMaterial.caixa_id == hidrometro.caixa_id,
    )

    movimento = query.filter(MovimentacaoMaterial.hidrometro_id == hidrometro.id).order_by(
        MovimentacaoMaterial.id.desc()
    ).first()
    if movimento:
        return movimento

    if hidrometro.instalado_em is None:
        return None

    candidates = query.filter(MovimentacaoMaterial.criado_em == hidrometro.instalado_em).order_by(
        MovimentacaoMaterial.id.desc()
    ).all()
    if not candidates:
        return None

    for candidate in candidates:
        if (
            hidrometro.baixado_por_id
            and hidrometro.instalador_baixa_id
            and candidate.registrado_por_id == hidrometro.baixado_por_id
            and candidate.instalador_id == hidrometro.instalador_baixa_id
        ):
            return candidate
    for candidate in candidates:
        if hidrometro.baixado_por_id and candidate.registrado_por_id == hidrometro.baixado_por_id:
            return candidate
    for candidate in candidates:
        if hidrometro.instalador_baixa_id and candidate.instalador_id == hidrometro.instalador_baixa_id:
            return candidate
    return candidates[0]


def resolver_instalador_retorno(hidrometro: Hidrometro, movimento: MovimentacaoMaterial | None = None) -> int | None:
    if hidrometro.instalador_baixa_id:
        return hidrometro.instalador_baixa_id
    if movimento and movimento.instalador_id:
        return movimento.instalador_id
    if hidrometro.caixa and hidrometro.caixa.instalador_id:
        return hidrometro.caixa.instalador_id
    return None


def resolver_contexto_retorno_baixa(
    hidrometro: Hidrometro,
    movimento: MovimentacaoMaterial | None = None,
) -> dict[str, object]:
    instalador_id = resolver_instalador_retorno(hidrometro, movimento)
    if instalador_id:
        return {
            "status": HidrometroStatus.COM_INSTALADOR,
            "instalador_id": instalador_id,
            "source": "instalador",
        }
    return {
        "status": HidrometroStatus.EM_ESTOQUE,
        "instalador_id": None,
        "source": "estoque",
    }


def _solicitacao_entrega_id(db, hidrometro: Hidrometro, instalador_id: int | None) -> int | None:
    if not hidrometro.caixa_id:
        return None
    query = db.query(MovimentacaoMaterial.solicitacao_id).filter(
        MovimentacaoMaterial.tipo == MovimentacaoTipo.SAIDA,
        MovimentacaoMaterial.caixa_id == hidrometro.caixa_id,
        MovimentacaoMaterial.solicitacao_id.isnot(None),
    )
    if instalador_id:
        query = query.filter(MovimentacaoMaterial.instalador_id == instalador_id)
    return query.order_by(MovimentacaoMaterial.criado_em.desc(), MovimentacaoMaterial.id.desc()).scalar()


def _finalizar_caixa_se_completa(db, hidrometro: Hidrometro, momento) -> bool:
    caixa = hidrometro.caixa
    if not caixa or not hidrometro.caixa_id:
        return False
    pendentes = db.query(Hidrometro).filter(
        Hidrometro.caixa_id == hidrometro.caixa_id,
        Hidrometro.status != HidrometroStatus.INSTALADO,
    ).count()
    if pendentes != 0:
        return False
    caixa.status = CaixaStatus.INSTALADA
    caixa.instalador_id = None
    caixa.atualizado_em = momento
    return True


def aplicar_baixa_hidrometro(
    db,
    hidrometro: Hidrometro,
    usuario_id: int,
    *,
    observacoes: str = "",
    override: bool = False,
    justificativa_override: str = "",
) -> dict[str, object]:
    db.flush()
    locked = db.query(Hidrometro).filter(Hidrometro.id == hidrometro.id).populate_existing().with_for_update().first()
    if locked is not None:
        hidrometro = locked

    mensagem = mensagem_baixa_hidrometro(hidrometro, override=override)
    if mensagem:
        raise ValueError(mensagem)

    momento = utc_now()
    instalador_id = hidrometro.instalador_id
    solicitacao_id = _solicitacao_entrega_id(db, hidrometro, instalador_id)

    hidrometro.status = HidrometroStatus.INSTALADO
    hidrometro.instalado_em = momento
    hidrometro.atualizado_em = momento
    hidrometro.baixado_por_id = usuario_id
    hidrometro.instalador_baixa_id = instalador_id
    hidrometro.instalador_id = None

    observacoes_limpas = normalize_text(observacoes)
    justificativa_limpa = normalize_text(justificativa_override)

    descricao_movimentacao = observacoes_limpas or f"Baixa do hidrometro {hidrometro.numero_serie}"
    if override:
        descricao_movimentacao = observacoes_limpas or f"Baixa administrativa do hidrometro {hidrometro.numero_serie}"
        if justificativa_limpa:
            descricao_movimentacao = f"{descricao_movimentacao} | motivo: {justificativa_limpa}"

    movimento = MovimentacaoMaterial(
        tipo=MovimentacaoTipo.BAIXA,
        caixa_id=hidrometro.caixa_id,
        hidrometro_id=hidrometro.id,
        instalador_id=instalador_id,
        solicitacao_id=solicitacao_id,
        observacoes=descricao_movimentacao,
        registrado_por_id=usuario_id,
        criado_em=momento,
    )
    db.add(movimento)
    db.flush()

    if db.query(InstalacaoHidrometro).filter(
        InstalacaoHidrometro.hidrometro_id == hidrometro.id,
    ).first():
        raise ValueError("Este hidrometro ja possui evento de instalacao registrado.")

    if instalador_id is not None:
        db.add(
            InstalacaoHidrometro(
                instalador_id=instalador_id,
                hidrometro_id=hidrometro.id,
                caixa_id=hidrometro.caixa_id,
                solicitacao_id=solicitacao_id,
                data_instalacao=momento,
                usuario_registro_id=usuario_id,
                criado_em=momento,
            )
        )

    caixa_finalizada = _finalizar_caixa_se_completa(db, hidrometro, momento)

    return {
        "momento": momento,
        "instalador_id": instalador_id,
        "solicitacao_id": solicitacao_id,
        "observacoes": observacoes_limpas or None,
        "override": override,
        "justificativa_override": justificativa_limpa or None,
        "movimentacao_id": movimento.id,
        "caixa_finalizada": caixa_finalizada,
    }


def reverter_baixa_hidrometro(db, hidrometro: Hidrometro) -> dict[str, object]:
    db.flush()
    locked = db.query(Hidrometro).filter(Hidrometro.id == hidrometro.id).populate_existing().with_for_update().first()
    if locked is not None:
        hidrometro = locked

    mensagem = mensagem_reversao_baixa_hidrometro(hidrometro)
    if mensagem:
        raise ValueError(mensagem)

    movimento = buscar_movimentacao_baixa(db, hidrometro)
    contexto = resolver_contexto_retorno_baixa(hidrometro, movimento)
    momento = utc_now()
    movimento_id = movimento.id if movimento else None
    operador_id = hidrometro.baixado_por_id
    instalador_baixa_id = hidrometro.instalador_baixa_id
    instalado_em = hidrometro.instalado_em

    hidrometro.status = contexto["status"]
    hidrometro.instalado_em = None
    hidrometro.instalador_id = contexto["instalador_id"]
    hidrometro.baixado_por_id = None
    hidrometro.instalador_baixa_id = None
    hidrometro.atualizado_em = momento

    if hidrometro.caixa:
        if contexto["status"] == HidrometroStatus.COM_INSTALADOR:
            hidrometro.caixa.status = CaixaStatus.ENTREGUE
            hidrometro.caixa.instalador_id = contexto["instalador_id"]
            hidrometro.caixa.atualizado_em = momento
        else:
            outros_fora_estoque = db.query(Hidrometro).filter(
                Hidrometro.caixa_id == hidrometro.caixa_id,
                Hidrometro.id != hidrometro.id,
                Hidrometro.status.in_([HidrometroStatus.COM_INSTALADOR, HidrometroStatus.INSTALADO]),
            ).count()
            if outros_fora_estoque == 0:
                hidrometro.caixa.status = CaixaStatus.EM_ESTOQUE
                hidrometro.caixa.instalador_id = None
                hidrometro.caixa.atualizado_em = momento

    if movimento is not None:
        db.delete(movimento)
    instalacao = db.query(InstalacaoHidrometro).filter(
        InstalacaoHidrometro.hidrometro_id == hidrometro.id,
    ).first()
    if instalacao is not None:
        db.delete(instalacao)

    return {
        "momento": momento,
        "movimentacao_id": movimento_id,
        "instalador_id_restaurado": contexto["instalador_id"],
        "status_restaurado": contexto["status"].value,
        "contexto_retorno": contexto["source"],
        "operador_id_original": operador_id,
        "instalador_id_original": instalador_baixa_id,
        "instalado_em_original": instalado_em,
    }
