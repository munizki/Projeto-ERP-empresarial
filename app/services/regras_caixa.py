from sqlalchemy.orm import Session

from app.models import BoxRuleConfig, CaixaHidrometro
from app.services.auditoria import registrar_auditoria
from app.utils import utc_now


DEFAULT_HIDROMETROS_POR_CAIXA = 6
MIN_HIDROMETROS_POR_CAIXA = 1
MAX_HIDROMETROS_POR_CAIXA = 100


def obter_regra_caixa_ativa(db: Session, *, criar_padrao: bool = True) -> BoxRuleConfig:
    regra = db.query(BoxRuleConfig).filter(BoxRuleConfig.ativo == True).order_by(
        BoxRuleConfig.vigente_desde.desc(),
        BoxRuleConfig.id.desc(),
    ).first()
    if regra or not criar_padrao:
        return regra

    regra = BoxRuleConfig(
        quantidade_hidrometros=DEFAULT_HIDROMETROS_POR_CAIXA,
        ativo=True,
        vigente_desde=utc_now(),
        criado_em=utc_now(),
    )
    db.add(regra)
    db.flush()
    return regra


def validar_quantidade_hidrometros_por_caixa(quantidade: int) -> None:
    if quantidade < MIN_HIDROMETROS_POR_CAIXA:
        raise ValueError("A quantidade de hidrometros por caixa precisa ser maior que zero.")
    if quantidade > MAX_HIDROMETROS_POR_CAIXA:
        raise ValueError(f"A quantidade maxima permitida por caixa e {MAX_HIDROMETROS_POR_CAIXA}.")


def alterar_regra_caixa(
    db: Session,
    *,
    quantidade_hidrometros: int,
    usuario_id: int,
    justificativa: str,
) -> BoxRuleConfig:
    validar_quantidade_hidrometros_por_caixa(quantidade_hidrometros)
    regra_anterior = obter_regra_caixa_ativa(db, criar_padrao=True)
    if regra_anterior and regra_anterior.quantidade_hidrometros == quantidade_hidrometros:
        raise ValueError("A nova quantidade precisa ser diferente da regra ativa.")

    regras_ativas = db.query(BoxRuleConfig).filter(BoxRuleConfig.ativo == True).with_for_update().all()
    for regra in regras_ativas:
        regra.ativo = False

    nova_regra = BoxRuleConfig(
        quantidade_hidrometros=quantidade_hidrometros,
        ativo=True,
        vigente_desde=utc_now(),
        criado_por_id=usuario_id,
        criado_em=utc_now(),
    )
    db.add(nova_regra)
    db.flush()

    registrar_auditoria(
        db=db,
        acao="ADMIN_UPDATE_REGRA_CAIXA",
        usuario_id=usuario_id,
        tabela=BoxRuleConfig.__tablename__,
        registro_id=nova_regra.id,
        descricao="Regra de quantidade de hidrometros por caixa alterada",
        dados_antes={
            "regra_id": regra_anterior.id if regra_anterior else None,
            "quantidade_hidrometros": regra_anterior.quantidade_hidrometros if regra_anterior else None,
        },
        dados_depois={
            "regra_id": nova_regra.id,
            "quantidade_hidrometros": nova_regra.quantidade_hidrometros,
            "justificativa": justificativa,
        },
    )
    return nova_regra


def quantidade_esperada_caixa(caixa: CaixaHidrometro | None) -> int:
    if not caixa:
        return DEFAULT_HIDROMETROS_POR_CAIXA
    return int(caixa.quantidade_esperada or DEFAULT_HIDROMETROS_POR_CAIXA)
