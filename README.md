# jellyfin-refresh-kit

One file per plugin type. Zero dependencies. No build step. Drop the file that
matches your plugin in and users stop running last week's code.

```
jellyfin-refresh-kit.js   3453 lines — vanilla JS for script collections and any
                          plugin's client side (the file doubles as its own docs)
RefreshKit.cs             2090 lines — the server companion for C# plugins:
                          revalidating index.html injection middleware, build
                          identity, version endpoint, cache-correct script serving
```

- **Script collection (KefinTweaks-style, no server code)?** → `jellyfin-refresh-kit.js`, ideally in bootstrap mode.
- **C# plugin?** → `RefreshKit.cs` for the shell + asset layers, and optionally the JS kit for open-tab convergence.
- **Several plugins each shipping the kit on one server?** → they compose: see **Multi-instance** below.

---

## The problem

You publish v1.2.0 of your script collection. Nobody gets it. Some users are on
v1.0.4 for a week. You tell them to press Ctrl+Shift+R. They don't.

There are **three independent layers** of staleness, and it matters a lot which
one you're actually fighting:

| # | Layer | What's stale | Fix |
|---|-------|--------------|-----|
| 1 | **The shell** — `index.html` and the `<script>` tag that bootstraps you | the entry point itself | **C# plugins: fully fixed by `RefreshKit.cs`** (request-time injection with a strong ETag, 304 revalidation, per-build tag URLs). **Script collections: minimized by the JS kit's bootstrap mode** — the shell references only the kit, a tiny stable loader; everything else is versioned. |
| 2 | **Sub-assets** — the scripts and stylesheets your bootstrap loads at runtime | every `.js` / `.css` your loader pulls in | **JS kit.** URLs are rewritten to `?v=<version>`. A new release is a new URL, so a stale cache entry is unreachable by construction. |
| 3 | **Open tabs** — the user who left Jellyfin open for two days | everything, because nothing is ever re-requested | **JS kit.** It polls a tiny version endpoint and safely auto-reloads the tab when the version changes. |

### What it honestly cannot do

- **It cannot beat a CDN's `@latest` resolution TTL.** jsDelivr caches the
  `@latest` → tag mapping for up to 24 hours. Adding `?v=1.2.0` does not help,
  because for the URL `.../KefinTweaks@latest/scripts/utils.js?v=1.2.0` the old
  file *is the correct response* — the CDN hasn't noticed the new tag yet.
  **Fix: pin an explicit version (`@1.2.0`) or self-host.** If you self-host out
  of `jellyfin-web/`, the kit solves the problem end to end.
- **The JS kit cannot add `ETag` / `Cache-Control` / `Last-Modified` headers.**
  That is a server concern — which is exactly what `RefreshKit.cs` does for C#
  plugins (see below).
- **Something must be the loader, and a loader cannot cache-bust itself.** In
  bootstrap mode the only unversioned file left is `jellyfin-refresh-kit.js`
  itself. Keep it stable and put volatile code in your entries; if you control
  the server, give that one path `Cache-Control: no-cache`.
- **In classic mode every page load races the version fetch — it is not a
  one-time cost.** The kit keeps no cross-load memory of the version: the
  baseline starts empty on every load and the version endpoint is fetched
  `no-store`, so load 200 is in exactly the state load 1 was. Any asset your
  bootstrap creates *synchronously at parse time*, before the fetch resolves,
  goes out unversioned on **every** load, and the browser keeps serving whatever
  it cached for that bare URL. Everything created after the version resolves is
  versioned — which is most of a real collection's surface, but that is a
  per-load property, not a cost you pay once. **Only bootstrap mode removes the
  race**, because there the kit decides when your entries load. (`bootVersion`
  also closes it, if your server can stamp the tag — see the config table.)
- **It only intercepts `document.createElement`.** Assets injected via
  `innerHTML`, `document.write`, `createElementNS`, or `import()` are not
  rewritten automatically. Use `JellyfinRefreshKit.versionedUrl(url)` explicitly
  for those.
- **Not every value assigned to `src`/`href` is rewritten.** Strings are, and so
  are the two wrappers that provably stringify to the same URL the browser would
  fetch — a `URL` instance (`s.src = new URL('scripts/a.js', base)`) and a boxed
  `String` — on both the property accessor and `setAttribute`. Anything else is
  passed through **untouched on purpose**: a `TrustedScriptURL` must reach the
  native setter as the trusted object it is (coercing it to a string under a
  `require-trusted-types-for 'script'` CSP throws and breaks the page), and an
  arbitrary object's `toString` is user code the kit will not run a second time.
  For those, call `versionedUrl(url)` yourself.
- **Multi-instance arbitration is by tag order.** When two instances'
  `assetPatterns` both match a URL, the first-registered (earlier tag) *that has
  resolved a version* wins — the kit warns once but cannot know which plugin you
  *meant*. And a **1.x kit copy that loads first** owns the page outright: 2.x
  copies go inert with one warning until the 1.x plugin upgrades its copy.
- **It cannot make an unstable version endpoint stable.** If several nodes
  behind a round-robin proxy report different build identities for the same
  release, the kit refuses to chase the oscillation (a candidate must be
  confirmed twice, and a tab **never auto-reloads to a version it has already
  reloaded away from** — which closes cycles of any length, not just the
  two-node case) — but it also cannot tell you which value is right. Serve
  **one identity per release across all nodes**.
- **It cannot force a host to honour `location.reload()`.** An embedded WebView
  or Electron shell may intercept the navigation, and a `beforeunload` confirm
  may be answered with "Stay". The kit notices — a 3s watchdog after every
  reload call treats a surviving document as a refused navigation, re-arms the
  pending update and retries — but it cannot navigate a host that will not.

---

## How it works

**Asset versioning.** The kit wraps `document.createElement`. When you ask for a
`script` or `link`, you get one back with a **per-instance** accessor installed
on `.src` / `.href` (and a wrapped `.setAttribute`). Assigning a URL that matches
your `assetPatterns` rewrites it to carry `?v=<version>` *before the browser ever
sees it*.

Two things this deliberately is **not**:

- **Not `Element.prototype` patching.** That changes behaviour for Jellyfin's own
  code and every other plugin on the page, and is effectively impossible to
  uninstall. Per-instance means only elements handed out by the kit's own wrapper
  are affected; anything built another way is untouched.
- **Not a `MutationObserver`.** An observer sees the element *after* insertion,
  by which point the browser has already started the fetch. Mutating `.src` then
  either does nothing or causes a second request. Assignment-time interception is
  the only reliably-early hook.

The accessor delegates to the element's real prototype getter/setter, so
`getAttribute`, URL resolution, and load behaviour stay exactly native.

**Update detection.** Poll a version endpoint (`cache: 'no-store'` *and* a
`?_=<timestamp>` buster — belt and braces, because the version endpoint is the
one request that must never be stale). The baseline — "this is the build this
tab is running" — comes from `bootVersion` when the document tells the kit which
build served it (`RefreshKit.cs` stamps `data-boot-version` on every tag), and
otherwise from the **first** version fetched. Seeding it from the document is
strictly better: an update that lands between page-serve and the first poll is
detected instead of being quietly absorbed into the baseline.

A change is only believed once it has been **seen twice in a row** (the kit
schedules the confirming fetch itself, ~1.5 s later, rather than making you wait
another poll interval). One observation is not evidence of a release — a version
source that alternates between two nodes' identities would otherwise reload the
tab once per poll forever, every reload comfortably inside its own budget
window. A tab also records, in `sessionStorage`, every version it has already
reloaded **away from**, and refuses to auto-reload back to any of them: going
back to a build this tab has already left is a flap, not a release. That closes
oscillations of **any length** — `A→B`, `B→C`, then `C→A` is refused — where a
pair-only rule (2.1.x) let a three-node source loop forever, each reload sitting
in its own budget window. It logs one line and keeps versioning URLs.
The confirming fetch is capped at **one per poll cycle**: if it comes back with
a *third* distinct identity, the kit stops chasing and waits for the ordinary
poll (one warning), so a source that never repeats itself cannot turn the
confirmation into a 1.5 s fetch loop.

**Safe reload.** In `auto` mode the kit reloads only when *every* gate passes:

- document is visible
- not on a `#/video` route, not fullscreen, not picture-in-picture
- no `<video>`/`<audio>` holding a real **session** — playing, or paused with a
  playback position, a played range, or an in-progress seek. A decorative
  element some plugin parks in the DOM with a `src` and `preload` set is *not* a
  session (nothing to protect at `currentTime 0` with an empty `played` list)
  and does not block
- no open dialog (`.dialog.opened`, `.actionSheet.opened`, `[role="dialog"]`,
  `[aria-modal="true"]`, ignoring ones parked inside `aria-hidden`/`hidden`)
- nothing focused that the user might be typing into
- user idle for at least `idleSeconds` (floored at 1s even when you configure 0 —
  reloading in the same task as a click steals that click)

Blocked? Re-check every second, bounded (~10 min so a tab parked on a video does
not tick at 1 Hz forever) — and after that cap the pending update is still live:
every user interaction, every tab refocus, and **every successful poll** re-tests
the gate directly and reloads the moment it clears. A hidden tab holds **zero
timers**, and so does an interaction: each discrete event supersedes the
previous one's settle timer rather than adding to it, so typing a query does not
queue one probe per keystroke.

**Ambient backdrop video does not block (2.3.0).** A `<video>` that is muted
(or at volume 0) **and** looping **and** has no controls is decoration, not a
session — that is exactly the full-bleed backdrop Media Bar and its forks put
behind the Home screen. Before 2.3.0 it blocked forever *and* defeated the
starvation escape below, because a looping video always shows fresh playback
progress: measured live, `blockedRetries` climbed 1 → 176 over 160s on `#/home`
with Media Bar installed, i.e. layer 3 was off for as long as the user stayed on
Home. All three conditions must hold — unmute it, give it controls or stop it
looping and it blocks exactly as before, Jellyfin's own player included — and an
ambient video that goes fullscreen is still caught by the `fullscreen_media`
gate. The deliberate false negative: someone who *chooses* to watch a muted,
looping, controls-less video can be reloaded under.

A media element that *is* a session but then **freezes** — paused and abandoned,
or stalled mid-buffer — cannot starve the reload forever either: once
`media_element` has held the gate for ~10 minutes of *zero* playback progress
anywhere on the page, the kit logs one line and re-tests with the media probe
suppressed. Every other gate still applies — including `playback_route`, which
is evaluated *first*, so a tab sitting on Jellyfin's own `#/video` route is
never reached by the escape at all. (`state().shared.mediaBlockedForMs` reports
the running total.)

An update the server **withdraws** disarms itself. If a reload is armed but
blocked and the endpoint then reports the version this tab is already running,
the kit logs one line, drops the pending intent and stands the retry machinery
down — a rolled-back deploy never reloads a tab for nothing. A genuine later
release re-arms normally.

**Reload budget.** Every reload is reserved against a rolling window
(default 3 per 60s) in `sessionStorage` *and* `localStorage`, merged and
de-duplicated. Writes are **read back and verified** — some WebViews accept
`setItem` and silently drop the value, and an uncounted reload is an infinite
reload loop. It **fails closed**: no readable storage, or no verified write, and
the reload does not happen *now*. It is **deferred, not discarded**: the pending
update is kept and re-attempted when the window rolls (~60 s), because the tab
that lost the race is still the one running stale code. You get one warning per
blocked episode, not one per retry.

**Failure handling.** A 404 or malformed version response logs *one* warning,
never reloads, and keeps polling. So does a version endpoint that accepts the
connection and **never answers**: every version check is capped at 10 s (aborted
where `AbortController` exists) and the poll loop re-arms *ahead of* the request
rather than off its promise, so no single stuck request can stop update
detection. One request per instance is in flight at a time. Every entry point is
wrapped so a bug in the kit can never escape into the host page.

---

## Multi-instance: N plugins on one page (kit 2.0)

Since 2.0.0 the kit is not a singleton. `window.JellyfinRefreshKit` is a
**manager** owning a registry of named **instances** — one per adopting
plugin/collection. Each `<script>` tag of the kit registers exactly one
instance from its own `data-*` attributes. Two plugins can each ship their own
copy of the file (even at *different kit versions*) and both adoptions work:

```html
<script src="/web/KefinTweaks/jellyfin-refresh-kit.js"
        data-version-url="/web/KefinTweaks/version.json" data-version-json-field="version"
        data-asset-patterns="/KefinTweaks/"
        data-entry-scripts="/web/KefinTweaks/kefinTweaks-config.js,/web/KefinTweaks/injector.js"></script>

<script src="/web/DemoPack/jellyfin-refresh-kit.js"
        data-name="DemoPack"
        data-version-url="/web/DemoPack/version.json" data-version-json-field="version"
        data-asset-patterns="/DemoPack/"
        data-entry-scripts="/web/DemoPack/demopack-entry.js"></script>
```

Each instance gets its **own** version source, baseline, poll cadence, entry
bootstrap (sequential within the instance, concurrent across instances), mode
and update callback. KefinTweaks' assets go out at `?v=<KT version>`,
DemoPack's at `?v=<DP version>` — verified live with both on one page.

### The registration contract (the cross-version promise)

The **first** kit copy to execute installs the manager, the **single**
`createElement` wrapper, and the shared reload engine. Every later copy
detects the manager and calls the frozen, forward-stable entry point instead
of re-wrapping anything:

```js
window.JellyfinRefreshKit.__registerInstance(tagConfig, KIT_VERSION)
// → instance handle {name, version, latestVersion, versionedUrl, checkNow, state} | null
```

The full contract is written (and frozen) in the file header — the short form:
the manager normalizes the arriving config with its own rules and **ignores
unknown keys**, so an older manager accepts a newer copy's config; the call
**never throws**; a duplicate registration (same name + same config — e.g. a
double-included tag) silently dedupes; `__contractVersion` (currently `3`) is
strictly additive forever — revision 2 adds `__claimSingularGlobal(obj)`, the
page-level once-only claim on a singular config object, which exists for objects
that cannot carry the ordinary non-enumerable claim marker (frozen, sealed,
exotic), and revision 3 adds `__handoffTo(newManager)` (below). A v1 caller
never calls either and is unaffected. If the already-installed global is a **1.x
singleton** (no `__registerInstance`), a 2.x copy logs one warning and goes
**inert** rather than fight it — mixing 1.x + 2.x means the 1.x-shipping
plugin should upgrade its kit copy.

### Newest wins: the manager handoff (kit 2.3)

"The first copy manages the page" was a real problem, not a detail. It meant a
plugin shipping the newest kit could not guarantee its own fixes governed the
page: whichever copy's `<script>` tag happened to parse first ran the flip
guard, the reload budget, the safety gates and the URL interception **for every
adoption on the page**. A live four-copy test made the consequence concrete — a
2.1.2 copy loading ahead of a 2.2.0 copy ran 2.1.2's pair-based flip guard and
reload-looped a tab (7 reloads in 185s across 3 version identities) that the
2.2.0 copy sitting right next to it had already fixed.

Since 2.3.0 the rule is **the newest copy on the page manages it**. An arriving
copy compares itself against `manager.kitVersion` *numerically, segment by
segment* (never as strings — `2.10.0` beats `2.9.0`) and:

| Arriving copy vs. manager | What happens |
| --- | --- |
| older, or equal | registers as an instance (unchanged; equal versions never hand over) |
| **newer**, manager speaks contract ≥ 3 | **handoff** — the newer copy takes the page over, one `console.log` says so |
| **newer**, manager speaks contract < 3 | registers as an instance **plus one loud `console.warn`** naming both versions and stating that page-level reload semantics on this page are the older copy's |

The handoff is lossless and synchronous. The old manager stops (every timer
cancelled, every listener removed, every instance latched off so a request
already in flight cannot act), and hands over each registered instance **with
its live state** — resolved baseline and latest version, pending update,
bootstrap entry progress, one-shot warning latches — plus the shared page state,
including a reload that is already committed. Nothing is re-fetched, no entry
chain is re-executed, no warning is printed to the console twice, and the reload
budget and per-tab flip history need no transfer at all: they live in
`localStorage`/`sessionStorage` under page-wide keys, keyed by instance names
the handoff preserves.

Two things about the old copy afterwards, both deliberate:

* Its `createElement` wrapper flips to a permanent **inert pass-through** rather
  than being uninstalled (other code may already hold a reference to it), and
  the new manager stacks its own on top. Still exactly one wrapper that does
  anything — the newest one — so URLs are never versioned twice.
* `window.JellyfinRefreshKit` still points at it, because that property is
  installed non-configurable on purpose and can never be re-pointed. It is an
  **inert delegate**: every member, `kitVersion` and `__contractVersion`
  included, forwards to the current manager, so app code and later kit copies
  always reach the live one. `state().shared.managerLineage` lists the copies in
  the order they ran the page.

> **No version of this kit has ever been released publicly, and pre-2.3.0
> copies must never be.** A pre-2.3.0 manager cannot hand a page over, so a
> newer copy arriving after one is stuck under it — everything works, but every
> page-level fix made since is inert. Ship 2.3.0 or newer in every adopting
> plugin.

`window.JellyfinRefreshKit` is installed **non-configurable**, and the manager
also keeps a non-enumerable backup handle (`__jellyfinRefreshKitManager`) that a
later copy consults before concluding "a 1.x singleton owns this page". A 1.x
copy loading *second* used to be able to redefine the global out from under a
live 2.x manager — stripping `__registerInstance` while the manager's registry,
timers and interceptor kept running invisibly, so every *subsequent* copy went
inert blaming a singleton that wasn't there. It can't any more.

### Names

`data-name` if you set it; otherwise derived deterministically from
`versionUrl` (its parent folder: `/web/KefinTweaks/version.json` →
`KefinTweaks`); otherwise `instance-<N>`, where **N counts anonymous adoptions
only** — an unrelated named plugin whose kit tag parses first cannot renumber
you, so `JellyfinRefreshKitConfigs['instance-1']` stays a usable handle.

Same name + *different* config registers as `name#2` with a warning — and a
later copy equivalent to an existing `#2`/`#3` dedupes against **it**, so
re-injected tags cannot accumulate live instances. An anonymous adoption has no
name to collide on, so it is deduped by comparing its declared config against
the anonymous instances already registered: an injector that applies the same
nameless payload twice gets **one** instance, not two. (A tag that declares
literally nothing is exempt — several of those are indistinguishable until their
own `JellyfinRefreshKitConfigs['instance-<N>']` entries are read.)

"Same config" is judged on what the **tags declared**
(`data-*` plus the singular global), before any keyed entry is merged, so a
keyed entry can neither create nor destroy a dedupe. Two instances declaring the **same `entryScripts`** never both
load them (loading the same file twice into one document re-executes it:
duplicate injectors, duplicate observers); the second registers with its entry
chain suppressed and warns.

### Overlap rule

On every `src`/`href` assignment the one wrapper consults **all** instances'
`assetPatterns`; the **first-registered** matching instance versions the URL
with **its** version. Registration order is document order of the kit tags.
If a URL matches two instances' patterns, first-registered wins and the
manager logs **one** `console.warn` naming the overlap. A matching instance
that has not resolved a version yet is **skipped** rather than allowed to veto
a sibling that has one — going out unversioned is the one outcome nobody wants,
and "matched first" is a tie-break between equals. URLs already carrying `v=`
always pass through untouched. Keep patterns disjoint (they name your own
folder — they naturally are).

### Shared reload, shared budget

A page reload is a page-level resource, so the safety gates, idle tracking and
the reload budget (storage key unchanged: `jellyfin-refresh-kit-budget-v1`)
are one shared engine:

- **Any** auto-mode instance that detects an update requests the shared safe
  reload; one reload serves every pending instance at once — and **one
  navigation spends exactly one budget slot**, however many instances arm in the
  window between `location.reload()` and the new document committing.
- The idle requirement is the **strictest (max) `idleSeconds`** among the
  instances currently wanting to reload; the effective budget is the
  **minimum `reloadBudget`** among all instances.
- After a reload triggered by plugin A, plugin B's unchanged assets are
  re-requested at their *same* immutable `?v=` URLs and come straight back
  from the HTTP cache — verified live: a DemoPack-only bump reloaded once and
  all 39 unchanged KefinTweaks assets were served from cache.

### Notify vs auto interplay

A `notify` instance **never** triggers the shared reload — its
`onUpdateAvailable` fires and that's it. If an `auto` instance later reloads
the page for its own update, the notify instance's tab naturally converges too
(its entries re-resolve at whatever version its endpoint now reports). Verified
live: DemoPack in notify mode saw its callback fire with no reload; a
subsequent KefinTweaks update reloaded the page.

### Targeted window config

`window.JellyfinRefreshKitConfig` (singular, 1.x) still works, and each kit copy
reads it **at its own tag position** — so it configures the adoption it was
written next to, exactly as §(a) documents, even when another plugin's kit tag
happens to come first in the document. Single-plugin adoptions are untouched.

The kit never *clears* that global, so it is still live at every later kit tag
in the document — and since **2.2.0** exactly one rule decides who gets it. The
same rule runs on both sides (each tag as it parses, and the manager's fallback
for copies that cannot read the global themselves), so there is one behaviour on
the page, not two implementations of one intention.

**The rule.** A singular global is **claimable exactly once**. A consumer may
claim an *unclaimed* global when any of these holds:

- **(a)** the global **positively identifies** it — same `versionUrl`, or the
  same *adoption identity*, which is the kit's own naming rule (`name`, else the
  name derived from `versionUrl`). So a global carrying the endpoint and a tag
  carrying `data-name="MyPlugin"` are the same adoption whenever the kit would
  name both `MyPlugin`;
- **(b)** the global identifies **nobody** (no `name`, and no `versionUrl` to
  derive one from) and this consumer got there first;
- **(c)** the consumer declares **nothing identifying** of its own — a bare tag,
  or one carrying only behavioural attributes (`data-mode`, `data-poll-seconds`,
  `data-idle-seconds`, `data-reload-budget`, `data-entry-timeout-ms`).

Anything else is a **disagreement**: the global identifies an adoption and the
consumer declares identifying config that does not match it. The consumer
**skips it with one warning** and keeps its own attributes — *without* claiming
it, so the adoption the global really names still gets it at its own tag,
whichever order the two tags appear in.

| The global says | Your tag says | Outcome |
|---|---|---|
| identity `A` (by `name`, or by a `versionUrl` deriving `A`) | the same identity | **applied** — unless an earlier tag already claimed that object |
| identity `A` | a different identity, or any other identifying config (`assetPatterns`, `entryScripts`, `bootVersion`, `getVersion`, `onUpdateAvailable`) | **skipped**, one warning |
| identity `A` | nothing identifying | **applied** — unless already claimed |
| nothing (behavioural keys only) | anything | **applied** to the first tag that reaches it; **skipped** with one warning at every later one |

That kills the class of bug the guard existed for, in both directions: a global
naming plugin A can never reach plugin B, and B can never absorb A's endpoint,
patterns, mode or callback.

Two consequences worth knowing:

- **Adopters that each assign their own global above their own tag are
  unaffected** — those are distinct objects, each claimed by its author. That
  now holds even if you `Object.freeze` yours (the claim falls back to a
  page-level identity set; before 2.2.0 a frozen object was refused).
- **The same payload injected twice** re-evaluates its own
  `window.JellyfinRefreshKitConfig = {...}`, so each copy claims its own object
  and both apply — and the registry then collapses them into **one** instance,
  comparing inline `getVersion`/`onUpdateAvailable` by source text. (Two *bare*
  tags sharing a **single** global object are the ambiguous case: the first
  takes it, the second is told so and registers on defaults. Use the keyed form
  there.)

For N instances use the keyed form, matched by instance name — including the
`#2` collision suffix and the `instance-<N>` fallback name, so a tag with no
`data-*` at all is addressable.

**It must be defined before every kit tag.** Each copy reads
`window.JellyfinRefreshKitConfigs` *synchronously, at its own `<script>`
position*, so an entry written below the tags is never consulted — put the
inline script above them:

```html
<script>
  window.JellyfinRefreshKitConfigs = {
    DemoPack: { mode: 'notify', onUpdateAvailable: function (nv, ov) { /* toast */ } }
  };
</script>

<script src="/web/KefinTweaks/jellyfin-refresh-kit.js" ...></script>
<script src="/web/DemoPack/jellyfin-refresh-kit.js" ...></script>
```

An entry that arrived too late no longer fails silently: once the document has
parsed, the manager warns once for every key naming an instance that had already
registered — and once for every key that matched **no** instance at all (a typo,
or an `instance-<N>` number that no longer names what you meant), listing the
registered instance names so the fix is one line away.

A keyed entry cannot rename its instance: the `name` key is ignored, and the
lookup key is the instance's **final** resolved name — including the `#2`
suffix, so `JellyfinRefreshKitConfigs['MyPlugin#2']` reaches the second adoption
and nothing else (a `#N` instance falls back to the base-name entry only when no
entry exists under its own key). A keyed `versionUrl` never re-derives the name
out from under the key it was found under, and it can never cause a dedupe
either: whether two registrations are the *same adoption* is decided on the
config **as the tags declared it**, before any keyed entry is merged.

Per-instance priority: `JellyfinRefreshKitConfigs[name]` >
`JellyfinRefreshKitConfig` > `data-*` > defaults.

---

## Adoption

### (a) Generic script collection

Put the kit's tag **before** your bootstrap's tag.

```html
<script src="/web/MyPlugin/jellyfin-refresh-kit.js"
        data-version-url="/web/MyPlugin/version.json"
        data-version-json-field="version"
        data-poll-seconds="300"
        data-idle-seconds="5"
        data-asset-patterns="/MyPlugin/"
        data-mode="auto"
        data-reload-budget="3"></script>

<script src="/web/MyPlugin/bootstrap.js"></script>
```

`version.json`:

```json
{ "version": "1.2.0" }
```

A plain-text response works too — just omit `data-version-json-field` and serve
`1.2.0` as the body.

If you need a function, a regex pattern, or an update callback, use the config
object instead (it must be set **before** the kit's tag and it **wins** over
`data-*` — unless it names a *different* adoption than the tag it lands on, in
which case that tag skips it with a warning; see *Targeted window config*):

```html
<script>
  window.JellyfinRefreshKitConfig = {
    versionUrl: '/web/MyPlugin/version.json',
    versionJsonField: 'version',
    assetPatterns: ['/MyPlugin/', /\/cdn\/myplugin@[^/]+\//],
    pollSeconds: 300,
    mode: 'notify',
    onUpdateAvailable: function (newV, oldV) {
      myToast('MyPlugin ' + newV + ' is available — refresh when convenient.');
    }
  };
</script>
<script src="/web/MyPlugin/jellyfin-refresh-kit.js"></script>
<script src="/web/MyPlugin/bootstrap.js"></script>
```

### (a2) Bootstrap mode — recommended for script collections

Classic adoption still leaves your config + injector tags in `index.html`
unversioned. Bootstrap mode collapses that surface to a single stable file: the
shell references **only the kit**, and the kit loads your entry files itself —
in order, each with `?v=<version>` — once the version resolves:

```html
<script src="/web/MyPlugin/jellyfin-refresh-kit.js"
        data-version-url="/web/MyPlugin/version.json"
        data-version-json-field="version"
        data-asset-patterns="/MyPlugin/"
        data-entry-scripts="/web/MyPlugin/config.js,/web/MyPlugin/bootstrap.js"
        data-entry-timeout-ms="3000"></script>
```

Ordering is guaranteed (entry N+1 only starts after N settles; a failed entry
logs once and the chain continues). Availability beats freshness: if the version
endpoint is down or slow, entries load **unversioned** after `entryTimeoutMs`
(default 3 s) rather than leaving the page without your plugin. Entries that
went out unversioned are code of *unknown* vintage — whatever the HTTP cache
held for that bare URL — so the kit does **not** adopt the version it later
resolves as a clean baseline (that would leave the tab permanently stale while
reporting itself healthy). It treats that resolution as an update and asks for
**one** safe reload, latched in `sessionStorage` to once per tab session, so a
chronically dead endpoint fails by running old code rather than by reload-looping
the browser. `.css` entries become stylesheets, everything else becomes a script.

> **Entry scripts should not assume user context.** Bootstrap entries execute as
> soon as the version resolves — often before Jellyfin has an authenticated
> `ApiClient` user. An entry that calls user-scoped APIs at parse/boot time
> (e.g. `getUserViews(..., getCurrentUserId())` while the id is still `null`)
> will race exactly as it would with plain static tags. Guard on
> `ApiClient.getCurrentUserId()` before user-scoped calls.

### (b) KefinTweaks, self-hosted (verified end-to-end)

KefinTweaks' loader does exactly what the interceptor is built for
(`injector.js`):

```js
const script = document.createElement('script');        // line 552
script.src = `${scriptRoot}${filename}${urlSuffix}`;    // line 553
document.head.appendChild(script);                      // line 566
```

…and `urlSuffix` is hardcoded to `''` — the cache-buster is commented out one
line above its definition (lines 309–310). `loadCSS()` does the same for
`<link href>` at lines 516–519. **Zero changes to KefinTweaks are required**;
line 553's assignment goes through the kit's accessor and comes out versioned.

Copy the KefinTweaks repo to `jellyfin-web/KefinTweaks/`, add
`jellyfin-refresh-kit.js` and a `version.json` alongside it, and inject **one**
tag (bootstrap mode — this exact setup is verified live, including auto-reload
convergence when `injector.js` itself changes):

```html
<script src="/web/KefinTweaks/jellyfin-refresh-kit.js"
        data-version-url="/web/KefinTweaks/version.json"
        data-version-json-field="version"
        data-poll-seconds="60"
        data-idle-seconds="5"
        data-asset-patterns="/KefinTweaks/"
        data-entry-scripts="/web/KefinTweaks/kefinTweaks-config.js,/web/KefinTweaks/injector.js"
        data-entry-timeout-ms="3000"
        data-mode="auto"
        data-reload-budget="3"></script>
```

`/web/KefinTweaks/version.json`:

```json
{ "version": "1.0.0" }
```

Where `kefinTweaks-config.js` is the usual `window.KefinTweaksConfig = { ... }`
blob with `"kefinTweaksRoot": "/web/KefinTweaks/"` — it must precede
`injector.js` in `data-entry-scripts`, which the kit guarantees. (The tag goes
into your JS Injector entry if you use that plugin.)

Bump `version.json` on every release. Open tabs converge on their own — the
verified end-to-end result: 39/40 requests versioned, the only unversioned
fetch being the kit itself, and an idle tab reloading onto a new release
(including a changed injector) within one poll interval.

### (c) C# plugin authors — `RefreshKit.cs`

Drop `RefreshKit.cs` (namespace `JellyfinRefreshKit`) into your plugin project.
It compiles into *your* assembly — no NuGet package, no shared dependency, and
two plugins both embedding it coexist safely (each gets its own types, caches,
and tag identity; the version controller is deliberately abstract so routes
can't collide).

What it gives you, all ported from the battle-tested Jellyfin-Enhanced
middleware: request-time injection of your script tag(s) into `index.html` with
an idempotent scrub-then-insert (a stale `?v=` tag is *replaced*, never kept), a
strong body-derived ETag (`rk-…`) with correct 304/412 conditional handling,
gzip/brotli preserved end to end, a bounded representation cache behind
single-flight gates, fail-open behavior on any error, per-build cache keys
(`{version}-{dllTicks}` — a same-version DLL swap busts caches without a
restart), and a content-derived build id (SHA-256 of the DLL).

Adoption — the whole thing:

```csharp
// 1. In your IPluginServiceRegistrator:
serviceCollection.AddRefreshKit(new RefreshKitOptions {
    PluginName  = "My Plugin",
    BasePath    = "MyPlugin",              // your controller's [Route]
    ScriptPaths = new[] { "script" },      // one injected tag per entry
    DevMode     = () => Plugin.Instance?.Configuration.DevMode == true,
    // Anything the JS kit needs is handed over as tag attributes. This is the
    // ONLY mechanism that emits data-version-url, so if you want layer 3
    // (open-tab convergence) you need this line — see the pairing note below.
    ExtraAttributes = _ => "data-version-url=\"../MyPlugin/RefreshVersion\" "
                         + "data-version-json-field=\"CacheKey\"",
});

// 2. First line of your script-serving endpoint. The no-argument overload reads
//    the SAME live DevMode delegate you registered above, so the endpoint's
//    headers and the tag's dev="…" can never drift apart:
RefreshKit.ApplyScriptCacheHeaders(Response);             // immutable vs no-store
//    (RefreshKit.ApplyScriptCacheHeaders(Response, devMode) is still there for
//     when you resolve the flag yourself.)

// 3. (Optional) a version endpoint for the JS kit to poll:
[HttpGet("RefreshVersion")] [AllowAnonymous]
public ActionResult Version() => VersionJson();          // subclass RefreshKitVersionControllerBase
```

Pair it with the JS kit for layer 3: serve `jellyfin-refresh-kit.js` as one of
your assets and point `data-version-url` at your version endpoint via
`ExtraAttributes`, as
in step 1 above (`data-version-json-field="CacheKey"` — the host serializer
emits PascalCase, or use the plain-text variant and omit the field).
`ExtraAttributes` is the **only** thing that puts attributes on the injected
tag: without it the tag carries no `versionUrl`, the JS kit's first poll fails
with one `no versionUrl configured` warning, and layer 3 never runs. Shell +
assets + open tabs, all three layers, one file each.

**`jellyfin-refresh-kit.js` must be the FIRST entry in `ScriptPaths`** — and,
more generally, its tag must come ahead of every other injected tag on the page.
`BuildScriptTags` emits one `defer` tag per entry *in `ScriptPaths` order*, and
deferred scripts execute in document order, so `ScriptPaths` order **is**
execution order. The kit installs its `document.createElement` hook at the
moment it runs, and any sub-asset a script creates before that is
[unversioned forever](#what-it-honestly-cannot-do) — layer 2 is silently off for
exactly those files, `state()` still reports a healthy resolved version, and
nothing warns. `new[] { "script", "jellyfin-refresh-kit.js" }` is the natural
edit and the wrong one; write `new[] { "jellyfin-refresh-kit.js", "script" }`.
(If you also have an early-capture/bootstrap script of your own, the kit still
goes first — it is what makes the others' assets versionable.)

### `RefreshKitOptions` reference

| Option | Type | Required | Notes |
|---|---|---|---|
| `PluginName` | `string` | **yes** | The tag's identity: every injected tag carries `plugin="<PluginName>"` and the scrub regex removes exactly those, so two plugins never scrub each other. Changing it between releases orphans the old tag — pick it once. |
| `BasePath` | `string` | **yes** | Your controller's `[Route("…")]` segment. Srcs are built as `../{BasePath}/{scriptPath}?v={cacheKey}`; the leading `../` keeps it working behind a base-url prefix. |
| `ScriptPaths` | `IReadOnlyList<string>` | **yes**, ≥1 | Ordered paths relative to `BasePath`, one injected tag each, **in this order — and `defer` tags execute in document order, so this list is the execution order**. If you serve `jellyfin-refresh-kit.js` here it must be the **first** entry: anything that runs before it creates its sub-assets unversioned, silently and on every load (see §(c)). Any other early-capture/bootstrap script goes immediately after it. |
| `DevMode` | `Func<bool>?` | no | Live flag stamped as `dev="true\|false"`. Read by `ApplyScriptCacheHeaders(Response)`, so headers and tag can't disagree. Exceptions are swallowed (→ false). |
| `VersionProvider` | `Func<string>?` | no | Overrides the cache key in tags and `?v=`. Default `{assembly version}-{DLL LastWriteTimeUtc ticks}`, which changes on every build even when the version number doesn't. Must be URL- and attribute-safe; exceptions fall back to the default. |
| `ExtraAttributes` | `Func<string, string?>?` | no | Raw attributes appended to a tag; the argument is the script path being emitted, so you can decorate one tag only. Spliced **verbatim** — you own quoting and escaping. This is how the JS kit gets `data-version-url` / `data-version-json-field`. |
| `Enabled` | `Func<bool>?` | no | Per-request kill switch. Return false and index.html passes through untouched — expose it as an admin toggle. Exceptions count as enabled. Default: enabled. |

Populate it once and treat it as immutable: it is read from request threads
without locking.

`BuildScriptTags` also stamps `data-boot-version="{CacheKey}"` on every tag it
emits, which the JS kit reads off its own `document.currentScript` and uses to
**seed its baseline**. That closes the page-serve → first-poll blind spot for
free: if you update the plugin while a client is still booting, its first poll
*detects* the change instead of absorbing it. It costs you nothing to configure —
just make sure the endpoint the kit polls reports the same identity
(`CacheKey`), which is what `data-version-json-field="CacheKey"` above already
does.

---

## Config reference

All options are **per instance** (each kit tag configures its own instance).

| Option | `data-*` | Default | Notes |
|---|---|---|---|
| `name` | `data-name` | derived | Instance name. Derived from `versionUrl`'s parent folder when omitted, else `instance-<N>` — N counting **anonymous adoptions only**, so a named plugin registering first cannot shift it. |
| `versionUrl` | `data-version-url` | — | Required unless `getVersion` is set. Fetched with `?_=<ts>` + `cache:'no-store'`. |
| `versionJsonField` | `data-version-json-field` | — | Set to e.g. `version` to parse the response as JSON. Omit for plain text. |
| `bootVersion` | `data-boot-version` | — | Identity of the build that **served this document**, stamped into the tag by your server. Seeds the baseline, so an update landing between page-serve and the first poll is detected instead of absorbed. Must name the **same** identity the version endpoint reports — `RefreshKit.cs` emits `data-boot-version="{CacheKey}"`, so pair it with `data-version-json-field="CacheKey"`. A mismatch self-heals: after one reload that does not change the served identity the kit discards the seed with a warning and falls back to the first-poll baseline. |
| `getVersion` | — | — | Config-object only. `async () => string`. Overrides `versionUrl`. |
| `pollSeconds` | `data-poll-seconds` | `60` | Clamped 15–3600. Only ticks while visible. |
| `idleSeconds` | `data-idle-seconds` | `5` | Clamped 0–300. Effective floor is 1s. Page reload uses the **max** among instances wanting one. |
| `assetPatterns` | `data-asset-patterns` | `[]` | Substrings, or `RegExp` via the config object. `data-*` is comma-separated substrings only. First-registered match wins across instances. |
| `mode` | `data-mode` | `auto` | `auto` \| `notify` \| `off`. `notify` never triggers the shared reload. `off` disables **polling and reloads only** — the instance still performs one version fetch so its `assetPatterns` keep getting `?v=`; turning off auto-reload does not turn off cache-busting. |
| `onUpdateAvailable` | — | — | Config-object only. `(newVersion, oldVersion) => void`. Fires once per distinct version. |
| `reloadBudget` | `data-reload-budget` | `3` | Reloads per rolling 60s. **Clamped 1–100** — `0` is *not* "never reload", it is silently raised to `1`. To keep `?v=` versioning with no auto-reload use `mode: 'notify'` (callback, no reload) or `mode: 'off'` (no polling either); see the `mode` row. Page-level budget is the **min** among all instances. |
| `entryScripts` | `data-entry-scripts` | `[]` | Bootstrap mode: ordered entry files the kit loads itself with `?v=<this instance's version>`. Comma-separated in the attribute (URLs containing commas need the config object). |
| `entryTimeoutMs` | `data-entry-timeout-ms` | `3000` | Clamped 250–30000. How long bootstrap mode waits for the first version fetch before loading entries unversioned. |

**Priority (per instance):** `window.JellyfinRefreshKitConfigs[name]` (read by
each copy at its own tag, under the instance's **final** name — define it
*above* the kit tags) > `window.JellyfinRefreshKitConfig` (read by each copy at
its own tag, under the one claim rule above — skipped with a warning on a
disagreement, or when an earlier tag already claimed that object) >
`data-*` attributes > defaults.

## Public API

`window.JellyfinRefreshKit` is the manager. The 1.x surface is kept and
delegates to the sole instance when exactly one exists; with 2+ instances the
top-level `version` / `latestVersion` report the **first-registered** instance
(prefer `get(name)`), `versionedUrl` uses the page-level matcher (all
instances, first-registered match wins; `force` applies the first instance's
version), and `checkNow()` checks every instance.

```js
// 1.x-compatible surface (delegates as described above)
window.JellyfinRefreshKit.version
window.JellyfinRefreshKit.latestVersion
window.JellyfinRefreshKit.versionedUrl(u, force)
window.JellyfinRefreshKit.checkNow()             // -> Promise (all instances)
window.JellyfinRefreshKit.state()                // aggregate snapshot, see below

// Multi-instance surface (2.0)
window.JellyfinRefreshKit.kitVersion             // CURRENT manager copy's kit version
window.JellyfinRefreshKit.instances()            // ['KefinTweaks', 'DemoPack', ...]
window.JellyfinRefreshKit.get(name)              // instance handle:
//   { name, version, latestVersion, versionedUrl(u, force), checkNow(), state() }

// Registration contract (for kit copies, not for app code)
window.JellyfinRefreshKit.__registerInstance(config, kitVersion)
window.JellyfinRefreshKit.__contractVersion      // 3
window.JellyfinRefreshKit.__claimSingularGlobal(obj)  // contract rev 2; for kit copies
window.JellyfinRefreshKit.__handoffTo(newManager)     // contract rev 3; for kit copies
```

After a handoff every one of these forwards to the manager that took over, so
the object you hold is always answering for the live one (see *Newest wins*).

`state()` keeps every 1.1.0 field at the top level (describing the sole/first
instance) and adds: `instanceCount`, `instances` (name → per-instance block),
`interceptorInstalled` / `interceptorCount` (always 1 — an inert wrapper left
behind by a handoff is not an interceptor), `contractVersion`,
`bootVersion` / `baselineFromBootSeed`, `candidateVersion`, `flapDisarmedFor`
(the **latest** refused transition), `restoredByHandoff`,
`entriesDeduped`, `wouldBlockNow`, `idleWindowMs` / `effectiveIdleWindowMs` /
`effectiveIdleWindowFrom`, and `shared` (pending instances, effective idle
window and the instance imposing it, `reloadCommitted` / `reloadsSurvived`,
`blockedRetries`, `mediaBlockedForMs`, effective budget, budget key,
`managerHandoffs` / `managerLineage`).

In `shared`, every field describing *the reload the engine is trying to
perform* is `null` when there is nothing pending — `blockReason`,
`effectiveIdleWindowMs` and `effectiveIdleWindowFrom` alike. A snapshot naming
an idle window while `pendingInstances` is empty would send you after a
constraint the engine would never apply (before 2.2.0 it could even name a
`mode: 'off'` sibling as imposing it). `reloadCommitted` is `true` only in the
window between `location.reload()` and the new document; if it is still `true`
long afterwards the host refused the navigation — `reloadsSurvived` counts how
many times that has happened, and the kit re-arms and retries each time.

Two fields answer two different questions, and reading the wrong one is the
classic false trail:

- **`blockReason`** — why the *pending* auto-reload isn't happening
  (`media_element`, `dialog`, `active_editor`, `not_idle`, `hidden`,
  `playback_route`, `fullscreen_media`, …). It is `null` unless there is
  actually an update waiting, so a healthy up-to-date tab reads `null`. This is
  the field to ask a user for. (`lastBlockReason` carries the most recent
  refusal, including `reload_budget`, which is a page-level refusal rather than
  a safety gate and so never appears in `blockReason`.)
- **`wouldBlockNow`** — the same live safety probe evaluated unconditionally:
  what *would* block a reload if one were wanted right now. Useful when
  reproducing a gate, meaningless as a diagnosis: a perfectly current tab
  showing a paused movie reads `media_element` here for as long as the movie is
  paused, and nothing is wrong.

`blockReason` and `wouldBlockNow` also answer against *different idle windows*,
on purpose. The shared engine gates a reload on the **strictest (max)**
`idleSeconds` among the instances currently wanting one, so a *pending*
instance's `blockReason` and `idle` are computed with that window —
`effectiveIdleWindowMs` reports it and `effectiveIdleWindowFrom` names the
instance imposing it, so an instance configured `idleSeconds: 0` sitting behind
a sibling's `300` reads `not_idle` and says who is responsible instead of
reading `null` while nothing happens. With nothing pending both shared fields
are `null` rather than a hypothetical. `wouldBlockNow` and `idleWindowMs` keep
the instance-local view: what would block *me*.

## License

MIT. Fill in the author line at the top of `jellyfin-refresh-kit.js`.
