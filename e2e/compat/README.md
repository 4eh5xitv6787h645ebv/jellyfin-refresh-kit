# Third-party compatibility lab

This directory is a disposable, project-scoped harness for Refresh Kit against every row in the audited Awesome Jellyfin plugin section. It consumes existing Refresh Kit stages; it does not call `plugin/build.sh`, alter the release build, or reuse the proxy/Jellyfin E2E state.

No Jellyfin container is started by `static`, `list`, `coverage`, or `fetch`. Runtime commands are deliberately gated and require `RK_COMPAT_ALLOW_CONTAINERS=1` after coordination.

## Safety and reproducibility

- Jellyfin 10.11.11 and the published `12.0-rc4` image (server version 12.0.0-rc4) are pinned by tag and SHA-256 digest. The runner verifies the created container's configured image again.
- Published ports are declared only on `127.0.0.1` (defaults `18216`, `18217`, and `18218` for the disposable writable-webroot service). Some Docker engines suppress host publication for an `internal` network; after verifying the selected container's Compose project/service labels, pinned image digest, exclusive internal-bridge membership, absent gateway, and private bridge IPv4, the runner falls back to that project-owned IPv4 from the host. It never adds an egress-capable network. The Compose network remains `internal`, so plugins cannot contact CDNs, ARR, Seerr, OAuth, or other Internet services during the run.
- Each Jellyfin root filesystem is read-only, with only project-owned `/config`, `/cache`, and `/tmp` writable. Thirteen matrices retain a read-only image webroot. The one direct-writer matrix mounts a fresh named volume at `/jellyfin/jellyfin-web`; Docker initializes it from the pinned image, it is destroyed with the project, and no other root path becomes writable.
- There are no fixed container names, host Docker-socket mounts, external volumes, or external networks. Every Docker resource belongs to the validated `rk-compat-*` Compose project.
- Every upstream archive has a fixed GitHub release URL and SHA-256 in `ecosystem.lock.json`. Downloads are capped, written atomically, checked before inspection, and safely extracted with traversal, symlink, member-count, and expanded-size rejection.
- `n00bcodr/Jellyfin-Enhanced` is strictly read-only. The harness only downloads the two locked 12.2 release assets and inspects them; it contains no GitHub write workflow and never pushes anywhere.
- A quarantined or unsupported artifact cannot reach the materializer, even if its ID is passed directly. Such packages are downloadable for digest inspection only.

The authoritative snapshot is `catalog.snapshot.json` at SHA-256 `49f57cab3c9122c03bc92da3c17c8125cb15041501ad937e2c89206b4ca23029`. It records all 101 plugin-section rows from Awesome Jellyfin commit `a60d3d24fe0e16e59518f95ea4743d8996fa81c9` (2026-08-05), including repository activity evidence. Static validation requires `ecosystem.lock.json` to classify those rows in exact index/category/name/repository order, exactly once, and requires every testable row to map to a current immutable artifact and matrix.

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

`all` runs 14 fresh matrices and writes `artifacts/summary.json`. `clean` removes only this directory's `.cache`, `.state`, `artifacts`, and the `rk-compat-*` Compose project's resources.

## Cache, cleanup, and resource expectations

The content-addressed archive cache is `e2e/compat/.cache/artifacts`. A complete `fetch all-locked` contains 44 archives totalling 217,738,784 bytes (about 208 MiB; about 212 MiB allocated on the development host). Cache hits are rehashed and reinspected rather than trusted. `down` removes only the selected Compose project's containers, network, and volumes and keeps the host cache/evidence; `clean` also removes and recreates this harness's `.cache`, `.state`, and `artifacts` directories. Neither command removes Docker images or anything outside `e2e/compat` and the validated `rk-compat-*` project.

The 44-archive container-free fetch/inspection and the 19 new package materializations are separate from runtime evidence: they prove immutable downloads, safe ZIP structure, binary identity tokens, framework evidence where packaged, and install sidecars without starting Jellyfin. Runtime duration for the expanded 14-matrix campaign must be measured by its first coordinated container run.

The earlier complete nine-matrix diagnostic campaign remains historical evidence only. It pre-dates this 101-row/44-artifact/14-matrix expansion. The first expanded campaign stopped fail-closed in the initial transform matrix when WhisperSubs exposed an upstream exact-key/regex registration conflict; the resulting isolated Whisper matrix has container evidence, while a complete 14-matrix campaign still requires a coordinated run.

Reserve 5 GiB of disk and 3 GiB of RAM for a cold full run. Only one service is started at a time, it has a 3 GiB memory limit and 256 MiB `/tmp`, and successful matrices remove their project volumes while retaining structured host evidence. The runner resolves `plugin/build` to one verified immutable snapshot before the first matrix and reuses that exact snapshot for the whole run; `RK_COMPAT_BUILD_SNAPSHOT` may select another snapshot under `plugin/.builds` explicitly.

## What a runtime matrix proves

For every selected plugin, the result records and checks:

- the source revision, release URL, archive digest, archive layout, main DLL digest, embedded GUID/version tokens, target framework evidence when a `.deps.json` exists, and upstream `meta.json` when supplied;
- a deterministic install sidecar containing GUID, name, version, target ABI, and status; explicit upstream assembly whitelists are preserved, while missing or empty whitelists retain Jellyfin's normal load-all-packaged-DLL behavior (missing fields are completed from the lock without modifying the cached upstream archive);
- exact GUID/name/version/status in Jellyfin's authenticated plugin inventory and `IsLoaded=true` in Refresh Kit diagnostics;
- Refresh Kit's own stage metadata, endpoint load, single shell tag, boot generation, and immutable generation-addressed runtime. Ordinary matrices require an `rk-` ETag and conditional `304`; the three audited outer-response-buffer matrices instead require the explicit safe-degradation contract described below;
- a real generation transition after adding a loose `.js` asset to one loaded third-party plugin, including that plugin's changed asset identity in diagnostics;
- third-party shell tags attributed only by structured tag/origin/normalized-path/parsed-query selectors—never query-substring matching—plus exact per-selector and per-artifact cardinality, requested install order, observed shell-tag order, and whole-stack coexistence while the server remains healthy. Shell parsing follows browser behavior by retaining the first duplicate attribute, consuming HTML entities exactly once, ignoring inert template/noscript/foreign content and non-executable script or non-CSS link types (including legacy `language` rules), normalizing HTTP(S) backslash paths, rejecting malformed explicit special-scheme bases as untrusted, and resolving relative live assets against the first effective `<base href>` and the selected origin that exactly matches retained loopback/internal network identity; the synthetic normalization host is never trusted as a real origin;
- distinct shell effects: every `current-rkv` tag must carry exactly the current generation, every `source-versioned` tag must be same-origin with one nonempty recognized version query and no `rkv`, every PowerToys `assembly-versioned-path` tag must match its exact same-origin assembly/resource MD5 route with no query, external assets must match an exact HTTPS authority/path, outer-unversioned tags remain an explicit limitation, and expected-absent identities must have zero matches even when an unexpected extra query key or request-irrelevant fragment is present;
- configured opt-in interaction modes (currently Gelato JavaScript injection), required public/authenticated JavaScript Injector content markers (including Media Preview), source-proven exact-cardinality inline functional markers for Custom Tabs, Media Bar, MDBList Ratings, and three PowerToys packages, plus Thumbnail Previews' deferred-script content marker;
- raw on-disk `index.html` before/after evidence for both direct-writer matrices. Read-only bytes and all source-proven raw markers must remain unchanged/absent; the disposable writable volume must change and contain every expected marker exactly once.

`matrices.json` is a fail-closed schema. Static validation rejects unknown fields at every manifest/selector/requirement level, any artifact-keyed use of `@refresh-kit`, duplicate or ambiguous exact selectors, requirement-list drift, and any change to the exact runtime declarations, 14 IDs, purposes, install orders, reverse pairs, runtime/service/webroot modes, cache/stamping modes, generation probes, body/config/content contracts, selector cardinalities, direct-disk contracts, or quarantined assertions. The audited contract has a pinned canonical SHA-256 and is exercised by positive and negative no-container regressions.

The `jf10-middleware-forward` and `jf10-middleware-reverse` matrices remain exact reverse on-disk install orders. Their positive contracts include the source-versioned Jellyfin Enhanced, Achievement Badges, Ratings, JMSFusion, and StarTrack tags as well as the current-generation Seasonals pair. A second exact reverse on-disk pair exercises Jellyfin Security plus all four web-interacting PowerToys packages and requires their independent response transformations to remain visible. Jellyfin sorts both pairs by manifest name before runtime registration, so each result records and enforces the same expected/observed runtime plugin order rather than falsely claiming that folder prefixes reverse middleware. The direct-writer pair distinguishes safe read-only degradation from real writes on the disposable webroot volume. GetAvatar remains the only explicit outer-owner unversioned limitation.

The core transform contract fixes two previously invisible classes of effect. Home Screen Sections is matched by its exact `/HomeScreen/home-screen-sections.css` and `.js` paths (both source-versioned with `v=2.5.11.0&c=0`), rather than the non-existent `homescreensections` URL substring. MDBList Ratings is inline-only and must emit its start marker, functional `window.__mdbListRatingsIconPatchApplied = true;` sentinel, and end marker exactly once and in that order.

PowerToys' `/_/...` paths encode an MD5 of the assembly full name and embedded-resource name; they are assembly-versioned routes, not content hashes. A same-version rebuild can therefore keep the same URL. The matrix locks each exact route and separately fetches Thumbnail Previews' deferred JavaScript and Remote Trailers' transformed `/web/config.json` content so a route-shaped decoy cannot satisfy the functional contract.

WhisperSubs registers `(^|/)index\.html$`, while File Transformation 2.5.11 stops at an exact `index.html` pipeline when exact and regex registrations coexist. `jf10-transform-whisper` therefore proves the genuine WhisperSubs serve-time callback by itself on the read-only webroot; `jf10-direct-writers-writable` separately proves its direct-write fallback. The exact-key transform chain remains in `jf10-transform-core`, and the isolation is recorded as an upstream coexistence limitation rather than silently accepting a missing tag.

The two middleware install-order matrices and `jf12-enhanced` are the exact, statically enforced `safe-degrade` cache whitelist. An accepted result still requires a complete injected HTML response, current boot/runtime generation, the same asset multiset on a conditional request, working identity/gzip/Brotli responses, and healthy generation/plugin evidence. Both the primary and stale-conditional responses must be `200`, carry `Cache-Control: no-store`, omit `ETag` and `Last-Modified`, and contain the full body. Every other cache-required matrix retains the normal `rk-` ETag plus conditional `304` requirement.

## Coverage classification

The machine-readable totals are enforced by static checks:

| Runtime | Testable | Quarantined | Unsupported | Not relevant | Manual only | Archived |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Jellyfin 10.11.11 | 33 | 3 | 2 | 60 | 2 | 1 |
| Jellyfin 12.0.0-rc4 | 3 | 1 | 34 | 60 | 2 | 1 |

Jellyfin 10.11 testable coverage contains 33 relevant catalog rows. In addition to the original UI slice, it now includes Intro Skipper, Discontinue Watching, Jellysleep, StreamLimit, Gelato, Letterboxd Sync, NotifySync, Jellyfin Security, AniLiberty STRM, four PowerToys packages, JellyfinTweaks, WhisperSubs, Jellyfin Oscars, and MDBList Ratings. File Transformation is an additional locked dependency and is exercised throughout the callback stacks.

Jellyfin 12 testable coverage is the dedicated net10/Jellyfin 12 artifacts for Jellyfin Enhanced, Intro Skipper, and StreamLimit. Seasonals is quarantined on 12 because its compatibility claim is contradicted by a net9/targetAbi-10.11 archive. All other relevant rows lack a defensible current Jellyfin 12 artifact.

Skin Manager, Static Assets, and Moonbase are quarantined on 10.11 because their packages target legacy ABIs; Moonbase's inspected 2.0.3 package is net8/targetAbi 10.10 and is never installed. Jellyscrub is unsupported on both current lines because upstream retired it. `jellyfin-icon-metadata` is CSS configuration, not a server plugin, and both `jellyfin-rpc` rows are companion executables rather than server plugins. These distinctions are machine-readable instead of being silently omitted.

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

Each `artifacts/<matrix>/` directory contains raw server/plugin/diagnostic JSON, before/after generations, shell bodies and headers, compression captures, install metadata, verified network/origin selection in `network.json`, the Jellyfin log, and `result.json`. Direct-writer matrices additionally retain raw `webroot-before.html` and `webroot-after.html`. Each plugin row in `result.json` carries its artifact, metadata, inventory, load, generation, structured shell attribution/cardinality, order, and outcome evidence. A failed phase still emits a small failure result and retains evidence for inspection; a successful run removes its disposable containers and volumes.

The Editor's Choice matrix preloads the upstream-documented, read-only-compatible mode (`DoScriptInject=false`, `FileTransformation=true`) before the plugin startup task runs, then verifies the effective Jellyfin configuration and still requires the real Editor's Choice tag to be generation-stamped. Its default direct-write mode is intentionally not treated as a pass on the read-only Jellyfin Web filesystem.
