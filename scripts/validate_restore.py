from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import subprocess
import sys
from pathlib import Path


REQUIRED_TABLES = {
    "usuarios",
    "caixas_hidrometros",
    "hidrômetros",
    "movimentacoes_material",
    "movimentacoes_pecas",
    "auditoria_logs",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Restaura backup em alvo separado e valida integridade basica.")
    parser.add_argument("backup", help="Arquivo de backup.")
    parser.add_argument("--target-database-url", required=True, help="DATABASE_URL do ambiente separado.")
    parser.add_argument("--force", action="store_true", help="Permite alvo fora do padrao seguro.")
    parser.add_argument("--status-file", default=os.getenv("RESTORE_STATUS_FILE"), help="Arquivo JSON para registrar a ultima validacao.")
    args = parser.parse_args()

    restore_script = Path(__file__).with_name("restore_database.py")
    command = [
        sys.executable,
        str(restore_script),
        args.backup,
        "--target-database-url",
        args.target_database_url,
    ]
    if args.force:
        command.append("--force")

    subprocess.run(command, check=True)

    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(args.target_database_url, pool_pre_ping=True)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        tables = set(inspect(connection).get_table_names())
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise RuntimeError("Restore incompleto. Tabelas ausentes: " + ", ".join(missing))

        counts = {}
        for table_name in sorted(REQUIRED_TABLES & tables):
            counts[table_name] = int(connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).scalar() or 0)

    payload = {
        "status": "ok",
        "backup": str(Path(args.backup)),
        "validado_em": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "tabelas": len(tables),
        "contagens": counts,
    }
    if args.status_file:
        Path(args.status_file).parent.mkdir(parents=True, exist_ok=True)
        Path(args.status_file).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
