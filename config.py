import os

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "eni_monitor.db")


def _load_env():
    """Read ~/eni-bot/.env into os.environ. KEY=value, # comments, no quoting rules."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_env()

# --- markers -----------------------------------------------------------------
MARKERS = ["prv8sup", "\U0001F34B"]

# Handles you post under. Comma separated in .env: ENI_AUTHORS=lo_handle
OUR_AUTHORS = [a.strip().lower() for a in os.environ.get("ENI_AUTHORS", "").split(",") if a.strip()]

# --- thresholds --------------------------------------------------------------
POST_DELETE_THRESHOLD = 2
SUBREDDIT_DELETE_RATE = 0.5
SUBREDDIT_MIN_SAMPLE = 5
FAST_DELETE_WINDOW = 3600  # under an hour alive -> smells like a filter, not a human

# --- reddit oauth (primary) --------------------------------------------------
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME = os.environ.get("REDDIT_USERNAME", "")
REDDIT_PASSWORD = os.environ.get("REDDIT_PASSWORD", "")
REDDIT_USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT",
    "macos:eni-monitor:0.1 (by /u/%s)" % (REDDIT_USERNAME or "unset"),
)

# --- redlib (read-only fallback, listings via RSS) ----------------------------
REDLIB_MIRRORS = [
    "https://redlib.catsarch.com",
    "https://redlib.perennialte.ch",
    "https://redlib.privacyredirect.com",
    "https://redlib.freedit.eu",
    "https://safereddit.com",
    "https://libreddit.projectsegfau.lt",
    "https://redlib.tiekoetter.com",
    "https://reddit.invak.id",
]

# --- subreddits you watch ----------------------------------------------------
WATCH_SUBREDDITS = [s.strip() for s in os.environ.get("ENI_SUBS", "IPTV,IPTVresellers").split(",") if s.strip()]

# --- llm (comment generator only, user-triggered) ----------------------------
LLM_BASE = os.environ.get("ENI_LLM_BASE", "http://localhost:3000/v1/chat/completions")
LLM_KEY = os.environ.get("ENI_LLM_KEY", "")
LLM_MODEL = os.environ.get("ENI_LLM_MODEL", "llama-3.3-70b-versatile")

HTTP_TIMEOUT = 25
