@echo off
REM ============================================================
REM setup.bat — Script de instalacao para Windows
REM Duplo clique ou execute no terminal: setup.bat
REM ============================================================

echo.
echo ╔══════════════════════════════════════════════════════╗
echo ║   Sistema de Controle de Hidrômetros — Setup         ║
echo ╚══════════════════════════════════════════════════════╝
echo.

REM ---- 1. Verificar Python ----
python --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo [ERRO] Python nao encontrado.
    echo        Instale Python 3.10+ em https://python.org
    echo        Marque "Add Python to PATH" durante a instalacao!
    pause
    exit /b 1
)
echo [OK] Python encontrado

REM ---- 2. Copiar .env ----
IF NOT EXIST ".env" (
    copy .env.example .env >nul
    echo [OK] Arquivo .env criado - edite antes de usar em producao
) ELSE (
    echo [OK] Arquivo .env ja existe
)

REM ---- 3. Criar ambiente virtual ----
IF NOT EXIST "venv" (
    python -m venv venv
    echo [OK] Ambiente virtual criado
) ELSE (
    echo [OK] Ambiente virtual ja existe
)

REM ---- 4. Instalar dependencias ----
echo.
echo Instalando dependencias Python (aguarde)...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q
echo [OK] Dependencias instaladas

REM ---- 5. Criar tabelas no banco ----
echo.
echo Criando tabelas no banco de dados...
python -c "from app.database import Base, engine; Base.metadata.create_all(bind=engine); print('[OK] Tabelas criadas')"
IF ERRORLEVEL 1 (
    echo.
    echo [AVISO] Nao foi possivel criar as tabelas.
    echo         Verifique se o PostgreSQL esta rodando
    echo         e se o DATABASE_URL no .env esta correto.
    echo.
    pause
)

REM ---- 6. Criar admin ----
python -m app.seed

REM ---- 7. Iniciar servidor ----
echo.
echo ╔══════════════════════════════════════════════════════╗
echo ║  Setup concluido! Iniciando servidor...              ║
echo ║                                                      ║
echo ║  Acesse: http://localhost:8000                       ║
echo ║  Login:  admin@sistema.com                          ║
echo ║  Senha:  admin123  ^(TROQUE NO PRIMEIRO ACESSO!^)     ║
echo ╚══════════════════════════════════════════════════════╝
echo.

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
pause
