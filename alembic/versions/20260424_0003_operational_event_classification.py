"""classificacao de eventos operacionais

Revision ID: 20260424_0003
Revises: 20260424_0002
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260424_0003"
down_revision = "20260424_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("auditoria_logs")}
    indexes = {index["name"] for index in inspector.get_indexes("auditoria_logs")}
    with op.batch_alter_table("auditoria_logs") as batch_op:
        if "severidade" not in columns:
            batch_op.add_column(sa.Column("severidade", sa.String(length=20), nullable=False, server_default="NORMAL"))
        if "categoria" not in columns:
            batch_op.add_column(sa.Column("categoria", sa.String(length=40), nullable=False, server_default="OPERACIONAL"))
        if "idx_auditoria_severidade" not in indexes:
            batch_op.create_index("idx_auditoria_severidade", ["severidade"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("auditoria_logs")}
    indexes = {index["name"] for index in inspector.get_indexes("auditoria_logs")}
    with op.batch_alter_table("auditoria_logs") as batch_op:
        if "idx_auditoria_severidade" in indexes:
            batch_op.drop_index("idx_auditoria_severidade")
        if "categoria" in columns:
            batch_op.drop_column("categoria")
        if "severidade" in columns:
            batch_op.drop_column("severidade")
