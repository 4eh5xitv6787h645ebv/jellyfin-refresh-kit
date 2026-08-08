#!/usr/bin/env bash
# Per-proxy freshness matrix for the Jellyfin Refresh Kit.
# usage: matrix.sh <label> <port> [path-prefix]
LABEL="$1"; PORT="$2"; PFX="${3:-}"
B="http://127.0.0.1:${PORT}${PFX}"
TMP=$(mktemp -d)
pass=0; fail=0
r() { # name expected actual
  if [ "$2" = "$3" ]; then echo "  PASS $1 ($3)"; pass=$((pass+1));
  else echo "  FAIL $1 (expected $2, got $3)"; fail=$((fail+1)); fi
}
echo "===== $LABEL (port $PORT prefix '${PFX}') ====="

# warm + capture identity ETag
curl -s -o "$TMP/id.html" -D "$TMP/id.h" "$B/web/" >/dev/null
CODE=$(head -1 "$TMP/id.h" | awk '{print $2}')
ET=$(grep -i '^etag:' "$TMP/id.h" | tail -1 | sed 's/^[Ee][Tt]ag:[[:space:]]*//' | tr -d '\r')
r "GET /web/ 200" 200 "$CODE"
case "$ET" in
  '"rk-'*) echo "  PASS rk- ETag present ($ET)"; pass=$((pass+1));;
  W/*rk-*) echo "  PASS rk- weak ETag present ($ET)"; pass=$((pass+1));;
  *) echo "  FAIL rk- ETag missing (got '$ET')"; fail=$((fail+1));;
esac

# 304 revalidation
C=$(curl -s -o /dev/null -w '%{http_code}' -H "If-None-Match: $ET" "$B/web/")
r "If-None-Match -> 304" 304 "$C"
C=$(curl -s -o /dev/null -w '%{http_code}' -H 'If-None-Match: "rk-bogus"' "$B/web/")
r "stale If-None-Match -> 200" 200 "$C"

# If-Match conditionals
C=$(curl -s -o /dev/null -w '%{http_code}' -H 'If-Match: "rk-bogus"' "$B/web/")
r "bad If-Match -> 412" 412 "$C"
C=$(curl -s -o /dev/null -w '%{http_code}' -H "If-Match: $ET" "$B/web/")
r "good If-Match -> 200" 200 "$C"

# gzip representation
curl -s -H 'Accept-Encoding: gzip' -o "$TMP/g.bin" -D "$TMP/g.h" "$B/web/" >/dev/null
GC=$(head -1 "$TMP/g.h" | awk '{print $2}')
GE=$(grep -ci '^content-encoding:' "$TMP/g.h")
GENC=$(grep -i '^content-encoding:' "$TMP/g.h" | tail -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
GET_ETAG=$(grep -i '^etag:' "$TMP/g.h" | tail -1 | sed 's/^[Ee][Tt]ag:[[:space:]]*//' | tr -d '\r')
r "gzip GET 200" 200 "$GC"
r "gzip single Content-Encoding header" 1 "$GE"
if [ "$GENC" = "gzip" ]; then
  if gzip -dc "$TMP/g.bin" 2>/dev/null | grep -qi '<html'; then
    echo "  PASS gzip body decodes to HTML (no double-compression)"; pass=$((pass+1))
  else
    echo "  FAIL gzip body did not decode to HTML"; fail=$((fail+1))
  fi
  C=$(curl -s -o /dev/null -w '%{http_code}' -H 'Accept-Encoding: gzip' -H "If-None-Match: $GET_ETAG" "$B/web/")
  r "gzip If-None-Match -> 304" 304 "$C"
else
  # proxy stripped/normalised the coding: body must still be plain HTML
  if grep -qi '<html' "$TMP/g.bin"; then
    echo "  PASS gzip not offered by proxy; identity body intact (CE='$GENC')"; pass=$((pass+1))
  else
    echo "  FAIL gzip request returned unusable body (CE='$GENC')"; fail=$((fail+1))
  fi
fi

# brotli representation
curl -s -H 'Accept-Encoding: br' -o "$TMP/b.bin" -D "$TMP/b.h" "$B/web/" >/dev/null
BENC=$(grep -i '^content-encoding:' "$TMP/b.h" | tail -1 | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
BN=$(grep -ci '^content-encoding:' "$TMP/b.h")
BET=$(grep -i '^etag:' "$TMP/b.h" | tail -1 | sed 's/^[Ee][Tt]ag:[[:space:]]*//' | tr -d '\r')
r "br single/absent Content-Encoding header" 1 "$([ "$BN" -le 1 ] && echo 1 || echo "$BN")"
if [ "$BENC" = "br" ]; then
  if command -v brotli >/dev/null && brotli -dc "$TMP/b.bin" 2>/dev/null | grep -qi '<html'; then
    echo "  PASS br body decodes to HTML"; pass=$((pass+1))
  elif python3 -c "import brotli,sys;d=brotli.decompress(open(sys.argv[1],'rb').read());sys.exit(0 if b'<html' in d.lower() else 1)" "$TMP/b.bin" 2>/dev/null; then
    echo "  PASS br body decodes to HTML"; pass=$((pass+1))
  else
    echo "  INFO br body present, no local decoder ($(wc -c <"$TMP/b.bin") bytes)"
  fi
  C=$(curl -s -o /dev/null -w '%{http_code}' -H 'Accept-Encoding: br' -H "If-None-Match: $BET" "$B/web/")
  r "br If-None-Match -> 304" 304 "$C"
else
  if grep -qi '<html' "$TMP/b.bin"; then
    echo "  PASS br not offered by proxy; identity body intact (CE='$BENC')"; pass=$((pass+1))
  else
    echo "  FAIL br request returned unusable body (CE='$BENC')"; fail=$((fail+1))
  fi
fi

# distinct representation ETags (identity vs gzip) when both are coded
if [ "$GENC" = "gzip" ] && [ -n "$GET_ETAG" ] && [ "$GET_ETAG" != "$ET" ]; then
  echo "  PASS gzip carries its own representation ETag ($GET_ETAG)"; pass=$((pass+1))
elif [ "$GENC" = "gzip" ]; then
  echo "  INFO gzip ETag == identity ETag ($GET_ETAG)"
fi

# generation endpoints, unauthenticated
GEN=$(curl -s "$B/RefreshKit/Generation")
GENC2=$(curl -s -o /dev/null -w '%{http_code}' "$B/RefreshKit/Generation")
r "GET /RefreshKit/Generation (anon) 200" 200 "$GENC2"
CK=$(echo "$GEN" | grep -oE '"CacheKey":"[^"]+"' | cut -d'"' -f4)
TXT=$(curl -s "$B/RefreshKit/Generation.txt" | tr -d '\r\n')
r "Generation.txt == CacheKey" "$CK" "$TXT"
KJ=$(curl -s -o /dev/null -w '%{http_code}' "$B/RefreshKit/kit.js?v=$CK")
r "GET /RefreshKit/kit.js 200" 200 "$KJ"

# stamped kit tag in the shell carries the live generation
if grep -q "kit.js?v=$CK" "$TMP/id.html"; then
  echo "  PASS shell kit tag stamped with live generation ($CK)"; pass=$((pass+1))
else
  echo "  FAIL shell kit tag generation mismatch: $(grep -oE 'kit\.js\?v=[^\"]+' "$TMP/id.html" | head -1) vs $CK"; fail=$((fail+1))
fi

echo "  ---- $LABEL: $pass passed, $fail failed"
rm -rf "$TMP"
exit $fail
