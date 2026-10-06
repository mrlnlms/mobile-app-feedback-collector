# Coleta de avaliações da App Store

## Modelo RSS + Web

O fluxo da App Store tem três partes: **coleta RSS → coleta Web → reconciliação por `review_id`**. O coletor RSS existente continua publicando `data/raw/app_store/<banco>/reviews_raw.parquet`. A Web usa `sort=recent` e guarda suas páginas brutas, journal, hashes e checkpoint; quando alcança a data inicial configurada ou o fim indicado pela fonte, publica `data/raw/app_store/<banco>/reviews_web.parquet`. A reconciliação escreve `data/derived/app_store/<banco>/reviews_reconciled.parquet`, com presença `rss_web`, `web_only` ou `rss_only`. Essa extensão foi executada e validada para **Inter e Nubank**; os demais bancos ainda precisam de suas próprias coletas e validações.

O corpus mantém `rss_updated_raw`, `rss_updated_utc`, `web_date_raw` e `web_date_utc` como campos independentes. O RSS histórico já guardava `data_avaliacao` em UTC, mas não o texto bruto de `updated`; os captures brutos preservados recuperam esse texto para 298 dos 746 IDs RSS atuais do Inter. Nos outros 448, `rss_updated_raw` é nulo e `rss_updated_raw_available=false`, enquanto `rss_updated_utc` mantém o valor da base RSS. Não há captures RSS brutos históricos do Nubank: seus 535 IDs RSS têm `rss_updated_raw` nulo e marcado como indisponível, com `rss_updated_utc` preservado. Coletas RSS futuras também gravam `rss_updated_raw` na própria base RSS; a reconciliação prefere esse valor e usa captures como complemento histórico. A diferença de uma hora observada em parte dos IDs vem dos offsets explícitos do RSS; a semântica de `updated` frente a `date` ainda não foi demonstrada como idêntica. O corpus não cria um `review_date` universal, não escolhe silenciosamente texto/nota/título de uma fonte e não usa datas para identificar reviews.

### Operação Web e retomada

Para uma nova coleta de um banco já configurado, execute `venv/bin/python -m scripts.collect.app_store_web --bank inter --collect`. O padrão é paginação sequencial, espera de 15 segundos entre páginas e repetição do mesmo offset após 429, com `Retry-After` ou backoff conservador. Cada resposta é gravada antes da próxima requisição. Runs finalizados ou pausados ficam em `data/runs/app_store/web/<banco>/`; o journal e os payloads também podem permanecer em `.runtime/app_store/web/` se a execução cair antes de arquivar. Um 429 após o limite de retries pausa a coleta e **não** significa fim da paginação. Retome com `--collect --resume-from CAMINHO_DO_RUN`; o journal é revalidado com SHA-256 e o próximo offset vem da última página 200 confirmada. Revise o `summary.json` antes de decidir se a coleta está completa; `page_limit` e `rate_limited_paused` não publicam a base Web. Ao alcançar a data inicial ou o fim indicado pela fonte, o comando materializa o Web Parquet automaticamente, unindo IDs novos aos IDs já preservados. Para IDs encontrados outra vez, prevalece a observação Web mais recente; a base anterior é preservada em snapshot.

O backfill já feito no Inter foi reaproveitado sem baixar novamente: `venv/bin/python -m scripts.collect.app_store_web --bank inter --from-run data/runs/app_store/experiments/inter_web_recent_resumed_20261005T235006_996254Z_b70ce5`. A origem bruta continua no experimento, apontada em cada linha do Parquet por run, arquivo e SHA-256 do payload. O recibo fica em `data/runs/app_store/web/inter/web_archive_state.json`.

O backfill do Nubank atravessou o corte configurado de `2024-11-01` com 7.090 IDs Web observados, entre `2024-10-31T10:59:34Z` e `2026-10-05T02:29:55Z`. O run está em `data/runs/app_store/web/nubank/nubank_recent_20261006T090439_350120Z_99176e/` e o recibo em `data/runs/app_store/web/nubank/web_archive_state.json`. Após 8 respostas 429 nas primeiras 30 páginas HTTP 200 com intervalo de 15s, o mesmo run foi retomado com `--delay-seconds 30`: houve mais 43 respostas 429 em 679 páginas HTTP 200; todas as 51 foram recuperadas no mesmo offset. Essa medição descreve este run, sem garantir a mesma taxa em outra coleta.

Depois de uma base Web publicada, execute `venv/bin/python -m scripts.collect.app_store_reconcile --bank inter --rss-capture data/runs/app_store/experiments/inter_rss_temporal_20261005T232113_538226Z_3c56a8 --rss-capture data/runs/app_store/experiments/inter_rss_mostrecent_temporal_20261005T232246_608898Z_6b0308`. O recibo fica em `data/runs/app_store/reconciliation/inter/state.json`. Uma reconciliação futura pode receber outros captures RSS brutos por `--rss-capture`; sem eles, o campo bruto RSS permanece nulo. Publicações usam staging local, conferência de SHA-256, snapshot da base anterior quando existir e substituição atômica. Recalcule `data/manifest.json` após publicar: `venv/bin/python -m scripts.archive_manifest`.

Para o Nubank, execute `venv/bin/python -m scripts.collect.app_store_reconcile --bank nubank`, sem `--rss-capture`. A publicação atual contém 7.093 IDs: 532 em RSS ∩ Web, 6.558 somente Web e 3 somente RSS. O recibo fica em `data/runs/app_store/reconciliation/nubank/state.json`.

O corpus é uma união dos IDs **observados**, não uma prova de que a App Store inteira foi recuperada. O painel Quarto atual ainda lê apenas o RSS; sua mudança exige decisão própria.

A parte RSS consulta o feed público da App Store brasileira. Execute os comandos a partir da raiz do projeto, com o ambiente indicado no [README](../README.md). O comando `venv/bin/python -m scripts.collect.app_store --bank nubank` coleta um banco. Use `--bank nubank itau` para uma seleção ou `--all` para os 11 bancos. O [`config.yaml`](../config.yaml) mantém os identificadores Android em `app_id` e os IDs iOS em `apple_app_id`. As datas `start_date` são compartilhadas com o pipeline Google Play.

Os dados iOS ficam em `data/raw/app_store/<banco>/reviews_raw.parquet`, com relatórios JSON em `data/runs/app_store/reports/`, checkpoints ativos em `.runtime/app_store/checkpoints/` e versões anteriores em `data/runs/app_store/snapshots/`. Esses caminhos de `data/` acessam o acervo oficial no Drive por symlink. Uma coleta interrompida retoma da última página salva. A base existente só é substituída após o sucesso de todos os sorts.

## Publicação automática e retomada

O comando `venv/bin/python -m scripts.collect.app_store --all` executa o fluxo completo dos 11 bancos: coleta em checkpoints locais, prepara o Parquet combinado em `.runtime/app_store/staging/<banco>/`, valida IDs, datas e notas, copia para um arquivo temporário no destino oficial e confere seu SHA-256. Antes de substituir a base, preserva e confere a versão anterior em `data/runs/app_store/snapshots/<banco>/`. O histórico anterior é mantido; para um mesmo ID, a versão recém-coletada tem prioridade.

Cada banco tem um bloqueio local contra coletas simultâneas nesta máquina. Se a base mudar durante a coleta, a publicação é cancelada. Um destino externo indisponível interrompe a execução. Não execute duas máquinas gravando na mesma base simultaneamente: o bloqueio é local.

Após a publicação, o estado em `data/runs/app_store/state/<banco>.json` registra o hash, a avaliação mais recente e a data da coleta concluída. Esse estado documenta a publicação; não cria uma fronteira cronológica para o RSS, que continua percorrendo os sorts configurados. O checkpoint e o Parquet preparado são removidos após o sucesso. Se a cópia, o snapshot ou o estado falhar, repita o mesmo comando: um checkpoint completo permite retomar a publicação sem baixar novamente, inclusive quando a base foi substituída antes da falha no estado.

Se todos os feeds vierem vazios, a base oficial é preservada e a execução falha. O mesmo comando pode consultar os feeds novamente na próxima tentativa. Se somente um sort vier vazio, os outros podem concluir a rodada; confira as limitações no relatório. Ao final, coletas com publicação bem-sucedida atualizam `data/manifest.json` automaticamente. Uma falha de manifesto é informada separadamente.

O Drive deve estar disponível offline. A conferência de hash confirma a gravação no destino acessível pelo Mac; não confirma que o aplicativo do Drive concluiu a sincronização na nuvem.

O coletor percorre as páginas de cada sort até o limite configurado ou até o feed terminar, sem parar por data. Depois deduplica por `id_review` e filtra pela data civil do campo `updated` do RSS. `data_avaliacao` é gravada em UTC. O RSS fornece `updated`, que pode representar uma edição da review; ele não fornece necessariamente a data original de publicação.

O Parquet mantém `id_review`, `usuario`, `nota`, `data_avaliacao`, `rss_updated_raw` para novas observações, `titulo`, `texto_avaliacao`, `versao_app`, `sorts_encontrados`, `apple_app_id`, `pais` e `plataforma`. `sorts_encontrados` registra em quais ordenações o mesmo ID apareceu; `data_avaliacao` corresponde ao `updated` do RSS em UTC. O [contrato do acervo](../data/README.md) descreve os caminhos e o manifesto local.

O painel descritivo da App Store fica em [`analysis/app-store-descritiva.qmd`](../analysis/app-store-descritiva.qmd). Depois da coleta, execute `venv/bin/python -m scripts.reports.render_app_store` para recalcular notas e datas, preservar uma nova versão completa no Drive e atualizar `analysis/app-store-output/app-store-descritiva.html`. O painel lê somente `nota` e `data_avaliacao`; suas médias e volumes descrevem a amostra RSS preservada, sem provar cobertura mensal contínua nem representar a nota pública do app.

## Cobertura e ordenações

O padrão no YAML usa `mostrecent` e `mosthelpful`. As opções vistas na interface da App Store não implicam feeds RSS públicos equivalentes. A [documentação da Apple sobre reviews](https://developer.apple.com/app-store/ratings-and-reviews/) descreve ordenação no App Store Connect para usuários autorizados do próprio app, e não acesso público aos reviews de outros bancos.

O coletor tenta no máximo 10 páginas de até 50 reviews por sort e país. `mosthelpful` não é cronológico. O relatório distingue a data mais antiga observada em todos os sorts da data mais antiga em `mostrecent`. Se `mostrecent_alcancou_inicio` for `false`, o feed cronológico não alcançou a data inicial configurada; reviews anteriores aos últimos itens do feed podem estar ausentes. Mesmo quando for `true`, o RSS não prova cobertura integral do histórico. As avaliações escritas coletadas também não representam todas as notas da loja, pois parte das notas não contém texto.

Os IDs iOS foram conferidos pelo [iTunes Search API da Apple](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/Searching.html), usando o catálogo brasileiro. A lista de bancos e as datas vieram do `config.yaml` existente.

Para acompanhar uma coleta, leia o relatório em `data/runs/app_store/reports/` antes de interpretar as datas ou repetir o banco. Cada medição abaixo descreve somente a rodada indicada; uma coleta posterior precisa ser avaliada pelo seu próprio relatório.

### Atualização da amostra em 05/10/2026

Os 11 bancos terminaram sem erros. A união dos dados anteriores com os dados acessíveis nesta rodada passou de **2.861 para 6.253 reviews**, com **3.392 IDs novos**, nenhum ID antigo removido e IDs únicos em todas as bases. Dois IDs existentes tiveram alteração em nota ou data; essa comparação não mede alterações de texto. Datas e notas foram validadas nos Parquets finais.

| Banco | Base anterior | Base atual | IDs novos | Páginas `mostrecent` / `mosthelpful` |
|---|---:|---:|---:|---:|
| Nubank | 334 | 535 | 201 | 9 / 3 |
| Itaú | 450 | 545 | 95 | 0 / 10 |
| Banco do Brasil | 156 | 656 | 500 | 10 / 2 |
| Bradesco | 258 | 558 | 300 | 8 / 2 |
| Santander | 269 | 328 | 59 | 4 / 4 |
| Inter | 547 | 746 | 199 | 10 / 10 |
| C6 Bank | 240 | 538 | 298 | 10 / 10 |
| PicPay | 200 | 548 | 348 | 10 / 10 |
| Mercado Pago | 109 | 622 | 513 | 10 / 10 |
| BTG Pactual | 250 | 502 | 252 | 10 / 10 |
| Caixa | 48 | 675 | 627 | 10 / 5 |

`mostrecent` não alcançou o início configurado em nenhum dos dez bancos com resposta nesse sort. No Itaú, veio vazio; os dados acessíveis por `mosthelpful` chegaram somente a 23/09/2026. Portanto, a execução concluída não significa que a amostra do Itaú esteja atualizada até outubro. Nos demais bancos, a data mais recente na base ficou em 03 ou 04/10/2026 (UTC). IDs novos nesta rodada podem corresponder a reviews antigas que o RSS não havia retornado antes.

A Caixa voltou a responder em `mostrecent`, que estava vazio na rodada de setembro. As quantidades por página e sort variaram entre as consultas; a amostra acumulada continua sem comprovação de cobertura histórica integral.

O relatório está em `data/runs/app_store/reports/report_20261005_150251.json` e a comparação antes/depois em `data/derived/app_store/current/collection_comparison.json`. Bases finais, snapshots anteriores, relatório e comparação foram preservados em `private/data/`, com conferência de SHA-256 das bases e snapshots. A cópia desta rodada foi feita manualmente antes da migração. Depois dela, os caminhos oficiais foram ligados ao Drive e o coletor recebeu a publicação automática descrita acima; nenhuma coleta adicional foi necessária para essa migração. O manifesto foi atualizado após a coleta.

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

O teste usa os mesmos sorts, páginas máximas, pausas e data inicial da coleta normal, mas grava em uma pasta nova `data/runs/app_store/experiments/caixa_<data-hora>/`. Nela ficam o novo Parquet e `probe.json`, que compara páginas por sort, quantidade de reviews, IDs compartilhados, IDs exclusivos e campos alterados. O script registra o SHA-256 do Parquet original antes e depois da execução. Mesmo se os dois feeds voltarem vazios, a rodada salva um Parquet vazio no experimento e registra o resultado. O comando não usa os checkpoints canônicos nem publica em `data/raw/app_store/caixa/`. A preparação e os checkpoints do experimento ficam em uma pasta temporária local; em caso de falha, sua evidência parcial é preservada em `partial_local_work/` dentro do experimento.

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
