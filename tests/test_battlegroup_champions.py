"""Testes unitários e de segurança para Campeões BR por Battlegroup (CoH3 Automatch)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from avatar_cache import RankingCacheDB
from battlegroup_champions import (
    BATTLEGROUP_CATALOG,
    COHDB_PLAYER_PATH_RE,
    RACE_ID_TO_FACTION,
    CohDBCanonicalClient,
    _compute_activity_multiplier,
    _compute_carry_adjustment,
    _compute_combat_efficiency_score,
    _display_player_name,
    apply_weekly_belt_tracking,
    build_smurf_owner_maps,
    compute_2v2_carry_profiles,
    compute_battlegroup_champions,
    compute_battlegroup_champions_digest,
    export_battlegroup_champions_files,
    extract_automatch_combat_rows,
    parse_cohdb_battlegroups_html,
    parse_cohdb_performance_html,
    summarize_combat_rows,
)
from discord_ranking import (
    build_battlegroup_champions_panel_payload,
    render_battlegroup_champions_attachments,
)
from ranking_renderer import BG_ICONS_DIR, SteamAvatarProvider, get_battlegroup_icon


class TestBattlegroupCatalogAndAssets(unittest.TestCase):
    def test_catalog_has_all_28_battlegroups_and_valid_png_icons(self) -> None:
        self.assertEqual(len(BATTLEGROUP_CATALOG), 28)
        by_fac = {}
        for item in BATTLEGROUP_CATALOG:
            by_fac.setdefault(item["faction"], []).append(item["bg_name"])
            icon_path = BG_ICONS_DIR / f"{item['icon_slug']}.png"
            self.assertTrue(icon_path.is_file(), f"Ícone ausente: {icon_path}")
            im = get_battlegroup_icon(item["icon_slug"], size=34)
            self.assertEqual(im.size, (34, 34))
        for fac in ("american", "british", "german", "afrika_corps"):
            self.assertEqual(len(by_fac.get(fac, [])), 7, f"Esperados 7 BGs para {fac}")

    def test_race_id_mapping_is_exact(self) -> None:
        self.assertEqual(RACE_ID_TO_FACTION[129494], "american")
        self.assertEqual(RACE_ID_TO_FACTION[137123], "german")
        self.assertEqual(RACE_ID_TO_FACTION[198437], "afrika_corps")
        self.assertEqual(RACE_ID_TO_FACTION[203852], "british")
        self.assertEqual(RACE_ID_TO_FACTION[197345], "british")


class TestCohdbParsersAndSecurity(unittest.TestCase):
    def test_parse_cohdb_battlegroups_html(self) -> None:
        sample = """
        <img alt="Special Operations Battlegroup" src="/assets/races/american/battlegroups/spec_ops_us_icon_mipped-abc.webp" />
        <span class="text-base font-bold text-white">Special Operations Battlegroup</span>
        <div>Record</div><div>68–28</div>
        <div>Win%</div><div>70.8%</div>
        <div>Last 30</div><div>20–10</div>
        <div>Selections</div><div>96</div>
        """
        parsed = parse_cohdb_battlegroups_html(sample)
        key = "american|Special Operations Battlegroup"
        self.assertIn(key, parsed)
        self.assertEqual(parsed[key]["wins"], 68)
        self.assertEqual(parsed[key]["losses"], 28)
        self.assertAlmostEqual(parsed[key]["wr"], 70.8, places=1)
        self.assertEqual(parsed[key]["l30_w"], 20)
        self.assertEqual(parsed[key]["l30_l"], 10)
        self.assertEqual(parsed[key]["selections"], 96)

    def test_parse_cohdb_performance_ignores_last_50_and_100_links(self) -> None:
        sample = """
        <a href="/players/10/performance?faction=americans&amp;mode=twos&amp;range=100"><span>USF</span><span>1,580</span></a>
        <a href="/players/10/performance?faction=germans&amp;mode=twos&amp;range=100"><span>Last 100</span></a>
        <a href="/players/10/performance?faction=british_africa&amp;mode=ones&amp;range=50"><span>Last 50</span></a>
        <a href="/players/10/performance?faction=afrika_corps&amp;mode=fours"><span>DAK</span><span>1,410</span></a>
        """
        parsed = parse_cohdb_performance_html(sample)
        self.assertEqual(parsed, {"american:2v2": 1580, "afrika_corps:4v4": 1410})

    def test_cohdb_client_blocks_invalid_paths_and_sections(self) -> None:
        client = CohDBCanonicalClient()
        with self.assertRaises(ValueError):
            client._fetch_canonical_page(0, "battlegroups")
        with self.assertRaises(ValueError):
            client._fetch_canonical_page(26910, "matches")
        self.assertIsNone(COHDB_PLAYER_PATH_RE.fullmatch("/players/26910/battlegroups?mode=ones"))
        self.assertIsNone(COHDB_PLAYER_PATH_RE.fullmatch("/players/../admin/battlegroups"))
        self.assertIsNotNone(COHDB_PLAYER_PATH_RE.fullmatch("/players/26910/battlegroups"))
        self.assertIsNotNone(COHDB_PLAYER_PATH_RE.fullmatch("/players/26910/performance"))


class TestAutomatchExtractionAndScoring(unittest.TestCase):
    def _make_match(
        self,
        matchtype_id: int,
        gt: int = 1200,
        p1_elo: int = 1480,
        p2_elo: int = 1600,
        relic_member_format: bool = False,
    ) -> dict:
        m: dict = {
            "id": 999001,
            "matchtype_id": matchtype_id,
            "completiontime": 1795000000,
            "matchhistoryreportresults": [
                {
                    "profile_id": 1001,
                    "teamid": 0,
                    "race_id": 129494,  # american
                    "resulttype": 1,
                    "counters": json.dumps(
                        {
                            "gt": gt,
                            "totalcmds": 1800,
                            "dmgdone": 14000,
                            "ekills": 120,
                            "edeaths": 60,
                            "sqkill": 5,
                            "sqlost": 1,
                            "vkill": 3,
                            "vlost": 1,
                            "pcap": 24,
                            "plost": 10,
                        }
                    ),
                },
                {
                    "profile_id": 2002,
                    "teamid": 1,
                    "race_id": 198437,  # afrika_corps
                    "resulttype": 0,
                    "counters": json.dumps(
                        {"gt": gt, "totalcmds": 1200, "dmgdone": 8000, "ekills": 60, "edeaths": 120, "pcap": 12, "plost": 20}
                    ),
                },
            ],
        }
        if relic_member_format:
            m["matchhistorymember"] = [
                {"profile_id": 1001, "oldrating": p1_elo},
                {"profile_id": 2002, "oldrating": p2_elo},
            ]
        else:
            m["matchhistoryreportresults"][0]["matchhistorymember"] = {"oldrating": p1_elo}
            m["matchhistoryreportresults"][1]["matchhistorymember"] = {"oldrating": p2_elo}
        return m

    def test_extract_automatch_rejects_custom_skirmish_and_extracts_upset_and_map_control(self) -> None:
        self.assertEqual(extract_automatch_combat_rows(self._make_match(0), {1001}), [])
        self.assertEqual(extract_automatch_combat_rows(self._make_match(6), {1001}), [])
        self.assertEqual(extract_automatch_combat_rows(self._make_match(101), {1001}), [])
        self.assertEqual(extract_automatch_combat_rows(self._make_match(21, gt=120), {1001}), [])

        for relic_fmt in (False, True):
            rows = extract_automatch_combat_rows(
                self._make_match(21, gt=1200, p1_elo=1480, p2_elo=1600, relic_member_format=relic_fmt),
                {1001, 2002},
            )
            self.assertEqual(len(rows), 2)
            r1 = next(r for r in rows if r["profile_id"] == 1001)
            r2 = next(r for r in rows if r["profile_id"] == 2002)
            self.assertEqual(r1["faction"], "american")
            self.assertEqual(r2["faction"], "afrika_corps")
            self.assertEqual(r1["mode"], "2v2")
            self.assertAlmostEqual(r1["apm"], 90.0, places=1)
            self.assertAlmostEqual(r1["dmg_ratio"], 1.75, places=2)
            self.assertEqual(r1["stomp_win"], 1)
            # Controle de mapa relativo ao inimigo: (24+1)/(12+1) = 1.923
            self.assertGreater(r1["pcap_vs_enemy"], 1.8)
            # Vitória contra oponente +120 ELO acima
            self.assertEqual(r1["upset_opp"], 1)
            self.assertEqual(r1["upset_win"], 1)
            self.assertAlmostEqual(r1["upset_elo_gap"], 120.0, places=1)
            # Oponente favorito não conta como upset
            self.assertEqual(r2["upset_opp"], 0)
            self.assertEqual(r2["upset_win"], 0)

        # Anti-carry em 2v2: jogador 1300 carregado por aliado 1850 contra dois 1500 não conta como upset
        carried_match = {
            "id": 999002,
            "matchtype_id": 21,
            "completiontime": 1795000100,
            "matchhistoryreportresults": [
                {
                    "profile_id": 1001,
                    "teamid": 0,
                    "race_id": 129494,
                    "resulttype": 1,
                    "matchhistorymember": {"oldrating": 1300},
                    "counters": {"gt": 1200, "totalcmds": 1200, "dmgdone": 8000, "ekills": 40, "edeaths": 40, "pcap": 12, "plost": 10},
                },
                {
                    "profile_id": 1002,
                    "teamid": 0,
                    "race_id": 203852,
                    "resulttype": 1,
                    "matchhistorymember": {"oldrating": 1850},
                    "counters": {"gt": 1200, "totalcmds": 1800, "dmgdone": 14000, "ekills": 90, "edeaths": 30, "pcap": 20, "plost": 5},
                },
                {
                    "profile_id": 2001,
                    "teamid": 1,
                    "race_id": 137123,
                    "resulttype": 0,
                    "matchhistorymember": {"oldrating": 1500},
                    "counters": {"gt": 1200, "totalcmds": 1400, "dmgdone": 10000, "ekills": 50, "edeaths": 60, "pcap": 10, "plost": 16},
                },
                {
                    "profile_id": 2002,
                    "teamid": 1,
                    "race_id": 198437,
                    "resulttype": 0,
                    "matchhistorymember": {"oldrating": 1500},
                    "counters": {"gt": 1200, "totalcmds": 1400, "dmgdone": 10000, "ekills": 50, "edeaths": 60, "pcap": 10, "plost": 16},
                },
            ],
        }
        carried_rows = extract_automatch_combat_rows(carried_match, {1001})
        self.assertEqual(len(carried_rows), 1)
        self.assertEqual(carried_rows[0]["upset_opp"], 0)
        self.assertEqual(carried_rows[0]["upset_win"], 0)

    def test_winrate_beats_inflated_match_volume_and_rewards_map_control_and_upsets(self) -> None:
        def _mk_player(pid: int, alias: str, elo: int = 1580) -> dict:
            return {
                "profile_id": pid,
                "steam_id": f"7656119800000000{pid}",
                "roster_alias": alias,
                "live_alias": alias,
                "country": "br",
                "category": "Veterano",
                "is_veterano": True,
                "smurf_of": "",
                "global_best_adj_elo": float(elo - 20),
                "fac_best_adj_elo": {"american": float(elo - 20)},
                "fac_best_raw_elo": {"american": elo},
                "fac_best_mode": {"american": "2v2"},
                "best_12_by_fac": {"american": [elo, "2v2"]},
                "queues": {},
            }

        qualified = {
            1: _mk_player(1, "HighWRSpecialist"),
            2: _mk_player(2, "VolumeGrinder"),
            3: _mk_player(3, "MapUpsetMaster"),
            4: _mk_player(4, "LowMapNoUpsets"),
        }
        bg_key = "american|Armored Battlegroup"
        bg_map = {
            # Aproveitamento (78.6% em 28 jogos) DEVE vencer quantidade inflada (55.6% em 540 jogos)
            1: {bg_key: {"wins": 22, "losses": 6, "wr": 78.6, "l30_w": 5, "l30_l": 1, "selections": 28}},
            2: {bg_key: {"wins": 300, "losses": 240, "wr": 55.6, "l30_w": 40, "l30_l": 32, "selections": 540}},
            # Mesmo BG record para comparar diretamente Controle de Mapa + Vitórias vs ELO Maior
            3: {bg_key: {"wins": 25, "losses": 12, "wr": 67.6, "l30_w": 5, "l30_l": 2, "selections": 37}},
            4: {bg_key: {"wins": 25, "losses": 12, "wr": 67.6, "l30_w": 5, "l30_l": 2, "selections": 37}},
        }
        same_combat = {
            "overall": {
                "n": 30,
                "apm_median": 90.0,
                "dmg_pm": 320.0,
                "dmg_ratio": 1.20,
                "kd": 1.30,
                "net_wipes": 1.0,
                "pcap_ratio": 1.50,
                "pcap_vs_enemy": 1.05,
                "upset_opps": 10,
                "upset_wins": 5,
                "upset_avg_gap": 75.0,
                "stomp_win_rate": 0.45,
            },
            "by_faction": {},
        }
        combat_map = {
            1: same_combat,
            2: same_combat,
            3: {
                "overall": {
                    "n": 30,
                    "apm_median": 90.0,
                    "dmg_pm": 320.0,
                    "dmg_ratio": 1.20,
                    "kd": 1.30,
                    "net_wipes": 1.0,
                    "pcap_ratio": 1.95,
                    "pcap_vs_enemy": 1.30,
                    "upset_opps": 12,
                    "upset_wins": 9,
                    "upset_avg_gap": 125.0,
                    "stomp_win_rate": 0.45,
                },
                "by_faction": {},
            },
            4: {
                "overall": {
                    "n": 30,
                    "apm_median": 90.0,
                    "dmg_pm": 320.0,
                    "dmg_ratio": 1.20,
                    "kd": 1.30,
                    "net_wipes": 1.0,
                    "pcap_ratio": 1.05,
                    "pcap_vs_enemy": 0.85,
                    "upset_opps": 12,
                    "upset_wins": 2,
                    "upset_avg_gap": 30.0,
                    "stomp_win_rate": 0.45,
                },
                "by_faction": {},
            },
        }
        report = compute_battlegroup_champions(qualified, bg_map, combat_map, perf_map={})
        armored = next(c for c in report["champions"] if c["bg_name"] == "Armored Battlegroup")
        scores = {p["alias"]: p["score"] for p in armored["top3"]}
        # HighWRSpecialist vence VolumeGrinder com ampla folga
        self.assertIn("HighWRSpecialist", scores)
        # Verifica par a par com pools dedicados
        rep_wr = compute_battlegroup_champions({1: qualified[1], 2: qualified[2]}, bg_map, combat_map)
        arm_wr = next(c for c in rep_wr["champions"] if c["bg_name"] == "Armored Battlegroup")
        self.assertEqual(arm_wr["champion"]["alias"], "HighWRSpecialist")
        self.assertGreater(arm_wr["champion"]["score"] - arm_wr["runner_up"]["score"], 70.0)

        rep_mu = compute_battlegroup_champions({3: qualified[3], 4: qualified[4]}, bg_map, combat_map)
        arm_mu = next(c for c in rep_mu["champions"] if c["bg_name"] == "Armored Battlegroup")
        self.assertEqual(arm_mu["champion"]["alias"], "MapUpsetMaster")
        self.assertGreater(arm_mu["champion"]["score"] - arm_mu["runner_up"]["score"], 90.0)

    def test_scoring_blocks_low_elo_spammer_and_smurfs_and_prefers_2v2_over_4v4(self) -> None:
        qualified = {
            1: {
                "profile_id": 1,
                "steam_id": "76561198000000001",
                "roster_alias": "Elite2v2",
                "live_alias": "Elite2v2",
                "country": "br",
                "category": "Veterano",
                "is_veterano": True,
                "smurf_of": "",
                "global_best_adj_elo": 1620.0,
                "fac_best_adj_elo": {"american": 1620.0},
                "fac_best_raw_elo": {"american": 1640},
                "fac_best_mode": {"american": "2v2"},
                "best_12_by_fac": {"american": [1640, "2v2"]},
                "queues": {},
            },
            2: {
                "profile_id": 2,
                "steam_id": "76561198000000002",
                "roster_alias": "LowEloOneTrick",
                "live_alias": "LowEloOneTrick",
                "country": "br",
                "category": "Outros BR",
                "is_veterano": False,
                "smurf_of": "",
                "global_best_adj_elo": 1120.0,
                "fac_best_adj_elo": {"american": 1120.0},
                "fac_best_raw_elo": {"american": 1140},
                "fac_best_mode": {"american": "2v2"},
                "best_12_by_fac": {"american": [1140, "2v2"]},
                "queues": {},
            },
            3: {
                "profile_id": 3,
                "steam_id": "76561198000000003",
                "roster_alias": "SmurfGod",
                "live_alias": "SmurfGod",
                "country": "br",
                "category": "Veterano",
                "is_veterano": True,
                "smurf_of": "Elite2v2",
                "global_best_adj_elo": 1800.0,
                "fac_best_adj_elo": {"american": 1800.0},
                "fac_best_raw_elo": {"american": 1800},
                "fac_best_mode": {"american": "1v1"},
                "best_12_by_fac": {"american": [1800, "1v1"]},
                "queues": {},
            },
            4: {
                "profile_id": 4,
                "steam_id": "76561198000000004",
                "roster_alias": "Stack4v4",
                "live_alias": "Stack4v4",
                "country": "br",
                "category": "Outros BR",
                "is_veterano": False,
                "smurf_of": "",
                "global_best_adj_elo": 1415.0,  # 1700 4v4 - 285 = 1415
                "fac_best_adj_elo": {"american": 1415.0},
                "fac_best_raw_elo": {"american": 1700},
                "fac_best_mode": {"american": "4v4"},
                "best_12_by_fac": {},
                "queues": {},
            },
        }
        bg_key = "american|Airborne Battlegroup"
        bg_map = {
            1: {bg_key: {"wins": 45, "losses": 20, "wr": 69.2, "l30_w": 15, "l30_l": 5, "selections": 65}},
            2: {bg_key: {"wins": 400, "losses": 200, "wr": 66.7, "l30_w": 20, "l30_l": 10, "selections": 600}},
            3: {bg_key: {"wins": 80, "losses": 10, "wr": 88.9, "l30_w": 20, "l30_l": 2, "selections": 90}},
            4: {bg_key: {"wins": 45, "losses": 20, "wr": 69.2, "l30_w": 15, "l30_l": 5, "selections": 65}},
        }
        base_combat = {
            "overall": {
                "n": 25,
                "apm_median": 90.0,
                "dmg_pm": 340.0,
                "dmg_ratio": 1.25,
                "kd": 1.35,
                "net_wipes": 1.5,
                "pcap_ratio": 1.55,
                "pcap_vs_enemy": 1.12,
                "upset_opps": 8,
                "upset_wins": 4,
                "upset_avg_gap": 85.0,
                "stomp_win_rate": 0.5,
            },
            "by_faction": {},
        }
        combat_map = {pid: base_combat for pid in qualified}

        report = compute_battlegroup_champions(qualified, bg_map, combat_map, perf_map={})
        airborne = next(c for c in report["champions"] if c["bg_name"] == "Airborne Battlegroup")
        self.assertEqual(airborne["champion"]["alias"], "Elite2v2")
        self.assertEqual(airborne["runner_up"]["alias"], "Stack4v4")
        # Smurf (pid=3) excluído e LowEloOneTrick (pid=2) abaixo de Elite2v2 e Stack4v4
        top_pids = [x["profile_id"] for x in airborne["top3"]]
        self.assertNotIn(3, top_pids)
        self.assertEqual(top_pids[0], 1)
        self.assertEqual(top_pids[1], 4)

        # Renderização PNG + Discord payload + CSV anti-injection + SQLite cache
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            with RankingCacheDB(tmp_p / "test.db") as db:
                db.save_bg_player_cache(1, battlegroups=bg_map[1], performance={"american:2v2": 1620})
                loaded = db.get_all_bg_player_cache()
                self.assertIn(1, loaded)
                self.assertEqual(loaded[1]["performance"]["american:2v2"], 1620)

                rows = extract_automatch_combat_rows(self._make_match(21, gt=1200), {1001})
                self.assertEqual(db.save_bg_combat_matches(rows), 1)
                loaded_matches = db.load_bg_combat_matches(0)
                self.assertEqual(len(loaded_matches), 1)
                self.assertEqual(loaded_matches[0]["upset_win"], 1)
                self.assertGreater(loaded_matches[0]["pcap_vs_enemy"], 1.8)
                agg = summarize_combat_rows(loaded_matches)[1001]["overall"]
                self.assertEqual(agg["n"], 1)
                self.assertEqual(agg["upset_wins"], 1)
                self.assertEqual(agg["upset_opps"], 1)

                provider = SteamAvatarProvider(db=db, allow_network=False)
                atts = render_battlegroup_champions_attachments(report, avatar_provider=provider)
                self.assertEqual(len(atts), 2)
                self.assertEqual([a[0] for a in atts], ["battlegroups_br_p1.png", "battlegroups_br_p2.png"])
                for _fname, png_bytes, ctype in atts:
                    self.assertEqual(ctype, "image/png")
                    with Image.open(io.BytesIO(png_bytes)) as im:
                        self.assertEqual(im.width, 1040)
                        self.assertGreater(im.height, 400)

                payload = build_battlegroup_champions_panel_payload(report, [a[0] for a in atts])
                self.assertEqual(payload["allowed_mentions"], {"parse": []})
                self.assertEqual(len(payload["embeds"]), 2)
                self.assertEqual(payload["embeds"][0]["image"]["url"], "attachment://battlegroups_br_p1.png")

            # Export JSON/CSV e digest
            report["champions"][0]["champion"]["alias"] = "=cmd|'/C calc'!A0"
            export_battlegroup_champions_files(report, tmp_p / "bg.json", tmp_p / "bg.csv")
            csv_txt = (tmp_p / "bg.csv").read_text(encoding="utf-8")
            self.assertIn("'=cmd|", csv_txt)
            self.assertIn("upset_win_rate", csv_txt)
            self.assertIn("controle_mapa_vs_inimigo", csv_txt)
            self.assertNotIn("tema_tatico", csv_txt)
            self.assertIn("l30_win_rate_bg", csv_txt)
            self.assertIn("cinturao_status", csv_txt)
            self.assertEqual(len(compute_battlegroup_champions_digest(report)), 24)

    def test_v3_neutral_combat_l30_form_activity_and_belts(self) -> None:
        # 1) Nenhum BG do catálogo possui rótulo de tema artificial
        for item in BATTLEGROUP_CATALOG:
            self.assertNotIn("theme", item)

        # 2) Combate neutro e equilibrado (sem peso por tema de BG): valoriza dano/min, razão dano, K/D, wipes e vKD
        strong_combat = _compute_combat_efficiency_score(
            has_combat=True, dmg_pm=460.0, dmg_ratio=1.38, kd=1.45, net_wipes=1.8, veh_kd=1.80,
        )
        weak_combat = _compute_combat_efficiency_score(
            has_combat=True, dmg_pm=250.0, dmg_ratio=0.85, kd=0.85, net_wipes=-0.5, veh_kd=0.75,
        )
        self.assertGreater(strong_combat, weak_combat + 0.55)
        self.assertEqual(
            _compute_combat_efficiency_score(
                has_combat=False, dmg_pm=0.0, dmg_ratio=1.0, kd=1.0, net_wipes=0.0, veh_kd=1.0,
            ),
            0.35,
        )

        # 3) Anti-pubstomp em 4v4 low-ELO (927 vs 976 -> enemy_adj_elo=691 < 1200 -> ZERO upset e ZERO stomp)
        low_4v4 = self._make_match(23, gt=1200, p1_elo=927, p2_elo=976)
        low_rows = extract_automatch_combat_rows(low_4v4, {1001})
        self.assertEqual(len(low_rows), 1)
        self.assertEqual(low_rows[0]["upset_opp"], 0)
        self.assertEqual(low_rows[0]["upset_win"], 0)
        self.assertEqual(low_rows[0]["stomp_win"], 0)

        # 4) Decaimento suave por inatividade na facção
        now_ts = 1_800_000_000.0
        m_active, _ = _compute_activity_multiplier(int(now_ts - 10 * 86400), int(now_ts), now_ts=now_ts)
        m_retired, _ = _compute_activity_multiplier(int(now_ts - 150 * 86400), int(now_ts), now_ts=now_ts)
        self.assertEqual(m_active, 1.0)
        self.assertLessEqual(m_retired, 0.86)

        # 5) Forma recente L30: mesmo WR histórico (60%), mas L30 80% vence L30 33%
        def _mk(pid: int, alias: str) -> dict:
            return {
                "profile_id": pid,
                "steam_id": f"7656119800000010{pid}",
                "roster_alias": alias,
                "live_alias": alias,
                "country": "br",
                "category": "Veterano",
                "is_veterano": True,
                "smurf_of": "",
                "global_best_adj_elo": 1560.0,
                "global_last_match_ts": int(now_ts - 2 * 86400),
                "fac_best_adj_elo": {"afrika_corps": 1560.0},
                "fac_best_raw_elo": {"afrika_corps": 1580},
                "fac_best_mode": {"afrika_corps": "2v2"},
                "fac_last_match_ts": {"afrika_corps": int(now_ts - 2 * 86400)},
                "best_12_by_fac": {"afrika_corps": [1580, "2v2"]},
                "queues": {},
            }

        bg_key = "afrika_corps|Battlefield Espionage Battlegroup"
        q = {1: _mk(1, "HotFormL30"), 2: _mk(2, "ColdFormL30")}
        bg_m = {
            1: {bg_key: {"wins": 60, "losses": 40, "wr": 60.0, "l30_w": 24, "l30_l": 6, "selections": 100}},
            2: {bg_key: {"wins": 60, "losses": 40, "wr": 60.0, "l30_w": 10, "l30_l": 20, "selections": 100}},
        }
        cm_base = {
            "overall": {
                "n": 25, "apm_median": 88.0, "dmg_pm": 320.0, "dmg_ratio": 1.15, "kd": 1.2,
                "net_wipes": 1.0, "veh_kd": 1.1, "vvet_pm": 500.0, "svet_pm": 300.0,
                "abil_pm": 3.5, "cabil_pm": 0.15, "struct_dmg": 30.0, "bprod_ratio": 3.0,
                "pcap_ratio": 1.5, "pcap_vs_enemy": 1.1, "precap_pm": 0.8, "net_cap_pm": 0.2,
                "avg_enemy_adj_elo": 1480.0, "upset_opps": 8, "upset_wins": 4,
                "upset_opps_w": 7.5, "upset_wins_w": 3.8, "upset_avg_gap": 70.0,
                "upset_avg_quality": 70.0, "stomp_win_rate": 0.4,
            },
            "by_faction": {},
        }
        rep = compute_battlegroup_champions(q, bg_m, {1: cm_base, 2: cm_base}, now_ts=now_ts)
        esp = next(c for c in rep["champions"] if c["bg_name"] == "Battlefield Espionage Battlegroup")
        self.assertEqual(esp["champion"]["alias"], "HotFormL30")
        self.assertGreater(esp["champion"]["score"] - esp["runner_up"]["score"], 75.0)

        # 6) Defesa de Cinturão semanal (Semana 1 initial -> Semana 2 defended -> re-run idempotente -> Semana 3 troca)
        with tempfile.TemporaryDirectory() as tmp:
            with RankingCacheDB(Path(tmp) / "belts.db") as db:
                rows_w1 = apply_weekly_belt_tracking(rep, db.get_all_bg_belt_state(), slot_id="2026-11-21_sat12")
                self.assertEqual(esp["belt_status"], "initial")
                self.assertEqual(esp["streak_weeks"], 1)
                db.save_bg_belt_state(rows_w1)

                rows_w2 = apply_weekly_belt_tracking(rep, db.get_all_bg_belt_state(), slot_id="2026-11-28_sat12")
                self.assertEqual(esp["belt_status"], "defended")
                self.assertEqual(esp["streak_weeks"], 2)
                db.save_bg_belt_state(rows_w2)

                # Re-execução na mesma semana 2026-11-28 não incrementa streak para 3
                apply_weekly_belt_tracking(rep, db.get_all_bg_belt_state(), slot_id="2026-11-28_sat12")
                self.assertEqual(esp["streak_weeks"], 2)

                # Semana 3: novo campeão toma o cinturão e entra em belt_changes + embed Discord
                esp["champion"]["profile_id"] = 2
                esp["champion"]["alias"] = "ColdFormL30"
                apply_weekly_belt_tracking(rep, db.get_all_bg_belt_state(), slot_id="2026-12-05_sat12")
                self.assertEqual(esp["belt_status"], "new")
                self.assertEqual(esp["streak_weeks"], 1)
                self.assertEqual(esp["prev_champion_alias"], "HotFormL30")
                self.assertEqual(len(rep["belt_changes"]), 1)
                payload = build_battlegroup_champions_panel_payload(rep, ["battlegroups_br_p1.png", "battlegroups_br_p2.png"])
                self.assertIn("Trocas de cinturão", payload["embeds"][0]["description"])

    def test_candidate_pool_blocks_foreign_alias_history_false_positives(self) -> None:
        from battlegroup_champions import build_br_bg_candidate_pool

        class _FakeRelic:
            def _get_json(self, _url: str) -> dict:
                return {
                    "statGroups": [
                        {"id": 1, "members": [{"profile_id": 999001, "alias": "ForeignPro", "country": "ca", "name": "/steam/76561198000000001"}]},
                        {"id": 2, "members": [{"profile_id": 999002, "alias": "ForeignUnflagged", "country": "kr", "name": "/steam/76561198000000002"}]},
                        {"id": 3, "members": [{"profile_id": 999003, "alias": "TrueBrazilian", "country": "br", "name": "/steam/76561198000000003"}]},
                    ],
                    "leaderboardStats": [
                        {"statgroup_id": 1, "leaderboard_id": 2130255, "wins": 50, "losses": 10, "rating": 2000, "rank": 5},
                        {"statgroup_id": 2, "leaderboard_id": 2130255, "wins": 50, "losses": 10, "rating": 1900, "rank": 15},
                        {"statgroup_id": 3, "leaderboard_id": 2130255, "wins": 50, "losses": 20, "rating": 1600, "rank": 120},
                    ],
                }

        comm = [
            {"profile_id": 999001, "alias": "ForeignPro", "br_source": "alias_history", "flag_country": "ca", "max_elo": 2000},
            {"profile_id": 999002, "alias": "ForeignUnflagged", "br_source": "open_data_lb", "flag_country": None, "max_elo": 1900},
            {"profile_id": 999003, "alias": "TrueBrazilian", "br_source": "open_data_lb", "flag_country": "br", "max_elo": 1600},
        ]
        qual = build_br_bg_candidate_pool(
            custom_players=[],
            relic_client=_FakeRelic(),  # type: ignore[arg-type]
            community_players=comm,
            include_top_leaderboard_scan=False,
        )
        self.assertNotIn(999001, qual)
        self.assertNotIn(999002, qual)
        self.assertIn(999003, qual)

    def test_roster_alias_consistency_honors_pin_alias_and_known_aliases(self) -> None:
        cases = [
            # Apelido fixo do roster via known_aliases / pin_alias (sem vazar nick temporário da Steam)
            ({"profile_id": 64941, "roster_alias": "Alekel", "live_alias": "Minhoca de ferro!", "pin_alias": True, "known_aliases": ["Minhoca de ferro!", "Alekel"]}, "Alekel"),
            ({"profile_id": 64941, "roster_alias": "Alekel", "live_alias": "Minhoca de ferro!", "pin_alias": False, "known_aliases": ["Minhoca de ferro!"]}, "Alekel"),
            ({"profile_id": 302269, "roster_alias": "Golden Cobra", "live_alias": "COHBRAMACHINE", "pin_alias": True, "known_aliases": ["COHBRAMACHINE"]}, "Golden Cobra"),
            ({"profile_id": 144500, "roster_alias": "Winter Animal", "live_alias": "Superior Culture", "pin_alias": True, "known_aliases": ["Superior Culture"]}, "Winter Animal"),
            ({"profile_id": 476645, "roster_alias": "Gabe", "live_alias": "É o Gabe", "pin_alias": True, "known_aliases": ["É o Gabe"]}, "Gabe"),
            ({"profile_id": 798482, "roster_alias": "Alface aka BinhoBiceps", "live_alias": "BinhoBiceps", "pin_alias": False, "known_aliases": []}, "Alface aka BinhoBiceps"),
            ({"profile_id": 318389, "roster_alias": "Nargoth", "live_alias": "Nargoth de Bebbanburg", "pin_alias": True, "known_aliases": ["Nargoth de Bebbanburg"]}, "Nargoth"),
            # Novo nick ainda não mapeado (sem pin_alias) preserva contexto "<Novo> (<Original>)"
            ({"profile_id": 55555, "roster_alias": "VeteranoX", "live_alias": "NovoNick", "pin_alias": False, "known_aliases": []}, "NovoNick (VeteranoX)"),
        ]
        for player, expected in cases:
            self.assertEqual(_display_player_name(player), expected)

    def test_v4_anticarry_smurf_aware_and_carrier_shield(self) -> None:
        # 1) Smurf disfarçado no time (oldrating 1200, mas dono tem 1650) dispara carried_match e bloqueia upset/stomp
        smurf_carry_match = {
            "id": 888001,
            "matchtype_id": 21,  # 2v2
            "completiontime": 1796000000,
            "matchhistoryreportresults": [
                {
                    "profile_id": 1114404,  # jogador carregado
                    "teamid": 0,
                    "race_id": 129494,
                    "resulttype": 1,
                    "matchhistorymember": {"oldrating": 1300},
                    "counters": {"gt": 1200, "totalcmds": 1100, "dmgdone": 9000, "ekills": 45, "edeaths": 40, "pcap": 14, "plost": 8},
                },
                {
                    "profile_id": 1333418,  # smurf de peso-pesado com rating baixo na conta smurf
                    "teamid": 0,
                    "race_id": 203852,
                    "resulttype": 1,
                    "matchhistorymember": {"oldrating": 1200},
                    "counters": {"gt": 1200, "totalcmds": 2000, "dmgdone": 14500, "ekills": 95, "edeaths": 25, "pcap": 20, "plost": 4},
                },
                {
                    "profile_id": 20001,
                    "teamid": 1,
                    "race_id": 137123,
                    "resulttype": 0,
                    "matchhistorymember": {"oldrating": 1420},
                    "counters": {"gt": 1200, "totalcmds": 1400, "dmgdone": 8000, "ekills": 35, "edeaths": 70, "pcap": 8, "plost": 18},
                },
                {
                    "profile_id": 20002,
                    "teamid": 1,
                    "race_id": 198437,
                    "resulttype": 0,
                    "matchhistorymember": {"oldrating": 1420},
                    "counters": {"gt": 1200, "totalcmds": 1400, "dmgdone": 8000, "ekills": 35, "edeaths": 70, "pcap": 8, "plost": 18},
                },
            ],
        }
        rows = extract_automatch_combat_rows(smurf_carry_match, {1114404}, smurf_elo_map={1333418: 1650.0})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["carried_match"], 1)
        self.assertAlmostEqual(r["ally_elo_gap"], 350.0, places=1)
        self.assertLess(r["dmg_vs_ally"], 0.70)
        self.assertEqual(r["upset_opp"], 0)
        self.assertEqual(r["upset_win"], 0)
        self.assertEqual(r["stomp_win"], 0)

        # 2) Inimigo de nível baixo (1300 no 2v2 -> adj 1280 < 1350) NÃO gera upset mesmo com +100 de diferença
        low_tier_match = self._make_match(21, gt=1200, p1_elo=1190, p2_elo=1300)
        low_rows = extract_automatch_combat_rows(low_tier_match, {1001})
        self.assertEqual(len(low_rows), 1)
        self.assertEqual(low_rows[0]["upset_opp"], 0)
        self.assertEqual(low_rows[0]["upset_win"], 0)

        # 3) Histórico 2v2 arranjado + ajuste anti-carry + escudo do carregador real e 1v1 puro
        qualified = {
            79327: {
                "profile_id": 79327,
                "steam_id": "76561198000000079",
                "roster_alias": "Chacineiro",
                "live_alias": "Chacineiro",
                "country": "br",
                "category": "Veterano",
                "is_veterano": True,
                "smurf_of": "",
                "global_best_adj_elo": 1640.0,
                "fac_best_adj_elo": {"american": 1610.0},
                "fac_best_raw_elo": {"american": 1630},
                "fac_best_mode": {"american": "2v2"},
                "best_12_by_fac": {"american": [1630, "2v2"]},
                "queues": {"american:1v1": {"rating": 1590, "games": 40}, "american:2v2": {"rating": 1630, "games": 120}},
            },
            1333418: {
                "profile_id": 1333418,
                "steam_id": "76561198000001333",
                "roster_alias": "Charles Bronson (Smurf: Chacineiro)",
                "live_alias": "Charles Bronson",
                "country": "br",
                "category": "Veterano",
                "is_veterano": True,
                "smurf_of": "Chacineiro",
                "global_best_adj_elo": 1360.0,
                "fac_best_adj_elo": {"american": 1360.0},
                "fac_best_raw_elo": {"american": 1380},
                "fac_best_mode": {"american": "2v2"},
                "best_12_by_fac": {"american": [1380, "2v2"]},
                "queues": {"american:2v2": {"rating": 1380, "games": 20}},
            },
            1114404: {
                "profile_id": 1114404,
                "steam_id": "76561198000001114",
                "roster_alias": "CarriedPlayer",
                "live_alias": "CarriedPlayer",
                "country": "br",
                "category": "Veterano",
                "is_veterano": True,
                "smurf_of": "",
                "global_best_adj_elo": 1490.0,
                "fac_best_adj_elo": {"american": 1490.0},
                "fac_best_raw_elo": {"american": 1510},
                "fac_best_mode": {"american": "2v2"},
                "best_12_by_fac": {"american": [1510, "2v2"]},
                "queues": {"american:1v1": {"rating": 1240, "games": 30}, "american:2v2": {"rating": 1510, "games": 200}},
            },
            500001: {
                "profile_id": 500001,
                "steam_id": "76561198000000500",
                "roster_alias": "EqualMate",
                "live_alias": "EqualMate",
                "country": "br",
                "category": "Veterano",
                "is_veterano": True,
                "smurf_of": "",
                "global_best_adj_elo": 1420.0,
                "fac_best_adj_elo": {"american": 1420.0},
                "fac_best_raw_elo": {"american": 1440},
                "fac_best_mode": {"american": "2v2"},
                "best_12_by_fac": {"american": [1440, "2v2"]},
                "queues": {"american:1v1": {"rating": 1280, "games": 25}},
            },
        }
        smurf_owner_map, smurf_elo_map = build_smurf_owner_maps(qualified)
        self.assertEqual(smurf_owner_map.get(1333418), 79327)
        self.assertGreaterEqual(smurf_elo_map.get(1333418, 0.0), 1640.0)

        # 20 jogos com Chacineiro (65% WR) + 10 com Smurf do Chacineiro (80% WR) + 20 com EqualMate (50% WR)
        arranged_pairs = (
            [(1114404, 79327, 1)] * 13
            + [(1114404, 79327, 0)] * 7
            + [(1114404, 1333418, 1)] * 8
            + [(1114404, 1333418, 0)] * 2
            + [(1114404, 500001, 1)] * 10
            + [(1114404, 500001, 0)] * 10
        )
        carry_profiles = compute_2v2_carry_profiles(arranged_pairs, qualified, smurf_owner_map=smurf_owner_map)
        self.assertIn(1114404, carry_profiles)
        self.assertAlmostEqual(carry_profiles[1114404]["carry_2v2_share"], 0.60, places=2)
        self.assertEqual(carry_profiles[1114404]["smurf_carry_games"], 10)
        self.assertGreater(carry_profiles[1114404]["carry_wr_lift"], 0.15)
        self.assertEqual(carry_profiles[79327]["carry_2v2_share"], 0.0)

        c_mult_carried, c_defl_carried, c_risk_carried = _compute_carry_adjustment(
            fac_mode="2v2",
            eff_elo=1490.0,
            best_1v1_elo=1240,
            team_n=25,
            avg_ally_elo_gap=145.0,
            avg_dmg_vs_ally=0.88,
            avg_kills_vs_ally=0.84,
            carried_match_rate=0.68,
            arr_profile=carry_profiles[1114404],
        )
        self.assertLess(c_mult_carried, 0.85)
        self.assertGreater(c_defl_carried, 80.0)
        self.assertGreater(c_risk_carried, 0.65)

        # Escudo do carregador real e do jogador 1v1 puro
        c_mult_carrier, c_defl_carrier, _ = _compute_carry_adjustment(
            fac_mode="2v2",
            eff_elo=1610.0,
            best_1v1_elo=1590,
            team_n=25,
            avg_ally_elo_gap=-190.0,
            avg_dmg_vs_ally=1.26,
            avg_kills_vs_ally=1.30,
            carried_match_rate=0.04,
            arr_profile=carry_profiles[79327],
        )
        self.assertEqual(c_mult_carrier, 1.0)
        self.assertEqual(c_defl_carrier, 0.0)

        c_mult_1v1, c_defl_1v1, _ = _compute_carry_adjustment(
            fac_mode="1v1",
            eff_elo=1500.0,
            best_1v1_elo=1500,
            team_n=20,
            avg_ally_elo_gap=150.0,
            avg_dmg_vs_ally=0.80,
            avg_kills_vs_ally=0.80,
            carried_match_rate=0.80,
            arr_profile=carry_profiles[1114404],
        )
        self.assertEqual(c_mult_1v1, 1.0)
        self.assertEqual(c_defl_1v1, 0.0)

        # Escudo de paridade solo 1v1 (ex.: 1560 1v1 vs 1593 2v2, sem dependência 2v2) e gap negativo de aliados
        c_mult_parity, c_defl_parity, _ = _compute_carry_adjustment(
            fac_mode="2v2",
            eff_elo=1593.0,
            best_1v1_elo=1560,
            team_n=24,
            avg_ally_elo_gap=-29.7,
            avg_dmg_vs_ally=0.97,
            avg_kills_vs_ally=1.00,
            carried_match_rate=0.45,
            arr_profile=None,
        )
        self.assertEqual(c_mult_parity, 1.0)
        self.assertEqual(c_defl_parity, 0.0)

        # 4) End-to-end: Chacineiro vence jogador carregado mesmo com WR inflado no BG
        bg_key = "american|Heavy Weapons"
        bg_map = {
            79327: {bg_key: {"wins": 30, "losses": 18, "wr": 62.5, "l30_w": 18, "l30_l": 12, "selections": 48}},
            1114404: {bg_key: {"wins": 42, "losses": 18, "wr": 70.0, "l30_w": 21, "l30_l": 9, "selections": 60}},
        }
        combat_map = {
            79327: {
                "overall": {
                    "n": 30, "team_n": 25, "apm_median": 95.0, "dmg_pm": 360.0, "dmg_ratio": 1.25, "kd": 1.35,
                    "net_wipes": 1.4, "veh_kd": 1.30, "pcap_ratio": 1.60, "pcap_vs_enemy": 1.15,
                    "avg_enemy_adj_elo": 1540.0, "avg_ally_elo_gap": -185.0, "avg_dmg_vs_ally": 1.26,
                    "avg_kills_vs_ally": 1.28, "carried_match_rate": 0.04,
                    "upset_opps": 8, "upset_wins": 4, "upset_opps_w": 8.0, "upset_wins_w": 4.0,
                    "upset_avg_gap": 85.0, "upset_avg_quality": 90.0, "stomp_win_rate": 0.48,
                },
                "by_faction": {},
            },
            1114404: {
                "overall": {
                    "n": 30, "team_n": 25, "apm_median": 90.0, "dmg_pm": 320.0, "dmg_ratio": 1.10, "kd": 1.15,
                    "net_wipes": 0.6, "veh_kd": 1.05, "pcap_ratio": 1.50, "pcap_vs_enemy": 1.05,
                    "avg_enemy_adj_elo": 1460.0, "avg_ally_elo_gap": 145.0, "avg_dmg_vs_ally": 0.88,
                    "avg_kills_vs_ally": 0.84, "carried_match_rate": 0.68,
                    "upset_opps": 2, "upset_wins": 0, "upset_opps_w": 2.0, "upset_wins_w": 0.0,
                    "upset_avg_gap": 0.0, "upset_avg_quality": 0.0, "stomp_win_rate": 0.15,
                },
                "by_faction": {},
            },
        }
        rep = compute_battlegroup_champions(qualified, bg_map, combat_map, carry_profiles=carry_profiles)
        hw = next(c for c in rep["champions"] if c["bg_name"] == "Heavy Weapons")
        self.assertEqual(hw["champion"]["alias"], "Chacineiro")
        self.assertEqual(hw["champion"]["carry_mult"], 1.0)
        self.assertEqual(hw["runner_up"]["alias"], "CarriedPlayer")
        self.assertLess(hw["runner_up"]["carry_mult"], 0.85)
        self.assertGreater(hw["champion"]["score"] - hw["runner_up"]["score"], 120.0)

        # 5) Persistência SQLite dos 4 novos campos anti-carry + load_arranged_2v2_pairs
        with tempfile.TemporaryDirectory() as tmp:
            with RankingCacheDB(Path(tmp) / "v4.db") as db:
                self.assertEqual(db.save_bg_combat_matches(rows), 1)
                loaded = db.load_bg_combat_matches(0)
                self.assertEqual(len(loaded), 1)
                self.assertEqual(loaded[0]["carried_match"], 1)
                self.assertAlmostEqual(loaded[0]["ally_elo_gap"], 350.0, places=1)
                self.assertLess(loaded[0]["dmg_vs_ally"], 0.70)
                db.save_arranged_2v2_matches([
                    {
                        "match_id": 5001,
                        "side": "allies",
                        "pair_key": "79327:1114404",
                        "p1_id": 79327,
                        "p1_alias": "Chacineiro",
                        "p1_country": "br",
                        "p2_id": 1114404,
                        "p2_alias": "CarriedPlayer",
                        "p2_country": "br",
                        "won": 1,
                        "enemy_elo": 1520.0,
                        "completion_ts": 1796000000,
                    }
                ])
                pairs = db.load_arranged_2v2_pairs()
                self.assertEqual(pairs, [(79327, 1114404, 1)])


if __name__ == "__main__":
    unittest.main()
