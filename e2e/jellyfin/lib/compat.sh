#!/usr/bin/env bash

# Deliberately put the net9/Jellyfin-10 stage into a pristine Jellyfin 12
# instance, classify whether it loads, save all evidence, then restore the
# matching net10 stage unless RK_COMPAT_KEEP_CROSS=1.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${HERE}/common.sh"

rk_require curl docker python3
[ -f "${RK_STAGE_JF10}/Jellyfin.Plugin.RefreshKit.dll" ] || \
    rk_die "missing jf10 stage; run ./run.sh build"

OUT="${RK_ARTIFACT_DIR}/compat-jf10-on-jf12"
mkdir -p "${OUT}"
STARTED_UTC="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
TOKEN_FILE="$(rk_token_file jf12)"

rk_log "compat: resetting only the project-scoped jf12 service and volumes"
rk_reset_service_storage jf12
rm -f -- "${TOKEN_FILE}"
rk_compose up -d --wait jf12
rk_assert_container_image jf12

set +e
bash "${HERE}/provision.sh" jf12 jf10 2>&1 | tee "${OUT}/provision.log"
PROVISION_RC="${PIPESTATUS[0]}"
set -e

CONTAINER="$(rk_compose ps --all --quiet jf12 | head -n 1)"
[ -n "${CONTAINER}" ] || rk_die "compat: jf12 container disappeared before logs could be captured"
docker logs --since "${STARTED_UTC}" "${CONTAINER}" > "${OUT}/server.log" 2>&1 || true

PUBLIC_STATUS="$(rk_http_code "${RK_JF12_ORIGIN}/System/Info/Public")"
GENERATION_STATUS="$(rk_http_code "${RK_JF12_ORIGIN}/RefreshKit/Generation")"
KIT_STATUS="$(rk_http_code "${RK_JF12_ORIGIN}/RefreshKit/kit.js")"
SHELL_STATUS="$(rk_http_code "${RK_JF12_ORIGIN}/web/index.html")"

if [ "${PUBLIC_STATUS}" = "200" ]; then
    curl --silent --show-error "${RK_JF12_ORIGIN}/System/Info/Public" > "${OUT}/public.json" || true
fi
if [ "${GENERATION_STATUS}" = "200" ]; then
    curl --silent --show-error "${RK_JF12_ORIGIN}/RefreshKit/Generation" > "${OUT}/generation.json" || true
fi
if [ "${SHELL_STATUS}" = "200" ]; then
    curl --silent --show-error "${RK_JF12_ORIGIN}/web/index.html" > "${OUT}/index.html" || true
fi

INVENTORY_FOUND=false
PLUGIN_STATUS=''
if [ "${PUBLIC_STATUS}" = "200" ] && [ -s "${TOKEN_FILE}" ]; then
    TOKEN="$(<"${TOKEN_FILE}")"
    if curl --fail --silent --show-error \
        -H "$(rk_auth_header "${TOKEN}")" \
        "${RK_JF12_ORIGIN}/Plugins" > "${OUT}/plugins.json"; then
        readarray -t INVENTORY < <(python3 - "${OUT}/plugins.json" "${RK_PLUGIN_GUID}" "${OUT}/plugin-record.json" <<'PY'
import json
import sys

path, guid, result_path = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    root = json.load(handle)

def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)

record = None
for item in walk(root):
    item_id = str(item.get("Id", item.get("id", ""))).lower()
    item_name = str(item.get("Name", item.get("name", ""))).lower()
    if item_id.replace("-", "") == guid.lower().replace("-", "") or item_name == "jellyfin refresh kit":
        record = item
        break

with open(result_path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(record, handle, indent=2, sort_keys=True)
    handle.write("\n")
print("true" if record is not None else "false")
if record is None:
    print("")
else:
    print(record.get("Status", record.get("status", "")))
PY
)
        INVENTORY_FOUND="${INVENTORY[0]:-false}"
        PLUGIN_STATUS="${INVENTORY[1]:-}"
    fi
fi

SHELL_INJECTED=false
if [ -s "${OUT}/index.html" ] && grep -Fq 'plugin="Jellyfin Refresh Kit"' "${OUT}/index.html"; then
    SHELL_INJECTED=true
fi

LOADED=false
if [ "${GENERATION_STATUS}" = "200" ] && [ "${KIT_STATUS}" = "200" ] && \
   [ "${SHELL_INJECTED}" = true ]; then
    LOADED=true
fi

# The pinned RC4 experiment currently demonstrates that the net9/JF10 build
# loads coherently. Keep that discovery as the default upgrade-path contract;
# `auto` remains available when intentionally classifying a future host first.
EXPECTED="${RK_EXPECT_JF10_ON_JF12:-load}"
case "${EXPECTED}" in auto|load|reject) ;; *) rk_die "RK_EXPECT_JF10_ON_JF12 must be auto, load or reject" ;; esac

python3 - "${OUT}/result.json" "${STARTED_UTC}" "${PROVISION_RC}" \
    "${PUBLIC_STATUS}" "${GENERATION_STATUS}" "${KIT_STATUS}" "${SHELL_STATUS}" \
    "${SHELL_INJECTED}" "${INVENTORY_FOUND}" "${PLUGIN_STATUS}" "${LOADED}" \
    "${EXPECTED}" "${RK_JF12_DIGEST}" <<'PY'
import json
import sys

(path, started, provision_rc, public, generation, kit, shell, shell_injected,
 inventory, plugin_status, loaded, expected, digest) = sys.argv[1:]
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump({
        "experiment": "net9/Jellyfin-10 build manually installed on Jellyfin 12",
        "startedUtc": started,
        "hostImageDigest": digest,
        "stageFramework": "net9.0",
        "stageTargetAbi": "10.11.0.0",
        "provisionExitCode": int(provision_rc),
        "http": {
            "public": int(public or 0),
            "generation": int(generation or 0),
            "kit": int(kit or 0),
            "shell": int(shell or 0),
        },
        "shellInjected": shell_injected == "true",
        "pluginInventoryFound": inventory == "true",
        "pluginInventoryStatus": plugin_status,
        "loaded": loaded == "true",
        "expected": expected,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

RESULT=0
if [ "${PROVISION_RC}" -ne 0 ]; then
    printf 'FAIL compat: cross-stage provisioning exited %s\n' "${PROVISION_RC}" >&2
    RESULT=1
elif [ "${PUBLIC_STATUS}" != "200" ]; then
    printf 'FAIL compat: Jellyfin 12 did not stay healthy (public status %s)\n' "${PUBLIC_STATUS}" >&2
    RESULT=1
elif [ "${LOADED}" = true ] && [ "${INVENTORY_FOUND}" != true ]; then
    printf 'FAIL compat: endpoints/shell loaded but plugin inventory has no Refresh Kit record\n' >&2
    RESULT=1
elif [ "${LOADED}" != true ] && { [ "${GENERATION_STATUS}" = "200" ] || \
     [ "${KIT_STATUS}" = "200" ] || [ "${SHELL_INJECTED}" = true ]; }; then
    printf 'FAIL compat: partial load (endpoint/shell evidence does not agree)\n' >&2
    RESULT=1
elif [ "${EXPECTED}" = load ] && [ "${LOADED}" != true ]; then
    printf 'FAIL compat: expected the net9 build to load on Jellyfin 12, but it was rejected\n' >&2
    RESULT=1
elif [ "${EXPECTED}" = reject ] && [ "${LOADED}" = true ]; then
    printf 'FAIL compat: expected the net9 build to be rejected by Jellyfin 12, but it loaded\n' >&2
    RESULT=1
fi

if [ "${LOADED}" = true ]; then
    rk_log "compat: net9/JF10 stage LOADED on Jellyfin 12 (inventory status ${PLUGIN_STATUS:-unknown})"
else
    rk_log "compat: net9/JF10 stage REJECTED on Jellyfin 12 (inventory ${INVENTORY_FOUND}, status ${PLUGIN_STATUS:-absent})"
fi
rk_log "compat: evidence written to ${OUT}/result.json and server.log"

if [ "${RK_COMPAT_KEEP_CROSS:-0}" != "1" ]; then
    rk_log "compat: restoring pristine jf12 service with the matching net10 stage"
    set +e
    rk_reset_service_storage jf12
    rm -f -- "${TOKEN_FILE}"
    rk_compose up -d --wait jf12
    bash "${HERE}/provision.sh" jf12 jf12
    RESTORE_RC=$?
    if [ "${RESTORE_RC}" -eq 0 ]; then
        bash "${HERE}/check.sh" jf12
        RESTORE_RC=$?
    fi
    set -e
    if [ "${RESTORE_RC}" -ne 0 ]; then
        printf 'FAIL compat: could not restore matching Jellyfin 12 lab after experiment\n' >&2
        RESULT=1
    fi
fi

exit "${RESULT}"
