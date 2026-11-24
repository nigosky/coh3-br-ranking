"""Ranking automatizado dos Campeões Brasileiros por Battlegroup (Company of Heroes 3 — Automatch 1v1 a 4v4)."""

from __future__ import annotations

import concurrent.futures
import csv
import hashlib
import html
import io
import json
import logging
import math
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from arranged_teams import Coh3StatsOpenDataClient
from relic_ranking import (
    ALLOWED_RELIC_HOSTS,
    BASE_URL as RELIC_BASE_URL,
    LEADERBOARD_ID_TO_MODE_FACTION,
    TITLE_STEAM,
    RelicRankingClient,
)
from roster import (
    CORE_BR_ROSTER,
    EXCLUDED_VETERANO_PIDS,
    MANUAL_OTHER_BR_ALIASES,
    extract_smurf_owner,
    extract_steam_id,
)
from security import (
    RateLimiter,
    atomic_write_text,
    get_thread_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    sanitize_csv_cell,
    strip_unsafe_chars,
    validate_https_endpoint,
)

logger = logging.getLogger("coh3_ranking.battlegroups")

ALLOWED_COHDB_HOSTS: frozenset[str] = frozenset({"cohdb.com"})
COHDB_PLAYER_PATH_RE = re.compile(r"^/players/\d{1,12}/(?:battlegroups|performance)$")
MAX_COHDB_HTML_BYTES = 1 * 1024 * 1024  # 1 MB

ALLOWED_COMMUNITY_HOSTS: frozenset[str] = frozenset({"raw.githubusercontent.com"})
COMMUNITY_ROSTER_URL = "https://raw.githubusercontent.com/nigosky/coh3-br-lobby-radar/main/br_community.json"
COMMUNITY_PATH_RE = re.compile(r"^/nigosky/coh3-br-lobby-radar/main/br_community\.json$")
MAX_COMMUNITY_JSON_BYTES = 4 * 1024 * 1024  # 4 MB
MAX_RELIC_RECENT_BYTES = 2 * 1024 * 1024  # 2 MB

DEFAULT_BG_CACHE_TTL_DAYS = 7
DEFAULT_BG_LOOKBACK_DAYS = 45
MIN_CANDIDATE_COMMUNITY_ELO = 1250
MIN_QUALIFIED_RAW_ELO = 1250
MIN_QUALIFIED_VETERANO_ELO = 1100
MIN_BG_SELECTIONS = 5
MIN_BG_WINS = 3
MIN_MATCH_DURATION_SEC = 300.0  # ignora quedas/remakes < 5 min

# Mapeamento oficial de race_id do CoH3 (198437 = DAK, 203852/197345 = British)
RACE_ID_TO_FACTION: dict[int, str] = {
    129494: "american",
    137123: "german",
    198437: "afrika_corps",
    203852: "british",
    197345: "british",
}

RELIC_FAC_TO_FACTION: dict[str, str] = {
    "american": "american",
    "AME": "american",
    "british": "british",
    "BRI": "british",
    "german": "german",
    "GER": "german",
    "dak": "afrika_corps",
    "DAK": "afrika_corps",
}

COHDB_PERF_FAC_TO_FACTION: dict[str, str] = {
    "americans": "american",
    "british_africa": "british",
    "germans": "german",
    "afrika_corps": "afrika_corps",
}

COHDB_PERF_MODE_TO_MODE: dict[str, str] = {
    "ones": "1v1",
    "twos": "2v2",
    "threes": "3v3",
    "fours": "4v4",
}

# Somente filas oficiais de Automatch PvP (exclui custom=0 e skirmish/AI=5..16,101)
AUTOMATCH_TYPE_TO_MODE: dict[int, str] = {
    20: "1v1",
    21: "2v2",
    22: "3v3",
    23: "4v4",
}

# Calibração empírica de skill por modo (1v1 e 2v2 exigem muito mais skill individual que 3v3/4v4)
MODE_ELO_OFFSET: dict[str, float] = {
    "1v1": 0.0,
    "2v2": -20.0,
    "3v3": -160.0,
    "4v4": -270.0,
}

# Contas Smurf conhecidas não devem tomar vaga de conta titular como representante de Battlegroup
KNOWN_SMURF_PIDS: dict[int, str] = {
    1333418: "Chacineiro",      # Charles Bronson
    1390350: "Chacineiro",      # TearsInTheRain
    1423426: "arc",             # geladeira velha
    557890: "Gabe",             # gabe44arg
    1318162: "General Winter",  # Smurf 1
    1297821: "General Winter",  # Smurf 2
    1297804: "『V』SKOBADARK",  # Leite Moço
    164152: "Sgt.Yoda",         # seu pai
}

# Brasileiros de ELO alto confirmados fora do br_community.json (ex.: fila decaída ou descobertos na API)
EXTRA_SEED_BR_PIDS: tuple[int, ...] = (
    40833,   # Tokai Teio (Aston Machan)
    197937,  # Armaddin
)

FACTION_META: dict[str, dict[str, str]] = {
    "american": {"label": "US FORCES", "short": "USF", "side": "allies"},
    "british": {"label": "BRITISH FORCES", "short": "UKF", "side": "allies"},
    "german": {"label": "WEHRMACHT", "short": "WER", "side": "axis"},
    "afrika_corps": {"label": "AFRIKAKORPS", "short": "DAK", "side": "axis"},
}

# Catálogo oficial completo dos 28 Battlegroups do CoH3 (Patch 3.0.2 — 7 por facção)
BATTLEGROUP_CATALOG: tuple[dict[str, str], ...] = (
    # US Forces (7)
    {"faction": "american", "bg_name": "Airborne Battlegroup", "short_name": "Airborne", "icon_slug": "american_airborne_battlegroup"},
    {"faction": "american", "bg_name": "Armored Battlegroup", "short_name": "Armored", "icon_slug": "american_armored_battlegroup"},
    {"faction": "american", "bg_name": "Special Operations Battlegroup", "short_name": "Special Operations", "icon_slug": "american_special_operations_battlegroup"},
    {"faction": "american", "bg_name": "Advanced Infantry Battlegroup", "short_name": "Advanced Infantry", "icon_slug": "american_advanced_infantry_battlegroup"},
    {"faction": "american", "bg_name": "Heavy Weapons", "short_name": "Heavy Weapons", "icon_slug": "american_heavy_weapons"},
    {"faction": "american", "bg_name": "Italian Partisan Battlegroup", "short_name": "Italian Partisan", "icon_slug": "american_italian_partisan_battlegroup"},
    {"faction": "american", "bg_name": "Free French Battlegroup", "short_name": "Free French", "icon_slug": "american_free_french_battlegroup"},
    # British Forces (7)
    {"faction": "british", "bg_name": "Air and Sea Battlegroup", "short_name": "Air and Sea", "icon_slug": "british_air_and_sea_battlegroup"},
    {"faction": "british", "bg_name": "Heavy Armor Battlegroup", "short_name": "Heavy Armor", "icon_slug": "british_heavy_armor_battlegroup"},
    {"faction": "british", "bg_name": "Indian Artillery Battlegroup", "short_name": "Indian Artillery", "icon_slug": "british_indian_artillery_battlegroup"},
    {"faction": "british", "bg_name": "Australian Defense Battlegroup", "short_name": "Australian Defense", "icon_slug": "british_australian_defense_battlegroup"},
    {"faction": "british", "bg_name": "Canadian Shock Battlegroup", "short_name": "Canadian Shock", "icon_slug": "british_canadian_shock_battlegroup"},
    {"faction": "british", "bg_name": "Polish Cavalry Battlegroup", "short_name": "Polish Cavalry", "icon_slug": "british_polish_cavalry_battlegroup"},
    {"faction": "british", "bg_name": "Special Service Battlegroup", "short_name": "Special Service", "icon_slug": "british_special_service_battlegroup"},
    # Wehrmacht (7)
    {"faction": "german", "bg_name": "Breakthrough Battlegroup", "short_name": "Breakthrough", "icon_slug": "german_breakthrough_battlegroup"},
    {"faction": "german", "bg_name": "Luftwaffe Battlegroup", "short_name": "Luftwaffe", "icon_slug": "german_luftwaffe_battlegroup"},
    {"faction": "german", "bg_name": "Mechanized Battlegroup", "short_name": "Mechanized", "icon_slug": "german_mechanized_battlegroup"},
    {"faction": "german", "bg_name": "Italian Coastal Battlegroup", "short_name": "Italian Coastal", "icon_slug": "german_italian_coastal_battlegroup"},
    {"faction": "german", "bg_name": "Terror Battlegroup", "short_name": "Terror", "icon_slug": "german_terror_battlegroup"},
    {"faction": "german", "bg_name": "Last Stand Battlegroup", "short_name": "Last Stand", "icon_slug": "german_last_stand_battlegroup"},
    {"faction": "german", "bg_name": "Siege Breaker Battlegroup", "short_name": "Siege Breaker", "icon_slug": "german_siege_breaker_battlegroup"},
    # Deutsches Afrikakorps (7)
    {"faction": "afrika_corps", "bg_name": "Armored Support Battlegroup", "short_name": "Armored Support", "icon_slug": "afrika_corps_armored_support_battlegroup"},
    {"faction": "afrika_corps", "bg_name": "Italian Combined Arms Battlegroup", "short_name": "Italian Combined Arms", "icon_slug": "afrika_corps_italian_combined_arms_battlegroup"},
    {"faction": "afrika_corps", "bg_name": "Italian Infantry", "short_name": "Italian Infantry", "icon_slug": "afrika_corps_italian_infantry"},
    {"faction": "afrika_corps", "bg_name": "Battlefield Espionage Battlegroup", "short_name": "Battlefield Espionage", "icon_slug": "afrika_corps_battlefield_espionage_battlegroup"},
    {"faction": "afrika_corps", "bg_name": "Panzerjäger Kommand Battlegroup", "short_name": "Panzerjäger Kommand", "icon_slug": "afrika_corps_panzerjager_kommand_battlegroup"},
    {"faction": "afrika_corps", "bg_name": "Kriegsmarine Battlegroup", "short_name": "Kriegsmarine", "icon_slug": "afrika_corps_kriegsmarine_battlegroup"},
    {"faction": "afrika_corps", "bg_name": "Elite Forces Battlegroup", "short_name": "Elite Forces", "icon_slug": "afrika_corps_elite_forces_battlegroup"},
)


def parse_cohdb_battlegroups_html(html_text: str) -> dict[str, dict[str, Any]]:
    """Extrai o histórico Automatch por Battlegroup da página canônica /players/<pid>/battlegroups do cohdb."""
    if not html_text:
        return {}
    cards = re.split(r'(?=src="/assets/races/[^/]+/battlegroups/)', html_text)
    bgs: dict[str, dict[str, Any]] = {}
    for card in cards[1:]:
        m_head = re.search(
            r'src="/assets/races/([^/]+)/battlegroups/([^"]+)"[^>]*/>\s*<span[^>]*>([^<]+)</span>',
            card,
            flags=re.S,
        )
        if not m_head:
            continue
        race = strip_unsafe_chars(m_head.group(1)).strip().lower()
        bg_name = strip_unsafe_chars(html.unescape(m_head.group(3))).strip()
        if race not in FACTION_META or not bg_name:
            continue

        clean = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", card[:2600])))
        m_rec = re.search(r"Record\s+(\d+)\s*[–-]\s*(\d+)", clean)
        m_wr = re.search(r"Win%\s+([\d.]+)%", clean)
        m_l30 = re.search(r"Last 30\s+(\d+)\s*[–-]\s*(\d+)", clean)
        m_sel = re.search(r"Selections\s+([\d,]+)", clean)
        wins = int(m_rec.group(1)) if m_rec else 0
        losses = int(m_rec.group(2)) if m_rec else 0
        sel = int(m_sel.group(1).replace(",", "")) if m_sel else (wins + losses)
        wr = float(m_wr.group(1)) if m_wr else (100.0 * wins / max(1, wins + losses))
        l30_w, l30_l = (int(m_l30.group(1)), int(m_l30.group(2))) if m_l30 else (0, 0)

        bgs[f"{race}|{bg_name}"] = {
            "faction": race,
            "bg_name": bg_name,
            "wins": wins,
            "losses": losses,
            "wr": round(wr, 1),
            "l30_w": l30_w,
            "l30_l": l30_l,
            "selections": sel,
        }
    return bgs


def parse_cohdb_performance_html(html_text: str) -> dict[str, int]:
    """Extrai os cohdb Ratings (500..3000) por (facção, modo) da página canônica /players/<pid>/performance."""
    if not html_text:
        return {}
    hits = re.findall(
        r'performance\?faction=([a-z_]+)&amp;mode=([a-z]+)[^"]*"[^>]*>(.*?)</a>',
        html_text,
        flags=re.S,
    )
    ratings: dict[str, int] = {}
    for cfac, cmode, body in hits:
        fac = COHDB_PERF_FAC_TO_FACTION.get(cfac)
        mode = COHDB_PERF_MODE_TO_MODE.get(cmode)
        if not fac or not mode:
            continue
        val_txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        if "Last" in val_txt or "All" in val_txt:
            continue
        m_num = re.search(r"(\d[\d,]{2,})", val_txt)
        if not m_num:
            continue
        val = int(m_num.group(1).replace(",", ""))
        if 500 <= val <= 3000:
            ratings[f"{fac}:{mode}"] = val
    return ratings


class CohDBCanonicalClient:
    """Cliente HTTPS endurecido e estritamente compatível com robots.txt do cohdb.com (somente URLs canônicas)."""

    def __init__(self, min_interval_sec: float = 0.30, timeout_sec: float = 20.0) -> None:
        self._rate_limiter = RateLimiter(min_interval_sec=min_interval_sec, floor_sec=0.15)
        self.timeout_sec = max(5.0, float(timeout_sec))

    def _fetch_canonical_page(self, profile_id: int, section: str) -> str:
        pid = int(profile_id)
        if pid <= 0 or section not in ("battlegroups", "performance"):
            raise ValueError(f"Seção/perfil inválido para cohdb: {profile_id}/{section}")
        url = f"https://cohdb.com/players/{pid}/{section}"
        parsed = validate_https_endpoint(
            url,
            ALLOWED_COHDB_HOSTS,
            path_prefix="/players/",
            path_regex=COHDB_PLAYER_PATH_RE,
        )
        if parsed.query:
            raise ValueError("Parâmetros de query no cohdb são bloqueados para respeitar robots.txt.")

        opener = get_thread_no_redirect_https_opener()
        headers = {
            "User-Agent": "CoH3-BR-Ranking-Unificado/1.0 (Canonical Player Stats)",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip",
        }
        for attempt in range(2):
            self._rate_limiter.wait()
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=self.timeout_sec) as resp:
                    if int(getattr(resp, "status", 200)) != 200:
                        return ""
                    return read_bounded_http_body(resp, MAX_COHDB_HTML_BYTES).decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                code = exc.code
                try:
                    exc.close()
                except Exception:
                    pass
                if code in (404, 410):
                    return ""
                if code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(1.5)
                    continue
                return ""
            except Exception:
                if attempt == 0:
                    time.sleep(0.8)
                    continue
                return ""
        return ""

    def fetch_player_battlegroups(self, profile_id: int) -> dict[str, dict[str, Any]]:
        html_txt = self._fetch_canonical_page(profile_id, "battlegroups")
        return parse_cohdb_battlegroups_html(html_txt)

    def fetch_player_performance(self, profile_id: int) -> dict[str, int]:
        html_txt = self._fetch_canonical_page(profile_id, "performance")
        return parse_cohdb_performance_html(html_txt)


def fetch_br_community_catalog(timeout_sec: float = 15.0) -> list[dict[str, Any]]:
    """Carrega br_community.json compartilhado (nigosky/coh3-br-lobby-radar) com validação anti-SSRF."""
    validate_https_endpoint(
        COMMUNITY_ROSTER_URL,
        ALLOWED_COMMUNITY_HOSTS,
        path_prefix="/nigosky/",
        path_regex=COMMUNITY_PATH_RE,
    )
    opener = get_thread_no_redirect_https_opener()
    req = urllib.request.Request(
        COMMUNITY_ROSTER_URL,
        headers={
            "User-Agent": "CoH3-BR-Ranking-Unificado/1.0",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with opener.open(req, timeout=timeout_sec) as resp:
            raw = read_bounded_http_body(resp, MAX_COMMUNITY_JSON_BYTES).decode("utf-8")
        payload = json.loads(raw)
        players = payload.get("players", payload) if isinstance(payload, dict) else payload
        if isinstance(players, dict):
            return [v for v in players.values() if isinstance(v, dict)]
        if isinstance(players, list):
            return [v for v in players if isinstance(v, dict)]
    except Exception as exc:
        logger.warning("Não foi possível carregar br_community.json remoto: %s", redact_secrets(exc))
    return []


def _relic_endpoint(path: str, query: str) -> str:
    base = RELIC_BASE_URL.rstrip("/")
    if not base.endswith("/community/leaderboard"):
        base += "/community/leaderboard"
    return f"{base}/{path.lstrip('/')}?{query}"


def discover_top_leaderboard_br_pids(
    relic_client: RelicRankingClient,
    max_rank: int = 400,
) -> set[int]:
    """Varre o Top 400 mundial de 1v1 e 2v2 para não perder nenhum jogador BR novo de ELO alto."""
    discovered: set[int] = set()
    page_size = 200
    for lid, (mode, _fac) in LEADERBOARD_ID_TO_MODE_FACTION.items():
        if mode not in ("1v1", "2v2"):
            continue
        for start in range(1, max_rank + 1, page_size):
            url = _relic_endpoint(
                "getLeaderboard2",
                f"title={TITLE_STEAM}&leaderboard_id={int(lid)}&start={int(start)}&count={int(page_size)}&sortBy=1",
            )
            try:
                data = relic_client._get_json(url)
            except Exception:
                continue
            for sg in data.get("statGroups") or []:
                members = sg.get("members") or []
                if len(members) != 1:
                    continue
                m = members[0]
                if str(m.get("country") or "").strip().lower() == "br":
                    pid = int(m.get("profile_id") or 0)
                    if pid > 0:
                        discovered.add(pid)
    return discovered


def _resolve_smurf_owner(pid: int, rec: dict[str, Any]) -> str:
    if pid in KNOWN_SMURF_PIDS:
        return KNOWN_SMURF_PIDS[pid]
    explicit = str(rec.get("smurf_of") or "").strip()
    if explicit:
        return strip_unsafe_chars(explicit)[:40]
    alias = str(rec.get("original_alias") or rec.get("alias") or "")
    return extract_smurf_owner(alias, pin_alias=bool(rec.get("pin_alias")))


def build_br_bg_candidate_pool(
    custom_players: list[dict[str, Any]],
    relic_client: RelicRankingClient,
    community_players: list[dict[str, Any]] | None = None,
    include_top_leaderboard_scan: bool = True,
) -> dict[int, dict[str, Any]]:
    """Monta o pool de elite brasileira (Veteranos + Manual BR + Comunidade ELO alto) com ELO ao vivo da Relic."""
    pool: dict[int, dict[str, Any]] = {}

    # 1) Veteranos (custom_players + CORE_BR_ROSTER)
    for src_list in (custom_players, list(CORE_BR_ROSTER)):
        for entry in src_list:
            if not isinstance(entry, dict):
                continue
            pid = int(entry.get("profile_id") or 0)
            if pid <= 0 or pid in pool:
                continue
            alias = strip_unsafe_chars(entry.get("original_alias") or entry.get("alias") or "") or f"Player {pid}"
            is_excluded = pid in EXCLUDED_VETERANO_PIDS
            pool[pid] = {
                "profile_id": pid,
                "steam_id": extract_steam_id(entry.get("steam_id")),
                "roster_alias": alias,
                "category": "Outros BR" if is_excluded else "Veterano",
                "is_veterano": not is_excluded,
                "smurf_of": _resolve_smurf_owner(pid, entry),
            }

    # 2) Manual BR (outros brasileiros mapeados manualmente)
    for entry in MANUAL_OTHER_BR_ALIASES:
        if not isinstance(entry, dict):
            continue
        pid = int(entry.get("profile_id") or 0)
        if pid <= 0 or pid in pool:
            continue
        alias = strip_unsafe_chars(entry.get("original_alias") or entry.get("alias") or "") or f"Player {pid}"
        pool[pid] = {
            "profile_id": pid,
            "steam_id": extract_steam_id(entry.get("steam_id")),
            "roster_alias": alias,
            "category": "Outros BR" if pid in EXCLUDED_VETERANO_PIDS else "Manual BR",
            "is_veterano": False,
            "smurf_of": _resolve_smurf_owner(pid, entry),
        }

    # 3) Comunidade BR (br_community.json com max_elo >= 1250)
    comm_list = community_players if community_players is not None else fetch_br_community_catalog()
    for entry in comm_list:
        pid = int(entry.get("profile_id") or 0)
        if pid <= 0:
            continue
        sid = extract_steam_id(entry.get("steam_id"))
        if pid in pool:
            if sid and not pool[pid]["steam_id"]:
                pool[pid]["steam_id"] = sid
            continue
        max_elo = int(entry.get("max_elo") or 0)
        if max_elo >= MIN_CANDIDATE_COMMUNITY_ELO:
            alias = strip_unsafe_chars(entry.get("alias") or "") or f"Player {pid}"
            pool[pid] = {
                "profile_id": pid,
                "steam_id": sid,
                "roster_alias": alias,
                "category": "Outros BR",
                "is_veterano": False,
                "smurf_of": _resolve_smurf_owner(pid, entry),
            }

    # 4) Seeds confirmados + varredura rápida Top 400 mundial 1v1/2v2
    extra_pids = set(EXTRA_SEED_BR_PIDS)
    if include_top_leaderboard_scan:
        try:
            extra_pids.update(discover_top_leaderboard_br_pids(relic_client, max_rank=400))
        except Exception as exc:
            logger.warning("Varredura complementar Top 400 1v1/2v2 indisponível: %s", redact_secrets(exc))
    for pid in extra_pids:
        if pid > 0 and pid not in pool:
            pool[pid] = {
                "profile_id": pid,
                "steam_id": "",
                "roster_alias": f"Player {pid}",
                "category": "Outros BR",
                "is_veterano": False,
                "smurf_of": KNOWN_SMURF_PIDS.get(pid, ""),
            }

    # 5) Busca estatísticas ao vivo na Relic API (lotes de 10)
    pids_all = sorted(pool.keys())
    raw_by_pid: dict[int, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    for idx in range(0, len(pids_all), 10):
        batch = pids_all[idx : idx + 10]
        url = _relic_endpoint(
            "getPersonalStat",
            f"title={TITLE_STEAM}&profile_ids={urllib.parse.quote(json.dumps(batch))}",
        )
        try:
            data = relic_client._get_json(url)
        except Exception as exc:
            logger.warning("Falha ao consultar lote Relic getPersonalStat (%d PIDs): %s", len(batch), redact_secrets(exc))
            continue
        sg_to_member: dict[int, dict[str, Any]] = {}
        for sg in data.get("statGroups") or []:
            members = sg.get("members") or []
            if len(members) == 1:
                sg_to_member[int(sg["id"])] = members[0]
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for st in data.get("leaderboardStats") or []:
            m = sg_to_member.get(int(st.get("statgroup_id") or 0))
            if m:
                grouped[int(m["profile_id"])].append(st)
        for m in sg_to_member.values():
            mpid = int(m["profile_id"])
            raw_by_pid[mpid] = (m, grouped.get(mpid, []))

    qualified: dict[int, dict[str, Any]] = {}
    for pid, pinfo in pool.items():
        pair = raw_by_pid.get(pid)
        if not pair:
            continue
        member, stats_list = pair
        live_alias = strip_unsafe_chars(member.get("alias") or pinfo["roster_alias"]) or pinfo["roster_alias"]
        live_sid = extract_steam_id(member.get("name") or "") or pinfo["steam_id"]

        queues: dict[str, dict[str, Any]] = {}
        fac_adj: dict[str, float] = {}
        fac_raw: dict[str, int] = {}
        fac_mode: dict[str, str] = {}
        best_12_by_fac: dict[str, tuple[int, str]] = {}
        total_games = total_wins = total_drops = 0

        for st in stats_list:
            lid = int(st.get("leaderboard_id") or 0)
            if lid not in LEADERBOARD_ID_TO_MODE_FACTION:
                continue
            mode, rfac = LEADERBOARD_ID_TO_MODE_FACTION[lid]
            fac = RELIC_FAC_TO_FACTION[rfac]
            wins = int(st.get("wins") or 0)
            losses = int(st.get("losses") or 0)
            games = wins + losses
            rating = int(st.get("rating") or 0)
            rank = int(st.get("rank") or -1)
            drops = int(st.get("drops") or 0)
            lmd = int(st.get("lastmatchdate") or 0)
            total_games += games
            total_wins += wins
            total_drops += drops

            queues[f"{fac}:{mode}"] = {
                "rating": rating,
                "rank": rank,
                "wins": wins,
                "losses": losses,
                "games": games,
                "drops": drops,
                "lastmatchdate": lmd,
            }
            if games >= 8 and rating > 0:
                adj = rating + MODE_ELO_OFFSET[mode]
                if fac not in fac_adj or adj > fac_adj[fac]:
                    fac_adj[fac] = round(adj, 1)
                    fac_raw[fac] = rating
                    fac_mode[fac] = mode
                if mode in ("1v1", "2v2") and (fac not in best_12_by_fac or rating > best_12_by_fac[fac][0]):
                    best_12_by_fac[fac] = (rating, mode)

        glob_raw = max((q["rating"] for q in queues.values() if q["games"] >= 8), default=0)
        glob_adj = max(fac_adj.values(), default=0.0)
        min_req = MIN_QUALIFIED_VETERANO_ELO if pinfo["is_veterano"] else MIN_QUALIFIED_RAW_ELO
        if glob_raw < min_req:
            continue

        rec = dict(pinfo)
        rec.update(
            {
                "steam_id": live_sid,
                "live_alias": live_alias,
                "country": str(member.get("country") or "br").strip().lower() or "br",
                "level": int(member.get("level") or 0),
                "total_games": total_games,
                "total_wins": total_wins,
                "total_drops": total_drops,
                "global_best_raw_elo": glob_raw,
                "global_best_adj_elo": round(glob_adj, 1),
                "fac_best_adj_elo": fac_adj,
                "fac_best_raw_elo": fac_raw,
                "fac_best_mode": fac_mode,
                "best_12_by_fac": {k: [v[0], v[1]] for k, v in best_12_by_fac.items()},
                "queues": queues,
            }
        )
        qualified[pid] = rec

    logger.info("Pool de elite BR para Battlegroups: %d jogadores qualificados.", len(qualified))
    return qualified


def extract_automatch_combat_rows(
    match: dict[str, Any],
    target_pids: set[int],
) -> list[dict[str, Any]]:
    """Extrai métricas Automatch (1v1..4v4, >= 5 min) de APM, dano, K/D, wipes e vitória dominante."""
    mtype = int(match.get("matchtype_id") if match.get("matchtype_id") is not None else -1)
    mode = AUTOMATCH_TYPE_TO_MODE.get(mtype)
    if not mode:
        return []

    reps = match.get("matchhistoryreportresults") or []
    if not any(int(r.get("profile_id") or 0) in target_pids for r in reps if isinstance(r, dict)):
        return []

    match_id = int(match.get("id") or 0)
    completion_ts = int(match.get("completiontime") or match.get("startgametime") or 0)
    if match_id <= 0:
        return []

    parsed_reps: list[tuple[dict[str, Any], dict[str, Any]]] = []
    team_dmgs: dict[int, list[float]] = defaultdict(list)
    for r in reps:
        if not isinstance(r, dict):
            continue
        raw_c = r.get("counters")
        c: dict[str, Any] = {}
        if isinstance(raw_c, dict):
            c = raw_c
        elif isinstance(raw_c, str) and raw_c.strip().startswith("{"):
            try:
                loaded = json.loads(raw_c)
                if isinstance(loaded, dict):
                    c = loaded
            except ValueError:
                c = {}
        tid = r.get("teamid")
        dmg = float(c.get("dmgdone") or 0.0)
        if tid is not None and dmg > 0.0:
            team_dmgs[int(tid)].append(dmg)
        parsed_reps.append((r, c))

    out: list[dict[str, Any]] = []
    for r, c in parsed_reps:
        pid = int(r.get("profile_id") or 0)
        if pid not in target_pids:
            continue
        fac = RACE_ID_TO_FACTION.get(int(r.get("race_id") or 0))
        if not fac:
            continue
        gt = float(c.get("gt") or 0.0)
        totalcmds = float(c.get("totalcmds") or 0.0)
        if gt < MIN_MATCH_DURATION_SEC or totalcmds <= 0.0:
            continue

        mins = gt / 60.0
        apm = totalcmds / mins
        dmg = float(c.get("dmgdone") or 0.0)
        dmg_pm = dmg / mins
        ekills = float(c.get("ekills") or 0.0)
        edeaths = float(c.get("edeaths") or 0.0)
        kd = min(5.0, ekills / max(1.0, edeaths))
        sqkill = float(c.get("sqkill") or 0.0)
        sqlost = float(c.get("sqlost") or 0.0)
        vkill = float(c.get("vkill") or 0.0)
        vlost = float(c.get("vlost") or 0.0)
        pcap = float(c.get("pcap") or 0.0)
        plost = float(c.get("plost") or 0.0)
        won = 1 if int(r.get("resulttype") or 0) == 1 else 0

        my_tid = r.get("teamid")
        enemy_dmgs = [d for tid, lst in team_dmgs.items() if my_tid is None or tid != int(my_tid) for d in lst]
        enemy_avg = (sum(enemy_dmgs) / len(enemy_dmgs)) if enemy_dmgs else max(1.0, dmg)
        dmg_ratio = min(4.0, dmg / max(100.0, enemy_avg))
        pcap_ratio = min(4.0, (pcap + 2.0) / (plost + 2.0))

        # Proxy Automatch para vitória elástica (domínio de dano + K/D, vitória rápida ou domínio de pontos/VP)
        is_stomp_win = 0
        if won == 1:
            if (
                (dmg_ratio >= 1.35 and kd >= 1.25)
                or (gt <= 1080.0 and kd >= 1.25 and pcap >= plost)
                or (pcap >= plost + 8.0 and kd >= 1.25)
            ):
                is_stomp_win = 1

        out.append(
            {
                "match_id": match_id,
                "profile_id": pid,
                "faction": fac,
                "mode": mode,
                "won": won,
                "gt": gt,
                "apm": apm,
                "dmg": dmg,
                "dmg_pm": dmg_pm,
                "dmg_ratio": dmg_ratio,
                "kd": kd,
                "net_wipes": sqkill - sqlost,
                "vkill": vkill,
                "vlost": vlost,
                "pcap_ratio": pcap_ratio,
                "stomp_win": is_stomp_win,
                "completion_ts": completion_ts,
            }
        )
    return out


def _fetch_relic_recent_automatch_rows(profile_id: int) -> list[dict[str, Any]]:
    base = RELIC_BASE_URL.rstrip("/")
    if not base.endswith("/community/leaderboard"):
        base += "/community/leaderboard"
    url = f"{base}/getRecentMatchHistoryByProfileId?title={TITLE_STEAM}&profile_id={int(profile_id)}"
    validate_https_endpoint(url, ALLOWED_RELIC_HOSTS, path_prefix="/community/leaderboard/")
    opener = get_thread_no_redirect_https_opener()
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "CoH3-BR-Ranking-Unificado/1.0", "Accept": "application/json", "Accept-Encoding": "gzip"},
    )
    try:
        with opener.open(req, timeout=18.0) as resp:
            raw = read_bounded_http_body(resp, MAX_RELIC_RECENT_BYTES).decode("utf-8")
        data = json.loads(raw)
    except Exception:
        return []
    rows: list[dict[str, Any]] = []
    target = {int(profile_id)}
    for m in data.get("matchHistoryStats") or []:
        if isinstance(m, dict):
            rows.extend(extract_automatch_combat_rows(m, target))
    return rows


def summarize_combat_rows(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Agrega métricas de combate Automatch por jogador (geral e por facção)."""
    by_pid_all: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_pid_fac: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))

    for r in rows:
        pid = int(r["profile_id"])
        fac = str(r["faction"])
        by_pid_all[pid].append(r)
        by_pid_fac[pid][fac].append(r)

    def _agg(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {}
        n = len(items)
        wins = [x for x in items if int(x["won"]) == 1]
        apms = [float(x["apm"]) for x in items]
        return {
            "n": n,
            "wins": len(wins),
            "apm_mean": round(sum(apms) / n, 1),
            "apm_median": round(statistics.median(apms), 1),
            "dmg_pm": round(sum(float(x["dmg_pm"]) for x in items) / n, 1),
            "dmg_avg": round(sum(float(x["dmg"]) for x in items) / n, 0),
            "dmg_ratio": round(sum(float(x["dmg_ratio"]) for x in items) / n, 3),
            "kd": round(sum(float(x["kd"]) for x in items) / n, 3),
            "net_wipes": round(sum(float(x["net_wipes"]) for x in items) / n, 2),
            "pcap_ratio": round(sum(float(x["pcap_ratio"]) for x in items) / n, 3),
            "stomp_win_rate": round(sum(int(x["stomp_win"]) for x in wins) / max(1, len(wins)), 3),
            "stomp_wins": sum(int(x["stomp_win"]) for x in wins),
        }

    out: dict[int, dict[str, Any]] = {}
    for pid, all_items in by_pid_all.items():
        out[pid] = {
            "overall": _agg(all_items),
            "by_faction": {f: _agg(lst) for f, lst in by_pid_fac[pid].items()},
        }
    return out


def sync_bg_automatch_combat_stats(
    open_data_client: Coh3StatsOpenDataClient,
    db: Any,
    candidate_pids: set[int],
    lookback_days: int = DEFAULT_BG_LOOKBACK_DAYS,
    max_scan_seconds: float = 120.0,
) -> dict[int, dict[str, Any]]:
    """Sincroniza incrementalmente os dias pendentes de Automatch no Open Data + complemento Relic."""
    now_utc = datetime.now(timezone.utc)
    today_ts = int(datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc).timestamp())
    latest_arr = db.get_latest_arranged_day_with_matches() if hasattr(db, "get_latest_arranged_day_with_matches") else 0
    start_ts = latest_arr if latest_arr > 0 else (today_ts - 86400)
    target_days = [start_ts - i * 86400 for i in range(max(7, int(lookback_days)))]
    processed_days = db.get_processed_bg_days()
    pending_days = [d for d in target_days if d not in processed_days]

    t0 = time.monotonic()
    if pending_days:
        logger.info("Battlegroups Automatch: varrendo %d dia(s) pendente(s) no COH3 Stats Open Data...", len(pending_days))
        # Lotes paralelos respeitando orçamento de tempo
        chunk_size = 6
        for idx in range(0, len(pending_days), chunk_size):
            if time.monotonic() - t0 >= max_scan_seconds:
                logger.info("Orçamento de varredura Open Data para Battlegroups atingido (%.0fs); seguindo com cache.", max_scan_seconds)
                break
            chunk = pending_days[idx : idx + chunk_size]

            def _scan_one(dts: int) -> tuple[int, int, list[dict[str, Any]]]:
                try:
                    matches = open_data_client.fetch_day_matches(dts, retries=2)
                    if matches is None:
                        return dts, 0, []
                    rows: list[dict[str, Any]] = []
                    for m in matches:
                        if isinstance(m, dict):
                            rows.extend(extract_automatch_combat_rows(m, candidate_pids))
                    return dts, len(matches), rows
                except Exception:
                    return dts, -1, []

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(chunk))) as ex:
                for dts, total_cnt, rows in ex.map(_scan_one, chunk):
                    if total_cnt >= 0:
                        if rows:
                            db.save_bg_combat_matches(rows)
                        db.mark_bg_day_processed(dts, matches_total=total_cnt, br_auto_matches=len(rows))

    db.prune_old_bg_combat_matches(max_age_days=max(60, int(lookback_days) + 15))
    since_ts = start_ts - (max(7, int(lookback_days)) + 2) * 86400
    cached_rows = db.load_bg_combat_matches(since_ts=since_ts)

    # Complementa jogadores com < 12 partidas Automatch no Open Data via histórico recente da Relic
    cnt_by_pid = Counter(int(r["profile_id"]) for r in cached_rows)
    low_pids = [pid for pid in sorted(candidate_pids) if cnt_by_pid.get(pid, 0) < 12]
    if low_pids:
        extra_rows: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            for rows in ex.map(_fetch_relic_recent_automatch_rows, low_pids):
                extra_rows.extend(rows)
        if extra_rows:
            db.save_bg_combat_matches(extra_rows)
            cached_rows = db.load_bg_combat_matches(since_ts=0)

    return summarize_combat_rows(cached_rows)


def sync_cohdb_battlegroups_and_performance(
    cohdb_client: CohDBCanonicalClient,
    db: Any,
    qualified: dict[int, dict[str, Any]],
    combat_map: dict[int, dict[str, Any]],
    cache_ttl_days: int = DEFAULT_BG_CACHE_TTL_DAYS,
    force_refresh: bool = False,
) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, int]]]:
    """Sincroniza /battlegroups (todos os qualificados vencidos) e /performance (finalistas) no SQLite."""
    cache = db.get_all_bg_player_cache()
    now_ts = int(time.time())
    cutoff_ts = now_ts - max(1, int(cache_ttl_days)) * 86400

    bg_map: dict[int, dict[str, Any]] = {}
    perf_map: dict[int, dict[str, int]] = {}
    to_fetch_bg: list[int] = []

    for pid in sorted(qualified.keys()):
        entry = cache.get(pid)
        if entry and not force_refresh and int(entry.get("updated_at") or 0) >= cutoff_ts:
            bg_map[pid] = entry.get("battlegroups") or {}
            perf_map[pid] = {
                k: int(v)
                for k, v in (entry.get("performance") or {}).items()
                if isinstance(v, (int, float)) and 500 <= int(v) <= 3000
            }
        else:
            if entry:
                bg_map[pid] = entry.get("battlegroups") or {}
                perf_map[pid] = {
                    k: int(v)
                    for k, v in (entry.get("performance") or {}).items()
                    if isinstance(v, (int, float)) and 500 <= int(v) <= 3000
                }
            to_fetch_bg.append(pid)

    if to_fetch_bg:
        logger.info("cohdb /battlegroups: atualizando %d jogador(es) (TTL %dd, URLs canônicas)...", len(to_fetch_bg), cache_ttl_days)
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            future_map = {ex.submit(cohdb_client.fetch_player_battlegroups, pid): pid for pid in to_fetch_bg}
            for fut in concurrent.futures.as_completed(future_map):
                pid = future_map[fut]
                try:
                    fetched = fut.result()
                except Exception:
                    fetched = {}
                if fetched or pid not in bg_map:
                    bg_map[pid] = fetched
                db.save_bg_player_cache(pid, battlegroups=bg_map[pid], performance=perf_map.get(pid, {}), updated_at=now_ts)

    # Pré-classificação para buscar /performance apenas para os finalistas (Top 4 de cada BG) sem cache válido
    pre_report = compute_battlegroup_champions(qualified, bg_map, combat_map, perf_map)
    finalist_pids = sorted(
        {
            int(c["profile_id"])
            for bg_entry in pre_report["champions"]
            for c in bg_entry.get("top3", [])
            if int(c.get("profile_id") or 0) > 0
        }
    )
    to_fetch_perf = [
        pid
        for pid in finalist_pids
        if force_refresh or not perf_map.get(pid) or int((cache.get(pid) or {}).get("updated_at") or 0) < cutoff_ts
    ]
    if to_fetch_perf:
        logger.info("cohdb /performance: atualizando %d finalista(s) (URLs canônicas)...", len(to_fetch_perf))
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            future_map = {ex.submit(cohdb_client.fetch_player_performance, pid): pid for pid in to_fetch_perf}
            for fut in concurrent.futures.as_completed(future_map):
                pid = future_map[fut]
                try:
                    fetched_p = fut.result()
                except Exception:
                    fetched_p = {}
                if fetched_p:
                    perf_map[pid] = fetched_p
                    db.save_bg_player_cache(pid, performance=fetched_p, updated_at=now_ts)

    return bg_map, perf_map


def _display_player_name(player: dict[str, Any]) -> str:
    live = strip_unsafe_chars(player.get("live_alias") or "") or f"Player {player.get('profile_id', 0)}"
    roster_al = strip_unsafe_chars(player.get("roster_alias") or "")
    if roster_al and roster_al.lower() != live.lower():
        if (
            not roster_al.startswith("Player ")
            and "Principal" not in roster_al
            and "(" not in roster_al
            and roster_al.lower() not in live.lower()
        ):
            return f"{live} ({roster_al})"
    return live


def compute_battlegroup_champions(
    qualified: dict[int, dict[str, Any]],
    bg_map: dict[int, dict[str, Any]],
    combat_map: dict[int, dict[str, Any]],
    perf_map: dict[int, dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Calcula o índice 0–1000 e seleciona #1 Campeão BR + #2 Vice para cada um dos 28 Battlegroups."""
    perf_lookup = perf_map or {}
    champions_out: list[dict[str, Any]] = []
    tally_counter: Counter[int] = Counter()
    tally_meta: dict[int, dict[str, Any]] = {}

    for meta in BATTLEGROUP_CATALOG:
        fac = meta["faction"]
        bg_name = meta["bg_name"]
        bg_key = f"{fac}|{bg_name}"
        finfo = FACTION_META[fac]

        candidates: list[dict[str, Any]] = []
        for pid, p in qualified.items():
            if p.get("smurf_of"):
                continue  # Smurfs não tomam vaga de contas principais
            bg = (bg_map.get(pid) or {}).get(bg_key)
            if not bg:
                continue
            wins = int(bg.get("wins") or 0)
            losses = int(bg.get("losses") or 0)
            sel = int(bg.get("selections") or (wins + losses))
            if sel < MIN_BG_SELECTIONS or wins < MIN_BG_WINS:
                continue

            # 1) ELO ajustado por modo (1v1/2v2 > 3v3 > 4v4) + cohdb Rating
            f_adj = float((p.get("fac_best_adj_elo") or {}).get(fac) or 0.0)
            f_raw = int((p.get("fac_best_raw_elo") or {}).get(fac) or 0)
            f_mode = str((p.get("fac_best_mode") or {}).get(fac) or "?")
            g_adj = float(p.get("global_best_adj_elo") or 0.0)

            if f_adj > 0.0:
                eff_elo = 0.78 * f_adj + 0.22 * g_adj
            else:
                uncal = max(
                    (
                        int(q.get("rating") or 0) + MODE_ELO_OFFSET[kq.split(":")[1]]
                        for kq, q in (p.get("queues") or {}).items()
                        if kq.startswith(fac + ":") and int(q.get("rating") or 0) > 0
                    ),
                    default=0.0,
                )
                eff_elo = (0.55 * uncal + 0.25 * g_adj) if uncal > 0.0 else (0.60 * g_adj)

            pr = perf_lookup.get(pid) or {}
            fac_prs = [
                int(v) + (0 if k.endswith(":1v1") else -15 if k.endswith(":2v2") else -70 if k.endswith(":3v3") else -110)
                for k, v in pr.items()
                if k.startswith(fac + ":") and 500 <= int(v) <= 3000
            ]
            all_prs = [
                int(v) + (0 if k.endswith(":1v1") else -15 if k.endswith(":2v2") else -70 if k.endswith(":3v3") else -110)
                for k, v in pr.items()
                if 500 <= int(v) <= 3000
            ]
            cohdb_r = max(fac_prs) if fac_prs else (max(all_prs) * 0.94 if all_prs else 0.0)
            cohdb_best_raw = max(
                (int(v) for k, v in pr.items() if k.startswith(fac + ":") and 500 <= int(v) <= 3000),
                default=max((int(v) for v in pr.values() if 500 <= int(v) <= 3000), default=0),
            )

            skill_elo = (0.80 * eff_elo + 0.20 * cohdb_r) if cohdb_r > 0.0 else eff_elo

            # 2) Métricas Automatch com shrinkage bayesiano por facção (elimina flukes de 1–3 jogos)
            c_over = (combat_map.get(pid) or {}).get("overall") or {}
            c_fac = ((combat_map.get(pid) or {}).get("by_faction") or {}).get(fac) or {}
            n_fac = int(c_fac.get("n") or 0)
            n_over = int(c_over.get("n") or 0)
            if n_fac > 0 and n_over > 0:
                wf = n_fac / (n_fac + 8.0)
                c_use = {
                    k: wf * float(c_fac.get(k, 0.0)) + (1.0 - wf) * float(c_over.get(k, 0.0))
                    for k in ("apm_median", "apm_mean", "dmg_pm", "dmg_ratio", "kd", "net_wipes", "pcap_ratio", "stomp_win_rate")
                }
                c_n = n_over
            elif n_fac > 0:
                c_use = c_fac
                c_n = n_fac
            else:
                c_use = c_over
                c_n = n_over

            apm = float(c_use.get("apm_median") or c_use.get("apm_mean") or 0.0)
            dmg_pm = float(c_use.get("dmg_pm") or 0.0)
            dmg_ratio = float(c_use.get("dmg_ratio") or 1.0)
            kd = float(c_use.get("kd") or 1.0)
            net_wipes = float(c_use.get("net_wipes") or 0.0)
            pcap_ratio = float(c_use.get("pcap_ratio") or 1.0)
            stomp_wr = float(c_use.get("stomp_win_rate") or 0.0)

            # ---- Pontuação composta (0..1000) na ordem de grandeza solicitada ----
            # 1) ELO + Skill (380 pts) — barreira forte contra ELO baixo
            elo_norm = max(0.0, min(1.0, (skill_elo - 1050.0) / 700.0))
            if eff_elo < 1300.0:
                elo_norm *= max(0.20, (eff_elo - 950.0) / 350.0)
            pts_elo = 380.0 * elo_norm

            # 2) APM Automatch (150 pts)
            apm_norm = max(0.0, min(1.0, (apm - 25.0) / 85.0)) if apm > 0.0 else 0.30
            pts_apm = 150.0 * apm_norm

            # 3) Vitórias no BG + experiência/recência (155 pts)
            wins_norm = min(1.0, math.log1p(wins) / math.log1p(150.0))
            sample_mult = min(1.0, sel / 12.0) if sel < 12 else 1.0
            l30_games = int(bg.get("l30_w") or 0) + int(bg.get("l30_l") or 0)
            pts_wins = (145.0 * wins_norm * sample_mult) + (10.0 * min(1.0, l30_games / 12.0))

            # 4) Aproveitamento bayesiano no BG (125 pts)
            bayes_wr = (wins + 5.0) / (sel + 10.0)
            wr_norm = max(0.0, min(1.0, (bayes_wr - 0.43) / 0.25))
            pts_wr = 125.0 * wr_norm

            # 5) Dano & eficiência de combate (105 pts)
            dmg_rat_n = max(0.0, min(1.0, (dmg_ratio - 0.78) / 0.65)) if dmg_pm > 0.0 else 0.35
            dmg_pm_n = max(0.0, min(1.0, (dmg_pm - 230.0) / 350.0)) if dmg_pm > 0.0 else 0.35
            kd_n = max(0.0, min(1.0, (kd - 0.80) / 0.85)) if dmg_pm > 0.0 else 0.35
            wipes_n = max(0.0, min(1.0, (net_wipes + 1.0) / 4.0)) if dmg_pm > 0.0 else 0.35
            pts_dmg = 105.0 * (0.40 * dmg_rat_n + 0.25 * dmg_pm_n + 0.20 * kd_n + 0.15 * wipes_n)

            # 6) Vitórias elásticas & domínio de mapa/VP (85 pts)
            stomp_n = max(0.0, min(1.0, stomp_wr / 0.65)) if c_n > 0 else 0.35
            pcap_n = max(0.0, min(1.0, (pcap_ratio - 0.82) / 0.65)) if c_n > 0 else 0.35
            pts_dom = 85.0 * (0.55 * stomp_n + 0.45 * pcap_n)

            total_score = pts_elo + pts_apm + pts_wins + pts_wr + pts_dmg + pts_dom
            b12_pair = (p.get("best_12_by_fac") or {}).get(fac)

            candidates.append(
                {
                    "profile_id": pid,
                    "steam_id": str(p.get("steam_id") or ""),
                    "alias": _display_player_name(p),
                    "live_alias": str(p.get("live_alias") or ""),
                    "country": str(p.get("country") or "br"),
                    "category": str(p.get("category") or "Outros BR"),
                    "is_veterano": bool(p.get("is_veterano")),
                    "score": round(total_score, 1),
                    "eff_elo": round(eff_elo, 1),
                    "fac_raw_elo": f_raw,
                    "fac_mode": f_mode,
                    "best_12_elo": int(b12_pair[0]) if b12_pair else 0,
                    "best_12_mode": str(b12_pair[1]) if b12_pair else "",
                    "cohdb_rating": int(cohdb_best_raw),
                    "wins": wins,
                    "losses": losses,
                    "selections": sel,
                    "win_rate": round(float(bg.get("wr") or 0.0), 1),
                    "bayes_win_rate": round(100.0 * bayes_wr, 1),
                    "l30_wins": int(bg.get("l30_w") or 0),
                    "l30_losses": int(bg.get("l30_l") or 0),
                    "apm": round(apm, 1),
                    "dmg_pm": round(dmg_pm, 1),
                    "dmg_ratio": round(dmg_ratio, 2),
                    "kd": round(kd, 2),
                    "net_wipes": round(net_wipes, 2),
                    "pcap_ratio": round(pcap_ratio, 2),
                    "stomp_win_rate": round(100.0 * stomp_wr, 1),
                }
            )

        candidates.sort(key=lambda c: (-c["score"], -c["eff_elo"], -c["wins"], c["profile_id"]))
        champ = candidates[0] if candidates else None
        runner_up = candidates[1] if len(candidates) > 1 else None
        if champ is not None:
            cpid = int(champ["profile_id"])
            tally_counter[cpid] += 1
            tally_meta[cpid] = {
                "profile_id": cpid,
                "alias": champ["alias"],
                "category": champ["category"],
                "is_veterano": champ["is_veterano"],
            }

        champions_out.append(
            {
                "faction": fac,
                "faction_label": finfo["label"],
                "faction_short": finfo["short"],
                "side": finfo["side"],
                "bg_name": bg_name,
                "short_name": meta["short_name"],
                "icon_slug": meta["icon_slug"],
                "candidates_count": len(candidates),
                "champion": champ,
                "runner_up": runner_up,
                "top3": candidates[:3],
            }
        )

    title_tally = [
        {**tally_meta[pid], "titles": cnt}
        for pid, cnt in sorted(tally_counter.items(), key=lambda kv: (-kv[1], tally_meta[kv[0]]["alias"].lower()))
    ]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates_evaluated": len(qualified),
        "total_battlegroups": len(champions_out),
        "title_tally": title_tally,
        "champions": champions_out,
    }


def sync_battlegroup_champions(
    *,
    relic_client: RelicRankingClient,
    open_data_client: Coh3StatsOpenDataClient,
    db: Any,
    custom_players: list[dict[str, Any]],
    cache_ttl_days: int = DEFAULT_BG_CACHE_TTL_DAYS,
    lookback_days: int = DEFAULT_BG_LOOKBACK_DAYS,
    max_scan_seconds: float = 120.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Pipeline completo: pool BR -> Open Data Automatch -> cohdb canônico -> ranking dos 28 Battlegroups."""
    qualified = build_br_bg_candidate_pool(
        custom_players=custom_players,
        relic_client=relic_client,
        include_top_leaderboard_scan=True,
    )
    combat_map = sync_bg_automatch_combat_stats(
        open_data_client=open_data_client,
        db=db,
        candidate_pids=set(qualified.keys()),
        lookback_days=lookback_days,
        max_scan_seconds=max_scan_seconds,
    )
    cohdb = CohDBCanonicalClient()
    bg_map, perf_map = sync_cohdb_battlegroups_and_performance(
        cohdb_client=cohdb,
        db=db,
        qualified=qualified,
        combat_map=combat_map,
        cache_ttl_days=cache_ttl_days,
        force_refresh=force_refresh,
    )
    return compute_battlegroup_champions(qualified, bg_map, combat_map, perf_map)


def compute_battlegroup_champions_digest(report: dict[str, Any]) -> str:
    """Digest determinístico SHA-256[:24] para detectar mudanças reais nos campeões/métricas dos 28 BGs."""
    payload = []
    for entry in report.get("champions") or []:
        c = entry.get("champion") or {}
        r2 = entry.get("runner_up") or {}
        payload.append(
            (
                str(entry.get("faction") or ""),
                str(entry.get("bg_name") or ""),
                int(c.get("profile_id") or 0),
                str(c.get("alias") or ""),
                int(round(float(c.get("score") or 0.0))),
                int(c.get("fac_raw_elo") or 0),
                str(c.get("fac_mode") or ""),
                int(c.get("wins") or 0),
                int(c.get("losses") or 0),
                int(round(float(c.get("apm") or 0.0))),
                int(r2.get("profile_id") or 0),
            )
        )
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def export_battlegroup_champions_files(
    report: dict[str, Any],
    json_path: Path,
    csv_path: Path,
) -> None:
    """Exporta campeoes_battlegroups_br.json e campeoes_battlegroups_br.csv atomicamente (anti-CSV injection)."""
    atomic_write_text(json_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "faccao",
            "lado",
            "battlegroup",
            "campeao_br",
            "categoria",
            "profile_id",
            "score",
            "elo_faccao",
            "modo_elo",
            "elo_1v1_2v2",
            "cohdb_rating",
            "vitorias_bg",
            "derrotas_bg",
            "win_rate_bg",
            "apm",
            "dano_por_min",
            "razao_dano",
            "kd",
            "stomp_pct",
            "vice_br",
            "vice_score",
        ]
    )
    for entry in report.get("champions") or []:
        c = entry.get("champion") or {}
        r2 = entry.get("runner_up") or {}
        writer.writerow(
            [
                sanitize_csv_cell(entry.get("faction_label") or ""),
                sanitize_csv_cell(entry.get("side") or ""),
                sanitize_csv_cell(entry.get("bg_name") or ""),
                sanitize_csv_cell(c.get("alias") or "N/D"),
                sanitize_csv_cell(c.get("category") or ""),
                int(c.get("profile_id") or 0),
                c.get("score", 0.0),
                int(c.get("fac_raw_elo") or 0),
                sanitize_csv_cell(c.get("fac_mode") or ""),
                int(c.get("best_12_elo") or 0),
                int(c.get("cohdb_rating") or 0),
                int(c.get("wins") or 0),
                int(c.get("losses") or 0),
                c.get("win_rate", 0.0),
                c.get("apm", 0.0),
                c.get("dmg_pm", 0.0),
                c.get("dmg_ratio", 0.0),
                c.get("kd", 0.0),
                c.get("stomp_win_rate", 0.0),
                sanitize_csv_cell(r2.get("alias") or ""),
                r2.get("score", 0.0),
            ]
        )
    atomic_write_text(csv_path, buf.getvalue())
