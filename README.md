# Avaliações de apps bancários: acervo local para pesquisa

Este projeto coleta e guarda avaliações públicas de aplicativos bancários da Google Play e da App Store. A ideia é poder consultar os textos e as notas localmente, voltar a períodos anteriores e ampliar o acervo com novas coletas quando surgir outra pergunta.

O primeiro caso de uso foi **Pix por voz**. A infraestrutura criada para respondê-lo também pode servir a pesquisas sobre Open Banking, acessibilidade, mudanças no app ou qualquer outro tema que apareça depois. As buscas são uma camada sobre os dados coletados; cada pesquisa pode ter seus próprios termos e método de leitura.

## Como surgiu

Em setembro de 2026, durante um trabalho para a Caixa sobre a possível implementação de Pix por voz, a equipe fez um benchmark e uma desk research em dois dias. Depois da apresentação, começou uma busca manual por comentários de usuários em redes sociais e nas lojas de aplicativos. Navegar pelas avaliações das lojas estava dificultando encontrar relatos sobre a função, especialmente os mais antigos.

A pergunta inicial era simples: **seria possível baixar as avaliações e procurar nelas de uma forma útil?** O teste funcionou. Como o interesse incluía o período próximo ao lançamento do recurso pelo Nubank, a coleta da Play Store foi levada até novembro de 2024 para esse app e até janeiro de 2025 para os demais bancos. As avaliações coletadas puderam ser pesquisadas e relidas com seus IDs e datas, sem depender da navegação nas lojas.

Essa entrega mostrou uma possibilidade maior: o esforço de coleta pode continuar servindo a novas perguntas. Quando os dados locais bastarem, basta consultá-los. Quando for necessário um período mais recente, é possível fazer outra rodada de coleta e atualizar a base.

## A barreira das lojas

As lojas exibem avaliações para quem está escolhendo um app, mas suas interfaces tornam difícil investigar um assunto específico em muitos comentários ou voltar a uma data antiga.

Na **App Store**, a navegação manual mostrou uma quantidade limitada de comentários na tela e formas de exibição que mudavam conforme o caminho percorrido. Era difícil avançar de modo previsível pelo histórico. A coleta pelo RSS público permitiu acessar avaliações que a equipe não conseguia encontrar facilmente na interface, mas ainda retorna um conjunto pequeno e irregular. O trabalho para ampliar essa cobertura continua aberto.

Na **Google Play**, a lista de avaliações é mais acessível, mas aparece em uma janela dentro da página do app e carrega novos itens conforme a pessoa rola a tela. Durante a busca manual, a equipe observou saltos aparentes entre meses: depois de descer de setembro para agosto e julho, a lista podia voltar a mostrar setembro. Naquele espaço, era difícil saber o que já havia sido visto, se havia repetições e até onde a busca tinha chegado. Essa observação descreve a experiência na interface; ela não estabelece a causa dos saltos.

Trazer os registros para arquivos locais muda a forma de trabalhar: é possível consultar texto, data e ID, repetir uma busca e conferir o que foi encontrado sem depender de rolagem ou da ordem exibida na tela. Foi essa barreira prática, além da demanda sobre Pix por voz, que motivou o projeto.

## Como o projeto está dividido

| Camada | O que faz | Arquivos principais |
|---|---|---|
| Coleta e acervo | Busca avaliações, guarda uma base por banco e permite novas rodadas | `scripts/collect/`, `config.yaml`, `data/raw/` |
| Descrição do acervo | Recalcula contagens, notas e séries temporais sem ler comentários | `analysis/google-play-descritiva.qmd` |
| Consulta e análise | Pesquisa as bases locais para responder a uma pergunta | `scripts/research/`, resultados locais em `private/research/` e saídas em `data/derived/` |

Os arquivos `reviews_raw.parquet` são o ponto de partida para outras pesquisas. Os scripts de Pix por voz registram **uma investigação feita com esse acervo**, não um limite para os temas que podem ser pesquisados.

## O que existe hoje

- **Google Play:** avaliações de 11 apps bancários em `data/raw/google_play/<banco>/reviews_raw.parquet`, com armazenamento real no Drive via symlink. Atualizações coletam até 24 horas antes da avaliação mais recente preservada; sem base, usam a data inicial configurada. O coletor prepara o resultado localmente e publica a base combinada pelo ID da avaliação, preservando o histórico e a versão anterior.
- **App Store:** avaliações dos mesmos 11 apps em `data/raw/app_store/<banco>/reviews_raw.parquet`, obtidas pelo RSS público brasileiro e armazenadas no Drive via symlink. O coletor prepara a base localmente, valida, preserva um snapshot e publica automaticamente no destino oficial. Essa fonte oferece uma amostra menor e cobertura histórica desigual entre os bancos.
- **Primeira pesquisa:** investigação de Pix por voz ou áudio, com resultados guardados localmente em `private/research/pix-voz/`.

As bases e saídas completas ficam em `data/` na raiz, em pastas ignoradas pelo Git. Planos, pesquisas específicas e mídia de trabalho ficam em `private/`, um symlink para a pasta `Mobile App Feedback Collector` no Google Drive do proprietário. O Git pode registrar o link, sem incluir os arquivos de destino; em outra máquina, o destino precisa ser configurado. O [contrato do acervo local](data/README.md) explica os caminhos, o acervo oficial no Drive e o manifesto versionado; uma cópia só do Git contém código, configuração e documentação operacional, sem os Parquets.

Encontrar uma avaliação antiga não garante que todas as avaliações entre ela e hoje estejam disponíveis. Para interpretar ausências ou comparar períodos, consulte os relatórios de coleta e a cobertura efetivamente observada em cada fonte.

## Começar a usar

O projeto usa Python. Para preparar o ambiente local:

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

Em um clone novo, configure a pasta externa existente (disponível offline) uma vez:

```bash
venv/bin/python -m scripts.archive_setup --private-dir "CAMINHO DA PASTA NO DRIVE"
```

Esse comando configura `private/`, os links de `data/{raw,derived,runs}` e `analysis/output/` quando há um relatório preservado. Também converte os antigos atalhos de relatório para esse único link. Se já houver arquivos locais, confere a cópia por SHA-256 antes de trocar os caminhos; arquivos divergentes interrompem a migração. Neste Mac, a configuração já está concluída.

Os bancos, IDs dos apps, datas iniciais e parâmetros de coleta ficam em [`config.yaml`](config.yaml). Para coletar ou atualizar **um banco** da Play Store:

```bash
venv/bin/python -m scripts.collect.google_play --bank nubank
```

O coletor da App Store roda separadamente:

```bash
venv/bin/python -m scripts.collect.app_store --bank nubank
```

Os guias da [Google Play](docs/playstore-guide.md) e da [App Store](docs/appstore-guide.md) explicam a atualização, os relatórios e os limites de cobertura. Consultas sobre temas específicos partem dos Parquets existentes; o exemplo de Pix por voz e seu método ficam na pesquisa local em `private/research/pix-voz/`.

Para atualizar os 11 bancos de cada plataforma:

```bash
venv/bin/python -m scripts.collect.google_play --all
venv/bin/python -m scripts.collect.app_store --all
```

Antes de uma atualização Google Play, `venv/bin/python -m scripts.collect.google_play --all --dry-run` mostra as fronteiras de coleta sem acessar a loja. Os dois coletores usam `.runtime/<plataforma>/` para checkpoints e preparação local. Após publicação confirmada, removem o checkpoint e o Parquet preparado; bases, estados, snapshots e relatórios ficam no Drive. Em caso de falha, repita o mesmo comando para retomar. O manifesto é atualizado automaticamente ao final de comandos com publicação bem-sucedida. As coletas não renderizam o Quarto automaticamente.

Para descrever as notas e datas da Google Play, instale `venv/bin/python -m pip install -r analysis/requirements.txt` e, com o Quarto instalado, execute:

```bash
venv/bin/python -m scripts.reports.render_google_play
```

O comando renderiza uma cópia do [QMD](analysis/google-play-descritiva.qmd) em uma pasta temporária local, publica uma versão completa e conferida em `private/reports/google-play-descritiva/<data-hora>/` e atualiza o único symlink `analysis/output/`. Abra `analysis/output/google-play-descritiva.html` para consultar o relatório. A fonte QMD permanece versionada. Use esse comando para publicar novas renderizações com seus recursos no Drive.

A renderização de 27/09/2026 e a primeira versão de 05/10/2026 foram preservadas nos caminhos anteriores. Novas versões ficam em pastas com data e hora; uma falha de renderização ou cópia mantém o link para a versão anterior. A aba “Atualização do acervo” mostra a comparação preservada de 05/10 apenas enquanto seus hashes corresponderem às bases atuais; a geração dessa comparação específica não é automática nas próximas coletas. Contagens, notas e séries do relatório são recalculadas com as bases atuais a cada renderização.

O projeto começou como resposta a uma demanda urgente e está sendo estruturado a partir do que funcionou. Seu valor central é manter as avaliações acessíveis para perguntas futuras, preservando a possibilidade de voltar às mensagens que sustentam cada achado.
