#!/usr/bin/env bash
#
# Reverse-proxy / CDN validation suite for the Jellyfin Refresh Kit.
#
#   ./run.sh up          bring the rig up and provision it
#   ./run.sh matrix      curl freshness matrix through every proxy
#   ./run.sh ws          websocket regression check through every proxy
#   ./run.sh e2e [port]  puppeteer end-to-end (login -> bump -> one reload)
#   ./run.sh cache       the misconfigured-cache demo + both remedies
#   ./run.sh subpath     set BaseUrl=/jellyfin, test :8125, restore
#   ./run.sh all         up + matrix + ws + cache + e2e + subpath
#   ./run.sh down        destroy every container, volume and network
#
# See README.md. Everything is throwaway; nothing here touches a pre-existing
# Jellyfin container.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

export NODE_PATH="${NODE_PATH:-$HOME/.nvm/versions/node/v22.20.0/lib/node_modules}"
CONTAINER="${RK_CONTAINER:-rk-jf}"
ORIGIN=8116
TOKEN_FILE="$HERE/.rk-token"

# label:port, in the order they are reported.
SETUPS=(
    "DIRECT (no proxy):$ORIGIN"
    "nginx OFFICIAL docs config:8117"
    "nginx Proxy Manager style:8118"
    "Caddy:8119"
    "Traefik:8120"
    "HAProxy:8121"
    "nginx proxy_cache NAIVE (ignores Cache-Control):8122"
    "nginx proxy_cache RESPECTING Cache-Control:8124"
    "nginx proxy_cache remedy 1 (no ignore_headers):8126"
    "nginx proxy_cache remedy 1+2 (+ exemption):8127"
)

compose() { docker compose -f "$HERE/docker-compose.yml" "$@"; }

cmd_up() {
    compose up -d
    bash "$HERE/lib/provision.sh"
}

# Mechanism 1 (the revalidating shell) can only be measured when no OUTER
# middleware replaces the shell's response headers. Jellyfin Enhanced's
# injection middleware does exactly that — see "the ordering caveat" in
# plugin/README.md — so the ETag legs are run with the third-party injectors
# parked, and the injectors are put back afterwards. Everything else in this
# suite runs with them installed.
park_injectors() {
    docker exec "$CONTAINER" sh -c '
        mkdir -p /config/plugins-parked
        for d in /config/plugins/*/; do
            case "$d" in
                */configurations/|*"Jellyfin Refresh Kit"*) continue ;;
            esac
            mv "$d" /config/plugins-parked/ 2>/dev/null || true
        done' >/dev/null
    restart_origin
}

restore_injectors() {
    docker exec "$CONTAINER" sh -c '
        [ -d /config/plugins-parked ] || exit 0
        for d in /config/plugins-parked/*/; do
            [ -e "$d" ] || continue
            mv "$d" /config/plugins/ 2>/dev/null || true
        done
        rmdir /config/plugins-parked 2>/dev/null || true' >/dev/null
    restart_origin
}

restart_origin() {
    docker restart "$CONTAINER" >/dev/null
    for _ in $(seq 1 120); do
        [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$ORIGIN/RefreshKit/Generation")" = 200 ] && break
        sleep 2
    done
    sleep 3
}

cmd_matrix() {
    local rc=0
    echo "==> parking third-party injectors (they replace the shell's headers)"
    park_injectors
    for s in "${SETUPS[@]}"; do
        bash "$HERE/lib/matrix.sh" "${s%:*}" "${s##*:}" || rc=1
    done
    echo "==> restoring third-party injectors"
    restore_injectors
    return $rc
}

cmd_ws() {
    for s in "${SETUPS[@]}"; do
        node "$HERE/lib/ws.js" "${s##*:}" "$TOKEN_FILE" || true
    done
}

cmd_e2e() {
    if [ $# -gt 0 ]; then
        node "$HERE/lib/e2e.js" "$@"
        return
    fi
    # Strictly sequential: a generation bump is server-wide, so two concurrent
    # runs would each see the other's reload and "exactly one reload" would be
    # meaningless.
    for s in "${SETUPS[@]}"; do
        node "$HERE/lib/e2e.js" "${s##*:}" || true
        echo
    done
}

cmd_cache() { bash "$HERE/lib/cache-adversarial.sh"; }

cmd_subpath() {
    local tok base auth
    tok="$(cat "$TOKEN_FILE")"
    auth="Authorization: MediaBrowser Client=\"t\", Device=\"t\", DeviceId=\"t\", Version=\"1\", Token=\"$tok\""
    base="${1:-/jellyfin}"

    echo "==> setting BaseUrl=$base (network configuration, NOT system configuration)"
    curl -s -H "$auth" "http://127.0.0.1:$ORIGIN/System/Configuration/network" \
        | python3 -c "import json,sys;d=json.load(sys.stdin);d['BaseUrl']='$base';print(json.dumps(d))" > /tmp/rk-net.json
    curl -s -X POST -H "$auth" -H 'Content-Type: application/json' \
        --data-binary @/tmp/rk-net.json -o /dev/null "http://127.0.0.1:$ORIGIN/System/Configuration/network"
    docker restart "$CONTAINER" >/dev/null
    for _ in $(seq 1 90); do
        [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$ORIGIN$base/RefreshKit/Generation")" = 200 ] && break
        sleep 2
    done

    park_injectors
    bash "$HERE/lib/matrix.sh" "nginx SUBPATH $base" 8125 "$base" || true
    restore_injectors
    node "$HERE/lib/ws.js" 8125 "$TOKEN_FILE" "$base" || true
    node "$HERE/lib/e2e.js" 8125 "$base" || true

    echo "==> restoring BaseUrl=''"
    curl -s -H "$auth" "http://127.0.0.1:$ORIGIN$base/System/Configuration/network" \
        | python3 -c "import json,sys;d=json.load(sys.stdin);d['BaseUrl']='';print(json.dumps(d))" > /tmp/rk-net.json
    curl -s -X POST -H "$auth" -H 'Content-Type: application/json' \
        --data-binary @/tmp/rk-net.json -o /dev/null "http://127.0.0.1:$ORIGIN$base/System/Configuration/network"
    docker restart "$CONTAINER" >/dev/null
}

cmd_down() {
    compose down -v --remove-orphans
    rm -f "$TOKEN_FILE"
    echo "==> rig destroyed"
}

case "${1:-all}" in
    up)      cmd_up ;;
    matrix)  cmd_matrix ;;
    ws)      cmd_ws ;;
    e2e)     shift; cmd_e2e "$@" ;;
    cache)   cmd_cache ;;
    subpath) shift; cmd_subpath "$@" ;;
    down)    cmd_down ;;
    all)     cmd_up; cmd_matrix || true; cmd_ws; cmd_cache; cmd_e2e; cmd_subpath ;;
    *)       sed -n '3,20p' "$0"; exit 2 ;;
esac
