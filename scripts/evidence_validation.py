#!/usr/bin/env python3
"""Reusable fail-closed validation for retained release evidence."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from collections import Counter
from html.parser import HTMLParser
from typing import Any

from host_upgrade_evidence import (
    HostUpgradeEvidenceError,
    validate_evidence as validate_host_upgrade_evidence,
)
from abi_floor_evidence import (
    AbiFloorEvidenceError,
    validate_evidence as validate_abi_floor_evidence,
)


INTEGRATION_JSON = (
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
    "abi-floor/server/public.json",
    "abi-floor/server/generation.json",
    "abi-floor/server/diagnostics.json",
    "abi-floor/server/plugins.json",
    "host-upgrade/result.json",
    "host-upgrade/jf10/result.json",
    "host-upgrade/jf12/result.json",
)
INTEGRATION_TEXT = (
    "abi-floor/server/generation.headers",
    "abi-floor/server/kit.headers",
    "abi-floor/server/kit.js",
    "abi-floor/server/shell.headers",
    "abi-floor/server/conditional.headers",
    "abi-floor/server/conditional.body",
    "abi-floor/server/index.html",
    "abi-floor/server.log",
)
INTEGRATION_IMAGES = tuple(
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
INTEGRATION_LOGS = ("abi-floor", "dual-jellyfin", "host-upgrade", "proxy")
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
SERVER_VERSIONS = {"jf10": "10.11.11", "jf12": "12.0.0"}
PLUGIN_GUID = "515255fe-3332-49b0-b471-0be58c8221d8"
IMAGE_DIGESTS = {
    "jf10": "aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db",
    "jf12": "db1df1d111c27ba1f10bb8fce6630892f66eb66b12c2b24e79011453ac18b3db",
}
IMAGE_REFERENCES = {
    "jf10": f"jellyfin/jellyfin:10.11.11@sha256:{IMAGE_DIGESTS['jf10']}",
    "jf12": f"jellyfin/jellyfin:12.0-rc4@sha256:{IMAGE_DIGESTS['jf12']}",
}
GENERATION = re.compile(r"^g-[0-9a-f]{16}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MD5 = re.compile(r"^[0-9a-f]{32}$")
COMPAT_DISPOSITIONS = ("testable", "quarantined", "unsupported")
COMPAT_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
COMPAT_MAX_MEMBER_COUNT = 20_000
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


class EvidenceValidationError(ValueError):
    pass


def file_hash(path: pathlib.Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceValidationError(f"cannot read evidence JSON {path}: {error}") from error
    if not isinstance(value, dict):
        raise EvidenceValidationError(f"evidence JSON is not an object: {path}")
    return value


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceValidationError(message)


def exact_json_value(actual: Any, expected: Any) -> bool:
    """Compare JSON values without Python's bool/int equality shortcut."""
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


def _safe_compat_archive_path(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "\x00" in value
    ):
        return False
    path = pathlib.PurePosixPath(value)
    return (
        not path.is_absolute()
        and value == path.as_posix()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _normalized_compat_abi(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", text):
        return text + ".0"
    return text


def load_compatibility_artifact_lock(path: pathlib.Path) -> list[dict[str, Any]]:
    value = load_object(path)
    artifacts = value.get("artifacts")
    require(
        type(value.get("schemaVersion")) is int
        and value["schemaVersion"] == 1
        and isinstance(artifacts, list)
        and bool(artifacts)
        and all(isinstance(row, dict) for row in artifacts),
        "compatibility artifact lock is invalid",
    )
    ids = [row.get("id") for row in artifacts]
    require(
        all(
            isinstance(artifact_id, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]*", artifact_id) is not None
            for artifact_id in ids
        )
        and len(ids) == len(set(ids)),
        "compatibility artifact lock ids are invalid",
    )
    for artifact in artifacts:
        artifact_id = artifact["id"]
        archive = artifact.get("archive")
        plugin = artifact.get("plugin")
        require(
            artifact.get("disposition") in COMPAT_DISPOSITIONS
            and (
                artifact.get("runtime") in ("jf10", "jf12")
                if artifact.get("disposition") == "testable"
                else artifact.get("runtime") is None
            )
            and isinstance(archive, dict)
            and set(archive) == {"name", "url", "sha256"}
            and isinstance(plugin, dict)
            and set(plugin)
            == {
                "archiveMeta",
                "assembly",
                "framework",
                "guid",
                "name",
                "targetAbi",
                "version",
            }
            and isinstance(archive.get("sha256"), str)
            and SHA256.fullmatch(archive["sha256"]) is not None
            and isinstance(archive.get("name"), str)
            and bool(archive["name"])
            and isinstance(archive.get("url"), str)
            and archive["url"].startswith("https://github.com/")
            and "?" not in archive["url"]
            and "#" not in archive["url"],
            f"compatibility artifact lock row is invalid: {artifact_id}",
        )
        require(
            plugin.get("archiveMeta") in {"upstream", "absent-generate", "absent"}
            and isinstance(plugin.get("assembly"), str)
            and plugin["assembly"].casefold().endswith(".dll")
            and plugin.get("framework") in {"net8.0", "net9.0", "net10.0"}
            and isinstance(plugin.get("guid"), str)
            and re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                plugin["guid"],
            )
            is not None
            and isinstance(plugin.get("name"), str)
            and bool(plugin["name"])
            and all(
                isinstance(plugin.get(field), str)
                and re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", plugin[field])
                is not None
                for field in ("targetAbi", "version")
            ),
            f"compatibility locked plugin identity is invalid: {artifact_id}",
        )
    return artifacts


def validate_artifact_verification_report(
    report: dict[str, Any],
    locked_artifacts: list[dict[str, Any]],
    expected_ids: list[str],
    label: str,
) -> list[dict[str, Any]]:
    """Independently validate a serialized archive-inspection receipt."""
    locked_by_id = {row["id"]: row for row in locked_artifacts}
    require(
        len(locked_by_id) == len(locked_artifacts)
        and all(artifact_id in locked_by_id for artifact_id in expected_ids)
        and len(expected_ids) == len(set(expected_ids)),
        f"{label}: expected artifact selection is invalid",
    )
    rows = report.get("artifacts")
    require(
        set(report) == {"schemaVersion", "artifacts", "allPassed"}
        and type(report.get("schemaVersion")) is int
        and report["schemaVersion"] == 1
        and report.get("allPassed") is True
        and isinstance(rows, list)
        and len(rows) == len(expected_ids),
        f"{label}: report envelope is invalid",
    )
    require(
        [row.get("id") if isinstance(row, dict) else None for row in rows]
        == expected_ids,
        f"{label}: artifact ids/order differ from the lock selection",
    )
    for row, artifact_id in zip(rows, expected_ids, strict=True):
        artifact = locked_by_id[artifact_id]
        require(
            isinstance(row, dict)
            and set(row)
            == {"id", "disposition", "runtime", "archive", "plugin", "verified"}
            and row.get("id") == artifact_id
            and row.get("disposition") == artifact["disposition"]
            and row.get("runtime") == artifact["runtime"]
            and row.get("verified") is True,
            f"{label}/{artifact_id}: row identity is invalid",
        )
        archive = row.get("archive")
        locked_archive = artifact["archive"]
        expected_cache_name = f"{locked_archive['sha256']}-{locked_archive['name']}"
        require(
            isinstance(archive, dict)
            and set(archive) == {"path", "url", "sha256", "size", "memberCount"}
            and isinstance(archive.get("path"), str)
            and pathlib.Path(archive["path"]).name == expected_cache_name
            and archive.get("url") == locked_archive["url"]
            and "?" not in archive["url"]
            and "#" not in archive["url"]
            and archive.get("sha256") == locked_archive["sha256"]
            and type(archive.get("size")) is int
            and 0 < archive["size"] <= COMPAT_MAX_ARCHIVE_BYTES
            and type(archive.get("memberCount")) is int
            and 0 < archive["memberCount"] <= COMPAT_MAX_MEMBER_COUNT,
            f"{label}/{artifact_id}: archive evidence is invalid",
        )
        plugin = row.get("plugin")
        computed_fields = {
            "assemblyPath",
            "assemblySha256",
            "managedDlls",
            "binaryTokenChecks",
            "meta",
            "frameworkEvidence",
        }
        require(
            isinstance(plugin, dict)
            and set(plugin) == set(artifact["plugin"]) | computed_fields
            and all(
                field in plugin and exact_json_value(plugin[field], expected)
                for field, expected in artifact["plugin"].items()
            ),
            f"{label}/{artifact_id}: locked plugin identity differs",
        )
        assembly_path = plugin.get("assemblyPath")
        assembly_sha = plugin.get("assemblySha256")
        require(
            _safe_compat_archive_path(assembly_path)
            and pathlib.PurePosixPath(assembly_path).name.casefold()
            == artifact["plugin"]["assembly"].casefold()
            and isinstance(assembly_sha, str)
            and SHA256.fullmatch(assembly_sha) is not None
            and exact_json_value(
                plugin.get("binaryTokenChecks"), {"guid": True, "version": True}
            ),
            f"{label}/{artifact_id}: assembly identity evidence is invalid",
        )
        managed_dlls = plugin.get("managedDlls")
        require(
            isinstance(managed_dlls, dict)
            and bool(managed_dlls)
            and len({str(path).casefold() for path in managed_dlls})
            == len(managed_dlls)
            and all(
                _safe_compat_archive_path(path)
                and pathlib.PurePosixPath(path).suffix.casefold() == ".dll"
                and isinstance(digest, str)
                and SHA256.fullmatch(digest) is not None
                for path, digest in managed_dlls.items()
            )
            and managed_dlls.get(assembly_path) == assembly_sha,
            f"{label}/{artifact_id}: managed DLL hashes are incoherent",
        )
        _validate_artifact_meta_receipt(plugin.get("meta"), artifact, label)
        _validate_artifact_framework_receipt(
            plugin.get("frameworkEvidence"), artifact, assembly_path, label
        )
        evidenced_paths = {
            str(path).casefold() for path in managed_dlls
        }
        for optional_path in (
            plugin["meta"].get("archivePath"),
            plugin["frameworkEvidence"].get("depsPath"),
        ):
            if optional_path is not None:
                evidenced_paths.add(str(optional_path).casefold())
        require(
            archive["memberCount"] >= len(evidenced_paths),
            f"{label}/{artifact_id}: archive member count contradicts evidence paths",
        )
    return rows


def _validate_artifact_meta_receipt(
    value: Any, artifact: dict[str, Any], label: str
) -> None:
    artifact_id = artifact["id"]
    policy = artifact["plugin"]["archiveMeta"]
    require(
        isinstance(value, dict)
        and set(value) == {"policy", "archivePath", "present", "checks"}
        and value.get("policy") == policy,
        f"{label}/{artifact_id}: metadata receipt is invalid",
    )
    if policy != "upstream":
        require(
            value.get("archivePath") is None
            and value.get("present") is False
            and value.get("checks") is None,
            f"{label}/{artifact_id}: absent metadata receipt is incoherent",
        )
        return
    checks = value.get("checks")
    archive_path = value.get("archivePath")
    require(
        value.get("present") is True
        and _safe_compat_archive_path(archive_path)
        and pathlib.PurePosixPath(archive_path).name.casefold() == "meta.json"
        and isinstance(checks, dict)
        and set(checks) == {"guid", "name", "version", "targetAbi", "assemblies"},
        f"{label}/{artifact_id}: upstream metadata receipt is incomplete",
    )
    for field in ("guid", "name", "version"):
        check = checks.get(field)
        expected = artifact["plugin"][field]
        require(
            isinstance(check, dict)
            and set(check) == {"expected", "actual", "matched"}
            and check.get("expected") == expected
            and check.get("matched") is True
            and str(check.get("actual", "")).casefold() == str(expected).casefold(),
            f"{label}/{artifact_id}: metadata {field} proof is invalid",
        )
    target = checks.get("targetAbi")
    expected_abi = artifact["plugin"]["targetAbi"]
    require(
        isinstance(target, dict)
        and set(target) == {"expected", "actual", "matched", "present"}
        and target.get("expected") == expected_abi
        and target.get("matched") is True
        and type(target.get("present")) is bool
        and target["present"] == (target.get("actual") is not None)
        and (
            not target["present"]
            or _normalized_compat_abi(target.get("actual"))
            == _normalized_compat_abi(expected_abi)
        ),
        f"{label}/{artifact_id}: metadata target ABI proof is invalid",
    )
    assemblies = checks.get("assemblies")
    require(
        assemblies in (None, [])
        or (
            isinstance(assemblies, list)
            and all(isinstance(item, str) and bool(item) for item in assemblies)
            and len(assemblies) == len(set(assemblies))
            and artifact["plugin"]["assembly"] in assemblies
        ),
        f"{label}/{artifact_id}: metadata assembly proof is invalid",
    )


def _validate_artifact_framework_receipt(
    value: Any,
    artifact: dict[str, Any],
    assembly_path: str,
    label: str,
) -> None:
    artifact_id = artifact["id"]
    expected = artifact["plugin"]["framework"]
    require(
        isinstance(value, dict)
        and set(value) == {"expected", "depsPath", "runtimeTarget", "matched"}
        and value.get("expected") == expected,
        f"{label}/{artifact_id}: framework receipt is invalid",
    )
    deps_path = value.get("depsPath")
    if deps_path is None:
        require(
            value.get("runtimeTarget") is None and value.get("matched") is None,
            f"{label}/{artifact_id}: absent framework receipt is incoherent",
        )
        return
    expected_name = pathlib.PurePosixPath(assembly_path).stem + ".deps.json"
    major = expected.removeprefix("net").split(".")[0]
    require(
        _safe_compat_archive_path(deps_path)
        and pathlib.PurePosixPath(deps_path).name.casefold() == expected_name.casefold()
        and isinstance(value.get("runtimeTarget"), str)
        and f"Version=v{major}.0" in value["runtimeTarget"]
        and value.get("matched") is True,
        f"{label}/{artifact_id}: framework proof is invalid",
    )


def artifact_verification_summary(
    report_path: pathlib.Path, report: dict[str, Any]
) -> dict[str, Any]:
    rows = report["artifacts"]
    dispositions = Counter(row["disposition"] for row in rows)
    return {
        "report": report_path.name,
        "reportSha256": file_hash(report_path),
        "artifactCount": len(rows),
        "archiveBytes": sum(row["archive"]["size"] for row in rows),
        "dispositionCounts": {
            disposition: dispositions.get(disposition, 0)
            for disposition in COMPAT_DISPOSITIONS
        },
        "allPassed": report["allPassed"],
    }


_COMPAT_INERT_CONTAINERS = {
    "iframe",
    "math",
    "noembed",
    "noframes",
    "noscript",
    "plaintext",
    "style",
    "svg",
    "template",
    "textarea",
    "title",
    "xmp",
}
_COMPAT_JAVASCRIPT_TYPES = {
    "application/ecmascript",
    "application/javascript",
    "application/x-ecmascript",
    "application/x-javascript",
    "text/ecmascript",
    "text/javascript",
    "text/javascript1.0",
    "text/javascript1.1",
    "text/javascript1.2",
    "text/javascript1.3",
    "text/javascript1.4",
    "text/javascript1.5",
    "text/jscript",
    "text/livescript",
    "text/x-ecmascript",
    "text/x-javascript",
}


class _CompatibilityShellAssetParser(HTMLParser):
    """Mirror the compatibility analyzer's executable shell-asset selection."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.assets: Counter[str] = Counter()
        self.inert: Counter[str] = Counter()

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._handle(tag, attrs, self_closing=False)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self._handle(tag, attrs, self_closing=True)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if self.inert[normalized_tag] > 0:
            self.inert[normalized_tag] -= 1

    def _handle(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
        *,
        self_closing: bool,
    ) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in _COMPAT_INERT_CONTAINERS:
            if not self_closing or normalized_tag not in {"math", "svg"}:
                self.inert[normalized_tag] += 1
            return
        if any(self.inert.values()):
            return
        normalized: dict[str, str] = {}
        for key, value in attrs:
            normalized.setdefault(key.casefold(), value or "")
        url = ""
        if normalized_tag == "script":
            if "type" in normalized:
                script_type = normalized["type"].strip().casefold()
                executable = (
                    not script_type
                    or script_type == "module"
                    or script_type in _COMPAT_JAVASCRIPT_TYPES
                )
            else:
                language = normalized.get("language", "").strip().casefold()
                executable = (
                    not language
                    or f"text/{language}" in _COMPAT_JAVASCRIPT_TYPES
                )
            if executable:
                url = normalized.get("src", "")
        elif normalized_tag == "link":
            rel = {part.casefold() for part in normalized.get("rel", "").split()}
            link_type = normalized.get("type", "").strip().casefold()
            link_essence = link_type.split(";", 1)[0].strip()
            if "stylesheet" in rel and (
                not link_type or link_essence == "text/css"
            ):
                url = normalized.get("href", "")
        if url:
            self.assets[url] += 1


def _compatibility_text(raw: bytes, label: str) -> str:
    try:
        return raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise EvidenceValidationError(
            f"compatibility raw HTTP evidence is not UTF-8: {label}: {error}"
        ) from error


def _compatibility_headers(raw: bytes, label: str) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for raw_line in _compatibility_text(raw, label).splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        headers.setdefault(key.strip().casefold(), []).append(value.strip())
    return headers


def _compatibility_http_status(raw: bytes, label: str) -> int | None:
    statuses: list[int] = []
    for raw_line in _compatibility_text(raw, label).splitlines():
        match = re.fullmatch(r"HTTP/\S+\s+([0-9]{3})(?:\s+.*)?", raw_line.strip())
        if match is not None:
            statuses.append(int(match.group(1)))
    return statuses[-1] if statuses else None


def _compatibility_framing_mode(
    headers: dict[str, list[str]], body: bytes
) -> str | None:
    content_lengths = headers.get("content-length", [])
    transfer_encodings = headers.get("transfer-encoding", [])
    if bool(content_lengths) == bool(transfer_encodings):
        return None
    if content_lengths:
        if (
            len(content_lengths) == 1
            and re.fullmatch(r"[0-9]+", content_lengths[0]) is not None
            and int(content_lengths[0]) == len(body)
        ):
            return "content-length"
        return None
    if (
        len(transfer_encodings) == 1
        and [token.strip().casefold() for token in transfer_encodings[0].split(",")]
        == ["chunked"]
    ):
        return "chunked"
    return None


def _compatibility_has_cache_directive(
    headers: dict[str, list[str]], directive: str
) -> bool:
    wanted = directive.casefold()
    return any(
        part.split("=", 1)[0].strip().casefold() == wanted
        for value in headers.get("cache-control", [])
        for part in value.split(",")
    )


def _compatibility_assets(text: str) -> Counter[str]:
    parser = _CompatibilityShellAssetParser()
    parser.feed(text)
    parser.close()
    return parser.assets


def reconstruct_compatibility_cache_evidence(
    captures: dict[str, bytes],
    result: dict[str, Any],
    expectation: str,
    matrix_id: str,
) -> dict[str, Any]:
    """Rebuild cache claims solely from the retained wire headers and bodies."""
    require(
        set(captures) == set(COMPAT_RAW_HTTP_FILES),
        f"{matrix_id}: compatibility raw HTTP inventory differs",
    )
    primary_body = captures["shell-after.html"]
    conditional_body = captures["conditional.html"]
    primary_headers = _compatibility_headers(
        captures["shell-after.headers"], f"{matrix_id}/shell-after.headers"
    )
    conditional_headers = _compatibility_headers(
        captures["conditional.headers"], f"{matrix_id}/conditional.headers"
    )
    primary_status = _compatibility_http_status(
        captures["shell-after.headers"], f"{matrix_id}/shell-after.headers"
    )
    conditional_http_status = _compatibility_http_status(
        captures["conditional.headers"], f"{matrix_id}/conditional.headers"
    )
    conditional_status = _compatibility_text(
        captures["conditional-status.txt"],
        f"{matrix_id}/conditional-status.txt",
    ).strip()
    primary_etags = primary_headers.get("etag", [])
    expected_etag = f'"rk-{hashlib.sha256(primary_body).hexdigest()}"'
    required_checks = {
        "primaryStatus200": primary_status == 200,
        "validFraming": _compatibility_framing_mode(
            primary_headers, primary_body
        )
        is not None,
        "singleStrongEtag": len(primary_etags) == 1
        and re.fullmatch(r'"rk-[0-9a-f]{64}"', primary_etags[0]) is not None,
        "etagMatchesBodySha256": primary_etags == [expected_etag],
        "lastModifiedAbsent": not primary_headers.get("last-modified"),
        "conditionalStatus304": conditional_status == "304"
        and conditional_http_status == 304,
        "conditionalBodyEmpty": len(conditional_body) == 0,
        "conditionalEtagMatches": conditional_headers.get("etag", [])
        == [expected_etag],
        "conditionalFramingBodyless": not any(
            conditional_headers.get(name)
            for name in (
                "content-length",
                "transfer-encoding",
                "content-encoding",
            )
        ),
        "conditionalLastModifiedAbsent": not conditional_headers.get(
            "last-modified"
        ),
    }
    refresh_kit = result.get("refreshKit")
    generation = (
        refresh_kit.get("generationAfter")
        if isinstance(refresh_kit, dict)
        else None
    )
    primary_text = _compatibility_text(
        primary_body, f"{matrix_id}/shell-after.html"
    )
    conditional_text = _compatibility_text(
        conditional_body, f"{matrix_id}/conditional.html"
    )
    safe_degrade_checks = {
        "primaryStatus200": primary_status == 200,
        "primaryFramingValid": _compatibility_framing_mode(
            primary_headers, primary_body
        )
        is not None,
        "primaryCacheControlNoStore": _compatibility_has_cache_directive(
            primary_headers, "no-store"
        ),
        "primaryEtagAbsent": not primary_headers.get("etag"),
        "primaryLastModifiedAbsent": not primary_headers.get("last-modified"),
        "conditionalStatus200": conditional_status == "200"
        and conditional_http_status == 200,
        "conditionalFramingValid": _compatibility_framing_mode(
            conditional_headers, conditional_body
        )
        is not None,
        "conditionalCacheControlNoStore": _compatibility_has_cache_directive(
            conditional_headers, "no-store"
        ),
        "conditionalEtagAbsent": not conditional_headers.get("etag"),
        "conditionalLastModifiedAbsent": not conditional_headers.get(
            "last-modified"
        ),
        "conditionalBodyNonEmpty": len(conditional_body) > 0,
        "conditionalHtmlDocument": "<html" in conditional_text.casefold()
        and "</html>" in conditional_text.casefold(),
        "conditionalSingleRefreshKitTag": conditional_text.count(
            'plugin="Jellyfin Refresh Kit"'
        )
        == 1,
        "conditionalNamedRuntime": 'data-name="RefreshKitPlugin"'
        in conditional_text,
        "conditionalBootGeneration": isinstance(generation, str)
        and f'data-boot-version="{generation}"' in conditional_text,
        "conditionalGenerationAddressedKit": isinstance(generation, str)
        and f"/RefreshKit/kit.js?v={generation}" in conditional_text,
        "conditionalAssetMultisetMatchesPrimary": _compatibility_assets(
            conditional_text
        )
        == _compatibility_assets(primary_text),
    }
    primary_framing = _compatibility_framing_mode(primary_headers, primary_body)
    conditional_framing = (
        "bodyless-304"
        if required_checks["conditionalStatus304"]
        and required_checks["conditionalBodyEmpty"]
        and required_checks["conditionalFramingBodyless"]
        else _compatibility_framing_mode(conditional_headers, conditional_body)
    )
    safe_mode = expectation == "safe-degrade"
    return {
        "expectation": expectation,
        "primary": {
            "status": primary_status,
            "bodyBytes": len(primary_body),
            "bodySha256": hashlib.sha256(primary_body).hexdigest(),
            "framingMode": primary_framing,
        },
        "conditional": {
            "status": conditional_http_status,
            "bodyBytes": len(conditional_body),
            "bodySha256": hashlib.sha256(conditional_body).hexdigest(),
            "framingMode": conditional_framing,
        },
        "requiredChecks": required_checks if expectation == "required" else {},
        "safeDegradeChecks": safe_degrade_checks if safe_mode else {},
    }


def reconstruct_webroot_disk(
    before: bytes,
    after: bytes,
    requirements: dict[str, Any],
    label: str,
) -> dict[str, Any]:
    """Rebuild the analyzer's complete direct-webroot result from retained bytes."""
    require(bool(before), f"{label}: raw webroot-before.html is empty")
    require(bool(after), f"{label}: raw webroot-after.html is empty")
    try:
        before_text = before.decode("utf-8-sig", errors="strict")
        after_text = after.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise EvidenceValidationError(
            f"{label}: raw webroot evidence is not strict UTF-8: {error}"
        ) from error

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


def checksum_inventory(root: pathlib.Path) -> dict[str, str]:
    """Require a direct checksum manifest covering every retained file.

    A combined release bundle intentionally retains each split worker's own
    checksum manifest below ``workers/``.  Those nested manifests are ordinary
    files in the outer inventory; only the checksum root being validated must
    be direct.
    """
    root = root.resolve(strict=True)
    checksum_path = root / "SHA256SUMS"
    require(checksum_path.is_file(), f"evidence root has no direct SHA256SUMS: {root}")
    expected: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\n]+)", line)
        require(match is not None, "evidence checksum manifest is malformed")
        assert match is not None
        name = match.group(2)
        pure = pathlib.PurePosixPath(name)
        require(
            not pure.is_absolute()
            and bool(pure.parts)
            and all(part not in ("", ".", "..") for part in pure.parts),
            f"unsafe evidence checksum path: {name!r}",
        )
        require(name not in expected, f"duplicate evidence checksum path: {name}")
        expected[name] = match.group(1)
    actual: set[str] = set()
    for path in root.rglob("*"):
        require(not path.is_symlink(), f"evidence contains a symlink: {path}")
        if path.is_file() and path != checksum_path:
            actual.add(path.relative_to(root).as_posix())
    require(set(expected) == actual, "evidence checksum inventory is incomplete or has extras")
    for name, digest in expected.items():
        require(file_hash(root / name) == digest, f"evidence checksum mismatch: {name}")
    return expected


def write_checksum_inventory(root: pathlib.Path) -> None:
    root = root.resolve(strict=True)
    checksum_path = root / "SHA256SUMS"
    rows = [
        f"{file_hash(path)}  {path.relative_to(root).as_posix()}"
        for path in sorted(root.rglob("*"))
        if path.is_file() and path != checksum_path
    ]
    checksum_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def package_identity(build: pathlib.Path) -> dict[str, Any]:
    build = build.resolve(strict=True)
    stage_meta = {
        name: load_object(build / name / "meta.json") for name in ("stage", "stage-jf12")
    }
    version = stage_meta["stage"].get("version")
    require(
        isinstance(version, str)
        and version == stage_meta["stage-jf12"].get("version")
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", version) is not None,
        "build stages have invalid or different versions",
    )
    names = (
        f"jellyfin-refresh-kit_{version}.zip",
        f"jellyfin-refresh-kit_{version}_jf12.zip",
    )
    packages = []
    for name in names:
        path = build / name
        require(path.is_file(), f"build package is missing: {path}")
        packages.append({"name": name, "bytes": path.stat().st_size, "sha256": file_hash(path)})
    return {
        "available": True,
        "immutableSnapshot": build.name,
        "packages": packages,
        "metadata": stage_meta,
    }


def _lifecycle_error(relative: str, message: str) -> EvidenceValidationError:
    return EvidenceValidationError(f"invalid integration lifecycle evidence {relative}: {message}")


def validate_lifecycle(relative: str, value: dict[str, Any], build: pathlib.Path) -> None:
    parts = pathlib.PurePosixPath(relative).parts
    require(len(parts) == 3, f"invalid lifecycle path: {relative}")
    target, kind_dir, _ = parts
    kind = "self" if kind_dir == "lifecycle" else "third-party"
    if value.get("target") != target:
        raise _lifecycle_error(relative, f"target is {value.get('target')!r}, expected {target!r}")
    for field, wanted in (
        ("completed", True),
        ("failures", []),
        ("unexpectedRefreshKitBrowserErrors", []),
        ("repositoryConfigurationRemoved", True),
    ):
        if value.get(field) != wanted:
            raise _lifecycle_error(relative, f"{field} is {value.get(field)!r}, expected {wanted!r}")
    if kind == "third-party" and value.get("versionFlapWarnings") != []:
        raise _lifecycle_error(relative, "versionFlapWarnings is not an empty array")
    phases = value.get("phases")
    require(isinstance(phases, list), str(_lifecycle_error(relative, "phases is not an array")))
    phase_names = [row.get("name") for row in phases if isinstance(row, dict)]
    wanted_phases = SELF_LIFECYCLE_PHASES if kind == "self" else THIRD_PARTY_LIFECYCLE_PHASES
    require(
        len(phases) == len(wanted_phases)
        and set(phase_names) == wanted_phases
        and len(phase_names) == len(wanted_phases),
        str(_lifecycle_error(relative, "required phases are missing, duplicated, or unexpected")),
    )
    captures = value.get("captureCounts")
    capture_names = {"primary", "secondary", "background"}
    require(
        isinstance(captures, dict)
        and set(captures) == capture_names
        and all(
            isinstance(captures[name], dict)
            and captures[name].get("truncated") is False
            for name in capture_names
        ),
        str(_lifecycle_error(relative, "browser captures are missing or truncated")),
    )
    metadata = value.get("metadata")
    require(isinstance(metadata, dict), str(_lifecycle_error(relative, "metadata is not an object")))
    require(metadata.get("target") == target, str(_lifecycle_error(relative, "metadata target differs")))
    stage_name = "stage" if target == "jf10" else "stage-jf12"
    stage = load_object(build / stage_name / "meta.json")
    if kind == "self":
        suffix = "" if target == "jf10" else "_jf12"
        package = build / f"jellyfin-refresh-kit_{stage['version']}{suffix}.zip"
        expected = {
            "candidateVersion": stage.get("version"),
            "embeddedCandidateVersion": stage.get("version"),
            "candidateSourceRevision": stage.get("sourceRevision"),
            "candidateSourceTreeSha256": stage.get("sourceTreeSha256"),
            "candidateMd5": file_hash(package, "md5"),
        }
    else:
        expected = {
            "refreshKitSnapshot": build.name,
            "refreshKitPackageVersion": stage.get("version"),
            "refreshKitSourceRevision": stage.get("sourceRevision"),
            "refreshKitSourceTreeSha256": stage.get("sourceTreeSha256"),
        }
    for field, wanted in expected.items():
        if metadata.get(field) != wanted:
            raise _lifecycle_error(
                relative, f"metadata {field} is {metadata.get(field)!r}, expected {wanted!r}"
            )


def _field(value: dict[str, Any], name: str) -> Any:
    return value.get(name, value.get(name[:1].lower() + name[1:]))


def validate_server(
    target: str,
    result: dict[str, Any],
    diagnostics: dict[str, Any],
    build: pathlib.Path,
) -> None:
    label = f"{target}/server"
    require(result.get("target") == target, f"{label}: target differs")
    require(result.get("serverVersion") == SERVER_VERSIONS[target], f"{label}: server version differs")
    generation = result.get("generation")
    kit_version = result.get("kitVersion")
    require(isinstance(generation, str) and GENERATION.fullmatch(generation) is not None,
            f"{label}: generation is invalid")
    require(isinstance(kit_version, str) and bool(kit_version), f"{label}: kit version is missing")
    require(result.get("conditionalStatus") == 304, f"{label}: conditional shell status is not 304")
    require(result.get("endpointAndShellChecksPassed") is True, f"{label}: endpoint/shell checks did not pass")
    require(isinstance(result.get("shellEtag"), str) and "rk-" in result["shellEtag"],
            f"{label}: shell ETag is not Refresh Kit-owned")
    require(_field(diagnostics, "Generation") == generation, f"{label}: diagnostics generation differs")
    require(_field(diagnostics, "KitVersion") == kit_version, f"{label}: diagnostics kit version differs")
    stage_name = "stage" if target == "jf10" else "stage-jf12"
    version = load_object(build / stage_name / "meta.json").get("version")
    require(_field(diagnostics, "PluginVersion") == version,
            f"{label}: diagnostics package version differs")
    plugins = _field(diagnostics, "Plugins")
    require(isinstance(plugins, list), f"{label}: diagnostics plugin list is missing")
    matches = [
        row for row in plugins
        if isinstance(row, dict)
        and str(_field(row, "Id") or "").lower() == PLUGIN_GUID
    ]
    require(len(matches) == 1, f"{label}: diagnostics Refresh Kit inventory is not exact")
    plugin = matches[0]
    require(_field(plugin, "Version") == version
            and str(_field(plugin, "Status") or "").lower() == "active"
            and _field(plugin, "IsLoaded") is True,
            f"{label}: diagnostics Refresh Kit record is not active/loaded candidate")


def validate_browser(
    target: str,
    result: dict[str, Any],
    expected_generation: str,
) -> None:
    label = f"{target}/browser"
    require(result.get("target") == target, f"{label}: target differs")
    require(result.get("failures") == [], f"{label}: failures are present")
    before, after = result.get("generationBefore"), result.get("generationAfter")
    require(isinstance(before, str) and GENERATION.fullmatch(before) is not None and before == after,
            f"{label}: restart generation evidence differs")
    require(before == expected_generation, f"{label}: browser/server generation differs")
    restart = result.get("restartWindow")
    require(isinstance(restart, dict) and restart.get("recoveryIncomplete") is False,
            f"{label}: restart recovery is incomplete")
    times = [restart.get(name) for name in (
        "startElapsedMs", "serverHealthyElapsedMs", "recoveryCompletedElapsedMs"
    )]
    require(all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in times)
            and times[0] <= times[1] <= times[2],
            f"{label}: restart timing proof differs")
    tabs: dict[str, dict[str, dict[str, Any]]] = {}
    wanted_tabs = {"primary", "secondary", "background"}
    for field in ("preRestart", "postRestart"):
        rows = result.get(field)
        require(isinstance(rows, list) and len(rows) == len(wanted_tabs)
                and all(isinstance(row, dict) for row in rows),
                f"{label}: {field} tab inventory differs")
        indexed = {str(row.get("name")): row for row in rows}
        require(set(indexed) == wanted_tabs and len(indexed) == len(rows),
                f"{label}: {field} tab names differ")
        tabs[field] = indexed
    for name in wanted_tabs:
        before_tab, after_tab = tabs["preRestart"][name], tabs["postRestart"][name]
        document = before_tab.get("documentId")
        require(before_tab.get("authenticated") is True and after_tab.get("authenticated") is True
                and isinstance(document, str) and bool(document)
                and after_tab.get("documentId") == document,
                f"{label}: {name} did not remain authenticated in the same document")
        for phase, row in (("before", before_tab), ("after", after_tab)):
            kit = row.get("kit")
            require(isinstance(kit, dict)
                    and kit.get("version") == expected_generation
                    and kit.get("latestVersion") == expected_generation,
                    f"{label}: {name} {phase} kit generation differs")
    hidden = result.get("hiddenAtRestart")
    expected_hidden = {
        name for name, row in tabs["preRestart"].items() if row.get("visibility") == "hidden"
    }
    require(isinstance(hidden, list) and bool(expected_hidden)
            and set(hidden) == expected_hidden and len(hidden) == len(expected_hidden),
            f"{label}: hidden-tab restart proof differs")
    reconnect = result.get("websocketReconnect")
    require(isinstance(reconnect, dict) and set(reconnect) == {"primary", "secondary", "background"}
            and all(value is True for value in reconnect.values()),
            f"{label}: websocket reconnection proof is incomplete")
    captures = result.get("captureCounts")
    require(isinstance(captures, dict) and set(captures) == {"primary", "secondary", "background"}
            and all(isinstance(row, dict) and row.get("truncated") is False for row in captures.values()),
            f"{label}: browser captures are missing or truncated")
    audit = result.get("errorAudit")
    expected_buckets = {
        "restartWindow",
        "restartTransportNoise",
        "allowlistedHostErrors",
        "unexpectedRefreshKitErrors",
        "otherHostOrUnattributedErrors",
    }
    require(isinstance(audit, dict) and set(audit) == expected_buckets,
            f"{label}: browser error audit inventory differs")
    require(audit.get("restartWindow") == restart,
            f"{label}: browser error audit restart window differs")
    require(all(isinstance(audit[name], dict) and audit[name].get("truncated") is False
                for name in expected_buckets - {"restartWindow"}),
            f"{label}: browser error audit is truncated")
    require(audit["unexpectedRefreshKitErrors"].get("count") == 0,
            f"{label}: unexpected Refresh Kit browser errors are present")
    navigation = result.get("navigation")
    configuration = navigation.get("configuration") if isinstance(navigation, dict) else None
    require(isinstance(configuration, dict) and configuration.get("initialized") is True
            and configuration.get("failedControllerResponses") == []
            and configuration.get("controllerImportErrors") == []
            and configuration.get("generation") == expected_generation,
            f"{label}: configuration-page proof is incomplete")


def validate_cross_compatibility(
    result: dict[str, Any],
    generation: dict[str, Any],
    plugin_record: dict[str, Any],
    public: dict[str, Any],
    build: pathlib.Path,
) -> None:
    expected_http = {"public": 200, "generation": 200, "kit": 200, "shell": 200}
    require(result.get("experiment") == "net9/Jellyfin-10 build manually installed on Jellyfin 12",
            "cross-generation experiment identity differs")
    require(result.get("hostImageDigest") == IMAGE_DIGESTS["jf12"],
            "cross-generation host image digest differs")
    require(result.get("stageFramework") == "net9.0"
            and result.get("stageTargetAbi") == "10.11.0.0",
            "cross-generation stage identity differs")
    require(result.get("provisionExitCode") == 0 and result.get("http") == expected_http,
            "cross-generation provisioning/HTTP proof differs")
    require(result.get("expected") == "load" and result.get("loaded") is True
            and result.get("shellInjected") is True
            and result.get("pluginInventoryFound") is True
            and isinstance(result.get("pluginInventoryStatus"), str)
            and bool(result["pluginInventoryStatus"]),
            "cross-generation load/inventory proof is incomplete")
    stage = load_object(build / "stage" / "meta.json")
    version = stage.get("version")
    cache_key = _field(generation, "CacheKey")
    require(stage.get("framework") == "net9.0" and stage.get("targetAbi") == "10.11.0.0",
            "cross-generation final net9 stage identity differs")
    require(_field(generation, "Version") == version
            and isinstance(cache_key, str) and GENERATION.fullmatch(cache_key) is not None
            and isinstance(_field(generation, "BuildId"), str)
            and bool(_field(generation, "BuildId"))
            and isinstance(_field(generation, "Epoch"), str)
            and bool(_field(generation, "Epoch")),
            "cross-generation generation endpoint is incomplete or stale")
    require(_field(public, "Version") == SERVER_VERSIONS["jf12"],
            "cross-generation public host version differs")
    require(str(_field(plugin_record, "Id") or "").lower() == PLUGIN_GUID
            and _field(plugin_record, "Version") == version
            and _field(plugin_record, "Status") == result.get("pluginInventoryStatus"),
            "cross-generation plugin inventory record differs")


def validate_integration_tree(root: pathlib.Path, build: pathlib.Path) -> set[str]:
    """Validate the canonical collected integration tree and exact inventory."""
    root, build = root.resolve(strict=True), build.resolve(strict=True)
    lab = root / "lab"
    logs = root / "logs"
    expected_lab = {
        pathlib.PurePosixPath(name).as_posix()
        for name in (*INTEGRATION_JSON, *INTEGRATION_TEXT, *INTEGRATION_IMAGES)
    }
    actual_lab = {
        path.relative_to(lab).as_posix() for path in lab.rglob("*") if path.is_file()
    } if lab.is_dir() else set()
    require(actual_lab == expected_lab,
            f"integration lab inventory differs: missing={sorted(expected_lab-actual_lab)}, "
            f"unexpected={sorted(actual_lab-expected_lab)}")
    expected_logs = {f"{name}.log" for name in INTEGRATION_LOGS}
    actual_logs = {path.name for path in logs.iterdir() if path.is_file()} if logs.is_dir() else set()
    require(actual_logs == expected_logs, "integration log inventory is not exact")
    for name in expected_logs:
        require((logs / name).stat().st_size > 0, f"integration log is empty: {name}")

    for target in ("jf10", "jf12"):
        server = load_object(lab / target / "server" / "result.json")
        validate_server(
            target,
            server,
            load_object(lab / target / "server" / "diagnostics.json"),
            build,
        )
        validate_browser(
            target,
            load_object(lab / target / "browser" / "result.json"),
            str(server["generation"]),
        )
        validate_lifecycle(
            f"{target}/lifecycle/result.json",
            load_object(lab / target / "lifecycle" / "result.json"),
            build,
        )
        validate_lifecycle(
            f"{target}/third-party-lifecycle/result.json",
            load_object(lab / target / "third-party-lifecycle" / "result.json"),
            build,
        )
    cross = lab / "compat-jf10-on-jf12"
    validate_cross_compatibility(
        load_object(cross / "result.json"),
        load_object(cross / "generation.json"),
        load_object(cross / "plugin-record.json"),
        load_object(cross / "public.json"),
        build,
    )
    try:
        validate_abi_floor_evidence(lab / "abi-floor", build)
    except AbiFloorEvidenceError as error:
        raise EvidenceValidationError(str(error)) from error
    try:
        validate_host_upgrade_evidence(lab / "host-upgrade", build)
    except HostUpgradeEvidenceError as error:
        raise EvidenceValidationError(str(error)) from error
    return {f"lab/{name}" for name in expected_lab} | {f"logs/{name}" for name in expected_logs}


def _matrix_contract(
    matrices_path: pathlib.Path,
) -> tuple[
    list[str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, list[str]],
    dict[str, dict[str, Any]],
    dict[str, str | None],
    dict[str, list[str] | None],
    dict[str, list[str]],
]:
    root = load_object(matrices_path)
    rows = root.get("matrices")
    require(isinstance(rows, list), "compatibility matrices has no array")
    ids: list[str] = []
    runtimes: dict[str, str] = {}
    services: dict[str, str] = {}
    cache: dict[str, str] = {}
    limitations: dict[str, list[str]] = {}
    webroot_disk: dict[str, dict[str, Any]] = {}
    order_pairs: dict[str, str | None] = {}
    runtime_orders: dict[str, list[str] | None] = {}
    install_orders: dict[str, list[str]] = {}
    for row in rows:
        require(isinstance(row, dict), "compatibility matrix row is not an object")
        matrix_id = row.get("id")
        runtime = row.get("runtime")
        service = row.get("service")
        expectation = row.get("cacheExpectation")
        require(isinstance(matrix_id, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9-]*", matrix_id) is not None
                and matrix_id not in runtimes,
                f"invalid/repeated compatibility matrix id: {matrix_id!r}")
        require(runtime in ("jf10", "jf12"),
                f"compatibility matrix {matrix_id} runtime is invalid")
        require(isinstance(service, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9-]*", service) is not None,
                f"compatibility matrix {matrix_id} service is invalid")
        require(expectation in ("required", "observe", "safe-degrade"),
                f"compatibility matrix {matrix_id} cache expectation is invalid")
        artifacts = row.get("requiredUnversionedOuterArtifacts", [])
        require(isinstance(artifacts, list)
                and all(isinstance(item, str) for item in artifacts),
                f"compatibility matrix {matrix_id} limitations are invalid")
        disk_requirements = row.get("webrootDiskRequirements")
        require(isinstance(disk_requirements, dict),
                f"compatibility matrix {matrix_id} disk requirements are invalid")
        for artifact_id, requirement in disk_requirements.items():
            require(isinstance(artifact_id, str) and bool(artifact_id)
                    and isinstance(requirement, dict)
                    and set(requirement) == {"mode", "cardinality", "markers"},
                    f"compatibility matrix {matrix_id} disk requirement is invalid")
            mode = requirement.get("mode")
            cardinality = requirement.get("cardinality")
            markers = requirement.get("markers")
            require(mode in ("absent", "added")
                    and type(cardinality) is int
                    and cardinality == (0 if mode == "absent" else 1)
                    and isinstance(markers, list) and bool(markers)
                    and all(isinstance(marker, str) and bool(marker) for marker in markers)
                    and len(markers) == len(set(markers)),
                    f"compatibility matrix {matrix_id}/{artifact_id} disk requirement is invalid")
        order_pair = row.get("orderPair")
        runtime_order = row.get("expectedRuntimePluginOrder")
        install_order = row.get("installOrder")
        require(
            isinstance(install_order, list)
            and bool(install_order)
            and all(isinstance(item, str) and bool(item) for item in install_order)
            and len(install_order) == len(set(install_order))
            and install_order.count("@refresh-kit") == 1,
            f"compatibility matrix {matrix_id} install order is invalid",
        )
        require(
            (order_pair is None and runtime_order is None)
            or (
                isinstance(order_pair, str)
                and re.fullmatch(r"[a-z0-9][a-z0-9-]*", order_pair) is not None
                and isinstance(runtime_order, list)
                and bool(runtime_order)
                and all(isinstance(item, str) and bool(item) for item in runtime_order)
                and len(runtime_order) == len(set(runtime_order))
            ),
            f"compatibility matrix {matrix_id} runtime-order contract is invalid",
        )
        ids.append(matrix_id)
        runtimes[matrix_id] = runtime
        services[matrix_id] = service
        cache[matrix_id] = expectation
        order_pairs[matrix_id] = order_pair
        runtime_orders[matrix_id] = runtime_order
        install_orders[matrix_id] = install_order
        if artifacts:
            limitations[matrix_id] = artifacts
        if disk_requirements:
            webroot_disk[matrix_id] = disk_requirements
    pair_members: dict[str, list[str]] = {}
    for matrix_id, order_pair in order_pairs.items():
        if order_pair is not None:
            pair_members.setdefault(order_pair, []).append(matrix_id)
    for order_pair, members in pair_members.items():
        require(
            len(members) == 2
            and runtime_orders[members[0]] == runtime_orders[members[1]]
            and runtimes[members[0]] == runtimes[members[1]]
            and cache[members[0]] == cache[members[1]],
            f"compatibility order pair {order_pair} contract is invalid",
        )
    return (
        ids,
        runtimes,
        services,
        cache,
        limitations,
        webroot_disk,
        order_pairs,
        runtime_orders,
        install_orders,
    )


def validate_compatibility_tree(
    root: pathlib.Path, build: pathlib.Path, matrices_path: pathlib.Path
) -> set[str]:
    root, build = root.resolve(strict=True), build.resolve(strict=True)
    compat = root / "compat"
    (
        ids,
        runtimes,
        services,
        cache_expectations,
        limitations,
        webroot_disk_requirements,
        order_pairs,
        runtime_orders,
        install_orders,
    ) = _matrix_contract(matrices_path)
    expected = {"summary.json", "all-locked-verification.json"} | {
        f"{matrix_id}/{name}" for matrix_id in ids for name in COMPAT_MATRIX_FILES
    }
    expected |= {
        f"{matrix_id}/{name}" for matrix_id in ids for name in COMPAT_RAW_HTTP_FILES
    }
    expected |= {
        f"{matrix_id}/{name}"
        for matrix_id in webroot_disk_requirements
        for name in COMPAT_WEBROOT_FILES
    }
    actual = {
        path.relative_to(compat).as_posix() for path in compat.rglob("*") if path.is_file()
    } if compat.is_dir() else set()
    require(actual == expected,
            f"compatibility inventory differs: missing={sorted(expected-actual)}, unexpected={sorted(actual-expected)}")
    safe_degraded = [matrix_id for matrix_id in ids if cache_expectations[matrix_id] == "safe-degrade"]
    limited = [matrix_id for matrix_id in ids if matrix_id in limitations]
    expected_pair_checks = {
        order_pair: True
        for order_pair in sorted(
            {value for value in order_pairs.values() if value is not None}
        )
    }
    summary = load_object(compat / "summary.json")
    lock_path = matrices_path.with_name("ecosystem.lock.json")
    locked_artifacts = load_compatibility_artifact_lock(lock_path)
    all_locked_path = compat / "all-locked-verification.json"
    all_locked = load_object(all_locked_path)
    locked_ids = [row["id"] for row in locked_artifacts]
    all_locked_rows = validate_artifact_verification_report(
        all_locked,
        locked_artifacts,
        locked_ids,
        "all-locked-verification.json",
    )
    all_locked_by_id = {row["id"]: row for row in all_locked_rows}
    testable_locked_ids = {
        row["id"] for row in locked_artifacts if row["disposition"] == "testable"
    }
    matrix_artifact_ids = {
        artifact_id
        for install_order in install_orders.values()
        for artifact_id in install_order
        if artifact_id != "@refresh-kit"
    }
    require(
        matrix_artifact_ids == testable_locked_ids,
        "compatibility matrices do not cover exactly every testable locked artifact",
    )
    expected_all_locked_summary = artifact_verification_summary(
        all_locked_path, all_locked
    )
    require(summary.get("schemaVersion") == 1, "compatibility summary schema differs")
    summary_expected = {
        "outcome": "pass-with-limitation" if limited else "pass",
        "expectedMatrices": ids,
        "completedMatrices": ids,
        "missingMatrices": [],
        "failedMatrices": [],
        "expectedSafeDegradedMatrices": safe_degraded,
        "safeDegradedMatrices": safe_degraded,
        "missingSafeDegradedMatrices": [],
        "expectedPassWithLimitationMatrices": limited,
        "passWithLimitationMatrices": limited,
        "missingPassWithLimitationMatrices": [],
        "unexpectedPassWithLimitationMatrices": [],
        "pairRuntimeOrderChecks": expected_pair_checks,
        "allLockedVerification": expected_all_locked_summary,
    }
    for field, wanted in summary_expected.items():
        require(
            field in summary and exact_json_value(summary[field], wanted),
            f"compatibility summary {field} differs",
        )

    retained_results: list[dict[str, Any]] = []
    for matrix_id in ids:
        directory = compat / matrix_id
        static = load_object(directory / "static.json")
        stage = load_object(directory / "stage.json")
        network = load_object(directory / "network.json")
        artifact = load_object(directory / "artifact-verification.json")
        result = load_object(directory / "result.json")
        retained_results.append(result)
        require(static.get("schemaVersion") == 1 and static.get("allPassed") is True,
                f"{matrix_id}: static evidence failed")
        require(network.get("schemaVersion") == 1
                and network.get("valid") is True and network.get("allPassed") is True,
                f"{matrix_id}: network evidence failed")
        expected_artifact_ids = [
            artifact_id
            for artifact_id in install_orders[matrix_id]
            if artifact_id != "@refresh-kit"
        ]
        artifact_rows = validate_artifact_verification_report(
            artifact,
            locked_artifacts,
            expected_artifact_ids,
            f"{matrix_id}/artifact-verification.json",
        )
        require(
            all(
                exact_json_value(row, all_locked_by_id[row["id"]])
                for row in artifact_rows
            ),
            f"{matrix_id}: matrix artifact receipt differs from all-locked verification",
        )
        runtime = runtimes[matrix_id]
        stage_name = "stage" if runtime == "jf10" else "stage-jf12"
        expected_stage = build / stage_name
        recorded_stage = pathlib.Path(str(stage.get("stage", "")))
        require(stage.get("schemaVersion") == 1
                and stage.get("valid") is True and stage.get("runtime") == runtime,
                f"{matrix_id}: stage validity/runtime differs")
        require(recorded_stage.name == stage_name and recorded_stage.parent.name == build.name,
                f"{matrix_id}: recorded stage does not name the immutable snapshot")
        require(stage.get("meta") == load_object(expected_stage / "meta.json"),
                f"{matrix_id}: stage metadata differs from final build")
        require(stage.get("dllSha256") == file_hash(expected_stage / "Jellyfin.Plugin.RefreshKit.dll"),
                f"{matrix_id}: stage DLL differs from final build")
        expected_limitations = limitations.get(matrix_id, [])
        expected_outcome = "pass-with-limitation" if expected_limitations else "pass"
        require(result.get("schemaVersion") == 1
                and result.get("matrix") == matrix_id
                and result.get("runtime") == runtime
                and result.get("service") == services[matrix_id]
                and result.get("serverVersion") == SERVER_VERSIONS[runtime]
                and result.get("image") == IMAGE_REFERENCES[runtime]
                and result.get("errors") == []
                and result.get("outcome") == expected_outcome,
                f"{matrix_id}: result outcome differs")
        require(network.get("service") == services[matrix_id]
                and network.get("configuredImage") == IMAGE_REFERENCES[runtime]
                and network.get("expectedImageDigest") == IMAGE_DIGESTS[runtime]
                and network.get("internalBridge") is True
                and network.get("noGateway") is True
                and network.get("originMode") == "verified-internal-bridge"
                and network.get("publishedLoopbackActive") is False,
                f"{matrix_id}: network isolation identity differs")
        require(result.get("cacheExpectation") == cache_expectations[matrix_id],
                f"{matrix_id}: result cache expectation differs")
        require(
            "requestedInstallOrder" in result
            and exact_json_value(
                result["requestedInstallOrder"], install_orders[matrix_id]
            ),
            f"{matrix_id}: requested install order differs from the matrix",
        )
        plugin_rows = result.get("plugins")
        require(
            isinstance(plugin_rows, list)
            and [
                row.get("artifactId") if isinstance(row, dict) else None
                for row in plugin_rows
            ]
            == expected_artifact_ids
            and all(
                isinstance(plugin_row, dict)
                and "artifactVerification" in plugin_row
                and exact_json_value(
                    plugin_row["artifactVerification"], artifact_row
                )
                and isinstance(plugin_row.get("materialization"), dict)
                and plugin_row["materialization"].get("materialized") is True
                and plugin_row["materialization"].get("archiveSha256")
                == artifact_row["archive"]["sha256"]
                and plugin_row["materialization"].get("assemblySha256")
                == artifact_row["plugin"]["assemblySha256"]
                and "dllInventory" in plugin_row["materialization"]
                and exact_json_value(
                    plugin_row["materialization"]["dllInventory"],
                    artifact_row["plugin"]["managedDlls"],
                )
                for plugin_row, artifact_row in zip(
                    plugin_rows, artifact_rows, strict=True
                )
            ),
            f"{matrix_id}: result plugins differ from the verified artifact projection",
        )
        expected_runtime_order = runtime_orders[matrix_id]
        require(
            "orderPair" in result
            and "expectedRuntimePluginOrder" in result
            and "observedRuntimePluginOrder" in result
            and exact_json_value(result["orderPair"], order_pairs[matrix_id])
            and exact_json_value(
                result["expectedRuntimePluginOrder"], expected_runtime_order
            )
            and exact_json_value(
                result["observedRuntimePluginOrder"], expected_runtime_order
            ),
            f"{matrix_id}: runtime plugin order proof differs",
        )
        rows = result.get("limitations")
        require(isinstance(rows, list) and [row.get("artifactId") for row in rows if isinstance(row, dict)]
                == expected_limitations and len(rows) == len(expected_limitations),
                f"{matrix_id}: limitations differ")
        require(all(isinstance(row, dict)
                    and row.get("code") == "outer-owner-unversioned-shell-tag"
                    and row.get("status") == "pass-with-limitation"
                    and isinstance(row.get("checks"), dict) and bool(row["checks"])
                    and all(value is True for value in row["checks"].values()) for row in rows),
                f"{matrix_id}: limitation proof is incomplete")
        requirements = webroot_disk_requirements.get(matrix_id)
        if requirements is None:
            require("webrootDisk" in result and result["webrootDisk"] is None,
                    f"{matrix_id}: nondisk result webrootDisk must be explicit null")
        else:
            before = (directory / "webroot-before.html").read_bytes()
            after = (directory / "webroot-after.html").read_bytes()
            reconstructed = reconstruct_webroot_disk(
                before, after, requirements, matrix_id
            )
            require(reconstructed["allPassed"] is True,
                    f"{matrix_id}: raw webroot marker/document/change contract failed")
            require("webrootDisk" in result
                    and exact_json_value(result["webrootDisk"], reconstructed),
                    f"{matrix_id}: result webrootDisk differs from retained raw bytes")
        captures = {
            name: (directory / name).read_bytes() for name in COMPAT_RAW_HTTP_FILES
        }
        reconstructed_cache = reconstruct_compatibility_cache_evidence(
            captures, result, cache_expectations[matrix_id], matrix_id
        )
        refresh = result.get("refreshKit")
        cache = refresh.get("cacheEvidence") if isinstance(refresh, dict) else None
        require(
            isinstance(cache, dict)
            and all(
                field in cache
                and exact_json_value(cache[field], reconstructed_cache[field])
                for field in (
                    "expectation",
                    "primary",
                    "conditional",
                    "requiredChecks",
                    "safeDegradeChecks",
                )
            ),
            f"{matrix_id}: cache evidence differs from retained HTTP bytes",
        )
        if cache_expectations[matrix_id] == "required":
            checks = cache.get("requiredChecks") if isinstance(cache, dict) else None
            require(
                isinstance(checks, dict)
                and set(checks) == REQUIRED_CACHE_CHECK_NAMES
                and all(value is True for value in checks.values()),
                f"{matrix_id}: required cache proof is incomplete",
            )
        elif cache_expectations[matrix_id] == "safe-degrade":
            checks = cache.get("safeDegradeChecks") if isinstance(cache, dict) else None
            primary = cache.get("primary") if isinstance(cache, dict) else None
            conditional = cache.get("conditional") if isinstance(cache, dict) else None
            require(isinstance(checks, dict) and set(checks) == SAFE_DEGRADE_CHECK_NAMES
                    and all(value is True for value in checks.values())
                    and isinstance(primary, dict)
                    and primary.get("framingMode") in ("content-length", "chunked")
                    and isinstance(conditional, dict)
                    and conditional.get("framingMode") in ("content-length", "chunked"),
                    f"{matrix_id}: safe-degrade proof is incomplete")
    require(
        "results" in summary
        and exact_json_value(summary["results"], retained_results),
        "compatibility summary results differ from retained matrix results",
    )
    return {f"compat/{name}" for name in expected}
