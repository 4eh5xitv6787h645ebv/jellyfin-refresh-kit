#!/usr/bin/env bash

# Reproducible disposable Jellyfin 10.11.11 + 12.0-rc4 lifecycle lab.
#
# Commands are intentionally composable for a later repository-level runner:
#   ./run.sh build
#   ./run.sh reset       clean project resources, start and provision both
#   ./run.sh check       endpoint + transformed-shell assertions on both
#   ./run.sh browser     real Chromium + plain process restart on both
#   ./run.sh lifecycle   real package/plugin API lifecycle on both
#   ./run.sh third-party genuine two-version third-party lifecycle on both
#   ./run.sh runner-negative verify lifecycle failures cannot be swallowed
#   ./run.sh compat      net9/JF10 build on pristine JF12, record, restore
#   ./run.sh abi-floor   exact isolated Jellyfin 10.11.0 net9 load smoke
#   ./run.sh abi-floor-negative no-Docker floor-smoke contract regression
#   ./run.sh host-upgrade reproducible in-place JF10 and JF12 upgrade paths
#   ./run.sh host-upgrade-negative no-Docker fail-closed lab regression
#   ./run.sh all         ABI floor + lifecycle + browser + compat + host upgrades
#   ./run.sh down        remove only this Compose project's resources
#   ./run.sh clean       down + remove this lab's generated state/artifacts
#
set -euo pipefail
umask 022
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
# Resolved from this script's directory at runtime.
# shellcheck disable=SC1091
source "${HERE}/lib/common.sh"

usage() {
    sed -n '3,20p' "$0"
}

check_prerequisites() {
    rk_require curl docker node npm python3 sha256sum
    docker info >/dev/null 2>&1 || rk_die "Docker daemon is unavailable"
    docker compose version >/dev/null 2>&1 || rk_die "Docker Compose is unavailable"
}

cmd_build() {
    check_prerequisites || return $?
    local selected_dotnet=""
    if [ -n "${DOTNET_ROOT:-}" ] && [ -x "${DOTNET_ROOT}/dotnet" ]; then
        selected_dotnet="${DOTNET_ROOT}"
    elif [ -x "${HOME}/.dotnet/dotnet" ]; then
        selected_dotnet="${HOME}/.dotnet"
    else
        local launcher
        launcher="$(command -v dotnet || true)"
        [ -n "${launcher}" ] && \
            selected_dotnet="$(python3 -c 'import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "${launcher}")"
    fi
    [ -x "${selected_dotnet}/dotnet" ] || \
        rk_die "pinned .NET SDK launcher not found at ${selected_dotnet}/dotnet"
    rk_log "building both current plugin stages with the repository-pinned SDK"
    unset RK_BUILD_SNAPSHOT
    export DOTNET_ROOT="${selected_dotnet}"
    bash "${RK_REPO_ROOT}/plugin/build.sh" || return $?
    rk_pin_build_snapshot
}

ensure_stages() {
    local flavor stage
    for flavor in jf10 jf12; do
        stage="$(rk_stage "${flavor}")"
        [ -f "${stage}/Jellyfin.Plugin.RefreshKit.dll" ] || \
            rk_die "missing ${flavor} stage; run ./run.sh build"
        [ -f "${stage}/meta.json" ] || rk_die "missing ${stage}/meta.json"
    done
}

invalidate_lifecycle_completion() {
    local target
    for target in "$@"; do
        case "${target}" in jf10|jf12) ;; *) rk_die "cannot invalidate unknown target ${target}" ;; esac
        rm -f -- \
            "${RK_STATE_DIR}/${target}.lifecycle.ok" \
            "${RK_STATE_DIR}/${target}.lifecycle.ok.part" || return $?
    done
}

provision_both() {
    local p10 p12 rc10 rc12
    bash "${HERE}/lib/provision.sh" jf10 jf10 & p10=$!
    bash "${HERE}/lib/provision.sh" jf12 jf12 & p12=$!
    if wait "${p10}"; then rc10=0; else rc10=$?; fi
    if wait "${p12}"; then rc12=0; else rc12=$?; fi
    if [ "${rc10}" -ne 0 ] || [ "${rc12}" -ne 0 ]; then
        rk_compose ps >&2 || true
        rk_die "provisioning failed (jf10=${rc10}, jf12=${rc12})"
    fi
}

cmd_up() {
    check_prerequisites || return $?
    invalidate_lifecycle_completion jf10 jf12 || return $?
    if [ "${RK_SKIP_BUILD:-0}" != "1" ]; then
        cmd_build || return $?
    else
        rk_pin_build_snapshot || return $?
    fi
    ensure_stages || return $?
    rk_log "starting isolated Compose project ${RK_PROJECT} on ${RK_JF10_PORT}/${RK_JF12_PORT}"
    rk_compose up -d --wait || return $?
    rk_assert_container_image jf10 || return $?
    rk_assert_container_image jf12 || return $?
    provision_both
}

cmd_reset() {
    check_prerequisites || return $?
    rk_log "removing prior resources owned by Compose project ${RK_PROJECT}"
    rk_compose --profile lifecycle --profile abi-floor --profile host-upgrade \
        down --volumes --remove-orphans >/dev/null 2>&1 || true
    rm -f -- \
        "${RK_STATE_DIR}/jf10.token" "${RK_STATE_DIR}/jf12.token" \
        "${RK_STATE_DIR}/abi-floor.token" "${RK_STATE_DIR}/abi-floor.token.part" \
        || return $?
    cmd_up || return $?
    cmd_check all
}

cmd_check() {
    local target="${1:-all}"
    case "${target}" in
        all)
            bash "${HERE}/lib/check.sh" jf10 || return $?
            bash "${HERE}/lib/check.sh" jf12
            ;;
        jf10|jf12) bash "${HERE}/lib/check.sh" "${target}" ;;
        *) rk_die "check target must be jf10, jf12 or all" ;;
    esac
}

run_browser_target() {
    local target="$1" origin container out
    origin="$(rk_origin "${target}")"
    container="$(rk_container_id "${target}")"
    out="${RK_ARTIFACT_DIR}/${target}/browser"
    RK_LAB_USER="${RK_LAB_USER}" RK_LAB_PASSWORD="${RK_LAB_PASSWORD}" \
        node "${HERE}/lib/browser.cjs" \
        --target "${target}" --origin "${origin}" \
        --container "${container}" --project "${RK_PROJECT}" --out "${out}"
}

cmd_browser() {
    local target="${1:-all}"
    case "${target}" in
        all)
            run_browser_target jf10 || return $?
            run_browser_target jf12
            ;;
        jf10|jf12) run_browser_target "${target}" ;;
        *) rk_die "browser target must be jf10, jf12 or all" ;;
    esac
}

prepare_lifecycle_target() {
    local target="$1"
    bash "${HERE}/lib/prepare-lifecycle-repository.sh" "${target}"
}

run_lifecycle_target() {
    local target="$1" origin container out token_file metadata started_utc result
    origin="$(rk_origin "${target}")"
    container="$(rk_container_id "${target}")"
    out="${RK_ARTIFACT_DIR}/${target}/lifecycle"
    token_file="$(rk_token_file "${target}")"
    metadata="${RK_STATE_DIR}/${target}.lifecycle.json"
    [ -s "${token_file}" ] || rk_die "${target}: lifecycle token is missing"
    [ -s "${metadata}" ] || rk_die "${target}: lifecycle repository metadata is missing"
    mkdir -p "${out}"
    started_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if RK_LAB_USER="${RK_LAB_USER}" RK_LAB_PASSWORD="${RK_LAB_PASSWORD}" \
        RK_LAB_TOKEN="$(<"${token_file}")" \
        node "${HERE}/lib/browser-lifecycle.cjs" \
        --target "${target}" --origin "${origin}" \
        --container "${container}" --project "${RK_PROJECT}" \
        --metadata "${metadata}" --out "${out}"; then
        result=0
    else
        result=$?
    fi
    docker logs --since "${started_utc}" "${container}" > "${out}/server.log" 2>&1 || true
    return "${result}"
}

prepare_third_party_target() {
    local target="$1"
    validate_completed_self_lifecycle "${target}" || return $?
    bash "${HERE}/lib/prepare-third-party-repository.sh" "${target}"
}

validate_completed_self_lifecycle() {
    local target="$1" result stage version package suffix package_md5 completion expected_snapshot
    result="${RK_ARTIFACT_DIR}/${target}/lifecycle/result.json"
    stage="$(rk_stage "${target}")"
    completion="${RK_STATE_DIR}/${target}.lifecycle.ok"
    expected_snapshot="$(basename "${RK_BUILD_SNAPSHOT}")"
    [ -s "${result}" ] || \
        rk_die "${target}: completed self-lifecycle evidence is missing: ${result}"
    [ -s "${completion}" ] || \
        rk_die "${target}: self-lifecycle completion marker is missing; rerun lifecycle for this snapshot"
    [ "$(<"${completion}")" = "${expected_snapshot}" ] || \
        rk_die "${target}: self-lifecycle completion marker does not match selected snapshot"
    version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${stage}/meta.json")"
    suffix=''
    [ "${target}" != jf12 ] || suffix='_jf12'
    package="${RK_BUILD_SNAPSHOT}/jellyfin-refresh-kit_${version}${suffix}.zip"
    [ -s "${package}" ] || rk_die "${target}: selected snapshot package is missing: ${package}"
    package_md5="$(md5sum "${package}" | awk '{print $1}')"

    python3 - "${result}" "${stage}/meta.json" "${package_md5}" "${target}" <<'PY'
import json
import sys

result_path, meta_path, expected_md5, target = sys.argv[1:]
with open(result_path, encoding="utf-8") as handle:
    result = json.load(handle)
with open(meta_path, encoding="utf-8") as handle:
    meta = json.load(handle)

def fail(message):
    raise SystemExit(f"FATAL: {target}: {message}")

if result.get("target") != target:
    fail("self-lifecycle result belongs to a different target")
if result.get("completed") is not True:
    fail("self-lifecycle scenario did not reach its final logout/relogin phase")
if result.get("failures") != []:
    fail("self-lifecycle result contains failures")
if result.get("unexpectedRefreshKitBrowserErrors") != []:
    fail("self-lifecycle result contains unexpected Refresh Kit browser errors")
if result.get("repositoryConfigurationRemoved") is not True:
    fail("self-lifecycle repository cleanup was not confirmed")

phase_names = [phase.get("name") for phase in result.get("phases", [])]
required_phases = {
    "update-candidate-converged",
    "disable-clean-shell",
    "enable-active",
    "uninstall-clean-shell",
    "reinstall-active",
    "plain-restart-in-place",
    "playback-pause-safety-gate",
    "ui-logout-and-relogin",
}
missing = sorted(required_phases.difference(phase_names))
if missing:
    fail(f"self-lifecycle evidence is missing required phases: {missing}")

lifecycle = result.get("metadata") or {}
comparisons = {
    "candidate version": (lifecycle.get("candidateVersion"), meta.get("version")),
    "source revision": (lifecycle.get("candidateSourceRevision"), meta.get("sourceRevision")),
    "source tree": (lifecycle.get("candidateSourceTreeSha256"), meta.get("sourceTreeSha256")),
    "package MD5": (lifecycle.get("candidateMd5"), expected_md5),
}
for label, (actual, expected) in comparisons.items():
    if actual != expected:
        fail(f"self-lifecycle {label} {actual!r} does not match selected snapshot {expected!r}")
PY
}

run_third_party_target() {
    local target="$1" origin container out token_file metadata started_utc result
    origin="$(rk_origin "${target}")"
    container="$(rk_container_id "${target}")"
    out="${RK_ARTIFACT_DIR}/${target}/third-party-lifecycle"
    token_file="$(rk_token_file "${target}")"
    metadata="${RK_STATE_DIR}/${target}.third-party.json"
    [ -s "${token_file}" ] || rk_die "${target}: run the completed self-lifecycle before third-party lifecycle"
    [ -s "${metadata}" ] || rk_die "${target}: third-party repository metadata is missing"
    mkdir -p "${out}"
    started_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    if RK_LAB_USER="${RK_LAB_USER}" RK_LAB_PASSWORD="${RK_LAB_PASSWORD}" \
        RK_LAB_TOKEN="$(<"${token_file}")" \
        node "${HERE}/lib/browser-third-party-lifecycle.cjs" \
        --target "${target}" --origin "${origin}" \
        --container "${container}" --project "${RK_PROJECT}" \
        --metadata "${metadata}" --out "${out}"; then
        result=0
    else
        result=$?
    fi
    docker logs --since "${started_utc}" "${container}" > "${out}/server.log" 2>&1 || true
    return "${result}"
}

lifecycle_target() {
    local target="$1" token_file completion_file
    token_file="$(rk_token_file "${target}")"
    completion_file="${RK_STATE_DIR}/${target}.lifecycle.ok"
    rk_log "${target}: resetting project-scoped storage for the full plugin lifecycle"
    rk_reset_service_storage "${target}" || return $?
    rm -f -- "${token_file}" "${completion_file}" "${completion_file}.part" || return $?
    rk_compose --profile lifecycle up -d --wait repository "${target}" || return $?
    rk_assert_container_image "${target}" || return $?
    RK_PROVISION_SKIP_PLUGIN=1 bash "${HERE}/lib/provision.sh" "${target}" "${target}" || return $?
    run_lifecycle_target "${target}" || return $?
    bash "${HERE}/lib/check.sh" "${target}" || return $?
    printf '%s\n' "$(basename "${RK_BUILD_SNAPSHOT}")" > "${completion_file}.part" || return $?
    mv -f -- "${completion_file}.part" "${completion_file}"
}

cmd_lifecycle() {
    local target="${1:-all}"
    check_prerequisites || return $?
    rk_require ffmpeg md5sum || return $?
    if [ "${RK_SKIP_BUILD:-0}" != "1" ]; then
        cmd_build || return $?
    else
        rk_pin_build_snapshot || return $?
    fi
    ensure_stages || return $?
    case "${target}" in
        all)
            prepare_lifecycle_target jf10 || return $?
            prepare_lifecycle_target jf12 || return $?
            lifecycle_target jf10 || return $?
            lifecycle_target jf12
            ;;
        jf10|jf12)
            prepare_lifecycle_target "${target}" || return $?
            lifecycle_target "${target}"
            ;;
        *) rk_die "lifecycle target must be jf10, jf12 or all" ;;
    esac
}

third_party_target() {
    local target="$1" result
    rk_compose --profile lifecycle up -d --wait repository "${target}" || return $?
    rk_assert_container_image "${target}" || return $?
    if run_third_party_target "${target}"; then
        result=0
    else
        result=$?
    fi
    [ "${result}" -eq 0 ] || return "${result}"
    bash "${HERE}/lib/check.sh" "${target}"
}

cmd_third_party() {
    local target="${1:-all}" rc=0
    check_prerequisites || return $?
    rk_require md5sum || return $?
    rk_pin_build_snapshot || return $?
    ensure_stages || return $?
    case "${target}" in
        all)
            prepare_third_party_target jf10 || return $?
            prepare_third_party_target jf12 || return $?
            third_party_target jf10 || rc=1
            third_party_target jf12 || rc=1
            return "${rc}"
            ;;
        jf10|jf12)
            prepare_third_party_target "${target}" || return $?
            third_party_target "${target}"
            ;;
        *) rk_die "third-party target must be jf10, jf12 or all" ;;
    esac
}

cmd_compat() {
    invalidate_lifecycle_completion jf12 || return $?
    rk_pin_build_snapshot || return $?
    ensure_stages || return $?
    bash "${HERE}/lib/compat.sh"
}

cmd_abi_floor() {
    bash "${HERE}/lib/abi-floor-smoke.sh"
}

cmd_host_upgrade() {
    local target="${1:-all}"
    case "${target}" in jf10|jf12|all) ;; *) rk_die "host-upgrade target must be jf10, jf12 or all" ;; esac
    bash "${HERE}/lib/host-upgrade.sh" "${target}"
}

cmd_restart() {
    local target="${1:-}"
    case "${target}" in jf10|jf12) ;; *) rk_die "restart target must be jf10 or jf12" ;; esac
    rk_compose restart "${target}" >/dev/null || return $?
    rk_wait_http "$(rk_origin "${target}")/System/Info/Public" 200 120 2 || return $?
    rk_log "${target}: restarted and healthy"
}

cmd_status() {
    rk_compose --profile abi-floor --profile host-upgrade ps
    printf 'jf10: %s/System/Info/Public -> %s\n' "${RK_JF10_ORIGIN}" \
        "$(rk_http_code "${RK_JF10_ORIGIN}/System/Info/Public")"
    printf 'jf12: %s/System/Info/Public -> %s\n' "${RK_JF12_ORIGIN}" \
        "$(rk_http_code "${RK_JF12_ORIGIN}/System/Info/Public")"
}

cmd_down() {
    rk_log "removing containers, network and volumes owned by ${RK_PROJECT}"
    rk_compose --profile lifecycle --profile abi-floor --profile host-upgrade \
        down --volumes --remove-orphans
    rm -f -- \
        "${RK_STATE_DIR}/jf10.token" "${RK_STATE_DIR}/jf12.token" \
        "${RK_STATE_DIR}/abi-floor.token" "${RK_STATE_DIR}/abi-floor.token.part" \
        "${RK_STATE_DIR}/jf10.lifecycle.ok" "${RK_STATE_DIR}/jf12.lifecycle.ok" \
        "${RK_STATE_DIR}/jf10.lifecycle.ok.part" "${RK_STATE_DIR}/jf12.lifecycle.ok.part" \
        "${RK_STATE_DIR}/host-upgrade-jf10.admin.token" \
        "${RK_STATE_DIR}/host-upgrade-jf10.viewer.token" \
        "${RK_STATE_DIR}/host-upgrade-jf10.admin.id" \
        "${RK_STATE_DIR}/host-upgrade-jf10.viewer.id" \
        "${RK_STATE_DIR}/host-upgrade-jf10.metadata.json" \
        "${RK_STATE_DIR}/host-upgrade-jf12.admin.token" \
        "${RK_STATE_DIR}/host-upgrade-jf12.viewer.token" \
        "${RK_STATE_DIR}/host-upgrade-jf12.admin.id" \
        "${RK_STATE_DIR}/host-upgrade-jf12.viewer.id" \
        "${RK_STATE_DIR}/host-upgrade-jf12.metadata.json"
}

cmd_clean() {
    cmd_down || true
    case "${RK_STATE_DIR}:${RK_ARTIFACT_DIR}" in
        "${RK_JELLYFIN_DIR}/.state:${RK_JELLYFIN_DIR}/artifacts") ;;
        *) rk_die "refusing to clean unexpected scratch paths" ;;
    esac
    rm -rf -- "${RK_STATE_DIR}" "${RK_ARTIFACT_DIR}"
    mkdir -p "${RK_STATE_DIR}" "${RK_ARTIFACT_DIR}"
    rk_log "removed generated state and captures"
}

cmd_all() {
    cmd_lifecycle all || return $?
    # lifecycle resolves/builds the immutable snapshot first; the ABI-floor
    # runner then pins that same snapshot instead of requiring a prior build.
    cmd_abi_floor || return $?
    cmd_third_party all || return $?
    cmd_compat || return $?
    cmd_browser all || return $?
    cmd_host_upgrade all
}

main() {
    case "${1:-}" in
        build)   cmd_build ;;
        up)      cmd_up ;;
        reset|init) cmd_reset ;;
        check)   cmd_check "${2:-all}" ;;
        browser) cmd_browser "${2:-all}" ;;
        lifecycle) cmd_lifecycle "${2:-all}" ;;
        third-party) cmd_third_party "${2:-all}" ;;
        runner-negative) bash "${HERE}/lib/runner-negative.sh" ;;
        compat)  cmd_compat ;;
        abi-floor) cmd_abi_floor ;;
        abi-floor-negative) bash "${HERE}/lib/abi-floor-negative.sh" ;;
        host-upgrade) cmd_host_upgrade "${2:-all}" ;;
        host-upgrade-negative) bash "${HERE}/lib/host-upgrade-negative.sh" ;;
        restart) cmd_restart "${2:-}" ;;
        status)  cmd_status ;;
        all)     cmd_all ;;
        down)    cmd_down ;;
        clean)   cmd_clean ;;
        -h|--help|help|'') usage ;;
        *) usage >&2; return 2 ;;
    esac
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
