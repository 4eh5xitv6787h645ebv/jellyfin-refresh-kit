#!/usr/bin/env bash

# Isolated exact Jellyfin 10.11.0 load smoke for the declared net9 ABI floor.
# It owns a profile-only service, loopback port, volumes, token, and artifact tree.

set -euo pipefail
umask 077

ABI_FLOOR_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
# Resolved from this file at runtime.
# shellcheck disable=SC1091
source "${ABI_FLOOR_HERE}/common.sh"

ABI_FLOOR_IMAGE='jellyfin/jellyfin:10.11.0@sha256:59417f441213e236a9f907d4e71a13472042409d85f9e9310dbdd87ee33d7bd4'
ABI_FLOOR_SERVICE='abi-floor'
ABI_FLOOR_OUT="${RK_ARTIFACT_DIR}/abi-floor"
ABI_FLOOR_RESULT="${ABI_FLOOR_OUT}/result.json"
ABI_FLOOR_TOKEN="$(rk_token_file abi-floor)"
ABI_FLOOR_CONTAINER=''
ABI_FLOOR_REFERENCE_IMAGE_ID=''

abi_floor_compose() {
    RK_ABI_FLOOR_IMAGE="${ABI_FLOOR_IMAGE}" rk_compose --profile abi-floor "$@"
}

validate_runtime_controls() {
    local image="${1:-}"
    [ "${image}" = "${ABI_FLOOR_IMAGE}" ] || {
        printf 'FATAL: image is outside the exact ABI-floor whitelist: %s\n' "${image}" >&2
        return 1
    }
    case "${RK_ABI_FLOOR_SKIP_PULL:-0}" in
        0|1) ;;
        *) printf 'FATAL: RK_ABI_FLOOR_SKIP_PULL must be exactly 0 or 1\n' >&2; return 1 ;;
    esac
    if ! [[ "${RK_ABI_FLOOR_PORT}" =~ ^[0-9]{4,5}$ ]] \
        || [ "${RK_ABI_FLOOR_PORT}" -lt 1024 ] \
        || [ "${RK_ABI_FLOOR_PORT}" -gt 65535 ]; then
        printf 'FATAL: RK_ABI_FLOOR_PORT must be an integer from 1024 through 65535\n' >&2
        return 1
    fi
    local pull_timeout="${RK_ABI_FLOOR_PULL_TIMEOUT_SECONDS:-900}"
    [[ "${pull_timeout}" =~ ^[1-9][0-9]{0,3}$ ]] || {
        printf 'FATAL: RK_ABI_FLOOR_PULL_TIMEOUT_SECONDS must be 1..9999\n' >&2
        return 1
    }
}

prepare_output() {
    if [ -d "${ABI_FLOOR_OUT}" ]; then
        mv -- "${ABI_FLOOR_OUT}" \
            "${ABI_FLOOR_OUT}.previous-$(date -u +%Y%m%dT%H%M%SZ)-$$" || return $?
    fi
    mkdir -p "${ABI_FLOOR_OUT}/server"
    rm -f -- "${ABI_FLOOR_TOKEN}" "${ABI_FLOOR_TOKEN}.part"
}

floor_container_id() {
    local id
    id="$(abi_floor_compose ps -q "${ABI_FLOOR_SERVICE}")" || return $?
    [ -n "${id}" ] || {
        printf 'FATAL: ABI-floor service has no running container\n' >&2
        return 1
    }
    printf '%s\n' "${id}"
}

capture_server_log() {
    [ -n "${ABI_FLOOR_CONTAINER}" ] || ABI_FLOOR_CONTAINER="$(floor_container_id 2>/dev/null || true)"
    [ -n "${ABI_FLOOR_CONTAINER}" ] || return 0
    docker logs "${ABI_FLOOR_CONTAINER}" > "${ABI_FLOOR_OUT}/server.log" 2>&1 || true
}

write_failure() {
    local message="$1"
    capture_server_log
    python3 - "${ABI_FLOOR_RESULT}" "${message}" <<'PY'
import datetime
import json
import pathlib
import sys

path, message = sys.argv[1:]
value = {
    "schemaVersion": 1,
    "scenario": "jellyfin-10.11.0-abi-floor",
    "completed": False,
    "failures": [message],
    "finishedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
}
target = pathlib.Path(path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
    printf 'FATAL: ABI-floor smoke: %s\n' "${message}" >&2
}

validate_floor_volume_identity() {
    local name="$1" logical="$2" labels="$3"
    python3 - "${name}" "${logical}" "${labels}" "${RK_PROJECT}" <<'PY'
import json
import sys

name, logical, labels_raw, project = sys.argv[1:]
labels = json.loads(labels_raw)
expected = f"{project}_{logical}"
if name != expected:
    raise SystemExit(f"FATAL: refusing ABI-floor volume {name!r}; expected {expected!r}")
if not isinstance(labels, dict) or {
    "com.docker.compose.project": labels.get("com.docker.compose.project"),
    "com.docker.compose.volume": labels.get("com.docker.compose.volume"),
} != {
    "com.docker.compose.project": project,
    "com.docker.compose.volume": logical,
}:
    raise SystemExit(f"FATAL: refusing ABI-floor volume with foreign labels: {labels!r}")
PY
}

validate_floor_network_identity() {
    local name="$1" internal="$2" labels="$3"
    python3 - "${name}" "${internal}" "${labels}" "${RK_PROJECT}" <<'PY'
import json
import sys

name, internal, labels_raw, project = sys.argv[1:]
labels = json.loads(labels_raw)
expected = f"{project}_abi-floor-internal"
if name != expected or internal != "true":
    raise SystemExit(
        f"FATAL: refusing ABI-floor network {name!r} (internal={internal!r}); "
        f"expected exact internal network {expected!r}"
    )
if not isinstance(labels, dict) or {
    "com.docker.compose.project": labels.get("com.docker.compose.project"),
    "com.docker.compose.network": labels.get("com.docker.compose.network"),
} != {
    "com.docker.compose.project": project,
    "com.docker.compose.network": "abi-floor-internal",
}:
    raise SystemExit(f"FATAL: refusing ABI-floor network with foreign labels: {labels!r}")
PY
}

assert_floor_resources_owned_if_present() {
    local logical expected candidate labels matches
    for logical in abi-floor-config abi-floor-cache; do
        expected="${RK_PROJECT}_${logical}"
        matches="$(docker volume ls --quiet \
            --filter "label=com.docker.compose.project=${RK_PROJECT}" \
            --filter "label=com.docker.compose.volume=${logical}")" || return $?
        while IFS= read -r candidate; do
            [ -n "${candidate}" ] || continue
            [ "${candidate}" = "${expected}" ] || {
                printf 'FATAL: refusing unexpected ABI-floor volume selected by labels: %s\n' \
                    "${candidate}" >&2
                return 1
            }
        done <<< "${matches}"
        if docker volume inspect "${expected}" >/dev/null 2>&1; then
            labels="$(docker volume inspect --format '{{json .Labels}}' "${expected}")" \
                || return $?
            validate_floor_volume_identity "${expected}" "${logical}" "${labels}" || return $?
        fi
    done

    expected="${RK_PROJECT}_abi-floor-internal"
    matches="$(docker network ls --quiet \
        --filter "label=com.docker.compose.project=${RK_PROJECT}" \
        --filter 'label=com.docker.compose.network=abi-floor-internal')" || return $?
    while IFS= read -r candidate; do
        [ -n "${candidate}" ] || continue
        candidate="$(docker network inspect --format '{{.Name}}' "${candidate}")" || return $?
        [ "${candidate}" = "${expected}" ] || {
            printf 'FATAL: refusing unexpected ABI-floor network selected by labels: %s\n' \
                "${candidate}" >&2
            return 1
        }
    done <<< "${matches}"
    if docker network inspect "${expected}" >/dev/null 2>&1; then
        labels="$(docker network inspect --format '{{json .Labels}}' "${expected}")" \
            || return $?
        candidate="$(docker network inspect --format '{{.Internal}}' "${expected}")" \
            || return $?
        validate_floor_network_identity "${expected}" "${candidate}" "${labels}" || return $?
    fi
}

reset_floor_storage() {
    local logical expected labels
    assert_floor_resources_owned_if_present || return $?
    abi_floor_compose stop "${ABI_FLOOR_SERVICE}" >/dev/null 2>&1 || true
    abi_floor_compose rm --force --stop "${ABI_FLOOR_SERVICE}" >/dev/null 2>&1 || true
    for logical in abi-floor-config abi-floor-cache; do
        expected="${RK_PROJECT}_${logical}"
        if docker volume inspect "${expected}" >/dev/null 2>&1; then
            labels="$(docker volume inspect --format '{{json .Labels}}' "${expected}")" \
                || return $?
            validate_floor_volume_identity "${expected}" "${logical}" "${labels}" || return $?
            docker volume rm "${expected}" >/dev/null || return $?
        fi
    done
}

pin_floor_snapshot() {
    local requested resolved
    requested="${RK_BUILD_SNAPSHOT:-${RK_REPO_ROOT}/plugin/build}"
    resolved="$(readlink -f -- "${requested}" 2>/dev/null || true)"
    if [ -z "${resolved}" ] || [ ! -d "${resolved}" ]; then
        printf 'FATAL: plugin build snapshot does not exist: %s\n' "${requested}" >&2
        return 1
    fi
    case "${resolved}" in
        "${RK_REPO_ROOT}/plugin/.builds/"*) ;;
        *) printf 'FATAL: plugin build resolved outside plugin/.builds: %s\n' "${resolved}" >&2; return 1 ;;
    esac
    python3 "${RK_REPO_ROOT}/scripts/verify-package.py" \
        --build-dir "${resolved}" --manifest "${RK_REPO_ROOT}/manifest.json" \
        --manifest-mode structure --require-immutable-snapshot || return $?
    RK_BUILD_SNAPSHOT="${resolved}"
    RK_STAGE_JF10="${resolved}/stage"
    export RK_BUILD_SNAPSHOT
    rk_log "pinned plugin snapshot $(basename "${resolved}") for the ABI-floor smoke"
}

ensure_exact_floor_image() {
    local image="$1" digest repo_digests pull_timeout
    validate_runtime_controls "${image}" || return $?
    pull_timeout="${RK_ABI_FLOOR_PULL_TIMEOUT_SECONDS:-900}"
    if [ "${RK_ABI_FLOOR_SKIP_PULL:-0}" != 1 ]; then
        rk_log "preflighting exact ABI-floor image ${image}"
        timeout "${pull_timeout}" docker pull "${image}" >/dev/null || {
            printf 'FATAL: bounded pull failed for exact ABI-floor image: %s\n' "${image}" >&2
            return 1
        }
    fi
    docker image inspect "${image}" >/dev/null 2>&1 || {
        printf 'FATAL: exact ABI-floor image is unavailable locally: %s\n' "${image}" >&2
        return 1
    }
    ABI_FLOOR_REFERENCE_IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${image}")" \
        || return $?
    [[ "${ABI_FLOOR_REFERENCE_IMAGE_ID}" =~ ^sha256:[0-9a-f]{64}$ ]] || {
        printf 'FATAL: exact ABI-floor reference resolved to malformed image ID: %s\n' \
            "${ABI_FLOOR_REFERENCE_IMAGE_ID}" >&2
        return 1
    }
    digest="${image##*@}"
    repo_digests="$(docker image inspect --format '{{json .RepoDigests}}' "${image}")" || return $?
    python3 - "${repo_digests}" "${digest}" <<'PY'
import json
import sys

repo_digests, expected = json.loads(sys.argv[1]), sys.argv[2]
if not isinstance(repo_digests, list) or not any(
    isinstance(item, str) and item.endswith("@" + expected) for item in repo_digests
):
    raise SystemExit(f"FATAL: local RepoDigests do not contain exact {expected}: {repo_digests!r}")
PY
}

start_floor_service() {
    assert_floor_resources_owned_if_present || return $?
    abi_floor_compose up -d --wait --pull never --no-deps --force-recreate \
        "${ABI_FLOOR_SERVICE}" || return $?
    ABI_FLOOR_CONTAINER="$(floor_container_id)" || return $?
    local configured labels running_image_id mounts networks network_name network_internal network_labels
    local network_containers
    local logical expected volume_labels
    configured="$(docker inspect --format '{{.Config.Image}}' "${ABI_FLOOR_CONTAINER}")" || return $?
    running_image_id="$(docker inspect --format '{{.Image}}' "${ABI_FLOOR_CONTAINER}")" || return $?
    labels="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' "${ABI_FLOOR_CONTAINER}")" || return $?
    [ "${configured}" = "${ABI_FLOOR_IMAGE}" ] || {
        printf 'FATAL: ABI-floor container uses %s, expected %s\n' \
            "${configured}" "${ABI_FLOOR_IMAGE}" >&2
        return 1
    }
    [ "${labels}" = "${RK_PROJECT}|${ABI_FLOOR_SERVICE}" ] || {
        printf 'FATAL: refusing ABI-floor container with labels %s\n' "${labels}" >&2
        return 1
    }
    if [ -z "${ABI_FLOOR_REFERENCE_IMAGE_ID}" ] \
        || [ "${running_image_id}" != "${ABI_FLOOR_REFERENCE_IMAGE_ID}" ]; then
        printf 'FATAL: ABI-floor container image ID %s differs from exact reference ID %s\n' \
            "${running_image_id}" "${ABI_FLOOR_REFERENCE_IMAGE_ID:-<unset>}" >&2
        return 1
    fi
    for logical in abi-floor-config abi-floor-cache; do
        expected="${RK_PROJECT}_${logical}"
        volume_labels="$(docker volume inspect --format '{{json .Labels}}' "${expected}")" \
            || return $?
        validate_floor_volume_identity "${expected}" "${logical}" "${volume_labels}" \
            || return $?
    done
    mounts="$(docker inspect --format '{{json .Mounts}}' "${ABI_FLOOR_CONTAINER}")" \
        || return $?
    python3 - "${mounts}" "${RK_PROJECT}" <<'PY'
import json
import sys

mounts, project = json.loads(sys.argv[1]), sys.argv[2]
expected = {
    ("/config", f"{project}_abi-floor-config", "volume", True),
    ("/cache", f"{project}_abi-floor-cache", "volume", True),
}
actual = {
    (row.get("Destination"), row.get("Name"), row.get("Type"), row.get("RW"))
    for row in mounts
    if isinstance(row, dict)
} if isinstance(mounts, list) else set()
if actual != expected or len(mounts) != 2:
    raise SystemExit(f"FATAL: refusing ABI-floor mount inventory: {mounts!r}")
PY
    networks="$(docker inspect --format '{{json .NetworkSettings.Networks}}' \
        "${ABI_FLOOR_CONTAINER}")" || return $?
    network_name="$(python3 - "${networks}" <<'PY'
import json
import sys

networks = json.loads(sys.argv[1])
if not isinstance(networks, dict) or len(networks) != 1:
    raise SystemExit(f"FATAL: ABI-floor network attachment differs: {networks!r}")
print(next(iter(networks)))
PY
)" || return $?
    network_internal="$(docker network inspect --format '{{.Internal}}' "${network_name}")" \
        || return $?
    network_labels="$(docker network inspect --format '{{json .Labels}}' "${network_name}")" \
        || return $?
    validate_floor_network_identity \
        "${network_name}" "${network_internal}" "${network_labels}" || return $?
    network_containers="$(docker network inspect --format '{{json .Containers}}' \
        "${network_name}")" || return $?
    python3 - "${network_containers}" "${ABI_FLOOR_CONTAINER}" <<'PY'
import json
import sys

containers, expected = json.loads(sys.argv[1]), sys.argv[2]
if not isinstance(containers, dict) or set(containers) != {expected}:
    raise SystemExit(
        f"FATAL: ABI-floor network membership is not exclusive: {sorted(containers)!r}"
    )
PY
}

write_success() {
    local configured image_id repo_digests labels mounts ports networks
    local network_name network_id network_internal network_labels network_containers
    local config_volume cache_volume config_volume_labels cache_volume_labels
    local plugin_version container_dll_path container_dll_sha stage_dll_sha managed_identity
    configured="$(docker inspect --format '{{.Config.Image}}' "${ABI_FLOOR_CONTAINER}")" || return $?
    image_id="$(docker inspect --format '{{.Image}}' "${ABI_FLOOR_CONTAINER}")" || return $?
    repo_digests="$(docker image inspect --format '{{json .RepoDigests}}' "${ABI_FLOOR_IMAGE}")" || return $?
    labels="$(docker inspect --format '{{json .Config.Labels}}' "${ABI_FLOOR_CONTAINER}")" || return $?
    mounts="$(docker inspect --format '{{json .Mounts}}' "${ABI_FLOOR_CONTAINER}")" || return $?
    ports="$(docker inspect --format '{{json .HostConfig.PortBindings}}' "${ABI_FLOOR_CONTAINER}")" || return $?
    networks="$(docker inspect --format '{{json .NetworkSettings.Networks}}' "${ABI_FLOOR_CONTAINER}")" || return $?
    network_name="$(python3 - "${networks}" <<'PY'
import json
import sys

networks = json.loads(sys.argv[1])
if not isinstance(networks, dict) or len(networks) != 1:
    raise SystemExit(f"FATAL: ABI-floor network attachment differs: {networks!r}")
print(next(iter(networks)))
PY
)" || return $?
    network_id="$(docker network inspect --format '{{.Id}}' "${network_name}")" || return $?
    network_internal="$(docker network inspect --format '{{.Internal}}' "${network_name}")" || return $?
    network_labels="$(docker network inspect --format '{{json .Labels}}' "${network_name}")" || return $?
    network_containers="$(docker network inspect --format '{{json .Containers}}' \
        "${network_name}")" || return $?
    config_volume="${RK_PROJECT}_abi-floor-config"
    cache_volume="${RK_PROJECT}_abi-floor-cache"
    config_volume_labels="$(docker volume inspect --format '{{json .Labels}}' \
        "${config_volume}")" || return $?
    cache_volume_labels="$(docker volume inspect --format '{{json .Labels}}' \
        "${cache_volume}")" || return $?
    validate_floor_volume_identity \
        "${config_volume}" abi-floor-config "${config_volume_labels}" || return $?
    validate_floor_volume_identity \
        "${cache_volume}" abi-floor-cache "${cache_volume_labels}" || return $?
    validate_floor_network_identity \
        "${network_name}" "${network_internal}" "${network_labels}" || return $?
    [ "${image_id}" = "${ABI_FLOOR_REFERENCE_IMAGE_ID}" ] || {
        printf 'FATAL: ABI-floor evidence image ID differs from exact reference ID\n' >&2
        return 1
    }
    plugin_version="$(python3 -c \
        'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' \
        "${RK_STAGE_JF10}/meta.json")" || return $?
    [[ "${plugin_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    container_dll_path="/config/plugins/Jellyfin Refresh Kit_${plugin_version}/Jellyfin.Plugin.RefreshKit.dll"
    container_dll_sha="$(docker exec "${ABI_FLOOR_CONTAINER}" \
        sha256sum -- "${container_dll_path}" | awk 'NR == 1 { print $1 }')" || return $?
    stage_dll_sha="$(sha256sum "${RK_STAGE_JF10}/Jellyfin.Plugin.RefreshKit.dll" \
        | awk 'NR == 1 { print $1 }')" || return $?
    if ! [[ "${container_dll_sha}" =~ ^[0-9a-f]{64}$ ]] \
        || [ "${container_dll_sha}" != "${stage_dll_sha}" ]; then
        printf 'FATAL: running ABI-floor container DLL differs from the pinned snapshot\n' >&2
        return 1
    fi
    managed_identity="$(python3 "${RK_REPO_ROOT}/scripts/abi_floor_evidence.py" \
        --managed-identity "${RK_STAGE_JF10}/Jellyfin.Plugin.RefreshKit.dll")" || return $?
    python3 - \
        "${ABI_FLOOR_RESULT}" "${RK_BUILD_SNAPSHOT}" "${RK_STAGE_JF10}/meta.json" \
        "${RK_STAGE_JF10}/Jellyfin.Plugin.RefreshKit.dll" \
        "${ABI_FLOOR_OUT}/server/result.json" "${ABI_FLOOR_OUT}/server/generation.json" \
        "${ABI_FLOOR_OUT}/server/diagnostics.json" "${ABI_FLOOR_OUT}/server/plugins.json" \
        "${ABI_FLOOR_OUT}/server.log" "${ABI_FLOOR_IMAGE}" "${configured}" \
        "${image_id}" "${repo_digests}" "${labels}" "${mounts}" "${ports}" \
        "${networks}" "${network_name}" "${network_id}" "${network_internal}" \
        "${network_labels}" "${RK_PROJECT}" "${ABI_FLOOR_REFERENCE_IMAGE_ID}" \
        "${config_volume_labels}" "${cache_volume_labels}" \
        "${container_dll_path}" "${container_dll_sha}" "${managed_identity}" \
        "${network_containers}" "${ABI_FLOOR_CONTAINER}" <<'PY'
import datetime
import hashlib
import json
import os
import pathlib
import re
import sys

(
    output, snapshot, meta_path, dll_path, server_path, generation_path,
    diagnostics_path, plugins_path, log_path, image, configured, image_id,
    repo_digests_raw, labels_raw, mounts_raw, ports_raw, networks_raw,
    network_name, network_id, network_internal, network_labels_raw, project,
    reference_image_id, config_volume_labels_raw, cache_volume_labels_raw,
    container_dll_path, container_dll_sha, managed_identity_raw,
    network_containers_raw, container_id,
) = sys.argv[1:]

def load(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))

def digest(path, algorithm="sha256"):
    value = hashlib.new(algorithm, usedforsecurity=False)
    value.update(pathlib.Path(path).read_bytes())
    return value.hexdigest()

meta = load(meta_path)
version = str(meta["version"])
package_path = pathlib.Path(snapshot) / f"jellyfin-refresh-kit_{version}.zip"
server = load(server_path)
generation = load(generation_path)
diagnostics = load(diagnostics_path)
plugins = load(plugins_path)
if not isinstance(plugins, list):
    raise SystemExit("FATAL: authenticated plugin inventory is not an array")

def field(value, name):
    return value.get(name, value.get(name[:1].lower() + name[1:]))

guid = "515255fe333249b0b4710be58c8221d8"
matches = [row for row in plugins if isinstance(row, dict)
           and str(field(row, "Id") or "").replace("-", "").lower() == guid]
diagnostic_rows = field(diagnostics, "Plugins")
if not isinstance(diagnostic_rows, list):
    raise SystemExit("FATAL: diagnostics plugin inventory is not an array")
diagnostic_matches = [row for row in diagnostic_rows if isinstance(row, dict)
                      and str(field(row, "Id") or "").replace("-", "").lower() == guid]
if len(matches) != 1 or len(diagnostic_matches) != 1:
    raise SystemExit("FATAL: Refresh Kit inventory is not exact")

labels = json.loads(labels_raw)
mounts = json.loads(mounts_raw)
ports = json.loads(ports_raw)
networks = json.loads(networks_raw)
network_labels = json.loads(network_labels_raw)
volume_labels = {
    "/config": json.loads(config_volume_labels_raw),
    "/cache": json.loads(cache_volume_labels_raw),
}
managed_identity = json.loads(managed_identity_raw)
network_containers = json.loads(network_containers_raw)
if not isinstance(labels, dict) or not isinstance(mounts, list) \
        or not isinstance(networks, dict) or not isinstance(network_labels, dict):
    raise SystemExit("FATAL: ABI-floor container labels/mounts/networks are malformed")
if not isinstance(ports, dict) or set(ports) != {"8096/tcp"}:
    raise SystemExit(f"FATAL: ABI-floor published-port inventory differs: {ports!r}")
bindings = ports.get("8096/tcp") if isinstance(ports, dict) else None
if not isinstance(bindings, list) or len(bindings) != 1:
    raise SystemExit(f"FATAL: ABI-floor port binding differs: {ports!r}")
binding = bindings[0]
volume_rows = sorted(({
    "destination": row.get("Destination"),
    "name": row.get("Name"),
    "type": row.get("Type"),
    "readWrite": row.get("RW"),
    "labels": {
        "com.docker.compose.project": volume_labels[row.get("Destination")].get(
            "com.docker.compose.project"
        ),
        "com.docker.compose.volume": volume_labels[row.get("Destination")].get(
            "com.docker.compose.volume"
        ),
    },
} for row in mounts if isinstance(row, dict)
                     and row.get("Destination") in ("/config", "/cache")),
                     key=lambda row: str(row["destination"]))
if len(mounts) != 2 or len(volume_rows) != 2:
    raise SystemExit(f"FATAL: ABI-floor mount inventory differs: {mounts!r}")
expected_volumes = {
    "/config": (f"{project}_abi-floor-config", "abi-floor-config"),
    "/cache": (f"{project}_abi-floor-cache", "abi-floor-cache"),
}
for row in volume_rows:
    expected_name, expected_logical = expected_volumes[row["destination"]]
    if row != {
        "destination": row["destination"],
        "name": expected_name,
        "type": "volume",
        "readWrite": True,
        "labels": {
            "com.docker.compose.project": project,
            "com.docker.compose.volume": expected_logical,
        },
    }:
        raise SystemExit(f"FATAL: ABI-floor exact volume identity differs: {row!r}")
if set(networks) != {network_name} or network_internal != "true" \
        or network_name != f"{project}_abi-floor-internal" \
        or network_labels.get("com.docker.compose.project") != project \
        or network_labels.get("com.docker.compose.network") != "abi-floor-internal":
    raise SystemExit(f"FATAL: ABI-floor internal-network identity differs: {networks!r}")
network_endpoint = networks[network_name]
if not isinstance(network_endpoint, dict):
    raise SystemExit("FATAL: ABI-floor network endpoint is malformed")
if not isinstance(network_containers, dict) or set(network_containers) != {container_id}:
    raise SystemExit(f"FATAL: ABI-floor network membership differs: {network_containers!r}")

log = pathlib.Path(log_path).read_text(encoding="utf-8", errors="replace")
assembly_text = f"Loaded assembly {managed_identity['assemblyFullName']} from {container_dll_path}"
plugin_text = f"Loaded plugin: Jellyfin Refresh Kit {version}"
assembly_loads = [line for line in log.splitlines() if line.endswith(assembly_text)]
plugin_loads = [line for line in log.splitlines() if line.endswith(plugin_text)]
scoped_errors = [
    line for line in log.splitlines()
    if ("Jellyfin.Plugin.RefreshKit" in line
        or "Jellyfin Refresh Kit" in line
        or "JellyfinRefreshKit" in line)
    and re.search(
        r"\[(?:ERR|WRN|FTL)\]|failed|incompatible|exception|could not load|"
        r"badimageformat|fileload|typeload|missingmethod",
        line,
        re.IGNORECASE,
    )
]
if len(assembly_loads) != 1 or len(plugin_loads) != 1 or scoped_errors:
    raise SystemExit("FATAL: server log does not prove a clean Refresh Kit load")

package = {
    "name": package_path.name,
    "bytes": package_path.stat().st_size,
    "md5": digest(package_path, "md5"),
    "sha256": digest(package_path),
}
result = {
    "schemaVersion": 1,
    "scenario": "jellyfin-10.11.0-abi-floor",
    "completed": True,
    "failures": [],
    "finishedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
    "immutableSnapshot": pathlib.Path(snapshot).name,
    "sourceIdentity": {
        "revision": meta["sourceRevision"],
        "treeSha256": meta["sourceTreeSha256"],
        "dateEpoch": meta["sourceDateEpoch"],
        "dirty": meta["sourceDirty"],
    },
    "stage": {
        "meta": meta,
        "dllSha256": digest(dll_path),
        "containerDll": {
            "path": container_dll_path,
            "sha256": container_dll_sha,
        },
        "managedIdentity": managed_identity,
        "package": package,
    },
    "image": {
        "reference": image,
        "digest": image.rsplit("@", 1)[1],
        "configuredReference": configured,
        "imageId": image_id,
        "referenceImageId": reference_image_id,
        "repoDigests": json.loads(repo_digests_raw),
    },
    "container": {
        "id": container_id,
        "project": project,
        "service": "abi-floor",
        "labels": {
            "com.docker.compose.project": labels.get("com.docker.compose.project"),
            "com.docker.compose.service": labels.get("com.docker.compose.service"),
        },
        "portBinding": {
            "containerPort": "8096/tcp",
            "hostIp": binding.get("HostIp"),
            "hostPort": binding.get("HostPort"),
        },
        "volumes": volume_rows,
        "network": {
            "name": network_name,
            "id": network_id,
            "endpointId": network_endpoint.get("EndpointID"),
            "attachedContainerIds": sorted(network_containers),
            "internal": True,
            "labels": {
                "com.docker.compose.project": network_labels.get("com.docker.compose.project"),
                "com.docker.compose.network": network_labels.get("com.docker.compose.network"),
            },
        },
    },
    "server": server,
    "generation": generation,
    "plugin": matches[0],
    "diagnosticsPlugin": diagnostic_matches[0],
    "logChecks": {
        "assemblyLoadMatches": len(assembly_loads),
        "pluginLoadMatches": len(plugin_loads),
        "scopedErrorLines": scoped_errors,
    },
}
target = pathlib.Path(output)
partial = target.with_suffix(".json.part")
partial.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(partial, target)
PY
}

run_smoke() {
    local required
    validate_runtime_controls "${ABI_FLOOR_IMAGE}" || {
        write_failure 'runtime control validation failed'; return 1;
    }
    for required in awk curl docker python3 readlink sha256sum timeout; do
        command -v "${required}" >/dev/null 2>&1 || {
            write_failure "required command not found: ${required}"; return 1;
        }
    done
    docker info >/dev/null 2>&1 || {
        write_failure 'Docker daemon is unavailable'; return 1;
    }
    docker compose version >/dev/null 2>&1 || {
        write_failure 'Docker Compose is unavailable'; return 1;
    }
    pin_floor_snapshot || {
        write_failure 'immutable plugin snapshot validation failed'; return 1;
    }
    ensure_exact_floor_image "${ABI_FLOOR_IMAGE}" || {
        write_failure 'exact Jellyfin 10.11.0 image preflight failed'; return 1;
    }
    reset_floor_storage || {
        write_failure 'dedicated ABI-floor storage reset failed'; return 1;
    }
    start_floor_service || {
        write_failure 'exact Jellyfin 10.11.0 service did not start'; return 1;
    }
    RK_BUILD_SNAPSHOT="${RK_BUILD_SNAPSHOT}" RK_ABI_FLOOR_IMAGE="${ABI_FLOOR_IMAGE}" \
        bash "${ABI_FLOOR_HERE}/provision.sh" abi-floor jf10 || {
            write_failure 'net9 plugin provisioning failed on Jellyfin 10.11.0'; return 1;
        }
    RK_BUILD_SNAPSHOT="${RK_BUILD_SNAPSHOT}" RK_ABI_FLOOR_IMAGE="${ABI_FLOOR_IMAGE}" \
        bash "${ABI_FLOOR_HERE}/check.sh" abi-floor || {
            write_failure 'endpoint/shell checks failed on Jellyfin 10.11.0'; return 1;
        }
    capture_server_log
    [ -s "${ABI_FLOOR_OUT}/server.log" ] || {
        write_failure 'Jellyfin 10.11.0 server log is missing'; return 1;
    }
    write_success || {
        write_failure 'could not assemble strict ABI-floor evidence'; return 1;
    }
    python3 "${RK_REPO_ROOT}/scripts/abi_floor_evidence.py" \
        --root "${ABI_FLOOR_OUT}" --build "${RK_BUILD_SNAPSHOT}" || {
            write_failure 'strict ABI-floor evidence validation failed'; return 1;
        }
    rk_log "exact Jellyfin 10.11.0 ABI-floor load smoke PASS"
}

main() {
    [ "$#" -eq 0 ] || {
        printf 'Usage: abi-floor-smoke.sh\n' >&2
        return 2
    }
    prepare_output || return $?
    run_smoke
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
