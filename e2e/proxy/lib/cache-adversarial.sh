#!/usr/bin/env bash
#
# The misconfigured-cache case, expressed as executable expectations:
#   1. prime a unique URL in every cache
#   2. change a loose client asset and require the origin generation to move
#   3. require the deliberately naive cache to keep serving the OLD shell and
#      generation (the positive control)
#   4. require every remedy to track the new origin immediately
#
# Run after provision.sh. Any violated expectation makes the suite fail.
set -euo pipefail

CONTAINER="${RK_CONTAINER:?RK_CONTAINER must be the verified project-scoped origin id}"
JE_VERSION="${RK_JE_VERSION:-12.1.0.0}"
BUMP="${RK_BUMP_FILE:-/config/plugins/Jellyfin Enhanced_${JE_VERSION}/rk-e2e-generation.js}"

NAIVE="${RK_PROXY_CACHE_NAIVE_PORT:-8122}"
RESPECT="${RK_PROXY_CACHE_RESPECT_PORT:-8124}"
FIX1="${RK_PROXY_CACHE_FIX1_PORT:-8126}"
FIX2="${RK_PROXY_CACHE_FIX2_PORT:-8127}"
ORIGIN="${RK_PROXY_ORIGIN_PORT:-8116}"
RUN_ID="$(date -u +%s%N)-$$"
QUERY="rk-cache-run=${RUN_ID}"
pass=0
fail=0

shell_stamp() {
    curl -fsS "http://127.0.0.1:$1/web/?$QUERY" \
        | grep -oE 'kit\.js\?v=[^"]+' | head -1 || true
}
gen() {
    curl -fsS "http://127.0.0.1:$1/RefreshKit/Generation.txt?$QUERY" \
        | tr -d '\r\n' || true
}
cache_status() {
    curl -fsS -o /dev/null -D - "http://127.0.0.1:$1/web/?$QUERY" \
        | grep -i '^x-cache-status' | tail -1 | tr -d '\r' || true
}
check_equal() {
    local label="$1" expected="$2" actual="$3"
    if [ "$actual" = "$expected" ]; then
        printf '  PASS %-43s %s\n' "$label" "$actual"
        pass=$((pass + 1))
    else
        printf '  FAIL %-43s expected %s, got %s\n' "$label" "$expected" "${actual:-<empty>}"
        fail=$((fail + 1))
    fi
}
check_changed() {
    local label="$1" before="$2" after="$3"
    if [ -n "$after" ] && [ "$after" != "$before" ]; then
        printf '  PASS %-43s %s -> %s\n' "$label" "$before" "$after"
        pass=$((pass + 1))
    else
        printf '  FAIL %-43s remained %s\n' "$label" "${after:-<empty>}"
        fail=$((fail + 1))
    fi
}

echo "### 1. prime every cache"
origin_before="$(gen "$ORIGIN")"
naive_stamp_before=""
for p in $NAIVE $RESPECT $FIX1 $FIX2; do
    stamp="$(shell_stamp "$p")"
    generation="$(gen "$p")"
    check_equal ":$p primed generation matches origin" "$origin_before" "$generation"
    check_equal ":$p primed shell carries origin" "kit.js?v=$origin_before" "$stamp"
    if [ "$p" = "$NAIVE" ]; then
        naive_stamp_before="$stamp"
    fi
done
printf '  INFO  origin generation: %s\n' "$origin_before"
printf '  INFO  naive cache after prime: %s\n' "$(cache_status "$NAIVE")"

echo
echo "### 2. move the generation at the origin (replace a loose client asset)"
printf '// refresh-kit cache test %s\n' "$RUN_ID" \
    | docker exec -i "$CONTAINER" tee "$BUMP" >/dev/null
origin_after="$origin_before"
for _ in $(seq 1 20); do
    sleep 1
    origin_after="$(gen "$ORIGIN")"
    [ -n "$origin_after" ] && [ "$origin_after" != "$origin_before" ] && break
done
check_changed "origin generation moved" "$origin_before" "$origin_after"
check_equal "origin shell carries new generation" \
    "kit.js?v=$origin_after" "$(shell_stamp "$ORIGIN")"

echo
echo "### 3. the broken control must be stale"
check_equal "naive generation remains stale" "$origin_before" "$(gen "$NAIVE")"
check_equal "naive shell remains stale" "$naive_stamp_before" "$(shell_stamp "$NAIVE")"
case "$(cache_status "$NAIVE")" in
    *HIT*) printf '  PASS %-43s %s\n' "naive shell is a cache hit" "$(cache_status "$NAIVE")"; pass=$((pass + 1)) ;;
    *)     printf '  FAIL %-43s %s\n' "naive shell is a cache hit" "$(cache_status "$NAIVE")"; fail=$((fail + 1)) ;;
esac

echo
echo "### 4. every remedy must follow the origin"
for p in $RESPECT $FIX1 $FIX2; do
    check_equal ":$p generation follows origin" "$origin_after" "$(gen "$p")"
    check_equal ":$p shell follows origin" "kit.js?v=$origin_after" "$(shell_stamp "$p")"
done

echo
printf '%s\n' "---- adversarial cache: $pass passed, $fail failed"
exit "$fail"
