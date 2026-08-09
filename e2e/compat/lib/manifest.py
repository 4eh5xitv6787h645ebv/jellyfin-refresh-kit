#!/usr/bin/env python3
"""Validate and query the compatibility matrix manifest."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import artifacts as artifact_lib


HERE = Path(__file__).resolve().parent
COMPAT_ROOT = HERE.parent
DEFAULT_LOCK = COMPAT_ROOT / "ecosystem.lock.json"
DEFAULT_MATRICES = COMPAT_ROOT / "matrices.json"
SAFE_DEGRADE_MATRIX_IDS = {
    "jf10-middleware-forward",
    "jf10-middleware-reverse",
    "jf10-response-transformers-forward",
    "jf10-response-transformers-reverse",
    "jf12-enhanced",
}
WRITABLE_WEBROOT_MATRIX_IDS = {"jf10-direct-writers-writable"}
UNVERSIONED_OUTER_ARTIFACTS_BY_MATRIX = {
    "jf10-middleware-forward": {"get-avatar-jf10"},
    "jf10-middleware-reverse": {"get-avatar-jf10"},
}


def load_and_validate(lock_path: Path, matrix_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    lock = artifact_lib.validate_lock(lock_path)
    document = artifact_lib.load_json(matrix_path)
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise artifact_lib.HarnessError("matrices must be a schemaVersion 1 object")
    runtimes = document.get("runtimes")
    matrices = document.get("matrices")
    if not isinstance(runtimes, dict) or set(runtimes) != {"jf10", "jf12"}:
        raise artifact_lib.HarnessError("matrices must declare exactly jf10 and jf12 runtimes")
    if not isinstance(matrices, list) or not matrices:
        raise artifact_lib.HarnessError("matrices array is empty")

    for runtime, details in runtimes.items():
        if not isinstance(details, dict):
            raise artifact_lib.HarnessError(f"{runtime}: runtime details must be an object")
        image = str(details.get("image", ""))
        digest = str(details.get("imageDigest", ""))
        if f"@sha256:{digest}" not in image or not artifact_lib.SHA256_RE.fullmatch(digest):
            raise artifact_lib.HarnessError(f"{runtime}: image is not pinned to imageDigest")
        if not str(details.get("serverVersionRegex", "")).startswith("^"):
            raise artifact_lib.HarnessError(f"{runtime}: serverVersionRegex must be anchored")
        expected_framework = "net9.0" if runtime == "jf10" else "net10.0"
        if details.get("refreshKitFramework") != expected_framework:
            raise artifact_lib.HarnessError(f"{runtime}: Refresh Kit framework mismatch")
        default_stage = str(details.get("defaultStage", ""))
        if not default_stage.startswith("plugin/build/stage") or default_stage.startswith("/"):
            raise artifact_lib.HarnessError(f"{runtime}: defaultStage must be project-relative")

    artifacts = artifact_lib.artifact_index(lock)
    matrix_ids: list[str] = []
    used: set[str] = set()
    order_pairs: dict[str, list[list[str]]] = defaultdict(list)
    for matrix in matrices:
        if not isinstance(matrix, dict):
            raise artifact_lib.HarnessError("matrix rows must be objects")
        matrix_id = str(matrix.get("id", ""))
        matrix_ids.append(matrix_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", matrix_id):
            raise artifact_lib.HarnessError(f"unsafe matrix id: {matrix_id!r}")
        runtime = matrix.get("runtime")
        if runtime not in runtimes:
            raise artifact_lib.HarnessError(f"{matrix_id}: unknown runtime {runtime!r}")
        service = matrix.get("service")
        allowed_services = {runtime}
        if runtime == "jf10":
            allowed_services.add("jf10-writable")
        if service not in allowed_services:
            raise artifact_lib.HarnessError(
                f"{matrix_id}: service {service!r} is invalid for {runtime}"
            )
        webroot_expectation = matrix.get("webrootExpectation")
        expected_webroot = "writable-volume" if service == "jf10-writable" else "read-only"
        if webroot_expectation != expected_webroot:
            raise artifact_lib.HarnessError(
                f"{matrix_id}: webrootExpectation must be {expected_webroot!r}"
            )
        order = matrix.get("installOrder")
        if not isinstance(order, list) or order.count("@refresh-kit") != 1:
            raise artifact_lib.HarnessError(
                f"{matrix_id}: installOrder must contain @refresh-kit exactly once"
            )
        if len(order) != len(set(order)):
            raise artifact_lib.HarnessError(f"{matrix_id}: installOrder contains duplicates")
        for artifact_id in order:
            if artifact_id == "@refresh-kit":
                continue
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                raise artifact_lib.HarnessError(f"{matrix_id}: unknown artifact {artifact_id}")
            if artifact["disposition"] != "testable":
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: cannot install {artifact['disposition']} artifact {artifact_id}"
                )
            if artifact["runtime"] != runtime:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: {artifact_id} is for {artifact['runtime']}, not {runtime}"
                )
            used.add(artifact_id)
        generation_probe = matrix.get("generationProbe")
        if generation_probe not in order or generation_probe == "@refresh-kit":
            raise artifact_lib.HarnessError(f"{matrix_id}: invalid generationProbe")
        expectation = matrix.get("stampingExpectation")
        if expectation not in {"required", "observe"}:
            raise artifact_lib.HarnessError(f"{matrix_id}: invalid stampingExpectation")
        if matrix.get("cacheExpectation") not in {"required", "observe", "safe-degrade"}:
            raise artifact_lib.HarnessError(f"{matrix_id}: invalid cacheExpectation")
        required = matrix.get("requiredStampedArtifacts")
        if not isinstance(required, list) or any(artifact_id not in order for artifact_id in required):
            raise artifact_lib.HarnessError(f"{matrix_id}: invalid requiredStampedArtifacts")
        required_unversioned = matrix.get("requiredUnversionedOuterArtifacts", [])
        if (
            not isinstance(required_unversioned, list)
            or len(required_unversioned) != len(set(required_unversioned))
            or any(artifact_id not in order for artifact_id in required_unversioned)
        ):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: invalid requiredUnversionedOuterArtifacts"
            )
        if set(required) & set(required_unversioned):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: an artifact cannot be both stamped and explicitly unversioned"
            )
        if expectation == "observe" and required:
            raise artifact_lib.HarnessError(
                f"{matrix_id}: observe-only matrix cannot require stamped artifacts"
            )
        required_present = matrix.get("requiredPresentArtifacts", [])
        if (
            not isinstance(required_present, list)
            or len(required_present) != len(set(required_present))
            or any(artifact_id not in order for artifact_id in required_present)
        ):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: invalid requiredPresentArtifacts"
            )
        for artifact_id in (*required, *required_unversioned, *required_present):
            if not artifacts[artifact_id]["shellUrlNeedles"]:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: required shell artifact {artifact_id} has no URL matcher"
                )
        body_markers = matrix.get("requiredBodyMarkers", {})
        if not isinstance(body_markers, dict):
            raise artifact_lib.HarnessError(f"{matrix_id}: requiredBodyMarkers must be an object")
        for artifact_id, markers in body_markers.items():
            if artifact_id not in order or artifact_id == "@refresh-kit":
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: body-marker artifact is not installed: {artifact_id}"
                )
            if (
                not isinstance(markers, list)
                or not markers
                or len(markers) != len(set(markers))
                or any(not isinstance(marker, str) or not marker for marker in markers)
            ):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: invalid body markers for {artifact_id}"
                )
        configuration_patches = matrix.get("configurationPatches", [])
        if not isinstance(configuration_patches, list):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: configurationPatches must be an array"
            )
        configured_artifacts: list[str] = []
        for patch in configuration_patches:
            if not isinstance(patch, dict):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: configuration patch must be an object"
                )
            artifact_id = patch.get("artifactId")
            configured_artifacts.append(str(artifact_id))
            if artifact_id not in order or artifact_id == "@refresh-kit":
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: configuration patch artifact is not installed"
                )
            if not isinstance(patch.get("payload"), dict) or not patch["payload"]:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: configuration patch payload is empty"
                )
        if len(configured_artifacts) != len(set(configured_artifacts)):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: duplicate configuration patch artifacts"
            )
        content_probes = matrix.get("contentProbes", [])
        if not isinstance(content_probes, list):
            raise artifact_lib.HarnessError(f"{matrix_id}: contentProbes must be an array")
        probe_ids: list[str] = []
        for probe in content_probes:
            if not isinstance(probe, dict):
                raise artifact_lib.HarnessError(f"{matrix_id}: content probe must be an object")
            probe_id = str(probe.get("id", ""))
            probe_ids.append(probe_id)
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", probe_id):
                raise artifact_lib.HarnessError(f"{matrix_id}: unsafe content probe id")
            path = str(probe.get("path", ""))
            if not path.startswith("/") or "?" in path or "#" in path:
                raise artifact_lib.HarnessError(f"{matrix_id}: invalid content probe path")
            if not isinstance(probe.get("authenticated"), bool):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: content probe authenticated must be boolean"
                )
            markers = probe.get("markers")
            if not isinstance(markers, dict) or not markers:
                raise artifact_lib.HarnessError(f"{matrix_id}: content probe markers are empty")
            for artifact_id, values in markers.items():
                if artifact_id not in order or artifact_id == "@refresh-kit":
                    raise artifact_lib.HarnessError(
                        f"{matrix_id}: content probe artifact is not installed: {artifact_id}"
                    )
                if (
                    not isinstance(values, list)
                    or not values
                    or len(values) != len(set(values))
                    or any(not isinstance(value, str) or not value for value in values)
                ):
                    raise artifact_lib.HarnessError(
                        f"{matrix_id}: invalid content markers for {artifact_id}"
                    )
        if len(probe_ids) != len(set(probe_ids)):
            raise artifact_lib.HarnessError(f"{matrix_id}: duplicate content probe ids")
        quarantined = matrix.get("quarantinedAssertions")
        if not isinstance(quarantined, list) or any(
            not isinstance(note, str) or not note.strip() for note in quarantined
        ):
            raise artifact_lib.HarnessError(f"{matrix_id}: invalid quarantinedAssertions")
        pair = matrix.get("orderPair")
        if pair is not None:
            order_pairs[str(pair)].append(order)
        if runtime == "jf12" and any(
            artifact_id not in {
                "@refresh-kit",
                "intro-skipper-jf12",
                "jellyfin-enhanced-jf12",
                "stream-limit-jf12",
            }
            for artifact_id in order
        ):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: Jellyfin 12 matrix includes an unapproved ABI"
            )

    duplicates = sorted(key for key, count in Counter(matrix_ids).items() if count != 1)
    if duplicates:
        raise artifact_lib.HarnessError(f"duplicate matrix ids: {duplicates}")
    safe_degrade_ids = {
        str(matrix["id"])
        for matrix in matrices
        if matrix.get("cacheExpectation") == "safe-degrade"
    }
    if safe_degrade_ids != SAFE_DEGRADE_MATRIX_IDS:
        raise artifact_lib.HarnessError(
            "safe-degrade cache mode must be assigned exactly to the audited outer-buffer "
            f"matrices; expected={sorted(SAFE_DEGRADE_MATRIX_IDS)}, "
            f"actual={sorted(safe_degrade_ids)}"
        )
    writable_ids = {
        str(matrix["id"])
        for matrix in matrices
        if matrix.get("webrootExpectation") == "writable-volume"
    }
    if writable_ids != WRITABLE_WEBROOT_MATRIX_IDS:
        raise artifact_lib.HarnessError(
            "writable webroot mode must remain limited to the audited direct-writer matrix; "
            f"expected={sorted(WRITABLE_WEBROOT_MATRIX_IDS)}, actual={sorted(writable_ids)}"
        )
    actual_unversioned_outer = {
        str(matrix["id"]): set(matrix.get("requiredUnversionedOuterArtifacts", []))
        for matrix in matrices
        if matrix.get("requiredUnversionedOuterArtifacts")
    }
    if actual_unversioned_outer != UNVERSIONED_OUTER_ARTIFACTS_BY_MATRIX:
        raise artifact_lib.HarnessError(
            "unversioned outer-owner limitations must remain exactly the audited "
            f"GetAvatar cases; expected={UNVERSIONED_OUTER_ARTIFACTS_BY_MATRIX}, "
            f"actual={actual_unversioned_outer}"
        )
    expected_used = {
        item["id"] for item in lock["artifacts"] if item["disposition"] == "testable"
    }
    if used != expected_used:
        raise artifact_lib.HarnessError(
            f"matrix coverage mismatch; missing={sorted(expected_used - used)}, "
            f"unexpected={sorted(used - expected_used)}"
        )
    for pair, orders in order_pairs.items():
        if len(orders) != 2 or orders[0] != list(reversed(orders[1])):
            raise artifact_lib.HarnessError(
                f"order pair {pair!r} must contain two exact reverse install orders"
            )
    return lock, document


def matrix_index(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {matrix["id"]: matrix for matrix in document["matrices"]}


def get_matrix(document: dict[str, Any], matrix_id: str) -> dict[str, Any]:
    matrix = matrix_index(document).get(matrix_id)
    if matrix is None:
        raise artifact_lib.HarnessError(f"unknown matrix: {matrix_id}")
    return matrix


def emit_scalar(value: Any) -> None:
    if isinstance(value, bool):
        print("true" if value else "false")
    elif value is None:
        print("")
    elif isinstance(value, (dict, list)):
        print(json.dumps(value, separators=(",", ":"), sort_keys=True))
    else:
        print(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--matrices", type=Path, default=DEFAULT_MATRICES)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("lint")
    subparsers.add_parser("list")
    coverage_parser = subparsers.add_parser("coverage")
    coverage_parser.add_argument("--pretty", action="store_true")
    matrix_parser = subparsers.add_parser("matrix")
    matrix_parser.add_argument("id")
    field_parser = subparsers.add_parser("field")
    field_parser.add_argument("id")
    field_parser.add_argument("field")
    order_parser = subparsers.add_parser("order")
    order_parser.add_argument("id")
    artifact_parser = subparsers.add_parser("artifacts")
    artifact_parser.add_argument("id")
    configuration_parser = subparsers.add_parser("configurations")
    configuration_parser.add_argument("id")
    probe_parser = subparsers.add_parser("probes")
    probe_parser.add_argument("id")
    runtime_parser = subparsers.add_parser("runtime-field")
    runtime_parser.add_argument("runtime", choices=("jf10", "jf12"))
    runtime_parser.add_argument("field")
    args = parser.parse_args()
    try:
        lock, document = load_and_validate(args.lock, args.matrices)
        if args.command == "lint":
            print(
                json.dumps(
                    {
                        "matrixCount": len(document["matrices"]),
                        "testableArtifactCount": sum(
                            item["disposition"] == "testable" for item in lock["artifacts"]
                        ),
                        "valid": True,
                    },
                    sort_keys=True,
                )
            )
        elif args.command == "list":
            for matrix in document["matrices"]:
                print(
                    "\t".join(
                        (
                            matrix["id"],
                            matrix["runtime"],
                            str(len(matrix["installOrder"]) - 1),
                            matrix["purpose"],
                        )
                    )
                )
        elif args.command == "coverage":
            payload = {
                "expectations": lock["coverageExpectations"],
                "plugins": lock["catalogCoverage"],
            }
            print(json.dumps(payload, indent=2 if args.pretty else None, sort_keys=True))
        elif args.command == "matrix":
            print(json.dumps(get_matrix(document, args.id), indent=2, sort_keys=True))
        elif args.command == "field":
            matrix = get_matrix(document, args.id)
            if args.field not in matrix:
                raise artifact_lib.HarnessError(f"{args.id}: no field {args.field}")
            emit_scalar(matrix[args.field])
        elif args.command == "order":
            print("\n".join(get_matrix(document, args.id)["installOrder"]))
        elif args.command == "artifacts":
            print(
                "\n".join(
                    item
                    for item in get_matrix(document, args.id)["installOrder"]
                    if item != "@refresh-kit"
                )
            )
        elif args.command == "configurations":
            matrix = get_matrix(document, args.id)
            indexed = artifact_lib.artifact_index(lock)
            for patch in matrix.get("configurationPatches", []):
                artifact_id = patch["artifactId"]
                print(
                    "\t".join(
                        (
                            artifact_id,
                            indexed[artifact_id]["plugin"]["guid"],
                            json.dumps(patch["payload"], separators=(",", ":"), sort_keys=True),
                        )
                    )
                )
        elif args.command == "probes":
            matrix = get_matrix(document, args.id)
            for probe in matrix.get("contentProbes", []):
                print(
                    "\t".join(
                        (
                            probe["id"],
                            probe["path"],
                            "true" if probe["authenticated"] else "false",
                        )
                    )
                )
        elif args.command == "runtime-field":
            runtime = document["runtimes"][args.runtime]
            if args.field not in runtime:
                raise artifact_lib.HarnessError(
                    f"{args.runtime}: no runtime field {args.field}"
                )
            emit_scalar(runtime[args.field])
        return 0
    except artifact_lib.HarnessError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
