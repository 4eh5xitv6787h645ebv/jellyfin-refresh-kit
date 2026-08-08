#!/usr/bin/env bash
#
# Builds the standalone Jellyfin Refresh Kit plugin — ONCE PER SUPPORTED SERVER
# GENERATION — and produces the artefacts a Jellyfin plugin repository needs:
#
#   plugin/build/jellyfin-refresh-kit_<version>.zip        Jellyfin 10.11.x
#                                                          (net9.0, abi 10.11.0.0)
#   plugin/build/stage/                                    its contents, unzipped
#   plugin/build/jellyfin-refresh-kit_<version>_jf12.zip   Jellyfin 12.x
#                                                          (net10.0, abi 12.0.0.0)
#   plugin/build/stage-jf12/                               its contents, unzipped
#
# Either stage directory drops straight into /config/plugins/<anything>/ for a
# manual install — pick the one matching the server.
#
# It prints each zip's MD5 — the value the matching manifest.json entry's
# "checksum" must carry. With --update-manifest it writes those checksums (and
# the timestamp) into the repository-root manifest.json for you, matching
# entries on (version, targetAbi) so the two builds never overwrite each other.
#
# WHY TWO ZIPS AND NOT ONE. A Jellyfin plugin zip contains a framework-specific
# assembly and a meta.json declaring the ABI it was built for. 10.11 runs a
# net9.0 host and 12 runs a net10.0 host, so no single assembly satisfies both:
# the server that loads the wrong one fails to load the plugin at all. Two zips
# behind one manifest is the arrangement that lets a single plugin-repository
# URL serve both server generations — the server filters by targetAbi and only
# ever sees the build it can run.
#
# The client runtime (jellyfin-refresh-kit.js) is NOT copied here: the csproj
# embeds it straight from the repository root at compile time, so there is
# exactly one copy of that file in the tree, shared by both builds.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_DIR="${SCRIPT_DIR}/Jellyfin.Plugin.RefreshKit"
BUILD_DIR="${SCRIPT_DIR}/build"

# The version lives in exactly one place: the csproj. Everything else reads it.
VERSION="$(grep -oPm1 '(?<=<Version>)[^<]+' "${PROJECT_DIR}/Jellyfin.Plugin.RefreshKit.csproj")"
GUID="$(grep -oPm1 '(?<=new Guid\(")[0-9a-fA-F-]+' "${PROJECT_DIR}/Plugin.cs")"

# The build matrix, one row per supported server generation:
#
#   <tfm>|<targetAbi>|<stage dir name>|<zip suffix>|<changelog>
#
# The frameworks are not a preference — they are what each host runs, read out
# of its own jellyfin.runtimeconfig.json. Adding a generation is adding a row.
TARGETS=(
    "net9.0|10.11.0.0|stage||Initial standalone plugin release."
    "net10.0|12.0.0.0|stage-jf12|_jf12|Initial standalone plugin release — Jellyfin 12 build (net10.0)."
)

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

# One timestamp for the whole run: the two zips are one release, and a manifest
# where the same version carries two different timestamps invites the question
# of which one is "the" release.
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Collected for the summary and for --update-manifest: "<targetAbi>|<md5>" rows.
RESULTS=()

for row in "${TARGETS[@]}"; do
    IFS='|' read -r TFM TARGET_ABI STAGE_NAME ZIP_SUFFIX CHANGELOG <<<"${row}"

    STAGE_DIR="${BUILD_DIR}/${STAGE_NAME}"
    PUBLISH_DIR="${BUILD_DIR}/publish-${TFM}"
    ZIP_NAME="jellyfin-refresh-kit_${VERSION}${ZIP_SUFFIX}.zip"

    echo
    echo "==> ${TFM} (targetAbi ${TARGET_ABI}) -> ${ZIP_NAME}"
    mkdir -p "${STAGE_DIR}"

    "${DOTNET}" publish "${PROJECT_DIR}/Jellyfin.Plugin.RefreshKit.csproj" \
        -c Release \
        -f "${TFM}" \
        -o "${PUBLISH_DIR}" \
        --nologo

    # A Jellyfin plugin folder holds the plugin's own assemblies and meta.json.
    # Everything the server already provides (Jellyfin.*, MediaBrowser.*, the
    # ASP.NET shared framework) is deliberately NOT shipped: loading a second
    # copy of a host assembly is how a plugin ends up with duplicate types at
    # runtime. This matters more across two generations, not less — a stray
    # MediaBrowser.Common from the wrong ABI is exactly the failure mode.
    cp "${PUBLISH_DIR}/Jellyfin.Plugin.RefreshKit.dll" "${STAGE_DIR}/"
    if [ -f "${PUBLISH_DIR}/Jellyfin.Plugin.RefreshKit.pdb" ]; then
        cp "${PUBLISH_DIR}/Jellyfin.Plugin.RefreshKit.pdb" "${STAGE_DIR}/"
    fi

    # meta.json is the manifest the SERVER keeps for the installed plugin.
    #
    # "autoUpdate" is deliberately true. Jellyfin PRESERVES the value it finds
    # here rather than resetting it — verified on 10.11.11: a copy of this
    # folder placed by hand kept "autoUpdate": false across a load and rewrite,
    # while every marketplace-installed plugin defaults to true. Shipping false
    # therefore opts the plugin out of the "Update Plugins" task permanently,
    # which is a strange thing for a plugin whose entire purpose is that nobody
    # should have to think about running stale code.
    cat > "${STAGE_DIR}/meta.json" <<EOF
{
    "category": "General",
    "guid": "${GUID}",
    "name": "Jellyfin Refresh Kit",
    "overview": "Fixes stale-cache and hard-refresh problems for every installed plugin.",
    "description": "Install this one plugin and cache/hard-refresh problems are fixed for all your other plugins, none of which need to know it exists. It serves index.html through a revalidating middleware, stamps other plugins' unversioned script tags with a cache-busting ?rkv= parameter derived from every installed plugin's id/version/binary timestamp, and ships the jellyfin-refresh-kit.js client runtime configured to poll that generation and safely reload open tabs when it changes.",
    "owner": "jellyfin-refresh-kit",
    "targetAbi": "${TARGET_ABI}",
    "framework": "${TFM}",
    "version": "${VERSION}",
    "changelog": "${CHANGELOG}",
    "timestamp": "${TIMESTAMP}",
    "status": "Active",
    "autoUpdate": true,
    "imagePath": ""
}
EOF

    # python3's zipfile rather than the `zip` binary: the SDK is already a
    # prerequisite, python3 is present on every distro Jellyfin ships for, and
    # `zip` is not installed by default on several of them (Arch, minimal
    # Debian images).
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
    RESULTS+=("${TARGET_ABI}|${CHECKSUM}|${ZIP_NAME}|${STAGE_DIR}|${TFM}")
done

echo
echo "==> Artefacts (version ${VERSION}, timestamp ${TIMESTAMP})"
for result in "${RESULTS[@]}"; do
    IFS='|' read -r R_ABI R_SUM R_ZIP R_STAGE R_TFM <<<"${result}"
    echo "    abi ${R_ABI} (${R_TFM})"
    echo "      zip       : ${BUILD_DIR}/${R_ZIP}"
    echo "      stage dir : ${R_STAGE}"
    echo "      md5       : ${R_SUM}"
done

if [ "${1:-}" = "--update-manifest" ]; then
    # Entries are matched on (version, targetAbi), not version alone: the two
    # builds share a version number and differ only in the ABI they declare, so
    # matching on version would have the second build overwrite the first's
    # checksum and quietly ship a manifest that fails verification on one of
    # the two server generations.
    python3 - "$REPO_ROOT/manifest.json" "$VERSION" "$TIMESTAMP" "${RESULTS[@]}" <<'PY'
import json, sys

path, version, timestamp = sys.argv[1:4]
checksums = {}
for row in sys.argv[4:]:
    abi, checksum = row.split("|")[:2]
    checksums[abi] = checksum

with open(path) as handle:
    manifest = json.load(handle)

updated = []
for entry in manifest:
    for version_entry in entry.get("versions", []):
        if version_entry.get("version") != version:
            continue
        abi = version_entry.get("targetAbi")
        if abi not in checksums:
            continue
        version_entry["checksum"] = checksums[abi]
        version_entry["timestamp"] = timestamp
        updated.append(abi)

missing = sorted(set(checksums) - set(updated))
if missing:
    raise SystemExit(
        f"manifest.json has no version {version} entry for targetAbi: {', '.join(missing)}")

# THE ORDER OF THESE ENTRIES IS LOAD-BEARING, so it is checked rather than
# trusted. Jellyfin picks a build with
#   versions.Where(targetAbi <= appVersion).OrderByDescending(versionNumber)
# and takes the first. On a 10.11 server the ABI filter alone decides it. On a
# 12 server BOTH entries survive the filter and their version numbers are
# EQUAL, so the only thing separating them is that LINQ's OrderByDescending is
# a stable sort — it preserves the manifest's own order among equals. Highest
# targetAbi first is therefore the difference between a 12 server installing
# the net10.0 build and it installing the net9.0 build it cannot load.
for entry in manifest:
    same_version = [v for v in entry.get("versions", []) if v.get("version") == version]
    abis = [tuple(int(p) for p in v.get("targetAbi", "0").split(".")) for v in same_version]
    if abis != sorted(abis, reverse=True):
        raise SystemExit(
            f"manifest.json: version {version} entries must be ordered highest targetAbi "
            f"first (a 12 server takes the first entry it can run); got "
            f"{[v.get('targetAbi') for v in same_version]}")

with open(path, "w") as handle:
    json.dump(manifest, handle, indent=4)
    handle.write("\n")

for abi in updated:
    print(f"==> manifest.json updated: {version} / abi {abi} -> {checksums[abi]}")
PY
fi
