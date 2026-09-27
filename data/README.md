# Acervo local de avaliações

As bases e saídas ficam nesta pasta na raiz do projeto, mas `raw/`, `derived/` e `runs/` são ignoradas pelo Git. O [manifesto versionado](manifest.json) registra caminhos, hashes SHA-256, tamanhos, quantidade de reviews e datas extremas dos Parquets observados localmente. O manifesto não contém textos de avaliações nem substitui os próprios arquivos.

| Pasta local | Conteúdo |
|---|---|
| `raw/google_play/<banco>/` | Base principal `reviews_raw.parquet` da Google Play. |
| `raw/app_store/<banco>/` | Base principal `reviews_raw.parquet` da App Store brasileira. |
| `derived/google_play/` | Saídas por banco; `initial-2026-09-24/` preserva a primeira rodada e `current/` recebe novas análises. |
| `derived/pix_voz/` | Consultas lexicais: `audit-2026-09-24/` preserva a primeira rodada; `current/` foi gerada sobre as bases atuais em 27/09/2026. |
| `derived/poc/` | CSVs da PoC original. |
| `runs/google_play/` e `runs/app_store/` | Relatórios JSON, checkpoints, snapshots e experimentos de coleta. |

O [`config.yaml`](../config.yaml) define os caminhos usados pelos scripts. Rode os comandos a partir da raiz do projeto. Para conferir as bases locais ou atualizar o manifesto após uma coleta:

```bash
venv/bin/python -m scripts.archive_manifest
```

Um clone do Git começa sem `raw/`, `derived/` e `runs/`; quem tiver uma cópia do acervo local pode colocá-la nesses caminhos e comparar os SHA-256 com `manifest.json`. Manter as bases fora do Git é uma decisão inicial de armazenamento, não uma afirmação de que o RSS da App Store ou a coleta da Play Store cobrem todo o período configurado.

Guias de coleta, planos, pesquisas, registros antigos e mídia de trabalho ficam em `private/`, também ignorada pelo Git. A pasta [docs/](../docs/README.md) fica reservada a documentos de projeto versionáveis.
