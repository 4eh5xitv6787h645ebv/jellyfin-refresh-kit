#!/usr/bin/env python3
"""Focused, network-free regressions for release/evidence helper contracts."""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True


def load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {relative}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("rk_release_policy", "scripts/verify-release-policy.py")
assets = load("rk_release_assets", "scripts/verify-release-assets.py")
retainer = load("rk_release_retainer", "scripts/retain-release-candidate.py")
collector = load("rk_evidence_collector", "scripts/collect-ci-evidence.py")
validation_run = load("rk_validation_run", "scripts/verify-validation-run.py")
evidence_validation = load("rk_evidence_validation", "scripts/evidence_validation.py")
abi_floor = load("rk_abi_floor_evidence", "scripts/abi_floor_evidence.py")
host_evidence = load("rk_host_upgrade_evidence", "scripts/host_upgrade_evidence.py")
host_semantics = load(
    "rk_host_upgrade_semantics", "e2e/jellyfin/lib/verify-host-upgrade-results.py"
)


class ReleasePolicyTests(unittest.TestCase):
    def test_final_is_exact(self) -> None:
        boundary = policy.CAMPAIGN_START_EPOCH + policy.BOUNDARY_WINDOW_SECONDS
        source = boundary + 10
        result = policy.validate_policy("final", "1.0.1.0", source, source + 1)
        self.assertEqual(result["kind"], "final")
        self.assertEqual(result["campaignStartEpoch"], 1786193837)
        self.assertEqual(result["boundaryEpoch"], boundary)
        self.assertNotIn("validatedAtEpoch", result)
        self.assertEqual(
            result,
            policy.validate_policy("final", "1.0.1.0", source, source + 10_000),
        )
        with self.assertRaises(policy.PolicyError):
            policy.validate_policy("final", "1.0.1.1", source, source + 1)
        with self.assertRaises(policy.PolicyError):
            policy.validate_policy("final", "1.0.1.0", boundary, boundary - 1)
        with self.assertRaises(policy.PolicyError):
            policy.validate_policy("final", "1.0.1.0", boundary - 1, boundary)

    def test_milestone_is_derived_from_fixed_campaign_clock(self) -> None:
        start = policy.CAMPAIGN_START_EPOCH
        day = policy.BOUNDARY_WINDOW_SECONDS
        with self.assertRaises(policy.PolicyError):
            policy.validate_policy("milestone", "1.0.0.1", start + 1, start + day - 1)

        boundary = start + 4 * day
        result = policy.validate_policy("milestone", "1.0.0.4", boundary, boundary)
        self.assertEqual(result["milestoneNumber"], 4)
        self.assertEqual(result["boundaryEpoch"], boundary)
        with self.assertRaises(policy.PolicyError):
            policy.validate_policy("milestone", "1.0.0.999", boundary, boundary)
        with self.assertRaises(policy.PolicyError):
            policy.validate_policy("milestone", "1.0.0.4", boundary - 1, boundary)
        with self.assertRaises(policy.PolicyError):
            policy.validate_policy("milestone", "1.0.0.4", boundary, boundary + day)

    def test_validation_dispatch_requires_candidate_manifest_child_and_absent_tag(self) -> None:
        manifest = "b" * 40
        version = "1.0.1.0"
        candidate = f"refs/heads/release-candidate/v{version}"
        policy.validate_validation_dispatch(version, candidate, manifest, manifest, "")
        for event_ref, event_revision, tag_commit in (
            ("refs/heads/main", manifest, ""),
            (candidate, "a" * 40, ""),
            (candidate, manifest, "a" * 40),
        ):
            with self.subTest(
                event_ref=event_ref,
                event_revision=event_revision,
                tag_commit=tag_commit,
            ), self.assertRaises(policy.PolicyError):
                policy.validate_validation_dispatch(
                    version, event_ref, event_revision, manifest, tag_commit
                )

    def test_workflow_invokes_the_dispatch_contract_without_boundary_input(self) -> None:
        validation = (ROOT / ".github/workflows/release-validation.yml").read_text(
            encoding="utf-8"
        )
        post_release = (ROOT / ".github/workflows/post-release-assets.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("--validation-dispatch", validation)
        self.assertIn("refs/heads/release-candidate/v$EXPECTED_VERSION", validation)
        self.assertGreaterEqual(validation.count("git ls-remote --refs origin"), 4)
        self.assertIn("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c", validation)
        self.assertIn("--candidate-ref \"$CANDIDATE_REF\"", validation)
        self.assertIn("EVENT_REF: ${{ github.ref }}", post_release)
        self.assertIn("test \"$EVENT_REF\" = refs/heads/main", post_release)
        self.assertGreaterEqual(
            post_release.count('git ls-remote --refs origin "$CANDIDATE_REF"'), 2
        )
        self.assertNotIn("active_boundary_epoch", validation)
        self.assertNotIn("active_boundary_epoch", post_release)

    def test_release_validation_is_five_bounded_jobs(self) -> None:
        validation = (ROOT / ".github/workflows/release-validation.yml").read_text(
            encoding="utf-8"
        )
        jobs = re.findall(r"^  ([a-z][a-z0-9_]*):\n    name:", validation, re.MULTILINE)
        self.assertEqual(
            jobs,
            [
                "preflight",
                "fast_repro_security",
                "integration_gate",
                "compatibility_gate",
                "final_retain",
            ],
        )
        timeouts = [int(value) for value in re.findall(r"timeout-minutes: ([0-9]+)", validation)]
        self.assertEqual(len(timeouts), 5)
        self.assertTrue(all(value <= 360 for value in timeouts))
        self.assertIn("branches-ignore:", (ROOT / ".github/workflows/ci.yml").read_text())


class BuildIsolationTests(unittest.TestCase):
    def test_package_and_test_invocations_disable_ancestor_analyzer_configs(self) -> None:
        for relative in ("plugin/build.sh", "test.sh"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("DiscoverEditorConfigFiles=false", text, relative)
            self.assertIn("DiscoverGlobalAnalyzerConfigFiles=false", text, relative)

    def test_static_gate_uses_checksum_pinned_workflow_parser(self) -> None:
        gate = (ROOT / "test.sh").read_text(encoding="utf-8")
        checker = (ROOT / "scripts" / "check-workflows.sh").read_text(encoding="utf-8")
        self.assertIn("bash scripts/check-workflows.sh", gate)
        self.assertIn("VERSION='1.7.10'", checker)
        self.assertGreaterEqual(len(re.findall(r"'[0-9a-f]{64}'", checker)), 2)
        self.assertIn("sha256sum -c", checker)
        self.assertIn('workflow_root.glob("*.yml")', gate)
        self.assertIn('workflow_root.glob("*.yaml")', gate)
        self.assertIn("bash e2e/jellyfin/run.sh runner-negative", gate)

    def test_reproducibility_copy_excludes_generated_runner_state(self) -> None:
        checker = (ROOT / "scripts" / "check-reproducible.sh").read_text(
            encoding="utf-8"
        )
        for exclusion in (
            "/plugin/.build-rollback.*",
            "__pycache__/",
            "*.py[cod]",
            "/e2e/proxy/.state/",
            "/e2e/compat/.cache/",
            "/e2e/compat/.state/",
            "/e2e/compat/artifacts/",
        ):
            self.assertIn(exclusion, checker)

    def test_integration_tee_and_empty_log_failures_are_gating(self) -> None:
        gate = (ROOT / "test.sh").read_text(encoding="utf-8")
        collector_text = (ROOT / "scripts" / "collect-ci-evidence.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(gate.count('pipeline_status=("${PIPESTATUS[@]}")'), 4)
        for variable in (
            "abi_floor_tee_rc",
            "jellyfin_tee_rc",
            "host_upgrade_tee_rc",
            "proxy_tee_rc",
        ):
            self.assertIn(f'[ "${{{variable}}}" -eq 0 ] || result=1', gate)
        self.assertIn("source.stat().st_size == 0", collector_text)
        self.assertNotIn(
            '(COMPAT_ARTIFACTS / "summary.json").is_file()', collector_text
        )
        self.assertIn("empty required integration log", collector_text)
        self.assertLess(
            gate.index("bash e2e/jellyfin/run.sh clean"),
            gate.index("bash e2e/jellyfin/run.sh lifecycle all"),
        )

    def test_compat_restore_precedes_browser_generation_evidence(self) -> None:
        gate = (ROOT / "test.sh").read_text(encoding="utf-8")
        runner = (ROOT / "e2e" / "jellyfin" / "run.sh").read_text(encoding="utf-8")

        # compat resets/reprovisions JF12 and its restore check rewrites the
        # canonical jf12/server evidence. Browser evidence must therefore be
        # captured afterwards so both artifacts name the same generation;
        # third-party remains first because compat invalidates its lifecycle
        # completion prerequisite.
        for source, commands in (
            (
                gate,
                (
                    "bash e2e/jellyfin/run.sh third-party all",
                    "bash e2e/jellyfin/run.sh compat",
                    "bash e2e/jellyfin/run.sh browser all",
                    "bash e2e/jellyfin/run.sh host-upgrade all",
                ),
            ),
            (
                runner,
                (
                    "cmd_third_party all",
                    "cmd_compat || return $?",
                    "cmd_browser all",
                    "cmd_host_upgrade all",
                ),
            ),
        ):
            positions = [source.index(command) for command in commands]
            self.assertEqual(positions, sorted(positions))


class ReleaseAssetTests(unittest.TestCase):
    def test_authenticated_download_redirects_strip_credentials_and_stay_trusted(self) -> None:
        modules = (assets, validation_run)
        for module in modules:
            with self.subTest(module=module.__name__):
                request = module.urllib.request.Request(
                    "https://api.github.com/repos/owner/project/actions/artifacts/1/zip",
                    headers={
                        "Authorization": "Bearer secret-value",
                        "Cookie": "session=secret-value",
                        "Proxy-Authorization": "Basic secret-value",
                    },
                )
                redirected = module.TrustedRedirectHandler().redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {},
                    "https://results.blob.core.windows.net/container/artifact.zip",
                )
                self.assertIsNotNone(redirected)
                assert redirected is not None
                retained_headers = {
                    name.lower(): value for name, value in redirected.header_items()
                }
                for name in ("authorization", "cookie", "proxy-authorization"):
                    self.assertNotIn(name, retained_headers)
                with self.assertRaises((assets.AssetError, validation_run.ValidationError)):
                    module.TrustedRedirectHandler().redirect_request(
                        request,
                        None,
                        302,
                        "Found",
                        {},
                        "https://attacker.invalid/artifact.zip",
                    )

    def test_asset_selection_is_exact_and_unique(self) -> None:
        names = ("a.zip", "b.zip")
        selected = assets.select_assets({"assets": [{"name": "a.zip"}, {"name": "b.zip"}]}, names)
        self.assertEqual(set(selected), set(names))
        with self.assertRaises(assets.AssetError):
            assets.select_assets({"assets": [{"name": "a.zip"}]}, names)
        with self.assertRaises(assets.AssetError):
            assets.select_assets(
                {"assets": [{"name": "a.zip"}, {"name": "a.zip"}, {"name": "b.zip"}]},
                names,
            )
        with self.assertRaises(assets.AssetError):
            assets.select_assets(
                {"assets": [{"name": "a.zip"}, {"name": "b.zip"}, {"name": "notes.txt"}]},
                names,
            )

    def test_release_kind_and_published_timestamp_are_bound(self) -> None:
        release = {
            "tag_name": "v1.0.0.4",
            "draft": False,
            "prerelease": True,
            "published_at": "2026-08-09T01:02:03Z",
        }
        self.assertEqual(
            assets.validate_release_state(release, "v1.0.0.4", "milestone"),
            1786237323,
        )
        with self.assertRaises(assets.AssetError):
            assets.validate_release_state(release, "v1.0.0.4", "final")


class ValidationRunTests(unittest.TestCase):
    def test_api_run_and_artifact_identity_are_exact(self) -> None:
        repository = "owner/project"
        source = "a" * 40
        manifest_revision = "b" * 40
        candidate_ref = "refs/heads/release-candidate/v1.0.1.0"
        run_id = 1234
        published_epoch = validation_run.github_epoch(
            "2026-08-09T01:02:03Z", "published_at"
        )
        run = {
            "id": run_id,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_sha": manifest_revision,
            "head_branch": "release-candidate/v1.0.1.0",
            "path": validation_run.WORKFLOW_PATH,
            "run_attempt": 2,
            "created_at": "2026-08-09T00:59:00Z",
            "run_started_at": "2026-08-09T01:00:00Z",
            "updated_at": "2026-08-09T01:01:00Z",
            "repository": {"full_name": repository},
            "html_url": f"https://github.com/{repository}/actions/runs/{run_id}",
        }
        attempt, completed_epoch = validation_run.validate_run(
            run, repository, run_id, manifest_revision, candidate_ref, published_epoch
        )
        self.assertEqual(attempt, 2)
        for field, bad in (
            ("conclusion", "failure"),
            ("head_sha", source),
            ("path", ".github/workflows/ci.yml"),
            ("event", "push"),
        ):
            changed = dict(run)
            changed[field] = bad
            with self.subTest(field=field), self.assertRaises(validation_run.ValidationError):
                validation_run.validate_run(
                    changed,
                    repository,
                    run_id,
                    manifest_revision,
                    candidate_ref,
                    published_epoch,
                )
        for completed_at in (
            "2026-08-09T01:02:03Z",
            "2026-08-09T01:03:00Z",
        ):
            postdated_run = {**run, "updated_at": completed_at}
            with self.subTest(completed_at=completed_at), self.assertRaises(
                validation_run.ValidationError
            ):
                validation_run.validate_run(
                    postdated_run,
                    repository,
                    run_id,
                    manifest_revision,
                    candidate_ref,
                    published_epoch,
                )

        name = f"release-validation-{source}-2"
        artifact = {
            "id": 88,
            "name": name,
            "expired": False,
            "workflow_run": {"id": run_id},
            "size_in_bytes": 100,
            "archive_download_url": (
                "https://api.github.com/repos/owner/project/actions/artifacts/88/zip"
            ),
            "digest": "sha256:" + "c" * 64,
            "created_at": "2026-08-09T01:00:30Z",
        }
        payload = {"total_count": 1, "artifacts": [artifact]}
        self.assertEqual(
            validation_run.select_artifact(
                payload,
                name,
                run_id,
                repository,
                completed_epoch,
                published_epoch,
            ),
            artifact,
        )
        for changed_payload in (
            {"total_count": 2, "artifacts": [artifact]},
            {"total_count": 2, "artifacts": [artifact, dict(artifact)]},
            {
                "total_count": 1,
                "artifacts": [{**artifact, "expired": True}],
            },
            {
                "total_count": 1,
                "artifacts": [{
                    **artifact,
                    "archive_download_url": (
                        "https://api.github.com/repos/owner/project/actions/artifacts/89/zip"
                    ),
                }],
            },
            {
                "total_count": 1,
                "artifacts": [{
                    **artifact,
                    "archive_download_url": (
                        "https://api.github.com/repos/other/project/actions/artifacts/88/zip"
                    ),
                }],
            },
        ):
            with self.assertRaises(validation_run.ValidationError):
                validation_run.select_artifact(
                    changed_payload,
                    name,
                    run_id,
                    repository,
                    completed_epoch,
                    published_epoch,
                )

        for created_at in (
            "2026-08-09T01:02:03Z",
            "2026-08-09T01:03:00Z",
        ):
            postdated_artifact = {**artifact, "created_at": created_at}
            with self.subTest(created_at=created_at), self.assertRaises(
                validation_run.ValidationError
            ):
                validation_run.select_artifact(
                    {"total_count": 1, "artifacts": [postdated_artifact]},
                    name,
                    run_id,
                    repository,
                    published_epoch + 120,
                    published_epoch,
                )

    def test_artifact_checksum_manifest_rejects_unlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            extracted = pathlib.Path(temporary)
            payload = extracted / "run.json"
            payload.write_text("{}\n", encoding="utf-8")
            nested = extracted / "workers" / "fast"
            nested.mkdir(parents=True)
            (nested / "SHA256SUMS").write_text("", encoding="utf-8")
            (extracted / "SHA256SUMS").write_text(
                f"{validation_run.sha256(payload)}  run.json\n"
                f"{validation_run.sha256(nested / 'SHA256SUMS')}  workers/fast/SHA256SUMS\n",
                encoding="utf-8",
            )
            self.assertEqual(validation_run.evidence_root(extracted), extracted)
            (extracted / "unlisted.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(validation_run.ValidationError):
                validation_run.evidence_root(extracted)

    def test_retained_candidate_is_bound_to_rebuilt_bytes_and_heavy_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            evidence = root / "evidence"
            candidate = evidence / "release-candidate"
            build = root / "snapshot-exact"
            candidate.mkdir(parents=True)
            build.mkdir()
            for name in ("lab", "logs", "compat", "workers"):
                (evidence / name).mkdir()
            (evidence / "SHA256SUMS").write_text("", encoding="utf-8")
            source = "a" * 40
            manifest_revision = "b" * 40
            version = "1.0.1.0"
            repository = "owner/project"
            candidate_ref = f"refs/heads/release-candidate/v{version}"
            manifest = root / "manifest.json"
            manifest.write_text("[]\n", encoding="utf-8")
            for stage_name in ("stage", "stage-jf12"):
                stage = build / stage_name
                stage.mkdir()
                (stage / "meta.json").write_text(
                    json.dumps({"version": version, "fixture": stage_name}),
                    encoding="utf-8",
                )
            names = (
                f"jellyfin-refresh-kit_{version}.zip",
                f"jellyfin-refresh-kit_{version}_jf12.zip",
            )
            packages = []
            for index, name in enumerate(names):
                payload = f"validated-package-{index}".encode()
                (candidate / name).write_bytes(payload)
                (build / name).write_bytes(payload)
                packages.append({
                    "name": name,
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                })
            (candidate / "manifest.json").write_bytes(manifest.read_bytes())
            receipt = {
                "schemaVersion": 1,
                "snapshotName": build.name,
                "manifestMode": "exact",
                "manifestSha256": validation_run.sha256(manifest),
                "version": version,
                "sourceRevision": source,
                "sourceDirty": False,
                "packages": packages,
            }
            receipt_path = candidate / "verification-receipt.json"
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
            provenance = {
                "schemaVersion": 1,
                "sourceRevision": source,
                "manifestRevision": manifest_revision,
                "version": version,
                "manifestSha256": validation_run.sha256(manifest),
                "verificationReceiptSha256": validation_run.sha256(receipt_path),
                "snapshotName": build.name,
                "packages": packages,
            }
            (candidate / "provenance.json").write_text(
                json.dumps(provenance), encoding="utf-8"
            )
            policy_receipt = policy.validate_policy(
                "final",
                version,
                policy.CAMPAIGN_START_EPOCH + policy.BOUNDARY_WINDOW_SECONDS,
                policy.CAMPAIGN_START_EPOCH + policy.BOUNDARY_WINDOW_SECONDS + 1,
            )
            release_policy_path = root / "release-policy.json"
            release_policy_path.write_text(
                json.dumps(policy_receipt, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            (candidate / "release-policy.json").write_bytes(
                release_policy_path.read_bytes()
            )
            current_receipt = root / "current-receipt.json"
            current_receipt.write_bytes(receipt_path.read_bytes())
            run_id, attempt = 1234, 2
            package_build = evidence_validation.package_identity(build)
            compatibility_collected = {
                path
                for path in validation_run.REQUIRED_COLLECTED
                if path.startswith("compat/")
            }
            integration_collected = {
                path
                for path in validation_run.REQUIRED_COLLECTED
                if not path.startswith("compat/")
            }
            run = {
                "schemaVersion": 2,
                "sourceRevision": source,
                "sourceDirty": False,
                "sourceStatus": [],
                "candidateRef": candidate_ref,
                "exitStatus": dict(validation_run.REQUIRED_EXIT_STATUS),
                "missing": [],
                "collected": sorted(integration_collected | compatibility_collected),
                "github": {
                    "GITHUB_RUN_ID": str(run_id),
                    "GITHUB_RUN_ATTEMPT": str(attempt),
                    "GITHUB_WORKFLOW": validation_run.WORKFLOW_NAME,
                    "GITHUB_SHA": manifest_revision,
                    "GITHUB_REF": candidate_ref,
                    "GITHUB_JOB": "final_retain",
                    "GITHUB_REPOSITORY": repository,
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_WORKFLOW_REF": (
                        f"{repository}/.github/workflows/release-validation.yml@"
                        f"{candidate_ref}"
                    ),
                    "GITHUB_WORKFLOW_SHA": manifest_revision,
                },
                "gates": {
                    "fast": "success",
                    "reproducibility": "success",
                    "securityAudit": "success",
                    "integration": "success",
                    "compatibility": "success",
                },
                "packageBuild": package_build,
            }
            (evidence / "run.json").write_text(json.dumps(run), encoding="utf-8")

            def validate(
                integration_error: Exception | None = None,
            ) -> dict[str, object]:
                integration_gate = mock.patch.object(
                    validation_run,
                    "validate_integration_tree",
                    return_value=integration_collected
                    if integration_error is None
                    else mock.DEFAULT,
                    side_effect=integration_error,
                )
                with (
                    integration_gate,
                    mock.patch.object(
                        validation_run,
                        "validate_compatibility_tree",
                        return_value=compatibility_collected,
                    ),
                    mock.patch.object(
                        validation_run, "validate_retained_workers"
                    ) as worker_gate,
                ):
                    result = validation_run.validate_candidate_bundle(
                        evidence,
                        build,
                        manifest,
                        current_receipt,
                        release_policy_path,
                        repository,
                        source,
                        manifest_revision,
                        candidate_ref,
                        version,
                        run_id,
                        attempt,
                    )
                    worker_gate.assert_called_once_with(
                        evidence,
                        build,
                        repository,
                        source,
                        manifest_revision,
                        candidate_ref,
                        run_id,
                        attempt,
                        version,
                    )
                    return result

            result = validate()
            self.assertEqual(result["packages"], packages)

            with self.assertRaises(validation_run.ValidationError):
                validate(
                    validation_run.EvidenceValidationError(
                    "stale semantic result"
                    )
                )

            run["exitStatus"]["hostUpgradeLab"] = False
            (evidence / "run.json").write_text(json.dumps(run), encoding="utf-8")
            with self.assertRaises(validation_run.ValidationError):
                validate()
            run["exitStatus"]["hostUpgradeLab"] = 0
            (evidence / "run.json").write_text(json.dumps(run), encoding="utf-8")

            current_receipt.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(validation_run.ValidationError):
                validate()
            current_receipt.write_bytes(receipt_path.read_bytes())
            (build / names[0]).write_bytes(b"different")
            with self.assertRaises(validation_run.ValidationError):
                validate()

            (build / names[0]).write_bytes(b"validated-package-0")
            release_policy_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(validation_run.ValidationError):
                validate()


class RetentionReceiptTests(unittest.TestCase):
    def test_receipt_binds_snapshot_manifest_and_both_packages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            build = root / "snapshot"
            build.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text("[]\n", encoding="utf-8")
            names = ("jellyfin-refresh-kit_1.0.1.0.zip", "jellyfin-refresh-kit_1.0.1.0_jf12.zip")
            rows = []
            for index, name in enumerate(names):
                data = f"package-{index}".encode()
                (build / name).write_bytes(data)
                rows.append({"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            receipt = {
                "schemaVersion": 1,
                "snapshotName": build.name,
                "manifestMode": "exact",
                "manifestSha256": retainer.sha256(manifest),
                "version": "1.0.1.0",
                "sourceRevision": "a" * 40,
                "sourceDirty": False,
                "packages": rows,
            }
            result = retainer.require_receipt_identity(
                receipt, build, manifest, "a" * 40, "1.0.1.0", names
            )
            self.assertEqual(set(result), set(names))
            (build / names[0]).write_bytes(b"changed")
            with self.assertRaises(ValueError):
                retainer.require_receipt_identity(
                    receipt, build, manifest, "a" * 40, "1.0.1.0", names
                )


class IntegrationEvidenceTests(unittest.TestCase):
    @staticmethod
    def make_build(root: pathlib.Path) -> pathlib.Path:
        build = root / "snapshot-exact"
        build.mkdir()
        for target, stage_name, suffix, framework, abi in (
            ("jf10", "stage", "", "net9.0", "10.11.0.0"),
            ("jf12", "stage-jf12", "_jf12", "net10.0", "12.0.0.0"),
        ):
            stage = build / stage_name
            stage.mkdir()
            (stage / "meta.json").write_text(
                json.dumps({
                    "version": "1.0.1.0",
                    "guid": "515255fe-3332-49b0-b471-0be58c8221d8",
                    "framework": framework,
                    "targetAbi": abi,
                    "sourceRevision": "a" * 40,
                    "sourceTreeSha256": "b" * 64,
                    "sourceDateEpoch": 1_786_193_837,
                    "sourceDirty": False,
                }),
                encoding="utf-8",
            )
            (stage / "Jellyfin.Plugin.RefreshKit.dll").write_bytes(
                f"dll-{target}".encode()
            )
            (build / f"jellyfin-refresh-kit_1.0.1.0{suffix}.zip").write_bytes(
                f"package-{target}".encode()
            )
        return build

    @staticmethod
    def self_result(target: str, build: pathlib.Path) -> dict[str, object]:
        stage_name = "stage" if target == "jf10" else "stage-jf12"
        suffix = "" if target == "jf10" else "_jf12"
        stage = json.loads((build / stage_name / "meta.json").read_text(encoding="utf-8"))
        package = build / f"jellyfin-refresh-kit_1.0.1.0{suffix}.zip"
        return {
            "target": target,
            "completed": True,
            "failures": [],
            "unexpectedRefreshKitBrowserErrors": [],
            "repositoryConfigurationRemoved": True,
            "captureCounts": {
                name: {"truncated": False}
                for name in ("primary", "secondary", "background")
            },
            "phases": [{"name": name} for name in sorted(collector.SELF_LIFECYCLE_PHASES)],
            "metadata": {
                "target": target,
                "candidateVersion": stage["version"],
                "embeddedCandidateVersion": stage["version"],
                "candidateSourceRevision": stage["sourceRevision"],
                "candidateSourceTreeSha256": stage["sourceTreeSha256"],
                "candidateMd5": collector.file_md5(package),
            },
        }

    @staticmethod
    def third_party_result(target: str, build: pathlib.Path) -> dict[str, object]:
        stage_name = "stage" if target == "jf10" else "stage-jf12"
        stage = json.loads((build / stage_name / "meta.json").read_text(encoding="utf-8"))
        return {
            "target": target,
            "completed": True,
            "failures": [],
            "unexpectedRefreshKitBrowserErrors": [],
            "versionFlapWarnings": [],
            "repositoryConfigurationRemoved": True,
            "captureCounts": {
                name: {"truncated": False}
                for name in ("primary", "secondary", "background")
            },
            "phases": [
                {"name": name} for name in sorted(collector.THIRD_PARTY_LIFECYCLE_PHASES)
            ],
            "metadata": {
                "target": target,
                "refreshKitSnapshot": build.name,
                "refreshKitPackageVersion": stage["version"],
                "refreshKitSourceRevision": stage["sourceRevision"],
                "refreshKitSourceTreeSha256": stage["sourceTreeSha256"],
            },
        }

    def test_all_self_and_third_party_results_are_required_and_bound(self) -> None:
        expected_paths = {
            f"{target}/{kind}/result.json"
            for target in ("jf10", "jf12")
            for kind in ("lifecycle", "third-party-lifecycle")
        }
        self.assertTrue(expected_paths.issubset(set(collector.SAFE_JSON)))
        with tempfile.TemporaryDirectory() as temporary:
            build = self.make_build(pathlib.Path(temporary))
            for target in ("jf10", "jf12"):
                cases = (
                    (f"{target}/lifecycle/result.json", self.self_result(target, build)),
                    (
                        f"{target}/third-party-lifecycle/result.json",
                        self.third_party_result(target, build),
                    ),
                )
                for relative, result in cases:
                    with self.subTest(relative=relative):
                        errors: list[str] = []
                        collector.validate_integration_lifecycle(relative, result, build, errors)
                        self.assertEqual(errors, [])
                        evidence_validation.validate_lifecycle(relative, result, build)

    def test_strict_lifecycle_validation_rejects_false_green_and_stale_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build = self.make_build(pathlib.Path(temporary))
            relative = "jf10/lifecycle/result.json"
            result = self.self_result("jf10", build)
            result["completed"] = False
            result["failures"] = ["hidden failure"]
            result["phases"] = result["phases"][1:]
            metadata = result["metadata"]
            assert isinstance(metadata, dict)
            metadata["candidateSourceTreeSha256"] = "f" * 64
            metadata["candidateMd5"] = "0" * 32
            errors: list[str] = []
            collector.validate_integration_lifecycle(relative, result, build, errors)
            rendered = "\n".join(errors)
            for fragment in ("completed", "failures", "required phases", "SourceTree", "candidateMd5"):
                self.assertIn(fragment, rendered)

            relative = "jf12/third-party-lifecycle/result.json"
            result = self.third_party_result("jf12", build)
            result["versionFlapWarnings"] = ["flap"]
            metadata = result["metadata"]
            assert isinstance(metadata, dict)
            metadata["refreshKitSnapshot"] = "stale-snapshot"
            errors = []
            collector.validate_integration_lifecycle(relative, result, build, errors)
            rendered = "\n".join(errors)
            self.assertIn("versionFlapWarnings", rendered)
            self.assertIn("refreshKitSnapshot", rendered)


class CanonicalEvidenceSemanticTests(unittest.TestCase):
    def test_server_browser_and_cross_generation_results_fail_closed(self) -> None:
        restart = {
            "startElapsedMs": 10,
            "serverHealthyElapsedMs": 20,
            "recoveryCompletedElapsedMs": 30,
            "recoveryIncomplete": False,
        }
        generation = "g-0123456789abcdef"

        def tab(name: str, visibility: str = "visible") -> dict[str, object]:
            return {
                "name": name,
                "authenticated": True,
                "documentId": f"document-{name}",
                "visibility": visibility,
                "kit": {"version": generation, "latestVersion": generation},
            }

        browser = {
            "target": "jf10",
            "failures": [],
            "generationBefore": generation,
            "generationAfter": generation,
            "restartWindow": restart,
            "preRestart": [tab("primary"), tab("secondary"), tab("background", "hidden")],
            "postRestart": [tab("primary"), tab("secondary"), tab("background")],
            "hiddenAtRestart": ["background"],
            "websocketReconnect": {
                "primary": True,
                "secondary": True,
                "background": True,
            },
            "captureCounts": {
                name: {"truncated": False}
                for name in ("primary", "secondary", "background")
            },
            "errorAudit": {
                "restartWindow": restart,
                "restartTransportNoise": {"count": 0, "truncated": False},
                "allowlistedHostErrors": {"count": 0, "truncated": False},
                "unexpectedRefreshKitErrors": {"count": 0, "truncated": False},
                "otherHostOrUnattributedErrors": {"count": 0, "truncated": False},
            },
            "navigation": {
                "configuration": {
                    "initialized": True,
                    "generation": generation,
                    "failedControllerResponses": [],
                    "controllerImportErrors": [],
                }
            },
        }
        evidence_validation.validate_browser("jf10", browser, generation)
        with self.assertRaisesRegex(
            evidence_validation.EvidenceValidationError,
            "browser/server generation differs",
        ):
            evidence_validation.validate_browser(
                "jf10", browser, "g-fedcba9876543210"
            )
        changed = copy.deepcopy(browser)
        changed["errorAudit"]["unexpectedRefreshKitErrors"]["count"] = 1
        with self.assertRaises(evidence_validation.EvidenceValidationError):
            evidence_validation.validate_browser("jf10", changed, generation)

        server = {
            "target": "jf10",
            "serverVersion": "10.11.11",
            "generation": "g-0123456789abcdef",
            "kitVersion": "2.4.5",
            "conditionalStatus": 304,
            "endpointAndShellChecksPassed": True,
            "shellEtag": '"rk-fixture"',
        }
        with tempfile.TemporaryDirectory() as temporary:
            build = IntegrationEvidenceTests.make_build(pathlib.Path(temporary))
            diagnostics = {
                "Generation": server["generation"],
                "KitVersion": server["kitVersion"],
                "PluginVersion": "1.0.1.0",
                "Plugins": [{
                    "Id": evidence_validation.PLUGIN_GUID,
                    "Version": "1.0.1.0",
                    "Status": "Active",
                    "IsLoaded": True,
                }],
            }
            evidence_validation.validate_server("jf10", server, diagnostics, build)
            server["conditionalStatus"] = 200
            with self.assertRaises(evidence_validation.EvidenceValidationError):
                evidence_validation.validate_server("jf10", server, diagnostics, build)

            cross = {
                "experiment": "net9/Jellyfin-10 build manually installed on Jellyfin 12",
                "hostImageDigest": evidence_validation.IMAGE_DIGESTS["jf12"],
                "stageFramework": "net9.0",
                "stageTargetAbi": "10.11.0.0",
                "provisionExitCode": 0,
                "http": {"public": 200, "generation": 200, "kit": 200, "shell": 200},
                "expected": "load",
                "loaded": True,
                "shellInjected": True,
                "pluginInventoryFound": True,
                "pluginInventoryStatus": "Active",
            }
            generation_result = {
                "Version": "1.0.1.0",
                "BuildId": "build-id",
                "CacheKey": generation,
                "Epoch": "process-epoch",
            }
            plugin_record = {
                "Id": evidence_validation.PLUGIN_GUID,
                "Version": "1.0.1.0",
                "Status": "Active",
            }
            public = {"Version": "12.0.0"}
            evidence_validation.validate_cross_compatibility(
                cross, generation_result, plugin_record, public, build
            )
            cross["loaded"] = False
            with self.assertRaises(evidence_validation.EvidenceValidationError):
                evidence_validation.validate_cross_compatibility(
                    cross, generation_result, plugin_record, public, build
                )


class AbiFloorEvidenceTests(unittest.TestCase):
    def test_semantic_validator_self_test_is_network_free_and_fail_closed(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            abi_floor.self_test()
        match = re.search(r"([0-9]+)/\1 PASS", output.getvalue())
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 56)

    def test_collector_redaction_preserves_nonanonymous_strict_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence, build = abi_floor.fixture(pathlib.Path(temporary))
            for path in evidence.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(evidence).as_posix()
                if f"abi-floor/{relative}" in collector.EXACT_ANONYMOUS_HTTP:
                    continue
                if path.suffix == ".json":
                    value = json.loads(path.read_text(encoding="utf-8"))
                    collector.write_json(path, collector.sanitize_json(value))
                else:
                    path.write_text(
                        collector.sanitize_text(
                            path.read_text(encoding="utf-8", errors="replace")
                        ),
                        encoding="utf-8",
                    )
            abi_floor.validate_evidence(evidence, build)

    def test_anonymous_http_collector_is_exact_and_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            source = root / "source.headers"
            target = root / "target.headers"
            raw = b"HTTP/1.1 200 OK\r\nCache-Control: no-cache\r\n\r\n"
            source.write_bytes(raw)
            collector.copy_exact_anonymous_http(
                source,
                target,
                "abi-floor/server/shell.headers",
            )
            self.assertEqual(target.read_bytes(), raw)

            source.write_bytes(b"")
            collector.copy_exact_anonymous_http(
                source,
                target,
                "abi-floor/server/conditional.body",
            )
            self.assertEqual(target.read_bytes(), b"")
            with self.assertRaises(ValueError):
                collector.copy_exact_anonymous_http(
                    source,
                    target,
                    "abi-floor/server/shell.headers",
                )

            source.write_text(
                "HTTP/1.1 200 OK\r\nAuthorization: Bearer exposed\r\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                collector.copy_exact_anonymous_http(
                    source,
                    target,
                    "abi-floor/server/shell.headers",
                )
            source.write_text("password=exposed\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                collector.copy_exact_anonymous_http(
                    source,
                    target,
                    "abi-floor/server/index.html",
                )
            source.write_bytes(b"\xff\xfe")
            with self.assertRaises(ValueError):
                collector.copy_exact_anonymous_http(
                    source,
                    target,
                    "abi-floor/server/shell.headers",
                )

            runtime = (ROOT / "jellyfin-refresh-kit.js").read_bytes()
            source.write_bytes(runtime)
            collector.copy_exact_anonymous_http(
                source,
                target,
                "abi-floor/server/kit.js",
            )
            self.assertEqual(target.read_bytes(), runtime)
            source.write_bytes(runtime + b"\n// unexpected served byte\n")
            with self.assertRaises(ValueError):
                collector.copy_exact_anonymous_http(
                    source,
                    target,
                    "abi-floor/server/kit.js",
                )

            # Retention must publish the inspected bytes. A source that changes
            # after inspection must not be able to substitute unvalidated
            # content through a second read.
            class MutatingSource:
                def __init__(
                    self,
                    path: pathlib.Path,
                    inspected: bytes,
                    mutated: bytes,
                ) -> None:
                    self.path = path
                    self.inspected = inspected
                    self.mutated = mutated

                def read_bytes(self) -> bytes:
                    self.path.write_bytes(self.mutated)
                    return self.inspected

                def __fspath__(self) -> str:
                    return str(self.path)

            mutated = b"HTTP/1.1 200 OK\r\nAuthorization: Bearer exposed\r\n\r\n"
            for label, relative, inspected in (
                ("headers", "abi-floor/server/shell.headers", raw),
                ("kit", "abi-floor/server/kit.js", runtime),
            ):
                with self.subTest(toctou=label):
                    source.write_bytes(inspected)
                    collector.copy_exact_anonymous_http(
                        MutatingSource(source, inspected, mutated),
                        target,
                        relative,
                    )
                    self.assertEqual(source.read_bytes(), mutated)
                    self.assertEqual(target.read_bytes(), inspected)

    def test_collector_release_inventory_and_runner_wiring_are_explicit(self) -> None:
        expected_json = {
            "abi-floor/result.json",
            "abi-floor/relay.json",
            "abi-floor/server/result.json",
            "abi-floor/server/public.json",
            "abi-floor/server/generation.json",
            "abi-floor/server/diagnostics.json",
            "abi-floor/server/plugins.json",
        }
        collector_inventory = set(collector.SAFE_JSON) | set(collector.EXACT_ANONYMOUS_HTTP)
        self.assertTrue(expected_json.issubset(collector_inventory))
        expected_text = {
            "abi-floor/server/generation.headers",
            "abi-floor/server/kit.headers",
            "abi-floor/server/kit.js",
            "abi-floor/server/shell.headers",
            "abi-floor/server/conditional.headers",
            "abi-floor/server/conditional.body",
            "abi-floor/server/index.html",
            "abi-floor/server.log",
        }
        collector_inventory |= set(collector.SAFE_TEXT)
        self.assertTrue(expected_text.issubset(collector_inventory))
        self.assertEqual(
            set(collector.EXACT_ANONYMOUS_HTTP),
            expected_text - {"abi-floor/server.log"} | {
                "abi-floor/server/public.json",
                "abi-floor/server/generation.json",
            },
        )
        self.assertEqual(validation_run.REQUIRED_EXIT_STATUS["abiFloorLab"], 0)
        self.assertTrue(
            {
                *(f"lab/{name}" for name in expected_json),
                *(f"lab/{name}" for name in expected_text),
                "logs/abi-floor.log",
            }.issubset(validation_run.REQUIRED_COLLECTED)
        )
        runner = (ROOT / "test.sh").read_text(encoding="utf-8")
        self.assertIn("bash e2e/jellyfin/run.sh abi-floor", runner)
        self.assertIn("--abi-floor-exit", runner)
        self.assertIn('--log "abi-floor=${abi_floor_log}"', runner)
        compose = (ROOT / "e2e/jellyfin/docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn('profiles: ["abi-floor"]', compose)
        abi_service = compose.split("  abi-floor:\n", 1)[1].split("\n  host-upgrade:\n", 1)[0]
        self.assertNotIn("ports:", abi_service)
        self.assertNotIn("RK_ABI_FLOOR_PORT", abi_service)
        self.assertIn("abi-floor-internal:", compose)
        self.assertIn("internal: true", compose)
        self.assertIn(abi_floor.IMAGE_REFERENCE, compose)


class HostUpgradeEvidenceTests(unittest.TestCase):
    @staticmethod
    def identity_documents(
        build: pathlib.Path,
    ) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
        source, stages = host_evidence.expected_candidate_identity(build)
        results = {
            scenario: {
                "metadata": {
                    "immutableSnapshot": build.name,
                    "sourceIdentity": copy.deepcopy(source),
                    "stages": copy.deepcopy(stages),
                }
            }
            for scenario in host_evidence.SCENARIOS
        }
        aggregate = {
            "immutableSnapshot": build.name,
            "sourceIdentity": copy.deepcopy(source),
        }
        return results, aggregate

    def test_full_semantic_validator_self_test_is_network_free_and_fail_closed(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            host_semantics.self_test()
        with self.assertRaises(host_semantics.EvidenceError):
            host_semantics.validate_result(
                {
                    "schemaVersion": 1,
                    "scenario": "jf10",
                    "completed": True,
                    "failures": [],
                    "phases": [{"name": "one-live-tab-converged"}],
                },
                "jf10",
                "snapshot",
            )

    def test_candidate_binding_rejects_stale_wrong_source_and_wrong_package(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            build = IntegrationEvidenceTests.make_build(pathlib.Path(temporary))
            results, aggregate = self.identity_documents(build)
            host_evidence.validate_candidate_identity(results, aggregate, build)

            mutations = []
            changed_results, changed_aggregate = copy.deepcopy((results, aggregate))
            changed_results["jf10"]["metadata"]["immutableSnapshot"] = "stale-snapshot"
            mutations.append((changed_results, changed_aggregate, "snapshot"))

            changed_results, changed_aggregate = copy.deepcopy((results, aggregate))
            changed_results["jf12"]["metadata"]["sourceIdentity"]["revision"] = "c" * 40
            mutations.append((changed_results, changed_aggregate, "source"))

            changed_results, changed_aggregate = copy.deepcopy((results, aggregate))
            changed_results["jf10"]["metadata"]["sourceIdentity"]["dirty"] = 0
            mutations.append((changed_results, changed_aggregate, "source type"))

            changed_results, changed_aggregate = copy.deepcopy((results, aggregate))
            changed_results["jf10"]["metadata"]["stages"]["net9"]["package"][
                "sha256"
            ] = "d" * 64
            mutations.append((changed_results, changed_aggregate, "stage/package"))

            changed_results, changed_aggregate = copy.deepcopy((results, aggregate))
            del changed_results["jf12"]
            mutations.append((changed_results, changed_aggregate, "inventory"))

            for changed_results, changed_aggregate, label in mutations:
                with self.subTest(label=label), self.assertRaises(
                    host_evidence.HostUpgradeEvidenceError
                ):
                    host_evidence.validate_candidate_identity(
                        changed_results, changed_aggregate, build
                    )

    def test_missing_authoritative_result_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            build = IntegrationEvidenceTests.make_build(root)
            artifacts = root / "artifacts" / "host-upgrade"
            artifacts.mkdir(parents=True)
            with self.assertRaises(host_evidence.HostUpgradeEvidenceError):
                host_evidence.validate_evidence(artifacts, build)

    def test_collector_release_inventory_and_runner_wiring_are_explicit(self) -> None:
        expected = {
            "host-upgrade/result.json",
            "host-upgrade/jf10/result.json",
            "host-upgrade/jf12/result.json",
        }
        self.assertTrue(expected.issubset(set(collector.SAFE_JSON)))
        self.assertEqual(validation_run.REQUIRED_EXIT_STATUS["hostUpgradeLab"], 0)
        self.assertTrue(
            {
                "lab/host-upgrade/result.json",
                "lab/host-upgrade/jf10/result.json",
                "lab/host-upgrade/jf12/result.json",
                "logs/host-upgrade.log",
            }.issubset(validation_run.REQUIRED_COLLECTED)
        )
        runner = (ROOT / "test.sh").read_text(encoding="utf-8")
        self.assertIn("bash e2e/jellyfin/run.sh host-upgrade all", runner)
        self.assertIn('RK_BUILD_SNAPSHOT="${build_snapshot}"', runner)
        self.assertIn("--host-upgrade-exit", runner)
        self.assertIn('--log "host-upgrade=${host_upgrade_log}"', runner)
        validation = (ROOT / ".github/workflows/release-validation.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("integration_gate:", validation)
        self.assertIn("RK_RESULTS_DIR: test-results/integration", validation)
        self.assertIn("scripts/merge-release-evidence.py", validation)
        self.assertIn("run: ./test.sh integration", validation)


class CompatibilityEvidenceTests(unittest.TestCase):
    @staticmethod
    def make_locked_artifact(artifact_id: str, runtime: str) -> dict[str, object]:
        digest = hashlib.sha256(f"archive:{artifact_id}".encode()).hexdigest()
        framework = "net9.0" if runtime == "jf10" else "net10.0"
        abi = "10.11.0.0" if runtime == "jf10" else "12.0.0.0"
        return {
            "id": artifact_id,
            "disposition": "testable",
            "runtime": runtime,
            "archive": {
                "name": f"{artifact_id}.zip",
                "url": (
                    "https://github.com/example/fixture/releases/download/v1/"
                    f"{artifact_id}.zip"
                ),
                "sha256": digest,
            },
            "plugin": {
                "archiveMeta": "absent-generate",
                "assembly": f"{artifact_id}.dll",
                "framework": framework,
                "guid": "11111111-2222-3333-4444-555555555555",
                "name": artifact_id,
                "targetAbi": abi,
                "version": "1.0.0.0",
            },
        }

    @staticmethod
    def make_artifact_report(
        artifacts: list[dict[str, object]],
    ) -> dict[str, object]:
        rows = []
        for artifact in artifacts:
            archive = artifact["archive"]
            plugin = artifact["plugin"]
            assembly = str(plugin["assembly"])
            assembly_sha = hashlib.sha256(
                f"assembly:{artifact['id']}".encode()
            ).hexdigest()
            if plugin["archiveMeta"] == "upstream":
                meta = {
                    "policy": "upstream",
                    "archivePath": "meta.json",
                    "present": True,
                    "checks": {
                        field: {
                            "expected": plugin[field],
                            "actual": plugin[field],
                            "matched": True,
                        }
                        for field in ("guid", "name", "version")
                    }
                    | {
                        "targetAbi": {
                            "expected": plugin["targetAbi"],
                            "actual": plugin["targetAbi"],
                            "matched": True,
                            "present": True,
                        },
                        "assemblies": [],
                    },
                }
            else:
                meta = {
                    "policy": plugin["archiveMeta"],
                    "archivePath": None,
                    "present": False,
                    "checks": None,
                }
            rows.append(
                {
                    "id": artifact["id"],
                    "disposition": artifact["disposition"],
                    "runtime": artifact["runtime"],
                    "archive": {
                        "path": f"/fixture-cache/{archive['sha256']}-{archive['name']}",
                    "url": archive["url"],
                    "sha256": archive["sha256"],
                    "size": 100,
                    "memberCount": 2 if plugin["archiveMeta"] == "upstream" else 1,
                    },
                    "plugin": {
                        **plugin,
                        "assemblyPath": assembly,
                        "assemblySha256": assembly_sha,
                        "managedDlls": {assembly: assembly_sha},
                        "binaryTokenChecks": {"guid": True, "version": True},
                        "meta": meta,
                        "frameworkEvidence": {
                            "expected": plugin["framework"],
                            "depsPath": None,
                            "runtimeTarget": None,
                            "matched": None,
                        },
                    },
                    "verified": True,
                }
            )
        return {"schemaVersion": 1, "artifacts": rows, "allPassed": True}

    @staticmethod
    def write_json_fixture(path: pathlib.Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    @staticmethod
    def result_plugin(row: dict[str, object]) -> dict[str, object]:
        plugin = row["plugin"]
        return {
            "artifactId": row["id"],
            "artifactVerification": copy.deepcopy(row),
            "materialization": {
                "materialized": True,
                "archiveSha256": row["archive"]["sha256"],
                "assemblySha256": plugin["assemblySha256"],
                "dllInventory": copy.deepcopy(plugin["managedDlls"]),
            },
        }

    @staticmethod
    def make_cache_http_fixture(
        expectation: str,
    ) -> tuple[dict[str, bytes], dict[str, object], str]:
        generation = "g-0123456789abcdef"
        if expectation == "safe-degrade":
            body = (
                "<html><body>"
                '<script plugin="Jellyfin Refresh Kit" '
                'data-name="RefreshKitPlugin" '
                f'data-boot-version="{generation}" '
                f'src="/RefreshKit/kit.js?v={generation}"></script>'
                "</body></html>"
            ).encode("utf-8")
            primary_headers = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Cache-Control: no-store\r\n\r\n"
            ).encode("ascii")
            conditional_headers = primary_headers
            conditional_body = body
            conditional_status = b"200\n"
        else:
            body = b"<html><body>required cache fixture</body></html>"
            etag = f'"rk-{hashlib.sha256(body).hexdigest()}"'
            primary_headers = (
                "HTTP/1.1 200 OK\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"ETag: {etag}\r\n\r\n"
            ).encode("ascii")
            conditional_headers = (
                "HTTP/1.1 304 Not Modified\r\n"
                f"ETag: {etag}\r\n\r\n"
            ).encode("ascii")
            conditional_body = b""
            conditional_status = b"304\n"
        captures = {
            "shell-after.html": body,
            "shell-after.headers": primary_headers,
            "conditional.html": conditional_body,
            "conditional.headers": conditional_headers,
            "conditional-status.txt": conditional_status,
        }
        cache = evidence_validation.reconstruct_compatibility_cache_evidence(
            captures,
            {"refreshKit": {"generationAfter": generation}},
            expectation,
            "fixture",
        )
        check_field = (
            "safeDegradeChecks"
            if expectation == "safe-degrade"
            else "requiredChecks"
        )
        if not all(cache[check_field].values()):
            raise AssertionError(cache)
        return captures, cache, generation

    @staticmethod
    def make_direct_webroot_fixture(root: pathlib.Path) -> dict[str, object]:
        artifact_root = root / "artifacts"
        output = root / "output"
        state = root / "state"
        matrix_path = root / "matrices.json"
        lock_path = root / "ecosystem.lock.json"
        build = root / "build"
        stage = build / "stage"
        stage.mkdir(parents=True)
        (stage / "Jellyfin.Plugin.RefreshKit.dll").write_bytes(b"jf10-stage")
        (stage / "meta.json").write_text(
            json.dumps({"runtimeFixture": "jf10"}), encoding="utf-8"
        )

        disk_id = "direct-disk"
        nondisk_id = "ordinary-transform"
        order_pair = "direct-order"
        marker = "<!-- direct-raw-marker -->"
        asset_marker = "/DirectWriter/inject.js"
        requirements = {
            "direct-plugin": {
                "mode": "added",
                "cardinality": 1,
                "markers": [marker, asset_marker],
            }
        }
        locked_artifacts = [
            CompatibilityEvidenceTests.make_locked_artifact(
                "direct-plugin", "jf10"
            ),
            CompatibilityEvidenceTests.make_locked_artifact(
                "sidecar-plugin", "jf10"
            ),
        ]
        CompatibilityEvidenceTests.write_json_fixture(
            lock_path,
            {"schemaVersion": 1, "artifacts": locked_artifacts},
        )
        install_order = ["@refresh-kit", "direct-plugin", "sidecar-plugin"]
        runtime_order = ["direct-plugin", "sidecar-plugin", "@refresh-kit"]
        rows = [
            {
                "id": disk_id,
                "runtime": "jf10",
                "service": "jf10-writable",
                "cacheExpectation": "required",
                "orderPair": order_pair,
                "expectedRuntimePluginOrder": runtime_order,
                "installOrder": install_order,
                "requiredUnversionedOuterArtifacts": [],
                "webrootDiskRequirements": requirements,
            },
            {
                "id": nondisk_id,
                "runtime": "jf10",
                "service": "jf10",
                "cacheExpectation": "required",
                "orderPair": order_pair,
                "expectedRuntimePluginOrder": runtime_order,
                "installOrder": install_order,
                "requiredUnversionedOuterArtifacts": [],
                "webrootDiskRequirements": {},
            },
        ]
        matrix_path.write_text(json.dumps({"matrices": rows}), encoding="utf-8")
        summary = {
            "schemaVersion": 1,
            "outcome": "pass",
            "expectedMatrices": [disk_id, nondisk_id],
            "completedMatrices": [disk_id, nondisk_id],
            "missingMatrices": [],
            "failedMatrices": [],
            "expectedSafeDegradedMatrices": [],
            "safeDegradedMatrices": [],
            "missingSafeDegradedMatrices": [],
            "expectedPassWithLimitationMatrices": [],
            "passWithLimitationMatrices": [],
            "missingPassWithLimitationMatrices": [],
            "unexpectedPassWithLimitationMatrices": [],
            "pairRuntimeOrderChecks": {order_pair: True},
        }
        artifact_root.mkdir()
        all_locked = CompatibilityEvidenceTests.make_artifact_report(
            locked_artifacts
        )
        CompatibilityEvidenceTests.write_json_fixture(
            artifact_root / "all-locked-verification.json", all_locked
        )
        summary["allLockedVerification"] = (
            evidence_validation.artifact_verification_summary(
                artifact_root / "all-locked-verification.json", all_locked
            )
        )
        (artifact_root / "summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )

        before = (
            b"\xef\xbb\xbf<!doctype html>\r\n<HTML>\n<body>before</body>\r\n</HTML>\n"
        )
        after = (
            b"\xef\xbb\xbf<!doctype html>\r\n<HTML>\n<body>after\r\n"
            + marker.encode("utf-8")
            + b"\n<script src=\""
            + asset_marker.encode("utf-8")
            + b"\"></script>\r\n</body>\n</HTML>\r\n"
        )
        webroot_disk = collector.reconstruct_webroot_disk(
            before, after, requirements
        )
        http_captures, cache_evidence, generation = (
            CompatibilityEvidenceTests.make_cache_http_fixture("required")
        )
        for matrix_id, service in (
            (disk_id, "jf10-writable"),
            (nondisk_id, "jf10"),
        ):
            directory = artifact_root / matrix_id
            directory.mkdir()
            matrix_report = copy.deepcopy(all_locked)
            report_rows = matrix_report["artifacts"]
            values = {
                "static.json": {"schemaVersion": 1, "allPassed": True},
                "stage.json": {
                    "schemaVersion": 1,
                    "valid": True,
                    "runtime": "jf10",
                    "stage": str(stage),
                    "meta": json.loads(
                        (stage / "meta.json").read_text(encoding="utf-8")
                    ),
                    "dllSha256": collector.file_hash(
                        stage / "Jellyfin.Plugin.RefreshKit.dll"
                    ),
                },
                "network.json": {
                    "schemaVersion": 1,
                    "valid": True,
                    "allPassed": True,
                    "service": service,
                    "configuredImage": evidence_validation.IMAGE_REFERENCES["jf10"],
                    "expectedImageDigest": evidence_validation.IMAGE_DIGESTS["jf10"],
                    "internalBridge": True,
                    "noGateway": True,
                    "originMode": "verified-internal-bridge",
                    "publishedLoopbackActive": False,
                    "internalIpv4": "10.77.0.2",
                    "selectedOrigin": "http://10.77.0.2:8096",
                },
                "artifact-verification.json": {
                    **matrix_report,
                },
                "result.json": {
                    "schemaVersion": 1,
                    "matrix": matrix_id,
                    "runtime": "jf10",
                    "service": service,
                    "serverVersion": evidence_validation.SERVER_VERSIONS["jf10"],
                    "image": evidence_validation.IMAGE_REFERENCES["jf10"],
                    "errors": [],
                    "outcome": "pass",
                    "cacheExpectation": "required",
                    "requestedInstallOrder": install_order,
                    "orderPair": order_pair,
                    "expectedRuntimePluginOrder": runtime_order,
                    "observedRuntimePluginOrder": runtime_order,
                    "limitations": [],
                    "contentProbes": {},
                    "webrootDisk": webroot_disk if matrix_id == disk_id else None,
                    "refreshKit": {
                        "generationAfter": generation,
                        "cacheEvidence": cache_evidence,
                    },
                    "plugins": [
                        CompatibilityEvidenceTests.result_plugin(report_row)
                        for report_row in report_rows
                    ],
                },
            }
            for name, value in values.items():
                (directory / name).write_text(json.dumps(value), encoding="utf-8")
            for name, raw in http_captures.items():
                (directory / name).write_bytes(raw)
        (artifact_root / disk_id / "webroot-before.html").write_bytes(before)
        (artifact_root / disk_id / "webroot-after.html").write_bytes(after)
        summary["results"] = [
            json.loads((artifact_root / matrix_id / "result.json").read_text())
            for matrix_id in (disk_id, nondisk_id)
        ]
        (artifact_root / "summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
        return {
            "artifact_root": artifact_root,
            "output": output,
            "state": state,
            "matrix_path": matrix_path,
            "lock_path": lock_path,
            "build": build,
            "disk_id": disk_id,
            "nondisk_id": nondisk_id,
            "requirements": requirements,
            "order_pair": order_pair,
            "runtime_order": runtime_order,
            "marker": marker,
            "before": before,
            "after": after,
        }

    @staticmethod
    def add_content_probe_fixture(
        fixture: dict[str, object],
    ) -> dict[str, object]:
        artifact_root = pathlib.Path(fixture["artifact_root"])
        matrix_path = pathlib.Path(fixture["matrix_path"])
        matrix_id = str(fixture["disk_id"])
        origin = "http://10.77.0.2:8096"
        token = b"0123456789abcdef0123456789abcdef"
        marker = "EXACT-SEMANTIC-PROBE-MARKER"
        body = f"fixture prefix {marker} fixture suffix".encode("utf-8")
        probe = {
            "id": "fixture-content-javascript",
            "path": "/Fixture/content.js",
            "authenticated": True,
            "request": {"accept": "text/javascript"},
            "response": {
                "status": 200,
                "mediaType": "text/javascript",
                "body": {
                    "mode": "semantic",
                    "minBytes": 32,
                    "maxBytes": 1024,
                },
            },
            "format": "text",
            "markers": {
                "direct-plugin": [
                    {"value": marker, "cardinality": 1}
                ]
            },
        }
        secondary_marker = "SECOND-EXACT-SEMANTIC-PROBE-MARKER"
        secondary_body = (
            f"fixture secondary prefix {secondary_marker} fixture suffix"
        ).encode("utf-8")
        secondary_probe = {
            "id": "aaa-fixture-content-javascript",
            "path": "/Fixture/alternate.js",
            "authenticated": False,
            "request": {"accept": "text/javascript"},
            "response": {
                "status": 200,
                "mediaType": "text/javascript",
                "body": {
                    "mode": "semantic",
                    "minBytes": 32,
                    "maxBytes": 1024,
                },
            },
            "format": "text",
            "markers": {
                "direct-plugin": [
                    {"value": secondary_marker, "cardinality": 1}
                ]
            },
        }
        probes = [probe, secondary_probe]
        manifest = json.loads(matrix_path.read_text(encoding="utf-8"))
        matrix = next(row for row in manifest["matrices"] if row["id"] == matrix_id)
        # Deliberately keep declaration order nonlexical. Production result
        # JSON is canonicalized with sort_keys=True, so this fixture proves
        # that validators bind the exact canonical inventory rather than
        # treating object-member order as execution order.
        matrix["contentProbes"] = probes
        matrix_path.write_text(json.dumps(manifest), encoding="utf-8")

        directory = artifact_root / matrix_id
        def captures_for(
            current_probe: dict[str, object], current_body: bytes
        ) -> dict[str, bytes]:
            return {
                ".txt": current_body,
                ".headers": (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/javascript; charset=utf-8\r\n"
                    f"Content-Length: {len(current_body)}\r\n\r\n"
                ).encode("ascii"),
                ".status": (
                    f"200\t{origin}{current_probe['path']}\n".encode("ascii")
                ),
            }

        captures = captures_for(probe, body)
        secondary_captures = captures_for(secondary_probe, secondary_body)
        probe_directory = directory / "content-probes"
        probe_directory.mkdir()
        for current_probe, current_captures in (
            (probe, captures),
            (secondary_probe, secondary_captures),
        ):
            for suffix, raw in current_captures.items():
                (
                    probe_directory / f"{current_probe['id']}{suffix}"
                ).write_bytes(raw)

        network_path = directory / "network.json"
        network = json.loads(network_path.read_text(encoding="utf-8"))
        network.update(
            {
                "internalIpv4": "10.77.0.2",
                "selectedOrigin": origin,
            }
        )
        network_path.write_text(json.dumps(network), encoding="utf-8")

        result_path = directory / "result.json"
        result = json.loads(result_path.read_text(encoding="utf-8"))
        projection = evidence_validation.reconstruct_compatibility_content_probe(
            captures, probe, matrix_id, origin
        )
        if projection["allPassed"] is not True:
            raise AssertionError(projection)
        secondary_projection = (
            evidence_validation.reconstruct_compatibility_content_probe(
                secondary_captures, secondary_probe, matrix_id, origin
            )
        )
        if secondary_projection["allPassed"] is not True:
            raise AssertionError(secondary_projection)
        result["contentProbes"] = {
            probe["id"]: projection,
            secondary_probe["id"]: secondary_projection,
        }
        result_path.write_text(
            json.dumps(result, sort_keys=True), encoding="utf-8"
        )

        summary_path = artifact_root / "summary.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary["results"] = [
            json.loads(
                (artifact_root / row["id"] / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            for row in manifest["matrices"]
        ]
        summary_path.write_text(json.dumps(summary), encoding="utf-8")

        state = pathlib.Path(fixture["state"]) / matrix_id
        state.mkdir(parents=True)
        (state / "token").write_bytes(token)
        fixture.update(
            {
                "probe": probe,
                "probes": probes,
                "probe_body": body,
                "probe_captures": captures,
                "probe_origin": origin,
                "probe_token": token,
            }
        )
        return fixture

    def collect_direct_webroot_fixture(
        self, fixture: dict[str, object]
    ) -> tuple[dict[str, list[str]], list[str]]:
        evidence: dict[str, list[str]] = {"missing": [], "collected": []}
        errors: list[str] = []
        with (
            mock.patch.object(
                collector, "COMPAT_ARTIFACTS", fixture["artifact_root"]
            ),
            mock.patch.object(collector, "COMPAT_MATRICES", fixture["matrix_path"]),
            mock.patch.object(collector, "COMPAT_LOCK", fixture["lock_path"]),
            mock.patch.object(collector, "COMPAT_STATE", fixture["state"]),
        ):
            collector.collect_compatibility(
                fixture["output"],
                evidence,
                errors,
                True,
                fixture["build"],
                0,
            )
        return evidence, errors

    def test_archive_verification_receipt_mutations_fail_closed(self) -> None:
        artifacts = [
            self.make_locked_artifact("first-plugin", "jf10"),
            self.make_locked_artifact("second-plugin", "jf10"),
        ]
        artifacts[1]["plugin"]["archiveMeta"] = "upstream"
        report = self.make_artifact_report(artifacts)
        second_plugin = report["artifacts"][1]["plugin"]
        second_plugin["frameworkEvidence"] = {
            "expected": "net9.0",
            "depsPath": "second-plugin.deps.json",
            "runtimeTarget": ".NETCoreApp,Version=v9.0",
            "matched": True,
        }
        report["artifacts"][1]["archive"]["memberCount"] = 3
        expected_ids = ["first-plugin", "second-plugin"]
        evidence_validation.validate_artifact_verification_report(
            report, artifacts, expected_ids, "fixture"
        )

        scenarios = (
            "schema",
            "all-passed-false",
            "all-passed-int",
            "missing-row",
            "extra-row",
            "reordered",
            "duplicate-row",
            "id",
            "disposition",
            "runtime",
            "verified",
            "archive-url",
            "archive-query",
            "archive-final-url",
            "archive-sha",
            "archive-cache-name",
            "archive-size-bool",
            "archive-size-zero",
            "archive-size-over-limit",
            "archive-members-bool",
            "archive-members-zero",
            "archive-members-over-limit",
            "archive-members-too-small",
            "plugin-identity",
            "assembly-path",
            "assembly-path-noncanonical",
            "assembly-path-nul",
            "assembly-sha",
            "managed-main-missing",
            "managed-nondll",
            "managed-sha",
            "token-false",
            "token-int",
            "token-missing",
            "absent-meta-contradiction",
            "upstream-meta-mismatch",
            "framework-absent-contradiction",
            "framework-runtime-mismatch",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                changed = copy.deepcopy(report)
                rows = changed["artifacts"]
                row = rows[0]
                if scenario == "schema":
                    changed["schemaVersion"] = 2
                elif scenario == "all-passed-false":
                    changed["allPassed"] = False
                elif scenario == "all-passed-int":
                    changed["allPassed"] = 1
                elif scenario == "missing-row":
                    rows.pop()
                elif scenario == "extra-row":
                    rows.append(copy.deepcopy(rows[-1]))
                elif scenario == "reordered":
                    rows.reverse()
                elif scenario == "duplicate-row":
                    rows[1] = copy.deepcopy(rows[0])
                elif scenario == "id":
                    row["id"] = "changed-plugin"
                elif scenario == "disposition":
                    row["disposition"] = "quarantined"
                elif scenario == "runtime":
                    row["runtime"] = "jf12"
                elif scenario == "verified":
                    row["verified"] = False
                elif scenario == "archive-url":
                    row["archive"]["url"] = row["archive"]["url"].replace(
                        "example", "changed"
                    )
                elif scenario == "archive-query":
                    row["archive"]["url"] += "?token=forbidden"
                elif scenario == "archive-final-url":
                    row["archive"]["finalUrl"] = "https://example.invalid/private"
                elif scenario == "archive-sha":
                    row["archive"]["sha256"] = "0" * 64
                elif scenario == "archive-cache-name":
                    row["archive"]["path"] = "/fixture-cache/wrong.zip"
                elif scenario == "archive-size-bool":
                    row["archive"]["size"] = True
                elif scenario == "archive-size-zero":
                    row["archive"]["size"] = 0
                elif scenario == "archive-size-over-limit":
                    row["archive"]["size"] = 512 * 1024 * 1024 + 1
                elif scenario == "archive-members-bool":
                    row["archive"]["memberCount"] = True
                elif scenario == "archive-members-zero":
                    row["archive"]["memberCount"] = 0
                elif scenario == "archive-members-over-limit":
                    row["archive"]["memberCount"] = 20_001
                elif scenario == "archive-members-too-small":
                    rows[1]["archive"]["memberCount"] = 2
                elif scenario == "plugin-identity":
                    row["plugin"]["name"] = "changed"
                elif scenario == "assembly-path":
                    row["plugin"]["assemblyPath"] = "../unsafe.dll"
                elif scenario == "assembly-path-noncanonical":
                    assembly = row["plugin"]["assemblyPath"]
                    row["plugin"]["assemblyPath"] = f"./{assembly}"
                elif scenario == "assembly-path-nul":
                    assembly = row["plugin"]["assemblyPath"]
                    row["plugin"]["assemblyPath"] = f"unsafe\x00/{assembly}"
                elif scenario == "assembly-sha":
                    row["plugin"]["assemblySha256"] = "invalid"
                elif scenario == "managed-main-missing":
                    row["plugin"]["managedDlls"] = {
                        "sidecar.dll": "0" * 64
                    }
                elif scenario == "managed-nondll":
                    row["plugin"]["managedDlls"]["sidecar.txt"] = "0" * 64
                elif scenario == "managed-sha":
                    assembly = row["plugin"]["assemblyPath"]
                    row["plugin"]["managedDlls"][assembly] = "invalid"
                elif scenario == "token-false":
                    row["plugin"]["binaryTokenChecks"]["guid"] = False
                elif scenario == "token-int":
                    row["plugin"]["binaryTokenChecks"]["guid"] = 1
                elif scenario == "token-missing":
                    row["plugin"]["binaryTokenChecks"].pop("version")
                elif scenario == "absent-meta-contradiction":
                    row["plugin"]["meta"]["present"] = True
                elif scenario == "upstream-meta-mismatch":
                    rows[1]["plugin"]["meta"]["checks"]["name"]["actual"] = (
                        "changed"
                    )
                elif scenario == "framework-absent-contradiction":
                    row["plugin"]["frameworkEvidence"]["matched"] = True
                else:
                    rows[1]["plugin"]["frameworkEvidence"]["runtimeTarget"] = (
                        ".NETCoreApp,Version=v8.0"
                    )
                with self.assertRaises(evidence_validation.EvidenceValidationError):
                    evidence_validation.validate_artifact_verification_report(
                        changed, artifacts, expected_ids, f"fixture/{scenario}"
                    )

    def test_real_nonruntime_receipt_rows_are_mandatory_and_ordered(self) -> None:
        artifacts = evidence_validation.load_compatibility_artifact_lock(
            ROOT / "e2e" / "compat" / "ecosystem.lock.json"
        )
        report = self.make_artifact_report(artifacts)
        expected_ids = [row["id"] for row in artifacts]
        evidence_validation.validate_artifact_verification_report(
            report, artifacts, expected_ids, "real-lock-fixture"
        )
        self.assertEqual(
            {
                disposition: sum(
                    row["disposition"] == disposition
                    for row in report["artifacts"]
                )
                for disposition in ("testable", "quarantined", "unsupported")
            },
            {"testable": 40, "quarantined": 3, "unsupported": 1},
        )
        nonruntime_indexes = [
            index
            for index, row in enumerate(report["artifacts"])
            if row["disposition"] != "testable"
        ]
        self.assertEqual(len(nonruntime_indexes), 4)

        dropped = copy.deepcopy(report)
        dropped["artifacts"].pop(nonruntime_indexes[0])
        with self.assertRaises(evidence_validation.EvidenceValidationError):
            evidence_validation.validate_artifact_verification_report(
                dropped, artifacts, expected_ids, "real-lock-dropped-nonruntime"
            )

        reordered = copy.deepcopy(report)
        left, right = nonruntime_indexes[:2]
        reordered["artifacts"][left], reordered["artifacts"][right] = (
            reordered["artifacts"][right],
            reordered["artifacts"][left],
        )
        with self.assertRaises(evidence_validation.EvidenceValidationError):
            evidence_validation.validate_artifact_verification_report(
                reordered, artifacts, expected_ids, "real-lock-reordered-nonruntime"
            )

    def test_manifest_raw_inventory_and_service_contract_are_exact(self) -> None:
        matrices_path = ROOT / "e2e" / "compat" / "matrices.json"
        (
            ids,
            _runtimes,
            services,
            _cache,
            _limitations,
            disk_requirements,
            order_pairs,
            runtime_orders,
            install_orders,
        ) = evidence_validation._matrix_contract(matrices_path)
        direct_ids = {
            "jf10-direct-writers-readonly",
            "jf10-direct-writers-writable",
        }
        self.assertEqual(set(disk_requirements), direct_ids)
        self.assertEqual(services["jf10-direct-writers-writable"], "jf10-writable")
        self.assertEqual(
            order_pairs["jf10-response-transformers-forward"],
            "jf10-response-transformers",
        )
        self.assertEqual(
            runtime_orders["jf10-response-transformers-forward"],
            runtime_orders["jf10-response-transformers-reverse"],
        )
        self.assertEqual(
            install_orders["jf10-response-transformers-forward"],
            [
                "@refresh-kit",
                "jellyfin-security-jf10",
                "powertoys-jellytag-jf10",
                "powertoys-privacy-mode-jf10",
                "powertoys-remote-trailers-jf10",
                "powertoys-thumbnail-previews-jf10",
            ],
        )
        collector_pairs, collector_orders, pair_members = (
            collector.compatibility_runtime_order_contract()
        )
        self.assertEqual(collector_pairs, order_pairs)
        self.assertEqual(collector_orders, runtime_orders)
        self.assertEqual(
            pair_members["jf10-response-transformers"],
            [
                "jf10-response-transformers-forward",
                "jf10-response-transformers-reverse",
            ],
        )
        self.assertEqual(
            set(collector.compatibility_webroot_disk_requirements()), direct_ids
        )
        self.assertEqual(
            collector.compatibility_services()["jf10-direct-writers-writable"],
            "jf10-writable",
        )
        raw_paths = {
            f"compat/{matrix_id}/{name}"
            for matrix_id in disk_requirements
            for name in evidence_validation.COMPAT_WEBROOT_FILES
        }
        self.assertEqual(
            raw_paths,
            {
                "compat/jf10-direct-writers-readonly/webroot-before.html",
                "compat/jf10-direct-writers-readonly/webroot-after.html",
                "compat/jf10-direct-writers-writable/webroot-before.html",
                "compat/jf10-direct-writers-writable/webroot-after.html",
            },
        )
        http_paths = {
            f"compat/{matrix_id}/{name}"
            for matrix_id in ids
            for name in evidence_validation.COMPAT_RAW_HTTP_FILES
        }
        probe_contracts = evidence_validation.load_compatibility_content_probes(
            matrices_path
        )
        probe_paths = {
            f"compat/{matrix_id}/content-probes/{probe['id']}{suffix}"
            for matrix_id, probes in probe_contracts.items()
            for probe in probes
            for suffix in evidence_validation.COMPAT_CONTENT_PROBE_SUFFIXES
        }
        self.assertEqual(len(probe_paths), 45)
        expected_inventory = {"summary.json", "all-locked-verification.json"} | {
            f"{matrix_id}/{name}"
            for matrix_id in ids
            for name in evidence_validation.COMPAT_MATRIX_FILES
        } | {
            path.removeprefix("compat/") for path in raw_paths
        } | {
            path.removeprefix("compat/") for path in http_paths
        } | {
            path.removeprefix("compat/") for path in probe_paths
        }
        self.assertEqual(len(expected_inventory), 191)
        self.assertEqual(
            {
                path
                for path in validation_run.REQUIRED_COLLECTED
                if path.startswith("compat/")
            },
            {
                "compat/summary.json",
                "compat/all-locked-verification.json",
                *raw_paths,
            },
        )
        self.assertTrue(
            set(collector.COMPAT_WEBROOT_FILES).isdisjoint(collector.COMPAT_MATRIX_FILES)
        )
        self.assertEqual(
            collector.COMPAT_RAW_HTTP_FILES,
            evidence_validation.COMPAT_RAW_HTTP_FILES,
        )
        self.assertEqual(
            collector.COMPAT_CONTENT_PROBE_SUFFIXES,
            evidence_validation.COMPAT_CONTENT_PROBE_SUFFIXES,
        )

    def test_raw_webroot_collection_is_byte_exact_and_semantically_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_direct_webroot_fixture(pathlib.Path(temporary))
            evidence, errors = self.collect_direct_webroot_fixture(fixture)
            self.assertEqual(errors, [])
            self.assertEqual(evidence["missing"], [])
            disk_id = str(fixture["disk_id"])
            output = pathlib.Path(fixture["output"])
            self.assertEqual(
                (output / "compat" / disk_id / "webroot-before.html").read_bytes(),
                fixture["before"],
            )
            self.assertEqual(
                (output / "compat" / disk_id / "webroot-after.html").read_bytes(),
                fixture["after"],
            )
            collected = evidence_validation.validate_compatibility_tree(
                output,
                pathlib.Path(fixture["build"]),
                pathlib.Path(fixture["matrix_path"]),
            )
            self.assertEqual(set(evidence["collected"]), collected)
            result = json.loads(
                (output / "compat" / disk_id / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            marker = str(fixture["marker"])
            counts = result["webrootDisk"]["effects"]["direct-plugin"]["markers"][
                marker
            ]
            self.assertTrue(
                all(type(counts[name]) is int for name in counts), counts
            )
            nondisk_result = json.loads(
                (
                    output
                    / "compat"
                    / str(fixture["nondisk_id"])
                    / "result.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIn("webrootDisk", nondisk_result)
            self.assertIsNone(nondisk_result["webrootDisk"])

    def test_exact_webroot_copy_rejects_empty_invalid_and_credentials(self) -> None:
        cases = {
            "empty": b"",
            "invalid-utf8": b"\xff\xfe<html></html>",
            "authorization": b"<html>Authorization: Bearer exposed</html>",
            "fixture-password": b"<html>Compat669Pw!x</html>",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            for label, raw in cases.items():
                with self.subTest(label=label):
                    source = root / f"{label}.html"
                    target = root / f"{label}.copied.html"
                    source.write_bytes(raw)
                    with self.assertRaises(ValueError):
                        collector.copy_exact_compatibility_webroot(
                            source, target, f"direct/{label}.html"
                        )
                    self.assertFalse(target.exists())

    def test_exact_compatibility_http_copy_is_byte_exact_and_fails_closed(self) -> None:
        cases = {
            "empty-header": ("shell-after.headers", b""),
            "invalid-utf8": ("shell-after.html", b"\xff<html></html>"),
            "authorization": (
                "shell-after.headers",
                b"HTTP/1.1 200 OK\r\nAuthorization: Bearer exposed\r\n",
            ),
            "proxy-authorization": (
                "shell-after.headers",
                b"HTTP/1.1 200 OK\r\nProxy-Authorization: Basic exposed-value\r\n",
            ),
            "set-cookie": (
                "shell-after.headers",
                b"HTTP/1.1 200 OK\r\nSet-Cookie: sid=exposed-value\r\n",
            ),
            "fixture-password": (
                "shell-after.html",
                b"<html>Compat669Pw!x</html>",
            ),
            "literal-token": (
                "shell-after.html",
                b'<script>const value = {"Token":"0123456789abcdef0123456789abcdef"}</script>',
            ),
            "literal-short-api-key": (
                "shell-after.html",
                b'<script>const API_KEY = "x";</script>',
            ),
            "literal-dollar-api-key": (
                "shell-after.html",
                b'<script>const $API_KEY = "x";</script>',
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            allowed_empty = root / "conditional.html"
            allowed_target = root / "conditional.copied.html"
            allowed_empty.write_bytes(b"")
            self.assertEqual(
                collector.copy_exact_compatibility_http(
                    allowed_empty, allowed_target, "matrix/conditional.html"
                ),
                b"",
            )
            self.assertEqual(allowed_target.read_bytes(), b"")
            benign_source = root / "benign-shell-after.html"
            benign_target = root / "benign.copied.html"
            benign = (
                b"<script>var token = ApiClient.accessToken(); "
                b"const h = { Authorization: `MediaBrowser Token=${token}` };</script>"
            )
            benign_values = (
                benign,
                b"<script>const h = { Authorization: authHeader };</script>",
                b'<script>const h = { "X-Emby-Token": ApiClient.accessToken() };</script>',
                b"<script>const h = { X-Emby-Token: ApiClient.accessToken() };</script>",
                b"<script>const h = { Authorization: MediaBrowser Token=ApiClient.accessToken() };</script>",
                b"<script>const url = `/x?api_key=${apiKey}`;</script>",
                b"<script>const h = { Cookie: cookieHeader };</script>",
                (
                    b'<script>const PROGRESS_API_KEY = '
                    b'"__JMS_CUSTOM_SPLASH_PROGRESS__";</script>'
                ),
            )
            for index, benign_value in enumerate(benign_values):
                with self.subTest(benign=index):
                    benign_source.write_bytes(benign_value)
                    self.assertEqual(
                        collector.copy_exact_compatibility_http(
                            benign_source,
                            benign_target,
                            "matrix/shell-after.html",
                        ),
                        benign_value,
                    )
            for label, (name, raw) in cases.items():
                with self.subTest(label=label):
                    source = root / f"{label}-{name}"
                    target = root / f"{label}.copied"
                    source.write_bytes(raw)
                    expected = (
                        "credential-shaped value in exact compatibility HTTP "
                        f"evidence: matrix/{name}"
                        if label
                        in {
                            "authorization",
                            "proxy-authorization",
                            "set-cookie",
                            "fixture-password",
                            "literal-token",
                        }
                        else None
                    )
                    context = (
                        self.assertRaisesRegex(ValueError, re.escape(expected))
                        if expected is not None
                        else self.assertRaises(ValueError)
                    )
                    with context:
                        collector.copy_exact_compatibility_http(
                            source, target, f"matrix/{name}"
                        )
                    self.assertFalse(target.exists())

    def test_raw_http_cache_reconstruction_is_exact_for_both_modes(self) -> None:
        for expectation in ("required", "safe-degrade"):
            with self.subTest(expectation=expectation):
                captures, cache, generation = self.make_cache_http_fixture(expectation)
                reconstructed = evidence_validation.reconstruct_compatibility_cache_evidence(
                    captures,
                    {"refreshKit": {"generationAfter": generation}},
                    expectation,
                    "fixture",
                )
                self.assertEqual(reconstructed, cache)
                check_field = (
                    "safeDegradeChecks"
                    if expectation == "safe-degrade"
                    else "requiredChecks"
                )
                self.assertTrue(all(reconstructed[check_field].values()))

                mutated = dict(captures)
                mutated["conditional.html"] = captures["conditional.html"].replace(
                    b"/RefreshKit/", b"/RefreshKix/"
                )
                if expectation == "required":
                    mutated["conditional.html"] = b"x"
                failed = evidence_validation.reconstruct_compatibility_cache_evidence(
                    mutated,
                    {"refreshKit": {"generationAfter": generation}},
                    expectation,
                    "fixture",
                )
                if expectation == "required":
                    self.assertIs(failed[check_field]["conditionalBodyEmpty"], False)
                else:
                    self.assertIs(
                        failed[check_field]["conditionalAssetMultisetMatchesPrimary"],
                        False,
                    )
                    self.assertIs(
                        failed[check_field]["conditionalGenerationAddressedKit"],
                        False,
                    )

    def test_content_probe_collection_is_byte_exact_and_independently_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.add_content_probe_fixture(
                self.make_direct_webroot_fixture(pathlib.Path(temporary))
            )
            evidence, errors = self.collect_direct_webroot_fixture(fixture)
            self.assertEqual(errors, [])
            self.assertEqual(evidence["missing"], [])
            output = pathlib.Path(fixture["output"])
            matrix_id = str(fixture["disk_id"])
            probe = fixture["probe"]
            for suffix, raw in fixture["probe_captures"].items():
                retained = (
                    output
                    / "compat"
                    / matrix_id
                    / "content-probes"
                    / f"{probe['id']}{suffix}"
                )
                self.assertEqual(retained.read_bytes(), raw)
            collected = evidence_validation.validate_compatibility_tree(
                output,
                pathlib.Path(fixture["build"]),
                pathlib.Path(fixture["matrix_path"]),
            )
            self.assertEqual(set(evidence["collected"]), collected)
            result = json.loads(
                (output / "compat" / matrix_id / "result.json").read_text(
                    encoding="utf-8"
                )
            )
            declared_probe_ids = [
                current_probe["id"] for current_probe in fixture["probes"]
            ]
            self.assertNotEqual(declared_probe_ids, sorted(declared_probe_ids))
            self.assertEqual(
                list(result["contentProbes"]), sorted(declared_probe_ids)
            )
            projection = result["contentProbes"][probe["id"]]
            self.assertTrue(projection["allPassed"])
            self.assertEqual(projection["response"]["bodyBytes"], len(fixture["probe_body"]))
            self.assertEqual(projection["response"]["headerBlockCount"], 1)

    def test_retained_content_probe_schema_bounds_are_reason_pinned(self) -> None:
        base = json.loads(
            (ROOT / "e2e" / "compat" / "matrices.json").read_text(
                encoding="utf-8"
            )
        )
        expected_errors = {
            "probe-count": "content probe count exceeds 64",
            "id-length": "content probe id exceeds 64 characters",
            "path-length": "content probe path exceeds 2048 characters",
            "path-ascii": "content probe path must be ASCII",
            "accept-length": "content probe Accept exceeds 128 characters",
            "json-map": "JSON array map exceeds 64 entries",
            "json-key-length": "JSON array key exceeds 128 characters",
            "json-key-control": "JSON array key contains control characters",
            "json-list": "JSON array requirement list exceeds 64 entries",
            "json-value-length": "JSON array value exceeds 4096 characters",
            "json-value-control": "JSON array value contains control characters",
            "marker-map": "content marker owner map exceeds 64 entries",
            "marker-owner-length": "content marker owner exceeds 128 characters",
            "marker-owner-control": "content marker owner contains control characters",
            "marker-list": "content marker requirement list exceeds 64 entries",
            "marker-value-length": "content marker value exceeds 4096 characters",
            "marker-value-control": "content marker value contains control characters",
            "total-requirements": "content probe total requirements exceeds 64",
        }
        for scenario, expected in expected_errors.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                manifest = copy.deepcopy(base)
                text_matrix = next(
                    row for row in manifest["matrices"]
                    if row["id"] == "jf10-registration-broker"
                )
                text_probe = text_matrix["contentProbes"][0]
                json_probe = next(
                    probe
                    for row in manifest["matrices"]
                    if row["id"] == "jf10-response-transformers-forward"
                    for probe in row["contentProbes"]
                    if probe["id"] == "powertoys-remote-trailers-config"
                )
                if scenario == "probe-count":
                    text_matrix["contentProbes"] = [
                        copy.deepcopy(text_probe) for _ in range(65)
                    ]
                elif scenario == "id-length":
                    text_probe["id"] = "a" * 65
                elif scenario == "path-length":
                    text_probe["path"] = "/" + ("a" * 2048)
                elif scenario == "path-ascii":
                    text_probe["path"] = "/Fixture/é.js"
                elif scenario == "accept-length":
                    value = "application/" + ("a" * 117)
                    text_probe["request"]["accept"] = value
                    text_probe["response"]["mediaType"] = value
                elif scenario == "json-map":
                    json_probe["jsonArrayContains"] = {
                        f"key-{index}": [
                            {"value": f"value-{index}", "cardinality": 1}
                        ]
                        for index in range(65)
                    }
                elif scenario == "json-key-length":
                    contract = json_probe["jsonArrayContains"]
                    contract["k" * 129] = contract.pop("plugins")
                elif scenario == "json-key-control":
                    contract = json_probe["jsonArrayContains"]
                    contract["plugins\n"] = contract.pop("plugins")
                elif scenario == "json-list":
                    json_probe["jsonArrayContains"]["plugins"] = [
                        {"value": f"value-{index}", "cardinality": 1}
                        for index in range(65)
                    ]
                elif scenario == "json-value-length":
                    json_probe["jsonArrayContains"]["plugins"][0]["value"] = (
                        "v" * 4097
                    )
                elif scenario == "json-value-control":
                    json_probe["jsonArrayContains"]["plugins"][0]["value"] = (
                        "value\u007f"
                    )
                elif scenario == "marker-map":
                    text_probe["markers"] = {
                        f"owner-{index}": [
                            {"value": f"value-{index}", "cardinality": 1}
                        ]
                        for index in range(65)
                    }
                elif scenario == "marker-owner-length":
                    markers = text_probe["markers"]
                    owner = next(iter(markers))
                    markers["o" * 129] = markers.pop(owner)
                elif scenario == "marker-owner-control":
                    markers = text_probe["markers"]
                    owner = next(iter(markers))
                    markers[f"{owner}\n"] = markers.pop(owner)
                elif scenario == "marker-list":
                    requirements = next(iter(text_probe["markers"].values()))
                    requirements[:] = [
                        {"value": f"value-{index}", "cardinality": 1}
                        for index in range(65)
                    ]
                elif scenario == "marker-value-length":
                    next(iter(text_probe["markers"].values()))[0]["value"] = (
                        "v" * 4097
                    )
                elif scenario == "marker-value-control":
                    next(iter(text_probe["markers"].values()))[0]["value"] = (
                        "value\n"
                    )
                else:
                    json_probe["jsonArrayContains"]["plugins"] = [
                        {"value": f"json-value-{index}", "cardinality": 1}
                        for index in range(32)
                    ]
                    owner = next(iter(json_probe["markers"]))
                    json_probe["markers"][owner] = [
                        {"value": f"marker-value-{index}", "cardinality": 1}
                        for index in range(33)
                    ]
                path = pathlib.Path(temporary) / "matrices.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaisesRegex(
                    evidence_validation.EvidenceValidationError,
                    re.escape(expected),
                ):
                    evidence_validation.load_compatibility_content_probes(path)

    def test_content_probe_json_reconstruction_is_strict_and_bounded(self) -> None:
        contracts = evidence_validation.load_compatibility_content_probes(
            ROOT / "e2e" / "compat" / "matrices.json"
        )
        probe = next(
            row
            for row in contracts["jf10-response-transformers-forward"]
            if row["id"] == "powertoys-remote-trailers-config"
        )
        origin = "http://10.77.0.2:8096"
        body = json.dumps(
            {
                "plugins": ["powertoys/RemoteTrailers"],
                "padding": "x" * 600,
            },
            separators=(",", ":"),
        ).encode("utf-8")

        def captures(value: bytes) -> dict[str, bytes]:
            return {
                ".txt": value,
                ".headers": (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: application/json; charset=utf-8\r\n"
                    f"Content-Length: {len(value)}\r\n\r\n"
                ).encode("ascii"),
                ".status": f"200\t{origin}{probe['path']}\n".encode("ascii"),
            }

        valid = evidence_validation.reconstruct_compatibility_content_probe(
            captures(body), probe, "fixture", origin
        )
        self.assertTrue(valid["allPassed"], valid)

        quoted_utf8 = captures(body)
        quoted_utf8[".headers"] = quoted_utf8[".headers"].replace(
            b"charset=utf-8", b'charset="UTF-8"'
        )
        quoted_result = evidence_validation.reconstruct_compatibility_content_probe(
            quoted_utf8, probe, "fixture", origin
        )
        self.assertTrue(quoted_result["checks"]["utf8CharsetCoherent"])
        self.assertTrue(quoted_result["allPassed"])

        wrong_charset = captures(body)
        wrong_charset[".headers"] = wrong_charset[".headers"].replace(
            b"charset=utf-8", b"charset=iso-8859-1"
        )
        wrong_charset_result = (
            evidence_validation.reconstruct_compatibility_content_probe(
                wrong_charset, probe, "fixture", origin
            )
        )
        self.assertFalse(
            wrong_charset_result["checks"]["utf8CharsetCoherent"]
        )
        self.assertFalse(wrong_charset_result["allPassed"])

        border_probe = copy.deepcopy(probe)
        border_probe["format"] = "text"
        border_probe.pop("jsonArrayContains")
        border_probe["request"]["accept"] = "text/javascript"
        border_probe["response"]["mediaType"] = "text/javascript"
        border_probe["response"]["body"] = {
            "mode": "semantic",
            "minBytes": 1,
            "maxBytes": 32,
        }
        border_probe["markers"] = {
            "powertoys-remote-trailers-jf10": [
                {"value": "aba", "cardinality": 1}
            ]
        }
        border_body = b"ababa"
        border_captures = {
            ".txt": border_body,
            ".headers": (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/javascript\r\n"
                b"Content-Length: 5\r\n\r\n"
            ),
            ".status": f"200\t{origin}{border_probe['path']}\n".encode("ascii"),
        }
        border_result = evidence_validation.reconstruct_compatibility_content_probe(
            border_captures, border_probe, "fixture", origin
        )
        self.assertEqual(
            border_result["artifactChecks"]["powertoys-remote-trailers-jf10"]
            ["aba"]["count"],
            2,
        )
        self.assertFalse(border_result["allPassed"])

        duplicate_value = json.dumps(
            {
                "plugins": [
                    "powertoys/RemoteTrailers",
                    "powertoys/RemoteTrailers",
                ],
                "padding": "x" * 600,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        duplicate_result = evidence_validation.reconstruct_compatibility_content_probe(
            captures(duplicate_value), probe, "fixture", origin
        )
        self.assertFalse(
            duplicate_result["jsonArrayContainsChecks"]["plugins"]
            ["powertoys/RemoteTrailers"]["passed"]
        )

        invalid_values = {
            "bom": b"\xef\xbb\xbf" + body,
            "literal-nan": (
                b'{"plugins":["powertoys/RemoteTrailers"],"padding":"'
                + (b"x" * 600)
                + b'","number":NaN}'
            ),
            "top-level-scalar": b'"' + (b"x" * 600) + b'"',
            "top-level-array": b'["' + (b"x" * 600) + b'"]',
            "duplicate-key": (
                b'{"plugins":["powertoys/RemoteTrailers"],'
                b'"plugins":["powertoys/RemoteTrailers"],"padding":"'
                + (b"x" * 600)
                + b'"}'
            ),
            "deep": (
                b'{"plugins":["powertoys/RemoteTrailers"],"padding":"'
                + (b"x" * 600)
                + b'","nested":'
                + (b"[" * 1100)
                + b"0"
                + (b"]" * 1100)
                + b"}"
            ),
            "positive-exponent-overflow": (
                b'{"plugins":["powertoys/RemoteTrailers"],"padding":"'
                + (b"x" * 600)
                + b'","number":1e9999}'
            ),
            "negative-exponent-overflow": (
                b'{"plugins":["powertoys/RemoteTrailers"],"padding":"'
                + (b"x" * 600)
                + b'","number":-1e9999}'
            ),
        }
        for label, value in invalid_values.items():
            with self.subTest(label=label):
                result = evidence_validation.reconstruct_compatibility_content_probe(
                    captures(value), probe, "fixture", origin
                )
                self.assertIs(result["checks"]["formatValid"], False)
                self.assertIs(result["allPassed"], False)

        invalid_utf8 = dict(captures(body))
        invalid_utf8[".txt"] = body + b"\xff"
        invalid_utf8[".headers"] = invalid_utf8[".headers"].replace(
            f"Content-Length: {len(body)}".encode("ascii"),
            f"Content-Length: {len(body) + 1}".encode("ascii"),
        )
        with self.assertRaises(evidence_validation.EvidenceValidationError):
            evidence_validation.reconstruct_compatibility_content_probe(
                invalid_utf8, probe, "fixture", origin
            )

    def test_content_probe_collector_rejects_tokens_credentials_and_oversize(self) -> None:
        scenarios = (
            "dynamic-token-body",
            "dynamic-token-header",
            "dynamic-token-status",
            "invalid-token-state",
            "authorization-body",
            "short-basic-body",
            "short-bearer-body",
            "cookie-body",
            "short-cookie-body",
            "emby-token-body",
            "short-emby-token-body",
            "oversize-body",
            "oversize-headers",
            "oversize-status",
            "empty-body",
            "empty-headers",
            "empty-status",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                fixture = self.add_content_probe_fixture(
                    self.make_direct_webroot_fixture(pathlib.Path(temporary))
                )
                artifact_root = pathlib.Path(fixture["artifact_root"])
                matrix_id = str(fixture["disk_id"])
                probe = fixture["probe"]
                probe_root = artifact_root / matrix_id / "content-probes" / probe["id"]
                token = bytes(fixture["probe_token"])
                if scenario == "dynamic-token-body":
                    probe_root.with_suffix(".txt").write_bytes(token)
                elif scenario == "dynamic-token-header":
                    probe_root.with_suffix(".headers").write_bytes(
                        b"HTTP/1.1 200 OK\r\nX-Echo: " + token + b"\r\n\r\n"
                    )
                elif scenario == "dynamic-token-status":
                    probe_root.with_suffix(".status").write_bytes(token + b"\n")
                elif scenario == "invalid-token-state":
                    (
                        pathlib.Path(fixture["state"]) / matrix_id / "token"
                    ).write_bytes(b"short")
                elif scenario == "authorization-body":
                    probe_root.with_suffix(".txt").write_bytes(
                        b'{Authorization:"Bearer aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}'
                    )
                elif scenario == "short-basic-body":
                    probe_root.with_suffix(".txt").write_bytes(
                        b"Authorization: Basic YQ=="
                    )
                elif scenario == "short-bearer-body":
                    probe_root.with_suffix(".txt").write_bytes(
                        b"Authorization: Bearer abc"
                    )
                elif scenario == "cookie-body":
                    probe_root.with_suffix(".txt").write_bytes(
                        b"Cookie: sid=aaaaaaaaaaaaaaaa"
                    )
                elif scenario == "short-cookie-body":
                    probe_root.with_suffix(".txt").write_bytes(b"Cookie: sid=x")
                elif scenario == "emby-token-body":
                    probe_root.with_suffix(".txt").write_bytes(
                        b"X-Emby-Token: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
                    )
                elif scenario == "short-emby-token-body":
                    probe_root.with_suffix(".txt").write_bytes(
                        b"X-Emby-Token: abc"
                    )
                elif scenario == "oversize-body":
                    probe_root.with_suffix(".txt").write_bytes(
                        b"x" * (evidence_validation.COMPAT_CONTENT_PROBE_MAX_BODY_BYTES + 1)
                    )
                elif scenario == "oversize-headers":
                    probe_root.with_suffix(".headers").write_bytes(
                        b"x" * (evidence_validation.COMPAT_CONTENT_PROBE_MAX_HEADER_BYTES + 1)
                    )
                elif scenario == "oversize-status":
                    probe_root.with_suffix(".status").write_bytes(
                        b"x" * (evidence_validation.COMPAT_CONTENT_PROBE_MAX_STATUS_BYTES + 1)
                    )
                elif scenario == "empty-body":
                    probe_root.with_suffix(".txt").write_bytes(b"")
                elif scenario == "empty-headers":
                    probe_root.with_suffix(".headers").write_bytes(b"")
                else:
                    probe_root.with_suffix(".status").write_bytes(b"")
                _evidence, errors = self.collect_direct_webroot_fixture(fixture)
                relative_root = f"{matrix_id}/content-probes/{probe['id']}"
                suffix = {
                    "dynamic-token-body": ".txt",
                    "dynamic-token-header": ".headers",
                    "dynamic-token-status": ".status",
                    "authorization-body": ".txt",
                    "short-basic-body": ".txt",
                    "short-bearer-body": ".txt",
                    "cookie-body": ".txt",
                    "short-cookie-body": ".txt",
                    "emby-token-body": ".txt",
                    "short-emby-token-body": ".txt",
                    "oversize-body": ".txt",
                    "oversize-headers": ".headers",
                    "oversize-status": ".status",
                    "empty-body": ".txt",
                    "empty-headers": ".headers",
                    "empty-status": ".status",
                }.get(scenario)
                if scenario == "invalid-token-state":
                    expected = (
                        f"{matrix_id}: compatibility probe token state is invalid"
                    )
                elif scenario.startswith("dynamic-token"):
                    expected = (
                        "dynamic authentication token in exact compatibility "
                        f"HTTP evidence: {relative_root}{suffix}"
                    )
                elif scenario in {
                    "authorization-body",
                    "short-basic-body",
                    "short-bearer-body",
                    "cookie-body",
                    "short-cookie-body",
                    "emby-token-body",
                    "short-emby-token-body",
                }:
                    expected = (
                        "credential-shaped value in exact compatibility HTTP "
                        f"evidence: {relative_root}{suffix}"
                    )
                else:
                    size = {
                        "oversize-body": (
                            evidence_validation.COMPAT_CONTENT_PROBE_MAX_BODY_BYTES
                            + 1
                        ),
                        "oversize-headers": (
                            evidence_validation.COMPAT_CONTENT_PROBE_MAX_HEADER_BYTES
                            + 1
                        ),
                        "oversize-status": (
                            evidence_validation.COMPAT_CONTENT_PROBE_MAX_STATUS_BYTES
                            + 1
                        ),
                    }.get(scenario, 0)
                    expected = (
                        "invalid exact compatibility content-probe size: "
                        f"{relative_root}{suffix}: {size}"
                    )
                self.assertIn(expected, errors)

    def test_retained_content_probe_mutations_fail_closed_for_intended_reason(self) -> None:
        scenarios = (
            "missing-raw",
            "extra-raw",
            "malformed-mime",
            "duplicate-mime-parameter",
            "wrong-charset",
            "wrong-effective-origin",
            "redirect-302",
            "raw-result-divergence",
            "result-projection-divergence",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                fixture = self.add_content_probe_fixture(
                    self.make_direct_webroot_fixture(pathlib.Path(temporary))
                )
                _evidence, errors = self.collect_direct_webroot_fixture(fixture)
                self.assertEqual(errors, [])
                output = pathlib.Path(fixture["output"])
                matrix_id = str(fixture["disk_id"])
                probe = fixture["probe"]
                directory = output / "compat" / matrix_id
                probe_root = directory / "content-probes" / probe["id"]
                if scenario == "missing-raw":
                    probe_root.with_suffix(".headers").unlink()
                    expected = "compatibility inventory differs"
                elif scenario == "extra-raw":
                    (directory / "content-probes" / "unexpected.txt").write_bytes(b"x")
                    expected = "compatibility inventory differs"
                elif scenario == "malformed-mime":
                    path = probe_root.with_suffix(".headers")
                    path.write_bytes(
                        path.read_bytes().replace(
                            b"text/javascript; charset=utf-8",
                            b"text/javascript;",
                        )
                    )
                    expected = "raw content probe contract failed"
                elif scenario == "duplicate-mime-parameter":
                    path = probe_root.with_suffix(".headers")
                    path.write_bytes(
                        path.read_bytes().replace(
                            b"text/javascript; charset=utf-8",
                            b"text/javascript; charset=utf-8; CHARSET=us-ascii",
                        )
                    )
                    expected = "raw content probe contract failed"
                elif scenario == "wrong-charset":
                    path = probe_root.with_suffix(".headers")
                    path.write_bytes(
                        path.read_bytes().replace(
                            b"text/javascript; charset=utf-8",
                            b"text/javascript; charset=iso-8859-1",
                        )
                    )
                    expected = "raw content probe contract failed"
                elif scenario == "wrong-effective-origin":
                    path = probe_root.with_suffix(".status")
                    path.write_bytes(
                        path.read_bytes().replace(b"10.77.0.2", b"10.77.0.3")
                    )
                    expected = "raw content probe contract failed"
                elif scenario == "redirect-302":
                    headers_path = probe_root.with_suffix(".headers")
                    headers_path.write_bytes(
                        headers_path.read_bytes().replace(
                            b"HTTP/1.1 200 OK", b"HTTP/1.1 302 Found"
                        )
                    )
                    status_path = probe_root.with_suffix(".status")
                    status_path.write_bytes(
                        status_path.read_bytes().replace(b"200\t", b"302\t")
                    )
                    expected = "raw content probe contract failed"
                elif scenario == "raw-result-divergence":
                    path = probe_root.with_suffix(".txt")
                    old_body = path.read_bytes()
                    new_body = old_body + b" "
                    path.write_bytes(new_body)
                    headers_path = probe_root.with_suffix(".headers")
                    headers_path.write_bytes(
                        headers_path.read_bytes().replace(
                            f"Content-Length: {len(old_body)}".encode("ascii"),
                            f"Content-Length: {len(new_body)}".encode("ascii"),
                        )
                    )
                    expected = "result content probe differs from raw captures"
                else:
                    result_path = directory / "result.json"
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    result["contentProbes"][probe["id"]]["response"][
                        "bodySha256"
                    ] = "0" * 64
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                    summary_path = output / "compat" / "summary.json"
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    summary["results"] = [
                        result if row.get("matrix") == matrix_id else row
                        for row in summary["results"]
                    ]
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                    expected = "result content probe differs from raw captures"
                with self.assertRaisesRegex(
                    evidence_validation.EvidenceValidationError, expected
                ):
                    evidence_validation.validate_compatibility_tree(
                        output,
                        pathlib.Path(fixture["build"]),
                        pathlib.Path(fixture["matrix_path"]),
                    )

    def test_collector_rejects_typed_marker_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fixture = self.make_direct_webroot_fixture(pathlib.Path(temporary))
            result_path = (
                pathlib.Path(fixture["artifact_root"])
                / str(fixture["disk_id"])
                / "result.json"
            )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            marker = str(fixture["marker"])
            result["webrootDisk"]["effects"]["direct-plugin"]["markers"][marker][
                "beforeCount"
            ] = False
            result_path.write_text(json.dumps(result), encoding="utf-8")
            _evidence, errors = self.collect_direct_webroot_fixture(fixture)
            self.assertTrue(
                any("webrootDisk differs" in error for error in errors), errors
            )

    def test_runtime_order_manifest_contract_fails_closed(self) -> None:
        scenarios = (
            "missing-order",
            "missing-pair",
            "singleton-pair",
            "different-order",
            "different-cache",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                fixture = self.make_direct_webroot_fixture(pathlib.Path(temporary))
                matrix_path = pathlib.Path(fixture["matrix_path"])
                manifest = json.loads(matrix_path.read_text(encoding="utf-8"))
                rows = manifest["matrices"]
                if scenario == "missing-order":
                    rows[0].pop("expectedRuntimePluginOrder")
                elif scenario == "missing-pair":
                    rows[0].pop("orderPair")
                elif scenario == "singleton-pair":
                    rows[1].pop("orderPair")
                    rows[1].pop("expectedRuntimePluginOrder")
                elif scenario == "different-order":
                    rows[1]["expectedRuntimePluginOrder"] = list(
                        reversed(rows[1]["expectedRuntimePluginOrder"])
                    )
                else:
                    rows[1]["cacheExpectation"] = "observe"
                matrix_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(evidence_validation.EvidenceValidationError):
                    evidence_validation._matrix_contract(matrix_path)
                with (
                    mock.patch.object(collector, "COMPAT_MATRICES", matrix_path),
                    self.assertRaises(ValueError),
                ):
                    collector.compatibility_runtime_order_contract()

    def test_collector_rejects_runtime_order_pair_and_required_cache_mutations(
        self,
    ) -> None:
        scenarios = (
            "result-order-pair",
            "result-expected-runtime-order",
            "result-observed-runtime-order",
            "summary-pair-missing",
            "summary-pair-false",
            "summary-result-stale",
            "required-check-missing",
            "required-check-not-bool",
            "required-raw-body",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                fixture = self.make_direct_webroot_fixture(pathlib.Path(temporary))
                artifact_root = pathlib.Path(fixture["artifact_root"])
                result_path = artifact_root / str(fixture["disk_id"]) / "result.json"
                if scenario.startswith("result-"):
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if scenario == "result-order-pair":
                        result["orderPair"] = "wrong-order"
                    elif scenario == "result-expected-runtime-order":
                        result["expectedRuntimePluginOrder"] = list(
                            reversed(result["expectedRuntimePluginOrder"])
                        )
                    else:
                        result["observedRuntimePluginOrder"] = list(
                            reversed(result["observedRuntimePluginOrder"])
                        )
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                elif scenario.startswith("summary-"):
                    summary_path = artifact_root / "summary.json"
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    if scenario == "summary-pair-missing":
                        summary.pop("pairRuntimeOrderChecks")
                    elif scenario == "summary-result-stale":
                        summary["results"][0]["service"] = "stale-service"
                    else:
                        summary["pairRuntimeOrderChecks"][
                            str(fixture["order_pair"])
                        ] = False
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                elif scenario.startswith("required-check-"):
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    required = result["refreshKit"]["cacheEvidence"][
                        "requiredChecks"
                    ]
                    check_name = next(iter(required))
                    if scenario == "required-check-missing":
                        required.pop(check_name)
                    else:
                        required[check_name] = 1
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                else:
                    raw_path = artifact_root / str(fixture["disk_id"]) / "shell-after.html"
                    raw_path.write_bytes(raw_path.read_bytes() + b" ")

                _evidence, errors = self.collect_direct_webroot_fixture(fixture)
                self.assertTrue(
                    any(
                        phrase in error
                        for phrase in (
                            "runtime plugin order proof",
                            "compatibility summary",
                            "summary results",
                            "required cache proof",
                            "retained HTTP bytes",
                        )
                        for error in errors
                    ),
                    errors,
                )

    def test_collector_rejects_all_locked_projection_and_result_mutations(
        self,
    ) -> None:
        scenarios = (
            "missing-all-locked",
            "all-locked-failed",
            "summary-binding",
            "matrix-projection",
            "requested-install-order",
            "result-plugin-order",
            "embedded-verification",
            "materialization-archive",
            "materialization-assembly",
            "materialization-dlls",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                fixture = self.make_direct_webroot_fixture(pathlib.Path(temporary))
                artifact_root = pathlib.Path(fixture["artifact_root"])
                matrix_id = str(fixture["disk_id"])
                all_path = artifact_root / "all-locked-verification.json"
                summary_path = artifact_root / "summary.json"
                report_path = artifact_root / matrix_id / "artifact-verification.json"
                result_path = artifact_root / matrix_id / "result.json"
                if scenario == "missing-all-locked":
                    all_path.unlink()
                elif scenario == "all-locked-failed":
                    value = json.loads(all_path.read_text(encoding="utf-8"))
                    value["artifacts"][0]["verified"] = False
                    all_path.write_text(json.dumps(value), encoding="utf-8")
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    summary["allLockedVerification"] = (
                        evidence_validation.artifact_verification_summary(
                            all_path, value
                        )
                    )
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                elif scenario == "summary-binding":
                    value = json.loads(summary_path.read_text(encoding="utf-8"))
                    value["allLockedVerification"]["artifactCount"] = 0
                    summary_path.write_text(json.dumps(value), encoding="utf-8")
                elif scenario == "matrix-projection":
                    value = json.loads(report_path.read_text(encoding="utf-8"))
                    value["artifacts"][0]["archive"]["size"] += 1
                    report_path.write_text(json.dumps(value), encoding="utf-8")
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    result["plugins"][0]["artifactVerification"] = copy.deepcopy(
                        value["artifacts"][0]
                    )
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    summary["results"] = [
                        copy.deepcopy(result)
                        if row.get("matrix") == matrix_id
                        else row
                        for row in summary["results"]
                    ]
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                else:
                    value = json.loads(result_path.read_text(encoding="utf-8"))
                    plugin = value["plugins"][0]
                    if scenario == "requested-install-order":
                        value["requestedInstallOrder"].reverse()
                    elif scenario == "result-plugin-order":
                        value["plugins"].reverse()
                    elif scenario == "embedded-verification":
                        plugin["artifactVerification"]["archive"]["size"] += 1
                    elif scenario == "materialization-archive":
                        plugin["materialization"]["archiveSha256"] = "0" * 64
                    elif scenario == "materialization-assembly":
                        plugin["materialization"]["assemblySha256"] = "0" * 64
                    else:
                        plugin["materialization"]["dllInventory"] = {}
                    result_path.write_text(json.dumps(value), encoding="utf-8")
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    summary["results"] = [
                        copy.deepcopy(value)
                        if row.get("matrix") == matrix_id
                        else row
                        for row in summary["results"]
                    ]
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                _evidence, errors = self.collect_direct_webroot_fixture(fixture)
                if scenario == "all-locked-failed":
                    self.assertTrue(
                        any("row identity is invalid" in error for error in errors),
                        errors,
                    )
                elif scenario == "matrix-projection":
                    self.assertTrue(
                        any(
                            "artifact report differs from all-locked projection" in error
                            for error in errors
                        ),
                        errors,
                    )
                elif scenario in {"requested-install-order", "result-plugin-order"}:
                    self.assertTrue(
                        any("result artifact/install order differs" in error for error in errors),
                        errors,
                    )
                elif scenario in {
                    "embedded-verification",
                    "materialization-archive",
                    "materialization-assembly",
                    "materialization-dlls",
                }:
                    self.assertTrue(
                        any("result artifact binding differs" in error for error in errors),
                        errors,
                    )
                else:
                    self.assertTrue(errors, scenario)

    def test_retained_webroot_mutations_fail_closed(self) -> None:
        scenarios = (
            "missing",
            "unexpected",
            "all-locked-missing",
            "all-locked-verified",
            "summary-all-locked-sha",
            "matrix-artifact-missing",
            "matrix-artifact-stale",
            "requested-install-order",
            "plugin-artifact-id",
            "plugin-order",
            "embedded-artifact-verification",
            "materialization-archive",
            "materialization-assembly",
            "materialization-dlls",
            "one-byte",
            "hash",
            "count",
            "count-bool",
            "check-bool",
            "marker-contract",
            "result-service",
            "network-service",
            "nondisk-null",
            "result-order-pair",
            "result-expected-runtime-order",
            "result-observed-runtime-order",
            "summary-pair-missing",
            "summary-pair-false",
            "summary-result-stale",
            "required-check-missing",
            "required-check-not-bool",
            "required-raw-body",
        )
        for scenario in scenarios:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                fixture = self.make_direct_webroot_fixture(pathlib.Path(temporary))
                _evidence, errors = self.collect_direct_webroot_fixture(fixture)
                self.assertEqual(errors, [])
                output = pathlib.Path(fixture["output"])
                disk_id = str(fixture["disk_id"])
                nondisk_id = str(fixture["nondisk_id"])
                disk_directory = output / "compat" / disk_id
                result_path = disk_directory / "result.json"
                network_path = disk_directory / "network.json"
                after_path = disk_directory / "webroot-after.html"
                marker = str(fixture["marker"])

                if scenario == "missing":
                    (disk_directory / "webroot-before.html").unlink()
                elif scenario == "unexpected":
                    unexpected = output / "compat" / nondisk_id / "webroot-before.html"
                    unexpected.write_bytes(b"<html></html>")
                elif scenario == "all-locked-missing":
                    (output / "compat" / "all-locked-verification.json").unlink()
                elif scenario == "all-locked-verified":
                    report_path = output / "compat" / "all-locked-verification.json"
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    report["artifacts"][0]["verified"] = False
                    report_path.write_text(json.dumps(report), encoding="utf-8")
                    summary_path = output / "compat" / "summary.json"
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    summary["allLockedVerification"] = (
                        evidence_validation.artifact_verification_summary(
                            report_path, report
                        )
                    )
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                elif scenario == "summary-all-locked-sha":
                    summary_path = output / "compat" / "summary.json"
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    summary["allLockedVerification"]["reportSha256"] = "0" * 64
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                elif scenario.startswith("matrix-artifact-"):
                    report_path = disk_directory / "artifact-verification.json"
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    if scenario == "matrix-artifact-missing":
                        report["artifacts"] = []
                    else:
                        report["artifacts"][0]["archive"]["size"] += 1
                    report_path.write_text(json.dumps(report), encoding="utf-8")
                    if scenario == "matrix-artifact-stale":
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                        result["plugins"][0]["artifactVerification"] = copy.deepcopy(
                            report["artifacts"][0]
                        )
                        result_path.write_text(json.dumps(result), encoding="utf-8")
                        summary_path = output / "compat" / "summary.json"
                        summary = json.loads(summary_path.read_text(encoding="utf-8"))
                        summary["results"] = [
                            copy.deepcopy(result)
                            if row.get("matrix") == disk_id
                            else row
                            for row in summary["results"]
                        ]
                        summary_path.write_text(json.dumps(summary), encoding="utf-8")
                elif scenario in {
                    "requested-install-order",
                    "plugin-artifact-id",
                    "plugin-order",
                    "embedded-artifact-verification",
                    "materialization-archive",
                    "materialization-assembly",
                    "materialization-dlls",
                }:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    plugin = result["plugins"][0]
                    if scenario == "requested-install-order":
                        result["requestedInstallOrder"].reverse()
                    elif scenario == "plugin-artifact-id":
                        plugin["artifactId"] = "wrong-plugin"
                    elif scenario == "plugin-order":
                        result["plugins"].reverse()
                    elif scenario == "embedded-artifact-verification":
                        plugin["artifactVerification"]["archive"]["size"] += 1
                    elif scenario == "materialization-archive":
                        plugin["materialization"]["archiveSha256"] = "0" * 64
                    elif scenario == "materialization-assembly":
                        plugin["materialization"]["assemblySha256"] = "0" * 64
                    else:
                        plugin["materialization"]["dllInventory"] = {}
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                    summary_path = output / "compat" / "summary.json"
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    summary["results"] = [
                        copy.deepcopy(result)
                        if row.get("matrix") == disk_id
                        else row
                        for row in summary["results"]
                    ]
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                elif scenario == "one-byte":
                    after_path.write_bytes(after_path.read_bytes() + b" ")
                elif scenario in {"hash", "count", "count-bool", "check-bool"}:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    disk = result["webrootDisk"]
                    marker_row = disk["effects"]["direct-plugin"]["markers"][marker]
                    if scenario == "hash":
                        disk["afterSha256"] = "0" * 64
                    elif scenario == "count":
                        marker_row["afterCount"] = 2
                    elif scenario == "count-bool":
                        marker_row["beforeCount"] = False
                    else:
                        disk["effects"]["direct-plugin"]["checks"][
                            "rawMarkersExactAfter"
                        ] = 1
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                elif scenario == "marker-contract":
                    changed = after_path.read_bytes().replace(
                        marker.encode("utf-8"), b"<!-- direct-raw-markeX -->"
                    )
                    after_path.write_bytes(changed)
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    result["webrootDisk"] = evidence_validation.reconstruct_webroot_disk(
                        (disk_directory / "webroot-before.html").read_bytes(),
                        changed,
                        fixture["requirements"],
                        disk_id,
                    )
                    self.assertIs(result["webrootDisk"]["allPassed"], False)
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                elif scenario == "result-service":
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    result["service"] = "jf10"
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                elif scenario == "network-service":
                    network = json.loads(network_path.read_text(encoding="utf-8"))
                    network["service"] = "jf10"
                    network_path.write_text(json.dumps(network), encoding="utf-8")
                elif scenario == "nondisk-null":
                    nondisk_result_path = (
                        output / "compat" / nondisk_id / "result.json"
                    )
                    nondisk = json.loads(
                        nondisk_result_path.read_text(encoding="utf-8")
                    )
                    nondisk.pop("webrootDisk")
                    nondisk_result_path.write_text(
                        json.dumps(nondisk), encoding="utf-8"
                    )
                elif scenario.startswith("result-"):
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if scenario == "result-order-pair":
                        result["orderPair"] = "wrong-order"
                    elif scenario == "result-expected-runtime-order":
                        result["expectedRuntimePluginOrder"] = list(
                            reversed(result["expectedRuntimePluginOrder"])
                        )
                    else:
                        result["observedRuntimePluginOrder"] = list(
                            reversed(result["observedRuntimePluginOrder"])
                        )
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                elif scenario.startswith("summary-"):
                    summary_path = output / "compat" / "summary.json"
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    if scenario == "summary-pair-missing":
                        summary.pop("pairRuntimeOrderChecks")
                    elif scenario == "summary-result-stale":
                        summary["results"][0]["service"] = "stale-service"
                    else:
                        summary["pairRuntimeOrderChecks"][
                            str(fixture["order_pair"])
                        ] = False
                    summary_path.write_text(json.dumps(summary), encoding="utf-8")
                elif scenario.startswith("required-check-"):
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    required = result["refreshKit"]["cacheEvidence"][
                        "requiredChecks"
                    ]
                    check_name = next(iter(required))
                    if scenario == "required-check-missing":
                        required.pop(check_name)
                    else:
                        required[check_name] = 1
                    result_path.write_text(json.dumps(result), encoding="utf-8")
                else:
                    raw_path = disk_directory / "shell-after.html"
                    raw_path.write_bytes(raw_path.read_bytes() + b" ")

                expected_error = {
                    "all-locked-verified": "row identity is invalid",
                    "matrix-artifact-stale": (
                        "matrix artifact receipt differs from all-locked verification"
                    ),
                    "requested-install-order": "requested install order differs",
                    "plugin-artifact-id": "result plugins differ",
                    "plugin-order": "result plugins differ",
                    "embedded-artifact-verification": "result plugins differ",
                    "materialization-archive": "result plugins differ",
                    "materialization-assembly": "result plugins differ",
                    "materialization-dlls": "result plugins differ",
                }.get(scenario, ".+")
                with self.assertRaisesRegex(
                    evidence_validation.EvidenceValidationError,
                    expected_error,
                ):
                    evidence_validation.validate_compatibility_tree(
                        output,
                        pathlib.Path(fixture["build"]),
                        pathlib.Path(fixture["matrix_path"]),
                    )

    def test_strict_compatibility_evidence_requires_every_matrix_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            artifact_root = root / "artifacts"
            output = root / "output"
            matrix_path = root / "matrices.json"
            lock_path = root / "ecosystem.lock.json"
            ids = ["first-matrix", "second-matrix"]
            runtimes = {ids[0]: "jf10", ids[1]: "jf12"}
            artifact_ids = {
                matrix_id: f"{matrix_id}-plugin" for matrix_id in ids
            }
            locked_artifacts = [
                self.make_locked_artifact(artifact_ids[matrix_id], runtimes[matrix_id])
                for matrix_id in ids
            ]
            self.write_json_fixture(
                lock_path,
                {"schemaVersion": 1, "artifacts": locked_artifacts},
            )
            matrix_path.write_text(
                json.dumps({
                    "matrices": [
                        {
                            "id": value,
                            "runtime": runtimes[value],
                            "service": runtimes[value],
                            "installOrder": ["@refresh-kit", artifact_ids[value]],
                            "cacheExpectation": "safe-degrade"
                            if value == ids[1]
                            else "required",
                            "requiredUnversionedOuterArtifacts": ["outer-plugin"]
                            if value == ids[1]
                            else [],
                            "webrootDiskRequirements": {},
                        }
                        for value in ids
                    ]
                }),
                encoding="utf-8",
            )
            summary = {
                "schemaVersion": 1,
                "outcome": "pass-with-limitation",
                "expectedMatrices": ids,
                "completedMatrices": ids,
                "missingMatrices": [],
                "failedMatrices": [],
                "expectedSafeDegradedMatrices": [ids[1]],
                "safeDegradedMatrices": [ids[1]],
                "missingSafeDegradedMatrices": [],
                "expectedPassWithLimitationMatrices": [ids[1]],
                "passWithLimitationMatrices": [ids[1]],
                "missingPassWithLimitationMatrices": [],
                "unexpectedPassWithLimitationMatrices": [],
                "pairRuntimeOrderChecks": {},
            }
            (artifact_root).mkdir()
            all_locked = self.make_artifact_report(locked_artifacts)
            self.write_json_fixture(
                artifact_root / "all-locked-verification.json", all_locked
            )
            summary["allLockedVerification"] = (
                evidence_validation.artifact_verification_summary(
                    artifact_root / "all-locked-verification.json", all_locked
                )
            )
            (artifact_root / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            build = root / "build"
            for runtime, stage_name in (("jf10", "stage"), ("jf12", "stage-jf12")):
                stage = build / stage_name
                stage.mkdir(parents=True)
                (stage / "Jellyfin.Plugin.RefreshKit.dll").write_bytes(runtime.encode())
                (stage / "meta.json").write_text(
                    json.dumps({"runtimeFixture": runtime}), encoding="utf-8"
                )
            for matrix_id in ids:
                directory = artifact_root / matrix_id
                directory.mkdir()
                runtime = runtimes[matrix_id]
                cache_expectation = (
                    "safe-degrade" if matrix_id == ids[1] else "required"
                )
                http_captures, cache_evidence, generation = (
                    self.make_cache_http_fixture(cache_expectation)
                )
                stage_name = "stage" if runtime == "jf10" else "stage-jf12"
                stage = build / stage_name
                all_locked_by_id = {
                    row["id"]: row for row in all_locked["artifacts"]
                }
                verification_row = copy.deepcopy(
                    all_locked_by_id[artifact_ids[matrix_id]]
                )
                matrix_report = {
                    "schemaVersion": 1,
                    "artifacts": [verification_row],
                    "allPassed": True,
                }
                values = {
                    "static.json": {"schemaVersion": 1, "allPassed": True},
                    "stage.json": {
                        "schemaVersion": 1,
                        "valid": True,
                        "runtime": runtime,
                        "stage": str(stage),
                        "meta": json.loads((stage / "meta.json").read_text(encoding="utf-8")),
                        "dllSha256": collector.file_hash(
                            stage / "Jellyfin.Plugin.RefreshKit.dll"
                        ),
                    },
                    "network.json": {
                        "schemaVersion": 1,
                        "valid": True,
                        "allPassed": True,
                        "service": runtime,
                        "configuredImage": evidence_validation.IMAGE_REFERENCES[runtime],
                        "expectedImageDigest": evidence_validation.IMAGE_DIGESTS[runtime],
                        "internalBridge": True,
                        "noGateway": True,
                        "originMode": "verified-internal-bridge",
                        "publishedLoopbackActive": False,
                        "internalIpv4": "10.77.0.2",
                        "selectedOrigin": "http://10.77.0.2:8096",
                    },
                    "artifact-verification.json": matrix_report,
                    "result.json": {
                        "schemaVersion": 1,
                        "matrix": matrix_id,
                        "runtime": runtime,
                        "service": runtime,
                        "serverVersion": evidence_validation.SERVER_VERSIONS[runtime],
                        "image": evidence_validation.IMAGE_REFERENCES[runtime],
                        "errors": [],
                        "outcome": "pass-with-limitation"
                        if matrix_id == ids[1]
                        else "pass",
                        "cacheExpectation": cache_expectation,
                        "requestedInstallOrder": [
                            "@refresh-kit",
                            artifact_ids[matrix_id],
                        ],
                        "orderPair": None,
                        "expectedRuntimePluginOrder": None,
                        "observedRuntimePluginOrder": None,
                        "webrootDisk": None,
                        "contentProbes": {},
                        "limitations": [
                            {
                                "code": "outer-owner-unversioned-shell-tag",
                                "artifactId": "outer-plugin",
                                "status": "pass-with-limitation",
                                "checks": {"singleTagPresent": True},
                            }
                        ]
                        if matrix_id == ids[1]
                        else [],
                        "refreshKit": {
                            "generationAfter": generation,
                            "cacheEvidence": cache_evidence,
                        },
                        "plugins": [self.result_plugin(verification_row)],
                    },
                }
                for name, value in values.items():
                    (directory / name).write_text(json.dumps(value), encoding="utf-8")
                for name, raw in http_captures.items():
                    (directory / name).write_bytes(raw)

            summary["results"] = [
                json.loads((artifact_root / matrix_id / "result.json").read_text())
                for matrix_id in ids
            ]
            (artifact_root / "summary.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )

            old_artifacts = collector.COMPAT_ARTIFACTS
            old_matrices = collector.COMPAT_MATRICES
            old_lock = collector.COMPAT_LOCK
            collector.COMPAT_ARTIFACTS = artifact_root
            collector.COMPAT_MATRICES = matrix_path
            collector.COMPAT_LOCK = lock_path
            try:
                evidence = {"missing": [], "collected": []}
                errors: list[str] = []
                collector.collect_compatibility(output, evidence, errors, True, build, 0)
                self.assertEqual(errors, [])
                summary["safeDegradedMatrices"] = []
                (artifact_root / "summary.json").write_text(
                    json.dumps(summary), encoding="utf-8"
                )
                evidence, errors = {"missing": [], "collected": []}, []
                collector.collect_compatibility(output, evidence, errors, True, build, 0)
                self.assertTrue(any("summary" in error for error in errors))
                summary["safeDegradedMatrices"] = [ids[1]]
                (artifact_root / "summary.json").write_text(
                    json.dumps(summary), encoding="utf-8"
                )
                result_path = artifact_root / ids[1] / "result.json"
                result_value = json.loads(result_path.read_text(encoding="utf-8"))
                result_value["cacheExpectation"] = "required"
                result_path.write_text(json.dumps(result_value), encoding="utf-8")
                evidence, errors = {"missing": [], "collected": []}, []
                collector.collect_compatibility(output, evidence, errors, True, build, 0)
                self.assertTrue(any("wrong cache expectation" in error for error in errors))
                result_value["cacheExpectation"] = "safe-degrade"
                result_path.write_text(json.dumps(result_value), encoding="utf-8")
                result_value["limitations"] = []
                result_path.write_text(json.dumps(result_value), encoding="utf-8")
                evidence, errors = {"missing": [], "collected": []}, []
                collector.collect_compatibility(output, evidence, errors, True, build, 0)
                self.assertTrue(any("limitations" in error for error in errors))
                result_value["limitations"] = [
                    {
                        "code": "outer-owner-unversioned-shell-tag",
                        "artifactId": "outer-plugin",
                        "status": "pass-with-limitation",
                        "checks": {"singleTagPresent": True},
                    }
                ]
                result_path.write_text(json.dumps(result_value), encoding="utf-8")
                result_value["refreshKit"]["cacheEvidence"]["primary"]["framingMode"] = None
                result_path.write_text(json.dumps(result_value), encoding="utf-8")
                evidence, errors = {"missing": [], "collected": []}, []
                collector.collect_compatibility(output, evidence, errors, True, build, 0)
                self.assertTrue(any("safe-degrade proof" in error for error in errors))
                result_value["refreshKit"]["cacheEvidence"]["primary"][
                    "framingMode"
                ] = "chunked"
                result_path.write_text(json.dumps(result_value), encoding="utf-8")
                (artifact_root / ids[1] / "result.json").unlink()
                evidence, errors = {"missing": [], "collected": []}, []
                collector.collect_compatibility(output, evidence, errors, True, build, 0)
                self.assertTrue(any("missing required" in error for error in errors))
            finally:
                collector.COMPAT_ARTIFACTS = old_artifacts
                collector.COMPAT_MATRICES = old_matrices
                collector.COMPAT_LOCK = old_lock


class EvidenceRedactionTests(unittest.TestCase):
    def test_default_and_custom_fixture_passwords_are_redacted_and_forbidden(self) -> None:
        environment = {
            "RK_LAB_PASSWORD": "custom-lab-password-91",
            "RK_LAB_VIEWER_PASSWORD": "custom-viewer-password-64",
            "RK_PASS": "custom-proxy-password-82",
            "RK_COMPAT_PASSWORD": "custom-compat-password-73",
        }
        with mock.patch.dict(collector.os.environ, environment, clear=False):
            values = (
                *environment.values(),
                "Test669Pw!x",
                "Viewer669Pw!x",
                "Compat669Pw!x",
            )
            sanitized = collector.sanitize_text(" | ".join(values))
            for custom in environment.values():
                self.assertNotIn(custom, sanitized)
                self.assertTrue(
                    collector.contains_forbidden_secret(custom),
                    f"custom fixture secret was absent from the upload guard: {custom}",
                )

        with mock.patch.dict(
            collector.os.environ,
            {name: "" for name in collector.FIXTURE_SECRET_DEFAULTS},
            clear=False,
        ):
            defaults = collector.known_fixture_secrets()
            self.assertIn("Test669Pw!x", defaults)
            self.assertIn("Viewer669Pw!x", defaults)
            self.assertIn("Compat669Pw!x", defaults)
            sanitized = collector.sanitize_text(
                "Test669Pw!x Viewer669Pw!x Compat669Pw!x"
            )
            self.assertNotIn("Test669Pw!x", sanitized)
            self.assertNotIn("Viewer669Pw!x", sanitized)
            self.assertNotIn("Compat669Pw!x", sanitized)

    def test_header_cookie_session_and_assignment_credentials_are_redacted(self) -> None:
        secrets = (
            "basic-value",
            "cookie-value",
            "refresh-value",
            "session-value",
            "environment-token",
            "assignment-bearer",
            "assignment-cookie",
            "query-emby-token",
        )
        source = (
            "Authorization: Basic basic-value\n"
            "Set-Cookie: sid=cookie-value; HttpOnly\n"
            "refresh_token=refresh-value session-id: session-value\n"
            "TOKEN=environment-token\n"
            "Authorization=Bearer assignment-bearer\n"
            "Cookie=sid=assignment-cookie; Secure\n"
            "https://example.invalid/System/Info?X-Emby-Token=query-emby-token\n"
        )
        sanitized = collector.sanitize_text(source)
        for secret in secrets:
            self.assertNotIn(secret, sanitized)
        self.assertGreaterEqual(sanitized.count("<redacted>"), 8)
        self.assertTrue(collector.contains_forbidden_secret(source))
        self.assertTrue(collector.contains_forbidden_secret("refresh_token=unredacted"))
        self.assertTrue(
            collector.contains_forbidden_secret("Authorization=Bearer unredacted")
        )
        self.assertTrue(collector.contains_forbidden_secret("Cookie=sid=unredacted"))
        self.assertFalse(collector.contains_forbidden_secret("refresh_token=<redacted>"))
        self.assertFalse(
            collector.contains_forbidden_secret("Authorization=<redacted>")
        )
        self.assertFalse(collector.contains_forbidden_secret("Cookie=<redacted>"))

    def test_collector_refuses_a_nonempty_output_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = pathlib.Path(temporary) / "evidence"
            output.mkdir()
            (output / "stale.json").write_text("{}\n", encoding="utf-8")
            with mock.patch.object(
                sys,
                "argv",
                ["collect-ci-evidence.py", "--output", str(output)],
            ), self.assertRaises(SystemExit) as raised:
                collector.main()
            self.assertIn("must be fresh", str(raised.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
