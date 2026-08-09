#!/usr/bin/env python3
"""Validate and query the compatibility matrix manifest."""

from __future__ import annotations

import argparse
import hashlib
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
    "jf12-enhanced",
}
WRITABLE_WEBROOT_MATRIX_IDS = {"jf10-direct-writers-writable"}
UNVERSIONED_OUTER_ARTIFACTS_BY_MATRIX = {
    "jf10-middleware-forward": {"get-avatar-jf10"},
    "jf10-middleware-reverse": {"get-avatar-jf10"},
}
ABSENT_ARTIFACTS_BY_MATRIX = {
    "jf10-direct-writers-readonly": {
        "stream-limit-jf10",
        "aniliberty-strm-jf10",
        "jellyfin-tweaks-jf10",
        "whisper-subs-jf10",
    },
    "jf12-enhanced": {"stream-limit-jf12"},
}
PREVERSIONED_ARTIFACTS_BY_MATRIX = {
    "jf10-transform-core": {
        "home-screen-sections-jf10",
        "seerrfin-jf10",
    },
    "jf10-middleware-forward": {
        "jellyfin-enhanced-jf10",
        "achievement-badges-jf10",
        "ratings-jf10",
        "jmsfusion-jf10",
        "startrack-jf10",
    },
    "jf10-middleware-reverse": {
        "jellyfin-enhanced-jf10",
        "achievement-badges-jf10",
        "ratings-jf10",
        "jmsfusion-jf10",
        "startrack-jf10",
    },
    "jf12-enhanced": {"jellyfin-enhanced-jf12"},
    "jf10-response-transformers-forward": {"jellyfin-security-jf10"},
    "jf10-response-transformers-reverse": {"jellyfin-security-jf10"},
    "jf10-direct-writers-writable": {"aniliberty-strm-jf10"},
}
ASSEMBLY_VERSIONED_ARTIFACTS_BY_MATRIX = {
    "jf10-response-transformers-forward": {
        "powertoys-jellytag-jf10",
        "powertoys-privacy-mode-jf10",
        "powertoys-remote-trailers-jf10",
        "powertoys-thumbnail-previews-jf10",
    },
    "jf10-response-transformers-reverse": {
        "powertoys-jellytag-jf10",
        "powertoys-privacy-mode-jf10",
        "powertoys-remote-trailers-jf10",
        "powertoys-thumbnail-previews-jf10",
    },
}
EXTERNAL_ARTIFACTS_BY_MATRIX = {
    "jf10-transform-core": {"media-bar-jf10"},
}
SOURCE_VERSION_KEYS = {
    "v", "ver", "vers", "version", "rev", "revision", "hash", "build",
    "buildid", "cb", "cachebust", "cachebuster",
}
MATRIX_FIELDS = {
    "id",
    "runtime",
    "service",
    "webrootExpectation",
    "purpose",
    "installOrder",
    "orderPair",
    "expectedRuntimePluginOrder",
    "generationProbe",
    "stampingExpectation",
    "cacheExpectation",
    "requiredStampedArtifacts",
    "requiredUnversionedOuterArtifacts",
    "requiredPresentArtifacts",
    "requiredAbsentArtifacts",
    "requiredAssemblyVersionedArtifacts",
    "requiredPreVersionedArtifacts",
    "requiredBodyMarkers",
    "configurationPatches",
    "contentProbes",
    "shellRequirements",
    "inlineRequirements",
    "webrootDiskRequirements",
    "quarantinedAssertions",
}
EXPECTED_MATRIX_SEQUENCE = (
    "jf10-transform-core",
    "jf10-transform-whisper",
    "jf10-transform-hover",
    "jf10-transform-player",
    "jf10-transform-editors",
    "jf10-transform-actor",
    "jf10-middleware-forward",
    "jf10-middleware-reverse",
    "jf10-registration-broker",
    "jf12-enhanced",
    "jf10-response-transformers-forward",
    "jf10-response-transformers-reverse",
    "jf10-direct-writers-readonly",
    "jf10-direct-writers-writable",
)
EXPECTED_RUNTIME_PLUGIN_ORDER = {
    "jf10-middleware-forward": (
        "achievement-badges-jf10",
        "file-transformation-jf10",
        "get-avatar-jf10",
        "jellyfin-enhanced-jf10",
        "@refresh-kit",
        "jmsfusion-jf10",
        "ratings-jf10",
        "seasonals-jf10",
        "startrack-jf10",
    ),
    "jf10-middleware-reverse": (
        "achievement-badges-jf10",
        "file-transformation-jf10",
        "get-avatar-jf10",
        "jellyfin-enhanced-jf10",
        "@refresh-kit",
        "jmsfusion-jf10",
        "ratings-jf10",
        "seasonals-jf10",
        "startrack-jf10",
    ),
    "jf10-response-transformers-forward": (
        "@refresh-kit",
        "jellyfin-security-jf10",
        "powertoys-jellytag-jf10",
        "powertoys-privacy-mode-jf10",
        "powertoys-remote-trailers-jf10",
        "powertoys-thumbnail-previews-jf10",
    ),
    "jf10-response-transformers-reverse": (
        "@refresh-kit",
        "jellyfin-security-jf10",
        "powertoys-jellytag-jf10",
        "powertoys-privacy-mode-jf10",
        "powertoys-remote-trailers-jf10",
        "powertoys-thumbnail-previews-jf10",
    ),
}
EXPECTED_MATRIX_BASE = {
    "jf10-transform-core": ("jf10", "jf10", "read-only", "letterboxd-sync-jf10", "required", "required", None),
    "jf10-transform-whisper": ("jf10", "jf10", "read-only", "whisper-subs-jf10", "required", "required", None),
    "jf10-transform-hover": ("jf10", "jf10", "read-only", "hover-trailer-jf10", "required", "required", None),
    "jf10-transform-player": ("jf10", "jf10", "read-only", "in-player-episode-preview-jf10", "required", "required", None),
    "jf10-transform-editors": ("jf10", "jf10", "read-only", "editors-choice-jf10", "required", "required", None),
    "jf10-transform-actor": ("jf10", "jf10", "read-only", "actor-plus-jf10", "required", "required", None),
    "jf10-middleware-forward": ("jf10", "jf10", "read-only", "get-avatar-jf10", "required", "safe-degrade", "jf10-middleware"),
    "jf10-middleware-reverse": ("jf10", "jf10", "read-only", "get-avatar-jf10", "required", "safe-degrade", "jf10-middleware"),
    "jf10-registration-broker": ("jf10", "jf10", "read-only", "gelato-jf10", "observe", "required", None),
    "jf12-enhanced": ("jf12", "jf12", "read-only", "intro-skipper-jf12", "observe", "safe-degrade", None),
    "jf10-response-transformers-forward": ("jf10", "jf10", "read-only", "jellyfin-security-jf10", "observe", "required", "jf10-response-transformers"),
    "jf10-response-transformers-reverse": ("jf10", "jf10", "read-only", "jellyfin-security-jf10", "observe", "required", "jf10-response-transformers"),
    "jf10-direct-writers-readonly": ("jf10", "jf10", "read-only", "stream-limit-jf10", "observe", "required", None),
    "jf10-direct-writers-writable": ("jf10", "jf10-writable", "writable-volume", "stream-limit-jf10", "required", "required", None),
}
EXPECTED_INSTALL_ORDER_SHA256 = {
    "jf10-transform-core": "0a61d24b94eb3dd75b21c321207f978eefb9bfd770373108f98c8dd759e26ddf",
    "jf10-transform-whisper": "d6e02c4b0d81322a65518b945eef9ea2cbb2db109f86839072b4df8da4a3bc5e",
    "jf10-transform-hover": "1d4104ac59e2aeed9207444f32a48c44e1775c54272424ba72a4212774e67b8e",
    "jf10-transform-player": "8a9f0809f9a43b67bcc747f599030edef7170bd93783458d9124e5537d850803",
    "jf10-transform-editors": "e4164407a2eda057cb67bef3cb09c17520ec08f536095865bb6a98cd9281683a",
    "jf10-transform-actor": "c48d7b7efee42eba13d13ef4f9fb01025be5bf89bc6237aa084df792e12d21cb",
    "jf10-middleware-forward": "449554445dc03df55f50a1f9d394c3c3cadfeae0147836723df4e10096fe78eb",
    "jf10-middleware-reverse": "bfe83e0fa0467bab037b8052ca6572bba08493af1cc18c6d6ccc811444e5434e",
    "jf10-registration-broker": "e08df43cf23d7002f3727e7abb06120e3f8b73b0d9e3e7df849f4a9103ce392b",
    "jf12-enhanced": "860500c99501971b6c55e30f326d039266b0710e8a33cc7cb150b99953d30645",
    "jf10-response-transformers-forward": "b83d4780e97286639d825b440df384cab00469c6b8cc1273bfe65fcec4458751",
    "jf10-response-transformers-reverse": "12558365329eea2dcac2efc755174328e88b3e8d3247971539a473b26c8f0ae2",
    "jf10-direct-writers-readonly": "b231ee80d5150067c045c0a4be6b02beab0e8d0e165859c1e906ecdecb458dbf",
    "jf10-direct-writers-writable": "b231ee80d5150067c045c0a4be6b02beab0e8d0e165859c1e906ecdecb458dbf",
}
CONTRACT_FIELDS = (
    "id", "runtime", "service", "webrootExpectation", "purpose", "installOrder",
    "orderPair", "expectedRuntimePluginOrder", "generationProbe",
    "stampingExpectation", "cacheExpectation",
    "requiredStampedArtifacts", "requiredUnversionedOuterArtifacts",
    "requiredPresentArtifacts", "requiredAbsentArtifacts",
    "requiredAssemblyVersionedArtifacts",
    "requiredPreVersionedArtifacts", "requiredBodyMarkers", "configurationPatches",
    "contentProbes", "shellRequirements", "inlineRequirements",
    "webrootDiskRequirements", "quarantinedAssertions",
)
EXPECTED_MATRIX_CONTRACT_SHA256 = "8fab51792f72d5892a45281b88c0d09c45dfb42d7cf4bb834849854c256ae0b1"


def reject_unknown_fields(value: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise artifact_lib.HarnessError(f"{context}: unknown fields: {unknown}")


def matrix_contract_digest(document: dict[str, Any]) -> str:
    projection = []
    for matrix in document["matrices"]:
        row: dict[str, Any] = {}
        for field in CONTRACT_FIELDS:
            if field == "orderPair":
                default: Any = None
            elif field.startswith("required") or field in {
                "configurationPatches",
                "contentProbes",
            }:
                default = []
            elif field.endswith("Requirements"):
                default = {}
            else:
                default = None
            row[field] = matrix.get(field, default)
        projection.append(row)
    contract = {
        "runtimes": document["runtimes"],
        "matrices": projection,
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_and_validate(lock_path: Path, matrix_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    lock = artifact_lib.validate_lock(lock_path)
    document = artifact_lib.load_json(matrix_path)
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise artifact_lib.HarnessError("matrices must be a schemaVersion 1 object")
    reject_unknown_fields(document, {"schemaVersion", "runtimes", "matrices"}, "matrices")
    runtimes = document.get("runtimes")
    matrices = document.get("matrices")
    if not isinstance(runtimes, dict) or set(runtimes) != {"jf10", "jf12"}:
        raise artifact_lib.HarnessError("matrices must declare exactly jf10 and jf12 runtimes")
    if not isinstance(matrices, list) or not matrices:
        raise artifact_lib.HarnessError("matrices array is empty")

    for runtime, details in runtimes.items():
        if not isinstance(details, dict):
            raise artifact_lib.HarnessError(f"{runtime}: runtime details must be an object")
        reject_unknown_fields(
            details,
            {
                "serverVersion",
                "serverVersionRegex",
                "image",
                "imageDigest",
                "refreshKitTargetAbi",
                "refreshKitFramework",
                "defaultStage",
            },
            f"{runtime} runtime",
        )
        image = str(details.get("image", ""))
        digest = str(details.get("imageDigest", ""))
        if f"@sha256:{digest}" not in image or not artifact_lib.SHA256_RE.fullmatch(digest):
            raise artifact_lib.HarnessError(f"{runtime}: image is not pinned to imageDigest")
        if not str(details.get("serverVersionRegex", "")).startswith("^"):
            raise artifact_lib.HarnessError(f"{runtime}: serverVersionRegex must be anchored")
        server_version = details.get("serverVersion")
        if (
            not isinstance(server_version, str)
            or re.fullmatch(
                r"[0-9]+(?:\.[0-9]+){2,3}(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?",
                server_version,
            )
            is None
        ):
            raise artifact_lib.HarnessError(f"{runtime}: invalid serverVersion")
        try:
            version_pattern = re.compile(str(details["serverVersionRegex"]))
        except re.error as exc:
            raise artifact_lib.HarnessError(
                f"{runtime}: invalid serverVersionRegex: {exc}"
            ) from exc
        if version_pattern.search(server_version) is None:
            raise artifact_lib.HarnessError(
                f"{runtime}: serverVersionRegex does not match serverVersion"
            )
        target_abi = details.get("refreshKitTargetAbi")
        if (
            not isinstance(target_abi, str)
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", target_abi) is None
        ):
            raise artifact_lib.HarnessError(f"{runtime}: invalid refreshKitTargetAbi")
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
        reject_unknown_fields(matrix, MATRIX_FIELDS, matrix_id or "matrix row")
        matrix_ids.append(matrix_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", matrix_id):
            raise artifact_lib.HarnessError(f"unsafe matrix id: {matrix_id!r}")
        purpose = matrix.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            raise artifact_lib.HarnessError(f"{matrix_id}: purpose must be nonempty")
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
        expected_runtime_order = matrix.get("expectedRuntimePluginOrder")
        locked_runtime_order = EXPECTED_RUNTIME_PLUGIN_ORDER.get(matrix_id)
        if locked_runtime_order is None:
            if expected_runtime_order is not None:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: runtime plugin order is allowed only on audited order pairs"
                )
        elif (
            not isinstance(expected_runtime_order, list)
            or tuple(expected_runtime_order) != locked_runtime_order
            or len(expected_runtime_order) != len(set(expected_runtime_order))
            or set(expected_runtime_order) != set(order)
        ):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: expected runtime plugin order differs from the "
                "manifest-name-sorted contract"
            )
        generation_probe = matrix.get("generationProbe")
        if generation_probe not in order or generation_probe == "@refresh-kit":
            raise artifact_lib.HarnessError(f"{matrix_id}: invalid generationProbe")
        expectation = matrix.get("stampingExpectation")
        if expectation not in {"required", "observe"}:
            raise artifact_lib.HarnessError(f"{matrix_id}: invalid stampingExpectation")
        if matrix.get("cacheExpectation") not in {"required", "observe", "safe-degrade"}:
            raise artifact_lib.HarnessError(f"{matrix_id}: invalid cacheExpectation")
        required = matrix.get("requiredStampedArtifacts")
        if (
            not isinstance(required, list)
            or len(required) != len(set(required))
            or any(artifact_id not in order for artifact_id in required)
        ):
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
        required_absent = matrix.get("requiredAbsentArtifacts", [])
        required_assembly_versioned = matrix.get("requiredAssemblyVersionedArtifacts", [])
        required_preversioned = matrix.get("requiredPreVersionedArtifacts", [])
        shell_requirements = {
            "requiredStampedArtifacts": required,
            "requiredUnversionedOuterArtifacts": required_unversioned,
            "requiredPresentArtifacts": required_present,
            "requiredAbsentArtifacts": required_absent,
            "requiredAssemblyVersionedArtifacts": required_assembly_versioned,
            "requiredPreVersionedArtifacts": required_preversioned,
        }
        for field, artifact_ids in shell_requirements.items():
            if (
                not isinstance(artifact_ids, list)
                or len(artifact_ids) != len(set(artifact_ids))
                or any(
                    artifact_id == "@refresh-kit" or artifact_id not in order
                    for artifact_id in artifact_ids
                )
            ):
                raise artifact_lib.HarnessError(f"{matrix_id}: invalid {field}")
        requirement_owners: dict[str, str] = {}
        for field, artifact_ids in shell_requirements.items():
            for artifact_id in artifact_ids:
                previous = requirement_owners.setdefault(artifact_id, field)
                if previous != field:
                    raise artifact_lib.HarnessError(
                        f"{matrix_id}: {artifact_id} cannot be required by both "
                        f"{previous} and {field}"
                    )
        structured_requirements = matrix.get("shellRequirements", {})
        if not isinstance(structured_requirements, dict):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: shellRequirements must be an object"
            )
        seen_selectors: dict[str, str] = {}
        for artifact_id, requirement in structured_requirements.items():
            if artifact_id == "@refresh-kit" or artifact_id not in order:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: shell requirement artifact is not installed: {artifact_id}"
                )
            if not isinstance(requirement, dict):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: shell requirement for {artifact_id} must be an object"
                )
            reject_unknown_fields(
                requirement,
                {"mode", "cardinality", "selectors"},
                f"{matrix_id}/{artifact_id} shell requirement",
            )
            mode = requirement.get("mode")
            if mode not in {
                "current-rkv",
                "source-versioned",
                "assembly-versioned-path",
                "external-present",
                "unversioned-outer",
                "absent",
            }:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}/{artifact_id}: invalid shell requirement mode"
                )
            cardinality = requirement.get("cardinality")
            selectors = requirement.get("selectors")
            if (
                not isinstance(cardinality, int)
                or isinstance(cardinality, bool)
                or cardinality < 0
                or not isinstance(selectors, list)
                or not selectors
            ):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}/{artifact_id}: invalid shell cardinality/selectors"
                )
            selector_total = 0
            for selector_index, selector in enumerate(selectors):
                context = f"{matrix_id}/{artifact_id} selector {selector_index}"
                if not isinstance(selector, dict):
                    raise artifact_lib.HarnessError(f"{context}: must be an object")
                reject_unknown_fields(
                    selector,
                    {"tag", "origin", "path", "query", "cardinality"},
                    context,
                )
                if selector.get("tag") not in {"script", "link"}:
                    raise artifact_lib.HarnessError(f"{context}: invalid tag")
                origin = str(selector.get("origin", ""))
                if origin != "same-origin" and re.fullmatch(
                    r"https://[a-z0-9.-]+(?::[0-9]+)?", origin
                ) is None:
                    raise artifact_lib.HarnessError(f"{context}: invalid origin")
                path = selector.get("path")
                if (
                    not isinstance(path, str)
                    or not path
                    or "?" in path
                    or "#" in path
                ):
                    raise artifact_lib.HarnessError(f"{context}: invalid exact path")
                selector_cardinality = selector.get("cardinality")
                if (
                    not isinstance(selector_cardinality, int)
                    or isinstance(selector_cardinality, bool)
                    or selector_cardinality < 0
                ):
                    raise artifact_lib.HarnessError(
                        f"{context}: invalid selector cardinality"
                    )
                selector_total += selector_cardinality
                query = selector.get("query")
                if not isinstance(query, dict):
                    raise artifact_lib.HarnessError(f"{context}: query must be an object")
                reject_unknown_fields(
                    query, {"requiredKeys", "allowedKeys", "equals"}, f"{context} query"
                )
                required_keys = query.get("requiredKeys")
                allowed_keys = query.get("allowedKeys")
                equals = query.get("equals")
                for label, keys in (
                    ("requiredKeys", required_keys),
                    ("allowedKeys", allowed_keys),
                ):
                    if (
                        not isinstance(keys, list)
                        or len(keys) != len(set(keys))
                        or any(
                            not isinstance(key, str)
                            or key != key.casefold()
                            or re.fullmatch(r"[a-z][a-z0-9_-]*", key) is None
                            for key in keys
                        )
                    ):
                        raise artifact_lib.HarnessError(f"{context}: invalid {label}")
                if not set(required_keys).issubset(allowed_keys):
                    raise artifact_lib.HarnessError(
                        f"{context}: required query keys must be allowed"
                    )
                if (
                    not isinstance(equals, dict)
                    or not set(equals).issubset(required_keys)
                    or any(
                        not isinstance(value, str) or not value
                        for value in equals.values()
                    )
                ):
                    raise artifact_lib.HarnessError(f"{context}: invalid query equals")
                if mode == "current-rkv" and (
                    origin != "same-origin" or "rkv" not in required_keys
                ):
                    raise artifact_lib.HarnessError(
                        f"{context}: current-rkv requires same-origin and required rkv"
                    )
                if mode == "source-versioned" and (
                    origin != "same-origin"
                    or len(SOURCE_VERSION_KEYS & set(required_keys)) != 1
                    or "rkv" in allowed_keys
                ):
                    raise artifact_lib.HarnessError(
                        f"{context}: source-versioned requires one source version query"
                    )
                if mode == "assembly-versioned-path" and (
                    origin != "same-origin"
                    or allowed_keys
                    or re.fullmatch(
                        r"/_/[0-9a-f]{32}/[0-9a-f]{32}\.(?:css|js)", path
                    )
                    is None
                ):
                    raise artifact_lib.HarnessError(
                        f"{context}: assembly-versioned-path requires an exact same-origin "
                        "two-digest JS/CSS path with no query"
                    )
                if mode == "external-present" and origin == "same-origin":
                    raise artifact_lib.HarnessError(
                        f"{context}: external-present requires an explicit external origin"
                    )
                if mode == "unversioned-outer" and (
                    origin != "same-origin" or allowed_keys
                ):
                    raise artifact_lib.HarnessError(
                        f"{context}: unversioned-outer cannot allow query parameters"
                    )
                canonical_selector = json.dumps(
                    {
                        "tag": selector["tag"],
                        "origin": origin.casefold(),
                        "path": path.casefold(),
                        "requiredKeys": sorted(required_keys),
                        "allowedKeys": sorted(allowed_keys),
                        "equals": {
                            key.casefold(): value
                            for key, value in sorted(equals.items())
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if canonical_selector in seen_selectors:
                    raise artifact_lib.HarnessError(
                        f"{context}: duplicate/ambiguous exact selector"
                    )
                seen_selectors[canonical_selector] = artifact_id
            if selector_total != cardinality:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}/{artifact_id}: selector cardinalities do not sum to artifact cardinality"
                )
            if (mode == "absent") != (cardinality == 0):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}/{artifact_id}: only absent requirements may have zero cardinality"
                )

        derived_requirement_groups = {
            "requiredStampedArtifacts": {
                artifact_id for artifact_id, row in structured_requirements.items()
                if row["mode"] == "current-rkv"
            },
            "requiredUnversionedOuterArtifacts": {
                artifact_id for artifact_id, row in structured_requirements.items()
                if row["mode"] == "unversioned-outer"
            },
            "requiredPresentArtifacts": {
                artifact_id for artifact_id, row in structured_requirements.items()
                if row["mode"] == "external-present"
            },
            "requiredAbsentArtifacts": {
                artifact_id for artifact_id, row in structured_requirements.items()
                if row["mode"] == "absent"
            },
            "requiredAssemblyVersionedArtifacts": {
                artifact_id for artifact_id, row in structured_requirements.items()
                if row["mode"] == "assembly-versioned-path"
            },
            "requiredPreVersionedArtifacts": {
                artifact_id for artifact_id, row in structured_requirements.items()
                if row["mode"] == "source-versioned"
            },
        }
        for field, derived in derived_requirement_groups.items():
            if set(shell_requirements[field]) != derived:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: {field} disagrees with structured shell requirements"
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
        inline_requirements = matrix.get("inlineRequirements", {})
        if not isinstance(inline_requirements, dict):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: inlineRequirements must be an object"
            )
        for artifact_id, requirement in inline_requirements.items():
            if artifact_id == "@refresh-kit" or artifact_id not in order:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: inline requirement artifact is not installed: {artifact_id}"
                )
            if not isinstance(requirement, dict):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}/{artifact_id}: inline requirement must be an object"
                )
            reject_unknown_fields(
                requirement,
                {"cardinality", "markers", "ordered"},
                f"{matrix_id}/{artifact_id} inline requirement",
            )
            cardinality = requirement.get("cardinality")
            markers = requirement.get("markers")
            if (
                not isinstance(cardinality, int)
                or isinstance(cardinality, bool)
                or cardinality < 1
                or not isinstance(markers, list)
                or not markers
                or len(markers) != len(set(markers))
                or any(not isinstance(marker, str) or not marker for marker in markers)
                or not isinstance(requirement.get("ordered"), bool)
            ):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}/{artifact_id}: invalid inline requirement"
                )
        expected_body_markers = {
            artifact_id: requirement["markers"]
            for artifact_id, requirement in inline_requirements.items()
        }
        if body_markers != expected_body_markers:
            raise artifact_lib.HarnessError(
                f"{matrix_id}: requiredBodyMarkers disagrees with inlineRequirements"
            )

        disk_requirements = matrix.get("webrootDiskRequirements", {})
        if not isinstance(disk_requirements, dict):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: webrootDiskRequirements must be an object"
            )
        for artifact_id, requirement in disk_requirements.items():
            if artifact_id == "@refresh-kit" or artifact_id not in order:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: disk requirement artifact is not installed: {artifact_id}"
                )
            if not isinstance(requirement, dict):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}/{artifact_id}: disk requirement must be an object"
                )
            reject_unknown_fields(
                requirement,
                {"mode", "cardinality", "markers"},
                f"{matrix_id}/{artifact_id} disk requirement",
            )
            disk_mode = requirement.get("mode")
            cardinality = requirement.get("cardinality")
            markers = requirement.get("markers")
            if (
                disk_mode not in {"absent", "added"}
                or not isinstance(cardinality, int)
                or isinstance(cardinality, bool)
                or cardinality != (0 if disk_mode == "absent" else 1)
                or not isinstance(markers, list)
                or not markers
                or len(markers) != len(set(markers))
                or any(not isinstance(marker, str) or not marker for marker in markers)
            ):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}/{artifact_id}: invalid disk requirement"
                )
        if bool(disk_requirements) != matrix_id.startswith("jf10-direct-writers-"):
            raise artifact_lib.HarnessError(
                f"{matrix_id}: disk requirements are mandatory only for direct-writer matrices"
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
            reject_unknown_fields(
                patch,
                {"artifactId", "payload"},
                f"{matrix_id} configuration patch",
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
            reject_unknown_fields(
                probe,
                {
                    "id",
                    "path",
                    "authenticated",
                    "format",
                    "jsonArrayContains",
                    "markers",
                },
                f"{matrix_id} content probe",
            )
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
            probe_format = probe.get("format", "text")
            if probe_format not in {"text", "json-object"}:
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: content probe format must be text or json-object"
                )
            json_array_contains = probe.get("jsonArrayContains", {})
            if (
                not isinstance(json_array_contains, dict)
                or (json_array_contains and probe_format != "json-object")
                or any(
                    not isinstance(key, str)
                    or not key
                    or not isinstance(values, list)
                    or not values
                    or len(values) != len(set(values))
                    or any(not isinstance(value, str) or not value for value in values)
                    for key, values in json_array_contains.items()
                )
            ):
                raise artifact_lib.HarnessError(
                    f"{matrix_id}: invalid JSON array content contract"
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
    if tuple(matrix_ids) != EXPECTED_MATRIX_SEQUENCE:
        raise artifact_lib.HarnessError(
            "matrix ids/order must remain exactly the audited 14-matrix sequence"
        )
    for matrix in matrices:
        matrix_id = matrix["id"]
        actual_base = (
            matrix["runtime"],
            matrix["service"],
            matrix["webrootExpectation"],
            matrix["generationProbe"],
            matrix["stampingExpectation"],
            matrix["cacheExpectation"],
            matrix.get("orderPair"),
        )
        if actual_base != EXPECTED_MATRIX_BASE[matrix_id]:
            raise artifact_lib.HarnessError(
                f"{matrix_id}: runtime/service/webroot/probe/stamping/cache/order-pair "
                "contract changed"
            )
        order_digest = hashlib.sha256(
            json.dumps(matrix["installOrder"], separators=(",", ":")).encode()
        ).hexdigest()
        if order_digest != EXPECTED_INSTALL_ORDER_SHA256[matrix_id]:
            raise artifact_lib.HarnessError(
                f"{matrix_id}: exact audited install order changed"
            )
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
    actual_external = {
        str(matrix["id"]): set(matrix.get("requiredPresentArtifacts", []))
        for matrix in matrices
        if matrix.get("requiredPresentArtifacts")
    }
    if actual_external != EXTERNAL_ARTIFACTS_BY_MATRIX:
        raise artifact_lib.HarnessError(
            "external shell assertions must remain exactly the audited Media Bar case; "
            f"expected={EXTERNAL_ARTIFACTS_BY_MATRIX}, actual={actual_external}"
        )
    actual_absent = {
        str(matrix["id"]): set(matrix.get("requiredAbsentArtifacts", []))
        for matrix in matrices
        if matrix.get("requiredAbsentArtifacts")
    }
    if actual_absent != ABSENT_ARTIFACTS_BY_MATRIX:
        raise artifact_lib.HarnessError(
            "required-absent shell assertions must remain exactly the audited "
            f"read-only direct-writer cases; expected={ABSENT_ARTIFACTS_BY_MATRIX}, "
            f"actual={actual_absent}"
        )
    actual_assembly_versioned = {
        str(matrix["id"]): set(matrix.get("requiredAssemblyVersionedArtifacts", []))
        for matrix in matrices
        if matrix.get("requiredAssemblyVersionedArtifacts")
    }
    if actual_assembly_versioned != ASSEMBLY_VERSIONED_ARTIFACTS_BY_MATRIX:
        raise artifact_lib.HarnessError(
            "assembly-versioned shell assertions must remain exactly the audited "
            f"PowerToys paths; expected={ASSEMBLY_VERSIONED_ARTIFACTS_BY_MATRIX}, "
            f"actual={actual_assembly_versioned}"
        )
    actual_preversioned = {
        str(matrix["id"]): set(matrix.get("requiredPreVersionedArtifacts", []))
        for matrix in matrices
        if matrix.get("requiredPreVersionedArtifacts")
    }
    if actual_preversioned != PREVERSIONED_ARTIFACTS_BY_MATRIX:
        raise artifact_lib.HarnessError(
            "source-preversioned shell assertions must remain exactly the audited "
            f"local version-query cases; expected={PREVERSIONED_ARTIFACTS_BY_MATRIX}, "
            f"actual={actual_preversioned}"
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
    contract_digest = matrix_contract_digest(document)
    if contract_digest != EXPECTED_MATRIX_CONTRACT_SHA256:
        raise artifact_lib.HarnessError(
            "exact runtime/matrix/body/config/content/shell/inline/disk/quarantine "
            "compatibility contract changed; "
            f"expected={EXPECTED_MATRIX_CONTRACT_SHA256}, actual={contract_digest}"
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
                        "contractSha256": matrix_contract_digest(document),
                        "matrixCount": len(document["matrices"]),
                        "shellRequirementCount": sum(
                            len(matrix.get("shellRequirements", {}))
                            for matrix in document["matrices"]
                        ),
                        "inlineRequirementCount": sum(
                            len(matrix.get("inlineRequirements", {}))
                            for matrix in document["matrices"]
                        ),
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
