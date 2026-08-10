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

- Jellyfin **10.11.x** or **12.x**
- Permission to install plugins and restart the Jellyfin server

The standalone plugin is built for both, from one source tree: a `net9.0` build against the Jellyfin `10.11.0` ABI floor, and a `net10.0` build against Jellyfin `12.0.0-rc4` packages. Building the net9 assembly at the declared floor lets later 10.11 hosts bind its shared-library references upward; package verification rejects a DLL whose MediaBrowser assembly references do not equal its `targetAbi`. One plugin-repository URL serves both — the server picks the build matching its own generation.

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

1. Download the zip for your server from [GitHub Releases](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/releases): `jellyfin-refresh-kit_<version>.zip` for Jellyfin 10.11.x, or `jellyfin-refresh-kit_<version>_jf12.zip` for Jellyfin 12.x.
2. Create a versioned plugin folder inside Jellyfin's `plugins` directory, for example:

   ```text
   /config/plugins/Jellyfin Refresh Kit_<version>/
   ```

3. Extract the archive into that folder. It should contain:

   ```text
   Jellyfin.Plugin.RefreshKit.dll
   Jellyfin.Plugin.RefreshKit.pdb
   meta.json
   ```

4. Restart Jellyfin.

### Verify the install

After startup:

- **Dashboard → Plugins** should show **Jellyfin Refresh Kit** as active.
- `GET /RefreshKit/Generation` should return JSON.

## What the standalone plugin does

### 1. Serves `index.html` with cache-correct handling

Refresh Kit places middleware in front of Jellyfin's web app shell and processes `index.html` before it reaches the browser.

On the ordinary Kestrel path, where Refresh Kit owns the final response bytes,
the transformed representation uses a strong body-derived `rk-` ETag and
supports normal HTTP conditional requests, including:

- `If-None-Match` → `304 Not Modified` when appropriate
- `If-Match` → `412 Precondition Failed` when appropriate
- `HEAD` requests
- identity, gzip, and Brotli representations
- Jellyfin's existing cache and `Vary` behaviour

If an outer middleware buffers the response and later owns the final bytes,
Refresh Kit cannot truthfully attach a body-derived validator to that outer
representation. It safely degrades instead: the complete transformed shell is
still returned, but with `Cache-Control: no-store`, without stale entity
validators or integrity metadata, and with the outer owner's final content
type, coding, and HTTP/1.1 framing preserved. A stale conditional request gets
the full `200` response rather than an invalid `304`. This is intentionally not
the strong-ETag path.

The middleware is **fail-open**. If it cannot safely process a response,
Jellyfin's original bytes are returned instead of breaking the web client.

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
- in standalone middleware, any real `<base href>` outside template content; it can redirect Refresh Kit's own PathBase-relative runtime URL, so the complete shell transform is left byte-for-byte unchanged
- when the stamper is used directly by an adopter, any unsafe or entity-ambiguous base candidate; DOM recovery can reorder candidates, so source order is not trusted (safe same-origin relative bases remain eligible in that direct API)
- URLs that already carry a recognised version/cache-busting parameter
- Jellyfin's opaque query identities
- content-hashed filenames
- Refresh Kit's own injected tag

Stamping is idempotent: repeated processing does not accumulate duplicate `rkv` parameters, and unrelated query parameters and fragments are preserved.

### 3. Detects plugin changes and refreshes open tabs safely

Refresh Kit exposes one server-wide opaque **generation** derived from the code and client state this Jellyfin process is actually running. The embedded browser runtime polls that generation and reacts when it changes.

A generation can move when:

- Jellyfin or a loaded plugin activates a different module identity/MVID after restart
- a plugin is installed, upgraded, enabled, disabled, or removed and that lifecycle change takes effect after the required restart
- a monitored client asset belonging to a loaded plugin changes
- a loaded plugin is reconfigured, when configuration watching is enabled

The exact generation selected for a shell response is used for the injected runtime URL, its boot identity, and every third-party `rkv` stamp in that response. The generation endpoint reports the provider's current value. This keeps each served representation internally consistent even if a five-second scan-cache boundary occurs while a request is being transformed.

## What is included in the generation?

The identity is folded deterministically from:

- selected loaded Jellyfin host assemblies: assembly name/version and module MVID
- actually loaded plugin assemblies: the stable plugin ID plus, for each loaded module, its assembly name, assembly version and module MVID. The manifest/instance version text is deliberately *not* folded — it is mutable and an installer can rewrite it in place — so only the versions carried by the loaded assemblies participate
- active loose client assets: relative path, size, and content hash for `.js`, `.mjs`, `.css`, and `.html`
- the exact content of the plugin's Jellyfin configuration XML when configuration watching is enabled

Manifest status, absolute paths, timestamps, source maps, databases, logs, and private runtime-data directories are not generation identity. Some remain available as diagnostics, but they cannot make two nodes with identical active bytes disagree merely because files were copied at different times.

The provider looks at loaded state rather than staged disk state. Installing,
disabling, or deleting a plugin before its required restart therefore does not
announce code that is not active yet; after restart, a changed loaded MVID set
moves the generation and existing tabs converge. A same-version replacement is
therefore detected when the replacement module has a new MVID and is loaded;
arbitrary PE-byte changes that preserve the MVID are not a generation input.

Scanning is deterministic and bounded. Per plugin it admits/charges at most 4,000 file entries, 512 directories, 8 MiB of active asset content, and 2 MiB of configuration content. One complete scan is additionally capped at 16,000 charged files, 2,048 charged directories, 32 MiB of assets, and 8 MiB of configuration. A native enumerator may yield one extra unadmitted entry for a plugin to detect that a file/directory ceiling was crossed. Budget exhaustion contributes a stable truncation sentinel and appears in admin diagnostics. A transient read failure consumes its conservative reserved budget and retains the last coherent active snapshot when one exists, instead of publishing a false lifecycle change.

### Settings changes

Plugin settings can affect UI that is built when the page loads, so configuration changes are watched by default.

Refresh Kit watches Jellyfin's plugin configuration XML rather than a plugin's private data directory. This avoids treating per-user preferences and runtime cache churn as server-wide UI changes.

A configuration file that is a symbolic link (or another reparse point) is never followed, because a plugin picks its own configuration filename and following the link would let that choice read a file outside the configuration store. On a deployment that symlinks the store — NixOS, some ansible layouts — that plugin's settings changes are therefore not detected; the admin diagnostics endpoint reports the skipped files per plugin.

Configuration signals are controlled in three ways:

- **Debounce:** a changed configuration-content identity must remain stable for 10 seconds before publication.
- **Per-plugin cooldown:** the first change publishes promptly; further changes during the configured window are coalesced into one later update.
- **Exclusions:** individual plugins can be ignored for configuration-change tracking.

Loaded-module and active loose-asset identity changes are not held behind the
settings cooldown.

## Safe automatic reloads

The browser runtime is designed to defer an automatic reload while its
light-DOM safety probes observe an interaction that should not be interrupted.

| Gate | Reload is blocked while… |
| --- | --- |
| Hidden-tab settle | the tab has not satisfied the hidden-tab settle rules |
| Playback route | a Jellyfin video route is open |
| Fullscreen media | media is fullscreen or in picture-in-picture |
| Dialog | a rendered native, Jellyfin, or ARIA dialog/action sheet is open |
| Media session | real media playback is active on the page |
| Active editor | a text-editing field has focus |
| Password entry | a rendered, enabled, non-inert password field still contains a value |
| Not idle | the configured user-idle period has not elapsed |

Refresh Kit also uses:

- repeated observation before arming an update
- a rolling reload budget whose reservations are coordinated across same-origin tabs
- state that refuses an already-left generation unless an optional fresh process epoch safely authorizes one revisit
- hidden-tab handling so an eligible reload can happen while the tab is out of the user's way

Refresh Kit 2.4.7 and newer use one overlapping IndexedDB `readwrite`
transaction both to serialize each automatic-reload reservation and to update
the authoritative bounded numeric-v1 ledger. Once its read is granted, an
admitting transaction synchronously reruns every safety gate before appending a
slot; navigation is authorized only after transaction completion, another full
gate pass, and a check against the document's current effective budget. This
preserves distinct reloads made in the same millisecond and prevents cooperating
tabs from losing one another's concurrent reservations. A gate that closes
before the append spends nothing; a gate that closes after commit leaves the
slot conservatively spent without navigating.

`localStorage` and `sessionStorage` hold read-back-verified compatibility
mirrors. Valid mirror history is max-multiset-merged for migration, but the
mirrors may be stale or written out of order and never replace or reduce the
IndexedDB authority. When the IDB record does not yet exist, both legacy stores
must be readable and valid (an absent key is a valid empty history); afterward,
unavailable mirrors cannot turn valid IDB authority into a failure. If
IndexedDB is unavailable, corrupt, cannot commit, or exceeds the bounded wall
or monotonic deadline, automatic reload fails closed and the update remains
pending.

That first-run rule reads like a one-off migration step, and on an ordinary
browser it is one. Where it is **not** temporary: the first authoritative IDB
record is only written by a reservation that got past this check, so a browser
profile in which `localStorage` or `sessionStorage` is permanently unreachable
— storage blocked for the site, a hardened privacy mode, a sandboxed frame with
no storage access — never completes the migration and therefore refuses every
automatic-reload reservation, indefinitely, not just at first run. Update
detection, notifications, and URL versioning are unaffected; only the automatic
reload is withheld, which is the intended fail-closed direction when the kit
cannot prove how many reloads this origin has already spent.

The shared object is reservation history, not cross-tab configuration. Each
document applies the minimum `reloadBudget` among the instances registered on
that document. Runtime copies older than 2.4.7 do not participate in the mutex,
so the cross-tab serialization guarantee applies only while the participating
same-origin tabs run 2.4.7 or newer.

The optional process epoch is a JSON-only sidecar. An exact fresh
generation/epoch pair must be observed twice and claimed in a strict,
saturating per-tab set before it can provide one-shot proof for a historical
target generation. That authorization remains attached to the target generation
while its reload is pending, even if polls rotate through other process epochs
serving the same generation; replica rotation is not a new update identity. A
same-generation restart is recorded without reloading. Missing, invalid,
previously seen, or unverifiable epoch state preserves the older fail-closed
flap refusal. If a page leaves before one instance's baseline epoch or even its
baseline generation is durably known, a separate non-evicting per-tab coverage
record permanently prevents a later epoch from claiming that ambiguous history
fresh; an unresolved-generation record conservatively disables automatic
updates for that instance for the rest of the tab session. Epochs never enter
asset URLs, ETags, or the generation itself.

A scripted reload keeps the current document alive until the new response
commits, so the runtime cannot tell a host that refused the navigation from an
origin that is simply slow to answer. Runtime 2.4.8 and newer therefore treat
the survival watchdog as a suspicion rather than a verdict: detection comes
straight back, the safety records the attempt wrote are kept, and no second
navigation is committed for a further 12 seconds — long enough for a slow
origin to land the reload it was already performing, rather than cancelling it
and spending another budget slot on a duplicate. Only a reload call that throws
proves nothing was started, and only that case retracts what the attempt
recorded.

If a reload is currently unsafe, the update remains pending until a safe
opportunity appears. These probes cannot inspect closed shadow roots or prove
the state of DRM/external-player integrations, so “safe” means the documented
light-DOM gates observed no blocker, not that every third-party playback or
editing surface is knowable from JavaScript.

## Admin settings

Open **Dashboard → Plugins → Jellyfin Refresh Kit**.

| Setting | Default | Purpose |
| --- | ---: | --- |
| Serve index.html through the refresh kit | On | Middleware switch. When off, Jellyfin's shell passes through unchanged; the configuration and public generation/runtime endpoints remain available. |
| Cache-bust other plugins' script tags | On | Adds the current generation to eligible plugin scripts and stylesheets in the shell. |
| Reload open tabs after a plugin update | On | Performs safe automatic reloads. When off, update detection remains available without automatic reloads. |
| Treat plugin settings changes as updates | On | Includes plugin configuration XML changes in generation tracking. |
| Settings-change cooldown | 5 min | Coalesces repeated configuration changes from the same plugin. `0` disables the cooldown; debounce still applies. |
| Ignore settings changes from these plugins | Empty | One entry per line. Accepts plugin name, install folder, GUID, or assembly name. |
| Poll interval | 60 sec | How often visible tabs check the generation. Client range: 15–3600 seconds. |
| Required idle time | 5 sec | Minimum user inactivity before an automatic reload. Client range: 0–300 seconds. |
| Max reloads per minute | 3 | Rolling automatic-reload ceiling applied to shared same-origin reservation history. Client range: 1–100. Unavailable coordination defers the reload. |
| Developer mode | Off | Serves the embedded browser runtime with `no-store` instead of immutable caching. |

Clearing a numeric field saves its default rather than zero. `0` is a real,
distinct value where the range allows it: `0` in **Settings-change cooldown**
disables the cooldown, and `0` in **Required idle time** removes the idle wait.

### Excluding noisy configuration files

If a plugin updates its normal configuration XML frequently even when an administrator is not changing settings, add it to **Ignore settings changes from these plugins**.

The admin diagnostics endpoint shows the loaded identities, content-scan budgets, truncation/unavailability flags, and configuration signals behind the current generation.

## HTTP endpoints

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /RefreshKit/Generation` | Anonymous | Returns `{ Version, BuildId, CacheKey, Epoch }`; `CacheKey` contains the current generation and `Epoch` identifies this server process. |
| `GET /RefreshKit/Generation.txt` | Anonymous | Returns the current generation as plain text. |
| `GET /RefreshKit/kit.js` | Anonymous | Serves the embedded browser runtime. |
| `GET /RefreshKit/Diagnostics` | Admin | Returns the current generation and the per-plugin inputs used to build it, read as one atomic snapshot, plus scan budgets, truncation/unavailability flags, skipped reparse-point configuration files, and stamping abort counters. |

The generation and runtime endpoints are intentionally available before login so a stale Jellyfin login page can also detect a plugin change.

`BuildId` and `CacheKey` are opaque comparison/cache-busting tokens. Their
current derivation is an implementation detail: adopters must not parse their
segments, lengths, or hash form, and should only compare or propagate the whole
string. `Epoch` is also opaque. It is stable only for one loaded server process,
changes on restart, and is deliberately excluded from cache identities.

Opaque here means unreadable, not secret. The token cannot be enumerated or
reversed — no plugin name, version, path or count can be recovered from it —
but it is folded from public material with no per-install entropy, so an
unauthenticated observer who can already guess an exact host version and plugin
inventory can compute candidate folds offline and confirm that guess. Watching
the token also reveals *when* an admin saved plugin settings or when an update
took effect, a timing oracle inherent to any change signal an anonymous login
page can read. A per-install salt would remove the first property but would
break the invariant that identical active bytes produce identical generations
across nodes, so it is deliberately not used. See
[`plugin/README.md`](plugin/README.md) for the full statement.

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

A common case is `password_entry`: Refresh Kit will not automatically reload while a rendered, enabled, non-inert password field still contains a value. A populated login field retained inside Jellyfin's hidden login page is ignored after authentication.

### The generation keeps changing

Open the admin-only diagnostics endpoint:

```text
GET /RefreshKit/Diagnostics
```

Check which loaded assembly identity, active client asset, or watched configuration content is changing. If a plugin's configuration XML is intentionally noisy, add it to the configuration-watch exclusion list or adjust the cooldown.

### Plugin tags are not stamped

Middleware ordering determines which serve-time tags are visible to Refresh Kit. A tag inserted after Refresh Kit's response transformation cannot be stamped by it.

Check the served `index.html` and response headers. The plugin that owns an unstamped runtime or later-injected asset can adopt Refresh Kit directly if it needs stronger control over its own cache identity.

### Pages are stale only behind a proxy

Compare `/web/index.html` and `/RefreshKit/Generation` through the proxy with the Jellyfin origin. Forced proxy caching or ignored origin cache directives are the usual cause.

## Standalone-plugin limitations

Refresh Kit closes the common stale-plugin path, but it cannot control every way another plugin may load code.

- **Runtime-created assets remain the owning plugin's responsibility.** Dynamic imports, `fetch()`, JavaScript-created resources, CSS `url()`, and similar URLs do not exist in `index.html` for the standalone stamper to rewrite.
- **A directly retained pre-2.4.6 `document.createElement` wrapper cannot be retrofitted.** The exact released 2.4.2 wrapper becomes an inert pass-through after a newer manager takes over. Elements it created before handoff and calls through the current wrapper remain versioned; wrappers created by 2.4.6 and newer carry the additive forwarding bridge.
- **Middleware ordering matters.** Refresh Kit can only stamp tags already present when its HTML transform runs. A later/outer middleware can add tags it never sees or replace response headers afterward.
- **A real `<noscript>` disables the complete stamping transform.** Server-side code cannot know the user agent's scripting flag, and under scripting-disabled HTML parsing an effective `<base>` inside `<noscript>` can change a relative asset's origin. `ThirdPartyTagStamper` therefore leaves the whole stamping input byte-for-byte unchanged.
- **A document with more than 512 simultaneously open elements disables the stamping transform.** The tokenizer scans its open-element list per end tag, so an unbounded list would make pathological nesting quadratic inside the shell transform. Past that ceiling the pass is abandoned and the shell is served byte-for-byte unchanged. Both this abort and the `<noscript>` one are counted in admin diagnostics.
- **A quoted legacy `PUBLIC`/`SYSTEM` doctype is a conservative transform boundary.** The bounded tokenizer does not implement the complete HTML doctype state machine, so such a shell is served unchanged. Jellyfin's ordinary `<!doctype html>` remains transformable.
- **A real document `<base href>` disables runtime injection.** Refresh Kit's own URL is relative so it follows Jellyfin's configured PathBase; any effective base could redirect that URL to another path or origin. Bases inside inert template content do not trigger this boundary.
- **Outer response owners use safe degradation.** When another middleware owns the final buffered bytes, Refresh Kit serves the complete transformed shell as `no-store` without its strong validator, while preserving the outer owner's final framing. This is freshness without conditional revalidation.
- **An outer-owned tag can remain unstamped.** In both audited GetAvatar middleware orders, its one eligible tag is added after Refresh Kit's transform and is explicitly reported as a compatibility limitation. Reloading the shell cannot guarantee fresh bytes for that unchanged URL.
- **Cross-origin assets are not rewritten.** Refresh Kit does not alter third-party CDN URLs or their cache semantics.
- **Generation is server-wide.** A monitored change to any plugin can make eligible open Jellyfin tabs reload once.
- **Broken intermediary caching still wins.** A proxy or CDN configured to ignore origin cache directives can serve stale content regardless of the origin's behaviour.
- **Background tabs are subject to browser timer throttling/freezing.** Detection can be delayed until the browser allows the tab to run again.
- **Cross-tab budget coordination starts with runtime 2.4.7.** Older tabs still write the legacy numeric-v1 storage history but do not update the authoritative IndexedDB ledger inside its transaction, so they cannot be included in the concurrent-reservation guarantee until they load 2.4.7 or newer. Unavailable or corrupt IndexedDB safely defers automatic reload; unreadable/corrupt legacy stores do the same only while the first authoritative record still needs migration.

---

# For plugin authors

The repository contains two reusable files for plugins that need direct cache/version control:

| File | Purpose |
| --- | --- |
| [`jellyfin-refresh-kit.js`](jellyfin-refresh-kit.js) | Dependency-free client runtime for versioned runtime assets, version polling, safe reloads, bootstrap loading, and diagnostics. |
| [`RefreshKit.cs`](RefreshKit.cs) | Self-contained C# helper for cache-correct `index.html` injection, script URLs, ordinary-path strong ETags/conditionals, nested-buffer safe degradation, and optional version endpoints. |

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
`document.write`, or `createElementNS`.

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

That timeout keeps a broken version endpoint from preventing the plugin itself from loading.

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
| `versionEpochJsonField` | — | Optional JSON property containing an opaque process epoch. After two observations of a fresh exact pair, it can provide one-shot authorization for an otherwise-historical target generation; same-generation epoch rotation is not another update and it never versions assets. |
| `bootVersion` | — | Build identity that produced the current document; should represent the same identity as the version endpoint. |
| `pollSeconds` | 60 | Visible-tab polling interval, clamped to 15–3600 seconds. |
| `idleSeconds` | 5 | Required idle time before automatic reload, clamped to 0–300 seconds. |
| `assetPatterns` | None | URL patterns whose dynamically-created assets should receive versioning. A URL that already carries a cache-busting query parameter (`v`, `ver`, `version`, `rev`, `hash`, `build`, `cb`, `nocache`, `_`, or the standalone plugin's own `rkv`, …) is left exactly as its author wrote it. |
| `entryScripts` | None | Ordered entry URLs for bootstrap mode. |
| `entryTimeoutMs` | 3000 | Maximum initial version wait before bootstrap entries fall back to unversioned loading. |
| `mode` | `auto` | `auto` reloads, `notify` reports updates without reloading, `off` leaves URL versioning active without update polling behaviour. |
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
| `PluginName` | Yes | Stable identity used by the injected tag and the helper's own-tag scrub logic. |
| `BasePath` | Yes | Controller route segment used to build relative script URLs. |
| `ScriptPaths` | Yes | Ordered script paths. Because injected tags use `defer`, this is also execution order. |
| `DevMode` | No | Live flag used by script-cache handling and stamped into the tag. Dev mode also adds `dev=1` to the script URL; that marker remains `no-store` even if the setting changes before the request, so an immutable production response cannot poison the dev URL. |
| `VersionProvider` | No | Replaces the assembly-derived cache identity with a custom one. |
| `EpochProvider` | No | Supplies an opaque process-incarnation sidecar for JSON version responses. It must be stable for the loaded process and change on a genuine restart; it is never part of cache identity. |
| `ExtraAttributes` | No | Adds plugin-owned attributes to emitted script tags, including JS-kit configuration. |
| `Enabled` | No | Live kill switch for the middleware. |

If `jellyfin-refresh-kit.js` is one of the injected scripts, put it **before** scripts that create runtime assets so its interception is active first.

## Drop-in limitations

- A plugin using non-bootstrap/classic loading can still have an initial race before the version resolves. Bootstrap mode avoids that for its entry files.
- The kit cannot version its own loader URL from inside itself; serve that file with an appropriate cache policy or through the C# helper.
- Bootstrap mode adds the initial version lookup before entry files load, bounded by `entryTimeoutMs`.
- A path-ending `.mjs` bootstrap entry is loaded as a module, but only its root URL is stamped. Native imports must be bundled or self-versioned, and top-level `await` can continue after the next entry starts.
- Keep `assetPatterns` scoped to your own plugin. Overlapping patterns between independent instances are resolved deterministically but should be avoided.
- All nodes behind a load balancer should expose one generation for the same deployed build. If epochs are enabled, each epoch must be stable for its process; fresh epochs bound legitimate restarts and finite mixed-node cycles, but a flapping deployment can still delay convergence.
- A CDN's own `latest`/resolution cache cannot be fixed by client-side versioning if the CDN maps the requested URL to stale content.
- JavaScript cannot add response `ETag` or `Cache-Control` headers; use the server helper when those guarantees are required.

---

# Compatibility

The standalone plugin declares support for **Jellyfin 10.11.x and Jellyfin
12.x**. Its exact build inputs are Jellyfin Controller/Model `10.11.0` on
`net9.0` (`targetAbi` `10.11.0.0`) and `12.0.0-rc4` on `net10.0`
(`targetAbi` `12.0.0.0`); the live labs pin Jellyfin `10.11.11` and
`12.0.0-rc4` images by digest. A harness or declared range is not evidence that
every future minor passed; current-candidate results and exact snapshot
identities are recorded separately in [COMPATIBILITY.md](COMPATIBILITY.md).

The current compatibility inventory classifies all 101 rows in the audited Awesome Jellyfin plugin section. The compatibility gate freshly downloads and cryptographically inspects all 44 immutable archives, retains an exact lock-ordered receipt, and exercises the 40 testable runtime artifacts across 14 matrices; the three quarantined and one unsupported archives remain inspection-only. Three outer-response-buffer matrices use the statically enforced safe-degrade contract. The detailed environments, tested plugin builds, verdicts, edge cases, and reproducible evidence live in [COMPATIBILITY.md](COMPATIBILITY.md).

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
├── benchmarks/
├── scripts/
├── test.sh
└── e2e/
    ├── jellyfin/
    ├── proxy/
    └── compat/
```

Key files:

- `jellyfin-refresh-kit.js` — canonical browser runtime used by drop-in consumers and embedded into the standalone plugin at build time.
- `RefreshKit.cs` — reusable C# integration helper.
- `manifest.json` — Jellyfin plugin-repository manifest.
- `plugin/Jellyfin.Plugin.RefreshKit/` — installable standalone plugin.
- `PluginGenerationProvider.cs` — computes the server-wide plugin generation.
- `ThirdPartyTagStamper.cs` — cache-busts eligible script and stylesheet tags.
- `plugin/Jellyfin.Plugin.RefreshKit.Tests/` — xUnit tests for generation, middleware, HTML, and stamping behaviour.
- `e2e/jellyfin/` — pinned Jellyfin 10.11/12 lifecycle and real-browser lab.
- `e2e/proxy/` — disposable proxy, caching, websocket, subpath, and browser matrix.
- `e2e/compat/` — locked third-party-plugin and hostile-fixture compatibility matrices.
- `benchmarks/` — opt-in, non-gating server/browser scale measurements emitted as JSONL.
- `test.sh` — the supported entry point for fast through complete validation.

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

The build creates one verified immutable snapshot and atomically points `plugin/build` at it:

```text
plugin/build/jellyfin-refresh-kit_<version>.zip
plugin/build/jellyfin-refresh-kit_<version>_jf12.zip
plugin/build/stage/
plugin/build/stage-jf12/
```

It uses locked dependencies, the exact SDK in `global.json`, a read-only copy of every package-producing input, reproducible timestamps and paths, and mandatory package verification. It prints MD5 and SHA-256 identities for both artifacts.

To update the corresponding checksum and timestamp in `manifest.json`:

```bash
bash plugin/build.sh --update-manifest
```

That command updates the local manifest only; it does not publish a GitHub
release. Publication uses a temporary, version-specific candidate ref so an
unvalidated finalized manifest is never placed on `main`:

1. **Bootstrap first.** GitHub exposes `workflow_dispatch` only when the
   workflow file already exists on the default branch. Land the release and
   post-release workflow infrastructure on `main` in an earlier, non-release
   commit. A candidate ref cannot bootstrap its own dispatch workflow.
2. Keep the package-producing source in one clean commit `S`, and put the
   finalized `manifest.json` in its direct manifest-only child `M`. Confirm that
   the intended push remote is
   `https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit`, that
   `v<version>` is absent, and that the candidate ref is absent. Push only `M`
   to `refs/heads/release-candidate/v<version>`; do not push `S` or `M` to
   `main` yet, and never move or reuse that candidate ref.
3. Dispatch `release-validation.yml` with `--ref
   release-candidate/v<version>` and inputs `source_revision=S`,
   `manifest_revision=M`, the four-part version, and the policy kind. Its five
   jobs keep every hosted-runner timeout at or below 360 minutes: immutable-ref
   preflight, fast/reproducibility/security, integration, compatibility, and a
   final rebuild/retention job. The final artifact contains the three original
   worker receipts, semantic lab evidence, one checksum root, and the exact
   candidate bytes. The ref and absent tag are checked again immediately before
   retention.
4. After that run succeeds, recheck that the candidate still names `M` and the
   tag is still absent. Create `v<version>` at `S` and publish a release with
   exactly the two ZIPs from the retained artifact's `release-candidate/`
   directory—no additional release assets.
5. Only after the tag and both assets exist, fast-forward `origin/main` to the
   already validated manifest child `M`. Do not rebuild, amend, merge, or
   regenerate the manifest between validation and this fast-forward.
6. Dispatch `post-release-assets.yml` from `main` at `M`, supplying the same
   source/manifest/version/policy inputs and successful validation run ID. It
   requires `M` to be the exact remote `main` tip with sole parent `S`, the
   remote tag to resolve to `S`, the still-immutable candidate ref to equal `M`,
   exactly two published assets, and the retained run and bytes to predate
   publication. It rechecks `main`, the candidate ref, and the tag just before
   retaining its read-only correspondence receipt. Keep the temporary candidate
   ref until this workflow succeeds.

The campaign verifier derives any milestone number and boundary from the fixed
campaign origin `1786193837` (2026-08-08 20:57:17 AWST); a caller cannot choose
either. Every release source and validation must be at or after the first fixed
24-hour boundary (2026-08-09 20:57:17 AWST). The workflows validate and retain
bytes; they never tag, publish, or move `main` themselves.

Before every candidate, tag, or `main` push, inspect the effective destination:

```bash
git remote get-url --push origin
```

Stop unless it is the intended writable fork. In particular, never push any
Refresh Kit or Jellyfin Enhanced work to `n00bcodr/Jellyfin-Enhanced`.

## Run tests

Use the repository entry point:

Prerequisites are Node.js 20 or newer (the repository pins `22.20.0`), `npm ci`
with the locked Puppeteer/Chromium package, the exact .NET SDK `10.0.302`, and
installed .NET Core plus ASP.NET Core 9.x and 10.x runtimes for the dual-runtime
tests. Packaging also requires Python 3 and the documented GNU/Linux shell tools
(`bash`, `curl`, `flock`, `readlink -f`, `sha256sum`, `tar`, and `timeout`).
Static validation downloads a checksum-pinned `actionlint` archive into a
temporary user cache on first use and requires Docker CLI with Compose for
configuration parsing; container suites require a working Docker engine. The
security audit requires access to the live NuGet advisory feed.

```bash
./test.sh fast             # packages, both .NET targets, Chromium, static checks
./test.sh dotnet           # standalone compile plus xUnit on actual net9 and net10 runtimes
./test.sh browser          # Chromium runtime regressions
./test.sh reproducibility  # path-isolated byte identity and controlled build locking
./test.sh security-audit   # current NuGet advisory audit
./test.sh integration      # pinned JF10/JF12 lifecycle/browser lab plus proxy matrix
./test.sh compatibility    # all locked third-party/hostile-fixture matrices
./test.sh all              # every gate above; intentionally long-running
```

The read-only **Locked ecosystem compatibility** workflow runs all 14 pinned
matrices weekly and can be manually dispatched for an exact source revision.
It retains the collector's sanitized, completeness-checked evidence artifact.

The fast and focused suites cover, among other invariants:

- install/enable/disable state changes
- same-version loaded-module replacement with a new MVID
- client-asset changes
- per-plugin and process-wide scan budgets, including failed-read reservation
- middleware validators, conditional requests, compression, late headers, and concurrency
- HTML tokenizer and malformed-input fail-safe behavior
- malformed or transient metadata
- configuration-change behaviour
- eligible and ineligible script/style tags
- idempotent stamping
- existing version identities
- query-string and fragment preservation

## Run individual Docker labs

The repository has three disposable labs under `e2e/`; none should be pointed
at an existing Jellyfin installation. For example, the proxy lab can be run
individually as follows:

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

See [e2e/proxy/README.md](e2e/proxy/README.md), [e2e/jellyfin/README.md](e2e/jellyfin/README.md), and [e2e/compat/README.md](e2e/compat/README.md) for prerequisites, cleanup boundaries, pinned inputs, and retained evidence.

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

## AI-assisted development

This project uses AI-assisted development and review. Behavioural claims are therefore grounded in reproducible source inspection, xUnit and Chromium regressions, deterministic package verification, disposable real-server labs, and the evidence recorded in [COMPATIBILITY.md](COMPATIBILITY.md), rather than in a model's assertion.

---

## License

Jellyfin Refresh Kit is licensed under the [MIT License](LICENSE).
