#!/bin/bash
# ============================================================
# setup.sh — Script de instalação e inicialização
# Uso: bash setup.sh
# ============================================================

set -e

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   Sistema de Controle de Hidrômetros — Setup         ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ---- 1. Verificar Python ----
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 não encontrado. Instale Python 3.10+ e tente novamente."
  exit 1
fi
PYTHON_VERSION=$(python3 --version 2>&1)
echo "✅ $PYTHON_VERSION encontrado"

# ---- 2. Copiar .env se não existir ----
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "✅ Arquivo .env criado a partir do .env.example"
  echo "   ⚠️  Edite o .env antes de continuar em produção!"
else
  echo "✅ Arquivo .env já existe"
fi

# ---- 3. Criar ambiente virtual ----
if [ ! -d "venv" ]; then
  python3 -m venv venv
  echo "✅ Ambiente virtual criado em ./venv"
else
  echo "✅ Ambiente virtual já existe"
fi

# ---- 4. Ativar venv e instalar dependências ----
echo ""
echo "📦 Instalando dependências Python..."
source venv/bin/activate || . venv/Scripts/activate 2>/dev/null || true
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo "✅ Dependências instaladas"

# ---- 5. Verificar PostgreSQL ----
echo ""
echo "🔍 Verificando conexão com PostgreSQL..."
python3 -c "
import os
from dotenv import load_dotenv
load_dotenv()
url = os.getenv('DATABASE_URL', '')
print('   DATABASE_URL:', url[:50] + '...' if len(url) > 50 else url)
try:
    from sqlalchemy import create_engine, text
    engine = create_engine(url)
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    print('✅ Conexão com banco de dados OK')
except Exception as e:
    print('⚠️  Não foi possível conectar ao banco:', e)
    print('   Verifique o DATABASE_URL no arquivo .env')
    print('   e certifique-se que o PostgreSQL está rodando.')
" 2>/dev/null || echo "⚠️  Não foi possível verificar o banco. Certifique-se que o PostgreSQL está rodando."

# ---- 6. Aplicar migrações ----
echo ""
echo "🗄️  Aplicando migrações do banco de dados..."
python3 -m alembic upgrade head 2>/dev/null || python3 -c "
from app.database import Base, engine
Base.metadata.create_all(bind=engine)
print('✅ Tabelas criadas diretamente (sem Alembic)')
"
echo "✅ Banco de dados pronto"

# ---- 7. Criar usuário admin ----
echo ""
echo "👤 Verificando usuário administrador..."
python3 -m app.seed

# ---- 8. Iniciar servidor ----
echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║   ✅ Setup concluído! Iniciando servidor...           ║"
echo "║                                                      ║"
echo "║   Acesse: http://localhost:8000                      ║"
echo "║   Login:  admin@sistema.com                          ║"
echo "║   Senha:  admin123  (TROQUE NO PRIMEIRO ACESSO!)     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
