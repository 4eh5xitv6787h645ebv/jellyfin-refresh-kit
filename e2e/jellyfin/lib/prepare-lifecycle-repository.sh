#!/usr/bin/env bash

# Build a target-specific, project-local Jellyfin plugin repository from a
# published baseline package and the immutable candidate selected by run.sh.
# The generated catalog and packages live only below e2e/jellyfin/.state.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${HERE}/common.sh"

TARGET="${1:-}"
case "${TARGET}" in jf10|jf12) ;; *) rk_die "usage: prepare-lifecycle-repository.sh jf10|jf12" ;; esac

rk_require curl md5sum python3
rk_pin_build_snapshot

case "${TARGET}" in
    jf10)
        ABI="10.11.0.0"
        DEFAULT_BASE_URL="https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/releases/download/v1.0.0.0/jellyfin-refresh-kit_1.0.0.0.zip"
        DEFAULT_BASE_MD5="57ad873276ad2f998596fead0767af57"
        ;;
    jf12)
        ABI="12.0.0.0"
        DEFAULT_BASE_URL="https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/releases/download/v1.0.0.0/jellyfin-refresh-kit_1.0.0.0_jf12.zip"
        DEFAULT_BASE_MD5="1b7ad43803d1756bcb18c4c0351efcd1"
        ;;
esac

STAGE="$(rk_stage "${TARGET}")"
CANDIDATE_PACKAGE="${RK_BUILD_SNAPSHOT}/jellyfin-refresh-kit_$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${STAGE}/meta.json")"
if [ "${TARGET}" = jf12 ]; then
    CANDIDATE_PACKAGE+="_jf12"
fi
CANDIDATE_PACKAGE+=".zip"
[ -f "${CANDIDATE_PACKAGE}" ] || rk_die "candidate package is missing: ${CANDIDATE_PACKAGE}"

readarray -t CANDIDATE_META < <(python3 - "${STAGE}/meta.json" "${ABI}" <<'PY'
import json
import re
import sys

path, expected_abi = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    data = json.load(handle)
version = str(data.get("version", ""))
abi = str(data.get("targetAbi", ""))
if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", version):
    raise SystemExit(f"FATAL: invalid candidate version in {path}: {version!r}")
if abi != expected_abi:
    raise SystemExit(f"FATAL: {path} targets {abi}, expected {expected_abi}")
print(version)
print(str(data.get("sourceRevision", "")))
print(str(data.get("sourceTreeSha256", "")))
PY
)
EMBEDDED_CANDIDATE_VERSION="${CANDIDATE_META[0]}"
CANDIDATE_VERSION="${EMBEDDED_CANDIDATE_VERSION}"
BASE_VERSION="1.0.0.0"

python3 - "${BASE_VERSION}" "${CANDIDATE_VERSION}" "${EMBEDDED_CANDIDATE_VERSION}" <<'PY'
import re
import sys

baseline, candidate, embedded = sys.argv[1:]
pattern = re.compile(r"[0-9]+(?:\.[0-9]+){3}")
if not pattern.fullmatch(baseline) or not pattern.fullmatch(candidate):
    raise SystemExit("FATAL: lifecycle versions must contain exactly four numeric components")
parts = lambda value: tuple(int(item) for item in value.split("."))
if parts(candidate) <= parts(baseline):
    raise SystemExit(
        f"FATAL: lifecycle candidate {candidate} must be newer than baseline {baseline}; "
        "the release-candidate run must use genuinely distinct package versions"
    )
PY

BASE_URL="${DEFAULT_BASE_URL}"
BASE_MD5="${DEFAULT_BASE_MD5}"
[[ "${BASE_URL}" =~ ^https:// ]] || rk_die "baseline package URL must use HTTPS"
[[ "${BASE_MD5}" =~ ^[0-9a-fA-F]{32}$ ]] || rk_die "baseline checksum is not an MD5 digest"
BASE_MD5="${BASE_MD5,,}"

REPOSITORY_ROOT="${RK_STATE_DIR}/repository"
TARGET_DIR="${REPOSITORY_ROOT}/${TARGET}"
mkdir -p "${TARGET_DIR}"
BASE_PACKAGE="${TARGET_DIR}/baseline.zip"
CANDIDATE_COPY="${TARGET_DIR}/candidate.zip"

if [ ! -f "${BASE_PACKAGE}" ] || \
   [ "$(md5sum "${BASE_PACKAGE}" 2>/dev/null | awk '{print $1}')" != "${BASE_MD5}" ]; then
    rm -f -- "${BASE_PACKAGE}.part"
    rk_log "${TARGET}: downloading published lifecycle baseline ${BASE_VERSION}"
    curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 \
        --output "${BASE_PACKAGE}.part" "${BASE_URL}"
    ACTUAL_BASE_MD5="$(md5sum "${BASE_PACKAGE}.part" | awk '{print $1}')"
    [ "${ACTUAL_BASE_MD5}" = "${BASE_MD5}" ] || {
        rm -f -- "${BASE_PACKAGE}.part"
        rk_die "${TARGET}: published baseline checksum ${ACTUAL_BASE_MD5} != ${BASE_MD5}"
    }
    mv -f -- "${BASE_PACKAGE}.part" "${BASE_PACKAGE}"
fi

cp -- "${CANDIDATE_PACKAGE}" "${CANDIDATE_COPY}.part"
mv -f -- "${CANDIDATE_COPY}.part" "${CANDIDATE_COPY}"
CANDIDATE_MD5="$(md5sum "${CANDIDATE_COPY}" | awk '{print $1}')"
REPOSITORY_URL="http://repository:8080/${TARGET}/manifest.json"

python3 - "${TARGET_DIR}/manifest-update.json.part" "${TARGET}" "${ABI}" \
    "${BASE_VERSION}" "${BASE_MD5}" "${CANDIDATE_VERSION}" "${CANDIDATE_MD5}" \
    "${RK_PLUGIN_GUID}" <<'PY'
import json
import sys

(path, target, abi, baseline, baseline_md5, candidate, candidate_md5, guid) = sys.argv[1:]
base = f"http://repository:8080/{target}"
candidate_entry = {
    "version": candidate,
    "changelog": "Candidate under lifecycle validation.",
    "targetAbi": abi,
    "sourceUrl": f"{base}/candidate.zip",
    "checksum": candidate_md5,
    "timestamp": "2026-08-09T00:00:00Z",
}
baseline_entry = {
    "version": baseline,
    "changelog": "Published lifecycle baseline.",
    "targetAbi": abi,
    "sourceUrl": f"{base}/baseline.zip",
    "checksum": baseline_md5,
    "timestamp": "2026-08-08T00:00:00Z",
}
plugin = {
    "guid": guid,
    "name": "Jellyfin Refresh Kit",
    "overview": "Disposable Refresh Kit lifecycle fixture.",
    "description": "Project-local packages used only by the Refresh Kit lifecycle lab.",
    "owner": "jellyfin-refresh-kit lifecycle lab",
    "category": "General",
    "imageUrl": "",
}
for output, versions in (
    (path, [candidate_entry, baseline_entry]),
    (path.replace("manifest-update.json.part", "manifest-baseline.json.part"), [baseline_entry]),
):
    plugin["versions"] = versions
    with open(output, "w", encoding="utf-8", newline="\n") as handle:
        json.dump([plugin], handle, indent=2, sort_keys=True)
        handle.write("\n")
PY
mv -f -- "${TARGET_DIR}/manifest-update.json.part" "${TARGET_DIR}/manifest-update.json"
mv -f -- "${TARGET_DIR}/manifest-baseline.json.part" "${TARGET_DIR}/manifest-baseline.json"
cp -- "${TARGET_DIR}/manifest-baseline.json" "${TARGET_DIR}/manifest.json.part"
mv -f -- "${TARGET_DIR}/manifest.json.part" "${TARGET_DIR}/manifest.json"
printf 'ok\n' > "${REPOSITORY_ROOT}/health.txt"

python3 - "${RK_STATE_DIR}/${TARGET}.lifecycle.json.part" "${TARGET}" \
    "${REPOSITORY_URL}" "${BASE_VERSION}" "${BASE_MD5}" "${BASE_URL}" \
    "${CANDIDATE_VERSION}" "${EMBEDDED_CANDIDATE_VERSION}" "${CANDIDATE_MD5}" \
    "${CANDIDATE_META[1]}" "${CANDIDATE_META[2]}" <<'PY'
import json
import sys

(path, target, repository_url, baseline, baseline_md5, baseline_url,
 candidate, embedded, candidate_md5, revision, tree) = sys.argv[1:]
with open(path, "w", encoding="utf-8", newline="\n") as handle:
    json.dump({
        "target": target,
        "repositoryUrl": repository_url,
        "baselineVersion": baseline,
        "baselineMd5": baseline_md5,
        "baselineSourceUrl": baseline_url,
        "candidateVersion": candidate,
        "embeddedCandidateVersion": embedded,
        "candidateMd5": candidate_md5,
        "candidateSourceRevision": revision,
        "candidateSourceTreeSha256": tree,
    }, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY
mv -f -- "${RK_STATE_DIR}/${TARGET}.lifecycle.json.part" "${RK_STATE_DIR}/${TARGET}.lifecycle.json"

rk_log "${TARGET}: lifecycle repository ready (${BASE_VERSION} -> ${CANDIDATE_VERSION}, candidate md5 ${CANDIDATE_MD5})"
