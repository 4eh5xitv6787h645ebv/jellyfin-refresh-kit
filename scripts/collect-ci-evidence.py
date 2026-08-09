#!/usr/bin/env python3
"""Collect a small, secret-safe evidence bundle from the heavy test rigs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
from typing import Any

from host_upgrade_evidence import (
    HostUpgradeEvidenceError,
    validate_evidence as validate_host_upgrade_evidence,
)
from evidence_validation import (
    EvidenceValidationError,
    INTEGRATION_LOGS,
    INTEGRATION_TEXT,
    artifact_verification_summary,
    load_compatibility_artifact_lock,
    reconstruct_compatibility_cache_evidence,
    validate_artifact_verification_report,
    validate_compatibility_tree,
    validate_integration_tree,
)


ROOT = pathlib.Path(__file__).resolve().parent.parent
LAB_ARTIFACTS = ROOT / "e2e" / "jellyfin" / "artifacts"
COMPAT_ARTIFACTS = ROOT / "e2e" / "compat" / "artifacts"
COMPAT_MATRICES = ROOT / "e2e" / "compat" / "matrices.json"
COMPAT_LOCK = ROOT / "e2e" / "compat" / "ecosystem.lock.json"
SAFE_JSON = (
    "jf10/server/result.json",
    "jf10/server/diagnostics.json",
    "jf10/browser/result.json",
    "jf10/lifecycle/result.json",
    "jf10/third-party-lifecycle/result.json",
    "jf12/server/result.json",
    "jf12/server/diagnostics.json",
    "jf12/browser/result.json",
    "jf12/lifecycle/result.json",
    "jf12/third-party-lifecycle/result.json",
    "compat-jf10-on-jf12/result.json",
    "compat-jf10-on-jf12/generation.json",
    "compat-jf10-on-jf12/plugin-record.json",
    "compat-jf10-on-jf12/public.json",
    "abi-floor/result.json",
    "abi-floor/relay.json",
    "abi-floor/server/result.json",
    "abi-floor/server/diagnostics.json",
    "abi-floor/server/plugins.json",
    "host-upgrade/result.json",
    "host-upgrade/jf10/result.json",
    "host-upgrade/jf12/result.json",
)
EXACT_ANONYMOUS_HTTP = (
    "abi-floor/server/public.json",
    "abi-floor/server/generation.json",
    "abi-floor/server/generation.headers",
    "abi-floor/server/kit.headers",
    "abi-floor/server/kit.js",
    "abi-floor/server/shell.headers",
    "abi-floor/server/conditional.headers",
    "abi-floor/server/conditional.body",
    "abi-floor/server/index.html",
)
SAFE_TEXT = tuple(path for path in INTEGRATION_TEXT if path not in EXACT_ANONYMOUS_HTTP)
INTEGRATION_LIFECYCLE_RESULTS = {
    "jf10/lifecycle/result.json": ("jf10", "self"),
    "jf10/third-party-lifecycle/result.json": ("jf10", "third-party"),
    "jf12/lifecycle/result.json": ("jf12", "self"),
    "jf12/third-party-lifecycle/result.json": ("jf12", "third-party"),
}
SELF_LIFECYCLE_PHASES = {
    "playback-fixture-indexed",
    "pristine-authenticated-tabs",
    "install-baseline-active",
    "update-candidate-converged",
    "disable-clean-shell",
    "enable-active",
    "uninstall-clean-shell",
    "reinstall-active",
    "plain-restart-in-place",
    "playback-pause-safety-gate",
    "ui-logout-and-relogin",
}
THIRD_PARTY_LIFECYCLE_PHASES = {
    "active-refresh-kit-tabs",
    "install-v1-converged",
    "update-v2-converged",
    "no-change-v2-restart-in-place",
    "disable-v2-converged",
    "enable-v2-converged",
    "uninstall-converged",
}
SAFE_IMAGES = tuple(
    f"{target}/browser/{name}"
    for target in ("jf10", "jf12")
    for name in (
        "dashboard.png",
        "configuration.png",
        "post-restart-primary.png",
        "post-restart-secondary.png",
        "post-restart-background.png",
    )
)
SENSITIVE_KEY = re.compile(
    r"^(?:token|access.?token|refresh.?token|id.?token|x.?emby.?token|api.?key|"
    r"authorization|password|passwd|pw|secret|credential|cookie|set.?cookie|"
    r"session|session.?id)$",
    re.I,
)
QUERY_SECRET = re.compile(
    r"([?&](?:token|access.?token|refresh.?token|id.?token|x.?emby.?token|"
    r"api.?key|authorization|password|cookie|session(?:.?id)?)=)[^&\s\"']+",
    re.I,
)
JSON_SECRET = re.compile(
    r'((?:\"?)(?:Token|AccessToken|RefreshToken|IdToken|X-Emby-Token|ApiKey|'
    r'Authorization|Password|Pw|Secret|Cookie|Set-Cookie|Session|SessionId)'
    r'\"?\s*[:=]\s*\")[^\"]*(\")',
    re.I,
)
HEADER_SECRET = re.compile(r'(\bToken\s*=\s*\")[^\"]+(\")', re.I)
AUTHORIZATION_SECRET = re.compile(r"(\bAuthorization\s*:\s*)[^\r\n]+", re.I)
EMBY_SECRET = re.compile(r"(\bX-Emby-Token\s*:\s*)[^\s\"']+", re.I)
COOKIE_SECRET = re.compile(r"(\b(?:Set-)?Cookie\s*:\s*)[^\r\n]+", re.I)
AUTHORIZATION_ASSIGNMENT_SECRET = re.compile(
    r"(\bAuthorization\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^\r\n]+)", re.I
)
COOKIE_ASSIGNMENT_SECRET = re.compile(
    r"(\b(?:Set-)?Cookie\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^\r\n]+)", re.I
)
ASSIGNMENT_SECRET = re.compile(
    r"(\b(?:token|access.?token|refresh.?token|id.?token|x.?emby.?token|api.?key|"
    r"password|passwd|secret|credential|session.?id)\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
    re.I,
)
LOG_TOKEN = re.compile(r"(\baccess token(?:\s+for\s+user)?\s+)[0-9a-f]{20,}", re.I)
FORBIDDEN_CREDENTIAL = re.compile(
    r'\"?(?:Token|AccessToken|RefreshToken|IdToken|X-Emby-Token|ApiKey|Authorization|'
    r'Password|Pw|Secret|Cookie|Set-Cookie|Session|SessionId)\"?\s*[:=]\s*'
    r'\"(?!<redacted>)[^\"]+\"'
    r'|\bToken\s*=\s*\"(?!<redacted>)[^\"]+\"'
    r'|\bAuthorization\s*:\s*(?!<redacted>)[^\r\n]+'
    r'|\bX-Emby-Token\s*:\s*(?!<redacted>)[^\s\"\x27]+'
    r'|\b(?:Set-)?Cookie\s*:\s*(?!<redacted>)[^\r\n]+',
    re.I,
)
COMPATIBILITY_LITERAL_SECRET = re.compile(
    r"(?:[?&](?:token|access.?token|refresh.?token|id.?token|x.?emby.?token|"
    r"api.?key|authorization|password|cookie|session(?:.?id)?)=|"
    r"[\"']?(?:token|access.?token|refresh.?token|id.?token|x.?emby.?token|"
    r"api.?key|authorization|password|secret|cookie|session(?:.?id)?)"
    r"[\"']?\s*[:=]\s*[\"']|\bMediaBrowser\s+Token\s*=\s*[\"']?)"
    r"[A-Za-z0-9._~+/=-]{16,}",
    re.I,
)
BINARY_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".zip"}
COMPAT_MATRIX_FILES = (
    "static.json",
    "stage.json",
    "network.json",
    "artifact-verification.json",
    "result.json",
)
COMPAT_WEBROOT_FILES = ("webroot-before.html", "webroot-after.html")
COMPAT_RAW_HTTP_FILES = (
    "shell-after.html",
    "shell-after.headers",
    "conditional.html",
    "conditional.headers",
    "conditional-status.txt",
)
SAFE_DEGRADE_CHECK_NAMES = {
    "primaryStatus200",
    "primaryFramingValid",
    "primaryCacheControlNoStore",
    "primaryEtagAbsent",
    "primaryLastModifiedAbsent",
    "conditionalStatus200",
    "conditionalFramingValid",
    "conditionalCacheControlNoStore",
    "conditionalEtagAbsent",
    "conditionalLastModifiedAbsent",
    "conditionalBodyNonEmpty",
    "conditionalHtmlDocument",
    "conditionalSingleRefreshKitTag",
    "conditionalNamedRuntime",
    "conditionalBootGeneration",
    "conditionalGenerationAddressedKit",
    "conditionalAssetMultisetMatchesPrimary",
}
REQUIRED_CACHE_CHECK_NAMES = {
    "primaryStatus200",
    "validFraming",
    "singleStrongEtag",
    "etagMatchesBodySha256",
    "lastModifiedAbsent",
    "conditionalStatus304",
    "conditionalBodyEmpty",
    "conditionalEtagMatches",
    "conditionalFramingBodyless",
    "conditionalLastModifiedAbsent",
}
FIXTURE_SECRET_DEFAULTS = {
    "RK_LAB_PASSWORD": "Test669Pw!x",
    "RK_PASS": "Test669Pw!x",
    "RK_COMPAT_PASSWORD": "Compat669Pw!x",
    "RK_LAB_VIEWER_PASSWORD": "Viewer669Pw!x",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, default=pathlib.Path("test-results"))
    parser.add_argument("--jellyfin-exit", type=int)
    parser.add_argument("--abi-floor-exit", type=int)
    parser.add_argument("--host-upgrade-exit", type=int)
    parser.add_argument("--proxy-exit", type=int)
    parser.add_argument("--compatibility-exit", type=int)
    parser.add_argument("--build-dir", type=pathlib.Path)
    parser.add_argument("--require-integration-evidence", action="store_true")
    parser.add_argument("--require-compatibility-evidence", action="store_true")
    parser.add_argument(
        "--log",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="sanitize a captured text log into the evidence bundle",
    )
    return parser.parse_args()


def run(*arguments: str) -> str | None:
    try:
        result = subprocess.run(
            arguments,
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=20,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip()


def known_fixture_secrets() -> tuple[str, ...]:
    """Return every effective lab password without recording its variable name."""
    values = {
        os.environ.get(name) or default
        for name, default in FIXTURE_SECRET_DEFAULTS.items()
    }
    return tuple(sorted(values, key=len, reverse=True))


def sanitize_text(value: str) -> str:
    value = QUERY_SECRET.sub(r"\1<redacted>", value)
    value = JSON_SECRET.sub(r"\1<redacted>\2", value)
    value = HEADER_SECRET.sub(r"\1<redacted>\2", value)
    value = AUTHORIZATION_SECRET.sub(r"\1<redacted>", value)
    value = EMBY_SECRET.sub(r"\1<redacted>", value)
    value = COOKIE_SECRET.sub(r"\1<redacted>", value)
    value = AUTHORIZATION_ASSIGNMENT_SECRET.sub(r"\1<redacted>", value)
    value = COOKIE_ASSIGNMENT_SECRET.sub(r"\1<redacted>", value)
    value = ASSIGNMENT_SECRET.sub(r"\1<redacted>", value)
    value = LOG_TOKEN.sub(r"\1<redacted>", value)
    # The lab credentials are disposable, but keeping both default and
    # caller-overridden values out of retained output makes the evidence
    # policy robust if fixture scope changes.
    for secret in known_fixture_secrets():
        value = value.replace(secret, "<redacted>")
    return value


def sanitize_json(value: Any, parents: tuple[str, ...] = ()) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            name = str(key)
            if SENSITIVE_KEY.fullmatch(name) or (
                name.lower() == "value" and any(parent.lower() == "passwords" for parent in parents)
            ):
                result[name] = "<redacted>"
            else:
                result[name] = sanitize_json(child, (*parents, name))
        return result
    if isinstance(value, list):
        return [sanitize_json(child, parents) for child in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def contains_forbidden_secret(value: str) -> bool:
    return (
        FORBIDDEN_CREDENTIAL.search(value) is not None
        or any(
            "<redacted>" not in match.group(0)
            for pattern in (
                ASSIGNMENT_SECRET,
                AUTHORIZATION_ASSIGNMENT_SECRET,
                COOKIE_ASSIGNMENT_SECRET,
            )
            for match in pattern.finditer(value)
        )
        or any(secret in value for secret in known_fixture_secrets())
    )


def copy_exact_anonymous_http(
    source: pathlib.Path,
    target: pathlib.Path,
    relative: str,
) -> None:
    """Retain anonymous HTTP proof byte-for-byte or refuse it as unsafe.

    Rewriting a header/body would invalidate Content-Length, the body-derived
    ETag, or the evidence checksum.  These paths therefore take a stricter
    copy-only path: invalid UTF-8, known fixture credentials, credential-shaped
    wire values, and a served runtime that differs from its repository source
    all abort collection instead of being redacted.
    """
    try:
        raw = source.read_bytes()
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"unsafe exact anonymous HTTP evidence {relative}: {error}") from error
    empty_body_proof = relative == "abi-floor/server/conditional.body"
    if not raw and not empty_body_proof:
        raise ValueError(f"empty exact anonymous HTTP evidence: {relative}")
    forbidden_patterns = (
        QUERY_SECRET,
        JSON_SECRET,
        HEADER_SECRET,
        AUTHORIZATION_SECRET,
        EMBY_SECRET,
        COOKIE_SECRET,
        LOG_TOKEN,
    )
    wire_secret = any(pattern.search(text) is not None for pattern in forbidden_patterns) \
        or any(secret in text for secret in known_fixture_secrets())
    if wire_secret:
        raise ValueError(f"credential-shaped value in exact anonymous HTTP evidence: {relative}")
    if relative.endswith("/kit.js"):
        expected = (ROOT / "jellyfin-refresh-kit.js").read_bytes()
        if raw != expected:
            raise ValueError("served ABI-floor kit.js differs from repository runtime bytes")
    elif contains_forbidden_secret(text) or sanitize_text(text) != text:
        raise ValueError(f"redaction would alter exact anonymous HTTP evidence: {relative}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def copy_exact_compatibility_webroot(
    source: pathlib.Path,
    target: pathlib.Path,
    relative: str,
) -> bytes:
    """Inspect and retain one direct-webroot HTML capture without rewriting it."""
    try:
        raw = source.read_bytes()
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(
            f"unsafe exact compatibility webroot evidence {relative}: {error}"
        ) from error
    if not raw:
        raise ValueError(f"empty exact compatibility webroot evidence: {relative}")
    if contains_forbidden_secret(text) or sanitize_text(text) != text:
        raise ValueError(
            f"credential-shaped value in exact compatibility webroot evidence: {relative}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write the bytes that were inspected, rather than reopening the source.
    target.write_bytes(raw)
    return raw


def copy_exact_compatibility_http(
    source: pathlib.Path,
    target: pathlib.Path,
    relative: str,
) -> bytes:
    """Inspect and retain cache-proof HTTP bytes without rewriting them."""
    try:
        raw = source.read_bytes()
        text = raw.decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(
            f"unsafe exact compatibility HTTP evidence {relative}: {error}"
        ) from error
    if not raw and pathlib.PurePosixPath(relative).name != "conditional.html":
        raise ValueError(f"empty exact compatibility HTTP evidence: {relative}")
    name = pathlib.PurePosixPath(relative).name
    header_secret = name.endswith(".headers") and any(
        pattern.search(text) is not None
        for pattern in (
            AUTHORIZATION_SECRET,
            EMBY_SECRET,
            COOKIE_SECRET,
        )
    )
    if (
        header_secret
        or COMPATIBILITY_LITERAL_SECRET.search(text) is not None
        or any(secret in text for secret in known_fixture_secrets())
    ):
        raise ValueError(
            f"credential-shaped value in exact compatibility HTTP evidence: {relative}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return raw


def exact_json_value(actual: Any, expected: Any) -> bool:
    """Compare JSON values without accepting True/False as integer counts."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        return (
            set(actual) == set(expected)
            and all(exact_json_value(actual[key], value) for key, value in expected.items())
        )
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(
            exact_json_value(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def reconstruct_webroot_disk(
    before: bytes,
    after: bytes,
    requirements: dict[str, Any],
) -> dict[str, Any]:
    """Reconstruct the analyzer's complete disk proof from the copied raw bytes."""
    try:
        before_text = before.decode("utf-8-sig", errors="strict")
        after_text = after.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError(f"compatibility webroot evidence is not strict UTF-8: {error}") from error
    effects: dict[str, Any] = {}
    for artifact_id, requirement in requirements.items():
        expected_after = requirement["cardinality"]
        markers = {
            marker: {
                "beforeCount": before_text.count(marker),
                "afterCount": after_text.count(marker),
                "expectedBeforeCount": 0,
                "expectedAfterCount": expected_after,
            }
            for marker in requirement["markers"]
        }
        checks = {
            "rawMarkersAbsentBefore": all(
                row["beforeCount"] == row["expectedBeforeCount"]
                for row in markers.values()
            ),
            "rawMarkersExactAfter": all(
                row["afterCount"] == row["expectedAfterCount"]
                for row in markers.values()
            ),
        }
        effects[artifact_id] = {
            "mode": requirement["mode"],
            "markers": markers,
            "checks": checks,
            "allPassed": all(checks.values()),
        }
    expects_writes = any(
        requirement["mode"] == "added" for requirement in requirements.values()
    )
    document_checks = {
        "beforeHtmlDocument": b"<html" in before.lower() and b"</html>" in before.lower(),
        "afterHtmlDocument": b"<html" in after.lower() and b"</html>" in after.lower(),
        "diskBytesChangedAsExpected": (before != after) == expects_writes,
    }
    return {
        "beforeBytes": len(before),
        "afterBytes": len(after),
        "beforeSha256": hashlib.sha256(before).hexdigest(),
        "afterSha256": hashlib.sha256(after).hexdigest(),
        "effects": effects,
        "checks": document_checks,
        "allPassed": all(document_checks.values())
        and all(row["allPassed"] for row in effects.values()),
    }


def file_hash(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_md5(path: pathlib.Path) -> str:
    digest = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def resolve_build_path(selected: pathlib.Path | None) -> pathlib.Path:
    public = selected if selected is not None else ROOT / "plugin" / "build"
    if not public.is_absolute():
        public = ROOT / public
    return public.resolve()


def collect_package_identity(selected: pathlib.Path | None) -> dict[str, Any]:
    public = resolve_build_path(selected)
    if not public.exists():
        return {"available": False}
    resolved = public
    snapshot_root = (ROOT / "plugin" / ".builds").resolve()
    try:
        snapshot_name = resolved.relative_to(snapshot_root).as_posix()
    except ValueError:
        snapshot_name = None

    packages = []
    for archive in sorted(resolved.glob("*.zip")):
        packages.append({
            "name": archive.name,
            "bytes": archive.stat().st_size,
            "sha256": file_hash(archive),
        })
    metadata = {}
    for name in ("stage", "stage-jf12"):
        path = resolved / name / "meta.json"
        if path.is_file():
            metadata[name] = sanitize_json(json.loads(path.read_text(encoding="utf-8")))
    return {
        "available": True,
        "immutableSnapshot": snapshot_name,
        "packages": packages,
        "metadata": metadata,
    }


def validate_integration_lifecycle(
    relative: str,
    value: Any,
    build: pathlib.Path | None,
    strict_errors: list[str],
) -> None:
    """Bind retained lifecycle success to the selected immutable package."""
    expected_target, kind = INTEGRATION_LIFECYCLE_RESULTS[relative]

    def fail(message: str) -> None:
        strict_errors.append(f"invalid integration lifecycle evidence {relative}: {message}")

    if not isinstance(value, dict):
        fail("result is not an object")
        return
    if value.get("target") != expected_target:
        fail(f"target is {value.get('target')!r}, expected {expected_target!r}")
    if value.get("completed") is not True:
        fail("completed is not true")
    for field in ("failures", "unexpectedRefreshKitBrowserErrors"):
        if value.get(field) != []:
            fail(f"{field} is not an empty array")
    if value.get("repositoryConfigurationRemoved") is not True:
        fail("repository cleanup was not confirmed")
    if kind == "third-party" and value.get("versionFlapWarnings") != []:
        fail("versionFlapWarnings is not an empty array")

    phases = value.get("phases")
    if not isinstance(phases, list):
        fail("phases is not an array")
    else:
        names = [row.get("name") for row in phases if isinstance(row, dict)]
        required = (
            SELF_LIFECYCLE_PHASES if kind == "self" else THIRD_PARTY_LIFECYCLE_PHASES
        )
        missing = sorted(name for name in required if names.count(name) != 1)
        if missing:
            fail(f"required phases are missing or repeated: {missing}")

    metadata = value.get("metadata")
    if not isinstance(metadata, dict):
        fail("metadata is not an object")
        return
    if metadata.get("target") != expected_target:
        fail(f"metadata target is {metadata.get('target')!r}, expected {expected_target!r}")
    if build is None:
        fail("no pinned build directory was supplied")
        return

    stage_name = "stage" if expected_target == "jf10" else "stage-jf12"
    stage_path = build / stage_name / "meta.json"
    try:
        stage = json.loads(stage_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
        fail(f"cannot read pinned stage metadata {stage_path}: {error}")
        return
    if not isinstance(stage, dict):
        fail(f"pinned stage metadata is not an object: {stage_path}")
        return

    if kind == "self":
        expected = {
            "candidateVersion": stage.get("version"),
            "embeddedCandidateVersion": stage.get("version"),
            "candidateSourceRevision": stage.get("sourceRevision"),
            "candidateSourceTreeSha256": stage.get("sourceTreeSha256"),
        }
        version = stage.get("version")
        if not isinstance(version, str):
            fail("pinned stage has no package version")
            return
        suffix = "" if expected_target == "jf10" else "_jf12"
        package = build / f"jellyfin-refresh-kit_{version}{suffix}.zip"
        try:
            expected["candidateMd5"] = file_md5(package)
        except OSError as error:
            fail(f"cannot hash pinned package {package}: {error}")
            return
    else:
        expected = {
            "refreshKitSnapshot": build.name,
            "refreshKitPackageVersion": stage.get("version"),
            "refreshKitSourceRevision": stage.get("sourceRevision"),
            "refreshKitSourceTreeSha256": stage.get("sourceTreeSha256"),
        }

    for field, wanted in expected.items():
        if metadata.get(field) != wanted:
            fail(f"metadata {field} is {metadata.get(field)!r}, expected {wanted!r}")


def compatibility_matrices() -> dict[str, str]:
    value = json.loads(COMPAT_MATRICES.read_text(encoding="utf-8"))
    matrices = value.get("matrices") if isinstance(value, dict) else None
    if not isinstance(matrices, list):
        raise ValueError("compatibility matrices.json has no matrices array")
    result: dict[str, str] = {}
    for row in matrices:
        matrix_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(matrix_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]*", matrix_id) is None:
            raise ValueError(f"invalid compatibility matrix id: {matrix_id!r}")
        runtime = row.get("runtime") if isinstance(row, dict) else None
        if runtime not in ("jf10", "jf12"):
            raise ValueError(f"compatibility matrix {matrix_id} has invalid runtime {runtime!r}")
        if matrix_id in result:
            raise ValueError("compatibility matrices.json repeats a matrix id")
        result[matrix_id] = str(runtime)
    return result


def compatibility_services() -> dict[str, str]:
    value = json.loads(COMPAT_MATRICES.read_text(encoding="utf-8"))
    matrices = value.get("matrices") if isinstance(value, dict) else None
    if not isinstance(matrices, list):
        raise ValueError("compatibility matrices.json has no matrices array")
    result: dict[str, str] = {}
    for row in matrices:
        matrix_id = row.get("id") if isinstance(row, dict) else None
        service = row.get("service") if isinstance(row, dict) else None
        if (
            not isinstance(matrix_id, str)
            or not isinstance(service, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9-]*", service) is None
        ):
            raise ValueError(
                f"compatibility matrix {matrix_id!r} has invalid service {service!r}"
            )
        result[matrix_id] = service
    return result


def compatibility_install_orders() -> dict[str, list[str]]:
    value = json.loads(COMPAT_MATRICES.read_text(encoding="utf-8"))
    matrices = value.get("matrices") if isinstance(value, dict) else None
    if not isinstance(matrices, list):
        raise ValueError("compatibility matrices.json has no matrices array")
    result: dict[str, list[str]] = {}
    for row in matrices:
        matrix_id = row.get("id") if isinstance(row, dict) else None
        install_order = row.get("installOrder") if isinstance(row, dict) else None
        if (
            not isinstance(matrix_id, str)
            or matrix_id in result
            or not isinstance(install_order, list)
            or not install_order
            or any(
                not isinstance(artifact_id, str) or not artifact_id
                for artifact_id in install_order
            )
            or len(install_order) != len(set(install_order))
            or install_order.count("@refresh-kit") != 1
        ):
            raise ValueError(
                f"compatibility matrix {matrix_id!r} has an invalid install order"
            )
        result[matrix_id] = install_order
    return result


def compatibility_webroot_disk_requirements() -> dict[str, dict[str, Any]]:
    value = json.loads(COMPAT_MATRICES.read_text(encoding="utf-8"))
    matrices = value.get("matrices") if isinstance(value, dict) else None
    if not isinstance(matrices, list):
        raise ValueError("compatibility matrices.json has no matrices array")
    result: dict[str, dict[str, Any]] = {}
    for row in matrices:
        if not isinstance(row, dict):
            raise ValueError("compatibility matrix row is not an object")
        matrix_id = row.get("id")
        requirements = row.get("webrootDiskRequirements")
        if not isinstance(matrix_id, str) or not isinstance(requirements, dict):
            raise ValueError(
                f"compatibility matrix {matrix_id!r} has invalid disk requirements"
            )
        for artifact_id, requirement in requirements.items():
            if (
                not isinstance(artifact_id, str)
                or not artifact_id
                or not isinstance(requirement, dict)
                or set(requirement) != {"mode", "cardinality", "markers"}
            ):
                raise ValueError(
                    f"compatibility matrix {matrix_id}/{artifact_id} has invalid disk requirement"
                )
            mode = requirement.get("mode")
            cardinality = requirement.get("cardinality")
            markers = requirement.get("markers")
            if (
                mode not in ("absent", "added")
                or type(cardinality) is not int
                or cardinality != (0 if mode == "absent" else 1)
                or not isinstance(markers, list)
                or not markers
                or any(not isinstance(marker, str) or not marker for marker in markers)
                or len(markers) != len(set(markers))
            ):
                raise ValueError(
                    f"compatibility matrix {matrix_id}/{artifact_id} has invalid disk requirement"
                )
        if requirements:
            result[matrix_id] = requirements
    return result


def compatibility_safe_degrade_matrices() -> list[str]:
    value = json.loads(COMPAT_MATRICES.read_text(encoding="utf-8"))
    matrices = value.get("matrices") if isinstance(value, dict) else None
    if not isinstance(matrices, list):
        raise ValueError("compatibility matrices.json has no matrices array")
    return [
        str(row["id"])
        for row in matrices
        if isinstance(row, dict) and row.get("cacheExpectation") == "safe-degrade"
    ]


def compatibility_cache_expectations() -> dict[str, str]:
    value = json.loads(COMPAT_MATRICES.read_text(encoding="utf-8"))
    matrices = value.get("matrices") if isinstance(value, dict) else None
    if not isinstance(matrices, list):
        raise ValueError("compatibility matrices.json has no matrices array")
    result: dict[str, str] = {}
    for row in matrices:
        if not isinstance(row, dict):
            raise ValueError("compatibility matrix row is not an object")
        matrix_id = str(row.get("id", ""))
        expectation = row.get("cacheExpectation")
        if expectation not in ("required", "observe", "safe-degrade"):
            raise ValueError(
                f"compatibility matrix {matrix_id} has invalid cache expectation {expectation!r}"
            )
        result[matrix_id] = str(expectation)
    return result


def compatibility_runtime_order_contract() -> tuple[
    dict[str, str | None],
    dict[str, list[str] | None],
    dict[str, list[str]],
]:
    value = json.loads(COMPAT_MATRICES.read_text(encoding="utf-8"))
    matrices = value.get("matrices") if isinstance(value, dict) else None
    if not isinstance(matrices, list):
        raise ValueError("compatibility matrices.json has no matrices array")
    order_pairs: dict[str, str | None] = {}
    runtime_orders: dict[str, list[str] | None] = {}
    pair_members: dict[str, list[str]] = {}
    pair_cache_expectations: dict[str, str] = {}
    pair_runtimes: dict[str, str] = {}
    for row in matrices:
        if not isinstance(row, dict):
            raise ValueError("compatibility matrix row is not an object")
        matrix_id = row.get("id")
        order_pair = row.get("orderPair")
        runtime_order = row.get("expectedRuntimePluginOrder")
        if (
            not isinstance(matrix_id, str)
            or matrix_id in order_pairs
            or not (
                (order_pair is None and runtime_order is None)
                or (
                    isinstance(order_pair, str)
                    and re.fullmatch(r"[a-z0-9][a-z0-9-]*", order_pair)
                    is not None
                    and isinstance(runtime_order, list)
                    and bool(runtime_order)
                    and all(
                        isinstance(artifact_id, str) and bool(artifact_id)
                        for artifact_id in runtime_order
                    )
                    and len(runtime_order) == len(set(runtime_order))
                )
            )
        ):
            raise ValueError(
                f"compatibility matrix {matrix_id!r} has invalid runtime-order contract"
            )
        order_pairs[matrix_id] = order_pair
        runtime_orders[matrix_id] = runtime_order
        if order_pair is not None:
            pair_members.setdefault(order_pair, []).append(matrix_id)
            expectation = row.get("cacheExpectation")
            runtime = row.get("runtime")
            if order_pair in pair_cache_expectations:
                if pair_cache_expectations[order_pair] != expectation:
                    raise ValueError(
                        f"compatibility order pair {order_pair} has differing cache expectations"
                    )
            elif isinstance(expectation, str):
                pair_cache_expectations[order_pair] = expectation
            if order_pair in pair_runtimes:
                if pair_runtimes[order_pair] != runtime:
                    raise ValueError(
                        f"compatibility order pair {order_pair} has differing runtimes"
                    )
            elif isinstance(runtime, str):
                pair_runtimes[order_pair] = runtime
    for order_pair, members in pair_members.items():
        if (
            len(members) != 2
            or runtime_orders[members[0]] != runtime_orders[members[1]]
        ):
            raise ValueError(
                f"compatibility order pair {order_pair} contract is invalid"
            )
    return order_pairs, runtime_orders, pair_members


def compatibility_unversioned_outer_limitations() -> dict[str, list[str]]:
    value = json.loads(COMPAT_MATRICES.read_text(encoding="utf-8"))
    matrices = value.get("matrices") if isinstance(value, dict) else None
    if not isinstance(matrices, list):
        raise ValueError("compatibility matrices.json has no matrices array")
    result: dict[str, list[str]] = {}
    for row in matrices:
        if not isinstance(row, dict):
            raise ValueError("compatibility matrix row is not an object")
        matrix_id = str(row.get("id", ""))
        artifacts = row.get("requiredUnversionedOuterArtifacts", [])
        if not isinstance(artifacts, list) or any(
            not isinstance(artifact_id, str) for artifact_id in artifacts
        ):
            raise ValueError(
                f"compatibility matrix {matrix_id} has invalid unversioned limitations"
            )
        if artifacts:
            result[matrix_id] = artifacts
    return result


def validate_compatibility_stage(
    value: dict[str, Any],
    matrix_id: str,
    expected_runtime: str,
    build_dir: pathlib.Path | None,
    strict_errors: list[str],
) -> None:
    label = pathlib.Path(matrix_id) / "stage.json"
    if build_dir is None or not build_dir.is_dir():
        strict_errors.append("strict compatibility evidence requires its pinned build snapshot")
        return
    stage_name = "stage" if expected_runtime == "jf10" else "stage-jf12"
    expected_stage = (build_dir / stage_name).resolve()
    recorded_stage = value.get("stage")
    if value.get("runtime") != expected_runtime:
        strict_errors.append(f"compatibility stage runtime is wrong: {label}")
    if not isinstance(recorded_stage, str) or pathlib.Path(recorded_stage).resolve() != expected_stage:
        strict_errors.append(f"compatibility stage does not name the pinned snapshot: {label}")
    try:
        expected_meta = json.loads((expected_stage / "meta.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        strict_errors.append(f"pinned compatibility stage metadata is unavailable: {expected_stage}")
        return
    if value.get("meta") != expected_meta:
        strict_errors.append(f"compatibility stage metadata does not match the pinned snapshot: {label}")
    expected_dll = expected_stage / "Jellyfin.Plugin.RefreshKit.dll"
    if not expected_dll.is_file() or value.get("dllSha256") != file_hash(expected_dll):
        strict_errors.append(f"compatibility stage DLL does not match the pinned snapshot: {label}")


def collect_compatibility(
    output: pathlib.Path,
    evidence: dict[str, Any],
    strict_errors: list[str],
    required: bool,
    build_dir: pathlib.Path | None = None,
    compatibility_exit: int | None = None,
) -> None:
    matrices = compatibility_matrices()
    matrix_ids = list(matrices)
    services = compatibility_services()
    install_orders = compatibility_install_orders()
    disk_requirements = compatibility_webroot_disk_requirements()
    cache_expectations = compatibility_cache_expectations()
    order_pairs, runtime_orders, pair_members = compatibility_runtime_order_contract()
    expected_safe_degraded = compatibility_safe_degrade_matrices()
    expected_limitations = compatibility_unversioned_outer_limitations()
    expected_limited_matrices = list(expected_limitations)
    expected_pair_checks = {order_pair: True for order_pair in sorted(pair_members)}
    if required and compatibility_exit != 0:
        strict_errors.append(
            f"compatibility runner exit status is {compatibility_exit!r}, expected 0"
        )
    relative_paths = [
        pathlib.Path("all-locked-verification.json"),
        pathlib.Path("summary.json"),
    ]
    relative_paths.extend(
        pathlib.Path(matrix_id) / name
        for matrix_id in matrix_ids
        for name in COMPAT_MATRIX_FILES
    )
    result_values: dict[str, dict[str, Any]] = {}
    artifact_values: dict[str, dict[str, Any]] = {}
    all_locked_value: dict[str, Any] | None = None
    summary_value: dict[str, Any] | None = None
    for relative in relative_paths:
        source = COMPAT_ARTIFACTS / relative
        label = f"compat:{relative.as_posix()}"
        if not source.is_file():
            evidence["missing"].append(label)
            if required:
                strict_errors.append(f"missing required compatibility evidence: {relative}")
            continue
        try:
            raw_json = source.read_bytes()
            value = json.loads(raw_json.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            evidence["missing"].append(f"{label} (invalid JSON)")
            if required:
                strict_errors.append(f"invalid compatibility evidence JSON: {relative}")
            continue
        target = output / "compat" / relative
        if relative.as_posix() == "all-locked-verification.json":
            if not exact_json_value(sanitize_json(value), value):
                evidence["missing"].append(f"{label} (unsafe exact JSON)")
                if required:
                    strict_errors.append(
                        "all-locked verification contains content requiring redaction"
                    )
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw_json)
        else:
            write_json(target, sanitize_json(value))
        relative_target = target.relative_to(output).as_posix()
        if relative_target not in evidence["collected"]:
            evidence["collected"].append(relative_target)

        if relative.name == "result.json" and isinstance(value, dict):
            result_values[relative.parent.name] = value
        elif (
            relative.name == "artifact-verification.json"
            and isinstance(value, dict)
        ):
            artifact_values[relative.parent.name] = value
        elif (
            relative.as_posix() == "all-locked-verification.json"
            and isinstance(value, dict)
        ):
            all_locked_value = value
        elif relative.as_posix() == "summary.json" and isinstance(value, dict):
            summary_value = value

        if not required or not isinstance(value, dict):
            if required and not isinstance(value, dict):
                strict_errors.append(f"compatibility evidence is not an object: {relative}")
            continue
        if relative.as_posix() == "summary.json":
            if (
                value.get("outcome")
                != ("pass-with-limitation" if expected_limited_matrices else "pass")
                or value.get("expectedMatrices") != matrix_ids
                or value.get("completedMatrices") != matrix_ids
                or value.get("missingMatrices") != []
                or value.get("failedMatrices") != []
                or value.get("expectedSafeDegradedMatrices") != expected_safe_degraded
                or value.get("safeDegradedMatrices") != expected_safe_degraded
                or value.get("missingSafeDegradedMatrices") != []
                or value.get("expectedPassWithLimitationMatrices")
                != expected_limited_matrices
                or value.get("passWithLimitationMatrices") != expected_limited_matrices
                or value.get("missingPassWithLimitationMatrices") != []
                or value.get("unexpectedPassWithLimitationMatrices") != []
                or "pairRuntimeOrderChecks" not in value
                or not exact_json_value(
                    value["pairRuntimeOrderChecks"], expected_pair_checks
                )
            ):
                strict_errors.append("compatibility summary does not prove every matrix passed")
        elif relative.name == "result.json":
            matrix_id = relative.parent.name
            expected_artifacts = expected_limitations.get(matrix_id, [])
            expected_outcome = "pass-with-limitation" if expected_artifacts else "pass"
            limitations = value.get("limitations")
            limitation_artifacts = (
                [row.get("artifactId") for row in limitations]
                if isinstance(limitations, list)
                and all(isinstance(row, dict) for row in limitations)
                else None
            )
            limitations_valid = (
                limitation_artifacts == expected_artifacts
                and isinstance(limitations, list)
                and all(
                    row.get("code") == "outer-owner-unversioned-shell-tag"
                    and row.get("status") == "pass-with-limitation"
                    and isinstance(row.get("checks"), dict)
                    and row["checks"]
                    and all(passed is True for passed in row["checks"].values())
                    for row in limitations
                )
            )
            expected_disk = disk_requirements.get(matrix_id)
            expected_runtime_order = runtime_orders[matrix_id]
            if value.get("matrix") != matrix_id or value.get("outcome") != expected_outcome:
                strict_errors.append(f"compatibility result is not a pass: {relative}")
            elif value.get("runtime") != matrices[matrix_id] or value.get("service") != services[matrix_id]:
                strict_errors.append(
                    f"compatibility result runtime/service identity is wrong: {relative}"
                )
            elif (
                "orderPair" not in value
                or "expectedRuntimePluginOrder" not in value
                or "observedRuntimePluginOrder" not in value
                or not exact_json_value(value["orderPair"], order_pairs[matrix_id])
                or not exact_json_value(
                    value["expectedRuntimePluginOrder"], expected_runtime_order
                )
                or not exact_json_value(
                    value["observedRuntimePluginOrder"], expected_runtime_order
                )
            ):
                strict_errors.append(
                    f"compatibility result runtime plugin order proof is wrong: {relative}"
                )
            elif expected_disk is None and (
                "webrootDisk" not in value or value["webrootDisk"] is not None
            ):
                strict_errors.append(
                    f"compatibility nondisk result webrootDisk is not explicit null: {relative}"
                )
            elif not limitations_valid:
                strict_errors.append(
                    f"compatibility result limitations are incomplete or unexpected: {relative}"
                )
            elif value.get("cacheExpectation") != cache_expectations[relative.parent.name]:
                strict_errors.append(
                    f"compatibility result has wrong cache expectation: {relative}"
                )
            elif cache_expectations[matrix_id] == "required":
                refresh_kit = value.get("refreshKit")
                cache_evidence = (
                    refresh_kit.get("cacheEvidence")
                    if isinstance(refresh_kit, dict)
                    else None
                )
                checks = (
                    cache_evidence.get("requiredChecks")
                    if isinstance(cache_evidence, dict)
                    else None
                )
                if (
                    not isinstance(checks, dict)
                    or set(checks) != REQUIRED_CACHE_CHECK_NAMES
                    or any(passed is not True for passed in checks.values())
                ):
                    strict_errors.append(
                        f"compatibility required cache proof is incomplete: {relative}"
                    )
            elif cache_expectations[matrix_id] == "safe-degrade":
                refresh_kit = value.get("refreshKit")
                cache_evidence = (
                    refresh_kit.get("cacheEvidence")
                    if isinstance(refresh_kit, dict)
                    else None
                )
                checks = (
                    cache_evidence.get("safeDegradeChecks")
                    if isinstance(cache_evidence, dict)
                    else None
                )
                primary = (
                    cache_evidence.get("primary")
                    if isinstance(cache_evidence, dict)
                    else None
                )
                conditional = (
                    cache_evidence.get("conditional")
                    if isinstance(cache_evidence, dict)
                    else None
                )
                if (
                    not isinstance(checks, dict)
                    or set(checks) != SAFE_DEGRADE_CHECK_NAMES
                    or any(passed is not True for passed in checks.values())
                    or not isinstance(primary, dict)
                    or primary.get("framingMode") not in ("content-length", "chunked")
                    or not isinstance(conditional, dict)
                    or conditional.get("framingMode") not in ("content-length", "chunked")
                ):
                    strict_errors.append(
                        f"compatibility safe-degrade proof is incomplete: {relative}"
                    )
        elif relative.name == "artifact-verification.json" and value.get("allPassed") is not True:
            strict_errors.append(f"artifact verification is not a pass: {relative}")
        elif relative.name == "stage.json" and value.get("valid") is not True:
            strict_errors.append(f"stage verification is not valid: {relative}")
        elif relative.name == "stage.json":
            validate_compatibility_stage(
                value,
                relative.parent.name,
                matrices[relative.parent.name],
                build_dir,
                strict_errors,
            )
        elif relative.name == "network.json":
            if value.get("valid") is not True or value.get("allPassed") is not True:
                strict_errors.append(f"network isolation evidence is not a pass: {relative}")
            if value.get("service") != services[relative.parent.name]:
                strict_errors.append(
                    f"network isolation service identity is wrong: {relative}"
                )
        elif relative.name == "static.json" and value.get("allPassed") is not True:
            strict_errors.append(f"static compatibility evidence is not a pass: {relative}")

    if required:
        try:
            locked_artifacts = load_compatibility_artifact_lock(COMPAT_LOCK)
            locked_ids = [row["id"] for row in locked_artifacts]
            if all_locked_value is None:
                raise EvidenceValidationError(
                    "all-locked-verification.json was not retained as a JSON object"
                )
            all_locked_rows = validate_artifact_verification_report(
                all_locked_value,
                locked_artifacts,
                locked_ids,
                "all-locked-verification.json",
            )
            all_locked_by_id = {row["id"]: row for row in all_locked_rows}
            testable_ids = {
                row["id"]
                for row in locked_artifacts
                if row["disposition"] == "testable"
            }
            covered_ids = {
                artifact_id
                for install_order in install_orders.values()
                for artifact_id in install_order
                if artifact_id != "@refresh-kit"
            }
            if covered_ids != testable_ids:
                raise EvidenceValidationError(
                    "compatibility matrices do not cover exactly every testable artifact"
                )
            retained_all_locked = output / "compat" / "all-locked-verification.json"
            expected_summary = artifact_verification_summary(
                retained_all_locked, all_locked_value
            )
            if (
                not isinstance(summary_value, dict)
                or "allLockedVerification" not in summary_value
                or not exact_json_value(
                    summary_value["allLockedVerification"], expected_summary
                )
            ):
                raise EvidenceValidationError(
                    "compatibility summary all-locked verification binding differs"
                )
            for matrix_id in matrix_ids:
                expected_ids = [
                    artifact_id
                    for artifact_id in install_orders[matrix_id]
                    if artifact_id != "@refresh-kit"
                ]
                report = artifact_values.get(matrix_id)
                if not isinstance(report, dict):
                    raise EvidenceValidationError(
                        f"{matrix_id}: artifact verification report is missing"
                    )
                rows = validate_artifact_verification_report(
                    report,
                    locked_artifacts,
                    expected_ids,
                    f"{matrix_id}/artifact-verification.json",
                )
                projection = [all_locked_by_id[artifact_id] for artifact_id in expected_ids]
                if not exact_json_value(rows, projection):
                    raise EvidenceValidationError(
                        f"{matrix_id}: artifact report differs from all-locked projection"
                    )
                result = result_values.get(matrix_id)
                plugins = result.get("plugins") if isinstance(result, dict) else None
                if (
                    not isinstance(result, dict)
                    or "requestedInstallOrder" not in result
                    or not exact_json_value(
                        result["requestedInstallOrder"], install_orders[matrix_id]
                    )
                    or not isinstance(plugins, list)
                    or [
                        row.get("artifactId") if isinstance(row, dict) else None
                        for row in plugins
                    ]
                    != expected_ids
                ):
                    raise EvidenceValidationError(
                        f"{matrix_id}: result artifact/install order differs"
                    )
                for plugin, row in zip(plugins, rows, strict=True):
                    materialization = plugin.get("materialization")
                    if (
                        "artifactVerification" not in plugin
                        or not exact_json_value(plugin["artifactVerification"], row)
                        or not isinstance(materialization, dict)
                        or materialization.get("materialized") is not True
                        or materialization.get("archiveSha256")
                        != row["archive"]["sha256"]
                        or materialization.get("assemblySha256")
                        != row["plugin"]["assemblySha256"]
                        or "dllInventory" not in materialization
                        or not exact_json_value(
                            materialization["dllInventory"],
                            row["plugin"]["managedDlls"],
                        )
                    ):
                        raise EvidenceValidationError(
                            f"{matrix_id}/{row['id']}: result artifact binding differs"
                        )
        except EvidenceValidationError as error:
            strict_errors.append(str(error))

    retained_raw: dict[str, dict[str, bytes]] = {}
    for matrix_id in disk_requirements:
        for name in COMPAT_WEBROOT_FILES:
            relative = pathlib.Path(matrix_id) / name
            source = COMPAT_ARTIFACTS / relative
            label = f"compat:{relative.as_posix()}"
            if not source.is_file():
                evidence["missing"].append(label)
                if required:
                    strict_errors.append(
                        f"missing required compatibility evidence: {relative}"
                    )
                continue
            target = output / "compat" / relative
            try:
                raw = copy_exact_compatibility_webroot(
                    source, target, relative.as_posix()
                )
            except ValueError as error:
                evidence["missing"].append(f"{label} (unsafe exact bytes)")
                if required:
                    strict_errors.append(str(error))
                continue
            retained_raw.setdefault(matrix_id, {})[name] = raw
            relative_target = target.relative_to(output).as_posix()
            if relative_target not in evidence["collected"]:
                evidence["collected"].append(relative_target)

    retained_http: dict[str, dict[str, bytes]] = {}
    for matrix_id in matrix_ids:
        for name in COMPAT_RAW_HTTP_FILES:
            relative = pathlib.Path(matrix_id) / name
            source = COMPAT_ARTIFACTS / relative
            label = f"compat:{relative.as_posix()}"
            if not source.is_file():
                evidence["missing"].append(label)
                if required:
                    strict_errors.append(
                        f"missing required compatibility evidence: {relative}"
                    )
                continue
            target = output / "compat" / relative
            try:
                raw = copy_exact_compatibility_http(
                    source, target, relative.as_posix()
                )
            except ValueError as error:
                evidence["missing"].append(f"{label} (unsafe exact bytes)")
                if required:
                    strict_errors.append(str(error))
                continue
            retained_http.setdefault(matrix_id, {})[name] = raw
            relative_target = target.relative_to(output).as_posix()
            if relative_target not in evidence["collected"]:
                evidence["collected"].append(relative_target)

    if required:
        if (
            summary_value is None
            or set(result_values) != set(matrix_ids)
            or "results" not in summary_value
            or not exact_json_value(
                summary_value["results"],
                [result_values[matrix_id] for matrix_id in matrix_ids]
                if set(result_values) == set(matrix_ids)
                else None,
            )
        ):
            strict_errors.append(
                "compatibility summary results differ from retained matrix results"
            )
        for matrix_id in matrix_ids:
            captures = retained_http.get(matrix_id, {})
            if set(captures) != set(COMPAT_RAW_HTTP_FILES):
                continue
            result = result_values.get(matrix_id)
            if not isinstance(result, dict):
                continue
            try:
                reconstructed_cache = reconstruct_compatibility_cache_evidence(
                    captures,
                    result,
                    cache_expectations[matrix_id],
                    matrix_id,
                )
            except EvidenceValidationError as error:
                strict_errors.append(str(error))
                continue
            refresh_kit = result.get("refreshKit")
            cache_evidence = (
                refresh_kit.get("cacheEvidence")
                if isinstance(refresh_kit, dict)
                else None
            )
            if not isinstance(cache_evidence, dict) or any(
                field not in cache_evidence
                or not exact_json_value(
                    cache_evidence[field], reconstructed_cache[field]
                )
                for field in (
                    "expectation",
                    "primary",
                    "conditional",
                    "requiredChecks",
                    "safeDegradeChecks",
                )
            ):
                strict_errors.append(
                    "compatibility cache evidence differs from retained HTTP bytes: "
                    f"{matrix_id}"
                )
        for matrix_id, requirements in disk_requirements.items():
            captures = retained_raw.get(matrix_id, {})
            if set(captures) != set(COMPAT_WEBROOT_FILES):
                continue
            reconstructed = reconstruct_webroot_disk(
                captures["webroot-before.html"],
                captures["webroot-after.html"],
                requirements,
            )
            result_disk = result_values.get(matrix_id, {}).get("webrootDisk")
            if reconstructed["allPassed"] is not True:
                strict_errors.append(
                    f"compatibility raw webroot contract failed: {matrix_id}"
                )
            if not exact_json_value(result_disk, reconstructed):
                strict_errors.append(
                    f"compatibility result webrootDisk differs from retained raw bytes: {matrix_id}"
                )


def main() -> int:
    args = parse_args()
    output = args.output
    if not output.is_absolute():
        output = ROOT / output
    if output.is_symlink():
        raise SystemExit(f"FATAL: evidence output must not be a symlink: {output}")
    output = output.resolve()
    protected = [
        (ROOT / ".git").resolve(),
        LAB_ARTIFACTS.resolve(),
        COMPAT_ARTIFACTS.resolve(),
    ]
    if output == ROOT.resolve() or any(output == path or output.is_relative_to(path) for path in protected):
        raise SystemExit(f"FATAL: unsafe evidence output path: {output}")
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise SystemExit(f"FATAL: evidence output is not a regular directory: {output}")
    if output.exists() and any(output.iterdir()):
        raise SystemExit(
            f"FATAL: evidence output must be fresh; refusing stale files in {output}"
        )
    output.mkdir(parents=True, exist_ok=True)

    run_path = output / "run.json"

    source_status = run("git", "status", "--porcelain", "--untracked-files=all")
    statuses: dict[str, int] = {}
    if args.jellyfin_exit is not None:
        statuses["dualJellyfinLab"] = args.jellyfin_exit
    if args.abi_floor_exit is not None:
        statuses["abiFloorLab"] = args.abi_floor_exit
    if args.host_upgrade_exit is not None:
        statuses["hostUpgradeLab"] = args.host_upgrade_exit
    if args.proxy_exit is not None:
        statuses["proxyRig"] = args.proxy_exit
    if args.compatibility_exit is not None:
        statuses["compatibilityMatrices"] = args.compatibility_exit
    evidence = {
        "schemaVersion": 1,
        "sourceRevision": run("git", "rev-parse", "HEAD"),
        "sourceDirty": bool(source_status),
        "sourceStatus": source_status.splitlines() if source_status else [],
        "github": {
            key: os.environ[key]
            for key in (
                "GITHUB_RUN_ID",
                "GITHUB_RUN_ATTEMPT",
                "GITHUB_WORKFLOW",
                "GITHUB_SHA",
                "GITHUB_REF",
                "GITHUB_JOB",
                "GITHUB_REPOSITORY",
                "GITHUB_EVENT_NAME",
                "GITHUB_WORKFLOW_REF",
                "GITHUB_WORKFLOW_SHA",
            )
            if key in os.environ
        },
        "exitStatus": statuses,
        "tools": {
            "dotnet": run("dotnet", "--version"),
            "node": run("node", "--version"),
            "npm": run("npm", "--version"),
            "python": run("python3", "--version"),
            "docker": run("docker", "--version"),
            "compose": run("docker", "compose", "version"),
        },
        "packageBuild": collect_package_identity(args.build_dir),
        "collected": [],
        "missing": [],
    }
    strict_errors: list[str] = []
    if args.require_integration_evidence:
        if args.jellyfin_exit != 0:
            strict_errors.append(
                f"dual Jellyfin runner exit status is {args.jellyfin_exit!r}, expected 0"
            )
        if args.abi_floor_exit != 0:
            strict_errors.append(
                f"ABI-floor runner exit status is {args.abi_floor_exit!r}, expected 0"
            )
        if args.host_upgrade_exit != 0:
            strict_errors.append(
                f"host-upgrade runner exit status is {args.host_upgrade_exit!r}, expected 0"
            )
        if args.proxy_exit != 0:
            strict_errors.append(f"proxy runner exit status is {args.proxy_exit!r}, expected 0")
    collect_integration = (
        args.require_integration_evidence
        or args.jellyfin_exit is not None
        or args.abi_floor_exit is not None
        or args.host_upgrade_exit is not None
        or args.proxy_exit is not None
        or "dualJellyfinLab" in statuses
        or "abiFloorLab" in statuses
        or "hostUpgradeLab" in statuses
        or "proxyRig" in statuses
    )
    integration_build = resolve_build_path(args.build_dir) if args.build_dir is not None else None
    if args.require_integration_evidence and integration_build is None:
        strict_errors.append("strict integration evidence requires its pinned build snapshot")
    elif (
        args.require_integration_evidence
        and evidence["packageBuild"].get("immutableSnapshot") is None
    ):
        strict_errors.append(
            "strict integration evidence requires an immutable repository build snapshot"
        )

    for relative in (SAFE_JSON if collect_integration else ()):
        source = LAB_ARTIFACTS / relative
        if not source.is_file():
            evidence["missing"].append(relative)
            if args.require_integration_evidence:
                strict_errors.append(f"missing required integration evidence: {relative}")
            continue
        try:
            value = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            evidence["missing"].append(f"{relative} (invalid JSON)")
            if args.require_integration_evidence:
                strict_errors.append(f"invalid integration evidence JSON: {relative}")
            continue
        if args.require_integration_evidence and relative in INTEGRATION_LIFECYCLE_RESULTS:
            validate_integration_lifecycle(relative, value, integration_build, strict_errors)
        target = output / "lab" / relative
        write_json(target, sanitize_json(value))
        relative_target = target.relative_to(output).as_posix()
        if relative_target not in evidence["collected"]:
            evidence["collected"].append(relative_target)

    for relative in (EXACT_ANONYMOUS_HTTP if collect_integration else ()):
        source = LAB_ARTIFACTS / relative
        if not source.is_file():
            evidence["missing"].append(relative)
            if args.require_integration_evidence:
                strict_errors.append(f"missing required integration evidence: {relative}")
            continue
        target = output / "lab" / relative
        try:
            copy_exact_anonymous_http(source, target, relative)
        except ValueError as error:
            evidence["missing"].append(f"{relative} (unsafe exact bytes)")
            if args.require_integration_evidence:
                strict_errors.append(str(error))
            continue
        relative_target = target.relative_to(output).as_posix()
        if relative_target not in evidence["collected"]:
            evidence["collected"].append(relative_target)

    for relative in (SAFE_TEXT if collect_integration else ()):
        source = LAB_ARTIFACTS / relative
        if not source.is_file():
            evidence["missing"].append(relative)
            if args.require_integration_evidence:
                strict_errors.append(f"missing required integration evidence: {relative}")
            continue
        if source.stat().st_size == 0:
            evidence["missing"].append(f"{relative} (empty)")
            if args.require_integration_evidence:
                strict_errors.append(f"empty required integration evidence: {relative}")
        target = output / "lab" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            sanitize_text(source.read_text(encoding="utf-8", errors="replace")),
            encoding="utf-8",
        )
        relative_target = target.relative_to(output).as_posix()
        if relative_target not in evidence["collected"]:
            evidence["collected"].append(relative_target)

    if args.require_integration_evidence and integration_build is not None:
        try:
            validate_host_upgrade_evidence(
                output / "lab" / "host-upgrade", integration_build
            )
        except HostUpgradeEvidenceError as error:
            strict_errors.append(str(error))

    for relative_value in (SAFE_IMAGES if collect_integration else ()):
        relative = pathlib.Path(relative_value)
        source = LAB_ARTIFACTS / relative
        if not source.is_file():
            evidence["missing"].append(relative_value)
            if args.require_integration_evidence:
                strict_errors.append(f"missing required integration evidence: {relative_value}")
            continue
        target = output / "lab" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        relative_target = target.relative_to(output).as_posix()
        if relative_target not in evidence["collected"]:
            evidence["collected"].append(relative_target)

    for specification in args.log:
        if "=" not in specification:
            raise SystemExit(f"FATAL: --log must be NAME=PATH, got {specification!r}")
        name, raw_path = specification.split("=", 1)
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) is None:
            raise SystemExit(f"FATAL: unsafe log name: {name!r}")
        source = pathlib.Path(raw_path)
        if not source.is_file():
            evidence["missing"].append(f"log:{name}")
            if args.require_integration_evidence:
                strict_errors.append(f"missing required integration log: {name}")
            continue
        if source.stat().st_size == 0:
            evidence["missing"].append(f"log:{name} (empty)")
            if args.require_integration_evidence:
                strict_errors.append(f"empty required integration log: {name}")
        target = output / "logs" / f"{name}.log"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sanitize_text(source.read_text(encoding="utf-8", errors="replace")),
                          encoding="utf-8")
        relative_target = target.relative_to(output).as_posix()
        if relative_target not in evidence["collected"]:
            evidence["collected"].append(relative_target)

    if args.require_integration_evidence:
        supplied_logs = {specification.split("=", 1)[0] for specification in args.log if "=" in specification}
        missing_logs = sorted(set(INTEGRATION_LOGS) - supplied_logs)
        if missing_logs:
            strict_errors.append("missing required integration logs: " + ", ".join(missing_logs))
        if integration_build is not None:
            try:
                validate_integration_tree(output, integration_build)
            except EvidenceValidationError as error:
                strict_errors.append(str(error))

    collect_compat = (
        args.require_compatibility_evidence
        or args.compatibility_exit is not None
        or "compatibilityMatrices" in statuses
    )
    if collect_compat:
        collect_compatibility(
            output,
            evidence,
            strict_errors,
            args.require_compatibility_evidence,
            resolve_build_path(args.build_dir) if args.build_dir is not None else None,
            args.compatibility_exit,
        )
        if args.require_compatibility_evidence and args.build_dir is not None:
            try:
                validate_compatibility_tree(
                    output,
                    resolve_build_path(args.build_dir),
                    COMPAT_MATRICES,
                )
            except EvidenceValidationError as error:
                strict_errors.append(str(error))

    # Source-status paths and workflow metadata are retained too, so apply the
    # same value redaction to the complete machine-readable envelope.
    evidence = sanitize_json(evidence)
    write_json(run_path, evidence)

    # Refuse known unredacted credential forms before the workflow can upload.
    exact_anonymous_targets = {
        (output / "lab" / relative).resolve() for relative in EXACT_ANONYMOUS_HTTP
    }
    exact_compatibility_http_targets = {
        path.resolve()
        for path in (output / "compat").glob("*/*")
        if path.is_file() and path.name in COMPAT_RAW_HTTP_FILES
    }
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.suffix.lower() not in BINARY_SUFFIXES:
            if path.resolve() in (
                exact_anonymous_targets | exact_compatibility_http_targets
            ):
                # This byte-preserving path was already checked by
                # copy_exact_anonymous_http; the general source/log assignment
                # matcher intentionally has false positives in shipped JS.
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if contains_forbidden_secret(content):
                raise SystemExit(f"FATAL: unredacted credential pattern in evidence: {path}")

    checksums = []
    checksum_path = output / "SHA256SUMS"
    for path in sorted(output.rglob("*")):
        if path.is_file() and path != checksum_path:
            checksums.append(f"{file_hash(path)}  {path.relative_to(output).as_posix()}")
    checksum_path.write_text("\n".join(checksums) + "\n", encoding="utf-8")
    if strict_errors:
        for error in strict_errors:
            print(f"FATAL: {error}", file=sys.stderr)
        return 1
    print(f"==> Collected secret-safe CI evidence in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
