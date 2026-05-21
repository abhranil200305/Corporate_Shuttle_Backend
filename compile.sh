#!/bin/bash

set -euo pipefail

cd /root/Corporate_Shuttle_Backend

APP_ENTRYPOINT="run_shuttlebe.py"
BINARY_NAME="shuttlebe"

RELEASE_ROOT="/root/Corporate_Shuttle_Backend/.compiled-releases"
BUILD_ID="$(date +%Y%m%d_%H%M%S)"
BUILD_DIR="$RELEASE_ROOT/$BUILD_ID"
CURRENT_LINK="/root/Corporate_Shuttle_Backend/shuttlebe.compiled"

echo "🧹 Preparing standalone Nuitka release directory..."
mkdir -p "$BUILD_DIR"

echo "🛠️ Compiling $APP_ENTRYPOINT with Nuitka standalone mode..."

./venv/bin/python3 -m nuitka "$APP_ENTRYPOINT" \
  --standalone \
  --output-filename="$BINARY_NAME" \
  --output-dir="$BUILD_DIR" \
  --remove-output \
  --nofollow-import-to=tests \
  --include-package=app \
  --include-module=_json \
  --include-data-dir="$PWD/app/auth/templates=app/auth/templates" \
  --static-libpython=yes \
  --clang

DIST_DIR="$BUILD_DIR/run_shuttlebe.dist"
DIST_BINARY="$DIST_DIR/$BINARY_NAME"

if [ ! -x "$DIST_BINARY" ]; then
  echo "❌ Expected compiled binary was not created or is not executable:"
  echo "$DIST_BINARY"
  exit 1
fi

echo "✅ Build completed:"
echo "$DIST_BINARY"

echo "🔁 Updating compiled runtime symlink atomically..."
ln -sfn "$DIST_DIR" "$CURRENT_LINK"

echo "🟢 Compiled Shuttle backend release is now active for next restart:"
echo "$CURRENT_LINK/$BINARY_NAME"
echo
echo "Restart when ready:"
echo "systemctl restart shuttlebe.service"
