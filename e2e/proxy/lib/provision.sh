#!/usr/bin/env bash
#
# Brings the throwaway origin from "empty container" to "wizard done, both
# plugins installed, admin token on disk". Idempotent enough to re-run.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SUITE="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$SUITE/../.." && pwd)"

ORIGIN="${RK_ORIGIN:-http://127.0.0.1:8116}"
CONTAINER="${RK_CONTAINER:-rk-jf}"
USER_NAME="${RK_USER:-rk_admin}"
PASSWORD="${RK_PASS:-Test669Pw!x}"
TOKEN_FILE="$SUITE/.rk-token"
JSON='Content-Type: application/json'
AUTH_NO_TOKEN='Authorization: MediaBrowser Client="t", Device="t", DeviceId="t", Version="1"'

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
STAGE="$REPO/plugin/build/stage"
if [ ! -f "$STAGE/Jellyfin.Plugin.RefreshKit.dll" ]; then
    echo "==> building the standalone plugin"
    DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}" bash "$REPO/plugin/build.sh"
fi
RK_VERSION="$(grep -oP '(?<="version": ")[^"]+' "$STAGE/meta.json")"
echo "==> installing Jellyfin Refresh Kit $RK_VERSION"
docker exec "$CONTAINER" mkdir -p "/config/plugins/Jellyfin Refresh Kit_${RK_VERSION}"
docker cp "$STAGE/Jellyfin.Plugin.RefreshKit.dll" "$CONTAINER:/config/plugins/Jellyfin Refresh Kit_${RK_VERSION}/"
docker cp "$STAGE/meta.json"                      "$CONTAINER:/config/plugins/Jellyfin Refresh Kit_${RK_VERSION}/"

# ── a real web-injecting third-party plugin, so the rig is not a lab vacuum ──
JE_DIR="$SUITE/.je"
JE_VERSION="${RK_JE_VERSION:-12.1.0.0}"
if [ ! -f "$JE_DIR/Jellyfin.Plugin.JellyfinEnhanced.dll" ]; then
    echo "==> downloading Jellyfin Enhanced"
    mkdir -p "$JE_DIR"
    URL=$(curl -s https://api.github.com/repos/n00bcodr/Jellyfin-Enhanced/releases/latest \
          | grep -oE '"browser_download_url": "[^"]+\.zip"' | head -1 | cut -d'"' -f4)
    curl -sL -o "$JE_DIR/je.zip" "$URL"
    (cd "$JE_DIR" && unzip -oq je.zip)
    # The release zip ships the DLL alone; the loader needs a Name_version folder
    # AND a meta.json, so synthesise one.
    cat > "$JE_DIR/meta.json" <<EOF
{
    "category": "General",
    "guid": "f69e946a-4b3c-4e9a-8f0a-8d7c1b2c4d9b",
    "name": "Jellyfin Enhanced",
    "overview": "Jellyfin Enhanced",
    "description": "Jellyfin Enhanced",
    "owner": "n00bcodr",
    "targetAbi": "10.11.0.0",
    "framework": "net8.0",
    "version": "${JE_VERSION}",
    "changelog": "",
    "timestamp": "2026-01-01T00:00:00Z",
    "status": "Active",
    "autoUpdate": false,
    "imagePath": ""
}
EOF
fi
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
