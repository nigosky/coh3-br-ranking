"""Testes unitários da guarda idempotente de agendamento e watchdog cruzado (BRT)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from avatar_cache import RankingCacheDB
from schedule_guard import (
    _write_github_outputs,
    evaluate_daily_recap_schedule,
    evaluate_ranking_schedule,
    evaluate_schedule_state,
    get_current_ranking_slot,
    get_expected_daily_recap_date,
)
from security import BRT_TZ


def _brt_ts(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(year, month, day, hour, minute, second, tzinfo=BRT_TZ).timestamp()


class TestScheduleGuard(unittest.TestCase):
    def test_ranking_slot_boundaries_in_brt(self) -> None:
        slot_00, start_00 = get_current_ranking_slot(_brt_ts(2026, 11, 21, 0, 2))
        self.assertEqual(slot_00, "2026-11-21_00")
        self.assertEqual(start_00, int(_brt_ts(2026, 11, 21, 0, 0)))

        slot_11, _ = get_current_ranking_slot(_brt_ts(2026, 11, 21, 11, 59, 59))
        self.assertEqual(slot_11, "2026-11-21_00")

        slot_12, start_12 = get_current_ranking_slot(_brt_ts(2026, 11, 21, 12, 0, 0))
        self.assertEqual(slot_12, "2026-11-21_12")
        self.assertEqual(start_12, int(_brt_ts(2026, 11, 21, 12, 0)))

        slot_23, _ = get_current_ranking_slot(_brt_ts(2026, 11, 21, 23, 59, 59))
        self.assertEqual(slot_23, "2026-11-21_12")

    def test_ranking_schedule_idempotency_and_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "cache.db"
            with RankingCacheDB(db_path) as db:
                ts_00_02 = _brt_ts(2026, 11, 21, 0, 2)
                # Banco vazio -> janela 00h pendente
                st = evaluate_ranking_schedule(db, now_ts=ts_00_02)
                self.assertTrue(st["due"])
                self.assertEqual(st["slot"], "2026-11-21_00")

                # Compatibilidade legada: last_publish_ts dentro da janela atual marca como concluído
                db.set_state("last_publish_ts", str(int(_brt_ts(2026, 11, 21, 0, 3))))
                self.assertFalse(evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 21, 0, 37))["due"])

                # Quando vira 12:02 BRT, nova janela fica pendente
                st_noon = evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 21, 12, 2))
                self.assertTrue(st_noon["due"])
                self.assertEqual(st_noon["slot"], "2026-11-21_12")

                # Após gravar last_ranking_schedule_slot, cron de backup (12:37 BRT) encerra cedo
                db.set_state("last_ranking_schedule_slot", "2026-11-21_12")
                self.assertFalse(evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 21, 12, 37))["due"])

                # No dia seguinte às 00:02 BRT, volta a ficar pendente
                st_next = evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 22, 0, 2))
                self.assertTrue(st_next["due"])
                self.assertEqual(st_next["slot"], "2026-11-22_00")

    def test_daily_recap_window_overdue_detection_and_zero_match_idempotency(self) -> None:
        target_early, open_early = get_expected_daily_recap_date(_brt_ts(2026, 11, 21, 5, 59))
        self.assertEqual(target_early, "2026-11-20")
        self.assertFalse(open_early)

        target_due, open_due = get_expected_daily_recap_date(_brt_ts(2026, 11, 21, 6, 0))
        self.assertEqual(target_due, "2026-11-20")
        self.assertTrue(open_due)

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "cache.db"
            with RankingCacheDB(db_path) as db:
                db.set_state("last_daily_recap_date", "2026-11-19")

                # Antes das 06:00 BRT -> ainda não abriu janela
                st_before = evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 21, 0, 37))
                self.assertFalse(st_before["due"])
                self.assertFalse(st_before["window_open"])

                # Às 06:02 BRT (ou atrasado às 08:57 BRT) -> detecta pendência de 2026-11-20
                st_due = evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 21, 8, 57))
                self.assertTrue(st_due["due"])
                self.assertEqual(st_due["target_date"], "2026-11-20")

                # Dia sem partidas (0 jogos): last_daily_recap_checked_date impede re-execução infinita
                db.set_state("last_daily_recap_checked_date", "2026-11-20")
                self.assertFalse(evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 21, 12, 2))["due"])

                # Publicação normal de 2026-11-20 também marca concluído e libera 2026-11-21 no dia seguinte
                db.set_state("last_daily_recap_date", "2026-11-20")
                self.assertFalse(evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 21, 14, 0))["due"])
                st_tomorrow = evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 22, 6, 2))
                self.assertTrue(st_tomorrow["due"])
                self.assertEqual(st_tomorrow["target_date"], "2026-11-21")

    def test_evaluate_schedule_state_and_github_output_security(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db_path = base / "ranking_cache.db"
            with RankingCacheDB(db_path) as db:
                db.set_state("last_ranking_schedule_slot", "2026-11-21_00")
                db.set_state("last_daily_recap_date", "2026-11-19")

            status = evaluate_schedule_state(db_path, now_ts=_brt_ts(2026, 11, 21, 8, 57))
            self.assertFalse(status["ranking"]["due"])
            self.assertTrue(status["daily"]["due"])

            gh_out = base / "gh_output.txt"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_out)}):
                _write_github_outputs(status)
            content = gh_out.read_text(encoding="utf-8")
            self.assertIn("ranking_due=false\n", content)
            self.assertIn("ranking_slot=2026-11-21_00\n", content)
            self.assertIn("daily_due=true\n", content)
            self.assertIn("daily_target_date=2026-11-20\n", content)

            # Bloqueia symlink em GITHUB_OUTPUT
            sym_out = base / "sym_out.txt"
            sym_out.symlink_to(gh_out)
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(sym_out)}):
                with self.assertRaises(RuntimeError):
                    _write_github_outputs(status)


if __name__ == "__main__":
    unittest.main()
