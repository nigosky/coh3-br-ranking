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


class TestDiscordPayloadsAndPatchSafety(unittest.TestCase):
    def setUp(self) -> None:
        self.ap = SteamAvatarProvider(db=None, allow_network=False)

    def test_render_ranking_attachments_excludes_non_custom_paginates_and_builds_safe_payload(self) -> None:
        entries = [
            {"profile_id": 9001, "alias": "OutroBR", "br_rank": 1, "skill_points": 2800.0, "anchor_mode": "1v1", "mode_max_elos": {"1v1": 1900}, "wins": 50, "losses": 5, "is_custom": False},
            {"profile_id": 79327, "alias": "Chacineiro", "br_rank": 2, "skill_points": 2435.2, "anchor_mode": "1v1", "mode_max_elos": {"1v1": 1566}, "wins": 40, "losses": 20, "is_custom": True, "inactive_fallback": False},
        ]
        atts = render_ranking_attachments(entries, avatar_provider=self.ap, unique_roster_count=44)
        self.assertEqual([a[0] for a in atts], ["ranking_unificado_p1.png"])

        # Paginação automática com 30 Veteranos -> 2 páginas balanceadas (15 + 15)
        many = [
            {"profile_id": 1000 + i, "alias": f"Vet{i}", "br_rank": i + 1, "skill_points": 2000.0 - i, "anchor_mode": "2v2", "mode_max_elos": {"2v2": 1400}, "wins": 20, "losses": 10, "is_custom": True, "inactive_fallback": False}
            for i in range(30)
        ]
        many_atts = render_ranking_attachments(many, avatar_provider=self.ap, unique_roster_count=44)
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
        self.assertNotIn("Top 20", desc)
        self.assertNotIn("@everyone", desc)
        self.assertNotIn("[spam]", desc)

    def test_arranged_attachments_pagination_and_empty_state(self) -> None:
        empty_atts = render_arranged_teams_attachments([], avatar_provider=self.ap, inactive_days=30)
        self.assertEqual(len(empty_atts), 1)
        payload = build_arranged_teams_panel_payload([], [empty_atts[0][0]], inactive_days=30)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertIn("COH3 Stats Open Data", payload["embeds"][0]["description"])

    def test_upsert_patch_clears_old_attachments_and_falls_back_on_404(self) -> None:
        valid_wh = "https://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz1234"
        client = DiscordRankingClient(webhook_url=valid_wh)
        files = [("test.png", b"\x89PNG\r\n\x1a\n", "image/png")]
        payload = {"embeds": [{"title": "Test"}]}

        with patch.object(client, "_request", return_value=(200, {"id": "1573027241493921948"})) as mock_req:
            msg_id = client._upsert_message(payload, files, existing_message_id="1573027241493921948")
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
            msg_id = client._upsert_message(payload, files, existing_message_id="1573027241493921948")
            self.assertEqual(msg_id, "1573099999999999999")
            self.assertEqual(mock_req.call_args_list[0][0][0], "PATCH")
            self.assertEqual(mock_req.call_args_list[1][0][0], "POST")
            self.assertIn("wait=true", mock_req.call_args_list[1][0][1])

    def test_multipart_sanitizes_filename(self) -> None:
        body, ctype = _encode_multipart_formdata({"a": 1}, [("../evil\r\n.png", b"PNG", "image/png")])
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        self.assertNotIn(b'filename="../evil', body)
        self.assertNotIn(b"\r\n.png", body)


if __name__ == "__main__":
    unittest.main()
