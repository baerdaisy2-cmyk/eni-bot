"""score.py — pure math, no network, no model, no opinion.

Ranking has to work in two worlds: one where Reddit hands us scores and comment
counts, and one where it hands us nothing. So the formula adds two signals that
are always available - how many of our comments are on a thread, and how many of
them survived - to whatever Reddit happens to give us.

    python3 score.py     rescore everything and print the table
"""
import time

import db

# --- weights -----------------------------------------------------------------
W_REDDIT_SCORE = 0.6       # per upvote, when we have it
W_REDDIT_COMMENTS = 4.0    # per comment on the thread, when we have it
W_OURS = 8.0               # per comment of ours on the thread
W_MARKER = 12.0            # per comment of ours carrying the marker
SURVIVAL_FLOOR = 0.25      # a thread that eats everything still ranks above zero
FRESH_MIN, FRESH_MAX = 0.3, 2.0


def clamp(value, low, high):
    return max(low, min(high, value))


def score_post(row, ours=0, marker=0, gone=0):
    if row["blacklisted"]:
        return 0.0

    reddit_base = (row["score"] or 0) * W_REDDIT_SCORE + \
                  (row["num_comments"] or 0) * W_REDDIT_COMMENTS
    own_base = ours * W_OURS + marker * W_MARKER
    base = reddit_base + own_base
    if base == 0:
        base = 1.0  # nothing known yet; let freshness do the ordering

    survival = 1.0
    if ours:
        survival = clamp(1.0 - (gone / float(ours)), SURVIVAL_FLOOR, 1.0)

    age_hours = max(1.0, (time.time() - (row["created_utc"] or 0)) / 3600.0)
    freshness = clamp(24.0 / age_hours, FRESH_MIN, FRESH_MAX)

    return round(base * freshness * survival, 2)


def _counts(con):
    """One pass: per-post tallies of our comments, marker hits, and losses."""
    rows = con.execute(
        """SELECT post_id,
                  SUM(is_ours) AS ours,
                  SUM(CASE WHEN mentions_marker=1 THEN 1 ELSE 0 END) AS marker,
                  SUM(CASE WHEN is_ours=1 AND deleted=1 THEN 1 ELSE 0 END) AS gone
           FROM comments GROUP BY post_id""").fetchall()
    return {r["post_id"]: (r["ours"] or 0, r["marker"] or 0, r["gone"] or 0) for r in rows}


def run_scoring():
    con = db.connect()
    tallies = _counts(con)
    posts = con.execute("SELECT * FROM posts").fetchall()
    for row in posts:
        ours, marker, gone = tallies.get(row["id"], (0, 0, 0))
        con.execute("UPDATE posts SET rank_score=? WHERE id=?",
                    (score_post(row, ours, marker, gone), row["id"]))
    con.commit()
    con.close()
    return len(posts)


def ranked(limit=50):
    con = db.connect()
    rows = con.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM comments c
                   WHERE c.post_id=p.id AND c.mentions_marker=1) AS marker_hits,
                  (SELECT COUNT(*) FROM comments c
                   WHERE c.post_id=p.id AND c.is_ours=1) AS ours,
                  (SELECT COUNT(*) FROM comments c
                   WHERE c.post_id=p.id AND c.is_ours=1 AND c.deleted=1) AS gone
           FROM posts p ORDER BY p.rank_score DESC, p.last_seen DESC LIMIT ?""",
        (limit,)).fetchall()
    con.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    n = run_scoring()
    rows = ranked(50)
    print("\nrescored %d posts\n" % n)
    print("  %-6s %-14s %5s %6s %5s %9s  %s" % (
        "rank", "sub", "ours", "marker", "gone", "score", "title"))
    for i, r in enumerate(rows, 1):
        flag = " BLACKLISTED" if r["blacklisted"] else ""
        print("  %-6d r/%-12s %5d %6d %5d %9.2f  %s%s" % (
            i, r["subreddit"][:12], r["ours"], r["marker_hits"], r["gone"],
            r["rank_score"], (r["title"] or "")[:34], flag))
    print()
