#!/usr/bin/env python3
"""Validate retained host-upgrade results against one immutable build snapshot."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent.parent
SEMANTIC_VALIDATOR = ROOT / "e2e" / "jellyfin" / "lib" / "verify-host-upgrade-results.py"
SCENARIOS = ("jf10", "jf12")
STAGES = (
    ("net9", "stage", ""),
    ("net10", "stage-jf12", "_jf12"),
)


class HostUpgradeEvidenceError(ValueError):
    """Raised when retained host-upgrade evidence is incomplete or stale."""


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
        raise HostUpgradeEvidenceError(
            f"cannot read host-upgrade evidence {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise HostUpgradeEvidenceError(f"host-upgrade evidence is not an object: {path}")
    return value


def _json_equal(left: Any, right: Any) -> bool:
    """Compare JSON values without Python's bool/int or int/float coercion."""
    return json.dumps(left, sort_keys=True, separators=(",", ":")) == json.dumps(
        right, sort_keys=True, separators=(",", ":")
    )


def _load_semantic_validator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "rk_host_upgrade_semantic_validator", SEMANTIC_VALIDATOR
    )
    if spec is None or spec.loader is None:
        raise HostUpgradeEvidenceError(
            f"cannot load host-upgrade semantic validator: {SEMANTIC_VALIDATOR}"
        )
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, SyntaxError) as error:
        raise HostUpgradeEvidenceError(
            f"cannot load host-upgrade semantic validator: {error}"
        ) from error
    if not callable(getattr(module, "validate_aggregate", None)):
        raise HostUpgradeEvidenceError("host-upgrade semantic validator has no aggregate gate")
    return module


def _package_record(path: pathlib.Path) -> dict[str, Any]:
    if not path.is_file():
        raise HostUpgradeEvidenceError(f"immutable host-upgrade package is missing: {path}")
    return {
        "file": path.name,
        "size": path.stat().st_size,
        "sha256": file_hash(path),
        "md5": file_hash(path, "md5"),
    }


def expected_candidate_identity(build: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the exact source and stage identities for ``build``."""
    build = build.resolve()
    if not build.is_dir():
        raise HostUpgradeEvidenceError(
            f"immutable host-upgrade build snapshot is unavailable: {build}"
        )
    stages: dict[str, Any] = {}
    for key, directory, package_suffix in STAGES:
        stage = build / directory
        meta = load_object(stage / "meta.json")
        dll = stage / "Jellyfin.Plugin.RefreshKit.dll"
        if not dll.is_file():
            raise HostUpgradeEvidenceError(f"immutable host-upgrade DLL is missing: {dll}")
        version = meta.get("version")
        if not isinstance(version, str) or not version:
            raise HostUpgradeEvidenceError(f"immutable host-upgrade stage has no version: {stage}")
        package = build / f"jellyfin-refresh-kit_{version}{package_suffix}.zip"
        stages[key] = {
            "meta": meta,
            "dllSha256": file_hash(dll),
            "package": _package_record(package),
        }

    net9 = stages["net9"]["meta"]
    net10 = stages["net10"]["meta"]
    identity_fields = (
        "version",
        "guid",
        "sourceRevision",
        "sourceTreeSha256",
        "sourceDateEpoch",
        "sourceDirty",
    )
    disagreements = [field for field in identity_fields if net9.get(field) != net10.get(field)]
    if disagreements:
        raise HostUpgradeEvidenceError(
            "immutable net9/net10 stages disagree on host-upgrade identity: "
            + ", ".join(disagreements)
        )
    source = {
        "revision": net9.get("sourceRevision"),
        "treeSha256": net9.get("sourceTreeSha256"),
        "dateEpoch": net9.get("sourceDateEpoch"),
        "dirty": net9.get("sourceDirty"),
    }
    return source, stages


def validate_candidate_identity(
    results: dict[str, dict[str, Any]],
    aggregate: dict[str, Any],
    build: pathlib.Path,
) -> None:
    """Bind both scenario documents and their aggregate to exact candidate bytes."""
    if set(results) != set(SCENARIOS):
        raise HostUpgradeEvidenceError("host-upgrade scenario result inventory is not exact")
    build = build.resolve()
    source, stages = expected_candidate_identity(build)
    for scenario in SCENARIOS:
        metadata = results[scenario].get("metadata")
        if not isinstance(metadata, dict):
            raise HostUpgradeEvidenceError(f"{scenario}: host-upgrade metadata is missing")
        if metadata.get("immutableSnapshot") != build.name:
            raise HostUpgradeEvidenceError(
                f"{scenario}: host-upgrade evidence names a different immutable snapshot"
            )
        if not _json_equal(metadata.get("sourceIdentity"), source):
            raise HostUpgradeEvidenceError(
                f"{scenario}: host-upgrade evidence names a different source identity"
            )
        if not _json_equal(metadata.get("stages"), stages):
            raise HostUpgradeEvidenceError(
                f"{scenario}: host-upgrade evidence does not bind exact stage/package bytes"
            )
    if aggregate.get("immutableSnapshot") != build.name:
        raise HostUpgradeEvidenceError("aggregate host-upgrade snapshot identity differs")
    if not _json_equal(aggregate.get("sourceIdentity"), source):
        raise HostUpgradeEvidenceError("aggregate host-upgrade source identity differs")


def validate_evidence(root: pathlib.Path, build: pathlib.Path) -> None:
    """Run the exhaustive semantic gate and exact immutable-candidate binding."""
    root = root.resolve()
    build = build.resolve()
    validator = _load_semantic_validator()
    try:
        validator.validate_aggregate(root, build.name)
    except (OSError, ValueError) as error:
        raise HostUpgradeEvidenceError(f"invalid host-upgrade result: {error}") from error

    results = {
        scenario: load_object(root / scenario / "result.json") for scenario in SCENARIOS
    }
    aggregate = load_object(root / "result.json")
    validate_candidate_identity(results, aggregate, build)
