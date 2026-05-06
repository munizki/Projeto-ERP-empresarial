# Documentacao Tecnica do Codigo

## 1. Visao Geral

O sistema e uma aplicacao web corporativa para controle de hidrometros, caixas, pecas, instaladores, solicitacoes, baixas, auditoria, produtividade, carcacas, transferencias externas, relatorios e monitoramento operacional.

Objetivo tecnico principal:

- manter rastreabilidade total de materiais;
- impedir erros operacionais relevantes;
- garantir que a interface e o backend validem as mesmas regras;
- preservar historico e auditoria;
- permitir que apenas o ADMIN execute correcoes excepcionais;
- preparar a operacao para uso empresarial com banco vazio ou dados existentes.

Stack principal:

- **Backend:** FastAPI.
- **Templates:** Jinja2.
- **Banco:** PostgreSQL em producao; SQLite apenas para cenarios locais/testes simples.
- **ORM:** SQLAlchemy.
- **Migracoes:** Alembic.
- **Autenticacao:** JWT em cookie HTTP.
- **Senhas:** Argon2 com compatibilidade de migracao para hashes antigos.
- **Rate limit:** Redis quando configurado.
- **Frontend:** HTML, CSS e JavaScript estaticos.
- **Deploy local/empresarial:** Docker Compose com app, PostgreSQL, Redis e backup automatico.

## 2. Estrutura do Projeto

Diretorios principais:

- `app/main.py`: cria a aplicacao FastAPI, registra middlewares, routers, arquivos estaticos, healthcheck e handlers globais de erro.
- `app/database.py`: configura engine SQLAlchemy, pool de conexoes e sessao de banco.
- `app/security.py`: autenticacao, autorizacao por perfil, politica de senha, JWT, CSRF, rate limit de login e verificacoes de seguranca.
- `app/models/__init__.py`: modelos ORM principais do sistema.
- `app/models/log.py`: modelo de auditoria.
- `app/routers/`: rotas HTTP separadas por dominio funcional.
- `app/services/`: regras de negocio, validacoes, auditoria, backup, exportacao, integridade e protecao operacional.
- `app/templates/`: telas Jinja2.
- `app/static/css/style.css`: visual corporativo, temas e responsividade.
- `app/static/js/app.js`: comportamentos globais da interface.
- `alembic/versions/`: migracoes reais de banco.
- `tests/test_enterprise_ready.py`: testes empresariais de regras, seguranca e fluxos.
- `docker-compose.yml`: ambiente com aplicacao, banco, Redis e backup automatico.
- `nginx/`: arquivos de proxy/reverse proxy quando usado em producao.
- `backups/`: pasta local de backups gerados.
- `logs/`: logs persistentes da aplicacao.

## 3. Entrada da Aplicacao

Arquivo: `app/main.py`

Responsabilidades:

- configurar logging antes da aplicacao iniciar;
- criar tabelas automaticamente somente fora de producao;
- bloquear inicializacao insegura em producao quando variaveis criticas estiverem incorretas;
- instalar hardening de runtime;
- registrar middlewares de contexto, modo leitura e protecao operacional;
- expor `/healthz` para verificar banco e saude basica;
- registrar handler de `HTTPException`;
- registrar handler global de excecao para evitar pagina em branco;
- auditar erro critico quando ocorrer excecao nao tratada.

Regra importante:

- em producao, o schema deve ser aplicado por Alembic, nao por `create_all()`.

## 4. Perfis e Permissoes

Perfis existentes:

- `ADMIN`
- `MANIPULADOR`
- `ALMOXARIFADO`

Modelo: `Usuario`

Regras gerais:

- ADMIN possui acesso total e executa correcoes excepcionais.
- MANIPULADOR opera solicitacoes, baixas, conferencias informativas, rastreamento e leitura de produtividade.
- ALMOXARIFADO opera estoque, caixas, separacao, entrega, carcacas e transferencias conforme fluxo atual.
- Relatorios sensiveis ficam restritos ao ADMIN.
- Acoes criticas exigem permissao, motivo, confirmacao contextual e auditoria.

Autorizacao:

- `requer_autenticacao()` exige usuario logado.
- `requer_role(...)` exige perfil especifico.
- Tentativas de acesso negado sao auditadas como evento de seguranca.

## 5. Banco de Dados e Modelos

Arquivo principal: `app/models/__init__.py`

Enums principais:

- `UserRole`: admin, almoxarifado, manipulador.
- `CaixaStatus`: em estoque, entregue, devolvida, instalada, transferida para outra empresa, cancelada.
- `HidrometroStatus`: em estoque, com instalador, instalado.
- `MovimentacaoTipo`: entrada, saida, devolucao, baixa.
- `SolicitacaoStatus`: pendente, separada, entregue, cancelada.
- `FeedbackStatus`: novo, em analise, resolvido.
- `CarcacaTipoMovimento`: devolucao do instalador, recolhimento Sabesp, ajuste admin.

Modelos principais:

- `Usuario`: usuarios, perfil, senha, ativo, token version e ultimo acesso.
- `BoxRuleConfig`: regra historica de quantidade de hidrometros por caixa.
- `SystemFlag`: flags operacionais, como modo leitura.
- `Instalador`: cadastro de instaladores.
- `CaixaHidrometro`: caixa fisica, status, regra historica e quantidade esperada.
- `Hidrometro`: hidrometro individual, caixa, status, instalador atual e baixa.
- `InstalacaoHidrometro`: evento real de instalacao usado para produtividade.
- `TipoPeca`: cadastro de tipos de pecas.
- `EstoquePeca`: saldo fisico de pecas no almoxarifado.
- `InstaladorPeca`: pecas sob responsabilidade atual de instalador.
- `Solicitacao`: pedido de materiais feito pelo manipulador.
- `SolicitacaoItemCaixa`: caixas solicitadas.
- `SolicitacaoItemPeca`: pecas solicitadas.
- `MovimentacaoMaterial`: historico de movimentacoes de caixas e hidrometros.
- `MovimentacaoPeca`: historico de movimentacoes de pecas.
- `CarcacaMovimentacao`: entradas e saidas de carcacas.
- `TransferenciaEmpresa`: cabecalho de transferencia para outra empresa.
- `TransferenciaEmpresaCaixa`: caixas transferidas.
- `TransferenciaEmpresaPeca`: pecas transferidas.
- `FeedbackOperacional`: canal interno de feedback.
- `ConferenciaPecas`: conferencia informativa de pecas por instalador.
- `ConferenciaItem`: item da conferencia.
- `ConferenciaInstaladorPeca`: registro normalizado da quantidade informada pelo manipulador.
- `AuditoriaLog`: trilha de auditoria completa.

Constraints e indices relevantes:

- estoque de pecas nao pode ser negativo;
- quantidade maxima de estoque precisa ser positiva;
- quantidade de movimentacao de peca precisa ser positiva;
- quantidade esperada da caixa precisa ser positiva;
- regra de caixa precisa ter quantidade positiva;
- hidrometro tem numero de serie unico;
- caixa tem serial e numero interno unicos;
- transferencia externa nao permite duplicar caixa transferida;
- instalacao de hidrometro e unica por hidrometro.

## 6. Migracoes Alembic

Diretorio: `alembic/versions/`

Migracoes existentes:

- `20260424_0001_enterprise_hardening.py`: reforcos iniciais empresariais.
- `20260424_0002_operational_safety.py`: seguranca operacional.
- `20260424_0003_operational_event_classification.py`: classificacao de eventos.
- `20260426_0004_audit_traceability.py`: rastreabilidade de IP e dispositivo.
- `20260426_0005_operational_expansion.py`: produtividade, carcacas, transferencias e novos status.

Comandos:

```bash
alembic upgrade head
alembic revision --autogenerate -m "descricao"
alembic downgrade -1
```

Regra de producao:

- nunca depender de alteracao automatica de schema em runtime;
- sempre aplicar migracoes em homologacao antes de producao;
- antes de migrar, gerar backup;
- apos migrar, validar login, dashboard, estoque, caixas, baixas, relatorios e monitoramento.

## 7. Routers

### `app/routers/auth.py`

Funcoes:

- login;
- logout;
- troca de senha;
- auditoria de login com sucesso, falha, bloqueio e usuario inativo;
- atualizacao transparente de hash legado quando necessario;
- cookie de autenticacao.

Rotas principais:

- `GET /auth/login`
- `POST /auth/login`
- `GET /auth/trocar-senha`
- `POST /auth/trocar-senha`
- `GET|POST /auth/logout`

### `app/routers/dashboard.py`

Funcoes:

- dashboard;
- modo avancado;
- feedback operacional;
- termo de responsabilidade;
- produtividade;
- relatorios;
- exportacoes XLSX.

Rotas importantes:

- `GET /dashboard`
- `GET /feedback`
- `POST /feedback`
- `GET /produtividade`
- `GET /produtividade/exportar.xlsx`
- `GET /relatorios`
- `GET /relatorios/estoque`
- `GET /relatorios/instaladores`
- `GET /relatorios/baixas`
- `GET /relatorios/baixas/exportar.xlsx`
- `GET /relatorios/caixas-completo`
- `GET /relatorios/caixas-completo/exportar.xlsx`
- `GET /relatorios/movimentacoes`
- `GET /relatorios/divergencias`

Observacao:

- relatorios sensiveis sao restritos ao ADMIN;
- produtividade e visivel para MANIPULADOR em modo leitura;
- exportacao oficial de produtividade e restrita ao ADMIN.

### `app/routers/admin.py`

Funcoes:

- usuarios;
- instaladores;
- tipos de pecas;
- regras de caixa;
- auditoria;
- monitoramento;
- diagnostico;
- backup;
- modo leitura;
- feedbacks;
- exclusoes controladas;
- reversoes;
- overrides administrativos.

Rotas importantes:

- `/admin/usuarios`
- `/admin/instaladores`
- `/admin/pecas`
- `/admin/regras-caixa`
- `/admin/monitoramento`
- `/admin/monitoramento/backup`
- `/admin/monitoramento/modo-leitura`
- `/admin/diagnostico`
- `/admin/diagnostico/exportar.xlsx`
- `/admin/exportacao-periodica.zip`
- `/admin/reversoes`
- `/admin/auditoria`
- `/admin/feedbacks`
- `/admin/limpeza/caixas/{id}`
- `/admin/limpeza/solicitacoes/{id}`
- `/admin/limpeza/instaladores/{id}`
- `/admin/limpeza/usuarios/{id}`
- `/admin/hidrometros/{id}/override-baixa`
- `/admin/hidrometros/{id}/reverter-baixa`
- `/admin/solicitacoes/{id}/reverter-entrega`
- `/admin/pecas/movimentacoes/{id}/estornar-entrada`
- `/admin/conferencias/{id}/reverter`

Regra:

- qualquer correcao excepcional deve ser feita pelo ADMIN e auditada.

### `app/routers/almoxarifado.py`

Funcoes:

- cadastrar caixas;
- editar caixas conforme permissao;
- listar e exportar caixas;
- controlar estoque de pecas;
- registrar entrada de pecas;
- separar solicitacoes;
- confirmar entregas;
- registrar carcacas;
- registrar transferencias para outra empresa.

Rotas importantes:

- `/almoxarifado/caixas`
- `/almoxarifado/caixas/exportar.xlsx`
- `/almoxarifado/caixas/nova`
- `/almoxarifado/caixas/{id}`
- `/almoxarifado/caixas/{id}/editar`
- `/almoxarifado/estoque`
- `/almoxarifado/estoque/exportar.xlsx`
- `/almoxarifado/pecas/entrada`
- `/almoxarifado/solicitacoes`
- `/almoxarifado/solicitacoes/{id}/separar`
- `/almoxarifado/solicitacoes/{id}/entregar`
- `/almoxarifado/carcacas`
- `/almoxarifado/carcacas/devolucao`
- `/almoxarifado/carcacas/recolhimento`
- `/almoxarifado/carcacas/exportar.xlsx`
- `/almoxarifado/transferencias`
- `/almoxarifado/transferencias/nova`
- `/almoxarifado/transferencias/exportar.xlsx`

Regra:

- estoque fisico so deve ser alterado por fluxo oficial de almoxarifado ou ADMIN.

### `app/routers/manipulador.py`

Funcoes:

- criar e acompanhar solicitacoes;
- cancelar solicitacoes quando permitido;
- consultar instaladores;
- baixar hidrometros instalados;
- registrar conferencia informativa de pecas;
- rastrear hidrometro.

Rotas importantes:

- `/manipulador/solicitacoes`
- `/manipulador/solicitacoes/nova`
- `/manipulador/solicitacoes/{id}/cancelar`
- `/manipulador/instaladores`
- `/manipulador/instaladores/exportar.xlsx`
- `/manipulador/instaladores/{id}`
- `/manipulador/baixa-hidrometro`
- `/manipulador/baixa-hidrometro/buscar`
- `/manipulador/baixa-hidrometro/confirmar`
- `/manipulador/conferencia`
- `/manipulador/conferencia/{id}`
- `/manipulador/rastrear`

Regra:

- conferencia do manipulador e informativa e nao altera estoque de pecas.

## 8. Services

### Auditoria

Arquivo: `app/services/auditoria.py`

Responsabilidades:

- criar `AuditoriaLog`;
- capturar usuario, perfil, IP, IP de conexao, headers de proxy, user agent, resultado, severidade, categoria e dados antes/depois;
- classificar evento como `NORMAL`, `SUSPEITO` ou `CRITICO`.

Regra:

- operacoes criticas devem falhar se a auditoria falhar.

### Estoque

Arquivo: `app/services/estoque.py`

Responsabilidades:

- listar caixas disponiveis;
- validar estoque de pecas;
- separar solicitacao;
- confirmar entrega;
- criar movimentacoes de material e pecas;
- reverter entrega quando ADMIN autorizar;
- impedir estoque negativo e caixa fora de contexto.

### Hidrometros

Arquivo: `app/services/hidrometros.py`

Responsabilidades:

- validar baixa;
- aplicar baixa;
- impedir baixa sem contexto valido;
- registrar evento de instalacao;
- finalizar caixa automaticamente quando todos os hidrometros esperados estiverem instalados;
- reverter baixa por ADMIN.

### Operacoes Empresariais

Arquivo: `app/services/operacoes_empresariais.py`

Responsabilidades:

- calcular saldo de carcacas;
- registrar devolucao de carcacas;
- registrar recolhimento Sabesp;
- impedir saldo negativo de carcacas;
- registrar transferencia para outra empresa;
- retirar caixa e pecas do estoque disponivel sem apagar historico.

### Regras de Caixa

Arquivo: `app/services/regras_caixa.py`

Responsabilidades:

- obter regra ativa;
- validar quantidade;
- alterar regra somente por ADMIN;
- manter caixas antigas com regra historica;
- calcular quantidade esperada de cada caixa.

### Backup

Arquivo: `app/services/backup.py`

Responsabilidades:

- gerar backup local do banco;
- usar `pg_dump` quando disponivel;
- usar fallback via Docker quando configurado;
- validar arquivo gerado;
- aplicar retencao;
- expor status de backup e restore.

### Integridade

Arquivo: `app/services/integridade.py`

Responsabilidades:

- gerar diagnostico operacional;
- detectar caixas incompletas;
- detectar hidrometros sem caixa;
- detectar numeros duplicados;
- detectar estoque negativo;
- detectar movimentacao sem usuario;
- contar eventos suspeitos e criticos;
- listar bloqueadores criticos.

### Protecao Operacional

Arquivo: `app/services/protecao_operacional.py`

Responsabilidades:

- detectar sequencias suspeitas de operacoes;
- bloquear tentativas repetidas quando necessario;
- auditar requisicoes bloqueadas;
- evitar abuso por URL/manual request.

### Modo Leitura

Arquivos:

- `app/services/operational_mode.py`
- `app/services/readonly_middleware.py`

Responsabilidades:

- ativar/desativar modo seguro;
- permitir consulta e relatorios;
- bloquear operacoes criticas durante emergencia;
- auditar ativacao e desativacao.

### Runtime Hardening

Arquivo: `app/services/runtime_hardening.py`

Responsabilidades:

- validar seguranca em startup;
- aplicar headers de seguranca;
- controlar CSRF em POST;
- configurar cookies e protecoes em runtime.

### Exportacao

Arquivos:

- `app/services/exportacao.py`
- `app/services/exportacao_periodica.py`

Responsabilidades:

- gerar XLSX;
- incluir marca oficial do sistema nos relatorios;
- gerar pacote ZIP administrativo;
- exportar dados operacionais para conferencia e auditoria.

## 9. Fluxos de Negocio

### Caixa de Hidrometros

Fluxo padrao:

1. ALMOXARIFADO cadastra caixa.
2. Sistema aplica regra ativa de quantidade esperada.
3. Caixa entra em `EM_ESTOQUE`.
4. Caixa pode ser separada para solicitacao valida.
5. Caixa pode ser entregue a instalador.
6. Hidrometros sao baixados conforme instalacao.
7. Quando todos os hidrometros esperados forem instalados, caixa fica `INSTALADA`.
8. Caixa instalada sai da lista ativa do instalador, mas fica no historico e relatorios.

Bloqueios:

- caixa incompleta nao pode seguir fluxo normal;
- caixa transferida nao pode ser separada ou entregue;
- caixa instalada nao deve voltar ao fluxo comum sem acao administrativa.

### Solicitacao

Fluxo padrao:

1. MANIPULADOR cria solicitacao para instalador.
2. ALMOXARIFADO separa materiais.
3. ALMOXARIFADO entrega materiais.
4. MANIPULADOR registra baixas conforme instalacao.

Status:

- `PENDENTE`
- `SEPARADA`
- `ENTREGUE`
- `CANCELADA`

### Pecas

Fluxos oficiais que alteram estoque:

- entrada de pecas;
- saida por entrega de solicitacao;
- transferencia para outra empresa;
- estorno/correcao administrativa autorizada.

Fluxo que nao altera estoque:

- conferencia informativa feita pelo MANIPULADOR.

### Baixa de Hidrometro

Regras:

- hidrometro precisa ter sido entregue ao instalador;
- baixa duplicada e bloqueada;
- baixa gera movimentacao de material;
- baixa gera evento de produtividade;
- baixa pode finalizar caixa automaticamente.

### Conferencia do Manipulador

Regra critica:

- nao altera estoque real;
- nao altera saldo fisico do almoxarifado;
- registra informacao declarada;
- ajuda visualizacao operacional na aba de instaladores;
- divergencias devem ser avaliadas pelo ADMIN/gestao.

### Carcacas

Entrada:

- devolucao de carcacas pelo instalador aumenta saldo.

Saida:

- recolhimento pela Sabesp reduz saldo.

Bloqueio:

- saldo nunca pode ficar negativo.

### Transferencia para Outra Empresa

Regras:

- caixa transferida sai do estoque disponivel;
- peca transferida baixa estoque;
- historico permanece;
- relatorio completo mostra status transferido;
- transferencia duplicada de caixa e bloqueada.

## 10. Auditoria

Auditoria deve registrar:

- login;
- logout;
- login falho;
- usuario inativo;
- acesso negado;
- erros criticos;
- backups;
- alteracao de regra de caixa;
- cadastros;
- edicoes;
- exclusoes logicas;
- reversoes;
- overrides;
- movimentacoes de pecas;
- movimentacoes de caixas;
- baixas;
- conferencias;
- transferencias;
- carcacas;
- feedbacks.

Campos esperados:

- usuario;
- email;
- perfil;
- acao;
- tabela/entidade;
- registro;
- descricao;
- severidade;
- categoria;
- resultado;
- dados antes;
- dados depois;
- IP interpretado;
- IP de conexao;
- `X-Forwarded-For`;
- `X-Real-IP`;
- user agent;
- data/hora.

## 11. Segurança

Controles implementados:

- senha forte;
- hash Argon2;
- JWT com expiracao;
- token version para invalidar sessoes;
- CSRF em formularios POST;
- cookies com configuracao por ambiente;
- rate limit de login;
- Redis para rate limit compartilhado;
- bloqueio por tentativas invalidas;
- auditoria de acesso negado;
- headers de seguranca;
- validacao de startup em producao;
- restricao de relatorios sensiveis;
- modo leitura de emergencia;
- handler global contra pagina em branco.

Variaveis importantes:

- `APP_ENV`
- `DATABASE_URL`
- `SECRET_KEY`
- `ACCESS_TOKEN_EXPIRE_MINUTES`
- `COOKIE_SECURE`
- `COOKIE_SAMESITE`
- `FORCE_HTTPS`
- `ALLOWED_HOSTS`
- `REDIS_URL`
- `BACKUP_DIR`
- `BACKUP_RETENTION_DAYS`
- `PG_DUMP_BIN`
- `PG_DUMP_DOCKER_CONTAINER`
- `LOG_DIR`

Regras de producao:

- usar HTTPS por proxy;
- configurar `COOKIE_SECURE=True`;
- usar `APP_ENV=production`;
- nao usar `SECRET_KEY` padrao;
- restringir `ALLOWED_HOSTS`;
- manter Redis ativo;
- manter backups fora do volume efemero;
- validar restore em ambiente separado.

## 12. Backup, Restore e Monitoramento

Backup:

- botao no monitoramento para ADMIN baixar backup;
- backup automatico via container `db-backup`;
- fallback de backup via Docker quando `pg_dump` nao estiver no app;
- validacao de arquivo nao vazio e formato correto;
- retencao configuravel.

Restore:

- restore nao deve ser feito diretamente no sistema em producao sem plano;
- validar restore em ambiente separado;
- registrar data do ultimo restore validado;
- conferir integridade apos restore.

Monitoramento ADMIN:

- ultimos backups;
- status de restore;
- erros criticos;
- tentativas de login;
- caixas inconsistentes;
- estoque critico;
- eventos suspeitos;
- modo leitura;
- diagnostico operacional.

## 13. UX e Interface

Principios:

- menos texto em tela, mais acao contextual;
- botao aparece quando a acao faz sentido;
- acao invalida deve ser bloqueada;
- mensagem de erro deve ser clara;
- tela deve funcionar em notebook, monitor, tablet e celular;
- logo/GMF leva ao dashboard;
- seta de voltar ajuda navegacao;
- feedback sempre disponivel para reportar erro.

Templates principais:

- `app/templates/shared/base.html`: layout geral, menu, usuario e navegacao.
- `app/templates/shared/dashboard.html`: painel operacional.
- `app/templates/shared/feedback.html`: canal de feedback.
- `app/templates/shared/produtividade.html`: produtividade.
- `app/templates/shared/relatorios.html`: central de relatorios.
- `app/templates/shared/erro.html`: erro amigavel.
- `app/templates/admin/*.html`: telas administrativas.
- `app/templates/almoxarifado/*.html`: telas de almoxarifado.
- `app/templates/manipulador/*.html`: telas de manipulador.

## 14. Testes

Arquivo principal: `tests/test_enterprise_ready.py`

Coberturas relevantes:

- edicao e exclusao controlada de caixas;
- regra dinamica de quantidade por caixa;
- ADMIN unico ativo;
- exclusao de usuarios com auditoria;
- rollback de solicitacao entregue;
- visibilidade de relatorios por perfil;
- relatorio de baixas e XLSX;
- reversao de baixa;
- bloqueio de baixa sem contexto;
- reversao de conferencia;
- reversao de entrega;
- feedback;
- login lockout;
- troca de senha;
- backup;
- auditoria com IP e user agent;
- handler global de excecao;
- checklist e diagnostico;
- concorrencia em PostgreSQL;
- protecao operacional;
- CSRF;
- caixa incompleta bloqueada;
- produtividade;
- carcacas;
- transferencia para outra empresa.

Comando:

```bash
venv\Scripts\python.exe -m unittest tests.test_enterprise_ready
```

Em Linux/container:

```bash
python -m unittest tests.test_enterprise_ready
```

## 15. Docker

Servicos:

- `gmf_hidrometros_app`: aplicacao.
- `gmf_hidrometros_db`: PostgreSQL.
- `gmf_hidrometros_redis`: Redis.
- `gmf_hidrometros_backup`: backup automatico.

Comandos:

```bash
docker compose up -d --build
docker compose ps
docker compose logs app
docker compose restart app
docker compose down
```

Healthcheck:

```bash
curl http://127.0.0.1:8000/healthz
```

Resposta esperada:

```json
{"status":"ok","database":"ok"}
```

## 16. Como Evoluir o Sistema com Seguranca

Antes de alterar:

- entender o fluxo existente;
- localizar router, service, model, template e teste relacionado;
- verificar permissao por perfil;
- verificar auditoria;
- verificar impacto em estoque;
- verificar impacto em relatorios.

Ao implementar:

- alterar de forma incremental;
- colocar regra de negocio em service quando possivel;
- validar frontend e backend;
- nao confiar apenas na tela;
- exigir POST para acao que altera dados;
- manter CSRF;
- auditar dados antes e depois;
- criar/atualizar teste.

Depois de implementar:

- rodar testes;
- validar Docker;
- validar migracao;
- testar no navegador;
- revisar logs;
- registrar qualquer comportamento estranho via Feedback no proprio sistema.

## 17. Pontos que Nunca Devem Ser Quebrados

- movimentacoes operacionais nao devem ser apagadas;
- historico deve permanecer consultavel;
- conferencia do manipulador nao altera estoque real;
- baixa de hidrometro exige contexto valido;
- estoque nao pode ficar negativo;
- caixa transferida nao pode voltar ao estoque disponivel sem acao administrativa;
- relatorios sensiveis sao do ADMIN;
- ADMIN precisa manter dominio total para corrigir excecoes;
- usuario comum nao deve conseguir burlar regra por URL;
- erro de sistema deve gerar pagina amigavel e auditoria critica.

## 18. Procedimento Tecnico em Caso de Erro

Quando houver erro reportado:

1. pedir que o usuario registre o erro em **Feedback** informando tela, horario, acao e impacto;
2. ADMIN consulta `Feedbacks`;
3. ADMIN consulta `Monitoramento`;
4. ADMIN consulta `Auditoria`;
5. ADMIN verifica logs em `logs/app.log`;
6. se houver risco operacional, ativar modo leitura;
7. se houver impacto em dados, gerar backup antes de corrigir;
8. aplicar correcao administrativa pelo sistema quando possivel;
9. se precisar alterar codigo, testar em homologacao antes de producao.

