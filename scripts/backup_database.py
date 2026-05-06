from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera backup operacional do banco de dados.")
    parser.add_argument("--database-url", default=None, help="DATABASE_URL de origem. Padrao: ambiente atual.")
    parser.add_argument("--backup-dir", default=None, help="Diretorio de destino do backup.")
    parser.add_argument("--retention-days", type=int, default=None, help="Retencao local em dias.")
    args = parser.parse_args()

    from app.services.backup import criar_backup_banco

    resultado = criar_backup_banco(
        database_url=args.database_url,
        backup_dir=Path(args.backup_dir) if args.backup_dir else None,
        retention_days=args.retention_days,
    )
    print(json.dumps({
        "arquivo": str(resultado.path),
        "filename": resultado.filename,
        "engine": resultado.engine,
        "tamanho_bytes": resultado.size_bytes,
        "criado_em": resultado.created_at.isoformat(),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
