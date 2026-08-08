# Confirmed compatibility

A living, evidence-backed list of plugins the jellyfin-refresh-kit has been
tested against on live Jellyfin servers. The plugin sweeps below were run
against v2.0.0; the multi-instance and C# evidence was re-proven on v2.1.0, on
v2.1.1 and again on v2.1.2 (see the last four sections). **Verdicts**: `coexists` = plugin fully
functional with the kit present, zero kit-attributable errors, network-level
non-interference proven; `adoptable` = additionally a candidate to *use* the kit
for its own cache-busting/refresh. Every row cites the test environment that
proved it. Nothing appears here without a passing run.

## Jellyfin 10.11.11 — kitchen-sink environment (8 plugins + 2 kit instances + 3 concurrent index.html rewriters)

| Plugin | Version tested | Class | Verdict | Notes |
|---|---|---|---|---|
| Jellyfin Enhanced (n00bcodr) | 12.1.0.0 | C#, own injection middleware + own `?v=` | **coexists** | JE's tag and cache-buster untouched by the kit; survived every kit-triggered reload; the two `?v=` schemes never crossed |
| JavaScript Injector (n00bcodr) | 3.5.0.0 | C#, web-touching | **coexists** | its `public.js?v=<ticks>` untouched |
| Jellyfin Tweaks (n00bcodr) | 3.1.0.0 | C#, web-touching | **coexists** | |
| File Transformation (IAmParadox27) | 2.5.11.0 | C#, HTML rewriter | **coexists** | kit tags survive FT's Harmony transforms; served HTML idempotent across repeated fetches |
| Plugin Pages (IAmParadox27) | 2.4.11.0 | C#, web-touching | **coexists** | `inject.js` untouched |
| Custom Tabs (IAmParadox27) | 0.2.10.0 | C#, web-touching | **coexists** | |
| Media Bar (IAmParadox27) | 2.4.12.0 | C#, web-touching (CDN assets) | **coexists** | DOM present before and after kit reloads; CDN `slideshowpure.js` untouched |
| Home Screen Sections (IAmParadox27) | 2.5.11.0 | C#, web-touching | **coexists** | its own `?v=2.5.11.0&c=0` untouched |
| KefinTweaks (ranaldsgift) | main @ 290b36f | JS collection | **coexists + adoptable** (kit's flagship adopter — bootstrap mode) | 37-39 sub-assets versioned per release, cache-hit convergence proven. Pre-existing KT bug (not kit-related, proven by kit-absent control): `homeScreen.js:761` calls `getUserViews` before user context resolves → 401/400 noise. Worth reporting upstream. |

Environment: three independent HTML rewriters active at once (on-disk kit tags, JE middleware, File Transformation), real media library, kit-triggered reloads, mid-boot update stress, container-restart cycle with open tab (no reload storm, budget intact). 38/43 assertions passed; all 5 fails attributed to third-party issues via control experiments.

## Jellyfin 10.11.11 — awesome-jellyfin breadth sweep (34 plugins + 2 kit instances)

Web-touching plugins (each verified present-and-functional with the kit active, its own URLs untouched):

| Plugin | Version tested | Verdict | Notes |
|---|---|---|---|
| Jellyfin Enhanced (n00bcodr) | 12.1.0.0 | **coexists** | second environment confirming the kitchen-sink result |
| Intro Skipper | 1.10.11.22 | **coexists** | on 10.11+FT it rewrites the web *bundles*, not index.html — a fourth rewriting mechanism, unaffected |
| File Transformation (IAmParadox27) | 2.5.11.0 | **coexists** | 10+ registered transformations served identically with kit tags present |
| Plugin Pages (IAmParadox27) | 2.4.11.0 | **coexists** | |
| Custom Tabs (IAmParadox27) | 0.2.10.0 | **coexists** | tabs rendered |
| InPlayerEpisodePreview (Namo2) | 1.6.1.2 | **coexists** | |
| Media Preview (spkesDE) | 0.3.1.0 | **coexists** | |
| Editor's Choice (lachlandcp) | 1.5.2.0 | **coexists** | slider needs favourites config; absent identically pre-kit |
| ActorPlus (Druidblack) | 1.0.0.0 | **coexists** | CSS+JS injection intact |
| JMSFusion / MonWUI (G-grbz) | 3.7.0.0 | **coexists** | own `?v=3.7.0.0-…` untouched; its parked ambient `<video>` elements exercise the media safety gate. On 2.0.0/2.1.0 such a tab read `wouldBlockNow: 'media_element'` permanently — and, once an update was pending, layer 3 was starved for good. Since 2.1.1 a never-played element is not a session and does not block (re-proven live, see the 2.1.1 table) |
| Seasonals (CodeDevMLH) | 3.1.0.0 | **coexists** | on-disk tags coexist with kit tags |
| Ratings (K3ntas) | 1.0.359.0 | **coexists** | own `?v=` untouched |
| StarTrack (ZL154) | 1.6.4.0 | **coexists** | own `?v=` untouched |
| JavaScript Injector (n00bcodr) | 3.5.0.0 | coexists (plugin itself degraded) | its client loader never appeared in served HTML in this environment **with or without the kit** — environment/plugin quirk, not interference |

Server-only plugins, all loaded clean alongside the kit (18): Trakt 30.0.0.0, Playback Reporting 17.0.0.0, TMDb Box Sets 13.0.0.0, Fanart 14.0.0.0, Open Subtitles 24.0.0.0, Webhook 21.0.0.0, Bookshelf 13.0.0.0, LDAP-Auth 23.0.0.0, SSO-Auth 4.0.0.4, Merge Versions 10.11.0.1, Theme Songs 10.11.0.2, Auto Collections 0.0.4.1, Streamyfin 0.67.0.0, Ani-Sync 4.4.0.0, ListenBrainz 6.3.1.2, Cinema Mode 0.6.0.0, Jellysleep 0.8.0.0, DiscontinueWatching 0.5.1.0. Plus Skin Manager 2.0.2.0 and Jellyfin Tweaks 3.1.0.0 (loaded; activation needs configuration — not exercised).

Not tested, with reasons: SmartLists, HoverTrailer, NotifySync, Media Cleaner (no manifest at standard locations); TheIntroDB, GhostLibrary, Transcode Nag (10.9-only releases); Dedupe Continue Watching (10.10-only); Shokofin (manifest without parseable targetAbi).

Test evidence: A0 pre-kit console baseline vs A1 diff (all new lines attributed to KefinTweaks' own pre-auth calls), zero kit-version URLs outside the two instances' patterns across the full network log, served index.html md5-stable across 5 fetches with 12 tracked tags exactly once each, one-reload convergence with 40/42 cache hits for the untouched instance, playback gate holding for a 40 s observed playback then converging ~1.4 s after stop, and a container restart with an open tab producing zero spurious reloads.

## Jellyfin 12.0-rc3 — RefreshKit.cs (C# companion)

| Host | Verdict | Notes |
|---|---|---|
| Demo plugin (clean-room JF12 example) | **works** | full curl matrix: idempotent injection, stale-tag scrub, `rk-` ETag + per-encoding 304/412, gzip/br, immutable scripts, DevMode live-flip, same-version DLL swap detected |
| Jellyfin-Enhanced project (compile-only) | **compiles** | jf10 (net9.0) + jf12 (net10.0), zero warnings |

Re-proven on 2.1.0 (same demo plugin, same host): the emitted tag now carries
`data-boot-version="{CacheKey}"` alongside `version=`, and it matches the
`CacheKey` the version endpoint reports byte for byte — injection stayed
idempotent (md5-stable served HTML across 5 fetches, exactly one owned tag) and
the `rk-` ETag still answers a conditional GET with 304. Both compile targets
rebuilt with zero warnings.

## Kit 2.1.0 — behavioural proofs (Jellyfin 10.11.11, two live kit instances)

| Proof | Result |
|---|---|
| Two-instance load (KefinTweaks bootstrap + DemoPack) | 2 instances, **1** interceptor, 39 KT assets at `?v=`, DemoPack at its own version, only the loader itself unversioned |
| One-sided convergence (bump one instance only) | one reload; all 39 unchanged sibling assets served from cache |
| Overlapping `assetPatterns` | exactly one warning, first matching instance with a resolved version wins, page healthy |
| Two physical kit copies, one page | both register through the contract, still one interceptor, URLs versioned exactly once |
| `notify` + `auto` interplay | notify callback fired with no reload; a later auto update reloaded and converged both |
| Budget-refused reload | deferred, not discarded: intent survived the refusal *and* an explicit `checkNow()`, and the reload landed once the 60 s window rolled |
| Flapping version source (A→B→A) | exactly one reload, then one disarm line and no further reload across three polls |
| `data-boot-version` seeding | baseline seeded from the document, the serve-vs-poll disagreement detected as an update; an unmovable seed then rejected once and the tab went stable |
| Unversioned bootstrap entries + endpoint recovery | the recovered version was not adopted as a clean baseline; one guarded reload brought the entries back version-addressed |
| Blocked reload past the ~10 min retry cap | update survived 11 minutes of `media_element` blocking; polling alone resumed it with no user input |
| Duplicate/`#N` registration | a third identical tag deduped into `#2` instead of minting `#3`; the entry chain ran exactly once |
| Keyed window config | reachable under the `instance-<N>` fallback name for a tag with no `data-*`; a keyed `versionUrl` no longer renames its instance |
| Singular window config, two adopters | each adopter's inline config landed on its own tag's instance |
| Manager global clobber attempt | `defineProperty`, assignment and `delete` all bounced; the later kit tag still registered |
| Classic `mode: 'off'` | one version fetch, no polling, layer-2 versioning intact |

## Kit 2.1.1 — behavioural proofs (same host, same two live kit instances)

Every 2.1.0 proof above was re-run on 2.1.1 and stayed green (R0/R3/R4/R6/R7/R8
plus budget-defer, flap-guard, boot-version and the keyed-config pair). New:

| Proof | Result |
|---|---|
| Singular window config with a later pure-`data-*` adopter | adopter A kept its own inline config; adopter B kept its own endpoint, patterns and mode; exactly **one** skip warning. (On 2.1.0 B silently inherited A's whole config.) |
| Keyed config under a `#N` final name | two adoptions deriving the same base name; `JellyfinRefreshKitConfigs['KefinTweaks#2']` applied to the suffixed instance **only** (mode/pollSeconds/versionUrl), and the base instance was untouched |
| Keyed entry defined *below* the kit tags | still not applied (it cannot be), but now reported with one warning after the document parses instead of failing silently |
| Retracted update | `1.0.0 → 2.0.0` detected and armed behind a `not_idle` gate, endpoint rolled back to `1.0.0` → one `RETRACTED` line, `updatePending` cleared, **zero** reloads; a later genuine `3.0.0` re-armed normally |
| Hung version endpoint (request accepted, never answered) | timed out after 10 s with one warning and polling continued on cadence — 2 further polls at +15 s and +30 s, `polling: true` throughout. (On 2.1.0 the loop stopped for the life of the tab.) |
| Parked, never-played `<video>` (`readyState 1`, `currentTime 0`, `played 0`) | `wouldBlockNow: null`; the pending update reloaded normally |
| Played-then-paused `<video>` (`currentTime 1.2`, `played 1`) | `wouldBlockNow: 'media_element'`; the update stayed pending and the tab did not reload |
| Frozen media starvation escape (same played-then-paused element, left alone) | blocked with `media_element` for the whole ladder (`shared.mediaBlockedForMs` tracking wall time 1:1, `blockedRetries` 1→600), then one `parked scenery` warning at 614 s and exactly one reload |
| Blocked reload past the ~10 min retry cap (a *playing* video) | unchanged: 11 minutes of blocking, no starvation escape (playback progress resets the clock every probe), and polling alone resumed it |

## Kit 2.1.2 — behavioural proofs (same host, same two live kit instances)

Every 2.1.0 and 2.1.1 proof above was re-run on 2.1.2 and stayed green (R0/R3/R4/R6/R7/R8,
budget-defer, flap-guard, boot-version, the keyed-config pair, R2a–R2f and the
blocked-reload ladder). Each new proof below was run **twice — once against 2.1.1
as a control** — so the row records both the fixed behaviour and the defect it
replaces.

| Proof | Result |
|---|---|
| `src`/`href` assigned a `URL` instance (`s.src = new URL('scripts/a.js', base)`) | versioned: `…/scripts/a.js?v=1.0.0`, through both the property accessor and `setAttribute`, for `<script src>` and `<link href>`. (On 2.1.1 all four came out bare while the plain-string control worked — layer 2 silently off for that idiom.) |
| `src` assigned a boxed `String` | versioned, accessor and `setAttribute` alike (bare on 2.1.1) |
| `src` assigned a plain object with a `toString` | forwarded **untouched** and unversioned, `toString` called exactly **once** (by the browser, not by the kit) — the same pass-through that keeps a `TrustedScriptURL` intact under a Trusted Types CSP |
| Non-matching `URL` object | left bare, no `?v=` — pattern matching is unchanged by the coercion |
| One nameless `window.JellyfinRefreshKitConfig` payload injected twice (two `data-*`-less tags) | **one** instance, `instance-1`. (On 2.1.1: two live instances, `instance-1` + `instance-2`, both polling the same endpoint.) |
| Anonymous tag registering *after* a named sibling | still `instance-1`, so `JellyfinRefreshKitConfigs['instance-1']` reached it (mode `notify`, its keyed `versionUrl`). On 2.1.1 it was renamed `instance-2`, the keyed entry was silently dropped and the instance ran inert on defaults. |
| `JellyfinRefreshKitConfigs` key matching no instance | exactly one warning naming the dead key and listing the live instances (`"KefinTweaks", "instance-1"`). 2.1.1 emitted nothing. |
| Two auto instances arming in the same tick | **one** navigation, **one** `reloading to pick up` line, **one** budget stamp in both `sessionStorage` and `localStorage`; both instances still converged to `2.0.0`. (On 2.1.1: 2 stamps and 2 reload lines for the same single navigation — two thirds of the default budget spent on one reload.) |
| Singular window config that names nobody (`assetPatterns` + `mode` only), with a later `data-*` adopter | adopter A kept it; adopter B kept `mode: 'auto'` and its own `/DemoPack/` patterns, with exactly **one** skip warning. (On 2.1.1 B inherited A's `mode: 'notify'` *and* A's `/KefinTweaks/` patterns — it stopped versioning its own folder and never auto-reloaded again.) |

RefreshKit.cs on the same demo plugin (Jellyfin 12.0-rc3, :8104), 21/21 curl
assertions: the **source-fallback** `304` (reached by making injection inactive —
an oversized shell past `MaxTransformBodyBytes`) now keeps `Vary: Accept-Encoding`
alongside its `ETag` and `Cache-Control`, and still drops `Content-Encoding`;
its `412` still strips `Vary`, `ETag`, `Last-Modified` and `Content-Type` to
reproduce the host's native shape. Verified against a pre-fix build of the same
plugin, whose source-fallback `304` carried **no** `Vary` at all. Injected-path
`200`/`304`/`412`, the `rk-` ETag and idempotent injection are unchanged. Both
compile targets (jf10 net9.0, jf12 net10.0) rebuilt with zero warnings.

## 2.1.1 C# evidence (unchanged)

RefreshKit.cs on the same demo plugin (Jellyfin 12.0-rc3, :8104): a warm HEAD on
a changed source now answers `304` with **no** `Content-Encoding` (and keeps
`Vary`, as RFC 9110 §15.4.5 requires), and its `412` carries neither
`Content-Encoding` nor `Content-Type` — the host's native shape. Injection,
`rk-` ETag and conditional GET unchanged. `ApplyScriptCacheHeaders(Response)`
(the new no-argument overload) served `immutable` with `DevMode` off and
`no-store` with it on, matching the tag's `dev="true"`.
