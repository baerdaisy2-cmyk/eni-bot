"""patch_disabled.py — make every scraper call respect SCRAPER_BASE=disabled.

The first patch only guarded status(). Pressing "Scrape and enrich" or "Sweep now"
still called the dead port and waited for a timeout. This guards the shared _get()
helper, so every path returns a clean message instead of a connection error.

    python3 patch_disabled.py
"""
import os
import sys

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper.py")

OLD = '''def _get(path, params=None):
    r = requests.get(SCRAPER_BASE + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()'''

NEW = '''def disabled():
    return SCRAPER_BASE.lower() in ("disabled", "off", "none", "")


def _get(path, params=None):
    if disabled():
        raise LookupError(
            "The scraper is switched off. Set SCRAPER_BASE in .env to a running "
            "source to turn it back on.")
    r = requests.get(SCRAPER_BASE + path, params=params or {}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()'''


def main():
    if not os.path.exists(PATH):
        print("scraper.py not found next to this script")
        return 1
    src = open(PATH, encoding="utf-8").read()

    if "def disabled():" in src:
        print("already patched, nothing to do")
        return 0
    if OLD not in src:
        print("could not find the _get() block to replace")
        return 1

    open(PATH + ".bak2", "w", encoding="utf-8").write(src)
    open(PATH, "w", encoding="utf-8").write(src.replace(OLD, NEW, 1))
    print("patched scraper.py  (backup at scraper.py.bak2)")
    print("  every scraper call now returns a clean message when disabled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
