# Reverse-proxy / CDN validation suite

Runs the Jellyfin Refresh Kit behind the reverse proxies real Jellyfin users
actually deploy, and answers one question per setup: **does a client behind this
proxy still find out that a plugin changed?**

Everything is throwaway. The rig creates its own Jellyfin origin, its own
volumes and its own network, and `./run.sh down` removes all of it. It never
touches a pre-existing Jellyfin container.

---

## Quick start

```bash
cd /path/to/jellyfin-refresh-kit
npm ci              # locked puppeteer + ws dependencies
cd e2e/proxy
./run.sh up        # rig + wizard + both plugins + admin token
./run.sh matrix    # the curl freshness matrix, every proxy
./run.sh ws        # websocket regression check, every proxy
./run.sh cache     # the misconfigured-cache demo and both remedies
./run.sh e2e       # puppeteer: login -> bump -> exactly one smart reload
./run.sh subpath   # BaseUrl=/jellyfin, test the subpath proxy, restore
./run.sh down      # destroy everything
./lib/static-regressions.sh  # no-Docker contract/configuration checks
```

`./run.sh all` does the lot in order. A full pass takes roughly 25 minutes,
almost all of it the browser leg (a bump has to be detected by a 15 s poll, and
the runs are strictly sequential because a generation bump is server-wide — two
concurrent browsers would each see the other's reload).

## What is running where

All host listeners bind to `127.0.0.1`. Compose container names are generated
inside the project selected by `RK_PROXY_PROJECT` (default
`rk-proxy-<uid>`); the table uses service names, not global container names.
Every default port can be overridden with the corresponding variable in
“Knobs”.

| Default port | Compose service | Setup |
|---|---|---|
| 8116 | `rk-jf` | the origin — Jellyfin 10.11.11, no proxy (baseline) |
| 8117 | `rk-nginx-official` | the official jellyfin.org/docs nginx config |
| 8118 | `rk-nginx-npm` | Nginx Proxy Manager-style (websocket upgrade + "Cache Assets") |
| 8119 | `rk-caddy` | Caddy, with synthetic upstream gzip disabled |
| 8120 | `rk-traefik` | Traefik v3, with absent `Accept-Encoding` normalized to identity |
| 8121 | `rk-haproxy` | HAProxy |
| 8122 | `rk-nginx-cache-naive` | **adversarial**: `proxy_cache` + `proxy_ignore_headers Cache-Control` |
| 8124 | `rk-nginx-cache-respect` | freshness control: `proxy_cache` honours origin `Cache-Control`, but consumes client conditionals |
| 8125 | `rk-nginx-subpath` | subpath: `location /jellyfin` + Jellyfin `BaseUrl=/jellyfin` |
| 8126 | `rk-nginx-cache-fix1` | remedy 1 — origin freshness restored; active nginx cache still consumes client conditionals |
| 8127 | `rk-nginx-cache-fix2` | remedy 1 + validator-sensitive paths exempted, preserving strict 304/412 behaviour |

Port 8123 is simply unassigned by this project. Credentials are `rk_admin` /
`Test669Pw!x`; the admin token
lands in the project-specific `.state/<project>.token` file (git-ignored,
created under `umask 077`, and removed by `down`).

### Traefik uses the file provider, not the docker provider

`conf/traefik-dyn.yml` declares the routers, middleware and service directly.
Its higher-priority router preserves every explicit client `Accept-Encoding`;
the fallback router sets `Accept-Encoding: identity` only when the field was
absent. This prevents Go's HTTP transport from silently requesting and decoding
gzip while retaining the coded representation's strong ETag. Caddy's equivalent
guard is `transport http { compression off }`; explicit gzip and Brotli requests
still pass through both proxies unchanged.

Traefik v3's docker provider negotiates Docker API 1.24, which Docker Engine 28
rejects outright (`client version 1.24 is too old`), so the label-driven form
cannot start on a current engine. The proxy behaviour under test is identical.
The equivalent labels, for a rig on an older engine:

```yaml
labels:
  - traefik.enable=true
  - "traefik.http.routers.jf-explicit.rule=PathPrefix(`/`) && HeaderRegexp(`Accept-Encoding`, `.+`)"
  - traefik.http.routers.jf-explicit.priority=100
  - traefik.http.routers.jf-explicit.entrypoints=web
  - traefik.http.routers.jf-explicit.service=jf
  - "traefik.http.routers.jf-default.rule=PathPrefix(`/`)"
  - traefik.http.routers.jf-default.priority=10
  - traefik.http.routers.jf-default.entrypoints=web
  - traefik.http.routers.jf-default.middlewares=jf-force-identity
  - traefik.http.routers.jf-default.service=jf
  - traefik.http.middlewares.jf-force-identity.headers.customrequestheaders.Accept-Encoding=identity
  - traefik.http.services.jf.loadbalancer.server.port=8096
```

## What each leg checks

**`matrix`** (`lib/matrix.sh`) applies one of two explicit, fail-closed
contracts. The strict contract covers the origin, the ordinary nginx/NPM/Caddy/
Traefik/HAProxy proxies and remedy 2 (plus the subpath proxy during `subpath`). It
requires matching identity/gzip/Brotli `If-None-Match` requests to produce a
bodyless `304`, a bad `If-Match` to produce a bodyless, no-store `412` without
representation metadata, and ordinary `200` responses to retain an exact
complete representation, ETag, content coding and byte-accurate
`Content-Length`. Every `rk-` ETag must be
strong. Every pair among identity, gzip and Brotli whose transferred bytes
differ must carry distinct validators.

Identity checks send `Accept-Encoding: identity` explicitly. A separate request
with the field absent must resolve to the exact identity response; this catches
transparent decoding that leaves a coded representation's ETag behind. A `304`
must repeat the selected ETag, `Cache-Control` and `Vary` and have no content.
`Content-Type` and `Content-Encoding` may be omitted; if present they must match
the selected `200`. `Content-Length` may be omitted or equal that selected
representation's length, as RFC 9110 permits. A bodyless `412` may similarly
omit `Content-Length` or send one `Content-Length: 0`.

The two active-nginx-cache freshness controls (ports 8124 and 8126) use the
`nginx-cache-suppresses-conditionals` contract. nginx does not pass a client's
`If-None-Match`, `If-Match`, `Range` or `If-Range` fields upstream while caching
is enabled. Matching and failing client preconditions must therefore return the
exact complete cached `200` representation with the expected ETag, coding and
length. Those are asserted observations, not ignored failures, and this group is
never reported as preserving strong end-to-end validators. Its separate cache
and browser legs still prove that respecting origin `Cache-Control` keeps users
fresh.

Both contracts require gzip negotiation to remain encoded. Brotli must be a
valid single coding when available and may otherwise use the exact identity
fallback. They also require independently decodable HTML, public generation endpoints,
agreement between `Generation` and `Generation.txt`, a non-empty `kit.js`, and a
shell script tag stamped with the live generation. A request, decoder or header
check that cannot be completed is a failure; the deliberately stale port 8122
remains a positive control for `cache`, not a matrix candidate.

Provisioning parks independent shell injectors for this leg, so `matrix`
deliberately exercises Refresh Kit's ordinary final-response ownership and
strong-validator contract. It is not evidence for a nested outer response
buffer; the five exact safe-degradation matrices are documented in
`../compat/README.md`.

**`ws`** (`lib/ws.js`) — opens `/socket` through the proxy with the admin token
and waits for a real message. This is a regression check on the combination: the
kit's middleware sits in front of the whole web app, and a middleware that
buffers or rewrites the wrong response can quietly kill the websocket upgrade.

**`cache`** (`lib/cache-adversarial.sh`) — primes the caches, moves the
generation at the origin, and prints what each proxy serves afterwards. The
naive cache keeps serving the old shell *and* the old generation; both remedies
track the origin.

**`e2e`** (`lib/e2e.js`, puppeteer, headless, `--no-sandbox`) — logs in through
the proxy, asserts the kit manager registered its instance, records the stamped
URLs, replaces the contents of a monitored loose `.js` asset, and then requires
**exactly one** reload followed
by stamped URLs carrying the new generation, with zero kit-attributed console
output.

The E2E deliberately leaves Jellyfin 10.11's populated login password field in
its retained, hidden page. That is a regression assertion: an inactive hidden
login view must not block a later safe reload, while a visible interactive
password field still must.

**`subpath`** — sets `BaseUrl=/jellyfin` through
`POST /System/Configuration/network` (**not** `/System/Configuration`, where the
field is silently ignored on 10.11), restarts, runs matrix + ws + e2e against
the configured subpath port (default `8125`) at `/jellyfin`, and puts `BaseUrl`
back. Every restart readiness request uses the active BaseUrl. Before mutation,
the exact original network document is retained in a mode-0600 temporary file.
EXIT, INT and TERM all replay it through whichever route is active and restore
injectors before temporary state is removed. Injector parking uses exclusive
state creation, refuses stale state or same-name restore collisions, and never
suppresses a move failure. A failed restart or readiness probe stops dependent
checks and returns failure. If scoped rollback still cannot complete, the suite
fails closed by removing its throwaway Compose project and volume rather than
leaving a changed BaseUrl or parked injector behind.

## Container-free regressions

`./lib/static-regressions.sh` needs Bash, Python and curl, but does not
invoke Docker. It syntax-checks the runner, checks the Traefik split-router
configuration, checks Caddy's transport guard, exercises successful/stale/
collision injector state transitions in a temporary directory, and runs both
matrix contracts against a loopback HTTP fixture. Negative fixtures prove that
weak validators and shared identity/gzip/Brotli validators fail the matrix.

## Third-party fixture lock

The Jellyfin Enhanced jf10 fixture has no independent proxy-suite version,
URL, ABI, framework or digest default. `run.sh` and provisioning load the
`jellyfin-enhanced-jf10` row from `../compat/ecosystem.lock.json`, currently
version `12.2.0.0`, and reject a conflicting `RK_JE_VERSION`. The locked archive
is downloaded to a temporary file, SHA-256 verified, atomically promoted into
the local cache, extracted into an isolated staging directory, and only then
installed. Provisioning replaces every older Jellyfin Enhanced plugin directory
with the verified staged directory before restart, so a persistent test volume
cannot accidentally load 12.1 beside 12.2.

## Requirements

* Docker with the compose plugin. Images are pulled on first run.
* Node.js matching `.node-version`, with the repository's locked packages
  installed by `npm ci`.
* `python3` (lock validation, fixture metadata and two JSON config blobs), plus
  either the `brotli` command or Python's `brotli` module when a proxy offers a
  Brotli representation. `gzip` is required for the equivalent gzip check.
* The repository-pinned .NET SDK. The runner builds, resolves, and verifies one
  immutable plugin snapshot before it creates Docker resources. Provisioning is
  passed that canonical directory and never re-resolves the mutable
  `plugin/build` link. Set `RK_SKIP_BUILD=1` only together with an explicit
  matching `RK_BUILD_SNAPSHOT`.
* GNU/Linux shell tools used by the deterministic build (`bash`, `flock`,
  `readlink -f`, and `timeout`).

## Teardown

```bash
./run.sh down     # project-scoped compose down -v, plus its token file
```

Catchable EXIT/INT/TERM paths restore active injector/BaseUrl state. If rollback
cannot be verified, the runner removes the project and its volume. `SIGKILL` and
an unavailable Docker daemon cannot be handled in-process; once Docker is
available, `./run.sh down` removes the project-scoped fixture either way.

## Knobs

| Variable | Default | Meaning |
|---|---|---|
| `NODE_PATH` | inherited | where `puppeteer`/`ws` are resolvable |
| `RK_PROXY_PROJECT` | `rk-proxy-<uid>` | isolated Compose project name |
| `RK_PROXY_ORIGIN_PORT` | `8116` | loopback origin port |
| `RK_PROXY_NGINX_OFFICIAL_PORT` | `8117` | official-nginx loopback port |
| `RK_PROXY_NGINX_NPM_PORT` | `8118` | NPM-style loopback port |
| `RK_PROXY_CADDY_PORT` | `8119` | Caddy loopback port |
| `RK_PROXY_TRAEFIK_PORT` | `8120` | Traefik loopback port |
| `RK_PROXY_HAPROXY_PORT` | `8121` | HAProxy loopback port |
| `RK_PROXY_CACHE_NAIVE_PORT` | `8122` | adversarial-cache loopback port |
| `RK_PROXY_CACHE_RESPECT_PORT` | `8124` | respecting-cache loopback port |
| `RK_PROXY_SUBPATH_PORT` | `8125` | subpath-proxy loopback port |
| `RK_PROXY_CACHE_FIX1_PORT` | `8126` | first-remedy loopback port |
| `RK_PROXY_CACHE_FIX2_PORT` | `8127` | second-remedy loopback port |
| `RK_USER` / `RK_PASS` | `rk_admin` / `Test669Pw!x` | admin credentials |
| `RK_BUMP_FILE` | `rk-e2e-generation.js` in the Jellyfin Enhanced plugin folder | the loose client asset whose contents are changed to move the generation |
| `RK_JE_VERSION` | lock-derived (`12.2.0.0` currently) | normally unset; if supplied it must exactly match `../compat/ecosystem.lock.json` |
| `RK_SKIP_BUILD` | `0` | set to `1` only to reuse an explicitly selected immutable snapshot |
| `RK_BUILD_SNAPSHOT` | unset | canonical directory directly under `plugin/.builds`; required with `RK_SKIP_BUILD=1`, verified again at the provisioning boundary |
