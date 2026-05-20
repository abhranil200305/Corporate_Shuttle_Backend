#!/bin/bash

set -u

cd /root/Corporate_Shuttle_Backend || exit 1

APP_BINARY="./shuttlebe"
APP_ENTRYPOINT="run_shuttlebe.py"

if [ -x "./venv/bin/nuitka" ]; then
  if [ ! -f "$APP_BINARY" ]; then
    echo "🛠️ Compiling $APP_ENTRYPOINT using venv Nuitka..."

    ./venv/bin/nuitka "$APP_ENTRYPOINT" \
      --standalone \
      --onefile \
      --output-filename=shuttlebe \
      --output-dir=. \
      --remove-output \
      --nofollow-import-to=tests \
      --include-package=app \
      --include-package=uvicorn \
      --include-package=fastapi \
      --include-package=starlette \
      --include-package=pydantic \
      --include-package=sqlalchemy \
      --include-package=asyncpg \
      --include-package=socketio \
      --include-package=engineio \
      --static-libpython=yes \
      --clang \
      --lto=yes

    if [ -f "./shuttlebe.bin" ] && [ ! -f "$APP_BINARY" ]; then
      mv ./shuttlebe.bin "$APP_BINARY"
    fi
  fi
else
  echo "❌ Nuitka not found in venv. Skipping compile."
fi

if [ -x "$APP_BINARY" ]; then
  echo "🟢 Running compiled Shuttle backend binary..."
  exec "$APP_BINARY"
else
  echo "🟡 Running Shuttle backend via Python script in venv..."
  exec ./venv/bin/python3 "$APP_ENTRYPOINT"
fi
