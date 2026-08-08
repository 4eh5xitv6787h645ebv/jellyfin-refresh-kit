#!/usr/bin/env bash
#
# Reverse-proxy / CDN validation suite for the Jellyfin Refresh Kit.
#
#   ./run.sh up          bring the rig up and provision it
#   ./run.sh matrix      curl freshness matrix through every proxy
#   ./run.sh ws          websocket regression check through every proxy
#   ./run.sh e2e [port]  puppeteer end-to-end (login -> bump -> one reload)
#   ./run.sh cache       the misconfigured-cache demo + both remedies
#   ./run.sh subpath     set BaseUrl=/jellyfin, test subpath proxy, restore
#   ./run.sh all         up + matrix + ws + cache + e2e + subpath
#   ./run.sh down        destroy every container, volume and network
#
# See README.md. Everything is throwaway; nothing here touches a pre-existing
# Jellyfin container.
set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
REPO="$(cd "$HERE/../.." && pwd -P)"
# shellcheck source=lib/build-snapshot.sh
source "$HERE/lib/build-snapshot.sh"

PROJECT="${RK_PROXY_PROJECT:-rk-proxy-$(id -u)}"
case "$PROJECT" in
    rk-proxy-[a-z0-9]*) ;;
    *) echo "FATAL: RK_PROXY_PROJECT must begin with rk-proxy- and have a lowercase alphanumeric suffix" >&2; exit 2 ;;
esac
case "$PROJECT" in
    *[!a-z0-9_-]*) echo "FATAL: RK_PROXY_PROJECT contains unsafe characters" >&2; exit 2 ;;
esac

ORIGIN="${RK_PROXY_ORIGIN_PORT:-8116}"
NGINX_OFFICIAL="${RK_PROXY_NGINX_OFFICIAL_PORT:-8117}"
NGINX_NPM="${RK_PROXY_NGINX_NPM_PORT:-8118}"
CADDY="${RK_PROXY_CADDY_PORT:-8119}"
TRAEFIK="${RK_PROXY_TRAEFIK_PORT:-8120}"
HAPROXY="${RK_PROXY_HAPROXY_PORT:-8121}"
CACHE_NAIVE="${RK_PROXY_CACHE_NAIVE_PORT:-8122}"
CACHE_RESPECT="${RK_PROXY_CACHE_RESPECT_PORT:-8124}"
SUBPATH="${RK_PROXY_SUBPATH_PORT:-8125}"
CACHE_FIX1="${RK_PROXY_CACHE_FIX1_PORT:-8126}"
CACHE_FIX2="${RK_PROXY_CACHE_FIX2_PORT:-8127}"
export RK_PROXY_ORIGIN_PORT="$ORIGIN"
export RK_PROXY_NGINX_OFFICIAL_PORT="$NGINX_OFFICIAL"
export RK_PROXY_NGINX_NPM_PORT="$NGINX_NPM"
export RK_PROXY_CADDY_PORT="$CADDY"
export RK_PROXY_TRAEFIK_PORT="$TRAEFIK"
export RK_PROXY_HAPROXY_PORT="$HAPROXY"
export RK_PROXY_CACHE_NAIVE_PORT="$CACHE_NAIVE"
export RK_PROXY_CACHE_RESPECT_PORT="$CACHE_RESPECT"
export RK_PROXY_SUBPATH_PORT="$SUBPATH"
export RK_PROXY_CACHE_FIX1_PORT="$CACHE_FIX1"
export RK_PROXY_CACHE_FIX2_PORT="$CACHE_FIX2"
for port in "$ORIGIN" "$NGINX_OFFICIAL" "$NGINX_NPM" "$CADDY" "$TRAEFIK" \
    "$HAPROXY" "$CACHE_NAIVE" "$CACHE_RESPECT" "$SUBPATH" "$CACHE_FIX1" "$CACHE_FIX2"; do
    case "$port" in *[!0-9]*|'') echo "FATAL: proxy ports must be numeric" >&2; exit 2 ;; esac
done

STATE_DIR="$HERE/.state"
TOKEN_FILE="$STATE_DIR/$PROJECT.token"
mkdir -p "$STATE_DIR"
NETWORK_JSON=''
cleanup_temporary_files() {
    if [ -n "$NETWORK_JSON" ]; then
        rm -f -- "$NETWORK_JSON"
    fi
}
trap cleanup_temporary_files EXIT

# label:port, in the order they are reported. The naive cache is a deliberately
# broken control: it belongs in websocket coverage and the adversarial-cache
# assertions, but a correct matrix/E2E run MUST NOT expect it to behave like a
# healthy deployment.
HEALTHY_SETUPS=(
    "DIRECT (no proxy):$ORIGIN"
    "nginx OFFICIAL docs config:$NGINX_OFFICIAL"
    "nginx Proxy Manager style:$NGINX_NPM"
    "Caddy:$CADDY"
    "Traefik:$TRAEFIK"
    "HAProxy:$HAPROXY"
    "nginx proxy_cache RESPECTING Cache-Control:$CACHE_RESPECT"
    "nginx proxy_cache remedy 1 (no ignore_headers):$CACHE_FIX1"
    "nginx proxy_cache remedy 1+2 (+ exemption):$CACHE_FIX2"
)
ADVERSARIAL_SETUPS=(
    "nginx proxy_cache NAIVE (ignores Cache-Control):$CACHE_NAIVE"
)
ALL_SETUPS=("${HEALTHY_SETUPS[@]}" "${ADVERSARIAL_SETUPS[@]}")

compose() { docker compose --project-name "$PROJECT" -f "$HERE/docker-compose.yml" "$@"; }

origin_container() {
    local container project_label
    container="$(compose ps -q rk-jf)"
    [ -n "$container" ] || { echo "FATAL: origin is not running in project $PROJECT" >&2; return 1; }
    project_label="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$container")"
    [ "$project_label" = "$PROJECT" ] || {
        echo "FATAL: refusing container outside project $PROJECT" >&2
        return 1
    }
    printf '%s\n' "$container"
}

verify_origin_image() {
    local configured
    configured="$(docker inspect --format '{{.Config.Image}}' "$(origin_container)")"
    case "$configured" in
        *sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db*) ;;
        *) echo "FATAL: origin is not the pinned Jellyfin image: $configured" >&2; return 1 ;;
    esac
}

cmd_up() {
    # Build/pin before creating any Docker resources. Provisioning receives the
    # canonical immutable directory, never the mutable plugin/build link.
    rk_proxy_pin_build_snapshot "$REPO"
    compose up -d --wait
    verify_origin_image
    RK_CONTAINER="$(origin_container)" RK_ORIGIN="http://127.0.0.1:$ORIGIN" \
        RK_TOKEN_FILE="$TOKEN_FILE" RK_SKIP_BUILD=1 \
        RK_BUILD_SNAPSHOT="$RK_BUILD_SNAPSHOT" bash "$HERE/lib/provision.sh"
}

# Mechanism 1 (the revalidating shell) can only be measured when no OUTER
# middleware replaces the shell's response headers. Jellyfin Enhanced's
# injection middleware does exactly that — see "the ordering caveat" in
# plugin/README.md — so the ETag legs are run with the third-party injectors
# parked, and the injectors are put back afterwards. Everything else in this
# suite runs with them installed.
park_injectors() {
    docker exec "$(origin_container)" sh -c '
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
    docker exec "$(origin_container)" sh -c '
        [ -d /config/plugins-parked ] || exit 0
        for d in /config/plugins-parked/*/; do
            [ -e "$d" ] || continue
            mv "$d" /config/plugins/ 2>/dev/null || true
        done
        rmdir /config/plugins-parked 2>/dev/null || true' >/dev/null
    restart_origin
}

restart_origin() {
    docker restart "$(origin_container)" >/dev/null
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
    for s in "${HEALTHY_SETUPS[@]}"; do
        bash "$HERE/lib/matrix.sh" "${s%:*}" "${s##*:}" || rc=1
    done
    echo "==> restoring third-party injectors"
    restore_injectors
    return $rc
}

cmd_ws() {
    local rc=0
    for s in "${ALL_SETUPS[@]}"; do
        node "$HERE/lib/ws.js" "${s##*:}" "$TOKEN_FILE" || rc=1
    done
    return "$rc"
}

cmd_e2e() {
    if [ $# -gt 0 ]; then
        RK_CONTAINER="$(origin_container)" node "$HERE/lib/e2e.js" "$@"
        return
    fi
    local rc=0
    # Strictly sequential: a generation bump is server-wide, so two concurrent
    # runs would each see the other's reload and "exactly one reload" would be
    # meaningless.
    for s in "${HEALTHY_SETUPS[@]}"; do
        RK_CONTAINER="$(origin_container)" node "$HERE/lib/e2e.js" "${s##*:}" || rc=1
        echo
    done
    return "$rc"
}

cmd_cache() {
    RK_CONTAINER="$(origin_container)" \
        RK_PROXY_ORIGIN_PORT="$ORIGIN" \
        RK_PROXY_CACHE_NAIVE_PORT="$CACHE_NAIVE" \
        RK_PROXY_CACHE_RESPECT_PORT="$CACHE_RESPECT" \
        RK_PROXY_CACHE_FIX1_PORT="$CACHE_FIX1" \
        RK_PROXY_CACHE_FIX2_PORT="$CACHE_FIX2" \
        bash "$HERE/lib/cache-adversarial.sh"
}

cmd_subpath() {
    local tok base auth rc=0
    tok="$(cat "$TOKEN_FILE")"
    auth="Authorization: MediaBrowser Client=\"t\", Device=\"t\", DeviceId=\"t\", Version=\"1\", Token=\"$tok\""
    base="${1:-/jellyfin}"
    NETWORK_JSON="$(mktemp "$STATE_DIR/$PROJECT-network.XXXXXX.json")"

    echo "==> setting BaseUrl=$base (network configuration, NOT system configuration)"
    curl -s -H "$auth" "http://127.0.0.1:$ORIGIN/System/Configuration/network" \
        | python3 -c "import json,sys;d=json.load(sys.stdin);d['BaseUrl']='$base';print(json.dumps(d))" > "$NETWORK_JSON"
    curl -s -X POST -H "$auth" -H 'Content-Type: application/json' \
        --data-binary @"$NETWORK_JSON" -o /dev/null "http://127.0.0.1:$ORIGIN/System/Configuration/network"
    docker restart "$(origin_container)" >/dev/null
    for _ in $(seq 1 90); do
        [ "$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$ORIGIN$base/RefreshKit/Generation")" = 200 ] && break
        sleep 2
    done

    park_injectors
    bash "$HERE/lib/matrix.sh" "nginx SUBPATH $base" "$SUBPATH" "$base" || rc=1
    restore_injectors
    node "$HERE/lib/ws.js" "$SUBPATH" "$TOKEN_FILE" "$base" || rc=1
    RK_CONTAINER="$(origin_container)" \
        node "$HERE/lib/e2e.js" "$SUBPATH" "$base" || rc=1

    echo "==> restoring BaseUrl=''"
    curl -s -H "$auth" "http://127.0.0.1:$ORIGIN$base/System/Configuration/network" \
        | python3 -c "import json,sys;d=json.load(sys.stdin);d['BaseUrl']='';print(json.dumps(d))" > "$NETWORK_JSON"
    curl -s -X POST -H "$auth" -H 'Content-Type: application/json' \
        --data-binary @"$NETWORK_JSON" -o /dev/null "http://127.0.0.1:$ORIGIN$base/System/Configuration/network"
    docker restart "$(origin_container)" >/dev/null
    return "$rc"
}

cmd_all() {
    local rc=0
    cmd_up
    cmd_matrix || rc=1
    cmd_ws || rc=1
    cmd_cache || rc=1
    cmd_e2e || rc=1
    cmd_subpath || rc=1
    return "$rc"
}

cmd_down() {
    compose down -v --remove-orphans
    rm -f -- "$TOKEN_FILE"
    echo "==> project $PROJECT destroyed"
}

case "${1:-all}" in
    up)      cmd_up ;;
    matrix)  cmd_matrix ;;
    ws)      cmd_ws ;;
    e2e)     shift; cmd_e2e "$@" ;;
    cache)   cmd_cache ;;
    subpath) shift; cmd_subpath "$@" ;;
    down)    cmd_down ;;
    all)     cmd_all ;;
    *)       sed -n '3,20p' "$0"; exit 2 ;;
esac
