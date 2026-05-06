# Plano de Homologacao

Antes de atualizar producao:

1. Restaurar o ultimo backup em ambiente de homologacao.
2. Executar `alembic upgrade head`.
3. Validar login do ADMIN, ALMOXARIFADO e MANIPULADOR.
4. Cadastrar caixa de teste e conferir regra de quantidade vigente.
5. Criar solicitacao, separar, entregar e registrar baixa de teste.
6. Validar estoque de pecas, relatorios, auditoria e exportacoes.
7. Rodar `python -m unittest -v tests.test_enterprise_ready`.
8. Registrar restore validado com `scripts/validate_restore.py`.
9. Liberar producao somente se nao houver bloqueador critico no diagnostico.
