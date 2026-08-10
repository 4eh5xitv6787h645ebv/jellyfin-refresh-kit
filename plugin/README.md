# Jellyfin Refresh Kit — the standalone plugin

Install **this one plugin** and it watches every loaded plugin on the server for
you.
No other plugin needs to know it exists, and none of them need a code change.
It does three things:

* **serves a fresh app shell** — on the ordinary Kestrel path, `index.html`
  goes out through a revalidating middleware with a body-derived ETag; when an
  outer response buffer owns the final bytes, the middleware serves the complete
  transformed shell as `no-store` instead;
* **cache-busts other plugins' `<script>` and stylesheet tags** that sit in that
  shell and carry no version of their own;
* **notices when active Jellyfin/plugin code or client state changes** —
  lifecycle changes publish only after the required restart actually loads
  them, while active loose assets and watched configuration can publish in the
  running process — and defers open-tab reloads while its documented light-DOM
  probes observe playback, a rendered dialog, or active editing.

**What it does not do:** it cannot version assets a plugin creates at *runtime*
(a dynamic `import()`, a `fetch`, a CSS `url()`), and it cannot stamp tags
injected by a middleware that runs outside this one. Those two limits are real,
and they are spelled out in [the ordering caveat](#ordering-caveat)
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

1. Download the zip for your server from the
   [releases](https://github.com/4eh5xitv6787h645ebv/jellyfin-refresh-kit/releases):
   `jellyfin-refresh-kit_<version>.zip` for Jellyfin 10.11.x, or
   `jellyfin-refresh-kit_<version>_jf12.zip` for Jellyfin 12.x. (See
   [Requirements](#requirements) — the wrong one will not load.)
2. Unzip it into a folder named `Jellyfin Refresh Kit_<version>` inside your
   Jellyfin config's `plugins` directory — e.g.
   `/config/plugins/Jellyfin Refresh Kit_<version>/`, containing
   `Jellyfin.Plugin.RefreshKit.dll`, its portable PDB, and `meta.json`.
   The `Name_version` folder layout is what the server's plugin loader expects;
   a folder without it is ignored.
   On a native install there is no `/config`: the loader reads
   `<datadir>/plugins/`, i.e. whatever `--datadir` points at (default
   `/var/lib/jellyfin/plugins/` for the packaged service). Same folder layout.
3. Restart Jellyfin.

Verify: Dashboard → Plugins shows **Jellyfin Refresh Kit — Active**, and
`GET /RefreshKit/Generation` returns JSON.

### Requirements

Jellyfin **10.11.x** or **12.x**. There is one plugin per
server generation, because a plugin assembly has to match the framework its host
runs on:

| Server | Zip | Framework | Built against | `targetAbi` |
|---|---|---|---|---|
| Jellyfin 10.11.x | `jellyfin-refresh-kit_<version>.zip` | `net9.0` | `Jellyfin.Controller` 10.11.0 | `10.11.0.0` |
| Jellyfin 12.x | `jellyfin-refresh-kit_<version>_jf12.zip` | `net10.0` | `Jellyfin.Controller` 12.0.0-rc4 | `12.0.0.0` |

Installing from the plugin repository, this is not a choice you have to make:
both are listed in the one `manifest.json`, and the server offers only the build
it can run. A 10.11 server never sees the 12 entry at all. Installing by hand,
take the zip whose row matches the server.

The two builds are the SAME SOURCE, compiled twice — the plugin uses only the
part of the plugin surface that survived the 12 rewrite, so there is no
`#if`-ed code and no second project to keep in step.

The net9 build intentionally references the first declared 10.11 ABI. Later
10.11 hosts can satisfy that lower shared-assembly version; the reverse is not
true. The package verifier reads the staged DLL's CLI metadata and requires its
MediaBrowser Common, Controller, and Model references to equal `targetAbi`.

Historical root-Docker, non-root-Docker, and native non-root Linux installation
probes remain available in Git history and release records. They are not
presented as current-candidate certification. Windows remains source-audited
rather than host-run. The exact current status and evidence rules live in the
[compatibility evidence ledger](../COMPATIBILITY.md#current-evidence-ledger).
The existence of a harness or command is not presented as a fresh pass of the
current working tree; see [Validation workflow](#validation-workflow) below.

---

## What it actually does

Three independent mechanisms. Each is useful alone; together they close the
common static-shell and open-tab loop described here.

### 1. index.html is served through a revalidating middleware

The plugin registers an `IStartupFilter` (via `IPluginServiceRegistrator`) that
puts the refresh kit's middleware in front of the web app shell. On the
ordinary Kestrel path, where Refresh Kit owns the final response bytes, each
shell representation it safely transforms carries a **strong, body-derived
ETag** (`"rk-…"`), answers `If-None-Match` with a real `304`, honours `If-Match`
with a `412`, preserves the host's `Cache-Control`/`Vary` and content coding,
and handles `HEAD`.

An outer middleware can instead buffer the response and own the bytes that are
eventually written. Refresh Kit detects that ownership and safely degrades: the
complete injected/stamped shell is served, but with `Cache-Control: no-store`,
without stale entity validators, digests, signatures, or trailers, and with the
outer owner's final content type, coding, and HTTP/1.1 framing. A conditional
request receives the full `200` body rather than an invalid `304`. In both
paths the middleware **fails open**: if it cannot safely process a response, it
serves the host's original bytes rather than breaking the page.

This is the same `RefreshKit.cs` machinery documented in the root README,
vendored into the plugin (see *Repository layout* below).

### 2. Other plugins' script tags get a cache-busting stamp

While it holds the shell, the middleware finds `<script src>` and
`<link rel="stylesheet" href>` tags that **other** plugins put there — whether
by patching `index.html` on disk or by injecting at serve time — and appends
`?rkv=<generation>` to the ones that carry no version of their own. When the
monitored active identity changes, those URLs change, so the browser cannot
serve the prior URL from cache.

It is deliberately conservative. A tag is **skipped** when:

| Skipped | Why |
| --- | --- |
| inline `<script>` (no `src`) | nothing to version |
| `<link>` that is not a stylesheet (manifest, icons, preload) | not client code; stamping can break them |
| absolute or protocol-relative URLs (`https://cdn…`, `//host/…`) | a third-party origin may key its cache/CORS/404 behaviour on the exact URL |
| standalone middleware: any real `<base href>` outside template content | it can redirect Refresh Kit's own PathBase-relative runtime URL, so the complete shell transform is left byte-for-byte unchanged |
| direct `ThirdPartyTagStamper` use: any unsafe or entity-ambiguous base candidate | DOM recovery can reorder candidates, so source order is not trusted; safe same-origin relative bases remain eligible in this direct API |
| any document containing a real `<noscript>` element | server-side code cannot know the user agent's scripting flag; with scripting disabled, an effective `<base>` inside `<noscript>` can change asset origin, so `ThirdPartyTagStamper` skips the whole transform byte-for-byte |
| any document that keeps more than 512 elements open at once | the walker's open-element list is scanned per end tag, so an unbounded list makes pathological nesting quadratic inside the shell transform; past the ceiling the whole pass is abandoned byte-for-byte. A real shell nests a couple of dozen deep |
| already carries `?v=`, `?ver=`, `?version=`, `?hash=`, `?rev=`, `?build=`, `?cb=`, `?_=` … | somebody's deliberate versioning; leave it alone (`rkv` is the exception: old values are scrubbed and restamped) |
| an opaque valueless query (`?3cf5acc8506265662d4f`) | jellyfin-web's own bundle convention — already an identity |
| a content-hashed filename (`main.jellyfin.f725276386e5b19afe0c.css`) | already immutable per URL; restamping would throw away a warm cache for nothing |

Stamping is **idempotent**: any existing `rkv` is scrubbed and the current
generation restamped, so repeated passes and generation changes converge instead
of accumulating. Attribute order, quoting and whitespace are untouched — only the
characters inside the `src`/`href` value change.

The two whole-document aborts above — `<noscript>` and the open-element ceiling
— leave a shell that is indistinguishable from one where nothing was eligible.
`GET /RefreshKit/Diagnostics` therefore reports both as process-lifetime
counters under `Stamping`, so "mechanism 2 does nothing on my server" has an
answer.

#### Ordering caveat

The stamping pass can only see tags that are **already in the response when it
runs**. ASP.NET Core composes startup filters so the *first-registered* filter
ends up *outermost*, and plugin registrators run in the host's plugin load
order, which no plugin can control:

* **On-disk-patched tags** — seen and stamped when eligible.
* **Serve-time tags injected by a middleware INSIDE this one** — seen and
  stamped.
* **Serve-time tags injected by a middleware OUTSIDE this one** — appended to
  the response *after* this pass, so **not stamped**. If that middleware owns a
  later response buffer, mechanism 1 uses the explicit `no-store`
  safe-degradation path instead of claiming an `rk-` validator for somebody
  else's final bytes.

The compatibility matrices preserve one concrete limitation rather than hiding
it: GetAvatar's single eligible outer-owned tag remains unstamped in both
audited install orders. Mechanism 3 can still notify or reload eligible tabs,
but an unchanged outer-owned URL is not guaranteed to return fresh bytes. There
is no supported way for a plugin to force itself outermost, so this is a
documented ownership boundary.

### 3. One loaded-state generation, and a safe reload

An unauthenticated endpoint reports one short, opaque **generation** token
derived from the code and client state this Jellyfin process is actually
running. Consumers must compare the whole token rather than parse its current
shape. The embedded `jellyfin-refresh-kit.js` runtime is injected as the
instance **`RefreshKitPlugin`**, polls that endpoint, and performs a safe reload
when the generation changes.

The token is folded deterministically from:

* selected **loaded Jellyfin host assemblies**, using assembly simple name,
  assembly version, and module MVID;
* the stable plugin id and **actually loaded plugin assemblies**, again using
  assembly simple name, assembly version, and MVID;
* bounded active loose client assets belonging to those loaded plugins: relative
  path, size, and exact content hash for `.js`, `.mjs`, `.css`, and `.html`;
* the filename and exact content of each loaded plugin's Jellyfin configuration
  XML, when configuration watching is enabled.

Absolute install paths, folder names, manifest version/status text, filesystem
timestamps, source maps (`.map`), databases, logs, and private runtime-data
directories are **not** generation identity. Timestamps and manifest fields
remain useful diagnostics, but copied identical active bytes produce the same
token regardless of where or when they were written.

#### Loaded state, not staged disk state

The provider joins Jellyfin's plugin records to assemblies already loaded in
the current process. A disk-only install, upgrade, enable, disable, or uninstall
is staged state and does not announce code that is not active yet. If Jellyfin
removes a plugin record or unlinks its folder while the old assembly still runs,
Refresh Kit retains that plugin's last coherent fingerprint for the rest of the
process. The next Jellyfin start publishes the new loaded set.

That distinction also closes the normal same-version edge case: replacing a DLL
with the same version, size, and modification time does not move the running
process prematurely. Once Jellyfin restarts and loads a replacement with a new
MVID, the generation moves. Arbitrary PE-byte changes that preserve the MVID are
not an input. A loaded Jellyfin host update is handled by the same MVID rule.
Active loose browser assets can still move immediately by content, including a
same-size edit with its timestamp preserved.

#### Process epochs and legitimate rollback

The JSON endpoint also returns an opaque process `Epoch`. It is stable for one
loaded server process, changes on a genuine restart, and is deliberately absent
from the generation, `rkv`/`v` URLs, ETags, and injected boot identity. It exists
only to distinguish a real new process serving a historical generation from a
flapping proxy or mixed-node cycle.

The client requires two observations of a fresh exact generation/epoch pair and
a verified claim in a strict per-tab `sessionStorage` set before granting
one-shot authorization to the otherwise-refused historical target generation.
That authorization survives epoch rotation among replicas serving the same
target while the safety gates keep the reload pending; those epochs are process
evidence, not separate releases or updates. A same-generation restart records
its epoch without a reload. The exact epoch set is non-evicting and saturates at
48 tuples. A separate non-evicting 128-record coverage set remembers a
generation departed without a durably verified epoch; if even the baseline
generation was unresolved, its typed instance tombstone refuses every later
automatic candidate for that instance for the rest of the tab session. Missing
or invalid epochs, an already-seen epoch, incomplete coverage, corrupt or
unavailable storage, and either saturation limit therefore fail closed. A
finite set of stable process epochs cannot create an endless reload cycle;
volatile or broken deployments may still delay convergence.

#### Deterministic scan budgets and failure behaviour

The five-second scan cache is bounded both per plugin and across the complete
scan:

| Budget | Per loaded plugin | Whole scan |
| --- | ---: | ---: |
| File entries admitted/charged | 4,000 | 16,000 |
| Directories admitted/charged | 512 | 2,048 |
| Active asset content hashed | 8 MiB | 32 MiB |
| Configuration content hashed | 2 MiB | 8 MiB |

Plugins are scanned in stable identity order, and paths/content records are
ordinal-sorted before folding. If a budget is exhausted, the affected identity
uses a deterministic truncation sentinel instead of a filesystem-dependent
prefix. A native enumerator may yield one additional unadmitted entry for each
plugin that crosses an entry ceiling, solely to detect the overflow. If an
asset or configuration read races a writer or becomes
unavailable, its reserved global budget remains consumed and the last-good
snapshot is retained when one exists. `GET /RefreshKit/Diagnostics` exposes
file/directory/byte counts, truncation and unavailability flags, skipped
reparse-point configuration files, last-good use, and retained plugin records.
The whole payload is projected from one snapshot of the provider, so a
generation is never reported beside rows from a different scan.

#### One response uses one exact tag identity

The middleware resolves the generation while generating this plugin's exact
`<script>` tag block. That value supplies the tag's `?v=` and
`data-boot-version`. The third-party stamper then reads and HTML-decodes the boot
identity from that already-generated tag block and uses it for every `rkv` in
the same shell response; it does not perform a second mutable provider read.
The representation cache is keyed by the generated tags, so a later generation
invalidates the cached shell. The polling endpoint always reports the provider's
current `CacheKey`; if it has advanced since an older shell was served, that is
the update the client is meant to detect.

---

## Settings changes count as updates (and what that costs)

Plugins render config-driven UI at page load: enable a tab in a plugin's
settings and every open tab is now showing UI built from the old settings. So a
plugin's **configuration file** is watched too, and saving settings propagates to
open clients.

The signal is kept deliberately narrow, because a noisy generation means
pointless server-wide reloads:

* **Watched:** the exact safe filename Jellyfin reports for the loaded plugin in
  its plugin-configuration store (normally
  `plugins/configurations/<AssemblyName>.xml`). The primary assembly-name XML is
  used only as a fallback when the plugin does not report a configured filename.
* **NOT watched: a configuration file that is a symbolic link or other reparse
  point.** Refresh Kit refuses to follow one, because a plugin chooses its own
  configuration filename and following a link would let that choice turn
  generation polling into a content oracle for a file outside the configuration
  store. The consequence is worth stating plainly: on a deployment that
  symlinks its plugin-configuration store — NixOS, some ansible layouts — the
  watched file contributes exactly what a missing file does, and mechanism 3
  quietly does nothing for that plugin. Loaded-module and loose-asset detection
  are unaffected. `GET /RefreshKit/Diagnostics` reports
  `ConfigurationReparsePointsSkipped` per plugin, which is the only way to tell
  the case apart from a plugin that has no configuration at all.
* **NOT watched:** the plugin's private `plugins/configurations/<AssemblyName>/`
  directory. Measured on 10.11.11 with Jellyfin Enhanced 12.1.0.0, that
  directory holds `<userId>/settings.json` (**per-user preferences**),
  `tag-cache.json` and a `cdn-cache/` tree (**runtime caches**). Saving one
  user's personal preference rewrites `<userId>/settings.json` and leaves the
  admin XML untouched — so watching the directory would reload *every client on
  the server* because *one* user toggled a personal setting, and the runtime
  caches would move the generation on their own with no user-visible change at
  all.
* **Debounced:** a new exact content identity must stand still for 10s; a burst
  of writes (a settings page that saves three times as you click) collapses into
  one bump. Preserving the file's timestamp does not hide changed content.
* **Cooled down on the LEADING edge:** a change that arrives while no cooldown
  window is open for that plugin publishes **immediately** (after the 10s
  debounce) and opens a window of *Settings-change cooldown* length
  (default **5 minutes**). Only changes arriving **inside** that window are
  held, and they coalesce into a single publish when it expires, carrying the
  latest content identity — nothing is dropped. A held publish **closes** the window
  rather than opening a new one, so the save after it is snappy again; without
  that, each deferred publish would re-arm the cooldown and a lone later save
  could sit unseen for another five minutes. The practical guarantee is
  therefore that an ordinary single save publishes on the first provider scan
  after the ten-second stable interval. The debounce clock starts only when a
  scan first observes the new content; a polling-only client may need one poll
  to start that clock and a later poll (plus the provider's five-second cache
  boundary) to observe publication. Saving the dashboard form is not a
  synchronous open-tab reload. A plugin that rewrites its configuration
  continuously remains bounded to about two bumps per window instead of one.
* **Excludable:** turn config watching off globally, or list individual plugins
  to ignore. Loaded module and active loose-asset detection still apply.

**Loaded-module and active loose-asset identity changes bypass the configuration
debounce and cooldown entirely.** A staged DLL remains invisible until restart;
the MVID of the module loaded after that restart is authoritative.

The default exclusion list is **empty**. Add a plugin if its normal XML content
moves while nobody is changing server-wide settings. The admin diagnostics
endpoint shows the loaded/content identities, byte budgets, truncation and
last-good state needed to distinguish configuration churn from code or asset
changes.

---

## Safe reload gates

When an update is pending, automatic reload waits while its light-DOM probes
observe playback routes, fullscreen or picture-in-picture media, live media
sessions, active editing, the configured idle window, or a full shared rolling
reload budget.

Runtime 2.4.7 and newer serialize automatic-reload reservations across
same-origin tabs and update their authoritative bounded numeric-v1 ledger in
one IndexedDB `readwrite` transaction. Once the transaction's read is granted,
an admitting callback synchronously reruns every gate before appending a slot.
Navigation waits for transaction completion, another full gate pass, and the
document's current effective-budget check. Equal-millisecond reloads retain
their multiplicity. A gate that closes before append spends nothing; one that
closes after commit leaves the slot conservatively spent without navigating.

Read-back-verified `localStorage` and `sessionStorage` values are compatibility
mirrors: valid histories are max-multiset-merged for migration, but mirrors may
be stale or written out of order and never replace or reduce IDB authority. On
first initialization, both legacy stores must be readable and valid (missing is
a valid empty history); once a valid IDB record exists, unavailable mirrors are
ignored. Unavailable/corrupt IDB, commit failure, or a bounded wall/monotonic
timeout fails closed with the update still pending. Each document applies its
own effective limit; the transaction shares reservations, not settings.

Dialogs and password fields are based on current interactive state, not merely
on matching DOM nodes:

* A **rendered** dialog or action sheet blocks. A retained dialog hidden by a
  `hidden`/`aria-hidden`/`inert` ancestor, layout, visibility, or
  `content-visibility` does not. If the visibility probe itself fails, the gate
  fails safe and blocks.
* A populated **rendered, natively enabled, non-inert** password field blocks
  whether focused or not. A populated login field that is retained but hidden,
  natively disabled (including by a disabled fieldset), or inert after
  authentication does not block the document for life. `aria-disabled` alone
  does not make a native input non-interactive.

The browser regressions cover both sides: Jellyfin 10.11's real hidden retained
login password must permit a later update, while a visible interactive password
field must continue to report `password_entry`.

The probes do not see inside closed shadow roots and cannot prove the state of
DRM or external-player integrations. They are conservative for the DOM state
they can observe, but are not an absolute guarantee about every third-party
playback, dialog, or editor surface.

---

## Admin settings

Dashboard → Plugins → **Jellyfin Refresh Kit**.

| Setting | Default | Meaning |
| --- | --- | --- |
| Serve index.html through the refresh kit | on | Middleware switch. Off = host shell bytes pass through untouched; the configuration page and public generation/runtime endpoints remain available. |
| Cache-bust other plugins' script tags | on | Mechanism 2. |
| Reload open tabs after a plugin update | on | Off switches the client to `notify` mode: it logs the update instead of reloading. |
| Treat plugin settings changes as updates | on | Mechanism 3's config input (above). |
| Settings-change cooldown (minutes, per plugin) | 5 | Length of the leading-edge burst window: after debounce and a provider scan, the change that opens it publishes; later changes inside it coalesce to one publish at its end. 0 disables the cooldown; the debounce still applies. |
| Ignore settings changes from these plugins | empty | One per line: plugin name, install folder, GUID or assembly name. |
| Poll interval (seconds) | 60 | Clamped 15–3600 by the client runtime. |
| Required idle time (seconds) | 5 | Clamped 0–300. |
| Max reloads per minute | 3 | Clamped 1–100 and applied to verified same-origin reservation history. Unavailable coordination defers automatic reload. |
| Developer mode | off | Serves the client runtime `no-store` and uses a distinct `dev=1` script URL. The marker itself remains `no-store` across setting races, so an immutable production response cannot poison the dev URL. |

A numeric field left blank (or filled with something that is not a number)
saves the default in the table above, not zero. A typed `0` is kept wherever
the range allows it, because zero means something specific there: no cooldown,
and no idle wait.

## Endpoints

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /RefreshKit/Generation` | anonymous | `{ Version, BuildId, CacheKey, Epoch }`; `CacheKey` = generation and `Epoch` = this process incarnation. `no-store`. |
| `GET /RefreshKit/Generation.txt` | anonymous | The bare generation, `text/plain`. |
| `GET /RefreshKit/kit.js` | anonymous | The embedded `jellyfin-refresh-kit.js`: immutable for a production generation URL, or `no-store` for developer mode / a `dev=1` URL. |
| `GET /RefreshKit/Diagnostics` | admin | Loaded host modules plus per-plugin loaded/content identities, diagnostic timestamps, scan counts/budgets, truncation/unavailability, skipped reparse-point configuration files, last-good/retained-record state, and stamping abort counters. All from one provider snapshot. |

The first three are anonymous **on purpose**: the login screen is a real page of
the web client, it is where a stale cache most often bites, and a tab can sit on
it for days. An authenticated version endpoint would leave exactly that page
unable to notice an update. The routes expose opaque generation/process tokens
and a public MIT-licensed script, not the authenticated diagnostics or plugin
inventory.

#### What an anonymous observer can and cannot learn

Stated plainly, because "opaque" is easy to over-read:

* **Cannot enumerate or reverse it.** The generation is a truncated SHA-256
  fold. No plugin name, version, path, count or file listing can be recovered
  from the token, and the endpoint answers nothing else.
* **Can confirm a guess.** The fold mixes in no per-install secret. Every input
  can be public: plugin GUIDs, the MVIDs of published release artifacts, the
  bytes of shipped client assets, and a plugin's default configuration XML.
  Someone who can already guess an exact host version plus plugin inventory can
  therefore compute candidate folds offline and check which one the endpoint
  returns — confirmation of an already-formed guess, not discovery. A
  per-install random salt would remove even that, but it would also break the
  invariant that two nodes running identical active bytes publish identical
  generations, which is what makes the kit work behind a load balancer or in a
  replica set. The invariant wins; the property is documented instead.
* **Can watch the timing.** The token changes shortly after an admin saves a
  plugin's settings, and it changes after a server or plugin update takes
  effect on restart. Polling it therefore reveals *when* those events happened,
  to within a poll interval and the debounce/cooldown delay. This is inherent
  to any change signal an unauthenticated login page can read, which is exactly
  what these routes are for.

None of this is affected by the settings switches: turning injection or
stamping off does not remove the endpoints, which the configuration page and
external health checks also use. An installation that cannot accept an
anonymous change signal should not deploy this plugin.

---

## Reverse proxies & CDNs

Normal reverse proxies should not need Refresh Kit-specific configuration. The
disposable rig in [`e2e/proxy/`](../e2e/proxy/README.md) covers direct Jellyfin,
official-nginx and Nginx Proxy Manager-style configurations, Caddy, Traefik,
HAProxy, a Jellyfin `BaseUrl` subpath, and four nginx cache variants. Its legs
check a 17/18-assertion shell/endpoint matrix (depending on whether Brotli is
offered), gzip and optional Brotli, conditional
requests, websocket upgrades, adversarial caching, login, a loose-asset content
change, exactly one safe reload, and updated `rkv` stamps.

Run it with `e2e/proxy/run.sh all`; individual `up`, `matrix`, `ws`, `cache`,
`e2e`, `subpath`, and `down` commands are also available. The runner builds and
verifies one immutable package snapshot, then pins that resolved snapshot for
the lab. These are coverage statements, not a claim that this documentation
edit reran the long container/browser matrix.

### What the kit actually asks of a proxy

Three things:

1. **Pass the ordinary shell path's validators through.** `ETag` in one direction,
   `If-None-Match` / `If-Match` in the other. That is what turns a page load
   into a `304` instead of a download.
2. **Do not override origin cache policy for the shell or `/RefreshKit/`.** The
   ordinary shell says `no-cache`, the nested-buffer path says `no-store`, and
   the generation endpoint says `no-store`. A proxy that honours those
   directives needs no Refresh Kit-specific configuration.
3. **Leave the content coding alone, or re-code it honestly.** At most one
   `Content-Encoding` header, and a body that matches it. When a coding is
   actually offered, the ordinary path revalidates that representation; an
   identity response to a Brotli request is also valid.

### The one misconfiguration that breaks freshness

`proxy_cache` **plus** `proxy_ignore_headers Cache-Control`:

```nginx
proxy_cache jfcache;
proxy_cache_valid 200 10m;
proxy_ignore_headers Cache-Control Expires;   # ← this line
```

The adversarial test leg demonstrates why this is unsafe: an intermediary can
pin both the shell and the `no-store` generation endpoint. The rolling reload
budget rate-limits attempts but does not by itself terminate a proxy that keeps
flapping forever; per-tab left-generation history and one-shot target-generation
authorization bound known finite cycles.

**The remedy is one line — delete `Cache-Control` (and `Expires`) from
`proxy_ignore_headers`:**

```nginx
proxy_ignore_headers Set-Cookie X-Accel-Expires;   # never Cache-Control
```

Also exempt the paths whose freshness/validators must reach the origin. This
preserves client conditional requests even when other Jellyfin responses use an
nginx cache:

```nginx
location = /web/           { proxy_cache off; proxy_pass http://jellyfin:8096; }
location = /web/index.html { proxy_cache off; proxy_pass http://jellyfin:8096; }
location /RefreshKit/      { proxy_cache off; proxy_pass http://jellyfin:8096; }
```

Simplest advice of all: **don't put `proxy_cache` in front of Jellyfin.** The
web client already gives its static assets stable cache identities.

### CDNs

Whatever CDN is used, bypass broad “cache everything” rules for `/web/` and
`/RefreshKit/`, or configure the CDN to honour the origin's `no-cache` and
`no-store`. Product defaults change; verify the effective response and cache
rules rather than relying on a brand-specific default claim here.

### Subpath / base URL

Covered by the disposable subpath lab without Refresh Kit-specific
configuration. Set the base URL in
Dashboard → Networking (`BaseUrl`, e.g. `/jellyfin`) and proxy the prefix
through unchanged. The injected tag uses **relative** URLs —
`src="../RefreshKit/kit.js?v=…"`, `data-version-url="../RefreshKit/Generation"`
— so from `/jellyfin/web/` they resolve to `/jellyfin/RefreshKit/…` with nothing
to configure and nothing to rewrite. The proxy harness's `subpath` leg exercises
the shell matrix, websocket, login, and safe-reload path under that prefix.

### What a proxy cannot fix

* **An outer response owner changes the cache contract.** Refresh Kit returns
  the complete transformed body as `no-store` without an `rk-` validator and
  preserves the outer owner's final framing. A tag that the outer owner adds
  after Refresh Kit's transform can remain unstamped — see
  [the ordering caveat](#ordering-caveat).

Jellyfin 10.11's populated login password retained in a hidden page is **not** a
remaining proxy limitation: hidden/disabled/inert password fields and retained
hidden dialogs no longer block. A visible interactive populated password or a
rendered dialog still blocks intentionally.

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

```text
global.json / NuGet.Config                    pinned SDK and restore sources
Directory.Build.props / Directory.Build.targets
                                               deterministic shared build rules
manifest.json                                  two-ABI plugin repository manifest
test.sh                                        supported validation entry point
scripts/                                       package/reproducibility/evidence checks
plugin/build.sh                                locked dual-target package publisher
plugin/.builds/                                read-only content-identified snapshots
plugin/build                                  atomic symlink to one verified snapshot
plugin/Jellyfin.Plugin.RefreshKit/
    Jellyfin.Plugin.RefreshKit.csproj          net9.0 (JF 10.11) + net10.0 (JF 12)
    Plugin.cs                                  plugin identity, embedded runtime
    PluginServiceRegistrator.cs                wires all three mechanisms
    PluginGenerationProvider.cs                loaded-state generation aggregator
    ThirdPartyTagStamper.cs                    the ?rkv= stamping rules
    Controllers/RefreshKitController.cs        generation / kit.js / diagnostics
    Configuration/                             settings + dashboard page
    RefreshKit.cs                              VENDORED from the repository root
plugin/Jellyfin.Plugin.RefreshKit.Tests/       net9/net10 xUnit tests
tests/browser/                                 headless Chromium regressions
tests/RefreshKit.Standalone.Compile/           root helper compile smoke
e2e/jellyfin/                                  pinned JF10/JF12 lifecycle/browser lab
e2e/proxy/                                     proxy/cache/websocket/subpath lab
e2e/compat/                                    locked ecosystem + hostile fixtures
```

`plugin/Jellyfin.Plugin.RefreshKit/RefreshKit.cs` is the root `RefreshKit.cs`
with exactly three changes, each marked `STANDALONE-PLUGIN ADAPTATION`: the
namespace, compatible legacy/contextual `RefreshKitOptions` post-processing
hooks plus their safe application method, and the one call site that invokes
them. To re-sync after the root file changes:

```bash
sed 's/^namespace JellyfinRefreshKit$/namespace Jellyfin.Plugin.RefreshKit/' \
    RefreshKit.cs > plugin/Jellyfin.Plugin.RefreshKit/RefreshKit.cs
# then re-apply the post-processing options/method and their call site
```

It is vendored rather than `<Compile Link>`ed because those two hooks do not
exist upstream and the root file belongs to the single-file adoption path.
`scripts/verify-vendored-refreshkit.py` makes that three-change contract a
static gate.

## Building

```bash
export DOTNET_ROOT="${DOTNET_ROOT:-$HOME/.dotnet}"
bash plugin/build.sh
```

The build is serialized with a repository lock. It selects the exact SDK from
`global.json`, restores the committed lock file in locked mode, records the
actual Git revision/tree/dirty state and commit epoch, copies every
package-producing input into a read-only compiler tree, builds both target
frameworks, creates deterministic stored ZIPs, and runs mandatory package/ABI/
provenance verification before publishing anything.

It produces:

```text
plugin/build/jellyfin-refresh-kit_<version>.zip       net9 / JF 10.11
plugin/build/jellyfin-refresh-kit_<version>_jf12.zip  net10 / JF 12
plugin/build/stage/                                   DLL + PDB + meta.json
plugin/build/stage-jf12/                              DLL + PDB + meta.json
```

The real output lives in a read-only snapshot under `plugin/.builds/`; only
after both targets verify does the build atomically switch the `plugin/build`
symlink. Existing consumers that already resolved an older snapshot can keep
using it, while the Jellyfin, proxy, and compatibility labs resolve and verify
one snapshot at startup and reuse that exact path for the entire run. The build
prints MD5 and SHA-256 identities for both ZIPs.

Updating local repository metadata is a distinct release action and is refused
for a dirty checkout:

```bash
bash plugin/build.sh --update-manifest
```

It pairs entries by `(version, targetAbi)`, verifies the candidate manifest,
finalizes the local immutable snapshot first, and replaces `manifest.json` only
after the local `plugin/build` pointer is durable. It does not upload a release.

Release publication is deliberately ordered so unvalidated repository metadata
never lands on `main`:

1. Install the dispatch and post-release workflows on the default branch in an
   earlier infrastructure commit. GitHub cannot dispatch a workflow that is not
   already present on the default branch; the release candidate cannot
   bootstrap itself.
2. Create clean source commit `S` and its direct manifest-only child `M` locally.
   After verifying the push URL is the writable
   `4eh5xitv6787h645ebv/jellyfin-refresh-kit` repository and both the version tag
   and candidate ref are absent, push only `M` to
   `refs/heads/release-candidate/v<version>`. Never move or reuse this ref.
3. Dispatch `release-validation.yml` from that exact candidate ref with `S`,
   `M`, the version, and policy kind. Its five jobs separately retain the
   fast/reproducibility/security, real-integration, and locked-compatibility
   receipts; a final independent rebuild semantically verifies and merges them
   under one checksum root. Every job is within GitHub's 360-minute hosted-job
   limit, and the remote candidate plus absent tag are checked both before and
   immediately after evidence assembly.
4. When validation succeeds, recheck the ref and absent tag, tag `S`, and attach
   **exactly two assets**: the two ZIPs retained under the final artifact's
   `release-candidate/` directory. Then fast-forward remote `main` to the already
   validated `M`; do not amend, merge, rebuild, or regenerate either commit.
5. Dispatch `post-release-assets.yml` from `main` at `M` with the successful
   validation run ID. It requires remote `main` to equal `M`, `M`'s sole parent
   to equal `S`, the remote tag to resolve to `S`, the still-immutable candidate
   ref to equal `M`, the release to contain exactly the two expected assets, and
   all retained/rebuilt/published hashes to agree. Keep the candidate ref until
   this read-only post-release check succeeds.

The verifier derives milestone timing from the fixed campaign origin
`1786193837` (2026-08-08 20:57:17 AWST), rejects a caller-selected boundary or
pre-existing tag, and never publishes, tags, or moves a branch itself.

## Validation workflow

Use the repository entry point from the repository root:

The full local prerequisites are Node.js 20 or newer (`.node-version` pins
`22.20.0`), `npm ci` for locked Puppeteer/Chromium, the exact .NET SDK
`10.0.302`, installed .NET Core and ASP.NET Core 9.x plus 10.x runtimes, Python
3, and GNU/Linux shell tools including `curl`, `flock`, `sha256sum`, and `tar`.
Static validation downloads a checksum-pinned `actionlint` archive into a
temporary user cache on first use and needs Docker CLI with Compose; container
suites need a Docker engine. `security-audit` also needs the live NuGet advisory
feed.

```bash
./test.sh static           # syntax/JSON/XML/workflow/Compose/vendored checks
./test.sh fast             # static + packages + both .NET targets + Chromium
./test.sh dotnet           # root helper compile + xUnit on net9 and net10
./test.sh browser          # focused headless-Chromium runtime regressions
./test.sh package          # verify the current immutable package snapshot
./test.sh reproducibility  # path-isolated byte identity + build-lock checks
./test.sh security-audit   # locked NuGet graph against the live advisory feed
./test.sh integration      # dual-Jellyfin lab, then the proxy/browser matrix
./test.sh compatibility    # locked ecosystem and hostile-fixture matrices
./test.sh all              # every gate above; intentionally long-running
```

The container suites have separate, project-scoped runners:

* `e2e/jellyfin/run.sh all` builds/pins one snapshot, provisions digest-pinned
  Jellyfin 10.11.11 and 12.0.0-rc4 servers, checks both transformed shells, runs
  real Chromium login/navigation/multi-tab/restart/reconnect coverage, exercises
  install, update, disable, enable, uninstall, reinstall, playback, logout, and
  open-tab convergence through Jellyfin's own APIs, then runs the same
  restart-bound lifecycle with genuine independently compiled third-party v1/v2
  packages on both hosts. It also records the bounded net9-package-on-Jellyfin-12
  experiment before restoring the proper net10 stage. The pinned RC4 result is
  a coherent load, not an assumed ABI guarantee for future hosts. The browser
  leg deliberately leaves Jellyfin 10.11's hidden populated login field intact.
* `e2e/proxy/run.sh all` provisions a disposable Jellyfin 10.11 origin and the
  proxy/cache/subpath matrix described above. It changes a loose `.js` file by
  content; touching or staging a DLL is intentionally not used as a live
  generation bump.
* `e2e/compat/run.sh` provides container-free `static`, `list`, `coverage`, and
  locked-archive `fetch` commands. Runtime `run`/`all` commands require explicit
  `RK_COMPAT_ALLOW_CONTAINERS=1`; `./test.sh compatibility` supplies that gate,
  builds one immutable snapshot, and runs the pinned third-party/hostile-fixture
  matrices serially. The compatibility README records which expensive matrices
  have or have not actually been launched. The read-only
  **Locked ecosystem compatibility** workflow runs this gate weekly and also
  accepts an optional exact 40-character source revision for manual dispatch;
  its sanitized structured evidence is retained as a workflow artifact.

`./test.sh integration` strictly checks and collects both self-lifecycle and
third-party-lifecycle results for each host, plus structured dual-Jellyfin and
proxy logs, into `test-results`; compatibility matrices retain their own
per-matrix evidence.
Commands and coverage describe what the harness asserts. Consult the retained
artifacts/CI result before claiming a particular revision passed a heavy suite.

## Known limitations

* **Runtime-created assets** — dynamic imports, `fetch()`, JavaScript-created
  resources and CSS `url()` references do not exist in `index.html` for this
  plugin to rewrite. The plugin that owns them should version them or adopt the
  single-file kit directly.
* **A directly retained pre-2.4.6 `document.createElement` wrapper stays inert
  after handoff.** The released 2.4.2 closure cannot acquire a bridge
  retroactively. Elements it returned before handoff and calls through the
  current wrapper remain versioned; retained wrappers created by 2.4.6 and
  newer forward to the newest manager.
* **Ordering** — see the caveat above; tags injected by a middleware outside
  this one are not stamped. The two audited GetAvatar orders deliberately report
  its one outer-owned tag as `PASS WITH LIMITATION`; an unchanged URL is not a
  fresh-byte guarantee.
* **A real `<noscript>` is a whole-transform boundary.** The server cannot know
  whether the user agent has scripting enabled, and HTML parsing with scripting
  disabled can make a `<base>` inside `<noscript>` effective. Rather than risk
  changing a relative asset's origin, `ThirdPartyTagStamper` leaves that entire
  stamping input byte-for-byte unchanged.
* **A quoted legacy `PUBLIC`/`SYSTEM` doctype is a conservative transform
  boundary.** The bounded tokenizer does not implement the complete HTML doctype
  state machine, so that shell is served unchanged. Jellyfin's normal
  `<!doctype html>` remains transformable.
* **A real document `<base href>` disables runtime injection.** Refresh Kit's
  own URL is relative so it follows Jellyfin's configured PathBase; any effective
  base could redirect that URL to another path or origin. Bases inside inert
  template content do not trigger this boundary.
* **Nested outer response buffers** — the complete transformed shell is served
  `no-store` without entity validators, using the outer owner's final framing.
  This is safe freshness degradation, not strong-ETag/`304` compatibility.
* **Cross-origin assets are never stamped.** A plugin loading its client code
  from a CDN (jsDelivr, unpkg) cannot be helped from here; the CDN's own
  `@latest` resolution TTL is invisible to both the server and the browser.
* **The generation is server-wide, not per-plugin.** Any monitored active
  identity changing can reload eligible tabs once. Unchanged assets remain cache
  hits; the client reload budget and configuration cooldown bound repeated work.
* **Budget truncation is intentionally coarse.** An over-budget plugin receives
  the stable truncation sentinel until it fits again; diagnostics expose that
  state instead of pretending a partial scan is complete.
* **Broken intermediary caching still wins.** A proxy or CDN configured to
  ignore origin cache directives can pin the shell or generation endpoint.
* **Safety probes are bounded.** Closed shadow roots and DRM/external-player
  state are outside the light-DOM probes; a blocker there cannot be promised.
* **Background browser scheduling can delay detection.** A browser may throttle
  or freeze hidden tabs until it allows their JavaScript to run again.
* **Cross-tab budget serialization requires runtime 2.4.7 or newer in every
  participating tab.** Older copies write the legacy numeric-v1 storage history
  but do not update the authoritative ledger inside the IndexedDB transaction.
  Unavailable/corrupt IDB safely defers automatic reload; unreadable/corrupt
  legacy stores do the same only while the first IDB record needs migration.
