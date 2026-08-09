#!/usr/bin/env python3
"""Static, container-free validation of the compatibility harness."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import analyze as analyze_lib
import artifacts as artifact_lib
import manifest as manifest_lib


HERE = Path(__file__).resolve().parent
COMPAT_ROOT = HERE.parent


def require(condition: bool, message: str) -> None:
    if not condition:
        raise artifact_lib.HarnessError(message)


def synthetic_verification_report(
    artifacts: list[dict[str, object]], cache_root: str = "/synthetic-cache"
) -> dict[str, object]:
    rows = []
    for artifact in artifacts:
        archive = artifact["archive"]
        plugin = artifact["plugin"]
        assembly = str(plugin["assembly"])
        assembly_sha = hashlib.sha256(str(artifact["id"]).encode()).hexdigest()
        policy = plugin["archiveMeta"]
        if policy == "upstream":
            meta = {
                "policy": policy,
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
                "policy": policy,
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
                    "path": f"{cache_root}/{archive['sha256']}-{archive['name']}",
                    "url": archive["url"],
                    "sha256": archive["sha256"],
                    "size": 1,
                    "memberCount": 2 if policy == "upstream" else 1,
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
        actual_external = {
            matrix["id"]: set(matrix.get("requiredPresentArtifacts", []))
            for matrix in matrices["matrices"]
            if matrix.get("requiredPresentArtifacts")
        }
        require(
            actual_external == manifest_lib.EXTERNAL_ARTIFACTS_BY_MATRIX,
            "external shell checks must remain limited to the exact Media Bar assets",
        )
        actual_absent = {
            matrix["id"]: set(matrix.get("requiredAbsentArtifacts", []))
            for matrix in matrices["matrices"]
            if matrix.get("requiredAbsentArtifacts")
        }
        require(
            actual_absent == manifest_lib.ABSENT_ARTIFACTS_BY_MATRIX,
            "required-absent checks must remain limited to the audited read-only "
            "direct-writer cases",
        )
        actual_assembly_versioned = {
            matrix["id"]: set(matrix.get("requiredAssemblyVersionedArtifacts", []))
            for matrix in matrices["matrices"]
            if matrix.get("requiredAssemblyVersionedArtifacts")
        }
        require(
            actual_assembly_versioned
            == manifest_lib.ASSEMBLY_VERSIONED_ARTIFACTS_BY_MATRIX,
            "assembly-versioned checks must remain limited to the exact PowerToys resources",
        )
        actual_preversioned = {
            matrix["id"]: set(matrix.get("requiredPreVersionedArtifacts", []))
            for matrix in matrices["matrices"]
            if matrix.get("requiredPreVersionedArtifacts")
        }
        require(
            actual_preversioned == manifest_lib.PREVERSIONED_ARTIFACTS_BY_MATRIX,
            "source-preversioned checks must remain the exact audited local "
            "version-query cases",
        )
        require(
            tuple(matrix["id"] for matrix in matrices["matrices"])
            == manifest_lib.EXPECTED_MATRIX_SEQUENCE
            and manifest_lib.matrix_contract_digest(matrices)
            == manifest_lib.EXPECTED_MATRIX_CONTRACT_SHA256,
            "the exact 14-matrix compatibility contract digest changed",
        )
        content_probe_occurrences = [
            probe
            for matrix in matrices["matrices"]
            for probe in matrix.get("contentProbes", [])
        ]
        exact_probe_occurrences = [
            probe
            for probe in content_probe_occurrences
            if probe["response"]["body"]["mode"] == "exact"
        ]
        expected_exact_resources = {
            "powertoys-jellytag-stylesheet": (
                330,
                "6df81644baeaf28b5361409516bb8bd39488e49894e084306370e89cc4a77578",
            ),
            "powertoys-privacy-mode-stylesheet": (
                1070,
                "8669c42b961582cd58533b9f96dbf1cf681c0f3cbc4d90b44c9cdb4f42b84f56",
            ),
            "powertoys-remote-trailers-stylesheet": (
                292,
                "3a42d2201e87f641bc7fdc1c0ac1fac60ae473a91d19f9e474dc862b9e1df700",
            ),
            "powertoys-thumbnail-stylesheet": (
                1340,
                "6aba971d7fdd2c555a52520d2cea3b74fe936b7782f61127725ade62c329978c",
            ),
            "powertoys-thumbnail-javascript": (
                21710,
                "2dd124f2e9dd9d9c646e57126c58ef2c0128fbd7da399e33a162cc92c751c20c",
            ),
        }
        require(
            len(content_probe_occurrences) == 15
            and len(content_probe_occurrences) * 3 == 45
            and Counter(probe["id"] for probe in exact_probe_occurrences)
            == Counter({probe_id: 2 for probe_id in expected_exact_resources})
            and all(
                (
                    probe["response"]["body"]["bytes"],
                    probe["response"]["body"]["sha256"],
                )
                == expected_exact_resources[probe["id"]]
                for probe in exact_probe_occurrences
            ),
            "content-probe count or source-derived PowerToys byte identities changed",
        )
        actual_runtime_orders = {
            matrix["id"]: tuple(matrix["expectedRuntimePluginOrder"])
            for matrix in matrices["matrices"]
            if matrix.get("expectedRuntimePluginOrder") is not None
        }
        require(
            actual_runtime_orders == manifest_lib.EXPECTED_RUNTIME_PLUGIN_ORDER,
            "order-pair matrices must retain their manifest-name runtime order",
        )
        require(
            locked_artifacts["actor-plus-jf10"]["plugin"]["name"] == "Actor Plus",
            "Actor Plus lock name must match the exact upstream Plugin.PluginName",
        )
        require(
            locked_artifacts["jellyfin-enhanced-jf10"]["plugin"]["version"] == "12.2.0.0"
            and locked_artifacts["jellyfin-enhanced-jf12"]["plugin"]["version"]
            == "12.2.0.0"
            and locked_artifacts["ratings-jf10"]["plugin"]["version"] == "1.0.374.0",
            "the three superseded Jellyfin Enhanced/Ratings artifacts were not refreshed",
        )
        require(
            locked_artifacts["moonbase-legacy"]["disposition"] == "quarantined"
            and locked_artifacts["moonbase-legacy"]["plugin"]["targetAbi"]
            == "10.10.0.0"
            and locked_artifacts["moonbase-legacy"]["plugin"]["framework"] == "net8.0",
            "Moonbase must remain an explicit legacy quarantine, never a current runtime pass",
        )
        generated_sidecar = artifact_lib.generated_meta(
            locked_artifacts["discontinue-watching-jf10"]
        )
        require(
            generated_sidecar.get("assemblies") == [],
            "generated install sidecars must preserve Jellyfin's load-all DLL semantics",
        )
        gelato_archive = locked_artifacts["gelato-jf10"]
        gelato_upstream_meta = {
            "guid": gelato_archive["plugin"]["guid"],
            "name": gelato_archive["plugin"]["name"],
            "version": gelato_archive["plugin"]["version"],
        }
        gelato_sidecar = artifact_lib.completed_upstream_meta(
            gelato_upstream_meta, gelato_archive
        )
        empty_sidecar = artifact_lib.completed_upstream_meta(
            {**gelato_upstream_meta, "assemblies": []}, gelato_archive
        )
        explicit_sidecar = artifact_lib.completed_upstream_meta(
            {**gelato_upstream_meta, "assemblies": ["Gelato.dll"]},
            gelato_archive,
        )
        require(
            gelato_archive["plugin"]["archiveMeta"] == "upstream"
            and gelato_archive["interactionMode"]
            == "javascript-injector-registration",
            "Gelato must retain its audited upstream-manifest runtime contract",
        )
        require(
            "assemblies" not in gelato_sidecar
            and empty_sidecar.get("assemblies") == []
            and explicit_sidecar.get("assemblies") == ["Gelato.dll"],
            "upstream assembly selection must preserve both load-all and explicit-whitelist semantics",
        )
        require(
            len(lock["catalogCoverage"]) == 101
            and sum(
                row["classification"] == artifact_lib.TESTABLE_CLASSIFICATION
                for row in lock["catalogCoverage"]
            )
            == 33,
            "the authoritative 101-row catalog or 33-row testable slice is incomplete",
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
            expected_count = 2 if runtime == "jf10" else 1
            require(
                compose.count(image) == expected_count,
                f"Compose does not contain the expected {runtime} image pins",
            )
        require(compose.count("read_only: true") == 3, "all Jellyfin roots must be read-only")
        require("internal: true" in compose, "runtime network must be internal-only")
        require(compose.count('"127.0.0.1:${RK_COMPAT_') == 3, "ports must bind loopback")
        require(
            "jf10-writable-web:/jellyfin/jellyfin-web" in compose,
            "direct-writer service lacks its disposable writable webroot volume",
        )
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
        safe_report = synthetic_verification_report([sample_artifact])
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
        runner = (COMPAT_ROOT / "run.sh").read_text(encoding="utf-8")
        require(
            runner.index("cmd_clean", runner.index("cmd_all()"))
            < runner.index("cmd_fetch all-locked", runner.index("cmd_all()"))
            < runner.index("while IFS=", runner.index("cmd_all()")),
            "the all-matrix gate must clean, inspect all locked archives, then run matrices",
        )
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
            'compat_container_details "${SERVICE}" "${RUNTIME}" "${OUT}/network.json"'
            in runtime,
            "runtime does not retain verified network fallback evidence",
        )
        require(
            '"${OUT}/network.json"' in runtime and '"${ORIGIN_MODE}"' in runtime,
            "runtime does not bind its selected origin into network evidence",
        )
        require(
            '[[ "${token}" =~ ^[0-9A-Fa-f]{32}$ ]]' in runtime,
            "runtime authentication must accept only exact 32-hex Jellyfin tokens",
        )
        for fragment in (
            'CONDITIONAL_ETAG=\'"rk-compat-probe"\'',
            ': > "${OUT}/conditional.html"',
            '"${OUT}/conditional.headers"',
            '"${OUT}/conditional.html"',
        ):
            require(fragment in runtime, f"safe-degrade conditional capture is missing: {fragment}")
        require(
            runtime.count("-H 'Accept: text/html'") == 6,
            "all five retained HTML representations and the route-status probe must "
            "send the browser-shaped text/html Accept header",
        )
        for fragment in (
            'configurations "${MATRIX_ID}"',
            'probes "${MATRIX_ID}"',
            '"${OUT}/configurations/${artifact_id}.json"',
            '"${OUT}/content-probes/${probe_id}.txt"',
            '"${OUT}/content-probes/${probe_id}.headers"',
            '"${OUT}/content-probes/${probe_id}.status"',
            "-H 'Accept-Encoding: identity'",
            '-H "Accept: ${probe_accept}"',
            '--connect-timeout 10 --max-time 30',
            '--max-filesize "${probe_max_bytes}"',
            '--no-location --globoff',
            "--write-out $'%{http_code}\\t%{url_effective}'",
            '"${OUT}/webroot-before.html"',
            '"${OUT}/webroot-after.html"',
            '/jellyfin/jellyfin-web/index.html',
        ):
            require(fragment in runtime, f"expanded interaction evidence is missing: {fragment}")
        probe_runtime = runtime[runtime.index('mkdir -p "${OUT}/content-probes"'):]
        require(
            "curl --fail" not in probe_runtime.split("while IFS=$'\\t' read -r _ artifact_id", 1)[0]
            and probe_runtime.count(': > "${probe_body}"') == 2
            and probe_runtime.count(': > "${probe_headers}"') == 2
            and probe_runtime.count(': > "${probe_status_file}"') == 2
            and probe_runtime.index(': > "${probe_body}"')
            < probe_runtime.index('if [ "${authenticated}" = true ]')
            and 'grep -Fq -- "${TOKEN}" "${probe_capture}"' in probe_runtime,
            "content probes must pre-truncate captures and wipe token-echo failures "
            "without curl --fail",
        )

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
            '"shellRequirements"',
            '"inlineRequirements"',
            'attribute_shell_assets(',
            'evaluate_webroot_disk(',
            '"contentProbes"',
            '"matrixConfigurations"',
            '"dllInventory"',
            '"assemblySelection"',
            '"load-all-packaged"',
            '"observedRuntimePluginOrder"',
            '"requiredChecks"',
            'evaluate_required_cache(',
            'evaluate_content_probe(',
            'read_bounded_content_probe_file(',
            'parse_http_response_blocks(',
            'content_type_details(',
            'content_type_essence(',
            '"utf8CharsetCoherent"',
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
        required_etag = f'"rk-{hashlib.sha256(safe_body).hexdigest()}"'
        required_headers = {
            "content-length": [str(len(safe_body))],
            "etag": [required_etag],
        }
        required_conditional_headers = {"etag": [required_etag]}
        required_checks = analyze_lib.evaluate_required_cache(
            200,
            required_headers,
            safe_body,
            "304",
            304,
            required_conditional_headers,
            b"",
        )
        require(
            all(required_checks.values()),
            "valid byte-bound required-cache response evidence was rejected",
        )
        required_negative_cases = {
            "primaryStatus200": (500, required_headers, safe_body, "304", 304, required_conditional_headers, b""),
            "validFraming": (200, {"etag": [required_etag]}, safe_body, "304", 304, required_conditional_headers, b""),
            "singleStrongEtag": (200, {**required_headers, "etag": [f'W/{required_etag}']}, safe_body, "304", 304, required_conditional_headers, b""),
            "etagMatchesBodySha256": (200, {**required_headers, "etag": ['"rk-' + ('0' * 64) + '"']}, safe_body, "304", 304, required_conditional_headers, b""),
            "lastModifiedAbsent": (200, {**required_headers, "last-modified": ["now"]}, safe_body, "304", 304, required_conditional_headers, b""),
            "conditionalStatus304": (200, required_headers, safe_body, "200", 200, required_conditional_headers, b""),
            "conditionalBodyEmpty": (200, required_headers, safe_body, "304", 304, required_conditional_headers, b"x"),
            "conditionalEtagMatches": (200, required_headers, safe_body, "304", 304, {"etag": ['"rk-' + ('0' * 64) + '"']}, b""),
            "conditionalFramingBodyless": (200, required_headers, safe_body, "304", 304, {**required_conditional_headers, "content-length": ["0"]}, b""),
            "conditionalLastModifiedAbsent": (200, required_headers, safe_body, "304", 304, {**required_conditional_headers, "last-modified": ["now"]}, b""),
        }
        for expected_failure, arguments in required_negative_cases.items():
            mutated = analyze_lib.evaluate_required_cache(*arguments)
            require(
                mutated[expected_failure] is False,
                f"required-cache mutation did not fail {expected_failure}",
            )
        remote_trailers_marker = "powertoys/RemoteTrailers"
        require(
            analyze_lib.count_exact_json_string(
                {"plugins": [remote_trailers_marker]}, remote_trailers_marker
            )
            == 1
            and analyze_lib.count_exact_json_string(
                {"description": f"prefix-{remote_trailers_marker}"},
                remote_trailers_marker,
            )
            == 0,
            "JSON content-probe markers must match exact scalar values",
        )
        json_array_contract = {"plugins": [remote_trailers_marker]}
        require(
            all(
                analyze_lib.evaluate_json_array_contains(
                    {"plugins": [remote_trailers_marker]}, json_array_contract
                ).values()
            ),
            "valid Jellyfin Web plugin-array content was rejected",
        )
        for invalid_json_shape in (
            remote_trailers_marker,
            [remote_trailers_marker],
            {"description": remote_trailers_marker},
            {"plugins": [f"prefix-{remote_trailers_marker}"]},
        ):
            require(
                not all(
                    analyze_lib.evaluate_json_array_contains(
                        invalid_json_shape, json_array_contract
                    ).values()
                ),
                f"invalid Jellyfin Web config shape was accepted: {invalid_json_shape!r}",
            )

        probe_origin = "http://127.0.0.1:18116"
        probe_body = b"prefix UNIQUE-CONTENT-MARKER suffix"
        semantic_probe = {
            "id": "synthetic-content",
            "path": "/assets/content.js",
            "authenticated": False,
            "request": {"accept": "text/javascript"},
            "response": {
                "status": 200,
                "mediaType": "text/javascript",
                "body": {
                    "mode": "semantic",
                    "minBytes": 1,
                    "maxBytes": 4096,
                },
            },
            "format": "text",
            "markers": {
                "synthetic-plugin": [
                    {"value": "UNIQUE-CONTENT-MARKER", "cardinality": 1}
                ]
            },
        }

        def content_headers(
            body: bytes,
            content_type: str = "text/javascript; charset=utf-8",
            extra: bytes = b"",
            status: bytes = b"HTTP/1.1 200 OK",
        ) -> bytes:
            return (
                status
                + b"\r\nContent-Type: "
                + content_type.encode("ascii")
                + b"\r\nContent-Length: "
                + str(len(body)).encode("ascii")
                + b"\r\n"
                + extra
                + b"\r\n"
            )

        probe_headers = content_headers(probe_body)
        probe_status = (
            f"200\t{probe_origin}{semantic_probe['path']}\n".encode("ascii")
        )
        positive_probe = analyze_lib.evaluate_content_probe(
            semantic_probe,
            probe_body,
            probe_headers,
            probe_status,
            probe_origin,
        )
        require(
            positive_probe["allPassed"] is True
            and all(positive_probe["checks"].values()),
            "valid strict content-probe response evidence was rejected",
        )

        protocol_mutations = {
            "wrong-origin": (
                probe_body,
                probe_headers,
                probe_status.replace(b"127.0.0.1", b"127.0.0.2"),
                "statusFileValid",
            ),
            "wrong-path": (
                probe_body,
                probe_headers,
                probe_status.replace(b"content.js", b"changed.js"),
                "statusFileValid",
            ),
            "http-1.0": (
                probe_body,
                probe_headers.replace(b"HTTP/1.1", b"HTTP/1.0"),
                probe_status,
                "headerSyntaxValid",
            ),
            "http-2": (
                probe_body,
                probe_headers.replace(b"HTTP/1.1", b"HTTP/2  "),
                probe_status,
                "headerSyntaxValid",
            ),
            "bare-lf": (
                probe_body,
                probe_headers.replace(b"\r\n", b"\n"),
                probe_status,
                "headerSyntaxValid",
            ),
            "bare-cr": (
                probe_body,
                probe_headers.replace(b"\r\n", b"\r"),
                probe_status,
                "headerSyntaxValid",
            ),
            "leading-blank": (
                probe_body,
                b"\r\n" + probe_headers,
                probe_status,
                "headerSyntaxValid",
            ),
            "truncated-termination": (
                probe_body,
                probe_headers[:-2],
                probe_status,
                "headerSyntaxValid",
            ),
            "extra-trailing-block": (
                probe_body,
                probe_headers + b"\r\n",
                probe_status,
                "headerSyntaxValid",
            ),
            "header-ctl": (
                probe_body,
                content_headers(probe_body, extra=b"X-Test: bad\x01value\r\n"),
                probe_status,
                "headerSyntaxValid",
            ),
            "header-unicode-line-separator": (
                probe_body,
                content_headers(probe_body).replace(
                    b"Content-Length:", "X-Test: bad\u2028value\r\nContent-Length:".encode("utf-8")
                ),
                probe_status,
                "headerSyntaxValid",
            ),
            "duplicate-response-block": (
                probe_body,
                b"HTTP/1.1 100 Continue\r\n\r\n" + probe_headers,
                probe_status,
                "singleResponseBlock",
            ),
            "duplicate-content-type": (
                probe_body,
                content_headers(
                    probe_body,
                    extra=b"Content-Type: text/javascript\r\n",
                ),
                probe_status,
                "singleContentType",
            ),
            "malformed-content-type": (
                probe_body,
                content_headers(probe_body, "text/javascript;"),
                probe_status,
                "contentTypeSyntaxValid",
            ),
            "duplicate-content-type-parameter": (
                probe_body,
                content_headers(
                    probe_body,
                    "text/javascript; charset=utf-8; CHARSET=us-ascii",
                ),
                probe_status,
                "contentTypeSyntaxValid",
            ),
            "wrong-content-type": (
                probe_body,
                content_headers(probe_body, "text/css"),
                probe_status,
                "mediaTypeExpected",
            ),
            "wrong-charset": (
                probe_body,
                content_headers(
                    probe_body, "text/javascript; charset=iso-8859-1"
                ),
                probe_status,
                "utf8CharsetCoherent",
            ),
            "compressed-response": (
                probe_body,
                content_headers(
                    probe_body, extra=b"Content-Encoding: gzip\r\n"
                ),
                probe_status,
                "identityContentEncoding",
            ),
            "missing-framing": (
                probe_body,
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/javascript\r\n\r\n",
                probe_status,
                "framingValid",
            ),
            "wrong-status": (
                probe_body,
                content_headers(
                    probe_body, status=b"HTTP/1.1 404 Not Found"
                ),
                probe_status.replace(b"200\t", b"404\t"),
                "statusExpected",
            ),
            "malformed-status-capture": (
                probe_body,
                probe_headers,
                probe_status.replace(b"200\t", b"200 "),
                "statusFileValid",
            ),
            "header-status-mismatch": (
                probe_body,
                probe_headers,
                probe_status.replace(b"200\t", b"404\t"),
                "statusConsistent",
            ),
            "redirect-302": (
                probe_body,
                content_headers(
                    probe_body, status=b"HTTP/1.1 302 Found"
                ),
                probe_status.replace(b"200\t", b"302\t"),
                "statusExpected",
            ),
            "invalid-utf8": (
                probe_body + b"\xff",
                content_headers(probe_body + b"\xff"),
                probe_status,
                "strictUtf8",
            ),
        }
        for label, (body, headers_value, status_value, failed_check) in (
            protocol_mutations.items()
        ):
            result = analyze_lib.evaluate_content_probe(
                semantic_probe,
                body,
                headers_value,
                status_value,
                probe_origin,
            )
            require(
                result["checks"][failed_check] is False
                and result["allPassed"] is False,
                f"content-probe protocol mutation did not fail closed: {label}",
            )

        for valid_content_type in (
            'text/javascript; charset="UTF-8"',
            "text/javascript; charset=utf8; profile=fixture",
        ):
            result = analyze_lib.evaluate_content_probe(
                semantic_probe,
                probe_body,
                content_headers(probe_body, valid_content_type),
                probe_status,
                probe_origin,
            )
            require(
                result["checks"]["utf8CharsetCoherent"] is True
                and result["allPassed"] is True,
                f"UTF-8-coherent Content-Type was rejected: {valid_content_type}",
            )

        duplicate_marker = analyze_lib.evaluate_content_probe(
            semantic_probe,
            probe_body + b" UNIQUE-CONTENT-MARKER",
            content_headers(probe_body + b" UNIQUE-CONTENT-MARKER"),
            probe_status,
            probe_origin,
        )
        require(
            duplicate_marker["artifactChecks"]["synthetic-plugin"]
            ["UNIQUE-CONTENT-MARKER"]["passed"] is False
            and duplicate_marker["allPassed"] is False,
            "duplicate content marker cardinality was accepted",
        )

        border_probe = json.loads(json.dumps(semantic_probe))
        border_probe["markers"] = {
            "synthetic-plugin": [{"value": "aba", "cardinality": 1}]
        }
        border_body = b"ababa"
        border_result = analyze_lib.evaluate_content_probe(
            border_probe,
            border_body,
            content_headers(border_body),
            probe_status,
            probe_origin,
        )
        require(
            border_result["artifactChecks"]["synthetic-plugin"]["aba"]["count"]
            == 2
            and border_result["allPassed"] is False,
            "overlapping content marker cardinality was accepted",
        )

        exact_probe = json.loads(json.dumps(semantic_probe))
        exact_probe["response"]["body"] = {
            "mode": "exact",
            "bytes": len(probe_body),
            "sha256": hashlib.sha256(probe_body).hexdigest(),
        }
        wrong_exact = analyze_lib.evaluate_content_probe(
            exact_probe,
            probe_body[:-1] + b"X",
            probe_headers,
            probe_status,
            probe_origin,
        )
        require(
            wrong_exact["checks"]["bodyHashValid"] is False
            and wrong_exact["allPassed"] is False,
            "wrong exact content hash was accepted",
        )
        wrong_exact_size_body = probe_body + b"x"
        wrong_exact_size = analyze_lib.evaluate_content_probe(
            exact_probe,
            wrong_exact_size_body,
            content_headers(wrong_exact_size_body),
            probe_status,
            probe_origin,
        )
        require(
            wrong_exact_size["checks"]["bodyBytesValid"] is False
            and wrong_exact_size["allPassed"] is False,
            "wrong exact content size was accepted",
        )

        json_probe = next(
            probe
            for matrix in matrices["matrices"]
            if matrix["id"] == "jf10-response-transformers-forward"
            for probe in matrix["contentProbes"]
            if probe["id"] == "powertoys-remote-trailers-config"
        )
        json_body = json.dumps(
            {
                "plugins": ["powertoys/RemoteTrailers"],
                "padding": "x" * 600,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        json_headers = content_headers(json_body, "application/json; charset=utf-8")
        json_status = f"200\t{probe_origin}/web/config.json\n".encode("ascii")
        json_positive = analyze_lib.evaluate_content_probe(
            json_probe, json_body, json_headers, json_status, probe_origin
        )
        require(json_positive["allPassed"] is True, "valid JSON content probe failed")
        duplicate_json_value = json.dumps(
            {
                "plugins": [
                    "powertoys/RemoteTrailers",
                    "powertoys/RemoteTrailers",
                ],
                "padding": "x" * 600,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        for label, invalid_json in {
            "duplicate-json-value": duplicate_json_value,
            "duplicate-json-key": (
                b'{"plugins":["powertoys/RemoteTrailers"],'
                b'"plugins":["powertoys/RemoteTrailers"],"padding":"'
                + (b"x" * 600)
                + b'"}'
            ),
            "bom-json": b"\xef\xbb\xbf" + json_body,
            "nonfinite-json": (
                b'{"plugins":["powertoys/RemoteTrailers"],"x":NaN,"padding":"'
                + (b"x" * 600)
                + b'"}'
            ),
            "positive-exponent-overflow-json": (
                b'{"plugins":["powertoys/RemoteTrailers"],"x":1e9999,"padding":"'
                + (b"x" * 600)
                + b'"}'
            ),
            "negative-exponent-overflow-json": (
                b'{"plugins":["powertoys/RemoteTrailers"],"x":-1e9999,"padding":"'
                + (b"x" * 600)
                + b'"}'
            ),
            "scalar-json": b'"' + (b"x" * 600) + b'"',
            "array-json": b'["' + (b"x" * 600) + b'"]',
            "deep-json": (
                b'{"plugins":["powertoys/RemoteTrailers"],"x":'
                + (b"[" * 1100)
                + b"0"
                + (b"]" * 1100)
                + b"}"
            ),
        }.items():
            invalid_result = analyze_lib.evaluate_content_probe(
                json_probe,
                invalid_json,
                content_headers(invalid_json, "application/json"),
                json_status,
                probe_origin,
            )
            if label == "duplicate-json-value":
                cardinality = invalid_result["jsonArrayContainsChecks"]["plugins"][
                    "powertoys/RemoteTrailers"
                ]
                require(
                    cardinality["passed"] is False
                    and cardinality["count"] == 2
                    and invalid_result["allPassed"] is False,
                    "duplicate JSON value cardinality was accepted",
                )
            else:
                require(
                    invalid_result["checks"]["formatValid"] is False
                    and invalid_result["allPassed"] is False,
                    f"invalid JSON content probe format was accepted: {label}",
                )
        empty_query = {"requiredKeys": [], "allowedKeys": [], "equals": {}}
        current_requirement = {
            "mode": "current-rkv",
            "cardinality": 2,
            "selectors": [
                {
                    "tag": "link",
                    "origin": "same-origin",
                    "path": "/ActorPlus/assets/birthage.css",
                    "query": {
                        "requiredKeys": ["rkv"],
                        "allowedKeys": ["rkv"],
                        "equals": {},
                    },
                    "cardinality": 1,
                },
                {
                    "tag": "script",
                    "origin": "same-origin",
                    "path": "/ActorPlus/assets/birthage.js",
                    "query": {
                        "requiredKeys": ["rkv"],
                        "allowedKeys": ["rkv"],
                        "equals": {},
                    },
                    "cardinality": 1,
                },
            ],
        }
        current_assets = [
            {
                "tag": "link",
                "url": f"../ActorPlus/assets/birthage.css?rkv={generation}",
            },
            {
                "tag": "script",
                "url": f"/ActorPlus/assets/birthage.js?rkv={generation}",
            },
        ]
        current_attribution, current_order, current_errors = (
            analyze_lib.attribute_shell_assets(
                current_assets, {"actor-plus-jf10": current_requirement}, generation
            )
        )
        require(
            not current_errors
            and current_order == ["actor-plus-jf10", "actor-plus-jf10"]
            and all(
                analyze_lib.evaluate_shell_requirement(
                    current_attribution["actor-plus-jf10"], current_requirement
                ).values()
            ),
            "valid normalized-path current-rkv evidence was rejected",
        )
        entity_parser = analyze_lib.ShellParser()
        entity_parser.feed(
            f'<script src="/web/configurationpage?&amp;amp;name=whisperSubs.js'
            f'&amp;rkv={generation}"></script>'
        )
        entity_requirement = {
            "mode": "current-rkv",
            "cardinality": 1,
            "selectors": [
                {
                    "tag": "script",
                    "origin": "same-origin",
                    "path": "/web/configurationpage",
                    "query": {
                        "requiredKeys": ["name", "rkv"],
                        "allowedKeys": ["name", "rkv"],
                        "equals": {"name": "whisperSubs.js"},
                    },
                    "cardinality": 1,
                }
            ],
        }
        entity_attribution, _, _ = analyze_lib.attribute_shell_assets(
            entity_parser.assets,
            {"whisper-subs-jf10": entity_requirement},
            generation,
        )
        require(
            len(entity_parser.assets) == 1
            and "&amp;name=whisperSubs.js" in entity_parser.assets[0]["url"]
            and entity_attribution["whisper-subs-jf10"]["tagCount"] == 0,
            "an already-decoded attribute entity was decoded a second time",
        )
        duplicate_parser = analyze_lib.ShellParser()
        duplicate_parser.feed(
            '<script src="/decoy.js" '
            f'src="/ActorPlus/assets/birthage.js?rkv={generation}"></script>'
        )
        duplicate_attribution, _, _ = analyze_lib.attribute_shell_assets(
            duplicate_parser.assets,
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        require(
            len(duplicate_parser.assets) == 1
            and duplicate_parser.assets[0]["url"] == "/decoy.js"
            and duplicate_attribution["actor-plus-jf10"]["tagCount"] == 0,
            "duplicate src handling did not retain the browser's first attribute",
        )
        external_base_parser = analyze_lib.ShellParser()
        external_base_parser.feed(
            '<base href="https://assets.example.invalid/web/">'
            '<script src="/ActorPlus/assets/birthage.js?'
            f'rkv={generation}"></script>'
        )
        external_base_attribution, _, _ = analyze_lib.attribute_shell_assets(
            external_base_parser.assets,
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        require(
            len(external_base_parser.assets) == 1
            and external_base_parser.assets[0]["baseUrl"]
            == "https://assets.example.invalid/web/"
            and external_base_attribution["actor-plus-jf10"]["tagCount"] == 0,
            "an external document base made a relative asset appear same-origin",
        )
        absolute_sentinel_attribution, _, _ = analyze_lib.attribute_shell_assets(
            [
                {
                    "tag": "script",
                    "url": (
                        "https://compat.invalid/ActorPlus/assets/birthage.js?"
                        f"rkv={generation}"
                    ),
                }
            ],
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        sentinel_base_parser = analyze_lib.ShellParser()
        sentinel_base_parser.feed(
            '<base href="https://compat.invalid/">'
            '<script src="/ActorPlus/assets/birthage.js?'
            f'rkv={generation}"></script>'
        )
        sentinel_base_attribution, _, _ = analyze_lib.attribute_shell_assets(
            sentinel_base_parser.assets,
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        require(
            absolute_sentinel_attribution["actor-plus-jf10"]["tagCount"] == 0
            and sentinel_base_attribution["actor-plus-jf10"]["tagCount"] == 0,
            "the synthetic normalization origin was accepted as the real origin",
        )
        actual_origin_parser = analyze_lib.ShellParser(
            "http://127.0.0.1:18096/web/index.html"
        )
        actual_origin_parser.feed(
            '<script src="http://127.0.0.1:18096/ActorPlus/assets/'
            f'birthage.js?rkv={generation}"></script>'
        )
        actual_origin_attribution, _, _ = analyze_lib.attribute_shell_assets(
            actual_origin_parser.assets,
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        require(
            actual_origin_attribution["actor-plus-jf10"]["tagCount"] == 1,
            "an absolute URL at the retained real document origin was rejected",
        )
        backslash_base_parser = analyze_lib.ShellParser(
            "http://127.0.0.1:18096/web/index.html"
        )
        backslash_base_parser.feed(
            r'<base href="\\evil.invalid\base\path">'
            '<script src="/ActorPlus/assets/birthage.js?'
            f'rkv={generation}"></script>'
        )
        backslash_base_attribution, _, _ = analyze_lib.attribute_shell_assets(
            backslash_base_parser.assets,
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        require(
            backslash_base_parser.base_url == "http://evil.invalid/base/path"
            and backslash_base_attribution["actor-plus-jf10"]["tagCount"] == 0,
            "a WHATWG network-path base written with backslashes appeared same-origin",
        )
        for malformed_base in (
            "http:////evil.invalid/base/",
            "http:///evil.invalid/base/",
        ):
            malformed_base_parser = analyze_lib.ShellParser(
                "http://127.0.0.1:18096/web/index.html"
            )
            malformed_base_parser.feed(
                f'<base href="{malformed_base}">'
                '<script src="/ActorPlus/assets/birthage.js?'
                f'rkv={generation}"></script>'
            )
            malformed_base_attribution, _, _ = analyze_lib.attribute_shell_assets(
                malformed_base_parser.assets,
                {"actor-plus-jf10": current_requirement},
                generation,
            )
            require(
                malformed_base_parser.assets[0]["baseSameOrigin"] is False
                and malformed_base_attribution["actor-plus-jf10"]["tagCount"] == 0,
                f"malformed explicit special-scheme base was trusted: {malformed_base}",
            )
        retained_network = {
            "originMode": "verified-internal-bridge",
            "selectedOrigin": "http://192.168.160.2:8096",
            "internalIpv4": "192.168.160.2",
            "publishedLoopbackActive": False,
        }
        require(
            analyze_lib.retained_selected_origin(retained_network)
            == "http://192.168.160.2:8096",
            "the retained internal network identity did not produce its real origin",
        )
        retained_network["selectedOrigin"] = "https://compat.invalid"
        require(
            analyze_lib.retained_selected_origin(retained_network) is None,
            "a selected origin that disagrees with retained network identity was accepted",
        )
        inert_template_parser = analyze_lib.ShellParser()
        inert_template_parser.feed(
            '<template><script src="/ActorPlus/assets/birthage.js?'
            f'rkv={generation}"></script></template>'
        )
        inert_template_attribution, _, _ = analyze_lib.attribute_shell_assets(
            inert_template_parser.assets,
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        require(
            inert_template_attribution["actor-plus-jf10"]["tagCount"] == 0,
            "a script inside inert template content was treated as live",
        )

        inert_base_parser = analyze_lib.ShellParser()
        inert_base_parser.feed(
            '<template><base href="https://assets.example.invalid/"></template>'
            '<noscript><script src="/ActorPlus/assets/birthage.js?'
            f'rkv={generation}"></script></noscript>'
            '<svg><base href="https://assets.example.invalid/"></base>'
            '<script src="/ActorPlus/assets/birthage.js?'
            f'rkv={generation}"></script></svg>'
            '<script src="/ActorPlus/assets/birthage.js?'
            f'rkv={generation}"></script>'
        )
        inert_base_attribution, _, _ = analyze_lib.attribute_shell_assets(
            inert_base_parser.assets,
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        require(
            inert_base_parser.base_url == analyze_lib.SHELL_DOCUMENT_URL
            and inert_base_attribution["actor-plus-jf10"]["tagCount"] == 1,
            "an inert template/noscript/foreign base or asset affected live attribution",
        )

        for inert_script_type in (
            "application/json",
            "module;foo",
            "text/javascript; charset=utf-8",
        ):
            data_block_parser = analyze_lib.ShellParser()
            data_block_parser.feed(
                f'<script type="{inert_script_type}" '
                'src="/ActorPlus/assets/birthage.js?'
                f'rkv={generation}"></script>'
            )
            data_block_attribution, _, _ = analyze_lib.attribute_shell_assets(
                data_block_parser.assets,
                {"actor-plus-jf10": current_requirement},
                generation,
            )
            require(
                data_block_attribution["actor-plus-jf10"]["tagCount"] == 0,
                f"non-executable script type was treated as live: {inert_script_type}",
            )
        for executable_script_type in ("module", "text/javascript"):
            executable_parser = analyze_lib.ShellParser()
            executable_parser.feed(
                f'<script type="{executable_script_type}" '
                'src="/ActorPlus/assets/birthage.js?'
                f'rkv={generation}"></script>'
            )
            executable_attribution, _, _ = analyze_lib.attribute_shell_assets(
                executable_parser.assets,
                {"actor-plus-jf10": current_requirement},
                generation,
            )
            require(
                executable_attribution["actor-plus-jf10"]["tagCount"] == 1,
                f"executable script type was ignored: {executable_script_type}",
            )
        for inert_language in ("vbscript", "module"):
            language_parser = analyze_lib.ShellParser()
            language_parser.feed(
                f'<script language="{inert_language}" '
                'src="/ActorPlus/assets/birthage.js?'
                f'rkv={generation}"></script>'
            )
            language_attribution, _, _ = analyze_lib.attribute_shell_assets(
                language_parser.assets,
                {"actor-plus-jf10": current_requirement},
                generation,
            )
            require(
                language_attribution["actor-plus-jf10"]["tagCount"] == 0,
                f"inert legacy script language was treated as live: {inert_language}",
            )
        for live_language in ("javascript", "JavaScript1.5"):
            language_parser = analyze_lib.ShellParser()
            language_parser.feed(
                f'<script language="{live_language}" '
                'src="/ActorPlus/assets/birthage.js?'
                f'rkv={generation}"></script>'
            )
            language_attribution, _, _ = analyze_lib.attribute_shell_assets(
                language_parser.assets,
                {"actor-plus-jf10": current_requirement},
                generation,
            )
            require(
                language_attribution["actor-plus-jf10"]["tagCount"] == 1,
                f"live legacy script language was ignored: {live_language}",
            )
        explicit_empty_type_parser = analyze_lib.ShellParser()
        explicit_empty_type_parser.feed(
            '<script type="" language="vbscript" '
            'src="/ActorPlus/assets/birthage.js?'
            f'rkv={generation}"></script>'
        )
        explicit_empty_type_attribution, _, _ = (
            analyze_lib.attribute_shell_assets(
                explicit_empty_type_parser.assets,
                {"actor-plus-jf10": current_requirement},
                generation,
            )
        )
        require(
            explicit_empty_type_attribution["actor-plus-jf10"]["tagCount"] == 1,
            "explicit empty script type did not override an inert language value",
        )
        inert_link_parser = analyze_lib.ShellParser()
        inert_link_parser.feed(
            '<link rel="stylesheet" type="application/json" '
            'href="/ActorPlus/assets/birthage.css?'
            f'rkv={generation}">'
        )
        inert_link_attribution, _, _ = analyze_lib.attribute_shell_assets(
            inert_link_parser.assets,
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        live_link_parser = analyze_lib.ShellParser()
        live_link_parser.feed(
            '<link rel="stylesheet" type="text/css; charset=utf-8" '
            'href="/ActorPlus/assets/birthage.css?'
            f'rkv={generation}">'
        )
        live_link_attribution, _, _ = analyze_lib.attribute_shell_assets(
            live_link_parser.assets,
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        require(
            inert_link_attribution["actor-plus-jf10"]["tagCount"] == 0
            and live_link_attribution["actor-plus-jf10"]["tagCount"] == 1,
            "stylesheet MIME type did not distinguish inert JSON from live CSS",
        )
        stale_assets = json.loads(json.dumps(current_assets))
        stale_assets[1]["url"] = "/ActorPlus/assets/birthage.js?rkv=g-ffffffffffffffff"
        stale_attribution, _, _ = analyze_lib.attribute_shell_assets(
            stale_assets, {"actor-plus-jf10": current_requirement}, generation
        )
        require(
            not all(
                analyze_lib.evaluate_shell_requirement(
                    stale_attribution["actor-plus-jf10"], current_requirement
                ).values()
            ),
            "one current plus one stale rkv tag was accepted",
        )

        source_requirement = {
            "mode": "source-versioned",
            "cardinality": 1,
            "selectors": [
                {
                    "tag": "script",
                    "origin": "same-origin",
                    "path": "/Ratings/ratings.js",
                    "query": {
                        "requiredKeys": ["v"],
                        "allowedKeys": ["v"],
                        "equals": {},
                    },
                    "cardinality": 1,
                }
            ],
        }
        source_attribution, _, source_errors = analyze_lib.attribute_shell_assets(
            [{"tag": "script", "url": "/Ratings/ratings.js?v=1.0.374.0"}],
            {"ratings-jf10": source_requirement},
            generation,
        )
        require(
            not source_errors
            and all(
                analyze_lib.evaluate_shell_requirement(
                    source_attribution["ratings-jf10"], source_requirement
                ).values()
            ),
            "valid same-origin nonempty source version was rejected",
        )
        for bad_source_url in (
            "/Ratings/ratings.js?v=",
            f"/Ratings/ratings.js?v=1.0.374.0&rkv={generation}",
            "https://cdn.example/Ratings/ratings.js?v=1.0.374.0",
        ):
            bad_attribution, _, _ = analyze_lib.attribute_shell_assets(
                [{"tag": "script", "url": bad_source_url}],
                {"ratings-jf10": source_requirement},
                generation,
            )
            require(
                not all(
                    analyze_lib.evaluate_shell_requirement(
                        bad_attribution["ratings-jf10"], source_requirement
                    ).values()
                ),
                f"invalid source-versioned URL was accepted: {bad_source_url}",
            )

        assembly_versioned_requirement = {
            "mode": "assembly-versioned-path",
            "cardinality": 1,
            "selectors": [
                {
                    "tag": "script",
                    "origin": "same-origin",
                    "path": (
                        "/_/9ad69ad4aa6db94bb91ba372701f75f8/"
                        "59b81a2dc57f3e32ef95fdacfd4cf666.js"
                    ),
                    "query": empty_query,
                    "cardinality": 1,
                }
            ],
        }
        assembly_versioned_url = (
            "/_/9ad69ad4aa6db94bb91ba372701f75f8/"
            "59b81a2dc57f3e32ef95fdacfd4cf666.js"
        )
        assembly_attribution, _, assembly_errors = (
            analyze_lib.attribute_shell_assets(
                [{"tag": "script", "url": assembly_versioned_url}],
                {"powertoys-thumbnail-previews-jf10": assembly_versioned_requirement},
                generation,
            )
        )
        require(
            not assembly_errors
            and all(
                analyze_lib.evaluate_shell_requirement(
                    assembly_attribution["powertoys-thumbnail-previews-jf10"],
                    assembly_versioned_requirement,
                ).values()
            ),
            "valid assembly-versioned embedded-resource path was rejected",
        )
        for invalid_assembly_url in (
            f"{assembly_versioned_url}?rkv={generation}",
            "/_/9ad69ad4aa6db94bb91ba372701f75f8/thumbnail-previews.js",
            "https://cdn.example.invalid" + assembly_versioned_url,
        ):
            invalid_attribution, _, _ = analyze_lib.attribute_shell_assets(
                [{"tag": "script", "url": invalid_assembly_url}],
                {"powertoys-thumbnail-previews-jf10": assembly_versioned_requirement},
                generation,
            )
            require(
                not all(
                    analyze_lib.evaluate_shell_requirement(
                        invalid_attribution["powertoys-thumbnail-previews-jf10"],
                        assembly_versioned_requirement,
                    ).values()
                ),
                f"invalid assembly-versioned URL was accepted: {invalid_assembly_url}",
            )

        external_requirement = {
            "mode": "external-present",
            "cardinality": 1,
            "selectors": [
                {
                    "tag": "script",
                    "origin": "https://cdn.jsdelivr.net",
                    "path": "/gh/example/plugin@main/client.js",
                    "query": empty_query,
                    "cardinality": 1,
                }
            ],
        }
        external_attribution, _, _ = analyze_lib.attribute_shell_assets(
            [
                {
                    "tag": "script",
                    "url": "https://cdn.jsdelivr.net/gh/example/plugin@main/client.js",
                }
            ],
            {"external": external_requirement},
            generation,
        )
        require(
            all(
                analyze_lib.evaluate_shell_requirement(
                    external_attribution["external"], external_requirement
                ).values()
            ),
            "valid exact external asset was rejected",
        )

        decoy_attribution, _, _ = analyze_lib.attribute_shell_assets(
            [
                {
                    "tag": "script",
                    "url": f"/decoy.js?next=/ActorPlus/assets/birthage.js&rkv={generation}",
                }
            ],
            {"actor-plus-jf10": current_requirement},
            generation,
        )
        require(
            decoy_attribution["actor-plus-jf10"]["tagCount"] == 0,
            "query-substring decoy was attributed as an asset path",
        )
        for wrong_case_url in (
            f"/actorplus/assets/birthage.js?rkv={generation}",
            f"/ActorPlus/assets/birthage.js?RKV={generation}",
        ):
            wrong_case_attribution, _, _ = analyze_lib.attribute_shell_assets(
                [{"tag": "script", "url": wrong_case_url}],
                {"actor-plus-jf10": current_requirement},
                generation,
            )
            require(
                wrong_case_attribution["actor-plus-jf10"]["tagCount"] == 0,
                f"non-exact path/query case was attributed: {wrong_case_url}",
            )

        absent_requirement = {
            "mode": "absent",
            "cardinality": 0,
            "selectors": [
                {
                    "tag": "script",
                    "origin": "same-origin",
                    "path": "/StreamLimit/inject.js",
                    "query": {
                        "requiredKeys": [],
                        "allowedKeys": ["rkv"],
                        "equals": {},
                    },
                    "cardinality": 0,
                }
            ],
        }
        absent_attribution, _, _ = analyze_lib.attribute_shell_assets(
            [{"tag": "script", "url": "/StreamLimit/inject.js?unexpected=1"}],
            {"stream-limit-jf10": absent_requirement},
            generation,
        )
        require(
            absent_attribution["stream-limit-jf10"]["tagCount"] == 1
            and not all(
                analyze_lib.evaluate_shell_requirement(
                    absent_attribution["stream-limit-jf10"], absent_requirement
                ).values()
            ),
            "unexpected query key hid an expected-absent artifact",
        )
        absent_fragment_attribution, _, _ = analyze_lib.attribute_shell_assets(
            [{"tag": "script", "url": "/StreamLimit/inject.js#decoy"}],
            {"stream-limit-jf10": absent_requirement},
            generation,
        )
        require(
            absent_fragment_attribution["stream-limit-jf10"]["tagCount"] == 1
            and not all(
                analyze_lib.evaluate_shell_requirement(
                    absent_fragment_attribution["stream-limit-jf10"],
                    absent_requirement,
                ).values()
            ),
            "a URL fragment hid an expected-absent resource identity",
        )

        absent_whisper = {
            "mode": "absent",
            "cardinality": 0,
            "selectors": [
                {
                    "tag": "script",
                    "origin": "same-origin",
                    "path": "/web/configurationpage",
                    "query": {
                        "requiredKeys": ["name"],
                        "allowedKeys": ["name", "rkv"],
                        "equals": {"name": "whisperSubs.js"},
                    },
                    "cardinality": 0,
                }
            ],
        }
        whisper_decoy, _, _ = analyze_lib.attribute_shell_assets(
            [
                {
                    "tag": "script",
                    "url": "/web/configurationpage?name=anotherPlugin.js&unexpected=1",
                }
            ],
            {"whisper-subs-jf10": absent_whisper},
            generation,
        )
        whisper_present, _, _ = analyze_lib.attribute_shell_assets(
            [
                {
                    "tag": "script",
                    "url": "/web/configurationpage?name=whisperSubs.js&unexpected=1",
                }
            ],
            {"whisper-subs-jf10": absent_whisper},
            generation,
        )
        require(
            whisper_decoy["whisper-subs-jf10"]["tagCount"] == 0
            and whisper_present["whisper-subs-jf10"]["tagCount"] == 1,
            "expected-absent query identity was not enforced precisely",
        )
        ambiguous_requirement = {
            "mode": "external-present",
            "cardinality": 1,
            "selectors": [external_requirement["selectors"][0]],
        }
        _, _, ambiguous_errors = analyze_lib.attribute_shell_assets(
            [
                {
                    "tag": "script",
                    "url": "https://cdn.jsdelivr.net/gh/example/plugin@main/client.js",
                }
            ],
            {"one": external_requirement, "two": ambiguous_requirement},
            generation,
        )
        require(ambiguous_errors, "cross-artifact ambiguous selector match was accepted")
        duplicate_selector_requirement = json.loads(json.dumps(external_requirement))
        duplicate_selector_requirement["selectors"].append(
            json.loads(json.dumps(external_requirement["selectors"][0]))
        )
        duplicate_selector_requirement["cardinality"] = 2
        _, _, multiple_errors = analyze_lib.attribute_shell_assets(
            [
                {
                    "tag": "script",
                    "url": "https://cdn.jsdelivr.net/gh/example/plugin@main/client.js",
                }
            ],
            {"one": duplicate_selector_requirement},
            generation,
        )
        require(multiple_errors, "one tag matching multiple selectors was accepted")

        disk_before = b"<html><head></head><body></body></html>"
        disk_after = b"<html><head></head><body><script src='/x.js'></script></body></html>"
        added_disk = analyze_lib.evaluate_webroot_disk(
            disk_before,
            disk_after,
            {"writer": {"mode": "added", "cardinality": 1, "markers": ["/x.js"]}},
        )
        absent_disk = analyze_lib.evaluate_webroot_disk(
            disk_before,
            disk_before,
            {"writer": {"mode": "absent", "cardinality": 0, "markers": ["/x.js"]}},
        )
        require(
            added_disk["allPassed"] and absent_disk["allPassed"],
            "valid direct-webroot before/after evidence was rejected",
        )
        require(
            not analyze_lib.evaluate_webroot_disk(
                disk_before,
                disk_after,
                {"writer": {"mode": "absent", "cardinality": 0, "markers": ["/x.js"]}},
            )["allPassed"],
            "changed/marked read-only webroot evidence was accepted",
        )

        negative_catalog_checks: list[str] = []
        with tempfile.TemporaryDirectory(prefix="rk-compat-lock-negative-") as temporary:
            negative_root = Path(temporary)

            def require_rejected(label: str, mutated: dict[str, object]) -> None:
                path = negative_root / f"{label}.json"
                artifact_lib.write_json(path, mutated)
                try:
                    artifact_lib.validate_lock(path)
                except artifact_lib.HarnessError:
                    negative_catalog_checks.append(label)
                else:
                    raise artifact_lib.HarnessError(
                        f"negative catalog mutation was accepted: {label}"
                    )

            missing_row = json.loads(json.dumps(lock))
            missing_row["catalogCoverage"].pop()
            require_rejected("missing-authoritative-row", missing_row)

            duplicated_row = json.loads(json.dumps(lock))
            duplicated_row["catalogCoverage"][-1] = json.loads(
                json.dumps(duplicated_row["catalogCoverage"][0])
            )
            require_rejected("duplicated-authoritative-row", duplicated_row)

            uncovered_testable = json.loads(json.dumps(lock))
            intro = next(
                row
                for row in uncovered_testable["catalogCoverage"]
                if row["name"] == "intro-skipper"
            )
            intro["artifacts"] = []
            require_rejected("testable-row-without-artifact", uncovered_testable)

            writable_protected_upstream = json.loads(json.dumps(lock))
            enhanced = next(
                artifact
                for artifact in writable_protected_upstream["artifacts"]
                if artifact["id"] == "jellyfin-enhanced-jf10"
            )
            enhanced.pop("repositoryAccess")
            require_rejected(
                "protected-upstream-without-readonly-marker",
                writable_protected_upstream,
            )

        negative_manifest_checks: list[str] = []
        with tempfile.TemporaryDirectory(prefix="rk-compat-manifest-negative-") as temporary:
            negative_root = Path(temporary)

            def require_manifest_rejected(
                label: str,
                mutated: dict[str, object],
                expected_error: str | None = None,
            ) -> None:
                path = negative_root / f"{label}.json"
                artifact_lib.write_json(path, mutated)
                try:
                    manifest_lib.load_and_validate(
                        COMPAT_ROOT / "ecosystem.lock.json", path
                    )
                except artifact_lib.HarnessError as error:
                    if expected_error is not None and expected_error not in str(error):
                        raise artifact_lib.HarnessError(
                            f"negative manifest mutation {label} failed for the wrong "
                            f"reason: {error}"
                        ) from error
                    negative_manifest_checks.append(label)
                else:
                    raise artifact_lib.HarnessError(
                        f"negative manifest mutation was accepted: {label}"
                    )

            def fresh_manifest() -> dict[str, object]:
                return json.loads(json.dumps(matrices))

            unknown_root = fresh_manifest()
            unknown_root["unknown"] = True
            require_manifest_rejected("unknown-root-field", unknown_root)

            unknown_runtime = fresh_manifest()
            unknown_runtime["runtimes"]["jf10"]["unknown"] = True
            require_manifest_rejected("unknown-runtime-field", unknown_runtime)

            changed_server_version = fresh_manifest()
            changed_server_version["runtimes"]["jf10"][
                "serverVersion"
            ] = "10.11.11.1"
            require_manifest_rejected(
                "exact-runtime-server-version", changed_server_version
            )

            changed_server_regex = fresh_manifest()
            changed_server_regex["runtimes"]["jf10"][
                "serverVersionRegex"
            ] = "^.*$"
            require_manifest_rejected(
                "exact-runtime-server-version-regex", changed_server_regex
            )

            changed_target_abi = fresh_manifest()
            changed_target_abi["runtimes"]["jf10"][
                "refreshKitTargetAbi"
            ] = "10.12.0.0"
            require_manifest_rejected(
                "exact-runtime-refresh-kit-target-abi", changed_target_abi
            )

            unknown_matrix = fresh_manifest()
            unknown_matrix["matrices"][0]["unknown"] = True
            require_manifest_rejected("unknown-matrix-field", unknown_matrix)

            blank_purpose = fresh_manifest()
            blank_purpose["matrices"][0]["purpose"] = "   "
            require_manifest_rejected("nonempty-matrix-purpose", blank_purpose)

            changed_purpose = fresh_manifest()
            changed_purpose["matrices"][0]["purpose"] = (
                "Invented but nonempty matrix purpose."
            )
            require_manifest_rejected("exact-matrix-purpose", changed_purpose)

            invented_quarantine = fresh_manifest()
            invented_quarantine["matrices"][0]["quarantinedAssertions"].append(
                "Invented unaudited limitation."
            )
            require_manifest_rejected(
                "exact-quarantined-assertions", invented_quarantine
            )

            unknown_shell = fresh_manifest()
            unknown_shell["matrices"][0]["shellRequirements"]["plugin-pages-jf10"][
                "unknown"
            ] = True
            require_manifest_rejected("unknown-shell-requirement-field", unknown_shell)

            unknown_selector = fresh_manifest()
            unknown_selector["matrices"][0]["shellRequirements"]["plugin-pages-jf10"][
                "selectors"
            ][0]["unknown"] = True
            require_manifest_rejected("unknown-selector-field", unknown_selector)

            unknown_query = fresh_manifest()
            unknown_query["matrices"][0]["shellRequirements"]["plugin-pages-jf10"][
                "selectors"
            ][0]["query"]["unknown"] = True
            require_manifest_rejected("unknown-query-field", unknown_query)

            unknown_inline = fresh_manifest()
            unknown_inline["matrices"][0]["inlineRequirements"]["custom-tabs-jf10"][
                "unknown"
            ] = True
            require_manifest_rejected("unknown-inline-field", unknown_inline)

            unknown_disk = fresh_manifest()
            unknown_disk["matrices"][-1]["webrootDiskRequirements"][
                "stream-limit-jf10"
            ]["unknown"] = True
            require_manifest_rejected("unknown-disk-field", unknown_disk)

            unknown_config = fresh_manifest()
            broker = next(
                row
                for row in unknown_config["matrices"]
                if row["id"] == "jf10-registration-broker"
            )
            broker["configurationPatches"][0]["unknown"] = True
            require_manifest_rejected("unknown-configuration-field", unknown_config)

            unknown_probe = fresh_manifest()
            broker = next(
                row
                for row in unknown_probe["matrices"]
                if row["id"] == "jf10-registration-broker"
            )
            broker["contentProbes"][0]["unknown"] = True
            require_manifest_rejected("unknown-content-probe-field", unknown_probe)

            refresh_kit_locations = {
                "refresh-kit-shell-list": lambda row: row.setdefault(
                    "requiredPresentArtifacts", []
                ).append("@refresh-kit"),
                "refresh-kit-body": lambda row: row.setdefault(
                    "requiredBodyMarkers", {}
                ).update({"@refresh-kit": ["decoy"]}),
                "refresh-kit-config": lambda row: row.setdefault(
                    "configurationPatches", []
                ).append({"artifactId": "@refresh-kit", "payload": {"x": True}}),
                "refresh-kit-content": lambda row: row["contentProbes"][0][
                    "markers"
                ].update({"@refresh-kit": ["decoy"]}),
                "refresh-kit-structured-shell": lambda row: row.setdefault(
                    "shellRequirements", {}
                ).update(
                    {
                        "@refresh-kit": {
                            "mode": "absent",
                            "cardinality": 0,
                            "selectors": [
                                {
                                    "tag": "script",
                                    "origin": "same-origin",
                                    "path": "/RefreshKit/kit.js",
                                    "query": {
                                        "requiredKeys": [],
                                        "allowedKeys": ["v"],
                                        "equals": {},
                                    },
                                    "cardinality": 0,
                                }
                            ],
                        }
                    }
                ),
            }
            for label, mutate in refresh_kit_locations.items():
                mutated = fresh_manifest()
                target = (
                    next(
                        row
                        for row in mutated["matrices"]
                        if row["id"] == "jf10-registration-broker"
                    )
                    if label == "refresh-kit-content"
                    else mutated["matrices"][0]
                )
                mutate(target)
                require_manifest_rejected(label, mutated)

            refresh_inline = fresh_manifest()
            refresh_inline["matrices"][0]["inlineRequirements"]["@refresh-kit"] = {
                "cardinality": 1,
                "markers": ["decoy"],
                "ordered": False,
            }
            require_manifest_rejected("refresh-kit-inline", refresh_inline)

            refresh_disk = fresh_manifest()
            refresh_disk["matrices"][-1]["webrootDiskRequirements"]["@refresh-kit"] = {
                "mode": "added",
                "cardinality": 1,
                "markers": ["decoy"],
            }
            require_manifest_rejected("refresh-kit-disk", refresh_disk)

            refresh_probe = fresh_manifest()
            refresh_probe["matrices"][0]["generationProbe"] = "@refresh-kit"
            require_manifest_rejected("refresh-kit-generation-probe", refresh_probe)

            for index, matrix in enumerate(matrices["matrices"]):
                matrix_id = matrix["id"]
                changed_order = fresh_manifest()
                order = changed_order["matrices"][index]["installOrder"]
                order[0], order[1] = order[1], order[0]
                require_manifest_rejected(f"exact-order-{matrix_id}", changed_order)

                changed_pair = fresh_manifest()
                changed_pair["matrices"][index]["orderPair"] = "mutated-pair"
                require_manifest_rejected(f"exact-order-pair-{matrix_id}", changed_pair)

                if matrix.get("expectedRuntimePluginOrder") is not None:
                    changed_runtime_order = fresh_manifest()
                    runtime_order = changed_runtime_order["matrices"][index][
                        "expectedRuntimePluginOrder"
                    ]
                    runtime_order[0], runtime_order[1] = (
                        runtime_order[1],
                        runtime_order[0],
                    )
                    require_manifest_rejected(
                        f"exact-runtime-order-{matrix_id}", changed_runtime_order
                    )

                changed_cache = fresh_manifest()
                current_cache = changed_cache["matrices"][index]["cacheExpectation"]
                changed_cache["matrices"][index]["cacheExpectation"] = (
                    "observe" if current_cache != "observe" else "required"
                )
                require_manifest_rejected(f"exact-cache-{matrix_id}", changed_cache)

                changed_stamping = fresh_manifest()
                current_stamping = changed_stamping["matrices"][index][
                    "stampingExpectation"
                ]
                changed_stamping["matrices"][index]["stampingExpectation"] = (
                    "observe" if current_stamping == "required" else "required"
                )
                require_manifest_rejected(
                    f"exact-stamping-{matrix_id}", changed_stamping
                )

                changed_probe = fresh_manifest()
                installed = [
                    artifact_id
                    for artifact_id in changed_probe["matrices"][index]["installOrder"]
                    if artifact_id != "@refresh-kit"
                ]
                current_probe = changed_probe["matrices"][index]["generationProbe"]
                changed_probe["matrices"][index]["generationProbe"] = next(
                    artifact_id
                    for artifact_id in installed
                    if artifact_id != current_probe
                )
                require_manifest_rejected(
                    f"exact-generation-probe-{matrix_id}", changed_probe
                )

            changed_id = fresh_manifest()
            changed_id["matrices"][0]["id"] = "changed-id"
            require_manifest_rejected("exact-matrix-id-sequence", changed_id)

            changed_body = fresh_manifest()
            core = changed_body["matrices"][0]
            core["requiredBodyMarkers"]["custom-tabs-jf10"][0] += " changed"
            core["inlineRequirements"]["custom-tabs-jf10"]["markers"][0] += " changed"
            require_manifest_rejected("exact-body-contract", changed_body)

            changed_assembly_versioned = fresh_manifest()
            response_forward = next(
                row
                for row in changed_assembly_versioned["matrices"]
                if row["id"] == "jf10-response-transformers-forward"
            )
            response_forward["requiredAssemblyVersionedArtifacts"].pop()
            require_manifest_rejected(
                "exact-assembly-versioned-contract", changed_assembly_versioned
            )

            changed_config = fresh_manifest()
            broker = next(
                row
                for row in changed_config["matrices"]
                if row["id"] == "jf10-registration-broker"
            )
            broker["configurationPatches"][0]["payload"][
                "EnableJavaScriptInjection"
            ] = False
            require_manifest_rejected("exact-configuration-contract", changed_config)

            changed_content = fresh_manifest()
            broker = next(
                row
                for row in changed_content["matrices"]
                if row["id"] == "jf10-registration-broker"
            )
            broker["contentProbes"][0]["markers"]["media-preview-jf10"][0][
                "value"
            ] += "x"
            require_manifest_rejected("exact-content-contract", changed_content)

            content_mutations: dict[str, tuple[object, str]] = {}

            missing_request = fresh_manifest()
            next(
                row for row in missing_request["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0].pop("request")
            content_mutations["content-request-required"] = (
                missing_request, "content probe request must be an object"
            )

            numeric_probe_id = fresh_manifest()
            next(
                row for row in numeric_probe_id["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]["id"] = 1
            content_mutations["content-id-must-be-string"] = (
                numeric_probe_id, "unsafe content probe id"
            )

            too_many_probes = fresh_manifest()
            too_many_matrix = next(
                row for row in too_many_probes["matrices"]
                if row["id"] == "jf10-registration-broker"
            )
            too_many_matrix["contentProbes"] = [
                json.loads(json.dumps(too_many_matrix["contentProbes"][0]))
                for _ in range(65)
            ]
            content_mutations["content-probe-count-over-limit"] = (
                too_many_probes, "content probe count exceeds 64"
            )

            long_probe_id = fresh_manifest()
            next(
                row for row in long_probe_id["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]["id"] = "a" * 65
            content_mutations["content-id-over-limit"] = (
                long_probe_id, "content probe id exceeds 64 characters"
            )

            long_path = fresh_manifest()
            next(
                row for row in long_path["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]["path"] = "/" + ("a" * 2048)
            content_mutations["content-path-over-limit"] = (
                long_path, "content probe path exceeds 2048 characters"
            )

            non_ascii_path = fresh_manifest()
            next(
                row for row in non_ascii_path["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]["path"] = "/Fixture/é.js"
            content_mutations["content-path-non-ascii"] = (
                non_ascii_path, "content probe path must be ASCII"
            )

            long_accept = fresh_manifest()
            long_accept_probe = next(
                row for row in long_accept["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]
            long_accept_value = "application/" + ("a" * 117)
            long_accept_probe["request"]["accept"] = long_accept_value
            long_accept_probe["response"]["mediaType"] = long_accept_value
            content_mutations["content-accept-over-limit"] = (
                long_accept, "content probe Accept exceeds 128 characters"
            )

            empty_json_on_text = fresh_manifest()
            next(
                row for row in empty_json_on_text["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]["jsonArrayContains"] = {}
            content_mutations["content-text-rejects-empty-json-contract"] = (
                empty_json_on_text, "invalid JSON array content contract"
            )

            wrong_accept = fresh_manifest()
            next(
                row for row in wrong_accept["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]["request"]["accept"] = "text/javascript"
            content_mutations["content-accept-media-mismatch"] = (
                wrong_accept, "Accept and response media type must match"
            )

            wrong_status = fresh_manifest()
            next(
                row for row in wrong_status["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]["response"]["status"] = 204
            content_mutations["content-status-not-200"] = (
                wrong_status, "content probe status must be exactly 200"
            )

            semantic_unbounded = fresh_manifest()
            next(
                row for row in semantic_unbounded["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]["response"]["body"]["maxBytes"] = 1048577
            content_mutations["content-semantic-over-hard-cap"] = (
                semantic_unbounded, "invalid bounded semantic body contract"
            )

            exact_unbounded = fresh_manifest()
            next(
                row for row in exact_unbounded["matrices"]
                if row["id"] == "jf10-response-transformers-forward"
            )["contentProbes"][0]["response"]["body"]["bytes"] = 1048577
            content_mutations["content-exact-over-hard-cap"] = (
                exact_unbounded, "invalid exact body contract"
            )

            bad_hash = fresh_manifest()
            next(
                row for row in bad_hash["matrices"]
                if row["id"] == "jf10-response-transformers-forward"
            )["contentProbes"][0]["response"]["body"]["sha256"] = "invalid"
            content_mutations["content-exact-invalid-hash"] = (
                bad_hash, "invalid exact body contract"
            )

            wrong_cardinality = fresh_manifest()
            next(
                row for row in wrong_cardinality["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]["markers"]["media-preview-jf10"][0][
                "cardinality"
            ] = 2
            content_mutations["content-marker-cardinality-not-exact"] = (
                wrong_cardinality, "invalid content markers"
            )

            duplicate_owner = fresh_manifest()
            duplicate_probe = next(
                row for row in duplicate_owner["matrices"]
                if row["id"] == "jf10-registration-broker"
            )["contentProbes"][0]
            duplicate_probe["markers"]["gelato-jf10"].append(
                json.loads(json.dumps(
                    duplicate_probe["markers"]["media-preview-jf10"][0]
                ))
            )
            content_mutations["content-marker-value-multiple-owners"] = (
                duplicate_owner, "multiple artifact owners"
            )

            wrong_json_cardinality = fresh_manifest()
            next(
                probe
                for row in wrong_json_cardinality["matrices"]
                if row["id"] == "jf10-response-transformers-forward"
                for probe in row["contentProbes"]
                if probe["id"] == "powertoys-remote-trailers-config"
            )["jsonArrayContains"]["plugins"][0]["cardinality"] = 2
            content_mutations["content-json-cardinality-not-exact"] = (
                wrong_json_cardinality, "invalid JSON array content contract"
            )

            def json_probe(manifest: dict[str, Any]) -> dict[str, Any]:
                return next(
                    probe
                    for row in manifest["matrices"]
                    if row["id"] == "jf10-response-transformers-forward"
                    for probe in row["contentProbes"]
                    if probe["id"] == "powertoys-remote-trailers-config"
                )

            json_map_over_limit = fresh_manifest()
            json_probe(json_map_over_limit)["jsonArrayContains"] = {
                f"key-{index}": [
                    {"value": f"value-{index}", "cardinality": 1}
                ]
                for index in range(65)
            }
            content_mutations["content-json-map-over-limit"] = (
                json_map_over_limit, "JSON array map exceeds 64 entries"
            )

            json_key_over_limit = fresh_manifest()
            contract = json_probe(json_key_over_limit)["jsonArrayContains"]
            contract["k" * 129] = contract.pop("plugins")
            content_mutations["content-json-key-over-limit"] = (
                json_key_over_limit, "JSON array key exceeds 128 characters"
            )

            json_key_control = fresh_manifest()
            contract = json_probe(json_key_control)["jsonArrayContains"]
            contract["plugins\n"] = contract.pop("plugins")
            content_mutations["content-json-key-control"] = (
                json_key_control, "JSON array key contains control characters"
            )

            json_list_over_limit = fresh_manifest()
            json_probe(json_list_over_limit)["jsonArrayContains"]["plugins"] = [
                {"value": f"value-{index}", "cardinality": 1}
                for index in range(65)
            ]
            content_mutations["content-json-list-over-limit"] = (
                json_list_over_limit,
                "JSON array requirement list exceeds 64 entries",
            )

            json_value_over_limit = fresh_manifest()
            json_probe(json_value_over_limit)["jsonArrayContains"]["plugins"][0][
                "value"
            ] = "v" * 4097
            content_mutations["content-json-value-over-limit"] = (
                json_value_over_limit, "JSON array value exceeds 4096 characters"
            )

            json_value_control = fresh_manifest()
            json_probe(json_value_control)["jsonArrayContains"]["plugins"][0][
                "value"
            ] = "value\u007f"
            content_mutations["content-json-value-control"] = (
                json_value_control,
                "JSON array value contains control characters",
            )

            def text_probe(manifest: dict[str, Any]) -> dict[str, Any]:
                return next(
                    row for row in manifest["matrices"]
                    if row["id"] == "jf10-registration-broker"
                )["contentProbes"][0]

            marker_map_over_limit = fresh_manifest()
            text_probe(marker_map_over_limit)["markers"] = {
                f"owner-{index}": [
                    {"value": f"value-{index}", "cardinality": 1}
                ]
                for index in range(65)
            }
            content_mutations["content-marker-map-over-limit"] = (
                marker_map_over_limit,
                "content marker owner map exceeds 64 entries",
            )

            marker_owner_over_limit = fresh_manifest()
            markers = text_probe(marker_owner_over_limit)["markers"]
            first_owner = next(iter(markers))
            markers["o" * 129] = markers.pop(first_owner)
            content_mutations["content-marker-owner-over-limit"] = (
                marker_owner_over_limit,
                "content marker owner exceeds 128 characters",
            )

            marker_owner_control = fresh_manifest()
            markers = text_probe(marker_owner_control)["markers"]
            first_owner = next(iter(markers))
            markers[f"{first_owner}\n"] = markers.pop(first_owner)
            content_mutations["content-marker-owner-control"] = (
                marker_owner_control,
                "content marker owner contains control characters",
            )

            marker_list_over_limit = fresh_manifest()
            marker_requirements = next(
                iter(text_probe(marker_list_over_limit)["markers"].values())
            )
            marker_requirements[:] = [
                {"value": f"value-{index}", "cardinality": 1}
                for index in range(65)
            ]
            content_mutations["content-marker-list-over-limit"] = (
                marker_list_over_limit,
                "content marker requirement list exceeds 64 entries",
            )

            marker_value_over_limit = fresh_manifest()
            next(iter(text_probe(marker_value_over_limit)["markers"].values()))[
                0
            ]["value"] = "v" * 4097
            content_mutations["content-marker-value-over-limit"] = (
                marker_value_over_limit,
                "content marker value exceeds 4096 characters",
            )

            marker_value_control = fresh_manifest()
            next(iter(text_probe(marker_value_control)["markers"].values()))[
                0
            ]["value"] = "value\n"
            content_mutations["content-marker-value-control"] = (
                marker_value_control,
                "content marker value contains control characters",
            )

            total_requirements_over_limit = fresh_manifest()
            total_probe = json_probe(total_requirements_over_limit)
            total_probe["jsonArrayContains"]["plugins"] = [
                {"value": f"json-value-{index}", "cardinality": 1}
                for index in range(32)
            ]
            owner = next(iter(total_probe["markers"]))
            total_probe["markers"][owner] = [
                {"value": f"marker-value-{index}", "cardinality": 1}
                for index in range(33)
            ]
            content_mutations["content-total-requirements-over-limit"] = (
                total_requirements_over_limit,
                "content probe total requirements exceeds 64",
            )

            missing_assembly_probe = fresh_manifest()
            next(
                row for row in missing_assembly_probe["matrices"]
                if row["id"] == "jf10-response-transformers-forward"
            )["contentProbes"].pop(0)
            content_mutations["content-assembly-selector-missing-probe"] = (
                missing_assembly_probe, "map one-to-one"
            )

            for label, unsafe_path in {
                "backslash": "/_/bad\\asset.css",
                "double-slash": "/_/bad//asset.css",
                "dot-segment": "/_/bad/../asset.css",
                "encoded": "/_/bad/%2e%2e/asset.css",
                "control": "/_/bad/asset.css\u0000",
            }.items():
                changed_path = fresh_manifest()
                next(
                    row for row in changed_path["matrices"]
                    if row["id"] == "jf10-registration-broker"
                )["contentProbes"][0]["path"] = unsafe_path
                content_mutations[f"content-path-{label}"] = (
                    changed_path, "invalid content probe path"
                )

            for label, (mutation, expected_error) in content_mutations.items():
                require_manifest_rejected(label, mutation, expected_error)

            invalid_content_format = fresh_manifest()
            response = next(
                row
                for row in invalid_content_format["matrices"]
                if row["id"] == "jf10-response-transformers-forward"
            )
            response["contentProbes"][1]["format"] = "javascript"
            require_manifest_rejected(
                "invalid-content-format", invalid_content_format
            )

            changed_selector = fresh_manifest()
            changed_selector["matrices"][0]["shellRequirements"]["plugin-pages-jf10"][
                "selectors"
            ][0]["path"] = "/PluginPages/changed.js"
            require_manifest_rejected("exact-selector-path", changed_selector)

            changed_query = fresh_manifest()
            changed_query["matrices"][0]["shellRequirements"][
                "home-screen-sections-jf10"
            ]["selectors"][0]["query"]["equals"]["v"] = "changed"
            require_manifest_rejected("exact-selector-query", changed_query)

            changed_origin = fresh_manifest()
            changed_origin["matrices"][0]["shellRequirements"]["media-bar-jf10"][
                "selectors"
            ][0]["origin"] = "https://example.invalid"
            require_manifest_rejected("exact-selector-origin", changed_origin)

            changed_cardinality = fresh_manifest()
            requirement = changed_cardinality["matrices"][0]["shellRequirements"][
                "plugin-pages-jf10"
            ]
            requirement["cardinality"] = 2
            requirement["selectors"][0]["cardinality"] = 2
            require_manifest_rejected("exact-artifact-cardinality", changed_cardinality)

            changed_disk_marker = fresh_manifest()
            changed_disk_marker["matrices"][-1]["webrootDiskRequirements"][
                "stream-limit-jf10"
            ]["markers"][0] += " changed"
            require_manifest_rejected("exact-disk-contract", changed_disk_marker)

            duplicate_selector = fresh_manifest()
            requirement = duplicate_selector["matrices"][0]["shellRequirements"][
                "plugin-pages-jf10"
            ]
            requirement["selectors"].append(
                json.loads(json.dumps(requirement["selectors"][0]))
            )
            requirement["cardinality"] = 2
            require_manifest_rejected("duplicate-selector", duplicate_selector)

            cross_artifact_selector = fresh_manifest()
            core = cross_artifact_selector["matrices"][0]
            core["shellRequirements"]["custom-tabs-jf10"] = json.loads(
                json.dumps(core["shellRequirements"]["media-bar-jf10"])
            )
            core["requiredPresentArtifacts"].append("custom-tabs-jf10")
            require_manifest_rejected(
                "cross-artifact-ambiguous-selector", cross_artifact_selector
            )

        with tempfile.TemporaryDirectory(prefix="rk-compat-static-") as temporary:
            aggregate_root = Path(temporary)
            all_locked_report = synthetic_verification_report(lock["artifacts"])
            artifact_lib.write_json(
                aggregate_root / "all-locked-verification.json",
                all_locked_report,
            )
            for matrix in matrices["matrices"]:
                matrix_dir = aggregate_root / matrix["id"]
                matrix_dir.mkdir()
                limited = bool(matrix.get("requiredUnversionedOuterArtifacts"))
                artifact_lib.write_json(
                    matrix_dir / "result.json",
                    {
                        "matrix": matrix["id"],
                        "cacheExpectation": matrix["cacheExpectation"],
                        "expectedRuntimePluginOrder": matrix.get(
                            "expectedRuntimePluginOrder"
                        ),
                        "observedRuntimePluginOrder": matrix.get(
                            "expectedRuntimePluginOrder"
                        ),
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
            require(
                aggregate_value.get("pairRuntimeOrderChecks")
                == {"jf10-middleware": True, "jf10-response-transformers": True},
                "aggregate did not prove runtime-order invariance for both install pairs",
            )
            require(
                aggregate_value.get("allLockedVerification")
                == {
                    "report": "all-locked-verification.json",
                    "reportSha256": artifact_lib.sha256_path(
                        aggregate_root / "all-locked-verification.json"
                    ),
                    "artifactCount": 44,
                    "archiveBytes": 44,
                    "dispositionCounts": {
                        "testable": 40,
                        "quarantined": 3,
                        "unsupported": 1,
                    },
                    "allPassed": True,
                },
                "aggregate did not bind the exact all-locked verification receipt",
            )
            nonruntime_indexes = [
                index
                for index, row in enumerate(all_locked_report["artifacts"])
                if row["disposition"] != "testable"
            ]
            require(
                len(nonruntime_indexes) == 4,
                "synthetic all-locked report lacks the four nonruntime rows",
            )
            for label in ("missing", "reordered"):
                changed_report = json.loads(json.dumps(all_locked_report))
                changed_rows = changed_report["artifacts"]
                if label == "missing":
                    changed_rows.pop(nonruntime_indexes[0])
                else:
                    left, right = nonruntime_indexes[:2]
                    changed_rows[left], changed_rows[right] = (
                        changed_rows[right],
                        changed_rows[left],
                    )
                artifact_lib.write_json(
                    aggregate_root / "all-locked-verification.json",
                    changed_report,
                )
                require(
                    analyze_lib.cmd_aggregate(aggregate_args) == 1,
                    f"aggregate accepted {label} nonruntime all-locked evidence",
                )
            artifact_lib.write_json(
                aggregate_root / "all-locked-verification.json",
                all_locked_report,
            )
            response_reverse = aggregate_root / "jf10-response-transformers-reverse" / "result.json"
            response_reverse_value = artifact_lib.load_json(response_reverse)
            response_reverse_value["observedRuntimePluginOrder"] = list(
                reversed(response_reverse_value["observedRuntimePluginOrder"])
            )
            artifact_lib.write_json(response_reverse, response_reverse_value)
            require(
                analyze_lib.cmd_aggregate(aggregate_args) == 1,
                "aggregate accepted divergent runtime plugin order within a pair",
            )
            response_reverse_value["observedRuntimePluginOrder"] = (
                response_reverse_value["expectedRuntimePluginOrder"]
            )
            artifact_lib.write_json(response_reverse, response_reverse_value)
            forged_order = list(
                reversed(response_reverse_value["expectedRuntimePluginOrder"])
            )
            response_forward = (
                aggregate_root
                / "jf10-response-transformers-forward"
                / "result.json"
            )
            response_forward_value = artifact_lib.load_json(response_forward)
            for value in (response_forward_value, response_reverse_value):
                value["expectedRuntimePluginOrder"] = forged_order
                value["observedRuntimePluginOrder"] = forged_order
            artifact_lib.write_json(response_forward, response_forward_value)
            artifact_lib.write_json(response_reverse, response_reverse_value)
            require(
                analyze_lib.cmd_aggregate(aggregate_args) == 1,
                "aggregate accepted a consistently forged pair runtime order",
            )
            response_forward_value["expectedRuntimePluginOrder"] = (
                manifest_lib.EXPECTED_RUNTIME_PLUGIN_ORDER[
                    "jf10-response-transformers-forward"
                ]
            )
            response_forward_value["observedRuntimePluginOrder"] = (
                response_forward_value["expectedRuntimePluginOrder"]
            )
            response_reverse_value["expectedRuntimePluginOrder"] = (
                manifest_lib.EXPECTED_RUNTIME_PLUGIN_ORDER[
                    "jf10-response-transformers-reverse"
                ]
            )
            response_reverse_value["observedRuntimePluginOrder"] = (
                response_reverse_value["expectedRuntimePluginOrder"]
            )
            artifact_lib.write_json(response_forward, response_forward_value)
            artifact_lib.write_json(response_reverse, response_reverse_value)
            first_limited = next(iter(manifest_lib.UNVERSIONED_OUTER_ARTIFACTS_BY_MATRIX))
            first_limited_path = aggregate_root / first_limited / "result.json"
            first_limited_value = artifact_lib.load_json(first_limited_path)
            first_limited_value["outcome"] = "pass"
            artifact_lib.write_json(first_limited_path, first_limited_value)
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
            "matrixContractSha256": manifest_lib.EXPECTED_MATRIX_CONTRACT_SHA256,
            "contentProbeCount": len(content_probe_occurrences),
            "contentProbeRawFileCount": len(content_probe_occurrences) * 3,
            "negativeCatalogChecks": negative_catalog_checks,
            "negativeManifestChecks": negative_manifest_checks,
            "safeDegradeCacheMatrices": sorted(safe_degrade_ids),
            "unversionedOuterLimitations": {
                matrix_id: sorted(artifact_ids)
                for matrix_id, artifact_ids in sorted(actual_unversioned_outer.items())
            },
            "requiredAbsentArtifacts": {
                matrix_id: sorted(artifact_ids)
                for matrix_id, artifact_ids in sorted(actual_absent.items())
            },
            "requiredAssemblyVersionedArtifacts": {
                matrix_id: sorted(artifact_ids)
                for matrix_id, artifact_ids in sorted(
                    actual_assembly_versioned.items()
                )
            },
            "requiredExternalArtifacts": {
                matrix_id: sorted(artifact_ids)
                for matrix_id, artifact_ids in sorted(actual_external.items())
            },
            "requiredPreVersionedArtifacts": {
                matrix_id: sorted(artifact_ids)
                for matrix_id, artifact_ids in sorted(actual_preversioned.items())
            },
            "expectedRuntimePluginOrder": {
                matrix_id: list(order)
                for matrix_id, order in sorted(actual_runtime_orders.items())
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
