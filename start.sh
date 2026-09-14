#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
export DROPBOX_AUTH_KEY="${DROPBOX_AUTH_KEY:-$(openssl rand -hex 16)}"
echo "X-Auth key: $DROPBOX_AUTH_KEY"
ngrok http 7777 --log=stdout > ngrok.log 2>&1 &
sleep 3
echo -n "public URL: "
curl -s localhost:4040/api/tunnels | python3 -c 'import sys,json;print(json.load(sys.stdin)["tunnels"][0]["public_url"])'
python3 dropbox.py
