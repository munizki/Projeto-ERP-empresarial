from __future__ import annotations

import csv
from io import BytesIO, StringIO
from zipfile import ZIP_DEFLATED, ZipFile

from sqlalchemy.orm import Session, joinedload

from app.models import (
    AuditoriaLog,
    CaixaHidrometro,
    CarcacaMovimentacao,
    EstoquePeca,
    Hidrometro,
    InstalacaoHidrometro,
    Instalador,
    MovimentacaoMaterial,
    MovimentacaoPeca,
    TipoPeca,
    TransferenciaEmpresa,
    TransferenciaEmpresaCaixa,
    TransferenciaEmpresaPeca,
    Usuario,
)
from app.services.regras_caixa import quantidade_esperada_caixa
from app.utils import format_datetime, summarize_user_agent, utc_now


CAIXA_HIDROMETROS_ATTR = getattr(CaixaHidrometro, "hidrômetros")


def _csv_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    buffer = StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")


def gerar_pacote_exportacao_admin(db: Session) -> tuple[str, BytesIO]:
    agora = utc_now()
    filename = f"gmf_exportacao_admin_{agora.strftime('%Y%m%d_%H%M%S')}.zip"
    output = BytesIO()

    caixas = db.query(CaixaHidrometro).options(
        joinedload(CAIXA_HIDROMETROS_ATTR),
        joinedload(CaixaHidrometro.instalador),
    ).order_by(CaixaHidrometro.id).all()
    hidrometros = db.query(Hidrometro).options(
        joinedload(Hidrometro.caixa),
        joinedload(Hidrometro.instalador_atual),
        joinedload(Hidrometro.instalador_baixa),
        joinedload(Hidrometro.baixado_por),
    ).order_by(Hidrometro.id).all()
    estoques = db.query(EstoquePeca).options(joinedload(EstoquePeca.tipo_peca)).order_by(EstoquePeca.id).all()
    tipos = db.query(TipoPeca).order_by(TipoPeca.id).all()
    instaladores = db.query(Instalador).order_by(Instalador.id).all()
    movimentos_material = db.query(MovimentacaoMaterial).options(
        joinedload(MovimentacaoMaterial.caixa),
        joinedload(MovimentacaoMaterial.hidrometro),
        joinedload(MovimentacaoMaterial.instalador),
        joinedload(MovimentacaoMaterial.registrado_por),
    ).order_by(MovimentacaoMaterial.id).all()
    movimentos_peca = db.query(MovimentacaoPeca).options(
        joinedload(MovimentacaoPeca.tipo_peca),
        joinedload(MovimentacaoPeca.instalador),
        joinedload(MovimentacaoPeca.registrado_por),
    ).order_by(MovimentacaoPeca.id).all()
    instalacoes = db.query(InstalacaoHidrometro).options(
        joinedload(InstalacaoHidrometro.instalador),
        joinedload(InstalacaoHidrometro.hidrometro),
        joinedload(InstalacaoHidrometro.caixa),
        joinedload(InstalacaoHidrometro.usuario_registro),
    ).order_by(InstalacaoHidrometro.id).all()
    carcacas = db.query(CarcacaMovimentacao).options(
        joinedload(CarcacaMovimentacao.instalador),
        joinedload(CarcacaMovimentacao.usuario),
    ).order_by(CarcacaMovimentacao.id).all()
    transferencias = db.query(TransferenciaEmpresa).options(
        joinedload(TransferenciaEmpresa.usuario),
        joinedload(TransferenciaEmpresa.caixas).joinedload(TransferenciaEmpresaCaixa.caixa),
        joinedload(TransferenciaEmpresa.pecas).joinedload(TransferenciaEmpresaPeca.tipo_peca),
    ).order_by(TransferenciaEmpresa.id).all()
    auditorias = db.query(AuditoriaLog).options(joinedload(AuditoriaLog.usuario)).order_by(AuditoriaLog.id).all()
    usuarios = db.query(Usuario).order_by(Usuario.id).all()

    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "caixas.csv",
            _csv_bytes(
                ["id", "numero_interno", "serial", "status", "ativo", "quantidade_esperada", "qtd_hidrometros", "instalador"],
                [
                    [
                        caixa.id,
                        caixa.numero_interno,
                        caixa.serial_number,
                        caixa.status.value,
                        caixa.ativo,
                        quantidade_esperada_caixa(caixa),
                        len(getattr(caixa, CAIXA_HIDROMETROS_ATTR.key) or []),
                        caixa.instalador.nome if caixa.instalador else "",
                    ]
                    for caixa in caixas
                ],
            ),
        )
        archive.writestr(
            "hidrometros.csv",
            _csv_bytes(
                ["id", "numero_serie", "status", "caixa", "instalador_atual", "instalador_baixa", "baixado_por", "instalado_em"],
                [
                    [
                        item.id,
                        item.numero_serie,
                        item.status.value,
                        item.caixa.numero_interno if item.caixa else "",
                        item.instalador_atual.nome if item.instalador_atual else "",
                        item.instalador_baixa.nome if item.instalador_baixa else "",
                        item.baixado_por.nome if item.baixado_por else "",
                        format_datetime(item.instalado_em, with_seconds=True),
                    ]
                    for item in hidrometros
                ],
            ),
        )
        archive.writestr(
            "estoque_pecas.csv",
            _csv_bytes(
                ["id", "peca", "quantidade_atual", "quantidade_maxima", "minimo_percentual", "atualizado_em"],
                [
                    [
                        estoque.id,
                        estoque.tipo_peca.nome if estoque.tipo_peca else "",
                        estoque.quantidade_atual,
                        estoque.quantidade_maxima,
                        estoque.tipo_peca.estoque_minimo_percentual if estoque.tipo_peca else "",
                        format_datetime(estoque.atualizado_em, with_seconds=True),
                    ]
                    for estoque in estoques
                ],
            ),
        )
        archive.writestr(
            "tipos_pecas.csv",
            _csv_bytes(
                ["id", "nome", "unidade", "ativo"],
                [[tipo.id, tipo.nome, tipo.unidade_medida, tipo.ativo] for tipo in tipos],
            ),
        )
        archive.writestr(
            "instaladores.csv",
            _csv_bytes(
                ["id", "nome", "matricula", "cpf", "ativo"],
                [[inst.id, inst.nome, inst.matricula, inst.cpf, inst.ativo] for inst in instaladores],
            ),
        )
        archive.writestr(
            "movimentacoes_material.csv",
            _csv_bytes(
                ["id", "tipo", "caixa", "hidrometro", "instalador", "solicitacao_id", "usuario", "criado_em", "observacoes"],
                [
                    [
                        mov.id,
                        mov.tipo.value,
                        mov.caixa.numero_interno if mov.caixa else "",
                        mov.hidrometro.numero_serie if mov.hidrometro else "",
                        mov.instalador.nome if mov.instalador else "",
                        mov.solicitacao_id or "",
                        mov.registrado_por.nome if mov.registrado_por else "",
                        format_datetime(mov.criado_em, with_seconds=True),
                        mov.observacoes or "",
                    ]
                    for mov in movimentos_material
                ],
            ),
        )
        archive.writestr(
            "movimentacoes_pecas.csv",
            _csv_bytes(
                ["id", "tipo", "peca", "quantidade", "instalador", "solicitacao_id", "usuario", "criado_em", "observacoes"],
                [
                    [
                        mov.id,
                        mov.tipo.value,
                        mov.tipo_peca.nome if mov.tipo_peca else "",
                        mov.quantidade,
                        mov.instalador.nome if mov.instalador else "",
                        mov.solicitacao_id or "",
                        mov.registrado_por.nome if mov.registrado_por else "",
                        format_datetime(mov.criado_em, with_seconds=True),
                        mov.observacoes or "",
                    ]
                    for mov in movimentos_peca
                ],
            ),
        )
        archive.writestr(
            "usuarios.csv",
            _csv_bytes(
                ["id", "nome", "email", "perfil", "ativo", "ultimo_acesso"],
                [[user.id, user.nome, user.email, user.role.value, user.ativo, format_datetime(user.ultimo_acesso, with_seconds=True)] for user in usuarios],
            ),
        )
        archive.writestr(
            "instalacoes_hidrometros.csv",
            _csv_bytes(
                ["id", "instalador", "hidrometro", "caixa", "solicitacao_id", "data_instalacao", "usuario_registro"],
                [
                    [
                        item.id,
                        item.instalador.nome if item.instalador else "",
                        item.hidrometro.numero_serie if item.hidrometro else "",
                        item.caixa.numero_interno if item.caixa else "",
                        item.solicitacao_id or "",
                        format_datetime(item.data_instalacao, with_seconds=True),
                        item.usuario_registro.nome if item.usuario_registro else "",
                    ]
                    for item in instalacoes
                ],
            ),
        )
        archive.writestr(
            "carcacas.csv",
            _csv_bytes(
                ["id", "tipo", "instalador", "quantidade", "data_movimento", "documento", "usuario", "observacao"],
                [
                    [
                        item.id,
                        item.tipo_movimento.value,
                        item.instalador.nome if item.instalador else "",
                        item.quantidade,
                        format_datetime(item.data_movimento, with_seconds=True),
                        item.documento_referencia or "",
                        item.usuario.nome if item.usuario else "",
                        item.observacao or "",
                    ]
                    for item in carcacas
                ],
            ),
        )
        archive.writestr(
            "transferencias_empresa.csv",
            _csv_bytes(
                ["id", "empresa_destino", "data_transferencia", "responsavel", "caixas", "pecas", "documento", "usuario"],
                [
                    [
                        item.id,
                        item.empresa_destino,
                        format_datetime(item.data_transferencia, with_seconds=True),
                        item.responsavel,
                        ", ".join(link.caixa.numero_interno for link in item.caixas if link.caixa),
                        ", ".join(f"{link.tipo_peca.nome}: {link.quantidade}" for link in item.pecas if link.tipo_peca),
                        item.documento_referencia or "",
                        item.usuario.nome if item.usuario else "",
                    ]
                    for item in transferencias
                ],
            ),
        )
        archive.writestr(
            "auditoria.csv",
            _csv_bytes(
                [
                    "id",
                    "acao",
                    "usuario",
                    "email",
                    "perfil",
                    "tabela",
                    "registro_id",
                    "criado_em",
                    "severidade",
                    "resultado",
                    "ip_cliente",
                    "ip_conexao",
                    "x_forwarded_for",
                    "navegador",
                    "user_agent_bruto",
                    "descricao",
                ],
                [
                    [
                        log.id,
                        log.acao,
                        log.usuario_nome or (log.usuario.nome if log.usuario else ""),
                        log.usuario_email or (log.usuario.email if log.usuario else ""),
                        log.usuario_perfil or "",
                        log.tabela or "",
                        log.registro_id or "",
                        format_datetime(log.criado_em, with_seconds=True),
                        log.severidade or "",
                        log.resultado or "",
                        log.ip_cliente or log.ip or "",
                        log.ip_conexao or "",
                        log.x_forwarded_for or "",
                        summarize_user_agent(log.user_agent),
                        log.user_agent or "",
                        log.descricao or "",
                    ]
                    for log in auditorias
                ],
            ),
        )

    output.seek(0)
    return filename, output
