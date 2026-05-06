from __future__ import annotations

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    CaixaHidrometro,
    CaixaStatus,
    CarcacaMovimentacao,
    CarcacaTipoMovimento,
    EstoquePeca,
    Hidrometro,
    HidrometroStatus,
    MovimentacaoMaterial,
    MovimentacaoPeca,
    MovimentacaoTipo,
    TipoPeca,
    TransferenciaEmpresa,
    TransferenciaEmpresaCaixa,
    TransferenciaEmpresaPeca,
)
from app.services.contexto_acoes import get_caixa_hidrometros
from app.services.manutencao_hidrometros import caixa_incompleta_por_manutencao
from app.services.regras_caixa import quantidade_esperada_caixa
from app.utils import normalize_text, utc_now


def saldo_carcacas(db: Session) -> int:
    entradas = db.query(func.coalesce(func.sum(CarcacaMovimentacao.quantidade), 0)).filter(
        CarcacaMovimentacao.tipo_movimento.in_(
            [CarcacaTipoMovimento.DEVOLUCAO_INSTALADOR, CarcacaTipoMovimento.AJUSTE_ADMIN]
        )
    ).scalar() or 0
    saidas = db.query(func.coalesce(func.sum(CarcacaMovimentacao.quantidade), 0)).filter(
        CarcacaMovimentacao.tipo_movimento == CarcacaTipoMovimento.RECOLHIMENTO_SABESP
    ).scalar() or 0
    return int(entradas) - int(saidas)


def resumo_carcacas(db: Session) -> dict[str, int]:
    total_devolvido = db.query(func.coalesce(func.sum(CarcacaMovimentacao.quantidade), 0)).filter(
        CarcacaMovimentacao.tipo_movimento == CarcacaTipoMovimento.DEVOLUCAO_INSTALADOR
    ).scalar() or 0
    total_recolhido = db.query(func.coalesce(func.sum(CarcacaMovimentacao.quantidade), 0)).filter(
        CarcacaMovimentacao.tipo_movimento == CarcacaTipoMovimento.RECOLHIMENTO_SABESP
    ).scalar() or 0
    total_ajustes = db.query(func.coalesce(func.sum(CarcacaMovimentacao.quantidade), 0)).filter(
        CarcacaMovimentacao.tipo_movimento == CarcacaTipoMovimento.AJUSTE_ADMIN
    ).scalar() or 0
    return {
        "saldo": int(total_devolvido) + int(total_ajustes) - int(total_recolhido),
        "devolvido": int(total_devolvido),
        "recolhido": int(total_recolhido),
        "ajustes": int(total_ajustes),
    }


def registrar_devolucao_carcacas(
    db: Session,
    *,
    usuario_id: int,
    instalador_id: int,
    quantidade: int,
    data_movimento: datetime,
    documento_referencia: str = "",
    observacao: str = "",
) -> CarcacaMovimentacao:
    if quantidade <= 0:
        raise ValueError("A quantidade de carcacas precisa ser maior que zero.")
    if not instalador_id:
        raise ValueError("Selecione o instalador que devolveu as carcacas.")

    movimento = CarcacaMovimentacao(
        tipo_movimento=CarcacaTipoMovimento.DEVOLUCAO_INSTALADOR,
        instalador_id=instalador_id,
        quantidade=quantidade,
        data_movimento=data_movimento,
        documento_referencia=normalize_text(documento_referencia) or None,
        observacao=normalize_text(observacao) or None,
        usuario_id=usuario_id,
        criado_em=utc_now(),
    )
    db.add(movimento)
    db.flush()
    return movimento


def registrar_recolhimento_carcacas(
    db: Session,
    *,
    usuario_id: int,
    quantidade: int,
    data_movimento: datetime,
    documento_referencia: str = "",
    observacao: str = "",
) -> CarcacaMovimentacao:
    if quantidade <= 0:
        raise ValueError("A quantidade recolhida precisa ser maior que zero.")

    saldo_atual = saldo_carcacas(db)
    if quantidade > saldo_atual:
        raise ValueError(f"Saida bloqueada: saldo atual de carcacas e {saldo_atual}.")

    movimento = CarcacaMovimentacao(
        tipo_movimento=CarcacaTipoMovimento.RECOLHIMENTO_SABESP,
        instalador_id=None,
        quantidade=quantidade,
        data_movimento=data_movimento,
        documento_referencia=normalize_text(documento_referencia) or None,
        observacao=normalize_text(observacao) or None,
        usuario_id=usuario_id,
        criado_em=utc_now(),
    )
    db.add(movimento)
    db.flush()
    return movimento


def _validar_caixa_transferivel(db: Session, caixa_id: int) -> CaixaHidrometro:
    caixa = db.query(CaixaHidrometro).options(
        joinedload(CaixaHidrometro.transferencias_empresa),
    ).filter(
        CaixaHidrometro.id == caixa_id
    ).with_for_update(of=CaixaHidrometro).first()

    if not caixa:
        raise ValueError("Uma das caixas selecionadas nao foi localizada.")
    if not caixa.ativo:
        raise ValueError(f"A caixa {caixa.numero_interno} esta inativa.")
    if caixa.status != CaixaStatus.EM_ESTOQUE:
        raise ValueError(f"A caixa {caixa.numero_interno} precisa estar em estoque para transferencia.")
    if caixa.transferencias_empresa:
        raise ValueError(f"A caixa {caixa.numero_interno} ja foi transferida anteriormente.")

    hidrometros = get_caixa_hidrometros(caixa)
    esperado = quantidade_esperada_caixa(caixa)
    if len(hidrometros) != esperado and not caixa_incompleta_por_manutencao(db, caixa):
        raise ValueError(f"A caixa {caixa.numero_interno} esta incompleta: {len(hidrometros)}/{esperado}.")
    if any(h.status != HidrometroStatus.EM_ESTOQUE for h in hidrometros):
        raise ValueError(f"A caixa {caixa.numero_interno} possui hidrometro fora do estoque.")
    return caixa


def registrar_transferencia_empresa(
    db: Session,
    *,
    usuario_id: int,
    empresa_destino: str,
    data_transferencia: datetime,
    responsavel: str,
    caixa_ids: list[int],
    pecas_quantidades: dict[int, int],
    documento_referencia: str = "",
    observacao: str = "",
) -> TransferenciaEmpresa:
    empresa_limpa = normalize_text(empresa_destino)
    responsavel_limpo = normalize_text(responsavel)
    documento_limpo = normalize_text(documento_referencia)
    observacao_limpa = normalize_text(observacao)
    caixa_ids_unicos = list(dict.fromkeys([int(cid) for cid in caixa_ids if int(cid) > 0]))
    pecas_validas = {int(pid): int(qtd) for pid, qtd in pecas_quantidades.items() if int(qtd) > 0}

    if not empresa_limpa:
        raise ValueError("Informe a empresa destino.")
    if not responsavel_limpo:
        raise ValueError("Informe o responsavel pela transferencia.")
    if not caixa_ids_unicos and not pecas_validas:
        raise ValueError("Selecione ao menos uma caixa ou uma peca para transferir.")

    caixas = [_validar_caixa_transferivel(db, caixa_id) for caixa_id in caixa_ids_unicos]

    estoques: dict[int, EstoquePeca] = {}
    for tipo_peca_id, quantidade in pecas_validas.items():
        tipo = db.query(TipoPeca).filter(TipoPeca.id == tipo_peca_id, TipoPeca.ativo == True).first()
        if not tipo:
            raise ValueError("Uma das pecas selecionadas nao esta ativa.")
        estoque = db.query(EstoquePeca).filter(
            EstoquePeca.tipo_peca_id == tipo_peca_id
        ).with_for_update().first()
        disponivel = estoque.quantidade_atual if estoque else 0
        if quantidade > disponivel:
            raise ValueError(f"Estoque insuficiente para {tipo.nome}: disponivel {disponivel}.")
        estoques[tipo_peca_id] = estoque

    momento = utc_now()
    transferencia = TransferenciaEmpresa(
        empresa_destino=empresa_limpa,
        data_transferencia=data_transferencia,
        responsavel=responsavel_limpo,
        documento_referencia=documento_limpo or None,
        observacao=observacao_limpa or None,
        usuario_id=usuario_id,
        criado_em=momento,
    )
    db.add(transferencia)
    db.flush()

    descricao = observacao_limpa or f"Transferencia para {empresa_limpa}"
    if documento_limpo:
        descricao = f"{descricao} | documento: {documento_limpo}"

    for caixa in caixas:
        caixa.status = CaixaStatus.TRANSFERIDA_OUTRA_EMPRESA
        caixa.instalador_id = None
        caixa.atualizado_em = momento
        db.add(TransferenciaEmpresaCaixa(transferencia_id=transferencia.id, caixa_id=caixa.id))
        db.add(
            MovimentacaoMaterial(
                tipo=MovimentacaoTipo.SAIDA,
                caixa_id=caixa.id,
                instalador_id=None,
                solicitacao_id=None,
                observacoes=descricao,
                registrado_por_id=usuario_id,
                criado_em=momento,
            )
        )

    for tipo_peca_id, quantidade in pecas_validas.items():
        estoque = estoques[tipo_peca_id]
        estoque.quantidade_atual -= quantidade
        estoque.atualizado_em = momento
        db.add(TransferenciaEmpresaPeca(
            transferencia_id=transferencia.id,
            tipo_peca_id=tipo_peca_id,
            quantidade=quantidade,
        ))
        db.add(
            MovimentacaoPeca(
                tipo=MovimentacaoTipo.SAIDA,
                tipo_peca_id=tipo_peca_id,
                quantidade=quantidade,
                instalador_id=None,
                solicitacao_id=None,
                observacoes=descricao,
                registrado_por_id=usuario_id,
                criado_em=momento,
            )
        )

    db.flush()
    return transferencia
