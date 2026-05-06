"""manutencao de hidrometros e prioridade de reutilizacao

Revision ID: 20260504_0010
Revises: 20260504_0009
Create Date: 2026-05-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260504_0010"
down_revision = "20260504_0009"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _hidrometros_table() -> str | None:
    inspector = sa.inspect(op.get_bind())
    for table in inspector.get_table_names():
        columns = {column["name"] for column in inspector.get_columns(table)}
        if {"numero_serie", "status", "caixa_id"}.issubset(columns):
            return table
    return None


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


def _add_enum_values() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    enum_exists = bind.execute(
        sa.text("SELECT 1 FROM pg_type WHERE typname = 'hidrometrostatus'")
    ).scalar()
    if not enum_exists:
        return
    for value in (
        "em_manutencao",
        "enviado_assistencia",
        "retornado_manutencao",
        "descartado_tecnico",
    ):
        op.execute(sa.text(f"ALTER TYPE hidrometrostatus ADD VALUE IF NOT EXISTS '{value}'"))


def upgrade() -> None:
    _add_enum_values()
    tables = _tables()
    hidrometros_table = _hidrometros_table()

    if hidrometros_table:
        columns = _columns(hidrometros_table)
        with op.batch_alter_table(hidrometros_table) as batch_op:
            if "status_operacional" not in columns:
                batch_op.add_column(sa.Column("status_operacional", sa.String(length=40), nullable=False, server_default="DISPONIVEL"))
            if "em_manutencao" not in columns:
                batch_op.add_column(sa.Column("em_manutencao", sa.Boolean(), nullable=False, server_default=sa.false()))
            if "bloqueado_por_manutencao" not in columns:
                batch_op.add_column(sa.Column("bloqueado_por_manutencao", sa.Boolean(), nullable=False, server_default=sa.false()))
            if "descartado_tecnico" not in columns:
                batch_op.add_column(sa.Column("descartado_tecnico", sa.Boolean(), nullable=False, server_default=sa.false()))
            if "retornou_manutencao" not in columns:
                batch_op.add_column(sa.Column("retornou_manutencao", sa.Boolean(), nullable=False, server_default=sa.false()))
            if "data_retorno_manutencao" not in columns:
                batch_op.add_column(sa.Column("data_retorno_manutencao", sa.DateTime(), nullable=True))
            if "prioridade_reutilizacao" not in columns:
                batch_op.add_column(sa.Column("prioridade_reutilizacao", sa.Boolean(), nullable=False, server_default=sa.false()))
            if "origem_prioridade" not in columns:
                batch_op.add_column(sa.Column("origem_prioridade", sa.String(length=80), nullable=True))
            if "observacao_prioridade" not in columns:
                batch_op.add_column(sa.Column("observacao_prioridade", sa.Text(), nullable=True))
            if "caixa_id" in columns:
                batch_op.alter_column("caixa_id", existing_type=sa.Integer(), nullable=True)

        indexes = _indexes(hidrometros_table)
        if "idx_hidrometro_prioridade_reutilizacao" not in indexes:
            op.create_index(
                "idx_hidrometro_prioridade_reutilizacao",
                hidrometros_table,
                ["prioridade_reutilizacao", "data_retorno_manutencao"],
            )
        if "idx_hidrometro_manutencao_flags" not in indexes:
            op.create_index(
                "idx_hidrometro_manutencao_flags",
                hidrometros_table,
                ["em_manutencao", "descartado_tecnico", "caixa_id"],
            )

    if "hidrometro_manutencao" not in tables and hidrometros_table:
        op.create_table(
            "hidrometro_manutencao",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("hidrometro_id", sa.Integer(), nullable=False),
            sa.Column("caixa_origem_id", sa.Integer(), nullable=True),
            sa.Column("instalador_origem_id", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="EM_MANUTENCAO"),
            sa.Column("motivo", sa.String(length=120), nullable=False),
            sa.Column("descricao_problema", sa.Text(), nullable=False),
            sa.Column("fornecedor_assistencia", sa.String(length=160), nullable=True),
            sa.Column("data_abertura", sa.DateTime(), nullable=False),
            sa.Column("data_envio", sa.DateTime(), nullable=True),
            sa.Column("data_retorno", sa.DateTime(), nullable=True),
            sa.Column("laudo", sa.Text(), nullable=True),
            sa.Column("decisao_final", sa.String(length=60), nullable=True),
            sa.Column("criado_por", sa.Integer(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["hidrometro_id"], [f"{hidrometros_table}.id"], name="fk_manutencao_hidrometro_id"),
            sa.ForeignKeyConstraint(["caixa_origem_id"], ["caixas_hidrometros.id"], name="fk_manutencao_caixa_origem_id"),
            sa.ForeignKeyConstraint(["instalador_origem_id"], ["instaladores.id"], name="fk_manutencao_instalador_origem_id"),
            sa.ForeignKeyConstraint(["criado_por"], ["usuarios.id"], name="fk_manutencao_criado_por"),
        )
    if "hidrometro_manutencao" in _tables():
        indexes = _indexes("hidrometro_manutencao")
        if "idx_hidrometro_manutencao_hidrometro" not in indexes:
            op.create_index("idx_hidrometro_manutencao_hidrometro", "hidrometro_manutencao", ["hidrometro_id"])
        if "idx_hidrometro_manutencao_caixa_origem" not in indexes:
            op.create_index("idx_hidrometro_manutencao_caixa_origem", "hidrometro_manutencao", ["caixa_origem_id"])
        if "idx_hidrometro_manutencao_status" not in indexes:
            op.create_index("idx_hidrometro_manutencao_status", "hidrometro_manutencao", ["status"])


def downgrade() -> None:
    if "hidrometro_manutencao" in _tables():
        for index_name in (
            "idx_hidrometro_manutencao_status",
            "idx_hidrometro_manutencao_caixa_origem",
            "idx_hidrometro_manutencao_hidrometro",
        ):
            if index_name in _indexes("hidrometro_manutencao"):
                op.drop_index(index_name, table_name="hidrometro_manutencao")
        op.drop_table("hidrometro_manutencao")

    hidrometros_table = _hidrometros_table()
    if not hidrometros_table:
        return
    for index_name in ("idx_hidrometro_manutencao_flags", "idx_hidrometro_prioridade_reutilizacao"):
        if index_name in _indexes(hidrometros_table):
            op.drop_index(index_name, table_name=hidrometros_table)
    columns = _columns(hidrometros_table)
    with op.batch_alter_table(hidrometros_table) as batch_op:
        for column_name in (
            "observacao_prioridade",
            "origem_prioridade",
            "prioridade_reutilizacao",
            "data_retorno_manutencao",
            "retornou_manutencao",
            "descartado_tecnico",
            "bloqueado_por_manutencao",
            "em_manutencao",
            "status_operacional",
        ):
            if column_name in columns:
                batch_op.drop_column(column_name)
