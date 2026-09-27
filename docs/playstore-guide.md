# Coleta de avaliações da Google Play

Execute os comandos a partir da raiz do projeto, com o ambiente indicado no [README](../README.md). Bancos, IDs Android, datas iniciais e parâmetros ficam em [`config.yaml`](../config.yaml).

```bash
venv/bin/python -m scripts.collect.google_play --bank nubank
```

Use `--bank nubank itau` para uma seleção ou `--all` para os 11 bancos. O coletor consulta a ordenação por avaliações mais recentes, grava checkpoints para retomada e para quando encontra avaliação anterior à data inicial configurada. A base por banco fica em `data/raw/google_play/<banco>/reviews_raw.parquet`. Um resultado novo é combinado por `id_review` com a base existente; antes de substituí-la, o coletor copia a versão anterior para `data/runs/google_play/snapshots/<banco>/`. Relatórios de cada execução ficam em `data/runs/google_play/reports/`.

Quando a paginação termina sem confirmar que chegou à data inicial, o coletor registra **cobertura não confirmada** e preserva a base anterior. A data mais antiga presente no Parquet é uma observação sobre os dados guardados; sozinha, não prova cobertura integral entre essa data e hoje. Antes de interpretar uma ausência no acervo, confira o status e as datas do relatório em `data/runs/google_play/reports/`.

O [manifesto do acervo](../data/README.md) registra as bases locais sem versioná-las no Git. Consultas sobre temas específicos usam essas bases, mas seus critérios e conclusões são documentados na pesquisa correspondente em `private/research/`.

## Registro das primeiras coletas

As anotações iniciais de terminal registraram estas duas rodadas:

| Indicador | Nubank | Itaú |
|---|---:|---:|
| Avaliações coletadas | 207.278 | 90.146 |
| Lotes processados | 415 | 181 |
| Checkpoints salvos | 83 | 36 |
| Duração informada | 20,1 min | 8,3 min |
| Data da avaliação que acionou a parada | 31/10/2024 | 31/12/2024 |
| Erros registrados | Nenhum | Nenhum |

Ambas as rodadas foram dadas como concluídas. A avaliação que acionou a parada era de um dia antes do início configurado para cada coleta. As anotações não traziam a data de execução e não demonstram cobertura integral dos períodos.
