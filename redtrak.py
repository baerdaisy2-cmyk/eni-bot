"""
redtrak.py — find Reddit threads worth commenting on for lemonrealm / prv8sup.

Reads search terms from SEARCH_TERMS (.env), loops them across SWEEP_SUBS via
Arctic Shift, filters by recency + subreddit health, scores each result, and
caches the payload in the scrapes table so the page loads instantly.

Call redtrak.refresh() to pull fresh results.
Call redtrak.payload() to render.
"""
import json
import os
import time

import db
import arctic_shift


CACHE_KEY = "opportunities"
DEFAULT_SINCE_HOURS = 168
DEFAULT_LIMIT = 60
MIN_COMMENTS = 2
RED_ZONE_THRESHOLD = 0.40  # >=40% removal rate marks a sub as red zone


def _terms():
    raw = os.environ.get("SEARCH_TERMS", "").strip()
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip() and len(t.strip()) >= 4]


def _subs():
    raw = os.environ.get("SWEEP_SUBS", "").strip()
    if not raw:
        return []
    return [s.strip() for s in raw.split(",") if s.strip()]


def _subreddit_health():
    """Return {subreddit: {'total': n, 'removed': n, 'rate': float, 'red': bool}}."""
    con = db.connect()
    try:
        rows = con.execute(
            """SELECT subreddit,
                      COUNT(*) AS total,
                      SUM(CASE WHEN removed_by_category IS NOT NULL THEN 1 ELSE 0 END) AS removed
               FROM as_posts
               WHERE subreddit IS NOT NULL
               GROUP BY subreddit"""
        ).fetchall()
    finally:
        con.close()

    out = {}
    for r in rows:
        total = r["total"] or 0
        removed = r["removed"] or 0
        rate = (removed / float(total)) if total else 0.0
        out[r["subreddit"]] = {
            "total": total,
            "removed": removed,
            "rate": round(rate, 3),
            "red": rate >= RED_ZONE_THRESHOLD and total >= 3,
        }
    return out


def _already_replied(post_id):
    """Check if we've already got a comment on this post from our_ranks."""
    con = db.connect()
    try:
        row = con.execute(
            "SELECT 1 FROM our_ranks WHERE post_id=? LIMIT 1", (post_id,)
        ).fetchone()
        return row is not None
    finally:
        con.close()


def _score(post, term, sub_health):
    """Return (label, reason). Labels: 'strong', 'review', 'weak'."""
    sub = post.get("subreddit", "")
    health = sub_health.get(sub)
    score = post.get("score") or 0
    comments = post.get("num_comments") or 0

    if health and health["red"]:
        return "weak", "subreddit is red-zoned (high removal rate)"

    if comments < MIN_COMMENTS:
        return "weak", "not enough conversation to join"

    if score < 1:
        return "weak", "no engagement yet"

    # Strong: healthy sub, real engagement, not red-zoned
    if health and health["total"] >= 5 and health["rate"] < 0.20:
        return "strong", "healthy subreddit, active thread"

    # Review: unknown sub, or moderate removal rate
    if health and health["rate"] >= 0.20:
        return "review", "subreddit has some removal activity"

    return "review", "check subreddit rules before commenting"



def passes_filters(post, sub_health, max_age_hours=168):
    """Hard filters. Returns (keep: bool, reason: str, label: str).

    label is 'strong' or 'review' when kept, '' when rejected.
    max_age_hours default is 7 days."""
    if post.get("locked") or post.get("archived"):
        return False, "locked or archived", ""
    if post.get("removed_by_category"):
        return False, "post removed", ""
    age_h = (time.time() - (post.get("created_utc") or 0)) / 3600.0
    if age_h > max_age_hours:
        return False, "too old (%.0fh)" % age_h, ""
    sub = post.get("subreddit", "")
    h = sub_health.get(sub)
    if h and h.get("total", 0) >= 5:
        rate = h.get("rate", 0)
        if rate >= 0.60:
            return False, "subreddit heavily red-zoned (%.0f%%)" % (rate * 100), ""
    if (post.get("num_comments") or 0) < 2:
        return False, "no conversation yet", ""
    if (post.get("score") or 0) < 1:
        return False, "no upvotes yet", ""
    if h and h.get("total", 0) >= 5:
        if h.get("rate", 0) >= 0.20:
            return True, "", "review"
        return True, "", "strong"
    return True, "", "review"



def refresh(since_hours=None, limit=60):
    """Pull fresh opportunities from Arctic Shift, filtered by passes_filters.

    Slow — one HTTPS call per term x sub. Returns (count, msg)."""
    if since_hours is None:
        try:
            since_hours = int(os.environ.get("MAX_AGE_HOURS", "168"))
        except ValueError:
            since_hours = 168

    terms = _terms()
    subs = _subs()
    if not terms or not subs:
        return 0, "no search terms or subreddits configured"

    health = _subreddit_health()
    since_ts = int(time.time()) - since_hours * 3600

    seen_post_ids = set()
    opportunities = []
    errors = 0

    for term in terms:
        for sub in subs:
            try:
                posts = arctic_shift.search_posts(
                    subreddit=sub, q=term, limit=25,
                    after=since_ts - 3600  # 1h buffer so boundary posts aren't lost
                )
            except Exception as exc:
                errors += 1
                db.log("WARN", "redtrak",
                       "search %r in r/%s failed: %s"
                       % (term, sub, str(exc)[:120]),
                       {"term": term, "subreddit": sub})
                continue

            for p in posts:
                pid = p.get("id")
                if not pid or pid in seen_post_ids:
                    continue

                created = p.get("created_utc") or 0
                if created < since_ts:
                    continue

                if _already_replied(pid):
                    continue

                keep, reason, label = passes_filters(
                    p, health, max_age_hours=since_hours
                )
                if not keep:
                    continue

                seen_post_ids.add(pid)
                opportunities.append({
                    "id": pid,
                    "subreddit": p.get("subreddit") or sub,
                    "author": p.get("author") or "[deleted]",
                    "title": p.get("title") or "",
                    "permalink": p.get("permalink") or "",
                    "score": p.get("score") or 0,
                    "num_comments": p.get("num_comments") or 0,
                    "created_utc": created,
                    "age_hours": max(0, int((time.time() - created) / 3600)),
                    "matched_term": term,
                    "label": label,
                    "reason": reason,
                })

    order = {"strong": 0, "review": 1}
    opportunities.sort(key=lambda o: (
        order.get(o["label"], 2),
        -(o["num_comments"] or 0),
        -(o["score"] or 0),
    ))

    payload = {
        "generated_at": int(time.time()),
        "since_hours": since_hours,
        "terms_count": len(terms),
        "subs_count": len(subs),
        "errors": errors,
        "opportunities": opportunities[:limit],
    }

    _cache_put(CACHE_KEY, payload)
    db.log("INFO", "redtrak",
           "refresh: %d kept, %d errors, %d terms x %d subs"
           % (len(opportunities), errors, len(terms), len(subs)),
           {"kept": len(opportunities), "errors": errors})
    return len(opportunities), "found %d opportunities" % len(opportunities)



def _cache_put(key, value):
    now = int(time.time())
    con = db.connect()
    try:
        con.execute(
            """INSERT INTO scrapes (post_id, url, subreddit, title, payload, scraped_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(post_id) DO UPDATE SET
                 payload = excluded.payload,
                 scraped_at = excluded.scraped_at""",
            ("__redtrak__" + key, "", "", key, json.dumps(value), now),
        )
        con.commit()
    except Exception:
        # scrapes table may not accept arbitrary keys; fall back silently
        pass
    finally:
        con.close()


def _cache_get(key):
    con = db.connect()
    try:
        row = con.execute(
            "SELECT payload FROM scrapes WHERE post_id=?", ("__redtrak__" + key,)
        ).fetchone()
        if row and row["payload"]:
            try:
                return json.loads(row["payload"])
            except Exception:
                return None
    finally:
        con.close()
    return None


def payload():
    """Return cached opportunities, or a fast stub if none yet."""
    cached = _cache_get(CACHE_KEY)
    if cached:
        return cached

    # no cache yet — return empty shell
    return {
        "generated_at": None,
        "since_hours": DEFAULT_SINCE_HOURS,
        "terms_count": len(_terms()),
        "subs_count": len(_subs()),
        "errors": 0,
        "opportunities": [],
        "fresh": False,
    }


def stats(p):
    """Derive the four counters from the payload."""
    opps = p.get("opportunities", [])
    strong = sum(1 for o in opps if o["label"] == "strong")
    review = sum(1 for o in opps if o["label"] == "review")
    return {
        "found": len(opps),
        "strong": strong,
        "not_yet_replied": len(opps),  # we already filtered out replied ones
        "needs_rule_review": review,
    }
