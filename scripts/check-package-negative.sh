#!/usr/bin/env bash
# Prove the package verifier rejects nested content on either side of the
# stage/archive boundary.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

SOURCE_SNAPSHOT="$(readlink -f plugin/build)"
case "${SOURCE_SNAPSHOT}" in
    "${REPO_ROOT}/plugin/.builds/"*) ;;
    *) echo "FATAL: package negative test needs an immutable source snapshot." >&2; exit 1 ;;
esac

FIXTURE="$(mktemp -d "${REPO_ROOT}/plugin/.builds/negative-inventory-XXXXXXXX")"
LOG="$(mktemp)"
cleanup() {
    if [ -n "${FIXTURE:-}" ] && [ -d "${FIXTURE}" ]; then
        chmod -R u+w "${FIXTURE}" 2>/dev/null || true
        rm -rf -- "${FIXTURE}"
    fi
    [ -z "${LOG:-}" ] || rm -f -- "${LOG}"
}
trap cleanup EXIT

cp -a -- "${SOURCE_SNAPSHOT}/." "${FIXTURE}/"
chmod -R u+w "${FIXTURE}"
mkdir -p "${FIXTURE}/stage/unexpected"
printf 'not shipped in the release archive\n' > "${FIXTURE}/stage/unexpected/payload.txt"
find "${FIXTURE}" -type f -exec chmod 0444 {} +
find "${FIXTURE}" -type d -exec chmod 0555 {} +

if python3 scripts/verify-package.py \
    --build-dir "${FIXTURE}" \
    --manifest manifest.json \
    --manifest-mode structure \
    --require-immutable-snapshot >"${LOG}" 2>&1; then
    echo "FATAL: package verifier accepted nested stage content absent from the ZIP." >&2
    exit 1
fi
grep -Fq 'stage: expected exactly' "${LOG}" || {
    echo "FATAL: package verifier failed for the wrong reason:" >&2
    sed -n '1,160p' "${LOG}" >&2
    exit 1
}

chmod -R u+w "${FIXTURE}" 2>/dev/null || true
rm -rf -- "${FIXTURE}"
FIXTURE="$(mktemp -d "${REPO_ROOT}/plugin/.builds/negative-archive-XXXXXXXX")"
cp -a -- "${SOURCE_SNAPSHOT}/." "${FIXTURE}/"
chmod -R u+w "${FIXTURE}"
VERSION="$(python3 -c 'import xml.etree.ElementTree as E; print(E.parse("plugin/Jellyfin.Plugin.RefreshKit/Jellyfin.Plugin.RefreshKit.csproj").getroot().findtext(".//Version"))')"
python3 - "${FIXTURE}/jellyfin-refresh-kit_${VERSION}.zip" <<'PY'
import sys
import zipfile

with zipfile.ZipFile(sys.argv[1], "a") as archive:
    archive.writestr("unexpected/payload.txt", b"not present in the verified stage\n")
PY
find "${FIXTURE}" -type f -exec chmod 0444 {} +
find "${FIXTURE}" -type d -exec chmod 0555 {} +

if python3 scripts/verify-package.py \
    --build-dir "${FIXTURE}" \
    --manifest manifest.json \
    --manifest-mode structure \
    --require-immutable-snapshot >"${LOG}" 2>&1; then
    echo "FATAL: package verifier accepted nested archive content absent from the stage." >&2
    exit 1
fi
grep -Fq 'members/order must be' "${LOG}" || {
    echo "FATAL: package verifier failed for the wrong reason:" >&2
    sed -n '1,160p' "${LOG}" >&2
    exit 1
}

chmod -R u+w "${FIXTURE}" 2>/dev/null || true
rm -rf -- "${FIXTURE}"
FIXTURE="$(mktemp -d "${REPO_ROOT}/plugin/.builds/negative-assemblyref-XXXXXXXX")"
cp -a -- "${SOURCE_SNAPSHOT}/." "${FIXTURE}/"
chmod -R u+w "${FIXTURE}"
VERSION="$(python3 -c 'import xml.etree.ElementTree as E; print(E.parse("plugin/Jellyfin.Plugin.RefreshKit/Jellyfin.Plugin.RefreshKit.csproj").getroot().findtext(".//Version"))')"
python3 - "${FIXTURE}/stage/Jellyfin.Plugin.RefreshKit.dll" \
    "${FIXTURE}/jellyfin-refresh-kit_${VERSION}.zip" <<'PY'
import importlib.util
import os
import pathlib
import struct
import sys
import zipfile

dll_path = pathlib.Path(sys.argv[1])
archive_path = pathlib.Path(sys.argv[2])
verifier_path = pathlib.Path("scripts/verify-package.py").resolve()
spec = importlib.util.spec_from_file_location("rk_verify_package", verifier_path)
if spec is None or spec.loader is None:
    raise SystemExit("FATAL: could not load package verifier")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)

dll_stat = dll_path.stat()
payload = bytearray(dll_path.read_bytes())
references = verifier.assembly_references(payload)
version, version_offset = references["MediaBrowser.Common"]
if version != "10.11.0.0":
    raise SystemExit(f"FATAL: negative fixture starts with unexpected AssemblyRef {version}")
struct.pack_into("<H", payload, version_offset + 4, 1)
dll_path.write_bytes(payload)
os.utime(dll_path, ns=(dll_stat.st_atime_ns, dll_stat.st_mtime_ns))

archive_stat = archive_path.stat()
temporary = archive_path.with_suffix(".zip.part")
with zipfile.ZipFile(archive_path, "r") as source:
    members = [
        (info, bytes(payload) if info.filename == dll_path.name else source.read(info.filename))
        for info in source.infolist()
    ]
with zipfile.ZipFile(temporary, "w") as destination:
    for info, content in members:
        destination.writestr(info, content)
os.replace(temporary, archive_path)
os.utime(archive_path, ns=(archive_stat.st_atime_ns, archive_stat.st_mtime_ns))
PY
find "${FIXTURE}" -type f -exec chmod 0444 {} +
find "${FIXTURE}" -type d -exec chmod 0555 {} +

if python3 scripts/verify-package.py \
    --build-dir "${FIXTURE}" \
    --manifest manifest.json \
    --manifest-mode structure \
    --require-immutable-snapshot >"${LOG}" 2>&1; then
    echo "FATAL: package verifier accepted an AssemblyRef newer than targetAbi." >&2
    exit 1
fi
grep -Fq 'AssemblyRef MediaBrowser.Common version 10.11.1.0 != targetAbi 10.11.0.0' "${LOG}" || {
    echo "FATAL: package verifier failed for the wrong reason:" >&2
    sed -n '1,160p' "${LOG}" >&2
    exit 1
}

echo "==> Package verifier rejected nested stage/archive and AssemblyRef/ABI mismatches"
