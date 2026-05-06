"""Add indexes for concurrent operational load.

Revision ID: 20260430_0007
Revises: 20260429_0006
Create Date: 2026-04-30
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260430_0007"
down_revision = "20260429_0006"
branch_labels = None
depends_on = None


INDEXES = [
    ("idx_solicitacao_instalador_status_entrega", "solicitacoes", ["instalador_id", "status", "entregue_em", "id"]),
    ("idx_solicitacao_instalador_criada", "solicitacoes", ["instalador_id", "criado_em", "id"]),
    ("idx_solicitacao_item_caixa_solicitacao", "solicitacao_itens_caixa", ["solicitacao_id"]),
    ("idx_solicitacao_item_caixa_caixa", "solicitacao_itens_caixa", ["caixa_id"]),
    ("idx_solicitacao_item_peca_solicitacao", "solicitacao_itens_peca", ["solicitacao_id"]),
    ("idx_solicitacao_item_peca_tipo", "solicitacao_itens_peca", ["tipo_peca_id"]),
    ("idx_mov_material_solicitacao", "movimentacoes_material", ["solicitacao_id"]),
    ("idx_mov_material_instalador_criado", "movimentacoes_material", ["instalador_id", "criado_em"]),
    ("idx_mov_material_caixa_criado", "movimentacoes_material", ["caixa_id", "criado_em"]),
    ("idx_mov_peca_solicitacao", "movimentacoes_pecas", ["solicitacao_id"]),
    ("idx_mov_peca_instalador_criado", "movimentacoes_pecas", ["instalador_id", "criado_em"]),
    ("idx_mov_peca_tipo_criado", "movimentacoes_pecas", ["tipo_peca_id", "criado_em"]),
]


def _existing_indexes(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {index["name"] for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    for index_name, table_name, columns in INDEXES:
        if index_name not in _existing_indexes(table_name):
            op.create_index(index_name, table_name, columns)


def downgrade() -> None:
    for index_name, table_name, _columns in reversed(INDEXES):
        if index_name in _existing_indexes(table_name):
            op.drop_index(index_name, table_name=table_name)
