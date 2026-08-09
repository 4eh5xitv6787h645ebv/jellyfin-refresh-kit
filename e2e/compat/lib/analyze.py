#!/usr/bin/env python3
"""Build structured stage, runtime, failure, and aggregate compatibility results."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import urllib.parse
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import artifacts as artifact_lib
import manifest as manifest_lib


HERE = Path(__file__).resolve().parent
COMPAT_ROOT = HERE.parent
LOCK_PATH = COMPAT_ROOT / "ecosystem.lock.json"
MATRICES_PATH = COMPAT_ROOT / "matrices.json"
REFRESH_KIT_GUID = "515255fe-3332-49b0-b471-0be58c8221d8"
VERSIONISH_KEYS = {
    "v", "ver", "vers", "version", "rev", "revision", "hash", "build",
    "buildid", "cb", "cachebust", "cachebuster", "nocache", "_",
}
CONTENT_HASH_RE = re.compile(r"(?:^|[.\-])[0-9a-f]{8,}(?=(?:\.[a-z0-9_-]+)+$)", re.I)


def write_json(path: Path, payload: Any) -> None:
    artifact_lib.write_json(path, payload)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise artifact_lib.HarnessError(f"cannot read {path}: {exc}") from exc


def read_json(path: Path) -> Any:
    return artifact_lib.load_json(path)


def normalize_guid(value: Any) -> str:
    return re.sub(r"[^0-9a-f]", "", str(value or "").casefold())


def ci(mapping: Any, key: str, default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    value = artifact_lib.ci_value(mapping, key)
    return default if value is None else value


def sha256(path: Path) -> str:
    return artifact_lib.sha256_path(path)


def require_file(path: Path) -> None:
    if not path.is_file():
        raise artifact_lib.HarnessError(f"missing evidence file: {path}")


class ShellParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.assets: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle(tag, attrs)

    def _handle(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = {key.casefold(): value or "" for key, value in attrs}
        url = ""
        if tag.casefold() == "script":
            url = normalized.get("src", "")
        elif tag.casefold() == "link":
            rel = {part.casefold() for part in normalized.get("rel", "").split()}
            if "stylesheet" in rel:
                url = normalized.get("href", "")
        if url:
            self.assets.append(
                {"position": len(self.assets), "tag": tag.casefold(), "url": html.unescape(url)}
            )


def parse_headers(path: Path) -> dict[str, list[str]]:
    headers: dict[str, list[str]] = {}
    for raw_line in read_text(path).splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        headers.setdefault(key.strip().casefold(), []).append(value.strip())
    return headers


def parse_http_status(path: Path) -> int | None:
    statuses = []
    for raw_line in read_text(path).splitlines():
        match = re.fullmatch(r"HTTP/\S+\s+([0-9]{3})(?:\s+.*)?", raw_line.strip())
        if match is not None:
            statuses.append(int(match.group(1)))
    return statuses[-1] if statuses else None


def has_cache_control_directive(headers: dict[str, list[str]], directive: str) -> bool:
    wanted = directive.casefold()
    return any(
        part.split("=", 1)[0].strip().casefold() == wanted
        for value in headers.get("cache-control", [])
        for part in value.split(",")
    )


def response_framing_mode(headers: dict[str, list[str]], body: bytes) -> str | None:
    """Return the one accepted HTTP/1.1 framing mode, or None when ambiguous."""
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


def evaluate_safe_degrade(
    primary_status: int | None,
    primary_headers: dict[str, list[str]],
    primary_body: bytes,
    conditional_status: str,
    conditional_http_status: int | None,
    conditional_headers: dict[str, list[str]],
    conditional_body: bytes,
    conditional_text: str,
    generation: str,
    primary_assets: Counter[str],
    conditional_assets: Counter[str],
) -> dict[str, bool]:
    return {
        "primaryStatus200": primary_status == 200,
        "primaryFramingValid": response_framing_mode(primary_headers, primary_body)
        is not None,
        "primaryCacheControlNoStore": has_cache_control_directive(
            primary_headers, "no-store"
        ),
        "primaryEtagAbsent": not primary_headers.get("etag"),
        "primaryLastModifiedAbsent": not primary_headers.get("last-modified"),
        "conditionalStatus200": conditional_status == "200"
        and conditional_http_status == 200,
        "conditionalFramingValid": response_framing_mode(
            conditional_headers, conditional_body
        ) is not None,
        "conditionalCacheControlNoStore": has_cache_control_directive(
            conditional_headers, "no-store"
        ),
        "conditionalEtagAbsent": not conditional_headers.get("etag"),
        "conditionalLastModifiedAbsent": not conditional_headers.get("last-modified"),
        "conditionalBodyNonEmpty": len(conditional_body) > 0,
        "conditionalHtmlDocument": "<html" in conditional_text.casefold()
        and "</html>" in conditional_text.casefold(),
        "conditionalSingleRefreshKitTag": conditional_text.count(
            'plugin="Jellyfin Refresh Kit"'
        ) == 1,
        "conditionalNamedRuntime": 'data-name="RefreshKitPlugin"' in conditional_text,
        "conditionalBootGeneration":
            f'data-boot-version="{generation}"' in conditional_text,
        "conditionalGenerationAddressedKit":
            f"/RefreshKit/kit.js?v={generation}" in conditional_text,
        # Outer plugins may emit the same tags in a different order on a later
        # request. Compare the complete asset multiset, not serialization order.
        "conditionalAssetMultisetMatchesPrimary": conditional_assets == primary_assets,
    }


def generation_from(path: Path) -> str:
    payload = read_json(path)
    value = ci(payload, "CacheKey")
    if not isinstance(value, str) or not re.fullmatch(r"g-[0-9a-f]{16}", value):
        raise artifact_lib.HarnessError(f"invalid Refresh Kit generation in {path}: {value!r}")
    return value


def query_values(url: str, key: str) -> list[str]:
    try:
        return [value for actual, value in urllib.parse.parse_qsl(
            urllib.parse.urlsplit(url).query, keep_blank_values=True
        ) if actual.casefold() == key.casefold()]
    except ValueError:
        return []


def eligible_unversioned(url: str) -> bool:
    if not url or url.startswith("//"):
        return False
    split = urllib.parse.urlsplit(url)
    if split.scheme or split.netloc:
        return False
    pairs = urllib.parse.parse_qsl(split.query, keep_blank_values=True)
    if any(key.casefold() in VERSIONISH_KEYS for key, _ in pairs):
        return False
    if split.query and "=" not in split.query:
        return False
    filename = PureName(split.path)
    return CONTENT_HASH_RE.search(filename) is None


def PureName(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def plugin_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        items = ci(payload, "Items", [])
        if isinstance(items, list):
            return [row for row in items if isinstance(row, dict)]
    return []


def find_by_guid(rows: list[dict[str, Any]], guid: str) -> list[dict[str, Any]]:
    expected = normalize_guid(guid)
    return [row for row in rows if normalize_guid(ci(row, "Id")) == expected]


def cmd_stage(args: argparse.Namespace) -> int:
    _, matrices = manifest_lib.load_and_validate(LOCK_PATH, MATRICES_PATH)
    runtime = matrices["runtimes"][args.runtime]
    stage = args.stage.resolve()
    meta_path = stage / "meta.json"
    dll_path = stage / "Jellyfin.Plugin.RefreshKit.dll"
    require_file(meta_path)
    require_file(dll_path)
    meta = read_json(meta_path)
    checks = {
        "guid": normalize_guid(ci(meta, "guid")) == normalize_guid(REFRESH_KIT_GUID),
        "name": ci(meta, "name") == "Jellyfin Refresh Kit",
        "targetAbi": artifact_lib.normalized_abi(ci(meta, "targetAbi"))
        == runtime["refreshKitTargetAbi"],
        "framework": ci(meta, "framework") == runtime["refreshKitFramework"],
        "version": artifact_lib.VERSION_RE.fullmatch(str(ci(meta, "version", ""))) is not None,
    }
    failures = [key for key, passed in checks.items() if not passed]
    if failures:
        raise artifact_lib.HarnessError(f"Refresh Kit stage metadata failed: {failures}")
    dll = dll_path.read_bytes()
    version = str(ci(meta, "version"))
    token_checks = {
        "guid": REFRESH_KIT_GUID.encode("utf-16-le") in dll or REFRESH_KIT_GUID.encode() in dll,
        "version": version.encode("utf-16-le") in dll or version.encode() in dll,
    }
    if not all(token_checks.values()):
        raise artifact_lib.HarnessError("Refresh Kit DLL does not contain locked GUID/version tokens")
    result = {
        "schemaVersion": 1,
        "runtime": args.runtime,
        "stage": str(stage),
        "meta": meta,
        "checks": checks,
        "binaryTokenChecks": token_checks,
        "dllSha256": hashlib.sha256(dll).hexdigest(),
        "valid": True,
    }
    write_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


def parse_install_tsv(path: Path) -> list[dict[str, Any]]:
    result = []
    for line_number, line in enumerate(read_text(path).splitlines(), 1):
        parts = line.split("\t")
        if len(parts) != 4:
            raise artifact_lib.HarnessError(f"{path}:{line_number}: expected four TSV fields")
        ordinal, artifact_id, remote_folder, report_path = parts
        result.append(
            {
                "ordinal": int(ordinal),
                "artifactId": artifact_id,
                "remoteFolder": remote_folder,
                "reportPath": report_path,
            }
        )
    return result


def shell_attribution(
    assets: list[dict[str, Any]], artifact: dict[str, Any], generation: str
) -> dict[str, Any]:
    needles = artifact["shellUrlNeedles"]
    matches = [
        row for row in assets
        if any(needle in row["url"].casefold() for needle in needles)
    ]
    for row in matches:
        stamps = query_values(row["url"], "rkv")
        row["rkv"] = stamps
        row["stampMatchesGeneration"] = stamps == [generation]
        row["eligibleWithoutRefreshKitStamp"] = eligible_unversioned(
            re.sub(r"([?&])rkv=[^&#]*&?", lambda match: match.group(1), row["url"], flags=re.I)
            .replace("?&", "?")
            .rstrip("?&")
        )
    return {
        "needles": needles,
        "tags": matches,
        "tagCount": len(matches),
        "currentStampCount": sum(row["stampMatchesGeneration"] for row in matches),
        "unstampedEligibleCount": sum(
            row["eligibleWithoutRefreshKitStamp"] and not row["stampMatchesGeneration"]
            for row in matches
        ),
    }


def evaluate_unversioned_outer_limitation(shell: dict[str, Any]) -> dict[str, bool]:
    return {
        "singleTagPresent": shell.get("tagCount") == 1,
        "currentStampAbsent": shell.get("currentStampCount") == 0,
        "singleEligibleUnversionedTag": shell.get("unstampedEligibleCount") == 1,
    }


def evaluate_current_stamped(shell: dict[str, Any]) -> dict[str, bool]:
    tag_count = shell.get("tagCount")
    return {
        "tagPresent": isinstance(tag_count, int) and tag_count > 0,
        "everyMatchedTagHasCurrentStamp": isinstance(tag_count, int)
        and tag_count > 0
        and shell.get("currentStampCount") == tag_count,
    }


def evaluate_source_preversioned(shell: dict[str, Any]) -> dict[str, bool]:
    tags = shell.get("tags")
    return {
        "tagPresent": isinstance(tags, list) and len(tags) > 0,
        "refreshKitStampAbsent": isinstance(tags, list)
        and bool(tags)
        and all(not row.get("rkv") for row in tags if isinstance(row, dict))
        and all(isinstance(row, dict) for row in tags),
        "sourceVersionMakesStampIneligible": shell.get("unstampedEligibleCount") == 0,
    }


def cmd_runtime(args: argparse.Namespace) -> int:
    lock, matrices = manifest_lib.load_and_validate(LOCK_PATH, MATRICES_PATH)
    matrix = manifest_lib.get_matrix(matrices, args.matrix)
    runtime = matrix["runtime"]
    runtime_lock = matrices["runtimes"][runtime]
    artifacts = artifact_lib.artifact_index(lock)
    evidence = args.evidence.resolve()
    required_names = (
        "artifact-verification.json",
        "network.json",
        "stage.json",
        "install.tsv",
        "system.json",
        "plugins.json",
        "diagnostics-before.json",
        "diagnostics-after.json",
        "generation-before.json",
        "generation-after.json",
        "shell-after.html",
        "shell-after.headers",
        "shell-gzip.html",
        "shell-br.html",
        "kit.js",
        "kit.headers",
        "conditional-status.txt",
        "conditional.headers",
        "conditional.html",
        "image.txt",
    )
    for name in required_names:
        require_file(evidence / name)

    editors_choice_configuration: dict[str, Any] | None = None
    if args.matrix == "jf10-transform-editors":
        preload_path = evidence / "editors-choice-preload.xml"
        configuration_path = evidence / "editors-choice-configuration.json"
        require_file(preload_path)
        require_file(configuration_path)
        expected_preload = COMPAT_ROOT / "fixtures" / "configurations" / "EditorsChoicePlugin.xml"
        editors_choice_configuration = {
            "preloadMatchesFixture": sha256(preload_path) == sha256(expected_preload),
            "effective": read_json(configuration_path),
        }

    errors: list[str] = []
    matrix_configurations: dict[str, Any] = {}
    for patch in matrix.get("configurationPatches", []):
        artifact_id = patch["artifactId"]
        configuration_path = evidence / "configurations" / f"{artifact_id}.json"
        require_file(configuration_path)
        effective = read_json(configuration_path)
        checks = {
            key: ci(effective, key) == expected
            for key, expected in patch["payload"].items()
        }
        matrix_configurations[artifact_id] = {
            "expected": patch["payload"],
            "effective": effective,
            "checks": checks,
            "allPassed": all(checks.values()),
        }
        for key, passed in checks.items():
            if not passed:
                errors.append(f"{artifact_id}: configuration check failed: {key}")
    if editors_choice_configuration is not None:
        effective = editors_choice_configuration["effective"]
        configuration_checks = {
            "preloadMatchesFixture": editors_choice_configuration["preloadMatchesFixture"],
            "directInjectionDisabled": ci(effective, "DoScriptInject") is False,
            "fileTransformationEnabled": ci(effective, "FileTransformation") is True,
        }
        editors_choice_configuration["checks"] = configuration_checks
        editors_choice_configuration["allPassed"] = all(configuration_checks.values())
        for key, passed in configuration_checks.items():
            if not passed:
                errors.append(f"Editor's Choice configuration check failed: {key}")
    system = read_json(evidence / "system.json")
    server_version = str(ci(system, "Version", ""))
    if re.search(runtime_lock["serverVersionRegex"], server_version) is None:
        errors.append(
            f"server version {server_version!r} does not match {runtime_lock['serverVersionRegex']}"
        )
    configured_image = read_text(evidence / "image.txt").strip()
    if f"sha256:{runtime_lock['imageDigest']}" not in configured_image:
        errors.append("container image does not contain the locked digest")

    network = read_json(evidence / "network.json")
    network_checks = {
        "valid": network.get("valid") is True and network.get("allPassed") is True,
        "project": network.get("project", "").startswith("rk-compat-"),
        "service": network.get("service") == matrix["service"],
        "runtime": network.get("runtime") == runtime,
        "imageDigest": network.get("expectedImageDigest") == runtime_lock["imageDigest"],
        "composeLabels": network.get("composeLabelsValid") is True,
        "internalBridge": network.get("internalBridge") is True,
        "noGateway": network.get("noGateway") is True,
        "exclusiveProjectNetwork": network.get("exclusiveProjectNetwork") is True,
        "originMode": network.get("originMode")
        in ("published-loopback", "verified-internal-bridge"),
    }
    for key, passed in network_checks.items():
        if not passed:
            errors.append(f"network isolation check failed: {key}")

    generation_before = generation_from(evidence / "generation-before.json")
    generation_after = generation_from(evidence / "generation-after.json")
    if generation_before == generation_after:
        errors.append("generation did not change after a loose third-party JavaScript asset changed")

    shell_text = read_text(evidence / "shell-after.html")
    shell_parser = ShellParser()
    shell_parser.feed(shell_text)
    own_tag_count = shell_text.count('plugin="Jellyfin Refresh Kit"')
    shell_checks = {
        "htmlDocument": "<html" in shell_text.casefold() and "</html>" in shell_text.casefold(),
        "singleRefreshKitTag": own_tag_count == 1,
        "namedRuntime": 'data-name="RefreshKitPlugin"' in shell_text,
        "bootGeneration": f'data-boot-version="{generation_after}"' in shell_text,
        "generationAddressedKit": f"/RefreshKit/kit.js?v={generation_after}" in shell_text,
    }
    for key, passed in shell_checks.items():
        if not passed:
            errors.append(f"shell check failed: {key}")

    compression_checks = {
        "gzipShellInjected": 'plugin="Jellyfin Refresh Kit"'
        in read_text(evidence / "shell-gzip.html"),
        "brotliShellInjected": 'plugin="Jellyfin Refresh Kit"'
        in read_text(evidence / "shell-br.html"),
    }
    for key, passed in compression_checks.items():
        if not passed:
            errors.append(f"compression check failed: {key}")

    headers = parse_headers(evidence / "shell-after.headers")
    etags = headers.get("etag", [])
    conditional_status = read_text(evidence / "conditional-status.txt").strip()
    conditional_headers = parse_headers(evidence / "conditional.headers")
    shell_body = (evidence / "shell-after.html").read_bytes()
    conditional_body = (evidence / "conditional.html").read_bytes()
    conditional_text = read_text(evidence / "conditional.html")
    conditional_parser = ShellParser()
    conditional_parser.feed(conditional_text)
    cache_checks = {
        "refreshKitEtag": len(etags) == 1 and "rk-" in etags[0],
        "conditional304": conditional_status == "304",
    }
    if matrix["cacheExpectation"] == "required":
        for key, passed in cache_checks.items():
            if not passed:
                errors.append(f"cache check failed: {key}")

    safe_degrade_checks: dict[str, bool] = {}
    safe_degrade_framing: dict[str, str | None] = {}
    if matrix["cacheExpectation"] == "safe-degrade":
        primary_assets = Counter(row["url"] for row in shell_parser.assets)
        conditional_assets = Counter(row["url"] for row in conditional_parser.assets)
        safe_degrade_checks = evaluate_safe_degrade(
            parse_http_status(evidence / "shell-after.headers"),
            headers,
            shell_body,
            conditional_status,
            parse_http_status(evidence / "conditional.headers"),
            conditional_headers,
            conditional_body,
            conditional_text,
            generation_after,
            primary_assets,
            conditional_assets,
        )
        safe_degrade_framing = {
            "primary": response_framing_mode(headers, shell_body),
            "conditional": response_framing_mode(conditional_headers, conditional_body),
        }
        for key, passed in safe_degrade_checks.items():
            if not passed:
                errors.append(f"safe-degrade cache check failed: {key}")

    kit_text = read_text(evidence / "kit.js")
    kit_headers = parse_headers(evidence / "kit.headers")
    kit_checks = {
        "runtimePresent": re.search(r"KIT_VERSION\s*=\s*'[^']+'", kit_text) is not None,
        "immutable": any("immutable" in value.casefold() for value in kit_headers.get("cache-control", [])),
    }
    for key, passed in kit_checks.items():
        if not passed:
            errors.append(f"kit check failed: {key}")

    content_probe_results: dict[str, Any] = {}
    for probe in matrix.get("contentProbes", []):
        probe_path = evidence / "content-probes" / f"{probe['id']}.txt"
        require_file(probe_path)
        probe_text = read_text(probe_path)
        artifact_checks = {
            artifact_id: {
                marker: marker in probe_text for marker in markers
            }
            for artifact_id, markers in probe["markers"].items()
        }
        content_probe_results[probe["id"]] = {
            "path": probe["path"],
            "authenticated": probe["authenticated"],
            "sha256": sha256(probe_path),
            "bytes": probe_path.stat().st_size,
            "artifactChecks": artifact_checks,
            "allPassed": all(
                passed
                for checks in artifact_checks.values()
                for passed in checks.values()
            ),
        }
        for artifact_id, checks in artifact_checks.items():
            for marker, passed in checks.items():
                if not passed:
                    errors.append(
                        f"{artifact_id}: content probe {probe['id']} missed marker {marker!r}"
                    )

    inventory_rows = plugin_rows(read_json(evidence / "plugins.json"))
    diagnostics_before = plugin_rows(ci(read_json(evidence / "diagnostics-before.json"), "Plugins", []))
    diagnostics_after = plugin_rows(ci(read_json(evidence / "diagnostics-after.json"), "Plugins", []))
    verification_rows = {
        row["id"]: row for row in read_json(evidence / "artifact-verification.json")["artifacts"]
    }
    installs = parse_install_tsv(evidence / "install.tsv")
    requested_order = matrix["installOrder"]
    installed_order = [row["artifactId"] for row in sorted(installs, key=lambda row: row["ordinal"])]
    if installed_order != requested_order:
        errors.append(f"installed order {installed_order} != requested order {requested_order}")

    stage = read_json(evidence / "stage.json")
    refresh_inventory = find_by_guid(inventory_rows, REFRESH_KIT_GUID)
    refresh_diagnostics = find_by_guid(diagnostics_after, REFRESH_KIT_GUID)
    refresh_checks = {
        "stageValid": stage.get("valid") is True,
        "inventoryExact": len(refresh_inventory) == 1,
        "diagnosticsExact": len(refresh_diagnostics) == 1,
        "diagnosticsLoaded": len(refresh_diagnostics) == 1
        and ci(refresh_diagnostics[0], "IsLoaded") is True,
        **shell_checks,
        **compression_checks,
        **cache_checks,
        **safe_degrade_checks,
        **kit_checks,
        "generationChangedForLooseAsset": generation_before != generation_after,
    }
    for key in (
        "stageValid",
        "inventoryExact",
        "diagnosticsExact",
        "diagnosticsLoaded",
        "generationChangedForLooseAsset",
    ):
        if not refresh_checks[key]:
            errors.append(f"Refresh Kit check failed: {key}")

    per_plugin = []
    required_unversioned_outer = set(
        matrix.get("requiredUnversionedOuterArtifacts", [])
    )
    required_present = set(matrix.get("requiredPresentArtifacts", []))
    required_absent = set(matrix.get("requiredAbsentArtifacts", []))
    required_preversioned = set(matrix.get("requiredPreVersionedArtifacts", []))
    observed_shell_order: list[str] = []
    for asset in shell_parser.assets:
        for artifact_id in requested_order:
            if artifact_id == "@refresh-kit":
                continue
            if any(
                needle in asset["url"].casefold()
                for needle in artifacts[artifact_id]["shellUrlNeedles"]
            ):
                observed_shell_order.append(artifact_id)
                break

    for artifact_id in requested_order:
        if artifact_id == "@refresh-kit":
            continue
        artifact = artifacts[artifact_id]
        plugin = artifact["plugin"]
        plugin_errors: list[str] = []
        inventory = find_by_guid(inventory_rows, plugin["guid"])
        before_rows = find_by_guid(diagnostics_before, plugin["guid"])
        after_rows = find_by_guid(diagnostics_after, plugin["guid"])
        if len(inventory) != 1:
            plugin_errors.append(f"inventory matches={len(inventory)}")
        if len(after_rows) != 1:
            plugin_errors.append(f"diagnostics matches={len(after_rows)}")
        inventory_row = inventory[0] if len(inventory) == 1 else {}
        diagnostics_row = after_rows[0] if len(after_rows) == 1 else {}
        actual_name = ci(inventory_row, "Name")
        actual_version = str(ci(inventory_row, "Version", ""))
        actual_status = str(ci(inventory_row, "Status", ""))
        if actual_name != plugin["name"]:
            plugin_errors.append(f"name {actual_name!r} != {plugin['name']!r}")
        if actual_version != plugin["version"]:
            plugin_errors.append(f"version {actual_version!r} != {plugin['version']!r}")
        if actual_status.casefold() != "active":
            plugin_errors.append(f"status {actual_status!r} is not Active")
        if ci(diagnostics_row, "IsLoaded") is not True:
            plugin_errors.append("Refresh Kit diagnostics does not report IsLoaded=true")

        verification = verification_rows.get(artifact_id)
        if not isinstance(verification, dict) or verification.get("verified") is not True:
            plugin_errors.append("artifact preflight verification is missing")
        install = next((row for row in installs if row["artifactId"] == artifact_id), None)
        materialization = read_json(Path(install["reportPath"])) if install else {}
        if materialization.get("materialized") is not True:
            plugin_errors.append("materialization/meta verification is missing")
        runtime_meta_path = evidence / "runtime-meta" / f"{artifact_id}.json"
        runtime_meta = read_json(runtime_meta_path) if runtime_meta_path.is_file() else {}
        runtime_meta_checks = {
            "guid": normalize_guid(ci(runtime_meta, "guid")) == normalize_guid(plugin["guid"]),
            "name": ci(runtime_meta, "name") == plugin["name"],
            "version": str(ci(runtime_meta, "version", "")) == plugin["version"],
            "targetAbi": artifact_lib.normalized_abi(ci(runtime_meta, "targetAbi"))
            == plugin["targetAbi"],
        }
        for key, passed in runtime_meta_checks.items():
            if not passed:
                plugin_errors.append(f"runtime meta check failed: {key}")

        shell = shell_attribution(shell_parser.assets, artifact, generation_after)
        shell_requirement_checks: dict[str, bool] = {}
        if artifact_id in matrix["requiredStampedArtifacts"]:
            shell_requirement_checks = evaluate_current_stamped(shell)
            for key, passed in shell_requirement_checks.items():
                if not passed:
                    plugin_errors.append(f"required generation-stamp check failed: {key}")
        if artifact_id in required_present and shell["tagCount"] < 1:
            plugin_errors.append("required shell tag was not observed")
        if artifact_id in required_absent:
            shell_requirement_checks = {"tagAbsent": shell["tagCount"] == 0}
            if not shell_requirement_checks["tagAbsent"]:
                plugin_errors.append("forbidden read-only direct-writer tag was observed")
        if artifact_id in required_preversioned:
            shell_requirement_checks = evaluate_source_preversioned(shell)
            for key, passed in shell_requirement_checks.items():
                if not passed:
                    plugin_errors.append(f"source-preversioned shell check failed: {key}")
        body_markers = {
            marker: marker in shell_text
            for marker in matrix.get("requiredBodyMarkers", {}).get(artifact_id, [])
        }
        for marker, present in body_markers.items():
            if not present:
                plugin_errors.append(f"required inline body marker was not observed: {marker!r}")
        limitation: dict[str, Any] | None = None
        if artifact_id in required_unversioned_outer:
            limitation_checks = evaluate_unversioned_outer_limitation(shell)
            for key, passed in limitation_checks.items():
                if not passed:
                    plugin_errors.append(
                        f"outer-owner unversioned limitation check failed: {key}"
                    )
            limitation = {
                "code": "outer-owner-unversioned-shell-tag",
                "artifactId": artifact_id,
                "status": "pass-with-limitation"
                if all(limitation_checks.values())
                else "fail",
                "reason": (
                    "The plugin injects its client tag after Refresh Kit's transform "
                    "boundary, so the required single tag is present but cannot carry rkv."
                ),
                "checks": limitation_checks,
            }

        asset_generation = {"probe": artifact_id == matrix["generationProbe"]}
        if asset_generation["probe"]:
            before_identity = ci(before_rows[0], "AssetIdentity") if len(before_rows) == 1 else None
            after_identity = ci(after_rows[0], "AssetIdentity") if len(after_rows) == 1 else None
            asset_generation.update(
                {
                    "beforeIdentity": before_identity,
                    "afterIdentity": after_identity,
                    "changed": bool(before_identity and after_identity and before_identity != after_identity),
                }
            )
            if not asset_generation["changed"]:
                plugin_errors.append("generation probe did not change the plugin asset identity")

        per_plugin.append(
            {
                "artifactId": artifact_id,
                "catalogName": artifact["catalogName"],
                "expected": plugin,
                "artifactVerification": verification,
                "materialization": materialization,
                "runtimeMeta": {"checks": runtime_meta_checks, "value": runtime_meta},
                "inventory": inventory_row,
                "diagnostics": diagnostics_row,
                "assetGeneration": asset_generation,
                "shell": shell,
                "shellRequirementChecks": shell_requirement_checks,
                "bodyMarkers": body_markers,
                "limitation": limitation,
                "install": install,
                "errors": plugin_errors,
                "outcome": "fail"
                if plugin_errors
                else "pass-with-limitation"
                if limitation is not None
                else "pass",
            }
        )
        errors.extend(f"{artifact_id}: {message}" for message in plugin_errors)

    limitations = [
        row["limitation"] for row in per_plugin if row["limitation"] is not None
    ]
    outcome = (
        "fail"
        if errors
        else "pass-with-limitation"
        if limitations
        else "pass"
    )
    result = {
        "schemaVersion": 1,
        "matrix": args.matrix,
        "runtime": runtime,
        "service": matrix["service"],
        "webrootExpectation": matrix["webrootExpectation"],
        "serverVersion": server_version,
        "image": configured_image,
        "network": {"value": network, "checks": network_checks},
        "requestedInstallOrder": requested_order,
        "observedShellTagOrder": observed_shell_order,
        "orderPair": matrix.get("orderPair"),
        "quarantinedAssertions": matrix["quarantinedAssertions"],
        "cacheExpectation": matrix["cacheExpectation"],
        "matrixConfiguration": editors_choice_configuration,
        "matrixConfigurations": matrix_configurations,
        "contentProbes": content_probe_results,
        "refreshKit": {
            "expectedGuid": REFRESH_KIT_GUID,
            "generationBefore": generation_before,
            "generationAfter": generation_after,
            "checks": refresh_checks,
            "cacheEvidence": {
                "expectation": matrix["cacheExpectation"],
                "primary": {
                    "status": parse_http_status(evidence / "shell-after.headers"),
                    "bodyBytes": len(shell_body),
                    "bodySha256": hashlib.sha256(shell_body).hexdigest(),
                    "framingMode": safe_degrade_framing.get("primary"),
                },
                "conditional": {
                    "status": parse_http_status(evidence / "conditional.headers"),
                    "bodyBytes": len(conditional_body),
                    "bodySha256": hashlib.sha256(conditional_body).hexdigest(),
                    "framingMode": safe_degrade_framing.get("conditional"),
                },
                "safeDegradeChecks": safe_degrade_checks,
            },
            "stage": stage,
        },
        "coexistence": {
            "expectedPluginCount": len(per_plugin),
            "loadedPluginCount": sum(
                ci(row["diagnostics"], "IsLoaded") is True for row in per_plugin
            ),
            "shellAssetCount": len(shell_parser.assets),
            "serverRemainedHealthy": not any("server version" in error for error in errors),
        },
        "plugins": per_plugin,
        "limitations": limitations,
        "errors": errors,
        "outcome": outcome,
    }
    write_json(args.output, result)
    print(json.dumps({"matrix": args.matrix, "outcome": result["outcome"], "errors": errors}))
    return 0 if not errors else 1


def cmd_failure(args: argparse.Namespace) -> int:
    payload = {
        "schemaVersion": 1,
        "matrix": args.matrix,
        "phase": args.phase,
        "errors": [args.message],
        "outcome": "fail",
    }
    write_json(args.output, payload)
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    lock, matrices = manifest_lib.load_and_validate(LOCK_PATH, MATRICES_PATH)
    results = []
    missing = []
    for matrix in matrices["matrices"]:
        path = args.results / matrix["id"] / "result.json"
        if path.is_file():
            results.append(read_json(path))
        else:
            missing.append(matrix["id"])
    accepted_outcomes = {"pass", "pass-with-limitation"}
    failed = [
        row.get("matrix") for row in results if row.get("outcome") not in accepted_outcomes
    ]
    expected_limited = [
        matrix["id"]
        for matrix in matrices["matrices"]
        if matrix.get("requiredUnversionedOuterArtifacts")
    ]
    limited = [
        row.get("matrix")
        for row in results
        if row.get("outcome") == "pass-with-limitation"
    ]
    missing_limited = [matrix_id for matrix_id in expected_limited if matrix_id not in limited]
    unexpected_limited = [matrix_id for matrix_id in limited if matrix_id not in expected_limited]
    expected_safe_degraded = [
        matrix["id"]
        for matrix in matrices["matrices"]
        if matrix["cacheExpectation"] == "safe-degrade"
    ]
    safe_degraded = [
        row.get("matrix")
        for row in results
        if row.get("outcome") in accepted_outcomes
        and row.get("cacheExpectation") == "safe-degrade"
    ]
    missing_safe_degraded = [
        matrix_id for matrix_id in expected_safe_degraded if matrix_id not in safe_degraded
    ]
    payload = {
        "schemaVersion": 1,
        "coverage": lock["coverageExpectations"],
        "expectedMatrices": [matrix["id"] for matrix in matrices["matrices"]],
        "completedMatrices": [row.get("matrix") for row in results],
        "missingMatrices": missing,
        "failedMatrices": failed,
        "expectedPassWithLimitationMatrices": expected_limited,
        "passWithLimitationMatrices": limited,
        "missingPassWithLimitationMatrices": missing_limited,
        "unexpectedPassWithLimitationMatrices": unexpected_limited,
        "expectedSafeDegradedMatrices": expected_safe_degraded,
        "safeDegradedMatrices": safe_degraded,
        "missingSafeDegradedMatrices": missing_safe_degraded,
        "results": results,
        "outcome": "pass-with-limitation"
        if not missing
        and not failed
        and not missing_limited
        and not unexpected_limited
        and not missing_safe_degraded
        and limited
        else "pass"
        if not missing
        and not failed
        and not missing_limited
        and not unexpected_limited
        and not missing_safe_degraded
        else "fail",
    }
    write_json(args.output, payload)
    return 0 if payload["outcome"] in accepted_outcomes else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)

    stage = subparsers.add_parser("stage")
    stage.add_argument("runtime", choices=("jf10", "jf12"))
    stage.add_argument("stage", type=Path)
    stage.add_argument("output", type=Path)
    stage.set_defaults(func=cmd_stage)

    runtime = subparsers.add_parser("runtime")
    runtime.add_argument("matrix")
    runtime.add_argument("evidence", type=Path)
    runtime.add_argument("output", type=Path)
    runtime.set_defaults(func=cmd_runtime)

    failure = subparsers.add_parser("failure")
    failure.add_argument("matrix")
    failure.add_argument("phase")
    failure.add_argument("message")
    failure.add_argument("output", type=Path)
    failure.set_defaults(func=cmd_failure)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("results", type=Path)
    aggregate.add_argument("output", type=Path)
    aggregate.set_defaults(func=cmd_aggregate)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return int(args.func(args))
    except artifact_lib.HarnessError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
