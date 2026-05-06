"""enterprise hardening and historical box rules

Revision ID: 20260424_0001
Revises:
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa


revision = "20260424_0001"
down_revision = None
branch_labels = None
depends_on = None


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


def _checks(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    return {constraint["name"] for constraint in inspector.get_check_constraints(table_name)}


def _constraints(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return set()
    names = {constraint["name"] for constraint in inspector.get_foreign_keys(table_name) if constraint.get("name")}
    names.update(_checks(table_name))
    return names


def _create_index_once(name: str, table_name: str, columns: list[str], **kwargs) -> None:
    if name not in _indexes(table_name):
        op.create_index(name, table_name, columns, **kwargs)


def _create_check_once(name: str, table_name: str, condition: str) -> None:
    if name not in _checks(table_name):
        op.create_check_constraint(name, table_name, condition)


def upgrade() -> None:
    # Banco novo: Alembic cria o schema completo a partir dos models atuais.
    # Banco existente: create_all nao altera tabelas existentes; os ALTERs abaixo preservam os dados.
    from app.database import Base
    import app.models  # noqa: F401

    bind = op.get_bind()
    dialect = bind.dialect.name
    Base.metadata.create_all(bind=bind)
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "box_rule_config" in tables:
        total_rules = bind.execute(sa.text("SELECT COUNT(*) FROM box_rule_config")).scalar() or 0
        if total_rules == 0:
            bind.execute(sa.text(
                "INSERT INTO box_rule_config (quantidade_hidrometros, ativo, vigente_desde, criado_em) "
                "VALUES (6, TRUE, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))

    if "caixas_hidrometros" in tables:
        caixa_columns = _columns("caixas_hidrometros")
        if "regra_caixa_id" not in caixa_columns:
            op.add_column("caixas_hidrometros", sa.Column("regra_caixa_id", sa.Integer(), nullable=True))
        if "quantidade_esperada" not in caixa_columns:
            op.add_column(
                "caixas_hidrometros",
                sa.Column("quantidade_esperada", sa.Integer(), nullable=False, server_default="6"),
            )
        if dialect != "sqlite" and "fk_caixas_hidrometros_regra_caixa" not in _constraints("caixas_hidrometros"):
            op.create_foreign_key(
                "fk_caixas_hidrometros_regra_caixa",
                "caixas_hidrometros",
                "box_rule_config",
                ["regra_caixa_id"],
                ["id"],
            )
        if dialect != "sqlite":
            _create_check_once(
                "ck_caixa_quantidade_esperada_positiva",
                "caixas_hidrometros",
                "quantidade_esperada > 0",
            )
        bind.execute(sa.text(
            "UPDATE caixas_hidrometros "
            "SET regra_caixa_id = COALESCE("
            "regra_caixa_id, "
            "(SELECT id FROM box_rule_config WHERE ativo = TRUE ORDER BY id DESC LIMIT 1)"
            "), quantidade_esperada = COALESCE(quantidade_esperada, 6)"
        ))

    if "feedbacks_operacionais" in tables and "urgente" not in _columns("feedbacks_operacionais"):
        op.add_column(
            "feedbacks_operacionais",
            sa.Column("urgente", sa.Boolean(), nullable=False, server_default=sa.false()),
        )

    if dialect != "sqlite" and "estoque_pecas" in tables:
        _create_check_once("ck_estoque_quantidade_nao_negativa", "estoque_pecas", "quantidade_atual >= 0")
        _create_check_once("ck_estoque_quantidade_maxima_positiva", "estoque_pecas", "quantidade_maxima > 0")
    if dialect != "sqlite" and "instalador_pecas" in tables:
        _create_check_once("ck_instalador_peca_quantidade_nao_negativa", "instalador_pecas", "quantidade >= 0")
    if dialect != "sqlite" and "solicitacao_itens_caixa" in tables:
        _create_check_once(
            "ck_solicitacao_caixa_quantidade_positiva",
            "solicitacao_itens_caixa",
            "quantidade_solicitada > 0",
        )
    if dialect != "sqlite" and "solicitacao_itens_peca" in tables:
        _create_check_once(
            "ck_solicitacao_peca_quantidade_positiva",
            "solicitacao_itens_peca",
            "quantidade_solicitada > 0",
        )
    if dialect != "sqlite" and "movimentacoes_pecas" in tables:
        _create_check_once("ck_movimentacao_peca_quantidade_positiva", "movimentacoes_pecas", "quantidade > 0")
    if dialect != "sqlite" and "conferencia_itens" in tables:
        _create_check_once(
            "ck_conferencia_quantidade_sistema_nao_negativa",
            "conferencia_itens",
            "quantidade_sistema >= 0",
        )
        _create_check_once(
            "ck_conferencia_quantidade_real_nao_negativa",
            "conferencia_itens",
            "quantidade_real >= 0",
        )

    if dialect == "postgresql" and "usuarios" in tables:
        _create_index_once(
            "uq_one_active_admin",
            "usuarios",
            ["role"],
            unique=True,
            postgresql_where=sa.text("role = 'ADMIN' AND ativo IS TRUE"),
        )
    if "caixas_hidrometros" in tables:
        _create_index_once("idx_caixas_status_ativo", "caixas_hidrometros", ["status", "ativo"])
    if "solicitacoes" in tables:
        _create_index_once("idx_solicitacoes_status", "solicitacoes", ["status"])
    if "movimentacoes_material" in tables:
        _create_index_once("idx_mov_material_tipo_data", "movimentacoes_material", ["tipo", "criado_em"])


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "movimentacoes_material" in tables and "idx_mov_material_tipo_data" in _indexes("movimentacoes_material"):
        op.drop_index("idx_mov_material_tipo_data", table_name="movimentacoes_material")
    if "solicitacoes" in tables and "idx_solicitacoes_status" in _indexes("solicitacoes"):
        op.drop_index("idx_solicitacoes_status", table_name="solicitacoes")
    if "caixas_hidrometros" in tables and "idx_caixas_status_ativo" in _indexes("caixas_hidrometros"):
        op.drop_index("idx_caixas_status_ativo", table_name="caixas_hidrometros")
    if "usuarios" in tables and "uq_one_active_admin" in _indexes("usuarios"):
        op.drop_index("uq_one_active_admin", table_name="usuarios")
