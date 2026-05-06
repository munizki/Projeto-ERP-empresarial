from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.models import (
    CaixaHidrometro,
    CaixaStatus,
    EstoquePeca,
    Hidrometro,
    HidrometroStatus,
    Instalador,
    Solicitacao,
    SolicitacaoStatus,
    UserRole,
)
from app.services.hidrometros import mensagem_baixa_hidrometro


ADVANCED_MODE_COOKIE = "modo_avancado"

STATUS_LABELS = {
    CaixaStatus.EM_ESTOQUE.value: "Em estoque",
    CaixaStatus.ENTREGUE.value: "Entregue",
    CaixaStatus.DEVOLVIDA.value: "Devolvida",
    CaixaStatus.INSTALADA.value: "Instalada",
    CaixaStatus.TRANSFERIDA_OUTRA_EMPRESA.value: "Transferida",
    CaixaStatus.CANCELADA.value: "Cancelada",
    HidrometroStatus.EM_ESTOQUE.value: "Em estoque",
    HidrometroStatus.COM_INSTALADOR.value: "Com instalador",
    HidrometroStatus.INSTALADO.value: "Instalado",
    HidrometroStatus.EM_MANUTENCAO.value: "Em manutencao",
    HidrometroStatus.ENVIADO_ASSISTENCIA.value: "Enviado assistencia",
    HidrometroStatus.RETORNADO_MANUTENCAO.value: "Retornado manutencao",
    HidrometroStatus.DESCARTADO_TECNICO.value: "Descartado tecnico",
    SolicitacaoStatus.PENDENTE.value: "Pendente",
    SolicitacaoStatus.SEPARADA.value: "Separada",
    SolicitacaoStatus.ENTREGUE.value: "Entregue",
    SolicitacaoStatus.CANCELADA.value: "Cancelada",
}

CAIXA_HIDROMETROS_REL = next(
    (name for name in CaixaHidrometro.__mapper__.relationships.keys() if "hidr" in name.lower()),
    None,
)
INSTALADOR_HIDROMETROS_REL = next(
    (name for name in Instalador.__mapper__.relationships.keys() if "hidr" in name.lower()),
    None,
)


def is_admin(usuario: Any | None) -> bool:
    return bool(usuario and getattr(getattr(usuario, "role", None), "value", None) == UserRole.ADMIN.value)


def has_role(usuario: Any | None, *roles: str) -> bool:
    role = getattr(getattr(usuario, "role", None), "value", None)
    return bool(role and role in roles)


def can_use_advanced_mode(request: Any | None, usuario: Any | None) -> bool:
    if not request or not is_admin(usuario):
        return False
    return request.cookies.get(ADVANCED_MODE_COOKIE) == "1"


def humanize_status(value: Any) -> str:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip()
    if not text:
        return "-"
    return STATUS_LABELS.get(text, text.replace("_", " ").title())


def get_caixa_hidrometros(caixa: CaixaHidrometro | None) -> list[Hidrometro]:
    if not caixa or not CAIXA_HIDROMETROS_REL:
        return []
    return list(getattr(caixa, CAIXA_HIDROMETROS_REL) or [])


def get_instalador_hidrometros(instalador: Instalador | None) -> list[Hidrometro]:
    if not instalador or not INSTALADOR_HIDROMETROS_REL:
        return []
    return list(getattr(instalador, INSTALADOR_HIDROMETROS_REL) or [])


def _action(
    label: str,
    *,
    href: str | None = None,
    tone: str = "outline",
    reason: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    return {
        "label": label,
        "href": href,
        "tone": tone,
        "reason": reason,
        "note": note,
    }


def caixa_action_bundle(
    caixa: CaixaHidrometro,
    usuario: Any | None,
    advanced_mode: bool = False,
) -> dict[str, Any]:
    hidrometros = get_caixa_hidrometros(caixa)
    pendentes = [hidro for hidro in hidrometros if hidro.status == HidrometroStatus.COM_INSTALADOR]
    instalados = [hidro for hidro in hidrometros if hidro.status == HidrometroStatus.INSTALADO]

    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    advanced: list[dict[str, Any]] = []

    if not getattr(caixa, "ativo", True):
        summary = "Caixa arquivada. Ela sai do fluxo operacional padrao, mas continua preservada para historico."
        blocked.append(
            _action(
                "Movimentar caixa",
                reason="Esta caixa esta inativa e foi mantida apenas para rastreabilidade e auditoria.",
            )
        )
    elif caixa.status == CaixaStatus.EM_ESTOQUE:
        summary = "Caixa pronta para entrar em uma solicitacao. A entrega so acontece depois da separacao."
        if has_role(usuario, UserRole.ALMOXARIFADO.value, UserRole.ADMIN.value):
            primary.append(_action("Ir para solicitacoes", href="/almoxarifado/solicitacoes", tone="warning"))
        if has_role(usuario, UserRole.MANIPULADOR.value, UserRole.ADMIN.value):
            secondary.append(_action("Nova solicitacao", href="/manipulador/solicitacoes/nova", tone="primary"))
        if has_role(usuario, UserRole.ALMOXARIFADO.value, UserRole.ADMIN.value):
            secondary.append(_action("Transferir empresa", href="/almoxarifado/transferencias/nova", tone="outline"))
        blocked.append(
            _action(
                "Entregar caixa",
                reason="Disponivel somente depois que a caixa for vinculada e separada em uma solicitacao.",
            )
        )
    elif caixa.status == CaixaStatus.ENTREGUE:
        instalador_nome = caixa.instalador.nome if caixa.instalador else "instalador nao identificado"
        summary = f"Caixa em posse de {instalador_nome}. A baixa e feita por hidrometro, nao pela caixa inteira."
        if caixa.instalador and has_role(usuario, UserRole.MANIPULADOR.value, UserRole.ADMIN.value):
            primary.append(
                _action(
                    "Acompanhar instalador",
                    href=f"/manipulador/instaladores/{caixa.instalador_id}",
                    tone="info",
                )
            )
        elif has_role(usuario, UserRole.ALMOXARIFADO.value, UserRole.ADMIN.value):
            primary.append(_action("Ver fila operacional", href="/almoxarifado/solicitacoes", tone="secondary"))

        if pendentes and has_role(usuario, UserRole.MANIPULADOR.value, UserRole.ADMIN.value):
            next_hidro = pendentes[0]
            secondary.append(
                _action(
                    "Registrar baixa do proximo",
                    href=f"/manipulador/baixa-hidrometro?numero_serie={quote(next_hidro.numero_serie)}",
                    tone="success",
                    note=f"{len(pendentes)} pendente(s)",
                )
            )
        elif has_role(usuario, UserRole.MANIPULADOR.value, UserRole.ADMIN.value):
            blocked.append(
                _action(
                    "Registrar baixa",
                    reason="Todos os hidrometros desta caixa ja foram instalados. Consulte apenas o historico.",
                )
            )

        if has_role(usuario, UserRole.MANIPULADOR.value, UserRole.ADMIN.value):
            secondary.append(
                _action(
                    "Rastrear um hidrometro",
                    href="/manipulador/rastrear",
                    tone="outline",
                )
            )
    elif caixa.status == CaixaStatus.INSTALADA:
        summary = "Caixa finalizada. Ela saiu da lista ativa do instalador e permanece nos historicos e relatorios."
        primary.append(_action("Ver historico", href=f"/almoxarifado/caixas/{caixa.id}", tone="info"))
        blocked.append(
            _action(
                "Movimentar caixa",
                reason="A caixa ja foi instalada. Reversao ou correcao fica restrita ao ADMIN.",
            )
        )
    elif caixa.status == CaixaStatus.TRANSFERIDA_OUTRA_EMPRESA:
        summary = "Caixa transferida para outra empresa. Nao fica disponivel para estoque, separacao ou entrega."
        primary.append(_action("Ver historico", href=f"/almoxarifado/caixas/{caixa.id}", tone="info"))
        blocked.append(
            _action(
                "Separar caixa",
                reason="Caixa transferida nao pode voltar ao fluxo operacional sem acao administrativa.",
            )
        )
    elif caixa.status == CaixaStatus.CANCELADA:
        summary = "Caixa cancelada. O historico permanece preservado para auditoria."
        blocked.append(
            _action(
                "Movimentar caixa",
                reason="Caixa cancelada fica fora do fluxo operacional padrao.",
            )
        )
    else:
        summary = "Caixa fora do fluxo padrao. Consulte o historico antes de qualquer nova movimentacao."
        blocked.append(
            _action(
                "Movimentar caixa",
                reason="Esta caixa esta fora do fluxo operacional padrao e exige revisao do historico.",
            )
        )

    if advanced_mode and is_admin(usuario):
        if caixa.instalador_id:
            advanced.append(
                _action(
                    "Editar instalador",
                    href=f"/admin/instaladores/{caixa.instalador_id}/editar",
                    tone="outline",
                )
            )
        advanced.append(
            _action(
                "Consultar auditoria",
                href="/admin/auditoria?acao=CAIXA",
                tone="outline",
            )
        )

    return {
        "summary": summary,
        "primary": primary,
        "secondary": secondary,
        "blocked": blocked,
        "advanced": advanced,
        "counts": {
            "hidrometros": len(hidrometros),
            "pendentes": len(pendentes),
            "instalados": len(instalados),
        },
    }


def hidrometro_action_bundle(
    hidrometro: Hidrometro,
    usuario: Any | None,
    advanced_mode: bool = False,
) -> dict[str, Any]:
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    advanced: list[dict[str, Any]] = []

    pode_operar_hidrometro = has_role(usuario, UserRole.MANIPULADOR.value, UserRole.ADMIN.value)
    rastrear_href = f"/manipulador/rastrear?numero_serie={quote(hidrometro.numero_serie)}"
    baixa_href = f"/manipulador/baixa-hidrometro?numero_serie={quote(hidrometro.numero_serie)}"

    if getattr(hidrometro, "descartado_tecnico", False) or hidrometro.status == HidrometroStatus.DESCARTADO_TECNICO:
        summary = "Hidrometro descartado tecnicamente. Consulta liberada, reutilizacao bloqueada permanentemente."
        blocked.append(_action("Registrar baixa", reason=mensagem_baixa_hidrometro(hidrometro)))
    elif getattr(hidrometro, "em_manutencao", False) or hidrometro.status in {
        HidrometroStatus.EM_MANUTENCAO,
        HidrometroStatus.ENVIADO_ASSISTENCIA,
        HidrometroStatus.RETORNADO_MANUTENCAO,
    }:
        summary = "Hidrometro fora do fluxo operacional por manutencao."
        blocked.append(_action("Registrar baixa", reason=mensagem_baixa_hidrometro(hidrometro)))
        if has_role(usuario, UserRole.ALMOXARIFADO.value, UserRole.ADMIN.value):
            primary.append(_action("Ver manutencao", href="/almoxarifado/manutencao", tone="warning"))
    elif hidrometro.status == HidrometroStatus.COM_INSTALADOR:
        summary = "Hidrometro pronto para registrar a instalacao em campo."
        if pode_operar_hidrometro:
            primary.append(_action("Registrar baixa", href=baixa_href, tone="success"))
            secondary.append(_action("Rastrear historico", href=rastrear_href, tone="outline"))
    elif hidrometro.status == HidrometroStatus.EM_ESTOQUE:
        if getattr(hidrometro, "prioridade_reutilizacao", False):
            summary = "Hidrometro retornou da assistencia. Priorize reutilizacao ao montar uma nova caixa."
        else:
            summary = "Hidrometro ainda em estoque. Primeiro ele precisa sair para um instalador."
        if pode_operar_hidrometro:
            primary.append(_action("Rastrear historico", href=rastrear_href, tone="outline"))
        blocked.append(_action("Registrar baixa", reason=mensagem_baixa_hidrometro(hidrometro)))
    else:
        summary = "Hidrometro ja instalado. A partir daqui o foco e consulta e auditoria."
        if pode_operar_hidrometro:
            primary.append(_action("Ver historico", href=rastrear_href, tone="info"))
        blocked.append(_action("Registrar baixa", reason=mensagem_baixa_hidrometro(hidrometro)))

    if hidrometro.caixa_id and has_role(usuario, UserRole.ALMOXARIFADO.value, UserRole.ADMIN.value):
        secondary.append(
            _action(
                "Abrir caixa",
                href=f"/almoxarifado/caixas/{hidrometro.caixa_id}",
                tone="outline",
            )
        )

    if (
        has_role(usuario, UserRole.ALMOXARIFADO.value, UserRole.ADMIN.value)
        and not getattr(hidrometro, "em_manutencao", False)
        and not getattr(hidrometro, "descartado_tecnico", False)
        and hidrometro.status != HidrometroStatus.INSTALADO
    ):
        secondary.append(
            _action(
                "Enviar para manutencao",
                href=f"/almoxarifado/hidrometros/{hidrometro.id}/manutencao",
                tone="warning",
            )
        )

    if advanced_mode and is_admin(usuario):
        if hidrometro.status != HidrometroStatus.INSTALADO:
            advanced.append(
                _action(
                    "Baixa administrativa",
                    href=f"/admin/hidrometros/{hidrometro.id}/override-baixa",
                    tone="warning",
                    note="Exige justificativa e dupla confirmacao",
                )
            )
        else:
            advanced.append(
                _action(
                    "Baixa administrativa",
                    reason="Indisponivel porque este hidrometro ja esta instalado.",
                )
            )
            advanced.append(
                _action(
                    "Reverter baixa instalada",
                    href=f"/admin/hidrometros/{hidrometro.id}/reverter-baixa",
                    tone="danger",
                    note="Remove a baixa e restaura o contexto do instalador",
                )
            )

    return {
        "summary": summary,
        "primary": primary,
        "secondary": secondary,
        "blocked": blocked,
        "advanced": advanced,
    }


def solicitacao_action_bundle(
    solicitacao: Solicitacao,
    usuario: Any | None,
    advanced_mode: bool = False,
) -> dict[str, Any]:
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    advanced: list[dict[str, Any]] = []

    separacao_href = f"/almoxarifado/solicitacoes/{solicitacao.id}/separar"
    entrega_href = f"/almoxarifado/solicitacoes/{solicitacao.id}/entregar"
    instalador_href = f"/manipulador/instaladores/{solicitacao.instalador_id}"

    if solicitacao.status == SolicitacaoStatus.PENDENTE:
        summary = "Aguardando separacao do almoxarifado antes de qualquer entrega."
        if has_role(usuario, UserRole.ALMOXARIFADO.value, UserRole.ADMIN.value):
            primary.append(_action("Separar", href=separacao_href, tone="warning"))
        blocked.append(
            _action(
                "Entregar",
                reason="Disponivel apenas depois que todas as caixas forem separadas e conferidas.",
            )
        )
    elif solicitacao.status == SolicitacaoStatus.SEPARADA:
        summary = "Caixas separadas. Falta apenas a conferencia final para concluir a entrega."
        if has_role(usuario, UserRole.ALMOXARIFADO.value, UserRole.ADMIN.value):
            primary.append(_action("Entregar", href=entrega_href, tone="success"))
            secondary.append(_action("Revisar separacao", href=separacao_href, tone="outline"))
    elif solicitacao.status == SolicitacaoStatus.ENTREGUE:
        summary = "Entrega concluida. Agora o acompanhamento segue com o instalador e as baixas de campo."
        if has_role(usuario, UserRole.MANIPULADOR.value, UserRole.ADMIN.value):
            primary.append(_action("Acompanhar instalador", href=instalador_href, tone="info"))
        blocked.append(_action("Separar", reason="Fluxo concluido. Esta solicitacao ja foi entregue."))
    else:
        summary = "Solicitacao encerrada fora do fluxo operacional ativo."
        blocked.append(_action("Processar solicitacao", reason="Solicitacao cancelada."))

    if has_role(usuario, UserRole.MANIPULADOR.value, UserRole.ADMIN.value):
        secondary.append(_action("Ver instalador", href=instalador_href, tone="outline"))

    if advanced_mode and is_admin(usuario):
        advanced.append(_action("Consultar auditoria", href="/admin/auditoria?acao=SOLICITACAO", tone="outline"))
        if solicitacao.status == SolicitacaoStatus.ENTREGUE:
            advanced.append(
                _action(
                    "Reverter entrega",
                    href=f"/admin/solicitacoes/{solicitacao.id}/reverter-entrega?next_path=/admin/reversoes",
                    tone="danger",
                    note="Devolve caixas e pecas para um contexto seguro",
                )
            )

    return {
        "summary": summary,
        "primary": primary,
        "secondary": secondary,
        "blocked": blocked,
        "advanced": advanced,
    }


def estoque_action_bundle(
    estoque: EstoquePeca,
    usuario: Any | None,
    advanced_mode: bool = False,
) -> dict[str, Any]:
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    advanced: list[dict[str, Any]] = []

    tipo = estoque.tipo_peca
    if not tipo:
        return {
            "summary": "Tipo de peca nao localizado.",
            "primary": primary,
            "secondary": secondary,
            "blocked": [_action("Registrar entrada", reason="Indisponivel sem um tipo de peca valido.")],
            "advanced": advanced,
        }

    if estoque.abaixo_minimo:
        summary = "Estoque abaixo do minimo configurado. Recomendado registrar nova entrada."
        primary.append(_action("Registrar entrada", href="/almoxarifado/pecas/entrada", tone="success"))
    else:
        summary = "Estoque dentro do nivel esperado. Entradas seguem disponiveis quando necessario."
        secondary.append(_action("Registrar entrada", href="/almoxarifado/pecas/entrada", tone="outline"))

    if not tipo.ativo:
        blocked.append(
            _action(
                "Usar em novas solicitacoes",
                reason="Este tipo de peca esta inativo e nao deve ser exposto no fluxo operacional padrao.",
            )
        )

    if advanced_mode and is_admin(usuario):
        advanced.append(_action("Editar tipo", href=f"/admin/pecas/{tipo.id}/editar", tone="outline"))

    return {
        "summary": summary,
        "primary": primary,
        "secondary": secondary,
        "blocked": blocked,
        "advanced": advanced,
    }
