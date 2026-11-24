"""Testes unitários de segurança: SSRF, redaction de segredos, Path Traversal, Gzip Bomb, CSV/BiDi e higiene do repo."""

from __future__ import annotations

import gzip
import io
import json
import logging
import re
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from unittest.mock import patch

from PIL import Image

from arranged_teams import _read_bounded_daily_dump
from avatar_cache import RankingCacheDB
from daily_recap import ALLOWED_COHDB_HOSTS, COHDB_MATCH_PATH_RE, CohdbReplayClient
from discord_ranking import sanitize_discord_text
from ranking_renderer import _SteamXMLRedirectHandler, _load_flag_asset, get_battlegroup_icon
from run_ranking import load_config
from security import (
    RateLimiter,
    SecretRedactingFilter,
    StrictNoRedirectHandler,
    atomic_write_bytes,
    atomic_write_text,
    configure_secure_logging,
    get_thread_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    resolve_safe_db_path,
    sanitize_csv_cell,
    strip_unsafe_chars,
    validate_discord_webhook_url,
    validate_https_endpoint,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeHTTPResponse:
    def __init__(self, data: bytes, headers: dict[str, str] | None = None) -> None:
        self._buf = io.BytesIO(data)
        self.headers = headers or {}

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


class TestSecurityControls(unittest.TestCase):
    def test_https_endpoint_allowlist_and_ssrf_blocks(self) -> None:
        allowed = frozenset({"coh3-api.reliclink.com"})
        validate_https_endpoint(
            "https://coh3-api.reliclink.com/community/leaderboard/getPersonalStat?title=coh3",
            allowed,
            path_prefix="/community/leaderboard/",
        )
        bad_urls = [
            "http://coh3-api.reliclink.com/community/leaderboard/x",           # HTTP sem TLS
            "https://evil.com/community/leaderboard/x",                        # host não permitido
            "https://coh3-api.reliclink.com.evil.com/community/leaderboard/",  # sufixo falso
            "https://coh3-api.reliclink.com:8443/community/leaderboard/x",     # porta não 443
            "https://user:pass@coh3-api.reliclink.com/community/leaderboard/", # credenciais na URL
            "https://coh3-api.reliclink.com/admin/secret",                     # path fora do prefixo
            "https://coh3-api.reliclink.com/community/leaderboard/x\r\nX: 1",  # CRLF injection
            "https://coh3-api.reliclink.com/community/leaderboard/../../etc",  # path traversal literal
            "https://coh3-api.reliclink.com/community/leaderboard/%2e%2e/x",   # path traversal URL-encoded
            "https://coh3-api.reliclink.com/community/leaderboard/foo%5cbar",  # backslash URL-encoded
            "https://127.0.0.1/community/leaderboard/x",                       # loopback SSRF
            "https://169.254.169.254/latest/meta-data/",                       # IMDS SSRF
        ]
        for url in bad_urls:
            with self.assertRaises(ValueError, msg=f"URL insegura deveria ser bloqueada: {url}"):
                validate_https_endpoint(url, allowed, path_prefix="/community/leaderboard/")

        # Validação estrita do path_regex de replays cohdb.com
        validate_https_endpoint(
            "https://cohdb.com/matches/12345678",
            ALLOWED_COHDB_HOSTS,
            path_prefix="/matches/",
            path_regex=COHDB_MATCH_PATH_RE,
        )
        for bad_cohdb in (
            "https://cohdb.com/matches/abc",
            "https://cohdb.com/matches/123/extra",
            "https://cohdb.com/matches/../users/1",
            "https://cohdb.com/matches/1234567890123",
        ):
            with self.assertRaises(ValueError, msg=f"URL cohdb insegura deveria ser bloqueada: {bad_cohdb}"):
                validate_https_endpoint(
                    bad_cohdb,
                    ALLOWED_COHDB_HOSTS,
                    path_prefix="/matches/",
                    path_regex=COHDB_MATCH_PATH_RE,
                )
        self.assertIsNone(CohdbReplayClient().inspect_match(-5))

    def test_discord_webhook_url_validation(self) -> None:
        valid = "https://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz1234"
        self.assertTrue(validate_discord_webhook_url(valid))
        self.assertTrue(validate_discord_webhook_url(valid + "/messages/1573027241493921948", allow_message_subpath=True))
        self.assertFalse(validate_discord_webhook_url(valid + "/messages/1573027241493921948", allow_message_subpath=False))

        for bad in (
            "",
            "COLE_AQUI_VIA_ENV_DISCORD_RANKING_WEBHOOK_URL",
            "http://discord.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz1234",
            "https://evil.com/api/webhooks/123456789012345678/AbCdEfGhIjKlMnOpQrStUvWxYz1234",
            valid + "/messages/../../other",
        ):
            self.assertFalse(validate_discord_webhook_url(bad, allow_message_subpath=True))

    def test_redirect_handlers_block_unsafe_targets(self) -> None:
        self.assertIs(get_thread_no_redirect_https_opener(), get_thread_no_redirect_https_opener())
        req = urllib.request.Request("https://coh3-api.reliclink.com/community/leaderboard/x")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            StrictNoRedirectHandler().redirect_request(req, None, 302, "Found", {}, "https://evil.com/")
        ctx.exception.close()

        steam_req = urllib.request.Request("https://steamcommunity.com/profiles/76561197963172584/?xml=1")
        handler = _SteamXMLRedirectHandler()
        for bad_redir in (
            "https://evil.com/profiles/76561197963172584/?xml=1",
            "https://steamcommunity.com/market/listings/",
            "https://steamcommunity.com/profiles/../../market/",
            "http://steamcommunity.com/profiles/76561197963172584/?xml=1",
        ):
            with self.assertRaises(urllib.error.HTTPError) as sctx:
                handler.redirect_request(steam_req, None, 302, "Found", {}, bad_redir)
            sctx.exception.close()

    def test_secret_redaction_and_logging_filter(self) -> None:
        leaks = [
            ("https://discord.com/api/webhooks/123456789012345678/SuperSecretTokenXYZ12345", "SuperSecretToken"),
            ("https://canary.discord.com/api/v10/webhooks/123456789012345678/CanarySecretTokenXYZ", "CanarySecretToken"),
            ("DISCORD_RANKING_WEBHOOK_URL=secret_env_val", "secret_env_val"),
            ("DISCORD_DAILY_RECAP_WEBHOOK_URL=daily_secret_val", "daily_secret_val"),
            ("DISCORD_BATTLEGROUPS_WEBHOOK_URL=bg_secret_val", "bg_secret_val"),
            ("DISCORD_WEBHOOK_URL=legacy_env_val", "legacy_env_val"),
            ("STEAM_WEB_API_KEY=ABCDEF0123456789ABCDEF0123456789", "ABCDEF0123456789"),
            ("STEAM_API_KEY=1234567890ABCDEF1234567890ABCDEF", "1234567890ABCDEF"),
            (
                "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/?key=DEADBEEF12345678DEADBEEF12345678&steamids=76561197963172584",
                "DEADBEEF12345678",
            ),
            ("ghp_1234567890abcdefghij1234567890", "ghp_123456"),
            ("github_pat_11ABCDEF1234567890_abcdefghijklmnop", "github_pat_11"),
            ("gho_1234567890abcdefghij1234567890", "gho_123456"),
        ]
        for raw, needle in leaks:
            scrubbed = redact_secrets(raw)
            self.assertNotIn(needle, scrubbed)

        flt = SecretRedactingFilter()
        rec = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="Falha em %s: %s",
            args=("https://discord.com/api/webhooks/123456789012345678/SecretToken1234567890", ValueError("ghp_1234567890abcdefghij1234567890")),
            exc_info=None,
        )
        rec.exc_text = "Traceback: ?key=DEADBEEF12345678DEADBEEF12345678"
        self.assertTrue(flt.filter(rec))
        formatted = rec.getMessage()
        self.assertNotIn("SecretToken1234567890", formatted)
        self.assertNotIn("ghp_123456", formatted)
        self.assertNotIn("DEADBEEF12345678", rec.exc_text)

        # Idempotência de configure_secure_logging (não duplica filtros ao importar múltiplos módulos)
        configure_secure_logging()
        configure_secure_logging()
        root = logging.getLogger()
        self.assertEqual(sum(1 for f in root.filters if isinstance(f, SecretRedactingFilter)), 1)
        for h in root.handlers:
            self.assertEqual(sum(1 for f in h.filters if isinstance(f, SecretRedactingFilter)), 1)

        rl = RateLimiter(min_interval_sec=0.001, floor_sec=0.005)
        self.assertEqual(rl.min_interval_sec, 0.005)
        rl.wait()

    def test_bounded_http_and_gzip_bomb_protection(self) -> None:
        # Payload raw acima do limite
        with self.assertRaises(RuntimeError):
            read_bounded_http_body(_FakeHTTPResponse(b"A" * 200), max_bytes=100)

        # Gzip bomb: ~1 KB comprimido que expande para 2 MB (> limite de 10 KB)
        bomb = gzip.compress(b"\x00" * (2 * 1024 * 1024))
        self.assertLess(len(bomb), 10_000)
        with self.assertRaises(RuntimeError):
            read_bounded_http_body(_FakeHTTPResponse(bomb, {"Content-Encoding": "gzip"}), max_bytes=10_000)

        # Gzip válido abaixo do limite
        ok_gz = gzip.compress(b'{"matches": []}')
        self.assertEqual(
            read_bounded_http_body(_FakeHTTPResponse(ok_gz, {"Content-Encoding": "gzip"}), max_bytes=1024),
            b'{"matches": []}',
        )
        self.assertEqual(
            _read_bounded_daily_dump(_FakeHTTPResponse(ok_gz, {"Content-Encoding": "gzip"})),
            b'{"matches": []}',
        )

    def test_path_traversal_and_symlink_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td) / "proj"
            base.mkdir()
            self.assertEqual(resolve_safe_db_path(base, "./ranking_cache.db"), (base / "ranking_cache.db").resolve())
            for bad in ("../escape.db", "../../etc/passwd", "/tmp/outside.db"):
                with self.assertRaises(ValueError):
                    resolve_safe_db_path(base, bad)

            real_file = base / "real.txt"
            atomic_write_text(real_file, "ok")
            self.assertEqual(real_file.read_text(encoding="utf-8"), "ok")
            bin_file = base / "sample.png"
            atomic_write_bytes(bin_file, b"\x89PNG\r\n\x1a\n")
            self.assertEqual(bin_file.read_bytes(), b"\x89PNG\r\n\x1a\n")
            # Nenhum arquivo temporário residual
            self.assertEqual(list(base.glob("*.tmp")), [])

            sym = base / "sym.txt"
            sym.symlink_to(real_file)
            with self.assertRaises(RuntimeError):
                atomic_write_text(sym, "blocked")
            with self.assertRaises(RuntimeError):
                atomic_write_bytes(sym, b"blocked")

            sym_cfg = base / "sym_config.json"
            sym_cfg.symlink_to(real_file)
            with self.assertRaises(RuntimeError):
                load_config(sym_cfg)

            sym_db = base / "sym.db"
            sym_db.symlink_to(real_file)
            with self.assertRaises(RuntimeError):
                resolve_safe_db_path(base, "./sym.db")
            with patch.dict("os.environ", {"SQLITE_DB_PATH": str(sym_db)}):
                with self.assertRaises(RuntimeError):
                    resolve_safe_db_path(base, "./ranking_cache.db")
            with self.assertRaises(RuntimeError):
                RankingCacheDB(sym_db)

        # Path traversal em códigos de bandeira e ícones de Battlegroup
        for bad_code in ("../", "br/..", "BR", "usa", "a1"):
            self.assertIsNone(_load_flag_asset(bad_code))
        for bad_slug in ("../etc/passwd", "../../ranking_cache", "american/../secret", "", "US/Airborne"):
            im_bg = get_battlegroup_icon(bad_slug, size=32)
            self.assertEqual(im_bg.size, (32, 32))
            self.assertEqual(im_bg.getpixel((0, 0)), (218, 165, 32, 200))

    def test_csv_formula_and_bidi_sanitization(self) -> None:
        for payload in ("=1+1", "  =CMD|' /C calc'!A0", "+2+3", "-10", "@SUM(1,2)", "|calc", "\t=1+1"):
            out = sanitize_csv_cell(payload)
            self.assertTrue(out.startswith("'"), f"Fórmula CSV não neutralizada: {payload!r} -> {out!r}")
        self.assertEqual(sanitize_csv_cell("Chacineiro"), "Chacineiro")
        self.assertEqual(sanitize_csv_cell(1234), 1234)

        self.assertEqual(strip_unsafe_chars("A\u202eB\x00C\u200bD"), "A B C D")
        sanitized_discord = sanitize_discord_text("@everyone [link](https://evil.test) `code`")
        self.assertNotIn("@everyone", sanitized_discord)
        self.assertNotIn("[link]", sanitized_discord)
        self.assertNotIn("`", sanitized_discord)

    def test_repository_hygiene_no_secrets_no_symlinks_and_pillow_cap(self) -> None:
        self.assertIsNotNone(Image.MAX_IMAGE_PIXELS)
        self.assertLessEqual(int(Image.MAX_IMAGE_PIXELS), 4_000_000)

        cfg = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
        self.assertFalse(validate_discord_webhook_url(str(cfg.get("discord_webhook_url") or "")))

        secret_re = re.compile(
            r"(((ptb|canary)\.)?discord(app)?\.com/api/(v[0-9]+/)?webhooks/\d{15,25}/[A-Za-z0-9_-]{20,}"
            r"|STEAM_(?:WEB_)?API_KEY=[A-Za-z0-9]{16,}"
            r"|[?&]key=[A-Za-z0-9]{16,64}\b"
            r"|gh[pos]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN (?:RSA|EC|OPENSSH|PRIVATE) KEY)",
            re.IGNORECASE,
        )
        dynamic_sql_re = re.compile(r"\.execute(?:many)?\(\s*f[\"']")
        for p in REPO_ROOT.rglob("*"):
            if ".git" in p.parts:
                continue
            self.assertFalse(p.is_symlink(), f"Symlink não esperado no repositório: {p}")
            if p.is_file() and p.suffix.lower() in (".py", ".json", ".csv", ".yml", ".yaml", ".md"):
                # Ignora este próprio arquivo de testes que contém strings fictícias para validar redaction
                if p.resolve() == Path(__file__).resolve() or p.parent.name == "tests":
                    continue
                txt = p.read_text(encoding="utf-8", errors="ignore")
                self.assertIsNone(secret_re.search(txt), f"Possível credencial encontrada em {p}")
                if p.suffix.lower() == ".py":
                    self.assertIsNone(dynamic_sql_re.search(txt), f"SQL dinâmico com f-string encontrado em {p}")

        db_file = REPO_ROOT / "ranking_cache.db"
        if db_file.is_file():
            raw_db = db_file.read_bytes().decode("latin1")
            self.assertIsNone(secret_re.search(raw_db), "Possível credencial encontrada em ranking_cache.db")


if __name__ == "__main__":
    unittest.main()
