#!/usr/bin/env bash

# Initialize one fresh Jellyfin service, install a selected stage, and configure
# the matching plugin. Usage: provision.sh jf10|jf12|abi-floor [jf10|jf12]
# The optional second argument selects the stage, enabling the jf10-on-jf12 leg.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
# Resolved from this script's directory at runtime.
# shellcheck disable=SC1091
source "${HERE}/common.sh"

TARGET="${1:-}"
STAGE_FLAVOR="${2:-${TARGET}}"
case "${TARGET}" in
    jf10|jf12|abi-floor) ;;
    *) rk_die "usage: provision.sh jf10|jf12|abi-floor [jf10|jf12]" ;;
esac
case "${STAGE_FLAVOR}" in jf10|jf12) ;; *) rk_die "unknown stage flavor ${STAGE_FLAVOR}" ;; esac

# Direct invocations and both parallel provisioning children resolve the same
# parent-selected immutable build rather than re-reading the mutable public link.
rk_pin_build_snapshot

ORIGIN="$(rk_origin "${TARGET}")"
STAGE="$(rk_stage "${STAGE_FLAVOR}")"
TOKEN_FILE="$(rk_token_file "${TARGET}")"

rk_require curl docker python3
[ -f "${STAGE}/Jellyfin.Plugin.RefreshKit.dll" ] || \
    rk_die "missing ${STAGE}/Jellyfin.Plugin.RefreshKit.dll; run ./run.sh build"
[ -f "${STAGE}/meta.json" ] || rk_die "missing ${STAGE}/meta.json"

readarray -t STAGE_META < <(python3 - "${STAGE}/meta.json" "${STAGE_FLAVOR}" <<'PY'
import json
import sys

path, flavor = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    meta = json.load(handle)
expected = {
    "jf10": ("10.11.0.0", "net9.0"),
    "jf12": ("12.0.0.0", "net10.0"),
}[flavor]
actual = (str(meta.get("targetAbi", "")), str(meta.get("framework", "")))
if actual != expected:
    raise SystemExit(
        f"FATAL: {path} is {actual[0]}/{actual[1]}, expected {expected[0]}/{expected[1]}"
    )
print(meta["version"])
print(actual[0])
print(actual[1])
PY
)
PLUGIN_VERSION="${STAGE_META[0]}"
[[ "${PLUGIN_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || \
    rk_die "refusing unsafe plugin version from ${STAGE}/meta.json: ${PLUGIN_VERSION}"

json_field() {
    local field="$1"
    python3 -c '
import json
import sys
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
    for _ in $(seq 1 20); do
        if curl --fail --silent --show-error --connect-timeout 2 --max-time 10 "$@"; then
            return 0
        fi
        sleep 1
    done
    rk_die "${TARGET}: startup wizard ${label} did not succeed after 20 attempts"
}

authenticate() {
    local payload response token
    payload="$(python3 - "${RK_LAB_USER}" "${RK_LAB_PASSWORD}" <<'PY'
import json
import sys
print(json.dumps({"Username": sys.argv[1], "Pw": sys.argv[2]}, separators=(",", ":")))
PY
)"
    for _ in $(seq 1 90); do
        response="$(curl --silent --show-error --connect-timeout 2 --max-time 5 \
            -X POST "${ORIGIN}/Users/AuthenticateByName" \
            -H 'Content-Type: application/json' \
            -H "$(rk_no_token_auth_header)" \
            --data "${payload}" 2>/dev/null || true)"
        token="$(printf '%s' "${response}" | json_field AccessToken)"
        if [ -n "${token}" ]; then
            umask 077
            printf '%s' "${token}" > "${TOKEN_FILE}"
            printf '%s\n' "${token}"
            return 0
        fi
        sleep 1
    done
    rk_die "${TARGET}: authentication returned no access token for 90 seconds"
}

rk_log "${TARGET}: waiting for pristine server at ${ORIGIN}"
rk_wait_http "${ORIGIN}/System/Info/Public" 200 120 2
rk_assert_container_image "${TARGET}"

PUBLIC_INFO=''
SERVER_VERSION=''
for ((info_attempt = 1; info_attempt <= 30; info_attempt++)); do
    if candidate_info="$(curl --fail --silent --show-error "${ORIGIN}/System/Info/Public" 2>/dev/null)"; then
        candidate_version="$(printf '%s' "${candidate_info}" | json_field Version)"
        if [ -n "${candidate_version}" ]; then
            PUBLIC_INFO="${candidate_info}"
            SERVER_VERSION="${candidate_version}"
            break
        fi
    fi
    sleep 1
done
[ -n "${SERVER_VERSION}" ] || rk_die "${TARGET}: public server info did not stabilize after 30 seconds"
EXPECTED_VERSION_PATTERN="$(rk_expected_version_pattern "${TARGET}")"
printf '%s' "${SERVER_VERSION}" | grep -Eq "${EXPECTED_VERSION_PATTERN}" || \
    rk_die "${TARGET}: server reports ${SERVER_VERSION}, expected ${EXPECTED_VERSION_PATTERN}"
rk_log "${TARGET}: pinned server reports ${SERVER_VERSION}"

WIZARD_DONE="$(printf '%s' "${PUBLIC_INFO}" | json_field StartupWizardCompleted)"
if [ "${WIZARD_DONE,,}" != "true" ]; then
    rk_log "${TARGET}: completing startup wizard"
    # Public info can turn healthy while first-run database migrations are
    # still finishing. Prove the wizard itself is responsive, then tolerate
    # bounded connection resets; these startup operations are idempotent.
    rk_wait_http "${ORIGIN}/Startup/User" 200 120 1
    wizard_request configuration -o /dev/null \
        -X POST "${ORIGIN}/Startup/Configuration" \
        -H 'Content-Type: application/json' \
        --data '{"UICulture":"en-US","MetadataCountryCode":"US","PreferredMetadataLanguage":"en"}'
    wizard_request user-query -o /dev/null "${ORIGIN}/Startup/User"
    USER_PAYLOAD="$(python3 - "${RK_LAB_USER}" "${RK_LAB_PASSWORD}" <<'PY'
import json
import sys
print(json.dumps({"Name": sys.argv[1], "Password": sys.argv[2]}, separators=(",", ":")))
PY
)"
    wizard_request user-create -o /dev/null \
        -X POST "${ORIGIN}/Startup/User" \
        -H 'Content-Type: application/json' \
        --data "${USER_PAYLOAD}"
    wizard_request remote-access -o /dev/null \
        -X POST "${ORIGIN}/Startup/RemoteAccess" \
        -H 'Content-Type: application/json' \
        --data '{"EnableRemoteAccess":true,"EnableAutomaticPortMapping":false}'
    wizard_request completion -o /dev/null \
        -X POST "${ORIGIN}/Startup/Complete"
    rk_wait_http "${ORIGIN}/System/Info/Public" 200 60 1
fi

TOKEN="$(authenticate)"

if [ "${RK_PROVISION_SKIP_PLUGIN:-0}" = "1" ]; then
    rk_log "${TARGET}: pristine authenticated server ready without Refresh Kit"
    exit 0
fi

CONTAINER="$(rk_container_id "${TARGET}")"
REMOTE_PLUGIN_DIR="/config/plugins/Jellyfin Refresh Kit_${PLUGIN_VERSION}"

rk_log "${TARGET}: installing ${STAGE_FLAVOR} stage ${PLUGIN_VERSION} (${STAGE_META[1]}/${STAGE_META[2]})"
docker exec "${CONTAINER}" sh -c 'rm -rf -- "$1" && mkdir -p -- "$1"' sh "${REMOTE_PLUGIN_DIR}"
docker cp "${STAGE}/." "${CONTAINER}:${REMOTE_PLUGIN_DIR}/" >/dev/null

HOST_DLL_SHA="$(sha256sum "${STAGE}/Jellyfin.Plugin.RefreshKit.dll" | awk '{print $1}')"
CONTAINER_DLL_SHA="$(docker exec "${CONTAINER}" sha256sum "${REMOTE_PLUGIN_DIR}/Jellyfin.Plugin.RefreshKit.dll" | awk '{print $1}')"
[ "${HOST_DLL_SHA}" = "${CONTAINER_DLL_SHA}" ] || rk_die "${TARGET}: copied DLL checksum mismatch"

rk_log "${TARGET}: restarting with installed plugin"
if [ "${TARGET}" = "abi-floor" ]; then
    rk_compose --profile abi-floor restart "${TARGET}" >/dev/null
else
    rk_compose restart "${TARGET}" >/dev/null
fi
if ! rk_wait_http "${ORIGIN}/System/Info/Public" 200 120 2; then
    if [ "${TARGET}" = "jf12" ] && [ "${STAGE_FLAVOR}" = "jf10" ]; then
        rk_die "${TARGET}: server did not recover after loading the cross-generation stage"
    fi
    rk_die "${TARGET}: server did not recover after plugin installation"
fi

# A restart preserves users but invalidates nothing important; authenticate
# again so every subsequent command has a token proven against this process.
TOKEN="$(authenticate)"

if [ "${TARGET}" = "${STAGE_FLAVOR}" ] \
    || { [ "${TARGET}" = "abi-floor" ] && [ "${STAGE_FLAVOR}" = "jf10" ]; }; then
    rk_wait_http "${ORIGIN}/RefreshKit/Generation" 200 120 2 || {
        docker logs --tail 200 "${CONTAINER}" >&2 || true
        rk_die "${TARGET}: matching Refresh Kit stage did not expose its endpoint"
    }

    CONFIG='{"EnableInjection":true,"EnableThirdPartyStamping":true,"EnableAutoReload":true,"PollSeconds":15,"IdleSeconds":0,"ReloadBudget":10,"EnableConfigWatching":true,"ConfigWatchExclusions":[],"ConfigCooldownMinutes":0,"DevMode":false}'
    curl --fail --silent --show-error -o /dev/null \
        -X POST "${ORIGIN}/Plugins/${RK_PLUGIN_GUID}/Configuration" \
        -H 'Content-Type: application/json' \
        -H "$(rk_auth_header "${TOKEN}")" \
        --data "${CONFIG}"

    # The provider discovers a configuration write on its next TTL scan, then
    # starts a ten-second debounce. Merely sleeping from the POST is therefore
    # insufficient: the first read after that sleep would only START the gate.
    # Read immediately, then require four identical five-second observations;
    # any debounced publication resets the stability window.
    LAST_GENERATION="$(curl --fail --silent --show-error "${ORIGIN}/RefreshKit/Generation.txt")"
    STABLE_READS=0
    for _ in $(seq 1 18); do
        sleep 5
        CURRENT_GENERATION="$(curl --fail --silent --show-error "${ORIGIN}/RefreshKit/Generation.txt")"
        if [ "${CURRENT_GENERATION}" = "${LAST_GENERATION}" ]; then
            STABLE_READS=$((STABLE_READS + 1))
        else
            LAST_GENERATION="${CURRENT_GENERATION}"
            STABLE_READS=0
        fi
        [ "${STABLE_READS}" -ge 3 ] && break
    done
    [ "${STABLE_READS}" -ge 3 ] || rk_die "${TARGET}: generation did not settle after configuration"
    rk_log "${TARGET}: matching stage ready at stable generation ${LAST_GENERATION}"
else
    # In the deliberate jf10-on-jf12 experiment the absence of this endpoint is
    # an outcome, not a provisioning failure. The compatibility driver records
    # the endpoint, plugin inventory, logs and shell as one classification.
    sleep 5
    rk_log "${TARGET}: cross-generation stage installed; endpoint status $(rk_http_code "${ORIGIN}/RefreshKit/Generation")"
fi
