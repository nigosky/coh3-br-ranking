"""Testes unitários das regras de elenco (roster), ranking individual unificado e exportações."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from relic_ranking import (
    _merge_player_stat_records,
    compute_merged_mode_rankings,
    export_merged_top_rankings,
)
from roster import (
    CORE_BR_ROSTER,
    STEAM_ID64_VALID_RE,
    count_unique_roster_players,
    ensure_core_roster_in_config,
    extract_steam_id,
    format_bound_display_alias,
    parse_profile_id_input,
    parse_steam_id_input,
)
from run_ranking import compute_arranged_teams_digest, compute_rankings_digest


class TestRosterRules(unittest.TestCase):
    def test_core_roster_integrity(self) -> None:
        self.assertGreaterEqual(len(CORE_BR_ROSTER), 40)
        seen_pids: set[int] = set()
        seen_sids: set[str] = set()
        merged_targets: list[int] = []

        for entry in CORE_BR_ROSTER:
            pid = int(entry["profile_id"])
            sid = str(entry["steam_id"])
            self.assertGreater(pid, 0)
            self.assertIsNotNone(STEAM_ID64_VALID_RE.fullmatch(sid), f"SteamID inválido: {sid}")
            self.assertNotIn(pid, seen_pids, f"profile_id duplicado: {pid}")
            self.assertNotIn(sid, seen_sids, f"steam_id duplicado: {sid}")
            seen_pids.add(pid)
            seen_sids.add(sid)
            if entry.get("merged_into_profile_id"):
                merged_targets.append(int(entry["merged_into_profile_id"]))

        for target in merged_targets:
            self.assertIn(target, seen_pids, f"Conta alvo de fusão não existe no roster: {target}")

        unique_cnt = count_unique_roster_players(CORE_BR_ROSTER)
        self.assertEqual(unique_cnt, len(CORE_BR_ROSTER) - len(merged_targets))

    def test_id_parsers(self) -> None:
        self.assertEqual(extract_steam_id("/steam/76561197963172584"), "76561197963172584")
        self.assertEqual(extract_steam_id("76561197963172584"), "76561197963172584")
        self.assertEqual(extract_steam_id("not_a_steam_id"), "")

        self.assertEqual(
            parse_steam_id_input("https://steamcommunity.com/profiles/76561197963172584/"),
            "76561197963172584",
        )
        self.assertIsNone(parse_steam_id_input("https://coh3stats.com/players/79327"))

        self.assertEqual(parse_profile_id_input("79327"), 79327)
        self.assertEqual(parse_profile_id_input("https://coh3stats.com/players/79327"), 79327)
        self.assertEqual(parse_profile_id_input("https://coh3stats.com/pt-BR/players/559962/eo-gabe"), 559962)
        self.assertIsNone(parse_profile_id_input("76561197963172584"))
        self.assertIsNone(parse_profile_id_input("--inject"))

    def test_format_bound_display_alias(self) -> None:
        self.assertEqual(format_bound_display_alias("Chacineiro", "Chacineiro"), "Chacineiro")
        self.assertEqual(format_bound_display_alias("chacineiro", "Chacineiro"), "Chacineiro")
        self.assertEqual(
            format_bound_display_alias("Minhoca de ferro!", "Alekel", ["Minhoca de ferro!", "Alekel"]),
            "Alekel",
        )
        self.assertEqual(
            format_bound_display_alias("NovoNick", "Chacineiro"),
            "Chacineiro (NovoNick)",
        )
        self.assertEqual(
            format_bound_display_alias("Chacineiro (NovoNick)", "Chacineiro"),
            "Chacineiro (NovoNick)",
        )

    def test_ensure_core_roster_in_config_idempotent(self) -> None:
        cfg: dict = {"custom_players": []}
        self.assertTrue(ensure_core_roster_in_config(cfg))
        self.assertEqual(len(cfg["custom_players"]), len(CORE_BR_ROSTER))
        self.assertFalse(ensure_core_roster_in_config(cfg))


class TestUnifiedRankingRules(unittest.TestCase):
    def _sample_players(self) -> list[dict]:
        return [
            {
                "profile_id": 9001,
                "alias": "OutroBR_Top1",
                "steam_id": "76561198000009001",
                "country": "br",
                "is_custom": False,
                "modes": {
                    "1v1_american": {"mode": "1v1", "faction": "american", "elo": 1850, "wins": 80, "losses": 10, "streak": 7, "lastmatchdate": 1795000000},
                    "3v3_american": {"mode": "3v3", "faction": "american", "elo": 1700, "wins": 40, "losses": 10, "streak": 3, "lastmatchdate": 1795000000},
                },
                "inactive_modes": {},
            },
            {
                "profile_id": 101,
                "alias": "VetActive_Multi",
                "steam_id": "76561198000000101",
                "country": "br",
                "is_custom": True,
                "modes": {
                    "1v1_american": {"mode": "1v1", "faction": "american", "elo": 1500, "wins": 30, "losses": 10, "streak": 2, "lastmatchdate": 1794900000},
                    "1v1_german": {"mode": "1v1", "faction": "german", "elo": 1400, "wins": 20, "losses": 20, "streak": -1, "lastmatchdate": 1795000000},
                    "3v3_american": {"mode": "3v3", "faction": "american", "elo": 1300, "wins": 10, "losses": 5, "streak": 1, "lastmatchdate": 1794900000},
                    "4v4_dak": {"mode": "4v4", "faction": "dak", "elo": 1500, "wins": 12, "losses": 8, "streak": 4, "lastmatchdate": 1795050000},
                },
                "inactive_modes": {},
            },
            {
                "profile_id": 102,
                "alias": "VetActive_Lower",
                "steam_id": "76561198000000102",
                "country": "br",
                "is_custom": True,
                "modes": {
                    "1v1_british": {"mode": "1v1", "faction": "british", "elo": 1200, "wins": 15, "losses": 15, "streak": 0, "lastmatchdate": 1794800000},
                },
                "inactive_modes": {},
            },
            {
                "profile_id": 103,
                "alias": "VetInactive_HighELO",
                "steam_id": "76561198000000103",
                "country": "br",
                "is_custom": True,
                "modes": {},
                "inactive_modes": {
                    "1v1_dak": {"mode": "1v1", "faction": "dak", "elo": 1750, "wins": 200, "losses": 50, "streak": 5, "lastmatchdate": 1780000000},
                },
            },
        ]

    def test_only_custom_excludes_other_brazilians_and_orders_active_before_inactive(self) -> None:
        rankings = compute_merged_mode_rankings(
            self._sample_players(),
            custom_ids={101, 102, 103},
            only_custom=True,
        )
        r1v1 = rankings["1v1"]
        self.assertEqual([e["alias"] for e in r1v1], ["VetActive_Multi", "VetActive_Lower", "VetInactive_HighELO"])
        self.assertEqual([e["br_rank"] for e in r1v1], [1, 2, 3])
        self.assertEqual([e["inactive_fallback"] for e in r1v1], [False, False, True])

        # Média simples de 1500 e 1400 = 1450.0; streak mais recente (-1)
        self.assertEqual(r1v1[0]["avg_elo"], 1450.0)
        self.assertEqual(r1v1[0]["wins"], 50)
        self.assertEqual(r1v1[0]["losses"], 30)
        self.assertEqual(r1v1[0]["streak"], -1)

        # Team Games unifica 3v3 (1300) + 4v4 (1500) = 1400.0 e exclui OutroBR_Top1
        rtg = rankings["Team Games"]
        self.assertEqual(len(rtg), 1)
        self.assertEqual(rtg[0]["alias"], "VetActive_Multi")
        self.assertEqual(rtg[0]["avg_elo"], 1400.0)
        self.assertEqual(rtg[0]["streak"], 4)

    def test_merge_secondary_account_records(self) -> None:
        primary = {
            "profile_id": 559962,
            "total_wins": 20,
            "total_losses": 10,
            "max_elo": 1200,
            "best_mode": "2V2 (AMERICAN)",
            "last_match_date": 1000,
            "modes": {
                "2v2_american": {"mode": "2v2", "faction": "american", "elo": 1200, "wins": 20, "losses": 10, "streak": 1, "lastmatchdate": 1000},
            },
            "inactive_modes": {},
        }
        secondary = {
            "profile_id": 557890,
            "total_wins": 15,
            "total_losses": 5,
            "max_elo": 1350,
            "best_mode": "2V2 (AMERICAN)",
            "last_match_date": 2000,
            "modes": {
                "2v2_american": {"mode": "2v2", "faction": "american", "elo": 1350, "wins": 15, "losses": 5, "streak": 3, "lastmatchdate": 2000},
                "3v3_british": {"mode": "3v3", "faction": "british", "elo": 1250, "wins": 8, "losses": 2, "streak": 2, "lastmatchdate": 1800},
            },
            "inactive_modes": {},
        }
        _merge_player_stat_records(primary, secondary)
        self.assertEqual(primary["total_wins"], 35)
        self.assertEqual(primary["total_losses"], 15)
        self.assertEqual(primary["max_elo"], 1350)
        self.assertEqual(primary["modes"]["2v2_american"]["elo"], 1350)
        self.assertEqual(primary["modes"]["2v2_american"]["wins"], 35)
        self.assertEqual(primary["modes"]["2v2_american"]["streak"], 3)
        self.assertIn("3v3_british", primary["modes"])

    def test_export_merged_top_rankings_filters_custom_and_escapes_csv_formulas(self) -> None:
        mixed = compute_merged_mode_rankings(self._sample_players(), custom_ids={101, 102, 103}, only_custom=False)
        # Injetar apelido com fórmula maliciosa para validar sanitização CSV
        for entry in mixed["1v1"]:
            if entry["profile_id"] == 102:
                entry["alias"] = "=HYPERLINK(\"https://evil.test\")"

        with tempfile.TemporaryDirectory() as td:
            jpath = Path(td) / "top.json"
            cpath = Path(td) / "top.csv"
            trimmed = export_merged_top_rankings(
                mixed,
                top_n=20,
                json_path=jpath,
                csv_path=cpath,
                custom_players=[{"profile_id": 101}, {"profile_id": 102}, {"profile_id": 103}],
                only_custom=True,
            )
            self.assertEqual([e["profile_id"] for e in trimmed["1v1"]], [101, 102, 103])
            self.assertEqual([e["br_rank"] for e in trimmed["1v1"]], [1, 2, 3])

            doc = json.loads(jpath.read_text(encoding="utf-8"))
            self.assertEqual(doc["unique_players_count"], 3)

            with cpath.open(encoding="utf-8") as fp:
                rows = list(csv.DictReader(fp))
            evil_row = next(r for r in rows if r["profile_id"] == "102" and r["mode"] == "1v1")
            self.assertTrue(evil_row["display_alias"].startswith("'="))

    def test_digests_are_deterministic_and_sensitive(self) -> None:
        r1 = compute_merged_mode_rankings(self._sample_players(), custom_ids={101, 102, 103}, only_custom=True)
        d1 = compute_rankings_digest(r1)
        d2 = compute_rankings_digest(r1)
        self.assertEqual(d1, d2)
        r1["1v1"][0]["avg_elo"] += 5.0
        self.assertNotEqual(d1, compute_rankings_digest(r1))

        t_list = [{"pair_key": "1_2", "br_rank": 1, "avg_elo": 1300.0, "sides": ["axis"], "wins": 5, "losses": 2, "streak": 2, "last_match_date": 1000, "players": [{"alias": "A"}, {"alias": "B"}], "has_veterano": True}]
        td1 = compute_arranged_teams_digest(t_list)
        t_list[0]["avg_elo"] = 1310.0
        self.assertNotEqual(td1, compute_arranged_teams_digest(t_list))


if __name__ == "__main__":
    unittest.main()
