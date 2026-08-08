#!/usr/bin/env bash
#
# The misconfigured-cache case, start to finish:
#   1. prime the naive cache (proxy_ignore_headers Cache-Control)
#   2. move the generation at the origin
#   3. show the naive cache still serving the OLD shell and the OLD generation
#   4. show the two remedied configs tracking the origin immediately
#
# Nothing here asserts; it prints the evidence. Run it after provision.sh.
set -euo pipefail

CONTAINER="${RK_CONTAINER:-rk-jf}"
BUMP="${RK_BUMP_FILE:-/config/plugins/Jellyfin Enhanced_12.1.0.0/Jellyfin.Plugin.JellyfinEnhanced.dll}"

NAIVE=8122; RESPECT=8124; FIX1=8126; FIX2=8127; ORIGIN=8116

shell_stamp() { curl -s "http://127.0.0.1:$1/web/" | grep -oE 'kit\.js\?v=[^"]+' | head -1; }
gen()         { curl -s "http://127.0.0.1:$1/RefreshKit/Generation.txt"; }
cache_status() { curl -s -o /dev/null -D - "http://127.0.0.1:$1/web/" | grep -i '^x-cache-status' | tr -d '\r'; }

echo "### 1. prime every cache"
for p in $NAIVE $RESPECT $FIX1 $FIX2; do
    curl -s -o /dev/null "http://127.0.0.1:$p/web/"
    curl -s -o /dev/null "http://127.0.0.1:$p/RefreshKit/Generation.txt"
done
printf '  origin generation      : %s\n' "$(gen $ORIGIN)"
printf '  naive  :%s shell stamp : %s   %s\n' "$NAIVE" "$(shell_stamp $NAIVE)" "$(cache_status $NAIVE)"

echo
echo "### 2. move the generation at the origin (touch a plugin binary)"
docker exec "$CONTAINER" touch "$BUMP"
sleep 8
printf '  origin generation      : %s\n' "$(gen $ORIGIN)"

echo
echo "### 3. what each proxy now serves"
printf '  %-42s %-24s %s\n' 'PROXY' 'GENERATION ENDPOINT' 'SHELL STAMP'
printf '  %-42s %-24s %s\n' "naive  :$NAIVE  (ignores Cache-Control)" "$(gen $NAIVE)"   "$(shell_stamp $NAIVE)"
printf '  %-42s %-24s %s\n' "respect:$RESPECT  (honours Cache-Control)" "$(gen $RESPECT)" "$(shell_stamp $RESPECT)"
printf '  %-42s %-24s %s\n' "remedy1:$FIX1  (no ignore_headers)"       "$(gen $FIX1)"    "$(shell_stamp $FIX1)"
printf '  %-42s %-24s %s\n' "remedy2:$FIX2  (+ cache exemption)"       "$(gen $FIX2)"    "$(shell_stamp $FIX2)"

echo
echo "### 4. client revalidation (a 304 is what keeps a warm tab cheap)"
for p in $ORIGIN $NAIVE $RESPECT $FIX1 $FIX2; do
    ET=$(curl -s -o /dev/null -D - "http://127.0.0.1:$p/web/" | grep -i '^etag:' | sed 's/^[^:]*:[[:space:]]*//' | tr -d '\r')
    C=$(curl -s -o /dev/null -w '%{http_code}' -H "If-None-Match: ${ET:-\"none\"}" "http://127.0.0.1:$p/web/")
    printf '  :%s  ETag=%-12s If-None-Match -> %s\n' "$p" "${ET:0:10}" "$C"
done
