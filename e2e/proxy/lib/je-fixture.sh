#!/usr/bin/env bash
# Load the Jellyfin 10 Jellyfin Enhanced fixture from the audited ecosystem
# lock. The proxy rig deliberately has no second version/URL/digest default:
# one tracked lock owns the artifact identity for every compatibility harness.

rk_proxy_load_je_fixture() {
    local repo="${1:?repository root is required}"
    local lock="$repo/e2e/compat/ecosystem.lock.json"
    local -a fields=()

    [ -f "$lock" ] || {
        echo "FATAL: Jellyfin Enhanced ecosystem lock is missing: $lock" >&2
        return 1
    }

    mapfile -t fields < <(python3 - "$lock" <<'PY'
import json
import re
import sys
from pathlib import Path

lock_path = Path(sys.argv[1])
try:
    document = json.loads(lock_path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as error:
    raise SystemExit(f"FATAL: cannot read Jellyfin Enhanced ecosystem lock: {error}")

expected_repository = "https://github.com/n00bcodr/Jellyfin-Enhanced"
audit = document.get("audit")
if not isinstance(audit, dict):
    raise SystemExit("FATAL: ecosystem lock has no audit object")
if audit.get("protectedRepository") != expected_repository:
    raise SystemExit("FATAL: ecosystem lock protected repository changed")
if audit.get("protectedRepositoryAccess") != "strictly-read-only":
    raise SystemExit("FATAL: Jellyfin Enhanced repository is not locked read-only")

artifacts = document.get("artifacts")
if not isinstance(artifacts, list):
    raise SystemExit("FATAL: ecosystem lock has no artifact list")
matches = [item for item in artifacts if isinstance(item, dict) and item.get("id") == "jellyfin-enhanced-jf10"]
if len(matches) != 1:
    raise SystemExit(f"FATAL: expected one jellyfin-enhanced-jf10 lock row, found {len(matches)}")

artifact = matches[0]
archive = artifact.get("archive")
plugin = artifact.get("plugin")
if not isinstance(archive, dict) or not isinstance(plugin, dict):
    raise SystemExit("FATAL: Jellyfin Enhanced lock row is incomplete")

expected = {
    "repository": expected_repository,
    "repositoryAccess": "strictly-read-only",
    "runtime": "jf10",
    "disposition": "testable",
}
for key, value in expected.items():
    if artifact.get(key) != value:
        raise SystemExit(f"FATAL: Jellyfin Enhanced lock field {key} must be {value!r}")

version = plugin.get("version")
guid = plugin.get("guid")
name = plugin.get("name")
target_abi = plugin.get("targetAbi")
framework = plugin.get("framework")
assembly = plugin.get("assembly")
archive_name = archive.get("name")
url = archive.get("url")
sha256 = archive.get("sha256")

if not isinstance(version, str) or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", version) is None:
    raise SystemExit("FATAL: Jellyfin Enhanced lock version is not four-part numeric")
if guid != "f69e946a-4b3c-4e9a-8f0a-8d7c1b2c4d9b":
    raise SystemExit("FATAL: Jellyfin Enhanced GUID changed")
if name != "Jellyfin Enhanced":
    raise SystemExit("FATAL: Jellyfin Enhanced plugin name changed")
if target_abi != "10.11.0.0" or framework != "net9.0":
    raise SystemExit("FATAL: Jellyfin Enhanced jf10 ABI/framework changed")
if assembly != "Jellyfin.Plugin.JellyfinEnhanced.dll":
    raise SystemExit("FATAL: Jellyfin Enhanced main assembly changed")
if archive.get("name") != "Jellyfin.Plugin.JellyfinEnhanced_10.11.0.zip":
    raise SystemExit("FATAL: Jellyfin Enhanced jf10 archive name changed")
expected_url = f"{expected_repository}/releases/download/{version}/{archive_name}"
if url != expected_url:
    raise SystemExit("FATAL: Jellyfin Enhanced archive URL does not match its locked release")
if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
    raise SystemExit("FATAL: Jellyfin Enhanced archive SHA-256 is invalid")
if plugin.get("archiveMeta") != "absent-generate":
    raise SystemExit("FATAL: Jellyfin Enhanced metadata contract changed")

for value in (version, guid, name, target_abi, framework, assembly, archive_name, url, sha256):
    print(value)
PY
    )

    [ "${#fields[@]}" -eq 9 ] || {
        echo "FATAL: could not load the locked Jellyfin Enhanced fixture" >&2
        return 1
    }

    RK_PROXY_JE_VERSION="${fields[0]}"
    RK_PROXY_JE_GUID="${fields[1]}"
    RK_PROXY_JE_NAME="${fields[2]}"
    RK_PROXY_JE_TARGET_ABI="${fields[3]}"
    RK_PROXY_JE_FRAMEWORK="${fields[4]}"
    RK_PROXY_JE_ASSEMBLY="${fields[5]}"
    RK_PROXY_JE_ARCHIVE_NAME="${fields[6]}"
    RK_PROXY_JE_URL="${fields[7]}"
    RK_PROXY_JE_SHA256="${fields[8]}"

    if [ -n "${RK_JE_VERSION:-}" ] && [ "$RK_JE_VERSION" != "$RK_PROXY_JE_VERSION" ]; then
        echo "FATAL: RK_JE_VERSION=$RK_JE_VERSION disagrees with locked Jellyfin Enhanced $RK_PROXY_JE_VERSION" >&2
        return 1
    fi
    export RK_JE_VERSION="$RK_PROXY_JE_VERSION"
}
