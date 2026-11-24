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

from anti_carry import (
    KNOWN_SMURF_PIDS,
    MODE_COMBAT_WEIGHT,
    MODE_ELO_OFFSET,
    UPSET_MAX_ALLY_CARRY_GAP,
    UPSET_MAX_TEAM_CARRY_GAP,
    UPSET_MIN_ELO_GAP,
    UPSET_MIN_ENEMY_ADJ_ELO,
    blend_team_ally_elo_gap,
    build_smurf_owner_maps,
    compute_2v2_carry_profiles,
    compute_carry_adjustment,
    evaluate_match_ally_carry,
    resolve_smurf_owner_name,
)
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
    extract_steam_id,
    is_excluded_suspected_foreign_pid,
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

# Teto rígido de vitórias para experiência no BG (evita inflação de jogadores com centenas de partidas)
BG_WINS_SATURATION_CAP = 18
STOMP_MIN_ENEMY_ADJ_ELO = 1150.0
STOMP_MAX_PUBSTOMP_ADVANTAGE = 200.0
# Decaimento suave por inatividade na facção (anti-aposentados)
ACTIVITY_GRACE_DAYS = 45.0
ACTIVITY_MAX_DECAY_DAYS = 135.0
ACTIVITY_MIN_MULT = 0.85

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
        snippet = card[:2600]
        m_head = re.search(
            r'src="/assets/races/([^/"]{1,32})/battlegroups/([^"]{1,128})"[^>]{0,300}/>\s*<span[^>]{0,200}>([^<]{1,120})</span>',
            snippet,
            flags=re.S,
        )
        if not m_head:
            continue
        race = strip_unsafe_chars(m_head.group(1)).strip().lower()
        bg_name = strip_unsafe_chars(html.unescape(m_head.group(3))).strip()
        if race not in FACTION_META or not bg_name:
            continue

        clean = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]{1,300}>", " ", snippet)))
        m_rec = re.search(r"Record\s+(\d{1,6})\s*[–-]\s*(\d{1,6})", clean)
        m_wr = re.search(r"Win%\s+([\d.]{1,6})%", clean)
        m_l30 = re.search(r"Last 30\s+(\d{1,3})\s*[–-]\s*(\d{1,3})", clean)
        m_sel = re.search(r"Selections\s+([\d,]{1,9})", clean)
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
        r'performance\?faction=([a-z_]{2,24})&amp;mode=([a-z]{2,12})[^"]{0,120}"[^>]{0,200}>(.{1,400}?)</a>',
        html_text,
        flags=re.S,
    )
    ratings: dict[str, int] = {}
    for cfac, cmode, body in hits:
        fac = COHDB_PERF_FAC_TO_FACTION.get(cfac)
        mode = COHDB_PERF_MODE_TO_MODE.get(cmode)
        if not fac or not mode:
            continue
        val_txt = re.sub(r"\s+", " ", re.sub(r"<[^>]{1,200}>", " ", body)).strip()
        if "Last" in val_txt or "All" in val_txt:
            continue
        m_num = re.search(r"(\d[\d,]{2,6})", val_txt)
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
            roster_country = str(entry.get("country") or "").strip().lower()
            pool[pid] = {
                "profile_id": pid,
                "steam_id": extract_steam_id(entry.get("steam_id")),
                "roster_alias": alias,
                "roster_country": roster_country,
                "known_aliases": [strip_unsafe_chars(k) for k in (entry.get("known_aliases") or []) if strip_unsafe_chars(k)],
                "pin_alias": bool(entry.get("pin_alias")),
                "category": "Outros BR" if is_excluded else "Veterano",
                "is_veterano": not is_excluded,
                "br_verified": True,
                "smurf_of": resolve_smurf_owner_name(pid, entry),
            }

    # 2) Manual BR (outros brasileiros mapeados manualmente)
    for entry in MANUAL_OTHER_BR_ALIASES:
        if not isinstance(entry, dict):
            continue
        pid = int(entry.get("profile_id") or 0)
        if pid <= 0 or pid in pool:
            continue
        alias = strip_unsafe_chars(entry.get("original_alias") or entry.get("alias") or "") or f"Player {pid}"
        roster_country = str(entry.get("country") or "").strip().lower()
        pool[pid] = {
            "profile_id": pid,
            "steam_id": extract_steam_id(entry.get("steam_id")),
            "roster_alias": alias,
            "roster_country": roster_country,
            "known_aliases": [strip_unsafe_chars(k) for k in (entry.get("known_aliases") or []) if strip_unsafe_chars(k)],
            "pin_alias": bool(entry.get("pin_alias")),
            "category": "Outros BR" if pid in EXCLUDED_VETERANO_PIDS else "Manual BR",
            "is_veterano": False,
            "br_verified": True,
            "smurf_of": resolve_smurf_owner_name(pid, entry),
        }

    # Veteranos e brasileiros curados manualmente são protegidos contra falsos positivos de VPN
    protected_pids: set[int] = set(pool.keys())
    blocked_vpn_pids: set[int] = set()

    # 3) Comunidade BR (br_community.json com max_elo >= 1250; bloqueia falsos positivos estrangeiros/VPN)
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
        if is_excluded_suspected_foreign_pid(pid, protected_pids=protected_pids):
            blocked_vpn_pids.add(pid)
            continue
        br_src = str(entry.get("br_source") or "").strip().lower()
        flag_c = str(entry.get("flag_country") or "").strip().lower()
        if br_src == "alias_history" and flag_c != "br":
            continue
        max_elo = int(entry.get("max_elo") or 0)
        if max_elo >= MIN_CANDIDATE_COMMUNITY_ELO:
            alias = strip_unsafe_chars(entry.get("alias") or "") or f"Player {pid}"
            pool[pid] = {
                "profile_id": pid,
                "steam_id": sid,
                "roster_alias": alias,
                "roster_country": flag_c if br_src in ("monitorados", "manual_other_br") else "",
                "known_aliases": [],
                "pin_alias": False,
                "category": "Outros BR",
                "is_veterano": False,
                "br_verified": br_src in ("monitorados", "manual_other_br", "steam_location"),
                "smurf_of": resolve_smurf_owner_name(pid, entry),
            }

    # 4) Seeds confirmados + varredura rápida Top 400 mundial 1v1/2v2
    seed_set = set(EXTRA_SEED_BR_PIDS)
    extra_pids = set(seed_set)
    if include_top_leaderboard_scan:
        try:
            extra_pids.update(discover_top_leaderboard_br_pids(relic_client, max_rank=400))
        except Exception as exc:
            logger.warning("Varredura complementar Top 400 1v1/2v2 indisponível: %s", redact_secrets(exc))
    for pid in extra_pids:
        if pid <= 0 or pid in pool:
            continue
        if is_excluded_suspected_foreign_pid(pid, protected_pids=protected_pids):
            blocked_vpn_pids.add(pid)
            continue
        pool[pid] = {
            "profile_id": pid,
            "steam_id": "",
            "roster_alias": f"Player {pid}",
            "roster_country": "",
            "known_aliases": [],
            "pin_alias": False,
            "category": "Outros BR",
            "is_veterano": False,
            "br_verified": pid in seed_set,
            "smurf_of": KNOWN_SMURF_PIDS.get(pid, ""),
        }

    if blocked_vpn_pids:
        logger.info(
            "[FILTRO VPN/ESTRANGEIRO] %d perfil(is) suspeito(s) removido(s) do pool de Campeões por Battlegroup.",
            len(blocked_vpn_pids),
        )

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
        live_country = str(member.get("country") or "").strip().lower()
        if not pinfo.get("br_verified") and live_country != "br":
            continue
        live_alias = strip_unsafe_chars(member.get("alias") or pinfo["roster_alias"]) or pinfo["roster_alias"]
        live_sid = extract_steam_id(member.get("name") or "") or pinfo["steam_id"]

        queues: dict[str, dict[str, Any]] = {}
        fac_adj: dict[str, float] = {}
        fac_raw: dict[str, int] = {}
        fac_mode: dict[str, str] = {}
        fac_last_ts: dict[str, int] = {}
        best_12_by_fac: dict[str, tuple[int, str]] = {}
        total_games = total_wins = total_drops = 0
        global_last_ts = 0

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
            if lmd > fac_last_ts.get(fac, 0):
                fac_last_ts[fac] = lmd
            if lmd > global_last_ts:
                global_last_ts = lmd

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

        curated_country = str(pinfo.get("roster_country") or "").strip().lower()
        final_country = curated_country if (curated_country and curated_country != "br") else (live_country or "br")
        rec = dict(pinfo)
        rec.pop("br_verified", None)
        rec.pop("roster_country", None)
        rec.update(
            {
                "steam_id": live_sid,
                "live_alias": live_alias,
                "country": final_country,
                "is_expat": bool(final_country and final_country != "br"),
                "level": int(member.get("level") or 0),
                "total_games": total_games,
                "total_wins": total_wins,
                "total_drops": total_drops,
                "global_best_raw_elo": glob_raw,
                "global_best_adj_elo": round(glob_adj, 1),
                "global_last_match_ts": global_last_ts,
                "fac_best_adj_elo": fac_adj,
                "fac_best_raw_elo": fac_raw,
                "fac_best_mode": fac_mode,
                "fac_last_match_ts": fac_last_ts,
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
    smurf_elo_map: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
    """Extrai métricas Automatch v4 (1v1..4v4, >= 5 min): combate, mapa/precap, força rival, upsets e anti-carry."""
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

    smurf_elos = smurf_elo_map or {}

    # Mapa de oldrating via lista matchhistorymember de nível superior (formato Relic API)
    top_member_elo: dict[int, float] = {}
    for mem in match.get("matchhistorymember") or []:
        if isinstance(mem, dict):
            mpid = int(mem.get("profile_id") or 0)
            old_r = float(mem.get("oldrating") or 0.0)
            if mpid > 0 and old_r >= 500.0:
                top_member_elo[mpid] = old_r

    parsed_reps: list[tuple[dict[str, Any], dict[str, Any], float, float]] = []
    team_dmgs: dict[int, list[tuple[int, float]]] = defaultdict(list)
    team_kills: dict[int, list[tuple[int, float]]] = defaultdict(list)
    team_pcaps: dict[int, list[float]] = defaultdict(list)
    team_elos: dict[int, list[tuple[int, float, float]]] = defaultdict(list)
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

        pid_r = int(r.get("profile_id") or 0)
        nested_mem = r.get("matchhistorymember")
        old_elo = 0.0
        if isinstance(nested_mem, dict):
            old_elo = float(nested_mem.get("oldrating") or 0.0)
        if old_elo < 500.0 and pid_r in top_member_elo:
            old_elo = top_member_elo[pid_r]
        if old_elo < 500.0:
            old_elo = 0.0
        true_elo = max(old_elo, float(smurf_elos.get(pid_r) or 0.0))

        tid = r.get("teamid")
        dmg = float(c.get("dmgdone") or 0.0)
        ek = float(c.get("ekills") or 0.0)
        pcap_v = float(c.get("pcap") or 0.0)
        if tid is not None:
            itid = int(tid)
            if dmg > 0.0:
                team_dmgs[itid].append((pid_r, dmg))
            if ek > 0.0:
                team_kills[itid].append((pid_r, ek))
            if pcap_v > 0.0:
                team_pcaps[itid].append(pcap_v)
            if old_elo >= 500.0 or true_elo >= 500.0:
                team_elos[itid].append((pid_r, old_elo, true_elo))
        parsed_reps.append((r, c, old_elo, true_elo))

    mode_offset = MODE_ELO_OFFSET.get(mode, -285.0)
    out: list[dict[str, Any]] = []
    for r, c, my_elo, _my_true_elo in parsed_reps:
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
        veh_kd = min(5.0, (vkill + 1.0) / (vlost + 1.0))
        pcap = float(c.get("pcap") or 0.0)
        plost = float(c.get("plost") or 0.0)
        precap = float(c.get("precap") or 0.0)
        precap_pm = min(5.0, precap / mins)
        net_cap_pm = max(-3.0, min(3.0, (pcap - plost) / mins))
        won = 1 if int(r.get("resulttype") or 0) == 1 else 0

        my_tid = r.get("teamid")
        enemy_dmgs = [d for tid, lst in team_dmgs.items() if my_tid is None or tid != int(my_tid) for _, d in lst]
        enemy_avg = (sum(enemy_dmgs) / len(enemy_dmgs)) if enemy_dmgs else max(1.0, dmg)
        dmg_ratio = min(4.0, dmg / max(100.0, enemy_avg))
        pcap_ratio = min(4.0, (pcap + 2.0) / (plost + 2.0))

        # Controle de mapa relativo ao time adversário (invariante ao modo 1v1..4v4 e tamanho do mapa)
        enemy_pcaps = [p_c for tid, lst in team_pcaps.items() if my_tid is None or tid != int(my_tid) for p_c in lst]
        if enemy_pcaps:
            enemy_avg_pcap = sum(enemy_pcaps) / len(enemy_pcaps)
            pcap_vs_enemy = min(4.0, (pcap + 1.0) / max(1.0, enemy_avg_pcap + 1.0))
        else:
            pcap_vs_enemy = 1.0

        # Anti-carry em partidas em equipe: gap de ELO real dos aliados (resolvendo smurfs!) + contribuição relativa
        if mode != "1v1" and my_tid is not None:
            itid = int(my_tid)
            other_ally_elos = [te for opid, _oe, te in team_elos.get(itid, []) if opid != pid and te >= 500.0]
            other_ally_dmgs = [d for opid, d in team_dmgs.get(itid, []) if opid != pid and d > 0.0]
            other_ally_kills = [k for opid, k in team_kills.get(itid, []) if opid != pid and k > 0.0]
        else:
            other_ally_elos, other_ally_dmgs, other_ally_kills = [], [], []
        ally_elo_gap, dmg_vs_ally, kills_vs_ally, carried_match = evaluate_match_ally_carry(
            mode=mode,
            my_elo=my_elo,
            my_dmg=dmg,
            my_kills=ekills,
            other_ally_elos=other_ally_elos,
            other_ally_dmgs=other_ally_dmgs,
            other_ally_kills=other_ally_kills,
        )

        # Força real de tabela (Strength of Schedule ajustada pelo modo) e vitórias competitivas vs ELO maior
        enemy_elos = [oe for tid, lst in team_elos.items() if my_tid is None or tid != int(my_tid) for _, oe, _ in lst if oe >= 500.0]
        ally_true_elos = [te for _, _oe, te in team_elos.get(int(my_tid), []) if te >= 500.0] if my_tid is not None else []
        enemy_avg_elo = (sum(enemy_elos) / len(enemy_elos)) if enemy_elos else 0.0
        enemy_adj_elo = max(0.0, enemy_avg_elo + mode_offset) if enemy_avg_elo >= 500.0 else 0.0
        ally_avg_true_elo = (sum(ally_true_elos) / len(ally_true_elos)) if ally_true_elos else my_elo
        upset_opp = 0
        upset_win = 0
        upset_elo_gap = 0.0
        upset_quality = 0.0
        is_carried_upset = mode != "1v1" and (
            carried_match == 1 or ally_elo_gap >= UPSET_MAX_ALLY_CARRY_GAP or dmg_vs_ally < 0.88
        )
        if my_elo >= 500.0 and enemy_avg_elo >= 500.0 and enemy_adj_elo >= UPSET_MIN_ENEMY_ADJ_ELO and not is_carried_upset:
            gap_vs_me = enemy_avg_elo - my_elo
            gap_vs_team = enemy_avg_elo - ally_avg_true_elo
            if gap_vs_me >= UPSET_MIN_ELO_GAP and gap_vs_team >= -UPSET_MAX_TEAM_CARRY_GAP:
                upset_opp = 1
                if won == 1:
                    upset_win = 1
                    upset_elo_gap = min(400.0, gap_vs_me)
                    # Qualidade do upset pondera diferença de ELO + nível absoluto do adversário vencido
                    rival_tier = max(0.45, min(1.35, (enemy_adj_elo - 1150.0) / 500.0))
                    upset_quality = min(450.0, upset_elo_gap * rival_tier)

        # Vitória elástica (bloqueia pubstomp contra lobbies fracos E vitórias carregadas em equipe)
        is_pubstomp_mismatch = (
            (enemy_adj_elo > 0.0 and enemy_adj_elo < STOMP_MIN_ENEMY_ADJ_ELO)
            or (my_elo >= 500.0 and enemy_avg_elo >= 500.0 and (my_elo - enemy_avg_elo) > STOMP_MAX_PUBSTOMP_ADVANTAGE)
            or (mode != "1v1" and (carried_match == 1 or dmg_vs_ally < 0.90))
        )
        is_stomp_win = 0
        if won == 1 and not is_pubstomp_mismatch:
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
                "veh_kd": veh_kd,
                "pcap_ratio": pcap_ratio,
                "pcap_vs_enemy": pcap_vs_enemy,
                "precap_pm": precap_pm,
                "net_cap_pm": net_cap_pm,
                "my_elo": my_elo,
                "enemy_avg_elo": enemy_avg_elo,
                "enemy_adj_elo": enemy_adj_elo,
                "upset_opp": upset_opp,
                "upset_win": upset_win,
                "upset_elo_gap": upset_elo_gap,
                "upset_quality": upset_quality,
                "ally_elo_gap": round(ally_elo_gap, 1),
                "dmg_vs_ally": round(dmg_vs_ally, 3),
                "kills_vs_ally": round(kills_vs_ally, 3),
                "carried_match": carried_match,
                "stomp_win": is_stomp_win,
                "completion_ts": completion_ts,
            }
        )
    return out


def _fetch_relic_recent_automatch_rows(
    profile_id: int,
    smurf_elo_map: dict[int, float] | None = None,
) -> list[dict[str, Any]]:
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
            rows.extend(extract_automatch_combat_rows(m, target, smurf_elo_map=smurf_elo_map))
    return rows


def summarize_combat_rows(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Agrega métricas Automatch ponderadas pelo modo (1v1/2v2 > 3v3 > 4v4) por jogador (geral e por facção)."""
    by_pid_all: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_pid_fac: dict[int, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))

    for r in rows:
        pid = int(r["profile_id"])
        fac = str(r["faction"])
        by_pid_all[pid].append(r)
        by_pid_fac[pid][fac].append(r)

    def _wavg(items: list[dict[str, Any]], key: str, default: float = 0.0) -> float:
        num = 0.0
        den = 0.0
        for x in items:
            w = MODE_COMBAT_WEIGHT.get(str(x.get("mode") or ""), 0.5)
            num += w * float(x.get(key, default) or 0.0)
            den += w
        return (num / den) if den > 0.0 else default

    def _agg(items: list[dict[str, Any]]) -> dict[str, Any]:
        if not items:
            return {}
        n = len(items)
        wins = [x for x in items if int(x["won"]) == 1]
        apms = [float(x["apm"]) for x in items]
        # APM ponderado por modo + mediana para estabilidade contra outliers
        w_apm_mean = _wavg(items, "apm", 0.0)
        med_apm = statistics.median(apms)
        apm_eff = 0.55 * w_apm_mean + 0.45 * med_apm

        # Força média real dos adversários enfrentados (Strength of Schedule ajustado por modo)
        sos_items = [x for x in items if float(x.get("enemy_adj_elo") or 0.0) > 0.0]
        avg_enemy_adj_elo = _wavg(sos_items, "enemy_adj_elo", 0.0) if sos_items else 0.0

        # Upsets competitivos ponderados por modo
        up_opps_raw = sum(int(x.get("upset_opp") or 0) for x in items)
        up_wins_items = [x for x in items if int(x.get("upset_win") or 0) == 1]
        up_wins_raw = len(up_wins_items)
        up_opps_w = sum(
            MODE_COMBAT_WEIGHT.get(str(x.get("mode") or ""), 0.5)
            for x in items
            if int(x.get("upset_opp") or 0) == 1
        )
        up_wins_w = sum(
            MODE_COMBAT_WEIGHT.get(str(x.get("mode") or ""), 0.5)
            for x in up_wins_items
        )
        up_gap_avg = _wavg(up_wins_items, "upset_elo_gap", 0.0) if up_wins_items else 0.0
        up_qual_avg = _wavg(up_wins_items, "upset_quality", 0.0) if up_wins_items else 0.0

        # Stomp ponderado por modo sobre vitórias
        stomp_num = sum(
            MODE_COMBAT_WEIGHT.get(str(x.get("mode") or ""), 0.5) * int(x["stomp_win"])
            for x in wins
        )
        stomp_den = sum(MODE_COMBAT_WEIGHT.get(str(x.get("mode") or ""), 0.5) for x in wins)
        stomp_wr = (stomp_num / stomp_den) if stomp_den > 0.0 else 0.0

        # Eficiência agregada de veículos (vkill/vlost)
        vkill_sum = sum(MODE_COMBAT_WEIGHT.get(str(x.get("mode") or ""), 0.5) * float(x.get("vkill") or 0.0) for x in items)
        vlost_sum = sum(MODE_COMBAT_WEIGHT.get(str(x.get("mode") or ""), 0.5) * float(x.get("vlost") or 0.0) for x in items)
        veh_kd_agg = min(5.0, (vkill_sum + 2.0) / (vlost_sum + 2.0))

        # Telemetria anti-carry em jogos de equipe (2v2/3v3/4v4 — idêntica ao Ranking Unificado)
        team_items = [x for x in items if str(x.get("mode") or "") != "1v1"]
        team_n = len(team_items)
        avg_ally_elo_gap = blend_team_ally_elo_gap(team_items) if team_n > 0 else 0.0
        avg_dmg_vs_ally = _wavg(team_items, "dmg_vs_ally", 1.0) if team_n > 0 else 1.0
        avg_kills_vs_ally = _wavg(team_items, "kills_vs_ally", 1.0) if team_n > 0 else 1.0
        carried_match_rate = _wavg(team_items, "carried_match", 0.0) if team_n > 0 else 0.0

        return {
            "n": n,
            "team_n": team_n,
            "wins": len(wins),
            "apm_mean": round(w_apm_mean, 1),
            "apm_median": round(apm_eff, 1),
            "dmg_pm": round(_wavg(items, "dmg_pm", 0.0), 1),
            "dmg_avg": round(_wavg(items, "dmg", 0.0), 0),
            "dmg_ratio": round(_wavg(items, "dmg_ratio", 1.0), 3),
            "kd": round(_wavg(items, "kd", 1.0), 3),
            "net_wipes": round(_wavg(items, "net_wipes", 0.0), 2),
            "veh_kd": round(veh_kd_agg, 3),
            "pcap_ratio": round(_wavg(items, "pcap_ratio", 1.0), 3),
            "pcap_vs_enemy": round(_wavg(items, "pcap_vs_enemy", 1.0), 3),
            "precap_pm": round(_wavg(items, "precap_pm", 0.0), 3),
            "net_cap_pm": round(_wavg(items, "net_cap_pm", 0.0), 3),
            "avg_enemy_adj_elo": round(avg_enemy_adj_elo, 1),
            "avg_ally_elo_gap": round(avg_ally_elo_gap, 1),
            "avg_dmg_vs_ally": round(avg_dmg_vs_ally, 3),
            "avg_kills_vs_ally": round(avg_kills_vs_ally, 3),
            "carried_match_rate": round(carried_match_rate, 3),
            "upset_opps": up_opps_raw,
            "upset_wins": up_wins_raw,
            "upset_opps_w": round(up_opps_w, 2),
            "upset_wins_w": round(up_wins_w, 2),
            "upset_win_rate": round(up_wins_raw / up_opps_raw, 3) if up_opps_raw > 0 else 0.0,
            "upset_avg_gap": round(up_gap_avg, 1),
            "upset_avg_quality": round(up_qual_avg, 1),
            "stomp_win_rate": round(stomp_wr, 3),
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
    smurf_elo_map: dict[int, float] | None = None,
    fetch_relic_fallback: bool = True,
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
                            rows.extend(extract_automatch_combat_rows(m, candidate_pids, smurf_elo_map=smurf_elo_map))
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

    # Complementa jogadores com < 12 partidas Automatch na janela via histórico recente da Relic,
    # sem vazar partidas antigas (>lookback_days) para jogadores já ativos na janela recente.
    if fetch_relic_fallback:
        cnt_by_pid = Counter(int(r["profile_id"]) for r in cached_rows)
        low_pids = [pid for pid in sorted(candidate_pids) if cnt_by_pid.get(pid, 0) < 12]
        if low_pids:
            low_pid_set = set(low_pids)
            extra_rows: list[dict[str, Any]] = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                for rows in ex.map(lambda p: _fetch_relic_recent_automatch_rows(p, smurf_elo_map=smurf_elo_map), low_pids):
                    extra_rows.extend(rows)
            if extra_rows:
                db.save_bg_combat_matches(extra_rows)
                all_rows = db.load_bg_combat_matches(since_ts=0)
                cached_rows = [
                    r
                    for r in all_rows
                    if int(r.get("completion_ts") or 0) >= since_ts or int(r["profile_id"]) in low_pid_set
                ]

    return summarize_combat_rows(cached_rows)


def sync_cohdb_battlegroups_and_performance(
    cohdb_client: CohDBCanonicalClient,
    db: Any,
    qualified: dict[int, dict[str, Any]],
    combat_map: dict[int, dict[str, Any]],
    cache_ttl_days: int = DEFAULT_BG_CACHE_TTL_DAYS,
    force_refresh: bool = False,
    carry_profiles: dict[int, dict[str, Any]] | None = None,
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
    pre_report = compute_battlegroup_champions(qualified, bg_map, combat_map, perf_map, carry_profiles=carry_profiles)
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
    """Resolve nome de exibição respeitando apelido fixo/known_aliases e smurf_of de `players_roster.json`."""
    live = strip_unsafe_chars(player.get("live_alias") or "") or f"Player {player.get('profile_id', 0)}"
    roster_al = strip_unsafe_chars(player.get("roster_alias") or "")
    smurf_owner = strip_unsafe_chars(player.get("smurf_of") or "").strip()
    if smurf_owner:
        if roster_al and f"(smurf: {smurf_owner.casefold()})" in roster_al.casefold():
            return roster_al
        if f"(smurf: {smurf_owner.casefold()})" in live.casefold():
            return live
        return f"{live} (Smurf: {smurf_owner})"
    if not roster_al or roster_al.startswith("Player "):
        return live
    if "Principal" in roster_al or "(" in roster_al:
        return live
    if bool(player.get("pin_alias")):
        return roster_al
    if roster_al.casefold() == live.casefold():
        return roster_al
    known_cf = {
        strip_unsafe_chars(k).casefold()
        for k in (player.get("known_aliases") or [])
        if strip_unsafe_chars(k)
    }
    if live.casefold() in known_cf or live.casefold() in roster_al.casefold():
        return roster_al
    if roster_al.casefold() not in live.casefold():
        return f"{live} ({roster_al})"
    return live


def _compute_carry_adjustment(
    *,
    fac_mode: str,
    eff_elo: float,
    best_1v1_elo: int,
    team_n: int,
    avg_ally_elo_gap: float,
    avg_dmg_vs_ally: float,
    avg_kills_vs_ally: float,
    carried_match_rate: float,
    arr_profile: dict[str, Any] | None,
) -> tuple[float, float, float]:
    """Wrapper fino para o motor Anti-Carry compartilhado (`anti_carry.compute_carry_adjustment`)."""
    return compute_carry_adjustment(
        mode=fac_mode,
        eff_elo=eff_elo,
        best_1v1_elo=best_1v1_elo,
        team_n=team_n,
        avg_ally_elo_gap=avg_ally_elo_gap,
        avg_dmg_vs_ally=avg_dmg_vs_ally,
        avg_kills_vs_ally=avg_kills_vs_ally,
        carried_match_rate=carried_match_rate,
        arr_profile=arr_profile,
    )


def _compute_activity_multiplier(
    fac_last_ts: int,
    global_last_ts: int,
    now_ts: float | None = None,
) -> tuple[float, int]:
    """Calcula fator suave de atividade na facção (1.00 até 45d; decai suavemente até 0.85 após 135d)."""
    ref_ts = fac_last_ts if fac_last_ts > 0 else global_last_ts
    if ref_ts <= 0:
        return 1.0, -1
    cur = float(now_ts if now_ts is not None else time.time())
    days_inactive = max(0.0, (cur - float(ref_ts)) / 86400.0)
    if days_inactive <= ACTIVITY_GRACE_DAYS:
        return 1.0, int(round(days_inactive))
    span = max(1.0, ACTIVITY_MAX_DECAY_DAYS - ACTIVITY_GRACE_DAYS)
    frac = min(1.0, (days_inactive - ACTIVITY_GRACE_DAYS) / span)
    mult = 1.0 - frac * (1.0 - ACTIVITY_MIN_MULT)
    return round(mult, 4), int(round(days_inactive))


def _compute_combat_efficiency_score(
    *,
    has_combat: bool,
    dmg_pm: float,
    dmg_ratio: float,
    kd: float,
    net_wipes: float,
    veh_kd: float,
) -> float:
    """Avalia eficiência de combate neutra (0..1) sem viés por tema de Battlegroup."""
    if not has_combat or dmg_pm <= 0.0:
        return 0.35

    dmg_rat_n = max(0.0, min(1.0, (dmg_ratio - 0.78) / 0.65))
    dmg_pm_n = max(0.0, min(1.0, (dmg_pm - 230.0) / 350.0))
    kd_n = max(0.0, min(1.0, (kd - 0.80) / 0.85))
    wipes_n = max(0.0, min(1.0, (net_wipes + 1.0) / 4.0))
    veh_kd_n = max(0.0, min(1.0, (veh_kd - 0.65) / 1.40))

    score = (
        0.32 * dmg_rat_n
        + 0.18 * dmg_pm_n
        + 0.20 * kd_n
        + 0.14 * wipes_n
        + 0.16 * veh_kd_n
    )
    return max(0.0, min(1.0, score))


def compute_battlegroup_champions(
    qualified: dict[int, dict[str, Any]],
    bg_map: dict[int, dict[str, Any]],
    combat_map: dict[int, dict[str, Any]],
    perf_map: dict[int, dict[str, int]] | None = None,
    now_ts: float | None = None,
    carry_profiles: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Calcula o índice v3.2 (0–1000) com anti-carry, forma L30, Força de Tabela, atividade, combate neutro e mapa."""
    perf_lookup = perf_map or {}
    carry_lookup = carry_profiles or {}
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

            # 1) ELO ajustado por modo (1v1/2v2 > 3v3 > 4v4) + cohdb Rating + Atividade na facção
            f_adj = float((p.get("fac_best_adj_elo") or {}).get(fac) or 0.0)
            f_raw = int((p.get("fac_best_raw_elo") or {}).get(fac) or 0)
            f_mode = str((p.get("fac_best_mode") or {}).get(fac) or "?")
            g_adj = float(p.get("global_best_adj_elo") or 0.0)
            best_1v1_elo = max(
                (
                    int(q.get("rating") or 0)
                    for kq, q in (p.get("queues") or {}).items()
                    if kq.endswith(":1v1") and int(q.get("games") or 0) >= 8
                ),
                default=0,
            )

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
                int(v) + MODE_ELO_OFFSET.get(k.split(":")[1], -285.0)
                for k, v in pr.items()
                if ":" in k and k.startswith(fac + ":") and 500 <= int(v) <= 3000
            ]
            all_prs = [
                int(v) + MODE_ELO_OFFSET.get(k.split(":")[1], -285.0)
                for k, v in pr.items()
                if ":" in k and 500 <= int(v) <= 3000
            ]
            cohdb_r = max(fac_prs) if fac_prs else (max(all_prs) * 0.94 if all_prs else 0.0)
            cohdb_best_raw = max(
                (int(v) for k, v in pr.items() if k.startswith(fac + ":") and 500 <= int(v) <= 3000),
                default=max((int(v) for v in pr.values() if 500 <= int(v) <= 3000), default=0),
            )

            skill_elo = (0.80 * eff_elo + 0.20 * cohdb_r) if cohdb_r > 0.0 else eff_elo

            fac_last_ts = int((p.get("fac_last_match_ts") or {}).get(fac) or 0)
            glob_last_ts = int(p.get("global_last_match_ts") or 0)
            activity_mult, days_inactive = _compute_activity_multiplier(fac_last_ts, glob_last_ts, now_ts=now_ts)

            # 2) Métricas Automatch ponderadas por modo com shrinkage bayesiano por facção
            c_over = (combat_map.get(pid) or {}).get("overall") or {}
            c_fac = ((combat_map.get(pid) or {}).get("by_faction") or {}).get(fac) or {}
            n_fac = int(c_fac.get("n") or 0)
            n_over = int(c_over.get("n") or 0)
            combat_keys = (
                "apm_median",
                "apm_mean",
                "dmg_pm",
                "dmg_ratio",
                "kd",
                "net_wipes",
                "veh_kd",
                "pcap_ratio",
                "pcap_vs_enemy",
                "precap_pm",
                "net_cap_pm",
                "avg_enemy_adj_elo",
                "stomp_win_rate",
            )
            if n_fac > 0 and n_over > 0:
                wf = n_fac / (n_fac + 8.0)
                c_use = {
                    k: wf * float(c_fac.get(k, 0.0)) + (1.0 - wf) * float(c_over.get(k, 0.0))
                    for k in combat_keys
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
            veh_kd = float(c_use.get("veh_kd") or 1.0)
            pcap_ratio = float(c_use.get("pcap_ratio") or 1.0)
            pcap_vs_enemy = float(c_use.get("pcap_vs_enemy") or 1.0)
            precap_pm = float(c_use.get("precap_pm") or 0.0)
            net_cap_pm = float(c_use.get("net_cap_pm") or 0.0)
            avg_enemy_adj_elo = float(c_use.get("avg_enemy_adj_elo") or 0.0)
            stomp_wr = float(c_use.get("stomp_win_rate") or 0.0)

            # Métricas anti-carry em jogos de equipe (usa overall para amostra robusta em 2v2/3v3/4v4)
            team_n = int(c_over.get("team_n") or 0)
            avg_ally_elo_gap = float(c_over.get("avg_ally_elo_gap") or 0.0)
            avg_dmg_vs_ally = float(c_over.get("avg_dmg_vs_ally") or 1.0)
            avg_kills_vs_ally = float(c_over.get("avg_kills_vs_ally") or 1.0)
            carried_match_rate = float(c_over.get("carried_match_rate") or 0.0)
            arr_prof = carry_lookup.get(pid)
            carry_mult, elo_deflation, carry_risk = _compute_carry_adjustment(
                fac_mode=f_mode,
                eff_elo=eff_elo,
                best_1v1_elo=best_1v1_elo,
                team_n=team_n,
                avg_ally_elo_gap=avg_ally_elo_gap,
                avg_dmg_vs_ally=avg_dmg_vs_ally,
                avg_kills_vs_ally=avg_kills_vs_ally,
                carried_match_rate=carried_match_rate,
                arr_profile=arr_prof,
            )
            adj_skill_elo = max(800.0, skill_elo - elo_deflation)
            adj_eff_elo = max(800.0, eff_elo - elo_deflation)

            # Vitórias competitivas contra adversários de ELO maior (ponderadas por modo + qualidade do rival)
            u_fac_opps_w = float(c_fac.get("upset_opps_w") if "upset_opps_w" in c_fac else (c_fac.get("upset_opps") or 0))
            u_fac_wins_w = float(c_fac.get("upset_wins_w") if "upset_wins_w" in c_fac else (c_fac.get("upset_wins") or 0))
            u_fac_qual = float(c_fac.get("upset_avg_quality") or c_fac.get("upset_avg_gap") or 0.0)
            u_all_opps = int(c_over.get("upset_opps") or 0)
            u_all_wins = int(c_over.get("upset_wins") or 0)
            u_all_opps_w = float(c_over.get("upset_opps_w") if "upset_opps_w" in c_over else u_all_opps)
            u_all_wins_w = float(c_over.get("upset_wins_w") if "upset_wins_w" in c_over else u_all_wins)
            u_all_qual = float(c_over.get("upset_avg_quality") or c_over.get("upset_avg_gap") or 0.0)
            u_all_gap = float(c_over.get("upset_avg_gap") or 0.0)

            other_u_opps = max(0.0, u_all_opps_w - u_fac_opps_w)
            other_u_wins = max(0.0, u_all_wins_w - u_fac_wins_w)
            eff_u_opps = u_fac_opps_w + 0.50 * other_u_opps
            eff_u_wins = u_fac_wins_w + 0.50 * other_u_wins
            if u_fac_wins_w > 0.0 and u_all_wins_w > 0.0:
                w_ug = u_fac_wins_w / (u_fac_wins_w + 4.0)
                eff_u_qual = w_ug * u_fac_qual + (1.0 - w_ug) * u_all_qual
            elif u_fac_wins_w > 0.0:
                eff_u_qual = u_fac_qual
            else:
                eff_u_qual = u_all_qual
            bayes_upset_wr = (eff_u_wins + 2.0) / (eff_u_opps + 5.0)
            raw_upset_wr = (u_all_wins / u_all_opps) if u_all_opps > 0 else 0.0

            # ---- Pontuação composta v3.2 (0..1000, anti-carry + sem viés por tema de BG) ----
            # 1) ELO Efetivo da Facção (deflacionado se carregado) + Força Real de Tabela (SoS) × Atividade (300 pts)
            elo_norm = max(0.0, min(1.0, (adj_skill_elo - 1050.0) / 700.0))
            elo_gate = min(adj_eff_elo, f_adj - elo_deflation) if f_adj > 0.0 else adj_eff_elo
            if elo_gate < 1300.0:
                gate_lin = max(0.20, (elo_gate - 950.0) / 350.0)
                elo_norm *= max(0.15, gate_lin * gate_lin)
            if avg_enemy_adj_elo > 0.0:
                sos_norm = max(0.0, min(1.0, (avg_enemy_adj_elo - 1000.0) / 650.0))
                elo_sos_n = 0.82 * elo_norm + 0.18 * sos_norm
            else:
                elo_sos_n = elo_norm
            pts_elo = 300.0 * elo_sos_n * activity_mult * carry_mult

            # Desconto anti-pubstomp para métricas de combate/mapa farmadas em lobbies muito abaixo de 1150 ELO ajustado
            if avg_enemy_adj_elo > 0.0 and avg_enemy_adj_elo < 1150.0:
                sos_combat_mult = max(0.55, (avg_enemy_adj_elo - 650.0) / 500.0)
            else:
                sos_combat_mult = 1.0

            # 2) Aproveitamento no BG: Histórico (62%) + Forma Recente Last 30 (38%) × anti-carry (250 pts)
            raw_wr_frac = wins / max(1, sel)
            l30_w = int(bg.get("l30_w") or 0)
            l30_l = int(bg.get("l30_l") or 0)
            l30_sel = l30_w + l30_l
            l30_raw_wr = (100.0 * l30_w / l30_sel) if l30_sel > 0 else float(bg.get("wr") or 0.0)
            bayes_wr_all = (wins + 5.0) / (sel + 10.0)
            if l30_sel > 0:
                bayes_wr_l30 = (l30_w + 4.0) / (l30_sel + 8.0)
                w_l30 = 0.38 * min(1.0, l30_sel / 20.0)
                bayes_wr = (1.0 - w_l30) * bayes_wr_all + w_l30 * bayes_wr_l30
            else:
                bayes_wr = bayes_wr_all
            wr_norm = max(0.0, min(1.0, (bayes_wr - 0.50) / 0.24))
            if raw_wr_frac < 0.50:
                wr_norm = 0.0
            elif l30_sel >= 15 and (l30_w / l30_sel) < 0.45:
                # Penalização suave para quem caiu abaixo de 45% nos últimos 15–30 jogos do BG (meta atual)
                wr_norm *= 0.72
            pts_wr = 250.0 * wr_norm * carry_mult

            # 3) Eficiência de Combate Neutra e Equilibrada (145 pts — mesmo critério objetivo para todos os BGs)
            combat_norm = _compute_combat_efficiency_score(
                has_combat=c_n > 0,
                dmg_pm=dmg_pm,
                dmg_ratio=dmg_ratio,
                kd=kd,
                net_wipes=net_wipes,
                veh_kd=veh_kd,
            )
            pts_combat = 145.0 * combat_norm * sos_combat_mult

            # 4) Controle de Mapa Fino (pcap/plost + vs inimigo + precap/min + saldo líquido) & Stomp × anti-carry (100 pts)
            if c_n > 0:
                pcap_rat_n = max(0.0, min(1.0, (pcap_ratio - 1.00) / 0.90))
                pcap_en_n = max(0.0, min(1.0, (pcap_vs_enemy - 0.80) / 0.50))
                precap_n = max(0.0, min(1.0, (precap_pm - 0.35) / 0.90))
                net_cap_n = max(0.0, min(1.0, (net_cap_pm + 0.25) / 0.80))
                map_ctrl_n = 0.40 * pcap_rat_n + 0.30 * pcap_en_n + 0.20 * precap_n + 0.10 * net_cap_n
                stomp_n = max(0.0, min(1.0, stomp_wr / 0.65))
            else:
                map_ctrl_n = 0.35
                stomp_n = 0.35
            pts_dom = 100.0 * (0.68 * map_ctrl_n + 0.32 * stomp_n) * sos_combat_mult * carry_mult

            # 5) Vitórias Competitivas contra ELO Maior — Giant-Killer (>=1350 adj, anti-carry) (95 pts)
            if eff_u_opps > 0.0:
                upset_wr_n = max(0.0, min(1.0, (bayes_upset_wr - 0.22) / 0.43))
                upset_qual_n = max(0.0, min(1.0, eff_u_qual / 160.0)) * min(1.0, eff_u_wins / 3.5)
                upset_norm = 0.70 * upset_wr_n + 0.30 * upset_qual_n
            else:
                upset_norm = 0.38  # neutro caso não tenha enfrentado ELO maior competitivo na janela
            pts_upset = 95.0 * upset_norm

            # 6) APM Automatch ponderado por modo (75 pts)
            apm_norm = max(0.0, min(1.0, (apm - 25.0) / 85.0)) if apm > 0.0 else 0.30
            pts_apm = 75.0 * apm_norm

            # 7) Amostra mínima no BG com teto rígido em 18 vitórias (35 pts — zero inflação por volume!)
            wins_capped = min(wins, BG_WINS_SATURATION_CAP)
            wins_norm = math.log1p(wins_capped) / math.log1p(float(BG_WINS_SATURATION_CAP))
            sample_mult = min(1.0, sel / 10.0) if sel < 10 else 1.0
            pts_wins = (31.0 * wins_norm * sample_mult) + (4.0 * min(1.0, l30_sel / 6.0))
            if raw_wr_frac < 0.50:
                pts_wins *= 0.50

            total_score = pts_elo + pts_wr + pts_combat + pts_dom + pts_upset + pts_apm + pts_wins
            if elo_gate < 1300.0:
                total_score *= max(0.75, 1.0 - (1300.0 - elo_gate) / 600.0)
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
                    "eff_elo": round(adj_eff_elo, 1),
                    "fac_raw_elo": f_raw,
                    "fac_mode": f_mode,
                    "best_12_elo": int(b12_pair[0]) if b12_pair else 0,
                    "best_12_mode": str(b12_pair[1]) if b12_pair else "",
                    "cohdb_rating": int(cohdb_best_raw),
                    "avg_enemy_adj_elo": round(avg_enemy_adj_elo, 1),
                    "days_inactive_fac": days_inactive,
                    "activity_mult": activity_mult,
                    "carry_mult": carry_mult,
                    "carry_risk": carry_risk,
                    "carry_2v2_share": round(100.0 * float((arr_prof or {}).get("carry_2v2_share") or 0.0), 1),
                    "avg_ally_elo_gap": round(avg_ally_elo_gap, 1),
                    "dmg_vs_ally": round(avg_dmg_vs_ally, 2),
                    "wins": wins,
                    "losses": losses,
                    "selections": sel,
                    "win_rate": round(float(bg.get("wr") or 0.0), 1),
                    "bayes_win_rate": round(100.0 * bayes_wr, 1),
                    "l30_wins": l30_w,
                    "l30_losses": l30_l,
                    "l30_win_rate": round(l30_raw_wr, 1),
                    "apm": round(apm, 1),
                    "dmg_pm": round(dmg_pm, 1),
                    "dmg_ratio": round(dmg_ratio, 2),
                    "kd": round(kd, 2),
                    "net_wipes": round(net_wipes, 2),
                    "veh_kd": round(veh_kd, 2),
                    "combat_score": round(100.0 * combat_norm, 1),
                    "pcap_ratio": round(pcap_ratio, 2),
                    "pcap_vs_enemy": round(pcap_vs_enemy, 2),
                    "precap_pm": round(precap_pm, 2),
                    "net_cap_pm": round(net_cap_pm, 2),
                    "upset_wins": u_all_wins,
                    "upset_opps": u_all_opps,
                    "upset_win_rate": round(100.0 * raw_upset_wr, 1),
                    "bayes_upset_wr": round(100.0 * bayes_upset_wr, 1),
                    "upset_avg_gap": round(u_all_gap, 1),
                    "stomp_win_rate": round(100.0 * stomp_wr, 1),
                }
            )

        candidates.sort(key=lambda c: (-c["score"], -c["bayes_win_rate"], -c["eff_elo"], c["profile_id"]))
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
                "belt_status": "initial",
                "streak_weeks": 1 if champ is not None else 0,
                "prev_champion_alias": "",
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
        "belt_changes": [],
        "champions": champions_out,
    }


def apply_weekly_belt_tracking(
    report: dict[str, Any],
    prev_belts: dict[str, dict[str, Any]] | None,
    slot_id: str = "",
) -> list[dict[str, Any]]:
    """Aplica o rastreamento semanal de Defesa de Cinturão (NOVO campeão / sequência de semanas) ao relatório.

    Retorna a lista de registros prontos para persistir em `db.save_bg_belt_state(...)` após publicação real.
    """
    prev_map = prev_belts or {}
    belt_changes: list[dict[str, Any]] = []
    rows_to_save: list[dict[str, Any]] = []
    now_ts = int(time.time())

    for entry in report.get("champions") or []:
        champ = entry.get("champion")
        if not isinstance(champ, dict):
            continue
        bg_key = f"{entry.get('faction')}|{entry.get('bg_name')}"
        cpid = int(champ.get("profile_id") or 0)
        calias = str(champ.get("alias") or "")
        cscore = float(champ.get("score") or 0.0)
        prev = prev_map.get(bg_key)

        if not prev:
            status = "initial"
            streak = 1
            prev_pid = 0
            prev_alias = ""
        elif slot_id and str(prev.get("last_slot_id") or "") == slot_id:
            # Mesma janela semanal já registrada (idempotente — não incrementa streak duas vezes)
            if int(prev.get("champion_pid") or 0) == cpid:
                streak = max(1, int(prev.get("streak_weeks") or 1))
                prev_pid = int(prev.get("prev_champion_pid") or 0)
                prev_alias = str(prev.get("prev_champion_alias") or "")
                status = "new" if (streak == 1 and prev_alias) else ("defended" if streak >= 2 else "initial")
            else:
                status = "new"
                streak = 1
                prev_pid = int(prev.get("champion_pid") or 0)
                prev_alias = str(prev.get("champion_alias") or "")
        elif int(prev.get("champion_pid") or 0) == cpid:
            status = "defended"
            streak = max(1, int(prev.get("streak_weeks") or 1)) + 1
            prev_pid = int(prev.get("prev_champion_pid") or 0)
            prev_alias = str(prev.get("prev_champion_alias") or "")
        else:
            status = "new"
            streak = 1
            prev_pid = int(prev.get("champion_pid") or 0)
            prev_alias = str(prev.get("champion_alias") or "")

        entry["belt_status"] = status
        entry["streak_weeks"] = streak
        entry["prev_champion_alias"] = prev_alias
        champ["belt_status"] = status
        champ["streak_weeks"] = streak
        champ["prev_champion_alias"] = prev_alias

        if status == "new" and prev_alias:
            belt_changes.append(
                {
                    "faction_short": str(entry.get("faction_short") or ""),
                    "short_name": str(entry.get("short_name") or entry.get("bg_name") or ""),
                    "new_champion": calias,
                    "prev_champion": prev_alias,
                    "score": cscore,
                }
            )

        rows_to_save.append(
            {
                "bg_key": bg_key,
                "champion_pid": cpid,
                "champion_alias": calias,
                "champion_score": cscore,
                "streak_weeks": streak,
                "prev_champion_pid": prev_pid,
                "prev_champion_alias": prev_alias,
                "last_slot_id": slot_id,
                "updated_at": now_ts,
            }
        )

    report["belt_changes"] = belt_changes
    return rows_to_save


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
    slot_id: str = "",
) -> dict[str, Any]:
    """Pipeline completo: pool BR -> Open Data Automatch v4 (anti-carry) -> cohdb canônico -> ranking dos 28 BGs + cinturões."""
    qualified = build_br_bg_candidate_pool(
        custom_players=custom_players,
        relic_client=relic_client,
        include_top_leaderboard_scan=True,
    )
    smurf_owner_map, smurf_elo_map = build_smurf_owner_maps(qualified)
    combat_map = sync_bg_automatch_combat_stats(
        open_data_client=open_data_client,
        db=db,
        candidate_pids=set(qualified.keys()),
        lookback_days=lookback_days,
        max_scan_seconds=max_scan_seconds,
        smurf_elo_map=smurf_elo_map,
    )
    arranged_pairs = db.load_arranged_2v2_pairs() if hasattr(db, "load_arranged_2v2_pairs") else []
    carry_profiles = compute_2v2_carry_profiles(arranged_pairs, qualified, smurf_owner_map=smurf_owner_map)
    cohdb = CohDBCanonicalClient()
    bg_map, perf_map = sync_cohdb_battlegroups_and_performance(
        cohdb_client=cohdb,
        db=db,
        qualified=qualified,
        combat_map=combat_map,
        cache_ttl_days=cache_ttl_days,
        force_refresh=force_refresh,
        carry_profiles=carry_profiles,
    )
    report = compute_battlegroup_champions(
        qualified,
        bg_map,
        combat_map,
        perf_map,
        carry_profiles=carry_profiles,
    )
    prev_belts = db.get_all_bg_belt_state() if hasattr(db, "get_all_bg_belt_state") else {}
    report["_belt_rows_to_save"] = apply_weekly_belt_tracking(report, prev_belts, slot_id=slot_id)
    return report


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
                int(c.get("l30_wins") or 0),
                int(c.get("l30_losses") or 0),
                int(round(float(c.get("apm") or 0.0))),
                round(float(c.get("pcap_ratio") or 0.0), 2),
                int(c.get("upset_wins") or 0),
                int(c.get("upset_opps") or 0),
                round(float(c.get("carry_mult") or 1.0), 3),
                str(entry.get("belt_status") or ""),
                int(entry.get("streak_weeks") or 0),
                int(r2.get("profile_id") or 0),
                str(r2.get("alias") or ""),
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
    clean_report = {k: v for k, v in report.items() if not str(k).startswith("_")}
    atomic_write_text(json_path, json.dumps(clean_report, ensure_ascii=False, indent=2) + "\n")

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
            "forca_tabela_rival_elo",
            "cohdb_rating",
            "carry_mult",
            "carry_2v2_pct",
            "ally_elo_gap",
            "dmg_vs_ally",
            "vitorias_bg",
            "derrotas_bg",
            "win_rate_bg",
            "l30_win_rate_bg",
            "bayes_win_rate_bg",
            "apm",
            "dano_por_min",
            "razao_dano",
            "kd",
            "veh_kd",
            "controle_mapa_ratio",
            "controle_mapa_vs_inimigo",
            "precap_por_min",
            "upset_vitorias",
            "upset_jogos",
            "upset_win_rate",
            "upset_elo_gap_medio",
            "stomp_pct",
            "cinturao_status",
            "semanas_trono",
            "vice_br",
            "vice_score",
        ]
    )
    for entry in clean_report.get("champions") or []:
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
                c.get("avg_enemy_adj_elo", 0.0),
                int(c.get("cohdb_rating") or 0),
                c.get("carry_mult", 1.0),
                c.get("carry_2v2_share", 0.0),
                c.get("avg_ally_elo_gap", 0.0),
                c.get("dmg_vs_ally", 1.0),
                int(c.get("wins") or 0),
                int(c.get("losses") or 0),
                c.get("win_rate", 0.0),
                c.get("l30_win_rate", 0.0),
                c.get("bayes_win_rate", 0.0),
                c.get("apm", 0.0),
                c.get("dmg_pm", 0.0),
                c.get("dmg_ratio", 0.0),
                c.get("kd", 0.0),
                c.get("veh_kd", 0.0),
                c.get("pcap_ratio", 0.0),
                c.get("pcap_vs_enemy", 0.0),
                c.get("precap_pm", 0.0),
                int(c.get("upset_wins") or 0),
                int(c.get("upset_opps") or 0),
                c.get("upset_win_rate", 0.0),
                c.get("upset_avg_gap", 0.0),
                c.get("stomp_win_rate", 0.0),
                sanitize_csv_cell(entry.get("belt_status") or ""),
                int(entry.get("streak_weeks") or 0),
                sanitize_csv_cell(r2.get("alias") or ""),
                r2.get("score", 0.0),
            ]
        )
    atomic_write_text(csv_path, buf.getvalue())
