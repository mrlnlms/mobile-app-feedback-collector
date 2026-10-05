# Coleta de avaliações da Google Play

Execute os comandos a partir da raiz do projeto, com o ambiente indicado no [README](../README.md). Bancos, IDs Android, datas iniciais e parâmetros ficam em [`config.yaml`](../config.yaml).

```bash
venv/bin/python -m scripts.collect.google_play --bank nubank
```

Use `--bank nubank itau` para uma seleção ou `--all` para os 11 bancos. O coletor consulta a ordenação por avaliações mais recentes. Quando já existe uma base, ele calcula a fronteira como **a data e hora da avaliação mais recente preservada menos 24 horas**. Por exemplo: uma base cuja última avaliação é de 22/09 às 16:38 será atualizada até 21/09 às 16:38, incluindo os registros nessa fronteira. Sem base existente, a coleta usa a data inicial do `config.yaml`.

A coleta para após um lote contendo avaliação anterior à fronteira; todo esse lote é examinado para incluir avaliações mais recentes que apareçam depois dela no mesmo lote. A sobreposição permite reencontrar registros e reduz o risco de perder avaliações que apareceram com atraso, mas não garante cobertura integral nem identifica todas as edições antigas.

Para conferir as fronteiras sem acessar a loja ou gravar arquivos:

```bash
venv/bin/python -m scripts.collect.google_play --all --dry-run
```

## Preparação local e publicação

A base oficial fica em `data/raw/google_play/<banco>/reviews_raw.parquet`, acessada pelo symlink `data/raw` para `private/data/raw/` no Drive. Checkpoints e tokens de retomada ficam em `.runtime/google_play/checkpoints/`; a preparação do Parquet completo fica em `.runtime/google_play/staging/<banco>/`. Esses diretórios locais são ignorados pelo Git e não ficam no Drive. A coleta do mesmo banco tem um bloqueio local contra execuções simultâneas nesta máquina.

Depois de alcançar a fronteira, o coletor combina os registros novos com **todo o histórico**, deduplica por `id_review` e prioriza a versão recém-coletada dos IDs repetidos. Valida IDs, datas e notas, grava o Parquet localmente e copia uma versão temporária para o destino oficial. A cópia é conferida por SHA-256. A base anterior é preservada e conferida em `data/runs/google_play/snapshots/<banco>/`, antes da substituição da base oficial. Se o hash da base oficial mudar durante a execução, a publicação é cancelada.

Quando a paginação termina sem confirmar que chegou à fronteira, o coletor registra **cobertura não confirmada** e preserva a base e o estado anteriores. Falhas antes da substituição também preservam a base. Checkpoints completos permitem repetir a publicação sem baixar de novo; checkpoints parciais retomam a paginação. Eles registram a identidade do app, fronteira, configuração e hash da base; uma identidade divergente interrompe a retomada sem apagar o checkpoint. Não iniciar outra máquina escrevendo na mesma base ao mesmo tempo: o bloqueio é local.

## Estado automático por banco

Após publicar, o coletor grava `data/runs/google_play/state/<banco>.json` com o SHA-256 da base, a avaliação mais recente preservada, o horário da última coleta publicada, a fronteira usada e a quantidade de registros na base. A data da execução e a data da avaliação são campos diferentes: a segunda determina até onde buscar na próxima rodada. Quando ainda não há estado, a referência é lida dos Parquets existentes. Se o estado faltar, estiver inválido ou não corresponder ao hash da base, a referência é reconstruída pelo Parquet.

Se houver interrupção entre a substituição do Parquet e a gravação do estado, um recibo local em staging permite concluir a gravação na próxima execução. Uma publicação bem-sucedida limpa o checkpoint e o Parquet preparado em staging; arquivos de bloqueio podem permanecer localmente, sem dados de avaliações. Os relatórios ficam em `data/runs/google_play/reports/`; ao final do comando, o manifesto versionado `data/manifest.json` é recalculado se alguma base foi publicada. Uma falha no manifesto é informada separadamente, pois as bases já publicadas continuam válidas.

Publicação significa arquivo gravado e conferido no destino acessível pelo Mac; o comando não confirma que o aplicativo do Drive terminou o upload. A pasta do Drive deve estar disponível offline. A data mais antiga presente no Parquet é uma observação sobre os dados guardados; sozinha, não prova cobertura integral entre essa data e hoje. Antes de interpretar uma ausência no acervo, confira o status e as datas dos relatórios.

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
