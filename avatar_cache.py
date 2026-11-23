"""Persistência SQLite leve dedicada ao Ranking Unificado BR (estado do painel + cache de avatares Steam)."""

from __future__ import annotations

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
