"""Apoio temporário para analysis/google-play-descritiva-v2.qmd.

Carrega e valida as bases da Google Play, e calcula as tabelas usadas no relatório.
Lê somente `nota` e `data_avaliacao`; os comentários nunca são carregados.
Fica em analysis/ de propósito: o foco do projeto é o coletor.
"""
from pathlib import Path

import pandas as pd
import yaml

START = pd.Timestamp("2025-01-01")


def find_root():
    """Sobe a partir da pasta atual até achar o config.yaml do repositório."""
    root = next(
        (p for p in (Path.cwd(), *Path.cwd().parents) if (p / "config.yaml").is_file()),
        None,
    )
    if root is None:
        raise FileNotFoundError("Execute o QMD dentro do repositório, que contém config.yaml.")
    return root


def load_reviews(start=START):
    """Retorna (reviews, audit).

    reviews: avaliações válidas desde `start`, com colunas Banco, nota, data_avaliacao.
    audit: uma linha por banco com totais, datas e registros descartados.
    """
    root = find_root()
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    raw_dir = root / config["collection"]["output_dir"]

    frames, audit = [], []
    for slug, bank in config["banks"].items():
        path = raw_dir / slug / "reviews_raw.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"Base local ausente: {path.relative_to(root)}")

        # A seleção de colunas impede a leitura dos comentários.
        frame = pd.read_parquet(path, columns=["nota", "data_avaliacao"])
        frame["data_avaliacao"] = pd.to_datetime(frame["data_avaliacao"], errors="coerce")

        invalid_date = frame["data_avaliacao"].isna()
        invalid_score = ~invalid_date & ~frame["nota"].between(1, 5)
        valid = ~(invalid_date | invalid_score)
        in_scope = valid & frame["data_avaliacao"].ge(start)
        before_start = valid & frame["data_avaliacao"].lt(start)

        scoped = frame.loc[in_scope].copy()
        scoped["Banco"] = bank["name"]
        frames.append(scoped)
        audit.append({
            "Banco": bank["name"],
            "Registros no arquivo": len(frame),
            "No recorte": int(in_scope.sum()),
            "Primeira avaliação": scoped["data_avaliacao"].min(),
            "Última avaliação": scoped["data_avaliacao"].max(),
            "Antes do recorte": int(before_start.sum()),
            "Nota inválida": int(invalid_score.sum()),
            "Data inválida": int(invalid_date.sum()),
        })

    reviews = pd.concat(frames, ignore_index=True)
    if reviews.empty:
        raise ValueError("Nenhuma avaliação válida encontrada no recorte.")
    return reviews, pd.DataFrame(audit)


def monthly_counts(reviews):
    """Avaliações por mês (linhas) e banco (colunas); mês sem registro vira 0."""
    month = reviews["data_avaliacao"].dt.to_period("M")
    counts = reviews.groupby([month, "Banco"]).size().unstack("Banco", fill_value=0)
    full_index = pd.period_range(counts.index.min(), counts.index.max(), freq="M")
    return counts.reindex(full_index, fill_value=0)


def monthly_mean_score(reviews, min_reviews=30):
    """Nota média por mês e banco; meses com poucas avaliações ficam vazios (NaN)."""
    month = reviews["data_avaliacao"].dt.to_period("M")
    grouped = reviews.groupby([month, "Banco"])["nota"].agg(["mean", "size"])
    means = grouped["mean"].where(grouped["size"] >= min_reviews).unstack("Banco")
    return means.reindex(monthly_counts(reviews).index)


def complete_months(reviews):
    """Meses inteiros do recorte: o último mês observado fica de fora (pode estar incompleto)."""
    counts = monthly_counts(reviews)
    return counts.index[:-1]


def score_summary(reviews, counts):
    """Tabela por banco: avaliações, média/mês, nota média e % de 1, 2-4 e 5 estrelas."""
    months = complete_months(reviews)
    summary = reviews.groupby("Banco")["nota"].agg(Avaliações="size", Nota_média="mean")
    summary["Média/mês"] = counts.loc[months].mean().round(0).astype(int)
    shares = pd.crosstab(reviews["Banco"], reviews["nota"], normalize="index") * 100
    summary["1 estrela (%)"] = shares[1]
    summary["2 a 4 estrelas (%)"] = shares[[2, 3, 4]].sum(axis=1)
    summary["5 estrelas (%)"] = shares[5]
    return summary.rename(columns={"Nota_média": "Nota média"})


def br_int(value):
    return f"{int(value):,}".replace(",", ".")


def br_num(value, decimals=1):
    return f"{value:.{decimals}f}".replace(".", ",")


def profile_archive():
    """Descreve o que há nas bases, sem aplicar o recorte de datas e sem exibir comentários.

    Retorna (por_banco, campos): uma linha por banco com tamanho e período, e uma linha por
    campo com tipo, quantos registros estão preenchidos e valores mínimo e máximo quando numéricos.
    """
    root = find_root()
    config = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
    raw_dir = root / config["collection"]["output_dir"]

    banks, filled, totals, dtypes = [], {}, 0, {}
    ids, ranges = 0, {"nota": [], "thumbs_up": []}
    for slug, bank in config["banks"].items():
        path = raw_dir / slug / "reviews_raw.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"Base local ausente: {path.relative_to(root)}")
        frame = pd.read_parquet(path)
        dates = pd.to_datetime(frame["data_avaliacao"], errors="coerce")
        banks.append({
            "Banco": bank["name"],
            "Registros": len(frame),
            "Primeira avaliação": dates.min(),
            "Última avaliação": dates.max(),
            "Dias cobertos": (dates.max() - dates.min()).days + 1,
        })
        totals += len(frame)
        ids += frame["id_review"].nunique()
        for column in frame.columns:
            dtypes[column] = str(frame[column].dtype)
            series = frame[column]
            has_value = series.notna()
            if series.dtype == object or str(series.dtype) == "str":
                has_value &= series.astype(str).str.strip().ne("")
            filled[column] = filled.get(column, 0) + int(has_value.sum())
        for column in ranges:
            ranges[column].append((frame[column].min(), frame[column].max()))

    fields = pd.DataFrame({
        "Campo": list(filled),
        "Tipo": [dtypes[c] for c in filled],
        "Preenchido (%)": [filled[c] / totals * 100 for c in filled],
    })
    fields["Mínimo"] = [min(v[0] for v in ranges[c]) if c in ranges else None for c in filled]
    fields["Máximo"] = [max(v[1] for v in ranges[c]) if c in ranges else None for c in filled]
    return pd.DataFrame(banks), fields, {"registros": totals, "ids_unicos": ids}
