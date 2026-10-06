# Inter App Store: checkpoint da investigação Web + RSS

Estado verificado em 2026-10-06. Este registro permite retomar o trabalho sem depender do histórico da conversa. Os artefatos privados e as respostas brutas permanecem no arquivo externo; este documento registra apenas métricas, decisões e caminhos.

## Evidência já demonstrada

- App Inter `839711154`, storefront `br`, Web `sort=recent`: offsets `0–5030` percorridos sequencialmente em páginas de dez, 5.040 IDs únicos, com a review mais antiga em `2024-12-31T04:54:04Z`. O critério de parada foi atravessar `2025-01-01`; isso não prova cobertura integral da App Store.
- A continuação retomou do offset `530` e repetiu cada offset após HTTP 429 e espera conservadora. Não foram observados saltos, duplicações entre páginas, páginas 200 vazias ou inversões cronológicas. Todas as respostas brutas, inclusive 429, foram preservadas; os hashes foram conferidos.
- O conjunto RSS canônico atual do Inter contém 746 IDs. Interseção Web ∩ RSS: 742; somente Web: 4.298; somente RSS: 4. O corpus união esperado contém 5.044 IDs.
- A repetição de nove páginas em momentos diferentes encontrou os mesmos bytes, IDs, ordem, campos e paginação. Isso sustenta reprodutibilidade observada, sem garantir estabilidade futura.
- Entre 700 IDs com timestamps brutos disponíveis nas duas fontes, 524 têm o mesmo instante em UTC e 176 diferem por exatamente uma hora. O RSS fornece `updated` com offset explícito `-07:00` mesmo em algumas datas de inverno de Los Angeles; a Web fornece `date` em UTC. O parser atual respeita corretamente o offset recebido. Não há evidência suficiente de que os campos representem sempre o mesmo evento, nem justificativa para `review_date` universal.

## Fontes da evidência e integridade

- Coleta Web completa, índice, checkpoint e payloads: `data/runs/app_store/experiments/inter_web_recent_resumed_20261005T235006_996254Z_b70ce5/` (`summary.json`, `pages.jsonl`, `pages/`). Os 504 offsets HTTP 200 representam 5.040 reviews; as 515 respostas incluem 11 HTTP 429.
- Auditoria dos hashes e das páginas repetidas: `data/runs/app_store/experiments/inter_web_final_integrity_20261006T015655_396697Z_b31789/audit.json`.
- Comparação Web × RSS: `data/runs/app_store/experiments/inter_web_rss_analysis_20261006T015618_935918Z_873ccf/`.
- Análise temporal: `data/runs/app_store/experiments/inter_web_temporal_semantics_20261005T235146_046193Z_c4cec0/` e `inter_web_temporal_semantics_20261006T015635_961139Z_64d2a5/` no mesmo diretório de experimentos.
- Relatório privado: `private/research/amp-appstore-investigation/inter-web-resume-temporal-followup.md`; os spikes anteriores estão nessa pasta.
- Os três planos históricos desta mesma frente AMP/Web estão reunidos em `private/workstreams/plans/app-store-amp-web-collection/`; o `README.md` dessa pasta aponta para este checkpoint e para o fechamento privado.
- RSS canônico de entrada: `data/raw/app_store/inter/reviews_raw.parquet`, SHA-256 `8463e75edf03a03ff4ff20ffb43ef9e599886e8cc630cd9b5420ace146595c3d`. A auditoria anterior confirmou que os 11 Parquets canônicos App Store permaneceram inalterados.

## Decisões para a promoção

1. Preservar o backfill Web existente e materializá-lo sem baixar as 5.040 reviews outra vez.
2. Manter RSS e Web como acervos independentes; reconciliar por `review_id` e registrar presença em cada fonte. O fluxo é `coleta RSS → coleta Web → reconciliação por review_id`.
3. Guardar `rss_updated_raw`, `rss_updated_utc`, `web_date_raw` e `web_date_utc` separadamente. Valores brutos indisponíveis no arquivo RSS histórico devem permanecer nulos e identificados como indisponíveis; nunca inferir um offset original a partir de UTC.
4. Preservar a procedência das respostas Web, seus hashes e o resultado da reconciliação. Não usar timestamps como identidade nem substituir silenciosamente divergências.
5. Não coletar os outros bancos nesta etapa. O mecanismo pode ser reutilizável; a publicação inicial é somente do Inter.

## Promoção executada

- `scripts/collect/app_store_web.py` executa coleta Web configurável por banco, com paginação sequencial, payloads brutos, hashes, journal, checkpoint, espera após 429 e retomada da última página 200 confirmada. `--from-run` materializa um run já preservado sem consultar a Apple. Runs novos completos passam a publicar a base Web.
- O run já existente foi materializado em `data/raw/app_store/inter/reviews_web.parquet`: **5.040 IDs**, SHA-256 `4352d2af108164a77e08dcfd5533f169af55aaaffd4727014c9cde8779434a97`. Seu journal tem SHA-256 `3aeedcc36e59c0ec9f9ed70712693b6e29c3e760830ab79f0f08daf351bbf64b`. Recibo: `data/runs/app_store/web/inter/web_archive_state.json`.
- `scripts/collect/app_store_reconcile.py` reconciliou o RSS canônico e esse Web Parquet em `data/derived/app_store/inter/reviews_reconciled.parquet`: **5.044 IDs**, SHA-256 `e9b6e6dfc1d09362052e96742e14f72e8bfed16b64a2a8ccadb19fecf53276af`. Classes: `rss_web=742`, `web_only=4298`, `rss_only=4`. Recibo: `data/runs/app_store/reconciliation/inter/state.json`. O primeiro Parquet intermediário foi preservado como snapshot quando o campo `rss_updated_raw_origin` foi acrescentado.
- O coletor RSS passou a guardar `rss_updated_raw` nas próximas publicações. A base RSS atual foi preservada com seu hash anterior; os valores brutos históricos recuperáveis são anexados somente no corpus reconciliado.
- O corpus preserva lado a lado campos de conteúdo RSS e Web e a diferença temporal observada; 298 IDs RSS têm `rss_updated_raw` autêntico dos captures preservados, 448 têm somente o UTC histórico. Dos 742 IDs comuns da base atual, 35 têm RSS UTC uma hora antes do Web UTC e 707 coincidem. Essa comparação do corpus usa o UTC canônico; a análise temporal anterior de 700 pares de payloads brutos tem amostra diferente.
- O RSS canônico `reviews_raw.parquet` permaneceu como fonte independente. A versão atual do relatório Quarto continua sendo uma análise da amostra RSS.
- Validação final: `venv/bin/python -m unittest discover -s tests` passou com **84 testes**; os hashes dos **11 Parquets RSS canônicos** App Store são idênticos aos registrados antes desta promoção. `data/manifest.json` inventaria os 50 Parquets atuais, incluindo Web, corpus e snapshot reconciliado.

## Aberto

- A semântica exata de RSS `updated` versus Web `date`, inclusive após edição da review, permanece indeterminada.
- Cobertura futura e estabilidade de offsets devem ser monitoradas; a travessia observada não prova completude universal nem garante paginação estável em novas coletas.
- Os dois captures RSS brutos existentes contêm 700 IDs, mas apenas 298 pertencem aos 746 IDs da base RSS canônica atual. Para os outros 448, o offset/texto original de `updated` não está preservado nesses captures; o UTC canônico continua disponível. A reconciliação deve registrar essa lacuna explicitamente.
- A extensão para os demais bancos e uma rotina incremental de produção ainda exigem validação própria. O próximo passo é selecionar outro banco, validar sua paginação Web com o mesmo mecanismo e desenhar sobreposição incremental por `review_id`, mantendo snapshots e proveniência.
