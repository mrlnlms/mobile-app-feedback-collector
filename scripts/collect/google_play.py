"""
collector.py - Pipeline de Coleta de Reviews da Play Store
==========================================================
Coleta avaliações de apps bancários com resiliência:
- Retry com backoff exponencial
- Checkpoint incremental (retomada automática)
- Early stopping por data
- Relatório final com erros e estatísticas

Uso:
    venv/bin/python -m scripts.collect.google_play --bank nubank
    venv/bin/python -m scripts.collect.google_play --all
"""

import argparse
import json
import os
import pickle
import shutil
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml
from google_play_scraper import Sort, reviews
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)
import logging

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("collector")


# ============================================================
# CONFIGURAÇÃO
# ============================================================
def load_config(config_path="config.yaml"):
    """Carrega configuração do YAML."""
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def write_final_base(new_reviews, output_file, start_date, snapshot_dir=None):
    """Archive the old Parquet, then publish the merged base atomically."""
    frames = [new_reviews]
    if output_file.exists():
        frames.append(pd.read_parquet(output_file))
    merged = pd.concat(frames, ignore_index=True)
    dates = pd.to_datetime(merged["data_avaliacao"], errors="coerce")
    if dates.isna().any():
        raise ValueError(f"Data inválida em {output_file}")
    merged = merged.loc[dates >= start_date]
    merged = merged.drop_duplicates(subset="id_review", keep="first")
    merged = merged.sort_values("data_avaliacao", ascending=False).reset_index(drop=True)
    temporary_file = output_file.with_name("reviews_raw.tmp.parquet")
    merged.to_parquet(temporary_file, index=False)
    if output_file.exists():
        snapshots = Path(snapshot_dir) if snapshot_dir is not None else output_file.parent / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snapshot = snapshots / f"reviews_raw_before_recollect_{stamp}.parquet"
        shutil.copy2(output_file, snapshot)
        logger.info(f"📦 Base anterior preservada: {snapshot}")
    os.replace(temporary_file, output_file)
    logger.info(f"💾 Base publicada: {output_file} ({len(merged):,} IDs únicos)")
    return merged


# ============================================================
# CHECKPOINT
# ============================================================
class CheckpointManager:
    """Gerencia salvamento e carregamento de progresso parcial."""

    def __init__(self, checkpoint_dir, bank_key):
        self.dir = Path(checkpoint_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.filepath = self.dir / f"{bank_key}_checkpoint.parquet"
        self.meta_filepath = self.dir / f"{bank_key}_checkpoint_meta.pkl"

    def exists(self):
        return self.filepath.exists() and self.meta_filepath.exists()

    def save(self, reviews_list, continuation_token, batch_count):
        """Salva progresso parcial."""
        df = pd.DataFrame(reviews_list)
        df.to_parquet(self.filepath, index=False)
        meta = {
            "continuation_token": continuation_token,
            "batch_count": batch_count,
            "total_reviews": len(reviews_list),
            "saved_at": datetime.now().isoformat(),
        }
        with open(self.meta_filepath, "wb") as f:
            pickle.dump(meta, f)
        logger.info(
            f"💾 Checkpoint salvo: {len(reviews_list)} reviews (lote #{batch_count})"
        )

    def load(self):
        """Carrega progresso parcial."""
        df = pd.read_parquet(self.filepath)
        with open(self.meta_filepath, "rb") as f:
            meta = pickle.load(f)
        logger.info(
            f"♻️  Checkpoint carregado: {meta['total_reviews']} reviews "
            f"(lote #{meta['batch_count']}, salvo em {meta['saved_at']})"
        )
        return df.to_dict("records"), meta["continuation_token"], meta["batch_count"]

    def cleanup(self):
        """Remove checkpoint após coleta completa."""
        if self.filepath.exists():
            self.filepath.unlink()
        if self.meta_filepath.exists():
            self.meta_filepath.unlink()
        logger.info("🗑️  Checkpoint removido (coleta completa)")


# ============================================================
# COLETA COM RETRY
# ============================================================
class CoverageUnconfirmed(Exception):
    """The source stopped before the requested start date; cause is unknown."""


def make_fetch_batch(max_retries, start_date):
    """Cria a função de fetch com retry configurável."""

    @retry(
        wait=wait_exponential(multiplier=1, min=4, max=60),
        stop=stop_after_attempt(max_retries),
        retry=retry_if_exception_type(Exception),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=True,
    )
    def fetch_batch(app_id, batch_size, continuation_token):
        """Busca um lote de reviews com retry automático."""
        result, token = reviews(
            app_id,
            lang="pt",
            country="br",
            sort=Sort.NEWEST,
            count=batch_size,
            continuation_token=continuation_token,
        )
        # google-play-scraper can swallow an HTTP/parsing error and return an
        # empty page (or a missing token) as if pagination had finished.
        if not result or (
            getattr(token, "token", None) is None
            and not any(review["at"] < start_date for review in result)
        ):
            raise CoverageUnconfirmed(
                "Fonte retornou página vazia ou sem token antes da data inicial; "
                "pode ser fim dos dados disponíveis ou falha da biblioteca"
            )
        return result, token

    return fetch_batch


# ============================================================
# PIPELINE DE COLETA
# ============================================================
def collect_bank(bank_key, bank_config, global_config):
    """
    Coleta reviews de um banco específico.
    Retorna (DataFrame, report_dict).
    """
    app_id = bank_config["app_id"]
    bank_name = bank_config.get("name", bank_key)
    start_date = datetime.strptime(bank_config["start_date"], "%Y-%m-%d")

    batch_size = global_config["batch_size"]
    delay = global_config["delay_seconds"]
    checkpoint_every = global_config["checkpoint_every"]
    max_retries = global_config["max_retries"]
    output_dir = Path(global_config["output_dir"]) / bank_key
    output_dir.mkdir(parents=True, exist_ok=True)

    fetch_batch = make_fetch_batch(max_retries, start_date)
    checkpoint = CheckpointManager(global_config["checkpoint_dir"], bank_key)

    # Report de execução
    report = {
        "banco": bank_name,
        "app_id": app_id,
        "data_inicio_filtro": start_date.strftime("%Y-%m-%d"),
        "inicio_execucao": datetime.now().isoformat(),
        "fim_execucao": None,
        "total_reviews_coletados": 0,
        "total_lotes": 0,
        "erros": [],
        "checkpoints_salvos": 0,
        "early_stop": False,
        "early_stop_date": None,
        "data_mais_antiga_coletada": None,
        "data_mais_recente_coletada": None,
        "status": "em andamento",
    }

    # ---- Header ----
    print()
    print("=" * 60)
    logger.info(f"🏦 Iniciando coleta: {bank_name} ({app_id})")
    logger.info(f"📅 Período: {start_date.strftime('%Y-%m-%d')} → hoje")
    logger.info(f"⚙️  Lote: {batch_size} | Delay: {delay}s | Retries: {max_retries}")
    print("=" * 60)

    # ---- Retomar de checkpoint se existir ----
    all_reviews = []
    continuation_token = None
    batch_count = 0

    if checkpoint.exists():
        all_reviews, continuation_token, batch_count = checkpoint.load()
        if getattr(continuation_token, "token", None) is None:
            raise RuntimeError(
                f"Checkpoint sem token de retomada: {checkpoint.meta_filepath}"
            )

    # ---- Loop de coleta ----
    stop_triggered = False

    while not stop_triggered:
        try:
            result, continuation_token = fetch_batch(
                app_id, batch_size, continuation_token
            )
        except CoverageUnconfirmed as e:
            logger.warning(f"⚠️  Cobertura não confirmada: {e}")
            report["erros"].append(str(e))
            report["status"] = "cobertura não confirmada"
            if all_reviews and getattr(continuation_token, "token", None) is not None:
                checkpoint.save(all_reviews, continuation_token, batch_count)
            break
        except Exception as e:
            error_msg = f"Erro irrecuperável no lote #{batch_count + 1}: {e}"
            logger.error(f"❌ {error_msg}")
            report["erros"].append(error_msg)
            # Salvar o que temos antes de parar
            if all_reviews and getattr(continuation_token, "token", None) is not None:
                checkpoint.save(all_reviews, continuation_token, batch_count)
            report["status"] = "interrompido por erro"
            break

        if not result:
            error_msg = (
                "Fonte retornou lote vazio antes da data inicial "
                f"{start_date:%Y-%m-%d}; causa não confirmada, base preservada"
            )
            logger.warning(error_msg)
            report["erros"].append(error_msg)
            report["status"] = "cobertura não confirmada"
            if all_reviews and getattr(continuation_token, "token", None) is not None:
                checkpoint.save(all_reviews, continuation_token, batch_count)
            break

        batch_count += 1
        added_this_batch = 0

        for review in result:
            review_date = review["at"]

            if review_date < start_date:
                stop_triggered = True
                report["early_stop"] = True
                report["early_stop_date"] = review_date.strftime("%Y-%m-%d")
                logger.info(
                    f"🛑 Early stop! Review de {review_date.strftime('%Y-%m-%d')} "
                    f"anterior a {start_date.strftime('%Y-%m-%d')}"
                )
                break

            all_reviews.append(review)
            added_this_batch += 1

        logger.info(
            f"   Lote #{batch_count:>3} | "
            f"+{added_this_batch} reviews | "
            f"Total: {len(all_reviews):,}"
        )

        # Checkpoint periódico
        if not stop_triggered and batch_count % checkpoint_every == 0:
            checkpoint.save(all_reviews, continuation_token, batch_count)
            report["checkpoints_salvos"] += 1

        # Delay entre lotes
        if not stop_triggered:
            time.sleep(delay)

    # ---- Salvar resultado final ----
    report["total_reviews_coletados"] = len(all_reviews)
    report["total_lotes"] = batch_count
    report["fim_execucao"] = datetime.now().isoformat()
    if all_reviews:
        report["data_mais_antiga_coletada"] = min(
            review["at"] for review in all_reviews
        ).strftime("%Y-%m-%d")
        report["data_mais_recente_coletada"] = max(
            review["at"] for review in all_reviews
        ).strftime("%Y-%m-%d")

    if report["status"] != "em andamento":
        return pd.DataFrame(), report

    if not all_reviews:
        logger.warning("⚠️  Nenhum review coletado!")
        report["status"] = "sem dados"
        return pd.DataFrame(), report

    df = pd.DataFrame(all_reviews)
    df["reviewed_at"] = pd.to_datetime(df["at"]).dt.tz_localize(None)

    # Selecionar e renomear colunas
    columns_map = {
        "reviewId": "id_review",
        "userName": "usuario",
        "score": "nota",
        "reviewed_at": "data_avaliacao",
        "content": "texto_avaliacao",
        "replyContent": "resposta_dev",
        "replyAt": "data_resposta",
        "thumbsUpCount": "thumbs_up",
        "appVersion": "versao_app",
    }

    available_cols = {k: v for k, v in columns_map.items() if k in df.columns}
    df_clean = df[list(available_cols.keys())].rename(columns=available_cols)
    df_clean = df_clean.drop_duplicates(subset="id_review", keep="first")
    df_clean = df_clean.sort_values("data_avaliacao", ascending=False).reset_index(
        drop=True
    )

    output_file = output_dir / "reviews_raw.parquet"
    write_final_base(
        df_clean, output_file, start_date,
        Path(global_config["snapshot_dir"]) / bank_key,
    )
    checkpoint.cleanup()

    if report["status"] == "em andamento":
        report["status"] = "concluído"

    return df_clean, report


# ============================================================
# RELATÓRIO FINAL
# ============================================================
def generate_report(reports, output_dir):
    """Gera relatório final consolidado."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print()
    print("=" * 60)
    print("📋 RELATÓRIO FINAL DE COLETA")
    print(f"   {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    all_ok = True

    for r in reports:
        status_icon = {
            "concluído": "✅",
            "cobertura não confirmada": "⚠️",
            "interrompido por erro": "❌",
            "sem dados": "⚠️",
        }.get(r["status"], "❓")

        print(f"\n{status_icon} {r['banco']} ({r['app_id']})")
        print(f"   Status:           {r['status']}")
        print(f"   Reviews coletados: {r['total_reviews_coletados']:,}")
        print(f"   Lotes processados: {r['total_lotes']}")
        print(f"   Período filtro:    {r['data_inicio_filtro']} → hoje")
        if r.get("data_mais_antiga_coletada"):
            print(f"   Review mais antigo: {r['data_mais_antiga_coletada']}")
            print(f"   Review mais recente: {r['data_mais_recente_coletada']}")

        if r["status"] != "concluído":
            all_ok = False

        if r["early_stop"]:
            print(f"   Early stop em:     {r['early_stop_date']}")

        if r["checkpoints_salvos"] > 0:
            print(f"   Checkpoints:       {r['checkpoints_salvos']}")

        # Calcular duração
        if r["inicio_execucao"] and r["fim_execucao"]:
            inicio = datetime.fromisoformat(r["inicio_execucao"])
            fim = datetime.fromisoformat(r["fim_execucao"])
            duracao = fim - inicio
            minutos = duracao.total_seconds() / 60
            print(f"   Duração:           {minutos:.1f} min")

        if r["erros"]:
            all_ok = False
            print(f"   ⚠️  ERROS ({len(r['erros'])}):")
            for err in r["erros"]:
                print(f"      - {err}")

    # Resumo geral
    total_reviews = sum(r["total_reviews_coletados"] for r in reports)
    total_erros = sum(len(r["erros"]) for r in reports)

    print()
    print("-" * 60)
    print(f"📊 Total geral: {total_reviews:,} reviews coletados")
    print(f"🏦 Bancos processados: {len(reports)}")

    if total_erros > 0:
        print(f"⚠️  Total de erros: {total_erros}")
    else:
        print("✅ Nenhum erro registrado")

    print("-" * 60)

    # Salvar relatório em JSON
    report_file = output_dir / f"report_{timestamp}.json"
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n📄 Relatório salvo: {report_file}")

    return all_ok


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="Coleta reviews da Play Store para bancos brasileiros."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--bank",
        nargs="+",
        help="Chave(s) do banco no config.yaml (ex: nubank itau)",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Coletar todos os bancos do config.yaml",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Caminho do arquivo de configuração (padrão: config.yaml)",
    )

    args = parser.parse_args()

    # Carregar config
    config = load_config(args.config)
    banks = config["banks"]
    global_config = config["collection"]

    # Determinar quais bancos coletar
    if args.all:
        bank_keys = list(banks.keys())
    else:
        bank_keys = args.bank
        # Validar que os bancos existem no config
        for key in bank_keys:
            if key not in banks:
                available = ", ".join(banks.keys())
                logger.error(
                    f"❌ Banco '{key}' não encontrado no config. "
                    f"Disponíveis: {available}"
                )
                sys.exit(1)

    logger.info(f"🚀 Iniciando pipeline para {len(bank_keys)} banco(s): "
                f"{', '.join(bank_keys)}")

    # Coletar cada banco
    reports = []
    for bank_key in bank_keys:
        try:
            _, report = collect_bank(bank_key, banks[bank_key], global_config)
            reports.append(report)
        except KeyboardInterrupt:
            logger.warning("\n⛔ Interrompido pelo usuário (Ctrl+C)")
            # Relatório parcial
            if reports:
                generate_report(reports, global_config["report_dir"])
            sys.exit(130)
        except Exception as e:
            logger.error(f"❌ Erro fatal ao coletar {bank_key}: {e}")
            traceback.print_exc()
            reports.append({
                "banco": banks[bank_key].get("name", bank_key),
                "app_id": banks[bank_key]["app_id"],
                "data_inicio_filtro": banks[bank_key]["start_date"],
                "inicio_execucao": datetime.now().isoformat(),
                "fim_execucao": datetime.now().isoformat(),
                "total_reviews_coletados": 0,
                "total_lotes": 0,
                "erros": [f"Erro fatal: {e}"],
                "checkpoints_salvos": 0,
                "early_stop": False,
                "early_stop_date": None,
                "status": "interrompido por erro",
            })

    # Relatório final
    success = generate_report(reports, global_config["report_dir"])

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
