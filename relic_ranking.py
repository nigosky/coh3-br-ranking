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
UNIFIED_MODES_ORDER: tuple[str, ...] = ("1v1", "2v2", "3v3", "4v4")
UNIFIED_MODE_WEIGHTS: dict[str, float] = {
    "1v1": 2.0,
    "2v2": 1.5,
    "3v3": 1.0,
    "4v4": 1.0,
}
UNIFIED_BASE_ELO: float = 1000.0
UNIFIED_SECONDARY_BONUS_RATE: float = 0.15
UNIFIED_MIN_GAMES_PER_QUEUE: int = 10

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
        disp_alias = format_bound_display_alias(
            cur_alias,
            orig_alias,
            r_entry.get("known_aliases"),
            pin_alias=bool(r_entry.get("pin_alias")),
        )

        results_by_pid[pid] = {
            "profile_id": pid,
            "alias": disp_alias,
            "current_alias": cur_alias,
            "original_alias": orig_alias or cur_alias,
            "steam_id": steam_id or str(r_entry.get("steam_id") or ""),
            "merged_into_profile_id": int(r_entry.get("merged_into_profile_id", 0) or 0),
            "merged_into_steam_id": str(r_entry.get("merged_into_steam_id") or ""),
            "country": str(r_entry.get("country") or mem.get("country") or "br").lower(),
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
    """Mescla estatísticas de conta secundária (ex.: gabe44arg) na conta principal (ex.: Gabe)."""
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
                pin_alias=bool(r_entry.get("pin_alias") or cp.get("pin_alias")),
            )

    return list(by_pid.values())


def compute_unified_skill_ranking(
    players: list[dict[str, Any]],
    custom_ids: set[int] | None = None,
    custom_steam_ids: set[str] | None = None,
    only_custom: bool = True,
    min_games: int = UNIFIED_MIN_GAMES_PER_QUEUE,
    mode_weights: dict[str, float] | None = None,
    base_elo: float = UNIFIED_BASE_ELO,
    bonus_rate: float = UNIFIED_SECONDARY_BONUS_RATE,
) -> list[dict[str, Any]]:
    """Calcula o Ranking Unificado BR em Skill Points combinando todos os modos (1v1, 2v2, 3v3, 4v4).

    Metodologia:
      1. Para cada modo m em {1v1, 2v2, 3v3, 4v4}, considera filas ativas e inativas com
         (wins + losses) >= min_games e seleciona o maior ELO M_m entre as 4 facções.
      2. Calcula o score ponderado sobre o excedente: P_m = base_elo + (M_m - base_elo) * w_m,
         com w_1v1=2.0, w_2v2=1.5, w_3v3=1.0, w_4v4=1.0.
      3. Skill Points = max_m(P_m) + bonus_rate * sum_{j != melhor} max(0, P_j - base_elo).
      4. Jogadores 100% inativos (nenhuma fila ativa na Relic) recebem `inactive_fallback=True`
         para destaque visual abaixo dos jogadores ativos.
    """
    weights = mode_weights or UNIFIED_MODE_WEIGHTS
    min_games_req = max(1, int(min_games))
    custom_set = {int(x) for x in (custom_ids or set())}
    custom_sid_set = {str(s) for s in (custom_steam_ids or set()) if s}
    entries: list[dict[str, Any]] = []

    for p in players:
        pid = int(p["profile_id"])
        sid = str(p.get("steam_id") or "")
        is_custom = pid in custom_set or (bool(sid) and sid in custom_sid_set) or bool(p.get("is_custom"))
        if only_custom and not is_custom:
            continue

        active_modes = p.get("modes") or {}
        inactive_modes = p.get("inactive_modes") or {}

        # Combina todas as 16 filas (ativas têm prioridade se a mesma chave existir em ambos)
        all_slots: dict[str, dict[str, Any]] = {}
        for k, st in inactive_modes.items():
            if isinstance(st, dict):
                all_slots[k] = st
        for k, st in active_modes.items():
            if isinstance(st, dict):
                all_slots[k] = st

        has_any_active_queue = any(
            isinstance(st, dict) and int(round(st.get("elo", 0))) > 0
            for st in active_modes.values()
        )
        is_fully_inactive = not has_any_active_queue

        mode_max_elos: dict[str, int] = {}
        mode_best_factions: dict[str, str] = {}
        mode_weighted_scores: dict[str, float] = {}

        for mode in UNIFIED_MODES_ORDER:
            best_candidate: tuple[int, int, int, str] | None = None
            for fac in FACTIONS_ORDER:
                key = f"{mode}_{fac}"
                st = all_slots.get(key)
                if not st:
                    continue
                elo_val = int(round(st.get("elo", 0)))
                games_val = int(st.get("wins", 0)) + int(st.get("losses", 0))
                if elo_val <= 0 or games_val < min_games_req:
                    continue
                is_active_slot = 1 if key in active_modes else 0
                cand = (elo_val, games_val, is_active_slot, fac)
                if best_candidate is None or cand[:3] > best_candidate[:3]:
                    best_candidate = cand

            if best_candidate is not None:
                best_elo, _, _, best_fac = best_candidate
                w_m = float(weights.get(mode, 1.0))
                p_m = max(0.0, float(base_elo) + (float(best_elo) - float(base_elo)) * w_m)
                mode_max_elos[mode] = best_elo
                mode_best_factions[mode] = best_fac
                mode_weighted_scores[mode] = round(p_m, 2)

        if not mode_max_elos:
            continue

        anchor_mode = max(
            mode_weighted_scores.keys(),
            key=lambda m: (
                mode_weighted_scores[m],
                float(weights.get(m, 1.0)),
                mode_max_elos[m],
            ),
        )
        anchor_score = mode_weighted_scores[anchor_mode]
        bonus_score = float(bonus_rate) * sum(
            max(0.0, score - float(base_elo))
            for m, score in mode_weighted_scores.items()
            if m != anchor_mode
        )
        skill_points = round(anchor_score + bonus_score, 1)

        # Estatísticas agregadas de carreira em todas as filas ranqueadas
        total_wins = 0
        total_losses = 0
        last_match_ts = int(p.get("last_match_date", 0) or 0)
        latest_streak = 0
        latest_streak_ts = -1
        for st in all_slots.values():
            total_wins += int(st.get("wins", 0))
            total_losses += int(st.get("losses", 0))
            lmd_val = int(st.get("lastmatchdate", 0))
            if lmd_val > last_match_ts:
                last_match_ts = lmd_val
            if lmd_val >= latest_streak_ts:
                latest_streak_ts = lmd_val
                latest_streak = int(st.get("streak", 0))

        total_games = total_wins + total_losses
        win_ratio = int(round((total_wins * 100.0) / total_games)) if total_games > 0 else 0

        entries.append(
            {
                "profile_id": pid,
                "alias": str(p["alias"]),
                "current_alias": str(p.get("current_alias") or p["alias"]),
                "original_alias": str(p.get("original_alias") or p["alias"]),
                "steam_id": sid,
                "country": str(p.get("country") or "br").lower(),
                "skill_points": skill_points,
                "anchor_mode": anchor_mode,
                "anchor_score": round(anchor_score, 1),
                "bonus_score": round(bonus_score, 1),
                "mode_max_elos": mode_max_elos,
                "mode_best_factions": mode_best_factions,
                "mode_weighted_scores": mode_weighted_scores,
                "valid_modes_count": len(mode_max_elos),
                "max_elo": max(mode_max_elos.values()),
                "streak": latest_streak,
                "wins": total_wins,
                "losses": total_losses,
                "total": total_games,
                "ratio": win_ratio,
                "last_match_date": last_match_ts,
                "is_custom": is_custom,
                "is_br_roster": bool(p.get("is_br_roster")) or is_custom,
                "inactive_fallback": is_fully_inactive,
                "coh3stats_url": f"https://coh3stats.com/players/{pid}",
            }
        )

    entries.sort(
        key=lambda x: (
            1 if x.get("inactive_fallback") else 0,
            -float(x["skill_points"]),
            -int(x["valid_modes_count"]),
            -int(x["max_elo"]),
            -int(x["wins"]),
            str(x["alias"]).lower(),
        )
    )
    for rank_pos, entry in enumerate(entries, start=1):
        entry["br_rank"] = rank_pos

    return entries


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


def export_unified_skill_ranking(
    entries: list[dict[str, Any]],
    json_path: Path,
    csv_path: Path,
    custom_players: list[dict[str, Any]] | None = None,
    only_custom: bool = True,
    min_games: int = UNIFIED_MIN_GAMES_PER_QUEUE,
) -> list[dict[str, Any]]:
    """Exporta atomicamente o Ranking Unificado BR (Skill Points) em JSON e CSV."""
    custom_ids = {int(cp["profile_id"]) for cp in (custom_players or []) if int(cp.get("profile_id", 0)) > 0}
    custom_sids = {str(cp.get("steam_id") or "") for cp in (custom_players or []) if cp.get("steam_id")}

    if only_custom:
        filtered = [
            dict(e)
            for e in entries
            if int(e["profile_id"]) in custom_ids
            or (bool(e.get("steam_id")) and str(e["steam_id"]) in custom_sids)
            or bool(e.get("is_custom"))
        ]
    else:
        filtered = [dict(e) for e in entries]

    for rank_pos, entry in enumerate(filtered, start=1):
        entry["br_rank"] = rank_pos

    active_cnt = sum(1 for e in filtered if not e.get("inactive_fallback"))
    inactive_cnt = len(filtered) - active_cnt

    json_str = json.dumps(
        {
            "updated_at": int(time.time()),
            "methodology": "unified_skill_points_anchor_plus_secondary_bonus",
            "formula": "max_m(P_m) + 0.15 * sum_{j != anchor} max(0, P_j - 1000); P_m = 1000 + (M_m - 1000) * w_m",
            "mode_weights": dict(UNIFIED_MODE_WEIGHTS),
            "base_elo": UNIFIED_BASE_ELO,
            "secondary_bonus_rate": UNIFIED_SECONDARY_BONUS_RATE,
            "min_games_per_queue": int(min_games),
            "total_players": len(filtered),
            "active_players": active_cnt,
            "inactive_players": inactive_cnt,
            "rankings": filtered,
        },
        ensure_ascii=False,
        indent=2,
    )

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf, lineterminator="\n")
    writer.writerow(
        [
            "br_rank",
            "is_monitored",
            "inactive_fallback",
            "profile_id",
            "steam_id",
            "original_alias",
            "display_alias",
            "skill_points",
            "anchor_mode",
            "anchor_score",
            "bonus_score",
            "elo_1v1",
            "elo_2v2",
            "elo_3v3",
            "elo_4v4",
            "wins",
            "losses",
            "ratio",
            "total",
            "last_match_date",
            "coh3stats_url",
        ]
    )
    for e in filtered:
        mm = e.get("mode_max_elos") or {}
        pid_int = int(e["profile_id"])
        writer.writerow(
            [
                e["br_rank"],
                "sim" if e.get("is_custom") else "nao",
                "sim" if e.get("inactive_fallback") else "nao",
                pid_int,
                sanitize_csv_cell(e.get("steam_id", "")),
                sanitize_csv_cell(e.get("original_alias", "")),
                sanitize_csv_cell(e["alias"]),
                e["skill_points"],
                sanitize_csv_cell(e.get("anchor_mode", "")),
                e.get("anchor_score", ""),
                e.get("bonus_score", ""),
                mm.get("1v1", ""),
                mm.get("2v2", ""),
                mm.get("3v3", ""),
                mm.get("4v4", ""),
                e.get("wins", 0),
                e.get("losses", 0),
                e.get("ratio", 0),
                e.get("total", 0),
                e.get("last_match_date", 0),
                f"https://coh3stats.com/players/{pid_int}",
            ]
        )

    atomic_write_text(json_path, json_str + "\n")
    atomic_write_text(csv_path, csv_buf.getvalue())
    return filtered
