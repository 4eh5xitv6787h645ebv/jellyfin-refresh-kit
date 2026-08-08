'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const puppeteer = require('puppeteer');

const schema = 'refresh-kit-benchmark-v1';
const root = path.resolve(__dirname, '..');
const runtime = fs.readFileSync(path.join(root, 'jellyfin-refresh-kit.js'), 'utf8');
const scales = [5, 25, 50, 100];

function browserExecutable() {
  const candidates = [
    process.env.RK_BROWSER_EXECUTABLE,
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
    puppeteer.executablePath(),
  ].filter(Boolean);
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }
  throw new Error('No Chromium executable found; set RK_BROWSER_EXECUTABLE');
}

function sampleCount() {
  const raw = Number(process.env.RK_BENCH_BROWSER_REPETITIONS || 7);
  if (!Number.isInteger(raw) || raw < 3 || raw > 25) {
    throw new Error('RK_BENCH_BROWSER_REPETITIONS must be an integer from 3 through 25');
  }
  return raw;
}

function median(ordered) {
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2
    ? ordered[middle]
    : (ordered[middle - 1] + ordered[middle]) / 2;
}

function nearestRank(ordered, percentile) {
  const index = Math.max(0, Math.min(
    ordered.length - 1,
    Math.ceil(ordered.length * percentile) - 1,
  ));
  return ordered[index];
}

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

async function measurePage(browser, count) {
  const page = await browser.newPage();
  try {
    await page.goto('about:blank');
    return await page.evaluate(({ source, copies }) => {
      const savedConsole = {
        debug: console.debug,
        info: console.info,
        log: console.log,
        warn: console.warn,
      };
      console.debug = console.info = console.log = console.warn = function () {};
      try {
        if (typeof globalThis.gc === 'function') globalThis.gc();
        const heapBefore = performance.memory
          && Number.isFinite(performance.memory.usedJSHeapSize)
          ? performance.memory.usedJSHeapSize
          : null;
        const started = performance.now();
        for (let index = 0; index < copies; index += 1) {
          window.JellyfinRefreshKitConfig = {
            name: `Benchmark-${index}`,
            bootVersion: 'g-browser-benchmark',
            mode: 'off',
            pollSeconds: 3600,
          };
          (0, eval)(source);
        }
        const durationMs = performance.now() - started;
        const observed = window.JellyfinRefreshKit.instances().length;
        delete window.JellyfinRefreshKitConfig;
        if (typeof globalThis.gc === 'function') globalThis.gc();
        const heapAfter = performance.memory
          && Number.isFinite(performance.memory.usedJSHeapSize)
          ? performance.memory.usedJSHeapSize
          : null;
        return {
          durationMs,
          heapDeltaBytes: heapBefore === null || heapAfter === null
            ? null
            : heapAfter - heapBefore,
          observed,
          gcExposed: typeof globalThis.gc === 'function',
        };
      } finally {
        console.debug = savedConsole.debug;
        console.info = savedConsole.info;
        console.log = savedConsole.log;
        console.warn = savedConsole.warn;
      }
    }, { source: runtime, copies: count });
  } finally {
    await page.close();
  }
}

async function main() {
  const repetitions = sampleCount();
  const executablePath = browserExecutable();
  const browser = await puppeteer.launch({
    executablePath,
    headless: true,
    args: [
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--enable-precise-memory-info',
      '--js-flags=--expose-gc',
    ],
  });
  try {
    emit({
      schema,
      record: 'environment',
      component: 'browser',
      capturedAtUtc: new Date().toISOString(),
      sourceRevision: process.env.RK_BENCH_SOURCE_REVISION || 'unknown',
      sourceDirty: process.env.RK_BENCH_SOURCE_DIRTY || 'unknown',
      node: process.version,
      v8: process.versions.v8,
      platform: process.platform,
      architecture: process.arch,
      processorCount: os.availableParallelism(),
      cpuModel: os.cpus()[0] ? os.cpus()[0].model : 'unknown',
      browser: await browser.version(),
      executablePath,
      repetitions,
      consoleSilenced: true,
    });

    const warmup = await measurePage(browser, 1);
    if (warmup.observed !== 1) {
      throw new Error(`browser warmup registered ${warmup.observed} instances, expected 1`);
    }

    for (const scale of scales) {
      const durations = [];
      const heapDeltas = [];
      let gcExposed = true;
      for (let index = 0; index < repetitions; index += 1) {
        const observed = await measurePage(browser, scale);
        if (observed.observed !== scale) {
          throw new Error(
            `browser scale ${scale} registered ${observed.observed} instances`,
          );
        }
        durations.push(observed.durationMs);
        if (observed.heapDeltaBytes !== null) heapDeltas.push(observed.heapDeltaBytes);
        gcExposed = gcExposed && observed.gcExposed;
      }

      durations.sort((left, right) => left - right);
      heapDeltas.sort((left, right) => left - right);
      emit({
        schema,
        record: 'measurement',
        component: 'browser',
        scenario: 'runtime.multi-copy-registration',
        scale,
        unit: 'milliseconds',
        sampleCount: durations.length,
        median: median(durations),
        p95: nearestRank(durations, 0.95),
        samples: durations,
        retainedHeapDeltaBytes: heapDeltas.length ? {
          sampleCount: heapDeltas.length,
          median: median(heapDeltas),
          p95: nearestRank(heapDeltas, 0.95),
          samples: heapDeltas,
        } : null,
        fixture: {
          completeRuntimeCopies: scale,
          distinctInstances: scale,
          mode: 'off',
          freshPagePerSample: true,
          gcExposed,
        },
      });
    }
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exitCode = 1;
});
