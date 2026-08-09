#!/usr/bin/env python3
"""Validate the isolated Jellyfin 10.11.0 ABI-floor smoke evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import re
import sys
import tempfile
from typing import Any


IMAGE_DIGEST = "59417f441213e236a9f907d4e71a13472042409d85f9e9310dbdd87ee33d7bd4"
IMAGE_REFERENCE = f"jellyfin/jellyfin:10.11.0@sha256:{IMAGE_DIGEST}"
PLUGIN_GUID = "515255fe333249b0b4710be58c8221d8"
SCENARIO = "jellyfin-10.11.0-abi-floor"
REQUIRED_FILES = (
    "result.json",
    "server/result.json",
    "server/public.json",
    "server/generation.json",
    "server/generation.headers",
    "server/diagnostics.json",
    "server/plugins.json",
    "server/kit.headers",
    "server/kit.js",
    "server/shell.headers",
    "server/conditional.headers",
    "server/index.html",
    "server.log",
)
GENERATION = re.compile(r"^g-[0-9a-f]{16}$")
HEX_32 = re.compile(r"^[0-9a-f]{32}$")
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


class AbiFloorEvidenceError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AbiFloorEvidenceError(message)


def load_object(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AbiFloorEvidenceError(f"cannot read ABI-floor evidence {path}: {error}") from error
    require(isinstance(value, dict), f"ABI-floor evidence is not an object: {path}")
    return value


def load_array(path: pathlib.Path) -> list[Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AbiFloorEvidenceError(f"cannot read ABI-floor evidence {path}: {error}") from error
    require(isinstance(value, list), f"ABI-floor evidence is not an array: {path}")
    return value


def load_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise AbiFloorEvidenceError(f"cannot read ABI-floor evidence {path}: {error}") from error


def header_values(text: str, name: str) -> list[str]:
    expected = name.lower()
    return [
        value.strip()
        for line in text.splitlines()
        for key, separator, value in (line.partition(":"),)
        if separator and key.strip().lower() == expected
    ]


def response_statuses(text: str) -> list[int]:
    return [
        int(match.group(1))
        for line in text.splitlines()
        if (match := re.fullmatch(r"HTTP/\S+\s+([0-9]{3})(?:\s+.*)?", line.strip()))
    ]


def file_hash(path: pathlib.Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def field(value: dict[str, Any], name: str) -> Any:
    return value.get(name, value.get(name[:1].lower() + name[1:]))


def normalized_guid(value: Any) -> str:
    return str(value or "").replace("-", "").lower()


def plugin_rows(value: Any, label: str) -> list[dict[str, Any]]:
    require(isinstance(value, list), f"{label} is not an array")
    matches = [
        row for row in value
        if isinstance(row, dict) and normalized_guid(field(row, "Id")) == PLUGIN_GUID
    ]
    require(len(matches) == 1, f"{label} Refresh Kit inventory is not exact")
    return matches


def expected_stage(build: pathlib.Path) -> tuple[dict[str, Any], dict[str, Any]]:
    stage = build / "stage"
    meta = load_object(stage / "meta.json")
    require(meta.get("framework") == "net9.0" and meta.get("targetAbi") == "10.11.0.0",
            "pinned ABI-floor stage is not net9.0 / 10.11.0.0")
    version = meta.get("version")
    require(isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version),
            "pinned ABI-floor stage has no four-component version")
    dll = stage / "Jellyfin.Plugin.RefreshKit.dll"
    package = build / f"jellyfin-refresh-kit_{version}.zip"
    require(dll.is_file() and package.is_file(), "pinned ABI-floor DLL/package is missing")
    identity = {
        "meta": meta,
        "dllSha256": file_hash(dll),
        "package": {
            "name": package.name,
            "bytes": package.stat().st_size,
            "md5": file_hash(package, "md5"),
            "sha256": file_hash(package),
        },
    }
    return meta, identity


def validate_container(value: Any) -> None:
    require(isinstance(value, dict), "ABI-floor container identity is missing")
    project = value.get("project")
    require(isinstance(project, str) and re.fullmatch(r"rk-jellyfin-[a-z0-9_-]+", project),
            "ABI-floor Compose project identity is invalid")
    require(value.get("service") == "abi-floor", "ABI-floor Compose service identity differs")
    labels = value.get("labels")
    require(isinstance(labels, dict)
            and labels.get("com.docker.compose.project") == project
            and labels.get("com.docker.compose.service") == "abi-floor",
            "ABI-floor container labels differ")
    binding = value.get("portBinding")
    require(isinstance(binding, dict)
            and binding.get("containerPort") == "8096/tcp"
            and binding.get("hostIp") == "127.0.0.1"
            and isinstance(binding.get("hostPort"), str)
            and re.fullmatch(r"[1-9][0-9]{3,4}", binding["hostPort"])
            and 1024 <= int(binding["hostPort"]) <= 65535,
            "ABI-floor service is not bound to one safe loopback port")
    volumes = value.get("volumes")
    require(isinstance(volumes, list) and len(volumes) == 2
            and all(isinstance(row, dict) for row in volumes),
            "ABI-floor volume inventory differs")
    by_destination = {row.get("destination"): row for row in volumes}
    require(set(by_destination) == {"/config", "/cache"} and len(by_destination) == len(volumes),
            "ABI-floor config/cache volume destinations differ")
    for destination, suffix in (("/config", "abi-floor-config"), ("/cache", "abi-floor-cache")):
        row = by_destination[destination]
        require(row.get("type") == "volume"
                and isinstance(row.get("name"), str)
                and row["name"].endswith("_" + suffix),
                f"ABI-floor {destination} is not its dedicated Compose volume")
    network = value.get("network")
    require(isinstance(network, dict)
            and isinstance(network.get("name"), str)
            and network["name"].endswith("_abi-floor-internal")
            and isinstance(network.get("id"), str)
            and HEX_64.fullmatch(network["id"]) is not None
            and isinstance(network.get("endpointId"), str)
            and HEX_64.fullmatch(network["endpointId"]) is not None
            and network.get("internal") is True,
            "ABI-floor service is not attached to one dedicated internal network")
    network_labels = network.get("labels")
    require(isinstance(network_labels, dict)
            and network_labels.get("com.docker.compose.project") == project
            and network_labels.get("com.docker.compose.network") == "abi-floor-internal",
            "ABI-floor internal-network labels differ")


def validate_evidence(root: pathlib.Path, build: pathlib.Path) -> None:
    """Validate exact runtime, package, endpoint, inventory, and server-log proof."""
    try:
        root, build = root.resolve(strict=True), build.resolve(strict=True)
    except OSError as error:
        raise AbiFloorEvidenceError(f"cannot resolve ABI-floor evidence/build: {error}") from error
    actual = {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }
    require(actual == set(REQUIRED_FILES),
            f"ABI-floor evidence inventory differs: "
            f"missing={sorted(set(REQUIRED_FILES)-actual)}, "
            f"unexpected={sorted(actual-set(REQUIRED_FILES))}")
    result = load_object(root / "result.json")
    server = load_object(root / "server" / "result.json")
    public = load_object(root / "server" / "public.json")
    generation = load_object(root / "server" / "generation.json")
    diagnostics = load_object(root / "server" / "diagnostics.json")
    plugins = load_array(root / "server" / "plugins.json")
    generation_headers = load_text(root / "server" / "generation.headers")
    kit_headers = load_text(root / "server" / "kit.headers")
    kit = load_text(root / "server" / "kit.js")
    shell_headers = load_text(root / "server" / "shell.headers")
    conditional_headers = load_text(root / "server" / "conditional.headers")
    shell = load_text(root / "server" / "index.html")
    log = load_text(root / "server.log")

    require(result.get("schemaVersion") == 1 and result.get("scenario") == SCENARIO,
            "ABI-floor result schema/scenario differs")
    require(result.get("completed") is True and result.get("failures") == [],
            "ABI-floor result did not complete cleanly")
    finished = result.get("finishedUtc")
    require(isinstance(finished, str) and UTC_TIMESTAMP.fullmatch(finished) is not None,
            "ABI-floor completion timestamp is invalid")
    require(result.get("immutableSnapshot") == build.name,
            "ABI-floor result names a different immutable snapshot")

    meta, stage = expected_stage(build)
    source = {
        "revision": meta.get("sourceRevision"),
        "treeSha256": meta.get("sourceTreeSha256"),
        "dateEpoch": meta.get("sourceDateEpoch"),
        "dirty": meta.get("sourceDirty"),
    }
    require(isinstance(source["revision"], str) and HEX_40.fullmatch(source["revision"]) is not None
            and isinstance(source["treeSha256"], str)
            and HEX_64.fullmatch(source["treeSha256"]) is not None
            and isinstance(source["dateEpoch"], int) and not isinstance(source["dateEpoch"], bool)
            and isinstance(source["dirty"], bool),
            "pinned ABI-floor source identity is invalid")
    require(result.get("sourceIdentity") == source, "ABI-floor source identity differs")
    require(result.get("stage") == stage, "ABI-floor stage/package bytes differ")

    image = result.get("image")
    require(isinstance(image, dict)
            and image.get("reference") == IMAGE_REFERENCE
            and image.get("digest") == f"sha256:{IMAGE_DIGEST}"
            and image.get("configuredReference") == IMAGE_REFERENCE
            and isinstance(image.get("imageId"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", image["imageId"])
            and isinstance(image.get("repoDigests"), list)
            and any(isinstance(row, str) and row.endswith("@sha256:" + IMAGE_DIGEST)
                    for row in image["repoDigests"]),
            "ABI-floor exact image identity differs")
    validate_container(result.get("container"))

    require(field(public, "Version") == "10.11.0"
            and server.get("target") == "abi-floor"
            and server.get("serverVersion") == field(public, "Version")
            and server.get("endpointAndShellChecksPassed") is True
            and server.get("conditionalStatus") == 304
            and isinstance(server.get("shellEtag"), str) and "rk-" in server["shellEtag"],
            "ABI-floor public-server/endpoint result differs")
    cache_key = field(generation, "CacheKey")
    build_id = field(generation, "BuildId")
    epoch = field(generation, "Epoch")
    require(field(generation, "Version") == meta["version"]
            and isinstance(cache_key, str) and GENERATION.fullmatch(cache_key) is not None
            and isinstance(build_id, str) and HEX_64.fullmatch(build_id) is not None
            and isinstance(epoch, str) and HEX_32.fullmatch(epoch) is not None
            and server.get("generation") == cache_key,
            "ABI-floor generation identity differs")
    require(field(diagnostics, "Generation") == cache_key
            and field(diagnostics, "BuildId") == build_id
            and field(diagnostics, "PluginVersion") == meta["version"]
            and isinstance(field(diagnostics, "KitVersion"), str)
            and bool(field(diagnostics, "KitVersion"))
            and server.get("kitVersion") == field(diagnostics, "KitVersion"),
            "ABI-floor diagnostics identity differs")

    generation_cache = header_values(generation_headers, "cache-control")
    kit_cache = header_values(kit_headers, "cache-control")
    shell_etags = header_values(shell_headers, "etag")
    require(response_statuses(generation_headers)[-1:] == [200]
            and response_statuses(kit_headers)[-1:] == [200]
            and response_statuses(shell_headers)[-1:] == [200]
            and response_statuses(conditional_headers)[-1:] == [304],
            "ABI-floor raw HTTP status sequence differs")
    require(any("no-store" in value.lower() for value in generation_cache),
            "ABI-floor raw generation response is not no-store")
    require(any("immutable" in value.lower() for value in kit_cache),
            "ABI-floor raw versioned runtime response is not immutable")
    require(shell_etags == [server["shellEtag"]],
            "ABI-floor raw transformed-shell ETag differs")
    kit_version = re.search(r"\bKIT_VERSION\s*=\s*(['\"])([^'\"]+)\1", kit)
    require(kit_version is not None
            and kit_version.group(2) == field(diagnostics, "KitVersion"),
            "ABI-floor raw runtime version differs from diagnostics")
    require('plugin="Jellyfin Refresh Kit"' in shell
            and 'data-name="RefreshKitPlugin"' in shell
            and f'data-boot-version="{cache_key}"' in shell
            and f"/RefreshKit/kit.js?v={cache_key}" in shell,
            "ABI-floor raw transformed shell differs from the retained generation")

    inventory_match = plugin_rows(plugins, "authenticated plugin inventory")[0]
    diagnostic_match = plugin_rows(field(diagnostics, "Plugins"), "diagnostics plugin inventory")[0]
    require(field(inventory_match, "Name") == "Jellyfin Refresh Kit"
            and field(inventory_match, "Version") == meta["version"]
            and str(field(inventory_match, "Status") or "").lower() == "active"
            and field(inventory_match, "CanUninstall") is True,
            "ABI-floor authenticated plugin record is not the active candidate")
    require(field(diagnostic_match, "Version") == meta["version"]
            and str(field(diagnostic_match, "Status") or "").lower() == "active"
            and field(diagnostic_match, "IsLoaded") is True,
            "ABI-floor diagnostics plugin record is not active/loaded")
    require(result.get("server") == server
            and result.get("generation") == generation
            and result.get("plugin") == inventory_match
            and result.get("diagnosticsPlugin") == diagnostic_match,
            "ABI-floor normalized result does not match retained raw evidence")

    version = re.escape(str(meta["version"]))
    assembly_load = re.search(
        rf"Loaded assembly Jellyfin\.Plugin\.RefreshKit, Version={version},", log
    )
    plugin_load = re.search(rf"Loaded plugin: Jellyfin Refresh Kit {version}(?:\s|$)", log)
    incompatible = [
        line for line in log.splitlines()
        if "Jellyfin.Plugin.RefreshKit.dll" in line
        and ("Failed to load assembly" in line or "incompatible version" in line)
    ]
    require(assembly_load is not None and plugin_load is not None and incompatible == [],
            "ABI-floor server log does not prove a clean assembly/plugin load")
    require(result.get("logChecks") == {
        "assemblyLoadObserved": True,
        "pluginLoadObserved": True,
        "incompatibleSharedLibraryErrors": [],
    }, "ABI-floor retained log-check summary differs")


def fixture(root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    build = root / "plugin" / ".builds" / "fixture-snapshot"
    stage = build / "stage"
    evidence = root / "evidence"
    server_dir = evidence / "server"
    stage.mkdir(parents=True)
    server_dir.mkdir(parents=True)
    version = "1.2.3.4"
    meta = {
        "framework": "net9.0",
        "targetAbi": "10.11.0.0",
        "version": version,
        "sourceRevision": "a" * 40,
        "sourceTreeSha256": "b" * 64,
        "sourceDateEpoch": 123,
        "sourceDirty": False,
    }
    (stage / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (stage / "Jellyfin.Plugin.RefreshKit.dll").write_bytes(b"fixture-dll")
    package = build / f"jellyfin-refresh-kit_{version}.zip"
    package.write_bytes(b"fixture-package")
    generation = {
        "Version": version,
        "BuildId": "c" * 64,
        "CacheKey": "g-0123456789abcdef",
        "Epoch": "d" * 32,
    }
    plugin = {
        "Name": "Jellyfin Refresh Kit",
        "Id": PLUGIN_GUID,
        "Version": version,
        "Status": "Active",
        "CanUninstall": True,
    }
    diagnostic_plugin = {
        "Id": "515255fe-3332-49b0-b471-0be58c8221d8",
        "Version": version,
        "Status": "Active",
        "IsLoaded": True,
    }
    diagnostics = {
        "Generation": generation["CacheKey"],
        "KitVersion": "9.8.7",
        "PluginVersion": version,
        "BuildId": generation["BuildId"],
        "Plugins": [diagnostic_plugin],
    }
    server = {
        "target": "abi-floor",
        "serverVersion": "10.11.0",
        "generation": generation["CacheKey"],
        "kitVersion": diagnostics["KitVersion"],
        "shellEtag": '"rk-fixture"',
        "conditionalStatus": 304,
        "endpointAndShellChecksPassed": True,
    }
    stage_identity = {
        "meta": meta,
        "dllSha256": file_hash(stage / "Jellyfin.Plugin.RefreshKit.dll"),
        "package": {
            "name": package.name,
            "bytes": package.stat().st_size,
            "md5": file_hash(package, "md5"),
            "sha256": file_hash(package),
        },
    }
    result = {
        "schemaVersion": 1,
        "scenario": SCENARIO,
        "completed": True,
        "failures": [],
        "finishedUtc": "2026-08-09T01:02:03Z",
        "immutableSnapshot": build.name,
        "sourceIdentity": {
            "revision": meta["sourceRevision"],
            "treeSha256": meta["sourceTreeSha256"],
            "dateEpoch": meta["sourceDateEpoch"],
            "dirty": meta["sourceDirty"],
        },
        "stage": stage_identity,
        "image": {
            "reference": IMAGE_REFERENCE,
            "digest": f"sha256:{IMAGE_DIGEST}",
            "configuredReference": IMAGE_REFERENCE,
            "imageId": "sha256:" + "e" * 64,
            "repoDigests": ["jellyfin/jellyfin@sha256:" + IMAGE_DIGEST],
        },
        "container": {
            "project": "rk-jellyfin-fixture",
            "service": "abi-floor",
            "labels": {
                "com.docker.compose.project": "rk-jellyfin-fixture",
                "com.docker.compose.service": "abi-floor",
            },
            "portBinding": {
                "containerPort": "8096/tcp",
                "hostIp": "127.0.0.1",
                "hostPort": "18119",
            },
            "volumes": [
                {"destination": "/cache", "name": "rk-jellyfin-fixture_abi-floor-cache", "type": "volume"},
                {"destination": "/config", "name": "rk-jellyfin-fixture_abi-floor-config", "type": "volume"},
            ],
            "network": {
                "name": "rk-jellyfin-fixture_abi-floor-internal",
                "id": "f" * 64,
                "endpointId": "1" * 64,
                "internal": True,
                "labels": {
                    "com.docker.compose.project": "rk-jellyfin-fixture",
                    "com.docker.compose.network": "abi-floor-internal",
                },
            },
        },
        "server": server,
        "generation": generation,
        "plugin": plugin,
        "diagnosticsPlugin": diagnostic_plugin,
        "logChecks": {
            "assemblyLoadObserved": True,
            "pluginLoadObserved": True,
            "incompatibleSharedLibraryErrors": [],
        },
    }
    for name, value in (
        ("result.json", result),
        ("server/result.json", server),
        ("server/public.json", {"Version": "10.11.0"}),
        ("server/generation.json", generation),
        ("server/diagnostics.json", diagnostics),
        ("server/plugins.json", [plugin]),
    ):
        path = evidence / name
        path.write_text(json.dumps(value), encoding="utf-8")
    (server_dir / "generation.headers").write_text(
        "HTTP/1.1 200 OK\r\nCache-Control: no-store\r\n", encoding="utf-8"
    )
    (server_dir / "kit.headers").write_text(
        "HTTP/1.1 200 OK\r\nCache-Control: public, max-age=31536000, immutable\r\n",
        encoding="utf-8",
    )
    (server_dir / "kit.js").write_text(
        "const KIT_VERSION = '9.8.7';\n", encoding="utf-8"
    )
    (server_dir / "shell.headers").write_text(
        'HTTP/1.1 200 OK\r\nETag: "rk-fixture"\r\n', encoding="utf-8"
    )
    (server_dir / "conditional.headers").write_text(
        'HTTP/1.1 304 Not Modified\r\nETag: "rk-fixture"\r\n', encoding="utf-8"
    )
    (server_dir / "index.html").write_text(
        '<script plugin="Jellyfin Refresh Kit" data-name="RefreshKitPlugin" '
        'data-boot-version="g-0123456789abcdef" '
        'src="/RefreshKit/kit.js?v=g-0123456789abcdef"></script>\n',
        encoding="utf-8",
    )
    (evidence / "server.log").write_text(
        f"Loaded assembly Jellyfin.Plugin.RefreshKit, Version={version}, Culture=neutral\n"
        f"Loaded plugin: Jellyfin Refresh Kit {version}\n",
        encoding="utf-8",
    )
    return evidence, build


def self_test() -> None:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="rk-abi-floor-validator-") as temporary:
        root = pathlib.Path(temporary)
        evidence, build = fixture(root)
        validate_evidence(evidence, build)
        checks += 1
        mutations = (
            ("result.json", ("image", "reference"), "jellyfin/jellyfin:10.11.1"),
            ("result.json", ("immutableSnapshot",), "stale"),
            ("server/result.json", ("serverVersion",), "10.11.1"),
            ("server/generation.json", ("CacheKey",), "g-fedcba9876543210"),
            ("server/plugins.json", (0, "Status"), "Disabled"),
        )
        originals: dict[pathlib.Path, bytes] = {}
        for relative, keys, changed in mutations:
            path = evidence / relative
            originals.setdefault(path, path.read_bytes())
            value: Any = json.loads(originals[path])
            target = value
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = changed
            path.write_text(json.dumps(value), encoding="utf-8")
            try:
                validate_evidence(evidence, build)
            except AbiFloorEvidenceError:
                checks += 1
            else:
                raise AssertionError(f"ABI-floor validator accepted mutation {relative}:{keys}")
            path.write_bytes(originals[path])

        log_path = evidence / "server.log"
        clean_log = log_path.read_text(encoding="utf-8")
        log_path.write_text(
            clean_log
            + "Failed to load assembly /config/plugins/Jellyfin.Plugin.RefreshKit.dll: "
              "incompatible version\n",
            encoding="utf-8",
        )
        try:
            validate_evidence(evidence, build)
        except AbiFloorEvidenceError:
            checks += 1
        else:
            raise AssertionError("ABI-floor validator accepted an incompatible shared-library log")
        log_path.write_text(clean_log, encoding="utf-8")

        result_path = evidence / "result.json"
        result = load_object(result_path)
        changed_result = copy.deepcopy(result)
        changed_result["container"]["portBinding"]["hostIp"] = "0.0.0.0"
        result_path.write_text(json.dumps(changed_result), encoding="utf-8")
        try:
            validate_evidence(evidence, build)
        except AbiFloorEvidenceError:
            checks += 1
        else:
            raise AssertionError("ABI-floor validator accepted a non-loopback binding")
        result_path.write_text(json.dumps(result), encoding="utf-8")

        changed_result = copy.deepcopy(result)
        changed_result["container"]["network"]["internal"] = False
        result_path.write_text(json.dumps(changed_result), encoding="utf-8")
        try:
            validate_evidence(evidence, build)
        except AbiFloorEvidenceError:
            checks += 1
        else:
            raise AssertionError("ABI-floor validator accepted an external network")
        result_path.write_text(json.dumps(result), encoding="utf-8")

        conditional_path = evidence / "server" / "conditional.headers"
        clean_conditional = conditional_path.read_text(encoding="utf-8")
        conditional_path.write_text(
            clean_conditional.replace("304 Not Modified", "200 OK"), encoding="utf-8"
        )
        try:
            validate_evidence(evidence, build)
        except AbiFloorEvidenceError:
            checks += 1
        else:
            raise AssertionError("ABI-floor validator accepted a false conditional status")
        conditional_path.write_text(clean_conditional, encoding="utf-8")

        shell_path = evidence / "server" / "index.html"
        clean_shell = shell_path.read_text(encoding="utf-8")
        shell_path.write_text(clean_shell.replace("RefreshKitPlugin", "WrongPlugin"), encoding="utf-8")
        try:
            validate_evidence(evidence, build)
        except AbiFloorEvidenceError:
            checks += 1
        else:
            raise AssertionError("ABI-floor validator accepted a changed raw shell")
        shell_path.write_text(clean_shell, encoding="utf-8")

        (evidence / "server" / "plugins.json").unlink()
        try:
            validate_evidence(evidence, build)
        except AbiFloorEvidenceError:
            checks += 1
        else:
            raise AssertionError("ABI-floor validator accepted missing raw evidence")
    print(f"ABI-floor retained-evidence validator self-test: {checks}/{checks} PASS")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path)
    parser.add_argument("--build", type=pathlib.Path)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        return 0
    require(args.root is not None and args.build is not None,
            "--root and --build are required outside self-test mode")
    validate_evidence(args.root, args.build)
    print("ABI-floor retained evidence: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AbiFloorEvidenceError as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
