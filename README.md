# jellyfin-refresh-kit

Jellyfin's web client caches plugin JavaScript hard. When a plugin ships a new
version, browsers keep running the old one — sometimes for days — until someone
presses <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>. This repository fixes
that, from two directions:

* **the server** makes sure a page load actually returns new bytes (a
  revalidating app shell, version-addressed asset URLs, cache-busting stamps on
  other plugins' script tags);
* **the browser** makes sure a page load happens at all — a tab left open for
  two days notices that the server moved and reloads itself, but only at a
  moment where a reload costs the user nothing.

You can consume it in either of two ways.

| | **Standalone plugin** | **Single-file drop-in** |
|---|---|---|
| For | server admins | plugin / script-collection authors |
| You install | one plugin, once | `jellyfin-refresh-kit.js` (+ optionally `RefreshKit.cs`) inside your own plugin |
| It covers | every plugin on the server, none of which need to know it exists | your plugin, including assets it creates at runtime |
| Get started | [Standalone plugin](#standalone-plugin-for-server-admins) | [Single-file drop-in](#single-file-drop-in-for-plugin-authors) |

The two are designed to run together: a server with the standalone plugin
installed and a plugin that ships its own copy of the kit will cooperate on one
page rather than fight over it.

* Licence: [MIT](LICENSE)
* Compatibility evidence: [COMPATIBILITY.md](COMPATIBILITY.md)
* Deeper plugin-specific documentation: [plugin/README.md](plugin/README.md)

---

## What goes stale, and what can fix it

There are three independent layers of staleness, and they need different tools:

1. **The app shell** — `index.html` and every `<script>` statically written
   into it. Only the server can fix this. The kit's C# half serves the shell
   through a revalidating middleware with a real ETag, and stamps unversioned
   tags so their URLs change when their content does.
2. **Sub-assets** — the scripts and stylesheets a plugin creates at runtime
   (`document.createElement('script')`, dynamic `import()`, CSS `url()`). The
   client runtime rewrites those URLs to carry `?v=<current version>`, so a new
   release is a new URL and can never come out of a stale cache entry.
3. **Open tabs** — a tab nobody has reloaded never re-requests anything. The
   client runtime polls a small version endpoint and, when the version changes,
   performs a *safe* auto-reload.

The standalone plugin covers layers 1 and 3 for every plugin on the server.
Layer 2 belongs to whoever created the asset, which is why plugin authors have
the drop-in path.

---

# Standalone plugin (for server admins)

Install **one** plugin and cache/hard-refresh behaviour is fixed for all your
other plugins. None of them need a code change, and none of them need to know
it is there.

## Requirements

* Jellyfin **10.11.x** (built against `Jellyfin.Controller` 10.11.11, `net9.0`
  — the framework the 10.11 server runs).
* No other dependencies. Reverse proxies and subpath/`BaseUrl` deployments are
  supported; see [Reverse proxies and CDNs](#reverse-proxies-and-cdns).

## Install

**Plugin repository (recommended)**

1. Dashboard → **Plugins** → **Repositories** → **+**
2. Repository name: `Jellyfin Refresh Kit`
   Repository URL:
   `https://raw.githubusercontent.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/main/manifest.json`
3. **Catalog** → **General** → *Jellyfin Refresh Kit* → **Install**
4. Restart Jellyfin.

**Manual folder install**

1. Download `jellyfin-refresh-kit_<version>.zip` from the
   [releases](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/releases).
2. Unzip it into a folder named `Jellyfin Refresh Kit_<version>` inside your
   Jellyfin config's `plugins` directory — e.g.
   `/config/plugins/Jellyfin Refresh Kit_1.0.0.0/`, containing
   `Jellyfin.Plugin.RefreshKit.dll` and `meta.json`. The `Name_version` folder
   layout is what the plugin loader expects; a folder without it is ignored.
3. Restart Jellyfin.

**Verify:** Dashboard → Plugins shows *Jellyfin Refresh Kit — Active*, and
`GET /RefreshKit/Generation` returns JSON.

## What it does

**1 — `index.html` is served fresh.** The plugin puts the refresh kit's
middleware in front of the web app shell. Every `/web/index.html` response
carries a strong, body-derived ETag (`"rk-…"`), answers `If-None-Match` with a
real `304` and `If-Match` with a `412`, preserves the host's
`Cache-Control`/`Vary` and content coding, handles `HEAD`, and **fails open** —
on any unexpected condition it serves the host's original bytes rather than
breaking your Jellyfin.

**2 — other plugins' script tags get a cache-busting stamp.** While it holds
the shell, the middleware appends `?rkv=<generation>` to `<script src>` and
`<link rel="stylesheet" href>` tags that carry no version of their own. It is
deliberately conservative: inline scripts, non-stylesheet links, cross-origin
URLs, tags that already carry a version parameter, jellyfin-web's own opaque
query convention and content-hashed filenames are all left untouched. Stamping
is idempotent — repeated passes converge instead of accumulating.

**3 — every plugin is watched, and open tabs reload safely.** An endpoint
reports one short **generation** token derived from *all* installed plugins:
each plugin's id, version and status from its `meta.json`, the newest write time
across its binaries and client assets, and the newest write time of its
plugin-configuration XML. Install, upgrade, uninstall, enable, disable, an
in-place binary swap, an edited script or a saved settings page all move it. The
embedded client runtime polls that generation and, when it changes, reloads open
tabs — safely (see below). One value does four jobs at once — the injected
tag's `?v=`, its `data-boot-version` seed, what the version endpoint reports,
and the `?rkv=` stamp — so they cannot drift apart.

### Settings changes count as updates

Plugins render config-driven UI at page load, so an admin enabling a feature
leaves every open tab showing UI built from the old settings. The plugin
therefore watches each plugin's **configuration XML** (the file the dashboard
writes when an admin saves) — and deliberately *not* the plugin's private data
directory, which holds per-user preferences and runtime caches. Watching that
would reload every client on the server because one user changed a personal
setting.

The signal is bounded so it cannot become a reload treadmill:

* **Debounced** — a new timestamp must stand still for 10 s, so a settings page
  that saves three times as you click produces one bump.
* **Cooled down on the leading edge** — a change arriving while no window is
  open publishes at once (your single save is live within seconds) and opens a
  window of *Settings-change cooldown* length. Only changes arriving *inside*
  that window are held, and they coalesce into one publish when it expires.
  Nothing is dropped.
* **Excludable** — turn config watching off globally, or list individual
  plugins to ignore.

Version and binary changes bypass both the debounce and the cooldown: a new
binary is unambiguous and rare.

### Safe reloads

A reload that interrupts something is worse than a stale tab, so the client
runtime refuses to reload unless the moment is genuinely free. In order, the
first refusal winning:

| Gate | Refuses while |
|---|---|
| `hidden` | the tab has been hidden for less than the settle grace (a hidden tab is *unwatched*, not *safe* — every gate below still applies afterwards) |
| `playback_route` | the video route is open |
| `fullscreen_media` | media is fullscreen |
| `dialog` | a dialog is open (overridden by Jellyfin's own screensaver, which is proof the user is gone) |
| `media_element` | a real playback session exists anywhere on the page (a parked or ambient backdrop video is not a session) |
| `active_editor` | a text field has focus |
| `password_entry` | any password field on the page holds a value |
| `not_idle` | the user has interacted more recently than the required idle time |

On top of the gates: reloads are spent against a **budget** (max reloads per
rolling 60 s window, shared across every tab), a version must be observed twice
before it arms a reload, and a tab refuses to reload back to a version it has
already left — so a flapping version source (a rolling deploy, round-robin
nodes disagreeing about the build) cannot loop a tab. If the tab is hidden and
has settled, the reload happens in that invisible moment instead of in the
user's face a second later.

## Admin settings

Dashboard → Plugins → **Jellyfin Refresh Kit**. Every switch is a kill switch:
you can turn off any part of the plugin without uninstalling it.

| Setting | Default | Meaning |
|---|---|---|
| Serve index.html through the refresh kit | on | Master switch. Off = the plugin is inert; host bytes pass through untouched. |
| Cache-bust other plugins' script tags | on | The `?rkv=` stamping pass. |
| Reload open tabs after a plugin update | on | Off switches the client runtime to `notify` mode: it logs the update instead of reloading. |
| Treat plugin settings changes as updates | on | Off falls back to version/binary-change detection only. |
| Settings-change cooldown (minutes, per plugin) | 5 | Length of the leading-edge burst window. 0 disables it; the 10 s debounce still applies. |
| Ignore settings changes from these plugins | empty | One per line: plugin name, install folder, GUID or assembly name. |
| Poll interval (seconds) | 60 | Clamped 15–3600 by the client runtime. |
| Required idle time (seconds) | 5 | Clamped 0–300. |
| Max reloads per minute | 3 | Clamped 1–100. |
| Developer mode | off | Serves the client runtime `no-store` instead of `immutable`. |

The default exclusion list is empty on purpose: of the plugins tested live, none
write churn into the watched configuration XML. Add one if you observe the
generation moving while nobody is changing settings —
`GET /RefreshKit/Diagnostics` shows exactly which plugin contributed which
timestamp.

## Endpoints

| Route | Auth | Purpose |
|---|---|---|
| `GET /RefreshKit/Generation` | anonymous | `{ Version, BuildId, CacheKey }`, `CacheKey` = the generation. Served `no-store`. |
| `GET /RefreshKit/Generation.txt` | anonymous | The bare generation, `text/plain`. |
| `GET /RefreshKit/kit.js` | anonymous | The embedded client runtime, `immutable` (its `src` carries `?v=<generation>`). |
| `GET /RefreshKit/Diagnostics` | admin | Per-plugin id / version / status / asset ticks / config ticks behind the current generation. |

The first three are anonymous deliberately: the login screen is a real page of
the web client, it is where a stale cache most often bites, and a tab can sit on
it for days. They disclose nothing a logged-out visitor cannot already see — an
opaque token and a public MIT-licensed script.

## Reverse proxies and CDNs

The plugin was validated behind the proxies people actually deploy — one
throwaway 10.11.11 origin, one proxy per port, an 18-assertion freshness matrix
plus a websocket check and a browser end-to-end run per setup. **nginx (official
Jellyfin docs config), Nginx Proxy Manager style, Caddy, Traefik v3, HAProxy and
an nginx subpath deployment all pass with no configuration at all**, as does no
proxy. Cloudflare's defaults are safe, because HTML is not cached by default.
Subpath/`BaseUrl` works untouched: the injected tags use relative URLs.

The one configuration that breaks it is a caching proxy told to ignore the
origin's cache directives:

```nginx
proxy_cache jfcache;
proxy_ignore_headers Cache-Control Expires;   # ← this line
```

That pins both the shell *and* the `no-store` generation endpoint, so the client
can neither get a new page nor be told one exists — and it turns one reload into
two. The remedy is to delete `Cache-Control` (and `Expires`) from
`proxy_ignore_headers`, and ideally to exempt `/web/` and `/RefreshKit/` from
the cache entirely. The exact nginx snippets, the Cloudflare equivalent, and the
full per-proxy matrix are in
[plugin/README.md](plugin/README.md#reverse-proxies--cdns); the measurements
behind them are in [COMPATIBILITY.md](COMPATIBILITY.md) and reproducible with
the rig in [e2e/proxy/](e2e/proxy/README.md).

## Limitations

* **Ordering.** The stamping pass can only see tags already in the response when
  it runs. Plugin startup filters compose in the host's plugin load order, which
  no plugin controls, so tags injected by a middleware that runs *outside* this
  one are not stamped — and such a middleware may also replace the shell's
  response headers, costing you the `rk-` ETag. Nothing breaks: those plugins
  keep whatever cache behaviour they had, and open tabs still get onto the new
  build. Details in
  [plugin/README.md](plugin/README.md#-the-ordering-caveat--read-this).
* **Runtime-created sub-assets are not covered.** A plugin that builds asset
  URLs at runtime needs to adopt the kit itself — see
  [Single-file drop-in](#single-file-drop-in-for-plugin-authors).
* **Cross-origin assets are never stamped.** A plugin loading its code from a
  CDN cannot be helped from the server; a CDN's own `@latest` resolution TTL is
  invisible to both server and browser. Pin a version or self-host.
* **The generation is server-wide.** Any plugin changing reloads tabs once —
  which is the point (unchanged plugins cost the reload nothing but cache hits),
  but a busy admin session can produce several reloads, bounded by the reload
  budget and the cooldown.
* **A hidden tab polls at the browser's mercy.** Background timers are throttled
  and frozen tabs run none at all; the worst case is that the reload waits until
  the tab is shown again.

## Troubleshooting

* **Tabs never reload.** Open the console and run
  `JellyfinRefreshKit.state()`. `shared.blockReason` names the gate that is
  refusing. `password_entry` is the common surprise: Jellyfin 10.11 keeps the
  login view in the DOM after sign-in with the typed password still in it, so a
  tab that signed in by typing refuses auto-reloads for the life of that
  document. A navigation clears it.
* **Nothing is stamped / the shell has no `rk-` ETag.** Another plugin's
  injection middleware is running outside this one — see the ordering
  limitation above. Check the shell's response headers.
* **Reloads happen too often.** Something is moving the generation. Check
  `GET /RefreshKit/Diagnostics` (admin) for the plugin whose timestamps keep
  changing, and either add it to the exclusion list or raise the cooldown.
* **Stale pages behind a proxy.** Check for `proxy_cache` in front of Jellyfin
  and apply the remedy above; `curl -I` the shell and `/RefreshKit/Generation`
  through the proxy and compare against the origin.

---

# Single-file drop-in (for plugin authors)

Copy the files into your own plugin. There is no package, no build step and no
dependency to add.

| File | What it gives you |
|---|---|
| [`jellyfin-refresh-kit.js`](jellyfin-refresh-kit.js) | Client runtime: versioned sub-asset URLs, version polling, safe auto-reload, optional bootstrap loading. Works for any plugin or script collection, including pure-JS ones with no C# at all. |
| [`RefreshKit.cs`](RefreshKit.cs) | Server half for C# plugins: request-time `<script>` injection into `index.html` with full HTTP caching semantics, a content-derived build identity, and cache-correct script serving. Optional, but it is what makes a reloaded tab actually receive new bytes. |

Multiple plugins may each ship their own copy: the copies register with a single
page-level manager, each configured by its own tag, and the newest copy on the
page manages it. Always ship the current file rather than an old one.

## Client runtime: `jellyfin-refresh-kit.js`

### Classic adoption

Serve the file from your plugin's static folder and give it one `<script>` tag:

```html
<script src="/web/MyCollection/jellyfin-refresh-kit.js"
        data-name="MyCollection"
        data-version-url="/web/MyCollection/version.json"
        data-version-json-field="version"
        data-asset-patterns="/MyCollection/">
</script>
```

Every script/stylesheet your code creates whose URL matches `assetPatterns` now
goes out as `…?v=<your version>`, and the tab reloads itself safely when
`version.json` changes.

### Bootstrap mode (recommended)

In classic mode your own entry files are still static, unversioned tags in
`index.html`, and every page load races the version fetch. Bootstrap mode
removes both problems: `index.html` carries one tag — the kit — and the kit
loads your entry files itself, after your version resolves.

```html
<script src="/web/MyCollection/jellyfin-refresh-kit.js"
        data-name="MyCollection"
        data-version-url="/web/MyCollection/version.json"
        data-version-json-field="version"
        data-asset-patterns="/MyCollection/"
        data-entry-scripts="/web/MyCollection/config.js,/web/MyCollection/injector.js">
</script>
```

Rules the entry loader follows, in priority order:

* never load an entry before the version resolves — but never let a dead version
  endpoint cost the user their plugin: the first fetch is raced against
  `entryTimeoutMs`, after which entries load unversioned with one warning
  (availability beats freshness);
* strict order within an instance, so a config script is guaranteed to have run
  before the injector that reads it;
* an entry that 404s or throws is logged and skipped — one bad file must not
  take the page down;
* `.css` entries become `<link rel="stylesheet">`, everything else `<script>`.

The only file left unversioned is the kit itself, which is why it is small and
deliberately stable. If you want the loader fresh too, that is a server job:
send `Cache-Control: no-cache` for that one file.

### Configuration

Options are set as `data-*` attributes on the kit's own `<script>` tag (the
kebab-case form of the option name), or as JavaScript objects on `window` —
`window.JellyfinRefreshKitConfigs = { "MyCollection": { … } }` keyed by instance
name, defined before the kit's tag. Priority per instance: keyed entry >
`window.JellyfinRefreshKitConfig` > `data-*` > defaults.

| Option / attribute | Default | Meaning |
|---|---|---|
| `name` / `data-name` | derived from `versionUrl` | Instance name. Appears in logs and is the key for the keyed window config. |
| `versionUrl` / `data-version-url` | — | Endpoint returning the current version. Required for polling. |
| `versionJsonField` / `data-version-json-field` | — | Parse the response as JSON and read this field. |
| `bootVersion` / `data-boot-version` | — | Identity of the build that served *this* document. Seeds the baseline so an update landing between page-serve and the first poll is detected instead of absorbed. Must name the same identity the version endpoint reports. |
| `pollSeconds` / `data-poll-seconds` | 60 | Poll interval while visible. Clamped 15–3600. |
| `idleSeconds` / `data-idle-seconds` | 5 | Required user-idle time before a reload. Clamped 0–300. |
| `assetPatterns` / `data-asset-patterns` | none | Comma-separated substrings (regexes via the config object). Matching script/link URLs get `?v=`. |
| `entryScripts` / `data-entry-scripts` | none | Bootstrap mode. Comma-separated, order significant. |
| `entryTimeoutMs` / `data-entry-timeout-ms` | 3000 | Max wait for the first version fetch before entries load unversioned. Clamped 250–30000. |
| `mode` / `data-mode` | `auto` | `auto` reloads, `notify` only fires the callback, `off` still versions URLs. |
| `reloadBudget` / `data-reload-budget` | 3 | Max reloads per 60 s window. The page uses the **minimum** across instances. |
| `hiddenReload` / `data-hidden-reload` | true | Allow the reload to happen while the tab is hidden. Any instance setting it false switches the whole page back to visible-only reloads. |
| `hiddenSettleSeconds` / `data-hidden-settle-seconds` | 25 | How long the tab must have been hidden first. The page uses the **maximum** across instances. |
| `getVersion` | — | Config-object only. A `() => Promise<string>` that replaces `versionUrl` entirely. |
| `onUpdateAvailable` | — | Config-object only. Called once per detected version change. |

Where instances disagree, the page honours the most conservative ask: the
strictest idle requirement, the smallest reload budget, the longest hidden
settle.

### Public API

`window.JellyfinRefreshKit` is the page manager:

| Member | Purpose |
|---|---|
| `get(name)` | The named instance's handle (`version`, `latestVersion`, `versionedUrl`, `checkNow`, `state`), or `null`. |
| `instances()` | Registered instance names, in registration order. |
| `versionedUrl(url, force)` | Version a URL built outside `createElement` — `fetch()`, dynamic `import()`, CSS `url()`. |
| `checkNow()` | Force an immediate version check on every instance. |
| `state()` | Full diagnostic snapshot: per-instance versions and config, plus shared reload state including `blockReason`. Start here in a support log. |

Every entry point is wrapped so a throw inside the kit cannot escape into
Jellyfin's own code.

## Server half: `RefreshKit.cs`

Drop the file into your plugin project and register it:

```csharp
using JellyfinRefreshKit;

serviceCollection.AddRefreshKit(new RefreshKitOptions
{
    PluginName  = "My Plugin",          // tag identity + scrub key
    BasePath    = "MyPlugin",           // your controller's [Route]
    ScriptPaths = new[] { "script" },
    DevMode     = () => Plugin.Instance?.Configuration.DevMode == true,
});
```

Every `index.html` now carries exactly one tag of yours:

```html
<script plugin="My Plugin" version="1.2.3-638…" data-boot-version="1.2.3-638…"
        dev="false" build="ab12…" src="../MyPlugin/script?v=1.2.3-638…" defer></script>
```

In the endpoint that serves your script, one call gives you
`public, max-age=31536000, immutable` in production and `no-store` in dev mode:

```csharp
[HttpGet("script")]
[AllowAnonymous]
public ActionResult GetScript()
{
    RefreshKit.ApplyScriptCacheHeaders(Response);
    return Content(js, "application/javascript");
}
```

Optionally expose a version endpoint for the JS kit to poll. It is opt-in
because two plugins embedding this file would otherwise claim the same route:

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

…and point the kit at it:

```csharp
ExtraAttributes = _ => "data-version-url=\"../MyPlugin/RefreshVersion\" "
                     + "data-version-json-field=\"CacheKey\"",
```

`CacheKey` is the right field because it is exactly what the injected tag stamps
into `data-boot-version` — the seed and the polled value then name the same
identity.

### `RefreshKitOptions`

| Option | Required | Meaning |
|---|---|---|
| `PluginName` | yes | Tag identity. Every injected tag carries `plugin="<PluginName>"`, and the scrub regex removes exactly those, so two plugins never scrub each other. Pick it once — changing it orphans the old tag. |
| `BasePath` | yes | The route segment your controller serves under. Injected srcs are `../{BasePath}/{scriptPath}?v={cacheKey}`, relative on purpose so they keep working behind a base-url prefix. |
| `ScriptPaths` | yes | Ordered script paths. **This list is the execution order** (every tag is `defer`). If you also serve `jellyfin-refresh-kit.js`, it must be the **first** entry — anything running before it creates sub-assets unversioned. |
| `DevMode` | no | Live flag stamped into the tag as `dev="true"` / `dev="false"` and read by `ApplyScriptCacheHeaders`. |
| `VersionProvider` | no | Overrides the assembly-derived identity, e.g. to make one aggregate value drive everything. |
| `ExtraAttributes` | no | Extra attributes per script path — how the JS kit's `data-*` config is emitted. |
| `Enabled` | no | Live kill switch: when false the middleware passes the host's bytes through untouched. |

Static helpers: `RefreshKit.BuildId`, `RefreshKit.CacheKey`, `RefreshKit.Version`,
`ApplyScriptCacheHeaders`, `ApplyNoStore`, `BuildScriptTags`,
`OwnScriptTagRegex`, `ReplaceOwnedScriptTags`.

Design guarantees worth knowing: the file references no plugin-specific types,
the middleware is thread-safe with a bounded cache, and every error path
**fails open** — it sits in front of the app shell and must never be the reason
Jellyfin fails to load. Two plugins can each embed the file; identical namespaces
in different assemblies are distinct types with distinct statics, so each gets
its own middleware, cache and scrub identity.

## Limitations of the drop-in path

* **Classic mode races the version fetch on every load.** Anything your
  bootstrap creates synchronously at parse time goes out unversioned. Bootstrap
  mode removes the race; `bootVersion` narrows it.
* **The kit's own tag is the one URL nobody can version from inside.** Something
  has to be the loader. Keep it stable, or serve it `no-cache`.
* **Bootstrap mode adds one round trip** before entries load, bounded by
  `entryTimeoutMs`.
* **Overlapping `assetPatterns` are resolved by registration order** (document
  order of the kit tags), with one warning. Keep patterns scoped to your own
  folder.
* **A flapping version source cannot be made stable from the client.** Serve one
  identity per release across all nodes.
* **A CDN's `@latest` resolution TTL is invisible to JavaScript** — the stale
  file *is* the correct response for that URL. Pin a version or self-host.
* **The kit cannot add `ETag` or `Cache-Control` headers.** That needs the
  server half.

---

# Compatibility

Full, per-plugin evidence lives in [COMPATIBILITY.md](COMPATIBILITY.md); nothing
appears there without a passing run. In summary:

* **Around 130 community plugin builds** validated on live Jellyfin 10.11.11
  servers across four sweeps — a 34-plugin breadth sweep, a 103-build ecosystem
  completion sweep (102 third-party plugins Active at once), and kitchen-sink
  environments of 8 and 22 concurrent plugins with up to five independent
  `index.html` rewriters running simultaneously. Verdicts are `coexists`
  (functional with the kit present, its own URLs untouched, network-level
  non-interference proven) and `adoptable`.
* **Reverse proxies and CDNs**: nginx (official docs config), Nginx Proxy
  Manager style, Caddy, Traefik v3, HAProxy, an nginx subpath deployment and no
  proxy all pass the full freshness matrix; the failure mode and its one-line
  remedy are the caching-proxy rows.
* **`RefreshKit.cs` on Jellyfin 12.0-rc3** — verified against a clean-room
  demo plugin (idempotent injection, stale-tag scrub, `rk-` ETag with
  per-encoding 304/412, gzip/br, immutable scripts, dev-mode live flip). The
  standalone plugin itself targets 10.11.x.

---

# Development

## Repository layout

```
jellyfin-refresh-kit.js                       the client runtime (single source of truth)
RefreshKit.cs                                 the C# half, for single-file adoption
manifest.json                                 plugin-repository manifest
COMPATIBILITY.md                              evidence log
plugin/
    build.sh                                  build → zip + meta.json + md5
    README.md                                 deeper standalone-plugin documentation
    Jellyfin.Plugin.RefreshKit/
        Jellyfin.Plugin.RefreshKit.csproj     net9.0, Jellyfin.Controller 10.11.11
        Plugin.cs                             plugin identity, embedded runtime
        PluginServiceRegistrator.cs           wires all three mechanisms
        PluginGenerationProvider.cs           the generation aggregator
        ThirdPartyTagStamper.cs               the ?rkv= stamping rules
        RefreshKit.cs                         vendored from the repository root
        Controllers/RefreshKitController.cs   generation / kit.js / diagnostics
        Configuration/                        settings + dashboard page
    Jellyfin.Plugin.RefreshKit.Tests/         xunit suite
e2e/proxy/                                    reverse-proxy / CDN validation rig
```

## Build

```bash
export DOTNET_ROOT=$HOME/.dotnet
bash plugin/build.sh                  # or: --update-manifest
```

Produces `plugin/build/jellyfin-refresh-kit_<version>.zip` (DLL + `meta.json`)
plus an unzipped `plugin/build/stage/` you can drop straight into
`/config/plugins/Jellyfin Refresh Kit_<version>/`, and prints the zip's MD5.
With `--update-manifest` it writes that checksum and timestamp into the root
`manifest.json`. Attach the zip to a GitHub release tagged `v<version>` so the
manifest's `sourceUrl` resolves.

The version lives in exactly one place — `<Version>` in the csproj. `build.sh`,
`meta.json` and the manifest update all read it from there.

## Test

```bash
export DOTNET_ROOT=$HOME/.dotnet
$DOTNET_ROOT/dotnet test plugin/Jellyfin.Plugin.RefreshKit.Tests/Jellyfin.Plugin.RefreshKit.Tests.csproj
```

49 xunit tests covering the two pieces of logic with real edge cases: the
generation aggregator (enable/disable detection, in-place DLL replacement,
client-asset vs runtime-data files, bounded directory walks, torn `meta.json`
rewrites) and the third-party tag stamper (which tags are stamped, which are
skipped, idempotency, byte-exact preservation of everything else).

## End-to-end: the proxy rig

[`e2e/proxy/`](e2e/proxy/README.md) builds its own throwaway world — its own
Jellyfin origin, volumes and network, plus eleven proxy containers — and never
touches a pre-existing Jellyfin container.

```bash
cd e2e/proxy
export NODE_PATH=$HOME/.nvm/versions/node/v22.20.0/lib/node_modules   # puppeteer + ws
./run.sh up        # rig + wizard + both plugins + admin token
./run.sh matrix    # 18-assertion curl freshness matrix, every proxy
./run.sh ws        # websocket regression check, every proxy
./run.sh cache     # misconfigured-cache demo and both remedies
./run.sh e2e       # puppeteer: login → bump → exactly one smart reload
./run.sh subpath   # BaseUrl=/jellyfin, test, restore
./run.sh all       # the lot, in order (~25 minutes)
./run.sh down      # destroy everything
```

Requirements: Docker with the compose plugin, `node` with `puppeteer` and `ws`
resolvable, `python3`, and the .NET SDK only if `plugin/build/stage/` is missing
(the rig then builds the plugin for you). Run it for any change that touches the
middleware, the injected tag, or anything that could affect a proxied response.

## Architecture notes

* **One copy of the client runtime.** The csproj embeds
  `../../jellyfin-refresh-kit.js` from the repository root as an embedded
  resource at compile time, and the controller serves it from there. There is
  deliberately no committed duplicate inside the plugin.
* **`RefreshKit.cs` is vendored, not linked.**
  `plugin/Jellyfin.Plugin.RefreshKit/RefreshKit.cs` is the root file with
  exactly three differences, each marked `STANDALONE-PLUGIN ADAPTATION`: the
  namespace, an added `RefreshKitOptions.HtmlPostProcess` hook, and its call
  site. Those hooks do not exist upstream and the root file belongs to the
  single-file adoption path. Re-sync after changing the root file with the `sed`
  recipe in the vendored file's header, then re-apply the two hooks.
* **The generation is the version.** The plugin overrides `VersionProvider` with
  the aggregate generation, so the injected `?v=`, the `data-boot-version` seed,
  the version endpoint's `CacheKey` and the `?rkv=` stamp are one value that
  cannot drift — and a generation change also invalidates the middleware's
  cached representation of the shell.
* **The plugin's kit instance declares no `assetPatterns` and no
  `entryScripts`.** On a page where an adopting plugin ships its own copy, the
  first-registered matching instance wins URL versioning, and a greedy pattern
  here would silently take over that plugin's versioning. Layer 2 stays the
  adopter's business.
* **The client runtime is written to survive anything.** Interception happens at
  URL *assignment* time (wrapping `document.createElement` and installing
  per-instance accessors), not via `MutationObserver` — an observer sees the
  element after insertion, when the fetch has usually already started. Nothing
  is ever installed on `Element.prototype`.
* **Cross-copy compatibility is a frozen contract.** The registration contract
  documented in the runtime's header (`__registerInstance`, `__contractVersion`,
  `__claimSingularGlobal`, `__handoffTo`) is strictly additive: a caller
  speaking an older revision must keep working against every future manager.
  Add clauses; never change one.

## Style constraints

* **`jellyfin-refresh-kit.js` has no build step and no dependencies**, and it
  must stay that way — it has to be pasteable into a JS-injector textarea or
  served straight from a plugin folder. Write conservative ES5-syntax vanilla
  JavaScript (`var`, function expressions; no arrow functions, template
  literals, classes or `let`/`const`); there is no transpiler to save you.
  Keep the file one self-contained IIFE, keep every observable entry point
  wrapped so a throw cannot escape into the host page, and document behaviour in
  the header block — it is the reference the README summarises.
* **`RefreshKit.cs` must remain self-contained and fail-open.** No
  plugin-specific types, no assumption of a writable web root, and any
  unexpected condition serves the host's original bytes.
* **C# in the plugin project** targets `net9.0` with nullable enabled.

## Contributing

* Work on a branch; keep commits incremental.
* Add or update xunit tests for any change to the generation provider or the tag
  stamper — that is where the subtle rules live.
* Run the proxy rig for anything touching the middleware or the served shell.
* Update the docs that the change makes wrong: this README for user- or
  developer-visible behaviour, [plugin/README.md](plugin/README.md) for
  standalone-plugin detail, and the runtime/`RefreshKit.cs` header blocks for
  behaviour changes.
* Only add rows to [COMPATIBILITY.md](COMPATIBILITY.md) that a real run proved,
  citing the environment.

---

## License

MIT — see [LICENSE](LICENSE).
