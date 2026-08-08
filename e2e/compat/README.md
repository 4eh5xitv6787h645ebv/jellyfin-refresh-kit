# Third-party compatibility lab

This directory is a disposable, project-scoped harness for Refresh Kit against the audited Awesome Jellyfin UI/customization ecosystem. It consumes existing Refresh Kit stages; it does not call `plugin/build.sh`, alter the release build, or reuse the proxy/Jellyfin E2E state.

No Jellyfin container is started by `static`, `list`, `coverage`, or `fetch`. Runtime commands are deliberately gated and require `RK_COMPAT_ALLOW_CONTAINERS=1` after coordination.

## Safety and reproducibility

- Jellyfin 10.11.11 and the published `12.0-rc4` image (server version 12.0.0-rc4) are pinned by tag and SHA-256 digest. The runner verifies the created container's configured image again.
- Published ports are declared only on `127.0.0.1` (defaults `18216` and `18217`). Some Docker engines suppress host publication for an `internal` network; after verifying the selected container's Compose project/service labels, pinned image digest, exclusive internal-bridge membership, absent gateway, and private bridge IPv4, the runner falls back to that project-owned IPv4 from the host. It never adds an egress-capable network. The Compose network remains `internal`, so plugins cannot contact CDNs, ARR, Seerr, OAuth, or other Internet services during the run.
- Each Jellyfin root filesystem is read-only, with only project-owned `/config`, `/cache`, and `/tmp` writable. This forces safe middleware/File Transformation fallbacks instead of allowing plugins to rewrite the image's webroot.
- There are no fixed container names, host Docker-socket mounts, external volumes, or external networks. Every Docker resource belongs to the validated `rk-compat-*` Compose project.
- Every upstream archive has a fixed GitHub release URL and SHA-256 in `ecosystem.lock.json`. Downloads are capped, written atomically, checked before inspection, and safely extracted with traversal, symlink, member-count, and expanded-size rejection.
- `n00bcodr/Jellyfin-Enhanced` is strictly read-only. The harness only downloads the two locked release assets and inspects them; it contains no GitHub write workflow and never pushes anywhere.
- A quarantined or unsupported artifact cannot reach the materializer, even if its ID is passed directly. Such packages are downloadable for digest inspection only.

The lock is derived from `/tmp/rk-ecosystem-audit.json` at SHA-256 `b9b5431eca9377f5f15b9636775f989f632bf89ae1554db9df68778f26d9bff2`. When that audit file is present, static validation compares every locked URL/digest pair back to it.

## Commands

From this directory:

```bash
./run.sh static
./run.sh list
./run.sh coverage
./run.sh fetch jf10-transform-hover
./run.sh fetch all-locked
```

The runtime consumes the already-built stages at `plugin/build/stage` and `plugin/build/stage-jf12`. Override them with `RK_COMPAT_JF10_STAGE` or `RK_COMPAT_JF12_STAGE` if needed. The stage validator requires the Refresh Kit GUID, four-part version, target ABI, framework, and DLL tokens to agree before Docker starts.

After explicit coordination:

```bash
RK_COMPAT_ALLOW_CONTAINERS=1 ./run.sh run jf10-transform-hover
RK_COMPAT_ALLOW_CONTAINERS=1 ./run.sh all
```

`all` runs nine fresh matrices and writes `artifacts/summary.json`. `clean` removes only this directory's `.cache`, `.state`, `artifacts`, and the `rk-compat-*` Compose project's resources.

## Cache, cleanup, and resource expectations

The content-addressed archive cache is `e2e/compat/.cache/artifacts`. A complete `fetch all-locked` currently contains 24 archives totalling 116,693,199 bytes (about 112 MiB on disk). Cache hits are rehashed and reinspected rather than trusted. `down` removes only the selected Compose project's containers, network, and volumes and keeps the host cache/evidence; `clean` also removes and recreates this harness's `.cache`, `.state`, and `artifacts` directories. Neither command removes Docker images or anything outside `e2e/compat` and the validated `rk-compat-*` project.

Measured container-free costs on the development host are about 3.3 seconds and 200 MiB peak RSS for `static`, and about 1.4 seconds for a warm-cache verification of all 24 archives. A cold fetch transfers 116.7 MB; allow roughly 2–10 minutes on a typical constrained CI network. These are observations/estimates, not runtime-matrix results.

The first complete nine-matrix diagnostic campaign ran from 2026-08-09 03:30:43 to 03:46:00 AWST (15 minutes 17 seconds, including one preserved transient Jellyfin 12 readiness retry). It used source tree `e7fd674aa36c38996316c52581c477116f6009e16516b23b5269e9771469937a` and immutable snapshot `v1.0.1.0-3f1c6b835f9facdc698121610d25eb8c53ba03c1-e7fd674aa36c38996316c52581c477116f6009e16516b23b5269e9771469937a-1786198450-true` (net9 ZIP SHA-256 `80e3ae63a964ddc31ce4b6eccf9097a9b83b6838a25365b29e65c8132290aa80`; net10 ZIP SHA-256 `f2af45428a901b916214e8eee2d0ca779d928ccf297c013340b809161250866b`). Seven matrices were full passes; both Jellyfin 10 middleware-order matrices were `pass-with-limitation` solely for GetAvatar's required single unversioned outer-owner tag. The working-session structured copy is under the ignored path `test-results/compat-failures/e7fd-pre-browser-fix-nine-matrix-20260809/`; it is not a durable artifact or release evidence because a separate browser generation-cycle defect was subsequently confirmed and requires a replacement product snapshot.

The two locally present image manifests report virtual sizes of 1,561,051,923 bytes (10.11) and 1,571,882,784 bytes (12); layers may be shared. Unique extracted plugin payloads total about 137 MiB, while all nine retained host work directories duplicate about 275 MiB. Reserve 5 GiB of disk and 3 GiB of RAM for a cold full run. Only one service is started at a time, it has a 3 GiB memory limit and 256 MiB `/tmp`, and successful matrices remove their project volumes while retaining structured host evidence. The runner resolves `plugin/build` to one verified immutable snapshot before the first matrix and reuses that exact snapshot for the whole run; `RK_COMPAT_BUILD_SNAPSHOT` may select another snapshot under `plugin/.builds` explicitly.

## What a runtime matrix proves

For every selected plugin, the result records and checks:

- the source revision, release URL, archive digest, archive layout, main DLL digest, embedded GUID/version tokens, target framework evidence when a `.deps.json` exists, and upstream `meta.json` when supplied;
- a deterministic install sidecar containing GUID, name, version, target ABI, status, and main assembly (missing fields are completed from the lock without modifying the cached upstream archive);
- exact GUID/name/version/status in Jellyfin's authenticated plugin inventory and `IsLoaded=true` in Refresh Kit diagnostics;
- Refresh Kit's own stage metadata, endpoint load, single shell tag, boot generation, and immutable generation-addressed runtime. Ordinary matrices require an `rk-` ETag and conditional `304`; the three audited outer-response-buffer matrices instead require the explicit safe-degradation contract described below;
- a real generation transition after adding a loose `.js` asset to one loaded third-party plugin, including that plugin's changed asset identity in diagnostics;
- attributed third-party shell tags, current/missing `rkv` stamps, requested install order, observed shell-tag order, and whole-stack coexistence while the server remains healthy.

The `jf10-middleware-forward` and `jf10-middleware-reverse` matrices are exact reverse install orders. Both require both Seasonals tags to carry the current `rkv` stamp. GetAvatar adds its client tag after Refresh Kit's transform boundary in both orders, so the analyzer requires exactly one eligible GetAvatar tag, requires it to remain unstamped, and reports each matrix as `pass-with-limitation`. This is not full stamping compatibility and is not silently folded into a generic pass. Stamping that outer-owned tag inside Refresh Kit would require a second client-side rewrite or broader asset-cache behavior, with duplicate-execution/global-cache risk; the lab therefore records the ownership boundary rather than manufacturing a misleading stamp. Tag serialization order itself is not assumed stable.

Those two matrices and `jf12-enhanced` contain independent middleware that buffers and mutates the response outside Refresh Kit. They are the exact, statically enforced `safe-degrade` cache whitelist. An accepted result still requires a complete injected HTML response, current boot/runtime generation, the same asset multiset on a conditional request, working identity/gzip/Brotli responses, and healthy generation/plugin evidence. Both the primary and stale-conditional responses must be `200`, carry `Cache-Control: no-store`, omit `ETag` and `Last-Modified`, and contain the full body. For the captured HTTP/1.1 responses, each response must also have exactly one valid framing mode: either one decimal `Content-Length` equal to the captured body bytes with `Transfer-Encoding` absent, or exactly `Transfer-Encoding: chunked` with `Content-Length` absent. Both headers, neither header, another transfer coding, duplicate values, or a mismatched length fail; the selected mode is recorded independently for the primary and conditional response. HTTP/2 and HTTP/3 use different transport framing and are not represented by this header-level assertion. A stale or invalid `rk-` validator, a `304`, a truncated body, or merely observing cache-header loss is a failure. Every other cache-required matrix retains the normal `rk-` ETag plus conditional `304` requirement.

## Coverage classification

The machine-readable totals are enforced by static checks:

| Runtime | Testable | Quarantined | Unsupported |
| --- | ---: | ---: | ---: |
| Jellyfin 10.11.11 | 19 | 2 | 2 |
| Jellyfin 12.0.0-rc4 | 1 | 1 | 21 |

Jellyfin 10.11 testable coverage is Achievement Badges, HoverTrailer, InPlayerEpisodePreview, Editor's Choice, Jellyfin Enhanced, JavaScript Injector, Media Preview, JMSFusion, ActorPlus, Collection Sections, Custom Tabs, GetAvatar, Home Screen Sections, Media Bar, Plugin Pages, Ratings, Seasonals, SeerrFin, and StarTrack. File Transformation is an additional locked dependency and is exercised throughout the callback stacks.

Jellyfin 12 testable coverage is intentionally only the dedicated net10/Jellyfin 12 Jellyfin Enhanced artifact. Seasonals is quarantined on 12 because its compatibility claim is contradicted by a net9/targetAbi-10.11 archive. All other server plugins lack a defensible 12 ABI artifact.

Skin Manager and Static Assets are quarantined on 10.11 because their legacy ABIs are unclaimed there. Jellyscrub is unsupported on both current lines because upstream retired it. `jellyfin-icon-metadata` is CSS configuration, not a server plugin, and is represented as a fixture rather than misreported as a load test.

External-service behavior is outside this bounded slice. Seerr, ARR, OAuth, avatar packs, fonts, and CDNs are deliberately unreachable at runtime; per-matrix results list those assertions as quarantined.

## Hostile static fixtures

`fixtures/StaticFixtureHarness.csproj` links the production `ThirdPartyTagStamper.cs` directly and adds no packages. Its six cases cover:

- retired legacy/direct writers, stale markers, stale `rkv`, and the Refresh Kit self-tag;
- Skin Manager-style branding CSS, remote imports/fonts, and dynamic Static Assets URLs;
- JavaScript broker serialization decoys and quoted-attribute traps;
- duplicate transformation callbacks, versioned resources, and content-hashed bundles;
- dynamic assets, CDN isolation, and non-script/link resources;
- a Jellyfin 12 synthetic ES-module/strict-CSP shell where the transform ecosystem does not exist.

These fixtures prove conservative stamping and idempotence without loading ABI-incompatible or destructive plugins. They do not substitute for the real install/load checks assigned to testable artifacts.

## Evidence layout

Each `artifacts/<matrix>/` directory contains raw server/plugin/diagnostic JSON, before/after generations, shell bodies and headers, compression captures, install metadata, verified network/origin selection in `network.json`, the Jellyfin log, and `result.json`. Each plugin row in `result.json` carries its artifact, metadata, inventory, load, generation, shell, order, and outcome evidence. A failed phase still emits a small failure result and retains evidence for inspection; a successful run removes its disposable containers and volumes.

The Editor's Choice matrix preloads the upstream-documented, read-only-compatible mode (`DoScriptInject=false`, `FileTransformation=true`) before the plugin startup task runs, then verifies the effective Jellyfin configuration and still requires the real Editor's Choice tag to be generation-stamped. Its default direct-write mode is intentionally not treated as a pass on the read-only Jellyfin Web filesystem.
