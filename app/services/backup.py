from __future__ import annotations

import os
import json
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.database import DATABASE_URL
from app.utils import utc_now


DEFAULT_BACKUP_DIR = "backups"
DEFAULT_BACKUP_RETENTION_DAYS = 7
POSTGRES_CUSTOM_DUMP_MAGIC = b"PGDMP"


@dataclass(frozen=True)
class BackupResult:
    path: Path
    filename: str
    engine: str
    size_bytes: int
    created_at: datetime


def _safe_timestamp() -> str:
    return utc_now().strftime("%Y%m%d_%H%M%S")


def backup_dir_from_env() -> Path:
    return Path(os.getenv("BACKUP_DIR", DEFAULT_BACKUP_DIR))


def backup_retention_days_from_env() -> int:
    raw_value = os.getenv("BACKUP_RETENTION_DAYS", str(DEFAULT_BACKUP_RETENTION_DAYS))
    try:
        return max(int(raw_value), 0)
    except ValueError:
        return DEFAULT_BACKUP_RETENTION_DAYS


def _cleanup_old_backups(backup_dir: Path, retention_days: int) -> None:
    if retention_days <= 0 or not backup_dir.exists():
        return
    cutoff = utc_now() - timedelta(days=retention_days)
    for item in backup_dir.glob("hidrometros_backup_*"):
        if not item.is_file():
            continue
        try:
            modified = datetime.utcfromtimestamp(item.stat().st_mtime)
            if modified < cutoff:
                item.unlink()
        except OSError:
            continue


def _postgres_pg_dump(database_url: str, output_path: Path) -> None:
    parsed = urlparse(database_url)
    if not parsed.hostname or not parsed.path.strip("/"):
        raise RuntimeError("DATABASE_URL PostgreSQL invalida para backup.")

    pg_dump = os.getenv("PG_DUMP_BIN", "pg_dump")
    username = unquote(parsed.username or "")
    database_name = unquote(parsed.path.strip("/"))
    command = [
        pg_dump,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(output_path),
        "--host",
        parsed.hostname,
        "--port",
        str(parsed.port or 5432),
    ]
    if username:
        command.extend(["--username", username])
    command.append(database_name)
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)

    if shutil.which(pg_dump):
        try:
            completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
        except subprocess.SubprocessError as exc:
            raise RuntimeError(f"Falha ao gerar backup PostgreSQL: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "pg_dump falhou").strip()
            raise RuntimeError(f"Falha ao gerar backup PostgreSQL: {detail}")
        return

    if _postgres_pg_dump_via_docker(parsed, output_path):
        return

    raise RuntimeError(
        "Backup indisponivel: pg_dump nao instalado no servidor da aplicacao e fallback Docker indisponivel. "
        "Instale postgresql-client, configure PG_DUMP_BIN ou execute pelo container PostgreSQL."
    )


def _postgres_pg_dump_via_docker(parsed, output_path: Path) -> bool:
    docker_bin = os.getenv("DOCKER_BIN", "docker")
    if not shutil.which(docker_bin):
        return False

    container = os.getenv("PG_DUMP_DOCKER_CONTAINER", "hidrometros_db").strip()
    if not container:
        return False
    if not _docker_container_available(container, docker_bin=docker_bin):
        return False

    container_user, container_db = _docker_postgres_identity(container, docker_bin=docker_bin)
    username = os.getenv("PG_DUMP_DOCKER_USER") or container_user or unquote(parsed.username or os.getenv("POSTGRES_USER", ""))
    database_name = os.getenv("PG_DUMP_DOCKER_DB") or container_db or unquote(parsed.path.strip("/") or os.getenv("POSTGRES_DB", ""))
    if not username or not database_name:
        return False

    command = [
        docker_bin,
        "exec",
        container,
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--username",
        username,
        database_name,
    ]
    try:
        with output_path.open("wb") as output_file:
            completed = subprocess.run(command, stdout=output_file, stderr=subprocess.PIPE, timeout=300)
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False

    if completed.returncode != 0:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _docker_postgres_identity(container: str, *, docker_bin: str = "docker") -> tuple[str | None, str | None]:
    if not shutil.which(docker_bin):
        return None, None
    try:
        completed = subprocess.run(
            [
                docker_bin,
                "exec",
                container,
                "sh",
                "-lc",
                'printf "%s\\n%s\\n" "$POSTGRES_USER" "$POSTGRES_DB"',
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None, None
    if completed.returncode != 0:
        return None, None
    lines = [line.strip() for line in completed.stdout.splitlines()]
    user = lines[0] if len(lines) >= 1 and lines[0] else None
    db_name = lines[1] if len(lines) >= 2 and lines[1] else None
    return user, db_name


def _docker_container_available(container: str, *, docker_bin: str = "docker") -> bool:
    if not shutil.which(docker_bin):
        return False
    try:
        completed = subprocess.run(
            [docker_bin, "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0 and completed.stdout.strip().lower() == "true"


def _sqlite_backup(database_url: str, output_path: Path) -> None:
    raw_path = database_url.replace("sqlite:///", "", 1)
    source = Path(raw_path)
    if not source.exists():
        raise RuntimeError("Arquivo SQLite nao encontrado para backup.")
    shutil.copy2(source, output_path)


def _validar_arquivo_backup(path: Path, *, engine: str) -> None:
    if not path.exists() or not path.is_file():
        raise RuntimeError("Backup falhou: arquivo nao foi gerado.")

    size = path.stat().st_size
    if size <= 0:
        raise RuntimeError("Backup falhou: arquivo gerado esta vazio.")

    if engine == "postgresql":
        with path.open("rb") as handle:
            magic = handle.read(len(POSTGRES_CUSTOM_DUMP_MAGIC))
        if magic != POSTGRES_CUSTOM_DUMP_MAGIC:
            raise RuntimeError("Backup falhou: arquivo PostgreSQL gerado nao possui formato custom valido.")
    elif engine == "sqlite":
        with path.open("rb") as handle:
            header = handle.read(16)
        if not header.startswith(b"SQLite format 3"):
            raise RuntimeError("Backup falhou: arquivo SQLite gerado nao possui formato valido.")


def criar_backup_banco(
    *,
    database_url: str | None = None,
    backup_dir: Path | None = None,
    retention_days: int | None = None,
) -> BackupResult:
    url = database_url or DATABASE_URL
    target_dir = backup_dir or backup_dir_from_env()
    target_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_old_backups(target_dir, backup_retention_days_from_env() if retention_days is None else retention_days)

    timestamp = _safe_timestamp()
    if url.startswith("postgresql"):
        filename = f"hidrometros_backup_{timestamp}.dump"
        path = target_dir / filename
        _postgres_pg_dump(url, path)
        engine = "postgresql"
    elif url.startswith("sqlite"):
        filename = f"hidrometros_backup_{timestamp}.sqlite3"
        path = target_dir / filename
        _sqlite_backup(url, path)
        engine = "sqlite"
    else:
        raise RuntimeError("Tipo de banco nao suportado para backup automatico.")

    try:
        _validar_arquivo_backup(path, engine=engine)
    except RuntimeError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return BackupResult(
        path=path,
        filename=filename,
        engine=engine,
        size_bytes=path.stat().st_size,
        created_at=utc_now(),
    )


def ultimo_backup_local(backup_dir: Path | None = None) -> BackupResult | None:
    target_dir = backup_dir or backup_dir_from_env()
    if not target_dir.exists():
        return None
    candidates = [item for item in target_dir.glob("hidrometros_backup_*") if item.is_file()]
    if not candidates:
        return None
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    engine = "postgresql" if latest.suffix == ".dump" else "sqlite"
    return BackupResult(
        path=latest,
        filename=latest.name,
        engine=engine,
        size_bytes=latest.stat().st_size,
        created_at=datetime.utcfromtimestamp(latest.stat().st_mtime),
    )


def backup_operacional_status(backup_dir: Path | None = None) -> dict[str, object]:
    target_dir = backup_dir or backup_dir_from_env()
    ultimo = ultimo_backup_local(target_dir)
    pg_dump_bin = os.getenv("PG_DUMP_BIN", "pg_dump")
    docker_bin = os.getenv("DOCKER_BIN", "docker")
    docker_container = os.getenv("PG_DUMP_DOCKER_CONTAINER", "hidrometros_db").strip()
    pg_dump_ok = bool(shutil.which(pg_dump_bin))
    docker_fallback_ok = bool(docker_container and _docker_container_available(docker_container, docker_bin=docker_bin))
    return {
        "diretorio": str(target_dir),
        "retencao_dias": backup_retention_days_from_env(),
        "ultimo_backup": ultimo,
        "configurado": target_dir.exists() or bool(os.getenv("BACKUP_DIR")),
        "pg_dump_disponivel": pg_dump_ok,
        "docker_fallback_configurado": docker_fallback_ok,
        "backup_postgres_disponivel": pg_dump_ok or docker_fallback_ok,
    }


def restore_status_file_from_env() -> Path:
    default_path = backup_dir_from_env() / "restore_status.json"
    return Path(os.getenv("RESTORE_STATUS_FILE", str(default_path)))


def restore_validation_status(status_file: Path | None = None) -> dict[str, object] | None:
    target = status_file or restore_status_file_from_env()
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_restore_validation_status(payload: dict[str, object], status_file: Path | None = None) -> Path:
    target = status_file or restore_status_file_from_env()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target
