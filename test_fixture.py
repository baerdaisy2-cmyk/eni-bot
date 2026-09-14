"""Fake three Reddit exports, import them, prove the diff engine fires.

Runs against a throwaway DB (test_fixture.db). Your real eni_monitor.db is untouched.

    cd ~/eni-bot && source .venv/bin/activate && python3 test_fixture.py
"""
import csv
import io
import os
import sys
import zipfile

import config

config.DB_PATH = os.path.join(config.HERE, "test_fixture.db")
if os.path.exists(config.DB_PATH):
    os.remove(config.DB_PATH)

import db
import import_export

db.init()
OUT = os.path.join(config.HERE, "exports")
os.makedirs(OUT, exist_ok=True)

POSTS = [
    ("p1", "IPTV", "Best service in 2026?"),
    ("p2", "IPTV", "Buffering on firestick"),
    ("p3", "IPTV", "Reseller panel recommendations"),
    ("p4", "TestSub", "Unrelated thread"),
]

# (comment_id, post_id, subreddit, body)
ALL_COMMENTS = [
    ("c1", "p1", "IPTV", "been running this a year, no complaints"),
    ("c2", "p1", "IPTV", "ping me, prv8sup has what you want"),
    ("c3", "p2", "IPTV", "swap the DNS, fixes it nine times out of ten"),
    ("c4", "p2", "IPTV", "firestick 4k max handles it fine"),
    ("c5", "p3", "IPTV", "panel uptime matters more than price"),
    ("c6", "p3", "IPTV", "avoid the cheap resellers"),
    ("c7", "p4", "TestSub", "unrelated, should stay clean"),
    ("c8", "p4", "TestSub", "second unrelated comment"),
]

REMOVED = {"c1", "c2", "c3", "c4"}  # gone in export 2


def build(name, comment_ids):
    path = os.path.join(OUT, name)
    with zipfile.ZipFile(path, "w") as z:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "permalink", "date", "subreddit", "title", "body"])
        for pid, sub, title in POSTS:
            w.writerow([pid, "https://reddit.com/r/%s/comments/%s/" % (sub, pid),
                        "2026-09-10 12:00:00", sub, title, ""])
        z.writestr("posts.csv", buf.getvalue())

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "permalink", "date", "parent", "subreddit", "body"])
        for cid, pid, sub, body in ALL_COMMENTS:
            if cid not in comment_ids:
                continue
            w.writerow([cid, "https://reddit.com/r/%s/comments/%s/x/%s" % (sub, pid, cid),
                        "2026-09-10 13:00:00", "t3_" + pid, sub, body])
        z.writestr("comments.csv", buf.getvalue())
    return path


def check(label, got, want):
    ok = got == want
    print("  %s %-42s got=%s want=%s" % ("PASS" if ok else "FAIL", label, got, want))
    return ok


def q(sql, args=()):
    con = db.connect()
    r = con.execute(sql, args).fetchone()
    val = list(r.values())[0] if isinstance(r, dict) else r[0]
    con.close()
    return val


passed = True

print("\n--- export 1: everything alive ---")
r1 = import_export.import_zip(build("fixture_1.zip", {c[0] for c in ALL_COMMENTS}))
print("  import:", r1)
passed &= check("comments stored", q("SELECT COUNT(*) FROM comments"), 8)
passed &= check("flagged as ours", q("SELECT COUNT(*) FROM comments WHERE is_ours=1"), 8)
passed &= check("marker detected on c2",
                q("SELECT mentions_marker FROM comments WHERE id='c2'"), 1)
passed &= check("nothing deleted yet", q("SELECT COUNT(*) FROM comments WHERE deleted=1"), 0)
passed &= check("no blacklist yet", q("SELECT COUNT(*) FROM blacklist"), 0)

print("\n--- export 2: c1-c4 have vanished ---")
r2 = import_export.import_zip(
    build("fixture_2.zip", {c[0] for c in ALL_COMMENTS if c[0] not in REMOVED}))
print("  import:", r2)
passed &= check("diff caught 4 disappearances", r2["vanished"], 4)
passed &= check("marked deleted in db", q("SELECT COUNT(*) FROM comments WHERE deleted=1"), 4)
passed &= check("reason inferred",
                q("SELECT delete_reason FROM comments WHERE id='c1'"),
                "mod_or_filter_suspected")
passed &= check("p1 blacklisted (2 of ours gone)",
                q("SELECT COUNT(*) FROM blacklist WHERE kind='post' AND value='p1'"), 1)
passed &= check("r/IPTV flagged (4 of 6 = 66%)",
                q("SELECT COUNT(*) FROM blacklist WHERE kind='subreddit' AND value='IPTV'"), 1)
passed &= check("r/TestSub left alone",
                q("SELECT COUNT(*) FROM blacklist WHERE value='TestSub'"), 0)
passed &= check("survivors untouched", q("SELECT COUNT(*) FROM comments WHERE deleted=0"), 4)

print("\n--- export 3: c1 came back (mod approved it) ---")
r3 = import_export.import_zip(
    build("fixture_3.zip", {c[0] for c in ALL_COMMENTS if c[0] != "c2"}))
print("  import:", r3)
passed &= check("c1 resurrected", q("SELECT deleted FROM comments WHERE id='c1'"), 0)
passed &= check("c2 still gone", q("SELECT deleted FROM comments WHERE id='c2'"), 1)

print("\n--- log lines written ---")
for line in db.recent_logs(4):
    print("  [%s] %s" % (line["level"], line["message"]))

print("\n%s\n" % ("ALL CHECKS PASSED" if passed else "SOME CHECKS FAILED"))
sys.exit(0 if passed else 1)
