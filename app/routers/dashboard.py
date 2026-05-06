import os
from datetime import timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    AuditoriaLog,
    CarcacaMovimentacao,
    CarcacaTipoMovimento,
    CaixaHidrometro,
    CaixaStatus,
    ConferenciaItem,
    ConferenciaPecas,
    EstoquePeca,
    FeedbackOperacional,
    Hidrometro,
    HidrometroStatus,
    InstalacaoHidrometro,
    Instalador,
    InstaladorPeca,
    MovimentacaoMaterial,
    MovimentacaoPeca,
    MovimentacaoTipo,
    Solicitacao,
    SolicitacaoStatus,
    TipoPeca,
    TransferenciaEmpresaCaixa,
)
from app.security import get_usuario_atual, login_redirect_location, requer_autenticacao
from app.services.confirmacoes_instalador import (
    linhas_exportacao_confirmacoes_instalador,
    query_confirmacoes_instalador,
    resumo_confirmacoes_instalador,
)
from app.services.contexto_acoes import ADVANCED_MODE_COOKIE
from app.services.estoque_inteligente import (
    montar_dashboard_estoque_inteligente,
    registrar_auditoria_alertas_estoque,
)
from app.services.feedback_attachments import salvar_anexos_feedback
from app.services.exportacao import resposta_xlsx
from app.services.regras_caixa import quantidade_esperada_caixa
from app.ui import templates
from app.utils import format_datetime, normalize_text, parse_bool_form, parse_date_end, parse_date_start, utc_now


router = APIRouter(tags=["dashboard"])
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "False").strip().lower() == "true"

REPORT_ACCESS = {
    "estoque": {"admin"},
    "baixas": {"admin"},
    "movimentacoes": {"admin"},
    "divergencias": {"admin"},
    "instaladores": {"admin"},
    "auditoria": {"admin"},
    "caixas_completo": {"admin"},
    "carcacas": {"admin"},
    "confirmacoes_instalador": {"admin"},
    "estoque_inteligente": {"admin"},
    "portal_instaladores": {"admin"},
    "hidrometros_estoque": {"admin"},
}


def _role_value(usuario) -> str:
    return getattr(getattr(usuario, "role", None), "value", getattr(usuario, "role", ""))


def _can_access_report(usuario, report_key: str) -> bool:
    return _role_value(usuario) in REPORT_ACCESS.get(report_key, set())


def _require_report_access(usuario, report_key: str) -> None:
    if not _can_access_report(usuario, report_key):
        raise HTTPException(status_code=403, detail="Acesso negado a este relatorio.")


def _require_reports_admin(usuario) -> None:
    if _role_value(usuario) != "admin":
        raise HTTPException(status_code=403, detail="Acesso restrito ao administrador.")


def _build_report_sections(usuario):
    role = _role_value(usuario)
    operacao = []
    controle = []

    if role in {"almoxarifado", "admin"}:
        operacao.extend(
            [
                {
                    "title": "Estoque de caixas",
                    "icon": "CX",
                    "tag": "PDF + XLSX",
                    "tone": "cyan",
                    "open_href": "/almoxarifado/caixas",
                    "download_href": "/almoxarifado/caixas/exportar.xlsx",
                    "download_label": "Baixar XLSX",
                },
                {
                    "title": "Estoque de pecas",
                    "icon": "PC",
                    "tag": "PDF + XLSX",
                    "tone": "amber",
                    "open_href": "/almoxarifado/estoque",
                    "download_href": "/almoxarifado/estoque/exportar.xlsx",
                    "download_label": "Baixar XLSX",
                },
                {
                    "title": "Resumo de estoque",
                    "icon": "ES",
                    "tag": "Painel",
                    "tone": "blue",
                    "open_href": "/relatorios/estoque",
                    "download_href": None,
                    "download_label": "PDF",
                },
                {
                    "title": "Carcacas",
                    "icon": "CR",
                    "tag": "Saldo",
                    "tone": "green",
                    "open_href": "/almoxarifado/carcacas",
                    "download_href": "/almoxarifado/carcacas/exportar.xlsx" if role == "admin" else None,
                    "download_label": "Baixar XLSX" if role == "admin" else None,
                },
                {
                    "title": "Transferencias",
                    "icon": "TR",
                    "tag": "Empresa",
                    "tone": "slate",
                    "open_href": "/almoxarifado/transferencias",
                    "download_href": "/almoxarifado/transferencias/exportar.xlsx",
                    "download_label": "Baixar XLSX",
                },
            ]
        )

    if role in {"manipulador", "admin"}:
        operacao.append(
            {
                "title": "Instaladores",
                "icon": "IN",
                "tag": "PDF + XLSX",
                "tone": "green",
                "open_href": "/manipulador/instaladores",
                "download_href": "/manipulador/instaladores/exportar.xlsx",
                "download_label": "Baixar XLSX",
            }
        )
        operacao.append(
            {
                "title": "Produtividade",
                "icon": "PR",
                "tag": "Leitura" if role == "manipulador" else "XLSX",
                "tone": "blue",
                "open_href": "/produtividade",
                "download_href": "/produtividade/exportar.xlsx" if role == "admin" else None,
                "download_label": "Baixar XLSX" if role == "admin" else None,
            }
        )
        controle.append(
            {
                "title": "Baixas instaladas",
                "icon": "BX",
                "tag": "XLSX",
                "tone": "green",
                "open_href": "/relatorios/baixas",
                "download_href": "/relatorios/baixas/exportar.xlsx",
                "download_label": "Baixar XLSX",
            }
        )

    if role in {"almoxarifado", "manipulador", "admin"}:
        controle.append(
            {
                "title": "Movimentacoes",
                "icon": "MV",
                "tag": "Historico",
                "tone": "slate",
                "open_href": "/relatorios/movimentacoes",
                "download_href": None,
                "download_label": None,
            }
        )

    if role in {"manipulador", "admin"}:
        controle.append(
            {
                "title": "Divergencias",
                "icon": "DV",
                "tag": "Conferencia",
                "tone": "red",
                "open_href": "/relatorios/divergencias",
                "download_href": None,
                "download_label": None,
            }
        )

    if role == "admin":
        controle.append(
            {
                "title": "Diagnostico",
                "icon": "DX",
                "tag": "Admin",
                "tone": "red",
                "open_href": "/admin/diagnostico",
                "download_href": None,
                "download_label": None,
            }
        )
        controle.append(
            {
                "title": "Auditoria",
                "icon": "AU",
                "tag": "Admin",
                "tone": "violet",
                "open_href": "/admin/auditoria",
                "download_href": None,
                "download_label": None,
            }
        )
        controle.append(
            {
                "title": "Usuarios ativos",
                "icon": "ON",
                "tag": "Admin",
                "tone": "slate",
                "open_href": "/admin/usuarios-ativos",
                "download_href": None,
                "download_label": None,
            }
        )
        controle.append(
            {
                "title": "Exportacao geral",
                "icon": "ZIP",
                "tag": "ZIP",
                "tone": "blue",
                "open_href": "/admin/exportacao-periodica.zip",
                "download_href": "/admin/exportacao-periodica.zip",
                "download_label": "Baixar ZIP",
            }
        )
        controle.append(
            {
                "title": "Caixas completo",
                "icon": "CX",
                "tag": "Admin",
                "tone": "cyan",
                "open_href": "/relatorios/caixas-completo",
                "download_href": "/relatorios/caixas-completo/exportar.xlsx",
                "download_label": "Baixar XLSX",
            }
        )
        controle.append(
            {
                "title": "Hidrometros em estoque",
                "icon": "HD",
                "tag": "XLSX",
                "tone": "cyan",
                "open_href": "/relatorios/hidrometros-estoque",
                "download_href": "/relatorios/hidrometros-estoque/exportar.xlsx",
                "download_label": "Baixar XLSX",
            }
        )
        controle.append(
            {
                "title": "Confirmacoes instalador",
                "icon": "OK",
                "tag": "XLSX",
                "tone": "green",
                "open_href": "/relatorios/confirmacoes-instalador",
                "download_href": "/relatorios/confirmacoes-instalador/exportar.xlsx",
                "download_label": "Baixar XLSX",
            }
        )
        controle.append(
            {
                "title": "Estoque inteligente",
                "icon": "EI",
                "tag": "XLSX",
                "tone": "amber",
                "open_href": "/relatorios/estoque-inteligente",
                "download_href": "/relatorios/estoque-inteligente/exportar.xlsx",
                "download_label": "Baixar XLSX",
            }
        )
        controle.append(
            {
                "title": "Portal instaladores",
                "icon": "MB",
                "tag": "XLSX",
                "tone": "green",
                "open_href": "/relatorios/portal-instaladores",
                "download_href": "/relatorios/portal-instaladores/exportar.xlsx",
                "download_label": "Baixar XLSX",
            }
        )

    return [
        {"title": "Operacao e exportacao", "cards": operacao},
        {"title": "Controle e auditoria", "cards": controle},
    ]


def _build_baixa_movements_index(
    db: Session,
    hidrometros: list[Hidrometro],
) -> tuple[dict[int, MovimentacaoMaterial], dict[tuple[int, object], MovimentacaoMaterial]]:
    hidrometro_ids = sorted({hidrometro.id for hidrometro in hidrometros if hidrometro.id})
    caixa_ids = sorted({hidrometro.caixa_id for hidrometro in hidrometros if hidrometro.caixa_id})
    momentos = [hidrometro.instalado_em for hidrometro in hidrometros if hidrometro.instalado_em]
    if not hidrometro_ids and (not caixa_ids or not momentos):
        return {}, {}

    movimentos_query = db.query(MovimentacaoMaterial).options(
        joinedload(MovimentacaoMaterial.instalador),
        joinedload(MovimentacaoMaterial.registrado_por),
    ).filter(MovimentacaoMaterial.tipo == MovimentacaoTipo.BAIXA)

    if hidrometro_ids:
        movimentos = movimentos_query.filter(
            or_(
                MovimentacaoMaterial.hidrometro_id.in_(hidrometro_ids),
                and_(
                    MovimentacaoMaterial.caixa_id.in_(caixa_ids),
                    MovimentacaoMaterial.criado_em.in_(momentos),
                ),
            )
        ).all()
    elif caixa_ids and momentos:
        movimentos = movimentos_query.filter(
            MovimentacaoMaterial.caixa_id.in_(caixa_ids),
            MovimentacaoMaterial.criado_em.in_(momentos),
        ).all()
    else:
        movimentos = []

    by_hidrometro = {
        movimento.hidrometro_id: movimento
        for movimento in movimentos
        if movimento.hidrometro_id
    }
    by_caixa_momento = {
        (movimento.caixa_id, movimento.criado_em): movimento
        for movimento in movimentos
        if movimento.caixa_id and movimento.criado_em
    }
    return by_hidrometro, by_caixa_momento


def _build_baixa_record(
    hidrometro: Hidrometro,
    movements_by_hidrometro: dict[int, MovimentacaoMaterial],
    movements_by_caixa_momento: dict[tuple[int, object], MovimentacaoMaterial],
) -> dict[str, object]:
    movimento = movements_by_hidrometro.get(hidrometro.id)
    if movimento is None and hidrometro.caixa_id and hidrometro.instalado_em:
        movimento = movements_by_caixa_momento.get((hidrometro.caixa_id, hidrometro.instalado_em))

    instalador = hidrometro.instalador_baixa or (movimento.instalador if movimento else None)
    operador = hidrometro.baixado_por or (movimento.registrado_por if movimento else None)
    contexto_legado = movimento is not None and (hidrometro.baixado_por is None or hidrometro.instalador_baixa is None)

    return {
        "id": hidrometro.id,
        "numero_serie": hidrometro.numero_serie,
        "caixa_numero": hidrometro.caixa.numero_interno if hidrometro.caixa else "-",
        "caixa_serial": hidrometro.caixa.serial_number if hidrometro.caixa else "-",
        "instalado_em": hidrometro.instalado_em,
        "instalado_em_label": hidrometro.instalado_em.strftime("%d/%m/%Y %H:%M:%S") if hidrometro.instalado_em else "",
        "instalador_nome": instalador.nome if instalador else "Nao identificado",
        "operador_nome": operador.nome if operador else "Nao identificado",
        "observacoes": movimento.observacoes if movimento and movimento.observacoes else "",
        "contexto_legado": contexto_legado,
    }


def _listar_baixas_instaladas(db: Session) -> list[dict[str, object]]:
    hidrometros = db.query(Hidrometro).options(
        joinedload(Hidrometro.caixa),
        joinedload(Hidrometro.baixado_por),
        joinedload(Hidrometro.instalador_baixa),
    ).filter(
        Hidrometro.status == HidrometroStatus.INSTALADO
    ).order_by(
        Hidrometro.instalado_em.desc(),
        Hidrometro.id.desc(),
    ).all()

    movements_by_hidrometro, movements_by_caixa_momento = _build_baixa_movements_index(db, hidrometros)
    return [
        _build_baixa_record(hidrometro, movements_by_hidrometro, movements_by_caixa_momento)
        for hidrometro in hidrometros
    ]


def _linhas_exportacao_baixas(baixas: list[dict[str, object]]) -> list[list[object]]:
    return [
        [
            item["numero_serie"],
            item["caixa_numero"],
            item["caixa_serial"],
            item["instalador_nome"],
            item["operador_nome"],
            item["instalado_em_label"],
            item["observacoes"],
            "Historico" if item["contexto_legado"] else "Atual",
        ]
        for item in baixas
    ]


def _redirect_back(request: Request, fallback: str = "/dashboard") -> str:
    referer = (request.headers.get("referer") or "").strip()
    if not referer:
        return fallback

    parsed = urlparse(referer)
    if parsed.netloc and request.url.netloc and parsed.netloc != request.url.netloc:
        return fallback

    destino = parsed.path or fallback
    if parsed.query:
        destino = f"{destino}?{parsed.query}"
    return destino


def _parse_month_period(mes: str | None) -> tuple[object | None, object | None]:
    mes_limpo = (mes or "").strip()
    if not mes_limpo:
        return None, None
    try:
        ano, numero_mes = [int(part) for part in mes_limpo.split("-", 1)]
        if numero_mes < 1 or numero_mes > 12:
            return None, None
        inicio = parse_date_start(f"{ano:04d}-{numero_mes:02d}-01")
        proximo_ano = ano + 1 if numero_mes == 12 else ano
        proximo_mes = 1 if numero_mes == 12 else numero_mes + 1
        fim = parse_date_start(f"{proximo_ano:04d}-{proximo_mes:02d}-01")
        return inicio, fim
    except Exception:
        return None, None


def _caixa_status_from_filter(value: str | None) -> CaixaStatus | None:
    text = (value or "").strip()
    if not text:
        return None
    for status in CaixaStatus:
        if text in {status.name, status.value}:
            return status
    return None


def _periodo_label(data_inicio, data_fim) -> str:
    if data_inicio and data_fim:
        return f"{format_datetime(data_inicio)} a {format_datetime(data_fim)}"
    if data_inicio:
        return f"A partir de {format_datetime(data_inicio)}"
    if data_fim:
        return f"Ate {format_datetime(data_fim)}"
    return "Todo historico"


def _produtividade_operacional(
    db: Session,
    *,
    instalador_id: int | None = None,
    data_inicio=None,
    data_fim=None,
    status: str = "",
    tipo_peca_id: int | None = None,
) -> dict[str, object]:
    status_caixa = _caixa_status_from_filter(status)
    periodo = _periodo_label(data_inicio, data_fim)

    instalacoes_query = db.query(InstalacaoHidrometro).options(
        joinedload(InstalacaoHidrometro.instalador),
        joinedload(InstalacaoHidrometro.hidrometro),
        joinedload(InstalacaoHidrometro.caixa),
        joinedload(InstalacaoHidrometro.solicitacao),
    )
    if instalador_id:
        instalacoes_query = instalacoes_query.filter(InstalacaoHidrometro.instalador_id == instalador_id)
    if data_inicio:
        instalacoes_query = instalacoes_query.filter(InstalacaoHidrometro.data_instalacao >= data_inicio)
    if data_fim:
        instalacoes_query = instalacoes_query.filter(InstalacaoHidrometro.data_instalacao < data_fim)
    if status_caixa:
        instalacoes_query = instalacoes_query.join(CaixaHidrometro).filter(CaixaHidrometro.status == status_caixa)

    instalacoes = instalacoes_query.order_by(
        InstalacaoHidrometro.data_instalacao.desc(),
        InstalacaoHidrometro.id.desc(),
    ).all()

    pecas_query = db.query(MovimentacaoPeca).options(
        joinedload(MovimentacaoPeca.instalador),
        joinedload(MovimentacaoPeca.tipo_peca),
    ).filter(
        MovimentacaoPeca.tipo == MovimentacaoTipo.SAIDA,
        MovimentacaoPeca.instalador_id.isnot(None),
    )
    if instalador_id:
        pecas_query = pecas_query.filter(MovimentacaoPeca.instalador_id == instalador_id)
    if tipo_peca_id:
        pecas_query = pecas_query.filter(MovimentacaoPeca.tipo_peca_id == tipo_peca_id)
    if data_inicio:
        pecas_query = pecas_query.filter(MovimentacaoPeca.criado_em >= data_inicio)
    if data_fim:
        pecas_query = pecas_query.filter(MovimentacaoPeca.criado_em < data_fim)
    pecas = pecas_query.order_by(MovimentacaoPeca.criado_em.desc(), MovimentacaoPeca.id.desc()).all()

    resumo: dict[int, dict[str, object]] = {}

    def garantir(instalador: Instalador | None, instalador_id_alvo: int | None):
        chave = instalador.id if instalador else instalador_id_alvo
        if chave is None:
            return None
        if chave not in resumo:
            resumo[chave] = {
                "instalador_id": chave,
                "instalador": instalador.nome if instalador else f"Instalador #{chave}",
                "hidrometros_instalados": 0,
                "caixas_finalizadas_set": set(),
                "total_pecas": 0,
                "pecas_por_tipo": {},
                "periodo": periodo,
                "ultima_instalacao": None,
            }
        return resumo[chave]

    for instalacao in instalacoes:
        item = garantir(instalacao.instalador, instalacao.instalador_id)
        if item is None:
            continue
        item["hidrometros_instalados"] = int(item["hidrometros_instalados"]) + 1
        if instalacao.caixa and instalacao.caixa.status == CaixaStatus.INSTALADA:
            item["caixas_finalizadas_set"].add(instalacao.caixa_id)
        ultima = item["ultima_instalacao"]
        if ultima is None or instalacao.data_instalacao > ultima:
            item["ultima_instalacao"] = instalacao.data_instalacao

    for movimento in pecas:
        item = garantir(movimento.instalador, movimento.instalador_id)
        if item is None:
            continue
        nome = movimento.tipo_peca.nome if movimento.tipo_peca else f"Peca #{movimento.tipo_peca_id}"
        item["total_pecas"] = int(item["total_pecas"]) + movimento.quantidade
        pecas_por_tipo = item["pecas_por_tipo"]
        pecas_por_tipo[nome] = int(pecas_por_tipo.get(nome, 0)) + movimento.quantidade

    linhas = []
    for item in resumo.values():
        linhas.append(
            {
                **item,
                "caixas_finalizadas": len(item["caixas_finalizadas_set"]),
                "pecas_por_tipo_texto": ", ".join(
                    f"{nome}: {quantidade}" for nome, quantidade in sorted(item["pecas_por_tipo"].items())
                ) or "-",
            }
        )
    linhas.sort(key=lambda item: (item["hidrometros_instalados"], item["total_pecas"]), reverse=True)

    return {
        "linhas": linhas,
        "instalacoes": instalacoes[:200],
        "periodo": periodo,
        "kpis": {
            "hidrometros": sum(int(item["hidrometros_instalados"]) for item in linhas),
            "caixas": sum(int(item["caixas_finalizadas"]) for item in linhas),
            "pecas": sum(int(item["total_pecas"]) for item in linhas),
            "instaladores": len(linhas),
        },
    }


def _linhas_exportacao_produtividade(linhas: list[dict[str, object]]) -> list[list[object]]:
    return [
        [
            item["instalador"],
            item["hidrometros_instalados"],
            item["caixas_finalizadas"],
            item["total_pecas"],
            item["pecas_por_tipo_texto"],
            item["periodo"],
            format_datetime(item["ultima_instalacao"], with_seconds=True) if item["ultima_instalacao"] else "-",
        ]
        for item in linhas
    ]


def _listar_caixas_completo(
    db: Session,
    *,
    status: str = "",
    instalador_id: int | None = None,
    empresa_destino: str = "",
    termo: str = "",
    data_inicio=None,
    data_fim=None,
) -> tuple[list[CaixaHidrometro], dict[int, object]]:
    hidrometros_rel = getattr(CaixaHidrometro, "hidrômetros")
    query = db.query(CaixaHidrometro).options(
        joinedload(hidrometros_rel),
        joinedload(CaixaHidrometro.instalador),
        joinedload(CaixaHidrometro.regra_caixa),
        joinedload(CaixaHidrometro.movimentacoes),
    )
    status_caixa = _caixa_status_from_filter(status)
    if status_caixa:
        query = query.filter(CaixaHidrometro.status == status_caixa)
    if instalador_id:
        query = query.filter(CaixaHidrometro.instalador_id == instalador_id)
    if data_inicio:
        query = query.filter(CaixaHidrometro.criado_em >= data_inicio)
    if data_fim:
        query = query.filter(CaixaHidrometro.criado_em < data_fim)

    termo_limpo = normalize_text(termo, upper=True)
    if termo_limpo:
        hidros_caixa_ids = db.query(Hidrometro.caixa_id).filter(
            func.upper(Hidrometro.numero_serie).contains(termo_limpo)
        )
        query = query.filter(
            or_(
                func.upper(CaixaHidrometro.numero_interno).contains(termo_limpo),
                func.upper(CaixaHidrometro.serial_number).contains(termo_limpo),
                CaixaHidrometro.id.in_(hidros_caixa_ids),
            )
        )

    caixas = query.order_by(CaixaHidrometro.criado_em.desc(), CaixaHidrometro.id.desc()).all()
    links = db.query(TransferenciaEmpresaCaixa).options(
        joinedload(TransferenciaEmpresaCaixa.transferencia)
    ).all()
    transferencias = {
        link.caixa_id: link.transferencia
        for link in links
        if link.transferencia
    }
    empresa_limpa = normalize_text(empresa_destino).lower()
    if empresa_limpa:
        caixas = [
            caixa for caixa in caixas
            if empresa_limpa in normalize_text(
                transferencias.get(caixa.id).empresa_destino if transferencias.get(caixa.id) else ""
            ).lower()
        ]
    return caixas, transferencias


def _linhas_exportacao_caixas_completo(caixas: list[CaixaHidrometro], transferencias: dict[int, object]) -> list[list[object]]:
    rows = []
    for caixa in caixas:
        hidrometros = list(getattr(caixa, "hidrômetros") or [])
        transferencia = transferencias.get(caixa.id)
        rows.append(
            [
                caixa.numero_interno,
                caixa.serial_number,
                caixa.status.value,
                "SIM" if caixa.ativo else "NAO",
                quantidade_esperada_caixa(caixa),
                len(hidrometros),
                sum(1 for hidro in hidrometros if hidro.status == HidrometroStatus.INSTALADO),
                caixa.regra_caixa.quantidade_hidrometros if caixa.regra_caixa else caixa.quantidade_esperada,
                caixa.instalador.nome if caixa.instalador else "",
                transferencia.empresa_destino if transferencia else "",
                format_datetime(transferencia.data_transferencia) if transferencia else "",
                len(caixa.movimentacoes or []),
                format_datetime(caixa.criado_em),
            ]
        )
    return rows


def _listar_hidrometros_estoque(db: Session, *, termo: str = "") -> list[Hidrometro]:
    query = db.query(Hidrometro).outerjoin(CaixaHidrometro, Hidrometro.caixa_id == CaixaHidrometro.id).options(
        joinedload(Hidrometro.caixa),
        joinedload(Hidrometro.instalador_atual),
    ).filter(
        Hidrometro.status == HidrometroStatus.EM_ESTOQUE,
        Hidrometro.instalador_id == None,
        Hidrometro.em_manutencao == False,
        Hidrometro.bloqueado_por_manutencao == False,
        Hidrometro.descartado_tecnico == False,
        or_(
            Hidrometro.caixa_id == None,
            and_(
                CaixaHidrometro.ativo == True,
                CaixaHidrometro.status == CaixaStatus.EM_ESTOQUE,
            ),
        ),
    )
    termo_limpo = normalize_text(termo, upper=True)
    if termo_limpo:
        query = query.filter(
            or_(
                func.upper(Hidrometro.numero_serie).contains(termo_limpo),
                func.upper(CaixaHidrometro.numero_interno).contains(termo_limpo),
                func.upper(CaixaHidrometro.serial_number).contains(termo_limpo),
            )
        )
    return query.order_by(
        CaixaHidrometro.numero_interno.asc(),
        Hidrometro.numero_serie.asc(),
    ).all()


def _linhas_exportacao_hidrometros_estoque(hidrometros: list[Hidrometro]) -> list[list[object]]:
    rows = []
    for hidrometro in hidrometros:
        caixa = hidrometro.caixa
        rows.append(
            [
                hidrometro.numero_serie,
                caixa.numero_interno if caixa else "SOLTO",
                caixa.serial_number if caixa else "",
                caixa.status.value if caixa else "estoque_solto",
                "SIM" if caixa and caixa.ativo else ("NAO" if caixa else ""),
                hidrometro.status.value,
                hidrometro.status_operacional,
                "SIM" if hidrometro.retornou_manutencao else "NAO",
                format_datetime(hidrometro.data_retorno_manutencao, with_seconds=True) if hidrometro.data_retorno_manutencao else "",
                "SIM" if hidrometro.prioridade_reutilizacao else "NAO",
                hidrometro.origem_prioridade or "",
                format_datetime(hidrometro.criado_em, with_seconds=True),
            ]
        )
    return rows


def _filtrar_itens_estoque_inteligente(dashboard: dict[str, object], *, status: str = "todos", q: str = "") -> list[dict[str, object]]:
    filtro_status = normalize_text(status or "todos", upper=True)
    if filtro_status in {"TODOS", "ALL", ""}:
        filtro_status = "TODOS"
    busca = normalize_text(q).lower()
    itens = list(dashboard.get("itens", []))
    if filtro_status != "TODOS":
        itens = [item for item in itens if item["status"] == filtro_status]
    if busca:
        itens = [
            item for item in itens
            if busca in normalize_text(item["nome"]).lower()
            or busca in normalize_text(item.get("descricao", "")).lower()
        ]
    return itens


def _linhas_exportacao_estoque_inteligente(itens: list[dict[str, object]]) -> list[list[object]]:
    rows = []
    for item in itens:
        rows.append(
            [
                item["nome"],
                item["estoque_atual"],
                item["quantidade_maxima"],
                item["minimo_percentual"],
                round(float(item["minimo_qtd"] or 0), 2),
                round(float(item["consumo_medio_diario"]), 2) if item["consumo_medio_diario"] is not None else "Sem historico",
                round(float(item["dias_restantes"]), 2) if item["dias_restantes"] is not None else "Sem historico",
                item["status"],
                format_datetime(item["ultima_movimentacao"], with_seconds=True) if item["ultima_movimentacao"] else "",
                item["previsao"],
            ]
        )
    return rows


def _portal_instaladores_rows(db: Session) -> list[dict[str, object]]:
    instaladores = db.query(Instalador).options(joinedload(Instalador.usuario)).order_by(Instalador.nome).all()
    rows: dict[int, dict[str, object]] = {
        instalador.id: {
            "instalador_id": instalador.id,
            "nome": instalador.nome,
            "matricula": instalador.matricula,
            "usuario": instalador.usuario.email if instalador.usuario else "-",
            "ativo": instalador.ativo,
            "solicitacoes_total": 0,
            "solicitacoes_pendentes": 0,
            "solicitacoes_separadas": 0,
            "solicitacoes_entregues": 0,
            "recebimentos_pendentes": 0,
            "recebimentos_confirmados": 0,
            "recebimentos_divergentes": 0,
            "hidrometros_instalados": 0,
            "pecas_recebidas": 0,
            "carcacas_devolvidas": 0,
            "ultima_solicitacao": None,
            "ultima_entrega": None,
            "ultima_confirmacao": None,
            "status_operacional": "OK",
        }
        for instalador in instaladores
    }

    def garantir(instalador_id: int | None) -> dict[str, object] | None:
        if instalador_id is None:
            return None
        return rows.get(instalador_id)

    solicitacoes_counts = db.query(
        Solicitacao.instalador_id,
        Solicitacao.status,
        Solicitacao.recebimento_instalador_status,
        func.count(Solicitacao.id).label("total"),
    ).group_by(
        Solicitacao.instalador_id,
        Solicitacao.status,
        Solicitacao.recebimento_instalador_status,
    ).all()
    for instalador_id, status, recebimento, total in solicitacoes_counts:
        row = garantir(instalador_id)
        if row is None:
            continue
        total_int = int(total or 0)
        row["solicitacoes_total"] = int(row["solicitacoes_total"]) + total_int
        if status == SolicitacaoStatus.PENDENTE:
            row["solicitacoes_pendentes"] = int(row["solicitacoes_pendentes"]) + total_int
        elif status == SolicitacaoStatus.SEPARADA:
            row["solicitacoes_separadas"] = int(row["solicitacoes_separadas"]) + total_int
        elif status == SolicitacaoStatus.ENTREGUE:
            row["solicitacoes_entregues"] = int(row["solicitacoes_entregues"]) + total_int
            if recebimento == "confirmado":
                row["recebimentos_confirmados"] = int(row["recebimentos_confirmados"]) + total_int
            elif recebimento == "divergencia":
                row["recebimentos_divergentes"] = int(row["recebimentos_divergentes"]) + total_int
            else:
                row["recebimentos_pendentes"] = int(row["recebimentos_pendentes"]) + total_int

    for instalador_id, total in db.query(
        InstalacaoHidrometro.instalador_id,
        func.count(InstalacaoHidrometro.id),
    ).group_by(InstalacaoHidrometro.instalador_id).all():
        row = garantir(instalador_id)
        if row:
            row["hidrometros_instalados"] = int(total or 0)

    for instalador_id, total in db.query(
        MovimentacaoPeca.instalador_id,
        func.coalesce(func.sum(MovimentacaoPeca.quantidade), 0),
    ).filter(
        MovimentacaoPeca.tipo == MovimentacaoTipo.SAIDA,
        MovimentacaoPeca.instalador_id.isnot(None),
    ).group_by(MovimentacaoPeca.instalador_id).all():
        row = garantir(instalador_id)
        if row:
            row["pecas_recebidas"] = int(total or 0)

    for instalador_id, total in db.query(
        CarcacaMovimentacao.instalador_id,
        func.coalesce(func.sum(CarcacaMovimentacao.quantidade), 0),
    ).filter(
        CarcacaMovimentacao.tipo_movimento == CarcacaTipoMovimento.DEVOLUCAO_INSTALADOR,
        CarcacaMovimentacao.instalador_id.isnot(None),
    ).group_by(CarcacaMovimentacao.instalador_id).all():
        row = garantir(instalador_id)
        if row:
            row["carcacas_devolvidas"] = int(total or 0)

    for instalador_id, ultima in db.query(
        Solicitacao.instalador_id,
        func.max(Solicitacao.criado_em),
    ).group_by(Solicitacao.instalador_id).all():
        row = garantir(instalador_id)
        if row:
            row["ultima_solicitacao"] = ultima

    for instalador_id, ultima in db.query(
        Solicitacao.instalador_id,
        func.max(Solicitacao.entregue_em),
    ).filter(Solicitacao.entregue_em.isnot(None)).group_by(Solicitacao.instalador_id).all():
        row = garantir(instalador_id)
        if row:
            row["ultima_entrega"] = ultima

    for instalador_id, ultima in db.query(
        Solicitacao.instalador_id,
        func.max(Solicitacao.confirmacao_instalador_em),
    ).filter(Solicitacao.confirmacao_instalador_em.isnot(None)).group_by(Solicitacao.instalador_id).all():
        row = garantir(instalador_id)
        if row:
            row["ultima_confirmacao"] = ultima

    for row in rows.values():
        if int(row["recebimentos_divergentes"]) > 0:
            row["status_operacional"] = "DIVERGENCIA"
        elif int(row["recebimentos_pendentes"]) > 0 or int(row["solicitacoes_separadas"]) > 0:
            row["status_operacional"] = "PENDENTE"
        elif not row["ativo"]:
            row["status_operacional"] = "INATIVO"

    ordem = {"DIVERGENCIA": 0, "PENDENTE": 1, "OK": 2, "INATIVO": 3}
    return sorted(rows.values(), key=lambda row: (ordem.get(str(row["status_operacional"]), 9), str(row["nome"]).lower()))


def _linhas_exportacao_portal_instaladores(rows: list[dict[str, object]]) -> list[list[object]]:
    return [
        [
            row["nome"],
            row["matricula"],
            row["usuario"],
            "Ativo" if row["ativo"] else "Inativo",
            row["status_operacional"],
            row["solicitacoes_total"],
            row["solicitacoes_pendentes"],
            row["solicitacoes_separadas"],
            row["solicitacoes_entregues"],
            row["recebimentos_pendentes"],
            row["recebimentos_confirmados"],
            row["recebimentos_divergentes"],
            row["hidrometros_instalados"],
            row["pecas_recebidas"],
            row["carcacas_devolvidas"],
            format_datetime(row["ultima_solicitacao"], with_seconds=True) if row["ultima_solicitacao"] else "",
            format_datetime(row["ultima_entrega"], with_seconds=True) if row["ultima_entrega"] else "",
            format_datetime(row["ultima_confirmacao"], with_seconds=True) if row["ultima_confirmacao"] else "",
        ]
        for row in rows
    ]


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if usuario:
        destino = "/instalador/entregas" if _role_value(usuario) == "instalador" else "/dashboard"
        return RedirectResponse(url=destino, status_code=302)
    return RedirectResponse(url=login_redirect_location(request), status_code=302)


@router.get("/preferencias/modo-avancado")
async def alternar_modo_avancado(
    request: Request,
    ativar: int = 1,
    usuario=Depends(requer_autenticacao),
):
    response = RedirectResponse(url=_redirect_back(request), status_code=302)
    if getattr(usuario.role, "value", None) != "admin":
        response.delete_cookie(key=ADVANCED_MODE_COOKIE, path="/", samesite="lax", secure=COOKIE_SECURE)
        return response

    if ativar:
        response.set_cookie(
            key=ADVANCED_MODE_COOKIE,
            value="1",
            httponly=True,
            samesite="lax",
            secure=COOKIE_SECURE,
            path="/",
        )
    else:
        response.delete_cookie(key=ADVANCED_MODE_COOKIE, path="/", samesite="lax", secure=COOKIE_SECURE)
    return response


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    if _role_value(usuario) == "instalador":
        return RedirectResponse(url="/instalador/entregas", status_code=302)

    feedbacks = db.query(FeedbackOperacional).options(
        joinedload(FeedbackOperacional.resolvido_por),
        joinedload(FeedbackOperacional.anexos),
    ).filter(
        FeedbackOperacional.usuario_id == usuario.id
    ).order_by(
        FeedbackOperacional.criado_em.desc(),
        FeedbackOperacional.id.desc(),
    ).limit(20).all()

    return templates.TemplateResponse(
        "shared/feedback.html",
        {
            "request": request,
            "usuario": usuario,
            "feedbacks": feedbacks,
            "erro_feedback": None,
        },
    )


@router.get("/termo-responsabilidade", response_class=HTMLResponse)
async def termo_responsabilidade_page(
    request: Request,
    usuario=Depends(requer_autenticacao),
):
    return templates.TemplateResponse(
        "shared/termo_responsabilidade.html",
        {"request": request, "usuario": usuario},
    )


@router.post("/feedback")
async def enviar_feedback(
    request: Request,
    usuario=Depends(requer_autenticacao),
    db: Session = Depends(get_db),
):
    async with request.form() as form:
        categoria = str(form.get("categoria") or "")
        urgente = str(form.get("urgente") or "")
        titulo = str(form.get("titulo") or "")
        mensagem = str(form.get("mensagem") or "")
        pagina_origem = str(form.get("pagina_origem") or "")
        anexos_upload = form.getlist("anexos")

        area_limpa = _role_value(usuario)
        categoria_limpa = (categoria or "").strip().lower()
        titulo_limpo = (titulo or "").strip()
        mensagem_limpa = (mensagem or "").strip()
        pagina_limpa = (pagina_origem or "").strip()
        urgente_flag = parse_bool_form(urgente, default=False)

        if area_limpa not in {"manipulador", "almoxarifado", "admin"}:
            raise HTTPException(status_code=400, detail="Area de feedback invalida.")
        if categoria_limpa not in {"bug", "melhoria", "usabilidade"}:
            raise HTTPException(status_code=400, detail="Categoria de feedback invalida.")
        if not titulo_limpo or not mensagem_limpa:
            raise HTTPException(status_code=400, detail="Preencha titulo e mensagem do feedback.")

        feedback = FeedbackOperacional(
            usuario_id=usuario.id,
            area=area_limpa,
            categoria=categoria_limpa,
            urgente=urgente_flag,
            titulo=titulo_limpo[:160],
            mensagem=mensagem_limpa,
            pagina_origem=pagina_limpa[:255] if pagina_limpa else request.url.path,
            criado_em=utc_now(),
            atualizado_em=utc_now(),
        )
        db.add(feedback)
        db.flush()

        try:
            anexos = await salvar_anexos_feedback(feedback, anexos_upload)
        except ValueError as exc:
            db.rollback()
            feedbacks = db.query(FeedbackOperacional).options(
                joinedload(FeedbackOperacional.resolvido_por),
                joinedload(FeedbackOperacional.anexos),
            ).filter(
                FeedbackOperacional.usuario_id == usuario.id
            ).order_by(
                FeedbackOperacional.criado_em.desc(),
                FeedbackOperacional.id.desc(),
            ).limit(20).all()
            return templates.TemplateResponse(
                "shared/feedback.html",
                {
                    "request": request,
                    "usuario": usuario,
                    "feedbacks": feedbacks,
                    "erro_feedback": str(exc),
                },
                status_code=400,
            )
        for anexo in anexos:
            db.add(anexo)

        from app.services.auditoria import registrar_auditoria

        registrar_auditoria(
            db=db,
            acao="FEEDBACK_CRIADO",
            usuario_id=usuario.id,
            tabela=FeedbackOperacional.__tablename__,
            registro_id=feedback.id,
            descricao=f"Feedback criado por {usuario.nome}",
            dados_depois={
                "area": feedback.area,
                "categoria": feedback.categoria,
                "urgente": feedback.urgente,
                "titulo": feedback.titulo,
                "pagina_origem": feedback.pagina_origem,
                "anexos": len(anexos),
            },
        )
        db.commit()
        return RedirectResponse(url="/feedback?sucesso=1", status_code=302)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    if _role_value(usuario) == "instalador":
        return RedirectResponse(url="/instalador/entregas", status_code=302)

    total_caixas_estoque = db.query(CaixaHidrometro).filter(
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.EM_ESTOQUE,
    ).count()
    total_caixas_entregues = db.query(CaixaHidrometro).filter(
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.ENTREGUE,
    ).count()
    total_hidros_estoque = db.query(Hidrometro).join(CaixaHidrometro).filter(
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.EM_ESTOQUE,
        Hidrometro.status == HidrometroStatus.EM_ESTOQUE,
    ).count()
    total_hidros_instalador = db.query(Hidrometro).join(CaixaHidrometro).filter(
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.ENTREGUE,
        Hidrometro.status == HidrometroStatus.COM_INSTALADOR,
    ).count()
    total_hidros_instalados = db.query(Hidrometro).join(CaixaHidrometro).filter(
        CaixaHidrometro.ativo == True,
        Hidrometro.status == HidrometroStatus.INSTALADO,
    ).count()
    total_instaladores = db.query(Instalador).filter(Instalador.ativo == True).count()

    solicitacoes_pendentes = db.query(Solicitacao).filter(Solicitacao.status == SolicitacaoStatus.PENDENTE).count()
    solicitacoes_separadas = db.query(Solicitacao).filter(Solicitacao.status == SolicitacaoStatus.SEPARADA).count()

    estoques = db.query(EstoquePeca).options(joinedload(EstoquePeca.tipo_peca)).all()
    alertas_estoque = [estoque for estoque in estoques if estoque.abaixo_minimo]
    conferencias_com_divergencia = db.query(ConferenciaPecas).filter(ConferenciaPecas.tem_divergencia == True).count()

    movimentacoes_recentes = db.query(MovimentacaoMaterial).options(
        joinedload(MovimentacaoMaterial.caixa),
        joinedload(MovimentacaoMaterial.instalador),
        joinedload(MovimentacaoMaterial.registrado_por),
    ).order_by(MovimentacaoMaterial.criado_em.desc(), MovimentacaoMaterial.id.desc()).limit(10).all()

    return templates.TemplateResponse(
        "shared/dashboard.html",
        {
            "request": request,
            "usuario": usuario,
            "total_caixas_estoque": total_caixas_estoque,
            "total_caixas_entregues": total_caixas_entregues,
            "total_hidros_estoque": total_hidros_estoque,
            "total_hidros_instalador": total_hidros_instalador,
            "total_hidros_instalados": total_hidros_instalados,
            "alertas_estoque": alertas_estoque,
            "solicitacoes_pendentes": solicitacoes_pendentes,
            "solicitacoes_separadas": solicitacoes_separadas,
            "total_instaladores": total_instaladores,
            "movimentacoes_recentes": movimentacoes_recentes,
            "conferencias_com_divergencia": conferencias_com_divergencia,
        },
    )


@router.get("/produtividade", response_class=HTMLResponse)
async def produtividade_page(
    request: Request,
    instalador_id: int = 0,
    data_inicio: str = "",
    data_fim: str = "",
    mes: str = "",
    status: str = "",
    tipo_peca_id: int = 0,
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    if _role_value(usuario) not in {"admin", "manipulador"}:
        raise HTTPException(status_code=403, detail="Acesso negado a produtividade.")

    inicio_mes, fim_mes = _parse_month_period(mes)
    try:
        inicio = inicio_mes or parse_date_start(data_inicio)
        fim = fim_mes or parse_date_end(data_fim)
    except ValueError:
        raise HTTPException(status_code=400, detail="Periodo informado invalido.")

    produtividade = _produtividade_operacional(
        db,
        instalador_id=instalador_id or None,
        data_inicio=inicio,
        data_fim=fim,
        status=status,
        tipo_peca_id=tipo_peca_id or None,
    )
    instaladores = db.query(Instalador).filter(Instalador.ativo == True).order_by(Instalador.nome).all()
    tipos_pecas = db.query(TipoPeca).filter(TipoPeca.ativo == True).order_by(TipoPeca.nome).all()

    return templates.TemplateResponse(
        "shared/produtividade.html",
        {
            "request": request,
            "usuario": usuario,
            "produtividade": produtividade,
            "instaladores": instaladores,
            "tipos_pecas": tipos_pecas,
            "CaixaStatus": CaixaStatus,
            "filtros": {
                "instalador_id": instalador_id,
                "data_inicio": data_inicio,
                "data_fim": data_fim,
                "mes": mes,
                "status": status,
                "tipo_peca_id": tipo_peca_id,
            },
            "pode_exportar": _role_value(usuario) == "admin",
        },
    )


@router.get("/produtividade/exportar.xlsx")
async def exportar_produtividade_xlsx(
    request: Request,
    instalador_id: int = 0,
    data_inicio: str = "",
    data_fim: str = "",
    mes: str = "",
    status: str = "",
    tipo_peca_id: int = 0,
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    if _role_value(usuario) != "admin":
        raise HTTPException(status_code=403, detail="Exportacao restrita ao administrador.")

    inicio_mes, fim_mes = _parse_month_period(mes)
    try:
        inicio = inicio_mes or parse_date_start(data_inicio)
        fim = fim_mes or parse_date_end(data_fim)
    except ValueError:
        raise HTTPException(status_code=400, detail="Periodo informado invalido.")

    produtividade = _produtividade_operacional(
        db,
        instalador_id=instalador_id or None,
        data_inicio=inicio,
        data_fim=fim,
        status=status,
        tipo_peca_id=tipo_peca_id or None,
    )
    return resposta_xlsx(
        filename="produtividade_instaladores.xlsx",
        sheet_name="Produtividade",
        headers=[
            "Instalador",
            "Hidrometros instalados",
            "Caixas finalizadas",
            "Pecas usadas",
            "Pecas por tipo",
            "Periodo",
            "Ultima instalacao",
        ],
        rows=_linhas_exportacao_produtividade(produtividade["linhas"]),
    )


@router.get("/relatorios", response_class=HTMLResponse)
async def relatorios(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_reports_admin(usuario)

    total_caixas = db.query(CaixaHidrometro).filter(CaixaHidrometro.ativo == True).count()
    caixas_em_campo = db.query(CaixaHidrometro).filter(
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.ENTREGUE,
    ).count()
    solicitacoes_abertas = db.query(Solicitacao).filter(
        Solicitacao.status.in_([SolicitacaoStatus.PENDENTE, SolicitacaoStatus.SEPARADA])
    ).count()
    alertas_peca = db.query(EstoquePeca).options(joinedload(EstoquePeca.tipo_peca)).all()
    total_alertas = sum(1 for estoque in alertas_peca if estoque.abaixo_minimo)
    total_divergencias = db.query(ConferenciaPecas).filter(ConferenciaPecas.tem_divergencia == True).count()
    total_movimentacoes = db.query(MovimentacaoMaterial).count()
    total_baixas = db.query(Hidrometro).filter(Hidrometro.status == HidrometroStatus.INSTALADO).count()
    desde_24h = utc_now() - timedelta(hours=24)
    total_criticos_24h = db.query(AuditoriaLog).filter(
        AuditoriaLog.criado_em >= desde_24h,
        AuditoriaLog.severidade == "CRITICO",
    ).count()
    total_suspeitos_24h = db.query(AuditoriaLog).filter(
        AuditoriaLog.criado_em >= desde_24h,
        AuditoriaLog.severidade == "SUSPEITO",
    ).count()
    alertas_relatorio = [
        {
            "titulo": "Estoque critico",
            "valor": total_alertas,
            "href": "/almoxarifado/estoque-inteligente?status=CRITICO",
            "tom": "red" if total_alertas else "green",
            "detalhe": "Itens abaixo do minimo",
        },
        {
            "titulo": "Auditoria critica",
            "valor": total_criticos_24h,
            "href": "/admin/auditoria?severidade=CRITICO",
            "tom": "red" if total_criticos_24h else "green",
            "detalhe": "Eventos criticos em 24h",
        },
        {
            "titulo": "Eventos suspeitos",
            "valor": total_suspeitos_24h,
            "href": "/admin/auditoria?severidade=SUSPEITO",
            "tom": "amber" if total_suspeitos_24h else "green",
            "detalhe": "Seguranca e bloqueios",
        },
        {
            "titulo": "Divergencias",
            "valor": total_divergencias,
            "href": "/relatorios/divergencias",
            "tom": "amber" if total_divergencias else "green",
            "detalhe": "Conferencia de pecas",
        },
    ]
    total_confirmacoes_instalador = db.query(Solicitacao).filter(
        Solicitacao.status == SolicitacaoStatus.ENTREGUE,
        Solicitacao.recebimento_instalador_status == "confirmado",
    ).count()

    return templates.TemplateResponse(
        "shared/relatorios.html",
        {
            "request": request,
            "usuario": usuario,
            "report_sections": _build_report_sections(usuario),
            "alertas_relatorio": alertas_relatorio,
            "relatorio_kpis": {
                "total_caixas": total_caixas,
                "caixas_em_campo": caixas_em_campo,
                "solicitacoes_abertas": solicitacoes_abertas,
                "alertas_peca": total_alertas + total_criticos_24h + total_suspeitos_24h,
                "divergencias": total_divergencias,
                "movimentacoes": total_movimentacoes,
                "total_baixas": total_baixas,
                "confirmacoes_instalador": total_confirmacoes_instalador,
            },
        },
    )


@router.get("/relatorios/estoque", response_class=HTMLResponse)
async def relatorio_estoque(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "estoque")

    estoques = db.query(EstoquePeca).options(joinedload(EstoquePeca.tipo_peca)).order_by(EstoquePeca.tipo_peca_id).all()
    caixas = db.query(CaixaHidrometro).options(
        joinedload(getattr(CaixaHidrometro, "hidrômetros")),
        joinedload(CaixaHidrometro.instalador),
    ).filter(CaixaHidrometro.ativo == True).order_by(CaixaHidrometro.numero_interno).all()
    caixas_por_status = {
        "em_estoque": db.query(CaixaHidrometro).filter(
            CaixaHidrometro.ativo == True,
            CaixaHidrometro.status == CaixaStatus.EM_ESTOQUE,
        ).count(),
        "entregues": db.query(CaixaHidrometro).filter(
            CaixaHidrometro.ativo == True,
            CaixaHidrometro.status == CaixaStatus.ENTREGUE,
        ).count(),
    }
    return templates.TemplateResponse(
        "shared/relatorio_estoque.html",
        {
            "request": request,
            "usuario": usuario,
            "estoques": estoques,
            "caixas_por_status": caixas_por_status,
            "caixas": caixas,
        },
    )


@router.get("/relatorios/caixas-completo", response_class=HTMLResponse)
async def relatorio_caixas_completo(
    request: Request,
    status: str = "",
    instalador_id: int = 0,
    empresa_destino: str = "",
    termo: str = "",
    data_inicio: str = "",
    data_fim: str = "",
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "caixas_completo")

    try:
        inicio = parse_date_start(data_inicio)
        fim = parse_date_end(data_fim)
    except ValueError:
        raise HTTPException(status_code=400, detail="Periodo informado invalido.")

    caixas, transferencias = _listar_caixas_completo(
        db,
        status=status,
        instalador_id=instalador_id or None,
        empresa_destino=empresa_destino,
        termo=termo,
        data_inicio=inicio,
        data_fim=fim,
    )
    instaladores = db.query(Instalador).order_by(Instalador.nome).all()

    return templates.TemplateResponse(
        "shared/relatorio_caixas_completo.html",
        {
            "request": request,
            "usuario": usuario,
            "caixas": caixas,
            "transferencias": transferencias,
            "instaladores": instaladores,
            "CaixaStatus": CaixaStatus,
            "filtros": {
                "status": status,
                "instalador_id": instalador_id,
                "empresa_destino": empresa_destino,
                "termo": termo,
                "data_inicio": data_inicio,
                "data_fim": data_fim,
            },
            "kpis": {
                "total": len(caixas),
                "estoque": sum(1 for c in caixas if c.status == CaixaStatus.EM_ESTOQUE),
                "instaladas": sum(1 for c in caixas if c.status == CaixaStatus.INSTALADA),
                "transferidas": sum(1 for c in caixas if c.status == CaixaStatus.TRANSFERIDA_OUTRA_EMPRESA),
            },
        },
    )


@router.get("/relatorios/caixas-completo/exportar.xlsx")
async def exportar_caixas_completo_xlsx(
    request: Request,
    status: str = "",
    instalador_id: int = 0,
    empresa_destino: str = "",
    termo: str = "",
    data_inicio: str = "",
    data_fim: str = "",
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "caixas_completo")

    try:
        inicio = parse_date_start(data_inicio)
        fim = parse_date_end(data_fim)
    except ValueError:
        raise HTTPException(status_code=400, detail="Periodo informado invalido.")

    caixas, transferencias = _listar_caixas_completo(
        db,
        status=status,
        instalador_id=instalador_id or None,
        empresa_destino=empresa_destino,
        termo=termo,
        data_inicio=inicio,
        data_fim=fim,
    )
    return resposta_xlsx(
        filename="relatorio_completo_caixas.xlsx",
        sheet_name="Caixas",
        headers=[
            "Caixa",
            "Serial",
            "Status",
            "Ativa",
            "Quantidade esperada",
            "Hidrometros",
            "Instalados",
            "Regra da caixa",
            "Instalador atual",
            "Empresa destino",
            "Data transferencia",
            "Movimentacoes",
            "Cadastro",
        ],
        rows=_linhas_exportacao_caixas_completo(caixas, transferencias),
    )


@router.get("/relatorios/hidrometros-estoque", response_class=HTMLResponse)
async def relatorio_hidrometros_estoque(
    request: Request,
    termo: str = "",
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "hidrometros_estoque")

    hidrometros = _listar_hidrometros_estoque(db, termo=termo)
    em_caixa = [hidro for hidro in hidrometros if hidro.caixa_id]
    soltos = [hidro for hidro in hidrometros if not hidro.caixa_id]
    return templates.TemplateResponse(
        "shared/relatorio_hidrometros_estoque.html",
        {
            "request": request,
            "usuario": usuario,
            "hidrometros": hidrometros,
            "filtros": {"termo": termo},
            "kpis": {
                "total": len(hidrometros),
                "em_caixa": len(em_caixa),
                "soltos": len(soltos),
                "prioridade": sum(1 for hidro in hidrometros if hidro.prioridade_reutilizacao),
            },
        },
    )


@router.get("/relatorios/hidrometros-estoque/exportar.xlsx")
async def exportar_hidrometros_estoque_xlsx(
    request: Request,
    termo: str = "",
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "hidrometros_estoque")

    hidrometros = _listar_hidrometros_estoque(db, termo=termo)
    return resposta_xlsx(
        filename="hidrometros_em_estoque.xlsx",
        sheet_name="Hidrometros em estoque",
        headers=[
            "Numero do hidrometro",
            "Caixa",
            "Serial da caixa",
            "Status da caixa",
            "Caixa ativa",
            "Status do hidrometro",
            "Status operacional",
            "Retornou manutencao",
            "Data retorno manutencao",
            "Prioridade reutilizacao",
            "Origem prioridade",
            "Cadastro",
        ],
        rows=_linhas_exportacao_hidrometros_estoque(hidrometros),
    )


@router.get("/relatorios/confirmacoes-instalador", response_class=HTMLResponse)
async def relatorio_confirmacoes_instalador(
    request: Request,
    status_recebimento: str = "",
    instalador_id: int = 0,
    data_inicio: str = "",
    data_fim: str = "",
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "confirmacoes_instalador")

    try:
        inicio = parse_date_start(data_inicio)
        fim = parse_date_end(data_fim)
    except ValueError:
        raise HTTPException(status_code=400, detail="Periodo informado invalido.")

    solicitacoes = query_confirmacoes_instalador(
        db,
        status_recebimento=status_recebimento,
        instalador_id=instalador_id or None,
        data_inicio=inicio,
        data_fim=fim,
    ).all()
    instaladores = db.query(Instalador).filter(Instalador.ativo == True).order_by(Instalador.nome).all()
    return templates.TemplateResponse(
        "shared/relatorio_confirmacoes_instalador.html",
        {
            "request": request,
            "usuario": usuario,
            "solicitacoes": solicitacoes,
            "instaladores": instaladores,
            "resumo": resumo_confirmacoes_instalador(solicitacoes),
            "filtros": {
                "status_recebimento": status_recebimento,
                "instalador_id": instalador_id,
                "data_inicio": data_inicio,
                "data_fim": data_fim,
            },
        },
    )


@router.get("/relatorios/confirmacoes-instalador/exportar.xlsx")
async def exportar_relatorio_confirmacoes_instalador_xlsx(
    request: Request,
    status_recebimento: str = "",
    instalador_id: int = 0,
    data_inicio: str = "",
    data_fim: str = "",
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "confirmacoes_instalador")

    try:
        inicio = parse_date_start(data_inicio)
        fim = parse_date_end(data_fim)
    except ValueError:
        raise HTTPException(status_code=400, detail="Periodo informado invalido.")

    solicitacoes = query_confirmacoes_instalador(
        db,
        status_recebimento=status_recebimento,
        instalador_id=instalador_id or None,
        data_inicio=inicio,
        data_fim=fim,
    ).all()
    return resposta_xlsx(
        filename="relatorio_confirmacoes_instalador.xlsx",
        sheet_name="Confirmacoes",
        headers=[
            "Solicitacao",
            "Status recebimento",
            "Instalador",
            "Matricula",
            "Criada em",
            "Entregue em",
            "Respondida em",
            "Manipulador",
            "Almoxarifado",
            "Usuario confirmador",
            "Caixas",
            "Hidrometros",
            "Pecas",
            "Motivo divergencia",
            "Observacoes",
        ],
        rows=linhas_exportacao_confirmacoes_instalador(solicitacoes),
    )


@router.get("/relatorios/estoque-inteligente", response_class=HTMLResponse)
async def relatorio_estoque_inteligente(
    request: Request,
    status: str = "todos",
    q: str = "",
    dias: int = 30,
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "estoque_inteligente")

    dias_periodo = min(max(int(dias or 30), 7), 90)
    dashboard = montar_dashboard_estoque_inteligente(db, dias_periodo=dias_periodo)
    registrar_auditoria_alertas_estoque(db, dashboard, usuario_id=usuario.id)
    db.commit()
    itens = _filtrar_itens_estoque_inteligente(dashboard, status=status, q=q)

    return templates.TemplateResponse(
        "shared/relatorio_estoque_inteligente.html",
        {
            "request": request,
            "usuario": usuario,
            "dashboard": dashboard,
            "itens": itens,
            "filtros": {
                "status": status,
                "q": q,
                "dias": dias_periodo,
            },
        },
    )


@router.get("/relatorios/estoque-inteligente/exportar.xlsx")
async def exportar_relatorio_estoque_inteligente_xlsx(
    request: Request,
    status: str = "todos",
    q: str = "",
    dias: int = 30,
    db: Session = Depends(get_db),
):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "estoque_inteligente")

    dias_periodo = min(max(int(dias or 30), 7), 90)
    dashboard = montar_dashboard_estoque_inteligente(db, dias_periodo=dias_periodo)
    itens = _filtrar_itens_estoque_inteligente(dashboard, status=status, q=q)
    return resposta_xlsx(
        filename="relatorio_estoque_inteligente.xlsx",
        sheet_name="Estoque inteligente",
        headers=[
            "Peca",
            "Estoque atual",
            "Quantidade maxima",
            "Estoque minimo %",
            "Estoque minimo qtd",
            "Consumo medio diario",
            "Dias restantes",
            "Status inteligente",
            "Ultima movimentacao",
            "Previsao",
        ],
        rows=_linhas_exportacao_estoque_inteligente(itens),
    )


@router.get("/relatorios/portal-instaladores", response_class=HTMLResponse)
async def relatorio_portal_instaladores(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "portal_instaladores")

    rows = _portal_instaladores_rows(db)
    return templates.TemplateResponse(
        "shared/relatorio_portal_instaladores.html",
        {
            "request": request,
            "usuario": usuario,
            "linhas": rows,
            "kpis": {
                "instaladores": len(rows),
                "pendentes": sum(1 for row in rows if row["status_operacional"] == "PENDENTE"),
                "divergencias": sum(1 for row in rows if row["status_operacional"] == "DIVERGENCIA"),
                "confirmados": sum(int(row["recebimentos_confirmados"]) for row in rows),
            },
        },
    )


@router.get("/relatorios/portal-instaladores/exportar.xlsx")
async def exportar_relatorio_portal_instaladores_xlsx(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "portal_instaladores")

    rows = _portal_instaladores_rows(db)
    return resposta_xlsx(
        filename="relatorio_portal_instaladores.xlsx",
        sheet_name="Portal instaladores",
        headers=[
            "Instalador",
            "Matricula",
            "Usuario vinculado",
            "Cadastro",
            "Status operacional",
            "Solicitacoes total",
            "Solicitacoes pendentes",
            "Solicitacoes separadas",
            "Solicitacoes entregues",
            "Recebimentos pendentes",
            "Recebimentos confirmados",
            "Recebimentos divergentes",
            "Hidrometros instalados",
            "Pecas recebidas",
            "Carcacas devolvidas",
            "Ultima solicitacao",
            "Ultima entrega",
            "Ultima confirmacao",
        ],
        rows=_linhas_exportacao_portal_instaladores(rows),
    )


@router.get("/relatorios/instaladores", response_class=HTMLResponse)
async def relatorio_instaladores(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "instaladores")

    instaladores = db.query(Instalador).options(
        joinedload(getattr(Instalador, "hidrômetros")).joinedload(Hidrometro.caixa),
        joinedload(Instalador.pecas_posse).joinedload(InstaladorPeca.tipo_peca),
    ).filter(Instalador.ativo == True).order_by(Instalador.nome).all()

    return templates.TemplateResponse(
        "shared/relatorio_instaladores.html",
        {"request": request, "usuario": usuario, "instaladores": instaladores},
    )


@router.get("/relatorios/baixas", response_class=HTMLResponse)
async def relatorio_baixas(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "baixas")

    baixas = _listar_baixas_instaladas(db)
    limite_24h = utc_now() - timedelta(hours=24)
    caixas_com_baixa = len({item["caixa_numero"] for item in baixas if item["caixa_numero"] != "-"})
    instaladores_com_baixa = len({item["instalador_nome"] for item in baixas if item["instalador_nome"] != "Nao identificado"})
    baixas_24h = sum(1 for item in baixas if item["instalado_em"] and item["instalado_em"] >= limite_24h)

    return templates.TemplateResponse(
        "shared/relatorio_baixas.html",
        {
            "request": request,
            "usuario": usuario,
            "baixas": baixas,
            "baixas_kpis": {
                "total": len(baixas),
                "caixas": caixas_com_baixa,
                "instaladores": instaladores_com_baixa,
                "ultimas_24h": baixas_24h,
            },
        },
    )


@router.get("/relatorios/baixas/exportar.xlsx")
async def exportar_relatorio_baixas_xlsx(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "baixas")

    baixas = _listar_baixas_instaladas(db)
    return resposta_xlsx(
        filename="relatorio_baixas.xlsx",
        sheet_name="Baixas",
        headers=[
            "Hidrometro",
            "Caixa",
            "Serial da caixa",
            "Instalador",
            "Baixa por",
            "Instalado em",
            "Observacoes",
            "Origem do contexto",
        ],
        rows=_linhas_exportacao_baixas(baixas),
    )


@router.get("/relatorios/movimentacoes", response_class=HTMLResponse)
async def relatorio_movimentacoes(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "movimentacoes")

    movimentacoes = db.query(MovimentacaoMaterial).options(
        joinedload(MovimentacaoMaterial.caixa),
        joinedload(MovimentacaoMaterial.instalador),
        joinedload(MovimentacaoMaterial.registrado_por),
    ).order_by(MovimentacaoMaterial.criado_em.desc(), MovimentacaoMaterial.id.desc()).limit(200).all()

    return templates.TemplateResponse(
        "shared/relatorio_movimentacoes.html",
        {"request": request, "usuario": usuario, "movimentacoes": movimentacoes},
    )


@router.get("/relatorios/divergencias", response_class=HTMLResponse)
async def relatorio_divergencias(request: Request, db: Session = Depends(get_db)):
    usuario = await get_usuario_atual(request, db)
    if not usuario:
        return RedirectResponse(url=login_redirect_location(request), status_code=302)
    _require_report_access(usuario, "divergencias")

    conferencias = db.query(ConferenciaPecas).options(
        joinedload(ConferenciaPecas.instalador),
        joinedload(ConferenciaPecas.responsavel),
        joinedload(ConferenciaPecas.itens).joinedload(ConferenciaItem.tipo_peca),
    ).filter(ConferenciaPecas.tem_divergencia == True).order_by(
        ConferenciaPecas.data_conferencia.desc(),
        ConferenciaPecas.id.desc(),
    ).all()

    return templates.TemplateResponse(
        "shared/relatorio_divergencias.html",
        {"request": request, "usuario": usuario, "conferencias": conferencias},
    )
