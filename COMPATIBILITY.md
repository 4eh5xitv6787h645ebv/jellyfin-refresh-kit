# Compatibility and validation evidence

This file describes the compatibility contract and the evidence that supports
it. It deliberately separates a test harness's coverage from a retained result:
the existence of a command, fixture, or assertion is not a claim that the
current release candidate passed it.

## Evidence rules

The terms used here are intentionally narrow:

- **PASS** means a retained structured result is bound to the exact source
  revision/tree, immutable package snapshot, package digests, host-image digest,
  and completed test phases named by that result.
- **PASS WITH LIMITATION** means the required behavior passed and the result
  also preserves a specific, machine-checked limitation. It is not folded into
  a generic pass.
- **Diagnostic only** means useful evidence exists, but it was produced from a
  dirty or superseded tree, or a later product defect invalidated it as release
  evidence.
- **Quarantined** means the artifact can be inspected but an external service,
  incompatible ABI, destructive mode, or untestable integration is deliberately
  excluded from the runtime verdict.
- **Unsupported** means there is no defensible artifact for that host line.
- **Pending** means the harness exists but no exact current-candidate receipt is
  claimed here.

Release evidence is promoted only from the exact immutable snapshot selected at
the start of a run. Browser, server, proxy, and compatibility results must agree
on that identity; stale success markers and results from a different source tree
are rejected by the harnesses.

## Current evidence ledger

| Evidence family | Current status | What may be claimed |
| --- | --- | --- |
| Fast/static, dual-runtime xUnit, and Chromium suites | Consult the CI/release-validation receipt for the candidate revision | The commands and assertions are documented below; no unbound local count is a release result. |
| Jellyfin 10.11.11 and 12.0.0-rc4 self lifecycle | **Pending** exact post-epoch candidate receipt | The lab covers real install/update/disable/enable/uninstall/reinstall APIs, restarts, playback gating, and open-tab convergence; that coverage is not itself a pass. |
| Genuine third-party v1/v2 lifecycle | **Pending** exact post-epoch candidate receipt | The lab requires automatic `G0 → G1 → G2 → G0 → G2 → G0` convergence, exact process epochs, real assemblies/assets, and no manual-reload fallback. |
| Reverse-proxy/browser matrix | Consult the retained integration receipt for the candidate revision | The harness covers the ordinary strong-validator path, common proxies, subpaths, websockets, a real loose-asset content change, and adversarial caching. |
| Locked nine-matrix ecosystem campaign | **Diagnostic only** for the retained `e7fd…` run; replacement candidate receipt pending | Seven full passes and two `PASS WITH LIMITATION` results were recorded, but the snapshot predates the process-epoch rollback fix and is not release evidence. |

The retained nine-matrix diagnostic ran on 2026-08-09 from source tree
`e7fd674aa36c38996316c52581c477116f6009e16516b23b5269e9771469937a`
and immutable snapshot
`v1.0.1.0-3f1c6b835f9facdc698121610d25eb8c53ba03c1-e7fd674aa36c38996316c52581c477116f6009e16516b23b5269e9771469937a-1786198450-true`.
Its net9 ZIP SHA-256 was
`80e3ae63a964ddc31ce4b6eccf9097a9b83b6838a25365b29e65c8132290aa80`;
its net10 ZIP SHA-256 was
`f2af45428a901b916214e8eee2d0ca779d928ccf297c013340b809161250866b`.
The working-session diagnostic copy is under
`test-results/compat-failures/e7fd-pre-browser-fix-nine-matrix-20260809/`.
`test-results` is intentionally ignored, so that local path is not a durable CI
artifact and must not be cited as release evidence. The summary here documents
the server-side and ordering findings without certifying the current product.

## Declared hosts and exact validation pins

The standalone plugin declares Jellyfin **10.11.x** and **12.x** support. Builds
and live labs use exact inputs:

| Declared line | Package references | Framework / ABI | Pinned live image |
| --- | --- | --- | --- |
| Jellyfin 10.11.x | Controller + Model `10.11.0` | `net9.0` / `10.11.0.0` | `jellyfin/jellyfin:10.11.11@sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db` |
| Jellyfin 12.x | Controller + Model `12.0.0-rc4` | `net10.0` / `12.0.0.0` | `jellyfin/jellyfin:12.0-rc4@sha256:db1df1d111c27ba1f10bb8fce6630892f66eb66b12c2b24e79011453ac18b3db` |

A declared minor range does not imply that every future host minor has already
run. The Jellyfin 12 RC4 result is evidence for that exact host/package pair,
not a promise that a future ABI-breaking host will load the same binary.
The net9 build uses the 10.11.0 floor so its shared MediaBrowser assembly
references match the declared `10.11.0.0` ABI; deterministic package
verification rejects any staged DLL/metadata disagreement before a live lab.

## Locked ecosystem coverage

The current compatibility inventory classifies all **101 Awesome Jellyfin
plugin-section rows** and locks **44 immutable archives**, including **40
testable runtime artifacts**. It is derived from catalog commit
`a60d3d24fe0e16e59518f95ea4743d8996fa81c9` (2026-08-05); the authoritative
catalog snapshot SHA-256 is
`49f57cab3c9122c03bc92da3c17c8125cb15041501ad937e2c89206b4ca23029`.

The machine-enforced classifications are:

| Runtime | Testable | Quarantined | Unsupported | Not relevant | Manual only | Archived |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Jellyfin 10.11.11 | 33 | 3 | 2 | 60 | 2 | 1 |
| Jellyfin 12.0.0-rc4 | 3 | 1 | 34 | 60 | 2 | 1 |

Jellyfin 10.11 testable coverage contains 33 relevant catalog rows. Jellyfin 12
testable coverage contains the dedicated net10/Jellyfin 12 artifacts for
Jellyfin Enhanced, Intro Skipper, and StreamLimit. Seasonals is quarantined on
12 because its archive claims a net9/10.11 ABI, and the remaining relevant rows
have no defensible current Jellyfin 12 artifact. File Transformation is an
additional locked dependency exercised throughout the callback stacks.

Six hostile static fixtures exercise bounded behaviors without pretending to
load incompatible plugins:

- `branding-css`
- `dynamic-static-assets`
- `javascript-broker`
- `jf12-synthetic`
- `legacy-direct-writer`
- `transformation-chain`

Fourteen runtime matrices exercise the selected real artifacts and orderings:

- `jf10-transform-core`
- `jf10-transform-whisper`
- `jf10-transform-hover`
- `jf10-transform-player`
- `jf10-transform-editors`
- `jf10-transform-actor`
- `jf10-middleware-forward`
- `jf10-middleware-reverse`
- `jf10-registration-broker`
- `jf12-enhanced`
- `jf10-response-transformers-forward`
- `jf10-response-transformers-reverse`
- `jf10-direct-writers-readonly`
- `jf10-direct-writers-writable`

The exact artifacts, URLs, digests, metadata, expected tags, quarantine reasons,
and install order live in `e2e/compat/ecosystem.lock.json` and
`e2e/compat/matrices.json`. `e2e/compat/README.md` documents how a retained
runtime result is produced and analyzed.

## Cache and response-ownership contract

There are two valid cache outcomes; they must not be conflated.

### Ordinary final-response ownership

When Refresh Kit owns the final representation on ordinary Kestrel, a safely
transformed identity/gzip/Brotli shell receives a strong body-derived `rk-`
ETag. Matching `If-None-Match` can return `304`, a failed `If-Match` can return
`412`, and `HEAD` uses the selected representation metadata.

### Nested outer-response-buffer ownership

Exactly three locked matrices contain a known outer response owner and use the
statically enforced `safe-degrade` expectation:

- `jf10-middleware-forward`
- `jf10-middleware-reverse`
- `jf12-enhanced`

The complete injected/stamped body must still be returned, but Refresh Kit must
not claim a validator for bytes the outer middleware owns. Primary and stale
conditional responses are therefore full `200` responses with
`Cache-Control: no-store`, no `ETag` or `Last-Modified`, and no stale digest,
signature, trailer, or connection-nominated entity metadata. The outer owner's
final content type and coding remain authoritative.

The `forward` and `reverse` suffixes describe opposite on-disk installation
enumeration only. Jellyfin 10.11 sorts discovered plugins by manifest name,
then ID/version, before loading assemblies and registering services. Each pair
therefore asserts one explicit runtime plugin order and proves that its
shell/cache result is independent of numeric folder prefixes; it does not claim
that reversing folder creation reverses middleware.

For the lab's captured HTTP/1.1 responses, the analyzer additionally requires
exactly one unambiguous framing mode: a single decimal `Content-Length` equal to
the body with no `Transfer-Encoding`, or exactly `Transfer-Encoding: chunked`
with no `Content-Length`. HTTP/2 and HTTP/3 use different transport framing and
are not represented by this header-level check.

### Candid outer-owner limitation

In both Jellyfin 10 middleware install-order matrices, Seasonals' eligible tags must be
stamped with the current `rkv`. GetAvatar adds one eligible tag after Refresh
Kit's transform boundary; the analyzer requires that tag to be present exactly
once and unstamped, and reports the matrix as `PASS WITH LIMITATION`. An
automatic shell reload does not guarantee fresh bytes for that unchanged,
outer-owned URL. The limitation cannot be reclassified as a pass merely because
the page stayed healthy.

## Lifecycle and browser contract

The Jellyfin lab uses the real 10.11.11 and 12.0.0-rc4 plugin repository/package
APIs, real v1/v2 third-party assemblies and assets, and authenticated browser
tabs kept open across required restarts. Staged install, disable, and uninstall
state must remain invisible until restart activates a different loaded MVID and
asset set.

The standalone endpoint supplies an opaque generation plus a process epoch. A
process epoch is stable for one loaded server process, changes after restart,
and never enters generation identity, asset URLs, ETags, or injected HTML. The
browser requires two observations of a fresh exact generation/epoch pair and a
verified per-tab claim before granting one-shot authorization to an already-left
target generation. That authorization remains attached to the target while a
reload is safety-blocked, even if polls rotate through other process epochs
serving the same generation; replica rotation is not a new release or update. A
same-generation restart is recorded without reloading. Invalid, missing, seen,
corrupt, unwritable, or saturated epoch state fails closed to the legacy flap
refusal. A permanent per-tab coverage record also blocks an epoch override for
any generation left before its epoch was durably known; an unresolved baseline
creates an instance-wide tombstone because no exact historical generation can
be named safely. This permits legitimate finite `A → B → A` lifecycle
rollback when its evidence is complete without turning incomplete history or a
finite mixed-node cycle into an endless reload loop.

The reload probes cover observable light-DOM playback routes/media, fullscreen
and picture-in-picture, rendered native/Jellyfin/ARIA dialogs, active editors,
password fields, idle time, hidden-tab settling, and a shared rolling budget.
They do not prove state inside closed shadow roots or external/DRM players.
Background timer throttling or freezing can delay detection.

## Known scope limits

- Runtime-created imports, `fetch()` URLs, JavaScript-created resources, and CSS
  `url()` references remain the owning plugin's responsibility unless it adopts
  the client kit directly.
- Cross-origin resources and CDN resolution caches are not rewritten.
- Middleware ordering limits which serve-time tags are visible to the stamper;
  the GetAvatar result above is the exact retained example.
- A same-version loaded DLL replacement is detected through module MVID. A PE
  byte change that preserves the MVID is not a generation input.
- External services such as ARR, Seerr, OAuth, avatar packs, fonts, and CDNs are
  deliberately unreachable in the isolated compatibility runtime and remain
  quarantined where applicable.
- The lifecycle lab does not perform an in-place Jellyfin host upgrade and does
  not claim Firefox, WebKit, DRM, or external-player coverage.
- A proxy configured to ignore origin `Cache-Control` can still pin both the
  shell and generation endpoint. The client budget rate-limits reloads; it is
  not a repair for a permanently broken intermediary.

## Reproducing and promoting evidence

Use the supported repository entry point:

```bash
./test.sh fast
./test.sh integration
./test.sh compatibility
./test.sh all
```

The heavy commands require their documented Docker gate and exact prerequisites.
Read each lab README before running it. Release claims must cite the retained
CI/release-validation artifact for the exact candidate rather than copying a
terminal summary from another tree.

The pre-2.4.6 chronological compatibility diary described useful earlier
experiments, including v2.0–2.4.1 browser behavior and standalone plugin 1.0.0
installation probes. Those records remain available in Git history and release
tags, but they are historical evidence and are not presented as current
candidate certification here.
