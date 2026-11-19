"""Motor do Ranking Unificado de Times Brasileiros 2v2 Arranjados via COH3 Stats Open Data (storage.coh3stats.com)."""

from __future__ import annotations

import concurrent.futures
import csv
import io
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from roster import (
    CORE_BR_ROSTER,
    build_roster_lookup_maps,
    extract_steam_id,
    format_bound_display_alias,
)
from security import (
    atomic_write_text,
    build_no_redirect_https_opener,
    redact_secrets,
    sanitize_csv_cell,
    validate_https_endpoint,
)

logger = logging.getLogger("coh3_ranking.arranged_teams")

COH3STATS_STORAGE_BASE_URL = "https://storage.coh3stats.com"
ALLOWED_COH3STATS_STORAGE_HOSTS: frozenset[str] = frozenset({"storage.coh3stats.com"})
MAX_DAILY_WIRE_BYTES = 45 * 1024 * 1024          # 45 MB comprimido no fio
MAX_DAILY_UNCOMPRESSED_BYTES = 220 * 1024 * 1024  # 220 MB descomprimido (anti-Gzip-bomb)

DEFAULT_INACTIVE_DAYS = 30
DEFAULT_BOOTSTRAP_DAYS = 60
MIN_GAMES_FOR_ARRANGED_TEAM = 3
INITIAL_TEAM_ELO = 1200.0
ELO_K_FACTOR = 32.0

VALID_SIDES = ("axis", "allies")
AUTOMATCH_2V2_TYPES: frozenset[int] = frozenset({2, 21})  # 2V2_Ranked, 2V2_Unranked
AXIS_RACE_IDS: frozenset[int] = frozenset({137123, 198437})  # Wehrmacht, DAK
ALLIES_RACE_IDS: frozenset[int] = frozenset({129494, 203852, 197345})  # USF, UKF

_THREAD_LOCAL = threading.local()


def _get_thread_opener() -> urllib.request.OpenerDirector:
    opener = getattr(_THREAD_LOCAL, "opener", None)
    if opener is None:
        opener = build_no_redirect_https_opener()
        _THREAD_LOCAL.opener = opener
    return opener


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


def is_brazilian_player(player: dict[str, Any], veterano_pids: set[int]) -> bool:
    """Retorna True se o jogador tiver bandeira BR ou fizer parte do elenco fixo de Veteranos."""
    pid = int(player.get("profile_id", 0) or 0)
    country = str(player.get("country") or "").strip().lower()
    return country == "br" or (pid > 0 and pid in veterano_pids)


def is_brazilian_2v2_team(raw_team: dict[str, Any], veterano_pids: set[int]) -> bool:
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
    return is_brazilian_player(players[0], veterano_pids) and is_brazilian_player(players[1], veterano_pids)


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
                mhm = r.get("matchhistorymember") if isinstance(r.get("matchhistorymember"), dict) else {}
                if res_raw not in (0, 1):
                    res_raw = mhm.get("outcome")
                if res_raw in (0, 1):
                    outcomes.add(int(res_raw))

                prof = r.get("profile") if isinstance(r.get("profile"), dict) else {}
                alias = str(prof.get("alias") or f"Jogador {pid}")
                country = str(prof.get("country") or "").strip().lower()
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
            if not (is_brazilian_player(p0, veterano_pids) and is_brazilian_player(p1, veterano_pids)):
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
        self.min_interval_sec = max(0.02, float(min_interval_sec))
        self.timeout_sec = max(5.0, float(timeout_sec))
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
        url = f"{COH3STATS_STORAGE_BASE_URL}/matches/matches-{int(day_ts)}.json"
        validate_https_endpoint(
            url,
            ALLOWED_COH3STATS_STORAGE_HOSTS,
            path_prefix="/matches/",
        )
        opener = _get_thread_opener()
        headers = {
            "User-Agent": "CoH3-BR-Ranking-Unificado/1.0 (COH3 Stats Open Data)",
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        }

        last_err: Exception | None = None
        for attempt in range(retries):
            self._reserve_rate_slot()
            try:
                req = urllib.request.Request(url, headers=headers)
                with opener.open(req, timeout=self.timeout_sec) as resp:
                    raw_bytes = _read_bounded_daily_dump(resp)
                payload = json.loads(raw_bytes.decode("utf-8"))
                del raw_bytes
                matches = payload.get("matches") if isinstance(payload, dict) else None
                if not isinstance(matches, list):
                    raise ValueError("Estrutura JSON inesperada no dump diário do COH3 Stats Open Data.")
                total_in_day = len(matches)
                br_rows = extract_brazilian_2v2_matches_from_dump(matches, veterano_pids)
                return "ok", total_in_day, br_rows
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code == 404:
                    return "not_found", 0, []
                if exc.code in (400, 401, 403):
                    break
                wait = (2**attempt) * 1.0
                logger.warning(
                    "HTTP %d no COH3 Stats Open Data (dia %d) tentativa %d/%d; aguardando %.1fs",
                    exc.code,
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


def sync_brazilian_2v2_arranged_matches(
    client: Coh3StatsOpenDataClient,
    db: Any,
    custom_players: list[dict[str, Any]] | None = None,
    bootstrap_days: int = DEFAULT_BOOTSTRAP_DAYS,
    max_scan_seconds: float = 150.0,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Sincroniza incrementalmente os dumps diários do COH3 Stats Open Data no SQLite."""
    veterano_pids = _extract_veterano_pids(custom_players)
    now_dt = datetime.fromtimestamp(int(now_ts if now_ts is not None else time.time()), tz=timezone.utc)
    today_midnight = int(datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=timezone.utc).timestamp())

    processed_days = db.get_processed_arranged_days()
    # Varre do dia mais recente para o mais antigo para priorizar a janela ativa dos últimos 30 dias
    pending_days: list[tuple[int, int]] = []
    for offset in range(1, max(1, int(bootstrap_days)) + 1):
        day_ts = today_midnight - offset * 86400
        if day_ts not in processed_days:
            pending_days.append((offset, day_ts))

    if pending_days:
        logger.info(
            "COH3 Stats Open Data: sincronizando %d dia(s) pendente(s) de partidas 2v2...",
            len(pending_days),
        )

    started_at = time.monotonic()
    synced_days = 0
    total_new_rows = 0
    errors_count = 0

    # Lotes de 3 dias por vez para manter uso de RAM moderado e respeitar o orçamento de tempo
    batch_size = 3
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(batch)) as pool:
            fut_map = {
                pool.submit(client.fetch_day_brazilian_2v2_matches, day_ts, veterano_pids): (offset, day_ts)
                for offset, day_ts in batch
            }
            for fut in concurrent.futures.as_completed(fut_map):
                offset, day_ts = fut_map[fut]
                try:
                    status, total_in_day, br_rows = fut.result()
                    if status == "ok":
                        db.save_arranged_2v2_matches(br_rows)
                        db.mark_arranged_day_processed(day_ts, matches_total=total_in_day, br_2v2_matches=len(br_rows))
                        synced_days += 1
                        total_new_rows += len(br_rows)
                    elif offset > 2:
                        # Dia histórico (> 48h atrás) ausente no bucket: marca como verificado para não repetir
                        db.mark_arranged_day_processed(day_ts, matches_total=0, br_2v2_matches=0)
                except Exception as exc:
                    errors_count += 1
                    logger.warning("Erro ao processar dia %d do COH3 Stats Open Data: %s", day_ts, redact_secrets(exc))

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


def build_side_teams_from_matches(
    match_rows: list[dict[str, Any]],
    min_games: int = MIN_GAMES_FOR_ARRANGED_TEAM,
    k_factor: float = ELO_K_FACTOR,
    initial_elo: float = INITIAL_TEAM_ELO,
) -> list[dict[str, Any]]:
    """
    Reconstrói times arranjados 2v2 por lado (axis/allies) em ordem cronológica a partir das partidas,
    aplicando ELO inicial 1200 e atualização Elo partida a partida (exigindo >= min_games jogos juntos).
    """
    if not match_rows:
        return []

    counts: Counter[tuple[str, str]] = Counter(
        (str(r["pair_key"]), str(r["side"]).lower()) for r in match_rows
    )
    qualified: set[tuple[str, str]] = {
        key for key, cnt in counts.items() if cnt >= max(1, int(min_games))
    }
    if not qualified:
        return []

    ordered = sorted(match_rows, key=lambda r: (int(r["completion_ts"]), int(r["match_id"]), str(r["side"])))

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
                st["players"] = [
                    {
                        "profile_id": int(r["p1_id"]),
                        "alias": str(r["p1_alias"]),
                        "country": str(r["p1_country"] or "br").lower(),
                        "steam_id": str(r.get("p1_steam_id") or ""),
                    },
                    {
                        "profile_id": int(r["p2_id"]),
                        "alias": str(r["p2_alias"]),
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
) -> list[dict[str, Any]]:
    """
    Filtra times brasileiros 2v2 ativos (ambos jogadores BR/Veteranos, última partida <= inactive_days)
    e unifica Eixo + Aliados da mesma dupla pela média simples de ELO.
    """
    veterano_pids = _extract_veterano_pids(custom_players)
    _, pid_map = build_roster_lookup_maps(custom_players)

    current_ts = int(now_ts if now_ts is not None else time.time())
    max_age_sec = max(1, int(inactive_days)) * 86400

    # Passo 1: deduplicar registros ativos por (pair_key, side)
    per_pair_side: dict[tuple[tuple[int, int], str], dict[str, Any]] = {}

    for t in raw_teams:
        if not isinstance(t, dict) or not is_brazilian_2v2_team(t, veterano_pids):
            continue
        side = str(t.get("side") or "").strip().lower()
        if side not in VALID_SIDES:
            continue

        lm_ts = _normalize_unix_seconds(t.get("lmTS"))
        if lm_ts <= 0 or (current_ts - lm_ts) > max_age_sec:
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
        pair_key = (pid0, pid1) if pid0 < pid1 else (pid1, pid0)

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
            "players_by_pid": {
                int(p["profile_id"]): {
                    "profile_id": int(p["profile_id"]),
                    "alias": str(p.get("alias") or f"Jogador {int(p['profile_id'])}"),
                    "country": str(p.get("country") or "br").strip().lower() or "br",
                    "steam_id": extract_steam_id(p.get("steam_id")),
                }
                for p in players_raw
            },
        }

        prev = per_pair_side.get(dedup_key)
        if prev is None or (candidate["lm_ts"], candidate["total"]) > (prev["lm_ts"], prev["total"]):
            per_pair_side[dedup_key] = candidate

    # Passo 2: agrupar Eixo + Aliados da mesma dupla (pair_key)
    by_pair: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
    for (pair_key, side), rec in per_pair_side.items():
        by_pair.setdefault(pair_key, {})[side] = rec

    unified_teams: list[dict[str, Any]] = []
    for pair_key, side_map in by_pair.items():
        active_sides = [s for s in VALID_SIDES if s in side_map]
        if not active_sides:
            continue

        active_recs = [side_map[s] for s in active_sides]
        latest_rec = max(active_recs, key=lambda r: (r["lm_ts"], r["total"]))

        avg_elo = round(sum(r["elo"] for r in active_recs) / len(active_recs), 1)
        axis_elo = int(round(side_map["axis"]["elo"])) if "axis" in side_map else None
        allies_elo = int(round(side_map["allies"]["elo"])) if "allies" in side_map else None

        wins = sum(r["wins"] for r in active_recs)
        losses = sum(r["losses"] for r in active_recs)
        total = wins + losses
        ratio = int(round((wins * 100.0) / total)) if total > 0 else 0
        last_match_date = max(r["lm_ts"] for r in active_recs)
        streak = int(latest_rec["streak"])

        players_info: list[dict[str, Any]] = []
        for pid in pair_key:
            p_raw = latest_rec["players_by_pid"].get(pid)
            if p_raw is None:
                for r in active_recs:
                    if pid in r["players_by_pid"]:
                        p_raw = r["players_by_pid"][pid]
                        break
            cur_alias = str((p_raw or {}).get("alias") or f"Jogador {pid}")
            country = str((p_raw or {}).get("country") or "br").lower()
            is_vet = pid in veterano_pids
            r_entry = pid_map.get(pid) or {}
            steam_id = str(r_entry.get("steam_id") or (p_raw or {}).get("steam_id") or "")
            if is_vet and r_entry:
                disp_alias = format_bound_display_alias(
                    cur_alias,
                    str(r_entry.get("original_alias") or ""),
                    r_entry.get("known_aliases"),
                )
            else:
                disp_alias = cur_alias

            players_info.append(
                {
                    "profile_id": pid,
                    "steam_id": steam_id,
                    "alias": disp_alias,
                    "current_alias": cur_alias,
                    "country": country,
                    "is_veterano": is_vet,
                }
            )

        # Ordenar jogadores da dupla: Veteranos primeiro, depois ordem alfabética
        players_info.sort(key=lambda p: (0 if p["is_veterano"] else 1, p["alias"].casefold(), p["profile_id"]))

        vet_count = sum(1 for p in players_info if p["is_veterano"])
        team_ids = {s: side_map[s]["id"] for s in active_sides if side_map[s]["id"]}

        unified_teams.append(
            {
                "pair_key": f"{pair_key[0]}_{pair_key[1]}",
                "team_ids": team_ids,
                "players": players_info,
                "sides": active_sides,
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
                "has_veterano": vet_count > 0,
                "all_veteranos": vet_count == 2,
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
    for pos, team in enumerate(unified_teams, start=1):
        team["br_rank"] = pos

    return unified_teams


def export_arranged_teams_files(
    teams: list[dict[str, Any]],
    json_path: Path,
    csv_path: Path,
    inactive_days: int = DEFAULT_INACTIVE_DAYS,
) -> None:
    """Exporta atomicamente o ranking unificado de times brasileiros 2v2 arranjados em JSON e CSV."""
    vet_teams_cnt = sum(1 for t in teams if t.get("has_veterano"))
    json_str = json.dumps(
        {
            "updated_at": int(time.time()),
            "source": "coh3stats_open_data_matches",
            "mode": "2v2",
            "methodology": "simple_average_of_active_sides_axis_allies_elo_both_players_br_min3_matches",
            "inactive_days_cutoff": int(inactive_days),
            "min_games_per_side": MIN_GAMES_FOR_ARRANGED_TEAM,
            "total_teams": len(teams),
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
