# Jellyfin Refresh Kit

Jellyfin Refresh Kit prevents Jellyfin web clients from getting stuck on stale plugin JavaScript and CSS after plugins change.

If you run a Jellyfin server, install the standalone plugin once and it handles the common cache-staleness path for every plugin on the server. If you write a Jellyfin plugin, the repository also provides small reusable JavaScript and C# helpers (the drop-in files) for plugins that need direct control over assets they create at runtime.

The goal is simple: **users should not need to hard-refresh Jellyfin just to receive the current plugin UI.**

## Why stale plugin code happens

There are three different places stale code can survive, and each needs a different fix:

1. **The app shell** — Jellyfin's `index.html` and the scripts/stylesheets referenced directly from it. The server needs to return a page the browser re-checks with the server (revalidates) instead of reusing from its cache.
2. **Runtime-created assets** — scripts, stylesheets, `fetch()` URLs, dynamic imports, and CSS assets created after the page has loaded. The plugin that creates those URLs needs to version them.
3. **Already-open tabs** — a browser tab that never reloads never asks the server for the new page or assets. It needs to detect a plugin change and reload at a safe time.

The standalone plugin handles the app shell and open-tab refreshes server-wide, and cache-busts eligible plugin scripts and stylesheets already present in the shell (it adds a version query to their URLs, so a change produces a new URL the browser has never cached). The drop-in runtime gives plugin authors control over runtime-created assets as well.

## Is this for me

- **You run a Jellyfin server** and users have to hard-refresh to see a plugin's new UI after an update: install the standalone plugin. Keep reading — [Install](#install) is next.
- **You write a Jellyfin plugin or script** and need to version assets your own code creates at runtime, or need `ETag`/`Cache-Control` control from C#: see [docs/plugin-authors.md](docs/plugin-authors.md).
- **You want to build, test, or release this repository:** see [docs/contributing.md](docs/contributing.md).

| | Standalone plugin | Drop-in integration |
| --- | --- | --- |
| Best for | Jellyfin server admins | Plugin and script authors |
| Install | One Jellyfin plugin | `jellyfin-refresh-kit.js`, optionally with `RefreshKit.cs` |
| Covers | The common stale-cache path for plugins across the server | Your plugin, including assets it creates dynamically |
| Start here | [Install](#install) | [docs/plugin-authors.md](docs/plugin-authors.md) |

Both approaches can coexist on the same Jellyfin page. A plugin can use its own Refresh Kit integration while the standalone plugin handles server-wide change detection and static plugin tags.

- **License:** [MIT](LICENSE)
- **Compatibility evidence:** [COMPATIBILITY.md](COMPATIBILITY.md)
- **Enhanced adoption readiness:** [validated integration, release receipts, and limits](docs/enhanced-readiness.md)
- **Detailed standalone-plugin reference** (mechanism internals, the anonymous-endpoint security statement, the proxy test matrix, diagnostics fields, and the full limitation list): [plugin/README.md](plugin/README.md)
- **How the standalone plugin works:** [docs/how-it-works.md](docs/how-it-works.md)

## Install

Install one plugin and Refresh Kit watches the plugin environment for changes. Other plugins do not need to know it is installed.

### Requirements

- Jellyfin **10.11.x** or **12.x**
- Permission to install plugins and restart the Jellyfin server

The standalone plugin is built for both from one source tree: a `net9.0` build compiled against Jellyfin `10.11.0` (the oldest supported 10.11 ABI, so any later 10.11.x server can load it) and a `net10.0` build compiled against the Jellyfin `12.0.0-rc4` packages. Package verification rejects a build whose `MediaBrowser` assembly references do not match its declared `targetAbi`. One plugin-repository URL serves both — the server picks the build matching its own Jellyfin version, so you only choose a file when installing manually.

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

## What happens after install

After the restart, Refresh Kit computes a server-wide **generation**: a short opaque token derived from the plugin code, client assets and (by default) plugin settings the server is actually running, which changes whenever any of them change. It then serves `index.html` through its middleware (a server-side filter that rewrites the page on its way to the browser), **stamps** other plugins' eligible script and stylesheet tags — appends `?rkv=<generation>` to their URLs — and injects its own small browser runtime, which polls the generation from every open tab.

When a monitored change takes effect — a plugin installed, upgraded, enabled, disabled, or removed and then loaded by the required restart; a plugin's loose client asset (a `.js`, `.mjs`, `.css` or `.html` file in its plugin folder) changed; or (by default) a plugin's settings saved — the generation moves and eligible open tabs reload once, at a moment the reload **safety gates** (no playback, no open dialog, no typing, user idle) consider safe. Nothing else needs configuring.

The three mechanisms, what goes into the generation, and the reload safety gates are described in [docs/how-it-works.md](docs/how-it-works.md).

## Admin settings

Open **Dashboard → Plugins → Jellyfin Refresh Kit**.

| Setting | Default | Purpose |
| --- | ---: | --- |
| Serve index.html through the refresh kit | On | Master switch for the middleware. When off, Jellyfin's `index.html` passes through unchanged — no stamping and no runtime injection, so the next two settings have nothing to act on — while this settings page and the public generation/runtime endpoints keep working. |
| Cache-bust other plugins' script tags | On | Appends `?rkv=<generation>` to eligible unversioned plugin scripts and stylesheets in the shell. |
| Reload open tabs after a plugin update | On | Performs safe automatic reloads. When off, update detection remains available without automatic reloads. |
| Treat plugin settings changes as updates | On | Includes each plugin's saved configuration (its Jellyfin configuration XML) in the generation, so saving a plugin's settings counts as an update. |
| Settings-change cooldown | 5 min | Per plugin: the first settings change publishes promptly and opens a window this long; further changes inside the window are merged into one update when it ends. `0` disables the cooldown; the 10-second debounce (a change must stay stable for 10 seconds before it is published) still applies. Range 0–1440 (a day), clamped by the settings page and again by the server. |
| Ignore settings changes from these plugins | Empty | One entry per line. Accepts the plugin's display name, its install folder (with or without the version suffix), its GUID in any common form, or an assembly name. An assembly-name entry matches every assembly a plugin loads, including bundled dependencies, so `Newtonsoft.Json` would exclude each plugin that ships that DLL. Prefer the next setting when only one element of a plugin's settings is noisy. |
| Ignore these settings elements | Enhanced 12.8 bookkeeping | One entry per line: a top-level element of a plugin's configuration XML that is left out of its settings identity. `Element` applies to every plugin; `Plugin:Element` limits it to one plugin, using the same plugin forms as above. The shipped entries come from the plugin's built-in registry and cover Jellyfin Enhanced 12.8 by GUID, whose translation-cache task rewrites `ClearTranslationCacheTimestamp` at every server start and whose optional usage analytics rewrite `Analytics*` receipts on a multi-day timer; without them every Jellyfin restart would reload every open tab. Plugins can also declare their own bookkeeping in a `RefreshKitIgnoredElements` element of their settings, which is always honoured and needs no entry here. A settings file that is not well-formed XML is hashed as exact bytes instead. The Diagnostics section lists the names in effect per plugin. |
| Poll interval | 60 sec | How often a visible tab asks the server for the current generation; hidden tabs do not poll and catch up when shown. Range 15–3600 seconds, clamped by the browser runtime. |
| Required idle time | 5 sec | Minimum time since the user's last click, key or scroll before an automatic reload. Range 0–300 seconds, clamped by the browser runtime; `0` leaves only a fixed 1-second settle. |
| Max reloads per minute | 3 | Ceiling on automatic reloads per rolling minute, shared by every Jellyfin tab of the same origin in one browser. Range 1–100, clamped by the browser runtime. A reload the budget refuses waits; it is not lost. If the browser's shared ledger (IndexedDB) is unavailable, the reload also waits. |
| Developer mode | Off | Serves the embedded browser runtime with `no-store` instead of immutable caching, for debugging this plugin. |

Clearing a numeric field saves its default rather than zero. `0` is a real,
distinct value where the range allows it: `0` in **Settings-change cooldown**
disables the cooldown, and `0` in **Required idle time** leaves only the fixed
1-second settle.

Saving this page is itself a plugin settings change: with settings watching on,
open tabs reload once after the 10-second debounce and their next poll (so up
to roughly two poll intervals later), which is how new poll, idle and budget
values reach them.

### Excluding noisy configuration files

If a plugin updates its normal configuration XML frequently even when an administrator is not changing settings, add it to **Ignore settings changes from these plugins**.

The **Diagnostics** section of the settings page (described under [Verifying it works](#verifying-it-works)) shows, per plugin, the loaded assemblies, the asset and configuration scan counts and budgets, truncation/unavailability flags, and the configuration signals behind the current generation.

## Verifying it works

After startup:

- **Dashboard → Plugins** should show **Jellyfin Refresh Kit** as active.
- `GET /RefreshKit/Generation` should return JSON.

The plugin's settings page also has a **Diagnostics** section: press **Show diagnostics** to see what the current generation is made of, one row per loaded plugin. It fetches the admin-only `/RefreshKit/Diagnostics` endpoint with your session token, so no API token is needed there.

### HTTP endpoints

| Endpoint | Access | Purpose |
| --- | --- | --- |
| `GET /RefreshKit/Generation` | Anonymous | Returns `{ Version, BuildId, CacheKey, Epoch }`; `CacheKey` contains the current generation and `Epoch` identifies this server process. |
| `GET /RefreshKit/Generation.txt` | Anonymous | Returns the current generation as plain text. |
| `GET /RefreshKit/kit.js` | Anonymous | Serves the embedded browser runtime. |
| `GET /RefreshKit/Diagnostics` | Admin | Returns the current generation and the per-plugin inputs used to build it, read as one atomic snapshot, plus scan budgets, truncation/unavailability flags, skipped reparse-point configuration files and asset entries, unreadable asset entries, and stamping abort and failure counters. |

The generation and runtime endpoints are intentionally available before login so a stale Jellyfin login page can also detect a plugin change.

`BuildId` and `CacheKey` are opaque comparison/cache-busting tokens. Their
current derivation is an implementation detail: anything that reads them (a
script, a health check, another plugin) must not parse their segments, lengths,
or hash form, and should only compare or propagate the whole string. `Epoch` is also opaque. It is stable only for one loaded server process,
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
[What an anonymous observer can and cannot learn](plugin/README.md#what-an-anonymous-observer-can-and-cannot-learn)
in `plugin/README.md` for the full statement.

## Reverse proxies, CDNs, and BaseUrl

Normal reverse-proxy configurations should not need Refresh Kit-specific changes. The repository includes E2E coverage for direct Jellyfin access, nginx, Nginx Proxy Manager-style nginx, Caddy, Traefik, HAProxy, and subpath deployments.

For correct freshness, proxies and CDNs should:

- respect origin `Cache-Control` directives
- forward conditional request headers such as `If-None-Match` and `If-Match`
- avoid caching `/RefreshKit/Generation`
- preserve content encoding correctly, or re-encode with matching response headers

### Avoid forced caching that ignores Jellyfin's headers

A proxy that caches Jellyfin while explicitly ignoring origin cache directives can keep serving a stale copy of both the app shell and the generation endpoint, so no tab ever learns that anything changed.

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

Runtime 2.5.0 also protects Enhanced review drafts and unsaved admin settings after focus moves away (`unsaved_work`), and runtime 2.5.1 treats Enhanced's own overlays (its settings panel, Seerr more-info modal, bookmark, hidden-content and multi-select overlays, the active-streams panel and the Elsewhere streaming-settings modal) as open dialogs (`dialog`). Adopting plugins can register application reload guards for other drafts and saves (`reload_guard`), or mark an element with `data-refresh-kit-unsaved`; see [the integration guide](docs/plugin-authors.md#protect-application-work-runtime-250).

The returned object includes the current reload `blockReason`, naming the safety gate that is holding the reload (the gates are listed in [docs/how-it-works.md](docs/how-it-works.md#safe-automatic-reloads)).

A common case is `password_entry`: Refresh Kit will not automatically reload while a rendered, enabled, non-inert password field still contains a value. Two exceptions: a populated login field that Jellyfin keeps in its hidden login page after you sign in is ignored, and (since runtime 2.4.9) a password the browser autofilled on the login or select-server page is ignored while nobody has typed into a text field and no trusted click or keypress has happened since the runtime booted, because the browser will fill it again after the reload.

### The generation keeps changing

Open the plugin's settings page and press **Show diagnostics**, or call the
admin-only endpoint with your API token (a plain browser tab gets a 401):

```text
GET /RefreshKit/Diagnostics
```

Check which loaded assembly identity, active client asset, or watched configuration content is changing. If a plugin's configuration XML is intentionally noisy, add it to the configuration-watch exclusion list or adjust the cooldown.

### Plugin tags are not stamped

Refresh Kit can only stamp tags that are already in `index.html` when its transform runs. A plugin whose middleware runs outside Refresh Kit's adds its tag afterwards, so that tag cannot be stamped; which plugin is outermost follows Jellyfin's plugin load order, which no plugin can choose.

Check the served `index.html` and response headers. The plugin that owns an unstamped runtime or later-injected asset can adopt Refresh Kit directly if it needs stronger control over its own cache identity.

### Pages are stale only behind a proxy

Compare `/web/index.html` and `/RefreshKit/Generation` through the proxy with the Jellyfin origin. Forced proxy caching or ignored origin cache directives are the usual cause.

## Standalone-plugin limitations

Refresh Kit closes the common stale-plugin path, but it cannot control every way another plugin may load code.

- **Runtime-created assets remain the owning plugin's responsibility.** Dynamic imports, `fetch()`, JavaScript-created resources, CSS `url()`, and similar URLs do not exist in `index.html` for the standalone stamper to rewrite.
- **A directly retained pre-2.4.6 `document.createElement` wrapper cannot be retrofitted.** When two plugins ship different copies of the browser runtime, the newest copy takes over the page (a *handoff*) and the older copy's `document.createElement` wrapper goes inert. Code that kept a direct reference to the exact released 2.4.2 wrapper then calls a pass-through: elements it created before the handoff, and calls that go through the current wrapper, remain versioned, but wrappers created by 2.4.6 and newer are the first that forward such direct calls to the newest copy.
- **Middleware ordering matters.** Refresh Kit can only stamp tags already present when its HTML transform runs. A later/outer middleware can add tags it never sees or replace response headers afterward.
- **A real `<noscript>` disables the complete stamping transform.** Server-side code cannot know the user agent's scripting flag, and under scripting-disabled HTML parsing an effective `<base>` inside `<noscript>` can change a relative asset's origin. `ThirdPartyTagStamper` therefore leaves the whole stamping input byte-for-byte unchanged.
- **A document with more than 512 simultaneously open elements disables the stamping transform.** The tokenizer scans its open-element list per end tag, so an unbounded list would make pathological nesting quadratic inside the shell transform. Past that ceiling the pass is abandoned and the shell is served byte-for-byte unchanged. Both this abort and the `<noscript>` one are counted in admin diagnostics.
- **A quoted legacy `PUBLIC`/`SYSTEM` doctype is a conservative transform boundary.** The bounded tokenizer does not implement the complete HTML doctype state machine, so such a shell is served unchanged. Jellyfin's ordinary `<!doctype html>` remains transformable.
- **A real document `<base href>` disables runtime injection.** Refresh Kit's own URL is relative so it follows Jellyfin's configured PathBase; any effective base could redirect that URL to another path or origin. Bases inside inert template content do not trigger this boundary.
- **When another middleware owns the final response bytes, Refresh Kit degrades safely.** It still serves the complete transformed shell, but as `no-store` and without its strong ETag, preserving the outer owner's final framing. The page is always fresh, but the browser cannot use a conditional request to get a cheap `304`.
- **A response that is already started when Refresh Kit runs is passed through.** An outer middleware that commits headers before Refresh Kit's turn (early headers, a streamed prelude) gets the host's bytes untouched — Refresh Kit never throws into the pipeline for it — and a downstream that starts the response itself without a Refresh Kit ETag is finalized like any other late-started source response.
- **A request that a downstream handler authenticates is served privately, not shared.** Forward-auth style headers Refresh Kit does not recognise (`X-SSO-User` and the like) look anonymous to the shared cache; when the source then confirms with `304` that the cached bytes are exactly what it would serve, that request gets them as `no-store` and the shared entry stays untouched. The shell is never answered with a `503`.
- **An outer-owned tag can remain unstamped.** In both audited install orders of the GetAvatar plugin, its one eligible tag is added after Refresh Kit's transform and is explicitly reported as a compatibility limitation. Reloading the shell cannot guarantee fresh bytes for that unchanged URL.
- **Cross-origin assets are not rewritten.** Refresh Kit does not alter third-party CDN URLs or their cache semantics.
- **Generation is server-wide.** A monitored change to any plugin can make eligible open Jellyfin tabs reload once.
- **Broken intermediary caching still wins.** A proxy or CDN configured to ignore origin cache directives can serve stale content regardless of the origin's behaviour.
- **Polling is suspended while a tab is hidden.** A hidden tab holds no poll timer at all (only the single hidden-settle shot for a reload that is already pending), so a new generation is detected when the tab is shown again — or by a request that was already in flight when it was hidden — not while it sits in the background. The hidden-settle shot is additionally subject to browser timer throttling and freezing.
- **Cross-tab budget coordination starts with runtime 2.4.7.** Tabs running an older runtime copy still write the legacy numeric-v1 storage history but do not update the authoritative IndexedDB ledger inside its transaction, so they cannot be included in the concurrent-reservation guarantee until they load 2.4.7 or newer. Unavailable or corrupt IndexedDB safely defers automatic reload; unreadable/corrupt legacy stores do the same only while the first authoritative record still needs migration.

## Compatibility

The standalone plugin declares support for **Jellyfin 10.11.x and Jellyfin
12.x**. Its exact build inputs are Jellyfin Controller/Model `10.11.0` on
`net9.0` (`targetAbi` `10.11.0.0`) and `12.0.0-rc4` on `net10.0`
(`targetAbi` `12.0.0.0`); the disposable Docker test labs pin Jellyfin
`10.11.11` and `12.0.0-rc4` images by digest, and the Enhanced adoption lab
pins `10.11.11` and `12.1`. A harness or declared range is not evidence that
every future minor passed; current-candidate results and exact snapshot
identities are recorded separately in [COMPATIBILITY.md](COMPATIBILITY.md).

The current compatibility inventory classifies all 101 rows in the audited Awesome Jellyfin plugin section. The compatibility gate freshly downloads and cryptographically inspects all 44 immutable archives, retains an exact lock-ordered receipt, and exercises the 40 testable runtime artifacts across 14 runtime matrices (test scenarios); the three quarantined and one unsupported archives remain inspection-only. Three outer-response-buffer matrices use the statically enforced safe-degrade contract. The detailed environments, tested plugin builds, verdicts, edge cases, and reproducible evidence live in [COMPATIBILITY.md](COMPATIBILITY.md).

Keeping the evidence in that file allows this README to describe the supported behaviour without turning into a test ledger.

## For plugin authors

The repository also provides two drop-in files for plugins that need direct cache/version control over assets they create at runtime: [`jellyfin-refresh-kit.js`](jellyfin-refresh-kit.js), the dependency-free browser runtime, and [`RefreshKit.cs`](RefreshKit.cs), the self-contained C# server helper. Integration, bootstrap mode, the JavaScript configuration and API, `RefreshKitOptions`, multi-instance behaviour, and the drop-in limitations are documented in [docs/plugin-authors.md](docs/plugin-authors.md).

## Contributing and releases

Repository layout, building the plugin package, the `test.sh` validation modes, the disposable Docker labs, the release publication procedure, and the architecture rules contributors must preserve are in [docs/contributing.md](docs/contributing.md). Compatibility evidence rules and the current evidence ledger live in [COMPATIBILITY.md](COMPATIBILITY.md).

---

## AI-assisted development

This project uses AI-assisted development and review. Behavioural claims are therefore grounded in reproducible source inspection, xUnit and Chromium regressions, deterministic package verification, disposable real-server labs, and the evidence recorded in [COMPATIBILITY.md](COMPATIBILITY.md), rather than in a model's assertion.

---

## License

Jellyfin Refresh Kit is licensed under the [MIT License](LICENSE).
