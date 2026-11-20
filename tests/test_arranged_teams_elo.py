"""Testes unitários do cálculo de ELO, filtragem 2v2 BR, unificação Eixo+Aliados e cache SQLite."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from arranged_teams import (
    ELO_K_FACTOR,
    INITIAL_TEAM_ELO,
    build_side_teams_from_matches,
    compute_unified_2v2_arranged_teams,
    export_arranged_teams_files,
    extract_brazilian_2v2_matches_from_dump,
    is_brazilian_2v2_team,
    is_brazilian_player,
)
from avatar_cache import RankingCacheDB


def _mk_match(
    mid: int,
    ts: int,
    t0_players: list[tuple[int, str, str, str]],
    t0_races: list[int],
    t0_won: bool,
    t1_players: list[tuple[int, str, str, str]],
    t1_races: list[int],
    t0_elo: float = 1200.0,
    t1_elo: float = 1200.0,
    mtype: int = 2,
    platform: str = "steam",
) -> dict:
    reports = []
    for idx, (pid, alias, country, sid) in enumerate(t0_players):
        reports.append(
            {
                "profile_id": pid,
                "resulttype": 1 if t0_won else 0,
                "teamid": 0,
                "race_id": t0_races[idx],
                "profile": {"alias": alias, "country": country, "name": f"/steam/{sid}"},
                "matchhistorymember": {"oldrating": t0_elo, "newrating": t0_elo, "outcome": 1 if t0_won else 0},
            }
        )
    for idx, (pid, alias, country, sid) in enumerate(t1_players):
        reports.append(
            {
                "profile_id": pid,
                "resulttype": 0 if t0_won else 1,
                "teamid": 1,
                "race_id": t1_races[idx],
                "profile": {"alias": alias, "country": country, "name": f"/steam/{sid}"},
                "matchhistorymember": {"oldrating": t1_elo, "newrating": t1_elo, "outcome": 0 if t0_won else 1},
            }
        )
    return {
        "id": mid,
        "matchtype_id": mtype,
        "platform": platform,
        "completiontime": ts,
        "matchhistoryreportresults": reports,
    }


class TestArrangedTeamsEloAndRules(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 1795100000
        self.custom_players = [
            {"profile_id": 26910, "steam_id": "76561197963741792", "original_alias": "nigo_BR ᵀᴹ"},
            {"profile_id": 110078, "steam_id": "76561198024839791", "original_alias": "Bineto"},
            {"profile_id": 366826, "steam_id": "76561198014985380", "original_alias": "Lem22"},
        ]
        self.vet_pids = {26910, 110078, 366826}

        self.nigo = (26910, "nigo_BR ᵀᴹ", "br", "76561197963741792")
        self.bineto = (110078, "Bineto", "br", "76561198024839791")
        self.lem = (366826, "lem22", "us", "76561198014985380")
        self.br1 = (1390350, "TearsInTheRain", "br", "76561198089278659")
        self.br2 = (9999991, "Charles Bronson", "br", "76561198000000011")
        self.ger1 = (8888881, "HansDE", "de", "76561198000000091")
        self.ger2 = (8888882, "FritzDE", "de", "76561198000000092")

    def test_brazilian_team_criteria_includes_veterano_with_us_flag_and_excludes_mixed(self) -> None:
        self.assertTrue(is_brazilian_player({"profile_id": 366826, "country": "us"}, self.vet_pids))
        self.assertFalse(is_brazilian_player({"profile_id": 8888881, "country": "de"}, self.vet_pids))

        # Lem22 (US flag, Veterano) + nigo_BR (BR) -> aceito
        self.assertTrue(
            is_brazilian_2v2_team(
                {"type": "2v2", "players": [{"profile_id": 366826, "country": "us"}, {"profile_id": 26910, "country": "br"}]},
                self.vet_pids,
            )
        )
        # BR + estrangeiro não-Veterano -> rejeitado
        self.assertFalse(
            is_brazilian_2v2_team(
                {"type": "2v2", "players": [{"profile_id": 26910, "country": "br"}, {"profile_id": 8888881, "country": "de"}]},
                self.vet_pids,
            )
        )
        # 3v3 -> rejeitado
        self.assertFalse(
            is_brazilian_2v2_team(
                {
                    "type": "3v3",
                    "players": [
                        {"profile_id": 26910, "country": "br"},
                        {"profile_id": 110078, "country": "br"},
                        {"profile_id": 1390350, "country": "br"},
                    ],
                },
                self.vet_pids,
            )
        )

    def test_elo_math_exact_formula(self) -> None:
        # 3 vitórias contra adversários de 1200, 1600 e 1000
        rows = [
            {
                "match_id": 1, "side": "axis", "pair_key": "26910_110078",
                "p1_id": 26910, "p1_alias": "nigo", "p1_country": "br", "p1_steam_id": "",
                "p2_id": 110078, "p2_alias": "Bineto", "p2_country": "br", "p2_steam_id": "",
                "won": 1, "enemy_elo": 1200.0, "completion_ts": self.now - 300,
            },
            {
                "match_id": 2, "side": "axis", "pair_key": "26910_110078",
                "p1_id": 26910, "p1_alias": "nigo", "p1_country": "br", "p1_steam_id": "",
                "p2_id": 110078, "p2_alias": "Bineto", "p2_country": "br", "p2_steam_id": "",
                "won": 1, "enemy_elo": 1600.0, "completion_ts": self.now - 200,
            },
            {
                "match_id": 3, "side": "axis", "pair_key": "26910_110078",
                "p1_id": 26910, "p1_alias": "nigo", "p1_country": "br", "p1_steam_id": "",
                "p2_id": 110078, "p2_alias": "Bineto", "p2_country": "br", "p2_steam_id": "",
                "won": 0, "enemy_elo": 1000.0, "completion_ts": self.now - 100,
            },
        ]

        # Cálculo manual esperado
        e = INITIAL_TEAM_ELO
        for won, opp in ((1, 1200.0), (1, 1600.0), (0, 1000.0)):
            exp = 1.0 / (1.0 + 10.0 ** ((opp - e) / 400.0))
            e = round(e + ELO_K_FACTOR * (won - exp), 1)

        # Passar fora de ordem cronológica para validar ordenação interna
        built = build_side_teams_from_matches([rows[2], rows[0], rows[1]], min_games=3)
        self.assertEqual(len(built), 1)
        self.assertAlmostEqual(built[0]["elo"], e, places=1)
        self.assertEqual(built[0]["w"], 2)
        self.assertEqual(built[0]["l"], 1)
        self.assertEqual(built[0]["s"], -1)

    def test_br_vs_br_arranged_match_uses_arranged_team_elos(self) -> None:
        # 3 jogos entre dupla A (axis) e dupla B (allies), 2 vitórias A e 1 vitória B
        # Na partida 1, ambas começam com 1200 -> soma de ELOs se conserva em 2400!
        dump = [
            _mk_match(1, self.now - 500, [self.nigo, self.bineto], [137123, 198437], True, [self.br1, self.br2], [129494, 203852], t0_elo=900, t1_elo=1800),
            _mk_match(2, self.now - 400, [self.nigo, self.bineto], [137123, 198437], False, [self.br1, self.br2], [129494, 203852], t0_elo=900, t1_elo=1800),
            _mk_match(3, self.now - 300, [self.nigo, self.bineto], [137123, 198437], True, [self.br1, self.br2], [129494, 203852], t0_elo=900, t1_elo=1800),
        ]
        extracted = extract_brazilian_2v2_matches_from_dump(dump, self.vet_pids)
        built = build_side_teams_from_matches(extracted, min_games=3)
        self.assertEqual(len(built), 2)
        # Como cada dupla usa o ELO arranjado da dupla adversária (soma zero), ELO_A + ELO_B ≈ 2400.0
        self.assertAlmostEqual(built[0]["elo"] + built[1]["elo"], 2400.0, places=0)

    def test_extract_filters_non_2v2_custom_console_and_sanitizes_fields(self) -> None:
        evil_player = (1390350, "\u202eEvil\x00Name" + "X" * 100, "BR/../", "76561198089278659")
        dump = [
            _mk_match(1, self.now - 100, [evil_player, self.br2], [137123, 198437], True, [self.ger1, self.ger2], [129494, 203852], mtype=2),
            _mk_match(2, self.now - 100, [self.nigo, self.bineto], [137123, 198437], True, [self.ger1, self.ger2], [129494, 203852], mtype=0),  # custom
            _mk_match(3, self.now - 100, [self.nigo, self.bineto], [137123, 198437], True, [self.ger1, self.ger2], [129494, 203852], platform="xbox"),  # console
            _mk_match(4, self.now - 100, [self.nigo, self.ger1], [137123, 198437], True, [self.ger2, self.br1], [129494, 203852], mtype=21),  # mixed
        ]
        ext = extract_brazilian_2v2_matches_from_dump(dump, self.vet_pids)
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["match_id"], 1)
        self.assertNotIn("\u202e", ext[0]["p1_alias"])
        self.assertNotIn("\x00", ext[0]["p1_alias"])
        self.assertLessEqual(len(ext[0]["p1_alias"]), 80)
        self.assertEqual(ext[0]["p1_country"], "br")

    def test_unification_inactive_exclusion_and_csv_export(self) -> None:
        raw_teams = [
            # Dupla Veteranos ativa em ambos os lados -> média (1460 + 1380) / 2 = 1420.0
            {"id": "ax1", "type": "2v2", "side": "axis", "elo": 1460.0, "w": 10, "l": 5, "s": 3, "lmTS": self.now - 2 * 86400,
             "players": [{"profile_id": 26910, "alias": "nigo_BR ᵀᴹ", "country": "br"}, {"profile_id": 110078, "alias": "Bineto", "country": "br"}]},
            {"id": "al1", "type": "2v2", "side": "allies", "elo": 1380.0, "w": 8, "l": 2, "s": -1, "lmTS": self.now - 5 * 86400,
             "players": [{"profile_id": 110078, "alias": "Bineto", "country": "br"}, {"profile_id": 26910, "alias": "nigo_BR ᵀᴹ", "country": "br"}]},
            # Lem22 (US, Veterano) + BR não-Veterano: Axis ativo (ms timestamp), Allies inativo (> 30d)
            {"id": "ax2", "type": "2v2", "side": "axis", "elo": 1500.0, "w": 6, "l": 1, "s": 4, "lmTS": (self.now - 4 * 86400) * 1000,
             "players": [{"profile_id": 1390350, "alias": "=SUM(A1)", "country": "br"}, {"profile_id": 366826, "alias": "lem22", "country": "us"}]},
            {"id": "al2_old", "type": "2v2", "side": "allies", "elo": 1100.0, "w": 3, "l": 5, "s": -2, "lmTS": self.now - 45 * 86400,
             "players": [{"profile_id": 1390350, "alias": "=SUM(A1)", "country": "br"}, {"profile_id": 366826, "alias": "lem22", "country": "us"}]},
            # Time totalmente inativo (> 30d) -> deve ser excluído
            {"id": "ax3_old", "type": "2v2", "side": "axis", "elo": 1700.0, "w": 20, "l": 2, "s": 8, "lmTS": self.now - 31 * 86400,
             "players": [{"profile_id": 1390350, "alias": "Tears", "country": "br"}, {"profile_id": 9999991, "alias": "Charles", "country": "br"}]},
        ]
        unified = compute_unified_2v2_arranged_teams(raw_teams, custom_players=self.custom_players, inactive_days=30, now_ts=self.now)
        self.assertEqual(len(unified), 2)

        # #1: Lem22 + =SUM(A1) — somente Eixo ativo, Veterano ordenado primeiro
        self.assertEqual(unified[0]["br_rank"], 1)
        self.assertEqual(unified[0]["avg_elo"], 1500.0)
        self.assertEqual(unified[0]["sides"], ["axis"])
        self.assertIsNone(unified[0]["allies_elo"])
        self.assertEqual(unified[0]["players"][0]["alias"], "Lem22")
        self.assertEqual(unified[0]["veteranos_count"], 1)

        # #2: Bineto + nigo_BR — unificado Eixo + Aliados, streak mais recente (+3)
        self.assertEqual(unified[1]["br_rank"], 2)
        self.assertEqual(unified[1]["avg_elo"], 1420.0)
        self.assertEqual(unified[1]["sides"], ["axis", "allies"])
        self.assertEqual(unified[1]["wins"], 18)
        self.assertEqual(unified[1]["losses"], 7)
        self.assertEqual(unified[1]["streak"], 3)
        self.assertTrue(unified[1]["all_veteranos"])

        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "arranged.json"
            cp = Path(td) / "arranged.csv"
            export_arranged_teams_files(unified, jp, cp, inactive_days=30)
            doc = json.loads(jp.read_text(encoding="utf-8"))
            self.assertEqual(doc["total_teams"], 2)
            with cp.open(encoding="utf-8") as fp:
                rows = list(csv.DictReader(fp))
            self.assertTrue(rows[0]["player2_alias"].startswith("'="))

    def test_sqlite_arranged_cache_and_pruning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "cache.db"
            with RankingCacheDB(db_path) as db:
                old_row = {
                    "match_id": 1, "side": "axis", "pair_key": "1_2",
                    "p1_id": 1, "p1_alias": "A", "p1_country": "br", "p1_steam_id": "",
                    "p2_id": 2, "p2_alias": "B", "p2_country": "br", "p2_steam_id": "",
                    "won": 1, "enemy_elo": 1200.0, "completion_ts": 100000,
                }
                recent_ts = 10_000_000_000  # futuro distante para garantir retenção
                new_row = dict(old_row, match_id=2, completion_ts=recent_ts)
                db.save_arranged_2v2_matches([old_row, new_row])
                db.save_arranged_2v2_matches([old_row, new_row])  # idempotência
                self.assertEqual(len(db.load_all_arranged_2v2_matches()), 2)

                db.mark_arranged_day_processed(100000, matches_total=10, br_2v2_matches=1)
                db.mark_arranged_day_processed(recent_ts, matches_total=10, br_2v2_matches=1)
                pruned = db.prune_old_arranged_matches(max_age_days=365)
                self.assertEqual(pruned, 1)
                self.assertEqual(len(db.load_all_arranged_2v2_matches()), 1)
                self.assertEqual(db.get_processed_arranged_days(), {recent_ts})

    def test_merged_veterano_accounts_golden_cobra_and_other_br_nick_change_asterisk(self) -> None:
        # 1. Gabe (559962/557890) + arc (1005730/1423426) jogam 4 partidas usando combinações de contas alt -> unifica em 1 time Gabe + arc
        # 2. Golden Cobra (16867, COHBRAMACHINE) + outro BR (1333418) que muda de nick (Charles Bronson -> Lord Charles Bronson)
        rows = [
            {
                "match_id": 10, "side": "allies", "pair_key": "559962_1423426",
                "p1_id": 559962, "p1_alias": "É o Gabe", "p1_country": "br", "p1_steam_id": "76561198062871244",
                "p2_id": 1423426, "p2_alias": "uncle hans", "p2_country": "br", "p2_steam_id": "76561199482388323",
                "won": 1, "enemy_elo": 1350.0, "completion_ts": self.now - 4000,
            },
            {
                "match_id": 11, "side": "allies", "pair_key": "557890_1423426",
                "p1_id": 557890, "p1_alias": "gabe44arg", "p1_country": "br", "p1_steam_id": "76561199538846112",
                "p2_id": 1423426, "p2_alias": "dibas", "p2_country": "br", "p2_steam_id": "76561199482388323",
                "won": 1, "enemy_elo": 1350.0, "completion_ts": self.now - 3000,
            },
            {
                "match_id": 12, "side": "allies", "pair_key": "559962_1005730",
                "p1_id": 559962, "p1_alias": "Gabe", "p1_country": "br", "p1_steam_id": "76561198062871244",
                "p2_id": 1005730, "p2_alias": "QualquerNickMaluco", "p2_country": "br", "p2_steam_id": "76561199791811246",
                "won": 1, "enemy_elo": 1350.0, "completion_ts": self.now - 2000,
            },
            # Golden Cobra (16867) + 1333418 (muda de nick durante o histórico)
            {
                "match_id": 20, "side": "axis", "pair_key": "16867_1333418",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 1333418, "p2_alias": "Charles Bronson", "p2_country": "br", "p2_steam_id": "",
                "won": 1, "enemy_elo": 1200.0, "completion_ts": self.now - 5000,
            },
            {
                "match_id": 21, "side": "axis", "pair_key": "16867_1333418",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 1333418, "p2_alias": "Charles Bronson", "p2_country": "br", "p2_steam_id": "",
                "won": 1, "enemy_elo": 1200.0, "completion_ts": self.now - 4000,
            },
            {
                "match_id": 22, "side": "axis", "pair_key": "16867_1333418",
                "p1_id": 16867, "p1_alias": "COHBRAMACHINE", "p1_country": "br", "p1_steam_id": "76561198874381986",
                "p2_id": 1333418, "p2_alias": "Lord Charles Bronson", "p2_country": "br", "p2_steam_id": "",
                "won": 0, "enemy_elo": 1200.0, "completion_ts": self.now - 1000,
            },
        ]

        built = build_side_teams_from_matches(rows, min_games=3)
        unified = compute_unified_2v2_arranged_teams(built, inactive_days=30, now_ts=self.now)
        self.assertEqual(len(unified), 2)

        by_key = {t["pair_key"]: t for t in unified}
        self.assertIn("559962_1005730", by_key)
        gabe_arc = by_key["559962_1005730"]
        self.assertTrue(gabe_arc["all_veteranos"])
        self.assertEqual(gabe_arc["wins"], 3)
        self.assertEqual([p["alias"] for p in gabe_arc["players"]], ["arc", "Gabe"])

        self.assertIn("16867_1333418", by_key)
        cobra_team = by_key["16867_1333418"]
        self.assertFalse(cobra_team["has_veterano"])
        by_pid = {p["profile_id"]: p for p in cobra_team["players"]}
        self.assertEqual(by_pid[16867]["alias"], "Golden Cobra")
        self.assertFalse(by_pid[16867]["is_veterano"])
        self.assertEqual(by_pid[1333418]["alias"], "Charles Bronson* (Lord Charles Bronson)")


if __name__ == "__main__":
    unittest.main()
