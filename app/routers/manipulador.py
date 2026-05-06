from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import (
    CaixaHidrometro,
    CaixaStatus,
    ConferenciaInstaladorPeca,
    ConferenciaItem,
    ConferenciaPecas,
    Hidrometro,
    HidrometroStatus,
    Instalador,
    InstaladorPeca,
    MovimentacaoMaterial,
    MovimentacaoTipo,
    Solicitacao,
    SolicitacaoItemCaixa,
    SolicitacaoItemPeca,
    SolicitacaoStatus,
    TipoPeca,
)
from app.security import requer_role
from app.services.auditoria import registrar_auditoria
from app.services.contexto_acoes import get_instalador_hidrometros
from app.services.exportacao import resposta_xlsx
from app.services.hidrometros import aplicar_baixa_hidrometro, mensagem_baixa_hidrometro
from app.services.protecao_operacional import registrar_bloqueio_operacional
from app.services.solicitacoes import cancelar_solicitacao
from app.ui import templates
from app.utils import format_datetime, get_request_ip, normalize_text, parse_bool_form, utc_now


router = APIRouter(prefix="/manipulador", tags=["manipulador"])
man_dep = Depends(requer_role("manipulador", "admin"))
solicitacao_cancel_dep = Depends(requer_role("manipulador", "almoxarifado", "admin"))
INSTALADOR_HIDROMETROS_ATTR = getattr(Instalador, "hidrômetros")
CAIXA_HIDROMETROS_ATTR = getattr(CaixaHidrometro, "hidrômetros")


def _carregar_hidrometro_baixa(db: Session, numero_serie: str) -> Hidrometro | None:
    numero_serie = normalize_text(numero_serie, upper=True)
    if not numero_serie:
        return None
    return db.query(Hidrometro).options(
        joinedload(Hidrometro.caixa),
        joinedload(Hidrometro.instalador_atual),
    ).filter(Hidrometro.numero_serie == numero_serie).first()


def _carregar_hidrometro_rastreamento(db: Session, numero_serie: str) -> Hidrometro | None:
    numero_serie = normalize_text(numero_serie, upper=True)
    if not numero_serie:
        return None
    return db.query(Hidrometro).options(
        joinedload(Hidrometro.caixa).joinedload(CaixaHidrometro.movimentacoes).joinedload(MovimentacaoMaterial.registrado_por),
        joinedload(Hidrometro.caixa).joinedload(CaixaHidrometro.movimentacoes).joinedload(MovimentacaoMaterial.instalador),
        joinedload(Hidrometro.caixa).joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(Hidrometro.instalador_atual),
    ).filter(Hidrometro.numero_serie == numero_serie).first()


def _linhas_exportacao_instaladores(instaladores: list[Instalador]) -> list[list[object]]:
    rows: list[list[object]] = []
    for instalador in instaladores:
        hidrometros = get_instalador_hidrometros(instalador)
        pendentes = [hidro for hidro in hidrometros if hidro.status == HidrometroStatus.COM_INSTALADOR]
        instalados = [hidro for hidro in hidrometros if hidro.status == HidrometroStatus.INSTALADO]
        pecas = ", ".join(
            f"{item.tipo_peca.nome}: {item.quantidade}"
            for item in instalador.pecas_posse
            if item.quantidade > 0 and item.tipo_peca
        )
        rows.append(
            [
                instalador.nome,
                instalador.matricula,
                "ATIVO" if instalador.ativo else "INATIVO",
                len({hidro.caixa_id for hidro in pendentes if hidro.caixa_id}),
                len(pendentes),
                len(instalados),
                pecas,
                instalador.data_nascimento or "",
            ]
        )
    return rows


@router.get("/instaladores", response_class=HTMLResponse)
async def listar_instaladores_man(
    request: Request,
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    instaladores = db.query(Instalador).options(
        joinedload(INSTALADOR_HIDROMETROS_ATTR),
        joinedload(Instalador.pecas_posse).joinedload(InstaladorPeca.tipo_peca),
    ).filter(Instalador.ativo == True).order_by(Instalador.nome).all()

    return templates.TemplateResponse(
        "manipulador/instaladores.html",
        {"request": request, "usuario": usuario, "instaladores": instaladores},
    )


@router.get("/instaladores/exportar.xlsx")
async def exportar_instaladores_xlsx(
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    instaladores = db.query(Instalador).options(
        joinedload(INSTALADOR_HIDROMETROS_ATTR),
        joinedload(Instalador.pecas_posse).joinedload(InstaladorPeca.tipo_peca),
    ).filter(Instalador.ativo == True).order_by(Instalador.nome).all()

    return resposta_xlsx(
        filename="instaladores.xlsx",
        sheet_name="Instaladores",
        headers=[
            "Instalador",
            "Matricula",
            "Status",
            "Caixas em posse",
            "Hidrometros pendentes",
            "Hidrometros instalados",
            "Pecas em posse",
            "Nascimento",
        ],
        rows=_linhas_exportacao_instaladores(instaladores),
    )


@router.get("/instaladores/{iid}", response_class=HTMLResponse)
async def detalhe_instalador_man(
    request: Request,
    iid: int,
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    instalador = db.query(Instalador).options(
        joinedload(INSTALADOR_HIDROMETROS_ATTR).joinedload(Hidrometro.caixa),
        joinedload(Instalador.pecas_posse).joinedload(InstaladorPeca.tipo_peca),
        joinedload(Instalador.solicitacoes).joinedload(Solicitacao.itens_caixa),
    ).filter(Instalador.id == iid).first()
    if not instalador:
        raise HTTPException(status_code=404, detail="Instalador nao encontrado.")

    caixas_posse = db.query(CaixaHidrometro).options(
        joinedload(CAIXA_HIDROMETROS_ATTR)
    ).filter(
        CaixaHidrometro.instalador_id == iid,
        CaixaHidrometro.status == CaixaStatus.ENTREGUE,
    ).all()

    return templates.TemplateResponse(
        "manipulador/instalador_detalhe.html",
        {
            "request": request,
            "usuario": usuario,
            "instalador": instalador,
            "caixas_posse": caixas_posse,
        },
    )


@router.get("/solicitacoes", response_class=HTMLResponse)
async def listar_solicitacoes_man(
    request: Request,
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    solicitacoes = db.query(Solicitacao).options(
        joinedload(Solicitacao.instalador),
        joinedload(Solicitacao.criado_por),
        joinedload(Solicitacao.itens_caixa),
        joinedload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca),
    ).order_by(Solicitacao.criado_em.desc(), Solicitacao.id.desc()).all()

    return templates.TemplateResponse(
        "manipulador/solicitacoes.html",
        {"request": request, "usuario": usuario, "solicitacoes": solicitacoes},
    )


@router.get("/solicitacoes/nova", response_class=HTMLResponse)
async def nova_solicitacao_page(
    request: Request,
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    instaladores = db.query(Instalador).filter(Instalador.ativo == True).order_by(Instalador.nome).all()
    tipos_pecas = db.query(TipoPeca).options(joinedload(TipoPeca.estoque)).filter(TipoPeca.ativo == True).order_by(TipoPeca.nome).all()
    total_caixas = db.query(CaixaHidrometro).filter(
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.EM_ESTOQUE,
    ).count()

    return templates.TemplateResponse(
        "manipulador/solicitacao_form.html",
        {
            "request": request,
            "usuario": usuario,
            "instaladores": instaladores,
            "tipos_pecas": tipos_pecas,
            "total_caixas_estoque": total_caixas,
        },
    )


@router.post("/solicitacoes/nova")
async def criar_solicitacao(
    request: Request,
    instalador_id: int = Form(...),
    qtd_caixas: int = Form(0),
    observacoes: str = Form(""),
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    form = await request.form()
    instalador = db.query(Instalador).filter(Instalador.id == instalador_id, Instalador.ativo == True).first()
    if not instalador:
        raise HTTPException(status_code=404, detail="Instalador nao encontrado.")

    total_caixas_estoque = db.query(CaixaHidrometro).filter(
        CaixaHidrometro.ativo == True,
        CaixaHidrometro.status == CaixaStatus.EM_ESTOQUE,
    ).count()
    tipos_pecas = db.query(TipoPeca).options(joinedload(TipoPeca.estoque)).filter(TipoPeca.ativo == True).order_by(TipoPeca.nome).all()

    if qtd_caixas < 0:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo="Solicitacao bloqueada: quantidade de caixas negativa.",
            ip=get_request_ip(request),
            status_code=400,
            tabela=Solicitacao.__tablename__,
        )
        raise HTTPException(status_code=400, detail="Quantidade de caixas invalida.")
    if qtd_caixas > total_caixas_estoque:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo=f"Solicitacao bloqueada: pedido de {qtd_caixas} caixa(s), disponivel {total_caixas_estoque}.",
            ip=get_request_ip(request),
            status_code=400,
            tabela=Solicitacao.__tablename__,
        )
        return templates.TemplateResponse(
            "manipulador/solicitacao_form.html",
            {
                "request": request,
                "usuario": usuario,
                "instaladores": db.query(Instalador).filter(Instalador.ativo == True).order_by(Instalador.nome).all(),
                "tipos_pecas": tipos_pecas,
                "total_caixas_estoque": total_caixas_estoque,
                "erro": f"Ha somente {total_caixas_estoque} caixa(s) disponivel(is) no estoque.",
            },
            status_code=400,
        )

    itens_peca_validos: list[tuple[TipoPeca, int]] = []
    for tipo in tipos_pecas:
        valor = str(form.get(f"peca_{tipo.id}", "0")).strip() or "0"
        try:
            quantidade = int(valor)
        except ValueError:
            quantidade = 0
        if quantidade < 0:
            quantidade = 0
        if quantidade == 0:
            continue
        disponivel = tipo.estoque.quantidade_atual if tipo.estoque else 0
        if quantidade > disponivel:
            registrar_bloqueio_operacional(
                usuario_id=usuario.id,
                path=str(request.url.path),
                metodo=request.method,
                motivo=f"Solicitacao bloqueada: peca {tipo.nome} solicitada acima do estoque.",
                ip=get_request_ip(request),
                status_code=400,
                tabela=Solicitacao.__tablename__,
            )
            return templates.TemplateResponse(
                "manipulador/solicitacao_form.html",
                {
                    "request": request,
                    "usuario": usuario,
                    "instaladores": db.query(Instalador).filter(Instalador.ativo == True).order_by(Instalador.nome).all(),
                    "tipos_pecas": tipos_pecas,
                    "total_caixas_estoque": total_caixas_estoque,
                    "erro": f"A peca {tipo.nome} possui somente {disponivel} unidade(s) disponivel(is).",
                },
                status_code=400,
            )
        itens_peca_validos.append((tipo, quantidade))

    if qtd_caixas == 0 and not itens_peca_validos:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo="Solicitacao bloqueada: nenhuma caixa ou peca informada.",
            ip=get_request_ip(request),
            status_code=400,
            tabela=Solicitacao.__tablename__,
        )
        return templates.TemplateResponse(
            "manipulador/solicitacao_form.html",
            {
                "request": request,
                "usuario": usuario,
                "instaladores": db.query(Instalador).filter(Instalador.ativo == True).order_by(Instalador.nome).all(),
                "tipos_pecas": tipos_pecas,
                "total_caixas_estoque": total_caixas_estoque,
                "erro": "Informe ao menos uma caixa ou uma peca para criar a solicitacao.",
            },
            status_code=400,
        )

    solicitacao = Solicitacao(
        instalador_id=instalador.id,
        criado_por_id=usuario.id,
        status=SolicitacaoStatus.PENDENTE,
        observacoes=normalize_text(observacoes) or None,
        criado_em=utc_now(),
    )
    db.add(solicitacao)
    db.flush()

    for _ in range(qtd_caixas):
        db.add(SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1))

    for tipo, quantidade in itens_peca_validos:
        db.add(SolicitacaoItemPeca(
            solicitacao_id=solicitacao.id,
            tipo_peca_id=tipo.id,
            quantidade_solicitada=quantidade,
        ))

    registrar_auditoria(
        db=db,
        acao="CREATE_SOLICITACAO",
        usuario_id=usuario.id,
        tabela="solicitacoes",
        registro_id=solicitacao.id,
        descricao=f"Solicitacao criada para o instalador {instalador.nome}",
        dados_depois={
            "instalador": instalador.nome,
            "qtd_caixas": qtd_caixas,
            "pecas": [{ "tipo": tipo.nome, "quantidade": quantidade } for tipo, quantidade in itens_peca_validos],
            "observacoes": solicitacao.observacoes,
        },
    )
    db.commit()
    return RedirectResponse(url="/manipulador/solicitacoes?sucesso=1", status_code=302)


@router.post("/solicitacoes/{sid}/cancelar")
async def cancelar_solicitacao_man(
    request: Request,
    sid: int,
    motivo: str = Form(""),
    next_path: str = Form(""),
    usuario=solicitacao_cancel_dep,
    db: Session = Depends(get_db),
):
    solicitacao = db.query(Solicitacao).options(
        joinedload(Solicitacao.instalador),
        joinedload(Solicitacao.itens_caixa).joinedload(SolicitacaoItemCaixa.caixa),
        joinedload(Solicitacao.itens_peca).joinedload(SolicitacaoItemPeca.tipo_peca),
    ).filter(Solicitacao.id == sid).first()
    if not solicitacao:
        raise HTTPException(status_code=404, detail="Solicitacao nao encontrada.")

    destino = next_path if str(next_path).startswith("/") and not str(next_path).startswith("//") else "/manipulador/solicitacoes"
    user_role = getattr(getattr(usuario, "role", None), "value", getattr(usuario, "role", ""))
    if user_role != "admin" and solicitacao.status != SolicitacaoStatus.PENDENTE:
        mensagem = "Solicitacao ja movimentada so pode ser cancelada ou revertida pelo ADMIN."
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo=mensagem,
            ip=get_request_ip(request),
            status_code=403,
            tabela=Solicitacao.__tablename__,
            registro_id=solicitacao.id,
            severidade="SUSPEITO",
        )
        separador = "&" if "?" in destino else "?"
        return RedirectResponse(url=f"{destino}{separador}erro={quote(mensagem)}", status_code=302)

    try:
        resultado = cancelar_solicitacao(solicitacao, motivo=motivo)
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
        separador = "&" if "?" in destino else "?"
        return RedirectResponse(url=f"{destino}{separador}erro={quote(str(exc))}", status_code=302)

    registrar_auditoria(
        db=db,
        acao="CANCELAR_SOLICITACAO",
        usuario_id=usuario.id,
        tabela="solicitacoes",
        registro_id=solicitacao.id,
        descricao=f"Solicitacao #{solicitacao.id} cancelada",
        dados_depois={
            "status": solicitacao.status.value,
            "instalador": solicitacao.instalador.nome if solicitacao.instalador else None,
            "motivo": resultado["motivo"],
            "caixas_liberadas": resultado["caixas_liberadas"],
        },
    )
    db.commit()
    separador = "&" if "?" in destino else "?"
    return RedirectResponse(url=f"{destino}{separador}cancelada=1", status_code=302)


@router.get("/baixa-hidrometro", response_class=HTMLResponse)
async def baixa_hidrometro_page(
    request: Request,
    numero_serie: str = "",
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    contexto = {"request": request, "usuario": usuario}
    numero_serie = normalize_text(numero_serie, upper=True)
    if not numero_serie:
        return templates.TemplateResponse("manipulador/baixa_hidrometro.html", contexto)

    hidrometro = _carregar_hidrometro_baixa(db, numero_serie)
    if not hidrometro:
        contexto["erro"] = f"Hidrometro '{numero_serie}' nao encontrado."
        contexto["numero_serie"] = numero_serie
        return templates.TemplateResponse("manipulador/baixa_hidrometro.html", contexto, status_code=404)

    mensagem = mensagem_baixa_hidrometro(hidrometro)
    if mensagem:
        contexto["erro"] = mensagem
    contexto["hidrometro"] = hidrometro
    contexto["numero_serie"] = numero_serie
    return templates.TemplateResponse("manipulador/baixa_hidrometro.html", contexto, status_code=400 if mensagem else 200)


@router.post("/baixa-hidrometro/buscar")
async def buscar_hidrometro_baixa(
    numero_serie: str = Form(...),
    usuario=man_dep,
):
    numero_serie = normalize_text(numero_serie, upper=True)
    return RedirectResponse(url=f"/manipulador/baixa-hidrometro?numero_serie={quote(numero_serie)}", status_code=302)


@router.post("/baixa-hidrometro/confirmar")
async def confirmar_baixa_hidrometro(
    request: Request,
    hidrometro_id: int = Form(...),
    confirmacao_serial: str = Form(""),
    confirmacao_contexto: str = Form(""),
    observacoes: str = Form(""),
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    hidrometro = db.query(Hidrometro).options(joinedload(Hidrometro.caixa)).filter(Hidrometro.id == hidrometro_id).first()
    if not hidrometro:
        raise HTTPException(status_code=404, detail="Hidrometro nao encontrado.")

    mensagem_contexto = mensagem_baixa_hidrometro(hidrometro)
    if mensagem_contexto:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo=mensagem_contexto,
            ip=get_request_ip(request),
            status_code=400,
            tabela=Hidrometro.__tablename__,
            registro_id=hidrometro.id,
        )
        return templates.TemplateResponse(
            "manipulador/baixa_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "numero_serie": hidrometro.numero_serie,
                "erro": mensagem_contexto,
            },
            status_code=400,
        )

    if normalize_text(confirmacao_serial, upper=True) != hidrometro.numero_serie or not parse_bool_form(confirmacao_contexto, default=False):
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo="Baixa bloqueada: confirmacao contextual do hidrometro nao confere.",
            ip=get_request_ip(request),
            status_code=400,
            tabela=Hidrometro.__tablename__,
            registro_id=hidrometro.id,
        )
        return templates.TemplateResponse(
            "manipulador/baixa_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "numero_serie": hidrometro.numero_serie,
                "erro": "Confirme o serial e o contexto antes de registrar a baixa.",
            },
            status_code=400,
        )

    try:
        resultado = aplicar_baixa_hidrometro(db, hidrometro, usuario.id, observacoes=observacoes)
    except ValueError as exc:
        registrar_bloqueio_operacional(
            usuario_id=usuario.id,
            path=str(request.url.path),
            metodo=request.method,
            motivo=str(exc),
            ip=get_request_ip(request),
            status_code=400,
            tabela=Hidrometro.__tablename__,
            registro_id=hidrometro.id,
        )
        return templates.TemplateResponse(
            "manipulador/baixa_hidrometro.html",
            {
                "request": request,
                "usuario": usuario,
                "hidrometro": hidrometro,
                "numero_serie": hidrometro.numero_serie,
                "erro": str(exc),
            },
            status_code=400,
        )

    registrar_auditoria(
        db=db,
        acao="BAIXA_HIDROMETRO",
        usuario_id=usuario.id,
        tabela="hidrômetros",
        registro_id=hidrometro.id,
        descricao=f"Baixa de hidrometro: {hidrometro.numero_serie}",
        dados_depois={
            "numero_serie": hidrometro.numero_serie,
            "caixa_id": hidrometro.caixa_id,
            "instalador_id": resultado["instalador_id"],
            "instalado_em": resultado["momento"],
            "observacoes": resultado["observacoes"],
        },
    )
    if resultado.get("caixa_finalizada") and hidrometro.caixa:
        registrar_auditoria(
            db=db,
            acao="FINALIZACAO_CAIXA",
            usuario_id=usuario.id,
            tabela=CaixaHidrometro.__tablename__,
            registro_id=hidrometro.caixa_id,
            descricao=f"Caixa finalizada automaticamente: {hidrometro.caixa.numero_interno}",
            dados_depois={
                "numero_interno": hidrometro.caixa.numero_interno,
                "status": hidrometro.caixa.status.value,
                "solicitacao_id": resultado.get("solicitacao_id"),
                "instalador_id": resultado.get("instalador_id"),
            },
        )
    db.commit()
    return RedirectResponse(url="/manipulador/baixa-hidrometro?sucesso=1", status_code=302)


@router.get("/conferencia", response_class=HTMLResponse)
async def conferencia_page(
    request: Request,
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    instaladores = db.query(Instalador).filter(Instalador.ativo == True).order_by(Instalador.nome).all()
    conferencias_recentes = db.query(ConferenciaPecas).options(
        joinedload(ConferenciaPecas.instalador),
        joinedload(ConferenciaPecas.responsavel),
    ).order_by(ConferenciaPecas.data_conferencia.desc(), ConferenciaPecas.id.desc()).limit(20).all()

    return templates.TemplateResponse(
        "manipulador/conferencia.html",
        {
            "request": request,
            "usuario": usuario,
            "instaladores": instaladores,
            "conferencias_recentes": conferencias_recentes,
        },
    )


@router.get("/conferencia/{iid}", response_class=HTMLResponse)
async def conferencia_instalador_page(
    request: Request,
    iid: int,
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    instalador = db.query(Instalador).filter(Instalador.id == iid).first()
    if not instalador:
        raise HTTPException(status_code=404, detail="Instalador nao encontrado.")

    pecas_instalador = db.query(InstaladorPeca).options(
        joinedload(InstaladorPeca.tipo_peca)
    ).filter(InstaladorPeca.instalador_id == iid).order_by(InstaladorPeca.tipo_peca_id).all()

    return templates.TemplateResponse(
        "manipulador/conferencia_form.html",
        {
            "request": request,
            "usuario": usuario,
            "instalador": instalador,
            "pecas_instalador": pecas_instalador,
        },
    )


@router.post("/conferencia/{iid}")
async def registrar_conferencia(
    request: Request,
    iid: int,
    observacoes: str = Form(""),
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    form = await request.form()
    instalador = db.query(Instalador).filter(Instalador.id == iid).first()
    if not instalador:
        raise HTTPException(status_code=404, detail="Instalador nao encontrado.")

    pecas_instalador = db.query(InstaladorPeca).options(
        joinedload(InstaladorPeca.tipo_peca)
    ).filter(InstaladorPeca.instalador_id == iid).order_by(InstaladorPeca.tipo_peca_id).all()

    conferencia = ConferenciaPecas(
        instalador_id=iid,
        responsavel_id=usuario.id,
        observacoes=normalize_text(observacoes) or None,
        data_conferencia=utc_now(),
        tem_divergencia=False,
    )
    db.add(conferencia)
    db.flush()

    momento = utc_now()
    divergencias: list[dict] = []
    for item_posse in pecas_instalador:
        qtd_real_texto = str(form.get(f"qtd_real_{item_posse.tipo_peca_id}", item_posse.quantidade)).strip()
        try:
            qtd_real = int(qtd_real_texto)
        except ValueError:
            registrar_bloqueio_operacional(
                usuario_id=usuario.id,
                path=str(request.url.path),
                metodo=request.method,
                motivo=f"Conferencia bloqueada: quantidade invalida para {item_posse.tipo_peca.nome}.",
                ip=get_request_ip(request),
                status_code=400,
                tabela=ConferenciaPecas.__tablename__,
            )
            return templates.TemplateResponse(
                "manipulador/conferencia_form.html",
                {
                    "request": request,
                    "usuario": usuario,
                    "instalador": instalador,
                    "pecas_instalador": pecas_instalador,
                    "erro": f"Informe uma quantidade valida para {item_posse.tipo_peca.nome}.",
                },
                status_code=400,
            )
        if qtd_real < 0:
            registrar_bloqueio_operacional(
                usuario_id=usuario.id,
                path=str(request.url.path),
                metodo=request.method,
                motivo=f"Conferencia bloqueada: quantidade negativa para {item_posse.tipo_peca.nome}.",
                ip=get_request_ip(request),
                status_code=400,
                tabela=ConferenciaPecas.__tablename__,
            )
            return templates.TemplateResponse(
                "manipulador/conferencia_form.html",
                {
                    "request": request,
                    "usuario": usuario,
                    "instalador": instalador,
                    "pecas_instalador": pecas_instalador,
                    "erro": f"A quantidade de {item_posse.tipo_peca.nome} nao pode ser negativa.",
                },
                status_code=400,
            )

        diferenca = qtd_real - item_posse.quantidade
        if diferenca != 0:
            conferencia.tem_divergencia = True
            divergencias.append({"tipo": item_posse.tipo_peca.nome, "diferenca": diferenca})

        db.add(ConferenciaItem(
            conferencia_id=conferencia.id,
            tipo_peca_id=item_posse.tipo_peca_id,
            quantidade_sistema=item_posse.quantidade,
            quantidade_real=qtd_real,
            diferenca=diferenca,
        ))
        db.add(ConferenciaInstaladorPeca(
            conferencia_id=conferencia.id,
            instalador_id=iid,
            peca_id=item_posse.tipo_peca_id,
            quantidade_informada=qtd_real,
            data_conferencia=momento,
            usuario_id=usuario.id,
            observacao=normalize_text(observacoes) or None,
            criado_em=momento,
        ))

    registrar_auditoria(
        db=db,
        acao="CONFERENCIA_PECA",
        usuario_id=usuario.id,
        tabela="conferencias_pecas",
        registro_id=conferencia.id,
        descricao=f"Conferencia de pecas do instalador {instalador.nome}",
        dados_depois={
            "instalador": instalador.nome,
            "tem_divergencia": conferencia.tem_divergencia,
            "divergencias": divergencias,
            "observacoes": conferencia.observacoes,
            "efeito_estoque": "informativo_sem_alteracao_de_saldo",
        },
    )
    db.commit()
    return RedirectResponse(url="/manipulador/conferencia?sucesso=1", status_code=302)


@router.get("/rastrear", response_class=HTMLResponse)
async def rastrear_hidrometro_page(
    request: Request,
    numero_serie: str = "",
    usuario=man_dep,
    db: Session = Depends(get_db),
):
    contexto = {"request": request, "usuario": usuario}
    numero_serie = normalize_text(numero_serie, upper=True)
    if not numero_serie:
        return templates.TemplateResponse("manipulador/rastrear.html", contexto)

    hidrometro = _carregar_hidrometro_rastreamento(db, numero_serie)
    if not hidrometro:
        contexto["erro"] = f"Hidrometro '{numero_serie}' nao encontrado."
        contexto["numero_serie"] = numero_serie
        return templates.TemplateResponse("manipulador/rastrear.html", contexto, status_code=404)

    movimentos_saida = sorted(
        [mov for mov in hidrometro.caixa.movimentacoes if mov.tipo == MovimentacaoTipo.SAIDA] if hidrometro.caixa else [],
        key=lambda mov: (mov.criado_em, mov.id),
        reverse=True,
    )
    contexto["hidrometro"] = hidrometro
    contexto["ultima_saida"] = movimentos_saida[0] if movimentos_saida else None
    contexto["numero_serie"] = numero_serie
    return templates.TemplateResponse("manipulador/rastrear.html", contexto)


@router.post("/rastrear/buscar")
async def buscar_rastreamento(
    numero_serie: str = Form(...),
    usuario=man_dep,
):
    numero_serie = normalize_text(numero_serie, upper=True)
    return RedirectResponse(url=f"/manipulador/rastrear?numero_serie={quote(numero_serie)}", status_code=302)
