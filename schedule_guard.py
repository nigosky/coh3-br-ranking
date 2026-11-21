#!/usr/bin/env python3
"""Guarda idempotente de agendamento e watchdog (BRT / UTC-3) para Ranking Unificado e Boletim Diário."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from avatar_cache import RankingCacheDB
from security import BRT_TZ, configure_secure_logging, redact_secrets, resolve_safe_db_path

logger = logging.getLogger("coh3_ranking.schedule_guard")

RANKING_MORNING_HOUR_BRT = 0
RANKING_NOON_HOUR_BRT = 12
DAILY_RECAP_HOUR_BRT = 6


def get_current_ranking_slot(now_ts: float | None = None) -> tuple[str, int]:
    """Retorna `(slot_id, slot_start_ts)` para a janela ativa em BRT (`YYYY-MM-DD_00` ou `YYYY-MM-DD_12`)."""
    now_dt = datetime.fromtimestamp(float(now_ts if now_ts is not None else time.time()), tz=BRT_TZ)
    if now_dt.hour >= RANKING_NOON_HOUR_BRT:
        slot_hour = RANKING_NOON_HOUR_BRT
        suffix = "12"
    else:
        slot_hour = RANKING_MORNING_HOUR_BRT
        suffix = "00"
    slot_start = datetime(now_dt.year, now_dt.month, now_dt.day, slot_hour, 0, 0, tzinfo=BRT_TZ)
    return f"{slot_start.date().isoformat()}_{suffix}", int(slot_start.timestamp())


def get_expected_daily_recap_date(now_ts: float | None = None) -> tuple[str, bool]:
    """Retorna `(target_date_iso, is_window_open)` em BRT para o Boletim Diário (janela abre às 06:00 BRT)."""
    now_dt = datetime.fromtimestamp(float(now_ts if now_ts is not None else time.time()), tz=BRT_TZ)
    yesterday_iso = (now_dt.date() - timedelta(days=1)).isoformat()
    window_open = now_dt.hour >= DAILY_RECAP_HOUR_BRT
    return yesterday_iso, window_open


def evaluate_ranking_schedule(db: Any, now_ts: float | None = None) -> dict[str, Any]:
    """Verifica se a janela atual do Ranking Unificado BR (00h ou 12h BRT) está pendente."""
    slot_id, slot_start_ts = get_current_ranking_slot(now_ts=now_ts)
    last_slot = str(db.get_state("last_ranking_schedule_slot") or "").strip()

    try:
        last_pub_ts = int(db.get_state("last_publish_ts") or 0)
    except (TypeError, ValueError):
        last_pub_ts = 0
    try:
        last_check_ts = int(db.get_state("last_check_ts") or 0)
    except (TypeError, ValueError):
        last_check_ts = 0
    latest_run_ts = max(last_pub_ts, last_check_ts)

    if last_slot == slot_id:
        return {
            "due": False,
            "slot": slot_id,
            "slot_start_ts": slot_start_ts,
            "last_slot": last_slot,
            "reason": f"Janela do Ranking {slot_id} (BRT) já concluída anteriormente.",
        }

    # Compatibilidade com bancos anteriores à introdução de last_ranking_schedule_slot
    if not last_slot and latest_run_ts >= slot_start_ts:
        return {
            "due": False,
            "slot": slot_id,
            "slot_start_ts": slot_start_ts,
            "last_slot": last_slot,
            "reason": f"Ranking já publicado/verificado nesta janela {slot_id} (ts={latest_run_ts}).",
        }

    return {
        "due": True,
        "slot": slot_id,
        "slot_start_ts": slot_start_ts,
        "last_slot": last_slot,
        "reason": f"Janela do Ranking {slot_id} (BRT) pendente (última janela: {last_slot or 'nenhuma'}).",
    }


def evaluate_daily_recap_schedule(db: Any, now_ts: float | None = None) -> dict[str, Any]:
    """Verifica se o Boletim Diário de ontem (BRT) está pendente após as 06:00 BRT."""
    target_date, window_open = get_expected_daily_recap_date(now_ts=now_ts)
    last_posted = str(db.get_state("last_daily_recap_date") or "").strip()
    last_checked = str(db.get_state("last_daily_recap_checked_date") or "").strip()

    if not window_open:
        return {
            "due": False,
            "target_date": target_date,
            "window_open": False,
            "last_posted": last_posted,
            "last_checked": last_checked,
            "reason": f"Antes das {DAILY_RECAP_HOUR_BRT:02d}:00 BRT; janela do Boletim de {target_date} ainda não abriu.",
        }

    if last_posted == target_date or last_checked == target_date:
        return {
            "due": False,
            "target_date": target_date,
            "window_open": True,
            "last_posted": last_posted,
            "last_checked": last_checked,
            "reason": f"Boletim Diário de {target_date} já publicado/verificado.",
        }

    return {
        "due": True,
        "target_date": target_date,
        "window_open": True,
        "last_posted": last_posted,
        "last_checked": last_checked,
        "reason": f"Boletim Diário de {target_date} pendente/atrasado (último publicado: {last_posted or 'nenhum'}).",
    }


def evaluate_schedule_state(db_path: Path, now_ts: float | None = None) -> dict[str, Any]:
    """Avalia o estado de agendamento do Ranking Unificado e do Boletim Diário sem efeitos colaterais."""
    with RankingCacheDB(db_path) as db:
        ranking = evaluate_ranking_schedule(db, now_ts=now_ts)
        daily = evaluate_daily_recap_schedule(db, now_ts=now_ts)
    return {"ranking": ranking, "daily": daily}


def _write_github_outputs(status: dict[str, Any]) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not gh_out:
        return
    out_path = Path(gh_out)
    if out_path.is_symlink():
        raise RuntimeError(f"GITHUB_OUTPUT não pode ser symlink: {out_path}")
    lines = [
        f"ranking_due={'true' if status['ranking']['due'] else 'false'}",
        f"ranking_slot={status['ranking']['slot']}",
        f"daily_due={'true' if status['daily']['due'] else 'false'}",
        f"daily_target_date={status['daily']['target_date']}",
    ]
    with out_path.open("a", encoding="utf-8") as fp:
        fp.write("\n".join(lines) + "\n")


def main() -> int:
    configure_secure_logging(level=logging.INFO, datefmt="%Y-%m-%d %H:%M:%S")
    parser = argparse.ArgumentParser(description="Checagem idempotente de janelas BRT (Ranking + Boletim Diário)")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("./ranking_cache.db"),
        help="Caminho para ranking_cache.db",
    )
    parser.add_argument(
        "--check",
        choices=("ranking", "daily", "both"),
        default="both",
        help="Qual agendamento inspecionar",
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="Grava ranking_due/daily_due em GITHUB_OUTPUT quando disponível",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Imprime o status completo em JSON no stdout",
    )
    args = parser.parse_args()

    try:
        base_dir = Path(__file__).resolve().parent
        db_path = resolve_safe_db_path(base_dir, str(args.db))
        status = evaluate_schedule_state(db_path)

        if args.github_output:
            _write_github_outputs(status)

        if args.check in ("ranking", "both"):
            r = status["ranking"]
            logger.info("[WATCHDOG RANKING] due=%s | slot=%s | %s", r["due"], r["slot"], r["reason"])
        if args.check in ("daily", "both"):
            d = status["daily"]
            logger.info("[WATCHDOG BOLETIM] due=%s | target=%s | %s", d["due"], d["target_date"], d["reason"])

        if args.json:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        logger.error("Erro na checagem de agendamento: %s", redact_secrets(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
