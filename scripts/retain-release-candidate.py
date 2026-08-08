#!/usr/bin/env python3
"""Copy the exact verified release inputs into the retained evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent.parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--build-dir", type=pathlib.Path, required=True)
    parser.add_argument("--manifest", type=pathlib.Path, required=True)
    parser.add_argument("--verification-receipt", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--manifest-revision", required=True)
    parser.add_argument("--version", required=True)
    return parser.parse_args()


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: pathlib.Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {path}")
    return value


def package_receipts(value: dict[str, object]) -> dict[str, dict[str, object]]:
    packages = value.get("packages")
    if not isinstance(packages, list):
        raise ValueError("verification receipt packages must be an array")
    result: dict[str, dict[str, object]] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise ValueError("verification receipt contains an invalid package row")
        name = str(package["name"])
        if name in result:
            raise ValueError(f"verification receipt repeats package {name}")
        result[name] = package
    return result


def require_receipt_identity(
    receipt: dict[str, object],
    build: pathlib.Path,
    manifest: pathlib.Path,
    source_revision: str,
    version: str,
    names: tuple[str, str],
) -> dict[str, dict[str, object]]:
    expected = {
        "schemaVersion": 1,
        "snapshotName": build.name,
        "manifestMode": "exact",
        "manifestSha256": sha256(manifest),
        "version": version,
        "sourceRevision": source_revision,
        "sourceDirty": False,
    }
    for field, wanted in expected.items():
        if receipt.get(field) != wanted:
            raise ValueError(
                f"verification receipt {field} is {receipt.get(field)!r}, expected {wanted!r}"
            )
    packages = package_receipts(receipt)
    if set(packages) != set(names):
        raise ValueError(
            f"verification receipt package names are {sorted(packages)}, expected {sorted(names)}"
        )
    for name, package in packages.items():
        source = build / name
        expected_hash = package.get("sha256")
        expected_bytes = package.get("bytes")
        if not isinstance(expected_hash, str) or re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            raise ValueError(f"verification receipt has invalid SHA-256 for {name}")
        if expected_bytes != source.stat().st_size or expected_hash != sha256(source):
            raise ValueError(f"build package no longer matches verification receipt: {source}")
    return packages


def main() -> int:
    args = parse_args()
    for label, revision in (
        ("source", args.source_revision), ("manifest", args.manifest_revision)
    ):
        if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
            raise ValueError(f"{label} revision must be a lowercase full commit")
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", args.version) is None:
        raise ValueError("version must contain four numeric parts")

    build = args.build_dir.resolve(strict=True)
    manifest = args.manifest.resolve(strict=True)
    verification_receipt = args.verification_receipt.resolve(strict=True)
    snapshot_root = (ROOT / "plugin" / ".builds").resolve(strict=True)
    if build.parent != snapshot_root or build.name.startswith((".", "legacy-")):
        raise ValueError(f"build directory is not a finalized immutable snapshot: {build}")
    output = args.output.resolve()
    names = (
        f"jellyfin-refresh-kit_{args.version}.zip",
        f"jellyfin-refresh-kit_{args.version}_jf12.zip",
    )

    recorded_receipt = load_json(verification_receipt)
    recorded_packages = require_receipt_identity(
        recorded_receipt,
        build,
        manifest,
        args.source_revision,
        args.version,
        names,
    )

    # Re-run exact verification against the same absolute immutable snapshot.
    # Comparing the new receipt to the workflow's earlier receipt binds the two
    # verification moments, while retained SHA-256 checks below bind copied bytes.
    with tempfile.TemporaryDirectory(prefix="rk-release-reverify-") as temporary:
        current_receipt_path = pathlib.Path(temporary) / "receipt.json"
        command = [
            sys.executable,
            str(ROOT / "scripts" / "verify-package.py"),
            "--build-dir", str(build),
            "--manifest", str(manifest),
            "--manifest-mode", "exact",
            "--expected-revision", args.source_revision,
            "--require-clean-source",
            "--require-immutable-snapshot",
            "--receipt", str(current_receipt_path),
        ]
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"release candidate re-verification failed:\n{result.stdout}")
        current_receipt = load_json(current_receipt_path)
        if current_receipt != recorded_receipt:
            raise ValueError("release candidate verification receipt changed before retention")

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"release-candidate output already exists: {output}")
    candidate = pathlib.Path(tempfile.mkdtemp(prefix=f".{output.name}.candidate-", dir=output.parent))
    try:
        packages = []
        for name in names:
            source = build / name
            if not source.is_file():
                raise ValueError(f"verified package is missing: {source}")
            target = candidate / name
            shutil.copyfile(source, target)
            packages.append({
                "name": name,
                "bytes": target.stat().st_size,
                "sha256": sha256(target),
            })
            expected = recorded_packages[name]
            if (
                sha256(source) != packages[-1]["sha256"]
                or expected["sha256"] != packages[-1]["sha256"]
                or expected["bytes"] != packages[-1]["bytes"]
            ):
                raise ValueError(
                    f"retained package does not match exact verification receipt: {source}"
                )

        final_manifest = candidate / "manifest.json"
        shutil.copyfile(manifest, final_manifest)
        if sha256(final_manifest) != recorded_receipt["manifestSha256"]:
            raise ValueError("retained manifest does not match exact verification receipt")
        retained_receipt = candidate / "verification-receipt.json"
        shutil.copyfile(verification_receipt, retained_receipt)
        if load_json(retained_receipt) != recorded_receipt:
            raise ValueError("verification receipt changed while the candidate was retained")
        provenance = {
            "schemaVersion": 1,
            "sourceRevision": args.source_revision,
            "manifestRevision": args.manifest_revision,
            "version": args.version,
            "manifestSha256": sha256(final_manifest),
            "verificationReceiptSha256": sha256(retained_receipt),
            "snapshotName": build.name,
            "packages": packages,
        }
        (candidate / "provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

        # Flush the complete candidate before its single directory rename, so
        # an interrupted runner never exposes a half-populated retained set.
        for path in sorted(candidate.iterdir()):
            descriptor = os.open(path, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        descriptor = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(candidate, output)
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)
    print(f"==> Retained exact verified release candidate in {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (FileExistsError, OSError, ValueError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
