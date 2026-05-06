"""operational expansion: productivity, carcacas and transfers

Revision ID: 20260426_0005
Revises: 20260426_0004
Create Date: 2026-04-26
"""

from alembic import op
import sqlalchemy as sa


revision = "20260426_0005"
down_revision = "20260426_0004"
branch_labels = None
depends_on = None


def _indexes(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table_name)}


def _create_index_once(name: str, table_name: str, columns: list[str], **kwargs) -> None:
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns, **kwargs)


def _add_caixa_status_values() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for value in ("INSTALADA", "TRANSFERIDA_OUTRA_EMPRESA", "CANCELADA"):
        op.execute(sa.text(f"ALTER TYPE caixastatus ADD VALUE IF NOT EXISTS '{value}'"))


def _backfill_instalacoes() -> None:
    from app.models import Hidrometro

    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "instalacoes_hidrometros" not in tables or Hidrometro.__tablename__ not in tables:
        return

    hidrometros_table = '"' + Hidrometro.__tablename__.replace('"', '""') + '"'
    bind.execute(
        sa.text(
            f"""
            INSERT INTO instalacoes_hidrometros (
                instalador_id,
                hidrometro_id,
                caixa_id,
                solicitacao_id,
                data_instalacao,
                usuario_registro_id,
                criado_em
            )
            SELECT
                COALESCE(h.instalador_baixa_id, mb.instalador_id) AS instalador_id,
                h.id AS hidrometro_id,
                h.caixa_id,
                (
                    SELECT ms.solicitacao_id
                    FROM movimentacoes_material ms
                    WHERE ms.caixa_id = h.caixa_id
                      AND ms.tipo = 'SAIDA'
                    ORDER BY ms.criado_em DESC, ms.id DESC
                    LIMIT 1
                ) AS solicitacao_id,
                COALESCE(h.instalado_em, mb.criado_em, CURRENT_TIMESTAMP) AS data_instalacao,
                COALESCE(h.baixado_por_id, mb.registrado_por_id) AS usuario_registro_id,
                COALESCE(h.instalado_em, mb.criado_em, CURRENT_TIMESTAMP) AS criado_em
            FROM {hidrometros_table} h
            LEFT JOIN movimentacoes_material mb
              ON mb.hidrometro_id = h.id
             AND mb.tipo = 'BAIXA'
            WHERE h.status = 'INSTALADO'
              AND h.caixa_id IS NOT NULL
              AND COALESCE(h.instalador_baixa_id, mb.instalador_id) IS NOT NULL
              AND COALESCE(h.baixado_por_id, mb.registrado_por_id) IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM instalacoes_hidrometros i
                  WHERE i.hidrometro_id = h.id
              )
            """
        )
    )


def upgrade() -> None:
    _add_caixa_status_values()

    from app.database import Base
    import app.models  # noqa: F401

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    tables = set(sa.inspect(bind).get_table_names())

    if "carcaca_movimentacoes" in tables:
        _create_index_once("idx_carcaca_tipo_data", "carcaca_movimentacoes", ["tipo_movimento", "data_movimento"])
        _create_index_once("idx_carcaca_instalador", "carcaca_movimentacoes", ["instalador_id"])
    if "instalacoes_hidrometros" in tables:
        _create_index_once("idx_instalacao_instalador_data", "instalacoes_hidrometros", ["instalador_id", "data_instalacao"])
        _create_index_once("idx_instalacao_caixa", "instalacoes_hidrometros", ["caixa_id"])
    if "transferencias_empresa" in tables:
        _create_index_once("idx_transferencia_empresa_data", "transferencias_empresa", ["data_transferencia"])
        _create_index_once("idx_transferencia_empresa_destino", "transferencias_empresa", ["empresa_destino"])
    if "conferencia_instalador_pecas" in tables:
        _create_index_once(
            "idx_conf_inst_peca_instalador_data",
            "conferencia_instalador_pecas",
            ["instalador_id", "data_conferencia"],
        )

    _backfill_instalacoes()


def downgrade() -> None:
    # Dados operacionais sao preservados; downgrade destrutivo nao e aplicado.
    pass
