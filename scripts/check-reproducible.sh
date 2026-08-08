#!/usr/bin/env bash
# Build the same current source in two unrelated absolute paths and compare all
# shipped bytes. Both copies retain the same Git metadata so build identity is
# measured rather than overridden, including for a dirty development tree.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

for required in git rsync; do
    command -v "${required}" >/dev/null 2>&1 || {
        echo "FATAL: ${required} is required for isolated reproducibility builds." >&2
        exit 1
    }
done

if [ -n "${DOTNET_ROOT:-}" ] && [ -x "${DOTNET_ROOT}/dotnet" ]; then
    REPRO_DOTNET_ROOT="${DOTNET_ROOT}"
elif [ -x "${HOME}/.dotnet/dotnet" ]; then
    REPRO_DOTNET_ROOT="${HOME}/.dotnet"
else
    REPRO_DOTNET="$(command -v dotnet || true)"
    [ -n "${REPRO_DOTNET}" ] || { echo "FATAL: dotnet is required." >&2; exit 1; }
    REPRO_DOTNET_ROOT="$(python3 -c 'import os,sys; print(os.path.dirname(os.path.realpath(sys.argv[1])))' "${REPRO_DOTNET}")"
fi

SOURCE_EPOCH="$(git show -s --format=%ct HEAD)"

TEMP_ROOT="$(mktemp -d)"
cleanup() {
    if [ "${RK_KEEP_REPRO:-0}" = 1 ]; then
        echo "==> Keeping reproducibility worktrees at ${TEMP_ROOT}" >&2
        return
    fi
    if [ -n "${TEMP_ROOT:-}" ] && [ -d "${TEMP_ROOT}" ]; then
        chmod -R u+w "${TEMP_ROOT}" 2>/dev/null || true
        rm -rf -- "${TEMP_ROOT}"
    fi
}
trap cleanup EXIT

FIRST="${TEMP_ROOT}/short/a"
SECOND="${TEMP_ROOT}/a deliberately different and much longer checkout path/b"

copy_source() {
    local destination="$1"
    mkdir -p "$(dirname "${destination}")"
    git clone --quiet --no-hardlinks --no-checkout "${REPO_ROOT}" "${destination}"
    git -C "${destination}" checkout --quiet --detach "$(git -C "${REPO_ROOT}" rev-parse HEAD)"
    rsync -a --delete \
        --exclude '/.git' \
        --exclude '/node_modules/' \
        --exclude '/plugin/build' \
        --exclude '/plugin/.builds/' \
        --exclude '/plugin/.build-work/' \
        --exclude '/plugin/.build.lock' \
        --exclude '/plugin/.build-link.*' \
        --exclude '/plugin/.build-rollback.*' \
        --exclude 'bin/' \
        --exclude 'obj/' \
        --exclude '__pycache__/' \
        --exclude '*.py[cod]' \
        --exclude '/test-results/' \
        --exclude '/e2e/proxy/.je/' \
        --exclude '/e2e/proxy/.rk-token' \
        --exclude '/e2e/proxy/.state/' \
        --exclude '/e2e/jellyfin/.state/' \
        --exclude '/e2e/jellyfin/artifacts/' \
        --exclude '/e2e/compat/.cache/' \
        --exclude '/e2e/compat/.state/' \
        --exclude '/e2e/compat/artifacts/' \
        "${REPO_ROOT}/" "${destination}/"
}
copy_source "${FIRST}"
copy_source "${SECOND}"

echo "==> Rejecting a package input symlink even when its target bytes are identical"
OUTSIDE_RUNTIME="${TEMP_ROOT}/outside-runtime.js"
SYMLINK_LOG="${TEMP_ROOT}/symlink-boundary.log"
cp -- "${FIRST}/jellyfin-refresh-kit.js" "${OUTSIDE_RUNTIME}"
mv -- "${FIRST}/jellyfin-refresh-kit.js" "${FIRST}/jellyfin-refresh-kit.js.regular"
ln -s "${OUTSIDE_RUNTIME}" "${FIRST}/jellyfin-refresh-kit.js"
set +e
(
    cd "${FIRST}"
    DOTNET_ROOT="${REPRO_DOTNET_ROOT}" ./plugin/build.sh
) >"${SYMLINK_LOG}" 2>&1
SYMLINK_BUILD_RC=$?
(
    cd "${FIRST}"
    python3 scripts/verify-package.py --build-dir plugin/build --manifest manifest.json
) >>"${SYMLINK_LOG}" 2>&1
SYMLINK_VERIFY_RC=$?
set -e
unlink -- "${FIRST}/jellyfin-refresh-kit.js"
mv -- "${FIRST}/jellyfin-refresh-kit.js.regular" "${FIRST}/jellyfin-refresh-kit.js"
if [ "${SYMLINK_BUILD_RC}" -eq 0 ] || [ "${SYMLINK_VERIFY_RC}" -eq 0 ]; then
    echo "FATAL: builder or verifier accepted a package input symlink." >&2
    sed -n '1,180p' "${SYMLINK_LOG}" >&2
    exit 1
fi
if [ "$(grep -Fc 'package-producing input uses a symlink' "${SYMLINK_LOG}")" -lt 2 ]; then
    echo "FATAL: builder and verifier did not both reject the symlink trust boundary." >&2
    sed -n '1,180p' "${SYMLINK_LOG}" >&2
    exit 1
fi
echo "    builder and verifier both rejected the out-of-checkout target"

build_copy() {
    local path="$1" label="$2"
    local cli_home="${TEMP_ROOT}/dotnet-${label}" packages="${TEMP_ROOT}/nuget-${label}"
    mkdir -p "${cli_home}" "${packages}"
    (
        cd "${path}"
        DOTNET_ROOT="${REPRO_DOTNET_ROOT}" \
        DOTNET_CLI_HOME="${cli_home}" \
        NUGET_PACKAGES="${packages}" \
        ./plugin/build.sh
    )
}

echo "==> Reproducibility build A: ${FIRST}"
build_copy "${FIRST}" first
echo "==> Reproducibility build B: ${SECOND}"
build_copy "${SECOND}" second

VERSION="$(python3 - <<'PY'
import xml.etree.ElementTree as ET
print(ET.parse("plugin/Jellyfin.Plugin.RefreshKit/Jellyfin.Plugin.RefreshKit.csproj").getroot().findtext(".//Version"))
PY
)"
ARTIFACTS=(
    "jellyfin-refresh-kit_${VERSION}.zip"
    "jellyfin-refresh-kit_${VERSION}_jf12.zip"
    "stage/Jellyfin.Plugin.RefreshKit.dll"
    "stage/Jellyfin.Plugin.RefreshKit.pdb"
    "stage/meta.json"
    "stage-jf12/Jellyfin.Plugin.RefreshKit.dll"
    "stage-jf12/Jellyfin.Plugin.RefreshKit.pdb"
    "stage-jf12/meta.json"
)

echo "==> Comparing package bytes across checkout paths"
for artifact in "${ARTIFACTS[@]}"; do
    left="${FIRST}/plugin/build/${artifact}"
    right="${SECOND}/plugin/build/${artifact}"
    cmp --silent "${left}" "${right}" || {
        echo "FATAL: path-dependent artifact: ${artifact}" >&2
        exit 1
    }
    digest="$(python3 - "${left}" <<'PY'
import hashlib
import pathlib
import sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
    printf '    %-48s %s\n' "${artifact}" "${digest}"
done

echo "==> Exercising clean release-manifest checksum finalization in the disposable copy"
(
    cd "${SECOND}"
    python3 - manifest.json "${TEMP_ROOT}/decoy-plugin.json" <<'PY'
import copy
import json
import pathlib
import sys

manifest_path = pathlib.Path(sys.argv[1])
record_path = pathlib.Path(sys.argv[2])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
decoy = copy.deepcopy(manifest[0])
decoy["guid"] = "00000000-0000-0000-0000-000000000001"
decoy["name"] = "Manifest finalization decoy"
for entry in decoy["versions"]:
    entry["checksum"] = "a" * 32
    entry["timestamp"] = "2000-01-01T00:00:00Z"
manifest.append(decoy)
manifest_path.write_text(json.dumps(manifest, indent=4) + "\n", encoding="utf-8")
record_path.write_text(json.dumps(decoy, sort_keys=True), encoding="utf-8")
PY
    git add -A
    GIT_AUTHOR_NAME='Refresh Kit reproducibility gate' \
    GIT_AUTHOR_EMAIL='reproducibility@invalid.example' \
    GIT_COMMITTER_NAME='Refresh Kit reproducibility gate' \
    GIT_COMMITTER_EMAIL='reproducibility@invalid.example' \
    GIT_AUTHOR_DATE="@${SOURCE_EPOCH} +0000" \
    GIT_COMMITTER_DATE="@${SOURCE_EPOCH} +0000" \
        git -c core.hooksPath=/dev/null commit --allow-empty --no-gpg-sign \
            -m 'Disposable reproducibility fixture' >/dev/null
    test -z "$(git status --porcelain --untracked-files=all)"
    DOTNET_ROOT="${REPRO_DOTNET_ROOT}" \
    DOTNET_CLI_HOME="${TEMP_ROOT}/dotnet-second" \
    NUGET_PACKAGES="${TEMP_ROOT}/nuget-second" \
    ./plugin/build.sh --update-manifest
    FINALIZED_MANIFEST="${TEMP_ROOT}/finalized-manifest.json"
    cp -- manifest.json "${FINALIZED_MANIFEST}"
    python3 - "${FINALIZED_MANIFEST}" "${TEMP_ROOT}/decoy-plugin.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
matches = [entry for entry in manifest if entry.get("guid") == expected["guid"]]
if matches != [expected]:
    raise SystemExit("FATAL: manifest finalization changed a different plugin's matching version/ABI rows")
print("    unrelated-plugin manifest entries remained field-for-field equivalent")
PY
    SOURCE_REVISION="$(git rev-parse HEAD)"
    git add manifest.json
    GIT_AUTHOR_NAME='Refresh Kit reproducibility gate' \
    GIT_AUTHOR_EMAIL='reproducibility@invalid.example' \
    GIT_COMMITTER_NAME='Refresh Kit reproducibility gate' \
    GIT_COMMITTER_EMAIL='reproducibility@invalid.example' \
    GIT_AUTHOR_DATE="@${SOURCE_EPOCH} +0000" \
    GIT_COMMITTER_DATE="@${SOURCE_EPOCH} +0000" \
        git -c core.hooksPath=/dev/null commit --no-gpg-sign \
            -m 'Disposable finalized manifest child' >/dev/null
    MANIFEST_REVISION="$(git rev-parse HEAD)"
    test "$(git rev-parse "${MANIFEST_REVISION}^")" = "${SOURCE_REVISION}"
    git checkout --quiet --detach "${SOURCE_REVISION}"
    # Release validation checks out the source commit and supplies its direct
    # child's finalized manifest separately, so reproduce that clean state.
    test -z "$(git status --porcelain --untracked-files=all)"
    VERIFICATION_RECEIPT="${TEMP_ROOT}/verification-receipt.json"
    python3 scripts/verify-package.py \
        --build-dir "$(readlink -f plugin/build)" \
        --manifest "${FINALIZED_MANIFEST}" \
        --manifest-mode exact \
        --expected-revision "${SOURCE_REVISION}" \
        --require-clean-source \
        --require-immutable-snapshot \
        --receipt "${VERIFICATION_RECEIPT}"
    RETAINED_CANDIDATE="${TEMP_ROOT}/retained-candidate"
    python3 scripts/retain-release-candidate.py \
        --build-dir "$(readlink -f plugin/build)" \
        --manifest "${FINALIZED_MANIFEST}" \
        --verification-receipt "${VERIFICATION_RECEIPT}" \
        --output "${RETAINED_CANDIDATE}" \
        --source-revision "${SOURCE_REVISION}" \
        --manifest-revision "${MANIFEST_REVISION}" \
        --version "${VERSION}"
    python3 - "${RETAINED_CANDIDATE}" "${VERIFICATION_RECEIPT}" <<'PY'
import hashlib
import json
import pathlib
import sys

candidate, receipt_path = (pathlib.Path(value) for value in sys.argv[1:])
receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
expected = {
    row["name"]: (row["bytes"], row["sha256"])
    for row in receipt["packages"]
}
for name, (size, digest) in expected.items():
    retained = candidate / name
    if retained.stat().st_size != size or hashlib.sha256(retained.read_bytes()).hexdigest() != digest:
        raise SystemExit(f"FATAL: retained candidate no longer matches verification receipt: {name}")
required = {*expected, "manifest.json", "verification-receipt.json", "provenance.json"}
actual = {path.name for path in candidate.iterdir()}
if actual != required:
    raise SystemExit(f"FATAL: retained candidate inventory is {sorted(actual)}, expected {sorted(required)}")
print("    retained packages match the exact verification receipt")
PY
)

echo "==> Reproducibility verification passed"
