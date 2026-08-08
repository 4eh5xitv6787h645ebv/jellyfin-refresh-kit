#!/usr/bin/env python3
"""Fail-closed semantic validator for retained host-upgrade evidence."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Any


IMAGES = {
    "jf10": (
        "jellyfin/jellyfin:10.11.10@sha256:f66273e014b307e4ac46778845ebc1e9ee24b2e57c1fc17d5ec5ac3015649bfa",
        "jellyfin/jellyfin:10.11.11@sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db",
        "10.11.10",
        "10.11.11",
        "net9",
    ),
    "jf12": (
        "jellyfin/jellyfin:10.11.11@sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db",
        "jellyfin/jellyfin:12.0-rc4@sha256:db1df1d111c27ba1f10bb8fce6630892f66eb66b12c2b24e79011453ac18b3db",
        "10.11.11",
        "12.0.0",
        "net10",
    ),
}
REQUIRED_PHASES = {
    "one-live-tab-converged",
    "two-tabs-two-contexts-users-converged",
    "ten-live-tabs-role-mix-converged",
    "generation-poll-stress-and-ten-tab-catch-up",
    "in-place-host-upgrade-converged",
}
REQUIRED_ROLES = {
    "admin-dashboard",
    "admin-config-editor",
    "admin-plugin-dialog",
    "admin-background",
    "viewer-home",
    "viewer-background",
    "viewer-playback",
    "anonymous-login",
}
ROLE_ROUTES = {
    "admin-dashboard": r"/dashboard(?:\.html)?(?:[/?]|$)",
    "admin-background": r"/dashboard(?:\.html)?(?:[/?]|$)",
    "admin-config-editor": r"/configurationpage(?:[?]|$)",
    "admin-plugin-dialog": r"/dashboard/plugins(?:[/?]|$)",
    "viewer-home": r"/home(?:\.html)?(?:[/?]|$)",
    "viewer-background": r"/home(?:\.html)?(?:[/?]|$)",
    "viewer-playback": r"/(?:home|details|video)(?:\.html)?(?:[/?]|$)",
    "anonymous-login": r"/login(?:\.html)?(?:[?]|$)",
}
GENERATION = re.compile(r"^g-[0-9a-f]{16}$")
EPOCH = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MD5 = re.compile(r"^[0-9a-f]{32}$")
MEDIA_LIBRARY_NAME = "Refresh Kit Host Upgrade Media"
MEDIA_REMOTE_DIR = "/config/rk-host-upgrade-media"
MEDIA_REMOTE_FILE = f"{MEDIA_REMOTE_DIR}/Refresh Kit Host Upgrade Fixture.mp4"
PLAYBACK_GATE_REASONS = {"playback_route", "media_element", "fullscreen"}
EXPECTED_CONFIG = {
    "EnableInjection": True,
    "EnableThirdPartyStamping": True,
    "EnableAutoReload": True,
    "PollSeconds": 5,
    "IdleSeconds": 0,
    "ReloadBudget": 10,
    "EnableConfigWatching": True,
    "ConfigWatchExclusions": [],
    "ConfigCooldownMinutes": 0,
    "DevMode": False,
}


class EvidenceError(ValueError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def load(path: pathlib.Path) -> dict[str, Any]:
    require(path.is_file(), f"result is missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"cannot parse {path}: {error}") from error
    require(isinstance(data, dict), f"result is not an object: {path}")
    return data


def validate_stage(stage: Any, framework: str, source: dict[str, Any], label: str) -> None:
    require(isinstance(stage, dict), f"{label} stage is missing")
    meta = stage.get("meta")
    package = stage.get("package")
    require(isinstance(meta, dict), f"{label} metadata is missing")
    require(meta.get("framework") == framework, f"{label} framework is not {framework}")
    require(meta.get("sourceDirty") == source.get("dirty"), f"{label} dirty identity differs")
    require(meta.get("sourceRevision") == source.get("revision"), f"{label} source revision differs")
    require(meta.get("sourceTreeSha256") == source.get("treeSha256"), f"{label} source tree differs")
    require(bool(SHA256.fullmatch(str(stage.get("dllSha256", "")))), f"{label} DLL hash is invalid")
    require(isinstance(package, dict), f"{label} package identity is missing")
    require(bool(SHA256.fullmatch(str(package.get("sha256", "")))), f"{label} package SHA-256 is invalid")
    require(bool(MD5.fullmatch(str(package.get("md5", "")))), f"{label} package MD5 is invalid")
    require(isinstance(package.get("size"), int) and package["size"] > 0, f"{label} package size is invalid")


def validate_server(server: Any, label: str) -> dict[str, Any]:
    require(isinstance(server, dict), f"{label}: server identity is missing")
    require(bool(GENERATION.fullmatch(str(server.get("generation", "")))),
            f"{label}: generation is invalid")
    require(bool(EPOCH.fullmatch(str(server.get("epoch", "")))),
            f"{label}: epoch is invalid")
    require(isinstance(server.get("version"), str) and server["version"],
            f"{label}: plugin version is missing")
    require(bool(SHA256.fullmatch(str(server.get("buildId", "")))),
            f"{label}: build id is invalid")
    return server


def normalized_item_id(item_id: Any) -> str:
    return str(item_id or "").replace("-", "").lower()


def validate_media_fixture(fixture: Any, label: str) -> dict[str, Any]:
    require(isinstance(fixture, dict), f"{label}: deterministic media fixture is missing")
    require(fixture.get("deterministicRecipe") == "lavfi-testsrc2+sine-120s-h264-aac-v1"
            and fixture.get("localFile") == "rk-host-upgrade-120s-v1.mp4"
            and fixture.get("remoteDirectory") == MEDIA_REMOTE_DIR
            and fixture.get("remoteFile") == MEDIA_REMOTE_FILE,
            f"{label}: deterministic media recipe/path identity differs")
    require(isinstance(fixture.get("bytes"), int) and fixture["bytes"] >= 100_000,
            f"{label}: deterministic media byte size is invalid")
    require(bool(SHA256.fullmatch(str(fixture.get("sha256", ""))))
            and fixture.get("remoteSha256") == fixture.get("sha256")
            and fixture.get("durationSeconds") == 120,
            f"{label}: deterministic media hash/duration differs")
    library = fixture.get("library")
    require(isinstance(library, dict)
            and library.get("name") == MEDIA_LIBRARY_NAME
            and library.get("locations") == [MEDIA_REMOTE_DIR]
            and re.fullmatch(r"[0-9a-f]{32}", normalized_item_id(library.get("itemId"))) is not None,
            f"{label}: real media library identity differs")
    item = fixture.get("item")
    require(isinstance(item, dict)
            and re.fullmatch(r"[0-9a-f]{32}", normalized_item_id(item.get("id"))) is not None
            and item.get("name") == "Refresh Kit Host Upgrade Fixture"
            and item.get("path") == MEDIA_REMOTE_FILE
            and item.get("type") == "Movie"
            and item.get("mediaType") == "Video"
            and isinstance(item.get("runTimeTicks"), int) and item["runTimeTicks"] >= 900_000_000
            and isinstance(item.get("mediaSourceCount"), int) and item["mediaSourceCount"] >= 1,
            f"{label}: viewer-indexed real media item differs")
    return fixture


def validate_video_state(media: Any, paused: bool, label: str) -> dict[str, Any]:
    require(isinstance(media, dict) and media.get("paused") is paused,
            f"{label}: media paused/playing state differs")
    numeric = (int, float)
    require(type(media.get("currentTime")) in numeric and media["currentTime"] > 0
            and type(media.get("duration")) in numeric and media["duration"] >= 90
            and isinstance(media.get("readyState"), int) and media["readyState"] >= 2
            and media.get("ended") is False
            and isinstance(media.get("videoWidth"), int) and media["videoWidth"] > 0
            and isinstance(media.get("videoHeight"), int) and media["videoHeight"] > 0,
            f"{label}: decoded real video evidence is invalid")
    return media


def expected_page_user(page: dict[str, Any], users: dict[str, Any]) -> dict[str, Any] | None:
    if page.get("role") == "anonymous-login":
        return None
    if str(page.get("name", "")).startswith("viewer-"):
        return users["viewer"]
    return users["admin"]


def validate_page(page: Any, server: dict[str, Any], users: dict[str, Any], label: str) -> None:
    require(isinstance(page, dict), f"{label}: page evidence is missing")
    require(isinstance(page.get("documentId"), str) and page["documentId"],
            f"{label}: document id is missing")
    require(isinstance(page.get("loadCount"), int) and page["loadCount"] >= 1,
            f"{label}: load count is invalid")
    require(re.search(ROLE_ROUTES.get(str(page.get("role")), r"a^"), str(page.get("hash", "")), re.I),
            f"{label}: page is not on its claimed real Jellyfin route")
    kit = page.get("kit")
    require(isinstance(kit, dict), f"{label}: Refresh Kit runtime state is missing")
    require(kit.get("version") == server["generation"]
            and kit.get("latestVersion") == server["generation"],
            f"{label}: runtime generation is not converged")
    require(kit.get("baselineEpoch") == server["epoch"]
            and kit.get("latestEpoch") == server["epoch"],
            f"{label}: runtime process epoch is not converged")
    expected = expected_page_user(page, users)
    if expected is None:
        require(page.get("authenticated") is False and page.get("user") is None,
                f"{label}: anonymous page resolved an authenticated identity")
        require(re.search(r"/login(?:\.html)?(?:[?]|$)", str(page.get("hash", "")), re.I),
                f"{label}: anonymous page is not on a Jellyfin login route")
    else:
        actual = page.get("user")
        require(page.get("authenticated") is True and isinstance(actual, dict),
                f"{label}: authenticated page identity is missing")
        expected_id = str(expected.get("id", "")).replace("-", "").lower()
        actual_id = str(actual.get("id", "")).replace("-", "").lower()
        require(actual_id == expected_id and actual.get("name") == expected.get("name"),
                f"{label}: authenticated page resolved the wrong user")


def validate_reload_set(before: Any, after: Any, server_before: dict[str, Any],
                        server_after: dict[str, Any], users: dict[str, Any],
                        expected_count: int, label: str) -> None:
    require(isinstance(before, list) and isinstance(after, list)
            and len(before) == len(after) == expected_count,
            f"{label}: page-set cardinality differs")
    before_by_name = {entry.get("name"): entry for entry in before if isinstance(entry, dict)}
    after_by_name = {entry.get("name"): entry for entry in after if isinstance(entry, dict)}
    require(len(before_by_name) == len(after_by_name) == expected_count
            and set(before_by_name) == set(after_by_name),
            f"{label}: page names are missing, duplicated, or changed")
    for name, prior in before_by_name.items():
        current = after_by_name[name]
        validate_page(prior, server_before, users, f"{label}/{name}/before")
        validate_page(current, server_after, users, f"{label}/{name}/after")
        require(current.get("role") == prior.get("role")
                and current.get("loadCount") == prior.get("loadCount") + 1
                and current.get("documentId") != prior.get("documentId"),
                f"{label}/{name}: exact one-reload evidence differs")


def validate_container_identity(identity: Any, image: str, label: str) -> dict[str, Any]:
    require(isinstance(identity, dict) and identity.get("configuredImage") == image,
            f"{label}: configured image differs")
    require(bool(re.fullmatch(r"[0-9a-f]{64}", str(identity.get("containerId", "")))),
            f"{label}: container id is invalid")
    require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", str(identity.get("localImageId", "")))),
            f"{label}: local image id is invalid")
    digest = image.split("@", 1)[1]
    repo_digests = identity.get("repoDigests")
    require(isinstance(repo_digests, list)
            and any(isinstance(item, str) and item.endswith("@" + digest) for item in repo_digests),
            f"{label}: exact repository digest is absent")
    mounts = identity.get("mounts")
    require(isinstance(mounts, dict) and set(mounts) == {"/config", "/cache"}
            and all(isinstance(mount, dict) and mount.get("type") == "volume"
                    and mount.get("rw") is True and isinstance(mount.get("name"), str)
                    and bool(mount["name"]) for mount in mounts.values()),
            f"{label}: exact writable named-volume identity differs")
    return identity


def validate_result(data: dict[str, Any], scenario: str, snapshot: str) -> None:
    source_image, target_image, source_version, target_version, target_stage = IMAGES[scenario]
    require(data.get("schemaVersion") == 2, f"{scenario}: unsupported schema")
    require(data.get("scenario") == scenario, f"{scenario}: scenario mismatch")
    require(data.get("completed") is True, f"{scenario}: run did not complete")
    require(data.get("failures") == [], f"{scenario}: failures are present")
    require(data.get("unexpectedRefreshKitBrowserErrors") == [], f"{scenario}: unexpected RK browser errors")
    require(data.get("versionFlapWarnings") == [], f"{scenario}: version-flap warnings are present")
    require(data.get("probeRemoved") is True, f"{scenario}: disposable probe was not removed")
    capture_counts = data.get("captureCounts")
    require(isinstance(capture_counts, dict) and len(capture_counts) == 10
            and all(isinstance(record, dict) and record.get("truncated") is False
                    for record in capture_counts.values()),
            f"{scenario}: browser capture inventory is missing or truncated")

    metadata = data.get("metadata")
    require(isinstance(metadata, dict), f"{scenario}: metadata is missing")
    require(metadata.get("immutableSnapshot") == snapshot, f"{scenario}: immutable snapshot differs")
    require(metadata.get("from") == {"image": source_image, "serverVersion": source_version},
            f"{scenario}: source identity differs")
    require(metadata.get("to") == {"image": target_image, "serverVersion": target_version},
            f"{scenario}: target identity differs")
    source = metadata.get("sourceIdentity")
    require(isinstance(source, dict) and isinstance(source.get("dirty"), bool),
            f"{scenario}: source dirty identity is missing")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", str(source.get("revision", "")))),
            f"{scenario}: source revision is invalid")
    require(bool(SHA256.fullmatch(str(source.get("treeSha256", "")))),
            f"{scenario}: source tree hash is invalid")
    stages = metadata.get("stages")
    require(isinstance(stages, dict), f"{scenario}: stage identities are missing")
    validate_stage(stages.get("net9"), "net9.0", source, f"{scenario}/net9")
    validate_stage(stages.get("net10"), "net10.0", source, f"{scenario}/net10")
    meta9 = stages["net9"]["meta"]
    meta10 = stages["net10"]["meta"]
    version = str(meta9.get("version", ""))
    require(re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", version) is not None
            and meta10.get("version") == version, f"{scenario}: stage versions differ/are invalid")
    require(meta9.get("guid") == meta10.get("guid") == "515255fe-3332-49b0-b471-0be58c8221d8",
            f"{scenario}: stage GUID differs")
    require(meta9.get("targetAbi") == "10.11.0.0" and meta10.get("targetAbi") == "12.0.0.0",
            f"{scenario}: stage ABIs differ")
    require(meta9.get("sourceDateEpoch") == meta10.get("sourceDateEpoch") == source.get("dateEpoch"),
            f"{scenario}: stage source epoch differs")
    require(stages["net9"]["package"].get("file") == f"jellyfin-refresh-kit_{version}.zip"
            and stages["net10"]["package"].get("file") == f"jellyfin-refresh-kit_{version}_jf12.zip",
            f"{scenario}: package filenames differ")
    users = metadata.get("users")
    require(isinstance(users, dict), f"{scenario}: user identities are missing")
    admin_id = str(users.get("admin", {}).get("id", "")).replace("-", "").lower()
    viewer_id = str(users.get("viewer", {}).get("id", "")).replace("-", "").lower()
    require(re.fullmatch(r"[0-9a-f]{32}", admin_id) is not None, f"{scenario}: admin id is invalid")
    require(re.fullmatch(r"[0-9a-f]{32}", viewer_id) is not None, f"{scenario}: viewer id is invalid")
    require(admin_id != viewer_id, f"{scenario}: admin/viewer identities are not distinct")
    require(isinstance(users.get("admin", {}).get("name"), str) and users["admin"]["name"]
            and isinstance(users.get("viewer", {}).get("name"), str) and users["viewer"]["name"]
            and users["admin"]["name"] != users["viewer"]["name"],
            f"{scenario}: admin/viewer names are missing or not distinct")

    phases = data.get("phases")
    require(isinstance(phases, list), f"{scenario}: phases are missing")
    phase_names = [phase.get("name") for phase in phases if isinstance(phase, dict)]
    require(set(phase_names) == REQUIRED_PHASES and len(phase_names) == len(REQUIRED_PHASES)
            and len(phases) == len(REQUIRED_PHASES),
            f"{scenario}: phase set differs: {phase_names}")
    phase_by_name = {phase["name"]: phase for phase in phases}

    sequential = (
        ("one-live-tab-converged", 1),
        ("two-tabs-two-contexts-users-converged", 2),
        ("ten-live-tabs-role-mix-converged", 10),
    )
    prior_after = None
    for phase_name, count in sequential:
        phase = phase_by_name[phase_name]
        require(phase.get("tabs") == count, f"{scenario}/{phase_name}: tab count differs")
        server_before = validate_server(phase.get("serverBefore"), f"{scenario}/{phase_name}/before")
        server_after = validate_server(phase.get("serverAfter"), f"{scenario}/{phase_name}/after")
        require(server_before["generation"] != server_after["generation"]
                and server_before["epoch"] == server_after["epoch"]
                and server_before["version"] == server_after["version"] == version
                and server_before["buildId"] == server_after["buildId"],
                f"{scenario}/{phase_name}: in-process generation/epoch transition differs")
        if prior_after is not None:
            require(server_before == prior_after,
                    f"{scenario}/{phase_name}: phase server identities are discontinuous")
        validate_reload_set(phase.get("pagesBefore"), phase.get("pagesAfter"),
                            server_before, server_after, users, count,
                            f"{scenario}/{phase_name}")
        prior_after = server_after

    ten_phase = phase_by_name["ten-live-tabs-role-mix-converged"]
    ten_before_server = ten_phase["serverBefore"]
    ten_after_server = ten_phase["serverAfter"]
    ten_before_pages = {entry.get("name"): entry for entry in ten_phase["pagesBefore"]}
    ten_after_pages = {entry.get("name"): entry for entry in ten_phase["pagesAfter"]}
    gated = ten_phase.get("gated")
    require(isinstance(gated, dict), f"{scenario}: ten-tab gated snapshots are missing")
    for key, page_name, reason in (("dialog", "admin-plugin-dialog", "dialog"),
                                   ("editor", "admin-config-editor", "text_entry")):
        page = gated.get(key)
        prior = ten_before_pages.get(page_name)
        require(isinstance(page, dict) and isinstance(prior, dict),
                f"{scenario}/{key}: gated page evidence is missing")
        kit = page.get("kit")
        require(page.get("documentId") == prior.get("documentId")
                and page.get("loadCount") == prior.get("loadCount"),
                f"{scenario}/{key}: gated document reloaded")
        require(isinstance(kit, dict) and kit.get("version") == ten_before_server["generation"]
                and kit.get("latestVersion") == ten_after_server["generation"]
                and kit.get("baselineEpoch") == ten_before_server["epoch"]
                and kit.get("latestEpoch") == ten_after_server["epoch"]
                and kit.get("wouldBlockNow") == reason,
                f"{scenario}/{key}: live gated runtime state differs")

    stress_phase = phase_by_name["generation-poll-stress-and-ten-tab-catch-up"]
    stress_waves = stress_phase.get("waves")
    require(stress_waves == data.get("pollStress") and isinstance(stress_waves, list)
            and len(stress_waves) == 3,
            f"{scenario}: stress phase/result waves differ")
    stress_after = stress_waves[-1].get("responseIdentity")
    validate_server(stress_after, f"{scenario}/stress/final-server")
    require(stress_phase.get("diagnosticsGeneration") == stress_after["generation"],
            f"{scenario}: stress diagnostics generation differs")
    validate_reload_set(stress_phase.get("pagesBefore"), stress_phase.get("pagesAfter"),
                        ten_after_server, stress_after, users, 10,
                        f"{scenario}/stress-catch-up")

    multi = data.get("multiTab")
    require(isinstance(multi, dict) and multi.get("tabCounts") == [1, 2, 10],
            f"{scenario}: tab checkpoints differ")
    require(re.search(r"/login(?:\.html)?(?:[?]|$)", str(multi.get("anonymousLoginRoute", "")), re.I),
            f"{scenario}: anonymous tab was not retained on a real login route")
    roles = multi.get("finalRoles")
    require(isinstance(roles, list) and len(roles) == 10, f"{scenario}: final role inventory is not ten tabs")
    role_names = [entry.get("name") for entry in roles if isinstance(entry, dict)]
    role_values = [entry.get("role") for entry in roles if isinstance(entry, dict)]
    expected_role_counts = {role: (3 if role == "viewer-background" else 1) for role in REQUIRED_ROLES}
    require(len(set(role_names)) == 10
            and {role: role_values.count(role) for role in REQUIRED_ROLES} == expected_role_counts
            and set(role_values) == REQUIRED_ROLES,
            f"{scenario}: role mix differs")
    require(any(entry.get("hiddenAtCheckpoint") is True for entry in roles),
            f"{scenario}: no hidden/background tab evidence")
    require(all(entry.get("documentId") and isinstance(entry.get("loadCount"), int)
                for entry in roles), f"{scenario}: document/load identities are incomplete")
    for entry in roles:
        expected = expected_page_user(entry, users)
        expected_id = None if expected is None else str(expected["id"]).replace("-", "").lower()
        actual_id = None if entry.get("userId") is None else str(entry["userId"]).replace("-", "").lower()
        require(entry.get("authenticated") is (expected is not None) and actual_id == expected_id,
                f"{scenario}/{entry.get('name')}: final role/user identity differs")

    contexts = data.get("browserContexts")
    require(isinstance(contexts, list) and len(contexts) == 3, f"{scenario}: context inventory differs")
    require([context.get("name") for context in contexts] == ["admin", "viewer", "anonymous"],
            f"{scenario}: context roles differ")
    require(contexts[0].get("authenticated") is True and contexts[1].get("authenticated") is True,
            f"{scenario}: authenticated contexts are incomplete")
    require(str(contexts[0].get("userId", "")).replace("-", "").lower() == admin_id,
            f"{scenario}: admin context identity differs")
    require(str(contexts[1].get("userId", "")).replace("-", "").lower() == viewer_id,
            f"{scenario}: viewer context identity differs")
    require(contexts[0].get("userName") == users["admin"].get("name")
            and contexts[1].get("userName") == users["viewer"].get("name"),
            f"{scenario}: context user names differ")
    require(contexts[2] == {"name": "anonymous", "userId": None, "userName": None, "authenticated": False},
            f"{scenario}: anonymous context is not exact")

    dialog = data.get("dialogSafety")
    require(isinstance(dialog, dict) and dialog.get("realJellyfinDialog") is True,
            f"{scenario}: real dialog evidence is missing")
    require(dialog.get("role") == "dialog" and dialog.get("blockReason") == "dialog",
            f"{scenario}: dialog safety gate differs")
    require(dialog.get("documentIdPreservedWhileOpen") is True
            and dialog.get("loadCountDeltaWhileOpen") == 0
            and dialog.get("cancelledWithoutUninstall") is True,
            f"{scenario}: dialog did not remain/release safely")
    require(dialog.get("inventoryBefore") == dialog.get("inventoryAfter"),
            f"{scenario}: dialog cancellation changed inventory")
    dialog_inventory = dialog.get("inventoryBefore")
    require(isinstance(dialog_inventory, list) and len(dialog_inventory) == 1
            and dialog_inventory[0].get("name") == "Jellyfin Refresh Kit"
            and dialog_inventory[0].get("status") == "Active"
            and dialog_inventory[0].get("canUninstall") is True,
            f"{scenario}: dialog did not target the active uninstallable Refresh Kit plugin")
    editor = data.get("editorSafety")
    require(isinstance(editor, dict) and editor.get("blockReason") == "text_entry",
            f"{scenario}: real editor gate differs")
    require(editor.get("documentIdPreservedWhileEditing") is True
            and editor.get("loadCountDeltaWhileEditing") == 0
            and editor.get("convergedAfterBlur") is True,
            f"{scenario}: editor did not remain/release safely")

    playback = data.get("playbackSafety")
    require(isinstance(playback, dict) and playback.get("realMediaPlayback") is True
            and playback.get("source")
            == "viewer-owned indexed MP4 on a real Jellyfin details/playback route",
            f"{scenario}: real viewer playback evidence is missing")
    fixture = validate_media_fixture(playback.get("fixture"), f"{scenario}/playback/fixture")
    fixture_item_id = normalized_item_id(fixture["item"]["id"])
    for route_key in ("detailsRoute", "playbackDetails"):
        route = playback.get(route_key)
        require(isinstance(route, dict)
                and re.search(r"/details(?:\.html)?(?:[?]|$)", str(route.get("hash", "")), re.I)
                and normalized_item_id(route.get("itemId")) == fixture_item_id
                and normalized_item_id(route.get("expectedId")) == fixture_item_id,
                f"{scenario}/playback: {route_key} is not the exact real item details route")
    progress_start = playback.get("progressStartSeconds")
    progress_end = playback.get("progressEndSeconds")
    progress = playback.get("progressSeconds")
    require(type(progress_start) in (int, float) and type(progress_end) in (int, float)
            and type(progress) in (int, float)
            and progress_start > 0 and progress_end >= progress_start + 0.5
            and progress >= 0.5 and abs(progress - (progress_end - progress_start)) < 0.001,
            f"{scenario}/playback: real media progress evidence is invalid")
    playing = playback.get("playing")
    playback_gated = playback.get("gated")
    paused = playback.get("paused")
    left_playback = playback.get("leftPlayback")
    playback_before = ten_before_pages.get("viewer-playback")
    playback_after = ten_after_pages.get("viewer-playback")
    require(playing == playback_before and playback_gated == gated.get("playback")
            and left_playback == playback_after,
            f"{scenario}/playback: phase snapshots and playback result are discontinuous")
    require(isinstance(playing, dict) and isinstance(playback_gated, dict)
            and isinstance(paused, dict) and isinstance(left_playback, dict),
            f"{scenario}/playback: playing/gated/paused/left snapshots are incomplete")
    playing_media = validate_video_state(playing.get("media"), False,
                                         f"{scenario}/playback/playing")
    gated_media = validate_video_state(playback_gated.get("media"), False,
                                       f"{scenario}/playback/gated")
    paused_media = validate_video_state(paused.get("media"), True,
                                        f"{scenario}/playback/paused")
    require(gated_media["currentTime"] > playing_media["currentTime"]
            and paused_media["currentTime"] >= gated_media["currentTime"],
            f"{scenario}/playback: media did not progress through the live mutation")
    require(playing.get("kit", {}).get("wouldBlockNow") in PLAYBACK_GATE_REASONS
            and playback_gated.get("kit", {}).get("wouldBlockNow") in PLAYBACK_GATE_REASONS
            and paused.get("kit", {}).get("wouldBlockNow") in PLAYBACK_GATE_REASONS
            and playback.get("blockReason") == playback_gated["kit"]["wouldBlockNow"],
            f"{scenario}/playback: media/playback safety reason differs")
    for state_name, state in (("gated", playback_gated), ("paused", paused)):
        kit = state.get("kit")
        require(state.get("role") == "viewer-playback"
                and re.search(ROLE_ROUTES["viewer-playback"], str(state.get("hash", "")), re.I)
                and state.get("authenticated") is True
                and state.get("user") == users["viewer"]
                and state.get("documentId") == playing.get("documentId")
                and state.get("loadCount") == playing.get("loadCount")
                and isinstance(kit, dict)
                and kit.get("version") == ten_before_server["generation"]
                and kit.get("latestVersion") == ten_after_server["generation"]
                and kit.get("baselineEpoch") == ten_before_server["epoch"]
                and kit.get("latestEpoch") == ten_after_server["epoch"],
                f"{scenario}/playback/{state_name}: gated document/runtime identity differs")
    require(re.search(r"/home(?:\.html)?(?:[/?]|$)", str(left_playback.get("hash", "")), re.I)
            and playback.get("documentIdPreservedWhilePlaying") is True
            and playback.get("loadCountDeltaWhilePlaying") == 0
            and playback.get("currentGenerationHeldWhileLatestAdvanced") is True
            and playback.get("pausedWithoutReload") is True
            and playback.get("exactOneReloadAfterLeave") is True
            and left_playback.get("loadCount") == playing.get("loadCount") + 1
            and left_playback.get("documentId") != playing.get("documentId"),
            f"{scenario}/playback: pause/leave exact convergence evidence differs")

    waves = data.get("pollStress")
    require(isinstance(waves, list) and [wave.get("clients") for wave in waves] == [10, 50, 100],
            f"{scenario}: stress client counts differ")
    wave_generations: set[str] = set()
    wave_epoch = None
    expected_generation_before = ten_after_server["generation"]
    for wave in waves:
        clients = wave["clients"]
        identity = wave.get("responseIdentity")
        timing = wave.get("timingMs")
        require(wave.get("allResponsesExact") is True, f"{scenario}/{clients}: response set is not exact")
        require(wave.get("independentSockets") == clients, f"{scenario}/{clients}: sockets are not independent")
        require(wave.get("maximumInFlight") == clients,
                f"{scenario}/{clients}: requests were not simultaneously in flight")
        require(isinstance(wave.get("startBarrierSpreadMs"), (int, float))
                and 0 <= wave["startBarrierSpreadMs"] <= 2000,
                f"{scenario}/{clients}: start-barrier spread exceeded bound")
        require(isinstance(identity, dict) and identity.get("status") == 200,
                f"{scenario}/{clients}: status differs")
        require("no-store" in str(identity.get("cacheControl", "")).lower(),
                f"{scenario}/{clients}: no-store is absent")
        require(str(identity.get("contentType", "")).lower().startswith("application/json"),
                f"{scenario}/{clients}: content type differs")
        require(bool(GENERATION.fullmatch(str(identity.get("generation", "")))),
                f"{scenario}/{clients}: generation is malformed")
        require(bool(EPOCH.fullmatch(str(identity.get("epoch", "")))),
                f"{scenario}/{clients}: epoch is malformed")
        require(identity.get("generation") not in wave_generations,
                f"{scenario}/{clients}: generation was reused")
        wave_generations.add(identity["generation"])
        wave_epoch = wave_epoch or identity["epoch"]
        require(identity["epoch"] == wave_epoch, f"{scenario}/{clients}: epoch changed within process")
        require(identity.get("version") == version
                and identity.get("buildId") == ten_after_server["buildId"]
                and identity.get("epoch") == ten_after_server["epoch"],
                f"{scenario}/{clients}: response version/build/epoch differs")
        require(wave.get("generationBefore") == expected_generation_before
                and wave.get("generationAfter") == identity["generation"],
                f"{scenario}/{clients}: wave generation transition differs")
        mutation = wave.get("mutation")
        require(isinstance(mutation, dict) and isinstance(mutation.get("sequence"), int)
                and mutation.get("label") == f"poll-{clients}",
                f"{scenario}/{clients}: unique monitored mutation evidence differs")
        expected_generation_before = identity["generation"]
        require(isinstance(timing, dict) and timing.get("perRequestLimit") == 20_000
                and timing.get("waveLimit") == 30_000
                and timing.get("max", 1e99) <= 20_000,
                f"{scenario}/{clients}: request timing exceeded bound")
        require(isinstance(wave.get("waveDurationMs"), (int, float))
                and 0 <= wave["waveDurationMs"] <= 30_000,
                f"{scenario}/{clients}: wave timing exceeded bound")
        require(all(isinstance(timing.get(key), (int, float)) for key in ("min", "p50", "p95", "max"))
                and 0 <= timing["min"] <= timing["p50"] <= timing["p95"] <= timing["max"],
                f"{scenario}/{clients}: latency distribution is invalid")
        responses = wave.get("responses")
        require(isinstance(responses, list) and len(responses) == clients
                and {response.get("client") for response in responses} == set(range(clients)),
                f"{scenario}/{clients}: per-client response inventory differs")
        for response in responses:
            require(all(response.get(key) == identity.get(key)
                        for key in ("status", "version", "buildId", "generation", "epoch"))
                    and "no-store" in str(response.get("cacheControl", "")).lower()
                    and str(response.get("contentType", "")).lower().startswith("application/json")
                    and response.get("connectionReused") is False
                    and isinstance(response.get("durationMs"), (int, float))
                    and 0 <= response["durationMs"] <= timing["perRequestLimit"],
                    f"{scenario}/{clients}/{response.get('client')}: exact response evidence differs")
    link = data.get("pollStressRegressionLink")
    require(isinstance(link, dict)
            and link.get("source") == "plugin/Jellyfin.Plugin.RefreshKit.Tests/ActivePluginGenerationTests.cs"
            and link.get("method") == "ConcurrentGenerationReadsShareExactlyOneScanPerInvalidation(int readerCount)"
            and link.get("cases") == [10, 50, 100], f"{scenario}: deterministic provider link differs")

    upgrade = data.get("hostUpgrade")
    require(isinstance(upgrade, dict), f"{scenario}: host-upgrade evidence is missing")
    require(upgrade.get("sourceImage") == source_image and upgrade.get("targetImage") == target_image,
            f"{scenario}: retained images differ")
    require(upgrade.get("sourcePublicVersion") == source_version
            and upgrade.get("targetPublicVersion") == target_version,
            f"{scenario}: retained server versions differ")
    require(upgrade.get("volumesPreserved") is True and upgrade.get("generationChanged") is True
            and upgrade.get("epochChanged") is True and upgrade.get("configPreserved") is True
            and upgrade.get("usersPreserved") is True and upgrade.get("mediaPreserved") is True
            and upgrade.get("browserCacheEnabled") is True
            and upgrade.get("exactOneReloadPerDocument") is True,
            f"{scenario}: a host-upgrade preservation/convergence invariant failed")
    require(upgrade.get("configBefore") == EXPECTED_CONFIG
            and upgrade.get("configAfter") == EXPECTED_CONFIG,
            f"{scenario}: exact pre/post plugin configuration differs")
    source_container = validate_container_identity(
        upgrade.get("sourceIdentity"), source_image, f"{scenario}/source-container")
    target_container = validate_container_identity(
        upgrade.get("targetIdentity"), target_image, f"{scenario}/target-container")
    require(source_container["mounts"] == target_container["mounts"],
            f"{scenario}: exact volume identities differ")
    media_before = validate_media_fixture(upgrade.get("mediaBefore"),
                                          f"{scenario}/host/media-before")
    media_after = upgrade.get("mediaAfter")
    require(media_before == fixture and isinstance(media_after, dict)
            and media_after.get("remoteSha256") == media_before["sha256"]
            and media_after.get("library") == media_before["library"]
            and media_after.get("item") == media_before["item"]
            and media_after.get("indexedForViewer") is True,
            f"{scenario}: preserved/indexed real media identity differs after host replacement")
    source_server = validate_server(upgrade.get("sourceServer"), f"{scenario}/host/source-server")
    target_server = validate_server(upgrade.get("targetServer"), f"{scenario}/host/target-server")
    require(all(source_server.get(key) == stress_after.get(key)
                for key in ("version", "buildId", "generation", "epoch")),
            f"{scenario}: stress-to-upgrade server identity is discontinuous")
    require(source_server["generation"] != target_server["generation"]
            and source_server["epoch"] != target_server["epoch"]
            and source_server["version"] == target_server["version"] == version,
            f"{scenario}: host generation/epoch did not both change")
    before = upgrade.get("openDocumentsBefore")
    after = upgrade.get("openDocumentsAfter")
    validate_reload_set(before, after, source_server, target_server, users, 10,
                        f"{scenario}/host-upgrade")
    require(upgrade.get("sourcePluginDllSha256") == stages["net9"]["dllSha256"]
            and upgrade.get("targetPluginDllSha256") == stages[target_stage]["dllSha256"],
            f"{scenario}: source/target plugin stage hash differs")

    transition = upgrade.get("transition")
    require(isinstance(transition, dict) and transition.get("source") == source_container
            and transition.get("target") == target_container,
            f"{scenario}: container transition identities differ")
    require(transition.get("sourceLogRetained") is True,
            f"{scenario}: source server log was not retained before replacement")
    windows = transition.get("windows")
    expected_window_kinds = (["disable-source-plugin", "host-image-replacement"]
                             if scenario == "jf12" else ["host-image-replacement"])
    require(isinstance(windows, list)
            and [window.get("kind") for window in windows] == expected_window_kinds
            and data.get("transitionWindows") == windows
            and all(isinstance(window.get("startElapsedMs"), (int, float))
                    and isinstance(window.get("healthyElapsedMs"), (int, float))
                    and 0 <= window["startElapsedMs"] <= window["healthyElapsedMs"]
                    for window in windows),
            f"{scenario}: transition-window evidence differs")
    final_inventory = transition.get("finalInventory")
    require(isinstance(final_inventory, list) and len(final_inventory) == 1
            and str(final_inventory[0].get("id", "")).replace("-", "").lower()
            == "515255fe333249b0b4710be58c8221d8"
            and final_inventory[0].get("name") == "Jellyfin Refresh Kit"
            and final_inventory[0].get("status") == "Active"
            and final_inventory[0].get("canUninstall") is True
            and final_inventory[0].get("version") == version,
            f"{scenario}: final active plugin inventory differs")
    if scenario == "jf12":
        disable = transition.get("disable")
        require(isinstance(disable, dict)
                and disable.get("apiStatus") == 204
                and disable.get("generationStatusAfterRestart") == 404
                and isinstance(disable.get("inventoryBefore"), list)
                and len(disable["inventoryBefore"]) == 1
                and disable["inventoryBefore"][0].get("status") == "Active"
                and disable["inventoryBefore"][0].get("version") == version
                and any(item.get("status") == "Restart"
                        for item in disable.get("pendingInventory", []))
                and isinstance(disable.get("disabledInventory"), list)
                and len(disable["disabledInventory"]) == 1
                and disable["disabledInventory"][0].get("status") == "Disabled",
                "jf12: net9 disable evidence is missing")
        replacement = transition.get("replacement")
        require(isinstance(replacement, dict)
                and replacement.get("targetFramework") == "net10.0"
                and replacement.get("fromSha256") == stages["net9"]["dllSha256"]
                and replacement.get("toSha256") == stages["net10"]["dllSha256"]
                and replacement["fromSha256"] != replacement["toSha256"],
                "jf12: net9-to-net10 replacement evidence differs")
        enable = transition.get("enable")
        require(isinstance(enable, dict)
                and enable.get("apiStatus") == 204
                and enable.get("generationStatusBeforeEnable") == 404
                and isinstance(enable.get("migratedDisabledInventory"), list)
                and len(enable["migratedDisabledInventory"]) == 1
                and enable["migratedDisabledInventory"][0].get("status") == "Disabled"
                and any(item.get("status") == "Restart"
                        for item in enable.get("pendingInventory", []))
                and enable.get("activeInventory") == final_inventory,
                "jf12: net10 enable evidence is missing")
    else:
        require(transition.get("disable") is None and transition.get("replacement") is None
                and transition.get("enable") is None,
                "jf10: unexpected plugin migration was recorded")


def validate_aggregate(root: pathlib.Path, snapshot: str) -> None:
    results = {}
    for scenario in ("jf10", "jf12"):
        results[scenario] = load(root / scenario / "result.json")
        validate_result(results[scenario], scenario, snapshot)
    aggregate = load(root / "result.json")
    require(aggregate.get("schemaVersion") == 1 and aggregate.get("completed") is True,
            "aggregate did not complete")
    require(aggregate.get("failures") == [], "aggregate contains failures")
    require(aggregate.get("immutableSnapshot") == snapshot, "aggregate snapshot differs")
    require(aggregate.get("sourceIdentity") == results["jf10"]["metadata"]["sourceIdentity"]
            == results["jf12"]["metadata"]["sourceIdentity"],
            "aggregate/source scenario identities differ")
    scenarios = aggregate.get("scenarios")
    require(isinstance(scenarios, dict) and set(scenarios) == {"jf10", "jf12"},
            "aggregate scenario set differs")
    require(all(record.get("completed") is True for record in scenarios.values()),
            "aggregate scenario completion differs")
    require(scenarios["jf10"].get("result") == "jf10/result.json"
            and scenarios["jf12"].get("result") == "jf12/result.json",
            "aggregate result paths are not stable relative paths")


def self_test() -> None:
    import copy
    import tempfile

    def fixture(scenario: str) -> dict[str, Any]:
        source_image, target_image, source_version, target_version, target_stage = IMAGES[scenario]
        source = {"revision": "a" * 40, "treeSha256": "b" * 64,
                  "dateEpoch": 1_786_000_000, "dirty": False}
        users = {
            "admin": {"name": "rk_admin", "id": "1" * 32},
            "viewer": {"name": "rk_viewer", "id": "2" * 32},
        }
        def stage(framework: str, abi: str, dll: str, suffix: str) -> dict[str, Any]:
            return {
                "meta": {"framework": framework, "targetAbi": abi,
                         "version": "1.0.1.0",
                         "guid": "515255fe-3332-49b0-b471-0be58c8221d8",
                         "sourceDirty": False,
                         "sourceRevision": source["revision"],
                         "sourceTreeSha256": source["treeSha256"],
                         "sourceDateEpoch": source["dateEpoch"]},
                "dllSha256": dll * 64,
                "package": {"file": f"jellyfin-refresh-kit_1.0.1.0{suffix}.zip", "size": 10,
                            "sha256": ("d" if suffix == "" else "e") * 64,
                            "md5": ("4" if suffix == "" else "5") * 32},
            }
        stages = {"net9": stage("net9.0", "10.11.0.0", "8", ""),
                  "net10": stage("net10.0", "12.0.0.0", "9", "_jf12")}
        role_rows = [
            ("admin-dashboard", "admin-dashboard", users["admin"]),
            ("viewer-home", "viewer-home", users["viewer"]),
            ("admin-config-editor", "admin-config-editor", users["admin"]),
            ("admin-plugin-dialog", "admin-plugin-dialog", users["admin"]),
            ("admin-background", "admin-background", users["admin"]),
            ("viewer-background-1", "viewer-background", users["viewer"]),
            ("viewer-background-2", "viewer-background", users["viewer"]),
            ("viewer-background-3", "viewer-background", users["viewer"]),
            ("viewer-playback", "viewer-playback", users["viewer"]),
            ("anonymous-login", "anonymous-login", None),
        ]
        def server(digit: int, epoch: str = "1" * 32) -> dict[str, Any]:
            return {"version": "1.0.1.0", "buildId": "f" * 64,
                    "generation": f"g-{digit:016x}", "epoch": epoch}
        def page(row: tuple[str, str, dict[str, Any] | None], document: str,
                 load_count: int, current_server: dict[str, Any]) -> dict[str, Any]:
            name, role, user = row
            return {"name": name, "role": role, "documentId": document,
                    "loadCount": load_count, "authenticated": user is not None,
                    "user": copy.deepcopy(user),
                    "hash": ({"admin-dashboard": "#/dashboard",
                              "admin-background": "#/dashboard",
                              "admin-config-editor": "#/configurationpage?name=Jellyfin%20Refresh%20Kit",
                              "admin-plugin-dialog": "#/dashboard/plugins/515255fe333249b0b4710be58c8221d8",
                              "viewer-playback": "#/home",
                              "anonymous-login": "#/login"}.get(role, "#/home")),
                    "kit": {"version": current_server["generation"],
                            "latestVersion": current_server["generation"],
                            "baselineEpoch": current_server["epoch"],
                            "latestEpoch": current_server["epoch"]}}
        def reload_set(rows: list[dict[str, Any]], prefix: str,
                       current_server: dict[str, Any]) -> list[dict[str, Any]]:
            result = []
            by_name = {row[0]: row for row in role_rows}
            for index, prior in enumerate(rows):
                result.append(page(by_name[prior["name"]], f"{prefix}-{index}",
                                   prior["loadCount"] + 1, current_server))
            return result

        boot_server = server(1)
        one_server = server(2)
        two_server = server(3)
        ten_server = server(4)
        source_server = server(7)
        target_server = server(8, "2" * 32)

        one_before = [page(role_rows[0], "one-before-0", 1, boot_server)]
        one_after = reload_set(one_before, "one-after", one_server)
        two_before = [copy.deepcopy(one_after[0]), page(role_rows[1], "two-before-1", 1, one_server)]
        two_after = reload_set(two_before, "two-after", two_server)
        ten_before = [copy.deepcopy(row) for row in two_after]
        ten_before.extend(page(row, f"ten-before-{index}", 1, two_server)
                          for index, row in enumerate(role_rows[2:], start=2))
        playback_before = next(row for row in ten_before if row["name"] == "viewer-playback")
        playback_before["hash"] = "#/video?itemId=" + "4" * 32
        playback_before["media"] = {
            "paused": False, "currentTime": 4.0, "duration": 120.0,
            "readyState": 4, "ended": False, "videoWidth": 640, "videoHeight": 360,
        }
        playback_before["kit"]["wouldBlockNow"] = "media_element"
        ten_after = reload_set(ten_before, "ten-after", ten_server)
        gated = {}
        for key, name, reason in (("dialog", "admin-plugin-dialog", "dialog"),
                                  ("editor", "admin-config-editor", "text_entry")):
            gated_page = copy.deepcopy(next(row for row in ten_before if row["name"] == name))
            gated_page["kit"]["latestVersion"] = ten_server["generation"]
            gated_page["kit"]["wouldBlockNow"] = reason
            gated[key] = gated_page
        playback_gated = copy.deepcopy(playback_before)
        playback_gated["media"]["currentTime"] = 8.0
        playback_gated["kit"]["latestVersion"] = ten_server["generation"]
        playback_gated["kit"]["wouldBlockNow"] = "media_element"
        gated["playback"] = playback_gated
        playback_paused = copy.deepcopy(playback_gated)
        playback_paused["media"]["paused"] = True
        playback_paused["media"]["currentTime"] = 8.5
        playback_left = next(row for row in ten_after if row["name"] == "viewer-playback")
        stress_before = copy.deepcopy(ten_after)
        stress_after = reload_set(stress_before, "stress-after", source_server)
        before = copy.deepcopy(stress_after)
        after = reload_set(before, "host-after", target_server)
        mounts = {"/config": {"name": "config-volume", "type": "volume", "rw": True},
                  "/cache": {"name": "cache-volume", "type": "volume", "rw": True}}
        source_identity = {"containerId": "a" * 64, "configuredImage": source_image,
                           "localImageId": "sha256:" + "b" * 64,
                           "repoDigests": ["jellyfin/jellyfin@" + source_image.split("@", 1)[1]],
                           "mounts": mounts}
        target_identity = {"containerId": "c" * 64, "configuredImage": target_image,
                           "localImageId": "sha256:" + "d" * 64,
                           "repoDigests": ["jellyfin/jellyfin@" + target_image.split("@", 1)[1]],
                           "mounts": copy.deepcopy(mounts)}
        window_kinds = (["disable-source-plugin", "host-image-replacement"]
                        if scenario == "jf12" else ["host-image-replacement"])
        windows = [{"kind": kind, "startElapsedMs": index * 10,
                    "healthyElapsedMs": index * 10 + 9}
                   for index, kind in enumerate(window_kinds)]
        transition: dict[str, Any] = {
            "source": source_identity,
            "target": target_identity,
            "sourceLogRetained": True,
            "windows": windows,
            "finalInventory": [{"id": "515255fe333249b0b4710be58c8221d8",
                                "name": "Jellyfin Refresh Kit", "status": "Active",
                                "version": "1.0.1.0", "canUninstall": True}],
            "disable": None,
            "replacement": None,
        }
        if scenario == "jf12":
            active_plugin = {"id": "515255fe333249b0b4710be58c8221d8",
                             "name": "Jellyfin Refresh Kit", "status": "Active",
                             "version": "1.0.1.0", "canUninstall": True}
            restart_plugin = {**active_plugin, "status": "Restart"}
            disabled_plugin = {**active_plugin, "status": "Disabled"}
            transition.update({
                "disable": {"apiStatus": 204, "generationStatusAfterRestart": 404,
                            "inventoryBefore": [active_plugin],
                            "pendingInventory": [restart_plugin],
                            "disabledInventory": [disabled_plugin]},
                "replacement": {"targetFramework": "net10.0",
                                "fromSha256": "8" * 64, "toSha256": "9" * 64},
                "enable": {"apiStatus": 204, "generationStatusBeforeEnable": 404,
                           "migratedDisabledInventory": [disabled_plugin],
                           "pendingInventory": [restart_plugin],
                           "activeInventory": [active_plugin]},
            })
        waves = []
        wave_before = ten_server["generation"]
        for clients, digit in zip((10, 50, 100), (5, 6, 7)):
            wave_after = f"g-{digit:016x}"
            response_identity = {"status": 200, "cacheControl": "no-store",
                                 "contentType": "application/json; charset=utf-8",
                                 "version": "1.0.1.0", "buildId": "f" * 64,
                                 "generation": wave_after, "epoch": "1" * 32}
            waves.append({
                "clients": clients,
                "mutation": {"sequence": digit, "label": f"poll-{clients}"},
                "generationBefore": wave_before,
                "generationAfter": wave_after,
                "allResponsesExact": True,
                "independentSockets": clients,
                "maximumInFlight": clients,
                "startBarrierSpreadMs": 1,
                "waveDurationMs": 10,
                "timingMs": {"min": 1, "p50": 4, "p95": 8, "max": 9,
                             "perRequestLimit": 20_000, "waveLimit": 30_000},
                "responseIdentity": response_identity,
                "responses": [{**response_identity, "client": index, "durationMs": 5,
                               "connectionReused": False} for index in range(clients)],
            })
            wave_before = wave_after
        inventory = [{"id": "515255fe333249b0b4710be58c8221d8",
                      "name": "Jellyfin Refresh Kit", "status": "Active",
                      "canUninstall": True}]
        media_fixture = {
            "deterministicRecipe": "lavfi-testsrc2+sine-120s-h264-aac-v1",
            "localFile": "rk-host-upgrade-120s-v1.mp4",
            "remoteDirectory": MEDIA_REMOTE_DIR,
            "remoteFile": MEDIA_REMOTE_FILE,
            "bytes": 1_000_000,
            "sha256": "6" * 64,
            "remoteSha256": "6" * 64,
            "durationSeconds": 120,
            "library": {
                "name": MEDIA_LIBRARY_NAME,
                "itemId": "3" * 32,
                "locations": [MEDIA_REMOTE_DIR],
            },
            "item": {
                "id": "4" * 32,
                "name": "Refresh Kit Host Upgrade Fixture",
                "path": MEDIA_REMOTE_FILE,
                "type": "Movie",
                "mediaType": "Video",
                "runTimeTicks": 1_200_000_000,
                "mediaSourceCount": 1,
            },
        }
        media_after = {
            "remoteSha256": media_fixture["sha256"],
            "library": copy.deepcopy(media_fixture["library"]),
            "item": copy.deepcopy(media_fixture["item"]),
            "indexedForViewer": True,
        }
        final_roles = [{"name": row["name"], "role": row["role"],
                        "authenticated": row["authenticated"],
                        "userId": row["user"]["id"] if row["user"] else None,
                        "documentId": row["documentId"], "loadCount": row["loadCount"],
                        "hiddenAtCheckpoint": index != 9}
                       for index, row in enumerate(after)]
        phases = [
            {"name": "one-live-tab-converged", "tabs": 1,
             "serverBefore": boot_server, "serverAfter": one_server,
             "pagesBefore": one_before, "pagesAfter": one_after},
            {"name": "two-tabs-two-contexts-users-converged", "tabs": 2,
             "serverBefore": one_server, "serverAfter": two_server,
             "pagesBefore": two_before, "pagesAfter": two_after},
            {"name": "ten-live-tabs-role-mix-converged", "tabs": 10,
             "serverBefore": two_server, "serverAfter": ten_server,
             "pagesBefore": ten_before, "gated": gated, "pagesAfter": ten_after},
            {"name": "generation-poll-stress-and-ten-tab-catch-up",
             "waves": waves, "pagesBefore": stress_before, "pagesAfter": stress_after,
             "diagnosticsGeneration": source_server["generation"]},
            {"name": "in-place-host-upgrade-converged"},
        ]
        return {
            "schemaVersion": 2,
            "scenario": scenario,
            "completed": True,
            "failures": [],
            "unexpectedRefreshKitBrowserErrors": [],
            "versionFlapWarnings": [],
            "probeRemoved": True,
            "captureCounts": {name: {"console": 1, "network": 1, "truncated": False}
                              for name, _, _ in role_rows},
            "transitionWindows": windows,
            "metadata": {
                "immutableSnapshot": "snapshot",
                "from": {"image": source_image, "serverVersion": source_version},
                "to": {"image": target_image, "serverVersion": target_version},
                "sourceIdentity": source,
                "stages": stages,
                "users": users,
            },
            "phases": phases,
            "multiTab": {"tabCounts": [1, 2, 10], "anonymousLoginRoute": "#/login",
                         "finalRoles": final_roles},
            "browserContexts": [
                {"name": "admin", "userId": users["admin"]["id"],
                 "userName": users["admin"]["name"], "authenticated": True},
                {"name": "viewer", "userId": users["viewer"]["id"],
                 "userName": users["viewer"]["name"], "authenticated": True},
                {"name": "anonymous", "userId": None, "userName": None,
                 "authenticated": False},
            ],
            "dialogSafety": {"realJellyfinDialog": True, "role": "dialog",
                             "blockReason": "dialog", "documentIdPreservedWhileOpen": True,
                             "loadCountDeltaWhileOpen": 0, "cancelledWithoutUninstall": True,
                             "inventoryBefore": inventory, "inventoryAfter": copy.deepcopy(inventory)},
            "editorSafety": {"blockReason": "text_entry",
                             "documentIdPreservedWhileEditing": True,
                             "loadCountDeltaWhileEditing": 0, "convergedAfterBlur": True},
            "playbackSafety": {
                "realMediaPlayback": True,
                "source": "viewer-owned indexed MP4 on a real Jellyfin details/playback route",
                "fixture": media_fixture,
                "detailsRoute": {"hash": "#/details?id=" + "4" * 32,
                                 "itemId": "4" * 32, "expectedId": "4" * 32},
                "playbackDetails": {"hash": "#/details?id=" + "4" * 32,
                                    "itemId": "4" * 32, "expectedId": "4" * 32},
                "progressStartSeconds": 1.0,
                "progressEndSeconds": 2.0,
                "progressSeconds": 1.0,
                "blockReason": "media_element",
                "playing": playback_before,
                "gated": playback_gated,
                "paused": playback_paused,
                "leftPlayback": playback_left,
                "documentIdPreservedWhilePlaying": True,
                "loadCountDeltaWhilePlaying": 0,
                "currentGenerationHeldWhileLatestAdvanced": True,
                "pausedWithoutReload": True,
                "exactOneReloadAfterLeave": True,
            },
            "pollStress": waves,
            "pollStressRegressionLink": {
                "source": "plugin/Jellyfin.Plugin.RefreshKit.Tests/ActivePluginGenerationTests.cs",
                "method": "ConcurrentGenerationReadsShareExactlyOneScanPerInvalidation(int readerCount)",
                "cases": [10, 50, 100],
            },
            "hostUpgrade": {
                "sourceImage": source_image, "targetImage": target_image,
                "sourcePublicVersion": source_version, "targetPublicVersion": target_version,
                "volumesPreserved": True, "generationChanged": True, "epochChanged": True,
                "configPreserved": True, "usersPreserved": True,
                "mediaBefore": media_fixture, "mediaAfter": media_after,
                "mediaPreserved": True,
                "browserCacheEnabled": True, "exactOneReloadPerDocument": True,
                "configBefore": copy.deepcopy(EXPECTED_CONFIG),
                "configAfter": copy.deepcopy(EXPECTED_CONFIG),
                "sourceIdentity": source_identity, "targetIdentity": target_identity,
                "openDocumentsBefore": before, "openDocumentsAfter": after,
                "sourceServer": source_server, "targetServer": target_server,
                "sourcePluginDllSha256": stages["net9"]["dllSha256"],
                "targetPluginDllSha256": stages[target_stage]["dllSha256"],
                "transition": transition,
            },
        }

    valid10 = fixture("jf10")
    valid12 = fixture("jf12")
    validate_result(valid10, "jf10", "snapshot")
    validate_result(valid12, "jf12", "snapshot")

    with tempfile.TemporaryDirectory(prefix="rk-host-upgrade-validator-") as temporary:
        root = pathlib.Path(temporary)
        for name, result in (("jf10", valid10), ("jf12", valid12)):
            (root / name).mkdir()
            (root / name / "result.json").write_text(
                json.dumps(result), encoding="utf-8")
        aggregate = {
            "schemaVersion": 1,
            "completed": True,
            "immutableSnapshot": "snapshot",
            "sourceIdentity": valid10["metadata"]["sourceIdentity"],
            "scenarios": {
                "jf10": {"result": "jf10/result.json", "completed": True},
                "jf12": {"result": "jf12/result.json", "completed": True},
            },
            "failures": [],
        }
        (root / "result.json").write_text(json.dumps(aggregate), encoding="utf-8")
        validate_aggregate(root, "snapshot")
        aggregate["scenarios"]["jf12"]["result"] = "/absolute/result.json"
        (root / "result.json").write_text(json.dumps(aggregate), encoding="utf-8")
        try:
            validate_aggregate(root, "snapshot")
        except EvidenceError:
            pass
        else:
            raise EvidenceError("invalid aggregate result path was accepted")

    mutations = []
    changed = copy.deepcopy(valid10)
    changed["pollStress"][2]["responseIdentity"]["cacheControl"] = "public, max-age=60"
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    changed["browserContexts"][1]["userId"] = changed["browserContexts"][0]["userId"]
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    changed["hostUpgrade"]["openDocumentsAfter"][0]["loadCount"] += 1
    mutations.append(changed)
    changed = copy.deepcopy(valid12)
    changed["hostUpgrade"]["transition"]["replacement"]["toSha256"] = "8" * 64
    mutations.append(changed)
    changed = copy.deepcopy(valid12)
    changed["metadata"]["stages"]["net10"]["package"]["sha256"] = "bad"
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    next(phase for phase in changed["phases"]
         if phase["name"] == "ten-live-tabs-role-mix-converged")["pagesAfter"][0]["kit"]["latestEpoch"] = "bad"
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    changed["captureCounts"]["admin-dashboard"]["truncated"] = True
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    changed["pollStress"][2]["responses"][99]["epoch"] = "0" * 32
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    changed["hostUpgrade"]["configAfter"]["PollSeconds"] = 6
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    changed["playbackSafety"]["gated"]["media"]["paused"] = True
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    changed["hostUpgrade"]["mediaAfter"]["remoteSha256"] = "7" * 64
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    changed["playbackSafety"]["exactOneReloadAfterLeave"] = False
    mutations.append(changed)
    changed = copy.deepcopy(valid10)
    changed["schemaVersion"] = 1
    mutations.append(changed)

    failures = 0
    for mutation in mutations:
        try:
            validate_result(mutation, mutation["scenario"], "snapshot")
        except EvidenceError:
            failures += 1
    require(failures == len(mutations), "negative fixtures were not all rejected")
    checks = 4 + len(mutations)
    print(f"host-upgrade retained-evidence validator self-test: {checks}/{checks} PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=pathlib.Path)
    parser.add_argument("--snapshot")
    parser.add_argument("--scenario", choices=("jf10", "jf12", "all"), default="all")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    try:
        if args.self_test:
            self_test()
            return 0
        require(args.root is not None and args.snapshot, "--root and --snapshot are required")
        if args.scenario == "all":
            validate_aggregate(args.root, args.snapshot)
        else:
            validate_result(load(args.root / args.scenario / "result.json"), args.scenario, args.snapshot)
        print(f"host-upgrade retained evidence ({args.scenario}): PASS")
        return 0
    except EvidenceError as error:
        print(f"FATAL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
