"""portal restrito do instalador

Revision ID: 20260429_0006
Revises: 20260426_0005
Create Date: 2026-04-29
"""

from alembic import op
import sqlalchemy as sa


revision = "20260429_0006"
down_revision = "20260426_0005"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _constraints(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints(table_name)}
    constraints.update({constraint["name"] for constraint in inspector.get_foreign_keys(table_name)})
    return {name for name in constraints if name}


def _add_user_role_value() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'INSTALADOR'"))


def upgrade() -> None:
    _add_user_role_value()
    tables = _tables()

    if "instaladores" in tables:
        columns = _columns("instaladores")
        constraints = _constraints("instaladores")
        with op.batch_alter_table("instaladores") as batch_op:
            if "usuario_id" not in columns:
                batch_op.add_column(sa.Column("usuario_id", sa.Integer(), nullable=True))
            if "fk_instaladores_usuario_id" not in constraints:
                batch_op.create_foreign_key("fk_instaladores_usuario_id", "usuarios", ["usuario_id"], ["id"])
            if "uq_instaladores_usuario_id" not in constraints:
                batch_op.create_unique_constraint("uq_instaladores_usuario_id", ["usuario_id"])

    if "solicitacoes" in tables:
        columns = _columns("solicitacoes")
        constraints = _constraints("solicitacoes")
        with op.batch_alter_table("solicitacoes") as batch_op:
            if "recebimento_instalador_status" not in columns:
                batch_op.add_column(sa.Column("recebimento_instalador_status", sa.String(length=40), nullable=True))
            if "confirmacao_instalador_em" not in columns:
                batch_op.add_column(sa.Column("confirmacao_instalador_em", sa.DateTime(), nullable=True))
            if "usuario_confirmacao_instalador_id" not in columns:
                batch_op.add_column(sa.Column("usuario_confirmacao_instalador_id", sa.Integer(), nullable=True))
            if "motivo_divergencia_instalador" not in columns:
                batch_op.add_column(sa.Column("motivo_divergencia_instalador", sa.Text(), nullable=True))
            if "fk_solicitacoes_usuario_confirmacao_instalador_id" not in constraints:
                batch_op.create_foreign_key(
                    "fk_solicitacoes_usuario_confirmacao_instalador_id",
                    "usuarios",
                    ["usuario_confirmacao_instalador_id"],
                    ["id"],
                )

        indexes = _indexes("solicitacoes")
        if "idx_solicitacoes_instalador_recebimento" not in indexes:
            op.create_index(
                "idx_solicitacoes_instalador_recebimento",
                "solicitacoes",
                ["instalador_id", "recebimento_instalador_status"],
            )


def downgrade() -> None:
    # Nao remove o valor do enum PostgreSQL para evitar quebra de dados existentes.
    tables = _tables()

    if "solicitacoes" in tables:
        indexes = _indexes("solicitacoes")
        if "idx_solicitacoes_instalador_recebimento" in indexes:
            op.drop_index("idx_solicitacoes_instalador_recebimento", table_name="solicitacoes")
        columns = _columns("solicitacoes")
        constraints = _constraints("solicitacoes")
        with op.batch_alter_table("solicitacoes") as batch_op:
            if "fk_solicitacoes_usuario_confirmacao_instalador_id" in constraints:
                batch_op.drop_constraint("fk_solicitacoes_usuario_confirmacao_instalador_id", type_="foreignkey")
            for column_name in [
                "motivo_divergencia_instalador",
                "usuario_confirmacao_instalador_id",
                "confirmacao_instalador_em",
                "recebimento_instalador_status",
            ]:
                if column_name in columns:
                    batch_op.drop_column(column_name)

    if "instaladores" in tables:
        columns = _columns("instaladores")
        constraints = _constraints("instaladores")
        with op.batch_alter_table("instaladores") as batch_op:
            if "uq_instaladores_usuario_id" in constraints:
                batch_op.drop_constraint("uq_instaladores_usuario_id", type_="unique")
            if "fk_instaladores_usuario_id" in constraints:
                batch_op.drop_constraint("fk_instaladores_usuario_id", type_="foreignkey")
            if "usuario_id" in columns:
                batch_op.drop_column("usuario_id")
