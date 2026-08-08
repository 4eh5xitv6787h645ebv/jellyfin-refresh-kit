#!/usr/bin/env bash

# Compile two genuine versions of a purpose-built third-party plugin and expose
# them through a target-specific, project-local Jellyfin repository. Refresh Kit
# itself is never built or modified here; run.sh must select an already-verified
# immutable Refresh Kit snapshot first.

set -euo pipefail
umask 022
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${HERE}/common.sh"

TARGET="${1:-}"
case "${TARGET}" in jf10|jf12) ;; *) rk_die "usage: prepare-third-party-repository.sh jf10|jf12" ;; esac

rk_require md5sum python3 sha256sum
[ -n "${RK_BUILD_SNAPSHOT:-}" ] || \
    rk_die "RK_BUILD_SNAPSHOT must be pinned by run.sh before preparing the fixture"
RESOLVED_BUILD_SNAPSHOT="$(readlink -f -- "${RK_BUILD_SNAPSHOT}" 2>/dev/null || true)"
case "${RESOLVED_BUILD_SNAPSHOT}" in
    "${RK_REPO_ROOT}/plugin/.builds/"*) ;;
    *) rk_die "fixture preparation received a build snapshot outside plugin/.builds" ;;
esac
[ -d "${RESOLVED_BUILD_SNAPSHOT}" ] || rk_die "fixture preparation build snapshot does not exist"
RK_BUILD_SNAPSHOT="${RESOLVED_BUILD_SNAPSHOT}"
export RK_BUILD_SNAPSHOT
rk_pin_build_snapshot
RK_CLIENT_VERSION="$(python3 - "${RK_REPO_ROOT}/jellyfin-refresh-kit.js" <<'PY'
import re
import sys

text = open(sys.argv[1], encoding="utf-8").read()
match = re.search(r"\bKIT_VERSION\s*=\s*'([0-9]+(?:\.[0-9]+)+)'", text)
if not match:
    raise SystemExit("FATAL: could not resolve KIT_VERSION from jellyfin-refresh-kit.js")
print(match.group(1))
PY
)"

SELECTED_DOTNET_ROOT=''
if [ -n "${DOTNET_ROOT:-}" ] && [ -x "${DOTNET_ROOT}/dotnet" ]; then
    SELECTED_DOTNET_ROOT="${DOTNET_ROOT}"
elif [ -x "${HOME}/.dotnet/dotnet" ]; then
    SELECTED_DOTNET_ROOT="${HOME}/.dotnet"
else
    DOTNET_LAUNCHER="$(command -v dotnet || true)"
    [ -n "${DOTNET_LAUNCHER}" ] && \
        SELECTED_DOTNET_ROOT="$(python3 -c 'import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "${DOTNET_LAUNCHER}")"
fi
[ -x "${SELECTED_DOTNET_ROOT}/dotnet" ] || rk_die "repository-pinned .NET SDK launcher was not found"
export DOTNET_ROOT="${SELECTED_DOTNET_ROOT}"
DOTNET="${SELECTED_DOTNET_ROOT}/dotnet"
EXPECTED_SDK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["sdk"]["version"])' "${RK_REPO_ROOT}/global.json")"
ACTUAL_SDK="$("${DOTNET}" --version)"
[ "${ACTUAL_SDK}" = "${EXPECTED_SDK}" ] || \
    rk_die "third-party fixture requires .NET SDK ${EXPECTED_SDK}, selected launcher reports ${ACTUAL_SDK}"

case "${TARGET}" in
    jf10)
        TFM='net9.0'
        ABI='10.11.0.0'
        ;;
    jf12)
        TFM='net10.0'
        ABI='12.0.0.0'
        ;;
esac

RK_STAGE="$(rk_stage "${TARGET}")"
readarray -t RK_IDENTITY < <(python3 - "${RK_STAGE}/meta.json" "${TFM}" "${ABI}" <<'PY'
import json
import re
import sys

path, expected_framework, expected_abi = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    metadata = json.load(handle)
if metadata.get("framework") != expected_framework or metadata.get("targetAbi") != expected_abi:
    raise SystemExit(f"FATAL: Refresh Kit stage metadata does not match {expected_framework}/{expected_abi}")
version = str(metadata.get("version", ""))
revision = str(metadata.get("sourceRevision", ""))
tree = str(metadata.get("sourceTreeSha256", ""))
if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", version):
    raise SystemExit(f"FATAL: invalid Refresh Kit stage version {version!r}")
if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"[0-9a-f]{64}", tree):
    raise SystemExit("FATAL: invalid Refresh Kit source identity in stage metadata")
print(version)
print(revision)
print(tree)
PY
)
[ "${#RK_IDENTITY[@]}" -eq 3 ] || rk_die "could not resolve Refresh Kit stage identity"

FIXTURE_ID='8f42f34a-a7d1-4b6e-9b77-17ed99d7a216'
FIXTURE_NAME='Refresh Kit Lifecycle Probe'
FIXTURE_ROOT="${RK_JELLYFIN_DIR}/fixtures/LifecycleProbe"
FIXTURE_PROJECT="${FIXTURE_ROOT}/LifecycleProbe.csproj"
LOCK_FILE="${FIXTURE_ROOT}/packages.lock.json"
BUILD_ROOT="${RK_STATE_DIR}/third-party-build/${TARGET}"
REPOSITORY_ROOT="${RK_STATE_DIR}/repository"
TARGET_DIR="${REPOSITORY_ROOT}/${TARGET}/third-party"
REPOSITORY_URL="http://repository:8080/${TARGET}/third-party/manifest.json"

[ -f "${FIXTURE_PROJECT}" ] || rk_die "third-party fixture project is missing"
[ -f "${LOCK_FILE}" ] || rk_die "third-party fixture lock file is missing"
case "${BUILD_ROOT}:${TARGET_DIR}" in
    "${RK_STATE_DIR}/third-party-build/${TARGET}:${RK_STATE_DIR}/repository/${TARGET}/third-party") ;;
    *) rk_die "refusing unexpected third-party scratch paths" ;;
esac
rm -rf -- "${BUILD_ROOT}" "${TARGET_DIR}"
mkdir -p "${BUILD_ROOT}" "${TARGET_DIR}"

build_release() {
    local release="$1" version="$2" work publish stage package asset dll_sha package_md5 package_sha
    work="${BUILD_ROOT}/${release}"
    publish="${work}/publish"
    stage="${work}/stage"
    package="${TARGET_DIR}/${release}.zip"
    mkdir -p "${publish}" "${stage}"

    "${DOTNET}" restore "${FIXTURE_PROJECT}" --locked-mode --nologo \
        -p:ProbeRelease="${release}" \
        -p:ProbeVersion="${version}" \
        -p:BaseIntermediateOutputPath="${work}/obj/" \
        -p:BaseOutputPath="${work}/bin/" >/dev/null
    "${DOTNET}" publish "${FIXTURE_PROJECT}" --no-restore --nologo \
        --configuration Release --framework "${TFM}" --output "${publish}" \
        -p:ProbeRelease="${release}" \
        -p:ProbeVersion="${version}" \
        -p:BaseIntermediateOutputPath="${work}/obj/" \
        -p:BaseOutputPath="${work}/bin/" \
        -p:PathMap="${RK_REPO_ROOT}=/_/" >/dev/null

    [ -s "${publish}/Jellyfin.Plugin.RefreshKitLifecycleProbe.dll" ] || \
        rk_die "${TARGET}/${release}: fixture DLL was not published"
    [ -s "${publish}/Jellyfin.Plugin.RefreshKitLifecycleProbe.pdb" ] || \
        rk_die "${TARGET}/${release}: fixture PDB was not published"
    cp -- "${publish}/Jellyfin.Plugin.RefreshKitLifecycleProbe.dll" "${stage}/"
    cp -- "${publish}/Jellyfin.Plugin.RefreshKitLifecycleProbe.pdb" "${stage}/"
    python3 - "${stage}/Jellyfin.Plugin.RefreshKitLifecycleProbe.dll" \
        "${stage}/Jellyfin.Plugin.RefreshKitLifecycleProbe.pdb" \
        "${release}" "${version}" "${RK_REPO_ROOT}" <<'PY'
import sys

dll_path, pdb_path, release, version, checkout = sys.argv[1:]
content = open(dll_path, "rb").read()
other_release = "v2" if release == "v1" else "v1"
expected_marker = f"Disposable third-party lifecycle fixture compiled as {release}.".encode("utf-16le")
wrong_marker = f"Disposable third-party lifecycle fixture compiled as {other_release}.".encode("utf-16le")
if expected_marker not in content or wrong_marker in content:
    raise SystemExit(f"FATAL: {dll_path} does not contain only the expected compiled release marker {release}")
if version.encode("ascii") not in content or version.encode("utf-16le") not in content:
    raise SystemExit(f"FATAL: {dll_path} does not contain its expected compiled assembly version {version}")
for artifact in (dll_path, pdb_path):
    artifact_content = open(artifact, "rb").read()
    if checkout.encode() in artifact_content or checkout.encode("utf-16le") in artifact_content:
        raise SystemExit(f"FATAL: deterministic fixture artifact leaks checkout path: {artifact}")
PY
    for asset in html js css; do
        cp -- "${FIXTURE_ROOT}/Assets/probe-${release}.${asset}" \
            "${stage}/lifecycle-probe.${asset}"
    done
    dll_sha="$(sha256sum "${stage}/Jellyfin.Plugin.RefreshKitLifecycleProbe.dll" | awk '{print $1}')"

    python3 - "${stage}/meta.json" "${TARGET}" "${TFM}" "${ABI}" \
        "${release}" "${version}" "${FIXTURE_ID}" "${dll_sha}" <<'PY'
import json
import sys

path, target, tfm, abi, release, version, guid, dll_sha = sys.argv[1:]
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump({
        "assemblies": ["Jellyfin.Plugin.RefreshKitLifecycleProbe.dll"],
        "autoUpdate": False,
        "category": "General",
        "changelog": f"Genuine lifecycle probe {release}.",
        "description": f"Disposable third-party lifecycle fixture compiled as {release}.",
        "name": "Refresh Kit Lifecycle Probe",
        "guid": guid,
        "overview": "Purpose-built third-party lifecycle fixture.",
        "owner": "jellyfin-refresh-kit lifecycle lab",
        "release": release,
        "status": "Active",
        "timestamp": "2026-08-08T00:00:00Z" if release == "v1" else "2026-08-09T00:00:00Z",
        "version": version,
        "target": target,
        "targetFramework": tfm,
        "targetAbi": abi,
        "assemblySha256": dll_sha,
        "purpose": "Disposable genuine third-party lifecycle fixture",
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

    python3 - "${package}.part" "${stage}" <<'PY'
import os
import sys
import zipfile

output, source = sys.argv[1:]
names = [
    "Jellyfin.Plugin.RefreshKitLifecycleProbe.dll",
    "Jellyfin.Plugin.RefreshKitLifecycleProbe.pdb",
    "lifecycle-probe.html",
    "lifecycle-probe.js",
    "lifecycle-probe.css",
    "meta.json",
]
with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
    for name in names:
        path = os.path.join(source, name)
        info = zipfile.ZipInfo(name, date_time=(2026, 8, 9, 0, 0, 0))
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        with open(path, "rb") as handle:
            archive.writestr(info, handle.read())
PY
    mv -f -- "${package}.part" "${package}"
    package_md5="$(md5sum "${package}" | awk '{print $1}')"
    package_sha="$(sha256sum "${package}" | awk '{print $1}')"
    printf '%s\n%s\n%s\n' "${dll_sha}" "${package_md5}" "${package_sha}" \
        > "${work}/identity.txt"
}

build_release v1 1.0.0.0
build_release v2 2.0.0.0

readarray -t V1_IDENTITY < "${BUILD_ROOT}/v1/identity.txt"
readarray -t V2_IDENTITY < "${BUILD_ROOT}/v2/identity.txt"
[ "${V1_IDENTITY[0]}" != "${V2_IDENTITY[0]}" ] || \
    rk_die "${TARGET}: v1 and v2 fixture assemblies are byte-identical"
[ "${V1_IDENTITY[2]}" != "${V2_IDENTITY[2]}" ] || \
    rk_die "${TARGET}: v1 and v2 fixture packages are byte-identical"

python3 - "${TARGET_DIR}/manifest-update.json.part" "${TARGET}" "${ABI}" \
    "${FIXTURE_ID}" "${FIXTURE_NAME}" "${V1_IDENTITY[1]}" "${V2_IDENTITY[1]}" <<'PY'
import json
import sys

path, target, abi, guid, name, v1_md5, v2_md5 = sys.argv[1:]
base = f"http://repository:8080/{target}/third-party"
v1 = {
    "version": "1.0.0.0",
    "changelog": "Genuine lifecycle probe v1.",
    "targetAbi": abi,
    "sourceUrl": f"{base}/v1.zip",
    "checksum": v1_md5,
    "timestamp": "2026-08-08T00:00:00Z",
}
v2 = {
    "version": "2.0.0.0",
    "changelog": "Genuine lifecycle probe v2 with changed assembly and browser assets.",
    "targetAbi": abi,
    "sourceUrl": f"{base}/v2.zip",
    "checksum": v2_md5,
    "timestamp": "2026-08-09T00:00:00Z",
}
plugin = {
    "guid": guid,
    "name": name,
    "overview": "Purpose-built third-party lifecycle fixture.",
    "description": "Two genuinely compiled versions used only by the disposable Refresh Kit lab.",
    "owner": "jellyfin-refresh-kit lifecycle lab",
    "category": "General",
    "imageUrl": "",
}
for output, versions in (
    (path, [v2, v1]),
    (path.replace("manifest-update.json.part", "manifest-baseline.json.part"), [v1]),
):
    document = dict(plugin)
    document["versions"] = versions
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump([document], handle, indent=2, sort_keys=True)
        handle.write("\n")
PY
mv -f -- "${TARGET_DIR}/manifest-update.json.part" "${TARGET_DIR}/manifest-update.json"
mv -f -- "${TARGET_DIR}/manifest-baseline.json.part" "${TARGET_DIR}/manifest-baseline.json"
cp -- "${TARGET_DIR}/manifest-baseline.json" "${TARGET_DIR}/manifest.json.part"
mv -f -- "${TARGET_DIR}/manifest.json.part" "${TARGET_DIR}/manifest.json"
printf 'ok\n' > "${REPOSITORY_ROOT}/health.txt"

python3 - "${RK_STATE_DIR}/${TARGET}.third-party.json.part" "${TARGET}" \
    "${REPOSITORY_URL}" "${FIXTURE_ID}" "${FIXTURE_NAME}" \
    "${V1_IDENTITY[0]}" "${V1_IDENTITY[1]}" "${V1_IDENTITY[2]}" \
    "${V2_IDENTITY[0]}" "${V2_IDENTITY[1]}" "${V2_IDENTITY[2]}" \
    "$(basename "${RK_BUILD_SNAPSHOT}")" "${RK_CLIENT_VERSION}" \
    "${RK_IDENTITY[0]}" "${RK_IDENTITY[1]}" "${RK_IDENTITY[2]}" <<'PY'
import json
import sys

(path, target, repository_url, guid, name,
 v1_dll, v1_md5, v1_sha, v2_dll, v2_md5, v2_sha, snapshot, kit_version,
 rk_version, rk_revision, rk_tree) = sys.argv[1:]
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump({
        "target": target,
        "repositoryUrl": repository_url,
        "fixtureId": guid,
        "fixtureName": name,
        "baselineVersion": "1.0.0.0",
        "candidateVersion": "2.0.0.0",
        "baselineAssemblySha256": v1_dll,
        "baselineMd5": v1_md5,
        "baselineSha256": v1_sha,
        "candidateAssemblySha256": v2_dll,
        "candidateMd5": v2_md5,
        "candidateSha256": v2_sha,
        "refreshKitSnapshot": snapshot,
        "refreshKitRuntimeVersion": kit_version,
        "refreshKitPackageVersion": rk_version,
        "refreshKitSourceRevision": rk_revision,
        "refreshKitSourceTreeSha256": rk_tree,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
mv -f -- "${RK_STATE_DIR}/${TARGET}.third-party.json.part" \
    "${RK_STATE_DIR}/${TARGET}.third-party.json"

rk_log "${TARGET}: genuine third-party repository ready (1.0.0.0 -> 2.0.0.0)"
