#!/usr/bin/env python3
"""Enforce the fixed 1.0 release-campaign clock and publication policy."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time


FINAL_VERSION = "1.0.1.0"
MILESTONE = re.compile(r"^1\.0\.0\.([1-9][0-9]*)$")
BOUNDARY_WINDOW_SECONDS = 24 * 60 * 60
# The persistent goal's authoritative createdAt value.  Milestone callers must
# not be able to move the clock origin or nominate a convenient boundary.
CAMPAIGN_START_EPOCH = 1786193837
REVISION = re.compile(r"^[0-9a-f]{40}$")


class PolicyError(ValueError):
    pass


def validate_policy(
    kind: str,
    version: str,
    source_epoch: int,
    now_epoch: int,
) -> dict[str, object]:
    if min(source_epoch, now_epoch) < 0:
        raise PolicyError("release epochs must be non-negative")
    if now_epoch < CAMPAIGN_START_EPOCH:
        raise PolicyError("release validation predates the active campaign")
    if source_epoch < CAMPAIGN_START_EPOCH:
        raise PolicyError("release source commit predates the active campaign")
    if source_epoch > now_epoch:
        raise PolicyError("source commit timestamp is in the future")

    milestone_number: int | None = None
    boundary_epoch = 0
    if kind == "final":
        if version != FINAL_VERSION:
            raise PolicyError(f"final release version must be {FINAL_VERSION}, got {version}")
        boundary_epoch = CAMPAIGN_START_EPOCH + BOUNDARY_WINDOW_SECONDS
        if now_epoch < boundary_epoch:
            raise PolicyError("the campaign has not reached its first 24-hour boundary")
        if source_epoch < boundary_epoch:
            raise PolicyError("final release source commit predates the first boundary")
    elif kind == "milestone":
        match = MILESTONE.fullmatch(version)
        if match is None:
            raise PolicyError("milestone version must match 1.0.0.n with n >= 1")
        milestone_number = int(match.group(1))
        active_number = (now_epoch - CAMPAIGN_START_EPOCH) // BOUNDARY_WINDOW_SECONDS
        if active_number < 1:
            raise PolicyError("the campaign has not reached its first 24-hour milestone")
        if milestone_number != active_number:
            raise PolicyError(
                f"active milestone is 1.0.0.{active_number}, got {version}"
            )
        boundary_epoch = (
            CAMPAIGN_START_EPOCH + milestone_number * BOUNDARY_WINDOW_SECONDS
        )
        if source_epoch < boundary_epoch:
            raise PolicyError("milestone source commit predates its active boundary")
    else:
        raise PolicyError(f"unknown release kind: {kind}")

    return {
        "schemaVersion": 2,
        "campaignStartEpoch": CAMPAIGN_START_EPOCH,
        "kind": kind,
        "version": version,
        "milestoneNumber": milestone_number,
        "boundaryEpoch": boundary_epoch,
        "sourceEpoch": source_epoch,
    }


def validate_validation_dispatch(
    version: str,
    event_ref: str,
    event_revision: str,
    manifest_revision: str,
    existing_tag_object: str,
) -> None:
    """Require validation from the version's temporary immutable candidate ref."""
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", version) is None:
        raise PolicyError("validation version must contain four numeric parts")
    candidate_ref = f"refs/heads/release-candidate/v{version}"
    if event_ref != candidate_ref:
        raise PolicyError(f"release validation must be dispatched from {candidate_ref}")
    for label, revision in (
        ("event", event_revision),
        ("manifest", manifest_revision),
    ):
        if REVISION.fullmatch(revision) is None:
            raise PolicyError(f"{label} revision must be a lowercase full commit")
    if event_revision != manifest_revision:
        raise PolicyError("release validation event must point at the manifest child")
    if existing_tag_object:
        raise PolicyError("release tag must not exist before validation succeeds")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("milestone", "final"), required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-epoch", type=int, required=True)
    parser.add_argument("--now-epoch", type=int, default=None)
    parser.add_argument("--validation-dispatch", action="store_true")
    parser.add_argument("--event-ref")
    parser.add_argument("--event-revision")
    parser.add_argument("--manifest-revision")
    parser.add_argument("--existing-tag-object")
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = validate_policy(
        args.kind,
        args.version,
        args.source_epoch,
        int(time.time()) if args.now_epoch is None else args.now_epoch,
    )
    dispatch_values = (
        args.event_ref,
        args.event_revision,
        args.manifest_revision,
        args.existing_tag_object,
    )
    if args.validation_dispatch:
        if any(value is None for value in dispatch_values):
            raise PolicyError(
                "validation dispatch requires event ref/revision, manifest revision, and tag state"
            )
        validate_validation_dispatch(args.version, *dispatch_values)
    elif any(value is not None for value in dispatch_values):
        raise PolicyError("validation dispatch fields require --validation-dispatch")
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, PolicyError) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
