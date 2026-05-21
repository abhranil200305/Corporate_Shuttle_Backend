#!/bin/bash

set -u

cd /root/Corporate_Shuttle_Backend || exit 1

COMPILED_BINARY="./shuttlebe.compiled/shuttlebe"
APP_ENTRYPOINT="run_shuttlebe.py"

if [ -x "$COMPILED_BINARY" ]; then
  echo "🟢 Running compiled standalone Shuttle backend binary..."
  exec "$COMPILED_BINARY"
fi

echo "🟡 Compiled standalone binary not found. Running Shuttle backend via Python script in venv..."
exec ./venv/bin/python3 "$APP_ENTRYPOINT"
