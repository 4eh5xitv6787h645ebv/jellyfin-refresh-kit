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
            integration_collected = set(validation_run.REQUIRED_COLLECTED) - {
                "compat/summary.json"
            }
            compatibility_collected = {"compat/summary.json"}
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
        self.assertGreaterEqual(int(match.group(1)), 38)

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

    def test_collector_release_inventory_and_runner_wiring_are_explicit(self) -> None:
        expected_json = {
            "abi-floor/result.json",
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
        self.assertIn("127.0.0.1:${RK_ABI_FLOOR_PORT:-18119}:8096", compose)
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
    def test_strict_compatibility_evidence_requires_every_matrix_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            artifact_root = root / "artifacts"
            output = root / "output"
            matrix_path = root / "matrices.json"
            ids = ["first-matrix", "second-matrix"]
            runtimes = {ids[0]: "jf10", ids[1]: "jf12"}
            matrix_path.write_text(
                json.dumps({
                    "matrices": [
                        {
                            "id": value,
                            "runtime": runtimes[value],
                            "cacheExpectation": "safe-degrade"
                            if value == ids[1]
                            else "required",
                            "requiredUnversionedOuterArtifacts": ["outer-plugin"]
                            if value == ids[1]
                            else [],
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
            }
            (artifact_root).mkdir()
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
                stage_name = "stage" if runtime == "jf10" else "stage-jf12"
                stage = build / stage_name
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
                    },
                    "artifact-verification.json": {"schemaVersion": 1, "allPassed": True},
                    "result.json": {
                        "schemaVersion": 1,
                        "matrix": matrix_id,
                        "runtime": runtime,
                        "serverVersion": evidence_validation.SERVER_VERSIONS[runtime],
                        "image": evidence_validation.IMAGE_REFERENCES[runtime],
                        "errors": [],
                        "outcome": "pass-with-limitation"
                        if matrix_id == ids[1]
                        else "pass",
                        "cacheExpectation": "safe-degrade"
                        if matrix_id == ids[1]
                        else "required",
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
                            "cacheEvidence": {
                                "primary": {"framingMode": "chunked"},
                                "conditional": {"framingMode": "content-length"},
                                "safeDegradeChecks": {
                                    name: True for name in collector.SAFE_DEGRADE_CHECK_NAMES
                                },
                            }
                        }
                        if matrix_id == ids[1]
                        else {},
                    },
                }
                for name, value in values.items():
                    (directory / name).write_text(json.dumps(value), encoding="utf-8")

            old_artifacts, old_matrices = collector.COMPAT_ARTIFACTS, collector.COMPAT_MATRICES
            collector.COMPAT_ARTIFACTS, collector.COMPAT_MATRICES = artifact_root, matrix_path
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
                collector.COMPAT_ARTIFACTS, collector.COMPAT_MATRICES = old_artifacts, old_matrices


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
