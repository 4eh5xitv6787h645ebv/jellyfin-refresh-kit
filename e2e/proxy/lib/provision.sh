#!/usr/bin/env bash
#
# Brings the throwaway origin from "empty container" to "wizard done, both
# plugins installed, admin token on disk". Idempotent enough to re-run.
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUITE="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$SUITE/../.." && pwd)"
# shellcheck source=build-snapshot.sh
# Resolved from this script's directory at runtime.
# shellcheck disable=SC1091
source "$HERE/build-snapshot.sh"

ORIGIN="${RK_ORIGIN:-http://127.0.0.1:8116}"
CONTAINER="${RK_CONTAINER:?RK_CONTAINER must be the verified project-scoped origin id}"
USER_NAME="${RK_USER:-rk_admin}"
PASSWORD="${RK_PASS:-Test669Pw!x}"
TOKEN_FILE="${RK_TOKEN_FILE:-$SUITE/.state/rk-proxy-$(id -u).token}"
case "$TOKEN_FILE" in
    "$SUITE/.state/"*.token) ;;
    *) echo "FATAL: refusing token path outside $SUITE/.state" >&2; exit 2 ;;
esac
mkdir -p "$SUITE/.state"
JSON='Content-Type: application/json'
AUTH_NO_TOKEN='Authorization: MediaBrowser Client="t", Device="t", DeviceId="t", Version="1"'

# run.sh owns build selection. Revalidate the explicit canonical directory at
# this process boundary so invoking provision.sh directly cannot fall back to
# the mutable plugin/build symlink or copy unverified package metadata.
rk_proxy_require_pinned_build_snapshot "$REPO"
BUILD_SNAPSHOT="$RK_BUILD_SNAPSHOT"
STAGE="$BUILD_SNAPSHOT/stage"

wait_for() { # url [tries]
    local url="$1" tries="${2:-90}" code=
    for _ in $(seq 1 "$tries"); do
        code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$url" || true)
        [ "$code" = "200" ] && { echo "$code"; return 0; }
        sleep 2
    done
    echo "$code"; return 1
}

echo "==> waiting for the origin"
wait_for "$ORIGIN/System/Info/Public" >/dev/null

if curl -s "$ORIGIN/System/Info/Public" | grep -q '"StartupWizardCompleted":false'; then
    echo "==> running the startup wizard"
    curl -s -X POST "$ORIGIN/Startup/Configuration" -H "$JSON" \
        -d '{"UICulture":"en-US","MetadataCountryCode":"US","PreferredMetadataLanguage":"en"}' -o /dev/null
    curl -s "$ORIGIN/Startup/User" -o /dev/null
    curl -s -X POST "$ORIGIN/Startup/User" -H "$JSON" \
        -d "{\"Name\":\"$USER_NAME\",\"Password\":\"$PASSWORD\"}" -o /dev/null
    curl -s -X POST "$ORIGIN/Startup/RemoteAccess" -H "$JSON" \
        -d '{"EnableRemoteAccess":true,"EnableAutomaticPortMapping":false}' -o /dev/null
    curl -s -X POST "$ORIGIN/Startup/Complete" -H "$JSON" -o /dev/null
fi

# ── the plugin under test ────────────────────────────────────────────────────
RK_VERSION="$(grep -oP '(?<="version": ")[^"]+' "$STAGE/meta.json")"
echo "==> installing Jellyfin Refresh Kit $RK_VERSION"
docker exec "$CONTAINER" mkdir -p "/config/plugins/Jellyfin Refresh Kit_${RK_VERSION}"
docker cp "$STAGE/Jellyfin.Plugin.RefreshKit.dll" "$CONTAINER:/config/plugins/Jellyfin Refresh Kit_${RK_VERSION}/"
docker cp "$STAGE/meta.json"                      "$CONTAINER:/config/plugins/Jellyfin Refresh Kit_${RK_VERSION}/"

# ── a real web-injecting third-party plugin, so the rig is not a lab vacuum ──
JE_DIR="$SUITE/.je"
JE_VERSION="12.1.0.0"
JE_URL="https://github.com/n00bcodr/Jellyfin-Enhanced/releases/download/12.1.0.0/Jellyfin.Plugin.JellyfinEnhanced_10.11.0.zip"
JE_SHA256="ef27604cb7711ade70a2ea659db1528fdbda9420b98d259a4f404b135079a24b"
JE_ARCHIVE="$JE_DIR/Jellyfin.Plugin.JellyfinEnhanced_10.11.0.zip"
JE_PART="$JE_ARCHIVE.part"

mkdir -p "$JE_DIR"
archive_sha256() {
    python3 - "$1" <<'PY'
import hashlib
import pathlib
import sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
}

if [ ! -f "$JE_ARCHIVE" ] || [ "$(archive_sha256 "$JE_ARCHIVE")" != "$JE_SHA256" ]; then
    echo "==> downloading pinned Jellyfin Enhanced $JE_VERSION fixture"
    rm -f -- "$JE_PART"
    curl --fail --show-error --silent --location -o "$JE_PART" "$JE_URL"
    ACTUAL_JE_SHA256="$(archive_sha256 "$JE_PART")"
    if [ "$ACTUAL_JE_SHA256" != "$JE_SHA256" ]; then
        rm -f -- "$JE_PART"
        echo "FATAL: Jellyfin Enhanced fixture SHA-256 mismatch: expected $JE_SHA256, got $ACTUAL_JE_SHA256" >&2
        exit 1
    fi
    mv -- "$JE_PART" "$JE_ARCHIVE"
fi

ACTUAL_JE_SHA256="$(archive_sha256 "$JE_ARCHIVE")"
[ "$ACTUAL_JE_SHA256" = "$JE_SHA256" ] || {
    echo "FATAL: cached Jellyfin Enhanced fixture failed SHA-256 verification" >&2
    exit 1
}
echo "==> extracting verified Jellyfin Enhanced $JE_VERSION fixture"
unzip -oq "$JE_ARCHIVE" -d "$JE_DIR"

# The fixture zip ships the DLL alone; the loader also needs meta.json.
python3 - "$JE_DIR/meta.json" "$JE_VERSION" <<'PY'
import json
import sys

path, version = sys.argv[1:]
metadata = {
    "category": "General",
    "guid": "f69e946a-4b3c-4e9a-8f0a-8d7c1b2c4d9b",
    "name": "Jellyfin Enhanced",
    "overview": "Jellyfin Enhanced",
    "description": "Jellyfin Enhanced",
    "owner": "n00bcodr",
    "targetAbi": "10.11.0.0",
    "framework": "net8.0",
    "version": version,
    "changelog": "",
    "timestamp": "2026-01-01T00:00:00Z",
    "status": "Active",
    "autoUpdate": False,
    "imagePath": "",
}
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(metadata, handle, indent=4)
    handle.write("\n")
PY
echo "==> installing Jellyfin Enhanced $JE_VERSION"
docker exec "$CONTAINER" mkdir -p "/config/plugins/Jellyfin Enhanced_${JE_VERSION}"
docker cp "$JE_DIR/Jellyfin.Plugin.JellyfinEnhanced.dll" "$CONTAINER:/config/plugins/Jellyfin Enhanced_${JE_VERSION}/"
docker cp "$JE_DIR/meta.json"                            "$CONTAINER:/config/plugins/Jellyfin Enhanced_${JE_VERSION}/"

docker restart "$CONTAINER" >/dev/null
wait_for "$ORIGIN/RefreshKit/Generation" 120 >/dev/null
sleep 3

# ── admin token ──────────────────────────────────────────────────────────────
TOKEN=$(curl -s -X POST "$ORIGIN/Users/AuthenticateByName" -H "$JSON" -H "$AUTH_NO_TOKEN" \
        -d "{\"Username\":\"$USER_NAME\",\"Pw\":\"$PASSWORD\"}" \
        | grep -oE '"AccessToken":"[^"]+"' | cut -d: -f2 | tr -d '"')
[ -n "$TOKEN" ] || { echo "FATAL: could not authenticate as $USER_NAME" >&2; exit 1; }
printf '%s' "$TOKEN" > "$TOKEN_FILE"

# ── make the client poll fast enough for a test to finish ────────────────────
AUTH="Authorization: MediaBrowser Client=\"t\", Device=\"t\", DeviceId=\"t\", Version=\"1\", Token=\"$TOKEN\""
curl -s -X POST -H "$AUTH" -H "$JSON" \
    -d '{"EnableInjection":true,"EnableThirdPartyStamping":true,"EnableAutoReload":true,"PollSeconds":15,"IdleSeconds":0,"ReloadBudget":10,"EnableConfigWatching":true,"ConfigWatchExclusions":[],"ConfigCooldownMinutes":0,"DevMode":false}' \
    -o /dev/null "$ORIGIN/Plugins/515255fe-3332-49b0-b471-0be58c8221d8/Configuration"

echo "==> ready. generation = $(curl -s "$ORIGIN/RefreshKit/Generation.txt")"
