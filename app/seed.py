#!/usr/bin/env python3

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import Base, SessionLocal, engine
from app.models import UserRole, Usuario
from app.security import hash_senha, validar_politica_senha
from app.services.auditoria import registrar_auditoria


def _resolve_admin_credentials(non_interactive: bool, optional: bool) -> tuple[str, str, str] | None:
    nome = (
        os.getenv("BOOTSTRAP_ADMIN_NAME")
        or os.getenv("INITIAL_ADMIN_NAME")
        or "Administrador"
    ).strip() or "Administrador"
    email = (os.getenv("BOOTSTRAP_ADMIN_EMAIL") or os.getenv("INITIAL_ADMIN_EMAIL") or "").strip().lower()
    senha = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or os.getenv("INITIAL_ADMIN_PASSWORD") or ""

    if email and senha:
        return nome, email, senha

    if non_interactive:
        if optional:
            print("[SEED] Nenhuma credencial inicial configurada. Seed opcional ignorado.")
            return None
        raise SystemExit(
            "[SEED] Defina BOOTSTRAP_ADMIN_EMAIL e BOOTSTRAP_ADMIN_PASSWORD para criar o primeiro administrador."
        )

    if not sys.stdin.isatty():
        if optional:
            print("[SEED] Ambiente sem terminal interativo e sem credenciais iniciais. Seed ignorado.")
            return None
        raise SystemExit(
            "[SEED] Terminal nao interativo sem credenciais iniciais. Defina BOOTSTRAP_ADMIN_EMAIL e BOOTSTRAP_ADMIN_PASSWORD."
        )

    print("[SEED] Criacao segura do primeiro administrador")
    nome_input = input("Nome do administrador [Administrador]: ").strip()
    email = input("Email do administrador: ").strip().lower()
    senha = getpass.getpass("Senha inicial: ")
    confirmar = getpass.getpass("Confirmar senha: ")
    if senha != confirmar:
        raise SystemExit("[SEED] A confirmacao da senha nao confere.")
    return nome_input or nome, email, senha


def seed(non_interactive: bool = False, optional: bool = False) -> None:
    if os.getenv("APP_ENV", "development").strip().lower() not in {"prod", "production"}:
        Base.metadata.create_all(bind=engine)

    db = SessionLocal()
    try:
        admins_ativos = db.query(Usuario).filter(Usuario.role == UserRole.ADMIN, Usuario.ativo == True).all()
        if len(admins_ativos) > 1:
            raise SystemExit("[SEED] Existe mais de um ADMIN ativo. Corrija antes do deploy.")
        if len(admins_ativos) == 1:
            print(f"[SEED] Admin ativo ja existe: {admins_ativos[0].email}")
            return

        credenciais = _resolve_admin_credentials(non_interactive=non_interactive, optional=optional)
        if credenciais is None:
            return

        nome, email, senha = credenciais
        if not email:
            raise SystemExit("[SEED] Email do administrador e obrigatorio.")

        erro_senha = validar_politica_senha(senha, nome=nome, email=email)
        if erro_senha:
            raise SystemExit(f"[SEED] {erro_senha}")

        admin = Usuario(
            nome=nome,
            email=email,
            senha_hash=hash_senha(senha),
            role=UserRole.ADMIN,
            ativo=True,
            token_version=0,
        )
        db.add(admin)
        db.flush()
        registrar_auditoria(
            db=db,
            acao="BOOTSTRAP_ADMIN",
            usuario_id=admin.id,
            tabela="usuarios",
            registro_id=admin.id,
            descricao="Administrador inicial criado pelo seed",
            dados_depois={"email": admin.email, "role": admin.role.value},
        )
        db.commit()
        print(f"[SEED] Administrador criado com sucesso: {email}")
    except Exception as exc:
        db.rollback()
        raise SystemExit(f"[SEED] Erro ao criar admin: {exc}")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap seguro do primeiro administrador")
    parser.add_argument("--non-interactive", action="store_true", help="Nao solicitar dados no terminal")
    parser.add_argument("--optional", action="store_true", help="Nao falhar se as credenciais iniciais nao estiverem definidas")
    args = parser.parse_args()
    seed(non_interactive=args.non_interactive, optional=args.optional)


if __name__ == "__main__":
    main()
