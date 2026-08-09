#!/usr/bin/env python3
"""Fail-closed artifact lock, downloader, inspector, and materializer."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


HERE = Path(__file__).resolve().parent
COMPAT_ROOT = HERE.parent
DEFAULT_LOCK = COMPAT_ROOT / "ecosystem.lock.json"
DEFAULT_CACHE = COMPAT_ROOT / ".cache" / "artifacts"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$")
SOURCE_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_DISPOSITIONS = {"testable", "quarantined", "unsupported"}
ALLOWED_COVERAGE = {
    "testable",
    "quarantined",
    "unsupported",
    "not-relevant",
    "manual-only",
    "archived",
}
TESTABLE_CLASSIFICATION = "live-web-interacting-testable"
ALLOWED_CLASSIFICATIONS = {
    TESTABLE_CLASSIFICATION,
    "configuration-not-server-plugin",
    "live-web-interacting-upstream-incompatible",
    "retired-stale-web-plugin",
    "stale-resource-route-upstream-incompatible",
    "live-server-plugin-not-relevant",
    "auth-paid-or-manual-not-relevant",
    "client-not-server-plugin",
    "archived-dead-auth-plugin",
}
RELEVANT_CLASSIFICATIONS = {
    TESTABLE_CLASSIFICATION,
    "configuration-not-server-plugin",
    "live-web-interacting-upstream-incompatible",
    "retired-stale-web-plugin",
    "stale-resource-route-upstream-incompatible",
}
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_COUNT = 20_000
MAX_EXPANDED_BYTES = 1024 * 1024 * 1024


class HarnessError(RuntimeError):
    """A deterministic validation failure suitable for a concise CLI error."""


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read JSON {path}: {exc}") from exc


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ci_value(mapping: dict[str, Any], key: str) -> Any:
    wanted = key.casefold()
    for actual, value in mapping.items():
        if actual.casefold() == wanted:
            return value
    return None


def normalized_abi(value: Any) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", text):
        return text + ".0"
    return text


def artifact_index(lock: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in lock["artifacts"]}


def validate_lock(path: Path) -> dict[str, Any]:
    lock = load_json(path)
    if not isinstance(lock, dict) or lock.get("schemaVersion") != 1:
        raise HarnessError("ecosystem lock must be a schemaVersion 1 object")

    artifacts = lock.get("artifacts")
    coverage = lock.get("catalogCoverage")
    expected = lock.get("coverageExpectations")
    if not isinstance(artifacts, list) or not isinstance(coverage, list):
        raise HarnessError("ecosystem lock needs artifacts and catalogCoverage arrays")
    if not isinstance(expected, dict):
        raise HarnessError("ecosystem lock needs coverageExpectations")

    ids: list[str] = []
    archive_urls: list[str] = []
    testable_jf12: list[str] = []
    for item in artifacts:
        if not isinstance(item, dict):
            raise HarnessError("every artifact entry must be an object")
        artifact_id = str(item.get("id", ""))
        ids.append(artifact_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", artifact_id):
            raise HarnessError(f"unsafe artifact id: {artifact_id!r}")
        disposition = item.get("disposition")
        if disposition not in ALLOWED_DISPOSITIONS:
            raise HarnessError(f"{artifact_id}: invalid disposition {disposition!r}")
        runtime = item.get("runtime")
        if disposition == "testable" and runtime not in {"jf10", "jf12"}:
            raise HarnessError(f"{artifact_id}: testable artifact needs a runtime")
        if disposition != "testable" and runtime is not None:
            raise HarnessError(f"{artifact_id}: non-testable artifact must not name a runtime")

        repository = str(item.get("repository", ""))
        parsed_repository = urllib.parse.urlparse(repository)
        if parsed_repository.scheme != "https" or parsed_repository.netloc != "github.com":
            raise HarnessError(f"{artifact_id}: repository must be an https://github.com URL")
        if not SOURCE_REVISION_RE.fullmatch(str(item.get("sourceRevision", ""))):
            raise HarnessError(f"{artifact_id}: sourceRevision must be a lowercase commit SHA")
        if repository == "https://github.com/n00bcodr/Jellyfin-Enhanced":
            if item.get("repositoryAccess") != "strictly-read-only":
                raise HarnessError(f"{artifact_id}: Jellyfin-Enhanced must be marked strictly read-only")

        archive = item.get("archive")
        plugin = item.get("plugin")
        if not isinstance(archive, dict) or not isinstance(plugin, dict):
            raise HarnessError(f"{artifact_id}: missing archive/plugin metadata")
        url = str(archive.get("url", ""))
        parsed_url = urllib.parse.urlparse(url)
        if (
            parsed_url.scheme != "https"
            or parsed_url.netloc != "github.com"
            or "/releases/download/" not in parsed_url.path
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise HarnessError(f"{artifact_id}: archive URL is not a fixed GitHub release URL")
        if Path(parsed_url.path).name != archive.get("name"):
            raise HarnessError(f"{artifact_id}: archive name does not match its URL")
        digest = str(archive.get("sha256", ""))
        if not SHA256_RE.fullmatch(digest):
            raise HarnessError(f"{artifact_id}: archive sha256 is invalid")
        archive_urls.append(url)

        guid = str(plugin.get("guid", ""))
        version = str(plugin.get("version", ""))
        target_abi = str(plugin.get("targetAbi", ""))
        if not GUID_RE.fullmatch(guid):
            raise HarnessError(f"{artifact_id}: plugin GUID is invalid")
        if not VERSION_RE.fullmatch(version):
            raise HarnessError(f"{artifact_id}: plugin version must have four components")
        if not VERSION_RE.fullmatch(target_abi):
            raise HarnessError(f"{artifact_id}: targetAbi must have four components")
        if plugin.get("framework") not in {"net8.0", "net9.0", "net10.0"}:
            raise HarnessError(f"{artifact_id}: unsupported framework declaration")
        if not str(plugin.get("assembly", "")).lower().endswith(".dll"):
            raise HarnessError(f"{artifact_id}: plugin assembly must be a DLL basename")
        if plugin.get("archiveMeta") not in {"upstream", "absent-generate", "absent"}:
            raise HarnessError(f"{artifact_id}: invalid archiveMeta policy")
        needles = item.get("shellUrlNeedles")
        if not isinstance(needles, list) or any(
            not isinstance(needle, str) or needle != needle.casefold() for needle in needles
        ):
            raise HarnessError(f"{artifact_id}: shellUrlNeedles must be lowercase strings")

        if runtime == "jf10" and plugin["framework"] != "net9.0":
            raise HarnessError(f"{artifact_id}: Jellyfin 10.11 testables must be net9.0")
        if runtime == "jf12":
            testable_jf12.append(artifact_id)
            if plugin["framework"] != "net10.0" or not target_abi.startswith("12."):
                raise HarnessError(f"{artifact_id}: Jellyfin 12 needs a dedicated net10/12 ABI")

    duplicates = sorted(key for key, count in Counter(ids).items() if count != 1)
    if duplicates:
        raise HarnessError(f"duplicate artifact ids: {duplicates}")
    duplicate_urls = sorted(key for key, count in Counter(archive_urls).items() if count != 1)
    if duplicate_urls:
        raise HarnessError(f"duplicate archive URLs: {duplicate_urls}")
    expected_jf12 = {
        "intro-skipper-jf12",
        "jellyfin-enhanced-jf12",
        "stream-limit-jf12",
    }
    if set(testable_jf12) != expected_jf12:
        raise HarnessError(
            "Jellyfin 12 testables must be exactly the three audited dedicated net10 artifacts; "
            f"expected={sorted(expected_jf12)}, actual={sorted(testable_jf12)}"
        )

    coverage_repositories: list[str] = []
    coverage_indexes: list[int] = []
    known_ids = set(ids)
    artifacts_by_id = artifact_index(lock)
    fixture_ids = set(expected.get("fixtureIds", []))
    counts: dict[str, Counter[str]] = {"jf10": Counter(), "jf12": Counter()}
    category_counts: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    artifact_references: Counter[str] = Counter()
    for row in coverage:
        if not isinstance(row, dict):
            raise HarnessError("coverage rows must be objects")
        name = str(row.get("name", ""))
        repository = str(row.get("repository", ""))
        category = str(row.get("category", ""))
        catalog_index = row.get("catalogIndex")
        classification = row.get("classification")
        coverage_repositories.append(repository)
        coverage_indexes.append(catalog_index)
        category_counts[category] += 1
        classification_counts[classification] += 1
        if classification not in ALLOWED_CLASSIFICATIONS:
            raise HarnessError(f"{name}: invalid classification {classification!r}")
        if not isinstance(catalog_index, int) or catalog_index < 1:
            raise HarnessError(f"{name}: invalid catalogIndex {catalog_index!r}")
        if not isinstance(row.get("relevant"), bool):
            raise HarnessError(f"{name}: relevant must be a boolean")
        if row["relevant"] != (classification in RELEVANT_CLASSIFICATIONS):
            raise HarnessError(f"{name}: relevant conflicts with classification")
        if not isinstance(row.get("interactionSurface"), str) or not row["interactionSurface"]:
            raise HarnessError(f"{name}: interactionSurface must be non-empty")
        if not isinstance(row.get("reconciliationDecision"), str) or not row["reconciliationDecision"]:
            raise HarnessError(f"{name}: reconciliationDecision must be non-empty")
        row_artifacts = row.get("artifacts")
        if not isinstance(row_artifacts, list) or len(row_artifacts) != len(set(row_artifacts)):
            raise HarnessError(f"{name}: artifacts must be a unique list")
        for artifact_id in row_artifacts:
            if artifact_id not in known_ids:
                raise HarnessError(f"{name}: unknown artifact reference {artifact_id}")
            artifact_references[artifact_id] += 1
        for runtime in ("jf10", "jf12"):
            state = row.get(runtime)
            if state not in ALLOWED_COVERAGE:
                raise HarnessError(f"{name}: invalid {runtime} coverage state {state!r}")
            counts[runtime][state] += 1
            matching_testables = [
                artifact_id
                for artifact_id in row_artifacts
                if artifacts_by_id[artifact_id].get("disposition") == "testable"
                and artifacts_by_id[artifact_id].get("runtime") == runtime
            ]
            if state == "testable" and not matching_testables:
                raise HarnessError(
                    f"{name}: {runtime} is testable but has no current runtime artifact"
                )
            if state != "testable" and matching_testables:
                raise HarnessError(
                    f"{name}: {runtime} has a testable artifact but coverage is {state}"
                )
        for fixture_id in row.get("fixtures", []):
            if fixture_id not in fixture_ids:
                raise HarnessError(f"{name}: unknown fixture reference {fixture_id}")
        if classification == TESTABLE_CLASSIFICATION:
            if row.get("relevant") is not True or not row_artifacts:
                raise HarnessError(f"{name}: testable web rows must be relevant and artifact-backed")
            if row.get("coverageStatus") != "covered-current-release":
                raise HarnessError(f"{name}: current testable row is not marked covered")
        elif "testable" in {row.get("jf10"), row.get("jf12")}:
            raise HarnessError(f"{name}: non-testable classification has testable coverage")

    if len(set(coverage_repositories)) != len(coverage_repositories):
        raise HarnessError("catalog coverage repositories must be unique")
    if coverage_indexes != list(range(1, len(coverage) + 1)):
        raise HarnessError("catalog coverage rows must be in exact authoritative index order")
    if len(coverage) != expected.get("catalogCount"):
        raise HarnessError("catalog coverage count does not match coverageExpectations")
    if dict(category_counts) != expected.get("categoryCounts"):
        raise HarnessError("catalog category counts do not match coverageExpectations")
    if dict(classification_counts) != expected.get("classificationCounts"):
        raise HarnessError("catalog classification counts do not match coverageExpectations")
    relevant_count = sum(row["relevant"] for row in coverage)
    if relevant_count != expected.get("relevantCount"):
        raise HarnessError("catalog relevant count does not match coverageExpectations")
    testable_row_count = sum(
        row["classification"] == TESTABLE_CLASSIFICATION for row in coverage
    )
    if testable_row_count != expected.get("testableRelevantRowCount"):
        raise HarnessError("testable relevant row count does not match coverageExpectations")
    for runtime in ("jf10", "jf12"):
        if dict(counts[runtime]) != expected.get(runtime):
            raise HarnessError(
                f"{runtime} coverage counts {dict(counts[runtime])} do not match "
                f"{expected.get(runtime)}"
            )

    unreferenced = sorted(
        artifact_id
        for artifact_id in known_ids
        if artifact_references[artifact_id] == 0
        and not artifacts_by_id[artifact_id].get("catalogDependency")
    )
    multiply_referenced = sorted(
        artifact_id for artifact_id, count in artifact_references.items() if count != 1
    )
    if unreferenced or multiply_referenced:
        raise HarnessError(
            f"catalog artifact mapping mismatch; unreferenced={unreferenced}, "
            f"multiplyReferenced={multiply_referenced}"
        )

    validate_catalog_snapshot(lock)
    return lock


def validate_catalog_snapshot(lock: dict[str, Any]) -> None:
    audit = lock.get("audit")
    if not isinstance(audit, dict):
        raise HarnessError("ecosystem lock needs catalog audit metadata")
    source = Path(str(audit.get("source", "")))
    audit_path = source if source.is_absolute() else COMPAT_ROOT / source
    if not audit_path.is_file():
        raise HarnessError(f"catalog snapshot is missing: {audit_path}")
    expected_digest = str(lock.get("audit", {}).get("sha256", ""))
    actual_digest = sha256_path(audit_path)
    if actual_digest != expected_digest:
        raise HarnessError(
            f"catalog snapshot digest changed: {actual_digest} != {expected_digest}"
        )
    snapshot = load_json(audit_path)
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != 1:
        raise HarnessError("catalog snapshot must be a schemaVersion 1 object")
    source_metadata = snapshot.get("source")
    expectations = snapshot.get("expectations")
    rows = snapshot.get("rows")
    if not isinstance(source_metadata, dict) or not isinstance(expectations, dict):
        raise HarnessError("catalog snapshot metadata is incomplete")
    if not isinstance(rows, list) or len(rows) != expectations.get("rowCount"):
        raise HarnessError("catalog snapshot row count is invalid")
    if source_metadata.get("commit") != audit.get("catalogCommit"):
        raise HarnessError("catalog snapshot commit differs from ecosystem lock")
    if source_metadata.get("repository") != audit.get("catalogRepository"):
        raise HarnessError("catalog snapshot repository differs from ecosystem lock")
    if any(not isinstance(row, dict) for row in rows):
        raise HarnessError("catalog snapshot rows must be objects")
    snapshot_indexes = [row.get("index") for row in rows]
    if snapshot_indexes != list(range(1, len(rows) + 1)):
        raise HarnessError("catalog snapshot indexes are not authoritative and contiguous")
    snapshot_categories = Counter(str(row.get("category", "")) for row in rows)
    if dict(snapshot_categories) != expectations.get("categoryCounts"):
        raise HarnessError("catalog snapshot category counts differ from expectations")
    repository_states: Counter[str] = Counter()
    for row in rows:
        evidence = row.get("repositoryEvidence")
        if not isinstance(evidence, dict):
            raise HarnessError("catalog snapshot row lacks repository evidence")
        if evidence.get("archived") is True:
            state = "archived"
        elif (
            evidence.get("available") is True
            and evidence.get("private") is False
            and evidence.get("visibility") == "PUBLIC"
        ):
            state = "live-public"
        elif evidence.get("private") is True:
            state = "private"
        else:
            state = "unavailable"
        repository_states[state] += 1
        head = evidence.get("headOid")
        if evidence.get("available") is True and not SOURCE_REVISION_RE.fullmatch(str(head or "")):
            raise HarnessError("catalog snapshot has invalid repository head evidence")
        if not isinstance(row.get("catalogMarkers"), list):
            raise HarnessError("catalog snapshot markers must be arrays")
    if dict(repository_states) != expectations.get("repositoryStates"):
        raise HarnessError("catalog snapshot repository states differ from expectations")
    snapshot_identity = [
        (row.get("index"), row.get("category"), row.get("name"), row.get("repository"))
        for row in rows
    ]
    if len(set(snapshot_identity)) != len(snapshot_identity):
        raise HarnessError("catalog snapshot contains a duplicate authoritative row")
    coverage_identity = [
        (
            row.get("catalogIndex"),
            row.get("category"),
            row.get("name"),
            row.get("repository"),
        )
        for row in lock["catalogCoverage"]
    ]
    if snapshot_identity != coverage_identity:
        raise HarnessError(
            "catalog coverage does not classify every authoritative snapshot row exactly once"
        )


def select_artifacts(
    lock: dict[str, Any], artifact_ids: Iterable[str], include_all: bool
) -> list[dict[str, Any]]:
    indexed = artifact_index(lock)
    requested = list(artifact_ids)
    if include_all:
        if requested:
            raise HarnessError("do not combine explicit artifact ids with --all-locked")
        return list(lock["artifacts"])
    if not requested:
        raise HarnessError("specify artifact ids or --all-locked")
    missing = [artifact_id for artifact_id in requested if artifact_id not in indexed]
    if missing:
        raise HarnessError(f"unknown artifact ids: {missing}")
    return [indexed[artifact_id] for artifact_id in requested]


def archive_cache_path(cache_dir: Path, artifact: dict[str, Any]) -> Path:
    archive = artifact["archive"]
    return cache_dir / f"{archive['sha256']}-{archive['name']}"


def download_artifact(cache_dir: Path, artifact: dict[str, Any]) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = archive_cache_path(cache_dir, artifact)
    expected = artifact["archive"]["sha256"]
    if destination.is_file() and sha256_path(destination) == expected:
        return destination
    if destination.exists():
        quarantine = destination.with_name(destination.name + ".corrupt")
        suffix = 0
        while quarantine.exists():
            suffix += 1
            quarantine = destination.with_name(destination.name + f".corrupt.{suffix}")
        destination.replace(quarantine)

    request = urllib.request.Request(
        artifact["archive"]["url"],
        headers={"User-Agent": "jellyfin-refresh-kit-compat/1"},
        method="GET",
    )
    temporary: Path | None = None
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            final_url = response.geturl()
            parsed_final = urllib.parse.urlparse(final_url)
            allowed_final_hosts = {
                "github.com",
                "objects.githubusercontent.com",
                "release-assets.githubusercontent.com",
            }
            if parsed_final.scheme != "https" or parsed_final.netloc not in allowed_final_hosts:
                raise HarnessError(
                    f"{artifact['id']}: download redirected to an unapproved origin"
                )
            with tempfile.NamedTemporaryFile(
                prefix=".download-", suffix=".tmp", dir=cache_dir, delete=False
            ) as output:
                temporary = Path(output.name)
                digest = hashlib.sha256()
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise HarnessError(f"{artifact['id']}: archive exceeds size limit")
                    digest.update(chunk)
                    output.write(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise HarnessError(
                f"{artifact['id']}: sha256 mismatch {actual} != {expected}"
            )
        os.chmod(temporary, 0o644)
        temporary.replace(destination)
        temporary = None
        # The effective release-assets URL can contain expiring credentials in
        # its query string.  It is used only for the origin check above and is
        # deliberately never returned to, or serialized by, evidence code.
        return destination
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def normalized_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_MEMBER_COUNT:
        raise HarnessError("archive has too many members")
    expanded = sum(member.file_size for member in members)
    if expanded > MAX_EXPANDED_BYTES:
        raise HarnessError("archive expands beyond the configured limit")

    raw_files: list[tuple[PurePosixPath, zipfile.ZipInfo]] = []
    for member in members:
        raw_name = member.filename.replace("\\", "/")
        path = PurePosixPath(raw_name)
        if (
            "\x00" in raw_name
            or path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise HarnessError(f"unsafe zip member: {member.filename!r}")
        file_type = (member.external_attr >> 16) & 0o170000
        if file_type == stat.S_IFLNK:
            raise HarnessError(f"zip symlink is forbidden: {member.filename!r}")
        if not member.is_dir():
            raw_files.append((path, member))
    if not raw_files:
        raise HarnessError("archive contains no files")

    first_parts = {path.parts[0] for path, _ in raw_files}
    strip_wrapper = len(first_parts) == 1 and all(len(path.parts) > 1 for path, _ in raw_files)
    normalized: dict[str, zipfile.ZipInfo] = {}
    folded_paths: set[str] = set()
    for path, member in raw_files:
        parts = path.parts[1:] if strip_wrapper else path.parts
        relative = PurePosixPath(*parts).as_posix()
        folded = relative.casefold()
        if folded in folded_paths:
            raise HarnessError(f"duplicate normalized zip member: {relative}")
        normalized[relative] = member
        folded_paths.add(folded)
    return normalized


def matching_members(members: dict[str, zipfile.ZipInfo], basename: str) -> list[str]:
    wanted = basename.casefold()
    return sorted(path for path in members if PurePosixPath(path).name.casefold() == wanted)


def validate_meta(meta: dict[str, Any], artifact: dict[str, Any]) -> dict[str, Any]:
    plugin = artifact["plugin"]
    checks: dict[str, Any] = {}
    for field in ("guid", "name", "version"):
        actual = ci_value(meta, field)
        expected = plugin[field]
        matched = str(actual).casefold() == str(expected).casefold()
        checks[field] = {"expected": expected, "actual": actual, "matched": matched}
        if not matched:
            raise HarnessError(
                f"{artifact['id']}: upstream meta {field} {actual!r} != {expected!r}"
            )
    actual_abi = ci_value(meta, "targetAbi")
    checks["targetAbi"] = {
        "expected": plugin["targetAbi"],
        "actual": actual_abi,
        "matched": actual_abi is None
        or normalized_abi(actual_abi) == normalized_abi(plugin["targetAbi"]),
        "present": actual_abi is not None,
    }
    if not checks["targetAbi"]["matched"]:
        raise HarnessError(f"{artifact['id']}: upstream meta targetAbi conflicts with lock")
    assemblies = ci_value(meta, "assemblies")
    if assemblies not in (None, []):
        if not isinstance(assemblies, list) or plugin["assembly"] not in assemblies:
            raise HarnessError(f"{artifact['id']}: upstream meta assemblies omit main DLL")
    checks["assemblies"] = assemblies
    return checks


def inspect_archive(path: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    expected_sha = artifact["archive"]["sha256"]
    actual_sha = sha256_path(path)
    if actual_sha != expected_sha:
        raise HarnessError(f"{artifact['id']}: cached archive digest changed")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise HarnessError(f"{artifact['id']}: invalid zip archive") from exc
    with archive:
        members = normalized_members(archive)
        assembly_matches = matching_members(members, artifact["plugin"]["assembly"])
        if len(assembly_matches) != 1:
            raise HarnessError(
                f"{artifact['id']}: expected one {artifact['plugin']['assembly']}, "
                f"found {assembly_matches}"
            )
        assembly_path = assembly_matches[0]
        assembly_bytes = archive.read(members[assembly_path])
        token_checks = {}
        for field in ("guid", "version"):
            token = artifact["plugin"][field]
            candidates = {token}
            if field == "guid":
                # GUID literals are semantically case-insensitive. Several current
                # upstreams compile an uppercase literal while the fail-closed lock
                # stores its normalized lowercase form.
                candidates.add(token.upper())
            present = any(
                candidate.encode("utf-16-le") in assembly_bytes
                or candidate.encode() in assembly_bytes
                for candidate in candidates
            )
            token_checks[field] = present
            if not present:
                raise HarnessError(f"{artifact['id']}: assembly does not contain {field} token")

        meta_matches = matching_members(members, "meta.json")
        policy = artifact["plugin"]["archiveMeta"]
        meta_result: dict[str, Any] = {
            "policy": policy,
            "archivePath": meta_matches[0] if len(meta_matches) == 1 else None,
            "present": bool(meta_matches),
            "checks": None,
        }
        if len(meta_matches) > 1:
            raise HarnessError(f"{artifact['id']}: archive has multiple meta.json files")
        if policy == "upstream":
            if len(meta_matches) != 1:
                raise HarnessError(f"{artifact['id']}: expected upstream meta.json")
            try:
                meta = json.loads(archive.read(members[meta_matches[0]]).decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HarnessError(f"{artifact['id']}: invalid upstream meta.json") from exc
            if not isinstance(meta, dict):
                raise HarnessError(f"{artifact['id']}: upstream meta.json must be an object")
            meta_result["checks"] = validate_meta(meta, artifact)
        elif meta_matches:
            raise HarnessError(
                f"{artifact['id']}: lock says archive meta is absent but one was found"
            )

        deps_matches = matching_members(
            members, PurePosixPath(assembly_path).stem + ".deps.json"
        )
        framework_result: dict[str, Any] = {
            "expected": artifact["plugin"]["framework"],
            "depsPath": None,
            "runtimeTarget": None,
            "matched": None,
        }
        if len(deps_matches) > 1:
            raise HarnessError(f"{artifact['id']}: multiple matching deps.json files")
        if deps_matches:
            deps_path = deps_matches[0]
            try:
                deps = json.loads(archive.read(members[deps_path]).decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HarnessError(f"{artifact['id']}: invalid deps.json") from exc
            runtime_target = ci_value(deps.get("runtimeTarget", {}), "name")
            expected_major = artifact["plugin"]["framework"].removeprefix("net").split(".")[0]
            matched = f"Version=v{expected_major}.0" in str(runtime_target)
            framework_result.update(
                {"depsPath": deps_path, "runtimeTarget": runtime_target, "matched": matched}
            )
            if not matched:
                raise HarnessError(f"{artifact['id']}: deps framework conflicts with lock")

        return {
            "id": artifact["id"],
            "disposition": artifact["disposition"],
            "runtime": artifact["runtime"],
            "archive": {
                "path": str(path),
                "url": artifact["archive"]["url"],
                "sha256": actual_sha,
                "size": path.stat().st_size,
                "memberCount": len(members),
            },
            "plugin": {
                **artifact["plugin"],
                "assemblyPath": assembly_path,
                "assemblySha256": hashlib.sha256(assembly_bytes).hexdigest(),
                "binaryTokenChecks": token_checks,
                "meta": meta_result,
                "frameworkEvidence": framework_result,
            },
            "verified": True,
        }


def generated_meta(artifact: dict[str, Any]) -> dict[str, Any]:
    plugin = artifact["plugin"]
    return {
        "assemblies": [plugin["assembly"]],
        "autoUpdate": False,
        "category": "General",
        "description": "Compatibility-harness sidecar generated from ecosystem.lock.json.",
        "guid": plugin["guid"],
        "name": plugin["name"],
        "status": "Active",
        "targetAbi": plugin["targetAbi"],
        "version": plugin["version"],
    }


def materialize(path: Path, artifact: dict[str, Any], destination: Path) -> dict[str, Any]:
    if artifact["disposition"] != "testable":
        raise HarnessError(f"{artifact['id']}: quarantined/unsupported artifacts cannot be installed")
    verification = inspect_archive(path, artifact)
    if destination.exists() and any(destination.iterdir()):
        raise HarnessError(f"materialization destination is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        members = normalized_members(archive)
        for relative, member in members.items():
            target = destination.joinpath(*PurePosixPath(relative).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            os.chmod(target, 0o644)

    meta_path = destination / "meta.json"
    policy = artifact["plugin"]["archiveMeta"]
    if policy == "absent-generate":
        with meta_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(generated_meta(artifact), handle, indent=2, sort_keys=True)
            handle.write("\n")
    elif policy == "upstream":
        matches = list(destination.rglob("meta.json"))
        if len(matches) != 1:
            raise HarnessError(f"{artifact['id']}: materialized upstream meta is ambiguous")
        if matches[0] != meta_path:
            shutil.move(str(matches[0]), meta_path)
        upstream_meta = load_json(meta_path)
        if not isinstance(upstream_meta, dict):
            raise HarnessError(f"{artifact['id']}: materialized upstream meta is not an object")
        upstream_meta.setdefault("targetAbi", artifact["plugin"]["targetAbi"])
        if not ci_value(upstream_meta, "assemblies"):
            upstream_meta["assemblies"] = [artifact["plugin"]["assembly"]]
        with meta_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(upstream_meta, handle, indent=2, sort_keys=True)
            handle.write("\n")
    else:
        raise HarnessError(f"{artifact['id']}: non-installable meta policy reached materializer")

    installed_meta = load_json(meta_path)
    meta_checks = validate_meta(installed_meta, artifact)
    return {
        "id": artifact["id"],
        "destination": str(destination),
        "archiveSha256": verification["archive"]["sha256"],
        "assemblySha256": verification["plugin"]["assemblySha256"],
        "metaPath": str(meta_path),
        "metaSource": "generated" if policy == "absent-generate" else "upstream-completed",
        "metaChecks": meta_checks,
        "materialized": True,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    temporary.replace(path)


def validate_fetch_report(report: dict[str, Any], artifacts: list[dict[str, Any]]) -> None:
    """Keep artifact evidence tied to public lock URLs and free of redirect credentials."""
    rows = report.get("artifacts")
    if not isinstance(rows, list) or len(rows) != len(artifacts):
        raise HarnessError("artifact verification report has the wrong row count")
    for row, artifact in zip(rows, artifacts, strict=True):
        archive = row.get("archive") if isinstance(row, dict) else None
        if not isinstance(archive, dict):
            raise HarnessError("artifact verification report has no archive object")
        if "finalUrl" in archive:
            raise HarnessError("artifact verification report exposes an effective redirect URL")
        public_url = archive.get("url")
        parsed = urllib.parse.urlparse(str(public_url))
        if public_url != artifact["archive"]["url"] or parsed.query or parsed.fragment:
            raise HarnessError("artifact verification report URL differs from its public lock URL")


def cmd_lint(args: argparse.Namespace) -> int:
    lock = validate_lock(args.lock)
    counts = {
        runtime: dict(Counter(row[runtime] for row in lock["catalogCoverage"]))
        for runtime in ("jf10", "jf12")
    }
    print(
        json.dumps(
            {
                "artifactCount": len(lock["artifacts"]),
                "catalogCount": len(lock["catalogCoverage"]),
                "coverage": counts,
                "valid": True,
            },
            sort_keys=True,
        )
    )
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    lock = validate_lock(args.lock)
    selected = select_artifacts(lock, args.ids, args.all_locked)
    results = []
    for artifact in selected:
        path = download_artifact(args.cache, artifact)
        result = inspect_archive(path, artifact)
        results.append(result)
        print(f"verified {artifact['id']} {artifact['archive']['sha256']}", file=sys.stderr)
    report = {"schemaVersion": 1, "artifacts": results, "allPassed": True}
    validate_fetch_report(report, selected)
    if args.report:
        write_json(args.report, report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def cmd_materialize(args: argparse.Namespace) -> int:
    lock = validate_lock(args.lock)
    indexed = artifact_index(lock)
    if args.id not in indexed:
        raise HarnessError(f"unknown artifact id: {args.id}")
    artifact = indexed[args.id]
    path = download_artifact(args.cache, artifact)
    report = materialize(path, artifact, args.destination)
    if args.report:
        write_json(args.report, report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    subparsers = result.add_subparsers(dest="command", required=True)

    lint = subparsers.add_parser("lint", help="validate the lock without network access")
    lint.set_defaults(func=cmd_lint)

    fetch = subparsers.add_parser("fetch", help="download and inspect locked archives")
    fetch.add_argument("ids", nargs="*")
    fetch.add_argument("--all-locked", action="store_true")
    fetch.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    fetch.add_argument("--report", type=Path)
    fetch.set_defaults(func=cmd_fetch)

    materialize_parser = subparsers.add_parser(
        "materialize", help="safely expand one testable archive with verified metadata"
    )
    materialize_parser.add_argument("id")
    materialize_parser.add_argument("destination", type=Path)
    materialize_parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    materialize_parser.add_argument("--report", type=Path)
    materialize_parser.set_defaults(func=cmd_materialize)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except HarnessError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1
    except (OSError, urllib.error.URLError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
