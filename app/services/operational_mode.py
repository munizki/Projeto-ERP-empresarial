from __future__ import annotations

import os

from sqlalchemy.orm import Session

from app.models import SystemFlag
from app.services.auditoria import registrar_auditoria
from app.services.integridade import diagnostico_operacional
from app.utils import normalize_text, utc_now


READ_ONLY_FLAG = "emergency_read_only"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "on", "yes", "sim"}


def get_system_flag(db: Session, chave: str) -> SystemFlag | None:
    return db.query(SystemFlag).filter(SystemFlag.chave == chave).first()


def set_system_flag(
    db: Session,
    *,
    chave: str,
    valor: str,
    usuario_id: int | None,
    motivo: str = "",
) -> SystemFlag:
    flag = get_system_flag(db, chave)
    if not flag:
        flag = SystemFlag(chave=chave)
        db.add(flag)
    flag.valor = valor
    flag.motivo = normalize_text(motivo)
    flag.atualizado_por_id = usuario_id
    flag.atualizado_em = utc_now()
    return flag


def modo_leitura_status(db: Session) -> dict[str, object]:
    flag = get_system_flag(db, READ_ONLY_FLAG)
    manual_ativo = _truthy(flag.valor if flag else None) or _truthy(os.getenv("READ_ONLY_MODE"))
    diagnostico = diagnostico_operacional(db)
    bloqueadores = list(diagnostico["bloqueadores_criticos"])
    ativo = manual_ativo or bool(bloqueadores)
    motivo = flag.motivo if flag and flag.motivo else ""
    if not motivo and bloqueadores:
        motivo = bloqueadores[0]

    return {
        "ativo": ativo,
        "manual_ativo": manual_ativo,
        "automatico_ativo": bool(bloqueadores),
        "motivo": motivo,
        "bloqueadores": bloqueadores,
        "atualizado_em": flag.atualizado_em if flag else None,
        "atualizado_por": flag.atualizado_por if flag else None,
        "diagnostico": diagnostico,
    }


def alterar_modo_leitura(
    db: Session,
    *,
    ativo: bool,
    usuario_id: int,
    motivo: str,
) -> SystemFlag:
    motivo_limpo = normalize_text(motivo)
    if not motivo_limpo:
        raise ValueError("Informe o motivo para alterar o modo leitura.")

    flag = set_system_flag(
        db,
        chave=READ_ONLY_FLAG,
        valor="1" if ativo else "0",
        usuario_id=usuario_id,
        motivo=motivo_limpo,
    )
    registrar_auditoria(
        db=db,
        acao="ADMIN_ATIVAR_MODO_LEITURA" if ativo else "ADMIN_LIBERAR_MODO_LEITURA",
        usuario_id=usuario_id,
        tabela=SystemFlag.__tablename__,
        registro_id=flag.id,
        descricao="Modo leitura operacional alterado pelo administrador",
        dados_depois={"ativo": ativo, "motivo": motivo_limpo},
    )
    return flag


def is_operational_mutation_path(path: str) -> bool:
    blocked_prefixes = ("/almoxarifado", "/manipulador")
    safe_prefixes = ("/manipulador/rastrear", "/manipulador/baixa-hidrometro/buscar")
    if path.startswith(safe_prefixes):
        return False
    return path.startswith(blocked_prefixes)
