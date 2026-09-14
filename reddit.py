import time
import xml.etree.ElementTree as ET

import requests

import config
import db


class Blocked(Exception):
    """A host actively refused us. Not a failover case."""


def _marker_hit(text):
    low = (text or "").lower()
    return int(any(m.lower() in low for m in config.MARKERS))


def _is_ours(author, body):
    if author and author.lower() in config.OUR_AUTHORS:
        return 1
    return _marker_hit(body)


# ---------------------------------------------------------------- OAuth ------
class OAuthBackend:
    name = "oauth"

    def __init__(self):
        self._token = None
        self._expires = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.REDDIT_USER_AGENT

    @property
    def configured(self):
        return all([config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET,
                    config.REDDIT_USERNAME, config.REDDIT_PASSWORD])

    def _auth(self):
        if self._token and time.time() < self._expires - 60:
            return self._token
        resp = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET),
            data={"grant_type": "password",
                  "username": config.REDDIT_USERNAME,
                  "password": config.REDDIT_PASSWORD},
            headers={"User-Agent": config.REDDIT_USER_AGENT},
            timeout=config.HTTP_TIMEOUT,
        )
        if resp.status_code in (401, 403):
            raise Blocked("reddit rejected the credentials (%s)" % resp.status_code)
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        self._expires = time.time() + payload.get("expires_in", 3600)
        return self._token

    def get(self, path, params=None):
        token = self._auth()
        resp = self.session.get(
            "https://oauth.reddit.com" + path,
            params=params or {},
            headers={"Authorization": "Bearer " + token},
            timeout=config.HTTP_TIMEOUT,
        )
        if resp.status_code == 429:
            raise Blocked("rate limited by reddit; back off and retry later")
        resp.raise_for_status()
        return resp.json()


# --------------------------------------------------------------- Redlib -----
class RedlibBackend:
    """Read-only listings via RSS. No comment threads. No challenge solving."""

    name = "redlib"

    def __init__(self):
        self.host = None
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.REDDIT_USER_AGENT

    @property
    def configured(self):
        return True

    def _fetch_rss(self, path):
        hosts = ([self.host] if self.host else []) + [h for h in config.REDLIB_MIRRORS if h != self.host]
        last = None
        for host in hosts:
            try:
                resp = self.session.get(host + path, timeout=config.HTTP_TIMEOUT)
            except requests.RequestException as exc:
                last = str(exc)
                continue
            if resp.status_code in (403, 429) or "not a bot" in resp.text[:600].lower():
                # A gate, not an outage. We do not shop mirrors to get around it.
                raise Blocked("%s is gated (%s). use OAuth." % (host, resp.status_code))
            if resp.status_code >= 500 or resp.status_code == 404:
                last = "%s -> %s" % (host, resp.status_code)
                continue
            if resp.status_code == 200:
                self.host = host
                return resp.text
            last = "%s -> %s" % (host, resp.status_code)
        raise RuntimeError("every redlib mirror was unreachable (%s)" % last)


# --------------------------------------------------------------- client -----
class RedditClient:
    def __init__(self):
        self.oauth = OAuthBackend()
        self.redlib = RedlibBackend()

    @property
    def active(self):
        return "oauth" if self.oauth.configured else "redlib"

    def status(self):
        return {
            "backend": self.active,
            "oauth_configured": self.oauth.configured,
            "redlib_host": self.redlib.host or "unused",
        }

    # -- normalizers ---------------------------------------------------------
    @staticmethod
    def _norm_post(data):
        return {
            "id": data.get("id", ""),
            "subreddit": data.get("subreddit", ""),
            "author": data.get("author", "[unknown]"),
            "title": data.get("title", ""),
            "body": data.get("selftext", "") or "",
            "score": int(data.get("score") or 0),
            "num_comments": int(data.get("num_comments") or 0),
            "created_utc": int(data.get("created_utc") or time.time()),
            "permalink": "https://reddit.com" + data.get("permalink", ""),
        }

    @staticmethod
    def _walk_comments(children, post_id, out):
        for child in children or []:
            if child.get("kind") != "t1":
                continue
            data = child.get("data", {})
            body = data.get("body", "") or ""
            author = data.get("author", "[unknown]")
            out.append({
                "id": data.get("id", ""),
                "post_id": post_id,
                "parent_id": (data.get("parent_id") or "").split("_")[-1],
                "author": author,
                "body": body,
                "score": int(data.get("score") or 0),
                "created_utc": int(data.get("created_utc") or time.time()),
                "is_ours": _is_ours(author, body),
                "mentions_marker": _marker_hit(body),
            })
            replies = data.get("replies")
            if isinstance(replies, dict):
                RedditClient._walk_comments(replies.get("data", {}).get("children"), post_id, out)
        return out

    # -- public --------------------------------------------------------------
    def fetch_posts(self, subreddit, limit=50):
        if self.oauth.configured:
            payload = self.oauth.get("/r/%s/new" % subreddit, {"limit": min(limit, 100)})
            return [self._norm_post(c["data"]) for c in payload["data"]["children"] if c.get("kind") == "t3"]

        xml = self.redlib._fetch_rss("/r/%s/new.rss" % subreddit)
        root = ET.fromstring(xml)
        posts = []
        for item in root.iter("item")[:limit] if hasattr(root.iter("item"), "__getitem__") else list(root.iter("item"))[:limit]:
            link = (item.findtext("link") or "")
            pid = link.rstrip("/").split("/comments/")[-1].split("/")[0] if "/comments/" in link else link[-8:]
            posts.append({
                "id": pid, "subreddit": subreddit,
                "author": (item.findtext("author") or "[unknown]").lstrip("/u/"),
                "title": item.findtext("title") or "", "body": "",
                "score": 0, "num_comments": 0,
                "created_utc": int(time.time()), "permalink": link,
            })
        db.log("WARN", "reddit", "using redlib fallback: listings only, no scores, no comment threads",
               {"host": self.redlib.host, "subreddit": subreddit})
        return posts

    def fetch_comments(self, post_id, subreddit=""):
        if not self.oauth.configured:
            raise NotImplementedError(
                "comment threads need OAuth. redlib fallback serves listings only.")
        payload = self.oauth.get("/comments/%s" % post_id, {"limit": 500, "depth": 10, "sort": "new"})
        if len(payload) < 2:
            return []
        return self._walk_comments(payload[1]["data"]["children"], post_id, [])

    def search_posts(self, query, limit=50):
        if not self.oauth.configured:
            raise NotImplementedError("search needs OAuth.")
        payload = self.oauth.get("/search", {"q": query, "limit": min(limit, 100), "sort": "new", "type": "link"})
        return [self._norm_post(c["data"]) for c in payload["data"]["children"] if c.get("kind") == "t3"]


client = RedditClient()
