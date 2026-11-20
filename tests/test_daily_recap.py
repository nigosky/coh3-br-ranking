"""Testes unitários do Boletim Diário dos Veteranos (06:00 BRT), Jogo do Dia, cohdb e renderização PNG."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from daily_recap import (
    CohdbReplayClient,
    annotate_unified_top5_impacts,
    build_daily_recap,
    build_unified_ranking_context,
    export_daily_recap_json,
    fetch_global_top_players,
    format_map_name,
    get_brt_day_window,
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
) -> dict:
    counters = {"dmgdone": dmg, "vkill": vkill, "sqkill": sqkill, "gt": gt}
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
        self.assertEqual(m["allies_team"][0]["canonical_pid"], 559962)
        self.assertEqual(m["allies_team"][0]["alias"], "É o Gabe")
        self.assertEqual(m["allies_team"][0]["dmgdone"], 40000)
        self.assertEqual(m["allies_team"][0]["elo_delta"], 18)

    def test_match_of_the_day_rewards_top20_tight_vp_and_long_duration(self) -> None:
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
        # Partida 2: batalha de 49 min contra Top #9 global (>1800 ELO) decidida em 14 x 0 VP + upset
        epic_match = {
            "id": 2002,
            "matchtype_id": 1,
            "mapname": "pachino_2p",
            "startgametime": ts + 3000 - 2940,
            "completiontime": ts + 3000,
            "matchhistoryreportresults": [
                {
                    "profile_id": 639157, "teamid": 0, "race_id": 197345, "resulttype": 1,
                    "counters": '{"dmgdone": 94000, "vkill": 8, "sqkill": 15, "gt": 2940, "vp0": 14, "vp1": 0}',
                    "profile": {"name": "/steam/76561197979883434", "alias": "Itaperuna", "country": "br"},
                    "matchhistorymember": {"outcome": 1, "oldrating": 1614, "newrating": 1637},
                },
                {
                    "profile_id": 900200, "teamid": 1, "race_id": 198437, "resulttype": 0,
                    "counters": '{"dmgdone": 91000, "vkill": 7, "sqkill": 13, "gt": 2940, "vp0": 14, "vp1": 0}',
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
        self.assertEqual(motd["selection_tier"], 4)
        self.assertGreaterEqual(motd["epicness_score"], 85)
        self.assertTrue(any("TOP #9" in b for b in motd["badges"]))
        self.assertTrue(any("14 x 0 VP" in b for b in motd["badges"]))

        short_scored = score_match_of_the_day(matches[0], top_global_by_pid=top_global)
        self.assertEqual(short_scored["selection_tier"], 2)
        self.assertLess(short_scored["epicness_score"], 30)

        # Verifica exportação atômica JSON
        with tempfile.TemporaryDirectory() as td:
            out_json = Path(td) / "boletim_diario_br.json"
            export_daily_recap_json(recap, out_json)
            loaded = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(loaded["summary"]["total_unique_matches"], 2)
            self.assertEqual(loaded["awards"]["mvp"]["alias"], "Itaperuna")

    def test_top_global_requires_rank_le_20_and_elo_gt_1800(self) -> None:
        self.assertTrue(is_top_global_entry({"rank": 1, "elo": 2150}))
        self.assertTrue(is_top_global_entry({"rank": 20, "elo": 1801}))
        self.assertFalse(is_top_global_entry({"rank": 20, "elo": 1800}))  # exatamente 1800 NÃO é acima de 1800
        self.assertFalse(is_top_global_entry({"rank": 12, "elo": 1760}))  # Top 20 mas <= 1800 ELO
        self.assertFalse(is_top_global_entry({"rank": 21, "elo": 2050}))  # >1800 ELO mas fora do Top 20
        self.assertFalse(is_top_global_entry(None))

        # fetch_global_top_players deve filtrar no ato da consulta à Relic
        fake_payload = {
            "statGroups": [
                {"id": 1, "members": [{"profile_id": 111, "alias": "TrueTop"}]},
                {"id": 2, "members": [{"profile_id": 222, "alias": "LowEloTop"}]},
                {"id": 3, "members": [{"profile_id": 333, "alias": "Boundary1800"}]},
                {"id": 4, "members": [{"profile_id": 444, "alias": "Rank25HighElo"}]},
            ],
            "leaderboardStats": [
                {"statgroup_id": 1, "rank": 5, "rating": 1950},
                {"statgroup_id": 2, "rank": 12, "rating": 1760},
                {"statgroup_id": 3, "rank": 18, "rating": 1800},
                {"statgroup_id": 4, "rank": 25, "rating": 2050},
            ],
        }

        class _FakeRelic:
            def _get_json(self, _url: str) -> dict:
                return fake_payload

        top_map = fetch_global_top_players(_FakeRelic(), top_count=20)
        self.assertEqual(set(top_map.keys()), {111})
        self.assertEqual(top_map[111]["rank"], 5)
        self.assertEqual(top_map[111]["elo"], 1950)

        # Mesmo que um dicionário não filtrado chegue a score_match_of_the_day, rank<=20 com ELO<=1800 é ignorado
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 20000
        raw_match = {
            "id": 2101,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts - 2200,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, False, "Chacineiro", "br", 1560, 1548, 18000, 2200, vp0=0, vp1=35),
                _make_report(910010, 1, 137123, True, "FakeTop15", "de", 1750, 1762, 18500, 2200, vp0=0, vp1=35),
            ],
        }
        norm = merge_and_filter_day_matches([raw_match], start_ts, end_ts)[0]
        unfiltered_low_elo = {910010: {"profile_id": 910010, "rank": 15, "mode": "1v1", "elo": 1750}}
        scored_low = score_match_of_the_day(norm, top_global_by_pid=unfiltered_low_elo)
        self.assertIsNone(scored_low["best_opp_rank"])
        self.assertFalse(scored_low["is_qualified_epic_loss"])
        self.assertFalse(scored_low["axis_team"][0]["is_top_global"])
        self.assertFalse(any("TOP #" in b for b in scored_low["badges"]))

        valid_high_elo = {910010: {"profile_id": 910010, "rank": 15, "mode": "1v1", "elo": 1825}}
        scored_high = score_match_of_the_day(norm, top_global_by_pid=valid_high_elo)
        self.assertEqual(scored_high["best_opp_rank"], 15)
        self.assertTrue(scored_high["is_qualified_epic_loss"])
        self.assertEqual(scored_high["selection_tier"], 3)
        self.assertTrue(scored_high["axis_team"][0]["is_top_global"])
        self.assertTrue(any("VS TOP #15 GLOBAL" in b for b in scored_high["badges"]))

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
        # B: Derrota épica qualificada em 1v1 (42 min, disputada 25x0 VP, contra Top #7 >1800) -> Tier 3
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
        # C: Vitória em 4v4 isolado (1 Veterano + 3 gringos vs 4 gringos, 45 min, 15x0 VP) -> Tier 2 (última prioridade)
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
        ]
        # Catálogo com segunda facção inferior para Itaperuna (2v2 dak 1400 vs 1v1 german 1600)
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

    def test_unified_top5_rank_swap_and_sp_impact_drive_match_of_the_day(self) -> None:
        _, start_ts, end_ts = get_brt_day_window("2026-11-19")
        ts = start_ts + 35000
        ctx = self._sample_unified_context()

        # Partida 1: Vitória longa comum de jogador fora do Top 5 (Dragoness, 34m, 2v2, pareada)
        routine_win = {
            "id": 2401,
            "matchtype_id": 2,
            "mapname": "rails_and_sand_4p",
            "startgametime": ts - 2040,
            "completiontime": ts,
            "matchhistoryreportresults": [
                _make_report(6557, 0, 129494, True, "Dragoness", "br", 1330, 1344, 15000, 2040),
                _make_report(940101, 0, 197345, True, "Ally", "us", 1250, 1264, 14500, 2040),
                _make_report(940102, 1, 137123, False, "Axis1", "de", 1310, 1296, 14200, 2040),
                _make_report(940103, 1, 198437, False, "Axis2", "fr", 1270, 1256, 14000, 2040),
            ],
        }
        # Partida 2: #3 Chacineiro vence 1v1 USF (1566 -> 1580 => 2132 -> 2160 SP) e ULTRAPASSA #2 hjax (2140 SP)!
        top5_climb_win = {
            "id": 2402,
            "matchtype_id": 1,
            "mapname": "semois_2p",
            "startgametime": ts + 100 - 1300,
            "completiontime": ts + 100,
            "matchhistoryreportresults": [
                _make_report(79327, 0, 129494, True, "Chacineiro", "br", 1566, 1580, 11000, 1300),
                _make_report(940201, 1, 137123, False, "Opp1v1", "de", 1580, 1566, 9500, 1300),
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

        m_routine, m_climb, m_off = merge_and_filter_day_matches(
            [routine_win, top5_climb_win, off_faction_match], start_ts, end_ts
        )
        impacts = annotate_unified_top5_impacts([m_routine, m_climb, m_off], ctx)
        self.assertNotIn(2401, impacts)
        self.assertNotIn(2403, impacts)  # facção secundária não altera M_1v1 nem Skill Points!
        self.assertIn(2402, impacts)
        self.assertTrue(impacts[2402]["affects_top5_rank"])
        self.assertEqual(impacts[2402]["badge"], "TOP 5 BR (#3→#2)")
        self.assertIn("#3 → #2", impacts[2402]["summary"])

        # build_daily_recap deve eleger a partida que mexeu no Top 5 mesmo sendo mais curta que a vitória rotineira
        fake_cohdb = CohdbReplayClient()
        with patch.object(fake_cohdb, "inspect_match", return_value={"verified": True, "url": "https://cohdb.com/matches/2402", "vp_pair": None, "share_pair": None}):
            recap = build_daily_recap(
                [m_routine, m_climb, m_off],
                "2026-11-19",
                cohdb_client=fake_cohdb,
                unified_ranking_context=ctx,
            )

        motd = recap["match_of_the_day"]
        self.assertEqual(motd["match_id"], 2402)
        self.assertEqual(motd["selection_tier"], 4)
        self.assertTrue(motd["affects_top5_rank"])
        self.assertIn("TOP 5 BR (#3→#2)", motd["badges"])
        self.assertTrue(motd["cohdb_verified"])  # confirma que re-score do cohdb preservou o impacto Top 5!

        # Embed do Discord deve exibir linha dedicada de impacto no Top 5 Unificado
        payload = build_daily_recap_payload(recap, ["boletim_diario_p1.png", "jogo_do_dia.png"])
        self.assertIn("Top 5 Unificado", payload["embeds"][1]["description"])
        self.assertIn("Chacineiro", payload["embeds"][1]["description"])

    def test_unified_top5_entry_promotes_4v4_and_handles_rank1_and_contested_drop(self) -> None:
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

        m_enter, m_drop = merge_and_filter_day_matches([enter_top5_4v4, drop_top5_loss], start_ts, end_ts)
        impacts = annotate_unified_top5_impacts([m_enter, m_drop], ctx)

        s_enter = score_match_of_the_day(m_enter, top5_impact=impacts.get(2501))
        self.assertTrue(s_enter["affects_top5_rank"])
        self.assertEqual(s_enter["selection_tier"], 4)  # 4v4 promovido a Tier 4 por mexer no Top 5!
        self.assertIn("ENTROU NO TOP 5 (#5)", s_enter["badges"])

        s_drop = score_match_of_the_day(m_drop, top5_impact=impacts.get(2502))
        self.assertTrue(s_drop["affects_top5_rank"])
        self.assertEqual(s_drop["selection_tier"], 3)  # derrota disputada que mexeu no Top 5 -> Tier 3 (abaixo de vitórias)
        self.assertIn("MEXEU NO TOP 5 (#2→#3)", s_drop["badges"])

        # Valida também carregamento real do ranking_unificado_br.json do repositório (sem erro)
        repo_root = Path(__file__).resolve().parent.parent
        loaded_ctx = load_unified_ranking_context(
            repo_root / "ranking_unificado_br.json",
            repo_root / "jogadores_br_steam.json",
        )
        self.assertGreaterEqual(len(loaded_ctx["players"]), 5)

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
        top_global = {910001: {"profile_id": 910001, "rank": 15, "mode": "1v1", "faction": "german", "elo": 1850}}
        return build_daily_recap(matches, "2026-11-19", top_global_by_pid=top_global)

    def test_daily_recap_and_motd_png_render_cleanly(self) -> None:
        recap = self._sample_recap()
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

        # Renderização direta do Jogo do Dia
        motd_png = render_match_of_the_day_png(recap["match_of_the_day"], "2026-11-19", self.ap)
        self.assertTrue(motd_png.startswith(b"\x89PNG"))

    def test_daily_recap_discord_payload_and_post_method(self) -> None:
        recap = self._sample_recap()
        payload = build_daily_recap_payload(recap, ["boletim_diario_p1.png", "jogo_do_dia.png"])
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertEqual(len(payload["embeds"]), 2)
        self.assertIn("https://coh3stats.com/matches/3001", payload["embeds"][1]["description"])
        self.assertIn("https://cohdb.com/matches", payload["embeds"][1]["description"])

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


if __name__ == "__main__":
    unittest.main()
