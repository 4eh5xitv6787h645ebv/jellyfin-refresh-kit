#!/usr/bin/env bash
#
# Builds the standalone Jellyfin Refresh Kit plugin and produces the artefacts a
# Jellyfin plugin repository needs:
#
#   plugin/build/jellyfin-refresh-kit_<version>.zip   the marketplace zip
#   plugin/build/stage/                               its contents, unzipped
#                                                     (drop straight into
#                                                      /config/plugins/... for a
#                                                      manual install)
#
# and prints the zip's MD5 — the value manifest.json's "checksum" must carry.
# With --update-manifest it writes that checksum (and the timestamp) into the
# repository-root manifest.json for you.
#
# The client runtime (jellyfin-refresh-kit.js) is NOT copied here: the csproj
# embeds it straight from the repository root at compile time, so there is
# exactly one copy of that file in the tree.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_DIR="${SCRIPT_DIR}/Jellyfin.Plugin.RefreshKit"
BUILD_DIR="${SCRIPT_DIR}/build"
STAGE_DIR="${BUILD_DIR}/stage"

# The version lives in exactly one place: the csproj. Everything else reads it.
VERSION="$(grep -oPm1 '(?<=<Version>)[^<]+' "${PROJECT_DIR}/Jellyfin.Plugin.RefreshKit.csproj")"
GUID="$(grep -oPm1 '(?<=new Guid\(")[0-9a-fA-F-]+' "${PROJECT_DIR}/Plugin.cs")"
TARGET_ABI="10.11.0.0"
ZIP_NAME="jellyfin-refresh-kit_${VERSION}.zip"

# The .NET SDK is a user install here; DOTNET_ROOT must point at it or the muxer
# resolves a different (or missing) runtime.
export DOTNET_ROOT="${DOTNET_ROOT:-${HOME}/.dotnet}"
DOTNET="${DOTNET_ROOT}/dotnet"
[ -x "${DOTNET}" ] || DOTNET="$(command -v dotnet)"

if [ ! -f "${REPO_ROOT}/jellyfin-refresh-kit.js" ]; then
    echo "FATAL: ${REPO_ROOT}/jellyfin-refresh-kit.js is missing — the plugin embeds it at build time." >&2
    exit 1
fi

echo "==> Building Jellyfin Refresh Kit ${VERSION} (guid ${GUID})"
rm -rf "${BUILD_DIR}"
mkdir -p "${STAGE_DIR}"

"${DOTNET}" publish "${PROJECT_DIR}/Jellyfin.Plugin.RefreshKit.csproj" \
    -c Release \
    -o "${BUILD_DIR}/publish" \
    --nologo

# A Jellyfin plugin folder holds the plugin's own assemblies and meta.json.
# Everything the server already provides (Jellyfin.*, MediaBrowser.*, the
# ASP.NET shared framework) is deliberately NOT shipped: loading a second copy
# of a host assembly is how a plugin ends up with duplicate types at runtime.
cp "${BUILD_DIR}/publish/Jellyfin.Plugin.RefreshKit.dll" "${STAGE_DIR}/"
if [ -f "${BUILD_DIR}/publish/Jellyfin.Plugin.RefreshKit.pdb" ]; then
    cp "${BUILD_DIR}/publish/Jellyfin.Plugin.RefreshKit.pdb" "${STAGE_DIR}/"
fi

TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "${STAGE_DIR}/meta.json" <<EOF
{
    "category": "General",
    "guid": "${GUID}",
    "name": "Jellyfin Refresh Kit",
    "overview": "Fixes stale-cache and hard-refresh problems for every installed plugin.",
    "description": "Install this one plugin and cache/hard-refresh problems are fixed for all your other plugins, none of which need to know it exists. It serves index.html through a revalidating middleware, stamps other plugins' unversioned script tags with a cache-busting ?rkv= parameter derived from every installed plugin's id/version/binary timestamp, and ships the jellyfin-refresh-kit.js client runtime configured to poll that generation and safely reload open tabs when it changes.",
    "owner": "jellyfin-refresh-kit",
    "targetAbi": "${TARGET_ABI}",
    "framework": "net9.0",
    "version": "${VERSION}",
    "changelog": "Initial standalone plugin release.",
    "timestamp": "${TIMESTAMP}",
    "status": "Active",
    "autoUpdate": false,
    "imagePath": ""
}
EOF

# python3's zipfile rather than the `zip` binary: the SDK is already a
# prerequisite, python3 is present on every distro Jellyfin ships for, and `zip`
# is not installed by default on several of them (Arch, minimal Debian images).
python3 - "${STAGE_DIR}" "${BUILD_DIR}/${ZIP_NAME}" <<'PY'
import os, sys, zipfile

stage, target = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
    for root, _, files in os.walk(stage):
        for name in sorted(files):
            full = os.path.join(root, name)
            archive.write(full, os.path.relpath(full, stage))
PY

CHECKSUM="$(md5sum "${BUILD_DIR}/${ZIP_NAME}" | cut -d' ' -f1)"

echo
echo "==> Artefacts"
echo "    zip       : ${BUILD_DIR}/${ZIP_NAME}"
echo "    stage dir : ${STAGE_DIR}"
echo "    version   : ${VERSION}"
echo "    md5       : ${CHECKSUM}"
echo "    timestamp : ${TIMESTAMP}"

if [ "${1:-}" = "--update-manifest" ]; then
    python3 - "$REPO_ROOT/manifest.json" "$VERSION" "$CHECKSUM" "$TIMESTAMP" <<'PY'
import json, sys

path, version, checksum, timestamp = sys.argv[1:5]
with open(path) as handle:
    manifest = json.load(handle)

for entry in manifest:
    for version_entry in entry.get("versions", []):
        if version_entry.get("version") == version:
            version_entry["checksum"] = checksum
            version_entry["timestamp"] = timestamp
            break
    else:
        raise SystemExit(f"manifest.json has no entry for version {version}")

with open(path, "w") as handle:
    json.dump(manifest, handle, indent=4)
    handle.write("\n")
print(f"==> manifest.json updated: {version} -> {checksum}")
PY
fi
