# Plano de Contingencia

Se o sistema ficar indisponivel:

1. Pausar novas movimentacoes e avisar o ADMIN.
2. Registrar temporariamente em planilha: data, usuario, instalador, caixa, hidrometro, peca, quantidade e motivo.
3. Verificar `/healthz`, logs persistentes e status do banco.
4. Confirmar existencia do ultimo backup.
5. Restaurar backup em ambiente separado antes de qualquer acao no banco real.
6. Se necessario, ativar modo leitura no monitoramento.
7. Apos retorno, lancar as movimentacoes temporarias no sistema com observacao de contingencia.
8. Conferir auditoria e diagnostico operacional antes de liberar a operacao.
