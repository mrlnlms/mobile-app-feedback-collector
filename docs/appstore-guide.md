# Coleta de avaliações da App Store

O coletor consulta o RSS público da App Store brasileira. Execute os comandos a partir da raiz do projeto, com o ambiente indicado no [README](../README.md). O comando `venv/bin/python -m scripts.collect.app_store --bank nubank` coleta um banco. Use `--bank nubank itau` para uma seleção ou `--all` para os 11 bancos. O [`config.yaml`](../config.yaml) mantém os identificadores Android em `app_id` e os IDs iOS em `apple_app_id`. As datas `start_date` são compartilhadas com o pipeline Google Play.

Os dados iOS ficam em `data/raw/app_store/<banco>/reviews_raw.parquet`, com relatórios JSON em `data/runs/app_store/reports/`, checkpoints em `data/runs/app_store/checkpoints/` e versões anteriores em `data/runs/app_store/snapshots/`. Uma coleta interrompida retoma da última página salva. A base existente só é substituída após o sucesso de todos os sorts.

O coletor percorre as páginas de cada sort até o limite configurado ou até o feed terminar, sem parar por data. Depois deduplica por `id_review` e filtra pela data civil do campo `updated` do RSS. `data_avaliacao` é gravada em UTC. O RSS fornece `updated`, que pode representar uma edição da review; ele não fornece necessariamente a data original de publicação.

O Parquet mantém `id_review`, `usuario`, `nota`, `data_avaliacao`, `titulo`, `texto_avaliacao`, `versao_app`, `sorts_encontrados`, `apple_app_id`, `pais` e `plataforma`. `sorts_encontrados` registra em quais ordenações o mesmo ID apareceu; `data_avaliacao` corresponde ao `updated` do RSS. O [contrato do acervo](../data/README.md) descreve os caminhos e o manifesto local.

## Cobertura e ordenações

O padrão no YAML usa `mostrecent` e `mosthelpful`. As opções vistas na interface da App Store não implicam feeds RSS públicos equivalentes. A [documentação da Apple sobre reviews](https://developer.apple.com/app-store/ratings-and-reviews/) descreve ordenação no App Store Connect para usuários autorizados do próprio app, e não acesso público aos reviews de outros bancos.

O coletor tenta no máximo 10 páginas de até 50 reviews por sort e país. `mosthelpful` não é cronológico. O relatório distingue a data mais antiga observada em todos os sorts da data mais antiga em `mostrecent`. Se `mostrecent_alcancou_inicio` for `false`, o feed cronológico não alcançou a data inicial configurada; reviews anteriores aos últimos itens do feed podem estar ausentes. Mesmo quando for `true`, o RSS não prova cobertura integral do histórico. As avaliações escritas coletadas também não representam todas as notas da loja, pois parte das notas não contém texto.

Os IDs iOS foram conferidos pelo [iTunes Search API da Apple](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/Searching.html), usando o catálogo brasileiro. A lista de bancos e as datas vieram do `config.yaml` existente.

Para acompanhar uma coleta, leia o relatório em `data/runs/app_store/reports/` antes de interpretar as datas ou repetir o banco. As medições abaixo descrevem **somente a rodada de 24/09/2026**; uma coleta posterior precisa ser avaliada pelo seu próprio relatório.

### Cobertura observada em 24/09/2026

Na verificação do Nubank, `mostrecent` e `mosthelpful` responderam; `mostcritical`, `mostfavourable` e `mostfavorable` responderam HTTP 500. As páginas 1 a 5 do Nubank continham 50 reviews cada e a página 6 veio vazia nos dois sorts: o máximo **observado nessa consulta** foi de 250 por sort, embora a configuração permita até 10 páginas.

Foram salvas **2.861 reviews únicas** dentro dos recortes dos 11 bancos. Todos os Parquets da rodada tinham IDs únicos e datas válidas. `mostrecent` não chegou à data inicial configurada em nenhum banco; na Caixa, esse sort retornou vazio. Estes arquivos são amostras acessíveis pelo RSS público, não bases completas desde 2025 ou, no caso do Nubank, desde novembro de 2024.

| Banco | Reviews no recorte | Data mais antiga em `mostrecent` | Páginas `mostrecent` / `mosthelpful` |
|---|---:|---|---:|
| Nubank | 334 | 2026-08-26 | 5 / 5 |
| Itaú | 450 | 2026-08-12 | 9 / 0 |
| Banco do Brasil | 156 | 2026-09-22 | 3 / 2 |
| Bradesco | 258 | 2026-09-03 | 2 / 7 |
| Santander | 269 | 2026-08-08 | 4 / 2 |
| Inter | 547 | 2026-07-22 | 10 / 2 |
| C6 Bank | 240 | 2026-05-31 | 4 / 6 |
| PicPay | 200 | 2026-09-16 | 4 / 0 |
| Mercado Pago | 109 | 2026-09-08 | 2 / 1 |
| BTG Pactual | 250 | 2026-02-02 | 5 / 0 |
| Caixa | 48 | sem reviews | 0 / 2 |

Os feeds vazios do Itaú, PicPay e BTG em `mosthelpful`, e da Caixa em `mostrecent`, continuaram vazios em consultas individuais posteriores. Isso não permite concluir se a ausência é permanente ou temporária. Os relatórios JSON desta rodada ficam no acervo local em `data/runs/app_store/reports/` e não entram no Git.

## Repetir um banco sem alterar a base

Para verificar se o RSS repete o comportamento observado na Caixa, execute:

```bash
venv/bin/python -m scripts.probe.app_store --bank caixa
```

O teste usa os mesmos sorts, páginas máximas, pausas e data inicial da coleta normal, mas grava em uma pasta nova `data/runs/app_store/experiments/caixa_<data-hora>/`. Nela ficam o novo Parquet e `probe.json`, que compara páginas por sort, quantidade de reviews, IDs compartilhados, IDs exclusivos e campos alterados. O script registra o SHA-256 do Parquet original antes e depois da execução. Mesmo se os dois feeds voltarem vazios, a rodada salva um Parquet vazio no experimento e registra o resultado. O comando não usa os checkpoints canônicos nem publica em `data/raw/app_store/caixa/`.

Ao terminar, guarde o caminho `Comparação completa:` exibido no terminal. Ele identifica a rodada específica para análise posterior.

### Repetição isolada da Caixa em 24/09/2026

A segunda consulta ocorreu cerca de 36 minutos após a primeira, com a mesma configuração. A comparação completa está em `data/runs/app_store/experiments/caixa_20260924T141356_461139Z/probe.json`. Alguns campos desse JSON ainda apontam para `output_applestore/`, o local original da execução antes da reorganização do acervo.

| Medida | Primeira coleta | Repetição isolada |
|---|---:|---:|
| Páginas `mostrecent` | 0 | 0 |
| Páginas `mosthelpful` | 2 | 1 |
| Reviews brutas no feed | 100 | 50 |
| Reviews após a data inicial | 48 | 6 |
| Review mais recente no Parquet (UTC) | 09/09/2026 | 06/08/2026 |

As seis reviews da repetição já estavam entre as 48 da primeira coleta, com os campos comparados idênticos. Nenhum ID novo apareceu; 42 IDs da primeira não reapareceram. A segunda página de `mosthelpful` veio vazia na repetição, embora tivesse 50 entradas na primeira. Isso demonstra variação na paginação acessível nesse intervalo curto; não identifica a causa técnica da resposta vazia. O Parquet original da Caixa manteve o mesmo SHA-256 antes e depois do teste.
