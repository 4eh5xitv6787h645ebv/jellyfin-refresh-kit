#!/usr/bin/env python3
"""Reusable fail-closed validation for retained release evidence."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
from typing import Any

from host_upgrade_evidence import (
    HostUpgradeEvidenceError,
    validate_evidence as validate_host_upgrade_evidence,
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
    "host-upgrade/result.json",
    "host-upgrade/jf10/result.json",
    "host-upgrade/jf12/result.json",
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
INTEGRATION_LOGS = ("dual-jellyfin", "host-upgrade", "proxy")
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
COMPAT_MATRIX_FILES = (
    "static.json",
    "stage.json",
    "network.json",
    "artifact-verification.json",
    "result.json",
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
    expected_lab = {pathlib.PurePosixPath(name).as_posix() for name in (*INTEGRATION_JSON, *INTEGRATION_IMAGES)}
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
        validate_host_upgrade_evidence(lab / "host-upgrade", build)
    except HostUpgradeEvidenceError as error:
        raise EvidenceValidationError(str(error)) from error
    return {f"lab/{name}" for name in expected_lab} | {f"logs/{name}" for name in expected_logs}


def _matrix_contract(matrices_path: pathlib.Path) -> tuple[list[str], dict[str, str], dict[str, str], dict[str, list[str]]]:
    root = load_object(matrices_path)
    rows = root.get("matrices")
    require(isinstance(rows, list), "compatibility matrices has no array")
    ids: list[str] = []
    runtimes: dict[str, str] = {}
    cache: dict[str, str] = {}
    limitations: dict[str, list[str]] = {}
    for row in rows:
        require(isinstance(row, dict), "compatibility matrix row is not an object")
        matrix_id, runtime, expectation = row.get("id"), row.get("runtime"), row.get("cacheExpectation")
        require(isinstance(matrix_id, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]*", matrix_id) is not None
                and matrix_id not in runtimes, f"invalid/repeated compatibility matrix id: {matrix_id!r}")
        require(runtime in ("jf10", "jf12"), f"compatibility matrix {matrix_id} runtime is invalid")
        require(expectation in ("required", "observe", "safe-degrade"),
                f"compatibility matrix {matrix_id} cache expectation is invalid")
        artifacts = row.get("requiredUnversionedOuterArtifacts", [])
        require(isinstance(artifacts, list) and all(isinstance(item, str) for item in artifacts),
                f"compatibility matrix {matrix_id} limitations are invalid")
        ids.append(matrix_id)
        runtimes[matrix_id] = runtime
        cache[matrix_id] = expectation
        if artifacts:
            limitations[matrix_id] = artifacts
    return ids, runtimes, cache, limitations


def validate_compatibility_tree(
    root: pathlib.Path, build: pathlib.Path, matrices_path: pathlib.Path
) -> set[str]:
    root, build = root.resolve(strict=True), build.resolve(strict=True)
    compat = root / "compat"
    ids, runtimes, cache_expectations, limitations = _matrix_contract(matrices_path)
    expected = {"summary.json"} | {
        f"{matrix_id}/{name}" for matrix_id in ids for name in COMPAT_MATRIX_FILES
    }
    actual = {
        path.relative_to(compat).as_posix() for path in compat.rglob("*") if path.is_file()
    } if compat.is_dir() else set()
    require(actual == expected,
            f"compatibility inventory differs: missing={sorted(expected-actual)}, unexpected={sorted(actual-expected)}")
    safe_degraded = [matrix_id for matrix_id in ids if cache_expectations[matrix_id] == "safe-degrade"]
    limited = [matrix_id for matrix_id in ids if matrix_id in limitations]
    summary = load_object(compat / "summary.json")
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
    }
    for field, wanted in summary_expected.items():
        require(summary.get(field) == wanted, f"compatibility summary {field} differs")

    for matrix_id in ids:
        directory = compat / matrix_id
        static = load_object(directory / "static.json")
        stage = load_object(directory / "stage.json")
        network = load_object(directory / "network.json")
        artifact = load_object(directory / "artifact-verification.json")
        result = load_object(directory / "result.json")
        require(static.get("schemaVersion") == 1 and static.get("allPassed") is True,
                f"{matrix_id}: static evidence failed")
        require(network.get("schemaVersion") == 1
                and network.get("valid") is True and network.get("allPassed") is True,
                f"{matrix_id}: network evidence failed")
        require(artifact.get("schemaVersion") == 1 and artifact.get("allPassed") is True,
                f"{matrix_id}: artifact verification failed")
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
                and result.get("serverVersion") == SERVER_VERSIONS[runtime]
                and result.get("image") == IMAGE_REFERENCES[runtime]
                and result.get("errors") == []
                and result.get("outcome") == expected_outcome,
                f"{matrix_id}: result outcome differs")
        require(network.get("service") == runtime
                and network.get("configuredImage") == IMAGE_REFERENCES[runtime]
                and network.get("expectedImageDigest") == IMAGE_DIGESTS[runtime]
                and network.get("internalBridge") is True
                and network.get("noGateway") is True
                and network.get("originMode") == "verified-internal-bridge"
                and network.get("publishedLoopbackActive") is False,
                f"{matrix_id}: network isolation identity differs")
        require(result.get("cacheExpectation") == cache_expectations[matrix_id],
                f"{matrix_id}: result cache expectation differs")
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
        if cache_expectations[matrix_id] == "safe-degrade":
            refresh = result.get("refreshKit")
            cache = refresh.get("cacheEvidence") if isinstance(refresh, dict) else None
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
    return {f"compat/{name}" for name in expected}
