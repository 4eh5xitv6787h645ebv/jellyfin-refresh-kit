# Contributing and releases

This page collects the developer-facing material that used to live in the root README: the repository layout, how to build the plugin package, the `test.sh` validation modes, the disposable Docker labs, the release publication procedure, and the architecture rules that should be preserved when changing the project. Admin-facing documentation is in [README.md](../README.md); compatibility evidence rules are in [COMPATIBILITY.md](../COMPATIBILITY.md).

## Repository layout

```text
.
├── README.md
├── LICENSE
├── manifest.json
├── COMPATIBILITY.md
├── docs/
│   ├── how-it-works.md
│   ├── plugin-authors.md
│   └── contributing.md
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

### Release steps

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
   `manifest_revision=M`, the four-part version, and the policy kind
   (`release_kind`: `final` or `milestone`). Its five
   jobs keep every hosted-runner timeout at or below 360 minutes: immutable-ref
   preflight, fast/reproducibility/security, integration, compatibility, and a
   final rebuild/retention job. The final artifact contains the three original
   worker receipts, semantic lab evidence, one checksum root, and the exact
   candidate bytes. The ref and absent tag are checked again immediately before
   retention. The artifact is retained for 90 days; steps 4-6 must complete
   inside that window, because `post-release-assets.yml` refuses an expired
   validation artifact and a re-run cannot predate publication.
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

The release-policy verifier (`scripts/verify-release-policy.py`) derives any
milestone number and boundary from the fixed campaign origin `1786193837`
(2026-08-08 20:57:17 AWST); a caller cannot choose either. Every release source and validation must be at or after the first fixed
24-hour boundary (2026-08-09 20:57:17 AWST). The workflows validate and retain
bytes; they never tag, publish, or move `main` themselves.

Before every candidate, tag, or `main` push, inspect the effective destination:

```bash
git remote get-url --push origin
```

Stop unless it is the intended writable fork. In particular, never push any
Refresh Kit or Jellyfin Enhanced work to `n00bcodr/Jellyfin-Enhanced`.

## Run tests

Use the repository entry point, `test.sh`. Prerequisites are Node.js 22.12 or newer (Puppeteer 25's floor; the repository pins `22.20.0`), `npm ci`
with the locked Puppeteer/Chromium package, the exact .NET SDK `10.0.302`, and
installed .NET Core plus ASP.NET Core 9.x and 10.x runtimes for the dual-runtime
tests. Every `test.sh` mode requires Python 3.10 or newer, and packaging also needs the documented GNU/Linux shell tools
(`bash`, `curl`, `flock`, `git`, `readlink -f`, `rsync`, `sha256sum`, `tar`, and `timeout`).
Static validation downloads a checksum-pinned `actionlint` archive into a
temporary user cache on first use and requires Docker CLI with Compose for
configuration parsing; container suites require a working Docker engine. The
security audit requires access to the live NuGet and npm advisory feeds.

```bash
./test.sh fast             # packages, both .NET targets, Chromium, static checks
./test.sh dotnet           # standalone compile plus xUnit on actual net9 and net10 runtimes
./test.sh browser          # Chromium runtime regressions
./test.sh reproducibility  # path-isolated byte identity and controlled build locking
./test.sh security-audit   # current NuGet and npm advisory audit (locked graphs, low or higher fails)
./test.sh integration      # ABI-floor smoke, pinned JF10/JF12 lifecycle/browser lab, proxy matrix, Enhanced adoption lab
./test.sh compatibility    # all locked third-party/hostile-fixture matrices
./test.sh all              # every gate above; intentionally long-running
```

Four narrower modes exist for iterating on one piece: `build` (both packages
only), `static` (the shell/JavaScript/JSON/Compose and release-tooling checks
only), `package` (verify an existing `plugin/build` snapshot) and `locking`
(the build-lock proof). `package` needs a prior `build`.

The read-only **Locked ecosystem compatibility** workflow
(`.github/workflows/compatibility.yml`) runs all 14 pinned matrices weekly and
can be manually dispatched for an exact source revision.
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

See [e2e/proxy/README.md](../e2e/proxy/README.md), [e2e/jellyfin/README.md](../e2e/jellyfin/README.md), and [e2e/compat/README.md](../e2e/compat/README.md) for prerequisites, cleanup boundaries, pinned inputs, and retained evidence.

## Architecture and contribution rules

A few constraints are intentional and should be preserved when changing the project:

- **One canonical JavaScript runtime.** The standalone plugin embeds the repository-root `jellyfin-refresh-kit.js`; do not add a second committed copy.
- **Keep the browser runtime dependency-free.** It is designed to be served or copied directly without a JavaScript build pipeline.
- **Keep `RefreshKit.cs` self-contained and fail-open.** It sits in the app-shell request path and must not make Jellyfin unavailable when an unexpected condition occurs.
- **Do not let the standalone instance claim other plugins' runtime asset patterns.** Dynamic assets remain the adopting plugin's responsibility.
- **Keep multi-instance behaviour conservative.** Independent plugins can embed the runtime on the same page, so shared reload behaviour must not let one instance weaken another instance's safety requirements.
- **Add or update tests for generation/stamping changes.** These behaviours contain the subtle cache and filesystem rules.
- **Run the proxy E2E suite for middleware, response-header, injection, or proxy-sensitive changes.**
- **Keep documentation aligned with current behaviour.** User-visible behaviour belongs in the root `README.md`; the mechanism description belongs in `docs/how-it-works.md`; the drop-in integration belongs in `docs/plugin-authors.md`; deeper standalone details belong in `plugin/README.md`; compatibility evidence belongs in `COMPATIBILITY.md`.

## Adding a plugin to the bookkeeping registry

Plugins should declare their own bookkeeping elements (see
[the author guide](plugin-authors.md#tell-refresh-kit-which-settings-are-bookkeeping-1104));
the registry is for plugins that cannot. To add one:

1. Confirm the churn from the plugin's source, not from a symptom: find the
   code path that calls `SaveConfiguration()` on its own (a scheduled task, a
   background service, a startup hook) and the exact property names it
   rewrites, as their configuration class serializes them.
2. Append an `Entry` to `plugin/Jellyfin.Plugin.RefreshKit/KnownPluginConfigurationHints.cs`
   with the plugin GUID (from its `meta.json`; stable across renames), its
   display name, the version and date you checked, the reason (which code path,
   how often), and the element names. Only direct children of the document
   element are matched. Do not list values an admin edits on the plugin's
   settings page.
3. Run `./test.sh dotnet`. `KnownPluginConfigurationHintsTests` rejects a
   malformed GUID, a duplicate, an element name that is not XML-name-like, or a
   reason too short to be useful; `ActivePluginGenerationTests` covers the
   matching.
4. Mention the plugin in the README settings table row and, when the plugin is
   part of the locked compatibility campaign, re-run its matrix.

An entry never overrides a plugin's own declaration; the two are combined.
Removing an entry once a plugin declares its own bookkeeping is safe.

## Enhanced adoption validation

For runtime reload-safety or embedding changes, run
`python3 e2e/enhanced/run.py --snapshot "$(readlink -f plugin/build)"` after
building. This supplements the general integration gate with the current
Enhanced release, both supported host lines, both middleware orders, duplicate
runtimes and actual Enhanced review-form behavior. See
[the lab contract](../e2e/enhanced/README.md).

The active final release version is `1.1.0.3` (runtime `2.5.0`); the source
tree currently carries the next candidate, `1.1.0.4` (runtime `2.5.1`), whose
manifest entry is added only by the release procedure above. The fixed
campaign clock, clean-source/manifest-child binding, exact validation receipts,
and immutable asset publication checks remain required. Updating the expected
version does not authorize replacing the existing `v1.0.1.0` assets.

Package builds also pin the compiler host to the runtime bundled with the exact
SDK and disable shared compiler reuse. A newer runtime installed on the build
machine otherwise changes Roslyn's portable-PDB `runtime-version` record and
therefore the DLL/ZIP hashes. The reproducibility gate checks that record for
both targets while varying the caller's runtime roll-forward setting. This
build-only pin does not constrain the runtime used by Jellyfin or its plugins.
