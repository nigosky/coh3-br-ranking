"""Motor do Boletim Diário dos Veteranos (08:03 BRT) e seleção do 'Jogo do Dia' (Relic API + Open Data + cohdb)."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from arranged_teams import (
    Coh3StatsOpenDataClient,
    _normalize_unix_seconds,
)
from relic_ranking import (
    BASE_URL as RELIC_BASE_URL,
    FACTIONS_ORDER,
    STEAM_LEADERBOARDS,
    TITLE_STEAM,
    UNIFIED_BASE_ELO,
    UNIFIED_MIN_GAMES_PER_QUEUE,
    UNIFIED_MODE_WEIGHTS,
    UNIFIED_MODES_ORDER,
    UNIFIED_SECONDARY_BONUS_RATE,
    RelicRankingClient,
)
from roster import (
    CORE_BR_ROSTER,
    build_manual_other_br_lookup_maps,
    build_roster_lookup_maps,
    extract_steam_id,
    format_bound_display_alias,
)
from security import (
    BRT_TZ,
    RateLimiter,
    atomic_write_text,
    get_thread_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    strip_unsafe_chars,
    validate_https_endpoint,
)

logger = logging.getLogger("coh3_ranking.daily_recap")

ALLOWED_COHDB_HOSTS: frozenset[str] = frozenset({"cohdb.com"})
MAX_COHDB_RESPONSE_BYTES = 512 * 1024  # 512 KB limite rígido

TOP_GLOBAL_MAX_RANK = 20
TOP_GLOBAL_MIN_ELO = 1800
EPIC_LOSS_MIN_DURATION_SEC = 30 * 60
EPIC_LOSS_MIN_DMG_RATIO = 0.80
MIN_MEANINGFUL_DURATION_SEC = 8 * 60
UNIFIED_TOP_N = 5
TOP5_MIN_SP_DELTA = 1.0
TOP5_LOSS_MIN_DURATION_SEC = 20 * 60

# Apenas automatch PvP oficiais (1v1, 2v2, 3v3, 4v4 — ranqueados e unranked automatch; exclui AI e Custom)
AUTOMATCH_TYPE_TO_MODE: dict[int, str] = {
    1: "1v1",
    20: "1v1",
    2: "2v2",
    21: "2v2",
    3: "3v3",
    22: "3v3",
    4: "4v4",
    23: "4v4",
}
MODE_EXPECTED_PLAYERS: dict[str, int] = {
    "1v1": 2,
    "2v2": 4,
    "3v3": 6,
    "4v4": 8,
}
MODES_SORT_ORDER: dict[str, int] = {"1v1": 0, "2v2": 1, "3v3": 2, "4v4": 3}

RACE_ID_TO_FACTION: dict[int, tuple[str, str, str]] = {
    # race_id -> (faction_key, short_label, side)
    129494: ("american", "USF", "allies"),
    197345: ("british", "UKF", "allies"),
    203852: ("british", "UKF", "allies"),
    137123: ("german", "WEHR", "axis"),
    198437: ("dak", "DAK", "axis"),
}

# Mapeamento oficial de códigos internos de mapas CoH3 para nomes legíveis
COH3_MAP_NAMES: dict[str, str] = {
    "twin_beach_2p_mkii": "Twin Beaches",
    "desert_village_2p_mkiii": "Road to Tunis",
    "cliff_crossing_2p": "Taranto Coastline",
    "rails_and_sand_4p": "Campbell's Convoy",
    "rural_town_4p": "Pachino Farmlands",
    "torrente_4p_mkiii": "Torrente",
    "rural_castle_4p": "Aere Perennius",
    "desert_airfield_6p_mkii": "Gazala Landing Ground",
    "industrial_railyard_6p_mkii": "L'Aquila",
    "winter_line_8p_mkii": "Winter Line",
    "mountain_ruins_8p_mkii": "Mignano Gap",
    "mountain_ruins_6p": "Mignano Summit",
    "gardens_2p_mm": "Gardens",
    "pachino_2p": "Pachino Stalemate",
    "rural_town_2p_mkii": "Pachino Farmlands",
    "monte_cavo_8p": "Monte Cavo",
    "benghazi_6p": "Benghazi",
    "sousse_wetlands_8p": "Sousse Wetlands",
    "catania_crossing_6p": "Catania Crossing",
    "day_101_4p": "Day 101",
    "villa_fiore_2p_mkii": "Villa Fiore",
    "semois_2p": "Semois",
    "semois_4p": "Semois",
    "elst_outskirts_4p": "Elst Outskirts",
    "montherme_6p": "Montherme",
    "sangro_river_crossing_6p": "Sangro River Crossing",
    "sousse_stronghold_8p": "Sousse Stronghold",
    "faymonville": "Faymonville",
    "steppe_8p": "Steppes",
    "eindhoven": "Operation Eindhoven",
    "gothic_line_8p": "The Gothic Line",
    "gothic_line_6p": "The Gothic Line",
    "oasis_depot_8p": "Oasis Depot",
    "black_gold_8p": "Black Gold",
    "primosole_4p": "Road to Primosole",
    "primosole_6p": "Road to Primosole",
    "longstop_hill_6p": "Longstop Hill",
    "halfa_8p": "Alam el Halfa",
    "santuario_4p": "Santuario",
    "rapido_river_8p": "Rapido River Crossing",
    "rapido_river_8p_redux": "Rapido River Crossing",
    "angoville_farms_2p": "Angoville Farms",
    "langres_2p": "Langres",
    "langres_winter_2p": "Langres Winter",
    "crossing_in_the_woods_2p": "Crossing in the Woods",
    "crossing_in_the_woods_4p": "Crossing in the Woods",
    "djebel_2p": "Djebel Pass",
    "djebel_4p": "Djebel Pass",
    "mignano_6p": "Mignano Summit",
    "mignano_6p_redux": "Mignano Summit",
    "red_ball_express_8p": "Red Ball Express",
    "gabes_gap_6p": "Gabès Gap",
    "alamein_4p": "El Alamein",
    "lorraine_4p": "Lorraine",
    "tuscan_vineyard_2p": "Tuscan Vineyards",
    "bologna_2p": "Bologna",
    "wadi_darnah_4p": "Wadi Darnah",
    "oliveto_citra_8p": "Oliveto Citra",
    "powderkeg_8p": "Powderkeg",
    "egletons_2p": "Égletons",
    "pisa_centrale_4p": "Pisa Centrale",
    "sedjenane_6p": "Sedjenane Marshes",
    "castello_8p": "Castello della Nebbia",
    "across_the_rhine_6p": "Across the Savio",
    "butcher_and_bolt_2p": "Butcher & Bolt",
    "ancona_railyard_2p": "Ancona Railyard",
    "foggia_airfield_winter_6p": "Foggia Airfield",
    "mammut_8p": "Radarstation Mammut",
}

COHDB_VP_PAIR_RE = re.compile(
    r"(?:victory\s*points?|vp)[^0-9]{0,24}(\d{1,3})\s*(?:[-:/]|vs\.?)\s*(\d{1,3})",
    re.IGNORECASE,
)
COHDB_JSON_VP_RE = re.compile(
    r'"(?:allies_vp|vp_allies|vp0)"\s*:\s*(\d{1,3}).{0,80}"(?:axis_vp|vp_axis|vp1)"\s*:\s*(\d{1,3})',
    re.IGNORECASE | re.DOTALL,
)
COHDB_PCT_PAIR_RE = re.compile(r"(\d{1,2})\s*%\s*[^0-9%]{1,120}(\d{1,2})\s*%")
COHDB_MATCH_PATH_RE = re.compile(r"^/matches/\d{1,12}$")


def get_brt_day_window(target_date_str: str | None = None, now_ts: float | None = None) -> tuple[str, int, int]:
    """Retorna (YYYY-MM-DD, start_ts, end_ts_inclusive) para o dia alvo em BRT (UTC-3).

    Quando `target_date_str` é None, seleciona automaticamente o dia anterior em BRT.
    """
    if target_date_str:
        clean = str(target_date_str).strip()
        target_day = date.fromisoformat(clean)
    else:
        ref_now = datetime.fromtimestamp(now_ts if now_ts is not None else time.time(), tz=BRT_TZ)
        target_day = (ref_now - timedelta(days=1)).date()

    start_dt = datetime(target_day.year, target_day.month, target_day.day, 0, 0, 0, tzinfo=BRT_TZ)
    end_dt = start_dt + timedelta(days=1) - timedelta(seconds=1)
    return target_day.isoformat(), int(start_dt.timestamp()), int(end_dt.timestamp())


def format_map_name(raw_map: Any) -> str:
    code = strip_unsafe_chars(raw_map).strip()
    if not code:
        return "Mapa Desconhecido"
    low = code.lower()
    if low in COH3_MAP_NAMES:
        return COH3_MAP_NAMES[low]
    # Remove sufixos técnicos comuns (_2p, _4p, _6p, _8p, _mkii, _mkiii, _redux)
    cleaned = re.sub(r"_(?:[2468]p|mk[iv]+|redux|mm)$", "", low)
    cleaned = re.sub(r"_(?:[2468]p|mk[iv]+|redux|mm)$", "", cleaned)
    pretty = " ".join(part.capitalize() for part in cleaned.split("_") if part)
    return pretty or code


def format_duration_hms(seconds: int) -> str:
    sec = max(0, int(seconds))
    hours, rem = divmod(sec, 3600)
    mins, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {mins:02d}m"
    return f"{mins}m {secs:02d}s"


def _safe_parse_counters(raw_counters: Any) -> dict[str, int]:
    if isinstance(raw_counters, dict):
        src = raw_counters
    elif isinstance(raw_counters, str) and raw_counters.strip():
        try:
            parsed = json.loads(raw_counters)
            src = parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            src = {}
    else:
        src = {}

    out: dict[str, int] = {}
    for key in (
        "dmgdone",
        "ekills",
        "edeaths",
        "sqkill",
        "sqlost",
        "sqprod",
        "vkill",
        "vlost",
        "vprod",
        "pcap",
        "plost",
        "gt",
        "vp0",
        "vp1",
        "totalcmds",
    ):
        val = src.get(key)
        if val is not None:
            try:
                out[key] = max(0, int(round(float(val))))
            except (TypeError, ValueError):
                pass
    return out


def _build_veterano_resolution(
    custom_players: list[dict[str, Any]] | None = None,
) -> tuple[dict[int, int], dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Constrói mapas de resolução dos Veteranos mantendo contas Smurf separadas (sem merge)."""
    combined: list[dict[str, Any]] = list(CORE_BR_ROSTER)
    seen_pids = {int(c["profile_id"]) for c in combined}
    for cp in custom_players or []:
        pid = int(cp.get("profile_id", 0) or 0)
        if pid > 0 and pid not in seen_pids:
            combined.append(cp)
            seen_pids.add(pid)

    steam_map, pid_map = build_roster_lookup_maps(combined)
    canonical_pid_map: dict[int, int] = {}
    for item in combined:
        pid = int(item.get("profile_id", 0) or 0)
        if pid <= 0:
            continue
        canonical_pid_map[pid] = pid

    return canonical_pid_map, pid_map, steam_map


def is_top_global_entry(top_info: dict[str, Any] | None) -> bool:
    """Retorna True apenas se o jogador for Top 20 global na Relic E possuir ELO > 1800."""
    if not isinstance(top_info, dict):
        return False
    try:
        rank = int(top_info.get("rank", 0) or 0)
        elo = int(round(float(top_info.get("elo", 0) or 0)))
    except (TypeError, ValueError):
        return False
    return 1 <= rank <= TOP_GLOBAL_MAX_RANK and elo > TOP_GLOBAL_MIN_ELO


def fetch_global_top_players(
    client: RelicRankingClient,
    top_count: int = TOP_GLOBAL_MAX_RANK,
    min_elo: int = TOP_GLOBAL_MIN_ELO,
) -> dict[int, dict[str, Any]]:
    """Consulta o Top N global das 16 leaderboards Steam da Relic mantendo apenas Top 20 com ELO > 1800."""
    count = max(1, min(TOP_GLOBAL_MAX_RANK, int(top_count)))
    elo_floor = int(min_elo)
    top_by_pid: dict[int, dict[str, Any]] = {}

    def _scan_lb(mode: str, faction: str, lb_id: int) -> list[dict[str, Any]]:
        url = (
            f"{RELIC_BASE_URL}/getLeaderboard2?title={TITLE_STEAM}"
            f"&leaderboard_id={int(lb_id)}&sortBy=1&count={count}&start=1"
        )
        data = client._get_json(url)
        sg_map = {int(sg["id"]): sg for sg in data.get("statGroups", []) if "id" in sg}
        found: list[dict[str, Any]] = []
        for st in data.get("leaderboardStats", []):
            rank = int(st.get("rank", 0) or 0)
            elo = int(round(float(st.get("rating", 0) or 0)))
            if rank <= 0 or rank > count or elo <= elo_floor:
                continue
            sg = sg_map.get(int(st.get("statgroup_id", -1) or -1))
            if not sg:
                continue
            for mem in sg.get("members", []):
                pid = int(mem.get("profile_id", 0) or 0)
                if pid <= 0:
                    continue
                found.append(
                    {
                        "profile_id": pid,
                        "rank": rank,
                        "mode": mode,
                        "faction": faction,
                        "elo": elo,
                        "alias": strip_unsafe_chars(mem.get("alias") or "")[:64],
                    }
                )
        return found

    tasks = [
        (mode, faction, lb_id)
        for mode, facs in STEAM_LEADERBOARDS.items()
        for faction, lb_id in facs.items()
    ]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(_scan_lb, m, f, lb) for m, f, lb in tasks]
        for fut in concurrent.futures.as_completed(futs):
            try:
                rows = fut.result()
            except Exception as exc:
                logger.warning("Falha parcial ao consultar Top %d global na Relic: %s", count, redact_secrets(exc))
                continue
            for row in rows:
                pid = row["profile_id"]
                cur = top_by_pid.get(pid)
                if cur is None or row["rank"] < cur["rank"] or (row["rank"] == cur["rank"] and row["elo"] > cur["elo"]):
                    top_by_pid[pid] = row

    return top_by_pid


def fetch_veteranos_recent_matches_from_relic(
    client: RelicRankingClient,
    custom_players: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Consulta getRecentMatchHistoryByProfileId para todas as contas Steam dos Veteranos."""
    _, pid_map, _ = _build_veterano_resolution(custom_players)
    pids = sorted(pid_map.keys())
    all_raw_matches: list[dict[str, Any]] = []

    def _fetch_one(pid: int) -> list[dict[str, Any]]:
        url = f"{RELIC_BASE_URL}/getRecentMatchHistoryByProfileId?title={TITLE_STEAM}&profile_id={int(pid)}"
        data = client._get_json(url)
        profiles_by_pid: dict[int, dict[str, Any]] = {}
        for prof in data.get("profiles", []) or []:
            if isinstance(prof, dict) and prof.get("profile_id"):
                profiles_by_pid[int(prof["profile_id"])] = prof

        enriched: list[dict[str, Any]] = []
        for match in data.get("matchHistoryStats", []) or []:
            if not isinstance(match, dict):
                continue
            m_copy = dict(match)
            m_copy["_profiles_by_pid"] = profiles_by_pid
            m_copy["_source"] = "relic"
            enriched.append(m_copy)
        return enriched

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(_fetch_one, pid): pid for pid in pids}
        for fut in concurrent.futures.as_completed(futs):
            pid = futs[fut]
            try:
                all_raw_matches.extend(fut.result())
            except Exception as exc:
                logger.warning("Falha ao buscar histórico recente Relic do profile_id=%d: %s", pid, redact_secrets(exc))

    return all_raw_matches


def fetch_open_data_matches_for_window(
    open_data_client: Coh3StatsOpenDataClient,
    start_ts: int,
    end_ts: int,
    veterano_pids: set[int],
) -> list[dict[str, Any]]:
    """Complementa o dia alvo usando os dumps diários do COH3 Stats Open Data (captura >10 jogos/dia)."""
    start_utc_day = (start_ts // 86400) * 86400
    end_utc_day = (end_ts // 86400) * 86400
    day_ts_list = sorted({start_utc_day, end_utc_day})

    matched: list[dict[str, Any]] = []
    for day_ts in day_ts_list:
        try:
            dump_matches = open_data_client.fetch_day_matches(day_ts)
        except Exception as exc:
            logger.info("Dump Open Data %d indisponível neste momento (complemento opcional): %s", day_ts, redact_secrets(exc))
            continue
        if not dump_matches:
            continue
        for m in dump_matches:
            if not isinstance(m, dict):
                continue
            comp = _normalize_unix_seconds(m.get("completiontime") or m.get("startgametime"))
            if comp < start_ts or comp > end_ts:
                continue
            # Filtro rápido por participação de algum Veterano
            pids = m.get("profile_ids")
            if isinstance(pids, list) and pids:
                if not any(int(p or 0) in veterano_pids for p in pids):
                    continue
            else:
                reps = m.get("matchhistoryreportresults") or []
                if not any(int(r.get("profile_id", 0) or 0) in veterano_pids for r in reps if isinstance(r, dict)):
                    continue
            m_copy = dict(m)
            m_copy["_source"] = "open_data"
            matched.append(m_copy)

    return matched


def normalize_pvp_match(
    raw_match: dict[str, Any],
    canonical_pid_map: dict[int, int],
    pid_map: dict[int, dict[str, Any]],
    steam_map: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Normaliza uma partida PvP automatch Steam da Relic API ou COH3 Stats Open Data."""
    matchtype_id = int(raw_match.get("matchtype_id", -1) or -1)
    mode = AUTOMATCH_TYPE_TO_MODE.get(matchtype_id)
    if not mode:
        return None

    platform = str(raw_match.get("platform") or "").strip().lower()
    if platform and platform != "steam":
        return None

    match_id = int(raw_match.get("id", 0) or 0)
    if match_id <= 0:
        return None

    start_ts = _normalize_unix_seconds(raw_match.get("startgametime"))
    comp_ts = _normalize_unix_seconds(raw_match.get("completiontime") or raw_match.get("startgametime"))
    if comp_ts <= 0:
        return None

    reports = raw_match.get("matchhistoryreportresults")
    expected_n = MODE_EXPECTED_PLAYERS[mode]
    if not isinstance(reports, list) or len(reports) != expected_n:
        return None

    profiles_by_pid: dict[int, dict[str, Any]] = dict(raw_match.get("_profiles_by_pid") or {})
    members_by_pid: dict[int, dict[str, Any]] = {}
    for mem_row in raw_match.get("matchhistorymember", []) or []:
        if isinstance(mem_row, dict) and mem_row.get("profile_id"):
            members_by_pid[int(mem_row["profile_id"])] = mem_row

    players_norm: list[dict[str, Any]] = []
    max_gt = 0
    vp_from_counters: tuple[int, int] | None = None
    has_veterano = False
    manual_steam_map, manual_pid_map = build_manual_other_br_lookup_maps()

    for rep in reports:
        if not isinstance(rep, dict):
            return None
        pid = int(rep.get("profile_id", 0) or 0)
        if pid <= 0:
            return None
        race_id = int(rep.get("race_id", 0) or 0)
        fac_info = RACE_ID_TO_FACTION.get(race_id)
        if not fac_info:
            return None
        faction_key, faction_short, side = fac_info

        prof = rep.get("profile") if isinstance(rep.get("profile"), dict) else profiles_by_pid.get(pid, {})
        member = (
            rep.get("matchhistorymember")
            if isinstance(rep.get("matchhistorymember"), dict)
            else members_by_pid.get(pid, {})
        )

        steam_id = extract_steam_id(prof.get("name"))
        raw_alias = strip_unsafe_chars(prof.get("alias") or "")[:64] or f"Jogador {pid}"
        country = re.sub(r"[^a-z]", "", str(prof.get("country") or "").strip().lower())[:2] or "un"

        canon_pid = canonical_pid_map.get(pid, 0)
        primary_entry = pid_map.get(canon_pid)
        direct_entry = pid_map.get(pid) or (steam_map.get(steam_id) if steam_id else None)
        roster_entry = primary_entry or direct_entry
        is_vet = roster_entry is not None
        if is_vet:
            has_veterano = True
            if not canon_pid:
                canon_pid = int(roster_entry.get("profile_id", pid))
            if primary_entry and primary_entry.get("steam_id"):
                steam_id = str(primary_entry["steam_id"])
            elif not steam_id:
                steam_id = str(roster_entry.get("steam_id") or "")
            merged_known: list[str] = []
            for src_entry in (primary_entry, direct_entry):
                if src_entry and src_entry.get("known_aliases"):
                    merged_known.extend(str(a) for a in src_entry["known_aliases"])
            is_pinned = bool(
                (primary_entry and primary_entry.get("pin_alias"))
                or (direct_entry and direct_entry.get("pin_alias"))
            )
            if roster_entry.get("country"):
                country = str(roster_entry["country"]).strip().lower() or country
            disp_alias = format_bound_display_alias(
                raw_alias,
                str(roster_entry.get("original_alias") or raw_alias),
                merged_known or None,
                pin_alias=is_pinned,
            )
        else:
            canon_pid = pid
            m_entry = (manual_steam_map.get(steam_id) if steam_id else None) or manual_pid_map.get(pid)
            if m_entry:
                if m_entry.get("country"):
                    country = str(m_entry["country"]).strip().lower() or country
                disp_alias = format_bound_display_alias(
                    raw_alias,
                    str(m_entry.get("original_alias") or raw_alias),
                    m_entry.get("known_aliases"),
                    pin_alias=bool(m_entry.get("pin_alias")),
                )
            else:
                disp_alias = raw_alias

        resulttype = int(rep.get("resulttype", 0) or 0)
        outcome_val = member.get("outcome")
        if outcome_val is not None:
            won = int(outcome_val) == 1
        else:
            won = resulttype == 1

        old_rating = int(round(float(member.get("oldrating", 0) or 0)))
        new_rating = int(round(float(member.get("newrating", 0) or 0)))
        elo_delta = (new_rating - old_rating) if (old_rating > 0 and new_rating > 0) else 0

        counters = _safe_parse_counters(rep.get("counters"))
        if counters.get("gt", 0) > max_gt:
            max_gt = counters["gt"]
        if vp_from_counters is None and (counters.get("vp0", 0) > 0 or counters.get("vp1", 0) > 0):
            vp_from_counters = (counters.get("vp0", 0), counters.get("vp1", 0))

        players_norm.append(
            {
                "profile_id": pid,
                "canonical_pid": canon_pid,
                "steam_id": steam_id,
                "alias": disp_alias,
                "country": country,
                "team_id": int(rep.get("teamid", 0) or 0),
                "faction": faction_key,
                "faction_short": faction_short,
                "side": side,
                "won": won,
                "old_elo": old_rating,
                "new_elo": new_rating,
                "elo_delta": elo_delta,
                "is_veterano": is_vet,
                "dmgdone": counters.get("dmgdone", 0),
                "ekills": counters.get("ekills", 0),
                "edeaths": counters.get("edeaths", 0),
                "sqkill": counters.get("sqkill", 0),
                "sqlost": counters.get("sqlost", 0),
                "vkill": counters.get("vkill", 0),
                "vlost": counters.get("vlost", 0),
                "pcap": counters.get("pcap", 0),
                "has_counters": bool(counters),
            }
        )

    if not has_veterano:
        return None

    allies = [p for p in players_norm if p["side"] == "allies"]
    axis = [p for p in players_norm if p["side"] == "axis"]
    half = expected_n // 2
    if len(allies) != half or len(axis) != half:
        return None

    duration_sec = max_gt if max_gt > 0 else (max(0, comp_ts - start_ts) if start_ts > 0 else 0)
    winner_side = "allies" if any(p["won"] for p in allies) else ("axis" if any(p["won"] for p in axis) else "")
    if not winner_side:
        return None

    # Mapeia vp0/vp1 (indexados por team_id 0 e 1 na Relic) para allies/axis
    vp_allies: int | None = None
    vp_axis: int | None = None
    vp_source = ""
    if vp_from_counters is not None:
        allies_tid = allies[0]["team_id"]
        if allies_tid == 0:
            vp_allies, vp_axis = vp_from_counters[0], vp_from_counters[1]
        else:
            vp_allies, vp_axis = vp_from_counters[1], vp_from_counters[0]
        vp_source = "relic_counters"

    matchurls = raw_match.get("matchurls")
    has_replay_urls = isinstance(matchurls, list) and len(matchurls) > 0

    return {
        "match_id": match_id,
        "mode": mode,
        "matchtype_id": matchtype_id,
        "map_code": strip_unsafe_chars(raw_match.get("mapname") or "")[:80],
        "map_name": format_map_name(raw_match.get("mapname") or ""),
        "start_ts": start_ts,
        "completion_ts": comp_ts,
        "duration_sec": duration_sec,
        "winner_side": winner_side,
        "allies_team": allies,
        "axis_team": axis,
        "vp_allies": vp_allies,
        "vp_axis": vp_axis,
        "vp_source": vp_source,
        "cohdb_share_allies": None,
        "cohdb_share_axis": None,
        "has_replay_urls": has_replay_urls,
        "coh3stats_url": f"https://coh3stats.com/matches/{match_id}",
        "cohdb_url": f"https://cohdb.com/matches/{match_id}",
        "cohdb_verified": False,
        "data_richness": sum(1 for p in players_norm if p["has_counters"]) + sum(1 for p in players_norm if p["old_elo"] > 0),
    }


def merge_and_filter_day_matches(
    raw_matches: list[dict[str, Any]],
    start_ts: int,
    end_ts: int,
    custom_players: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Normaliza, filtra pela janela BRT exata e deduplica partidas priorizando registros mais completos."""
    canonical_pid_map, pid_map, steam_map = _build_veterano_resolution(custom_players)
    by_match_id: dict[int, dict[str, Any]] = {}

    for raw in raw_matches:
        norm = normalize_pvp_match(raw, canonical_pid_map, pid_map, steam_map)
        if norm is None:
            continue
        comp = norm["completion_ts"]
        if comp < start_ts or comp > end_ts:
            continue
        mid = norm["match_id"]
        prev = by_match_id.get(mid)
        if prev is None or norm["data_richness"] > prev["data_richness"]:
            by_match_id[mid] = norm

    return sorted(by_match_id.values(), key=lambda m: (m["completion_ts"], m["match_id"]))


class CohdbReplayClient:
    """Cliente HTTPS endurecido para checar página de replay e eventual placar de VP no cohdb.com."""

    def __init__(self, timeout_sec: float = 6.0, min_interval_sec: float = 0.25) -> None:
        self._rate_limiter = RateLimiter(min_interval_sec=min_interval_sec, floor_sec=0.10)
        self.timeout_sec = max(2.0, float(timeout_sec))

    @staticmethod
    def parse_cohdb_match_html(html_text: str) -> dict[str, Any]:
        """Extrai indícios de contagem de VP / equilíbrio de partida da página pública do cohdb."""
        info: dict[str, Any] = {
            "vp_pair": None,
            "share_pair": None,
            "has_download": False,
        }
        if not html_text:
            return info

        low = html_text.lower()
        info["has_download"] = ("download" in low and ".rec" in low) or ("/replays/" in low) or ("replay" in low)

        m_json = COHDB_JSON_VP_RE.search(html_text)
        if m_json:
            v0, v1 = int(m_json.group(1)), int(m_json.group(2))
            if 0 <= v0 <= 500 and 0 <= v1 <= 500 and (v0 > 0 or v1 > 0):
                info["vp_pair"] = (v0, v1)
                return info

        m_vp = COHDB_VP_PAIR_RE.search(html_text)
        if m_vp:
            v0, v1 = int(m_vp.group(1)), int(m_vp.group(2))
            if 0 <= v0 <= 500 and 0 <= v1 <= 500 and (v0 > 0 or v1 > 0) and (v0 == 0 or v1 == 0 or min(v0, v1) <= 150):
                info["vp_pair"] = (v0, v1)
                return info

        # Percentual de controle / probabilidade exibido nas listagens do cohdb (ex.: 49% / 51%)
        for m_pct in COHDB_PCT_PAIR_RE.finditer(html_text):
            p1, p2 = int(m_pct.group(1)), int(m_pct.group(2))
            if 5 <= p1 <= 95 and 5 <= p2 <= 95 and (p1 + p2) == 100:
                info["share_pair"] = (p1, p2)
                break

        return info

    def inspect_match(self, match_id: int) -> dict[str, Any] | None:
        mid = int(match_id)
        if mid <= 0:
            return None
        url = f"https://cohdb.com/matches/{mid}"
        parsed_url = validate_https_endpoint(
            url,
            ALLOWED_COHDB_HOSTS,
            path_prefix="/matches/",
            path_regex=COHDB_MATCH_PATH_RE,
        )
        if parsed_url.query:
            raise ValueError("Parâmetros de query bloqueados na consulta de replay do cohdb.")
        opener = get_thread_no_redirect_https_opener()
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "CoH3-BR-Ranking-Unificado/1.0",
                "Accept": "text/html,application/json",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            self._rate_limiter.wait()
            with opener.open(req, timeout=self.timeout_sec) as resp:
                if resp.status != 200:
                    return None
                raw = read_bounded_http_body(resp, MAX_COHDB_RESPONSE_BYTES).decode("utf-8", errors="ignore")
                parsed = self.parse_cohdb_match_html(raw)
                parsed["url"] = url
                parsed["verified"] = True
                return parsed
        except Exception as exc:
            logger.debug("cohdb indisponível para match %d: %s", mid, redact_secrets(exc))
            return None


def _compute_team_combat_totals(team: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "dmgdone": sum(int(p.get("dmgdone", 0)) for p in team),
        "vkill": sum(int(p.get("vkill", 0)) for p in team),
        "sqkill": sum(int(p.get("sqkill", 0)) for p in team),
        "ekills": sum(int(p.get("ekills", 0)) for p in team),
        "pcap": sum(int(p.get("pcap", 0)) for p in team),
    }


def build_unified_ranking_context(
    unified_rows: list[dict[str, Any]] | None,
    catalog_players: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Constrói estado base do Ranking Unificado BR (Skill Points + filas calibradas >= 10 jogos)."""
    if not unified_rows:
        return {"players": {}}

    catalog_queues_by_pid: dict[int, dict[tuple[str, str], dict[str, int]]] = {}
    for cp in catalog_players or []:
        if not isinstance(cp, dict):
            continue
        canon_pid = int(cp.get("profile_id", 0) or 0)
        if canon_pid <= 0:
            continue
        qmap = catalog_queues_by_pid.setdefault(canon_pid, {})
        for bucket_key in ("inactive_modes", "modes"):
            bucket = cp.get(bucket_key)
            if not isinstance(bucket, dict):
                continue
            for slot in bucket.values():
                if not isinstance(slot, dict):
                    continue
                m = str(slot.get("mode") or "")
                fac = str(slot.get("faction") or "")
                if m not in UNIFIED_MODE_WEIGHTS or fac not in FACTIONS_ORDER:
                    continue
                elo_val = int(round(float(slot.get("elo", 0) or 0)))
                games_val = int(slot.get("wins", 0) or 0) + int(slot.get("losses", 0) or 0)
                if elo_val <= 0:
                    continue
                prev = qmap.get((m, fac))
                if prev is None or (games_val >= UNIFIED_MIN_GAMES_PER_QUEUE and (prev["games"] < UNIFIED_MIN_GAMES_PER_QUEUE or elo_val > prev["elo"])):
                    qmap[(m, fac)] = {"elo": elo_val, "games": games_val}

    players_state: dict[int, dict[str, Any]] = {}
    for row in unified_rows:
        if not isinstance(row, dict):
            continue
        pid = int(row.get("profile_id", 0) or 0)
        if pid <= 0:
            continue
        queues: dict[tuple[str, str], dict[str, int]] = {
            k: dict(v) for k, v in catalog_queues_by_pid.get(pid, {}).items()
        }
        # Garante consistência com os máximos por modo registrados no ranking_unificado_br.json
        mode_max_elos = row.get("mode_max_elos") or {}
        mode_best_factions = row.get("mode_best_factions") or {}
        for mode in UNIFIED_MODES_ORDER:
            m_elo = int(round(float(mode_max_elos.get(mode, 0) or 0)))
            if m_elo <= 0:
                continue
            best_fac = str(mode_best_factions.get(mode) or "american")
            if best_fac not in FACTIONS_ORDER:
                best_fac = "american"
            cur = queues.get((mode, best_fac))
            if cur is None or cur["games"] < UNIFIED_MIN_GAMES_PER_QUEUE or cur["elo"] < m_elo:
                queues[(mode, best_fac)] = {
                    "elo": m_elo,
                    "games": max(UNIFIED_MIN_GAMES_PER_QUEUE, cur["games"] if cur else UNIFIED_MIN_GAMES_PER_QUEUE),
                }

        mode_carry_offsets: dict[str, float] = {}
        mode_effective_elos = row.get("mode_effective_elos") or {}
        if isinstance(mode_effective_elos, dict):
            for mode in UNIFIED_MODES_ORDER:
                if mode == "1v1":
                    continue
                raw_m = float(mode_max_elos.get(mode, 0) or 0)
                eff_m = float(mode_effective_elos.get(mode, 0) or 0)
                if raw_m > 0.0 and 0.0 < eff_m < raw_m:
                    mode_carry_offsets[mode] = round(eff_m - raw_m, 2)

        players_state[pid] = {
            "profile_id": pid,
            "alias": strip_unsafe_chars(row.get("alias") or f"Jogador {pid}")[:64],
            "inactive_fallback": bool(row.get("inactive_fallback", False)),
            "wins": int(row.get("wins", 0) or 0),
            "baseline_rank": int(row.get("br_rank", 0) or 0),
            "baseline_sp": float(row.get("skill_points", 0.0) or 0.0),
            "mode_carry_offsets": mode_carry_offsets,
            "queues": queues,
        }

    return {"players": players_state}


def load_unified_ranking_context(
    unified_json_path: Path | None = None,
    catalog_json_path: Path | None = None,
) -> dict[str, Any]:
    """Carrega ranking_unificado_br.json e jogadores_br_steam.json para detectar impacto no Top 5 Unificado."""
    if unified_json_path is None or not unified_json_path.exists() or unified_json_path.is_symlink():
        return {"players": {}}
    try:
        u_raw = json.loads(unified_json_path.read_text(encoding="utf-8"))
        unified_rows = u_raw.get("rankings") if isinstance(u_raw, dict) else None
        if not isinstance(unified_rows, list):
            return {"players": {}}
    except Exception as exc:
        logger.warning("Não foi possível carregar %s: %s", unified_json_path.name, redact_secrets(exc))
        return {"players": {}}

    catalog_players: list[dict[str, Any]] | None = None
    if catalog_json_path is not None and catalog_json_path.exists() and not catalog_json_path.is_symlink():
        try:
            c_raw = json.loads(catalog_json_path.read_text(encoding="utf-8"))
            if isinstance(c_raw, dict) and isinstance(c_raw.get("players"), list):
                catalog_players = c_raw["players"]
        except Exception as exc:
            logger.debug("Catálogo opcional %s indisponível para contexto Top 5: %s", catalog_json_path.name, redact_secrets(exc))

    return build_unified_ranking_context(unified_rows, catalog_players)


def _compute_player_unified_metrics(
    queues: dict[tuple[str, str], dict[str, int]],
    mode_carry_offsets: dict[str, float] | None = None,
) -> tuple[float, int, int]:
    """Calcula (skill_points, valid_modes_count, max_elo) respeitando a deflação Anti-Carry por modo de equipe."""
    carry_offsets = mode_carry_offsets or {}
    mode_max_elos: dict[str, int] = {}
    mode_weighted: dict[str, float] = {}
    for mode in UNIFIED_MODES_ORDER:
        best_elo = 0
        for fac in FACTIONS_ORDER:
            st = queues.get((mode, fac))
            if not st:
                continue
            if int(st.get("games", 0)) < UNIFIED_MIN_GAMES_PER_QUEUE:
                continue
            elo_val = int(st.get("elo", 0))
            if elo_val > best_elo:
                best_elo = elo_val
        if best_elo > 0:
            w_m = float(UNIFIED_MODE_WEIGHTS[mode])
            eff_elo = float(best_elo)
            if mode != "1v1":
                offset = min(0.0, float(carry_offsets.get(mode, 0.0) or 0.0))
                if offset < 0.0:
                    eff_elo = float(best_elo) + offset
                    if best_elo >= UNIFIED_BASE_ELO:
                        eff_elo = max(float(UNIFIED_BASE_ELO), eff_elo)
            p_m = max(0.0, float(UNIFIED_BASE_ELO) + (eff_elo - float(UNIFIED_BASE_ELO)) * w_m)
            mode_max_elos[mode] = best_elo
            mode_weighted[mode] = round(p_m, 2)

    if not mode_max_elos:
        return 0.0, 0, 0

    anchor_mode = max(
        mode_weighted.keys(),
        key=lambda m: (mode_weighted[m], float(UNIFIED_MODE_WEIGHTS[m]), mode_max_elos[m]),
    )
    anchor_score = mode_weighted[anchor_mode]
    bonus = float(UNIFIED_SECONDARY_BONUS_RATE) * sum(
        max(0.0, score - float(UNIFIED_BASE_ELO))
        for m, score in mode_weighted.items()
        if m != anchor_mode
    )
    return round(anchor_score + bonus, 1), len(mode_max_elos), max(mode_max_elos.values())


def _rank_unified_standings(players_state: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Ordena todos os Veteranos pela classificação oficial do Ranking Unificado BR (com Anti-Carry)."""
    ranked_rows: list[dict[str, Any]] = []
    for pid, pstate in players_state.items():
        sp, valid_modes, max_elo = _compute_player_unified_metrics(
            pstate["queues"],
            mode_carry_offsets=pstate.get("mode_carry_offsets"),
        )
        if sp <= 0.0 or valid_modes <= 0:
            continue
        ranked_rows.append(
            {
                "profile_id": pid,
                "alias": pstate["alias"],
                "inactive_fallback": bool(pstate.get("inactive_fallback", False)),
                "skill_points": sp,
                "valid_modes_count": valid_modes,
                "max_elo": max_elo,
                "wins": int(pstate.get("wins", 0)),
            }
        )

    ranked_rows.sort(
        key=lambda x: (
            1 if x["inactive_fallback"] else 0,
            -float(x["skill_points"]),
            -int(x["valid_modes_count"]),
            -int(x["max_elo"]),
            -int(x["wins"]),
            str(x["alias"]).lower(),
        )
    )
    standings: dict[int, dict[str, Any]] = {}
    for pos, item in enumerate(ranked_rows, start=1):
        standings[int(item["profile_id"])] = {
            "rank": pos,
            "skill_points": float(item["skill_points"]),
            "alias": str(item["alias"]),
        }
    return standings


def _format_top5_impact_labels(
    moves: list[dict[str, Any]],
    sp_changes: list[dict[str, Any]],
    top_n: int = UNIFIED_TOP_N,
) -> tuple[str, str]:
    """Gera selo curto (badge) e resumo legível para o impacto da partida no Top 5 Unificado."""
    if moves:
        def _move_prio(mv: dict[str, Any]) -> tuple[int, int, int, float]:
            if mv.get("took_rank1"):
                cat = 4
            elif mv.get("entered_top5"):
                cat = 3
            elif int(mv["rank_after"]) < int(mv["rank_before"]):
                cat = 2
            else:
                cat = 1
            return (cat, 1 if mv.get("won") else 0, -min(int(mv["rank_before"]), int(mv["rank_after"])), abs(float(mv["sp_delta"])))

        best = max(moves, key=_move_prio)
        alias = str(best["alias"])
        rb = int(best["rank_before"])
        ra = int(best["rank_after"])
        sp_d = float(best["sp_delta"])
        if best.get("took_rank1"):
            return "NOVO #1 UNIFICADO", f"{alias} assumiu #1 BR (#{rb} → #1 • {sp_d:+.1f} SP)"
        if best.get("entered_top5"):
            return f"ENTROU NO TOP {top_n} (#{ra})", f"{alias} entrou no Top {top_n} (#{rb} → #{ra} • {sp_d:+.1f} SP)"
        if ra < rb:
            return f"TOP {top_n} BR (#{rb}→#{ra})", f"{alias} subiu no Top {top_n} (#{rb} → #{ra} • {sp_d:+.1f} SP)"
        return f"MEXEU NO TOP {top_n} (#{rb}→#{ra})", f"{alias} (#{rb} → #{ra} • {sp_d:+.1f} SP)"

    if sp_changes:
        best_sp = max(sp_changes, key=lambda x: (1 if x.get("won") else 0, abs(float(x["sp_delta"])), -int(x["rank_after"])))
        alias = str(best_sp["alias"])
        ra = int(best_sp["rank_after"])
        sp_d = float(best_sp["sp_delta"])
        sp_lbl = f"{sp_d:+.1f}" if abs(sp_d) < 10.0 else f"{int(round(sp_d)):+d}"
        return f"TOP {top_n} BR (#{ra} {sp_lbl} SP)", f"{alias} (#{ra} BR • {sp_d:+.1f} SP)"

    return "", ""


def annotate_unified_top5_impacts(
    day_matches: list[dict[str, Any]],
    unified_context: dict[str, Any] | None,
    top_n: int = UNIFIED_TOP_N,
) -> dict[int, dict[str, Any]]:
    """Simula cronologicamente cada partida do dia para detectar impacto real no Top 5 do Ranking Unificado."""
    base_players = (unified_context or {}).get("players")
    if not day_matches or not isinstance(base_players, dict) or not base_players:
        return {}

    chronological = sorted(day_matches, key=lambda m: (int(m.get("completion_ts", 0)), int(m.get("match_id", 0))))

    # Copia profunda leve do estado inicial e retrocede filas jogadas no dia para o old_elo da 1ª partida
    state: dict[int, dict[str, Any]] = {}
    for pid, pinfo in base_players.items():
        state[int(pid)] = {
            "profile_id": int(pid),
            "alias": str(pinfo["alias"]),
            "inactive_fallback": bool(pinfo.get("inactive_fallback", False)),
            "wins": int(pinfo.get("wins", 0)),
            "mode_carry_offsets": dict(pinfo.get("mode_carry_offsets") or {}),
            "queues": {k: dict(v) for k, v in (pinfo.get("queues") or {}).items()},
        }

    rewound_keys: set[tuple[int, str, str]] = set()
    for m in chronological:
        mode = str(m.get("mode") or "")
        for p in list(m.get("allies_team") or []) + list(m.get("axis_team") or []):
            if not p.get("is_veterano"):
                continue
            cpid = int(p.get("canonical_pid", p.get("profile_id", 0)) or 0)
            fac = str(p.get("faction") or "")
            old_elo = int(p.get("old_elo", 0) or 0)
            if cpid not in state or old_elo <= 0:
                continue
            rkey = (cpid, mode, fac)
            if rkey in rewound_keys:
                continue
            q = state[cpid]["queues"].get((mode, fac))
            if q is not None and int(q.get("games", 0)) >= UNIFIED_MIN_GAMES_PER_QUEUE:
                q["elo"] = old_elo
                rewound_keys.add(rkey)

    impacts: dict[int, dict[str, Any]] = {}
    for m in chronological:
        mid = int(m.get("match_id", 0) or 0)
        mode = str(m.get("mode") or "")
        vets_in_match = [
            p
            for p in list(m.get("allies_team") or []) + list(m.get("axis_team") or [])
            if p.get("is_veterano") and int(p.get("canonical_pid", p.get("profile_id", 0)) or 0) in state
        ]
        if not vets_in_match:
            continue

        # Sincroniza old_elo antes da partida para blindar contra lacunas entre partidas
        updates: list[tuple[int, str, int, bool]] = []
        for p in vets_in_match:
            cpid = int(p.get("canonical_pid", p.get("profile_id", 0)) or 0)
            fac = str(p.get("faction") or "")
            old_elo = int(p.get("old_elo", 0) or 0)
            new_elo = int(p.get("new_elo", 0) or 0)
            q = state[cpid]["queues"].get((mode, fac))
            if q is not None and int(q.get("games", 0)) >= UNIFIED_MIN_GAMES_PER_QUEUE and old_elo > 0 and new_elo > 0:
                q["elo"] = old_elo
                updates.append((cpid, fac, new_elo, bool(p.get("won"))))

        if not updates:
            continue

        before = _rank_unified_standings(state)
        for cpid, fac, new_elo, _won in updates:
            state[cpid]["queues"][(mode, fac)]["elo"] = new_elo
            state[cpid]["inactive_fallback"] = False
        after = _rank_unified_standings(state)

        vet_won_by_pid = {cpid: won for cpid, _fac, _ne, won in updates}
        moves: list[dict[str, Any]] = []
        sp_changes: list[dict[str, Any]] = []

        for cpid in sorted(vet_won_by_pid.keys()):
            b_info = before.get(cpid)
            a_info = after.get(cpid)
            if not b_info or not a_info:
                continue
            rb, ra = int(b_info["rank"]), int(a_info["rank"])
            sb, sa = float(b_info["skill_points"]), float(a_info["skill_points"])
            sp_delta = round(sa - sb, 1)
            if min(rb, ra) > top_n:
                continue

            entry = {
                "profile_id": cpid,
                "alias": a_info["alias"],
                "rank_before": rb,
                "rank_after": ra,
                "sp_before": sb,
                "sp_after": sa,
                "sp_delta": sp_delta,
                "won": vet_won_by_pid[cpid],
                "entered_top5": rb > top_n and ra <= top_n,
                "left_top5": rb <= top_n and ra > top_n,
                "took_rank1": rb > 1 and ra == 1,
            }
            if rb != ra:
                moves.append(entry)
            if abs(sp_delta) >= TOP5_MIN_SP_DELTA:
                sp_changes.append(entry)

        if moves or sp_changes:
            badge_lbl, summary_lbl = _format_top5_impact_labels(moves, sp_changes, top_n=top_n)
            max_abs_sp = max((abs(float(x["sp_delta"])) for x in (moves + sp_changes)), default=0.0)
            impacts[mid] = {
                "affects_top5_rank": bool(moves),
                "affects_top5_sp": bool(sp_changes),
                "affects_unified_top5": True,
                "max_top5_abs_sp_delta": round(max_abs_sp, 1),
                "top5_moves": moves,
                "top5_sp_changes": sp_changes,
                "badge": badge_lbl,
                "summary": summary_lbl,
            }

    return impacts


def _match_of_the_day_sort_key(match: dict[str, Any]) -> tuple[int, int, int, int]:
    """Ordena candidatas ao Jogo do Dia por faixa de prioridade (tier) e depois Índice Épico."""
    return (
        -int(match.get("selection_tier", 0)),
        -int(match.get("epicness_score", 0)),
        -int(match.get("duration_sec", 0)),
        -int(match.get("avg_match_elo", 0)),
    )


def score_match_of_the_day(
    match: dict[str, Any],
    top_global_by_pid: dict[int, dict[str, Any]] | None = None,
    top5_impact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pontua e classifica uma partida para seleção transparente do 'Jogo do Dia'.

    Hierarquia de prioridade (`selection_tier`, maior sempre vence):
      4: Vitória de Veterano (>= 8 min) em modo prioritário (`1v1`, `2v2`, `3v3`/`4v4` time fechado
         100% Veteranos, qualquer modo enfrentando outro brasileiro do outro lado) OU vitória (>= 8 min)
         que altere posição direta no Top 5 do Ranking Unificado dos Veteranos.
      3: Derrota épica qualificada em modo prioritário: disputada + longa (>= 30 min) + contra
         Top Global (Top 20 Relic E ELO > 1800) OU derrota disputada (>= 20 min) em modo prioritário
         que altere posição direta no Top 5 do Ranking Unificado.
      2: Vitória em `3v3`/`4v4` isolado (sem time fechado 100% Veteranos, sem BR adversário e sem
         troca no Top 5) OU vitória relâmpago (< 8 min) em modo prioritário.
      1: Derrota comum em modo prioritário OU derrota épica qualificada / vitória relâmpago em `3v3`/`4v4` isolado.
      0: Demais derrotas (derrotas comuns em `3v3`/`4v4` isolado ou drops < 8 min).
    """
    top_map = top_global_by_pid or {}
    impact = top5_impact if top5_impact is not None else match.get("unified_top5_impact")
    impact_dict = impact if isinstance(impact, dict) else {}
    affects_top5_rank = bool(impact_dict.get("affects_top5_rank"))
    affects_top5_sp = bool(impact_dict.get("affects_top5_sp"))
    affects_unified_top5 = affects_top5_rank or affects_top5_sp
    max_top5_abs_sp = float(impact_dict.get("max_top5_abs_sp_delta", 0.0) or 0.0)
    top5_badge = str(impact_dict.get("badge") or "")
    top5_summary = str(impact_dict.get("summary") or "")

    allies = [dict(p) for p in match["allies_team"]]
    axis = [dict(p) for p in match["axis_team"]]
    winner_side = match["winner_side"]
    mode = str(match.get("mode") or "")

    # Enriquecer cada jogador apenas quando for Top 20 global na Relic E ELO > 1800
    for pl in allies + axis:
        top_info = top_map.get(int(pl["profile_id"]))
        if is_top_global_entry(top_info):
            pl["global_rank"] = int(top_info["rank"])
            pl["global_rank_mode"] = str(top_info.get("mode") or "")
            pl["global_rank_elo"] = int(round(float(top_info.get("elo", 0) or 0)))
            pl["is_top_global"] = True
        else:
            pl["global_rank"] = None
            pl["global_rank_mode"] = ""
            pl["global_rank_elo"] = 0
            pl["is_top_global"] = False

    vet_allies = [p for p in allies if p["is_veterano"]]
    vet_axis = [p for p in axis if p["is_veterano"]]
    is_br_derby = bool(vet_allies and vet_axis)

    # Identifica Veteranos, adversários e se houve vitória de Veterano
    if is_br_derby:
        opponents = allies + axis
        vet_won = True
        vet_players = vet_allies + vet_axis
        opp_players = [p for p in allies + axis if not p["is_veterano"]]
    elif vet_allies:
        vet_players = vet_allies
        opp_players = axis
        opponents = axis
        vet_won = winner_side == "allies"
    else:
        vet_players = vet_axis
        opp_players = allies
        opponents = allies
        vet_won = winner_side == "axis"

    has_br_opponent = is_br_derby or any(
        str(op.get("country") or "").strip().lower() == "br" or bool(op.get("is_veterano"))
        for op in opp_players
    )
    is_full_vet_team_game = (mode in ("3v3", "4v4")) and (
        (len(allies) >= 3 and all(p["is_veterano"] for p in allies))
        or (len(axis) >= 3 and all(p["is_veterano"] for p in axis))
    )
    is_isolated_team_game = (mode in ("3v3", "4v4")) and (not is_full_vet_team_game) and (not has_br_opponent)
    is_priority_mode = not is_isolated_team_game

    # 1) Prestígio / Top Global Relic (Top 20 E ELO > 1800) (0..35 pts)
    best_opp_rank: int | None = None
    best_opp_player: dict[str, Any] | None = None
    for op in opponents:
        if not op.get("is_top_global"):
            continue
        rk = op.get("global_rank")
        if rk is not None and (best_opp_rank is None or rk < best_opp_rank):
            best_opp_rank = rk
            best_opp_player = op
    has_top_global_opp = best_opp_rank is not None

    elos_valid = [int(p["old_elo"]) for p in (allies + axis) if int(p.get("old_elo", 0)) > 0]
    avg_match_elo = int(round(sum(elos_valid) / len(elos_valid))) if elos_valid else 0

    prestige_pts = 0.0
    top_badge = ""
    if has_top_global_opp:
        prestige_pts = 35.0 if vet_won else 25.0
        top_badge = f"VENCEU TOP #{best_opp_rank} GLOBAL" if vet_won else f"VS TOP #{best_opp_rank} GLOBAL"

    if avg_match_elo >= 1200:
        elo_bonus = min(16.0, ((avg_match_elo - 1200) / 500.0) * 16.0)
        prestige_pts = min(35.0, prestige_pts + elo_bonus)

    # 2) Zebra / Upset de ELO (0..18 pts) — mede déficit real de ELO entre times para evitar falsos upsets de calibração
    upset_pts = 0.0
    upset_badge = ""
    max_vet_gain = max((int(p.get("elo_delta", 0)) for p in vet_players), default=0)
    my_team = allies if (vet_allies and not is_br_derby) else (axis if (vet_axis and not is_br_derby) else vet_players)
    my_team_elos = [int(p["old_elo"]) for p in my_team if int(p.get("old_elo", 0)) > 0]
    opp_elos = [int(p["old_elo"]) for p in opp_players if int(p.get("old_elo", 0)) > 0]
    has_elo_comparison = bool(my_team_elos and opp_elos)
    elo_deficit = (sum(opp_elos) / len(opp_elos) - sum(my_team_elos) / len(my_team_elos)) if has_elo_comparison else 0.0

    if vet_won and not is_br_derby:
        if has_elo_comparison:
            if elo_deficit >= 80:
                upset_pts = min(18.0, (elo_deficit - 50) / 8.0)
        elif max_vet_gain >= 18:
            upset_pts = min(18.0, (max_vet_gain - 12) * 1.4)
        if upset_pts >= 8.0:
            upset_badge = f"UPSET +{max_vet_gain} ELO" if max_vet_gain > 0 else f"UPSET +{int(round(elo_deficit))} DIF"

    # 3) Drama: Duração longa (0..14 pts) + VP apertado / equilíbrio de combate (0..16 pts)
    dur_sec = int(match.get("duration_sec", 0) or 0)
    dur_min = dur_sec / 60.0
    if dur_sec > 0 and dur_sec < MIN_MEANINGFUL_DURATION_SEC:
        duration_pts = -12.0  # Penaliza drop precoce / stomp < 8 min
    elif dur_min >= 20.0:
        duration_pts = min(14.0, ((dur_min - 18.0) / 32.0) * 14.0)
    elif dur_min >= 12.0:
        duration_pts = ((dur_min - 12.0) / 8.0) * 3.0
    else:
        duration_pts = 0.0

    marathon_badge = f"MARATONA {format_duration_hms(dur_sec)}" if dur_min >= 35.0 else ""

    allies_tot = _compute_team_combat_totals(allies)
    axis_tot = _compute_team_combat_totals(axis)

    closeness_pts = 0.0
    closeness_badge = ""
    is_contested = False
    vp_a = match.get("vp_allies")
    vp_x = match.get("vp_axis")
    if vp_a is not None and vp_x is not None and (vp_a > 0 or vp_x > 0):
        vp_win = vp_a if winner_side == "allies" else vp_x
        vp_lose = vp_x if winner_side == "allies" else vp_a
        vp_margin = abs(vp_win - vp_lose)
        if vp_margin <= 50 or (vp_lose == 0 and vp_win <= 50):
            closeness_pts = 16.0
            closeness_badge = f"NO SUFOCO ({vp_a} x {vp_x} VP)"
            is_contested = True
        elif vp_margin <= 120 or (vp_lose == 0 and vp_win <= 120):
            closeness_pts = 12.0
            closeness_badge = f"VP APERTADO ({vp_a} x {vp_x} VP)"
            is_contested = True
        elif vp_margin <= 220:
            closeness_pts = 7.0
            closeness_badge = f"PLACAR {vp_a} x {vp_x} VP"
    elif match.get("cohdb_share_allies") is not None and match.get("cohdb_share_axis") is not None:
        diff_pct = abs(int(match["cohdb_share_allies"]) - int(match["cohdb_share_axis"]))
        if diff_pct <= 12:
            closeness_pts = 14.0
            closeness_badge = f"EQUILÍBRIO {match['cohdb_share_allies']}% x {match['cohdb_share_axis']}%"
            is_contested = True
        elif diff_pct <= 20:
            closeness_pts = 10.0
            closeness_badge = f"EQUILÍBRIO {match['cohdb_share_allies']}% x {match['cohdb_share_axis']}%"
            is_contested = True
        elif diff_pct <= 24:
            closeness_pts = 8.0
    else:
        # Fallback confiável via counters da Relic: equilíbrio de dano escalado pelo tamanho do time
        team_size = max(1, len(allies))
        min_closeness_dmg = team_size * 3_000
        min_contested_dmg = team_size * 5_000
        max_dmg = max(allies_tot["dmgdone"], axis_tot["dmgdone"])
        min_dmg = min(allies_tot["dmgdone"], axis_tot["dmgdone"])
        if max_dmg >= min_closeness_dmg:
            dmg_ratio = min_dmg / max_dmg
            if dmg_ratio >= 0.85:
                closeness_pts = 12.0
                if dur_min >= 25.0:
                    closeness_badge = "BATALHA PAREADA"
            elif dmg_ratio >= 0.72:
                closeness_pts = 8.0
            elif dmg_ratio >= 0.60:
                closeness_pts = 4.0
            if max_dmg >= min_contested_dmg and dmg_ratio >= EPIC_LOSS_MIN_DMG_RATIO:
                is_contested = True
            total_vkills = allies_tot["vkill"] + axis_tot["vkill"]
            if total_vkills >= 8:
                closeness_pts = min(14.0, closeness_pts + 2.0)
            if 0 < dur_min < 22.0:
                closeness_pts *= 0.65

    # 4) Impacto no Top 5 do Ranking Unificado dos Veteranos (0..26 pts)
    unified_top5_pts = 0.0
    if affects_top5_rank:
        unified_top5_pts = 26.0 if vet_won else 16.0
    elif affects_top5_sp:
        if vet_won:
            unified_top5_pts = min(18.0, 10.0 + max_top5_abs_sp * 0.35)
        else:
            unified_top5_pts = min(10.0, 4.0 + max_top5_abs_sp * 0.25)

    # 5) Peso de Modo & Fator Comunidade
    mode_pts = 0.0
    mode_badge = ""
    if mode == "1v1":
        mode_pts = 8.0
    elif mode == "2v2":
        mode_pts = 5.0
    elif is_full_vet_team_game:
        mode_pts = 4.0
        mode_badge = "TIME FECHADO VETERANOS"
    elif is_isolated_team_game:
        mode_pts = -10.0

    community_pts = 0.0
    br_badge = ""
    squad_badge = ""
    if is_br_derby:
        community_pts += 14.0
        br_badge = "CLÁSSICO VETERANOS"
    elif has_br_opponent:
        community_pts += 10.0
        br_badge = "DUELO BR vs BR"
    elif mode == "2v2" and len(vet_players) >= 2:
        community_pts += 3.0
        squad_badge = f"SQUAD {len(vet_players)} VETERANOS"

    if vet_won:
        community_pts += 10.0

    # Derrota só se qualifica como épica se for disputada, longa (>= 30 min) e contra Top Global (>1800 ELO)
    is_qualified_epic_loss = (
        (not vet_won)
        and has_top_global_opp
        and dur_sec >= EPIC_LOSS_MIN_DURATION_SEC
        and is_contested
    )
    is_qualified_top5_loss = (
        (not vet_won)
        and affects_top5_rank
        and dur_sec >= TOP5_LOSS_MIN_DURATION_SEC
        and is_contested
    )

    has_meaningful_duration = dur_sec >= MIN_MEANINGFUL_DURATION_SEC or dur_sec == 0
    if (is_priority_mode or affects_top5_rank) and vet_won and has_meaningful_duration:
        selection_tier = 4
    elif is_priority_mode and (is_qualified_epic_loss or is_qualified_top5_loss):
        selection_tier = 3
    elif (is_isolated_team_game and vet_won and has_meaningful_duration) or (is_priority_mode and vet_won):
        selection_tier = 2
    elif (
        (is_priority_mode and not vet_won and has_meaningful_duration)
        or (is_isolated_team_game and (is_qualified_epic_loss or is_qualified_top5_loss))
        or (is_isolated_team_game and vet_won)
    ):
        selection_tier = 1
    else:
        selection_tier = 0

    total_score = int(
        round(
            max(
                0.0,
                min(
                    100.0,
                    prestige_pts
                    + upset_pts
                    + duration_pts
                    + closeness_pts
                    + unified_top5_pts
                    + mode_pts
                    + community_pts,
                ),
            )
        )
    )
    badges: list[str] = [
        b
        for b in (top_badge, top5_badge, br_badge, mode_badge, closeness_badge, upset_badge, marathon_badge, squad_badge)
        if b
    ]
    if not badges:
        badges.append(f"DESTAQUE {mode.upper()}")

    scored = dict(match)
    scored.update(
        {
            "allies_team": allies,
            "axis_team": axis,
            "epicness_score": total_score,
            "selection_tier": selection_tier,
            "score_breakdown": {
                "prestige": round(prestige_pts, 1),
                "upset": round(upset_pts, 1),
                "duration": round(duration_pts, 1),
                "closeness": round(closeness_pts, 1),
                "unified_top5": round(unified_top5_pts, 1),
                "mode": round(mode_pts, 1),
                "community": round(community_pts, 1),
            },
            "badges": badges[:4],
            "best_opp_rank": best_opp_rank,
            "best_opp_alias": best_opp_player["alias"] if best_opp_player else "",
            "avg_match_elo": avg_match_elo,
            "is_br_derby": is_br_derby,
            "has_br_opponent": has_br_opponent,
            "is_full_vet_team_game": is_full_vet_team_game,
            "is_isolated_team_game": is_isolated_team_game,
            "is_priority_mode": is_priority_mode,
            "is_contested": is_contested,
            "is_qualified_epic_loss": is_qualified_epic_loss,
            "affects_unified_top5": affects_unified_top5,
            "affects_top5_rank": affects_top5_rank,
            "affects_top5_sp": affects_top5_sp,
            "unified_top5_summary": top5_summary,
            "unified_top5_impact": impact_dict if affects_unified_top5 else None,
            "vet_won": vet_won,
            "team_stats": {"allies": allies_tot, "axis": axis_tot},
        }
    )
    return scored


def enrich_top_candidates_with_cohdb(
    scored_matches: list[dict[str, Any]],
    top_global_by_pid: dict[int, dict[str, Any]] | None = None,
    cohdb_client: CohdbReplayClient | None = None,
    max_candidates: int = 4,
) -> list[dict[str, Any]]:
    """Consulta o cohdb.com apenas para as melhores candidatas (máx. 4) para refinar VP e validar link de replay."""
    if not scored_matches or cohdb_client is None:
        return scored_matches

    ordered = sorted(scored_matches, key=_match_of_the_day_sort_key)
    candidates = ordered[: max(1, min(5, int(max_candidates)))]
    rest = ordered[len(candidates) :]

    refreshed: list[dict[str, Any]] = []
    for cand in candidates:
        info = cohdb_client.inspect_match(int(cand["match_id"]))
        if info:
            cand = dict(cand)
            cand["cohdb_verified"] = bool(info.get("verified"))
            if info.get("url"):
                cand["cohdb_url"] = str(info["url"])
            if info.get("vp_pair") and cand.get("vp_allies") is None:
                cand["vp_allies"], cand["vp_axis"] = info["vp_pair"]
                cand["vp_source"] = "cohdb"
            if info.get("share_pair"):
                cand["cohdb_share_allies"], cand["cohdb_share_axis"] = info["share_pair"]
            cand = score_match_of_the_day(
                cand,
                top_global_by_pid=top_global_by_pid,
                top5_impact=cand.get("unified_top5_impact"),
            )
        refreshed.append(cand)

    final_list = refreshed + rest
    final_list.sort(key=_match_of_the_day_sort_key)
    return final_list


def build_daily_recap(
    day_matches: list[dict[str, Any]],
    target_date_str: str,
    top_global_by_pid: dict[int, dict[str, Any]] | None = None,
    cohdb_client: CohdbReplayClient | None = None,
    unified_ranking_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Agrega estatísticas diárias por Veterano, KPIs da comunidade, prêmios do dia e elege o Jogo do Dia."""
    top_map = top_global_by_pid or {}
    top5_impacts = annotate_unified_top5_impacts(day_matches, unified_ranking_context)
    scored_matches = [
        score_match_of_the_day(
            m,
            top_global_by_pid=top_map,
            top5_impact=top5_impacts.get(int(m["match_id"])),
        )
        for m in day_matches
    ]
    if cohdb_client is not None and scored_matches:
        scored_matches = enrich_top_candidates_with_cohdb(
            scored_matches,
            top_global_by_pid=top_map,
            cohdb_client=cohdb_client,
        )
    else:
        scored_matches.sort(key=_match_of_the_day_sort_key)

    per_player: dict[int, dict[str, Any]] = {}
    duo_stats: dict[tuple[int, int], dict[str, Any]] = {}
    derby_matches: list[dict[str, Any]] = []
    top_dmg_feat: dict[str, Any] | None = None

    total_combat_sec = 0
    total_appearances = 0
    total_wins = 0
    total_losses = 0
    net_community_elo = 0

    for m in scored_matches:
        dur = int(m.get("duration_sec", 0) or 0)
        total_combat_sec += dur
        if m.get("is_br_derby"):
            derby_matches.append(m)

        for team_key, opp_key in (
            ("allies_team", "axis_team"),
            ("axis_team", "allies_team"),
        ):
            team = m[team_key]
            opps = m[opp_key]
            opp_top20_rank = min(
                (int(op["global_rank"]) for op in opps if op.get("is_top_global") and op.get("global_rank")),
                default=None,
            )
            vets_on_team = [p for p in team if p["is_veterano"]]

            # Contabiliza duplas de Veteranos jogando juntos no mesmo dia
            if len(vets_on_team) >= 2:
                sorted_v = sorted(vets_on_team, key=lambda x: int(x["canonical_pid"]))
                for i in range(len(sorted_v)):
                    for j in range(i + 1, len(sorted_v)):
                        a, b = sorted_v[i], sorted_v[j]
                        if a["canonical_pid"] == b["canonical_pid"]:
                            continue
                        dk = (int(a["canonical_pid"]), int(b["canonical_pid"]))
                        ds = duo_stats.get(dk)
                        if ds is None:
                            ds = {
                                "aliases": [a["alias"], b["alias"]],
                                "games": 0,
                                "wins": 0,
                                "losses": 0,
                            }
                            duo_stats[dk] = ds
                        ds["games"] += 1
                        if a["won"]:
                            ds["wins"] += 1
                        else:
                            ds["losses"] += 1

            for p in vets_on_team:
                cpid = int(p["canonical_pid"])
                rec = per_player.get(cpid)
                if rec is None:
                    rec = {
                        "profile_id": cpid,
                        "steam_id": p["steam_id"],
                        "alias": p["alias"],
                        "country": p["country"],
                        "games": 0,
                        "wins": 0,
                        "losses": 0,
                        "elo_delta": 0,
                        "modes_set": set(),
                        "total_duration_sec": 0,
                        "total_dmg": 0,
                        "max_single_dmg": 0,
                        "total_vkills": 0,
                        "total_sqkills": 0,
                        "best_top20_faced": None,
                        "best_top20_beaten": None,
                        "beat_top20": False,
                    }
                    per_player[cpid] = rec

                rec["games"] += 1
                total_appearances += 1
                if p["won"]:
                    rec["wins"] += 1
                    total_wins += 1
                else:
                    rec["losses"] += 1
                    total_losses += 1

                delta = int(p.get("elo_delta", 0))
                rec["elo_delta"] += delta
                net_community_elo += delta
                rec["modes_set"].add(m["mode"])
                rec["total_duration_sec"] += dur

                dmg = int(p.get("dmgdone", 0))
                vk = int(p.get("vkill", 0))
                sqk = int(p.get("sqkill", 0))
                rec["total_dmg"] += dmg
                rec["total_vkills"] += vk
                rec["total_sqkills"] += sqk
                if dmg > rec["max_single_dmg"]:
                    rec["max_single_dmg"] = dmg

                if dmg > 0 and (top_dmg_feat is None or dmg > top_dmg_feat["dmgdone"]):
                    top_dmg_feat = {
                        "alias": p["alias"],
                        "profile_id": cpid,
                        "dmgdone": dmg,
                        "vkill": vk,
                        "mode": m["mode"],
                        "map_name": m["map_name"],
                    }

                if opp_top20_rank is not None:
                    if rec["best_top20_faced"] is None or opp_top20_rank < rec["best_top20_faced"]:
                        rec["best_top20_faced"] = opp_top20_rank
                    if p["won"]:
                        rec["beat_top20"] = True
                        if rec["best_top20_beaten"] is None or opp_top20_rank < rec["best_top20_beaten"]:
                            rec["best_top20_beaten"] = opp_top20_rank

    player_rows: list[dict[str, Any]] = []
    for rec in per_player.values():
        g = rec["games"]
        w = rec["wins"]
        l = rec["losses"]
        wr = int(round((w * 100.0) / g)) if g > 0 else 0
        modes_sorted = sorted(rec["modes_set"], key=lambda mk: MODES_SORT_ORDER.get(mk, 99))
        is_undefeated = w >= 2 and l == 0

        highlight = ""
        if rec["best_top20_beaten"] is not None:
            highlight = f"VENCEU TOP #{rec['best_top20_beaten']}"
        elif rec["best_top20_faced"] is not None:
            highlight = f"VS TOP #{rec['best_top20_faced']}"
        elif is_undefeated:
            highlight = f"INVICTO {w}-0"
        elif rec["elo_delta"] >= 25:
            highlight = f"+{rec['elo_delta']} ELO"

        player_rows.append(
            {
                "profile_id": rec["profile_id"],
                "steam_id": rec["steam_id"],
                "alias": rec["alias"],
                "country": rec["country"],
                "games": g,
                "wins": w,
                "losses": l,
                "win_rate": wr,
                "elo_delta": rec["elo_delta"],
                "modes": modes_sorted,
                "total_duration_sec": rec["total_duration_sec"],
                "total_dmg": rec["total_dmg"],
                "max_single_dmg": rec["max_single_dmg"],
                "total_vkills": rec["total_vkills"],
                "total_sqkills": rec["total_sqkills"],
                "best_top20_faced": rec["best_top20_faced"],
                "beat_top20": rec["beat_top20"],
                "is_undefeated": is_undefeated,
                "highlight": highlight,
            }
        )

    # Ordena por total de jogos no dia, depois vitórias e saldo ELO
    player_rows.sort(key=lambda r: (-r["games"], -r["wins"], -r["elo_delta"], r["alias"].lower()))

    # Prêmios do Dia
    mvp = None
    if player_rows:
        mvp_candidates = [r for r in player_rows if r["wins"] > 0]
        if mvp_candidates:
            mvp = max(mvp_candidates, key=lambda r: (r["elo_delta"], r["wins"], r["win_rate"], -r["losses"]))

    marathoner = max(player_rows, key=lambda r: (r["games"], r["total_duration_sec"], r["wins"])) if player_rows else None

    hot_duo = None
    valid_duos = [d for d in duo_stats.values() if d["games"] >= 2 and d["wins"] >= 2]
    if valid_duos:
        hot_duo = max(valid_duos, key=lambda d: (d["wins"], d["wins"] / d["games"], d["games"]))

    br_derby = derby_matches[0] if derby_matches else None
    match_of_the_day = scored_matches[0] if scored_matches else None

    overall_wr = int(round((total_wins * 100.0) / total_appearances)) if total_appearances > 0 else 0

    return {
        "generated_at": int(time.time()),
        "target_date_brt": target_date_str,
        "summary": {
            "total_unique_matches": len(scored_matches),
            "active_veteranos": len(player_rows),
            "total_appearances": total_appearances,
            "total_wins": total_wins,
            "total_losses": total_losses,
            "overall_win_rate": overall_wr,
            "total_combat_seconds": total_combat_sec,
            "net_community_elo": net_community_elo,
        },
        "players": player_rows,
        "awards": {
            "mvp": {
                "alias": mvp["alias"],
                "games": mvp["games"],
                "wins": mvp["wins"],
                "losses": mvp["losses"],
                "elo_delta": mvp["elo_delta"],
            }
            if mvp
            else None,
            "marathoner": {
                "alias": marathoner["alias"],
                "games": marathoner["games"],
                "wins": marathoner["wins"],
                "losses": marathoner["losses"],
                "duration_sec": marathoner["total_duration_sec"],
            }
            if marathoner
            else None,
            "top_destroyer": top_dmg_feat,
            "hot_duo": hot_duo,
            "br_derby": {
                "match_id": br_derby["match_id"],
                "mode": br_derby["mode"],
                "map_name": br_derby["map_name"],
                "allies_vets": [p["alias"] for p in br_derby["allies_team"] if p["is_veterano"]],
                "axis_vets": [p["alias"] for p in br_derby["axis_team"] if p["is_veterano"]],
                "winner_side": br_derby["winner_side"],
            }
            if br_derby
            else None,
        },
        "match_of_the_day": match_of_the_day,
    }


def export_daily_recap_json(recap: dict[str, Any], json_path: Path) -> None:
    """Exporta atomicamente o Boletim Diário em JSON auditável."""
    atomic_write_text(json_path, json.dumps(recap, ensure_ascii=False, indent=2) + "\n")
