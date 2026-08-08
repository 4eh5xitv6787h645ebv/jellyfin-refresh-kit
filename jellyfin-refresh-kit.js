/*!
 * jellyfin-refresh-kit.js — drop-in stale-cache / hard-refresh fix for Jellyfin
 * client-script plugins and script collections.
 *
 * MIT License
 * Copyright (c) 2026 <YOUR NAME HERE>
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 *
 * ---------------------------------------------------------------------------
 * WHAT THIS FIXES
 * ---------------------------------------------------------------------------
 * Jellyfin client-script plugins ship JavaScript that the browser caches hard.
 * When you publish a new version, users keep running the old one — sometimes
 * for days — until they hit Ctrl+Shift+R. There are three independent layers of
 * staleness, and a pure-JS kit can only reach two of them:
 *
 *   1. THE SHELL (index.html and every <script> tag statically written into it).
 *      Only the SERVER can fix a statically-tagged file. This kit cannot — but
 *      BOOTSTRAP MODE (below) removes almost all of those tags, so there is
 *      barely any shell left to go stale. What the kit CAN always do is notice
 *      that a new version exists and reload the tab, which re-fetches the shell
 *      under normal revalidation rules.
 *
 *   2. SUB-ASSETS (the sub-scripts and stylesheets your bootstrap loads at
 *      runtime, usually with document.createElement('script')). THIS is the
 *      layer the kit actually fixes: it rewrites those URLs to carry
 *      ?v=<current version> so a new release is a new URL and can never be
 *      served from a stale cache entry.
 *
 *   3. OPEN TABS. A user who left Jellyfin open in a tab for two days never
 *      re-requests anything. The kit polls a tiny version endpoint and, when the
 *      version changes, performs a *safe* auto-reload — never over playback,
 *      never over an open dialog, never while typing.
 *
 * ---------------------------------------------------------------------------
 * MULTI-INSTANCE (v2): N PLUGINS, ONE PAGE
 * ---------------------------------------------------------------------------
 * Since 2.0.0 the kit is no longer a singleton. window.JellyfinRefreshKit is a
 * MANAGER owning a registry of named INSTANCES — one instance per adopting
 * plugin / script collection. Each <script> tag of this file registers exactly
 * ONE instance, configured from that tag's own data-* attributes. Two, three,
 * N plugins can each ship their own copy of the kit (even different kit
 * versions) and they compose on one page:
 *
 *   • THE NEWEST COPY MANAGES THE PAGE (2.3.0). The first copy to execute
 *     installs the machinery, but a copy that arrives afterwards and is
 *     STRICTLY NEWER takes the page over through a handoff (REGISTRATION
 *     CONTRACT clause 7): the sitting manager stops, hands over every
 *     registered instance WITH its live state, and becomes a permanent inert
 *     delegate that forwards the contract surface to the new manager. Equal
 *     versions never hand over. Before 2.3.0 the first copy managed the page
 *     forever, which meant a plugin shipping the newest kit could not
 *     guarantee its own safety fixes governed the page it was running on —
 *     an older copy's flip guard, reload budget and gates decided for
 *     everyone. Version comparison is numeric per segment, never string
 *     order ("2.10.0" is newer than "2.9.0").
 *
 *   • ONE ACTIVE createElement wrapper, ever. On src/href assignment it
 *     consults ALL registered instances' assetPatterns; the FIRST-REGISTERED
 *     instance whose patterns match versions the URL with THAT instance's
 *     resolved version. Registration order is document order of the kit tags
 *     (and a handoff preserves it). If patterns of two instances overlap on a
 *     URL, the first-registered instance wins and the manager logs ONE
 *     console.warn naming the overlapping instances the first time it happens.
 *     URLs already carrying v= always pass through untouched. A handoff cannot
 *     UNINSTALL the old wrapper (other code may hold a reference to it by
 *     then), so the old one flips to a permanent inert pass-through and the
 *     new manager stacks its own on top: still exactly one wrapper that does
 *     anything, and it is the newest copy's.
 *
 *   • PER-INSTANCE: version source + polling cadence + baseline/latest
 *     version, bootstrap entry loading (an instance's entries wait on ITS
 *     version and load sequentially within the instance; different instances
 *     load their entries concurrently with each other), mode (auto/notify/
 *     off), onUpdateAvailable, entryTimeoutMs, idleSeconds, pollSeconds.
 *
 *   • SHARED PAGE-LEVEL MACHINERY: the safety gates, idle tracking, and the
 *     reload budget (storage key 'jellyfin-refresh-kit-budget-v1', unchanged
 *     from 1.x — a page reload is a page-level resource) are one engine. ANY
 *     auto-mode instance that detects an update requests the shared safe
 *     reload. The idle requirement used for that reload is the STRICTEST
 *     (i.e. the MAXIMUM) idleSeconds among the instances currently wanting to
 *     reload, and the effective reload budget is the MINIMUM reloadBudget
 *     among all registered instances — the page honours every adopter's most
 *     conservative ask. Notify-mode instances never trigger the shared
 *     reload; their callback still fires. ONE NAVIGATION SPENDS ONE BUDGET
 *     SLOT (2.1.2): the engine latches once location.reload() is called, so
 *     instances that arm in the window before the new document commits ride
 *     that reload instead of reserving another.
 *
 *   • RELOAD COST WITH N INSTANCES: after a reload triggered by instance A,
 *     instance B's unchanged-version assets are re-requested at their SAME
 *     immutable ?v= URLs and come straight back out of the HTTP cache — a
 *     reload for one plugin's update costs the others nothing but cache hits.
 *
 *   • NAMES: each instance has a name — config `name` / attribute data-name.
 *     If omitted, the name is derived deterministically from versionUrl (the
 *     last directory segment of its path, e.g. "/web/KefinTweaks/version.json"
 *     → "KefinTweaks"); with neither, it falls back to "instance-<N>", where N
 *     counts ANONYMOUS adoptions only — a named adopter whose tag parses first
 *     cannot renumber it (since 2.1.2), so the name stays addressable from
 *     JellyfinRefreshKitConfigs. Registering the same name again with an
 *     EQUIVALENT config is a silent dedupe (you get the existing instance
 *     back); the same name with a DIFFERENT config registers as "name#2" and
 *     warns. An anonymous adoption has no name to collide on, so it is deduped
 *     by comparing its declared config against the anonymous instances already
 *     registered — an injector that applies the same nameless payload twice
 *     gets one instance, not two. (A tag declaring literally nothing is exempt:
 *     several of those are indistinguishable until their own
 *     JellyfinRefreshKitConfigs["instance-<N>"] entries are read.)
 *
 *   • WINDOW CONFIG: window.JellyfinRefreshKitConfig (the 1.x singular form) is
 *     read by EACH COPY at its own tag position, synchronously, in the same
 *     breath as document.currentScript — so it configures the tag it was
 *     authored next to, not "whichever instance happened to register first".
 *     A single-plugin page behaves exactly as it did in 1.x. On a page where
 *     several adopters each write the singular global before their own tag,
 *     each one lands on its own instance (they are distinct objects).
 *
 *     WHICH global reaches WHICH adoption is decided since 2.2.0 by ONE
 *     UNIFORM RULE, implemented in ONE function used by BOTH the tag-side merge
 *     and the manager-side fallback, with the full truth table in its doc
 *     comment (search: singularGlobalOutcome). The rule, in one paragraph: the
 *     kit never clears the global, so it stays live at every later tag — it is
 *     therefore CLAIMABLE EXACTLY ONCE, and a consumer may claim an unclaimed
 *     global iff (a) the global positively identifies it (same name, or same
 *     versionUrl), or (b) the global identifies nobody and this consumer got
 *     there first, or (c) the consumer declares nothing identifying of its own
 *     (the same adoption injected twice; the bare eval'd shape). A ONE-SIDED
 *     identifier mismatch — the global names an adoption and the consumer
 *     declares any identifying config that does not positively match — is a
 *     DISAGREEMENT and is skipped with one warning, WITHOUT claiming, so the
 *     adoption it does name can still take it whichever order the tags appear
 *     in. That is a no-op for the single-adopter 1.x shape.
 *
 *     The claim is a non-enumerable marker on the object; an object that
 *     cannot carry one (frozen, sealed, exotic) is claimed BY IDENTITY through
 *     a page-level WeakSet, exposed on the manager as __claimSingularGlobal
 *     (REGISTRATION CONTRACT clause 6). Freezing a config object you publish
 *     on window is an ordinary defensive habit and must not cost an adopter
 *     its own configuration.
 *
 *     BOTH halves of the claim are PAGE-LEVEL, not manager-level — the marker
 *     lives on the config object, the WeakSet on `window` — which is exactly
 *     what makes a claim survive a MANAGER HANDOFF (clause 7) with nothing to
 *     transfer: a global claimed under the old manager is still claimed under
 *     the new one, and a transferred instance is never re-offered the global
 *     it already merged (that would warn "already claimed" about an
 *     instance's own configuration).
 *
 *     The manager runs the SAME rule as a fallback for copies that cannot read
 *     the global themselves (pre-2.1 copies, and eval'd copies with no
 *     currentScript) — per registration, bounded by the same one-time claim.
 *     For targeted config use the keyed form: window.JellyfinRefreshKitConfigs
 *     = { "KefinTweaks": {...}, "DemoPack": {...} } — each entry merges over
 *     (and wins against) the matching instance's tag attributes, and is looked
 *     up under the instance's FINAL resolved name (including the "#2"
 *     collision suffix and an "instance-<N>" fallback name); a "#N" instance
 *     falls back to the base-name entry only when no entry exists under its
 *     own key. The keyed form is read SYNCHRONOUSLY by each tag at its own
 *     position, so it must be defined BEFORE every kit tag; an entry that
 *     names an already-registered instance — or (since 2.1.2) no instance at
 *     all — is reported with one warning once the document has parsed AND
 *     registrations have settled (since 2.2.0 the audit is re-armed by every
 *     registration, so a copy injected after DOMContentLoaded is not accused
 *     of never consuming the key it is about to consume).
 *     Priority per instance:
 *     keyed entry > singular > data-* > defaults.
 *
 * ---------------------------------------------------------------------------
 * REGISTRATION CONTRACT (revision 3) — FROZEN. This section is the
 * compatibility promise between kit copies of DIFFERENT versions cohabiting a
 * page. Revisions are STRICTLY ADDITIVE: any future kit version MUST keep
 * every numbered clause working forever, and a caller speaking an older
 * revision must keep working against every future manager.
 *
 * PRE-2.3.0 COPIES MUST NEVER BE SHIPPED PUBLICLY. No version of this kit has
 * ever been released, and revision 3 — the newest-wins manager rule — is the
 * one change that could not have been made additively after the fact: a
 * pre-2.3.0 manager cannot hand a page over, so a 2.3.0+ copy arriving after
 * one is stuck registering under it, and the OLDER copy's reload semantics
 * govern the page (the kit says so, loudly, and that is the best it can do).
 * Every adopting plugin must ship 2.3.0 or newer.
 * ---------------------------------------------------------------------------
 *  1. The FIRST kit copy to execute on a page becomes the manager: it installs
 *     window.JellyfinRefreshKit, the single createElement wrapper, and the
 *     shared page machinery, then registers its own instance. It KEEPS that
 *     role until a strictly newer copy takes it over under clause 2b.
 *  2. Every copy, at the top of its IIFE, synchronously captures its own
 *     <script> tag config (document.currentScript data-*) and then inspects
 *     window.JellyfinRefreshKit:
 *       a. absent            → become the manager (clause 1).
 *       b. present AND has a function-valued __registerInstance → compare
 *          KIT_VERSION with the manager's `kitVersion`, NUMERICALLY, segment by
 *          segment (never as strings — "2.10.0" must beat "2.9.0"):
 *            • manager's version >= mine, or either version unparseable →
 *              call window.JellyfinRefreshKit.__registerInstance(tagConfig,
 *              KIT_VERSION) and do NOTHING else. No second wrapper, no
 *              listeners, no timers — the manager runs everything.
 *            • mine is STRICTLY NEWER and the manager's __contractVersion is
 *              >= 3 and it exposes __handoffTo → TAKE THE PAGE OVER (clause 7):
 *              build this copy's manager, call __handoffTo(myApi), re-register
 *              every transferred instance, log ONE info line. Registration
 *              order — which decides who versions an ambiguous URL — is
 *              preserved, and the transferred instances register BEFORE this
 *              copy's own tag, because they were on the page first.
 *            • mine is strictly newer but the manager speaks contract < 3 (a
 *              pre-2.3.0 copy) → register as above, but ALSO emit one loud
 *              console.warn naming both versions and stating that page-level
 *              reload semantics on this page are the OLDER copy's.
 *          A handoff that is declined (null return) falls back to plain
 *          registration: a page must never be left without a manager.
 *          Two copies of the SAME version never hand over — the sitting
 *          manager stays, so an ordinary multi-copy page is untouched by this
 *          rule.
 *       c. present WITHOUT __registerInstance (a 1.x singleton) → log ONE
 *          console.warn and go fully inert. The 2.x copy must not fight the
 *          1.x wrapper (double-versioning, double reload engines). Mixing
 *          1.x + 2.x on one page means the 1.x-shipping plugin should upgrade
 *          its kit copy; until then only the 1.x plugin is served.
 *       RECOVERY (2.1.0, additive — clause 2 semantics are unchanged): the
 *          manager also publishes itself on the NON-ENUMERABLE window property
 *          __jellyfinRefreshKitManager, and window.JellyfinRefreshKit is
 *          installed non-configurable. A copy whose clause-2 inspection finds
 *          no usable __registerInstance consults that backup BEFORE taking
 *          clause 2c, so a manager whose global was clobbered (by a 1.x copy
 *          running second, or by an unrelated plugin) is recovered instead of
 *          silently stranding every later copy.
 *  3. manager.__registerInstance(config, kitVersion) → handle | null:
 *       • config: a PLAIN OBJECT of tag-level options using the documented
 *         option names (name, versionUrl, versionJsonField, getVersion,
 *         pollSeconds, idleSeconds, assetPatterns, entryScripts,
 *         entryTimeoutMs, mode, onUpdateAvailable, reloadBudget, bootVersion —
 *         plus the private marker `__singularApplied`, set by a 2.1+ copy that
 *         already SETTLED window.JellyfinRefreshKitConfig for its own tag —
 *         either merging it over that tag's config or declining it under the
 *         uniform rule — so the manager does not apply it a second time
 *         somewhere else; an older manager simply ignores the unknown key).
 *         The MANAGER
 *         normalizes and clamps with its own rules and MUST ignore unknown
 *         keys — that is what lets an older manager accept a config written
 *         for a newer kit. The manager also applies the window config layers
 *         (clause: MULTI-INSTANCE → WINDOW CONFIG) itself, so merge behaviour
 *         is decided by exactly one version of the code — the running manager.
 *       • kitVersion: the arriving copy's KIT_VERSION string (diagnostics;
 *         surfaced in state() as registeredByKitVersion).
 *       • returns an instance handle { name, version, latestVersion,
 *         versionedUrl(url, force), checkNow(), state() } — or null on any
 *         internal failure. It MUST NEVER throw.
 *       • duplicate registration (same resolved name + equivalent config)
 *         silently returns the existing instance's handle.
 *  4. manager.__contractVersion is a number (currently 3) naming the newest
 *     contract revision the manager speaks. Revisions are strictly additive:
 *     a v1 call MUST keep working against every future manager. After a
 *     handoff it reports the CURRENT manager's revision.
 *  5. manager.kitVersion is the CURRENT manager copy's own version string, so
 *     an arriving copy can compare itself against it (clause 2b) and log
 *     version skew. Feature skew is bounded by the manager: features the
 *     manager's version lacks are unavailable to later-arriving instances, but
 *     registration itself never breaks. Since 2.3.0 it is an ACCESSOR, because
 *     the object window.JellyfinRefreshKit points at may have handed the page
 *     over — a stale value there would make every later copy compare itself
 *     against a manager that retired long ago.
 *  6. (REVISION 2, 2.2.0 — ADDITIVE.) manager.__claimSingularGlobal(obj) →
 *     boolean. The page-level, once-only claim on a singular window config
 *     OBJECT: the first caller for a given object gets true, every later caller
 *     gets false. It exists for config objects that cannot carry the ordinary
 *     non-enumerable claim marker (frozen, sealed, non-extensible, exotic
 *     proxies), which are tracked by identity in a WeakSet the manager owns.
 *     It MUST NEVER throw. A contract-v1 caller never calls it and is
 *     unaffected; a v2 caller against a v1 manager falls back to the marker and
 *     to its own page-level store, so mixed-version pages keep working.
 *  7. (REVISION 3, 2.3.0 — ADDITIVE.) manager.__handoffTo(newManager) →
 *     transferRecord | null. The newest-wins rule of clause 2b. Called by a
 *     STRICTLY NEWER arriving copy that has already built its own manager;
 *     MUST NEVER throw, and returns null when it declines (the caller then
 *     registers as an ordinary instance).
 *     On success the OLD manager, in one synchronous step:
 *       • deactivates every instance (cancels its timers and latches its entry
 *         points off, so a version request already in flight cannot act),
 *         cancels the shared retry ladder, the keyed-config audit and the
 *         reload-survival watchdog, and removes every page listener;
 *       • flips its createElement wrapper to a permanent INERT PASS-THROUGH.
 *         The wrapper cannot be uninstalled — third-party code may hold a
 *         reference to it, and restoring the native function would strip the
 *         new manager's wrapper off the page — so the new manager stacks its
 *         own on top and the old one creates elements without touching them.
 *         Elements the old wrapper ALREADY handed out keep their per-element
 *         accessors, which from now on delegate their versioning decision to
 *         the current manager;
 *       • becomes a permanent INERT DELEGATE: every member of its api object
 *         (__registerInstance, versionedUrl, checkNow, get, instances, state,
 *         version, latestVersion, kitVersion, __contractVersion, __handoffTo
 *         itself) forwards to the new manager. This is what makes the handoff
 *         possible at all, because window.JellyfinRefreshKit is installed
 *         NON-CONFIGURABLE (clause 2 RECOVERY) and can never be re-pointed:
 *         the retired object stays the page's entry point and answers for the
 *         live manager. A chained handoff (old → new → newer) forwards and
 *         then re-points straight at the newest, so the chain stays flat.
 *         __claimSingularGlobal is the ONE member that does not forward: the
 *         claim it records is page-level either way (a marker on the config
 *         object, or a WeakSet on `window`), and forwarding it would build a
 *         cycle.
 *     THE TRANSFER RECORD is a plain object:
 *       { contractVersion, kitVersion, handoffs, lineage[], anonymousCount,
 *         lateKeys{}, instances: [ { name, anonymous, keyedConfigKey,
 *         registeredByKitVersion, entriesSuppressed, declaredConfig,
 *         effectiveConfig, state } ], shared: { ...page-level state... } }
 *     `state` is the instance's LIVE internals (resolved baseline + latest
 *     version, candidate, announced version, pending update, entry-chain
 *     progress and promise, one-shot warning latches) and `shared` carries the
 *     page-level ones, INCLUDING the committed-reload latch, the records that
 *     reload wrote and the instances it disarmed. The new manager inherits the
 *     latch and RE-ARMS the survival watchdog; it must never call
 *     location.reload() again for a navigation already in flight.
 *     NOT in the record, deliberately: the reload budget and the per-tab flip /
 *     left-version / recovery history. Those already live in localStorage and
 *     sessionStorage under page-wide keys, keyed by instance NAME — and a
 *     handoff preserves every name, including "#N" collision suffixes — so
 *     they survive a handoff exactly as they survive the reloads they police.
 *     The singular-global claim is page-level for the same reason and is not
 *     transferred either.
 *     A newer manager re-NORMALIZES every transferred config under its own
 *     rules, which is the point of newest-wins: a clamp or validation the newer
 *     copy tightened governs the instances it inherited too.
 *
 * ---------------------------------------------------------------------------
 * BOOTSTRAP MODE (recommended adoption)
 * ---------------------------------------------------------------------------
 * Without bootstrap mode, layer 2 is fixed but the collection's OWN entry files
 * (its config script and its injector/entry script) are still static <script>
 * tags in index.html — unversioned, and therefore cacheable-stale exactly like
 * the shell. Ship a bugfix inside the injector itself and a browser can keep
 * running the old injector indefinitely.
 *
 * Bootstrap mode collapses that surface. index.html carries ONE tag per
 * adopting collection — this kit — and the kit loads the collection's entry
 * files itself:
 *
 *   <script src="/web/KefinTweaks/jellyfin-refresh-kit.js"
 *           data-name="KefinTweaks"
 *           data-version-url="/web/KefinTweaks/version.json"
 *           data-version-json-field="version"
 *           data-asset-patterns="/KefinTweaks/"
 *           data-entry-scripts="/web/KefinTweaks/kefinTweaks-config.js,/web/KefinTweaks/injector.js">
 *   </script>
 *
 * Sequence: resolve the instance's version FIRST → then append each entry in
 * order, each carrying ?v=<version> → the entry code then creates its
 * sub-assets, which the createElement interceptor versions as usual.
 * Everything under the collection's folder is now version-addressed. The ONLY
 * file left unversioned is this kit — a small, stable loader that changes
 * rarely (see LIMITATIONS).
 *
 * Rules the entry loader follows, in priority order:
 *   • Never load an entry before the instance's version resolves...
 *   • ...but never let a dead version endpoint cost the user their plugin.
 *     The first version fetch is raced against entryTimeoutMs (default 3s); on
 *     failure or timeout the entries load UNVERSIONED and the kit logs exactly
 *     one warning. Availability beats freshness.
 *   • Strict order WITHIN an instance. Entry N+1 is appended only after entry
 *     N settles, so a config script is guaranteed to have executed before the
 *     injector that reads it. (script.async = false is also set, belt and
 *     braces.) Different instances' chains run concurrently — they are
 *     independent collections.
 *   • An entry that 404s or throws is logged and SKIPPED; the remaining entries
 *     still load. One bad file must not take the page down with it.
 *   • .css entries become <link rel="stylesheet">, everything else <script>.
 *
 * Bootstrap mode is opt-in and purely additive: with no entryScripts configured
 * the instance behaves exactly as it did before, so existing adoptions keep
 * working.
 *
 * ---------------------------------------------------------------------------
 * DESIGN NOTES (why it looks like this)
 * ---------------------------------------------------------------------------
 * • Zero dependencies, one file, ES2017, no build step. It has to be pasteable
 *   into a JS Injector textarea or served from a plugin's static folder.
 * • It must NEVER break the host page. Every observable entry point is wrapped
 *   so a throw inside the kit cannot escape into Jellyfin's own code.
 * • Interception happens at ASSIGNMENT time, not via MutationObserver. A
 *   document-level observer sees the element AFTER it is inserted, and the
 *   browser has usually already kicked off the fetch by then — mutating .src at
 *   that point either does nothing or causes a double fetch. Wrapping
 *   document.createElement and installing a per-INSTANCE accessor (never on
 *   Element.prototype — that is far too invasive and collides with other
 *   plugins) rewrites the URL before it is ever assigned.
 * • The manager/instance split exists because two plugins each shipping a kit
 *   tag would otherwise collide on the global and double-wrap createElement.
 *   The first copy owns the machinery; every later copy is just configuration.
 *
 * ---------------------------------------------------------------------------
 * KNOWN, DELIBERATE LIMITATIONS
 * ---------------------------------------------------------------------------
 * • WITHOUT bootstrap mode (classic adoption) EVERY page load races the version
 *   fetch. The kit holds no cross-load memory of the version: baselineVersion
 *   starts null on every load and the version endpoint is fetched no-store, so
 *   load 2 and load 200 are in exactly the state load 1 was. Any asset the host
 *   bootstrap creates SYNCHRONOUSLY at parse time — before the fetch resolves —
 *   is therefore unversioned on every load, not just the first, and the browser
 *   keeps serving whatever it cached for that bare URL. Everything created
 *   AFTER the version resolves is versioned, which is most of a real
 *   collection's surface but is a per-load property, not a one-time cost.
 *   Only BOOTSTRAP MODE removes the race, because there the kit itself decides
 *   when the entries load. Alternatively, seed the baseline from the document
 *   with `bootVersion` / data-boot-version (below) so load N+1 starts already
 *   versioned.
 * • `bootVersion` / data-boot-version seeds the baseline from the identity of
 *   the build that SERVED this document, which is what closes the
 *   page-serve → first-poll blind spot (an update landing in that window is
 *   otherwise absorbed into the baseline and never detected). It must name the
 *   SAME identity the version endpoint reports, or every load looks like an
 *   update: RefreshKit.cs emits data-boot-version="{CacheKey}", so pair it with
 *   data-version-json-field="CacheKey". The kit self-heals a mismatch — after
 *   one reload that does not change the served boot identity it discards the
 *   seed with a warning and falls back to the first-poll baseline.
 * • WITH bootstrap mode, one file per collection remains unversioned and
 *   un-fixable from JS: the kit's own <script src> in index.html. Something
 *   has to be the loader, and the loader cannot cache-bust itself. The
 *   mitigation is that this file is small and deliberately stable — the
 *   volatile code lives in the entries. If you need the loader itself to be
 *   fresh, that is a SERVER job: send `Cache-Control: no-cache` (revalidate,
 *   cheap 304s) for this one file.
 * • Bootstrap mode adds one round-trip of latency before the entries start
 *   loading (the version fetch, bounded by entryTimeoutMs). It is a small
 *   same-origin JSON GET, but it is not free.
 * • MULTI-INSTANCE: which instance versions an ambiguous URL is decided by
 *   REGISTRATION ORDER, which is the document order of the kit <script> tags.
 *   Keep assetPatterns disjoint (they name your own folder — they naturally
 *   are); the overlap warning exists to surface accidents, not to arbitrate
 *   deliberate sharing.
 * • A MANAGER HANDOFF (newest-wins) has three costs, all deliberate. ONE: the
 *   createElement wrapper chain grows by one INERT frame per handoff, because
 *   a wrapper can never be uninstalled safely — bounded by the number of kit
 *   copies in the document, and every frame but the newest returns the element
 *   untouched. TWO: window.JellyfinRefreshKit keeps pointing at the FIRST
 *   manager's object forever (it is installed non-configurable on purpose), so
 *   the object identity a debugger sees is not the version that is running;
 *   every member forwards, and `state().shared.managerLineage` lists the
 *   copies in the order they ran the page. THREE: a version fetch that was in
 *   flight when the handoff happened resolves into the retired closure and is
 *   discarded; the instance that took over re-issues it immediately, so the
 *   cost is one duplicate request, not a missed update.
 * • MIXING PRE-2.3.0 AND 2.3.0+: a pre-2.3.0 copy that runs FIRST manages the
 *   page even though a newer copy is present — it cannot hand over, because
 *   __handoffTo did not exist yet. The newer copy says so with one loud
 *   warning naming both versions, and everything still WORKS; what you lose is
 *   every fix the newer copy made to page-level behaviour (the flip guard, the
 *   reload budget, the safety gates, URL interception). Since no version of
 *   this kit was ever released publicly, the fix is simply never to ship one:
 *   every adopting plugin must carry 2.3.0 or newer.
 * • MIXING 1.x AND 2.x: a 1.x copy that runs FIRST owns the page (2.x copies
 *   go inert with one warning); a 1.x copy that runs SECOND was never
 *   multi-instance-aware and will double-wrap createElement on top of the 2.x
 *   manager — harmless for correctness (the inner wrapper sees already-
 *   versioned URLs and passes them through) but sloppy. Since 2.1.0 it can no
 *   longer STRIP the 2.x manager: window.JellyfinRefreshKit is non-configurable
 *   and a backup handle is kept (REGISTRATION CONTRACT clause 2, RECOVERY), so
 *   later 2.x copies still register. Either way: the 1.x-shipping plugin should
 *   upgrade its kit copy.
 * • A VERSION SOURCE THAT FLAPS (two nodes behind a round-robin proxy reporting
 *   different build identities for the same release, three replicas whose
 *   per-container DLL mtimes make one release look like three, a rolling
 *   deploy) would otherwise reload the tab once per poll forever, because each
 *   reload is inside its own budget window and so is always affordable. Two
 *   defences: a candidate version must be seen on TWO consecutive observations
 *   before it arms a reload (2.1.0), and a tab REFUSES to auto-reload to any
 *   version it has already reloaded AWAY FROM (2.2.0 — the per-tab record lives
 *   in sessionStorage, so it survives the reloads it is policing). The second
 *   defence used to be pair-based ("I went X → Y, so I refuse Y → X"), which
 *   cannot close a cycle of three or more identities: A→B, B→C, C→A traverses
 *   only forward edges and loops forever at the budget ceiling. Asking instead
 *   "have I already left the version I am being sent to?" terminates cycles of
 *   any length, and never blocks a genuine release chain, which only ever moves
 *   to versions this tab has never run. The kit logs one line and keeps
 *   versioning URLs; it cannot make an unstable endpoint stable — serve one
 *   identity per release across all nodes.
 * • A HOST THAT BLOCKS OR IGNORES location.reload() (an embedded WebView or
 *   Electron shell intercepting navigation, a sandboxed context where reload()
 *   throws, a beforeunload confirm the user answers with "Stay") cannot be
 *   detected any way but empirically: the kit arms a 3s survival watchdog after
 *   every reload call, and a document still alive when it fires re-opens the
 *   commit latch, re-arms the instances it disarmed, retracts the version
 *   transition it recorded for a navigation that never happened, and retries.
 *   The budget slot already spent is NOT refunded — that is what stops such a
 *   host from being asked once a second forever.
 * • THE MEDIA GATE is deliberately coarse: any <video>/<audio> on the page that
 *   represents a real session blocks the auto-reload, not just Jellyfin's own
 *   player. Since 2.1.1 "a real session" means playing, or paused with a
 *   playback position / a played range / an in-progress seek — a decorative
 *   element a plugin parks in the DOM with a src and preload set is NOT a
 *   session and no longer blocks (before, it blocked forever and silently
 *   switched layer 3 off for that page). Since 2.3.0 an AMBIENT BACKDROP VIDEO
 *   — muted (or volume 0) AND looping AND without controls, which is what Media
 *   Bar and friends put behind the Home screen — is not a session either, even
 *   while it plays. It used to block forever AND keep the starvation escape
 *   from ever firing, because a looping video always shows fresh playback
 *   progress. The false negative is deliberate and narrow: a user who
 *   deliberately watches a muted, looping, controls-less video can be reloaded
 *   under. Unmute it, give it controls, stop it looping or make it fullscreen
 *   and it blocks exactly as before. A media element that IS a session but
 *   then freezes — paused and abandoned, or stalled mid-buffer — still blocks,
 *   but only for ~10 minutes of zero playback progress anywhere on the page
 *   (the same span the retry ladder covers); after that the kit logs one line
 *   and re-tests with the media probe suppressed. Every other gate (dialog,
 *   editor, route, fullscreen, visibility, idle) still applies.
 * • A CDN's own "@latest" resolution TTL is invisible to JavaScript. jsDelivr
 *   caches the @latest → tag mapping for up to 24h; no amount of ?v= changes
 *   that, because the STALE FILE IS THE CORRECT RESPONSE for that URL. Pin a
 *   version (@1.2.3) or self-host. See README.
 * • The kit cannot add ETag / Cache-Control headers. That needs the server.
 */

(function () {
    'use strict';

    // ─────────────────────────────────────────────────────────────────────────
    // Constants
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @type {string} Version of the kit itself, surfaced in state(). Bump on any
     * behaviour change so a support log identifies the loader precisely.
     *   1.0.0 — interceptor + polling + safe reload.
     *   1.1.0 — bootstrap mode (entryScripts): the kit loads the collection's
     *           entry files itself, after the version resolves.
     *   2.0.0 — multi-instance: manager + named instances, one shared
     *           interceptor and reload engine, registration contract v1 so
     *           multiple kit copies (even different versions) cohabit a page.
     *   2.1.0 — reload-safety and attribution fixes, plus `bootVersion`:
     *           a budget-refused update is deferred, never discarded; a
     *           candidate version must be confirmed twice and a version flap
     *           cannot loop the tab; entries that loaded unversioned no longer
     *           poison the baseline; the singular window config follows its own
     *           tag; the manager global can no longer be clobbered; classic
     *           mode 'off' still resolves one version so URLs stay versioned.
     *   2.1.1 — multi-adopter and liveness fixes: the singular window config is
     *           no longer absorbed by a LATER adopter's tag; the keyed config is
     *           looked up under the FINAL name (including "#N") and can no
     *           longer manufacture a dedupe; an update the server WITHDRAWS
     *           disarms instead of reloading for nothing; every version check
     *           has a 10s ceiling and the poll loop re-arms ahead of the
     *           request, so a hung endpoint cannot stop polling; the
     *           confirmation fetch is capped at one per poll cycle; the two
     *           per-interaction timers are tracked and superseded; a parked,
     *           never-played media element no longer blocks (and a frozen one
     *           cannot starve the reload forever); per-instance
     *           blockReason/idle use the window the shared engine enforces.
     *   2.1.2 — interception and multi-adopter correctness: `src`/`href`
     *           assigned a URL or boxed String object is versioned instead of
     *           silently bypassing layer 2 (both the accessor and
     *           setAttribute); ONE navigation reserves ONE reload-budget slot,
     *           however many instances arm during unload; the "instance-<N>"
     *           fallback name is numbered across ANONYMOUS adoptions only and
     *           an anonymous adoption registered twice dedupes; a keyed config
     *           key that matches no instance is reported instead of ignored;
     *           and a singular window config that identifies nobody binds to
     *           the first tag that takes it rather than leaking into every
     *           later adopter.
     *   2.2.0 — ONE UNIFORM RULE for window.JellyfinRefreshKitConfig (see
     *           singularGlobalOutcome's truth table), replacing the accumulated
     *           name/versionUrl agreement special cases that leaked a config
     *           across adopters in three consecutive review rounds; the
     *           manager-side fallback runs that same rule per registration
     *           instead of a one-shot that could never fire; a frozen/sealed
     *           config object is claimed by identity (contract revision 2 adds
     *           manager.__claimSingularGlobal) instead of being refused; a
     *           reload the host blocks or ignores no longer kills layer 3 for
     *           the life of the document (survival watchdog); the flap guard
     *           refuses any version this tab has already left, so an
     *           oscillation over 3+ node identities terminates; double-injected
     *           config objects with inline callbacks dedupe; the aggregate
     *           snapshot no longer names an idle window (or the instance
     *           imposing it) when nothing is pending; and the keyed-config
     *           audit waits for late-injected copies instead of warning about
     *           keys they were about to consume.
     *   2.3.0 — NEWEST-WINS: the page manager is no longer "whichever copy
     *           parsed first" but the NEWEST copy on the page. A newer copy
     *           arriving after an older manager takes the page over through
     *           contract revision 3's manager.__handoffTo(newManager), which
     *           stops the old manager, transfers every registered instance
     *           WITH its live state (baseline/latest version, pending update,
     *           entry-chain progress, warning latches) plus the shared reload
     *           latch, and leaves the old copy as a permanent INERT DELEGATE
     *           that forwards the whole contract surface to the new manager.
     *           Without it, a plugin shipping the newest kit could not
     *           guarantee its own safety fixes governed the page: a 2.1.2 copy
     *           parsing first ran ITS pair-based flip guard instead of 2.2.0's
     *           hasLeftVersion guard, which a live 4-copy test turned into a
     *           real reload loop (7 reloads in 185s over 3 version identities)
     *           on a page where the newest copy present had already fixed it.
     *           Also: an AMBIENT BACKDROP VIDEO (muted + looping + no controls
     *           — Media Bar's Home-screen backdrop and every fork of it) no
     *           longer holds the reload gate forever, which it did while also
     *           keeping the parked-media starvation escape from ever firing;
     *           the recurring "update available" line names the flap refusal
     *           instead of leaving it to a warning logged once and scrolled
     *           away; flapDisarmedFor reports the LATEST refused transition;
     *           and a frozen singular window config takes the WeakSet claim
     *           path silently instead of via a swallowed throw.
     */
    var KIT_VERSION = '2.3.0';

    /**
     * @type {number} Registration-contract revision this copy speaks (see the
     * REGISTRATION CONTRACT header section). Strictly additive; a v1 caller
     * must keep working against every future manager.
     *   1 — __registerInstance / __contractVersion / kitVersion.
     *   2 — adds __claimSingularGlobal (2.2.0). A v1 caller never calls it and
     *       is unaffected.
     *   3 — adds __handoffTo (2.3.0), which makes the manager rule NEWEST-WINS.
     *       A v1/v2 caller never calls it; a v3 caller checks
     *       __contractVersion >= 3 before it does, and registers (loudly) when
     *       the manager cannot hand over.
     */
    var CONTRACT_VERSION = 3;

    /** @type {string} Console prefix for every message this kit emits. */
    var LOG = '[RefreshKit]';

    /** @type {string} Shared storage key for the cross-tab reload budget. */
    var BUDGET_KEY = 'jellyfin-refresh-kit-budget-v1';

    /**
     * Per-TAB (sessionStorage) record of version transitions this tab has
     * already reloaded for: "<instance>|<from>><to>". It exists to break a
     * flapping version source — if we reloaded X → Y and are now asked to go
     * Y → X, the endpoint is oscillating, not releasing.
     * @type {string}
     */
    var FLIP_KEY = 'jellyfin-refresh-kit-flips-v1';

    /**
     * Per-TAB (sessionStorage) set of versions this tab has already reloaded
     * AWAY FROM: "<instance>|<version>". This — not the pair record above — is
     * what the flap guard actually asks, because the pair record can only close
     * a cycle of length two.
     *
     * A pair-only guard permits a reload P → Q whenever the exact record Q > P
     * is absent, and then writes P > Q. The record set can therefore never hold
     * both directions of a pair, so with THREE identities (three replicas
     * behind a round robin, each reporting its own DLL mtime for the same
     * release) the cyclic orientation {A>B, B>C, C>A} is ABSORBING: every
     * baseline has exactly one always-permitted forward edge, no new record is
     * ever added, and the tab reloads forever — throttled by the reload budget,
     * never stopped by it, because each reload sits in its own rolling window.
     *
     * Asking "have I already left the version I am being asked to go to?"
     * closes cycles of ANY length: A → B is allowed, B → C is allowed, C → A is
     * refused because A was abandoned. A genuine release chain only ever moves
     * to versions this tab has never run, so it is unaffected.
     * @type {string}
     */
    var LEFT_KEY = 'jellyfin-refresh-kit-left-v1';

    /**
     * Per-TAB record of one-shot recoveries already spent this browsing
     * session, so a chronically broken endpoint cannot turn a recovery into a
     * reload loop. Holds instance-scoped markers (unversioned-entry recovery,
     * boot-seed disagreement).
     * @type {string}
     */
    var RECOVERY_KEY = 'jellyfin-refresh-kit-recovery-v1';

    /** @type {number} Cap on remembered flip records (per tab, all instances). */
    var MAX_FLIP_RECORDS = 24;

    /** @type {number} Rolling window for the reload budget, in ms. */
    var BUDGET_WINDOW_MS = 60000;

    /**
     * Minimum settle time after the last user interaction, even when the caller
     * configures idleSeconds: 0. Reloading in the same task as a click steals
     * the click from whatever handler was about to run.
     * @type {number}
     */
    var MIN_SETTLE_MS = 1000;

    /** @type {number} How often to re-test the safety gate while blocked. */
    var RETRY_MS = 1000;

    /**
     * How long after `location.reload()` the document is allowed to still be
     * here before the kit concludes the navigation is not going to happen.
     *
     * Some hosts BLOCK or IGNORE a scripted reload: an embedded WebView or
     * Electron shell that intercepts navigation, a sandboxed frame where
     * reload() throws, a `beforeunload` confirm the user answers with "Stay".
     * The document surviving this window is the proof, and it is the only proof
     * available — there is no callback for "the navigation was refused".
     * Same value the reference uses (client-refresh.js RELOAD_SURVIVAL_WATCHDOG_MS).
     * @type {number}
     */
    var RELOAD_SURVIVAL_WATCHDOG_MS = 3000;

    /**
     * Floor between two version fetches (PER INSTANCE — each instance has its
     * own endpoint). visibilitychange/focus/pageshow all fire in a burst when a
     * tab is restored; without a floor that is three fetches back to back.
     * @type {number}
     */
    var MIN_FETCH_GAP_MS = 5000;

    /** @type {number} Hard cap on consecutive blocked-reload retries (~10 min). */
    var MAX_BLOCKED_RETRIES = 600;

    /**
     * How long a <video>/<audio> element may hold the reload gate with ZERO
     * playback progress before the kit stops treating it as a live session.
     * Deliberately the same ~10 minutes the 1Hz retry ladder covers: past that
     * the element is not a session anyone is having, it is a decoration (or a
     * permanently stalled load) that would otherwise switch layer 3 off for the
     * life of the page. Every other gate still applies after the escape.
     * @type {number}
     */
    var MEDIA_STARVATION_MS = MAX_BLOCKED_RETRIES * RETRY_MS;

    /**
     * Delay before the confirmation fetch that promotes a freshly-sighted
     * candidate version into a real update. A candidate must be seen TWICE in
     * a row (with no sighting of the baseline in between) before it can arm a
     * reload, which is what stops an oscillating version source from reloading
     * the tab once per poll. Waiting a whole pollSeconds for that second
     * observation would double every adopter's update latency, so the kit
     * schedules the confirmation itself, shortly after the first sighting.
     * @type {number}
     */
    var VERSION_CONFIRM_MS = 1500;

    /**
     * Hard ceiling on ONE version check (fetch or a caller's getVersion()).
     * fetch() has no timeout of its own: a proxy that accepts the connection and
     * never answers leaves the promise permanently unsettled, and every re-arm
     * of the poll loop used to hang off that promise — so one stuck request
     * killed update detection for the life of the tab, silently, with
     * state() reporting only `polling: false`. The reference sets the same
     * 10s ceiling on every state poll (client-refresh.js STATE_TIMEOUT_MS).
     * @type {number}
     */
    var VERSION_FETCH_TIMEOUT_MS = 10000;

    /**
     * Bootstrap mode only: how long to wait for the FIRST version fetch before
     * giving up and loading the entry files unversioned. This is the dial
     * between freshness and availability, and availability has to win — a
     * version.json that 404s must never cost the user their plugin.
     * @type {number}
     */
    var DEFAULT_ENTRY_TIMEOUT_MS = 3000;

    // ─────────────────────────────────────────────────────────────────────────
    // Tiny safe helpers
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Run `fn`, swallowing anything it throws. The kit is a guest on someone
     * else's page; a bug in here must degrade to "the kit stops working", never
     * to "Jellyfin stops working".
     * @template T
     * @param {() => T} fn
     * @param {T} [fallback] Returned when `fn` throws.
     * @returns {T|undefined}
     */
    function safe(fn, fallback) {
        try {
            return fn();
        } catch (err) {
            try { console.debug(LOG, 'suppressed error:', err); } catch (_) { /* console itself is gone */ }
            return fallback;
        }
    }

    /**
     * Clamp a number into [min, max], falling back when it is not a finite
     * number (covers undefined, null, NaN, "12abc" → NaN, Infinity).
     * @param {*} value
     * @param {number} min
     * @param {number} max
     * @param {number} fallback
     * @returns {number}
     */
    function clampNumber(value, min, max, fallback) {
        var n = typeof value === 'string' ? Number(value) : value;
        if (typeof n !== 'number' || !isFinite(n)) return fallback;
        return Math.min(max, Math.max(min, n));
    }

    /**
     * Parse a kit version string into numeric segments.
     *
     * Deliberately forgiving and deliberately DUMB: each dot-separated segment
     * contributes its LEADING digits, everything else in the segment is
     * ignored, and a missing segment is 0. So "2.3.0" → [2,3,0], "2.3.0.1" →
     * [2,3,0,1], "2.3.0-rc1" → [2,3,0]. A string whose first segment holds no
     * digits at all ("unknown", "", a manager that never set kitVersion) is
     * UNPARSEABLE and returns null — the caller must then not act on a guess.
     *
     * A pre-release suffix is therefore neither newer nor older than its
     * release, which is the safe reading for the one decision this feeds: two
     * copies that compare EQUAL leave the sitting manager in place.
     * @param {*} v
     * @returns {number[]|null}
     */
    function parseKitVersion(v) {
        if (typeof v !== 'string' || !v) return null;
        var parts = v.split('.');
        var out = [];
        for (var i = 0; i < parts.length; i++) {
            var m = /^\s*(\d+)/.exec(parts[i]);
            if (i === 0 && !m) return null;
            out.push(m ? parseInt(m[1], 10) : 0);
        }
        return out;
    }

    /**
     * Compare two kit versions SEGMENT BY SEGMENT AS NUMBERS. String compare
     * would be wrong in the ordinary case the manager rule depends on:
     * "2.10.0" < "2.9.0" lexicographically, and a kit that mistakes its newest
     * copy for its oldest is worse than one that never compares at all.
     * @param {*} a
     * @param {*} b
     * @returns {number} 1 when a > b, -1 when a < b, 0 when equal OR when
     *   either side is unparseable (an unknown version never wins).
     */
    function compareKitVersions(a, b) {
        var pa = parseKitVersion(a);
        var pb = parseKitVersion(b);
        if (!pa || !pb) return 0;
        var len = Math.max(pa.length, pb.length);
        for (var i = 0; i < len; i++) {
            var x = i < pa.length ? pa[i] : 0;
            var y = i < pb.length ? pb[i] : 0;
            if (x !== y) return x > y ? 1 : -1;
        }
        return 0;
    }

    /**
     * Guarantee that a promise SETTLES. Whatever the wrapped work does — a
     * fetch against a proxy that never answers, a caller's getVersion() that
     * awaits something dead — the returned promise rejects after `ms` at the
     * latest, so nothing downstream can be parked forever waiting on it.
     *
     * `onTimeout` is the optional teardown for the abandoned work (aborting the
     * request, so the connection is freed rather than leaked); it runs at most
     * once and its failure is swallowed.
     *
     * @template T
     * @param {Promise<T>} promise
     * @param {number} ms
     * @param {string} label Used in the timeout Error's message.
     * @param {() => void} [onTimeout]
     * @returns {Promise<T>}
     */
    function withTimeout(promise, ms, label, onTimeout) {
        return new Promise(function (resolve, reject) {
            var settled = false;
            var timer = setTimeout(function () {
                if (settled) return;
                settled = true;
                if (onTimeout) safe(onTimeout);
                reject(new Error(label + ' timed out after ' + ms + 'ms'));
            }, ms);
            promise.then(function (value) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                resolve(value);
            }, function (err) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                reject(err);
            });
        });
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Configuration
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * @typedef {Object} RefreshKitConfig
     * @property {string}   [name]              Instance name. Default: derived from versionUrl's parent folder, else "instance-<index>".
     * @property {string}   [versionUrl]        Endpoint returning the current version. Required for polling.
     * @property {string}   [versionJsonField]  If set, the response is parsed as JSON and this field is read (e.g. "version").
     * @property {string}   [bootVersion]       Identity of the build that SERVED this document, stamped into the tag by the server. Seeds the baseline so an update landing between page-serve and the first poll is detected instead of absorbed. Must name the same identity the version endpoint reports.
     * @property {() => Promise<string>} [getVersion] Config-object only. Overrides versionUrl entirely.
     * @property {number}   [pollSeconds]       Poll interval while visible. Default 60, clamped 15–3600.
     * @property {number}   [idleSeconds]       Required user-idle time before an auto reload. Default 5, clamped 0–300. Page-level reloads use the MAX among instances wanting one.
     * @property {Array<string|RegExp>} [assetPatterns] Substrings/regexes; matching script/link URLs get ?v=<this instance's version>.
     * @property {string[]} [entryScripts] BOOTSTRAP MODE. URLs the kit loads itself, IN ORDER, after this instance's version resolves. Empty = classic mode.
     * @property {number}   [entryTimeoutMs] Max wait for the first version fetch before loading entries unversioned. Default 3000, clamped 250–30000.
     * @property {'auto'|'notify'|'off'} [mode] Default 'auto'.
     * @property {(newV: string, oldV: string) => void} [onUpdateAvailable] Called once per detected version change.
     * @property {number}   [reloadBudget]      Max reloads per 60s window. Default 3. Page-level budget is the MIN among all instances.
     */

    /** @type {Required<Pick<RefreshKitConfig,'pollSeconds'|'idleSeconds'|'mode'|'reloadBudget'>> & RefreshKitConfig} */
    var DEFAULTS = {
        name: '',
        versionUrl: '',
        versionJsonField: '',
        bootVersion: '',
        getVersion: null,
        pollSeconds: 60,
        idleSeconds: 5,
        assetPatterns: [],
        entryScripts: [],
        entryTimeoutMs: DEFAULT_ENTRY_TIMEOUT_MS,
        mode: 'auto',
        onUpdateAvailable: null,
        reloadBudget: 3
    };

    /**
     * Read `data-*` attributes off THIS copy's own <script> tag.
     *
     * document.currentScript is only valid during the SYNCHRONOUS execution of
     * the script, which is exactly where this runs — so we capture it now and
     * never rely on it again. Note that a `defer`/`async` tag still reports
     * currentScript correctly; only eval()/injected-text execution does not,
     * which is why the window-config path exists as an escape hatch.
     *
     * Attribute names are the kebab-case form of the option names:
     *   data-name, data-version-url, data-version-json-field, data-boot-version,
     *   data-poll-seconds, data-idle-seconds, data-asset-patterns
     *   (comma-separated), data-mode, data-reload-budget, data-entry-scripts
     *   (comma-separated, ORDER MATTERS), data-entry-timeout-ms
     *
     * @returns {Partial<RefreshKitConfig>}
     */
    function readScriptTagConfig() {
        var el = document.currentScript;
        if (!el || !el.dataset) return {};
        var d = el.dataset;
        /** @type {Partial<RefreshKitConfig>} */
        var out = {};
        if (d.name) out.name = d.name;
        if (d.versionUrl) out.versionUrl = d.versionUrl;
        if (d.versionJsonField) out.versionJsonField = d.versionJsonField;
        if (d.bootVersion) out.bootVersion = d.bootVersion;
        if (d.pollSeconds) out.pollSeconds = Number(d.pollSeconds);
        if (d.idleSeconds) out.idleSeconds = Number(d.idleSeconds);
        if (d.mode) out.mode = /** @type {any} */ (d.mode);
        if (d.reloadBudget) out.reloadBudget = Number(d.reloadBudget);
        if (d.entryTimeoutMs) out.entryTimeoutMs = Number(d.entryTimeoutMs);
        if (d.entryScripts) {
            // Comma-separated, order-significant. A URL containing a literal
            // comma would have to use the window-config path instead.
            out.entryScripts = d.entryScripts
                .split(',')
                .map(function (s) { return s.trim(); })
                .filter(Boolean);
        }
        if (d.assetPatterns) {
            // Comma-separated substrings only. Regex cannot survive an HTML
            // attribute unambiguously, so regex support is config-object only.
            out.assetPatterns = d.assetPatterns
                .split(',')
                .map(function (s) { return s.trim(); })
                .filter(Boolean);
        }
        return out;
    }

    // Capture this copy's tag config NOW, while currentScript is still valid —
    // regardless of whether this copy ends up being the manager or a mere
    // registrant (REGISTRATION CONTRACT clause 2).
    var tagConfig = safe(readScriptTagConfig, {}) || {};

    /**
     * Derive a deterministic instance name from a versionUrl: the last
     * DIRECTORY segment of its path. "/web/KefinTweaks/version.json" →
     * "KefinTweaks"; "/MyPlugin/RefreshVersion" → "MyPlugin". Deterministic on
     * purpose — the same adoption always resolves to the same name, which is
     * what makes accidental double-registration a silent dedupe, and what lets
     * the singular-global rule recognise "the global names my endpoint, my tag
     * names me" as the SAME adoption rather than a disagreement.
     * @param {string} versionUrl
     * @returns {string} Derived name, or '' when it cannot be derived.
     */
    function deriveName(versionUrl) {
        if (typeof versionUrl !== 'string' || !versionUrl) return '';
        var s = versionUrl.split('#')[0].split('?')[0];
        s = s.replace(/^[a-z][a-z0-9+.-]*:\/\/[^/]*/i, ''); // strip scheme://host
        var parts = s.split('/').filter(Boolean);
        if (parts.length >= 2) return parts[parts.length - 2];
        if (parts.length === 1) {
            var stem = parts[0].replace(/\.[^.]*$/, '');
            return stem || parts[0];
        }
        return '';
    }

    /**
     * The ADOPTION IDENTITY a config declares, using exactly the kit's own
     * naming rule: an explicit `name`, else the name derived from `versionUrl`.
     * "" when the config identifies nobody.
     *
     * Using the same function the registry names instances with is what makes
     * the singular-global rule agree with the rest of the kit: a global holding
     * `versionUrl: "/web/KefinTweaks/version.json"` and a tag holding
     * `data-name="KefinTweaks"` are the SAME adoption — the kit would name both
     * "KefinTweaks" — even though neither identifier literally equals the other.
     * @param {Object} cfg
     * @returns {string}
     */
    function adoptionIdentity(cfg) {
        if (!cfg || typeof cfg !== 'object') return '';
        var n = typeof cfg.name === 'string' ? cfg.name.trim() : '';
        if (n) return n;
        return deriveName(typeof cfg.versionUrl === 'string' ? cfg.versionUrl : '');
    }

    /**
     * Property used to record that a singular window config object has already
     * been claimed by a kit tag. Non-enumerable, so it never reaches a merged
     * config through the `for (k in w)` copy below (or through JSON, or through
     * an adopter's own inspection of the object they wrote).
     * @type {string}
     */
    var SINGULAR_CLAIM_KEY = '__jellyfinRefreshKitBoundToTag';

    /**
     * Non-enumerable window property holding the page-level WeakSet of singular
     * config objects that have already been claimed. It is the SECOND HALF of
     * the claim, and the reason "unmarkable" no longer means "undecidable": an
     * object that cannot carry the marker above — frozen, sealed, otherwise
     * non-extensible, an exotic proxy — is tracked BY IDENTITY here instead.
     *
     * Freezing an object you publish on `window` for a third-party script to
     * read is an ordinary defensive habit (this kit freezes its own API objects
     * for the same reason), and before 2.2.0 an unmarkable global fell back to
     * the page-level question "has any kit copy already run?" — true for every
     * adopter after the first, so an adopter's OWN frozen config was refused
     * with a warning that blamed an earlier tag. Identity is the right question
     * and a WeakSet answers it exactly, with no retention.
     *
     * The manager exposes the same store as `__claimSingularGlobal` (REGISTRATION
     * CONTRACT clause 6) so a copy that cannot define window properties can still
     * take part in the one claim.
     * @type {string}
     */
    var SINGULAR_CLAIM_STORE_KEY = '__jellyfinRefreshKitClaimedGlobals';

    /**
     * The page-level identity store for unmarkable singular globals, created on
     * first need by whichever copy needs it (that is the first kit copy on the
     * page in practice, i.e. the manager).
     * @returns {Object|null} Something with .has()/.add(), or null.
     */
    function singularClaimStore() {
        return safe(function () {
            var existing = window[SINGULAR_CLAIM_STORE_KEY];
            if (existing && typeof existing.has === 'function' && typeof existing.add === 'function') {
                return existing;
            }
            if (typeof WeakSet !== 'function') return null;
            Object.defineProperty(window, SINGULAR_CLAIM_STORE_KEY, {
                value: new WeakSet(), writable: false, configurable: false, enumerable: false
            });
            var installed = window[SINGULAR_CLAIM_STORE_KEY];
            return (installed && typeof installed.has === 'function' &&
                typeof installed.add === 'function') ? installed : null;
        }, null) || null;
    }

    /**
     * CLAIM a singular window config object for the caller, if nobody has it.
     *
     * "Claimable exactly once" is the invariant the whole singular-global rule
     * rests on (see singularGlobalOutcome): the 1.x global is a
     * one-adoption-per-page form, the kit never clears it, and it therefore
     * stays live at every later kit tag in the document. Whoever takes it first
     * owns it; everybody else is told so.
     *
     * Two mechanisms, tried in order, because they cover disjoint object shapes:
     *   1. a non-enumerable marker ON the object (works for any extensible
     *      object, survives across kit copies and kit versions, needs no
     *      page-level state at all);
     *   2. the page-level WeakSet, for objects that cannot be marked.
     *
     * @param {Object} w The live window.JellyfinRefreshKitConfig object.
     * @returns {boolean} True when THIS caller now holds the claim; false when
     *   an earlier consumer already did.
     */
    function claimSingularGlobal(w) {
        var alreadyMarked = safe(function () { return w[SINGULAR_CLAIM_KEY] === true; }, false) === true;
        if (alreadyMarked) return false;
        // ASK BEFORE MARKING (2.3.0). defineProperty on a non-extensible object
        // — frozen, sealed, preventExtensions'd — THROWS, and safe() turns every
        // such throw into a `suppressed error:` console.debug line. Freezing a
        // config object you publish on `window` is the ordinary defensive habit
        // this fallback exists to support, so the WeakSet path below is the
        // INTENDED route for those objects, not an error path: take it silently.
        // (Exotic hosts with no Object.isExtensible fall through to the try —
        // the pre-check only ever skips work that was going to throw.)
        var extensible = safe(function () {
            return typeof Object.isExtensible !== 'function' || Object.isExtensible(w);
        }, true) !== false;
        var marked = extensible && safe(function () {
            Object.defineProperty(w, SINGULAR_CLAIM_KEY, {
                value: true,
                enumerable: false,
                configurable: true,
                writable: true
            });
            return w[SINGULAR_CLAIM_KEY] === true;
        }, false) === true;
        if (marked) return true;

        var store = singularClaimStore();
        if (store) {
            if (safe(function () { return store.has(w); }, false) === true) return false;
            safe(function () { store.add(w); });
            return true;
        }

        // This copy cannot reach the page-level store (a window that refuses
        // new properties). A manager speaking contract revision 2 owns one:
        // ask it. `api` is this closure's own manager object when THIS copy is
        // the manager, and skipping it there is what stops the delegation from
        // recursing into itself.
        var delegated = safe(function () {
            var mgr = window.JellyfinRefreshKit || window.__jellyfinRefreshKitManager || null;
            if (!mgr || mgr === api || typeof mgr.__claimSingularGlobal !== 'function') return null;
            var r = mgr.__claimSingularGlobal(w);
            return (r === true || r === false) ? r : null;
        }, null);
        if (delegated === true || delegated === false) return delegated;

        // Neither mechanism is available (a frozen object on an engine with no
        // WeakSet, a window that refuses new properties, and no contract-v2
        // manager). Nothing can record a claim here, so nothing can evidence
        // somebody else's: GRANT it.
        // Declining an adopter's own config object is strictly worse than the
        // leak this guards against, and the disagreement branch of the rule
        // still refuses every global that names a different adoption.
        safe(function () {
            console.debug(LOG, 'the singular window config object can be neither claim-marked nor ' +
                'tracked by identity on this page; treating it as unclaimed.');
        });
        return true;
    }

    /**
     * Config keys that IDENTIFY an adoption rather than describe how it
     * behaves. Cadence (pollSeconds), idle window, mode, reload budget and
     * entry timeout are deliberately absent: they say how an adoption acts, not
     * which one it is.
     * @type {string[]}
     */
    var IDENTIFYING_KEYS = ['name', 'versionUrl', 'getVersion', 'bootVersion',
        'assetPatterns', 'entryScripts', 'onUpdateAvailable'];

    /**
     * Does this config (raw tag-level or normalized) say anything that could
     * identify the adoption it belongs to? Used by the singular-global rule and
     * by the anonymous dedupe scan — one definition, so "declares nothing of
     * its own" means the same thing everywhere.
     * @param {Object} cfg
     * @returns {boolean}
     */
    function declaresIdentity(cfg) {
        if (!cfg || typeof cfg !== 'object') return false;
        for (var i = 0; i < IDENTIFYING_KEYS.length; i++) {
            var v = cfg[IDENTIFYING_KEYS[i]];
            if (v === undefined || v === null) continue;
            if (typeof v === 'string') { if (v.trim() !== '') return true; continue; }
            if (Array.isArray(v)) { if (v.length > 0) return true; continue; }
            if (v) return true;
        }
        return false;
    }

    /**
     * THE singular-window-config rule. ONE function, ONE truth table, used by
     * BOTH consumers of window.JellyfinRefreshKitConfig:
     *   • the TAG-SIDE merge every 2.1+ copy performs at its own <script>
     *     position (`ownConfig` below), and
     *   • the MANAGER-SIDE fallback that serves copies which cannot read the
     *     global themselves — pre-2.1 copies and eval'd copies with no
     *     currentScript (applySingularWindowConfigFallback).
     *
     * Before 2.2.0 these were two copies of an accumulating matrix of
     * name/versionUrl agreement special cases, and the matrix leaked a critical
     * in three consecutive review rounds (a global that named ITSELF was treated
     * as agreeing with a tag that named nothing, because the "disagrees" test
     * needed the same identifier kind on both sides). One uniform rule replaces
     * all of it.
     *
     * ─────────────────────────────────────────────────────────────────────────
     * THE RULE. The global is CLAIMABLE EXACTLY ONCE (claimSingularGlobal
     * above). A consumer may claim an UNCLAIMED global iff any of:
     *   (a) the global POSITIVELY IDENTIFIES this consumer — same `versionUrl`,
     *       or the same ADOPTION IDENTITY, which is the kit's own naming rule:
     *       `name`, else the name derived from `versionUrl` (adoptionIdentity).
     *       So a global holding the endpoint and a tag holding `data-name`
     *       identify the same adoption whenever the kit would name both the
     *       same thing — which is the documented "endpoint in the global,
     *       behavioural keys in the global, name on the tag" shape;
     *   (b) the global carries NO identifier at all (neither `name` nor a
     *       `versionUrl` to derive one from) and this consumer is the textually
     *       associated one, i.e. the first to reach that object;
     *   (c) the consumer declares NO identifying config of its own (the same
     *       adoption injected twice, and the bare eval'd/JS-Injector shape where
     *       the global IS the whole configuration).
     * A ONE-SIDED IDENTIFIER MISMATCH — the global identifies an adoption and
     * the consumer declares ANY identifying config that does not positively
     * match it — is a DISAGREEMENT, and is skipped with one warning WITHOUT
     * claiming, so the adoption the global actually names can still take it at
     * its own tag, whichever order the two tags appear in.
     * ─────────────────────────────────────────────────────────────────────────
     *
     * TRUTH TABLE (exhaustive; "identifying config" = any of IDENTIFYING_KEYS;
     * identity(cfg) = cfg.name || deriveName(cfg.versionUrl)):
     *
     *  GLOBAL                      CONSUMER                       CLAIM      OUTCOME
     *  ─────────────────────────── ────────────────────────────── ────────── ─────────────
     *  identity "A" (name and/or   same identity "A" (by name,    unclaimed  APPLY   (a)
     *    a versionUrl deriving A)    by versionUrl, or by either)  claimed    SKIP  claimed
     *  identity "A"                identity "B"                   either     SKIP  disagree
     *  identity "A"                no identity, but declares      either     SKIP  disagree
     *                                identifying config
     *                                (patterns/entries/bootVersion/
     *                                 getVersion/onUpdateAvailable)
     *  identity "A"                declares nothing identifying   unclaimed  APPLY   (c)
     *                                (bare tag, or only mode /    claimed    SKIP  claimed
     *                                 pollSeconds / idleSeconds /
     *                                 reloadBudget / entryTimeoutMs)
     *  identity "A" AND a          matches EITHER the identity    unclaimed  APPLY   (a)
     *    versionUrl                  or the versionUrl exactly    claimed    SKIP  claimed
     *  anonymous (no name, no      anything at all                unclaimed  APPLY (b)/(c)
     *    versionUrl)                                              claimed    SKIP  claimed
     *
     * Consequences worth stating out loud, because they are the whole point:
     *   • A global that names ADOPTION A can never reach adopter B, whatever B
     *     declares and in whatever order the tags appear — B either positively
     *     matches (then it IS A) or disagrees. That is round-4's asymmetric-guard
     *     critical killed by construction rather than by another special case.
     *   • Several adopters each ASSIGNING their own global above their own tag
     *     are unaffected: those are distinct objects, each claimed by its author.
     *   • The same payload injected twice assigns a NEW object per evaluation,
     *     so each copy claims its own and both apply — and the registry's
     *     equivalence dedupe (configsEquivalent, which compares callbacks by
     *     source text since 2.2.0) collapses them into one instance. ONE object
     *     surviving to a second consumer is the leak, and is refused: a second
     *     BARE tag sharing one already-claimed global registers on defaults
     *     (inert, with the skip warning saying why) instead of silently
     *     inheriting somebody's configuration.
     *
     * @param {Object} w The live window.JellyfinRefreshKitConfig object.
     * @param {Object} consumer The config this global would be merged into,
     *   exactly as its tag declared it (data-* on the tag side, the arriving
     *   copy's raw config on the manager side).
     * @returns {'apply'|'disagreement'|'claimed'}
     */
    function singularGlobalOutcome(w, consumer) {
        var wUrl = typeof w.versionUrl === 'string' ? w.versionUrl : '';
        var cUrl = (consumer && typeof consumer.versionUrl === 'string') ? consumer.versionUrl : '';
        var wId = adoptionIdentity(w);
        var cId = adoptionIdentity(consumer);
        var globalIdentifies = !!wId || !!wUrl;
        var positiveMatch = (!!wUrl && wUrl === cUrl) || (!!wId && wId === cId);
        if (globalIdentifies && !positiveMatch && declaresIdentity(consumer)) return 'disagreement';
        return claimSingularGlobal(w) ? 'apply' : 'claimed';
    }

    /**
     * Human-readable identity of a config, for the rule's warnings.
     * @param {Object} cfg
     * @returns {string}
     */
    function describeAdoption(cfg) {
        var n = (cfg && typeof cfg.name === 'string') ? cfg.name.trim() : '';
        if (n) return '"' + n + '"';
        var u = (cfg && typeof cfg.versionUrl === 'string') ? cfg.versionUrl : '';
        return u || '';
    }

    /**
     * Run the rule above and act on it: merge on APPLY, warn once otherwise.
     * The single point where window.JellyfinRefreshKitConfig is ever consumed.
     * @param {Object} consumer Mutated in place on APPLY.
     * @param {Object} w The live singular global.
     * @returns {'apply'|'disagreement'|'claimed'}
     */
    function applySingularGlobal(consumer, w) {
        var outcome = singularGlobalOutcome(w, consumer);
        if (outcome === 'apply') {
            for (var k in w) {
                if (Object.prototype.hasOwnProperty.call(w, k)) consumer[k] = w[k];
            }
            return outcome;
        }
        var who = describeAdoption(consumer);
        var mine = who ? ' (' + who + ')' : '';
        if (outcome === 'disagreement') {
            safe(function () {
                console.warn(LOG, 'window.JellyfinRefreshKitConfig identifies ' + describeAdoption(w) +
                    ', which is not the adoption this tag declares' + mine + ' — NOT applying it to this ' +
                    'instance. The singular global is a 1.x, one-adoption-per-page form and the kit never ' +
                    'clears it, so it is still live at every later kit tag; a global that identifies one ' +
                    'adoption is claimable only by that adoption, or by a tag that declares nothing ' +
                    'identifying of its own. Use window.JellyfinRefreshKitConfigs = ' +
                    '{ "<instance name>": {...} } to configure a specific instance.');
            });
        } else {
            safe(function () {
                console.warn(LOG, 'window.JellyfinRefreshKitConfig ' + (describeAdoption(w)
                    ? 'identifies ' + describeAdoption(w)
                    : 'declares neither "name" nor "versionUrl", so it identifies no adoption') +
                    ', and an earlier kit tag has already CLAIMED it — NOT applying it to this instance' +
                    mine + '. A singular global is claimable exactly ONCE: the kit never clears it, so ' +
                    "applying it again would silently replace this tag's own assetPatterns/mode/callback " +
                    "with somebody else's. Use window.JellyfinRefreshKitConfigs = " +
                    '{ "<instance name>": {...} } to configure a specific instance.');
            });
        }
        return outcome;
    }

    /**
     * The 1.x singular window config belongs to the TAG it was authored next
     * to, not to "whichever instance registers first". README §(a) documents
     * writing `window.JellyfinRefreshKitConfig = {...}` in an inline script
     * immediately before the kit's own tag — which is the ONLY way a bare tag
     * can pass a RegExp assetPattern, getVersion, or onUpdateAvailable. So each
     * copy reads it HERE, in the same synchronous breath that captured
     * currentScript, and merges it over its own data-* before registering.
     *
     * On a single-adopter page this is byte-identical to 1.x (window wins over
     * data-*, both win over defaults). On a multi-adopter page it is the only
     * reading that attributes each config to its author.
     *
     * WHICH global reaches WHICH tag is decided by exactly one function —
     * applySingularGlobal / singularGlobalOutcome above, whose doc block holds
     * the rule and its full truth table. The manager-side fallback for copies
     * that cannot read the global themselves calls the SAME function, so there
     * is one rule on this page, not two implementations of one intention.
     *
     * `__singularApplied` tells the manager this copy already SETTLED the
     * singular global for its own tag — whether by merging it or by declining
     * it — so the manager's fallback (which serves pre-2.1 and eval'd copies)
     * does not apply it a second time somewhere else; an older manager simply
     * ignores the unknown key, which leaves the pre-2.1 behaviour exactly as
     * it was.
     */
    var ownConfig = (function () {
        var out = {};
        var k;
        for (k in tagConfig) {
            if (Object.prototype.hasOwnProperty.call(tagConfig, k)) out[k] = tagConfig[k];
        }
        var w = safe(function () {
            var g = window.JellyfinRefreshKitConfig;
            return (g && typeof g === 'object') ? g : null;
        }, null);
        if (w) {
            safe(function () { applySingularGlobal(out, w); });
            // Either way this copy has SETTLED the singular global for its own
            // tag; the manager must not re-apply it on this registration.
            out.__singularApplied = true;
        }
        return out;
    })();

    // ─────────────────────────────────────────────────────────────────────────
    // Role decision (REGISTRATION CONTRACT clause 2)
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * The manager this copy is TAKING OVER FROM, or null. Set only on the
     * newest-wins path (REGISTRATION CONTRACT clause 2b): this copy falls
     * through into the manager body below and performs the handoff itself,
     * because the handoff has to pass THIS copy's own `api` object, which does
     * not exist until that body has been evaluated.
     * @type {Object|null}
     */
    var handoffFrom = null;

    var existingManager = safe(function () { return window.JellyfinRefreshKit; }, null) || null;
    if (!existingManager || typeof existingManager.__registerInstance !== 'function') {
        // The global is missing or is not a 2.x manager. Before concluding "a
        // 1.x singleton owns this page" (clause 2c — permanently inert), check
        // the manager's own backup handle: a 1.x copy running SECOND, or any
        // unrelated plugin, may simply have overwritten the global out from
        // under a live 2.x manager. Recovering is always better than stranding.
        var backupManager = safe(function () { return window.__jellyfinRefreshKitManager; }, null) || null;
        if (backupManager && typeof backupManager.__registerInstance === 'function') {
            existingManager = backupManager;
        }
    }
    if (existingManager) {
        if (typeof existingManager.__registerInstance === 'function') {
            // A 2.x manager already owns the page. WHICH copy should run it?
            //
            // Until 2.3.0 the answer was always "the one that got here first",
            // and that made the kit's own safety fixes un-shippable: the page
            // ran the FIRST-loaded copy's reload engine, flip guard and gates
            // even when a newer copy — with the fix for the very loop the page
            // was in — was sitting right there as a registered instance. A live
            // 4-copy test proved it end to end (a 2.1.2 copy parsing ahead of a
            // 2.2.0 copy reload-looped a tab that 2.2.0 alone had latched shut).
            //
            // Since 2.3.0 the rule is NEWEST WINS, implemented as a handoff so
            // that nothing about the page is rebuilt from scratch: only a
            // STRICTLY newer copy takes over, and only from a manager that
            // speaks contract revision 3, which is what makes the takeover
            // lossless. Equal versions never hand over — the sitting manager
            // stays, so two identical copies behave exactly as they did in 2.2.
            var managerKitVersion = safe(function () {
                return String(existingManager.kitVersion == null ? '' : existingManager.kitVersion);
            }, '') || '';
            var managerContract = safe(function () {
                var n = Number(existingManager.__contractVersion);
                return isFinite(n) ? n : 0;
            }, 0) || 0;
            var iAmNewer = compareKitVersions(KIT_VERSION, managerKitVersion) > 0;
            var canHandOff = managerContract >= 3 &&
                safe(function () { return typeof existingManager.__handoffTo === 'function'; }, false) === true;

            if (iAmNewer && canHandOff) {
                // Fall through into the manager body; the boot section performs
                // the handoff and adopts everything the old manager was running.
                handoffFrom = existingManager;
            } else {
                if (iAmNewer) {
                    // A newer copy that CANNOT take over. This can only be a
                    // pre-2.3.0 manager, i.e. a kit copy from before the
                    // newest-wins rule existed — which is why no pre-2.3.0 copy
                    // may ever be shipped publicly. Say so loudly and name both
                    // versions: the page's reload semantics (flip guard, budget,
                    // gates, interception) are the OLDER copy's, so any bug
                    // fixed since is still live on this page.
                    safe(function () {
                        console.warn(LOG, 'this ' + KIT_VERSION + ' copy is NEWER than the kit copy ' +
                            'already managing this page (' + (managerKitVersion || 'unknown') +
                            ', registration contract ' + managerContract + '), but that manager is too ' +
                            'old to hand the page over (handoff needs contract revision 3, added in ' +
                            '2.3.0). Registering as an instance, as before — which means PAGE-LEVEL ' +
                            'RELOAD SEMANTICS ON THIS PAGE ARE ' + (managerKitVersion || 'the older copy') +
                            "'S, not " + KIT_VERSION + "'s: the older copy's flip guard, reload budget, " +
                            'safety gates and createElement interception govern every instance here. ' +
                            'Ship 2.3.0 or newer in every adopting plugin.');
                    });
                }
                // Register and bow out: no second wrapper, no listeners, no
                // timers from this copy. The manager runs everything.
                safe(function () {
                    existingManager.__registerInstance(ownConfig, KIT_VERSION);
                });
            }
        } else {
            // A 1.x singleton owns the page. It already wrapped createElement
            // and owns the reload engine; fighting it would mean two wrappers
            // and two reload engines. Degrade gracefully: one warning, inert.
            safe(function () {
                console.warn(LOG, 'a pre-2.0 (singleton) jellyfin-refresh-kit already owns this page; ' +
                    'this ' + KIT_VERSION + ' copy is going inert. The plugin shipping the 1.x copy ' +
                    'should upgrade its kit so both can register as instances.');
            });
        }
        // The ONE case that keeps going is the newest-wins takeover, which needs
        // the whole manager body below (and its `api`) before it can ask the
        // sitting manager to hand the page over.
        if (!handoffFrom) return;
    }

    // ═════════════════════════════════════════════════════════════════════════
    // From here on: THIS COPY IS THE MANAGER — either because it is the first
    // kit copy on the page, or (2.3.0) because it is NEWER than the copy that
    // was managing it and is about to take the page over by handoff.
    // ═════════════════════════════════════════════════════════════════════════

    /**
     * Normalize + clamp a merged raw config so the rest of the file can trust
     * it. Only keys present in DEFAULTS are copied — unknown keys from a newer
     * kit copy's config are ignored (REGISTRATION CONTRACT clause 3).
     * @param {Object} raw
     * @returns {RefreshKitConfig}
     */
    function normalizeConfig(raw) {
        /** @type {any} */
        var cfg = {};
        var key;
        for (key in DEFAULTS) { if (Object.prototype.hasOwnProperty.call(DEFAULTS, key)) cfg[key] = DEFAULTS[key]; }
        for (key in DEFAULTS) {
            if (Object.prototype.hasOwnProperty.call(DEFAULTS, key) &&
                raw && Object.prototype.hasOwnProperty.call(raw, key) && raw[key] !== undefined) {
                cfg[key] = raw[key];
            }
        }

        cfg.name = typeof cfg.name === 'string' ? cfg.name.trim() : '';
        cfg.pollSeconds = clampNumber(cfg.pollSeconds, 15, 3600, DEFAULTS.pollSeconds);
        cfg.idleSeconds = clampNumber(cfg.idleSeconds, 0, 300, DEFAULTS.idleSeconds);
        cfg.reloadBudget = clampNumber(cfg.reloadBudget, 1, 100, DEFAULTS.reloadBudget);
        if (cfg.mode !== 'auto' && cfg.mode !== 'notify' && cfg.mode !== 'off') cfg.mode = DEFAULTS.mode;
        cfg.entryTimeoutMs = clampNumber(cfg.entryTimeoutMs, 250, 30000, DEFAULTS.entryTimeoutMs);
        if (!Array.isArray(cfg.assetPatterns)) cfg.assetPatterns = [];
        // Entries must be plain non-empty strings: they are appended to the
        // document, so a stray object/regex here is a broken page, not a
        // mis-versioned asset.
        cfg.entryScripts = Array.isArray(cfg.entryScripts)
            ? cfg.entryScripts.filter(function (u) { return typeof u === 'string' && u.trim() !== ''; })
                .map(function (u) { return u.trim(); })
            : [];
        if (typeof cfg.getVersion !== 'function') cfg.getVersion = null;
        if (typeof cfg.onUpdateAvailable !== 'function') cfg.onUpdateAvailable = null;
        cfg.versionUrl = typeof cfg.versionUrl === 'string' ? cfg.versionUrl : '';
        cfg.versionJsonField = typeof cfg.versionJsonField === 'string' ? cfg.versionJsonField : '';
        cfg.bootVersion = typeof cfg.bootVersion === 'string' ? cfg.bootVersion.trim() : '';
        // A boot identity that does not look like one (an HTML error page
        // stamped into the attribute, a template placeholder) is worse than
        // none: it would make every load look like an update.
        if (cfg.bootVersion.length > 200 || cfg.bootVersion.charAt(0) === '<') cfg.bootVersion = '';
        return cfg;
    }

    /**
     * Are these two option values the same, for dedupe purposes?
     *
     * Reference equality first, then — for FUNCTIONS ONLY — source text.
     * `getVersion` and `onUpdateAvailable` can only be passed through a config
     * object, and the documented double-injection shape (an injector applying
     * the same nameless payload twice, one kit tag per slot) RE-EVALUATES that
     * object literal, minting a fresh closure each time. Reference equality
     * therefore fails for exactly the adoptions that actually poll, and the
     * "one instance, not two" promise turned into two instances polling the
     * same endpoint and firing the user's toast twice per release. Source text
     * is stable across re-evaluations of the same payload, which is the
     * question being asked; it is also how assetPatterns are already compared
     * (by String() form), so this is the existing convention, not a new one.
     *
     * Two DIFFERENT functions that happen to share source text are, for this
     * purpose, the same adoption re-injected — which is precisely the case
     * being detected.
     * @param {*} x
     * @param {*} y
     * @returns {boolean}
     */
    function optionsEquivalent(x, y) {
        if (x === y) return true;
        if (typeof x === 'function' && typeof y === 'function') {
            return safe(function () { return String(x) === String(y); }, false) === true;
        }
        return false;
    }

    /**
     * Structural config equivalence, for silent dedupe of an accidental double
     * include of the SAME adoption. Compared on normalized configs: scalars by
     * ===, functions by reference then by source text (optionsEquivalent),
     * patterns by type + string form.
     *
     * `name` is deliberately NOT compared. Every caller has already established
     * that the two configs resolve to the same BASE name (that is how the
     * candidate was found), and the collision-suffixed variants of that base —
     * "KefinTweaks#2", "#3" — carry the suffix in their own cfg.name. Comparing
     * it would make a duplicate of "#2" look different from "#2" and mint a
     * pointless "#3" whose entry chain runs the same files a second time.
     * @param {RefreshKitConfig} a
     * @param {RefreshKitConfig} b
     * @returns {boolean}
     */
    function configsEquivalent(a, b) {
        var scalar = ['versionUrl', 'versionJsonField', 'bootVersion', 'pollSeconds', 'idleSeconds',
            'entryTimeoutMs', 'mode', 'reloadBudget', 'getVersion', 'onUpdateAvailable'];
        for (var i = 0; i < scalar.length; i++) {
            if (!optionsEquivalent(a[scalar[i]], b[scalar[i]])) return false;
        }
        if (a.entryScripts.length !== b.entryScripts.length) return false;
        for (var j = 0; j < a.entryScripts.length; j++) {
            if (a.entryScripts[j] !== b.entryScripts[j]) return false;
        }
        if (a.assetPatterns.length !== b.assetPatterns.length) return false;
        for (var k = 0; k < a.assetPatterns.length; k++) {
            var pa = a.assetPatterns[k], pb = b.assetPatterns[k];
            if ((pa instanceof RegExp) !== (pb instanceof RegExp)) return false;
            if (String(pa) !== String(pb)) return false;
        }
        return true;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Shared page-level state
    // ─────────────────────────────────────────────────────────────────────────

    /** @type {Array<Object>} Instances, in registration (= tag document) order. */
    var registry = [];
    /** @type {Object<string, Object>} name → instance. */
    var byName = Object.create(null);
    /**
     * How many ANONYMOUS adoptions (neither `name` nor a derivable `versionUrl`)
     * have registered. The "instance-<N>" fallback is numbered off this counter
     * and NOT off registry.length, so an unrelated named adopter whose kit tag
     * happens to parse first cannot renumber somebody else's instance — which
     * would silently void the one thing that names it,
     * `JellyfinRefreshKitConfigs['instance-1']`.
     * @type {number}
     */
    var anonymousCount = 0;
    /** @type {number} Timestamp of the last user interaction (page-level). */
    var lastInteractionAt = Date.now();
    /** @type {number|null} setTimeout handle for the blocked-reload retry. */
    var retryTimer = null;
    /**
     * The two timers a discrete interaction arms (a task-0 hop so the host's own
     * handler runs first, then a wait for the remaining idle window). TRACKED
     * since 2.1.1: untracked, one pair leaked per discrete event — typing a
     * 40-character query queued 160 orphaned timers, each of which ran two
     * document-wide querySelectorAll safety probes and burned a tick of the
     * blocked-retry allowance, and none of which the hidden-tab suspend path
     * could cancel (breaking "a hidden tab holds zero timers"). Now the latest
     * interaction supersedes the previous one, exactly as the reference does
     * with its single `decision` timer.
     * @type {number|null}
     */
    var settleHopTimer = null;
    /** @type {number|null} See settleHopTimer. */
    var settleTimer = null;
    /** @type {number} Consecutive blocked retries, to stop an unbounded 1Hz loop. */
    var blockedRetries = 0;
    /**
     * When the current zero-progress 'media_element' block started, or null when
     * there is no such streak. Measured in WALL TIME rather than counted in
     * retry ticks on purpose: the 1Hz ladder is capped and can retire long
     * before a stuck element does, after which only polls and interactions
     * re-probe — a tick count would then stall short of the threshold forever
     * and the escape would never fire.
     * @type {number|null}
     */
    var mediaBlockSince = null;
    /** @type {string|null} Progress fingerprint the streak above is measured against. */
    var mediaBlockSignature = null;
    /** @type {boolean} One-shot latch for the parked-media starvation-escape log. */
    var warnedMediaStarvation = false;
    /** @type {string|null} Last recorded reason a reload was refused (diagnostics). */
    var lastBlockReason = null;
    /**
     * One-shot latch for the budget-refusal warning. The refusal now DEFERS
     * instead of discarding, so it can legitimately re-fire every budget window
     * until the window rolls; one warning per blocked episode is the signal, a
     * warning per retry is noise. Cleared the moment a reservation succeeds.
     * @type {boolean}
     */
    var warnedBudgetRefusal = false;
    /**
     * ONE NAVIGATION, ONE RESERVATION. `location.reload()` does not stop the
     * page: script keeps running until the new document commits, which is a
     * full network round trip away. Without this latch a second instance whose
     * version fetch resolves in that window arms, re-enters tryReload(), passes
     * every gate again and spends a SECOND slot of the shared reload budget on
     * the very same navigation — so a two-adopter page burns 2 of the default 3
     * per reload and defers the next genuine update for a whole budget window
     * for no reason. Mirrors client-refresh.js's `reloadCommitted`.
     *
     * IT IS NOT A ONE-WAY LATCH (2.2.0). Until 2.2.0 nothing ever cleared it
     * and nothing verified that the navigation happened, so a reload the host
     * blocked, ignored, or the user cancelled left the flag set, every
     * instance's updatePending false, and one budget slot spent — with layer 3
     * dead page-wide for the life of the document, reported by state() as
     * perfectly healthy. The survival watchdog below is the other half of the
     * mechanism this was ported from, and it re-opens the latch.
     * @type {boolean}
     */
    var reloadCommitted = false;
    /** @type {number|null} setTimeout handle for the reload-survival watchdog. */
    var reloadWatchdogTimer = null;
    /**
     * Instances disarmed by the reload attempt currently in flight, so the
     * watchdog can put them back exactly as they were if the document survives.
     * @type {Array<Object>}
     */
    var reloadDisarmed = [];
    /**
     * [storageKey, entry] records the in-flight reload attempt WROTE (and that
     * did not already exist), so a navigation that never lands does not leave
     * the flap guard believing this tab ran a version it never ran.
     * @type {Array<string[]>}
     */
    var reloadRecordsWritten = [];
    /** @type {boolean} One-shot latch for the survived-reload warning. */
    var warnedReloadSurvived = false;
    /** @type {number} How many reload attempts this document has outlived. */
    var reloadsSurvived = 0;
    /** @type {boolean} One-shot latch: warn about overlapping assetPatterns once. */
    var warnedOverlap = false;
    /** @type {boolean} True once the single createElement wrapper is installed. */
    var interceptorInstalled = false;
    /**
     * The NEWER manager this copy handed the page over to (REGISTRATION
     * CONTRACT clause 7), or null while this copy still owns the page.
     *
     * Once set, this copy is a permanent INERT DELEGATE: it holds no instances,
     * runs no timers, listens to nothing, and its `api` object — which stays
     * reachable forever, because window.JellyfinRefreshKit is installed
     * non-configurable by whichever copy got there first and can never be
     * re-pointed — forwards every contract call here. A copy arriving later
     * therefore lands on the CURRENT manager whichever object it happens to
     * hold.
     * @type {Object|null}
     */
    var delegate = null;
    /** @type {boolean} True once this copy has handed the page over. */
    var handedOff = false;
    /**
     * True when this copy's createElement wrapper has been neutralized by a
     * handoff. The wrapper cannot be UNINSTALLED — `document.createElement` may
     * have been captured by any amount of third-party code by then, and
     * restoring the native function would strip a newer manager's wrapper right
     * off the page — so it has a permanent inert-delegate mode instead: it
     * creates the element and touches nothing, while the per-element accessors
     * it installed BEFORE the handoff keep working by delegating their
     * versioning decision to the current manager (see versionUrlForPage).
     * @type {boolean}
     */
    var interceptorInert = false;
    /** @type {number} How many handoffs THIS copy has received (chain depth). */
    var handoffsReceived = 0;
    /** @type {string[]} Kit versions that managed this page before this copy. */
    var inheritedFrom = [];

    // ─────────────────────────────────────────────────────────────────────────
    // URL versioning helpers (shared, pure)
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Does the URL's QUERY STRING already carry a `v=` parameter? A path
     * segment named "v=" is not a parameter, and a fragment is not sent to the
     * server at all — never clobber a caller's own cache-busting;
     * double-versioning is a cache-miss storm.
     * @param {string} url
     * @returns {boolean}
     */
    function hasVersionParam(url) {
        var hashAt = url.indexOf('#');
        var base = hashAt === -1 ? url : url.slice(0, hashAt);
        var qAt = base.indexOf('?');
        var query = qAt === -1 ? '' : base.slice(qAt + 1);
        return !!query && /(^|&)v=/.test(query);
    }

    /**
     * Append `?v=<version>` (or `&v=`), keeping any fragment in place. Callers
     * are responsible for the "should we" checks (hasVersionParam etc.).
     * @param {string} url
     * @param {string} version
     * @returns {string}
     */
    function appendVersion(url, version) {
        var hashAt = url.indexOf('#');
        var base = hashAt === -1 ? url : url.slice(0, hashAt);
        var hash = hashAt === -1 ? '' : url.slice(hashAt);
        var sep = base.indexOf('?') === -1 ? '?' : '&';
        return base + sep + 'v=' + encodeURIComponent(version) + hash;
    }

    /**
     * The PAGE-LEVEL versioning decision, used by the single interceptor (and
     * the manager-level versionedUrl API):
     *   • URLs already carrying v= pass through untouched, always.
     *   • All registered instances' assetPatterns are consulted; the FIRST
     *     REGISTERED instance whose patterns match AND has a resolved version
     *     versions the URL with THAT instance's version. An instance that
     *     matches but has not resolved a version yet is skipped rather than
     *     being allowed to veto a sibling that HAS one — a URL going out
     *     unversioned is the one outcome nobody wants, and "matched first" is
     *     a tie-breaker between equals, not a licence to lose the version.
     *     With no resolved version anywhere, pass through untouched (same as
     *     1.x before the first fetch).
     *   • If MORE THAN ONE instance matches, the winner is the first matching
     *     instance with a version and we log ONE console.warn naming the
     *     overlap — the first time it bites, only.
     * @param {string} url
     * @returns {string}
     */
    function versionUrlForPage(url) {
        if (typeof url !== 'string' || !url) return url;
        if (hasVersionParam(url)) return url;
        // HANDED OFF: this copy owns no instances any more, but elements it
        // handed out BEFORE the handoff still carry its per-element accessors,
        // and those must not silently stop versioning. Ask the current manager
        // instead — its `versionedUrl(url)` without `force` IS the page-level
        // matcher, so this is the same decision, taken by the newest code.
        if (handedOff && delegate) {
            return safe(function () { return delegate.versionedUrl(url); }, url);
        }
        var matches = null;
        for (var i = 0; i < registry.length; i++) {
            if (registry[i].matchesAssetPattern(url)) {
                if (!matches) matches = [];
                matches.push(registry[i]);
            }
        }
        if (!matches) return url;

        var chosen = null, version = null;
        for (var m = 0; m < matches.length; m++) {
            var candidate = matches[m].getBaselineVersion();
            if (candidate) { chosen = matches[m]; version = candidate; break; }
        }

        if (matches.length > 1 && !warnedOverlap) {
            warnedOverlap = true;
            var winner = chosen || matches[0];
            safe(function () {
                console.warn(LOG, 'assetPatterns OVERLAP: "' + url + '" matches instances [' +
                    matches.map(function (mm) { return mm.name; }).join(', ') + ']. ' +
                    '"' + winner.name + '" wins (its version is applied). ' +
                    'Keep patterns disjoint per collection. (Warned once.)');
            });
        }

        if (!version) return url;
        return appendVersion(url, version);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // The single shared interceptor
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Install a per-ELEMENT accessor for `prop` ('src' or 'href') on a freshly
     * created element so that assignment rewrites the URL before the browser
     * ever sees it.
     *
     * Why per-element and not on Element.prototype: patching the prototype
     * changes behaviour for every script/link on the page including Jellyfin's
     * own and any other plugin's, is near-impossible to un-install, and breaks
     * `instanceof`-style feature probes. Per-element means only elements this
     * kit's own createElement wrapper handed out are affected, and any code that
     * builds its element some other way is untouched (a fail-safe default).
     *
     * We delegate storage to the element's real prototype accessor, so
     * getAttribute/setAttribute, resolution to an absolute URL, and load
     * behaviour all stay exactly native.
     *
     * @param {Element} el
     * @param {'src'|'href'} prop
     */
    /**
     * Normalize a value assigned to `src`/`href` into the string the browser
     * would resolve, WITHOUT changing what any other value is.
     *
     * Only two wrappers qualify: a `URL` instance and a boxed `String`. Both are
     * spec-guaranteed to stringify to exactly the URL the native setter would
     * use, so rewriting them is behaviour-preserving. Everything else is
     * returned untouched on purpose:
     *
     *   • A `TrustedScriptURL` (or any Trusted Types object) MUST reach the
     *     native setter as the object it is. Under
     *     `require-trusted-types-for 'script'` a string re-assignment throws a
     *     TypeError at `native.set`, which is outside this function's safe()
     *     wrapper — i.e. blanket coercion would break the page it was meant to
     *     help.
     *   • An arbitrary object's `toString` may be user code with side effects.
     *     Calling it here would run it twice (once for us, once for the
     *     browser), which is not a rewrite, it is a behaviour change.
     *
     * @param {*} value
     * @returns {*} The string form for URL/String wrappers, otherwise `value`.
     */
    function normalizeUrlValue(value) {
        if (typeof value === 'string') return value;
        if (typeof URL === 'function' && value instanceof URL) return String(value);
        if (value instanceof String) return String(value);
        return value;
    }

    function interceptUrlProperty(el, prop) {
        var proto = Object.getPrototypeOf(el);
        var native = Object.getOwnPropertyDescriptor(proto, prop);
        // Some very old / exotic engines expose src as an own value property.
        // Without a native accessor pair there is nothing to delegate to.
        if (!native || typeof native.get !== 'function' || typeof native.set !== 'function') return;

        Object.defineProperty(el, prop, {
            configurable: true,
            enumerable: true,
            get: function () { return native.get.call(this); },
            set: function (value) {
                var rewritten = safe(function () {
                    // `s.src = new URL('scripts/a.js', base)` is an ordinary
                    // modern idiom, and the browser ToStrings it on the way in
                    // — so a raw `typeof value === 'string'` gate let the whole
                    // of layer 2 silently switch itself off for any collection
                    // that builds URLs that way. Normalize the two wrappers the
                    // spec guarantees stringify to the URL the browser will use
                    // (URL and boxed String), and NOTHING else: a
                    // TrustedScriptURL must reach the native setter as the
                    // trusted object it is (coercing it to a string under a
                    // `require-trusted-types-for 'script'` CSP throws at
                    // native.set, outside this safe() wrapper), and an arbitrary
                    // object's toString may have side effects that are not ours
                    // to trigger twice.
                    var v = normalizeUrlValue(value);
                    return typeof v === 'string' ? versionUrlForPage(v) : v;
                }, value);
                native.set.call(this, rewritten);
            }
        });
    }

    /**
     * Patch `setAttribute` on the element too. Plenty of code writes
     * `el.setAttribute('src', url)` instead of `el.src = url`, and that path
     * bypasses the property accessor entirely.
     * @param {Element} el
     * @param {'src'|'href'} prop
     */
    function interceptSetAttribute(el, prop) {
        var nativeSetAttribute = el.setAttribute;
        Object.defineProperty(el, 'setAttribute', {
            configurable: true,
            enumerable: false,
            writable: true,
            value: function (name, value) {
                if (typeof name === 'string' && name.toLowerCase() === prop) {
                    // Same normalization as the property accessor — see
                    // normalizeUrlValue: `setAttribute('src', new URL(...))`
                    // is as common as the property form and must not be a
                    // silent hole in layer 2.
                    var v = safe(function () { return normalizeUrlValue(value); }, value);
                    if (typeof v === 'string') {
                        value = safe(function () { return versionUrlForPage(v); }, v);
                    }
                }
                return nativeSetAttribute.call(this, name, value);
            }
        });
    }

    /**
     * Wrap document.createElement so that <script> and <link> elements come back
     * with the URL interceptors already installed. Installed ONCE per page, by
     * the manager — later kit copies register instances instead of re-wrapping
     * (that is the whole point of the registration contract).
     *
     * This is the whole trick, and it is why the kit works on collections that
     * were never written with cache-busting in mind. KefinTweaks' loader, for
     * example, does exactly this in injector.js:
     *
     *     const script = document.createElement('script');       // line 552
     *     script.src = `${scriptRoot}${filename}${urlSuffix}`;   // line 553
     *     document.head.appendChild(script);                     // line 566
     *
     * ...with `urlSuffix = ''` hardcoded (injector.js line 309 — the cache-buster
     * is commented out on line 310). The assignment on line 553 goes through our
     * accessor and comes out versioned, with zero changes to KefinTweaks.
     * Its loadCSS() does the same for <link href> (lines 516–519).
     *
     * Note we intentionally do NOT patch createElementNS, innerHTML, or
     * document.write. Those are rarer, much more invasive to intercept, and the
     * cost of missing them is only that a given asset stays unversioned.
     *
     * The VALUE assigned is handled by normalizeUrlValue: strings and the two
     * wrappers that provably stringify to the same URL (URL, boxed String) are
     * versioned; a TrustedScriptURL or any other object is passed through
     * untouched, which is a deliberate, documented limit.
     */
    function installCreateElementHook() {
        var nativeCreateElement = document.createElement;
        if (typeof nativeCreateElement !== 'function') return;
        // Belt and braces: if an ACTIVE wrapper of ours is already present
        // (manager global failed to install but the hook stuck), never stack a
        // second one — two live wrappers would install two sets of per-element
        // accessors and version the same URL twice.
        //
        // An INERT one is different (2.3.0). A manager that handed the page
        // over flips its wrapper to pass-through and says so here, and the new
        // manager stacks ITS wrapper on top: `document.createElement` is then
        // new-wrapper → inert-old-wrapper → native, exactly one of which does
        // anything. That is what makes the NEWEST copy's interception code —
        // its normalizeUrlValue, its accessors, its setAttribute patch — govern
        // the page, which is the whole point of the newest-wins rule. The
        // wrapper chain grows by one frame per handoff, i.e. by at most one per
        // kit copy in the document.
        if (nativeCreateElement.__jellyfinRefreshKitWrapper &&
            safe(function () { return nativeCreateElement.__jellyfinRefreshKitInert === true; }, false) !== true) {
            interceptorInstalled = true;
            return;
        }

        var wrapper = function (tagName) {
            var el = nativeCreateElement.apply(this, arguments);
            // Inert-delegate mode: this copy handed the page over, so it must
            // not touch the element. The manager stacked above us has already
            // installed (or is about to install) its own interceptors on it.
            if (interceptorInert) return el;
            safe(function () {
                if (typeof tagName !== 'string') return;
                var tag = tagName.toLowerCase();
                if (tag === 'script') {
                    interceptUrlProperty(el, 'src');
                    interceptSetAttribute(el, 'src');
                } else if (tag === 'link') {
                    // Only stylesheets are worth versioning; but `rel` is often
                    // assigned AFTER href, so we cannot filter on it here. The
                    // assetPatterns check is the real filter, and it keeps this
                    // from touching favicons/preconnects that don't match.
                    interceptUrlProperty(el, 'href');
                    interceptSetAttribute(el, 'href');
                }
            });
            return el;
        };
        safe(function () {
            Object.defineProperty(wrapper, '__jellyfinRefreshKitWrapper', { value: true });
            // Read by a NEWER copy's installCreateElementHook to decide whether
            // it may stack. A getter, not a value: inertness is a property of
            // this copy's live state, and the wrapper is frozen-by-convention
            // API surface between kit versions.
            Object.defineProperty(wrapper, '__jellyfinRefreshKitInert', {
                get: function () { return interceptorInert; }
            });
        });
        document.createElement = wrapper;
        interceptorInstalled = true;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Shared safety gate
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Does this media element represent a real SESSION we must not destroy?
     *
     * Playing always counts. A paused element counts only with evidence that a
     * session actually happened: a playback position, a played range, or an
     * in-progress seek. Before 2.1.1 the test was `readyState > 0`, i.e. "the
     * browser has loaded some metadata" — which is true of any decorative
     * <video> a plugin parks in the DOM with a src and preload set (JMSFusion
     * does exactly this). Such an element never plays, never ends, and never
     * changes, so it returned 'media_element' on every evaluation for the life
     * of the page and permanently starved the auto-reload of a tab that had
     * nothing playing at all. There is nothing to protect at currentTime 0 with
     * an empty played list: the gate was blocking on the mere existence of a
     * media element, not on a session.
     * @param {HTMLMediaElement} el
     * @returns {boolean}
     */
    /**
     * Is this element AMBIENT DECORATION rather than something anyone is
     * watching? Ambient video does not block the auto-reload.
     *
     * THE CASE THIS EXISTS FOR. Media Bar (and its forks, and several skins)
     * puts a full-bleed backdrop video behind the Home screen —
     * `.video-container > video.preview-video`: autoplay, muted, looping, no
     * controls, never started by the user. To hasLiveMedia() that is
     * indistinguishable from a film: it is not paused, so it is "playing", so
     * the gate returns 'media_element'. And because it genuinely plays, the
     * parked-media starvation escape can never fire either — every probe sees
     * fresh playback progress and restarts the ~10-minute clock. A live
     * revalidation measured exactly that: blockedRetries climbing 1 → 176 over
     * 160s on #/home with Media Bar installed, i.e. layer 3 switched off for as
     * long as the user sits on Home, which is where users sit.
     *
     * THE HEURISTIC, deliberately conservative: MUTED (or volume 0) AND LOOPING
     * AND WITHOUT CONTROLS. All three together describe decoration and nothing
     * else in practice — a person actually watching something either hears it
     * or has the controls to scrub it, and content does not loop. Any ONE of
     * them being false leaves the element blocking exactly as before: an
     * unmuted video blocks, a video with controls blocks, a non-looping video
     * blocks. Jellyfin's own player has controls and is not looping, so the
     * player is untouched by this; a trailer/theme-song preview with sound
     * still blocks; and an ambient video that goes FULLSCREEN is caught earlier
     * by the 'fullscreen_media' gate, which runs before the media probe.
     *
     * THE TRADEOFF, stated plainly: a user who deliberately sits watching a
     * muted, looping, controls-less video can be reloaded under. That is
     * accepted — the description IS ambient decoration, the reload is
     * safe-gated on everything else (idle, dialogs, editors, route,
     * fullscreen, visibility), and the alternative is that a single decorative
     * element on the most-visited page in Jellyfin permanently disables the
     * kit's third layer for every adopter on the page.
     *
     * Video only: an <audio> element is never "ambient backdrop", and muting a
     * podcast does not make it decoration.
     *
     * @param {HTMLMediaElement} el
     * @returns {boolean}
     */
    function isAmbientVideo(el) {
        try {
            if (String(el.tagName || '').toUpperCase() !== 'VIDEO') return false;
            var silent = el.muted === true || el.volume === 0;
            if (!silent) return false;
            if (el.loop !== true) return false;
            if (el.controls === true) return false;
            return true;
        } catch (_) {
            // Unreadable element: NOT ambient. Every unknown is treated as a
            // session, which is the same direction hasLiveMedia() fails in.
            return false;
        }
    }

    function hasLiveMedia(el) {
        try {
            // Ambient backdrop decoration is not a session anybody is having.
            if (isAmbientVideo(el)) return false;
            var src = el.currentSrc || el.getAttribute('src') || '';
            if (!src) {
                var sourceEl = el.querySelector('source[src]');
                src = sourceEl ? (sourceEl.getAttribute('src') || '') : '';
            }
            if (!src) return false;
            if (el.ended) return false;
            if (!el.paused) return true;                                  // playing
            if (typeof el.currentTime === 'number' && el.currentTime > 0) return true;
            if (el.played && el.played.length > 0) return true;
            if (el.seeking) return true;
            return false;                                                 // parked, never played
        } catch (_) {
            // Unreadable element: assume it is live rather than reload over it.
            return true;
        }
    }

    /**
     * @param {number} idleMs The idle window that must have elapsed (already
     *   floored at MIN_SETTLE_MS by the caller).
     * @param {boolean} [skipMediaGate] Ignore the <video>/<audio> probe. Set
     *   ONLY by the starvation escape in tryReload(), after a media element has
     *   held the gate for the whole retry ladder without a single frame of
     *   progress. Every other gate still applies.
     * @returns {string|null} A stable reason key why reloading now is unsafe, or
     *   null when a reload is safe. Order is cheapest-and-most-decisive first.
     */
    function blockReasonFor(idleMs, skipMediaGate) {
        try {
            if (document.visibilityState === 'hidden') return 'hidden';

            // Jellyfin's own video route. Even before a <video> exists, being on
            // #/video means a session is starting.
            var hash = String(location.hash || '');
            if (hash.indexOf('#/video') === 0 || hash.indexOf('#!/video') === 0) return 'playback_route';

            if (document.fullscreenElement || document.pictureInPictureElement) return 'fullscreen_media';

            // Open modal/dialog: the user is mid-task and probably mid-write.
            var dialogs = document.querySelectorAll(
                '.dialog.opened, .actionSheet.opened, [role="dialog"], [aria-modal="true"]'
            );
            for (var i = 0; i < dialogs.length; i++) {
                // A closed-but-retained dialog stays in the DOM inside an
                // aria-hidden/hidden subtree; only visible ones block.
                if (!dialogs[i].closest('[aria-hidden="true"], [hidden]')) return 'dialog';
            }

            if (!skipMediaGate) {
                var media = document.querySelectorAll('video, audio');
                for (var j = 0; j < media.length; j++) {
                    if (hasLiveMedia(/** @type {HTMLMediaElement} */ (media[j]))) return 'media_element';
                }
            }

            var active = document.activeElement;
            if (active && (active.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName || ''))) {
                return 'active_editor';
            }

            if ((Date.now() - lastInteractionAt) < idleMs) return 'not_idle';
            return null;
        } catch (err) {
            // A probe that throws leaves safety unknown — refuse the reload.
            safe(function () { console.debug(LOG, 'safety probe failed:', err); });
            return 'probe_failed';
        }
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Shared reload budget
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * The page-level reload budget: the MINIMUM reloadBudget among all
     * registered instances. A reload nukes every instance's tab, so the most
     * conservative adopter sets the page's ceiling.
     * @returns {number}
     */
    function effectiveReloadBudget() {
        var budget = DEFAULTS.reloadBudget;
        for (var i = 0; i < registry.length; i++) {
            if (registry[i].cfg.reloadBudget < budget || i === 0) budget = registry[i].cfg.reloadBudget;
        }
        return budget;
    }

    /**
     * Read the stamp list from one Storage.
     * @param {Storage} storage
     * @returns {number[]|null} Stamps, [] when absent, null when unreadable/corrupt.
     */
    function readBudget(storage) {
        try {
            var raw = storage.getItem(BUDGET_KEY);
            if (raw === null || raw === undefined) return [];
            var parsed = JSON.parse(raw);
            if (!Array.isArray(parsed)) return null;
            for (var i = 0; i < parsed.length; i++) {
                if (typeof parsed[i] !== 'number' || !isFinite(parsed[i])) return null;
            }
            return parsed;
        } catch (_) {
            return null;
        }
    }

    /**
     * Write the budget and PROVE it stuck. Some embedded WebViews (and Safari in
     * certain private modes) accept setItem without throwing and then silently
     * drop the value; only a read-after-write match proves this reload was
     * actually counted. An uncounted reload is an infinite reload loop.
     * @param {Storage} storage
     * @param {string} serialized
     * @param {number[]} expected
     * @returns {boolean}
     */
    function writeBudget(storage, serialized, expected) {
        try {
            storage.setItem(BUDGET_KEY, serialized);
        } catch (_) {
            return false;
        }
        var verified = readBudget(storage);
        if (!verified || verified.length !== expected.length) return false;
        for (var i = 0; i < expected.length; i++) {
            if (verified[i] !== expected[i]) return false;
        }
        return true;
    }

    /**
     * @param {'sessionStorage'|'localStorage'} name
     * @returns {Storage|null}
     */
    function safeStorage(name) {
        try {
            var s = window[name];
            // Merely touching the object is what throws in locked-down browsers.
            return (s && typeof s.getItem === 'function' && typeof s.setItem === 'function') ? s : null;
        } catch (_) {
            return null;
        }
    }

    /**
     * Reserve one reload against the rolling budget.
     *
     * FAILS CLOSED: if no storage can be read, or no write can be verified, the
     * reload does NOT happen. A refresh kit that cannot count its own reloads is
     * exactly the thing that reload-loops a user's browser.
     *
     * sessionStorage and localStorage are both used and merged: sessionStorage
     * is per-tab (survives reloads, catches a single tab looping), localStorage
     * is shared (catches every tab reloading at once). Stamps are de-duplicated
     * so mirroring the same reservation into both cannot consume the budget twice.
     *
     * @returns {boolean} True when a reload may proceed.
     */
    function reserveReload() {
        var budget = effectiveReloadBudget();
        var adapters = [];
        var ss = safeStorage('sessionStorage');
        var ls = safeStorage('localStorage');
        if (ss) adapters.push(ss);
        if (ls) adapters.push(ls);

        var readable = [];
        for (var a = 0; a < adapters.length; a++) {
            var history = readBudget(adapters[a]);
            if (history !== null) readable.push({ storage: adapters[a], history: history });
        }
        if (readable.length === 0) return false;

        var now = Date.now();
        var seen = Object.create(null);
        var combined = [];
        for (var r = 0; r < readable.length; r++) {
            var list = readable[r].history;
            for (var i = 0; i < list.length; i++) {
                var stamp = list[i];
                // Keep only live stamps inside the rolling window; drop future
                // stamps entirely (a clock change must not grant free reloads).
                if (stamp < now - BUDGET_WINDOW_MS || stamp > now) continue;
                if (!seen[stamp]) { seen[stamp] = true; combined.push(stamp); }
            }
        }
        combined.sort(function (x, y) { return x - y; });
        combined = combined.slice(-budget);

        if (combined.length >= budget) return false;

        var next = combined.concat([now]);
        var serialized = JSON.stringify(next);
        var persisted = false;
        for (var w = 0; w < readable.length; w++) {
            if (writeBudget(readable[w].storage, serialized, next)) persisted = true;
        }
        return persisted;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Per-tab memory: version flips and one-shot recoveries
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Read a string-array from sessionStorage. sessionStorage — not local — on
     * purpose: both of these facts are properties of THIS TAB's history ("this
     * tab already reloaded for that transition", "this tab already spent its
     * one recovery"), they must survive the reloads they are policing, and they
     * must not leak into a tab the user opened fresh.
     * @param {string} key
     * @returns {string[]} Always an array; [] when absent, unreadable or corrupt.
     */
    function readTabList(key) {
        return safe(function () {
            var ss = safeStorage('sessionStorage');
            if (!ss) return [];
            var raw = ss.getItem(key);
            if (!raw) return [];
            var parsed = JSON.parse(raw);
            if (!Array.isArray(parsed)) return [];
            return parsed.filter(function (s) { return typeof s === 'string'; });
        }, []) || [];
    }

    /**
     * Append one entry to a per-tab list, de-duplicated and bounded.
     * Best-effort: a tab with no writable sessionStorage simply loses the
     * memory, which degrades to the pre-2.1 behaviour rather than breaking.
     * @param {string} key
     * @param {string} entry
     * @param {number} cap
     * @returns {boolean} True when the entry was NOT already in the list, i.e.
     *   this call is what added it. The reload-survival watchdog uses that to
     *   retract exactly the records a navigation that never happened wrote, and
     *   nothing an earlier, real navigation wrote.
     */
    function pushTabList(key, entry, cap) {
        return safe(function () {
            var ss = safeStorage('sessionStorage');
            var current = readTabList(key);
            var isNew = current.indexOf(entry) === -1;
            if (!ss) return false;
            var list = current.filter(function (s) { return s !== entry; });
            list.push(entry);
            if (list.length > cap) list = list.slice(list.length - cap);
            ss.setItem(key, JSON.stringify(list));
            return isNew;
        }, false) === true;
    }

    /**
     * Remove one entry from a per-tab list. Best-effort, like pushTabList.
     * @param {string} key
     * @param {string} entry
     */
    function removeFromTabList(key, entry) {
        safe(function () {
            var ss = safeStorage('sessionStorage');
            if (!ss) return;
            var list = readTabList(key).filter(function (s) { return s !== entry; });
            ss.setItem(key, JSON.stringify(list));
        });
    }

    /**
     * @param {string} name Instance name.
     * @param {string} from Version reloaded away FROM.
     * @param {string} to Version reloaded TO.
     * @returns {string} The canonical flip-record string.
     */
    function flipRecord(name, from, to) {
        return name + '|' + from + '>' + to;
    }

    /**
     * @param {string} name Instance name.
     * @param {string} version Version this tab reloaded away FROM.
     * @returns {string} The canonical left-version record string.
     */
    function leftRecord(name, version) {
        return name + '|' + version;
    }

    /**
     * Remember that this tab reloaded `name` from `from` to `to`: the directed
     * PAIR (kept for the log message, so a refusal can say what the tab
     * actually did) and the fact that `from` has now been LEFT (the guard).
     * @param {string} name
     * @param {string} from
     * @param {string} to
     * @returns {string[][]} The [key, entry] records this call newly wrote, for
     *   the reload-survival watchdog to retract if the navigation never lands.
     */
    function rememberFlip(name, from, to) {
        // `from === to` is not a transition. It can only be reached from a
        // reload that was armed for an update the server later withdrew, and
        // recording "X>X" would burn one of the 24 per-tab slots on a record
        // that can never match anything.
        if (!from || !to || from === to) return [];
        var written = [];
        if (pushTabList(FLIP_KEY, flipRecord(name, from, to), MAX_FLIP_RECORDS)) {
            written.push([FLIP_KEY, flipRecord(name, from, to)]);
        }
        if (pushTabList(LEFT_KEY, leftRecord(name, from), MAX_FLIP_RECORDS)) {
            written.push([LEFT_KEY, leftRecord(name, from)]);
        }
        return written;
    }

    /**
     * Has this tab already reloaded AWAY FROM the version it is now being asked
     * to reload TO? Then the endpoint is oscillating, not releasing — two nodes
     * behind a round-robin proxy, three replicas with per-container DLL mtimes,
     * a rolling deploy. Each such reload sits in its own budget window, so the
     * reload budget can never catch it; this is the only thing that can, and
     * unlike the pre-2.2 pair test it terminates cycles of any length.
     * @param {string} name
     * @param {string} to Candidate version.
     * @returns {boolean}
     */
    function hasLeftVersion(name, to) {
        if (!to) return false;
        return readTabList(LEFT_KEY).indexOf(leftRecord(name, to)) !== -1;
    }

    /**
     * What did this tab reload `version` INTO, the last time it left it? Used
     * only to make the flap warning concrete ("already reloaded A → B").
     * @param {string} name
     * @param {string} version
     * @returns {string} The recorded destination, or '' when none is remembered.
     */
    function flipDestinationFrom(name, version) {
        var prefix = name + '|' + version + '>';
        var list = readTabList(FLIP_KEY);
        for (var i = list.length - 1; i >= 0; i--) {
            if (list[i].indexOf(prefix) === 0) return list[i].slice(prefix.length);
        }
        return '';
    }

    /**
     * Claim a named one-shot recovery for this tab, returning false when it was
     * already spent. Recoveries reload the page to repair a boot that went
     * wrong; without a per-tab latch a permanently broken endpoint would repair
     * itself into a reload loop.
     * @param {string} marker
     * @returns {boolean} True when the caller may proceed.
     */
    function claimRecovery(marker) {
        if (readTabList(RECOVERY_KEY).indexOf(marker) !== -1) return false;
        pushTabList(RECOVERY_KEY, marker, MAX_FLIP_RECORDS);
        return true;
    }

    /**
     * @param {string} marker
     * @returns {boolean} True when this recovery was already spent in this tab.
     */
    function recoverySpent(marker) {
        return readTabList(RECOVERY_KEY).indexOf(marker) !== -1;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Shared reload engine
    // ─────────────────────────────────────────────────────────────────────────

    /** @returns {Array<Object>} Auto-mode instances with a pending update. */
    function pendingInstances() {
        var out = [];
        for (var i = 0; i < registry.length; i++) {
            if (registry[i].updatePending && registry[i].cfg.mode === 'auto') out.push(registry[i]);
        }
        return out;
    }

    /**
     * Idle window used for a page-level reload decision: the STRICTEST (max)
     * idleSeconds among the instances currently wanting a reload — one plugin
     * asking for a calm 30s of idle is not overruled by another asking for 2s.
     * With no pending instances there is no window to impose, so this is the
     * floor; callers that want a hypothetical use their OWN idleSeconds.
     * @param {Array<Object>} [pending]
     * @returns {number} Milliseconds, floored at MIN_SETTLE_MS.
     */
    function effectiveIdleWindowMs(pending) {
        var strictest = strictestIdleInstance(pending);
        return Math.max((strictest ? strictest.cfg.idleSeconds : 0) * 1000, MIN_SETTLE_MS);
    }

    /**
     * Which instance IMPOSES the effective idle window — i.e. the one with the
     * largest idleSeconds among those currently wanting a reload. Exposed
     * through state() so a snapshot showing a lax instance held back by a
     * strict sibling names the sibling instead of looking broken.
     *
     * PENDING ONLY (2.2.0). It used to fall back to the whole registry when
     * nothing was pending, so the aggregate snapshot — whose normal state IS
     * "nothing pending" — named whichever instance had the largest idleSeconds
     * as imposing a window, including a `mode: 'off'` instance that can never
     * enter the pending set and can never impose anything. Support then debugs
     * a constraint the engine would never apply, and blames an unrelated
     * third-party plugin for it. Same treatment `blockReason` got in 2.1.0:
     * answer the question the field is named for, or answer null.
     * @param {Array<Object>} [pending]
     * @returns {Object|null}
     */
    function strictestIdleInstance(pending) {
        var list = pending || [];
        var chosen = null;
        for (var i = 0; i < list.length; i++) {
            if (!chosen || list[i].cfg.idleSeconds > chosen.cfg.idleSeconds) chosen = list[i];
        }
        return chosen;
    }

    /**
     * Cancel the blocked-reload retry timer AND the pair a discrete interaction
     * armed. All three exist for one purpose — "re-evaluate the reload soon" —
     * so they are cancelled together; leaving the interaction pair behind is
     * what let a hidden or already-satisfied tab keep running safety probes.
     */
    function clearRetry() {
        if (retryTimer !== null) { clearTimeout(retryTimer); retryTimer = null; }
        clearInteractionTimers();
    }

    /** Cancel the task-0 hop and the idle-window wait armed by an interaction. */
    function clearInteractionTimers() {
        if (settleHopTimer !== null) { clearTimeout(settleHopTimer); settleHopTimer = null; }
        if (settleTimer !== null) { clearTimeout(settleTimer); settleTimer = null; }
    }

    /**
     * Stand the shared engine down when nothing wants a reload any more (the
     * last pending instance had its update withdrawn by the server, or
     * reloaded). Without this a retracted update left the 1Hz ladder, the
     * interaction timers and a stale lastBlockReason running against nobody.
     */
    function releaseEngineIfIdle() {
        if (pendingInstances().length > 0) return;
        clearRetry();
        blockedRetries = 0;
        lastBlockReason = null;
        warnedBudgetRefusal = false;
        resetMediaBlockStreak();
    }

    /**
     * A fingerprint of every media element's playback position. Two identical
     * consecutive readings mean nothing about the page's media moved between
     * them — no playback, no seek, no source change.
     * @returns {string|null} null when the DOM could not be probed.
     */
    function mediaProgressSignature() {
        try {
            var media = document.querySelectorAll('video, audio');
            var out = '';
            for (var i = 0; i < media.length; i++) {
                var el = /** @type {HTMLMediaElement} */ (media[i]);
                // AMBIENT VIDEO IS NOT PROGRESS. An exempted backdrop loops
                // forever, so including it here would restart the starvation
                // clock on every probe and starve the escape that exists for a
                // genuinely stuck element sharing the page with it. It cannot
                // be what is holding the gate — it does not block — so it has
                // no business in the fingerprint of what does.
                if (isAmbientVideo(el)) continue;
                out += (el.currentSrc || el.getAttribute('src') || '') + '@' +
                    (el.paused ? 'p' : 'r') + ':' +
                    (typeof el.currentTime === 'number' ? el.currentTime.toFixed(1) : '?') + '|';
            }
            return out;
        } catch (_) {
            return null;
        }
    }

    /** Forget the parked-media streak (progress happened, or the block cleared). */
    function resetMediaBlockStreak() {
        mediaBlockSince = null;
        mediaBlockSignature = null;
    }

    /** @returns {number} How long the current zero-progress media block has lasted. */
    function mediaBlockedForMs() {
        return mediaBlockSince === null ? 0 : Date.now() - mediaBlockSince;
    }

    /**
     * Note one 'media_element' refusal, restarting the clock whenever anything
     * about the page's media actually moved.
     * @returns {boolean} True once the block has outlasted the whole retry
     *   ladder with zero progress — the parked-media starvation escape.
     */
    function noteMediaBlock() {
        var sig = mediaProgressSignature();
        if (sig === null) {
            // The DOM could not be probed; do not accumulate against a reading
            // we do not have.
            resetMediaBlockStreak();
            return false;
        }
        if (sig !== mediaBlockSignature) {
            mediaBlockSignature = sig;
            mediaBlockSince = Date.now();
            return false;
        }
        return mediaBlockedForMs() >= MEDIA_STARVATION_MS;
    }

    /**
     * Schedule one more safety re-evaluation. Bounded so a tab left on a video
     * forever does not tick at 1Hz for eternity — after the cap the 1Hz loop
     * stops and the slower re-entry points (an interaction, a tab refocus, or
     * the next successful poll) re-test the gate instead.
     *
     * @param {number} [delayMs] Defaults to the 1Hz safety-gate cadence. The
     *   budget-refusal path passes BUDGET_WINDOW_MS: nothing about that refusal
     *   can change before the rolling window rolls, so re-testing every second
     *   would be a thousand pointless storage reads.
     */
    function scheduleRetry(delayMs) {
        clearRetry();
        if (handedOff) return;
        if (blockedRetries >= MAX_BLOCKED_RETRIES) return;
        blockedRetries++;
        retryTimer = setTimeout(function () {
            retryTimer = null;
            safe(tryReload);
        }, typeof delayMs === 'number' && isFinite(delayMs) ? delayMs : RETRY_MS);
    }

    /**
     * Attempt the shared reload if every gate passes; otherwise arm a retry.
     * Called on: any instance's update detection, every interaction settle, and
     * each retry tick. ONE reload serves every pending instance at once —
     * after it, each instance's assets re-resolve at their own (new or
     * unchanged) versions.
     */
    function tryReload() {
        // Handed off: the reload engine of this copy is retired. The manager
        // that took over holds the transferred latch, budget view and pending
        // set, and is the only engine allowed to navigate this document —
        // two engines calling location.reload() is exactly the double-fire the
        // one-navigation-one-slot latch exists to prevent.
        if (handedOff) return;

        // The navigation is already committed; this document is on its way out.
        // Anything that arms between here and unload is served by the reload
        // that is already in flight, so it must not reserve a second slot of
        // the shared budget (nor log a second "reloading to pick up" line).
        if (reloadCommitted) {
            // It IS going to be picked up, though — so record its transition
            // with the same evidence value the committing instances got, or the
            // flap guard would have no memory of a reload that did happen.
            var late = pendingInstances();
            for (var l = 0; l < late.length; l++) {
                safe(function (p) {
                    return function () {
                        console.debug(LOG, 'reload already committed; ' + p.name + ' ' +
                            p.getBaselineVersion() + ' → ' + p.getLatestVersion() +
                            ' rides the navigation already in flight.');
                        noteReloadTransition(p);
                    };
                }(late[l]));
                // Ride-along instances join the disarmed set, so a navigation
                // that never lands rearms them too.
                reloadDisarmed.push(late[l]);
                late[l].updatePending = false;
            }
            return;
        }

        var pending = pendingInstances();
        if (pending.length === 0) return;

        var idleWindow = effectiveIdleWindowMs(pending);
        var reason = blockReasonFor(idleWindow);

        // PARKED-MEDIA STARVATION ESCAPE. hasLiveMedia() already refuses to
        // block on an element that never played, but a media element can also
        // be stuck: paused at a position and abandoned, or "playing" and frozen
        // on a stall that never recovers. Either way, once the reason has been
        // 'media_element' for the whole retry ladder (~10 min at 1Hz) with not
        // one frame of movement anywhere on the page, this is not a session
        // being protected — it is layer 3 being switched off permanently by a
        // decoration. Re-test with the media probe suppressed; EVERY other gate
        // (dialog, editor, route, fullscreen, visibility, idle) still applies,
        // so this widens exactly one condition and nothing else. Note that
        // 'playback_route' is evaluated BEFORE the media probe, so a tab parked
        // on Jellyfin's own #/video route never reaches this branch — the
        // escape can only ever apply to media OUTSIDE the player route.
        if (reason === 'media_element') {
            if (noteMediaBlock()) {
                var escaped = blockReasonFor(idleWindow, true);
                if (!warnedMediaStarvation) {
                    warnedMediaStarvation = true;
                    safe(function () {
                        console.warn(LOG, 'a <video>/<audio> element has held the reload gate for ' +
                            Math.round(mediaBlockedForMs() / 1000) + 's with no playback progress at ' +
                            'all — treating it as parked scenery rather than a live session and ' +
                            'letting the pending reload proceed once the other gates clear' +
                            (escaped ? ' (still blocked by: ' + escaped + ')' : '') +
                            '. (Warned once.)');
                    });
                }
                reason = escaped;
            }
        } else {
            resetMediaBlockStreak();
        }

        if (reason) {
            if (reason !== lastBlockReason) {
                lastBlockReason = reason;
                safe(function () { console.debug(LOG, 'reload deferred:', reason); });
            }
            // 'hidden' needs no timer at all — visibilitychange will wake us and
            // burning a 1Hz timer in a background tab is exactly what we promise
            // not to do.
            if (reason !== 'hidden') scheduleRetry();
            return;
        }
        lastBlockReason = null;
        clearRetry();

        if (!reserveReload()) {
            // DEFERRED, NOT DISCARDED. The refusal is a property of a 60-second
            // rolling window (or of storage we could not verify), not of the
            // update — the tab that lost the race is still running stale code
            // and still wants the reload. Clearing updatePending here is what
            // used to strand it forever: onVersion's watermark then made every
            // later poll, and checkNow(), a no-op for that same version.
            //
            // So: keep the intent, warn once per blocked episode, and re-test
            // when the window has rolled. This mirrors the reference this was
            // ported from (client-refresh.js: scheduleRetry(RELOAD_BUDGET_WINDOW_MS)).
            if (!warnedBudgetRefusal) {
                warnedBudgetRefusal = true;
                safe(function () {
                    console.warn(LOG, 'reload budget exhausted (' + effectiveReloadBudget() + ' per ' +
                        (BUDGET_WINDOW_MS / 1000) + 's) or unverifiable — deferring the reload for ' +
                        (BUDGET_WINDOW_MS / 1000) + 's. The pending update is kept; this is the ' +
                        'loop-protection fail-closed path, not an abandonment. (Warned once per episode.)');
                });
            }
            lastBlockReason = 'reload_budget';
            scheduleRetry(BUDGET_WINDOW_MS);
            return;
        }
        warnedBudgetRefusal = false;

        safe(function () {
            console.log(LOG, 'reloading to pick up: ' + pending.map(function (p) {
                return p.name + ' ' + p.getBaselineVersion() + ' → ' + p.getLatestVersion();
            }).join(', '));
        });
        // Record the transition BEFORE the page goes away: on the other side of
        // this reload it is the only evidence that a version source flapping
        // back to where we came from is a flap and not a release.
        for (var j = 0; j < pending.length; j++) {
            safe(function (p) {
                return function () { noteReloadTransition(p); };
            }(pending[j]));
        }
        // Commit BEFORE the reload call and disarm EVERY registered instance
        // that wanted the reload, not just the ones pending at this instant:
        // the reload that is now in flight serves all of them, and an instance
        // that arms during the unload window would otherwise re-enter
        // tryReload() (the latch above is the second half of the same guard).
        reloadCommitted = true;
        for (var r = 0; r < registry.length; r++) {
            if (registry[r].updatePending) reloadDisarmed.push(registry[r]);
            registry[r].updatePending = false;
        }
        clearRetry();
        beginReloadAttempt();
    }

    /**
     * Record one instance's version transition for the reload now being
     * committed, remembering which records this attempt wrote so the survival
     * watchdog can retract them.
     * @param {Object} p Instance record.
     */
    function noteReloadTransition(p) {
        var written = rememberFlip(p.name, p.getBaselineVersion(), p.getLatestVersion());
        for (var i = 0; i < written.length; i++) reloadRecordsWritten.push(written[i]);
    }

    /**
     * Call location.reload() and ARM A SURVIVAL WATCHDOG.
     *
     * There is no callback for "the navigation was refused", so the only proof
     * a scripted reload did not happen is that this document is still running a
     * moment later. Hosts that do exactly that are ordinary for a drop-in kit:
     * an embedded WebView or Electron shell that intercepts navigation, a
     * `beforeunload` confirm the user answers with "Stay", a sandboxed context
     * where reload() throws (which safe() would otherwise swallow into a debug
     * line). A synchronous throw is immediate proof and needs no timer.
     *
     * Mirrors client-refresh.js's beginReloadAttempt/recoverFailedReload pair.
     */
    function beginReloadAttempt() {
        try {
            location.reload();
        } catch (err) {
            safe(function () { console.debug(LOG, 'location.reload() threw:', err); });
            safe(recoverFailedReload);
            return;
        }
        armReloadSurvivalWatchdog();
    }

    /**
     * Arm (or RE-arm) the watchdog that decides a reload never navigated.
     *
     * Separate from beginReloadAttempt() because a HANDOFF can inherit a
     * committed reload (REGISTRATION CONTRACT clause 7): the old manager's
     * timers are all stopped as part of handing over, so without re-arming
     * here, a navigation the host blocks a moment later would leave the new
     * manager latched shut for the life of the document — the exact failure
     * 2.2.0's watchdog was added to end. The new manager must NOT call
     * location.reload() again (that would be the double-fire), only re-arm the
     * watch. Worst case the recovery lands a fraction of a second later than it
     * would have.
     */
    function armReloadSurvivalWatchdog() {
        var fired = false;
        var survived = function () {
            if (fired) return;
            fired = true;
            reloadWatchdogTimer = null;
            safe(recoverFailedReload);
        };
        reloadWatchdogTimer = safe(function () {
            return setTimeout(survived, RELOAD_SURVIVAL_WATCHDOG_MS);
        }, null);
        if (reloadWatchdogTimer === null || reloadWatchdogTimer === undefined) {
            // No timer could be armed (an exotic host). Better to re-open the
            // latch immediately than to risk latching it forever: the worst
            // case is one extra tryReload() in a document that is unloading,
            // which the budget reservation already bounds.
            survived();
        }
    }

    /**
     * The watchdog fired (or reload() threw): this document is still here, so
     * the navigation did not happen. Undo the commit completely — re-open the
     * latch, re-arm the instances it disarmed, retract the flip records this
     * attempt wrote (a version transition that never occurred must not make the
     * flap guard refuse the real one later) — warn ONCE, and get back in line.
     *
     * The budget slot is deliberately NOT refunded: the reservation is what
     * stops a host that refuses reloads from being asked once a second forever,
     * and the retry ladder re-tests as soon as the rolling window rolls.
     */
    function recoverFailedReload() {
        if (!reloadCommitted) return;
        reloadCommitted = false;
        reloadsSurvived++;

        var i;
        for (i = 0; i < reloadRecordsWritten.length; i++) {
            removeFromTabList(reloadRecordsWritten[i][0], reloadRecordsWritten[i][1]);
        }
        reloadRecordsWritten = [];

        var rearmed = [];
        for (i = 0; i < reloadDisarmed.length; i++) {
            var p = reloadDisarmed[i];
            if (registry.indexOf(p) === -1) continue;
            p.updatePending = true;
            if (rearmed.indexOf(p.name) === -1) rearmed.push(p.name);
        }
        reloadDisarmed = [];

        if (!warnedReloadSurvived) {
            warnedReloadSurvived = true;
            safe(function () {
                console.warn(LOG, 'location.reload() did not navigate — this document is still here ' +
                    (RELOAD_SURVIVAL_WATCHDOG_MS / 1000) + 's later, so the host blocked, ignored or ' +
                    'cancelled the reload. Re-arming the pending update' +
                    (rearmed.length ? ' for ' + rearmed.join(', ') : '') + ' and retrying; the version ' +
                    'transition recorded for the navigation that never happened has been retracted. ' +
                    'One reload-budget slot was spent and is not refunded (that is what keeps a host ' +
                    'which refuses reloads from being asked once a second). (Warned once.)');
            });
        }

        blockedRetries = 0;
        scheduleRetry();
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Shared interaction tracking
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Discrete interactions: stamp the idle clock AND re-evaluate the reload —
     * but deferred by one task, so the host page's own handler for this event
     * has already run (and, e.g., opened its dialog) before we probe safety.
     */
    function onDiscreteInteraction() {
        if (handedOff) return;
        lastInteractionAt = Date.now();
        // Supersede any chain an earlier interaction armed. Typing fires
        // keydown + input per character; without this every keystroke left two
        // live timers behind that nothing could cancel.
        clearInteractionTimers();
        if (pendingInstances().length === 0) return;
        clearRetry();
        blockedRetries = 0;
        settleHopTimer = setTimeout(function () {
            settleHopTimer = null;
            // Wait out the remaining idle window from THIS interaction.
            var remaining = Math.max(0,
                lastInteractionAt + effectiveIdleWindowMs(pendingInstances()) - Date.now());
            settleTimer = setTimeout(function () {
                settleTimer = null;
                safe(tryReload);
            }, remaining);
        }, 0);
    }

    /**
     * Continuous interactions (mousemove/wheel/scroll/touchmove): stamp only.
     * Re-arming a timer per event here would schedule thousands of timers during
     * a single scroll. The 1Hz retry tick already re-evaluates on its own.
     */
    function onContinuousInteraction() {
        if (handedOff) return;
        lastInteractionAt = Date.now();
    }

    /**
     * Tab became visible again (or regained focus, or came back from bfcache).
     * Catch up every instance immediately — the tab may have been hidden for
     * hours — each instance's own min-gap floor keeps the
     * visibilitychange/focus/pageshow burst at one fetch per instance, not three.
     */
    function onWake() {
        if (handedOff) return;
        var i;
        if (document.visibilityState === 'hidden') {
            for (i = 0; i < registry.length; i++) registry[i].stopPolling();
            clearRetry();
            return;
        }
        for (i = 0; i < registry.length; i++) registry[i].wake();
        // A pending update that was blocked purely by 'hidden' can now proceed.
        if (pendingInstances().length > 0) { blockedRetries = 0; safe(tryReload); }
    }

    /**
     * Every document/window listener this copy installed, kept so that a
     * HANDOFF can take them all off again (REGISTRATION CONTRACT clause 7).
     * The guards at the top of the handlers are the correctness half — a
     * handed-off copy must observe nothing — and this is the tidiness half: a
     * retired manager should not be left holding capture-phase listeners on
     * every pointer and key event for the life of the document.
     * @type {Array<Object>}
     */
    var pageListeners = [];

    /**
     * @param {EventTarget} target
     * @param {string} type
     * @param {Function} fn
     * @param {Object|boolean} opts Passed to addEventListener AND to
     *   removeEventListener — they must match for the removal to take.
     */
    function addPageListener(target, type, fn, opts) {
        safe(function () {
            target.addEventListener(type, fn, opts);
            pageListeners.push({ target: target, type: type, fn: fn, opts: opts });
        });
    }

    /** Remove every listener addPageListener installed. */
    function removePageListeners() {
        for (var i = 0; i < pageListeners.length; i++) {
            safe(function (l) {
                return function () { l.target.removeEventListener(l.type, l.fn, l.opts); };
            }(pageListeners[i]));
        }
        pageListeners = [];
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Instance factory — everything per-collection lives in this closure
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Create one instance: its own version source + polling + baseline, its own
     * bootstrap entry chain, its own mode/callback. Reloads and interception
     * stay page-level (shared engine / shared wrapper).
     *
     * @param {string} name Resolved unique instance name.
     * @param {RefreshKitConfig} cfg Normalized config.
     * @param {string} sourceKitVersion KIT_VERSION of the registering copy.
     * @param {boolean} [entriesSuppressed] Register normally but do NOT load
     *   entryScripts: an already-registered instance is loading the same files
     *   and loading them twice into one document is never correct.
     * @param {Object} [restore] HANDOFF ONLY (REGISTRATION CONTRACT clause 7):
     *   the live internals of the same instance as it was running under the
     *   PREVIOUS manager (transferState() below produces it). An instance
     *   rebuilt from one does not start over — it RESUMES: same baseline, same
     *   pending update, same entry-chain progress, same one-shot warning
     *   latches. Starting over would be a correctness bug, not just churn: a
     *   fresh baseline would silently adopt whatever the endpoint says NOW as
     *   "the build this tab is running", so an update that landed before the
     *   handoff would never be detected, and a bootstrap chain would re-execute
     *   the collection's entry files into a document that already has them.
     * @returns {Object} Internal instance record.
     */
    function createInstance(name, cfg, sourceKitVersion, entriesSuppressed, restore) {
        /** Log prefix so N instances' messages stay attributable. */
        var TAG = '[' + name + ']';
        /** @type {boolean} True when this instance was rebuilt by a handoff. */
        var restored = !!restore;

        /**
         * Marker for the one-shot per-tab recovery that fires when this
         * instance's boot seed disagrees with its own version endpoint.
         * @type {string}
         */
        var BOOT_SEED_MARKER = 'boot-seed|' + name + '|' + cfg.bootVersion;

        /**
         * Should we trust `bootVersion`? The seed is the identity of the build
         * that SERVED this document, so it is strictly better than a first-poll
         * baseline — but only if it names the same identity the version
         * endpoint reports. An adopter who stamps CacheKey into the tag and
         * points versionJsonField at a bare assembly Version has configured a
         * permanent disagreement, and a naively-trusted seed would make every
         * single page load look like an update and burn the reload budget.
         *
         * The self-heal: the first disagreement spends a one-shot recovery
         * marked with THIS boot identity and reloads. If the reload comes back
         * with the same boot identity still disagreeing, the server did not
         * change — the disagreement is provenance, not an update — so the seed
         * is discarded here, with one warning, and the instance falls back to
         * the pre-2.1 first-poll baseline. Cost of a misconfiguration: one
         * reload per tab session, then correct (if blind-spotted) behaviour.
         */
        var bootSeedRejected = !!cfg.bootVersion && recoverySpent(BOOT_SEED_MARKER);
        // A restored instance already logged this (or not) under the previous
        // manager; a handoff must not re-say anything the page has been told.
        if (bootSeedRejected && !restored) {
            safe(function () {
                console.warn(LOG, TAG, 'data-boot-version "' + cfg.bootVersion + '" still disagrees with ' +
                    'the version endpoint after a reload, so it does not describe the same identity the ' +
                    'endpoint reports — ignoring it and using the first-poll baseline. Point ' +
                    'data-boot-version and the version endpoint at the SAME value (RefreshKit.cs emits ' +
                    'data-boot-version="{CacheKey}"; pair it with data-version-json-field="CacheKey").');
            });
        }

        /**
         * @type {string|null} The build this tab is RUNNING for this instance.
         * Seeded from `bootVersion` when the document told us which build
         * produced it — that closes the page-serve → first-poll window, in
         * which an update would otherwise be absorbed into the baseline and
         * never detected. Otherwise established by the first successful fetch.
         */
        var baselineVersion = (cfg.bootVersion && !bootSeedRejected) ? cfg.bootVersion : null;
        /** @type {boolean} True while the baseline is the un-confirmed document seed. */
        var baselineFromBootSeed = baselineVersion !== null;
        /** @type {string|null} Most recent version seen on this instance's server endpoint. */
        var latestVersion = baselineVersion;
        /** @type {number} Timestamp of the last version fetch attempt. */
        var lastFetchAt = 0;
        /** @type {number|null} setTimeout handle for this instance's poll loop. */
        var pollTimer = null;
        /**
         * Last version we announced (log + onUpdateAvailable). In 'notify' mode
         * nothing ever clears the mismatch, so without this watermark every poll
         * would re-announce the SAME unchanged update — once a minute, forever.
         * @type {string|null}
         */
        var notifiedVersion = null;
        /**
         * A version seen once that differs from the baseline, waiting for a
         * second consecutive sighting before it is believed. Reset the moment
         * the baseline is observed again, which is what makes an A→B→A→B
         * oscillation never confirm anything.
         * @type {string|null}
         */
        var candidateVersion = null;
        /** @type {number|null} One-shot timer for the confirmation fetch. */
        var confirmTimer = null;
        /**
         * True once this poll cycle has spent its one confirmation fetch. Reset
         * by any ordinary (non-confirmation) poll, which is what starts a new
         * cycle. @type {boolean}
         */
        var confirmSpentThisCycle = false;
        /** @type {boolean} One-shot latch for the confirmation-churn warning. */
        var warnedConfirmChurn = false;
        /** @type {boolean} True while a version fetch for this instance is in flight. */
        var fetchInFlight = false;
        /** @type {boolean} One-shot latch for the flap-disarm log line. */
        var warnedFlap = false;
        /** @type {string|null} The version pair auto-reload was disarmed for. */
        var flapDisarmedFor = null;
        /** @type {boolean} One-shot latch so version-source failures warn exactly once. */
        var warnedFetchFailure = false;
        /** @type {boolean} True when entryScripts are configured (bootstrap mode). */
        var bootstrapMode = cfg.entryScripts.length > 0 && !entriesSuppressed;
        /** @type {boolean} True when entryScripts exist but a sibling instance owns them. */
        var entriesDeduped = cfg.entryScripts.length > 0 && !!entriesSuppressed;
        /** @type {boolean} One-shot latch so the entry chain can only start once. */
        var entriesStarted = false;
        /** @type {boolean} True once every entry has settled (loaded, or failed and skipped). */
        var entriesLoaded = false;
        /** @type {boolean} Did the entries get a ?v= — i.e. did the version resolve in time? */
        var entriesVersioned = false;
        /** @type {number|null} setTimeout handle for the bootstrap entry-timeout race. */
        var entryBootTimer = null;
        /**
         * The promise of the entry chain, kept so a handoff can hand the NEW
         * instance something to observe: a chain already in flight belongs to
         * the closure that started it and must be allowed to finish (the page
         * needs those files), but the instance that takes over still has to
         * learn when it settled or its state() would report entriesLoaded:false
         * forever. @type {Promise<void>|null}
         */
        var entriesChain = null;
        /**
         * DEACTIVATED by a handoff: this closure is retired and must stop
         * acting on the document. It is not the same as suspended — a suspended
         * instance wakes up again. @type {boolean}
         */
        var deactivated = false;

        // ── Handoff: resume the previous manager's instance, don't restart it ──
        if (restored) {
            baselineVersion = typeof restore.baselineVersion === 'string' ? restore.baselineVersion : null;
            baselineFromBootSeed = restore.baselineFromBootSeed === true;
            latestVersion = typeof restore.latestVersion === 'string' ? restore.latestVersion : baselineVersion;
            notifiedVersion = typeof restore.notifiedVersion === 'string' ? restore.notifiedVersion : null;
            candidateVersion = typeof restore.candidateVersion === 'string' ? restore.candidateVersion : null;
            lastFetchAt = typeof restore.lastFetchAt === 'number' ? restore.lastFetchAt : 0;
            bootSeedRejected = restore.bootSeedRejected === true;
            // One-shot latches, so the page is not told the same thing twice by
            // two different copies of the same kit.
            warnedFlap = restore.warnedFlap === true;
            flapDisarmedFor = typeof restore.flapDisarmedFor === 'string' ? restore.flapDisarmedFor : null;
            warnedFetchFailure = restore.warnedFetchFailure === true;
            warnedConfirmChurn = restore.warnedConfirmChurn === true;
            confirmSpentThisCycle = restore.confirmSpentThisCycle === true;
            // Entry-chain progress. `entriesStarted` is the load-bearing one:
            // loadEntries() is a one-shot latched on it, so carrying it across
            // is what stops a handoff from re-executing a collection's entry
            // files into a document that already ran them.
            entriesStarted = restore.entriesStarted === true;
            entriesLoaded = restore.entriesLoaded === true;
            entriesVersioned = restore.entriesVersioned === true;
            if (entriesStarted && !entriesLoaded && restore.entriesChain &&
                typeof restore.entriesChain.then === 'function') {
                safe(function () {
                    restore.entriesChain.then(function () { entriesLoaded = true; },
                        function () { entriesLoaded = true; });
                });
            }
        }

        /**
         * Does this URL belong to an asset THIS instance is supposed to version?
         * @param {string} url
         * @returns {boolean}
         */
        function matchesAssetPattern(url) {
            if (!url || typeof url !== 'string') return false;
            for (var i = 0; i < cfg.assetPatterns.length; i++) {
                var p = cfg.assetPatterns[i];
                if (typeof p === 'string') {
                    if (p && url.indexOf(p) !== -1) return true;
                } else if (p instanceof RegExp) {
                    // Reset lastIndex: a /g regex is stateful across .test() calls.
                    p.lastIndex = 0;
                    if (p.test(url)) return true;
                }
            }
            return false;
        }

        /**
         * Instance-scoped versioning: append `?v=<this instance's version>` when
         * — and only when — all of:
         *   • this instance's version is known (before that, pass through),
         *   • the URL matches one of ITS assetPatterns OR `force` is set (entry
         *     scripts are explicitly this instance's own, so they are versioned
         *     whether or not the adopter bothered to list a matching pattern),
         *   • the URL does not already carry a `v=` parameter.
         * @param {string} url
         * @param {boolean} [force] Skip the assetPattern check (bootstrap entries).
         * @returns {string} The versioned URL, or the input unchanged.
         */
        function versionedUrl(url, force) {
            return safe(function () {
                if (typeof url !== 'string' || !url) return url;
                if (!baselineVersion) return url;
                if (!force && !matchesAssetPattern(url)) return url;
                if (hasVersionParam(url)) return url;
                return appendVersion(url, baselineVersion);
            }, url);
        }

        /**
         * Fetch the current version from this instance's configured source.
         *
         * Two layers of cache defeat are needed here, because the version
         * endpoint is the one request that absolutely must not be stale:
         *   • cache: 'no-store'   — tells the HTTP cache not to serve or store it,
         *   • ?_=<timestamp>      — defeats caches that ignore no-store anyway
         *                           (some proxies, some embedded WebViews).
         *
         * @returns {Promise<string>} Resolves to a non-empty version string.
         */
        function fetchVersion() {
            lastFetchAt = Date.now();

            if (cfg.getVersion) {
                return withTimeout(
                    Promise.resolve()
                        .then(function () { return cfg.getVersion(); })
                        .then(function (v) {
                            var s = String(v == null ? '' : v).trim();
                            if (!s) throw new Error('getVersion() returned an empty version');
                            return s;
                        }),
                    VERSION_FETCH_TIMEOUT_MS,
                    'getVersion()');
            }

            if (!cfg.versionUrl) return Promise.reject(new Error('no versionUrl configured'));

            var url = cfg.versionUrl + (cfg.versionUrl.indexOf('?') === -1 ? '?' : '&') + '_=' + Date.now();
            // AbortController where it exists, so a timed-out request is also
            // CANCELLED rather than left holding one of the browser's six
            // per-host connections. Where it does not, withTimeout still
            // guarantees the promise settles, which is the load-bearing half.
            var controller = safe(function () {
                return typeof AbortController === 'function' ? new AbortController() : null;
            }, null);
            var init = { cache: 'no-store', credentials: 'same-origin' };
            if (controller) init.signal = controller.signal;
            return withTimeout(fetch(url, init)
                .then(function (res) {
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    return res.text();
                })
                .then(function (text) {
                    var value = text;
                    if (cfg.versionJsonField) {
                        var parsed = JSON.parse(text);
                        value = parsed ? parsed[cfg.versionJsonField] : '';
                    }
                    var s = String(value == null ? '' : value).trim();
                    if (!s) throw new Error('empty version response');
                    // Guard against an HTML error page being read as a "version".
                    if (s.length > 200 || s.charAt(0) === '<') throw new Error('version response does not look like a version');
                    return s;
                }),
                VERSION_FETCH_TIMEOUT_MS,
                'version fetch',
                function () { if (controller) controller.abort(); });
        }

        /** Cancel a pending candidate-confirmation fetch. */
        function clearConfirmTimer() {
            if (confirmTimer !== null) { clearTimeout(confirmTimer); confirmTimer = null; }
        }

        /**
         * Ask for the second consecutive observation that promotes a candidate
         * into a real update. Scheduled rather than waiting for the next
         * ordinary poll, so confirmation costs ~VERSION_CONFIRM_MS instead of a
         * whole pollSeconds.
         *
         * BOUNDED TO ONE PER POLL CYCLE (2.1.1). The confirmation poll is
         * forced, so it also skips MIN_FETCH_GAP_MS; before the bound, a
         * version source whose successive reads never repeat (per-replica DLL
         * mtimes behind a round-robin, a volatile versionJsonField) re-armed a
         * new candidate on every confirmation and produced a self-sustaining
         * 1.5s fetch loop — ~40x the configured cadence, forever, while never
         * confirming anything. Now the FIRST sighting in a cycle earns one
         * confirmation; if that confirmation brings back a THIRD distinct
         * value, the kit stops chasing and waits for the ordinary poll, which
         * opens the next cycle.
         */
        function scheduleConfirm() {
            clearConfirmTimer();
            if (cfg.mode === 'off') return;
            if (confirmSpentThisCycle) {
                if (!warnedConfirmChurn) {
                    warnedConfirmChurn = true;
                    safe(function () {
                        console.warn(LOG, TAG, 'the version source returned a THIRD distinct identity ' +
                            'within one poll cycle — that is an unstable source, not a release. Not ' +
                            'scheduling another confirmation; waiting for the next ordinary poll ' +
                            '(one confirmation per cycle). Serve one identity per release across all ' +
                            'nodes. (Warned once.)');
                    });
                }
                return;
            }
            confirmSpentThisCycle = true;
            confirmTimer = setTimeout(function () {
                confirmTimer = null;
                safe(function () { poll(true, true); });
            }, VERSION_CONFIRM_MS);
        }

        /**
         * The entries for this instance went out with no ?v=, so the code
         * actually running in this tab is whatever the HTTP cache happened to
         * hold — NOT the version the endpoint has now recovered to. Adopting
         * that version as a clean baseline is what used to leave the tab
         * permanently stale while state() reported it healthy.
         *
         * So: treat it as an update and ask for ONE reload, guarded by a
         * per-tab marker. A chronically slow or dead endpoint must fail by
         * running last week's code, not by reload-looping the browser.
         * @param {string} version The version the endpoint has recovered to.
         */
        function recoverUnversionedBoot(version) {
            var marker = 'unversioned-entries|' + name;
            if (cfg.mode !== 'auto') {
                safe(function () {
                    console.warn(LOG, TAG, 'entries loaded UNVERSIONED and the version endpoint has ' +
                        'since recovered (' + version + '); the code in this tab may be stale. ' +
                        "mode '" + cfg.mode + "' does not reload — refresh to converge.");
                });
                return;
            }
            if (!claimRecovery(marker)) {
                safe(function () {
                    console.log(LOG, TAG, 'entries loaded UNVERSIONED again; this tab has already spent ' +
                        'its one recovery reload this session — not reloading (a dead endpoint must not loop).');
                });
                return;
            }
            safe(function () {
                console.warn(LOG, TAG, 'entries loaded UNVERSIONED but the version endpoint has recovered (' +
                    version + '); the loaded code may be a stale cache entry. Requesting ONE safe reload ' +
                    '(once per tab session) to reload them version-addressed.');
            });
            inst.updatePending = true;
            blockedRetries = 0;
            safe(tryReload);
        }

        /**
         * Called with each successfully fetched version for THIS instance.
         *
         * Shape of the decision, in order:
         *   1. no baseline yet          → this fetch establishes it,
         *   2. version === baseline     → nothing new; clear any candidate,
         *   3. first sighting of a new  → hold it as a CANDIDATE and confirm,
         *   4. second consecutive sight → announce, then (auto) arm the reload.
         * @param {string} version
         */
        function onVersion(version) {
            // A fetch this closure started before a handoff can only land after
            // it. The instance that took over owns the decision now (and has
            // its own request in flight); acting on it here would arm a reload
            // engine that has been retired, against a baseline nobody reads.
            if (deactivated) return;

            // First success establishes the baseline: "this is the build this tab
            // is running". It is deliberately NOT the newest version — it is the
            // one whose assets are already in memory.
            if (baselineVersion === null) {
                baselineVersion = version;
                latestVersion = version;
                safe(function () { console.log(LOG, TAG, 'version resolved:', version); });
                // ...unless the assets already in memory went out unversioned,
                // in which case "the version we just resolved" describes the
                // server, not this tab.
                if (entriesStarted && !entriesVersioned) recoverUnversionedBoot(version);
                return;
            }

            latestVersion = version;

            if (version === baselineVersion) {
                // The baseline is confirmed by the server. Anything we were
                // holding as a candidate was a blip (or a flap): drop it, so a
                // later sighting has to earn two consecutive observations again.
                candidateVersion = null;
                clearConfirmTimer();
                if (baselineFromBootSeed) baselineFromBootSeed = false;

                // RETRACTED UPDATE. The server has come back to the build this
                // tab is already running, so a still-armed reload no longer
                // describes reality — the operator rolled the deploy back while
                // the reload was refused by a gate (a paused video, an open
                // dialog, the budget). Left armed it would eventually reload
                // the tab for nothing, logging "X → X" and spending a budget
                // slot, in every open tab. Mirrors the reference's watermark
                // convergence (client-refresh.js: `if (next.BuildId ===
                // loadedBuildId) pendingSources.delete('plugin')`).
                //
                // Gated on notifiedVersion so the UNVERSIONED-BOOT recovery —
                // which legitimately arms a reload while version ===
                // baselineVersion, and never announces a version — is not
                // cancelled by the very poll that recovered it.
                if (inst.updatePending && notifiedVersion !== null && notifiedVersion !== version) {
                    inst.updatePending = false;
                    var retracted = notifiedVersion;
                    notifiedVersion = null;
                    safe(function () {
                        console.log(LOG, TAG, 'update ' + retracted + ' was RETRACTED — the endpoint ' +
                            'reports ' + version + ' again, which is what this tab is running. ' +
                            'Disarming the pending reload; a genuine later release re-arms normally.');
                    });
                    releaseEngineIfIdle();
                }
                return;
            }

            // A candidate must be seen TWICE IN A ROW before it is believed.
            // One observation is not evidence of a release: a version source
            // that alternates between two nodes' build identities would
            // otherwise reload this tab once per poll, forever, with every
            // single reload comfortably inside its own budget window.
            if (version !== candidateVersion) {
                candidateVersion = version;
                safe(function () {
                    console.debug(LOG, TAG, 'candidate version ' + version + ' (baseline ' +
                        baselineVersion + ') — confirming before acting on it');
                });
                scheduleConfirm();
                return;
            }
            clearConfirmTimer();

            // Confirmed. A boot seed that survives its first confirmed
            // disagreement has done its job: it is a real update, not a
            // provenance mismatch. Remember the attempt so that if the reload
            // brings back the SAME boot identity still disagreeing, the next
            // page discards the seed instead of reloading again.
            if (baselineFromBootSeed) {
                safe(function () { claimRecovery(BOOT_SEED_MARKER); });
            }

            // Decide the flap refusal BEFORE announcing (2.3.0). The full
            // explanation below is once-gated on purpose — it is a paragraph —
            // but the one-line "update available" announcement is NOT: a tab
            // sitting behind a flapping endpoint keeps meeting new candidate
            // identities and logs that line for each of them. Before 2.3.0 the
            // line carried no hint that the kit had latched, so a support log
            // read as "the kit sees updates and does nothing", forever, with
            // the single warning explaining why scrolled far out of view.
            // Same fact, same line, every time.
            var flapRefused = cfg.mode === 'auto' && hasLeftVersion(name, version);

            var firstAnnouncement = version !== notifiedVersion;
            if (firstAnnouncement) {
                notifiedVersion = version;
                // A genuinely new update earns a fresh 1Hz retry allowance.
                // Repeat sightings of the SAME pending update deliberately do
                // not, so a tab parked on a video cannot be made to tick at 1Hz
                // for eternity — those polls re-test the gate directly instead.
                blockedRetries = 0;
                safe(function () {
                    console.log(LOG, TAG, 'update available: ' + baselineVersion + ' → ' + version +
                        (flapRefused
                            ? ' — auto-reload REFUSED: this tab has already reloaded AWAY FROM ' +
                              version + ', so the version source is flapping, not releasing ' +
                              '(see the one "version FLAP" warning for the full explanation)'
                            : ''));
                });
                if (cfg.onUpdateAvailable) {
                    safe(function () { cfg.onUpdateAvailable(version, baselineVersion); });
                }
            }

            // 'notify' and 'off' stop here — a notify instance NEVER triggers the
            // shared reload; its callback above already fired.
            if (cfg.mode !== 'auto') return;

            // Has this tab already reloaded AWAY FROM the version it is now
            // being asked to go back to? Then the endpoint is oscillating and
            // reloading again just walks back — over two identities or twenty.
            if (flapRefused) {
                // ALWAYS current (2.3.0). This used to live inside the
                // once-only warning block, so state() kept reporting the FIRST
                // refused pair while later versions were being refused for the
                // same reason — a snapshot that named a transition the kit was
                // no longer talking about. The paragraph below stays once-only;
                // the diagnostic field tracks the latest refusal.
                flapDisarmedFor = baselineVersion + ' ⇄ ' + version;
                if (!warnedFlap) {
                    warnedFlap = true;
                    safe(function () {
                        var went = flipDestinationFrom(name, version);
                        console.warn(LOG, TAG, 'version FLAP: this tab already reloaded away from ' +
                            version + (went ? ' (' + version + ' → ' + went + ')' : '') +
                            ', and the endpoint now reports ' + version + ' again. That is an unstable ' +
                            'version source, not a release — auto-reload is refused for every version ' +
                            'this tab has already left, so an oscillation over any number of node ' +
                            'identities stops here. Serve one identity per release across all nodes.');
                    });
                }
                inst.updatePending = false;
                return;
            }

            // Arm (or RE-arm). Re-arming matters: a reload refused by the
            // budget keeps updatePending set and is retried by the engine, but
            // a reload abandoned any other way must be recoverable by the very
            // next successful poll — which is exactly what the README promises
            // and what checkNow() is for.
            if (!inst.updatePending) inst.updatePending = true;
            safe(tryReload);
        }

        /**
         * One poll cycle for this instance: fetch and react. Rescheduling is
         * deliberately NOT chained to this promise — see startPolling().
         * @param {boolean} [force] Skip the min-gap floor (used by checkNow()).
         * @param {boolean} [isConfirm] This is the one confirmation fetch of the
         *   current cycle. Any other poll OPENS a new cycle.
         * @returns {Promise<void>}
         */
        function poll(force, isConfirm) {
            if (deactivated) return Promise.resolve();
            if (cfg.mode === 'off') return Promise.resolve();
            if (!force && (Date.now() - lastFetchAt) < MIN_FETCH_GAP_MS) return Promise.resolve();
            // One version request per instance at a time. Without this every
            // wake of a tab whose endpoint is hanging started another one.
            if (!force && fetchInFlight) return Promise.resolve();

            if (!isConfirm) confirmSpentThisCycle = false;
            fetchInFlight = true;
            return fetchVersion().then(function (v) {
                fetchInFlight = false;
                warnedFetchFailure = false;
                safe(function () { onVersion(v); });
            }, function (err) {
                fetchInFlight = false;
                // Version-source failure is NOT an update. Warn once, stay quiet
                // afterwards (a 404'd version.json must not spam the console every
                // minute), never reload, and keep polling — the endpoint may come
                // back after a deploy.
                if (!warnedFetchFailure) {
                    warnedFetchFailure = true;
                    safe(function () { console.warn(LOG, TAG, 'version check failed (further failures silenced):', err && err.message ? err.message : err); });
                }
            });
        }

        /**
         * Stop this instance's poll timer. Deliberately does NOT touch the
         * confirmation timer: startPolling() re-arms the loop through here at
         * the top of every poll tick, and the confirmation is armed moments
         * later from inside that same tick's onVersion — clearing it here would
         * cancel every confirmation the moment it was scheduled, and no
         * candidate would ever be confirmed. Suspending the instance entirely
         * is suspend()'s job.
         */
        function stopPolling() {
            if (pollTimer !== null) { clearTimeout(pollTimer); pollTimer = null; }
        }

        /**
         * Suspend this instance completely — used when the tab goes hidden,
         * where "a hidden tab holds ZERO timers" is a promise the kit makes.
         * wake() re-polls and re-observes any candidate from scratch.
         *
         * The one timer this cannot reach is the ceiling on a request that was
         * already in flight when the tab was hidden; it is self-clearing and
         * lives at most VERSION_FETCH_TIMEOUT_MS, and abandoning a hung request
         * with no ceiling is the strictly worse trade.
         */
        function suspend() {
            stopPolling();
            clearConfirmTimer();
        }

        /**
         * (Re)arm this instance's poll loop. Polling only runs while the document
         * is visible: a hidden tab holds ZERO timers, which is the difference
         * between a kit you can ship to 100k users and one that melts laptops in
         * background tabs.
         */
        function startPolling() {
            stopPolling();
            if (cfg.mode === 'off') return;
            if (document.visibilityState === 'hidden') return;
            pollTimer = setTimeout(function () {
                pollTimer = null;
                // RE-ARM FIRST, then poll. Hanging the re-arm off the fetch's
                // promise (`poll().then(startPolling, startPolling)`) meant a
                // request that never settled stopped the loop permanently: the
                // handler simply never ran and pollTimer stayed null. Arming
                // ahead of the request makes the next poll the retry — the same
                // shape as the reference's `.finally(schedulePoll)` — and the
                // in-flight guard in poll() keeps a slow endpoint from stacking
                // requests on top of each other.
                safe(startPolling);
                safe(function () { poll(); });
            }, cfg.pollSeconds * 1000);
        }

        /** Visibility catch-up for this instance (shared onWake fans out to these). */
        function wake() {
            if (cfg.mode === 'off') return;
            safe(startPolling);
            safe(function () { poll(); });
        }

        // ── Bootstrap mode: loading this instance's own entry files ──────────

        /**
         * Is this URL a stylesheet? Extension test against the PATH only, so
         * `theme.css?v=1` and `theme.css#x` count and `/css/loader.js` does not.
         * @param {string} url
         * @returns {boolean}
         */
        function isStylesheetUrl(url) {
            return /\.css(?:$|[?#])/i.test(String(url).split('#')[0]);
        }

        /**
         * Append ONE entry and resolve when it has settled.
         *
         * The promise resolves on error as well as on success — deliberately. The
         * contract is "order is preserved and the page survives"; a 404 on entry 2
         * must not strand entries 3..n. The failure is logged once, right here,
         * with the URL, because that is the only place that knows which file died.
         *
         * @param {string} url
         * @returns {Promise<void>}
         */
        function appendEntry(url) {
            return new Promise(function (resolve) {
                var settled = false;
                function finish(ok) {
                    if (settled) return;
                    settled = true;
                    if (!ok) {
                        safe(function () {
                            console.warn(LOG, TAG, 'entry failed to load (skipping, remaining entries continue):', url);
                        });
                    }
                    resolve();
                }

                var failed = safe(function () {
                    // Entries are this instance's own by definition, so force the
                    // ?v= with ITS version. If the version never resolved,
                    // versionedUrl() returns the URL untouched and we load
                    // unversioned — the availability path.
                    var finalUrl = versionedUrl(url, true);
                    var isCss = isStylesheetUrl(url);
                    // Note: document.createElement is already the shared wrapper
                    // here, so these elements carry the interceptors too. That is
                    // harmless and intentional — the URL already has v=, so the
                    // page-level matcher sees it and passes through rather than
                    // double-versioning (or cross-versioning by another instance).
                    var el = document.createElement(isCss ? 'link' : 'script');
                    el.onload = function () { finish(true); };
                    el.onerror = function () { finish(false); };
                    if (isCss) {
                        el.rel = 'stylesheet';
                        el.href = finalUrl;
                    } else {
                        // Dynamically-created scripts default to "force async".
                        // Explicitly clearing it keeps document-order execution even
                        // if a caller ever appends several at once; the sequential
                        // chain below is the primary guarantee, this is the backstop.
                        el.async = false;
                        el.src = finalUrl;
                    }
                    (document.head || document.documentElement).appendChild(el);
                    return false;
                }, true);

                // Creating/appending threw (no <head>, CSP, exotic engine): treat it
                // exactly like a load failure so the chain keeps moving.
                if (failed) finish(false);
            });
        }

        /**
         * Load every configured entry, strictly in order, once. (Order holds
         * WITHIN this instance; other instances' chains run concurrently.)
         * @returns {Promise<void>}
         */
        function loadEntries() {
            if (entriesStarted) return Promise.resolve();
            // Deactivated BEFORE the chain started: the instance that took over
            // owns these entries and will load them itself. Starting here would
            // be the double-execution the whole handoff is careful to avoid.
            if (deactivated) return Promise.resolve();
            entriesStarted = true;
            entriesVersioned = !!baselineVersion;

            safe(function () {
                console.log(LOG, TAG, 'bootstrap: loading ' + cfg.entryScripts.length + ' entr' +
                    (cfg.entryScripts.length === 1 ? 'y' : 'ies') +
                    (entriesVersioned ? ' at v=' + baselineVersion : ' UNVERSIONED'));
            });

            var chain = Promise.resolve();
            cfg.entryScripts.forEach(function (url) {
                chain = chain.then(function () { return appendEntry(url); });
            });
            entriesChain = chain.then(function () {
                entriesLoaded = true;
                safe(function () { console.log(LOG, TAG, 'bootstrap: all entries settled'); });
            });
            return entriesChain;
        }

        /**
         * Record — at most once, and at exactly one severity level — that the
         * entries had to go out without a version.
         *
         * Why the warnedFetchFailure latch is shared with poll(): a failed version
         * fetch already warns there. Warning again here would mean two warnings for
         * one root cause, and "how many warnings did you see" is how an operator
         * triages this. So: first message about the version problem is the warning,
         * any follow-up is an informational log.
         *
         * @param {string} why
         */
        function noteUnversionedEntries(why) {
            safe(function () {
                var message = 'entry scripts loading UNVERSIONED — ' + why +
                    '. Availability over freshness; if the endpoint recovers, the kit treats the ' +
                    'resolved version as an update and asks for one safe reload so the entries come ' +
                    'back version-addressed (once per tab session).';
                if (!warnedFetchFailure) {
                    warnedFetchFailure = true;
                    console.warn(LOG, TAG, message);
                } else {
                    console.log(LOG, TAG, message);
                }
            });
        }

        /**
         * The one version fetch that gates this instance's entries.
         *
         * mode 'off' disables polling and reloads, but bootstrap mode still needs
         * a version to build URLs with, so in that combination we do a single
         * direct fetch instead of going through poll() (which returns early when
         * off).
         * @returns {Promise<void>}
         */
        function firstVersionAttempt() {
            if (cfg.mode !== 'off') return poll(true);
            return fetchVersion().then(function (v) {
                safe(function () { onVersion(v); });
            }, function () { /* handled by the caller's fallback */ });
        }

        /** Arm this instance's poll loop, unless the caller disabled it. */
        function armPolling() {
            if (cfg.mode !== 'off') safe(startPolling);
        }

        /**
         * Bootstrap boot sequence: resolve the version, then load the entries —
         * but never wait longer than entryTimeoutMs to start loading them.
         *
         * Polling is armed as soon as the version question is settled either way.
         * It deliberately does NOT wait for the entries to finish downloading: a
         * slow (or stuck) entry must not also cost us update detection.
         *
         * @returns {Promise<void>}
         */
        function bootstrapEntries() {
            // Tracked on the closure (not a local) so deactivate() can cancel
            // it: a handoff in the window between "kit tag parsed" and "version
            // resolved" is the NORMAL case, and a retired closure firing this
            // timer would load the entry files the new instance is about to.
            entryBootTimer = setTimeout(function () {
                entryBootTimer = null;
                if (entriesStarted || deactivated) return;
                noteUnversionedEntries('version not resolved within ' + cfg.entryTimeoutMs + 'ms');
                safe(loadEntries);
                armPolling();
            }, cfg.entryTimeoutMs);

            function proceed() {
                if (entryBootTimer !== null) { clearTimeout(entryBootTimer); entryBootTimer = null; }
                if (deactivated) return Promise.resolve();
                if (!entriesStarted && !baselineVersion) {
                    noteUnversionedEntries('version source unavailable');
                }
                var done = safe(loadEntries, Promise.resolve());
                armPolling();
                return done;
            }

            return firstVersionAttempt().then(proceed, proceed);
        }

        /** Kick this instance off (called exactly once, at registration). */
        function start() {
            if (restored) { resume(); return; }

            // The first fetch is immediate so the version is known as early as
            // possible — every millisecond before it resolves is a window in
            // which the host bootstrap may create unversioned assets.
            //
            // In bootstrap mode there IS no host bootstrap racing us: nothing of
            // this collection loads until bootstrapEntries() says so, and polling
            // only starts once the entries have been kicked off (their fetches
            // matter more than the next poll).
            if (bootstrapMode) {
                safe(bootstrapEntries);
            } else if (cfg.mode !== 'off') {
                // Arm the loop before the first fetch, not from its promise: a
                // first request that never settles must not leave the instance
                // permanently unpolled (see startPolling).
                safe(startPolling);
                safe(function () { poll(true); });
            } else {
                // mode 'off' switches off POLLING and RELOADS — it does not
                // switch off layer 2. Without a resolved version, versionedUrl()
                // and the page matcher pass every one of this instance's assets
                // through unversioned, which silently disables the kit's primary
                // function on a flag documented as "no auto-reload". So: exactly
                // one fetch to establish the baseline, then nothing. (Bootstrap
                // + 'off' already did this via firstVersionAttempt(); this makes
                // classic + 'off' behave the same way.)
                safe(function () {
                    console.log(LOG, TAG, "mode 'off' — one version fetch for URL versioning, " +
                        'then no polling and no reloads');
                });
                safe(function () { return firstVersionAttempt(); });
            }
        }

        /**
         * Take over from the same instance running under the PREVIOUS manager
         * (REGISTRATION CONTRACT clause 7). Everything already done stays done;
         * only what the retired closure was still going to do is picked up:
         *
         *   • entries configured but never started — the old chain was
         *     cancelled with its closure, so this instance owns them now and
         *     runs the ordinary bootstrap sequence (version race included);
         *   • no baseline yet — whatever version request the old copy had in
         *     flight resolves into a deactivated closure and is discarded, so
         *     ask again immediately rather than waiting a whole pollSeconds
         *     with every asset going out unversioned;
         *   • otherwise just re-arm the poll loop. The baseline, the pending
         *     update, the announced version and the warning latches all came
         *     across in `restore`, so nothing is re-detected and nothing is
         *     re-logged.
         */
        function resume() {
            if (bootstrapMode && !entriesStarted) { safe(bootstrapEntries); return; }
            if (baselineVersion === null) {
                if (cfg.mode !== 'off') {
                    safe(startPolling);
                    safe(function () { poll(true); });
                } else {
                    safe(function () { return firstVersionAttempt(); });
                }
                return;
            }
            armPolling();
        }

        /**
         * RETIRE this closure as part of handing the page to a newer manager.
         *
         * Stronger than suspend(): a suspended instance wakes up, a deactivated
         * one never acts again. Every timer it owns is cancelled and every
         * entry point is latched off (poll, onVersion, loadEntries), because a
         * request or timer started before the handoff can still land after it —
         * and two live copies of ONE instance would poll the same endpoint
         * twice, announce twice, and arm two reload engines.
         *
         * The ONE thing deliberately left running is an entry chain already in
         * flight: those files are needed by the page, the elements are already
         * appended, and the instance taking over observes the chain's promise
         * instead of starting a second one.
         */
        function deactivate() {
            deactivated = true;
            suspend();
            if (entryBootTimer !== null) { clearTimeout(entryBootTimer); entryBootTimer = null; }
        }

        /**
         * The live internals a handoff carries to the instance that replaces
         * this one (see createInstance's `restore`). Everything here is state
         * that exists ONLY in this closure — the reload budget and the per-tab
         * flip/left/recovery records are already in session/localStorage under
         * page-wide keys (BUDGET_KEY, FLIP_KEY, LEFT_KEY, RECOVERY_KEY) and are
         * keyed by INSTANCE NAME, which the handoff preserves, so that history
         * survives on its own and must not be copied through here.
         * @returns {Object}
         */
        function transferState() {
            return {
                baselineVersion: baselineVersion,
                baselineFromBootSeed: baselineFromBootSeed,
                latestVersion: latestVersion,
                notifiedVersion: notifiedVersion,
                candidateVersion: candidateVersion,
                lastFetchAt: lastFetchAt,
                bootSeedRejected: bootSeedRejected,
                updatePending: inst.updatePending,
                warnedFlap: warnedFlap,
                flapDisarmedFor: flapDisarmedFor,
                warnedFetchFailure: warnedFetchFailure,
                warnedConfirmChurn: warnedConfirmChurn,
                confirmSpentThisCycle: confirmSpentThisCycle,
                entriesStarted: entriesStarted,
                entriesLoaded: entriesLoaded,
                entriesVersioned: entriesVersioned,
                entriesChain: entriesChain
            };
        }

        /**
         * Diagnostic snapshot for this instance. Field-compatible with the
         * 1.1.0 singleton state() so support workflows keep working, plus name /
         * registeredByKitVersion.
         *
         * On `blockReason` vs `wouldBlockNow`: blockReasonFor() is a live
         * safety probe with no notion of pendingness, so computing it
         * unconditionally produced snapshots reading `blockReason:
         * 'media_element'` next to `updatePending: false` on a perfectly
         * up-to-date tab — support then hunts a block that does not exist.
         * Since 2.1.0 `blockReason` answers the question it is named for
         * ("why is the pending auto-reload not happening?") and is null when
         * nothing is pending; the always-computed hypothetical kept its value
         * under the honest name `wouldBlockNow`.
         *
         * Since 2.1.1 a PENDING instance is evaluated against the window the
         * shared engine will actually use — the MAX idleSeconds across every
         * pending instance — not its own. A lax instance (idleSeconds 0) next
         * to a strict sibling (300) used to report `blockReason: null, idle:
         * true` while the engine was deferring with `not_idle`, which reads as
         * "the reload engine is broken" rather than "a sibling is stricter".
         * `effectiveIdleWindowMs` / `effectiveIdleWindowFrom` name the window
         * and the instance imposing it. `wouldBlockNow` deliberately keeps the
         * instance-local view: it answers "what would block ME".
         * @returns {Object}
         */
        function state() {
            var ownIdleWindow = Math.max(cfg.idleSeconds * 1000, MIN_SETTLE_MS);
            var isPending = cfg.mode === 'auto' && inst.updatePending;
            var pending = isPending ? pendingInstances() : null;
            var strictest = isPending ? strictestIdleInstance(pending) : null;
            var idleWindow = isPending ? effectiveIdleWindowMs(pending) : ownIdleWindow;
            return {
                kitVersion: KIT_VERSION,
                name: name,
                registeredByKitVersion: sourceKitVersion,
                // True when this instance was carried across a manager handoff
                // rather than registered from a <script> tag in this document.
                restoredByHandoff: restored,
                mode: cfg.mode,
                bootstrapMode: bootstrapMode,
                entriesLoaded: entriesLoaded,
                entriesVersioned: entriesVersioned,
                entriesDeduped: entriesDeduped,
                entryScripts: cfg.entryScripts.slice(),
                version: baselineVersion,
                latestVersion: latestVersion,
                bootVersion: cfg.bootVersion,
                baselineFromBootSeed: baselineFromBootSeed,
                candidateVersion: candidateVersion,
                flapDisarmedFor: flapDisarmedFor,
                updatePending: inst.updatePending,
                blockReason: isPending ? blockReasonFor(idleWindow) : null,
                wouldBlockNow: cfg.mode === 'auto' ? blockReasonFor(ownIdleWindow) : null,
                lastBlockReason: lastBlockReason,
                idle: (Date.now() - lastInteractionAt) >= idleWindow,
                idleWindowMs: ownIdleWindow,
                effectiveIdleWindowMs: idleWindow,
                effectiveIdleWindowFrom: strictest ? strictest.name : null,
                msSinceInteraction: Date.now() - lastInteractionAt,
                pollSeconds: cfg.pollSeconds,
                idleSeconds: cfg.idleSeconds,
                reloadBudget: cfg.reloadBudget,
                assetPatterns: cfg.assetPatterns.map(String),
                versionUrl: cfg.versionUrl,
                polling: pollTimer !== null,
                lastFetchAt: lastFetchAt
            };
        }

        /**
         * The PUBLIC per-instance handle (returned by manager.get(name) and by
         * __registerInstance). Frozen: it is API surface shared across kit
         * versions.
         */
        var handle = Object.freeze({
            /** @type {string} */
            name: name,
            /** @returns {string|null} The version this tab is running (this instance's baseline). */
            get version() { return baselineVersion; },
            /** @returns {string|null} The newest version seen on this instance's server. */
            get latestVersion() { return latestVersion; },
            /**
             * Version a URL with THIS instance's version/patterns. `force` skips
             * the pattern match (still never clobbers an existing v=).
             * @param {string} url
             * @param {boolean} [force]
             * @returns {string}
             */
            versionedUrl: versionedUrl,
            /**
             * Force an immediate version check for this instance, bypassing the
             * min-gap floor.
             * @returns {Promise<void>}
             */
            checkNow: function () { return safe(function () { return poll(true); }, Promise.resolve()); },
            /** @returns {Object} Snapshot of this instance's state. */
            state: function () { return safe(state, {}); }
        });

        var inst = {
            name: name,
            cfg: cfg,
            /** @type {boolean} True once an update has been detected and the shared engine should act. */
            updatePending: restored && restore.updatePending === true,
            /** @type {string} KIT_VERSION of the copy whose tag registered this adoption. */
            sourceKitVersion: sourceKitVersion,
            /** @type {boolean} Whether this instance's entry chain was suppressed as a duplicate. */
            entriesSuppressed: !!entriesSuppressed,
            handle: handle,
            matchesAssetPattern: matchesAssetPattern,
            versionedUrl: versionedUrl,
            getBaselineVersion: function () { return baselineVersion; },
            getLatestVersion: function () { return latestVersion; },
            poll: poll,
            // The shared engine only ever calls this to park a hidden tab, so
            // it gets the full suspend (poll timer AND confirmation timer).
            stopPolling: suspend,
            wake: wake,
            start: start,
            state: state,
            // Handoff surface (REGISTRATION CONTRACT clause 7).
            deactivate: deactivate,
            transferState: transferState
        };
        return inst;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Registration (the REGISTRATION CONTRACT implementation)
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * Apply the singular 1.x window config on behalf of a copy that could not
     * read it itself — a pre-2.1 copy (which passes no `__singularApplied`
     * marker) or a copy executed from eval'd text, where currentScript is null
     * and the window config is the documented escape hatch.
     *
     * It is a FALLBACK, not the primary path (2.1+ copies read the global at
     * their own tag position, which is the only reading that attributes a
     * config to its author), and since 2.2.0 it runs THE SAME RULE, through the
     * same function, as that primary path — see singularGlobalOutcome's truth
     * table.
     *
     * PER REGISTRATION, PER OBJECT (2.2.0). Until 2.1.2 this was gated on
     * `!singularConfigApplied && registry.length === 0`, which made it dead
     * code: `registry.length === 0` holds only for the MANAGER copy's own
     * registration, and the manager is by definition a 2.1+ copy that has
     * already settled the global at its own tag — so the one constituency the
     * fallback exists for, a pre-2.1 copy arriving SECOND, could never reach
     * it. The claim is what bounds it now, exactly as on the tag side: every
     * registration that did not settle the global itself is offered it, and the
     * object can be claimed only once.
     *
     * @param {Object} merged The registration's merged config, mutated in place.
     * @returns {void}
     */
    function applySingularWindowConfigFallback(merged) {
        safe(function () {
            var w = window.JellyfinRefreshKitConfig;
            if (!w || typeof w !== 'object') return;
            applySingularGlobal(merged, w);
        });
    }

    /**
     * @param {string[]} a
     * @param {string[]} b
     * @returns {boolean} True when both lists name the same entries in the same order.
     */
    function sameEntryList(a, b) {
        if (a.length !== b.length || a.length === 0) return false;
        for (var i = 0; i < a.length; i++) { if (a[i] !== b[i]) return false; }
        return true;
    }

    /**
     * Register one instance from a raw tag-level config. This is what
     * __registerInstance delegates to; the first copy's own registration goes
     * through the very same path, so there is exactly one code path.
     *
     * Merge order (last wins):
     *   defaults < data-* (rawConfig) < window.JellyfinRefreshKitConfig
     *   (read by the copy at its own tag, and skipped when it names a different
     *   adoption; manager fallback for older copies)
     *   < window.JellyfinRefreshKitConfigs[FINAL resolved name, "#N" included]
     *
     * The keyed layer is applied AFTER the name (and any collision suffix) is
     * settled and after the duplicate-registration test, which is run on the
     * config as the TAG declared it. See the block comment inside.
     *
     * @param {Object} rawConfig Plain object of tag-level options.
     * @param {string} sourceKitVersion KIT_VERSION of the registering copy.
     * @returns {Object|null} The instance's public handle, or null on failure.
     */
    function registerInstance(rawConfig, sourceKitVersion) {
        var raw = (rawConfig && typeof rawConfig === 'object') ? rawConfig : {};

        /** @type {any} */
        var merged = {};
        var key;
        for (key in raw) { if (Object.prototype.hasOwnProperty.call(raw, key)) merged[key] = raw[key]; }

        // 1.x back-compat. A 2.1+ copy has already SETTLED the singular global
        // for its own tag (merged or deliberately declined) and says so.
        // Anything else — a pre-2.1 copy, an eval'd copy with no currentScript —
        // is offered it here, under the identical rule and the identical
        // once-only claim (see applySingularWindowConfigFallback). For a
        // single-plugin page that is exactly the 1.x behaviour:
        // window > data-* > defaults.
        if (!raw.__singularApplied) applySingularWindowConfigFallback(merged);

        // Resolve the instance name BEFORE consulting the keyed config. Deriving
        // it straight off the raw merge made the keyed form unreachable for
        // exactly the tags that need it most: a tag with neither data-name nor
        // data-version-url (the eval/JS-Injector shape) resolved to '' and
        // skipped the lookup entirely, so its "instance-<N>" name was never
        // addressable.
        //
        // `declared` is the adoption exactly as the TAG declared it (data-* plus
        // the singular global), normalized but with no keyed entry merged in.
        var declared = normalizeConfig(merged);
        var declaredName = declared.name || deriveName(declared.versionUrl);

        // ANONYMOUS ADOPTIONS (no data-name, no versionUrl to derive one from —
        // the eval'd / JS-Injector shape configured entirely through the window
        // config). Two things have to hold for them that a plain
        // "instance-<registry.length + 1>" cannot deliver:
        //
        //  • A double registration of the SAME anonymous adoption must dedupe.
        //    An ordinal name is a function of arrival order, not of the
        //    adoption, so the byName lookup below always missed and the
        //    equivalence machinery was never reached: an injector applying the
        //    same payload twice got two live instances polling the same
        //    endpoint, firing onUpdateAvailable twice per release and warning
        //    about "overlapping assetPatterns" against itself. So compare
        //    against the anonymous instances already registered FIRST, using
        //    the same declared-config equivalence the named path uses.
        //  • The number must not move when an unrelated NAMED adopter registers
        //    first. It is the only handle such an adoption has
        //    (JellyfinRefreshKitConfigs['instance-1']), and a name that depends
        //    on somebody else's tag order is not a handle at all.
        //
        // The scan is skipped for an adoption that declares NOTHING identifying
        // at all (no version source, no patterns, no entries, no callback —
        // only defaults). Two of those are indistinguishable by construction,
        // and collapsing them would break the supported shape where several
        // bare tags are each configured by their own
        // JellyfinRefreshKitConfigs['instance-<N>'] entry, which is read only
        // AFTER the name is settled. Two such instances are inert until their
        // keyed entries arrive, so leaving them apart costs nothing.
        var anonymous = !declaredName;
        if (anonymous && declaresIdentity(declared)) {
            for (var q = 0; q < registry.length; q++) {
                if (registry[q].anonymous && configsEquivalent(registry[q].declaredCfg, declared)) {
                    return registry[q].handle;
                }
            }
        }

        var baseName = declaredName || ('instance-' + (anonymousCount + 1));
        var name = baseName;

        // DEDUPE / COLLISION FIRST, KEYED CONFIG SECOND (2.1.1). Two things
        // depend on this order:
        //
        //  • The equivalence test must run against the adoption AS THE TAG
        //    DECLARED IT (`declared` below), never against a config a keyed
        //    entry has already rewritten. A keyed entry that supplies e.g. a
        //    versionUrl would otherwise MANUFACTURE equivalence between two
        //    genuinely different adoptions, and the second one would vanish
        //    into the first's handle with no warning and no entry chain.
        //  • The keyed lookup must use the name the instance ENDS UP with,
        //    including the "#2" collision suffix. Looking it up under the
        //    pre-collision name made `JellyfinRefreshKitConfigs['Foo#2']`
        //    unreachable — the one key the docs tell you to use for the one
        //    instance that needs it — while `'Foo'` bled onto an unrelated
        //    second adoption that merely derived the same folder name.
        //
        // (`configsEquivalent` ignores `name` on purpose — see its doc block —
        // so a duplicate of a "#2" still compares equal to that "#2".)

        // Same name again?
        var existing = byName[name];
        if (existing) {
            if (configsEquivalent(existing.declaredCfg, declared)) {
                // Identical duplicate registration (double-included tag, or two
                // plugins genuinely shipping the same adoption): silent dedupe.
                return existing.handle;
            }
            // ...and compare against every collision-suffixed variant too.
            // Checking only the base name is how a third copy of an adoption
            // that already lost the base name became a live "#3" instance and
            // ran the same entry chain a third time.
            var n = 2, variant;
            while ((variant = byName[baseName + '#' + n])) {
                if (configsEquivalent(variant.declaredCfg, declared)) return variant.handle;
                n++;
            }
            name = baseName + '#' + n;
            safe(function () {
                console.warn(LOG, 'instance name "' + baseName + '" already registered with a different ' +
                    'config; registering this one as "' + name + '". Give each adoption a distinct ' +
                    'data-name (or versionUrl) to silence this.');
            });
        }

        // Keyed window config, looked up under the instance's FINAL name. A
        // suffixed instance falls back to the BASE-name entry only when no
        // entry exists under its own "#N" key, which keeps the ordinary
        // "same plugin adopted twice" case configurable from one entry while
        // still letting an author address the second instance precisely.
        //
        // `name` is excluded from the merge — and the final name is NOT
        // re-derived afterwards, so a keyed entry that supplies its own
        // versionUrl cannot rename the instance out from under the key it was
        // found under (which used to break get(name) silently).
        declared.name = name;
        var keyedConfigKey = null;
        safe(function () {
            var all = window.JellyfinRefreshKitConfigs;
            if (!all || typeof all !== 'object') return;
            var entry = all[name];
            var usedKey = name;
            if (!(entry && typeof entry === 'object') && name !== baseName) {
                entry = all[baseName];
                usedKey = baseName;
            }
            if (!(entry && typeof entry === 'object')) return;
            keyedConfigKey = usedKey;
            for (var k in entry) {
                if (Object.prototype.hasOwnProperty.call(entry, k) && k !== 'name') merged[k] = entry[k];
            }
        });

        var cfg = normalizeConfig(merged);
        cfg.name = name;

        // Two instances must never load the same entry files into one document.
        // The browser serves the second copy from cache at the identical ?v=
        // URL and RE-EXECUTES it: duplicate injectors, duplicate observers,
        // duplicate DOM. The instance still registers (its patterns and version
        // are useful); only the entry chain is suppressed.
        var entriesSuppressed = false;
        var entryOwner = null;
        if (cfg.entryScripts.length) {
            for (var e = 0; e < registry.length; e++) {
                if (sameEntryList(registry[e].cfg.entryScripts, cfg.entryScripts)) {
                    entriesSuppressed = true;
                    entryOwner = registry[e].name;
                    break;
                }
            }
        }
        if (entriesSuppressed) {
            safe(function () {
                console.warn(LOG, 'instance "' + name + '" declares the same entryScripts as already-' +
                    'registered instance "' + entryOwner + '"; loading them twice would re-execute the ' +
                    'same files. Registering WITHOUT the entry chain (versioning and update detection ' +
                    'still apply).');
            });
        }

        var sourceVersion = String(sourceKitVersion || 'unknown');
        var inst = createInstance(name, cfg, sourceVersion, entriesSuppressed);
        // The config AS DECLARED by the tag, i.e. before any keyed entry was
        // merged. This — not the effective config — is what a later duplicate
        // registration is compared against (see the dedupe block above).
        inst.declaredCfg = declared;
        /** @type {string|null} Which JellyfinRefreshKitConfigs key configured this instance. */
        inst.keyedConfigKey = keyedConfigKey;
        /** @type {boolean} Registered without a declared/derivable name (see above). */
        inst.anonymous = anonymous;
        registry.push(inst);
        byName[name] = inst;
        if (anonymous) anonymousCount++;
        safe(function () {
            console.log(LOG, 'instance registered: "' + name + '" (kit ' + sourceVersion +
                ', manager ' + KIT_VERSION + ', ' + registry.length + ' total)' +
                (keyedConfigKey ? ', configured by JellyfinRefreshKitConfigs["' + keyedConfigKey + '"]' : ''));
        });
        // A key this registration consumed is not a late/dead key, even if an
        // earlier audit pass said so before this copy existed.
        if (keyedConfigKey && warnedLateKeys[keyedConfigKey]) delete warnedLateKeys[keyedConfigKey];
        scheduleKeyedConfigAudit();
        inst.start();
        return inst.handle;
    }

    /** @type {boolean} True once the DOMContentLoaded arm has been registered. */
    var keyedAuditDomArmed = false;
    /** @type {number|null} setTimeout handle for the keyed-config audit settle. */
    var keyedAuditTimer = null;
    /**
     * How long after the LATEST registration the keyed-config audit waits
     * before judging a key. Long enough for a second kit copy's <script> to
     * finish downloading and register — the audit's verdicts are latched per
     * key, so judging early is judging wrong forever.
     * @type {number}
     */
    var KEYED_AUDIT_SETTLE_MS = 2000;
    /** @type {Object<string, boolean>} Keys already reported as too late. */
    var warnedLateKeys = Object.create(null);

    /**
     * Warn about keyed entries that arrived TOO LATE to be applied.
     *
     * window.JellyfinRefreshKitConfigs is read synchronously, by each kit tag,
     * at that tag's own position in the document — so an entry defined BELOW
     * the kit tags is never consulted. Nothing about the keyed form hints at
     * that (it is name-addressed, which reads as position-independent), and the
     * silent failure mode is nasty: an entry meant to force `mode: 'notify'`
     * does nothing and the tab hard-reloads under the user instead.
     *
     * So once the document has parsed AND registrations have settled (see
     * scheduleKeyedConfigAudit), compare the keys against the instances that
     * registered: a key naming a live instance that did NOT consume it can only
     * mean the entry was defined after that instance's tag.
     *
     * A key matching NO instance at all is reported too (2.1.2). It used to be
     * skipped silently, which made the audit blind to exactly the misconfigured
     * shape it exists for: a typo'd key, or an `instance-<N>` key whose number
     * no longer names the anonymous adoption the author meant. The instance the
     * entry was written for then runs on pure defaults — for a `versionUrl`-less
     * adoption, completely inert — behind no signal at all. The warning lists
     * the live instance names, because the fix is always "use one of these".
     */
    function auditKeyedConfigs() {
        // Handed off: this copy's registry is empty by design, so every key
        // would look like it "matched NO registered instance". The manager that
        // took over inherited the audit — and the keys already judged with it.
        if (handedOff) return;
        safe(function () {
            var all = window.JellyfinRefreshKitConfigs;
            if (!all || typeof all !== 'object') return;
            for (var key in all) {
                if (!Object.prototype.hasOwnProperty.call(all, key)) continue;
                if (warnedLateKeys[key]) continue;
                var inst = byName[key];
                if (!inst) {
                    // Not just "no instance under that name": no instance
                    // consumed the key under any name either (a "#2" instance
                    // may have fallen back to the base-name entry).
                    var consumed = false;
                    for (var c = 0; c < registry.length; c++) {
                        if (registry[c].keyedConfigKey === key) { consumed = true; break; }
                    }
                    if (consumed) continue;
                    warnedLateKeys[key] = true;
                    safe(function (k) {
                        return function () {
                            console.warn(LOG, 'window.JellyfinRefreshKitConfigs["' + k + '"] matched NO ' +
                                'registered instance and was never applied. Registered instance names: ' +
                                (registry.length
                                    ? registry.map(function (r) { return '"' + r.name + '"'; }).join(', ')
                                    : '(none)') + '. Keys are matched against an instance\'s FINAL name — ' +
                                'data-name, else the versionUrl\'s parent folder, else "instance-<N>" ' +
                                'numbered across anonymous adoptions only, plus any "#2" collision suffix.');
                        };
                    }(key));
                    continue;
                }
                if (inst.keyedConfigKey === key) continue;
                warnedLateKeys[key] = true;
                safe(function (k) {
                    return function () {
                        console.warn(LOG, 'window.JellyfinRefreshKitConfigs["' + k + '"] was NOT applied: ' +
                            'instance "' + k + '" had already registered by the time the entry existed. ' +
                            'The keyed config is read synchronously by each kit <script> tag at its own ' +
                            'position, so it must be defined BEFORE every kit tag — put the inline ' +
                            'script above them.');
                    };
                }(key));
            }
        });
    }

    /**
     * (Re-)arm the keyed-config audit, on EVERY registration.
     *
     * It used to be a one-shot armed by the FIRST registration, on
     * setTimeout(0) whenever the document was past 'loading' — and that is the
     * normal case, not an exotic one: RefreshKit.cs emits every tag with
     * `defer`, and deferred scripts run at readyState 'interactive'. So on a
     * page with two kit copies the audit ran after the first copy registered
     * and before the second copy's <script> had finished downloading, declared
     * the second plugin's perfectly good key "matched NO registered instance",
     * and latched that verdict forever. The operator then "fixes" a working
     * config.
     *
     * So: each registration pushes the audit out by a settle window, and the
     * audit itself re-tests consumption at the moment it runs — a key consumed
     * by a copy that registered since the arm is simply skipped.
     */
    function scheduleKeyedConfigAudit() {
        safe(function () {
            if (document.readyState === 'loading') {
                // Still parsing: more kit tags may be below us in the document.
                // Start the settle window at DOMContentLoaded instead, and let
                // any registration after that re-arm it directly.
                if (keyedAuditDomArmed) return;
                keyedAuditDomArmed = true;
                document.addEventListener('DOMContentLoaded', function () {
                    safe(armKeyedConfigAudit);
                }, false);
                return;
            }
            armKeyedConfigAudit();
        });
    }

    /** Debounce the audit to KEYED_AUDIT_SETTLE_MS after the latest arm. */
    function armKeyedConfigAudit() {
        if (keyedAuditTimer !== null) { clearTimeout(keyedAuditTimer); keyedAuditTimer = null; }
        // A DOMContentLoaded arm registered before a handoff still fires after
        // it; a retired manager must hold no timers.
        if (handedOff) return;
        keyedAuditTimer = setTimeout(function () {
            keyedAuditTimer = null;
            safe(auditKeyedConfigs);
        }, KEYED_AUDIT_SETTLE_MS);
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Manager handoff (REGISTRATION CONTRACT clause 7) — the newest-wins rule
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * One registered instance, packed for the manager taking the page over.
     *
     * TWO configs travel, and the difference matters:
     *   • `effectiveConfig` is what the instance is actually RUNNING (tag
     *     attributes + singular global + keyed entry, normalized). The new
     *     manager re-normalizes it under ITS OWN rules — that is the point of
     *     newest-wins: a clamp or a validation the newer copy tightened governs
     *     the transferred instances too, not just the ones registered after it.
     *   • `declaredConfig` is the adoption AS ITS TAG DECLARED IT, which is
     *     what a later duplicate registration is compared against
     *     (configsEquivalent). Losing it would break dedupe across a handoff:
     *     the same adoption's second tag would register a "#2" and re-run the
     *     collection's entry chain.
     * @param {Object} r Internal instance record.
     * @returns {Object}
     */
    function instanceTransferRecord(r) {
        return {
            name: r.name,
            anonymous: r.anonymous === true,
            keyedConfigKey: r.keyedConfigKey || null,
            registeredByKitVersion: r.sourceKitVersion,
            entriesSuppressed: r.entriesSuppressed === true,
            declaredConfig: r.declaredCfg,
            effectiveConfig: r.cfg,
            state: safe(function () { return r.transferState(); }, null) || null
        };
    }

    /**
     * HAND THE PAGE OVER to a newer kit copy, and become an inert delegate.
     *
     * The whole newest-wins rule reduces to this method. It must be LOSSLESS —
     * the page keeps running while the swap happens, and a user must not be
     * able to tell it occurred — so it does four things in one synchronous
     * step, with no window in between:
     *
     *   1. STOPS this copy. Every instance is deactivated (timers cancelled,
     *      entry points latched off, so a version request already in flight
     *      cannot act on a manager that no longer exists), the shared retry
     *      ladder and keyed-config audit are cancelled, the reload-survival
     *      watchdog is cancelled — its re-arming is the new manager's job —
     *      and every page listener is removed.
     *   2. NEUTRALIZES the interception layer. The createElement wrapper CANNOT
     *      be uninstalled (see installCreateElementHook): third-party code may
     *      already hold a reference to it, and restoring the native function
     *      would strip whatever the new manager installs. So it flips to a
     *      permanent inert-delegate mode instead: brand-new elements are left
     *      entirely to the new manager's wrapper, while elements this copy
     *      already handed out keep their accessors and delegate the versioning
     *      DECISION to the current manager (versionUrlForPage).
     *   3. RETURNS everything the new manager needs to continue: every instance
     *      with its live state, and the shared page state that exists only in
     *      memory. Note what is NOT here: the reload budget and the per-tab
     *      flip/left/recovery records already live in session/localStorage
     *      under page-wide keys, so they survive a handoff (and a reload) by
     *      themselves; and the singular-global claim is recorded either as a
     *      non-enumerable marker ON the config object or in a WeakSet held on
     *      `window`, both of which are page-level and equally unaffected.
     *   4. Points this copy at the new manager, permanently.
     *
     * NEVER THROWS, and returns null when it declines. A null return is the
     * caller's signal to register as an ordinary instance instead — a failed
     * handoff must never leave the page with no manager, and this copy has not
     * changed anything until the point of no return below.
     *
     * @param {Object} newManager The arriving copy's manager API object.
     * @returns {Object|null} The transfer record, or null when declined.
     */
    function handoffTo(newManager) {
        if (!newManager || typeof newManager.__registerInstance !== 'function') return null;
        // Handing off to ourselves would retire the page's only manager.
        if (newManager === api) return null;

        if (handedOff) {
            // CHAINED HANDOFF (old → new → newer). This copy is already inert,
            // and the caller reached it only because the global still points
            // here; the live manager is at the end of the chain. Forward, then
            // re-point straight at the newest so the chain stays FLAT — a
            // document with N kit copies must not build an N-deep forwarding
            // chain that every later contract call walks.
            var chained = safe(function () {
                return (delegate && typeof delegate.__handoffTo === 'function')
                    ? delegate.__handoffTo(newManager)
                    : null;
            }, null) || null;
            if (chained) delegate = newManager;
            return chained;
        }

        var payload = {
            contractVersion: CONTRACT_VERSION,
            kitVersion: KIT_VERSION,
            // Chain depth and lineage, so the newest manager can report the
            // whole sequence of copies that ran this page before it.
            handoffs: handoffsReceived,
            lineage: inheritedFrom.concat([KIT_VERSION]),
            anonymousCount: anonymousCount,
            instances: registry.map(instanceTransferRecord),
            lateKeys: (function () {
                var out = {}, k;
                for (k in warnedLateKeys) {
                    if (Object.prototype.hasOwnProperty.call(warnedLateKeys, k)) out[k] = true;
                }
                return out;
            })(),
            shared: {
                lastInteractionAt: lastInteractionAt,
                blockedRetries: blockedRetries,
                lastBlockReason: lastBlockReason,
                warnedOverlap: warnedOverlap,
                warnedBudgetRefusal: warnedBudgetRefusal,
                warnedReloadSurvived: warnedReloadSurvived,
                warnedMediaStarvation: warnedMediaStarvation,
                mediaBlockSince: mediaBlockSince,
                mediaBlockSignature: mediaBlockSignature,
                // THE RELOAD LATCH. A handoff can land in the window between
                // location.reload() and the new document committing. The latch
                // has to travel or the new manager would pass every gate again
                // and spend a second budget slot on a navigation that is
                // already in flight — and the disarmed set has to travel with
                // it so a navigation the host blocks can still be recovered.
                reloadCommitted: reloadCommitted,
                reloadsSurvived: reloadsSurvived,
                reloadRecordsWritten: reloadRecordsWritten.slice(),
                reloadDisarmed: (function () {
                    var names = [];
                    for (var i = 0; i < reloadDisarmed.length; i++) {
                        if (names.indexOf(reloadDisarmed[i].name) === -1) names.push(reloadDisarmed[i].name);
                    }
                    return names;
                })()
            }
        };

        // ── Point of no return: this copy stops owning the page here. ────────
        handedOff = true;
        interceptorInert = true;

        var i;
        for (i = 0; i < registry.length; i++) {
            safe(function (r) { return function () { r.deactivate(); }; }(registry[i]));
        }
        clearRetry();
        if (keyedAuditTimer !== null) { clearTimeout(keyedAuditTimer); keyedAuditTimer = null; }
        if (reloadWatchdogTimer !== null) { clearTimeout(reloadWatchdogTimer); reloadWatchdogTimer = null; }
        safe(removePageListeners);

        registry = [];
        byName = Object.create(null);
        reloadDisarmed = [];
        reloadRecordsWritten = [];
        delegate = newManager;
        return payload;
    }

    /**
     * Register ONE instance that came across a handoff.
     *
     * Deliberately NOT registerInstance(): every merge decision this instance
     * was subject to has already been taken, once, by the manager it is coming
     * from, and re-taking them would be wrong rather than merely wasteful —
     *   • the singular window config has been CLAIMED (once per page, by
     *     design), so re-offering it would warn "already claimed" about this
     *     instance's own config;
     *   • the keyed entry is already merged into `effectiveConfig`; re-reading
     *     it would be harmless but re-deriving the name from it is not;
     *   • the name is FINAL, including any "#N" collision suffix, and must not
     *     be re-derived — a transferred instance keeps its identity or the
     *     per-tab flip records, the keyed config key and get(name) all break;
     *   • the entryScripts duplicate scan already ran, and its verdict travels
     *     as `entriesSuppressed`; running it again against the transferred
     *     siblings would suppress the ORIGINAL owner's chain instead.
     * @param {Object} rec One entry of a transfer record's `instances`.
     * @returns {Object|null} The internal instance record.
     */
    function adoptTransferredInstance(rec) {
        if (!rec || typeof rec !== 'object') return null;
        if (typeof rec.name !== 'string' || !rec.name) return null;
        // Defensive: never adopt one name twice (a malformed transfer record).
        if (byName[rec.name]) return byName[rec.name];

        var cfg = normalizeConfig(rec.effectiveConfig);
        cfg.name = rec.name;
        var declared = normalizeConfig(rec.declaredConfig);
        declared.name = rec.name;

        var inst = createInstance(rec.name, cfg, String(rec.registeredByKitVersion || 'unknown'),
            rec.entriesSuppressed === true, rec.state || {});
        inst.declaredCfg = declared;
        inst.keyedConfigKey = rec.keyedConfigKey || null;
        inst.anonymous = rec.anonymous === true;
        registry.push(inst);
        byName[rec.name] = inst;
        inst.start();
        return inst;
    }

    /**
     * Adopt a transfer record: become the manager of everything the previous
     * copy was running. Called exactly once, from the boot section, and only
     * after the interceptor and listeners of THIS copy are installed — an
     * instance is resumed into a running engine, never into a half-built one.
     * @param {Object} t A transfer record from handoffTo().
     * @returns {boolean} True when the page was taken over.
     */
    function applyHandoffTransfer(t) {
        if (!t || typeof t !== 'object') return false;

        var adopted = [];
        var list = Array.isArray(t.instances) ? t.instances : [];
        var i;
        for (i = 0; i < list.length; i++) {
            var inst = safe(function (rec) {
                return function () { return adoptTransferredInstance(rec); };
            }(list[i]), null);
            if (inst) adopted.push(inst.name);
        }

        // Anonymous numbering is a page-level sequence ("instance-<N>" is the
        // only handle such an adoption has); continue it rather than restart it.
        var count = Number(t.anonymousCount);
        if (isFinite(count) && count > anonymousCount) anonymousCount = count;

        var s = (t.shared && typeof t.shared === 'object') ? t.shared : {};
        if (typeof s.lastInteractionAt === 'number' && isFinite(s.lastInteractionAt)) {
            lastInteractionAt = s.lastInteractionAt;
        }
        if (typeof s.blockedRetries === 'number' && isFinite(s.blockedRetries)) blockedRetries = s.blockedRetries;
        if (typeof s.lastBlockReason === 'string') lastBlockReason = s.lastBlockReason;
        // One-shot warning latches: the page has already been told these things
        // once. "Warned once" is a promise to the operator reading the console,
        // not a per-copy allowance.
        warnedOverlap = s.warnedOverlap === true;
        warnedBudgetRefusal = s.warnedBudgetRefusal === true;
        warnedReloadSurvived = s.warnedReloadSurvived === true;
        warnedMediaStarvation = s.warnedMediaStarvation === true;
        if (typeof s.mediaBlockSince === 'number') mediaBlockSince = s.mediaBlockSince;
        if (typeof s.mediaBlockSignature === 'string') mediaBlockSignature = s.mediaBlockSignature;
        if (typeof s.reloadsSurvived === 'number' && isFinite(s.reloadsSurvived)) {
            reloadsSurvived = s.reloadsSurvived;
        }
        if (t.lateKeys && typeof t.lateKeys === 'object') {
            for (var k in t.lateKeys) {
                if (Object.prototype.hasOwnProperty.call(t.lateKeys, k)) warnedLateKeys[k] = true;
            }
        }
        handoffsReceived = (typeof t.handoffs === 'number' && isFinite(t.handoffs) ? t.handoffs : 0) + 1;
        inheritedFrom = Array.isArray(t.lineage) ? t.lineage.slice() : [String(t.kitVersion || 'unknown')];

        // A COMMITTED RELOAD IN FLIGHT. Inherit the latch (so this manager
        // cannot fire a second navigation for the one already under way), the
        // records it wrote (so a navigation that never lands can retract them)
        // and the instances it disarmed (so they can be re-armed) — then re-arm
        // the survival watchdog, because the one that was watching this
        // navigation was cancelled as part of the handoff. This manager must
        // NOT call location.reload() again: that is the double-fire.
        if (s.reloadCommitted === true) {
            reloadCommitted = true;
            if (Array.isArray(s.reloadRecordsWritten)) {
                for (i = 0; i < s.reloadRecordsWritten.length; i++) {
                    var rec2 = s.reloadRecordsWritten[i];
                    if (Array.isArray(rec2) && rec2.length === 2) reloadRecordsWritten.push(rec2);
                }
            }
            if (Array.isArray(s.reloadDisarmed)) {
                for (i = 0; i < s.reloadDisarmed.length; i++) {
                    var disarmed = byName[s.reloadDisarmed[i]];
                    if (disarmed && reloadDisarmed.indexOf(disarmed) === -1) reloadDisarmed.push(disarmed);
                }
            }
            armReloadSurvivalWatchdog();
        }

        safe(function () {
            console.log(LOG, 'manager HANDOFF: kit ' + (t.kitVersion || 'unknown') + ' → ' + KIT_VERSION +
                ' (newest wins, registration contract ' + CONTRACT_VERSION + '). Took over ' +
                adopted.length + ' instance' + (adopted.length === 1 ? '' : 's') +
                (adopted.length ? ' [' + adopted.join(', ') + ']' : '') +
                ' with their resolved versions, pending updates and entry progress' +
                (reloadCommitted ? ', INCLUDING a reload already committed' : '') +
                '. The previous manager is now an inert delegate; page-level reload semantics, ' +
                'safety gates and URL versioning are ' + KIT_VERSION + "'s from here.");
        });

        scheduleKeyedConfigAudit();
        // Anything that was waiting for a reload under the old engine is now
        // waiting under this one — give it a tick.
        if (pendingInstances().length > 0) safe(tryReload);
        return true;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Public API — the manager
    // ─────────────────────────────────────────────────────────────────────────

    /**
     * The manager that answers for this copy: null while this copy owns the
     * page, and the copy it handed off to once it does not.
     *
     * Everything on the public API consults this, because the object this
     * function belongs to STAYS REACHABLE FOREVER: window.JellyfinRefreshKit is
     * installed non-configurable by whichever copy got there first, so a
     * handoff can never re-point the global — the retired object has to answer
     * for the live manager instead. That is the inert-delegate half of the
     * newest-wins rule (REGISTRATION CONTRACT clause 7).
     * @returns {Object|null}
     */
    function forwardTo() {
        return handedOff ? delegate : null;
    }

    /** @returns {Object|null} First-registered instance (the 1.x delegate). */
    function firstInstance() {
        return registry.length > 0 ? registry[0] : null;
    }

    /**
     * The manager. Keeps the whole 1.x surface (version, latestVersion,
     * versionedUrl, checkNow, state) delegating to the sole instance when
     * exactly one exists; with 2+ instances the top-level getters follow a
     * documented rule — version/latestVersion report the FIRST-REGISTERED
     * instance, versionedUrl uses the page-level matcher (all instances),
     * checkNow fans out to every instance. Prefer the explicit surface:
     * get(name) / instances() / state().
     *
     * Frozen: __registerInstance is the cross-version compatibility promise and
     * must not be reassignable by anyone, including a later kit copy.
     *
     * EVERY member forwards to `forwardTo()` once this copy has handed the page
     * over, so this object keeps answering correctly forever — including
     * `kitVersion` and `__contractVersion`, which an arriving copy reads to
     * decide whether IT should take over. Those two are accessors, not values,
     * for exactly that reason: a stale kitVersion on the object the global
     * points at would make every later copy compare itself against a manager
     * that retired long ago (and a newer one would try to hand off to itself).
     */
    var api = Object.freeze({
        /** @returns {string} The CURRENT manager copy's kit version. */
        get kitVersion() {
            var d = forwardTo();
            return d ? (safe(function () { return String(d.kitVersion); }, KIT_VERSION) || KIT_VERSION)
                : KIT_VERSION;
        },

        /** @returns {number} REGISTRATION CONTRACT revision the CURRENT manager speaks. */
        get __contractVersion() {
            var d = forwardTo();
            return d ? (safe(function () { return Number(d.__contractVersion); }, CONTRACT_VERSION) || CONTRACT_VERSION)
                : CONTRACT_VERSION;
        },

        /**
         * REGISTRATION CONTRACT clause 7 (additive, contract revision 3) — hand
         * the page over to a STRICTLY NEWER kit copy.
         *
         * The newest-wins rule: an arriving copy that is newer than the sitting
         * manager and finds `__contractVersion >= 3` calls this, becomes the
         * manager, and re-registers everything the returned record describes.
         * The caller must be prepared for null (this manager declined, or the
         * transfer could not be built) and register as an ordinary instance
         * instead — a page must never be left with no manager.
         *
         * Never throws. See handoffTo() for what "lossless" means here.
         * @param {Object} newManager The arriving copy's manager API object.
         * @returns {Object|null} Transfer record, or null when declined.
         */
        __handoffTo: function (newManager) {
            return safe(function () { return handoffTo(newManager); }, null) || null;
        },

        /**
         * REGISTRATION CONTRACT clause 3 — the frozen, forward-stable entry
         * point every later-loaded kit copy calls instead of installing its own
         * machinery. Never throws; returns the instance handle or null.
         * @param {Object} config Plain object of tag-level options.
         * @param {string} kitVersion The registering copy's KIT_VERSION.
         * @returns {Object|null}
         */
        __registerInstance: function (config, kitVersion) {
            var d = forwardTo();
            if (d) return safe(function () { return d.__registerInstance(config, kitVersion); }, null) || null;
            return safe(function () { return registerInstance(config, kitVersion); }, null) || null;
        },

        /**
         * REGISTRATION CONTRACT clause 6 (additive, contract revision 2) — the
         * page-level, once-only claim on a singular window config OBJECT.
         *
         * A copy normally claims by writing a non-enumerable marker on the
         * object itself, which needs no manager at all. This exists for the
         * objects that cannot carry one — frozen, sealed, otherwise
         * non-extensible, exotic proxies — which are tracked by IDENTITY in a
         * page-level WeakSet instead. Before 2.2.0 such an object fell back to
         * "has any kit copy already run?", which refused an adopter's OWN
         * frozen config on every page but the first.
         *
         * Idempotent per object: the FIRST caller gets true, every later caller
         * gets false. Never throws.
         *
         * This is the ONE member that does NOT forward after a handoff, and
         * deliberately: the claim it records is page-level either way (a marker
         * on the object, or a WeakSet held on `window`), so a retired copy
         * answers it exactly as correctly as the live manager — while
         * forwarding would build a cycle, because claimSingularGlobal()'s own
         * last-resort branch asks whatever `window.JellyfinRefreshKit` holds,
         * which after a handoff is this very object.
         * @param {Object} obj The singular config object being claimed.
         * @returns {boolean} True when THIS caller now holds the claim.
         */
        __claimSingularGlobal: function (obj) {
            return safe(function () {
                if (!obj || typeof obj !== 'object') return false;
                return claimSingularGlobal(obj);
            }, false) === true;
        },

        /** @returns {string|null} Sole/first instance's running version (1.x compat). */
        get version() {
            var d = forwardTo();
            if (d) return safe(function () { return d.version; }, null) || null;
            var f = firstInstance();
            return f ? f.getBaselineVersion() : null;
        },

        /** @returns {string|null} Sole/first instance's newest seen version (1.x compat). */
        get latestVersion() {
            var d = forwardTo();
            if (d) return safe(function () { return d.latestVersion; }, null) || null;
            var f = firstInstance();
            return f ? f.getLatestVersion() : null;
        },

        /**
         * Explicitly version a URL. Useful for code that builds asset URLs
         * outside of createElement (fetch(), import(), CSS url()).
         * Without `force`: the page-level matcher (all instances' patterns,
         * first-registered match wins) — identical to what the interceptor does.
         * With `force`: the FIRST-REGISTERED instance's version is applied
         * regardless of patterns (1.x compat; prefer get(name).versionedUrl).
         * @param {string} url
         * @param {boolean} [force]
         * @returns {string}
         */
        versionedUrl: function (url, force) {
            var d = forwardTo();
            if (d) return safe(function () { return d.versionedUrl(url, force); }, url);
            return safe(function () {
                if (force) {
                    var f = firstInstance();
                    return f ? f.versionedUrl(url, true) : url;
                }
                return versionUrlForPage(url);
            }, url);
        },

        /**
         * Force an immediate version check on EVERY instance, bypassing the
         * min-gap floor.
         * @returns {Promise<void>}
         */
        checkNow: function () {
            var d = forwardTo();
            if (d) return safe(function () { return d.checkNow(); }, Promise.resolve());
            return safe(function () {
                return Promise.all(registry.map(function (inst) {
                    return safe(function () { return inst.poll(true); }, Promise.resolve());
                })).then(function () { return undefined; });
            }, Promise.resolve());
        },

        /**
         * @param {string} name
         * @returns {Object|null} The named instance's handle, or null.
         */
        get: function (name) {
            var d = forwardTo();
            if (d) return safe(function () { return d.get(name); }, null) || null;
            return safe(function () {
                var inst = byName[name];
                return inst ? inst.handle : null;
            }, null) || null;
        },

        /** @returns {string[]} Registered instance names, in registration order. */
        instances: function () {
            var d = forwardTo();
            if (d) return safe(function () { return d.instances(); }, []) || [];
            return safe(function () {
                return registry.map(function (inst) { return inst.name; });
            }, []) || [];
        },

        /**
         * Aggregate diagnostic snapshot. With exactly one instance the top
         * level is field-compatible with 1.1.0's state() (same names, same
         * semantics) — it just GAINS instanceCount / instances / interceptor /
         * shared. With 2+ instances the top-level scalar fields describe the
         * FIRST-REGISTERED instance; per-instance truth is in .instances.
         * @returns {Object}
         */
        state: function () {
            var d = forwardTo();
            if (d) return safe(function () { return d.state(); }, { kitVersion: KIT_VERSION });
            return safe(function () {
                var f = firstInstance();
                var out = f ? f.state() : { kitVersion: KIT_VERSION };
                out.contractVersion = CONTRACT_VERSION;
                out.instanceCount = registry.length;
                out.instances = {};
                for (var i = 0; i < registry.length; i++) {
                    out.instances[registry[i].name] = registry[i].state();
                }
                out.interceptorInstalled = interceptorInstalled;
                out.interceptorCount = interceptorInstalled ? 1 : 0;
                var pending = pendingInstances();
                // Every field describing the reload the engine is trying to
                // perform is gated on there BEING one. A snapshot that names an
                // idle window and the instance imposing it while nothing is
                // pending sends support after a constraint that does not exist
                // — and named a `mode: 'off'` sibling as its author before
                // 2.2.0. Same rule as blockReason since 2.1.0.
                var hasPending = pending.length > 0;
                out.shared = {
                    pendingInstances: pending.map(function (p) { return p.name; }),
                    blockReason: hasPending ? blockReasonFor(effectiveIdleWindowMs(pending)) : null,
                    lastBlockReason: lastBlockReason,
                    msSinceInteraction: Date.now() - lastInteractionAt,
                    effectiveIdleWindowMs: hasPending ? effectiveIdleWindowMs(pending) : null,
                    effectiveIdleWindowFrom: hasPending
                        ? ((strictestIdleInstance(pending) || {}).name || null)
                        : null,
                    reloadCommitted: reloadCommitted,
                    reloadsSurvived: reloadsSurvived,
                    blockedRetries: blockedRetries,
                    // MANAGER LINEAGE. 0 handoffs is the ordinary page (one kit
                    // copy, or several of the same version). Anything higher
                    // means an older copy parsed first and a newer one took the
                    // page over — `managerLineage` lists the copies in the
                    // order they ran it, oldest first, this one last.
                    managerHandoffs: handoffsReceived,
                    managerLineage: inheritedFrom.concat([KIT_VERSION]),
                    // How long a media element has held the gate with zero
                    // playback progress. At MEDIA_STARVATION_MS the parked-media
                    // starvation escape fires.
                    mediaBlockedForMs: mediaBlockedForMs(),
                    effectiveReloadBudget: effectiveReloadBudget(),
                    budgetKey: BUDGET_KEY,
                    budgetWindowMs: BUDGET_WINDOW_MS
                };
                return out;
            }, { kitVersion: KIT_VERSION });
        }
    });

    // ─────────────────────────────────────────────────────────────────────────
    // Boot — manager path
    // ─────────────────────────────────────────────────────────────────────────

    // NEWEST-WINS TAKEOVER (REGISTRATION CONTRACT clause 2b/7). Ask the sitting
    // manager to hand the page over BEFORE anything of this copy is installed:
    // the handoff is what makes its createElement wrapper inert, and installing
    // ours on top of a still-active wrapper would version every URL twice.
    // Both steps run in the same synchronous turn, so no asset can be created
    // in between.
    //
    // A REFUSED handoff is not fatal and not even unusual (a manager that has
    // itself just been retired, an exotic failure inside it): fall back to the
    // pre-2.3.0 behaviour — register with it as an ordinary instance and stop.
    // Nothing of this copy has been installed at this point, so that fallback
    // is exactly the clause-2b path, taken a few microseconds later.
    /** @type {Object|null} */
    var handoffTransfer = null;
    if (handoffFrom) {
        handoffTransfer = safe(function () { return handoffFrom.__handoffTo(api); }, null) || null;
        if (!handoffTransfer) {
            safe(function () {
                console.warn(LOG, 'this ' + KIT_VERSION + ' copy is newer than the manager on this page ' +
                    'but the handoff was declined, so the older copy keeps the page. Registering as an ' +
                    'instance instead; page-level reload semantics are the older copy\'s.');
            });
            safe(function () { handoffFrom.__registerInstance(ownConfig, KIT_VERSION); });
            return;
        }
    }

    // Install the createElement hook FIRST and synchronously. Any sub-script the
    // host bootstrap creates before this point is unversioned forever, so the
    // kit's <script> tag must come before the bootstrap's in the document.
    safe(installCreateElementHook);

    safe(function () {
        // Capture phase, passive: we only observe. Capture means we see the event
        // even if a handler below calls stopPropagation().
        var opts = { capture: true, passive: true };
        var discrete = ['pointerdown', 'keydown', 'click', 'input', 'change'];
        var continuous = ['pointermove', 'wheel', 'scroll', 'touchmove'];
        // ONE named function per wake source, registered through
        // addPageListener, so a handoff can remove exactly what it added.
        var wakeListener = function () { safe(onWake); };
        var i;
        for (i = 0; i < discrete.length; i++) {
            addPageListener(document, discrete[i], onDiscreteInteraction, opts);
        }
        for (i = 0; i < continuous.length; i++) {
            addPageListener(document, continuous[i], onContinuousInteraction, opts);
        }
        addPageListener(document, 'visibilitychange', wakeListener, false);
        addPageListener(window, 'focus', wakeListener, false);
        addPageListener(window, 'pageshow', wakeListener, false);
    });

    /**
     * Publish this manager on `window`, unless the slot is already permanently
     * owned.
     *
     * REGISTRATION CONTRACT clause 1 makes "the first copy installs
     * window.JellyfinRefreshKit" a load-bearing invariant, so it goes in
     * NON-CONFIGURABLE: writable:false alone only blocks plain assignment, and
     * a configurable property can still be replaced wholesale by
     * defineProperty — which is precisely what an older 1.x copy loading second
     * does. That used to strip __registerInstance off the page while this
     * manager's registry, timers and interceptor kept running invisibly, so
     * every LATER copy went inert blaming a "pre-2.0 singleton" that wasn't
     * there.
     *
     * The consequence, since 2.3.0, is that a HANDOFF cannot re-point the
     * global: the first manager's object owns that slot for the life of the
     * document. It does not need to be re-pointed — that object is an inert
     * delegate now and forwards the entire contract surface here (clause 7) —
     * so the right thing is to leave the slot alone rather than call
     * defineProperty just to have it throw into safe() on every takeover.
     * Checking first also keeps the console clean when an unrelated plugin owns
     * the name.
     * @param {string} prop
     * @param {boolean} enumerable
     */
    function publishManagerGlobal(prop, enumerable) {
        safe(function () {
            var existing = Object.getOwnPropertyDescriptor(window, prop);
            if (existing && !existing.configurable) return;
            Object.defineProperty(window, prop, {
                value: api, writable: false, configurable: false, enumerable: enumerable
            });
        });
    }

    publishManagerGlobal('JellyfinRefreshKit', true);

    // Belt and braces for the case the line above could not win (an unrelated
    // plugin got there first with its own non-configurable property, a handoff
    // left the slot with the first manager, or a future engine quirk): a
    // non-enumerable backup handle that a later copy's role decision consults
    // before concluding the page belongs to a 1.x singleton. Non-enumerable so
    // it stays out of for-in / Object.keys sweeps over window, which some
    // plugins do.
    publishManagerGlobal('__jellyfinRefreshKitManager', false);

    // Adopt everything the previous manager was running, BEFORE this copy's own
    // tag registers: the transferred instances were on the page first, and
    // registration order is what versionUrlForPage arbitrates ambiguous asset
    // URLs by. It is also what makes this copy's own tag dedupe against a
    // transferred instance when they are the same adoption.
    if (handoffTransfer) safe(function () { applyHandoffTransfer(handoffTransfer); });

    // Finally: register THIS copy's own instance from its tag config, through
    // exactly the same contract path a later copy would use.
    safe(function () { api.__registerInstance(ownConfig, KIT_VERSION); });
})();
