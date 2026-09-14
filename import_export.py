import csv
import io
import os
import time
import zipfile

import config
import db

EXPORT_DIR = os.path.join(config.HERE, "exports")


def _marker(text):
    low = (text or "").lower()
    return int(any(m.lower() in low for m in config.MARKERS))


def _parse_date(raw):
    raw = (raw or "").strip().replace(" UTC", "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(raw, fmt)))
        except ValueError:
            continue
    return int(time.time())


def _post_id(link):
    if "/comments/" not in (link or ""):
        return ""
    return link.split("/comments/")[1].split("/")[0]


def latest_zip():
    if not os.path.isdir(EXPORT_DIR):
        return None
    zips = [os.path.join(EXPORT_DIR, f) for f in os.listdir(EXPORT_DIR)
            if f.lower().endswith(".zip")]
    return max(zips, key=os.path.getmtime) if zips else None


def _read_csv(archive, name):
    for member in archive.namelist():
        if member.lower().endswith(name):
            raw = archive.read(member).decode("utf-8", "replace")
            return list(csv.DictReader(io.StringIO(raw)))
    return []


def import_zip(path):
    """One export = one snapshot. Anything we knew about that is not in here is gone.

    SQLite allows a single writer. Every block below finishes its transaction and
    closes before anything else touches the database. Logs are collected first and
    written after the connection is released.
    """
    now = int(time.time())
    summary = {"file": os.path.basename(path), "comments": 0, "posts": 0,
               "vanished": 0, "new": 0}
    archive = zipfile.ZipFile(path)

    post_rows = _read_csv(archive, "posts.csv")
    comment_rows = _read_csv(archive, "comments.csv")

    # ---- posts -------------------------------------------------------------
    for row in post_rows:
        pid = (row.get("id") or "").strip()
        if not pid:
            continue
        db.upsert_post({
            "id": pid, "subreddit": row.get("subreddit", ""),
            "author": config.REDDIT_USERNAME or "me",
            "title": row.get("title", ""), "body": row.get("body", "") or "",
            "score": 0, "num_comments": 0,
            "created_utc": _parse_date(row.get("date")),
            "permalink": row.get("permalink", ""),
        })
        summary["posts"] += 1

    # ---- what we knew was alive before this export --------------------------
    con = db.connect()
    known = {r["id"] for r in con.execute(
        "SELECT id FROM comments WHERE is_ours=1 AND deleted=0")}
    existing_posts = {r["id"] for r in con.execute("SELECT id FROM posts")}
    con.close()

    # ---- comments ----------------------------------------------------------
    seen = set()
    for row in comment_rows:
        cid = (row.get("id") or "").strip()
        if not cid:
            continue
        link = row.get("permalink") or row.get("link") or ""
        pid = _post_id(link)
        sub = row.get("subreddit", "")
        body = row.get("body", "") or ""

        if pid and pid not in existing_posts:
            db.upsert_post({
                "id": pid, "subreddit": sub, "author": "[unknown]",
                "title": "(thread %s)" % pid, "body": "", "score": 0,
                "num_comments": 0, "created_utc": _parse_date(row.get("date")),
                "permalink": link,
            })
            existing_posts.add(pid)

        db.upsert_comment({
            "id": cid, "post_id": pid,
            "parent_id": (row.get("parent") or "").split("_")[-1],
            "author": config.REDDIT_USERNAME or "me", "body": body, "score": 0,
            "created_utc": _parse_date(row.get("date")),
            "is_ours": 1, "mentions_marker": _marker(body),
        })
        seen.add(cid)
        if cid not in known:
            summary["new"] += 1
        summary["comments"] += 1

    # ---- the diff ----------------------------------------------------------
    gone = known - seen
    pending_logs = []
    affected = []

    if gone:
        con = db.connect()
        for cid in gone:
            row = con.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
            if row is None:
                continue
            alive = now - (row["first_seen"] or now)
            reason = ("mod_or_filter_suspected" if alive < config.FAST_DELETE_WINDOW
                      else "unknown_removed_or_selfdelete")
            con.execute(
                "UPDATE comments SET deleted=1, deleted_at=?, delete_reason=? WHERE id=?",
                (now, reason, cid))
            sub_row = con.execute("SELECT subreddit FROM posts WHERE id=?",
                                  (row["post_id"],)).fetchone()
            sub = sub_row["subreddit"] if sub_row else ""
            affected.append((row["post_id"], sub))
            pending_logs.append((
                "WARN", "import",
                "comment %s is missing from this export (r/%s post %s, %s)" % (
                    cid, sub or "?", row["post_id"], reason),
                {"comment_id": cid, "post_id": row["post_id"], "subreddit": sub,
                 "reason": reason, "alive_seconds": alive,
                 "source": summary["file"]},
            ))
        con.commit()
        con.close()

    summary["vanished"] = len(gone)

    # ---- connection is closed; safe to write logs now ----------------------
    for level, actor, message, context in pending_logs:
        db.log(level, actor, message, context)

    if affected:
        import monitor
        for post_id, sub in set(affected):
            monitor.check_blacklist(post_id, sub)

    db.log("INFO", "import",
           "imported %s: %d comments (%d new), %d posts, %d gone since last export" % (
               summary["file"], summary["comments"], summary["new"],
               summary["posts"], summary["vanished"]), summary)
    return summary


def import_latest():
    path = latest_zip()
    if not path:
        db.log("ERROR", "import",
               "no export zip found in exports/ - drop one in and refresh",
               {"dir": EXPORT_DIR})
        return {"error": "no zip in %s" % EXPORT_DIR}
    return import_zip(path)
