#!/usr/bin/env python3
"""Validate the isolated Jellyfin 10.11.0 ABI-floor smoke evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import pathlib
import re
import struct
import sys
import tempfile
import uuid
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
    "server/conditional.body",
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
        return path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError) as error:
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


def unpack_from(fmt: str, data: bytes, offset: int, label: str) -> tuple[int, ...]:
    size = struct.calcsize(fmt)
    require(0 <= offset <= len(data) - size, f"truncated {label}")
    return struct.unpack_from(fmt, data, offset)


def pe_rva_offset(
    data: bytes,
    rva: int,
    size: int,
    size_of_headers: int,
    sections: tuple[tuple[int, int, int, int], ...],
    label: str,
) -> int:
    if rva < size_of_headers:
        require(rva <= len(data) - size, f"{label} extends beyond PE headers")
        return rva
    for virtual_address, virtual_size, raw_offset, raw_size in sections:
        if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
            relative = rva - virtual_address
            require(relative <= raw_size - size, f"{label} extends beyond its PE section")
            require(raw_offset + relative <= len(data) - size,
                    f"{label} extends beyond the PE file")
            return raw_offset + relative
    raise AbiFloorEvidenceError(f"{label} RVA 0x{rva:x} is not mapped by a PE section")


def coded_index_size(
    row_counts: tuple[int, ...],
    tag_bits: int,
    tables: tuple[int, ...],
) -> int:
    maximum = max((row_counts[table] for table in tables), default=0)
    return 2 if maximum < (1 << (16 - tag_bits)) else 4


def metadata_table_row_size(
    table: int,
    row_counts: tuple[int, ...],
    string_size: int,
    guid_size: int,
    blob_size: int,
) -> int:
    """Return ECMA-335 row widths through the Assembly table."""
    simple = lambda target: 2 if row_counts[target] < 65536 else 4
    coded = lambda bits, *targets: coded_index_size(row_counts, bits, targets)
    sizes = {
        0: 2 + string_size + (3 * guid_size),
        1: coded(2, 0, 26, 35, 1) + (2 * string_size),
        2: 4 + (2 * string_size) + coded(2, 2, 1, 27) + simple(4) + simple(6),
        3: simple(4),
        4: 2 + string_size + blob_size,
        5: simple(6),
        6: 8 + string_size + blob_size + simple(8),
        7: simple(8),
        8: 4 + string_size,
        9: simple(2) + coded(2, 2, 1, 27),
        10: coded(3, 2, 1, 26, 6, 27) + string_size + blob_size,
        11: 2 + coded(2, 4, 8, 23) + blob_size,
        12: coded(5, 6, 4, 1, 2, 8, 9, 10, 0, 14, 23, 20, 17,
                  26, 27, 32, 35, 38, 39, 40, 42, 44, 43)
            + coded(3, 6, 10) + blob_size,
        13: coded(1, 4, 8) + blob_size,
        14: 2 + coded(2, 2, 6, 32) + blob_size,
        15: 6 + simple(2),
        16: 4 + simple(4),
        17: blob_size,
        18: simple(2) + simple(20),
        19: simple(20),
        20: 2 + string_size + coded(2, 2, 1, 27),
        21: simple(2) + simple(23),
        22: simple(23),
        23: 2 + string_size + blob_size,
        24: 2 + simple(6) + coded(1, 20, 23),
        25: simple(2) + (2 * coded(1, 6, 10)),
        26: string_size,
        27: blob_size,
        28: 2 + coded(1, 4, 6) + string_size + simple(26),
        29: 4 + simple(4),
        30: 8,
        31: 4,
        32: 16 + blob_size + (2 * string_size),
    }
    require(table in sizes, f"unsupported metadata table {table} before Assembly")
    return sizes[table]


def _heap_index(data: bytes, offset: int, size: int, label: str) -> int:
    if size == 2:
        return unpack_from("<H", data, offset, label)[0]
    return unpack_from("<I", data, offset, label)[0]


def _metadata_string(data: bytes, heap: tuple[int, int], index: int, label: str) -> str:
    start, size = heap
    require(0 <= index < size, f"{label} has an invalid string index")
    end = data.find(b"\0", start + index, start + size)
    require(end != -1, f"{label} string is unterminated")
    try:
        return data[start + index:end].decode("utf-8")
    except UnicodeDecodeError as error:
        raise AbiFloorEvidenceError(f"{label} string is not UTF-8") from error


def _metadata_blob(data: bytes, heap: tuple[int, int], index: int, label: str) -> bytes:
    start, size = heap
    require(0 <= index < size, f"{label} has an invalid blob index")
    if index == 0:
        return b""
    cursor = start + index
    first = unpack_from("<B", data, cursor, f"{label} blob length")[0]
    if first & 0x80 == 0:
        length, prefix = first, 1
    elif first & 0xC0 == 0x80:
        second = unpack_from("<B", data, cursor + 1, f"{label} blob length")[0]
        length, prefix = ((first & 0x3F) << 8) | second, 2
    elif first & 0xE0 == 0xC0:
        tail = unpack_from(">I", data, cursor, f"{label} blob length")[0]
        length, prefix = tail & 0x1FFFFFFF, 4
    else:
        raise AbiFloorEvidenceError(f"{label} blob has an invalid compressed length")
    require(index + prefix + length <= size, f"{label} blob is truncated")
    return data[cursor + prefix:cursor + prefix + length]


def managed_pe_identity(path: pathlib.Path) -> dict[str, Any]:
    """Derive the runtime identities from a managed PE without loading it."""
    try:
        data = path.read_bytes()
    except OSError as error:
        raise AbiFloorEvidenceError(f"cannot read managed DLL {path}: {error}") from error
    require(data[:2] == b"MZ", "managed DLL is not a PE image")
    pe_offset = unpack_from("<I", data, 0x3C, "DOS header")[0]
    require(data[pe_offset:pe_offset + 4] == b"PE\0\0", "managed DLL has no PE signature")
    coff = pe_offset + 4
    _, section_count, _, _, _, optional_size, _ = unpack_from(
        "<HHIIIHH", data, coff, "PE COFF header"
    )
    optional = coff + 20
    magic = unpack_from("<H", data, optional, "PE optional header")[0]
    require(magic in (0x10B, 0x20B), f"unsupported PE optional-header magic 0x{magic:x}")
    directory_offset = optional + (96 if magic == 0x10B else 112)
    require(optional_size >= directory_offset - optional + (15 * 8),
            "PE optional header has no CLR data directory")
    size_of_headers = unpack_from("<I", data, optional + 60, "PE SizeOfHeaders")[0]
    clr_rva, clr_size = unpack_from(
        "<II", data, directory_offset + (14 * 8), "CLR data directory"
    )
    require(clr_rva != 0 and clr_size >= 16, "PE image has no CLR header")
    section_rows = []
    section_offset = optional + optional_size
    for index in range(section_count):
        row = section_offset + (index * 40)
        virtual_size, virtual_address, raw_size, raw_offset = unpack_from(
            "<IIII", data, row + 8, f"PE section {index}"
        )
        section_rows.append((virtual_address, virtual_size, raw_offset, raw_size))
    sections = tuple(section_rows)
    clr_offset = pe_rva_offset(
        data, clr_rva, clr_size, size_of_headers, sections, "CLR header"
    )
    metadata_rva, metadata_size = unpack_from(
        "<II", data, clr_offset + 8, "CLR metadata directory"
    )
    require(metadata_rva != 0 and metadata_size >= 20, "CLR image has no metadata root")
    metadata = pe_rva_offset(
        data, metadata_rva, metadata_size, size_of_headers, sections, "CLR metadata"
    )
    require(unpack_from("<I", data, metadata, "CLR metadata signature")[0] == 0x424A5342,
            "CLR metadata signature is not BSJB")
    version_length = unpack_from("<I", data, metadata + 12, "CLR metadata version")[0]
    stream_header = metadata + 16 + ((version_length + 3) & ~3)
    _, stream_count = unpack_from("<HH", data, stream_header, "CLR stream header")
    cursor = stream_header + 4
    streams: dict[str, tuple[int, int]] = {}
    for index in range(stream_count):
        relative, stream_size = unpack_from("<II", data, cursor, f"CLR stream {index} header")
        name_start = cursor + 8
        name_end = data.find(b"\0", name_start, min(name_start + 32, len(data)))
        require(name_end != -1, f"CLR stream {index} has no terminated name")
        try:
            name = data[name_start:name_end].decode("ascii")
        except UnicodeDecodeError as error:
            raise AbiFloorEvidenceError(f"CLR stream {index} name is not ASCII") from error
        absolute = metadata + relative
        require(absolute <= len(data) - stream_size, f"CLR stream {name!r} is truncated")
        require(name not in streams, f"duplicate CLR stream {name!r}")
        streams[name] = (absolute, stream_size)
        cursor = name_start + (((name_end - name_start) + 1 + 3) & ~3)

    table_name = "#~" if "#~" in streams else "#-"
    require(table_name in streams and "#Strings" in streams
            and "#GUID" in streams and "#Blob" in streams,
            "CLR metadata lacks a required table/string/GUID/blob stream")
    tables, table_size = streams[table_name]
    require(table_size >= 24, "CLR metadata table stream is truncated")
    heap_sizes = unpack_from("<B", data, tables + 6, "CLR heap-size flags")[0]
    valid, _ = unpack_from("<QQ", data, tables + 8, "CLR table masks")
    cursor = tables + 24
    counts = [0] * 64
    for table in range(64):
        if valid & (1 << table):
            counts[table] = unpack_from(
                "<I", data, cursor, f"metadata table {table} count"
            )[0]
            cursor += 4
    if heap_sizes & 0x40:
        cursor += 4
        require(cursor <= tables + table_size, "CLR table extra-data marker is truncated")
    row_counts = tuple(counts)
    require(row_counts[0] == 1 and row_counts[32] == 1,
            "managed DLL must contain exactly one Module and Assembly row")
    string_index_size = 4 if heap_sizes & 0x01 else 2
    guid_index_size = 4 if heap_sizes & 0x02 else 2
    blob_index_size = 4 if heap_sizes & 0x04 else 2
    offsets: dict[int, int] = {}
    for table in range(33):
        offsets[table] = cursor
        cursor += row_counts[table] * metadata_table_row_size(
            table, row_counts, string_index_size, guid_index_size, blob_index_size
        )
    require(cursor <= tables + table_size, "Module/Assembly metadata tables are truncated")

    module_row = offsets[0]
    mvid_index = _heap_index(
        data, module_row + 2 + string_index_size, guid_index_size, "Module MVID"
    )
    guid_heap, guid_heap_size = streams["#GUID"]
    require(mvid_index > 0 and mvid_index * 16 <= guid_heap_size,
            "Module MVID has an invalid GUID index")
    mvid_offset = guid_heap + ((mvid_index - 1) * 16)
    mvid = uuid.UUID(bytes_le=data[mvid_offset:mvid_offset + 16]).hex

    assembly_row = offsets[32]
    major, minor, build, revision = unpack_from(
        "<HHHH", data, assembly_row + 4, "Assembly version"
    )
    public_key_index = _heap_index(
        data, assembly_row + 16, blob_index_size, "Assembly public key"
    )
    name_offset = assembly_row + 16 + blob_index_size
    name_index = _heap_index(data, name_offset, string_index_size, "Assembly name")
    culture_index = _heap_index(
        data, name_offset + string_index_size, string_index_size, "Assembly culture"
    )
    name = _metadata_string(data, streams["#Strings"], name_index, "Assembly name")
    culture_value = _metadata_string(
        data, streams["#Strings"], culture_index, "Assembly culture"
    )
    require(name == "Jellyfin.Plugin.RefreshKit", f"unexpected managed assembly name {name!r}")
    version = f"{major}.{minor}.{build}.{revision}"
    culture = culture_value or "neutral"
    public_key = _metadata_blob(data, streams["#Blob"], public_key_index, "Assembly public key")
    if public_key:
        public_key_token = hashlib.sha1(public_key, usedforsecurity=False).digest()[-8:][::-1].hex()
    else:
        public_key_token = "null"
    full_name = (
        f"{name}, Version={version}, Culture={culture}, PublicKeyToken={public_key_token}"
    )
    build_id = hashlib.sha256(f"{full_name}\n{mvid}".encode("utf-8")).hexdigest()
    module_material = f"rk-loaded-modules-v1\n{name}|{version}|{mvid}"
    loaded_module_identity = hashlib.sha256(module_material.encode("utf-8")).hexdigest()[:32]
    return {
        "assemblyName": name,
        "assemblyVersion": version,
        "assemblyFullName": full_name,
        "moduleVersionId": mvid,
        "buildId": build_id,
        "loadedModuleIdentity": loaded_module_identity,
    }


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
    managed = managed_pe_identity(dll)
    require(managed["assemblyVersion"] == version,
            "pinned ABI-floor DLL assembly version differs from stage metadata")
    identity = {
        "meta": meta,
        "dllSha256": file_hash(dll),
        "containerDll": {
            "path": f"/config/plugins/Jellyfin Refresh Kit_{version}/Jellyfin.Plugin.RefreshKit.dll",
            "sha256": file_hash(dll),
        },
        "managedIdentity": managed,
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
    container_id = value.get("id")
    require(isinstance(container_id, str) and HEX_64.fullmatch(container_id) is not None,
            "ABI-floor container ID is invalid")
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
                and row.get("name") == f"{project}_{suffix}"
                and row.get("readWrite") is True,
                f"ABI-floor {destination} is not its dedicated Compose volume")
        volume_labels = row.get("labels")
        require(isinstance(volume_labels, dict)
                and volume_labels.get("com.docker.compose.project") == project
                and volume_labels.get("com.docker.compose.volume") == suffix,
                f"ABI-floor {destination} volume labels differ")
    network = value.get("network")
    require(isinstance(network, dict)
            and network.get("name") == f"{project}_abi-floor-internal"
            and isinstance(network.get("id"), str)
            and HEX_64.fullmatch(network["id"]) is not None
            and isinstance(network.get("endpointId"), str)
            and HEX_64.fullmatch(network["endpointId"]) is not None
            and network.get("attachedContainerIds") == [container_id]
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
    conditional_body = (root / "server" / "conditional.body").read_bytes()
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
            and image.get("referenceImageId") == image.get("imageId")
            and isinstance(image.get("repoDigests"), list)
            and any(isinstance(row, str) and row.endswith("@sha256:" + IMAGE_DIGEST)
                    for row in image["repoDigests"]),
            "ABI-floor exact image identity differs")
    validate_container(result.get("container"))

    managed = stage["managedIdentity"]
    require(field(public, "Version") == "10.11.0"
            and server.get("target") == "abi-floor"
            and server.get("serverVersion") == field(public, "Version")
            and server.get("endpointAndShellChecksPassed") is True
            and server.get("exactHttpContractPassed") is True
            and server.get("conditionalStatus") == 304
            and server.get("directResponseFraming") == {
                "generation": "chunked",
                "kit": "content-length",
                "shell": "content-length",
            },
            "ABI-floor public-server/endpoint result differs")
    cache_key = field(generation, "CacheKey")
    build_id = field(generation, "BuildId")
    epoch = field(generation, "Epoch")
    require(field(generation, "Version") == meta["version"]
            and isinstance(cache_key, str) and GENERATION.fullmatch(cache_key) is not None
            and build_id == managed["buildId"]
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
    shell_cache = header_values(shell_headers, "cache-control")
    conditional_cache = header_values(conditional_headers, "cache-control")
    shell_etags = header_values(shell_headers, "etag")
    conditional_etags = header_values(conditional_headers, "etag")
    require(response_statuses(generation_headers) == [200]
            and response_statuses(kit_headers) == [200]
            and response_statuses(shell_headers) == [200]
            and response_statuses(conditional_headers) == [304]
            and conditional_body == b"",
            "ABI-floor raw HTTP status sequence differs")
    require(generation_cache == ["no-store, no-cache, must-revalidate, max-age=0"],
            "ABI-floor raw generation Cache-Control is not exact/singleton")
    require(kit_cache == ["public, max-age=31536000, immutable"],
            "ABI-floor raw runtime Cache-Control is not exact/singleton")
    require(shell_cache == ["no-cache"]
            and conditional_cache == ["no-cache"]
            and header_values(conditional_headers, "expires") == []
            and header_values(conditional_headers, "pragma") == [],
            "ABI-floor shell/conditional Cache-Control is not exact/singleton")
    shell_bytes = (root / "server" / "index.html").read_bytes()
    expected_etag = '"rk-' + hashlib.sha256(shell_bytes).hexdigest() + '"'
    require(shell_etags == [expected_etag] and conditional_etags == [expected_etag]
            and server.get("shellEtag") == expected_etag
            and server.get("conditionalEtag") == expected_etag
            and server.get("shellSha256") == hashlib.sha256(shell_bytes).hexdigest()
            and server.get("shellBytes") == len(shell_bytes)
            and server.get("shellCacheControl") == "no-cache"
            and server.get("conditionalCacheControl") == "no-cache",
            "ABI-floor shell validator is not derived from its exact body bytes")
    for header_name in (
        "content-encoding", "content-length", "transfer-encoding", "last-modified",
        "accept-ranges", "content-range",
    ):
        require(header_values(conditional_headers, header_name) == [],
                f"ABI-floor conditional response retained forbidden {header_name}")
    shell_content_type = header_values(shell_headers, "content-type")
    require(len(shell_content_type) == 1
            and header_values(conditional_headers, "content-type") == shell_content_type,
            "ABI-floor conditional Content-Type does not exactly match the shell")

    def framing(headers: str, body: bytes, label: str) -> str:
        lengths = header_values(headers, "content-length")
        transfers = header_values(headers, "transfer-encoding")
        if len(lengths) == 1 and not transfers:
            require(lengths[0].isdigit() and int(lengths[0]) == len(body),
                    f"ABI-floor {label} Content-Length differs from exact body bytes")
            return "content-length"
        require(not lengths and transfers == ["chunked"],
                f"ABI-floor {label} response framing is ambiguous or invalid")
        return "chunked"

    require({
        "generation": framing(
            generation_headers, (root / "server" / "generation.json").read_bytes(), "generation"
        ),
        "kit": framing(kit_headers, (root / "server" / "kit.js").read_bytes(), "kit.js"),
        "shell": framing(shell_headers, shell_bytes, "shell"),
    } == server.get("directResponseFraming"),
            "ABI-floor retained direct-response framing summary differs")
    kit_version = re.search(r"\bKIT_VERSION\s*=\s*(['\"])([^'\"]+)\1", kit)
    require(kit_version is not None
            and kit_version.group(2) == field(diagnostics, "KitVersion"),
            "ABI-floor raw runtime version differs from diagnostics")
    require(shell.count('plugin="Jellyfin Refresh Kit"') == 1
            and shell.count('data-name="RefreshKitPlugin"') == 1
            and shell.count(f'data-boot-version="{cache_key}"') == 1
            and shell.count(f"/RefreshKit/kit.js?v={cache_key}") == 1,
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
            and field(diagnostic_match, "IsLoaded") is True
            and field(diagnostic_match, "Folder") == f"Jellyfin Refresh Kit_{meta['version']}"
            and field(diagnostic_match, "LoadedModuleIdentity")
            == managed["loadedModuleIdentity"]
            and isinstance(field(diagnostic_match, "AssetIdentity"), str)
            and HEX_32.fullmatch(field(diagnostic_match, "AssetIdentity")) is not None
            and isinstance(field(diagnostic_match, "ConfigurationIdentity"), str)
            and HEX_32.fullmatch(field(diagnostic_match, "ConfigurationIdentity")) is not None
            and all(field(diagnostic_match, name) is False for name in (
                "AssetScanTruncated",
                "AssetScanUnavailable",
                "UsingLastGoodAssets",
                "ConfigurationScanTruncated",
                "ConfigurationScanUnavailable",
                "UsingLastGoodConfiguration",
                "UsingLastKnownPluginRecord",
            )),
            "ABI-floor diagnostics plugin record is not active/loaded")
    require(result.get("server") == server
            and result.get("generation") == generation
            and result.get("plugin") == inventory_match
            and result.get("diagnosticsPlugin") == diagnostic_match,
            "ABI-floor normalized result does not match retained raw evidence")

    version = re.escape(str(meta["version"]))
    dll_path = re.escape(stage["containerDll"]["path"])
    full_name = re.escape(managed["assemblyFullName"])
    assembly_loads = [
        line for line in log.splitlines()
        if re.search(rf"(?:^|PluginManager: )Loaded assembly {full_name} from {dll_path}$", line)
    ]
    plugin_loads = [
        line for line in log.splitlines()
        if re.search(rf"(?:^|PluginManager: )Loaded plugin: Jellyfin Refresh Kit {version}$", line)
    ]
    scoped_errors = [
        line for line in log.splitlines()
        if ("Jellyfin.Plugin.RefreshKit" in line
            or "Jellyfin Refresh Kit" in line
            or "JellyfinRefreshKit" in line)
        and re.search(
            r"\[(?:ERR|WRN|FTL)\]|failed|incompatible|exception|could not load|"
            r"badimageformat|fileload|typeload|missingmethod",
            line,
            re.IGNORECASE,
        )
    ]
    require(len(assembly_loads) == 1 and len(plugin_loads) == 1 and scoped_errors == [],
            "ABI-floor server log does not prove a clean assembly/plugin load")
    require(result.get("logChecks") == {
        "assemblyLoadMatches": 1,
        "pluginLoadMatches": 1,
        "scopedErrorLines": [],
    }, "ABI-floor retained log-check summary differs")


def fixture_managed_pe(version: str, mvid: uuid.UUID) -> bytes:
    """Build the smallest metadata-only PE needed by the no-Docker self-test."""
    version_parts = tuple(int(part) for part in version.split("."))
    require(len(version_parts) == 4 and all(0 <= part <= 65535 for part in version_parts),
            "fixture assembly version is invalid")
    strings = b"\0Jellyfin.Plugin.RefreshKit\0fixture-module\0"
    assembly_name_index = 1
    module_name_index = strings.index(b"fixture-module")
    tables = bytearray()
    tables.extend(struct.pack("<IBBBBQQ", 0, 2, 0, 0, 1, (1 << 0) | (1 << 32), 0))
    tables.extend(struct.pack("<II", 1, 1))
    tables.extend(struct.pack("<HHHHH", 0, module_name_index, 1, 0, 0))
    tables.extend(struct.pack(
        "<IHHHHIHHH",
        0x8004,
        *version_parts,
        0,
        0,
        assembly_name_index,
        0,
    ))
    streams = (
        ("#~", bytes(tables)),
        ("#Strings", strings),
        ("#GUID", mvid.bytes_le),
        ("#Blob", b"\0"),
    )
    metadata_version = b"v4.0.30319\0\0"
    prefix = bytearray(struct.pack(
        "<IHHII", 0x424A5342, 1, 1, 0, len(metadata_version)
    ))
    prefix.extend(metadata_version)
    prefix.extend(struct.pack("<HH", 0, len(streams)))
    stream_headers_size = sum(
        8 + ((len(name.encode("ascii")) + 1 + 3) & ~3) for name, _ in streams
    )
    data_offset = len(prefix) + stream_headers_size
    cursor = data_offset
    headers = bytearray()
    bodies = bytearray()
    for name, body in streams:
        headers.extend(struct.pack("<II", cursor, len(body)))
        encoded = name.encode("ascii") + b"\0"
        headers.extend(encoded)
        headers.extend(b"\0" * (((len(encoded) + 3) & ~3) - len(encoded)))
        bodies.extend(body)
        padding = (-len(bodies)) & 3
        bodies.extend(b"\0" * padding)
        cursor = data_offset + len(bodies)
    metadata = bytes(prefix + headers + bodies)

    pe_offset = 0x80
    optional_size = 0xF0
    headers_size = 0x200
    section_rva = 0x2000
    section_raw = 0x200
    metadata_in_section = 0x100
    raw_size = ((metadata_in_section + len(metadata) + 0x1FF) // 0x200) * 0x200
    image = bytearray(section_raw + raw_size)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3C, pe_offset)
    image[pe_offset:pe_offset + 4] = b"PE\0\0"
    coff = pe_offset + 4
    struct.pack_into("<HHIIIHH", image, coff, 0x8664, 1, 0, 0, 0, optional_size, 0x2022)
    optional = coff + 20
    struct.pack_into("<H", image, optional, 0x20B)
    struct.pack_into("<I", image, optional + 60, headers_size)
    directory_offset = optional + 112
    struct.pack_into("<II", image, directory_offset + (14 * 8), section_rva, 72)
    section = optional + optional_size
    image[section:section + 8] = b".text\0\0\0"
    struct.pack_into(
        "<IIII", image, section + 8, raw_size, section_rva, raw_size, section_raw
    )
    clr = section_raw
    struct.pack_into("<IHH", image, clr, 72, 2, 5)
    struct.pack_into(
        "<II", image, clr + 8, section_rva + metadata_in_section, len(metadata)
    )
    image[section_raw + metadata_in_section:section_raw + metadata_in_section + len(metadata)] = metadata
    return bytes(image)


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
    fixture_mvid = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
    (stage / "Jellyfin.Plugin.RefreshKit.dll").write_bytes(
        fixture_managed_pe(version, fixture_mvid)
    )
    package = build / f"jellyfin-refresh-kit_{version}.zip"
    package.write_bytes(b"fixture-package")
    managed = managed_pe_identity(stage / "Jellyfin.Plugin.RefreshKit.dll")
    shell_text = (
        '<script plugin="Jellyfin Refresh Kit" data-name="RefreshKitPlugin" '
        'data-boot-version="g-0123456789abcdef" '
        'src="/RefreshKit/kit.js?v=g-0123456789abcdef"></script>\n'
    )
    shell_body = shell_text.encode("utf-8")
    shell_etag = '"rk-' + hashlib.sha256(shell_body).hexdigest() + '"'
    generation = {
        "Version": version,
        "BuildId": managed["buildId"],
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
        "Folder": f"Jellyfin Refresh Kit_{version}",
        "Id": "515255fe-3332-49b0-b471-0be58c8221d8",
        "Version": version,
        "Status": "Active",
        "IsLoaded": True,
        "LoadedModuleIdentity": managed["loadedModuleIdentity"],
        "AssetIdentity": "8" * 32,
        "ConfigurationIdentity": "9" * 32,
        "AssetScanTruncated": False,
        "AssetScanUnavailable": False,
        "UsingLastGoodAssets": False,
        "ConfigurationScanTruncated": False,
        "ConfigurationScanUnavailable": False,
        "UsingLastGoodConfiguration": False,
        "UsingLastKnownPluginRecord": False,
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
        "shellEtag": shell_etag,
        "shellSha256": hashlib.sha256(shell_body).hexdigest(),
        "shellBytes": len(shell_body),
        "shellCacheControl": "no-cache",
        "conditionalStatus": 304,
        "conditionalEtag": shell_etag,
        "conditionalCacheControl": "no-cache",
        "directResponseFraming": {
            "generation": "chunked",
            "kit": "content-length",
            "shell": "content-length",
        },
        "exactHttpContractPassed": True,
        "endpointAndShellChecksPassed": True,
    }
    stage_identity = {
        "meta": meta,
        "dllSha256": file_hash(stage / "Jellyfin.Plugin.RefreshKit.dll"),
        "containerDll": {
            "path": f"/config/plugins/Jellyfin Refresh Kit_{version}/Jellyfin.Plugin.RefreshKit.dll",
            "sha256": file_hash(stage / "Jellyfin.Plugin.RefreshKit.dll"),
        },
        "managedIdentity": managed,
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
            "referenceImageId": "sha256:" + "e" * 64,
            "repoDigests": ["jellyfin/jellyfin@sha256:" + IMAGE_DIGEST],
        },
        "container": {
            "id": "a" * 64,
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
                {
                    "destination": "/cache",
                    "name": "rk-jellyfin-fixture_abi-floor-cache",
                    "type": "volume",
                    "readWrite": True,
                    "labels": {
                        "com.docker.compose.project": "rk-jellyfin-fixture",
                        "com.docker.compose.volume": "abi-floor-cache",
                    },
                },
                {
                    "destination": "/config",
                    "name": "rk-jellyfin-fixture_abi-floor-config",
                    "type": "volume",
                    "readWrite": True,
                    "labels": {
                        "com.docker.compose.project": "rk-jellyfin-fixture",
                        "com.docker.compose.volume": "abi-floor-config",
                    },
                },
            ],
            "network": {
                "name": "rk-jellyfin-fixture_abi-floor-internal",
                "id": "f" * 64,
                "endpointId": "1" * 64,
                "attachedContainerIds": ["a" * 64],
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
            "assemblyLoadMatches": 1,
            "pluginLoadMatches": 1,
            "scopedErrorLines": [],
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
    kit_body = b"const KIT_VERSION = '9.8.7';\n"
    (server_dir / "kit.js").write_bytes(kit_body)
    (server_dir / "index.html").write_bytes(shell_body)
    generation_bytes = (server_dir / "generation.json").read_bytes()
    (server_dir / "generation.headers").write_bytes(
        b"HTTP/1.1 200 OK\r\n"
        b"Cache-Control: no-store, no-cache, must-revalidate, max-age=0\r\n"
        b"Transfer-Encoding: chunked\r\n"
    )
    (server_dir / "kit.headers").write_bytes(
        (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/javascript; charset=utf-8\r\n"
            f"Content-Length: {len(kit_body)}\r\n"
            "Cache-Control: public, max-age=31536000, immutable\r\n"
        ).encode("ascii")
    )
    (server_dir / "shell.headers").write_bytes(
        (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html;charset=utf-8\r\n"
            f"Content-Length: {len(shell_body)}\r\n"
            "Cache-Control: no-cache\r\n"
            f"ETag: {shell_etag}\r\n"
        ).encode("ascii")
    )
    (server_dir / "conditional.headers").write_bytes(
        (
            "HTTP/1.1 304 Not Modified\r\n"
            "Content-Type: text/html;charset=utf-8\r\n"
            "Cache-Control: no-cache\r\n"
            f"ETag: {shell_etag}\r\n"
        ).encode("ascii")
    )
    (server_dir / "conditional.body").write_bytes(b"")
    assert generation_bytes
    (evidence / "server.log").write_text(
        f"Loaded assembly {managed['assemblyFullName']} from "
        f"/config/plugins/Jellyfin Refresh Kit_{version}/Jellyfin.Plugin.RefreshKit.dll\n"
        f"Loaded plugin: Jellyfin Refresh Kit {version}\n",
        encoding="utf-8",
    )
    return evidence, build


def self_test() -> None:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="rk-abi-floor-validator-") as temporary:
        root = pathlib.Path(temporary)
        evidence, build = fixture(root)
        require(managed_pe_identity(build / "stage" / "Jellyfin.Plugin.RefreshKit.dll") == {
            "assemblyName": "Jellyfin.Plugin.RefreshKit",
            "assemblyVersion": "1.2.3.4",
            "assemblyFullName": (
                "Jellyfin.Plugin.RefreshKit, Version=1.2.3.4, "
                "Culture=neutral, PublicKeyToken=null"
            ),
            "moduleVersionId": "00112233445566778899aabbccddeeff",
            "buildId": "ffc6a8372ba0a80352323c0b2c1d935e9b1a7b652c7ea860da666a7c1366b1a8",
            "loadedModuleIdentity": "bfa930b4e4dec52c1a6f3897719ccc12",
        }, "fixture managed identity derivation differs")
        checks += 1
        validate_evidence(evidence, build)
        checks += 1
        mutations = (
            ("result.json", ("image", "reference"), "jellyfin/jellyfin:10.11.1"),
            ("result.json", ("image", "referenceImageId"), "sha256:" + "0" * 64),
            ("result.json", ("immutableSnapshot",), "stale"),
            ("result.json", ("stage", "containerDll", "sha256"), "0" * 64),
            ("result.json", ("stage", "managedIdentity", "buildId"), "1" * 64),
            ("result.json", ("container", "volumes", 0, "name"), "foreign_cache"),
            (
                "result.json",
                ("container", "volumes", 0, "labels", "com.docker.compose.volume"),
                "foreign-cache",
            ),
            ("result.json", ("container", "volumes", 0, "readWrite"), False),
            ("result.json", ("container", "network", "name"), "foreign_network"),
            (
                "result.json",
                ("container", "network", "attachedContainerIds"),
                ["b" * 64],
            ),
            (
                "result.json",
                ("container", "network", "labels", "com.docker.compose.network"),
                "default",
            ),
            ("result.json", ("logChecks", "assemblyLoadMatches"), 2),
            ("server/result.json", ("serverVersion",), "10.11.1"),
            ("server/result.json", ("exactHttpContractPassed",), False),
            ("server/result.json", ("shellSha256",), "2" * 64),
            ("server/generation.json", ("CacheKey",), "g-fedcba9876543210"),
            ("server/generation.json", ("BuildId",), "3" * 64),
            (
                "server/diagnostics.json",
                ("Plugins", 0, "LoadedModuleIdentity"),
                "4" * 32,
            ),
            ("server/diagnostics.json", ("Plugins", 0, "AssetScanTruncated"), True),
            (
                "server/diagnostics.json",
                ("Plugins", 0, "UsingLastGoodConfiguration"),
                True,
            ),
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

        conditional_body_path = evidence / "server" / "conditional.body"
        conditional_body_path.write_bytes(b"unexpected")
        try:
            validate_evidence(evidence, build)
        except AbiFloorEvidenceError:
            checks += 1
        else:
            raise AssertionError("ABI-floor validator accepted a nonempty 304 body")
        conditional_body_path.write_bytes(b"")

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

        raw_mutations = (
            (
                "server/shell.headers",
                lambda value: value.replace(
                    "ETag: \"rk-", "ETag: \"rk-0", 1
                ),
                "body-independent shell ETag",
            ),
            (
                "server/shell.headers",
                lambda value: value + "Cache-Control: no-cache\r\n",
                "duplicate shell Cache-Control",
            ),
            (
                "server/generation.headers",
                lambda value: value.replace(
                    "no-store, no-cache, must-revalidate, max-age=0", "no-store"
                ),
                "weakened generation Cache-Control",
            ),
            (
                "server/conditional.headers",
                lambda value: value.replace("ETag: \"rk-", "ETag: \"rk-0", 1),
                "stale conditional ETag",
            ),
            (
                "server/conditional.headers",
                lambda value: value.replace(
                    "Cache-Control: no-cache", "Cache-Control: no-store", 1
                ),
                "changed conditional Cache-Control",
            ),
            (
                "server/conditional.headers",
                lambda value: value + "Content-Length: 0\r\n",
                "framed 304 response",
            ),
            (
                "server/kit.headers",
                lambda value: re.sub(r"Content-Length: [0-9]+", "Content-Length: 1", value),
                "incorrect runtime Content-Length",
            ),
            (
                "server.log",
                lambda value: value + value.splitlines()[0] + "\n",
                "duplicate assembly load proof",
            ),
            (
                "server.log",
                lambda value: value
                + "[WRN] Jellyfin Refresh Kit used an unexpected fallback\n",
                "scoped Refresh Kit warning",
            ),
        )
        for relative, mutate, label in raw_mutations:
            path = evidence / relative
            original = path.read_bytes()
            value = original.decode("utf-8")
            path.write_bytes(mutate(value).encode("utf-8"))
            try:
                validate_evidence(evidence, build)
            except AbiFloorEvidenceError:
                checks += 1
            else:
                raise AssertionError(f"ABI-floor validator accepted {label}")
            path.write_bytes(original)

        corrupt = root / "corrupt-managed.dll"
        corrupt.write_bytes(b"not-a-managed-PE")
        try:
            managed_pe_identity(corrupt)
        except AbiFloorEvidenceError:
            checks += 1
        else:
            raise AssertionError("managed PE identity parser accepted arbitrary bytes")

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
    parser.add_argument("--managed-identity", type=pathlib.Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        require(args.managed_identity is None and args.root is None and args.build is None,
                "--self-test cannot be combined with other modes")
        self_test()
        return 0
    if args.managed_identity is not None:
        require(args.root is None and args.build is None,
                "--managed-identity cannot be combined with evidence validation")
        print(json.dumps(managed_pe_identity(args.managed_identity), sort_keys=True))
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
