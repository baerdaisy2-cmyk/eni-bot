"""patch_status.py — stop every page load waiting on a dead scraper port.

Before: integrations() called scraper.status() on every request. With nothing
listening on port 8000 that is a 4 second connect timeout per page.
After: the result is cached for 60 seconds, and the timeout drops to 1.5s.

    python3 patch_status.py
"""
import os
import re
import sys

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper.py")

NEW = '''_STATUS_CACHE = {"at": 0, "value": None}
STATUS_TTL = int(os.environ.get("SCRAPER_STATUS_TTL", "60"))


def status(force=False):
    """Is the local scraper reachable? Cached, never raises."""
    now = time.time()
    if not force and _STATUS_CACHE["value"] and (now - _STATUS_CACHE["at"]) < STATUS_TTL:
        return _STATUS_CACHE["value"]

    if SCRAPER_BASE.lower() in ("disabled", "off", "none", ""):
        out = {"ok": False, "base": "disabled", "note": "turned off in .env"}
        _STATUS_CACHE.update({"at": now, "value": out})
        return out

    try:
        r = requests.get(SCRAPER_BASE + "/subreddits", timeout=1.5)
        if r.status_code == 200:
            data = r.json()
            n = len(data) if isinstance(data, list) else len(data.get("subreddits", []))
            out = {"ok": True, "base": SCRAPER_BASE, "note": "%d subreddits indexed" % n}
        else:
            out = {"ok": False, "base": SCRAPER_BASE, "note": "responded %d" % r.status_code}
    except Exception as exc:
        out = {"ok": False, "base": SCRAPER_BASE, "note": str(exc)[:70]}

    _STATUS_CACHE.update({"at": now, "value": out})
    return out
'''

OLD = re.compile(
    r'def status\(\):\n'
    r'    """Is the local scraper reachable\? Never raises\."""\n'
    r'(?:.*\n)*?'
    r'        return \{"ok": False, "base": SCRAPER_BASE, "note": str\(exc\)\[:90\]\}\n'
)


def main():
    if not os.path.exists(PATH):
        print("scraper.py not found next to this script")
        return 1
    src = open(PATH, encoding="utf-8").read()

    if "_STATUS_CACHE" in src:
        print("already patched, nothing to do")
        return 0
    if not OLD.search(src):
        print("could not find the status() block to replace")
        return 1

    open(PATH + ".bak", "w", encoding="utf-8").write(src)
    open(PATH, "w", encoding="utf-8").write(OLD.sub(NEW, src, count=1))
    print("patched scraper.py  (backup at scraper.py.bak)")
    print("  status cached for 60s, timeout 1.5s, SCRAPER_BASE=disabled honoured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
