#!/usr/bin/env bash

# Parse GitHub Actions workflows with one pinned upstream actionlint binary.
# The verified release archive is cached outside the checkout; its SHA-256 is
# rechecked on every invocation before anything is executed.

set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION='1.7.10'
BASE_URL="https://github.com/rhysd/actionlint/releases/download/v${VERSION}"

case "$(uname -s):$(uname -m)" in
    Linux:x86_64)
        ARCHIVE="actionlint_${VERSION}_linux_amd64.tar.gz"
        ARCHIVE_SHA256='f4c76b71db5755a713e6055cbb0857ed07e103e028bda117817660ebadb4386f'
        ;;
    Linux:aarch64|Linux:arm64)
        ARCHIVE="actionlint_${VERSION}_linux_arm64.tar.gz"
        ARCHIVE_SHA256='cd3dfe5f66887ec6b987752d8d9614e59fd22f39415c5ad9f28374623f41773a'
        ;;
    *)
        printf 'FATAL: pinned actionlint is available only for Linux amd64/arm64\n' >&2
        exit 1
        ;;
esac

for command in curl flock mktemp sha256sum tar; do
    command -v "${command}" >/dev/null 2>&1 || {
        printf 'FATAL: workflow validation requires %s\n' "${command}" >&2
        exit 1
    }
done

CACHE_ROOT="${RK_ACTIONLINT_CACHE_DIR:-${TMPDIR:-/tmp}/jellyfin-refresh-kit-actionlint-${UID}}"
case "${CACHE_ROOT}" in
    /*) ;;
    *) printf 'FATAL: RK_ACTIONLINT_CACHE_DIR must be absolute\n' >&2; exit 1 ;;
esac
mkdir -p -- "${CACHE_ROOT}"
chmod 700 -- "${CACHE_ROOT}"
CACHED_ARCHIVE="${CACHE_ROOT}/${ARCHIVE}"

exec {LOCK_FD}>"${CACHE_ROOT}/download.lock"
flock "${LOCK_FD}"
if [ ! -f "${CACHED_ARCHIVE}" ] ||
   ! printf '%s  %s\n' "${ARCHIVE_SHA256}" "${CACHED_ARCHIVE}" | sha256sum -c - >/dev/null 2>&1; then
    PART="${CACHED_ARCHIVE}.part-${BASHPID}"
    trap 'rm -f -- "${PART:-}"' EXIT
    curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
        --output "${PART}" "${BASE_URL}/${ARCHIVE}"
    printf '%s  %s\n' "${ARCHIVE_SHA256}" "${PART}" | sha256sum -c - >/dev/null
    mv -f -- "${PART}" "${CACHED_ARCHIVE}"
    trap - EXIT
fi
flock -u "${LOCK_FD}"
exec {LOCK_FD}>&-

WORK="$(mktemp -d)"
trap 'rm -rf -- "${WORK}"' EXIT
tar -xzf "${CACHED_ARCHIVE}" -C "${WORK}" actionlint
[ -x "${WORK}/actionlint" ] || {
    printf 'FATAL: verified actionlint archive did not contain its executable\n' >&2
    exit 1
}
"${WORK}/actionlint" -version | grep -F "${VERSION}" >/dev/null

mapfile -d '' WORKFLOWS < <(find "${ROOT}/.github/workflows" -maxdepth 1 -type f \
    \( -name '*.yml' -o -name '*.yaml' \) -print0 | sort -z)
[ "${#WORKFLOWS[@]}" -gt 0 ] || {
    printf 'FATAL: no GitHub Actions workflows were found\n' >&2
    exit 1
}
"${WORK}/actionlint" -no-color -shellcheck= -pyflakes= "${WORKFLOWS[@]}"
