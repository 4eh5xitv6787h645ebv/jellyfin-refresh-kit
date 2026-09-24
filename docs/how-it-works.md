# How the standalone plugin works

This page describes the standalone plugin's three mechanisms, what is folded into the server-wide generation, and the gates that keep automatic reloads safe. It was moved here from the root README so that [README.md](../README.md) can stay focused on installing and operating the plugin; the deeper implementation-level reference is [plugin/README.md](../plugin/README.md).

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
- `<link rel="preload" as="script|style" href="…">` and `<link rel="modulepreload" href="…">`, but only when this same document also stamps a `<script src>` or stylesheet tag with the identical URL, so the hint keeps matching the tag it preloads instead of causing a second download; a hint whose consumer is a runtime `import()` or a loader-created script has no such tag and is left unstamped, like that import

When an eligible URL does not already carry its own version identity, Refresh Kit adds:

```text
?rkv=<generation>
```

When the monitored plugin state changes, the generation changes and the browser sees a new URL instead of reusing a stale cached copy.

The stamper is deliberately conservative. It leaves these alone:

- inline scripts
- `<link>` elements other than stylesheets and script/style preload hints
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
- the plugin's Jellyfin configuration XML when configuration watching is enabled: its exact bytes, or, when the admin's ignore list names top-level elements for that plugin, the document with those elements removed (insignificant whitespace and comments do not count either)

Manifest status, absolute paths, timestamps, source maps, databases, logs, and private runtime-data directories are not generation identity. Some remain available as diagnostics, but they cannot make two nodes with identical active bytes disagree merely because files were copied at different times.

The provider looks at loaded state rather than staged disk state. Installing,
disabling, or deleting a plugin before its required restart therefore does not
announce code that is not active yet; after restart, a changed loaded MVID set
moves the generation and existing tabs converge. A same-version replacement is
therefore detected when the replacement module has a new MVID and is loaded;
arbitrary PE-byte changes that preserve the MVID are not a generation input.

Scanning is deterministic and bounded. Per plugin it admits/charges at most 4,000 file entries, 512 directories, 8 MiB of active asset content, and 2 MiB of configuration content. One complete scan is additionally capped at 16,000 charged files, 2,048 charged directories, 32 MiB of assets, and 8 MiB of configuration. A native enumerator may yield one extra unadmitted entry for a plugin to detect that a file/directory ceiling was crossed. Budget exhaustion contributes a stable truncation sentinel and appears in admin diagnostics. A transient read failure reserves the last coherent snapshot's charge (or the ceiling when none exists yet) and retains that snapshot, instead of publishing a false lifecycle change; because it never reserves more than that snapshot cost, the plugins scanned after it keep the capacity they had and do not flip to the truncation sentinel. An entry the server process cannot read at all — a permission-denied subdirectory or asset file — is skipped on its own, counted in diagnostics, and folded as a deterministic per-path sentinel so the rest of that plugin's assets still participate; only a plugin folder that cannot be listed at all is treated as unavailable.

### Settings changes

Plugin settings can affect UI that is built when the page loads, so configuration changes are watched by default.

Refresh Kit watches Jellyfin's plugin configuration XML rather than a plugin's private data directory. This avoids treating per-user preferences and runtime cache churn as server-wide UI changes.

A configuration file that is a symbolic link (or another reparse point) is never followed, because a plugin picks its own configuration filename and following the link would let that choice read a file outside the configuration store. On a deployment that symlinks the store — NixOS, some ansible layouts — that plugin's settings changes are therefore not detected; the admin diagnostics endpoint reports the skipped files per plugin. The same rule applies to loose client assets: a symlinked asset file or asset directory under a plugin folder is not followed, contributes what an absent entry does, and is reported per plugin in diagnostics.

Configuration signals are controlled in three ways:

- **Debounce:** a changed configuration-content identity must remain stable for 10 seconds before publication.
- **Per-plugin cooldown:** the first change publishes promptly; further changes during the configured window are coalesced into one later update. The window's length follows the current setting, so lowering the cooldown releases an already-held change on the next scan (as the held publish it is, closing the window rather than arming a new one) and raising it extends a window that is still open; a window that has already expired is never revived by a later raise.
- **Exclusions:** individual plugins can be ignored for configuration-change tracking.
- **Ignored elements:** individual top-level elements of a plugin's configuration XML can be left out of its identity, so bookkeeping a plugin writes on its own does not count as a settings change. Three layers combine: a plugin's own `RefreshKitIgnoredElements` declaration inside its configuration (the durable option; it ships with the plugin), the built-in registry in `KnownPluginConfigurationHints.cs` for plugins that cannot declare (today Jellyfin Enhanced 12.8, which rewrites a translation-cache timestamp at every server start and its analytics receipts on a timer; without it every restart would reload every open tab), and the admin setting as the per-server override. The diagnostics endpoint lists the names in effect per plugin.

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
| Dialog | a rendered native, Jellyfin, or ARIA dialog/action sheet is open, or (since 2.5.1) one of Jellyfin Enhanced's role-less overlays: its settings panel, Seerr more-info modal, bookmark, hidden-content and multi-select overlays, active-streams panel, or Elsewhere streaming-settings modal |
| Media session | real media playback is active on the page |
| Active editor | a text-editing field has focus (on `#/login` and `#/selectserver` only, an empty field — or, since 2.4.9, one the browser autofilled and the user never edited, while the page has seen no trusted click or keypress since the kit booted — does not count) |
| Password entry | a rendered, enabled, non-inert password field still contains a value (on the empty routes only, since 2.4.9, a browser-autofilled password is ignored while no text field on the page holds typed text and no trusted click or keypress has happened since the kit booted; a credential the user had to pick from the browser's chooser is not refilled unprompted after a reload) |
| Not idle | the configured user-idle period has not elapsed — never less than the runtime's 1-second settle floor, and relaxed to that floor on the empty routes, under Jellyfin's screensaver and for 2.5 s after leaving playback. Since 2.5.1 the clock restarts when a hidden tab becomes visible again, so time spent away never counts as idle |
| Application work (`unsaved_work`) | a connected Enhanced review form, dirty Enhanced admin settings or an Enhanced save in progress exist, or any light-DOM element carries `data-refresh-kit-unsaved` (any value but `false`; inside a shadow root, mark the host). Never overridden by the screensaver or the hidden-tab path |
| Registered guard (`reload_guard`) | a guard registered through `registerReloadGuard` did not return exactly `true` |

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
slot conservatively spent without navigating. Since runtime 2.4.9 the per-tab
safety records a navigation must write (the LEFT-version set and the
epoch-coverage gaps in `sessionStorage`) are rehearsed in that same pre-append
pass — the exact bytes are written, verified and restored — so a tab that could
never write them (storage that refuses writes) refuses before
the append and spends nothing, instead of burning one origin-wide slot per
window and starving its sibling tabs.

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
generation/epoch pair must be observed twice and claimed in a strict, bounded
per-tab set before it can provide one-shot proof for a historical
target generation. That authorization remains attached to the target generation
while its reload is pending, even if polls rotate through other process epochs
serving the same generation; replica rotation is not a new update identity. A
same-generation restart is recorded without reloading. Missing, invalid,
previously seen, or unverifiable epoch state preserves the older fail-closed
flap refusal. If a page leaves before one instance's baseline epoch or even its
baseline generation is durably known, a separate bounded per-tab coverage
record prevents a later epoch from claiming that ambiguous history
fresh; an unresolved-generation record conservatively disables automatic
updates for that instance until it is evicted or the tab session ends. Epochs
never enter asset URLs, ETags, or the generation itself.

The per-tab LEFT, epoch and coverage sets are bounded (128, 48 and 128
records). Before runtime 2.5.1 a full set refused every further automatic
reload for the life of the tab, which a long-lived wall-display tab reached
through nothing but ordinary updates and settings saves and then went stale
for good. Since 2.5.1 a full set drops its oldest records instead: a flap
between generations only ever needs a handful of records, so the protection
these sets provide is unchanged for any realistic cycle, while corrupt,
unreadable or unwritable storage still fails closed. The one guarantee
saturation used to carry — that a finite set of generations served under an
endless supply of new process epochs cannot reload a tab forever — is kept by
a separate strict, saturating counter: at most 16 epoch-authorized revisits of
a historical generation are ever spent in one tab session. Ordinary forward
updates never touch it.

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

## Application-owned unfinished work

Runtime 2.5.0 evaluates page-wide synchronous reload guards at every safety
decision, including the final checks around shared-budget acquisition. Unknown
guard state refuses a reload. Guard registrations and release handles transfer
with a newer runtime. Enhanced 12.8 review forms and its dirty admin-settings
indicator are also checked directly, protecting drafts after blur and while
saving. These checks are not bypassed by hidden tabs or screensavers. Runtime
2.5.1 adds a declarative form that needs no JavaScript API: any light-DOM
element carrying `data-refresh-kit-unsaved` blocks the same way (inside a
shadow root, mark the host element), which is the path to use when an older
runtime copy owns the page's frozen global and `registerReloadGuard` is
therefore missing from it. See
[the author API](plugin-authors.md#protect-application-work-runtime-250).
