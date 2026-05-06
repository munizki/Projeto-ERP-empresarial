import json
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import AuditoriaLog, Usuario
from app.services.request_context import get_current_request
from app.utils import get_request_audit_meta, safe_jsonable, utc_now


CRITICAL_PREFIXES = (
    "ADMIN_",
    "OVERRIDE_",
)
CRITICAL_TOKENS = (
    "REVERSE",
    "REVERSAO",
    "REVERTER",
    "BACKUP_FALHA",
    "MODO_LEITURA",
    "EXCLUIR",
    "DELETE",
    "LIMPEZA",
)
SUSPICIOUS_ACTIONS = {
    "LOGIN_FALHA",
    "LOGIN_BLOQUEADO",
    "LOGIN_USUARIO_INATIVO",
    "REQUISICAO_BLOQUEADA",
    "VALIDACAO_OPERACIONAL_BLOQUEADA",
    "OPERACAO_SUSPEITA_SEQUENCIA",
}


def classificar_evento(acao: str, severidade: str | None = None, categoria: str | None = None) -> tuple[str, str]:
    acao_limpa = str(acao or "").strip().upper()
    severidade_limpa = str(severidade or "").strip().upper()
    categoria_limpa = str(categoria or "").strip().upper()

    if severidade_limpa not in {"NORMAL", "SUSPEITO", "CRITICO"}:
        if acao_limpa in SUSPICIOUS_ACTIONS:
            severidade_limpa = "SUSPEITO"
        elif acao_limpa.startswith(CRITICAL_PREFIXES) or any(token in acao_limpa for token in CRITICAL_TOKENS):
            severidade_limpa = "CRITICO"
        else:
            severidade_limpa = "NORMAL"

    if not categoria_limpa:
        if acao_limpa.startswith("LOGIN") or "BLOQUEADA" in acao_limpa or "SUSPEITA" in acao_limpa:
            categoria_limpa = "SEGURANCA"
        elif acao_limpa.startswith("ADMIN") or acao_limpa.startswith("OVERRIDE"):
            categoria_limpa = "ADMINISTRATIVO"
        else:
            categoria_limpa = "OPERACIONAL"

    return severidade_limpa, categoria_limpa


def registrar_auditoria(
    db: Session,
    acao: str,
    usuario_id: Optional[int] = None,
    tabela: Optional[str] = None,
    registro_id: Optional[int] = None,
    descricao: Optional[str] = None,
    dados_antes: Optional[dict] = None,
    dados_depois: Optional[dict] = None,
    ip: Optional[str] = None,
    severidade: Optional[str] = None,
    categoria: Optional[str] = None,
    request: Request | None = None,
    resultado: Optional[str] = None,
    request_id: Optional[str] = None,
):
    try:
        severidade_final, categoria_final = classificar_evento(acao, severidade, categoria)
        resultado_final = str(resultado or ("FALHA" if severidade_final == "CRITICO" and "FALHA" in str(acao).upper() else "SUCESSO")).strip().upper()
        request_atual = request or get_current_request()
        audit_meta = get_request_audit_meta(request_atual)
        usuario_snapshot = None
        if usuario_id:
            usuario_snapshot = db.get(Usuario, usuario_id)
        ip_final = ip or audit_meta["ip_cliente"]
        registro = AuditoriaLog(
            usuario_id=usuario_id,
            usuario_nome=usuario_snapshot.nome if usuario_snapshot else None,
            usuario_email=usuario_snapshot.email if usuario_snapshot else None,
            usuario_perfil=getattr(getattr(usuario_snapshot, "role", None), "value", getattr(usuario_snapshot, "role", None)) if usuario_snapshot else None,
            acao=str(acao).strip() or "ACAO_NAO_INFORMADA",
            tabela=tabela,
            registro_id=registro_id,
            descricao=json.dumps(safe_jsonable(descricao), ensure_ascii=False) if isinstance(descricao, (dict, list, tuple, set)) else (str(descricao) if descricao is not None else None),
            severidade=severidade_final,
            categoria=categoria_final,
            resultado=resultado_final or "SUCESSO",
            dados_antes=json.loads(json.dumps(safe_jsonable(dados_antes), ensure_ascii=False)) if dados_antes else None,
            dados_depois=json.loads(json.dumps(safe_jsonable(dados_depois), ensure_ascii=False)) if dados_depois else None,
            ip=ip_final,
            ip_cliente=audit_meta["ip_cliente"],
            ip_conexao=audit_meta["ip_conexao"],
            x_forwarded_for=audit_meta["x_forwarded_for"],
            x_real_ip=audit_meta["x_real_ip"],
            user_agent=audit_meta["user_agent"],
            request_id=request_id,
            criado_em=utc_now(),
        )

        db.add(registro)

    except Exception as e:
        raise RuntimeError(f"Falha ao registrar auditoria: {e}") from e
        
