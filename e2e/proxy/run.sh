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
# Resolved from this script's directory at runtime.
# shellcheck disable=SC1091
source "$HERE/lib/build-snapshot.sh"
# shellcheck source=lib/je-fixture.sh
# Resolved from this script's directory at runtime.
# shellcheck disable=SC1091
source "$HERE/lib/je-fixture.sh"

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
NETWORK_ORIGINAL_JSON=''
ROLLBACK_INJECTORS=0
ROLLBACK_BASEURL=0
ROLLBACK_BASE=''
ROLLBACK_AUTH=''
ROLLBACK_RUNNING=0
cleanup_temporary_files() {
    if [ -n "$NETWORK_JSON" ]; then
        rm -f -- "$NETWORK_JSON"
    fi
    if [ -n "$NETWORK_ORIGINAL_JSON" ]; then
        rm -f -- "$NETWORK_ORIGINAL_JSON"
    fi
}

handle_signal() { # signal exit-code
    echo "FATAL: received $1; rolling back active proxy-suite state" >&2
    exit "$2"
}

handle_exit() {
    local rc=$? rollback_rc=0 teardown_rc=0
    trap - EXIT
    # Once cleanup begins, a repeated terminal signal must not interrupt it.
    trap '' INT TERM
    set +e
    rollback_active_state || rollback_rc=1
    if [ "$rollback_rc" -ne 0 ]; then
        echo "FATAL: rollback is unresolved; destroying the throwaway Compose project" >&2
        if compose down -v --remove-orphans; then
            rm -f -- "$TOKEN_FILE"
            ROLLBACK_INJECTORS=0
            ROLLBACK_BASEURL=0
            echo "==> unrecoverable fixture state removed with project $PROJECT" >&2
        else
            teardown_rc=1
            echo "FATAL: could not remove project $PROJECT; run ./run.sh down once Docker is available" >&2
        fi
    fi
    cleanup_temporary_files
    if [ "$rollback_rc" -ne 0 ]; then
        if [ "$teardown_rc" -eq 0 ]; then
            echo "FATAL: automatic rollback failed closed by removing the fixture" >&2
        else
            echo "FATAL: automatic proxy-suite rollback and fixture removal did not complete" >&2
        fi
        if [ "$rc" -eq 0 ]; then
            rc=1
        fi
    fi
    exit "$rc"
}

trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM
trap handle_exit EXIT

# Active nginx proxy_cache suppresses the original client's conditional request
# fields before proxying. Keep those freshness controls out of the strict
# validator contract while still requiring their cache and browser freshness
# legs to pass. The naive cache remains a deliberately broken positive control.
STRONG_VALIDATOR_SETUPS=(
    "DIRECT (no proxy):$ORIGIN"
    "nginx OFFICIAL docs config:$NGINX_OFFICIAL"
    "nginx Proxy Manager style:$NGINX_NPM"
    "Caddy:$CADDY"
    "Traefik:$TRAEFIK"
    "HAProxy:$HAPROXY"
    "nginx proxy_cache remedy 1+2 (+ exemption):$CACHE_FIX2"
)
CACHE_FRESHNESS_CONTROLS=(
    "nginx proxy_cache RESPECTING Cache-Control:$CACHE_RESPECT"
    "nginx proxy_cache remedy 1 (honours origin freshness):$CACHE_FIX1"
)
FRESH_SETUPS=("${STRONG_VALIDATOR_SETUPS[@]}" "${CACHE_FRESHNESS_CONTROLS[@]}")
ADVERSARIAL_SETUPS=(
    "nginx proxy_cache NAIVE (ignores Cache-Control):$CACHE_NAIVE"
)
ALL_SETUPS=("${FRESH_SETUPS[@]}" "${ADVERSARIAL_SETUPS[@]}")

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
    rk_proxy_load_je_fixture "$REPO"
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
    local base="${1:-}"
    docker exec -i "$(origin_container)" sh -s -- park /config \
        < "$HERE/lib/injector-state.sh" >/dev/null || return 1
    restart_origin "$base"
}

restore_injectors() {
    local base="${1:-}"
    docker exec -i "$(origin_container)" sh -s -- restore /config \
        < "$HERE/lib/injector-state.sh" >/dev/null || return 1
    restart_origin "$base"
}

restore_injectors_scoped() {
    local base="${1:-}"
    if restore_injectors "$base"; then
        ROLLBACK_INJECTORS=0
        return 0
    fi
    return 1
}

restart_origin() {
    local base="${1:-}" code='' url
    case "$base" in
        ''|/*) ;;
        *) echo "FATAL: origin readiness prefix must be empty or start with '/': $base" >&2; return 2 ;;
    esac
    url="http://127.0.0.1:$ORIGIN$base/RefreshKit/Generation"
    docker restart "$(origin_container)" >/dev/null || return 1
    for _ in $(seq 1 120); do
        code="$(curl -s -m 3 -o /dev/null -w '%{http_code}' "$url" || true)"
        if [ "$code" = 200 ]; then
            sleep 3
            return 0
        fi
        sleep 2
    done
    echo "FATAL: origin did not become ready at $url (last HTTP status: ${code:-<none>})" >&2
    return 1
}

restore_base_url() {
    local prefix posted=0
    if [ "$ROLLBACK_BASEURL" -eq 0 ]; then
        return 0
    fi
    if [ -z "$ROLLBACK_AUTH" ] || [ -z "$NETWORK_ORIGINAL_JSON" ] \
        || [ ! -s "$NETWORK_ORIGINAL_JSON" ]; then
        echo "FATAL: BaseUrl rollback was armed without its original configuration" >&2
        return 1
    fi

    echo "==> restoring the original BaseUrl"
    # A BaseUrl change can take effect before or after the pending restart.
    # Try the configured route first, then the root route, using the exact
    # pre-mutation network document captured before the POST.
    for prefix in "$ROLLBACK_BASE" ''; do
        if curl --fail --show-error --silent -X POST -H "$ROLLBACK_AUTH" \
            -H 'Content-Type: application/json' --data-binary @"$NETWORK_ORIGINAL_JSON" \
            -o /dev/null \
            "http://127.0.0.1:$ORIGIN$prefix/System/Configuration/network"; then
            posted=1
            break
        fi
    done
    if [ "$posted" -ne 1 ]; then
        echo "FATAL: could not POST the original BaseUrl through either active route" >&2
        return 1
    fi
    if ! restart_origin; then
        echo "FATAL: original BaseUrl was posted but root readiness was not restored" >&2
        return 1
    fi
    ROLLBACK_BASEURL=0
    ROLLBACK_BASE=''
    ROLLBACK_AUTH=''
}

rollback_active_state() {
    if [ "$ROLLBACK_RUNNING" -ne 0 ]; then
        echo "FATAL: recursive proxy-suite rollback refused" >&2
        return 1
    fi
    ROLLBACK_RUNNING=1

    # Restore injectors while the configured BaseUrl is still active. If that
    # fails, restore BaseUrl and make one more injector attempt at the root.
    if [ "$ROLLBACK_INJECTORS" -ne 0 ]; then
        echo "==> restoring parked injectors"
        restore_injectors_scoped "$ROLLBACK_BASE" || true
    fi
    if [ "$ROLLBACK_BASEURL" -ne 0 ]; then
        restore_base_url || true
    fi
    if [ "$ROLLBACK_INJECTORS" -ne 0 ] && [ "$ROLLBACK_BASEURL" -eq 0 ]; then
        echo "==> retrying parked-injector restoration at the root BaseUrl"
        restore_injectors_scoped '' || true
    fi

    ROLLBACK_RUNNING=0
    if [ "$ROLLBACK_INJECTORS" -ne 0 ] || [ "$ROLLBACK_BASEURL" -ne 0 ]; then
        echo "FATAL: unresolved rollback state (injectors=$ROLLBACK_INJECTORS, BaseUrl=$ROLLBACK_BASEURL)" >&2
        return 1
    fi
    return 0
}

cmd_matrix() {
    local rc=0
    echo "==> parking third-party injectors (they replace the shell's headers)"
    ROLLBACK_BASE=''
    ROLLBACK_INJECTORS=1
    if ! park_injectors; then
        echo "FATAL: could not park injectors and restore origin readiness; attempting rollback" >&2
        restore_injectors_scoped '' || echo "FATAL: injector rollback also failed" >&2
        return 1
    fi
    for s in "${STRONG_VALIDATOR_SETUPS[@]}"; do
        bash "$HERE/lib/matrix.sh" "${s%:*}" "${s##*:}" '' strict || rc=1
    done
    for s in "${CACHE_FRESHNESS_CONTROLS[@]}"; do
        bash "$HERE/lib/matrix.sh" "${s%:*}" "${s##*:}" '' nginx-cache-suppresses-conditionals || rc=1
    done
    echo "==> restoring third-party injectors"
    restore_injectors_scoped '' || rc=1
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
    rk_proxy_load_je_fixture "$REPO"
    if [ $# -gt 0 ]; then
        RK_CONTAINER="$(origin_container)" node "$HERE/lib/e2e.js" "$@"
        return
    fi
    local rc=0
    # Strictly sequential: a generation bump is server-wide, so two concurrent
    # runs would each see the other's reload and "exactly one reload" would be
    # meaningless.
    for s in "${FRESH_SETUPS[@]}"; do
        RK_CONTAINER="$(origin_container)" node "$HERE/lib/e2e.js" "${s##*:}" || rc=1
        echo
    done
    return "$rc"
}

cmd_cache() {
    rk_proxy_load_je_fixture "$REPO"
    RK_CONTAINER="$(origin_container)" \
        RK_PROXY_ORIGIN_PORT="$ORIGIN" \
        RK_PROXY_CACHE_NAIVE_PORT="$CACHE_NAIVE" \
        RK_PROXY_CACHE_RESPECT_PORT="$CACHE_RESPECT" \
        RK_PROXY_CACHE_FIX1_PORT="$CACHE_FIX1" \
        RK_PROXY_CACHE_FIX2_PORT="$CACHE_FIX2" \
        bash "$HERE/lib/cache-adversarial.sh"
}

cmd_subpath() {
    local tok base auth rc=0 probes_unblocked=0 subpath_ready=0
    rk_proxy_load_je_fixture "$REPO"
    tok="$(cat "$TOKEN_FILE")"
    auth="Authorization: MediaBrowser Client=\"t\", Device=\"t\", DeviceId=\"t\", Version=\"1\", Token=\"$tok\""
    base="${1:-/jellyfin}"
    case "$base" in
        /*) ;;
        *) echo "FATAL: subpath BaseUrl must start with '/': $base" >&2; return 2 ;;
    esac
    NETWORK_ORIGINAL_JSON="$(mktemp "$STATE_DIR/$PROJECT-network-original.XXXXXX.json")"
    NETWORK_JSON="$(mktemp "$STATE_DIR/$PROJECT-network-subpath.XXXXXX.json")"

    echo "==> setting BaseUrl=$base (network configuration, NOT system configuration)"
    if ! curl --fail --show-error --silent -H "$auth" \
        "http://127.0.0.1:$ORIGIN/System/Configuration/network" \
        -o "$NETWORK_ORIGINAL_JSON"; then
        echo "FATAL: could not read the original network configuration" >&2
        return 1
    fi
    if ! python3 - "$base" "$NETWORK_ORIGINAL_JSON" "$NETWORK_JSON" <<'PY'
import json
import sys

base, source, destination = sys.argv[1:]
with open(source, encoding="utf-8") as handle:
    document = json.load(handle)
if document.get("BaseUrl") != "":
    raise SystemExit("FATAL: proxy fixture BaseUrl was not empty before the subpath test")
document["BaseUrl"] = base
with open(destination, "w", encoding="utf-8") as handle:
    json.dump(document, handle, separators=(",", ":"))
PY
    then
        echo "FATAL: could not prepare the subpath network configuration" >&2
        return 1
    fi
    # Arm rollback before the mutating request. A transport failure can occur
    # after Jellyfin accepted the POST, so even an unsuccessful curl must cause
    # the original document to be replayed on EXIT/INT/TERM.
    ROLLBACK_BASE="$base"
    ROLLBACK_AUTH="$auth"
    ROLLBACK_BASEURL=1
    if ! curl --fail --show-error --silent -X POST -H "$auth" -H 'Content-Type: application/json' \
        --data-binary @"$NETWORK_JSON" -o /dev/null \
        "http://127.0.0.1:$ORIGIN/System/Configuration/network"; then
        echo "FATAL: could not set BaseUrl=$base" >&2
        return 1
    fi

    ROLLBACK_INJECTORS=1
    if restart_origin "$base" && park_injectors "$base"; then
        probes_unblocked=1
        bash "$HERE/lib/matrix.sh" "nginx SUBPATH $base" "$SUBPATH" "$base" strict || rc=1
    else
        echo "FATAL: subpath origin was not ready; skipping dependent matrix/browser probes" >&2
        rc=1
    fi

    # Always attempt this rollback: park_injectors may have moved directories
    # before a restart/readiness failure, and restoring is harmless when it did not.
    if restore_injectors_scoped "$base"; then
        if [ "$probes_unblocked" = 1 ]; then
            subpath_ready=1
        fi
    else
        echo "FATAL: could not restore injectors at the configured subpath" >&2
        rc=1
    fi
    if [ "$subpath_ready" = 1 ]; then
        node "$HERE/lib/ws.js" "$SUBPATH" "$TOKEN_FILE" "$base" || rc=1
        RK_CONTAINER="$(origin_container)" \
            node "$HERE/lib/e2e.js" "$SUBPATH" "$base" || rc=1
    fi

    if ! rollback_active_state; then
        echo "FATAL: could not restore the subpath fixture state" >&2
        rc=1
    fi
    return "$rc"
}

cmd_all() {
    local rc=0
    cmd_up
    if ! cmd_matrix; then
        rc=1
        # Later legs require the ordinary injector state. Let the EXIT trap
        # retry rollback immediately rather than running invalid follow-ups.
        if [ "$ROLLBACK_INJECTORS" -ne 0 ]; then
            return 1
        fi
    fi
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
