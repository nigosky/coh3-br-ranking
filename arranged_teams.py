"""Motor do Ranking Unificado de Times Brasileiros 2v2 Arranjados via COH3 Stats Open Data (storage.coh3stats.com)."""

from __future__ import annotations

import concurrent.futures
import csv
import io
import json
import logging
import multiprocessing
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relic_ranking import (
    BASE_URL as RELIC_BASE_URL,
    LEADERBOARD_ID_TO_MODE_FACTION,
    TITLE_STEAM,
)
from roster import (
    CORE_BR_ROSTER,
    MANUAL_OTHER_BR_ALIASES,
    build_manual_other_br_lookup_maps,
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
    redact_secrets,
    sanitize_csv_cell,
    strip_unsafe_chars,
    validate_https_endpoint,
)

logger = logging.getLogger("coh3_ranking.arranged_teams")

COH3STATS_STORAGE_BASE_URL = "https://storage.coh3stats.com"
ALLOWED_COH3STATS_STORAGE_HOSTS: frozenset[str] = frozenset({"storage.coh3stats.com"})
MAX_DAILY_WIRE_BYTES = 60 * 1024 * 1024           # 60 MB comprimido no fio
MAX_DAILY_UNCOMPRESSED_BYTES = 350 * 1024 * 1024  # 350 MB descomprimido (anti-Gzip-bomb; suporta pico 2023)
MAX_RELIC_HISTORY_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB para histórico recente individual Relic

DEFAULT_INACTIVE_DAYS = 30
DEFAULT_BOOTSTRAP_DAYS = 1460
MIN_GAMES_FOR_ARRANGED_TEAM = 3
MIN_TOTAL_GAMES_ARRANGED_TEAM = 10
MAX_ARRANGED_TEAMS_TOP_N = 32
MAX_CONSECUTIVE_HISTORICAL_404_DAYS = 14
INITIAL_TEAM_ELO = 1200.0
ELO_K_FACTOR = 32.0

VALID_SIDES = ("axis", "allies")
AUTOMATCH_2V2_TYPES: frozenset[int] = frozenset({2, 21})  # 2V2_Ranked, 2V2_Unranked
AXIS_RACE_IDS: frozenset[int] = frozenset({137123, 198437})  # Wehrmacht, DAK
ALLIES_RACE_IDS: frozenset[int] = frozenset({129494, 203852, 197345})  # USF, UKF


def _normalize_unix_seconds(raw_ts: Any) -> int:
    """Converte timestamp Unix (segundos ou milissegundos) para inteiro em segundos."""
    try:
        val = int(float(raw_ts or 0))
    except (TypeError, ValueError):
        return 0
    if val <= 0:
        return 0
    if val > 10**12:
        val //= 1000
    return val


def _extract_veterano_pids(custom_players: list[dict[str, Any]] | None = None) -> set[int]:
    pids: set[int] = set()
    for src in (CORE_BR_ROSTER, custom_players or []):
        for item in src:
            pid = int(item.get("profile_id", 0) or 0)
            if pid > 0:
                pids.add(pid)
    return pids


def _extract_manual_other_br_pids() -> frozenset[int]:
    return frozenset(
        int(m.get("profile_id", 0) or 0)
        for m in MANUAL_OTHER_BR_ALIASES
        if isinstance(m, dict) and int(m.get("profile_id", 0) or 0) > 0
    )


def is_brazilian_player(
    player: dict[str, Any],
    veterano_pids: set[int] | frozenset[int],
    manual_br_pids: set[int] | frozenset[int] | None = None,
) -> bool:
    """Retorna True se o jogador for Veterano, manual_other_br ou bandeira BR sem bloqueio de VPN estrangeira."""
    pid = int(player.get("profile_id", 0) or 0)
    if pid > 0 and pid in veterano_pids:
        return True
    if pid > 0:
        manual_ids = manual_br_pids if manual_br_pids is not None else _extract_manual_other_br_pids()
        if pid in manual_ids:
            return True
        if is_excluded_suspected_foreign_pid(pid):
            return False
    country = str(player.get("country") or "").strip().lower()
    return country == "br"


def is_brazilian_2v2_team(
    raw_team: dict[str, Any],
    veterano_pids: set[int] | frozenset[int],
    manual_br_pids: set[int] | frozenset[int] | None = None,
) -> bool:
    """Retorna True somente se o time for 2v2 e AMBOS os jogadores forem brasileiros/Veteranos."""
    if str(raw_team.get("type") or "").strip().lower() != "2v2":
        return False
    players = raw_team.get("players")
    if not isinstance(players, list) or len(players) != 2:
        return False
    pid0 = int(players[0].get("profile_id", 0) or 0)
    pid1 = int(players[1].get("profile_id", 0) or 0)
    if pid0 <= 0 or pid1 <= 0 or pid0 == pid1:
        return False
    return is_brazilian_player(players[0], veterano_pids, manual_br_pids) and is_brazilian_player(
        players[1], veterano_pids, manual_br_pids
    )


def _race_id_to_side(race_id: int) -> str:
    if race_id in AXIS_RACE_IDS:
        return "axis"
    if race_id in ALLIES_RACE_IDS:
        return "allies"
    return ""


def _read_bounded_daily_dump(resp: Any) -> bytes:
    """Lê dump diário JSON do Open Data com limites rígidos de fio e descompressão Gzip."""
    encoding = ""
    if hasattr(resp, "headers") and resp.headers is not None:
        encoding = str(resp.headers.get("Content-Encoding", "")).strip().lower()

    is_gzip = encoding in ("gzip", "x-gzip")
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS) if is_gzip else None

    wire_total = 0
    out_total = 0
    chunks: list[bytes] = []

    while True:
        block = resp.read(65_536)
        if not block:
            break
        wire_total += len(block)
        if wire_total > MAX_DAILY_WIRE_BYTES:
            raise RuntimeError(f"Dump diário excedeu o limite seguro no fio ({MAX_DAILY_WIRE_BYTES} bytes).")

        if decomp is not None:
            part = decomp.decompress(block, MAX_DAILY_UNCOMPRESSED_BYTES + 1 - out_total)
            if decomp.unconsumed_tail:
                raise RuntimeError("Dump diário Gzip excedeu o limite máximo descomprimido.")
            out_total += len(part)
            if out_total > MAX_DAILY_UNCOMPRESSED_BYTES:
                raise RuntimeError("Dump diário Gzip excedeu o limite máximo descomprimido.")
            if part:
                chunks.append(part)
        else:
            out_total += len(block)
            if out_total > MAX_DAILY_UNCOMPRESSED_BYTES:
                raise RuntimeError("Dump diário excedeu o limite máximo de tamanho.")
            chunks.append(block)

    if decomp is not None:
        tail = decomp.flush(MAX_DAILY_UNCOMPRESSED_BYTES + 1 - out_total)
        out_total += len(tail)
        if out_total > MAX_DAILY_UNCOMPRESSED_BYTES:
            raise RuntimeError("Dump diário Gzip excedeu o limite máximo descomprimido.")
        if tail:
            chunks.append(tail)

    return b"".join(chunks)


def extract_brazilian_2v2_matches_from_dump(
    matches: list[dict[str, Any]],
    veterano_pids: set[int],
) -> list[dict[str, Any]]:
    """Extrai partidas 2v2 automatch Steam em que ao menos uma dupla tem AMBOS jogadores BR/Veteranos."""
    extracted: list[dict[str, Any]] = []
    manual_br_pids = _extract_manual_other_br_pids()

    for m in matches:
        if not isinstance(m, dict):
            continue
        matchtype_id = int(m.get("matchtype_id", -1) or -1)
        if matchtype_id not in AUTOMATCH_2V2_TYPES:
            continue

        platform = str(m.get("platform") or "").strip().lower()
        if platform and platform != "steam":
            continue

        match_id = int(m.get("id", 0) or 0)
        if match_id <= 0:
            continue

        completion_ts = _normalize_unix_seconds(m.get("completiontime") or m.get("startgametime"))
        if completion_ts <= 0:
            continue

        reports = m.get("matchhistoryreportresults")
        if not isinstance(reports, list) or len(reports) != 4:
            continue

        by_team: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for rep in reports:
            if not isinstance(rep, dict):
                continue
            tid = int(rep.get("teamid", -1) if rep.get("teamid") is not None else -1)
            if tid in (0, 1):
                by_team[tid].append(rep)

        if len(by_team) != 2 or len(by_team[0]) != 2 or len(by_team[1]) != 2:
            continue

        top_mhm_by_pid: dict[int, dict[str, Any]] = {}
        raw_top_mhm = m.get("matchhistorymember")
        if isinstance(raw_top_mhm, list):
            for item in raw_top_mhm:
                if isinstance(item, dict):
                    mpid = int(item.get("profile_id", 0) or 0)
                    if mpid > 0:
                        top_mhm_by_pid[mpid] = item

        top_prof_by_pid: dict[int, dict[str, Any]] = {}
        raw_top_profs = m.get("profiles")
        if isinstance(raw_top_profs, list):
            for item in raw_top_profs:
                if isinstance(item, dict):
                    ppid = int(item.get("profile_id", 0) or 0)
                    if ppid > 0:
                        top_prof_by_pid[ppid] = item

        team_info: dict[int, dict[str, Any]] = {}
        valid_match = True
        for tid in (0, 1):
            reps = by_team[tid]
            sides = {_race_id_to_side(int(r.get("race_id", 0) or 0)) for r in reps}
            if len(sides) != 1 or "" in sides:
                valid_match = False
                break
            side = next(iter(sides))

            outcomes: set[int] = set()
            players: list[dict[str, Any]] = []
            ratings: list[float] = []

            for r in reps:
                pid = int(r.get("profile_id", 0) or 0)
                if pid <= 0:
                    valid_match = False
                    break
                res_raw = r.get("resulttype")
                mhm = r.get("matchhistorymember") if isinstance(r.get("matchhistorymember"), dict) else top_mhm_by_pid.get(pid, {})
                if res_raw not in (0, 1):
                    res_raw = mhm.get("outcome")
                if res_raw in (0, 1):
                    outcomes.add(int(res_raw))

                prof = r.get("profile") if isinstance(r.get("profile"), dict) else top_prof_by_pid.get(pid, {})
                alias = strip_unsafe_chars(prof.get("alias"))[:80] or f"Jogador {pid}"
                raw_country = str(prof.get("country") or "").strip().lower()
                country = "".join(ch for ch in raw_country if "a" <= ch <= "z")[:2]
                steam_id = extract_steam_id(prof.get("name"))

                old_r = mhm.get("oldrating")
                if old_r is None or float(old_r or 0) <= 0:
                    old_r = mhm.get("newrating")
                try:
                    rv = float(old_r or 0)
                    if rv > 0:
                        ratings.append(rv)
                except (TypeError, ValueError):
                    pass

                players.append(
                    {
                        "profile_id": pid,
                        "alias": alias,
                        "country": country,
                        "steam_id": steam_id,
                    }
                )

            if not valid_match or len(players) != 2 or players[0]["profile_id"] == players[1]["profile_id"]:
                valid_match = False
                break
            if len(outcomes) != 1:
                valid_match = False
                break

            avg_rating = sum(ratings) / len(ratings) if ratings else INITIAL_TEAM_ELO
            team_info[tid] = {
                "side": side,
                "won": next(iter(outcomes)) == 1,
                "players": players,
                "avg_rating": avg_rating,
            }

        if not valid_match:
            continue
        if team_info[0]["side"] == team_info[1]["side"]:
            continue
        if team_info[0]["won"] == team_info[1]["won"]:
            continue

        for tid in (0, 1):
            t_cur = team_info[tid]
            t_opp = team_info[1 - tid]
            p0, p1 = t_cur["players"][0], t_cur["players"][1]
            if not (
                is_brazilian_player(p0, veterano_pids, manual_br_pids)
                and is_brazilian_player(p1, veterano_pids, manual_br_pids)
            ):
                continue

            if p0["profile_id"] > p1["profile_id"]:
                p0, p1 = p1, p0
            pair_key = f"{p0['profile_id']}_{p1['profile_id']}"

            extracted.append(
                {
                    "match_id": match_id,
                    "side": t_cur["side"],
                    "pair_key": pair_key,
                    "p1_id": int(p0["profile_id"]),
                    "p1_alias": p0["alias"],
                    "p1_country": p0["country"] or "br",
                    "p1_steam_id": p0["steam_id"],
                    "p2_id": int(p1["profile_id"]),
                    "p2_alias": p1["alias"],
                    "p2_country": p1["country"] or "br",
                    "p2_steam_id": p1["steam_id"],
                    "won": 1 if t_cur["won"] else 0,
                    "enemy_elo": round(float(t_opp["avg_rating"]), 1),
                    "completion_ts": completion_ts,
                }
            )

    return extracted


class Coh3StatsOpenDataClient:
    """Cliente HTTPS endurecido para o repositório público COH3 Stats Open Data (storage.coh3stats.com)."""

    def __init__(self, min_interval_sec: float = 0.10, timeout_sec: float = 35.0) -> None:
        self._rate_limiter = RateLimiter(min_interval_sec=min_interval_sec, floor_sec=0.02)
        self.min_interval_sec = self._rate_limiter.min_interval_sec
        self.timeout_sec = max(5.0, float(timeout_sec))

    def fetch_day_matches(
        self,
        day_ts: int,
        retries: int = 2,
    ) -> list[dict[str, Any]] | None:
        """Baixa https://storage.coh3stats.com/matches/matches-{day_ts}.json (retorna None em HTTP 404)."""
        url = f"{COH3STATS_STORAGE_BASE_URL}/matches/matches-{int(day_ts)}.json"
        validate_https_endpoint(
            url,
            ALLOWED_COH3STATS_STORAGE_HOSTS,
            path_prefix="/matches/",
        )
        opener = get_thread_no_redirect_https_opener()
        headers = {
            "User-Agent": "CoH3-BR-Ranking-Unificado/1.0 (COH3 Stats Open Data)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

        last_err: Exception | None = None
        for attempt in range(retries):
            self._rate_limiter.wait()
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=self.timeout_sec) as resp:
                    raw_bytes = _read_bounded_daily_dump(resp)
                payload = json.loads(raw_bytes.decode("utf-8"))
                del raw_bytes
                matches = payload.get("matches") if isinstance(payload, dict) else None
                if not isinstance(matches, list):
                    raise ValueError("Estrutura JSON inesperada no dump diário do COH3 Stats Open Data.")
                return matches
            except urllib.error.HTTPError as exc:
                code = exc.code
                last_err = exc
                try:
                    exc.close()
                except Exception:
                    pass
                if code == 404:
                    return None
                if code in (400, 401, 403):
                    break
                wait = (2**attempt) * 1.0
                logger.warning(
                    "HTTP %d no COH3 Stats Open Data (dia %d) tentativa %d/%d; aguardando %.1fs",
                    code,
                    day_ts,
                    attempt + 1,
                    retries,
                    wait,
                )
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError, RuntimeError) as exc:
                last_err = exc
                wait = (2**attempt) * 1.0
                logger.warning(
                    "Falha ao ler dump diário COH3 Stats Open Data (dia %d) tentativa %d/%d: %s",
                    day_ts,
                    attempt + 1,
                    retries,
                    redact_secrets(exc),
                )
                time.sleep(wait)

        raise RuntimeError(f"Erro ao consultar COH3 Stats Open Data ({url}): {redact_secrets(last_err)}")

    def fetch_day_brazilian_2v2_matches(
        self,
        day_ts: int,
        veterano_pids: set[int],
        retries: int = 2,
    ) -> tuple[str, int, list[dict[str, Any]]]:
        """
        Baixa https://storage.coh3stats.com/matches/matches-{day_ts}.json e retorna:
        (status: 'ok' | 'not_found', total_matches_no_dia, partidas_2v2_br).
        """
        matches = self.fetch_day_matches(day_ts=day_ts, retries=retries)
        if matches is None:
            return "not_found", 0, []
        total_in_day = len(matches)
        br_rows = extract_brazilian_2v2_matches_from_dump(matches, veterano_pids)
        return "ok", total_in_day, br_rows


_WORKER_OPEN_DATA_CLIENT: Coh3StatsOpenDataClient | None = None


def _fetch_day_brazilian_2v2_worker(
    day_ts: int,
    br_pids: frozenset[int],
    timeout_sec: float = 35.0,
) -> tuple[str, int, list[dict[str, Any]]]:
    """Worker isolado em processo filho para baixar e parsear dumps diários sem contenção de GIL."""
    global _WORKER_OPEN_DATA_CLIENT
    if _WORKER_OPEN_DATA_CLIENT is None:
        _WORKER_OPEN_DATA_CLIENT = Coh3StatsOpenDataClient(min_interval_sec=0.05, timeout_sec=timeout_sec)
    return _WORKER_OPEN_DATA_CLIENT.fetch_day_brazilian_2v2_matches(day_ts, set(br_pids))


def sync_brazilian_2v2_arranged_matches(
    client: Coh3StatsOpenDataClient,
    db: Any,
    custom_players: list[dict[str, Any]] | None = None,
    bootstrap_days: int = DEFAULT_BOOTSTRAP_DAYS,
    max_scan_seconds: float = 240.0,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Sincroniza incrementalmente todo o histórico disponível do COH3 Stats Open Data no SQLite."""
    veterano_pids = _extract_veterano_pids(custom_players)
    all_br_pids = veterano_pids | set(_extract_manual_other_br_pids())
    all_br_pids_frozen = frozenset(all_br_pids)

    now_dt = datetime.fromtimestamp(int(now_ts if now_ts is not None else time.time()), tz=timezone.utc)
    today_midnight = int(datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc).timestamp())

    floor_ts = 0
    if hasattr(db, "get_state"):
        try:
            floor_ts = int(db.get_state("arranged_history_floor_ts") or 0)
        except (TypeError, ValueError):
            floor_ts = 0

    processed_days = db.get_processed_arranged_days()
    # Varre do dia mais recente para o mais antigo para priorizar dias recentes e completar o histórico
    pending_days: list[tuple[int, int]] = []
    for offset in range(1, max(1, int(bootstrap_days)) + 1):
        day_ts = today_midnight - offset * 86400
        if floor_ts > 0 and day_ts <= floor_ts:
            break
        if day_ts not in processed_days:
            pending_days.append((offset, day_ts))

    use_multiprocess = type(client) is Coh3StatsOpenDataClient and len(pending_days) >= 8
    max_workers = max(2, min(4, os.cpu_count() or 4)) if use_multiprocess else min(4, max(1, len(pending_days)))
    batch_size = (max_workers * 4) if use_multiprocess else 4

    if pending_days:
        logger.info(
            "COH3 Stats Open Data: sincronizando %d dia(s) pendente(s) de partidas 2v2 (histórico completo; modo=%s, workers=%d, orçamento=%.0fs)...",
            len(pending_days),
            "multiprocesso" if use_multiprocess else "threads",
            max_workers,
            max_scan_seconds,
        )

    started_at = time.monotonic()
    synced_days = 0
    total_new_rows = 0
    errors_count = 0
    consecutive_hist_404 = 0
    reached_archive_floor = False
    last_progress_log_ts = started_at

    def _run_batches(executor: concurrent.futures.Executor) -> None:
        nonlocal synced_days, total_new_rows, errors_count, consecutive_hist_404, reached_archive_floor, last_progress_log_ts
        timeout_sec = float(getattr(client, "timeout_sec", 35.0))
        for i in range(0, len(pending_days), batch_size):
            if i > 0 and (time.monotonic() - started_at) >= max_scan_seconds:
                logger.info(
                    "Orçamento de tempo (%.0fs) atingido no Open Data (%d/%d dias sincronizados; restante continua no próximo ciclo).",
                    max_scan_seconds,
                    synced_days,
                    len(pending_days),
                )
                break

            batch = pending_days[i : i + batch_size]
            batch_results: dict[int, tuple[str, int, list[dict[str, Any]]]] = {}
            if use_multiprocess:
                fut_map = {
                    executor.submit(_fetch_day_brazilian_2v2_worker, day_ts, all_br_pids_frozen, timeout_sec): (offset, day_ts)
                    for offset, day_ts in batch
                }
            else:
                fut_map = {
                    executor.submit(client.fetch_day_brazilian_2v2_matches, day_ts, all_br_pids): (offset, day_ts)
                    for offset, day_ts in batch
                }

            for fut in concurrent.futures.as_completed(fut_map):
                offset, day_ts = fut_map[fut]
                try:
                    batch_results[offset] = fut.result()
                except Exception as exc:
                    errors_count += 1
                    logger.warning("Erro ao processar dia %d do COH3 Stats Open Data: %s", day_ts, redact_secrets(exc))

            oldest_ok_ts = 0
            for offset, day_ts in batch:
                res = batch_results.get(offset)
                if res is None:
                    continue
                status, total_in_day, br_rows = res
                if status == "ok":
                    db.save_arranged_2v2_matches(br_rows)
                    db.mark_arranged_day_processed(day_ts, matches_total=total_in_day, br_2v2_matches=len(br_rows))
                    synced_days += 1
                    total_new_rows += len(br_rows)
                    consecutive_hist_404 = 0
                    oldest_ok_ts = day_ts
                elif offset > 2:
                    # Dia histórico (> 48h atrás) ausente no bucket: marca como verificado para não repetir
                    db.mark_arranged_day_processed(day_ts, matches_total=0, br_2v2_matches=0)
                    if offset > 30:
                        consecutive_hist_404 += 1
                        if consecutive_hist_404 >= MAX_CONSECUTIVE_HISTORICAL_404_DAYS:
                            if hasattr(db, "set_state"):
                                db.set_state("arranged_history_floor_ts", str(day_ts))
                            logger.info(
                                "Início real do arquivo COH3 Stats Open Data atingido em %s (UTC); backfill histórico concluído.",
                                datetime.fromtimestamp(day_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                            )
                            reached_archive_floor = True
                            break
            if reached_archive_floor:
                break

            now_m = time.monotonic()
            if oldest_ok_ts > 0 and (now_m - last_progress_log_ts) >= 45.0:
                last_progress_log_ts = now_m
                logger.info(
                    "Progresso Open Data: %d/%d dias sincronizados (+%d partidas 2v2 BR, alcançado %s UTC em %.0fs).",
                    synced_days,
                    len(pending_days),
                    total_new_rows,
                    datetime.fromtimestamp(oldest_ok_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
                    now_m - started_at,
                )

    if pending_days:
        if use_multiprocess:
            mp_ctx = multiprocessing.get_context("spawn")
            with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers, mp_context=mp_ctx) as pool:
                _run_batches(pool)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                _run_batches(pool)

    all_rows: list[dict[str, Any]] = db.load_all_arranged_2v2_matches()
    if pending_days and synced_days == 0 and errors_count > 0 and not all_rows:
        raise RuntimeError("Não foi possível baixar nenhum dump diário do COH3 Stats Open Data.")

    logger.info(
        "COH3 Stats Open Data pronto: %d dia(s) sincronizado(s) neste ciclo (+%d partidas 2v2 BR; total acumulado no cache: %d).",
        synced_days,
        total_new_rows,
        len(all_rows),
    )
    return all_rows


def sync_recent_brazilian_2v2_matches_from_relic(
    relic_client: Any,
    db: Any,
    custom_players: list[dict[str, Any]] | None = None,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    now_ts: int | None = None,
    max_live_seconds: float = 60.0,
) -> int:
    """
    Complementa o cache SQLite de partidas 2v2 BR em tempo real via API oficial da Relic.

    Elimina o atraso de 24-36h do arquivo diário UTC do COH3 Stats Open Data (especialmente
    partidas brasileiras jogadas à noite >= 21:00 BRT, que caem no dia UTC seguinte e só
    entram no Open Data após o encerramento daquele dia UTC):
      1. Pré-filtra todos os jogadores BR candidatos (Veteranos + ativos nos últimos 60d +
         duplas históricas com >=7 jogos) via lotes rápidos de `getPersonalStat` (10 PIDs/req);
      2. Consulta `getRecentMatchHistoryByProfileId` apenas para quem teve atividade 2v2 após
         o último dump do Open Data (com margem de 2h) e mais recente que o cache SQLite;
      3. Extrai todas as partidas 2v2 BR (com `enemy_elo`, `won`, `side`, `completion_ts`
         idênticos ao Open Data) e persiste idempotentemente via `INSERT OR IGNORE`.
    """
    started_at = time.monotonic()
    current_ts = int(now_ts if now_ts is not None else time.time())
    veterano_pids = _extract_veterano_pids(custom_players)
    manual_br_pids = set(_extract_manual_other_br_pids())
    all_br_pids = veterano_pids | manual_br_pids

    recent_since_ts = current_ts - max(int(inactive_days) + 30, 60) * 86400
    db_candidates, player_max_ts = db.get_arranged_live_sync_candidates(
        recent_since_ts=recent_since_ts,
        min_pair_games=7,
    )
    candidate_pids = sorted(pid for pid in (all_br_pids | db_candidates) if pid > 0)
    if not candidate_pids:
        return 0

    latest_day_ts = int(db.get_latest_arranged_day_with_matches() or 0)
    open_data_covered_until = (latest_day_ts + 86400) if latest_day_ts > 0 else (current_ts - 7 * 86400)
    live_window_start = max(current_ts - 14 * 86400, open_data_covered_until - 7200)

    # Passo 1: pré-filtro rápido em lotes de 10 contas via getPersonalStat (~1-3s para ~400 jogadores BR)
    batches = [candidate_pids[i : i + 10] for i in range(0, len(candidate_pids), 10)]
    live_2v2_lmd: dict[int, int] = {}

    def _fetch_personal_2v2_batch(batch_pids: list[int]) -> dict[int, int]:
        encoded_ids = urllib.parse.quote("[" + ",".join(str(p) for p in batch_pids) + "]")
        url = f"{RELIC_BASE_URL}/getPersonalStat?title={TITLE_STEAM}&profile_ids={encoded_ids}"
        data = relic_client._get_json(url)
        sg_to_pid: dict[int, int] = {}
        for sg in data.get("statGroups") or []:
            if not isinstance(sg, dict):
                continue
            members = sg.get("members") or []
            if sg.get("type") == 1 and len(members) == 1 and isinstance(members[0], dict):
                pid_v = int(members[0].get("profile_id", 0) or 0)
                if pid_v > 0:
                    sg_to_pid[int(sg["id"])] = pid_v
        out: dict[int, int] = {}
        for st in data.get("leaderboardStats") or []:
            if not isinstance(st, dict):
                continue
            pid_v = sg_to_pid.get(int(st.get("statgroup_id", -1)))
            lb_info = LEADERBOARD_ID_TO_MODE_FACTION.get(int(st.get("leaderboard_id", -1)))
            if pid_v and lb_info and lb_info[0] == "2v2":
                lmd = _normalize_unix_seconds(st.get("lastmatchdate"))
                if lmd > out.get(pid_v, 0):
                    out[pid_v] = lmd
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, max(1, len(batches)))) as pool:
        futs = {pool.submit(_fetch_personal_2v2_batch, bp): bp for bp in batches}
        for fut in concurrent.futures.as_completed(futs):
            try:
                for pid_v, lmd in fut.result().items():
                    if lmd > live_2v2_lmd.get(pid_v, 0):
                        live_2v2_lmd[pid_v] = lmd
            except Exception as exc:
                logger.warning("Falha no pré-filtro 2v2 Relic (lote): %s", redact_secrets(exc))

    pids_to_fetch = sorted(
        pid_v
        for pid_v, lmd in live_2v2_lmd.items()
        if lmd >= live_window_start and lmd > player_max_ts.get(pid_v, 0)
    )
    if not pids_to_fetch:
        logger.info(
            "Relic Live API (2v2 arranjado): %d jogadores BR verificados em %.1fs — nenhuma partida 2v2 nova além do cache.",
            len(candidate_pids),
            time.monotonic() - started_at,
        )
        return 0

    # Passo 2: buscar histórico recente apenas de quem efetivamente jogou 2v2 após o último dump
    def _fetch_recent_2v2_for_pid(pid_v: int) -> list[dict[str, Any]]:
        url = f"{RELIC_BASE_URL}/getRecentMatchHistoryByProfileId?title={TITLE_STEAM}&profile_id={int(pid_v)}"
        try:
            data = relic_client._get_json(url, max_bytes=MAX_RELIC_HISTORY_RESPONSE_BYTES)
        except TypeError:
            data = relic_client._get_json(url)
        profiles = data.get("profiles") or []
        raw_matches = data.get("matchHistoryStats") or []
        enriched = [dict(m, profiles=profiles) for m in raw_matches if isinstance(m, dict)]
        return extract_brazilian_2v2_matches_from_dump(enriched, all_br_pids)

    dedup_rows: dict[tuple[int, str, str], dict[str, Any]] = {}
    fetched_cnt = 0
    max_workers = min(4, max(1, len(pids_to_fetch)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for i in range(0, len(pids_to_fetch), max_workers * 2):
            if i > 0 and (time.monotonic() - started_at) >= max(5.0, float(max_live_seconds)):
                logger.info(
                    "Orçamento de tempo (%.0fs) atingido no Relic Live 2v2 (%d/%d jogadores consultados).",
                    max_live_seconds,
                    fetched_cnt,
                    len(pids_to_fetch),
                )
                break
            chunk = pids_to_fetch[i : i + max_workers * 2]
            futs = {pool.submit(_fetch_recent_2v2_for_pid, pid_v): pid_v for pid_v in chunk}
            for fut in concurrent.futures.as_completed(futs):
                pid_v = futs[fut]
                fetched_cnt += 1
                try:
                    for row in fut.result():
                        key = (int(row["match_id"]), str(row["side"]), str(row["pair_key"]))
                        dedup_rows[key] = row
                except Exception as exc:
                    logger.warning(
                        "Falha ao buscar histórico recente 2v2 Relic (profile_id=%d): %s",
                        pid_v,
                        redact_secrets(exc),
                    )

    inserted = int(db.save_arranged_2v2_matches(list(dedup_rows.values()))) if dedup_rows else 0
    logger.info(
        "Relic Live API (2v2 arranjado): %d jogadores BR verificados -> %d ativos recentes consultados -> +%d nova(s) partida(s) 2v2 BR em tempo real (%.1fs).",
        len(candidate_pids),
        fetched_cnt,
        inserted,
        time.monotonic() - started_at,
    )
    return inserted


def build_side_teams_from_matches(
    match_rows: list[dict[str, Any]],
    min_games: int = MIN_GAMES_FOR_ARRANGED_TEAM,
    k_factor: float = ELO_K_FACTOR,
    initial_elo: float = INITIAL_TEAM_ELO,
    custom_players: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Reconstrói times arranjados 2v2 por lado (axis/allies) em ordem cronológica a partir das partidas,
    mantendo contas Smurf separadas (sem merge), preservando o primeiro nick observado de cada jogador
    para sinalizar mudanças com '*' e aplicando ELO inicial 1200 + atualização Elo partida a partida
    (exigindo >= min_games jogos juntos).
    """
    _ = custom_players
    if not match_rows:
        return []

    norm_rows: list[dict[str, Any]] = []
    seen_match_keys: set[tuple[int, str, str]] = set()
    for r in match_rows:
        p1 = int(r["p1_id"])
        p2 = int(r["p2_id"])
        if p1 <= 0 or p2 <= 0 or p1 == p2:
            continue

        p1_alias = str(r["p1_alias"])
        p1_country = str(r.get("p1_country") or "br").lower()
        p1_sid = str(r.get("p1_steam_id") or "")
        p2_alias = str(r["p2_alias"])
        p2_country = str(r.get("p2_country") or "br").lower()
        p2_sid = str(r.get("p2_steam_id") or "")

        if p1 > p2:
            p1, p2 = p2, p1
            p1_alias, p2_alias = p2_alias, p1_alias
            p1_country, p2_country = p2_country, p1_country
            p1_sid, p2_sid = p2_sid, p1_sid

        side = str(r["side"]).lower()
        pair_key = f"{p1}_{p2}"
        dedup_m = (int(r["match_id"]), pair_key, side)
        if dedup_m in seen_match_keys:
            continue
        seen_match_keys.add(dedup_m)

        norm_rows.append(
            {
                **r,
                "side": side,
                "pair_key": pair_key,
                "p1_id": p1,
                "p1_alias": p1_alias,
                "p1_country": p1_country,
                "p1_steam_id": p1_sid,
                "p2_id": p2,
                "p2_alias": p2_alias,
                "p2_country": p2_country,
                "p2_steam_id": p2_sid,
            }
        )

    if not norm_rows:
        return []

    counts: Counter[tuple[str, str]] = Counter(
        (str(r["pair_key"]), str(r["side"]).lower()) for r in norm_rows
    )
    qualified: set[tuple[str, str]] = {
        key for key, cnt in counts.items() if cnt >= max(1, int(min_games))
    }
    if not qualified:
        return []

    ordered = sorted(norm_rows, key=lambda r: (int(r["completion_ts"]), int(r["match_id"]), str(r["side"])))

    # Histórico cronológico de nicks por jogador (permite preservar nick anterior com '*')
    first_alias_by_pid: dict[int, str] = {}
    latest_alias_by_pid: dict[int, str] = {}
    for r in ordered:
        for pid_k, alias_k in (("p1_id", "p1_alias"), ("p2_id", "p2_alias")):
            pid_v = int(r[pid_k])
            al_v = str(r.get(alias_k) or "").strip()
            if pid_v > 0 and al_v:
                if pid_v not in first_alias_by_pid:
                    first_alias_by_pid[pid_v] = al_v
                latest_alias_by_pid[pid_v] = al_v

    # Agrupar por match_id preservando ordem cronológica (para usar ELO do oponente se ambos forem arranjados)
    match_groups: list[list[dict[str, Any]]] = []
    idx = 0
    n_rows = len(ordered)
    while idx < n_rows:
        mid = int(ordered[idx]["match_id"])
        grp = [ordered[idx]]
        idx += 1
        while idx < n_rows and int(ordered[idx]["match_id"]) == mid:
            grp.append(ordered[idx])
            idx += 1
        match_groups.append(grp)

    team_state: dict[tuple[str, str], dict[str, Any]] = {}

    for grp in match_groups:
        # Captura ELO pré-partida de todos os times qualificados presentes nesta partida
        pre_elos: dict[tuple[str, str], float] = {}
        for r in grp:
            key = (str(r["pair_key"]), str(r["side"]).lower())
            if key in qualified:
                pre_elos[key] = team_state[key]["elo"] if key in team_state else float(initial_elo)

        for r in grp:
            side = str(r["side"]).lower()
            pair_key = str(r["pair_key"])
            key = (pair_key, side)
            if key not in qualified:
                continue

            # Se o adversário na mesma partida também for um time arranjado BR, usa o ELO do time adversário
            opp_elo = float(r.get("enemy_elo") or initial_elo)
            if opp_elo <= 0:
                opp_elo = float(initial_elo)
            for other in grp:
                other_key = (str(other["pair_key"]), str(other["side"]).lower())
                if other_key != key and other_key in pre_elos:
                    opp_elo = pre_elos[other_key]
                    break

            cur_elo = pre_elos[key]
            won = bool(int(r["won"]))
            expected = 1.0 / (1.0 + 10.0 ** ((opp_elo - cur_elo) / 400.0))
            new_elo = cur_elo + float(k_factor) * ((1.0 if won else 0.0) - expected)

            st = team_state.get(key)
            if st is None:
                st = {
                    "id": f"{side}_{pair_key}",
                    "type": "2v2",
                    "side": side,
                    "elo": float(initial_elo),
                    "w": 0,
                    "l": 0,
                    "s": 0,
                    "t": 0,
                    "lmTS": 0,
                    "players": [],
                }
                team_state[key] = st

            st["elo"] = round(new_elo, 1)
            if won:
                st["w"] += 1
                st["s"] = (st["s"] + 1) if st["s"] > 0 else 1
            else:
                st["l"] += 1
                st["s"] = (st["s"] - 1) if st["s"] < 0 else -1
            st["t"] = st["w"] + st["l"]

            comp_ts = int(r["completion_ts"])
            if comp_ts >= st["lmTS"]:
                st["lmTS"] = comp_ts
                p1_id = int(r["p1_id"])
                p2_id = int(r["p2_id"])
                p1_cur = latest_alias_by_pid.get(p1_id, str(r["p1_alias"]))
                p2_cur = latest_alias_by_pid.get(p2_id, str(r["p2_alias"]))
                st["players"] = [
                    {
                        "profile_id": p1_id,
                        "alias": p1_cur,
                        "original_alias": first_alias_by_pid.get(p1_id, p1_cur),
                        "country": str(r["p1_country"] or "br").lower(),
                        "steam_id": str(r.get("p1_steam_id") or ""),
                    },
                    {
                        "profile_id": p2_id,
                        "alias": p2_cur,
                        "original_alias": first_alias_by_pid.get(p2_id, p2_cur),
                        "country": str(r["p2_country"] or "br").lower(),
                        "steam_id": str(r.get("p2_steam_id") or ""),
                    },
                ]

    return list(team_state.values())


def compute_unified_2v2_arranged_teams(
    raw_teams: list[dict[str, Any]],
    custom_players: list[dict[str, Any]] | None = None,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    now_ts: int | None = None,
    min_total_games: int = MIN_TOTAL_GAMES_ARRANGED_TEAM,
    max_teams: int | None = MAX_ARRANGED_TEAMS_TOP_N,
) -> list[dict[str, Any]]:
    """
    Considera todo o histórico de times brasileiros 2v2 arranjados (ambos BR/Veteranos),
    unificando Eixo + Aliados da mesma dupla pela média simples de ELO e exigindo no mínimo
    `min_total_games` partidas no geral (padrão 10) para calibragem de ELO.

    Regras de corte:
    - Todos os times inativos há mais de `inactive_days` (padrão 30 dias) são removidos do ranking;
    - Mantém exclusivamente o Top `max_teams` (padrão 32), não exibindo nenhum time após a 32ª colocação.
    """
    veterano_pids = _extract_veterano_pids(custom_players)
    _, pid_map = build_roster_lookup_maps(custom_players)
    manual_steam_map, manual_pid_map = build_manual_other_br_lookup_maps()
    manual_br_pids = frozenset(manual_pid_map.keys())

    current_ts = int(now_ts if now_ts is not None else time.time())
    max_age_sec = max(1, int(inactive_days)) * 86400
    min_total_req = max(1, int(min_total_games))

    # Passo 1: deduplicar registros históricos por (pair_key, side) sem descartar lados antigos
    per_pair_side: dict[tuple[tuple[int, int], str], dict[str, Any]] = {}

    for t in raw_teams:
        if not isinstance(t, dict) or not is_brazilian_2v2_team(t, veterano_pids, manual_br_pids):
            continue
        side = str(t.get("side") or "").strip().lower()
        if side not in VALID_SIDES:
            continue

        lm_ts = _normalize_unix_seconds(t.get("lmTS"))
        if lm_ts <= 0:
            continue

        try:
            elo_val = float(t.get("elo", 0) or 0)
        except (TypeError, ValueError):
            continue
        if elo_val <= 0:
            continue

        wins = max(0, int(t.get("w", 0) or 0))
        losses = max(0, int(t.get("l", 0) or 0))
        total = wins + losses
        if total <= 0:
            continue

        streak = int(t.get("s", 0) or 0)
        players_raw = t["players"]
        pid0 = int(players_raw[0]["profile_id"])
        pid1 = int(players_raw[1]["profile_id"])
        if pid0 <= 0 or pid1 <= 0 or pid0 == pid1:
            continue
        pair_key = (pid0, pid1) if pid0 < pid1 else (pid1, pid0)

        players_by_pid: dict[int, dict[str, Any]] = {}
        for p in players_raw:
            ppid = int(p["profile_id"])
            players_by_pid[ppid] = {
                "profile_id": ppid,
                "alias": str(p.get("alias") or f"Jogador {ppid}"),
                "original_alias": str(p.get("original_alias") or "").strip(),
                "country": str(p.get("country") or "br").strip().lower() or "br",
                "steam_id": extract_steam_id(p.get("steam_id")),
            }

        dedup_key = (pair_key, side)
        candidate = {
            "id": str(t.get("id") or ""),
            "side": side,
            "elo": round(elo_val, 1),
            "wins": wins,
            "losses": losses,
            "total": total,
            "streak": streak,
            "lm_ts": lm_ts,
            "players_by_pid": players_by_pid,
        }

        prev = per_pair_side.get(dedup_key)
        if prev is None or (candidate["lm_ts"], candidate["total"]) > (prev["lm_ts"], prev["total"]):
            per_pair_side[dedup_key] = candidate

    # Passo 2: agrupar Eixo + Aliados de todo o histórico da mesma dupla (pair_key)
    by_pair: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
    for (pair_key, side), rec in per_pair_side.items():
        by_pair.setdefault(pair_key, {})[side] = rec

    unified_teams: list[dict[str, Any]] = []
    for pair_key, side_map in by_pair.items():
        sides_present = [s for s in VALID_SIDES if s in side_map]
        if not sides_present:
            continue

        side_recs = [side_map[s] for s in sides_present]
        latest_rec = max(side_recs, key=lambda r: (r["lm_ts"], r["total"]))

        wins = sum(r["wins"] for r in side_recs)
        losses = sum(r["losses"] for r in side_recs)
        total = wins + losses
        if total < min_total_req:
            continue

        avg_elo = round(sum(r["elo"] for r in side_recs) / len(side_recs), 1)
        axis_elo = int(round(side_map["axis"]["elo"])) if "axis" in side_map else None
        allies_elo = int(round(side_map["allies"]["elo"])) if "allies" in side_map else None
        ratio = int(round((wins * 100.0) / total)) if total > 0 else 0
        last_match_date = max(r["lm_ts"] for r in side_recs)
        streak = int(latest_rec["streak"])
        is_inactive = (current_ts - last_match_date) > max_age_sec

        # Remove todos os times inativos há mais de inactive_days (30 dias)
        if is_inactive:
            continue

        players_info: list[dict[str, Any]] = []
        for pid in pair_key:
            p_raw = latest_rec["players_by_pid"].get(pid)
            if p_raw is None:
                for r in side_recs:
                    if pid in r["players_by_pid"]:
                        p_raw = r["players_by_pid"][pid]
                        break
            cur_alias = str((p_raw or {}).get("alias") or f"Jogador {pid}")
            obs_orig = str((p_raw or {}).get("original_alias") or "").strip()
            country = str((p_raw or {}).get("country") or "br").lower()
            is_vet = pid in veterano_pids
            r_entry = pid_map.get(pid) or {}
            steam_id = str(r_entry.get("steam_id") or (p_raw or {}).get("steam_id") or "")
            ref_entry = (
                r_entry
                if (is_vet and r_entry)
                else ((manual_steam_map.get(steam_id) if steam_id else None) or manual_pid_map.get(pid))
            )
            smurf_owner = ""
            if ref_entry:
                if ref_entry.get("country"):
                    country = str(ref_entry["country"]).strip().lower() or country
                if ref_entry.get("steam_id") and not steam_id:
                    steam_id = str(ref_entry["steam_id"])
                is_pin = bool(ref_entry.get("pin_alias"))
                orig_al = str(ref_entry.get("original_alias") or "")
                disp_alias = format_bound_display_alias(
                    cur_alias,
                    orig_al,
                    ref_entry.get("known_aliases"),
                    pin_alias=is_pin,
                )
                smurf_owner = extract_smurf_owner(
                    orig_al or disp_alias,
                    pin_alias=is_pin,
                    explicit_owner=str(ref_entry.get("smurf_of") or ""),
                )
            elif obs_orig and obs_orig.casefold() != cur_alias.casefold():
                disp_alias = format_bound_display_alias(cur_alias, obs_orig)
            else:
                disp_alias = cur_alias

            players_info.append(
                {
                    "profile_id": pid,
                    "steam_id": steam_id,
                    "alias": disp_alias,
                    "current_alias": cur_alias,
                    "country": country,
                    "is_expat": bool(country and country != "br"),
                    "is_veterano": is_vet,
                    "smurf_of": smurf_owner,
                    "is_smurf": bool(smurf_owner),
                }
            )

        # Ordenar jogadores da dupla: Veteranos primeiro, depois ordem alfabética
        players_info.sort(key=lambda p: (0 if p["is_veterano"] else 1, p["alias"].casefold(), p["profile_id"]))

        vet_count = sum(1 for p in players_info if p["is_veterano"])
        has_veterano = vet_count > 0
        all_veteranos = vet_count == 2
        has_smurf = any(bool(p.get("is_smurf") or p.get("smurf_of")) for p in players_info)

        team_ids = {s: side_map[s]["id"] for s in sides_present if side_map[s]["id"]}

        unified_teams.append(
            {
                "pair_key": f"{pair_key[0]}_{pair_key[1]}",
                "team_ids": team_ids,
                "players": players_info,
                "sides": sides_present,
                "avg_elo": avg_elo,
                "axis_elo": axis_elo,
                "allies_elo": allies_elo,
                "wins": wins,
                "losses": losses,
                "total": total,
                "ratio": ratio,
                "streak": streak,
                "last_match_date": last_match_date,
                "veteranos_count": vet_count,
                "has_veterano": has_veterano,
                "all_veteranos": all_veteranos,
                "has_smurf": has_smurf,
                "inactive_fallback": False,
                "coh3stats_url": f"https://coh3stats.com/players/{players_info[0]['profile_id']}",
            }
        )

    unified_teams.sort(
        key=lambda x: (
            -float(x["avg_elo"]),
            -len(x["sides"]),
            -int(x["wins"]),
            -int(x["total"]),
            -int(x["last_match_date"]),
            str(x["pair_key"]),
        )
    )
    if max_teams is not None and int(max_teams) > 0:
        unified_teams = unified_teams[: int(max_teams)]
    for pos, team in enumerate(unified_teams, start=1):
        team["br_rank"] = pos

    return unified_teams


def export_arranged_teams_files(
    teams: list[dict[str, Any]],
    json_path: Path,
    csv_path: Path,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
    min_total_games: int = MIN_TOTAL_GAMES_ARRANGED_TEAM,
    max_teams: int = MAX_ARRANGED_TEAMS_TOP_N,
) -> None:
    """Exporta atomicamente o ranking unificado Top 32 de times brasileiros 2v2 arranjados em JSON e CSV."""
    vet_teams_cnt = sum(1 for t in teams if t.get("has_veterano"))
    json_str = json.dumps(
        {
            "updated_at": int(time.time()),
            "source": "coh3stats_open_data_matches",
            "mode": "2v2",
            "methodology": "full_history_elo_avg_axis_allies_both_br_min10_total_games_active_only_top32",
            "inactive_days_cutoff": int(inactive_days),
            "min_games_per_side": MIN_GAMES_FOR_ARRANGED_TEAM,
            "min_total_games": int(min_total_games),
            "top_n_limit": int(max_teams),
            "total_teams": len(teams),
            "active_teams": len(teams),
            "veteranos_teams_count": vet_teams_cnt,
            "teams": teams,
        },
        ensure_ascii=False,
        indent=2,
    )

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf, lineterminator="\n")
    writer.writerow(
        [
            "br_rank",
            "status",
            "has_veterano",
            "veteranos_count",
            "sides",
            "avg_elo",
            "axis_elo",
            "allies_elo",
            "player1_id",
            "player1_alias",
            "player1_country",
            "player1_veterano",
            "player2_id",
            "player2_alias",
            "player2_country",
            "player2_veterano",
            "wins",
            "losses",
            "total",
            "ratio",
            "streak",
            "last_match_date",
            "coh3stats_url",
        ]
    )
    for t in teams:
        p1, p2 = t["players"][0], t["players"][1]
        writer.writerow(
            [
                t["br_rank"],
                "inativo" if t.get("inactive_fallback") else "ativo",
                "sim" if t.get("has_veterano") else "nao",
                t.get("veteranos_count", 0),
                "+".join(t.get("sides", [])),
                t["avg_elo"],
                t.get("axis_elo") if t.get("axis_elo") is not None else "",
                t.get("allies_elo") if t.get("allies_elo") is not None else "",
                int(p1["profile_id"]),
                sanitize_csv_cell(p1["alias"]),
                sanitize_csv_cell(p1.get("country", "br")),
                "sim" if p1.get("is_veterano") else "nao",
                int(p2["profile_id"]),
                sanitize_csv_cell(p2["alias"]),
                sanitize_csv_cell(p2.get("country", "br")),
                "sim" if p2.get("is_veterano") else "nao",
                t["wins"],
                t["losses"],
                t["total"],
                t["ratio"],
                t["streak"],
                t["last_match_date"],
                sanitize_csv_cell(t.get("coh3stats_url", "")),
            ]
        )

    atomic_write_text(json_path, json_str + "\n")
    atomic_write_text(csv_path, csv_buf.getvalue())
