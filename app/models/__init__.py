# ============================================================
# app/models/__init__.py
# Modelos ORM do sistema - mapeamento objeto-relacional
# com SQLAlchemy 2.0
# ============================================================
from datetime import datetime
from enum import Enum as PyEnum
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, ForeignKey,
    Text, Enum, Float, UniqueConstraint, Index, CheckConstraint
)
from sqlalchemy.orm import relationship
from app.database import Base
from .log import AuditoriaLog

# ============================================================
# ENUMS - Tipos controlados usados nos models
# ============================================================

class UserRole(str, PyEnum):
    """Perfis de acesso do sistema"""
    ADMIN = "admin"
    ALMOXARIFADO = "almoxarifado"
    MANIPULADOR = "manipulador"
    INSTALADOR = "instalador"


class CaixaStatus(str, PyEnum):
    """Status possíveis para uma caixa de hidrômetros"""
    EM_ESTOQUE = "em_estoque"
    ENTREGUE = "entregue"
    DEVOLVIDA = "devolvida"
    INSTALADA = "instalada"
    TRANSFERIDA_OUTRA_EMPRESA = "transferida_outra_empresa"
    CANCELADA = "cancelada"


class HidrometroStatus(str, PyEnum):
    """Status possíveis para um hidrômetro individual"""
    EM_ESTOQUE = "em_estoque"        # Na caixa, no almoxarifado
    COM_INSTALADOR = "com_instalador" # Entregue ao instalador, aguardando instalação
    INSTALADO = "instalado"           # Instalado no campo - baixa definitiva
    EM_MANUTENCAO = "em_manutencao"
    ENVIADO_ASSISTENCIA = "enviado_assistencia"
    RETORNADO_MANUTENCAO = "retornado_manutencao"
    DESCARTADO_TECNICO = "descartado_tecnico"


class ManutencaoHidrometroStatus(str, PyEnum):
    """Etapas do fluxo tecnico de manutencao do hidrometro."""
    EM_MANUTENCAO = "EM_MANUTENCAO"
    ENVIADO_ASSISTENCIA = "ENVIADO_ASSISTENCIA"
    RETORNADO_MANUTENCAO = "RETORNADO_MANUTENCAO"
    DESCARTADO_TECNICO = "DESCARTADO_TECNICO"


class MovimentacaoTipo(str, PyEnum):
    """Tipos de movimentação de materiais"""
    ENTRADA = "entrada"       # Entrada no estoque
    SAIDA = "saida"           # Saída para instalador
    DEVOLUCAO = "devolucao"   # Devolução pelo instalador
    BAIXA = "baixa"           # Hidrômetro instalado no campo


class SolicitacaoStatus(str, PyEnum):
    """Status de uma solicitação de materiais"""
    PENDENTE = "pendente"       # Criada pelo manipulador
    SEPARADA = "separada"       # Almoxarifado separou
    ENTREGUE = "entregue"       # Entregue ao instalador
    CANCELADA = "cancelada"     # Cancelada


class FeedbackStatus(str, PyEnum):
    """Status operacional do feedback interno"""
    NOVO = "novo"
    EM_ANALISE = "em_analise"
    RESOLVIDO = "resolvido"


class CarcacaTipoMovimento(str, PyEnum):
    """Movimentos controlados de carcacas de hidrometros antigos."""
    DEVOLUCAO_INSTALADOR = "devolucao_instalador"
    RECOLHIMENTO_SABESP = "recolhimento_sabesp"
    AJUSTE_ADMIN = "ajuste_admin"


# ============================================================
# MODEL: Usuario
# ============================================================

class Usuario(Base):
    """
    Usuários do sistema com controle de acesso por perfil (role).
    Perfis: admin, almoxarifado, manipulador
    """
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(100), nullable=False)
    email = Column(String(150), unique=True, nullable=False, index=True)
    matricula = Column(String(50), unique=True, nullable=True, index=True)
    senha_hash = Column(String(255), nullable=False)
    role = Column(Enum(UserRole), nullable=False, default=UserRole.MANIPULADOR)
    ativo = Column(Boolean, default=True)
    token_version = Column(Integer, default=0, nullable=False)
    session_notice_code = Column(String(80), nullable=True)
    session_notice_message = Column(Text, nullable=True)
    session_notice_at = Column(DateTime, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    ultimo_acesso = Column(DateTime, nullable=True)

    # Relacionamentos - auditoria
    auditorias = relationship("AuditoriaLog", backref="usuario")
    solicitacoes_criadas = relationship(
        "Solicitacao", foreign_keys="Solicitacao.criado_por_id", back_populates="criado_por"
    )
    solicitacoes_entregues = relationship(
        "Solicitacao", foreign_keys="Solicitacao.entregue_por_id", back_populates="entregue_por"
    )
    conferencias = relationship("ConferenciaPecas", back_populates="responsavel")
    feedbacks_enviados = relationship(
        "FeedbackOperacional",
        foreign_keys="FeedbackOperacional.usuario_id",
        back_populates="usuario",
    )
    feedbacks_resolvidos = relationship(
        "FeedbackOperacional",
        foreign_keys="FeedbackOperacional.resolvido_por_id",
        back_populates="resolvido_por",
    )
    instalador = relationship("Instalador", back_populates="usuario", uselist=False)

    def __repr__(self):
        return f"<Usuario {self.email} ({self.role})>"


# ============================================================
# MODEL: BoxRuleConfig
# ============================================================

class BoxRuleConfig(Base):
    """
    Regra historica de quantidade de hidrometros por caixa.
    Caixas antigas mantem a quantidade esperada gravada no cadastro.
    """
    __tablename__ = "box_rule_config"
    __table_args__ = (
        CheckConstraint("quantidade_hidrometros > 0", name="ck_box_rule_quantidade_positiva"),
        Index("idx_box_rule_ativo", "ativo"),
    )

    id = Column(Integer, primary_key=True, index=True)
    quantidade_hidrometros = Column(Integer, nullable=False)
    ativo = Column(Boolean, nullable=False, default=True)
    vigente_desde = Column(DateTime, default=datetime.utcnow, nullable=False)
    criado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False)

    criado_por = relationship("Usuario")

    def __repr__(self):
        return f"<BoxRuleConfig qtd={self.quantidade_hidrometros} ativo={self.ativo}>"


class SystemFlag(Base):
    """Configuracoes operacionais persistentes controladas pelo ADMIN."""
    __tablename__ = "system_flags"
    __table_args__ = (
        UniqueConstraint("chave", name="uq_system_flags_chave"),
        Index("idx_system_flags_chave", "chave"),
    )

    id = Column(Integer, primary_key=True, index=True)
    chave = Column(String(80), nullable=False)
    valor = Column(Text, nullable=True)
    motivo = Column(Text, nullable=True)
    atualizado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    atualizado_em = Column(DateTime, default=datetime.utcnow, nullable=False)

    atualizado_por = relationship("Usuario")


# ============================================================
# MODEL: Instalador
# ============================================================

class Instalador(Base):
    """
    Instaladores cadastrados pelo ADMIN.
    Recebem materiais (caixas + peças) para instalação em campo.
    """
    __tablename__ = "instaladores"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(100), nullable=False)
    cpf = Column(String(14), unique=True, nullable=True, index=True)
    data_nascimento = Column(String(10), nullable=True)  # Armazenado como DD/MM/AAAA
    matricula = Column(String(50), unique=True, nullable=False)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), unique=True, nullable=True)
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)

    # Relacionamentos
    hidrômetros = relationship(
        "Hidrometro",
        back_populates="instalador_atual",
        foreign_keys="Hidrometro.instalador_id",
    )
    movimentacoes = relationship("MovimentacaoMaterial", back_populates="instalador")
    solicitacoes = relationship("Solicitacao", back_populates="instalador")
    conferencias = relationship("ConferenciaPecas", back_populates="instalador")
    pecas_posse = relationship("InstaladorPeca", back_populates="instalador")
    usuario = relationship("Usuario", back_populates="instalador", foreign_keys=[usuario_id])

    def __repr__(self):
        return f"<Instalador {self.nome} - {self.matricula}>"


# ============================================================
# MODEL: CaixaHidrometro
# ============================================================

class CaixaHidrometro(Base):
    """
    Caixa fisica com quantidade historica esperada de hidrometros.
    O serial_number vem da fábrica; numero_interno é controle interno.
    """
    __tablename__ = "caixas_hidrometros"
    __table_args__ = (
        CheckConstraint("quantidade_esperada > 0", name="ck_caixa_quantidade_esperada_positiva"),
    )

    id = Column(Integer, primary_key=True, index=True)
    serial_number = Column(String(100), unique=True, nullable=False, index=True)
    numero_interno = Column(String(50), unique=True, nullable=False, index=True)
    status = Column(Enum(CaixaStatus), default=CaixaStatus.EM_ESTOQUE, nullable=False)
    ativo = Column(Boolean, default=True, nullable=False)
    regra_caixa_id = Column(Integer, ForeignKey("box_rule_config.id"), nullable=True)
    quantidade_esperada = Column(Integer, nullable=False, default=6)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Instalador atual (quando status = ENTREGUE)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=True)
    instalador = relationship("Instalador", foreign_keys=[instalador_id])
    regra_caixa = relationship("BoxRuleConfig")

    # Hidrometros desta caixa
    hidrômetros = relationship("Hidrometro", back_populates="caixa")

    # Movimentações desta caixa
    movimentacoes = relationship("MovimentacaoMaterial", back_populates="caixa")
    instalacoes = relationship("InstalacaoHidrometro", back_populates="caixa")
    transferencias_empresa = relationship("TransferenciaEmpresaCaixa", back_populates="caixa")

    def __repr__(self):
        return f"<Caixa {self.numero_interno} / SN:{self.serial_number}>"


# ============================================================
# MODEL: Hidrometro
# ============================================================

class Hidrometro(Base):
    """
    Hidrômetro individual com número de série único.
    Em operação normal o hidrômetro pertence a uma caixa; retornos de manutenção
    podem ficar soltos no estoque com rastreabilidade pela tabela de manutenção.
    """
    __tablename__ = "hidrômetros"

    id = Column(Integer, primary_key=True, index=True)
    numero_serie = Column(String(100), unique=True, nullable=False, index=True)
    status = Column(Enum(HidrometroStatus), default=HidrometroStatus.EM_ESTOQUE, nullable=False)
    status_operacional = Column(String(40), nullable=False, default="DISPONIVEL")
    em_manutencao = Column(Boolean, nullable=False, default=False)
    bloqueado_por_manutencao = Column(Boolean, nullable=False, default=False)
    descartado_tecnico = Column(Boolean, nullable=False, default=False)
    retornou_manutencao = Column(Boolean, nullable=False, default=False)
    data_retorno_manutencao = Column(DateTime, nullable=True)
    prioridade_reutilizacao = Column(Boolean, nullable=False, default=False)
    origem_prioridade = Column(String(80), nullable=True)
    observacao_prioridade = Column(Text, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    instalado_em = Column(DateTime, nullable=True)  # Data de instalação no campo

    # Vínculo obrigatório com caixa
    caixa_id = Column(Integer, ForeignKey("caixas_hidrometros.id"), nullable=True)
    caixa = relationship("CaixaHidrometro", back_populates="hidrômetros")

    # Instalador atual (quando status = COM_INSTALADOR)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=True)
    instalador_atual = relationship(
        "Instalador",
        back_populates="hidrômetros",
        foreign_keys=[instalador_id],
    )
    baixado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    baixado_por = relationship("Usuario", foreign_keys=[baixado_por_id])
    instalador_baixa_id = Column(Integer, ForeignKey("instaladores.id"), nullable=True)
    instalador_baixa = relationship("Instalador", foreign_keys=[instalador_baixa_id])
    instalacao = relationship("InstalacaoHidrometro", back_populates="hidrometro", uselist=False)
    manutencoes = relationship("HidrometroManutencao", back_populates="hidrometro")

    def __repr__(self):
        return f"<Hidrometro SN:{self.numero_serie} [{self.status}]>"


class HidrometroManutencao(Base):
    """Historico tecnico de retirada, assistencia, retorno e descarte de hidrometros."""
    __tablename__ = "hidrometro_manutencao"
    __table_args__ = (
        Index("idx_hidrometro_manutencao_hidrometro", "hidrometro_id"),
        Index("idx_hidrometro_manutencao_caixa_origem", "caixa_origem_id"),
        Index("idx_hidrometro_manutencao_status", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    hidrometro_id = Column(Integer, ForeignKey(f"{Hidrometro.__tablename__}.id"), nullable=False)
    caixa_origem_id = Column(Integer, ForeignKey("caixas_hidrometros.id"), nullable=True)
    instalador_origem_id = Column(Integer, ForeignKey("instaladores.id"), nullable=True)
    status = Column(String(40), nullable=False, default=ManutencaoHidrometroStatus.EM_MANUTENCAO.value)
    motivo = Column(String(120), nullable=False)
    descricao_problema = Column(Text, nullable=False)
    fornecedor_assistencia = Column(String(160), nullable=True)
    data_abertura = Column(DateTime, default=datetime.utcnow, nullable=False)
    data_envio = Column(DateTime, nullable=True)
    data_retorno = Column(DateTime, nullable=True)
    laudo = Column(Text, nullable=True)
    decisao_final = Column(String(60), nullable=True)
    revertida = Column(Boolean, nullable=False, default=False)
    revertida_em = Column(DateTime, nullable=True)
    revertida_por = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    justificativa_reversao = Column(Text, nullable=True)
    destino_reversao = Column(String(60), nullable=True)
    criado_por = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    hidrometro = relationship("Hidrometro", back_populates="manutencoes")
    caixa_origem = relationship("CaixaHidrometro", foreign_keys=[caixa_origem_id])
    instalador_origem = relationship("Instalador", foreign_keys=[instalador_origem_id])
    criado_por_usuario = relationship("Usuario", foreign_keys=[criado_por])
    revertida_por_usuario = relationship("Usuario", foreign_keys=[revertida_por])


class InstalacaoHidrometro(Base):
    """
    Evento empresarial de instalacao usado para produtividade.
    Um hidrometro instalado gera no maximo um evento ativo.
    """
    __tablename__ = "instalacoes_hidrometros"
    __table_args__ = (
        UniqueConstraint("hidrometro_id", name="uq_instalacao_hidrometro_unica"),
        Index("idx_instalacao_instalador_data", "instalador_id", "data_instalacao"),
        Index("idx_instalacao_caixa", "caixa_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=False)
    hidrometro_id = Column(Integer, ForeignKey("hidrômetros.id"), nullable=False)
    caixa_id = Column(Integer, ForeignKey("caixas_hidrometros.id"), nullable=False)
    solicitacao_id = Column(Integer, ForeignKey("solicitacoes.id"), nullable=True)
    data_instalacao = Column(DateTime, nullable=False, default=datetime.utcnow)
    usuario_registro_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False)

    instalador = relationship("Instalador")
    hidrometro = relationship("Hidrometro", back_populates="instalacao")
    caixa = relationship("CaixaHidrometro", back_populates="instalacoes")
    solicitacao = relationship("Solicitacao")
    usuario_registro = relationship("Usuario")


# ============================================================
# MODEL: TipoPeca
# ============================================================

class TipoPeca(Base):
    """
    Tipos de peças cadastrados dinamicamente pelo ADMIN.
    Exemplos: Lacre, Conector, Vedação, etc.
    """
    __tablename__ = "tipos_pecas"

    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String(100), unique=True, nullable=False)
    descricao = Column(Text, nullable=True)
    unidade_medida = Column(String(20), default="unidade")  # unidade, metro, kg, etc.
    estoque_minimo_percentual = Column(Float, default=20.0)  # % mínimo antes de alertar
    ativo = Column(Boolean, default=True)
    criado_em = Column(DateTime, default=datetime.utcnow)

    # Relacionamentos
    estoque = relationship("EstoquePeca", back_populates="tipo_peca", uselist=False)
    movimentacoes = relationship("MovimentacaoPeca", back_populates="tipo_peca")

    def __repr__(self):
        return f"<TipoPeca {self.nome}>"


# ============================================================
# MODEL: EstoquePeca
# ============================================================

class EstoquePeca(Base):
    """
    Controle de estoque atual de cada tipo de peça.
    Um registro por tipo de peça.
    """
    __tablename__ = "estoque_pecas"
    __table_args__ = (
        CheckConstraint("quantidade_atual >= 0", name="ck_estoque_quantidade_nao_negativa"),
        CheckConstraint("quantidade_maxima > 0", name="ck_estoque_quantidade_maxima_positiva"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tipo_peca_id = Column(Integer, ForeignKey("tipos_pecas.id"), unique=True, nullable=False)
    quantidade_atual = Column(Integer, default=0, nullable=False)
    quantidade_maxima = Column(Integer, default=100, nullable=False)  # Para cálculo do percentual
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tipo_peca = relationship("TipoPeca", back_populates="estoque")

    @property
    def percentual_atual(self):
        """Retorna o percentual atual do estoque em relação ao máximo"""
        if self.quantidade_maxima == 0:
            return 0
        return (self.quantidade_atual / self.quantidade_maxima) * 100

    @property
    def abaixo_minimo(self):
        """Verifica se o estoque está abaixo do mínimo configurado"""
        if not self.tipo_peca:
            return False
        return self.percentual_atual < self.tipo_peca.estoque_minimo_percentual

    def __repr__(self):
        return f"<EstoquePeca tipo={self.tipo_peca_id} qtd={self.quantidade_atual}>"


# ============================================================
# MODEL: InstaladorPeca
# ============================================================

class InstaladorPeca(Base):
    """
    Controle de peças em posse de cada instalador.
    Atualizado a cada movimentação ou conferência.
    """
    __tablename__ = "instalador_pecas"

    id = Column(Integer, primary_key=True, index=True)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=False)
    tipo_peca_id = Column(Integer, ForeignKey("tipos_pecas.id"), nullable=False)
    quantidade = Column(Integer, default=0, nullable=False)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    instalador = relationship("Instalador", back_populates="pecas_posse")
    tipo_peca = relationship("TipoPeca")

    __table_args__ = (
        UniqueConstraint("instalador_id", "tipo_peca_id", name="uq_instalador_peca"),
        CheckConstraint("quantidade >= 0", name="ck_instalador_peca_quantidade_nao_negativa"),
    )


# ============================================================
# MODEL: Solicitacao
# ============================================================

class Solicitacao(Base):
    """
    Solicitação de materiais criada pelo MANIPULADOR.
    Fluxo: PENDENTE → SEPARADA (almoxarifado) → ENTREGUE → confirmada pelo instalador
    """
    __tablename__ = "solicitacoes"
    __table_args__ = (
        Index("idx_solicitacao_instalador_status_entrega", "instalador_id", "status", "entregue_em", "id"),
        Index("idx_solicitacao_instalador_criada", "instalador_id", "criado_em", "id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=False)
    status = Column(Enum(SolicitacaoStatus), default=SolicitacaoStatus.PENDENTE, nullable=False)
    observacoes = Column(Text, nullable=True)

    # Rastreabilidade de quem criou e quem entregou
    criado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    entregue_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)

    criado_em = Column(DateTime, default=datetime.utcnow)
    separado_em = Column(DateTime, nullable=True)
    entregue_em = Column(DateTime, nullable=True)
    recebimento_instalador_status = Column(String(40), nullable=True)
    confirmacao_instalador_em = Column(DateTime, nullable=True)
    usuario_confirmacao_instalador_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    motivo_divergencia_instalador = Column(Text, nullable=True)

    # Relacionamentos
    instalador = relationship("Instalador", back_populates="solicitacoes")
    criado_por = relationship("Usuario", foreign_keys=[criado_por_id], back_populates="solicitacoes_criadas")
    entregue_por = relationship("Usuario", foreign_keys=[entregue_por_id], back_populates="solicitacoes_entregues")
    usuario_confirmacao_instalador = relationship("Usuario", foreign_keys=[usuario_confirmacao_instalador_id])
    itens_caixa = relationship("SolicitacaoItemCaixa", back_populates="solicitacao")
    itens_peca = relationship("SolicitacaoItemPeca", back_populates="solicitacao")

    def __repr__(self):
        return f"<Solicitacao #{self.id} [{self.status}]>"


class SolicitacaoItemCaixa(Base):
    """Itens de caixa de uma solicitação"""
    __tablename__ = "solicitacao_itens_caixa"
    __table_args__ = (
        CheckConstraint("quantidade_solicitada > 0", name="ck_solicitacao_caixa_quantidade_positiva"),
        Index("idx_solicitacao_item_caixa_solicitacao", "solicitacao_id"),
        Index("idx_solicitacao_item_caixa_caixa", "caixa_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    solicitacao_id = Column(Integer, ForeignKey("solicitacoes.id"), nullable=False)
    caixa_id = Column(Integer, ForeignKey("caixas_hidrometros.id"), nullable=True)  # Preenchido após separação
    quantidade_solicitada = Column(Integer, default=1, nullable=False)

    solicitacao = relationship("Solicitacao", back_populates="itens_caixa")
    caixa = relationship("CaixaHidrometro")


class SolicitacaoItemPeca(Base):
    """Itens de peça de uma solicitação"""
    __tablename__ = "solicitacao_itens_peca"
    __table_args__ = (
        CheckConstraint("quantidade_solicitada > 0", name="ck_solicitacao_peca_quantidade_positiva"),
        Index("idx_solicitacao_item_peca_solicitacao", "solicitacao_id"),
        Index("idx_solicitacao_item_peca_tipo", "tipo_peca_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    solicitacao_id = Column(Integer, ForeignKey("solicitacoes.id"), nullable=False)
    tipo_peca_id = Column(Integer, ForeignKey("tipos_pecas.id"), nullable=False)
    quantidade_solicitada = Column(Integer, nullable=False)

    solicitacao = relationship("Solicitacao", back_populates="itens_peca")
    tipo_peca = relationship("TipoPeca")


# ============================================================
# MODEL: MovimentacaoMaterial
# ============================================================

class MovimentacaoMaterial(Base):
    """
    Log de todas as movimentações de caixas/hidrômetros.
    Imutável - nunca deletar registros de movimentação.
    """
    __tablename__ = "movimentacoes_material"
    __table_args__ = (
        Index("idx_mov_material_solicitacao", "solicitacao_id"),
        Index("idx_mov_material_instalador_criado", "instalador_id", "criado_em"),
        Index("idx_mov_material_caixa_criado", "caixa_id", "criado_em"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tipo = Column(Enum(MovimentacaoTipo), nullable=False)
    caixa_id = Column(Integer, ForeignKey("caixas_hidrometros.id"), nullable=True)
    hidrometro_id = Column(Integer, ForeignKey("hidrômetros.id"), nullable=True)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=True)
    solicitacao_id = Column(Integer, ForeignKey("solicitacoes.id"), nullable=True)
    observacoes = Column(Text, nullable=True)
    registrado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)

    # Relacionamentos
    caixa = relationship("CaixaHidrometro", back_populates="movimentacoes")
    hidrometro = relationship("Hidrometro", foreign_keys=[hidrometro_id])
    instalador = relationship("Instalador", back_populates="movimentacoes")
    registrado_por = relationship("Usuario")


# ============================================================
# MODEL: MovimentacaoPeca
# ============================================================

class MovimentacaoPeca(Base):
    """
    Log de movimentações de peças (entrada/saída/devolução).
    Imutável - nunca deletar registros.
    """
    __tablename__ = "movimentacoes_pecas"
    __table_args__ = (
        CheckConstraint("quantidade > 0", name="ck_movimentacao_peca_quantidade_positiva"),
        Index("idx_mov_peca_solicitacao", "solicitacao_id"),
        Index("idx_mov_peca_instalador_criado", "instalador_id", "criado_em"),
        Index("idx_mov_peca_tipo_criado", "tipo_peca_id", "criado_em"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tipo = Column(Enum(MovimentacaoTipo), nullable=False)
    tipo_peca_id = Column(Integer, ForeignKey("tipos_pecas.id"), nullable=False)
    quantidade = Column(Integer, nullable=False)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=True)
    solicitacao_id = Column(Integer, ForeignKey("solicitacoes.id"), nullable=True)
    observacoes = Column(Text, nullable=True)
    registrado_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow)

    tipo_peca = relationship("TipoPeca", back_populates="movimentacoes")
    instalador = relationship("Instalador")
    registrado_por = relationship("Usuario")


class CarcacaMovimentacao(Base):
    """Controle de carcacas devolvidas por instaladores e recolhidas pela Sabesp."""
    __tablename__ = "carcaca_movimentacoes"
    __table_args__ = (
        CheckConstraint("quantidade > 0", name="ck_carcaca_quantidade_positiva"),
        Index("idx_carcaca_tipo_data", "tipo_movimento", "data_movimento"),
        Index("idx_carcaca_instalador", "instalador_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    tipo_movimento = Column(Enum(CarcacaTipoMovimento), nullable=False)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=True)
    quantidade = Column(Integer, nullable=False)
    data_movimento = Column(DateTime, nullable=False)
    documento_referencia = Column(String(120), nullable=True)
    observacao = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False)

    instalador = relationship("Instalador")
    usuario = relationship("Usuario")


class TransferenciaEmpresa(Base):
    """Cabecalho de transferencia de materiais para outra empresa."""
    __tablename__ = "transferencias_empresa"
    __table_args__ = (
        Index("idx_transferencia_empresa_data", "data_transferencia"),
        Index("idx_transferencia_empresa_destino", "empresa_destino"),
    )

    id = Column(Integer, primary_key=True, index=True)
    empresa_destino = Column(String(180), nullable=False)
    data_transferencia = Column(DateTime, nullable=False)
    responsavel = Column(String(150), nullable=False)
    documento_referencia = Column(String(120), nullable=True)
    observacao = Column(Text, nullable=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False)

    usuario = relationship("Usuario")
    caixas = relationship("TransferenciaEmpresaCaixa", back_populates="transferencia")
    pecas = relationship("TransferenciaEmpresaPeca", back_populates="transferencia")


class TransferenciaEmpresaCaixa(Base):
    """Caixas vinculadas a uma transferencia externa."""
    __tablename__ = "transferencia_empresa_caixas"
    __table_args__ = (
        UniqueConstraint("caixa_id", name="uq_transferencia_caixa_unica"),
    )

    id = Column(Integer, primary_key=True, index=True)
    transferencia_id = Column(Integer, ForeignKey("transferencias_empresa.id"), nullable=False)
    caixa_id = Column(Integer, ForeignKey("caixas_hidrometros.id"), nullable=False)

    transferencia = relationship("TransferenciaEmpresa", back_populates="caixas")
    caixa = relationship("CaixaHidrometro", back_populates="transferencias_empresa")


class TransferenciaEmpresaPeca(Base):
    """Pecas transferidas para outra empresa."""
    __tablename__ = "transferencia_empresa_pecas"
    __table_args__ = (
        CheckConstraint("quantidade > 0", name="ck_transferencia_peca_quantidade_positiva"),
    )

    id = Column(Integer, primary_key=True, index=True)
    transferencia_id = Column(Integer, ForeignKey("transferencias_empresa.id"), nullable=False)
    tipo_peca_id = Column(Integer, ForeignKey("tipos_pecas.id"), nullable=False)
    quantidade = Column(Integer, nullable=False)

    transferencia = relationship("TransferenciaEmpresa", back_populates="pecas")
    tipo_peca = relationship("TipoPeca")


# ============================================================
# MODEL: FeedbackOperacional
# ============================================================

class FeedbackOperacional(Base):
    """
    Canal interno para feedback de UX, erro operacional e sugestao.
    """
    __tablename__ = "feedbacks_operacionais"

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    area = Column(String(40), nullable=False)
    categoria = Column(String(40), nullable=False)
    urgente = Column(Boolean, nullable=False, default=False)
    titulo = Column(String(160), nullable=False)
    mensagem = Column(Text, nullable=False)
    pagina_origem = Column(String(255), nullable=True)
    status = Column(Enum(FeedbackStatus), nullable=False, default=FeedbackStatus.NOVO)
    criado_em = Column(DateTime, default=datetime.utcnow)
    atualizado_em = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolvido_em = Column(DateTime, nullable=True)
    resolvido_por_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)

    usuario = relationship("Usuario", foreign_keys=[usuario_id], back_populates="feedbacks_enviados")
    resolvido_por = relationship("Usuario", foreign_keys=[resolvido_por_id], back_populates="feedbacks_resolvidos")
    anexos = relationship("FeedbackAnexo", back_populates="feedback", cascade="all, delete-orphan")


class FeedbackAnexo(Base):
    """Arquivo enviado junto ao feedback operacional."""
    __tablename__ = "feedback_anexos"
    __table_args__ = (
        Index("idx_feedback_anexo_feedback", "feedback_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    feedback_id = Column(Integer, ForeignKey("feedbacks_operacionais.id"), nullable=False)
    nome_original = Column(String(255), nullable=False)
    nome_armazenado = Column(String(255), nullable=False)
    caminho_relativo = Column(String(500), nullable=False)
    content_type = Column(String(120), nullable=True)
    tamanho_bytes = Column(Integer, nullable=False, default=0)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False)

    feedback = relationship("FeedbackOperacional", back_populates="anexos")


# ============================================================
# MODEL: ConferenciaPecas
# ============================================================

class ConferenciaPecas(Base):
    """
    Registro das conferências quinzenais obrigatórias de peças por instalador.
    Controla divergências e ajustes de estoque.
    """
    __tablename__ = "conferencias_pecas"

    id = Column(Integer, primary_key=True, index=True)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=False)
    responsavel_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    data_conferencia = Column(DateTime, default=datetime.utcnow)
    observacoes = Column(Text, nullable=True)
    tem_divergencia = Column(Boolean, default=False)

    instalador = relationship("Instalador", back_populates="conferencias")
    responsavel = relationship("Usuario", back_populates="conferencias")
    itens = relationship("ConferenciaItem", back_populates="conferencia")


class ConferenciaItem(Base):
    """Item individual de uma conferência de peças"""
    __tablename__ = "conferencia_itens"
    __table_args__ = (
        CheckConstraint("quantidade_sistema >= 0", name="ck_conferencia_quantidade_sistema_nao_negativa"),
        CheckConstraint("quantidade_real >= 0", name="ck_conferencia_quantidade_real_nao_negativa"),
    )

    id = Column(Integer, primary_key=True, index=True)
    conferencia_id = Column(Integer, ForeignKey("conferencias_pecas.id"), nullable=False)
    tipo_peca_id = Column(Integer, ForeignKey("tipos_pecas.id"), nullable=False)
    quantidade_sistema = Column(Integer, nullable=False)    # O que o sistema dizia
    quantidade_real = Column(Integer, nullable=False)       # O que foi contado fisicamente
    diferenca = Column(Integer, nullable=False)             # real - sistema

    conferencia = relationship("ConferenciaPecas", back_populates="itens")
    tipo_peca = relationship("TipoPeca")


class ConferenciaInstaladorPeca(Base):
    """Registro informativo normalizado de peca declarada em conferencia."""
    __tablename__ = "conferencia_instalador_pecas"
    __table_args__ = (
        CheckConstraint("quantidade_informada >= 0", name="ck_conf_inst_peca_quantidade_nao_negativa"),
        Index("idx_conf_inst_peca_instalador_data", "instalador_id", "data_conferencia"),
    )

    id = Column(Integer, primary_key=True, index=True)
    conferencia_id = Column(Integer, ForeignKey("conferencias_pecas.id"), nullable=True)
    instalador_id = Column(Integer, ForeignKey("instaladores.id"), nullable=False)
    peca_id = Column(Integer, ForeignKey("tipos_pecas.id"), nullable=False)
    quantidade_informada = Column(Integer, nullable=False)
    data_conferencia = Column(DateTime, default=datetime.utcnow, nullable=False)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=False)
    observacao = Column(Text, nullable=True)
    criado_em = Column(DateTime, default=datetime.utcnow, nullable=False)

    conferencia = relationship("ConferenciaPecas")
    instalador = relationship("Instalador")
    peca = relationship("TipoPeca")
    usuario = relationship("Usuario")
