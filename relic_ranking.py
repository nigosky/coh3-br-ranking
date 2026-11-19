"""Cliente endurecido da API Comunitária da Relic para cálculo do Ranking Unificado BR (CoH3 Steam)."""

from __future__ import annotations

import concurrent.futures
import csv
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from roster import (
    CORE_BR_ROSTER,
    STEAM_ID64_VALID_RE,
    build_roster_lookup_maps,
    extract_steam_id,
    format_bound_display_alias,
)
from security import (
    atomic_write_text,
    build_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    sanitize_csv_cell,
    validate_https_endpoint,
)

logger = logging.getLogger("coh3_ranking.relic")

BASE_URL = "https://coh3-api.reliclink.com/community/leaderboard"
ALLOWED_RELIC_HOSTS: frozenset[str] = frozenset({"coh3-api.reliclink.com"})
TITLE_STEAM = "coh3"
SOUTH_AMERICA_REGION_ID = 2074438
MAX_RELIC_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB limite rígido

# IDs oficiais das 16 leaderboards Steam usadas pelo coh3stats.com
STEAM_LEADERBOARDS: dict[str, dict[str, int]] = {
    "1v1": {
        "american": 2130255,
        "british": 2130257,
        "dak": 2130259,
        "german": 2130261,
    },
    "2v2": {
        "american": 2130300,
        "british": 2130302,
        "dak": 2130304,
        "german": 2130306,
    },
    "3v3": {
        "american": 2130329,
        "british": 2130331,
        "dak": 2130333,
        "german": 2130335,
    },
    "4v4": {
        "american": 2130353,
        "british": 2130356,
        "dak": 2130358,
        "german": 2130360,
    },
}

LEADERBOARD_ID_TO_MODE_FACTION: dict[int, tuple[str, str]] = {
    lb_id: (m_name, f_name)
    for m_name, facs in STEAM_LEADERBOARDS.items()
    for f_name, lb_id in facs.items()
}

FACTIONS_ORDER = ("american", "british", "dak", "german")
STUDY_MODE_GROUPS: dict[str, tuple[str, ...]] = {
    "1v1": ("1v1",),
    "2v2": ("2v2",),
    "Team Games": ("3v3", "4v4"),
}

_THREAD_LOCAL = threading.local()


def _get_thread_opener() -> urllib.request.OpenerDirector:
    opener = getattr(_THREAD_LOCAL, "opener", None)
    if opener is None:
        opener = build_no_redirect_https_opener()
        _THREAD_LOCAL.opener = opener
    return opener


class RelicRankingClient:
    """Cliente HTTPS endurecido com TLS 1.2+, allowlist estrita, anti-Gzip-bomb e rate-limit."""

    def __init__(self, min_interval_sec: float = 0.06, timeout_sec: float = 15.0) -> None:
        self.min_interval_sec = max(0.01, float(min_interval_sec))
        self.timeout_sec = max(2.0, float(timeout_sec))
        self._next_req_ts = 0.0
        self._rate_lock = threading.Lock()

    def _reserve_rate_slot(self) -> None:
        with self._rate_lock:
            now_m = time.monotonic()
            target = max(now_m, self._next_req_ts)
            self._next_req_ts = target + self.min_interval_sec
            wait = target - now_m
        if wait > 0:
            time.sleep(wait)

    def _get_json(
        self,
        url: str,
        retries: int = 3,
        max_bytes: int = MAX_RELIC_RESPONSE_BYTES,
    ) -> dict[str, Any]:
        validate_https_endpoint(
            url,
            ALLOWED_RELIC_HOSTS,
            path_prefix="/community/leaderboard/",
        )
        opener = _get_thread_opener()
        headers = {
            "User-Agent": "CoH3-BR-Ranking-Unificado/1.0",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

        last_err: Exception | None = None
        for attempt in range(retries):
            self._reserve_rate_slot()
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=self.timeout_sec) as resp:
                    raw_bytes = read_bounded_http_body(resp, max_bytes)
                payload = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Formato JSON inesperado retornado pela API da Relic.")
                return payload
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code in (400, 401, 403, 404):
                    break
                wait = (2**attempt) * 0.6
                logger.warning(
                    "HTTP %d na API Relic (%s) tentativa %d/%d; aguardando %.1fs",
                    exc.code,
                    url,
                    attempt + 1,
                    retries,
                    wait,
                )
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_err = exc
                wait = (2**attempt) * 0.6
                logger.warning(
                    "Falha na requisição Relic (%s) tentativa %d/%d: %s",
                    url,
                    attempt + 1,
                    retries,
                    redact_secrets(exc),
                )
                time.sleep(wait)

        raise RuntimeError(f"Erro ao consultar API da Relic ({url}): {redact_secrets(last_err)}")

    def fetch_brazilian_players(
        self,
        include_global_top_pages: int = 2,
        max_sa_rank: int = 1000,
    ) -> list[dict[str, Any]]:
        """Varre em paralelo seguro as 16 leaderboards oficiais da Steam filtrando country == 'br'."""
        players: dict[int, dict[str, Any]] = {}
        ingest_lock = threading.Lock()

        def ingest_response(data: dict[str, Any], mode: str, faction: str) -> int:
            stat_groups = {sg["id"]: sg for sg in data.get("statGroups", []) if "id" in sg}
            lb_stats = data.get("leaderboardStats", [])
            key = f"{mode}_{faction}"
            mode_badge = f"{mode.upper()} ({faction.upper()})"

            with ingest_lock:
                for st in lb_stats:
                    sg = stat_groups.get(st.get("statgroup_id"))
                    if not sg:
                        continue
                    for member in sg.get("members", []):
                        if str(member.get("country", "")).lower() != "br":
                            continue
                        pid = int(member["profile_id"])
                        steam_id = extract_steam_id(member.get("name"))
                        p = players.get(pid)
                        if p is None:
                            p = {
                                "profile_id": pid,
                                "alias": member.get("alias") or f"Player {pid}",
                                "steam_id": steam_id,
                                "country": "br",
                                "level": int(member.get("level", 0)),
                                "xp": int(member.get("xp", 0)),
                                "max_elo": 0,
                                "best_mode": "",
                                "total_wins": 0,
                                "total_losses": 0,
                                "last_match_date": 0,
                                "modes": {},
                                "coh3stats_url": f"https://coh3stats.com/players/{pid}",
                            }
                            players[pid] = p
                        elif steam_id and not p.get("steam_id"):
                            p["steam_id"] = steam_id

                        elo = int(round(st.get("rating", 0)))
                        wins = int(st.get("wins", 0))
                        losses = int(st.get("losses", 0))
                        lmd = int(st.get("lastmatchdate", 0))
                        if key not in p["modes"]:
                            p["modes"][key] = {
                                "mode": mode,
                                "faction": faction,
                                "elo": elo,
                                "rank": int(st.get("rank", 0)),
                                "regionrank": int(st.get("regionrank", 0)),
                                "wins": wins,
                                "losses": losses,
                                "streak": int(st.get("streak", 0)),
                                "lastmatchdate": lmd,
                            }
                            p["total_wins"] += wins
                            p["total_losses"] += losses
                        if elo > p["max_elo"]:
                            p["max_elo"] = elo
                            p["best_mode"] = mode_badge
                        if lmd > p["last_match_date"]:
                            p["last_match_date"] = lmd
            return len(lb_stats)

        def scan_single_leaderboard(mode: str, faction: str, lb_id: int) -> None:
            start = 1
            while start <= max_sa_rank:
                url = (
                    f"{BASE_URL}/getleaderboard2?title={TITLE_STEAM}&leaderboard_id={lb_id}"
                    f"&sortBy=1&count=200&start={start}&leaderboardRegion_id={SOUTH_AMERICA_REGION_ID}"
                )
                data = self._get_json(url)
                count_rows = ingest_response(data, mode, faction)
                rank_total = int(data.get("rankTotal", 0))
                if count_rows == 0 or start + 200 > rank_total:
                    break
                start += 200

            for page in range(include_global_top_pages):
                g_start = 1 + page * 200
                g_url = (
                    f"{BASE_URL}/getleaderboard2?title={TITLE_STEAM}&leaderboard_id={lb_id}"
                    f"&sortBy=1&count=200&start={g_start}"
                )
                g_data = self._get_json(g_url)
                if ingest_response(g_data, mode, faction) == 0:
                    break

        tasks = [
            (mode, faction, lb_id)
            for mode, facs in STEAM_LEADERBOARDS.items()
            for faction, lb_id in facs.items()
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(scan_single_leaderboard, m, f, lb) for m, f, lb in tasks]
            for fut in concurrent.futures.as_completed(futures):
                fut.result()

        return sorted(players.values(), key=lambda x: (-x["max_elo"], -x["total_wins"], x["alias"].lower()))


def _ingest_personal_stat_payload(
    data: dict[str, Any],
    results_by_pid: dict[int, dict[str, Any]],
    roster_maps: tuple[dict[str, dict[str, Any]], dict[int, dict[str, Any]]],
    allowed_pids: set[int] | None = None,
    allowed_sids: set[str] | None = None,
) -> None:
    steam_map, pid_map = roster_maps
    solo_sg_to_pid: dict[int, int] = {}

    for sg in data.get("statGroups", []):
        members = sg.get("members", [])
        if sg.get("type") != 1 or len(members) != 1:
            continue
        mem = members[0]
        pid = int(mem.get("profile_id", 0))
        steam_id = extract_steam_id(mem.get("name"))
        if allowed_sids is not None and steam_id not in allowed_sids:
            if allowed_pids is None or pid not in allowed_pids:
                continue
        elif allowed_sids is None and allowed_pids is not None and pid not in allowed_pids:
            continue

        sg_id = int(sg["id"])
        solo_sg_to_pid[sg_id] = pid

        r_entry = steam_map.get(steam_id) or pid_map.get(pid) or {}
        orig_alias = str(r_entry.get("original_alias") or "")
        cur_alias = str(mem.get("alias") or orig_alias or f"Player {pid}")
        disp_alias = format_bound_display_alias(cur_alias, orig_alias, r_entry.get("known_aliases"))

        results_by_pid[pid] = {
            "profile_id": pid,
            "alias": disp_alias,
            "current_alias": cur_alias,
            "original_alias": orig_alias or cur_alias,
            "steam_id": steam_id or str(r_entry.get("steam_id") or ""),
            "merged_into_profile_id": int(r_entry.get("merged_into_profile_id", 0) or 0),
            "merged_into_steam_id": str(r_entry.get("merged_into_steam_id") or ""),
            "country": str(mem.get("country") or "br").lower(),
            "level": int(mem.get("level", 0)),
            "xp": int(mem.get("xp", 0)),
            "max_elo": 0,
            "best_mode": "",
            "total_wins": 0,
            "total_losses": 0,
            "last_match_date": 0,
            "modes": {},
            "inactive_modes": {},
            "is_custom": True,
            "is_br_roster": True,
            "coh3stats_url": f"https://coh3stats.com/players/{pid}",
        }

    for st in data.get("leaderboardStats", []):
        sg_id = int(st.get("statgroup_id", -1))
        lb_id = int(st.get("leaderboard_id", -1))
        if sg_id not in solo_sg_to_pid or lb_id not in LEADERBOARD_ID_TO_MODE_FACTION:
            continue
        p = results_by_pid[solo_sg_to_pid[sg_id]]
        mode, faction = LEADERBOARD_ID_TO_MODE_FACTION[lb_id]
        elo = int(round(st.get("rating", 0)))
        if elo <= 0:
            continue
        wins = int(st.get("wins", 0))
        losses = int(st.get("losses", 0))
        lmd = int(st.get("lastmatchdate", 0))
        rank_val = int(st.get("rank", -1))
        stat_obj = {
            "mode": mode,
            "faction": faction,
            "elo": elo,
            "rank": rank_val,
            "regionrank": int(st.get("regionrank", -1)),
            "wins": wins,
            "losses": losses,
            "streak": int(st.get("streak", 0)),
            "lastmatchdate": lmd,
        }
        target_dict = p["modes"] if rank_val > 0 else p["inactive_modes"]
        target_dict[f"{mode}_{faction}"] = stat_obj
        p["total_wins"] += wins
        p["total_losses"] += losses
        if elo > p["max_elo"]:
            p["max_elo"] = elo
            p["best_mode"] = f"{mode.upper()} ({faction.upper()})"
        if lmd > p["last_match_date"]:
            p["last_match_date"] = lmd


def fetch_custom_players_stats(
    client: RelicRankingClient,
    targets: list[int] | list[dict[str, Any]],
    steam_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Consulta getPersonalStat em paralelo seguro (lotes de até 10 contas) priorizando SteamID64."""
    dict_targets: list[dict[str, Any]] = []
    steam_set: set[str] = set()
    fallback_pid_set: set[int] = set()

    for item in targets or []:
        if isinstance(item, dict):
            dict_targets.append(item)
            sid = extract_steam_id(item.get("steam_id"))
            pid = int(item.get("profile_id", 0) or 0)
            if sid:
                steam_set.add(sid)
            elif pid > 0:
                fallback_pid_set.add(pid)
        else:
            pid = int(item or 0)
            if pid > 0:
                fallback_pid_set.add(pid)

    for sid in steam_ids or []:
        clean_sid = extract_steam_id(sid)
        if clean_sid:
            steam_set.add(clean_sid)

    roster_maps = build_roster_lookup_maps(dict_targets)
    steam_map, pid_map = roster_maps

    for pid in list(fallback_pid_set):
        known = pid_map.get(pid)
        if known and known.get("steam_id"):
            steam_set.add(known["steam_id"])
            fallback_pid_set.discard(pid)

    results_by_pid: dict[int, dict[str, Any]] = {}
    ordered_sids = sorted(steam_set)
    sid_batches = [ordered_sids[i : i + 10] for i in range(0, len(ordered_sids), 10)]

    if sid_batches:
        def _fetch_sid_batch(batch_sids: list[str]) -> tuple[list[str], dict[str, Any]]:
            names_json = json.dumps([f"/steam/{s}" for s in batch_sids], separators=(",", ":"))
            encoded_names = urllib.parse.quote(names_json)
            url = f"{BASE_URL}/getPersonalStat?title={TITLE_STEAM}&profile_names={encoded_names}"
            return batch_sids, client._get_json(url)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(sid_batches))) as pool:
            for batch_sids, data in pool.map(_fetch_sid_batch, sid_batches):
                _ingest_personal_stat_payload(
                    data,
                    results_by_pid,
                    roster_maps=roster_maps,
                    allowed_sids=set(batch_sids),
                )

    resolved_sids = {str(v.get("steam_id") or "") for v in results_by_pid.values()}
    for s in ordered_sids:
        if s not in resolved_sids and s in steam_map and int(steam_map[s].get("profile_id", 0)) > 0:
            fallback_pid_set.add(int(steam_map[s]["profile_id"]))

    ordered_pids = sorted(pid for pid in fallback_pid_set if pid not in results_by_pid)
    pid_batches = [ordered_pids[i : i + 10] for i in range(0, len(ordered_pids), 10)]

    if pid_batches:
        def _fetch_pid_batch(batch_pids: list[int]) -> tuple[list[int], dict[str, Any]]:
            encoded_ids = urllib.parse.quote("[" + ",".join(str(i) for i in batch_pids) + "]")
            url = f"{BASE_URL}/getPersonalStat?title={TITLE_STEAM}&profile_ids={encoded_ids}"
            return batch_pids, client._get_json(url)

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(pid_batches))) as pool:
            for batch_pids, data in pool.map(_fetch_pid_batch, pid_batches):
                _ingest_personal_stat_payload(
                    data,
                    results_by_pid,
                    roster_maps=roster_maps,
                    allowed_pids=set(batch_pids),
                )

    return list(results_by_pid.values())


def _merge_player_stat_records(primary: dict[str, Any], secondary: dict[str, Any]) -> None:
    """Mescla estatísticas de conta secundária (ex.: gabe44arg) na conta principal (ex.: É o Gabe)."""
    p_modes = primary.setdefault("modes", {})
    p_inact = primary.setdefault("inactive_modes", {})
    s_modes = secondary.get("modes", {})
    s_inact = secondary.get("inactive_modes", {})

    for key in set(p_modes) | set(p_inact) | set(s_modes) | set(s_inact):
        pm, pi = p_modes.get(key), p_inact.get(key)
        sm, si = s_modes.get(key), s_inact.get(key)
        p_rec, s_rec = pm or pi, sm or si

        if p_rec and not s_rec:
            continue
        if s_rec and not p_rec:
            if sm:
                p_modes[key] = dict(sm)
            elif si:
                p_inact[key] = dict(si)
            continue

        assert p_rec is not None and s_rec is not None
        comb_wins = int(p_rec.get("wins", 0)) + int(s_rec.get("wins", 0))
        comb_losses = int(p_rec.get("losses", 0)) + int(s_rec.get("losses", 0))
        p_lmd, s_lmd = int(p_rec.get("lastmatchdate", 0)), int(s_rec.get("lastmatchdate", 0))
        latest_lmd = max(p_lmd, s_lmd)
        latest_streak = int((s_rec if s_lmd > p_lmd else p_rec).get("streak", 0))

        active_candidates = [r for r in (pm, sm) if r is not None]
        if active_candidates:
            merged_slot = dict(max(active_candidates, key=lambda r: int(r.get("elo", 0))))
            merged_slot.update(wins=comb_wins, losses=comb_losses, lastmatchdate=latest_lmd, streak=latest_streak)
            p_modes[key] = merged_slot
            p_inact.pop(key, None)
        else:
            merged_slot = dict(max((p_rec, s_rec), key=lambda r: int(r.get("elo", 0))))
            merged_slot.update(wins=comb_wins, losses=comb_losses, lastmatchdate=latest_lmd, streak=latest_streak)
            p_inact[key] = merged_slot

    primary["total_wins"] = int(primary.get("total_wins", 0)) + int(secondary.get("total_wins", 0))
    primary["total_losses"] = int(primary.get("total_losses", 0)) + int(secondary.get("total_losses", 0))
    if int(secondary.get("max_elo", 0)) > int(primary.get("max_elo", 0)):
        primary["max_elo"] = secondary["max_elo"]
        primary["best_mode"] = secondary.get("best_mode", primary.get("best_mode", ""))
    if int(secondary.get("last_match_date", 0)) > int(primary.get("last_match_date", 0)):
        primary["last_match_date"] = secondary["last_match_date"]


def merge_catalog_with_custom_players(
    base_players: list[dict[str, Any]],
    custom_players: list[dict[str, Any]],
    relic: RelicRankingClient,
) -> list[dict[str, Any]]:
    """Consulta getPersonalStat por SteamID64, aplica apelidos preferenciais e funde contas vinculadas."""
    by_pid: dict[int, dict[str, Any]] = {int(p["profile_id"]): dict(p) for p in base_players}
    effective_custom = custom_players if custom_players else list(CORE_BR_ROSTER)
    steam_map, pid_map = build_roster_lookup_maps(effective_custom)

    fetched_custom = fetch_custom_players_stats(relic, effective_custom)
    fetched_by_sid: dict[str, dict[str, Any]] = {}
    fetched_by_pid: dict[int, dict[str, Any]] = {}

    for cp_stat in fetched_custom:
        pid = int(cp_stat["profile_id"])
        sid = str(cp_stat.get("steam_id") or "")
        if sid:
            fetched_by_sid[sid] = cp_stat
        fetched_by_pid[pid] = cp_stat

        if pid in by_pid:
            existing = by_pid[pid]
            existing["alias"] = cp_stat["alias"]
            existing["current_alias"] = cp_stat.get("current_alias", cp_stat["alias"])
            existing["original_alias"] = cp_stat.get("original_alias", cp_stat["alias"])
            if sid:
                existing["steam_id"] = sid
            existing["is_custom"] = True
            existing["is_br_roster"] = True
            existing["merged_into_profile_id"] = int(cp_stat.get("merged_into_profile_id", 0) or 0)
            existing["merged_into_steam_id"] = str(cp_stat.get("merged_into_steam_id") or "")
            existing.setdefault("inactive_modes", {}).update(cp_stat.get("inactive_modes", {}))
            existing.setdefault("modes", {}).update(cp_stat.get("modes", {}))
            if cp_stat.get("max_elo", 0) > existing.get("max_elo", 0):
                existing["max_elo"] = cp_stat["max_elo"]
                existing["best_mode"] = cp_stat.get("best_mode", "")
            if cp_stat.get("last_match_date", 0) > existing.get("last_match_date", 0):
                existing["last_match_date"] = cp_stat["last_match_date"]
        else:
            by_pid[pid] = cp_stat

    merged_away_pids: list[int] = []
    for pid, p_obj in list(by_pid.items()):
        sid = str(p_obj.get("steam_id") or "")
        r_entry = steam_map.get(sid) or pid_map.get(pid) or {}
        target_pid = int(p_obj.get("merged_into_profile_id") or r_entry.get("merged_into_profile_id") or 0)
        if target_pid > 0 and target_pid != pid and target_pid in by_pid:
            _merge_player_stat_records(by_pid[target_pid], p_obj)
            merged_away_pids.append(pid)

    for sec_pid in merged_away_pids:
        by_pid.pop(sec_pid, None)

    for cp in custom_players:
        sid = extract_steam_id(cp.get("steam_id"))
        pid = int(cp.get("profile_id", 0) or 0)
        resolved = fetched_by_sid.get(sid) or fetched_by_pid.get(pid)
        r_entry = steam_map.get(sid) or pid_map.get(pid) or {}
        if resolved:
            cp["profile_id"] = int(resolved["profile_id"])
            if resolved.get("steam_id"):
                cp["steam_id"] = str(resolved["steam_id"])
            orig = str(
                r_entry.get("original_alias")
                or cp.get("original_alias")
                or resolved.get("original_alias")
                or resolved["alias"]
            )
            cp["original_alias"] = orig
            cp["alias"] = format_bound_display_alias(
                str(resolved.get("current_alias") or resolved["alias"]),
                orig,
                r_entry.get("known_aliases") or cp.get("known_aliases"),
            )

    return list(by_pid.values())


def _accumulate_mode_stats(
    source_modes: dict[str, Any],
    sub_modes: tuple[str, ...],
) -> tuple[list[int], dict[str, list[int]], dict[str, int], int, int, int, int]:
    all_elos: list[int] = []
    per_faction: dict[str, list[int]] = {f: [] for f in FACTIONS_ORDER}
    submode_elos: dict[str, int] = {}
    wins = 0
    losses = 0
    last_match = 0
    latest_streak = 0
    latest_streak_ts = -1

    for sub in sub_modes:
        for fac in FACTIONS_ORDER:
            key = f"{sub}_{fac}"
            st = source_modes.get(key)
            if not st:
                continue
            elo_val = int(round(st.get("elo", 0)))
            if elo_val <= 0:
                continue
            all_elos.append(elo_val)
            per_faction[fac].append(elo_val)
            submode_elos[key] = elo_val
            wins += int(st.get("wins", 0))
            losses += int(st.get("losses", 0))
            lmd_val = int(st.get("lastmatchdate", 0))
            if lmd_val > last_match:
                last_match = lmd_val
            if lmd_val >= latest_streak_ts:
                latest_streak_ts = lmd_val
                latest_streak = int(st.get("streak", 0))

    return all_elos, per_faction, submode_elos, wins, losses, last_match, latest_streak


def compute_merged_mode_rankings(
    players: list[dict[str, Any]],
    mode_groups: dict[str, tuple[str, ...]] | None = None,
    custom_ids: set[int] | None = None,
    custom_steam_ids: set[str] | None = None,
    only_custom: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Mescla os rankings das facções ativas dentro de '1v1', '2v2' e 'Team Games' pela média de ELO (Veteranos)."""
    groups = mode_groups or STUDY_MODE_GROUPS
    custom_set = {int(x) for x in (custom_ids or set())}
    custom_sid_set = {str(s) for s in (custom_steam_ids or set()) if s}
    rankings_by_mode: dict[str, list[dict[str, Any]]] = {}

    for group_name, sub_modes in groups.items():
        mode_entries: list[dict[str, Any]] = []
        max_slots = len(sub_modes) * len(FACTIONS_ORDER)

        for p in players:
            pid = int(p["profile_id"])
            sid = str(p.get("steam_id") or "")
            is_custom = pid in custom_set or (bool(sid) and sid in custom_sid_set) or bool(p.get("is_custom"))
            if only_custom and not is_custom:
                continue

            (
                all_active_elos,
                per_faction_lists,
                submode_elos,
                wins_mode,
                losses_mode,
                last_match_mode,
                latest_streak,
            ) = _accumulate_mode_stats(p.get("modes", {}), sub_modes)

            used_inactive_fallback = False
            if not all_active_elos and is_custom and p.get("inactive_modes"):
                used_inactive_fallback = True
                (
                    all_active_elos,
                    per_faction_lists,
                    submode_elos,
                    wins_mode,
                    losses_mode,
                    last_match_mode,
                    latest_streak,
                ) = _accumulate_mode_stats(p["inactive_modes"], sub_modes)

            if not all_active_elos:
                continue

            faction_elos = {
                fac: int(round(sum(vals) / len(vals)))
                for fac, vals in per_faction_lists.items()
                if vals
            }
            avg_elo = round(sum(all_active_elos) / len(all_active_elos), 1)
            total_games = wins_mode + losses_mode
            win_ratio = int(round((wins_mode * 100.0) / total_games)) if total_games > 0 else 0

            mode_entries.append(
                {
                    "profile_id": pid,
                    "alias": str(p["alias"]),
                    "current_alias": str(p.get("current_alias") or p["alias"]),
                    "original_alias": str(p.get("original_alias") or p["alias"]),
                    "steam_id": sid,
                    "country": str(p.get("country") or "br").lower(),
                    "mode": group_name,
                    "sub_modes": list(sub_modes),
                    "avg_elo": avg_elo,
                    "max_mode_elo": max(all_active_elos),
                    "active_factions_count": len(faction_elos),
                    "active_rankings_count": len(all_active_elos),
                    "max_rankings_slots": max_slots,
                    "faction_elos": faction_elos,
                    "submode_elos": submode_elos,
                    "streak": latest_streak,
                    "wins": wins_mode,
                    "losses": losses_mode,
                    "total": total_games,
                    "ratio": win_ratio,
                    "last_match_date": last_match_mode,
                    "is_custom": is_custom,
                    "is_br_roster": bool(p.get("is_br_roster")) or is_custom,
                    "inactive_fallback": used_inactive_fallback,
                    "rank_delta": int(p.get("rank_delta", 0)),
                    "prev_rank": p.get("prev_rank"),
                    "coh3stats_url": f"https://coh3stats.com/players/{pid}",
                }
            )

        mode_entries.sort(
            key=lambda x: (
                1 if x.get("inactive_fallback") else 0,
                -x["avg_elo"],
                -x["active_rankings_count"],
                -x["max_mode_elo"],
                -x["wins"],
            )
        )
        for rank_pos, entry in enumerate(mode_entries, start=1):
            entry["br_rank"] = rank_pos

        rankings_by_mode[group_name] = mode_entries

    return rankings_by_mode


def export_players_files(players: list[dict[str, Any]], json_path: Path, csv_path: Path) -> None:
    """Salva atomicamente o catálogo completo de jogadores brasileiros em JSON e CSV."""
    json_str = json.dumps(
        {
            "updated_at": int(time.time()),
            "total_players": len(players),
            "players": players,
        },
        ensure_ascii=False,
        indent=2,
    )
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf, lineterminator="\n")
    writer.writerow(
        [
            "profile_id",
            "alias",
            "steam_id",
            "max_elo",
            "best_mode",
            "total_wins",
            "total_losses",
            "modes",
            "coh3stats_url",
        ]
    )
    for p in players:
        modes = ",".join(sorted({v["mode"] for v in p.get("modes", {}).values()}))
        pid_int = int(p["profile_id"])
        writer.writerow(
            [
                pid_int,
                sanitize_csv_cell(p["alias"]),
                sanitize_csv_cell(p.get("steam_id", "")),
                p.get("max_elo", 0),
                sanitize_csv_cell(p.get("best_mode", "")),
                p.get("total_wins", 0),
                p.get("total_losses", 0),
                sanitize_csv_cell(modes),
                f"https://coh3stats.com/players/{pid_int}",
            ]
        )
    atomic_write_text(json_path, json_str + "\n")
    atomic_write_text(csv_path, csv_buf.getvalue())


def export_merged_top_rankings(
    rankings_by_mode: dict[str, list[dict[str, Any]]],
    top_n: int,
    json_path: Path,
    csv_path: Path,
    custom_players: list[dict[str, Any]] | None = None,
    only_custom: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    """Exporta atomicamente o ranking unificado dos Veteranos (ou Top N + monitorados) em JSON/CSV."""
    custom_ids = {int(cp["profile_id"]) for cp in (custom_players or []) if int(cp.get("profile_id", 0)) > 0}
    custom_sids = {str(cp.get("steam_id") or "") for cp in (custom_players or []) if cp.get("steam_id")}
    trimmed: dict[str, list[dict[str, Any]]] = {}

    for mode, entries in rankings_by_mode.items():
        if only_custom:
            filtered = [
                dict(e)
                for e in entries
                if e["profile_id"] in custom_ids
                or (bool(e.get("steam_id")) and str(e["steam_id"]) in custom_sids)
                or bool(e.get("is_custom"))
            ]
            for rank_pos, entry in enumerate(filtered, start=1):
                entry["br_rank"] = rank_pos
            trimmed[mode] = filtered
        else:
            trimmed[mode] = [
                e
                for idx, e in enumerate(entries)
                if idx < top_n
                or e["profile_id"] in custom_ids
                or (bool(e.get("steam_id")) and str(e["steam_id"]) in custom_sids)
                or bool(e.get("is_custom"))
            ]

    unique_pids = {int(e["profile_id"]) for entries in trimmed.values() for e in entries}

    json_str = json.dumps(
        {
            "updated_at": int(time.time()),
            "methodology": "simple_average_of_active_factions_elo_1v1_2v2_team_games",
            "modes": list(rankings_by_mode.keys()),
            "top_n_per_mode": top_n,
            "unique_players_count": len(unique_pids),
            "rankings_by_mode": trimmed,
        },
        ensure_ascii=False,
        indent=2,
    )

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf, lineterminator="\n")
    writer.writerow(
        [
            "mode",
            "br_rank",
            "rank_delta",
            "is_monitored",
            "inactive_fallback",
            "profile_id",
            "steam_id",
            "original_alias",
            "display_alias",
            "avg_elo",
            "active_rankings",
            "elo_usf",
            "elo_ukf",
            "elo_dak",
            "elo_wehr",
            "mode_wins",
            "mode_losses",
            "coh3stats_url",
        ]
    )
    for mode, entries in trimmed.items():
        for e in entries:
            fe = e["faction_elos"]
            pid_int = int(e["profile_id"])
            writer.writerow(
                [
                    sanitize_csv_cell(mode),
                    e["br_rank"],
                    e.get("rank_delta", 0),
                    "sim" if e.get("is_custom") else "nao",
                    "sim" if e.get("inactive_fallback") else "nao",
                    pid_int,
                    sanitize_csv_cell(e.get("steam_id", "")),
                    sanitize_csv_cell(e.get("original_alias", "")),
                    sanitize_csv_cell(e["alias"]),
                    e["avg_elo"],
                    f"{e['active_rankings_count']}/{e['max_rankings_slots']}",
                    fe.get("american", ""),
                    fe.get("british", ""),
                    fe.get("dak", ""),
                    fe.get("german", ""),
                    e["wins"],
                    e["losses"],
                    f"https://coh3stats.com/players/{pid_int}",
                ]
            )

    atomic_write_text(json_path, json_str + "\n")
    atomic_write_text(csv_path, csv_buf.getvalue())
    return trimmed
