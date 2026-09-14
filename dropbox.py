import os
import shutil

from flask import Flask, jsonify, request

ROOT = os.path.expanduser(os.environ.get("DROPBOX_ROOT", "~/eni-bot"))
AUTH_KEY = os.environ.get("DROPBOX_AUTH_KEY", "")
PORT = int(os.environ.get("DROPBOX_PORT", "7777"))

app = Flask(__name__)


def guard():
    if not AUTH_KEY:
        return jsonify({"error": "DROPBOX_AUTH_KEY is not set; refusing to start unguarded"}), 500
    if request.headers.get("X-Auth") != AUTH_KEY:
        return jsonify({"error": "bad or missing X-Auth"}), 401
    return None


def resolve(rel):
    target = os.path.realpath(os.path.join(ROOT, (rel or "").lstrip("/")))
    if not (target == os.path.realpath(ROOT) or target.startswith(os.path.realpath(ROOT) + os.sep)):
        raise ValueError("path escapes the root")
    return target


@app.route("/write", methods=["POST"])
def write():
    bad = guard()
    if bad:
        return bad
    try:
        path = resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(request.get_data())
    return jsonify({"ok": True, "path": path, "bytes": os.path.getsize(path)})


@app.route("/append", methods=["POST"])
def append():
    bad = guard()
    if bad:
        return bad
    try:
        path = resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "ab") as fh:
        fh.write(request.get_data())
    return jsonify({"ok": True, "path": path, "bytes": os.path.getsize(path)})


@app.route("/read")
def read():
    bad = guard()
    if bad:
        return bad
    try:
        path = resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not os.path.isfile(path):
        return jsonify({"error": "not a file"}), 404
    with open(path, "rb") as fh:
        return fh.read(), 200, {"Content-Type": "application/octet-stream"}


@app.route("/list")
def listing():
    bad = guard()
    if bad:
        return bad
    try:
        path = resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if not os.path.isdir(path):
        return jsonify({"error": "not a directory"}), 404
    items = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        items.append({"name": name, "is_dir": os.path.isdir(full),
                      "size": os.path.getsize(full) if os.path.isfile(full) else 0})
    return jsonify({"root": path, "items": items})


@app.route("/delete", methods=["DELETE"])
def delete():
    bad = guard()
    if bad:
        return bad
    try:
        path = resolve(request.args.get("path", ""))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.isfile(path):
        os.remove(path)
    else:
        return jsonify({"error": "nothing there"}), 404
    return jsonify({"ok": True, "removed": path})


if __name__ == "__main__":
    if not AUTH_KEY:
        raise SystemExit("set DROPBOX_AUTH_KEY first:  export DROPBOX_AUTH_KEY=$(openssl rand -hex 16)")
    print("dropbox root: %s" % ROOT)
    print("local: http://127.0.0.1:%d   (ngrok prints the public URL in its own window)" % PORT)
    app.run(host="127.0.0.1", port=PORT)
