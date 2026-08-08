# Confirmed compatibility

A living, evidence-backed list of plugins the jellyfin-refresh-kit has been
tested against on live Jellyfin servers. The first two plugin sweeps below were
run against v2.0.0; the multi-instance and C# evidence was re-proven on v2.1.0,
v2.1.1 and v2.1.2; the ecosystem completion sweep and the widened kitchen-sink
were run on v2.2.0; and the multi-copy/manager-handoff evidence is v2.3.0. The
final section covers the standalone plugin. **Verdicts**: `coexists` = plugin fully
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

## Jellyfin 10.11.11 — ecosystem completion sweep, kit 2.2.0 (103 additional plugin builds + 2 kit instances)

Second breadth pass, covering everything in the awesome-jellyfin list and the
official `repo.jellyfin.org` catalog that the first sweep did not reach. 103
plugin builds installed cumulatively on one server (**102 third-party plugins
Active at once**, alongside two live kit instances: `RKSweep` in bootstrap mode
and `DemoPack` in classic mode). File Transformation 2.5.11.0, Plugin Pages
2.4.11.0 and Home Screen Sections 2.5.11.0 were installed as infrastructure so
the FT-dependent adopters actually inject.

Web-touching plugins (each verified Active, its client assets served 200, and
its own URLs untouched by the kit):

| Plugin | Version tested | Verdict | Notes |
|---|---|---|---|
| Achievement Badges (ZL154) | 2.2.0.0 | **coexists** | three client-script tags at its own `?v=2.2.0.0-cdff799…` untouched; injects via File Transformation + Plugin Pages |
| HoverTrailer (Fovty) | 0.3.1.0 | **coexists** | `/HoverTrailer/ClientScript` 200 (first ledger entry — previously listed "no manifest") |
| GetAvatar (cedev-1) | 1.6.4.1 | **coexists** | `../GetAvatar/ClientScript` 200 |
| SeerrFin (varunaditya-plus) | 1.6.5.1 | **coexists** | 5 JS + 4 CSS at its own `?v=1.6.5.1&c=0` untouched; `seerrFin*` globals live |
| StreamLimiter (JellyboxAD) | 1.1.0.0 | **coexists** | `/StreamLimit/inject.js` tag survives alongside the kit tags |
| LetterboxdSync (Gizmo091) | 2.2.0.0 | **coexists** | `ClientScript` 200 |
| NotifySync (peterdu1109) | 5.7.19.0 | **coexists** | `/NotifySync/client.js` + `/NotifySync/Data` 200 (previously listed "no manifest") |
| Jellyfin Security / TwoFactorAuth (ZL154) | 2.5.21.0 | **coexists** | its `?v=2.5.21.0-b7532c9b` cache-buster untouched; a fifth independent index.html rewriter |
| Moonbase / Moonfin (Moonfin-Client) | 2.0.3.0 | **coexists** | `../Moonfin/Web/loader.js` 200; ~300 MB Flutter web payload served intact |
| MDBList Ratings (Druidblack) | 1.0.0.7 | **coexists** | `__mdbListRatingsIconPatchApplied` global set, `WebClientSettings` 200 |
| AniLiberty STRM (queukat) | 2.0.0.12 | **coexists** | own `?v=2.0.0.11.2` tag untouched |
| Newsletters (Sanidhya30) | 1.6.4.0 | **coexists** | |
| TeleJelly (hexxone) | 1.0.11 | **coexists** | login-page assets not exercised |
| Local Posters (NooNameR) | 0.2.0.2 | **coexists** | |
| The Dwarf's Hammer (Kamoba) | 1.0.0.0 | **coexists** | client script not injected in default config |
| PhoenixAdult (DirtyRacer1337) | 2.7.0.47 | **coexists** | 10.8 targetAbi, loads and runs on 10.11.11 |
| Privacy Mode / Remote Trailers / Thumbnail Previews / JellyTag (jellyfin-powertoys) | 1.3.0.0 | **coexists** | `powertoys/RemoteTrailers` global registered |
| Gelato (lostb1t) | 0.26.15.1 | **coexists** | tested with Meilisearch removed — the two are mutually exclusive (see below) |
| WhisperSubs (GeiserX) | 4.6.0.1 | **coexists** (plugin defect) | line 319 calls `MutationObserver.observe(document.body, …)` from a **synchronous `<head>` script**, so it throws `TypeError … parameter 1 is not of type 'Node'` on every load. Proven by a kit-absent control that throws identically. Worth reporting upstream |

Admin-config-page-only plugins (dashboard JS/CSS, no index.html injection), all
loaded clean with the kit and their config pages served 200: SmartLists
10.11.30.2, Shokofin 6.0.5.11, Media Cleaner 3.2.0.0, Discord Notifier 1.8.0.0,
Telegram Notifier 12.3.0.0, Mediathek Downloader 0.8.2.0, Xtream Library
1.42.1.0, JellySTRMprobe 1.2.0.0, QualityGate 3.3.6.0, Jellyfin Oscars 1.0.6.0,
Jellynext 1.3.0.0.

Server-only plugins, all loaded clean alongside the kit (68): ACdb.tv 3.0.0.4,
Air Times 0.0.2.0, AniDB 11.0.0.0, AniList 13.0.0.0, AniSearch 6.0.0.0, Anime
Multi Source 1.0.5.0, AnimeThemes 6.0.0.0, Apple Music 3.0.6.2, Artwork 2.0.0.0,
Artwork Multi Source 1.0.0.0, Cast Curator 1.3.0.0, Chapter Segments Provider
4.0.0.0, Collection Import 0.48.0.0, Collection Sections 2.3.10.0, Comic Vine
1.0.0.0, Continue Watching Deduplicator 1.0.1.0, Cover Art Archive 9.0.0.0, DLNA
11.0.0.0, Discogs 2.0.0.0, Enigma2 6.0.0.0, Favorited Songs Playlist 1.0.0.2,
GhostLibrary 1.0.0.14, Google Books 1.0.0.0, Harmonie 1.6.1.1, Hikka 1.0.2.0,
IMDb Ratings 1.0.0.20, IMVDb 5.0.0.0, JF To Stash Sync 1.0.0.2, Jellyfin Ignore
0.5.0.0, JustWatch 10.11.0.5, Kinopoisk 10.10.3.0, Kitsu 7.0.0.0, Kodi Sync
Queue 15.0.0.0, Language Tags 0.5.3.0, Local Intros 4.0.0.0, Local
Recommendations 0.6.1.0, LrcLib Lyrics 3.0.0.0, Meilisearch 1.11.1.15, Mind the
Gaps 10.11.6.0, MusicTags 10.11.3.3, MyAnimeList 11.11.1.1, MyAnimeSync
1.6.2.2, Newsletters (Cloud9) 0.6.5.0, NextPVR 13.0.0.0, OPDS 7.0.0.0, Playlist
Generator 1.5.1.0, Plexyfin 0.6.3.0, ProviderStuff 1.2.0.0, RemoteUpload
1.8.0.0, Reports 18.0.0.0, Session Cleaner 5.0.0.0, Shikimori 5.0.0.0, Simkl
8.0.0.0, SmartCovers 7.3.2.0, Stash 1.2.0.3, Static Assets Manager 0.0.1.0,
Studio Curator 1.3.0.0, Subtitle Extract 7.0.0.0, TVHeadend 13.0.0.0, TVmaze
13.0.0.0, TheIntroDB 1.0.7.2, ThePornDB 1.6.0.11, TheTVDB 22.0.0.0, Transcode
Killer 4.0.0.0, Transcode Nag 1.0.1.26, VGMdb 5.0.0.0, Watch History Janitor
1.3.0.0, YouTube Metadata 1.0.3.15.

Test evidence: 107 plugins loaded with **zero non-Active** entries and zero
kit-attributed console lines across 9 kit runs and 6 kit-absent controls; the
kit versioned exactly its own 6 assets and left all 13 third-party `?v=` URLs
byte-identical; served `index.html` md5-stable across repeated fetches with five
independent rewriters active (on-disk kit tags, File Transformation, Jellyfin
Security, Achievement Badges, Moonfin); 150/150 plugin configuration pages served
200; and a version bump converged in 18 s with one reload while the untouched
sibling instance's assets all came back from cache.

Retractions from the previous sweep's "not tested" list: **SmartLists,
HoverTrailer, NotifySync, Media Cleaner, TheIntroDB, GhostLibrary, Transcode
Nag, Dedupe Continue Watching and Shokofin are all testable and all pass.** A
`targetAbi` below the server version is not a blocker — Jellyfin only requires
`targetAbi ≤ server`, so the 10.8/10.9/10.10-ABI plugins above load and run on
10.11.11.

Not tested, with reasons: **jellyfin-icon-metadata** (bare CSS snippets, not a
compiled plugin — no release or manifest); **jellyfin-plugin-onepace** (latest
release carries zero assets); **jellyfin-rpc** ×2 (standalone Discord apps, not
server plugins). Attempted and failed for upstream reasons, not kit-related:
**Jellyscrub 2.1.0.0** (`TypeLoadException: Jellyfin.Data.Entities.TrickplayInfo`
— removed in 10.11; Jellyfin deletes the plugin directory) and **Wikipedia
Episode Order 1.0.26.0** (its own release zip ships 1 DLL while its `meta.json`
declares 4 assemblies → *Malfunctioned*). Also found: **Gelato and Meilisearch
cannot coexist** — both decorate `IItemRepository` and the server refuses to
start with an `InvalidCastException`; each passes on its own.

## Jellyfin 10.11.11 — kitchen-sink revalidation, kit 2.2.0 (22 plugins + 2 kit instances)

The kitchen-sink environment rebuilt on 2.2.0 and widened from 8 concurrent
plugins to 22. All 22 targets installed and passing **at once** (27 plugins
Active including the bundled ones), with two live kit instances: `KefinTweaks`
in bootstrap mode, self-hosted from `main @ 290b36f` with 38 sub-assets, and
`DemoPack` in classic mode. Contract version 2.

| Plugin | Version tested | Verdict | Notes |
|---|---|---|---|
| Jellyfin Enhanced (n00bcodr) | 12.1.0.0 | **coexists** | third environment confirming the earlier results |
| JavaScript Injector (n00bcodr) | 3.5.0.0 | **coexists** | fully functional here (unlike the breadth sweep): its loader appeared and its custom script executed |
| Jellyfin Tweaks (n00bcodr) | 3.1.0.0 | **coexists** | |
| File Transformation (IAmParadox27) | 2.5.11.0 | **coexists** | rewrites index.html *and* the main web bundle; the kit tags survive both, exactly once each |
| Plugin Pages (IAmParadox27) | 2.4.11.0 | **coexists** | |
| Custom Tabs (IAmParadox27) | 0.2.10.0 | **coexists** | the `RKSinkTab` test tab renders |
| Media Bar (IAmParadox27) | 2.4.12.0 | **coexists** | see the ambient-video finding below |
| Home Screen Sections (IAmParadox27) | 2.5.11.0 | **coexists** | 20 sections rendered |
| Intro Skipper | 1.10.11.22 | **coexists** | |
| InPlayerEpisodePreview (Namo2) | 1.6.1.2 | **coexists** | |
| Media Preview (spkesDE) | 0.3.1.0 | **coexists** | |
| Editor's Choice (lachlandcp) | 1.5.2.0 | **coexists** | |
| ActorPlus (Druidblack) | 1.0.0.0 | **coexists** (partial exercise) | injection intact; the library held no Person records to decorate |
| JMSFusion / MonWUI (G-grbz) | 3.7.0.0 | **coexists** | its 3 parked, never-played `<video>` elements correctly do **not** hold the media gate (the 2.1.1 fix, re-proven) |
| Seasonals (CodeDevMLH) | 3.1.0.0 | **coexists** | |
| Ratings (K3ntas) | 1.0.359.0 | **coexists** | |
| StarTrack (ZL154) | 1.6.4.0 | **coexists** | |
| KefinTweaks (ranaldsgift) | main @ 290b36f | **coexists + adoptable** | bootstrap mode, 38 sub-assets |
| Trakt / Playback Reporting / Webhook / Open Subtitles / TMDb Box Sets | as bundled | **coexists** | admin config pages all render |

Kit-level evidence: two instances behind one interceptor with **four**
independent index.html rewriters live; a KefinTweaks `1.0.0 → 2.0.0` bump
produced **exactly one** reload with 38/38 sub-assets refetched at `?v=2.0.0`
and a single budget stamp, while the untouched sibling converged one-sidedly
from cache; 75 s and 120 s idle soaks produced **zero** reloads; **zero**
kit-version URL leakage across 1259 logged requests; served HTML byte-idempotent
across repeated fetches (the only delta being JavaScript Injector's own
per-request nonce); a container restart with an open tab produced no reload
storm; and zero kit console errors, warnings or exceptions. Assertion sweeps
53/54 and 16/16 — both nominal failures were harness selector artefacts,
identical in the kit-absent controls.

**Behavioural finding, actioned in 2.3.0.** Media Bar's ambient backdrop is a
muted, looping, controls-less autoplay `<video>`. On 2.2.0 it held the media
safety gate open forever on `#/home`: `blockReason: media_element` with
`blockedRetries` climbing 1 → 176 over 160 s, and the starvation clock resetting
on every playback-progress probe, so the layer-3 escape never fired for a tab
simply sitting on Home. That is within the documented design — the gate cannot
tell decoration from viewing by state alone — but it is the wrong answer in
practice. 2.3.0 adds an ambient-video exemption: muted + looping + no controls
does not block, while real playback still does.

Also reproduced (documented limitation, unchanged): a classic-mode bootstrap
that creates its sub-assets **synchronously at parse time** emits them bare;
deferring creation until `ApiClient` exists lands all of them at `?v=`.

Third-party defects, each proven with 4 kit-present vs 4 kit-absent runs
yielding identical error sets: Media Bar issues `GET /Items/undefined` (400) and
throws null `Type`/`classList` TypeErrors; JMSFusion makes a pre-auth call to
`gmmp/state` (401) plus an aborted ping; jellyfin-web/Home Screen Sections
throws `t.getScrollSlider` 2–4 times per load; KefinTweaks intermittently throws
on an undefined watchlist and in `getItemsHtml`.

## Kit 2.3.0 — multi-copy coexistence and the newest-wins manager handoff

The scenario this release exists for: **several plugins each shipping their own
copy of `jellyfin-refresh-kit.js`, at different versions, on the same page.**
Live-tested with 2, 3 and 4 concurrent copies of mixed versions on one server.

The multi-copy result on 2.2.0 was already good — every configuration produced
**one manager, one reload engine, one shared budget, one navigation per update,
and zero page errors**, with each copy keeping its own config, patterns and
mode. But the test also exposed the flaw the release fixes: the manager was
**first-loaded-wins**. Whichever copy registered first ran the show for the life
of the tab, so a server whose plugins were mid-upgrade could be driven by the
*oldest* kit on the page indefinitely — every bug fix in every newer copy
inert until a full page load happened to reorder the tags.

2.3.0 raises the wire contract to **revision 3** and makes the manager
**newest-wins**: an arriving copy compares its `KIT_VERSION` numerically,
segment by segment, against the incumbent manager's, and if it is newer it takes
over — the incumbent deactivates and hands across its live state (budget
accounting, pending-update arming, flip/convergence bookkeeping, registered
instances) rather than restarting from zero.

| Proof | Result |
|---|---|
| 2 / 3 / 4 concurrent mixed-version copies | one manager, one reload engine, one shared budget, one navigation per update, zero page errors, per-copy config preserved (re-run on 2.3.0) |
| Older copy loads first, newer copy second | the newer copy takes the manager role; the older one deactivates and registers as a plain instance |
| Chained handoffs (three ascending versions in load order) | each handoff transfers cleanly; the final manager is the newest copy, with **one** manager alive at every point in the chain |
| Budget continuity across a handoff | the successor inherits the spent budget — a reload already charged before the handoff is not charged twice, and the budget cap still holds |
| Flip / convergence continuity across a handoff | pending-update arming and flip bookkeeping survive the transfer; the update converges with exactly one navigation |
| Handoff **during** an in-flight reload sequence | the successor resumes the sequence; still one navigation, no double-reload, no orphaned timer |
| Newer copy arriving with an equal version | no handoff (numeric comparison is strictly-greater), incumbent keeps the role — no churn from identical copies |
| Ambient-video media-gate exemption | a muted + looping + controls-less backdrop no longer reports `media_element`; a real playing element still blocks (see the 2.2.0 kitchen-sink finding) |

**Distribution note.** Because the handoff is what makes mixed-version pages
safe, and pre-2.3.0 copies can only ever lose the argument by load order,
**copies older than 2.3.0 must not be shipped publicly.** Any plugin embedding
the kit should ship 2.3.0 or newer.

## Standalone plugin — Jellyfin Refresh Kit 1.0.0.0 (Jellyfin 10.11.11)

The standalone plugin — install one plugin, and cache/hard-refresh behaviour is
fixed for every other plugin on the server, none of which need to know it
exists. Validated on 10.11.11 against a set of adopters that deliberately
**do not ship the kit themselves**: Jellyfin Enhanced, Media Bar, File
Transformation and InPlayerEpisodePreview.

| Proof | Result |
|---|---|
| Third-party tag stamping | precise and idempotent: exactly the unversioned third-party script/stylesheet tags gained `?rkv=`, plugins carrying their own cache-buster were left byte-identical, and repeated fetches of the served HTML produced the same stamps |
| Plugin DLL touched (binary timestamp changes) | generation changes → **one** reload in the open tab, and the reloaded page carries the new stamps |
| Jellyfin Enhanced admin setting changed (adds visible UI) | **one** reload, and the new tab is live in the open tab afterwards — the config path works end to end without a server restart |
| Per-user Jellyfin Enhanced preference saves | **zero** generation bumps. Per-user preferences live outside the watched admin configuration XML, so one user changing their own settings never reloads anybody's tab |
| Debounce | a burst of configuration writes collapses to one generation change after the 10 s debounce |
| Per-plugin cooldown | a plugin that has just bumped cannot bump again for 5 minutes — a chatty plugin cannot turn into a reload treadmill |
| Admin toggle | configuration watching switched off ⇒ no config-driven generation changes at all (binary/version changes still counted) |
| Exclusion list | a named plugin's configuration writes produce no generation change while the rest continue to |

The configuration signal is deliberately narrow: the plugin watches only each
plugin's configuration XML, never its data directory, and pays for that with the
debounce and cooldown above. The tradeoff is stated in `plugin/README.md` — a
settings change reaches open clients within seconds rather than instantly, in
exchange for a signal that cannot be spammed by ordinary plugin activity.
