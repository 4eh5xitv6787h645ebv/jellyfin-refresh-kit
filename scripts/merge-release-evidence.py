#!/usr/bin/env python3
"""Validate split release jobs and assemble one exact retained evidence bundle."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
from typing import Any

from evidence_validation import (
    EvidenceValidationError,
    checksum_inventory,
    file_hash,
    load_object,
    package_identity,
    validate_compatibility_tree,
    validate_integration_tree,
    write_checksum_inventory,
)


ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW_NAME = "Release candidate validation (no publishing)"
WORKERS = {
    "fast": ("fast_repro_security", {}),
    "integration": (
        "integration_gate",
        {"abiFloorLab": 0, "dualJellyfinLab": 0, "proxyRig": 0},
    ),
    "compatibility": ("compatibility_gate", {"compatibilityMatrices": 0}),
}


class MergeError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MergeError(message)


def package_rows(value: Any, label: str) -> dict[str, dict[str, Any]]:
    require(isinstance(value, list), f"{label} package rows are not an array")
    rows: dict[str, dict[str, Any]] = {}
    for row in value:
        require(isinstance(row, dict) and isinstance(row.get("name"), str),
                f"{label} contains an invalid package row")
        name = str(row["name"])
        require(name not in rows, f"{label} repeats package {name}")
        rows[name] = row
    return rows


def require_package_identity(recorded: Any, expected: dict[str, Any], label: str) -> None:
    require(isinstance(recorded, dict), f"{label} has no package identity")
    for field in ("available", "immutableSnapshot", "metadata"):
        require(recorded.get(field) == expected[field], f"{label} package identity {field} differs")
    actual_rows = package_rows(recorded.get("packages"), label)
    expected_rows = package_rows(expected["packages"], "final build")
    require(set(actual_rows) == set(expected_rows), f"{label} package inventory differs")
    for name, wanted in expected_rows.items():
        actual = actual_rows[name]
        require(actual.get("bytes") == wanted["bytes"] and actual.get("sha256") == wanted["sha256"],
                f"{label} package bytes differ: {name}")


def validate_worker(
    name: str,
    root: pathlib.Path,
    build: pathlib.Path,
    matrices: pathlib.Path,
    repository: str,
    source_revision: str,
    manifest_revision: str,
    candidate_ref: str,
    run_id: int,
    run_attempt: int,
) -> tuple[dict[str, Any], set[str]]:
    root = root.resolve(strict=True)
    inventory = checksum_inventory(root)
    run = load_object(root / "run.json")
    expected_job, expected_status = WORKERS[name]
    require(run.get("schemaVersion") == 1, f"{name} worker schema differs")
    require(run.get("sourceRevision") == source_revision, f"{name} worker source differs")
    require(run.get("sourceDirty") is False and run.get("sourceStatus") == [],
            f"{name} worker source was not clean")
    require(run.get("missing") == [], f"{name} worker reports missing evidence")
    require(run.get("exitStatus") == expected_status, f"{name} worker exit-status set differs")
    github = run.get("github")
    expected_github = {
        "GITHUB_RUN_ID": str(run_id),
        "GITHUB_RUN_ATTEMPT": str(run_attempt),
        "GITHUB_WORKFLOW": WORKFLOW_NAME,
        "GITHUB_SHA": manifest_revision,
        "GITHUB_REF": candidate_ref,
        "GITHUB_JOB": expected_job,
        "GITHUB_REPOSITORY": repository,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_SHA": manifest_revision,
    }
    require(isinstance(github, dict)
            and all(github.get(field) == wanted for field, wanted in expected_github.items()),
            f"{name} worker belongs to a different workflow execution")
    workflow_ref = github.get("GITHUB_WORKFLOW_REF")
    require(isinstance(workflow_ref, str)
            and workflow_ref == (
                f"{repository}/.github/workflows/"
                f"release-validation.yml@{candidate_ref}"
            ), f"{name} worker workflow ref differs")
    expected_package = package_identity(build)
    require_package_identity(run.get("packageBuild"), expected_package, f"{name} worker")

    if name == "integration":
        collected = validate_integration_tree(root, build)
    elif name == "compatibility":
        collected = validate_compatibility_tree(root, build, matrices)
    else:
        collected = set()
    recorded_collected = run.get("collected")
    require(isinstance(recorded_collected, list) and set(recorded_collected) == collected
            and len(recorded_collected) == len(collected),
            f"{name} worker collected-file inventory differs")
    require(set(inventory) == {"run.json", *collected},
            f"{name} worker retained unexpected or unreported files")
    return run, collected


def validate_candidate(
    candidate: pathlib.Path,
    manifest: pathlib.Path,
    release_policy: pathlib.Path,
    expected_package: dict[str, Any],
    source_revision: str,
    manifest_revision: str,
    version: str,
) -> None:
    names = {
        f"jellyfin-refresh-kit_{version}.zip",
        f"jellyfin-refresh-kit_{version}_jf12.zip",
        "manifest.json",
        "verification-receipt.json",
        "provenance.json",
        "release-policy.json",
    }
    require(candidate.is_dir() and not candidate.is_symlink(), "release candidate is not a directory")
    actual = {path.name for path in candidate.iterdir()}
    require(actual == names and all(path.is_file() and not path.is_symlink() for path in candidate.iterdir()),
            "release candidate inventory is not exact")
    require((candidate / "manifest.json").read_bytes() == manifest.read_bytes(),
            "release candidate manifest differs")
    require((candidate / "release-policy.json").read_bytes() == release_policy.read_bytes(),
            "release candidate policy differs")
    provenance = load_object(candidate / "provenance.json")
    receipt = load_object(candidate / "verification-receipt.json")
    expected_fields = {
        "sourceRevision": source_revision,
        "manifestRevision": manifest_revision,
        "version": version,
        "manifestSha256": file_hash(manifest),
        "snapshotName": expected_package["immutableSnapshot"],
    }
    for field, wanted in expected_fields.items():
        require(provenance.get(field) == wanted, f"release candidate provenance {field} differs")
    require(provenance.get("schemaVersion") == 1
            and provenance.get("verificationReceiptSha256")
            == file_hash(candidate / "verification-receipt.json"),
            "release candidate provenance does not bind its verification receipt")
    require(receipt.get("schemaVersion") == 1
            and receipt.get("sourceRevision") == source_revision
            and receipt.get("version") == version
            and receipt.get("manifestMode") == "exact"
            and receipt.get("manifestSha256") == file_hash(manifest)
            and receipt.get("sourceDirty") is False
            and receipt.get("snapshotName") == expected_package["immutableSnapshot"],
            "release candidate verification receipt identity differs")
    expected_rows = package_rows(expected_package["packages"], "final build")
    for label, rows_value in (
        ("release candidate provenance", provenance.get("packages")),
        ("release candidate receipt", receipt.get("packages")),
    ):
        rows = package_rows(rows_value, label)
        require(set(rows) == set(expected_rows), f"{label} inventory differs")
        for name, wanted in expected_rows.items():
            path = candidate / name
            require(path.stat().st_size == wanted["bytes"] and file_hash(path) == wanted["sha256"],
                    f"release candidate package differs: {name}")
            require(rows[name].get("bytes") == wanted["bytes"]
                    and rows[name].get("sha256") == wanted["sha256"],
                    f"{label} package identity differs: {name}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", type=pathlib.Path, required=True)
    parser.add_argument("--integration", type=pathlib.Path, required=True)
    parser.add_argument("--compatibility", type=pathlib.Path, required=True)
    parser.add_argument("--candidate", type=pathlib.Path, required=True)
    parser.add_argument("--build-dir", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--release-policy", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--manifest-revision", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    for label, revision in (
        ("source", args.source_revision),
        ("manifest", args.manifest_revision),
    ):
        require(re.fullmatch(r"[0-9a-f]{40}", revision) is not None,
                f"{label} revision must be a lowercase full commit")
    require(re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", args.version) is not None,
            "version must contain four numeric parts")
    require(args.candidate_ref == f"refs/heads/release-candidate/v{args.version}",
            "candidate ref does not match the version")
    require(args.run_id > 0 and args.run_attempt > 0, "run identity must be positive")
    expected_environment = {
        "GITHUB_RUN_ID": str(args.run_id),
        "GITHUB_RUN_ATTEMPT": str(args.run_attempt),
        "GITHUB_WORKFLOW": WORKFLOW_NAME,
        "GITHUB_SHA": args.manifest_revision,
        "GITHUB_REF": args.candidate_ref,
        "GITHUB_JOB": "final_retain",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_WORKFLOW_SHA": args.manifest_revision,
    }
    require(all(os.environ.get(name) == wanted for name, wanted in expected_environment.items()),
            "final retention environment belongs to another workflow execution")
    repository = os.environ.get("GITHUB_REPOSITORY")
    require(isinstance(repository, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is not None,
            "final retention repository identity is invalid")
    require(os.environ.get("GITHUB_WORKFLOW_REF") == (
        f"{repository}/.github/workflows/release-validation.yml@{args.candidate_ref}"
    ), "final retention workflow ref differs")

    build = args.build_dir.resolve(strict=True)
    manifest = args.manifest.resolve(strict=True)
    release_policy = args.release_policy.resolve(strict=True)
    candidate = args.candidate.resolve(strict=True)
    worker_roots = {
        "fast": args.fast.resolve(strict=True),
        "integration": args.integration.resolve(strict=True),
        "compatibility": args.compatibility.resolve(strict=True),
    }
    expected_package = package_identity(build)
    worker_runs: dict[str, dict[str, Any]] = {}
    collected: dict[str, set[str]] = {}
    for name, worker_root in worker_roots.items():
        worker_runs[name], collected[name] = validate_worker(
            name,
            worker_root,
            build,
            ROOT / "e2e" / "compat" / "matrices.json",
            repository,
            args.source_revision,
            args.manifest_revision,
            args.candidate_ref,
            args.run_id,
            args.run_attempt,
        )
    validate_candidate(
        candidate,
        manifest,
        release_policy,
        expected_package,
        args.source_revision,
        args.manifest_revision,
        args.version,
    )

    output = args.output.resolve()
    require(not output.exists() and not output.is_symlink(), f"evidence output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(tempfile.mkdtemp(prefix=f".{output.name}.candidate-", dir=output.parent))
    try:
        shutil.copytree(worker_roots["integration"] / "lab", temporary / "lab")
        shutil.copytree(worker_roots["integration"] / "logs", temporary / "logs")
        shutil.copytree(worker_roots["compatibility"] / "compat", temporary / "compat")
        workers_output = temporary / "workers"
        workers_output.mkdir()
        for name, worker_root in worker_roots.items():
            shutil.copytree(worker_root, workers_output / name)
        shutil.copytree(candidate, temporary / "release-candidate")

        combined_collected = sorted(collected["integration"] | collected["compatibility"])
        run = {
            "schemaVersion": 2,
            "sourceRevision": args.source_revision,
            "sourceDirty": False,
            "sourceStatus": [],
            "candidateRef": args.candidate_ref,
            "github": {
                "GITHUB_RUN_ID": str(args.run_id),
                "GITHUB_RUN_ATTEMPT": str(args.run_attempt),
                "GITHUB_WORKFLOW": WORKFLOW_NAME,
                "GITHUB_SHA": args.manifest_revision,
                "GITHUB_REF": args.candidate_ref,
                "GITHUB_JOB": "final_retain",
                "GITHUB_REPOSITORY": repository,
                "GITHUB_EVENT_NAME": "workflow_dispatch",
                "GITHUB_WORKFLOW_REF": os.environ["GITHUB_WORKFLOW_REF"],
                "GITHUB_WORKFLOW_SHA": args.manifest_revision,
            },
            "exitStatus": {
                "abiFloorLab": 0,
                "dualJellyfinLab": 0,
                "proxyRig": 0,
                "compatibilityMatrices": 0,
            },
            "gates": {
                "fast": "success",
                "reproducibility": "success",
                "securityAudit": "success",
                "integration": "success",
                "compatibility": "success",
            },
            "packageBuild": expected_package,
            "collected": combined_collected,
            "missing": [],
            "workerEvidence": {
                name: {
                    "job": WORKERS[name][0],
                    "sha256sumsSha256": file_hash(worker_roots[name] / "SHA256SUMS"),
                    "runJsonSha256": file_hash(worker_roots[name] / "run.json"),
                }
                for name in WORKERS
            },
        }
        (temporary / "run.json").write_text(
            json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        write_checksum_inventory(temporary)
        checksum_inventory(temporary)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    print(f"==> Merged exact split-job release evidence in {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (EvidenceValidationError, FileNotFoundError, json.JSONDecodeError, MergeError, OSError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
