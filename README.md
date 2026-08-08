# Jellyfin Refresh Kit

Jellyfin Refresh Kit prevents Jellyfin web clients from getting stuck on stale plugin JavaScript and CSS after plugins change.

For server admins, install the standalone plugin once and it handles the common cache-staleness path across the server. For plugin authors, the repository also provides small reusable JavaScript and C# helpers for plugins that need direct control over assets they create at runtime.

The goal is simple: **users should not need to hard-refresh Jellyfin just to receive the current plugin UI.**

## Choose how you want to use it

| | Standalone plugin | Drop-in integration |
| --- | --- | --- |
| Best for | Jellyfin server admins | Plugin and script authors |
| Install | One Jellyfin plugin | `jellyfin-refresh-kit.js`, optionally with `RefreshKit.cs` |
| Covers | The common stale-cache path for plugins across the server | Your plugin, including assets it creates dynamically |
| Start here | [Standalone plugin](#standalone-plugin) | [For plugin authors](#for-plugin-authors) |

Both approaches can coexist on the same Jellyfin page. A plugin can use its own Refresh Kit integration while the standalone plugin handles server-wide change detection and static plugin tags.

- **License:** [MIT](LICENSE)
- **Compatibility evidence:** [COMPATIBILITY.md](COMPATIBILITY.md)
- **Detailed standalone-plugin notes:** [plugin/README.md](plugin/README.md)

---

## Why stale plugin code happens

There are three different places stale code can survive, and each needs a different fix:

1. **The app shell** — Jellyfin's `index.html` and the scripts/stylesheets referenced directly from it. The server needs to return a correctly revalidating page.
2. **Runtime-created assets** — scripts, stylesheets, `fetch()` URLs, dynamic imports, and CSS assets created after the page has loaded. The plugin that creates those URLs needs to version them.
3. **Already-open tabs** — a browser tab that never reloads never asks the server for the new page or assets. It needs to detect a plugin change and reload at a safe time.

The standalone plugin handles the app shell and open-tab refreshes server-wide, and cache-busts eligible plugin scripts/stylesheets already present in the shell. The drop-in runtime gives plugin authors control over runtime-created assets as well.

---

# Standalone plugin

Install one plugin and Refresh Kit watches the plugin environment for changes. Other plugins do not need to know it is installed.

## Requirements

- Jellyfin **10.11.x**
- Permission to install plugins and restart the Jellyfin server

The standalone plugin targets `net9.0` and is built against Jellyfin `10.11.11` packages.

## Installation

### Plugin repository — recommended

1. Open **Dashboard → Plugins → Repositories**.
2. Add a repository named `Jellyfin Refresh Kit`.
3. Use this repository URL:

   ```text
   https://raw.githubusercontent.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/main/manifest.json
   ```

4. Open **Catalog → General → Jellyfin Refresh Kit** and install it.
5. Restart Jellyfin.

### Manual installation

1. Download `jellyfin-refresh-kit_<version>.zip` from [GitHub Releases](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/releases).
2. Create a versioned plugin folder inside Jellyfin's `plugins` directory, for example:

   ```text
   /config/plugins/Jellyfin Refresh Kit_1.0.0.0/
   ```

3. Extract the archive into that folder. It should contain:

   ```text
   Jellyfin.Plugin.RefreshKit.dll
   meta.json
   ```

4. Restart Jellyfin.

### Verify the install

After startup:

- **Dashboard → Plugins** should show **Jellyfin Refresh Kit** as active.
- `GET /RefreshKit/Generation` should return JSON.

## What the standalone plugin does

### 1. Serves `index.html` with proper revalidation

Refresh Kit places middleware in front of Jellyfin's web app shell and processes `index.html` before it reaches the browser.

The resulting response uses a strong body-derived `rk-` ETag and supports normal HTTP conditional requests, including:

- `If-None-Match` → `304 Not Modified` when appropriate
- `If-Match` → `412 Precondition Failed` when appropriate
- `HEAD` requests
- identity, gzip, and Brotli representations
- Jellyfin's existing cache and `Vary` behaviour

The middleware is **fail-open**. If it cannot safely process a response, Jellyfin's original bytes are returned instead of breaking the web client.

### 2. Cache-busts eligible plugin scripts and stylesheets

While processing the app shell, Refresh Kit looks for other plugins' same-origin:

- `<script src="…">`
- `<link rel="stylesheet" href="…">`

When an eligible URL does not already carry its own version identity, Refresh Kit adds:

```text
?rkv=<generation>
```

When the monitored plugin state changes, the generation changes and the browser sees a new URL instead of reusing a stale cached copy.

The stamper is deliberately conservative. It leaves these alone:

- inline scripts
- non-stylesheet `<link>` elements
- cross-origin and protocol-relative URLs
- URLs that already carry a recognised version/cache-busting parameter
- Jellyfin's opaque query identities
- content-hashed filenames
- Refresh Kit's own injected tag

Stamping is idempotent: repeated processing does not accumulate duplicate `rkv` parameters, and unrelated query parameters and fragments are preserved.

### 3. Detects plugin changes and refreshes open tabs safely

Refresh Kit exposes one server-wide **generation** derived from the installed plugin state. The embedded browser runtime polls that generation and reacts when it changes.

A generation can move when a plugin is:

- installed or removed
- upgraded
- enabled or disabled
- replaced in place without a version-number change
- changed in a monitored client asset
- reconfigured, when configuration watching is enabled

The same generation is used for the injected runtime URL, its boot identity, the generation endpoint, and third-party `rkv` stamps. That keeps the server and browser on one shared cache identity.

## What is included in the generation?

For each installed plugin, Refresh Kit considers the state that can represent a user-visible plugin change, including:

- plugin ID
- plugin version
- plugin status
- plugin binaries
- client assets such as `.js`, `.mjs`, `.css`, `.map`, and `.html`
- the plugin's Jellyfin configuration XML when configuration watching is enabled

General runtime data such as databases, logs, and private data directories are not treated as client-code changes.

Directory scanning is bounded so a plugin with a large tree cannot cause an unlimited filesystem walk.

### Settings changes

Plugin settings can affect UI that is built when the page loads, so configuration changes are watched by default.

Refresh Kit watches Jellyfin's plugin configuration XML rather than a plugin's private data directory. This avoids treating per-user preferences and runtime cache churn as server-wide UI changes.

Configuration signals are controlled in three ways:

- **Debounce:** a changed timestamp must remain stable for 10 seconds before publication.
- **Per-plugin cooldown:** the first change publishes promptly; further changes during the configured window are coalesced into one later update.
- **Exclusions:** individual plugins can be ignored for configuration-change tracking.

Version and binary changes are not held behind the settings cooldown.

## Safe automatic reloads

A reload should never interrupt something more important than receiving fresh plugin code. The browser runtime therefore blocks automatic reloads while the page is not in a safe state.

| Gate | Reload is blocked while… |
| --- | --- |
| Hidden-tab settle | the tab has not satisfied the hidden-tab settle rules |
| Playback route | a Jellyfin video route is open |
| Fullscreen media | media is fullscreen or in picture-in-picture |
| Dialog | an active dialog/action sheet is open |
| Media session | real media playback is active on the page |
| Active editor | a text-editing field has focus |
| Password entry | a password field on the page still contains a value |
| Not idle | the configured user-idle period has not elapsed |

Refresh Kit also uses:

- repeated observation before arming an update
- a rolling reload budget shared by the page
- state that prevents a tab from repeatedly reloading back to a generation it has already left
- hidden-tab handling so an eligible reload can happen while the tab is out of the user's way

If a reload is currently unsafe, the update remains pending until a safe opportunity appears.

## Admin settings

Open **Dashboard → Plugins → Jellyfin Refresh Kit**.

| Setting | Default | Purpose |
| --- | ---: | --- |
| Serve index.html through the refresh kit | On | Master switch. When off, the middleware passes Jellyfin's shell through unchanged. |
| Cache-bust other plugins' script tags | On | Adds the current generation to eligible plugin scripts and stylesheets in the shell. |
| Reload open tabs after a plugin update | On | Performs safe automatic reloads. When off, update detection remains available without automatic reloads. |
| Treat plugin settings changes as updates | On | Includes plugin configuration XML changes in generation tracking. |
| Settings-change cooldown | 5 min | Coalesces repeated configuration changes from the same plugin. `0` disables the cooldown; debounce still applies. |
| Ignore settings changes from these plugins | Empty | One entry per line. Accepts plugin name, install folder, GUID, or assembly name. |
| Poll interval | 60 sec | How often visible tabs check the generation. Client range: 15–3600 seconds. |
| Required idle time | 5 sec | Minimum user inactivity before an automatic reload. Client range: 0–300 seconds. |
| Max reloads per minute | 3 | Rolling safety budget for automatic reloads. Client range: 1–100. |
| Developer mode | Off | Serves the embedded browser runtime with `no-store` instead of immutable caching. |

### Excluding noisy configuration files

If a plugin updates its normal configuration XML frequently even when an administrator is not changing settings, add it to **Ignore settings changes from these plugins**.

The admin diagnostics endpoint shows the inputs behind the current generation and can help identify the plugin whose timestamps are moving.

## HTTP endpoints

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /RefreshKit/Generation` | Anonymous | Returns `{ Version, BuildId, CacheKey }`; `CacheKey` contains the current generation. |
| `GET /RefreshKit/Generation.txt` | Anonymous | Returns the current generation as plain text. |
| `GET /RefreshKit/kit.js` | Anonymous | Serves the embedded browser runtime. |
| `GET /RefreshKit/Diagnostics` | Admin | Returns the current generation and per-plugin inputs used to build it. |

The generation and runtime endpoints are intentionally available before login so a stale Jellyfin login page can also detect a plugin change.

## Reverse proxies, CDNs, and BaseUrl

Normal reverse-proxy configurations should not need Refresh Kit-specific changes. The repository includes E2E coverage for direct Jellyfin access, nginx, Nginx Proxy Manager-style nginx, Caddy, Traefik, HAProxy, and subpath deployments.

For correct freshness, proxies and CDNs should:

- respect origin `Cache-Control` directives
- forward conditional request headers such as `If-None-Match` and `If-Match`
- avoid caching `/RefreshKit/Generation`
- preserve content encoding correctly, or re-encode with matching response headers

### Avoid forced caching that ignores Jellyfin's headers

A proxy that caches Jellyfin while explicitly ignoring origin cache directives can pin both the app shell and generation endpoint.

For nginx, avoid configurations such as:

```nginx
proxy_cache jfcache;
proxy_ignore_headers Cache-Control Expires;
```

Remove the directive that ignores `Cache-Control`/`Expires`, or exempt Jellyfin's web shell and Refresh Kit endpoints from the proxy cache.

The detailed proxy matrix, failure cases, and reproducible test rig are documented in [plugin/README.md](plugin/README.md) and [e2e/proxy/README.md](e2e/proxy/README.md).

### Jellyfin BaseUrl / subpaths

Jellyfin `BaseUrl` deployments such as `/jellyfin` are supported. Injected resources use relative URLs so they resolve under the configured Jellyfin prefix.

## Troubleshooting

### Open tabs do not reload

In the browser console, run:

```javascript
JellyfinRefreshKit.state()
```

The shared state includes the current reload `blockReason`, which identifies the safety gate preventing the reload.

A common case is `password_entry`: if a login password remains in a retained password input, Refresh Kit will not automatically reload that document while the value is present.

### The generation keeps changing

Open the admin-only diagnostics endpoint:

```text
GET /RefreshKit/Diagnostics
```

Check which plugin's binary, asset, or configuration timestamp is moving. If a plugin's configuration XML is intentionally noisy, add it to the configuration-watch exclusion list or adjust the cooldown.

### Plugin tags are not stamped

Middleware ordering determines which serve-time tags are visible to Refresh Kit. A tag inserted after Refresh Kit's response transformation cannot be stamped by it.

Check the served `index.html` and response headers. The plugin that owns an unstamped runtime or later-injected asset can adopt Refresh Kit directly if it needs stronger control over its own cache identity.

### Pages are stale only behind a proxy

Compare `/web/index.html` and `/RefreshKit/Generation` through the proxy with the Jellyfin origin. Forced proxy caching or ignored origin cache directives are the usual cause.

## Standalone-plugin limitations

Refresh Kit closes the common stale-plugin path, but it cannot control every way another plugin may load code.

- **Runtime-created assets remain the owning plugin's responsibility.** Dynamic imports, `fetch()`, JavaScript-created resources, CSS `url()`, and similar URLs do not exist in `index.html` for the standalone stamper to rewrite.
- **Middleware ordering matters.** Refresh Kit can only stamp tags already present when its HTML transform runs. A later/outer middleware can add tags it never sees or replace response headers afterward.
- **Cross-origin assets are not rewritten.** Refresh Kit does not alter third-party CDN URLs or their cache semantics.
- **Generation is server-wide.** A monitored change to any plugin can make eligible open Jellyfin tabs reload once.
- **Broken intermediary caching still wins.** A proxy or CDN configured to ignore origin cache directives can serve stale content regardless of the origin's behaviour.
- **Background tabs are subject to browser timer throttling/freezing.** Detection can be delayed until the browser allows the tab to run again.

---

# For plugin authors

The repository contains two reusable files for plugins that need direct cache/version control:

| File | Purpose |
| --- | --- |
| [`jellyfin-refresh-kit.js`](jellyfin-refresh-kit.js) | Dependency-free client runtime for versioned runtime assets, version polling, safe reloads, bootstrap loading, and diagnostics. |
| [`RefreshKit.cs`](RefreshKit.cs) | Self-contained C# helper for cache-correct `index.html` injection, script URLs, strong ETags, conditional requests, and optional version endpoints. |

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

Matching scripts/stylesheets created after the kit starts receive the resolved version in their URL, and the tab can detect when the version endpoint changes.

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
- preserves entry order
- versions the entry URLs
- treats `.css` entries as stylesheets
- logs and skips a failed entry instead of taking down the page
- falls back to unversioned entries if the initial version lookup exceeds `entryTimeoutMs`

That timeout keeps a broken version endpoint from preventing the plugin itself from loading.

## JavaScript configuration

Options can be supplied as `data-*` attributes or through JavaScript configuration objects.

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
| `versionJsonField` | — | JSON property containing the version when the endpoint returns JSON. |
| `bootVersion` | — | Build identity that produced the current document; should represent the same identity as the version endpoint. |
| `pollSeconds` | 60 | Visible-tab polling interval, clamped to 15–3600 seconds. |
| `idleSeconds` | 5 | Required idle time before automatic reload, clamped to 0–300 seconds. |
| `assetPatterns` | None | URL patterns whose dynamically-created assets should receive versioning. |
| `entryScripts` | None | Ordered entry URLs for bootstrap mode. |
| `entryTimeoutMs` | 3000 | Maximum initial version wait before bootstrap entries fall back to unversioned loading. |
| `mode` | `auto` | `auto` reloads, `notify` reports updates without reloading, `off` leaves URL versioning active without update polling behaviour. |
| `reloadBudget` | 3 | Maximum reloads per rolling 60-second window. |
| `hiddenReload` | `true` | Allows an otherwise-safe pending reload while the tab is hidden. |
| `hiddenSettleSeconds` | 25 | Required hidden period before a hidden-tab reload is considered. |
| `getVersion` | — | Config-object callback that can replace `versionUrl`. |
| `onUpdateAvailable` | — | Callback invoked when an update is detected. |

When several kit instances share one page, reload safety resolves conservatively: stricter idle/hidden requirements and smaller reload budgets win where shared behaviour must be chosen.

## JavaScript API

`window.JellyfinRefreshKit` exposes the page-level manager.

| Member | Purpose |
| --- | --- |
| `get(name)` | Returns the named instance handle, including its versions, `versionedUrl`, `checkNow`, and `state`. |
| `instances()` | Returns registered instance names in registration order. |
| `versionedUrl(url, force)` | Versions a URL created outside the normal script/link interception path, such as `fetch()` or a dynamic import URL. |
| `checkNow()` | Immediately checks all registered version sources. |
| `state()` | Returns diagnostic state for instances and the shared reload engine. |

Use `state()` as the first diagnostic when collecting a support log for reload behaviour.

## Server helper: `RefreshKit.cs`

`RefreshKit.cs` can be copied directly into a C# Jellyfin plugin and registered through its service registrator.

```csharp
using JellyfinRefreshKit;

serviceCollection.AddRefreshKit(new RefreshKitOptions
{
    PluginName = "My Plugin",
    BasePath = "MyPlugin",
    ScriptPaths = new[] { "script" },
    DevMode = () => Plugin.Instance?.Configuration.DevMode == true,
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
    + "data-version-json-field=\"CacheKey\"";
```

Using the same cache identity for the page's boot version and the polled version avoids treating unrelated values as comparable builds.

## `RefreshKitOptions`

| Option | Required | Purpose |
| --- | --- | --- |
| `PluginName` | Yes | Stable identity used by the injected tag and the helper's own-tag scrub logic. |
| `BasePath` | Yes | Controller route segment used to build relative script URLs. |
| `ScriptPaths` | Yes | Ordered script paths. Because injected tags use `defer`, this is also execution order. |
| `DevMode` | No | Live flag used by script-cache handling and stamped into the tag. |
| `VersionProvider` | No | Replaces the assembly-derived cache identity with a custom one. |
| `ExtraAttributes` | No | Adds plugin-owned attributes to emitted script tags, including JS-kit configuration. |
| `Enabled` | No | Live kill switch for the middleware. |

If `jellyfin-refresh-kit.js` is one of the injected scripts, put it **before** scripts that create runtime assets so its interception is active first.

## Drop-in limitations

- A plugin using non-bootstrap/classic loading can still have an initial race before the version resolves. Bootstrap mode avoids that for its entry files.
- The kit cannot version its own loader URL from inside itself; serve that file with an appropriate cache policy or through the C# helper.
- Bootstrap mode adds the initial version lookup before entry files load, bounded by `entryTimeoutMs`.
- Keep `assetPatterns` scoped to your own plugin. Overlapping patterns between independent instances are resolved deterministically but should be avoided.
- All nodes behind a load balancer should expose one stable identity for the same deployed build.
- A CDN's own `latest`/resolution cache cannot be fixed by client-side versioning if the CDN maps the requested URL to stale content.
- JavaScript cannot add response `ETag` or `Cache-Control` headers; use the server helper when those guarantees are required.

---

# Compatibility

The standalone plugin is built for **Jellyfin 10.11.x**. The reusable `RefreshKit.cs` integration is also exercised separately against Jellyfin 12 development/release-candidate environments in this repository's compatibility work.

The project includes compatibility testing across a broad set of community plugins and common reverse-proxy configurations. The detailed environments, tested plugin builds, verdicts, edge cases, and reproducible evidence live in [COMPATIBILITY.md](COMPATIBILITY.md).

Keeping the evidence in that file allows this README to describe the supported behaviour without turning into a test ledger.

---

# Development

## Repository layout

```text
.
├── README.md
├── LICENSE
├── manifest.json
├── COMPATIBILITY.md
├── jellyfin-refresh-kit.js
├── RefreshKit.cs
├── plugin/
│   ├── README.md
│   ├── build.sh
│   ├── Jellyfin.Plugin.RefreshKit/
│   │   ├── Jellyfin.Plugin.RefreshKit.csproj
│   │   ├── Plugin.cs
│   │   ├── PluginServiceRegistrator.cs
│   │   ├── PluginGenerationProvider.cs
│   │   ├── ThirdPartyTagStamper.cs
│   │   ├── RefreshKit.cs
│   │   ├── Controllers/
│   │   └── Configuration/
│   └── Jellyfin.Plugin.RefreshKit.Tests/
└── e2e/
    └── proxy/
```

Key files:

- `jellyfin-refresh-kit.js` — canonical browser runtime used by drop-in consumers and embedded into the standalone plugin at build time.
- `RefreshKit.cs` — reusable C# integration helper.
- `manifest.json` — Jellyfin plugin-repository manifest.
- `plugin/Jellyfin.Plugin.RefreshKit/` — installable standalone plugin.
- `PluginGenerationProvider.cs` — computes the server-wide plugin generation.
- `ThirdPartyTagStamper.cs` — cache-busts eligible script and stylesheet tags.
- `plugin/Jellyfin.Plugin.RefreshKit.Tests/` — xUnit tests for generation and stamping behaviour.
- `e2e/proxy/` — disposable Docker-based proxy, caching, websocket, subpath, and browser test rig.

## Build the standalone plugin

The plugin version is defined in:

```text
plugin/Jellyfin.Plugin.RefreshKit/Jellyfin.Plugin.RefreshKit.csproj
```

Build the marketplace package with:

```bash
export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
bash plugin/build.sh
```

The build creates:

```text
plugin/build/jellyfin-refresh-kit_<version>.zip
plugin/build/stage/
```

It also prints the package MD5 used by Jellyfin's repository manifest.

To update the corresponding checksum and timestamp in `manifest.json`:

```bash
bash plugin/build.sh --update-manifest
```

## Run tests

```bash
export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
$DOTNET_ROOT/dotnet test \
  plugin/Jellyfin.Plugin.RefreshKit.Tests/Jellyfin.Plugin.RefreshKit.Tests.csproj
```

The xUnit suite covers the generation provider and tag stamper, including:

- install/enable/disable state changes
- same-version binary replacement
- client-asset changes
- bounded directory traversal
- malformed or transient metadata
- configuration-change behaviour
- eligible and ineligible script/style tags
- idempotent stamping
- existing version identities
- query-string and fragment preservation

## Run the proxy/browser E2E rig

The E2E environment lives in `e2e/proxy/` and uses disposable Docker resources rather than an existing Jellyfin installation.

```bash
cd e2e/proxy
./run.sh up
./run.sh matrix
./run.sh ws
./run.sh cache
./run.sh e2e
./run.sh subpath
./run.sh down
```

Run the complete suite with:

```bash
./run.sh all
```

See [e2e/proxy/README.md](e2e/proxy/README.md) for prerequisites and the exact proxy/test matrix.

## Architecture and contribution rules

A few constraints are intentional and should be preserved when changing the project:

- **One canonical JavaScript runtime.** The standalone plugin embeds the repository-root `jellyfin-refresh-kit.js`; do not add a second committed copy.
- **Keep the browser runtime dependency-free.** It is designed to be served or copied directly without a JavaScript build pipeline.
- **Keep `RefreshKit.cs` self-contained and fail-open.** It sits in the app-shell request path and must not make Jellyfin unavailable when an unexpected condition occurs.
- **Do not let the standalone instance claim other plugins' runtime asset patterns.** Dynamic assets remain the adopting plugin's responsibility.
- **Keep multi-instance behaviour conservative.** Independent plugins can embed the runtime on the same page, so shared reload behaviour must not let one instance weaken another instance's safety requirements.
- **Add or update tests for generation/stamping changes.** These behaviours contain the subtle cache and filesystem rules.
- **Run the proxy E2E suite for middleware, response-header, injection, or proxy-sensitive changes.**
- **Keep documentation aligned with current behaviour.** User-visible behaviour belongs here; deeper standalone details belong in `plugin/README.md`; compatibility evidence belongs in `COMPATIBILITY.md`.

---

## License

Jellyfin Refresh Kit is licensed under the [MIT License](LICENSE).
