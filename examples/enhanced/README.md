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

In Enhanced's existing `BuildScriptTag`, return these two tags in order:

```html
<script defer src="../JellyfinEnhanced/refresh-kit.js?v=BUILD_KEY"
        data-name="JellyfinEnhanced"
        data-boot-version="BUILD_KEY"
        data-version-url="../JellyfinEnhanced/refresh-version"
        data-version-json-field="CacheKey"
        data-version-epoch-json-field="Epoch"></script>
<!-- Enhanced's existing script tag follows, unchanged. -->
```

HTML-encode attributes and URL-encode query values. Construct prefix-relative
URLs using the same BaseUrl-aware logic as the existing injector. Versioned
runtime responses can be immutable; bare/unversioned runtime URLs and version
responses must revalidate or be no-store. Preserve Enhanced's injection kill
switch. Disable only automatic refreshing with `data-mode="notify"` (wire
`onUpdateAvailable` to an Enhanced notification) or `data-mode="off"`; these are
separate from disabling Enhanced itself.

The built-in Enhanced safeguards protect a connected `.je-review-form` until
successful save/cancel removes it, including rating-only drafts and disabled
Submit buttons during saves. `.je-save-dock.je-dirty` blocks for unsaved admin
settings. These selectors are verified against 12.8; retain their semantics or
replace them with a registered guard when changing Enhanced's UI. An empty open
review form also blocks conservatively. Hiding a draft does not discard it.

For other application-owned state, use the generic API:

```js
let dirty = false;
let savesInFlight = 0;
const protection = JellyfinRefreshKit.registerReloadGuard(
    'Enhanced editor', () => !dirty && savesInFlight === 0
);

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
