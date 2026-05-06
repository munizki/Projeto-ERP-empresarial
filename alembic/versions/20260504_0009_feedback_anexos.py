"""anexos para feedback operacional

Revision ID: 20260504_0009
Revises: 20260430_0008
Create Date: 2026-05-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260504_0009"
down_revision = "20260430_0008"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _indexes(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if "feedback_anexos" not in _tables():
        op.create_table(
            "feedback_anexos",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("feedback_id", sa.Integer(), nullable=False),
            sa.Column("nome_original", sa.String(length=255), nullable=False),
            sa.Column("nome_armazenado", sa.String(length=255), nullable=False),
            sa.Column("caminho_relativo", sa.String(length=500), nullable=False),
            sa.Column("content_type", sa.String(length=120), nullable=True),
            sa.Column("tamanho_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("criado_em", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["feedback_id"], ["feedbacks_operacionais.id"], name="fk_feedback_anexo_feedback_id", ondelete="CASCADE"),
        )
    if "ix_feedback_anexos_id" not in _indexes("feedback_anexos"):
        op.create_index("ix_feedback_anexos_id", "feedback_anexos", ["id"])
    if "idx_feedback_anexo_feedback" not in _indexes("feedback_anexos"):
        op.create_index("idx_feedback_anexo_feedback", "feedback_anexos", ["feedback_id"])


def downgrade() -> None:
    if "feedback_anexos" in _tables():
        if "idx_feedback_anexo_feedback" in _indexes("feedback_anexos"):
            op.drop_index("idx_feedback_anexo_feedback", table_name="feedback_anexos")
        if "ix_feedback_anexos_id" in _indexes("feedback_anexos"):
            op.drop_index("ix_feedback_anexos_id", table_name="feedback_anexos")
        op.drop_table("feedback_anexos")
