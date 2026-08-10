#!/usr/bin/env bash
# One-command local/CI gate. The default "fast" mode builds both packages,
# exercises both target frameworks, runs browser regressions, and validates
# source/package/configuration hygiene without starting containers.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${REPO_ROOT}"

export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1
export NUGET_XMLDOC_MODE=skip

if [ -n "${DOTNET_ROOT:-}" ] && [ -x "${DOTNET_ROOT}/dotnet" ]; then
    DOTNET="${DOTNET_ROOT}/dotnet"
elif [ -x "${HOME}/.dotnet/dotnet" ]; then
    DOTNET="${HOME}/.dotnet/dotnet"
else
    DOTNET="$(command -v dotnet || true)"
fi
if [ -z "${DOTNET}" ] || [ ! -x "${DOTNET}" ]; then
    echo "FATAL: install the .NET SDK pinned by global.json." >&2
    exit 1
fi
DOTNET_INSTALL_ROOT="$(python3 -c 'import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "${DOTNET}")"

TEST_PROJECT="plugin/Jellyfin.Plugin.RefreshKit.Tests/Jellyfin.Plugin.RefreshKit.Tests.csproj"
STANDALONE_PROJECT="tests/RefreshKit.Standalone.Compile/RefreshKit.Standalone.Compile.csproj"
REPRO_MSBUILD_PROPERTIES=(
    -noAutoResponse
    -p:ImportDirectoryBuildProps=false
    -p:ImportDirectoryBuildTargets=false
    -p:ImportDirectoryPackagesProps=false
    -p:DiscoverEditorConfigFiles=false
    -p:DiscoverGlobalAnalyzerConfigFiles=false
    -p:NuGetAudit=false
)

heading() { printf '\n==> %s\n' "$1"; }

install_browser_dependencies() {
    heading "Installing the locked browser-test dependencies"
    command -v npm >/dev/null 2>&1 || { echo "FATAL: npm is required." >&2; return 1; }
    local node_major
    node_major="$(node -p 'Number(process.versions.node.split(".")[0])')"
    [ "${node_major}" -ge 20 ] || {
        echo "FATAL: browser tests require Node.js 20 or newer (see .node-version)." >&2
        return 1
    }
    if [ "${RK_SKIP_NPM_CI:-0}" != 1 ]; then
        npm_config_audit=false npm_config_fund=false npm ci
    fi
    node -e "require.resolve('puppeteer')" >/dev/null
}

validate_static_inputs() {
    heading "Validating shell, JavaScript, JSON, and Compose inputs"
    for benchmark_input in \
        benchmarks/run.sh \
        benchmarks/browser-registration.cjs \
        benchmarks/server/RefreshKit.Benchmarks.csproj \
        benchmarks/server/Program.cs; do
        [ -f "${benchmark_input}" ] || {
            echo "FATAL: missing benchmark harness input: ${benchmark_input}" >&2
            return 1
        }
    done
    bash -n benchmarks/run.sh
    node --check benchmarks/browser-registration.cjs >/dev/null

    while IFS= read -r -d '' path; do
        bash -n "${path}"
    done < <(find . \
        -path './.git' -prune -o \
        -path './node_modules' -prune -o \
        -path './plugin/build' -prune -o \
        -path './plugin/.builds' -prune -o \
        -path './plugin/.build-work' -prune -o \
        -path './test-results' -prune -o \
        -path './e2e/jellyfin/.state' -prune -o \
        -path './e2e/jellyfin/artifacts' -prune -o \
        -path './e2e/compat/.cache' -prune -o \
        -path '*/bin' -prune -o \
        -path '*/obj' -prune -o \
        -type f -name '*.sh' -print0)

    while IFS= read -r -d '' path; do
        node --check "${path}" >/dev/null
    done < <(find . \
        -path './.git' -prune -o \
        -path './node_modules' -prune -o \
        -path './plugin/build' -prune -o \
        -path './plugin/.builds' -prune -o \
        -path './plugin/.build-work' -prune -o \
        -path './test-results' -prune -o \
        -path './e2e/jellyfin/.state' -prune -o \
        -path './e2e/jellyfin/artifacts' -prune -o \
        -path './e2e/proxy/.je' -prune -o \
        -path './e2e/compat/.cache' -prune -o \
        -type f \( -name '*.js' -o -name '*.cjs' \) -print0)

    python3 - <<'PY'
import json
import ast
import pathlib
import re
import xml.etree.ElementTree as ET

root = pathlib.Path(".")
excluded = {
    ".git", "node_modules", "build", ".builds", ".build-work", "test-results",
    "bin", "obj", ".cache", ".je", ".state", "artifacts",
}
for path in sorted(root.rglob("*.json")):
    if excluded & set(path.parts):
        continue
    with path.open(encoding="utf-8") as handle:
        json.load(handle)
for path in sorted(root.rglob("*.py")):
    if excluded & set(path.parts):
        continue
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
for path in sorted(root.rglob("*.csproj")):
    if excluded & set(path.parts):
        continue
    ET.parse(path)
benchmark_project = root / "benchmarks" / "server" / "RefreshKit.Benchmarks.csproj"
benchmark_xml = ET.parse(benchmark_project)
benchmark_dir = benchmark_project.parent
compile_inputs = [
    element.attrib.get("Include")
    for element in benchmark_xml.findall(".//Compile")
    if element.attrib.get("Include")
]
if not compile_inputs:
    raise SystemExit(f"{benchmark_project}: benchmark project has no compile inputs")
for include in compile_inputs:
    resolved = (benchmark_dir / include).resolve()
    if not resolved.is_file():
        raise SystemExit(f"{benchmark_project}: missing Compile input: {include}")
ET.parse(root / "NuGet.Config")
workflow_root = root / ".github" / "workflows"
for path in sorted({*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")}):
    text = path.read_text(encoding="utf-8")
    for action in re.findall(r"^\s*uses:\s*([^\s#]+)", text, flags=re.MULTILINE):
        if re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) is None:
            raise SystemExit(f"{path}: action is not pinned to a full commit: {action}")
PY

    python3 scripts/verify-vendored-refreshkit.py
    python3 scripts/test-release-tools.py
    bash scripts/check-workflows.sh
    bash e2e/proxy/lib/build-snapshot-negative.sh
    bash e2e/proxy/lib/static-regressions.sh
    bash e2e/jellyfin/run.sh runner-negative
    bash e2e/jellyfin/run.sh host-upgrade-negative
    bash e2e/jellyfin/run.sh abi-floor-negative

    command -v docker >/dev/null 2>&1 || {
        echo "FATAL: Docker Compose is required for compose validation." >&2
        return 1
    }
    docker compose version >/dev/null
    docker compose -f e2e/proxy/docker-compose.yml config --quiet
    docker compose -f e2e/jellyfin/docker-compose.yml config --quiet
    docker compose -f e2e/compat/docker-compose.yml config --quiet
    DiscoverEditorConfigFiles=false \
    DiscoverGlobalAnalyzerConfigFiles=false \
    DOTNET_ROOT="${DOTNET_INSTALL_ROOT}" \
        bash e2e/compat/run.sh static
}

build_packages() {
    heading "Building deterministic net9/net10 packages"
    DOTNET_ROOT="${DOTNET_INSTALL_ROOT}" ./plugin/build.sh
}

VALIDATION_BUILD_SNAPSHOT=""
prepare_validation_build_snapshot() {
    local requested snapshot_root
    if [ -n "${RK_VALIDATION_BUILD_SNAPSHOT:-}" ]; then
        requested="${RK_VALIDATION_BUILD_SNAPSHOT}"
    else
        build_packages
        requested="${REPO_ROOT}/plugin/build"
    fi
    VALIDATION_BUILD_SNAPSHOT="$(readlink -f -- "${requested}" 2>/dev/null || true)"
    snapshot_root="$(readlink -f -- "${REPO_ROOT}/plugin/.builds" 2>/dev/null || true)"
    case "${VALIDATION_BUILD_SNAPSHOT}" in
        "${snapshot_root}/"*) ;;
        *) echo "FATAL: validation build is not an immutable repository snapshot: ${requested}" >&2; return 1 ;;
    esac
    python3 scripts/verify-package.py \
        --build-dir "${VALIDATION_BUILD_SNAPSHOT}" \
        --manifest manifest.json \
        --manifest-mode structure \
        --require-immutable-snapshot
}

test_dotnet() {
    heading "Compiling the repository-root RefreshKit.cs for net9.0 and net10.0"
    [ -f "${STANDALONE_PROJECT%/*}/packages.lock.json" ] || {
        echo "FATAL: standalone compile-smoke packages.lock.json is missing." >&2
        return 1
    }
    "${DOTNET}" restore "${STANDALONE_PROJECT}" --locked-mode --nologo \
        --configfile "${REPO_ROOT}/NuGet.Config" \
        "${REPRO_MSBUILD_PROPERTIES[@]}"
    "${DOTNET}" build "${STANDALONE_PROJECT}" \
        --configuration Release \
        --no-restore \
        --nologo \
        "${REPRO_MSBUILD_PROPERTIES[@]}"

    local runtimes
    runtimes="$("${DOTNET}" --list-runtimes)"
    if ! grep -q '^Microsoft.NETCore.App 9\.' <<<"${runtimes}" || \
        ! grep -q '^Microsoft.AspNetCore.App 9\.' <<<"${runtimes}"; then
        echo "FATAL: net9 tests require installed Microsoft.NETCore.App and Microsoft.AspNetCore.App 9.x runtimes; refusing a net10 roll-forward." >&2
        return 1
    fi
    if ! grep -q '^Microsoft.NETCore.App 10\.' <<<"${runtimes}" || \
        ! grep -q '^Microsoft.AspNetCore.App 10\.' <<<"${runtimes}"; then
        echo "FATAL: net10 tests require installed Microsoft.NETCore.App and Microsoft.AspNetCore.App 10.x runtimes." >&2
        return 1
    fi

    heading "Restoring the locked .NET test graph"
    "${DOTNET}" restore "${TEST_PROJECT}" --locked-mode --nologo \
        --configfile "${REPO_ROOT}/NuGet.Config" \
        "${REPRO_MSBUILD_PROPERTIES[@]}"
    heading "Running .NET tests for net9.0 and net10.0"
    "${DOTNET}" test "${TEST_PROJECT}" \
        --configuration Release \
        --no-restore \
        --nologo \
        "${REPRO_MSBUILD_PROPERTIES[@]}" \
        --logger "console;verbosity=minimal"
}

test_security_audit() {
    heading "Auditing the locked NuGet graph against the current advisory feed"
    "${DOTNET}" restore "${TEST_PROJECT}" \
        -noAutoResponse \
        --locked-mode \
        --force-evaluate \
        --nologo \
        --configfile "${REPO_ROOT}/NuGet.Config" \
        -p:ImportDirectoryBuildProps=false \
        -p:ImportDirectoryBuildTargets=false \
        -p:ImportDirectoryPackagesProps=false \
        -p:NuGetAudit=true \
        -p:NuGetAuditMode=all \
        -p:NuGetAuditLevel=low \
        -p:TreatWarningsAsErrors=false \
        '-p:WarningsAsErrors=NU1901%3BNU1902%3BNU1903%3BNU1904'
    heading "NuGet security audit passed"
}

test_browser() {
    heading "Running headless Chromium regressions"
    npm run test:browser
}

verify_packages() {
    heading "Verifying package, ABI, source identity, and manifest structure"
    python3 scripts/verify-package.py \
        --build-dir plugin/build \
        --manifest manifest.json \
        --manifest-mode structure \
        --require-immutable-snapshot
    scripts/check-package-negative.sh
}

test_fast() {
    install_browser_dependencies
    validate_static_inputs
    build_packages
    test_dotnet
    test_browser
    verify_packages
    heading "Fast gate passed"
}

test_integration() {
    install_browser_dependencies
    validate_static_inputs
    prepare_validation_build_snapshot
    local result=0 abi_floor_rc=0 abi_floor_tee_rc=0
    local jellyfin_rc=0 jellyfin_tee_rc=0 host_upgrade_rc=0 host_upgrade_tee_rc=0
    local proxy_rc=0 proxy_tee_rc=0 build_snapshot
    local -a pipeline_status=()
    local log_dir abi_floor_log jellyfin_log host_upgrade_log proxy_log
    build_snapshot="${VALIDATION_BUILD_SNAPSHOT}"
    log_dir="$(mktemp -d)"
    abi_floor_log="${log_dir}/abi-floor.log"
    jellyfin_log="${log_dir}/dual-jellyfin.log"
    host_upgrade_log="${log_dir}/host-upgrade.log"
    proxy_log="${log_dir}/proxy.log"
    export NODE_PATH="${REPO_ROOT}/node_modules${NODE_PATH:+:${NODE_PATH}}"
    export DOTNET_ROOT="${DOTNET_INSTALL_ROOT}"

    integration_cleanup() {
        bash "${REPO_ROOT}/e2e/jellyfin/run.sh" down >/dev/null 2>&1 || true
        bash "${REPO_ROOT}/e2e/proxy/run.sh" down >/dev/null 2>&1 || true
        if [ -n "${log_dir:-}" ] && [ -d "${log_dir}" ]; then
            rm -rf -- "${log_dir}"
        fi
    }
    trap integration_cleanup EXIT

    heading "Removing prior generated Jellyfin evidence before the gate"
    RK_SKIP_BUILD=1 RK_BUILD_SNAPSHOT="${build_snapshot}" \
        bash e2e/jellyfin/run.sh clean

    heading "Running the isolated exact Jellyfin 10.11.0 ABI-floor load smoke"
    set +e
    RK_SKIP_BUILD=1 RK_BUILD_SNAPSHOT="${build_snapshot}" \
        bash e2e/jellyfin/run.sh abi-floor 2>&1 | tee "${abi_floor_log}"
    pipeline_status=("${PIPESTATUS[@]}")
    abi_floor_rc="${pipeline_status[0]}"
    abi_floor_tee_rc="${pipeline_status[1]}"
    set -e
    [ "${abi_floor_rc}" -eq 0 ] && [ "${abi_floor_tee_rc}" -eq 0 ] || result=1

    heading "Running the pinned Jellyfin 10.11 + 12 lifecycle/browser lab"
    set +e
    {
        RK_SKIP_BUILD=1 RK_BUILD_SNAPSHOT="${build_snapshot}" \
            bash e2e/jellyfin/run.sh lifecycle all &&
        RK_SKIP_BUILD=1 RK_BUILD_SNAPSHOT="${build_snapshot}" \
            bash e2e/jellyfin/run.sh third-party all &&
        RK_SKIP_BUILD=1 RK_BUILD_SNAPSHOT="${build_snapshot}" \
            bash e2e/jellyfin/run.sh compat &&
        RK_SKIP_BUILD=1 RK_BUILD_SNAPSHOT="${build_snapshot}" \
            bash e2e/jellyfin/run.sh browser all
    } 2>&1 | tee "${jellyfin_log}"
    pipeline_status=("${PIPESTATUS[@]}")
    jellyfin_rc="${pipeline_status[0]}"
    jellyfin_tee_rc="${pipeline_status[1]}"
    set -e
    [ "${jellyfin_rc}" -eq 0 ] && [ "${jellyfin_tee_rc}" -eq 0 ] || result=1

    heading "Running both pinned in-place Jellyfin host-upgrade paths"
    set +e
    RK_SKIP_BUILD=1 RK_BUILD_SNAPSHOT="${build_snapshot}" \
        bash e2e/jellyfin/run.sh host-upgrade all 2>&1 | tee "${host_upgrade_log}"
    pipeline_status=("${PIPESTATUS[@]}")
    host_upgrade_rc="${pipeline_status[0]}"
    host_upgrade_tee_rc="${pipeline_status[1]}"
    set -e
    [ "${host_upgrade_rc}" -eq 0 ] && [ "${host_upgrade_tee_rc}" -eq 0 ] || result=1
    bash e2e/jellyfin/run.sh down >/dev/null 2>&1 || result=1

    heading "Running the Jellyfin 10.11 reverse-proxy/browser matrix"
    set +e
    RK_SKIP_BUILD=1 RK_BUILD_SNAPSHOT="${build_snapshot}" \
        bash e2e/proxy/run.sh all 2>&1 | tee "${proxy_log}"
    pipeline_status=("${PIPESTATUS[@]}")
    proxy_rc="${pipeline_status[0]}"
    proxy_tee_rc="${pipeline_status[1]}"
    set -e
    [ "${proxy_rc}" -eq 0 ] && [ "${proxy_tee_rc}" -eq 0 ] || result=1
    bash e2e/proxy/run.sh down >/dev/null 2>&1 || result=1

    python3 scripts/collect-ci-evidence.py \
        --output "${RK_RESULTS_DIR:-test-results}" \
        --build-dir "${build_snapshot}" \
        --abi-floor-exit "${abi_floor_rc}" \
        --jellyfin-exit "${jellyfin_rc}" \
        --host-upgrade-exit "${host_upgrade_rc}" \
        --proxy-exit "${proxy_rc}" \
        --require-integration-evidence \
        --log "abi-floor=${abi_floor_log}" \
        --log "dual-jellyfin=${jellyfin_log}" \
        --log "host-upgrade=${host_upgrade_log}" \
        --log "proxy=${proxy_log}" || result=1

    integration_cleanup
    trap - EXIT
    [ "${result}" -ne 0 ] || heading "Integration gate passed"
    return "${result}"
}

test_compatibility() {
    # The static negative gates below run the Node-based browser-driver
    # self-tests, and the fast/integration paths install the locked
    # dependencies before reaching them; this path must do the same.
    install_browser_dependencies
    validate_static_inputs
    prepare_validation_build_snapshot
    local result=0 compatibility_rc=0 build_snapshot
    build_snapshot="${VALIDATION_BUILD_SNAPSHOT}"
    compatibility_cleanup() {
        bash "${REPO_ROOT}/e2e/compat/run.sh" down >/dev/null 2>&1 || true
    }
    trap compatibility_cleanup EXIT
    heading "Running the pinned third-party and hostile-fixture compatibility matrices"
    set +e
    RK_COMPAT_ALLOW_CONTAINERS=1 RK_COMPAT_BUILD_SNAPSHOT="${build_snapshot}" \
        bash e2e/compat/run.sh all
    compatibility_rc=$?
    set -e
    [ "${compatibility_rc}" -eq 0 ] || result=1
    python3 scripts/collect-ci-evidence.py \
        --output "${RK_RESULTS_DIR:-test-results}" \
        --build-dir "${build_snapshot}" \
        --compatibility-exit "${compatibility_rc}" \
        --require-compatibility-evidence || result=1
    compatibility_cleanup
    trap - EXIT
    [ "${result}" -ne 0 ] || heading "Compatibility gate passed"
    return "${result}"
}

test_all() {
    local results_root="${RK_RESULTS_DIR:-test-results}"
    test_fast
    test_security_audit
    scripts/check-reproducible.sh
    scripts/check-build-lock.sh
    RK_RESULTS_DIR="${results_root}/integration" test_integration
    RK_RESULTS_DIR="${results_root}/compatibility" test_compatibility
    heading "Complete validation passed"
}

case "${1:-fast}" in
    fast)            test_fast ;;
    build)           build_packages ;;
    dotnet)          test_dotnet ;;
    browser)         install_browser_dependencies; test_browser ;;
    static)          validate_static_inputs ;;
    package)         verify_packages ;;
    reproducibility) scripts/check-reproducible.sh; scripts/check-build-lock.sh ;;
    locking)         exec scripts/check-build-lock.sh ;;
    integration)     test_integration ;;
    compatibility)   test_compatibility ;;
    security-audit)  test_security_audit ;;
    all)             test_all ;;
    *)
        echo "Usage: $0 [fast|build|dotnet|browser|static|package|reproducibility|locking|integration|compatibility|security-audit|all]" >&2
        exit 2
        ;;
esac
