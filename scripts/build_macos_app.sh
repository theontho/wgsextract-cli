#!/bin/bash

set -euo pipefail

APP_NAME="WGS Extract"
BUNDLE_ID="org.wgsextract.cli"
CREATE_DMG=1
SIGN_IDENTITY="${CODESIGN_IDENTITY:--}"

usage() {
    printf '%s\n' "Usage: scripts/build_macos_app.sh [--no-dmg] [--app-name NAME] [--bundle-id ID]"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-dmg)
            CREATE_DMG=0
            shift
            ;;
        --app-name)
            APP_NAME="$2"
            shift 2
            ;;
        --bundle-id)
            BUNDLE_ID="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            printf 'Unknown argument: %s\n' "$1" >&2
            exit 2
            ;;
    esac
done

if [[ "$(uname -s)" != "Darwin" ]]; then
    printf '%s\n' "macOS app packaging must run on macOS." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PACKAGING_DIR="${REPO_ROOT}/packaging/macos"

PIXI_BIN="${PIXI_BIN:-}"
if [[ -z "${PIXI_BIN}" ]]; then
    PIXI_BIN="$(command -v pixi || true)"
fi

if [[ -z "${PIXI_BIN}" ]]; then
    printf '%s\n' "Pixi is required to build the macOS app." >&2
    printf '%s\n' "Install Pixi from https://pixi.sh, then rerun this script." >&2
    exit 1
fi

VERSION="$("${PIXI_BIN}" run python - <<'PY'
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

with Path("pyproject.toml").open("rb") as f:
    print(tomllib.load(f)["project"]["version"])
PY
)"

BUILD_ROOT="${REPO_ROOT}/out/macos"
APP_BUNDLE="${BUILD_ROOT}/${APP_NAME}.app"
CONTENTS_DIR="${APP_BUNDLE}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RESOURCES_DIR="${CONTENTS_DIR}/Resources"
APP_PROJECT_DIR="${RESOURCES_DIR}/app"
DMG_STAGE="${BUILD_ROOT}/dmg-stage"
DMG_SAFE_NAME="$(printf '%s' "${APP_NAME}" | tr ' ' '-')-${VERSION}-macOS"
DMG_PATH="${BUILD_ROOT}/${DMG_SAFE_NAME}.dmg"

printf 'Building %s %s\n' "${APP_NAME}" "${VERSION}"
printf 'Using Pixi: %s\n' "${PIXI_BIN}"
"${PIXI_BIN}" --version

SOURCE_REV="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
if [[ -z "${SOURCE_REV}" ]]; then
    SOURCE_REV="unknown"
fi
if [[ -n "$(git -C "${REPO_ROOT}" status --porcelain 2>/dev/null || true)" ]]; then
    SOURCE_REV="${SOURCE_REV}-dirty"
fi

rm -rf "${BUILD_ROOT}"
mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}" "${APP_PROJECT_DIR}"

rsync -a \
    --exclude '__pycache__/' \
    --exclude '*.pyc' \
    "${REPO_ROOT}/src/" "${APP_PROJECT_DIR}/src/"

cp "${REPO_ROOT}/pyproject.toml" "${APP_PROJECT_DIR}/"
cp "${REPO_ROOT}/pixi.toml" "${APP_PROJECT_DIR}/"
cp "${REPO_ROOT}/README.md" "${APP_PROJECT_DIR}/"
if [[ -f "${REPO_ROOT}/pixi.lock" ]]; then
    cp "${REPO_ROOT}/pixi.lock" "${APP_PROJECT_DIR}/"
fi
if [[ -f "${REPO_ROOT}/LICENSE" ]]; then
    cp "${REPO_ROOT}/LICENSE" "${APP_PROJECT_DIR}/"
fi
cat >"${APP_PROJECT_DIR}/.wgsextract-app-build" <<EOF
version=${VERSION}
source=${SOURCE_REV}
built_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
EOF

cp "${PACKAGING_DIR}/launch_gui.sh" "${MACOS_DIR}/${APP_NAME}"
cp "${PACKAGING_DIR}/app_env.sh" "${RESOURCES_DIR}/app_env.sh"
chmod +x "${MACOS_DIR}/${APP_NAME}"

ICON_SRC="${REPO_ROOT}/src/wgsextract_cli/ui/assets/icon.png"
ICON_PLIST_LINES=""
if [[ -f "${ICON_SRC}" && -x "/usr/bin/sips" && -x "/usr/bin/iconutil" ]]; then
    ICONSET="${BUILD_ROOT}/AppIcon.iconset"
    mkdir -p "${ICONSET}"
    /usr/bin/sips -z 16 16 "${ICON_SRC}" --out "${ICONSET}/icon_16x16.png" >/dev/null
    /usr/bin/sips -z 32 32 "${ICON_SRC}" --out "${ICONSET}/icon_16x16@2x.png" >/dev/null
    /usr/bin/sips -z 32 32 "${ICON_SRC}" --out "${ICONSET}/icon_32x32.png" >/dev/null
    /usr/bin/sips -z 64 64 "${ICON_SRC}" --out "${ICONSET}/icon_32x32@2x.png" >/dev/null
    /usr/bin/sips -z 128 128 "${ICON_SRC}" --out "${ICONSET}/icon_128x128.png" >/dev/null
    /usr/bin/sips -z 256 256 "${ICON_SRC}" --out "${ICONSET}/icon_128x128@2x.png" >/dev/null
    /usr/bin/sips -z 256 256 "${ICON_SRC}" --out "${ICONSET}/icon_256x256.png" >/dev/null
    /usr/bin/sips -z 512 512 "${ICON_SRC}" --out "${ICONSET}/icon_256x256@2x.png" >/dev/null
    /usr/bin/sips -z 512 512 "${ICON_SRC}" --out "${ICONSET}/icon_512x512.png" >/dev/null
    /usr/bin/sips -z 1024 1024 "${ICON_SRC}" --out "${ICONSET}/icon_512x512@2x.png" >/dev/null
    /usr/bin/iconutil -c icns "${ICONSET}" -o "${RESOURCES_DIR}/AppIcon.icns"
    ICON_PLIST_LINES="    <key>CFBundleIconFile</key>
    <string>AppIcon</string>"
fi

cat >"${CONTENTS_DIR}/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleDisplayName</key>
    <string>${APP_NAME}</string>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
${ICON_PLIST_LINES}
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>${VERSION}</string>
    <key>CFBundleVersion</key>
    <string>${VERSION}</string>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.medical</string>
    <key>LSMinimumSystemVersion</key>
    <string>12.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
EOF

/usr/bin/plutil -lint "${CONTENTS_DIR}/Info.plist"

if [[ "${WGSEXTRACT_SKIP_CODESIGN:-0}" != "1" ]] && command -v codesign >/dev/null 2>&1; then
    codesign --force --deep --sign "${SIGN_IDENTITY}" "${APP_BUNDLE}"
fi

if [[ "${CREATE_DMG}" -eq 1 ]]; then
    mkdir -p "${DMG_STAGE}"
    cp -R "${APP_BUNDLE}" "${DMG_STAGE}/"
    ln -s /Applications "${DMG_STAGE}/Applications"
    cp "${PACKAGING_DIR}/open_cli.command" "${DMG_STAGE}/WGS Extract CLI.command"
    chmod +x "${DMG_STAGE}/WGS Extract CLI.command"
    cp "${PACKAGING_DIR}/README.md" "${DMG_STAGE}/README.md"

    hdiutil create -volname "${APP_NAME}" -srcfolder "${DMG_STAGE}" -ov -format UDZO "${DMG_PATH}"
    shasum -a 256 "${DMG_PATH}" >"${DMG_PATH}.sha256"
    hdiutil verify "${DMG_PATH}"
    printf 'DMG: %s\n' "${DMG_PATH}"
    printf 'SHA256: %s.sha256\n' "${DMG_PATH}"
else
    printf 'App: %s\n' "${APP_BUNDLE}"
fi
