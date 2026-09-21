"""scraper.py — talks to the local reddit-universal-scraper and enriches its output.

Post and comment data comes ONLY from the local scraper at SCRAPER_BASE.
Account age and avatar come from reddit.com/user/<name>/about.json, cached so a
username is never fetched twice. If that endpoint refuses (it is gated for
anonymous callers), age is None and a generated avatar is used. No retries, no
user-agent rotation, no mirror hopping.
"""
import json
import os
import re
import time

import requests

import config
import db

SCRAPER_BASE = os.environ.get("SCRAPER_BASE", "http://localhost:8000").rstrip("/")
ABOUT_URL = "https://www.reddit.com/user/%s/about.json"
UA = os.environ.get("ENI_USER_AGENT", "eni-monitor/1.0 (personal dashboard)")
TIMEOUT = int(os.environ.get("SCRAPER_TIMEOUT", "12"))
CACHE_TTL = 14 * 86400

POST_ID = re.compile(r"/comments/([A-Za-z0-9]+)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS user_cache (
  username TEXT PRIMARY KEY, avatar TEXT, account_age_days INTEGER,
  account_created_utc BIGINT, fetched_at BIGINT, ok INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS scrapes (
  post_id TEXT PRIMARY KEY, url TEXT, subreddit TEXT, title TEXT,
  payload TEXT, scraped_at BIGINT
);
CREATE TABLE IF NOT EXISTS our_ranks (
  comment_id TEXT PRIMARY KEY, post_id TEXT, subreddit TEXT,
  rank INTEGER, upvotes INTEGER, seen_at BIGINT
);
CREATE TABLE IF NOT EXISTS keywords (
  word TEXT PRIMARY KEY, added_at BIGINT, last_detected BIGINT, matches INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS keyword_hits (
  hit_id TEXT PRIMARY KEY, word TEXT, subreddit TEXT, post_id TEXT,
  comment_id TEXT, author TEXT, body TEXT, found_at BIGINT
);
CREATE INDEX IF NOT EXISTS idx_hits_word ON keyword_hits(word);
CREATE INDEX IF NOT EXISTS idx_ranks_post ON our_ranks(post_id);
"""

MAX_KEYWORDS = 10


def init():
    con = db.connect()
    con.executescript(SCHEMA)
    con.commit()
    con.close()


def one(row):
    if row is None:
        return 0
    return list(row.values())[0] if isinstance(row, dict) else row[0]


def iso(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


# ------------------------------------------------------------------ status --
_STATUS_CACHE = {"at": 0, "value": None}
STATUS_TTL = int(os.environ.get("SCRAPER_STATUS_TTL", "60"))


def status(force=False):
    """Report the current scrape source. Always Arctic Shift now."""
    now = int(time.time())
    out = {
        "ok": True,
        "base": "arctic-shift.photon-reddit.com",
        "note": "using Arctic Shift archive (no API key, no auth)",
    }
    _STATUS_CACHE.update({"at": now, "value": out})
    return out



def disabled():
    """The scrape path uses Arctic Shift directly. No external service needed."""
    return False



def _get(path, params=None):
    if disabled():
        raise LookupError(
            "The scraper is switched off. Set SCRAPER_BASE in .env to a running "
            "source to turn it back on.")
    r = requests.get(SCRAPER_BASE + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


# -------------------------------------------------------------- enrichment --
def fallback_avatar(username):
    return "https://api.dicebear.com/7.x/thumbs/svg?seed=" + requests.utils.quote(
        username or "anon")


def enrich_user(username):
    """Return {username, avatar, account_age_days, account_created_utc}. Cached."""
    name = (username or "").strip()
    if not name or name.lower() in ("[deleted]", "[removed]", "none"):
        return {"username": "[deleted]", "avatar": fallback_avatar("deleted"),
                "account_age_days": None, "account_created_utc": None}

    now = int(time.time())
    con = db.connect()
    row = con.execute("SELECT * FROM user_cache WHERE username=?", (name,)).fetchone()
    con.close()
    if row and (now - (row["fetched_at"] or 0)) < CACHE_TTL:
        return {"username": name, "avatar": row["avatar"] or fallback_avatar(name),
                "account_age_days": row["account_age_days"],
                "account_created_utc": row["account_created_utc"]}

    avatar, age, created, ok = fallback_avatar(name), None, None, 0
    try:
        r = requests.get(ABOUT_URL % requests.utils.quote(name),
                         headers={"User-Agent": UA}, timeout=8)
        if r.status_code == 200:
            d = (r.json() or {}).get("data", {})
            created = int(d.get("created_utc") or 0) or None
            if created:
                age = int((now - created) / 86400)
            raw = d.get("snoovatar_img") or d.get("icon_img") or ""
            if raw:
                avatar = raw.split("?")[0]
            ok = 1
        elif r.status_code == 404:
            return {"username": "[deleted]", "avatar": fallback_avatar("deleted"),
                    "account_age_days": None, "account_created_utc": None}
        # 403 / 429 / anything else: keep the fallback, do not retry
    except Exception:
        pass

    con = db.connect()
    con.execute(
        "INSERT INTO user_cache (username, avatar, account_age_days, account_created_utc,"
        " fetched_at, ok) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(username) DO UPDATE SET avatar=excluded.avatar,"
        " account_age_days=excluded.account_age_days,"
        " account_created_utc=excluded.account_created_utc,"
        " fetched_at=excluded.fetched_at, ok=excluded.ok",
        (name, avatar, age, created, now, ok))
    con.commit()
    con.close()
    return {"username": name, "avatar": avatar,
            "account_age_days": age, "account_created_utc": created}


def enrichment_working():
    """True if any cached lookup actually succeeded recently."""
    con = db.connect()
    n = one(con.execute("SELECT COUNT(*) FROM user_cache WHERE ok=1").fetchone())
    total = one(con.execute("SELECT COUNT(*) FROM user_cache").fetchone())
    con.close()
    return n > 0, n, total


# ------------------------------------------------------------------ scrape --
def post_id_from(url_or_id):
    m = POST_ID.search(url_or_id or "")
    if m:
        return m.group(1)
    clean = (url_or_id or "").strip().strip("/")
    return clean.split("/")[-1] if clean else ""


def is_deleted(body, author):
    b = (body or "").strip().lower()
    a = (author or "").strip().lower()
    return b in ("[deleted]", "[removed]") or a in ("[deleted]", "[removed]")


def _ours(author, body):
    # auto-detect if author is our configured Reddit username
    try:
        import config as _cfg
        _me = getattr(_cfg, 'REDDIT_USERNAME', '') or ''
        if _me and author and author.lower().lstrip('u/') == _me.lower().lstrip('u/'):
            return True
    except Exception:
        pass
    a = (author or "").lower()
    if a and a in [x.lower() for x in config.OUR_AUTHORS]:
        return True
    low = (body or "").lower()
    return any(m.lower() in low for m in config.MARKERS)


def scrape(url):
    """Fetch a post + comments from Arctic Shift, enrich, store, return payload."""
    pid = post_id_from(url)
    if not pid:
        raise ValueError("could not find a post id in that URL")

    import arctic_shift
    import arctic_shift_db

    post = arctic_shift.fetch_post(pid)
    if post is None:
        raise LookupError("Arctic Shift has no record of post %s yet" % pid)

    tree = arctic_shift.fetch_thread(pid)
    flat = arctic_shift.flatten_comments(tree)

    arctic_shift_db.save_posts([post])
    arctic_shift_db.save_comments(flat)

    raw = [{
        "comment_id": c.get("id"),
        "id": c.get("id"),
        "author": c.get("author"),
        "body": c.get("body") or "",
        "score": c.get("score") or 0,
        "created_utc": c.get("created_utc"),
    } for c in flat]
    raw = sorted(raw, key=lambda c: int(c.get("score") or 0), reverse=True)

    op = enrich_user(post.get("author"))
    sub = post.get("subreddit") or ""
    now = int(time.time())

    comments, seen_users = [], {}
    for i, c in enumerate(raw, 1):
        name = c.get("author")
        if name not in seen_users:
            seen_users[name] = enrich_user(name)
        body = c.get("body") or ""
        comments.append({
            "rank": i,
            "id": c.get("comment_id") or c.get("id") or "c%03d" % i,
            "content": body,
            "upvotes": int(c.get("score") or 0),
            "author": seen_users[name],
            "is_deleted": is_deleted(body, name),
            "posted_at": iso(c.get("created_utc")),
        })

    payload = {
        "scraped_post": {
            "url": post.get("permalink") or url,
            "title": post.get("title") or "",
            "subreddit": sub,
            "author": op,
            "upvotes": int(post.get("score") or 0),
            "comment_count": int(post.get("num_comments") or len(comments)),
            "scraped_at": iso(now),
        },
        "comments": comments,
    }

    con = db.connect()
    con.execute(
        "INSERT INTO scrapes (post_id, url, subreddit, title, payload, scraped_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET "
        "payload=excluded.payload, scraped_at=excluded.scraped_at, title=excluded.title",
        (pid, payload["scraped_post"]["url"], sub, payload["scraped_post"]["title"],
         json.dumps(payload), now))
    for c in comments:
        if _ours(c["author"]["username"], c["content"]):
            con.execute(
                "INSERT INTO our_ranks (comment_id, post_id, subreddit, rank, upvotes, seen_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(comment_id) DO UPDATE SET "
                "rank=excluded.rank, upvotes=excluded.upvotes, seen_at=excluded.seen_at",
                (c["id"], pid, sub, c["rank"], c["upvotes"], now))
    con.commit()
    con.close()

    db.log("INFO", "scraper",
           "scraped post %s from r/%s: %d comments, %d removed" % (
               pid, sub or "?", len(comments),
               sum(1 for c in comments if c["is_deleted"])),
           {"post_id": pid, "subreddit": sub, "comments": len(comments)})
    scan_keywords_in(comments, sub, pid)
    return payload


def last_scrape():
    con = db.connect()
    row = con.execute("SELECT payload FROM scrapes ORDER BY scraped_at DESC LIMIT 1").fetchone()
    con.close()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


# ---------------------------------------------------------------- keywords --
def keywords():
    con = db.connect()
    rows = con.execute("SELECT * FROM keywords ORDER BY added_at ASC").fetchall()
    con.close()
    return [dict(r) for r in rows]


def add_keyword(word):
    word = (word or "").strip().lower()
    if not word:
        return False, "empty keyword"
    if len(keywords()) >= MAX_KEYWORDS:
        return False, "limit is %d keywords" % MAX_KEYWORDS
    con = db.connect()
    con.execute("INSERT INTO keywords (word, added_at, last_detected, matches) "
                "VALUES (?,?,NULL,0) ON CONFLICT(word) DO NOTHING",
                (word, int(time.time())))
    con.commit()
    con.close()
    return True, word


def remove_keyword(word):
    con = db.connect()
    con.execute("DELETE FROM keywords WHERE word=?", (word,))
    con.execute("DELETE FROM keyword_hits WHERE word=?", (word,))
    con.commit()
    con.close()


def _record_hit(word, sub, post_id, comment_id, author, body):
    """Insert a keyword hit in one round trip. Idempotent on hit_id."""
    import hashlib
    key = "%s|%s|%s|%s|%s" % (word, sub, post_id, comment_id or "", author or "")
    hid = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    now = int(time.time())
    con = db.connect()
    try:
        con.execute(
            """INSERT INTO keyword_hits
               (hit_id, word, subreddit, post_id, comment_id, author, body, found_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(hit_id) DO NOTHING""",
            (hid, word, sub or "", post_id or "", comment_id or "",
             author or "", (body or "")[:600], now),
        )
        con.commit()
    finally:
        con.close()
    return True



def scan_keywords_in(comments, sub, post_id):
    """Check an already-fetched comment list against the watch words."""
    words = [k["word"] for k in keywords()]
    found = 0
    for c in comments:
        low = (c.get("content") or "").lower()
        for w in words:
            if w in low:
                if _record_hit(w, sub, post_id, c.get("id"),
                               c.get("author", {}).get("username"), c.get("content")):
                    found += 1
    if found:
        db.log("WARN", "scraper", "%d new keyword match(es) in r/%s" % (found, sub or "?"),
               {"subreddit": sub, "post_id": post_id, "new_matches": found})
    return found


def sweep_keywords():
    """Search each stored keyword within the configured subreddits, via Arctic Shift."""
    words = [k["word"] for k in keywords()]
    if not words:
        return 0, "no keywords set"

    subs_raw = os.environ.get("SWEEP_SUBS", "").strip()
    if not subs_raw:
        try:
            con = db.connect()
            rows = con.execute(
                "SELECT DISTINCT subreddit FROM as_posts WHERE subreddit IS NOT NULL"
            ).fetchall()
            con.close()
            subs = [r["subreddit"] for r in rows]
        except Exception:
            subs = []
    else:
        subs = [s.strip() for s in subs_raw.split(",") if s.strip()]

    if not subs:
        return 0, "no subreddits to sweep (set SWEEP_SUBS in .env)"

    import arctic_shift

    MIN_LEN = 4
    found = 0
    errors = 0
    skipped = 0
    last_err = ""

    for w in words:
        if len(w) < MIN_LEN:
            skipped += 1
            db.log("INFO", "scraper",
                   "skipping %r: too short for Arctic Shift (min %d chars)"
                   % (w, MIN_LEN), {"keyword": w})
            continue

        for sub in subs:
            try:
                posts = arctic_shift.search_posts(subreddit=sub, q=w, limit=25)
            except Exception as exc:
                errors += 1
                last_err = str(exc)[:120]
                db.log("WARN", "scraper", "sweep %r in r/%s failed: %s"
                       % (w, sub, last_err), {"keyword": w, "subreddit": sub})
                continue

            if not posts:
                continue

            try:
                import arctic_shift_db
                arctic_shift_db.save_posts(posts)
            except Exception as exc:
                db.log("WARN", "scraper", "could not cache sweep posts for %r: %s"
                       % (w, str(exc)[:120]), {"keyword": w})

            for p in posts:
                psub = p.get("subreddit") or sub
                pid = p.get("id") or ""
                author = p.get("author") or ""
                text = p.get("title") or ""
                if _record_hit(w, psub, pid, "", author, text):
                    found += 1

    tail = ""
    if skipped:
        tail += " (%d too short)" % skipped
    if errors:
        tail += " (%d error%s)" % (errors, "" if errors == 1 else "s")
    if found == 0 and errors and not skipped:
        return 0, "0 hits, last error: %s" % last_err
    return found, "checked %d keyword(s) across %d subreddit(s)%s" % (
        len(words), len(subs), tail)



def keyword_hits(limit=80):
    con = db.connect()
    rows = con.execute("SELECT * FROM keyword_hits ORDER BY found_at DESC LIMIT ?",
                       (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------- stats --
def our_tracking():
    con = db.connect()
    made = one(con.execute("SELECT COUNT(*) FROM comments WHERE is_ours=1").fetchone())
    r1 = one(con.execute("SELECT COUNT(*) FROM our_ranks WHERE rank=1").fetchone())
    rm = one(con.execute("SELECT COUNT(*) FROM our_ranks WHERE rank BETWEEN 2 AND 5").fetchone())
    rl = one(con.execute("SELECT COUNT(*) FROM our_ranks WHERE rank > 5").fetchone())
    con.close()
    return {"total_comments_made": made, "rank_1_count": r1,
            "rank_medium_count": rm, "rank_low_count": rl}


def deleted_stats():
    """Deletions from our own watchlist plus anything the scraper saw removed."""
    con = db.connect()
    ours_total = one(con.execute("SELECT COUNT(*) FROM comments WHERE is_ours=1").fetchone())
    ours_gone = one(con.execute(
        "SELECT COUNT(*) FROM comments WHERE is_ours=1 AND deleted=1").fetchone())
    rows = con.execute(
        """SELECT p.subreddit AS sub, COUNT(*) AS total, SUM(c.deleted) AS gone
           FROM comments c JOIN posts p ON p.id = c.post_id
           WHERE c.is_ours=1 AND p.subreddit != '' GROUP BY p.subreddit""").fetchall()
    scraped = con.execute("SELECT subreddit, payload FROM scrapes").fetchall()
    con.close()

    tally = {}
    for r in rows:
        tally[r["sub"]] = {"deletions": r["gone"] or 0, "total": r["total"] or 0,
                           "source": "watchlist"}
    for s in scraped:
        try:
            data = json.loads(s["payload"])
        except Exception:
            continue
        sub = s["subreddit"] or ""
        t = tally.setdefault(sub, {"deletions": 0, "total": 0, "source": "scrape"})
        for c in data.get("comments", []):
            t["total"] += 1
            if c.get("is_deleted"):
                t["deletions"] += 1

    aggressive = []
    for sub, t in tally.items():
        if not sub or not t["total"]:
            continue
        aggressive.append({
            "subreddit": sub,
            "deletions": t["deletions"],
            "total": t["total"],
            "aggression_score": round(t["deletions"] / float(t["total"]), 2),
        })
    aggressive.sort(key=lambda a: (a["aggression_score"], a["deletions"]), reverse=True)

    total_deleted = ours_gone + sum(
        a["deletions"] for a in aggressive if a["subreddit"] not in
        [r["sub"] for r in rows])
    rate = round(ours_gone / float(ours_total), 3) if ours_total else 0.0
    return {"total_deleted": total_deleted, "deletion_rate": rate,
            "aggressive_subreddits": aggressive}


def top_performers():
    """Everyone who has added comments through this app, ranked by volume.

    Returns per-user: username, avatar, account_age_days, comments_made,
    live_count, gone_count, survival_rate,
    mentions_lemonrealm, mentions_prv8sup, mentions_total,
    rank_1, rank_2, rank_3, rank_other."""
    con = db.connect()
    try:
        base = con.execute(
            """SELECT owner AS username,
                      COUNT(*) AS made,
                      SUM(CASE WHEN deleted=1 THEN 1 ELSE 0 END) AS gone
               FROM comments
               WHERE is_ours=1 AND owner != ''
               GROUP BY owner
               ORDER BY COUNT(*) DESC"""
        ).fetchall()

        # per-user rank breakdown
        rank_rows = con.execute(
            """SELECT c.owner AS username, r.rank AS rank, COUNT(*) AS n
               FROM our_ranks r
               JOIN comments c ON c.id = r.comment_id
               WHERE c.is_ours = 1 AND c.owner != ''
               GROUP BY c.owner, r.rank"""
        ).fetchall()

        # per-user brand mentions from keyword_hits
        mention_rows = con.execute(
            """SELECT author AS username, word, COUNT(*) AS n
               FROM keyword_hits
               WHERE author IS NOT NULL AND author != ''
               GROUP BY author, word"""
        ).fetchall()

        cache = {}
        for r in con.execute("SELECT * FROM user_cache").fetchall():
            cache[r["username"]] = r
    finally:
        con.close()

    ranks_by_user = {}
    for r in rank_rows:
        ranks_by_user.setdefault(r["username"], {})[r["rank"]] = r["n"]

    mentions_by_user = {}
    for r in mention_rows:
        mentions_by_user.setdefault(r["username"], {})[r["word"]] = r["n"]

    out = []
    for r in base:
        name = r["username"]
        made = r["made"] or 0
        gone = r["gone"] or 0
        live = made - gone
        survival = round((live / float(made)) * 100) if made else 0

        ranks = ranks_by_user.get(name, {})
        r1 = ranks.get(1, 0)
        r2 = ranks.get(2, 0)
        r3 = ranks.get(3, 0)
        r_other = sum(v for k, v in ranks.items() if k > 3)

        mentions = mentions_by_user.get(name, {})
        m_lem = mentions.get("lemonrealm", 0)
        m_prv = mentions.get("prv8sup", 0)
        m_total = sum(mentions.values())

        c = cache.get(name, {})

        out.append({
            "username": name,
            "avatar": c.get("avatar") or fallback_avatar(name),
            "account_age_days": c.get("account_age_days"),
            "comments_made": made,
            "live_count": live,
            "gone_count": gone,
            "survival_rate": survival,
            "mentions_lemonrealm": m_lem,
            "mentions_prv8sup": m_prv,
            "mentions_total": m_total,
            "rank_1": r1,
            "rank_2": r2,
            "rank_3": r3,
            "rank_other": r_other,
            "rank_1_hits": r1,
        })

    return out



def long_term():
    con = db.connect()
    posts = one(con.execute("SELECT COUNT(*) FROM posts").fetchone())
    comments = one(con.execute("SELECT COUNT(*) FROM comments").fetchone())
    scrapes = one(con.execute("SELECT COUNT(*) FROM scrapes").fetchone())
    avg = con.execute("SELECT AVG(rank) AS a FROM our_ranks").fetchone()
    first = con.execute("SELECT MIN(first_seen) AS f FROM comments").fetchone()
    con.close()
    a = avg["a"] if avg and avg["a"] is not None else 0
    days = 0
    if first and first["f"]:
        days = max(1, int((time.time() - first["f"]) / 86400))
    return {"period": "all_time", "total_posts_tracked": posts,
            "total_comments_tracked": comments, "total_scrapes": scrapes,
            "avg_rank": round(float(a), 2), "days_running": days}


def full_payload():
    """The exact shape the app exposes at /data.json."""
    last = last_scrape() or {"scraped_post": None, "comments": []}
    return {
        "scraped_post": last.get("scraped_post"),
        "comments": last.get("comments", []),
        "our_tracking": our_tracking(),
        "deleted_comments_stats": deleted_stats(),
        "scraper_keywords": [
            {"keyword": k["word"], "matches_found": k["matches"] or 0,
             "last_detected": iso(k["last_detected"])}
            for k in keywords()
        ],
        "top_performers": top_performers(),
        "long_term_stats": long_term(),
    }
