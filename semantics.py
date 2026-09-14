"""semantics.py — PROJECT.md is the source, SQLite is the index, reality is the judge.

    python3 semantics.py           parse, verify, report
    python3 semantics.py --quiet   exit code only (0 clean, 1 drift)

What it does:
  1. Parses the markdown tables in PROJECT.md into semantics.db
  2. Runs every `verify` command in the FILES table and records what actually happened
  3. Prints where the file's claims and reality disagree

A row that says "ok" but whose verify command fails is drift, and drift is reported
loudly. Nothing here trusts a claim it has not run.
"""
import hashlib
import os
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
MD_PATH = os.path.join(HERE, "PROJECT.md")
DB_PATH = os.path.join(HERE, "semantics.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS identity (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS decisions (id TEXT PRIMARY KEY, decision TEXT, rationale TEXT);
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, role TEXT, claimed_state TEXT, verify_cmd TEXT,
  exists_on_disk INTEGER, bytes INTEGER, sha1 TEXT,
  verify_rc INTEGER, verify_out TEXT, checked_at INTEGER
);
CREATE TABLE IF NOT EXISTS assumptions (id TEXT PRIMARY KEY, assumption TEXT, verified TEXT, how TEXT);
CREATE TABLE IF NOT EXISTS open_items (id TEXT PRIMARY KEY, item TEXT, blocked_on TEXT);
CREATE TABLE IF NOT EXISTS next_steps (step INTEGER PRIMARY KEY, action TEXT);
CREATE TABLE IF NOT EXISTS sync_runs (
  ts INTEGER PRIMARY KEY, md_sha1 TEXT, rows INTEGER, drift INTEGER
);
"""


def parse_tables(text):
    """Return {SECTION_NAME: [rowdict, ...]} for every markdown table under an H2."""
    sections, current, header, rows = {}, None, None, []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if current and rows:
                sections[current] = rows
            current, header, rows = stripped[3:].strip(), None, []
            continue
        if not stripped.startswith("|") or current is None:
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if set("".join(cells)) <= set("-: "):
            continue
        if header is None:
            header = cells
            continue
        rows.append(dict(zip(header, cells)))
    if current and rows:
        sections[current] = rows
    return sections


def sha1_of(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:12]


def run_verify(cmd):
    if not cmd:
        return None, ""
    try:
        proc = subprocess.run(cmd, shell=True, cwd=HERE, capture_output=True,
                              text=True, timeout=30)
        out = (proc.stderr or proc.stdout or "").strip().splitlines()
        return proc.returncode, (out[-1][:180] if out else "")
    except subprocess.TimeoutExpired:
        return 124, "timed out after 30s"
    except Exception as exc:
        return 1, str(exc)[:180]


def main():
    quiet = "--quiet" in sys.argv
    if not os.path.exists(MD_PATH):
        print("PROJECT.md not found at %s" % MD_PATH)
        return 1

    text = open(MD_PATH, encoding="utf-8").read()
    sections = parse_tables(text)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table in ("identity", "decisions", "files", "assumptions", "open_items", "next_steps"):
        con.execute("DELETE FROM %s" % table)

    rows_written = 0
    now = int(time.time())

    for r in sections.get("IDENTITY", []):
        con.execute("INSERT INTO identity VALUES (?,?)", (r.get("key"), r.get("value")))
        rows_written += 1

    for r in sections.get("DECISIONS", []):
        con.execute("INSERT INTO decisions VALUES (?,?,?)",
                    (r.get("id"), r.get("decision"), r.get("rationale")))
        rows_written += 1

    for r in sections.get("ASSUMPTIONS", []):
        con.execute("INSERT INTO assumptions VALUES (?,?,?,?)",
                    (r.get("id"), r.get("assumption"), r.get("verified"),
                     r.get("how to settle it")))
        rows_written += 1

    for r in sections.get("OPEN", []):
        con.execute("INSERT INTO open_items VALUES (?,?,?)",
                    (r.get("id"), r.get("item"), r.get("blocked on")))
        rows_written += 1

    for r in sections.get("NEXT", []):
        try:
            con.execute("INSERT INTO next_steps VALUES (?,?)",
                        (int(r.get("step")), r.get("action")))
            rows_written += 1
        except (TypeError, ValueError):
            pass

    drift = []
    missing = []
    for r in sections.get("FILES", []):
        path = r.get("path", "")
        full = os.path.join(HERE, path)
        on_disk = os.path.isfile(full)
        size = os.path.getsize(full) if on_disk else 0
        digest = sha1_of(full) if on_disk else ""
        rc, out = run_verify(r.get("verify", ""))
        con.execute("INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (path, r.get("role"), r.get("state"), r.get("verify"),
                     int(on_disk), size, digest, rc, out, now))
        rows_written += 1
        if not on_disk:
            missing.append(path)
        elif rc not in (0, None):
            drift.append((path, r.get("state"), out))

    md_sha = hashlib.sha1(text.encode()).hexdigest()[:12]
    con.execute("INSERT OR REPLACE INTO sync_runs VALUES (?,?,?,?)",
                (now, md_sha, rows_written, len(drift) + len(missing)))
    con.commit()

    if quiet:
        con.close()
        return 1 if (drift or missing) else 0

    print("\nPROJECT.md -> semantics.db")
    print("  %d rows across %d sections, md sha1 %s" % (rows_written, len(sections), md_sha))

    print("\nfiles")
    for row in con.execute("SELECT * FROM files ORDER BY path"):
        if not row["exists_on_disk"]:
            mark, detail = "MISSING", "not on disk"
        elif row["verify_rc"] in (0, None):
            mark, detail = "ok", "%d bytes, %s" % (row["bytes"], row["sha1"])
        else:
            mark, detail = "DRIFT", "rc=%s %s" % (row["verify_rc"], row["verify_out"])
        print("  %-7s %-24s %-10s %s" % (mark, row["path"], row["claimed_state"], detail))

    unverified = con.execute(
        "SELECT id, assumption FROM assumptions WHERE verified='no' ORDER BY id").fetchall()
    if unverified:
        print("\nunverified assumptions - do not build on these")
        for row in unverified:
            print("  %s  %s" % (row["id"], row["assumption"]))

    print("\nopen")
    for row in con.execute("SELECT * FROM open_items ORDER BY id"):
        print("  %s  %-44s blocked on: %s" % (row["id"], row["item"], row["blocked_on"]))

    print("\nnext")
    for row in con.execute("SELECT * FROM next_steps ORDER BY step"):
        print("  %d. %s" % (row["step"], row["action"]))

    con.close()
    if missing or drift:
        print("\n%d problem(s): %d missing, %d failing verify\n" % (
            len(missing) + len(drift), len(missing), len(drift)))
        return 1
    print("\nno drift - PROJECT.md matches what is on disk\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
