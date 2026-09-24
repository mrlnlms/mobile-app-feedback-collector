"""
filter.py - Filtragem Pós-Coleta de Reviews
=============================================
Filtra reviews já coletados por keywords do config.yaml.
Permite re-filtrar sem precisar re-coletar.

Uso:
    python filter.py --bank nubank
    python filter.py --bank nubank --keywords "pix voz" "comando de voz"
    python filter.py --all
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml


def load_config(config_path="config.yaml"):
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def filter_reviews(bank_key, bank_config, output_dir, custom_keywords=None):
    """Filtra reviews de um banco por keywords."""
    bank_name = bank_config.get("name", bank_key)
    output_dir = Path(output_dir) / bank_key

    raw_file = output_dir / "reviews_raw.parquet"
    if not raw_file.exists():
        print(f"⚠️  Arquivo não encontrado: {raw_file}")
        print(f"   Execute primeiro: python collector.py --bank {bank_key}")
        return None

    df = pd.read_parquet(raw_file)
    print(f"\n🏦 {bank_name}: {len(df):,} reviews carregados de {raw_file.name}")

    # Determinar keywords
    keywords = custom_keywords if custom_keywords else bank_config.get("keywords", [])

    if not keywords:
        print(f"   ℹ️  Sem keywords configuradas para {bank_name}. "
              f"Exportando todos os reviews.")
        df_filtered = df.copy()
    else:
        pattern = "|".join(keywords)
        df["match_keyword"] = df["texto_avaliacao"].str.contains(
            pattern, case=False, na=False
        )
        df_filtered = df[df["match_keyword"]].drop(columns=["match_keyword"]).copy()

        print(f"   🔍 Keywords: {', '.join(keywords)}")
        print(f"   📊 Reviews correspondentes: {len(df_filtered):,} "
              f"({len(df_filtered)/len(df)*100:.1f}%)")

    # Exportar CSV filtrado
    filtered_file = output_dir / "reviews_filtered.csv"
    df_filtered.to_csv(filtered_file, index=False)
    print(f"   💾 Salvo: {filtered_file}")

    # Resumo estatístico
    if len(df_filtered) > 0:
        print(f"\n   --- Resumo ---")
        print(f"   Período: {df_filtered['data_avaliacao'].min()} → "
              f"{df_filtered['data_avaliacao'].max()}")
        print(f"   Média de notas: {df_filtered['nota'].mean():.2f} "
              f"(σ = {df_filtered['nota'].std():.2f})")
        print(f"   Distribuição:")
        for nota, count in df_filtered["nota"].value_counts().sort_index().items():
            bar = "█" * int(count / len(df_filtered) * 30)
            pct = count / len(df_filtered) * 100
            print(f"     {nota}⭐ {bar} {count:,} ({pct:.1f}%)")

        # Amostra
        print(f"\n   --- Amostra (3 reviews) ---")
        for _, row in df_filtered.head(3).iterrows():
            texto = str(row["texto_avaliacao"])[:150]
            print(f"   [{row['nota']}⭐ {str(row['data_avaliacao'])[:10]}] {texto}...")

    # Salvar summary JSON
    summary = {
        "banco": bank_name,
        "total_raw": len(df),
        "total_filtered": len(df_filtered),
        "keywords_used": keywords,
        "filtered_at": datetime.now().isoformat(),
    }
    if len(df_filtered) > 0:
        summary["media_nota"] = round(df_filtered["nota"].mean(), 2)
        summary["distribuicao_notas"] = (
            df_filtered["nota"].value_counts().sort_index().to_dict()
        )

    import json
    summary_file = output_dir / "summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    print(f"   📄 Summary: {summary_file}")

    return df_filtered


def main():
    parser = argparse.ArgumentParser(
        description="Filtra reviews coletados por keywords."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bank", nargs="+", help="Banco(s) para filtrar")
    group.add_argument("--all", action="store_true", help="Filtrar todos os bancos")
    parser.add_argument("--keywords", nargs="+", help="Keywords customizadas (override)")
    parser.add_argument("--config", default="config.yaml", help="Arquivo de config")

    args = parser.parse_args()

    config = load_config(args.config)
    banks = config["banks"]
    output_dir = config["collection"]["output_dir"]

    if args.all:
        bank_keys = list(banks.keys())
    else:
        bank_keys = args.bank
        for key in bank_keys:
            if key not in banks:
                print(f"❌ Banco '{key}' não encontrado. "
                      f"Disponíveis: {', '.join(banks.keys())}")
                sys.exit(1)

    for bank_key in bank_keys:
        filter_reviews(bank_key, banks[bank_key], output_dir, args.keywords)

    print("\n✅ Filtragem concluída!")


if __name__ == "__main__":
    main()
