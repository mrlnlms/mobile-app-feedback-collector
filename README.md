# Avaliações de apps bancários: acervo local para pesquisa

Este projeto coleta e guarda avaliações públicas de aplicativos bancários da Google Play e da App Store. A ideia é poder consultar os textos e as notas localmente, voltar a períodos anteriores e ampliar o acervo com novas coletas quando surgir outra pergunta.

O primeiro caso de uso foi **Pix por voz**. A infraestrutura criada para respondê-lo também pode servir a pesquisas sobre Open Banking, acessibilidade, mudanças no app ou qualquer outro tema que apareça depois. As buscas são uma camada sobre os dados coletados; cada pesquisa pode ter seus próprios termos e método de leitura.

## Como surgiu

Em setembro de 2026, durante um trabalho para a Caixa sobre a possível implementação de Pix por voz, a equipe fez um benchmark e uma desk research em dois dias. Depois da apresentação, começou uma busca manual por comentários de usuários em redes sociais e nas lojas de aplicativos. Navegar pelas avaliações das lojas estava dificultando encontrar relatos sobre a função, especialmente os mais antigos.

A pergunta inicial era simples: **seria possível baixar as avaliações e procurar nelas de uma forma útil?** O teste funcionou. Como o interesse incluía o período próximo ao lançamento do recurso pelo Nubank, a coleta da Play Store foi levada até novembro de 2024 para esse app e até janeiro de 2025 para os demais bancos. O trabalho encontrou seis mensagens explícitas sobre fazer Pix por voz ou áudio em um acervo de mais de um milhão de avaliações da Play Store.

Essa entrega mostrou uma possibilidade maior: o esforço de coleta pode continuar servindo a novas perguntas. Quando os dados locais bastarem, basta consultá-los. Quando for necessário um período mais recente, é possível fazer outra rodada de coleta e atualizar a base.

## A barreira das lojas

As lojas exibem avaliações para quem está escolhendo um app, mas suas interfaces tornam difícil investigar um assunto específico em muitos comentários ou voltar a uma data antiga.

Na **App Store**, a navegação manual mostrou uma quantidade limitada de comentários na tela e formas de exibição que mudavam conforme o caminho percorrido. Era difícil avançar de modo previsível pelo histórico. A coleta pelo RSS público permitiu acessar avaliações que a equipe não conseguia encontrar facilmente na interface, mas ainda retorna um conjunto pequeno e irregular. O trabalho para ampliar essa cobertura continua aberto.

Na **Google Play**, a lista de avaliações é mais acessível, mas aparece em uma janela dentro da página do app e carrega novos itens conforme a pessoa rola a tela. Durante a busca manual, a equipe observou saltos aparentes entre meses: depois de descer de setembro para agosto e julho, a lista podia voltar a mostrar setembro. Naquele espaço, era difícil saber o que já havia sido visto, se havia repetições e até onde a busca tinha chegado. Essa observação descreve a experiência na interface; ela não estabelece a causa dos saltos.

Trazer os registros para arquivos locais muda a forma de trabalhar: é possível consultar texto, data e ID, repetir uma busca e conferir o que foi encontrado sem depender de rolagem ou da ordem exibida na tela. Foi essa barreira prática, além da demanda sobre Pix por voz, que motivou o projeto.

## Como o projeto está dividido

| Camada | O que faz | Arquivos principais |
|---|---|---|
| Coleta e acervo | Busca avaliações, guarda uma base por banco e permite novas rodadas | `collector.py`, `collector_applestore.py`, `config.yaml`, `output/`, `output_applestore/` |
| Consulta e análise | Pesquisa as bases locais para responder a uma pergunta | `filter.py`, `analyze_pix_audio.py`, `search_pix_audio.py`, relatórios e CSVs de resultados |

Os arquivos `reviews_raw.parquet` são o ponto de partida para outras pesquisas. Os scripts de Pix por voz registram **uma investigação feita com esse acervo**, não um limite para os temas que podem ser pesquisados.

## O que existe hoje

- **Google Play:** avaliações de 11 apps bancários em `output/<banco>/reviews_raw.parquet`. O coletor usa a ordenação por mais recentes, interrompe a busca ao passar da data configurada e combina novas coletas com a base local pelo ID da avaliação.
- **App Store:** avaliações dos mesmos 11 apps em `output_applestore/<banco>/reviews_raw.parquet`, obtidas pelo RSS público brasileiro. Essa fonte oferece uma amostra menor e cobertura histórica desigual entre os bancos.
- **Primeira pesquisa:** seis comentários explícitos sobre Pix por voz ou áudio, com texto e ID; resultado na App Store; e guia para explorar outros candidatos.

Encontrar uma avaliação antiga não garante que todas as avaliações entre ela e hoje estejam disponíveis. Para interpretar ausências ou comparar períodos, consulte os relatórios de coleta e a cobertura efetivamente observada em cada fonte.

## Começar a usar

O projeto usa Python. Para preparar o ambiente local:

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

Os bancos, IDs dos apps, datas iniciais e parâmetros de coleta ficam em [`config.yaml`](config.yaml). Para coletar ou atualizar **um banco** da Play Store:

```bash
venv/bin/python collector.py --bank nubank
```

Para refazer a busca de Pix por voz nos dados **já armazenados**, sem acessar a loja:

```bash
venv/bin/python search_pix_audio.py
```

Esse comando refaz a busca exploratória por Pix, voz, áudio, WhatsApp e conversa e grava os candidatos em `output/pix_audio_review/`. Para pesquisar outro assunto, o caminho é partir dos Parquets existentes e criar uma consulta adequada à nova pergunta. A coleta pode ser repetida depois, se a base precisar de avaliações mais recentes.

O coletor da App Store roda separadamente:

```bash
venv/bin/python collector_applestore.py --bank nubank
```

## Onde ler mais

- [Documentos do projeto](docs/README.md)

O projeto começou como resposta a uma demanda urgente e está sendo estruturado a partir do que funcionou. Seu valor central é manter as avaliações acessíveis para perguntas futuras, preservando a possibilidade de voltar às mensagens que sustentam cada achado.