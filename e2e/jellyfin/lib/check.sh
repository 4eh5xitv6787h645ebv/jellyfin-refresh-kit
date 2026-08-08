#!/usr/bin/env bash

# Assert the installed plugin's anonymous endpoints, authenticated diagnostics,
# and transformed/revalidating web shell. Usage: check.sh jf10|jf12

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
# Resolved from this script's directory at runtime.
# shellcheck disable=SC1091
source "${HERE}/common.sh"

TARGET="${1:-}"
case "${TARGET}" in jf10|jf12) ;; *) rk_die "usage: check.sh jf10|jf12" ;; esac
ORIGIN="$(rk_origin "${TARGET}")"
TOKEN_FILE="$(rk_token_file "${TARGET}")"
[ -s "${TOKEN_FILE}" ] || rk_die "${TARGET}: missing token file; provision the lab first"
TOKEN="$(<"${TOKEN_FILE}")"
AUTH="$(rk_auth_header "${TOKEN}")"
OUT="${RK_ARTIFACT_DIR}/${TARGET}/server"
mkdir -p "${OUT}"

rk_require curl python3
rk_assert_container_image "${TARGET}"
rk_wait_http "${ORIGIN}/RefreshKit/Generation" 200 30 1

curl --fail --silent --show-error \
    -D "${OUT}/generation.headers" -o "${OUT}/generation.json" \
    "${ORIGIN}/RefreshKit/Generation"
readarray -t GENERATION_INFO < <(python3 - "${OUT}/generation.json" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)

def value(name):
    return data.get(name, data.get(name[:1].lower() + name[1:]))

for key in ("Version", "BuildId", "CacheKey"):
    found = value(key)
    if not isinstance(found, str) or not found:
        raise SystemExit(f"FATAL: generation endpoint has no non-empty {key}: {data!r}")
    print(found)
PY
)
GENERATION="${GENERATION_INFO[2]}"
grep -Eiq '^cache-control:.*no-store' "${OUT}/generation.headers" || \
    rk_die "${TARGET}: generation response is not no-store"

curl --fail --silent --show-error \
    -D "${OUT}/kit.headers" -o "${OUT}/kit.js" \
    "${ORIGIN}/RefreshKit/kit.js?v=${GENERATION}"
grep -Eq "KIT_VERSION[[:space:]]*=[[:space:]]*'[^']+'" "${OUT}/kit.js" || \
    rk_die "${TARGET}: served kit.js does not contain the runtime"
grep -Eiq '^cache-control:.*immutable' "${OUT}/kit.headers" || \
    rk_die "${TARGET}: version-addressed kit.js is not immutable"

curl --fail --silent --show-error \
    -D "${OUT}/shell.headers" -o "${OUT}/index.html" \
    "${ORIGIN}/web/index.html"
grep -Fq 'plugin="Jellyfin Refresh Kit"' "${OUT}/index.html" || \
    rk_die "${TARGET}: transformed shell has no Refresh Kit tag"
grep -Fq 'data-name="RefreshKitPlugin"' "${OUT}/index.html" || \
    rk_die "${TARGET}: transformed shell has no named runtime instance"
grep -Fq "data-boot-version=\"${GENERATION}\"" "${OUT}/index.html" || \
    rk_die "${TARGET}: shell boot seed does not match generation ${GENERATION}"
grep -Fq "/RefreshKit/kit.js?v=${GENERATION}" "${OUT}/index.html" || \
    rk_die "${TARGET}: shell kit URL does not carry generation ${GENERATION}"

ETAG="$(awk 'BEGIN { IGNORECASE=1 } /^etag:/ { sub(/^[^:]*:[[:space:]]*/, ""); sub(/\r$/, ""); value=$0 } END { print value }' "${OUT}/shell.headers")"
[ -n "${ETAG}" ] || rk_die "${TARGET}: transformed shell has no ETag"
case "${ETAG}" in *rk-*) ;; *) rk_die "${TARGET}: shell ETag is not a Refresh Kit ETag: ${ETAG}" ;; esac

CONDITIONAL_STATUS="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    -H "If-None-Match: ${ETAG}" "${ORIGIN}/web/index.html")"
[ "${CONDITIONAL_STATUS}" = "304" ] || \
    rk_die "${TARGET}: If-None-Match returned ${CONDITIONAL_STATUS}, expected 304"

curl --fail --silent --show-error \
    -H "${AUTH}" -o "${OUT}/diagnostics.json" \
    "${ORIGIN}/RefreshKit/Diagnostics"
curl --fail --silent --show-error \
    -H "${AUTH}" -o "${OUT}/plugins.json" \
    "${ORIGIN}/Plugins"

python3 - "${OUT}/diagnostics.json" "${OUT}/plugins.json" "${RK_PLUGIN_GUID}" <<'PY'
import json
import sys

diagnostics_path, plugins_path, guid = sys.argv[1:]
with open(diagnostics_path, encoding="utf-8") as handle:
    diagnostics = json.load(handle)
with open(plugins_path, encoding="utf-8") as handle:
    plugins = json.load(handle)

text = json.dumps(plugins, sort_keys=True).lower()
if guid.lower().replace("-", "") not in text.replace("-", "") or "jellyfin refresh kit" not in text:
    raise SystemExit("FATAL: authenticated plugin inventory does not contain Refresh Kit")
if not isinstance(diagnostics, dict):
    raise SystemExit("FATAL: diagnostics response is not an object")
generation = diagnostics.get("Generation", diagnostics.get("generation"))
kit_version = diagnostics.get("KitVersion", diagnostics.get("kitVersion"))
rows = diagnostics.get("Plugins", diagnostics.get("plugins"))
if not generation or not kit_version or not isinstance(rows, list):
    raise SystemExit(f"FATAL: incomplete diagnostics response: {diagnostics!r}")
PY

PUBLIC_VERSION="$(curl --fail --silent --show-error "${ORIGIN}/System/Info/Public" | \
    python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("Version", d.get("version", "")))')"
KIT_VERSION="$(python3 - "${OUT}/diagnostics.json" <<'PY'
import json, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))
print(d.get("KitVersion", d.get("kitVersion", "")))
PY
)"

python3 - "${OUT}/result.json" "${TARGET}" "${PUBLIC_VERSION}" "${GENERATION}" \
    "${KIT_VERSION}" "${ETAG}" "${CONDITIONAL_STATUS}" <<'PY'
import json
import sys
path, target, server, generation, kit, etag, conditional = sys.argv[1:]
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump({
        "target": target,
        "serverVersion": server,
        "generation": generation,
        "kitVersion": kit,
        "shellEtag": etag,
        "conditionalStatus": int(conditional),
        "endpointAndShellChecksPassed": True,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

rk_log "${TARGET}: endpoints, diagnostics and shell PASS (server ${PUBLIC_VERSION}, kit ${KIT_VERSION}, generation ${GENERATION})"
