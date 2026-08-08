#!/usr/bin/env python3
"""Restrict a release child to current-version checksum/timestamp finalization."""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import re
import sys


ABIS = ("12.0.0.0", "10.11.0.0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=pathlib.Path, required=True)
    parser.add_argument("--after", type=pathlib.Path, required=True)
    parser.add_argument("--guid", required=True)
    parser.add_argument("--version", required=True)
    return parser.parse_args()


def load(path: pathlib.Path) -> list[object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError(f"{path}: manifest root must be an array")
    return value


def plugin(manifest: list[object], guid: str, label: str) -> dict[str, object]:
    matches = [
        item for item in manifest
        if isinstance(item, dict) and str(item.get("guid", "")).lower() == guid.lower()
    ]
    if len(matches) != 1:
        raise ValueError(f"{label}: expected one plugin {guid}, found {len(matches)}")
    return matches[0]


def version_entry(
    plugin_value: dict[str, object], version: str, abi: str, label: str
) -> dict[str, object]:
    versions = plugin_value.get("versions")
    if not isinstance(versions, list):
        raise ValueError(f"{label}: plugin versions must be an array")
    matches = [
        item for item in versions
        if isinstance(item, dict)
        and item.get("version") == version
        and item.get("targetAbi") == abi
    ]
    if len(matches) != 1:
        raise ValueError(
            f"{label}: expected one {version}/{abi} entry, found {len(matches)}"
        )
    return matches[0]


def main() -> int:
    args = parse_args()
    before = load(args.before)
    after = load(args.after)
    normalized = copy.deepcopy(before)
    before_plugin = plugin(normalized, args.guid, "source manifest")
    after_plugin = plugin(after, args.guid, "final manifest")
    changes: list[str] = []
    for abi in ABIS:
        before_entry = version_entry(before_plugin, args.version, abi, "source manifest")
        after_entry = version_entry(after_plugin, args.version, abi, "final manifest")
        old_checksum = before_entry.get("checksum")
        new_checksum = after_entry.get("checksum")
        if not isinstance(new_checksum, str) or re.fullmatch(r"[0-9a-f]{32}", new_checksum) is None:
            raise ValueError(f"final manifest: {args.version}/{abi} checksum is not lowercase MD5")
        if new_checksum == old_checksum:
            raise ValueError(f"final manifest: {args.version}/{abi} checksum was not finalized")
        for field in ("checksum", "timestamp"):
            if field not in after_entry:
                raise ValueError(f"final manifest: {args.version}/{abi} lacks {field}")
            if before_entry.get(field) != after_entry.get(field):
                changes.append(f"{args.version}/{abi}/{field}")
            before_entry[field] = after_entry[field]

    if normalized != after:
        raise ValueError(
            "manifest child changes data outside the two current-version checksum/timestamp pairs"
        )
    print("==> Manifest child is restricted to release finalization")
    for change in changes:
        print(f"    {change}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
