# For plugin authors

This guide is for plugin and script authors who want to embed Refresh Kit in their own Jellyfin plugin. Server admins who only want to stop seeing stale plugin UI should install the standalone plugin instead — see the root [README.md](../README.md). Both approaches can coexist on the same Jellyfin page; the standalone plugin's side of that relationship is described in [plugin/README.md](../plugin/README.md#relationship-to-single-file-adoption).

The repository contains two reusable files for plugins that need direct cache/version control:

| File | Purpose |
| --- | --- |
| [`jellyfin-refresh-kit.js`](../jellyfin-refresh-kit.js) | Dependency-free client runtime for versioned runtime assets, version polling, safe reloads, bootstrap loading, and diagnostics. |
| [`RefreshKit.cs`](../RefreshKit.cs) | Self-contained C# helper for cache-correct `index.html` injection, script URLs, ordinary-path strong ETags/conditionals, nested-buffer safe degradation, and optional version endpoints. |

You can use the JavaScript runtime by itself, or pair it with `RefreshKit.cs` when your Jellyfin plugin can provide the server-side integration.

## Client runtime: `jellyfin-refresh-kit.js`

The runtime has no package dependency and no build step. Serve it from your plugin and configure it from its own `<script>` tag or from a JavaScript config object.

### Basic integration

```html
<script src="/web/MyPlugin/jellyfin-refresh-kit.js"
        data-name="MyPlugin"
        data-version-url="/web/MyPlugin/version.json"
        data-version-json-field="version"
        data-asset-patterns="/MyPlugin/">
</script>
```

Matching scripts and links created through `document.createElement` after the
kit starts receive the resolved version in supported string/`URL` assignments,
and the tab can detect when the version endpoint changes. The interceptor
deliberately does not rewrite markup created through `innerHTML`,
`document.write`, or `createElementNS`; nor does it see a URL set through
`setAttributeNS`, `setAttributeNode`, or on an element created by calling
`Document.prototype.createElement.call(document, …)` directly (only the `src`
/ `href` property and `setAttribute` on elements handed out by the page's
`document.createElement` are intercepted).

`data:`, `blob:`, `javascript:` and
`about:` URLs are never versioned, and `assetPatterns` are matched against the
resolved URL: a same-origin URL on its path and query only, a cross-origin URL
on the full URL, and never on the fragment (relative URLs are resolved against
the document base first).

### Bootstrap mode — recommended when the kit should load your entry files

If your entry scripts themselves would otherwise be static unversioned tags, let Refresh Kit load them after the current version resolves:

```html
<script src="/web/MyPlugin/jellyfin-refresh-kit.js"
        data-name="MyPlugin"
        data-version-url="/web/MyPlugin/version.json"
        data-version-json-field="version"
        data-asset-patterns="/MyPlugin/"
        data-entry-scripts="/web/MyPlugin/config.js,/web/MyPlugin/injector.js">
</script>
```

Bootstrap mode:

- waits for the initial version before loading entries
- preserves load settlement and synchronous execution order
- versions the entry URLs
- treats path-ending `.css` entries as stylesheets
- treats path-ending `.mjs` entries as ES modules
- logs and skips an entry element that reports a load failure
- falls back to unversioned entries if the initial version lookup exceeds `entryTimeoutMs`
- loads the entries at `?v=<bootVersion>` immediately when a `bootVersion` seed is configured (runtime 2.4.9 and newer; `bootVersion` is the build identity of the served page, see the options table below); in `auto` and `notify` the first version fetch still runs for update detection, while `off` makes no fetch at all (the seed is the served build)

That timeout keeps a broken version endpoint from preventing the plugin itself from loading. Its effective ceiling is the runtime's 10-second version-fetch timeout: the option accepts values up to 30000, but a version request that has not answered after 10 s fails on its own, so the entries never wait longer than that.

For an `.mjs` entry, Refresh Kit versions the root module URL only. Native
static/dynamic imports do not inherit its query string and do not pass through
the DOM interceptor, so bundle the graph or put the build identity in imported
specifiers when those files need immutable freshness. Ordinary synchronous
module graphs settle before the next entry; a top-level-`await` continuation can
outlive the module element's `load` event and overlap a later entry.

## JavaScript configuration

Options can be supplied as `data-*` attributes or through JavaScript configuration objects.
Tag-local `data-*` discovery requires the kit itself to run as a classic script
(ordinary, `defer`, and `async` classic tags all work), because module-script
evaluation has no `document.currentScript`. If the kit is loaded with
`type="module"`, its `data-*` attributes are not read; define
`window.JellyfinRefreshKitConfig` before that module tag instead. This does not
affect `.mjs` files listed in `entryScripts`, which are bootstrap entries rather
than the kit tag itself.

For multiple instances, use:

```javascript
window.JellyfinRefreshKitConfigs = {
    MyPlugin: {
        versionUrl: '/web/MyPlugin/version.json',
        versionJsonField: 'version',
        assetPatterns: ['/MyPlugin/']
    }
};
```

Important options:

| Option | Default | Purpose |
| --- | ---: | --- |
| `name` | Derived | Instance identity used in logs, diagnostics, and keyed configuration. |
| `versionUrl` | — | Endpoint that reports the current version. |
| `versionJsonField` | — | JSON property containing the string version when the endpoint returns JSON; arrays, objects, numbers, booleans, and null are rejected. |
| `versionEpochJsonField` | — | Optional JSON property containing an opaque process epoch (an identity for the server process that changes on restart). After the same fresh version/epoch pair has been observed twice, it can authorize one revisit of a version this tab has already left — a genuine rollback after a restart — which the flap guard would otherwise refuse; an epoch change on an unchanged version is not another update, and the epoch never versions assets. |
| `bootVersion` | — | Build identity that produced the current document; should represent the same identity as the version endpoint. |
| `pollSeconds` | 60 | Visible-tab polling interval, clamped to 15–3600 seconds. |
| `idleSeconds` | 5 | Required idle time before automatic reload, clamped to 0–300 seconds. |
| `assetPatterns` | None | URL patterns (substrings, or `RegExp` objects in JavaScript config) whose dynamically-created assets should receive versioning. Matched against the resolved URL — a same-origin URL on its path and query, a cross-origin URL on the full URL, never the fragment — so a relative `MyPlugin/x.js` under `/web/` matches `/MyPlugin/` like `/web/MyPlugin/x.js` does, an anchored `/^\/web\/MyPlugin\//` keeps matching, a pattern that happens to name this host cannot version every asset, and a pattern that names a CDN host keeps working. A URL that already carries a cache-busting query parameter (`v`, `ver`, `version`, `rev`, `hash`, `build`, `cb`, `nocache`, `_`, or the standalone plugin's own `rkv`, …) is left exactly as its author wrote it. |
| `entryScripts` | None | Ordered entry URLs for bootstrap mode. |
| `entryTimeoutMs` | 3000 | Maximum initial version wait before bootstrap entries fall back to unversioned loading. Clamped to 250–30000, but effectively capped at the 10-second version-fetch timeout. Ignored when `bootVersion` seeds the version. |
| `mode` | `auto` | `auto` reloads, `notify` reports updates without reloading, `off` leaves URL versioning active without update polling behaviour. The value is trimmed and case-insensitive; any other value logs one warning and falls back to `notify` (a mistyped mode never enables automatic reloads). |
| `reloadBudget` | 3 | Maximum reloads per rolling 60-second window, applied by this document to the authoritative same-origin IndexedDB ledger. Range 1–100; unavailable or corrupt coordination defers automatic reload. |
| `hiddenReload` | `true` | Allows an otherwise-safe pending reload while the tab is hidden. |
| `hiddenSettleSeconds` | 25 | Required hidden period before a hidden-tab reload is considered. |
| `getVersion` | — | Config-object callback that can replace `versionUrl`; its raw string result must be at most 200 characters, then its trimmed identity must be non-empty and not begin with `<`. |
| `onUpdateAvailable` | — | Callback invoked when an update is detected. |

When several kit instances share one page, reload safety resolves conservatively: stricter idle/hidden requirements and smaller reload budgets win where shared behaviour must be chosen. Across tabs, reservations are shared but configuration is not negotiated; each attempting document applies its own page-level effective budget.

## JavaScript API

`window.JellyfinRefreshKit` exposes the page-level manager.

| Member | Purpose |
| --- | --- |
| `get(name)` | Returns the named instance handle, including its versions, `versionedUrl`, `checkNow`, and `state`. |
| `instances()` | Returns registered instance names in registration order. |
| `versionedUrl(url, force)` | Versions a URL outside normal interception. Pattern matching uses all instances and the first registered match; `force=true` uses the first registered instance. |
| `checkNow()` | Immediately checks all registered version sources, bypassing the polling interval's spacing floor. A `mode: 'off'` instance is not polled, but if its single startup version resolution failed it is made here (runtime 2.4.8 and newer), so URL versioning can still start. |
| `state()` | Returns diagnostic state for instances and the shared reload engine. |

Handles returned by Refresh Kit 2.4.5 and newer remain safe to retain when a
newer kit copy takes over the page: the same frozen handle forwards to the live
replacement instance without changing its public members.

On a multi-instance page, prefer
`JellyfinRefreshKit.get(name).versionedUrl(url, force)` for adopter-owned URLs.
The manager-level `versionedUrl` retains its 1.x first-registered-instance rule
for forced versioning and can therefore apply another adopter's version.

Use `state()` as the first diagnostic when collecting a support log for reload behaviour.

## Server helper: `RefreshKit.cs`

`RefreshKit.cs` can be copied directly into a C# Jellyfin plugin and registered through its service registrator.

```csharp
using JellyfinRefreshKit;

var processEpoch = System.Guid.NewGuid().ToString("N"); // create once at startup

serviceCollection.AddRefreshKit(new RefreshKitOptions
{
    PluginName = "My Plugin",
    BasePath = "MyPlugin",
    ScriptPaths = new[] { "script" },
    DevMode = () => Plugin.Instance?.Configuration.DevMode == true,
    // Optional: return one opaque value that is stable for this process and
    // changes after a genuine restart. Do not generate a new value per call.
    EpochProvider = () => processEpoch,
});
```

The helper injects your script tag into `index.html` with a cache identity in the URL and with a `data-boot-version` representing the build that produced the page.

In your script endpoint, apply the matching cache headers:

```csharp
[HttpGet("script")]
[AllowAnonymous]
public ActionResult GetScript()
{
    RefreshKit.ApplyScriptCacheHeaders(Response);
    return Content(js, "application/javascript");
}
```

`BasePath` and each `ScriptPaths` entry are spliced into the injected `src` exactly as given, so `AddRefreshKit` rejects values containing whitespace, control characters, quotes, or any of `< > & \ ` ? #` — an author mistake that would otherwise emit a corrupt tag on every request now fails on first run.

### More than one plugin embedding the helper

Two plugins can each copy `RefreshKit.cs` in. Each gets its own middleware instance, its own representation cache, and its own `plugin="…"` scrub identity (the marker each instance uses to find and remove its own previously injected tag), so their tags never scrub each other and the only real collision risk is route names (which is why the version controller is opt-in).

The **innermost** instance owns the shell response. ASP.NET composes startup filters first-registered-outermost and Jellyfin's plugin load order is not something a plugin can choose, so the instance nearest the shell finishes first and commits its own representation — status, framing, and its strong `rk-` ETag. An outer instance recognises that commitment arriving through its own response body feature before it had decided anything, **signed with the `rk-` ETag**, and **stands down** for that response: it forwards the owner's bytes, validators, and conditional answers untouched, and it does not inject, rewrite a late status, harden metadata, or evaluate preconditions. The signature is what makes it an owner: a plain downstream that merely starts the response early (`HttpResponse.WriteAsync(string)` does so on every call) is not one, and is finalized like any other late-started source response. Once a signed owner has committed a complete shell, the outer instance also steps aside from the start of later shell requests, so the owner keeps seeing the client's own `If-None-Match` and can answer it with a real `304`. That stand-down is **recoverable**: while stood down the outer instance still watches each shell response, and two in a row that arrive without the signature — the inner kit was disabled through its kill switch, or its plugin was unloaded — clear it, so the outer instance injects again from the request after that without a restart. A single unsigned shell is not enough on purpose: a live owner serves one whenever it fails open (its transform cap, a decode failure), and resuming on it would cost the next client its `304`. It logs once each way (standing down, resuming).

The consequence is the same ownership boundary as the [standalone plugin's ordering caveat](../plugin/README.md#ordering-caveat): the outer instance's tag is not on that page. If your plugin's tag must always be present, inject it yourself rather than relying on being outermost; two instances still coexist safely, and a page that already carries the inner owner's tag is a correctly revalidating page.

An outer response **buffer** that is not a kit instance is a different shape — nothing has committed there, so the helper still transforms and then serves the complete shell `no-store` without validators, exactly as before.

### Optional version endpoint

If the JavaScript runtime should poll the same identity used by the server helper, expose a plugin-specific route by subclassing `RefreshKitVersionControllerBase`:

```csharp
[ApiController]
[Route("MyPlugin")]
public class MyVersionController : RefreshKitVersionControllerBase
{
    [HttpGet("RefreshVersion")]
    [AllowAnonymous]
    public ActionResult Version() => VersionJson();
}
```

Then configure the injected kit tag to read the returned `CacheKey`:

```csharp
ExtraAttributes = _ =>
    "data-version-url=\"../MyPlugin/RefreshVersion\" "
    + "data-version-json-field=\"CacheKey\" "
    + "data-version-epoch-json-field=\"Epoch\"";
```

Using the same cache identity for the page's boot version and the polled version avoids treating unrelated values as comparable builds.

## `RefreshKitOptions`

| Option | Required | Purpose |
| --- | --- | --- |
| `PluginName` | Yes | Stable identity used by the injected tag and the helper's own-tag scrub (removal of a previously injected copy of its tag). |
| `BasePath` | Yes | Controller route segment used to build relative script URLs. Spliced into `src` unescaped, so it is validated at registration. |
| `ScriptPaths` | Yes | Ordered script paths. Because injected tags use `defer`, this is also execution order. Each entry is spliced into `src` unescaped and validated at registration. |
| `DevMode` | No | Live flag used by script-cache handling and stamped into the tag. Dev mode also adds `dev=1` to the script URL; that marker remains `no-store` even if the setting changes before the request, so an immutable production response cannot poison the dev URL. |
| `VersionProvider` | No | Replaces the assembly-derived cache identity with a custom one. |
| `EpochProvider` | No | Supplies an opaque process-incarnation sidecar for JSON version responses. It must be stable for the loaded process and change on a genuine restart; it is never part of cache identity. |
| `ExtraAttributes` | No | Adds plugin-owned attributes to emitted script tags, including JS-kit configuration. Emitted before the kit's `src`, so never return a `src` of your own: HTML keeps the first occurrence and it would shadow the versioned URL. |
| `Enabled` | No | Live kill switch for the middleware. |

If `jellyfin-refresh-kit.js` is one of the injected scripts, put it **before** scripts that create runtime assets so its interception is active first.

## Drop-in limitations

- A plugin using non-bootstrap/classic loading can still have an initial race before the version resolves. Bootstrap mode avoids that for its entry files.
- The kit cannot version its own loader URL from inside itself; serve that file with an appropriate cache policy or through the C# helper.
- Bootstrap mode adds the initial version lookup before entry files load, bounded by `entryTimeoutMs` (and by the 10-second version-fetch timeout) — unless `bootVersion` seeds the version, in which case the entries load at once.
- A path-ending `.mjs` bootstrap entry is loaded as a module, but only its root URL is stamped. Native imports must be bundled or self-versioned, and top-level `await` can continue after the next entry starts.
- Keep `assetPatterns` scoped to your own plugin. Overlapping patterns between independent instances are resolved deterministically but should be avoided.
- All nodes behind a load balancer should expose one generation for the same deployed build. If epochs are enabled, each epoch must be stable for its process; fresh epochs bound legitimate restarts and finite mixed-node cycles, but a flapping deployment can still delay convergence.
- A CDN's own `latest`/resolution cache cannot be fixed by client-side versioning if the CDN maps the requested URL to stale content.
- JavaScript cannot add response `ETag` or `Cache-Control` headers; use the server helper when those guarantees are required.
- When two plugins embed `RefreshKit.cs`, the innermost instance owns the shell response and the outer one stands down, so the outer plugin's tag is not injected on that page while the inner one keeps committing it; the outer instance resumes on its own once the inner one stops. See [More than one plugin embedding the helper](#more-than-one-plugin-embedding-the-helper).

## Tell Refresh Kit which settings are bookkeeping (1.1.0.4)

The standalone plugin treats a change to a plugin's configuration XML as a
settings change and reloads open tabs for it. If your plugin writes to its own
configuration on a timer or at startup — a last-run timestamp, a telemetry
receipt, a cache stamp — declare those elements so they are left out of the
identity. Nothing else in Refresh Kit needs to know about your plugin, and the
declaration ships and versions with your code:

```csharp
public class PluginConfiguration : BasePluginConfiguration
{
    public bool EnableFeature { get; set; } = true;

    // Rewritten by a scheduled task; not a setting.
    public long LastRunUtc { get; set; }

    // Read by Jellyfin Refresh Kit: top-level elements of THIS document that
    // are bookkeeping. The element itself is never treated as a setting.
    public string[] RefreshKitIgnoredElements { get; set; } = new[] { "LastRunUtc" };
}
```

XmlSerializer writes that as
`<RefreshKitIgnoredElements><string>LastRunUtc</string></RefreshKitIgnoredElements>`;
a plain text body with names separated by whitespace or commas is accepted
too. Rules: only a direct child of the document element named
`RefreshKitIgnoredElements` is honoured; names match direct children of the
document element, case-insensitively; a document that is not well-formed XML
declares nothing; and an admin's own **Ignore these settings elements** entries
are added on top, never subtracted. List only values that change nothing a
browser renders — a setting an admin edits belongs in the generation, because
reloading for it is the point.

If you cannot ship the declaration (an older release you no longer build, or a
plugin you do not maintain), the fallback layers are the built-in registry in
`plugin/Jellyfin.Plugin.RefreshKit/KnownPluginConfigurationHints.cs` (see
[docs/contributing.md](contributing.md#adding-a-plugin-to-the-bookkeeping-registry))
and the admin setting. The admin **Diagnostics** section lists the names in
effect per plugin, whichever layer they came from.

## Protect application work (runtime 2.5.0)

`JellyfinRefreshKit.registerReloadGuard(name, canReload)` adds page-wide
protection independent of a particular version source or instance mode. Every
live callback must return **exactly `true`, synchronously** to permit an
automatic reload. False, missing/unknown returns, Promises and exceptions block
with `reload_guard`. Async callbacks are not awaited. Do not perform I/O, mutate
guards, or call the manager's diagnostic methods from a callback; read already
maintained application state. Names are nonempty strings of at most 100
characters and need not be unique.

The returned frozen handle provides:

- `changed()`: notify the engine after dirty/save state changes; it rechecks
  pending work while retaining all normal idle, playback and budget checks.
- `release()`: idempotently remove only this registration after successful
  completion or explicit discard. Same-name registrations remain independent.

Callbacks are reevaluated before a budget reservation, inside its final safety
check and again before navigation. Registration/state notification cancels a
reservation in flight. Guard closures and retained handles survive a newer
runtime taking over; no callback or draft text is persisted to browser storage.
`state().reloadGuards` reports names and the last evaluated permission only.

Use a guard for draft editors, custom/shadow-DOM forms and asynchronous saves.
Always feature-detect: the kit may be absent (disabled, blocked, not
installed), a 1.x singleton may own the page, or an OLDER kit copy may own
the frozen `window.JellyfinRefreshKit` global — a newer copy that takes the
page over cannot re-point that global, so `registerReloadGuard` is missing
from it even though the live manager supports guards. Your save path must
never depend on the kit:

```js
const rk = window.JellyfinRefreshKit;
const guard = (rk && typeof rk.registerReloadGuard === 'function')
    ? rk.registerReloadGuard('My editor', () => !editor.isDirty && editor.pendingSaves === 0)
    : { changed() {}, release() {} };
// Notify after updates to those values:
guard.changed();
// On an intentional, safe teardown:
guard.release();
```

### Declarative protection (runtime 2.5.1)

Where the API is unreachable, or simpler, mark the element that holds the
unfinished work:

```html
<form class="my-editor" data-refresh-kit-unsaved>…</form>
```

Any element in the document's light DOM carrying `data-refresh-kit-unsaved`
with a value other than `false` blocks automatic reloads with `unsaved_work`,
whichever kit copy manages the page (2.5.1 or newer). The probe does not look
inside shadow roots, so a web-component editor marks its host element. Hidden
elements count too — hiding a draft is not saving it — so set the attribute
when the work becomes dirty or a save starts, and remove it (or set it to
`false`) only after a confirmed successful save or an explicit discard. When
`registerReloadGuard` is called on a retired runtime copy whose live manager
lacks the API (a contract violation), it throws rather than returning a handle
that protects nothing; catch that and use the attribute. This is part of the frozen registration contract
(clause 8), as are `registerReloadGuard` and guard transfer between copies.

Do not release at submit time. Wait for a confirmed successful save and account
for edits made while that save was pending. A failed save still holds work.
A populated settings field alone does not establish dirty state; the owning
application must distinguish saved values from unsaved edits.

Enhanced 12.8's connected `.je-review-form` and `.je-save-dock.je-dirty` are
protected automatically with `unsaved_work`. Review forms remain protected
when blurred, hidden, or saving; removing a successfully saved/cancelled form
releases that protection. Empty open review forms also block conservatively.
Screensavers and hidden-tab reloads do not override application work. Other
plugins must register a guard for state not covered by the standard DOM probes.
See [the Enhanced adoption example](../examples/enhanced/README.md) and
[its real-server lab](../e2e/enhanced/README.md).

Enhanced admin saves also block while `.je-save-dock-btn[disabled]` is present,
including saves started without a dirty marker. Re-enabling the button releases
that save-state protection; a remaining dirty marker continues to protect edits.
