#!/usr/bin/env bash
# Per-proxy freshness matrix for the Jellyfin Refresh Kit.
# usage: matrix.sh <label> <port> [path-prefix] <contract>
# contract: strict | nginx-cache-suppresses-conditionals
set -uo pipefail

LABEL="${1:?proxy label is required}"
PORT="${2:?proxy port is required}"
PFX="${3:-}"
CONTRACT="${4:?matrix contract is required}"

case "$PFX" in
    ''|/*) ;;
    *) echo "FATAL: matrix path prefix must be empty or start with '/': $PFX" >&2; exit 2 ;;
esac
case "$CONTRACT" in
    strict|nginx-cache-suppresses-conditionals) ;;
    *) echo "FATAL: unknown proxy matrix contract: $CONTRACT" >&2; exit 2 ;;
esac

B="http://127.0.0.1:${PORT}${PFX}"
TMP="$(mktemp -d)"
trap 'rm -rf -- "$TMP"' EXIT
pass=0
fail=0

r() { # name expected actual
    if [ "$2" = "$3" ]; then
        echo "  PASS $1 ($3)"
        pass=$((pass + 1))
    else
        echo "  FAIL $1 (expected $2, got ${3:-<empty>})"
        fail=$((fail + 1))
    fi
}

ok() {
    echo "  PASS $1"
    pass=$((pass + 1))
}

bad() {
    echo "  FAIL $1"
    fail=$((fail + 1))
}

fetch() { # description headers body [curl arguments...]
    local description="$1" headers="$2" body="$3"
    shift 3
    : > "$headers"
    : > "$body"
    if curl --silent --show-error --max-time 20 --dump-header "$headers" \
        --output "$body" "$@"; then
        return 0
    fi
    bad "$description request completed"
    return 1
}

status_code() {
    awk '$1 ~ /^HTTP\// { value=$2 } END { printf "%s", value }' "$1"
}

header_value() { # headers name
    awk -v wanted="$2" '
        {
            line=$0
            sub(/\r$/, "", line)
            colon=index(line, ":")
            if (colon > 0 && tolower(substr(line, 1, colon - 1)) == tolower(wanted)) {
                value=substr(line, colon + 1)
                sub(/^[ \t]+/, "", value)
            }
        }
        END { printf "%s", value }
    ' "$1"
}

header_count() { # headers name
    awk -v wanted="$2" '
        {
            line=$0
            sub(/\r$/, "", line)
            colon=index(line, ":")
            if (colon > 0 && tolower(substr(line, 1, colon - 1)) == tolower(wanted)) {
                count++
            }
        }
        END { print count + 0 }
    ' "$1"
}

byte_count() {
    wc -c < "$1" | tr -d '[:space:]'
}

assert_rk_etag() {
    case "$2" in
        '"rk-'*'"'|W/'"rk-'*'"') ok "$1 rk- ETag present ($2)" ;;
        *) bad "$1 rk- ETag missing (got '${2:-<empty>}')" ;;
    esac
}

assert_content_length() { # label headers body
    local count value bytes
    count="$(header_count "$2" Content-Length)"
    value="$(header_value "$2" Content-Length)"
    bytes="$(byte_count "$3")"
    r "$1 single Content-Length header" 1 "$count"
    r "$1 Content-Length matches complete body" "$bytes" "$value"
}

assert_same_header() { # label current-headers reference-headers header-name
    local label="$1" current="$2" reference="$3" name="$4"
    r "$label $name header count" \
        "$(header_count "$reference" "$name")" "$(header_count "$current" "$name")"
    r "$label $name" \
        "$(header_value "$reference" "$name")" "$(header_value "$current" "$name")"
}

assert_full_representation() { # label headers body reference-headers reference-body etag encoding
    local label="$1" headers="$2" body="$3" reference_headers="$4" reference_body="$5"
    local etag="$6" encoding="$7" actual_etag actual_encoding encoding_count
    actual_etag="$(header_value "$headers" ETag)"
    actual_encoding="$(header_value "$headers" Content-Encoding)"
    encoding_count="$(header_count "$headers" Content-Encoding)"

    r "$label ETag" "$etag" "$actual_etag"
    if [ -n "$encoding" ]; then
        r "$label single Content-Encoding header" 1 "$encoding_count"
        r "$label Content-Encoding" "$encoding" "$actual_encoding"
    else
        r "$label has no Content-Encoding" 0 "$encoding_count"
    fi
    assert_same_header "$label" "$headers" "$reference_headers" Content-Type
    assert_same_header "$label" "$headers" "$reference_headers" Cache-Control
    assert_same_header "$label" "$headers" "$reference_headers" Vary
    assert_content_length "$label" "$headers" "$body"
    if cmp -s -- "$reference_body" "$body"; then
        ok "$label carries the exact complete representation"
    else
        bad "$label representation differs from the warm body ($(byte_count "$reference_body") vs $(byte_count "$body") bytes)"
    fi
}

assert_not_modified() { # label headers body reference-headers etag
    local label="$1" headers="$2" body="$3" reference_headers="$4" etag="$5"
    r "$label ETag" "$etag" "$(header_value "$headers" ETag)"
    assert_same_header "$label" "$headers" "$reference_headers" Content-Type
    assert_same_header "$label" "$headers" "$reference_headers" Cache-Control
    assert_same_header "$label" "$headers" "$reference_headers" Vary
    r "$label has no Content-Encoding" 0 "$(header_count "$headers" Content-Encoding)"
    r "$label has no Content-Length" 0 "$(header_count "$headers" Content-Length)"
    r "$label has an empty body" 0 "$(byte_count "$body")"
}

assert_precondition_failed() { # label headers body
    local label="$1" headers="$2" body="$3" cache_control
    r "$label has no ETag" 0 "$(header_count "$headers" ETag)"
    r "$label has no Content-Encoding" 0 "$(header_count "$headers" Content-Encoding)"
    r "$label has no Content-Length" 0 "$(header_count "$headers" Content-Length)"
    r "$label has no Content-Type" 0 "$(header_count "$headers" Content-Type)"
    r "$label has an empty body" 0 "$(byte_count "$body")"
    cache_control="$(header_value "$headers" Cache-Control | tr '[:upper:]' '[:lower:]')"
    case ",$cache_control," in
        *,no-store,*|*,no-store|*'no-store'*) ok "$label is explicitly no-store" ;;
        *) bad "$label Cache-Control does not contain no-store (got '${cache_control:-<empty>}')" ;;
    esac
}

decode_html() { # label encoding input output
    local label="$1" encoding="$2" input="$3" output="$4"
    case "$encoding" in
        '') cp -- "$input" "$output" ;;
        gzip) gzip -dc -- "$input" > "$output" 2>/dev/null ;;
        br)
            if command -v brotli >/dev/null 2>&1; then
                brotli --decompress --stdout "$input" > "$output" 2>/dev/null
            else
                python3 - "$input" "$output" <<'PY'
import sys
import brotli

source, destination = sys.argv[1:]
with open(source, "rb") as handle:
    decoded = brotli.decompress(handle.read())
with open(destination, "wb") as handle:
    handle.write(decoded)
PY
            fi
            ;;
        *) return 1 ;;
    esac || return 1

    if grep -qi '<html' "$output"; then
        ok "$label body decodes to HTML"
        return 0
    fi
    return 1
}

echo "===== $LABEL (port $PORT prefix '$PFX'; contract $CONTRACT) ====="

# Establish the complete identity representation used as the byte-for-byte
# reference for every conditional request that is expected to return 200.
fetch "identity warm GET" "$TMP/id.h" "$TMP/id.html" "$B/web/" || true
ID_CODE="$(status_code "$TMP/id.h")"
ID_ETAG="$(header_value "$TMP/id.h" ETag)"
r "GET /web/" 200 "$ID_CODE"
assert_rk_etag "identity" "$ID_ETAG"
r "identity has no Content-Encoding" 0 "$(header_count "$TMP/id.h" Content-Encoding)"
r "identity has one Content-Type" 1 "$(header_count "$TMP/id.h" Content-Type)"
case "$(header_value "$TMP/id.h" Content-Type)" in
    text/html*) ok "identity Content-Type is HTML" ;;
    *) bad "identity Content-Type is not HTML ($(header_value "$TMP/id.h" Content-Type))" ;;
esac
assert_content_length "identity" "$TMP/id.h" "$TMP/id.html"
if grep -qi '<html' "$TMP/id.html"; then
    ok "identity body is HTML"
else
    bad "identity body is not HTML"
fi

# Identity conditional requests. Active nginx proxy_cache intentionally strips
# these client conditionals before contacting the origin, so its two freshness
# controls must return the full cached 200 exactly; that is safe degradation,
# not evidence of strong-validator support.
fetch "matching identity If-None-Match" "$TMP/inm-match.h" "$TMP/inm-match.body" \
    -H "If-None-Match: $ID_ETAG" "$B/web/" || true
if [ "$CONTRACT" = strict ]; then
    r "matching identity If-None-Match" 304 "$(status_code "$TMP/inm-match.h")"
    assert_not_modified "matching identity If-None-Match 304" \
        "$TMP/inm-match.h" "$TMP/inm-match.body" "$TMP/id.h" "$ID_ETAG"
else
    r "nginx cache suppresses matching identity If-None-Match" 200 "$(status_code "$TMP/inm-match.h")"
    assert_full_representation "suppressed identity If-None-Match 200" \
        "$TMP/inm-match.h" "$TMP/inm-match.body" "$TMP/id.h" "$TMP/id.html" "$ID_ETAG" ''
fi

fetch "stale identity If-None-Match" "$TMP/inm-stale.h" "$TMP/inm-stale.body" \
    -H 'If-None-Match: "rk-bogus"' "$B/web/" || true
r "stale identity If-None-Match" 200 "$(status_code "$TMP/inm-stale.h")"
assert_full_representation "stale identity If-None-Match 200" \
    "$TMP/inm-stale.h" "$TMP/inm-stale.body" "$TMP/id.h" "$TMP/id.html" "$ID_ETAG" ''

fetch "bad identity If-Match" "$TMP/im-bad.h" "$TMP/im-bad.body" \
    -H 'If-Match: "rk-bogus"' "$B/web/" || true
if [ "$CONTRACT" = strict ]; then
    r "bad identity If-Match" 412 "$(status_code "$TMP/im-bad.h")"
    assert_precondition_failed "bad identity If-Match 412" "$TMP/im-bad.h" "$TMP/im-bad.body"
else
    r "nginx cache suppresses bad identity If-Match" 200 "$(status_code "$TMP/im-bad.h")"
    assert_full_representation "suppressed bad identity If-Match 200" \
        "$TMP/im-bad.h" "$TMP/im-bad.body" "$TMP/id.h" "$TMP/id.html" "$ID_ETAG" ''
fi

fetch "good identity If-Match" "$TMP/im-good.h" "$TMP/im-good.body" \
    -H "If-Match: $ID_ETAG" "$B/web/" || true
r "good identity If-Match" 200 "$(status_code "$TMP/im-good.h")"
assert_full_representation "good identity If-Match 200" \
    "$TMP/im-good.h" "$TMP/im-good.body" "$TMP/id.h" "$TMP/id.html" "$ID_ETAG" ''

test_coded_representation() { # request encoding file stem
    local requested="$1" stem="$2"
    local headers="$TMP/$stem.h" body="$TMP/$stem.bin" decoded="$TMP/$stem.html"
    local conditional_headers="$TMP/$stem-inm.h" conditional_body="$TMP/$stem-inm.body"
    local code etag encoding encoding_count

    fetch "$requested warm GET" "$headers" "$body" \
        -H "Accept-Encoding: $requested" "$B/web/" || true
    code="$(status_code "$headers")"
    etag="$(header_value "$headers" ETag)"
    encoding="$(header_value "$headers" Content-Encoding)"
    encoding_count="$(header_count "$headers" Content-Encoding)"
    r "$requested GET" 200 "$code"
    assert_rk_etag "$requested" "$etag"
    assert_content_length "$requested" "$headers" "$body"

    if [ "$encoding" = "$requested" ]; then
        r "$requested single Content-Encoding header" 1 "$encoding_count"
        if ! decode_html "$requested" "$encoding" "$body" "$decoded"; then
            bad "$requested body did not decode to HTML"
        fi
    elif [ -z "$encoding" ]; then
        r "$requested identity fallback has no Content-Encoding" 0 "$encoding_count"
        r "$requested identity fallback ETag" "$ID_ETAG" "$etag"
        if cmp -s -- "$TMP/id.html" "$body"; then
            ok "$requested identity fallback carries the exact complete identity body"
        else
            bad "$requested identity fallback differs from the identity body"
        fi
    else
        bad "$requested response has unexpected Content-Encoding '$encoding'"
    fi

    fetch "matching $requested If-None-Match" "$conditional_headers" "$conditional_body" \
        -H "Accept-Encoding: $requested" -H "If-None-Match: $etag" "$B/web/" || true
    if [ "$CONTRACT" = strict ]; then
        r "matching $requested If-None-Match" 304 "$(status_code "$conditional_headers")"
        assert_not_modified "matching $requested If-None-Match 304" \
            "$conditional_headers" "$conditional_body" "$headers" "$etag"
    else
        r "nginx cache suppresses matching $requested If-None-Match" 200 \
            "$(status_code "$conditional_headers")"
        assert_full_representation "suppressed $requested If-None-Match 200" \
            "$conditional_headers" "$conditional_body" "$headers" "$body" "$etag" "$encoding"
    fi
}

test_coded_representation gzip g
test_coded_representation br b

# Distinct bytes must not share a strong ETag. An identity fallback is checked
# against the identity ETag in test_coded_representation above.
G_ENCODING="$(header_value "$TMP/g.h" Content-Encoding)"
G_ETAG="$(header_value "$TMP/g.h" ETag)"
if [ "$G_ENCODING" = gzip ]; then
    if [ -n "$G_ETAG" ] && [ "$G_ETAG" != "$ID_ETAG" ]; then
        ok "gzip carries its own representation ETag ($G_ETAG)"
    else
        bad "gzip representation shares the identity ETag ($G_ETAG)"
    fi
fi

# Generation endpoints are public and must agree with the generation stamped
# into the exact identity shell captured above.
fetch "Generation endpoint" "$TMP/gen.h" "$TMP/gen.json" "$B/RefreshKit/Generation" || true
r "GET /RefreshKit/Generation (anonymous)" 200 "$(status_code "$TMP/gen.h")"
CK="$(grep -oE '"CacheKey":"[^"]+"' "$TMP/gen.json" | head -1 | cut -d'"' -f4)"
if [ -n "$CK" ]; then
    ok "Generation JSON contains CacheKey ($CK)"
else
    bad "Generation JSON has no CacheKey"
fi

fetch "Generation.txt endpoint" "$TMP/gen-txt.h" "$TMP/gen.txt" "$B/RefreshKit/Generation.txt" || true
r "GET /RefreshKit/Generation.txt (anonymous)" 200 "$(status_code "$TMP/gen-txt.h")"
TXT="$(tr -d '\r\n' < "$TMP/gen.txt")"
r "Generation.txt equals CacheKey" "$CK" "$TXT"

fetch "kit.js endpoint" "$TMP/kit.h" "$TMP/kit.js" "$B/RefreshKit/kit.js?v=$CK" || true
r "GET /RefreshKit/kit.js" 200 "$(status_code "$TMP/kit.h")"
if [ -s "$TMP/kit.js" ]; then
    ok "kit.js body is non-empty"
else
    bad "kit.js body is empty"
fi

if [ -n "$CK" ] && grep -q "kit.js?v=$CK" "$TMP/id.html"; then
    ok "shell kit tag is stamped with live generation ($CK)"
else
    bad "shell kit tag generation mismatch ($(grep -oE 'kit\.js\?v=[^"]+' "$TMP/id.html" | head -1) vs ${CK:-<empty>})"
fi

echo "  ---- $LABEL [$CONTRACT]: $pass passed, $fail failed"
exit "$fail"
