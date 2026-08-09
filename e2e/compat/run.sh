#!/usr/bin/env bash

# Project-scoped third-party Jellyfin compatibility harness.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The path is resolved from this file, not the caller's working directory.
# shellcheck disable=SC1091
source "${HERE}/lib/common.sh"
cd "${RK_COMPAT_REPO_ROOT}"

usage() {
    sed -n '3,23p' "$0"
    printf '\nCommands:\n'
    printf '  static                 Container-free lock, source, fixture, and syntax checks\n'
    printf '  list                   List runnable matrices\n'
    printf '  coverage               Print testable/quarantined/unsupported coverage JSON\n'
    printf '  fetch MATRIX           Verify the artifacts used by one matrix\n'
    printf '  fetch all-locked       Verify every locked archive, including non-installable ones\n'
    printf '  run MATRIX             Run one fresh matrix (requires RK_COMPAT_ALLOW_CONTAINERS=1)\n'
    printf '  all                    Clean, run every matrix, aggregate (same explicit opt-in)\n'
    printf '  status                 Show only this Compose project\n'
    printf '  down                   Remove only this Compose project and its volumes\n'
    printf '  clean                  Down, then remove this harness generated cache/state/results\n'
}

cmd_static() {
    local dotnet_launcher fixture_project fixture_dll
    compat_require bash python3
    dotnet_launcher="$(compat_dotnet_launcher)"
    python3 "${HERE}/lib/artifacts.py" lint
    python3 "${HERE}/lib/manifest.py" lint
    python3 "${HERE}/lib/check_static.py"
    python3 -m py_compile \
        "${HERE}/lib/artifacts.py" \
        "${HERE}/lib/manifest.py" \
        "${HERE}/lib/check_static.py" \
        "${HERE}/lib/analyze.py"
    bash -n "${HERE}/run.sh" "${HERE}/lib/common.sh" "${HERE}/lib/runtime.sh"
    if command -v shellcheck >/dev/null 2>&1; then
        shellcheck "${HERE}/run.sh" "${HERE}/lib/common.sh" "${HERE}/lib/runtime.sh"
    fi
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        docker compose -f "${RK_COMPAT_COMPOSE_FILE}" config --quiet
    fi
    fixture_project="${HERE}/fixtures/StaticFixtureHarness.csproj"
    fixture_dll="${HERE}/fixtures/bin/Release/net10.0/StaticFixtureHarness.dll"
    "${dotnet_launcher}" restore "${fixture_project}" \
        -noAutoResponse \
        --configfile "${RK_COMPAT_REPO_ROOT}/NuGet.Config" \
        --nologo \
        -p:ImportDirectoryBuildProps=false \
        -p:ImportDirectoryBuildTargets=false \
        -p:ImportDirectoryPackagesProps=false \
        -p:NuGetAudit=false
    "${dotnet_launcher}" build "${fixture_project}" \
        -noAutoResponse \
        --configuration Release \
        --no-restore \
        --nologo \
        -p:ImportDirectoryBuildProps=false \
        -p:ImportDirectoryBuildTargets=false \
        -p:ImportDirectoryPackagesProps=false \
        -p:NuGetAudit=false
    "${dotnet_launcher}" "${fixture_dll}" "${HERE}/fixtures/stamping.json"
    compat_log "static compatibility checks PASS (no container started)"
}

cmd_list() {
    python3 "${HERE}/lib/manifest.py" list
}

cmd_coverage() {
    python3 "${HERE}/lib/manifest.py" coverage --pretty
}

cmd_fetch() {
    local selection="${1:-}"
    compat_require python3
    case "${selection}" in
        all-locked)
            python3 "${HERE}/lib/artifacts.py" fetch --all-locked \
                --cache "${RK_COMPAT_CACHE_DIR}/artifacts" \
                --report "${RK_COMPAT_ARTIFACT_DIR}/all-locked-verification.json"
            ;;
        '') compat_die "fetch requires a matrix id or all-locked" ;;
        *)
            mapfile -t selected_artifacts < <(python3 "${HERE}/lib/manifest.py" artifacts "${selection}")
            python3 "${HERE}/lib/artifacts.py" fetch \
                --cache "${RK_COMPAT_CACHE_DIR}/artifacts" \
                --report "${RK_COMPAT_ARTIFACT_DIR}/${selection}-artifact-verification.json" \
                "${selected_artifacts[@]}"
            ;;
    esac
}

check_container_opt_in() {
    if [ "${RK_COMPAT_ALLOW_CONTAINERS:-0}" != "1" ]; then
        compat_die "container execution is gated; coordinate first, then set RK_COMPAT_ALLOW_CONTAINERS=1"
    fi
    compat_require curl docker python3
    docker info >/dev/null 2>&1 || compat_die "Docker daemon is unavailable"
    docker compose version >/dev/null 2>&1 || compat_die "Docker Compose is unavailable"
}

cmd_run() {
    local matrix_id="${1:-}"
    [ -n "${matrix_id}" ] || compat_die "run requires a matrix id"
    check_container_opt_in
    compat_pin_build_snapshot
    bash "${HERE}/lib/runtime.sh" "${matrix_id}"
}

cmd_all() {
    check_container_opt_in
    compat_pin_build_snapshot
    cmd_clean
    # The release gate starts from an empty cache and cryptographically
    # reinspects every locked archive, including the deliberately non-installable
    # quarantine/unsupported rows.  Matrices then rehash their exact subsets.
    cmd_fetch all-locked
    while IFS=$'\t' read -r matrix_id _; do
        bash "${HERE}/lib/runtime.sh" "${matrix_id}"
    done < <(python3 "${HERE}/lib/manifest.py" list)
    python3 "${HERE}/lib/analyze.py" aggregate \
        "${RK_COMPAT_ARTIFACT_DIR}" "${RK_COMPAT_ARTIFACT_DIR}/summary.json"
    compat_log "all compatibility matrices accepted (including explicit limitations)"
}

cmd_status() {
    compat_require docker
    compat_compose ps
}

cmd_down() {
    compat_require docker
    compat_log "removing only Compose project ${RK_COMPAT_PROJECT} and its volumes"
    compat_compose down --volumes --remove-orphans
}

cmd_clean() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        compat_compose down --volumes --remove-orphans >/dev/null 2>&1 || true
    fi
    compat_assert_generated_paths
    rm -rf -- "${RK_COMPAT_CACHE_DIR}" "${RK_COMPAT_STATE_DIR}" "${RK_COMPAT_ARTIFACT_DIR}"
    mkdir -p "${RK_COMPAT_CACHE_DIR}" "${RK_COMPAT_STATE_DIR}" "${RK_COMPAT_ARTIFACT_DIR}"
    compat_log "removed only e2e/compat generated cache, state, and result artifacts"
}

case "${1:-}" in
    static) cmd_static ;;
    list) cmd_list ;;
    coverage) cmd_coverage ;;
    fetch) cmd_fetch "${2:-}" ;;
    run) cmd_run "${2:-}" ;;
    all) cmd_all ;;
    status) cmd_status ;;
    down) cmd_down ;;
    clean) cmd_clean ;;
    -h|--help|help|'') usage ;;
    *) usage >&2; exit 2 ;;
esac
