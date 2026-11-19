"""Cliente endurecido da API pública do COH3 Stats para o Ranking de Times Brasileiros 2v2 Arranjados."""

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
    build_roster_lookup_maps,
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

logger = logging.getLogger("coh3_ranking.arranged_teams")

COH3STATS_BASE_URL = "https://cache.coh3stats.com/sharedAPIGen2Http"
ALLOWED_COH3STATS_HOSTS: frozenset[str] = frozenset({"cache.coh3stats.com"})
MAX_COH3STATS_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MB limite rígido
DEFAULT_INACTIVE_DAYS = 30
VALID_SIDES = ("axis", "allies")

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


class Coh3StatsTeamsClient:
    """Cliente HTTPS endurecido para consultar times 2v2 arranjados via cache.coh3stats.com."""

    def __init__(self, min_interval_sec: float = 0.08, timeout_sec: float = 15.0) -> None:
        self.min_interval_sec = max(0.02, float(min_interval_sec))
        self.timeout_sec = max(3.0, float(timeout_sec))
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
        allow_404: bool = False,
        max_bytes: int = MAX_COH3STATS_RESPONSE_BYTES,
    ) -> dict[str, Any] | None:
        validate_https_endpoint(
            url,
            ALLOWED_COH3STATS_HOSTS,
            path_prefix="/sharedAPIGen2Http/",
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
                    raise ValueError("Formato JSON inesperado retornado pelo COH3 Stats.")
                return payload
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code == 404 and allow_404:
                    return None
                if exc.code in (400, 401, 403, 404):
                    break
                wait = (2**attempt) * 0.7
                logger.warning(
                    "HTTP %d no COH3 Stats (%s) tentativa %d/%d; aguardando %.1fs",
                    exc.code,
                    url,
                    attempt + 1,
                    retries,
                    wait,
                )
                time.sleep(wait)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last_err = exc
                wait = (2**attempt) * 0.7
                logger.warning(
                    "Falha na requisição COH3 Stats (%s) tentativa %d/%d: %s",
                    url,
                    attempt + 1,
                    retries,
                    redact_secrets(exc),
                )
                time.sleep(wait)

        raise RuntimeError(f"Erro ao consultar API COH3 Stats ({url}): {redact_secrets(last_err)}")

    def fetch_2v2_leaderboard_side(
        self,
        side: str,
        veterano_pids: set[int],
        limit: int = 100,
        max_pages: int = 150,
        max_scan_seconds: float = 45.0,
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Paginates global 2v2 arranged team leaderboards for one side, filtering Brazilian duos."""
        if side not in VALID_SIDES:
            raise ValueError(f"Lado inválido para times arranjados: {side}")

        page_limit = max(10, min(100, int(limit)))
        br_teams: list[dict[str, Any]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        scanned_count = 0
        total_teams = 0
        started_at = time.monotonic()

        for page_idx in range(max_pages):
            if page_idx > 0 and (time.monotonic() - started_at) >= max_scan_seconds:
                logger.info(
                    "Limite de tempo (%.0fs) atingido na varredura COH3 Stats 2v2 %s (%d/%d times).",
                    max_scan_seconds,
                    side,
                    scanned_count,
                    total_teams,
                )
                break

            url = (
                f"{COH3STATS_BASE_URL}/teams/leaderboards"
                f"?side={side}&type=2v2&orderBy=elo&limit={page_limit}"
            )
            if cursor:
                url += f"&cursor={urllib.parse.quote(cursor, safe='')}"

            data = self._get_json(url)
            if not data:
                break

            if page_idx == 0:
                total_teams = int(data.get("totalTeams", 0) or 0)

            page_teams = data.get("teams")
            if not isinstance(page_teams, list) or not page_teams:
                break

            scanned_count += len(page_teams)
            for t in page_teams:
                if not isinstance(t, dict):
                    continue
                if is_brazilian_2v2_team(t, veterano_pids):
                    rec = dict(t)
                    rec["side"] = side
                    rec["type"] = "2v2"
                    br_teams.append(rec)

            next_cursor = data.get("nextCursor")
            if not next_cursor or not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        return br_teams, scanned_count, total_teams

    def fetch_player_teams(self, profile_id: int) -> list[dict[str, Any]]:
        """Consulta /players/{profile_id}/teams para garantir 100% de cobertura dos Veteranos."""
        pid = int(profile_id or 0)
        if pid <= 0:
            return []
        url = f"{COH3STATS_BASE_URL}/players/{pid}/teams"
        data = self._get_json(url, retries=2, allow_404=True)
        if not data:
            return []

        found: list[dict[str, Any]] = []
        for field_name, side in (("axisTeams", "axis"), ("alliesTeams", "allies")):
            entries = data.get(field_name)
            if not isinstance(entries, list):
                continue
            for t in entries:
                if not isinstance(t, dict):
                    continue
                if str(t.get("type") or "").strip().lower() != "2v2":
                    continue
                rec = dict(t)
                rec["side"] = side
                rec["type"] = "2v2"
                found.append(rec)
        return found

    def fetch_brazilian_2v2_raw_teams(
        self,
        custom_players: list[dict[str, Any]] | None = None,
        max_pages_per_side: int = 150,
        max_scan_seconds: float = 45.0,
    ) -> list[dict[str, Any]]:
        """Combina varredura global 2v2 (Eixo + Aliados) com consulta direta dos Veteranos."""
        veterano_pids = _extract_veterano_pids(custom_players)
        raw_teams: list[dict[str, Any]] = []
        side_errors: list[str] = []

        # 1. Varredura paralela das leaderboards globais 2v2 (Eixo e Aliados) para incluir todos os BR
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fut_map = {
                pool.submit(
                    self.fetch_2v2_leaderboard_side,
                    side,
                    veterano_pids,
                    100,
                    max_pages_per_side,
                    max_scan_seconds,
                ): side
                for side in VALID_SIDES
            }
            for fut in concurrent.futures.as_completed(fut_map):
                side = fut_map[fut]
                try:
                    br_side_teams, scanned, total = fut.result()
                    raw_teams.extend(br_side_teams)
                    logger.info(
                        "COH3 Stats 2v2 %-6s: %d times BR encontrados (varridos %d/%d times globais).",
                        side.upper(),
                        len(br_side_teams),
                        scanned,
                        total,
                    )
                except Exception as exc:
                    side_errors.append(f"{side}: {redact_secrets(exc)}")
                    logger.warning(
                        "Falha ao varrer leaderboard global 2v2 %s no COH3 Stats: %s",
                        side,
                        redact_secrets(exc),
                    )

        # 2. Complemento direto por perfil dos Veteranos (garante 100% dos Veteranos independente do ELO)
        ordered_vet_pids = sorted(veterano_pids)
        vet_teams_added = 0
        if ordered_vet_pids:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                fut_vet = {pool.submit(self.fetch_player_teams, pid): pid for pid in ordered_vet_pids}
                for fut in concurrent.futures.as_completed(fut_vet):
                    pid = fut_vet[fut]
                    try:
                        for t in fut.result():
                            if is_brazilian_2v2_team(t, veterano_pids):
                                raw_teams.append(t)
                                vet_teams_added += 1
                    except Exception as exc:
                        logger.debug(
                            "Falha ao consultar times do Veterano #%d no COH3 Stats: %s",
                            pid,
                            redact_secrets(exc),
                        )

        logger.info(
            "Coleta COH3 Stats concluída: %d registros brutos 2v2 BR (%d via consulta direta Veteranos).",
            len(raw_teams),
            vet_teams_added,
        )

        if len(side_errors) == len(VALID_SIDES) and not raw_teams:
            raise RuntimeError("Não foi possível consultar times arranjados no COH3 Stats: " + "; ".join(side_errors))

        return raw_teams


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

        # Montar metadados dos 2 jogadores (aplicando apelido preferencial aos Veteranos)
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
            steam_id = str(r_entry.get("steam_id") or "")
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
                "coh3stats_url": (
                    f"https://coh3stats.com/players/{players_info[0]['profile_id']}?view=teamDetails&team={latest_rec['id']}"
                    if latest_rec.get("id")
                    else f"https://coh3stats.com/players/{players_info[0]['profile_id']}"
                ),
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
            "source": "coh3stats_arranged_teams",
            "mode": "2v2",
            "methodology": "simple_average_of_active_sides_axis_allies_elo_both_players_br",
            "inactive_days_cutoff": int(inactive_days),
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
