from sqlalchemy.orm import Session, joinedload

from app.models import (
    CaixaHidrometro,
    CaixaStatus,
    EstoquePeca,
    Hidrometro,
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
)
from app.services.regras_caixa import quantidade_esperada_caixa
from app.services.manutencao_hidrometros import caixa_incompleta_por_manutencao
from app.utils import normalize_text, utc_now


CAIXA_HIDROMETROS_ATTR = getattr(CaixaHidrometro, "hidrômetros")
INSTALADOR_HIDROMETROS_ATTR = getattr(Instalador, "hidrômetros")
TIPO_PECA_ESTOQUE_ATTR = getattr(TipoPeca, "estoque")


def _get_caixa_hidrometros(caixa: CaixaHidrometro | None) -> list[Hidrometro]:
    if not caixa:
        return []
    return list(getattr(caixa, CAIXA_HIDROMETROS_ATTR.key) or [])


def carregar_solicitacao_operacional(db: Session, solicitacao_id: int) -> Solicitacao | None:
    return db.query(Solicitacao).options(
        joinedload(Solicitacao.instalador),
        joinedload(Solicitacao.criado_por),
        joinedload(Solicitacao.entregue_por),
        joinedload(Solicitacao.itens_caixa).joinedload(SolicitacaoItemCaixa.caixa).joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca).joinedload(TIPO_PECA_ESTOQUE_ATTR),
    ).filter(Solicitacao.id == solicitacao_id).first()


def caixas_reservadas_ids(db: Session, ignorar_solicitacao_id: int | None = None) -> list[int]:
    query = db.query(SolicitacaoItemCaixa.caixa_id).join(Solicitacao).filter(
        SolicitacaoItemCaixa.caixa_id.isnot(None),
        Solicitacao.status == SolicitacaoStatus.SEPARADA,
    )
    if ignorar_solicitacao_id is not None:
        query = query.filter(Solicitacao.id != ignorar_solicitacao_id)
    return [caixa_id for (caixa_id,) in query.all()]


def listar_caixas_disponiveis(db: Session, ignorar_solicitacao_id: int | None = None, *, bloquear: bool = False):
    reservadas = caixas_reservadas_ids(db, ignorar_solicitacao_id=ignorar_solicitacao_id)
    query = db.query(CaixaHidrometro).options(joinedload(CAIXA_HIDROMETROS_ATTR)).filter(
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.EM_ESTOQUE,
    )
    if reservadas:
        query = query.filter(~CaixaHidrometro.id.in_(reservadas))
    if bloquear:
        query = query.with_for_update(of=CaixaHidrometro)
    return query.order_by(CaixaHidrometro.numero_interno).all()


def validar_estoque_pecas(solicitacao: Solicitacao) -> list[str]:
    erros: list[str] = []
    for item in solicitacao.itens_peca:
        estoque = item.tipo_peca.estoque
        disponivel = estoque.quantidade_atual if estoque else 0
        if item.quantidade_solicitada > disponivel:
            erros.append(
                f"{item.tipo_peca.nome}: solicitado {item.quantidade_solicitada}, disponivel {disponivel}."
            )
    return erros


def validar_caixa_pronta_para_saida(caixa: CaixaHidrometro | None, db: Session | None = None) -> str:
    if caixa is None:
        return "Existe caixa sem vinculo fisico."
    if not caixa.ativo:
        return f"A caixa {caixa.numero_interno} esta inativa."
    if caixa.status != CaixaStatus.EM_ESTOQUE:
        return f"A caixa {caixa.numero_interno} nao esta em estoque."

    hidrometros = _get_caixa_hidrometros(caixa)
    esperado = quantidade_esperada_caixa(caixa)
    if len(hidrometros) != esperado:
        if db is None or not caixa_incompleta_por_manutencao(db, caixa):
            return f"A caixa {caixa.numero_interno} esta incompleta: {len(hidrometros)}/{esperado} hidrometros."
    if any(hidrometro.status != HidrometroStatus.EM_ESTOQUE for hidrometro in hidrometros):
        return f"A caixa {caixa.numero_interno} possui hidrometro fora do estoque."
    return ""


def separar_solicitacao(db: Session, solicitacao: Solicitacao, caixas_por_item: dict[int, int]) -> None:
    db.flush()
    locked = db.query(Solicitacao).filter(Solicitacao.id == solicitacao.id).populate_existing().with_for_update().first()
    if locked is not None:
        solicitacao = locked
    if solicitacao.status not in {SolicitacaoStatus.PENDENTE, SolicitacaoStatus.SEPARADA}:
        raise ValueError("A solicitacao nao pode mais ser separada.")

    itens_caixa = list(solicitacao.itens_caixa)
    if len(caixas_por_item) != len(itens_caixa):
        raise ValueError("Selecione uma caixa para cada item solicitado.")

    caixa_ids = list(caixas_por_item.values())
    if len(set(caixa_ids)) != len(caixa_ids):
        raise ValueError("A mesma caixa nao pode ser usada duas vezes na mesma solicitacao.")

    caixas_disponiveis = {
        caixa.id: caixa
        for caixa in listar_caixas_disponiveis(db, ignorar_solicitacao_id=solicitacao.id, bloquear=True)
    }
    reservadas_apos_lock = set(caixas_reservadas_ids(db, ignorar_solicitacao_id=solicitacao.id))

    for item in itens_caixa:
        caixa_id = caixas_por_item.get(item.id)
        if caixa_id in reservadas_apos_lock:
            raise ValueError("Uma das caixas selecionadas acabou de ser reservada por outra operacao.")
        caixa = caixas_disponiveis.get(caixa_id)
        if not caixa:
            raise ValueError("Uma das caixas selecionadas nao esta disponivel para separacao.")
        erro_caixa = validar_caixa_pronta_para_saida(caixa, db)
        if erro_caixa:
            raise ValueError(erro_caixa)
        item.caixa_id = caixa.id
        item.caixa = caixa

    erros_pecas = validar_estoque_pecas(solicitacao)
    if erros_pecas:
        raise ValueError("Estoque insuficiente para as pecas solicitadas: " + " | ".join(erros_pecas))

    solicitacao.status = SolicitacaoStatus.SEPARADA
    solicitacao.separado_em = utc_now()
    db.flush()


def confirmar_entrega_solicitacao(
    db: Session,
    solicitacao: Solicitacao,
    usuario_id: int,
    observacoes: str = "",
) -> None:
    db.flush()
    locked = db.query(Solicitacao).filter(Solicitacao.id == solicitacao.id).populate_existing().with_for_update().first()
    if locked is not None:
        solicitacao = locked
    if solicitacao.status != SolicitacaoStatus.SEPARADA:
        raise ValueError("A solicitacao precisa estar separada antes da entrega.")

    if any(item.caixa is None for item in solicitacao.itens_caixa):
        raise ValueError("Todas as caixas precisam estar selecionadas antes da entrega.")

    erros_pecas = validar_estoque_pecas(solicitacao)
    if erros_pecas:
        raise ValueError("Estoque insuficiente para concluir a entrega: " + " | ".join(erros_pecas))

    momento = utc_now()
    texto_obs = normalize_text(observacoes)
    descricao_mov = texto_obs or f"Entrega da solicitacao #{solicitacao.id}"

    for item in solicitacao.itens_caixa:
        caixa = db.query(CaixaHidrometro).options(joinedload(CAIXA_HIDROMETROS_ATTR)).filter(
            CaixaHidrometro.id == item.caixa_id
        ).with_for_update(of=CaixaHidrometro).first() or item.caixa
        if caixa.status != CaixaStatus.EM_ESTOQUE:
            raise ValueError(f"A caixa {caixa.numero_interno} nao esta mais em estoque.")
        erro_caixa = validar_caixa_pronta_para_saida(caixa, db)
        if erro_caixa:
            raise ValueError(erro_caixa)

        caixa.status = CaixaStatus.ENTREGUE
        caixa.instalador_id = solicitacao.instalador_id

        for hidrometro in caixa.hidrômetros:
            hidrometro.status = HidrometroStatus.COM_INSTALADOR
            hidrometro.instalador_id = solicitacao.instalador_id
            hidrometro.atualizado_em = momento

        db.add(MovimentacaoMaterial(
            tipo=MovimentacaoTipo.SAIDA,
            caixa_id=caixa.id,
            instalador_id=solicitacao.instalador_id,
            solicitacao_id=solicitacao.id,
            observacoes=descricao_mov,
            registrado_por_id=usuario_id,
            criado_em=momento,
        ))

    for item in solicitacao.itens_peca:
        estoque = db.query(EstoquePeca).filter(
            EstoquePeca.tipo_peca_id == item.tipo_peca_id
        ).with_for_update().first()
        if not estoque or estoque.quantidade_atual < item.quantidade_solicitada:
            disponivel = estoque.quantidade_atual if estoque else 0
            raise ValueError(
                f"Estoque insuficiente para concluir a entrega de {item.tipo_peca.nome}: "
                f"solicitado {item.quantidade_solicitada}, disponivel {disponivel}."
            )
        estoque.quantidade_atual -= item.quantidade_solicitada
        estoque.atualizado_em = momento

        posse = db.query(InstaladorPeca).filter(
            InstaladorPeca.instalador_id == solicitacao.instalador_id,
            InstaladorPeca.tipo_peca_id == item.tipo_peca_id,
        ).with_for_update().first()
        if not posse:
            posse = InstaladorPeca(
                instalador_id=solicitacao.instalador_id,
                tipo_peca_id=item.tipo_peca_id,
                quantidade=0,
            )
            db.add(posse)
            db.flush()

        posse.quantidade += item.quantidade_solicitada
        posse.atualizado_em = momento

        db.add(MovimentacaoPeca(
            tipo=MovimentacaoTipo.SAIDA,
            tipo_peca_id=item.tipo_peca_id,
            quantidade=item.quantidade_solicitada,
            instalador_id=solicitacao.instalador_id,
            solicitacao_id=solicitacao.id,
            observacoes=descricao_mov,
            registrado_por_id=usuario_id,
            criado_em=momento,
        ))

    solicitacao.status = SolicitacaoStatus.ENTREGUE
    solicitacao.entregue_em = momento
    solicitacao.entregue_por_id = usuario_id
    solicitacao.recebimento_instalador_status = "pendente"
    solicitacao.confirmacao_instalador_em = None
    solicitacao.usuario_confirmacao_instalador_id = None
    solicitacao.motivo_divergencia_instalador = None


def mensagem_reversao_entrega_solicitacao(db: Session, solicitacao: Solicitacao | None) -> str:
    if not solicitacao:
        return "Solicitacao nao encontrada."
    if solicitacao.status != SolicitacaoStatus.ENTREGUE:
        return "A reversao administrativa da entrega so esta disponivel para solicitacoes ja entregues."

    for item in solicitacao.itens_caixa:
        caixa = item.caixa
        if caixa is None:
            return "Existe item de caixa sem vinculo fisico nesta solicitacao."
        if caixa.status != CaixaStatus.ENTREGUE:
            return f"A caixa {caixa.numero_interno} ja saiu do contexto esperado da entrega."

        hidrometros = _get_caixa_hidrometros(caixa)
        if any(h.status == HidrometroStatus.INSTALADO for h in hidrometros):
            return f"A caixa {caixa.numero_interno} ja possui hidrometro instalado e nao pode ter a entrega revertida."
        if any(h.status != HidrometroStatus.COM_INSTALADOR for h in hidrometros):
            return f"A caixa {caixa.numero_interno} ja possui hidrometros fora do contexto do instalador."

    for item in solicitacao.itens_peca:
        posse = db.query(InstaladorPeca).filter(
            InstaladorPeca.instalador_id == solicitacao.instalador_id,
            InstaladorPeca.tipo_peca_id == item.tipo_peca_id,
        ).first()
        quantidade_posse = posse.quantidade if posse else 0
        if quantidade_posse < item.quantidade_solicitada:
            nome_peca = item.tipo_peca.nome if item.tipo_peca else f"ID {item.tipo_peca_id}"
            return (
                f"O instalador ja nao possui saldo suficiente da peca {nome_peca} "
                "para desfazer esta entrega com seguranca."
            )

    return ""


def reverter_entrega_solicitacao(
    db: Session,
    solicitacao: Solicitacao,
    usuario_id: int,
    *,
    justificativa: str = "",
) -> dict[str, object]:
    mensagem = mensagem_reversao_entrega_solicitacao(db, solicitacao)
    if mensagem:
        raise ValueError(mensagem)

    momento = utc_now()
    justificativa_limpa = normalize_text(justificativa)
    descricao_mov = justificativa_limpa or f"Reversao admin da entrega da solicitacao #{solicitacao.id}"

    caixas_revertidas = 0
    pecas_revertidas = 0

    for item in solicitacao.itens_caixa:
        caixa = item.caixa
        if caixa is None:
            continue

        caixa.status = CaixaStatus.EM_ESTOQUE
        caixa.instalador_id = None
        caixa.atualizado_em = momento
        caixas_revertidas += 1

        for hidrometro in _get_caixa_hidrometros(caixa):
            hidrometro.status = HidrometroStatus.EM_ESTOQUE
            hidrometro.instalador_id = None
            hidrometro.atualizado_em = momento

        db.add(MovimentacaoMaterial(
            tipo=MovimentacaoTipo.DEVOLUCAO,
            caixa_id=caixa.id,
            instalador_id=solicitacao.instalador_id,
            solicitacao_id=solicitacao.id,
            observacoes=descricao_mov,
            registrado_por_id=usuario_id,
            criado_em=momento,
        ))

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

        db.add(MovimentacaoPeca(
            tipo=MovimentacaoTipo.DEVOLUCAO,
            tipo_peca_id=item.tipo_peca_id,
            quantidade=item.quantidade_solicitada,
            instalador_id=solicitacao.instalador_id,
            solicitacao_id=solicitacao.id,
            observacoes=descricao_mov,
            registrado_por_id=usuario_id,
            criado_em=momento,
        ))
        pecas_revertidas += item.quantidade_solicitada

    solicitacao.status = SolicitacaoStatus.SEPARADA
    solicitacao.entregue_em = None
    solicitacao.entregue_por_id = None
    solicitacao.recebimento_instalador_status = None
    solicitacao.confirmacao_instalador_em = None
    solicitacao.usuario_confirmacao_instalador_id = None
    solicitacao.motivo_divergencia_instalador = None

    return {
        "momento": momento,
        "caixas_revertidas": caixas_revertidas,
        "pecas_revertidas": pecas_revertidas,
        "justificativa": justificativa_limpa or None,
        "status_restaurado": solicitacao.status.value,
    }
