#!/bin/bash

set -u

cd /root/Corporate_Shuttle_Backend || exit 1

APP_BINARY="./shuttlebe"
APP_ENTRYPOINT="run_shuttlebe.py"

if [ -x "$APP_BINARY" ]; then
  echo "🟢 Running compiled Shuttle backend binary..."
  exec "$APP_BINARY"
fi

echo "🟡 Compiled binary not found. Running Shuttle backend via Python script in venv..."
exec ./venv/bin/python3 "$APP_ENTRYPOINT"
