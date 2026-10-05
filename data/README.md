# Acervo de avaliações

`data/` mantém caminhos estáveis para os scripts e análises. Neste Mac, `raw/`, `derived/` e `runs/` são symlinks relativos para `private/data/`, o acervo oficial no Google Drive. Os links são ignorados pelo Git; `README.md` e `manifest.json` são versionados.

| Caminho | Conteúdo |
|---|---|
| `raw/google_play/<banco>/` | Base principal `reviews_raw.parquet` da Google Play. |
| `raw/app_store/<banco>/` | Base principal `reviews_raw.parquet` da App Store brasileira. |
| `derived/google_play/` | Resultados por banco, primeira rodada e comparação antes/depois. |
| `derived/app_store/current/` | Comparação preservada da atualização RSS de 05/10/2026. |
| `derived/pix_voz/` | Consultas lexicais: auditoria inicial e resultados recalculados em 27/09/2026. |
| `derived/poc/` | CSVs da PoC original. |
| `runs/google_play/` | Relatórios, estado por banco, snapshots e checkpoints históricos. |
| `runs/app_store/` | Relatórios, estado por banco, snapshots, experimentos e checkpoints históricos. |
| `runs/migration/` | Recibos de preservação e verificação da migração. |

## Operação

O [`config.yaml`](../config.yaml) define os caminhos. Os dois coletores preparam novas bases localmente em `.runtime/<plataforma>/`, preservam snapshots no acervo, conferem SHA-256 e publicam no Drive. Após o sucesso, removem checkpoints ativos e Parquets preparados. Em caso de falha, os arquivos necessários à retomada continuam locais; repita o mesmo comando. Locks locais pequenos podem permanecer.

Os arquivos de estado registram a data da coleta concluída, a avaliação mais recente e o hash da base. A Google Play usa uma fronteira com sobreposição de 24 horas; a App Store continua percorrendo o RSS, sem parada cronológica. Consulte os guias da [Google Play](../docs/playstore-guide.md) e da [App Store](../docs/appstore-guide.md).

O [manifesto](manifest.json) registra caminho, hash, tamanho, número de reviews e datas extremas dos Parquets. Não contém textos de avaliações nem substitui os próprios arquivos. É atualizado pelos comandos de coleta após publicações bem-sucedidas. Para recalculá-lo separadamente:

```bash
venv/bin/python -m scripts.archive_manifest
```

## Reconstruir os caminhos

Um clone contém código, configuração, QMD, documentação e manifesto. Para usar o acervo externo existente, prepare o ambiente Python e execute na raiz:

```bash
venv/bin/python -m scripts.archive_setup --private-dir "CAMINHO DA PASTA NO DRIVE"
```

O comando configura `private/` e estes links locais:

```text
data/raw     -> ../private/data/raw
data/derived -> ../private/data/derived
data/runs    -> ../private/data/runs
```

Se já houver arquivos locais, confere sua cópia por SHA-256 antes de substituir os diretórios por links. Arquivos divergentes interrompem a migração e mantêm o original local. A pasta externa precisa existir e estar disponível offline. Apagar o projeto ou seus symlinks não apaga o destino; apagar arquivos através de `data/raw/`, por exemplo, altera o acervo oficial.

## Relatórios Quarto

O QMD permanece em Git. `venv/bin/python -m scripts.reports.render_google_play` renderiza localmente, copia e confere uma versão completa no Drive e atualiza o único symlink `analysis/output/`. O HTML e seus recursos ficam juntos dentro dessa versão; abra `analysis/output/google-play-descritiva.html`. Na pasta `analysis/`, apenas o QMD, as dependências e a configuração permanecem como arquivos locais. O comando `scripts.archive_setup` converte os três atalhos antigos para `output/`, conferindo os destinos antes de removê-los. A renderização anterior permanece disponível se o comando falhar; cada publicação cria uma pasta com data e hora em `private/reports/google-play-descritiva/`.

## Migração de 05/10/2026

As árvores originais foram inicialmente copiadas com conferência de 169 arquivos. A Google Play foi atualizada para 1.280.384 reviews e a App Store para 6.253 reviews; seus relatórios, snapshots anteriores e comparações foram preservados. Os três diretórios de dados passaram a acessar o Drive, incluindo os resultados de Pix por voz e da PoC. As cópias locais concluídas foram removidas após comprovar sua preservação; evidências e recibos ficam em `runs/migration/`.

A renderização de 27/09/2026 permanece em `private/reports/google-play-descritiva/` e a primeira versão de 05/10 permanece em sua subpasta `2026-10-05/`. Novas versões são publicadas pelo comando de renderização. A comparação antes/depois de 05/10 é uma evidência dessa atualização: não é regenerada automaticamente pelas próximas coletas e é ocultada no QMD se seus hashes ficarem desatualizados.

Conferir um arquivo no destino acessível pelo Mac não confirma que o aplicativo do Drive terminou o upload. Manter as bases fora do Git e preservá-las no Drive também não demonstra cobertura integral das lojas.
