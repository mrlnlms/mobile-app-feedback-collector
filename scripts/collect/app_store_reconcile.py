"""Reconcile independent RSS and Web App Store archives by review ID.

RSS ``updated`` and Web ``date`` retain distinct raw/UTC fields. No universal
review date is inferred. This module never modifies the RSS or Web inputs.
"""

import argparse
import json
from collections import Counter
from pathlib import Path

import pandas as pd
import yaml

from scripts.collect.app_store_web import STAGING_ROOT, parse_utc, publish_frame, utc_now
from scripts.collect.storage import assert_available, save_json, sha256


RSS_ROOT = Path("data/raw/app_store")
DERIVED_ROOT = Path("data/derived/app_store")
RECEIPT_ROOT = Path("data/runs/app_store/reconciliation")
SNAPSHOT_ROOT = Path("data/runs/app_store/snapshots_reconciled")


def raw_rss_index(captures):
    """Index only authentic RSS updated strings; verify each captured response."""
    found = {}
    for run in map(Path, captures):
        requests = run / "requests.json"
        journal = run / "pages.jsonl"
        if journal.is_file():
            entries = [json.loads(line) for line in journal.read_text(encoding="utf-8").splitlines()]
        elif requests.is_file():
            entries = json.loads(requests.read_text(encoding="utf-8"))
        else:
            raise FileNotFoundError(f"Journal RSS ausente: {run}")
        for entry in entries:
            if entry.get("http_status") != 200 or not entry.get("body_file"):
                continue
            body = run / entry["body_file"]
            if not body.is_file() or sha256(body) != entry.get("body_sha256"):
                raise ValueError(f"Payload RSS ausente ou hash divergente: {body}")
            payload = json.loads(body.read_bytes())
            for review in payload.get("feed", {}).get("entry", []):
                if "im:rating" not in review:
                    continue
                review_id = review["id"]["label"]
                raw = review["updated"]["label"]
                parse_utc(raw)
                observation = {"raw": raw, "run": str(run), "body_file": entry["body_file"],
                               "body_sha256": entry["body_sha256"]}
                if review_id in found and found[review_id]["raw"] != raw:
                    raise ValueError(f"RSS updated bruto conflitante para {review_id}")
                found.setdefault(review_id, observation)
    return found


def build_reconciled(rss_path, web_path, rss_captures=()):
    rss_path, web_path = Path(rss_path), Path(web_path)
    assert_available(rss_path)
    assert_available(web_path)
    if not rss_path.is_file() or not web_path.is_file():
        raise FileNotFoundError("Acervos RSS e Web são necessários para reconciliar")
    rss = pd.read_parquet(rss_path)
    web = pd.read_parquet(web_path)
    for name, frame in (("RSS", rss), ("Web", web)):
        if frame.empty or frame.id_review.isna().any() or frame.id_review.duplicated().any():
            raise ValueError(f"Acervo {name} vazio ou com IDs inválidos")
    required_web = {"web_date_raw", "web_date_utc", "web_body_sha256", "web_body_file", "web_run"}
    if not required_web.issubset(web.columns):
        raise ValueError("Acervo Web sem datas ou proveniência")
    raw = raw_rss_index(rss_captures)
    rss_rows = {str(row.id_review): row for row in rss.itertuples(index=False)}
    web_rows = {str(row.id_review): row for row in web.itertuples(index=False)}
    records = []
    for review_id in sorted(set(rss_rows) | set(web_rows)):
        r = rss_rows.get(review_id)
        w = web_rows.get(review_id)
        captured = raw.get(review_id) if r is not None else None
        base_raw = getattr(r, "rss_updated_raw", None) if r is not None else None
        if pd.isna(base_raw):
            base_raw = None
        raw_value = base_raw if base_raw is not None else captured["raw"] if captured else None
        raw_origin = "rss_base" if base_raw is not None else "rss_capture" if captured else "unavailable"
        if base_raw is not None:
            captured = None
        rss_utc = pd.Timestamp(r.data_avaliacao).tz_convert("UTC") if r is not None else pd.NaT
        web_utc = pd.Timestamp(w.web_date_utc).tz_convert("UTC") if w is not None else pd.NaT
        if w is not None and parse_utc(w.web_date_raw) != web_utc.to_pydatetime():
            raise ValueError(f"Web raw/UTC divergentes para {review_id}")
        if raw_value is not None and parse_utc(raw_value) != rss_utc.to_pydatetime():
            raise ValueError(f"RSS raw/UTC divergentes para {review_id}")
        if r is not None and w is not None:
            presence = "rss_web"
        elif w is not None:
            presence = "web_only"
        else:
            presence = "rss_only"
        records.append({
            "id_review": review_id, "source_presence": presence,
            "rss_present": r is not None, "web_present": w is not None,
            "apple_app_id": str(r.apple_app_id) if r is not None else str(w.apple_app_id),
            "pais": r.pais if r is not None else w.pais,
            "rss_updated_raw": raw_value,
            "rss_updated_utc": rss_utc,
            "rss_updated_raw_available": raw_value is not None,
            "rss_updated_raw_origin": raw_origin,
            "rss_raw_capture_run": captured["run"] if captured else None,
            "rss_raw_body_file": captured["body_file"] if captured else None,
            "rss_raw_body_sha256": captured["body_sha256"] if captured else None,
            "rss_usuario": r.usuario if r is not None else None,
            "rss_nota": int(r.nota) if r is not None else None,
            "rss_titulo": r.titulo if r is not None else None,
            "rss_texto_avaliacao": r.texto_avaliacao if r is not None else None,
            "rss_versao_app": r.versao_app if r is not None else None,
            "rss_sorts_encontrados": r.sorts_encontrados if r is not None else None,
            "web_date_raw": w.web_date_raw if w is not None else None,
            "web_date_utc": web_utc,
            "web_usuario": w.web_usuario if w is not None else None,
            "web_nota": int(w.web_nota) if w is not None else None,
            "web_titulo": w.web_titulo if w is not None else None,
            "web_texto_avaliacao": w.web_texto_avaliacao if w is not None else None,
            "web_is_edited": bool(w.web_is_edited) if w is not None and pd.notna(w.web_is_edited) else None,
            "web_offset": int(w.web_offset) if w is not None else None,
            "web_run": w.web_run if w is not None else None,
            "web_body_file": w.web_body_file if w is not None else None,
            "web_body_sha256": w.web_body_sha256 if w is not None else None,
            "rss_web_time_delta_seconds": int((rss_utc - web_utc).total_seconds()) if r is not None and w is not None else None,
            "rss_web_text_equal": r.texto_avaliacao == w.web_texto_avaliacao if r is not None and w is not None else None,
            "rss_web_title_equal": r.titulo == w.web_titulo if r is not None and w is not None else None,
            "rss_web_rating_equal": int(r.nota) == int(w.web_nota) if r is not None and w is not None else None,
        })
    result = pd.DataFrame(records)
    result["rss_updated_utc"] = pd.to_datetime(result["rss_updated_utc"], utc=True)
    result["web_date_utc"] = pd.to_datetime(result["web_date_utc"], utc=True)
    if result.id_review.duplicated().any() or len(result) != len(set(rss_rows) | set(web_rows)):
        raise ValueError("Falha na reconciliação por ID")
    return result


def reconcile(bank, rss_captures=()):
    rss_path = RSS_ROOT / bank / "reviews_raw.parquet"
    web_path = RSS_ROOT / bank / "reviews_web.parquet"
    rss_before = sha256(rss_path)
    web_before = sha256(web_path)
    frame = build_reconciled(rss_path, web_path, rss_captures)
    if sha256(rss_path) != rss_before or sha256(web_path) != web_before:
        raise RuntimeError("Acervo fonte mudou durante a reconciliação")
    output = DERIVED_ROOT / bank / "reviews_reconciled.parquet"
    receipt = publish_frame(frame, output, STAGING_ROOT / bank / "reconciliation", SNAPSHOT_ROOT / bank)
    if sha256(rss_path) != rss_before or sha256(web_path) != web_before:
        raise RuntimeError("Acervo fonte mudou durante a publicação")
    counts = Counter(frame.source_presence)
    receipt.update({"bank": bank, "published_at": utc_now(), "rss_source": str(rss_path),
                    "rss_source_sha256": rss_before, "web_source": str(web_path),
                    "web_source_sha256": web_before, "rss_captures": [str(x) for x in rss_captures],
                    "rss_web": counts["rss_web"], "web_only": counts["web_only"],
                    "rss_only": counts["rss_only"],
                    "rss_updated_raw_available": int(frame.rss_updated_raw_available.sum()),
                    "rss_updated_raw_unavailable": int(frame.rss_present.sum() - frame.rss_updated_raw_available.sum()),
                    "rss_updated_raw_origins": dict(Counter(frame.rss_updated_raw_origin)),
                    "shared_content_differences": {
                        "text": int(frame.rss_web_text_equal.eq(False).sum()),
                        "title": int(frame.rss_web_title_equal.eq(False).sum()),
                        "rating": int(frame.rss_web_rating_equal.eq(False).sum()),
                    },
                    "time_delta_seconds": dict(Counter(str(x) for x in frame.rss_web_time_delta_seconds.dropna().astype(int)))})
    save_json(RECEIPT_ROOT / bank / "state.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", required=True)
    parser.add_argument("--rss-capture", type=Path, action="append", default=[])
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    if args.bank not in config["banks"]:
        parser.error(f"Banco desconhecido: {args.bank}")
    print(json.dumps(reconcile(args.bank, args.rss_capture), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
