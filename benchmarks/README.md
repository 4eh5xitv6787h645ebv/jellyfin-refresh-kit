# Refresh Kit microbenchmarks

This is an opt-in, non-gating benchmark harness for the hot paths that are hard
to see in functional tests. It deliberately has no pass/fail thresholds. Record
results before and after a change on the same otherwise-idle machine and compare
the distributions and allocations.

From the repository root:

```bash
npm ci
./benchmarks/run.sh > /tmp/refresh-kit-benchmarks.jsonl
```

The script enters the repository root before invoking .NET, so the SDK pinned
by `global.json` is also honored when `run.sh` is launched by an absolute path
from another working directory.

`run.sh` writes build progress to stderr and newline-delimited JSON (JSONL) to
stdout. Each line is independently parseable. Environment records identify the
source revision, runtime, OS, architecture, processor count, .NET GC mode,
Chromium build, and sample count. Measurement records contain the raw samples,
median, nearest-rank p95, and allocation or retained-heap observations. Set
`RK_BROWSER_EXECUTABLE` to select Chromium, or
`RK_BENCH_BROWSER_REPETITIONS` (3-25, default 7) to change the browser sample
count.

The server harness compiles the exact current plugin source for net10.0 against
the same exact Jellyfin 12 packages as the plugin project. Its fixtures are:

| Scale | Provider fixture | HTML payload |
| ---: | --- | ---: |
| 5 | 8 x 8 KiB JS assets + one config per plugin | 64 KiB |
| 25 | same per plugin | 256 KiB |
| 50 | same per plugin | 1 MiB |
| 100 | same per plugin | 1,900 KiB |

For each scale it measures:

- an invalidated provider rescan with a warm filesystem cache;
- a provider TTL-cache hit;
- third-party tag stamping (one script and one stylesheet per plugin);
- a cold identity and gzip middleware transform; and
- a warm middleware hit after the source answers 304.

The middleware fixture uses an in-process ASP.NET response pipeline and a
counting destination stream. It includes decode/rewrite/re-encode and cache
logic, but intentionally excludes sockets, TLS, proxying, and client transport
time. The browser fixture opens a fresh page per sample, silences console I/O,
and evaluates 5/25/50/100 complete runtime copies with distinct inert instances.
That is a multi-copy registration stress case, not ordinary steady-state page
cost. Chromium is launched with precise heap reporting and exposed GC when the
installed build supports them.

Absolute timings are sensitive to CPU frequency scaling, filesystem cache,
runtime/Chromium versions, background load, and thermal state. Do not compare
numbers from unlike environments, and do not turn a single noisy p95 into a CI
gate. This harness complements, rather than replaces, the functional,
integration, lifecycle, and browser suites.
