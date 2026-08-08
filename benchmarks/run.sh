#!/usr/bin/env bash
# Opt-in, non-gating performance observations. JSONL goes to stdout; build
# progress goes to stderr so callers can redirect a clean machine-readable run.

set -euo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${BENCH_DIR}/.." && pwd)"
SERVER_PROJECT="${BENCH_DIR}/server/RefreshKit.Benchmarks.csproj"

if [ -n "${DOTNET_ROOT:-}" ] && [ -x "${DOTNET_ROOT}/dotnet" ]; then
    DOTNET="${DOTNET_ROOT}/dotnet"
elif [ -x "${HOME}/.dotnet/dotnet" ]; then
    DOTNET="${HOME}/.dotnet/dotnet"
else
    DOTNET="$(command -v dotnet || true)"
fi
[ -n "${DOTNET}" ] && [ -x "${DOTNET}" ] || {
    echo "FATAL: install the .NET SDK pinned by global.json." >&2
    exit 1
}

command -v node >/dev/null 2>&1 || {
    echo "FATAL: Node.js is required." >&2
    exit 1
}
NODE_MAJOR="$(node -p 'Number(process.versions.node.split(".")[0])')"
[ "${NODE_MAJOR}" -ge 20 ] || {
    echo "FATAL: Node.js 20 or newer is required (see .node-version)." >&2
    exit 1
}
(
    cd "${REPO_ROOT}"
    node -e "require.resolve('puppeteer')" >/dev/null
) || {
    echo "FATAL: browser dependencies are missing; run npm ci." >&2
    exit 1
}

export DOTNET_CLI_TELEMETRY_OPTOUT=1
export DOTNET_NOLOGO=1
export NUGET_XMLDOC_MODE=skip

# SDK discovery walks up from the process working directory. Enter the
# repository so global.json is honored even when this script is called by an
# absolute path from elsewhere.
cd "${REPO_ROOT}"

export RK_BENCH_SOURCE_REVISION=unknown
export RK_BENCH_SOURCE_DIRTY=unknown
if git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    export RK_BENCH_SOURCE_REVISION
    RK_BENCH_SOURCE_REVISION="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
    export RK_BENCH_SOURCE_DIRTY=false
    if [ -n "$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=all)" ]; then
        RK_BENCH_SOURCE_DIRTY=true
    fi
fi

echo "==> Restoring and compiling the server benchmark" >&2
"${DOTNET}" restore "${SERVER_PROJECT}" \
    --nologo \
    --configfile "${REPO_ROOT}/NuGet.Config" \
    -p:NuGetAudit=false \
    1>&2
"${DOTNET}" build "${SERVER_PROJECT}" \
    --configuration Release \
    --no-restore \
    --nologo \
    --verbosity quiet \
    -p:NuGetAudit=false \
    1>&2

echo "==> Measuring provider, stamper, and middleware" >&2
"${DOTNET}" "${BENCH_DIR}/server/bin/Release/net10.0/RefreshKit.Benchmarks.dll"

echo "==> Measuring browser multi-copy registration" >&2
(
    cd "${REPO_ROOT}"
    node "${BENCH_DIR}/browser-registration.cjs"
)
