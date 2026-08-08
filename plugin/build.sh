#!/usr/bin/env bash
# Build the standalone plugin once for each supported Jellyfin generation.
# Builds are serialized and published as immutable snapshots. The public
# plugin/build path is switched atomically only after both packages verify.

set -euo pipefail
umask 022

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_DIR="${SCRIPT_DIR}/Jellyfin.Plugin.RefreshKit"
PROJECT_FILE="${PROJECT_DIR}/Jellyfin.Plugin.RefreshKit.csproj"
VERIFIER="${REPO_ROOT}/scripts/verify-package.py"
PUBLIC_BUILD="${SCRIPT_DIR}/build"
SNAPSHOT_ROOT="${SCRIPT_DIR}/.builds"
WORK_ROOT="${SCRIPT_DIR}/.build-work"
WORK_DIR="${WORK_ROOT}/current"
LOCK_FILE="${SCRIPT_DIR}/.build.lock"
MANIFEST="${REPO_ROOT}/manifest.json"

UPDATE_MANIFEST=false
case "${1:-}" in
    "") ;;
    --update-manifest) UPDATE_MANIFEST=true ;;
    *) echo "Usage: $0 [--update-manifest]" >&2; exit 2 ;;
esac
[ "$#" -le 1 ] || { echo "Usage: $0 [--update-manifest]" >&2; exit 2; }

for required in flock python3; do
    command -v "${required}" >/dev/null 2>&1 || {
        echo "FATAL: ${required} is required to build deterministic packages." >&2
        exit 1
    }
done

cd "${REPO_ROOT}"

# Hold the lock in this shell, with no environment/argument worker bypass.
# Long-lived compiler children receive the descriptor explicitly closed at
# each dotnet invocation below; the shell keeps ownership until publication and
# cleanup are complete.
if [ -n "${RK_BUILD_TEST_ATTEMPT_FILE:-}" ]; then
    case "${RK_BUILD_TEST_ATTEMPT_FILE}" in
        /*) printf '%s\n' "$$" > "${RK_BUILD_TEST_ATTEMPT_FILE}" ;;
        *) echo "FATAL: RK_BUILD_TEST_ATTEMPT_FILE must be absolute." >&2; exit 2 ;;
    esac
fi
exec {RK_BUILD_LOCK_FD}>"${LOCK_FILE}"
if [ -n "${RK_BUILD_TEST_BLOCKED_FILE:-}" ]; then
    case "${RK_BUILD_TEST_BLOCKED_FILE}" in
        /*) ;;
        *) echo "FATAL: RK_BUILD_TEST_BLOCKED_FILE must be absolute." >&2; exit 2 ;;
    esac
    # The controlled contention test requires this invocation to encounter a
    # held lock. A failed nonblocking attempt is an unambiguous proof that it
    # was excluded before falling back to the normal blocking acquisition.
    if flock --nonblock "${RK_BUILD_LOCK_FD}"; then
        echo "FATAL: build-lock test expected contention but acquired immediately." >&2
        exit 1
    fi
    printf '%s\n' "$$" > "${RK_BUILD_TEST_BLOCKED_FILE}"
fi
flock --exclusive "${RK_BUILD_LOCK_FD}"

# Test-only coordination points let check-build-lock.sh prove that the second
# process reached the lock while the first process still held it. They have no
# effect unless the caller supplies explicit absolute paths.
if [ -n "${RK_BUILD_TEST_ACQUIRED_FILE:-}" ]; then
    case "${RK_BUILD_TEST_ACQUIRED_FILE}" in
        /*) printf '%s\n' "$$" > "${RK_BUILD_TEST_ACQUIRED_FILE}" ;;
        *) echo "FATAL: RK_BUILD_TEST_ACQUIRED_FILE must be absolute." >&2; exit 2 ;;
    esac
fi
if [ -n "${RK_BUILD_TEST_RELEASE_FILE:-}" ]; then
    case "${RK_BUILD_TEST_RELEASE_FILE}" in
        /*) ;;
        *) echo "FATAL: RK_BUILD_TEST_RELEASE_FILE must be absolute." >&2; exit 2 ;;
    esac
    for ((attempt = 0; attempt < 600; attempt++)); do
        [ -e "${RK_BUILD_TEST_RELEASE_FILE}" ] && break
        sleep 0.1
    done
    [ -e "${RK_BUILD_TEST_RELEASE_FILE}" ] || {
        echo "FATAL: timed out waiting for the build-lock test release marker." >&2
        exit 1
    }
fi
unset RK_BUILD_TEST_ATTEMPT_FILE RK_BUILD_TEST_BLOCKED_FILE \
    RK_BUILD_TEST_ACQUIRED_FILE RK_BUILD_TEST_RELEASE_FILE

# Prefer an explicitly selected SDK, then the repository owner's user install,
# then PATH. global.json makes the selected SDK version an exact requirement.
if [ -n "${DOTNET_ROOT:-}" ] && [ -x "${DOTNET_ROOT}/dotnet" ]; then
    DOTNET="${DOTNET_ROOT}/dotnet"
elif [ -x "${HOME}/.dotnet/dotnet" ]; then
    DOTNET="${HOME}/.dotnet/dotnet"
else
    DOTNET="$(command -v dotnet || true)"
fi
[ -n "${DOTNET}" ] && [ -x "${DOTNET}" ] || {
    echo "FATAL: dotnet is required; install the SDK pinned by global.json." >&2
    exit 1
}

[ -f "${REPO_ROOT}/jellyfin-refresh-kit.js" ] || {
    echo "FATAL: jellyfin-refresh-kit.js is missing; it is embedded at build time." >&2
    exit 1
}
[ -f "${PROJECT_DIR}/packages.lock.json" ] || {
    echo "FATAL: plugin packages.lock.json is missing; restore inputs must be committed." >&2
    exit 1
}
[ -f "${VERIFIER}" ] || {
    echo "FATAL: scripts/verify-package.py is required before packages can be published." >&2
    exit 1
}

calculate_source_tree() {
    local source_root="${1:-${REPO_ROOT}}"
    python3 - "${source_root}" <<'PY'
import hashlib
import os
import pathlib
import stat
import sys

root = pathlib.Path(sys.argv[1]).resolve()

def checked_mode(path):
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise SystemExit(f"FATAL: package-producing input escapes the checkout: {path}") from error
    current = root
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as error:
            raise SystemExit(f"FATAL: package-producing input is missing: {current}") from error
        if stat.S_ISLNK(mode):
            raise SystemExit(f"FATAL: package-producing input uses a symlink: {current}")
    return path.lstat().st_mode

def require_regular(path):
    mode = checked_mode(path)
    if not stat.S_ISREG(mode):
        raise SystemExit(f"FATAL: package-producing input is not a regular file: {path}")

def read_regular(path):
    require_regular(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SystemExit(f"FATAL: package-producing input changed type: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)

inputs = [
    root / "Directory.Build.props",
    root / "Directory.Build.targets",
    root / "global.json",
    root / "NuGet.Config",
    root / "jellyfin-refresh-kit.js",
    root / "plugin" / "build.sh",
    root / "scripts" / "verify-package.py",
]
project = root / "plugin" / "Jellyfin.Plugin.RefreshKit"
project_mode = checked_mode(project)
if not stat.S_ISDIR(project_mode):
    raise SystemExit(f"FATAL: package project is not a regular directory: {project}")
for path in project.rglob("*"):
    if {"bin", "obj"} & set(path.relative_to(project).parts):
        continue
    mode = checked_mode(path)
    if stat.S_ISDIR(mode):
        continue
    if not stat.S_ISREG(mode):
        raise SystemExit(f"FATAL: package-producing input is not a regular file: {path}")
    inputs.append(path)
digest = hashlib.sha256()
for path in sorted(set(inputs), key=lambda value: value.relative_to(root).as_posix()):
    relative = path.relative_to(root).as_posix().encode("utf-8")
    digest.update(len(relative).to_bytes(4, "big"))
    digest.update(relative)
    content = read_regular(path)
    digest.update(len(content).to_bytes(8, "big"))
    digest.update(content)
print(digest.hexdigest())
PY
}

git_dirty() {
    if [ -n "$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=all)" ]; then
        printf 'true\n'
    else
        printf 'false\n'
    fi
}

# The parent-held lock protects this fixed compiler work directory and prevents
# one build from deleting or replacing files another build is consuming.
mkdir -p "${SNAPSHOT_ROOT}" "${WORK_ROOT}"
if [ -d "${WORK_DIR}" ]; then
    chmod -R u+w "${WORK_DIR}" 2>/dev/null || true
fi
rm -rf -- "${WORK_DIR}"
mkdir -p "${WORK_DIR}"

CANDIDATE_DIR=""
LINK_CANDIDATE=""
cleanup() {
    if [ -n "${LINK_CANDIDATE:-}" ] && { [ -e "${LINK_CANDIDATE}" ] || [ -L "${LINK_CANDIDATE}" ]; }; then
        rm -f -- "${LINK_CANDIDATE}"
    fi
    if [ -n "${CANDIDATE_DIR:-}" ] && [ -d "${CANDIDATE_DIR}" ]; then
        chmod -R u+w "${CANDIDATE_DIR}" 2>/dev/null || true
        rm -rf -- "${CANDIDATE_DIR}"
    fi
    if [ -n "${WORK_DIR:-}" ] && [ -d "${WORK_DIR}" ]; then
        chmod -R u+w "${WORK_DIR}" 2>/dev/null || true
        rm -rf -- "${WORK_DIR}"
    fi
}
trap cleanup EXIT

# A Git checkout has authoritative identity: callers may assert its values but
# cannot override them. A source-only archive is necessarily marked dirty,
# because without Git metadata it cannot prove release cleanliness.
HAS_GIT=false
if command -v git >/dev/null 2>&1 && \
   git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    GIT_TOP="$(git -C "${REPO_ROOT}" rev-parse --show-toplevel)"
    if [ "$(cd "${GIT_TOP}" && pwd -P)" = "$(cd "${REPO_ROOT}" && pwd -P)" ]; then
        HAS_GIT=true
    fi
fi

if [ "${HAS_GIT}" = true ]; then
    SOURCE_REVISION="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
    SOURCE_REVISION="${SOURCE_REVISION,,}"
    if [ -n "${RK_SOURCE_REVISION:-}" ] && [ "${RK_SOURCE_REVISION,,}" != "${SOURCE_REVISION}" ]; then
        echo "FATAL: RK_SOURCE_REVISION does not match the checked-out HEAD ${SOURCE_REVISION}." >&2
        exit 1
    fi

    SOURCE_DIRTY="$(git_dirty)"
    if [ -n "${RK_SOURCE_DIRTY:-}" ] && [ "${RK_SOURCE_DIRTY}" != "${SOURCE_DIRTY}" ]; then
        echo "FATAL: RK_SOURCE_DIRTY=${RK_SOURCE_DIRTY} does not match the actual Git state ${SOURCE_DIRTY}." >&2
        exit 1
    fi

    BUILD_EPOCH="$(git -C "${REPO_ROOT}" show -s --format=%ct HEAD)"
    if [ -n "${SOURCE_DATE_EPOCH:-}" ] && [ "${SOURCE_DATE_EPOCH}" != "${BUILD_EPOCH}" ]; then
        echo "FATAL: SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} does not match the HEAD commit epoch ${BUILD_EPOCH}." >&2
        exit 1
    fi
else
    [ -n "${RK_SOURCE_REVISION:-}" ] && [ -n "${SOURCE_DATE_EPOCH:-}" ] || {
        echo "FATAL: a source-only build requires RK_SOURCE_REVISION and SOURCE_DATE_EPOCH." >&2
        exit 1
    }
    SOURCE_REVISION="${RK_SOURCE_REVISION,,}"
    BUILD_EPOCH="${SOURCE_DATE_EPOCH}"
    if [ -n "${RK_SOURCE_DIRTY:-}" ] && [ "${RK_SOURCE_DIRTY}" != true ]; then
        echo "FATAL: a source-only build cannot assert RK_SOURCE_DIRTY=false without Git evidence." >&2
        exit 1
    fi
    SOURCE_DIRTY=true
fi

[[ "${SOURCE_REVISION}" =~ ^[0-9a-f]{40}$ ]] || {
    echo "FATAL: source revision must be a full 40-character Git commit." >&2
    exit 1
}
case "${SOURCE_DIRTY}" in
    true|false) ;;
    *) echo "FATAL: RK_SOURCE_DIRTY must be true or false." >&2; exit 1 ;;
esac
[[ "${BUILD_EPOCH}" =~ ^[0-9]+$ ]] || {
    echo "FATAL: SOURCE_DATE_EPOCH must be a non-negative integer." >&2
    exit 1
}

SOURCE_TREE_SHA256="$(calculate_source_tree)"
readarray -t PROJECT_IDENTITY < <(python3 - "${PROJECT_FILE}" "${PROJECT_DIR}/Plugin.cs" <<'PY'
import re
import os
import sys
import xml.etree.ElementTree as ET

project, plugin = sys.argv[1:]
def read_text(path):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)

root = ET.fromstring(read_text(project))
version = root.findtext(".//Version")
if not version:
    raise SystemExit(f"FATAL: no <Version> in {project}")
match = re.search(r'new Guid\("([0-9a-fA-F-]+)"\)', read_text(plugin))
if not match:
    raise SystemExit(f"FATAL: no plugin GUID in {plugin}")
print(version)
print(match.group(1))
PY
)
VERSION="${PROJECT_IDENTITY[0]}"
GUID="${PROJECT_IDENTITY[1]}"
[[ "${VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    echo "FATAL: plugin version must contain exactly four numeric parts." >&2
    exit 1
}

TARGETS=(
    "net9.0|10.11.0.0|stage||Lifecycle-correct loaded-state generation, process-epoch rollback convergence, nested-buffer safe degradation, hardened cache/HTML handling, deterministic packages, and expanded compatibility harness coverage."
    "net10.0|12.0.0.0|stage-jf12|_jf12|Lifecycle-correct loaded-state generation, process-epoch rollback convergence, nested-buffer safe degradation, hardened cache/HTML handling, deterministic packages, and expanded Jellyfin 12/third-party harness coverage."
)

TIMESTAMP="$(python3 - "${BUILD_EPOCH}" <<'PY'
import datetime
import sys
print(datetime.datetime.fromtimestamp(int(sys.argv[1]), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
)"

assert_source_unchanged() {
    local current_tree current_head current_dirty current_epoch
    current_tree="$(calculate_source_tree)"
    [ "${current_tree}" = "${SOURCE_TREE_SHA256}" ] || {
        echo "FATAL: package-producing source changed while the build was running." >&2
        return 1
    }
    if [ "${HAS_GIT}" = true ]; then
        current_head="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
        current_dirty="$(git_dirty)"
        current_epoch="$(git -C "${REPO_ROOT}" show -s --format=%ct HEAD)"
        [ "${current_head,,}" = "${SOURCE_REVISION}" ] || {
            echo "FATAL: Git HEAD changed while the build was running." >&2
            return 1
        }
        [ "${current_dirty}" = "${SOURCE_DIRTY}" ] || {
            echo "FATAL: Git cleanliness changed while the build was running." >&2
            return 1
        }
        [ "${current_epoch}" = "${BUILD_EPOCH}" ] || {
            echo "FATAL: Git commit epoch changed while the build was running." >&2
            return 1
        }
    fi
}

# Copy only package-producing inputs, verify that the copy is byte-identical to
# the recorded source digest, and make it read-only before invoking MSBuild.
# The compiler and package verifier therefore cannot observe a transient edit
# to the live checkout after provenance was recorded.
SOURCE_COPY="${WORK_DIR}/source"
python3 - "${REPO_ROOT}" "${SOURCE_COPY}" <<'PY'
import os
import pathlib
import shutil
import stat
import sys

root = pathlib.Path(sys.argv[1]).resolve()
target = pathlib.Path(sys.argv[2]).resolve()

def checked_mode(path):
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise SystemExit(f"FATAL: package-producing input escapes the checkout: {path}") from error
    current = root
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as error:
            raise SystemExit(f"FATAL: package-producing input is missing: {current}") from error
        if stat.S_ISLNK(mode):
            raise SystemExit(f"FATAL: package-producing input uses a symlink: {current}")
    return path.lstat().st_mode

def require_regular(path):
    mode = checked_mode(path)
    if not stat.S_ISREG(mode):
        raise SystemExit(f"FATAL: package-producing input is not a regular file: {path}")

def copy_regular(source, destination):
    require_regular(source)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SystemExit(f"FATAL: package-producing input changed type: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(descriptor, "rb", closefd=False) as input_handle:
            with destination.open("wb") as output_handle:
                shutil.copyfileobj(input_handle, output_handle)
    finally:
        os.close(descriptor)

inputs = [
    root / "Directory.Build.props",
    root / "Directory.Build.targets",
    root / "global.json",
    root / "NuGet.Config",
    root / "jellyfin-refresh-kit.js",
    root / "plugin" / "build.sh",
    root / "scripts" / "verify-package.py",
]
project = root / "plugin" / "Jellyfin.Plugin.RefreshKit"
project_mode = checked_mode(project)
if not stat.S_ISDIR(project_mode):
    raise SystemExit(f"FATAL: package project is not a regular directory: {project}")
for path in project.rglob("*"):
    if {"bin", "obj"} & set(path.relative_to(project).parts):
        continue
    mode = checked_mode(path)
    if stat.S_ISDIR(mode):
        continue
    if not stat.S_ISREG(mode):
        raise SystemExit(f"FATAL: package-producing input is not a regular file: {path}")
    inputs.append(path)
for source in sorted(set(inputs), key=lambda value: value.relative_to(root).as_posix()):
    destination = target / source.relative_to(root)
    copy_regular(source, destination)
PY
COPIED_SOURCE_TREE_SHA256="$(calculate_source_tree "${SOURCE_COPY}")"
[ "${COPIED_SOURCE_TREE_SHA256}" = "${SOURCE_TREE_SHA256}" ] || {
    echo "FATAL: immutable compiler input copy does not match the recorded source tree." >&2
    exit 1
}
find "${SOURCE_COPY}" -type f -exec chmod 0444 {} +
find "${SOURCE_COPY}" -type d -exec chmod 0555 {} +
BUILD_PROJECT_FILE="${SOURCE_COPY}/plugin/Jellyfin.Plugin.RefreshKit/Jellyfin.Plugin.RefreshKit.csproj"
BUILD_VERIFIER="${SOURCE_COPY}/scripts/verify-package.py"

readarray -t COPIED_PROJECT_IDENTITY < <(python3 - "${BUILD_PROJECT_FILE}" \
    "${SOURCE_COPY}/plugin/Jellyfin.Plugin.RefreshKit/Plugin.cs" <<'PY'
import re
import sys
import xml.etree.ElementTree as ET

project, plugin = sys.argv[1:]
root = ET.parse(project).getroot()
version = root.findtext(".//Version")
match = re.search(r'new Guid\("([0-9a-fA-F-]+)"\)',
                  open(plugin, encoding="utf-8").read())
if not version or not match:
    raise SystemExit("FATAL: immutable source copy has no plugin version or GUID")
print(version)
print(match.group(1))
PY
)
[ "${COPIED_PROJECT_IDENTITY[0]}" = "${VERSION}" ] && \
    [ "${COPIED_PROJECT_IDENTITY[1],,}" = "${GUID,,}" ] || {
    echo "FATAL: plugin version or GUID changed while immutable inputs were captured." >&2
    exit 1
}

if [ "${UPDATE_MANIFEST}" = true ]; then
    [ "${HAS_GIT}" = true ] || {
        echo "FATAL: refusing --update-manifest without authoritative Git metadata." >&2
        exit 1
    }
    [ "${SOURCE_DIRTY}" = false ] || {
        echo "FATAL: refusing --update-manifest from a dirty source tree." >&2
        exit 1
    }
fi

PINNED_SDK="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["sdk"]["version"])' \
    "${SOURCE_COPY}/global.json")"
ACTUAL_SDK="$(cd "${SOURCE_COPY}" && "${DOTNET}" --version {RK_BUILD_LOCK_FD}>&-)"
[ "${ACTUAL_SDK}" = "${PINNED_SDK}" ] || {
    echo "FATAL: global.json pins .NET SDK ${PINNED_SDK}, but ${DOTNET} selected ${ACTUAL_SDK}." >&2
    exit 1
}

CANDIDATE_DIR="$(mktemp -d "${SNAPSHOT_ROOT}/.candidate.XXXXXXXXXX")"
BUILD_DIR="${CANDIDATE_DIR}"

echo "==> Building Jellyfin Refresh Kit ${VERSION} (guid ${GUID})"
echo "    source revision : ${SOURCE_REVISION}"
echo "    source tree     : ${SOURCE_TREE_SHA256} (dirty: ${SOURCE_DIRTY})"
echo "    source timestamp: ${TIMESTAMP} (${BUILD_EPOCH})"
echo "    .NET SDK        : ${ACTUAL_SDK}"

COMMON_BUILD_PROPERTIES=(
    "-p:BaseIntermediateOutputPath=${WORK_DIR}/obj/"
    "-p:BaseOutputPath=${WORK_DIR}/bin/"
    "-p:PathMap=${SOURCE_COPY}=/_/%2C${WORK_DIR}=/_build/"
    "-p:ImportDirectoryBuildProps=false"
    "-p:ImportDirectoryBuildTargets=false"
    "-p:ImportDirectoryPackagesProps=false"
    "-p:DiscoverEditorConfigFiles=false"
    "-p:DiscoverGlobalAnalyzerConfigFiles=false"
    "-p:NuGetAudit=false"
)

echo "==> Restoring the locked dependency graph"
(
    cd "${SOURCE_COPY}"
    "${DOTNET}" restore "${BUILD_PROJECT_FILE}" \
        -noAutoResponse \
        --locked-mode \
        --configfile "${SOURCE_COPY}/NuGet.Config" \
        --nologo \
        "${COMMON_BUILD_PROPERTIES[@]}" \
        {RK_BUILD_LOCK_FD}>&-
)

RESULTS=()
for row in "${TARGETS[@]}"; do
    IFS='|' read -r TFM TARGET_ABI STAGE_NAME ZIP_SUFFIX CHANGELOG <<<"${row}"
    STAGE_DIR="${BUILD_DIR}/${STAGE_NAME}"
    PUBLISH_DIR="${WORK_DIR}/publish-${TFM}"
    ZIP_NAME="jellyfin-refresh-kit_${VERSION}${ZIP_SUFFIX}.zip"

    echo
    echo "==> ${TFM} (targetAbi ${TARGET_ABI}) -> ${ZIP_NAME}"
    mkdir -p "${STAGE_DIR}"
    (
        cd "${SOURCE_COPY}"
        "${DOTNET}" publish "${BUILD_PROJECT_FILE}" \
            -noAutoResponse \
            -c Release \
            -f "${TFM}" \
            -o "${PUBLISH_DIR}" \
            --no-restore \
            --nologo \
            "${COMMON_BUILD_PROPERTIES[@]}" \
            -p:RepositoryCommit="${SOURCE_REVISION}" \
            -p:SourceRevisionId="${SOURCE_REVISION}" \
            -p:InformationalVersion="${VERSION}+${SOURCE_REVISION}.${SOURCE_TREE_SHA256}" \
            -p:UseAppHost=false \
            {RK_BUILD_LOCK_FD}>&-
    )

    install -m 0644 "${PUBLISH_DIR}/Jellyfin.Plugin.RefreshKit.dll" "${STAGE_DIR}/"
    install -m 0644 "${PUBLISH_DIR}/Jellyfin.Plugin.RefreshKit.pdb" "${STAGE_DIR}/"

    python3 - "${STAGE_DIR}/meta.json" "${GUID}" "${TARGET_ABI}" "${TFM}" \
        "${VERSION}" "${CHANGELOG}" "${TIMESTAMP}" "${SOURCE_REVISION}" \
        "${SOURCE_TREE_SHA256}" "${SOURCE_DIRTY}" "${BUILD_EPOCH}" "${ACTUAL_SDK}" <<'PY'
import json
import sys

(path, guid, target_abi, framework, version, changelog, timestamp,
 revision, tree_hash, dirty, epoch, sdk_version) = sys.argv[1:]
metadata = {
    "category": "General",
    "guid": guid,
    "name": "Jellyfin Refresh Kit",
    "overview": "Keeps eligible plugin shell assets and open tabs aligned with active server state.",
    "description": "Handles the common stale app-shell path without changes to other plugins: transforms index.html with strong conditional revalidation when it owns the final bytes, safely degrades nested outer-buffer responses to a complete no-store shell, stamps eligible unversioned script and stylesheet tags visible at its transform boundary, and defers open-tab reloads while its documented light-DOM safety probes observe a blocker. Runtime-created, cross-origin and later outer-owned assets remain the owning plugin's responsibility.",
    "owner": "jellyfin-refresh-kit",
    "targetAbi": target_abi,
    "framework": framework,
    "version": version,
    "changelog": changelog,
    "timestamp": timestamp,
    "sourceRevision": revision,
    "sourceTreeSha256": tree_hash,
    "sourceDirty": dirty == "true",
    "sourceDateEpoch": int(epoch),
    "buildSdk": sdk_version,
    "status": "Active",
    "autoUpdate": True,
    "imagePath": "",
}
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(metadata, handle, indent=4, ensure_ascii=False)
    handle.write("\n")
PY

    python3 - "${STAGE_DIR}" "${BUILD_DIR}/${ZIP_NAME}" "${BUILD_EPOCH}" <<'PY'
import os
import pathlib
import sys
import time
import zipfile

stage = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
# ZIP timestamps cannot predate 1980 and have two-second precision.
epoch = max(int(sys.argv[3]), 315532800)
epoch -= epoch % 2
date_time = time.gmtime(epoch)[:6]
# Stored members avoid making package identity depend on the host's zlib
# version while remaining standard ZIP files accepted by Jellyfin.
with zipfile.ZipFile(target, "w") as archive:
    for path in sorted((entry for entry in stage.rglob("*") if entry.is_file()),
                       key=lambda entry: entry.relative_to(stage).as_posix()):
        info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), date_time)
        info.create_system = 3
        info.compress_type = zipfile.ZIP_STORED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_STORED)
for path in [*stage.rglob("*"), stage, target]:
    os.utime(path, (int(sys.argv[3]), int(sys.argv[3])), follow_symlinks=False)
PY

    read -r MD5 SHA256 < <(python3 - "${BUILD_DIR}/${ZIP_NAME}" <<'PY'
import hashlib
import pathlib
import sys
data = pathlib.Path(sys.argv[1]).read_bytes()
print(hashlib.md5(data, usedforsecurity=False).hexdigest(), hashlib.sha256(data).hexdigest())
PY
)
    RESULTS+=("${TARGET_ABI}|${MD5}|${SHA256}|${ZIP_NAME}|${STAGE_NAME}|${TFM}")
done

assert_source_unchanged

MANIFEST_TO_VERIFY="${MANIFEST}"
MANIFEST_CANDIDATE="${WORK_DIR}/manifest.json"
if [ "${UPDATE_MANIFEST}" = true ]; then
    MANIFEST_SOURCE="${WORK_DIR}/manifest.source.json"
    git -C "${REPO_ROOT}" show "${SOURCE_REVISION}:manifest.json" > "${MANIFEST_SOURCE}"
    python3 - "${MANIFEST_SOURCE}" "${MANIFEST_CANDIDATE}" "${GUID}" "${VERSION}" \
        "${TIMESTAMP}" "${RESULTS[@]}" <<'PY'
import json
import sys

source, target, guid, version, timestamp = sys.argv[1:6]
checksums = {}
for row in sys.argv[6:]:
    abi, checksum = row.split("|")[:2]
    checksums[abi] = checksum

with open(source, encoding="utf-8") as handle:
    manifest = json.load(handle)

updated = []
for plugin in manifest:
    if str(plugin.get("guid", "")).lower() != guid.lower():
        continue
    for entry in plugin.get("versions", []):
        if entry.get("version") == version and entry.get("targetAbi") in checksums:
            abi = entry["targetAbi"]
            entry["checksum"] = checksums[abi]
            entry["timestamp"] = timestamp
            updated.append(abi)

missing = sorted(set(checksums) - set(updated))
duplicates = sorted(abi for abi in checksums if updated.count(abi) != 1)
if missing or duplicates:
    raise SystemExit(
        f"manifest.json must contain exactly one {guid} / {version} entry for each targetAbi; "
        f"missing={missing}, non-unique={duplicates}")

for plugin in manifest:
    if str(plugin.get("guid", "")).lower() != guid.lower():
        continue
    same_version = [entry for entry in plugin.get("versions", []) if entry.get("version") == version]
    abis = [tuple(int(part) for part in entry.get("targetAbi", "0").split("."))
            for entry in same_version]
    if abis != sorted(abis, reverse=True):
        raise SystemExit(
            f"manifest.json: version {version} entries must be ordered highest targetAbi first; "
            f"got {[entry.get('targetAbi') for entry in same_version]}")

with open(target, "w", encoding="utf-8", newline="\n") as handle:
    json.dump(manifest, handle, indent=4)
    handle.write("\n")
PY
    MANIFEST_TO_VERIFY="${MANIFEST_CANDIDATE}"
fi

VERIFY_MODE=structure
[ "${UPDATE_MANIFEST}" = false ] || VERIFY_MODE=exact
VERIFY_ARGS=(
    --build-dir "${BUILD_DIR}"
    --manifest "${MANIFEST_TO_VERIFY}"
    --manifest-mode "${VERIFY_MODE}"
    --expected-revision "${SOURCE_REVISION}"
)
python3 "${BUILD_VERIFIER}" "${VERIFY_ARGS[@]}"

assert_source_unchanged

SNAPSHOT_ID="v${VERSION}-${SOURCE_REVISION}-${SOURCE_TREE_SHA256}-${BUILD_EPOCH}-${SOURCE_DIRTY}"
FINAL_SNAPSHOT="${SNAPSHOT_ROOT}/${SNAPSHOT_ID}"
if [ -L "${FINAL_SNAPSHOT}" ]; then
    echo "FATAL: immutable snapshot path must not be a symlink: ${FINAL_SNAPSHOT}" >&2
    exit 1
elif [ -e "${FINAL_SNAPSHOT}" ]; then
    python3 - "${BUILD_DIR}" "${FINAL_SNAPSHOT}" <<'PY'
import hashlib
import pathlib
import stat
import sys

candidate, existing = (pathlib.Path(value) for value in sys.argv[1:])

def inventory(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            result[relative] = ("dir", path.stat().st_mtime_ns)
        elif path.is_file():
            result[relative] = (
                "file", path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()
            )
        else:
            raise SystemExit(f"FATAL: unexpected snapshot entry: {path}")
    return result

if inventory(candidate) != inventory(existing):
    raise SystemExit(f"FATAL: immutable snapshot collision at {existing}")
for path in [existing, *existing.rglob("*")]:
    mode = stat.S_IMODE(path.stat().st_mode)
    expected = 0o555 if path.is_dir() else 0o444
    if mode != expected:
        raise SystemExit(
            f"FATAL: immutable snapshot mode changed: {path} is {oct(mode)}, expected {oct(expected)}")
PY
    rm -rf -- "${BUILD_DIR}"
    CANDIDATE_DIR=""
else
    find "${BUILD_DIR}" -type f -exec chmod 0444 {} +
    find "${BUILD_DIR}" -type d -exec chmod 0555 {} +
    mv -- "${BUILD_DIR}" "${FINAL_SNAPSHOT}"
    CANDIDATE_DIR=""
fi

# Make the finalized target durable before a durable public symlink can name it.
# fsyncing plugin/ below persists a newly created .builds directory as well as
# the later public-link replacement; fsyncing .builds here persists this
# snapshot rename and every file/directory it contains.
python3 - "${FINAL_SNAPSHOT}" "${SNAPSHOT_ROOT}" "${SCRIPT_DIR}" <<'PY'
import os
import pathlib
import sys

snapshot, snapshot_root, plugin_dir = (pathlib.Path(value) for value in sys.argv[1:])

for path in sorted((entry for entry in snapshot.rglob("*") if entry.is_file())):
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

directories = [snapshot, *(entry for entry in snapshot.rglob("*") if entry.is_dir())]
for path in sorted(directories, key=lambda value: len(value.parts), reverse=True):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

for path in (snapshot_root, plugin_dir):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
PY

# Migrate the pre-snapshot build directory once, then atomically replace the
# public symlink. Resolved old targets remain present for existing consumers.
OLD_PUBLIC_TARGET=""
PUBLIC_SWITCHED=false
PUBLIC_ROLLBACK_ALLOWED=false
rollback_public() {
    [ "${PUBLIC_SWITCHED:-false}" = true ] || return 0
    [ "${PUBLIC_ROLLBACK_ALLOWED:-false}" = true ] || return 0
    if [ -n "${OLD_PUBLIC_TARGET:-}" ]; then
        local rollback_link="${SCRIPT_DIR}/.build-rollback.$$.$RANDOM"
        ln -s "${OLD_PUBLIC_TARGET}" "${rollback_link}"
        mv -Tf -- "${rollback_link}" "${PUBLIC_BUILD}"
    else
        rm -f -- "${PUBLIC_BUILD}"
    fi
}
trap 'rollback_public; cleanup' EXIT

if { [ -e "${PUBLIC_BUILD}" ] || [ -L "${PUBLIC_BUILD}" ]; } && [ ! -L "${PUBLIC_BUILD}" ]; then
    LEGACY_SNAPSHOT="${SNAPSHOT_ROOT}/legacy-$(date +%s)-$$"
    mv -- "${PUBLIC_BUILD}" "${LEGACY_SNAPSHOT}"
    OLD_PUBLIC_TARGET=".builds/$(basename "${LEGACY_SNAPSHOT}")"
    PUBLIC_SWITCHED=true
    PUBLIC_ROLLBACK_ALLOWED=true
elif [ -L "${PUBLIC_BUILD}" ]; then
    OLD_PUBLIC_TARGET="$(readlink "${PUBLIC_BUILD}")"
fi
LINK_CANDIDATE="${SCRIPT_DIR}/.build-link.$$.$RANDOM"
ln -s ".builds/${SNAPSHOT_ID}" "${LINK_CANDIDATE}"
PUBLIC_SWITCHED=true
PUBLIC_ROLLBACK_ALLOWED=true
python3 - "${LINK_CANDIDATE}" "${PUBLIC_BUILD}" <<'PY'
import os
import pathlib
import sys
os.replace(sys.argv[1], sys.argv[2])
directory = os.open(pathlib.Path(sys.argv[2]).parent, os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
LINK_CANDIDATE=""
PUBLIC_ROLLBACK_ALLOWED=false

# A manifest that advertises new checksums must never precede the corresponding
# public packages. Once the new public link is durable it is never rolled back:
# a failure before manifest replacement leaves old metadata with new local
# artifacts, while a failure after replacement leaves the matching new pair.
if [ "${UPDATE_MANIFEST}" = true ]; then
    assert_source_unchanged
    python3 - "${MANIFEST_CANDIDATE}" "${MANIFEST}" <<'PY'
import os
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
with source.open("rb") as handle:
    os.fsync(handle.fileno())
os.replace(source, target)
directory = os.open(target.parent, os.O_RDONLY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PY
    for result in "${RESULTS[@]}"; do
        IFS='|' read -r R_ABI R_MD5 _ <<<"${result}"
        echo "==> manifest.json updated: ${VERSION} / abi ${R_ABI} -> ${R_MD5}"
    done
fi

echo
echo "==> Artifacts (version ${VERSION}, timestamp ${TIMESTAMP})"
echo "    immutable snapshot: ${FINAL_SNAPSHOT}"
echo "    public path       : ${PUBLIC_BUILD} -> .builds/${SNAPSHOT_ID}"
for result in "${RESULTS[@]}"; do
    IFS='|' read -r R_ABI R_MD5 R_SHA256 R_ZIP R_STAGE R_TFM <<<"${result}"
    echo "    abi ${R_ABI} (${R_TFM})"
    echo "      zip       : ${PUBLIC_BUILD}/${R_ZIP}"
    echo "      stage dir : ${PUBLIC_BUILD}/${R_STAGE}"
    echo "      md5       : ${R_MD5}"
    echo "      sha256    : ${R_SHA256}"
done
