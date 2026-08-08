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
cd e2e/proxy
export NODE_PATH=$HOME/.nvm/versions/node/v22.20.0/lib/node_modules  # puppeteer + ws
./run.sh up        # rig + wizard + both plugins + admin token
./run.sh matrix    # the curl freshness matrix, every proxy
./run.sh ws        # websocket regression check, every proxy
./run.sh cache     # the misconfigured-cache demo and both remedies
./run.sh e2e       # puppeteer: login -> bump -> exactly one smart reload
./run.sh subpath   # BaseUrl=/jellyfin, test :8125, restore
./run.sh down      # destroy everything
```

`./run.sh all` does the lot in order. A full pass takes roughly 25 minutes,
almost all of it the browser leg (a bump has to be detected by a 15 s poll, and
the runs are strictly sequential because a generation bump is server-wide — two
concurrent browsers would each see the other's reload).

## What is running where

| Port | Container | Setup |
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

8123 is deliberately skipped (it belongs to an unrelated long-lived container on
the author's box). Credentials are `rk_admin` / `Test669Pw!x`; the admin token
lands in `.rk-token` (git-ignored).

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

**`matrix`** (`lib/matrix.sh`, one run per proxy) — 18 assertions against the app
shell and the kit's endpoints: a `rk-` ETag arrives through the proxy; a matching
`If-None-Match` gets a real `304`; a stale one gets `200`; a bad `If-Match` gets
`412`; `Accept-Encoding: gzip` and `br` each come back with exactly one
`Content-Encoding` header and a body that actually decodes (the double-compression
trap); each coding revalidates on its own representation ETag;
`/RefreshKit/Generation`, `Generation.txt` and `kit.js` are reachable
unauthenticated; and the shell's injected `<script>` tag carries the generation
the endpoint is reporting right now.

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
URLs, touches a plugin binary, and then requires **exactly one** reload followed
by stamped URLs carrying the new generation, with zero kit-attributed console
output.

> The E2E clears any `input[type=password]` value after logging in. Jellyfin
> 10.11 keeps the login view in the DOM (`class="… hide"`) with the typed
> password still in `#txtManualPassword`, and the kit's 2.4.0 `password_entry`
> gate counts hidden fields — so without that line the harness measures the gate
> instead of the proxy. See the note in `plugin/README.md`.

**`subpath`** — sets `BaseUrl=/jellyfin` through
`POST /System/Configuration/network` (**not** `/System/Configuration`, where the
field is silently ignored on 10.11), restarts, runs matrix + ws + e2e against
`:8125/jellyfin`, and puts `BaseUrl` back.

## Requirements

* Docker with the compose plugin. Images are pulled on first run.
* `node` with `puppeteer` and `ws` resolvable — set `NODE_PATH` as above.
* `python3` (used only to edit two JSON config blobs).
* The .NET SDK, *only* if `plugin/build/stage/` is missing: `provision.sh` then
  runs `plugin/build.sh` for you (`DOTNET_ROOT` defaults to `~/.dotnet`).

## Teardown

```bash
./run.sh down     # docker compose down -v --remove-orphans, plus .rk-token
```

If a run was interrupted before `run.sh subpath` restored the base URL, the
origin is still on `/jellyfin`; `./run.sh down` removes it either way.

## Knobs

| Variable | Default | Meaning |
|---|---|---|
| `NODE_PATH` | `~/.nvm/versions/node/v22.20.0/lib/node_modules` | where `puppeteer`/`ws` live |
| `RK_CONTAINER` | `rk-jf` | origin container name |
| `RK_ORIGIN` | `http://127.0.0.1:8116` | origin base URL |
| `RK_USER` / `RK_PASS` | `rk_admin` / `Test669Pw!x` | admin credentials |
| `RK_BUMP_FILE` | the Jellyfin Enhanced DLL | the file whose mtime moves the generation |
| `RK_JE_VERSION` | `12.1.0.0` | folder version for the downloaded Jellyfin Enhanced |
