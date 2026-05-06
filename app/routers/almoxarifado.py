from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    CaixaHidrometro,
    CaixaStatus,
    CarcacaMovimentacao,
    CarcacaTipoMovimento,
    EstoquePeca,
    Hidrometro,
    HidrometroManutencao,
    HidrometroStatus,
    Instalador,
    MovimentacaoMaterial,
    MovimentacaoPeca,
    MovimentacaoTipo,
    Solicitacao,
    SolicitacaoItemCaixa,
    SolicitacaoItemPeca,
    SolicitacaoStatus,
    TipoPeca,
    TransferenciaEmpresa,
    TransferenciaEmpresaCaixa,
    TransferenciaEmpresaPeca,
)
from app.security import requer_role
from app.services.auditoria import registrar_auditoria
from app.services.confirmacoes_instalador import (
    linhas_exportacao_confirmacoes_instalador,
    query_confirmacoes_instalador,
    resumo_confirmacoes_instalador,
)
from app.services.contexto_acoes import can_use_advanced_mode, get_caixa_hidrometros
from app.services.estoque import (
    carregar_solicitacao_operacional,
    confirmar_entrega_solicitacao,
    listar_caixas_disponiveis,
    separar_solicitacao as executar_separacao,
    validar_estoque_pecas,
)
from app.services.estoque_inteligente import (
    montar_dashboard_estoque_inteligente,
    registrar_auditoria_alertas_estoque,
)
from app.services.exportacao import resposta_xlsx
from app.services.manutencao_hidrometros import (
    DECISAO_CONTINUAR_MANUTENCAO,
    DECISAO_DESCARTAR,
    DECISAO_VOLTAR_CAIXA_ORIGEM,
    DECISAO_VOLTAR_ESTOQUE,
    abrir_manutencao_hidrometro,
    descartar_hidrometro_manutencao,
    enviar_assistencia,
    hidrometros_disponiveis_almoxarifado,
    caixa_origem_disponivel_para_retorno,
    linhas_exportacao_manutencao,
    listar_hidrometros_soltos_disponiveis,
    manutencoes_caixa,
    registrar_retorno_assistencia,
    remover_prioridade_reutilizacao,
    resumo_manutencao_hidrometros,
    vincular_hidrometro_solto_a_caixa,
)
from app.services.operacoes_empresariais import (
    registrar_devolucao_carcacas,
    registrar_recolhimento_carcacas,
    registrar_transferencia_empresa,
    resumo_carcacas,
    saldo_carcacas,
)
from app.services.protecao_operacional import registrar_bloqueio_operacional
from app.services.regras_caixa import obter_regra_caixa_ativa, quantidade_esperada_caixa
from app.ui import templates
from app.utils import format_datetime, get_request_ip, normalize_text, parse_bool_form, parse_date_end, parse_date_start, utc_now


router = APIRouter(prefix="/almoxarifado", tags=["almoxarifado"])
alm_dep = Depends(requer_role("almoxarifado", "admin"))
admin_dep = Depends(requer_role("admin"))
CAIXA_HIDROMETROS_REL = next(
    (name for name in CaixaHidrometro.__mapper__.relationships.keys() if "hidr" in name.lower()),
    None,
)
CAIXA_HIDROMETROS_ATTR = getattr(CaixaHidrometro, CAIXA_HIDROMETROS_REL) if CAIXA_HIDROMETROS_REL else None


def _render_caixa_form(
    request: Request,
    usuario,
    db: Session,
    erro: str | None = None,
    status_code: int = 200,
):
    regra_caixa = obter_regra_caixa_ativa(db, criar_padrao=True)
    hidrometros_soltos = listar_hidrometros_soltos_disponiveis(db, limite=80)
    return templates.TemplateResponse(
        "almoxarifado/caixa_form.html",
        {
            "request": request,
            "usuario": usuario,
            "erro": erro,
            "regra_caixa": regra_caixa,
            "quantidade_esperada": regra_caixa.quantidade_hidrometros,
            "hidrometros_soltos": hidrometros_soltos,
        },
        status_code=status_code,
    )


def _render_caixa_edicao_form(
    request: Request,
    usuario,
    caixa: CaixaHidrometro,
    erro: str | None = None,
    status_code: int = 200,
):
    return templates.TemplateResponse(
        "almoxarifado/caixa_editar.html",
        {"request": request, "usuario": usuario, "caixa": caixa, "erro": erro},
        status_code=status_code,
    )


def _carregar_caixa(db: Session, caixa_id: int) -> CaixaHidrometro | None:
    return db.query(CaixaHidrometro).options(
        joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(CaixaHidrometro.instalador),
        joinedload(CaixaHidrometro.movimentacoes).joinedload(MovimentacaoMaterial.instalador),
        joinedload(CaixaHidrometro.movimentacoes).joinedload(MovimentacaoMaterial.registrado_por),
    ).filter(CaixaHidrometro.id == caixa_id).first()


def _carregar_manutencao(db: Session, manutencao_id: int) -> HidrometroManutencao | None:
    return db.query(HidrometroManutencao).options(
        joinedload(HidrometroManutencao.hidrometro),
        joinedload(HidrometroManutencao.caixa_origem),
        joinedload(HidrometroManutencao.instalador_origem),
        joinedload(HidrometroManutencao.criado_por_usuario),
    ).filter(HidrometroManutencao.id == manutencao_id).first()


def _normalizar_series_caixa(series_brutas: list[str]) -> list[str]:
    return [normalize_text(serie, upper=True) for serie in series_brutas]


def _carregar_solicitacao_ou_404(db: Session, solicitacao_id: int) -> Solicitacao:
    solicitacao = carregar_solicitacao_operacional(db, solicitacao_id)
    if not solicitacao:
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")
    return solicitacao


def _query_caixas_listagem(db: Session, *, incluir_inativas: bool = False):
    query = db.query(CaixaHidrometro).options(
        joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(CaixaHidrometro.instalador),
    )
    if not incluir_inativas:
        query = query.filter(CaixaHidrometro.ativo == True)
    return query


def _caixa_reservada_em_separacao(db: Session, caixa_id: int) -> bool:
    return db.query(SolicitacaoItemCaixa).join(Solicitacao).filter(
        SolicitacaoItemCaixa.caixa_id == caixa_id,
        Solicitacao.status == SolicitacaoStatus.SEPARADA,
    ).first() is not None


def _linhas_exportacao_caixas(caixas: list[CaixaHidrometro]) -> list[list[object]]:
    rows: list[list[object]] = []
    for caixa in caixas:
        hidrometros = get_caixa_hidrometros(caixa)
        rows.append(
            [
                caixa.numero_interno,
                caixa.serial_number,
                caixa.status.value,
                "SIM" if caixa.ativo else "NAO",
                caixa.instalador.nome if caixa.instalador else "",
                quantidade_esperada_caixa(caixa),
                len(hidrometros),
                sum(1 for hidro in hidrometros if hidro.status == HidrometroStatus.COM_INSTALADOR),
                sum(1 for hidro in hidrometros if hidro.status == HidrometroStatus.INSTALADO),
                format_datetime(caixa.criado_em),
            ]
        )
    return rows


def _linhas_exportacao_estoque(estoques: list[EstoquePeca]) -> list[list[object]]:
    rows: list[list[object]] = []
    for estoque in estoques:
        if not estoque.tipo_peca or not estoque.tipo_peca.ativo:
            continue
        rows.append(
            [
                estoque.tipo_peca.nome,
                estoque.tipo_peca.unidade_medida,
                estoque.quantidade_atual,
                estoque.quantidade_maxima,
                round(estoque.percentual_atual, 2),
                estoque.tipo_peca.estoque_minimo_percentual,
                "BAIXO" if estoque.abaixo_minimo else "OK",
                format_datetime(estoque.atualizado_em),
            ]
        )
    return rows


def _data_operacional(value: str | None):
    try:
        return parse_date_start(value) if value else utc_now()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data informada invalida.")


def _linhas_exportacao_carcacas(movimentos: list[CarcacaMovimentacao]) -> list[list[object]]:
    rows: list[list[object]] = []
    saldo = 0
    for movimento in sorted(movimentos, key=lambda item: (item.data_movimento, item.id)):
        if movimento.tipo_movimento in {CarcacaTipoMovimento.DEVOLUCAO_INSTALADOR, CarcacaTipoMovimento.AJUSTE_ADMIN}:
            saldo += movimento.quantidade
        else:
            saldo -= movimento.quantidade
        rows.append(
            [
                format_datetime(movimento.data_movimento),
                movimento.tipo_movimento.value,
                movimento.instalador.nome if movimento.instalador else "",
                movimento.quantidade,
                saldo,
                movimento.documento_referencia or "",
                movimento.usuario.nome if movimento.usuario else "",
                movimento.observacao or "",
                format_datetime(movimento.criado_em, with_seconds=True),
            ]
        )
    return rows


def _linhas_exportacao_transferencias(transferencias: list[TransferenciaEmpresa]) -> list[list[object]]:
    rows: list[list[object]] = []
    for transferencia in transferencias:
        caixas = ", ".join(
            item.caixa.numero_interno for item in transferencia.caixas if item.caixa
        )
        pecas = ", ".join(
            f"{item.tipo_peca.nome}: {item.quantidade}" for item in transferencia.pecas if item.tipo_peca
        )
        rows.append(
            [
                transferencia.id,
                transferencia.empresa_destino,
                format_datetime(transferencia.data_transferencia),
                transferencia.responsavel,
                caixas,
                pecas,
                transferencia.documento_referencia or "",
                transferencia.usuario.nome if transferencia.usuario else "",
                transferencia.observacao or "",
                format_datetime(transferencia.criado_em, with_seconds=True),
            ]
        )
    return rows


def _render_carcacas(
    request: Request,
    usuario,
    db: Session,
    erro: str | None = None,
    status_code: int = 200,
):
    movimentos = db.query(CarcacaMovimentacao).options(
        joinedload(CarcacaMovimentacao.instalador),
        joinedload(CarcacaMovimentacao.usuario),
    ).order_by(CarcacaMovimentacao.data_movimento.desc(), CarcacaMovimentacao.id.desc()).limit(300).all()
    instaladores = db.query(Instalador).filter(Instalador.ativo == True).order_by(Instalador.nome).all()
    return templates.TemplateResponse(
        "almoxarifado/carcacas.html",
        {
            "request": request,
            "usuario": usuario,
            "movimentos": movimentos,
            "instaladores": instaladores,
            "resumo": resumo_carcacas(db),
            "erro": erro,
        },
        status_code=status_code,
    )


def _render_transferencia_form(
    request: Request,
    usuario,
    db: Session,
    erro: str | None = None,
    status_code: int = 200,
):
    caixas = listar_caixas_disponiveis(db)
    tipos = db.query(TipoPeca).options(joinedload(TipoPeca.estoque)).filter(
        TipoPeca.ativo == True
    ).order_by(TipoPeca.nome).all()
    return templates.TemplateResponse(
        "almoxarifado/transferencia_form.html",
        {
            "request": request,
            "usuario": usuario,
            "caixas": caixas,
            "tipos": tipos,
            "erro": erro,
        },
        status_code=status_code,
    )


@router.get("/caixas", response_class=HTMLResponse)
async def listar_caixas(
    request: Request,
    status_filtro: str = "",
    mostrar_inativas: int = 0,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    incluir_inativas = bool(mostrar_inativas and can_use_advanced_mode(request, usuario))
    query = _query_caixas_listagem(db, incluir_inativas=incluir_inativas)
    if status_filtro:
        query = query.filter(CaixaHidrometro.status == status_filtro)

    caixas = query.order_by(CaixaHidrometro.criado_em.desc(), CaixaHidrometro.id.desc()).all()
    total_ativas = db.query(CaixaHidrometro).filter(CaixaHidrometro.ativo == True).count()
    total_inativas = db.query(CaixaHidrometro).filter(CaixaHidrometro.ativo == False).count()

    return templates.TemplateResponse(
        "almoxarifado/caixas.html",
        {
            "request": request,
            "usuario": usuario,
            "caixas": caixas,
            "status_filtro": status_filtro,
            "mostrar_inativas": incluir_inativas,
            "CaixaStatus": CaixaStatus,
            "total_caixas_ativas": total_ativas,
            "total_caixas_inativas": total_inativas,
        },
    )


@router.get("/caixas/exportar.xlsx")
async def exportar_caixas_xlsx(
    request: Request,
    status_filtro: str = "",
    mostrar_inativas: int = 0,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    incluir_inativas = bool(mostrar_inativas and can_use_advanced_mode(request, usuario))
    query = _query_caixas_listagem(db, incluir_inativas=incluir_inativas)
    if status_filtro:
        query = query.filter(CaixaHidrometro.status == status_filtro)
    caixas = query.order_by(CaixaHidrometro.numero_interno).all()

    return resposta_xlsx(
        filename="estoque_caixas.xlsx",
        sheet_name="Caixas",
        headers=[
            "Numero interno",
            "Serial",
            "Status",
            "Ativa",
            "Instalador",
            "Quantidade esperada",
            "Hidrometros",
            "Pendentes",
            "Instalados",
            "Cadastro",
        ],
        rows=_linhas_exportacao_caixas(caixas),
    )


@router.get("/caixas/nova", response_class=HTMLResponse)
async def nova_caixa_page(request: Request, usuario=alm_dep, db: Session = Depends(get_db)):
    return _render_caixa_form(request, usuario, db)


@router.post("/caixas/nova")
async def criar_caixa(
    request: Request,
    serial_number: str = Form(...),
    numero_interno: str = Form(...),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    serial_limpo = normalize_text(serial_number, upper=True)
    numero_interno_limpo = normalize_text(numero_interno, upper=True)
    regra_caixa = obter_regra_caixa_ativa(db, criar_padrao=True)
    quantidade_esperada = regra_caixa.quantidade_hidrometros
    form = await request.form()
    series = _normalizar_series_caixa(
        [str(form.get(f"hidrometro_{index}", "")) for index in range(1, quantidade_esperada + 1)]
    )

    if db.query(CaixaHidrometro).filter(CaixaHidrometro.serial_number == serial_limpo).first():
        return _render_caixa_form(request, usuario, db, erro="Serial Number ja cadastrado.", status_code=400)

    if db.query(CaixaHidrometro).filter(CaixaHidrometro.numero_interno == numero_interno_limpo).first():
        return _render_caixa_form(request, usuario, db, erro="Numero interno ja cadastrado.", status_code=400)

    if any(not serie for serie in series):
        return _render_caixa_form(
            request,
            usuario,
            db,
            erro=f"Todos os {quantidade_esperada} hidrometros sao obrigatorios.",
            status_code=400,
        )

    if len(series) != quantidade_esperada:
        return _render_caixa_form(
            request,
            usuario,
            db,
            erro=f"A caixa precisa ter exatamente {quantidade_esperada} hidrometros.",
            status_code=400,
        )

    if len(set(series)) != quantidade_esperada:
        return _render_caixa_form(request, usuario, db, erro="Ha hidrometros duplicados na mesma caixa.", status_code=400)

    hidros_existentes = db.query(Hidrometro).filter(Hidrometro.numero_serie.in_(series)).all()
    hidros_por_serie = {hidro.numero_serie: hidro for hidro in hidros_existentes}
    indisponiveis = [
        hidro.numero_serie
        for hidro in hidros_existentes
        if not (
            hidro.status == HidrometroStatus.EM_ESTOQUE
            and hidro.caixa_id is None
            and hidro.instalador_id is None
            and not hidro.em_manutencao
            and not hidro.bloqueado_por_manutencao
            and not hidro.descartado_tecnico
        )
    ]
    if indisponiveis:
        return _render_caixa_form(
            request,
            usuario,
            db,
            erro=f"Ja existem hidrometros cadastrados e indisponiveis com estas series: {', '.join(sorted(indisponiveis))}.",
            status_code=400,
        )

    caixa = CaixaHidrometro(
        serial_number=serial_limpo,
        numero_interno=numero_interno_limpo,
        status=CaixaStatus.EM_ESTOQUE,
        ativo=True,
        regra_caixa_id=regra_caixa.id,
        quantidade_esperada=quantidade_esperada,
        criado_em=utc_now(),
        atualizado_em=utc_now(),
    )
    db.add(caixa)
    db.flush()

    reutilizados: list[str] = []
    for serie in series:
        hidrometro_existente = hidros_por_serie.get(serie)
        if hidrometro_existente:
            vincular_hidrometro_solto_a_caixa(db, hidrometro_existente, caixa, usuario_id=usuario.id, request=request)
            reutilizados.append(serie)
            continue
        db.add(
            Hidrometro(
                numero_serie=serie,
                caixa_id=caixa.id,
                status=HidrometroStatus.EM_ESTOQUE,
                status_operacional="DISPONIVEL",
                criado_em=utc_now(),
                atualizado_em=utc_now(),
            )
        )

    registrar_auditoria(
        db=db,
        acao="CREATE_CAIXA",
        usuario_id=usuario.id,
        tabela="caixas_hidrometros",
        registro_id=caixa.id,
        descricao=f"Caixa cadastrada: {caixa.numero_interno}",
        dados_depois={
            "numero_interno": caixa.numero_interno,
            "serial_number": caixa.serial_number,
            "regra_caixa_id": regra_caixa.id,
            "quantidade_esperada": quantidade_esperada,
            "hidrometros": series,
            "hidrometros_reutilizados_manutencao": reutilizados,
        },
    )
    db.commit()
    return RedirectResponse(url="/almoxarifado/caixas?sucesso=1", status_code=302)


@router.get("/caixas/{caixa_id}/editar", response_class=HTMLResponse)
async def editar_caixa_page(
    request: Request,
    caixa_id: int,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    caixa = _carregar_caixa(db, caixa_id)
    if not caixa:
        raise HTTPException(status_code=404, detail="Caixa nao encontrada.")
    return _render_caixa_edicao_form(request, usuario, caixa)


@router.post("/caixas/{caixa_id}/editar")
async def editar_caixa(
    request: Request,
    caixa_id: int,
    serial_number: str = Form(...),
    numero_interno: str = Form(...),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    caixa = _carregar_caixa(db, caixa_id)
    if not caixa:
        raise HTTPException(status_code=404, detail="Caixa nao encontrada.")

    if caixa.movimentacoes:
        return _render_caixa_edicao_form(
            request,
            usuario,
            caixa,
            erro="Esta caixa ja possui movimentacoes. Use o historico e a limpeza admin para casos excepcionais.",
            status_code=400,
        )

    if caixa.status != CaixaStatus.EM_ESTOQUE:
        return _render_caixa_edicao_form(
            request,
            usuario,
            caixa,
            erro="A edicao direta so fica disponivel para caixas ainda em estoque.",
            status_code=400,
        )

    serial_limpo = normalize_text(serial_number, upper=True)
    numero_interno_limpo = normalize_text(numero_interno, upper=True)

    serial_existente = db.query(CaixaHidrometro).filter(
        CaixaHidrometro.serial_number == serial_limpo,
        CaixaHidrometro.id != caixa.id,
    ).first()
    if serial_existente:
        caixa.serial_number = serial_limpo
        caixa.numero_interno = numero_interno_limpo
        return _render_caixa_edicao_form(
            request,
            usuario,
            caixa,
            erro="Ja existe outra caixa com este serial number.",
            status_code=400,
        )

    numero_existente = db.query(CaixaHidrometro).filter(
        CaixaHidrometro.numero_interno == numero_interno_limpo,
        CaixaHidrometro.id != caixa.id,
    ).first()
    if numero_existente:
        caixa.serial_number = serial_limpo
        caixa.numero_interno = numero_interno_limpo
        return _render_caixa_edicao_form(
            request,
            usuario,
            caixa,
            erro="Ja existe outra caixa com este numero interno.",
            status_code=400,
        )

    dados_antes = {
        "serial_number": caixa.serial_number,
        "numero_interno": caixa.numero_interno,
        "status": caixa.status.value,
        "ativo": caixa.ativo,
    }

    caixa.serial_number = serial_limpo
    caixa.numero_interno = numero_interno_limpo
    caixa.atualizado_em = utc_now()

    registrar_auditoria(
        db=db,
        acao="UPDATE_CAIXA",
        usuario_id=usuario.id,
        tabela="caixas_hidrometros",
        registro_id=caixa.id,
        descricao=f"Caixa editada: {caixa.numero_interno}",
        dados_antes=dados_antes,
        dados_depois={
            "serial_number": caixa.serial_number,
            "numero_interno": caixa.numero_interno,
            "status": caixa.status.value,
            "ativo": caixa.ativo,
        },
    )
    db.commit()
    return RedirectResponse(url=f"/almoxarifado/caixas/{caixa.id}?edicao=1", status_code=302)


@router.post("/caixas/{caixa_id}/remover")
async def remover_caixa_seguro(
    caixa_id: int,
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    caixa = _carregar_caixa(db, caixa_id)
    if not caixa:
        raise HTTPException(status_code=404, detail="Caixa nao encontrada.")

    hidrometros = get_caixa_hidrometros(caixa)
    if caixa.status == CaixaStatus.ENTREGUE or any(h.status == HidrometroStatus.COM_INSTALADOR for h in hidrometros):
        return RedirectResponse(
            url=f"/almoxarifado/caixas/{caixa.id}?erro_operacao={quote('Esta caixa ainda esta em operacao e nao pode ser removida ou inativada agora.')}",
            status_code=302,
        )

    if _caixa_reservada_em_separacao(db, caixa.id):
        return RedirectResponse(
            url=f"/almoxarifado/caixas/{caixa.id}?erro_operacao={quote('Esta caixa esta reservada em uma solicitacao separada. Cancele ou revise a solicitacao antes.')}",
            status_code=302,
        )

    possui_movimentacao = bool(caixa.movimentacoes)
    possui_hidrometros = bool(hidrometros)
    acao_realizada = "inativada"

    if not possui_movimentacao and not possui_hidrometros:
        registrar_auditoria(
            db=db,
            acao="DELETE_CAIXA_SEGURO",
            usuario_id=usuario.id,
            tabela="caixas_hidrometros",
            registro_id=caixa.id,
            descricao=f"Caixa excluida definitivamente: {caixa.numero_interno}",
            dados_depois={"numero_interno": caixa.numero_interno, "serial_number": caixa.serial_number},
        )
        db.delete(caixa)
        acao_realizada = "excluida"
    else:
        caixa.ativo = False
        caixa.atualizado_em = utc_now()
        registrar_auditoria(
            db=db,
            acao="SOFT_DELETE_CAIXA",
            usuario_id=usuario.id,
            tabela="caixas_hidrometros",
            registro_id=caixa.id,
            descricao=f"Caixa inativada com seguranca: {caixa.numero_interno}",
            dados_depois={
                "numero_interno": caixa.numero_interno,
                "serial_number": caixa.serial_number,
                "possui_movimentacao": possui_movimentacao,
                "possui_hidrometros": possui_hidrometros,
                "ativo": caixa.ativo,
            },
        )

    db.commit()
    return RedirectResponse(url=f"/almoxarifado/caixas?removida={acao_realizada}", status_code=302)


@router.get("/caixas/{caixa_id}", response_class=HTMLResponse)
async def detalhe_caixa(
    request: Request,
    caixa_id: int,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    caixa = _carregar_caixa(db, caixa_id)
    if not caixa:
        raise HTTPException(status_code=404, detail="Caixa nao encontrada.")
    removidos_manutencao = manutencoes_caixa(db, caixa.id)
    return templates.TemplateResponse(
        "almoxarifado/caixa_detalhe.html",
        {"request": request, "usuario": usuario, "caixa": caixa, "removidos_manutencao": removidos_manutencao},
    )


@router.get("/hidrometros/{hidrometro_id}/manutencao", response_class=HTMLResponse)
async def abrir_manutencao_page(
    request: Request,
    hidrometro_id: int,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    hidrometro = db.query(Hidrometro).options(
        joinedload(Hidrometro.caixa),
        joinedload(Hidrometro.instalador_atual),
    ).filter(Hidrometro.id == hidrometro_id).first()
    if not hidrometro:
        raise HTTPException(status_code=404, detail="Hidrometro nao encontrado.")
    return templates.TemplateResponse(
        "almoxarifado/manutencao_form.html",
        {"request": request, "usuario": usuario, "hidrometro": hidrometro},
    )


@router.post("/hidrometros/{hidrometro_id}/manutencao")
async def abrir_manutencao(
    request: Request,
    hidrometro_id: int,
    motivo: str = Form(""),
    descricao_problema: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    hidrometro = db.query(Hidrometro).options(
        joinedload(Hidrometro.caixa),
        joinedload(Hidrometro.instalador_atual),
    ).filter(Hidrometro.id == hidrometro_id).first()
    if not hidrometro:
        raise HTTPException(status_code=404, detail="Hidrometro nao encontrado.")
    try:
        abrir_manutencao_hidrometro(
            db,
            hidrometro,
            usuario_id=usuario.id,
            motivo=motivo,
            descricao_problema=descricao_problema,
            request=request,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "almoxarifado/manutencao_form.html",
            {"request": request, "usuario": usuario, "hidrometro": hidrometro, "erro": str(exc)},
            status_code=400,
        )
    db.commit()
    return RedirectResponse(url="/almoxarifado/manutencao?aberta=1", status_code=302)


@router.get("/manutencao", response_class=HTMLResponse)
async def manutencao_hidrometros_page(
    request: Request,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    manutencoes = db.query(HidrometroManutencao).options(
        joinedload(HidrometroManutencao.hidrometro),
        joinedload(HidrometroManutencao.caixa_origem).joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(HidrometroManutencao.instalador_origem),
        joinedload(HidrometroManutencao.criado_por_usuario),
    ).order_by(HidrometroManutencao.updated_at.desc(), HidrometroManutencao.id.desc()).limit(500).all()
    return templates.TemplateResponse(
        "almoxarifado/manutencao.html",
        {
            "request": request,
            "usuario": usuario,
            "manutencoes": manutencoes,
            "resumo": resumo_manutencao_hidrometros(db),
            "hidrometros_soltos": listar_hidrometros_soltos_disponiveis(db, limite=200),
            "DECISAO_VOLTAR_ESTOQUE": DECISAO_VOLTAR_ESTOQUE,
            "DECISAO_VOLTAR_CAIXA_ORIGEM": DECISAO_VOLTAR_CAIXA_ORIGEM,
            "DECISAO_CONTINUAR_MANUTENCAO": DECISAO_CONTINUAR_MANUTENCAO,
            "DECISAO_DESCARTAR": DECISAO_DESCARTAR,
            "caixa_origem_disponivel_para_retorno": caixa_origem_disponivel_para_retorno,
        },
    )


@router.get("/manutencao/exportar.xlsx")
async def exportar_manutencao_xlsx(
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    manutencoes = db.query(HidrometroManutencao).options(
        joinedload(HidrometroManutencao.hidrometro),
        joinedload(HidrometroManutencao.caixa_origem),
    ).order_by(HidrometroManutencao.data_abertura.desc(), HidrometroManutencao.id.desc()).all()
    return resposta_xlsx(
        filename="manutencao_hidrometros.xlsx",
        sheet_name="Manutencao",
        headers=[
            "ID",
            "Serie",
            "Caixa origem",
            "Status manutencao",
            "Motivo",
            "Descricao problema",
            "Fornecedor",
            "Data abertura",
            "Data envio",
            "Data retorno",
            "Laudo",
            "Decisao final",
            "Revertida",
            "Data reversao",
            "Destino reversao",
            "Justificativa reversao",
            "retornou_manutencao",
            "data_retorno_manutencao",
            "prioridade_reutilizacao",
            "dias_parado",
            "origem_prioridade",
        ],
        rows=linhas_exportacao_manutencao(manutencoes),
    )


@router.post("/manutencao/{manutencao_id}/enviar")
async def enviar_manutencao_assistencia(
    request: Request,
    manutencao_id: int,
    fornecedor: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    manutencao = _carregar_manutencao(db, manutencao_id)
    if not manutencao:
        raise HTTPException(status_code=404, detail="Manutencao nao encontrada.")
    try:
        enviar_assistencia(db, manutencao, usuario_id=usuario.id, fornecedor=fornecedor, request=request)
    except ValueError as exc:
        return RedirectResponse(url=f"/almoxarifado/manutencao?erro={quote(str(exc))}", status_code=302)
    db.commit()
    return RedirectResponse(url="/almoxarifado/manutencao?enviado=1", status_code=302)


@router.post("/manutencao/{manutencao_id}/retorno")
async def retorno_manutencao_assistencia(
    request: Request,
    manutencao_id: int,
    laudo: str = Form(""),
    decisao_final: str = Form(DECISAO_VOLTAR_ESTOQUE),
    observacao_prioridade: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    manutencao = _carregar_manutencao(db, manutencao_id)
    if not manutencao:
        raise HTTPException(status_code=404, detail="Manutencao nao encontrada.")
    user_role = getattr(getattr(usuario, "role", None), "value", getattr(usuario, "role", None))
    if normalize_text(decisao_final, upper=True) == DECISAO_DESCARTAR and user_role != "admin":
        registrar_auditoria(
            db=db,
            acao="MANUTENCAO_DESCARTE_TENTATIVA_BLOQUEADA",
            usuario_id=usuario.id,
            tabela=HidrometroManutencao.__tablename__,
            registro_id=manutencao.id,
            descricao="Perfil sem permissao tentou descartar hidrometro.",
            request=request,
            severidade="SUSPEITO",
            categoria="SEGURANCA",
            resultado="BLOQUEADO",
        )
        db.commit()
        raise HTTPException(status_code=403, detail="Descarte tecnico e permitido apenas para ADMIN.")
    try:
        registrar_retorno_assistencia(
            db,
            manutencao,
            usuario_id=usuario.id,
            laudo=laudo,
            decisao_final=decisao_final,
            observacao_prioridade=observacao_prioridade,
            request=request,
        )
    except ValueError as exc:
        return RedirectResponse(url=f"/almoxarifado/manutencao?erro={quote(str(exc))}", status_code=302)
    db.commit()
    if normalize_text(decisao_final, upper=True) == DECISAO_VOLTAR_CAIXA_ORIGEM:
        return RedirectResponse(url="/almoxarifado/manutencao?retorno_caixa=1", status_code=302)
    return RedirectResponse(url="/almoxarifado/manutencao?retorno=1", status_code=302)


@router.post("/manutencao/{manutencao_id}/descartar")
async def descartar_manutencao(
    request: Request,
    manutencao_id: int,
    justificativa: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    manutencao = _carregar_manutencao(db, manutencao_id)
    if not manutencao:
        raise HTTPException(status_code=404, detail="Manutencao nao encontrada.")
    user_role = getattr(getattr(usuario, "role", None), "value", getattr(usuario, "role", None))
    if user_role != "admin":
        registrar_auditoria(
            db=db,
            acao="MANUTENCAO_DESCARTE_TENTATIVA_BLOQUEADA",
            usuario_id=usuario.id,
            tabela=HidrometroManutencao.__tablename__,
            registro_id=manutencao.id,
            descricao="Perfil sem permissao tentou descartar hidrometro.",
            request=request,
            severidade="SUSPEITO",
            categoria="SEGURANCA",
            resultado="BLOQUEADO",
        )
        db.commit()
        raise HTTPException(status_code=403, detail="Descarte tecnico e permitido apenas para ADMIN.")
    try:
        descartar_hidrometro_manutencao(db, manutencao, usuario_id=usuario.id, justificativa=justificativa, request=request)
    except ValueError as exc:
        return RedirectResponse(url=f"/almoxarifado/manutencao?erro={quote(str(exc))}", status_code=302)
    db.commit()
    return RedirectResponse(url="/almoxarifado/manutencao?descartado=1", status_code=302)


@router.post("/hidrometros/{hidrometro_id}/prioridade/remover")
async def remover_prioridade_hidrometro(
    request: Request,
    hidrometro_id: int,
    justificativa: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
    if not hidrometro:
        raise HTTPException(status_code=404, detail="Hidrometro nao encontrado.")
    try:
        remover_prioridade_reutilizacao(db, hidrometro, usuario_id=usuario.id, justificativa=justificativa, request=request)
    except ValueError as exc:
        return RedirectResponse(url=f"/almoxarifado/manutencao?erro={quote(str(exc))}", status_code=302)
    db.commit()
    return RedirectResponse(url="/almoxarifado/manutencao?prioridade_removida=1", status_code=302)


@router.get("/estoque", response_class=HTMLResponse)
async def estoque_page(
    request: Request,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    estoques = db.query(EstoquePeca).options(joinedload(EstoquePeca.tipo_peca)).order_by(EstoquePeca.tipo_peca_id).all()
    total_caixas_estoque = db.query(CaixaHidrometro).filter(
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.EM_ESTOQUE,
    ).count()
    total_hidrometros_disponiveis = hidrometros_disponiveis_almoxarifado(db)
    entradas_manuais_recentes = []
    if can_use_advanced_mode(request, usuario) and getattr(getattr(usuario, "role", None), "value", None) == "admin":
        entradas_manuais_recentes = db.query(MovimentacaoPeca).options(
            joinedload(MovimentacaoPeca.tipo_peca),
            joinedload(MovimentacaoPeca.registrado_por),
        ).filter(
            MovimentacaoPeca.tipo == MovimentacaoTipo.ENTRADA,
            MovimentacaoPeca.instalador_id == None,
            MovimentacaoPeca.solicitacao_id == None,
        ).order_by(MovimentacaoPeca.criado_em.desc(), MovimentacaoPeca.id.desc()).limit(10).all()
    return templates.TemplateResponse(
        "almoxarifado/estoque.html",
        {
            "request": request,
            "usuario": usuario,
            "estoques": estoques,
            "total_caixas_estoque": total_caixas_estoque,
            "total_hidrometros_disponiveis": total_hidrometros_disponiveis,
            "resumo_manutencao": resumo_manutencao_hidrometros(db),
            "entradas_manuais_recentes": entradas_manuais_recentes,
        },
    )


@router.get("/estoque-inteligente", response_class=HTMLResponse)
async def estoque_inteligente_page(
    request: Request,
    status: str = "todos",
    q: str = "",
    dias: int = 30,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    dias_periodo = min(max(int(dias or 30), 7), 90)
    dashboard = montar_dashboard_estoque_inteligente(db, dias_periodo=dias_periodo)
    registrar_auditoria_alertas_estoque(db, dashboard, usuario_id=usuario.id)
    db.commit()

    filtro_status = normalize_text(status or "todos", upper=True)
    if filtro_status in {"TODOS", "ALL", ""}:
        filtro_status = "TODOS"

    termo_busca = normalize_text(q or "", upper=True)
    itens_filtrados = []
    for item in dashboard["itens"]:
        if filtro_status != "TODOS" and item["status"] != filtro_status:
            continue
        if termo_busca and termo_busca not in normalize_text(item["nome"], upper=True):
            continue
        itens_filtrados.append(item)

    return templates.TemplateResponse(
        "almoxarifado/estoque_inteligente.html",
        {
            "request": request,
            "usuario": usuario,
            "dashboard": dashboard,
            "itens": itens_filtrados,
            "filtro_status": filtro_status.lower(),
            "busca": q,
            "dias_periodo": dias_periodo,
            "admin_full": getattr(getattr(usuario, "role", None), "value", None) == "admin",
        },
    )


@router.get("/estoque/exportar.xlsx")
async def exportar_estoque_xlsx(
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    estoques = db.query(EstoquePeca).options(joinedload(EstoquePeca.tipo_peca)).order_by(EstoquePeca.tipo_peca_id).all()
    return resposta_xlsx(
        filename="estoque_pecas.xlsx",
        sheet_name="Pecas",
        headers=[
            "Peca",
            "Unidade",
            "Quantidade atual",
            "Quantidade maxima",
            "Percentual atual",
            "Estoque minimo",
            "Situacao",
            "Atualizado em",
        ],
        rows=_linhas_exportacao_estoque(estoques),
    )


@router.get("/carcacas", response_class=HTMLResponse)
async def carcacas_page(
    request: Request,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    return _render_carcacas(request, usuario, db)


@router.post("/carcacas/devolucao")
async def registrar_devolucao_carcacas_page(
    request: Request,
    instalador_id: int = Form(...),
    quantidade: int = Form(...),
    data_movimento: str = Form(""),
    documento_referencia: str = Form(""),
    observacao: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    instalador = db.query(Instalador).filter(Instalador.id == instalador_id, Instalador.ativo == True).first()
    if not instalador:
        return _render_carcacas(request, usuario, db, erro="Selecione um instalador ativo.", status_code=400)
    try:
        movimento = registrar_devolucao_carcacas(
            db,
            usuario_id=usuario.id,
            instalador_id=instalador_id,
            quantidade=quantidade,
            data_movimento=_data_operacional(data_movimento),
            documento_referencia=documento_referencia,
            observacao=observacao,
        )
    except ValueError as exc:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo=str(exc),
            ip=get_request_ip(request),
            status_code=400,
            tabela=CarcacaMovimentacao.__tablename__,
        )
        return _render_carcacas(request, usuario, db, erro=str(exc), status_code=400)

    registrar_auditoria(
        db=db,
        acao="CARCACA_DEVOLUCAO",
        usuario_id=usuario.id,
        tabela=CarcacaMovimentacao.__tablename__,
        registro_id=movimento.id,
        descricao=f"Devolucao de carcacas por {instalador.nome}",
        dados_depois={
            "instalador_id": instalador_id,
            "quantidade": quantidade,
            "documento_referencia": normalize_text(documento_referencia) or None,
            "saldo_atual": saldo_carcacas(db),
        },
    )
    db.commit()
    return RedirectResponse(url="/almoxarifado/carcacas?sucesso=devolucao", status_code=302)


@router.post("/carcacas/recolhimento")
async def registrar_recolhimento_carcacas_page(
    request: Request,
    quantidade: int = Form(...),
    data_movimento: str = Form(""),
    documento_referencia: str = Form(""),
    observacao: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    try:
        movimento = registrar_recolhimento_carcacas(
            db,
            usuario_id=usuario.id,
            quantidade=quantidade,
            data_movimento=_data_operacional(data_movimento),
            documento_referencia=documento_referencia,
            observacao=observacao,
        )
    except ValueError as exc:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo=str(exc),
            ip=get_request_ip(request),
            status_code=400,
            tabela=CarcacaMovimentacao.__tablename__,
        )
        return _render_carcacas(request, usuario, db, erro=str(exc), status_code=400)

    registrar_auditoria(
        db=db,
        acao="CARCACA_RECOLHIMENTO_SABESP",
        usuario_id=usuario.id,
        tabela=CarcacaMovimentacao.__tablename__,
        registro_id=movimento.id,
        descricao="Recolhimento de carcacas pela Sabesp",
        dados_depois={
            "quantidade": quantidade,
            "documento_referencia": normalize_text(documento_referencia) or None,
            "saldo_atual": saldo_carcacas(db),
        },
    )
    db.commit()
    return RedirectResponse(url="/almoxarifado/carcacas?sucesso=recolhimento", status_code=302)


@router.get("/carcacas/exportar.xlsx")
async def exportar_carcacas_xlsx(
    usuario=admin_dep,
    db: Session = Depends(get_db),
):
    movimentos = db.query(CarcacaMovimentacao).options(
        joinedload(CarcacaMovimentacao.instalador),
        joinedload(CarcacaMovimentacao.usuario),
    ).order_by(CarcacaMovimentacao.data_movimento.asc(), CarcacaMovimentacao.id.asc()).all()
    return resposta_xlsx(
        filename="relatorio_carcacas.xlsx",
        sheet_name="Carcacas",
        headers=[
            "Data",
            "Tipo",
            "Instalador",
            "Quantidade",
            "Saldo",
            "Documento",
            "Usuario",
            "Observacao",
            "Registrado em",
        ],
        rows=_linhas_exportacao_carcacas(movimentos),
    )


@router.get("/transferencias", response_class=HTMLResponse)
async def transferencias_page(
    request: Request,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    transferencias = db.query(TransferenciaEmpresa).options(
        joinedload(TransferenciaEmpresa.usuario),
        joinedload(TransferenciaEmpresa.caixas).joinedload(TransferenciaEmpresaCaixa.caixa),
        joinedload(TransferenciaEmpresa.pecas).joinedload(TransferenciaEmpresaPeca.tipo_peca),
    ).order_by(TransferenciaEmpresa.data_transferencia.desc(), TransferenciaEmpresa.id.desc()).limit(200).all()
    return templates.TemplateResponse(
        "almoxarifado/transferencias.html",
        {"request": request, "usuario": usuario, "transferencias": transferencias},
    )


@router.get("/transferencias/nova", response_class=HTMLResponse)
async def nova_transferencia_page(
    request: Request,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    return _render_transferencia_form(request, usuario, db)


@router.post("/transferencias/nova")
async def criar_transferencia_empresa(
    request: Request,
    empresa_destino: str = Form(...),
    data_transferencia: str = Form(""),
    responsavel: str = Form(...),
    documento_referencia: str = Form(""),
    observacao: str = Form(""),
    confirmacao_contexto: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    form = await request.form()
    if not parse_bool_form(confirmacao_contexto, default=False):
        return _render_transferencia_form(
            request,
            usuario,
            db,
            erro="Confirme o resumo da transferencia antes de registrar.",
            status_code=400,
        )

    caixa_ids = []
    for value in form.getlist("caixa_ids"):
        try:
            caixa_ids.append(int(value))
        except (TypeError, ValueError):
            continue

    tipos = db.query(TipoPeca).filter(TipoPeca.ativo == True).all()
    pecas_quantidades: dict[int, int] = {}
    for tipo in tipos:
        valor = str(form.get(f"peca_{tipo.id}", "0")).strip() or "0"
        try:
            quantidade = int(valor)
        except ValueError:
            return _render_transferencia_form(
                request,
                usuario,
                db,
                erro=f"Quantidade invalida para {tipo.nome}.",
                status_code=400,
            )
        if quantidade > 0:
            pecas_quantidades[tipo.id] = quantidade

    try:
        transferencia = registrar_transferencia_empresa(
            db,
            usuario_id=usuario.id,
            empresa_destino=empresa_destino,
            data_transferencia=_data_operacional(data_transferencia),
            responsavel=responsavel,
            caixa_ids=caixa_ids,
            pecas_quantidades=pecas_quantidades,
            documento_referencia=documento_referencia,
            observacao=observacao,
        )
    except ValueError as exc:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo=str(exc),
            ip=get_request_ip(request),
            status_code=400,
            tabela=TransferenciaEmpresa.__tablename__,
        )
        return _render_transferencia_form(request, usuario, db, erro=str(exc), status_code=400)

    registrar_auditoria(
        db=db,
        acao="TRANSFERENCIA_EMPRESA",
        usuario_id=usuario.id,
        tabela=TransferenciaEmpresa.__tablename__,
        registro_id=transferencia.id,
        descricao=f"Transferencia para {transferencia.empresa_destino}",
        dados_depois={
            "empresa_destino": transferencia.empresa_destino,
            "caixas": caixa_ids,
            "pecas": pecas_quantidades,
            "documento_referencia": transferencia.documento_referencia,
        },
    )
    db.commit()
    return RedirectResponse(url="/almoxarifado/transferencias?sucesso=1", status_code=302)


@router.get("/transferencias/exportar.xlsx")
async def exportar_transferencias_xlsx(
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    transferencias = db.query(TransferenciaEmpresa).options(
        joinedload(TransferenciaEmpresa.usuario),
        joinedload(TransferenciaEmpresa.caixas).joinedload(TransferenciaEmpresaCaixa.caixa),
        joinedload(TransferenciaEmpresa.pecas).joinedload(TransferenciaEmpresaPeca.tipo_peca),
    ).order_by(TransferenciaEmpresa.data_transferencia.asc(), TransferenciaEmpresa.id.asc()).all()
    return resposta_xlsx(
        filename="transferencias_empresa.xlsx",
        sheet_name="Transferencias",
        headers=[
            "ID",
            "Empresa destino",
            "Data",
            "Responsavel",
            "Caixas",
            "Pecas",
            "Documento",
            "Usuario",
            "Observacao",
            "Registrado em",
        ],
        rows=_linhas_exportacao_transferencias(transferencias),
    )


@router.get("/confirmacoes-instalador", response_class=HTMLResponse)
async def confirmacoes_instalador_page(
    request: Request,
    status_recebimento: str = "",
    instalador_id: int = 0,
    data_inicio: str = "",
    data_fim: str = "",
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
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
    ).limit(500).all()
    instaladores = db.query(Instalador).filter(Instalador.ativo == True).order_by(Instalador.nome).all()
    return templates.TemplateResponse(
        "almoxarifado/confirmacoes_instalador.html",
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


@router.get("/confirmacoes-instalador/exportar.xlsx")
async def exportar_confirmacoes_instalador_xlsx(
    status_recebimento: str = "",
    instalador_id: int = 0,
    data_inicio: str = "",
    data_fim: str = "",
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
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
        filename="confirmacoes_instalador.xlsx",
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


@router.get("/pecas/entrada", response_class=HTMLResponse)
async def entrada_pecas_page(
    request: Request,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    tipos = db.query(TipoPeca).filter(TipoPeca.ativo == True).order_by(TipoPeca.nome).all()
    return templates.TemplateResponse(
        "almoxarifado/entrada_pecas.html",
        {"request": request, "usuario": usuario, "tipos": tipos},
    )


@router.post("/pecas/entrada")
async def registrar_entrada_pecas(
    request: Request,
    tipo_peca_id: int = Form(...),
    quantidade: int = Form(...),
    observacoes: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    if quantidade <= 0:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo="Entrada de pecas bloqueada: quantidade precisa ser maior que zero.",
            ip=get_request_ip(request),
            status_code=400,
            tabela=EstoquePeca.__tablename__,
        )
        raise HTTPException(status_code=400, detail="A quantidade precisa ser maior que zero.")

    tipo = db.query(TipoPeca).options(joinedload(TipoPeca.estoque)).filter(TipoPeca.id == tipo_peca_id).first()
    if not tipo or not tipo.ativo:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo="Entrada de pecas bloqueada: tipo de peca inexistente ou inativo.",
            ip=get_request_ip(request),
            status_code=404,
            tabela=TipoPeca.__tablename__,
            registro_id=tipo_peca_id,
        )
        raise HTTPException(status_code=404, detail="Tipo de peca nao encontrado.")

    estoque = tipo.estoque
    if not estoque:
        estoque = EstoquePeca(tipo_peca_id=tipo.id, quantidade_atual=0, quantidade_maxima=max(quantidade, 100))
        db.add(estoque)
        db.flush()

    estoque.quantidade_atual += quantidade
    estoque.atualizado_em = utc_now()

    db.add(
        MovimentacaoPeca(
            tipo=MovimentacaoTipo.ENTRADA,
            tipo_peca_id=tipo.id,
            quantidade=quantidade,
            instalador_id=None,
            solicitacao_id=None,
            observacoes=normalize_text(observacoes) or f"Entrada de {tipo.nome}",
            registrado_por_id=usuario.id,
            criado_em=utc_now(),
        )
    )

    registrar_auditoria(
        db=db,
        acao="ENTRADA_PECA",
        usuario_id=usuario.id,
        tabela="estoque_pecas",
        registro_id=estoque.id,
        descricao=f"Entrada de pecas: {tipo.nome}",
        dados_depois={
            "tipo_peca": tipo.nome,
            "quantidade_entrada": quantidade,
            "quantidade_atual": estoque.quantidade_atual,
            "observacoes": normalize_text(observacoes) or None,
        },
    )
    db.commit()
    return RedirectResponse(url="/almoxarifado/estoque?sucesso=1", status_code=302)


@router.get("/solicitacoes", response_class=HTMLResponse)
async def listar_solicitacoes(
    request: Request,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    solicitacoes = db.query(Solicitacao).options(
        joinedload(Solicitacao.instalador),
        joinedload(Solicitacao.criado_por),
        joinedload(Solicitacao.itens_caixa).joinedload(SolicitacaoItemCaixa.caixa),
        joinedload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca),
    ).order_by(Solicitacao.criado_em.desc(), Solicitacao.id.desc()).all()
    return templates.TemplateResponse(
        "almoxarifado/solicitacoes.html",
        {"request": request, "usuario": usuario, "solicitacoes": solicitacoes},
    )


@router.get("/solicitacoes/{sid}/separar", response_class=HTMLResponse)
async def separar_solicitacao_page(
    request: Request,
    sid: int,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    solicitacao = _carregar_solicitacao_ou_404(db, sid)
    caixas_disponiveis = listar_caixas_disponiveis(db, ignorar_solicitacao_id=sid)
    pecas_insuficientes = validar_estoque_pecas(solicitacao)
    return templates.TemplateResponse(
        "almoxarifado/separar_solicitacao.html",
        {
            "request": request,
            "usuario": usuario,
            "solicitacao": solicitacao,
            "caixas_disponiveis": caixas_disponiveis,
            "pecas_insuficientes": pecas_insuficientes,
        },
    )


@router.post("/solicitacoes/{sid}/separar")
async def separar_solicitacao(
    request: Request,
    sid: int,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    solicitacao = _carregar_solicitacao_ou_404(db, sid)
    form = await request.form()

    caixas_por_item: dict[int, int] = {}
    for item in solicitacao.itens_caixa:
        valor = str(form.get(f"item_caixa_{item.id}", "")).strip()
        if not valor:
            registrar_bloqueio_operacional(
                usuario_id=usuario.id,
                path=str(request.url.path),
                metodo=request.method,
                motivo="Separacao bloqueada: caixa obrigatoria nao selecionada.",
                ip=get_request_ip(request),
                status_code=400,
                tabela=Solicitacao.__tablename__,
                registro_id=solicitacao.id,
            )
            caixas_disponiveis = listar_caixas_disponiveis(db, ignorar_solicitacao_id=sid)
            return templates.TemplateResponse(
                "almoxarifado/separar_solicitacao.html",
                {
                    "request": request,
                    "usuario": usuario,
                    "solicitacao": solicitacao,
                    "caixas_disponiveis": caixas_disponiveis,
                    "pecas_insuficientes": validar_estoque_pecas(solicitacao),
                    "erro": "Selecione uma caixa para cada item solicitado.",
                },
                status_code=400,
            )
        caixas_por_item[item.id] = int(valor)

    if not parse_bool_form(str(form.get("confirmacao_contexto", "")), default=False):
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo="Separacao bloqueada: confirmacao contextual obrigatoria nao marcada.",
            ip=get_request_ip(request),
            status_code=400,
            tabela=Solicitacao.__tablename__,
            registro_id=solicitacao.id,
        )
        caixas_disponiveis = listar_caixas_disponiveis(db, ignorar_solicitacao_id=sid)
        return templates.TemplateResponse(
            "almoxarifado/separar_solicitacao.html",
            {
                "request": request,
                "usuario": usuario,
                "solicitacao": solicitacao,
                "caixas_disponiveis": caixas_disponiveis,
                "pecas_insuficientes": validar_estoque_pecas(solicitacao),
                "erro": "Confirme o resumo da separacao antes de continuar.",
            },
            status_code=400,
        )

    try:
        executar_separacao(db, solicitacao, caixas_por_item)
    except ValueError as exc:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo=str(exc),
            ip=get_request_ip(request),
            status_code=400,
            tabela=Solicitacao.__tablename__,
            registro_id=solicitacao.id,
        )
        caixas_disponiveis = listar_caixas_disponiveis(db, ignorar_solicitacao_id=sid)
        return templates.TemplateResponse(
            "almoxarifado/separar_solicitacao.html",
            {
                "request": request,
                "usuario": usuario,
                "solicitacao": solicitacao,
                "caixas_disponiveis": caixas_disponiveis,
                "pecas_insuficientes": validar_estoque_pecas(solicitacao),
                "erro": str(exc),
            },
            status_code=400,
        )

    registrar_auditoria(
        db=db,
        acao="SEPARAR_SOLICITACAO",
        usuario_id=usuario.id,
        tabela="solicitacoes",
        registro_id=solicitacao.id,
        descricao=f"Solicitacao #{solicitacao.id} separada",
        dados_depois={
            "caixas": [
                {
                    "item_id": item.id,
                    "caixa_id": item.caixa_id,
                    "numero_interno": item.caixa.numero_interno if item.caixa else None,
                }
                for item in solicitacao.itens_caixa
            ],
            "pecas": [{"tipo": item.tipo_peca.nome, "quantidade": item.quantidade_solicitada} for item in solicitacao.itens_peca],
        },
    )
    db.commit()
    return RedirectResponse(url=f"/almoxarifado/solicitacoes/{sid}/entregar", status_code=302)


@router.get("/solicitacoes/{sid}/entregar", response_class=HTMLResponse)
async def entregar_solicitacao_page(
    request: Request,
    sid: int,
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    solicitacao = _carregar_solicitacao_ou_404(db, sid)
    if solicitacao.status != SolicitacaoStatus.SEPARADA:
        raise HTTPException(status_code=400, detail="A solicitacao precisa estar separada antes da entrega.")
    return templates.TemplateResponse(
        "almoxarifado/entregar_solicitacao.html",
        {
            "request": request,
            "usuario": usuario,
            "solicitacao": solicitacao,
            "pecas_insuficientes": validar_estoque_pecas(solicitacao),
        },
    )


@router.post("/solicitacoes/{sid}/entregar")
async def confirmar_entrega(
    request: Request,
    sid: int,
    observacoes: str = Form(""),
    usuario=alm_dep,
    db: Session = Depends(get_db),
):
    solicitacao = _carregar_solicitacao_ou_404(db, sid)
    form = await request.form()

    for item in solicitacao.itens_caixa:
        if not item.caixa:
            registrar_bloqueio_operacional(
                usuario_id=usuario.id,
                path=str(request.url.path),
                metodo=request.method,
                motivo="Entrega bloqueada: existe caixa sem separacao concluida.",
                ip=get_request_ip(request),
                status_code=400,
                tabela=Solicitacao.__tablename__,
                registro_id=solicitacao.id,
            )
            raise HTTPException(status_code=400, detail="Ha caixas sem separacao concluida.")

        numero_confirmado = normalize_text(str(form.get(f"confirm_numero_{item.id}", "")), upper=True)
        serial_confirmado = normalize_text(str(form.get(f"confirm_serial_{item.id}", "")), upper=True)
        if numero_confirmado != item.caixa.numero_interno or serial_confirmado != item.caixa.serial_number:
            registrar_bloqueio_operacional(
                usuario_id=usuario.id,
                path=str(request.url.path),
                metodo=request.method,
                motivo=f"Entrega bloqueada: conferencia visual da caixa {item.caixa.numero_interno} nao corresponde.",
                ip=get_request_ip(request),
                status_code=400,
                tabela=Solicitacao.__tablename__,
                registro_id=solicitacao.id,
            )
            return templates.TemplateResponse(
                "almoxarifado/entregar_solicitacao.html",
                {
                    "request": request,
                    "usuario": usuario,
                    "solicitacao": solicitacao,
                    "pecas_insuficientes": validar_estoque_pecas(solicitacao),
                    "erro": f"Conferencia visual da caixa {item.caixa.numero_interno} nao corresponde aos dados esperados.",
                },
                status_code=400,
            )

    if not parse_bool_form(str(form.get("confirmacao_contexto", "")), default=False):
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo="Entrega bloqueada: confirmacao contextual obrigatoria nao marcada.",
            ip=get_request_ip(request),
            status_code=400,
            tabela=Solicitacao.__tablename__,
            registro_id=solicitacao.id,
        )
        return templates.TemplateResponse(
            "almoxarifado/entregar_solicitacao.html",
            {
                "request": request,
                "usuario": usuario,
                "solicitacao": solicitacao,
                "pecas_insuficientes": validar_estoque_pecas(solicitacao),
                "erro": "Confirme o resumo da entrega antes de finalizar.",
            },
            status_code=400,
        )

    try:
        confirmar_entrega_solicitacao(db, solicitacao, usuario.id, observacoes=observacoes)
    except ValueError as exc:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo=str(exc),
            ip=get_request_ip(request),
            status_code=400,
            tabela=Solicitacao.__tablename__,
            registro_id=solicitacao.id,
        )
        return templates.TemplateResponse(
            "almoxarifado/entregar_solicitacao.html",
            {
                "request": request,
                "usuario": usuario,
                "solicitacao": solicitacao,
                "pecas_insuficientes": validar_estoque_pecas(solicitacao),
                "erro": str(exc),
            },
            status_code=400,
        )

    registrar_auditoria(
        db=db,
        acao="ENTREGAR_SOLICITACAO",
        usuario_id=usuario.id,
        tabela="solicitacoes",
        registro_id=solicitacao.id,
        descricao=f"Solicitacao #{solicitacao.id} entregue ao instalador",
        dados_depois={
            "instalador": solicitacao.instalador.nome,
            "caixas": [{"numero_interno": item.caixa.numero_interno, "serial_number": item.caixa.serial_number} for item in solicitacao.itens_caixa],
            "pecas": [{"tipo": item.tipo_peca.nome, "quantidade": item.quantidade_solicitada} for item in solicitacao.itens_peca],
            "observacoes": normalize_text(observacoes) or None,
        },
    )
    db.commit()
    return RedirectResponse(url="/almoxarifado/solicitacoes?sucesso=1", status_code=302)
