#!/bin/bash

set -euo pipefail

cd /root/Corporate_Shuttle_Backend

APP_ENTRYPOINT="run_shuttlebe.py"
FINAL_BINARY="./shuttlebe"
BUILD_DIR="./.nuitka-build"
TEMP_BINARY_NAME="shuttlebe.new"
TEMP_BINARY="$BUILD_DIR/$TEMP_BINARY_NAME"

echo "🧹 Cleaning old Nuitka build residue..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

echo "🛠️ Compiling $APP_ENTRYPOINT with Nuitka..."

./venv/bin/python3 -m nuitka "$APP_ENTRYPOINT" \
  --standalone \
  --onefile \
  --output-filename="$TEMP_BINARY_NAME" \
  --output-dir="$BUILD_DIR" \
  --remove-output \
  --nofollow-import-to=tests \
  --include-package=app \
  --static-libpython=yes \
  --clang

if [ ! -f "$TEMP_BINARY" ]; then
  echo "❌ Expected binary was not created: $TEMP_BINARY"
  exit 1
fi

chmod +x "$TEMP_BINARY"

echo "✅ Build completed."
echo "🔁 Replacing runtime binary atomically..."

mv "$TEMP_BINARY" "$FINAL_BINARY"
chmod +x "$FINAL_BINARY"

echo "🟢 Compiled Shuttle backend binary is ready at:"
echo "$FINAL_BINARY"
echo
echo "Restart service when you want systemd to use it:"
echo "systemctl restart shuttlebe.service"
