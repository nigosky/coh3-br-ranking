"""Cliente endurecido da API Comunitária da Relic para cálculo do Ranking Unificado BR (CoH3 Steam)."""

from __future__ import annotations

import concurrent.futures
import csv
import io
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from anti_carry import (
    ANTI_CARRY_VERSION,
    compute_carry_adjustment,
    extract_player_best_1v1,
)
from roster import (
    CORE_BR_ROSTER,
    build_roster_lookup_maps,
    extract_smurf_owner,
    extract_steam_id,
    format_bound_display_alias,
    is_excluded_suspected_foreign_pid,
)
from security import (
    RateLimiter,
    atomic_write_text,
    get_thread_no_redirect_https_opener,
    read_bounded_http_body,
    redact_secrets,
    sanitize_csv_cell,
    validate_https_endpoint,
)

logger = logging.getLogger("coh3_ranking.relic")

BASE_URL = "https://coh3-api.reliclink.com/community/leaderboard"
ALLOWED_RELIC_HOSTS: frozenset[str] = frozenset({"coh3-api.reliclink.com"})
TITLE_STEAM = "coh3"
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


class RelicRankingClient:
    """Cliente HTTPS endurecido com TLS 1.2+, allowlist estrita, anti-Gzip-bomb e rate-limit."""

    def __init__(self, min_interval_sec: float = 0.06, timeout_sec: float = 15.0) -> None:
        self._rate_limiter = RateLimiter(min_interval_sec=min_interval_sec, floor_sec=0.01)
        self.min_interval_sec = self._rate_limiter.min_interval_sec
        self.timeout_sec = max(2.0, float(timeout_sec))

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
        opener = get_thread_no_redirect_https_opener()
        headers = {
            "User-Agent": "CoH3-BR-Ranking-Unificado/1.0",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

        last_err: Exception | None = None
        for attempt in range(retries):
            self._rate_limiter.wait()
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=self.timeout_sec) as resp:
                    raw_bytes = read_bounded_http_body(resp, max_bytes)
                payload = json.loads(raw_bytes.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Formato JSON inesperado retornado pela API da Relic.")
                return payload
            except urllib.error.HTTPError as exc:
                code = exc.code
                last_err = exc
                try:
                    exc.close()
                except Exception:
                    pass
                if code in (400, 401, 403, 404):
                    break
                wait = (2**attempt) * 0.6
                logger.warning(
                    "HTTP %d na API Relic (%s) tentativa %d/%d; aguardando %.1fs",
                    code,
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
        is_pinned = bool(r_entry.get("pin_alias"))
        disp_alias = format_bound_display_alias(
            cur_alias,
            orig_alias,
            r_entry.get("known_aliases"),
            pin_alias=is_pinned,
        )
        smurf_owner = extract_smurf_owner(
            orig_alias or disp_alias,
            pin_alias=is_pinned,
            explicit_owner=str(r_entry.get("smurf_of") or ""),
        )

        results_by_pid[pid] = {
            "profile_id": pid,
            "alias": disp_alias,
            "current_alias": cur_alias,
            "original_alias": orig_alias or cur_alias,
            "steam_id": steam_id or str(r_entry.get("steam_id") or ""),
            "smurf_of": smurf_owner,
            "is_smurf": bool(smurf_owner),
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
        elo = max(0, int(round(st.get("rating", 0))))
        wins = max(0, int(st.get("wins", 0)))
        losses = max(0, int(st.get("losses", 0)))
        if elo <= 0 and (wins + losses) <= 0:
            continue
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
        target_dict = p["modes"] if (rank_val > 0 and elo > 0) else p["inactive_modes"]
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


def merge_catalog_with_custom_players(
    base_players: list[dict[str, Any]],
    custom_players: list[dict[str, Any]],
    relic: RelicRankingClient,
) -> list[dict[str, Any]]:
    """Consulta getPersonalStat por SteamID64 e aplica apelidos preferenciais (contas Smurf mantidas separadas, sem merge)."""
    effective_custom = custom_players if custom_players else list(CORE_BR_ROSTER)
    protected_pids = {
        int(cp.get("profile_id", 0) or 0)
        for cp in effective_custom
        if isinstance(cp, dict) and int(cp.get("profile_id", 0) or 0) > 0
    }
    by_pid: dict[int, dict[str, Any]] = {
        int(p["profile_id"]): dict(p)
        for p in base_players
        if not is_excluded_suspected_foreign_pid(int(p["profile_id"]), protected_pids=protected_pids)
    }
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
            existing["smurf_of"] = str(cp_stat.get("smurf_of") or "")
            existing["is_smurf"] = bool(cp_stat.get("is_smurf") or cp_stat.get("smurf_of"))
            existing.setdefault("inactive_modes", {}).update(cp_stat.get("inactive_modes", {}))
            existing.setdefault("modes", {}).update(cp_stat.get("modes", {}))
            if cp_stat.get("max_elo", 0) > existing.get("max_elo", 0):
                existing["max_elo"] = cp_stat["max_elo"]
                existing["best_mode"] = cp_stat.get("best_mode", "")
            if cp_stat.get("last_match_date", 0) > existing.get("last_match_date", 0):
                existing["last_match_date"] = cp_stat["last_match_date"]
        else:
            by_pid[pid] = cp_stat

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
            is_pin = bool(r_entry.get("pin_alias") or cp.get("pin_alias"))
            cp["original_alias"] = orig
            cp["alias"] = format_bound_display_alias(
                str(resolved.get("current_alias") or resolved["alias"]),
                orig,
                r_entry.get("known_aliases") or cp.get("known_aliases"),
                pin_alias=is_pin,
            )
            smurf_owner = extract_smurf_owner(
                orig,
                pin_alias=is_pin,
                explicit_owner=str(r_entry.get("smurf_of") or cp.get("smurf_of") or ""),
            )
            if smurf_owner:
                cp["smurf_of"] = smurf_owner
            else:
                cp.pop("smurf_of", None)

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
    carry_context: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Calcula o Ranking Unificado BR em Skill Points combinando todos os modos (1v1, 2v2, 3v3, 4v4).

    Metodologia:
      1. Para cada modo m em {1v1, 2v2, 3v3, 4v4}, considera filas ativas e inativas com
         (wins + losses) >= min_games e seleciona o maior ELO M_m entre as 4 facções.
      2. Quando `carry_context` é fornecido (Sistema Anti-Carry compartilhado com Battlegroups),
         aplica deflação objetiva apenas em filas de equipe (2v2/3v3/4v4) infladas por parceiros
         ou smurfs muito mais fortes — mantendo 1v1 sempre 100% intacto e protegendo carregadores
         comprovados e jogadores com paridade solo no 1v1.
      3. Calcula o score ponderado sobre o excedente: P_m = base_elo + (M_m_eff - base_elo) * w_m,
         com w_1v1=2.0, w_2v2=1.5, w_3v3=1.0, w_4v4=1.0.
      4. Skill Points = max_m(P_m) + bonus_rate * sum_{j != melhor} max(0, P_j - base_elo).
      5. Jogadores 100% inativos (nenhuma fila ativa na Relic) recebem `inactive_fallback=True`
         para destaque visual abaixo dos jogadores ativos.
    """
    weights = mode_weights or UNIFIED_MODE_WEIGHTS
    min_games_req = max(1, int(min_games))
    custom_set = {int(x) for x in (custom_ids or set())}
    custom_sid_set = {str(s) for s in (custom_steam_ids or set()) if s}
    carry_lookup = carry_context or {}
    entries: list[dict[str, Any]] = []

    for p in players:
        pid = int(p["profile_id"])
        sid = str(p.get("steam_id") or "")
        is_custom = pid in custom_set or (bool(sid) and sid in custom_sid_set) or bool(p.get("is_custom"))
        if only_custom and not is_custom:
            continue
        if not is_custom and is_excluded_suspected_foreign_pid(pid):
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
        raw_mode_weighted_scores: dict[str, float] = {}

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
                p_m_raw = max(0.0, float(base_elo) + (float(best_elo) - float(base_elo)) * w_m)
                mode_max_elos[mode] = best_elo
                mode_best_factions[mode] = best_fac
                raw_mode_weighted_scores[mode] = round(p_m_raw, 2)

        if not mode_max_elos:
            continue

        # Sistema Anti-Carry compartilhado (atua apenas em modos de equipe 2v2/3v3/4v4; 1v1 sempre intacto)
        cc = carry_lookup.get(pid)
        best_1v1 = int(mode_max_elos.get("1v1") or extract_player_best_1v1(p, min_games=8))
        mode_effective_elos: dict[str, float] = {}
        mode_weighted_scores: dict[str, float] = {}
        min_carry_mult = 1.0
        max_carry_risk = 0.0

        for mode, raw_elo in mode_max_elos.items():
            w_m = float(weights.get(mode, 1.0))
            if mode == "1v1" or not cc:
                eff_elo_m = float(raw_elo)
            else:
                c_mult, c_defl, c_risk = compute_carry_adjustment(
                    mode=mode,
                    eff_elo=float(raw_elo),
                    best_1v1_elo=best_1v1,
                    team_n=int(cc.get("team_n") or 0),
                    avg_ally_elo_gap=float(cc.get("avg_ally_elo_gap") or 0.0),
                    avg_dmg_vs_ally=float(cc.get("avg_dmg_vs_ally") or 1.0),
                    avg_kills_vs_ally=float(cc.get("avg_kills_vs_ally") or 1.0),
                    carried_match_rate=float(cc.get("carried_match_rate") or 0.0),
                    arr_profile=cc.get("arr_profile"),
                )
                min_carry_mult = min(min_carry_mult, c_mult)
                max_carry_risk = max(max_carry_risk, c_risk)
                eff_elo_m = float(raw_elo) - c_defl
                if raw_elo >= base_elo:
                    eff_elo_m = max(float(base_elo), eff_elo_m)
            mode_effective_elos[mode] = round(eff_elo_m, 1)
            p_m = max(0.0, float(base_elo) + (eff_elo_m - float(base_elo)) * w_m)
            mode_weighted_scores[mode] = round(p_m, 2)

        raw_anchor_mode = max(
            raw_mode_weighted_scores.keys(),
            key=lambda m: (
                raw_mode_weighted_scores[m],
                float(weights.get(m, 1.0)),
                mode_max_elos[m],
            ),
        )
        raw_skill_points = round(
            raw_mode_weighted_scores[raw_anchor_mode]
            + float(bonus_rate)
            * sum(
                max(0.0, score - float(base_elo))
                for m, score in raw_mode_weighted_scores.items()
                if m != raw_anchor_mode
            ),
            1,
        )

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
        carry_sp_delta = round(skill_points - raw_skill_points, 1)

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
        arr_prof_cc = (cc or {}).get("arr_profile") if isinstance(cc, dict) else None

        smurf_of = str(p.get("smurf_of") or "")
        country_code = str(p.get("country") or "br").lower()
        entries.append(
            {
                "profile_id": pid,
                "alias": str(p["alias"]),
                "current_alias": str(p.get("current_alias") or p["alias"]),
                "original_alias": str(p.get("original_alias") or p["alias"]),
                "steam_id": sid,
                "smurf_of": smurf_of,
                "is_smurf": bool(p.get("is_smurf") or smurf_of),
                "country": country_code,
                "is_expat": bool(country_code and country_code != "br"),
                "skill_points": skill_points,
                "raw_skill_points": raw_skill_points,
                "carry_mult": round(min_carry_mult, 4),
                "carry_risk": round(max_carry_risk, 3),
                "carry_sp_delta": carry_sp_delta,
                "carry_2v2_share": round(100.0 * float((arr_prof_cc or {}).get("carry_2v2_share") or 0.0), 1),
                "avg_ally_elo_gap": round(float((cc or {}).get("avg_ally_elo_gap") or 0.0), 1),
                "anchor_mode": anchor_mode,
                "anchor_score": round(anchor_score, 1),
                "bonus_score": round(bonus_score, 1),
                "mode_max_elos": mode_max_elos,
                "mode_effective_elos": mode_effective_elos,
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
    smurf_cnt = sum(1 for e in filtered if e.get("smurf_of") or e.get("is_smurf"))
    anticarry_cnt = sum(1 for e in filtered if float(e.get("carry_sp_delta") or 0.0) < 0.0)

    json_str = json.dumps(
        {
            "updated_at": int(time.time()),
            "methodology": "unified_skill_points_anchor_plus_secondary_bonus_no_smurf_merge",
            "anti_carry_version": ANTI_CARRY_VERSION,
            "anti_carry_adjusted_players": anticarry_cnt,
            "games_scope": "lifetime_pvp_since_coh3_launch_all_16_queues_per_account",
            "formula": "max_m(P_m) + 0.15 * sum_{j != anchor} max(0, P_j - 1000); P_m = 1000 + (M_m_eff - 1000) * w_m",
            "mode_weights": dict(UNIFIED_MODE_WEIGHTS),
            "base_elo": UNIFIED_BASE_ELO,
            "secondary_bonus_rate": UNIFIED_SECONDARY_BONUS_RATE,
            "min_games_per_queue": int(min_games),
            "total_players": len(filtered),
            "active_players": active_cnt,
            "inactive_players": inactive_cnt,
            "smurf_accounts_count": smurf_cnt,
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
            "smurf_of",
            "profile_id",
            "steam_id",
            "original_alias",
            "display_alias",
            "skill_points",
            "raw_skill_points",
            "carry_mult",
            "carry_sp_delta",
            "carry_2v2_pct",
            "ally_elo_gap",
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
                sanitize_csv_cell(e.get("smurf_of", "")),
                pid_int,
                sanitize_csv_cell(e.get("steam_id", "")),
                sanitize_csv_cell(e.get("original_alias", "")),
                sanitize_csv_cell(e["alias"]),
                e["skill_points"],
                e.get("raw_skill_points", e["skill_points"]),
                e.get("carry_mult", 1.0),
                e.get("carry_sp_delta", 0.0),
                e.get("carry_2v2_share", 0.0),
                e.get("avg_ally_elo_gap", 0.0),
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
