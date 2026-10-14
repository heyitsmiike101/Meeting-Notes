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
UNINSTALLER_PATH = "/install/uninstall-mac.sh"
PACKAGE_PATH = "/install/" + PACKAGE_NAME

_TEMPLATE = r"""#!/bin/bash
# Meeting Notes installer for macOS (per-user; no sudo, no drivers).
# Usage:  curl -fsSL <server>/install/mac.sh | bash
set -Eeuo pipefail

# The parent (the app doing a self-update) quits mid-run: never die from its hangup.
trap '' HUP

SERVER=__SERVER_ADDRESS__
MANIFEST_URL="$SERVER__MANIFEST_PATH__"
APP_NAME="__APP_NAME__"
BUNDLE_ID="__BUNDLE_ID__"
APP_DIR="$HOME/Applications"
APP="$APP_DIR/$APP_NAME"
CONFIG_DIR="$HOME/.meeting-notes"
CONFIG="$CONFIG_DIR/config.json"
LOG_DIR="$CONFIG_DIR/logs"
UPDATE_LOG="$LOG_DIR/update.log"
TMP=""
STAGED=""

# Logging. Every run appends timestamped lines to $UPDATE_LOG. The in-app updater sets
# MEETING_NOTES_UPDATE=1 and has no terminal, so there all output goes to the log file;
# an interactive curl | bash install also prints to the terminal as before.
UPDATING="${MEETING_NOTES_UPDATE:-}"

logline() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$UPDATE_LOG" 2>/dev/null || true; }

# Keep the log small: past 256 KB keep only the last 200 lines.
setup_log() {
    mkdir -p "$LOG_DIR" 2>/dev/null || true
    if [ -f "$UPDATE_LOG" ]; then
        local size
        size="$(wc -c <"$UPDATE_LOG" 2>/dev/null | tr -d ' ' || true)"
        if [ "${size:-0}" -gt 262144 ] 2>/dev/null; then
            tail -n 200 "$UPDATE_LOG" >"$UPDATE_LOG.tmp" 2>/dev/null && mv "$UPDATE_LOG.tmp" "$UPDATE_LOG" || true
        fi
    fi
    if [ -n "$UPDATING" ]; then
        exec >>"$UPDATE_LOG" 2>&1 </dev/null
    fi
}

info() { logline "$1"; if [ -z "$UPDATING" ]; then printf '%s\n' "$1"; fi; }
step() { logline "==> $1"; if [ -z "$UPDATING" ]; then printf '\n==> %s\n' "$1"; fi; }
warn() { logline "WARNING: $1"; if [ -z "$UPDATING" ]; then printf 'Warning: %s\n' "$1" >&2; fi; }
die()  { logline "ERROR: $1"; if [ -z "$UPDATING" ]; then printf 'Error: %s\n' "$1" >&2; fi; exit 1; }

on_err() { local rc=$1 line=$2 cmd=$3; logline "command failed (status $rc) at line $line: $cmd"; }
on_exit() {
    local rc=$?
    rm -rf "${TMP:-}" "${STAGED:-}" 2>/dev/null || true
    logline "installer finished with status $rc"
}
trap 'on_err $? $LINENO "$BASH_COMMAND"' ERR
trap on_exit EXIT
trap 'logline "received SIGTERM"; exit 143' TERM

main() {
    setup_log
    logline "---- installer started (pid $$, update=${UPDATING:-0}, server=$SERVER)"
    [ "$(uname -s)" = "Darwin" ] || die "This installer is for macOS only."
    [ "$(uname -m)" = "arm64" ] || die "This build of Meeting Notes is for Apple silicon Macs."
    macos_major="$(sw_vers -productVersion | cut -d. -f1)"
    [ "$macos_major" -ge 13 ] || die "Meeting Notes needs macOS 13 or newer (this is $(sw_vers -productVersion))."
    [ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not with sudo."

    TMP="$(mktemp -d "${TMPDIR:-/tmp}/MeetingNotes.XXXXXX")"
    STAGED="$APP.new-$$"
    PREVIOUS="$APP.old-$$"

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
        info "Server is reachable: $SERVER"
    else
        warn "The client was installed, but the server is not reachable yet: $SERVER"
    fi

    step "Installation complete"
    info "Installed: $APP"
    info "First run: allow Microphone, and Screen & System Audio Recording"
    info "(System Settings > Privacy & Security) when macOS asks, then reopen Meeting Notes."
    if [ -z "${MEETING_NOTES_NO_LAUNCH:-}" ]; then
        launch_app
    else
        info "MEETING_NOTES_NO_LAUNCH is set: not opening the app."
    fi
}

app_running() { [ -n "$(pgrep -f "$APP/Contents/MacOS/" 2>/dev/null || true)" ]; }

# Wait up to $1 seconds for the app's process to show up.
wait_for_app() {
    local i=0
    while [ "$i" -lt "$1" ]; do
        if app_running; then return 0; fi
        sleep 1
        i=$((i + 1))
    done
    return 1
}

# Reopen the app. Right after the old copy was quit, LaunchServices can still treat it as
# running and `open` silently does nothing, so confirm the process really started and fall
# back through three methods, logging which one worked.
launch_app() {
    local exe_name exe rc
    step "Opening Meeting Notes"
    sleep 2
    if app_running; then info "Meeting Notes is already running."; return 0; fi
    rc=0; open "$APP" || rc=$?
    if [ "$rc" -eq 0 ]; then
        if wait_for_app 8; then info "Launched with: open"; return 0; fi
        warn "open returned success but the app did not start"
    else
        warn "open failed (status $rc)"
    fi
    sleep 2
    rc=0; open -a "$APP" || rc=$?
    if [ "$rc" -eq 0 ]; then
        if wait_for_app 8; then info "Launched with: open -a"; return 0; fi
        warn "open -a returned success but the app did not start"
    else
        warn "open -a failed (status $rc)"
    fi
    exe_name="$(plutil -extract CFBundleExecutable raw -o - "$APP/Contents/Info.plist" 2>/dev/null || true)"
    if [ -z "$exe_name" ]; then exe_name="MeetingNotes"; fi
    exe="$APP/Contents/MacOS/$exe_name"
    if [ -x "$exe" ]; then
        # Detached from this script (and its log): the app must outlive the installer.
        env -u MEETING_NOTES_UPDATE -u MEETING_NOTES_NO_LAUNCH nohup "$exe" >/dev/null 2>&1 </dev/null &
        if wait_for_app 8; then info "Launched directly: $exe"; return 0; fi
    fi
    warn "Could not launch the app; open it from ~/Applications."
    return 0
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
    info "Closing the running Meeting Notes..."
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


_UNINSTALL_TEMPLATE = r"""#!/bin/bash
# Meeting Notes uninstaller for macOS (per-user; no sudo).
# Usage:  curl -fsSL <server>/install/uninstall-mac.sh | bash
#         curl -fsSL <server>/install/uninstall-mac.sh | bash -s -- --keep-settings
#
# Removes the app and (unless --keep-settings) the settings and logs in ~/.meeting-notes.
# It never deletes recordings. The installer creates no LaunchAgent and the app registers
# no login item, so there is nothing of that kind to remove.
set -euo pipefail

APP_NAME="__APP_NAME__"
BUNDLE_ID="__BUNDLE_ID__"
USER_APP="$HOME/Applications/$APP_NAME"
SYSTEM_APP="/Applications/$APP_NAME"
CONFIG_DIR="$HOME/.meeting-notes"
CONFIG="$CONFIG_DIR/config.json"
KEEP_SETTINGS=0
TARGETS=""

info() { printf '%s\n' "$1"; }
step() { printf '\n==> %s\n' "$1"; }
warn() { printf 'Warning: %s\n' "$1" >&2; }
die()  { printf 'Error: %s\n' "$1" >&2; exit 1; }

usage() {
    info "Usage: bash uninstall-mac.sh [--keep-settings]"
    info "  --keep-settings   keep ~/.meeting-notes (server address, token, logs)"
    info "Recordings are never deleted."
}

# The folder a path really is (symlinks resolved) when it exists, else the path as given.
phys() {
    if [ -d "$1" ]; then
        (cd "$1" 2>/dev/null && pwd -P) || printf '%s' "$1"
    else
        printf '%s' "$1"
    fi
}

# True when $1 is $2 or lies below it.
inside() {
    case "$1/" in
        "$2/"*) return 0 ;;
    esac
    return 1
}

# The bundle id of an app, read with whatever this Mac has.
bundle_id_of() {
    local plist="$1/Contents/Info.plist" id=""
    [ -f "$plist" ] || return 0
    if [ -x /usr/libexec/PlistBuddy ]; then
        id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$plist" 2>/dev/null || true)"
    fi
    if [ -z "$id" ] && command -v plutil >/dev/null 2>&1; then
        id="$(plutil -extract CFBundleIdentifier raw -o - "$plist" 2>/dev/null || true)"
    fi
    if [ -z "$id" ]; then
        id="$(grep -A1 '<key>CFBundleIdentifier</key>' "$plist" 2>/dev/null | sed -n 's/.*<string>\(.*\)<\/string>.*/\1/p' | head -n 1 || true)"
    fi
    printf '%s' "$id"
}

# Where recordings live: save_dir from config.json, else ~/Meeting Notes.
recordings_dir() {
    local dir=""
    if [ -f "$CONFIG" ]; then
        if command -v plutil >/dev/null 2>&1; then
            dir="$(plutil -extract save_dir raw -o - "$CONFIG" 2>/dev/null || true)"
        fi
        if [ -z "$dir" ]; then
            dir="$(sed -n 's/.*"save_dir"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG" 2>/dev/null | head -n 1 || true)"
        fi
    fi
    if [ -z "$dir" ]; then dir="$HOME/Meeting Notes"; fi
    case "$dir" in
        "~") dir="$HOME" ;;
        "~/"*) dir="$HOME/${dir#\~/}" ;;
    esac
    printf '%s' "$dir"
}

add_target() { TARGETS="$TARGETS$1
"; }

# Refuse (changing nothing) if a recordings folder is inside something this script would delete.
guard_recordings() {
    local rec="$1" target rec_p target_p
    case "$rec" in
        /*) ;;
        *) die "The recordings folder setting is a relative path ($rec). Set an absolute folder in Meeting Notes Settings and run this again. Nothing was changed." ;;
    esac
    case "$rec" in
        */../*|*/..) die "The recordings folder ($rec) is not a plain path. Nothing was changed." ;;
    esac
    while [ "${#rec}" -gt 1 ] && [ "${rec%/}" != "$rec" ]; do rec="${rec%/}"; done
    rec_p="$(phys "$rec")"
    while IFS= read -r target; do
        [ -n "$target" ] || continue
        target_p="$(phys "$target")"
        if inside "$rec" "$target" || inside "$rec_p" "$target" || inside "$rec" "$target_p" || inside "$rec_p" "$target_p"; then
            die "Your recordings folder ($rec) is inside $target, which this would delete. Move the recordings out (or change the folder in Meeting Notes Settings) and run this again; for a recordings folder inside ~/.meeting-notes you can also use --keep-settings. Nothing was changed."
        fi
    done <<EOF
$TARGETS
EOF
}

# Recordings that were put inside a folder this would delete.
guard_stray_recordings() {
    local target hit
    while IFS= read -r target; do
        [ -n "$target" ] || continue
        [ -d "$target" ] || continue
        hit="$(find "$target" \( -name '*.wav' -o -name '.upload-queue' \) -print -quit 2>/dev/null || true)"
        if [ -n "$hit" ]; then
            die "Recordings were found inside $target ($hit). Removing it would delete them. Move them out and run this again. Nothing was changed."
        fi
    done <<EOF
$TARGETS
EOF
}

app_running() { [ -n "$(pgrep -f "$1/Contents/MacOS/" 2>/dev/null || true)" ]; }

# Quit a running copy of this app gracefully, then forcefully if it does not go (never another process).
stop_app() {
    local app="$1" i=0
    app_running "$app" || return 0
    info "Closing the running Meeting Notes..."
    osascript -e 'quit app "Meeting Notes"' >/dev/null 2>&1 || true
    while [ "$i" -lt 10 ]; do
        app_running "$app" || return 0
        sleep 1
        i=$((i + 1))
    done
    pkill -f "$app/Contents/MacOS/" >/dev/null 2>&1 || true
    sleep 1
}

remove_path() {
    if rm -rf -- "$1" 2>/dev/null; then
        info "Removed $1"
    else
        warn "Could not remove $1. Drag it to the Trash yourself."
    fi
}

main() {
    local arg app id rec default_rec leftover removed_any=0
    for arg in "$@"; do
        case "$arg" in
            --keep-settings) KEEP_SETTINGS=1 ;;
            -h|--help) usage; exit 0 ;;
            *) usage >&2; die "Unknown option: $arg" ;;
        esac
    done

    [ "$(uname -s)" = "Darwin" ] || die "This uninstaller is for macOS only."
    [ "$(id -u)" -ne 0 ] || die "Run this as your normal user, not with sudo."
    [ -n "${HOME:-}" ] && [ "$HOME" != "/" ] || die "HOME is not set to a user folder."

    step "Checking what to remove"
    for app in "$USER_APP" "$SYSTEM_APP"; do
        [ -d "$app" ] || continue
        id="$(bundle_id_of "$app")"
        if [ "$id" = "$BUNDLE_ID" ]; then
            add_target "$app"
        else
            warn "$app is not Meeting Notes (bundle id '${id:-unknown}'); leaving it alone."
        fi
    done
    for leftover in "$USER_APP".old-* "$USER_APP".new-*; do
        if [ -e "$leftover" ]; then add_target "$leftover"; fi
    done
    if [ "$KEEP_SETTINGS" -eq 0 ] && [ -e "$CONFIG_DIR" ]; then add_target "$CONFIG_DIR"; fi

    rec="$(recordings_dir)"
    default_rec="$HOME/Meeting Notes"
    guard_recordings "$rec"
    if [ "$default_rec" != "$rec" ]; then guard_recordings "$default_rec"; fi
    guard_stray_recordings

    step "Removing the app"
    for app in "$USER_APP" "$SYSTEM_APP"; do
        case "$TARGETS" in *"$app
"*) ;; *) continue ;; esac
        stop_app "$app"
        remove_path "$app"
        removed_any=1
    done
    for leftover in "$USER_APP".old-* "$USER_APP".new-*; do
        if [ -e "$leftover" ]; then remove_path "$leftover"; removed_any=1; fi
    done
    if [ "$removed_any" -eq 0 ]; then info "The app was not installed (nothing to remove)."; fi

    step "Settings and logs"
    if [ "$KEEP_SETTINGS" -eq 1 ]; then
        info "Kept $CONFIG_DIR (--keep-settings)."
    elif [ -e "$CONFIG_DIR" ]; then
        remove_path "$CONFIG_DIR"
    else
        info "No settings folder found."
    fi

    step "macOS privacy permissions"
    if command -v tccutil >/dev/null 2>&1; then
        tccutil reset Microphone "$BUNDLE_ID" >/dev/null 2>&1 || true
        tccutil reset ScreenCapture "$BUNDLE_ID" >/dev/null 2>&1 || true
        info "Reset the Microphone and Screen Recording entries for $BUNDLE_ID."
    fi
    info "Any leftover Meeting Notes rows can be removed in System Settings > Privacy & Security."

    step "Recordings"
    if [ -d "$rec" ]; then info "Your recordings were not touched: $rec"; fi
    if [ "$default_rec" != "$rec" ] && [ -d "$default_rec" ]; then info "Your recordings were not touched: $default_rec"; fi

    printf '\n'
    info "Meeting Notes was uninstalled for this Mac user."
}

main "$@"
"""


def render_mac_uninstaller() -> str:
    """The uninstall script. It holds no server address and no token."""
    return (
        _UNINSTALL_TEMPLATE.replace("\r\n", "\n")
        .replace("__APP_NAME__", APP_NAME)
        .replace("__BUNDLE_ID__", BUNDLE_ID)
    )
