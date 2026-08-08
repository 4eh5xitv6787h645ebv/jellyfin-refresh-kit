#!/usr/bin/env python3
"""Static, container-free validation of the compatibility harness."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import analyze as analyze_lib
import artifacts as artifact_lib
import manifest as manifest_lib


HERE = Path(__file__).resolve().parent
COMPAT_ROOT = HERE.parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise artifact_lib.HarnessError(message)


def main() -> int:
    try:
        lock, matrices = manifest_lib.load_and_validate(
            COMPAT_ROOT / "ecosystem.lock.json", COMPAT_ROOT / "matrices.json"
        )
        locked_artifacts = artifact_lib.artifact_index(lock)
        safe_degrade_ids = {
            matrix["id"]
            for matrix in matrices["matrices"]
            if matrix["cacheExpectation"] == "safe-degrade"
        }
        require(
            safe_degrade_ids == manifest_lib.SAFE_DEGRADE_MATRIX_IDS,
            "safe-degrade cache assertions must remain limited to the audited "
            "outer-buffer matrices",
        )
        actual_unversioned_outer = {
            matrix["id"]: set(matrix.get("requiredUnversionedOuterArtifacts", []))
            for matrix in matrices["matrices"]
            if matrix.get("requiredUnversionedOuterArtifacts")
        }
        require(
            actual_unversioned_outer
            == manifest_lib.UNVERSIONED_OUTER_ARTIFACTS_BY_MATRIX,
            "unversioned outer-owner limitations must remain limited to both "
            "audited GetAvatar order matrices",
        )
        require(
            locked_artifacts["actor-plus-jf10"]["plugin"]["name"] == "Actor Plus",
            "Actor Plus lock name must match the exact upstream Plugin.PluginName",
        )
        fixture_path = COMPAT_ROOT / "fixtures" / "stamping.json"
        fixtures = artifact_lib.load_json(fixture_path)
        require(
            isinstance(fixtures, dict) and fixtures.get("schemaVersion") == 1,
            "stamping fixtures must be schemaVersion 1",
        )
        cases = fixtures.get("cases")
        require(isinstance(cases, list), "stamping fixtures need a cases array")
        fixture_ids = [case.get("id") for case in cases if isinstance(case, dict)]
        expected_fixture_ids = lock["coverageExpectations"]["fixtureIds"]
        require(
            sorted(fixture_ids) == sorted(expected_fixture_ids),
            f"fixture ids {sorted(fixture_ids)} != {sorted(expected_fixture_ids)}",
        )
        for case in cases:
            require(isinstance(case.get("html"), str) and case["html"], "fixture html is empty")
            require(
                re.fullmatch(r"g-[0-9a-f]{16}", str(case.get("generation", ""))) is not None,
                f"{case.get('id')}: fixture generation is not a production-shaped token",
            )
            require(
                isinstance(case.get("expectedStampCount"), int)
                and case["expectedStampCount"] >= 0,
                f"{case.get('id')}: invalid expectedStampCount",
            )
            require(isinstance(case.get("mustContain"), list), "fixture missing mustContain")
            require(isinstance(case.get("mustNotContain"), list), "fixture missing mustNotContain")

        compose = (COMPAT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for runtime in ("jf10", "jf12"):
            image = matrices["runtimes"][runtime]["image"]
            require(compose.count(image) == 1, f"Compose does not contain exact {runtime} image pin")
        require(compose.count("read_only: true") == 2, "both Jellyfin services must be read-only")
        require("internal: true" in compose, "runtime network must be internal-only")
        require(compose.count('"127.0.0.1:${RK_COMPAT_') == 2, "ports must bind loopback")
        forbidden_compose = (
            "container_name:",
            "network_mode: host",
            "privileged: true",
            "/var/run/docker.sock",
            "external: true",
        )
        for fragment in forbidden_compose:
            require(fragment not in compose, f"forbidden Compose setting: {fragment}")

        editors_config = COMPAT_ROOT / "fixtures" / "configurations" / "EditorsChoicePlugin.xml"
        editors_root = ET.parse(editors_config).getroot()
        require(
            editors_root.findtext("DoScriptInject") == "false",
            "Editor's Choice direct web-root injection must be disabled",
        )
        require(
            editors_root.findtext("FileTransformation") == "true",
            "Editor's Choice File Transformation mode must be enabled",
        )
        # GitHub's effective release-assets URL can carry expiring signed query
        # values.  Prove the fetch-report validator accepts only the stable
        # public URL from the audited lock and rejects the old leak shape.
        sample_artifact = lock["artifacts"][0]
        safe_report = {
            "schemaVersion": 1,
            "artifacts": [{"archive": {"url": sample_artifact["archive"]["url"]}}],
            "allPassed": True,
        }
        artifact_lib.validate_fetch_report(safe_report, [sample_artifact])
        unsafe_report = json.loads(json.dumps(safe_report))
        unsafe_report["artifacts"][0]["archive"]["finalUrl"] = (
            "https://release-assets.githubusercontent.com/example/archive.zip?jwt=secret"
        )
        try:
            artifact_lib.validate_fetch_report(unsafe_report, [sample_artifact])
        except artifact_lib.HarnessError:
            pass
        else:
            raise artifact_lib.HarnessError("signed effective URL evidence was not rejected")

        common = (COMPAT_ROOT / "lib" / "common.sh").read_text(encoding="utf-8")
        runtime = (COMPAT_ROOT / "lib" / "runtime.sh").read_text(encoding="utf-8")
        require(
            "EDITORS_CONFIG_SOURCE" in runtime
            and '"${CONTAINER}:${EDITORS_CONFIG_DESTINATION}"' in runtime,
            "runtime does not preload the documented Editor's Choice configuration",
        )
        require(
            runtime.index("EDITORS_CONFIG_SOURCE") < runtime.index('PHASE="plugin load"'),
            "Editor's Choice configuration must be copied before plugin startup",
        )
        for fragment in (
            "com.docker.compose.project",
            "com.docker.compose.service",
            "expectedImageDigest",
            'network.get("Internal") is not True',
            'attachment.get("Gateway")',
            "exclusiveProjectNetwork",
            "verified-internal-bridge",
        ):
            require(fragment in common, f"internal bridge fallback lacks assertion: {fragment}")
        require(
            'compat_container_details "${RUNTIME}" "${OUT}/network.json"' in runtime,
            "runtime does not retain verified network fallback evidence",
        )
        require(
            '"${OUT}/network.json"' in runtime and '"${ORIGIN_MODE}"' in runtime,
            "runtime does not bind its selected origin into network evidence",
        )
        for fragment in (
            'CONDITIONAL_ETAG=\'"rk-compat-probe"\'',
            ': > "${OUT}/conditional.html"',
            '"${OUT}/conditional.headers"',
            '"${OUT}/conditional.html"',
        ):
            require(fragment in runtime, f"safe-degrade conditional capture is missing: {fragment}")

        analyzer = (COMPAT_ROOT / "lib" / "analyze.py").read_text(encoding="utf-8")
        for fragment in (
            'matrix["cacheExpectation"] == "safe-degrade"',
            '"primaryFramingValid"',
            '"conditionalFramingValid"',
            '"framingMode"',
            '"conditionalAssetMultisetMatchesPrimary"',
            '"expectedSafeDegradedMatrices"',
            '"missingSafeDegradedMatrices"',
            '"pass-with-limitation"',
            '"expectedPassWithLimitationMatrices"',
        ):
            require(fragment in analyzer, f"safe-degrade analyzer assertion is missing: {fragment}")

        generation = "g-0123456789abcdef"
        safe_body = (
            '<html><body><script plugin="Jellyfin Refresh Kit" '
            'data-name="RefreshKitPlugin" '
            f'data-boot-version="{generation}" '
            f'src="/RefreshKit/kit.js?v={generation}"></script></body></html>'
        ).encode()
        safe_headers = {
            "cache-control": ["private, no-store"],
            "content-length": [str(len(safe_body))],
        }
        safe_checks = analyze_lib.evaluate_safe_degrade(
            200,
            safe_headers,
            safe_body,
            "200",
            200,
            safe_headers,
            safe_body,
            safe_body.decode(),
            generation,
            Counter([f"/RefreshKit/kit.js?v={generation}"]),
            Counter([f"/RefreshKit/kit.js?v={generation}"]),
        )
        require(all(safe_checks.values()), "valid safe-degrade response evidence was rejected")
        chunked_headers = {
            "cache-control": ["private, no-store"],
            "transfer-encoding": ["Chunked"],
        }
        chunked_checks = analyze_lib.evaluate_safe_degrade(
            200,
            chunked_headers,
            safe_body,
            "200",
            200,
            chunked_headers,
            safe_body,
            safe_body.decode(),
            generation,
            Counter([f"/RefreshKit/kit.js?v={generation}"]),
            Counter([f"/RefreshKit/kit.js?v={generation}"]),
        )
        require(
            all(chunked_checks.values())
            and analyze_lib.response_framing_mode(chunked_headers, safe_body) == "chunked",
            "valid chunked safe-degrade framing was rejected",
        )
        require(
            analyze_lib.response_framing_mode(safe_headers, safe_body) == "content-length",
            "valid exact Content-Length framing was not classified",
        )
        invalid_framing = {
            "both Content-Length and Transfer-Encoding": {
                **safe_headers,
                "transfer-encoding": ["chunked"],
            },
            "neither Content-Length nor Transfer-Encoding": {
                "cache-control": ["private, no-store"],
            },
            "non-chunked Transfer-Encoding": {
                "cache-control": ["private, no-store"],
                "transfer-encoding": ["gzip, chunked"],
            },
            "mismatched Content-Length": {
                "cache-control": ["private, no-store"],
                "content-length": [str(len(safe_body) + 1)],
            },
        }
        for label, invalid_headers in invalid_framing.items():
            require(
                analyze_lib.response_framing_mode(invalid_headers, safe_body) is None,
                f"invalid safe-degrade framing was accepted: {label}",
            )
        unsafe_headers = {**safe_headers, "etag": ['"rk-stale"']}
        unsafe_checks = analyze_lib.evaluate_safe_degrade(
            200,
            unsafe_headers,
            safe_body,
            "200",
            200,
            safe_headers,
            safe_body[:-1],
            safe_body[:-1].decode(),
            generation,
            Counter([f"/RefreshKit/kit.js?v={generation}"]),
            Counter([f"/RefreshKit/kit.js?v={generation}"]),
        )
        require(
            unsafe_checks["primaryEtagAbsent"] is False
            and unsafe_checks["conditionalFramingValid"] is False
            and unsafe_checks["conditionalHtmlDocument"] is False,
            "safe-degrade negative evidence was not rejected",
        )
        valid_limitation = analyze_lib.evaluate_unversioned_outer_limitation(
            {"tagCount": 1, "currentStampCount": 0, "unstampedEligibleCount": 1}
        )
        require(
            all(valid_limitation.values()),
            "valid single unversioned outer-owner tag was rejected",
        )
        for bad_shell in (
            {"tagCount": 0, "currentStampCount": 0, "unstampedEligibleCount": 0},
            {"tagCount": 1, "currentStampCount": 1, "unstampedEligibleCount": 0},
            {"tagCount": 2, "currentStampCount": 0, "unstampedEligibleCount": 2},
        ):
            require(
                not all(analyze_lib.evaluate_unversioned_outer_limitation(bad_shell).values()),
                "missing, stamped, or duplicate outer-owner tag limitation was accepted",
            )

        with tempfile.TemporaryDirectory(prefix="rk-compat-static-") as temporary:
            aggregate_root = Path(temporary)
            for matrix in matrices["matrices"]:
                matrix_dir = aggregate_root / matrix["id"]
                matrix_dir.mkdir()
                limited = bool(matrix.get("requiredUnversionedOuterArtifacts"))
                artifact_lib.write_json(
                    matrix_dir / "result.json",
                    {
                        "matrix": matrix["id"],
                        "cacheExpectation": matrix["cacheExpectation"],
                        "outcome": "pass-with-limitation" if limited else "pass",
                    },
                )
            aggregate_path = aggregate_root / "summary.json"
            aggregate_args = SimpleNamespace(
                results=aggregate_root,
                output=aggregate_path,
            )
            require(
                analyze_lib.cmd_aggregate(aggregate_args) == 0,
                "complete expected pass-with-limitation aggregate was rejected",
            )
            aggregate_value = artifact_lib.load_json(aggregate_path)
            require(
                aggregate_value.get("outcome") == "pass-with-limitation"
                and aggregate_value.get("passWithLimitationMatrices")
                == list(manifest_lib.UNVERSIONED_OUTER_ARTIFACTS_BY_MATRIX),
                "aggregate did not preserve exact limitation completeness",
            )
            first_limited = next(iter(manifest_lib.UNVERSIONED_OUTER_ARTIFACTS_BY_MATRIX))
            artifact_lib.write_json(
                aggregate_root / first_limited / "result.json",
                {
                    "matrix": first_limited,
                    "cacheExpectation": "safe-degrade",
                    "outcome": "pass",
                },
            )
            require(
                analyze_lib.cmd_aggregate(aggregate_args) == 1,
                "aggregate accepted a silently omitted expected limitation",
            )

        project = (COMPAT_ROOT / "fixtures" / "StaticFixtureHarness.csproj").read_text(
            encoding="utf-8"
        )
        require("ThirdPartyTagStamper.cs" in project, "fixture harness must link production stamper")
        require("PackageReference" not in project, "fixture harness must not add network packages")

        result = {
            "schemaVersion": 1,
            "artifactLock": {"count": len(lock["artifacts"]), "valid": True},
            "artifactReports": {"publicUrlsOnly": True, "redirectCredentialsExcluded": True},
            "catalogCoverage": lock["coverageExpectations"],
            "compose": {
                "loopbackOnly": True,
                "internalRuntimeNetwork": True,
                "verifiedInternalBridgeFallback": True,
                "pinnedImages": True,
                "projectScoped": True,
                "readOnlyRootFilesystem": True,
            },
            "fixtureCount": len(cases),
            "matrixCount": len(matrices["matrices"]),
            "safeDegradeCacheMatrices": sorted(safe_degrade_ids),
            "unversionedOuterLimitations": {
                matrix_id: sorted(artifact_ids)
                for matrix_id, artifact_ids in sorted(actual_unversioned_outer.items())
            },
            "allPassed": True,
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (artifact_lib.HarnessError, OSError, json.JSONDecodeError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
