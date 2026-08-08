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
