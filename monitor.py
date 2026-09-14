import time

import config
import db
import reddit


def _infer_reason(row, now):
    alive_for = now - (row["first_seen"] or now)
    if row["is_ours"] and alive_for < config.FAST_DELETE_WINDOW:
        return "mod_or_filter_suspected"
    return "unknown_removed_or_selfdelete"


def sync_comments(post_id, subreddit=""):
    """Fetch live comments, upsert, then diff away whatever stopped showing up.

    SQLite allows one writer at a time. The transaction below commits and closes
    before any log line is written.
    """
    now = int(time.time())
    live = reddit.client.fetch_comments(post_id, subreddit)
    live_ids = set()
    for c in live:
        if not c["id"]:
            continue
        db.upsert_comment(c)
        live_ids.add(c["id"])

    con = db.connect()
    known = con.execute(
        "SELECT * FROM comments WHERE post_id=? AND deleted=0", (post_id,)
    ).fetchall()
    pending_logs = []
    for row in known:
        if row["id"] in live_ids:
            continue
        reason = _infer_reason(row, now)
        con.execute(
            "UPDATE comments SET deleted=1, deleted_at=?, delete_reason=? WHERE id=?",
            (now, reason, row["id"]),
        )
        pending_logs.append((
            "WARN", "monitor",
            "comment %s disappeared from r/%s post %s (%s, %s)" % (
                row["id"], subreddit or "?", post_id,
                "ours" if row["is_ours"] else "theirs", reason),
            {"comment_id": row["id"], "post_id": post_id, "subreddit": subreddit,
             "is_ours": bool(row["is_ours"]), "reason": reason,
             "alive_seconds": now - (row["first_seen"] or now)},
        ))
    con.commit()
    con.close()

    for level, actor, message, context in pending_logs:
        db.log(level, actor, message, context)

    if pending_logs:
        check_blacklist(post_id, subreddit)
    return {"live": len(live_ids), "vanished": len(pending_logs)}


def check_blacklist(post_id, subreddit=""):
    """Read everything first, close, then write. Never log with a connection open."""
    con = db.connect()
    ours_gone = con.execute(
        "SELECT COUNT(*) FROM comments WHERE post_id=? AND is_ours=1 AND deleted=1",
        (post_id,)).fetchone()[0]
    sub_total, sub_gone = 0, 0
    if subreddit:
        row = con.execute(
            """SELECT COUNT(*) AS total, SUM(deleted) AS gone FROM comments c
               JOIN posts p ON p.id=c.post_id
               WHERE p.subreddit=? AND c.is_ours=1""", (subreddit,)).fetchone()
        sub_total, sub_gone = row["total"] or 0, row["gone"] or 0
    con.close()

    if ours_gone >= config.POST_DELETE_THRESHOLD and not db.is_blacklisted("post", post_id):
        db.blacklist_add("post", post_id, "our comment removed %dx" % ours_gone)
        db.log("ERROR", "monitor",
               "post %s blacklisted: our comment was removed %d times" % (post_id, ours_gone),
               {"post_id": post_id, "deletions": ours_gone})

    if subreddit and sub_total >= config.SUBREDDIT_MIN_SAMPLE:
        rate = sub_gone / float(sub_total)
        if rate > config.SUBREDDIT_DELETE_RATE and not db.is_blacklisted("subreddit", subreddit):
            db.blacklist_add("subreddit", subreddit,
                             "delete rate %.0f%% over %d comments" % (rate * 100, sub_total))
            db.log("ERROR", "monitor",
                   "r/%s flagged high-risk: %.0f%% of our comments removed (%d of %d)" % (
                       subreddit, rate * 100, sub_gone, sub_total),
                   {"subreddit": subreddit, "rate": round(rate, 3), "sample": sub_total})


def sync_all(post_depth=15):
    """One pass: listings for every watched sub, then comment diffs on the hottest posts."""
    summary = {"subs": 0, "posts": 0, "threads": 0, "vanished": 0, "errors": []}
    for sub in config.WATCH_SUBREDDITS:
        try:
            posts = reddit.client.fetch_posts(sub, limit=50)
        except Exception as exc:
            summary["errors"].append("%s: %s" % (sub, exc))
            db.log("ERROR", "monitor", "could not read r/%s: %s" % (sub, exc),
                   {"subreddit": sub})
            continue
        for p in posts:
            db.upsert_post(p)
        summary["subs"] += 1
        summary["posts"] += len(posts)
        db.log("INFO", "monitor", "read %d posts from r/%s" % (len(posts), sub),
               {"subreddit": sub, "count": len(posts)})

    con = db.connect()
    targets = con.execute(
        """SELECT id, subreddit FROM posts
           WHERE blacklisted=0
           ORDER BY (SELECT COUNT(*) FROM comments c
                     WHERE c.post_id=posts.id AND c.is_ours=1) DESC,
                    rank_score DESC, last_seen DESC LIMIT ?""", (post_depth,)).fetchall()
    con.close()

    for row in targets:
        try:
            res = sync_comments(row["id"], row["subreddit"])
            summary["threads"] += 1
            summary["vanished"] += res["vanished"]
        except NotImplementedError as exc:
            summary["errors"].append(str(exc))
            break
        except Exception as exc:
            summary["errors"].append("%s: %s" % (row["id"], exc))
    return summary
