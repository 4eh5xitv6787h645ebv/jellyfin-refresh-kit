# Enhanced adoption lab

Run `python3 e2e/enhanced/run.py --snapshot /absolute/plugin/.builds/SNAPSHOT`
after building the candidate. `--target jf10` or `--target jf12` selects one
host. The lab uses the official Enhanced 12.8 archives from the ecosystem lock
and digest-pinned Jellyfin 10.11.11 and 12.1 containers. It creates only fresh
`rk-enhanced-*` containers, binds them to host loopback, and removes them on exit.
No existing Jellyfin configuration or media is used.

The independent adoption fixture models adding the runtime to Enhanced's
existing injector; a second fixture embeds the C# helper. The standalone kit is
also installed. Both plugin orders must produce different observed middleware
traces, while Enhanced's entry remains present exactly once and initializes.
The browser compares the fixture's embedded runtime to the exact standalone
candidate, tests duplicate runtime registration, and obtains the actual review
form builder from the verified running Enhanced release. Draft blur, rating
edits, pending and failed saves, and eventual successful-save reload are checked
through real browser events and navigation. Enhanced injection disable/reenable
is also checked. Save completion is controlled by
the fixture; this does not exercise the upstream review database endpoint.

JSON evidence and sanitized startup identities are written to `artifacts/`,
including source revision/tree, harness identity, image/artifact hashes, runtime bytes, middleware
order and browser state. The general Jellyfin/proxy labs remain responsible for
real plugin install/update/rollback lifecycle, playback, websocket, proxy and
subpath checks. Browser evidence is Chromium; other engines are not certified.

The fixture is test-only and must never be installed on a production server.
Its authenticated admin `change` endpoint supplies a deterministic build-change
signal so reload safety can be tested without recompiling a plugin for each
form edit. Neither Enhanced's binaries nor source are modified.
