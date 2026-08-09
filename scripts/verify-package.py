#!/usr/bin/env python3
"""Verify Refresh Kit build artifacts without loading Jellyfin assemblies."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import stat
import struct
import subprocess
import sys
import urllib.parse
import zipfile
import xml.etree.ElementTree as ET


MATRIX = (
    ("net9.0", "10.11.0.0", "stage", ""),
    ("net10.0", "12.0.0.0", "stage-jf12", "_jf12"),
)
EXPECTED_MEMBERS = (
    "Jellyfin.Plugin.RefreshKit.dll",
    "Jellyfin.Plugin.RefreshKit.pdb",
    "meta.json",
)
HEX_32 = re.compile(r"^[0-9a-f]{32}$")
HEX_40 = re.compile(r"^[0-9a-f]{40}$")
HEX_64 = re.compile(r"^[0-9a-f]{64}$")


class VerificationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise VerificationError(message)


def unpack_from(fmt: str, data: bytes, offset: int, label: str) -> tuple[int, ...]:
    """Read a little-endian binary structure with a useful truncation error."""
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
    """Map a PE RVA to a checked file offset without loading the image."""
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
    raise VerificationError(f"{label} RVA 0x{rva:x} is not mapped by a PE section")


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
    """Return the ECMA-335 row width for metadata tables before AssemblyRef."""
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
        33: 4,
        34: 12,
    }
    require(table in sizes, f"unsupported metadata table {table} before AssemblyRef")
    return sizes[table]


def assembly_references(data: bytes) -> dict[str, tuple[str, int]]:
    """Return AssemblyRef versions and version-field offsets from a managed PE."""
    require(data[:2] == b"MZ", "DLL is not a PE image")
    (pe_offset,) = unpack_from("<I", data, 0x3C, "DOS header")
    require(data[pe_offset:pe_offset + 4] == b"PE\0\0", "DLL has no PE signature")
    coff = pe_offset + 4
    _, section_count, _, _, _, optional_size, _ = unpack_from(
        "<HHIIIHH", data, coff, "PE COFF header"
    )
    optional = coff + 20
    (magic,) = unpack_from("<H", data, optional, "PE optional header")
    require(magic in (0x10B, 0x20B), f"unsupported PE optional-header magic 0x{magic:x}")
    directory_offset = optional + (96 if magic == 0x10B else 112)
    require(optional_size >= directory_offset - optional + (15 * 8),
            "PE optional header has no CLR data directory")
    (size_of_headers,) = unpack_from("<I", data, optional + 60, "PE SizeOfHeaders")
    clr_rva, clr_size = unpack_from(
        "<II", data, directory_offset + (14 * 8), "CLR data directory"
    )
    require(clr_rva != 0 and clr_size >= 16, "PE image has no CLR header")

    section_rows: list[tuple[int, int, int, int]] = []
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
    (signature,) = unpack_from("<I", data, metadata, "CLR metadata signature")
    require(signature == 0x424A5342, "CLR metadata signature is not BSJB")
    (version_length,) = unpack_from("<I", data, metadata + 12, "CLR metadata version")
    stream_header = metadata + 16 + ((version_length + 3) & ~3)
    _, stream_count = unpack_from("<HH", data, stream_header, "CLR stream header")
    cursor = stream_header + 4
    streams: dict[str, tuple[int, int]] = {}
    for index in range(stream_count):
        relative, stream_size = unpack_from(
            "<II", data, cursor, f"CLR stream {index} header"
        )
        name_start = cursor + 8
        name_end = data.find(b"\0", name_start, min(name_start + 32, len(data)))
        require(name_end != -1, f"CLR stream {index} has no terminated name")
        try:
            name = data[name_start:name_end].decode("ascii")
        except UnicodeDecodeError as error:
            raise VerificationError(f"CLR stream {index} name is not ASCII") from error
        absolute = metadata + relative
        require(absolute <= len(data) - stream_size, f"CLR stream {name!r} is truncated")
        streams[name] = (absolute, stream_size)
        cursor = name_start + (((name_end - name_start) + 1 + 3) & ~3)

    table_name = "#~" if "#~" in streams else "#-"
    require(table_name in streams, "CLR metadata has no table stream")
    require("#Strings" in streams, "CLR metadata has no string heap")
    tables, table_size = streams[table_name]
    strings, strings_size = streams["#Strings"]
    require(table_size >= 24, "CLR metadata table stream is truncated")
    (heap_sizes,) = unpack_from("<B", data, tables + 6, "CLR heap-size flags")
    valid, _ = unpack_from("<QQ", data, tables + 8, "CLR table masks")
    cursor = tables + 24
    counts = [0] * 64
    for table in range(64):
        if valid & (1 << table):
            (counts[table],) = unpack_from("<I", data, cursor, f"metadata table {table} count")
            cursor += 4
    if heap_sizes & 0x40:
        cursor += 4
        require(cursor <= tables + table_size, "CLR table extra-data marker is truncated")
    row_counts = tuple(counts)
    string_index_size = 4 if heap_sizes & 0x01 else 2
    guid_index_size = 4 if heap_sizes & 0x02 else 2
    blob_index_size = 4 if heap_sizes & 0x04 else 2
    for table in range(35):
        cursor += row_counts[table] * metadata_table_row_size(
            table, row_counts, string_index_size, guid_index_size, blob_index_size
        )
    require(cursor <= tables + table_size, "AssemblyRef table begins beyond table stream")

    row_size = 12 + (2 * blob_index_size) + (2 * string_index_size)
    references: dict[str, tuple[str, int]] = {}
    for row_index in range(row_counts[35]):
        row = cursor + (row_index * row_size)
        major, minor, build, revision = unpack_from(
            "<HHHH", data, row, f"AssemblyRef row {row_index} version"
        )
        name_index_offset = row + 12 + blob_index_size
        if string_index_size == 2:
            (name_index,) = unpack_from(
                "<H", data, name_index_offset, f"AssemblyRef row {row_index} name"
            )
        else:
            (name_index,) = unpack_from(
                "<I", data, name_index_offset, f"AssemblyRef row {row_index} name"
            )
        require(name_index < strings_size, f"AssemblyRef row {row_index} has invalid name index")
        name_start = strings + name_index
        name_end = data.find(b"\0", name_start, strings + strings_size)
        require(name_end != -1, f"AssemblyRef row {row_index} has unterminated name")
        try:
            name = data[name_start:name_end].decode("utf-8")
        except UnicodeDecodeError as error:
            raise VerificationError(f"AssemblyRef row {row_index} name is not UTF-8") from error
        require(name not in references, f"duplicate AssemblyRef {name!r}")
        references[name] = (f"{major}.{minor}.{build}.{revision}", row)
    return references


def file_hash(path: pathlib.Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm, usedforsecurity=False)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_mode(root: pathlib.Path, path: pathlib.Path) -> int:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise VerificationError(f"package-producing input escapes the checkout: {path}") from error
    current = root
    for part in relative.parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError as error:
            raise VerificationError(f"package-producing input is missing: {current}") from error
        require(not stat.S_ISLNK(mode), f"package-producing input uses a symlink: {current}")
    return path.lstat().st_mode


def regular_bytes(root: pathlib.Path, path: pathlib.Path) -> bytes:
    require(stat.S_ISREG(checked_mode(root, path)),
            f"package-producing input is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        require(stat.S_ISREG(os.fstat(descriptor).st_mode),
                f"package-producing input changed type: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def source_tree_hash(root: pathlib.Path) -> str:
    inputs = [
        root / "Directory.Build.props",
        root / "Directory.Build.targets",
        root / "global.json",
        root / "NuGet.Config",
        root / "jellyfin-refresh-kit.js",
        root / "plugin" / "build.sh",
        root / "scripts" / "verify-package.py",
    ]
    project = root / "plugin" / "Jellyfin.Plugin.RefreshKit"
    require(stat.S_ISDIR(checked_mode(root, project)),
            f"package project is not a regular directory: {project}")
    for path in project.rglob("*"):
        if {"bin", "obj"} & set(path.relative_to(project).parts):
            continue
        mode = checked_mode(root, path)
        if stat.S_ISDIR(mode):
            continue
        require(stat.S_ISREG(mode), f"package-producing input is not a regular file: {path}")
        inputs.append(path)
    digest = hashlib.sha256()
    for path in sorted(set(inputs), key=lambda value: value.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = regular_bytes(root, path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def project_identity(root: pathlib.Path) -> tuple[str, str]:
    project = root / "plugin" / "Jellyfin.Plugin.RefreshKit" / "Jellyfin.Plugin.RefreshKit.csproj"
    plugin = root / "plugin" / "Jellyfin.Plugin.RefreshKit" / "Plugin.cs"
    version = ET.fromstring(regular_bytes(root, project)).findtext(".//Version")
    match = re.search(
        r'new Guid\("([0-9a-fA-F-]+)"\)',
        regular_bytes(root, plugin).decode("utf-8"),
    )
    require(bool(version), f"no <Version> in {project}")
    require(bool(match), f"no plugin GUID in {plugin}")
    return str(version), match.group(1).lower()  # type: ignore[union-attr]


def git_value(root: pathlib.Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def verify_archive(
    root: pathlib.Path,
    build_dir: pathlib.Path,
    version: str,
    guid: str,
    framework: str,
    abi: str,
    stage_name: str,
    suffix: str,
    expected_tree: str,
    expected_sdk: str,
) -> dict[str, object]:
    archive_path = build_dir / f"jellyfin-refresh-kit_{version}{suffix}.zip"
    stage = build_dir / stage_name
    require(archive_path.is_file(), f"missing archive: {archive_path}")
    require(stage.is_dir(), f"missing stage directory: {stage}")

    staged_entries = tuple(
        sorted(path.relative_to(stage).as_posix() for path in stage.rglob("*"))
    )
    require(staged_entries == EXPECTED_MEMBERS,
            f"{stage_name}: expected exactly {EXPECTED_MEMBERS}, got {staged_entries}")

    with zipfile.ZipFile(archive_path) as archive:
        require(archive.testzip() is None, f"{archive_path.name}: corrupt member data")
        names = tuple(archive.namelist())
        require(names == EXPECTED_MEMBERS,
                f"{archive_path.name}: members/order must be {EXPECTED_MEMBERS}, got {names}")
        for info in archive.infolist():
            require(not info.is_dir(), f"{archive_path.name}: unexpected directory {info.filename}")
            require(info.create_system == 3,
                    f"{archive_path.name}:{info.filename}: archive creator must be Unix")
            mode = info.external_attr >> 16
            require(stat.S_ISREG(mode) and stat.S_IMODE(mode) == 0o644,
                    f"{archive_path.name}:{info.filename}: mode must be regular 0644, got {oct(mode)}")
            require(info.compress_type == zipfile.ZIP_STORED,
                    f"{archive_path.name}:{info.filename}: member must use deterministic stored mode")
            staged = (stage / info.filename).read_bytes()
            require(archive.read(info.filename) == staged,
                    f"{archive_path.name}:{info.filename}: stage/archive bytes differ")

        meta = json.loads(archive.read("meta.json"))
        dll = archive.read("Jellyfin.Plugin.RefreshKit.dll")
        pdb = archive.read("Jellyfin.Plugin.RefreshKit.pdb")
        zip_times = {info.date_time for info in archive.infolist()}

    required_meta = {
        "guid": guid,
        "name": "Jellyfin Refresh Kit",
        "targetAbi": abi,
        "framework": framework,
        "version": version,
        "status": "Active",
        "autoUpdate": True,
        "buildSdk": expected_sdk,
    }
    for key, value in required_meta.items():
        actual = meta.get(key)
        if key == "guid" and isinstance(actual, str):
            actual = actual.lower()
        require(actual == value, f"{archive_path.name}: meta.{key} is {actual!r}, expected {value!r}")

    revision = meta.get("sourceRevision")
    tree_hash = meta.get("sourceTreeSha256")
    dirty = meta.get("sourceDirty")
    epoch = meta.get("sourceDateEpoch")
    require(isinstance(revision, str) and HEX_40.fullmatch(revision) is not None,
            f"{archive_path.name}: invalid sourceRevision")
    require(isinstance(tree_hash, str) and HEX_64.fullmatch(tree_hash) is not None,
            f"{archive_path.name}: invalid sourceTreeSha256")
    require(tree_hash == expected_tree,
            f"{archive_path.name}: stale package: source tree is {expected_tree}, package records {tree_hash}")
    require(isinstance(dirty, bool), f"{archive_path.name}: sourceDirty must be boolean")
    require(isinstance(epoch, int) and epoch >= 0,
            f"{archive_path.name}: sourceDateEpoch must be a non-negative integer")
    for shipped_path in (archive_path, *(stage / name for name in EXPECTED_MEMBERS)):
        require(int(shipped_path.stat().st_mtime) == epoch,
                f"{shipped_path}: filesystem timestamp is not sourceDateEpoch {epoch}")
    expected_timestamp = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    require(meta.get("timestamp") == expected_timestamp,
            f"{archive_path.name}: timestamp is not sourceDateEpoch in UTC")
    require(len(zip_times) == 1, f"{archive_path.name}: members have different timestamps")
    zip_epoch = max(epoch, 315532800)
    zip_epoch -= zip_epoch % 2
    expected_zip_time = dt.datetime.fromtimestamp(zip_epoch, dt.timezone.utc).timetuple()[:6]
    require(next(iter(zip_times)) == expected_zip_time,
            f"{archive_path.name}: ZIP time is not the normalized source epoch")

    require(dll[:2] == b"MZ", f"{archive_path.name}: DLL is not a PE image")
    require(pdb[:4] == b"BSJB", f"{archive_path.name}: PDB is not portable")
    require(revision.encode("ascii") in dll,
            f"{archive_path.name}: DLL lacks its source revision")
    require(tree_hash.encode("ascii") in dll,
            f"{archive_path.name}: DLL lacks its source-tree digest")
    require(version.encode("ascii") in dll,
            f"{archive_path.name}: DLL lacks its declared version")
    require(b"Jellyfin.Plugin.RefreshKit" in dll,
            f"{archive_path.name}: DLL lacks the expected assembly identity")
    references = assembly_references(dll)
    for shared_name in (
        "MediaBrowser.Common",
        "MediaBrowser.Controller",
        "MediaBrowser.Model",
    ):
        require(shared_name in references,
                f"{archive_path.name}: DLL lacks required AssemblyRef {shared_name}")
        shared_version = references[shared_name][0]
        require(shared_version == abi,
                f"{archive_path.name}: AssemblyRef {shared_name} version "
                f"{shared_version} != targetAbi {abi}")
    tfm_marker = f".NETCoreApp,Version=v{framework.removeprefix('net')}".encode("ascii")
    require(tfm_marker in dll,
            f"{archive_path.name}: DLL lacks expected target framework marker {tfm_marker!r}")
    checkout = str(root.resolve()).encode("utf-8")
    require(checkout not in dll and checkout not in pdb,
            f"{archive_path.name}: checkout path leaked into DLL/PDB")
    runtime = (root / "jellyfin-refresh-kit.js").read_bytes()
    require(runtime in dll,
            f"{archive_path.name}: embedded browser runtime is not current source")

    return {
        "abi": abi,
        "framework": framework,
        "archive": archive_path,
        "name": archive_path.name,
        "bytes": archive_path.stat().st_size,
        "md5": file_hash(archive_path, "md5"),
        "sha256": file_hash(archive_path, "sha256"),
        "revision": revision,
        "tree": tree_hash,
        "dirty": dirty,
        "epoch": epoch,
        "timestamp": meta["timestamp"],
        "changelog": meta.get("changelog"),
    }


def abi_key(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as error:
        raise VerificationError(f"invalid targetAbi {value!r}") from error


def verify_manifest(
    path: pathlib.Path,
    guid: str,
    version: str,
    artifacts: list[dict[str, object]],
    mode: str,
) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(manifest, list), "manifest root must be an array")
    plugins = [entry for entry in manifest if str(entry.get("guid", "")).lower() == guid]
    require(len(plugins) == 1, f"manifest must contain exactly one entry for plugin {guid}")
    entries = [entry for entry in plugins[0].get("versions", []) if entry.get("version") == version]
    expected_abis = [row[1] for row in reversed(MATRIX)]
    actual_abis = [entry.get("targetAbi") for entry in entries]
    require(actual_abis == expected_abis,
            f"manifest version {version} must order ABIs highest first: {expected_abis}; got {actual_abis}")
    require([abi_key(str(value)) for value in actual_abis] ==
            sorted((abi_key(str(value)) for value in actual_abis), reverse=True),
            f"manifest version {version} ABIs are not descending")

    by_abi = {str(artifact["abi"]): artifact for artifact in artifacts}
    for entry in entries:
        abi = str(entry["targetAbi"])
        artifact = by_abi[abi]
        checksum = entry.get("checksum")
        require(isinstance(checksum, str) and HEX_32.fullmatch(checksum) is not None,
                f"manifest {version}/{abi}: checksum must be lowercase MD5")
        source_url = entry.get("sourceUrl")
        require(isinstance(source_url, str), f"manifest {version}/{abi}: sourceUrl is missing")
        parsed = urllib.parse.urlparse(source_url)
        expected_path = (
            f"/4eh5xitv6787h645ebv/jellyfin-refresh-kit/releases/download/v{version}/"
            f"{pathlib.Path(artifact['archive']).name}"
        )
        require(parsed.scheme == "https" and parsed.netloc == "github.com" and parsed.path == expected_path,
                f"manifest {version}/{abi}: unexpected sourceUrl {source_url!r}")
        timestamp = entry.get("timestamp")
        require(isinstance(timestamp, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", timestamp),
                f"manifest {version}/{abi}: timestamp must be UTC RFC 3339")
        require(entry.get("changelog") == artifact["changelog"],
                f"manifest {version}/{abi}: changelog does not match package metadata")
        if mode == "exact":
            require(checksum == artifact["md5"],
                    f"manifest {version}/{abi}: checksum {checksum} != package {artifact['md5']}")
            require(timestamp == artifact["timestamp"],
                    f"manifest {version}/{abi}: timestamp {timestamp} != package {artifact['timestamp']}")
        elif checksum != artifact["md5"]:
            print(
                f"    note: {abi} manifest checksum identifies the published asset; "
                f"this source build is {artifact['md5']}"
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-dir", type=pathlib.Path, default=pathlib.Path("plugin/build"))
    parser.add_argument("--manifest", type=pathlib.Path, default=pathlib.Path("manifest.json"))
    parser.add_argument("--manifest-mode", choices=("structure", "exact"), default="structure")
    parser.add_argument("--expected-revision")
    parser.add_argument("--require-clean-source", action="store_true")
    parser.add_argument("--require-immutable-snapshot", action="store_true")
    parser.add_argument(
        "--receipt",
        type=pathlib.Path,
        help="atomically write a machine-readable receipt for the verified bytes",
    )
    return parser.parse_args()


def verify_immutable_snapshot(root: pathlib.Path, build_dir: pathlib.Path) -> None:
    snapshot_root = (root / "plugin" / ".builds").resolve()
    require(
        build_dir.parent == snapshot_root and not build_dir.name.startswith(".")
        and not build_dir.name.startswith("legacy-"),
        f"build directory is not a finalized immutable snapshot: {build_dir}",
    )
    for path in (build_dir, *build_dir.rglob("*")):
        require(not path.is_symlink(), f"immutable snapshot contains a symlink: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        expected = 0o555 if path.is_dir() else 0o444
        require(mode == expected,
                f"immutable snapshot mode for {path} is {oct(mode)}, expected {oct(expected)}")


def verify_build_inventory(build_dir: pathlib.Path, version: str) -> None:
    expected = tuple(sorted((
        "stage",
        "stage-jf12",
        f"jellyfin-refresh-kit_{version}.zip",
        f"jellyfin-refresh-kit_{version}_jf12.zip",
    )))
    actual = tuple(sorted(path.name for path in build_dir.iterdir()))
    require(actual == expected,
            f"build snapshot inventory must be exactly {expected}, got {actual}")


def write_receipt(
    path: pathlib.Path,
    build_dir: pathlib.Path,
    manifest: pathlib.Path,
    manifest_mode: str,
    version: str,
    guid: str,
    expected_sdk: str,
    artifacts: list[dict[str, object]],
) -> None:
    common = artifacts[0]
    receipt = {
        "schemaVersion": 1,
        "snapshotName": build_dir.name,
        "manifestMode": manifest_mode,
        "manifestSha256": file_hash(manifest, "sha256"),
        "version": version,
        "guid": guid,
        "buildSdk": expected_sdk,
        "sourceRevision": common["revision"],
        "sourceTreeSha256": common["tree"],
        "sourceDirty": common["dirty"],
        "sourceDateEpoch": common["epoch"],
        "packages": [
            {
                "name": artifact["name"],
                "bytes": artifact["bytes"],
                "framework": artifact["framework"],
                "targetAbi": artifact["abi"],
                "md5": artifact["md5"],
                "sha256": artifact["sha256"],
            }
            for artifact in artifacts
        ],
    }
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(receipt, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> int:
    args = parse_args()
    root = pathlib.Path(__file__).resolve().parent.parent
    build_dir = args.build_dir.resolve()
    manifest = args.manifest.resolve()
    tree_hash = source_tree_hash(root)
    version, guid = project_identity(root)
    if args.require_immutable_snapshot:
        verify_immutable_snapshot(root, build_dir)
    verify_build_inventory(build_dir, version)
    expected_sdk = str(json.loads((root / "global.json").read_text(encoding="utf-8"))["sdk"]["version"])
    artifacts = [
        verify_archive(
            root, build_dir, version, guid, framework, abi, stage, suffix, tree_hash, expected_sdk
        )
        for framework, abi, stage, suffix in MATRIX
    ]

    common_fields = ("revision", "tree", "dirty", "epoch", "timestamp")
    for field in common_fields:
        require(len({artifact[field] for artifact in artifacts}) == 1,
                f"dual-target packages disagree on {field}")
    require(file_hash(build_dir / "stage" / "Jellyfin.Plugin.RefreshKit.dll", "sha256") !=
            file_hash(build_dir / "stage-jf12" / "Jellyfin.Plugin.RefreshKit.dll", "sha256"),
            "dual-target DLLs are unexpectedly identical")

    revision = str(artifacts[0]["revision"])
    commit_epoch = git_value(root, "show", "-s", "--format=%ct", revision)
    if commit_epoch is not None:
        require(str(artifacts[0]["epoch"]) == commit_epoch,
                f"package epoch {artifacts[0]['epoch']} != source commit epoch {commit_epoch}")
    expected_revision = args.expected_revision or git_value(root, "rev-parse", "HEAD")
    if expected_revision:
        require(HEX_40.fullmatch(expected_revision.lower()) is not None,
                "expected revision must be a full 40-character commit")
        require(revision == expected_revision.lower(),
                f"package revision {revision} != expected revision {expected_revision.lower()}")
    if args.require_clean_source or args.manifest_mode == "exact":
        require(artifacts[0]["dirty"] is False,
                "release verification requires sourceDirty=false")
    if args.require_clean_source:
        checkout_status = git_value(root, "status", "--porcelain", "--untracked-files=all")
        require(checkout_status is not None,
                "clean-source verification requires authoritative Git metadata")
        require(checkout_status == "",
                "clean-source verification requires an unchanged, clean checkout")

    verify_manifest(manifest, guid, version, artifacts, args.manifest_mode)
    if args.receipt:
        write_receipt(
            args.receipt,
            build_dir,
            manifest,
            args.manifest_mode,
            version,
            guid,
            expected_sdk,
            artifacts,
        )
    print("==> Package verification passed")
    print(f"    version/source : {version} / {revision}")
    print(f"    source tree    : {tree_hash} (dirty: {str(artifacts[0]['dirty']).lower()})")
    for artifact in artifacts:
        print(
            f"    {artifact['framework']} / abi {artifact['abi']}: "
            f"md5 {artifact['md5']}, sha256 {artifact['sha256']}"
        )
    if args.receipt:
        print(f"    verification receipt: {args.receipt.resolve()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (VerificationError, FileNotFoundError, json.JSONDecodeError, zipfile.BadZipFile) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
