"""
arctic_shift.py — Reddit data via Arctic Shift (no API key, no auth).
"""
import requests

BASE = "https://arctic-shift.photon-reddit.com/api"


def search_posts(subreddit=None, q=None, limit=25, after=None, before=None):
    """Search Reddit posts by subreddit, keyword, or date range."""
    params = {"limit": limit}
    if subreddit:
        params["subreddit"] = subreddit
    if q:
        params["query"] = q
    if after:
        params["after"] = after
    if before:
        params["before"] = before

    r = requests.get(f"{BASE}/posts/search", params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("data", [])


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
