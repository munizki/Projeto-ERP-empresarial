from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import or_

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.database import SessionLocal
from app.models import (
    AuditoriaLog,
    CarcacaMovimentacao,
    ConferenciaItem,
    ConferenciaInstaladorPeca,
    ConferenciaPecas,
    EstoquePeca,
    FeedbackOperacional,
    InstalacaoHidrometro,
    Instalador,
    InstaladorPeca,
    MovimentacaoMaterial,
    MovimentacaoPeca,
    Solicitacao,
    SolicitacaoItemCaixa,
    SolicitacaoItemPeca,
    SolicitacaoStatus,
    TipoPeca,
    UserRole,
    Usuario,
)
from app.security import criar_token, gerar_csrf_token, hash_senha
from app.utils import utc_now


warnings.filterwarnings("ignore", category=Warning)

PASSWORD = "LoadTest@2026!"
EMAIL_DOMAIN = "loadtest.gmf.local"
TEST_MARKER = "LOADTEST-OPERACIONAL"


@dataclass
class LoadIdentity:
    index: int
    usuario_id: int
    email: str
    senha: str
    solicitacao_id: int


@dataclass
class RequestMetric:
    user: int
    step: str
    method: str
    path: str
    status_code: int | None
    elapsed_ms: float
    error: str | None = None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(max(int(round((len(ordered) - 1) * percentile)), 0), len(ordered) - 1)
    return ordered[index]


def _delete_existing_loadtest_data(db) -> None:
    usuarios = db.query(Usuario).filter(Usuario.email.like(f"%@{EMAIL_DOMAIN}")).all()
    usuario_ids = [usuario.id for usuario in usuarios]
    instaladores = db.query(Instalador).filter(Instalador.matricula.like("LT-OP-%")).all()
    instalador_ids = [instalador.id for instalador in instaladores]
    solicitacao_conditions = [Solicitacao.observacoes.like(f"{TEST_MARKER}%")]
    if instalador_ids:
        solicitacao_conditions.append(Solicitacao.instalador_id.in_(instalador_ids))
    solicitacoes = db.query(Solicitacao).filter(or_(*solicitacao_conditions)).all()
    solicitacao_ids = [solicitacao.id for solicitacao in solicitacoes]
    tipos = db.query(TipoPeca).filter(TipoPeca.nome.like("LOADTEST-%")).all()
    tipo_ids = [tipo.id for tipo in tipos]

    audit_conditions = [AuditoriaLog.descricao.like(f"%{TEST_MARKER}%")]
    if usuario_ids:
        audit_conditions.append(AuditoriaLog.usuario_id.in_(usuario_ids))
    if solicitacao_ids:
        audit_conditions.append(
            (AuditoriaLog.tabela == Solicitacao.__tablename__) & AuditoriaLog.registro_id.in_(solicitacao_ids)
        )
    db.query(AuditoriaLog).filter(or_(*audit_conditions)).delete(synchronize_session=False)

    conferencia_ids: list[int] = []
    if instalador_ids or usuario_ids:
        conferencia_conditions = []
        conf_inst_conditions = []
        if instalador_ids:
            conferencia_conditions.append(ConferenciaPecas.instalador_id.in_(instalador_ids))
            conf_inst_conditions.append(ConferenciaInstaladorPeca.instalador_id.in_(instalador_ids))
        if usuario_ids:
            conferencia_conditions.append(ConferenciaPecas.responsavel_id.in_(usuario_ids))
            conf_inst_conditions.append(ConferenciaInstaladorPeca.usuario_id.in_(usuario_ids))
        conferencia_ids = [row[0] for row in db.query(ConferenciaPecas.id).filter(or_(*conferencia_conditions)).all()]
        db.query(ConferenciaInstaladorPeca).filter(or_(*conf_inst_conditions)).delete(synchronize_session=False)
        if conferencia_ids:
            db.query(ConferenciaItem).filter(ConferenciaItem.conferencia_id.in_(conferencia_ids)).delete(synchronize_session=False)
            db.query(ConferenciaPecas).filter(ConferenciaPecas.id.in_(conferencia_ids)).delete(synchronize_session=False)

    if solicitacao_ids:
        db.query(SolicitacaoItemCaixa).filter(SolicitacaoItemCaixa.solicitacao_id.in_(solicitacao_ids)).delete(synchronize_session=False)
        db.query(SolicitacaoItemPeca).filter(SolicitacaoItemPeca.solicitacao_id.in_(solicitacao_ids)).delete(synchronize_session=False)
        db.query(MovimentacaoMaterial).filter(MovimentacaoMaterial.solicitacao_id.in_(solicitacao_ids)).delete(synchronize_session=False)
        db.query(MovimentacaoPeca).filter(MovimentacaoPeca.solicitacao_id.in_(solicitacao_ids)).delete(synchronize_session=False)
        db.query(Solicitacao).filter(Solicitacao.id.in_(solicitacao_ids)).delete(synchronize_session=False)

    if instalador_ids:
        db.query(InstalacaoHidrometro).filter(InstalacaoHidrometro.instalador_id.in_(instalador_ids)).delete(synchronize_session=False)
        db.query(CarcacaMovimentacao).filter(CarcacaMovimentacao.instalador_id.in_(instalador_ids)).delete(synchronize_session=False)
        db.query(InstaladorPeca).filter(InstaladorPeca.instalador_id.in_(instalador_ids)).delete(synchronize_session=False)
        db.query(MovimentacaoPeca).filter(MovimentacaoPeca.instalador_id.in_(instalador_ids)).delete(synchronize_session=False)
        db.query(MovimentacaoMaterial).filter(MovimentacaoMaterial.instalador_id.in_(instalador_ids)).delete(synchronize_session=False)
        db.query(Instalador).filter(Instalador.id.in_(instalador_ids)).delete(synchronize_session=False)

    if usuario_ids:
        db.query(FeedbackOperacional).filter(
            or_(
                FeedbackOperacional.usuario_id.in_(usuario_ids),
                FeedbackOperacional.resolvido_por_id.in_(usuario_ids),
            )
        ).delete(synchronize_session=False)
        db.query(Usuario).filter(Usuario.id.in_(usuario_ids)).delete(synchronize_session=False)

    if tipo_ids:
        db.query(EstoquePeca).filter(EstoquePeca.tipo_peca_id.in_(tipo_ids)).delete(synchronize_session=False)
        db.query(TipoPeca).filter(TipoPeca.id.in_(tipo_ids)).delete(synchronize_session=False)

    db.commit()


def seed_loadtest_data(users: int, run_id: str) -> list[LoadIdentity]:
    with SessionLocal() as db:
        _delete_existing_loadtest_data(db)
        senha_hash = hash_senha(PASSWORD)
        manip = Usuario(
            nome=f"{TEST_MARKER} Manipulador",
            email=f"manip-{run_id}@{EMAIL_DOMAIN}",
            senha_hash=senha_hash,
            role=UserRole.MANIPULADOR,
            ativo=True,
            criado_em=utc_now(),
        )
        almox = Usuario(
            nome=f"{TEST_MARKER} Almoxarifado",
            email=f"almox-{run_id}@{EMAIL_DOMAIN}",
            senha_hash=senha_hash,
            role=UserRole.ALMOXARIFADO,
            ativo=True,
            criado_em=utc_now(),
        )
        db.add_all([manip, almox])
        db.flush()

        tipo = TipoPeca(
            nome=f"LOADTEST-PECA-{run_id}",
            descricao=f"{TEST_MARKER} peca temporaria para carga",
            unidade_medida="unidade",
            estoque_minimo_percentual=20,
            ativo=True,
            criado_em=utc_now(),
        )
        db.add(tipo)
        db.flush()
        db.add(EstoquePeca(tipo_peca_id=tipo.id, quantidade_atual=users * 2, quantidade_maxima=users * 4, atualizado_em=utc_now()))
        db.flush()

        identities: list[LoadIdentity] = []
        for index in range(1, users + 1):
            usuario = Usuario(
                nome=f"{TEST_MARKER} Instalador {index:03d}",
                email=f"instalador-{run_id}-{index:03d}@{EMAIL_DOMAIN}",
                senha_hash=senha_hash,
                role=UserRole.INSTALADOR,
                ativo=True,
                criado_em=utc_now(),
            )
            db.add(usuario)
            db.flush()
            instalador = Instalador(
                nome=f"{TEST_MARKER} Instalador {index:03d}",
                cpf=f"990{index:08d}",
                matricula=f"LT-OP-{run_id}-{index:03d}",
                usuario_id=usuario.id,
                ativo=True,
                criado_em=utc_now(),
            )
            db.add(instalador)
            db.flush()
            solicitacao = Solicitacao(
                instalador_id=instalador.id,
                status=SolicitacaoStatus.ENTREGUE,
                observacoes=f"{TEST_MARKER} {run_id} usuario {index:03d}",
                criado_por_id=manip.id,
                entregue_por_id=almox.id,
                criado_em=utc_now(),
                separado_em=utc_now(),
                entregue_em=utc_now(),
            )
            db.add(solicitacao)
            db.flush()
            db.add(SolicitacaoItemPeca(solicitacao_id=solicitacao.id, tipo_peca_id=tipo.id, quantidade_solicitada=1))
            identities.append(LoadIdentity(index=index, usuario_id=usuario.id, email=usuario.email, senha=PASSWORD, solicitacao_id=solicitacao.id))

        db.commit()
        return identities


def cleanup_loadtest_data() -> None:
    with SessionLocal() as db:
        _delete_existing_loadtest_data(db)


async def timed_request(
    metrics: list[RequestMetric],
    client: httpx.AsyncClient,
    user_index: int,
    step: str,
    method: str,
    path: str,
    **kwargs,
) -> httpx.Response | None:
    started = time.perf_counter()
    try:
        response = await client.request(method, path, **kwargs)
        elapsed = (time.perf_counter() - started) * 1000
        metrics.append(RequestMetric(user_index, step, method, path, response.status_code, elapsed))
        return response
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        metrics.append(RequestMetric(user_index, step, method, path, None, elapsed, repr(exc)))
        return None


async def run_user_flow(
    identity: LoadIdentity,
    base_url: str,
    host_header: str | None,
    start_event: asyncio.Event,
    auth_mode: str,
) -> list[RequestMetric]:
    metrics: list[RequestMetric] = []
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "user-agent": f"GMF-LoadTest-Mobile/2026 user-{identity.index:03d}",
    }
    if host_header:
        headers["host"] = host_header

    limits = httpx.Limits(max_keepalive_connections=4, max_connections=8)
    async with httpx.AsyncClient(
        base_url=base_url,
        verify=False,
        timeout=httpx.Timeout(20.0, connect=10.0),
        follow_redirects=False,
        headers=headers,
        limits=limits,
    ) as client:
        if auth_mode == "token":
            csrf = gerar_csrf_token()
            client.cookies.set("access_token", criar_token({"sub": str(identity.usuario_id), "ver": 0}), path="/")
            client.cookies.set("csrf_token", csrf, path="/")
            await start_event.wait()
        else:
            await start_event.wait()
            login_page = await timed_request(metrics, client, identity.index, "login_page", "GET", "/auth/login")
            csrf = client.cookies.get("csrf_token")
            if not csrf and login_page is not None:
                csrf = login_page.cookies.get("csrf_token")
            if not csrf:
                metrics.append(RequestMetric(identity.index, "csrf", "GET", "/auth/login", None, 0, "csrf_token ausente"))
                return metrics

            await timed_request(metrics, client, identity.index, "static_css", "GET", "/static/css/style.css")
            await timed_request(metrics, client, identity.index, "static_js", "GET", "/static/js/app.js")

            login = await timed_request(
                metrics,
                client,
                identity.index,
                "login_post",
                "POST",
                "/auth/login",
                data={"email": identity.email, "senha": identity.senha, "csrf_token": csrf},
                headers={"x-csrf-token": csrf, **({"host": host_header} if host_header else {})},
            )
            if login is None or login.status_code not in {302, 303}:
                return metrics

        for step, path in [
            ("entregas", "/instalador/entregas"),
            ("detalhe_entrega", f"/instalador/entregas/{identity.solicitacao_id}"),
            ("solicitacoes", "/instalador/solicitacoes"),
            ("carcacas", "/instalador/carcacas"),
            ("produtividade", "/instalador/produtividade"),
            ("session_ping", "/auth/session-ping"),
        ]:
            await timed_request(metrics, client, identity.index, step, "GET", path)

        if auth_mode != "token":
            csrf = client.cookies.get("csrf_token") or csrf
        confirm = await timed_request(
            metrics,
            client,
            identity.index,
            "confirmar_recebimento",
            "POST",
            f"/instalador/entregas/{identity.solicitacao_id}/confirmar",
            data={"csrf_token": csrf},
            headers={"x-csrf-token": csrf, **({"host": host_header} if host_header else {})},
        )
        if confirm is not None and confirm.status_code in {302, 303}:
            await timed_request(metrics, client, identity.index, "detalhe_confirmado", "GET", f"/instalador/entregas/{identity.solicitacao_id}?confirmado=1")

    return metrics


def summarize(metrics: list[RequestMetric], identities: list[LoadIdentity], started_at: float, finished_at: float) -> dict[str, Any]:
    by_step: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        bucket = by_step.setdefault(metric.step, {"count": 0, "errors": 0, "statuses": {}, "elapsed": []})
        bucket["count"] += 1
        bucket["statuses"][str(metric.status_code)] = bucket["statuses"].get(str(metric.status_code), 0) + 1
        if metric.error or metric.status_code is None or metric.status_code >= 400:
            bucket["errors"] += 1
        bucket["elapsed"].append(metric.elapsed_ms)

    for bucket in by_step.values():
        elapsed = bucket.pop("elapsed")
        bucket["avg_ms"] = round(statistics.mean(elapsed), 2) if elapsed else 0
        bucket["p50_ms"] = round(_percentile(elapsed, 0.50), 2)
        bucket["p95_ms"] = round(_percentile(elapsed, 0.95), 2)
        bucket["p99_ms"] = round(_percentile(elapsed, 0.99), 2)
        bucket["max_ms"] = round(max(elapsed), 2) if elapsed else 0

    total_errors = sum(1 for metric in metrics if metric.error or metric.status_code is None or metric.status_code >= 400)
    successful_confirms = sum(1 for metric in metrics if metric.step == "confirmar_recebimento" and metric.status_code in {302, 303})
    users_with_errors = sorted({metric.user for metric in metrics if metric.error or metric.status_code is None or metric.status_code >= 400})
    elapsed_seconds = max(finished_at - started_at, 0.001)

    return {
        "users": len(identities),
        "total_requests": len(metrics),
        "total_errors": total_errors,
        "users_with_errors": users_with_errors,
        "successful_confirmations": successful_confirms,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "requests_per_second": round(len(metrics) / elapsed_seconds, 2),
        "by_step": by_step,
        "sample_errors": [asdict(metric) for metric in metrics if metric.error or metric.status_code is None or metric.status_code >= 400][:10],
    }


async def async_main(args) -> dict[str, Any]:
    run_id = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    identities = seed_loadtest_data(args.users, run_id)
    start_event = asyncio.Event()
    started_at = time.perf_counter()
    try:
        tasks = [
            asyncio.create_task(run_user_flow(identity, args.base_url.rstrip("/"), args.host_header, start_event, args.auth_mode))
            for identity in identities
        ]
        start_event.set()
        nested_metrics = await asyncio.gather(*tasks)
        finished_at = time.perf_counter()
        metrics = [metric for user_metrics in nested_metrics for metric in user_metrics]
        summary = summarize(metrics, identities, started_at, finished_at)
        summary["run_id"] = run_id
        summary["auth_mode"] = args.auth_mode
        summary["base_url"] = args.base_url
        summary["host_header"] = args.host_header

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        return summary
    finally:
        if not args.keep_data:
            cleanup_loadtest_data()


def parse_args():
    parser = argparse.ArgumentParser(description="Simulacao operacional GMF com usuarios instaladores simultaneos.")
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--base-url", default="https://127.0.0.1")
    parser.add_argument("--host-header", default="")
    parser.add_argument("--output", default="")
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--auth-mode", choices=["login", "token"], default="login")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = asyncio.run(async_main(args))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
