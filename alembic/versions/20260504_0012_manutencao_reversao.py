"""adiciona reversao administrativa de manutencao

Revision ID: 20260504_0012
Revises: 20260504_0011
Create Date: 2026-05-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260504_0012"
down_revision = "20260504_0011"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "hidrometro_manutencao" not in inspector.get_table_names():
        return

    with op.batch_alter_table("hidrometro_manutencao") as batch_op:
        if not _column_exists("hidrometro_manutencao", "revertida"):
            batch_op.add_column(sa.Column("revertida", sa.Boolean(), nullable=False, server_default=sa.false()))
        if not _column_exists("hidrometro_manutencao", "revertida_em"):
            batch_op.add_column(sa.Column("revertida_em", sa.DateTime(), nullable=True))
        if not _column_exists("hidrometro_manutencao", "revertida_por"):
            batch_op.add_column(sa.Column("revertida_por", sa.Integer(), nullable=True))
        if not _column_exists("hidrometro_manutencao", "justificativa_reversao"):
            batch_op.add_column(sa.Column("justificativa_reversao", sa.Text(), nullable=True))
        if not _column_exists("hidrometro_manutencao", "destino_reversao"):
            batch_op.add_column(sa.Column("destino_reversao", sa.String(length=60), nullable=True))

    if bind.dialect.name != "sqlite":
        op.alter_column("hidrometro_manutencao", "revertida", server_default=None)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "hidrometro_manutencao" not in inspector.get_table_names():
        return

    with op.batch_alter_table("hidrometro_manutencao") as batch_op:
        for column in (
            "destino_reversao",
            "justificativa_reversao",
            "revertida_por",
            "revertida_em",
            "revertida",
        ):
            if _column_exists("hidrometro_manutencao", column):
                batch_op.drop_column(column)
