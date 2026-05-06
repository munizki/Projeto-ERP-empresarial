"""matricula em usuarios e cpf opcional para instaladores

Revision ID: 20260430_0008
Revises: 20260430_0007
Create Date: 2026-04-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260430_0008"
down_revision = "20260430_0007"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> dict[str, object]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return {}
    return {column["name"]: column for column in inspector.get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _sync_usuario_matricula() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                """
                UPDATE usuarios AS u
                   SET matricula = i.matricula
                  FROM instaladores AS i
                 WHERE i.usuario_id = u.id
                   AND u.matricula IS NULL
                   AND i.matricula IS NOT NULL
                """
            )
        )
    else:
        op.execute(
            sa.text(
                """
                UPDATE usuarios
                   SET matricula = (
                       SELECT instaladores.matricula
                         FROM instaladores
                        WHERE instaladores.usuario_id = usuarios.id
                        LIMIT 1
                   )
                 WHERE matricula IS NULL
                   AND EXISTS (
                       SELECT 1
                         FROM instaladores
                        WHERE instaladores.usuario_id = usuarios.id
                          AND instaladores.matricula IS NOT NULL
                   )
                """
            )
        )


def upgrade() -> None:
    tables = _tables()

    if "usuarios" in tables:
        usuarios_columns = _columns("usuarios")
        with op.batch_alter_table("usuarios") as batch_op:
            if "matricula" not in usuarios_columns:
                batch_op.add_column(sa.Column("matricula", sa.String(length=50), nullable=True))
        if "instaladores" in tables:
            _sync_usuario_matricula()
        if "ix_usuarios_matricula" not in _indexes("usuarios"):
            op.create_index("ix_usuarios_matricula", "usuarios", ["matricula"], unique=True)

    if "instaladores" in tables:
        instaladores_columns = _columns("instaladores")
        if "cpf" in instaladores_columns:
            op.execute(sa.text("UPDATE instaladores SET cpf = NULL WHERE cpf = ''"))
            with op.batch_alter_table("instaladores") as batch_op:
                batch_op.alter_column(
                    "cpf",
                    existing_type=sa.String(length=14),
                    nullable=True,
                )


def downgrade() -> None:
    tables = _tables()

    if "instaladores" in tables and "cpf" in _columns("instaladores"):
        bind = op.get_bind()
        if bind.dialect.name == "postgresql":
            op.execute(
                sa.text(
                    """
                    UPDATE instaladores
                       SET cpf = lpad(id::text, 11, '0')
                     WHERE cpf IS NULL
                    """
                )
            )
        else:
            op.execute(
                sa.text(
                    """
                    UPDATE instaladores
                       SET cpf = substr('00000000000' || id, -11, 11)
                     WHERE cpf IS NULL
                    """
                )
            )
        with op.batch_alter_table("instaladores") as batch_op:
            batch_op.alter_column(
                "cpf",
                existing_type=sa.String(length=14),
                nullable=False,
            )

    if "usuarios" in tables:
        if "ix_usuarios_matricula" in _indexes("usuarios"):
            op.drop_index("ix_usuarios_matricula", table_name="usuarios")
        if "matricula" in _columns("usuarios"):
            with op.batch_alter_table("usuarios") as batch_op:
                batch_op.drop_column("matricula")
