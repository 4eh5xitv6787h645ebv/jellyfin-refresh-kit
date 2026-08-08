#!/usr/bin/env bash

# Shared paths and safety checks for the isolated compatibility harness.
# Source this file; do not execute it directly.

set -euo pipefail

RK_COMPAT_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RK_COMPAT_DIR="$(cd "${RK_COMPAT_LIB_DIR}/.." && pwd)"
RK_COMPAT_REPO_ROOT="$(cd "${RK_COMPAT_DIR}/../.." && pwd)"
RK_COMPAT_LOCK="${RK_COMPAT_DIR}/ecosystem.lock.json"
RK_COMPAT_MATRICES="${RK_COMPAT_DIR}/matrices.json"
RK_COMPAT_COMPOSE_FILE="${RK_COMPAT_DIR}/docker-compose.yml"
RK_COMPAT_CACHE_DIR="${RK_COMPAT_CACHE_DIR:-${RK_COMPAT_DIR}/.cache}"
RK_COMPAT_STATE_DIR="${RK_COMPAT_STATE_DIR:-${RK_COMPAT_DIR}/.state}"
RK_COMPAT_ARTIFACT_DIR="${RK_COMPAT_ARTIFACT_DIR:-${RK_COMPAT_DIR}/artifacts}"

RK_COMPAT_JF10_PORT="${RK_COMPAT_JF10_PORT:-18216}"
RK_COMPAT_JF12_PORT="${RK_COMPAT_JF12_PORT:-18217}"
export RK_COMPAT_JF10_PORT RK_COMPAT_JF12_PORT

RK_COMPAT_USER="${RK_COMPAT_USER:-rk_compat_admin}"
RK_COMPAT_PASSWORD="${RK_COMPAT_PASSWORD:-Compat669Pw!x}"
RK_COMPAT_REFRESH_KIT_GUID="515255fe-3332-49b0-b471-0be58c8221d8"
RK_COMPAT_BUILD_SNAPSHOT="${RK_COMPAT_BUILD_SNAPSHOT:-${RK_COMPAT_REPO_ROOT}/plugin/build}"
RK_COMPAT_JF10_STAGE=""
RK_COMPAT_JF12_STAGE=""

RK_COMPAT_PROJECT="${RK_COMPAT_PROJECT:-rk-compat-$(id -u)}"
case "${RK_COMPAT_PROJECT}" in
    rk-compat-[a-z0-9]* ) ;;
    * )
        printf 'FATAL: RK_COMPAT_PROJECT must start with rk-compat- and end in lowercase alphanumerics, _ or -.\n' >&2
        exit 2
        ;;
esac
case "${RK_COMPAT_PROJECT}" in
    *[!a-z0-9_-]* )
        printf 'FATAL: RK_COMPAT_PROJECT contains an unsafe character.\n' >&2
        exit 2
        ;;
esac

compat_log() { printf '==> %s\n' "$*"; }
compat_die() { printf 'FATAL: %s\n' "$*" >&2; exit 1; }

compat_require() {
    local command_name
    for command_name in "$@"; do
        command -v "${command_name}" >/dev/null 2>&1 || \
            compat_die "required command not found: ${command_name}"
    done
}

compat_dotnet_launcher() {
    if [ -n "${RK_COMPAT_DOTNET:-}" ] && [ -x "${RK_COMPAT_DOTNET}" ]; then
        printf '%s\n' "${RK_COMPAT_DOTNET}"
    elif [ -n "${DOTNET_ROOT:-}" ] && [ -x "${DOTNET_ROOT}/dotnet" ]; then
        printf '%s\n' "${DOTNET_ROOT}/dotnet"
    elif [ -x "${HOME}/.dotnet/dotnet" ]; then
        printf '%s\n' "${HOME}/.dotnet/dotnet"
    else
        command -v dotnet 2>/dev/null || compat_die "required command not found: dotnet"
    fi
}

compat_compose() {
    docker compose --project-name "${RK_COMPAT_PROJECT}" \
        -f "${RK_COMPAT_COMPOSE_FILE}" "$@"
}

compat_origin() {
    local runtime="${1:-}" internal_ip="${2:-}" published="${3:-}"
    local port loopback
    case "${runtime}" in
        jf10) port="${RK_COMPAT_JF10_PORT}" ;;
        jf12) port="${RK_COMPAT_JF12_PORT}" ;;
        *) compat_die "unknown runtime: ${runtime:-<empty>}" ;;
    esac
    loopback="http://127.0.0.1:${port}"
    if [ "${published}" = true ]; then
        printf '%s\t%s\n' "${loopback}" published-loopback
        return
    fi
    [ -n "${internal_ip}" ] || compat_die "${runtime}: verified internal bridge IPv4 is empty"
    compat_log "${runtime}: Docker did not activate ${loopback}; using the verified project-owned internal bridge IPv4" >&2
    printf 'http://%s:8096\t%s\n' "${internal_ip}" verified-internal-bridge
}

compat_pin_build_snapshot() {
    local resolved expected_root
    resolved="$(readlink -f -- "${RK_COMPAT_BUILD_SNAPSHOT}")" || \
        compat_die "cannot resolve Refresh Kit build snapshot: ${RK_COMPAT_BUILD_SNAPSHOT}"
    expected_root="$(readlink -f -- "${RK_COMPAT_REPO_ROOT}/plugin/.builds")"
    case "${resolved}" in
        "${expected_root}"/*) ;;
        *) compat_die "Refresh Kit build must resolve inside plugin/.builds: ${resolved}" ;;
    esac
    [ -f "${resolved}/stage/meta.json" ] || \
        compat_die "net9 stage is missing from build snapshot: ${resolved}"
    [ -f "${resolved}/stage-jf12/meta.json" ] || \
        compat_die "net10 stage is missing from build snapshot: ${resolved}"
    python3 "${RK_COMPAT_REPO_ROOT}/scripts/verify-package.py" \
        --build-dir "${resolved}" \
        --manifest "${RK_COMPAT_REPO_ROOT}/manifest.json" \
        --manifest-mode structure \
        --require-immutable-snapshot >/dev/null || \
        compat_die "Refresh Kit build snapshot failed exact package verification: ${resolved}"

    # Export the immutable path once. Every matrix in an `all` run inherits the
    # same value, so a concurrent package build cannot mix validated metadata
    # from snapshot A with DLLs copied later from snapshot B.
    RK_COMPAT_BUILD_SNAPSHOT="${resolved}"
    RK_COMPAT_JF10_STAGE="${resolved}/stage"
    RK_COMPAT_JF12_STAGE="${resolved}/stage-jf12"
    export RK_COMPAT_BUILD_SNAPSHOT RK_COMPAT_JF10_STAGE RK_COMPAT_JF12_STAGE
}

compat_stage() {
    case "${1:-}" in
        jf10) printf '%s\n' "${RK_COMPAT_JF10_STAGE}" ;;
        jf12) printf '%s\n' "${RK_COMPAT_JF12_STAGE}" ;;
        *) compat_die "unknown runtime: ${1:-<empty>}" ;;
    esac
}

compat_container_details() {
    local runtime="$1" report="$2" expected_digest expected_port network_name
    local -a container_ids
    mapfile -t container_ids < <(compat_compose ps -q "${runtime}")
    [ "${#container_ids[@]}" -eq 1 ] || \
        compat_die "${runtime} has ${#container_ids[@]} containers in Compose project ${RK_COMPAT_PROJECT}"
    expected_digest="$(python3 "${RK_COMPAT_DIR}/lib/manifest.py" runtime-field "${runtime}" imageDigest)"
    case "${runtime}" in
        jf10) expected_port="${RK_COMPAT_JF10_PORT}" ;;
        jf12) expected_port="${RK_COMPAT_JF12_PORT}" ;;
        *) compat_die "unknown runtime: ${runtime}" ;;
    esac
    network_name="${RK_COMPAT_PROJECT}_compat-internal"
    python3 - "${container_ids[0]}" "${RK_COMPAT_PROJECT}" "${runtime}" \
        "${expected_digest}" "${expected_port}" "${network_name}" "${report}" <<'PY'
import ipaddress
import json
import pathlib
import subprocess
import sys

(container_id, project, service, expected_digest, expected_port,
 network_name, report_path) = sys.argv[1:]

def inspect(kind, identifier):
    result = subprocess.run(
        ["docker", kind, "inspect", identifier],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise SystemExit(f"FATAL: docker {kind} inspect returned an unexpected payload")
    return value[0]

container = inspect("container", container_id)
labels = container.get("Config", {}).get("Labels") or {}
if labels.get("com.docker.compose.project") != project:
    raise SystemExit("FATAL: compatibility container has the wrong Compose project label")
if labels.get("com.docker.compose.service") != service:
    raise SystemExit("FATAL: compatibility container has the wrong Compose service label")
configured_image = str(container.get("Config", {}).get("Image", ""))
if not configured_image.endswith(f"@sha256:{expected_digest}"):
    raise SystemExit("FATAL: compatibility container does not use the pinned image digest")
if container.get("State", {}).get("Running") is not True:
    raise SystemExit("FATAL: compatibility container is not running")

networks = container.get("NetworkSettings", {}).get("Networks") or {}
if set(networks) != {network_name}:
    raise SystemExit(f"FATAL: compatibility container networks are {sorted(networks)!r}")
attachment = networks[network_name]
if attachment.get("Gateway") not in (None, ""):
    raise SystemExit("FATAL: internal compatibility network unexpectedly has a gateway")
try:
    address = ipaddress.ip_address(str(attachment.get("IPAddress", "")))
except ValueError as error:
    raise SystemExit("FATAL: compatibility container has no valid bridge IPv4") from error
if address.version != 4 or not address.is_private:
    raise SystemExit(f"FATAL: compatibility bridge address is not private IPv4: {address}")

network = inspect("network", network_name)
network_labels = network.get("Labels") or {}
if network.get("Internal") is not True or network.get("Driver") != "bridge":
    raise SystemExit("FATAL: compatibility network is not an internal bridge")
if network_labels.get("com.docker.compose.project") != project:
    raise SystemExit("FATAL: compatibility network has the wrong Compose project label")
membership = (network.get("Containers") or {}).get(container.get("Id"))
if not isinstance(membership, dict):
    raise SystemExit("FATAL: project network does not contain the selected container")
if str(membership.get("IPv4Address", "")).split("/", 1)[0] != str(address):
    raise SystemExit("FATAL: container and network inspections disagree on bridge IPv4")

declared = (container.get("HostConfig", {}).get("PortBindings") or {}).get("8096/tcp")
expected_binding = [{"HostIp": "127.0.0.1", "HostPort": expected_port}]
if declared != expected_binding:
    raise SystemExit(f"FATAL: declared loopback binding is {declared!r}, expected {expected_binding!r}")
active = (container.get("NetworkSettings", {}).get("Ports") or {}).get("8096/tcp")
if active not in (None, expected_binding):
    raise SystemExit(f"FATAL: active port binding is unexpected: {active!r}")
published = active == expected_binding

report = {
    "schemaVersion": 1,
    "project": project,
    "service": service,
    "containerId": container.get("Id"),
    "configuredImage": configured_image,
    "expectedImageDigest": expected_digest,
    "composeLabelsValid": True,
    "network": network_name,
    "internalBridge": True,
    "noGateway": True,
    "exclusiveProjectNetwork": True,
    "internalIpv4": str(address),
    "declaredLoopback": f"127.0.0.1:{expected_port}",
    "publishedLoopbackActive": published,
    "valid": True,
}
path = pathlib.Path(report_path)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"{container_id}\t{address}\t{str(published).lower()}")
PY
}

compat_http_code() {
    local url="$1"
    curl --silent --show-error --connect-timeout 2 --max-time 8 \
        --output /dev/null --write-out '%{http_code}' "${url}" 2>/dev/null || true
}

compat_wait_http() {
    local url="$1" expected="${2:-200}" attempts="${3:-120}" delay="${4:-2}"
    local code='' attempt
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        code="$(compat_http_code "${url}")"
        if [ "${code}" = "${expected}" ]; then
            return 0
        fi
        sleep "${delay}"
    done
    printf 'Timed out waiting for %s (wanted %s, last status %s)\n' \
        "${url}" "${expected}" "${code:-none}" >&2
    return 1
}

compat_auth_header() {
    local token="$1"
    printf 'Authorization: MediaBrowser Client="RefreshKit Compat", Device="Disposable Compat Lab", DeviceId="%s", Version="1", Token="%s"\n' \
        "${RK_COMPAT_PROJECT}" "${token}"
}

compat_no_token_auth_header() {
    printf 'Authorization: MediaBrowser Client="RefreshKit Compat", Device="Disposable Compat Lab", DeviceId="%s", Version="1"\n' \
        "${RK_COMPAT_PROJECT}"
}

compat_assert_generated_paths() {
    case "${RK_COMPAT_CACHE_DIR}:${RK_COMPAT_STATE_DIR}:${RK_COMPAT_ARTIFACT_DIR}" in
        "${RK_COMPAT_DIR}/.cache:${RK_COMPAT_DIR}/.state:${RK_COMPAT_DIR}/artifacts") ;;
        *)
            compat_die "clean refuses non-default generated paths; remove custom paths explicitly"
            ;;
    esac
}

mkdir -p "${RK_COMPAT_CACHE_DIR}" "${RK_COMPAT_STATE_DIR}" "${RK_COMPAT_ARTIFACT_DIR}"
