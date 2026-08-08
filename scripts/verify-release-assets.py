#!/usr/bin/env python3
"""Read-only comparison of published GitHub release assets to verified ZIPs."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


GITHUB_API = "https://api.github.com"
USER_AGENT = "jellyfin-refresh-kit-release-verifier/1"


class AssetError(ValueError):
    pass


def trusted_download_host(hostname: str) -> bool:
    hostname = hostname.lower()
    return (
        hostname in {"api.github.com", "github.com", "www.github.com"}
        or hostname.endswith(".githubusercontent.com")
        or hostname.endswith(".blob.core.windows.net")
    )


class TrustedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Constrain redirects and strip API credentials at every host boundary."""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        redirected = super().redirect_request(
            request, fp, code, message, headers, new_url
        )
        if redirected is None:
            return None
        old = urllib.parse.urlparse(request.full_url)
        new = urllib.parse.urlparse(redirected.full_url)
        if new.scheme != "https" or not trusted_download_host(new.hostname or ""):
            raise AssetError(f"refusing untrusted GitHub redirect: {new_url}")
        if (old.hostname or "").lower() != (new.hostname or "").lower():
            sensitive = {"authorization", "cookie", "proxy-authorization"}
            for name, _ in tuple(redirected.header_items()):
                if name.lower() in sensitive:
                    redirected.remove_header(name)
        return redirected


def trusted_urlopen(request: urllib.request.Request, timeout: int):
    return urllib.request.build_opener(TrustedRedirectHandler()).open(
        request, timeout=timeout
    )


def hashes(path: pathlib.Path) -> dict[str, object]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            sha256.update(chunk)
            md5.update(chunk)
    return {"bytes": size, "sha256": sha256.hexdigest(), "md5": md5.hexdigest()}


def api_json(url: str, token: str | None) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with trusted_urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise AssetError("GitHub release API returned a non-object")
    return value


def select_assets(release: dict[str, Any], names: tuple[str, str]) -> dict[str, dict[str, Any]]:
    rows = release.get("assets")
    if not isinstance(rows, list):
        raise AssetError("GitHub release API response has no assets array")
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            raise AssetError("GitHub release contains an invalid asset row")
        name = str(row["name"])
        if name in selected:
            raise AssetError(f"GitHub release repeats asset {name}")
        selected[name] = row
    if set(selected) != set(names):
        missing = sorted(set(names) - set(selected))
        unexpected = sorted(set(selected) - set(names))
        raise AssetError(
            "GitHub release asset inventory is not exact: "
            f"missing={missing}, unexpected={unexpected}"
        )
    return selected


def validate_release_state(release: dict[str, Any], tag: str, kind: str) -> int:
    if release.get("tag_name") != tag:
        raise AssetError(f"release API returned tag {release.get('tag_name')!r}")
    if release.get("draft") is not False:
        raise AssetError("release is still a draft")
    expected_prerelease = kind == "milestone"
    if release.get("prerelease") is not expected_prerelease:
        raise AssetError(
            f"{kind} release prerelease flag is {release.get('prerelease')!r}, "
            f"expected {expected_prerelease}"
        )
    published_at = release.get("published_at")
    if not isinstance(published_at, str):
        raise AssetError("release has no published_at timestamp")
    try:
        parsed = dt.datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise AssetError(f"release has invalid published_at timestamp: {published_at!r}") from error
    if parsed.tzinfo is None:
        raise AssetError("release published_at timestamp lacks a timezone")
    return int(parsed.timestamp())


def remote_hashes(url: str, expected_bytes: int) -> dict[str, object]:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {"github.com", "www.github.com"}:
        raise AssetError(f"refusing non-GitHub asset URL: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    sha256 = hashlib.sha256()
    md5 = hashlib.md5(usedforsecurity=False)
    size = 0
    with trusted_urlopen(request, timeout=60) as response:
        final = urllib.parse.urlparse(response.geturl())
        if final.scheme != "https" or not trusted_download_host(final.hostname or ""):
            raise AssetError("published release asset redirected to an untrusted host")
        while True:
            chunk = response.read(min(1024 * 1024, expected_bytes + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > expected_bytes:
                raise AssetError("published release asset exceeds the verified byte length")
            sha256.update(chunk)
            md5.update(chunk)
    return {"bytes": size, "sha256": sha256.hexdigest(), "md5": md5.hexdigest()}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True, help="GitHub owner/repository")
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-kind", choices=("milestone", "final"), required=True)
    parser.add_argument("--build-dir", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository) is None:
        raise AssetError("repository must be owner/name")
    if args.tag != f"v{args.version}" or re.fullmatch(r"v\d+\.\d+\.\d+\.\d+", args.tag) is None:
        raise AssetError("tag must be v<four-part-version> and agree with --version")

    build = args.build_dir.resolve(strict=True)
    names = (
        f"jellyfin-refresh-kit_{args.version}.zip",
        f"jellyfin-refresh-kit_{args.version}_jf12.zip",
    )
    local = {}
    for name in names:
        path = build / name
        if not path.is_file():
            raise AssetError(f"verified local package is missing: {path}")
        local[name] = hashes(path)

    endpoint = (
        f"{GITHUB_API}/repos/{urllib.parse.quote(args.repository, safe='/')}/releases/tags/"
        f"{urllib.parse.quote(args.tag, safe='')}"
    )
    release = api_json(endpoint, os.environ.get("GITHUB_TOKEN"))
    published_epoch = validate_release_state(release, args.tag, args.release_kind)
    selected = select_assets(release, names)

    verified_assets = []
    for name in names:
        row = selected[name]
        url = row.get("browser_download_url")
        if not isinstance(url, str):
            raise AssetError(f"release asset has no download URL: {name}")
        if row.get("size") != local[name]["bytes"]:
            raise AssetError(
                f"published size for {name} is {row.get('size')}, expected {local[name]['bytes']}"
            )
        remote = remote_hashes(url, int(local[name]["bytes"]))
        if remote != local[name]:
            raise AssetError(f"published bytes for {name} do not match the verified candidate")
        api_digest = row.get("digest")
        if api_digest not in (None, f"sha256:{remote['sha256']}"):
            raise AssetError(f"GitHub API digest disagrees for {name}: {api_digest}")
        verified_assets.append({
            "name": name,
            "assetId": row.get("id"),
            "url": url,
            **remote,
        })

    result = {
        "schemaVersion": 1,
        "repository": args.repository,
        "tag": args.tag,
        "releaseId": release.get("id"),
        "releaseUrl": release.get("html_url"),
        "publishedAt": release.get("published_at"),
        "publishedAtEpoch": published_epoch,
        "releaseKind": args.release_kind,
        "assets": verified_assets,
        "allPassed": True,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"==> Published release assets match verified local bytes for {args.tag}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        AssetError,
        FileNotFoundError,
        json.JSONDecodeError,
        urllib.error.HTTPError,
        urllib.error.URLError,
    ) as error:
        print(f"FATAL: {error}", file=sys.stderr)
        raise SystemExit(1)
