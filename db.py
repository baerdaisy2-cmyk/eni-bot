"""db.py — one API, two backends.

No DATABASE_URL  -> sqlite file, same as it has always been.
DATABASE_URL set -> Postgres (Supabase), shared between you and anyone else you invite.

Every query in this file is written with `?` placeholders. On Postgres they are
translated to `%s` on the way out, so the SQL stays readable and there is one
version of every statement rather than two that can drift apart.
"""
import json
import os
import re
import time

import config

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = bool(DATABASE_URL)

if IS_PG:
    import psycopg
    from psycopg.rows import dict_row
else:
    import sqlite3


# ------------------------------------------------------------------ schema --
SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, subreddit TEXT, author TEXT, title TEXT, body TEXT,
  score INTEGER DEFAULT 0, num_comments INTEGER DEFAULT 0, created_utc INTEGER,
  permalink TEXT, first_seen INTEGER, last_seen INTEGER,
  rank_score REAL DEFAULT 0, blacklisted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, post_id TEXT, parent_id TEXT, author TEXT, body TEXT,
  score INTEGER DEFAULT 0, created_utc INTEGER, first_seen INTEGER, last_seen INTEGER,
  is_ours INTEGER DEFAULT 0, mentions_marker INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0, deleted_at INTEGER, delete_reason TEXT,
  owner TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS blacklist (
  kind TEXT, value TEXT, reason TEXT, created_at INTEGER,
  PRIMARY KEY (kind, value)
);
CREATE TABLE IF NOT EXISTS logs (
  ts INTEGER, level TEXT, actor TEXT, message TEXT, context TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_posts_rank ON posts(rank_score DESC);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);

CREATE TABLE IF NOT EXISTS as_posts (
    id                  TEXT PRIMARY KEY,
    subreddit           TEXT,
    author              TEXT,
    title               TEXT,
    selftext            TEXT,
    permalink           TEXT,
    url                 TEXT,
    created_utc         INTEGER,
    score               INTEGER,
    num_comments        INTEGER,
    removed_by_category TEXT,
    link_flair_text     TEXT,
    fetched_at          INTEGER
);

CREATE TABLE IF NOT EXISTS as_comments (
    id          TEXT PRIMARY KEY,
    post_id     TEXT,
    parent_id   TEXT,
    author      TEXT,
    body        TEXT,
    permalink   TEXT,
    created_utc INTEGER,
    score       INTEGER,
    fetched_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_as_posts_subreddit   ON as_posts(subreddit);
CREATE INDEX IF NOT EXISTS idx_as_posts_created     ON as_posts(created_utc);
CREATE INDEX IF NOT EXISTS idx_as_posts_removed     ON as_posts(removed_by_category);
CREATE INDEX IF NOT EXISTS idx_as_comments_post     ON as_comments(post_id);
CREATE INDEX IF NOT EXISTS idx_as_comments_author   ON as_comments(author);
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id TEXT PRIMARY KEY, subreddit TEXT, author TEXT, title TEXT, body TEXT,
  score INTEGER DEFAULT 0, num_comments INTEGER DEFAULT 0, created_utc BIGINT,
  permalink TEXT, first_seen BIGINT, last_seen BIGINT,
  rank_score DOUBLE PRECISION DEFAULT 0, blacklisted INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS comments (
  id TEXT PRIMARY KEY, post_id TEXT, parent_id TEXT, author TEXT, body TEXT,
  score INTEGER DEFAULT 0, created_utc BIGINT, first_seen BIGINT, last_seen BIGINT,
  is_ours INTEGER DEFAULT 0, mentions_marker INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0, deleted_at BIGINT, delete_reason TEXT,
  owner TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS blacklist (
  kind TEXT, value TEXT, reason TEXT, created_at BIGINT,
  PRIMARY KEY (kind, value)
);
CREATE TABLE IF NOT EXISTS logs (
  ts BIGINT, level TEXT, actor TEXT, message TEXT, context TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_posts_rank ON posts(rank_score DESC);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);

CREATE TABLE IF NOT EXISTS as_posts (
    id                  TEXT PRIMARY KEY,
    subreddit           TEXT,
    author              TEXT,
    title               TEXT,
    selftext            TEXT,
    permalink           TEXT,
    url                 TEXT,
    created_utc         INTEGER,
    score               INTEGER,
    num_comments        INTEGER,
    removed_by_category TEXT,
    link_flair_text     TEXT,
    fetched_at          INTEGER
);

CREATE TABLE IF NOT EXISTS as_comments (
    id          TEXT PRIMARY KEY,
    post_id     TEXT,
    parent_id   TEXT,
    author      TEXT,
    body        TEXT,
    permalink   TEXT,
    created_utc INTEGER,
    score       INTEGER,
    fetched_at  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_as_posts_subreddit   ON as_posts(subreddit);
CREATE INDEX IF NOT EXISTS idx_as_posts_created     ON as_posts(created_utc);
CREATE INDEX IF NOT EXISTS idx_as_posts_removed     ON as_posts(removed_by_category);
CREATE INDEX IF NOT EXISTS idx_as_comments_post     ON as_comments(post_id);
CREATE INDEX IF NOT EXISTS idx_as_comments_author   ON as_comments(author);
"""

_PLACEHOLDER = re.compile(r"\?")


class Cursor:
    """Thin wrapper so callers keep writing `?` and keep calling .fetchone()[0]."""

    def __init__(self, inner):
        self._inner = inner

    def fetchone(self):
        return self._inner.fetchone()

    def fetchall(self):
        return self._inner.fetchall()

    def __iter__(self):
        return iter(self._inner)


class Connection:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, args=()):
        if IS_PG:
            cur = self._raw.cursor()
            cur.execute(_PLACEHOLDER.sub("%s", sql), tuple(args))
            return Cursor(cur)
        return Cursor(self._raw.execute(sql, args))

    def executemany(self, sql, rows):
        """Batch execute. One round trip for the whole list of rows."""
        rows = [tuple(r) for r in rows if r]
        if not rows:
            return 0
        if IS_PG:
            cur = self._raw.cursor()
            cur.executemany(_PLACEHOLDER.sub("%s", sql), rows)
            cur.close()
        else:
            self._raw.executemany(sql, rows)
        return len(rows)

    def executescript(self, sql):
        if IS_PG:
            with self._raw.cursor() as cur:
                cur.execute(sql)
            return
        self._raw.executescript(sql)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def connect():
    if IS_PG:
        return Connection(psycopg.connect(DATABASE_URL, row_factory=dict_row,
                                          connect_timeout=15))
    raw = sqlite3.connect(config.DB_PATH, timeout=30)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    return Connection(raw)


def backend():
    return "postgres" if IS_PG else "sqlite"


def init():
    con = connect()
    con.executescript(PG_SCHEMA if IS_PG else SQLITE_SCHEMA)
    # older sqlite files predate the owner column
    if not IS_PG:
        cols = [r["name"] for r in con.execute("PRAGMA table_info(comments)")]
        if "owner" not in cols:
            con.execute("ALTER TABLE comments ADD COLUMN owner TEXT DEFAULT ''")
    con.commit()
    con.close()


# -------------------------------------------------------------------- write --
def log(level, actor, message, context=None):
    con = connect()
    con.execute(
        "INSERT INTO logs (ts, level, actor, message, context) VALUES (?,?,?,?,?)",
        (int(time.time()), level.upper(), actor, message,
         json.dumps(context or {}, ensure_ascii=False)),
    )
    con.commit()
    con.close()


def upsert_post(p):
    now = int(time.time())
    con = connect()
    con.execute(
        """INSERT INTO posts (id, subreddit, author, title, body, score, num_comments,
             created_utc, permalink, first_seen, last_seen)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             score=excluded.score, num_comments=excluded.num_comments,
             title=excluded.title, body=excluded.body, last_seen=excluded.last_seen""",
        (p["id"], p["subreddit"], p["author"], p["title"], p.get("body", ""),
         p.get("score", 0), p.get("num_comments", 0), p.get("created_utc", now),
         p.get("permalink", ""), now, now),
    )
    con.commit()
    con.close()


def upsert_comment(c):
    now = int(time.time())
    con = connect()
    con.execute(
        """INSERT INTO comments (id, post_id, parent_id, author, body, score, created_utc,
             first_seen, last_seen, is_ours, mentions_marker, deleted, deleted_at,
             delete_reason, owner)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,?)
           ON CONFLICT(id) DO UPDATE SET
             body=excluded.body, score=excluded.score, last_seen=excluded.last_seen,
             is_ours=excluded.is_ours, mentions_marker=excluded.mentions_marker,
             owner=excluded.owner,
             deleted=0, deleted_at=NULL, delete_reason=NULL""",
        (c["id"], c["post_id"], c.get("parent_id", ""), c["author"], c.get("body", ""),
         c.get("score", 0), c.get("created_utc", now), now, now,
         int(c.get("is_ours", 0)), int(c.get("mentions_marker", 0)),
         c.get("owner", "")),
    )
    con.commit()
    con.close()


def blacklist_add(kind, value, reason):
    con = connect()
    con.execute(
        "INSERT INTO blacklist (kind, value, reason, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(kind, value) DO NOTHING",
        (kind, value, reason, int(time.time())),
    )
    if kind == "post":
        con.execute("UPDATE posts SET blacklisted=1, rank_score=0 WHERE id=?", (value,))
    elif kind == "subreddit":
        con.execute("UPDATE posts SET blacklisted=1, rank_score=0 WHERE subreddit=?", (value,))
    con.commit()
    con.close()


# --------------------------------------------------------------------- read --
def is_blacklisted(kind, value):
    con = connect()
    row = con.execute("SELECT 1 AS hit FROM blacklist WHERE kind=? AND value=?",
                      (kind, value)).fetchone()
    con.close()
    return row is not None


def stats():
    con = connect()

    def count(sql):
        row = con.execute(sql).fetchone()
        return list(row.values())[0] if isinstance(row, dict) else row[0]

    out = {
        "posts": count("SELECT COUNT(*) FROM posts"),
        "comments": count("SELECT COUNT(*) FROM comments"),
        "ours": count("SELECT COUNT(*) FROM comments WHERE is_ours=1"),
        "deleted": count("SELECT COUNT(*) FROM comments WHERE deleted=1"),
        "blacklisted_posts": count("SELECT COUNT(*) FROM blacklist WHERE kind='post'"),
        "blacklisted_subs": count("SELECT COUNT(*) FROM blacklist WHERE kind='subreddit'"),
    }
    con.close()
    return out


def recent_logs(limit=8, level=None):
    con = connect()
    if level:
        rows = con.execute("SELECT * FROM logs WHERE level=? ORDER BY ts DESC LIMIT ?",
                           (level, limit)).fetchall()
    else:
        rows = con.execute("SELECT * FROM logs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]
