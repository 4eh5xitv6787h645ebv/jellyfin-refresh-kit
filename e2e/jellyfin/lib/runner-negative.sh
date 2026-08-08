#!/usr/bin/env bash

# Fast no-Docker regression for Bash errexit suppression. A lifecycle function
# invoked from an OR-list/conditional does not inherit reliable `set -e`
# behavior, so every wrapper must propagate the simulated Node exit explicitly.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../run.sh
source "${HERE}/../run.sh"

declare -a EVENTS=()

fail() {
    printf 'FAIL runner-negative: %s\n' "$*" >&2
    exit 1
}

event_seen() {
    local wanted="$1" event
    for event in "${EVENTS[@]}"; do
        [ "${event}" = "${wanted}" ] && return 0
    done
    return 1
}

# Isolate orchestration from every external dependency. Returning 37 from this
# function models browser-third-party-lifecycle.cjs exiting nonzero.
check_prerequisites() { EVENTS+=("prerequisites"); }
rk_require() { EVENTS+=("require:$*"); }
rk_pin_build_snapshot() { EVENTS+=("pin"); RK_BUILD_SNAPSHOT='/fixture/snapshot'; }
ensure_stages() { EVENTS+=("stages"); }
prepare_third_party_target() { EVENTS+=("prepare:$1"); }
rk_compose() { EVENTS+=("compose:$*"); }
rk_assert_container_image() { EVENTS+=("image:$1"); }
run_third_party_target() {
    EVENTS+=("node:$1")
    [ "$1" != jf10 ] || return 37
    return 0
}
bash() { EVENTS+=("bash:$*"); }

EVENTS=()
if third_party_target jf10; then
    direct_status=0
else
    direct_status=$?
fi
[ "${direct_status}" -eq 37 ] || \
    fail "third_party_target swallowed simulated Node exit 37 as ${direct_status}"
event_seen "node:jf10" || fail "direct target never invoked the simulated Node lifecycle"
for event in "${EVENTS[@]}"; do
    case "${event}" in
        bash:*check.sh*jf10) fail "post-check ran after the simulated jf10 Node failure" ;;
    esac
done

EVENTS=()
if cmd_third_party all; then
    third_party_status=0
else
    third_party_status=$?
fi
[ "${third_party_status}" -eq 1 ] || \
    fail "third-party all swallowed its failed target as ${third_party_status}"
event_seen "node:jf10" || fail "third-party all did not execute jf10"
event_seen "node:jf12" || fail "third-party all did not continue to jf12"
for event in "${EVENTS[@]}"; do
    case "${event}" in
        bash:*check.sh*jf10) fail "jf10 post-check overwrote the simulated lifecycle failure" ;;
    esac
done

cmd_lifecycle() { EVENTS+=("self:$*"); }
cmd_browser() { EVENTS+=("browser:$*"); }
cmd_compat() { EVENTS+=("compat"); }

EVENTS=()
if cmd_all; then
    all_status=0
else
    all_status=$?
fi
[ "${all_status}" -eq 1 ] || fail "all swallowed third-party failure as ${all_status}"
event_seen "self:all" || fail "all did not execute self-lifecycle first"
event_seen "node:jf10" || fail "all did not reach the failing jf10 third-party lifecycle"
event_seen "node:jf12" || fail "all did not finish collecting the jf12 third-party result"
event_seen "browser:all" && fail "all continued to browser after third-party failure"
event_seen "compat" && fail "all continued to compatibility after third-party failure"

printf 'PASS runner-negative: Node failure propagated through target, third-party all, and all\n'
