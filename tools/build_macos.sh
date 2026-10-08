#!/bin/bash
# Build "Meeting Notes.app" (Apple silicon) and MeetingNotes-macOS.zip on a Mac.
#
# Reproduces the build the release engineer runs by hand on the build Mac:
#
#     cd <repo> && tools/build_macos.sh
#
# User-space only: no sudo, nothing outside $MN_BUILD_DIR (default
# ~/meeting-notes-build) and uv's own directories. Needs Xcode Command Line
# Tools (clang) and uv (https://astral.sh/uv, installed to ~/.local/bin if
# missing). Produces:
#
#     $MN_BUILD_DIR/dist/Meeting Notes.app
#     $MN_BUILD_DIR/dist/MeetingNotes-macOS.zip     (upload to <data>/client/)
#
# Environment overrides: MN_BUILD_DIR, MN_PYTHON (default 3.13), MN_SKIP_TESTS=1,
# MN_SIGN_DIR (absolute path of the signing directory made by tools/macos_signing_setup.sh;
# default ~/.meeting-notes-signing. Keep it outside MN_BUILD_DIR and never delete it).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${MN_BUILD_DIR:-$HOME/meeting-notes-build}"
PYVER="${MN_PYTHON:-3.13}"
VENV="$BUILD/venv-build"   # client + Nuitka only: what ships in the app
TEST_VENV="$BUILD/venv"     # everything, for the unit tests
WORK="$BUILD/nuitka"
DIST="$BUILD/dist"
APP_NAME="Meeting Notes"
BUNDLE_ID="lan.meeting.notes"
MIN_MACOS="13.0"
export PATH="$HOME/.local/bin:$PATH"
# Never let an unattended build pop a permission dialog on the Mac's screen.
export MEETING_NOTES_NO_PERMISSION_PROMPT=1
# Keep Nuitka's downloads/caches inside the build directory (not ~/Library/Caches).
export NUITKA_CACHE_DIR="$BUILD/nuitka-cache"

[ "$(uname -s)" = "Darwin" ] || { echo "This script must run on macOS." >&2; exit 1; }
[ "$(uname -m)" = "arm64" ] || echo "Warning: building on $(uname -m); the app will match this machine." >&2
[ "$(id -u)" -ne 0 ] || { echo "Do not run as root." >&2; exit 1; }
case "$BUILD" in "$HOME"/*) ;; *) echo "MN_BUILD_DIR must be under \$HOME." >&2; exit 1 ;; esac
command -v clang >/dev/null || { echo "Xcode Command Line Tools are missing (clang)." >&2; exit 1; }

step() { printf '\n==> %s\n' "$1"; }

step "Toolchain (uv + Python $PYVER in $VENV and $TEST_VENV)"
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
mkdir -p "$BUILD"
uv python install "$PYVER"
for v in "$VENV" "$TEST_VENV"; do
    [ -x "$v/bin/python" ] || uv venv --python "$PYVER" "$v"
done
PY="$VENV/bin/python"
TEST_PY="$TEST_VENV/bin/python"

step "Dependencies"
# The app is compiled from an environment with the client extra only (as on Windows),
# so the server stack (faster-whisper, onnxruntime, ...) never leaks into the bundle.
uv pip install --python "$PY" "$ROOT[client,build]"

VERSION="$(cd "$ROOT" && "$PY" -c 'import meeting_notes; print(meeting_notes.__version__)')"
echo "Meeting Notes version $VERSION"

if [ -z "${MN_SKIP_TESTS:-}" ]; then
    step "Unit tests"
    (cd "$ROOT" && uv pip install --python "$TEST_PY" "$ROOT[client,server,dev]" "av<17" && "$TEST_PY" -m pytest -p no:cacheprovider -q -rf --basetemp "$BUILD/pytest-tmp")
fi

step "App icon"
mkdir -p "$BUILD/icon"
"$PY" "$ROOT/tools/make_icns.py" "$BUILD/icon/MeetingNotes.icns"

step "Compiling with Nuitka (takes a while)"
rm -rf "$WORK" "$DIST"
mkdir -p "$WORK" "$DIST"
(
    cd "$ROOT"
    "$PY" -m nuitka --standalone \
        --macos-create-app-bundle --macos-app-mode=gui \
        --macos-target-arch=arm64 \
        --macos-app-name="$APP_NAME" \
        --macos-signed-app-name="$BUNDLE_ID" \
        --macos-app-version="$VERSION" \
        --macos-app-icon="$BUILD/icon/MeetingNotes.icns" \
        --macos-app-protected-resource="NSMicrophoneUsageDescription:Meeting Notes records your microphone so your side of a meeting can be transcribed." \
        --enable-plugin=pyside6 \
        --include-package=meeting_notes --include-package=soundcard \
        --include-package=objc --include-package=Foundation --include-package=AppKit \
        --include-package=CoreFoundation --include-package=Quartz \
        --include-package=CoreMedia --include-package=ScreenCaptureKit --include-package=AVFoundation --include-package=PyObjCTools \
        --include-package-data=meeting_notes --include-package-data=soundcard \
        --output-dir="$WORK" --output-filename=MeetingNotes \
        --assume-yes-for-downloads --remove-output \
        --disable-ccache \
        windows_client.py
)
BUILT="$(find "$WORK" -maxdepth 1 -name '*.app' -type d -print -quit)"
[ -n "$BUILT" ] || { echo "Nuitka did not produce an .app bundle." >&2; exit 1; }
APP="$DIST/$APP_NAME.app"
ditto "$BUILT" "$APP"

step "Info.plist"
PLIST="$APP/Contents/Info.plist"
set_plist() { # key type value
    /usr/bin/plutil -replace "$1" "-$2" "$3" "$PLIST" 2>/dev/null || /usr/bin/plutil -insert "$1" "-$2" "$3" "$PLIST"
}
set_plist CFBundleIdentifier string "$BUNDLE_ID"
set_plist CFBundleName string "$APP_NAME"
set_plist CFBundleDisplayName string "$APP_NAME"
set_plist CFBundleShortVersionString string "$VERSION"
set_plist CFBundleVersion string "$VERSION"
set_plist LSMinimumSystemVersion string "$MIN_MACOS"
set_plist NSHighResolutionCapable bool true
set_plist NSMicrophoneUsageDescription string "Meeting Notes records your microphone so your side of a meeting can be transcribed."
# ScreenCaptureKit system audio: shown by macOS in the Screen & System Audio Recording prompt.
set_plist NSScreenCaptureUsageDescription string "Meeting Notes captures the audio your Mac plays (the other people in a call). It never records your screen."
set_plist NSAudioCaptureUsageDescription string "Meeting Notes captures the audio your Mac plays (the other people in a call)."
# macOS 15+ Local Network privacy: without this key the prompt has no text, and a server on the LAN
# answers "No route to host" until the app is switched on under Privacy & Security > Local Network.
set_plist NSLocalNetworkUsageDescription string "Meeting Notes connects to your Meeting Notes server on this network."
/usr/bin/plutil -lint "$PLIST"

step "Code signing (identifier-based requirement so permission grants survive updates)"
# A stable self-signed identity (created once by tools/macos_signing_setup.sh) keeps the
# code signature's signer the same on every build, so macOS keeps the Microphone / Screen
# Recording grants after an update. Without it we fall back to an ad-hoc signature, which is
# different on every build and can make macOS ask for the permissions again.
SIGN_DIR="${MN_SIGN_DIR:-$HOME/.meeting-notes-signing}"
SIGN_KC="$SIGN_DIR/signing.keychain-db"
SIGN_PW_FILE="$SIGN_DIR/keychain-password"
SIGN_NAME="Meeting Notes Local Signing"
SIGN_ID="-"
SIGN_KC_ARGS=()
ORIG_KEYCHAINS=()
KEYCHAINS_CHANGED=""
restore_keychains() {
    if [ -n "$KEYCHAINS_CHANGED" ] && [ "${#ORIG_KEYCHAINS[@]}" -gt 0 ]; then
        security list-keychains -d user -s "${ORIG_KEYCHAINS[@]}" >/dev/null 2>&1 || true
    fi
}
trap restore_keychains EXIT
case "$SIGN_DIR" in /*) ;; *) echo "MN_SIGN_DIR must be an absolute path." >&2; exit 1 ;; esac
if [ -f "$SIGN_KC" ] && [ -f "$SIGN_PW_FILE" ]; then
    security unlock-keychain -p "$(cat "$SIGN_PW_FILE")" "$SIGN_KC"
    SIGN_HASH="$(security find-identity -p codesigning "$SIGN_KC" | grep -F "\"$SIGN_NAME\"" | awk '{print $2}' | head -n 1)"
    [ -n "$SIGN_HASH" ] || { echo "No \"$SIGN_NAME\" identity in $SIGN_KC. Re-run tools/macos_signing_setup.sh." >&2; exit 1; }
    # Add the signing keychain to the user's search list for this build only (restored on exit).
    while IFS= read -r line; do
        line="${line#"${line%%[![:space:]]*}"}"; line="${line%\"}"; line="${line#\"}"
        [ -n "$line" ] && ORIG_KEYCHAINS+=("$line")
    done < <(security list-keychains -d user)
    KEYCHAINS_CHANGED=1
    security list-keychains -d user -s "$SIGN_KC" ${ORIG_KEYCHAINS[@]+"${ORIG_KEYCHAINS[@]}"}
    SIGN_ID="$SIGN_HASH"
    SIGN_KC_ARGS=(--keychain "$SIGN_KC")
    echo "Signing with the stable local identity \"$SIGN_NAME\" (SHA-1 $SIGN_HASH)"
else
    echo "WARNING: no stable signing identity at $SIGN_KC; signing ad-hoc." >&2
    echo "WARNING: an ad-hoc signature changes on every build, so macOS may reset the Microphone /" >&2
    echo "WARNING: Screen Recording permissions after an update. Run tools/macos_signing_setup.sh once." >&2
fi
# Nested code first (each Mach-O gets its own signature), then the bundle.
# The designated requirement pins the bundle id instead of the code hash, so the
# Microphone / Screen Recording grants macOS stored for this app keep matching
# after an update replaces the binary.
find "$APP/Contents" -type f \( -name '*.dylib' -o -name '*.so' \) -print0 |
    xargs -0 -n 50 codesign --force -s "$SIGN_ID" ${SIGN_KC_ARGS[@]+"${SIGN_KC_ARGS[@]}"} >/dev/null 2>&1 || true
# With the stable identity the requirement also pins its certificate, so only builds signed with it match the
# stored grants (an ad-hoc build has no certificate to pin and keeps the identifier-only requirement).
REQ="designated => identifier \"$BUNDLE_ID\""
if [ -n "${SIGN_HASH:-}" ]; then REQ="$REQ and certificate leaf = H\"$SIGN_HASH\""; fi
codesign --force --deep -s "$SIGN_ID" ${SIGN_KC_ARGS[@]+"${SIGN_KC_ARGS[@]}"} --identifier "$BUNDLE_ID" \
    -r="$REQ" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
codesign -d -r- "$APP" 2>&1 | grep -i designated || true
codesign -dvv "$APP" 2>&1 | grep -E 'Authority|Signature|TeamIdentifier' || true

step "Smoke test (headless, direct binary)"
"$APP/Contents/MacOS/MeetingNotes" --smoke-test
echo "smoke test passed"

step "Zip"
rm -f "$DIST/MeetingNotes-macOS.zip"
(cd "$DIST" && ditto -c -k --keepParent "$APP_NAME.app" MeetingNotes-macOS.zip)
ls -l "$DIST/MeetingNotes-macOS.zip"
shasum -a 256 "$DIST/MeetingNotes-macOS.zip"
echo
echo "Done. Upload $DIST/MeetingNotes-macOS.zip to <server data>/client/MeetingNotes-macOS.zip"
