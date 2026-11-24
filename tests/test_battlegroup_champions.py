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
    def _make_match(self, matchtype_id: int, gt: int = 1200) -> dict:
        return {
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
                    "counters": json.dumps({"gt": gt, "totalcmds": 1200, "dmgdone": 8000, "ekills": 60, "edeaths": 120}),
                },
            ],
        }

    def test_extract_automatch_rejects_custom_skirmish_and_short_games(self) -> None:
        self.assertEqual(extract_automatch_combat_rows(self._make_match(0), {1001}), [])
        self.assertEqual(extract_automatch_combat_rows(self._make_match(6), {1001}), [])
        self.assertEqual(extract_automatch_combat_rows(self._make_match(101), {1001}), [])
        self.assertEqual(extract_automatch_combat_rows(self._make_match(21, gt=120), {1001}), [])

        rows = extract_automatch_combat_rows(self._make_match(21, gt=1200), {1001, 2002})
        self.assertEqual(len(rows), 2)
        r1 = next(r for r in rows if r["profile_id"] == 1001)
        r2 = next(r for r in rows if r["profile_id"] == 2002)
        self.assertEqual(r1["faction"], "american")
        self.assertEqual(r2["faction"], "afrika_corps")
        self.assertEqual(r1["mode"], "2v2")
        self.assertAlmostEqual(r1["apm"], 90.0, places=1)
        self.assertAlmostEqual(r1["dmg_ratio"], 1.75, places=2)
        self.assertEqual(r1["stomp_win"], 1)

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
                "global_best_adj_elo": 1430.0,  # 1700 4v4 - 270 = 1430
                "fac_best_adj_elo": {"american": 1430.0},
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
            "overall": {"n": 25, "apm_median": 90.0, "dmg_pm": 340.0, "dmg_ratio": 1.25, "kd": 1.35, "net_wipes": 1.5, "pcap_ratio": 1.3, "stomp_win_rate": 0.5},
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
                self.assertEqual(len(db.load_bg_combat_matches(0)), 1)
                self.assertEqual(summarize_combat_rows(db.load_bg_combat_matches(0))[1001]["overall"]["n"], 1)

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
            self.assertEqual(len(compute_battlegroup_champions_digest(report)), 24)


if __name__ == "__main__":
    unittest.main()
