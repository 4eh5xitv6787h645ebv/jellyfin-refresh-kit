#!/usr/bin/env bash

# Immutable package selection shared by the proxy runner and its provisioner.
# Source this file; do not execute it directly.

set -euo pipefail

rk_proxy_snapshot_die() {
    printf 'FATAL: %s\n' "$*" >&2
    return 1
}

rk_proxy_resolve_build_snapshot() {
    if [ "$#" -ne 2 ]; then
        rk_proxy_snapshot_die "rk_proxy_resolve_build_snapshot requires REPO and PATH"
        return 1
    fi

    local repo requested resolved snapshot_root
    repo="$(cd "$1" && pwd -P)" || return $?
    requested="$2"
    resolved="$(readlink -f -- "${requested}" 2>/dev/null || true)"
    snapshot_root="$(readlink -f -- "${repo}/plugin/.builds" 2>/dev/null || true)"

    [ -n "${resolved}" ] && [ -d "${resolved}" ] || {
        rk_proxy_snapshot_die "plugin build snapshot does not exist: ${requested}"
        return 1
    }
    [ -n "${snapshot_root}" ] && [ -d "${snapshot_root}" ] || {
        rk_proxy_snapshot_die "immutable build root does not exist: ${repo}/plugin/.builds"
        return 1
    }
    [ "$(dirname -- "${resolved}")" = "${snapshot_root}" ] || {
        rk_proxy_snapshot_die "plugin build resolved outside plugin/.builds: ${resolved}"
        return 1
    }
    case "$(basename -- "${resolved}")" in
        .*|'')
            rk_proxy_snapshot_die "plugin build is not a finalized snapshot: ${resolved}"
            return 1
            ;;
    esac

    printf '%s\n' "${resolved}"
}

rk_proxy_verify_build_snapshot() {
    if [ "$#" -ne 2 ]; then
        rk_proxy_snapshot_die "rk_proxy_verify_build_snapshot requires REPO and SNAPSHOT"
        return 1
    fi

    local repo snapshot resolved
    repo="$(cd "$1" && pwd -P)" || return $?
    snapshot="$2"
    resolved="$(rk_proxy_resolve_build_snapshot "${repo}" "${snapshot}")" || return $?
    [ "${resolved}" = "${snapshot}" ] || {
        rk_proxy_snapshot_die "RK_BUILD_SNAPSHOT must be the canonical immutable path: ${snapshot}"
        return 1
    }

    python3 "${repo}/scripts/verify-package.py" \
        --build-dir "${snapshot}" \
        --manifest "${repo}/manifest.json" \
        --manifest-mode structure \
        --require-immutable-snapshot
}

rk_proxy_pin_build_snapshot() {
    if [ "$#" -ne 1 ]; then
        rk_proxy_snapshot_die "rk_proxy_pin_build_snapshot requires REPO"
        return 1
    fi

    local repo requested resolved selected_dotnet launcher
    repo="$(cd "$1" && pwd -P)" || return $?

    if [ "${RK_SKIP_BUILD:-0}" = "1" ]; then
        [ -n "${RK_BUILD_SNAPSHOT:-}" ] || {
            rk_proxy_snapshot_die "RK_SKIP_BUILD=1 requires an explicit RK_BUILD_SNAPSHOT"
            return 1
        }
        requested="${RK_BUILD_SNAPSHOT}"
    else
        [ -z "${RK_BUILD_SNAPSHOT:-}" ] || {
            rk_proxy_snapshot_die "set RK_SKIP_BUILD=1 when selecting RK_BUILD_SNAPSHOT"
            return 1
        }

        selected_dotnet=""
        if [ -n "${DOTNET_ROOT:-}" ] && [ -x "${DOTNET_ROOT}/dotnet" ]; then
            selected_dotnet="${DOTNET_ROOT}"
        elif [ -x "${HOME}/.dotnet/dotnet" ]; then
            selected_dotnet="${HOME}/.dotnet"
        else
            launcher="$(command -v dotnet || true)"
            if [ -n "${launcher}" ]; then
                selected_dotnet="$(python3 -c 'import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "${launcher}")"
            fi
        fi
        [ -x "${selected_dotnet}/dotnet" ] || {
            rk_proxy_snapshot_die "pinned .NET SDK launcher not found at ${selected_dotnet}/dotnet"
            return 1
        }

        printf '==> building the standalone plugin from current source\n'
        DOTNET_ROOT="${selected_dotnet}" bash "${repo}/plugin/build.sh" || return $?
        requested="${repo}/plugin/build"
    fi

    resolved="$(rk_proxy_resolve_build_snapshot "${repo}" "${requested}")" || return $?
    rk_proxy_verify_build_snapshot "${repo}" "${resolved}" || return $?
    RK_BUILD_SNAPSHOT="${resolved}"
    export RK_BUILD_SNAPSHOT
    printf '==> pinned plugin snapshot %s for the full proxy run\n' \
        "$(basename -- "${RK_BUILD_SNAPSHOT}")"
}

rk_proxy_require_pinned_build_snapshot() {
    if [ "$#" -ne 1 ]; then
        rk_proxy_snapshot_die "rk_proxy_require_pinned_build_snapshot requires REPO"
        return 1
    fi
    [ -n "${RK_BUILD_SNAPSHOT:-}" ] || {
        rk_proxy_snapshot_die "provisioning requires a pinned RK_BUILD_SNAPSHOT from run.sh"
        return 1
    }
    rk_proxy_verify_build_snapshot "$1" "${RK_BUILD_SNAPSHOT}"
}
