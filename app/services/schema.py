from __future__ import annotations

from sqlalchemy import inspect, text

from app.database import engine


def ensure_runtime_schema() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "caixas_hidrometros" not in tables:
        return

    usuarios_columns = {column["name"] for column in inspector.get_columns("usuarios")} if "usuarios" in tables else set()
    caixas_columns = {column["name"] for column in inspector.get_columns("caixas_hidrometros")}
    hidrometros_table = ""
    for table_name in tables:
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if {"numero_serie", "status", "caixa_id"}.issubset(columns):
            hidrometros_table = table_name
            break
    hidrometros_columns = {column["name"] for column in inspector.get_columns(hidrometros_table)} if hidrometros_table else set()
    movimentacoes_material_columns = (
        {column["name"] for column in inspector.get_columns("movimentacoes_material")}
        if "movimentacoes_material" in tables
        else set()
    )
    auditoria_columns = (
        {column["name"] for column in inspector.get_columns("auditoria_logs")}
        if "auditoria_logs" in tables
        else set()
    )
    feedback_columns = (
        {column["name"] for column in inspector.get_columns("feedbacks_operacionais")}
        if "feedbacks_operacionais" in tables
        else set()
    )
    instaladores_columns = (
        {column["name"] for column in inspector.get_columns("instaladores")}
        if "instaladores" in tables
        else set()
    )
    solicitacoes_columns = (
        {column["name"] for column in inspector.get_columns("solicitacoes")}
        if "solicitacoes" in tables
        else set()
    )
    manutencao_columns = (
        {column["name"] for column in inspector.get_columns("hidrometro_manutencao")}
        if "hidrometro_manutencao" in tables
        else set()
    )

    statements: list[str] = []
    if "token_version" not in usuarios_columns:
        statements.append("ALTER TABLE usuarios ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
    if "session_notice_code" not in usuarios_columns:
        statements.append("ALTER TABLE usuarios ADD COLUMN session_notice_code VARCHAR(80)")
    if "session_notice_message" not in usuarios_columns:
        statements.append("ALTER TABLE usuarios ADD COLUMN session_notice_message TEXT")
    if "session_notice_at" not in usuarios_columns:
        statements.append("ALTER TABLE usuarios ADD COLUMN session_notice_at DATETIME")
    if "matricula" not in usuarios_columns:
        statements.append("ALTER TABLE usuarios ADD COLUMN matricula VARCHAR(50)")
    if "instaladores" in tables and "usuario_id" not in instaladores_columns:
        statements.append("ALTER TABLE instaladores ADD COLUMN usuario_id INTEGER")
    if "solicitacoes" in tables and "recebimento_instalador_status" not in solicitacoes_columns:
        statements.append("ALTER TABLE solicitacoes ADD COLUMN recebimento_instalador_status VARCHAR(40)")
    if "solicitacoes" in tables and "confirmacao_instalador_em" not in solicitacoes_columns:
        statements.append("ALTER TABLE solicitacoes ADD COLUMN confirmacao_instalador_em DATETIME")
    if "solicitacoes" in tables and "usuario_confirmacao_instalador_id" not in solicitacoes_columns:
        statements.append("ALTER TABLE solicitacoes ADD COLUMN usuario_confirmacao_instalador_id INTEGER")
    if "solicitacoes" in tables and "motivo_divergencia_instalador" not in solicitacoes_columns:
        statements.append("ALTER TABLE solicitacoes ADD COLUMN motivo_divergencia_instalador TEXT")
    if "ativo" not in caixas_columns:
        statements.append("ALTER TABLE caixas_hidrometros ADD COLUMN ativo BOOLEAN NOT NULL DEFAULT TRUE")
    if "regra_caixa_id" not in caixas_columns:
        statements.append("ALTER TABLE caixas_hidrometros ADD COLUMN regra_caixa_id INTEGER")
    if "quantidade_esperada" not in caixas_columns:
        statements.append("ALTER TABLE caixas_hidrometros ADD COLUMN quantidade_esperada INTEGER NOT NULL DEFAULT 6")

    if hidrometros_table:
        if "baixado_por_id" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN baixado_por_id INTEGER')
        if "instalador_baixa_id" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN instalador_baixa_id INTEGER')
        if "status_operacional" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN status_operacional VARCHAR(40) NOT NULL DEFAULT \'DISPONIVEL\'')
        if "em_manutencao" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN em_manutencao BOOLEAN NOT NULL DEFAULT FALSE')
        if "bloqueado_por_manutencao" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN bloqueado_por_manutencao BOOLEAN NOT NULL DEFAULT FALSE')
        if "descartado_tecnico" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN descartado_tecnico BOOLEAN NOT NULL DEFAULT FALSE')
        if "retornou_manutencao" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN retornou_manutencao BOOLEAN NOT NULL DEFAULT FALSE')
        if "data_retorno_manutencao" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN data_retorno_manutencao DATETIME')
        if "prioridade_reutilizacao" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN prioridade_reutilizacao BOOLEAN NOT NULL DEFAULT FALSE')
        if "origem_prioridade" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN origem_prioridade VARCHAR(80)')
        if "observacao_prioridade" not in hidrometros_columns:
            statements.append(f'ALTER TABLE "{hidrometros_table}" ADD COLUMN observacao_prioridade TEXT')

    if hidrometros_table and "hidrometro_manutencao" not in tables:
        statements.append(
            """
            CREATE TABLE hidrometro_manutencao (
                id INTEGER NOT NULL PRIMARY KEY,
                hidrometro_id INTEGER NOT NULL,
                caixa_origem_id INTEGER,
                instalador_origem_id INTEGER,
                status VARCHAR(40) NOT NULL DEFAULT 'EM_MANUTENCAO',
                motivo VARCHAR(120) NOT NULL,
                descricao_problema TEXT NOT NULL,
                fornecedor_assistencia VARCHAR(160),
                data_abertura DATETIME NOT NULL,
                data_envio DATETIME,
                data_retorno DATETIME,
                laudo TEXT,
                decisao_final VARCHAR(60),
                revertida BOOLEAN NOT NULL DEFAULT FALSE,
                revertida_em DATETIME,
                revertida_por INTEGER,
                justificativa_reversao TEXT,
                destino_reversao VARCHAR(60),
                criado_por INTEGER,
                updated_at DATETIME NOT NULL
            )
            """
        )
    elif "hidrometro_manutencao" in tables:
        if "revertida" not in manutencao_columns:
            statements.append("ALTER TABLE hidrometro_manutencao ADD COLUMN revertida BOOLEAN NOT NULL DEFAULT FALSE")
        if "revertida_em" not in manutencao_columns:
            statements.append("ALTER TABLE hidrometro_manutencao ADD COLUMN revertida_em DATETIME")
        if "revertida_por" not in manutencao_columns:
            statements.append("ALTER TABLE hidrometro_manutencao ADD COLUMN revertida_por INTEGER")
        if "justificativa_reversao" not in manutencao_columns:
            statements.append("ALTER TABLE hidrometro_manutencao ADD COLUMN justificativa_reversao TEXT")
        if "destino_reversao" not in manutencao_columns:
            statements.append("ALTER TABLE hidrometro_manutencao ADD COLUMN destino_reversao VARCHAR(60)")

    if "hidrometro_id" not in movimentacoes_material_columns:
        statements.append("ALTER TABLE movimentacoes_material ADD COLUMN hidrometro_id INTEGER")
    if "feedbacks_operacionais" in tables and "urgente" not in feedback_columns:
        statements.append("ALTER TABLE feedbacks_operacionais ADD COLUMN urgente BOOLEAN NOT NULL DEFAULT FALSE")
    if "auditoria_logs" in tables and "severidade" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN severidade VARCHAR(20) NOT NULL DEFAULT 'NORMAL'")
    if "auditoria_logs" in tables and "categoria" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN categoria VARCHAR(40) NOT NULL DEFAULT 'OPERACIONAL'")
    if "auditoria_logs" in tables and "resultado" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN resultado VARCHAR(40) NOT NULL DEFAULT 'SUCESSO'")
    if "auditoria_logs" in tables and "usuario_nome" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN usuario_nome VARCHAR(150)")
    if "auditoria_logs" in tables and "usuario_email" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN usuario_email VARCHAR(150)")
    if "auditoria_logs" in tables and "usuario_perfil" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN usuario_perfil VARCHAR(40)")
    if "auditoria_logs" in tables and "ip_cliente" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN ip_cliente VARCHAR(80)")
    if "auditoria_logs" in tables and "ip_conexao" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN ip_conexao VARCHAR(80)")
    if "auditoria_logs" in tables and "x_forwarded_for" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN x_forwarded_for VARCHAR")
    if "auditoria_logs" in tables and "x_real_ip" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN x_real_ip VARCHAR(80)")
    if "auditoria_logs" in tables and "user_agent" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN user_agent VARCHAR")
    if "auditoria_logs" in tables and "request_id" not in auditoria_columns:
        statements.append("ALTER TABLE auditoria_logs ADD COLUMN request_id VARCHAR(40)")

    if not statements:
        return

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
