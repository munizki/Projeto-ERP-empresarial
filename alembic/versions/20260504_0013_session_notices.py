"""adiciona avisos de encerramento de sessao

Revision ID: 20260504_0013
Revises: 20260504_0012
Create Date: 2026-05-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260504_0013"
down_revision = "20260504_0012"
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return column in {item["name"] for item in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "usuarios" not in inspector.get_table_names():
        return

    with op.batch_alter_table("usuarios") as batch_op:
        if not _column_exists("usuarios", "session_notice_code"):
            batch_op.add_column(sa.Column("session_notice_code", sa.String(length=80), nullable=True))
        if not _column_exists("usuarios", "session_notice_message"):
            batch_op.add_column(sa.Column("session_notice_message", sa.Text(), nullable=True))
        if not _column_exists("usuarios", "session_notice_at"):
            batch_op.add_column(sa.Column("session_notice_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "usuarios" not in inspector.get_table_names():
        return

    with op.batch_alter_table("usuarios") as batch_op:
        for column in ("session_notice_at", "session_notice_message", "session_notice_code"):
            if _column_exists("usuarios", column):
                batch_op.drop_column(column)
