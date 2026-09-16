import hmac
import json
import os
import re
import time

from flask import (Flask, jsonify, redirect, render_template, request,
                   session, url_for)

import comment_gen
import config
import db
import import_export
import monitor
import reddit
import score

app = Flask(__name__)
app.secret_key = os.environ.get("ENI_SECRET", "")
db.init()

PERMALINK = re.compile(
    r"reddit\.com/r/(?P<sub>[A-Za-z0-9_]+)/comments/(?P<post>[A-Za-z0-9]+)"
    r"(?:/[^/\s]*)?(?:/(?P<comment>[A-Za-z0-9]+))?", re.I)

OPEN_PATHS = {"/login", "/healthz"}
STALE_AFTER = int(os.environ.get("ENI_STALE_HOURS", "48")) * 3600
BOOTED = int(time.time())


def users():
    out = {}
    for pair in os.environ.get("ENI_USERS", "").split(","):
        pair = pair.strip()
        if ":" in pair:
            name, _, pw = pair.partition(":")
            out[name.strip()] = pw.strip()
    return out


def me():
    return session.get("user", "")


def one(row):
    if row is None:
        return 0
    return list(row.values())[0] if isinstance(row, dict) else row[0]


@app.before_request
def require_login():
    if request.path in OPEN_PATHS or request.path.startswith("/static/"):
        return None
    if not users():
        return None
    if me():
        return None
    return redirect(url_for("login", next=request.path))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    if request.method == "POST":
        name = request.form.get("user", "").strip()
        pw = request.form.get("password", "")
        known = users().get(name)
        if known and hmac.compare_digest(pw, known):
            session["user"] = name
            session.permanent = True
            db.log("INFO", "auth", "%s signed in" % name, {"user": name})
            return redirect(request.args.get("next") or url_for("watch"))
        error = "that combination did not work"
        db.log("WARN", "auth", "failed sign-in attempt for '%s'" % (name or "(blank)"),
               {"user": name,
                "ip": request.headers.get("X-Forwarded-For", request.remote_addr)})
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    who = me()
    session.clear()
    db.log("INFO", "auth", "%s signed out" % who, {"user": who})
    return redirect(url_for("login"))


@app.template_filter("ts")
def _ts(value):
    if not value:
        return "--"
    return time.strftime("%b %d %H:%M", time.localtime(int(value)))


@app.template_filter("ago")
def _ago(value):
    if not value:
        return "--"
    delta = max(0, int(time.time()) - int(value))
    if delta < 3600:
        return "%dm" % (delta // 60)
    if delta < 86400:
        return "%dh" % (delta // 3600)
    return "%dd" % (delta // 86400)


def stale_count():
    cutoff = int(time.time()) - STALE_AFTER
    con = db.connect()
    n = one(con.execute(
        "SELECT COUNT(*) FROM comments WHERE is_ours=1 AND deleted=0 AND last_seen < ?",
        (cutoff,)).fetchone())
    con.close()
    return n


def integrations():
    con = db.connect()
    live = one(con.execute(
        "SELECT COUNT(*) FROM comments WHERE is_ours=1 AND deleted=0").fetchone())
    con.close()
    return {
        "who": {"label": me() or "local", "ok": bool(me()),
                "detail": "signed in" if me() else "no login set"},
        "store": {"label": "Data", "ok": db.backend() == "postgres",
                  "detail": "shared (%s)" % db.backend() if db.backend() == "postgres"
                            else "this mac only"},
        "watch": {"label": "Watching", "ok": live > 0, "detail": "%d live" % live},
    }


def llm_state():
    """Honest: a localhost default is not reachable from a hosted server."""
    base = config.LLM_BASE or ""
    local = "localhost" in base or "127.0.0.1" in base
    hosted = bool(os.environ.get("RENDER") or os.environ.get("PORT"))
    if not comment_gen.FORMULA:
        return False, "no formula yet — the generate button stays off"
    if local and hosted:
        return False, "points at localhost, unreachable from this server"
    if local:
        return True, "local model at %s" % base
    if not config.LLM_KEY:
        return False, "remote endpoint set but no API key"
    return True, config.LLM_MODEL


def health_report():
    checks = []
    db_ok, db_note = True, db.backend()
    started = time.time()
    try:
        con = db.connect()
        con.execute("SELECT 1 AS ok").fetchone()
        con.close()
        db_note = "%s, responded in %dms" % (db.backend(), (time.time() - started) * 1000)
    except Exception as exc:
        db_ok = False
        db_note = str(exc)[:110]
    checks.append({"name": "Database", "ok": db_ok, "note": db_note,
                   "why": "where every comment, log line and blacklist entry lives"})

    shared = db.backend() == "postgres"
    checks.append({"name": "Shared storage", "ok": shared,
                   "note": "Supabase Postgres" if shared else "local sqlite file only",
                   "why": "without this, you and gustavo see different data"})

    n_users = len(users())
    checks.append({"name": "Sign-in", "ok": n_users > 0,
                   "note": "%d account%s configured" % (n_users, "" if n_users == 1 else "s"),
                   "why": "no accounts means the site is open to anyone who finds it"})

    stale = 0
    try:
        stale = stale_count()
    except Exception:
        pass
    checks.append({"name": "Checks up to date", "ok": stale == 0,
                   "note": "nothing overdue" if stale == 0
                           else "%d comment%s unchecked for over %dh" % (
                                stale, "" if stale == 1 else "s", STALE_AFTER // 3600),
                   "why": "the blacklist only learns from checks you actually make"})

    llm_ok, llm_note = llm_state()
    checks.append({"name": "Draft generator", "ok": llm_ok, "note": llm_note,
                   "why": "optional — writes reply drafts you read before posting"})

    reddit_note = "manual checks only — Reddit's API is closed to new apps"
    checks.append({"name": "Reddit access", "ok": False, "note": reddit_note,
                   "why": "you verify comments by eye; nothing is fetched automatically"})

    return checks


@app.route("/")
def index():
    score.run_scoring()
    return render_template(
        "index.html", rows=score.ranked(50), stats=db.stats(),
        integrations=integrations(), logs=db.recent_logs(8),
        stale=stale_count(), stale_hours=STALE_AFTER // 3600,
    )


@app.route("/subs")
def subs():
    con = db.connect()
    rows = con.execute(
        """SELECT p.subreddit AS sub, COUNT(*) AS total,
                  SUM(c.deleted) AS gone, MAX(c.last_seen) AS seen
           FROM comments c JOIN posts p ON p.id = c.post_id
           WHERE c.is_ours = 1 AND p.subreddit IS NOT NULL AND p.subreddit != ''
           GROUP BY p.subreddit ORDER BY COUNT(*) DESC""").fetchall()
    flagged = set()
    for r in con.execute("SELECT value FROM blacklist WHERE kind='subreddit'").fetchall():
        flagged.add(r["value"] if isinstance(r, dict) else r[0])
    con.close()

    table = []
    for r in rows:
        total = r["total"] or 0
        gone = r["gone"] or 0
        survived = total - gone
        rate = (survived / float(total) * 100) if total else 0.0
        if total < config.SUBREDDIT_MIN_SAMPLE:
            verdict, tone = "too few to judge", "dim"
        elif rate >= 75:
            verdict, tone = "friendly", "good"
        elif rate >= 50:
            verdict, tone = "mixed", "warn"
        else:
            verdict, tone = "hostile", "bad"
        table.append({"sub": r["sub"], "total": total, "gone": gone, "survived": survived,
                      "rate": round(rate), "verdict": verdict, "tone": tone,
                      "seen": r["seen"], "flagged": r["sub"] in flagged})
    return render_template("subs.html", table=table, integrations=integrations(),
                           minimum=config.SUBREDDIT_MIN_SAMPLE)


@app.route("/watch")
def watch():
    only_stale = request.args.get("stale") == "1"
    cutoff = int(time.time()) - STALE_AFTER
    con = db.connect()
    base = ("""SELECT c.*, p.subreddit, p.title, p.permalink AS thread_url
               FROM comments c LEFT JOIN posts p ON p.id = c.post_id
               WHERE c.is_ours = 1 """)
    if only_stale:
        rows = con.execute(base + "AND c.deleted=0 AND c.last_seen < ? "
                           "ORDER BY c.last_seen ASC", (cutoff,)).fetchall()
    else:
        rows = con.execute(base + "ORDER BY c.deleted ASC, c.last_seen ASC").fetchall()
    con.close()
    return render_template("watch.html", rows=[dict(r) for r in rows],
                           integrations=integrations(), now=int(time.time()),
                           me=me(), only_stale=only_stale,
                           stale=stale_count(), stale_seconds=STALE_AFTER)


@app.route("/watch/add", methods=["POST"])
def watch_add():
    raw = request.form.get("urls", "")
    note = request.form.get("note", "").strip()
    owner = me() or config.REDDIT_USERNAME or "me"
    added, skipped = 0, 0
    now = int(time.time())

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = PERMALINK.search(line)
        if not m:
            skipped += 1
            continue
        sub, post_id, cid = m.group("sub"), m.group("post"), m.group("comment")
        if not cid or cid == post_id:
            skipped += 1
            continue

        con = db.connect()
        known = con.execute("SELECT 1 AS hit FROM posts WHERE id=?", (post_id,)).fetchone()
        con.close()
        if not known:
            db.upsert_post({
                "id": post_id, "subreddit": sub, "author": "[unknown]",
                "title": note or "(thread %s)" % post_id, "body": "",
                "score": 0, "num_comments": 0, "created_utc": now,
                "permalink": "https://reddit.com/r/%s/comments/%s/" % (sub, post_id),
            })

        body = note or "(added %s)" % time.strftime("%Y-%m-%d", time.localtime(now))
        db.upsert_comment({
            "id": cid, "post_id": post_id, "parent_id": post_id,
            "author": owner, "body": body, "score": 0, "created_utc": now,
            "is_ours": 1, "owner": owner,
            "mentions_marker": int(any(mk.lower() in note.lower()
                                       for mk in config.MARKERS)) if note else 0,
        })
        added += 1

    db.log("INFO", "watch",
           "%s added %d comment(s) to the watchlist, skipped %d unparseable line(s)" % (
               owner, added, skipped),
           {"owner": owner, "added": added, "skipped": skipped})
    return redirect(url_for("watch"))


@app.route("/watch/<cid>/<verdict>", methods=["POST"])
def watch_verdict(cid, verdict):
    now = int(time.time())
    who = me() or "local"
    back = request.form.get("back") or url_for("watch")
    con = db.connect()
    row = con.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
    if row is None:
        con.close()
        return redirect(back)
    sub_row = con.execute("SELECT subreddit FROM posts WHERE id=?",
                          (row["post_id"],)).fetchone()
    sub = sub_row["subreddit"] if sub_row else ""

    if verdict == "alive":
        con.execute(
            "UPDATE comments SET deleted=0, deleted_at=NULL, delete_reason=NULL, "
            "last_seen=? WHERE id=?", (now, cid))
        con.commit()
        con.close()
        db.log("INFO", "watch",
               "%s checked comment %s in r/%s and it is still up" % (who, cid, sub),
               {"comment_id": cid, "post_id": row["post_id"], "subreddit": sub,
                "checked_by": who, "owner": row["owner"]})
        return redirect(back)

    alive_for = now - (row["first_seen"] or now)
    reason = ("mod_or_filter_suspected" if alive_for < config.FAST_DELETE_WINDOW
              else "unknown_removed_or_selfdelete")
    con.execute("UPDATE comments SET deleted=1, deleted_at=?, delete_reason=? WHERE id=?",
                (now, reason, cid))
    con.commit()
    con.close()

    db.log("WARN", "watch",
           "%s reported comment %s gone from r/%s post %s (owner %s, %s)" % (
               who, cid, sub or "?", row["post_id"], row["owner"] or "?", reason),
           {"comment_id": cid, "post_id": row["post_id"], "subreddit": sub,
            "reason": reason, "alive_seconds": alive_for,
            "checked_by": who, "owner": row["owner"]})
    monitor.check_blacklist(row["post_id"], sub)
    return redirect(back)


@app.route("/watch/<cid>/drop", methods=["POST"])
def watch_drop(cid):
    con = db.connect()
    con.execute("DELETE FROM comments WHERE id=?", (cid,))
    con.commit()
    con.close()
    db.log("INFO", "watch", "%s removed comment %s from the watchlist" % (me() or "local", cid),
           {"comment_id": cid, "by": me()})
    return redirect(request.form.get("back") or url_for("watch"))


@app.route("/sync", methods=["POST"])
def sync():
    result = import_export.import_latest()
    score.run_scoring()
    db.log("INFO", "app", "refresh finished: %s" % result, result)
    return redirect(url_for("index"))


@app.route("/post/<pid>")
def post_view(pid):
    con = db.connect()
    post = con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if post is None:
        con.close()
        return "no such post", 404
    comments = con.execute(
        "SELECT * FROM comments WHERE post_id=? ORDER BY created_utc ASC", (pid,)).fetchall()
    con.close()
    llm_ok, _ = llm_state()
    return render_template(
        "post.html", post=dict(post), comments=[dict(c) for c in comments],
        has_marker=any(c["mentions_marker"] for c in comments),
        can_generate=llm_ok,
    )


@app.route("/post/<pid>/generate", methods=["POST"])
def generate(pid):
    con = db.connect()
    post = con.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    con.close()
    if post is None:
        return jsonify({"error": "no such post"}), 404
    try:
        return jsonify({"text": comment_gen.generate_comment(dict(post))})
    except NotImplementedError as exc:
        return jsonify({"error": str(exc)}), 501
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/logs")
def logs_view():
    level = request.args.get("level") or None
    con = db.connect()
    if level:
        rows = con.execute("SELECT * FROM logs WHERE level=? ORDER BY ts DESC LIMIT 400",
                           (level,)).fetchall()
    else:
        rows = con.execute("SELECT * FROM logs ORDER BY ts DESC LIMIT 400").fetchall()
    counts = {}
    for r in con.execute("SELECT level, COUNT(*) AS n FROM logs GROUP BY level"):
        counts[r["level"]] = r["n"]
    con.close()
    return render_template("logs.html", rows=[dict(r) for r in rows],
                           counts=counts, level=level, integrations=integrations())


@app.route("/logs.json")
def logs_json():
    con = db.connect()
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM logs ORDER BY ts DESC LIMIT 2000").fetchall()]
    con.close()
    for r in rows:
        try:
            r["context"] = json.loads(r["context"] or "{}")
        except Exception:
            pass
    return jsonify(rows)


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "backend": db.backend(), "ts": int(time.time())})


@app.route("/health")
def health():
    checks = health_report()
    up = int(time.time()) - BOOTED
    if up < 3600:
        uptime = "%d minutes" % max(1, up // 60)
    elif up < 86400:
        uptime = "%d hours" % (up // 3600)
    else:
        uptime = "%d days" % (up // 86400)
    ok = all(c["ok"] for c in checks if c["name"] not in
             ("Draft generator", "Reddit access"))
    return render_template("health.html", checks=checks, integrations=integrations(),
                           all_ok=ok, uptime=uptime, stats=db.stats())


@app.route("/health.json")
def health_json():
    checks = health_report()
    return jsonify({
        "status": "ok" if all(c["ok"] for c in checks if c["name"] not in
                              ("Draft generator", "Reddit access")) else "degraded",
        "backend": db.backend(),
        "checks": {c["name"]: {"ok": c["ok"], "note": c["note"]} for c in checks},
        "ts": int(time.time()),
    })


if __name__ == "__main__":
    if users() and len(app.secret_key) < 16:
        raise SystemExit(
            "ENI_USERS is set but ENI_SECRET is missing or too short.\n"
            "Add a line to .env:  ENI_SECRET=<run: openssl rand -hex 24>")
    db.log("INFO", "app", "ENI Monitor started",
           {"backend": db.backend(), "users": len(users())})
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5001")))
