"""
PoC - Coleta de Reviews do Nubank (Play Store)
Objetivo: Validar que o pipeline funciona puxando poucos dados.
Feature de interesse: Pix por Voz (lançado em 2024)
"""

import pandas as pd
from google_play_scraper import Sort, reviews
from datetime import datetime
import time
from pathlib import Path

# ============================================================
# PARÂMETROS DA PoC (valores baixos para teste rápido)
# ============================================================
APP_ID = "com.nu.production"
TARGET_START_DATE = datetime(2024, 8, 1)  # Limite inferior
MAX_PAGES = 3  # Apenas 3 páginas (~600 reviews) para PoC
REVIEWS_PER_PAGE = 200

# Palavras-chave para identificar o feature "Pix por Voz"
KEYWORDS = [
    "pix por voz", "pix voz", "pix com voz", "comando de voz",
    "falar pix", "enviar pix falando", "áudio pix", "ditado pix",
    "reconhecimento de voz"
]


def fetch_reviews_poc(app_id, start_date, keywords, max_pages, per_page):
    """Coleta reviews com paginação e early stopping."""
    print(f"🚀 Iniciando PoC de coleta - App: {app_id}")
    print(f"📅 Data limite: {start_date.strftime('%Y-%m-%d')}")
    print(f"📄 Máximo de páginas: {max_pages}\n")

    all_reviews = []
    continuation_token = None
    stop_triggered = False

    for page in range(max_pages):
        if stop_triggered:
            break

        result, continuation_token = reviews(
            app_id,
            lang='pt',
            country='br',
            sort=Sort.NEWEST,
            count=per_page,
            continuation_token=continuation_token
        )

        if not result:
            print("⚠️  Sem mais resultados disponíveis.")
            break

        for review in result:
            review_date = review['at']

            # Early stopping: parar se review for anterior à data alvo
            if review_date < start_date:
                stop_triggered = True
                print(f"🛑 Early stop! Review encontrado de {review_date.strftime('%Y-%m-%d')}")
                break

            all_reviews.append(review)

        print(f"   Página {page + 1}/{max_pages} processada | "
              f"Reviews coletados: {len(all_reviews)}")

        # Pausa entre requisições para evitar rate limit
        time.sleep(1)

    print(f"\n✅ Coleta finalizada. Total dentro do período: {len(all_reviews)}")
    return all_reviews


def filter_and_structure(raw_reviews, keywords):
    """Filtra por keywords e estrutura o DataFrame."""
    if not raw_reviews:
        print("❌ Nenhum review coletado. Verifique os parâmetros.")
        return pd.DataFrame()

    df = pd.DataFrame(raw_reviews)

    # Garantir tipagem de data
    df['reviewed_at'] = pd.to_datetime(df['at']).dt.tz_localize(None)

    # Filtro por keywords
    pattern = '|'.join(keywords)
    df['match_keyword'] = df['content'].str.contains(
        pattern, case=False, na=False
    )

    # Separar: todos os reviews e só os filtrados
    df_all = df[['reviewId', 'userName', 'score', 'reviewed_at',
                  'content', 'replyContent']].copy()
    df_all.columns = ['id', 'usuario', 'nota', 'data', 'texto', 'resposta_dev']

    df_filtered = df[df['match_keyword']].copy()
    df_filtered = df_filtered[['reviewId', 'userName', 'score',
                                'reviewed_at', 'content', 'replyContent']].copy()
    df_filtered.columns = ['id', 'usuario', 'nota', 'data', 'texto', 'resposta_dev']

    return df_all, df_filtered


def show_summary(df_all, df_filtered):
    """Exibe resumo estatístico da coleta."""
    print("\n" + "=" * 60)
    print("📊 RESUMO DA PoC")
    print("=" * 60)
    print(f"Total de reviews coletados: {len(df_all)}")
    print(f"Reviews sobre Pix por Voz:  {len(df_filtered)}")

    if len(df_all) > 0:
        print(f"\nPeríodo coberto: {df_all['data'].min().strftime('%Y-%m-%d')} → "
              f"{df_all['data'].max().strftime('%Y-%m-%d')}")
        print(f"Média geral de notas: {df_all['nota'].mean():.2f}")

    if len(df_filtered) > 0:
        print(f"\n--- Reviews filtrados (Pix por Voz) ---")
        print(f"Média de notas: {df_filtered['nota'].mean():.2f}")
        print(f"Distribuição de notas:")
        print(df_filtered['nota'].value_counts().sort_index().to_string())

        print(f"\n--- Amostra de textos encontrados ---")
        for _, row in df_filtered.head(5).iterrows():
            print(f"  [{row['nota']}⭐] {row['data'].strftime('%Y-%m-%d')}: "
                  f"{row['texto'][:120]}...")
    else:
        print("\n⚠️  Nenhum review encontrado com as keywords. "
              "Isso é esperado com amostra pequena.")
        print("   Na coleta completa (15k+ reviews), resultados devem aparecer.")


# ============================================================
# EXECUÇÃO
# ============================================================
if __name__ == "__main__":
    try:
        # 1. Coleta
        raw = fetch_reviews_poc(
            APP_ID, TARGET_START_DATE, KEYWORDS, MAX_PAGES, REVIEWS_PER_PAGE
        )

        # 2. Filtragem e estruturação
        df_all, df_filtered = filter_and_structure(raw, KEYWORDS)

        # 3. Exportação
        output_dir = Path("data/derived/poc")
        output_dir.mkdir(parents=True, exist_ok=True)
        df_all.to_csv(output_dir / "poc_reviews_all.csv", index=False)
        df_filtered.to_csv(output_dir / "poc_reviews_pix_voz.csv", index=False)
        print(f"\n💾 Arquivos exportados:")
        print(f"   - {output_dir / 'poc_reviews_all.csv'} ({len(df_all)} registros)")
        print(f"   - {output_dir / 'poc_reviews_pix_voz.csv'} ({len(df_filtered)} registros)")

        # 4. Resumo
        show_summary(df_all, df_filtered)

        print("\n🎉 PoC concluída com sucesso! Pipeline validado.")

    except Exception as e:
        print(f"\n❌ Erro no pipeline: {e}")
        import traceback
        traceback.print_exc()
