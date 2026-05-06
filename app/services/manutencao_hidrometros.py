from __future__ import annotations

import os
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload, object_session

from app.models import (
    AuditoriaLog,
    CaixaHidrometro,
    CaixaStatus,
    Hidrometro,
    HidrometroManutencao,
    HidrometroStatus,
    ManutencaoHidrometroStatus,
    MovimentacaoMaterial,
    MovimentacaoTipo,
)
from app.services.auditoria import registrar_auditoria
from app.services.regras_caixa import quantidade_esperada_caixa
from app.utils import normalize_text, utc_now


CAIXA_HIDROMETROS_REL = next(
    (name for name in CaixaHidrometro.__mapper__.relationships.keys() if "hidr" in name.lower()),
    None,
)
CAIXA_HIDROMETROS_ATTR = getattr(CaixaHidrometro, CAIXA_HIDROMETROS_REL) if CAIXA_HIDROMETROS_REL else None

DECISAO_VOLTAR_ESTOQUE = "VOLTAR_ESTOQUE"
DECISAO_VOLTAR_CAIXA_ORIGEM = "VOLTAR_CAIXA_ORIGEM"
DECISAO_CONTINUAR_MANUTENCAO = "CONTINUAR_MANUTENCAO"
DECISAO_DESCARTAR = "DESCARTAR"
ORIGEM_PRIORIDADE_RETORNO = "RETORNO_ASSISTENCIA"
STATUS_OPERACIONAL_DISPONIVEL = "DISPONIVEL"
STATUS_OPERACIONAL_MANUTENCAO = "MANUTENCAO"
STATUS_OPERACIONAL_DESCARTADO = "DESCARTADO"
REVERSAO_DESTINO_ESTOQUE_SOLTO = "ESTOQUE_SOLTO"
REVERSAO_DESTINO_CAIXA_ORIGEM = "CAIXA_ORIGEM"

ALERTA_PRIORIDADE_DIAS = int(os.getenv("MANUTENCAO_PRIORIDADE_ALERTA_DIAS", "7"))
CRITICO_PRIORIDADE_DIAS = int(os.getenv("MANUTENCAO_PRIORIDADE_CRITICO_DIAS", "15"))


def manutencao_ativa_do_hidrometro(db: Session, hidrometro_id: int) -> HidrometroManutencao | None:
    return db.query(HidrometroManutencao).filter(
        HidrometroManutencao.hidrometro_id == hidrometro_id,
        HidrometroManutencao.revertida == False,
        HidrometroManutencao.status.in_(
            [
                ManutencaoHidrometroStatus.EM_MANUTENCAO.value,
                ManutencaoHidrometroStatus.ENVIADO_ASSISTENCIA.value,
                ManutencaoHidrometroStatus.RETORNADO_MANUTENCAO.value,
            ]
        ),
    ).order_by(HidrometroManutencao.id.desc()).first()


def manutencoes_caixa(db: Session, caixa_id: int) -> list[HidrometroManutencao]:
    return db.query(HidrometroManutencao).options(
        joinedload(HidrometroManutencao.hidrometro),
        joinedload(HidrometroManutencao.instalador_origem),
        joinedload(HidrometroManutencao.criado_por_usuario),
        joinedload(HidrometroManutencao.revertida_por_usuario),
    ).filter(
        HidrometroManutencao.caixa_origem_id == caixa_id
    ).order_by(HidrometroManutencao.data_abertura.desc(), HidrometroManutencao.id.desc()).all()


def caixa_incompleta_por_manutencao(db: Session, caixa: CaixaHidrometro | None) -> bool:
    if not caixa:
        return False
    atuais = 0
    if CAIXA_HIDROMETROS_REL:
        atuais = len(getattr(caixa, CAIXA_HIDROMETROS_REL, None) or [])
    esperado = quantidade_esperada_caixa(caixa)
    if atuais >= esperado:
        return False
    removidos = db.query(HidrometroManutencao.id).filter(
        HidrometroManutencao.caixa_origem_id == caixa.id,
    ).count()
    return removidos >= (esperado - atuais)


def hidrometro_disponivel_para_caixa(hidrometro: Hidrometro) -> bool:
    return bool(
        hidrometro
        and hidrometro.status == HidrometroStatus.EM_ESTOQUE
        and hidrometro.caixa_id is None
        and hidrometro.instalador_id is None
        and not hidrometro.em_manutencao
        and not hidrometro.bloqueado_por_manutencao
        and not hidrometro.descartado_tecnico
    )


def caixa_origem_disponivel_para_retorno(manutencao: HidrometroManutencao) -> bool:
    caixa = manutencao.caixa_origem
    if not caixa:
        return False
    hidrometro = manutencao.hidrometro
    session = object_session(caixa) or object_session(manutencao)
    if session and caixa.id:
        query = session.query(Hidrometro.id).filter(Hidrometro.caixa_id == caixa.id)
        if hidrometro and hidrometro.id:
            query = query.filter(Hidrometro.id != hidrometro.id)
        quantidade_atual = query.count()
    else:
        quantidade_atual = len(
            [
                item for item in (getattr(caixa, CAIXA_HIDROMETROS_REL, None) or [])
                if not hidrometro or item.id != hidrometro.id
            ]
        )
    ja_esta_na_caixa = bool(hidrometro and hidrometro.caixa_id == caixa.id)
    return bool(
        caixa.ativo
        and caixa.status == CaixaStatus.EM_ESTOQUE
        and (ja_esta_na_caixa or quantidade_atual < quantidade_esperada_caixa(caixa))
    )


def listar_hidrometros_soltos_disponiveis(db: Session, *, limite: int = 200) -> list[Hidrometro]:
    return db.query(Hidrometro).filter(
        Hidrometro.status == HidrometroStatus.EM_ESTOQUE,
        Hidrometro.caixa_id == None,
        Hidrometro.instalador_id == None,
        Hidrometro.em_manutencao == False,
        Hidrometro.bloqueado_por_manutencao == False,
        Hidrometro.descartado_tecnico == False,
    ).order_by(
        Hidrometro.prioridade_reutilizacao.desc(),
        Hidrometro.data_retorno_manutencao.asc().nullslast(),
        Hidrometro.numero_serie.asc(),
    ).limit(limite).all()


def abrir_manutencao_hidrometro(
    db: Session,
    hidrometro: Hidrometro,
    *,
    usuario_id: int,
    motivo: str,
    descricao_problema: str,
    request=None,
) -> HidrometroManutencao:
    motivo_limpo = normalize_text(motivo)
    descricao_limpa = normalize_text(descricao_problema)
    if not motivo_limpo:
        raise ValueError("Informe o motivo da manutencao.")
    if not descricao_limpa:
        raise ValueError("Descreva o problema encontrado no hidrometro.")

    db.flush()
    locked = db.query(Hidrometro).filter(Hidrometro.id == hidrometro.id).populate_existing().with_for_update().first()
    if locked is not None:
        hidrometro = locked

    if hidrometro.descartado_tecnico or hidrometro.status == HidrometroStatus.DESCARTADO_TECNICO:
        raise ValueError("Hidrometro descartado tecnicamente nunca pode voltar ao fluxo.")
    if hidrometro.em_manutencao or hidrometro.status in {
        HidrometroStatus.EM_MANUTENCAO,
        HidrometroStatus.ENVIADO_ASSISTENCIA,
        HidrometroStatus.RETORNADO_MANUTENCAO,
    }:
        raise ValueError("Este hidrometro ja esta em fluxo de manutencao.")
    if hidrometro.status == HidrometroStatus.INSTALADO:
        raise ValueError("Hidrometro ja instalado exige reversao administrativa antes de qualquer manutencao.")

    momento = utc_now()
    caixa_origem_id = hidrometro.caixa_id
    instalador_origem_id = hidrometro.instalador_id
    status_anterior = hidrometro.status.value

    manutencao = HidrometroManutencao(
        hidrometro_id=hidrometro.id,
        caixa_origem_id=caixa_origem_id,
        instalador_origem_id=instalador_origem_id,
        status=ManutencaoHidrometroStatus.EM_MANUTENCAO.value,
        motivo=motivo_limpo[:120],
        descricao_problema=descricao_limpa,
        data_abertura=momento,
        criado_por=usuario_id,
        updated_at=momento,
    )
    db.add(manutencao)
    db.flush()

    hidrometro.status = HidrometroStatus.EM_MANUTENCAO
    hidrometro.status_operacional = STATUS_OPERACIONAL_MANUTENCAO
    hidrometro.em_manutencao = True
    hidrometro.bloqueado_por_manutencao = True
    hidrometro.prioridade_reutilizacao = False
    hidrometro.origem_prioridade = None
    hidrometro.observacao_prioridade = None
    hidrometro.caixa_id = None
    hidrometro.instalador_id = None
    hidrometro.atualizado_em = momento

    if caixa_origem_id or instalador_origem_id:
        db.add(
            MovimentacaoMaterial(
                tipo=MovimentacaoTipo.DEVOLUCAO,
                caixa_id=caixa_origem_id,
                hidrometro_id=hidrometro.id,
                instalador_id=instalador_origem_id,
                observacoes=f"Retirada para manutencao: {motivo_limpo}",
                registrado_por_id=usuario_id,
                criado_em=momento,
            )
        )

    if instalador_origem_id:
        registrar_auditoria(
            db=db,
            acao="MANUTENCAO_DESVINCULA_INSTALADOR",
            usuario_id=usuario_id,
            tabela=Hidrometro.__tablename__,
            registro_id=hidrometro.id,
            descricao="Hidrometro removido do instalador por defeito em campo.",
            dados_antes={"instalador_id": instalador_origem_id, "status": status_anterior},
            dados_depois={"instalador_id": None, "status": hidrometro.status.value},
            request=request,
            categoria="MANUTENCAO",
        )

    registrar_auditoria(
        db=db,
        acao="MANUTENCAO_HIDROMETRO_ABERTA",
        usuario_id=usuario_id,
        tabela=HidrometroManutencao.__tablename__,
        registro_id=manutencao.id,
        descricao=f"Hidrometro {hidrometro.numero_serie} enviado para manutencao.",
        dados_antes={
            "status": status_anterior,
            "caixa_id": caixa_origem_id,
            "instalador_id": instalador_origem_id,
        },
        dados_depois={
            "status": hidrometro.status.value,
            "motivo": motivo_limpo,
            "descricao": descricao_limpa,
        },
        request=request,
        categoria="MANUTENCAO",
    )
    return manutencao


def enviar_assistencia(
    db: Session,
    manutencao: HidrometroManutencao,
    *,
    usuario_id: int,
    fornecedor: str,
    request=None,
) -> HidrometroManutencao:
    fornecedor_limpo = normalize_text(fornecedor)
    if not fornecedor_limpo:
        raise ValueError("Informe o fornecedor ou assistencia tecnica.")
    if manutencao.revertida:
        raise ValueError("Manutencao revertida administrativamente nao pode ser enviada.")
    if manutencao.status == ManutencaoHidrometroStatus.DESCARTADO_TECNICO.value:
        raise ValueError("Manutencao descartada nao pode ser enviada.")

    momento = utc_now()
    status_anterior = manutencao.status
    manutencao.status = ManutencaoHidrometroStatus.ENVIADO_ASSISTENCIA.value
    manutencao.fornecedor_assistencia = fornecedor_limpo[:160]
    manutencao.data_envio = momento
    manutencao.updated_at = momento
    manutencao.hidrometro.status = HidrometroStatus.ENVIADO_ASSISTENCIA
    manutencao.hidrometro.status_operacional = STATUS_OPERACIONAL_MANUTENCAO
    manutencao.hidrometro.em_manutencao = True
    manutencao.hidrometro.bloqueado_por_manutencao = True
    manutencao.hidrometro.atualizado_em = momento

    registrar_auditoria(
        db=db,
        acao="MANUTENCAO_HIDROMETRO_ENVIADA_ASSISTENCIA",
        usuario_id=usuario_id,
        tabela=HidrometroManutencao.__tablename__,
        registro_id=manutencao.id,
        descricao=f"Hidrometro {manutencao.hidrometro.numero_serie} enviado para assistencia.",
        dados_antes={"status": status_anterior},
        dados_depois={"status": manutencao.status, "fornecedor": fornecedor_limpo},
        request=request,
        categoria="MANUTENCAO",
    )
    return manutencao


def registrar_retorno_assistencia(
    db: Session,
    manutencao: HidrometroManutencao,
    *,
    usuario_id: int,
    laudo: str,
    decisao_final: str = DECISAO_VOLTAR_ESTOQUE,
    observacao_prioridade: str = "",
    request=None,
) -> HidrometroManutencao:
    laudo_limpo = normalize_text(laudo)
    if not laudo_limpo:
        raise ValueError("Informe o laudo tecnico do retorno.")

    decisao = normalize_text(decisao_final, upper=True) or DECISAO_VOLTAR_ESTOQUE
    if decisao not in {DECISAO_VOLTAR_ESTOQUE, DECISAO_VOLTAR_CAIXA_ORIGEM, DECISAO_CONTINUAR_MANUTENCAO, DECISAO_DESCARTAR}:
        raise ValueError("Decisao final invalida.")
    if manutencao.hidrometro.descartado_tecnico:
        raise ValueError("Hidrometro descartado tecnicamente nao pode receber retorno operacional.")
    if manutencao.revertida:
        raise ValueError("Manutencao revertida administrativamente nao pode receber novas acoes.")
    if decisao == DECISAO_VOLTAR_CAIXA_ORIGEM and not caixa_origem_disponivel_para_retorno(manutencao):
        raise ValueError("A caixa de origem precisa estar ativa, em estoque e com vaga para receber o hidrometro.")

    momento = utc_now()
    hidrometro = manutencao.hidrometro
    status_anterior = hidrometro.status.value

    manutencao.data_retorno = momento
    manutencao.laudo = laudo_limpo
    manutencao.decisao_final = decisao
    manutencao.updated_at = momento

    if decisao == DECISAO_CONTINUAR_MANUTENCAO:
        manutencao.status = ManutencaoHidrometroStatus.EM_MANUTENCAO.value
        hidrometro.status = HidrometroStatus.EM_MANUTENCAO
        hidrometro.status_operacional = STATUS_OPERACIONAL_MANUTENCAO
        hidrometro.em_manutencao = True
        hidrometro.bloqueado_por_manutencao = True
        hidrometro.atualizado_em = momento
        registrar_auditoria(
            db=db,
            acao="MANUTENCAO_HIDROMETRO_RETORNO_CONTINUA",
            usuario_id=usuario_id,
            tabela=HidrometroManutencao.__tablename__,
            registro_id=manutencao.id,
            descricao=f"Hidrometro {hidrometro.numero_serie} retornou, mas continua em manutencao.",
            dados_depois={"laudo": laudo_limpo, "decisao_final": decisao},
            request=request,
            categoria="MANUTENCAO",
        )
        return manutencao

    if decisao == DECISAO_DESCARTAR:
        return descartar_hidrometro_manutencao(
            db,
            manutencao,
            usuario_id=usuario_id,
            justificativa=laudo_limpo,
            request=request,
        )

    manutencao.status = ManutencaoHidrometroStatus.RETORNADO_MANUTENCAO.value
    hidrometro.status = HidrometroStatus.EM_ESTOQUE
    hidrometro.status_operacional = STATUS_OPERACIONAL_DISPONIVEL
    hidrometro.em_manutencao = False
    hidrometro.bloqueado_por_manutencao = False
    hidrometro.descartado_tecnico = False
    if decisao == DECISAO_VOLTAR_CAIXA_ORIGEM:
        hidrometro.caixa_id = manutencao.caixa_origem_id
    else:
        hidrometro.caixa_id = None
    hidrometro.instalador_id = None
    hidrometro.retornou_manutencao = True
    hidrometro.data_retorno_manutencao = momento
    if decisao == DECISAO_VOLTAR_CAIXA_ORIGEM:
        hidrometro.prioridade_reutilizacao = False
        hidrometro.origem_prioridade = None
        hidrometro.observacao_prioridade = None
    else:
        hidrometro.prioridade_reutilizacao = True
        hidrometro.origem_prioridade = ORIGEM_PRIORIDADE_RETORNO
        hidrometro.observacao_prioridade = normalize_text(observacao_prioridade) or "Retornou da assistencia tecnica; priorizar reutilizacao."
    hidrometro.atualizado_em = momento

    registrar_auditoria(
        db=db,
        acao="MANUTENCAO_HIDROMETRO_RETORNO_CAIXA_ORIGEM" if decisao == DECISAO_VOLTAR_CAIXA_ORIGEM else "MANUTENCAO_HIDROMETRO_RETORNO_APROVADO",
        usuario_id=usuario_id,
        tabela=HidrometroManutencao.__tablename__,
        registro_id=manutencao.id,
        descricao=(
            f"Hidrometro {hidrometro.numero_serie} voltou aprovado para a caixa de origem em estoque."
            if decisao == DECISAO_VOLTAR_CAIXA_ORIGEM
            else f"Hidrometro {hidrometro.numero_serie} voltou aprovado ao estoque."
        ),
        dados_antes={"status": status_anterior},
        dados_depois={
            "status": hidrometro.status.value,
            "status_operacional": hidrometro.status_operacional,
            "caixa_id": hidrometro.caixa_id,
            "instalador_id": hidrometro.instalador_id,
            "laudo": laudo_limpo,
        },
        request=request,
        categoria="MANUTENCAO",
    )
    if decisao == DECISAO_VOLTAR_CAIXA_ORIGEM:
        return manutencao

    registrar_auditoria(
        db=db,
        acao="MANUTENCAO_PRIORIDADE_REUTILIZACAO_CRIADA",
        usuario_id=usuario_id,
        tabela=Hidrometro.__tablename__,
        registro_id=hidrometro.id,
        descricao="Prioridade de reutilizacao criada por retorno de assistencia.",
        dados_depois={
            "prioridade_reutilizacao": True,
            "origem_prioridade": hidrometro.origem_prioridade,
            "data_retorno_manutencao": momento,
        },
        request=request,
        categoria="MANUTENCAO",
    )
    return manutencao


def descartar_hidrometro_manutencao(
    db: Session,
    manutencao: HidrometroManutencao,
    *,
    usuario_id: int,
    justificativa: str,
    request=None,
) -> HidrometroManutencao:
    justificativa_limpa = normalize_text(justificativa)
    if not justificativa_limpa:
        raise ValueError("Informe a justificativa tecnica do descarte.")
    if manutencao.revertida:
        raise ValueError("Manutencao revertida administrativamente nao pode ser descartada.")

    momento = utc_now()
    hidrometro = manutencao.hidrometro
    manutencao.status = ManutencaoHidrometroStatus.DESCARTADO_TECNICO.value
    manutencao.decisao_final = DECISAO_DESCARTAR
    manutencao.laudo = justificativa_limpa
    manutencao.data_retorno = manutencao.data_retorno or momento
    manutencao.updated_at = momento

    hidrometro.status = HidrometroStatus.DESCARTADO_TECNICO
    hidrometro.status_operacional = STATUS_OPERACIONAL_DESCARTADO
    hidrometro.em_manutencao = False
    hidrometro.bloqueado_por_manutencao = True
    hidrometro.descartado_tecnico = True
    hidrometro.prioridade_reutilizacao = False
    hidrometro.origem_prioridade = None
    hidrometro.observacao_prioridade = None
    hidrometro.caixa_id = None
    hidrometro.instalador_id = None
    hidrometro.atualizado_em = momento

    registrar_auditoria(
        db=db,
        acao="MANUTENCAO_HIDROMETRO_DESCARTADO_TECNICO",
        usuario_id=usuario_id,
        tabela=HidrometroManutencao.__tablename__,
        registro_id=manutencao.id,
        descricao=f"Hidrometro {hidrometro.numero_serie} descartado tecnicamente.",
        dados_depois={"justificativa": justificativa_limpa, "status": hidrometro.status.value},
        request=request,
        severidade="CRITICO",
        categoria="MANUTENCAO",
    )
    return manutencao


def reverter_manutencao_hidrometro(
    db: Session,
    manutencao: HidrometroManutencao,
    *,
    usuario_id: int,
    justificativa: str,
    destino: str = REVERSAO_DESTINO_ESTOQUE_SOLTO,
    request=None,
) -> HidrometroManutencao:
    justificativa_limpa = normalize_text(justificativa)
    destino_normalizado = normalize_text(destino, upper=True) or REVERSAO_DESTINO_ESTOQUE_SOLTO
    if not justificativa_limpa:
        raise ValueError("Informe a justificativa obrigatoria da reversao.")
    if destino_normalizado not in {REVERSAO_DESTINO_ESTOQUE_SOLTO, REVERSAO_DESTINO_CAIXA_ORIGEM}:
        raise ValueError("Destino de reversao invalido.")
    if manutencao.revertida:
        raise ValueError("Esta manutencao ja foi revertida.")
    if not manutencao.hidrometro:
        raise ValueError("Hidrometro da manutencao nao encontrado.")

    db.flush()
    hidrometro = db.query(Hidrometro).filter(
        Hidrometro.id == manutencao.hidrometro_id
    ).populate_existing().with_for_update().first()
    if not hidrometro:
        raise ValueError("Hidrometro da manutencao nao encontrado.")
    manutencao.hidrometro = hidrometro

    if hidrometro.status == HidrometroStatus.INSTALADO or hidrometro.instalado_em:
        raise ValueError("Hidrometro instalado nao pode ter manutencao revertida por este fluxo.")
    if hidrometro.caixa_id and hidrometro.caixa_id != manutencao.caixa_origem_id:
        raise ValueError("Hidrometro ja foi vinculado a outra caixa; reverta essa operacao primeiro.")
    if hidrometro.instalador_id:
        raise ValueError("Hidrometro esta vinculado a instalador; reverta a entrega antes de alterar a manutencao.")
    if hidrometro.descartado_tecnico or hidrometro.status == HidrometroStatus.DESCARTADO_TECNICO:
        raise ValueError("Descarte tecnico e final e nao deve ser revertido por este fluxo.")

    if destino_normalizado == REVERSAO_DESTINO_CAIXA_ORIGEM and not caixa_origem_disponivel_para_retorno(manutencao):
        raise ValueError("A caixa de origem precisa estar ativa, em estoque e com vaga para receber o hidrometro.")

    momento = utc_now()
    dados_antes = {
        "manutencao_id": manutencao.id,
        "status_manutencao": manutencao.status,
        "hidrometro_status": hidrometro.status.value,
        "caixa_id": hidrometro.caixa_id,
        "instalador_id": hidrometro.instalador_id,
        "em_manutencao": hidrometro.em_manutencao,
        "bloqueado_por_manutencao": hidrometro.bloqueado_por_manutencao,
        "prioridade_reutilizacao": hidrometro.prioridade_reutilizacao,
    }

    hidrometro.status = HidrometroStatus.EM_ESTOQUE
    hidrometro.status_operacional = STATUS_OPERACIONAL_DISPONIVEL
    hidrometro.em_manutencao = False
    hidrometro.bloqueado_por_manutencao = False
    hidrometro.descartado_tecnico = False
    hidrometro.instalador_id = None
    hidrometro.caixa_id = manutencao.caixa_origem_id if destino_normalizado == REVERSAO_DESTINO_CAIXA_ORIGEM else None
    hidrometro.prioridade_reutilizacao = False
    hidrometro.origem_prioridade = None
    hidrometro.observacao_prioridade = None
    if manutencao.data_retorno and hidrometro.data_retorno_manutencao == manutencao.data_retorno:
        hidrometro.retornou_manutencao = False
        hidrometro.data_retorno_manutencao = None
    hidrometro.atualizado_em = momento

    manutencao.revertida = True
    manutencao.revertida_em = momento
    manutencao.revertida_por = usuario_id
    manutencao.justificativa_reversao = justificativa_limpa
    manutencao.destino_reversao = destino_normalizado
    manutencao.updated_at = momento

    registrar_auditoria(
        db=db,
        acao="ADMIN_REVERTE_MANUTENCAO_HIDROMETRO",
        usuario_id=usuario_id,
        tabela=HidrometroManutencao.__tablename__,
        registro_id=manutencao.id,
        descricao=f"Manutencao do hidrometro {hidrometro.numero_serie} revertida administrativamente.",
        dados_antes=dados_antes,
        dados_depois={
            "destino": destino_normalizado,
            "caixa_id": hidrometro.caixa_id,
            "hidrometro_status": hidrometro.status.value,
            "justificativa": justificativa_limpa,
        },
        request=request,
        severidade="CRITICO",
        categoria="MANUTENCAO",
    )
    return manutencao


def vincular_hidrometro_solto_a_caixa(
    db: Session,
    hidrometro: Hidrometro,
    caixa: CaixaHidrometro,
    *,
    usuario_id: int,
    request=None,
) -> Hidrometro:
    if not hidrometro_disponivel_para_caixa(hidrometro):
        raise ValueError(f"Hidrometro {hidrometro.numero_serie} nao esta disponivel como item solto no estoque.")
    if caixa.status != CaixaStatus.EM_ESTOQUE:
        raise ValueError("A nova caixa precisa estar em estoque para receber hidrometros.")

    tinha_prioridade = bool(hidrometro.prioridade_reutilizacao)
    dados_antes = {
        "caixa_id": hidrometro.caixa_id,
        "prioridade_reutilizacao": hidrometro.prioridade_reutilizacao,
        "origem_prioridade": hidrometro.origem_prioridade,
    }
    hidrometro.caixa_id = caixa.id
    hidrometro.status = HidrometroStatus.EM_ESTOQUE
    hidrometro.status_operacional = STATUS_OPERACIONAL_DISPONIVEL
    hidrometro.instalador_id = None
    hidrometro.em_manutencao = False
    hidrometro.bloqueado_por_manutencao = False
    hidrometro.descartado_tecnico = False
    hidrometro.prioridade_reutilizacao = False
    hidrometro.origem_prioridade = None
    hidrometro.observacao_prioridade = None
    hidrometro.atualizado_em = utc_now()

    if tinha_prioridade:
        registrar_auditoria(
            db=db,
            acao="MANUTENCAO_HIDROMETRO_REUTILIZADO_NOVA_CAIXA",
            usuario_id=usuario_id,
            tabela=Hidrometro.__tablename__,
            registro_id=hidrometro.id,
            descricao="Hidrometro retornado da assistencia reutilizado em nova caixa.",
            dados_antes=dados_antes,
            dados_depois={
                "caixa_id": caixa.id,
                "numero_interno": caixa.numero_interno,
                "prioridade_reutilizacao": False,
            },
            request=request,
            categoria="MANUTENCAO",
        )
    return hidrometro


def remover_prioridade_reutilizacao(
    db: Session,
    hidrometro: Hidrometro,
    *,
    usuario_id: int,
    justificativa: str,
    request=None,
) -> None:
    justificativa_limpa = normalize_text(justificativa)
    if not justificativa_limpa:
        raise ValueError("Informe a justificativa para remover a prioridade.")
    if not hidrometro.prioridade_reutilizacao:
        raise ValueError("Este hidrometro nao possui prioridade ativa.")

    dados_antes = {
        "prioridade_reutilizacao": hidrometro.prioridade_reutilizacao,
        "origem_prioridade": hidrometro.origem_prioridade,
        "observacao_prioridade": hidrometro.observacao_prioridade,
    }
    hidrometro.prioridade_reutilizacao = False
    hidrometro.origem_prioridade = None
    hidrometro.observacao_prioridade = None
    hidrometro.atualizado_em = utc_now()
    registrar_auditoria(
        db=db,
        acao="MANUTENCAO_PRIORIDADE_REUTILIZACAO_REMOVIDA",
        usuario_id=usuario_id,
        tabela=Hidrometro.__tablename__,
        registro_id=hidrometro.id,
        descricao="Prioridade de reutilizacao removida manualmente.",
        dados_antes=dados_antes,
        dados_depois={"prioridade_reutilizacao": False, "justificativa": justificativa_limpa},
        request=request,
        categoria="MANUTENCAO",
    )


def hidrometros_prioridade_parados(db: Session) -> list[dict[str, Any]]:
    agora = utc_now()
    linhas = db.query(Hidrometro).filter(
        Hidrometro.prioridade_reutilizacao == True,
        Hidrometro.status == HidrometroStatus.EM_ESTOQUE,
        Hidrometro.caixa_id == None,
        Hidrometro.instalador_id == None,
        Hidrometro.data_retorno_manutencao.isnot(None),
        Hidrometro.descartado_tecnico == False,
        Hidrometro.em_manutencao == False,
    ).order_by(Hidrometro.data_retorno_manutencao.asc()).all()
    alertas: list[dict[str, Any]] = []
    for hidrometro in linhas:
        dias = int((agora - hidrometro.data_retorno_manutencao).total_seconds() // 86400)
        if dias < ALERTA_PRIORIDADE_DIAS:
            continue
        alertas.append(
            {
                "hidrometro": hidrometro,
                "numero_serie": hidrometro.numero_serie,
                "dias_parado": dias,
                "data_retorno": hidrometro.data_retorno_manutencao,
                "status": "CRITICO" if dias >= CRITICO_PRIORIDADE_DIAS else "ALERTA",
            }
        )
    return alertas


def resumo_manutencao_hidrometros(db: Session) -> dict[str, Any]:
    em_manutencao = db.query(Hidrometro).filter(Hidrometro.em_manutencao == True).count()
    retornados = db.query(Hidrometro).filter(Hidrometro.retornou_manutencao == True).count()
    descartados = db.query(Hidrometro).filter(Hidrometro.descartado_tecnico == True).count()
    prioridades = db.query(Hidrometro).filter(Hidrometro.prioridade_reutilizacao == True).count()
    query_caixas = db.query(CaixaHidrometro).filter(CaixaHidrometro.ativo == True)
    if CAIXA_HIDROMETROS_ATTR is not None:
        query_caixas = query_caixas.options(joinedload(CAIXA_HIDROMETROS_ATTR))
    caixas = query_caixas.all()
    caixas_incompletas = [
        caixa
        for caixa in caixas
        if (len(getattr(caixa, CAIXA_HIDROMETROS_REL, None) or []) if CAIXA_HIDROMETROS_REL else 0)
        < quantidade_esperada_caixa(caixa)
    ]
    tempos = [
        (row.data_retorno - row.data_abertura).total_seconds() / 86400
        for row in db.query(HidrometroManutencao).filter(
            HidrometroManutencao.data_retorno.isnot(None),
            HidrometroManutencao.data_abertura.isnot(None),
        ).all()
        if row.data_retorno and row.data_abertura
    ]
    reutilizados = db.query(AuditoriaLog).filter(
        AuditoriaLog.acao == "MANUTENCAO_HIDROMETRO_REUTILIZADO_NOVA_CAIXA"
    ).count()
    alertas = hidrometros_prioridade_parados(db)
    return {
        "em_manutencao": em_manutencao,
        "retornados": retornados,
        "descartados": descartados,
        "prioridade_reutilizacao": prioridades,
        "retornados_disponiveis": db.query(Hidrometro).filter(
            Hidrometro.retornou_manutencao == True,
            Hidrometro.status == HidrometroStatus.EM_ESTOQUE,
            Hidrometro.caixa_id == None,
            Hidrometro.em_manutencao == False,
            Hidrometro.descartado_tecnico == False,
        ).count(),
        "retornados_reutilizados": reutilizados,
        "parados_alerta": len(alertas),
        "prioridade_vencida": sum(1 for item in alertas if item["status"] == "CRITICO"),
        "caixas_incompletas": len(caixas_incompletas),
        "tempo_medio_dias": (sum(tempos) / len(tempos)) if tempos else None,
        "alertas_prioridade": alertas,
        "alerta_dias": ALERTA_PRIORIDADE_DIAS,
        "critico_dias": CRITICO_PRIORIDADE_DIAS,
    }


def hidrometros_disponiveis_almoxarifado(db: Session) -> int:
    em_caixas = db.query(Hidrometro.id).join(CaixaHidrometro).filter(
        Hidrometro.status == HidrometroStatus.EM_ESTOQUE,
        Hidrometro.em_manutencao == False,
        Hidrometro.descartado_tecnico == False,
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.EM_ESTOQUE,
    )
    soltos = db.query(Hidrometro.id).filter(
        Hidrometro.status == HidrometroStatus.EM_ESTOQUE,
        Hidrometro.caixa_id == None,
        Hidrometro.instalador_id == None,
        Hidrometro.em_manutencao == False,
        Hidrometro.descartado_tecnico == False,
        or_(Hidrometro.retornou_manutencao == True, Hidrometro.prioridade_reutilizacao == True),
    )
    return em_caixas.count() + soltos.count()


def linhas_exportacao_manutencao(manutencoes: list[HidrometroManutencao]) -> list[list[object]]:
    agora = utc_now()
    rows: list[list[object]] = []
    for item in manutencoes:
        h = item.hidrometro
        dias_parado = ""
        if h and h.prioridade_reutilizacao and h.data_retorno_manutencao:
            dias_parado = int((agora - h.data_retorno_manutencao).total_seconds() // 86400)
        rows.append(
            [
                item.id,
                h.numero_serie if h else "",
                item.caixa_origem.numero_interno if item.caixa_origem else "",
                item.status,
                item.motivo,
                item.descricao_problema,
                item.fornecedor_assistencia or "",
                item.data_abertura,
                item.data_envio,
                item.data_retorno,
                item.laudo or "",
                item.decisao_final or "",
                "SIM" if item.revertida else "NAO",
                item.revertida_em,
                item.destino_reversao or "",
                item.justificativa_reversao or "",
                "SIM" if h and h.retornou_manutencao else "NAO",
                h.data_retorno_manutencao if h else None,
                "SIM" if h and h.prioridade_reutilizacao else "NAO",
                dias_parado,
                h.origem_prioridade if h else "",
            ]
        )
    return rows
