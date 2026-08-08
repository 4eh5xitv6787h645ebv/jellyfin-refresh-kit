#!/usr/bin/env bash

# Container-free regression for the proxy build handoff. The public build link
# is deliberately moved after selection; consumers must retain snapshot-a's
# canonical directory and must reject aliases outside the immutable root.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=build-snapshot.sh
# Resolved from this script's directory at runtime.
# shellcheck disable=SC1091
source "${HERE}/build-snapshot.sh"

FIXTURE_ROOT="$(mktemp -d)"
cleanup() {
    rm -rf -- "${FIXTURE_ROOT}"
}
trap cleanup EXIT

REPO="${FIXTURE_ROOT}/repo"
SNAPSHOT_ROOT="${REPO}/plugin/.builds"
SNAPSHOT_A="${SNAPSHOT_ROOT}/snapshot-a"
SNAPSHOT_B="${SNAPSHOT_ROOT}/snapshot-b"
mkdir -p \
    "${SNAPSHOT_A}/stage" \
    "${SNAPSHOT_B}/stage" \
    "${FIXTURE_ROOT}/outside" \
    "${REPO}/scripts"
printf 'a\n' > "${SNAPSHOT_A}/stage/identity"
printf 'b\n' > "${SNAPSHOT_B}/stage/identity"
printf 'raise SystemExit(0)\n' > "${REPO}/scripts/verify-package.py"
printf '[]\n' > "${REPO}/manifest.json"
ln -s '.builds/snapshot-a' "${REPO}/plugin/build"

# Exercise the same explicit handoff used by test.sh. The fixture verifier is a
# stub because this test concerns identity selection; production calls the real
# package/meta verifier at both boundaries.
export RK_SKIP_BUILD=1
export RK_BUILD_SNAPSHOT="${SNAPSHOT_A}"
rk_proxy_pin_build_snapshot "${REPO}" >/dev/null
PINNED="${RK_BUILD_SNAPSHOT}"
[ "${PINNED}" = "${SNAPSHOT_A}" ] || {
    echo "FAIL: explicit handoff did not pin snapshot-a" >&2
    exit 1
}

# Model another build's atomic publication between the caller's pin and the
# provisioner's use. The retained canonical path must remain snapshot-a even
# though a fresh resolution of plugin/build now selects snapshot-b.
ln -s '.builds/snapshot-b' "${REPO}/plugin/build.next"
mv -Tf -- "${REPO}/plugin/build.next" "${REPO}/plugin/build"
PUBLIC_NOW="$(rk_proxy_resolve_build_snapshot "${REPO}" "${REPO}/plugin/build")"
rk_proxy_require_pinned_build_snapshot "${REPO}" >/dev/null
RETAINED="${RK_BUILD_SNAPSHOT}"
[ "${PUBLIC_NOW}" = "${SNAPSHOT_B}" ] || {
    echo "FAIL: fixture did not move the public link to snapshot-b" >&2
    exit 1
}
if [ "${RETAINED}" != "${SNAPSHOT_A}" ] || \
    [ "$(cat "${RETAINED}/stage/identity")" != a ]; then
        echo "FAIL: pinned proxy snapshot followed a later public-link update" >&2
        exit 1
fi

# A process boundary must receive the canonical directory, not another mutable
# alias. The verifier stub would accept it, so success here can only come from
# missing canonical-path enforcement.
RK_BUILD_SNAPSHOT="${REPO}/plugin/build"
if rk_proxy_require_pinned_build_snapshot "${REPO}" >/dev/null 2>&1; then
    echo "FAIL: provisioning accepted a mutable snapshot alias" >&2
    exit 1
fi
RK_BUILD_SNAPSHOT="${PINNED}"

if (
    unset RK_BUILD_SNAPSHOT
    RK_SKIP_BUILD=1 rk_proxy_pin_build_snapshot "${REPO}"
) >/dev/null 2>&1; then
    echo "FAIL: RK_SKIP_BUILD=1 accepted a missing explicit snapshot" >&2
    exit 1
fi

ln -s "${FIXTURE_ROOT}/outside" "${REPO}/plugin/outside-build"
if rk_proxy_resolve_build_snapshot "${REPO}" "${REPO}/plugin/outside-build" >/dev/null 2>&1; then
    echo "FAIL: resolver accepted a directory outside plugin/.builds" >&2
    exit 1
fi

mkdir -p "${SNAPSHOT_ROOT}/.candidate-unfinished"
if rk_proxy_resolve_build_snapshot "${REPO}" "${SNAPSHOT_ROOT}/.candidate-unfinished" >/dev/null 2>&1; then
    echo "FAIL: resolver accepted an unfinished candidate directory" >&2
    exit 1
fi

mkdir -p "${SNAPSHOT_A}/nested"
if rk_proxy_resolve_build_snapshot "${REPO}" "${SNAPSHOT_A}/nested" >/dev/null 2>&1; then
    echo "FAIL: resolver accepted a nested non-snapshot directory" >&2
    exit 1
fi

if rk_proxy_resolve_build_snapshot "${REPO}" "${FIXTURE_ROOT}/missing" >/dev/null 2>&1; then
    echo "FAIL: resolver accepted a missing snapshot" >&2
    exit 1
fi

echo "Proxy immutable-snapshot handoff negative tests passed."
