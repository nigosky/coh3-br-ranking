"""Testes unitários de renderização PNG (Pillow), cores de destaque e payloads/PATCH do Discord."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from PIL import Image

from discord_ranking import (
    DiscordRankingClient,
    _encode_multipart_formdata,
    build_arranged_teams_panel_payload,
    build_ranking_panel_payload,
    render_arranged_teams_attachments,
    render_ranking_attachments,
)
from ranking_renderer import (
    SteamAvatarProvider,
    render_arranged_teams_table_png,
    render_unified_ranking_table_png,
)


class TestPngRenderingAndVisualColors(unittest.TestCase):
    def setUp(self) -> None:
        self.ap = SteamAvatarProvider(db=None, allow_network=False)

    def test_unified_ranking_png_distinguishes_active_inactive_and_anchor_pill(self) -> None:
        entries = [
            {
                "profile_id": 639157,
                "steam_id": "76561197979883434",
                "alias": "Itaperuna",
                "country": "br",
                "br_rank": 1,
                "skill_points": 2523.3,
                "anchor_mode": "1v1",
                "mode_max_elos": {"1v1": 1614, "2v2": 1613, "3v3": 1562, "4v4": 1487},
                "wins": 2117,
                "losses": 1333,
                "total": 3450,
                "ratio": 61,
                "last_match_date": 1795000000,
                "is_custom": True,
                "inactive_fallback": False,
            },
            {
                "profile_id": 28937,
                "steam_id": "76561198089948995",
                "alias": "Mjr Bruno \u202e中文",
                "country": "br",
                "br_rank": 2,
                "skill_points": 1481.7,
                "anchor_mode": "2v2",
                "mode_max_elos": {"1v1": 1061, "2v2": 1276, "3v3": 1200, "4v4": 1129},
                "wins": 254,
                "losses": 208,
                "total": 462,
                "ratio": 55,
                "last_match_date": 1785582458,
                "is_custom": True,
                "inactive_fallback": True,
            },
        ]
        png_bytes = render_unified_ranking_table_png(entries, avatar_provider=self.ap)
        with Image.open(io.BytesIO(png_bytes), formats=["PNG"]) as im:
            self.assertEqual(im.size, (1040, 38 + 34 + 2 * 36 + 4))
            # Amostra de pixel de fundo na linha ativa (idx=0, y=72..108) vs inativa (idx=1, y=108..144)
            active_px = im.getpixel((450, 90))
            inactive_px = im.getpixel((450, 126))
            accent_px = im.getpixel((2, 126))

            self.assertNotEqual(active_px, inactive_px, "Jogadores inativos devem ter cor de fundo diferente dos ativos")
            self.assertEqual(active_px, (17, 19, 24))
            self.assertEqual(inactive_px, (48, 28, 35))
            self.assertEqual(accent_px, (175, 72, 84))

            # Verifica pill dourada no modo âncora (linha 0 -> 1v1 cx=508, borda superior em y=78; 2v2 cx=576 sem pill)
            anchor_pill_bg = im.getpixel((484, 82))
            non_anchor_bg = im.getpixel((552, 82))
            self.assertEqual(anchor_pill_bg, (58, 45, 18))
            self.assertEqual(non_anchor_bg, (17, 19, 24))

    def test_unified_ranking_empty_state_renders(self) -> None:
        png_bytes = render_unified_ranking_table_png([], avatar_provider=self.ap)
        with Image.open(io.BytesIO(png_bytes), formats=["PNG"]) as im:
            self.assertEqual(im.width, 1040)
            self.assertGreater(im.height, 72)

    def test_arranged_teams_png_highlights_veteranos_vs_other_br(self) -> None:
        teams = [
            {
                "pair_key": "1390350_9999991",
                "br_rank": 1,
                "avg_elo": 1410.0,
                "axis_elo": 1450,
                "allies_elo": 1370,
                "sides": ["axis", "allies"],
                "wins": 20,
                "losses": 10,
                "total": 30,
                "ratio": 67,
                "streak": 4,
                "last_match_date": 1795000000,
                "has_veterano": False,
                "veteranos_count": 0,
                "players": [
                    {"profile_id": 1390350, "steam_id": "", "alias": "OutroBR1", "country": "br", "is_veterano": False},
                    {"profile_id": 9999991, "steam_id": "", "alias": "OutroBR2", "country": "br", "is_veterano": False},
                ],
            },
            {
                "pair_key": "26910_366826",
                "br_rank": 2,
                "avg_elo": 1380.0,
                "axis_elo": 1380,
                "allies_elo": None,
                "sides": ["axis"],
                "wins": 12,
                "losses": 4,
                "total": 16,
                "ratio": 75,
                "streak": -1,
                "last_match_date": 1795000000,
                "has_veterano": True,
                "veteranos_count": 2,
                "inactive_fallback": False,
                "players": [
                    {"profile_id": 366826, "steam_id": "76561198014985380", "alias": "Lem22", "country": "us", "is_veterano": True},
                    {"profile_id": 26910, "steam_id": "76561197963741792", "alias": "nigo_BR ᵀᴹ", "country": "br", "is_veterano": True},
                ],
            },
        ]
        png_bytes = render_arranged_teams_table_png(teams, avatar_provider=self.ap)
        with Image.open(io.BytesIO(png_bytes), formats=["PNG"]) as im:
            self.assertEqual(im.size, (980, 36 + 34 + 2 * 54 + 4))
            other_br_px = im.getpixel((400, 96))
            vet_team_px = im.getpixel((400, 150))
            vet_accent_px = im.getpixel((2, 150))

            self.assertNotEqual(other_br_px, vet_team_px)
            self.assertEqual(other_br_px, (17, 19, 24))
            self.assertEqual(vet_team_px, (42, 35, 22))
            self.assertEqual(vet_accent_px, (245, 184, 46))

    def test_smurf_visual_highlight_in_unified_and_arranged_pngs(self) -> None:
        unified_entries = [
            {
                "profile_id": 1390350,
                "steam_id": "76561198089278659",
                "alias": "TearsInTheRain (Chacineiro)",
                "country": "br",
                "br_rank": 5,
                "skill_points": 2206.4,
                "anchor_mode": "1v1",
                "mode_max_elos": {"1v1": 1605},
                "wins": 90,
                "losses": 35,
                "total": 125,
                "ratio": 72,
                "last_match_date": 1795000000,
                "is_custom": True,
                "inactive_fallback": False,
                "is_smurf": True,
                "smurf_of": "Chacineiro",
            },
        ]
        png_u = render_unified_ranking_table_png(unified_entries, avatar_provider=self.ap)
        with Image.open(io.BytesIO(png_u), formats=["PNG"]) as im:
            self.assertEqual(im.size, (1040, 38 + 34 + 36 + 4))
            # Anel ciano no avatar do Smurf (avatar 30x30 colado em x=166, y=75 -> borda esquerda em x=167, y=90)
            ring_px = im.getpixel((167, 90))
            self.assertGreater(ring_px[2], 200, f"Esperado anel ciano no avatar Smurf, obtido {ring_px}")
            self.assertGreater(ring_px[1], 140)

        arranged_teams = [
            {
                "pair_key": "16867_1333418",
                "br_rank": 1,
                "avg_elo": 1401.0,
                "axis_elo": 1401,
                "allies_elo": None,
                "sides": ["axis"],
                "wins": 29,
                "losses": 12,
                "total": 41,
                "ratio": 71,
                "streak": 2,
                "last_match_date": 1795000000,
                "has_veterano": True,
                "has_smurf": True,
                "veteranos_count": 1,
                "inactive_fallback": False,
                "players": [
                    {
                        "profile_id": 1333418,
                        "steam_id": "76561198698969876",
                        "alias": "Charles Bronson (Chacineiro)",
                        "country": "br",
                        "is_veterano": True,
                        "is_smurf": True,
                        "smurf_of": "Chacineiro",
                    },
                    {
                        "profile_id": 16867,
                        "steam_id": "76561198874381986",
                        "alias": "Golden Cobra",
                        "country": "br",
                        "is_veterano": False,
                        "is_smurf": False,
                        "smurf_of": "",
                    },
                ],
            }
        ]
        png_a = render_arranged_teams_table_png(arranged_teams, avatar_provider=self.ap)
        with Image.open(io.BytesIO(png_a), formats=["PNG"]) as im:
            self.assertEqual(im.size, (980, 36 + 34 + 54 + 4))


class TestDiscordPayloadsAndPatchSafety(unittest.TestCase):
    def setUp(self) -> None:
        self.ap = SteamAvatarProvider(db=None, allow_network=False)

    def test_render_ranking_attachments_excludes_non_custom_paginates_and_builds_safe_payload(self) -> None:
        entries = [
            {"profile_id": 9001, "alias": "OutroBR", "br_rank": 1, "skill_points": 2800.0, "anchor_mode": "1v1", "mode_max_elos": {"1v1": 1900}, "wins": 50, "losses": 5, "is_custom": False},
            {"profile_id": 79327, "alias": "Chacineiro", "br_rank": 2, "skill_points": 2435.2, "anchor_mode": "1v1", "mode_max_elos": {"1v1": 1566}, "wins": 40, "losses": 20, "is_custom": True, "inactive_fallback": False},
        ]
        atts = render_ranking_attachments(entries, avatar_provider=self.ap, unique_roster_count=43)
        self.assertEqual([a[0] for a in atts], ["ranking_unificado_p1.png"])

        # Paginação automática com 30 Veteranos -> 2 páginas balanceadas (15 + 15)
        many = [
            {"profile_id": 1000 + i, "alias": f"Vet{i}", "br_rank": i + 1, "skill_points": 2000.0 - i, "anchor_mode": "2v2", "mode_max_elos": {"2v2": 1400}, "wins": 20, "losses": 10, "is_custom": True, "inactive_fallback": False}
            for i in range(30)
        ]
        many_atts = render_ranking_attachments(many, avatar_provider=self.ap, unique_roster_count=43)
        self.assertEqual([a[0] for a in many_atts], ["ranking_unificado_p1.png", "ranking_unificado_p2.png"])

        payload = build_ranking_panel_payload(
            entries,
            custom_players=[
                {"profile_id": 79327, "alias": "Chacineiro"},
                {"profile_id": 99999, "alias": "@everyone [spam](https://evil.test)"},
            ],
            attachment_names=[a[0] for a in atts],
        )
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        desc = payload["embeds"][0]["description"]
        self.assertIn("Skill Points", desc)
        self.assertIn("lançamento do CoH3", desc)
        self.assertNotIn("Top 20", desc)
        self.assertNotIn("@everyone", desc)
        self.assertNotIn("[spam]", desc)

    def test_discord_panels_announce_smurf_original_owners(self) -> None:
        unified_entries = [
            {
                "profile_id": 79327,
                "alias": "Chacineiro",
                "br_rank": 2,
                "skill_points": 2435.2,
                "anchor_mode": "1v1",
                "mode_max_elos": {"1v1": 1566},
                "wins": 40,
                "losses": 20,
                "is_custom": True,
                "inactive_fallback": False,
                "is_smurf": False,
                "smurf_of": "",
            },
            {
                "profile_id": 1390350,
                "alias": "TearsInTheRain (Chacineiro)",
                "br_rank": 5,
                "skill_points": 2206.4,
                "anchor_mode": "1v1",
                "mode_max_elos": {"1v1": 1605},
                "wins": 90,
                "losses": 35,
                "is_custom": True,
                "inactive_fallback": False,
                "is_smurf": True,
                "smurf_of": "Chacineiro",
            },
            {
                "profile_id": 1423426,
                "alias": "geladeira velha (arc)",
                "br_rank": 21,
                "skill_points": 1462.0,
                "anchor_mode": "2v2",
                "mode_max_elos": {"2v2": 1398},
                "wins": 12,
                "losses": 4,
                "is_custom": True,
                "inactive_fallback": False,
                "is_smurf": True,
                "smurf_of": "arc",
            },
        ]
        custom_roster = [
            {"profile_id": 79327, "original_alias": "Chacineiro"},
            {"profile_id": 1390350, "original_alias": "TearsInTheRain (Chacineiro)", "pin_alias": True, "smurf_of": "Chacineiro"},
            {"profile_id": 1423426, "original_alias": "geladeira velha (arc)", "pin_alias": True, "smurf_of": "arc"},
        ]
        payload_u = build_ranking_panel_payload(
            unified_entries,
            custom_players=custom_roster,
            attachment_names=["ranking_unificado_p1.png"],
        )
        desc_u = payload_u["embeds"][0]["description"]
        self.assertIn("3 contas de Veteranos (1 titular + 2 Smurfs)", desc_u)
        self.assertIn("Contas Smurf neste ranking (sem merge):", desc_u)
        self.assertIn("`#5` **TearsInTheRain (Chacineiro)** → conta original: **Chacineiro**", desc_u)
        self.assertIn("`#21` **geladeira velha (arc)** → conta original: **arc**", desc_u)
        self.assertNotIn("mescladas", desc_u)
        self.assertIn("• 2 Smurfs", payload_u["embeds"][-1]["footer"]["text"])

        arranged_teams = [
            {
                "br_rank": 1,
                "avg_elo": 1401.0,
                "sides": ["axis"],
                "wins": 29,
                "losses": 12,
                "total": 41,
                "ratio": 71,
                "streak": 2,
                "last_match_date": 1795000000,
                "has_veterano": True,
                "has_smurf": True,
                "inactive_fallback": False,
                "players": [
                    {"profile_id": 1333418, "alias": "Charles Bronson (Chacineiro)", "country": "br", "is_veterano": True, "is_smurf": True, "smurf_of": "Chacineiro"},
                    {"profile_id": 16867, "alias": "Golden Cobra", "country": "br", "is_veterano": False, "is_smurf": False, "smurf_of": ""},
                ],
            },
            {
                "br_rank": 4,
                "avg_elo": 1340.0,
                "sides": ["allies"],
                "wins": 15,
                "losses": 5,
                "total": 20,
                "ratio": 75,
                "streak": 3,
                "last_match_date": 1795000000,
                "has_veterano": False,
                "has_smurf": True,
                "inactive_fallback": False,
                "players": [
                    {"profile_id": 1297853, "alias": "Shadow (Smurf: Alekel)", "country": "br", "is_veterano": False, "is_smurf": True, "smurf_of": "Alekel"},
                    {"profile_id": 212034, "alias": "XAPA KENTE", "country": "ca", "is_veterano": False, "is_smurf": False, "smurf_of": ""},
                ],
            },
        ]
        payload_a = build_arranged_teams_panel_payload(
            arranged_teams,
            ["arranged_2v2_p1.png"],
            inactive_days=30,
            min_total_games=10,
            max_teams=32,
        )
        desc_a = payload_a["embeds"][0]["description"]
        self.assertIn("Contas Smurf no Top 32 (sem merge):", desc_a)
        self.assertIn("`#1` **Charles Bronson (Chacineiro)** *(com Golden Cobra)* → conta original: **Chacineiro**", desc_a)
        self.assertIn("`#4` **Shadow (Smurf: Alekel)** *(com XAPA KENTE)* → conta original: **Alekel**", desc_a)
        # Jogador regular sem is_smurf não dispara falso positivo
        self.assertNotIn("conta original: **BR**", desc_a)
        self.assertIn("• 2 c/ Smurf", payload_a["embeds"][-1]["footer"]["text"])
        self.assertIn("BR EXT = brasileiro no exterior", payload_a["embeds"][-1]["footer"]["text"])

        # Verifica pílula verde-e-ouro BR EXT desenhada ao lado da bandeira dos EUA (Lem22) no Ranking Unificado
        expat_entries = [
            {
                "profile_id": 366826,
                "steam_id": "76561198014985380",
                "alias": "Lem22",
                "country": "us",
                "br_rank": 1,
                "skill_points": 2100.0,
                "anchor_mode": "2v2",
                "mode_max_elos": {"2v2": 1500},
                "wins": 40,
                "losses": 20,
                "total": 60,
                "ratio": 67,
                "last_match_date": 1795000000,
                "is_custom": True,
                "inactive_fallback": False,
            }
        ]
        png_expat = render_unified_ranking_table_png(expat_entries, avatar_provider=self.ap)
        with Image.open(io.BytesIO(png_expat), formats=["PNG"]) as im_ex:
            # Bandeira em x=202..223; pílula BR EXT em x=227..269, y=83..98 (borda dourada em y=83, fundo verde escuro dentro)
            pill_border_px = im_ex.getpixel((240, 83))
            self.assertGreater(pill_border_px[0], 180, f"Esperada borda dourada da pílula BR EXT, obtido {pill_border_px}")
            self.assertGreater(pill_border_px[1], 140)
        expat_payload = build_ranking_panel_payload(
            expat_entries,
            custom_players=[{"profile_id": 366826, "original_alias": "Lem22", "country": "us"}],
            attachment_names=["ranking_unificado_p1.png"],
        )
        self.assertIn("BR EXT = brasileiro no exterior", expat_payload["embeds"][-1]["footer"]["text"])

    def test_arranged_attachments_pagination_and_empty_state(self) -> None:
        empty_atts = render_arranged_teams_attachments([], avatar_provider=self.ap, inactive_days=30, min_total_games=10)
        self.assertEqual(len(empty_atts), 1)
        payload = build_arranged_teams_panel_payload([], [empty_atts[0][0]], inactive_days=30, min_total_games=10)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("TOP 32", payload["embeds"][0]["title"])
        desc = payload["embeds"][0]["description"]
        self.assertIn("COH3 Stats Open Data", desc)
        self.assertIn("+10 jogos no geral", desc)
        self.assertIn("calibragem de ELO", desc)
        self.assertIn("Top 32", desc)
        self.assertNotIn("vinho", desc)

        # 40 duplas ativas (inclusive Veteranas abaixo do #32) + 2 inativas -> deve renderizar EXATAMENTE 32 (2 páginas de 16)
        many_teams = [
            {
                "br_rank": i + 1,
                "avg_elo": 1600.0 - i * 5.0,
                "sides": ["axis"],
                "wins": 12,
                "losses": 4,
                "total": 16,
                "ratio": 75,
                "streak": 2,
                "last_match_date": 1795000000,
                "has_veterano": (i % 2 == 0 or i >= 32),
                "inactive_fallback": False,
                "players": [
                    {"profile_id": 1000 + i * 2, "alias": f"P{i}A" + ("*" if i == 0 else ""), "country": "br", "is_veterano": i % 2 == 0},
                    {"profile_id": 1001 + i * 2, "alias": f"P{i}B", "country": "br", "is_veterano": False},
                ],
            }
            for i in range(40)
        ]
        many_teams.append(
            {
                "br_rank": 41,
                "avg_elo": 1750.0,
                "sides": ["axis"],
                "wins": 20,
                "losses": 2,
                "total": 22,
                "ratio": 91,
                "streak": 5,
                "last_match_date": 1790000000,
                "has_veterano": True,
                "inactive_fallback": True,
                "players": [
                    {"profile_id": 2001, "alias": "arc", "country": "br", "is_veterano": True},
                    {"profile_id": 2002, "alias": "Gabe", "country": "br", "is_veterano": True},
                ],
            }
        )
        many_atts = render_arranged_teams_attachments(many_teams, avatar_provider=self.ap, inactive_days=30, min_total_games=10)
        self.assertEqual([a[0] for a in many_atts], ["arranged_2v2_p1.png", "arranged_2v2_p2.png"])
        for _, png_b, _ in many_atts:
            with Image.open(io.BytesIO(png_b), formats=["PNG"]) as im:
                # 16 linhas por página (32 / 2 = 16) -> altura exata 36 + 34 + 16*54 + 4 = 938px
                self.assertEqual(im.size, (980, 36 + 34 + 16 * 54 + 4))

        pop_payload = build_arranged_teams_panel_payload(
            many_teams,
            [a[0] for a in many_atts],
            inactive_days=30,
            min_total_games=10,
            max_teams=32,
        )
        footer_txt = pop_payload["embeds"][-1]["footer"]["text"]
        self.assertIn("Top 32", footer_txt)
        self.assertIn("32 duplas (16 c/ Veteranos)", footer_txt)
        self.assertNotIn("inativas", footer_txt)
        self.assertIn("* = mudou de nick", footer_txt)

    def test_upsert_patch_clears_old_attachments_and_falls_back_on_404(self) -> None:
        valid_wh = "https://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz1234"
        client = DiscordRankingClient(webhook_url=valid_wh)
        files = [("test.png", b"\x89PNG\r\n\x1a\n", "image/png")]
        payload = {"embeds": [{"title": "Test"}]}

        with patch.object(client, "_request", return_value=(200, {"id": "1573027241493921948"})) as mock_req:
            msg_id = client._upsert_message(
                payload, files, existing_message_id="1573027241493921948", replace_existing=False
            )
            self.assertEqual(msg_id, "1573027241493921948")
            method, url, sent_payload = mock_req.call_args[0]
            self.assertEqual(method, "PATCH")
            self.assertTrue(url.endswith("/messages/1573027241493921948"))
            self.assertEqual(sent_payload.get("attachments"), [])

        # Se PATCH retorna 404, deve publicar via POST com wait=true
        with patch.object(
            client,
            "_request",
            side_effect=[(404, None), (200, {"id": "1573099999999999999"})],
        ) as mock_req:
            msg_id = client._upsert_message(
                payload, files, existing_message_id="1573027241493921948", replace_existing=False
            )
            self.assertEqual(msg_id, "1573099999999999999")
            self.assertEqual(mock_req.call_args_list[0][0][0], "PATCH")
            self.assertEqual(mock_req.call_args_list[1][0][0], "POST")
            self.assertIn("wait=true", mock_req.call_args_list[1][0][1])

    def test_replace_message_posts_new_then_deletes_old_and_retries_pending_deletes(self) -> None:
        import tempfile
        from pathlib import Path
        from avatar_cache import RankingCacheDB

        valid_wh = "https://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz1234"
        files = [("test.png", b"\x89PNG\r\n\x1a\n", "image/png")]
        payload = {"embeds": [{"title": "Novo Ranking"}]}

        with tempfile.TemporaryDirectory() as td:
            with RankingCacheDB(Path(td) / "cache.db") as db:
                client = DiscordRankingClient(webhook_url=valid_wh, db=db)

                # Padrão (replace_existing=True): POST novo primeiro, depois DELETE no antigo
                with patch.object(
                    client,
                    "_request",
                    side_effect=[(200, {"id": "1573999999999999999"}), (204, None)],
                ) as mock_req:
                    new_id = client._upsert_message(payload, files, existing_message_id="1573027241493921948")
                    self.assertEqual(new_id, "1573999999999999999")
                    self.assertEqual(mock_req.call_args_list[0][0][0], "POST")
                    self.assertIn("wait=true", mock_req.call_args_list[0][0][1])
                    self.assertEqual(mock_req.call_args_list[1][0][0], "DELETE")
                    self.assertTrue(mock_req.call_args_list[1][0][1].endswith("/messages/1573027241493921948"))

                # Se DELETE falhar transitoriamente, enfileira no SQLite e limpa no próximo ciclo sem tocar em keep_ids
                with patch.object(
                    client,
                    "_request",
                    side_effect=[(200, {"id": "1574000000000000001"}), RuntimeError("timeout")],
                ):
                    new_id2 = client._upsert_message(payload, files, existing_message_id="1573999999999999999")
                    self.assertEqual(new_id2, "1574000000000000001")
                    self.assertEqual(db.get_state("pending_delete_ranking_ids"), "1573999999999999999")

                with patch.object(client, "_request", return_value=(204, None)) as mock_del:
                    cleaned = client.cleanup_pending_deletes(
                        state_key="pending_delete_ranking_ids",
                        extra_ids=["1573367688217235568", "1574000000000000001"],
                        keep_ids={"1574000000000000001"},
                    )
                    self.assertEqual(cleaned, 2)
                    called_urls = [c[0][1] for c in mock_del.call_args_list]
                    self.assertTrue(any(u.endswith("/messages/1573999999999999999") for u in called_urls))
                    self.assertTrue(any(u.endswith("/messages/1573367688217235568") for u in called_urls))
                    self.assertFalse(any(u.endswith("/messages/1574000000000000001") for u in called_urls))
                    self.assertEqual(db.get_state("pending_delete_ranking_ids"), "")

                # HTTP 403 (mensagem de outro webhook/usuário) não deve ficar retentando em loop infinito
                with patch.object(client, "_request", return_value=(403, None)):
                    cleaned_403 = client.cleanup_pending_deletes(
                        state_key="pending_delete_ranking_ids",
                        extra_ids=["1573000000000000123"],
                    )
                    self.assertEqual(cleaned_403, 0)
                    self.assertEqual(db.get_state("pending_delete_ranking_ids"), "")

    def test_multipart_sanitizes_filename(self) -> None:
        body, ctype = _encode_multipart_formdata({"a": 1}, [("../evil\r\n.png", b"PNG", "image/png")])
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        self.assertNotIn(b'filename="../evil', body)
        self.assertNotIn(b"\r\n.png", body)

    def test_steam_web_api_batch_avatar_prefetch_and_fallback(self) -> None:
        import json
        import ranking_renderer as rr

        # Chave inválida ou vazia é ignorada com segurança (fallback para XML)
        ap_invalid = SteamAvatarProvider(allow_network=True, steam_api_key="curta!")
        self.assertFalse(ap_invalid.has_steam_api_key)
        self.assertEqual(
            ap_invalid.prefetch_avatar_urls([{"profile_id": 79327, "steam_id": "76561197963172584"}]),
            0,
        )

        # Chave válida pré-carrega URLs em lote via GetPlayerSummaries/v2 sem chamar XML individual
        fake_key = "ABCDEF0123456789ABCDEF0123456789"
        ap_api = SteamAvatarProvider(allow_network=True, steam_api_key=fake_key)
        self.assertTrue(ap_api.has_steam_api_key)

        sample_png_buf = io.BytesIO()
        Image.new("RGBA", (32, 32), (245, 184, 46, 255)).save(sample_png_buf, format="PNG")
        sample_png = sample_png_buf.getvalue()

        api_json = json.dumps(
            {
                "response": {
                    "players": [
                        {
                            "steamid": "76561197963172584",
                            "avatarmedium": "https://avatars.steamstatic.com/abc123_medium.jpg",
                        },
                        {
                            "steamid": "76561198014985380",
                            "avatarmedium": "https://evil.example/blocked.jpg",  # bloqueado pela allowlist
                        },
                    ]
                }
            }
        ).encode("utf-8")

        class _FakeResp:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload
                self.headers: dict[str, str] = {}

            def read(self, n: int = -1) -> bytes:
                return self._payload if n < 0 else self._payload[:n]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        opened_urls: list[str] = []

        def fake_open(req, timeout=10):
            url = req.full_url
            opened_urls.append(url)
            if "api.steampowered.com" in url:
                return _FakeResp(api_json)
            return _FakeResp(sample_png)

        with patch.object(rr._NO_REDIRECT_OPENER, "open", side_effect=fake_open), patch.object(
            rr._STEAM_XML_OPENER, "open", side_effect=AssertionError("Não deveria consultar XML quando pré-carregado!")
        ):
            count = ap_api.prefetch_avatar_urls(
                [
                    {"profile_id": 79327, "steam_id": "76561197963172584"},
                    {"profile_id": 366826, "steam_id": "76561198014985380"},
                ]
            )
            self.assertEqual(count, 1)  # apenas URL em host oficial da Steam é aceita
            fetched = ap_api._fetch_steam_avatar_bytes("76561197963172584")
            self.assertIsNotNone(fetched)
            av_url, png_bytes = fetched
            self.assertEqual(av_url, "https://avatars.steamstatic.com/abc123_medium.jpg")
            self.assertTrue(png_bytes.startswith(b"\x89PNG"))
            self.assertEqual(len(opened_urls), 2)
            self.assertIn("api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/", opened_urls[0])


if __name__ == "__main__":
    unittest.main()
