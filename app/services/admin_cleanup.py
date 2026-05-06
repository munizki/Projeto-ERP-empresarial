from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    CaixaHidrometro,
    CaixaStatus,
    ConferenciaPecas,
    HidrometroStatus,
    Instalador,
    InstaladorPeca,
    MovimentacaoMaterial,
    MovimentacaoPeca,
    Solicitacao,
    SolicitacaoItemCaixa,
    SolicitacaoItemPeca,
    SolicitacaoStatus,
)
from app.services.contexto_acoes import get_caixa_hidrometros, get_instalador_hidrometros
from app.utils import utc_now


class CleanupBlockedError(ValueError):
    """Raised when a destructive cleanup would leave the system inconsistent."""


def build_caixa_cleanup_preview(db: Session, caixa: CaixaHidrometro) -> dict[str, Any]:
    hidrometros = get_caixa_hidrometros(caixa)
    referencias = db.query(SolicitacaoItemCaixa).join(Solicitacao).filter(
        SolicitacaoItemCaixa.caixa_id == caixa.id,
    ).all()

    blockers: list[str] = []
    warnings: list[str] = []
    impact_items = [
        f"{len(hidrometros)} hidrômetro(s) desta caixa serão apagados junto com o cadastro.",
    ]

    if caixa.movimentacoes:
        blockers.append("A caixa já possui movimentações registradas e deve ser preservada para histórico.")
    if referencias:
        blockers.append("A caixa ainda aparece em solicitações e não pode ser apagada antes da limpeza dessas solicitações.")
    if caixa.instalador_id:
        blockers.append("A caixa ainda está vinculada a um instalador.")
    if any(h.status != HidrometroStatus.EM_ESTOQUE for h in hidrometros):
        blockers.append("Há hidrômetros fora do estoque nesta caixa. Corrija o fluxo operacional antes de excluir.")

    if not caixa.ativo:
        warnings.append("A caixa já está arquivada. A exclusão agora removerá o cadastro definitivamente.")

    return {
        "entity_kind": "Caixa",
        "entity_title": caixa.numero_interno,
        "entity_subtitle": caixa.serial_number,
        "confirm_value": caixa.numero_interno,
        "details": [
            ("Status", getattr(caixa.status, "value", str(caixa.status))),
            ("Ativa", "Sim" if caixa.ativo else "Não"),
            ("Hidrômetros", str(len(hidrometros))),
            ("Movimentações", str(len(caixa.movimentacoes or []))),
        ],
        "impact_items": impact_items,
        "warnings": warnings,
        "blockers": blockers,
        "allowed": not blockers,
    }


def execute_caixa_cleanup(db: Session, caixa: CaixaHidrometro) -> dict[str, Any]:
    preview = build_caixa_cleanup_preview(db, caixa)
    if not preview["allowed"]:
        raise CleanupBlockedError("Esta caixa não pode ser excluída definitivamente no estado atual.")

    hidrometros = list(get_caixa_hidrometros(caixa))
    for hidrometro in hidrometros:
        db.delete(hidrometro)
    db.delete(caixa)

    return {
        "kind": "caixa",
        "caixa_id": caixa.id,
        "numero_interno": caixa.numero_interno,
        "serial_number": caixa.serial_number,
        "hidrometros_removidos": len(hidrometros),
    }


def _extra_box_history_for_solicitacao(db: Session, solicitacao: Solicitacao) -> bool:
    caixa_ids = [item.caixa_id for item in solicitacao.itens_caixa if item.caixa_id]
    if not caixa_ids:
        return False

    return db.query(MovimentacaoMaterial).filter(
        MovimentacaoMaterial.caixa_id.in_(caixa_ids),
        MovimentacaoMaterial.solicitacao_id != solicitacao.id,
    ).first() is not None


def build_solicitacao_cleanup_preview(db: Session, solicitacao: Solicitacao) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    impact_items = [
        f"{len(solicitacao.itens_caixa)} item(ns) de caixa serão removidos.",
        f"{len(solicitacao.itens_peca)} item(ns) de peça serão removidos.",
    ]

    movimento_material = db.query(MovimentacaoMaterial).filter(
        MovimentacaoMaterial.solicitacao_id == solicitacao.id,
    ).all()
    movimento_peca = db.query(MovimentacaoPeca).filter(
        MovimentacaoPeca.solicitacao_id == solicitacao.id,
    ).all()

    if solicitacao.status == SolicitacaoStatus.ENTREGUE:
        warnings.append("A entrega será revertida antes da exclusão, devolvendo caixas e peças para o estoque.")

        if _extra_box_history_for_solicitacao(db, solicitacao):
            blockers.append("As caixas desta solicitação já tiveram outras movimentações e não podem mais ser apagadas com segurança.")

        for item in solicitacao.itens_caixa:
            caixa = item.caixa
            if caixa is None:
                blockers.append("Há item de caixa sem vínculo físico. Revise a solicitação antes da exclusão.")
                continue

            hidrometros = get_caixa_hidrometros(caixa)
            if any(h.status == HidrometroStatus.INSTALADO for h in hidrometros):
                blockers.append(f"A caixa {caixa.numero_interno} já possui hidrômetro instalado em campo.")
            elif any(h.status != HidrometroStatus.COM_INSTALADOR for h in hidrometros):
                blockers.append(f"A caixa {caixa.numero_interno} já não está mais integralmente com o instalador.")

        for item in solicitacao.itens_peca:
            posse = db.query(InstaladorPeca).filter(
                InstaladorPeca.instalador_id == solicitacao.instalador_id,
                InstaladorPeca.tipo_peca_id == item.tipo_peca_id,
            ).first()
            quantidade_posse = posse.quantidade if posse else 0
            if quantidade_posse < item.quantidade_solicitada:
                nome_peca = item.tipo_peca.nome if item.tipo_peca else f"ID {item.tipo_peca_id}"
                blockers.append(
                    f"O instalador já não possui saldo suficiente da peça {nome_peca} para reverter esta solicitação."
                )

        if not movimento_material and solicitacao.itens_caixa:
            blockers.append("Não foram encontrados os movimentos de saída das caixas desta solicitação.")

    elif solicitacao.status == SolicitacaoStatus.SEPARADA:
        warnings.append("As reservas das caixas serão liberadas ao apagar a solicitação.")
    elif solicitacao.status == SolicitacaoStatus.CANCELADA:
        warnings.append("A solicitação já está cancelada. A exclusão agora removerá o registro definitivamente.")

    return {
        "entity_kind": "Solicitação",
        "entity_title": f"#{solicitacao.id}",
        "entity_subtitle": solicitacao.instalador.nome if solicitacao.instalador else "Sem instalador",
        "confirm_value": str(solicitacao.id),
        "details": [
            ("Status", getattr(solicitacao.status, "value", str(solicitacao.status))),
            ("Caixas", str(len(solicitacao.itens_caixa))),
            ("Peças", str(len(solicitacao.itens_peca))),
            ("Mov. material", str(len(movimento_material))),
            ("Mov. peças", str(len(movimento_peca))),
        ],
        "impact_items": impact_items,
        "warnings": warnings,
        "blockers": blockers,
        "allowed": not blockers,
    }


def execute_solicitacao_cleanup(db: Session, solicitacao: Solicitacao) -> dict[str, Any]:
    preview = build_solicitacao_cleanup_preview(db, solicitacao)
    if not preview["allowed"]:
        raise CleanupBlockedError("Esta solicitação não pode ser excluída definitivamente no estado atual.")

    movimento_material = db.query(MovimentacaoMaterial).filter(
        MovimentacaoMaterial.solicitacao_id == solicitacao.id,
    ).all()
    movimento_peca = db.query(MovimentacaoPeca).filter(
        MovimentacaoPeca.solicitacao_id == solicitacao.id,
    ).all()

    momento = utc_now()
    if solicitacao.status == SolicitacaoStatus.ENTREGUE:
        for item in solicitacao.itens_caixa:
            caixa = item.caixa
            if caixa is None:
                continue
            caixa.status = CaixaStatus.EM_ESTOQUE
            caixa.instalador_id = None
            caixa.atualizado_em = momento

            for hidrometro in get_caixa_hidrometros(caixa):
                hidrometro.status = HidrometroStatus.EM_ESTOQUE
                hidrometro.instalador_id = None
                hidrometro.instalado_em = None
                hidrometro.atualizado_em = momento

        for item in solicitacao.itens_peca:
            estoque = item.tipo_peca.estoque if item.tipo_peca else None
            if estoque is not None:
                estoque.quantidade_atual += item.quantidade_solicitada
                estoque.atualizado_em = momento

            posse = db.query(InstaladorPeca).filter(
                InstaladorPeca.instalador_id == solicitacao.instalador_id,
                InstaladorPeca.tipo_peca_id == item.tipo_peca_id,
            ).first()
            if posse is not None:
                posse.quantidade -= item.quantidade_solicitada
                posse.atualizado_em = momento
                if posse.quantidade <= 0:
                    db.delete(posse)

    for movimento in movimento_material:
        db.delete(movimento)
    for movimento in movimento_peca:
        db.delete(movimento)

    caixas_removidas = len(solicitacao.itens_caixa)
    pecas_removidas = len(solicitacao.itens_peca)
    status_original = solicitacao.status.value

    for item in list(solicitacao.itens_caixa):
        db.delete(item)
    for item in list(solicitacao.itens_peca):
        db.delete(item)

    db.delete(solicitacao)

    return {
        "kind": "solicitacao",
        "solicitacao_id": solicitacao.id,
        "status_original": status_original,
        "caixas_removidas": caixas_removidas,
        "pecas_removidas": pecas_removidas,
        "movimentos_materiais_removidos": len(movimento_material),
        "movimentos_pecas_removidos": len(movimento_peca),
    }


def build_instalador_cleanup_preview(db: Session, instalador: Instalador) -> dict[str, Any]:
    hidrometros = get_instalador_hidrometros(instalador)
    caixas_vinculadas = db.query(CaixaHidrometro).filter(CaixaHidrometro.instalador_id == instalador.id).count()
    solicitacoes_count = db.query(Solicitacao).filter(Solicitacao.instalador_id == instalador.id).count()
    mov_material_count = db.query(MovimentacaoMaterial).filter(MovimentacaoMaterial.instalador_id == instalador.id).count()
    mov_peca_count = db.query(MovimentacaoPeca).filter(MovimentacaoPeca.instalador_id == instalador.id).count()
    conferencias_count = db.query(ConferenciaPecas).filter(ConferenciaPecas.instalador_id == instalador.id).count()
    posse_linhas = db.query(InstaladorPeca).filter(InstaladorPeca.instalador_id == instalador.id).all()
    posse_com_saldo = [item for item in posse_linhas if item.quantidade > 0]

    blockers: list[str] = []
    warnings: list[str] = []

    if hidrometros:
        blockers.append("O instalador ainda possui hidrômetros vinculados.")
    if caixas_vinculadas:
        blockers.append("O instalador ainda possui caixas vinculadas.")
    if solicitacoes_count:
        blockers.append("Ainda existem solicitações ligadas a este instalador.")
    if mov_material_count:
        blockers.append("Já existem movimentações de material ligadas a este instalador.")
    if mov_peca_count:
        blockers.append("Já existem movimentações de peças ligadas a este instalador.")
    if conferencias_count:
        blockers.append("Já existem conferências registradas para este instalador.")
    if posse_com_saldo:
        blockers.append("Ainda existem peças em posse deste instalador.")

    if posse_linhas and not posse_com_saldo:
        warnings.append("As linhas de posse zeradas serão removidas junto com o cadastro.")

    return {
        "entity_kind": "Instalador",
        "entity_title": instalador.nome,
        "entity_subtitle": instalador.matricula,
        "confirm_value": instalador.matricula,
        "details": [
            ("Matrícula", instalador.matricula),
            ("Ativo", "Sim" if instalador.ativo else "Não"),
            ("Solicitações", str(solicitacoes_count)),
            ("Hidrômetros", str(len(hidrometros))),
            ("Caixas", str(caixas_vinculadas)),
        ],
        "impact_items": [
            "O cadastro do instalador será apagado definitivamente.",
        ],
        "warnings": warnings,
        "blockers": blockers,
        "allowed": not blockers,
    }


def execute_instalador_cleanup(db: Session, instalador: Instalador) -> dict[str, Any]:
    preview = build_instalador_cleanup_preview(db, instalador)
    if not preview["allowed"]:
        raise CleanupBlockedError("Este instalador não pode ser excluído definitivamente no estado atual.")

    posse_linhas = db.query(InstaladorPeca).filter(InstaladorPeca.instalador_id == instalador.id).all()
    for item in posse_linhas:
        db.delete(item)

    db.delete(instalador)

    return {
        "kind": "instalador",
        "instalador_id": instalador.id,
        "nome": instalador.nome,
        "matricula": instalador.matricula,
        "linhas_posse_removidas": len(posse_linhas),
    }
