"""corrige enum postgres dos status de manutencao

Revision ID: 20260504_0011
Revises: 20260504_0010
Create Date: 2026-05-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260504_0011"
down_revision = "20260504_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    enum_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'hidrometrostatus'")
    ).scalar()
    if not enum_exists:
        return
    for value in (
        "EM_MANUTENCAO",
        "ENVIADO_ASSISTENCIA",
        "RETORNADO_MANUTENCAO",
        "DESCARTADO_TECNICO",
    ):
        op.execute(sa.text(f"ALTER TYPE hidrometrostatus ADD VALUE IF NOT EXISTS '{value}'"))


def downgrade() -> None:
    # PostgreSQL nao permite remover valores de enum com seguranca em downgrade simples.
    pass
