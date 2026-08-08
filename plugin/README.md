# Jellyfin Refresh Kit — the standalone plugin

Install **this one plugin** and it watches every plugin on the server for you.
No other plugin needs to know it exists, and none of them need a code change.
It does three things:

* **serves a fresh app shell** — `index.html` goes out through a revalidating
  middleware with a real ETag, so the page itself can never be stuck;
* **cache-busts other plugins' `<script>` and stylesheet tags** that sit in that
  shell and carry no version of their own;
* **notices when any plugin changes** — installed, upgraded, enabled, disabled,
  reconfigured — and reloads open tabs *safely*: never during playback, never
  over a dialog, never while you are typing.

**What it does not do:** it cannot version assets a plugin creates at *runtime*
(a dynamic `import()`, a `fetch`, a CSS `url()`), and it cannot stamp tags
injected by a middleware that runs outside this one. Those two limits are real,
and they are spelled out in [the ordering caveat](#-the-ordering-caveat--read-this)
and [Known limitations](#known-limitations) below. A plugin that needs runtime
sub-assets versioned should adopt the kit directly — see
[Relationship to single-file adoption](#relationship-to-single-file-adoption).

So: for the common case — a plugin ships a `.js`, the shell references it, the
browser caches it forever — installing this fixes it, and you stop telling users
"press Ctrl+Shift+R".

It is the packaged form of this repository. The single-file adoption path
(`jellyfin-refresh-kit.js` + `RefreshKit.cs` copied into your own plugin) is
unchanged and still supported — see [the root README](../README.md). The two
coexist on one page by design.

---

## Install

### Method 1 — plugin repository (recommended)

1. Dashboard → **Plugins** → **Repositories** → **+**
2. Repository name: `Jellyfin Refresh Kit`
   Repository URL:
   `https://raw.githubusercontent.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/main/manifest.json`
3. **Catalog** → **General** → *Jellyfin Refresh Kit* → **Install**
4. Restart Jellyfin.

### Method 2 — manual folder install

1. Download `jellyfin-refresh-kit_<version>.zip` from the
   [releases](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/releases).
2. Unzip it into a folder named `Jellyfin Refresh Kit_<version>` inside your
   Jellyfin config's `plugins` directory — e.g.
   `/config/plugins/Jellyfin Refresh Kit_1.0.0.0/`, containing
   `Jellyfin.Plugin.RefreshKit.dll` and `meta.json`.
   The `Name_version` folder layout is what the server's plugin loader expects;
   a folder without it is ignored.
3. Restart Jellyfin.

Verify: Dashboard → Plugins shows **Jellyfin Refresh Kit — Active**, and
`GET /RefreshKit/Generation` returns JSON.

**Requirements:** Jellyfin **10.11.x** (built against `Jellyfin.Controller`
10.11.11, `net9.0`, which is what the 10.11 server runs).

---

## What it actually does

Three independent mechanisms. Each is useful alone; together they close the
whole loop.

### 1. index.html is served through a revalidating middleware

The plugin registers an `IStartupFilter` (via `IPluginServiceRegistrator`) that
puts the refresh kit's middleware in front of the web app shell. Every
`/web/index.html` response then carries a **strong, body-derived ETag**
(`"rk-…"`), answers `If-None-Match` with a real `304`, honours `If-Match` with a
`412`, preserves the host's `Cache-Control`/`Vary` and content coding, handles
`HEAD`, and **fails open**: on any unexpected condition it serves the host's
original bytes rather than breaking the page.

This is the same `RefreshKit.cs` machinery documented in the root README,
vendored into the plugin (see *Repository layout* below).

### 2. Other plugins' script tags get a cache-busting stamp

While it holds the shell, the middleware finds `<script src>` and
`<link rel="stylesheet" href>` tags that **other** plugins put there — whether
by patching `index.html` on disk or by injecting at serve time — and appends
`?rkv=<generation>` to the ones that carry no version of their own. When any
plugin changes, the generation changes, so those URLs change, so the browser
cannot serve them from cache.

It is deliberately conservative. A tag is **skipped** when:

| Skipped | Why |
| --- | --- |
| inline `<script>` (no `src`) | nothing to version |
| `<link>` that is not a stylesheet (manifest, icons, preload) | not client code; stamping can break them |
| absolute or protocol-relative URLs (`https://cdn…`, `//host/…`) | a third-party origin may key its cache/CORS/404 behaviour on the exact URL |
| already carries `?v=`, `?ver=`, `?version=`, `?hash=`, `?rev=`, `?build=`, `?cb=`, `?_=`, `?rkv=` … | somebody's deliberate versioning; leave it alone |
| an opaque valueless query (`?3cf5acc8506265662d4f`) | jellyfin-web's own bundle convention — already an identity |
| a content-hashed filename (`main.jellyfin.f725276386e5b19afe0c.css`) | already immutable per URL; restamping would throw away a warm cache for nothing |

Stamping is **idempotent**: any existing `rkv` is scrubbed and the current
generation restamped, so repeated passes and generation changes converge instead
of accumulating. Attribute order, quoting and whitespace are untouched — only the
characters inside the `src`/`href` value change.

#### ⚠ The ordering caveat — read this

The stamping pass can only see tags that are **already in the response when it
runs**. ASP.NET Core composes startup filters so the *first-registered* filter
ends up *outermost*, and plugin registrators run in the host's plugin load
order, which no plugin can control. Measured on a live 10.11.11 server with four
third-party plugins installed:

* **On-disk-patched tags** — always seen, always stamped.
* **Serve-time tags injected by a middleware INSIDE this one** — seen and
  stamped. (Observed: InPlayerEpisodePreview's
  `/InPlayerPreview/ClientScript` came back as
  `/InPlayerPreview/ClientScript?rkv=5p-…`.)
* **Serve-time tags injected by a middleware OUTSIDE this one** — appended to
  the response *after* this pass, so **not stamped**. (Observed: Jellyfin
  Enhanced's tag, which is self-versioned anyway and would have been skipped.)
  A plugin whose outer middleware rewrites the shell may also **replace the
  response headers**, which can cost you the `rk-` ETag from mechanism 1 —
  observed with Jellyfin Enhanced's injection middleware, which serves the shell
  as `Cache-Control: no-cache` with no validator.

Nothing breaks in that case: those plugins simply keep whatever cache behaviour
they already had, and mechanism 3 still gets every open tab onto the new build.
There is no supported way for a plugin to force itself outermost, so this is a
documented limit, not a bug that can be fixed from here.

### 3. A generation for every plugin, and a safe reload

An unauthenticated endpoint reports one short **generation** token derived from
*all* installed plugins. The embedded `jellyfin-refresh-kit.js` runtime is
injected (as the instance **`RefreshKitPlugin`**) pointed at that endpoint; it
polls, and when the generation changes it performs a **safe** reload — never
during playback, never over an open dialog, never while typing, never in
fullscreen, only after an idle window, and inside a reload budget.

The generation is `{plugin count}p-{16 hex}`, folded from, for every folder in
the plugins directory:

* the plugin **id** and **version** from its `meta.json` — install / uninstall /
  upgrade;
* its **status** from the same file — Jellyfin rewrites `"status"` in place when
  an admin enables or disables a plugin, and *nothing else moves*: same folder,
  same version, byte-identical binaries with untouched timestamps. Without this
  field a disable was invisible;
* the **newest write-ticks** across its binaries **and its client assets**
  (`.js`, `.mjs`, `.css`, `.map`, `.html`) — a same-version binary replaced in
  place, or a script edited without the DLL moving at all. Runtime data files
  (`.json`, `.db`, logs) are deliberately excluded: they churn on their own
  schedule with nothing user-visible behind them;
* the **newest write-time of its plugin-configuration XML** — see below.

The scan is bounded per folder (files *and* directories visited), so a plugin
shipping a large asset tree cannot turn it into a stat storm.

One value does four jobs — the injected tag's `?v=`, its `data-boot-version`
seed, what the version endpoint reports as `CacheKey`, and the `?rkv=` stamp — so
they can never drift apart, and a generation change also invalidates the
middleware's cached representation of the shell.

---

## Settings changes count as updates (and what that costs)

Plugins render config-driven UI at page load: enable a tab in a plugin's
settings and every open tab is now showing UI built from the old settings. So a
plugin's **configuration file** is watched too, and saving settings propagates to
open clients.

The signal is kept deliberately narrow, because a noisy generation means
pointless server-wide reloads:

* **Watched:** `plugins/configurations/<AssemblyName>.xml` — Jellyfin's own
  plugin-configuration store, the file the dashboard writes when an admin saves.
* **NOT watched:** the plugin's private `plugins/configurations/<AssemblyName>/`
  directory. Measured on 10.11.11 with Jellyfin Enhanced 12.1.0.0, that
  directory holds `<userId>/settings.json` (**per-user preferences**),
  `tag-cache.json` and a `cdn-cache/` tree (**runtime caches**). Saving one
  user's personal preference rewrites `<userId>/settings.json` and leaves the
  admin XML untouched — so watching the directory would reload *every client on
  the server* because *one* user toggled a personal setting, and the runtime
  caches would move the generation on their own with no user-visible change at
  all.
* **Debounced:** a new timestamp must stand still for 10s; a burst of writes
  (a settings page that saves three times as you click) collapses into one bump.
* **Cooled down on the LEADING edge:** a change that arrives while no cooldown
  window is open for that plugin publishes **immediately** (after the 10s
  debounce) and opens a window of *Settings-change cooldown* length
  (default **5 minutes**). Only changes arriving **inside** that window are
  held, and they coalesce into a single publish when it expires, carrying the
  latest timestamp — nothing is dropped. A held publish **closes** the window
  rather than opening a new one, so the save after it is snappy again; without
  that, each deferred publish would re-arm the cooldown and a lone later save
  could sit unseen for another five minutes. The practical guarantee is
  therefore "an ordinary single save is live within the debounce plus one client
  poll", with a plugin that rewrites its configuration continuously bounded to
  about two bumps per window instead of one.
* **Excludable:** turn config watching off globally, or list individual plugins
  to ignore. An excluded plugin falls back to version/DLL-change detection only.

**Version/DLL changes bypass the debounce and the cooldown entirely** — a new
binary is unambiguous and rare.

The default exclusion list is **empty**: of the plugins tested live (Jellyfin
Enhanced, Media Bar, File Transformation, InPlayerEpisodePreview) none write
churn into the watched XML. Add a plugin to the list if you observe the
generation moving while nobody is changing settings — `GET /RefreshKit/Diagnostics`
(admin) shows exactly which plugin contributed which timestamp.

---

## Admin settings

Dashboard → Plugins → **Jellyfin Refresh Kit**.

| Setting | Default | Meaning |
| --- | --- | --- |
| Serve index.html through the refresh kit | on | Master switch. Off = the plugin is inert; host bytes pass through untouched. |
| Cache-bust other plugins' script tags | on | Mechanism 2. |
| Reload open tabs after a plugin update | on | Off switches the client to `notify` mode: it logs the update instead of reloading. |
| Treat plugin settings changes as updates | on | Mechanism 3's config input (above). |
| Settings-change cooldown (minutes, per plugin) | 5 | Length of the leading-edge burst window: the change that opens it publishes at once, later changes inside it coalesce to one publish at its end. 0 disables the cooldown; the debounce still applies. |
| Ignore settings changes from these plugins | empty | One per line: plugin name, install folder, GUID or assembly name. |
| Poll interval (seconds) | 60 | Clamped 15–3600 by the client runtime. |
| Required idle time (seconds) | 5 | Clamped 0–300. |
| Max reloads per minute | 3 | Clamped 1–100. |
| Developer mode | off | Serves the client runtime `no-store` instead of `immutable`. |

## Endpoints

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /RefreshKit/Generation` | anonymous | `{ Version, BuildId, CacheKey }`, `CacheKey` = generation. `no-store`. |
| `GET /RefreshKit/Generation.txt` | anonymous | The bare generation, `text/plain`. |
| `GET /RefreshKit/kit.js` | anonymous | The embedded `jellyfin-refresh-kit.js`, `immutable` (the injected `src` carries `?v=<generation>`). |
| `GET /RefreshKit/Diagnostics` | admin | Per-plugin id / version / status / asset ticks / config ticks behind the current generation. |

The first three are anonymous **on purpose**: the login screen is a real page of
the web client, it is where a stale cache most often bites, and a tab can sit on
it for days. An authenticated version endpoint would leave exactly that page
unable to notice an update. They disclose nothing a logged-out visitor cannot
already see — an opaque token and a public MIT-licensed script.

---

## Relationship to single-file adoption

Nothing about the copy-the-file path changes.

* A plugin that ships its own `jellyfin-refresh-kit.js` keeps working. Both
  copies register with the one page manager (the kit's multi-instance
  registration contract), each configured by its own tag. This plugin's instance
  is named **`RefreshKitPlugin`** so it is unmistakable in a support log.
* This plugin's instance declares **no `assetPatterns`** and **no
  `entryScripts`**. That is deliberate: on a page where an adopting plugin ships
  its own kit copy, the first-registered matching instance wins URL versioning,
  and a greedy pattern here would silently take over that plugin's own
  versioning. Layer 2 (runtime-created sub-assets) stays the adopter's business;
  this plugin owns layer 3 (detect + safe reload) server-wide, and the
  server-side stamping covers the static tags.
* This plugin's own tag carries `plugin="Jellyfin Refresh Kit"`, and the
  middleware scrubs exactly that marker — so it never removes or double-injects
  another plugin's tags, and another embedding plugin never scrubs this one's.
* The shipped runtime is **not a committed duplicate**: the csproj embeds
  `../../jellyfin-refresh-kit.js` from the repository root at build time, so the
  plugin always ships the same bytes the single-file path documents.

---

## Repository layout

```
manifest.json                                 plugin-repository manifest (root)
plugin/build.sh                               build → zip + meta.json + md5
plugin/Jellyfin.Plugin.RefreshKit/
    Jellyfin.Plugin.RefreshKit.csproj         net9.0, Jellyfin.Controller 10.11.11
    Plugin.cs                                 plugin identity, embedded runtime
    PluginServiceRegistrator.cs               wires all three mechanisms
    PluginGenerationProvider.cs               the generation aggregator
    ThirdPartyTagStamper.cs                   the ?rkv= stamping rules
    Controllers/RefreshKitController.cs       generation / kit.js / diagnostics
    Configuration/                            settings + dashboard page
    RefreshKit.cs                             VENDORED from the repository root
```

`plugin/Jellyfin.Plugin.RefreshKit/RefreshKit.cs` is the root `RefreshKit.cs`
with exactly three changes, each marked `STANDALONE-PLUGIN ADAPTATION`: the
namespace, a new `RefreshKitOptions.HtmlPostProcess` hook, and the one call site
that invokes it. To re-sync after the root file changes:

```bash
sed 's/^namespace JellyfinRefreshKit$/namespace Jellyfin.Plugin.RefreshKit/' \
    RefreshKit.cs > plugin/Jellyfin.Plugin.RefreshKit/RefreshKit.cs
# then re-apply the HtmlPostProcess option and its call site
```

It is vendored rather than `<Compile Link>`ed because those two hooks do not
exist upstream and the root file belongs to the single-file adoption path.

## Building

```bash
export DOTNET_ROOT=$HOME/.dotnet
bash plugin/build.sh --update-manifest
```

Produces `plugin/build/jellyfin-refresh-kit_<version>.zip` (DLL + `meta.json`),
prints its MD5, and writes that checksum and timestamp into the root
`manifest.json`. Attach the zip to a GitHub release tagged `v<version>` so the
manifest's `sourceUrl` resolves.

## Known limitations

* **Ordering** — see the caveat above; tags injected by a middleware outside
  this one are not stamped, and such a middleware can replace the shell's
  response headers.
* **Cross-origin assets are never stamped.** A plugin loading its client code
  from a CDN (jsDelivr, unpkg) cannot be helped from here; the CDN's own
  `@latest` resolution TTL is invisible to both the server and the browser.
* **The kit's own `<script>` tag is the one URL nobody can version from inside.**
  It carries `?v=<generation>` from the server side, which is exactly the fix —
  but it is served by this plugin, so if this plugin's own tag were ever cached
  by a broken intermediary, the loader could go stale. It is small and stable.
* **The generation is server-wide, not per-plugin.** Any plugin changing reloads
  tabs once. That is the point (a reload costs unchanged plugins nothing but
  cache hits), but it does mean a busy admin session can produce several
  reloads — bounded by the client's reload budget and the config cooldown.
* **A same-second in-place DLL replacement can be missed** if the filesystem
  timestamp resolution and the 5s generation cache align badly. Restarting the
  server always resolves it, and marketplace upgrades change the folder name
  anyway.
