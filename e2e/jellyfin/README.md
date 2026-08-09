# Disposable Jellyfin lifecycle lab

This lab provisions independent, project-scoped instances of:

- Jellyfin **10.11.0** in a profile-only ABI-floor smoke with the Refresh Kit
  `net9.0` package
- Jellyfin **10.11.11** with the Refresh Kit `net9.0` package
- Jellyfin **12.0.0-rc4** (published image tag `12.0-rc4`) with the Refresh Kit `net10.0` package

All Jellyfin container images are pinned by digest in `docker-compose.yml`.
The ordinary lab ports bind only to loopback (`127.0.0.1:18116` and
`127.0.0.1:18117` by default). The ABI-floor container has no published port;
its runner temporarily binds a host relay to `127.0.0.1:18119`. Each run
resolves one verified immutable plugin-build snapshot before copying any files.
The lifecycle-only repository helper image is digest-pinned as well.

## Prerequisites

- Docker with Docker Compose v2
- the ABI-floor live smoke requires a rootful Linux Docker Engine whose host can
  route to its local internal bridge (the no-Docker negative gate has no such requirement)
- Node.js 20 or newer and the repository dependencies installed with `npm ci`
- the repository-pinned .NET SDK from `global.json`
- Bash, curl, Python 3, SHA-256 utilities, and GNU `timeout`
- ffmpeg and MD5 utilities (for the generated playback fixture and Jellyfin package checksums)
- Chromium, either from Puppeteer or selected with `RK_BROWSER_EXECUTABLE`

Run commands from this directory, or use the repository-level `./test.sh integration` entry point.

## Commands

```bash
./run.sh build             # build and pin both plugin packages
./run.sh reset             # recreate, initialise, install, and check both servers
./run.sh check             # endpoint, diagnostics, shell, ETag, and 304 checks
./run.sh browser           # Chromium lifecycle/restart test on both servers
./run.sh browser jf10      # run one browser target
./run.sh browser jf12
./run.sh lifecycle         # real install/update/disable/enable/uninstall/reinstall on both
./run.sh lifecycle jf10    # run one complete package/plugin API lifecycle
./run.sh lifecycle jf12
./run.sh third-party       # genuine v1/v2 third-party lifecycle after self-lifecycle
./run.sh third-party jf10  # run one already-provisioned target
./run.sh third-party jf12
./run.sh runner-negative   # no-Docker regression for lifecycle exit propagation
./run.sh compat            # classify the net9/JF10 package on JF12, then restore net10
./run.sh abi-floor         # isolated exact Jellyfin 10.11.0 net9 load smoke
./run.sh abi-floor-negative # no-Docker floor-smoke/validator regressions
./run.sh host-upgrade      # run both pinned in-place server-upgrade paths
./run.sh host-upgrade jf10 # Jellyfin 10.11.10 -> 10.11.11 with net9 active
./run.sh host-upgrade jf12 # 10.11.11 -> 12.0-rc4 with net9 -> net10 migration
./run.sh host-upgrade-negative # no-Docker fail-closed/static checks
./run.sh restart jf10      # bounded plain server restart
./run.sh status
./run.sh all               # ABI floor + lifecycle + browser + compatibility + host upgrades
./run.sh down              # remove this Compose project's containers, network, and volumes
./run.sh clean             # down plus this lab's generated state and captures
```

Set `RK_SKIP_BUILD=1` only when a verified `plugin/build` snapshot already
exists. `RK_BUILD_SNAPSHOT` may select a particular immutable snapshot under
`plugin/.builds`; the lab verifies it before use. Override ports with
`RK_ABI_FLOOR_PORT`, `RK_JF10_PORT`, and `RK_JF12_PORT`. A custom Compose
project name must begin with `rk-jellyfin-` and contain only lowercase letters,
digits, `_`, or `-`.

The ABI-floor runner accepts only
`jellyfin/jellyfin:10.11.0@sha256:59417f441213e236a9f907d4e71a13472042409d85f9e9310dbdd87ee33d7bd4`.
It performs a bounded pull and verifies the local repository digest and running
container identity. Set `RK_ABI_FLOOR_SKIP_PULL=1` for an explicit offline run;
the exact digest must already exist locally. The pull timeout defaults to 900
seconds and accepts `RK_ABI_FLOOR_PULL_TIMEOUT_SECONDS=1..9999`.

The host-upgrade service binds separately to `127.0.0.1:18118` by default
(`RK_HOST_UPGRADE_PORT` overrides it) and owns dedicated project-scoped config
and cache volumes. Its runner performs a bounded pull of each exact
tag-and-digest reference, then verifies the local `RepoDigests` and the running
container's configured image. `RK_HOST_UPGRADE_SKIP_PULL=1` is an explicit
offline mode: it still requires those exact digest identities to exist locally
and fails rather than accepting a tag-only alias. The pull timeout defaults to
900 seconds and can be changed with
`RK_HOST_UPGRADE_PULL_TIMEOUT_SECONDS=1..9999`.

The lifecycle leg is intentionally strict about update identity. It downloads and checksum-verifies the immutable published `1.0.0.0` package, then requires the selected candidate package to contain a genuinely newer four-component version. It does not relabel an assembly or simulate an update with two copies of the same build. Consequently, it fails during repository preparation if the selected build is still `1.0.0.0`; the release-candidate run must select the real `1.0.1.0` build.

A digest-pinned, unprivileged BusyBox service exposes only the generated catalogs and package archives on the private Compose network. Jellyfin installs them through its authenticated `Packages/Installed` API, including checksum validation. The repository has no host port and is removed with the rest of the Compose project.

## What the browser leg proves

For each matching Jellyfin generation, the Chromium test:

- completes login and opens authenticated dashboard, home, and plugin-configuration views;
- checks that the configuration page initialises and reports the endpoint generation;
- preserves a populated but hidden login password field to verify it does not block later safe reload decisions;
- keeps three authenticated documents, including hidden tabs, open across a plain server-process restart;
- verifies document identities and authentication survive without an unnecessary page reload;
- waits for API, Refresh Kit runtime, generation, and WebSocket convergence;
- records network, console, raw runtime-exception, screenshot, and restart-window evidence;
- fails on unexpected Refresh Kit-attributed browser errors.

The server checks also verify the anonymous generation/runtime endpoints, authenticated diagnostics and plugin inventory, transformed shell tags, immutable versioned runtime response, strong transformed ETag, and conditional `304` behavior.

## What the full lifecycle leg proves

For each pinned Jellyfin server, `lifecycle` starts with a pristine authenticated server and three real Chromium tabs. It then exercises:

- install of the published `1.0.0.0` package through Jellyfin's package repository API and activation after restart;
- an actual update to the selected newer candidate, including `Restart`/`Superseded` inventory state, automatic open-tab reload, new generation/runtime convergence, and rejection of stale `kit.js` URLs;
- disable and enable through Jellyfin's versioned plugin APIs, with their required restarts;
- normal browser reloads after disable that prove cached transformed HTML cannot resurrect the removed runtime;
- uninstall of every installed Refresh Kit version, a restart with no endpoints or shell injection, and normal reloads with no stale cached runtime;
- reinstall of the candidate through the package API, followed by a generation-stable restart that preserves authenticated document identities;
- a generated local movie played in Jellyfin Web, a live generation change while playback is active, continued reload blocking while playing and paused, then convergence only after leaving playback;
- logout through Jellyfin Web's real UI followed by a successful login and runtime reconvergence.

The playback file is generated locally from ffmpeg test sources and copied only into the disposable container. No external media or user library is touched.

## Genuine third-party lifecycle fixture

`third-party` is deliberately separate from Refresh Kit's self-lifecycle: the latter is not treated as proof that some other plugin's changes converge. Run it after the matching `lifecycle` target so Refresh Kit `1.0.1.0` is already active and the disposable authentication token exists.

The runner refuses stale handoff state even when two builds share the same plugin version. Before compiling or installing the fixture, it requires the success marker written only after the self-lifecycle and post-run server checks both pass, then matches the result's source revision, source-tree digest, package MD5, required final phases, repository cleanup, and browser-error audit to the exact immutable Refresh Kit snapshot selected for the third-party run.

The fixture project under `fixtures/LifecycleProbe/` compiles two real packages for each target framework:

- `1.0.0.0` / release `v1` with its own assembly and embedded/loose HTML, JavaScript, and CSS;
- `2.0.0.0` / release `v2` with a separately compiled assembly and different asset bytes.

The builder uses exact Jellyfin 10.11.0 ABI-floor and 12.0-rc4 package references, a NuGet lock file, deterministic compiler settings, fixed package timestamps, and a private project-local repository. It rejects byte-identical assemblies or archives; nothing is relabeled to simulate an update.

The fast `runner-negative` command replaces external operations with local test doubles and injects a nonzero browser-lifecycle exit. It verifies that the failure survives the individual target wrapper, the two-target `third-party all` aggregator, and the top-level `all` command even in Bash conditional contexts where implicit `errexit` behavior is suppressed.

With three authenticated tabs open and Refresh Kit continuously active, the browser scenario installs v1, updates to v2, disables and re-enables v2, then uninstalls it. It verifies exact fixture catalog entries, unique active inventory, Refresh Kit diagnostics, loaded-module and loose-asset identities, document identities, and cache convergence after every restart. The required content-generation sequence is `G0 → G1 → G2 → G0 → G2 → G0`; every transition must converge automatically in every tab. There is no manual-reload or confirmed-issue continuation path for disable, enable, or uninstall: a refusal or timeout aborts the scenario.

The JSON generation endpoint must also expose a valid nonempty process `Epoch`. The scenario requires one epoch to remain stable for the life of a process and a fresh epoch after every actual restart. A generation-stable restart must rotate only the epoch while preserving every document ID, versioned asset URL, generation, and transformed-shell digest. Historical `G2 → G0`, `G0 → G2`, and repeated `G2 → G0` returns must use fresh epochs while restoring the exact earlier generation, asset URL, and HTML identity, proving that epoch data never enters cache keys or emitted HTML. Each tab must observe the exact generation/epoch pair before convergence; the runtime confirms a historical pair twice and permits only a fresh per-process epoch to authorize that revisit. Missing, malformed, previously claimed, unwritable, or saturated epoch state remains fail-closed under the runtime contract rather than weakening the version-flap guard.

Before every lifecycle restart, the scenario derives the slowest live polling interval from the open tabs and holds the staged state for longer than both the provider cache TTL and two full browser polls, with a five-second margin and a fail-closed two-minute cap. During that window it requires the served generation and process epoch, loaded module/asset/configuration identities, versioned runtime URL, transformed-shell digest, and every document identity to remain unchanged. Uninstall must first reach a real absent/`Deleted` pending inventory state. Requested and actual hold timing are retained in the structured result.

The `compat` command is a deliberately bounded ABI experiment: it installs the Jellyfin-10/net9 package into a pristine pinned Jellyfin 12 instance, records health, inventory, endpoint, shell, and log evidence, and then restores the matching net10 package. It does not replace the broader third-party matrix in `../compat/`.

## Exact Jellyfin 10.11.0 ABI-floor smoke

`abi-floor` is deliberately separate from the current Jellyfin 10 lifecycle and
the in-place host-upgrade scenarios. Its profile-only service owns a dedicated
internal Compose network, token, config/cache volumes, and
`artifacts/abi-floor/` tree. Docker does not publish a port for a container
attached only to an `internal: true` bridge, so the service deliberately requests
no Docker port binding. Instead, a bounded Python relay owned by the runner binds
only `127.0.0.1:${RK_ABI_FLOOR_PORT}` and forwards to port 8096 at the exact
private IPv4 endpoint retained by Docker for that container and network. This is
not an arbitrary container-IP fallback: the runner cross-checks the endpoint
against the sole network member, container ID, endpoint ID, prefix, network ID,
and Compose labels before the relay starts. The
runner refuses foreign or ambiguously labelled resources, then resets only the
two exact project-qualified ABI-floor volumes. The live container must retain
those exact read/write mounts, the exact internal network name/labels, and the
same image ID resolved from the digest-pinned reference, with both requested and
effective published-port inventories empty.

The smoke installs the verified immutable snapshot's `net9.0` stage on the exact
10.11.0 image. Success requires server version `10.11.0`, the candidate plugin
to be uniquely `Active` and loaded without truncated, unavailable, last-good,
or last-known-record diagnostics. The container DLL is re-hashed after restart;
its managed MVID is used to derive the expected public `BuildId` and diagnostic
`LoadedModuleIdentity` from the pinned snapshot rather than trusting runtime
claims. The direct responses must have singleton cache policies and unambiguous
framing, while the shell ETag must equal the SHA-256 of its exact body and the
conditional response must carry the matching validator with a bodyless `304`
shape. Exact scoped log lines prove one assembly and plugin load with no scoped
warning/error. Raw anonymous HTTP proof is copied byte-for-byte only after a
fail-closed credential scan; authenticated diagnostics/inventory and the
sanitized server log remain separately retained. `scripts/abi_floor_evidence.py`
also cross-checks the relay's loopback bind, fixed target, implementation hash,
parent-scoped lifecycle, connection/buffer limits, zero rejections or target
failures, coherent traffic counters, and atomic completion receipt against the
same internal-network evidence. Bootstrap traffic is finalized separately, then
the stable endpoint/shell checks use a fresh retained relay receipt. The validator
cross-checks all proof against the same snapshot's stage metadata, DLL, MVID, and
package bytes. The no-Docker negative command and validator mutation suite
fail unless every mismatch is rejected. This describes the proof contract; a
passing live result must come from an actual `abi-floor` run and is not inferred
from the harness itself.

## In-place Jellyfin host upgrades and live load coverage

`host-upgrade` uses one additional profile-only Compose service. Each scenario
starts with new dedicated volumes, then replaces only that service container;
the exact `/config` and `/cache` volume names must remain unchanged. The two
independent, pinned paths are:

- Jellyfin **10.11.10** to **10.11.11**, preserving the active net9 Refresh Kit
  installation, configuration, users, indexed media, browser storage, and open
  documents;
- Jellyfin **10.11.11** to **12.0-rc4**, first disabling net9 Refresh Kit through
  Jellyfin's real versioned plugin API and applying that state on 10.11.11,
  replacing its files with the checksum-verified net10 stage, migrating the
  unchanged server volumes, and re-enabling it through the Jellyfin 12 API.

The second sequence deliberately follows Jellyfin 12's external-plugin
migration boundary; it does not pretend that a loaded net9 assembly can remain
active inside the net10 host. Both paths record the exact source/target image
references, local image IDs and repository digests, immutable Refresh Kit stage
metadata/DLL hashes, server versions, plugin inventory transitions, and volume
identities. A mismatched image, stage, user, status, volume, version, generation,
or epoch aborts the run.

One cache-enabled Chromium process remains open throughout. It exercises three
isolated contexts (the exact administrator identity, a distinct exact viewer
identity, and an unauthenticated login context) and explicit **1**, **2**, then
**10** Jellyfin-tab checkpoints. The ten-tab role inventory includes an admin
dashboard, the real Refresh Kit configuration editor, the real Jellyfin plugin
detail page, an admin background document, viewer home, three viewer background
documents, one real viewer playback document, and an anonymous login document.
That inventory is exactly ten tabs. Every page carries a session-persistent load
counter and a per-document ID; each authenticated generation transition must
resolve the exact expected user, while the login tab must remain anonymous.
Every browser-convergence checkpoint requires exactly one reload, without a
reload storm; the deliberately coalesced stress mutations are one catch-up
checkpoint rather than three forced intermediate reloads.

The plugin-detail page opens Jellyfin Web's actual Uninstall confirmation
(`role=dialog`) but cancels it. While that dialog is rendered, the runner makes
a unique monitored loose-asset change and requires the runtime to report the
new generation without reloading the protected document. A real focused field
on the Refresh Kit configuration page independently proves the text-entry gate.
After Cancel and blur, both documents must converge automatically, and plugin
inventory must prove that cancellation did not uninstall anything.

The same ten-tab mutation also uses ffmpeg to generate a deterministic 120-second
H.264/AAC MP4, copies it to `/config/rk-host-upgrade-media`, creates a real Movies
library for that preserved path, and waits until the exact viewer can resolve the
indexed Movie and its media source. The viewer playback tab opens the exact item
through Jellyfin Web's real details page, starts its Play action, and requires a
decoded video element with advancing `currentTime`. During the monitored
generation change, the tab must keep the same document/load identity and current
generation while observing the newer latest generation, with a media/playback
safety reason. It then pauses without reloading, leaves playback for the home
route, and converges through exactly one automatic reload. The fixture SHA-256,
library identity, item ID/path/runtime/media-source count, and viewer indexing
must all remain exact after the same `/config` volume is mounted into the target
host. This is independent retained evidence from the separate lifecycle playback
scenario; neither result is used as a substitute for the other.

Finally, three post-mutation waves use **10**, **50**, and **100** genuinely
concurrent, independent TCP clients against `/RefreshKit/Generation`. Every
response must be status 200 JSON with `Cache-Control: no-store`, one exact
version/build/generation/process-epoch identity, and bounded per-request and
whole-wave time. The retained result links this real HTTP evidence to
`ActivePluginGenerationTests.ConcurrentGenerationReadsShareExactlyOneScanPerInvalidation(int readerCount)`,
which deterministically proves one provider scan/content read per 10/50/100
request wave after cold-cache invalidation, and separately identifies the transformed-shell middleware's
single-flight regression. Those tests cover distinct layers and are not treated
as interchangeable evidence.

## Scope and limitations

This lab automates fresh provisioning, real package install/update, disable/enable, uninstall/reinstall, logout/login, local indexed-media playback gating, matching-package configuration, authenticated navigation, cached-shell checks, open-tab restart convergence, the cross-generation package experiment, and the two explicit in-place host-upgrade paths above. It does **not** exercise DRM/external-player media or claim Firefox/WebKit coverage. No unlisted server-upgrade path should be inferred from these two results.

The default credentials are disposable lab credentials. Access tokens are written under `.state/` with private permissions and are never part of retained evidence. Do not point this rig at an existing Jellyfin installation.

## Evidence and cleanup

Structured results, redacted network/console/WebSocket captures, screenshots,
and selected server evidence are written below `artifacts/`; tokens remain below
`.state/`. ABI-floor evidence is isolated at `artifacts/abi-floor/`.
The ABI-floor relay is a child of the foreground smoke command, stops before
successful evidence is finalized, and also has shell-exit and Linux parent-death
cleanup; cleanup commands never search for or signal unrelated host processes.
Host-upgrade results are kept separately at
`artifacts/host-upgrade/jf10/result.json` and
`artifacts/host-upgrade/jf12/result.json`, with
`artifacts/host-upgrade/result.json` aggregating both when `all` is requested.
A new attempt rotates rather than deletes earlier ABI-floor or per-scenario
artifacts and rotates the prior host-upgrade aggregate before replacing it, so
setup or migration failure cannot erase prior evidence. Both scratch trees are
ignored by Git. A normal successful run may retain captures for inspection
while `./run.sh down` removes all Compose-owned runtime resources. Use
`./run.sh clean` to remove both runtime resources and generated captures.

The cleanup commands validate the project and scratch paths before deleting anything. They do not remove unrelated Docker containers, images, networks, volumes, or host files.
