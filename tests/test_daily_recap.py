"""Testes unitários do Boletim Diário dos Veteranos (08:03 BRT), Jogo do Dia, cohdb e renderização PNG."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from daily_recap import (
    TOP_GLOBAL_MAX_RANK,
    TOP_GLOBAL_MIN_ELO_BY_MODE,
    UNIFIED_TOP_N,
    CohdbReplayClient,
    annotate_unified_top5_impacts,
    annotate_unified_top15_impacts,
    build_daily_recap,
    build_unified_ranking_context,
    export_daily_recap_json,
    fetch_global_top_players,
    format_map_name,
    get_brt_day_window,
    get_top_global_band,
    get_top_global_min_elo,
    is_top_global_entry,
    load_unified_ranking_context,
    merge_and_filter_day_matches,
    score_match_of_the_day,
)
from daily_recap_renderer import (
    render_daily_recap_table_png,
    render_match_of_the_day_png,
)
from discord_ranking import (
    DiscordRankingClient,
    build_daily_recap_payload,
    render_daily_recap_attachments,
)
from ranking_renderer import SteamAvatarProvider


def _make_report(
    pid: int,
    team_id: int,
    race_id: int,
    won: bool,
    alias: str,
    country: str,
    old_elo: int,
    new_elo: int,
    dmg: int,
    gt: int,
    vp0: int = 0,
    vp1: int = 0,
    vkill: int = 2,
    sqkill: int = 4,
    ekills: int = 0,
    edeaths: int = 0,
) -> dict:
    counters = {"dmgdone": dmg, "vkill": vkill, "sqkill": sqkill, "gt": gt}
    if ekills or edeaths:
        counters["ekills"] = ekills
        counters["edeaths"] = edeaths
    if vp0 or vp1:
        counters["vp0"] = vp0
        counters["vp1"] = vp1
    return {
        "profile_id": pid,
        "teamid": team_id,
        "race_id": race_id,
        "resulttype": 1 if won else 0,
        "counters": json.dumps(counters),
        "profile": {"name": f"/steam/76561198000{pid:06d}", "alias": alias, "country": country},
        "matchhistorymember": {"outcome": 1 if won else 0, "oldrating": old_elo, "newrating": new_elo},
    }


class TestDailyRecapLogicAndScoring(unittest.TestCase):
    def test_brt_day_window_exact_boundaries(self) -> None:
        # 2026-11-19 00:00:00 BRT = 2026-11-19 03:00:00 UTC
        # 2026-11-19 23:59:59 BRT = 2026-11-20 02:59:59 UTC
        day_str, start_ts, end_ts = get_brt_day_window("2026-11-19")
        self.assertEqual(day_str, "2026-11-19")
        self.assertEqual(end_ts - start_ts, 86399)

        # Se agora é 2026-11-20 06:00 BRT (09:00 UTC), o padrão é ontem (2026-11-19)
        now_6am_brt = end_ts + 1 + 6 * 3600
        auto_day, auto_start, auto_end = get_brt_day_window(None, now_ts=now_6am_brt)
        self.assertEqual((auto_day, auto_start, auto_end), (day_str, start_ts, end_ts))

    def test_map_name_formatting(self) -> None:
        self.assertEqual(format_map_name("pachino_2p"), "Pachino Stalemate")
        self.assertEqual(format_map_name("rails_and_sand_4p"), "Campbell's Convoy")
        self.assertEqual(format_map_name("custom_future_map_4p_mkii"), "Custom Future Map")

    def test_merge_filters_non_pvp_console_out_of_window_and_merges_secondary_account(self) -> None:
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        late_night_brt = end_ts - 600  # 23:50 BRT (já é dia seguinte em UTC, mas DEVE entrar!)
        outside_ts = start_ts - 60

        raw_matches = [
            # Partida válida no limite da noite BRT usando conta secundária gabe44arg (557890 -> 559962 É o Gabe)
            {
                "id": 1001,
                "matchtype_id": 1,
                "mapname": "semois_2p",
                "startgametime": late_night_brt - 1800,
                "completiontime": late_night_brt,
                "platform": "steam",
                "matchhistoryreportresults": [
                    {"profile_id": 557890, "teamid": 0, "race_id": 129494, "resulttype": 1, "counters": '{"dmgdone": 40000, "gt": 1800}'},
                    {"profile_id": 999001, "teamid": 1, "race_id": 137123, "resulttype": 0, "counters": '{"dmgdone": 35000, "gt": 1800}'},
                ],
                "matchhistorymember": [
                    {"profile_id": 557890, "outcome": 1, "oldrating": 1250, "newrating": 1268},
                    {"profile_id": 999001, "outcome": 0, "oldrating": 1280, "newrating": 1262},
                ],
                "_profiles_by_pid": {
                    557890: {"profile_id": 557890, "name": "/steam/76561199538846112", "alias": "gabe44arg", "country": "br"},
                    999001: {"profile_id": 999001, "name": "/steam/76561198000999001", "alias": "Opponent1", "country": "de"},
                },
            },
            # Duplicata mais pobre da mesma partida 1001 (sem counters) -> descartada em favor da mais rica
            {
                "id": 1001,
                "matchtype_id": 1,
                "mapname": "semois_2p",
                "startgametime": late_night_brt - 1800,
                "completiontime": late_night_brt,
                "platform": "steam",
                "matchhistoryreportresults": [
                    {"profile_id": 557890, "teamid": 0, "race_id": 129494, "resulttype": 1, "profile": {"alias": "gabe44arg", "country": "br"}},
                    {"profile_id": 999001, "teamid": 1, "race_id": 137123, "resulttype": 0, "profile": {"alias": "Opponent1", "country": "de"}},
                ],
            },
            # Partida contra IA (matchtype_id=5) -> ignorada
            {
                "id": 1002,
                "matchtype_id": 5,
                "mapname": "torrente_4p_mkiii",
                "completiontime": late_night_brt,
                "matchhistoryreportresults": [],
            },
            # Partida fora da janela (dia anterior) -> ignorada
            {
                "id": 1003,
                "matchtype_id": 1,
                "mapname": "semois_2p",
                "completiontime": outside_ts,
                "matchhistoryreportresults": [
                    {"profile_id": 79327, "teamid": 0, "race_id": 129494, "resulttype": 1, "profile": {"alias": "Chacineiro", "country": "br"}},
                    {"profile_id": 999002, "teamid": 1, "race_id": 137123, "resulttype": 0, "profile": {"alias": "Opp2", "country": "us"}},
                ],
            },
            # Partida de console -> ignorada
            {
                "id": 1004,
                "matchtype_id": 1,
                "platform": "xbox",
                "completiontime": late_night_brt,
                "matchhistoryreportresults": [
                    {"profile_id": 79327, "teamid": 0, "race_id": 129494, "resulttype": 1, "profile": {"alias": "Chacineiro", "country": "br"}},
                    {"profile_id": 999002, "teamid": 1, "race_id": 137123, "resulttype": 0, "profile": {"alias": "Opp2", "country": "us"}},
                ],
            },
        ]

        merged = merge_and_filter_day_matches(raw_matches, start_ts, end_ts)
        self.assertEqual(len(merged), 1)
        m = merged[0]
        self.assertEqual(m["match_id"], 1001)
        self.assertEqual(m["allies_team"][0]["canonical_pid"], 557890)
        self.assertEqual(m["allies_team"][0]["alias"], "gabe44arg (Smurf: Gabe)")
        self.assertEqual(m["allies_team"][0]["dmgdone"], 40000)
        self.assertEqual(m["allies_team"][0]["elo_delta"], 18)

    def test_arc_smurf_stays_separate_pinned_and_golden_cobra_stays_non_veterano(self) -> None:
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 36000
        raw_matches = [
            {
                "id": 1999,
                "matchtype_id": 2,
                "mapname": "semois_2p",
                "startgametime": ts - 1200,
                "completiontime": ts,
                "platform": "steam",
                "matchhistoryreportresults": [
                    {"profile_id": 1423426, "teamid": 0, "race_id": 129494, "resulttype": 1, "profile": {"name": "/steam/76561199482388323", "alias": "geladeira velha", "country": "br"}},
                    {"profile_id": 16867, "teamid": 0, "race_id": 203852, "resulttype": 1, "profile": {"name": "/steam/76561198874381986", "alias": "COHBRAMACHINE", "country": "br"}},
                    {"profile_id": 999001, "teamid": 1, "race_id": 137123, "resulttype": 0, "profile": {"name": "/steam/76561198000999001", "alias": "OppA", "country": "de"}},
                    {"profile_id": 999002, "teamid": 1, "race_id": 198437, "resulttype": 0, "profile": {"name": "/steam/76561198000999002", "alias": "OppB", "country": "de"}},
                ],
                "matchhistorymember": [
                    {"profile_id": 1423426, "outcome": 1, "oldrating": 1300, "newrating": 1315},
                    {"profile_id": 16867, "outcome": 1, "oldrating": 1580, "newrating": 1590},
                    {"profile_id": 999001, "outcome": 0, "oldrating": 1450, "newrating": 1435},
                    {"profile_id": 999002, "outcome": 0, "oldrating": 1450, "newrating": 1440},
                ],
            }
        ]
        merged = merge_and_filter_day_matches(raw_matches, start_ts, end_ts)
        self.assertEqual(len(merged), 1)
        by_pid = {int(p["profile_id"]): p for p in merged[0]["allies_team"]}
        self.assertEqual(by_pid[1423426]["canonical_pid"], 1423426)
        self.assertEqual(by_pid[1423426]["alias"], "geladeira velha (Smurf: arc)")
        self.assertTrue(by_pid[1423426]["is_veterano"])
        self.assertEqual(by_pid[16867]["alias"], "Golden Cobra")
        self.assertFalse(by_pid[16867]["is_veterano"])

    def test_match_of_the_day_rewards_top_global_tight_vp_and_long_duration(self) -> None:
        _, start_ts, _ = get_brt_day_window("2026-11-19")
        ts = start_ts + 36000

        # Partida 1: jogo curto/comum de 7 minutos (stomp)
        short_match = {
            "id": 2001,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts - 420,
            "completiontime": ts,
            "matchhistoryreportresults": [
                {
                    "profile_id": 79327, "teamid": 0, "race_id": 129494, "resulttype": 1,
                    "counters": '{"dmgdone": 12000, "vkill": 0, "gt": 420, "vp0": 480, "vp1": 0}',
                    "profile": {"name": "/steam/76561197963172584", "alias": "Chacineiro", "country": "br"},
                    "matchhistorymember": {"outcome": 1, "oldrating": 1560, "newrating": 1568},
                },
                {
                    "profile_id": 900100, "teamid": 1, "race_id": 137123, "resulttype": 0,
                    "counters": '{"dmgdone": 4000, "vkill": 0, "gt": 420, "vp0": 480, "vp1": 0}',
                    "profile": {"name": "/steam/76561198000900100", "alias": "RandomOpp", "country": "fr"},
                    "matchhistorymember": {"outcome": 0, "oldrating": 1350, "newrating": 1342},
                },
            ],
        }
        # Partida 2: batalha de 49 min contra Top #9 global decidida em 14 x 0 VP + upset
        epic_match = {
            "id": 2002,
            "matchtype_id": 1,
            "mapname": "pachino_2p",
            "startgametime": ts + 3000 - 2940,
            "completiontime": ts + 3000,
            "matchhistoryreportresults": [
                {
                    "profile_id": 639157, "teamid": 0, "race_id": 197345, "resulttype": 1,
                    "counters": '{"dmgdone": 94000, "ekills": 90, "edeaths": 55, "vkill": 8, "sqkill": 15, "gt": 2940, "vp0": 14, "vp1": 0}',
                    "profile": {"name": "/steam/76561197979883434", "alias": "Itaperuna", "country": "br"},
                    "matchhistorymember": {"outcome": 1, "oldrating": 1614, "newrating": 1637},
                },
                {
                    "profile_id": 900200, "teamid": 1, "race_id": 198437, "resulttype": 0,
                    "counters": '{"dmgdone": 91000, "ekills": 55, "edeaths": 90, "vkill": 7, "sqkill": 13, "gt": 2940, "vp0": 14, "vp1": 0}',
                    "profile": {"name": "/steam/76561198000900200", "alias": "WorldTop9", "country": "de"},
                    "matchhistorymember": {"outcome": 0, "oldrating": 1880, "newrating": 1857},
                },
            ],
        }

        matches = merge_and_filter_day_matches([short_match, epic_match], start_ts, start_ts + 86399)
        top_global = {900200: {"profile_id": 900200, "rank": 9, "mode": "1v1", "faction": "dak", "elo": 1880, "alias": "WorldTop9"}}
        recap = build_daily_recap(matches, "2026-11-19", top_global_by_pid=top_global)

        motd = recap["match_of_the_day"]
        self.assertIsNotNone(motd)
        self.assertEqual(motd["match_id"], 2002)
        self.assertEqual(motd["selection_tier"], 5)
        self.assertGreaterEqual(motd["epicness_score"], 78)
        self.assertLessEqual(motd["epicness_score"], 100)
        bd = motd["score_breakdown"]
        self.assertLessEqual(bd["top_global"], 35.0)
        self.assertLessEqual(bd["upset_lobby"], 20.0)
        self.assertLessEqual(bd["drama"], 20.0)
        self.assertLessEqual(bd["unified_top15"], 15.0)
        self.assertLessEqual(bd["mode_perf_br"], 10.0)
        self.assertTrue(any("TOP #9" in b for b in motd["badges"]))
        self.assertTrue(any("14 x 0 VP" in b for b in motd["badges"]))

        short_scored = score_match_of_the_day(matches[0], top_global_by_pid=top_global)
        self.assertEqual(short_scored["selection_tier"], 2)
        self.assertLess(short_scored["epicness_score"], 15)
        self.assertLess(short_scored["score_breakdown"]["drama"], 0.0)

        # Verifica exportação atômica JSON
        with tempfile.TemporaryDirectory() as td:
            out_json = Path(td) / "boletim_diario_br.json"
            export_daily_recap_json(recap, out_json)
            loaded = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(loaded["summary"]["total_unique_matches"], 2)
            self.assertEqual(loaded["awards"]["mvp"]["alias"], "Itaperuna")

    def test_top_global_top50_per_mode_elo_floors_and_banded_scoring(self) -> None:
        self.assertEqual(TOP_GLOBAL_MAX_RANK, 50)
        self.assertEqual(UNIFIED_TOP_N, 15)

        # Pisos competitivos por modo (1v1: 1700, 2v2: 1750, 3v3: 1700, 4v4: 1850)
        self.assertEqual(get_top_global_min_elo("1v1"), TOP_GLOBAL_MIN_ELO_BY_MODE["1v1"])
        self.assertEqual(get_top_global_min_elo("2v2"), TOP_GLOBAL_MIN_ELO_BY_MODE["2v2"])
        self.assertEqual(get_top_global_min_elo("3v3"), TOP_GLOBAL_MIN_ELO_BY_MODE["3v3"])
        self.assertEqual(get_top_global_min_elo("4v4"), TOP_GLOBAL_MIN_ELO_BY_MODE["4v4"])

        self.assertTrue(is_top_global_entry({"rank": 1, "mode": "1v1", "elo": 2150}))
        self.assertTrue(is_top_global_entry({"rank": 50, "mode": "1v1", "elo": 1700}))
        self.assertFalse(is_top_global_entry({"rank": 50, "mode": "1v1", "elo": 1699}))
        self.assertTrue(is_top_global_entry({"rank": 25, "mode": "2v2", "elo": 1750}))
        self.assertFalse(is_top_global_entry({"rank": 25, "mode": "2v2", "elo": 1749}))
        self.assertTrue(is_top_global_entry({"rank": 40, "mode": "3v3", "elo": 1700}))
        self.assertFalse(is_top_global_entry({"rank": 40, "mode": "3v3", "elo": 1699}))
        self.assertTrue(is_top_global_entry({"rank": 50, "mode": "4v4", "elo": 1850}))
        self.assertFalse(is_top_global_entry({"rank": 15, "mode": "4v4", "elo": 1849}))
        self.assertFalse(is_top_global_entry({"rank": 51, "mode": "1v1", "elo": 2200}))
        self.assertFalse(is_top_global_entry(None))

        self.assertEqual(get_top_global_band(1), "top10")
        self.assertEqual(get_top_global_band(10), "top10")
        self.assertEqual(get_top_global_band(11), "top25")
        self.assertEqual(get_top_global_band(25), "top25")
        self.assertEqual(get_top_global_band(26), "top50")
        self.assertEqual(get_top_global_band(50), "top50")
        self.assertEqual(get_top_global_band(51), "")

        # fetch_global_top_players deve consultar Top 50 e aplicar o piso de ELO do modo de cada fila
        from relic_ranking import STEAM_LEADERBOARDS

        lb_1v1_usf = STEAM_LEADERBOARDS["1v1"]["american"]
        lb_4v4_usf = STEAM_LEADERBOARDS["4v4"]["american"]

        class _FakeRelic:
            def __init__(self) -> None:
                self.seen_urls: list[str] = []

            def _get_json(self, url: str) -> dict:
                self.seen_urls.append(url)
                if f"leaderboard_id={lb_1v1_usf}&" in url:
                    return {
                        "statGroups": [
                            {"id": 1, "members": [{"profile_id": 111, "alias": "Top5_1v1"}]},
                            {"id": 2, "members": [{"profile_id": 222, "alias": "Top48_1v1_Ok"}]},
                            {"id": 3, "members": [{"profile_id": 333, "alias": "Top30_1v1_Low"}]},
                            {"id": 4, "members": [{"profile_id": 444, "alias": "Rank51_Out"}]},
                        ],
                        "leaderboardStats": [
                            {"statgroup_id": 1, "rank": 5, "rating": 2050},
                            {"statgroup_id": 2, "rank": 48, "rating": 1715},
                            {"statgroup_id": 3, "rank": 30, "rating": 1690},
                            {"statgroup_id": 4, "rank": 51, "rating": 2100},
                        ],
                    }
                if f"leaderboard_id={lb_4v4_usf}&" in url:
                    return {
                        "statGroups": [
                            {"id": 10, "members": [{"profile_id": 555, "alias": "Top40_4v4_Ok"}]},
                            {"id": 11, "members": [{"profile_id": 666, "alias": "Top12_4v4_BelowFloor"}]},
                        ],
                        "leaderboardStats": [
                            {"statgroup_id": 10, "rank": 40, "rating": 1950},
                            {"statgroup_id": 11, "rank": 12, "rating": 1820},  # < 1850 piso 4v4
                        ],
                    }
                return {"statGroups": [], "leaderboardStats": []}

        fake_relic = _FakeRelic()
        top_map = fetch_global_top_players(fake_relic)
        self.assertEqual(len(fake_relic.seen_urls), 16)
        self.assertTrue(all("count=50" in u for u in fake_relic.seen_urls))
        self.assertEqual(set(top_map.keys()), {111, 222, 555})
        self.assertEqual(top_map[111]["band"], "top10")
        self.assertEqual(top_map[222]["band"], "top50")
        self.assertEqual(top_map[555]["mode"], "4v4")

        # Mesmo com dicionário não filtrado, jogador abaixo do piso do modo é ignorado em score_match_of_the_day
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 20000
        raw_loss = {
            "id": 2101,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts - 2200,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, False, "Chacineiro", "br", 1560, 1548, 18000, 2200, vp0=0, vp1=35),
                _make_report(910010, 1, 137123, True, "OppTop", "de", 1750, 1762, 18500, 2200, vp0=0, vp1=35),
            ],
        }
        norm_loss = merge_and_filter_day_matches([raw_loss], start_ts, end_ts)[0]
        unfiltered_low = {910010: {"profile_id": 910010, "rank": 15, "mode": "4v4", "elo": 1800}}
        scored_low = score_match_of_the_day(norm_loss, top_global_by_pid=unfiltered_low)
        self.assertIsNone(scored_low["best_opp_rank"])
        self.assertFalse(scored_low["is_qualified_epic_loss"])
        self.assertFalse(scored_low["axis_team"][0]["is_top_global"])
        self.assertFalse(any("TOP #" in b for b in scored_low["badges"]))

        valid_top = {910010: {"profile_id": 910010, "rank": 15, "mode": "1v1", "elo": 1825}}
        scored_high = score_match_of_the_day(norm_loss, top_global_by_pid=valid_top)
        self.assertEqual(scored_high["best_opp_rank"], 15)
        self.assertEqual(scored_high["best_opp_global_band"], "top25")
        self.assertTrue(scored_high["is_qualified_epic_loss"])
        self.assertEqual(scored_high["selection_tier"], 3)
        self.assertTrue(scored_high["axis_team"][0]["is_top_global"])
        self.assertTrue(any("VS TOP #15 GLOBAL" in b for b in scored_high["badges"]))

        # Pontuação em faixas: Top 1-10 > Top 11-25 > Top 26-50 > teto Top 15 BR (15 pts)
        raw_win = {
            "id": 2102,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts + 100 - 1500,
            "completiontime": ts + 100,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, True, "Chacineiro", "br", 1560, 1580, 14000, 1500),
                _make_report(910010, 1, 137123, False, "OppTop", "de", 1780, 1760, 13500, 1500),
            ],
        }
        norm_win = merge_and_filter_day_matches([raw_win], start_ts, end_ts)[0]
        s_t10 = score_match_of_the_day(norm_win, top_global_by_pid={910010: {"rank": 8, "mode": "1v1", "elo": 2010}})
        s_t25 = score_match_of_the_day(norm_win, top_global_by_pid={910010: {"rank": 18, "mode": "1v1", "elo": 1900}})
        s_t50 = score_match_of_the_day(norm_win, top_global_by_pid={910010: {"rank": 50, "mode": "1v1", "elo": 1720}})
        self.assertEqual(s_t10["selection_tier"], 5)
        self.assertEqual(s_t25["selection_tier"], 5)
        self.assertEqual(s_t50["selection_tier"], 5)
        self.assertGreater(s_t10["score_breakdown"]["top_global"], s_t25["score_breakdown"]["top_global"])
        self.assertGreater(s_t25["score_breakdown"]["top_global"], s_t50["score_breakdown"]["top_global"])
        self.assertGreater(s_t50["score_breakdown"]["top_global"], 15.0)
        self.assertLessEqual(s_t10["score_breakdown"]["top_global"], 35.0)

    def test_top_global_win_in_any_mode_is_tier5_and_beats_rank1_br_impact(self) -> None:
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 36000
        ctx = self._sample_unified_context()

        # A: 1v1 que assume o #1 do Ranking Unificado BR (hjax ultrapassa Itaperuna), SEM Top Global -> Tier 4
        rank1_br_win = {
            "id": 2151,
            "matchtype_id": 1,
            "mapname": "pachino_2p",
            "startgametime": ts - 2400,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(21206, 0, 129494, True, "hjax", "br", 1570, 1605, 22000, 2400, vp0=18, vp1=0, vkill=6, sqkill=12, ekills=80, edeaths=50),
                _make_report(915101, 1, 137123, False, "StrongNonTop", "de", 1680, 1645, 21000, 2400, vp0=18, vp1=0),
            ],
        }
        # B: 4v4 isolado onde Veterano vence Top #42 Global (4v4 ELO 1980 >= 1850) -> Tier 5!
        win_4v4_vs_top50 = {
            "id": 2152,
            "matchtype_id": 4,
            "mapname": "winter_line_8p_mkii",
            "startgametime": ts + 100 - 1500,
            "completiontime": ts + 100,
            "matchhistoryreportresults": [
                _make_report(5768, 0, 129494, True, "The Dog", "br", 1450, 1465, 16000, 1500),
                _make_report(915201, 0, 197345, True, "Ally1", "us", 1440, 1455, 15000, 1500),
                _make_report(915202, 0, 129494, True, "Ally2", "ca", 1430, 1445, 15000, 1500),
                _make_report(915203, 0, 197345, True, "Ally3", "fr", 1420, 1435, 14500, 1500),
                _make_report(915299, 1, 137123, False, "Global4v4Pro", "de", 1980, 1965, 16500, 1500),
                _make_report(915205, 1, 198437, False, "Axis2", "pl", 1440, 1425, 14000, 1500),
                _make_report(915206, 1, 137123, False, "Axis3", "ru", 1430, 1415, 13500, 1500),
                _make_report(915207, 1, 198437, False, "Axis4", "gb", 1420, 1405, 13500, 1500),
            ],
        }
        # C: Drop relâmpago (< 8 min) contra Top #5 Global NÃO pode subir para Tier 5
        drop_vs_top5 = {
            "id": 2153,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts + 200 - 180,
            "completiontime": ts + 200,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, True, "Chacineiro", "br", 1566, 1585, 800, 180),
                _make_report(915399, 1, 137123, False, "Top5DC", "de", 2050, 2031, 200, 180),
            ],
        }

        m_rank1, m_4v4_top, m_drop = merge_and_filter_day_matches(
            [rank1_br_win, win_4v4_vs_top50, drop_vs_top5], start_ts, end_ts
        )
        top_global = {
            915299: {"profile_id": 915299, "rank": 42, "mode": "4v4", "elo": 1980, "alias": "Global4v4Pro"},
            915399: {"profile_id": 915399, "rank": 5, "mode": "1v1", "elo": 2050, "alias": "Top5DC"},
        }
        recap = build_daily_recap(
            [m_rank1, m_4v4_top, m_drop],
            "2026-11-19",
            top_global_by_pid=top_global,
            unified_ranking_context=ctx,
        )
        motd = recap["match_of_the_day"]
        self.assertEqual(motd["match_id"], 2152)
        self.assertEqual(motd["selection_tier"], 5)
        self.assertIn("VENCEU TOP #42 GLOBAL", motd["badges"])

        # Verifica individualmente que o #1 BR ficou em Tier 4 (15 pts máx. Top 15) e drop < 8 min ficou em Tier 2
        impacts = annotate_unified_top15_impacts([m_rank1, m_4v4_top, m_drop], ctx)
        s_rank1 = score_match_of_the_day(m_rank1, top_global_by_pid=top_global, top15_impact=impacts.get(2151))
        self.assertEqual(s_rank1["selection_tier"], 4)
        self.assertEqual(s_rank1["score_breakdown"]["unified_top15"], 15.0)
        self.assertIn("NOVO #1 UNIFICADO", s_rank1["badges"])
        self.assertGreater(motd["score_breakdown"]["top_global"], s_rank1["score_breakdown"]["unified_top15"])

        s_drop = score_match_of_the_day(m_drop, top_global_by_pid=top_global)
        self.assertEqual(s_drop["selection_tier"], 2)

    def test_victory_priority_and_qualified_epic_loss_rules(self) -> None:
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 30000
        top_global = {920001: {"profile_id": 920001, "rank": 7, "mode": "1v1", "elo": 1920}}

        # A: Vitória comum em 2v2 (22 min, sem Top Global) -> Tier 4
        win_2v2 = {
            "id": 2201,
            "matchtype_id": 2,
            "mapname": "torrente_4p_mkiii",
            "startgametime": ts - 1320,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(639157, 0, 129494, True, "Itaperuna", "br", 1550, 1564, 14000, 1320),
                _make_report(79327, 0, 197345, True, "Chacineiro", "br", 1520, 1534, 15000, 1320),
                _make_report(920101, 1, 137123, False, "OppA", "de", 1530, 1516, 13000, 1320),
                _make_report(920102, 1, 198437, False, "OppB", "uk", 1520, 1506, 12500, 1320),
            ],
        }
        # B: Derrota épica qualificada em 1v1 (42 min, disputada 25x0 VP, contra Top #7) -> Tier 3
        epic_loss_1v1 = {
            "id": 2202,
            "matchtype_id": 1,
            "mapname": "pachino_2p",
            "startgametime": ts + 100 - 2520,
            "completiontime": ts + 100,
            "matchhistoryreportresults": [
                _make_report(639157, 0, 129494, False, "Itaperuna", "br", 1600, 1592, 18500, 2520, vp0=0, vp1=25),
                _make_report(920001, 1, 137123, True, "Top7Boss", "de", 1920, 1928, 19200, 2520, vp0=0, vp1=25),
            ],
        }
        # C: Vitória em 4v4 isolado (1 Veterano + 3 gringos vs 4 gringos, 45 min, 15x0 VP, sem Top Global) -> Tier 2
        isolated_4v4_win = {
            "id": 2203,
            "matchtype_id": 4,
            "mapname": "winter_line_8p_mkii",
            "startgametime": ts + 200 - 2700,
            "completiontime": ts + 200,
            "matchhistoryreportresults": [
                _make_report(5768, 0, 129494, True, "The Dog", "br", 1450, 1465, 22000, 2700, vp0=15, vp1=0),
                _make_report(920201, 0, 197345, True, "Ally1", "us", 1440, 1455, 20000, 2700, vp0=15, vp1=0),
                _make_report(920202, 0, 129494, True, "Ally2", "ca", 1430, 1445, 21000, 2700, vp0=15, vp1=0),
                _make_report(920203, 0, 197345, True, "Ally3", "fr", 1420, 1435, 19000, 2700, vp0=15, vp1=0),
                _make_report(920204, 1, 137123, False, "Axis1", "de", 1450, 1435, 20500, 2700, vp0=15, vp1=0),
                _make_report(920205, 1, 198437, False, "Axis2", "pl", 1440, 1425, 19500, 2700, vp0=15, vp1=0),
                _make_report(920206, 1, 137123, False, "Axis3", "ru", 1430, 1415, 20000, 2700, vp0=15, vp1=0),
                _make_report(920207, 1, 198437, False, "Axis4", "gb", 1420, 1405, 20000, 2700, vp0=15, vp1=0),
            ],
        }
        # D: Derrota NÃO qualificada contra Top #7 (curta: 20 min < 30 min e desequilibrada 380x0 VP) -> Tier 1
        unqualified_loss_1v1 = {
            "id": 2204,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts + 300 - 1200,
            "completiontime": ts + 300,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, False, "Chacineiro", "br", 1560, 1552, 4500, 1200, vp0=0, vp1=380),
                _make_report(920001, 1, 137123, True, "Top7Boss", "de", 1920, 1928, 12000, 1200, vp0=0, vp1=380),
            ],
        }

        m_a, m_b, m_c, m_d = merge_and_filter_day_matches(
            [win_2v2, epic_loss_1v1, isolated_4v4_win, unqualified_loss_1v1], start_ts, end_ts
        )

        # 1) Vitória em modo prioritário (A) vence derrota épica (B) e 4v4 isolado (C)
        recap_all = build_daily_recap([m_a, m_b, m_c, m_d], "2026-11-19", top_global_by_pid=top_global)
        self.assertEqual(recap_all["match_of_the_day"]["match_id"], 2201)
        self.assertEqual(recap_all["match_of_the_day"]["selection_tier"], 4)

        # 2) Sem vitória prioritária, derrota épica qualificada (B) supera 4v4 isolado (C) e derrota comum (D)
        recap_no_prio_win = build_daily_recap([m_b, m_c, m_d], "2026-11-19", top_global_by_pid=top_global)
        self.assertEqual(recap_no_prio_win["match_of_the_day"]["match_id"], 2202)
        self.assertEqual(recap_no_prio_win["match_of_the_day"]["selection_tier"], 3)
        self.assertTrue(recap_no_prio_win["match_of_the_day"]["is_qualified_epic_loss"])

        # 3) Derrota não qualificada (D) perde até para vitória em 4v4 isolado (C)
        recap_fallback = build_daily_recap([m_c, m_d], "2026-11-19", top_global_by_pid=top_global)
        self.assertEqual(recap_fallback["match_of_the_day"]["match_id"], 2203)
        self.assertEqual(recap_fallback["match_of_the_day"]["selection_tier"], 2)

    def test_upset_anticarry_guard_and_short_stomp_penalty_and_individual_perf(self) -> None:
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 38000

        # 1) 2v2 com Veterano carregado (1200 ELO, 5k dano) por aliado gringo forte (1660 ELO, 19k dano) vs 1560+1560
        carried_2v2 = {
            "id": 2251,
            "matchtype_id": 2,
            "mapname": "torrente_4p_mkiii",
            "startgametime": ts - 1600,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(6557, 0, 129494, True, "Dragoness", "br", 1200, 1222, 5000, 1600, ekills=18, edeaths=40),
                _make_report(925101, 0, 197345, True, "ForeignCarry", "de", 1660, 1682, 19000, 1600, ekills=75, edeaths=25),
                _make_report(925102, 1, 137123, False, "Opp1", "fr", 1560, 1538, 12000, 1600, ekills=35, edeaths=45),
                _make_report(925103, 1, 198437, False, "Opp2", "gb", 1560, 1538, 12000, 1600, ekills=30, edeaths=48),
            ],
        }
        # 2) 2v2 genuíno 100% Veteranos (mesmo déficit de ELO +130, mas dupla equilibrada) -> sem trava anti-carry
        genuine_2v2 = {
            "id": 2252,
            "matchtype_id": 2,
            "mapname": "torrente_4p_mkiii",
            "startgametime": ts + 100 - 1600,
            "completiontime": ts + 100,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, True, "Chacineiro", "br", 1430, 1452, 14500, 1600, vkill=5, sqkill=8, ekills=65, edeaths=30),
                _make_report(639157, 0, 197345, True, "Itaperuna", "br", 1430, 1452, 13800, 1600, vkill=4, sqkill=7, ekills=58, edeaths=32),
                _make_report(925201, 1, 137123, False, "Opp1", "fr", 1560, 1538, 13500, 1600),
                _make_report(925202, 1, 198437, False, "Opp2", "gb", 1560, 1538, 13200, 1600),
            ],
        }
        # 3) Stomp curto (10 min, 420 x 0 VP, desequilibrado) vs batalha disputada de 32 min
        short_stomp_10m = {
            "id": 2253,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts + 200 - 600,
            "completiontime": ts + 200,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, True, "Chacineiro", "br", 1500, 1510, 8000, 600, vp0=420, vp1=0),
                _make_report(925301, 1, 137123, False, "StompedOpp", "de", 1500, 1490, 2800, 600, vp0=420, vp1=0),
            ],
        }
        contested_32m = {
            "id": 2254,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts + 300 - 1920,
            "completiontime": ts + 300,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, True, "Chacineiro", "br", 1500, 1510, 22000, 1920, vp0=32, vp1=0, vkill=6, sqkill=10, ekills=75, edeaths=45),
                _make_report(925401, 1, 137123, False, "CloseOpp", "de", 1500, 1490, 21000, 1920, vp0=32, vp1=0),
            ],
        }
        # 4) Clássico BR vs BR 1v1 com grande atuação individual -> Pilar 5 respeita teto de 10 pts
        br_derby_1v1 = {
            "id": 2255,
            "matchtype_id": 1,
            "mapname": "pachino_2p",
            "startgametime": ts + 400 - 1800,
            "completiontime": ts + 400,
            "matchhistoryreportresults": [
                _make_report(639157, 0, 129494, True, "Itaperuna", "br", 1560, 1572, 24000, 1800, vkill=8, sqkill=12, ekills=95, edeaths=40),
                _make_report(79327, 1, 137123, False, "Chacineiro", "br", 1560, 1548, 22000, 1800),
            ],
        }

        m_car, m_gen, m_stomp, m_close, m_derby = merge_and_filter_day_matches(
            [carried_2v2, genuine_2v2, short_stomp_10m, contested_32m, br_derby_1v1], start_ts, end_ts
        )

        s_car = score_match_of_the_day(m_car)
        s_gen = score_match_of_the_day(m_gen)
        self.assertTrue(s_car["anti_carry_capped"])
        self.assertEqual(s_car["score_breakdown"]["upset"], 0.0)
        self.assertFalse(any("UPSET" in b for b in s_car["badges"]))
        self.assertFalse(s_gen["anti_carry_capped"])
        self.assertGreater(s_gen["score_breakdown"]["upset"], 5.0)
        self.assertGreater(s_gen["score_breakdown"]["upset_lobby"], s_car["score_breakdown"]["upset_lobby"])
        self.assertGreater(s_gen["epicness_score"], s_car["epicness_score"])

        s_stomp = score_match_of_the_day(m_stomp)
        s_close = score_match_of_the_day(m_close)
        self.assertLess(s_stomp["score_breakdown"]["drama"], 0.0)
        self.assertGreater(s_close["score_breakdown"]["drama"], 14.0)
        self.assertGreater(s_close["epicness_score"], s_stomp["epicness_score"] + 15)

        s_derby = score_match_of_the_day(m_derby)
        self.assertEqual(s_derby["score_breakdown"]["br_derby"], 4.0)
        self.assertGreater(s_derby["score_breakdown"]["individual"], 3.0)
        self.assertLessEqual(s_derby["score_breakdown"]["mode_perf_br"], 10.0)
        self.assertEqual(s_derby["score_breakdown"]["mode_perf_br"], 10.0)

    def test_closed_full_veteranos_team_game_and_br_vs_br_promote_team_modes(self) -> None:
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 40000

        # 4v4 com apenas 2 Veteranos + 2 gringos vs 4 gringos -> ainda é isolado (Tier 2)
        partial_4v4 = {
            "id": 2301,
            "matchtype_id": 4,
            "mapname": "winter_line_8p_mkii",
            "startgametime": ts - 2800,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(5768, 0, 129494, True, "The Dog", "br", 1500, 1515, 25000, 2800, vp0=12, vp1=0),
                _make_report(337051, 0, 197345, True, "Maiti", "br", 1500, 1515, 24000, 2800, vp0=12, vp1=0),
                _make_report(930101, 0, 129494, True, "RandomA", "us", 1500, 1515, 22000, 2800, vp0=12, vp1=0),
                _make_report(930102, 0, 197345, True, "RandomB", "de", 1500, 1515, 22000, 2800, vp0=12, vp1=0),
                _make_report(930103, 1, 137123, False, "Opp1", "de", 1510, 1495, 23000, 2800, vp0=12, vp1=0),
                _make_report(930104, 1, 198437, False, "Opp2", "fr", 1510, 1495, 23000, 2800, vp0=12, vp1=0),
                _make_report(930105, 1, 137123, False, "Opp3", "pl", 1510, 1495, 23000, 2800, vp0=12, vp1=0),
                _make_report(930106, 1, 198437, False, "Opp4", "gb", 1510, 1495, 23000, 2800, vp0=12, vp1=0),
            ],
        }
        # 3v3 com TIME FECHADO 100% Veteranos (3/3) -> promovido a Tier 4
        closed_3v3 = {
            "id": 2302,
            "matchtype_id": 3,
            "mapname": "benghazi_6p",
            "startgametime": ts + 100 - 1500,
            "completiontime": ts + 100,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, True, "Chacineiro", "br", 1480, 1495, 14000, 1500),
                _make_report(639157, 0, 197345, True, "Itaperuna", "br", 1490, 1505, 14500, 1500),
                _make_report(6557, 0, 129494, True, "Dragoness", "br", 1470, 1485, 13500, 1500),
                _make_report(930201, 1, 137123, False, "OppX1", "de", 1480, 1465, 12000, 1500),
                _make_report(930202, 1, 198437, False, "OppX2", "fr", 1480, 1465, 11500, 1500),
                _make_report(930203, 1, 137123, False, "OppX3", "us", 1480, 1465, 11000, 1500),
            ],
        }
        # 4v4 com 1 Veterano enfrentando outro BRASILEIRO (country="br") do outro lado -> promovido a Tier 4!
        br_vs_br_4v4 = {
            "id": 2303,
            "matchtype_id": 4,
            "mapname": "monte_cavo_8p",
            "startgametime": ts + 200 - 1600,
            "completiontime": ts + 200,
            "matchhistoryreportresults": [
                _make_report(5768, 0, 129494, True, "The Dog", "br", 1460, 1475, 15000, 1600),
                _make_report(930301, 0, 197345, True, "Ally1", "us", 1450, 1465, 14000, 1600),
                _make_report(930302, 0, 129494, True, "Ally2", "ca", 1450, 1465, 14000, 1600),
                _make_report(930303, 0, 197345, True, "Ally3", "gb", 1450, 1465, 14000, 1600),
                _make_report(930304, 1, 137123, False, "OutroBrasileiro", "br", 1460, 1445, 14500, 1600),
                _make_report(930305, 1, 198437, False, "Axis2", "de", 1450, 1435, 13500, 1600),
                _make_report(930306, 1, 137123, False, "Axis3", "pl", 1450, 1435, 13500, 1600),
                _make_report(930307, 1, 198437, False, "Axis4", "ru", 1450, 1435, 13500, 1600),
            ],
        }

        m_partial, m_closed, m_br = merge_and_filter_day_matches([partial_4v4, closed_3v3, br_vs_br_4v4], start_ts, end_ts)

        s_partial = score_match_of_the_day(m_partial)
        self.assertTrue(s_partial["is_isolated_team_game"])
        self.assertFalse(s_partial["is_full_vet_team_game"])
        self.assertEqual(s_partial["selection_tier"], 2)

        s_closed = score_match_of_the_day(m_closed)
        self.assertTrue(s_closed["is_full_vet_team_game"])
        self.assertFalse(s_closed["is_isolated_team_game"])
        self.assertEqual(s_closed["selection_tier"], 4)
        self.assertIn("TIME FECHADO VETERANOS", s_closed["badges"])

        s_br = score_match_of_the_day(m_br)
        self.assertTrue(s_br["has_br_opponent"])
        self.assertFalse(s_br["is_isolated_team_game"])
        self.assertEqual(s_br["selection_tier"], 4)
        self.assertIn("DUELO BR vs BR", s_br["badges"])

        # Time fechado 3v3 vence o 4v4 parcial mesmo sendo mais curto
        recap_closed = build_daily_recap([m_partial, m_closed], "2026-11-19")
        self.assertEqual(recap_closed["match_of_the_day"]["match_id"], 2302)

        # 4v4 com outro brasileiro do outro lado também vence o 4v4 isolado
        recap_br = build_daily_recap([m_partial, m_br], "2026-11-19")
        self.assertEqual(recap_br["match_of_the_day"]["match_id"], 2303)

    def _sample_unified_context(self) -> dict:
        rows = [
            {"profile_id": 639157, "alias": "Itaperuna", "br_rank": 1, "skill_points": 2200.0, "wins": 500,
             "mode_max_elos": {"1v1": 1600}, "mode_best_factions": {"1v1": "german"}},
            {"profile_id": 21206, "alias": "hjax", "br_rank": 2, "skill_points": 2140.0, "wins": 400,
             "mode_max_elos": {"1v1": 1570}, "mode_best_factions": {"1v1": "american"}},
            {"profile_id": 79327, "alias": "Chacineiro", "br_rank": 3, "skill_points": 2132.0, "wins": 400,
             "mode_max_elos": {"1v1": 1566}, "mode_best_factions": {"1v1": "american"}},
            {"profile_id": 691, "alias": "Vítor", "br_rank": 4, "skill_points": 2080.0, "wins": 300,
             "mode_max_elos": {"1v1": 1540}, "mode_best_factions": {"1v1": "german"}},
            {"profile_id": 366826, "alias": "Lem22", "br_rank": 5, "skill_points": 1750.0, "wins": 600,
             "mode_max_elos": {"4v4": 1750}, "mode_best_factions": {"4v4": "american"}},
            {"profile_id": 31145, "alias": "vonMises", "br_rank": 6, "skill_points": 1742.0, "wins": 350,
             "mode_max_elos": {"4v4": 1742}, "mode_best_factions": {"4v4": "german"}},
            {"profile_id": 991764, "alias": "SaNgar [BR]", "br_rank": 7, "skill_points": 1680.0, "wins": 300,
             "mode_max_elos": {"1v1": 1340}, "mode_best_factions": {"1v1": "american"}},
            {"profile_id": 1005730, "alias": "arc", "br_rank": 8, "skill_points": 1660.0, "wins": 300,
             "mode_max_elos": {"1v1": 1330}, "mode_best_factions": {"1v1": "german"}},
            {"profile_id": 1088455, "alias": "luiz_fmh", "br_rank": 9, "skill_points": 1640.0, "wins": 250,
             "mode_max_elos": {"1v1": 1320}, "mode_best_factions": {"1v1": "american"}},
            {"profile_id": 900358, "alias": "Margheriti", "br_rank": 10, "skill_points": 1620.0, "wins": 240,
             "mode_max_elos": {"1v1": 1310}, "mode_best_factions": {"1v1": "german"}},
            {"profile_id": 55693, "alias": "Alface aka BinhoBiceps", "br_rank": 11, "skill_points": 1600.0, "wins": 230,
             "mode_max_elos": {"1v1": 1300}, "mode_best_factions": {"1v1": "american"}},
            {"profile_id": 110078, "alias": "Bineto", "br_rank": 12, "skill_points": 1580.0, "wins": 220,
             "mode_max_elos": {"1v1": 1290}, "mode_best_factions": {"1v1": "german"}},
            {"profile_id": 337051, "alias": "Maiti", "br_rank": 13, "skill_points": 1560.0, "wins": 210,
             "mode_max_elos": {"1v1": 1280}, "mode_best_factions": {"1v1": "american"}},
            {"profile_id": 559962, "alias": "Gabe", "br_rank": 14, "skill_points": 1540.0, "wins": 200,
             "mode_max_elos": {"1v1": 1270}, "mode_best_factions": {"1v1": "german"}},
            {"profile_id": 5768, "alias": "The Dog", "br_rank": 15, "skill_points": 1520.0, "wins": 190,
             "mode_max_elos": {"4v4": 1520}, "mode_best_factions": {"4v4": "american"}},
            {"profile_id": 1022333, "alias": "gaules", "br_rank": 16, "skill_points": 1510.0, "wins": 180,
             "mode_max_elos": {"4v4": 1510}, "mode_best_factions": {"4v4": "german"}},
            {"profile_id": 6557, "alias": "Dragoness", "br_rank": 17, "skill_points": 1400.0, "wins": 150,
             "mode_max_elos": {"2v2": 1266}, "mode_best_factions": {"2v2": "american"}},
        ]
        # Catálogo com segunda facção inferior para Itaperuna (1v1 american 1320 vs 1v1 german 1600)
        catalog = [
            {
                "profile_id": 639157,
                "modes": {
                    "1v1_german": {"mode": "1v1", "faction": "german", "elo": 1600, "wins": 100, "losses": 50},
                    "1v1_american": {"mode": "1v1", "faction": "american", "elo": 1320, "wins": 40, "losses": 30},
                },
            }
        ]
        return build_unified_ranking_context(rows, catalog)

    def test_unified_top15_rank_swap_and_top5_highlight_drive_match_of_the_day(self) -> None:
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 35000
        ctx = self._sample_unified_context()

        # Partida 1: Vitória longa comum de jogador fora do Top 15 (#17 Dragoness, 34m, 2v2, pareada)
        routine_win = {
            "id": 2401,
            "matchtype_id": 2,
            "mapname": "rails_and_sand_4p",
            "startgametime": ts - 2040,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(6557, 0, 129494, True, "Dragoness", "br", 1266, 1280, 15000, 2040),
                _make_report(940101, 0, 197345, True, "Ally", "us", 1250, 1264, 14500, 2040),
                _make_report(940102, 1, 137123, False, "Axis1", "de", 1270, 1256, 14200, 2040),
                _make_report(940103, 1, 198437, False, "Axis2", "fr", 1260, 1246, 14000, 2040),
            ],
        }
        # Partida 2: #3 Chacineiro vence 1v1 USF (1566 -> 1580 => 2132 -> 2160 SP) e ULTRAPASSA #2 hjax (2140 SP)!
        top5_climb_win = {
            "id": 2402,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts + 100 - 1500,
            "completiontime": ts + 100,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, True, "Chacineiro", "br", 1566, 1580, 13500, 1500, vkill=5, sqkill=8),
                _make_report(940201, 1, 137123, False, "Opp1v1", "de", 1580, 1566, 12500, 1500),
            ],
        }
        # Partida 3: #1 Itaperuna joga 1v1 com facção secundária (american 1320 < german 1600) -> NÃO mexe nos SP!
        off_faction_match = {
            "id": 2403,
            "matchtype_id": 1,
            "mapname": "pachino_2p",
            "startgametime": ts + 200 - 1400,
            "completiontime": ts + 200,
            "matchhistoryreportresults": [
                _make_report(639157, 0, 129494, True, "Itaperuna", "br", 1320, 1338, 12000, 1400),
                _make_report(940301, 1, 137123, False, "OppOff", "pl", 1330, 1312, 11000, 1400),
            ],
        }
        # Partida 4: #12 Bineto sobe para #11 ultrapassando Alface -> rastreado no Top 15 BR!
        top15_climb_win = {
            "id": 2404,
            "matchtype_id": 1,
            "mapname": "langres_2p",
            "startgametime": ts + 300 - 1500,
            "completiontime": ts + 300,
            "matchhistoryreportresults": [
                _make_report(110078, 0, 137123, True, "Bineto", "br", 1290, 1308, 12500, 1500),
                _make_report(940401, 1, 129494, False, "OppMid", "us", 1305, 1287, 11800, 1500),
            ],
        }

        m_routine, m_climb, m_off, m_top15 = merge_and_filter_day_matches(
            [routine_win, top5_climb_win, off_faction_match, top15_climb_win], start_ts, end_ts
        )
        impacts = annotate_unified_top15_impacts([m_routine, m_climb, m_off, m_top15], ctx)
        self.assertNotIn(2401, impacts)  # #17 fora do Top 15
        self.assertNotIn(2403, impacts)  # facção secundária não altera M_1v1 nem Skill Points!
        self.assertIn(2402, impacts)
        self.assertIn(2404, impacts)
        self.assertTrue(impacts[2402]["affects_top15_rank"])
        self.assertTrue(impacts[2402]["affects_top5_zone"])
        self.assertEqual(impacts[2402]["badge"], "TOP 5 BR (#3→#2)")
        self.assertIn("#3 → #2", impacts[2402]["summary"])

        self.assertTrue(impacts[2404]["affects_top15_rank"])
        self.assertFalse(impacts[2404]["affects_top5_zone"])
        self.assertEqual(impacts[2404]["badge"], "TOP 15 BR (#12→#11)")
        self.assertIn("#12 → #11", impacts[2404]["summary"])

        # build_daily_recap deve eleger a partida do Top 5 (mais pontos que #12->#11 e rotina fora do Top 15)
        fake_cohdb = CohdbReplayClient()
        with patch.object(fake_cohdb, "inspect_match", return_value={"verified": True, "url": "https://cohdb.com/matches/2402", "vp_pair": None, "share_pair": None}):
            recap = build_daily_recap(
                [m_routine, m_climb, m_off, m_top15],
                "2026-11-19",
                cohdb_client=fake_cohdb,
                unified_ranking_context=ctx,
            )

        motd = recap["match_of_the_day"]
        self.assertEqual(motd["match_id"], 2402)
        self.assertEqual(motd["selection_tier"], 4)
        self.assertTrue(motd["affects_top15_rank"])
        self.assertTrue(motd["affects_top5_zone"])
        self.assertIn("TOP 5 BR (#3→#2)", motd["badges"])
        self.assertTrue(motd["cohdb_verified"])  # confirma que re-score do cohdb preservou o impacto Top 15!

        # Embed do Discord deve exibir linha dedicada de Top 15 Unificado BR destacando Top 5
        payload = build_daily_recap_payload(recap, ["boletim_diario_p1.png", "jogo_do_dia.png"])
        self.assertIn("Top 15 Unificado BR (Top 5)", payload["embeds"][1]["description"])
        self.assertIn("Chacineiro", payload["embeds"][1]["description"])

    def test_unified_top15_and_top5_entry_promote_4v4_and_handle_contested_drop(self) -> None:
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 42000
        ctx = self._sample_unified_context()

        # #6 vonMises vence 4v4 isolado de WEHR (1742 -> 1758) e ENTRA NO TOP 5 ultrapassando #5 Lem22 (1750)!
        enter_top5_4v4 = {
            "id": 2501,
            "matchtype_id": 4,
            "mapname": "winter_line_8p_mkii",
            "startgametime": ts - 1800,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(31145, 1, 137123, True, "vonMises", "br", 1742, 1758, 21000, 1800),
                _make_report(950101, 1, 198437, True, "Ax2", "de", 1700, 1715, 19000, 1800),
                _make_report(950102, 1, 137123, True, "Ax3", "pl", 1700, 1715, 19000, 1800),
                _make_report(950103, 1, 198437, True, "Ax4", "ru", 1700, 1715, 19000, 1800),
                _make_report(950104, 0, 129494, False, "Al1", "us", 1710, 1695, 18000, 1800),
                _make_report(950105, 0, 197345, False, "Al2", "ca", 1710, 1695, 18000, 1800),
                _make_report(950106, 0, 129494, False, "Al3", "gb", 1710, 1695, 18000, 1800),
                _make_report(950107, 0, 197345, False, "Al4", "fr", 1710, 1695, 18000, 1800),
            ],
        }
        # #2 hjax perde 1v1 disputado (25 min, 35x0 VP) e cai para #3 atrás de Chacineiro -> Tier 3
        drop_top5_loss = {
            "id": 2502,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts + 100 - 1500,
            "completiontime": ts + 100,
            "matchhistoryreportresults": [
                _make_report(21206, 0, 129494, False, "hjax", "br", 1570, 1555, 14000, 1500, vp0=0, vp1=35),
                _make_report(950201, 1, 137123, True, "OppStrong", "de", 1590, 1605, 14500, 1500, vp0=0, vp1=35),
            ],
        }
        # #16 gaules vence 4v4 isolado (1510 -> 1528) e ENTRA NO TOP 15 ultrapassando #15 The Dog (1520)!
        enter_top15_4v4 = {
            "id": 2503,
            "matchtype_id": 4,
            "mapname": "winter_line_8p_mkii",
            "startgametime": ts + 200 - 1800,
            "completiontime": ts + 200,
            "matchhistoryreportresults": [
                _make_report(1022333, 1, 137123, True, "gaules", "br", 1510, 1528, 19000, 1800),
                _make_report(950301, 1, 198437, True, "Ax2", "de", 1500, 1518, 18000, 1800),
                _make_report(950302, 1, 137123, True, "Ax3", "pl", 1500, 1518, 18000, 1800),
                _make_report(950303, 1, 198437, True, "Ax4", "ru", 1500, 1518, 18000, 1800),
                _make_report(950304, 0, 129494, False, "Al1", "us", 1510, 1492, 17000, 1800),
                _make_report(950305, 0, 197345, False, "Al2", "ca", 1510, 1492, 17000, 1800),
                _make_report(950306, 0, 129494, False, "Al3", "gb", 1510, 1492, 17000, 1800),
                _make_report(950307, 0, 197345, False, "Al4", "fr", 1510, 1492, 17000, 1800),
            ],
        }

        m_enter5, m_drop, m_enter15 = merge_and_filter_day_matches(
            [enter_top5_4v4, drop_top5_loss, enter_top15_4v4], start_ts, end_ts
        )
        impacts = annotate_unified_top15_impacts([m_enter5, m_drop, m_enter15], ctx)

        s_enter5 = score_match_of_the_day(m_enter5, top15_impact=impacts.get(2501))
        self.assertTrue(s_enter5["affects_top15_rank"])
        self.assertTrue(s_enter5["affects_top5_zone"])
        self.assertEqual(s_enter5["selection_tier"], 4)  # 4v4 promovido a Tier 4 por entrar no Top 5!
        self.assertIn("ENTROU NO TOP 5 (#5)", s_enter5["badges"])
        self.assertEqual(s_enter5["score_breakdown"]["unified_top15"], 13.0)

        s_drop = score_match_of_the_day(m_drop, top15_impact=impacts.get(2502))
        self.assertTrue(s_drop["affects_top15_rank"])
        self.assertEqual(s_drop["selection_tier"], 3)  # derrota disputada que mexeu no Top 5 -> Tier 3
        self.assertIn("MEXEU NO TOP 5 (#2→#3)", s_drop["badges"])
        self.assertEqual(s_drop["score_breakdown"]["unified_top15"], 6.5)

        s_enter15 = score_match_of_the_day(m_enter15, top15_impact=impacts.get(2503))
        self.assertTrue(s_enter15["affects_top15_rank"])
        self.assertFalse(s_enter15["affects_top5_zone"])
        self.assertEqual(s_enter15["selection_tier"], 4)  # 4v4 promovido a Tier 4 por entrar no Top 15!
        self.assertIn("ENTROU NO TOP 15 (#15)", s_enter15["badges"])
        self.assertEqual(s_enter15["score_breakdown"]["unified_top15"], 10.0)

        # Valida também carregamento real do ranking_unificado_br.json do repositório (sem erro)
        repo_root = Path(__file__).resolve().parent.parent
        loaded_ctx = load_unified_ranking_context(
            repo_root / "ranking_unificado_br.json",
            repo_root / "jogadores_br_steam.json",
        )
        self.assertGreaterEqual(len(loaded_ctx["players"]), 15)

    def test_unified_context_preserves_anticarry_effective_elos(self) -> None:
        from daily_recap import _rank_unified_standings

        # Jogador A tem ELO 2v2 bruto alto (1750), mas Anti-Carry deflacionou para 1600 (-150 offset)
        # Jogador B tem 1v1 puro 1660 (sem deflação) -> B deve ficar acima de A!
        unified_rows = [
            {
                "profile_id": 2001,
                "alias": "PureSolo",
                "inactive_fallback": False,
                "wins": 50,
                "mode_max_elos": {"1v1": 1660, "2v2": 1500},
                "mode_effective_elos": {"1v1": 1660.0, "2v2": 1500.0},
            },
            {
                "profile_id": 2002,
                "alias": "CarriedDuo",
                "inactive_fallback": False,
                "wins": 50,
                "mode_max_elos": {"1v1": 1200, "2v2": 1750},
                "mode_effective_elos": {"1v1": 1200.0, "2v2": 1600.0},
            },
        ]
        catalog_players = [
            {
                "profile_id": 2001,
                "alias": "PureSolo",
                "modes": {
                    "1v1": {"american": {"elo": 1660, "games": 40, "wins": 25, "losses": 15}},
                    "2v2": {"american": {"elo": 1500, "games": 20, "wins": 10, "losses": 10}},
                },
            },
            {
                "profile_id": 2002,
                "alias": "CarriedDuo",
                "modes": {
                    "1v1": {"american": {"elo": 1200, "games": 20, "wins": 10, "losses": 10}},
                    "2v2": {"american": {"elo": 1750, "games": 40, "wins": 30, "losses": 10}},
                },
            },
        ]
        ctx = build_unified_ranking_context(unified_rows, catalog_players)
        self.assertAlmostEqual(ctx["players"][2002]["mode_carry_offsets"]["2v2"], -150.0, places=2)
        self.assertEqual(ctx["players"][2002]["mode_carry_offsets"].get("1v1", 0.0), 0.0)

        ranked = _rank_unified_standings(ctx["players"])
        self.assertEqual(ranked[2001]["rank"], 1)
        self.assertEqual(ranked[2002]["rank"], 2)
        self.assertGreater(ranked[2001]["skill_points"], ranked[2002]["skill_points"])

        # Simulação cronológica com partida 2v2 do CarriedDuo não pode fazê-lo ultrapassar PureSolo com +10 ELO bruto
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 40000
        raw_match = {
            "id": 9901,
            "matchtype_id": 2,
            "mapname": "torrente_4p_mkiii",
            "startgametime": ts - 1200,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(2002, 0, 129494, True, "CarriedDuo", "br", 1740, 1750, 22000, 1200),
                _make_report(79327, 0, 203852, True, "Chacineiro", "br", 1900, 1905, 38000, 1200),
                _make_report(991001, 1, 137123, False, "Opp1", "de", 1700, 1690, 24000, 1200),
                _make_report(991002, 1, 198437, False, "Opp2", "de", 1700, 1690, 24000, 1200),
            ],
        }
        with patch("daily_recap._build_veterano_resolution", return_value=(
            {2001: 2001, 2002: 2002, 79327: 79327},
            {
                2001: {"profile_id": 2001, "alias": "PureSolo", "is_veterano": True},
                2002: {"profile_id": 2002, "alias": "CarriedDuo", "is_veterano": True},
                79327: {"profile_id": 79327, "alias": "Chacineiro", "is_veterano": True},
            },
            {},
        )):
            norm = merge_and_filter_day_matches([raw_match], start_ts, end_ts)
            # Valida também o alias retrocompatível annotate_unified_top5_impacts
            impacts = annotate_unified_top5_impacts(norm, ctx)
            self.assertIn(9901, impacts)
            self.assertFalse(impacts[9901]["affects_top15_rank"])
            self.assertEqual(impacts[9901]["top15_sp_changes"][0]["rank_after"], 2)

    def test_real_boletim_diario_match_recalibration_smoke(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        boletim_path = repo_root / "boletim_diario_br.json"
        if not boletim_path.exists():
            self.skipTest("boletim_diario_br.json ausente")
        boletim = json.loads(boletim_path.read_text(encoding="utf-8"))
        motd = boletim.get("match_of_the_day")
        if not isinstance(motd, dict):
            self.skipTest("match_of_the_day ausente em boletim_diario_br.json")

        ctx = load_unified_ranking_context(
            repo_root / "ranking_unificado_br.json",
            repo_root / "jogadores_br_steam.json",
        )
        impacts = annotate_unified_top15_impacts([motd], ctx)
        scored = score_match_of_the_day(motd, top15_impact=impacts.get(int(motd["match_id"])))
        bd = scored["score_breakdown"]
        self.assertGreaterEqual(scored["epicness_score"], 0)
        self.assertLessEqual(scored["epicness_score"], 100)
        self.assertLessEqual(bd["top_global"], 35.0)
        self.assertLessEqual(bd["upset_lobby"], 20.0)
        self.assertLessEqual(bd["drama"], 20.0)
        self.assertLessEqual(bd["unified_top15"], 15.0)
        self.assertLessEqual(bd["mode_perf_br"], 10.0)

    def test_cohdb_html_parser_and_security(self) -> None:
        parsed_vp = CohdbReplayClient.parse_cohdb_match_html(
            '<html><body><span>Victory Points: 27 - 0</span><a href="/replays/123.rec">Download</a></body></html>'
        )
        self.assertEqual(parsed_vp["vp_pair"], (27, 0))
        self.assertTrue(parsed_vp["has_download"])

        parsed_pct = CohdbReplayClient.parse_cohdb_match_html(
            "<html><td>Allies 48%</td><td>Axis 52%</td></html>"
        )
        self.assertEqual(parsed_pct["share_pair"], (48, 52))

        # ID inválido retorna None sem rede
        self.assertIsNone(CohdbReplayClient().inspect_match(0))

    def test_open_data_window_helper_integrates_with_client(self) -> None:
        from arranged_teams import Coh3StatsOpenDataClient
        from daily_recap import fetch_open_data_matches_for_window

        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        client = Coh3StatsOpenDataClient()
        self.assertTrue(hasattr(client, "fetch_day_matches"))

        sample_dump = [
            {"id": 5001, "completiontime": start_ts + 1000, "profile_ids": [79327, 999001]},
            {"id": 5002, "completiontime": start_ts + 1000, "profile_ids": [999001, 999002]},  # sem veterano
            {"id": 5003, "completiontime": start_ts - 500, "profile_ids": [79327, 999001]},   # fora da janela
        ]
        with patch.object(client, "fetch_day_matches", side_effect=[sample_dump, None]):
            got = fetch_open_data_matches_for_window(client, start_ts, end_ts, {79327})
        self.assertEqual([m["id"] for m in got], [5001])


class TestDailyRecapRendererAndDiscord(unittest.TestCase):
    def setUp(self) -> None:
        self.ap = SteamAvatarProvider(db=None, allow_network=False)

    def _sample_recap(self) -> dict:
        _, start_ts, _ = get_brt_day_window("2026-11-19")
        ts = start_ts + 40000
        raw = [
            {
                "id": 3001,
                "matchtype_id": 1,
                "mapname": "pachino_2p",
                "startgametime": ts - 2600,
                "completiontime": ts,
                "matchhistoryreportresults": [
                    {
                        "profile_id": 79327, "teamid": 0, "race_id": 129494, "resulttype": 1,
                        "counters": '{"dmgdone": 78000, "vkill": 6, "sqkill": 12, "gt": 2600, "vp0": 22, "vp1": 0}',
                        "profile": {"name": "/steam/76561197963172584", "alias": "Chacineiro", "country": "br"},
                        "matchhistorymember": {"outcome": 1, "oldrating": 1566, "newrating": 1586},
                    },
                    {
                        "profile_id": 910001, "teamid": 1, "race_id": 137123, "resulttype": 0,
                        "counters": '{"dmgdone": 74000, "vkill": 5, "sqkill": 11, "gt": 2600, "vp0": 22, "vp1": 0}',
                        "profile": {"name": "/steam/76561198000910001", "alias": "@everyone [evil]", "country": "de"},
                        "matchhistorymember": {"outcome": 0, "oldrating": 1850, "newrating": 1830},
                    },
                ],
            }
        ]
        matches = merge_and_filter_day_matches(raw, start_ts, start_ts + 86399)
        top_global = {910001: {"profile_id": 910001, "rank": 38, "mode": "1v1", "faction": "german", "elo": 1750}}
        return build_daily_recap(matches, "2026-11-19", top_global_by_pid=top_global)

    def test_daily_recap_and_motd_png_render_cleanly(self) -> None:
        recap = self._sample_recap()
        self.assertEqual(recap["match_of_the_day"]["selection_tier"], 5)
        self.assertEqual(recap["match_of_the_day"]["best_opp_rank"], 38)
        atts = render_daily_recap_attachments(recap, avatar_provider=self.ap)
        self.assertEqual([a[0] for a in atts], ["boletim_diario_p1.png", "jogo_do_dia.png"])

        with Image.open(io.BytesIO(atts[0][1]), formats=["PNG"]) as im1:
            self.assertEqual(im1.width, 1040)
            self.assertGreater(im1.height, 200)

        with Image.open(io.BytesIO(atts[1][1]), formats=["PNG"]) as im2:
            self.assertEqual(im2.width, 1040)
            self.assertGreater(im2.height, 250)
            # Faixa dourada no topo do cartaz do Jogo do Dia
            self.assertEqual(im2.getpixel((500, 1)), (245, 184, 46))

        # Estado vazio renderiza sem erro
        empty_png = render_daily_recap_table_png(
            {"target_date_brt": "2026-11-19", "summary": {}, "awards": {}},
            [],
            avatar_provider=self.ap,
        )
        with Image.open(io.BytesIO(empty_png), formats=["PNG"]) as im_empty:
            self.assertEqual(im_empty.width, 1040)

        # Renderização direta do Jogo do Dia com badges Top 15 BR + Top #38 Global
        motd_copy = dict(recap["match_of_the_day"])
        motd_copy["badges"] = ["VENCEU TOP #38 GLOBAL", "TOP 15 BR (#12→#11)", "UPSET +20 ELO"]
        motd_png = render_match_of_the_day_png(motd_copy, "2026-11-19", self.ap)
        self.assertTrue(motd_png.startswith(b"\x89PNG"))

    def test_daily_recap_discord_payload_and_post_method(self) -> None:
        recap = self._sample_recap()
        payload = build_daily_recap_payload(recap, ["boletim_diario_p1.png", "jogo_do_dia.png"])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"]), 2)
        motd_desc = payload["embeds"][1]["description"]
        self.assertIn("Top Global Relic", motd_desc)
        self.assertIn("Top #38 1V1", motd_desc)
        self.assertNotIn("@everyone", motd_desc)  # sanitização obrigatória do nick malicioso
        self.assertIn("https://coh3stats.com/matches/3001", motd_desc)
        self.assertIn("https://cohdb.com/matches", motd_desc)

        # Verifica rótulos do Discord para #1, Top 5 e Top 15 BR
        recap_top15 = dict(recap)
        recap_top15["match_of_the_day"] = {
            **recap["match_of_the_day"],
            "affects_rank1": False,
            "affects_top5_zone": False,
            "unified_top15_summary": "Bineto subiu no Top 15 BR (#12 → #11 • +18.0 SP)",
        }
        p15 = build_daily_recap_payload(recap_top15, ["boletim_diario_p1.png", "jogo_do_dia.png"])
        self.assertIn("Top 15 Unificado BR:** Bineto", p15["embeds"][1]["description"])

        recap_rank1 = dict(recap)
        recap_rank1["match_of_the_day"] = {
            **recap["match_of_the_day"],
            "affects_rank1": True,
            "affects_top5_zone": True,
            "unified_top15_summary": "hjax assumiu #1 BR (#2 → #1 • +32.0 SP)",
        }
        p1 = build_daily_recap_payload(recap_rank1, ["boletim_diario_p1.png", "jogo_do_dia.png"])
        self.assertIn("Top 15 Unificado BR (#1):", p1["embeds"][1]["description"])

        valid_wh = "https://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz1234"
        client = DiscordRankingClient(webhook_url=valid_wh)
        client._avatar_provider = self.ap
        with patch.object(client, "_request", return_value=(200, {"id": "1573111111111111111"})) as mock_req:
            msg_id = client.post_daily_recap(recap)
            self.assertEqual(msg_id, "1573111111111111111")
            method, url, sent_payload = mock_req.call_args[0]
            self.assertEqual(method, "POST")
            self.assertIn("wait=true", url)
            self.assertEqual(sent_payload["allowed_mentions"], {"parse": []})

        # Ao postar um novo boletim existindo um anterior, posta o novo e apaga o antigo no Discord
        with patch.object(
            client,
            "_request",
            side_effect=[(200, {"id": "1573222222222222222"}), (204, None)],
        ) as mock_req:
            msg_id2 = client.post_daily_recap(recap, existing_message_id="1573111111111111111")
            self.assertEqual(msg_id2, "1573222222222222222")
            self.assertEqual(mock_req.call_args_list[0][0][0], "POST")
            self.assertEqual(mock_req.call_args_list[1][0][0], "DELETE")
            self.assertTrue(mock_req.call_args_list[1][0][1].endswith("/messages/1573111111111111111"))


if __name__ == "__main__":
    unittest.main()
