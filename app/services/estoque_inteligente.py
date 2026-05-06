from __future__ import annotations

from datetime import timedelta
from math import ceil
from typing import Any

from sqlalchemy import distinct, func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    AuditoriaLog,
    CaixaHidrometro,
    CaixaStatus,
    EstoquePeca,
    Hidrometro,
    HidrometroStatus,
    InstalacaoHidrometro,
    Instalador,
    MovimentacaoPeca,
    MovimentacaoTipo,
    TipoPeca,
)
from app.services.auditoria import registrar_auditoria
from app.services.manutencao_hidrometros import hidrometros_disponiveis_almoxarifado, resumo_manutencao_hidrometros
from app.utils import utc_now


STATUS_ORDER = {"CRITICO": 0, "ATENCAO": 1, "SEM_HISTORICO": 2, "OK": 3}


def _inicio_periodo(dias: int):
    return utc_now() - timedelta(days=max(int(dias or 30), 1))


def _inicio_mes_atual():
    agora = utc_now()
    return agora.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _hidrometros_disponiveis(db: Session) -> int:
    return hidrometros_disponiveis_almoxarifado(db)


def _fator_pressao(hidrometros_disponiveis: int) -> str:
    if hidrometros_disponiveis >= 300:
        return "ALTO"
    if hidrometros_disponiveis >= 100:
        return "MEDIO"
    if hidrometros_disponiveis >= 1:
        return "BAIXO"
    return "SEM_PRESSAO_OPERACIONAL"


def _consumo_por_peca(db: Session, inicio) -> dict[int, dict[str, float]]:
    linhas = db.query(
        MovimentacaoPeca.tipo_peca_id,
        func.coalesce(func.sum(MovimentacaoPeca.quantidade), 0).label("total"),
        func.count(distinct(func.date(MovimentacaoPeca.criado_em))).label("dias_com_movimento"),
        func.max(MovimentacaoPeca.criado_em).label("ultima"),
    ).filter(
        MovimentacaoPeca.tipo == MovimentacaoTipo.SAIDA,
        MovimentacaoPeca.criado_em >= inicio,
    ).group_by(MovimentacaoPeca.tipo_peca_id).all()
    return {
        tipo_peca_id: {
            "total": float(total or 0),
            "dias": int(dias or 0),
            "ultima": ultima,
        }
        for tipo_peca_id, total, dias, ultima in linhas
    }


def _status_item(estoque_atual: int, minimo_qtd: float, consumo_medio: float | None, dias_restantes: float | None, hidros: int) -> str:
    if estoque_atual <= minimo_qtd:
        return "CRITICO"
    if dias_restantes is not None and (dias_restantes <= 3 or (hidros >= 300 and dias_restantes <= 7)):
        return "CRITICO"
    if estoque_atual <= minimo_qtd * 1.3:
        return "ATENCAO"
    if dias_restantes is not None and (4 <= dias_restantes <= 7 or (hidros >= 100 and dias_restantes <= 10)):
        return "ATENCAO"
    if consumo_medio is None:
        return "SEM_HISTORICO"
    return "OK"


def _previsao_ruptura(dias_restantes: float | None) -> str:
    if dias_restantes is None:
        return "Sem historico suficiente"
    dias = max(int(ceil(dias_restantes)), 0)
    return f"Pode acabar em {dias} dia(s)"


def montar_dashboard_estoque_inteligente(db: Session, *, dias_periodo: int = 30) -> dict[str, Any]:
    inicio = _inicio_periodo(dias_periodo)
    inicio_mes = _inicio_mes_atual()
    hidros_disponiveis = _hidrometros_disponiveis(db)
    fator_pressao = _fator_pressao(hidros_disponiveis)
    consumo_periodo = _consumo_por_peca(db, inicio)

    estoques = db.query(EstoquePeca).options(
        joinedload(EstoquePeca.tipo_peca),
    ).join(TipoPeca).filter(TipoPeca.ativo == True).order_by(TipoPeca.nome).all()

    itens: list[dict[str, Any]] = []
    for estoque in estoques:
        tipo = estoque.tipo_peca
        consumo = consumo_periodo.get(tipo.id, {"total": 0.0, "dias": 0, "ultima": None})
        dias_com_movimento = int(consumo["dias"])
        consumo_medio = (float(consumo["total"]) / dias_com_movimento) if dias_com_movimento > 0 else None
        dias_restantes = (estoque.quantidade_atual / consumo_medio) if consumo_medio and consumo_medio > 0 else None
        minimo_qtd = (estoque.quantidade_maxima * float(tipo.estoque_minimo_percentual or 0)) / 100
        status = _status_item(estoque.quantidade_atual, minimo_qtd, consumo_medio, dias_restantes, hidros_disponiveis)
        itens.append(
            {
                "tipo_peca_id": tipo.id,
                "estoque_id": estoque.id,
                "nome": tipo.nome,
                "descricao": tipo.descricao or "",
                "unidade": tipo.unidade_medida,
                "estoque_atual": estoque.quantidade_atual,
                "quantidade_maxima": estoque.quantidade_maxima,
                "minimo_percentual": float(tipo.estoque_minimo_percentual or 0),
                "minimo_qtd": minimo_qtd,
                "consumo_periodo": float(consumo["total"] or 0),
                "dias_com_movimento": dias_com_movimento,
                "consumo_medio_diario": consumo_medio,
                "dias_restantes": dias_restantes,
                "status": status,
                "ultima_movimentacao": consumo["ultima"] or estoque.atualizado_em,
                "previsao": _previsao_ruptura(dias_restantes),
                "percentual_atual": estoque.percentual_atual,
                "abaixo_minimo_percentual": estoque.abaixo_minimo,
            }
        )

    itens.sort(key=lambda item: (STATUS_ORDER.get(item["status"], 9), item["dias_restantes"] if item["dias_restantes"] is not None else 999999, item["estoque_atual"]))

    consumo_mes_linhas = db.query(
        MovimentacaoPeca.tipo_peca_id,
        TipoPeca.nome,
        func.coalesce(func.sum(MovimentacaoPeca.quantidade), 0).label("total"),
    ).join(TipoPeca, TipoPeca.id == MovimentacaoPeca.tipo_peca_id).filter(
        MovimentacaoPeca.tipo == MovimentacaoTipo.SAIDA,
        MovimentacaoPeca.criado_em >= inicio_mes,
    ).group_by(MovimentacaoPeca.tipo_peca_id, TipoPeca.nome).order_by(func.sum(MovimentacaoPeca.quantidade).desc()).all()
    consumo_total_mes = int(sum(total or 0 for _, _, total in consumo_mes_linhas))
    ranking_pecas = [
        {
            "tipo_peca_id": tipo_peca_id,
            "nome": nome,
            "quantidade": int(total or 0),
            "percentual": ((float(total or 0) / consumo_total_mes) * 100) if consumo_total_mes else 0,
        }
        for tipo_peca_id, nome, total in consumo_mes_linhas[:10]
    ]

    analise_instaladores = _analise_instaladores(db, inicio)
    manutencao = resumo_manutencao_hidrometros(db)
    maior_consumo = max(analise_instaladores, key=lambda item: item["pecas_recebidas"], default=None)
    candidatos_relacao = [item for item in analise_instaladores if item["hidrometros_instalados"] > 0]
    melhor_relacao = min(candidatos_relacao, key=lambda item: item["media_pecas_por_hidrometro"], default=None)

    return {
        "periodo_dias": dias_periodo,
        "hidrometros_disponiveis": hidros_disponiveis,
        "fator_pressao": fator_pressao,
        "itens": itens,
        "ranking_pecas": ranking_pecas,
        "analise_instaladores": analise_instaladores,
        "kpis": {
            "total_pecas": len(itens),
            "criticos": sum(1 for item in itens if item["status"] == "CRITICO"),
            "atencao": sum(1 for item in itens if item["status"] == "ATENCAO"),
            "sem_historico": sum(1 for item in itens if item["status"] == "SEM_HISTORICO"),
            "ok": sum(1 for item in itens if item["status"] == "OK"),
            "hidrometros_disponiveis": hidros_disponiveis,
            "consumo_total_mes": consumo_total_mes,
            "maior_consumo_instalador": maior_consumo,
            "melhor_relacao_instalador": melhor_relacao,
            "manutencao": manutencao,
        },
        "manutencao": manutencao,
    }


def _analise_instaladores(db: Session, inicio) -> list[dict[str, Any]]:
    pecas_linhas = db.query(
        MovimentacaoPeca.instalador_id,
        func.coalesce(func.sum(MovimentacaoPeca.quantidade), 0).label("total_pecas"),
    ).filter(
        MovimentacaoPeca.tipo == MovimentacaoTipo.SAIDA,
        MovimentacaoPeca.instalador_id.isnot(None),
        MovimentacaoPeca.criado_em >= inicio,
    ).group_by(MovimentacaoPeca.instalador_id).all()
    pecas_por_instalador = {instalador_id: int(total or 0) for instalador_id, total in pecas_linhas}

    instalacoes_linhas = db.query(
        InstalacaoHidrometro.instalador_id,
        func.count(InstalacaoHidrometro.id).label("total"),
    ).filter(
        InstalacaoHidrometro.data_instalacao >= inicio,
    ).group_by(InstalacaoHidrometro.instalador_id).all()
    instalacoes_por_instalador = {instalador_id: int(total or 0) for instalador_id, total in instalacoes_linhas}

    instalador_ids = sorted(set(pecas_por_instalador) | set(instalacoes_por_instalador))
    instaladores = {
        instalador.id: instalador
        for instalador in db.query(Instalador).filter(Instalador.id.in_(instalador_ids)).all()
    } if instalador_ids else {}

    total_pecas = sum(pecas_por_instalador.values())
    total_instalacoes = sum(instalacoes_por_instalador.values())
    media_geral = (total_pecas / total_instalacoes) if total_instalacoes else 0

    analise: list[dict[str, Any]] = []
    for instalador_id in instalador_ids:
        instalador = instaladores.get(instalador_id)
        pecas = pecas_por_instalador.get(instalador_id, 0)
        hidros = instalacoes_por_instalador.get(instalador_id, 0)
        media = (pecas / hidros) if hidros else float(pecas)
        desvio = ((media - media_geral) / media_geral * 100) if media_geral else 0
        status = "NORMAL"
        if media_geral and pecas >= 5 and media >= media_geral * 2:
            status = "SUSPEITO"
        elif media_geral and pecas >= 3 and media >= media_geral * 1.5:
            status = "ATENCAO"
        analise.append(
            {
                "instalador_id": instalador_id,
                "instalador": instalador.nome if instalador else f"Instalador #{instalador_id}",
                "hidrometros_instalados": hidros,
                "pecas_recebidas": pecas,
                "media_pecas_por_hidrometro": media,
                "media_geral": media_geral,
                "desvio_percentual": desvio,
                "status": status,
            }
        )
    analise.sort(key=lambda item: (item["status"] == "NORMAL", -item["pecas_recebidas"], item["instalador"]))
    return analise


def registrar_auditoria_alertas_estoque(db: Session, dashboard: dict[str, Any], usuario_id: int | None = None) -> None:
    inicio_dia = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    for item in dashboard["itens"]:
        if item["status"] != "CRITICO":
            continue
        existe = db.query(AuditoriaLog.id).filter(
            AuditoriaLog.acao == "ESTOQUE_INTELIGENTE_CRITICO",
            AuditoriaLog.tabela == "estoque_pecas",
            AuditoriaLog.registro_id == item["estoque_id"],
            AuditoriaLog.criado_em >= inicio_dia,
        ).first()
        if existe:
            continue
        registrar_auditoria(
            db=db,
            acao="ESTOQUE_INTELIGENTE_CRITICO",
            usuario_id=usuario_id,
            tabela="estoque_pecas",
            registro_id=item["estoque_id"],
            descricao=f"Estoque inteligente apontou item critico: {item['nome']}",
            dados_depois=item,
            severidade="CRITICO",
            categoria="ESTOQUE",
            resultado="ALERTA",
        )

    for item in dashboard["analise_instaladores"]:
        if item["status"] != "SUSPEITO":
            continue
        existe = db.query(AuditoriaLog.id).filter(
            AuditoriaLog.acao == "CONSUMO_INSTALADOR_SUSPEITO",
            AuditoriaLog.tabela == "instaladores",
            AuditoriaLog.registro_id == item["instalador_id"],
            AuditoriaLog.criado_em >= inicio_dia,
        ).first()
        if existe:
            continue
        registrar_auditoria(
            db=db,
            acao="CONSUMO_INSTALADOR_SUSPEITO",
            usuario_id=usuario_id,
            tabela="instaladores",
            registro_id=item["instalador_id"],
            descricao=f"Consumo de pecas fora do padrao: {item['instalador']}",
            dados_depois=item,
            severidade="SUSPEITO",
            categoria="ESTOQUE",
            resultado="ALERTA",
        )
