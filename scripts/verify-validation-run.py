#!/usr/bin/env python3
"""Bind published-byte verification to one successful validation workflow run."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from typing import Any

from evidence_validation import (
    EvidenceValidationError,
    checksum_inventory,
    package_identity,
    validate_compatibility_tree,
    validate_integration_tree,
)


GITHUB_API = "https://api.github.com"
USER_AGENT = "jellyfin-refresh-kit-validation-predecessor/1"
WORKFLOW_PATH = ".github/workflows/release-validation.yml"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 4 * 1024 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000
REQUIRED_EXIT_STATUS = {
    "abiFloorLab": 0,
    "dualJellyfinLab": 0,
    "hostUpgradeLab": 0,
    "proxyRig": 0,
    "compatibilityMatrices": 0,
}
REQUIRED_COLLECTED = {
    "lab/abi-floor/result.json",
    "lab/abi-floor/server/result.json",
    "lab/abi-floor/server/public.json",
    "lab/abi-floor/server/generation.json",
    "lab/abi-floor/server/diagnostics.json",
    "lab/abi-floor/server/plugins.json",
    "lab/abi-floor/server/generation.headers",
    "lab/abi-floor/server/kit.headers",
    "lab/abi-floor/server/kit.js",
    "lab/abi-floor/server/shell.headers",
    "lab/abi-floor/server/conditional.headers",
    "lab/abi-floor/server/conditional.body",
    "lab/abi-floor/server/index.html",
    "lab/abi-floor/server.log",
    "lab/jf10/server/result.json",
    "lab/jf10/server/diagnostics.json",
    "lab/jf10/browser/result.json",
    "lab/jf10/lifecycle/result.json",
    "lab/jf10/third-party-lifecycle/result.json",
    "lab/jf12/server/result.json",
    "lab/jf12/server/diagnostics.json",
    "lab/jf12/browser/result.json",
    "lab/jf12/lifecycle/result.json",
    "lab/jf12/third-party-lifecycle/result.json",
    "lab/compat-jf10-on-jf12/result.json",
    "lab/host-upgrade/result.json",
    "lab/host-upgrade/jf10/result.json",
    "lab/host-upgrade/jf12/result.json",
    "logs/host-upgrade.log",
    "logs/abi-floor.log",
    "logs/dual-jellyfin.log",
    "logs/proxy.log",
    "compat/summary.json",
}
WORKER_CONTRACTS = {
    "fast": ("fast_repro_security", {}),
    "integration": (
        "integration_gate",
        {"abiFloorLab": 0, "dualJellyfinLab": 0, "hostUpgradeLab": 0, "proxyRig": 0},
    ),
    "compatibility": ("compatibility_gate", {"compatibilityMatrices": 0}),
}
WORKFLOW_NAME = "Release candidate validation (no publishing)"


class ValidationError(ValueError):
    pass


def trusted_download_host(hostname: str) -> bool:
    hostname = hostname.lower()
    return (
        hostname in {"api.github.com", "github.com", "www.github.com"}
        or hostname.endswith(".githubusercontent.com")
        or hostname.endswith(".blob.core.windows.net")
    )


class TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep redirects on known HTTPS download hosts and never forward credentials."""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        redirected = super().redirect_request(
            request, fp, code, message, headers, new_url
        )
        if redirected is None:
            return None
        old = urllib.parse.urlparse(request.full_url)
        new = urllib.parse.urlparse(redirected.full_url)
        if new.scheme != "https" or not trusted_download_host(new.hostname or ""):
            raise ValidationError(f"refusing untrusted GitHub redirect: {new_url}")
        if (old.hostname or "").lower() != (new.hostname or "").lower():
            sensitive = {"authorization", "cookie", "proxy-authorization"}
            for name, _ in tuple(redirected.header_items()):
                if name.lower() in sensitive:
                    redirected.remove_header(name)
        return redirected


def trusted_urlopen(request: urllib.request.Request, timeout: int):
    return urllib.request.build_opener(TrustedRedirectHandler()).open(
        request, timeout=timeout
    )


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_object(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValidationError(f"JSON object required: {path}")
    return value


def api_json(url: str, token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with trusted_urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise ValidationError("GitHub API returned a non-object")
    return value


def github_epoch(value: Any, label: str) -> int:
    if not isinstance(value, str):
        raise ValidationError(f"{label} is missing")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{label} is invalid: {value!r}") from error
    if parsed.tzinfo is None:
        raise ValidationError(f"{label} lacks a timezone")
    return int(parsed.timestamp())


def validate_run(
    run: dict[str, Any],
    repository: str,
    run_id: int,
    manifest_revision: str,
    candidate_ref: str,
    published_epoch: int,
) -> tuple[int, int]:
    expected = {
        "id": run_id,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_sha": manifest_revision,
        "head_branch": candidate_ref.removeprefix("refs/heads/"),
        "path": WORKFLOW_PATH,
    }
    for field, wanted in expected.items():
        if run.get(field) != wanted:
            raise ValidationError(
                f"validation run {field} is {run.get(field)!r}, expected {wanted!r}"
            )
    recorded_repository = run.get("repository")
    if not isinstance(recorded_repository, dict) or recorded_repository.get("full_name") != repository:
        raise ValidationError("validation run belongs to a different repository")
    html_url = urllib.parse.urlparse(str(run.get("html_url") or ""))
    expected_path = f"/{repository}/actions/runs/{run_id}"
    if (
        html_url.scheme != "https"
        or html_url.hostname != "github.com"
        or html_url.path != expected_path
        or html_url.query
        or html_url.fragment
    ):
        raise ValidationError("validation run URL does not match its repository and run id")
    attempt = run.get("run_attempt")
    if not isinstance(attempt, int) or attempt < 1:
        raise ValidationError("validation run has no valid run_attempt")
    created_epoch = github_epoch(run.get("created_at"), "validation run created_at")
    started_epoch = github_epoch(run.get("run_started_at"), "validation run run_started_at")
    completed_epoch = github_epoch(run.get("updated_at"), "validation run updated_at")
    if not created_epoch <= started_epoch <= completed_epoch:
        raise ValidationError("validation run timestamps are out of order")
    if completed_epoch >= published_epoch:
        raise ValidationError("validation run did not complete before release publication")
    return attempt, completed_epoch


def select_artifact(
    payload: dict[str, Any],
    expected_name: str,
    run_id: int,
    repository: str,
    run_completed_epoch: int,
    published_epoch: int,
) -> dict[str, Any]:
    rows = payload.get("artifacts")
    total = payload.get("total_count")
    if not isinstance(rows, list) or not isinstance(total, int) or total != len(rows):
        raise ValidationError("validation artifact response is incomplete or malformed")
    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == expected_name]
    if len(matches) != 1:
        raise ValidationError(
            f"validation run has {len(matches)} artifacts named {expected_name!r}, expected one"
        )
    artifact = matches[0]
    if artifact.get("expired") is not False:
        raise ValidationError("validation artifact is expired")
    workflow_run = artifact.get("workflow_run")
    if not isinstance(workflow_run, dict) or workflow_run.get("id") != run_id:
        raise ValidationError("validation artifact belongs to a different workflow run")
    if not isinstance(artifact.get("id"), int) or artifact["id"] < 1:
        raise ValidationError("validation artifact has no valid id")
    if not isinstance(artifact.get("size_in_bytes"), int) or artifact["size_in_bytes"] < 1:
        raise ValidationError("validation artifact has no valid byte length")
    if artifact["size_in_bytes"] > MAX_ARCHIVE_BYTES:
        raise ValidationError("validation artifact exceeds the archive size limit")
    url = artifact.get("archive_download_url")
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise ValidationError("validation artifact download URL is not GitHub API HTTPS")
    prefix = f"/repos/{urllib.parse.quote(repository, safe='/')}/actions/artifacts/"
    path_match = re.fullmatch(re.escape(prefix) + r"([0-9]+)/zip", parsed.path)
    if path_match is None or parsed.query or parsed.fragment:
        raise ValidationError("validation artifact download URL has an unexpected path")
    if int(path_match.group(1)) != artifact["id"]:
        raise ValidationError("validation artifact download URL names a different artifact id")
    digest = artifact.get("digest")
    if digest is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", str(digest)) is None:
        raise ValidationError("validation artifact API digest is malformed")
    created_epoch = github_epoch(artifact.get("created_at"), "validation artifact created_at")
    if created_epoch > run_completed_epoch:
        raise ValidationError("validation artifact was created after its workflow completed")
    if created_epoch >= published_epoch:
        raise ValidationError("validation artifact was not created before release publication")
    return artifact


def download_artifact(artifact: dict[str, Any], token: str, target: pathlib.Path) -> dict[str, Any]:
    request = urllib.request.Request(
        str(artifact["archive_download_url"]),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    digest = hashlib.sha256()
    size = 0
    with trusted_urlopen(request, timeout=120) as response, target.open("xb") as handle:
        final = urllib.parse.urlparse(response.geturl())
        hostname = (final.hostname or "").lower()
        if final.scheme != "https" or not trusted_download_host(hostname):
            raise ValidationError(f"validation artifact redirected to an untrusted host: {hostname}")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise ValidationError("validation artifact exceeds the archive size limit")
            digest.update(chunk)
            handle.write(chunk)
    if size == 0:
        raise ValidationError("validation artifact download is empty")
    if size != artifact["size_in_bytes"]:
        raise ValidationError("downloaded validation artifact has the wrong byte length")
    actual_digest = digest.hexdigest()
    recorded_digest = artifact.get("digest")
    if recorded_digest is not None and recorded_digest != f"sha256:{actual_digest}":
        raise ValidationError("downloaded validation artifact disagrees with its API digest")
    return {
        "bytes": size,
        "sha256": actual_digest,
        "finalHost": hostname,
        "finalPath": final.path,
    }


def extract_artifact(archive: pathlib.Path, output: pathlib.Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if len(entries) > MAX_ARCHIVE_FILES:
            raise ValidationError("validation artifact contains too many entries")
        declared = sum(row.file_size for row in entries)
        if declared > MAX_EXTRACTED_BYTES:
            raise ValidationError("validation artifact expands beyond the evidence size limit")
        seen: set[str] = set()
        written = 0
        for row in entries:
            name = row.filename
            if "\\" in name or "\x00" in name:
                raise ValidationError(f"unsafe validation artifact member: {name!r}")
            pure = pathlib.PurePosixPath(name)
            if pure.is_absolute() or not pure.parts or any(part in ("", ".", "..") for part in pure.parts):
                raise ValidationError(f"unsafe validation artifact member: {name!r}")
            normalized = pure.as_posix().rstrip("/")
            if normalized in seen:
                raise ValidationError(f"duplicate validation artifact member: {normalized}")
            seen.add(normalized)
            mode = (row.external_attr >> 16) & 0o177777
            kind = stat.S_IFMT(mode)
            if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise ValidationError(f"non-regular validation artifact member: {name!r}")
            if row.flag_bits & 0x1:
                raise ValidationError(f"encrypted validation artifact member: {name!r}")
            target = output.joinpath(*pure.parts)
            if row.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(row) as source, target.open("xb") as destination:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_EXTRACTED_BYTES:
                        raise ValidationError("validation artifact exceeds the extracted size limit")
                    destination.write(chunk)
            if target.stat().st_size != row.file_size:
                raise ValidationError(f"validation artifact member was truncated: {name!r}")


def evidence_root(extracted: pathlib.Path) -> pathlib.Path:
    root = extracted.resolve(strict=True)
    if not (root / "SHA256SUMS").is_file():
        raise ValidationError("validation artifact checksum root is not the archive root")
    try:
        checksum_inventory(root)
    except EvidenceValidationError as error:
        raise ValidationError(str(error)) from error
    return root


def _package_rows(value: Any, label: str, expected_names: tuple[str, str]) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} packages is not an array")
    rows: dict[str, dict[str, Any]] = {}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise ValidationError(f"{label} has an invalid package row")
        if row["name"] in rows:
            raise ValidationError(f"{label} repeats package {row['name']}")
        rows[row["name"]] = row
    if set(rows) != set(expected_names):
        raise ValidationError(f"{label} package inventory is not exact")
    return rows


def validate_retained_workers(
    root: pathlib.Path,
    build: pathlib.Path,
    repository: str,
    source_revision: str,
    manifest_revision: str,
    candidate_ref: str,
    run_id: int,
    run_attempt: int,
    version: str,
) -> None:
    workers = root / "workers"
    if not workers.is_dir() or {path.name for path in workers.iterdir()} != set(WORKER_CONTRACTS):
        raise ValidationError("retained split-worker inventory is not exact")
    combined = load_object(root / "run.json")
    recorded_workers = combined.get("workerEvidence")
    if not isinstance(recorded_workers, dict) or set(recorded_workers) != set(WORKER_CONTRACTS):
        raise ValidationError("combined run evidence has no exact worker binding")
    expected_package = package_identity(build)
    names = (
        f"jellyfin-refresh-kit_{version}.zip",
        f"jellyfin-refresh-kit_{version}_jf12.zip",
    )
    expected_rows = _package_rows(expected_package["packages"], "rebuilt", names)
    for name, (job, statuses) in WORKER_CONTRACTS.items():
        worker = workers / name
        try:
            inventory = checksum_inventory(worker)
        except EvidenceValidationError as error:
            raise ValidationError(f"retained {name} worker {error}") from error
        binding = recorded_workers[name]
        if (
            not isinstance(binding, dict)
            or binding.get("job") != job
            or binding.get("sha256sumsSha256") != sha256(worker / "SHA256SUMS")
            or binding.get("runJsonSha256") != sha256(worker / "run.json")
        ):
            raise ValidationError(f"combined run evidence does not bind the {name} worker")
        run = load_object(worker / "run.json")
        if (
            run.get("schemaVersion") != 1
            or run.get("sourceRevision") != source_revision
            or run.get("sourceDirty") is not False
            or run.get("sourceStatus") != []
            or run.get("missing") != []
            or run.get("exitStatus") != statuses
        ):
            raise ValidationError(f"retained {name} worker identity/status differs")
        github = run.get("github")
        expected_github = {
            "GITHUB_RUN_ID": str(run_id),
            "GITHUB_RUN_ATTEMPT": str(run_attempt),
            "GITHUB_WORKFLOW": WORKFLOW_NAME,
            "GITHUB_SHA": manifest_revision,
            "GITHUB_REF": candidate_ref,
            "GITHUB_JOB": job,
            "GITHUB_REPOSITORY": repository,
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_WORKFLOW_SHA": manifest_revision,
        }
        if not isinstance(github, dict) or any(
            github.get(field) != wanted for field, wanted in expected_github.items()
        ):
            raise ValidationError(f"retained {name} worker belongs to another execution")
        expected_workflow_ref = (
            f"{repository}/.github/workflows/"
            f"release-validation.yml@{candidate_ref}"
        )
        if github.get("GITHUB_WORKFLOW_REF") != expected_workflow_ref:
            raise ValidationError(f"retained {name} worker workflow ref differs")
        package_build = run.get("packageBuild")
        if (
            not isinstance(package_build, dict)
            or package_build.get("available") is not True
            or package_build.get("immutableSnapshot") != build.name
            or package_build.get("metadata") != expected_package["metadata"]
        ):
            raise ValidationError(f"retained {name} worker package identity differs")
        rows = _package_rows(package_build.get("packages"), f"{name} worker", names)
        for package_name, wanted in expected_rows.items():
            if (
                rows[package_name].get("bytes") != wanted["bytes"]
                or rows[package_name].get("sha256") != wanted["sha256"]
            ):
                raise ValidationError(f"retained {name} worker package differs: {package_name}")
        try:
            if name == "integration":
                expected_collected = validate_integration_tree(worker, build)
            elif name == "compatibility":
                expected_collected = validate_compatibility_tree(
                    worker, build, pathlib.Path(__file__).resolve().parent.parent / "e2e" / "compat" / "matrices.json"
                )
            else:
                expected_collected = set()
        except EvidenceValidationError as error:
            raise ValidationError(f"retained {name} worker {error}") from error
        collected = run.get("collected")
        if (
            not isinstance(collected, list)
            or set(collected) != expected_collected
            or len(collected) != len(expected_collected)
        ):
            raise ValidationError(f"retained {name} worker collected inventory differs")
        if set(inventory) != {"run.json", *expected_collected}:
            raise ValidationError(f"retained {name} worker has unexpected/unreported files")


def validate_candidate_bundle(
    root: pathlib.Path,
    build: pathlib.Path,
    manifest: pathlib.Path,
    current_receipt: pathlib.Path,
    release_policy: pathlib.Path,
    repository: str,
    source_revision: str,
    manifest_revision: str,
    candidate_ref: str,
    version: str,
    run_id: int,
    run_attempt: int,
) -> dict[str, Any]:
    expected_root_entries = {
        "SHA256SUMS",
        "run.json",
        "lab",
        "logs",
        "compat",
        "workers",
        "release-candidate",
    }
    if {path.name for path in root.iterdir()} != expected_root_entries:
        raise ValidationError("retained validation artifact top-level inventory is not exact")
    run = load_object(root / "run.json")
    if (
        run.get("schemaVersion") != 2
        or run.get("sourceRevision") != source_revision
        or run.get("candidateRef") != candidate_ref
    ):
        raise ValidationError("retained run evidence has the wrong source identity")
    if run.get("sourceDirty") is not False or run.get("sourceStatus") != []:
        raise ValidationError("retained run evidence was collected from a dirty source tree")
    exit_status = run.get("exitStatus")
    if (
        not isinstance(exit_status, dict)
        or set(exit_status) != set(REQUIRED_EXIT_STATUS)
        or any(
            type(exit_status.get(name)) is not int or exit_status.get(name) != wanted
            for name, wanted in REQUIRED_EXIT_STATUS.items()
        )
    ):
        raise ValidationError("retained run evidence does not prove every heavy gate passed")
    if run.get("missing") != []:
        raise ValidationError("retained run evidence reports missing files")
    try:
        integration_collected = validate_integration_tree(root, build)
        compatibility_collected = validate_compatibility_tree(
            root,
            build,
            pathlib.Path(__file__).resolve().parent.parent / "e2e" / "compat" / "matrices.json",
        )
    except EvidenceValidationError as error:
        raise ValidationError(f"retained {error}") from error
    expected_collected = integration_collected | compatibility_collected
    collected = run.get("collected")
    if (
        not isinstance(collected, list)
        or set(collected) != expected_collected
        or len(collected) != len(expected_collected)
        or not REQUIRED_COLLECTED.issubset(expected_collected)
    ):
        raise ValidationError("retained run evidence collected inventory is not exact")
    github = run.get("github")
    expected_github = {
        "GITHUB_RUN_ID": str(run_id),
        "GITHUB_RUN_ATTEMPT": str(run_attempt),
        "GITHUB_WORKFLOW": WORKFLOW_NAME,
        "GITHUB_SHA": manifest_revision,
        "GITHUB_REF": candidate_ref,
        "GITHUB_JOB": "final_retain",
        "GITHUB_REPOSITORY": repository,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_REF": (
            f"{repository}/.github/workflows/"
            f"release-validation.yml@{candidate_ref}"
        ),
        "GITHUB_WORKFLOW_SHA": manifest_revision,
    }
    if not isinstance(github, dict) or any(github.get(key) != value for key, value in expected_github.items()):
        raise ValidationError("retained run evidence belongs to a different workflow execution")
    expected_gates = {
        "fast": "success",
        "reproducibility": "success",
        "securityAudit": "success",
        "integration": "success",
        "compatibility": "success",
    }
    if run.get("gates") != expected_gates:
        raise ValidationError("retained run evidence does not prove every split gate passed")
    validate_retained_workers(
        root,
        build,
        repository,
        source_revision,
        manifest_revision,
        candidate_ref,
        run_id,
        run_attempt,
        version,
    )

    candidate = root / "release-candidate"
    if not candidate.is_dir():
        raise ValidationError("validation artifact has no retained release-candidate directory")
    expected_names = (
        f"jellyfin-refresh-kit_{version}.zip",
        f"jellyfin-refresh-kit_{version}_jf12.zip",
    )
    expected_inventory = {
        *expected_names,
        "manifest.json",
        "verification-receipt.json",
        "provenance.json",
        "release-policy.json",
    }
    actual_inventory = {path.name for path in candidate.iterdir() if path.is_file()}
    if actual_inventory != expected_inventory or any(path.is_dir() for path in candidate.iterdir()):
        raise ValidationError("retained release-candidate inventory is not exact")
    retained_manifest = candidate / "manifest.json"
    retained_receipt = candidate / "verification-receipt.json"
    retained_policy = candidate / "release-policy.json"
    provenance = load_object(candidate / "provenance.json")
    receipt = load_object(retained_receipt)
    policy = load_object(retained_policy)
    if retained_manifest.read_bytes() != manifest.read_bytes():
        raise ValidationError("validated manifest bytes do not match the requested manifest child")
    if retained_receipt.read_bytes() != current_receipt.read_bytes():
        raise ValidationError("rebuilt verification receipt does not match the validated receipt")
    expected_policy = load_object(release_policy)
    if policy != expected_policy or retained_policy.read_bytes() != release_policy.read_bytes():
        raise ValidationError(
            "validated release policy does not exactly match publication policy"
        )
    expected_provenance = {
        "schemaVersion": 1,
        "sourceRevision": source_revision,
        "manifestRevision": manifest_revision,
        "version": version,
        "manifestSha256": sha256(manifest),
        "verificationReceiptSha256": sha256(retained_receipt),
        "snapshotName": build.name,
    }
    for field, wanted in expected_provenance.items():
        if provenance.get(field) != wanted:
            raise ValidationError(
                f"retained candidate {field} is {provenance.get(field)!r}, expected {wanted!r}"
            )
    expected_receipt = {
        "schemaVersion": 1,
        "snapshotName": build.name,
        "manifestMode": "exact",
        "manifestSha256": sha256(manifest),
        "version": version,
        "sourceRevision": source_revision,
        "sourceDirty": False,
    }
    for field, wanted in expected_receipt.items():
        if receipt.get(field) != wanted:
            raise ValidationError(
                f"retained verification receipt {field} is {receipt.get(field)!r}, expected {wanted!r}"
            )

    def package_rows(value: Any, label: str) -> dict[str, dict[str, Any]]:
        if not isinstance(value, list):
            raise ValidationError(f"{label} packages is not an array")
        rows: dict[str, dict[str, Any]] = {}
        for row in value:
            if not isinstance(row, dict) or not isinstance(row.get("name"), str):
                raise ValidationError(f"{label} has an invalid package row")
            if row["name"] in rows:
                raise ValidationError(f"{label} repeats package {row['name']}")
            rows[row["name"]] = row
        if set(rows) != set(expected_names):
            raise ValidationError(f"{label} package inventory is not exact")
        return rows

    provenance_rows = package_rows(provenance.get("packages"), "provenance")
    receipt_rows = package_rows(receipt.get("packages"), "verification receipt")
    packages = []
    for name in expected_names:
        retained = candidate / name
        rebuilt = build / name
        if not rebuilt.is_file():
            raise ValidationError(f"rebuilt package is missing: {rebuilt}")
        identity = {"name": name, "bytes": retained.stat().st_size, "sha256": sha256(retained)}
        if rebuilt.stat().st_size != identity["bytes"] or sha256(rebuilt) != identity["sha256"]:
            raise ValidationError(f"rebuilt package does not match validated bytes: {name}")
        for label, row in (("provenance", provenance_rows[name]), ("verification receipt", receipt_rows[name])):
            if row.get("bytes") != identity["bytes"] or row.get("sha256") != identity["sha256"]:
                raise ValidationError(f"{label} does not bind validated package bytes: {name}")
        packages.append(identity)

    package_build = run.get("packageBuild")
    expected_build_identity = package_identity(build)
    if (
        not isinstance(package_build, dict)
        or package_build.get("available") is not True
        or package_build.get("immutableSnapshot") != build.name
        or package_build.get("metadata") != expected_build_identity["metadata"]
    ):
        raise ValidationError("retained run evidence names a different immutable snapshot")
    run_packages = package_rows(package_build.get("packages"), "run evidence")
    for row in packages:
        recorded = run_packages[row["name"]]
        if recorded.get("bytes") != row["bytes"] or recorded.get("sha256") != row["sha256"]:
            raise ValidationError(f"retained run evidence has a different package: {row['name']}")
    return {
        "snapshotName": build.name,
        "manifestSha256": sha256(manifest),
        "verificationReceiptSha256": sha256(retained_receipt),
        "releasePolicySha256": sha256(retained_policy),
        "packages": packages,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--manifest-revision", required=True)
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--build-dir", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--verification-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--release-policy", type=pathlib.Path, required=True)
    parser.add_argument("--published-epoch", type=int, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) is None:
        raise ValidationError("repository must be owner/name")
    if args.run_id < 1:
        raise ValidationError("validation run id must be positive")
    if args.published_epoch < 1:
        raise ValidationError("published epoch must be positive")
    for label, revision in (
        ("source", args.source_revision),
        ("manifest", args.manifest_revision),
    ):
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValidationError(f"{label} revision must be a lowercase full commit")
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", args.version) is None:
        raise ValidationError("version must contain four numeric parts")
    if args.candidate_ref != f"refs/heads/release-candidate/v{args.version}":
        raise ValidationError("candidate ref does not match the release version")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise ValidationError("GITHUB_TOKEN is required for read-only Actions artifact access")
    build = args.build_dir.resolve(strict=True)
    manifest = args.manifest.resolve(strict=True)
    current_receipt = args.verification_receipt.resolve(strict=True)
    release_policy = args.release_policy.resolve(strict=True)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    base = f"{GITHUB_API}/repos/{urllib.parse.quote(args.repository, safe='/')}"
    run = api_json(f"{base}/actions/runs/{args.run_id}", token)
    attempt, completed_epoch = validate_run(
        run,
        args.repository,
        args.run_id,
        args.manifest_revision,
        args.candidate_ref,
        args.published_epoch,
    )
    expected_name = f"release-validation-{args.source_revision}-{attempt}"
    payload = api_json(f"{base}/actions/runs/{args.run_id}/artifacts?per_page=100", token)
    artifact = select_artifact(
        payload,
        expected_name,
        args.run_id,
        args.repository,
        completed_epoch,
        args.published_epoch,
    )

    with tempfile.TemporaryDirectory(prefix="rk-validation-predecessor-") as temporary:
        scratch = pathlib.Path(temporary)
        archive = scratch / "artifact.zip"
        download = download_artifact(artifact, token, archive)
        extracted = scratch / "extracted"
        extracted.mkdir()
        extract_artifact(archive, extracted)
        root = evidence_root(extracted)
        candidate = validate_candidate_bundle(
            root,
            build,
            manifest,
            current_receipt,
            release_policy,
            args.repository,
            args.source_revision,
            args.manifest_revision,
            args.candidate_ref,
            args.version,
            args.run_id,
            attempt,
        )

    result = {
        "schemaVersion": 1,
        "repository": args.repository,
        "validationRunId": args.run_id,
        "validationRunAttempt": attempt,
        "validationRunUrl": run.get("html_url"),
        "validationCompletedAt": run.get("updated_at"),
        "validationCompletedAtEpoch": completed_epoch,
        "publishedAtEpoch": args.published_epoch,
        "sourceRevision": args.source_revision,
        "manifestRevision": args.manifest_revision,
        "candidateRef": args.candidate_ref,
        "version": args.version,
        "artifact": {
            "id": artifact["id"],
            "name": artifact["name"],
            "archiveBytes": download["bytes"],
            "archiveSha256": download["sha256"],
            "downloadHost": download["finalHost"],
            "downloadPath": download["finalPath"],
            "createdAt": artifact.get("created_at"),
        },
        "candidate": candidate,
        "allPassed": True,
    }
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"==> Successful release-validation run {args.run_id} retained these exact candidate bytes"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        urllib.error.HTTPError,
        urllib.error.URLError,
        EvidenceValidationError,
        ValidationError,
        zipfile.BadZipFile,
    ) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
