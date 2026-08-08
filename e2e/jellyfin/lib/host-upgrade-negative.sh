#!/usr/bin/env bash

# Fast, network-free fail-closed checks for the host-upgrade lab.  Deliberately
# does not query Docker: it is safe in default CI and before a release snapshot
# has been built.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAB="$(cd "${HERE}/.." && pwd)"

for script in \
    "${HERE}/host-upgrade.sh" \
    "${HERE}/host-upgrade-negative.sh" \
    "${LAB}/run.sh"; do
    bash -n "${script}"
done
node --check "${HERE}/host-upgrade-browser.cjs"
node "${HERE}/host-upgrade-browser.cjs" --self-test
python3 -c 'compile(open(__import__("sys").argv[1], encoding="utf-8").read(), __import__("sys").argv[1], "exec")' \
    "${HERE}/verify-host-upgrade-results.py"
python3 "${HERE}/verify-host-upgrade-results.py" --self-test

# Source-only tests stop before any Docker invocation: whitelist, offline-mode,
# and timeout validation all precede image inspection/pull by contract.
(
    # shellcheck source=host-upgrade.sh
    source "${HERE}/host-upgrade.sh"
    if ensure_exact_image 'jellyfin/jellyfin:10.11.10' 2>/dev/null; then
        printf 'FATAL: tag-only image escaped the exact whitelist\n' >&2
        exit 1
    fi
    if RK_HOST_UPGRADE_SKIP_PULL=maybe ensure_exact_image "${JF101110_IMAGE}" 2>/dev/null; then
        printf 'FATAL: invalid offline-mode selector was accepted\n' >&2
        exit 1
    fi
    if RK_HOST_UPGRADE_PULL_TIMEOUT_SECONDS=0 ensure_exact_image "${JF101110_IMAGE}" 2>/dev/null; then
        printf 'FATAL: invalid pull timeout was accepted\n' >&2
        exit 1
    fi
)
if RK_HOST_UPGRADE_PORT=80 bash -c 'source "$1"' sh "${HERE}/host-upgrade.sh" 2>/dev/null; then
    printf 'FATAL: unsafe host port was accepted\n' >&2
    exit 1
fi

python3 - \
    "${LAB}/docker-compose.yml" \
    "${HERE}/host-upgrade.sh" \
    "${HERE}/host-upgrade-browser.cjs" \
    "${HERE}/verify-host-upgrade-results.py" <<'PY'
import pathlib
import re
import sys

compose_path, shell_path, browser_path, validator_path = map(pathlib.Path, sys.argv[1:])
compose = compose_path.read_text(encoding="utf-8")
shell = shell_path.read_text(encoding="utf-8")
browser = browser_path.read_text(encoding="utf-8")
validator = validator_path.read_text(encoding="utf-8")

images = {
    "jellyfin/jellyfin:10.11.10@sha256:f66273e014b307e4ac46778845ebc1e9ee24b2e57c1fc17d5ec5ac3015649bfa",
    "jellyfin/jellyfin:10.11.11@sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db",
    "jellyfin/jellyfin:12.0-rc4@sha256:db1df1d111c27ba1f10bb8fce6630892f66eb66b12c2b24e79011453ac18b3db",
}

for image in images:
    if image not in shell or image not in browser:
        raise SystemExit(f"FATAL: exact image is not duplicated in both fail-closed validators: {image}")

required_compose = (
    'host-upgrade:',
    'profiles: ["host-upgrade"]',
    'host-upgrade-config:/config',
    'host-upgrade-cache:/cache',
    '127.0.0.1:${RK_HOST_UPGRADE_PORT:-18118}:8096',
)
for token in required_compose:
    if token not in compose:
        raise SystemExit(f"FATAL: Compose host-upgrade isolation token is missing: {token}")

if '10.11.11@sha256:aefb67e6' not in compose:
    raise SystemExit("FATAL: host-upgrade Compose default is not digest pinned")
if '--pull never' not in shell or 'docker pull "${image}"' not in shell:
    raise SystemExit("FATAL: exact pull preflight/offline execution split is missing")
if 'RepoDigests' not in shell or 'RepoDigests' not in browser:
    raise SystemExit("FATAL: image RepoDigest verification is missing")
if 'RK_HOST_UPGRADE_SKIP_PULL must be exactly 0 or 1' not in shell:
    raise SystemExit("FATAL: explicit offline-mode validation is missing")
if 'result.previous-' not in shell or 'sourceLogRetained' not in browser:
    raise SystemExit("FATAL: retained failure/source evidence preservation is missing")
if 'rk_require curl docker ffmpeg node python3 sha256sum timeout' not in shell:
    raise SystemExit("FATAL: host-upgrade deterministic-media prerequisite is missing")

for unsafe in ('docker volume prune', 'docker system prune', 'docker compose down', 'rm -rf /config'):
    if unsafe in shell or unsafe in browser:
        raise SystemExit(f"FATAL: unsafe broad operation found: {unsafe}")

requirements = (
    'POLL_CLIENT_COUNTS = Object.freeze([10, 50, 100])',
    "'admin-dashboard'",
    "'admin-config-editor'",
    "'admin-plugin-dialog'",
    "'viewer-home'",
    "'viewer-playback'",
    "'anonymous-login'",
    "#/configurationpage?name=Jellyfin%20Refresh%20Kit",
    "blockReason: dialogGated.kit.wouldBlockNow",
    'state?.baselineEpoch === expectedEpoch',
    'generationStatusAfterRestart: 404',
    'responses,',
    "ConcurrentGenerationReadsShareExactlyOneScanPerInvalidation(int readerCount)",
    'exactOneReloadPerDocument',
    'realMediaPlayback: true',
    'MEDIA_REMOTE_DIR = \'/config/rk-host-upgrade-media\'',
    'token: viewerToken',
    'media.currentTime > priorTime',
    'exactOneReloadAfterLeave',
    'mediaPreserved:',
    'assert.deepEqual(upgraded.identity.mounts, beforeIdentity.mounts',
)
for token in requirements:
    if token not in browser:
        raise SystemExit(f"FATAL: browser/result invariant is missing: {token}")

validator_requirements = (
    'data.get("schemaVersion") == 2',
    '"viewer-playback"',
    'validate_media_fixture',
    'validate_video_state',
    'PLAYBACK_GATE_REASONS',
    'playback.get("exactOneReloadAfterLeave") is True',
    'media_after.get("indexedForViewer") is True',
    'upgrade.get("mediaPreserved") is True',
)
for token in validator_requirements:
    if token not in validator:
        raise SystemExit(f"FATAL: retained playback evidence validator is missing: {token}")

if re.search(r'JSON\.stringify\([^\n]*(?:adminToken|viewerToken)', browser):
    raise SystemExit("FATAL: token appears to be serialized into retained JSON")
if 'fs.readFileSync(adminTokenFile' not in browser or 'fs.readFileSync(viewerTokenFile' not in browser:
    raise SystemExit("FATAL: disposable token handoff is missing")

print("host-upgrade static/negative source contract: PASS")
PY

if command -v shellcheck >/dev/null 2>&1; then
    shellcheck \
        "${HERE}/host-upgrade.sh" \
        "${HERE}/host-upgrade-negative.sh"
fi

printf 'host-upgrade no-Docker negative gate: PASS\n'
