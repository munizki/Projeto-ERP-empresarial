# 💧 Sistema de Controle de Hidrômetros e Estoque

Sistema web corporativo desenvolvido com **FastAPI + PostgreSQL + Jinja2**.

---

## 🚀 Como Rodar (Passo a Passo)

### ✅ Pré-requisitos

| Requisito | Versão | Download |
|-----------|--------|---------|
| Python | 3.10+ | https://python.org |
| PostgreSQL | 14+ | https://postgresql.org |
| Git (opcional) | qualquer | https://git-scm.com |

> **Windows:** Ao instalar o Python, marque ✅ "Add Python to PATH"

---

### 🗄️ 1. Configurar o Banco de Dados PostgreSQL

Abra o terminal do PostgreSQL (`psql`) e execute:

```sql
CREATE USER hidrometros_user WITH PASSWORD 'hidrometros_pass';
CREATE DATABASE hidrometros_db OWNER hidrometros_user;
GRANT ALL PRIVILEGES ON DATABASE hidrometros_db TO hidrometros_user;
```

---

### ⚙️ 2. Configurar o Arquivo .env

Copie o arquivo de exemplo e edite:

# Windows
copy .env.example .env
```

Abra o `.env` e verifique:

```env
DATABASE_URL=postgresql://hidrometros_user:hidrometros_pass@localhost:5432/hidrometros_db
SECRET_KEY=mude-esta-chave-em-producao
```

---

### 🖥️ 3. Instalar e Rodar

#### Windows (mais simples)
```
Duplo clique em: setup.bat
```

#### Linux / Mac
```bash
bash setup.sh
```

#### Ou manualmente (qualquer OS)
```bash
# Criar ambiente virtual
python -m venv venv

# Ativar
source venv/bin/activate      # Linux/Mac
venv\Scripts\activate.bat     # Windows

# Instalar dependências
pip install -r requirements.txt

# Criar tabelas
python -c "from app.database import Base, engine; Base.metadata.create_all(bind=engine)"

# Criar o primeiro admin com credenciais seguras
INITIAL_ADMIN_EMAIL=admin@seudominio.com.br INITIAL_ADMIN_PASSWORD='SenhaForte@123' python -m app.seed --non-interactive

# Iniciar servidor
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

### 🌐 4. Acessar o Sistema

Abra o navegador:

```
http://localhost:8000
```

**Primeiro acesso seguro:**

Defina `INITIAL_ADMIN_EMAIL` e `INITIAL_ADMIN_PASSWORD` antes de executar o seed inicial.

Use uma senha forte com no minimo 12 caracteres, letras maiusculas e minusculas, numero e caractere especial.

---

### 🐳 Alternativa: Rodar com Docker(Mais recomendado para facilitade do sistema)

Se tiver Docker instalado:

```bash
docker compose up -d --build
```

O sistema estará disponível em `http://localhost:8000`.

Os containers oficiais deste projeto usam o prefixo `gmf_hidrometros_*`.

Para verificar: `docker compose ps`

Para parar: `docker compose down`

---

## 🔐 Perfis de Acesso

| Perfil | O que pode fazer |
|--------|-----------------|
| **Admin** | Tudo: usuários, instaladores, tipos de peças, auditoria |
| **Almoxarifado** | Cadastrar caixas, entrada de peças, separar e entregar |
| **Manipulador** | Solicitações, baixa de hidrômetros, conferência, rastreamento |

---

## 📋 Fluxo Operacional

```
1. ADMIN cadastra instaladores e tipos de peças
2. ALMOXARIFADO cadastra caixas (com 6 hidrômetros cada)
3. MANIPULADOR cria solicitação (caixas + peças para instalador)
4. ALMOXARIFADO separa os itens físicos
5. ALMOXARIFADO confirma entrega → sistema atualiza automaticamente
6. MANIPULADOR dá baixa quando hidrômetro é instalado em campo
7. MANIPULADOR faz conferência quinzenal de peças
```

---

## 📁 Estrutura do Projeto

```
hidrometros/
│
├── app/
│   ├── main.py              # Entrada FastAPI + registro de routers
│   ├── database.py          # Conexão PostgreSQL com SQLAlchemy
│   ├── security.py          # JWT, bcrypt, controle de roles
│   ├── seed.py              # Cria usuário admin padrão
│   │
│   ├── models/
│   │   └── __init__.py      # Todos os 13 models ORM
│   │
│   ├── routers/
│   │   ├── auth.py          # Login, logout, troca de senha
│   │   ├── admin.py         # Usuários, instaladores, peças, auditoria
│   │   ├── almoxarifado.py  # Caixas, estoque, separação, entrega
│   │   ├── manipulador.py   # Solicitações, baixas, conferência
│   │   └── dashboard.py     # Dashboard e relatórios
│   │
│   ├── services/
│   │   └── auditoria.py     # Serviço de log de auditoria
│   │
│   ├── static/
│   │   ├── css/style.css    # Estilos corporativos
│   │   └── js/app.js        # JavaScript global
│   │
│   └── templates/           # Templates Jinja2
│       ├── shared/          # Base, dashboard, relatórios, erro
│       ├── auth/            # Login, trocar senha
│       ├── admin/           # Todas as telas do admin
│       ├── almoxarifado/    # Todas as telas do almoxarifado
│       └── manipulador/     # Todas as telas do manipulador
│
├── alembic/                 # Migrações do banco de dados
│   ├── env.py
│   └── versions/
│
├── .env.example             # Template de variáveis de ambiente
├── alembic.ini              # Configuração do Alembic
├── requirements.txt         # Dependências Python
├── Dockerfile               # Container da aplicação
├── docker-compose.yml       # PostgreSQL + App juntos
├── setup.sh                 # Instalação automática Linux/Mac
└── setup.bat                # Instalação automática Windows
```

---

## 🗄️ Models do Banco de Dados

| Model | Descrição |
|-------|-----------|
| `Usuario` | Usuários do sistema (admin/almoxarifado/manipulador) |
| `Instalador` | Instaladores cadastrados pelo admin |
| `CaixaHidrometro` | Caixa com serial e número interno |
| `Hidrometro` | Hidrômetro individual (sempre vinculado a uma caixa) |
| `TipoPeca` | Tipos de peças cadastrados dinamicamente |
| `EstoquePeca` | Quantidade atual de cada tipo de peça |
| `InstaladorPeca` | Peças em posse de cada instalador |
| `Solicitacao` | Pedido de materiais (caixas + peças) |
| `SolicitacaoItemCaixa` | Caixas de uma solicitação |
| `SolicitacaoItemPeca` | Peças de uma solicitação |
| `MovimentacaoMaterial` | Log de movimentações de caixas |
| `MovimentacaoPeca` | Log de movimentações de peças |
| `ConferenciaPecas` | Conferência quinzenal |
| `ConferenciaItem` | Item individual de uma conferência |
| `Auditoria` | Log imutável de todas as ações |

---

## 🔧 Comandos Úteis

```bash
# Rodar em modo desenvolvimento (com auto-reload)
uvicorn app.main:app --reload --port 8000

# Rodar em produção (sem reload)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2

# Gerar nova migração após alterar models
alembic revision --autogenerate -m "descricao da mudanca"

# Aplicar migrações
alembic upgrade head

# Reverter última migração
alembic downgrade -1

# Ver histórico de migrações
alembic history
```

---

## 🔒 Segurança em Produção

Antes de colocar em produção, edite o `.env`:

```env
# Gere uma chave segura:
# python -c "import secrets; print(secrets.token_hex(32))"
SECRET_KEY=cole-aqui-a-chave-gerada

# Desativar debug
DEBUG=False

# Aumentar tempo de sessão se necessário (em minutos)
ACCESS_TOKEN_EXPIRE_MINUTES=480
```

---

## 🆘 Solução de Problemas

**Erro: `connection refused` no banco**
- Verifique se o PostgreSQL está rodando
- Confirme o `DATABASE_URL` no `.env`
- No Windows: Services → PostgreSQL → Start

**Erro: `Module not found`**
- Certifique-se que o ambiente virtual está ativado
- Rode `pip install -r requirements.txt` novamente

**Porta 8000 em uso**
```bash
uvicorn app.main:app --port 8001
```

**Resetar senha do admin**
```bash
python -c "
from app.database import SessionLocal
from app.models import Usuario
from app.security import hash_senha
db = SessionLocal()
u = db.query(Usuario).filter(Usuario.email=='admin@sistema.com').first()
u.senha_hash = hash_senha('nova_senha_aqui')
db.commit()
print('Senha redefinida!')
"
```

---

## 📞 Suporte

Para adicionar novas funcionalidades ou corrigir erros:
1. Abra o arquivo relevante (comentários explicam cada seção)
2. Os models estão em `app/models/__init__.py`
3. As rotas estão em `app/routers/`
4. Os templates HTML estão em `app/templates/`

---

## Documentacao Empresarial

A documentacao oficial do projeto fica em `docs/`:

- `docs/DOCUMENTACAO_TECNICA.md`: arquitetura, codigo, banco, seguranca, auditoria, deploy, testes e manutencao.
- `docs/MANUAL_USO_SISTEMA.md`: manual operacional por perfil e por funcao do sistema.
- `docs/README.md`: indice da documentacao.
- `MIGRACAO_WINDOWS_11.md`: roteiro de implantacao para maquina dedicada Windows 11.
- `RELATORIO_VALIDACAO_MIGRACAO_WINDOWS_11.md`: registro da validacao feita antes do pacote de implantacao.

Regra de suporte: qualquer erro, duvida operacional, tela confusa ou divergencia deve ser registrado pela area **Feedback** do sistema, marcando como urgente quando afetar a operacao.

---

*Sistema desenvolvido com FastAPI, SQLAlchemy, PostgreSQL e Jinja2.*
