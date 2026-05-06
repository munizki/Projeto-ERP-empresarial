# ============================================================
# alembic/env.py
# Configuração do ambiente Alembic — integra com SQLAlchemy
# ============================================================

import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context
from dotenv import load_dotenv

# Adicionar raiz do projeto ao path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if os.getenv("APP_ENV", "development").strip().lower() not in {"prod", "production"}:
    load_dotenv()

# Importar models para que o Alembic os detecte nas migrações autogenerate
from app.database import Base
from app.models import *  # noqa: F401,F403

# Configuração Alembic
config = context.config

# Sobrescrever URL com variável de ambiente
database_url = os.getenv("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)

# Configurar logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata dos models — necessário para autogenerate
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Executar migrações em modo offline (sem conexão ativa)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Executar migrações em modo online (com conexão ativa)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
