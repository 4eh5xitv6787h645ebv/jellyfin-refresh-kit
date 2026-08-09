#!/usr/bin/env bash

# Assert the installed plugin's anonymous endpoints, authenticated diagnostics,
# and transformed/revalidating web shell. Usage: check.sh jf10|jf12|abi-floor

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
# Resolved from this script's directory at runtime.
# shellcheck disable=SC1091
source "${HERE}/common.sh"

TARGET="${1:-}"
case "${TARGET}" in
    jf10|jf12|abi-floor) ;;
    *) rk_die "usage: check.sh jf10|jf12|abi-floor" ;;
esac
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

ETAG="$(python3 - "${OUT}/index.html" <<'PY'
import hashlib
import pathlib
import sys

print('"rk-' + hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest() + '"')
PY
)"

CONDITIONAL_STATUS="$(curl --silent --show-error \
    -D "${OUT}/conditional.headers" --output "${OUT}/conditional.body" \
    --write-out '%{http_code}' \
    -H "If-None-Match: ${ETAG}" "${ORIGIN}/web/index.html")"

HTTP_INFO_RAW="$(python3 - \
    "${OUT}/generation.headers" "${OUT}/generation.json" \
    "${OUT}/kit.headers" "${OUT}/kit.js" \
    "${OUT}/shell.headers" "${OUT}/index.html" \
    "${OUT}/conditional.headers" "${OUT}/conditional.body" <<'PY'
import hashlib
import pathlib
import re
import sys

(
    generation_headers_path,
    generation_body_path,
    kit_headers_path,
    kit_body_path,
    shell_headers_path,
    shell_body_path,
    conditional_headers_path,
    conditional_body_path,
) = map(pathlib.Path, sys.argv[1:])


def fail(message):
    raise SystemExit(f"FATAL: {message}")


def parse_headers(path):
    try:
        text = path.read_bytes().decode("iso-8859-1")
    except OSError as error:
        fail(f"cannot read response headers {path}: {error}")
    statuses = []
    fields = {}
    for raw_line in text.splitlines():
        line = raw_line.strip("\r")
        match = re.fullmatch(r"HTTP/\S+\s+([0-9]{3})(?:\s+.*)?", line)
        if match:
            statuses.append(int(match.group(1)))
            continue
        name, separator, value = line.partition(":")
        if separator:
            fields.setdefault(name.strip().lower(), []).append(value.strip())
    return statuses, fields


def one(fields, name, expected=None):
    values = fields.get(name.lower(), [])
    if len(values) != 1:
        fail(f"{name} must occur exactly once, got {values!r}")
    if expected is not None and values[0] != expected:
        fail(f"{name} is {values[0]!r}, expected {expected!r}")
    return values[0]


def absent(fields, *names):
    unexpected = {name: fields[name.lower()] for name in names if fields.get(name.lower())}
    if unexpected:
        fail(f"bodyless response has forbidden representation framing: {unexpected!r}")


def framing(fields, body, label):
    lengths = fields.get("content-length", [])
    transfers = fields.get("transfer-encoding", [])
    if len(lengths) == 1 and not transfers:
        try:
            length = int(lengths[0])
        except ValueError:
            fail(f"{label} Content-Length is malformed: {lengths[0]!r}")
        if length != len(body):
            fail(f"{label} Content-Length {length} differs from {len(body)} bytes")
        return "content-length"
    if not lengths and len(transfers) == 1 and transfers[0].lower() == "chunked":
        return "chunked"
    fail(
        f"{label} must have exactly one valid framing mode; "
        f"Content-Length={lengths!r}, Transfer-Encoding={transfers!r}"
    )


generation_status, generation_headers = parse_headers(generation_headers_path)
kit_status, kit_headers = parse_headers(kit_headers_path)
shell_status, shell_headers = parse_headers(shell_headers_path)
conditional_status, conditional_headers = parse_headers(conditional_headers_path)
if generation_status != [200] or kit_status != [200] or shell_status != [200]:
    fail(
        "direct response status sequence differs: "
        f"generation={generation_status}, kit={kit_status}, shell={shell_status}"
    )
if conditional_status != [304]:
    fail(f"conditional response status sequence differs: {conditional_status}")

generation_body = generation_body_path.read_bytes()
kit_body = kit_body_path.read_bytes()
shell_body = shell_body_path.read_bytes()
conditional_body = conditional_body_path.read_bytes()
if conditional_body:
    fail(f"304 response unexpectedly carried {len(conditional_body)} body bytes")

one(
    generation_headers,
    "Cache-Control",
    "no-store, no-cache, must-revalidate, max-age=0",
)
one(kit_headers, "Cache-Control", "public, max-age=31536000, immutable")
shell_cache = one(shell_headers, "Cache-Control", "no-cache")
shell_etag = one(shell_headers, "ETag")
expected_etag = '"rk-' + hashlib.sha256(shell_body).hexdigest() + '"'
if shell_etag != expected_etag:
    fail(f"shell ETag {shell_etag!r} is not derived from its exact body bytes")
if shell_headers.get("last-modified"):
    fail("transformed shell retained Last-Modified")
if shell_headers.get("content-encoding"):
    fail("identity shell request unexpectedly returned Content-Encoding")

conditional_etag = one(conditional_headers, "ETag", shell_etag)
conditional_cache = one(
    conditional_headers,
    "Cache-Control",
    "no-cache",
)
one(conditional_headers, "Content-Type", one(shell_headers, "Content-Type"))
absent(
    conditional_headers,
    "Content-Encoding",
    "Content-Length",
    "Transfer-Encoding",
    "Last-Modified",
    "Accept-Ranges",
    "Content-Range",
    "Expires",
    "Pragma",
)

print(shell_etag)
print(hashlib.sha256(shell_body).hexdigest())
print(len(shell_body))
print(shell_cache)
print(framing(generation_headers, generation_body, "generation"))
print(framing(kit_headers, kit_body, "kit.js"))
print(framing(shell_headers, shell_body, "shell"))
print(conditional_etag)
print(conditional_cache)
PY
)" || rk_die "${TARGET}: exact HTTP response contract validation failed"
readarray -t HTTP_INFO <<< "${HTTP_INFO_RAW}"
ETAG="${HTTP_INFO[0]}"
SHELL_SHA256="${HTTP_INFO[1]}"
SHELL_BYTES="${HTTP_INFO[2]}"
SHELL_CACHE_CONTROL="${HTTP_INFO[3]}"
GENERATION_FRAMING="${HTTP_INFO[4]}"
KIT_FRAMING="${HTTP_INFO[5]}"
SHELL_FRAMING="${HTTP_INFO[6]}"
CONDITIONAL_ETAG="${HTTP_INFO[7]}"
CONDITIONAL_CACHE_CONTROL="${HTTP_INFO[8]}"

[ "${CONDITIONAL_STATUS}" = "304" ] || \
    rk_die "${TARGET}: If-None-Match returned ${CONDITIONAL_STATUS}, expected 304"

curl --fail --silent --show-error \
    -H "${AUTH}" -o "${OUT}/diagnostics.json" \
    "${ORIGIN}/RefreshKit/Diagnostics"
curl --fail --silent --show-error \
    -H "${AUTH}" -o "${OUT}/plugins.json" \
    "${ORIGIN}/Plugins"

python3 - "${OUT}/diagnostics.json" "${OUT}/plugins.json" \
    "${OUT}/generation.json" "${RK_PLUGIN_GUID}" <<'PY'
import json
import re
import sys

diagnostics_path, plugins_path, generation_path, guid = sys.argv[1:]
with open(diagnostics_path, encoding="utf-8") as handle:
    diagnostics = json.load(handle)
with open(plugins_path, encoding="utf-8") as handle:
    plugins = json.load(handle)
with open(generation_path, encoding="utf-8") as handle:
    generation_document = json.load(handle)

def value(document, name):
    return document.get(name, document.get(name[:1].lower() + name[1:]))

def matches(rows):
    if not isinstance(rows, list):
        raise SystemExit("FATAL: plugin inventory is not an array")
    normalized = guid.lower().replace("-", "")
    return [
        row for row in rows
        if isinstance(row, dict)
        and str(value(row, "Id") or "").lower().replace("-", "") == normalized
    ]

inventory_matches = matches(plugins)
if len(inventory_matches) != 1:
    raise SystemExit("FATAL: authenticated Refresh Kit inventory is not exact")
if not isinstance(diagnostics, dict):
    raise SystemExit("FATAL: diagnostics response is not an object")
generation = value(diagnostics, "Generation")
kit_version = value(diagnostics, "KitVersion")
rows = value(diagnostics, "Plugins")
diagnostic_matches = matches(rows)
if not generation or not kit_version or len(diagnostic_matches) != 1:
    raise SystemExit(f"FATAL: incomplete diagnostics response: {diagnostics!r}")
if generation != value(generation_document, "CacheKey") \
        or value(diagnostics, "BuildId") != value(generation_document, "BuildId"):
    raise SystemExit("FATAL: generation and diagnostics identities differ")

inventory = inventory_matches[0]
diagnostic = diagnostic_matches[0]
version = value(generation_document, "Version")
if value(inventory, "Name") != "Jellyfin Refresh Kit" \
        or value(inventory, "Version") != version \
        or str(value(inventory, "Status") or "").lower() != "active":
    raise SystemExit("FATAL: authenticated Refresh Kit record is not the active candidate")
if value(diagnostic, "Version") != version \
        or str(value(diagnostic, "Status") or "").lower() != "active" \
        or value(diagnostic, "IsLoaded") is not True \
        or re.fullmatch(r"[0-9a-f]{32}", str(value(diagnostic, "LoadedModuleIdentity") or "")) is None:
    raise SystemExit("FATAL: diagnostics Refresh Kit record is not the exact loaded candidate")
for name in (
    "AssetScanTruncated",
    "AssetScanUnavailable",
    "UsingLastGoodAssets",
    "ConfigurationScanTruncated",
    "ConfigurationScanUnavailable",
    "UsingLastGoodConfiguration",
    "UsingLastKnownPluginRecord",
):
    if value(diagnostic, name) is not False:
        raise SystemExit(f"FATAL: diagnostics {name} is not exactly false")
PY

curl --fail --silent --show-error \
    -o "${OUT}/public.json" "${ORIGIN}/System/Info/Public"
PUBLIC_VERSION="$(python3 - "${OUT}/public.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    data = json.load(handle)
print(data.get("Version", data.get("version", "")))
PY
)"
KIT_VERSION="$(python3 - "${OUT}/diagnostics.json" <<'PY'
import json, sys
d=json.load(open(sys.argv[1], encoding="utf-8"))
print(d.get("KitVersion", d.get("kitVersion", "")))
PY
)"

python3 - "${OUT}/result.json" "${TARGET}" "${PUBLIC_VERSION}" "${GENERATION}" \
    "${KIT_VERSION}" "${ETAG}" "${CONDITIONAL_STATUS}" \
    "${SHELL_SHA256}" "${SHELL_BYTES}" "${SHELL_CACHE_CONTROL}" \
    "${GENERATION_FRAMING}" "${KIT_FRAMING}" "${SHELL_FRAMING}" \
    "${CONDITIONAL_ETAG}" "${CONDITIONAL_CACHE_CONTROL}" <<'PY'
import json
import sys
(
    path, target, server, generation, kit, etag, conditional,
    shell_sha256, shell_bytes, shell_cache_control,
    generation_framing, kit_framing, shell_framing,
    conditional_etag, conditional_cache_control,
) = sys.argv[1:]
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump({
        "target": target,
        "serverVersion": server,
        "generation": generation,
        "kitVersion": kit,
        "shellEtag": etag,
        "shellSha256": shell_sha256,
        "shellBytes": int(shell_bytes),
        "shellCacheControl": shell_cache_control,
        "conditionalStatus": int(conditional),
        "conditionalEtag": conditional_etag,
        "conditionalCacheControl": conditional_cache_control,
        "directResponseFraming": {
            "generation": generation_framing,
            "kit": kit_framing,
            "shell": shell_framing,
        },
        "exactHttpContractPassed": True,
        "endpointAndShellChecksPassed": True,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

rk_log "${TARGET}: endpoints, diagnostics and shell PASS (server ${PUBLIC_VERSION}, kit ${KIT_VERSION}, generation ${GENERATION})"
