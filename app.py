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
import scraper
import redtrak

app = Flask(__name__)
app.secret_key = os.environ.get("ENI_SECRET", "")
db.init()
scraper.init()

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
    if not users() or me():
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
    s = scraper.status()
    return {
        "who": {"label": me() or "local", "ok": bool(me()),
                "detail": "signed in" if me() else "no login set"},
        "store": {"label": "Data", "ok": db.backend() == "postgres",
                  "detail": "shared (%s)" % db.backend() if db.backend() == "postgres"
                            else "this mac only"},
        "watch": {"label": "Watching", "ok": live > 0, "detail": "%d live" % live},
        "scraper": {"label": "Scraper", "ok": s["ok"],
                    "detail": s["note"] if s["ok"] else "offline"},
    }


def llm_state():
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


# ------------------------------------------------------------- dashboard --
@app.route("/")
def index():
    score.run_scoring()
    return render_template(
        "index.html", rows=score.ranked(50), stats=db.stats(),
        integrations=integrations(), logs=db.recent_logs(8),
        stale=stale_count(), stale_hours=STALE_AFTER // 3600,
        tracking=scraper.our_tracking(), longterm=scraper.long_term(),
        deleted=scraper.deleted_stats(),
    )


# --------------------------------------------------------------- scraper --
@app.route("/scrape", methods=["GET", "POST"])
def scrape():
    payload, error = None, ""
    if request.method == "POST":
        url = request.form.get("url", "").strip()
        try:
            payload = scraper.scrape(url)
        except ValueError as exc:
            error = str(exc)
        except LookupError as exc:
            error = str(exc)
        except Exception as exc:
            error = "local scraper problem: %s" % str(exc)[:140]
            db.log("ERROR", "scraper", "scrape failed for %s" % url, {"error": str(exc)[:200]})
    if payload is None and not error:
        payload = scraper.last_scrape()
    enrich_ok, enrich_hits, enrich_total = scraper.enrichment_working()
    return render_template("scrape.html", payload=payload, error=error,
                           integrations=integrations(), status=scraper.status(),
                           enrich_ok=enrich_ok, enrich_hits=enrich_hits,
                           enrich_total=enrich_total)


@app.route("/scrape.json")
def scrape_json():
    return jsonify(scraper.full_payload())


@app.route("/data.json")
def data_json():
    return jsonify(scraper.full_payload())


# -------------------------------------------------------------- keywords --
@app.route("/keywords", methods=["GET", "POST"])
def keywords_view():
    note = ""
    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "add":
            ok, msg = scraper.add_keyword(request.form.get("word", ""))
            note = ("watching \"%s\"" % msg) if ok else msg
            if ok:
                db.log("INFO", "scraper", "%s started watching the keyword \"%s\"" % (
                    me() or "local", msg), {"keyword": msg})
        elif action == "remove":
            scraper.remove_keyword(request.form.get("word", ""))
            note = "stopped watching that keyword"
        elif action == "sweep":
            found, msg = scraper.sweep_keywords()
            note = "%d new match%s — %s" % (found, "" if found == 1 else "es", msg)
        return redirect(url_for("keywords_view", note=note))
    return render_template("keywords.html", words=scraper.keywords(),
                           hits=scraper.keyword_hits(), integrations=integrations(),
                           status=scraper.status(), note=request.args.get("note", ""),
                           maximum=scraper.MAX_KEYWORDS)


# ------------------------------------------------------------- deletions --


# ---------------------------------------------------------- opportunities --
@app.route("/opportunities", methods=["GET", "POST"])
def opportunities_view():
    if request.method == "POST" and request.form.get("action") == "refresh":
        found, msg = redtrak.refresh()
        return redirect(url_for("opportunities_view", note=msg))
    p = redtrak.payload()
    return render_template(
        "opportunities.html",
        nav="opportunities",
        payload=p,
        stats=redtrak.stats(p),
        note=request.args.get("note", ""),
    )

@app.route("/deletions")
def deletions():
    stats = scraper.deleted_stats()
    con = db.connect()
    recent = con.execute(
        """SELECT c.id, c.owner, c.deleted_at, c.delete_reason, p.subreddit, p.id AS post_id
           FROM comments c LEFT JOIN posts p ON p.id = c.post_id
           WHERE c.deleted = 1 ORDER BY c.deleted_at DESC LIMIT 60""").fetchall()
    fast = one(con.execute(
        "SELECT COUNT(*) FROM comments WHERE deleted=1 AND "
        "delete_reason='mod_or_filter_suspected'").fetchone())
    con.close()
    return render_template("deletions.html", stats=stats,
                           recent=[dict(r) for r in recent], fast=fast,
                           integrations=integrations(),
                           minimum=config.SUBREDDIT_MIN_SAMPLE)


# ------------------------------------------------------------ performers --
@app.route("/performers")
def performers():
    return render_template("performers.html", people=scraper.top_performers(),
                           tracking=scraper.our_tracking(),
                           longterm=scraper.long_term(), integrations=integrations())


# ------------------------------------------------------------- subreddits --
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


# ------------------------------------------------------------------ watch --
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
        has_marker=any(c["mentions_marker"] for c in comments), can_generate=llm_ok)


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


# ------------------------------------------------------------------ health --
def health_report():
    checks = []
    db_ok, started = True, time.time()
    try:
        con = db.connect()
        con.execute("SELECT 1 AS ok").fetchone()
        con.close()
        db_note = "%s, responded in %dms" % (db.backend(), (time.time() - started) * 1000)
    except Exception as exc:
        db_ok, db_note = False, str(exc)[:110]
    checks.append({"name": "Database", "ok": db_ok, "note": db_note,
                   "why": "where every comment, log line and blacklist entry lives"})

    shared = db.backend() == "postgres"
    checks.append({"name": "Shared storage", "ok": shared,
                   "note": "Supabase Postgres" if shared else "local sqlite file only",
                   "why": "without this, you and gustavo see different data"})

    n = len(users())
    checks.append({"name": "Sign-in", "ok": n > 0,
                   "note": "%d account%s configured" % (n, "" if n == 1 else "s"),
                   "why": "no accounts means the site is open to anyone who finds it"})

    s = scraper.status()
    checks.append({"name": "Local scraper", "ok": s["ok"],
                   "note": "%s — %s" % (s["base"], s["note"]),
                   "why": "supplies post and comment data for the Scraper page"})

    enrich_ok, hits, total = scraper.enrichment_working()
    checks.append({"name": "Account-age lookup", "ok": enrich_ok,
                   "note": "%d of %d cached lookups succeeded" % (hits, total)
                           if total else "no lookups attempted yet",
                   "why": "reddit.com blocks anonymous callers; ages stay empty when it does"})

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
    return checks


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "backend": db.backend(), "ts": int(time.time())})


@app.route("/health")
def health():
    checks = health_report()
    up = int(time.time()) - BOOTED
    uptime = ("%d minutes" % max(1, up // 60)) if up < 3600 else (
        "%d hours" % (up // 3600) if up < 86400 else "%d days" % (up // 86400))
    soft = ("Draft generator", "Local scraper", "Account-age lookup")
    ok = all(c["ok"] for c in checks if c["name"] not in soft)
    return render_template("health.html", checks=checks, integrations=integrations(),
                           all_ok=ok, uptime=uptime, stats=db.stats())


@app.route("/health.json")
def health_json():
    checks = health_report()
    soft = ("Draft generator", "Local scraper", "Account-age lookup")
    return jsonify({
        "status": "ok" if all(c["ok"] for c in checks if c["name"] not in soft)
                  else "degraded",
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
           {"backend": db.backend(), "users": len(users()),
            "scraper": scraper.SCRAPER_BASE})
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5001")))


# ---------------------------------------------------------- opportunities --


