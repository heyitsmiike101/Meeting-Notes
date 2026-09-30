"""The macOS client installer script served at ``/install/mac.sh``.

``curl -fsSL http://meeting.lan/install/mac.sh | bash`` downloads the package the
operator uploaded to ``<data>/client/MeetingNotes-macOS.zip``, verifies its size
and SHA-256 against ``/install/client-manifest-macos.json``, swaps the app bundle
into ``~/Applications`` safely (the previous copy is put back on any failure),
never touches recordings, keeps the existing config and token, points the config
at this server, and launches the app. The in-app updater downloads and runs the
same script.

The script targets the stock macOS ``/bin/bash`` (3.2) and only tools that ship
with macOS: curl, shasum, ditto, plutil, osascript, pgrep, xattr, codesign and open.
"""

from __future__ import annotations

import shlex

APP_NAME = "Meeting Notes.app"
BUNDLE_ID = "lan.meeting.notes"
PACKAGE_NAME = "MeetingNotes-macOS.zip"
MANIFEST_PATH = "/install/client-manifest-macos.json"
INSTALLER_PATH = "/install/mac.sh"
PACKAGE_PATH = "/install/" + PACKAGE_NAME

_TEMPLATE = r"""#!/bin/bash
# Meeting Notes installer for macOS (per-user; no sudo, no drivers).
# Usage:  curl -fsSL <server>/install/mac.sh | bash
set -euo pipefail

SERVER=__SERVER_ADDRESS__
MANIFEST_URL="$SERVER__MANIFEST_PATH__"
APP_NAME="__APP_NAME__"
BUNDLE_ID="__BUNDLE_ID__"
APP_DIR="$HOME/Applications"
APP="$APP_DIR/$APP_NAME"
CONFIG_DIR="$HOME/.meeting-notes"
CONFIG="$CONFIG_DIR/config.json"

step() { printf '\n==> %s\n' "$1"; }
warn() { printf 'Warning: %s\n' "$1" >&2; }
die()  { printf 'Error: %s\n' "$1" >&2; exit 1; }

main() {
    [ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS only."
    [ "$(uname -m)" = "arm64" ] || die "This build of Meeting Notes is for Apple silicon Macs."
    macos_major="$(sw_vers -productVersion | cut -d. -f1)"
    [ "$macos_major" -ge 13 ] || die "Meeting Notes needs macOS 13 or newer (this is $(sw_vers -productVersion))."
    [ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not with sudo."

    TMP="$(mktemp -d "${TMPDIR:-/tmp}/MeetingNotes.XXXXXX")"
    STAGED="$APP.new-$$"
    PREVIOUS="$APP.old-$$"
    trap 'rm -rf "$TMP" "$STAGED"' EXIT

    guard_recordings

    step "Downloading the Meeting Notes app"
    curl -fsSL --retry 2 -o "$TMP/manifest.json" "$MANIFEST_URL" || die "Could not read $MANIFEST_URL"
    url="$(plutil -extract url raw -o - "$TMP/manifest.json" 2>/dev/null || true)"
    want_hash="$(plutil -extract sha256 raw -o - "$TMP/manifest.json" 2>/dev/null | tr 'A-F' 'a-f' || true)"
    want_size="$(plutil -extract size raw -o - "$TMP/manifest.json" 2>/dev/null || true)"
    [ -n "$url" ] && [ -n "$want_hash" ] && [ -n "$want_size" ] || die "The server returned an invalid client manifest."
    [ "$want_size" -ge 1 ] 2>/dev/null || die "The server returned an invalid client manifest."
    case "$url" in http://*|https://*) ;; *) die "The client package URL is invalid." ;; esac
    host_of() { local rest="${1#*://}"; printf '%s' "${rest%%/*}"; }
    [ "$(host_of "$url")" = "$(host_of "$MANIFEST_URL")" ] || die "The client package must be hosted by the same server."
    curl -fSL --retry 2 -o "$TMP/app.zip" "$url" || die "Download failed."
    got_size="$(stat -f%z "$TMP/app.zip")"
    [ "$got_size" = "$want_size" ] || die "The downloaded package size does not match the server manifest."
    got_hash="$(shasum -a 256 "$TMP/app.zip" | cut -d' ' -f1)"
    [ "$got_hash" = "$want_hash" ] || die "The downloaded package hash does not match the server manifest."

    step "Unpacking"
    mkdir -p "$TMP/expanded"
    ditto -x -k "$TMP/app.zip" "$TMP/expanded"
    src="$(find "$TMP/expanded" -maxdepth 2 -name "$APP_NAME" -type d -print -quit)"
    [ -n "$src" ] || die "The package does not contain $APP_NAME."
    [ -d "$src/Contents/MacOS" ] || die "The package's app bundle is incomplete."
    found_id="$(plutil -extract CFBundleIdentifier raw -o - "$src/Contents/Info.plist" 2>/dev/null || true)"
    [ "$found_id" = "$BUNDLE_ID" ] || die "The package is not Meeting Notes (bundle id '$found_id')."
    xattr -dr com.apple.quarantine "$src" 2>/dev/null || true
    codesign --verify --deep "$src" 2>/dev/null || warn "The app's code signature did not verify."

    step "Installing to $APP"
    mkdir -p "$APP_DIR"
    ditto "$src" "$STAGED"
    stop_running_app
    if [ -e "$APP" ]; then
        mv "$APP" "$PREVIOUS" || die "Could not replace $APP (is it in use?). The installed version was left unchanged."
    fi
    if ! mv "$STAGED" "$APP"; then
        if [ -e "$PREVIOUS" ]; then mv "$PREVIOUS" "$APP"; fi
        die "Could not install the new app. The previous version was restored."
    fi
    rm -rf "$PREVIOUS" 2>/dev/null || true
    for leftover in "$APP".old-*; do
        if [ -e "$leftover" ]; then rm -rf "$leftover" 2>/dev/null || true; fi
    done

    step "Writing client configuration without replacing existing secrets"
    write_config

    step "Checking the transcription server"
    if curl -fsS --max-time 10 "$SERVER/health" >/dev/null 2>&1; then
        echo "Server is reachable: $SERVER"
    else
        warn "The client was installed, but the server is not reachable yet: $SERVER"
    fi

    step "Installation complete"
    echo "Installed: $APP"
    echo "First run: allow Microphone, and Screen & System Audio Recording"
    echo "(System Settings > Privacy & Security) when macOS asks, then reopen Meeting Notes."
    if [ -z "${MEETING_NOTES_NO_LAUNCH:-}" ]; then
        open "$APP" || warn "Could not launch the app; open it from ~/Applications."
    fi
}

# Where recordings live: save_dir from config.json, else ~/Meeting Notes.
recordings_dir() {
    local dir=""
    if [ -f "$CONFIG" ]; then
        dir="$(plutil -extract save_dir raw -o - "$CONFIG" 2>/dev/null || true)"
    fi
    if [ -z "$dir" ]; then dir="$HOME/Meeting Notes"; fi
    case "$dir" in
        "~") dir="$HOME" ;;
        "~/"*) dir="$HOME/${dir#\~/}" ;;
    esac
    printf '%s' "$dir"
}

# Replacing the app bundle must never take recordings with it.
guard_recordings() {
    local dir hit
    dir="$(recordings_dir)"
    case "$dir" in
        /*) ;;
        *) dir="$APP/$dir" ;;  # a relative folder resolves inside the app
    esac
    case "$dir/" in
        "$APP/"*|"$APP".old-*|"$APP".new-*)
            die "Your recordings folder is inside the app ($dir). Replacing the app would delete your recordings. Change the folder in Meeting Notes Settings (or move it) and run this again. Nothing was changed."
            ;;
    esac
    if [ -d "$APP" ]; then
        hit="$(find "$APP" \( -name '*.wav' -o -name '.upload-queue' \) -print -quit 2>/dev/null || true)"
        if [ -n "$hit" ]; then
            die "Recordings were found inside the app ($hit). Replacing the app would delete them. Move them out and run this again. Nothing was changed."
        fi
    fi
}

# Quit a running copy of the installed app (never any other process).
stop_running_app() {
    local pids pid
    pids="$(pgrep -f "$APP/Contents/MacOS/" 2>/dev/null || true)"
    if [ -z "$pids" ]; then return 0; fi
    echo "Closing the running Meeting Notes..."
    for pid in $pids; do kill -TERM "$pid" 2>/dev/null || true; done
    for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        pids="$(pgrep -f "$APP/Contents/MacOS/" 2>/dev/null || true)"
        if [ -z "$pids" ]; then return 0; fi
        sleep 0.5
    done
    for pid in $pids; do kill -KILL "$pid" 2>/dev/null || true; done
    sleep 1
}

# Point the config at this server, keeping the token and every other setting.
# JSON is merged with JavaScript for Automation (ships with macOS): plutil cannot
# edit an empty "{}" file and escapes slashes, and a token must never be lost.
write_config() {
    mkdir -p "$CONFIG_DIR"
    cat > "$TMP/merge-config.js" <<'JXA'
ObjC.import("Foundation");
function run(argv) {
    var path = argv[0], url = argv[1], cfg = {};
    if ($.NSFileManager.defaultManager.fileExistsAtPath(path)) {
        var text = ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError(path, $.NSUTF8StringEncoding, null));
        try { cfg = JSON.parse(text); } catch (e) { return "invalid"; }
        if (cfg === null || typeof cfg !== "object" || Array.isArray(cfg)) { return "invalid"; }
    }
    if (typeof cfg.server !== "object" || cfg.server === null) { cfg.server = {}; }
    if (cfg.server.token === undefined) { cfg.server.token = ""; }
    if (cfg.server.live_preview === undefined) { cfg.server.live_preview = true; }
    if (cfg.server.auto_upload === undefined) { cfg.server.auto_upload = true; }
    cfg.server.url = url;
    var out = $.NSString.stringWithString(JSON.stringify(cfg, null, 2) + "\n");
    var ok = out.writeToFileAtomicallyEncodingError(path, true, $.NSUTF8StringEncoding, null);
    return ok ? "ok" : "write-failed";
}
JXA
    result="$(/usr/bin/osascript -l JavaScript "$TMP/merge-config.js" "$CONFIG" "$SERVER" 2>&1 || true)"
    if [ "$result" = "invalid" ]; then
        backup="$CONFIG.invalid-$(date +%Y%m%d-%H%M%S)"
        cp "$CONFIG" "$backup"
        warn "The previous config was invalid and was backed up to $backup"
        rm -f "$CONFIG"
        result="$(/usr/bin/osascript -l JavaScript "$TMP/merge-config.js" "$CONFIG" "$SERVER" 2>&1 || true)"
    fi
    if [ "$result" != "ok" ]; then
        warn "Could not update $CONFIG ($result). Set the server URL to $SERVER in Settings."
    fi
}

main "$@"
"""


def render_mac_installer(server_address: str) -> str:
    """The installer script, configured for ``server_address`` (no token embedded)."""
    address = server_address.rstrip("/")
    return (
        # A Windows checkout may have turned the template's LFs into CRLFs; bash
        # would choke on the carriage returns.
        _TEMPLATE.replace("\r\n", "\n")
        .replace("__SERVER_ADDRESS__", shlex.quote(address))
        .replace("__MANIFEST_PATH__", MANIFEST_PATH)
        .replace("__APP_NAME__", APP_NAME)
        .replace("__BUNDLE_ID__", BUNDLE_ID)
    )
