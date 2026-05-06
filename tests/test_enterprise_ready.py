import os
import shutil
import tempfile
import threading
import unittest
from datetime import timedelta
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile


TEST_DB_PATH = Path(tempfile.gettempdir()) / "hidrometros_enterprise_ready.sqlite3"
TEST_BACKUP_DIR = Path(tempfile.gettempdir()) / "hidrometros_enterprise_backups"
TEST_UPLOAD_DIR = Path(tempfile.gettempdir()) / "hidrometros_enterprise_uploads"
os.environ["DATABASE_URL"] = os.getenv("ENTERPRISE_TEST_DATABASE_URL") or f"sqlite:///{TEST_DB_PATH.as_posix()}"
os.environ["SECRET_KEY"] = "enterprise-ready-test-secret"
os.environ["COOKIE_SECURE"] = "False"
os.environ["APP_ENV"] = "testing"
os.environ["ALLOWED_HOSTS"] = "testserver,localhost,127.0.0.1"
os.environ["BACKUP_DIR"] = str(TEST_BACKUP_DIR)
os.environ["FEEDBACK_UPLOAD_DIR"] = str(TEST_UPLOAD_DIR)

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    AuditoriaLog,
    BoxRuleConfig,
    CaixaHidrometro,
    CaixaStatus,
    CarcacaMovimentacao,
    CarcacaTipoMovimento,
    ConferenciaInstaladorPeca,
    ConferenciaPecas,
    EstoquePeca,
    FeedbackAnexo,
    FeedbackOperacional,
    Hidrometro,
    HidrometroManutencao,
    HidrometroStatus,
    InstalacaoHidrometro,
    Instalador,
    InstaladorPeca,
    MovimentacaoMaterial,
    MovimentacaoPeca,
    MovimentacaoTipo,
    ManutencaoHidrometroStatus,
    Solicitacao,
    SolicitacaoItemCaixa,
    SolicitacaoItemPeca,
    SolicitacaoStatus,
    TipoPeca,
    TransferenciaEmpresa,
    TransferenciaEmpresaCaixa,
    UserRole,
    Usuario,
)
from app.services.auditoria import registrar_auditoria
from app.services.hidrometros import aplicar_baixa_hidrometro
from app.security import criar_token, gerar_csrf_token, hash_senha
from app.services.estoque import carregar_solicitacao_operacional, confirmar_entrega_solicitacao, separar_solicitacao
from app.services.estoque_inteligente import montar_dashboard_estoque_inteligente
from app.services.manutencao_hidrometros import (
    DECISAO_DESCARTAR,
    DECISAO_VOLTAR_CAIXA_ORIGEM,
    REVERSAO_DESTINO_CAIXA_ORIGEM,
    abrir_manutencao_hidrometro,
    enviar_assistencia,
    listar_hidrometros_soltos_disponiveis,
    registrar_retorno_assistencia,
    reverter_manutencao_hidrometro,
    resumo_manutencao_hidrometros,
)
from app.services.schema import ensure_runtime_schema
from app.utils import get_request_audit_meta, summarize_user_agent, utc_now


@app.get("/__test_unhandled_error")
async def __test_unhandled_error():
    raise RuntimeError("erro intencional de teste")


class EnterpriseReadyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        TEST_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        if TEST_UPLOAD_DIR.exists():
            shutil.rmtree(TEST_UPLOAD_DIR)
        TEST_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        ensure_runtime_schema()

    def tearDown(self):
        with SessionLocal() as db:
            db.rollback()

    def test_audit_meta_prefers_lan_ip_over_docker_proxy_and_summarizes_browser(self):
        class DummyClient:
            host = "172.18.0.1"

        class DummyRequest:
            headers = {
                "x-forwarded-for": "172.18.0.1, 192.168.130.51",
                "x-real-ip": "172.18.0.1",
                "user-agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
                ),
            }
            client = DummyClient()

        meta = get_request_audit_meta(DummyRequest())
        self.assertEqual(meta["ip_cliente"], "192.168.130.51")
        self.assertEqual(meta["ip_conexao"], "172.18.0.1")
        self.assertEqual(summarize_user_agent(meta["user_agent"]), "Microsoft Edge 147 no Windows 10/11")

    def test_non_admin_session_expires_by_idle_time_and_admin_stays_logged_in(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="idle-admin@example.com")
            operador = self._make_user(
                db,
                nome="Operador Inativo",
                email="idle-operador@example.com",
                role=UserRole.MANIPULADOR,
            )
            admin.ultimo_acesso = utc_now() - timedelta(hours=8)
            operador.ultimo_acesso = utc_now() - timedelta(minutes=31)
            db.commit()
            admin_cookies = self._auth_cookies(admin)
            operador_cookies = self._auth_cookies(operador)
            operador_id = operador.id

        expirado = self.client.get("/dashboard", cookies=operador_cookies, follow_redirects=False)
        self.assertEqual(expirado.status_code, 302)
        self.assertEqual(expirado.headers.get("location"), "/auth/login?expirou=1")

        admin_page = self.client.get("/dashboard", cookies=admin_cookies, follow_redirects=False)
        self.assertEqual(admin_page.status_code, 200)

        with SessionLocal() as db:
            evento = db.query(AuditoriaLog).filter(
                AuditoriaLog.acao == "SESSAO_EXPIRADA_INATIVIDADE",
                AuditoriaLog.usuario_id == operador_id,
            ).first()
            self.assertIsNotNone(evento)

    def test_active_non_admin_session_ping_renews_activity(self):
        with SessionLocal() as db:
            operador = self._make_user(
                db,
                nome="Operador Ativo",
                email="idle-ativo@example.com",
                role=UserRole.ALMOXARIFADO,
            )
            operador.ultimo_acesso = utc_now() - timedelta(minutes=10)
            db.commit()
            cookies = self._auth_cookies(operador)
            operador_id = operador.id

        response = self.client.get("/auth/session-ping", cookies=cookies, follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["idle_timeout_seconds"], 1800)
        self.assertTrue(response.cookies.get("access_token"))

        with SessionLocal() as db:
            operador = db.query(Usuario).filter(Usuario.id == operador_id).first()
            self.assertGreater(operador.ultimo_acesso, utc_now() - timedelta(minutes=2))

    def test_non_admin_login_invalidates_previous_session_but_admin_keeps_parallel_access(self):
        with SessionLocal() as db:
            admin = self._make_user(db, nome="Admin Paralelo", email="admin-paralelo@example.com", role=UserRole.ADMIN)
            operador = self._make_user(
                db,
                nome="Operador Sessao Unica",
                email="sessao-unica@example.com",
                role=UserRole.ALMOXARIFADO,
            )
            db.commit()
            admin_id = admin.id
            operador_id = operador.id

        client = TestClient(app)

        def login_cookie(email: str) -> dict[str, str]:
            csrf_token = gerar_csrf_token()
            response = client.post(
                "/auth/login",
                data={"email": email, "senha": "123456", "csrf_token": csrf_token},
                cookies={"csrf_token": csrf_token},
                follow_redirects=False,
            )
            self.assertEqual(response.status_code, 302)
            return {"access_token": response.cookies.get("access_token")}

        primeiro_operador = login_cookie("sessao-unica@example.com")
        segundo_operador = login_cookie("sessao-unica@example.com")

        client.cookies.clear()
        antigo = client.get("/dashboard", cookies=primeiro_operador, follow_redirects=False)
        self.assertIn(antigo.status_code, {302, 303})
        self.assertEqual(antigo.headers.get("location"), "/auth/login?limite_sessao=1")
        aviso_limite = client.get(antigo.headers["location"])
        self.assertIn("Limite de conta conectada excedido", aviso_limite.text)

        client.cookies.clear()
        atual = client.get("/dashboard", cookies=segundo_operador, follow_redirects=False)
        self.assertEqual(atual.status_code, 200)

        primeiro_admin = login_cookie("admin-paralelo@example.com")
        segundo_admin = login_cookie("admin-paralelo@example.com")
        client.cookies.clear()
        self.assertEqual(client.get("/dashboard", cookies=primeiro_admin, follow_redirects=False).status_code, 200)
        client.cookies.clear()
        self.assertEqual(client.get("/dashboard", cookies=segundo_admin, follow_redirects=False).status_code, 200)

        with SessionLocal() as db:
            operador = db.query(Usuario).filter(Usuario.id == operador_id).first()
            admin = db.query(Usuario).filter(Usuario.id == admin_id).first()
            self.assertEqual(operador.token_version, 2)
            self.assertEqual(admin.token_version, 0)
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "SESSAO_UNICA_LOGIN").first())
            self.assertIsNotNone(
                db.query(AuditoriaLog)
                .filter(AuditoriaLog.acao == "SESSAO_INVALIDADA_LOGIN_POSTERIOR")
                .first()
            )

    def test_admin_can_monitor_active_users_and_disconnect_for_maintenance_with_warning(self):
        with SessionLocal() as db:
            admin = self._make_user(db, nome="Admin Monitor", email="admin-monitor@example.com", role=UserRole.ADMIN)
            operador = self._make_user(
                db,
                nome="Operador Ativo",
                email="operador-ativo@example.com",
                role=UserRole.ALMOXARIFADO,
            )
            operador.ultimo_acesso = utc_now()
            db.commit()
            admin_cookies = self._auth_cookies(admin)
            operador_cookies = self._auth_cookies(operador)
            operador_id = operador.id

        page = self.client.get("/admin/usuarios-ativos", cookies=admin_cookies)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Usuarios ativos no sistema", page.text)
        self.assertIn("Operador Ativo", page.text)
        self.assertIn("Desconectar operacionais ativos", page.text)
        self.assertNotIn("DESCONECTAR TODOS", page.text)
        self.assertNotIn('placeholder="DESCONECTAR"', page.text)

        response = self._post(
            f"/admin/usuarios/{operador_id}/desconectar",
            data={"justificativa": "Manutencao programada"},
            cookies=admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("location"), "/admin/usuarios-ativos?desconectado=1")

        invalidada = self.client.get("/dashboard", cookies=operador_cookies, follow_redirects=False)
        self.assertIn(invalidada.status_code, {302, 303})
        self.assertEqual(invalidada.headers.get("location"), "/auth/login?desconectado_admin=1")
        aviso = self.client.get(invalidada.headers["location"])
        self.assertIn("administrador desconectou sua sessao", aviso.text)
        self.assertIn("manutencao programada", aviso.text)

        auditoria = self.client.get("/admin/auditoria?severidade=CRITICO", cookies=admin_cookies)
        self.assertEqual(auditoria.status_code, 200)
        self.assertIn("ADMIN_DESCONECTA_USUARIO", auditoria.text)

        with SessionLocal() as db:
            operador = db.query(Usuario).filter(Usuario.id == operador_id).first()
            self.assertEqual(operador.token_version, 1)
            self.assertEqual(operador.session_notice_code, "ADMIN_DISCONNECT")
            self.assertIsNotNone(
                db.query(AuditoriaLog)
                .filter(AuditoriaLog.acao == "ADMIN_DESCONECTA_USUARIO")
                .first()
            )
            self.assertIsNotNone(
                db.query(AuditoriaLog)
                .filter(AuditoriaLog.acao == "SESSAO_INVALIDADA_ADMIN")
                .first()
            )

    def _make_user(self, db, *, nome="Admin", email="admin@example.com", role=UserRole.ADMIN, matricula=None):
        usuario = Usuario(
            nome=nome,
            email=email,
            matricula=matricula,
            senha_hash=hash_senha("123456"),
            role=role,
            ativo=True,
            criado_em=utc_now(),
        )
        db.add(usuario)
        db.flush()
        return usuario

    def _auth_cookies(self, usuario, *, advanced=False):
        cookies = {"access_token": criar_token({"sub": str(usuario.id), "ver": int(getattr(usuario, "token_version", 0) or 0)})}
        if advanced:
            cookies["modo_avancado"] = "1"
        return cookies

    def _post(self, path, *, data=None, cookies=None, follow_redirects=False):
        final_cookies = dict(cookies or {})
        csrf_token = final_cookies.get("csrf_token")
        if not csrf_token:
            csrf_token = gerar_csrf_token()
            final_cookies["csrf_token"] = csrf_token

        payload = dict(data or {})
        payload.setdefault("csrf_token", csrf_token)
        return self.client.post(path, data=payload, cookies=final_cookies, follow_redirects=follow_redirects)

    def _skip_unless_postgresql(self):
        if engine.dialect.name != "postgresql":
            self.skipTest("Teste de concorrencia real exige PostgreSQL.")

    def _make_instalador(self, db, *, nome="Instalador Teste", matricula="MAT-001", cpf="12345678901"):
        instalador = Instalador(
            nome=nome,
            cpf=cpf,
            matricula=matricula,
            ativo=True,
            criado_em=utc_now(),
        )
        db.add(instalador)
        db.flush()
        return instalador

    def _make_caixa(self, db, *, numero="CX-001", serial="BOX-001"):
        caixa = CaixaHidrometro(
            numero_interno=numero,
            serial_number=serial,
            status=CaixaStatus.EM_ESTOQUE,
            ativo=True,
            criado_em=utc_now(),
            atualizado_em=utc_now(),
        )
        db.add(caixa)
        db.flush()

        for index in range(1, 7):
            db.add(
                Hidrometro(
                    numero_serie=f"{numero}-H{index}",
                    caixa_id=caixa.id,
                    status=HidrometroStatus.EM_ESTOQUE,
                    criado_em=utc_now(),
                    atualizado_em=utc_now(),
                )
            )

        db.flush()
        return caixa

    def _make_tipo_peca(self, db, *, nome="LACRE", quantidade=10):
        tipo = TipoPeca(
            nome=nome,
            descricao="Peca de teste",
            unidade_medida="unidade",
            estoque_minimo_percentual=20,
            ativo=True,
            criado_em=utc_now(),
        )
        db.add(tipo)
        db.flush()
        estoque = EstoquePeca(
            tipo_peca_id=tipo.id,
            quantidade_atual=quantidade,
            quantidade_maxima=20,
            atualizado_em=utc_now(),
        )
        db.add(estoque)
        db.flush()
        return tipo

    def _make_solicitacao_entregue(self, db, *, instalador, criado_por, entregue_por, caixa, tipo_peca=None):
        solicitacao = Solicitacao(
            instalador_id=instalador.id,
            criado_por_id=criado_por.id,
            observacoes="Entrega de teste para portal",
            criado_em=utc_now(),
        )
        db.add(solicitacao)
        db.flush()
        db.add(SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1))
        if tipo_peca:
            db.add(SolicitacaoItemPeca(solicitacao_id=solicitacao.id, tipo_peca_id=tipo_peca.id, quantidade_solicitada=1))
        db.flush()
        solicitacao = carregar_solicitacao_operacional(db, solicitacao.id)
        separar_solicitacao(db, solicitacao, {solicitacao.itens_caixa[0].id: caixa.id})
        confirmar_entrega_solicitacao(db, solicitacao, entregue_por.id, observacoes="Entrega portal")
        db.flush()
        return solicitacao

    def test_admin_can_edit_and_delete_clean_box(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="box-admin@example.com")
            caixa = self._make_caixa(db, numero="CX-TESTE-01", serial="BOX-TESTE-01")
            caixa_id = caixa.id
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        response = self.client.get(f"/almoxarifado/caixas/{caixa_id}/editar", cookies=cookies)
        self.assertEqual(response.status_code, 200)

        response = self._post(
            f"/almoxarifado/caixas/{caixa_id}/editar",
            data={"serial_number": "BOX-TESTE-99", "numero_interno": "CX-TESTE-99"},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            caixa = db.query(CaixaHidrometro).filter(CaixaHidrometro.id == caixa_id).first()
            self.assertIsNotNone(caixa)
            self.assertEqual(caixa.numero_interno, "CX-TESTE-99")
            self.assertEqual(caixa.serial_number, "BOX-TESTE-99")

        response = self._post(
            f"/admin/limpeza/caixas/{caixa_id}",
            data={
                "justificativa": "Limpeza de implantacao",
                "confirmacao": "CX-TESTE-99",
                "confirmacao_final": "1",
                "next_path": f"/almoxarifado/caixas/{caixa_id}",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("location"), "/almoxarifado/caixas?limpeza=caixa_excluida")

        response = self.client.get(response.headers["location"], cookies=cookies)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Caixa apagada definitivamente", response.text)

        with SessionLocal() as db:
            caixa = db.query(CaixaHidrometro).filter(CaixaHidrometro.id == caixa_id).first()
            hidrometros = db.query(Hidrometro).filter(Hidrometro.numero_serie.like("CX-TESTE-01-%")).count()
            self.assertIsNone(caixa)
            self.assertEqual(hidrometros, 0)

    def test_admin_can_change_box_rule_without_touching_old_boxes(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="rule-admin@example.com")
            caixa_antiga = self._make_caixa(db, numero="CX-RULE-OLD", serial="BOX-RULE-OLD")
            old_id = caixa_antiga.id
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        response = self._post(
            "/admin/regras-caixa",
            data={
                "quantidade_hidrometros": "8",
                "justificativa": "Mudanca operacional homologada",
                "confirmacao": "ALTERAR",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        form_page = self.client.get("/almoxarifado/caixas/nova", cookies=cookies)
        self.assertEqual(form_page.status_code, 200)
        self.assertIn('name="hidrometro_8"', form_page.text)

        payload = {
            "serial_number": "BOX-RULE-NEW",
            "numero_interno": "CX-RULE-NEW",
        }
        for index in range(1, 9):
            payload[f"hidrometro_{index}"] = f"CX-RULE-NEW-H{index}"

        create_response = self._post(
            "/almoxarifado/caixas/nova",
            data=payload,
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(create_response.status_code, 302)

        with SessionLocal() as db:
            antiga = db.query(CaixaHidrometro).filter(CaixaHidrometro.id == old_id).first()
            nova = db.query(CaixaHidrometro).filter(CaixaHidrometro.numero_interno == "CX-RULE-NEW").first()
            self.assertEqual(antiga.quantidade_esperada, 6)
            self.assertEqual(db.query(Hidrometro).filter(Hidrometro.caixa_id == antiga.id).count(), 6)
            self.assertEqual(nova.quantidade_esperada, 8)
            self.assertEqual(db.query(Hidrometro).filter(Hidrometro.caixa_id == nova.id).count(), 8)
            self.assertEqual(db.query(BoxRuleConfig).filter(BoxRuleConfig.ativo == True).count(), 1)
            self.assertEqual(db.query(BoxRuleConfig).filter(BoxRuleConfig.ativo == True).first().quantidade_hidrometros, 8)
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ADMIN_UPDATE_REGRA_CAIXA").first())

    def test_admin_user_forms_enforce_single_active_admin(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="single-admin@example.com")
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        response = self._post(
            "/admin/usuarios/novo",
            data={
                "nome": "Segundo Admin",
                "email": "segundo.admin@example.com",
                "senha": "SenhaForte!123",
                "role": "admin",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("somente 1 ADMIN ativo", response.text)

        response = self._post(
            "/admin/usuarios/novo",
            data={
                "nome": "Operador Seguro",
                "email": "operador.seguro@example.com",
                "senha": "SenhaForte!123",
                "role": "manipulador",
                "matricula": "MAT-USR-01",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        with SessionLocal() as db:
            usuario = db.query(Usuario).filter(Usuario.email == "operador.seguro@example.com").first()
            self.assertEqual(usuario.matricula, "MAT-USR-01")

    def test_admin_can_create_instalador_user_and_link_operational_record(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="installer-admin@example.com")
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        response = self._post(
            "/admin/usuarios/novo",
            data={
                "nome": "Instalador Mobile",
                "email": "instalador.mobile@example.com",
                "senha": "SenhaForte!123",
                "role": "instalador",
                "matricula": "MAT-MOB-01",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            usuario = db.query(Usuario).filter(Usuario.email == "instalador.mobile@example.com").first()
            self.assertIsNotNone(usuario)
            self.assertEqual(usuario.role, UserRole.INSTALADOR)
            self.assertEqual(usuario.matricula, "MAT-MOB-01")
            instalador = db.query(Instalador).filter(Instalador.matricula == "MAT-MOB-01").first()
            self.assertIsNotNone(instalador)
            self.assertEqual(instalador.usuario_id, usuario.id)
            self.assertEqual(instalador.matricula, "MAT-MOB-01")
            self.assertIsNone(instalador.cpf)
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "CREATE_INSTALADOR").first())

        page = self.client.get("/manipulador/instaladores", cookies=cookies)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Instalador Mobile", page.text)

    def test_instalador_portal_is_scoped_and_confirms_delivery(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="portal-admin@example.com")
            almox = self._make_user(db, nome="Almox Portal", email="portal-almox@example.com", role=UserRole.ALMOXARIFADO)
            manipulador = self._make_user(db, nome="Manip Portal", email="portal-man@example.com", role=UserRole.MANIPULADOR)
            user_instalador = self._make_user(db, nome="Instalador Um", email="instalador.um@example.com", role=UserRole.INSTALADOR)
            user_outro = self._make_user(db, nome="Instalador Dois", email="instalador.dois@example.com", role=UserRole.INSTALADOR)
            instalador = self._make_instalador(db, nome="Instalador Um", matricula="MAT-PORT-01", cpf="12345678952")
            outro_instalador = self._make_instalador(db, nome="Instalador Dois", matricula="MAT-PORT-02", cpf="12345678953")
            instalador.usuario_id = user_instalador.id
            outro_instalador.usuario_id = user_outro.id
            caixa = self._make_caixa(db, numero="CX-PORT-01", serial="BOX-PORT-01")
            outra_caixa = self._make_caixa(db, numero="CX-PORT-02", serial="BOX-PORT-02")
            solicitacao = self._make_solicitacao_entregue(
                db,
                instalador=instalador,
                criado_por=manipulador,
                entregue_por=almox,
                caixa=caixa,
            )
            outra_solicitacao = self._make_solicitacao_entregue(
                db,
                instalador=outro_instalador,
                criado_por=manipulador,
                entregue_por=almox,
                caixa=outra_caixa,
            )
            solicitacao_id = solicitacao.id
            outra_solicitacao_id = outra_solicitacao.id
            instalador_id = instalador.id
            db.commit()
            cookies_instalador = self._auth_cookies(user_instalador)
            cookies_almox = self._auth_cookies(almox)
            cookies_admin = self._auth_cookies(admin, advanced=True)

        dashboard = self.client.get("/dashboard", cookies=cookies_instalador, follow_redirects=False)
        self.assertEqual(dashboard.status_code, 302)
        self.assertEqual(dashboard.headers.get("location"), "/instalador/entregas")

        entregas = self.client.get("/instalador/entregas", cookies=cookies_instalador)
        self.assertEqual(entregas.status_code, 200)
        self.assertIn(f"Solicitacao #{solicitacao_id}", entregas.text)
        self.assertNotIn("Instalador Dois", entregas.text)

        admin_selector = self.client.get("/admin/portal-instaladores", cookies=cookies_admin)
        self.assertEqual(admin_selector.status_code, 200)
        self.assertIn("Portal Instalador", admin_selector.text)
        self.assertIn(f"/instalador/entregas?instalador_id={instalador_id}", admin_selector.text)

        admin_preview = self.client.get(f"/instalador/entregas?instalador_id={instalador_id}", cookies=cookies_admin)
        self.assertEqual(admin_preview.status_code, 200)
        self.assertIn("Visualizacao admin", admin_preview.text)
        self.assertIn(f"Solicitacao #{solicitacao_id}", admin_preview.text)

        admin_preview_detail = self.client.get(
            f"/instalador/entregas/{solicitacao_id}?instalador_id={instalador_id}",
            cookies=cookies_admin,
        )
        self.assertEqual(admin_preview_detail.status_code, 200)
        self.assertIn(f"/instalador/entregas/{solicitacao_id}/confirmar?instalador_id={instalador_id}", admin_preview_detail.text)

        acesso_outro = self.client.get(f"/instalador/entregas/{outra_solicitacao_id}", cookies=cookies_instalador)
        self.assertEqual(acesso_outro.status_code, 403)

        admin_bloqueado = self.client.get("/admin/usuarios", cookies=cookies_instalador, follow_redirects=False)
        self.assertIn(admin_bloqueado.status_code, {302, 403})
        baixa_bloqueada = self.client.get("/manipulador/baixa-hidrometro", cookies=cookies_instalador, follow_redirects=False)
        self.assertIn(baixa_bloqueada.status_code, {302, 403})

        confirmacao = self._post(
            f"/instalador/entregas/{solicitacao_id}/confirmar",
            cookies=cookies_instalador,
            follow_redirects=False,
        )
        self.assertEqual(confirmacao.status_code, 302)

        with SessionLocal() as db:
            solicitacao = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
            self.assertEqual(solicitacao.recebimento_instalador_status, "confirmado")
            self.assertIsNotNone(solicitacao.confirmacao_instalador_em)
            self.assertEqual(solicitacao.usuario_confirmacao_instalador_id, user_instalador.id)
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "INSTALADOR_CONFIRMA_RECEBIMENTO").first())
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "INSTALADOR_ACESSO_NEGADO_REGISTRO").first())

        almox_page = self.client.get("/almoxarifado/confirmacoes-instalador?status_recebimento=confirmado", cookies=cookies_almox)
        self.assertEqual(almox_page.status_code, 200)
        self.assertIn("Instalador Um", almox_page.text)

        report = self.client.get("/relatorios/confirmacoes-instalador", cookies=cookies_admin)
        self.assertEqual(report.status_code, 200)
        self.assertIn("Instalador Um", report.text)
        export = self.client.get("/relatorios/confirmacoes-instalador/exportar.xlsx", cookies=cookies_admin)
        self.assertEqual(export.status_code, 200)
        self.assertTrue(export.content.startswith(b"PK"))

    def test_instalador_divergencia_carcacas_produtividade_and_reports(self):
        with SessionLocal() as db:
            almox = self._make_user(db, nome="Almox Divergencia", email="div-almox@example.com", role=UserRole.ALMOXARIFADO)
            manipulador = self._make_user(db, nome="Manip Divergencia", email="div-man@example.com", role=UserRole.MANIPULADOR)
            user_instalador = self._make_user(db, nome="Instalador Divergente", email="div-inst@example.com", role=UserRole.INSTALADOR)
            user_outro = self._make_user(db, nome="Outro Campo", email="div-outro@example.com", role=UserRole.INSTALADOR)
            instalador = self._make_instalador(db, nome="Instalador Divergente", matricula="MAT-DIV-01", cpf="12345678954")
            outro_instalador = self._make_instalador(db, nome="Outro Campo", matricula="MAT-DIV-02", cpf="12345678955")
            instalador.usuario_id = user_instalador.id
            outro_instalador.usuario_id = user_outro.id
            caixa = self._make_caixa(db, numero="CX-DIV-01", serial="BOX-DIV-01")
            caixa_prod = self._make_caixa(db, numero="CX-DIV-PROD", serial="BOX-DIV-PROD")
            solicitacao = self._make_solicitacao_entregue(
                db,
                instalador=instalador,
                criado_por=manipulador,
                entregue_por=almox,
                caixa=caixa,
            )
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa_prod.id).order_by(Hidrometro.id).first()
            db.add(
                InstalacaoHidrometro(
                    instalador_id=instalador.id,
                    hidrometro_id=hidrometro.id,
                    caixa_id=caixa_prod.id,
                    solicitacao_id=solicitacao.id,
                    data_instalacao=utc_now(),
                    usuario_registro_id=manipulador.id,
                )
            )
            db.add(
                CarcacaMovimentacao(
                    tipo_movimento=CarcacaTipoMovimento.DEVOLUCAO_INSTALADOR,
                    instalador_id=instalador.id,
                    quantidade=3,
                    data_movimento=utc_now(),
                    documento_referencia="FORM-DIV",
                    observacao="Devolucao propria",
                    usuario_id=almox.id,
                )
            )
            db.add(
                CarcacaMovimentacao(
                    tipo_movimento=CarcacaTipoMovimento.DEVOLUCAO_INSTALADOR,
                    instalador_id=outro_instalador.id,
                    quantidade=8,
                    data_movimento=utc_now(),
                    documento_referencia="FORM-OUTRO",
                    observacao="Nao deve aparecer",
                    usuario_id=almox.id,
                )
            )
            solicitacao_id = solicitacao.id
            db.commit()
            cookies_instalador = self._auth_cookies(user_instalador)
            cookies_almox = self._auth_cookies(almox)

        sem_motivo = self._post(
            f"/instalador/entregas/{solicitacao_id}/divergencia",
            data={"motivo": ""},
            cookies=cookies_instalador,
            follow_redirects=False,
        )
        self.assertEqual(sem_motivo.status_code, 400)
        self.assertIn("Informe o motivo", sem_motivo.text)

        divergencia = self._post(
            f"/instalador/entregas/{solicitacao_id}/divergencia",
            data={"motivo": "Faltou lacre na entrega"},
            cookies=cookies_instalador,
            follow_redirects=False,
        )
        self.assertEqual(divergencia.status_code, 302)

        for path, expected, forbidden in [
            ("/instalador/solicitacoes", f"Solicitacao #{solicitacao_id}", "Outro Campo"),
            ("/instalador/carcacas", "3 carcaca", "Nao deve aparecer"),
            ("/instalador/produtividade", "CX-DIV-PROD-H1", "Outro Campo"),
        ]:
            page = self.client.get(path, cookies=cookies_instalador)
            self.assertEqual(page.status_code, 200, msg=path)
            self.assertIn(expected, page.text)
            self.assertNotIn(forbidden, page.text)

        with SessionLocal() as db:
            solicitacao = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
            self.assertEqual(solicitacao.recebimento_instalador_status, "divergencia")
            self.assertEqual(solicitacao.motivo_divergencia_instalador, "Faltou lacre na entrega")
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "INSTALADOR_DIVERGENCIA_RECEBIMENTO").first())

        almox_page = self.client.get("/almoxarifado/confirmacoes-instalador?status_recebimento=divergencia", cookies=cookies_almox)
        self.assertEqual(almox_page.status_code, 200)
        self.assertIn("Faltou lacre na entrega", almox_page.text)

    def test_admin_can_delete_clean_user_and_see_it_in_auditoria(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="user-admin@example.com")
            usuario_limpo = self._make_user(
                db,
                nome="Usuario Limpo",
                email="usuario.limpo@example.com",
                role=UserRole.MANIPULADOR,
            )
            usuario_limpo_id = usuario_limpo.id
            usuario_limpo_email = usuario_limpo.email
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        response = self._post(
            f"/admin/limpeza/usuarios/{usuario_limpo_id}",
            data={
                "justificativa": "Limpeza de implantacao",
                "confirmacao": usuario_limpo_email,
                "confirmacao_final": "1",
                "next_path": "/admin/usuarios",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("location"), "/admin/usuarios?limpeza=usuario_excluido")

        response = self.client.get("/admin/auditoria?acao=ADMIN_DELETE_USUARIO", cookies=cookies)
        self.assertEqual(response.status_code, 200)
        self.assertIn("ADMIN_DELETE_USUARIO", response.text)
        self.assertIn(usuario_limpo_email, response.text)

        with SessionLocal() as db:
            usuario_limpo = db.query(Usuario).filter(Usuario.id == usuario_limpo_id).first()
            log = db.query(AuditoriaLog).filter(
                AuditoriaLog.acao == "ADMIN_DELETE_USUARIO",
                AuditoriaLog.registro_id == usuario_limpo_id,
            ).first()
            self.assertIsNone(usuario_limpo)
            self.assertIsNotNone(log)
            self.assertIn(usuario_limpo_email, log.descricao)

    def test_admin_can_delete_delivered_solicitacao_with_full_rollback(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="sol-admin@example.com")
            instalador = self._make_instalador(db, matricula="MAT-SOL-01", cpf="12345678902")
            tipo = self._make_tipo_peca(db, nome="VEDACAO", quantidade=10)
            caixa = self._make_caixa(db, numero="CX-SOL-01", serial="BOX-SOL-01")

            solicitacao = Solicitacao(
                instalador_id=instalador.id,
                criado_por_id=admin.id,
                observacoes="Solicitacao de teste",
                criado_em=utc_now(),
            )
            db.add(solicitacao)
            db.flush()
            db.add(SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1))
            db.add(SolicitacaoItemPeca(solicitacao_id=solicitacao.id, tipo_peca_id=tipo.id, quantidade_solicitada=2))
            db.commit()

            solicitacao = carregar_solicitacao_operacional(db, solicitacao.id)
            self.assertIsNotNone(solicitacao)
            separar_solicitacao(db, solicitacao, {solicitacao.itens_caixa[0].id: caixa.id})
            confirmar_entrega_solicitacao(db, solicitacao, admin.id, observacoes="Entrega para teste")
            db.commit()

            solicitacao_id = solicitacao.id
            caixa_id = caixa.id
            instalador_id = instalador.id
            tipo_id = tipo.id
            cookies = self._auth_cookies(admin, advanced=True)

        response = self._post(
            f"/admin/limpeza/solicitacoes/{solicitacao_id}",
            data={
                "justificativa": "Limpeza final antes da implantacao",
                "confirmacao": str(solicitacao_id),
                "confirmacao_final": "1",
                "next_path": "/manipulador/solicitacoes",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            solicitacao = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
            caixa = db.query(CaixaHidrometro).filter(CaixaHidrometro.id == caixa_id).first()
            estoque = db.query(EstoquePeca).filter(EstoquePeca.tipo_peca_id == tipo_id).first()
            posse = db.query(InstaladorPeca).filter(
                InstaladorPeca.instalador_id == instalador_id,
                InstaladorPeca.tipo_peca_id == tipo_id,
            ).first()
            movimentos_material = db.query(MovimentacaoMaterial).filter(MovimentacaoMaterial.solicitacao_id == solicitacao_id).count()
            movimentos_peca = db.query(MovimentacaoPeca).filter(MovimentacaoPeca.solicitacao_id == solicitacao_id).count()
            hidrometros = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa_id).all()

            self.assertIsNone(solicitacao)
            self.assertIsNotNone(caixa)
            self.assertEqual(caixa.status, CaixaStatus.EM_ESTOQUE)
            self.assertIsNone(caixa.instalador_id)
            self.assertEqual(estoque.quantidade_atual, 10)
            self.assertIsNone(posse)
            self.assertEqual(movimentos_material, 0)
            self.assertEqual(movimentos_peca, 0)
            self.assertTrue(all(h.status == HidrometroStatus.EM_ESTOQUE for h in hidrometros))
            self.assertTrue(all(h.instalador_id is None for h in hidrometros))

    def test_reports_exports_and_clean_instalador_delete_work(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="report-admin@example.com")
            self._make_instalador(db, nome="Carlos Lima", matricula="MAT-REP-01", cpf="12345678903")
            self._make_caixa(db, numero="CX-REP-01", serial="BOX-REP-01")
            self._make_tipo_peca(db, nome="LACRE", quantidade=15)

            instalador_limpo = self._make_instalador(db, nome="Instalador Limpo", matricula="MAT-LIMPO", cpf="12345678904")
            instalador_limpo_id = instalador_limpo.id
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        for path in [
            "/dashboard",
            "/relatorios",
            "/relatorios/estoque",
            "/relatorios/baixas",
            "/relatorios/movimentacoes",
            "/relatorios/divergencias",
            "/relatorios/confirmacoes-instalador",
            "/relatorios/estoque-inteligente",
            "/relatorios/portal-instaladores",
            "/almoxarifado/caixas",
            "/almoxarifado/estoque",
            "/almoxarifado/manutencao",
            "/almoxarifado/confirmacoes-instalador",
            "/manipulador/instaladores",
            "/admin/instaladores",
        ]:
            response = self.client.get(path, cookies=cookies)
            self.assertEqual(response.status_code, 200, msg=path)

        instaladores_page = self.client.get("/admin/instaladores", cookies=cookies)
        self.assertNotIn("CPF</th>", instaladores_page.text)
        self.assertNotIn("Nascimento", instaladores_page.text)
        self.assertNotIn("12345678903", instaladores_page.text)

        login_page = self.client.get("/auth/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn("GMF", login_page.text)
        self.assertIn("Gestao de Medicao e Faturamento Ltda", login_page.text)

        dashboard_page = self.client.get("/dashboard", cookies=cookies)
        self.assertEqual(dashboard_page.status_code, 200)
        self.assertIn("GMF", dashboard_page.text)

        for path in [
            "/almoxarifado/caixas/exportar.xlsx",
            "/almoxarifado/estoque/exportar.xlsx",
            "/almoxarifado/manutencao/exportar.xlsx",
            "/almoxarifado/confirmacoes-instalador/exportar.xlsx",
            "/relatorios/baixas/exportar.xlsx",
            "/relatorios/confirmacoes-instalador/exportar.xlsx",
            "/relatorios/estoque-inteligente/exportar.xlsx",
            "/relatorios/portal-instaladores/exportar.xlsx",
            "/manipulador/instaladores/exportar.xlsx",
        ]:
            response = self.client.get(path, cookies=cookies)
            self.assertEqual(response.status_code, 200, msg=path)
            self.assertIn("spreadsheetml", response.headers.get("content-type", ""))
            self.assertTrue(response.content.startswith(b"PK"))

        response = self._post(
            f"/admin/limpeza/instaladores/{instalador_limpo_id}",
            data={
                "justificativa": "Limpeza de cadastro de teste",
                "confirmacao": "MAT-LIMPO",
                "confirmacao_final": "1",
                "next_path": "/admin/instaladores",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            instalador = db.query(Instalador).filter(Instalador.id == instalador_limpo_id).first()
            self.assertIsNone(instalador)

    def test_report_visibility_respects_user_role(self):
        with SessionLocal() as db:
            manipulador = self._make_user(
                db,
                nome="Manipulador",
                email="manipulador@example.com",
                role=UserRole.MANIPULADOR,
            )
            self._make_instalador(db, nome="Equipe Campo", matricula="MAT-VIS-01", cpf="12345678905")
            db.commit()
            cookies = self._auth_cookies(manipulador)

        response = self.client.get("/dashboard", cookies=cookies)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Feedback", response.text)
        self.assertNotIn("/relatorios", response.text)

        blocked = self.client.get("/relatorios", cookies=cookies)
        self.assertEqual(blocked.status_code, 403)

        for path in ["/relatorios/divergencias", "/relatorios/estoque-inteligente", "/relatorios/portal-instaladores"]:
            blocked_detail = self.client.get(path, cookies=cookies)
            self.assertEqual(blocked_detail.status_code, 403, msg=path)

    def test_baixas_report_and_xlsx_show_installed_hidrometers(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="baixa-admin@example.com")
            manipulador = self._make_user(
                db,
                nome="Equipe Baixa",
                email="baixa-man@example.com",
                role=UserRole.MANIPULADOR,
            )
            almox = self._make_user(
                db,
                nome="Equipe Estoque",
                email="baixa-almox@example.com",
                role=UserRole.ALMOXARIFADO,
            )
            instalador = self._make_instalador(db, nome="Joao Campo", matricula="MAT-BAIXA-01", cpf="12345678906")
            caixa = self._make_caixa(db, numero="CX-BAIXA-01", serial="BOX-BAIXA-01")
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()
            hidrometro.status = HidrometroStatus.COM_INSTALADOR
            hidrometro.instalador_id = instalador.id
            caixa.status = CaixaStatus.ENTREGUE
            caixa.instalador_id = instalador.id

            aplicar_baixa_hidrometro(db, hidrometro, manipulador.id, observacoes="OS 12345")
            hidrometro_id = hidrometro.id
            numero_serie = hidrometro.numero_serie
            instalador_id = instalador.id
            manipulador_id = manipulador.id
            db.commit()

            cookies_man = self._auth_cookies(manipulador)
            cookies_almox = self._auth_cookies(almox)
            cookies_admin = self._auth_cookies(admin, advanced=True)

        response = self.client.get("/relatorios/baixas", cookies=cookies_admin)
        self.assertEqual(response.status_code, 200)
        self.assertIn(numero_serie, response.text)
        self.assertIn("Joao Campo", response.text)
        self.assertIn("Equipe Baixa", response.text)

        xlsx = self.client.get("/relatorios/baixas/exportar.xlsx", cookies=cookies_admin)
        self.assertEqual(xlsx.status_code, 200)
        self.assertIn("spreadsheetml", xlsx.headers.get("content-type", ""))
        self.assertTrue(xlsx.content.startswith(b"PK"))

        blocked = self.client.get("/relatorios/baixas", cookies=cookies_almox)
        self.assertEqual(blocked.status_code, 403)

        blocked_man = self.client.get("/relatorios/baixas", cookies=cookies_man)
        self.assertEqual(blocked_man.status_code, 403)

        with SessionLocal() as db:
            hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
            self.assertEqual(hidrometro.status, HidrometroStatus.INSTALADO)
            self.assertIsNotNone(hidrometro.instalado_em)
            self.assertEqual(hidrometro.baixado_por_id, manipulador_id)
            self.assertEqual(hidrometro.instalador_baixa_id, instalador_id)

    def test_admin_can_reverse_installed_baixa_and_then_clean_test_flow(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="reverse-admin@example.com")
            manipulador = self._make_user(
                db,
                nome="Equipe Campo",
                email="reverse-man@example.com",
                role=UserRole.MANIPULADOR,
            )
            instalador = self._make_instalador(db, nome="Rafael Campo", matricula="MAT-REV-01", cpf="12345678907")
            caixa = self._make_caixa(db, numero="CX-REV-01", serial="BOX-REV-01")

            solicitacao = Solicitacao(
                instalador_id=instalador.id,
                criado_por_id=admin.id,
                observacoes="Fluxo de teste para reversao",
                criado_em=utc_now(),
            )
            db.add(solicitacao)
            db.flush()
            db.add(SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1))
            db.commit()

            solicitacao = carregar_solicitacao_operacional(db, solicitacao.id)
            self.assertIsNotNone(solicitacao)
            separar_solicitacao(db, solicitacao, {solicitacao.itens_caixa[0].id: caixa.id})
            confirmar_entrega_solicitacao(db, solicitacao, admin.id, observacoes="Entrega para reversao")

            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()
            aplicar_baixa_hidrometro(db, hidrometro, manipulador.id, observacoes="OS REV-01")

            hidrometro_id = hidrometro.id
            numero_serie = hidrometro.numero_serie
            instalador_id = instalador.id
            solicitacao_id = solicitacao.id
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        response = self._post(
            f"/admin/hidrometros/{hidrometro_id}/reverter-baixa",
            data={
                "justificativa": "Baixa registrada por engano em ambiente de teste",
                "confirmacao_serial": numero_serie,
                "confirmacao_reversao": "1",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response.headers.get("location"),
            f"/manipulador/rastrear?numero_serie={numero_serie}&reversao=1",
        )

        report = self.client.get("/relatorios/baixas", cookies=cookies)
        self.assertEqual(report.status_code, 200)
        self.assertNotIn(numero_serie, report.text)

        with SessionLocal() as db:
            hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
            auditoria = db.query(AuditoriaLog).filter(
                AuditoriaLog.acao == "ADMIN_REVERSE_BAIXA_HIDROMETRO",
                AuditoriaLog.registro_id == hidrometro_id,
            ).first()
            baixa_mov = db.query(MovimentacaoMaterial).filter(
                MovimentacaoMaterial.hidrometro_id == hidrometro_id
            ).first()

            self.assertEqual(hidrometro.status, HidrometroStatus.COM_INSTALADOR)
            self.assertEqual(hidrometro.instalador_id, instalador_id)
            self.assertIsNone(hidrometro.instalado_em)
            self.assertIsNone(hidrometro.baixado_por_id)
            self.assertIsNone(hidrometro.instalador_baixa_id)
            self.assertIsNone(baixa_mov)
            self.assertIsNotNone(auditoria)

        cleanup = self._post(
            f"/admin/limpeza/solicitacoes/{solicitacao_id}",
            data={
                "justificativa": "Limpeza de implantacao apos reverter baixa",
                "confirmacao": str(solicitacao_id),
                "confirmacao_final": "1",
                "next_path": "/manipulador/solicitacoes",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(cleanup.status_code, 302)

        with SessionLocal() as db:
            solicitacao = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
            self.assertIsNone(solicitacao)

    def test_admin_can_reverse_baixa_without_instalador_back_to_stock(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="reverse-stock-admin@example.com")
            caixa = self._make_caixa(db, numero="CX-ERR-01", serial="BOX-ERR-01")
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()

            aplicar_baixa_hidrometro(
                db,
                hidrometro,
                admin.id,
                observacoes="Baixa errada em estoque",
                override=True,
                justificativa_override="Teste de reversao",
            )
            hidrometro_id = hidrometro.id
            numero_serie = hidrometro.numero_serie
            caixa_id = caixa.id
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        response = self._post(
            f"/admin/hidrometros/{hidrometro_id}/reverter-baixa",
            data={
                "justificativa": "Baixa digitada em serial errado",
                "confirmacao_serial": numero_serie,
                "confirmacao_reversao": "1",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
            caixa = db.query(CaixaHidrometro).filter(CaixaHidrometro.id == caixa_id).first()
            baixa_mov = db.query(MovimentacaoMaterial).filter(
                MovimentacaoMaterial.hidrometro_id == hidrometro_id,
                MovimentacaoMaterial.tipo == MovimentacaoTipo.BAIXA,
            ).first()
            auditoria = db.query(AuditoriaLog).filter(
                AuditoriaLog.acao == "ADMIN_REVERSE_BAIXA_HIDROMETRO",
                AuditoriaLog.registro_id == hidrometro_id,
            ).first()

            self.assertEqual(hidrometro.status, HidrometroStatus.EM_ESTOQUE)
            self.assertIsNone(hidrometro.instalador_id)
            self.assertIsNone(hidrometro.instalado_em)
            self.assertEqual(caixa.status, CaixaStatus.EM_ESTOQUE)
            self.assertIsNone(caixa.instalador_id)
            self.assertIsNone(baixa_mov)
            self.assertIsNotNone(auditoria)

    def test_baixa_requires_valid_delivery_context_and_admin_can_reverse_piece_entry(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="rules-admin@example.com")
            manipulador = self._make_user(
                db,
                nome="Equipe Regra",
                email="rules-man@example.com",
                role=UserRole.MANIPULADOR,
            )
            instalador = self._make_instalador(db, nome="Lucas Campo", matricula="MAT-RULE-01", cpf="12345678908")
            tipo = self._make_tipo_peca(db, nome="LACRE-RULE", quantidade=10)
            caixa = self._make_caixa(db, numero="CX-RULE-01", serial="BOX-RULE-01")
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()
            hidrometro.status = HidrometroStatus.COM_INSTALADOR
            hidrometro.instalador_id = instalador.id
            caixa.status = CaixaStatus.EM_ESTOQUE
            caixa.instalador_id = None
            hidrometro_id = hidrometro.id
            instalador_id = instalador.id
            tipo_id = tipo.id
            db.commit()
            cookies_admin = self._auth_cookies(admin, advanced=True)
            cookies_man = self._auth_cookies(manipulador)

        baixa = self._post(
            "/manipulador/baixa-hidrometro/confirmar",
            data={"hidrometro_id": hidrometro_id, "observacoes": "Tentativa indevida"},
            cookies=cookies_man,
            follow_redirects=False,
        )
        self.assertEqual(baixa.status_code, 400)
        self.assertIn("caixa entregue ao instalador", baixa.text)

        solicitacao = self._post(
            "/manipulador/solicitacoes/nova",
            data={
                "instalador_id": instalador_id,
                "qtd_caixas": 0,
                f"peca_{tipo_id}": 11,
                "observacoes": "Acima do estoque",
            },
            cookies=cookies_man,
            follow_redirects=False,
        )
        self.assertEqual(solicitacao.status_code, 400)
        self.assertIn("possui somente 10 unidade", solicitacao.text)

        entrada = self._post(
            "/almoxarifado/pecas/entrada",
            data={
                "tipo_peca_id": tipo_id,
                "quantidade": 5,
                "observacoes": "Entrada digitada errada",
            },
            cookies=cookies_admin,
            follow_redirects=False,
        )
        self.assertEqual(entrada.status_code, 302)

        with SessionLocal() as db:
            estoque = db.query(EstoquePeca).filter(EstoquePeca.tipo_peca_id == tipo_id).first()
            movimento = db.query(MovimentacaoPeca).filter(
                MovimentacaoPeca.tipo == MovimentacaoTipo.ENTRADA,
                MovimentacaoPeca.tipo_peca_id == tipo_id,
                MovimentacaoPeca.instalador_id == None,
                MovimentacaoPeca.solicitacao_id == None,
            ).order_by(MovimentacaoPeca.id.desc()).first()
            self.assertEqual(estoque.quantidade_atual, 15)
            self.assertIsNotNone(movimento)
            movimento_id = movimento.id

        estorno = self._post(
            f"/admin/pecas/movimentacoes/{movimento_id}/estornar-entrada",
            data={
                "justificativa": "Correcao de digitacao na entrada",
                "confirmacao": str(movimento_id),
                "confirmacao_final": "1",
            },
            cookies=cookies_admin,
            follow_redirects=False,
        )
        self.assertEqual(estorno.status_code, 302)
        self.assertEqual(estorno.headers.get("location"), "/almoxarifado/estoque?estorno=1")

        with SessionLocal() as db:
            estoque = db.query(EstoquePeca).filter(EstoquePeca.tipo_peca_id == tipo_id).first()
            auditoria = db.query(AuditoriaLog).filter(
                AuditoriaLog.acao == "ADMIN_REVERSE_ENTRADA_PECA",
                AuditoriaLog.registro_id == movimento_id,
            ).first()
            saida_estorno = db.query(MovimentacaoPeca).filter(
                MovimentacaoPeca.tipo == MovimentacaoTipo.SAIDA,
                MovimentacaoPeca.tipo_peca_id == tipo_id,
                MovimentacaoPeca.instalador_id == None,
                MovimentacaoPeca.solicitacao_id == None,
            ).order_by(MovimentacaoPeca.id.desc()).first()
            self.assertEqual(estoque.quantidade_atual, 10)
            self.assertIsNotNone(auditoria)
            self.assertIsNotNone(saida_estorno)
            self.assertIn("Estorno admin da entrada", saida_estorno.observacoes)

    def test_admin_can_reverse_conferencia_and_restore_instalador_balance(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="conf-admin@example.com")
            manipulador = self._make_user(
                db,
                nome="Equipe Conferencia",
                email="conf-man@example.com",
                role=UserRole.MANIPULADOR,
            )
            instalador = self._make_instalador(db, nome="Paulo Campo", matricula="MAT-CONF-01", cpf="12345678909")
            tipo = self._make_tipo_peca(db, nome="LACRE-CONF", quantidade=20)
            db.add(
                InstaladorPeca(
                    instalador_id=instalador.id,
                    tipo_peca_id=tipo.id,
                    quantidade=5,
                    atualizado_em=utc_now(),
                )
            )
            db.commit()
            cookies_admin = self._auth_cookies(admin, advanced=True)
            cookies_man = self._auth_cookies(manipulador)
            instalador_id = instalador.id
            tipo_id = tipo.id

        conferencia = self._post(
            f"/manipulador/conferencia/{instalador_id}",
            data={
                f"qtd_real_{tipo_id}": 3,
                "observacoes": "Contagem digitada errada",
            },
            cookies=cookies_man,
            follow_redirects=False,
        )
        self.assertEqual(conferencia.status_code, 302)

        with SessionLocal() as db:
            conferencia = db.query(ConferenciaPecas).filter(
                ConferenciaPecas.instalador_id == instalador_id
            ).order_by(ConferenciaPecas.id.desc()).first()
            posse = db.query(InstaladorPeca).filter(
                InstaladorPeca.instalador_id == instalador_id,
                InstaladorPeca.tipo_peca_id == tipo_id,
            ).first()
            registro_informativo = db.query(ConferenciaInstaladorPeca).filter(
                ConferenciaInstaladorPeca.instalador_id == instalador_id,
                ConferenciaInstaladorPeca.peca_id == tipo_id,
            ).first()
            self.assertIsNotNone(conferencia)
            self.assertEqual(posse.quantidade, 5)
            self.assertIsNotNone(registro_informativo)
            self.assertEqual(registro_informativo.quantidade_informada, 3)
            conferencia_id = conferencia.id

        reversao = self._post(
            f"/admin/conferencias/{conferencia_id}/reverter",
            data={
                "justificativa": "Conferencia digitada com saldo errado",
                "confirmacao": str(conferencia_id),
                "confirmacao_final": "1",
            },
            cookies=cookies_admin,
            follow_redirects=False,
        )
        self.assertEqual(reversao.status_code, 302)
        self.assertEqual(reversao.headers.get("location"), "/manipulador/conferencia?reversao=1")

        with SessionLocal() as db:
            conferencia = db.query(ConferenciaPecas).filter(ConferenciaPecas.id == conferencia_id).first()
            registro_informativo = db.query(ConferenciaInstaladorPeca).filter(
                ConferenciaInstaladorPeca.conferencia_id == conferencia_id,
            ).first()
            posse = db.query(InstaladorPeca).filter(
                InstaladorPeca.instalador_id == instalador_id,
                InstaladorPeca.tipo_peca_id == tipo_id,
            ).first()
            movimento_reversao = db.query(MovimentacaoPeca).filter(
                MovimentacaoPeca.instalador_id == instalador_id,
                MovimentacaoPeca.tipo_peca_id == tipo_id,
                MovimentacaoPeca.tipo == MovimentacaoTipo.ENTRADA,
                MovimentacaoPeca.observacoes.like(f"Reversao admin da conferencia #{conferencia_id}%"),
            ).order_by(MovimentacaoPeca.id.desc()).first()
            auditoria = db.query(AuditoriaLog).filter(
                AuditoriaLog.acao == "ADMIN_REVERSE_CONFERENCIA_PECA",
                AuditoriaLog.registro_id == conferencia_id,
            ).first()

            self.assertIsNone(conferencia)
            self.assertIsNone(registro_informativo)
            self.assertIsNotNone(posse)
            self.assertEqual(posse.quantidade, 5)
            self.assertIsNone(movimento_reversao)
            self.assertIsNotNone(auditoria)

    def test_admin_can_reverse_delivery_from_material_movement(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="delivery-admin@example.com")
            instalador = self._make_instalador(db, nome="Marco Campo", matricula="MAT-DEL-01", cpf="12345678910")
            tipo = self._make_tipo_peca(db, nome="LACRE-DEL", quantidade=10)
            caixa = self._make_caixa(db, numero="CX-DEL-01", serial="BOX-DEL-01")

            solicitacao = Solicitacao(
                instalador_id=instalador.id,
                criado_por_id=admin.id,
                observacoes="Entrega para reversao",
                criado_em=utc_now(),
            )
            db.add(solicitacao)
            db.flush()
            db.add(SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1))
            db.add(SolicitacaoItemPeca(solicitacao_id=solicitacao.id, tipo_peca_id=tipo.id, quantidade_solicitada=2))
            db.commit()

            solicitacao = carregar_solicitacao_operacional(db, solicitacao.id)
            separar_solicitacao(db, solicitacao, {solicitacao.itens_caixa[0].id: caixa.id})
            confirmar_entrega_solicitacao(db, solicitacao, admin.id, observacoes="Entrega para campo")
            db.commit()

            solicitacao_id = solicitacao.id
            caixa_id = caixa.id
            instalador_id = instalador.id
            tipo_id = tipo.id
            movimento_saida_id = db.query(MovimentacaoMaterial).filter(
                MovimentacaoMaterial.solicitacao_id == solicitacao_id,
                MovimentacaoMaterial.tipo == MovimentacaoTipo.SAIDA,
            ).order_by(MovimentacaoMaterial.id.desc()).first().id
            cookies = self._auth_cookies(admin, advanced=True)

        redirect = self.client.get(
            f"/admin/movimentacoes/material/{movimento_saida_id}/reverter",
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(redirect.status_code, 302)
        self.assertIn(f"/admin/solicitacoes/{solicitacao_id}/reverter-entrega", redirect.headers.get("location"))

        response = self._post(
            f"/admin/solicitacoes/{solicitacao_id}/reverter-entrega",
            data={
                "justificativa": "Entrega de teste precisa voltar para o estoque",
                "confirmacao": str(solicitacao_id),
                "confirmacao_final": "1",
                "next_path": "/almoxarifado/solicitacoes",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("location"), "/almoxarifado/solicitacoes?reversao_entrega=1")

        with SessionLocal() as db:
            solicitacao = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
            caixa = db.query(CaixaHidrometro).filter(CaixaHidrometro.id == caixa_id).first()
            hidrometros = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa_id).all()
            estoque = db.query(EstoquePeca).filter(EstoquePeca.tipo_peca_id == tipo_id).first()
            posse = db.query(InstaladorPeca).filter(
                InstaladorPeca.instalador_id == instalador_id,
                InstaladorPeca.tipo_peca_id == tipo_id,
            ).first()
            devolucao_material = db.query(MovimentacaoMaterial).filter(
                MovimentacaoMaterial.solicitacao_id == solicitacao_id,
                MovimentacaoMaterial.tipo == MovimentacaoTipo.DEVOLUCAO,
            ).count()
            devolucao_peca = db.query(MovimentacaoPeca).filter(
                MovimentacaoPeca.solicitacao_id == solicitacao_id,
                MovimentacaoPeca.tipo == MovimentacaoTipo.DEVOLUCAO,
            ).count()
            auditoria = db.query(AuditoriaLog).filter(
                AuditoriaLog.acao == "ADMIN_REVERSE_ENTREGA_SOLICITACAO",
                AuditoriaLog.registro_id == solicitacao_id,
            ).first()

            self.assertIsNotNone(solicitacao)
            self.assertEqual(solicitacao.status, SolicitacaoStatus.SEPARADA)
            self.assertIsNone(solicitacao.entregue_em)
            self.assertIsNone(solicitacao.entregue_por_id)
            self.assertEqual(caixa.status, CaixaStatus.EM_ESTOQUE)
            self.assertIsNone(caixa.instalador_id)
            self.assertEqual(estoque.quantidade_atual, 10)
            self.assertIsNone(posse)
            self.assertEqual(devolucao_material, 1)
            self.assertEqual(devolucao_peca, 1)
            self.assertIsNotNone(auditoria)
            self.assertTrue(all(h.status == HidrometroStatus.EM_ESTOQUE for h in hidrometros))
            self.assertTrue(all(h.instalador_id is None for h in hidrometros))

    def test_feedback_flow_and_navigation_shortcuts_work(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="feedback-admin@example.com")
            manipulador = self._make_user(
                db,
                nome="Equipe UX",
                email="feedback-man@example.com",
                role=UserRole.MANIPULADOR,
            )
            almox = self._make_user(
                db,
                nome="Equipe Estoque UX",
                email="feedback-almox@example.com",
                role=UserRole.ALMOXARIFADO,
            )
            instalador = self._make_instalador(db, nome="Nando Campo", matricula="MAT-FEED-01", cpf="12345678911")
            db.commit()
            cookies_admin = self._auth_cookies(admin, advanced=True)
            cookies_man = self._auth_cookies(manipulador)
            cookies_almox = self._auth_cookies(almox)
            instalador_id = instalador.id

        csrf_token = gerar_csrf_token()
        cookies_upload = dict(cookies_man)
        cookies_upload["csrf_token"] = csrf_token
        response = self.client.post(
            "/feedback",
            data={
                "csrf_token": csrf_token,
                "categoria": "usabilidade",
                "titulo": "Tela de baixa confusa",
                "mensagem": "A operacao ficou melhor, mas ainda pode ficar mais intuitiva.",
                "pagina_origem": "/dashboard",
                "urgente": "1",
            },
            files=[("anexos", ("erro-tela.png", b"\x89PNG\r\n\x1a\nprint", "image/png"))],
            cookies=cookies_upload,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("location"), "/feedback?sucesso=1")

        response = self._post(
            "/feedback",
            data={
                "categoria": "usabilidade",
                "titulo": "Entrega precisa de destaque",
                "mensagem": "A operacao ficou melhor, mas ainda pode ficar mais intuitiva.",
                "pagina_origem": "/dashboard",
            },
            cookies=cookies_almox,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers.get("location"), "/feedback?sucesso=1")

        page = self.client.get(f"/manipulador/instaladores/{instalador_id}", cookies=cookies_man)
        self.assertEqual(page.status_code, 200)
        self.assertIn('href="/dashboard"', page.text)
        self.assertIn('class="topbar-back"', page.text)

        admin_feedbacks = self.client.get("/admin/feedbacks", cookies=cookies_admin)
        self.assertEqual(admin_feedbacks.status_code, 200)
        self.assertIn("Tela de baixa confusa", admin_feedbacks.text)
        self.assertIn("Entrega precisa de destaque", admin_feedbacks.text)
        self.assertIn("Urgente", admin_feedbacks.text)
        self.assertIn("erro-tela.png", admin_feedbacks.text)

        with SessionLocal() as db:
            feedback = db.query(FeedbackOperacional).filter(
                FeedbackOperacional.titulo == "Tela de baixa confusa"
            ).first()
            self.assertIsNotNone(feedback)
            self.assertEqual(feedback.area, "manipulador")
            self.assertTrue(feedback.urgente)
            feedback_id = feedback.id
            anexo = db.query(FeedbackAnexo).filter(FeedbackAnexo.feedback_id == feedback_id).first()
            self.assertIsNotNone(anexo)
            anexo_id = anexo.id
            anexo_path = TEST_UPLOAD_DIR / anexo.caminho_relativo
            self.assertTrue(anexo_path.exists())

        download = self.client.get(f"/admin/feedbacks/anexos/{anexo_id}", cookies=cookies_admin)
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"\x89PNG\r\n\x1a\nprint")

        resolve = self._post(
            f"/admin/feedbacks/{feedback_id}/status",
            data={"status": "resolvido"},
            cookies=cookies_admin,
            follow_redirects=False,
        )
        self.assertEqual(resolve.status_code, 302)
        self.assertEqual(resolve.headers.get("location"), "/admin/feedbacks?status=resolvido")

        with SessionLocal() as db:
            feedback = db.query(FeedbackOperacional).filter(FeedbackOperacional.id == feedback_id).first()
            self.assertEqual(feedback.status.value, "resolvido")
            self.assertIsNotNone(feedback.resolvido_em)
            feedback.resolvido_em = utc_now() - timedelta(days=31)
            db.commit()

        limpeza = self._post(
            "/admin/feedbacks/limpar-resolvidos",
            data={"dias_minimos": "30", "confirmacao": "LIMPAR"},
            cookies=cookies_admin,
            follow_redirects=False,
        )
        self.assertEqual(limpeza.status_code, 302)
        self.assertEqual(limpeza.headers.get("location"), "/admin/feedbacks?limpeza=1")

        with SessionLocal() as db:
            self.assertIsNone(db.query(FeedbackOperacional).filter(FeedbackOperacional.id == feedback_id).first())
            self.assertIsNone(db.query(FeedbackAnexo).filter(FeedbackAnexo.id == anexo_id).first())
            self.assertFalse(anexo_path.exists())

    def test_login_lockout_and_password_rotation_are_active(self):
        with SessionLocal() as db:
            usuario = self._make_user(
                db,
                nome="Seguranca",
                email="seguranca@example.com",
                role=UserRole.ADMIN,
            )
            usuario_id = usuario.id
            db.commit()
            cookies = self._auth_cookies(usuario)

        for _ in range(5):
            response = self._post(
                "/auth/login",
                data={"email": "seguranca@example.com", "senha": "senha-errada"},
                follow_redirects=False,
            )
            self.assertIn(response.status_code, {401, 429})

        blocked = self._post(
            "/auth/login",
            data={"email": "seguranca@example.com", "senha": "senha-errada"},
            follow_redirects=False,
        )
        self.assertEqual(blocked.status_code, 429)

        password_change = self._post(
            "/auth/trocar-senha",
            data={
                "senha_atual": "123456",
                "nova_senha": "NovaSenha@123",
                "confirmar_senha": "NovaSenha@123",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(password_change.status_code, 200)
        novo_access_token = password_change.cookies.get("access_token")
        self.assertTrue(novo_access_token)

        old_token_dashboard = self.client.get("/dashboard", cookies=cookies, follow_redirects=False)
        self.assertEqual(old_token_dashboard.status_code, 302)
        self.assertEqual(old_token_dashboard.headers.get("location"), "/auth/login?sessao_invalidada=1")

        new_cookies = dict(cookies)
        new_cookies["access_token"] = novo_access_token
        refreshed_dashboard = self.client.get("/dashboard", cookies=new_cookies, follow_redirects=False)
        self.assertEqual(refreshed_dashboard.status_code, 200)

        with SessionLocal() as db:
            usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
            self.assertEqual(usuario.token_version, 1)

    def test_admin_monitoring_backup_button_and_healthcheck_work(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="backup-admin@example.com")
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")

        response = self._post(
            "/admin/monitoramento/backup",
            cookies=cookies,
            follow_redirects=False,
        )
        if engine.dialect.name == "postgresql" and shutil.which("pg_dump") is None:
            self.assertEqual(response.status_code, 302)
            self.assertIn("erro_backup", response.headers.get("location", ""))
            with SessionLocal() as db:
                log = db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ADMIN_BACKUP_FALHA").first()
                self.assertIsNotNone(log)
            return

        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        self.assertGreater(len(response.content), 100)

        page = self.client.get("/admin/monitoramento", cookies=cookies)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Baixar banco", page.text)

        with SessionLocal() as db:
            log = db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ADMIN_BACKUP_DOWNLOAD").first()
            self.assertIsNotNone(log)
            self.assertTrue(any(TEST_BACKUP_DIR.glob("hidrometros_backup_*")))

    def test_backup_missing_pg_dump_is_controlled_and_audited_as_critical(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="backup-failure-admin@example.com")
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        with patch("app.services.backup.DATABASE_URL", "postgresql://user:pass@localhost:5432/db"), patch("app.services.backup.shutil.which", return_value=False):
            response = self._post(
                "/admin/monitoramento/backup",
                cookies=cookies,
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn("erro_backup", response.headers.get("location", ""))
        with SessionLocal() as db:
            log = db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ADMIN_BACKUP_FALHA").first()
            self.assertIsNotNone(log)
            self.assertEqual(log.severidade, "CRITICO")
            self.assertEqual(log.resultado, "FALHA")
            self.assertTrue(log.ip_cliente)

    def test_auditoria_records_forwarded_ip_connection_ip_and_user_agent(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="audit-ip-admin@example.com")
            db.commit()

        csrf_token = gerar_csrf_token()
        response = self.client.post(
            "/auth/login",
            data={"email": "audit-ip-admin@example.com", "senha": "123456", "csrf_token": csrf_token},
            cookies={"csrf_token": csrf_token},
            headers={
                "x-forwarded-for": "203.0.113.10, 10.0.0.2",
                "x-real-ip": "198.51.100.20",
                "user-agent": "GMF-Test-Agent/1.0",
            },
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            log = db.query(AuditoriaLog).filter(AuditoriaLog.acao == "LOGIN_SUCESSO").first()
            self.assertIsNotNone(log)
            self.assertEqual(log.ip_cliente, "203.0.113.10")
            self.assertEqual(log.x_forwarded_for, "203.0.113.10, 10.0.0.2")
            self.assertEqual(log.x_real_ip, "198.51.100.20")
            self.assertEqual(log.user_agent, "GMF-Test-Agent/1.0")
            self.assertEqual(log.usuario_email, "audit-ip-admin@example.com")

    def test_global_exception_handler_returns_error_id_and_audits_critical_event(self):
        safe_client = TestClient(app, raise_server_exceptions=False)
        response = safe_client.get(
            "/__test_unhandled_error",
            headers={"user-agent": "GMF-Error-Test/1.0", "x-forwarded-for": "203.0.113.99"},
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn("ID do erro", response.text)

        with SessionLocal() as db:
            log = db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ERRO_CRITICO_PAGINA").first()
            self.assertIsNotNone(log)
            self.assertEqual(log.severidade, "CRITICO")
            self.assertEqual(log.resultado, "FALHA")
            self.assertEqual(log.ip_cliente, "203.0.113.99")
            self.assertEqual(log.user_agent, "GMF-Error-Test/1.0")

    def test_admin_daily_checklist_diagnostic_export_term_and_read_only_mode(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="ops-admin@example.com")
            manipulador = self._make_user(
                db,
                nome="Operador Leitura",
                email="ops-man@example.com",
                role=UserRole.MANIPULADOR,
            )
            instalador = self._make_instalador(db, nome="Operador Campo", matricula="MAT-OPS-01", cpf="12345678931")
            tipo = self._make_tipo_peca(db, nome="LACRE-OPS", quantidade=5)
            db.commit()
            admin_cookies = self._auth_cookies(admin, advanced=True)
            man_cookies = self._auth_cookies(manipulador)
            instalador_id = instalador.id
            tipo_id = tipo.id

        monitoramento = self.client.get("/admin/monitoramento", cookies=admin_cookies)
        self.assertEqual(monitoramento.status_code, 200)
        self.assertIn("Checklist diario", monitoramento.text)
        self.assertIn("Modo leitura", monitoramento.text)
        self.assertIn("Politica de backup", monitoramento.text)
        self.assertIn("Alertas acionaveis", monitoramento.text)
        self.assertIn("Usuarios ativos", monitoramento.text)
        self.assertIn("/admin/usuarios-ativos", monitoramento.text)
        self.assertIn("/admin/auditoria?severidade=CRITICO", monitoramento.text)

        diagnostico = self.client.get("/admin/diagnostico", cookies=admin_cookies)
        self.assertEqual(diagnostico.status_code, 200)
        self.assertIn("Diagnostico Operacional", diagnostico.text)

        blocked_diag = self.client.get("/admin/diagnostico", cookies=man_cookies)
        self.assertEqual(blocked_diag.status_code, 403)

        termo = self.client.get("/termo-responsabilidade", cookies=man_cookies)
        self.assertEqual(termo.status_code, 200)
        self.assertIn("Uso responsavel", termo.text)

        pacote = self.client.get("/admin/exportacao-periodica.zip", cookies=admin_cookies)
        self.assertEqual(pacote.status_code, 200)
        self.assertEqual(pacote.headers.get("content-type"), "application/zip")
        with ZipFile(BytesIO(pacote.content)) as archive:
            nomes = set(archive.namelist())
        self.assertIn("auditoria.csv", nomes)
        self.assertIn("hidrometros.csv", nomes)
        self.assertIn("estoque_pecas.csv", nomes)

        ativar = self._post(
            "/admin/monitoramento/modo-leitura",
            data={"acao": "ATIVAR", "motivo": "Teste de contingencia", "confirmacao": "ATIVAR"},
            cookies=admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(ativar.status_code, 302)

        bloqueado = self._post(
            "/manipulador/solicitacoes/nova",
            data={
                "instalador_id": instalador_id,
                "qtd_caixas": 0,
                f"peca_{tipo_id}": 1,
                "observacoes": "Nao deve criar em modo leitura",
            },
            cookies=man_cookies,
            follow_redirects=False,
        )
        self.assertEqual(bloqueado.status_code, 423)
        self.assertIn("Modo leitura ativo", bloqueado.text)

        relatorios = self.client.get("/relatorios", cookies=admin_cookies)
        self.assertEqual(relatorios.status_code, 200)
        self.assertIn("Alertas e atalhos de decisao", relatorios.text)
        self.assertIn("Usuarios ativos", relatorios.text)
        self.assertIn("/admin/auditoria?severidade=CRITICO", relatorios.text)

        liberar = self._post(
            "/admin/monitoramento/modo-leitura",
            data={"acao": "LIBERAR", "motivo": "Teste concluido", "confirmacao": "LIBERAR"},
            cookies=admin_cookies,
            follow_redirects=False,
        )
        self.assertEqual(liberar.status_code, 302)

        criado = self._post(
            "/manipulador/solicitacoes/nova",
            data={
                "instalador_id": instalador_id,
                "qtd_caixas": 0,
                f"peca_{tipo_id}": 1,
                "observacoes": "Criacao apos liberacao",
            },
            cookies=man_cookies,
            follow_redirects=False,
        )
        self.assertEqual(criado.status_code, 302)

        with SessionLocal() as db:
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ADMIN_ATIVAR_MODO_LEITURA").first())
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ADMIN_LIBERAR_MODO_LEITURA").first())

    def test_postgresql_concurrent_delivery_allows_single_commit(self):
        self._skip_unless_postgresql()
        workers = 6
        with SessionLocal() as db:
            admin = self._make_user(db, email="concurrency-delivery-admin@example.com")
            almox = self._make_user(
                db,
                nome="Almox Concorrencia",
                email="concurrency-delivery-almox@example.com",
                role=UserRole.ALMOXARIFADO,
            )
            instalador = self._make_instalador(db, nome="Concorrente Campo", matricula="MAT-CONC-ENT", cpf="12345678921")
            tipo = self._make_tipo_peca(db, nome="LACRE-CONC-ENT", quantidade=3)
            caixa = self._make_caixa(db, numero="CX-CONC-ENT", serial="BOX-CONC-ENT")
            solicitacao = Solicitacao(
                instalador_id=instalador.id,
                criado_por_id=admin.id,
                observacoes="Concorrencia de entrega",
                criado_em=utc_now(),
            )
            db.add(solicitacao)
            db.flush()
            item_caixa = SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1)
            item_peca = SolicitacaoItemPeca(solicitacao_id=solicitacao.id, tipo_peca_id=tipo.id, quantidade_solicitada=1)
            db.add(item_caixa)
            db.add(item_peca)
            db.commit()

            solicitacao = carregar_solicitacao_operacional(db, solicitacao.id)
            separar_solicitacao(db, solicitacao, {solicitacao.itens_caixa[0].id: caixa.id})
            db.commit()
            solicitacao_id = solicitacao.id
            almox_id = almox.id
            tipo_id = tipo.id

        barrier = threading.Barrier(workers)
        results: list[tuple[str, str]] = []
        results_lock = threading.Lock()

        def worker(index: int) -> None:
            with SessionLocal() as db:
                try:
                    solicitacao = carregar_solicitacao_operacional(db, solicitacao_id)
                    barrier.wait(timeout=15)
                    confirmar_entrega_solicitacao(db, solicitacao, almox_id, observacoes=f"Entrega concorrente {index}")
                    registrar_auditoria(
                        db=db,
                        acao="ENTREGA_CONCORRENTE_TESTE",
                        usuario_id=almox_id,
                        tabela="solicitacoes",
                        registro_id=solicitacao_id,
                    )
                    db.commit()
                    outcome = ("ok", "")
                except Exception as exc:
                    db.rollback()
                    outcome = ("erro", str(exc))
                with results_lock:
                    results.append(outcome)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(results), workers)
        self.assertEqual(sum(1 for status, _ in results if status == "ok"), 1)

        with SessionLocal() as db:
            solicitacao = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
            estoque = db.query(EstoquePeca).filter(EstoquePeca.tipo_peca_id == tipo_id).first()
            saidas_peca = db.query(MovimentacaoPeca).filter(
                MovimentacaoPeca.solicitacao_id == solicitacao_id,
                MovimentacaoPeca.tipo == MovimentacaoTipo.SAIDA,
            ).count()
            saidas_material = db.query(MovimentacaoMaterial).filter(
                MovimentacaoMaterial.solicitacao_id == solicitacao_id,
                MovimentacaoMaterial.tipo == MovimentacaoTipo.SAIDA,
            ).count()
            self.assertEqual(solicitacao.status, SolicitacaoStatus.ENTREGUE)
            self.assertEqual(estoque.quantidade_atual, 2)
            self.assertEqual(saidas_peca, 1)
            self.assertEqual(saidas_material, 1)

    def test_postgresql_concurrent_separation_keeps_box_unique(self):
        self._skip_unless_postgresql()
        workers = 6
        with SessionLocal() as db:
            admin = self._make_user(db, email="concurrency-separation-admin@example.com")
            almox = self._make_user(
                db,
                nome="Almox Separacao",
                email="concurrency-separation-almox@example.com",
                role=UserRole.ALMOXARIFADO,
            )
            instalador = self._make_instalador(db, nome="Equipe Separacao", matricula="MAT-CONC-SEP", cpf="12345678922")
            caixa = self._make_caixa(db, numero="CX-CONC-SEP", serial="BOX-CONC-SEP")
            solicitacao_ids = []
            for index in range(workers):
                solicitacao = Solicitacao(
                    instalador_id=instalador.id,
                    criado_por_id=admin.id,
                    observacoes=f"Concorrencia separacao {index}",
                    criado_em=utc_now(),
                )
                db.add(solicitacao)
                db.flush()
                db.add(SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1))
                solicitacao_ids.append(solicitacao.id)
            db.commit()
            caixa_id = caixa.id
            almox_id = almox.id

        barrier = threading.Barrier(workers)
        results: list[tuple[str, str]] = []
        results_lock = threading.Lock()

        def worker(solicitacao_id: int) -> None:
            with SessionLocal() as db:
                try:
                    solicitacao = carregar_solicitacao_operacional(db, solicitacao_id)
                    item_id = solicitacao.itens_caixa[0].id
                    barrier.wait(timeout=15)
                    separar_solicitacao(db, solicitacao, {item_id: caixa_id})
                    registrar_auditoria(
                        db=db,
                        acao="SEPARACAO_CONCORRENTE_TESTE",
                        usuario_id=almox_id,
                        tabela="solicitacoes",
                        registro_id=solicitacao_id,
                    )
                    db.commit()
                    outcome = ("ok", "")
                except Exception as exc:
                    db.rollback()
                    outcome = ("erro", str(exc))
                with results_lock:
                    results.append(outcome)

        threads = [threading.Thread(target=worker, args=(solicitacao_id,)) for solicitacao_id in solicitacao_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(len(results), workers)
        self.assertEqual(sum(1 for status, _ in results if status == "ok"), 1)

        with SessionLocal() as db:
            vinculadas = db.query(SolicitacaoItemCaixa).filter(SolicitacaoItemCaixa.caixa_id == caixa_id).count()
            separadas = db.query(Solicitacao).filter(Solicitacao.status == SolicitacaoStatus.SEPARADA).count()
            self.assertEqual(vinculadas, 1)
            self.assertEqual(separadas, 1)

    def test_operational_blocks_are_audited_and_baixa_requires_context_confirmation(self):
        with SessionLocal() as db:
            self._make_user(db, email="admin-protecao-operacional@example.com")
            manipulador = self._make_user(
                db,
                nome="Operador Protegido",
                email="operador-protegido@example.com",
                role=UserRole.MANIPULADOR,
            )
            instalador = self._make_instalador(db, nome="Equipe Baixa", matricula="MAT-BAIXA-CTX", cpf="12345678923")
            caixa = self._make_caixa(db, numero="CX-BAIXA-CTX", serial="BOX-BAIXA-CTX")
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()
            caixa.status = CaixaStatus.ENTREGUE
            caixa.instalador_id = instalador.id
            for item in db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).all():
                item.status = HidrometroStatus.COM_INSTALADOR
                item.instalador_id = instalador.id
            hidrometro_id = hidrometro.id
            numero_serie = hidrometro.numero_serie
            db.commit()
            cookies = self._auth_cookies(manipulador)

        bloqueada = self._post(
            "/manipulador/baixa-hidrometro/confirmar",
            data={"hidrometro_id": hidrometro_id, "observacoes": "Sem confirmacao"},
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(bloqueada.status_code, 400)
        self.assertIn("Confirme o serial", bloqueada.text)

        with SessionLocal() as db:
            evento = db.query(AuditoriaLog).filter(
                AuditoriaLog.acao == "VALIDACAO_OPERACIONAL_BLOQUEADA",
                AuditoriaLog.registro_id == hidrometro_id,
            ).first()
            self.assertIsNotNone(evento)
            self.assertEqual(evento.severidade, "SUSPEITO")

        confirmada = self._post(
            "/manipulador/baixa-hidrometro/confirmar",
            data={
                "hidrometro_id": hidrometro_id,
                "confirmacao_serial": numero_serie,
                "confirmacao_contexto": "1",
                "observacoes": "Instalado com contexto confirmado",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(confirmada.status_code, 302)

        with SessionLocal() as db:
            hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
            log = db.query(AuditoriaLog).filter(AuditoriaLog.acao == "BAIXA_HIDROMETRO").first()
            self.assertEqual(hidrometro.status, HidrometroStatus.INSTALADO)
            self.assertIsNotNone(log)
            self.assertEqual(log.severidade, "NORMAL")

    def test_only_admin_can_cancel_separated_request_and_diagnostic_xlsx_exists(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="admin-cancelamento-contextual@example.com")
            manipulador = self._make_user(
                db,
                nome="Operador Cancelamento",
                email="operador-cancelamento@example.com",
                role=UserRole.MANIPULADOR,
            )
            instalador = self._make_instalador(db, nome="Equipe Cancelamento", matricula="MAT-CANCEL-CTX", cpf="12345678924")
            caixa = self._make_caixa(db, numero="CX-CANCEL-CTX", serial="BOX-CANCEL-CTX")
            solicitacao = Solicitacao(
                instalador_id=instalador.id,
                criado_por_id=manipulador.id,
                observacoes="Cancelamento protegido",
                criado_em=utc_now(),
            )
            db.add(solicitacao)
            db.flush()
            db.add(SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1))
            db.flush()
            solicitacao = carregar_solicitacao_operacional(db, solicitacao.id)
            separar_solicitacao(db, solicitacao, {solicitacao.itens_caixa[0].id: caixa.id})
            solicitacao_id = solicitacao.id
            db.commit()
            cookies_admin = self._auth_cookies(admin)
            cookies_man = self._auth_cookies(manipulador)

        bloqueada = self._post(
            f"/manipulador/solicitacoes/{solicitacao_id}/cancelar",
            data={"motivo": "Operador tentando corrigir", "next_path": "/manipulador/solicitacoes"},
            cookies=cookies_man,
            follow_redirects=False,
        )
        self.assertEqual(bloqueada.status_code, 302)

        with SessionLocal() as db:
            solicitacao = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
            evento = db.query(AuditoriaLog).filter(
                AuditoriaLog.acao == "VALIDACAO_OPERACIONAL_BLOQUEADA",
                AuditoriaLog.registro_id == solicitacao_id,
            ).first()
            self.assertEqual(solicitacao.status, SolicitacaoStatus.SEPARADA)
            self.assertIsNotNone(evento)
            self.assertEqual(evento.severidade, "SUSPEITO")

        cancelada = self._post(
            f"/manipulador/solicitacoes/{solicitacao_id}/cancelar",
            data={"motivo": "ADMIN liberando teste", "next_path": "/manipulador/solicitacoes"},
            cookies=cookies_admin,
            follow_redirects=False,
        )
        self.assertEqual(cancelada.status_code, 302)

        with SessionLocal() as db:
            solicitacao = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
            self.assertEqual(solicitacao.status, SolicitacaoStatus.CANCELADA)

        relatorio = self.client.get("/admin/diagnostico/exportar.xlsx", cookies=cookies_admin)
        self.assertEqual(relatorio.status_code, 200)
        self.assertIn(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            relatorio.headers.get("content-type", ""),
        )

    def test_incomplete_box_is_blocked_before_separation(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="admin-caixa-incompleta@example.com")
            instalador = self._make_instalador(db, nome="Equipe Caixa Incompleta", matricula="MAT-INCOMP", cpf="12345678925")
            caixa = self._make_caixa(db, numero="CX-INCOMP", serial="BOX-INCOMP")
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id.desc()).first()
            db.delete(hidrometro)
            solicitacao = Solicitacao(
                instalador_id=instalador.id,
                criado_por_id=admin.id,
                observacoes="Caixa incompleta",
                criado_em=utc_now(),
            )
            db.add(solicitacao)
            db.flush()
            db.add(SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1))
            db.flush()
            solicitacao = carregar_solicitacao_operacional(db, solicitacao.id)
            with self.assertRaises(ValueError) as ctx:
                separar_solicitacao(db, solicitacao, {solicitacao.itens_caixa[0].id: caixa.id})
            self.assertIn("incompleta", str(ctx.exception))

    def test_manutencao_aprovada_volta_solto_priorizada_e_caixa_incompleta_segue_fluxo(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="admin-manutencao@example.com")
            almox = self._make_user(db, nome="Almox Manutencao", email="almox-manutencao@example.com", role=UserRole.ALMOXARIFADO)
            manipulador = self._make_user(db, nome="Manip Manutencao", email="manutencao-man@example.com", role=UserRole.MANIPULADOR)
            instalador = self._make_instalador(db, nome="Campo Manutencao", matricula="MAT-MANUT", cpf="12345678971")
            caixa = self._make_caixa(db, numero="CX-MANUT", serial="BOX-MANUT")
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()
            numero_serie = hidrometro.numero_serie

            manutencao = abrir_manutencao_hidrometro(
                db,
                hidrometro,
                usuario_id=almox.id,
                motivo="Defeito antes da instalacao",
                descricao_problema="Mostrador travado na conferencia.",
            )
            self.assertEqual(manutencao.caixa_origem_id, caixa.id)
            self.assertIsNone(hidrometro.caixa_id)
            self.assertEqual(hidrometro.status, HidrometroStatus.EM_MANUTENCAO)
            self.assertTrue(hidrometro.em_manutencao)

            solicitacao = Solicitacao(
                instalador_id=instalador.id,
                criado_por_id=manipulador.id,
                observacoes="Caixa incompleta por manutencao rastreada",
                criado_em=utc_now(),
            )
            db.add(solicitacao)
            db.flush()
            db.add(SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1))
            db.flush()
            solicitacao = carregar_solicitacao_operacional(db, solicitacao.id)
            separar_solicitacao(db, solicitacao, {solicitacao.itens_caixa[0].id: caixa.id})
            confirmar_entrega_solicitacao(db, solicitacao, almox.id, observacoes="Entrega permitida com manutencao rastreada")

            enviar_assistencia(db, manutencao, usuario_id=almox.id, fornecedor="Assistencia GMF")
            registrar_retorno_assistencia(
                db,
                manutencao,
                usuario_id=almox.id,
                laudo="Aprovado apos troca de componente.",
                observacao_prioridade="Retornou aprovado; reutilizar primeiro quando fizer nova caixa.",
            )
            hidrometro_id = hidrometro.id
            db.commit()
            admin_cookies = self._auth_cookies(admin, advanced=True)

        form_page = self.client.get(f"/almoxarifado/hidrometros/{hidrometro_id}/manutencao", cookies=admin_cookies)
        self.assertEqual(form_page.status_code, 200)
        self.assertIn("maintenance-form-layout", form_page.text)
        self.assertIn("Impacto operacional", form_page.text)

        with SessionLocal() as db:
            hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
            self.assertEqual(hidrometro.status, HidrometroStatus.EM_ESTOQUE)
            self.assertEqual(hidrometro.status_operacional, "DISPONIVEL")
            self.assertIsNone(hidrometro.caixa_id)
            self.assertIsNone(hidrometro.instalador_id)
            self.assertFalse(hidrometro.em_manutencao)
            self.assertFalse(hidrometro.bloqueado_por_manutencao)
            self.assertTrue(hidrometro.retornou_manutencao)
            self.assertTrue(hidrometro.prioridade_reutilizacao)
            self.assertEqual(hidrometro.origem_prioridade, "RETORNO_ASSISTENCIA")
            self.assertEqual(listar_hidrometros_soltos_disponiveis(db, limite=1)[0].id, hidrometro_id)
            self.assertIsNotNone(
                db.query(AuditoriaLog)
                .filter(AuditoriaLog.acao == "MANUTENCAO_PRIORIDADE_REUTILIZACAO_CRIADA")
                .first()
            )

        manutencao_page = self.client.get("/almoxarifado/manutencao", cookies=admin_cookies)
        self.assertEqual(manutencao_page.status_code, 200)
        self.assertIn("maintenance-hero", manutencao_page.text)
        self.assertIn("maintenance-card", manutencao_page.text)
        self.assertIn("Fluxo encerrado", manutencao_page.text)
        self.assertNotIn("Historico de manutencao</div>", manutencao_page.text)
        self.assertNotIn("Registrar retorno</label>", manutencao_page.text)

        payload = {"serial_number": "BOX-REUSO", "numero_interno": "CX-REUSO", "hidrometro_1": numero_serie}
        for index in range(2, 7):
            payload[f"hidrometro_{index}"] = f"CX-REUSO-H{index}"
        response = self._post("/almoxarifado/caixas/nova", data=payload, cookies=admin_cookies, follow_redirects=False)
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
            nova_caixa = db.query(CaixaHidrometro).filter(CaixaHidrometro.numero_interno == "CX-REUSO").first()
            self.assertEqual(hidrometro.caixa_id, nova_caixa.id)
            self.assertFalse(hidrometro.prioridade_reutilizacao)
            self.assertIsNotNone(
                db.query(AuditoriaLog)
                .filter(AuditoriaLog.acao == "MANUTENCAO_HIDROMETRO_REUTILIZADO_NOVA_CAIXA")
                .first()
            )

    def test_manutencao_bloqueia_baixa_e_descarte_nunca_recebe_prioridade(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="admin-manutencao-descarte@example.com")
            caixa = self._make_caixa(db, numero="CX-DESCARTE", serial="BOX-DESCARTE")
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()
            manutencao = abrir_manutencao_hidrometro(
                db,
                hidrometro,
                usuario_id=admin.id,
                motivo="Defeito tecnico",
                descricao_problema="Vazamento no corpo do hidrometro.",
            )

            with self.assertRaises(ValueError) as ctx:
                aplicar_baixa_hidrometro(db, hidrometro, admin.id, observacoes="Nao pode baixar em manutencao")
            self.assertIn("manutencao", str(ctx.exception).lower())

            enviar_assistencia(db, manutencao, usuario_id=admin.id, fornecedor="Assistencia GMF")
            registrar_retorno_assistencia(
                db,
                manutencao,
                usuario_id=admin.id,
                laudo="Laudo tecnico indicou descarte definitivo.",
                decisao_final=DECISAO_DESCARTAR,
            )
            hidrometro_id = hidrometro.id
            db.commit()

        with SessionLocal() as db:
            hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
            self.assertEqual(hidrometro.status, HidrometroStatus.DESCARTADO_TECNICO)
            self.assertTrue(hidrometro.descartado_tecnico)
            self.assertFalse(hidrometro.prioridade_reutilizacao)
            self.assertNotIn(hidrometro_id, [item.id for item in listar_hidrometros_soltos_disponiveis(db)])
            self.assertIsNotNone(
                db.query(AuditoriaLog)
                .filter(AuditoriaLog.acao == "MANUTENCAO_HIDROMETRO_DESCARTADO_TECNICO")
                .first()
            )

    def test_admin_reverte_manutencao_e_retorno_para_caixa_exige_estoque(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="admin-rev-manut@example.com")
            almox = self._make_user(db, nome="Almox Rev Manut", email="almox-rev-manut@example.com", role=UserRole.ALMOXARIFADO)
            caixa = self._make_caixa(db, numero="CX-REV-MANUT", serial="BOX-REV-MANUT")
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()
            numero_serie = hidrometro.numero_serie
            manutencao = abrir_manutencao_hidrometro(
                db,
                hidrometro,
                usuario_id=almox.id,
                motivo="Envio indevido",
                descricao_problema="Operador selecionou o hidrometro errado.",
            )
            manutencao_id = manutencao.id
            hidrometro_id = hidrometro.id
            caixa_id = caixa.id
            admin_id = admin.id
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        reversoes = self.client.get("/admin/reversoes", cookies=cookies)
        self.assertEqual(reversoes.status_code, 200)
        self.assertIn("Manutencoes reversiveis", reversoes.text)
        self.assertIn(numero_serie, reversoes.text)

        form = self.client.get(f"/admin/manutencoes/{manutencao_id}/reverter", cookies=cookies)
        self.assertEqual(form.status_code, 200)
        self.assertIn("Retornar para caixa de origem em estoque", form.text)
        self.assertIn('data-scanner-field="true"', form.text)

        response = self._post(
            f"/admin/manutencoes/{manutencao_id}/reverter",
            data={
                "destino": REVERSAO_DESTINO_CAIXA_ORIGEM,
                "justificativa": "Erro operacional corrigido antes da entrega",
                "confirmacao_serial": numero_serie,
                "confirmacao_reversao": "1",
                "next_path": "/admin/reversoes",
            },
            cookies=cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
            manutencao = db.query(HidrometroManutencao).filter(HidrometroManutencao.id == manutencao_id).first()
            self.assertEqual(hidrometro.status, HidrometroStatus.EM_ESTOQUE)
            self.assertEqual(hidrometro.caixa_id, caixa_id)
            self.assertFalse(hidrometro.em_manutencao)
            self.assertFalse(hidrometro.bloqueado_por_manutencao)
            self.assertTrue(manutencao.revertida)
            self.assertEqual(manutencao.destino_reversao, REVERSAO_DESTINO_CAIXA_ORIGEM)
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ADMIN_REVERTE_MANUTENCAO_HIDROMETRO").first())

            caixa_bloqueada = self._make_caixa(db, numero="CX-RET-BLOQ", serial="BOX-RET-BLOQ")
            hidrometro_bloq = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa_bloqueada.id).order_by(Hidrometro.id).first()
            manutencao_bloq = abrir_manutencao_hidrometro(
                db,
                hidrometro_bloq,
                usuario_id=admin_id,
                motivo="Teste caixa fora estoque",
                descricao_problema="Retorno para caixa entregue deve bloquear.",
            )
            caixa_bloqueada.status = CaixaStatus.ENTREGUE
            with self.assertRaises(ValueError) as ctx:
                registrar_retorno_assistencia(
                    db,
                    manutencao_bloq,
                    usuario_id=admin_id,
                    laudo="Aprovado, mas caixa nao esta em estoque.",
                    decisao_final=DECISAO_VOLTAR_CAIXA_ORIGEM,
                )
            self.assertIn("caixa de origem precisa estar", str(ctx.exception))

            caixa_retorno = self._make_caixa(db, numero="CX-RET-OK", serial="BOX-RET-OK")
            hidrometro_retorno = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa_retorno.id).order_by(Hidrometro.id).first()
            manutencao_retorno = abrir_manutencao_hidrometro(
                db,
                hidrometro_retorno,
                usuario_id=admin_id,
                motivo="Assistencia com caixa em estoque",
                descricao_problema="Validar retorno direto para caixa em estoque.",
            )
            enviar_assistencia(db, manutencao_retorno, usuario_id=admin_id, fornecedor="Assistencia GMF")
            registrar_retorno_assistencia(
                db,
                manutencao_retorno,
                usuario_id=admin_id,
                laudo="Aprovado e caixa ainda em estoque.",
                decisao_final=DECISAO_VOLTAR_CAIXA_ORIGEM,
            )
            self.assertEqual(hidrometro_retorno.status, HidrometroStatus.EM_ESTOQUE)
            self.assertEqual(hidrometro_retorno.caixa_id, caixa_retorno.id)
            self.assertFalse(hidrometro_retorno.prioridade_reutilizacao)
            db.flush()
            self.assertIsNotNone(
                db.query(AuditoriaLog)
                .filter(AuditoriaLog.acao == "MANUTENCAO_HIDROMETRO_RETORNO_CAIXA_ORIGEM")
                .first()
            )

    def test_relatorio_hidrometros_estoque_e_campos_scanner(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="admin-hidros-estoque@example.com")
            caixa = self._make_caixa(db, numero="CX-HIDRO-EST", serial="BOX-HIDRO-EST")
            hidrometro_instalado = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()
            hidrometro_instalado.status = HidrometroStatus.INSTALADO
            numero_instalado = hidrometro_instalado.numero_serie
            hidrometro_instalado.instalado_em = utc_now()
            hidrometro_instalado.instalador_baixa_id = None
            db.flush()
            numero_em_estoque = (
                db.query(Hidrometro)
                .filter(
                    Hidrometro.caixa_id == caixa.id,
                    Hidrometro.status == HidrometroStatus.EM_ESTOQUE,
                    Hidrometro.id != hidrometro_instalado.id,
                )
                .order_by(Hidrometro.id)
                .first()
                .numero_serie
            )
            solto = Hidrometro(
                numero_serie="HIDRO-SOLTO-EST",
                status=HidrometroStatus.EM_ESTOQUE,
                caixa_id=None,
                instalador_id=None,
                retornou_manutencao=True,
                prioridade_reutilizacao=True,
                origem_prioridade="RETORNO_ASSISTENCIA",
                criado_em=utc_now(),
                atualizado_em=utc_now(),
            )
            db.add(solto)
            db.commit()
            cookies = self._auth_cookies(admin, advanced=True)

        relatorios = self.client.get("/relatorios", cookies=cookies)
        self.assertEqual(relatorios.status_code, 200)
        self.assertIn("Hidrometros em estoque", relatorios.text)

        page = self.client.get("/relatorios/hidrometros-estoque", cookies=cookies)
        self.assertEqual(page.status_code, 200)
        self.assertIn(numero_em_estoque, page.text)
        self.assertIn("CX-HIDRO-EST", page.text)
        self.assertIn("HIDRO-SOLTO-EST", page.text)
        self.assertNotIn(numero_instalado, page.text)

        export = self.client.get("/relatorios/hidrometros-estoque/exportar.xlsx", cookies=cookies)
        self.assertEqual(export.status_code, 200)
        self.assertTrue(export.content.startswith(b"PK"))

        caixa_form = self.client.get("/almoxarifado/caixas/nova", cookies=cookies)
        self.assertEqual(caixa_form.status_code, 200)
        self.assertIn('name="hidrometro_1"', caixa_form.text)
        self.assertIn('data-scanner-field="true"', caixa_form.text)

        baixa = self.client.get("/manipulador/baixa-hidrometro", cookies=cookies)
        self.assertEqual(baixa.status_code, 200)
        self.assertIn('name="numero_serie"', baixa.text)
        self.assertIn('data-scanner-field="true"', baixa.text)

    def test_dashboard_alerta_prioridade_parada_e_perfis_sem_permissao_bloqueados(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="admin-alerta-manutencao@example.com")
            manipulador = self._make_user(db, nome="Manip Sem Manutencao", email="manip-sem-manut@example.com", role=UserRole.MANIPULADOR)
            user_instalador = self._make_user(db, nome="Instalador Sem Manutencao", email="inst-sem-manut@example.com", role=UserRole.INSTALADOR)
            caixa = self._make_caixa(db, numero="CX-ALERTA-MAN", serial="BOX-ALERTA-MAN")
            hidrometro = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).first()
            numero_serie = hidrometro.numero_serie
            manutencao = abrir_manutencao_hidrometro(
                db,
                hidrometro,
                usuario_id=admin.id,
                motivo="Retorno para alerta",
                descricao_problema="Teste de prioridade parada.",
            )
            enviar_assistencia(db, manutencao, usuario_id=admin.id, fornecedor="Assistencia GMF")
            registrar_retorno_assistencia(db, manutencao, usuario_id=admin.id, laudo="Aprovado para retorno.")
            hidrometro.data_retorno_manutencao = utc_now() - timedelta(days=16)
            hidrometro_id = hidrometro.id
            db.commit()
            admin_cookies = self._auth_cookies(admin, advanced=True)
            manip_cookies = self._auth_cookies(manipulador)
            instalador_cookies = self._auth_cookies(user_instalador)

        manutencao_page = self.client.get("/almoxarifado/manutencao", cookies=admin_cookies)
        self.assertEqual(manutencao_page.status_code, 200)
        self.assertIn("Hidrometros retornados da assistencia parados", manutencao_page.text)
        self.assertIn(numero_serie, manutencao_page.text)

        estoque_inteligente = self.client.get("/almoxarifado/estoque-inteligente", cookies=admin_cookies)
        self.assertEqual(estoque_inteligente.status_code, 200)
        self.assertIn("Retornados parados", estoque_inteligente.text)
        self.assertIn(numero_serie, estoque_inteligente.text)

        blocked_man = self._post(
            f"/almoxarifado/hidrometros/{hidrometro_id}/prioridade/remover",
            data={"justificativa": "Tentativa sem permissao"},
            cookies=manip_cookies,
            follow_redirects=False,
        )
        self.assertEqual(blocked_man.status_code, 403)
        blocked_instalador = self.client.get("/almoxarifado/manutencao", cookies=instalador_cookies, follow_redirects=False)
        self.assertIn(blocked_instalador.status_code, {302, 403})

        with SessionLocal() as db:
            resumo = resumo_manutencao_hidrometros(db)
            hidrometro = db.query(Hidrometro).filter(Hidrometro.id == hidrometro_id).first()
            self.assertEqual(resumo["parados_alerta"], 1)
            self.assertEqual(resumo["prioridade_vencida"], 1)
            self.assertTrue(hidrometro.prioridade_reutilizacao)
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ACESSO_NEGADO").first())

    def test_productivity_finalizes_box_and_export_is_admin_only(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="prod-admin@example.com")
            manipulador = self._make_user(db, nome="Prod Campo", email="prod-man@example.com", role=UserRole.MANIPULADOR)
            instalador = self._make_instalador(db, nome="Equipe Produtiva", matricula="MAT-PROD", cpf="12345678941")
            caixa = self._make_caixa(db, numero="CX-PROD", serial="BOX-PROD")
            for hidrometro in db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).all():
                hidrometro.status = HidrometroStatus.COM_INSTALADOR
                hidrometro.instalador_id = instalador.id
            caixa.status = CaixaStatus.ENTREGUE
            caixa.instalador_id = instalador.id
            db.flush()

            for hidrometro in db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa.id).order_by(Hidrometro.id).all():
                aplicar_baixa_hidrometro(db, hidrometro, manipulador.id, observacoes="Produtividade")

            caixa_id = caixa.id
            instalador_id = instalador.id
            db.commit()
            cookies_admin = self._auth_cookies(admin, advanced=True)
            cookies_man = self._auth_cookies(manipulador)

        with SessionLocal() as db:
            caixa = db.query(CaixaHidrometro).filter(CaixaHidrometro.id == caixa_id).first()
            self.assertEqual(caixa.status, CaixaStatus.INSTALADA)
            self.assertIsNone(caixa.instalador_id)
            self.assertEqual(db.query(InstalacaoHidrometro).filter(InstalacaoHidrometro.instalador_id == instalador_id).count(), 6)

        page = self.client.get("/produtividade", cookies=cookies_man)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Equipe Produtiva", page.text)
        self.assertNotIn("/produtividade/exportar.xlsx", page.text)

        blocked_export = self.client.get("/produtividade/exportar.xlsx", cookies=cookies_man)
        self.assertEqual(blocked_export.status_code, 403)

        admin_export = self.client.get("/produtividade/exportar.xlsx", cookies=cookies_admin)
        self.assertEqual(admin_export.status_code, 200)
        self.assertTrue(admin_export.content.startswith(b"PK"))

        detalhe = self.client.get(f"/manipulador/instaladores/{instalador_id}", cookies=cookies_man)
        self.assertEqual(detalhe.status_code, 200)
        self.assertIn("Este instalador nao possui caixas em posse.", detalhe.text)

    def test_carcacas_balance_and_admin_export(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="carcaca-admin@example.com")
            almox = self._make_user(db, nome="Almox Carcaca", email="carcaca-almox@example.com", role=UserRole.ALMOXARIFADO)
            instalador = self._make_instalador(db, nome="Devolve Carcaca", matricula="MAT-CARC", cpf="12345678942")
            db.commit()
            admin_cookies = self._auth_cookies(admin, advanced=True)
            almox_cookies = self._auth_cookies(almox)
            instalador_id = instalador.id

        entrada = self._post(
            "/almoxarifado/carcacas/devolucao",
            data={"instalador_id": instalador_id, "quantidade": 10, "documento_referencia": "FORM-10"},
            cookies=almox_cookies,
            follow_redirects=False,
        )
        self.assertEqual(entrada.status_code, 302)

        saida = self._post(
            "/almoxarifado/carcacas/recolhimento",
            data={"quantidade": 4, "documento_referencia": "SABESP-04"},
            cookies=almox_cookies,
            follow_redirects=False,
        )
        self.assertEqual(saida.status_code, 302)

        bloqueada = self._post(
            "/almoxarifado/carcacas/recolhimento",
            data={"quantidade": 20},
            cookies=almox_cookies,
            follow_redirects=False,
        )
        self.assertEqual(bloqueada.status_code, 400)
        self.assertIn("saldo atual", bloqueada.text)

        with SessionLocal() as db:
            movimentos = db.query(CarcacaMovimentacao).count()
            self.assertEqual(movimentos, 2)
            logs = db.query(AuditoriaLog).filter(AuditoriaLog.acao.in_(["CARCACA_DEVOLUCAO", "CARCACA_RECOLHIMENTO_SABESP"])).count()
            self.assertEqual(logs, 2)

        page = self.client.get("/almoxarifado/carcacas", cookies=almox_cookies)
        self.assertEqual(page.status_code, 200)
        self.assertIn("6", page.text)

        export = self.client.get("/almoxarifado/carcacas/exportar.xlsx", cookies=admin_cookies)
        self.assertEqual(export.status_code, 200)
        self.assertTrue(export.content.startswith(b"PK"))

    def test_transferencia_empresa_updates_stock_and_full_box_report(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="trans-admin@example.com")
            almox = self._make_user(db, nome="Almox Transfer", email="trans-almox@example.com", role=UserRole.ALMOXARIFADO)
            caixa = self._make_caixa(db, numero="CX-TRANS", serial="BOX-TRANS")
            tipo = self._make_tipo_peca(db, nome="LACRE-TRANS", quantidade=20)
            db.commit()
            admin_cookies = self._auth_cookies(admin, advanced=True)
            almox_cookies = self._auth_cookies(almox)
            caixa_id = caixa.id
            tipo_id = tipo.id

        response = self._post(
            "/almoxarifado/transferencias/nova",
            data={
                "empresa_destino": "Empresa Parceira",
                "responsavel": "Supervisor Operacional",
                "caixa_ids": str(caixa_id),
                f"peca_{tipo_id}": "5",
                "confirmacao_contexto": "1",
            },
            cookies=almox_cookies,
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 302)

        with SessionLocal() as db:
            caixa = db.query(CaixaHidrometro).filter(CaixaHidrometro.id == caixa_id).first()
            estoque = db.query(EstoquePeca).filter(EstoquePeca.tipo_peca_id == tipo_id).first()
            transferencia = db.query(TransferenciaEmpresa).first()
            link = db.query(TransferenciaEmpresaCaixa).filter(TransferenciaEmpresaCaixa.caixa_id == caixa_id).first()
            self.assertEqual(caixa.status, CaixaStatus.TRANSFERIDA_OUTRA_EMPRESA)
            self.assertEqual(estoque.quantidade_atual, 15)
            self.assertIsNotNone(transferencia)
            self.assertIsNotNone(link)
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "TRANSFERENCIA_EMPRESA").first())

        estoque_page = self.client.get("/dashboard", cookies=almox_cookies)
        self.assertEqual(estoque_page.status_code, 200)
        report = self.client.get("/relatorios/caixas-completo", cookies=admin_cookies)
        self.assertEqual(report.status_code, 200)
        self.assertIn("Empresa Parceira", report.text)
        self.assertIn("Transferida", report.text)

    def test_almox_separation_and_delivery_pages_render_and_complete_operation(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="crud-admin@example.com")
            almox = self._make_user(db, nome="Almox CRUD", email="crud-almox@example.com", role=UserRole.ALMOXARIFADO)
            manipulador = self._make_user(db, nome="Manip CRUD", email="crud-man@example.com", role=UserRole.MANIPULADOR)
            instalador = self._make_instalador(db, nome="Campo CRUD", matricula="MAT-CRUD", cpf="12345678961")
            caixa = self._make_caixa(db, numero="CX-CRUD", serial="BOX-CRUD")
            tipo = self._make_tipo_peca(db, nome="LACRE-CRUD", quantidade=12)
            solicitacao = Solicitacao(
                instalador_id=instalador.id,
                criado_por_id=manipulador.id,
                observacoes="Fluxo operacional completo",
                criado_em=utc_now(),
            )
            db.add(solicitacao)
            db.flush()
            item_caixa = SolicitacaoItemCaixa(solicitacao_id=solicitacao.id, quantidade_solicitada=1)
            item_peca = SolicitacaoItemPeca(solicitacao_id=solicitacao.id, tipo_peca_id=tipo.id, quantidade_solicitada=2)
            db.add(item_caixa)
            db.add(item_peca)
            db.flush()
            solicitacao_id = solicitacao.id
            item_caixa_id = item_caixa.id
            caixa_id = caixa.id
            caixa_numero = caixa.numero_interno
            caixa_serial = caixa.serial_number
            db.commit()
            almox_cookies = self._auth_cookies(almox)
            admin_cookies = self._auth_cookies(admin, advanced=True)

        separar_page = self.client.get(f"/almoxarifado/solicitacoes/{solicitacao_id}/separar", cookies=almox_cookies)
        self.assertEqual(separar_page.status_code, 200)
        self.assertIn("Selecione as caixas fisicas", separar_page.text)
        self.assertIn(caixa_numero, separar_page.text)

        separar_response = self._post(
            f"/almoxarifado/solicitacoes/{solicitacao_id}/separar",
            data={f"item_caixa_{item_caixa_id}": str(caixa_id), "confirmacao_contexto": "1"},
            cookies=almox_cookies,
            follow_redirects=False,
        )
        self.assertEqual(separar_response.status_code, 302)
        self.assertEqual(separar_response.headers.get("location"), f"/almoxarifado/solicitacoes/{solicitacao_id}/entregar")

        entregar_page = self.client.get(f"/almoxarifado/solicitacoes/{solicitacao_id}/entregar", cookies=almox_cookies)
        self.assertEqual(entregar_page.status_code, 200)
        self.assertIn("Conferencia final obrigatoria", entregar_page.text)
        self.assertIn(caixa_serial, entregar_page.text)

        entregar_response = self._post(
            f"/almoxarifado/solicitacoes/{solicitacao_id}/entregar",
            data={
                f"confirm_numero_{item_caixa_id}": caixa_numero,
                f"confirm_serial_{item_caixa_id}": caixa_serial,
                "confirmacao_contexto": "1",
                "observacoes": "Entrega conferida no teste operacional",
            },
            cookies=almox_cookies,
            follow_redirects=False,
        )
        self.assertEqual(entregar_response.status_code, 302)

        for path in ["/admin/diagnostico", "/admin/monitoramento"]:
            page = self.client.get(path, cookies=admin_cookies)
            self.assertEqual(page.status_code, 200, msg=path)

        with SessionLocal() as db:
            solicitacao = db.query(Solicitacao).filter(Solicitacao.id == solicitacao_id).first()
            self.assertEqual(solicitacao.status, SolicitacaoStatus.ENTREGUE)
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "SEPARAR_SOLICITACAO").first())
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ENTREGAR_SOLICITACAO").first())

    def test_intelligent_stock_dashboard_calculates_alerts_permissions_and_audit(self):
        with SessionLocal() as db:
            admin = self._make_user(db, email="intel-admin@example.com")
            almox = self._make_user(db, nome="Almox Intel", email="intel-almox@example.com", role=UserRole.ALMOXARIFADO)
            user_instalador = self._make_user(db, nome="Instalador Intel", email="intel-inst@example.com", role=UserRole.INSTALADOR)
            instalador_risco = self._make_instalador(db, nome="Campo Alto Consumo", matricula="MAT-INT-01", cpf="12345678962")
            instalador_base = self._make_instalador(db, nome="Campo Base", matricula="MAT-INT-02", cpf="12345678963")
            tipo_risco = self._make_tipo_peca(db, nome="LACRE-INTEL", quantidade=10)
            tipo_base = self._make_tipo_peca(db, nome="ARRUELA-INTEL", quantidade=60)
            estoque_risco = db.query(EstoquePeca).filter(EstoquePeca.tipo_peca_id == tipo_risco.id).first()
            estoque_base = db.query(EstoquePeca).filter(EstoquePeca.tipo_peca_id == tipo_base.id).first()
            estoque_risco.quantidade_maxima = 100
            estoque_base.quantidade_maxima = 100
            caixa_risco = self._make_caixa(db, numero="CX-INT-RISCO", serial="BOX-INT-RISCO")
            caixa_base = self._make_caixa(db, numero="CX-INT-BASE", serial="BOX-INT-BASE")
            hidros_risco = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa_risco.id).order_by(Hidrometro.id).all()
            hidros_base = db.query(Hidrometro).filter(Hidrometro.caixa_id == caixa_base.id).order_by(Hidrometro.id).all()
            db.add_all(
                [
                    MovimentacaoPeca(
                        tipo=MovimentacaoTipo.SAIDA,
                        tipo_peca_id=tipo_risco.id,
                        quantidade=6,
                        instalador_id=instalador_risco.id,
                        registrado_por_id=almox.id,
                        criado_em=utc_now() - timedelta(days=1),
                    ),
                    MovimentacaoPeca(
                        tipo=MovimentacaoTipo.SAIDA,
                        tipo_peca_id=tipo_risco.id,
                        quantidade=4,
                        instalador_id=instalador_risco.id,
                        registrado_por_id=almox.id,
                        criado_em=utc_now(),
                    ),
                    MovimentacaoPeca(
                        tipo=MovimentacaoTipo.SAIDA,
                        tipo_peca_id=tipo_base.id,
                        quantidade=2,
                        instalador_id=instalador_base.id,
                        registrado_por_id=almox.id,
                        criado_em=utc_now(),
                    ),
                ]
            )
            db.add(
                InstalacaoHidrometro(
                    instalador_id=instalador_risco.id,
                    hidrometro_id=hidros_risco[0].id,
                    caixa_id=caixa_risco.id,
                    data_instalacao=utc_now(),
                    usuario_registro_id=almox.id,
                )
            )
            for hidrometro in hidros_base[:4]:
                db.add(
                    InstalacaoHidrometro(
                        instalador_id=instalador_base.id,
                        hidrometro_id=hidrometro.id,
                        caixa_id=caixa_base.id,
                        data_instalacao=utc_now(),
                        usuario_registro_id=almox.id,
                    )
                )
            db.commit()
            admin_cookies = self._auth_cookies(admin, advanced=True)
            almox_cookies = self._auth_cookies(almox)
            instalador_cookies = self._auth_cookies(user_instalador)

        with SessionLocal() as db:
            dashboard = montar_dashboard_estoque_inteligente(db, dias_periodo=30)
            item = next(item for item in dashboard["itens"] if item["nome"] == "LACRE-INTEL")
            self.assertEqual(item["status"], "CRITICO")
            self.assertEqual(item["minimo_qtd"], 20)
            self.assertEqual(item["consumo_periodo"], 10)
            self.assertEqual(item["consumo_medio_diario"], 5)
            self.assertEqual(item["dias_restantes"], 2)
            self.assertTrue(any(item["status"] == "SUSPEITO" for item in dashboard["analise_instaladores"]))

        admin_page = self.client.get("/almoxarifado/estoque-inteligente", cookies=admin_cookies)
        self.assertEqual(admin_page.status_code, 200)
        self.assertIn("LACRE-INTEL", admin_page.text)
        self.assertIn("CRITICO", admin_page.text)
        self.assertIn("Analise por instalador", admin_page.text)
        self.assertIn("Campo Alto Consumo", admin_page.text)

        almox_page = self.client.get("/almoxarifado/estoque-inteligente?status=critico&q=LACRE", cookies=almox_cookies)
        self.assertEqual(almox_page.status_code, 200)
        self.assertIn("LACRE-INTEL", almox_page.text)
        self.assertNotIn("Analise por instalador", almox_page.text)
        self.assertNotIn("Campo Alto Consumo", almox_page.text)

        blocked = self.client.get("/almoxarifado/estoque-inteligente", cookies=instalador_cookies, follow_redirects=False)
        self.assertIn(blocked.status_code, {302, 403})

        with SessionLocal() as db:
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "ESTOQUE_INTELIGENTE_CRITICO").first())
            self.assertIsNotNone(db.query(AuditoriaLog).filter(AuditoriaLog.acao == "CONSUMO_INSTALADOR_SUSPEITO").first())

    def test_csrf_blocks_post_without_token(self):
        response = self.client.post(
            "/auth/login",
            data={"email": "qualquer@example.com", "senha": "qualquer"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("Sessao de formulario invalida", response.text)


if __name__ == "__main__":
    unittest.main()
