"""rastreabilidade completa de auditoria

Revision ID: 20260426_0004
Revises: 20260424_0003
Create Date: 2026-04-26
"""

from alembic import op
import sqlalchemy as sa


revision = "20260426_0004"
down_revision = "20260424_0003"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    columns = _columns("auditoria_logs")
    with op.batch_alter_table("auditoria_logs") as batch_op:
        if "usuario_nome" not in columns:
            batch_op.add_column(sa.Column("usuario_nome", sa.String(length=150), nullable=True))
        if "usuario_email" not in columns:
            batch_op.add_column(sa.Column("usuario_email", sa.String(length=150), nullable=True))
        if "usuario_perfil" not in columns:
            batch_op.add_column(sa.Column("usuario_perfil", sa.String(length=40), nullable=True))
        if "resultado" not in columns:
            batch_op.add_column(sa.Column("resultado", sa.String(length=40), nullable=False, server_default="SUCESSO"))
        if "ip_cliente" not in columns:
            batch_op.add_column(sa.Column("ip_cliente", sa.String(length=80), nullable=True))
        if "ip_conexao" not in columns:
            batch_op.add_column(sa.Column("ip_conexao", sa.String(length=80), nullable=True))
        if "x_forwarded_for" not in columns:
            batch_op.add_column(sa.Column("x_forwarded_for", sa.String(), nullable=True))
        if "x_real_ip" not in columns:
            batch_op.add_column(sa.Column("x_real_ip", sa.String(length=80), nullable=True))
        if "user_agent" not in columns:
            batch_op.add_column(sa.Column("user_agent", sa.String(), nullable=True))
        if "request_id" not in columns:
            batch_op.add_column(sa.Column("request_id", sa.String(length=40), nullable=True))


def downgrade() -> None:
    columns = _columns("auditoria_logs")
    with op.batch_alter_table("auditoria_logs") as batch_op:
        for column_name in [
            "request_id",
            "user_agent",
            "x_real_ip",
            "x_forwarded_for",
            "ip_conexao",
            "ip_cliente",
            "resultado",
            "usuario_perfil",
            "usuario_email",
            "usuario_nome",
        ]:
            if column_name in columns:
                batch_op.drop_column(column_name)
