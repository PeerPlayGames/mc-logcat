#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Merge Cruise Logcat — Release Builder
#  Usage: ./build.sh [version]
#  Example: ./build.sh 1.2.0
# ─────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

VERSION="${1:-1.0.0}"
APP_NAME="MergeCruiseLogcat"
DIST_DIR="dist"
RELEASE_DIR="release"

echo ""
echo "  ⚓  Merge Cruise Logcat — Build v${VERSION}"
echo "  ──────────────────────────────────────────"

# 1. Ensure dependencies
echo "  → Checking dependencies..."
pip3 install -q pyinstaller flask flask-socketio anthropic mitmproxy

# 2. Clean previous build
echo "  → Cleaning previous build..."
rm -rf build/ "$DIST_DIR/" "$RELEASE_DIR/"
mkdir -p "$RELEASE_DIR"

# 3. Update version in spec
sed -i '' "s/'CFBundleVersion':.*/'CFBundleVersion': '${VERSION}',/" mc-logcat.spec
sed -i '' "s/'CFBundleShortVersionString':.*/'CFBundleShortVersionString': '${VERSION}',/" mc-logcat.spec

# 4. Build the .app
echo "  → Building .app (this takes ~30s)..."
python3 -m PyInstaller mc-logcat.spec --noconfirm 2>&1 | grep -E "(INFO|ERROR|WARNING.*missing|completed)" | tail -20

# 5. Check build succeeded
if [ ! -d "$DIST_DIR/${APP_NAME}.app" ]; then
  echo "  ✗ Build failed — .app not found in dist/"
  exit 1
fi

# 6. Zip for GitHub release
echo "  → Zipping for release..."
cd "$DIST_DIR"
zip -r -q "../${RELEASE_DIR}/${APP_NAME}-v${VERSION}-macOS.zip" "${APP_NAME}.app"
cd ..

SIZE=$(du -sh "${RELEASE_DIR}/${APP_NAME}-v${VERSION}-macOS.zip" | cut -f1)
echo ""
echo "  ✓  Build complete!"
echo "  📦  ${RELEASE_DIR}/${APP_NAME}-v${VERSION}-macOS.zip (${SIZE})"
echo ""
echo "  Next steps:"
echo "  1. git tag v${VERSION} && git push origin v${VERSION}"
echo "  2. Go to GitHub → Releases → Draft new release"
echo "  3. Attach: ${RELEASE_DIR}/${APP_NAME}-v${VERSION}-macOS.zip"
echo ""
