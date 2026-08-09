#!/usr/bin/env bash
# Container-free regressions for proxy configuration, HTTP contracts and the
# fail-closed injector parking state machine.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_DIR="$(cd "$HERE/.." && pwd)"
TMP="$(mktemp -d)"
FIXTURE_PID=''
FIXTURE_PORT=''

cleanup() {
    if [ -n "$FIXTURE_PID" ]; then
        kill "$FIXTURE_PID" 2>/dev/null || true
        wait "$FIXTURE_PID" 2>/dev/null || true
    fi
    rm -rf -- "$TMP"
}
trap cleanup EXIT

die() {
    echo "FAIL: $*" >&2
    exit 1
}

start_fixture() {
    local name="$1"
    shift
    local port_file="$TMP/$name.port"
    python3 "$HERE/matrix-fixture.py" --port-file "$port_file" "$@" &
    FIXTURE_PID=$!
    for _ in $(seq 1 100); do
        if [ -s "$port_file" ]; then
            FIXTURE_PORT="$(tr -d '[:space:]' < "$port_file")"
            return 0
        fi
        if ! kill -0 "$FIXTURE_PID" 2>/dev/null; then
            wait "$FIXTURE_PID" || true
            die "$name HTTP fixture exited before publishing its port"
        fi
        sleep 0.05
    done
    die "$name HTTP fixture did not publish its port"
}

stop_fixture() {
    kill "$FIXTURE_PID" 2>/dev/null || true
    wait "$FIXTURE_PID" 2>/dev/null || true
    FIXTURE_PID=''
    FIXTURE_PORT=''
}

for script in "$PROXY_DIR/run.sh" "$HERE"/*.sh; do
    bash -n "$script"
done
PYTHONPYCACHEPREFIX="$TMP/pycache" python3 -m py_compile "$HERE/matrix-fixture.py"

python3 - "$PROXY_DIR/conf/traefik-dyn.yml" <<'PY'
from pathlib import Path
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
required_once = (
    "    jellyfin-explicit-accept-encoding:\n",
    "      rule: \"PathPrefix(`/`) && HeaderRegexp(`Accept-Encoding`, `.+`)\"\n",
    "      priority: 100\n",
    "    jellyfin-identity-default:\n",
    "      priority: 10\n",
    "      middlewares: [jellyfin-force-identity]\n",
    "    jellyfin-force-identity:\n",
    "          Accept-Encoding: identity\n",
)
for fragment in required_once:
    assert text.count(fragment) == 1, fragment
explicit, default = text.split("    jellyfin-identity-default:\n", 1)
assert "middlewares:" not in explicit
assert "service: jellyfin" in explicit
assert "service: jellyfin" in default
PY

if [ "$(awk '$1 == "compression" && $2 == "off" { count++ } END { print count + 0 }' \
    "$PROXY_DIR/conf/Caddyfile")" -ne 1 ]; then
    die "Caddy must disable exactly one HTTP transport compression default"
fi

# The parking helper must reject stale state and destination collisions without
# losing either copy, then recover cleanly once the collision is resolved.
CONFIG_ROOT="$TMP/config"
mkdir -p "$CONFIG_ROOT/plugins/configurations" \
    "$CONFIG_ROOT/plugins/Jellyfin Refresh Kit_1.0" \
    "$CONFIG_ROOT/plugins/Jellyfin Enhanced_12.2"
printf 'fixture\n' > "$CONFIG_ROOT/plugins/Jellyfin Enhanced_12.2/marker"
sh "$HERE/injector-state.sh" park "$CONFIG_ROOT"
[ -d "$CONFIG_ROOT/plugins/configurations" ] || die "configurations directory was parked"
[ -d "$CONFIG_ROOT/plugins/Jellyfin Refresh Kit_1.0" ] || die "Refresh Kit was parked"
[ -f "$CONFIG_ROOT/plugins-parked/Jellyfin Enhanced_12.2/marker" ] \
    || die "independent injector was not parked"
if sh "$HERE/injector-state.sh" park "$CONFIG_ROOT" >"$TMP/stale.out" 2>&1; then
    die "second parking operation accepted stale state"
fi
mkdir "$CONFIG_ROOT/plugins/Jellyfin Enhanced_12.2"
if sh "$HERE/injector-state.sh" restore "$CONFIG_ROOT" >"$TMP/collision.out" 2>&1; then
    die "restore overwrote an active same-name plugin"
fi
[ -f "$CONFIG_ROOT/plugins-parked/Jellyfin Enhanced_12.2/marker" ] \
    || die "collision failure lost the parked plugin"
rmdir "$CONFIG_ROOT/plugins/Jellyfin Enhanced_12.2"
sh "$HERE/injector-state.sh" restore "$CONFIG_ROOT"
[ -f "$CONFIG_ROOT/plugins/Jellyfin Enhanced_12.2/marker" ] \
    || die "injector did not restore after collision resolution"
[ ! -e "$CONFIG_ROOT/plugins-parked" ] || die "empty parking state was not removed"
sh "$HERE/injector-state.sh" restore "$CONFIG_ROOT"

# Exercise both HTTP contracts against loopback fixtures. The strict fixture's
# identity 304 omits representation metadata, its coded 304s include legal
# optional metadata/length, and its bodyless 412 includes Content-Length: 0.
start_fixture strict
bash "$HERE/matrix.sh" static-strict "$FIXTURE_PORT" '' strict \
    > "$TMP/strict.log"
stop_fixture

start_fixture suppressed --suppress-conditionals
bash "$HERE/matrix.sh" static-suppressed "$FIXTURE_PORT" '' \
    nginx-cache-suppresses-conditionals > "$TMP/suppressed.log"
stop_fixture

start_fixture weak --etag-mode weak
if bash "$HERE/matrix.sh" static-weak "$FIXTURE_PORT" '' strict > "$TMP/weak.log"; then
    die "strict matrix accepted weak rk- validators"
fi
stop_fixture
grep -q 'rk- ETag is weak' "$TMP/weak.log" \
    || die "weak-validator negative fixture failed for an unrelated reason"

start_fixture gzip-degraded --disable-gzip
if bash "$HERE/matrix.sh" static-gzip-degraded "$FIXTURE_PORT" '' strict \
    > "$TMP/gzip-degraded.log"; then
    die "strict matrix accepted loss of explicit gzip negotiation"
fi
stop_fixture
grep -q 'gzip request degraded to identity' "$TMP/gzip-degraded.log" \
    || die "gzip-negotiation negative fixture failed for an unrelated reason"

start_fixture gzip-shared --etag-mode gzip-equals-identity
if bash "$HERE/matrix.sh" static-gzip-shared "$FIXTURE_PORT" '' strict \
    > "$TMP/gzip-shared.log"; then
    die "strict matrix accepted shared identity/gzip ETags for distinct bytes"
fi
stop_fixture
grep -q 'identity and gzip distinct bytes share an ETag' "$TMP/gzip-shared.log" \
    || die "identity/gzip negative fixture failed for an unrelated reason"

if python3 - <<'PY'
try:
    import brotli  # noqa: F401
except ImportError:
    raise SystemExit(1)
PY
then
    start_fixture br-identity-shared --etag-mode br-equals-identity
    if bash "$HERE/matrix.sh" static-br-identity-shared "$FIXTURE_PORT" '' strict \
        > "$TMP/br-identity-shared.log"; then
        die "strict matrix accepted shared identity/Brotli ETags for distinct bytes"
    fi
    stop_fixture
    grep -q 'identity and br distinct bytes share an ETag' "$TMP/br-identity-shared.log" \
        || die "identity/Brotli negative fixture failed for an unrelated reason"

    start_fixture br-shared --etag-mode br-equals-gzip
    if bash "$HERE/matrix.sh" static-br-shared "$FIXTURE_PORT" '' strict \
        > "$TMP/br-shared.log"; then
        die "strict matrix accepted shared gzip/Brotli ETags for distinct bytes"
    fi
    stop_fixture
    grep -q 'gzip and br distinct bytes share an ETag' "$TMP/br-shared.log" \
        || die "gzip/Brotli negative fixture failed for an unrelated reason"
fi

echo "PASS: proxy static and no-Docker regressions"
