"""Testes unitários da guarda idempotente de agendamento e watchdog cruzado (BRT)."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import run_ranking
from avatar_cache import RankingCacheDB
from schedule_guard import (
    _write_github_outputs,
    evaluate_battlegroups_schedule,
    evaluate_daily_recap_schedule,
    evaluate_ranking_schedule,
    evaluate_schedule_state,
    get_current_ranking_slot,
    get_expected_battlegroups_slot,
    get_expected_daily_recap_date,
    get_expected_ranking_slot,
)
from security import BRT_TZ


def _brt_ts(year: int, month: int, day: int, hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime(year, month, day, hour, minute, second, tzinfo=BRT_TZ).timestamp()


class TestScheduleGuard(unittest.TestCase):
    def test_ranking_daily_slot_boundaries_after_00_01_brt(self) -> None:
        # Antes das 00:01 BRT (00:00:00..00:00:59) -> janela ainda fechada
        slot_0000, open_0000, start_0000 = get_expected_ranking_slot(_brt_ts(2026, 11, 21, 0, 0, 0))
        self.assertEqual(slot_0000, "2026-11-21")
        self.assertFalse(open_0000)
        self.assertEqual(start_0000, int(_brt_ts(2026, 11, 21, 0, 1, 0)))

        _, open_0059, _ = get_expected_ranking_slot(_brt_ts(2026, 11, 21, 0, 0, 59))
        self.assertFalse(open_0059)

        # A partir de 00:01 BRT -> janela única aberta pelo resto do dia (sem slot extra às 12h)
        slot_0001, open_0001, start_0001 = get_expected_ranking_slot(_brt_ts(2026, 11, 21, 0, 1, 0))
        self.assertEqual(slot_0001, "2026-11-21")
        self.assertTrue(open_0001)
        self.assertEqual(start_0001, int(_brt_ts(2026, 11, 21, 0, 1, 0)))

        slot_0002, start_0002 = get_current_ranking_slot(_brt_ts(2026, 11, 21, 0, 2))
        self.assertEqual(slot_0002, "2026-11-21")
        self.assertEqual(start_0002, int(_brt_ts(2026, 11, 21, 0, 1)))

        slot_1202, open_1202, start_1202 = get_expected_ranking_slot(_brt_ts(2026, 11, 21, 12, 2, 0))
        self.assertEqual(slot_1202, "2026-11-21")
        self.assertTrue(open_1202)
        self.assertEqual(start_1202, int(_brt_ts(2026, 11, 21, 0, 1)))

        slot_2359, _ = get_current_ranking_slot(_brt_ts(2026, 11, 21, 23, 59, 59))
        self.assertEqual(slot_2359, "2026-11-21")

    def test_ranking_schedule_strict_once_per_day_idempotency_and_legacy_slots(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "cache.db"
            with RankingCacheDB(db_path) as db:
                # 00:00:30 BRT -> antes das 00:01 BRT, mesmo com banco vazio NÃO executa
                st_early = evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 21, 0, 0, 30))
                self.assertFalse(st_early["due"])
                self.assertFalse(st_early["window_open"])

                # 00:02 BRT -> janela diária aberta e pendente
                st_due = evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 21, 0, 2))
                self.assertTrue(st_due["due"])
                self.assertTrue(st_due["window_open"])
                self.assertEqual(st_due["slot"], "2026-11-21")

                # Proteção redundante por timestamp (mesmo com last_ranking_schedule_slot de ontem):
                # se last_publish_ts ou arranged_teams_publish_ts >= 00:01 BRT hoje, bloqueia pelo resto do dia!
                db.set_state("last_ranking_schedule_slot", "2026-11-20")
                db.set_state("arranged_teams_publish_ts", str(int(_brt_ts(2026, 11, 21, 0, 3))))
                for hh, mm in ((0, 37), (2, 17), (8, 3), (12, 2), (12, 37), (18, 0), (23, 59)):
                    st_same_day = evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 21, hh, mm))
                    self.assertFalse(st_same_day["due"], msg=f"Não deveria disparar novamente às {hh:02d}:{mm:02d} BRT")

                # Compatibilidade com formatos legados (2026-11-21_00, 2026-11-21_12, 2026-11-21-00) e novo (2026-11-21)
                db.set_state("arranged_teams_publish_ts", "0")
                for legacy_slot in ("2026-11-21_00", "2026-11-21_12", "2026-11-21-00", "2026-11-21"):
                    db.set_state("last_ranking_schedule_slot", legacy_slot)
                    self.assertFalse(
                        evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 21, 12, 2))["due"],
                        msg=f"Slot {legacy_slot} deve bloquear reexecução no mesmo dia",
                    )

                # Execução de ontem às 23:55 BRT NÃO bloqueia o dia seguinte após 00:01 BRT
                db.set_state("last_ranking_schedule_slot", "2026-11-21")
                db.set_state("last_publish_ts", str(int(_brt_ts(2026, 11, 21, 23, 55))))

                # Dia seguinte 00:00:59 BRT -> ainda fechado
                st_next_early = evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 22, 0, 0, 59))
                self.assertFalse(st_next_early["due"])
                self.assertFalse(st_next_early["window_open"])

                # Dia seguinte 00:01:00 e 00:02:00 BRT -> libera a execução única do novo dia
                st_next_0001 = evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 22, 0, 1, 0))
                self.assertTrue(st_next_0001["due"])
                self.assertEqual(st_next_0001["slot"], "2026-11-22")

                st_next_0002 = evaluate_ranking_schedule(db, now_ts=_brt_ts(2026, 11, 22, 0, 2, 0))
                self.assertTrue(st_next_0002["due"])
                self.assertEqual(st_next_0002["slot"], "2026-11-22")

    def test_run_ranking_sync_respect_daily_schedule_exits_early_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db_path = base / "ranking_cache.db"
            cfg_path = base / "config.json"
            cfg_path.write_text(
                json.dumps({"cache_db_path": "./ranking_cache.db", "custom_players": []}),
                encoding="utf-8",
            )
            with RankingCacheDB(db_path) as db:
                db.set_state("last_ranking_schedule_slot", "2026-11-21")

            # Já executou hoje (12:02 BRT) -> sai com 0 sem tocar em Relic API ou Discord
            with patch.object(
                run_ranking,
                "evaluate_ranking_schedule",
                side_effect=lambda db_arg: evaluate_ranking_schedule(db_arg, now_ts=_brt_ts(2026, 11, 21, 12, 2)),
            ), patch.object(
                run_ranking, "refresh_core_br_roster", side_effect=AssertionError("Não deve sincronizar roster")
            ), patch.object(
                run_ranking, "RelicRankingClient", side_effect=AssertionError("Não deve consultar Relic API")
            ):
                rc = run_ranking.run_ranking_sync(
                    config_path=cfg_path,
                    force=True,
                    respect_daily_schedule=True,
                )
            self.assertEqual(rc, 0)

            # Antes das 00:01 BRT do dia seguinte (00:00:30 BRT) -> também bloqueia antes de qualquer chamada
            with patch.object(
                run_ranking,
                "evaluate_ranking_schedule",
                side_effect=lambda db_arg: evaluate_ranking_schedule(db_arg, now_ts=_brt_ts(2026, 11, 22, 0, 0, 30)),
            ), patch.object(
                run_ranking, "refresh_core_br_roster", side_effect=AssertionError("Não deve sincronizar antes de 00:01")
            ), patch.object(
                run_ranking, "RelicRankingClient", side_effect=AssertionError("Não deve consultar Relic antes de 00:01")
            ):
                rc_early = run_ranking.run_ranking_sync(
                    config_path=cfg_path,
                    force=True,
                    respect_daily_schedule=True,
                )
            self.assertEqual(rc_early, 0)

    def test_ranking_workflow_enforces_once_per_day_after_00_01_brt_without_push_trigger(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        ranking_wf = (repo_root / ".github" / "workflows" / "ranking_unificado.yml").read_text(encoding="utf-8")
        daily_wf = (repo_root / ".github" / "workflows" / "boletim_diario.yml").read_text(encoding="utf-8")

        # Não pode disparar em push (evita republicações extras ao longo do dia)
        self.assertIsNone(re.search(r"^\s{2}push\s*:", ranking_wf, flags=re.MULTILINE))

        # Extrai todos os crons configurados em ranking_unificado.yml e valida que são apenas após 00:01 BRT (03:02..06:59 UTC)
        crons = re.findall(r'cron:\s*["\']([^"\']+)["\']', ranking_wf)
        self.assertGreaterEqual(len(crons), 1)
        for expr in crons:
            minute_s, hour_s, dom, month, dow = expr.split()
            self.assertEqual((dom, month, dow), ("*", "*", "*"))
            minute_i, hour_i = int(minute_s), int(hour_s)
            brt_hour = (hour_i - 3) % 24
            self.assertIn(brt_hour, (0, 1, 2, 3), msg=f"Cron fora da janela pós-00:01 BRT: {expr}")
            self.assertGreaterEqual(
                (brt_hour, minute_i),
                (0, 2),
                msg=f"Cron deve ser estritamente após 00:01 BRT: {expr}",
            )

        # force_update deve ter default false e workflow precisa aplicar --respect-daily-schedule
        self.assertRegex(ranking_wf, r"force_update:[\s\S]*?default:\s*false")
        self.assertIn("--respect-daily-schedule", ranking_wf)
        self.assertIn("MANUAL_OVERRIDE", ranking_wf)

        # Watchdog do Boletim Diário não pode forçar bypass (deve usar force_update=false)
        self.assertIn("gh workflow run ranking_unificado.yml", daily_wf)
        self.assertIn("-f force_update=false", daily_wf)
        self.assertNotIn("ranking_unificado.yml --repo nigosky/coh3-br-ranking --ref main -f force_update=true", daily_wf)

    def test_daily_recap_window_overdue_detection_and_zero_match_idempotency(self) -> None:
        target_early, open_early = get_expected_daily_recap_date(_brt_ts(2026, 11, 21, 8, 2))
        self.assertEqual(target_early, "2026-11-20")
        self.assertFalse(open_early)

        target_due, open_due = get_expected_daily_recap_date(_brt_ts(2026, 11, 21, 8, 3))
        self.assertEqual(target_due, "2026-11-20")
        self.assertTrue(open_due)

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "cache.db"
            with RankingCacheDB(db_path) as db:
                db.set_state("last_daily_recap_date", "2026-11-19")

                # Antes das 08:03 BRT -> ainda não abriu janela
                st_before = evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 21, 7, 59))
                self.assertFalse(st_before["due"])
                self.assertFalse(st_before["window_open"])

                # Às 08:03 BRT (ou atrasado às 08:57 BRT) -> detecta pendência de 2026-11-20
                st_due = evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 21, 8, 57))
                self.assertTrue(st_due["due"])
                self.assertEqual(st_due["target_date"], "2026-11-20")

                # Dia sem partidas (0 jogos): last_daily_recap_checked_date impede re-execução infinita
                db.set_state("last_daily_recap_checked_date", "2026-11-20")
                self.assertFalse(evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 21, 12, 2))["due"])

                # Publicação normal de 2026-11-20 também marca concluído e libera 2026-11-21 no dia seguinte às 08:03 BRT
                db.set_state("last_daily_recap_date", "2026-11-20")
                self.assertFalse(evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 21, 14, 0))["due"])
                self.assertFalse(evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 22, 8, 2))["due"])
                st_tomorrow = evaluate_daily_recap_schedule(db, now_ts=_brt_ts(2026, 11, 22, 8, 3))
                self.assertTrue(st_tomorrow["due"])
                self.assertEqual(st_tomorrow["target_date"], "2026-11-21")

    def test_battlegroups_weekly_saturday_noon_schedule_and_recovery(self) -> None:
        # 2026-11-28 é sábado; 2026-11-27 sexta; 2026-11-29 domingo; 2026-11-30 segunda
        slot_fri, open_fri, _ = get_expected_battlegroups_slot(_brt_ts(2026, 11, 27, 23, 59))
        self.assertEqual(slot_fri, "2026-11-21_sat12")
        self.assertFalse(open_fri)

        slot_sat_early, open_sat_early, _ = get_expected_battlegroups_slot(_brt_ts(2026, 11, 28, 11, 59))
        self.assertEqual(slot_sat_early, "2026-11-28_sat12")
        self.assertFalse(open_sat_early)

        slot_sat_noon, open_sat_noon, start_sat = get_expected_battlegroups_slot(_brt_ts(2026, 11, 28, 12, 5))
        self.assertEqual(slot_sat_noon, "2026-11-28_sat12")
        self.assertTrue(open_sat_noon)
        self.assertEqual(start_sat, int(_brt_ts(2026, 11, 28, 12, 0)))

        # Domingo 00:05 BRT -> ainda na janela de recuperação 24h do sábado 2026-11-28
        slot_sun_rec, open_sun_rec, _ = get_expected_battlegroups_slot(_brt_ts(2026, 11, 29, 0, 5))
        self.assertEqual(slot_sun_rec, "2026-11-28_sat12")
        self.assertTrue(open_sun_rec)

        # Domingo 12:00 BRT e Segunda -> fora da janela
        _, open_sun_noon, _ = get_expected_battlegroups_slot(_brt_ts(2026, 11, 29, 12, 0))
        self.assertFalse(open_sun_noon)
        _, open_mon, _ = get_expected_battlegroups_slot(_brt_ts(2026, 11, 30, 12, 5))
        self.assertFalse(open_mon)

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "cache.db"
            with RankingCacheDB(db_path) as db:
                # Sábado 11:55 BRT -> ainda não abriu
                self.assertFalse(evaluate_battlegroups_schedule(db, now_ts=_brt_ts(2026, 11, 28, 11, 55))["due"])

                # Sábado 12:05 BRT -> pendente!
                st_due = evaluate_battlegroups_schedule(db, now_ts=_brt_ts(2026, 11, 28, 12, 5))
                self.assertTrue(st_due["due"])
                self.assertEqual(st_due["slot"], "2026-11-28_sat12")

                # Se falhasse no sábado, domingo 00:05 BRT o watchdog detecta pendente
                self.assertTrue(evaluate_battlegroups_schedule(db, now_ts=_brt_ts(2026, 11, 29, 0, 5))["due"])

                # Após gravar last_bg_schedule_slot, backups de sábado (12:39/14:19) e domingo 00:05 encerram cedo
                db.set_state("last_bg_schedule_slot", "2026-11-28_sat12")
                self.assertFalse(evaluate_battlegroups_schedule(db, now_ts=_brt_ts(2026, 11, 28, 12, 39))["due"])
                self.assertFalse(evaluate_battlegroups_schedule(db, now_ts=_brt_ts(2026, 11, 28, 14, 19))["due"])
                self.assertFalse(evaluate_battlegroups_schedule(db, now_ts=_brt_ts(2026, 11, 29, 0, 5))["due"])

                # Próximo sábado (2026-12-05 às 12:05 BRT) abre nova janela
                st_next = evaluate_battlegroups_schedule(db, now_ts=_brt_ts(2026, 12, 5, 12, 5))
                self.assertTrue(st_next["due"])
                self.assertEqual(st_next["slot"], "2026-12-05_sat12")

    def test_evaluate_schedule_state_and_github_output_security(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db_path = base / "ranking_cache.db"
            with RankingCacheDB(db_path) as db:
                db.set_state("last_ranking_schedule_slot", "2026-11-21")
                db.set_state("last_daily_recap_date", "2026-11-19")

            # 2026-11-21 às 08:57 BRT -> ranking já atualizado hoje, daily pendente e battlegroups fechado
            status = evaluate_schedule_state(db_path, now_ts=_brt_ts(2026, 11, 21, 8, 57))
            self.assertFalse(status["ranking"]["due"])
            self.assertTrue(status["ranking"]["window_open"])
            self.assertTrue(status["daily"]["due"])
            self.assertFalse(status["battlegroups"]["due"])  # antes das 12h BRT de sábado

            gh_out = base / "gh_output.txt"
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(gh_out)}):
                _write_github_outputs(status)
            content = gh_out.read_text(encoding="utf-8")
            self.assertIn("ranking_due=false\n", content)
            self.assertIn("ranking_slot=2026-11-21\n", content)
            self.assertIn("ranking_window_open=true\n", content)
            self.assertIn("daily_due=true\n", content)
            self.assertIn("daily_target_date=2026-11-20\n", content)
            self.assertIn("battlegroups_due=false\n", content)
            self.assertIn("battlegroups_slot=2026-11-21_sat12\n", content)

            # Bloqueia symlink em GITHUB_OUTPUT
            sym_out = base / "sym_out.txt"
            sym_out.symlink_to(gh_out)
            with patch.dict(os.environ, {"GITHUB_OUTPUT": str(sym_out)}):
                with self.assertRaises(RuntimeError):
                    _write_github_outputs(status)


if __name__ == "__main__":
    unittest.main()
