#!/usr/bin/env bash

# Shared paths and project-scoped Docker helpers. Source this file; do not run it.

set -euo pipefail

RK_JELLYFIN_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RK_JELLYFIN_DIR="$(cd "${RK_JELLYFIN_LIB_DIR}/.." && pwd)"
RK_REPO_ROOT="$(cd "${RK_JELLYFIN_DIR}/../.." && pwd)"
RK_COMPOSE_FILE="${RK_JELLYFIN_DIR}/docker-compose.yml"
RK_STATE_DIR="${RK_JELLYFIN_DIR}/.state"
RK_ARTIFACT_DIR="${RK_JELLYFIN_DIR}/artifacts"

RK_JF10_PORT="${RK_JF10_PORT:-18116}"
RK_JF12_PORT="${RK_JF12_PORT:-18117}"
export RK_JF10_PORT RK_JF12_PORT
RK_JF10_ORIGIN="http://127.0.0.1:${RK_JF10_PORT}"
RK_JF12_ORIGIN="http://127.0.0.1:${RK_JF12_PORT}"

RK_LAB_USER="${RK_LAB_USER:-rk_admin}"
RK_LAB_PASSWORD="${RK_LAB_PASSWORD:-Test669Pw!x}"
RK_PLUGIN_GUID="515255fe-3332-49b0-b471-0be58c8221d8"
RK_BUILD_SNAPSHOT="${RK_BUILD_SNAPSHOT:-}"
if [ -n "${RK_BUILD_SNAPSHOT}" ]; then
    RK_STAGE_JF10="${RK_BUILD_SNAPSHOT}/stage"
    RK_STAGE_JF12="${RK_BUILD_SNAPSHOT}/stage-jf12"
else
    RK_STAGE_JF10="${RK_REPO_ROOT}/plugin/build/stage"
    RK_STAGE_JF12="${RK_REPO_ROOT}/plugin/build/stage-jf12"
fi

RK_JF10_DIGEST="aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db"
RK_JF12_DIGEST="db1df1d111c27ba1f10bb8fce6630892f66eb66b12c2b24e79011453ac18b3db"

RK_PROJECT="${RK_JELLYFIN_PROJECT:-rk-jellyfin-$(id -u)}"
case "${RK_PROJECT}" in
    *[!a-z0-9_-]*|'')
        echo "FATAL: RK_JELLYFIN_PROJECT must contain only lowercase a-z, digits, _ or -." >&2
        exit 2
        ;;
esac
case "${RK_PROJECT}" in
    rk-jellyfin-[a-z0-9]*) ;;
    *)
        echo "FATAL: RK_JELLYFIN_PROJECT must begin with rk-jellyfin- and have a nonempty alphanumeric suffix." >&2
        exit 2
        ;;
esac

rk_log() { printf '==> %s\n' "$*"; }
rk_die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

rk_require() {
    local tool
    for tool in "$@"; do
        command -v "${tool}" >/dev/null 2>&1 || rk_die "required command not found: ${tool}"
    done
}

rk_pin_build_snapshot() {
    local requested resolved
    requested="${RK_BUILD_SNAPSHOT:-${RK_REPO_ROOT}/plugin/build}"
    resolved="$(readlink -f -- "${requested}" 2>/dev/null || true)"
    [ -n "${resolved}" ] && [ -d "${resolved}" ] || \
        rk_die "plugin build snapshot does not exist: ${requested}"
    case "${resolved}" in
        "${RK_REPO_ROOT}/plugin/.builds/"*) ;;
        *) rk_die "plugin build resolved outside plugin/.builds: ${resolved}" ;;
    esac
    python3 "${RK_REPO_ROOT}/scripts/verify-package.py" \
        --build-dir "${resolved}" \
        --manifest "${RK_REPO_ROOT}/manifest.json" \
        --manifest-mode structure \
        --require-immutable-snapshot
    RK_BUILD_SNAPSHOT="${resolved}"
    RK_STAGE_JF10="${resolved}/stage"
    RK_STAGE_JF12="${resolved}/stage-jf12"
    export RK_BUILD_SNAPSHOT
    rk_log "pinned plugin snapshot $(basename "${resolved}") for the full lab run"
}

rk_compose() {
    docker compose --project-name "${RK_PROJECT}" -f "${RK_COMPOSE_FILE}" "$@"
}

rk_origin() {
    case "${1:-}" in
        jf10) printf '%s\n' "${RK_JF10_ORIGIN}" ;;
        jf12) printf '%s\n' "${RK_JF12_ORIGIN}" ;;
        *) rk_die "unknown Jellyfin target: ${1:-<empty>}" ;;
    esac
}

rk_expected_digest() {
    case "${1:-}" in
        jf10) printf '%s\n' "${RK_JF10_DIGEST}" ;;
        jf12) printf '%s\n' "${RK_JF12_DIGEST}" ;;
        *) rk_die "unknown Jellyfin target: ${1:-<empty>}" ;;
    esac
}

rk_expected_version_pattern() {
    case "${1:-}" in
        jf10) printf '%s\n' '^10\.11\.11([.-]|$)' ;;
        jf12) printf '%s\n' '^12\.0([.-]|$)' ;;
        *) rk_die "unknown Jellyfin target: ${1:-<empty>}" ;;
    esac
}

rk_stage() {
    case "${1:-}" in
        jf10) printf '%s\n' "${RK_STAGE_JF10}" ;;
        jf12) printf '%s\n' "${RK_STAGE_JF12}" ;;
        *) rk_die "unknown Jellyfin stage: ${1:-<empty>}" ;;
    esac
}

rk_token_file() {
    case "${1:-}" in
        jf10|jf12) printf '%s/%s.token\n' "${RK_STATE_DIR}" "$1" ;;
        *) rk_die "unknown Jellyfin target: ${1:-<empty>}" ;;
    esac
}

rk_container_id() {
    local target="$1" id
    id="$(rk_compose ps -q "${target}")"
    [ -n "${id}" ] || rk_die "${target} container is not running in project ${RK_PROJECT}"
    printf '%s\n' "${id}"
}

rk_assert_container_image() {
    local target="$1" id configured expected
    id="$(rk_container_id "${target}")"
    configured="$(docker inspect --format '{{.Config.Image}}' "${id}")"
    expected="$(rk_expected_digest "${target}")"
    case "${configured}" in
        *"sha256:${expected}"*) ;;
        *) rk_die "${target} is not running the pinned digest ${expected}: ${configured}" ;;
    esac
}

rk_http_code() {
    local url="$1"
    curl --silent --show-error --connect-timeout 2 --max-time 5 \
        --output /dev/null --write-out '%{http_code}' "${url}" 2>/dev/null || true
}

rk_wait_http() {
    local url="$1" expected="${2:-200}" attempts="${3:-120}" delay="${4:-2}" code='' i
    for ((i = 1; i <= attempts; i++)); do
        code="$(rk_http_code "${url}")"
        if [ "${code}" = "${expected}" ]; then
            return 0
        fi
        sleep "${delay}"
    done
    printf 'Timed out waiting for %s (wanted %s, last status %s)\n' \
        "${url}" "${expected}" "${code:-none}" >&2
    return 1
}

rk_auth_header() {
    local token="$1"
    printf 'Authorization: MediaBrowser Client="RefreshKit E2E", Device="Disposable Lab", DeviceId="rk-%s", Version="1", Token="%s"\n' \
        "${RK_PROJECT}" "${token}"
}

rk_no_token_auth_header() {
    printf 'Authorization: MediaBrowser Client="RefreshKit E2E", Device="Disposable Lab", DeviceId="rk-%s", Version="1"\n' \
        "${RK_PROJECT}"
}

rk_reset_service_storage() {
    local target="$1" volume_name volume
    case "${target}" in jf10|jf12) ;; *) rk_die "unknown target ${target}" ;; esac

    rk_compose stop "${target}" >/dev/null 2>&1 || true
    rk_compose rm --force --stop "${target}" >/dev/null 2>&1 || true
    for volume_name in "${target}-config" "${target}-cache"; do
        while IFS= read -r volume; do
            [ -n "${volume}" ] || continue
            docker volume rm "${volume}" >/dev/null
        done < <(docker volume ls --quiet \
            --filter "label=com.docker.compose.project=${RK_PROJECT}" \
            --filter "label=com.docker.compose.volume=${volume_name}")
    done
}

mkdir -p "${RK_STATE_DIR}" "${RK_ARTIFACT_DIR}"
