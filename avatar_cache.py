"""Persistência SQLite leve dedicada ao Ranking Unificado BR (estado do painel + cache de avatares Steam)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class RankingCacheDB:
    """Armazena estado persistente do painel fixo no Discord e cache local de avatares Steam (PNG 64x64)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        if self.db_path.is_symlink():
            raise RuntimeError(f"Caminho do banco SQLite não pode ser symlink: {self.db_path}")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all_conns: list[sqlite3.Connection] = []
        self._init_schema()

    def __enter__(self) -> RankingCacheDB:
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _connect(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA synchronous = NORMAL")
        self._local.conn = conn
        with self._lock:
            self._all_conns.append(conn)
        return conn

    def close(self) -> None:
        with self._lock:
            conns = list(self._all_conns)
            self._all_conns.clear()
        for c in conns:
            try:
                c.close()
            except sqlite3.Error:
                pass
        if hasattr(self._local, "conn"):
            delattr(self._local, "conn")

    def _init_schema(self) -> None:
        conn = self._connect()
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ranking_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS player_avatars (
                    steam_id TEXT PRIMARY KEY,
                    profile_id INTEGER NOT NULL DEFAULT 0,
                    avatar_url TEXT NOT NULL,
                    png_blob BLOB NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_player_avatars_pid ON player_avatars(profile_id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS arranged_processed_days (
                    day_ts INTEGER PRIMARY KEY,
                    matches_total INTEGER NOT NULL DEFAULT 0,
                    br_2v2_matches INTEGER NOT NULL DEFAULT 0,
                    processed_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS arranged_2v2_matches (
                    match_id INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    pair_key TEXT NOT NULL,
                    p1_id INTEGER NOT NULL,
                    p1_alias TEXT NOT NULL,
                    p1_country TEXT NOT NULL,
                    p1_steam_id TEXT NOT NULL DEFAULT '',
                    p2_id INTEGER NOT NULL,
                    p2_alias TEXT NOT NULL,
                    p2_country TEXT NOT NULL,
                    p2_steam_id TEXT NOT NULL DEFAULT '',
                    won INTEGER NOT NULL,
                    enemy_elo REAL NOT NULL DEFAULT 1200.0,
                    completion_ts INTEGER NOT NULL,
                    PRIMARY KEY (match_id, side, pair_key)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_arranged_2v2_ts ON arranged_2v2_matches(completion_ts)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bg_player_cache (
                    profile_id INTEGER PRIMARY KEY,
                    battlegroups_json TEXT NOT NULL,
                    performance_json TEXT NOT NULL DEFAULT '{}',
                    updated_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bg_combat_days (
                    day_ts INTEGER PRIMARY KEY,
                    matches_total INTEGER NOT NULL DEFAULT 0,
                    br_auto_matches INTEGER NOT NULL DEFAULT 0,
                    processed_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bg_combat_matches (
                    match_id INTEGER NOT NULL,
                    profile_id INTEGER NOT NULL,
                    faction TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    won INTEGER NOT NULL,
                    gt REAL NOT NULL,
                    apm REAL NOT NULL,
                    dmg REAL NOT NULL,
                    dmg_pm REAL NOT NULL,
                    dmg_ratio REAL NOT NULL,
                    kd REAL NOT NULL,
                    net_wipes REAL NOT NULL,
                    vkill REAL NOT NULL DEFAULT 0.0,
                    vlost REAL NOT NULL DEFAULT 0.0,
                    pcap_ratio REAL NOT NULL,
                    pcap_vs_enemy REAL NOT NULL DEFAULT 1.0,
                    my_elo REAL NOT NULL DEFAULT 0.0,
                    enemy_avg_elo REAL NOT NULL DEFAULT 0.0,
                    upset_opp INTEGER NOT NULL DEFAULT 0,
                    upset_win INTEGER NOT NULL DEFAULT 0,
                    upset_elo_gap REAL NOT NULL DEFAULT 0.0,
                    stomp_win INTEGER NOT NULL,
                    completion_ts INTEGER NOT NULL,
                    PRIMARY KEY (match_id, profile_id)
                )
                """
            )
            existing_cols = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(bg_combat_matches)").fetchall()
            }
            for col_name, col_def in (
                ("pcap_vs_enemy", "REAL NOT NULL DEFAULT 1.0"),
                ("my_elo", "REAL NOT NULL DEFAULT 0.0"),
                ("enemy_avg_elo", "REAL NOT NULL DEFAULT 0.0"),
                ("upset_opp", "INTEGER NOT NULL DEFAULT 0"),
                ("upset_win", "INTEGER NOT NULL DEFAULT 0"),
                ("upset_elo_gap", "REAL NOT NULL DEFAULT 0.0"),
            ):
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE bg_combat_matches ADD COLUMN {col_name} {col_def}")

            # Migração única para popular colunas v2 (controle de mapa vs inimigo + vitórias vs ELO maior)
            schema_row = conn.execute(
                "SELECT value FROM ranking_state WHERE key = 'bg_combat_schema_version'"
            ).fetchone()
            cur_ver = str(schema_row["value"]) if schema_row else ""
            if cur_ver != "v2_upset_mapctrl":
                has_legacy = conn.execute(
                    "SELECT 1 FROM bg_combat_matches WHERE my_elo = 0.0 LIMIT 1"
                ).fetchone()
                if has_legacy is not None:
                    conn.execute("DELETE FROM bg_combat_matches")
                    conn.execute("DELETE FROM bg_combat_days")
                conn.execute(
                    "INSERT INTO ranking_state (key, value) VALUES ('bg_combat_schema_version', 'v2_upset_mapctrl') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bg_combat_ts ON bg_combat_matches(completion_ts)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_bg_combat_pid ON bg_combat_matches(profile_id)"
            )

    def get_state(self, key: str) -> str | None:
        row = self._connect().execute(
            "SELECT value FROM ranking_state WHERE key = ?",
            (str(key),),
        ).fetchone()
        return str(row["value"]) if row else None

    def set_state(self, key: str, value: str) -> None:
        conn = self._connect()
        with conn:
            conn.execute(
                "INSERT INTO ranking_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )

    def get_cached_avatar(
        self,
        steam_id: str = "",
        profile_id: int = 0,
        max_age_sec: int = 14 * 86400,
    ) -> bytes | None:
        cutoff = int(time.time()) - max(3600, int(max_age_sec))
        conn = self._connect()
        row = None
        if steam_id:
            row = conn.execute(
                "SELECT png_blob FROM player_avatars WHERE steam_id = ? AND updated_at >= ?",
                (str(steam_id), cutoff),
            ).fetchone()
        if row is None and profile_id > 0:
            row = conn.execute(
                "SELECT png_blob FROM player_avatars WHERE profile_id = ? AND updated_at >= ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (int(profile_id), cutoff),
            ).fetchone()
        return bytes(row["png_blob"]) if row and row["png_blob"] else None

    def save_cached_avatar(
        self,
        steam_id: str,
        avatar_url: str,
        png_blob: bytes,
        profile_id: int = 0,
    ) -> None:
        if not steam_id or not png_blob:
            return
        conn = self._connect()
        with conn:
            conn.execute(
                """
                INSERT INTO player_avatars (steam_id, profile_id, avatar_url, png_blob, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(steam_id) DO UPDATE SET
                    profile_id = CASE WHEN excluded.profile_id > 0 THEN excluded.profile_id ELSE player_avatars.profile_id END,
                    avatar_url = excluded.avatar_url,
                    png_blob = excluded.png_blob,
                    updated_at = excluded.updated_at
                """,
                (str(steam_id), int(profile_id or 0), str(avatar_url), sqlite3.Binary(png_blob), int(time.time())),
            )

    def get_steam_id_map(self) -> dict[int, str]:
        rows = self._connect().execute(
            "SELECT profile_id, steam_id FROM player_avatars WHERE profile_id > 0 AND steam_id != ''"
        ).fetchall()
        return {int(r["profile_id"]): str(r["steam_id"]) for r in rows}

    def prune_expired_avatars(self, max_age_sec: int = 21 * 86400) -> int:
        cutoff = int(time.time()) - max(86400, int(max_age_sec))
        conn = self._connect()
        with conn:
            cur = conn.execute("DELETE FROM player_avatars WHERE updated_at < ?", (cutoff,))
            return int(cur.rowcount or 0)

    def get_processed_arranged_days(self) -> set[int]:
        rows = self._connect().execute("SELECT day_ts FROM arranged_processed_days").fetchall()
        return {int(r["day_ts"]) for r in rows}

    def get_latest_arranged_day_with_matches(self) -> int:
        row = self._connect().execute(
            "SELECT COALESCE(MAX(day_ts), 0) AS max_day FROM arranged_processed_days WHERE matches_total > 0"
        ).fetchone()
        return int(row["max_day"]) if row and row["max_day"] else 0

    def get_arranged_live_sync_candidates(
        self,
        recent_since_ts: int,
        min_pair_games: int = 7,
    ) -> tuple[set[int], dict[int, int]]:
        """Retorna (pids_candidatos_br, max_completion_ts_por_pid) para sincronização ao vivo na API Relic."""
        conn = self._connect()
        candidates: set[int] = set()
        for row in conn.execute(
            "SELECT p1_id, p2_id FROM arranged_2v2_matches WHERE completion_ts >= ?",
            (int(recent_since_ts),),
        ):
            candidates.add(int(row["p1_id"]))
            candidates.add(int(row["p2_id"]))

        for row in conn.execute(
            "SELECT p1_id, p2_id FROM arranged_2v2_matches GROUP BY pair_key HAVING COUNT(*) >= ?",
            (max(1, int(min_pair_games)),),
        ):
            candidates.add(int(row["p1_id"]))
            candidates.add(int(row["p2_id"]))

        max_ts_by_pid: dict[int, int] = {
            int(row["pid"]): int(row["mx"] or 0)
            for row in conn.execute(
                """
                SELECT pid, MAX(mx) AS mx FROM (
                    SELECT p1_id AS pid, MAX(completion_ts) AS mx FROM arranged_2v2_matches GROUP BY p1_id
                    UNION ALL
                    SELECT p2_id AS pid, MAX(completion_ts) AS mx FROM arranged_2v2_matches GROUP BY p2_id
                ) GROUP BY pid
                """
            )
        }

        return candidates, max_ts_by_pid

    def mark_arranged_day_processed(
        self,
        day_ts: int,
        matches_total: int = 0,
        br_2v2_matches: int = 0,
    ) -> None:
        conn = self._connect()
        with conn:
            conn.execute(
                """
                INSERT INTO arranged_processed_days (day_ts, matches_total, br_2v2_matches, processed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day_ts) DO UPDATE SET
                    matches_total = excluded.matches_total,
                    br_2v2_matches = excluded.br_2v2_matches,
                    processed_at = excluded.processed_at
                """,
                (int(day_ts), int(matches_total), int(br_2v2_matches), int(time.time())),
            )

    def save_arranged_2v2_matches(self, match_rows: list[dict[str, Any]]) -> int:
        if not match_rows:
            return 0
        conn = self._connect()
        params = [
            (
                int(r["match_id"]),
                str(r["side"]),
                str(r["pair_key"]),
                int(r["p1_id"]),
                str(r["p1_alias"]),
                str(r["p1_country"]),
                str(r.get("p1_steam_id") or ""),
                int(r["p2_id"]),
                str(r["p2_alias"]),
                str(r["p2_country"]),
                str(r.get("p2_steam_id") or ""),
                int(r["won"]),
                float(r.get("enemy_elo", 1200.0)),
                int(r["completion_ts"]),
            )
            for r in match_rows
        ]
        before_changes = conn.total_changes
        with conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO arranged_2v2_matches (
                    match_id, side, pair_key,
                    p1_id, p1_alias, p1_country, p1_steam_id,
                    p2_id, p2_alias, p2_country, p2_steam_id,
                    won, enemy_elo, completion_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
        return max(0, int(conn.total_changes - before_changes))

    def load_all_arranged_2v2_matches(self) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            """
            SELECT
                match_id, side, pair_key,
                p1_id, p1_alias, p1_country, p1_steam_id,
                p2_id, p2_alias, p2_country, p2_steam_id,
                won, enemy_elo, completion_ts
            FROM arranged_2v2_matches
            ORDER BY completion_ts ASC, match_id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_old_arranged_matches(self, max_age_days: int = 365) -> int:
        """Remove partidas 2v2 muito antigas (> max_age_days) para conter o crescimento do banco no Git."""
        cutoff = int(time.time()) - max(180, int(max_age_days)) * 86400
        conn = self._connect()
        with conn:
            cur = conn.execute("DELETE FROM arranged_2v2_matches WHERE completion_ts < ?", (cutoff,))
            conn.execute("DELETE FROM arranged_processed_days WHERE day_ts < ?", (cutoff,))
            return int(cur.rowcount or 0)

    def get_all_bg_player_cache(self) -> dict[int, dict[str, Any]]:
        """Carrega todo o cache local de Battlegroups + cohdb Performance por profile_id."""
        rows = self._connect().execute(
            "SELECT profile_id, battlegroups_json, performance_json, updated_at FROM bg_player_cache"
        ).fetchall()
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            try:
                bgs = json.loads(str(r["battlegroups_json"] or "{}"))
            except (ValueError, TypeError):
                bgs = {}
            try:
                perf = json.loads(str(r["performance_json"] or "{}"))
            except (ValueError, TypeError):
                perf = {}
            out[int(r["profile_id"])] = {
                "battlegroups": bgs if isinstance(bgs, dict) else {},
                "performance": perf if isinstance(perf, dict) else {},
                "updated_at": int(r["updated_at"] or 0),
            }
        return out

    def save_bg_player_cache(
        self,
        profile_id: int,
        battlegroups: dict[str, Any] | None = None,
        performance: dict[str, Any] | None = None,
        updated_at: int | None = None,
    ) -> None:
        """Persiste ou atualiza parcialmente o cache cohdb (battlegroups e/ou performance) de um jogador."""
        pid = int(profile_id)
        if pid <= 0:
            return
        now_ts = int(updated_at if updated_at is not None else time.time())
        conn = self._connect()
        existing = conn.execute(
            "SELECT battlegroups_json, performance_json FROM bg_player_cache WHERE profile_id = ?",
            (pid,),
        ).fetchone()
        bgs_str = (
            json.dumps(battlegroups, ensure_ascii=False, separators=(",", ":"))
            if battlegroups is not None
            else (str(existing["battlegroups_json"]) if existing else "{}")
        )
        perf_str = (
            json.dumps(performance, ensure_ascii=False, separators=(",", ":"))
            if performance is not None
            else (str(existing["performance_json"]) if existing else "{}")
        )
        with conn:
            conn.execute(
                """
                INSERT INTO bg_player_cache (profile_id, battlegroups_json, performance_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    battlegroups_json = excluded.battlegroups_json,
                    performance_json = excluded.performance_json,
                    updated_at = excluded.updated_at
                """,
                (pid, bgs_str, perf_str, now_ts),
            )

    def get_processed_bg_days(self) -> set[int]:
        rows = self._connect().execute("SELECT day_ts FROM bg_combat_days").fetchall()
        return {int(r["day_ts"]) for r in rows}

    def mark_bg_day_processed(
        self,
        day_ts: int,
        matches_total: int = 0,
        br_auto_matches: int = 0,
    ) -> None:
        conn = self._connect()
        with conn:
            conn.execute(
                """
                INSERT INTO bg_combat_days (day_ts, matches_total, br_auto_matches, processed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day_ts) DO UPDATE SET
                    matches_total = excluded.matches_total,
                    br_auto_matches = excluded.br_auto_matches,
                    processed_at = excluded.processed_at
                """,
                (int(day_ts), int(matches_total), int(br_auto_matches), int(time.time())),
            )

    def save_bg_combat_matches(self, match_rows: list[dict[str, Any]]) -> int:
        if not match_rows:
            return 0
        conn = self._connect()
        params = [
            (
                int(r["match_id"]),
                int(r["profile_id"]),
                str(r["faction"]),
                str(r["mode"]),
                int(r["won"]),
                float(r["gt"]),
                float(r["apm"]),
                float(r["dmg"]),
                float(r["dmg_pm"]),
                float(r["dmg_ratio"]),
                float(r["kd"]),
                float(r["net_wipes"]),
                float(r.get("vkill", 0.0)),
                float(r.get("vlost", 0.0)),
                float(r["pcap_ratio"]),
                float(r.get("pcap_vs_enemy", 1.0)),
                float(r.get("my_elo", 0.0)),
                float(r.get("enemy_avg_elo", 0.0)),
                int(r.get("upset_opp", 0)),
                int(r.get("upset_win", 0)),
                float(r.get("upset_elo_gap", 0.0)),
                int(r["stomp_win"]),
                int(r["completion_ts"]),
            )
            for r in match_rows
        ]
        before_changes = conn.total_changes
        with conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO bg_combat_matches (
                    match_id, profile_id, faction, mode, won, gt, apm,
                    dmg, dmg_pm, dmg_ratio, kd, net_wipes, vkill, vlost,
                    pcap_ratio, pcap_vs_enemy, my_elo, enemy_avg_elo,
                    upset_opp, upset_win, upset_elo_gap, stomp_win, completion_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params,
            )
        return max(0, int(conn.total_changes - before_changes))

    def load_bg_combat_matches(self, since_ts: int = 0) -> list[dict[str, Any]]:
        rows = self._connect().execute(
            """
            SELECT
                match_id, profile_id, faction, mode, won, gt, apm,
                dmg, dmg_pm, dmg_ratio, kd, net_wipes, vkill, vlost,
                pcap_ratio, pcap_vs_enemy, my_elo, enemy_avg_elo,
                upset_opp, upset_win, upset_elo_gap, stomp_win, completion_ts
            FROM bg_combat_matches
            WHERE completion_ts >= ?
            ORDER BY completion_ts ASC, match_id ASC
            """,
            (int(since_ts),),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_old_bg_combat_matches(self, max_age_days: int = 60) -> int:
        """Mantém apenas a janela recente de métricas Automatch de combate para manter o SQLite compacto."""
        cutoff = int(time.time()) - max(30, int(max_age_days)) * 86400
        conn = self._connect()
        with conn:
            cur = conn.execute("DELETE FROM bg_combat_matches WHERE completion_ts < ?", (cutoff,))
            conn.execute("DELETE FROM bg_combat_days WHERE day_ts < ?", (cutoff,))
            return int(cur.rowcount or 0)

