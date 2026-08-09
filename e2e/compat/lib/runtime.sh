#!/usr/bin/env bash

# Run one fresh compatibility matrix. The public entry point is ../run.sh.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The path is resolved from this file, not the caller's working directory.
# shellcheck disable=SC1091
source "${HERE}/common.sh"
compat_pin_build_snapshot

MATRIX_ID="${1:-}"
[ -n "${MATRIX_ID}" ] || compat_die "usage: runtime.sh MATRIX_ID"

MANIFEST_TOOL="${HERE}/manifest.py"
ARTIFACT_TOOL="${HERE}/artifacts.py"
ANALYZE_TOOL="${HERE}/analyze.py"
RUNTIME="$(python3 "${MANIFEST_TOOL}" field "${MATRIX_ID}" runtime)"
SERVICE="$(python3 "${MANIFEST_TOOL}" field "${MATRIX_ID}" service)"
ORIGIN=""
ORIGIN_MODE=""
STAGE="$(compat_stage "${RUNTIME}")"
OUT="${RK_COMPAT_ARTIFACT_DIR}/${MATRIX_ID}"
WORK="${RK_COMPAT_STATE_DIR}/${MATRIX_ID}"
RESULT="${OUT}/result.json"
PHASE="initialization"

if [ -e "${OUT}" ] || [ -e "${WORK}" ]; then
    compat_die "${MATRIX_ID}: generated state already exists; run ./run.sh clean before rerunning"
fi
mkdir -p "${OUT}/materialized" "${OUT}/runtime-meta" "${WORK}/install"

capture_failure() {
    local exit_code=$?
    if [ "${exit_code}" -eq 0 ]; then
        return
    fi
    set +e
    if command -v docker >/dev/null 2>&1; then
        compat_compose logs --no-color "${SERVICE}" > "${OUT}/server.log" 2>&1
    fi
    if [ ! -f "${RESULT}" ]; then
        python3 "${ANALYZE_TOOL}" failure "${MATRIX_ID}" "${PHASE}" \
            "matrix command exited with status ${exit_code}" "${RESULT}" >/dev/null 2>&1
    fi
    compat_log "${MATRIX_ID}: failed during ${PHASE}; evidence retained in ${OUT}"
    exit "${exit_code}"
}
trap capture_failure EXIT

json_field() {
    local field="$1"
    python3 -c '
import json
import sys
try:
    data = json.load(sys.stdin)
    wanted = sys.argv[1].casefold()
    for key, value in data.items():
        if key.casefold() == wanted:
            print(value)
            break
except Exception:
    pass
' "${field}"
}

wizard_request() {
    local label="$1" response
    shift
    for _ in {1..25}; do
        if response="$(curl --fail --silent --show-error --connect-timeout 2 --max-time 12 "$@")"; then
            printf '%s' "${response}"
            return 0
        fi
        sleep 1
    done
    compat_die "${MATRIX_ID}: startup wizard ${label} failed"
}

authenticate() {
    local payload response token
    payload="$(python3 - "${RK_COMPAT_USER}" "${RK_COMPAT_PASSWORD}" <<'PY'
import json
import sys
print(json.dumps({"Username": sys.argv[1], "Pw": sys.argv[2]}, separators=(",", ":")))
PY
)"
    for _ in {1..90}; do
        response="$(curl --silent --show-error --connect-timeout 2 --max-time 8 \
            -X POST "${ORIGIN}/Users/AuthenticateByName" \
            -H 'Content-Type: application/json' \
            -H "$(compat_no_token_auth_header)" \
            --data "${payload}" 2>/dev/null || true)"
        token="$(printf '%s' "${response}" | json_field AccessToken)"
        if [[ "${token}" =~ ^[0-9A-Fa-f]{32}$ ]]; then
            printf '%s\n' "${token}"
            return 0
        fi
        sleep 1
    done
    compat_die "${MATRIX_ID}: authentication returned no valid 32-hex token"
}

complete_wizard() {
    local public_info wizard_done user_payload
    # Jellyfin's first boot hands the port from its temporary SetupServer host
    # to the main application host.  A readiness request can succeed just
    # before that handoff, so this fetch must tolerate the short refusal window
    # rather than turning an infrastructure race into a matrix failure.
    public_info="$(wizard_request public-info "${ORIGIN}/System/Info/Public")"
    wizard_done="$(printf '%s' "${public_info}" | json_field StartupWizardCompleted)"
    case "${wizard_done,,}" in
        true) return ;;
        false) ;;
        *) compat_die "${MATRIX_ID}: public info returned no boolean StartupWizardCompleted" ;;
    esac
    compat_log "${MATRIX_ID}: completing the disposable startup wizard"
    compat_wait_http "${ORIGIN}/Startup/User" 200 120 1
    wizard_request configuration -o /dev/null \
        -X POST "${ORIGIN}/Startup/Configuration" \
        -H 'Content-Type: application/json' \
        --data '{"UICulture":"en-US","MetadataCountryCode":"US","PreferredMetadataLanguage":"en"}'
    wizard_request user-query -o /dev/null "${ORIGIN}/Startup/User"
    user_payload="$(python3 - "${RK_COMPAT_USER}" "${RK_COMPAT_PASSWORD}" <<'PY'
import json
import sys
print(json.dumps({"Name": sys.argv[1], "Password": sys.argv[2]}, separators=(",", ":")))
PY
)"
    wizard_request user-create -o /dev/null \
        -X POST "${ORIGIN}/Startup/User" \
        -H 'Content-Type: application/json' \
        --data "${user_payload}"
    wizard_request remote-access -o /dev/null \
        -X POST "${ORIGIN}/Startup/RemoteAccess" \
        -H 'Content-Type: application/json' \
        --data '{"EnableRemoteAccess":false,"EnableAutomaticPortMapping":false}'
    wizard_request completion -o /dev/null -X POST "${ORIGIN}/Startup/Complete"
    compat_wait_http "${ORIGIN}/System/Info/Public" 200 60 1
}

wait_for_stable_generation() {
    local output="$1" previous='' current='' stable=0 temporary
    temporary="${WORK}/generation-stability.json"
    for _ in {1..24}; do
        curl --fail --silent --show-error -o "${temporary}" "${ORIGIN}/RefreshKit/Generation"
        current="$(json_field CacheKey < "${temporary}")"
        if [ -n "${previous}" ] && [ "${current}" = "${previous}" ]; then
            stable=$((stable + 1))
        else
            stable=0
        fi
        previous="${current}"
        if [ "${stable}" -ge 3 ]; then
            cp "${temporary}" "${output}"
            return 0
        fi
        sleep 5
    done
    compat_die "${MATRIX_ID}: generation did not become stable"
}

PHASE="static validation"
python3 "${HERE}/check_static.py" > "${OUT}/static.json"
python3 "${ANALYZE_TOOL}" stage "${RUNTIME}" "${STAGE}" "${OUT}/stage.json" >/dev/null

mapfile -t ARTIFACT_IDS < <(python3 "${MANIFEST_TOOL}" artifacts "${MATRIX_ID}")
PHASE="artifact verification"
python3 "${ARTIFACT_TOOL}" fetch --cache "${RK_COMPAT_CACHE_DIR}/artifacts" \
    --report "${OUT}/artifact-verification.json" "${ARTIFACT_IDS[@]}"

PHASE="fresh server startup"
compat_compose down --volumes --remove-orphans >/dev/null 2>&1 || true
compat_log "${MATRIX_ID}: starting ${SERVICE} (${RUNTIME}) in isolated project ${RK_COMPAT_PROJECT}"
compat_compose up -d --wait "${SERVICE}"
IFS=$'\t' read -r CONTAINER CONTAINER_IP LOOPBACK_ACTIVE < <(
    compat_container_details "${SERVICE}" "${RUNTIME}" "${OUT}/network.json"
)
IFS=$'\t' read -r ORIGIN ORIGIN_MODE < <(
    compat_origin "${SERVICE}" "${CONTAINER_IP}" "${LOOPBACK_ACTIVE}"
)
python3 - "${OUT}/network.json" "${ORIGIN}" "${ORIGIN_MODE}" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
value["selectedOrigin"] = sys.argv[2]
value["originMode"] = sys.argv[3]
value["allPassed"] = True
path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
compat_wait_http "${ORIGIN}/System/Info/Public" 200 120 2
docker inspect --format '{{.Config.Image}}' "${CONTAINER}" > "${OUT}/image.txt"
EXPECTED_DIGEST="$(python3 "${MANIFEST_TOOL}" runtime-field "${RUNTIME}" imageDigest)"
grep -Fq "sha256:${EXPECTED_DIGEST}" "${OUT}/image.txt" || \
    compat_die "${MATRIX_ID}: created container is not using the locked image digest"
complete_wizard
TOKEN="$(authenticate)"
umask 077
printf '%s' "${TOKEN}" > "${WORK}/token"
umask 022

WEBROOT_DISK_REQUIREMENTS="$(python3 "${MANIFEST_TOOL}" field "${MATRIX_ID}" webrootDiskRequirements)"
if [ "${WEBROOT_DISK_REQUIREMENTS}" != "{}" ]; then
    PHASE="baseline direct webroot capture"
    docker exec "${CONTAINER}" cat /jellyfin/jellyfin-web/index.html \
        > "${OUT}/webroot-before.html"
fi

PHASE="plugin installation"
: > "${OUT}/install.tsv"
mapfile -t INSTALL_ORDER < <(python3 "${MANIFEST_TOOL}" order "${MATRIX_ID}")
ordinal=0
for artifact_id in "${INSTALL_ORDER[@]}"; do
    prefix="$(printf '%02d' "${ordinal}")"
    if [ "${artifact_id}" = "@refresh-kit" ]; then
        local_plugin_dir="${STAGE}"
        plugin_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${STAGE}/meta.json")"
        remote_folder="${prefix}-refresh-kit_${plugin_version}"
        report_path="${OUT}/stage.json"
    else
        local_plugin_dir="${WORK}/install/${prefix}-${artifact_id}"
        report_path="${OUT}/materialized/${artifact_id}.json"
        python3 "${ARTIFACT_TOOL}" materialize --cache "${RK_COMPAT_CACHE_DIR}/artifacts" \
            --report "${report_path}" "${artifact_id}" "${local_plugin_dir}"
        plugin_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["metaChecks"]["version"]["expected"])' "${report_path}")"
        remote_folder="${prefix}-${artifact_id}_${plugin_version}"
    fi
    remote_path="/config/plugins/${remote_folder}"
    docker exec "${CONTAINER}" mkdir -p "${remote_path}"
    docker cp "${local_plugin_dir}/." "${CONTAINER}:${remote_path}/" >/dev/null
    printf '%s\t%s\t%s\t%s\n' \
        "${ordinal}" "${artifact_id}" "${remote_folder}" "${report_path}" >> "${OUT}/install.tsv"
    ordinal=$((ordinal + 1))
done

if [ "${MATRIX_ID}" = "jf10-transform-editors" ]; then
    PHASE="Editor's Choice preload configuration"
    EDITORS_CONFIG_SOURCE="${RK_COMPAT_DIR}/fixtures/configurations/EditorsChoicePlugin.xml"
    EDITORS_CONFIG_DESTINATION="/config/plugins/configurations/EditorsChoicePlugin.xml"
    docker exec "${CONTAINER}" mkdir -p /config/plugins/configurations
    docker cp "${EDITORS_CONFIG_SOURCE}" \
        "${CONTAINER}:${EDITORS_CONFIG_DESTINATION}" >/dev/null
    docker exec "${CONTAINER}" cat "${EDITORS_CONFIG_DESTINATION}" \
        > "${OUT}/editors-choice-preload.xml"
    cmp --silent "${EDITORS_CONFIG_SOURCE}" "${OUT}/editors-choice-preload.xml" || \
        compat_die "${MATRIX_ID}: Editor's Choice preload configuration changed in transit"
fi

PHASE="plugin load"
compat_compose restart "${SERVICE}" >/dev/null
compat_wait_http "${ORIGIN}/System/Info/Public" 200 150 2 || {
    compat_compose logs --no-color "${SERVICE}" >&2
    compat_die "${MATRIX_ID}: server did not recover after plugin installation"
}
TOKEN="$(authenticate)"
printf '%s' "${TOKEN}" > "${WORK}/token"
AUTH="$(compat_auth_header "${TOKEN}")"
compat_wait_http "${ORIGIN}/RefreshKit/Generation" 200 90 2 || {
    compat_compose logs --no-color "${SERVICE}" >&2
    compat_die "${MATRIX_ID}: Refresh Kit endpoint did not load"
}
PHASE="matrix plugin configuration"
mkdir -p "${OUT}/configurations"
while IFS=$'\t' read -r artifact_id plugin_guid payload; do
    [ -n "${artifact_id}" ] || continue
    curl --fail --silent --show-error -o /dev/null \
        -X POST "${ORIGIN}/Plugins/${plugin_guid}/Configuration" \
        -H 'Content-Type: application/json' -H "${AUTH}" --data "${payload}"
    curl --fail --silent --show-error -H "${AUTH}" \
        -o "${OUT}/configurations/${artifact_id}.json" \
        "${ORIGIN}/Plugins/${plugin_guid}/Configuration"
done < <(python3 "${MANIFEST_TOOL}" configurations "${MATRIX_ID}")
if [ "${MATRIX_ID}" = "jf10-transform-editors" ]; then
    curl --fail --silent --show-error -H "${AUTH}" \
        -o "${OUT}/editors-choice-configuration.json" \
        "${ORIGIN}/Plugins/70bb2ec1-f19e-46b5-b49a-942e6b96ebae/Configuration"
fi

PHASE="Refresh Kit configuration"
RK_CONFIG='{"EnableInjection":true,"EnableThirdPartyStamping":true,"EnableAutoReload":true,"PollSeconds":15,"IdleSeconds":0,"ReloadBudget":10,"EnableConfigWatching":true,"ConfigWatchExclusions":[],"ConfigCooldownMinutes":0,"DevMode":false}'
curl --fail --silent --show-error -o /dev/null \
    -X POST "${ORIGIN}/Plugins/${RK_COMPAT_REFRESH_KIT_GUID}/Configuration" \
    -H 'Content-Type: application/json' -H "${AUTH}" --data "${RK_CONFIG}"
wait_for_stable_generation "${OUT}/generation-before.json"
GENERATION_BEFORE="$(json_field CacheKey < "${OUT}/generation-before.json")"
PHASE="baseline shell capture"
curl --fail --silent --show-error -H "${AUTH}" \
    -o "${OUT}/diagnostics-before.json" "${ORIGIN}/RefreshKit/Diagnostics"
curl --fail --silent --show-error -o "${OUT}/shell-before.html" \
    -H 'Accept: text/html' \
    "${ORIGIN}/web/index.html"

PHASE="generation mutation probe"
GENERATION_PROBE="$(python3 "${MANIFEST_TOOL}" field "${MATRIX_ID}" generationProbe)"
PROBE_FOLDER="$(awk -F '\t' -v wanted="${GENERATION_PROBE}" '$2 == wanted { print $3 }' "${OUT}/install.tsv")"
[ -n "${PROBE_FOLDER}" ] || compat_die "${MATRIX_ID}: generation probe install folder not found"
docker exec "${CONTAINER}" sh -c \
    'printf "%s\n" "window.__rkCompatGenerationProbe = 2;" > "$1/rk-compat-generation-probe.js"' \
    sh "/config/plugins/${PROBE_FOLDER}"

generation_changed=0
for _ in $(seq 1 20); do
    sleep 6
    curl --fail --silent --show-error -o "${OUT}/generation-after.json" \
        "${ORIGIN}/RefreshKit/Generation"
    GENERATION_AFTER="$(json_field CacheKey < "${OUT}/generation-after.json")"
    if [ -n "${GENERATION_AFTER}" ] && [ "${GENERATION_AFTER}" != "${GENERATION_BEFORE}" ]; then
        generation_changed=1
        break
    fi
done
[ "${generation_changed}" -eq 1 ] || \
    compat_die "${MATRIX_ID}: generation stayed ${GENERATION_BEFORE} after loose asset change"

PHASE="runtime evidence capture"
curl --fail --silent --show-error -o "${OUT}/system.json" \
    "${ORIGIN}/System/Info/Public"
curl --fail --silent --show-error -H "${AUTH}" -o "${OUT}/plugins.json" \
    "${ORIGIN}/Plugins"
curl --fail --silent --show-error -H "${AUTH}" -o "${OUT}/diagnostics-after.json" \
    "${ORIGIN}/RefreshKit/Diagnostics"
curl --fail --silent --show-error -D "${OUT}/shell-after.headers" \
    -H 'Accept: text/html' \
    -o "${OUT}/shell-after.html" "${ORIGIN}/web/index.html"

ETAG="$(awk 'BEGIN { IGNORECASE=1 } /^etag:/ { sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); value=$0 } END { print value }' "${OUT}/shell-after.headers")"
CACHE_EXPECTATION="$(python3 "${MANIFEST_TOOL}" field "${MATRIX_ID}" cacheExpectation)"
CONDITIONAL_ETAG="${ETAG}"
if [ "${CACHE_EXPECTATION}" = "safe-degrade" ]; then
    CONDITIONAL_ETAG='"rk-compat-probe"'
fi
# curl does not create its -o target for a bodyless 304. Pre-create both files
# so ordinary cache-required matrices retain explicit zero-byte body evidence.
: > "${OUT}/conditional.headers"
: > "${OUT}/conditional.html"
CONDITIONAL_STATUS="$(curl --fail --silent --show-error \
    -D "${OUT}/conditional.headers" -o "${OUT}/conditional.html" \
    --write-out '%{http_code}' -H 'Accept: text/html' \
    -H "If-None-Match: ${CONDITIONAL_ETAG}" \
    "${ORIGIN}/web/index.html")"
printf '%s\n' "${CONDITIONAL_STATUS}" > "${OUT}/conditional-status.txt"
curl --fail --silent --show-error -D "${OUT}/kit.headers" -o "${OUT}/kit.js" \
    "${ORIGIN}/RefreshKit/kit.js?v=${GENERATION_AFTER}"
curl --fail --silent --show-error --compressed -H 'Accept-Encoding: gzip' \
    -H 'Accept: text/html' \
    -D "${OUT}/shell-gzip.headers" -o "${OUT}/shell-gzip.html" \
    "${ORIGIN}/web/index.html"
curl --fail --silent --show-error --compressed -H 'Accept-Encoding: br' \
    -H 'Accept: text/html' \
    -D "${OUT}/shell-br.headers" -o "${OUT}/shell-br.html" \
    "${ORIGIN}/web/index.html"

: > "${OUT}/route-status.tsv"
for route in / /web /web/ /web/index.html; do
    status="$(curl --silent --show-error --output /dev/null \
        --write-out '%{http_code}' -H 'Accept: text/html' "${ORIGIN}${route}")"
    printf '%s\t%s\n' "${route}" "${status}" >> "${OUT}/route-status.tsv"
done

mkdir -p "${OUT}/content-probes"
while IFS=$'\t' read -r probe_id probe_path authenticated probe_accept probe_max_bytes; do
    [ -n "${probe_id}" ] || continue
    if [[ ! "${probe_max_bytes}" =~ ^[0-9]+$ ]] \
        || [ "${probe_max_bytes}" -lt 1 ] \
        || [ "${probe_max_bytes}" -gt 1048576 ]; then
        compat_die "${MATRIX_ID}: content probe ${probe_id} has an invalid transfer cap"
    fi
    probe_body="${OUT}/content-probes/${probe_id}.txt"
    probe_headers="${OUT}/content-probes/${probe_id}.headers"
    probe_status_file="${OUT}/content-probes/${probe_id}.status"
    : > "${probe_body}"
    : > "${probe_headers}"
    : > "${probe_status_file}"
    if [ "${authenticated}" = true ]; then
        if probe_status="$(curl --silent --show-error --no-location --globoff \
            --connect-timeout 10 --max-time 30 --max-filesize "${probe_max_bytes}" \
            -H "${AUTH}" -H "Accept: ${probe_accept}" \
            -H 'Accept-Encoding: identity' -D "${probe_headers}" \
            -o "${probe_body}" --write-out $'%{http_code}\t%{url_effective}' \
            "${ORIGIN}${probe_path}")"; then
            probe_rc=0
        else
            probe_rc=$?
        fi
    else
        if probe_status="$(curl --silent --show-error --no-location --globoff \
            --connect-timeout 10 --max-time 30 --max-filesize "${probe_max_bytes}" \
            -H "Accept: ${probe_accept}" -H 'Accept-Encoding: identity' \
            -D "${probe_headers}" -o "${probe_body}" \
            --write-out $'%{http_code}\t%{url_effective}' \
            "${ORIGIN}${probe_path}")"; then
            probe_rc=0
        else
            probe_rc=$?
        fi
    fi
    printf '%s\n' "${probe_status}" > "${probe_status_file}"
    for probe_capture in "${probe_body}" "${probe_headers}" "${probe_status_file}"; do
        if grep -Fq -- "${TOKEN}" "${probe_capture}"; then
            : > "${probe_body}"
            : > "${probe_headers}"
            : > "${probe_status_file}"
            compat_die "${MATRIX_ID}: content probe ${probe_id} echoed the authentication token"
        fi
    done
    if [ "${probe_rc}" -ne 0 ]; then
        compat_die "${MATRIX_ID}: content probe ${probe_id} transport failed with curl status ${probe_rc}"
    fi
done < <(python3 "${MANIFEST_TOOL}" probes "${MATRIX_ID}")

while IFS=$'\t' read -r _ artifact_id remote_folder _; do
    meta_output="${OUT}/runtime-meta/${artifact_id}.json"
    docker exec "${CONTAINER}" cat "/config/plugins/${remote_folder}/meta.json" > "${meta_output}"
done < "${OUT}/install.tsv"
if [ "${WEBROOT_DISK_REQUIREMENTS}" != "{}" ]; then
    docker exec "${CONTAINER}" cat /jellyfin/jellyfin-web/index.html \
        > "${OUT}/webroot-after.html"
fi
compat_compose logs --no-color "${SERVICE}" > "${OUT}/server.log" 2>&1

PHASE="structured analysis"
python3 "${ANALYZE_TOOL}" runtime "${MATRIX_ID}" "${OUT}" "${RESULT}"

PHASE="cleanup"
compat_compose down --volumes --remove-orphans >/dev/null
trap - EXIT
compat_log "${MATRIX_ID}: PASS; structured result ${RESULT}"
