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

Scanning is deterministic and bounded. Per plugin it admits/charges at most 4,000 file entries, 512 directories, 8 MiB of active asset content, and 2 MiB of configuration content. One complete scan is additionally capped at 16,000 charged files, 2,048 charged directories, 32 MiB of assets, and 8 MiB of configuration. A native enumerator may yield one extra unadmitted entry for a plugin to detect that a file/directory ceiling was crossed. Budget exhaustion contributes a stable truncation sentinel and appears in admin diagnostics. A transient read failure reserves the last coherent snapshot's charge (or the ceiling when none exists yet) and retains that snapshot, instead of publishing a false lifecycle change; because it never reserves more than that snapshot cost, the plugins scanned after it keep the capacity they had and do not flip to the truncation sentinel. An entry the server process cannot read at all — a permission-denied subdirectory or asset file — is skipped on its own, counted in diagnostics, and folded as a deterministic per-path sentinel so the rest of that plugin's assets still participate; only a plugin folder that cannot be listed at all is treated as unavailable.

### Settings changes

Plugin settings can affect UI that is built when the page loads, so configuration changes are watched by default.

Refresh Kit watches Jellyfin's plugin configuration XML rather than a plugin's private data directory. This avoids treating per-user preferences and runtime cache churn as server-wide UI changes.

A configuration file that is a symbolic link (or another reparse point) is never followed, because a plugin picks its own configuration filename and following the link would let that choice read a file outside the configuration store. On a deployment that symlinks the store — NixOS, some ansible layouts — that plugin's settings changes are therefore not detected; the admin diagnostics endpoint reports the skipped files per plugin. The same rule applies to loose client assets: a symlinked asset file or asset directory under a plugin folder is not followed, contributes what an absent entry does, and is reported per plugin in diagnostics.

Configuration signals are controlled in three ways:

- **Debounce:** a changed configuration-content identity must remain stable for 10 seconds before publication.
- **Per-plugin cooldown:** the first change publishes promptly; further changes during the configured window are coalesced into one later update. The window's length follows the current setting, so lowering the cooldown releases an already-held change on the next scan (as the held publish it is, closing the window rather than arming a new one) and raising it extends a window that is still open; a window that has already expired is never revived by a later raise.
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
| Active editor | a text-editing field has focus (on `#/login` and `#/selectserver` only, an empty field — or, since 2.4.9, one the browser autofilled and the user never edited, while the page has seen no trusted click or keypress since the kit booted — does not count) |
| Password entry | a rendered, enabled, non-inert password field still contains a value (on the empty routes only, since 2.4.9, a browser-autofilled password is ignored while no text field on the page holds typed text and no trusted click or keypress has happened since the kit booted; a credential the user had to pick from the browser's chooser is not refilled unprompted after a reload) |
| Not idle | the configured user-idle period has not elapsed — never less than the runtime's 1-second settle floor, and relaxed to that floor on the empty routes, under Jellyfin's screensaver and for 2.5 s after leaving playback |

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
never write them (a saturated set, storage that refuses writes) refuses before
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

## Application-owned unfinished work

Runtime 2.5.0 evaluates page-wide synchronous reload guards at every safety
decision, including the final checks around shared-budget acquisition. Unknown
guard state refuses a reload. Guard registrations and release handles transfer
with a newer runtime. Enhanced 12.8 review forms and its dirty admin-settings
indicator are also checked directly, protecting drafts after blur and while
saving. These checks are not bypassed by hidden tabs or screensavers. See
[the author API](plugin-authors.md#protect-application-work-runtime-250).
