from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String

from app.database import Base
from app.utils import utc_now


class AuditoriaLog(Base):
    __tablename__ = "auditoria_logs"
    __table_args__ = (
        Index("idx_auditoria_usuario", "usuario_id"),
        Index("idx_auditoria_data", "criado_em"),
        Index("idx_auditoria_severidade", "severidade"),
    )

    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), nullable=True)
    usuario_nome = Column(String(150), nullable=True)
    usuario_email = Column(String(150), nullable=True)
    usuario_perfil = Column(String(40), nullable=True)
    acao = Column(String, nullable=False)
    tabela = Column(String, nullable=True)
    registro_id = Column(Integer, nullable=True)
    descricao = Column(String, nullable=True)
    severidade = Column(String(20), nullable=False, default="NORMAL")
    categoria = Column(String(40), nullable=False, default="OPERACIONAL")
    resultado = Column(String(40), nullable=False, default="SUCESSO")
    dados_antes = Column(JSON, nullable=True)
    dados_depois = Column(JSON, nullable=True)
    ip = Column(String, nullable=True)
    ip_cliente = Column(String(80), nullable=True)
    ip_conexao = Column(String(80), nullable=True)
    x_forwarded_for = Column(String, nullable=True)
    x_real_ip = Column(String(80), nullable=True)
    user_agent = Column(String, nullable=True)
    request_id = Column(String(40), nullable=True)
    criado_em = Column(DateTime, default=utc_now, nullable=False)
