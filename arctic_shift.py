"""
arctic_shift.py — Reddit data via Arctic Shift (no API key, no auth).
"""
import requests
import time

BASE = "https://arctic-shift.photon-reddit.com/api"


MIN_LIMIT = 5
MAX_LIMIT = 50


MIN_LIMIT = 5
MAX_LIMIT = 50

_last_call = [0.0]
_MIN_GAP = 1.5  # seconds between Arctic Shift calls, global


def search_posts(subreddit=None, q=None, limit=25, after=None, before=None, _retries=3):
    """Search Reddit posts via Arctic Shift.

    - Clamps limit to [5, 50]
    - Rejects keywords shorter than 4 chars
    - Throttles calls to at most 1 per 1.5s (global)
    - Retries 3x with exponential backoff on 422/429/5xx
    """
    if q is not None and len(q) < 4:
        raise ValueError("keyword %r too short for Arctic Shift (min 4 chars)" % q)

    limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit)))

    params = {"limit": limit}
    if subreddit:
        params["subreddit"] = subreddit
    if q:
        params["query"] = q
    if after:
        params["after"] = after
    if before:
        params["before"] = before

    last_exc = None
    for attempt in range(_retries + 1):
        # global throttle: don't call faster than _MIN_GAP seconds apart
        now = time.time()
        wait = _last_call[0] + _MIN_GAP - now
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()

        try:
            r = requests.get(f"{BASE}/posts/search", params=params, timeout=30)
            r.raise_for_status()
            return r.json().get("data", [])
        except requests.HTTPError as exc:
            last_exc = exc
            code = exc.response.status_code if exc.response is not None else 0
            # 422 is Arctic Shift's soft rate-limit signal
            if code in (422, 429, 500, 502, 503, 504) and attempt < _retries:
                backoff = 2 ** attempt + (2 if code == 422 else 0)
                time.sleep(backoff)
                continue
            raise
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _retries:
                time.sleep(2 ** attempt)
                continue
            raise

    if last_exc:
        raise last_exc
    return []



def fetch_post(post_id):
    """Fetch a single post by id, with or without the 't3_' prefix."""
    bare_id = post_id[3:] if post_id.startswith("t3_") else post_id

    r = requests.get(
        f"{BASE}/posts/ids",
        params={"ids": bare_id},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return data[0] if data else None


def fetch_thread(post_id):
    """Fetch a full comment tree for a post. post_id like '1wkvpev' or 't3_1wkvpev'."""
    if not post_id.startswith("t3_"):
        post_id = f"t3_{post_id}"

    r = requests.get(
        f"{BASE}/comments/tree",
        params={"link_id": post_id},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def flatten_comments(children, out=None):
    """Turn the nested replies tree into a flat list of comment dicts."""
    if out is None:
        out = []
    for node in children:
        if node.get("kind") != "t1":
            continue
        d = node["data"]
        out.append(d)
        replies = d.get("replies")
        if isinstance(replies, dict):
            kids = replies.get("data", {}).get("children", [])
            flatten_comments(kids, out)
    return out


if __name__ == "__main__":
    posts = search_posts(subreddit="Morocco", limit=5)
    for p in posts:
        print(p["id"], p.get("removed_by_category"), p["title"][:60])
