#!/bin/bash
# One-time setup of a stable code-signing identity for Meeting Notes.app on the build Mac.
#
#     tools/macos_signing_setup.sh
#
# Why: an ad-hoc signature is different on every build, so macOS can forget the Microphone /
# Screen & System Audio Recording grants after a self-update. Signing every build with the
# same identity keeps them. tools/build_macos.sh uses this identity automatically when it
# exists (and falls back to ad-hoc with a warning when it does not).
#
# User-space only: no sudo, the login keychain is never touched, and the keychain search list
# is left exactly as it was. Safe to run again (it changes nothing when the identity exists).
#
# Creates $MN_SIGN_DIR (default ~/.meeting-notes-signing, mode 700) holding:
#     signing.keychain-db   a dedicated keychain with a self-signed "Meeting Notes Local Signing"
#                           code-signing identity (RSA 2048, valid 10 years)
#     keychain-password     its random password (mode 600)
# NEVER delete that directory (it is deliberately outside ~/meeting-notes-build, which is
# deleted after each build). Losing it means a new identity and one more permission re-grant.
set -euo pipefail

SIGN_DIR="${MN_SIGN_DIR:-$HOME/.meeting-notes-signing}"
IDENTITY="Meeting Notes Local Signing"
KC="$SIGN_DIR/signing.keychain-db"
PW_FILE="$SIGN_DIR/keychain-password"

[ "$(uname -s)" = "Darwin" ] || { echo "This script must run on macOS." >&2; exit 1; }
[ "$(id -u)" -ne 0 ] || { echo "Do not run as root." >&2; exit 1; }
case "$SIGN_DIR" in /*) ;; *) echo "MN_SIGN_DIR must be an absolute path." >&2; exit 1 ;; esac

# security create-keychain can add the new keychain to the search list: remember the list
# and put it back afterwards so nothing outside $SIGN_DIR changes.
ORIG_KEYCHAINS=()
while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"; line="${line%\"}"; line="${line#\"}"
    if [ -n "$line" ]; then ORIG_KEYCHAINS+=("$line"); fi
done < <(security list-keychains -d user)
WORK=""
cleanup() {
    if [ "${#ORIG_KEYCHAINS[@]}" -gt 0 ]; then
        security list-keychains -d user -s "${ORIG_KEYCHAINS[@]}" >/dev/null 2>&1 || true
    fi
    if [ -n "$WORK" ]; then rm -rf "$WORK"; fi
}
trap cleanup EXIT

umask 077
mkdir -p "$SIGN_DIR"
chmod 700 "$SIGN_DIR"

if [ ! -s "$PW_FILE" ]; then
    openssl rand -hex 24 >"$PW_FILE"
fi
chmod 600 "$PW_FILE"
PW="$(cat "$PW_FILE")"

if [ ! -f "$KC" ]; then
    echo "Creating the signing keychain $KC"
    security create-keychain -p "$PW" "$KC"
fi
security set-keychain-settings "$KC"           # no auto-lock, no timeout
security unlock-keychain -p "$PW" "$KC"

find_hash() {
    security find-identity -p codesigning "$KC" | grep -F "\"$IDENTITY\"" | awk '{print $2}' | head -n 1
}

HASH="$(find_hash || true)"
if [ -z "$HASH" ]; then
    echo "Creating the self-signed code-signing identity \"$IDENTITY\""
    WORK="$(mktemp -d "${TMPDIR:-/tmp}/mn-signing.XXXXXX")"
    cat >"$WORK/openssl.cnf" <<CNF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $IDENTITY
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CNF
    # /usr/bin/openssl (LibreSSL) writes a PKCS#12 that `security import` understands.
    /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 -sha256 \
        -config "$WORK/openssl.cnf" -keyout "$WORK/key.pem" -out "$WORK/cert.pem"
    /usr/bin/openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" \
        -name "$IDENTITY" -out "$WORK/identity.p12" -passout "pass:$PW"
    security import "$WORK/identity.p12" -k "$KC" -P "$PW" -T /usr/bin/codesign
    # Let codesign use the key without a "allow access" prompt.
    security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$PW" "$KC" >/dev/null
    HASH="$(find_hash || true)"
    [ -n "$HASH" ] || { echo "The identity was imported but is not listed; see: security find-identity -p codesigning \"$KC\"" >&2; exit 1; }
else
    echo "The identity \"$IDENTITY\" already exists; nothing to do."
fi

echo
echo "Signing directory : $SIGN_DIR  (never delete it)"
echo "Identity          : $IDENTITY"
echo "SHA-1             : $HASH"
echo "tools/build_macos.sh now signs with this identity. The first update after switching from"
echo "an ad-hoc build asks for Microphone and Screen & System Audio Recording one last time."
