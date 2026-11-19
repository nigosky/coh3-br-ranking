"""Renderizador gráfico seguro (Pillow) para as tabelas do Ranking Unificado BR (CoH3)."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from relic_ranking import ALLOWED_RELIC_HOSTS, BASE_URL as RELIC_BASE_URL
from roster import CORE_BR_ROSTER, STEAM_ID64_VALID_RE, count_unique_roster_players
from security import (
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
VETERANOS_BADGE_PATH = BASE_DIR / "assets" / "discord_veteranos_patente.png"
BRT_TZ = timezone(timedelta(hours=-3))

SAFE_FLAG_CODE_RE = re.compile(r"^[a-z]{2}$")
MAX_FLAG_ASSET_BYTES = 65_536      # 64 KB
MAX_STEAM_XML_BYTES = 65_536       # 64 KB
MAX_STEAM_AVATAR_BYTES = 524_288   # 512 KB
MAX_RELIC_LOOKUP_BYTES = 131_072   # 128 KB

STEAM_XML_AVATAR_MEDIUM_RE = re.compile(
    r"<avatarMedium>\s*<!\[CDATA\[(https://[^\s\]]+)\]\]>\s*</avatarMedium>",
    re.IGNORECASE,
)
STEAM_XML_AVATAR_FULL_RE = re.compile(
    r"<avatarFull>\s*<!\[CDATA\[(https://[^\s\]]+)\]\]>\s*</avatarFull>",
    re.IGNORECASE,
)
SAFE_AVATAR_EXT_RE = re.compile(r"\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE)

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


def format_mode_title_pt(mode_raw: str) -> str:
    if mode_raw == "Team Games":
        return "JOGOS EM EQUIPE (3V3 + 4V4)"
    return str(mode_raw).upper()


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
    """Busca e faz cache de avatares reais da Steam com validação estrita de URL, formato e dimensões."""

    def __init__(
        self,
        db: Any | None = None,
        catalog_json_path: Path | None = None,
        allow_network: bool = True,
    ) -> None:
        self.db = db
        self.allow_network = allow_network
        self.catalog_json_path = catalog_json_path or (BASE_DIR / "jogadores_br_steam.json")
        self._catalog_loaded = False
        self._lock = threading.Lock()
        self._mem_png_cache: dict[str, Image.Image] = {}
        self._circle_cache: dict[tuple[str, int, tuple[int, int, int, int] | None], Image.Image] = {}
        self._pid_to_steam: dict[int, str] = {}
        self._failed_steam_ids: set[str] = set()
        self._network_fetches_this_run = 0
        self._max_network_fetches_per_run = 100
        self._last_net_ts = 0.0

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

    def _fetch_steam_avatar_bytes(self, steam_id: str) -> tuple[str, bytes] | None:
        if not STEAM_ID64_VALID_RE.fullmatch(steam_id) or steam_id in self._failed_steam_ids:
            return None

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

                avatar_url = m.group(1).strip()
                parsed_av = validate_https_endpoint(avatar_url, ALLOWED_STEAM_AVATAR_HOSTS)
                if parsed_av.query or parsed_av.fragment or not SAFE_AVATAR_EXT_RE.search(parsed_av.path):
                    break

                img_req = urllib.request.Request(
                    avatar_url,
                    headers={"User-Agent": "CoH3-BR-Ranking-Unificado/1.0", "Accept": "image/*"},
                )
                with _NO_REDIRECT_OPENER.open(img_req, timeout=8) as img_resp:
                    img_bytes = read_bounded_http_body(img_resp, MAX_STEAM_AVATAR_BYTES)

                with Image.open(io.BytesIO(img_bytes)) as im:
                    if im.format not in ALLOWED_DECODER_FORMATS or not (1 <= im.width <= 1024 and 1 <= im.height <= 1024):
                        break
                    clean_im = im.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
                    out_buf = io.BytesIO()
                    clean_im.save(out_buf, format="PNG", compress_level=6)
                    return avatar_url, out_buf.getvalue()
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
                    with Image.open(io.BytesIO(png_blob)) as decoded:
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


# ---------------------------------------------------------------------------
# Renderizador da Tabela Tática — Ranking Unificado BR
# ---------------------------------------------------------------------------
def render_ranking_table_png(
    entries: list[dict[str, Any]],
    mode_title: str,
    avatar_provider: SteamAvatarProvider,
    max_rows: int = 30,
    subtitle_override: str | None = None,
    unique_roster_count: int | None = None,
) -> bytes:
    """Renderiza uma página da tabela do Ranking Unificado BR (980px de largura)."""
    width = 980
    banner_h = 36
    col_hdr_h = 34
    row_h = 36

    rows = entries[:max_rows]
    total_h = banner_h + col_hdr_h + max(1, len(rows)) * row_h + 4
    img = Image.new("RGBA", (width, total_h), (15, 17, 21, 255))
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, width, banner_h + col_hdr_h), fill=(22, 20, 17, 255))
    rng = random.Random(int.from_bytes(hashlib.sha256(mode_title.encode("utf-8")).digest()[:8], "big"))
    for _ in range(10):
        x0, y0 = rng.randint(0, width), rng.randint(0, banner_h + col_hdr_h)
        x1, y1 = x0 + rng.randint(-160, 160), y0 + rng.randint(-30, 30)
        draw.line([(x0, y0), (x1, y1)], fill=(46, 41, 33, 95), width=1)

    core_cnt = unique_roster_count if unique_roster_count is not None else count_unique_roster_players()
    sub_txt = subtitle_override or f"Top 20 + {core_cnt} Monitorados ({len(rows)})"
    s_fnt = pick_font(sub_txt, size=14, bold=True)
    sw = text_width(draw, sub_txt, s_fnt)

    mode_pt = format_mode_title_pt(mode_title)
    title_txt = f"COH3 BRASIL  •  RANKING UNIFICADO {mode_pt} (MÉDIA ELO)"
    t_fnt = pick_font(title_txt, size=16, bold=True, condensed=True)
    header_badge = get_veteranos_badge_icon(size=22)
    img.paste(header_badge, (16, 7), header_badge)
    title_x = 44
    max_title_w = width - title_x - 24 - sw
    if text_width(draw, title_txt, t_fnt) > max_title_w:
        title_txt = f"COH3 BRASIL • RANKING UNIFICADO {mode_pt}"
        title_txt, t_fnt = truncate_to_px(draw, title_txt, int(max_title_w), size=15, bold=True)

    draw.text((title_x, 9), title_txt, font=t_fnt, fill=(245, 184, 46, 255))
    draw.text((width - 20 - sw, 10), sub_txt, font=s_fnt, fill=(190, 184, 172, 255))
    draw.line([(0, banner_h), (width, banner_h)], fill=(48, 44, 36, 255), width=1)

    gold_col = (245, 184, 46, 255)
    ch_y = banner_h + 7
    for lbl, cx, align in (
        ("POS.", 46, "center"),
        ("ELO ▾", 122, "center"),
        ("JOGADOR", 166, "left"),
        ("SEQ.", 498, "right"),
        ("VITÓRIAS ▾", 600, "right"),
        ("DERROTAS", 700, "right"),
        ("TAXA", 768, "right"),
        ("JOGOS", 838, "right"),
        ("ÚLT. JOGO", 964, "right"),
    ):
        hdr_fnt = pick_font(lbl, size=15, bold=True, condensed=True)
        lw = text_width(draw, lbl, hdr_fnt)
        tx = (cx - lw / 2) if align == "center" else ((cx - lw) if align == "right" else cx)
        draw.text((tx, ch_y), lbl, font=hdr_fnt, fill=gold_col)

    rule_y = banner_h + col_hdr_h - 1
    draw.line([(10, rule_y), (width - 10, rule_y)], fill=(234, 179, 8, 255), width=2)

    if not rows:
        efnt = pick_font("Sem jogadores ranqueados neste modo.", size=16, bold=False)
        draw.text((330, rule_y + 9), "Sem jogadores ranqueados neste modo.", font=efnt, fill=(156, 163, 175, 255))

    y = banner_h + col_hdr_h
    num_fnt = pick_font("1566", size=17, bold=True, condensed=True)
    row_vet_badge = get_veteranos_badge_icon(size=19)
    white = (243, 244, 246, 255)

    for idx, entry in enumerate(rows):
        is_custom = bool(entry.get("is_custom"))
        rank_val = int(entry.get("br_rank", idx + 1))
        r_bg = (
            (33, 28, 19, 255)
            if (is_custom and rank_val > 20)
            else ((17, 19, 24, 255) if idx % 2 == 0 else (24, 27, 34, 255))
        )

        draw.rectangle((0, y, width, y + row_h), fill=r_bg)
        draw.line([(10, y + row_h - 1), (width - 10, y + row_h - 1)], fill=(34, 38, 46, 255), width=1)

        elo_int = int(round(float(entry.get("avg_elo", 0))))
        wins = int(entry.get("wins", 0))
        losses = int(entry.get("losses", 0))
        total = int(entry.get("total", wins + losses))
        ratio = int(entry.get("ratio", round((wins * 100.0) / total) if total > 0 else 0))
        streak = int(entry.get("streak", 0))
        lmd = int(entry.get("last_match_date", 0))
        last_game_str = datetime.fromtimestamp(lmd, tz=BRT_TZ).strftime("%d/%m/%Y") if lmd > 0 else "—"

        rank_color = (250, 204, 21, 255) if rank_val == 1 else white
        r_str = str(rank_val)
        rw = text_width(draw, r_str, num_fnt)
        draw.text((46 - rw / 2, y + 8), r_str, font=num_fnt, fill=rank_color)

        e_str = str(elo_int)
        ew = text_width(draw, e_str, num_fnt)
        draw.text((122 - ew / 2, y + 8), e_str, font=num_fnt, fill=white)

        pid = int(entry.get("profile_id", 0))
        sid = str(entry.get("steam_id") or "")
        alias_raw = str(entry.get("alias") or f"Jogador {pid}")
        country = str(entry.get("country") or "br").lower()

        ring_col = (245, 184, 46, 245) if is_custom else (34, 197, 94, 230)
        av_im = avatar_provider.get_circular_avatar(
            size=30,
            profile_id=pid,
            steam_id=sid,
            alias=alias_raw,
            ring_color=ring_col,
        )
        img.paste(av_im, (166, y + 3), av_im)

        flag_im = get_country_flag(country, w=21, h=15)
        img.paste(flag_im, (202, y + 11), flag_im)

        disp_alias, a_fnt = truncate_to_px(
            draw,
            alias_raw,
            max_px=196 if is_custom else 224,
            size=17,
            bold=True,
        )
        name_col = (253, 230, 138, 255) if is_custom else white
        draw.text((230, y + 8), disp_alias, font=a_fnt, fill=name_col)

        if is_custom:
            bx = int(230 + text_width(draw, disp_alias, a_fnt) + 7)
            img.paste(row_vet_badge, (bx, y + 8), row_vet_badge)

        if streak > 0:
            st_str, st_col = f"+{streak}", (74, 222, 128, 255)
        elif streak < 0:
            st_str, st_col = f"{streak}", (248, 113, 113, 255)
        else:
            st_str, st_col = "0", (156, 163, 175, 255)
        stw = text_width(draw, st_str, num_fnt)
        draw.text((498 - stw, y + 8), st_str, font=num_fnt, fill=st_col)

        for val_str, right_x in (
            (str(wins), 600),
            (str(losses), 700),
            (f"{ratio}%", 768),
            (str(total), 838),
            (last_game_str, 964),
        ):
            vw = text_width(draw, val_str, num_fnt)
            draw.text((right_x - vw, y + 8), val_str, font=num_fnt, fill=white)

        y += row_h

    return _encode_rgb_png(img)
