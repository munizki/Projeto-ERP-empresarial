# Manual de Uso do Sistema GMF

## 1. Objetivo do Sistema

O sistema GMF controla hidrometros, caixas, pecas, instaladores, solicitacoes, baixas, produtividade, carcacas, transferencias, relatorios, auditoria e monitoramento.

O objetivo e manter a operacao simples para o usuario e segura para a empresa:

- o usuario segue o fluxo indicado;
- o sistema bloqueia acoes fora de contexto;
- o ADMIN corrige excecoes;
- toda acao relevante fica registrada;
- qualquer duvida, erro ou dificuldade deve ser enviada pela area **Feedback**.

## 2. Regra Geral de Uso

Use sempre o fluxo normal do sistema. Nao tente contornar uma regra por outra tela.

Se algo nao aparecer, estiver bloqueado ou parecer errado:

1. nao force outro caminho;
2. confira se o item esta no status correto;
3. registre um **Feedback**;
4. marque como urgente se afetar a operacao do dia, estoque, caixa, hidrometro, peca, instalador ou relatorio.

## 3. Perfis do Sistema

### ADMIN

Perfil com acesso completo.

Pode:

- cadastrar e editar usuarios;
- cadastrar e editar instaladores;
- cadastrar e editar tipos de pecas;
- alterar regra de quantidade por caixa;
- acessar monitoramento;
- acessar auditoria;
- acessar relatorios administrativos;
- executar reversoes;
- excluir ou inativar registros quando permitido;
- gerar backup;
- acompanhar feedbacks;
- corrigir excecoes operacionais.

Uso esperado:

- manter o sistema seguro;
- liberar correcoes quando houver erro real;
- acompanhar divergencias;
- revisar feedbacks;
- validar backup e monitoramento.

### MANIPULADOR

Perfil operacional ligado a solicitacoes, baixas, instaladores, conferencias e rastreamento.

Pode:

- criar solicitacoes;
- acompanhar solicitacoes;
- consultar instaladores;
- registrar baixa de hidrometro instalado;
- registrar conferencia informativa de pecas;
- rastrear hidrometro;
- visualizar produtividade em modo leitura;
- enviar feedback.

Nao deve:

- alterar estoque real de pecas pela conferencia;
- reverter movimentacoes;
- acessar relatorios sensiveis;
- corrigir erro critico por conta propria.

### ALMOXARIFADO

Perfil operacional ligado ao estoque fisico.

Pode:

- cadastrar caixas;
- consultar caixas;
- registrar entrada de pecas;
- acompanhar estoque;
- separar solicitacoes;
- confirmar entrega de materiais;
- registrar carcacas devolvidas;
- registrar recolhimento de carcacas pela Sabesp;
- registrar transferencia para outra empresa quando habilitado no fluxo;
- enviar feedback.

Nao deve:

- alterar regra global de caixa;
- acessar auditoria completa;
- executar reversoes administrativas sem ADMIN.

## 4. Entrada no Sistema

### Login

Como usar:

1. acesse a tela de login;
2. informe email e senha;
3. clique em entrar.

Se der erro:

- confira email e senha;
- aguarde se houver bloqueio por tentativas repetidas;
- registre Feedback se o acesso estiver correto e mesmo assim falhar;
- se for urgente, avise o ADMIN.

### Trocar senha

Como usar:

1. abra a area de conta no menu;
2. clique em trocar senha;
3. informe senha atual e nova senha;
4. confirme.

Boa pratica:

- nao compartilhe senha;
- use senha forte;
- troque a senha se suspeitar de uso indevido.

Se houver erro:

- registre Feedback informando o horario e a mensagem exibida.

### Sair do sistema

Use o botao **Sair do sistema** no rodape do menu lateral.

Boa pratica:

- sempre saia ao terminar o uso em computador compartilhado.

## 5. Dashboard

O Dashboard e a entrada principal do sistema.

Ele mostra:

- indicadores operacionais;
- atalhos por perfil;
- alertas de estoque ou divergencia;
- caminhos rapidos para as telas principais.

Como usar:

1. clique nos cards ou atalhos da tela;
2. use o menu lateral para navegar;
3. clique em GMF para voltar ao Dashboard;
4. use a seta de voltar quando estiver dentro de uma tela.

Se algum indicador parecer incorreto:

- registre Feedback com print ou descricao;
- marque como urgente se afetar estoque, caixa ou baixa.

## 6. Feedback

O Feedback e o canal oficial para reportar:

- erro visual;
- erro de sistema;
- dificuldade de uso;
- divergencia de informacao;
- sugestao de melhoria;
- bloqueio indevido;
- comportamento estranho.

Como usar:

1. clique em **Feedback** no menu;
2. informe titulo curto;
3. descreva o que aconteceu;
4. marque urgente se afetar a operacao;
5. envie.

O sistema identifica a area conforme seu perfil sempre que aplicavel.

Quando marcar urgente:

- nao consegue concluir baixa;
- estoque ou peca ficou divergente;
- caixa sumiu ou apareceu indevidamente;
- relatorio oficial esta incorreto;
- nao consegue acessar tela essencial;
- erro impede entrega, separacao ou atendimento.

Boa descricao de Feedback:

- tela usada;
- horario aproximado;
- acao feita;
- numero da caixa, hidrometro, solicitacao ou instalador;
- mensagem exibida;
- impacto na operacao.

## 7. Fluxo Principal da Operacao

Fluxo recomendado:

1. ADMIN cadastra usuarios, instaladores e tipos de pecas.
2. ADMIN valida a regra de quantidade por caixa.
3. ALMOXARIFADO cadastra caixas e hidrometros.
4. ALMOXARIFADO registra entrada de pecas.
5. MANIPULADOR cria solicitacao para instalador.
6. ALMOXARIFADO separa solicitacao.
7. ALMOXARIFADO entrega materiais.
8. MANIPULADOR baixa hidrometros instalados.
9. Sistema finaliza caixa automaticamente quando todos os hidrometros forem instalados.
10. ADMIN acompanha produtividade, auditoria, diagnostico e relatorios.

Se qualquer etapa estiver bloqueada:

- nao pule a etapa;
- leia a mensagem exibida;
- envie Feedback se a mensagem nao resolver.

## 8. Funcoes do MANIPULADOR

### Nova Solicitacao

Objetivo:

- pedir caixas e pecas para um instalador.

Como usar:

1. acesse **Nova Solicitacao**;
2. selecione instalador;
3. escolha caixas e pecas conforme disponibilidade;
4. revise os dados;
5. confirme.

O sistema bloqueia:

- item sem estoque;
- caixa fora de contexto;
- dados obrigatorios ausentes;
- solicitacao invalida.

Se algo estiver indisponivel:

- confirme com o ALMOXARIFADO;
- registre Feedback se o sistema parecer divergente.

### Solicitacoes

Objetivo:

- acompanhar solicitacoes criadas.

Status comuns:

- pendente: aguardando separacao;
- separada: almoxarifado ja separou;
- entregue: material entregue ao instalador;
- cancelada: solicitacao encerrada sem seguir fluxo.

Como usar:

1. abra a lista;
2. filtre ou localize a solicitacao;
3. confira status;
4. use as acoes disponiveis.

Se nao houver botao esperado:

- o status provavelmente nao permite aquela acao;
- envie Feedback se acreditar que o status esta errado.

### Instaladores

Objetivo:

- consultar materiais associados aos instaladores.

A tela mostra:

- caixas em andamento;
- materiais sob responsabilidade;
- dados de apoio operacional;
- historico quando disponivel.

Regra:

- caixa ja instalada nao deve ficar poluindo a lista ativa.

Se uma caixa finalizada continuar ativa:

- registre Feedback urgente.

### Baixa de Hidrometro

Objetivo:

- registrar que um hidrometro foi instalado em campo.

Como usar:

1. acesse **Baixa**;
2. informe ou localize o numero de serie;
3. confira caixa, instalador e status;
4. confirme somente se os dados estiverem corretos.

O sistema bloqueia:

- hidrometro que nao foi entregue ao instalador;
- hidrometro ja instalado;
- hidrometro sem contexto valido;
- tentativa duplicada;
- confirmacao fora do padrao.

Se houve baixa errada:

- nao tente corrigir por outra tela;
- registre Feedback urgente;
- ADMIN deve usar reversao apropriada.

### Conferencia

Objetivo:

- registrar informacao operacional sobre pecas declaradas pelo instalador.

Regra muito importante:

- a conferencia do MANIPULADOR nao altera o estoque real de pecas.

Como usar:

1. selecione instalador;
2. informe as quantidades declaradas/conferidas;
3. salve;
4. acompanhe divergencias.

Se a conferencia indicar divergencia:

- registre observacao clara;
- se afetar operacao, envie Feedback;
- ADMIN/gestao deve avaliar a correcao.

### Rastrear

Objetivo:

- consultar onde esta um hidrometro e seu historico.

Como usar:

1. informe numero de serie;
2. consulte status, caixa e movimentacoes;
3. use a informacao para orientar a operacao.

Se o hidrometro nao for encontrado:

- confira digitacao;
- confira se foi cadastrado;
- envie Feedback se deveria existir.

### Produtividade em modo leitura

Objetivo:

- permitir que o MANIPULADOR acompanhe desempenho sem alterar ou exportar dados oficiais.

Regra:

- MANIPULADOR visualiza, mas nao exporta relatorio oficial.

Se precisar de relatorio:

- solicite ao ADMIN.

## 9. Funcoes do ALMOXARIFADO

### Cadastrar Caixa

Objetivo:

- registrar caixa fisica e hidrometros.

Como usar:

1. acesse **Cadastrar Caixa**;
2. informe numero interno e serial;
3. informe os hidrometros exigidos pela regra ativa;
4. confirme.

O sistema bloqueia:

- caixa duplicada;
- hidrometro duplicado;
- quantidade incorreta;
- dado obrigatorio ausente.

Se a regra de quantidade estiver errada:

- nao cadastre caixa incompleta;
- peça ao ADMIN revisar a regra de caixa;
- registre Feedback se necessario.

### Caixas

Objetivo:

- consultar caixas em estoque, em campo, instaladas, transferidas ou canceladas conforme permissao.

Como usar:

1. abra a lista;
2. use filtros quando disponiveis;
3. acesse detalhes;
4. use exportacao quando autorizada.

Se caixa nao aparecer:

- confira status;
- confira se foi transferida ou instalada;
- registre Feedback se for divergencia.

### Estoque

Objetivo:

- acompanhar saldo de pecas.

Como usar:

1. acesse **Estoque**;
2. veja saldos e alertas;
3. use entrada de pecas para aumentar saldo;
4. use fluxos oficiais para saida.

Regra:

- estoque nao pode ficar negativo.

Se saldo parecer errado:

- nao ajuste por fora;
- registre Feedback urgente;
- ADMIN deve avaliar auditoria e movimentacoes.

### Entrada de Pecas

Objetivo:

- registrar chegada de pecas no almoxarifado.

Como usar:

1. selecione tipo de peca;
2. informe quantidade;
3. confirme entrada.

O sistema bloqueia:

- quantidade zero ou negativa;
- tipo de peca invalido;
- dados obrigatorios ausentes.

Se entrada foi registrada errada:

- registre Feedback urgente;
- ADMIN pode estornar entrada quando cabivel.

### Separacao e Entrega

Objetivo:

- separar materiais solicitados e confirmar entrega ao instalador.

Separacao:

1. abra solicitacao pendente;
2. confira instalador e itens;
3. selecione caixas validas;
4. confirme separacao.

Entrega:

1. abra solicitacao separada;
2. confira materiais;
3. confirme entrega.

O sistema bloqueia:

- entrega sem separacao;
- separacao duplicada;
- entrega duplicada;
- caixa incompleta;
- peca sem estoque;
- caixa ja transferida, instalada ou fora de contexto.

Se algo fisico divergir do sistema:

- pare o fluxo;
- registre Feedback urgente;
- acione ADMIN antes de movimentar.

### Carcacas

Objetivo:

- controlar carcacas de hidrometros antigos devolvidas pelos instaladores e recolhidas pela Sabesp.

Registrar devolucao:

1. clique em registrar devolucao;
2. selecione instalador;
3. informe quantidade;
4. informe data;
5. informe documento/formulario quando existir;
6. salve.

Registrar recolhimento Sabesp:

1. clique em registrar recolhimento;
2. informe quantidade;
3. informe data;
4. informe documento/protocolo quando existir;
5. salve.

O sistema bloqueia:

- saida maior que saldo;
- quantidade zero ou negativa;
- devolucao sem instalador;
- dados obrigatorios ausentes.

Se saldo de carcacas parecer errado:

- registre Feedback urgente;
- ADMIN deve comparar historico e auditoria.

### Transferencias para Outra Empresa

Objetivo:

- registrar envio de caixas, hidrometros e pecas para outra empresa sem apagar historico.

Como usar:

1. acesse **Transferencias**;
2. clique em nova transferencia;
3. informe empresa destino;
4. informe data, responsavel e documento;
5. selecione caixas;
6. selecione pecas e quantidades;
7. revise o resumo;
8. confirme.

O sistema bloqueia:

- caixa instalada;
- caixa ja transferida;
- caixa com instalador ativo sem fluxo administrativo;
- peca sem saldo;
- transferencia sem itens;
- duplicidade de caixa.

Depois de transferir:

- caixa sai do estoque disponivel;
- peca reduz saldo;
- historico permanece;
- relatorio completo mostra transferencia.

Se transferencia foi registrada errada:

- registre Feedback urgente;
- ADMIN deve avaliar reversao/correcao.

## 10. Funcoes do ADMIN

### Usuarios

Objetivo:

- controlar acesso ao sistema.

Como usar:

1. acesse **Usuarios**;
2. crie, edite ou exclua/inative usuario conforme necessidade;
3. mantenha apenas usuarios reais e ativos;
4. revise perfil antes de salvar.

Regra:

- deve existir somente um ADMIN ativo conforme decisao operacional atual.

Se usuario nao consegue acessar:

- verificar se esta ativo;
- verificar perfil;
- verificar auditoria de login;
- pedir Feedback com horario e email usado.

### Instaladores

Objetivo:

- cadastrar e manter instaladores.

Como usar:

1. acesse **Instaladores**;
2. crie ou edite cadastro;
3. confira CPF e matricula;
4. inative/exclua somente quando permitido.

Se instalador tem historico:

- prefira inativar em vez de apagar fisicamente;
- mantenha rastreabilidade.

### Tipos de Pecas

Objetivo:

- cadastrar tipos de pecas usadas no estoque.

Como usar:

1. acesse **Tipos de Pecas**;
2. crie ou edite tipo;
3. configure unidade e minimo;
4. salve.

Se peca ja tem movimentacao:

- evite apagar;
- inative ou ajuste com cuidado.

### Regra de Caixa

Objetivo:

- alterar quantidade de hidrometros esperada por caixa.

Como usar:

1. acesse **Regra de Caixa**;
2. informe nova quantidade;
3. informe motivo;
4. confirme explicitamente.

Regra:

- caixas antigas mantem regra antiga;
- caixas novas usam regra nova;
- relatorios respeitam historico.

Se regra foi alterada por engano:

- registre Feedback urgente;
- crie nova regra correta;
- confira auditoria e caixas cadastradas apos a mudanca.

### Monitoramento

Objetivo:

- acompanhar saude operacional e seguranca.

Verificar diariamente:

- ultimo backup;
- restore validado;
- erros criticos;
- tentativas de login invalidas;
- eventos suspeitos;
- estoque critico;
- caixas inconsistentes;
- usuarios ativos;
- status do modo leitura.

Backup:

1. acesse monitoramento;
2. clique no botao de backup;
3. baixe o arquivo;
4. envie para local seguro conforme politica interna;
5. valide restore em ambiente separado periodicamente.

Se backup falhar:

- registre Feedback urgente;
- verificar `pg_dump`, Docker e logs;
- nao considerar sistema pronto para producao sem backup/restore validado.

### Diagnostico

Objetivo:

- identificar divergencias e riscos operacionais.

O diagnostico mostra:

- caixas incompletas;
- hidrometros sem caixa;
- hidrometros duplicados;
- estoque negativo;
- saldo de instalador negativo;
- solicitacoes pendentes antigas;
- movimentacoes sem usuario;
- eventos suspeitos e criticos.

Se aparecer bloqueador critico:

- ativar modo leitura se houver risco;
- corrigir somente pelo fluxo ADMIN;
- registrar Feedback com prioridade.

### Auditoria

Objetivo:

- rastrear acoes do sistema.

Usar para verificar:

- quem fez;
- quando fez;
- qual acao;
- qual entidade;
- IP;
- user agent;
- dados antes e depois;
- resultado;
- severidade.

Regra:

- auditoria nao deve ser limpa pelo sistema;
- operador comum nao acessa auditoria completa.

### Reversoes

Objetivo:

- corrigir erros excepcionais sem alterar codigo.

Exemplos:

- reverter entrega;
- reverter baixa;
- estornar entrada de peca;
- reverter conferencia;
- limpar dados de teste quando permitido.

Como usar:

1. acesse **Reversoes**;
2. escolha o tipo correto;
3. confira todos os dados;
4. informe motivo obrigatorio;
5. confirme explicitamente.

Regra:

- reversao e acao critica;
- sempre auditar;
- usar somente quando houver necessidade real.

### Feedbacks

Objetivo:

- acompanhar relatos dos usuarios.

Como usar:

1. acesse **Feedbacks**;
2. priorize urgentes;
3. altere status para em analise ou resolvido;
4. use auditoria/monitoramento para investigar;
5. responda operacionalmente fora do sistema quando necessario.

Boa pratica:

- usar feedbacks como fila oficial de melhoria e suporte.

### Relatorios

Objetivo:

- visualizar dados oficiais e exportar quando necessario.

Relatorios disponiveis:

- estoque;
- instaladores;
- baixas instaladas;
- movimentacoes;
- divergencias;
- produtividade;
- caixas completas;
- carcacas;
- transferencias;
- exportacao periodica administrativa.

Regra:

- relatorios sensiveis devem ser acessados pelo ADMIN;
- relatorios oficiais exportados possuem marca do sistema com data/hora de geracao;
- se outro perfil precisar de relatorio, deve solicitar ao ADMIN.

Se relatorio parecer errado:

- confira filtros;
- compare com auditoria;
- registre Feedback urgente se afetar decisao operacional.

## 11. Relatorios e Exportacoes

### XLSX

Usado para:

- conferencia;
- envio gerencial;
- analise externa;
- backup administrativo complementar.

Todo relatorio oficial deve conter marca de geracao automatica pelo sistema.

### PDF

Quando disponivel, usar para leitura ou envio formal.

Se PDF nao estiver disponivel para uma tela:

- usar XLSX oficial;
- registrar Feedback se PDF for necessario para a rotina.

### ZIP Administrativo

Uso:

- exportacao consolidada para ADMIN.

Regra:

- nao substitui backup tecnico do banco;
- serve como apoio administrativo e conferencia.

## 12. Mensagens e Bloqueios

O sistema foi desenhado para bloquear erro antes que ele gere problema.

Quando um botao nao aparecer:

- provavelmente o item nao esta no status correto;
- o perfil pode nao ter permissao;
- a acao pode ser critica e exigir ADMIN.

Quando uma acao for bloqueada:

- leia a mensagem;
- nao tente por outra tela;
- envie Feedback se a regra parecer incorreta.

## 13. Modo Leitura

Objetivo:

- proteger a operacao em emergencia.

Quando ativado:

- consultas continuam liberadas;
- relatorios continuam disponiveis conforme perfil;
- novas movimentacoes criticas ficam bloqueadas.

Quando usar:

- suspeita de divergencia grave;
- falha de banco;
- falha de auditoria;
- erro critico recorrente;
- necessidade de congelar operacao para analise.

Somente ADMIN deve ativar/desativar.

## 14. Procedimentos Paliativos em Caso de Problema

### Sistema fora do ar

Fazer:

1. nao registrar dados por fora sem autorizacao;
2. avisar ADMIN;
3. registrar Feedback quando o sistema voltar;
4. seguir plano de contingencia manual se ja estiver aprovado internamente.

### Estoque divergente

Fazer:

1. parar movimentacoes do item se houver risco;
2. registrar Feedback urgente;
3. ADMIN verifica auditoria e diagnostico;
4. ADMIN corrige por fluxo administrativo.

### Baixa feita errada

Fazer:

1. nao baixar novamente;
2. registrar Feedback urgente;
3. ADMIN usa reversao de baixa se cabivel.

### Solicitacao errada

Fazer:

1. se ainda pendente, cancelar quando permitido;
2. se ja separada/entregue, acionar ADMIN;
3. registrar Feedback urgente.

### Caixa cadastrada errada

Fazer:

1. se nao teve movimentacao, ADMIN pode limpar/excluir conforme regra;
2. se teve movimentacao, manter historico e inativar/corrigir;
3. registrar Feedback com numero da caixa.

### Relatorio incorreto

Fazer:

1. conferir filtros;
2. conferir periodo;
3. registrar Feedback;
4. ADMIN compara com auditoria.

### Usuario sem acesso

Fazer:

1. conferir email;
2. verificar se senha foi digitada corretamente;
3. aguardar se houve bloqueio por tentativas;
4. enviar Feedback ou acionar ADMIN.

## 15. Boas Praticas por Usuario

Todos os usuarios:

- nao compartilhar senha;
- conferir dados antes de confirmar;
- nao tentar contornar bloqueios;
- usar Feedback para erro e duvida;
- sair do sistema ao finalizar;
- nao usar relatorio antigo como base final sem conferir data.

MANIPULADOR:

- baixar hidrometro somente quando houver instalacao real;
- conferir numero de serie com cuidado;
- registrar conferencia com observacao clara;
- avisar divergencia via Feedback.

ALMOXARIFADO:

- conferir material fisico antes de separar;
- nao entregar sem validacao no sistema;
- registrar entrada de pecas no momento correto;
- registrar carcacas com documento quando existir.

ADMIN:

- revisar monitoramento diariamente;
- acompanhar feedbacks urgentes;
- validar backup e restore;
- usar reversoes com motivo claro;
- manter auditoria preservada;
- revisar permissoes de usuarios.

## 16. Implantacao com Banco Vazio

Sequencia recomendada:

1. configurar ambiente;
2. aplicar migracoes;
3. criar ADMIN inicial;
4. validar login;
5. cadastrar usuarios reais;
6. cadastrar instaladores;
7. cadastrar tipos de pecas;
8. validar regra de caixa;
9. cadastrar estoque inicial de pecas;
10. cadastrar caixas reais;
11. fazer uma solicitacao de teste controlada;
12. validar separacao, entrega e baixa;
13. validar relatorios;
14. gerar backup;
15. testar restore em ambiente separado;
16. liberar uso.

Se qualquer etapa falhar:

- nao liberar implantacao;
- registrar Feedback interno;
- corrigir antes de operar com dados reais.

## 17. Checklist Diario do ADMIN

Verificar:

- sistema acessa normalmente;
- ultimo backup foi gerado;
- restore validado esta atualizado;
- nao ha erro critico nas ultimas 24h;
- nao ha estoque negativo;
- nao ha caixa inconsistente;
- nao ha movimentacao sem usuario;
- feedbacks urgentes foram vistos;
- usuarios ativos estao corretos;
- relatorios principais abrem.

Se algo estiver errado:

- registrar Feedback urgente;
- ativar modo leitura se houver risco operacional;
- investigar auditoria e logs.

## 18. Glossario

Caixa:

- agrupamento fisico de hidrometros.

Hidrometro:

- equipamento individual controlado por numero de serie.

Solicitacao:

- pedido de materiais para instalador.

Separacao:

- etapa onde o almoxarifado reserva/prepara materiais.

Entrega:

- confirmacao de envio dos materiais ao instalador.

Baixa:

- registro de hidrometro instalado em campo.

Conferencia:

- declaracao informativa de pecas com instalador.

Carcaca:

- hidrometro antigo devolvido pelo instalador e recolhido posteriormente pela Sabesp.

Transferencia:

- envio de materiais da GMF para outra empresa.

Auditoria:

- historico rastreavel das acoes do sistema.

Feedback:

- canal oficial para erro, duvida, sugestao ou dificuldade.

## 19. Regra Final de Suporte

Sempre que encontrar erro, duvida, divergencia ou tela confusa:

1. registre no **Feedback**;
2. marque como urgente se afetar a operacao;
3. inclua o maximo de contexto operacional;
4. aguarde avaliacao do ADMIN ou responsavel.

O Feedback e o caminho correto para evitar perda de rastreabilidade, evitar correcao informal e manter o sistema evoluindo sem quebrar a operacao.

