#!/usr/bin/env bash

# Real, project-scoped Jellyfin host upgrades with an authenticated browser kept
# open by host-upgrade-browser.cjs.  This script deliberately owns a separate
# Compose service and volume pair; it never reuses the normal jf10/jf12 labs.

set -euo pipefail
umask 077

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=e2e/jellyfin/lib/common.sh
source "${HERE}/common.sh"

JF101110_IMAGE='jellyfin/jellyfin:10.11.10@sha256:f66273e014b307e4ac46778845ebc1e9ee24b2e57c1fc17d5ec5ac3015649bfa'
JF101111_IMAGE='jellyfin/jellyfin:10.11.11@sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db'
JF12RC4_IMAGE='jellyfin/jellyfin:12.0-rc4@sha256:db1df1d111c27ba1f10bb8fce6630892f66eb66b12c2b24e79011453ac18b3db'

UPGRADE_SERVICE='host-upgrade'
UPGRADE_PORT="${RK_HOST_UPGRADE_PORT:-18118}"
if ! [[ "${UPGRADE_PORT}" =~ ^[0-9]{4,5}$ ]] \
    || [ "${UPGRADE_PORT}" -lt 1024 ] \
    || [ "${UPGRADE_PORT}" -gt 65535 ]; then
    printf 'FATAL: RK_HOST_UPGRADE_PORT must be an integer from 1024 through 65535\n' >&2
    exit 2
fi
UPGRADE_ORIGIN="http://127.0.0.1:${UPGRADE_PORT}"
VIEWER_USER="${RK_LAB_VIEWER_USER:-rk_viewer}"
VIEWER_PASSWORD="${RK_LAB_VIEWER_PASSWORD:-Viewer669Pw!x}"
PLUGIN_VERSION=''
PLUGIN_DIR=''

usage() {
    cat <<'EOF'
Usage: host-upgrade.sh jf10|jf12|all|negative

  jf10      Jellyfin 10.11.10 -> 10.11.11 with the net9 plugin active
  jf12      Jellyfin 10.11.11 -> 12.0-rc4, disabling net9 then enabling net10
  all       run both paths independently and write an aggregate result
  negative  no-Docker fail-closed/static regression checks
EOF
}

upgrade_compose() {
    local image="$1"
    shift
    RK_HOST_UPGRADE_IMAGE="${image}" docker compose \
        --project-name "${RK_PROJECT}" -f "${RK_COMPOSE_FILE}" "$@"
}

upgrade_container_id() {
    local image="$1" id
    id="$(upgrade_compose "${image}" --profile host-upgrade ps -q "${UPGRADE_SERVICE}")"
    [ -n "${id}" ] || {
        printf 'FATAL: host-upgrade service has no running container\n' >&2
        return 1
    }
    printf '%s\n' "${id}"
}

assert_exact_container() {
    local image="$1" id configured labels
    id="$(upgrade_container_id "${image}")" || return $?
    configured="$(docker inspect --format '{{.Config.Image}}' "${id}")" || return $?
    labels="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}' "${id}")" || return $?
    [ "${configured}" = "${image}" ] || {
        printf 'FATAL: host-upgrade container uses %s, expected exact %s\n' "${configured}" "${image}" >&2
        return 1
    }
    [ "${labels}" = "${RK_PROJECT}|${UPGRADE_SERVICE}" ] || {
        printf 'FATAL: refusing container with labels %s\n' "${labels}" >&2
        return 1
    }
}

reset_upgrade_storage() {
    local image="$1" volume
    upgrade_compose "${image}" --profile host-upgrade stop "${UPGRADE_SERVICE}" >/dev/null 2>&1 || true
    upgrade_compose "${image}" --profile host-upgrade rm --force --stop "${UPGRADE_SERVICE}" >/dev/null 2>&1 || true
    while IFS= read -r volume; do
        [ -n "${volume}" ] || continue
        docker volume rm "${volume}" >/dev/null || return $?
    done < <(docker volume ls --quiet \
        --filter "label=com.docker.compose.project=${RK_PROJECT}" \
        --filter 'label=com.docker.compose.volume=host-upgrade-config')
    while IFS= read -r volume; do
        [ -n "${volume}" ] || continue
        docker volume rm "${volume}" >/dev/null || return $?
    done < <(docker volume ls --quiet \
        --filter "label=com.docker.compose.project=${RK_PROJECT}" \
        --filter 'label=com.docker.compose.volume=host-upgrade-cache')
}

ensure_exact_image() {
    local image="$1" digest repo_digests pull_timeout
    case "${image}" in
        "${JF101110_IMAGE}"|"${JF101111_IMAGE}"|"${JF12RC4_IMAGE}") ;;
        *) printf 'FATAL: image is outside the pinned host-upgrade whitelist: %s\n' "${image}" >&2; return 1 ;;
    esac
    case "${RK_HOST_UPGRADE_SKIP_PULL:-0}" in
        0|1) ;;
        *) printf 'FATAL: RK_HOST_UPGRADE_SKIP_PULL must be exactly 0 or 1\n' >&2; return 1 ;;
    esac
    pull_timeout="${RK_HOST_UPGRADE_PULL_TIMEOUT_SECONDS:-900}"
    [[ "${pull_timeout}" =~ ^[1-9][0-9]{0,3}$ ]] || {
        printf 'FATAL: RK_HOST_UPGRADE_PULL_TIMEOUT_SECONDS must be 1..9999\n' >&2
        return 1
    }
    if [ "${RK_HOST_UPGRADE_SKIP_PULL:-0}" != 1 ]; then
        rk_log "preflighting exact host image ${image}"
        timeout "${pull_timeout}" docker pull "${image}" >/dev/null || {
            printf 'FATAL: bounded pull failed for exact pinned image: %s\n' "${image}" >&2
            return 1
        }
    fi
    docker image inspect "${image}" >/dev/null 2>&1 || {
        printf 'FATAL: exact pinned image is unavailable locally: %s\n' "${image}" >&2
        return 1
    }
    digest="${image##*@}"
    repo_digests="$(docker image inspect --format '{{json .RepoDigests}}' "${image}")" || return $?
    python3 -c '
import json, sys
repo_digests, expected = json.loads(sys.argv[1]), sys.argv[2]
if not isinstance(repo_digests, list) or not any(
    isinstance(item, str) and item.endswith("@" + expected) for item in repo_digests
):
    raise SystemExit(f"FATAL: local RepoDigests do not contain exact {expected}: {repo_digests!r}")
' "${repo_digests}" "${digest}" || return $?
}

start_exact_image() {
    local image="$1"
    ensure_exact_image "${image}" || return $?
    upgrade_compose "${image}" --profile host-upgrade up -d --wait \
        --pull never --no-deps --force-recreate "${UPGRADE_SERVICE}" || return $?
    assert_exact_container "${image}"
}

json_field() {
    local field="$1"
    python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
    key = sys.argv[1]
    print(data.get(key, data.get(key[:1].lower() + key[1:], "")))
except Exception:
    print("")
' "${field}"
}

wizard_request() {
    local label="$1"
    shift
    for _ in {1..30}; do
        if curl --fail --silent --show-error --connect-timeout 2 --max-time 15 "$@"; then
            return 0
        fi
        sleep 1
    done
    printf 'FATAL: startup wizard %s failed after 30 attempts\n' "${label}" >&2
    return 1
}

authenticate_to_files() {
    local username="$1" password="$2" token_file="$3" id_file="$4"
    local payload response
    payload="$(python3 - "${username}" "${password}" <<'PY'
import json, sys
print(json.dumps({"Username": sys.argv[1], "Pw": sys.argv[2]}, separators=(",", ":")))
PY
)"
    for _ in $(seq 1 90); do
        response="$(curl --silent --show-error --connect-timeout 2 --max-time 8 \
            -X POST "${UPGRADE_ORIGIN}/Users/AuthenticateByName" \
            -H 'Content-Type: application/json' \
            -H "$(rk_no_token_auth_header)" \
            --data "${payload}" 2>/dev/null || true)"
        if python3 - "${token_file}.part" "${id_file}.part" 3<<<"${response}" <<'PY'
import json, os, re, sys
token_path, id_path = sys.argv[1:]
try:
    data = json.load(os.fdopen(3, encoding="utf-8"))
except Exception:
    raise SystemExit(1)
token = data.get("AccessToken", data.get("accessToken"))
user = data.get("User", data.get("user")) or {}
user_id = user.get("Id", user.get("id"))
if not isinstance(token, str) or not token or not isinstance(user_id, str):
    raise SystemExit(1)
if not re.fullmatch(r"[0-9a-fA-F-]{32,36}", user_id):
    raise SystemExit(1)
for path, value in ((token_path, token), (id_path, user_id)):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(value + "\n")
    os.chmod(path, 0o600)
PY
        then
            mv -f -- "${token_file}.part" "${token_file}"
            mv -f -- "${id_file}.part" "${id_file}"
            return 0
        fi
        rm -f -- "${token_file}.part" "${id_file}.part"
        sleep 1
    done
    printf 'FATAL: authentication failed for disposable user %s\n' "${username}" >&2
    return 1
}

initialize_server() {
    local expected_version="$1" public_info server_version wizard_done user_payload
    rk_wait_http "${UPGRADE_ORIGIN}/System/Info/Public" 200 180 2 || return $?
    public_info="$(curl --fail --silent --show-error "${UPGRADE_ORIGIN}/System/Info/Public")" || return $?
    server_version="$(printf '%s' "${public_info}" | json_field Version)"
    [ "${server_version}" = "${expected_version}" ] || {
        printf 'FATAL: baseline reports %s, expected exact %s\n' "${server_version}" "${expected_version}" >&2
        return 1
    }
    wizard_done="$(printf '%s' "${public_info}" | json_field StartupWizardCompleted)"
    if [ "${wizard_done,,}" != true ]; then
        rk_wait_http "${UPGRADE_ORIGIN}/Startup/User" 200 120 1 || return $?
        wizard_request configuration -o /dev/null \
            -X POST "${UPGRADE_ORIGIN}/Startup/Configuration" \
            -H 'Content-Type: application/json' \
            --data '{"UICulture":"en-US","MetadataCountryCode":"US","PreferredMetadataLanguage":"en"}' || return $?
        wizard_request user-query -o /dev/null "${UPGRADE_ORIGIN}/Startup/User" || return $?
        user_payload="$(python3 - "${RK_LAB_USER}" "${RK_LAB_PASSWORD}" <<'PY'
import json, sys
print(json.dumps({"Name": sys.argv[1], "Password": sys.argv[2]}, separators=(",", ":")))
PY
)"
        wizard_request user-create -o /dev/null \
            -X POST "${UPGRADE_ORIGIN}/Startup/User" \
            -H 'Content-Type: application/json' --data "${user_payload}" || return $?
        wizard_request remote-access -o /dev/null \
            -X POST "${UPGRADE_ORIGIN}/Startup/RemoteAccess" \
            -H 'Content-Type: application/json' \
            --data '{"EnableRemoteAccess":true,"EnableAutomaticPortMapping":false}' || return $?
        wizard_request completion -o /dev/null -X POST "${UPGRADE_ORIGIN}/Startup/Complete" || return $?
    fi
}

create_viewer() {
    local admin_token="$1" viewer_token_file="$2" viewer_id_file="$3"
    local auth response viewer_id payload
    auth="$(rk_auth_header "${admin_token}")"
    payload="$(python3 - "${VIEWER_USER}" "${VIEWER_PASSWORD}" <<'PY'
import json, sys
print(json.dumps({"Name": sys.argv[1], "Password": sys.argv[2]}, separators=(",", ":")))
PY
)"
    response="$(curl --fail --silent --show-error \
        -X POST "${UPGRADE_ORIGIN}/Users/New" \
        -H 'Content-Type: application/json' -H "${auth}" --data "${payload}")" || return $?
    viewer_id="$(printf '%s' "${response}" | json_field Id)"
    [[ "${viewer_id}" =~ ^[0-9a-fA-F-]{32,36}$ ]] || {
        printf 'FATAL: create-user response has no usable viewer id\n' >&2
        return 1
    }
    authenticate_to_files "${VIEWER_USER}" "${VIEWER_PASSWORD}" "${viewer_token_file}" "${viewer_id_file}" || return $?
    [ "$(<"${viewer_id_file}")" = "${viewer_id}" ] || {
        printf 'FATAL: authenticated viewer identity differs from created user\n' >&2
        return 1
    }
}

install_stage() {
    local image="$1" stage="$2" container host_sha remote_sha
    container="$(upgrade_container_id "${image}")" || return $?
    [ -f "${stage}/Jellyfin.Plugin.RefreshKit.dll" ] || {
        printf 'FATAL: missing stage DLL: %s\n' "${stage}" >&2
        return 1
    }
    docker exec "${container}" sh -c 'rm -rf -- "$1" && mkdir -p -- "$1"' sh "${PLUGIN_DIR}" || return $?
    docker cp "${stage}/." "${container}:${PLUGIN_DIR}/" >/dev/null || return $?
    host_sha="$(sha256sum "${stage}/Jellyfin.Plugin.RefreshKit.dll" | awk '{print $1}')"
    remote_sha="$(docker exec "${container}" sha256sum "${PLUGIN_DIR}/Jellyfin.Plugin.RefreshKit.dll" | awk '{print $1}')" || return $?
    [ "${host_sha}" = "${remote_sha}" ] || {
        printf 'FATAL: staged plugin DLL checksum mismatch\n' >&2
        return 1
    }
}

restart_and_wait() {
    local image="$1" container
    container="$(upgrade_container_id "${image}")" || return $?
    docker restart "${container}" >/dev/null || return $?
    rk_wait_http "${UPGRADE_ORIGIN}/System/Info/Public" 200 180 2
}

configure_plugin() {
    local token="$1" auth config last current stable=0
    auth="$(rk_auth_header "${token}")"
    rk_wait_http "${UPGRADE_ORIGIN}/RefreshKit/Generation" 200 120 2 || return $?
    config='{"EnableInjection":true,"EnableThirdPartyStamping":true,"EnableAutoReload":true,"PollSeconds":5,"IdleSeconds":0,"ReloadBudget":10,"EnableConfigWatching":true,"ConfigWatchExclusions":[],"ConfigCooldownMinutes":0,"DevMode":false}'
    curl --fail --silent --show-error -o /dev/null \
        -X POST "${UPGRADE_ORIGIN}/Plugins/${RK_PLUGIN_GUID}/Configuration" \
        -H 'Content-Type: application/json' -H "${auth}" --data "${config}" || return $?
    last="$(curl --fail --silent --show-error "${UPGRADE_ORIGIN}/RefreshKit/Generation.txt")" || return $?
    for _ in $(seq 1 18); do
        sleep 5
        current="$(curl --fail --silent --show-error "${UPGRADE_ORIGIN}/RefreshKit/Generation.txt")" || return $?
        if [ "${current}" = "${last}" ]; then
            stable=$((stable + 1))
        else
            last="${current}"
            stable=0
        fi
        [ "${stable}" -ge 3 ] && return 0
    done
    printf 'FATAL: plugin generation did not settle after configuration\n' >&2
    return 1
}

validate_snapshot_stages() {
    local version10 version12
    python3 - "${RK_STAGE_JF10}/meta.json" "${RK_STAGE_JF12}/meta.json" <<'PY'
import json, pathlib, re, sys
p10, p12 = map(pathlib.Path, sys.argv[1:])
for path in (p10, p12):
    if not path.is_file():
        raise SystemExit(f"FATAL: missing immutable stage metadata: {path}")
m10, m12 = (json.loads(path.read_text(encoding="utf-8")) for path in (p10, p12))
expected = ((m10, "10.11.0.0", "net9.0"), (m12, "12.0.0.0", "net10.0"))
for meta, abi, framework in expected:
    if (meta.get("targetAbi"), meta.get("framework")) != (abi, framework):
        raise SystemExit(f"FATAL: wrong immutable stage identity: {meta!r}")
if m10.get("version") != m12.get("version") or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", str(m10.get("version", ""))):
    raise SystemExit("FATAL: net9/net10 stages do not share one four-component version")
for key in ("sourceRevision", "sourceTreeSha256", "sourceDateEpoch"):
    if m10.get(key) != m12.get(key):
        raise SystemExit(f"FATAL: net9/net10 stages disagree on {key}")
if not isinstance(m10.get("sourceDirty"), bool) or m10.get("sourceDirty") != m12.get("sourceDirty"):
    raise SystemExit("FATAL: net9/net10 stages disagree on source-dirty identity")
print(m10["version"])
PY
    version10="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${RK_STAGE_JF10}/meta.json")"
    version12="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${RK_STAGE_JF12}/meta.json")"
    [ "${version10}" = "${version12}" ] || return 1
    PLUGIN_VERSION="${version10}"
    PLUGIN_DIR="/config/plugins/Jellyfin Refresh Kit_${PLUGIN_VERSION}"
}

write_metadata() {
    local scenario="$1" from_image="$2" to_image="$3" from_version="$4" to_version="$5"
    local admin_id_file="$6" viewer_id_file="$7" path="$8"
    python3 - "${path}" "${scenario}" "${from_image}" "${to_image}" \
        "${from_version}" "${to_version}" "${RK_BUILD_SNAPSHOT}" \
        "${RK_STAGE_JF10}/meta.json" "${RK_STAGE_JF12}/meta.json" \
        "${RK_STAGE_JF10}/Jellyfin.Plugin.RefreshKit.dll" \
        "${RK_STAGE_JF12}/Jellyfin.Plugin.RefreshKit.dll" \
        "${admin_id_file}" "${viewer_id_file}" "${PLUGIN_DIR}" \
        "${RK_LAB_USER}" "${VIEWER_USER}" <<'PY'
import hashlib, json, pathlib, sys
(
    output, scenario, from_image, to_image, from_version, to_version,
    snapshot, meta10_path, meta12_path, dll10_path, dll12_path,
    admin_id_path, viewer_id_path, plugin_dir, admin_name, viewer_name,
) = sys.argv[1:]
def read_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
def package_record(path):
    payload = pathlib.Path(path).read_bytes()
    return {
        "file": pathlib.Path(path).name,
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "md5": hashlib.md5(payload, usedforsecurity=False).hexdigest(),
    }
m10 = read_json(meta10_path)
m12 = read_json(meta12_path)
version = str(m10["version"])
snapshot_path = pathlib.Path(snapshot)
package10 = snapshot_path / f"jellyfin-refresh-kit_{version}.zip"
package12 = snapshot_path / f"jellyfin-refresh-kit_{version}_jf12.zip"
if not package10.is_file() or not package12.is_file():
    raise SystemExit("FATAL: selected immutable snapshot is missing one target package")
data = {
    "schemaVersion": 1,
    "scenario": scenario,
    "from": {"image": from_image, "serverVersion": from_version},
    "to": {"image": to_image, "serverVersion": to_version},
    "immutableSnapshot": pathlib.Path(snapshot).name,
    "sourceIdentity": {
        "revision": m10["sourceRevision"],
        "treeSha256": m10["sourceTreeSha256"],
        "dateEpoch": m10["sourceDateEpoch"],
        "dirty": m10.get("sourceDirty"),
    },
    "stages": {
        "net9": {"meta": m10, "dllSha256": sha(dll10_path), "package": package_record(package10)},
        "net10": {"meta": m12, "dllSha256": sha(dll12_path), "package": package_record(package12)},
    },
    "users": {
        "admin": {"name": admin_name, "id": pathlib.Path(admin_id_path).read_text().strip()},
        "viewer": {"name": viewer_name, "id": pathlib.Path(viewer_id_path).read_text().strip()},
    },
    "pluginDirectory": plugin_dir,
}
pathlib.Path(output).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

write_setup_failure() {
    local result="$1" scenario="$2" message="$3"
    [ -s "${result}" ] && return 0
    python3 - "${result}" "${scenario}" "${message}" <<'PY'
import datetime, json, pathlib, sys
path, scenario, message = sys.argv[1:]
data = {
    "schemaVersion": 2,
    "scenario": scenario,
    "completed": False,
    "failures": [message],
    "finishedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
}
pathlib.Path(path).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

run_scenario() {
    local scenario="$1" from_image to_image from_version to_version out state_prefix
    local admin_token viewer_token admin_id viewer_id metadata container started result rc=0
    case "${scenario}" in
        jf10)
            from_image="${JF101110_IMAGE}"; to_image="${JF101111_IMAGE}"
            from_version='10.11.10'; to_version='10.11.11'
            ;;
        jf12)
            from_image="${JF101111_IMAGE}"; to_image="${JF12RC4_IMAGE}"
            from_version='10.11.11'; to_version='12.0.0'
            ;;
        *) printf 'FATAL: unknown host-upgrade scenario %s\n' "${scenario}" >&2; return 2 ;;
    esac

    out="${RK_ARTIFACT_DIR}/host-upgrade/${scenario}"
    state_prefix="${RK_STATE_DIR}/host-upgrade-${scenario}"
    admin_token="${state_prefix}.admin.token"
    viewer_token="${state_prefix}.viewer.token"
    admin_id="${state_prefix}.admin.id"
    viewer_id="${state_prefix}.viewer.id"
    metadata="${state_prefix}.metadata.json"
    result="${out}/result.json"
    if [ -d "${out}" ]; then
        mv -- "${out}" "${out}.previous-$(date -u +%Y%m%dT%H%M%SZ)-$$" || return $?
    fi
    mkdir -p "${out}"
    rm -f -- "${admin_token}" "${viewer_token}" "${admin_id}" "${viewer_id}" "${metadata}"
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    rk_log "${scenario}: resetting the dedicated host-upgrade service storage"

    if ! reset_upgrade_storage "${from_image}"; then
        write_setup_failure "${result}" "${scenario}" 'could not reset dedicated host-upgrade storage'
        return 1
    fi
    if ! start_exact_image "${from_image}"; then
        write_setup_failure "${result}" "${scenario}" 'could not start the exact pinned baseline image'
        return 1
    fi
    if ! ensure_exact_image "${to_image}"; then
        write_setup_failure "${result}" "${scenario}" 'could not preflight the exact pinned target image'
        rc=1
    elif ! initialize_server "${from_version}"; then
        write_setup_failure "${result}" "${scenario}" 'could not initialize the exact baseline server'
        rc=1
    elif ! authenticate_to_files "${RK_LAB_USER}" "${RK_LAB_PASSWORD}" "${admin_token}" "${admin_id}"; then
        write_setup_failure "${result}" "${scenario}" 'admin authentication failed during baseline provisioning'
        rc=1
    elif ! create_viewer "$(<"${admin_token}")" "${viewer_token}" "${viewer_id}"; then
        write_setup_failure "${result}" "${scenario}" 'distinct viewer provisioning/authentication failed'
        rc=1
    elif ! install_stage "${from_image}" "${RK_STAGE_JF10}"; then
        write_setup_failure "${result}" "${scenario}" 'net9 plugin staging failed'
        rc=1
    elif ! restart_and_wait "${from_image}"; then
        write_setup_failure "${result}" "${scenario}" 'baseline restart after plugin staging failed'
        rc=1
    elif ! authenticate_to_files "${RK_LAB_USER}" "${RK_LAB_PASSWORD}" "${admin_token}" "${admin_id}"; then
        write_setup_failure "${result}" "${scenario}" 'admin reauthentication after plugin restart failed'
        rc=1
    elif ! configure_plugin "$(<"${admin_token}")"; then
        write_setup_failure "${result}" "${scenario}" 'baseline plugin configuration did not settle'
        rc=1
    elif ! write_metadata "${scenario}" "${from_image}" "${to_image}" \
        "${from_version}" "${to_version}" "${admin_id}" "${viewer_id}" "${metadata}"; then
        write_setup_failure "${result}" "${scenario}" 'could not write non-secret scenario metadata'
        rc=1
    else
        container="$(upgrade_container_id "${from_image}")"
        if ! RK_LAB_USER="${RK_LAB_USER}" RK_LAB_PASSWORD="${RK_LAB_PASSWORD}" \
            RK_LAB_VIEWER_USER="${VIEWER_USER}" RK_LAB_VIEWER_PASSWORD="${VIEWER_PASSWORD}" \
            node "${HERE}/host-upgrade-browser.cjs" \
            --scenario "${scenario}" --origin "${UPGRADE_ORIGIN}" \
            --project "${RK_PROJECT}" --service "${UPGRADE_SERVICE}" \
            --compose "${RK_COMPOSE_FILE}" --container "${container}" \
            --from-image "${from_image}" --to-image "${to_image}" \
            --admin-token-file "${admin_token}" --viewer-token-file "${viewer_token}" \
            --metadata "${metadata}" --net9-stage "${RK_STAGE_JF10}" \
            --net10-stage "${RK_STAGE_JF12}" --out "${out}"; then
            rc=1
        elif ! python3 "${HERE}/verify-host-upgrade-results.py" \
            --root "${RK_ARTIFACT_DIR}/host-upgrade" \
            --snapshot "$(basename "${RK_BUILD_SNAPSHOT}")" \
            --scenario "${scenario}"; then
            rc=1
        fi
    fi

    container="$(upgrade_compose "${to_image}" --profile host-upgrade ps --all -q "${UPGRADE_SERVICE}" 2>/dev/null || true)"
    if [ -z "${container}" ]; then
        container="$(upgrade_compose "${from_image}" --profile host-upgrade ps --all -q "${UPGRADE_SERVICE}" 2>/dev/null || true)"
    fi
    if [ -n "${container}" ]; then
        docker logs --since "${started}" "${container}" > "${out}/server.log" 2>&1 || true
        docker inspect "${container}" > "${out}/container.inspect.json" 2>/dev/null || true
    fi
    write_setup_failure "${result}" "${scenario}" 'host-upgrade browser runner ended without structured result'
    return "${rc}"
}

write_aggregate() {
    local path="${RK_ARTIFACT_DIR}/host-upgrade/result.json"
    if [ -f "${path}" ]; then
        mv -- "${path}" "${RK_ARTIFACT_DIR}/host-upgrade/result.previous-$(date -u +%Y%m%dT%H%M%SZ)-$$.json" \
            || return $?
    fi
    python3 - "${path}" \
        "${RK_ARTIFACT_DIR}/host-upgrade/jf10/result.json" \
        "${RK_ARTIFACT_DIR}/host-upgrade/jf12/result.json" \
        "$(basename "${RK_BUILD_SNAPSHOT}")" <<'PY'
import datetime, json, pathlib, sys
output = pathlib.Path(sys.argv[1])
paths = [pathlib.Path(value) for value in sys.argv[2:4]]
expected_snapshot = sys.argv[4]
scenarios = {}
failures = []
source_identities = []
for path in paths:
    name = path.parent.name
    if not path.is_file():
        failures.append(f"{name}: result missing")
        scenarios[name] = {"result": f"{name}/result.json", "completed": False}
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    metadata = data.get("metadata") or {}
    source_identities.append(metadata.get("sourceIdentity"))
    completed = (
        data.get("completed") is True
        and data.get("failures") == []
        and metadata.get("immutableSnapshot") == expected_snapshot
    )
    scenarios[name] = {"result": f"{name}/result.json", "completed": completed}
    if not completed:
        failures.append(f"{name}: scenario did not complete cleanly for {expected_snapshot}")
if len(source_identities) == 2 and source_identities[0] != source_identities[1]:
    failures.append("jf10/jf12 source identities differ")
result = {
    "schemaVersion": 1,
    "completed": len(failures) == 0,
    "immutableSnapshot": expected_snapshot,
    "sourceIdentity": source_identities[0] if source_identities else None,
    "scenarios": scenarios,
    "failures": failures,
    "finishedUtc": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
}
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

main() {
    local target="${1:-}" rc=0
    case "${target}" in
        negative)
            exec bash "${HERE}/host-upgrade-negative.sh"
            ;;
        jf10|jf12|all) ;;
        *) usage >&2; return 2 ;;
    esac

    rk_require curl docker ffmpeg node python3 sha256sum timeout
    docker info >/dev/null 2>&1 || rk_die 'Docker daemon is unavailable'
    docker compose version >/dev/null 2>&1 || rk_die 'Docker Compose is unavailable'
    rk_pin_build_snapshot
    validate_snapshot_stages >/dev/null

    case "${target}" in
        jf10|jf12)
            run_scenario "${target}" || rc=1
            ;;
        all)
            run_scenario jf10 || rc=1
            run_scenario jf12 || rc=1
            write_aggregate || rc=1
            python3 "${HERE}/verify-host-upgrade-results.py" \
                --root "${RK_ARTIFACT_DIR}/host-upgrade" \
                --snapshot "$(basename "${RK_BUILD_SNAPSHOT}")" \
                --scenario all || rc=1
            ;;
    esac
    return "${rc}"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
