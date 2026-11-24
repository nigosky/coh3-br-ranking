"""Renderizador gráfico seguro (Pillow) para as tabelas do Ranking Unificado BR (CoH3)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from relic_ranking import ALLOWED_RELIC_HOSTS, BASE_URL as RELIC_BASE_URL
from roster import CORE_BR_ROSTER, STEAM_ID64_VALID_RE, count_unique_roster_players
from security import (
    BRT_TZ,
    build_no_redirect_https_opener,
    create_hardened_ssl_context,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_https_endpoint,
)

# Proteção estrita contra Decompression Bomb (CWE-409)
Image.MAX_IMAGE_PIXELS = 4_000_000

logger = logging.getLogger("coh3_ranking.renderer")

BASE_DIR = Path(__file__).resolve().parent
FLAGS_DIR = BASE_DIR / "assets" / "flags"
BG_ICONS_DIR = BASE_DIR / "assets" / "battlegroups"
VETERANOS_BADGE_PATH = BASE_DIR / "assets" / "discord_veteranos_patente.png"

SAFE_FLAG_CODE_RE = re.compile(r"^[a-z]{2}$")
SAFE_BG_SLUG_RE = re.compile(r"^[a-z0-9_]{3,80}$")
MAX_FLAG_ASSET_BYTES = 65_536      # 64 KB
MAX_BG_ICON_BYTES = 131_072        # 128 KB
MAX_STEAM_XML_BYTES = 65_536       # 64 KB
MAX_STEAM_API_JSON_BYTES = 524_288 # 512 KB (100 perfis GetPlayerSummaries ~ 40 KB)
MAX_STEAM_AVATAR_BYTES = 524_288   # 512 KB
MAX_RELIC_LOOKUP_BYTES = 131_072   # 128 KB

STEAM_API_SUMMARIES_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
STEAM_API_SUMMARIES_PATH_RE = re.compile(r"^/ISteamUser/GetPlayerSummaries/v0*2/?$")
STEAM_API_KEY_VALID_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")

STEAM_XML_AVATAR_MEDIUM_RE = re.compile(
    r"<avatarMedium>\s*<!\[CDATA\[(https://[^\s\]]+)\]\]>\s*</avatarMedium>",
    re.IGNORECASE,
)
STEAM_XML_AVATAR_FULL_RE = re.compile(
    r"<avatarFull>\s*<!\[CDATA\[(https://[^\s\]]+)\]\]>\s*</avatarFull>",
    re.IGNORECASE,
)
SAFE_AVATAR_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE)

ALLOWED_STEAM_API_HOSTS: frozenset[str] = frozenset({"api.steampowered.com"})
ALLOWED_STEAM_XML_HOSTS: frozenset[str] = frozenset({"steamcommunity.com"})
ALLOWED_STEAM_AVATAR_HOSTS: frozenset[str] = frozenset(
    {
        "avatars.steamstatic.com",
        "avatars.akamai.steamstatic.com",
        "avatars.cloudflare.steamstatic.com",
        "avatars.fastly.steamstatic.com",
        "steamcdn-a.akamaihd.net",
        "cdn.akamai.steamstatic.com",
    }
)
ALLOWED_DECODER_FORMATS: frozenset[str] = frozenset({"JPEG", "PNG", "WEBP", "GIF"})

HAS_CJK_RE = re.compile(
    r"[\u1100-\u11ff\u2e80-\u33ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]"
)
NEEDS_DEJAVU_RE = re.compile(r"[^\u0000-\u024f\u2013\u2014\u2022\u2026]")


class _SteamXMLRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Permite até 3 redirecionamentos exclusivamente dentro de https://steamcommunity.com/(id|profiles)/."""

    max_redirections = 3

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        try:
            parsed = validate_https_endpoint(newurl, ALLOWED_STEAM_XML_HOSTS)
        except ValueError as exc:
            raise urllib.error.HTTPError(req.full_url, code, f"Redirect Steam bloqueado: {exc}", headers, fp) from None
        if not (parsed.path.startswith("/id/") or parsed.path.startswith("/profiles/")):
            raise urllib.error.HTTPError(req.full_url, code, "Caminho de redirect Steam não autorizado", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_STEAM_XML_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=create_hardened_ssl_context()),
    _SteamXMLRedirectHandler(),
)
_NO_REDIRECT_OPENER = build_no_redirect_https_opener()


# ---------------------------------------------------------------------------
# Carregamento seguro de fontes (Roboto, DejaVu Unicode e Noto Sans CJK)
# ---------------------------------------------------------------------------
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}
_DISCOVERED_CJK_FONTS: dict[str, list[str]] | None = None
_WARNED_MISSING_CJK = False

FONT_CANDIDATES: dict[str, list[str]] = {
    "regular": [
        "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/roboto/hinted/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/roboto/Roboto-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
    "bold": [
        "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Bold.ttf",
        "/usr/share/fonts/truetype/roboto/hinted/Roboto-Bold.ttf",
        "/usr/share/fonts/truetype/roboto/Roboto-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "condensed_bold": [
        "/usr/share/fonts/truetype/roboto/unhinted/RobotoCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/roboto/hinted/RobotoCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/roboto/RobotoCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF/Roboto-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ],
    "unicode_regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    "unicode_bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    "cjk_regular": [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    ],
    "cjk_bold": [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ],
}


def _discover_cjk_fonts() -> dict[str, list[str]]:
    global _DISCOVERED_CJK_FONTS
    if _DISCOVERED_CJK_FONTS is not None:
        return _DISCOVERED_CJK_FONTS
    regular: list[str] = []
    bold: list[str] = []
    fonts_root = Path("/usr/share/fonts")
    if fonts_root.is_dir():
        for p in sorted(fonts_root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in (".ttc", ".ttf", ".otf"):
                continue
            low = p.name.lower()
            if not any(tok in low for tok in ("notosanscjk", "notosanssc", "notosanstc", "wqy-", "sourcehansans")):
                continue
            if "bold" in low:
                bold.append(str(p))
            else:
                regular.append(str(p))
    _DISCOVERED_CJK_FONTS = {"cjk_regular": regular + bold, "cjk_bold": bold + regular}
    return _DISCOVERED_CJK_FONTS


def _load_truetype_font(path: Path, size: int, is_cjk: bool = False) -> ImageFont.FreeTypeFont:
    if is_cjk and path.suffix.lower() == ".ttc" and "notosanscjk" in path.name.lower():
        for idx in (2, 0):
            try:
                return ImageFont.truetype(str(path), size=size, index=idx)
            except OSError:
                continue
    return ImageFont.truetype(str(path), size=size)


def _get_font_by_role(role: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    global _WARNED_MISSING_CJK
    key = (role, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached

    is_cjk = role in ("cjk_regular", "cjk_bold")
    candidates = list(FONT_CANDIDATES.get(role, FONT_CANDIDATES["regular"]))
    if is_cjk:
        for extra in _discover_cjk_fonts().get(role, []):
            if extra not in candidates:
                candidates.append(extra)

    for path_str in candidates:
        p = Path(path_str)
        if p.is_file():
            try:
                fnt = _load_truetype_font(p, size=size, is_cjk=is_cjk)
                _FONT_CACHE[key] = fnt
                return fnt
            except OSError:
                continue

    if is_cjk:
        if not _WARNED_MISSING_CJK:
            logger.warning("Fonte CJK não encontrada em /usr/share/fonts; usando fallback DejaVu.")
            _WARNED_MISSING_CJK = True
        return _get_font_by_role("unicode_bold" if role == "cjk_bold" else "unicode_regular", size)

    fallback = ImageFont.load_default()
    _FONT_CACHE[key] = fallback
    return fallback


def pick_font(
    text: str,
    size: int,
    bold: bool = False,
    condensed: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if HAS_CJK_RE.search(text):
        return _get_font_by_role("cjk_bold" if bold else "cjk_regular", size)
    if NEEDS_DEJAVU_RE.search(text):
        return _get_font_by_role("unicode_bold" if bold else "unicode_regular", size)
    if condensed:
        return _get_font_by_role("condensed_bold", size)
    return _get_font_by_role("bold" if bold else "regular", size)


def text_width(draw: ImageDraw.ImageDraw, text: str, font: Any) -> float:
    try:
        return float(draw.textlength(text, font=font))
    except AttributeError:
        bbox = draw.textbbox((0, 0), text, font=font)
        return float(bbox[2] - bbox[0])


def truncate_to_px(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_px: int,
    size: int,
    bold: bool = False,
) -> tuple[str, Any]:
    txt = strip_unsafe_chars(text) or "Desconhecido"
    for try_sz in (size, max(12, size - 2), max(11, size - 4)):
        fnt = pick_font(txt, size=try_sz, bold=bold)
        if text_width(draw, txt, fnt) <= max_px:
            return txt, fnt
    fnt = pick_font(txt, size=max(11, size - 3), bold=bold)
    while len(txt) > 2 and text_width(draw, txt + "…", fnt) > max_px:
        txt = txt[:-1]
    return txt + "…", fnt


def _encode_rgb_png(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", compress_level=6)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Avatares Steam (XML público sem chave de API + cache local SQLite)
# ---------------------------------------------------------------------------
def _build_fallback_avatar(alias: str, seed: int) -> Image.Image:
    palettes = (
        ((22, 101, 52), (74, 222, 128)),
        ((30, 64, 175), (96, 165, 250)),
        ((146, 64, 14), (251, 191, 36)),
        ((88, 28, 135), (192, 132, 252)),
        ((15, 118, 110), (45, 212, 191)),
    )
    bg, fg = palettes[abs(int(seed)) % len(palettes)]
    im = Image.new("RGBA", (64, 64), (*bg, 255))
    drw = ImageDraw.Draw(im)
    alnum = "".join(ch for ch in strip_unsafe_chars(alias) if ch.isalnum())
    initials = (alnum[:2] or "BR").upper()
    fnt = pick_font(initials, size=26, bold=True)
    bbox = drw.textbbox((0, 0), initials, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    drw.text(((64 - tw) / 2 - bbox[0], (64 - th) / 2 - bbox[1]), initials, font=fnt, fill=(*fg, 255))
    return im


def _mask_circle_rgba(
    src: Image.Image,
    size: int,
    ring_color: tuple[int, int, int, int] | None = None,
) -> Image.Image:
    ss = size * 2
    resized = src.resize((ss, ss), Image.Resampling.LANCZOS)
    mask = Image.new("L", (ss, ss), 0)
    mdraw = ImageDraw.Draw(mask)
    pad = 2 if ring_color else 0
    mdraw.ellipse((pad, pad, ss - 1 - pad, ss - 1 - pad), fill=255)

    out = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
    out.paste(resized, (0, 0), mask)
    if ring_color:
        odraw = ImageDraw.Draw(out)
        odraw.ellipse((1, 1, ss - 2, ss - 2), outline=ring_color, width=3)
    return out.resize((size, size), Image.Resampling.LANCZOS)


class SteamAvatarProvider:
    """Busca e faz cache de avatares reais da Steam (Web API opcional em lote + fallback XML público)."""

    def __init__(
        self,
        db: Any | None = None,
        catalog_json_path: Path | None = None,
        allow_network: bool = True,
        steam_api_key: str | None = None,
    ) -> None:
        self.db = db
        self.allow_network = allow_network
        self.catalog_json_path = catalog_json_path or (BASE_DIR / "jogadores_br_steam.json")
        self._catalog_loaded = False
        self._lock = threading.Lock()
        self._mem_png_cache: dict[str, Image.Image] = {}
        self._circle_cache: dict[tuple[str, int, tuple[int, int, int, int] | None], Image.Image] = {}
        self._pid_to_steam: dict[int, str] = {}
        self._prefetched_avatar_urls: dict[str, str] = {}
        self._failed_steam_ids: set[str] = set()
        self._network_fetches_this_run = 0
        self._max_network_fetches_per_run = 160
        self._last_net_ts = 0.0

        raw_key = (
            steam_api_key
            if steam_api_key is not None
            else (os.environ.get("STEAM_WEB_API_KEY") or os.environ.get("STEAM_API_KEY") or "")
        )
        clean_key = str(raw_key or "").strip()
        self._steam_api_key = clean_key if STEAM_API_KEY_VALID_RE.fullmatch(clean_key) else ""

        for r in CORE_BR_ROSTER:
            r_pid = int(r.get("profile_id", 0))
            r_sid = str(r.get("steam_id") or "").strip()
            if r_pid > 0 and STEAM_ID64_VALID_RE.fullmatch(r_sid):
                self._pid_to_steam[r_pid] = r_sid

        if db is not None and hasattr(db, "get_steam_id_map"):
            try:
                self._pid_to_steam.update(db.get_steam_id_map())
            except Exception:
                pass

    @property
    def has_steam_api_key(self) -> bool:
        return bool(self._steam_api_key)

    def _ensure_catalog_loaded(self) -> None:
        if self._catalog_loaded:
            return
        self._catalog_loaded = True
        if not self.catalog_json_path.is_file():
            return
        try:
            doc = json.loads(self.catalog_json_path.read_text(encoding="utf-8"))
            for p in doc.get("players", []):
                pid = int(p.get("profile_id", 0))
                sid = str(p.get("steam_id") or "").strip()
                if pid > 0 and STEAM_ID64_VALID_RE.fullmatch(sid):
                    self._pid_to_steam.setdefault(pid, sid)
        except Exception:
            pass

    def _throttle_network(self) -> bool:
        with self._lock:
            if self._network_fetches_this_run >= self._max_network_fetches_per_run:
                return False
            self._network_fetches_this_run += 1
            now_m = time.monotonic()
            wait = max(0.0, (self._last_net_ts + 0.10) - now_m)
            self._last_net_ts = max(now_m, self._last_net_ts) + 0.10
        if wait > 0:
            time.sleep(wait)
        return True

    def _lookup_steam_id_from_relic(self, profile_id: int) -> str:
        if profile_id <= 0 or not self.allow_network or not self._throttle_network():
            return ""
        url = f"{RELIC_BASE_URL}/getPersonalStat?title=coh3&profile_ids=%5B{int(profile_id)}%5D"
        try:
            validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "CoH3-BR-Ranking-Unificado/1.0", "Accept": "application/json", "Accept-Encoding": "gzip"},
            )
            with _NO_REDIRECT_OPENER.open(req, timeout=6) as resp:
                raw = read_bounded_http_body(resp, MAX_RELIC_LOOKUP_BYTES)
            data = json.loads(raw.decode("utf-8", errors="ignore"))
            for sg in data.get("statGroups", []):
                for mem in sg.get("members", []):
                    if int(mem.get("profile_id", 0)) == int(profile_id):
                        nm = str(mem.get("name") or "")
                        sid = nm[7:] if nm.startswith("/steam/") else ""
                        if STEAM_ID64_VALID_RE.fullmatch(sid):
                            return sid
        except Exception:
            pass
        return ""

    def resolve_steam_id(self, profile_id: int = 0, steam_id: str = "") -> str:
        clean_sid = str(steam_id or "").strip()
        if clean_sid.startswith("/steam/"):
            clean_sid = clean_sid[7:].strip()
        if STEAM_ID64_VALID_RE.fullmatch(clean_sid):
            if profile_id > 0:
                self._pid_to_steam[int(profile_id)] = clean_sid
            return clean_sid
        if profile_id > 0:
            pid_int = int(profile_id)
            if pid_int in self._pid_to_steam:
                return self._pid_to_steam[pid_int]
            self._ensure_catalog_loaded()
            if pid_int in self._pid_to_steam:
                return self._pid_to_steam[pid_int]
            looked_up = self._lookup_steam_id_from_relic(pid_int)
            self._pid_to_steam[pid_int] = looked_up
            return looked_up
        return ""

    def _validate_steam_avatar_url(self, raw_url: str) -> str:
        clean = str(raw_url or "").strip()
        if not clean:
            return ""
        try:
            parsed_av = validate_https_endpoint(clean, ALLOWED_STEAM_AVATAR_HOSTS)
            if parsed_av.query or parsed_av.fragment or not SAFE_AVATAR_EXT_RE.search(parsed_av.path):
                return ""
            return clean
        except ValueError:
            return ""

    def _download_and_normalize_avatar(self, avatar_url: str) -> tuple[str, bytes] | None:
        valid_url = self._validate_steam_avatar_url(avatar_url)
        if not valid_url:
            return None
        img_req = urllib.request.Request(
            valid_url,
            headers={"User-Agent": "CoH3-BR-Ranking-Unificado/1.0", "Accept": "image/*"},
        )
        with _NO_REDIRECT_OPENER.open(img_req, timeout=8) as img_resp:
            img_bytes = read_bounded_http_body(img_resp, MAX_STEAM_AVATAR_BYTES)

        with Image.open(io.BytesIO(img_bytes), formats=list(ALLOWED_DECODER_FORMATS)) as im:
            if im.format not in ALLOWED_DECODER_FORMATS or not (1 <= im.width <= 1024 and 1 <= im.height <= 1024):
                return None
            clean_im = im.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
            out_buf = io.BytesIO()
            clean_im.save(out_buf, format="PNG", compress_level=6)
            return valid_url, out_buf.getvalue()

    def prefetch_avatar_urls(self, players: list[dict[str, Any]]) -> int:
        """
        Pré-carrega URLs de avatar em lote (até 100 SteamIDs por requisição) via Steam Web API
        (`ISteamUser/GetPlayerSummaries/v2`) quando `STEAM_WEB_API_KEY` estiver configurado,
        evitando consultas XML individuais sujeitas a rate-limit 429.
        """
        if not self.allow_network or not self._steam_api_key or not players:
            return 0

        pending_sids: list[str] = []
        seen: set[str] = set()
        for pl in players:
            if not isinstance(pl, dict):
                continue
            pid = int(pl.get("profile_id", 0) or 0)
            raw_sid = str(pl.get("steam_id") or "")
            sid = self.resolve_steam_id(profile_id=pid, steam_id=raw_sid)
            if (
                not sid
                or sid in seen
                or sid in self._mem_png_cache
                or sid in self._prefetched_avatar_urls
                or sid in self._failed_steam_ids
            ):
                continue
            if self.db is not None and hasattr(self.db, "get_cached_avatar"):
                try:
                    if self.db.get_cached_avatar(steam_id=sid, profile_id=pid) is not None:
                        continue
                except Exception:
                    pass
            seen.add(sid)
            pending_sids.append(sid)

        if not pending_sids:
            return 0

        fetched_urls = 0
        requests_made = 0
        for idx in range(0, len(pending_sids), 100):
            batch = pending_sids[idx : idx + 100]
            if not self._throttle_network():
                break
            query = urllib.parse.urlencode({"key": self._steam_api_key, "steamids": ",".join(batch)})
            url = f"{STEAM_API_SUMMARIES_URL}?{query}"
            try:
                validate_https_endpoint(
                    url,
                    ALLOWED_STEAM_API_HOSTS,
                    path_prefix="/ISteamUser/",
                    path_regex=STEAM_API_SUMMARIES_PATH_RE,
                )
                req = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "CoH3-BR-Ranking-Unificado/1.0",
                        "Accept": "application/json",
                        "Accept-Encoding": "gzip",
                    },
                )
                with _NO_REDIRECT_OPENER.open(req, timeout=10) as resp:
                    raw = read_bounded_http_body(resp, MAX_STEAM_API_JSON_BYTES)
                requests_made += 1
                payload = json.loads(raw.decode("utf-8", errors="ignore"))
                api_players = (payload.get("response") or {}).get("players") if isinstance(payload, dict) else None
                if not isinstance(api_players, list):
                    continue
                for item in api_players:
                    if not isinstance(item, dict):
                        continue
                    sid = str(item.get("steamid") or "").strip()
                    if not STEAM_ID64_VALID_RE.fullmatch(sid):
                        continue
                    av_url = self._validate_steam_avatar_url(
                        str(item.get("avatarmedium") or item.get("avatarfull") or item.get("avatar") or "")
                    )
                    if av_url:
                        self._prefetched_avatar_urls[sid] = av_url
                        fetched_urls += 1
            except Exception as exc:
                logger.warning(
                    "Steam Web API GetPlayerSummaries indisponível (fallback para XML público): %s",
                    redact_secrets(exc),
                )
                break

        if fetched_urls > 0:
            logger.info(
                "Steam Web API: pré-carregadas %d URL(s) de avatar em lote (%d requisição(ões)).",
                fetched_urls,
                requests_made,
            )
        return fetched_urls

    def _fetch_steam_avatar_bytes(self, steam_id: str) -> tuple[str, bytes] | None:
        if not STEAM_ID64_VALID_RE.fullmatch(steam_id) or steam_id in self._failed_steam_ids:
            return None

        # 1. Caminho rápido: URL já pré-carregada em lote via STEAM_WEB_API_KEY (zero requisição XML)
        prefetched_url = self._prefetched_avatar_urls.get(steam_id)
        if prefetched_url:
            if self._throttle_network():
                try:
                    downloaded = self._download_and_normalize_avatar(prefetched_url)
                    if downloaded is not None:
                        return downloaded
                except Exception as exc:
                    logger.debug(
                        "Falha ao baixar avatar pré-carregado (%s); tentando fallback XML: %s",
                        steam_id,
                        redact_secrets(exc),
                    )

        # 2. Fallback automático: XML público da Steam (funciona sem chave de API)
        xml_url = f"https://steamcommunity.com/profiles/{steam_id}/?xml=1"
        validate_https_endpoint(xml_url, ALLOWED_STEAM_XML_HOSTS, path_prefix="/profiles/")

        for attempt in range(2):
            if not self._throttle_network():
                return None
            try:
                req = urllib.request.Request(
                    xml_url,
                    headers={"User-Agent": "CoH3-BR-Ranking-Unificado/1.0", "Accept": "application/xml,text/xml"},
                )
                with _STEAM_XML_OPENER.open(req, timeout=8) as resp:
                    xml_raw = read_bounded_http_body(resp, MAX_STEAM_XML_BYTES)
                xml_text = xml_raw.decode("utf-8", errors="ignore")

                m = STEAM_XML_AVATAR_MEDIUM_RE.search(xml_text) or STEAM_XML_AVATAR_FULL_RE.search(xml_text)
                if not m:
                    break

                downloaded = self._download_and_normalize_avatar(m.group(1).strip())
                if downloaded is not None:
                    return downloaded
                break
            except Exception as exc:
                if attempt == 0:
                    time.sleep(0.25)
                    continue
                logger.debug("Avatar Steam indisponível (%s): %s", steam_id, redact_secrets(exc))

        self._failed_steam_ids.add(steam_id)
        return None

    def get_circular_avatar(
        self,
        size: int,
        profile_id: int = 0,
        steam_id: str = "",
        alias: str = "",
        ring_color: tuple[int, int, int, int] | None = (34, 197, 94, 230),
    ) -> Image.Image:
        sid = self.resolve_steam_id(profile_id=profile_id, steam_id=steam_id)
        cache_key = sid or f"pid_{profile_id}_{alias[:6]}"
        circle_key = (cache_key, size, ring_color)

        cached_circle = self._circle_cache.get(circle_key)
        if cached_circle is not None:
            return cached_circle

        base_img = self._mem_png_cache.get(cache_key)
        if base_img is None:
            png_blob: bytes | None = None
            if self.db is not None and hasattr(self.db, "get_cached_avatar"):
                try:
                    png_blob = self.db.get_cached_avatar(steam_id=sid, profile_id=profile_id)
                except Exception:
                    png_blob = None

            if png_blob is None and sid and self.allow_network:
                fetched = self._fetch_steam_avatar_bytes(sid)
                if fetched is not None:
                    av_url, png_blob = fetched
                    if self.db is not None and hasattr(self.db, "save_cached_avatar"):
                        try:
                            self.db.save_cached_avatar(sid, av_url, png_blob, profile_id=profile_id)
                        except Exception:
                            pass

            if png_blob:
                try:
                    with Image.open(io.BytesIO(png_blob), formats=["PNG"]) as decoded:
                        base_img = decoded.convert("RGBA")
                        self._mem_png_cache[cache_key] = base_img
                except Exception:
                    base_img = None

        if base_img is None:
            det_seed = int(profile_id) if profile_id else int.from_bytes(hashlib.sha256(alias.encode("utf-8")).digest()[:4], "big")
            base_img = _build_fallback_avatar(alias or "BR", seed=det_seed)
            self._mem_png_cache[cache_key] = base_img

        masked = _mask_circle_rgba(base_img, size=size, ring_color=ring_color)
        if len(self._circle_cache) >= 256:
            self._circle_cache.clear()
        self._circle_cache[circle_key] = masked
        return masked


# ---------------------------------------------------------------------------
# Bandeiras oficiais ISO-3166-1 PNG e Patente Discord Veteranos
# ---------------------------------------------------------------------------
_FLAG_BASE_CACHE: dict[str, Image.Image | None] = {}
_FLAG_CACHE: dict[tuple[str, int, int], Image.Image] = {}
_VETERANOS_BADGE_CACHE: dict[int, Image.Image] = {}
FLAG_CODE_ALIASES: dict[str, str] = {"uk": "gb"}


def _load_flag_asset(code: str) -> Image.Image | None:
    if not SAFE_FLAG_CODE_RE.match(code):
        return None
    if code in _FLAG_BASE_CACHE:
        return _FLAG_BASE_CACHE[code]

    png_path = FLAGS_DIR / f"{code}.png"
    base: Image.Image | None = None
    try:
        flags_root = FLAGS_DIR.resolve()
        if not png_path.is_symlink() and png_path.is_file():
            resolved = png_path.resolve()
            if resolved.parent == flags_root and resolved.stat().st_size <= MAX_FLAG_ASSET_BYTES:
                with Image.open(resolved, formats=["PNG"]) as im:
                    base = im.convert("RGBA")
    except Exception:
        base = None

    _FLAG_BASE_CACHE[code] = base
    return base


def get_country_flag(code: str, w: int = 21, h: int = 15) -> Image.Image:
    raw = (code or "br").strip().lower()[:4]
    c = FLAG_CODE_ALIASES.get(raw, raw)
    ck = (c, w, h)
    cached = _FLAG_CACHE.get(ck)
    if cached is not None:
        return cached

    asset = _load_flag_asset(c) or _load_flag_asset("un")
    if asset is not None:
        resized = asset.resize((w, h), Image.Resampling.LANCZOS)
        ImageDraw.Draw(resized).rectangle((0, 0, w - 1, h - 1), outline=(0, 0, 0, 110), width=1)
        _FLAG_CACHE[ck] = resized
        return resized

    # Fallback vetorial mínimo (Brasil / genérico)
    sw, sh = w * 2, h * 2
    im = Image.new("RGBA", (sw, sh), (45, 50, 62, 255))
    d = ImageDraw.Draw(im)
    if c == "br":
        d.rectangle((0, 0, sw - 1, sh - 1), fill=(0, 156, 59, 255))
        d.polygon([(sw // 2, 3), (sw - 4, sh // 2), (sw // 2, sh - 4), (4, sh // 2)], fill=(255, 223, 0, 255))
        r = max(4, sh // 4)
        d.ellipse((sw // 2 - r, sh // 2 - r, sw // 2 + r, sh // 2 + r), fill=(0, 39, 118, 255))
    else:
        lbl = (c[:2] or "BR").upper()
        fnt = pick_font(lbl, size=max(10, sh // 2), bold=True)
        bb = d.textbbox((0, 0), lbl, font=fnt)
        d.text(((sw - (bb[2] - bb[0])) / 2, (sh - (bb[3] - bb[1])) / 2 - 1), lbl, font=fnt, fill=(209, 213, 219, 255))
    d.rectangle((0, 0, sw - 1, sh - 1), outline=(0, 0, 0, 110), width=1)
    final = im.resize((w, h), Image.Resampling.LANCZOS)
    _FLAG_CACHE[ck] = final
    return final


def draw_flag_with_expat_badge(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    country_code: str,
    x: int,
    y: int,
    *,
    flag_w: int = 21,
    flag_h: int = 15,
    compact: bool = False,
) -> int:
    """
    Desenha a bandeira do país e, para brasileiros emigrados (`country_code != 'br'`),
    adiciona a pílula verde-e-ouro `BR EXT` logo ao lado da bandeira.
    Retorna a largura horizontal total consumida (em pixels).
    """
    c = (country_code or "br").strip().lower()
    flag_im = get_country_flag(c, w=flag_w, h=flag_h)
    img.paste(flag_im, (x, y), flag_im)
    if not c or c == "br":
        return flag_w

    label = "BR EXT"
    font_sz = 9 if compact else 10
    pill_w = 36 if compact else 42
    pill_h = max(flag_h, 13 if compact else 15)
    gap = 4
    px0 = x + flag_w + gap
    py0 = y + (flag_h - pill_h) // 2
    draw.rounded_rectangle(
        (px0, py0, px0 + pill_w, py0 + pill_h),
        radius=3,
        fill=(16, 62, 38, 255),
        outline=(234, 179, 8, 235),
        width=1,
    )
    fnt = pick_font(label, size=font_sz, bold=True, condensed=True)
    bbox = draw.textbbox((0, 0), label, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = px0 + (pill_w - tw) / 2 - bbox[0]
    ty = py0 + (pill_h - th) / 2 - bbox[1]
    draw.text((tx, ty), label, font=fnt, fill=(253, 224, 71, 255))
    return flag_w + gap + pill_w


def get_veteranos_badge_icon(size: int = 20) -> Image.Image:
    sz = max(12, min(int(size), 128))
    cached = _VETERANOS_BADGE_CACHE.get(sz)
    if cached is not None:
        return cached

    loaded: Image.Image | None = None
    try:
        if (
            VETERANOS_BADGE_PATH.is_file()
            and not VETERANOS_BADGE_PATH.is_symlink()
            and VETERANOS_BADGE_PATH.stat().st_size <= 262_144
        ):
            with Image.open(VETERANOS_BADGE_PATH, formats=["PNG"]) as raw_im:
                raw_im.load()
                loaded = raw_im.convert("RGBA").resize((sz, sz), Image.Resampling.LANCZOS)
    except Exception as exc:
        logger.warning("Fallback vetorial ativado para patente Veteranos: %s", redact_secrets(exc))

    if loaded is None:
        ss = sz * 4
        fb = Image.new("RGBA", (ss, ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(fb)
        gold = (242, 193, 68, 255)
        cx = ss // 2
        d.polygon([(cx, int(ss * 0.08)), (int(ss * 0.88), int(ss * 0.42)), (int(ss * 0.88), int(ss * 0.56)), (cx, int(ss * 0.24)), (int(ss * 0.12), int(ss * 0.56)), (int(ss * 0.12), int(ss * 0.42))], fill=gold)
        d.polygon([(cx, int(ss * 0.34)), (int(ss * 0.88), int(ss * 0.68)), (int(ss * 0.88), int(ss * 0.82)), (cx, int(ss * 0.50)), (int(ss * 0.12), int(ss * 0.82)), (int(ss * 0.12), int(ss * 0.68))], fill=gold)
        d.polygon([(cx, int(ss * 0.62)), (int(ss * 0.68), int(ss * 0.78)), (cx, int(ss * 0.94)), (int(ss * 0.32), int(ss * 0.78))], fill=gold)
        loaded = fb.resize((sz, sz), Image.Resampling.LANCZOS)

    _VETERANOS_BADGE_CACHE[sz] = loaded
    return loaded


def _fit_alias_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_px: int,
    base_size: int = 17,
    min_size: int = 13,
    bold: bool = True,
) -> tuple[str, ImageFont.ImageFont, int]:
    """Ajusta o tamanho da fonte (até min_size) para evitar truncar nomes longos como 'Smurf (ContaOriginal)'."""
    clean = strip_unsafe_chars(text).strip() or "Jogador"
    for sz in range(base_size, min_size - 1, -1):
        fnt = pick_font(clean, size=sz, bold=bold)
        if text_width(draw, clean, fnt) <= max_px:
            return clean, fnt, sz
    trunc, fnt = truncate_to_px(draw, clean, max_px=max_px, size=min_size, bold=bold)
    return trunc, fnt, min_size


def draw_bg_streaks(draw: ImageDraw.ImageDraw, width: int, height: int, seed_bytes: bytes) -> None:
    """Desenha linhas diagonais sutis no cabeçalho das tabelas PNG (determinísticas por seed)."""
    rng = random.Random(int.from_bytes(hashlib.sha256(seed_bytes).digest()[:8], "big"))
    for _ in range(10):
        x0, y0 = rng.randint(0, width), rng.randint(0, height)
        x1, y1 = x0 + rng.randint(-160, 160), y0 + rng.randint(-30, 30)
        draw.line([(x0, y0), (x1, y1)], fill=(46, 41, 33, 95), width=1)


# ---------------------------------------------------------------------------
# Renderizador da Tabela Tática — Ranking Unificado BR (Skill Points)
# ---------------------------------------------------------------------------
def render_unified_ranking_table_png(
    entries: list[dict[str, Any]],
    avatar_provider: SteamAvatarProvider,
    max_rows: int = 25,
    subtitle_override: str | None = None,
    unique_roster_count: int | None = None,
) -> bytes:
    """Renderiza uma página da tabela do Ranking Unificado BR em Skill Points (1040px de largura)."""
    width = 1040
    banner_h = 38
    col_hdr_h = 34
    row_h = 36

    rows = entries[:max_rows]
    total_h = banner_h + col_hdr_h + max(1, len(rows)) * row_h + 4
    img = Image.new("RGBA", (width, total_h), (15, 17, 21, 255))
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, width, banner_h + col_hdr_h), fill=(22, 20, 17, 255))
    draw_bg_streaks(draw, width, banner_h + col_hdr_h, b"ranking_unificado_skill_points")

    core_cnt = unique_roster_count if unique_roster_count is not None else count_unique_roster_players()
    sub_txt = subtitle_override or f"Veteranos ({len(rows)}/{core_cnt})"
    s_fnt = pick_font(sub_txt, size=14, bold=True)
    sw = text_width(draw, sub_txt, s_fnt)

    title_txt = "COH3 BRASIL  •  RANKING UNIFICADO (SKILL POINTS)"
    t_fnt = pick_font(title_txt, size=16, bold=True, condensed=True)
    header_badge = get_veteranos_badge_icon(size=22)
    img.paste(header_badge, (16, 8), header_badge)
    title_x = 44
    max_title_w = width - title_x - 24 - sw
    if text_width(draw, title_txt, t_fnt) > max_title_w:
        title_txt = "COH3 BRASIL • RANKING UNIFICADO"
        title_txt, t_fnt = truncate_to_px(draw, title_txt, int(max_title_w), size=15, bold=True)

    draw.text((title_x, 10), title_txt, font=t_fnt, fill=(245, 184, 46, 255))
    draw.text((width - 20 - sw, 11), sub_txt, font=s_fnt, fill=(190, 184, 172, 255))
    draw.line([(0, banner_h), (width, banner_h)], fill=(48, 44, 36, 255), width=1)

    gold_col = (245, 184, 46, 255)
    ch_y = banner_h + 7
    mode_col_xs: tuple[tuple[str, int], ...] = (
        ("1v1", 508),
        ("2v2", 576),
        ("3v3", 644),
        ("4v4", 712),
    )
    for lbl, cx, align in (
        ("POS.", 40, "center"),
        ("SKILL ▾", 114, "center"),
        ("JOGADOR", 166, "left"),
        ("1V1", 508, "center"),
        ("2V2", 576, "center"),
        ("3V3", 644, "center"),
        ("4V4", 712, "center"),
        ("TAXA", 816, "right"),
        ("JOGOS", 900, "right"),
        ("ÚLT. JOGO", 1022, "right"),
    ):
        hdr_fnt = pick_font(lbl, size=15, bold=True, condensed=True)
        lw = text_width(draw, lbl, hdr_fnt)
        tx = (cx - lw / 2) if align == "center" else ((cx - lw) if align == "right" else cx)
        draw.text((tx, ch_y), lbl, font=hdr_fnt, fill=gold_col)

    rule_y = banner_h + col_hdr_h - 1
    draw.line([(10, rule_y), (width - 10, rule_y)], fill=(234, 179, 8, 255), width=2)

    if not rows:
        empty_msg = "Sem veteranos calibrados (mínimo de 10 jogos em ao menos uma fila)."
        efnt = pick_font(empty_msg, size=16, bold=False)
        ew = text_width(draw, empty_msg, efnt)
        draw.text(((width - ew) / 2, rule_y + 9), empty_msg, font=efnt, fill=(156, 163, 175, 255))

    y = banner_h + col_hdr_h
    num_fnt = pick_font("1566", size=17, bold=True, condensed=True)
    skill_fnt = pick_font("2523", size=18, bold=True, condensed=True)
    mode_fnt = pick_font("1614", size=16, bold=True, condensed=True)
    meta_fnt = pick_font("19/11/2026", size=16, bold=True, condensed=True)
    row_vet_badge = get_veteranos_badge_icon(size=19)

    white = (243, 244, 246, 255)
    bright_white = (255, 255, 255, 255)
    inactive_text = (214, 218, 226, 255)
    sec_mode_active = (206, 212, 222, 255)
    sec_mode_inactive = (178, 184, 194, 255)
    dash_col = (94, 102, 116, 255)

    active_bg_even = (17, 19, 24, 255)
    active_bg_odd = (24, 27, 34, 255)
    active_div = (34, 38, 46, 255)

    inactive_bg_even = (40, 23, 29, 255)
    inactive_bg_odd = (48, 28, 35, 255)
    inactive_div = (70, 40, 49, 255)
    inactive_accent = (175, 72, 84, 255)

    anchor_pill_active = ((58, 45, 18, 255), (218, 165, 32, 215), (250, 204, 21, 255))
    anchor_pill_inactive = ((66, 42, 34, 255), (186, 136, 68, 190), (232, 198, 128, 255))

    prev_inactive = False
    for idx, entry in enumerate(rows):
        is_custom = bool(entry.get("is_custom"))
        is_inactive = bool(entry.get("inactive_fallback"))
        rank_val = int(entry.get("br_rank", idx + 1))

        if is_inactive:
            r_bg = inactive_bg_even if idx % 2 == 0 else inactive_bg_odd
            row_div = inactive_div
        else:
            r_bg = active_bg_even if idx % 2 == 0 else active_bg_odd
            row_div = active_div

        draw.rectangle((0, y, width, y + row_h), fill=r_bg)
        if is_inactive:
            if not prev_inactive and idx > 0:
                draw.line([(0, y), (width, y)], fill=inactive_accent, width=2)
            draw.rectangle((0, y, 4, y + row_h), fill=inactive_accent)
        draw.line([(10, y + row_h - 1), (width - 10, y + row_h - 1)], fill=row_div, width=1)
        prev_inactive = is_inactive

        skill_int = int(round(float(entry.get("skill_points", 0))))
        anchor_mode = str(entry.get("anchor_mode") or "")
        mode_max_elos = entry.get("mode_max_elos") or {}
        wins = int(entry.get("wins", 0))
        losses = int(entry.get("losses", 0))
        total = int(entry.get("total", wins + losses))
        ratio = int(entry.get("ratio", round((wins * 100.0) / total) if total > 0 else 0))
        lmd = int(entry.get("last_match_date", 0))
        last_game_str = datetime.fromtimestamp(lmd, tz=BRT_TZ).strftime("%d/%m/%Y") if lmd > 0 else "—"

        row_text_col = inactive_text if is_inactive else white
        rank_color = (250, 204, 21, 255) if (rank_val == 1 and not is_inactive) else row_text_col
        r_str = str(rank_val)
        rw = text_width(draw, r_str, num_fnt)
        draw.text((40 - rw / 2, y + 8), r_str, font=num_fnt, fill=rank_color)

        sp_col = (250, 204, 21, 255) if (rank_val == 1 and not is_inactive) else (inactive_text if is_inactive else bright_white)
        sp_str = str(skill_int)
        spw = text_width(draw, sp_str, skill_fnt)
        draw.text((114 - spw / 2, y + 7), sp_str, font=skill_fnt, fill=sp_col)

        pid = int(entry.get("profile_id", 0))
        sid = str(entry.get("steam_id") or "")
        alias_raw = str(entry.get("alias") or f"Jogador {pid}")
        country = str(entry.get("country") or "br").lower()
        is_smurf = bool(entry.get("is_smurf") or entry.get("smurf_of"))

        if is_smurf:
            ring_col = (108, 152, 178, 220) if is_inactive else (56, 189, 248, 245)
        elif is_custom:
            ring_col = (176, 136, 92, 220) if is_inactive else (245, 184, 46, 245)
        else:
            ring_col = (34, 197, 94, 230)
        av_im = avatar_provider.get_circular_avatar(
            size=30,
            profile_id=pid,
            steam_id=sid,
            alias=alias_raw,
            ring_color=ring_col,
        )
        img.paste(av_im, (166, y + 3), av_im)

        flag_used_w = draw_flag_with_expat_badge(
            img,
            draw,
            country,
            202,
            y + 11,
            flag_w=21,
            flag_h=15,
            compact=False,
        )
        alias_x = 202 + flag_used_w + 7
        extra_w = max(0, alias_x - 230)

        disp_alias, a_fnt, a_sz = _fit_alias_text(
            draw,
            alias_raw,
            max_px=max(120, (216 if is_custom else 238) - extra_w),
            base_size=17,
            min_size=13,
            bold=True,
        )
        if is_smurf:
            name_col = (166, 200, 218, 255) if is_inactive else (186, 230, 253, 255)
        elif is_custom:
            name_col = (226, 206, 158, 255) if is_inactive else (253, 230, 138, 255)
        else:
            name_col = row_text_col
        alias_y = y + 8 + max(0, (17 - a_sz) // 2)
        draw.text((alias_x, alias_y), disp_alias, font=a_fnt, fill=name_col)

        if is_custom:
            bx = int(alias_x + text_width(draw, disp_alias, a_fnt) + 6)
            img.paste(row_vet_badge, (bx, y + 8), row_vet_badge)

        # Colunas de ELO base por modo (1v1, 2v2, 3v3, 4v4) com destaque dourado no modo âncora
        p_bg, p_border, p_text = anchor_pill_inactive if is_inactive else anchor_pill_active
        sec_col = sec_mode_inactive if is_inactive else sec_mode_active
        for m_key, m_cx in mode_col_xs:
            m_val = mode_max_elos.get(m_key)
            if m_val is None:
                dw = text_width(draw, "—", mode_fnt)
                draw.text((m_cx - dw / 2, y + 9), "—", font=mode_fnt, fill=dash_col)
                continue

            m_str = str(int(round(float(m_val))))
            if m_key == anchor_mode:
                pill_w, pill_h = 56, 24
                px0 = m_cx - pill_w // 2
                py0 = y + (row_h - pill_h) // 2
                draw.rounded_rectangle(
                    (px0, py0, px0 + pill_w, py0 + pill_h),
                    radius=5,
                    fill=p_bg,
                    outline=p_border,
                    width=1,
                )
                bbox = draw.textbbox((0, 0), m_str, font=mode_fnt)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                draw.text(
                    (px0 + (pill_w - tw) / 2 - bbox[0], py0 + (pill_h - th) / 2 - bbox[1]),
                    m_str,
                    font=mode_fnt,
                    fill=p_text,
                )
            else:
                mw = text_width(draw, m_str, mode_fnt)
                draw.text((m_cx - mw / 2, y + 9), m_str, font=mode_fnt, fill=sec_col)

        for val_str, right_x in (
            (f"{ratio}%", 816),
            (str(total), 900),
            (last_game_str, 1022),
        ):
            vw = text_width(draw, val_str, meta_fnt)
            draw.text((right_x - vw, y + 9), val_str, font=meta_fnt, fill=row_text_col)

        y += row_h

    return _encode_rgb_png(img)


# ---------------------------------------------------------------------------
# Renderizador da Tabela Tática — Times Brasileiros 2v2 Arranjados (COH3 Stats)
# ---------------------------------------------------------------------------
def _draw_side_pill(
    draw: ImageDraw.ImageDraw,
    cx: int,
    top_y: int,
    pill_w: int,
    pill_h: int,
    label: str,
    bg_col: tuple[int, int, int, int],
    border_col: tuple[int, int, int, int],
    text_col: tuple[int, int, int, int],
    font_size: int = 12,
) -> None:
    x0 = int(cx - pill_w // 2)
    y0 = int(top_y)
    x1 = x0 + int(pill_w)
    y1 = y0 + int(pill_h)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=4, fill=bg_col, outline=border_col, width=1)
    fnt = pick_font(label, size=font_size, bold=True, condensed=True)
    bbox = draw.textbbox((0, 0), label, font=fnt)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    tx = x0 + (pill_w - tw) / 2 - bbox[0]
    ty = y0 + (pill_h - th) / 2 - bbox[1]
    draw.text((tx, ty), label, font=fnt, fill=text_col)


def render_arranged_teams_table_png(
    teams: list[dict[str, Any]],
    avatar_provider: SteamAvatarProvider,
    max_rows: int = 18,
    subtitle_override: str | None = None,
    inactive_days: int = 30,
    min_total_games: int = 10,
) -> bytes:
    """Renderiza uma página da tabela do Ranking de Times Brasileiros 2v2 Arranjados (980px de largura)."""
    width = 980
    banner_h = 36
    col_hdr_h = 34
    row_h = 54

    rows = teams[:max_rows]
    total_h = banner_h + col_hdr_h + max(1, len(rows)) * row_h + 4
    img = Image.new("RGBA", (width, total_h), (15, 17, 21, 255))
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, width, banner_h + col_hdr_h), fill=(22, 20, 17, 255))
    draw_bg_streaks(draw, width, banner_h + col_hdr_h, b"arranged_2v2_br")

    if subtitle_override:
        sub_txt = subtitle_override
    else:
        sub_txt = f"Top {len(rows)} Ativas (≤{int(inactive_days)}d) • Mín. {int(min_total_games)} jogos"
    s_fnt = pick_font(sub_txt, size=14, bold=True)
    sw = text_width(draw, sub_txt, s_fnt)

    title_txt = "COH3 BRASIL  •  TIMES ARRANJADOS 2V2 (MÉDIA ELO)"
    t_fnt = pick_font(title_txt, size=16, bold=True, condensed=True)
    header_badge = get_veteranos_badge_icon(size=22)
    img.paste(header_badge, (16, 7), header_badge)
    title_x = 44
    max_title_w = width - title_x - 24 - sw
    if text_width(draw, title_txt, t_fnt) > max_title_w:
        title_txt = "COH3 BRASIL • TIMES ARRANJADOS 2V2"
        title_txt, t_fnt = truncate_to_px(draw, title_txt, int(max_title_w), size=15, bold=True)

    draw.text((title_x, 9), title_txt, font=t_fnt, fill=(245, 184, 46, 255))
    draw.text((width - 20 - sw, 10), sub_txt, font=s_fnt, fill=(190, 184, 172, 255))
    draw.line([(0, banner_h), (width, banner_h)], fill=(48, 44, 36, 255), width=1)

    gold_col = (245, 184, 46, 255)
    ch_y = banner_h + 7
    for lbl, cx, align in (
        ("POS.", 38, "center"),
        ("ELO ▾", 104, "center"),
        ("DUPLA (2V2)", 146, "left"),
        ("LADO", 468, "center"),
        ("SEQ.", 564, "right"),
        ("VITÓRIAS ▾", 682, "right"),
        ("DERROTAS", 762, "right"),
        ("TAXA", 818, "right"),
        ("JOGOS", 876, "right"),
        ("ÚLT. JOGO", 966, "right"),
    ):
        hdr_fnt = pick_font(lbl, size=15, bold=True, condensed=True)
        lw = text_width(draw, lbl, hdr_fnt)
        tx = (cx - lw / 2) if align == "center" else ((cx - lw) if align == "right" else cx)
        draw.text((tx, ch_y), lbl, font=hdr_fnt, fill=gold_col)

    rule_y = banner_h + col_hdr_h - 1
    draw.line([(10, rule_y), (width - 10, rule_y)], fill=(234, 179, 8, 255), width=2)

    if not rows:
        empty_msg = f"Nenhuma dupla brasileira 2v2 ativa (≤{int(inactive_days)}d • mín. {int(min_total_games)} jogos)."
        efnt = pick_font(empty_msg, size=16, bold=False)
        ew = text_width(draw, empty_msg, efnt)
        draw.text(((width - ew) / 2, rule_y + 17), empty_msg, font=efnt, fill=(156, 163, 175, 255))

    y = banner_h + col_hdr_h
    num_fnt = pick_font("1566", size=17, bold=True, condensed=True)
    row_vet_badge = get_veteranos_badge_icon(size=16)
    white = (243, 244, 246, 255)

    br_bg_even = (17, 19, 24, 255)
    br_bg_odd = (24, 27, 34, 255)
    br_div = (34, 38, 46, 255)

    vet_bg_even = (35, 29, 18, 255)
    vet_bg_odd = (42, 35, 22, 255)
    vet_div = (64, 54, 35, 255)
    vet_accent = (245, 184, 46, 245)

    axis_colors = ((86, 28, 32, 255), (196, 76, 84, 255), (254, 202, 202, 255))
    allies_colors = ((24, 54, 92, 255), (76, 146, 232, 255), (191, 219, 254, 255))

    for idx, team in enumerate(rows):
        has_vet = bool(team.get("has_veterano"))
        rank_val = int(team.get("br_rank", idx + 1))

        if has_vet:
            r_bg = vet_bg_even if idx % 2 == 0 else vet_bg_odd
            row_div = vet_div
        else:
            r_bg = br_bg_even if idx % 2 == 0 else br_bg_odd
            row_div = br_div

        draw.rectangle((0, y, width, y + row_h), fill=r_bg)
        if has_vet:
            draw.rectangle((0, y, 4, y + row_h), fill=vet_accent)
        draw.line([(10, y + row_h - 1), (width - 10, y + row_h - 1)], fill=row_div, width=1)

        elo_int = int(round(float(team.get("avg_elo", 0))))
        wins = int(team.get("wins", 0))
        losses = int(team.get("losses", 0))
        total = int(team.get("total", wins + losses))
        ratio = int(team.get("ratio", round((wins * 100.0) / total) if total > 0 else 0))
        streak = int(team.get("streak", 0))
        lmd = int(team.get("last_match_date", 0))
        last_game_str = datetime.fromtimestamp(lmd, tz=BRT_TZ).strftime("%d/%m/%Y") if lmd > 0 else "—"

        row_text_col = white
        rank_color = (250, 204, 21, 255) if rank_val == 1 else row_text_col
        r_str = str(rank_val)
        rw = text_width(draw, r_str, num_fnt)
        draw.text((38 - rw / 2, y + 17), r_str, font=num_fnt, fill=rank_color)

        e_str = str(elo_int)
        ew = text_width(draw, e_str, num_fnt)
        draw.text((104 - ew / 2, y + 17), e_str, font=num_fnt, fill=row_text_col)

        # Renderizar os 2 jogadores da dupla empilhados
        players = list(team.get("players") or [])[:2]
        for p_idx, pl in enumerate(players):
            py = y + (4 if p_idx == 0 else 29)
            pid = int(pl.get("profile_id", 0))
            sid = str(pl.get("steam_id") or "")
            alias_raw = str(pl.get("alias") or f"Jogador {pid}")
            country = str(pl.get("country") or "br").lower()
            is_vet = bool(pl.get("is_veterano"))
            is_smurf = bool(pl.get("is_smurf") or pl.get("smurf_of"))

            if is_smurf:
                ring_col = (56, 189, 248, 245)
            elif is_vet:
                ring_col = (245, 184, 46, 245)
            else:
                ring_col = (34, 197, 94, 230)
            av_im = avatar_provider.get_circular_avatar(
                size=22,
                profile_id=pid,
                steam_id=sid,
                alias=alias_raw,
                ring_color=ring_col,
            )
            img.paste(av_im, (146, py), av_im)

            flag_used_w = draw_flag_with_expat_badge(
                img,
                draw,
                country,
                173,
                py + 5,
                flag_w=18,
                flag_h=13,
                compact=True,
            )
            alias_x = 173 + flag_used_w + 6
            extra_w = max(0, alias_x - 197)

            disp_alias, a_fnt, a_sz = _fit_alias_text(
                draw,
                alias_raw,
                max_px=max(115, (196 if is_vet else 214) - extra_w),
                base_size=15,
                min_size=11,
                bold=True,
            )
            if is_smurf:
                name_col = (186, 230, 253, 255)
            elif is_vet:
                name_col = (253, 230, 138, 255)
            else:
                name_col = row_text_col
            alias_py = py + 2 + max(0, (15 - a_sz) // 2)
            draw.text((alias_x, alias_py), disp_alias, font=a_fnt, fill=name_col)

            if is_vet:
                bx = int(alias_x + text_width(draw, disp_alias, a_fnt) + 6)
                img.paste(row_vet_badge, (bx, py + 3), row_vet_badge)

        # Selo de lado (Eixo / Aliados / ambos com ELO individual por lado)
        sides = [str(s).lower() for s in (team.get("sides") or [])]
        if "axis" in sides and "allies" in sides:
            ax_elo = team.get("axis_elo")
            al_elo = team.get("allies_elo")
            ax_lbl = f"EIXO {ax_elo}" if ax_elo is not None else "EIXO"
            al_lbl = f"ALIADOS {al_elo}" if al_elo is not None else "ALIADOS"
            _draw_side_pill(draw, 468, y + 6, 98, 19, ax_lbl, *axis_colors, font_size=11)
            _draw_side_pill(draw, 468, y + 29, 98, 19, al_lbl, *allies_colors, font_size=11)
        elif "axis" in sides:
            _draw_side_pill(draw, 468, y + 16, 82, 22, "EIXO", *axis_colors, font_size=12)
        elif "allies" in sides:
            _draw_side_pill(draw, 468, y + 16, 82, 22, "ALIADOS", *allies_colors, font_size=12)

        if streak > 0:
            st_str, st_col = f"+{streak}", (74, 222, 128, 255)
        elif streak < 0:
            st_str, st_col = f"{streak}", (248, 113, 113, 255)
        else:
            st_str, st_col = "0", (156, 163, 175, 255)
        stw = text_width(draw, st_str, num_fnt)
        draw.text((564 - stw, y + 17), st_str, font=num_fnt, fill=st_col)

        for val_str, right_x in (
            (str(wins), 682),
            (str(losses), 762),
            (f"{ratio}%", 818),
            (str(total), 876),
            (last_game_str, 966),
        ):
            vw = text_width(draw, val_str, num_fnt)
            draw.text((right_x - vw, y + 17), val_str, font=num_fnt, fill=row_text_col)

        y += row_h

    return _encode_rgb_png(img)


# ---------------------------------------------------------------------------
# Renderizador da Tabela Tática — Campeões BR por Battlegroup (CoH3 Automatch)
# ---------------------------------------------------------------------------
_BG_ICON_CACHE: dict[tuple[str, int], Image.Image] = {}

_FACTION_BANNER_STYLE: dict[str, dict[str, Any]] = {
    "american": {
        "label": "US FORCES  •  7 BATTLEGROUPS",
        "flag": "us",
        "accent": (59, 130, 246, 255),
        "bg": (20, 29, 44, 255),
        "title_col": (147, 197, 253, 255),
    },
    "british": {
        "label": "BRITISH FORCES  •  7 BATTLEGROUPS",
        "flag": "gb",
        "accent": (16, 185, 129, 255),
        "bg": (17, 33, 30, 255),
        "title_col": (110, 231, 183, 255),
    },
    "german": {
        "label": "WEHRMACHT  •  7 BATTLEGROUPS",
        "flag": "de",
        "accent": (239, 68, 68, 255),
        "bg": (39, 21, 25, 255),
        "title_col": (252, 165, 165, 255),
    },
    "afrika_corps": {
        "label": "DEUTSCHES AFRIKAKORPS  •  7 BATTLEGROUPS",
        "flag": "de",
        "accent": (245, 158, 11, 255),
        "bg": (39, 30, 17, 255),
        "title_col": (253, 224, 71, 255),
    },
}


def get_battlegroup_icon(icon_slug: str, size: int = 34) -> Image.Image:
    """Carrega ícone PNG oficial do Battlegroup em assets/battlegroups/ com proteção anti-traversal."""
    slug = (icon_slug or "").strip().lower()
    sz = max(16, min(int(size), 64))
    ck = (slug, sz)
    cached = _BG_ICON_CACHE.get(ck)
    if cached is not None:
        return cached

    loaded: Image.Image | None = None
    if SAFE_BG_SLUG_RE.fullmatch(slug):
        png_path = BG_ICONS_DIR / f"{slug}.png"
        try:
            icons_root = BG_ICONS_DIR.resolve()
            if not png_path.is_symlink() and png_path.is_file():
                resolved = png_path.resolve()
                if resolved.parent == icons_root and resolved.stat().st_size <= MAX_BG_ICON_BYTES:
                    with Image.open(resolved, formats=["PNG"]) as im:
                        im.load()
                        loaded = im.convert("RGBA").resize((sz, sz), Image.Resampling.LANCZOS)
        except Exception:
            loaded = None

    if loaded is None:
        im = Image.new("RGBA", (sz, sz), (34, 38, 48, 255))
        d = ImageDraw.Draw(im)
        d.rectangle((0, 0, sz - 1, sz - 1), outline=(218, 165, 32, 200), width=1)
        fnt = pick_font("BG", size=max(10, sz // 3), bold=True)
        bb = d.textbbox((0, 0), "BG", font=fnt)
        d.text(((sz - (bb[2] - bb[0])) / 2 - bb[0], (sz - (bb[3] - bb[1])) / 2 - bb[1]), "BG", font=fnt, fill=(245, 184, 46, 255))
        loaded = im

    if len(_BG_ICON_CACHE) >= 128:
        _BG_ICON_CACHE.clear()
    _BG_ICON_CACHE[ck] = loaded
    return loaded


def render_battlegroup_champions_png(
    entries: list[dict[str, Any]],
    avatar_provider: SteamAvatarProvider,
    subtitle_override: str | None = None,
    candidates_evaluated: int = 0,
) -> bytes:
    """Renderiza uma página (ex.: Aliados 14 BGs ou Eixo 14 BGs) do Painel de Campeões BR por Battlegroup (1040px)."""
    width = 1040
    banner_h = 38
    col_hdr_h = 34
    section_h = 28
    row_h = 44
    footer_h = 24

    # Agrupa por facção preservando a ordem recebida
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for entry in entries:
        fac = str(entry.get("faction") or "american")
        if not groups or groups[-1][0] != fac:
            groups.append((fac, [entry]))
        else:
            groups[-1][1].append(entry)

    total_rows = sum(len(g_rows) for _, g_rows in groups)
    total_h = banner_h + col_hdr_h + len(groups) * section_h + max(1, total_rows) * row_h + footer_h
    img = Image.new("RGBA", (width, total_h), (15, 17, 21, 255))
    draw = ImageDraw.Draw(img)

    # Banner superior (mesma identidade visual do Ranking Unificado BR)
    draw.rectangle((0, 0, width, banner_h + col_hdr_h), fill=(22, 20, 17, 255))
    draw_bg_streaks(draw, width, banner_h + col_hdr_h, b"battlegroup_champions_br")

    header_badge = get_veteranos_badge_icon(size=22)
    img.paste(header_badge, (16, 8), header_badge)
    title_txt = "COH3 BRASIL  •  CAMPEÕES POR BATTLEGROUP (AUTOMATCH)"
    t_fnt = pick_font(title_txt, size=16, bold=True, condensed=True)
    sub_txt = subtitle_override or f"{total_rows} Battlegroups"
    s_fnt = pick_font(sub_txt, size=14, bold=True)
    sw = text_width(draw, sub_txt, s_fnt)
    max_tw = width - 44 - 24 - sw
    if text_width(draw, title_txt, t_fnt) > max_tw:
        title_txt, t_fnt = truncate_to_px(draw, "COH3 BRASIL • CAMPEÕES BATTLEGROUP", int(max_tw), size=15, bold=True)
    draw.text((44, 10), title_txt, font=t_fnt, fill=(245, 184, 46, 255))
    draw.text((width - 20 - sw, 11), sub_txt, font=s_fnt, fill=(190, 184, 172, 255))
    draw.line([(0, banner_h), (width, banner_h)], fill=(48, 44, 36, 255), width=1)

    # Cabeçalho das colunas
    gold_col = (245, 184, 46, 255)
    ch_y = banner_h + 8
    for lbl, cx, align in (
        ("BATTLEGROUP", 14, "left"),
        ("#1 CAMPEÃO BR", 226, "left"),
        ("SCORE ▾", 454, "center"),
        ("ELO/MODO", 522, "center"),
        ("WR% • L30", 604, "center"),
        ("APM", 670, "center"),
        ("DANO/EFF", 728, "center"),
        ("MAPA/STP", 798, "center"),
        ("VS ELO+", 872, "center"),
        ("#2 VICE BR", 916, "left"),
    ):
        hfnt = pick_font(lbl, size=13, bold=True, condensed=True)
        lw = text_width(draw, lbl, hfnt)
        tx = (cx - lw / 2) if align == "center" else ((cx - lw) if align == "right" else cx)
        draw.text((tx, ch_y), lbl, font=hfnt, fill=gold_col)

    rule_y = banner_h + col_hdr_h - 1
    draw.line([(10, rule_y), (width - 10, rule_y)], fill=(234, 179, 8, 255), width=2)

    y = banner_h + col_hdr_h
    if not groups:
        empty_msg = "Nenhum Battlegroup calculado ainda (aguardando coleta Automatch)."
        efnt = pick_font(empty_msg, size=15, bold=False)
        ew = text_width(draw, empty_msg, efnt)
        draw.text(((width - ew) / 2, y + 12), empty_msg, font=efnt, fill=(156, 163, 175, 255))
        y += row_h

    row_vet_badge = get_veteranos_badge_icon(size=17)
    score_fnt = pick_font("808", size=15, bold=True, condensed=True)
    num_top_fnt = pick_font("1662", size=15, bold=True, condensed=True)
    sub_micro_fnt = pick_font("70.8%", size=11, bold=True, condensed=True)
    sub_tiny_fnt = pick_font("CINTURAO", size=10, bold=True, condensed=True)
    apm_fnt = pick_font("109", size=15, bold=True, condensed=True)
    vice_sub_fnt = pick_font("742 pts • 68%", size=11, bold=False)

    bg_even = (17, 19, 24, 255)
    bg_odd = (23, 26, 33, 255)
    div_col = (34, 38, 46, 255)
    white = (243, 244, 246, 255)
    muted = (148, 163, 184, 255)

    for fac, g_rows in groups:
        fstyle = _FACTION_BANNER_STYLE.get(
            fac,
            {
                "label": fac.upper(),
                "flag": "un",
                "accent": (234, 179, 8, 255),
                "bg": (28, 28, 34, 255),
                "title_col": (245, 184, 46, 255),
            },
        )
        draw.rectangle((0, y, width, y + section_h), fill=fstyle["bg"])
        draw.rectangle((0, y, 5, y + section_h), fill=fstyle["accent"])
        flag_im = get_country_flag(fstyle["flag"], w=20, h=14)
        img.paste(flag_im, (14, y + 7), flag_im)
        sec_fnt = pick_font(fstyle["label"], size=13, bold=True, condensed=True)
        draw.text((42, y + 6), fstyle["label"], font=sec_fnt, fill=fstyle["title_col"])
        draw.line([(0, y + section_h - 1), (width, y + section_h - 1)], fill=(48, 52, 62, 255), width=1)
        y += section_h

        for idx, entry in enumerate(g_rows):
            r_bg = bg_even if idx % 2 == 0 else bg_odd
            draw.rectangle((0, y, width, y + row_h), fill=r_bg)
            draw.rectangle((0, y, 3, y + row_h), fill=(*fstyle["accent"][:3], 130))
            draw.line([(10, y + row_h - 1), (width - 10, y + row_h - 1)], fill=div_col, width=1)

            # Ícone oficial do Battlegroup
            bg_ic = get_battlegroup_icon(str(entry.get("icon_slug") or ""), size=34)
            img.paste(bg_ic, (12, y + 5), bg_ic)
            draw.rectangle((12, y + 5, 45, y + 38), outline=(72, 66, 54, 190), width=1)

            # Nome do Battlegroup (limpo, sem rótulo de tema)
            bg_lbl = str(entry.get("short_name") or entry.get("bg_name") or "Battlegroup")
            disp_bg, bg_fnt, bg_sz = _fit_alias_text(draw, bg_lbl, max_px=168, base_size=14, min_size=11, bold=True)
            draw.text((52, y + 14 + max(0, (14 - bg_sz) // 2)), disp_bg, font=bg_fnt, fill=white)

            champ = entry.get("champion")
            if not isinstance(champ, dict):
                nd_fnt = pick_font("Sem dados", size=13, bold=False)
                draw.text((226, y + 14), "Sem representante calibrado", font=nd_fnt, fill=muted)
                y += row_h
                continue

            pid = int(champ.get("profile_id") or 0)
            sid = str(champ.get("steam_id") or "")
            alias_raw = str(champ.get("alias") or f"Jogador {pid}")
            is_vet = bool(champ.get("is_veterano"))
            ring_col = (245, 184, 46, 245) if is_vet else (34, 197, 94, 230)

            av_im = avatar_provider.get_circular_avatar(
                size=32,
                profile_id=pid,
                steam_id=sid,
                alias=alias_raw,
                ring_color=ring_col,
            )
            img.paste(av_im, (226, y + 6), av_im)

            belt_st = str(entry.get("belt_status") or champ.get("belt_status") or "initial")
            streak_w = int(entry.get("streak_weeks") or champ.get("streak_weeks") or 0)
            prev_holder = str(entry.get("prev_champion_alias") or champ.get("prev_champion_alias") or "")
            champ_country = str(champ.get("country") or "br").strip().lower()
            is_champ_expat = bool(champ_country and champ_country != "br")
            has_belt_sub = (belt_st == "new" and bool(prev_holder)) or (belt_st == "defended" and streak_w >= 2)
            has_sub_line = has_belt_sub or is_champ_expat

            disp_alias, a_fnt, a_sz = _fit_alias_text(
                draw,
                alias_raw,
                max_px=134 if is_vet else 156,
                base_size=14,
                min_size=11,
                bold=True,
            )
            name_col = (253, 230, 138, 255) if is_vet else white
            alias_y = (y + 5 if has_sub_line else y + 14) + max(0, (14 - a_sz) // 2)
            draw.text((264, alias_y), disp_alias, font=a_fnt, fill=name_col)
            if is_vet:
                bx = int(264 + text_width(draw, disp_alias, a_fnt) + 5)
                if bx + 17 <= 424:
                    img.paste(row_vet_badge, (bx, alias_y - 1), row_vet_badge)
            sub_x = 264
            if is_champ_expat:
                used_ex = draw_flag_with_expat_badge(
                    img,
                    draw,
                    champ_country,
                    sub_x,
                    y + 25,
                    flag_w=16,
                    flag_h=11,
                    compact=True,
                )
                sub_x += used_ex + 5
            rem_sub_w = max(40, 422 - sub_x)
            if belt_st == "new" and prev_holder:
                b_txt, b_fnt = truncate_to_px(draw, f"NOVO • ex: {prev_holder}", rem_sub_w, size=10, bold=True)
                draw.text((sub_x, y + 25), b_txt, font=b_fnt, fill=(74, 222, 128, 240))
            elif belt_st == "defended" and streak_w >= 2:
                b_txt, b_fnt = truncate_to_px(draw, f"CINTURÃO • {streak_w} sem", rem_sub_w, size=10, bold=True)
                draw.text((sub_x, y + 25), b_txt, font=b_fnt, fill=(245, 184, 46, 220))

            # Score pill
            sc_val = float(champ.get("score") or 0.0)
            sc_str = f"{int(round(sc_val))}"
            if sc_val >= 680.0:
                p_fill, p_out, p_txt = (62, 47, 16, 255), (234, 179, 8, 235), (253, 224, 71, 255)
            elif sc_val >= 600.0:
                p_fill, p_out, p_txt = (24, 46, 38, 255), (52, 211, 153, 210), (167, 243, 208, 255)
            else:
                p_fill, p_out, p_txt = (30, 38, 52, 255), (100, 116, 139, 210), (226, 232, 240, 255)
            draw.rounded_rectangle((428, y + 10, 480, y + 34), radius=6, fill=p_fill, outline=p_out, width=1)
            scw = text_width(draw, sc_str, score_fnt)
            draw.text((454 - scw / 2, y + 13), sc_str, font=score_fnt, fill=p_txt)

            # ELO / Modo (2 linhas)
            fac_elo = int(champ.get("fac_raw_elo") or 0)
            fac_mode = str(champ.get("fac_mode") or "?").upper()
            elo_s = str(fac_elo) if fac_elo > 0 else "—"
            ew = text_width(draw, elo_s, num_top_fnt)
            draw.text((522 - ew / 2, y + 5), elo_s, font=num_top_fnt, fill=white)
            mode_col = (245, 184, 46, 255) if fac_mode in ("1V1", "2V2") else (147, 197, 253, 255)
            b12_elo = int(champ.get("best_12_elo") or 0)
            b12_mode = str(champ.get("best_12_mode") or "").upper()
            if fac_mode in ("3V3", "4V4") and b12_elo > 0:
                mode_sub = f"{fac_mode} • {b12_mode} {b12_elo}"
            else:
                mode_sub = fac_mode
            ms_fnt = pick_font(mode_sub, size=10 if "•" in mode_sub else 11, bold=True, condensed=True)
            msw = text_width(draw, mode_sub, ms_fnt)
            draw.text((522 - msw / 2, y + 24), mode_sub, font=ms_fnt, fill=mode_col)

            # WR% em destaque (topo) + V-D & L30 (base) — Aproveitamento + Forma Recente > Quantidade
            bw = int(champ.get("wins") or 0)
            bl = int(champ.get("losses") or 0)
            bwr = float(champ.get("win_rate") or 0.0)
            l30_tot = int(champ.get("l30_wins") or 0) + int(champ.get("l30_losses") or 0)
            l30_wr = int(round(float(champ.get("l30_win_rate") or 0.0)))
            wr_s = f"{bwr:.1f}%"
            wr_col = (74, 222, 128, 255) if bwr >= 68.0 else ((134, 239, 172, 255) if bwr >= 60.0 else white)
            wrw = text_width(draw, wr_s, num_top_fnt)
            draw.text((604 - wrw / 2, y + 5), wr_s, font=num_top_fnt, fill=wr_col)
            if l30_tot > 0:
                vd_s = f"{bw}V-{bl}D • {l30_wr}%"
                vd_fnt = sub_tiny_fnt
            else:
                vd_s = f"{bw}V-{bl}D"
                vd_fnt = sub_micro_fnt
            vdw = text_width(draw, vd_s, vd_fnt)
            draw.text((604 - vdw / 2, y + 25), vd_s, font=vd_fnt, fill=muted)

            # APM
            apm_v = int(round(float(champ.get("apm") or 0.0)))
            apm_s = str(apm_v) if apm_v > 0 else "—"
            apm_col = (56, 189, 248, 255) if apm_v >= 95 else ((253, 224, 71, 255) if apm_v >= 85 else white)
            apw = text_width(draw, apm_s, apm_fnt)
            draw.text((670 - apw / 2, y + 13), apm_s, font=apm_fnt, fill=apm_col)

            # Dano/min + Razão de Dano (uniforme para todos os BGs, sem viés temático)
            dmg_pm = int(round(float(champ.get("dmg_pm") or 0.0)))
            dmg_rat = float(champ.get("dmg_ratio") or 0.0)
            c1_s = f"{dmg_pm}/m" if dmg_pm > 0 else "—"
            c2_s = f"x{dmg_rat:.2f}" if dmg_rat > 0 else "—"
            c2_col = (74, 222, 128, 255) if dmg_rat >= 1.25 else ((253, 224, 71, 255) if dmg_rat >= 1.10 else muted)
            c1w = text_width(draw, c1_s, num_top_fnt)
            draw.text((728 - c1w / 2, y + 5), c1_s, font=num_top_fnt, fill=white)
            c2w = text_width(draw, c2_s, sub_micro_fnt)
            draw.text((728 - c2w / 2, y + 24), c2_s, font=sub_micro_fnt, fill=c2_col)

            # Controle de Mapa (topo) + Stomp% (base)
            pcap_rat = float(champ.get("pcap_ratio") or 0.0)
            stomp_v = int(round(float(champ.get("stomp_win_rate") or 0.0)))
            mp_s = f"{pcap_rat:.2f}x" if pcap_rat > 0 else "—"
            mp_col = (56, 189, 248, 255) if pcap_rat >= 1.70 else ((74, 222, 128, 255) if pcap_rat >= 1.50 else white)
            mpw = text_width(draw, mp_s, num_top_fnt)
            draw.text((798 - mpw / 2, y + 5), mp_s, font=num_top_fnt, fill=mp_col)
            st_s = f"{stomp_v}% stp"
            st_col = (134, 239, 172, 255) if stomp_v >= 55 else muted
            stw = text_width(draw, st_s, sub_micro_fnt)
            draw.text((798 - stw / 2, y + 24), st_s, font=sub_micro_fnt, fill=st_col)

            # Vitórias contra adversários de ELO maior (VS ELO+)
            up_opps = int(champ.get("upset_opps") or 0)
            up_wins = int(champ.get("upset_wins") or 0)
            up_wr = float(champ.get("upset_win_rate") or 0.0)
            if up_opps > 0:
                u1_s = f"{int(round(up_wr))}%"
                u1_col = (
                    (253, 224, 71, 255)
                    if up_wr >= 55.0
                    else ((74, 222, 128, 255) if up_wr >= 45.0 else white)
                )
                u1w = text_width(draw, u1_s, num_top_fnt)
                draw.text((872 - u1w / 2, y + 5), u1_s, font=num_top_fnt, fill=u1_col)
                u2_s = f"{up_wins}/{up_opps}"
                u2w = text_width(draw, u2_s, sub_micro_fnt)
                draw.text((872 - u2w / 2, y + 24), u2_s, font=sub_micro_fnt, fill=muted)
            else:
                u1w = text_width(draw, "—", num_top_fnt)
                draw.text((872 - u1w / 2, y + 13), "—", font=num_top_fnt, fill=muted)

            # #2 Vice BR (2 linhas — Score + WR%, respeitando apelido oficial do roster)
            r2 = entry.get("runner_up")
            if isinstance(r2, dict):
                v_raw = str(r2.get("alias") or r2.get("live_alias") or "")
                v_name, v_fnt, _ = _fit_alias_text(draw, v_raw, max_px=112, base_size=13, min_size=11, bold=True)
                draw.text((916, y + 6), v_name, font=v_fnt, fill=(214, 220, 230, 255))
                v_wr = int(round(float(r2.get("win_rate") or 0.0)))
                v_sub = f"{int(round(float(r2.get('score') or 0)))} pts • {v_wr}%"
                draw.text((916, y + 24), v_sub, font=vice_sub_fnt, fill=muted)
            else:
                draw.text((916, y + 14), "—", font=vice_sub_fnt, fill=muted)

            y += row_h

    # Rodapé informativo compacto
    draw.rectangle((0, y, width, total_h), fill=(18, 20, 25, 255))
    draw.line([(0, y), (width, y)], fill=(44, 48, 58, 255), width=1)
    foot_l = (
        f"Automatch 1v1–4v4 • ELO+Força Rival + WR%/L30 (teto 18V) + Dano/vKD + Mapa + Anti-Carry • Base: {candidates_evaluated} BRs"
        if candidates_evaluated > 0
        else "Automatch 1v1–4v4 • ELO+Força Rival + WR%/L30 (teto 18V) + Dano/vKD + Mapa + Anti-Carry"
    )
    f_fnt = pick_font(foot_l, size=11, bold=False)
    draw.text((14, y + 5), foot_l, font=f_fnt, fill=muted)
    foot_r = datetime.now(tz=BRT_TZ).strftime("Atualizado %d/%m/%Y %H:%M BRT")
    frw = text_width(draw, foot_r, f_fnt)
    draw.text((width - 14 - frw, y + 5), foot_r, font=f_fnt, fill=muted)

    return _encode_rgb_png(img)


