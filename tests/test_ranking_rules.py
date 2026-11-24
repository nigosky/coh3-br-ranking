"""Testes unitários das regras de elenco (roster), ranking individual unificado e exportações."""

from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from anti_carry import KNOWN_SMURF_PIDS
from relic_ranking import (
    _ingest_personal_stat_payload,
    compute_unified_skill_ranking,
    export_unified_skill_ranking,
)
from roster import (
    CORE_BR_ROSTER,
    MANUAL_OTHER_BR_ALIASES,
    STEAM_ID64_VALID_RE,
    build_roster_lookup_maps,
    count_smurf_roster_players,
    count_unique_roster_players,
    ensure_core_roster_in_config,
    extract_smurf_owner,
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

        for entry in CORE_BR_ROSTER:
            pid = int(entry["profile_id"])
            sid = str(entry["steam_id"])
            self.assertGreater(pid, 0)
            self.assertIsNotNone(STEAM_ID64_VALID_RE.fullmatch(sid), f"SteamID inválido: {sid}")
            self.assertNotIn(pid, seen_pids, f"profile_id duplicado: {pid}")
            self.assertNotIn(sid, seen_sids, f"steam_id duplicado: {sid}")
            self.assertFalse(entry.get("merged_into_profile_id"), f"Smurf não deve sofrer merge: {pid}")
            seen_pids.add(pid)
            seen_sids.add(sid)

        self.assertEqual(count_unique_roster_players(CORE_BR_ROSTER), len(CORE_BR_ROSTER))
        self.assertEqual(count_smurf_roster_players(CORE_BR_ROSTER), 6)
        self.assertNotIn(1076221, seen_pids, "Lucielle (Smurf: Colossus) não pertence aos Veteranos.")
        other_by_pid = {int(e["profile_id"]): e for e in MANUAL_OTHER_BR_ALIASES}
        self.assertIn(1076221, other_by_pid)
        self.assertEqual(other_by_pid[1076221]["original_alias"], "Lucielle (Smurf: Colossus)")
        self.assertIn(1297853, other_by_pid)
        self.assertEqual(other_by_pid[1297853]["original_alias"], "Shadow (Smurf: Alekel)")
        self.assertEqual(other_by_pid[1297853].get("smurf_of"), "Alekel")
        self.assertIn(164152, other_by_pid)
        self.assertEqual(other_by_pid[164152]["original_alias"], "Sgt.Yoda")
        self.assertFalse(other_by_pid[164152].get("smurf_of"))
        self.assertNotIn(164152, KNOWN_SMURF_PIDS)
        for mon_pid in (941524, 40833, 991551, 667564):
            self.assertIn(mon_pid, other_by_pid)

    def test_extract_smurf_owner_all_naming_conventions(self) -> None:
        self.assertEqual(extract_smurf_owner("Charles Bronson (Chacineiro)", pin_alias=True), "Chacineiro")
        self.assertEqual(extract_smurf_owner("Charles Bronson (Smurf: Chacineiro)"), "Chacineiro")
        self.assertEqual(extract_smurf_owner("TearsInTheRain (Chacineiro)", pin_alias=True), "Chacineiro")
        self.assertEqual(extract_smurf_owner("gabe44arg (Gabe)", pin_alias=True), "Gabe")
        self.assertEqual(extract_smurf_owner("geladeira velha (arc)", pin_alias=True), "arc")
        self.assertEqual(extract_smurf_owner("Državljan H (Smurf: arc)"), "arc")
        self.assertEqual(extract_smurf_owner("Vog (Smurf: arc)"), "arc")
        self.assertEqual(extract_smurf_owner("Lucielle (Colossus)", pin_alias=True), "Colossus")
        self.assertEqual(extract_smurf_owner("General Winter (Smurf 1)", pin_alias=True), "General Winter")
        self.assertEqual(extract_smurf_owner("General Winter (Smurf 2)"), "General Winter")
        self.assertEqual(extract_smurf_owner("Shadow (Smurf: Alekel)"), "Alekel")
        self.assertEqual(extract_smurf_owner("Leite Moço (Smurf: 『V』SKOBADARK)"), "『V』SKOBADARK")
        self.assertEqual(extract_smurf_owner("QualquerNick", explicit_owner="Chacineiro"), "Chacineiro")
        # Não-Smurfs não devem gerar falso positivo
        self.assertEqual(extract_smurf_owner("General Winter (Principal)", pin_alias=True), "")
        self.assertEqual(extract_smurf_owner("Chacineiro* (NovoNick)", pin_alias=True), "")
        self.assertEqual(extract_smurf_owner("JogadorAleatorio (BR)", pin_alias=False), "")
        self.assertEqual(extract_smurf_owner("SaNgar [BR]", pin_alias=True), "")

    def test_suspected_foreign_vpn_exclusion_ssrf_and_refresh(self) -> None:
        from roster import (
            EXCLUDED_SUSPECTED_FOREIGN_PIDS,
            FALLBACK_EXCLUDED_SUSPECTED_FOREIGN_PIDS,
            fetch_suspected_foreign_br_pids,
            is_excluded_suspected_foreign_pid,
            parse_suspected_foreign_excluded_pids,
            refresh_suspected_foreign_br_pids,
        )

        # Fallback inclui Nocturne (1142621) desde o boot
        self.assertIn(1142621, FALLBACK_EXCLUDED_SUSPECTED_FOREIGN_PIDS)
        self.assertTrue(is_excluded_suspected_foreign_pid(1142621))
        self.assertFalse(is_excluded_suspected_foreign_pid(1142621, protected_pids={1142621}))

        # Proteção SSRF
        for bad_url in (
            "http://raw.githubusercontent.com/nigosky/coh3-br-lobby-radar/main/suspected_foreign_br.json",
            "https://evil.com/nigosky/coh3-br-lobby-radar/main/suspected_foreign_br.json",
            "https://raw.githubusercontent.com/attacker/repo/main/suspected_foreign_br.json",
        ):
            with self.assertRaises(ValueError):
                fetch_suspected_foreign_br_pids(url=bad_url)

        doc = {
            "excluded_from_rankings_pids": [1142621, 3984],
            "players": {
                "1142621": {"profile_id": 1142621, "status": "suspected_foreign_vpn", "exclude_from_rankings": True},
                "20523": {"profile_id": 20523, "status": "confirmed_br", "exclude_from_rankings": False},
                "999888": {"profile_id": 999888, "status": "confirmed_foreign_vpn", "exclude_from_rankings": True},
            },
        }
        parsed = parse_suspected_foreign_excluded_pids(doc)
        self.assertEqual(parsed, {1142621, 3984, 999888})
        self.assertNotIn(20523, parsed)

        snapshot = set(EXCLUDED_SUSPECTED_FOREIGN_PIDS)
        try:
            with tempfile.TemporaryDirectory() as td:
                sp = Path(td) / "suspected_foreign_br.json"
                sp.write_text(json.dumps(doc), encoding="utf-8")
                self.assertTrue(refresh_suspected_foreign_br_pids(local_path=sp))
                self.assertTrue(is_excluded_suspected_foreign_pid(999888))
                self.assertFalse(is_excluded_suspected_foreign_pid(20523))

                # Ranking unificado ignora não-custom suspeito de VPN mesmo com only_custom=False
                mixed_vpn = [
                    {
                        "profile_id": 1142621,
                        "alias": "Nocturne",
                        "steam_id": "76561198817268049",
                        "country": "br",
                        "is_custom": False,
                        "modes": {"2v2_american": {"mode": "2v2", "faction": "american", "elo": 1850, "wins": 50, "losses": 10, "lastmatchdate": 1795000000}},
                        "inactive_modes": {},
                    },
                    {
                        "profile_id": 20523,
                        "alias": "Goa Trance Mix",
                        "steam_id": "76561198000020523",
                        "country": "br",
                        "is_custom": False,
                        "modes": {"2v2_american": {"mode": "2v2", "faction": "american", "elo": 1600, "wins": 40, "losses": 20, "lastmatchdate": 1795000000}},
                        "inactive_modes": {},
                    },
                ]
                ranked = compute_unified_skill_ranking(mixed_vpn, only_custom=False)
                self.assertEqual([r["profile_id"] for r in ranked], [20523])
        finally:
            EXCLUDED_SUSPECTED_FOREIGN_PIDS.clear()
            EXCLUDED_SUSPECTED_FOREIGN_PIDS.update(snapshot)

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

        # 2. Ranking inclui APENAS veteranos com in_ranking != False (exclui manual_other_br, in_ranking=False e EXCLUDED_VETERANO_PIDS)
        payload = {
            "schema_version": 1,
            "veteranos": [
                {"profile_id": 79327, "steam_id": "76561197963172584", "original_alias": "Chacineiro", "country": "br", "in_ranking": True},
                {"profile_id": 366826, "steam_id": "76561198014985380", "original_alias": "Lem22", "country": "us", "in_ranking": True},
                {"profile_id": 1005730, "steam_id": "76561199791811246", "original_alias": "arc", "pin_alias": True, "in_ranking": True},
                {"profile_id": 999111, "steam_id": "76561198000999111", "original_alias": "ForaDoRanking", "country": "br", "in_ranking": False},
                {
                    "profile_id": 1076221,
                    "steam_id": "76561198815935562",
                    "original_alias": "Lucielle (Colossus)",
                    "country": "br",
                    "in_ranking": True,
                    "pin_alias": True,
                    "known_aliases": ["Lucielle", "Lucifer", "Lucielle (Colossus)"],
                },
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
        self.assertEqual([int(r["profile_id"]) for r in parsed_manual], [33487, 16867, 1076221])
        self.assertEqual(parsed_manual[1]["original_alias"], "Golden Cobra")
        self.assertEqual(parsed_manual[2]["original_alias"], "Lucielle (Colossus)")
        self.assertTrue(parsed_manual[2].get("pin_alias"))
        self.assertEqual(parsed_manual[2].get("smurf_of"), "Colossus")

        # 2b. Seções 'monitorados' + 'outros_br_emigrados' + legado 'manual_other_br' lidas sem duplicatas (185 entradas no fallback)
        from roster import FALLBACK_EXCLUDED_SUSPECTED_FOREIGN_PIDS

        self.assertEqual(len(FALLBACK_MANUAL_OTHER_BR_ALIASES), 185)
        self.assertNotIn(448521, FALLBACK_EXCLUDED_SUSPECTED_FOREIGN_PIDS)
        other_fb = {int(r["profile_id"]): r for r in FALLBACK_MANUAL_OTHER_BR_ALIASES}
        self.assertEqual(other_fb[6880]["original_alias"], "ArguS (VonDun)")
        self.assertEqual(other_fb[33487].get("country"), "us")
        self.assertEqual(other_fb[1304336].get("country"), "us")
        self.assertEqual(other_fb[720147]["original_alias"], "アリス (Smurf: icemaker)")
        self.assertEqual(other_fb[720147].get("smurf_of"), "icemaker")
        self.assertEqual(KNOWN_SMURF_PIDS.get(720147), "icemaker")
        for fpid in (673604, 893043, 1315226, 1115599, 942935, 26172):
            self.assertIn(fpid, other_fb)
        payload_mon = {
            "schema_version": 1,
            "veteranos": payload["veteranos"][:3],
            "monitorados": [
                {"profile_id": 164152, "steam_id": "76561198029749621", "original_alias": "Sgt.Yoda", "country": "br", "priority_watch": True},
                {"profile_id": 212034, "steam_id": "76561198207565088", "original_alias": "XAPA KENTE", "country": "ca"},
            ],
            "outros_br_emigrados": [
                {"profile_id": 33487, "steam_id": "76561197995712470", "original_alias": "Generick", "country": "us"},
            ],
            "manual_other_br": [
                {"profile_id": 33487, "steam_id": "76561197995712470", "original_alias": "Generick", "country": "us"},
            ],
        }
        parsed_mon = parse_shared_manual_other_br_aliases(payload_mon)
        self.assertEqual([int(r["profile_id"]) for r in parsed_mon], [164152, 212034, 33487])
        self.assertFalse(parsed_mon[0].get("smurf_of"))
        self.assertEqual(parsed_mon[1].get("country"), "ca")
        self.assertEqual(parsed_mon[2].get("country"), "us")

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
                        {
                            "profile_id": 1076221,
                            "steam_id": "76561198815935562",
                            "original_alias": "Lucielle (Colossus)",
                            "alias": "Lucielle (Colossus)",
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
                self.assertEqual([int(m["profile_id"]) for m in MANUAL_OTHER_BR_ALIASES], [33487, 16867, 1076221])
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
            self.assertEqual(doc["methodology"], "unified_skill_points_anchor_plus_secondary_bonus_no_smurf_merge")
            self.assertEqual(doc["games_scope"], "lifetime_pvp_since_coh3_launch_all_16_queues_per_account")
            self.assertEqual(doc["total_players"], 3)
            self.assertEqual(doc["active_players"], 2)
            self.assertEqual(doc["inactive_players"], 1)
            self.assertEqual(doc["smurf_accounts_count"], 0)

            with cpath.open(encoding="utf-8") as fp:
                rows = list(csv.DictReader(fp))
            evil_row = next(r for r in rows if r["profile_id"] == "102")
            self.assertTrue(evil_row["display_alias"].startswith("'="))
            self.assertEqual(evil_row["smurf_of"], "")
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

    def test_lifetime_games_and_smurf_accounts_stay_separate_with_owner_metadata(self) -> None:
        roster = [
            {"profile_id": 79327, "steam_id": "76561197963172584", "original_alias": "Chacineiro"},
            {
                "profile_id": 1333418,
                "steam_id": "76561198698969876",
                "original_alias": "Charles Bronson (Chacineiro)",
                "pin_alias": True,
                "smurf_of": "Chacineiro",
            },
        ]
        roster_maps = build_roster_lookup_maps(roster)
        payload = {
            "statGroups": [
                {
                    "id": 1001,
                    "type": 1,
                    "members": [{"profile_id": 79327, "name": "/steam/76561197963172584", "alias": "Chacineiro", "country": "br"}],
                },
                {
                    "id": 1002,
                    "type": 1,
                    "members": [{"profile_id": 1333418, "name": "/steam/76561198698969876", "alias": "Charles Bronson", "country": "br"}],
                },
            ],
            "leaderboardStats": [
                # Conta principal: 1v1 American ativa (1500, 30W/10L) + 4v4 DAK não calibrada com elo=0 (4W/2L) + 3v3 DAK inativa calibrada (1300, 15W/10L)
                {"statgroup_id": 1001, "leaderboard_id": 2130255, "rating": 1500, "rank": 150, "wins": 30, "losses": 10, "streak": 2, "lastmatchdate": 1795000000},
                {"statgroup_id": 1001, "leaderboard_id": 2130358, "rating": 0, "rank": -1, "wins": 4, "losses": 2, "streak": 1, "lastmatchdate": 1700000000},
                {"statgroup_id": 1001, "leaderboard_id": 2130333, "rating": 1300, "rank": -1, "wins": 15, "losses": 10, "streak": -1, "lastmatchdate": 1780000000},
                # Conta smurf (SEPARADA, sem merge): 2v2 German ativa calibrada (1450, 10W/2L) + 3v3 DAK não calibrada <10j (1650, 2W/0L -> fora do ELO) + 4v4 German elo=0 (5W/5L)
                {"statgroup_id": 1002, "leaderboard_id": 2130306, "rating": 1450, "rank": 180, "wins": 10, "losses": 2, "streak": 4, "lastmatchdate": 1795001000},
                {"statgroup_id": 1002, "leaderboard_id": 2130333, "rating": 1650, "rank": -1, "wins": 2, "losses": 0, "streak": 2, "lastmatchdate": 1795002000},
                {"statgroup_id": 1002, "leaderboard_id": 2130360, "rating": 0, "rank": -1, "wins": 5, "losses": 5, "streak": 0, "lastmatchdate": 1690000000},
                # Linha vazia (0W/0L, elo=0) deve continuar ignorada
                {"statgroup_id": 1001, "leaderboard_id": 2130353, "rating": 0, "rank": -1, "wins": 0, "losses": 0, "streak": 0, "lastmatchdate": 0},
            ],
        }
        results_by_pid: dict[int, dict] = {}
        _ingest_personal_stat_payload(payload, results_by_pid, roster_maps=roster_maps)

        main_p = results_by_pid[79327]
        smurf_p = results_by_pid[1333418]
        # Fila com elo=0 e wins+losses>0 entra em inactive_modes (nunca em modes)
        self.assertIn("4v4_dak", main_p["inactive_modes"])
        self.assertNotIn("4v4_dak", main_p["modes"])
        self.assertNotIn("4v4_american", main_p["inactive_modes"])

        # Metadados de Smurf identificados sem mesclar
        self.assertEqual(main_p["smurf_of"], "")
        self.assertFalse(main_p["is_smurf"])
        self.assertEqual(smurf_p["alias"], "Charles Bronson (Smurf: Chacineiro)")
        self.assertEqual(smurf_p["smurf_of"], "Chacineiro")
        self.assertTrue(smurf_p["is_smurf"])

        ranking = compute_unified_skill_ranking([main_p, smurf_p], custom_ids={79327, 1333418}, only_custom=True)
        self.assertEqual(len(ranking), 2)
        by_pid = {e["profile_id"]: e for e in ranking}

        # Conta principal: apenas suas próprias filas (sem somar partidas da Smurf!)
        # 1v1 = 1500 -> P_1v1 = 2000 (âncora), 3v3 = 1300 -> P_3v3 = 1300 (+45 bônus)
        main_entry = by_pid[79327]
        self.assertEqual(main_entry["mode_max_elos"], {"1v1": 1500, "3v3": 1300})
        self.assertEqual(main_entry["anchor_mode"], "1v1")
        self.assertEqual(main_entry["skill_points"], 2045.0)
        self.assertFalse(main_entry["inactive_fallback"])
        self.assertFalse(main_entry["is_smurf"])
        self.assertEqual(main_entry["smurf_of"], "")
        # Wins: 30 + 4 + 15 = 49; Losses: 10 + 2 + 10 = 22 -> Total = 71
        self.assertEqual(main_entry["wins"], 49)
        self.assertEqual(main_entry["losses"], 22)
        self.assertEqual(main_entry["total"], 71)
        self.assertEqual(main_entry["ratio"], 69)

        # Conta Smurf: listada separadamente com referência à conta original
        smurf_entry = by_pid[1333418]
        self.assertEqual(smurf_entry["alias"], "Charles Bronson (Smurf: Chacineiro)")
        self.assertTrue(smurf_entry["is_smurf"])
        self.assertEqual(smurf_entry["smurf_of"], "Chacineiro")
        # Apenas 2v2 calibrada (>=10 jogos) entra no ELO/SP; 3v3 (<10j) e 4v4 (elo=0) somam apenas no total
        self.assertEqual(smurf_entry["mode_max_elos"], {"2v2": 1450})
        self.assertEqual(smurf_entry["anchor_mode"], "2v2")
        self.assertEqual(smurf_entry["wins"], 17)
        self.assertEqual(smurf_entry["losses"], 7)
        self.assertEqual(smurf_entry["total"], 24)

        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "u.json"
            cp = Path(td) / "u.csv"
            export_unified_skill_ranking(ranking, jp, cp, custom_players=roster, only_custom=True)
            doc = json.loads(jp.read_text(encoding="utf-8"))
            self.assertEqual(doc["smurf_accounts_count"], 1)
            with cp.open(encoding="utf-8") as fp:
                csv_by_pid = {r["profile_id"]: r for r in csv.DictReader(fp)}
            self.assertEqual(csv_by_pid["1333418"]["smurf_of"], "Chacineiro")
            self.assertEqual(csv_by_pid["79327"]["smurf_of"], "")

    def test_shared_anticarry_deflates_carried_team_modes_keeps_1v1_intact_and_shields_carriers_and_smurfs(self) -> None:
        from anti_carry import ANTI_CARRY_VERSION, build_shared_anti_carry_context, compute_carry_adjustment
        from battlegroup_champions import _compute_carry_adjustment

        # 1) Confirma que Battlegroups e Ranking Unificado usam exatamente o mesmo motor Anti-Carry
        params = dict(
            eff_elo=1506.0,
            best_1v1_elo=1314,
            team_n=12,
            avg_ally_elo_gap=160.0,
            avg_dmg_vs_ally=0.95,
            avg_kills_vs_ally=0.92,
            carried_match_rate=0.85,
            arr_profile={"tot_2v2": 100, "carry_2v2_share": 0.55, "smurf_carry_games": 17, "carry_wr_lift": 0.09},
        )
        self.assertEqual(
            compute_carry_adjustment(mode="2v2", **params),
            _compute_carry_adjustment(fac_mode="2v2", **params),
        )

        # 2) Elenco realista: peso-pesado carregador, smurf de carregador, solo-paridade com 1 outlier 3v3,
        #    carregador 2v2 puro (arc) e jogador carregado (Vikitor)
        players = [
            {
                "profile_id": 79327,
                "alias": "Chacineiro",
                "original_alias": "Chacineiro",
                "steam_id": "76561197963172584",
                "is_custom": True,
                "modes": {
                    "1v1_american": {"mode": "1v1", "faction": "american", "elo": 1566, "wins": 50, "losses": 20, "lastmatchdate": 1795000000},
                    "2v2_american": {"mode": "2v2", "faction": "american", "elo": 1662, "wins": 80, "losses": 20, "lastmatchdate": 1795000000},
                },
                "inactive_modes": {},
            },
            {
                "profile_id": 1333418,
                "alias": "Charles Bronson (Chacineiro)",
                "original_alias": "Charles Bronson (Chacineiro)",
                "steam_id": "76561198698969876",
                "smurf_of": "Chacineiro",
                "is_smurf": True,
                "is_custom": True,
                "modes": {
                    "2v2_american": {"mode": "2v2", "faction": "american", "elo": 1390, "wins": 25, "losses": 5, "lastmatchdate": 1795000000},
                },
                "inactive_modes": {},
            },
            {
                "profile_id": 21206,
                "alias": "hjax",
                "original_alias": "hjax",
                "steam_id": "76561198061434984",
                "is_custom": True,
                "modes": {
                    "1v1_german": {"mode": "1v1", "faction": "german", "elo": 1573, "wins": 60, "losses": 25, "lastmatchdate": 1795000000},
                    "2v2_german": {"mode": "2v2", "faction": "german", "elo": 1647, "wins": 40, "losses": 15, "lastmatchdate": 1795000000},
                },
                "inactive_modes": {},
            },
            {
                "profile_id": 1005730,
                "alias": "arc",
                "original_alias": "arc",
                "steam_id": "76561199791811246",
                "is_custom": True,
                "modes": {
                    "2v2_german": {"mode": "2v2", "faction": "german", "elo": 1466, "wins": 50, "losses": 20, "lastmatchdate": 1795000000},
                    "3v3_german": {"mode": "3v3", "faction": "german", "elo": 1560, "wins": 40, "losses": 12, "lastmatchdate": 1795000000},
                },
                "inactive_modes": {},
            },
            {
                "profile_id": 1423426,
                "alias": "geladeira velha (arc)",
                "original_alias": "geladeira velha (arc)",
                "steam_id": "76561199482388323",
                "smurf_of": "arc",
                "is_smurf": True,
                "is_custom": True,
                "modes": {
                    "2v2_german": {"mode": "2v2", "faction": "german", "elo": 1308, "wins": 15, "losses": 5, "lastmatchdate": 1795000000},
                },
                "inactive_modes": {},
            },
            {
                "profile_id": 1114404,
                "alias": "Vikitor",
                "original_alias": "Vikitor",
                "steam_id": "76561198024118058",
                "is_custom": True,
                "modes": {
                    "1v1_american": {"mode": "1v1", "faction": "american", "elo": 1314, "wins": 30, "losses": 30, "lastmatchdate": 1795000000},
                    "2v2_american": {"mode": "2v2", "faction": "american", "elo": 1506, "wins": 120, "losses": 70, "lastmatchdate": 1795000000},
                    "3v3_american": {"mode": "3v3", "faction": "american", "elo": 1389, "wins": 40, "losses": 30, "lastmatchdate": 1795000000},
                },
                "inactive_modes": {},
            },
        ]
        players_by_pid = {int(p["profile_id"]): p for p in players}

        combat_rows = (
            # Chacineiro: carregador real (-120 ELO gap, 1.20x dmg)
            [{"profile_id": 79327, "mode": "2v2", "my_elo": 1660.0, "ally_elo_gap": -120.0, "dmg_vs_ally": 1.20, "kills_vs_ally": 1.22, "carried_match": 0}] * 10
            # arc: carregador real (-220 ELO gap, 1.15x dmg)
            + [{"profile_id": 1005730, "mode": "2v2", "my_elo": 1466.0, "ally_elo_gap": -220.0, "dmg_vs_ally": 1.15, "kills_vs_ally": 1.15, "carried_match": 0}] * 10
            # geladeira velha (smurf de arc): partida com ELO de conta baixo aparentando gap positivo -> deve herdar perfil real do arc!
            + [{"profile_id": 1423426, "mode": "2v2", "my_elo": 1280.0, "ally_elo_gap": 200.0, "dmg_vs_ally": 1.16, "kills_vs_ally": 1.10, "carried_match": 1}] * 10
            # hjax: 9 jogos normais (-10 gap) + 1 outlier aleatório de 3v3 (+500 gap) -> protegido por mediana+paridade 1v1!
            + [{"profile_id": 21206, "mode": "4v4", "my_elo": 1400.0, "ally_elo_gap": -10.0, "dmg_vs_ally": 0.98, "kills_vs_ally": 1.05, "carried_match": 0}] * 9
            + [{"profile_id": 21206, "mode": "3v3", "my_elo": 1300.0, "ally_elo_gap": 500.0, "dmg_vs_ally": 0.99, "kills_vs_ally": 0.98, "carried_match": 1}]
            # Vikitor: 10 jogos em equipe fortemente carregados (+180 gap)
            + [{"profile_id": 1114404, "mode": "2v2", "my_elo": 1320.0, "ally_elo_gap": 180.0, "dmg_vs_ally": 0.95, "kills_vs_ally": 0.90, "carried_match": 1}] * 10
        )
        # Histórico 2v2: Vikitor joga 20x com Chacineiro (65% WR) + 10x com Smurf do Chacineiro (80% WR) + 20x com colega igual (50% WR)
        arranged_pairs = (
            [(1114404, 79327, 1)] * 13
            + [(1114404, 79327, 0)] * 7
            + [(1114404, 1333418, 1)] * 8
            + [(1114404, 1333418, 0)] * 2
            + [(1114404, 999999, 1)] * 10
            + [(1114404, 999999, 0)] * 10
        )

        ctx = build_shared_anti_carry_context(players_by_pid, combat_rows, arranged_pairs)
        raw_ranking = compute_unified_skill_ranking(players, custom_ids=set(players_by_pid), only_custom=True)
        adj_ranking = compute_unified_skill_ranking(
            players,
            custom_ids=set(players_by_pid),
            only_custom=True,
            carry_context=ctx,
        )
        by_pid = {e["profile_id"]: e for e in adj_ranking}

        # Carregadores reais, paridade 1v1 (hjax) e smurf de carregador (geladeira velha) permanecem 100% intactos
        for shielded_pid in (79327, 1333418, 21206, 1005730, 1423426):
            self.assertEqual(by_pid[shielded_pid]["carry_sp_delta"], 0.0, f"Não deveria penalizar pid={shielded_pid}")
            self.assertEqual(by_pid[shielded_pid]["carry_mult"], 1.0)

        # Vikitor sofre deflação em 2v2/3v3, mantém 1v1 intacto (1314), âncora vira 1v1 e fica ABAIXO do arc!
        vik = by_pid[1114404]
        arc_e = by_pid[1005730]
        self.assertEqual(vik["mode_effective_elos"]["1v1"], 1314.0)
        self.assertLess(vik["mode_effective_elos"]["2v2"], 1430.0)
        self.assertEqual(vik["anchor_mode"], "1v1")
        self.assertLess(vik["carry_sp_delta"], -100.0)
        self.assertLess(vik["skill_points"], arc_e["skill_points"])
        self.assertGreater(vik["br_rank"], arc_e["br_rank"])
        self.assertNotEqual(compute_rankings_digest(raw_ranking), compute_rankings_digest(adj_ranking))

        # Exportação JSON/CSV inclui metadados anti-carry sem quebrar contratos anteriores
        with tempfile.TemporaryDirectory() as td:
            jp = Path(td) / "u_ac.json"
            cp = Path(td) / "u_ac.csv"
            export_unified_skill_ranking(adj_ranking, jp, cp, custom_players=players, only_custom=True)
            doc = json.loads(jp.read_text(encoding="utf-8"))
            self.assertEqual(doc["anti_carry_version"], ANTI_CARRY_VERSION)
            self.assertEqual(doc["anti_carry_adjusted_players"], 1)
            with cp.open(encoding="utf-8") as fp:
                rows_csv = {r["profile_id"]: r for r in csv.DictReader(fp)}
            self.assertLess(float(rows_csv["1114404"]["carry_sp_delta"]), -100.0)
            self.assertEqual(float(rows_csv["21206"]["carry_sp_delta"]), 0.0)


if __name__ == "__main__":
    unittest.main()
