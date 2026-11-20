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
    build_daily_recap,
    export_daily_recap_json,
    format_map_name,
    get_brt_day_window,
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
        self.assertGreaterEqual(motd["epicness_score"], 85)
        self.assertTrue(any("TOP #9" in b for b in motd["badges"]))
        self.assertTrue(any("14 x 0 VP" in b for b in motd["badges"]))

        short_scored = score_match_of_the_day(matches[0], top_global_by_pid=top_global)
        self.assertLess(short_scored["epicness_score"], 30)

        # Verifica exportação atômica JSON
        with tempfile.TemporaryDirectory() as td:
            out_json = Path(td) / "boletim_diario_br.json"
            export_daily_recap_json(recap, out_json)
            loaded = json.loads(out_json.read_text(encoding="utf-8"))
            self.assertEqual(loaded["summary"]["total_unique_matches"], 2)
            self.assertEqual(loaded["awards"]["mvp"]["alias"], "Itaperuna")

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
                        "matchhistorymember": {"outcome": 0, "oldrating": 1790, "newrating": 1770},
                    },
                ],
            }
        ]
        matches = merge_and_filter_day_matches(raw, start_ts, start_ts + 86399)
        top_global = {910001: {"profile_id": 910001, "rank": 15, "mode": "1v1", "faction": "german", "elo": 1790}}
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
