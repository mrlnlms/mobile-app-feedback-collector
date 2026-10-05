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
import tempfile
import fcntl
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

from scripts.collect.google_play_storage import collection_reference, publish, recover_publication, save_json, sha256

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
    """Compatibilidade: prepara localmente e publica preservando o histórico."""
    output_file = Path(output_file)
    snapshots = Path(snapshot_dir) if snapshot_dir is not None else output_file.parent / "snapshots"
    state_file = snapshots.parent / "state" / (output_file.parent.name + ".json")
    reference = collection_reference(output_file, state_file, "legacy", start_date)
    with tempfile.TemporaryDirectory(prefix="google-play-publish-") as directory:
        merged, _, _ = publish(new_reviews, output_file, snapshots, Path(directory),
                               state_file, "legacy", start_date, reference,
                               datetime.now().astimezone().isoformat())
    return merged


# ============================================================
# CHECKPOINT
# ============================================================
class CheckpointManager:
    """Gerencia salvamento e carregamento de progresso parcial."""

    def __init__(self, checkpoint_dir, bank_key, identity=None):
        self.identity = identity
        self.dir = Path(checkpoint_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.filepath = self.dir / f"{bank_key}_checkpoint.parquet"
        self.meta_filepath = self.dir / f"{bank_key}_checkpoint_meta.pkl"

    def exists(self):
        return self.filepath.exists() and self.meta_filepath.exists()

    def save(self, reviews_list, continuation_token, batch_count, completed=False):
        """Salva progresso parcial."""
        df = pd.DataFrame(reviews_list)
        temporary = self.filepath.with_suffix(".tmp.parquet")
        df.to_parquet(temporary, index=False)
        os.replace(temporary, self.filepath)
        meta = {
            "continuation_token": continuation_token,
            "batch_count": batch_count,
            "total_reviews": len(reviews_list),
            "saved_at": datetime.now().isoformat(),
            "identity": self.identity,
            "completed": completed,
            "checkpoint_sha256": sha256(self.filepath),
        }
        temporary_meta = self.meta_filepath.with_suffix(".tmp.pkl")
        with open(temporary_meta, "wb") as f:
            pickle.dump(meta, f)
        os.replace(temporary_meta, self.meta_filepath)
        logger.info(
            f"💾 Checkpoint salvo: {len(reviews_list)} reviews (lote #{batch_count})"
        )

    def load(self):
        """Carrega progresso parcial."""
        df = pd.read_parquet(self.filepath)
        with open(self.meta_filepath, "rb") as f:
            meta = pickle.load(f)
        if meta.get("identity") != self.identity:
            raise ValueError("Checkpoint incompatível com a base ou configuração atual; preservado")
        if meta.get("checkpoint_sha256") != sha256(self.filepath):
            raise ValueError("Checkpoint incompleto ou alterado; preservado")
        logger.info(
            f"♻️  Checkpoint carregado: {meta['total_reviews']} reviews "
            f"(lote #{meta['batch_count']}, salvo em {meta['saved_at']})"
        )
        return df.to_dict("records"), meta["continuation_token"], meta["batch_count"], meta.get("completed", False)

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
    """Impede coletas simultâneas do mesmo banco nesta máquina."""
    staging = Path(global_config["staging_dir"]) / bank_key
    official = Path(global_config["output_dir"]).resolve()
    for local in (staging, Path(global_config["checkpoint_dir"])):
        resolved = local.resolve()
        if resolved.is_relative_to(official) or "CloudStorage" in resolved.parts:
            raise ValueError("Staging e checkpoints da Google Play precisam ficar fora do Drive")
    staging.mkdir(parents=True, exist_ok=True)
    with (staging / "collection.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Já existe uma coleta local em andamento para {bank_key}") from exc
        return _collect_bank(bank_key, bank_config, global_config)


def _collect_bank(bank_key, bank_config, global_config):
    """
    Coleta reviews de um banco específico.
    Retorna (DataFrame, report_dict).
    """
    app_id = bank_config["app_id"]
    bank_name = bank_config.get("name", bank_key)
    historical_start = datetime.strptime(bank_config["start_date"], "%Y-%m-%d")
    output_file = Path(global_config["output_dir"]) / bank_key / "reviews_raw.parquet"
    state_file = Path(global_config["state_dir"]) / f"{bank_key}.json"
    staging_dir = Path(global_config["staging_dir"]) / bank_key
    if recover_publication(output_file, state_file, staging_dir, app_id):
        CheckpointManager(global_config["checkpoint_dir"], bank_key).cleanup()
        logger.info("Estado recuperado de uma publicação anterior para %s", bank_key)
    reference = collection_reference(output_file, state_file, app_id, historical_start,
                                     global_config["overlap_hours"])
    start_date = reference["cutoff"]
    identity = {
        "app_id": app_id, "historical_start": historical_start.isoformat(),
        "cutoff": start_date.isoformat(), "base_sha256": reference["base_sha256"],
        "batch_size": global_config["batch_size"], "lang": "pt", "country": "br",
    }

    batch_size = global_config["batch_size"]
    delay = global_config["delay_seconds"]
    checkpoint_every = global_config["checkpoint_every"]
    max_retries = global_config["max_retries"]

    fetch_batch = make_fetch_batch(max_retries, start_date)
    checkpoint = CheckpointManager(global_config["checkpoint_dir"], bank_key, identity)

    # Report de execução
    report = {
        "banco": bank_name,
        "app_id": app_id,
        "data_inicio_filtro": start_date.isoformat(),
        "data_inicio_historico": historical_start.isoformat(),
        "modo": "incremental" if reference["base_sha256"] else "primeira coleta",
        "sobreposicao_horas": 24,
        "referencia_origem": reference["origin"],
        "ultima_avaliacao_anterior": reference["latest_review_at"],
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
    logger.info(f"📅 Fronteira: {start_date.isoformat()} → mais recentes ({report['modo']})")
    logger.info(f"⚙️  Lote: {batch_size} | Delay: {delay}s | Retries: {max_retries}")
    print("=" * 60)

    # ---- Retomar de checkpoint se existir ----
    all_reviews = []
    continuation_token = None
    batch_count = 0
    stop_triggered = False

    if checkpoint.exists():
        all_reviews, continuation_token, batch_count, stop_triggered = checkpoint.load()
        if stop_triggered:
            report["early_stop"] = True
        if not stop_triggered and getattr(continuation_token, "token", None) is None:
            raise RuntimeError(
                f"Checkpoint sem token de retomada: {checkpoint.meta_filepath}"
            )

    # ---- Loop de coleta ----
    while not stop_triggered:
        try:
            result, continuation_token = fetch_batch(
                app_id, batch_size, continuation_token
            )
        except KeyboardInterrupt:
            if all_reviews and getattr(continuation_token, "token", None) is not None:
                checkpoint.save(all_reviews, continuation_token, batch_count)
            raise
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
                    f"🛑 Fronteira alcançada: review de {review_date.isoformat()} "
                    f"anterior a {start_date.isoformat()}"
                )
                continue

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
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                if all_reviews and getattr(continuation_token, "token", None) is not None:
                    checkpoint.save(all_reviews, continuation_token, batch_count)
                raise

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

    # Preserve the complete local result if validation/publication fails.
    if all_reviews:
        checkpoint.save(all_reviews, continuation_token, batch_count, completed=True)

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

    merged, state, snapshot = publish(
        df_clean, output_file, Path(global_config["snapshot_dir"]) / bank_key,
        staging_dir, state_file, app_id, historical_start, reference,
        report["inicio_execucao"],
    )
    report["reviews_na_base"] = len(merged)
    report["base_sha256"] = state["base_sha256"]
    report["estado"] = str(state_file)
    report["snapshot"] = str(snapshot) if snapshot else None
    report["publicacao"] = "gravada e conferida no destino; sincronização da nuvem não verificada"
    checkpoint.cleanup()

    if report["status"] == "em andamento":
        report["status"] = "concluído"

    return merged, report


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
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostrar referência e fronteira por banco sem consultar a loja ou gravar arquivos")

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

    if args.dry_run:
        for key in bank_keys:
            bank = banks[key]
            reference = collection_reference(
                Path(global_config["output_dir"]) / key / "reviews_raw.parquet",
                Path(global_config["state_dir"]) / f"{key}.json", bank["app_id"],
                datetime.fromisoformat(bank["start_date"]), global_config["overlap_hours"],
            )
            print(f"{key}: última avaliação={reference['latest_review_at']}; "
                  f"coletar até={reference['cutoff'].isoformat()}; "
                  f"referência={reference['origin']}")
        return

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

    # Refresh the versioned manifest after any successful publication.
    if any(report["status"] == "concluído" for report in reports):
        from scripts.archive_manifest import build
        try:
            save_json(Path(global_config["manifest_file"]), build(config))
        except Exception as exc:
            logger.error("Bases publicadas, mas atualização do manifesto falhou: %s", exc)
            generate_report(reports, global_config["report_dir"])
            sys.exit(1)

    # Relatório final
    success = generate_report(reports, global_config["report_dir"])

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
