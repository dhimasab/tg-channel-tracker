"""SQLite state store (PRD §6.2, §20 poin 1).

Aturan penting:
- sqlite3 connection TIDAK thread-safe. Tiap worker bikin koneksi sendiri lewat
  `connect()`; jangan diwariskan dari parent.
- `PRAGMA journal_mode=WAL` supaya baca/tulis bareng aman.
- Dedup gratis: PRIMARY KEY (username, msg_id) + INSERT OR IGNORE.
- `last_msg_id` = nilai tertinggi yang pernah terlihat. Jangan pakai timestamp:
  msg_id selalu naik, timestamp bisa sama dan bisa mundur saat post dihapus.
- `notified_at IS NULL` = belum terkirim -> itu antrean recovery (§8.5).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Channel, Post

log = logging.getLogger("tracker.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    username   TEXT PRIMARY KEY,
    label      TEXT,
    enabled    INTEGER DEFAULT 1,
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS posts (
    username    TEXT    NOT NULL,
    msg_id      INTEGER NOT NULL,
    posted_at   TEXT,
    text        TEXT,
    links       TEXT,
    media_url   TEXT,
    cas         TEXT,
    seen_at     TEXT NOT NULL,
    notified_at TEXT,
    PRIMARY KEY (username, msg_id)
);

CREATE TABLE IF NOT EXISTS channel_state (
    username             TEXT PRIMARY KEY,
    last_msg_id          INTEGER NOT NULL DEFAULT 0,
    last_poll_at         TEXT,
    last_success_at      TEXT,
    last_error           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_posts          INTEGER NOT NULL DEFAULT 0,
    total_sent           INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS send_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    msg_id   INTEGER,
    sent_at  TEXT,
    status   TEXT,
    error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_posts_pending ON posts(notified_at);
CREATE INDEX IF NOT EXISTS idx_posts_posted  ON posts(posted_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Koneksi baru. Panggil di dalam thread yang memakainya."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def init(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    init_schema(conn)
    return conn


# ------------------------------------------------------------------ channels
def sync_channels(conn: sqlite3.Connection, channels: list[Channel]) -> None:
    """Mirror channels.json ke tabel channels (buat historis & join stats)."""
    now = utcnow()
    for ch in channels:
        conn.execute(
            """INSERT INTO channels (username, label, enabled, added_at)
               VALUES (?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET
                   label   = excluded.label,
                   enabled = excluded.enabled""",
            (ch.username, ch.label, 1 if ch.enabled else 0, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO channel_state (username) VALUES (?)",
            (ch.username,),
        )
    conn.commit()


# --------------------------------------------------------------------- state
def get_state(conn: sqlite3.Connection, username: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM channel_state WHERE username=?", (username,)
    ).fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO channel_state (username) VALUES (?)", (username,))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM channel_state WHERE username=?", (username,)
        ).fetchone()
    return row


def record_success(
    conn: sqlite3.Connection, username: str, newest_id: int, added_posts: int
) -> None:
    now = utcnow()
    conn.execute(
        """UPDATE channel_state SET
               last_msg_id          = MAX(COALESCE(last_msg_id, 0), ?),
               last_poll_at         = ?,
               last_success_at      = ?,
               last_error           = NULL,
               consecutive_failures = 0,
               total_posts          = total_posts + ?
           WHERE username = ?""",
        (newest_id, now, now, added_posts, username),
    )
    conn.commit()


def record_failure(conn: sqlite3.Connection, username: str, error: str) -> int:
    """Catat error. Return jumlah kegagalan beruntun setelah update."""
    conn.execute(
        """UPDATE channel_state SET
               last_poll_at         = ?,
               last_error           = ?,
               consecutive_failures = consecutive_failures + 1
           WHERE username = ?""",
        (utcnow(), error[:500], username),
    )
    conn.commit()
    row = conn.execute(
        "SELECT consecutive_failures FROM channel_state WHERE username=?", (username,)
    ).fetchone()
    return int(row[0]) if row else 1


def bump_sent(conn: sqlite3.Connection, username: str) -> None:
    conn.execute(
        "UPDATE channel_state SET total_sent = total_sent + 1 WHERE username = ?",
        (username,),
    )


# --------------------------------------------------------------------- posts
def insert_posts(conn: sqlite3.Connection, posts: list[Post]) -> list[int]:
    """INSERT OR IGNORE. Return daftar msg_id yang benar-benar baru masuk."""
    added: list[int] = []
    now = utcnow()
    for p in posts:
        cur = conn.execute(
            """INSERT OR IGNORE INTO posts
                   (username, msg_id, posted_at, text, links, media_url, cas, seen_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                p.username,
                p.msg_id,
                p.posted_at,
                p.text,
                json.dumps(p.links),
                p.media_url,
                json.dumps(p.cas),
                now,
            ),
        )
        if cur.rowcount and cur.rowcount > 0:
            added.append(p.msg_id)
    conn.commit()
    return added


def pending_posts(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Post yang belum terkirim, terlama dulu (PRD §8.5)."""
    return conn.execute(
        """SELECT username, msg_id, posted_at, text, links, media_url, cas
             FROM posts
            WHERE notified_at IS NULL
            ORDER BY COALESCE(posted_at, seen_at) ASC, msg_id ASC
            LIMIT ?""",
        (limit,),
    ).fetchall()


def pending_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM posts WHERE notified_at IS NULL"
    ).fetchone()[0])


def pending_ids(conn: sqlite3.Connection, username: str) -> list[int]:
    """msg_id yang belum terkirim untuk satu channel."""
    return [
        int(r[0]) for r in conn.execute(
            "SELECT msg_id FROM posts WHERE username = ? AND notified_at IS NULL",
            (username,),
        )
    ]


def mark_notified(
    conn: sqlite3.Connection,
    username: str,
    msg_id: int,
    status: str,
    error: str | None = None,
) -> None:
    """Tandai post sudah diproses ('ok' | 'failed' | 'skipped') + audit ke send_log.

    'failed' tetap di-set notified_at supaya tidak di-retry selamanya; kegagalan
    permanen (403/400) memang tidak akan berhasil kalau diulang.
    """
    now = utcnow()
    conn.execute(
        "UPDATE posts SET notified_at = ? WHERE username = ? AND msg_id = ?",
        (now, username, msg_id),
    )
    conn.execute(
        """INSERT INTO send_log (username, msg_id, sent_at, status, error)
           VALUES (?,?,?,?,?)""",
        (username, msg_id, now, status, (error or "")[:500] or None),
    )
    conn.commit()


def mark_skipped(conn: sqlite3.Connection, username: str, msg_ids: list[int]) -> int:
    """Tandai sekumpulan post sebagai 'sudah dianggap terkirim' tanpa mengirim.

    Dipakai guard run pertama (PRD §8.3): channel baru didaftarkan -> 20 post lama
    disimpan tapi tidak dikirim.
    """
    if not msg_ids:
        return 0
    now = utcnow()
    conn.executemany(
        "UPDATE posts SET notified_at = ? WHERE username = ? AND msg_id = ?",
        [(now, username, mid) for mid in msg_ids],
    )
    conn.executemany(
        """INSERT INTO send_log (username, msg_id, sent_at, status, error)
           VALUES (?,?,?,'skipped','backfill run pertama')""",
        [(username, mid, now) for mid in msg_ids],
    )
    conn.commit()
    return len(msg_ids)


# --------------------------------------------------------------------- stats
def stats_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT s.username,
                  COALESCE(c.label, s.username) AS label,
                  s.last_msg_id, s.total_posts, s.total_sent,
                  s.consecutive_failures, s.last_poll_at, s.last_success_at, s.last_error,
                  (SELECT COUNT(*) FROM posts p
                    WHERE p.username = s.username AND p.notified_at IS NULL) AS pending
             FROM channel_state s
             LEFT JOIN channels c ON c.username = s.username
            ORDER BY s.total_posts DESC, s.username"""
    ).fetchall()


def total_posts(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0])


def recent_send_errors(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT username, msg_id, sent_at, status, error
             FROM send_log
            WHERE status = 'failed'
            ORDER BY id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
