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
| 8119 | `rk-caddy` | Caddy, the docs' two-line `reverse_proxy` |
| 8120 | `rk-traefik` | Traefik v3 |
| 8121 | `rk-haproxy` | HAProxy |
| 8122 | `rk-nginx-cache-naive` | **adversarial**: `proxy_cache` + `proxy_ignore_headers Cache-Control` |
| 8124 | `rk-nginx-cache-respect` | `proxy_cache` that honours the origin's `Cache-Control` |
| 8125 | `rk-nginx-subpath` | subpath: `location /jellyfin` + Jellyfin `BaseUrl=/jellyfin` |
| 8126 | `rk-nginx-cache-fix1` | remedy 1 — the ignore-headers line removed |
| 8127 | `rk-nginx-cache-fix2` | remedy 1 + the shell and `/RefreshKit/` exempted from the cache |

Port 8123 is simply unassigned by this project. Credentials are `rk_admin` /
`Test669Pw!x`; the admin token
lands in the project-specific `.state/<project>.token` file (git-ignored,
created under `umask 077`, and removed by `down`).

### Traefik uses the file provider, not the docker provider

`conf/traefik-dyn.yml` declares the router and service directly. Traefik v3's
docker provider negotiates Docker API 1.24, which Docker Engine 28 rejects
outright (`client version 1.24 is too old`), so the label-driven form cannot
start on a current engine. The proxy behaviour under test is identical. The
equivalent labels, for a rig on an older engine:

```yaml
labels:
  - traefik.enable=true
  - "traefik.http.routers.jf.rule=PathPrefix(`/`)"
  - traefik.http.routers.jf.entrypoints=web
  - traefik.http.services.jf.loadbalancer.server.port=8096
```

## What each leg checks

**`matrix`** (`lib/matrix.sh`, one run per proxy) — 17 or 18 assertions against
the app shell and the kit's endpoints, depending on whether the proxy offers a
Brotli representation: a `rk-` ETag arrives through the proxy; a matching
`If-None-Match` gets a real `304`; a stale one gets `200`; a bad `If-Match` gets
`412`; gzip comes back with one matching `Content-Encoding` and a decodable body
(the double-compression trap); Brotli is either one valid `br` representation or
an intact identity response with no content coding; each coding actually offered
revalidates on its own representation ETag;
`/RefreshKit/Generation`, `Generation.txt` and `kit.js` are reachable
unauthenticated; and the shell's injected `<script>` tag carries the generation
the endpoint is reporting right now.

Provisioning parks independent shell injectors for this leg, so `matrix`
deliberately exercises Refresh Kit's ordinary final-response ownership and
strong-validator contract. It is not evidence for a nested outer response
buffer; the three exact safe-degradation matrices are documented in
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
back.

## Requirements

* Docker with the compose plugin. Images are pulled on first run.
* Node.js matching `.node-version`, with the repository's locked packages
  installed by `npm ci`.
* `python3` (used only to edit two JSON config blobs).
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

If a run was interrupted before `run.sh subpath` restored the base URL, the
origin is still on `/jellyfin`; `./run.sh down` removes it either way.

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
| `RK_JE_VERSION` | `12.1.0.0` | folder version for the downloaded Jellyfin Enhanced |
| `RK_SKIP_BUILD` | `0` | set to `1` only to reuse an explicitly selected immutable snapshot |
| `RK_BUILD_SNAPSHOT` | unset | canonical directory directly under `plugin/.builds`; required with `RK_SKIP_BUILD=1`, verified again at the provisioning boundary |
