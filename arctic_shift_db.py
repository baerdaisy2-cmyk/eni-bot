"""
arctic_shift_db.py — persist Arctic Shift data into the local/Supabase DB.

Batched: one round trip per call, not one per row.
"""
import time
import db


def _now():
    return int(time.time())


def save_posts(posts):
    """Upsert a list of post dicts. One commit for the whole batch."""
    if not posts:
        return 0
    now = _now()
    rows = [(
        p.get("id"),
        p.get("subreddit"),
        p.get("author"),
        p.get("title"),
        p.get("selftext"),
        p.get("permalink"),
        p.get("url"),
        p.get("created_utc"),
        p.get("score"),
        p.get("num_comments"),
        p.get("removed_by_category"),
        p.get("link_flair_text"),
        now,
    ) for p in posts if p.get("id")]

    if not rows:
        return 0

    con = db.connect()
    try:
        sql = """INSERT INTO as_posts
                 (id, subreddit, author, title, selftext, permalink, url,
                  created_utc, score, num_comments, removed_by_category,
                  link_flair_text, fetched_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(id) DO UPDATE SET
                   score = excluded.score,
                   num_comments = excluded.num_comments,
                   removed_by_category = excluded.removed_by_category,
                   selftext = excluded.selftext,
                   fetched_at = excluded.fetched_at"""
        n = con.executemany(sql, rows)
        con.commit()
        return n
    finally:
        con.close()


def save_comments(comments):
    """Upsert a list of comment dicts. One commit for the whole batch."""
    if not comments:
        return 0
    now = _now()
    rows = [(
        c.get("id"),
        c.get("link_id"),
        c.get("parent_id"),
        c.get("author"),
        c.get("body"),
        c.get("permalink"),
        c.get("created_utc"),
        c.get("score"),
        now,
    ) for c in comments if c.get("id")]

    if not rows:
        return 0

    con = db.connect()
    try:
        sql = """INSERT INTO as_comments
                 (id, post_id, parent_id, author, body, permalink,
                  created_utc, score, fetched_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(id) DO UPDATE SET
                   body = excluded.body,
                   score = excluded.score,
                   fetched_at = excluded.fetched_at"""
        n = con.executemany(sql, rows)
        con.commit()
        return n
    finally:
        con.close()
