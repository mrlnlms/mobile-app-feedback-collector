"""Apply the Nubank Pix/voice search strategy to collected bank review bases.

Nubank is excluded from --all because its analysis is already complete. Use
--bank nubank only when intentionally reproducing that bank's searches.
"""

import argparse
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(ch for ch in value if not unicodedata.combining(ch)).lower()


def phrase_pattern(phrases):
    return re.compile("(?:" + "|".join(re.escape(normalize(p)) for p in phrases) + ")")


QUERIES = {
    "keywords_originais": [
        "pix por voz", "pix voz", "pix com voz", "comando de voz", "falar pix",
        "enviar pix falando", "audio pix", "ditado pix", "reconhecimento de voz",
    ],
    "voz_fala_audio_amplo": [
        "voz", "falar", "falando", "audio", "microfone", "ditado", "comando de voz",
    ],
    "termos_feature_acessibilidade": [
        "pix por voz", "comando de voz", "assistente de voz", "talkback",
        "deficiente visual", "deficiencia visual", "acessibilidade", "pix por audio",
    ],
    "pix": ["pix"],
    "voz": ["voz"],
    "pix_voz": ["pix", "voz"],
    "pix_audio": ["pix", "audio"],
    "pix_falar": ["pix", "falar"],
    "pix_whatsapp": ["pix", "whatsapp"],
    "pix_acessibilidade": ["pix", "acessibilidade", "deficiente visual", "talkback"],
}


def query_masks(texts):
    normalized = texts.map(normalize)
    masks = {}
    for name, terms in QUERIES.items():
        if name.startswith("pix_") and name != "pix":
            patterns = [phrase_pattern([term]) for term in terms]
            if name == "pix_acessibilidade":
                masks[name] = normalized.map(
                    lambda value, patterns=patterns: bool(patterns[0].search(value))
                    and any(pattern.search(value) for pattern in patterns[1:])
                )
            else:
                masks[name] = normalized.map(
                    lambda value, patterns=patterns: all(pattern.search(value) for pattern in patterns)
                )
        else:
            pattern = phrase_pattern(terms)
            masks[name] = normalized.map(lambda value, pattern=pattern: bool(pattern.search(value)))
    return normalized, pd.DataFrame(masks, index=texts.index)


def load_config(path):
    with open(path, encoding="utf-8") as stream:
        return yaml.safe_load(stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--bank", nargs="+", help="Banco(s) a analisar")
    selection.add_argument("--all", action="store_true", help="Analisar as bases, exceto Nubank")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--from-date", help="Data inicial inclusiva YYYY-MM-DD")
    args = parser.parse_args()

    config = load_config(args.config)
    banks = config["banks"]
    keys = list(banks) if args.all else (args.bank or [key for key in banks if key != "nubank"])
    if args.all:
        keys = [key for key in keys if key != "nubank"]
    out_root = Path(config["collection"]["output_dir"])
    report = {
        "metodo": "mesmas familias de busca documentadas para Nubank; busca lexical, triagem humana necessária",
        "data_inicial": args.from_date,
        "nubank_incluido": "nubank" in keys,
        "executado_em": datetime.now().isoformat(timespec="seconds"),
        "bancos": {},
    }

    for key in keys:
        if key not in banks:
            parser.error(f"Banco desconhecido: {key}")
        source = out_root / key / "reviews_raw.parquet"
        if not source.exists():
            parser.error(f"Base ausente: {source}")
        df = pd.read_parquet(source)
        if "texto_avaliacao" not in df:
            parser.error(f"Coluna texto_avaliacao ausente em {source}")
        if args.from_date:
            dates = pd.to_datetime(df["data_avaliacao"], errors="coerce")
            df = df.loc[dates >= pd.Timestamp(args.from_date)].copy()

        normalized, masks = query_masks(df["texto_avaliacao"])
        # Candidate pool mirrors the intersection and phrase searches used to
        # find the three Nubank records, while retaining the exact hit trace.
        pool_queries = ["keywords_originais", "pix_voz", "pix_audio", "pix_whatsapp", "pix_acessibilidade"]
        candidate_mask = masks[pool_queries].any(axis=1)
        candidates = df.loc[candidate_mask].copy()
        hit_matrix = masks.loc[candidate_mask, pool_queries]
        candidates["consultas_correspondentes"] = [
            ";".join(hit_matrix.columns[row].tolist())
            for row in hit_matrix.to_numpy(dtype=bool)
        ]
        candidates["triagem"] = "revisao_manual_necessaria"
        candidates.to_csv(out_root / key / "pix_voz_audio_candidates.csv", index=False)

        counts = {name: int(mask.sum()) for name, mask in masks.items()}
        report["bancos"][key] = {
            "nome": banks[key].get("name", key),
            "arquivo_fonte": str(source),
            "reviews_no_periodo": int(len(df)),
            "contagens_por_busca": counts,
            "candidatos_unicos_para_triagem": int(candidate_mask.sum()),
            "csv_candidatos": str(out_root / key / "pix_voz_audio_candidates.csv"),
        }
        print(f"{key}: {len(df):,} reviews | candidatos {int(candidate_mask.sum())} | "
              + ", ".join(f"{name}={count}" for name, count in counts.items()))

    path = out_root / "pix_voz_audio_search.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Resumo por banco e consulta: {path}")


if __name__ == "__main__":
    main()
