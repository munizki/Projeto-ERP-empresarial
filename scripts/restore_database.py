from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse


SAFE_TARGET_MARKERS = ("test", "teste", "restore", "staging", "homolog", "dev")


def _is_transaction_timeout_compat_issue(detail: str) -> bool:
    normalized = detail.lower()
    return (
        'unrecognized configuration parameter "transaction_timeout"' in normalized
        and "warning: errors ignored on restore: 1" in normalized
        and normalized.count("pg_restore: error:") == 1
    )


def _target_looks_safe(database_url: str) -> bool:
    parsed = urlparse(database_url)
    database_name = unquote(parsed.path.strip("/")).lower()
    return any(marker in database_name for marker in SAFE_TARGET_MARKERS)


def _restore_postgres(backup_path: Path, database_url: str) -> None:
    parsed = urlparse(database_url)
    if not parsed.hostname or not parsed.path.strip("/"):
        raise RuntimeError("DATABASE_URL PostgreSQL invalida para restore.")

    pg_restore = os.getenv("PG_RESTORE_BIN", "pg_restore")
    username = unquote(parsed.username or "")
    database_name = unquote(parsed.path.strip("/"))
    command = [
        pg_restore,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-privileges",
        "--host",
        parsed.hostname,
        "--port",
        str(parsed.port or 5432),
        "--dbname",
        database_name,
    ]
    if username:
        command.extend(["--username", username])
    command.append(str(backup_path))

    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)

    try:
        completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=900)
    except FileNotFoundError as exc:
        raise RuntimeError("pg_restore nao encontrado. Instale o cliente PostgreSQL no ambiente de restore.") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "pg_restore falhou").strip()
        if _is_transaction_timeout_compat_issue(detail):
            return
        raise RuntimeError(f"Falha no restore PostgreSQL: {detail}")


def _restore_sqlite(backup_path: Path, database_url: str, *, force: bool) -> None:
    target = Path(database_url.replace("sqlite:///", "", 1))
    if target.exists() and not force:
        raise RuntimeError("Arquivo SQLite de destino ja existe. Use --force para sobrescrever.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup_path, target)


def main() -> int:
    parser = argparse.ArgumentParser(description="Restaura um backup em ambiente separado.")
    parser.add_argument("backup", help="Arquivo .dump ou .sqlite3 gerado pelo backup.")
    parser.add_argument("--target-database-url", required=True, help="DATABASE_URL do ambiente de restore.")
    parser.add_argument("--force", action="store_true", help="Confirma restore em alvo fora do padrao seguro.")
    args = parser.parse_args()

    backup_path = Path(args.backup)
    if not backup_path.exists():
        raise RuntimeError(f"Backup nao encontrado: {backup_path}")

    target_url = args.target_database_url
    if not args.force and not _target_looks_safe(target_url):
        raise RuntimeError(
            "Destino de restore nao parece ambiente separado. Use banco com nome test/restore/staging/homolog/dev "
            "ou confirme conscientemente com --force."
        )

    if target_url.startswith("postgresql"):
        _restore_postgres(backup_path, target_url)
    elif target_url.startswith("sqlite"):
        _restore_sqlite(backup_path, target_url, force=args.force)
    else:
        raise RuntimeError("Tipo de banco nao suportado para restore.")

    print(f"Restore concluido em ambiente alvo: {urlparse(target_url).hostname or 'local'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
