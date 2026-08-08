#!/usr/bin/env bash
# Launch two package builds together. They share one fixed compiler scratch
# path, so both can succeed only when build.sh serializes them and atomically
# reuses the same immutable snapshot.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

for required in flock timeout python3; do
    command -v "${required}" >/dev/null 2>&1 || {
        echo "FATAL: ${required} is required for the concurrent build gate." >&2
        exit 1
    }
done

LOG_DIR="$(mktemp -d)"
FIRST_PID=""
SECOND_PID=""
RELEASE_FILE="${LOG_DIR}/release-first"
cleanup() {
    : > "${RELEASE_FILE}" 2>/dev/null || true
    for pid in "${FIRST_PID:-}" "${SECOND_PID:-}"; do
        [ -z "${pid}" ] || ! kill -0 "${pid}" 2>/dev/null || kill "${pid}" 2>/dev/null || true
    done
    if [ -n "${LOG_DIR:-}" ] && [ -d "${LOG_DIR}" ]; then
        chmod -R u+w "${LOG_DIR}" 2>/dev/null || true
        rm -rf -- "${LOG_DIR}"
    fi
}
trap cleanup EXIT

wait_for_file() {
    local path="$1" description="$2"
    local attempt
    for ((attempt = 0; attempt < 600; attempt++)); do
        [ -e "${path}" ] && return 0
        sleep 0.1
    done
    echo "FATAL: timed out waiting for ${description}: ${path}" >&2
    return 1
}

FIRST_ATTEMPT="${LOG_DIR}/first-attempt"
FIRST_ACQUIRED="${LOG_DIR}/first-acquired"
SECOND_ATTEMPT="${LOG_DIR}/second-attempt"
SECOND_BLOCKED="${LOG_DIR}/second-blocked"
SECOND_ACQUIRED="${LOG_DIR}/second-acquired"

echo "==> Holding the first build after it acquires the package lock"
timeout --kill-after=10 240 env \
    RK_BUILD_TEST_ATTEMPT_FILE="${FIRST_ATTEMPT}" \
    RK_BUILD_TEST_ACQUIRED_FILE="${FIRST_ACQUIRED}" \
    RK_BUILD_TEST_RELEASE_FILE="${RELEASE_FILE}" \
    ./plugin/build.sh >"${LOG_DIR}/first.log" 2>&1 &
FIRST_PID=$!
wait_for_file "${FIRST_ATTEMPT}" "the first build to reach flock"
wait_for_file "${FIRST_ACQUIRED}" "the first build to acquire the lock"
if flock --nonblock "${REPO_ROOT}/plugin/.build.lock" true; then
    echo "FATAL: an independent nonblocking probe acquired the build lock while the first build owns it." >&2
    exit 1
fi
echo "    independent nonblocking probe was excluded by the held lock"

echo "==> Starting the second build while the first demonstrably owns the lock"
timeout --kill-after=10 480 env \
    RK_BUILD_TEST_ATTEMPT_FILE="${SECOND_ATTEMPT}" \
    RK_BUILD_TEST_BLOCKED_FILE="${SECOND_BLOCKED}" \
    RK_BUILD_TEST_ACQUIRED_FILE="${SECOND_ACQUIRED}" \
    ./plugin/build.sh >"${LOG_DIR}/second.log" 2>&1 &
SECOND_PID=$!
wait_for_file "${SECOND_ATTEMPT}" "the second build to reach flock"
wait_for_file "${SECOND_BLOCKED}" "the second build's rejected nonblocking lock attempt"
[ ! -e "${SECOND_ACQUIRED}" ] || {
    echo "FATAL: the second build crossed the post-lock marker while the first still held the lock." >&2
    exit 1
}
if flock --nonblock "${REPO_ROOT}/plugin/.build.lock" true; then
    echo "FATAL: lock exclusion disappeared before the first build was released." >&2
    exit 1
fi
echo "    second build was deterministically excluded until release"

: > "${RELEASE_FILE}"

set +e
wait "${FIRST_PID}"
FIRST_RC=$?
FIRST_PID=""
wait "${SECOND_PID}"
SECOND_RC=$?
SECOND_PID=""
set -e

if [ "${FIRST_RC}" -ne 0 ] || [ "${SECOND_RC}" -ne 0 ]; then
    echo "FATAL: concurrent builds failed (first=${FIRST_RC}, second=${SECOND_RC})" >&2
    echo "--- first build ---" >&2
    sed -n '1,260p' "${LOG_DIR}/first.log" >&2
    echo "--- second build ---" >&2
    sed -n '1,260p' "${LOG_DIR}/second.log" >&2
    exit 1
fi

[ -e "${SECOND_ACQUIRED}" ] || {
    echo "FATAL: the second build never acquired the lock after the first released it." >&2
    exit 1
}

python3 - "${LOG_DIR}/first.log" "${LOG_DIR}/second.log" <<'PY'
import pathlib
import re
import sys

identities = []
for value in sys.argv[1:]:
    text = pathlib.Path(value).read_text(encoding="utf-8", errors="replace")
    revision = re.search(r"source revision\s*:\s*([0-9a-f]{40})", text)
    tree = re.search(r"source tree\s*:\s*([0-9a-f]{64})", text)
    snapshot = re.search(r"immutable snapshot:\s*(.+)", text)
    if not revision or not tree or not snapshot:
        raise SystemExit(f"FATAL: build log lacks source/snapshot identity: {value}")
    identities.append((revision.group(1), tree.group(1), pathlib.Path(snapshot.group(1)).name))
if len(set(identities)) != 1:
    raise SystemExit(f"FATAL: concurrent builds published different identities: {identities}")
print(f"    shared identity: {identities[0][0]} / {identities[0][1]}")
print(f"    shared snapshot: {identities[0][2]}")
PY

test -L plugin/build || { echo "FATAL: plugin/build is not an atomic snapshot symlink." >&2; exit 1; }
RESOLVED_SNAPSHOT="$(readlink -f plugin/build)"
case "${RESOLVED_SNAPSHOT}" in
    "${REPO_ROOT}/plugin/.builds/"*) ;;
    *) echo "FATAL: plugin/build resolved outside plugin/.builds: ${RESOLVED_SNAPSHOT}" >&2; exit 1 ;;
esac

if find plugin/.builds -maxdepth 1 -type d -name '.candidate.*' -print -quit | grep -q . || \
   [ -d plugin/.build-work/current ]; then
    echo "FATAL: concurrent builds left a partial candidate/work directory." >&2
    exit 1
fi

python3 scripts/verify-package.py \
    --build-dir "${RESOLVED_SNAPSHOT}" \
    --manifest manifest.json \
    --manifest-mode structure \
    --require-immutable-snapshot

echo "==> Concurrent build locking verification passed"
