#!/usr/bin/env bash

# Network- and Docker-free fail-closed checks for the exact 10.11.0 floor smoke.

set -euo pipefail

NEGATIVE_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAB="$(cd "${NEGATIVE_HERE}/.." && pwd)"
ROOT="$(cd "${LAB}/../.." && pwd)"

for script in \
    "${NEGATIVE_HERE}/abi-floor-smoke.sh" \
    "${NEGATIVE_HERE}/abi-floor-negative.sh" \
    "${NEGATIVE_HERE}/provision.sh" \
    "${NEGATIVE_HERE}/check.sh" \
    "${LAB}/run.sh"; do
    bash -n "${script}"
done
python3 -m py_compile "${ROOT}/scripts/abi_floor_evidence.py"
python3 "${ROOT}/scripts/abi_floor_evidence.py" --self-test

(
    # shellcheck disable=SC1091
    source "${NEGATIVE_HERE}/abi-floor-smoke.sh"
    if validate_runtime_controls 'jellyfin/jellyfin:10.11.0' 2>/dev/null; then
        printf 'FATAL: tag-only image escaped the ABI-floor whitelist\n' >&2
        exit 1
    fi
    if RK_ABI_FLOOR_SKIP_PULL=maybe \
        validate_runtime_controls "${ABI_FLOOR_IMAGE}" 2>/dev/null; then
        printf 'FATAL: invalid ABI-floor offline mode was accepted\n' >&2
        exit 1
    fi
    if RK_ABI_FLOOR_PULL_TIMEOUT_SECONDS=0 \
        validate_runtime_controls "${ABI_FLOOR_IMAGE}" 2>/dev/null; then
        printf 'FATAL: invalid ABI-floor pull timeout was accepted\n' >&2
        exit 1
    fi
)
if RK_ABI_FLOOR_PORT=80 bash -c \
    'source "$1"; validate_runtime_controls "$ABI_FLOOR_IMAGE"' \
    sh "${NEGATIVE_HERE}/abi-floor-smoke.sh" 2>/dev/null; then
    printf 'FATAL: unsafe ABI-floor host port was accepted\n' >&2
    exit 1
fi

python3 - \
    "${LAB}/docker-compose.yml" \
    "${NEGATIVE_HERE}/common.sh" \
    "${NEGATIVE_HERE}/abi-floor-smoke.sh" \
    "${NEGATIVE_HERE}/provision.sh" \
    "${NEGATIVE_HERE}/check.sh" \
    "${LAB}/run.sh" \
    "${ROOT}/scripts/abi_floor_evidence.py" <<'PY'
import pathlib
import sys

compose_path, common_path, runner_path, provision_path, check_path, run_path, validator_path = map(
    pathlib.Path, sys.argv[1:]
)
compose = compose_path.read_text(encoding="utf-8")
common = common_path.read_text(encoding="utf-8")
runner = runner_path.read_text(encoding="utf-8")
provision = provision_path.read_text(encoding="utf-8")
check = check_path.read_text(encoding="utf-8")
run = run_path.read_text(encoding="utf-8")
validator = validator_path.read_text(encoding="utf-8")

image = (
    "jellyfin/jellyfin:10.11.0@sha256:"
    "59417f441213e236a9f907d4e71a13472042409d85f9e9310dbdd87ee33d7bd4"
)
for label, text in (("Compose", compose), ("runner", runner)):
    if image not in text:
        raise SystemExit(f"FATAL: exact ABI-floor image is missing from {label}")
if "10.11.0" not in validator or image.rsplit("sha256:", 1)[1] not in validator:
    raise SystemExit("FATAL: exact ABI-floor image contract is missing from validator")
if "RK_ABI_FLOOR_DIGEST" not in common or image.rsplit("sha256:", 1)[1] not in common:
    raise SystemExit("FATAL: common ABI-floor digest mapping is missing")

compose_tokens = (
    "abi-floor:",
    'profiles: ["abi-floor"]',
    '127.0.0.1:${RK_ABI_FLOOR_PORT:-18119}:8096',
    "abi-floor-config:/config",
    "abi-floor-cache:/cache",
    "abi-floor-internal:",
    "internal: true",
)
for token in compose_tokens:
    if token not in compose:
        raise SystemExit(f"FATAL: isolated ABI-floor Compose token is missing: {token}")

runner_tokens = (
    "ABI_FLOOR_OUT=\"${RK_ARTIFACT_DIR}/abi-floor\"",
    "ABI_FLOOR_TOKEN=\"$(rk_token_file abi-floor)\"",
    "--pull never",
    'docker pull "${image}"',
    "RepoDigests",
    'com.docker.compose.volume=${volume_name}',
    "com.docker.compose.network",
    "abi-floor-config abi-floor-cache",
    "provision.sh\" abi-floor jf10",
    "check.sh\" abi-floor",
    "scripts/abi_floor_evidence.py",
)
for token in runner_tokens:
    if token not in runner:
        raise SystemExit(f"FATAL: ABI-floor runner contract is missing: {token}")
if "host-upgrade-config" in runner or "host-upgrade-cache" in runner:
    raise SystemExit("FATAL: ABI-floor runner references host-upgrade storage")
for unsafe in ("docker volume prune", "docker system prune", "docker compose down", "rm -rf /config"):
    if unsafe in runner:
        raise SystemExit(f"FATAL: unsafe broad operation found in ABI-floor runner: {unsafe}")

if "jf10|jf12|abi-floor" not in provision or '"${TARGET}" = "abi-floor"' not in provision:
    raise SystemExit("FATAL: provisioner does not treat abi-floor as the matching jf10 stage")
if "jf10|jf12|abi-floor" not in check:
    raise SystemExit("FATAL: endpoint/shell checker does not accept abi-floor")
for token in ("cmd_abi_floor", "abi-floor-negative", "--profile abi-floor"):
    if token not in run:
        raise SystemExit(f"FATAL: top-level Jellyfin runner is missing ABI-floor wiring: {token}")

required_evidence = (
    '"result.json"',
    '"server/result.json"',
    '"server/public.json"',
    '"server/generation.json"',
    '"server/diagnostics.json"',
    '"server/plugins.json"',
    '"server.log"',
    '"server/index.html"',
    '"server/conditional.headers"',
    'field(public, "Version") == "10.11.0"',
    'field(diagnostic_match, "IsLoaded") is True',
    '"incompatibleSharedLibraryErrors": []',
)
for token in required_evidence:
    if token not in validator:
        raise SystemExit(f"FATAL: strict ABI-floor evidence invariant is missing: {token}")

print("ABI-floor static/negative source contract: PASS")
PY

if command -v shellcheck >/dev/null 2>&1; then
    shellcheck \
        "${NEGATIVE_HERE}/abi-floor-smoke.sh" \
        "${NEGATIVE_HERE}/abi-floor-negative.sh" \
        "${NEGATIVE_HERE}/provision.sh" \
        "${NEGATIVE_HERE}/check.sh"
fi

printf 'ABI-floor no-Docker negative gate: PASS\n'
