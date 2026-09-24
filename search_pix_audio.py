"""Export auditable lexical searches for Pix by voice, audio, chat and WhatsApp.

Reads only existing Parquet bases. Every matching review is exported in full;
the script never decides whether a review is relevant to the research question.
"""

import argparse
import hashlib
import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml


PIX = re.compile(r"\bpix\b")
QUERIES = {
    "pix_voz_audio": r"\b(?:voz|audio|microfone|ditad[oa]|ditar|vocal)\b",
    "pix_fala": r"\b(?:falar|falando|fala|falado|falo|falei)\b",
    "pix_whatsapp": r"\b(?:whats\s?app|whatsap|zap|wpp)\b",
    "pix_chat_mensagem": (
        r"\b(?:chat|conversa|conversando|mensag(?:em|ens)|"
        r"assistente virtual|chat\s?bot)\b"
    ),
}
COMPILED = {name: re.compile(pattern) for name, pattern in QUERIES.items()}
DIRECT = re.compile(
    r"\bpix\s+(?:por|via|pelo|com|no|em formato de)\s+"
    r"(?:voz|audio|whats\s?app|whatsap|zap|chat|mensagem|conversa)\b"
)
CHAT_NEAR = re.compile(
    r"\bpix\b.{0,120}\b(?:chat|conversa|assistente)\b|"
    r"\b(?:chat|conversa|assistente)\b.{0,120}\bpix\b"
)
AUTO_COLUMNS = [
    "banco", "app_id", "id_review", "data_avaliacao", "nota",
    "consultas", "termos_encontrados", "distancia_minima_caracteres",
    "frase_direta", "texto_avaliacao", "arquivo_fonte", "na_busca_atual",
]


def normalize(value):
    value = unicodedata.normalize("NFKD", str(value or "").lower())
    return "".join(char for char in value if not unicodedata.combining(char))


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def minimum_gap(pix_matches, topic_matches):
    return min(
        max(0, max(pix.start() - topic.end(), topic.start() - pix.end()))
        for pix in pix_matches
        for topic in topic_matches
    )


def preserve_triage(path, matches):
    current = matches.copy()
    current["na_busca_atual"] = 1
    if path.exists():
        old = pd.read_csv(path, dtype=str, keep_default_na=False)
        if "id_review" not in old:
            raise ValueError(f"Fila sem id_review: {path}")
        manual_columns = [name for name in old if name not in AUTO_COLUMNS]
        manual = old[["id_review", *manual_columns]].drop_duplicates("id_review")
        current = current.merge(manual, on="id_review", how="left")
        retired = old.loc[~old["id_review"].isin(current["id_review"])].copy()
        retired["na_busca_atual"] = 0
        current = pd.concat([current, retired], ignore_index=True)
    else:
        current["decisao_revisor"] = ""
        current["nota_revisor"] = ""
    temporary = path.with_name(path.name + ".tmp")
    current.to_csv(temporary, index=False)
    os.replace(temporary, path)


def search_bank(bank, bank_config, source, destination):
    source_hash = sha256(source)
    df = pd.read_parquet(
        source, columns=["id_review", "data_avaliacao", "nota", "texto_avaliacao"]
    )
    if df["id_review"].isna().any() or df["id_review"].duplicated().any():
        raise ValueError(f"IDs ausentes ou duplicados em {source}")
    normalized = df["texto_avaliacao"].map(normalize)
    pix_mask = normalized.str.contains(PIX, na=False)
    masks = {
        name: pix_mask & normalized.str.contains(pattern, na=False)
        for name, pattern in COMPILED.items()
    }
    union = pd.DataFrame(masks).any(axis=1)
    results = []
    for index, review in df.loc[union].iterrows():
        text = normalized.at[index]
        pix_matches = list(PIX.finditer(text))
        names = [name for name, mask in masks.items() if mask.at[index]]
        topic_matches = [
            match for name in names for match in COMPILED[name].finditer(text)
        ]
        terms = sorted({match.group(0) for match in topic_matches})
        results.append({
            "banco": bank,
            "app_id": bank_config["app_id"],
            "id_review": review["id_review"],
            "data_avaliacao": review["data_avaliacao"],
            "nota": review["nota"],
            "consultas": ";".join(names),
            "termos_encontrados": ";".join(terms),
            "distancia_minima_caracteres": minimum_gap(pix_matches, topic_matches),
            "frase_direta": int(bool(DIRECT.search(text))),
            "texto_avaliacao": review["texto_avaliacao"],
            "arquivo_fonte": str(source),
        })
    matches = pd.DataFrame(results, columns=AUTO_COLUMNS[:-1])
    if not matches.empty:
        matches = matches.sort_values(
            ["frase_direta", "distancia_minima_caracteres", "data_avaliacao"],
            ascending=[False, True, False],
        )
    bank_dir = destination / bank
    bank_dir.mkdir(parents=True, exist_ok=True)
    matches.to_csv(bank_dir / "matches.csv", index=False)
    first_read = matches.loc[
        matches["frase_direta"].eq(1)
        | matches["consultas"].str.contains("pix_voz_audio|pix_whatsapp", na=False)
    ]
    first_read.to_csv(bank_dir / "primeira_leitura.csv", index=False)
    chat_near = matches.loc[
        matches["texto_avaliacao"].map(
            lambda value: bool(CHAT_NEAR.search(normalize(value)))
        )
    ]
    chat_near.to_csv(bank_dir / "chat_conversa_proximo.csv", index=False)
    preserve_triage(bank_dir / "triagem.csv", matches)

    dates = pd.to_datetime(df["data_avaliacao"])
    cutoff = pd.Timestamp(bank_config["start_date"])
    return {
        "banco": bank,
        "app_id": bank_config["app_id"],
        "arquivo_fonte": str(source),
        "sha256_fonte": source_hash,
        "avaliacoes_na_base": len(df),
        "data_mais_antiga": dates.min().date().isoformat(),
        "data_mais_recente": dates.max().date().isoformat(),
        "data_inicial_configurada": cutoff.date().isoformat(),
        "atingiu_data_inicial": bool(dates.min().normalize() <= cutoff),
        "candidatos_unicos": len(matches),
        "primeira_leitura": len(first_read),
        "chat_conversa_proximo": len(chat_near),
        "contagens_por_consulta": {name: int(mask.sum()) for name, mask in masks.items()},
        "frases_diretas": int(matches["frase_direta"].sum()) if len(matches) else 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--output", default="output/pix_audio_review")
    parser.add_argument("--bank", nargs="+", help="Bancos específicos; padrão: todos")
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    banks = config["banks"]
    keys = args.bank or list(banks)
    unknown = [key for key in keys if key not in banks]
    if unknown:
        parser.error(f"Banco(s) desconhecido(s): {', '.join(unknown)}")
    destination = Path(args.output)
    destination.mkdir(parents=True, exist_ok=True)
    summaries = []
    for key in keys:
        source = Path(config["collection"]["output_dir"]) / key / "reviews_raw.parquet"
        if not source.exists():
            parser.error(f"Base ausente: {source}")
        result = search_bank(key, banks[key], source, destination)
        summaries.append(result)
        print(f"{key}: {result['candidatos_unicos']} candidatos; "
              f"cobertura desde {result['data_mais_antiga']}")
    first_read_frames = [
        pd.read_csv(destination / key / "primeira_leitura.csv") for key in keys
    ]
    pd.concat(first_read_frames, ignore_index=True).to_csv(
        destination / "primeira_leitura_todos.csv", index=False
    )
    manifest = {
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "criterio": "mesmo comentario contem a palavra pix e um termo da consulta",
        "normalizacao": "minusculas e sem acentos; busca literal com limites de palavra",
        "consultas": QUERIES,
        "frase_direta": DIRECT.pattern,
        "chat_conversa_proximo": CHAT_NEAR.pattern,
        "bancos": summaries,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary_rows = []
    for bank in summaries:
        for name, count in bank["contagens_por_consulta"].items():
            summary_rows.append({
                "banco": bank["banco"], "consulta": name, "correspondencias": count,
                "candidatos_unicos_banco": bank["candidatos_unicos"],
                "avaliacoes_na_base": bank["avaliacoes_na_base"],
                "data_mais_antiga": bank["data_mais_antiga"],
                "data_inicial_configurada": bank["data_inicial_configurada"],
                "atingiu_data_inicial": bank["atingiu_data_inicial"],
            })
    pd.DataFrame(summary_rows).to_csv(destination / "consultas.csv", index=False)


if __name__ == "__main__":
    main()
