"""SQLite persistence (users, download log, settings, admins).

All public coroutines run the blocking sqlite3 work in a worker thread. Settings
are cached in memory so ``get_setting`` / ``get_int`` / ``get_bool`` are plain
synchronous calls.
"""
from __future__ import annotations

import asyncio
import csv
import functools
import io
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    first_name TEXT,
    last_name  TEXT,
    joined_at  INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    is_banned  INTEGER NOT NULL DEFAULT 0,
    ban_reason TEXT,
    is_blocked INTEGER NOT NULL DEFAULT 0,
    downloads  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS downloads(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    url         TEXT,
    platform    TEXT,
    status      TEXT NOT NULL,
    error       TEXT,
    task_id     TEXT,
    size_bytes  INTEGER DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dl_user_time ON downloads(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dl_time ON downloads(created_at);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS admins(
    user_id  INTEGER PRIMARY KEY,
    added_by INTEGER,
    added_at INTEGER NOT NULL
);
"""

DEFAULT_SETTINGS: dict[str, str] = {
    "maintenance": "0",
    "maintenance_msg": "",
    "force_join": "1",
    "channel_id": "",
    "channel_link": "",
    "daily_limit": "0",       # 0 = unlimited
    "cooldown": "8",          # seconds between requests
    "welcome": "",            # empty = built-in welcome
    "auto_backup": "1",
    "premium_emoji": "1",
    "notify_new_users": "0",
    "last_backup": "0",
    "platform_youtube": "1",
    "platform_tiktok": "1",
    "platform_instagram": "1",
    "platform_facebook": "1",
    "platform_other": "1",
}

REQUIRED_TABLES = {"users", "downloads", "settings", "admins"}


def _aio(fn):
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        return await asyncio.to_thread(fn, self, *args, **kwargs)
    return wrapper


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._settings: dict[str, str] = {}

    # ── lifecycle ─────────────────────────────────────────────────────
    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        self._settings = {**DEFAULT_SETTINGS, **{r["key"]: r["value"] for r in rows}}

    @_aio
    def init(self, defaults: dict[str, str] | None = None) -> None:
        with self._lock:
            self._open()
            for key, value in (defaults or {}).items():
                if not self._settings.get(key):
                    self._write_setting(key, value)

    def close(self) -> None:
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

    # ── settings ──────────────────────────────────────────────────────
    def get_setting(self, key: str) -> str:
        return self._settings.get(key, DEFAULT_SETTINGS.get(key, ""))

    def get_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get_setting(key))
        except ValueError:
            return default

    def get_bool(self, key: str) -> bool:
        return self.get_setting(key) in ("1", "true", "yes", "on")

    def _write_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
            self._conn.commit()
            self._settings[key] = value

    @_aio
    def set_setting(self, key: str, value: Any) -> None:
        self._write_setting(key, str(value))

    # ── users ─────────────────────────────────────────────────────────
    @_aio
    def upsert_user(self, user_id: int, username: str | None, first: str | None, last: str | None) -> bool:
        now = int(time.time())
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row:
                self._conn.execute(
                    "UPDATE users SET username=?, first_name=?, last_name=?, last_seen=?, is_blocked=0 "
                    "WHERE user_id=?", (username, first, last, now, user_id))
            else:
                self._conn.execute(
                    "INSERT INTO users(user_id, username, first_name, last_name, joined_at, last_seen) "
                    "VALUES(?,?,?,?,?,?)", (user_id, username, first, last, now, now))
            self._conn.commit()
            return row is None

    @_aio
    def get_user(self, user_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    @_aio
    def find_user(self, query: str) -> dict | None:
        query = query.strip().lstrip("@")
        with self._lock:
            if query.lstrip("-").isdigit():
                row = self._conn.execute("SELECT * FROM users WHERE user_id=?", (int(query),)).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (query,)).fetchone()
        return dict(row) if row else None

    @_aio
    def set_banned(self, user_id: int, banned: bool, reason: str = "") -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE users SET is_banned=?, ban_reason=? WHERE user_id=?",
                (1 if banned else 0, reason if banned else None, user_id))
            self._conn.commit()
            return cur.rowcount > 0

    @_aio
    def set_blocked(self, user_id: int, blocked: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE users SET is_blocked=? WHERE user_id=?",
                               (1 if blocked else 0, user_id))
            self._conn.commit()

    @_aio
    def broadcast_targets(self, active_days: int | None = None) -> list[int]:
        sql = "SELECT user_id FROM users WHERE is_banned=0 AND is_blocked=0"
        args: list[Any] = []
        if active_days:
            sql += " AND last_seen >= ?"
            args.append(int(time.time()) - active_days * 86400)
        with self._lock:
            return [r[0] for r in self._conn.execute(sql, args).fetchall()]

    @_aio
    def banned_users(self, limit: int = 30) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE is_banned=1 ORDER BY last_seen DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    @_aio
    def top_users(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users WHERE downloads>0 ORDER BY downloads DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    @_aio
    def export_users_csv(self) -> str:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM users ORDER BY joined_at").fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["user_id", "username", "first_name", "last_name", "joined_at", "last_seen",
                         "is_banned", "is_blocked", "downloads"])
        for r in rows:
            writer.writerow([r["user_id"], r["username"] or "", r["first_name"] or "", r["last_name"] or "",
                             r["joined_at"], r["last_seen"], r["is_banned"], r["is_blocked"], r["downloads"]])
        return buf.getvalue()

    # ── downloads ─────────────────────────────────────────────────────
    @_aio
    def record_download(self, user_id: int, url: str, platform: str, ok: bool, error: str = "",
                        task_id: str = "", size_bytes: int = 0, duration_ms: int = 0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO downloads(user_id, url, platform, status, error, task_id, size_bytes, "
                "duration_ms, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (user_id, url[:500], platform, "ok" if ok else "fail", error[:300], task_id,
                 size_bytes, duration_ms, int(time.time())))
            if ok:
                self._conn.execute("UPDATE users SET downloads=downloads+1 WHERE user_id=?", (user_id,))
            self._conn.commit()

    @_aio
    def count_ok_since(self, user_id: int, since: int) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM downloads WHERE user_id=? AND status='ok' AND created_at>=?",
                (user_id, since)).fetchone()[0]

    @_aio
    def recent_downloads(self, limit: int = 15, failed_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM downloads" + (" WHERE status='fail'" if failed_only else "")
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    @_aio
    def clear_logs(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM downloads")
            self._conn.commit()
            return cur.rowcount

    @_aio
    def stats(self, day_start: int) -> dict:
        now = int(time.time())
        with self._lock:
            q = lambda sql, *a: self._conn.execute(sql, a).fetchone()[0]  # noqa: E731
            data = {
                "users": q("SELECT COUNT(*) FROM users"),
                "new_today": q("SELECT COUNT(*) FROM users WHERE joined_at>=?", day_start),
                "active_24h": q("SELECT COUNT(*) FROM users WHERE last_seen>=?", now - 86400),
                "active_7d": q("SELECT COUNT(*) FROM users WHERE last_seen>=?", now - 7 * 86400),
                "banned": q("SELECT COUNT(*) FROM users WHERE is_banned=1"),
                "blocked": q("SELECT COUNT(*) FROM users WHERE is_blocked=1"),
                "dl_ok": q("SELECT COUNT(*) FROM downloads WHERE status='ok'"),
                "dl_fail": q("SELECT COUNT(*) FROM downloads WHERE status='fail'"),
                "dl_today": q("SELECT COUNT(*) FROM downloads WHERE status='ok' AND created_at>=?", day_start),
                "avg_ms": q("SELECT COALESCE(AVG(duration_ms),0) FROM (SELECT duration_ms FROM downloads "
                            "WHERE status='ok' ORDER BY id DESC LIMIT 100)"),
                "bytes": q("SELECT COALESCE(SUM(size_bytes),0) FROM downloads WHERE status='ok'"),
            }
            data["platforms"] = {
                r[0] or "other": r[1] for r in self._conn.execute(
                    "SELECT platform, COUNT(*) FROM downloads WHERE status='ok' GROUP BY platform "
                    "ORDER BY 2 DESC").fetchall()
            }
        return data

    # ── admins ────────────────────────────────────────────────────────
    @_aio
    def list_admins(self) -> list[int]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT user_id FROM admins ORDER BY added_at")]

    @_aio
    def add_admin(self, user_id: int, added_by: int) -> None:
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO admins(user_id, added_by, added_at) VALUES(?,?,?)",
                               (user_id, added_by, int(time.time())))
            self._conn.commit()

    @_aio
    def remove_admin(self, user_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM admins WHERE user_id=?", (user_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ── backup / restore ──────────────────────────────────────────────
    @_aio
    def backup_to(self, dest: Path) -> Path:
        with self._lock:
            out = sqlite3.connect(dest)
            try:
                self._conn.backup(out)
            finally:
                out.close()
        return dest

    @staticmethod
    def validate_file(path: Path) -> dict:
        """Check that ``path`` is a bot database. Returns basic counts."""
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not REQUIRED_TABLES <= tables:
                    raise ValueError("This file is not a bot database backup.")
                return {"users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                        "downloads": conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]}
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise ValueError("This file is not a valid SQLite database.") from exc

    @_aio
    def restore_from(self, src: Path) -> None:
        self.validate_file(src)
        with self._lock:
            self._conn.close()
            shutil.copyfile(src, self.path)
            self._open()

    @_aio
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    @_aio
    def ping(self) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1").fetchone()[0] == 1
