# Embed Refresh Kit in Enhanced

This adoption keeps Enhanced's existing request-time script injector. It does
not register a second `RefreshKit.cs` shell middleware. The injector emits the
Refresh Kit runtime immediately before Enhanced's entry tag. This avoids the
stacked-helper stand-down boundary: an independent injector still contributes
its tags when another helper owns shell revalidation. The standalone kit and
other embedded runtime copies may coexist; the newest runtime manages the page.

Serve the canonical `jellyfin-refresh-kit.js` as an embedded resource at an
Enhanced-owned route, and expose a no-store version endpoint. Its `CacheKey`
must identify the *loaded* Enhanced build (e.g. loaded assembly MVID plus build
version), rather than a DLL awaiting restart on disk. An optional `Epoch` is
one random identity created once per process. The emitted `data-boot-version`
and endpoint `CacheKey` must match. Keep Enhanced's existing versioned module
loader; Refresh Kit respects existing `v=` parameters.

In `ScriptInjectionStartupFilter`, emit the kit tag immediately before the
output of the existing `BuildScriptTag()`, from a separate
`BuildRefreshKitTag()`:

```html
<script defer src="../JellyfinEnhanced/refresh-kit.js?v=BUILD_KEY"
        data-name="JellyfinEnhanced"
        data-boot-version="BUILD_KEY"
        data-version-url="../JellyfinEnhanced/refresh-version"
        data-version-json-field="CacheKey"
        data-version-epoch-json-field="Epoch"></script>
<!-- Enhanced's existing script tag follows, unchanged. -->
```

Do **not** return the kit tag from `BuildScriptTag()` itself. That method also
feeds the legacy on-disk `index.html` rewrite (`UpdateIndexHtml`), whose
cleanup regex removes only tags carrying `plugin="Jellyfin Enhanced"`; the kit
tag cannot carry that attribute (`plugin.js` finds Enhanced's own tag with
`querySelector('script[plugin="Jellyfin Enhanced"]')`, which returns the first
match), so one stale kit tag would be left on disk per restart while
`DisableScriptInjectionMiddleware` is on, and would outlive an uninstall. If
the on-disk path must also carry the kit, extend both scrub regexes to remove
`<script[^>]*data-name=["']JellyfinEnhanced["'][^>]*>\s*</script>\n?` as
well.

HTML-encode attributes and URL-encode query values. Construct prefix-relative
URLs using the same BaseUrl-aware logic as the existing injector. Versioned
runtime responses can be immutable; bare/unversioned runtime URLs and version
responses must revalidate or be no-store. Preserve Enhanced's injection kill
switch: with it on, Enhanced falls back to the on-disk rewrite at the next
restart; it does not remove Enhanced. Disable only automatic refreshing with
`data-mode="notify"` or `data-mode="off"`; these are separate from disabling
Enhanced itself.

`onUpdateAvailable` cannot be set through a `data-*` attribute, and the keyed
configuration is read synchronously when the kit tag runs, before `plugin.js`
loads. To surface an Enhanced notification in notify mode, emit one inline
script **before** the kit tag that creates the `JellyfinEnhanced` entry of the
keyed configuration without clobbering other adopters' entries. The callback
fires once per detected version, possibly before Enhanced has loaded, so it
records the notification and hands it to a hook Enhanced adds
(`JellyfinEnhanced.notifyRefreshKitUpdate`, which `plugin.js` should also
call for a recorded `__jeRefreshKitUpdate` at initialisation):

```html
<script>
(function () {
  var cfgs = window.JellyfinRefreshKitConfigs = window.JellyfinRefreshKitConfigs || {};
  cfgs.JellyfinEnhanced = Object.assign(cfgs.JellyfinEnhanced || {}, {
    onUpdateAvailable: function (n, o) {
      window.__jeRefreshKitUpdate = [n, o];
      var je = window.JellyfinEnhanced;
      if (je && typeof je.notifyRefreshKitUpdate === 'function') je.notifyRefreshKitUpdate(n, o);
    }
  });
})();
</script>
```

Enhanced's global is `window.JellyfinEnhanced`; `window.JE` is only a local
alias in most modules. Do not derive a notification from
`JellyfinRefreshKit.get('JellyfinEnhanced').latestVersion` alone: that value
moves on the first sighting, before the kit has confirmed the candidate.

Refresh Kit 1.1.0.4 ships Enhanced's bookkeeping elements
(`ClearTranslationCacheTimestamp`, the `Analytics*` receipts) in its built-in
registry so they do not reload tabs. The durable arrangement is for Enhanced
to declare them itself, in its own configuration class, so future fields need
no Refresh Kit release:

```csharp
public string[] RefreshKitIgnoredElements { get; set; } = new[]
{
    "ClearTranslationCacheTimestamp",
    "AnalyticsInstallId", "AnalyticsInstallSecret", "AnalyticsLastReportedAt",
    "AnalyticsLastPayloadJson", "AnalyticsLastReportedPluginVersion",
    "AnalyticsLastReportedJellyfinTarget", "AnalyticsLastReportedJellyfinVersion",
    "AnalyticsForbiddenSinceLastSuccess",
};
```

Two Enhanced-side behaviours interact with automatic reloads and are worth
fixing in Enhanced:

- In legacy on-disk mode (`DisableScriptInjectionMiddleware=true`) Enhanced's
  constructor removes its tag from `index.html` and the startup scheduled task
  re-adds it a moment later. A tab that polls the new generation inside that
  gap reloads into a shell without Enhanced and stays that way until the user
  reloads by hand; the on-disk write is not a generation input. Injecting
  (rather than only cleaning) in the constructor when legacy mode is on closes
  the gap.
- Enhanced's admin page registers a `beforeunload` listener that consults the
  page instance's `.je-save-dock` and is never removed. Once jellyfin-web has
  dropped a dirty instance from the DOM, the kit's gate no longer sees it,
  the reload proceeds, and the stale listener shows a native "Leave site?"
  prompt for edits the user can no longer reach. Checking `isConnected` in
  that listener (or removing it on view destroy) fixes it.

The built-in Enhanced safeguards protect a connected `.je-review-form` until
successful save/cancel removes it, including rating-only drafts and disabled
Submit buttons during saves. `.je-save-dock.je-dirty` blocks for unsaved admin
settings. These selectors are verified against 12.8; retain their semantics or
replace them with a registered guard when changing Enhanced's UI. An empty open
review form also blocks conservatively. Hiding a draft does not discard it.

Enhanced's role-less overlays — the settings panel, the Seerr more-info modal,
the bookmark, hidden-content and multi-select overlays, the active-streams
panel and the Elsewhere streaming-settings modal — are treated as open dialogs
by runtime 2.5.1 while they are in the DOM, carry their open class, or are
displayed. For other application-owned state, use the generic
API, always feature-detected (the kit may be absent or an older copy may own
the global; Enhanced's save path must never depend on it), or mark the
element with `data-refresh-kit-unsaved`, which works under any 2.5.1+ manager
without touching the API:

```js
let dirty = false;
let savesInFlight = 0;
const rk = window.JellyfinRefreshKit;
const protection = (rk && typeof rk.registerReloadGuard === 'function')
    ? rk.registerReloadGuard('Enhanced editor', () => !dirty && savesInFlight === 0)
    : { changed() {}, release() {} };

async function save() {
    const submittedRevision = editRevision;
    savesInFlight++;
    protection.changed();
    try {
        await saveToServer(); // must reject on unsuccessful HTTP responses
        if (editRevision === submittedRevision) dirty = false;
    } finally {
        savesInFlight--;
        protection.changed();
    }
}
// Release only when the editor is successfully saved and closed, or the user
// explicitly discards it. Failed saves must retain dirty state. A new edit
// during an older save must not be marked clean by that older completion.
```

`editRevision` and dirty-state updates belong to the editor. The example is
application wiring, not an API wrapper around Enhanced's existing save helper:
that helper can swallow failures, so the guard must follow confirmed persisted
state. Release handles on intentional editor teardown. Guard names do not confer
ownership: two registrations with the same name remain independent.

`e2e/enhanced` validates this injection arrangement against the locked official
Enhanced binaries, in both runtime plugin orders, with another independent
C# helper and the standalone kit present. No upstream Enhanced files are edited.

Enhanced admin saves also block while `.je-save-dock-btn[disabled]` is present,
including saves started without a dirty marker. Re-enabling the button releases
that save-state protection; a remaining dirty marker continues to protect edits.
