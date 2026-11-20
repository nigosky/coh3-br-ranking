"""Testes unitários das regras de elenco (roster), ranking individual unificado e exportações."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from relic_ranking import (
    _merge_player_stat_records,
    compute_unified_skill_ranking,
    export_unified_skill_ranking,
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
            format_bound_display_alias("É o Gabe", "Gabe", ["É o Gabe", "Gabe"]),
            "Gabe",
        )
        self.assertEqual(
            format_bound_display_alias("NovoNick", "Chacineiro"),
            "Chacineiro* (NovoNick)",
        )
        self.assertEqual(
            format_bound_display_alias("Chacineiro (NovoNick)", "Chacineiro"),
            "Chacineiro* (NovoNick)",
        )
        self.assertEqual(
            format_bound_display_alias("Chacineiro* (NovoNick)", "Chacineiro"),
            "Chacineiro* (NovoNick)",
        )
        # pin_alias=True (ex.: arc) sempre mantém o nick original fixo sem asterisco
        self.assertEqual(
            format_bound_display_alias("geladeira velha", "arc", pin_alias=True),
            "arc",
        )
        self.assertEqual(
            format_bound_display_alias("QualquerNickNovo", "arc", ["uncle hans"], pin_alias=True),
            "arc",
        )

    def test_ensure_core_roster_in_config_idempotent(self) -> None:
        cfg: dict = {"custom_players": []}
        self.assertTrue(ensure_core_roster_in_config(cfg))
        self.assertEqual(len(cfg["custom_players"]), len(CORE_BR_ROSTER))
        self.assertFalse(ensure_core_roster_in_config(cfg))

    def test_shared_roster_filters_strictly_listed_veteranos_blocks_ssrf_and_prunes_removed(self) -> None:
        from roster import (
            FALLBACK_CORE_BR_ROSTER,
            FALLBACK_MANUAL_OTHER_BR_ALIASES,
            MANUAL_OTHER_BR_ALIASES,
            fetch_shared_ranking_roster,
            parse_shared_manual_other_br_aliases,
            parse_shared_ranking_roster,
            refresh_core_br_roster,
        )

        # 1. Segurança SSRF: bloqueia HTTP / host externo / caminho fora da allowlist
        for bad_url in (
            "http://raw.githubusercontent.com/nigosky/coh3-br-lobby-radar/main/players_roster.json",
            "https://evil.com/nigosky/coh3-br-lobby-radar/main/players_roster.json",
            "https://raw.githubusercontent.com/attacker/repo/main/players_roster.json",
        ):
            with self.assertRaises(ValueError):
                fetch_shared_ranking_roster(url=bad_url)

        # 2. Ranking inclui APENAS veteranos com in_ranking != False (exclui manual_other_br e in_ranking=False)
        payload = {
            "schema_version": 1,
            "veteranos": [
                {"profile_id": 79327, "steam_id": "76561197963172584", "original_alias": "Chacineiro", "country": "br", "in_ranking": True},
                {"profile_id": 366826, "steam_id": "76561198014985380", "original_alias": "Lem22", "country": "us", "in_ranking": True},
                {"profile_id": 1005730, "steam_id": "76561199791811246", "original_alias": "arc", "pin_alias": True, "in_ranking": True},
                {"profile_id": 999111, "steam_id": "76561198000999111", "original_alias": "ForaDoRanking", "country": "br", "in_ranking": False},
            ],
            "manual_other_br": [
                {"profile_id": 33487, "steam_id": "76561197995712470", "original_alias": "Generick", "country": "us"},
                {"profile_id": 16867, "steam_id": "76561198874381986", "original_alias": "Golden Cobra", "known_aliases": ["COHBRAMACHINE", "Golden Cobra"]},
            ],
        }
        parsed = parse_shared_ranking_roster(payload)
        self.assertEqual([int(r["profile_id"]) for r in parsed], [79327, 366826, 1005730])
        self.assertEqual(parsed[1].get("country"), "us")
        self.assertTrue(parsed[2].get("pin_alias"))

        parsed_manual = parse_shared_manual_other_br_aliases(payload)
        self.assertEqual([int(r["profile_id"]) for r in parsed_manual], [33487, 16867])
        self.assertEqual(parsed_manual[1]["original_alias"], "Golden Cobra")

        # 3. Sincronização completa (adição + bandeira + pin_alias + remoção de Veterano excluído; manual_other_br fora do Ranking)
        try:
            with tempfile.TemporaryDirectory() as td:
                rp = Path(td) / "players_roster.json"
                rp.write_text(json.dumps(payload), encoding="utf-8")
                self.assertTrue(refresh_core_br_roster(local_path=rp))
                cfg = {
                    "custom_players": [
                        {
                            "profile_id": 79327,
                            "steam_id": "76561197963172584",
                            "original_alias": "Chacineiro",
                            "alias": "Chacineiro",
                            "is_core_roster": True,
                        },
                        {
                            "profile_id": 1005730,
                            "steam_id": "76561199791811246",
                            "original_alias": "arc",
                            "alias": "arc (geladeira velha)",
                            "is_core_roster": True,
                        },
                        {
                            "profile_id": 555000,
                            "steam_id": "76561198000555000",
                            "original_alias": "VeteranoRemovido",
                            "alias": "VeteranoRemovido",
                            "is_core_roster": True,
                        },
                    ]
                }
                self.assertTrue(ensure_core_roster_in_config(cfg))
                by_pid = {int(c["profile_id"]): c for c in cfg["custom_players"]}
                self.assertEqual(sorted(by_pid.keys()), [79327, 366826, 1005730])
                self.assertEqual(by_pid[366826].get("country"), "us")
                self.assertTrue(by_pid[1005730].get("pin_alias"))
                self.assertEqual(by_pid[1005730].get("alias"), "arc")
                self.assertNotIn(555000, by_pid)
                self.assertNotIn(33487, by_pid)
                self.assertNotIn(16867, by_pid)
                self.assertNotIn(999111, by_pid)
                self.assertEqual([int(m["profile_id"]) for m in MANUAL_OTHER_BR_ALIASES], [33487, 16867])
                self.assertFalse(ensure_core_roster_in_config(cfg))
        finally:
            CORE_BR_ROSTER[:] = [dict(r) for r in FALLBACK_CORE_BR_ROSTER]
            MANUAL_OTHER_BR_ALIASES[:] = [dict(r) for r in FALLBACK_MANUAL_OTHER_BR_ALIASES]


class TestUnifiedSkillPointsRules(unittest.TestCase):
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
                },
                "inactive_modes": {},
            },
            {
                # 1v1: max(1600 active, 1400 active; 1900 w/ 9 games ignorado) => M_1v1=1600 -> P_1v1=2200 (âncora)
                # 2v2: 1400 inativa (15 jogos) => M_2v2=1400 -> P_2v2=1600 (excedente 600)
                # 3v3: 1200 ativa (12 jogos) => M_3v3=1200 -> P_3v3=1200 (excedente 200)
                # 4v4: 900 ativa (20 jogos) => M_4v4=900 -> P_4v4=900 (excedente 0)
                # Skill Points = 2200 + 0.15 * (600 + 200 + 0) = 2320.0
                "profile_id": 101,
                "alias": "VetActive_Multi",
                "steam_id": "76561198000000101",
                "country": "br",
                "is_custom": True,
                "modes": {
                    "1v1_american": {"mode": "1v1", "faction": "american", "elo": 1600, "wins": 30, "losses": 10, "streak": 2, "lastmatchdate": 1794900000},
                    "1v1_british": {"mode": "1v1", "faction": "british", "elo": 1900, "wins": 8, "losses": 1, "streak": 8, "lastmatchdate": 1794950000},
                    "1v1_german": {"mode": "1v1", "faction": "german", "elo": 1400, "wins": 20, "losses": 20, "streak": -1, "lastmatchdate": 1795000000},
                    "3v3_american": {"mode": "3v3", "faction": "american", "elo": 1200, "wins": 7, "losses": 5, "streak": 1, "lastmatchdate": 1794900000},
                    "4v4_dak": {"mode": "4v4", "faction": "dak", "elo": 900, "wins": 8, "losses": 12, "streak": 4, "lastmatchdate": 1795050000},
                },
                "inactive_modes": {
                    "2v2_german": {"mode": "2v2", "faction": "german", "elo": 1400, "wins": 10, "losses": 5, "streak": 2, "lastmatchdate": 1785000000},
                },
            },
            {
                # Mono-facção 2v2: 1200 (20 jogos) -> P_2v2 = 1300.0
                "profile_id": 102,
                "alias": "VetActive_Mono",
                "steam_id": "76561198000000102",
                "country": "br",
                "is_custom": True,
                "modes": {
                    "2v2_british": {"mode": "2v2", "faction": "british", "elo": 1200, "wins": 10, "losses": 10, "streak": 0, "lastmatchdate": 1794800000},
                },
                "inactive_modes": {},
            },
            {
                # 100% inativo (modes vazio), mas com 1v1 inativa de 1750 (250 jogos) -> P_1v1 = 2500.0
                # Deve ficar ABAIXO dos ativos com inactive_fallback=True
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
            {
                # Sem nenhuma fila com >= 10 partidas (ex.: MonteiroBR): não entra na tabela
                "profile_id": 104,
                "alias": "VetUncalibrated",
                "steam_id": "76561198000000104",
                "country": "br",
                "is_custom": True,
                "modes": {
                    "1v1_american": {"mode": "1v1", "faction": "american", "elo": 1500, "wins": 5, "losses": 4, "streak": 1, "lastmatchdate": 1794800000},
                },
                "inactive_modes": {},
            },
        ]

    def test_exact_skill_points_formula_and_filtering(self) -> None:
        ranking = compute_unified_skill_ranking(
            self._sample_players(),
            custom_ids={101, 102, 103, 104},
            only_custom=True,
        )
        # Exclui OutroBR_Top1 (não-custom) e VetUncalibrated (<10 jogos)
        self.assertEqual([e["alias"] for e in ranking], ["VetActive_Multi", "VetActive_Mono", "VetInactive_HighELO"])
        self.assertEqual([e["br_rank"] for e in ranking], [1, 2, 3])
        self.assertEqual([e["inactive_fallback"] for e in ranking], [False, False, True])

        # Verificação matemática exata de VetActive_Multi
        top = ranking[0]
        self.assertEqual(top["mode_max_elos"], {"1v1": 1600, "2v2": 1400, "3v3": 1200, "4v4": 900})
        self.assertEqual(top["anchor_mode"], "1v1")
        self.assertEqual(top["anchor_score"], 2200.0)
        self.assertEqual(top["bonus_score"], 120.0)
        self.assertEqual(top["skill_points"], 2320.0)
        self.assertEqual(top["last_match_date"], 1795050000)

        # Mono-facção sem penalização
        self.assertEqual(ranking[1]["anchor_mode"], "2v2")
        self.assertEqual(ranking[1]["skill_points"], 1300.0)

        # Inativo 100% usa filas inativas para Skill Points, mas fica abaixo dos ativos
        self.assertEqual(ranking[2]["skill_points"], 2500.0)
        self.assertTrue(ranking[2]["inactive_fallback"])

    def test_non_punitive_and_mono_faction_invariants(self) -> None:
        base_player = {
            "profile_id": 201,
            "alias": "Specialist1v1",
            "steam_id": "76561198000000201",
            "is_custom": True,
            "modes": {
                "1v1_german": {"mode": "1v1", "faction": "german", "elo": 1600, "wins": 30, "losses": 10, "lastmatchdate": 1000},
            },
            "inactive_modes": {},
        }
        r_base = compute_unified_skill_ranking([base_player], custom_ids={201})[0]
        self.assertEqual(r_base["skill_points"], 2200.0)

        # Jogar 4v4 casual a 1300 ELO só ADICIONA pontos (+45.0), nunca derruba
        with_4v4_1300 = dict(base_player, modes={
            **base_player["modes"],
            "4v4_american": {"mode": "4v4", "faction": "american", "elo": 1300, "wins": 10, "losses": 10, "lastmatchdate": 2000},
        })
        r_plus = compute_unified_skill_ranking([with_4v4_1300], custom_ids={201})[0]
        self.assertEqual(r_plus["skill_points"], 2245.0)
        self.assertGreater(r_plus["skill_points"], r_base["skill_points"])

        # Jogar 4v4 abaixo de 1000 ELO (ex.: 750) nunca penaliza (bônus = 0)
        with_4v4_750 = dict(base_player, modes={
            **base_player["modes"],
            "4v4_dak": {"mode": "4v4", "faction": "dak", "elo": 750, "wins": 5, "losses": 15, "lastmatchdate": 2000},
        })
        r_low = compute_unified_skill_ranking([with_4v4_750], custom_ids={201})[0]
        self.assertEqual(r_low["skill_points"], 2200.0)

        # Jogador com todos os modos abaixo de 1000 escolhe maior P_m (3v3=925 > 1v1=496)
        low_all = {
            "profile_id": 202,
            "alias": "BelowBase",
            "steam_id": "76561198000000202",
            "is_custom": True,
            "modes": {
                "1v1_american": {"mode": "1v1", "faction": "american", "elo": 748, "wins": 5, "losses": 10, "lastmatchdate": 1000},
                "3v3_british": {"mode": "3v3", "faction": "british", "elo": 925, "wins": 8, "losses": 8, "lastmatchdate": 1000},
            },
            "inactive_modes": {},
        }
        r_below = compute_unified_skill_ranking([low_all], custom_ids={202})[0]
        self.assertEqual(r_below["anchor_mode"], "3v3")
        self.assertEqual(r_below["skill_points"], 925.0)
        self.assertEqual(r_below["bonus_score"], 0.0)

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

    def test_export_unified_skill_ranking_filters_custom_and_escapes_csv_formulas(self) -> None:
        mixed = compute_unified_skill_ranking(self._sample_players(), custom_ids={101, 102, 103}, only_custom=False)
        for entry in mixed:
            if entry["profile_id"] == 102:
                entry["alias"] = '=HYPERLINK("https://evil.test")'

        with tempfile.TemporaryDirectory() as td:
            jpath = Path(td) / "unified.json"
            cpath = Path(td) / "unified.csv"
            exported = export_unified_skill_ranking(
                mixed,
                json_path=jpath,
                csv_path=cpath,
                custom_players=[{"profile_id": 101}, {"profile_id": 102}, {"profile_id": 103}],
                only_custom=True,
            )
            self.assertEqual([e["profile_id"] for e in exported], [101, 102, 103])
            self.assertEqual([e["br_rank"] for e in exported], [1, 2, 3])

            doc = json.loads(jpath.read_text(encoding="utf-8"))
            self.assertEqual(doc["methodology"], "unified_skill_points_anchor_plus_secondary_bonus")
            self.assertEqual(doc["total_players"], 3)
            self.assertEqual(doc["active_players"], 2)
            self.assertEqual(doc["inactive_players"], 1)

            with cpath.open(encoding="utf-8") as fp:
                rows = list(csv.DictReader(fp))
            evil_row = next(r for r in rows if r["profile_id"] == "102")
            self.assertTrue(evil_row["display_alias"].startswith("'="))
            top_row = next(r for r in rows if r["profile_id"] == "101")
            self.assertEqual(top_row["anchor_mode"], "1v1")
            self.assertEqual(top_row["elo_1v1"], "1600")
            self.assertEqual(top_row["elo_2v2"], "1400")

    def test_digests_are_deterministic_and_sensitive(self) -> None:
        r1 = compute_unified_skill_ranking(self._sample_players(), custom_ids={101, 102, 103}, only_custom=True)
        d1 = compute_rankings_digest(r1)
        d2 = compute_rankings_digest(r1)
        self.assertEqual(d1, d2)
        r1[0]["skill_points"] += 5.0
        self.assertNotEqual(d1, compute_rankings_digest(r1))

        t_list = [{"pair_key": "1_2", "br_rank": 1, "avg_elo": 1300.0, "sides": ["axis"], "wins": 5, "losses": 2, "streak": 2, "last_match_date": 1000, "players": [{"alias": "A"}, {"alias": "B"}], "has_veterano": True}]
        td1 = compute_arranged_teams_digest(t_list)
        t_list[0]["avg_elo"] = 1310.0
        self.assertNotEqual(td1, compute_arranged_teams_digest(t_list))


if __name__ == "__main__":
    unittest.main()
