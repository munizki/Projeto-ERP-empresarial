"""operational safety flags

Revision ID: 20260424_0002
Revises: 20260424_0001
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260424_0002"
down_revision = "20260424_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "system_flags" not in tables:
        op.create_table(
            "system_flags",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column("chave", sa.String(length=80), nullable=False),
            sa.Column("valor", sa.Text(), nullable=True),
            sa.Column("motivo", sa.Text(), nullable=True),
            sa.Column("atualizado_por_id", sa.Integer(), nullable=True),
            sa.Column("atualizado_em", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
            sa.ForeignKeyConstraint(["atualizado_por_id"], ["usuarios.id"]),
            sa.UniqueConstraint("chave", name="uq_system_flags_chave"),
        )
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("system_flags")}
    if "idx_system_flags_chave" not in indexes:
        op.create_index("idx_system_flags_chave", "system_flags", ["chave"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "system_flags" in tables:
        op.drop_table("system_flags")
