import requests

import config
import db

FORMULA = None  # you'll hand me this later; until then nothing generates.


def configured():
    return bool(config.LLM_KEY) or config.LLM_BASE.startswith("http://localhost")


def status():
    return {"configured": configured(), "base": config.LLM_BASE,
            "model": config.LLM_MODEL, "formula": bool(FORMULA)}


def generate_comment(post, formula=None, model="auto"):
    formula = formula or FORMULA
    if not formula:
        raise NotImplementedError("waiting on the user's comment formula")

    top = ""
    con = db.connect()
    rows = con.execute(
        "SELECT author, body FROM comments WHERE post_id=? AND deleted=0 ORDER BY score DESC LIMIT 5",
        (post["id"],)).fetchall()
    con.close()
    for r in rows:
        top += "u/%s: %s\n" % (r["author"], (r["body"] or "")[:400])

    prompt = (
        "Thread: r/%s\nTitle: %s\n\nBody:\n%s\n\nTop replies:\n%s\n\n"
        "Write one reply following this structure:\n%s" % (
            post.get("subreddit", ""), post.get("title", ""),
            (post.get("body") or "")[:1500], top or "(none)", formula)
    )

    headers = {"Content-Type": "application/json"}
    if config.LLM_KEY:
        headers["Authorization"] = "Bearer " + config.LLM_KEY

    resp = requests.post(
        config.LLM_BASE,
        headers=headers,
        json={"model": config.LLM_MODEL if model == "auto" else model,
              "messages": [{"role": "user", "content": prompt}],
              "max_tokens": 500, "temperature": 0.8},
        timeout=60,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    db.log("INFO", "comment_gen", "drafted a reply for post %s (not posted)" % post["id"],
           {"post_id": post["id"], "chars": len(text)})
    return text
