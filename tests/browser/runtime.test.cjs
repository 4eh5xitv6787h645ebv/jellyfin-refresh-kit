'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const test = require('node:test');
const zlib = require('node:zlib');
const puppeteer = require('puppeteer');

const root = path.resolve(__dirname, '..', '..');
const runtime = fs.readFileSync(path.join(root, 'jellyfin-refresh-kit.js'), 'utf8');
const historicalRuntime242 = fs.readFileSync(
  path.join(__dirname, 'fixtures', 'jellyfin-refresh-kit-2.4.2.js'),
  'utf8',
);

const storageKeys = {
  budget: 'jellyfin-refresh-kit-budget-v1',
  epochs: 'jellyfin-refresh-kit-epochs-v1',
  gaps: 'jellyfin-refresh-kit-epoch-gaps-v1',
  flips: 'jellyfin-refresh-kit-flips-v1',
  left: 'jellyfin-refresh-kit-left-v1',
  recovery: 'jellyfin-refresh-kit-recovery-v1',
  tab: 'jellyfin-refresh-kit-tab-v1',
};

test('a fresh process epoch authorizes one legitimate historical generation rollback', async (t) => {
  let versionRequests = 0;
  let versionResponse = { CacheKey: 'G0', Epoch: 'process-g0-fresh' };
  const origin = await startServer(t, (req, res) => {
    if (new URL(req.url, 'http://runtime.test').pathname === '/version') {
      versionRequests += 1;
      res.writeHead(200, {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store',
      });
      res.end(JSON.stringify(versionResponse));
      return;
    }
    serveHtml(res);
  });

  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/#/home`);
  await page.evaluate(({ keys, base }) => {
    sessionStorage.setItem(keys.left, JSON.stringify(['EpochRollbackTest|G0']));
    window.JellyfinRefreshKitConfig = {
      name: 'EpochRollbackTest',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: `${base}/version`,
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
    };
  }, { keys: storageKeys, base: origin });
  const fastConfirmRuntime = runtime.replace(
    'var VERSION_CONFIRM_MS = 1500;',
    'var VERSION_CONFIRM_MS = 25;',
  );
  assert.notEqual(fastConfirmRuntime, runtime, 'test confirmation delay was accelerated');
  await injectRuntime(page, fastConfirmRuntime);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('EpochRollbackTest')?.state().candidateVersion === 'G0'
  ));
  await new Promise((resolve) => setTimeout(resolve, 100));

  const observed = await page.evaluate((keys) => ({
    state: window.JellyfinRefreshKit.get('EpochRollbackTest').state(),
    epochs: sessionStorage.getItem(keys.epochs),
  }), storageKeys);
  assert.ok(versionRequests >= 2, 'the exact version/epoch pair was observed twice');
  assert.equal(observed.state.updatePending, true);
  assert.equal(observed.state.latestEpoch, 'process-g0-fresh');
  assert.match(observed.epochs || '', /process-g0-fresh/);

  // Authorization is exact and one-use. Returning to the baseline clears the
  // live intent but keeps the claimed epoch, so the same historical process
  // can never authorize a second revisit.
  versionResponse = { CacheKey: 'G2', Epoch: 'process-g2-current' };
  await page.evaluate(() => window.JellyfinRefreshKit.get('EpochRollbackTest').checkNow());
  assert.equal(await page.evaluate(() => (
    window.JellyfinRefreshKit.get('EpochRollbackTest').state().updatePending
  )), false);

  versionResponse = { CacheKey: 'G0', Epoch: 'process-g0-fresh' };
  await page.evaluate(() => window.JellyfinRefreshKit.get('EpochRollbackTest').checkNow());
  await page.evaluate(() => window.JellyfinRefreshKit.get('EpochRollbackTest').checkNow());
  const reused = await page.evaluate((keys) => ({
    state: window.JellyfinRefreshKit.get('EpochRollbackTest').state(),
    epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
  }), storageKeys);
  assert.equal(reused.state.updatePending, false);
  assert.equal(reused.state.authorizedEpoch, null);
  assert.ok(reused.epochs.some((tuple) => tuple[1] === 'process-g0-fresh'));
});

function browserExecutable() {
  const candidates = [
    process.env.RK_BROWSER_EXECUTABLE,
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
    '/usr/bin/google-chrome-stable',
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }

  const bundled = puppeteer.executablePath();
  if (bundled && fs.existsSync(bundled)) return bundled;
  throw new Error('No Chromium executable found; set RK_BROWSER_EXECUTABLE');
}

async function openBrowser(t) {
  const browser = await puppeteer.launch({
    executablePath: browserExecutable(),
    headless: true,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
  });
  t.after(() => browser.close());
  return browser;
}

async function startServer(t, handler) {
  const sockets = new Set();
  const server = http.createServer(handler);
  server.on('connection', (socket) => {
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
  });

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });

  t.after(() => new Promise((resolve) => {
    for (const socket of sockets) socket.destroy();
    server.close(resolve);
  }));

  const address = server.address();
  return `http://127.0.0.1:${address.port}`;
}

function serveHtml(res) {
  res.writeHead(200, {
    'Content-Type': 'text/html; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  res.end('<!doctype html><html><head></head><body></body></html>');
}

async function injectRuntime(page, source = runtime) {
  await page.evaluate((text) => {
    const script = document.createElement('script');
    script.textContent = text;
    document.head.appendChild(script);
  }, source);
}

async function injectConfiguredRuntime(page, source, attributes) {
  await page.evaluate(({ text, attrs }) => {
    const script = document.createElement('script');
    for (const [name, value] of Object.entries(attrs)) script.setAttribute(name, value);
    script.textContent = text;
    document.head.appendChild(script);
  }, { text: source, attrs: attributes });
}

function fastEpochRuntime(source = runtime) {
  const accelerated = source.replace(
    'var VERSION_CONFIRM_MS = 1500;',
    'var VERSION_CONFIRM_MS = 25;',
  );
  assert.notEqual(accelerated, source, 'test confirmation delay was accelerated');
  return accelerated;
}

async function configureMockEpochPage(page, origin, options) {
  const {
    name,
    bootVersion = 'G2',
    responses = [{ CacheKey: 'G0', Epoch: 'epoch-fresh' }],
    left = ['G0'],
    epochRecords,
    epochRaw,
    storageMode = 'normal',
    idleSeconds = 300,
    mode = 'auto',
    assetPatterns = [],
  } = options;
  await page.goto(`${origin}/#/home`);
  await page.evaluate((payload) => {
    const {
      instanceName, baseline, observations, leftVersions, records, rawEpochs,
      storageBehavior, idle, instanceMode, patterns, keys,
    } = payload;
    sessionStorage.setItem(
      keys.left,
      JSON.stringify(leftVersions.map((version) => `${instanceName}|${version}`)),
    );
    if (records !== undefined) sessionStorage.setItem(keys.epochs, JSON.stringify(records));
    if (rawEpochs !== undefined) sessionStorage.setItem(keys.epochs, rawEpochs);

    const nativeGetItem = Storage.prototype.getItem;
    const nativeSetItem = Storage.prototype.setItem;
    if (storageBehavior === 'unreadable') {
      Storage.prototype.getItem = function getItem(key) {
        if (key === keys.epochs) throw new DOMException('epoch storage blocked', 'SecurityError');
        return nativeGetItem.call(this, key);
      };
    } else if (storageBehavior === 'unwritable') {
      Storage.prototype.setItem = function setItem(key, value) {
        if (key === keys.epochs) return undefined;
        return nativeSetItem.call(this, key, value);
      };
    }

    window.__epochResponses = observations.slice();
    window.__epochLastResponse = observations[observations.length - 1];
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      const next = window.__epochResponses.length
        ? window.__epochResponses.shift() : window.__epochLastResponse;
      const body = Object.prototype.hasOwnProperty.call(next, 'plainText')
        ? next.plainText : JSON.stringify(next);
      return Promise.resolve(new Response(body, {
        status: 200,
        headers: { 'Content-Type': 'application/json; charset=utf-8' },
      }));
    };
    window.JellyfinRefreshKitConfig = {
      name: instanceName,
      mode: instanceMode,
      bootVersion: baseline,
      versionUrl: '/version',
      versionJsonField: observations[0]?.plainText === undefined ? 'CacheKey' : '',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: idle,
      assetPatterns: patterns,
    };
  }, {
    instanceName: name,
    baseline: bootVersion,
    observations: responses,
    leftVersions: left,
    records: epochRecords,
    rawEpochs: epochRaw,
    storageBehavior: storageMode,
    idle: idleSeconds,
    instanceMode: mode,
    patterns: assetPatterns,
    keys: storageKeys,
  });
}

async function waitForEpochFetches(page, count) {
  await page.waitForFunction((minimum) => window.__epochFetchCount >= minimum, {}, count);
}

function runtimeAtVersion(version) {
  const marker = "var KIT_VERSION = '2.4.6';";
  assert.equal(runtime.split(marker).length, 2, 'runtime must contain one current KIT_VERSION marker');
  return runtime.replace(marker, `var KIT_VERSION = '${version}';`);
}

test('missing, invalid, seen, plain-text, and callback epochs preserve legacy flap refusal', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const cases = [
    { name: 'EpochMissing', response: { CacheKey: 'G0' } },
    { name: 'EpochBlank', response: { CacheKey: 'G0', Epoch: '   ' } },
    { name: 'EpochHtml', response: { CacheKey: 'G0', Epoch: '  <html>error</html>' } },
    { name: 'EpochOversized', response: { CacheKey: 'G0', Epoch: 'x'.repeat(201) } },
    {
      name: 'EpochSeen',
      response: { CacheKey: 'G0', Epoch: 'seen-epoch' },
      records: [['EpochSeen', 'seen-epoch']],
    },
    { name: 'EpochPlainText', response: { plainText: 'G0' } },
  ];

  for (const scenario of cases) {
    const page = await browser.newPage();
    await configureMockEpochPage(page, origin, {
      name: scenario.name,
      responses: [scenario.response],
      epochRecords: scenario.records,
    });
    await injectRuntime(page, fastEpochRuntime());
    await waitForEpochFetches(page, 2);
    const state = await page.evaluate((name) => (
      window.JellyfinRefreshKit.get(name).state()
    ), scenario.name);
    assert.equal(state.updatePending, false, `${scenario.name} must fail closed`);
    assert.equal(state.authorizedEpoch, null, `${scenario.name} must not claim an epoch`);
    if (scenario.name !== 'EpochSeen') assert.equal(state.latestEpoch, null);
    await page.close();
  }

  const callbackPage = await browser.newPage();
  await callbackPage.goto(`${origin}/#/home`);
  await callbackPage.evaluate((keys) => {
    sessionStorage.setItem(keys.left, JSON.stringify(['EpochCallback|G0']));
    window.__callbackVersionCalls = 0;
    window.JellyfinRefreshKitConfig = {
      name: 'EpochCallback',
      mode: 'auto',
      bootVersion: 'G2',
      getVersion() {
        window.__callbackVersionCalls += 1;
        return 'G0';
      },
      pollSeconds: 3600,
      idleSeconds: 300,
    };
  }, storageKeys);
  await injectRuntime(callbackPage, fastEpochRuntime());
  await callbackPage.waitForFunction(() => window.__callbackVersionCalls >= 2);
  const callbackState = await callbackPage.evaluate(() => (
    window.JellyfinRefreshKit.get('EpochCallback').state()
  ));
  assert.equal(callbackState.updatePending, false);
  assert.equal(callbackState.latestEpoch, null);
  assert.equal(callbackState.authorizedEpoch, null);
});

test('legacy source departures leave gaps that block unsafe epoch-era rollback upgrades', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const source = fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');

  for (const kind of ['plain-text', 'callback']) {
    const page = await browser.newPage();
    await page.goto(`${origin}/${kind}-epoch-gap#/home`);
    const name = kind === 'callback' ? 'CallbackUpgradeGap' : 'PlainTextUpgradeGap';
    await page.evaluate(({ sourceKind, instanceName }) => {
      window.__reloadAttempts = 0;
      window.__sourceCalls = 0;
      const config = {
        name: instanceName,
        mode: 'auto',
        bootVersion: 'G2',
        versionUrl: '/version',
        versionEpochJsonField: 'Epoch',
        pollSeconds: 3600,
        idleSeconds: 0,
      };
      if (sourceKind === 'callback') {
        config.versionJsonField = 'CacheKey';
        config.getVersion = () => {
          window.__sourceCalls += 1;
          return 'G3';
        };
      } else {
        config.versionJsonField = '';
        window.fetch = () => {
          window.__sourceCalls += 1;
          return Promise.resolve(new Response('G3', { status: 200 }));
        };
      }
      window.JellyfinRefreshKitConfig = config;
    }, { sourceKind: kind, instanceName: name });
    await injectRuntime(page, source);
    await page.waitForFunction(() => window.__reloadAttempts === 1);
    const departed = await page.evaluate((keys) => ({
      calls: window.__sourceCalls,
      gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
      left: JSON.parse(sessionStorage.getItem(keys.left)),
      epochs: sessionStorage.getItem(keys.epochs),
    }), storageKeys);
    assert.ok(departed.calls >= 2, `${kind} target was confirmed`);
    assert.deepEqual(departed.gaps, [[name, 'G2']],
      `${kind} departure records its unknown process history`);
    assert.ok(departed.left.includes(`${name}|G2`));
    assert.equal(departed.epochs, null, `${kind} has no process epoch to record`);

    // The next document adopts the JSON epoch sidecar. The old G2 process now
    // looks syntactically fresh, but the legacy departure could not record its
    // epoch, so the permanent gap must veto the historical override.
    await page.goto(`${origin}/${kind}-epoch-upgrade#/home`);
    await page.evaluate((instanceName) => {
      window.__reloadAttempts = 0;
      window.__epochFetchCount = 0;
      window.__responses = [
        { CacheKey: 'G3', Epoch: 'upgrade-current-e3' },
        { CacheKey: 'G2', Epoch: 'old-pre-epoch-e2' },
        { CacheKey: 'G2', Epoch: 'old-pre-epoch-e2' },
      ];
      window.__lastResponse = window.__responses[window.__responses.length - 1];
      window.fetch = () => {
        window.__epochFetchCount += 1;
        const response = window.__responses.length
          ? window.__responses.shift() : window.__lastResponse;
        return Promise.resolve(new Response(JSON.stringify(response), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }));
      };
      window.JellyfinRefreshKitConfig = {
        name: instanceName,
        mode: 'auto',
        bootVersion: 'G3',
        versionUrl: '/version',
        versionJsonField: 'CacheKey',
        versionEpochJsonField: 'Epoch',
        pollSeconds: 3600,
        idleSeconds: 0,
      };
    }, name);
    await injectRuntime(page, source);
    await waitForEpochFetches(page, 1);
    await page.evaluate((instanceName) => (
      window.JellyfinRefreshKit.get(instanceName).checkNow()
    ), name);
    await page.evaluate((instanceName) => (
      window.JellyfinRefreshKit.get(instanceName).checkNow()
    ), name);
    await waitForEpochFetches(page, 3);
    const refused = await page.evaluate(({ keys, instanceName }) => ({
      reloads: window.__reloadAttempts,
      state: window.JellyfinRefreshKit.get(instanceName).state(),
      gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
      epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
    }), { keys: storageKeys, instanceName: name });
    assert.equal(refused.reloads, 0);
    assert.equal(refused.state.updatePending, false);
    assert.equal(refused.state.authorizedEpoch, null);
    assert.deepEqual(refused.gaps, [[name, 'G2']]);
    assert.deepEqual(refused.epochs, [[name, 'upgrade-current-e3']]);
    await page.close();
  }
});

test('same-generation process restarts consume epochs without reload or cache identity changes', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await configureMockEpochPage(page, origin, {
    name: 'NoChangeEpoch',
    bootVersion: 'G2',
    responses: [{ CacheKey: 'G2', Epoch: 'same-generation-a' }],
    left: [],
    assetPatterns: ['/asset/'],
  });
  await injectRuntime(page);
  await waitForEpochFetches(page, 1);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('NoChangeEpoch').state().latestEpoch === 'same-generation-a'
  ));

  await page.evaluate(() => {
    window.__epochLastResponse = { CacheKey: 'G2', Epoch: 'same-generation-b' };
  });
  await page.evaluate(() => window.JellyfinRefreshKit.get('NoChangeEpoch').checkNow());
  const observed = await page.evaluate((keys) => ({
    state: window.JellyfinRefreshKit.get('NoChangeEpoch').state(),
    epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
    asset: window.JellyfinRefreshKit.get('NoChangeEpoch').versionedUrl('/asset/plugin.js'),
  }), storageKeys);
  assert.equal(observed.state.updatePending, false);
  assert.equal(observed.state.version, 'G2');
  assert.equal(observed.state.latestEpoch, 'same-generation-b');
  assert.deepEqual(observed.epochs, [
    ['NoChangeEpoch', 'same-generation-a'],
    ['NoChangeEpoch', 'same-generation-b'],
  ]);
  assert.equal(observed.asset, '/asset/plugin.js?v=G2');
});

test('a missing baseline epoch remains a permanent coverage gap after a later concrete epoch', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await configureMockEpochPage(page, origin, {
    name: 'MissingBaselineEpochGap',
    bootVersion: 'G2',
    responses: [{ CacheKey: 'G2' }],
    left: [],
    idleSeconds: 0,
  });
  await page.evaluate(() => { window.__reloadAttempts = 0; });
  const source = fastEpochRuntime(runtime)
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  await injectRuntime(page, source);
  await waitForEpochFetches(page, 1);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('MissingBaselineEpochGap').state()
      .baselineEpochCoverageUnreliable === true
  ));

  await page.evaluate(() => {
    window.__epochLastResponse = { CacheKey: 'G2', Epoch: 'known-g2-process' };
    return window.JellyfinRefreshKit.get('MissingBaselineEpochGap').checkNow();
  });
  const covered = await page.evaluate(() => (
    window.JellyfinRefreshKit.get('MissingBaselineEpochGap').state()
  ));
  assert.equal(covered.baselineEpoch, 'known-g2-process');
  assert.equal(covered.baselineEpochCoverageUnreliable, true,
    'a later replica/process cannot erase the earlier unknown baseline process');

  await page.evaluate(() => {
    window.__epochLastResponse = { CacheKey: 'G3', Epoch: 'forward-g3-process' };
    return window.JellyfinRefreshKit.get('MissingBaselineEpochGap').checkNow();
  });
  await page.evaluate(() => (
    window.JellyfinRefreshKit.get('MissingBaselineEpochGap').checkNow()
  ));
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  assert.deepEqual(await page.evaluate((keys) => (
    JSON.parse(sessionStorage.getItem(keys.gaps))
  ), storageKeys), [['MissingBaselineEpochGap', 'G2']]);
});

test('failed baseline epoch coverage persists through rotation and handoff before departure', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/epoch-coverage-rotation#/home`);
  await page.evaluate((keys) => {
    const nativeSet = Storage.prototype.setItem;
    window.__blockEpochWrites = true;
    Storage.prototype.setItem = function setItem(key, value) {
      if (key === keys.epochs && window.__blockEpochWrites) return undefined;
      return nativeSet.call(this, key, value);
    };
    window.__epochFetchCount = 0;
    window.__reloadAttempts = 0;
    window.__response = { CacheKey: 'G2', Epoch: 'baseline-e2a' };
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify(window.__response), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    };
  }, storageKeys);
  const attrs = {
    'data-name': 'EpochCoverageRotation',
    'data-boot-version': 'G2',
    'data-version-url': '/version',
    'data-version-json-field': 'CacheKey',
    'data-version-epoch-json-field': 'Epoch',
    'data-mode': 'auto',
    'data-poll-seconds': '3600',
    'data-idle-seconds': '0',
  };
  const sourceAt = (version) => fastEpochRuntime(runtimeAtVersion(version))
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  await injectConfiguredRuntime(page, sourceAt('2.4.6'), attrs);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('EpochCoverageRotation').state().baselineEpoch === 'baseline-e2a'
  ));
  assert.equal(await page.evaluate(() => (
    window.JellyfinRefreshKit.get('EpochCoverageRotation').state()
      .baselineEpochCoverageUnreliable
  )), true, 'the initial e2 claim silently failed');
  await page.evaluate(() => {
    window.__blockEpochWrites = false;
    window.__response = { CacheKey: 'G2', Epoch: 'baseline-e2b-recorded' };
    return window.JellyfinRefreshKit.get('EpochCoverageRotation').checkNow();
  });
  assert.equal(await page.evaluate(() => (
    window.JellyfinRefreshKit.get('EpochCoverageRotation').state()
      .baselineEpochCoverageUnreliable
  )), true);

  await injectConfiguredRuntime(page, sourceAt('2.4.7'), attrs);
  await page.waitForFunction(() => window.JellyfinRefreshKit.kitVersion === '2.4.7');
  assert.equal(await page.evaluate(() => (
    window.JellyfinRefreshKit.get('EpochCoverageRotation').state()
      .baselineEpochCoverageUnreliable
  )), true, 'the failed-coverage latch survives manager handoff');

  await page.evaluate(() => {
    window.__response = { CacheKey: 'G3', Epoch: 'forward-e3' };
    return window.JellyfinRefreshKit.get('EpochCoverageRotation').checkNow();
  });
  await page.evaluate(() => window.JellyfinRefreshKit.get('EpochCoverageRotation').checkNow());
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  const departed = await page.evaluate((keys) => ({
    gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
    epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
  }), storageKeys);
  assert.deepEqual(departed.gaps, [['EpochCoverageRotation', 'G2']]);
  assert.deepEqual(departed.epochs, [['EpochCoverageRotation', 'baseline-e2b-recorded']],
    'the failed concrete e2a is never silently treated as consumed');
});

test('a preflight epoch verification failure latches a permanent coverage gap', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/epoch-preflight-gap#/home`);
  await page.evaluate((keys) => {
    const nativeGet = Storage.prototype.getItem;
    window.__blockEpochReads = false;
    Storage.prototype.getItem = function getItem(key) {
      if (key === keys.epochs && window.__blockEpochReads) {
        throw new DOMException('blocked', 'SecurityError');
      }
      return nativeGet.call(this, key);
    };
    window.__reloadAttempts = 0;
    window.__response = { CacheKey: 'G2', Epoch: 'verified-e2' };
    window.fetch = () => Promise.resolve(new Response(JSON.stringify(window.__response), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    window.JellyfinRefreshKitConfig = {
      name: 'EpochPreflightGap',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 0,
    };
  }, storageKeys);
  const source = fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  await injectRuntime(page, source);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('EpochPreflightGap').state().baselineEpoch === 'verified-e2'
  ));

  await page.evaluate(() => {
    window.__blockEpochReads = true;
    window.__response = { CacheKey: 'G3', Epoch: 'forward-e3' };
    return window.JellyfinRefreshKit.get('EpochPreflightGap').checkNow();
  });
  await page.evaluate(() => window.JellyfinRefreshKit.get('EpochPreflightGap').checkNow());
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  const result = await page.evaluate((keys) => ({
    state: window.JellyfinRefreshKit.get('EpochPreflightGap').state(),
    gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
  }), storageKeys);
  assert.equal(result.state.baselineEpochCoverageUnreliable, true);
  assert.deepEqual(result.gaps, [['EpochPreflightGap', 'G2']]);
});

test('unknown boot epoch creates a permanent gap that blocks a later old-process rollback', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  const name = 'UnknownBootEpoch';
  const makeSource = () => fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');

  await page.goto(`${origin}/unknown-boot-forward#/home`);
  await page.evaluate((instanceName) => {
    window.__reloadAttempts = 0;
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'G3', Epoch: 'forward-e3',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
    window.JellyfinRefreshKitConfig = {
      name: instanceName,
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 0,
    };
  }, name);
  await injectRuntime(page, makeSource());
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  assert.deepEqual(await page.evaluate((keys) => (
    JSON.parse(sessionStorage.getItem(keys.gaps))
  ), storageKeys), [[name, 'G2']], 'departing the stamped boot without e2 records a gap');

  await page.goto(`${origin}/unknown-boot-return#/home`);
  await page.evaluate((instanceName) => {
    window.__reloadAttempts = 0;
    window.__response = { CacheKey: 'G3', Epoch: 'forward-e3' };
    window.fetch = () => Promise.resolve(new Response(JSON.stringify(window.__response), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    window.JellyfinRefreshKitConfig = {
      name: instanceName,
      mode: 'auto',
      bootVersion: 'G3',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 0,
    };
  }, name);
  await injectRuntime(page, makeSource());
  await page.waitForFunction((instanceName) => (
    window.JellyfinRefreshKit.get(instanceName).state().baselineEpoch === 'forward-e3'
  ), {}, name);
  await page.evaluate(() => {
    window.__response = { CacheKey: 'G2', Epoch: 'old-e2' };
    return window.JellyfinRefreshKit.get('UnknownBootEpoch').checkNow();
  });
  await page.evaluate(() => window.JellyfinRefreshKit.get('UnknownBootEpoch').checkNow());
  await new Promise((resolve) => setTimeout(resolve, 75));
  const refused = await page.evaluate((keys) => ({
    reloads: window.__reloadAttempts,
    state: window.JellyfinRefreshKit.get('UnknownBootEpoch').state(),
    gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
    epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
  }), storageKeys);
  assert.equal(refused.reloads, 0);
  assert.equal(refused.state.updatePending, false);
  assert.equal(refused.state.authorizedEpoch, null);
  assert.deepEqual(refused.gaps, [[name, 'G2']]);
  assert.ok(!refused.epochs.some((tuple) => tuple[1] === 'old-e2'),
    'the old process epoch is not consumed as a fresh rollback authorization');
});

test('a shared reload tombstones an unresolved sibling and refuses all later auto candidates', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  const source = fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  const unknownAttrs = {
    'data-name': 'UnknownSibling',
    'data-version-url': '/unknown-version',
    'data-version-json-field': 'CacheKey',
    'data-version-epoch-json-field': 'Epoch',
    'data-mode': 'auto',
    'data-poll-seconds': '3600',
    'data-idle-seconds': '0',
  };
  const triggerAttrs = {
    'data-name': 'TriggerSibling',
    'data-boot-version': 'T0',
    'data-version-url': '/trigger-version',
    'data-version-json-field': 'CacheKey',
    'data-version-epoch-json-field': 'Epoch',
    'data-mode': 'auto',
    'data-poll-seconds': '3600',
    'data-idle-seconds': '0',
  };

  await page.goto(`${origin}/unresolved-sibling-departure#/home`);
  await page.evaluate(() => {
    window.__reloadAttempts = 0;
    window.__unknownFetches = 0;
    window.__triggerResponse = { CacheKey: 'T0', Epoch: 'trigger-e0' };
    window.fetch = (input) => {
      const pathName = new URL(String(input), location.href).pathname;
      if (pathName === '/unknown-version') {
        window.__unknownFetches += 1;
        return new Promise(() => {});
      }
      return Promise.resolve(new Response(JSON.stringify(window.__triggerResponse), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    };
  });
  await injectConfiguredRuntime(page, source, unknownAttrs);
  await page.waitForFunction(() => window.__unknownFetches === 1);
  await injectConfiguredRuntime(page, source, triggerAttrs);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('TriggerSibling').state().baselineEpoch === 'trigger-e0'
  ));
  await page.evaluate(() => {
    window.__triggerResponse = { CacheKey: 'T1', Epoch: 'trigger-e1' };
    return window.JellyfinRefreshKit.get('TriggerSibling').checkNow();
  });
  await page.evaluate(() => window.JellyfinRefreshKit.get('TriggerSibling').checkNow());
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  const departure = await page.evaluate((keys) => ({
    gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
    left: JSON.parse(sessionStorage.getItem(keys.left)),
  }), storageKeys);
  assert.deepEqual(departure.gaps, [['UnknownSibling', null]],
    'the unresolved sibling gets a typed instance-wide tombstone');
  assert.ok(departure.left.includes('TriggerSibling|T0'));
  assert.ok(!departure.left.some((record) => record.startsWith('UnknownSibling|')),
    'an unresolved baseline cannot manufacture an exact LEFT generation');

  await page.goto(`${origin}/unresolved-sibling-return#/home`);
  await page.evaluate(() => {
    window.__reloadAttempts = 0;
    window.__responses = [
      { CacheKey: 'G1', Epoch: 'unknown-current-e1' },
      { CacheKey: 'G0', Epoch: 'unknown-old-e0' },
      { CacheKey: 'G0', Epoch: 'unknown-old-e0' },
    ];
    window.__lastResponse = window.__responses[window.__responses.length - 1];
    window.fetch = () => {
      const response = window.__responses.length
        ? window.__responses.shift() : window.__lastResponse;
      return Promise.resolve(new Response(JSON.stringify(response), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'UnknownSibling',
      mode: 'auto',
      bootVersion: 'G1',
      versionUrl: '/unknown-version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 0,
    };
  });
  await injectRuntime(page, source);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('UnknownSibling').state().baselineEpoch === 'unknown-current-e1'
  ));
  await page.evaluate(() => window.JellyfinRefreshKit.get('UnknownSibling').checkNow());
  await page.evaluate(() => window.JellyfinRefreshKit.get('UnknownSibling').checkNow());
  await new Promise((resolve) => setTimeout(resolve, 75));
  const refused = await page.evaluate((keys) => ({
    reloads: window.__reloadAttempts,
    state: window.JellyfinRefreshKit.get('UnknownSibling').state(),
    gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
    epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
  }), storageKeys);
  assert.equal(refused.reloads, 0);
  assert.equal(refused.state.updatePending, false);
  assert.equal(refused.state.authorizedEpoch, null);
  assert.deepEqual(refused.gaps, [['UnknownSibling', null]]);
  assert.ok(refused.epochs.some((tuple) => (
    tuple[0] === 'UnknownSibling' && tuple[1] === 'unknown-current-e1'
  )));
  assert.ok(!refused.epochs.some((tuple) => (
    tuple[0] === 'UnknownSibling' && tuple[1] === 'unknown-old-e0'
  )));
});

test('an unresolved tombstone survives a failed-reload watchdog and newest-manager handoff', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/tombstone-watchdog-handoff#/home`);
  await page.evaluate(() => {
    window.__reloadAttempts = 0;
    window.__unknownCalls = 0;
    window.__unknownResponse = { CacheKey: 'U1', Epoch: 'unknown-current-e1' };
    window.__triggerResponse = { CacheKey: 'T0', Epoch: 'trigger-current-e0' };
    window.fetch = (input) => {
      const pathName = new URL(String(input), location.href).pathname;
      if (pathName === '/handoff-unknown') {
        window.__unknownCalls += 1;
        if (window.__unknownCalls === 1) return new Promise(() => {});
        return Promise.resolve(new Response(JSON.stringify(window.__unknownResponse), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }));
      }
      return Promise.resolve(new Response(JSON.stringify(window.__triggerResponse), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    };
  });
  const sourceAt = (version) => fastEpochRuntime(runtimeAtVersion(version))
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('var RETRY_MS = 1000;', 'var RETRY_MS = 25;')
    .replace(
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 3000;',
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 100;',
    )
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  const common = {
    'data-version-json-field': 'CacheKey',
    'data-version-epoch-json-field': 'Epoch',
    'data-mode': 'auto',
    'data-poll-seconds': '3600',
    'data-idle-seconds': '0',
    'data-reload-budget': '1',
  };
  const unknownAttrs = {
    ...common,
    'data-name': 'WatchdogUnknown',
    'data-version-url': '/handoff-unknown',
  };
  const triggerAttrs = {
    ...common,
    'data-name': 'WatchdogTrigger',
    'data-boot-version': 'T0',
    'data-version-url': '/handoff-trigger',
  };

  await injectConfiguredRuntime(page, sourceAt('2.4.6'), unknownAttrs);
  await page.waitForFunction(() => window.__unknownCalls === 1);
  await injectConfiguredRuntime(page, sourceAt('2.4.6'), triggerAttrs);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('WatchdogTrigger').state().baselineEpoch === 'trigger-current-e0'
  ));
  await page.evaluate(() => {
    window.__triggerResponse = { CacheKey: 'T1', Epoch: 'trigger-update-e1' };
    return window.JellyfinRefreshKit.get('WatchdogTrigger').checkNow();
  });
  await page.evaluate(() => window.JellyfinRefreshKit.get('WatchdogTrigger').checkNow());
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  assert.deepEqual(await page.evaluate((keys) => (
    JSON.parse(sessionStorage.getItem(keys.gaps))
  ), storageKeys), [['WatchdogUnknown', null]]);

  await injectConfiguredRuntime(page, sourceAt('2.4.7'), triggerAttrs);
  await page.waitForFunction(() => window.JellyfinRefreshKit.kitVersion === '2.4.7');
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.reloadsSurvived === 1 &&
    window.JellyfinRefreshKit.state().shared.reloadRevalidationPending === false &&
    window.JellyfinRefreshKit.get('WatchdogUnknown').state().baselineEpoch === 'unknown-current-e1'
  ));
  assert.deepEqual(await page.evaluate((keys) => (
    JSON.parse(sessionStorage.getItem(keys.gaps))
  ), storageKeys), [['WatchdogUnknown', null]],
  'the permanent tombstone is neither retracted by the watchdog nor copied through handoff state');

  await page.evaluate(() => {
    window.__unknownResponse = { CacheKey: 'U2', Epoch: 'unknown-candidate-e2' };
    return window.JellyfinRefreshKit.get('WatchdogUnknown').checkNow();
  });
  await page.evaluate(() => window.JellyfinRefreshKit.get('WatchdogUnknown').checkNow());
  await new Promise((resolve) => setTimeout(resolve, 75));
  const refused = await page.evaluate((keys) => ({
    manager: window.JellyfinRefreshKit.kitVersion,
    reloads: window.__reloadAttempts,
    state: window.JellyfinRefreshKit.get('WatchdogUnknown').state(),
    gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
    epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
  }), storageKeys);
  assert.equal(refused.manager, '2.4.7');
  assert.equal(refused.reloads, 1);
  assert.equal(refused.state.updatePending, false);
  assert.equal(refused.state.authorizedEpoch, null);
  assert.deepEqual(refused.gaps, [['WatchdogUnknown', null]]);
  assert.ok(!refused.epochs.some((tuple) => (
    tuple[0] === 'WatchdogUnknown' && tuple[1] === 'unknown-candidate-e2'
  )));
});

test('epoch-gap storage is strict, saturating, and claimed before any reload budget', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const full = Array.from({ length: 128 }, (_, index) => [`Gap${index}`, `G${index}`]);
  const cases = [
    { name: 'GapSilentNoop', behavior: 'noop' },
    { name: 'GapWriteThrow', behavior: 'throw' },
    { name: 'GapUnreadable', behavior: 'unreadable' },
    { name: 'GapCorrupt', behavior: 'corrupt' },
    { name: 'GapFull', behavior: 'full' },
  ];

  for (const scenario of cases) {
    const page = await browser.newPage();
    await page.goto(`${origin}/${scenario.name}#/home`);
    await page.evaluate(({ keys, item, filled }) => {
      if (item.behavior === 'corrupt') sessionStorage.setItem(keys.gaps, '{not-json');
      if (item.behavior === 'full') sessionStorage.setItem(keys.gaps, JSON.stringify(filled));
      const nativeGet = Storage.prototype.getItem;
      const nativeSet = Storage.prototype.setItem;
      if (item.behavior === 'unreadable') {
        Storage.prototype.getItem = function getItem(key) {
          if (key === keys.gaps) throw new DOMException('blocked', 'SecurityError');
          return nativeGet.call(this, key);
        };
      }
      if (item.behavior === 'noop' || item.behavior === 'throw') {
        Storage.prototype.setItem = function setItem(key, value) {
          if (key === keys.gaps) {
            if (item.behavior === 'throw') throw new DOMException('blocked', 'QuotaExceededError');
            return undefined;
          }
          return nativeSet.call(this, key, value);
        };
      }
      window.__reloadAttempts = 0;
      window.__unknownFetches = 0;
      window.__triggerResponse = { CacheKey: 'G2', Epoch: 'gap-current-e2' };
      window.fetch = (input) => {
        const pathName = new URL(String(input), location.href).pathname;
        if (pathName === '/gap-unknown') {
          window.__unknownFetches += 1;
          return new Promise(() => {});
        }
        return Promise.resolve(new Response(JSON.stringify(window.__triggerResponse), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }));
      };
    }, { keys: storageKeys, item: scenario, filled: full });
    const source = fastEpochRuntime()
      .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
      .replace('location.reload();', 'window.__reloadAttempts += 1;');
    await injectConfiguredRuntime(page, source, {
      'data-name': `${scenario.name}Unknown`,
      'data-version-url': '/gap-unknown',
      'data-version-json-field': 'CacheKey',
      'data-version-epoch-json-field': 'Epoch',
      'data-mode': 'auto',
      'data-poll-seconds': '3600',
      'data-idle-seconds': '0',
    });
    await page.waitForFunction(() => window.__unknownFetches === 1);
    await injectConfiguredRuntime(page, source, {
      'data-name': scenario.name,
      'data-boot-version': 'G2',
      'data-version-url': '/gap-trigger',
      'data-version-json-field': 'CacheKey',
      'data-version-epoch-json-field': 'Epoch',
      'data-mode': 'auto',
      'data-poll-seconds': '3600',
      'data-idle-seconds': '0',
      'data-reload-budget': '1',
    });
    await page.waitForFunction((instanceName) => (
      window.JellyfinRefreshKit.get(instanceName).state().baselineEpoch === 'gap-current-e2'
    ), {}, scenario.name);
    await page.evaluate((instanceName) => {
      window.__triggerResponse = { CacheKey: 'G3', Epoch: 'gap-forward-e3' };
      return window.JellyfinRefreshKit.get(instanceName).checkNow();
    }, scenario.name);
    await page.evaluate((instanceName) => (
      window.JellyfinRefreshKit.get(instanceName).checkNow()
    ), scenario.name);
    await new Promise((resolve) => setTimeout(resolve, 75));
    const outcome = await page.evaluate(({ keys, instanceName }) => ({
      reloads: window.__reloadAttempts,
      state: window.JellyfinRefreshKit.get(instanceName).state(),
      sessionBudget: sessionStorage.getItem(keys.budget),
      localBudget: localStorage.getItem(keys.budget),
    }), { keys: storageKeys, instanceName: scenario.name });
    assert.equal(outcome.reloads, 0);
    const reachedPreflight = ['noop', 'throw', 'full'].includes(scenario.behavior);
    assert.equal(outcome.state.updatePending, reachedPreflight);
    if (reachedPreflight) assert.equal(outcome.state.lastBlockReason, 'epoch_history');
    assert.equal(outcome.state.authorizedEpoch, null);
    assert.equal(outcome.sessionBudget, null);
    assert.equal(outcome.localBudget, null);
    if (scenario.behavior === 'full') {
      assert.deepEqual(await page.evaluate((keys) => (
        JSON.parse(sessionStorage.getItem(keys.gaps))
      ), storageKeys), full, 'gap saturation never evicts older incomplete history');
    }
    await page.close();
  }
});

test('two gap preflight claims fail atomically at one remaining slot without spending budget', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  const existing = Array.from({ length: 127 }, (_, index) => [
    `ExistingGap${index}`,
    `G${index}`,
  ]);
  await page.goto(`${origin}/atomic-gap-capacity#/home`);
  await page.evaluate(({ keys, gaps }) => {
    sessionStorage.setItem(keys.gaps, JSON.stringify(gaps));
    window.__reloadAttempts = 0;
    window.__responses = {
      '/atomic-gap-sibling': { CacheKey: 'S0' },
      '/atomic-gap-trigger': { CacheKey: 'T0' },
    };
    window.fetch = (input) => {
      const pathName = new URL(String(input), location.href).pathname;
      return Promise.resolve(new Response(JSON.stringify(window.__responses[pathName]), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    };
  }, { keys: storageKeys, gaps: existing });
  const source = fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  const common = {
    'data-version-json-field': 'CacheKey',
    'data-version-epoch-json-field': 'Epoch',
    'data-mode': 'auto',
    'data-poll-seconds': '3600',
    'data-idle-seconds': '0',
    'data-reload-budget': '1',
  };
  await injectConfiguredRuntime(page, source, {
    ...common,
    'data-name': 'AtomicGapSibling',
    'data-boot-version': 'S0',
    'data-version-url': '/atomic-gap-sibling',
  });
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('AtomicGapSibling').state().latestVersion === 'S0'
  ));
  await injectConfiguredRuntime(page, source, {
    ...common,
    'data-name': 'AtomicGapTrigger',
    'data-boot-version': 'T0',
    'data-version-url': '/atomic-gap-trigger',
  });
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('AtomicGapTrigger').state().latestVersion === 'T0'
  ));
  await page.evaluate(() => {
    window.__responses['/atomic-gap-trigger'] = { CacheKey: 'T1' };
    return window.JellyfinRefreshKit.get('AtomicGapTrigger').checkNow();
  });
  await page.evaluate(() => window.JellyfinRefreshKit.get('AtomicGapTrigger').checkNow());
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('AtomicGapTrigger').state().lastBlockReason === 'epoch_history'
  ));

  const outcome = await page.evaluate((keys) => ({
    reloads: window.__reloadAttempts,
    pending: window.JellyfinRefreshKit.get('AtomicGapTrigger').state().updatePending,
    gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
    left: sessionStorage.getItem(keys.left),
    sessionBudget: sessionStorage.getItem(keys.budget),
    localBudget: localStorage.getItem(keys.budget),
  }), storageKeys);
  assert.equal(outcome.reloads, 0);
  assert.equal(outcome.pending, true);
  assert.deepEqual(outcome.gaps, existing,
    'neither of the two new gaps is partially written when only one slot remains');
  assert.equal(outcome.left, null, 'LEFT preflight is not reached');
  assert.equal(outcome.sessionBudget, null);
  assert.equal(outcome.localBudget, null);
});

test('exact epoch evidence accumulates across a finite round robin while volatile epochs never confirm', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);

  const roundRobin = await browser.newPage();
  await configureMockEpochPage(roundRobin, origin, {
    name: 'EpochRoundRobin',
    responses: [
      { CacheKey: 'G0', Epoch: 'node-a' },
      { CacheKey: 'G0', Epoch: 'node-b' },
      { CacheKey: 'G0', Epoch: 'node-a' },
    ],
  });
  await roundRobin.evaluate(() => {
    window.__epochAnnouncements = 0;
    window.JellyfinRefreshKitConfig.onUpdateAvailable = () => {
      window.__epochAnnouncements += 1;
    };
  });
  await injectRuntime(roundRobin, fastEpochRuntime());
  await waitForEpochFetches(roundRobin, 2);
  assert.equal(await roundRobin.evaluate(() => (
    window.JellyfinRefreshKit.get('EpochRoundRobin').state().updatePending
  )), false, 'A then B is not an exact pair');
  await roundRobin.evaluate(() => window.JellyfinRefreshKit.get('EpochRoundRobin').checkNow());
  const converged = await roundRobin.evaluate((keys) => ({
    state: window.JellyfinRefreshKit.get('EpochRoundRobin').state(),
    epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
  }), storageKeys);
  assert.equal(converged.state.updatePending, true, 'A/B/A confirms tuple A nonconsecutively');
  assert.equal(converged.state.authorizedEpoch, 'node-a');
  assert.deepEqual(converged.state.candidateEpochEvidence, [
    { epoch: 'node-a', count: 2 },
    { epoch: 'node-b', count: 1 },
  ]);
  assert.deepEqual(converged.epochs, [['EpochRoundRobin', 'node-a']]);

  // A,B,A proves the historical generation through A. Further B,A rotation
  // while the reload is safety-blocked must keep that proof and must not spend
  // B merely because a load balancer selected another process.
  await roundRobin.evaluate(() => {
    window.__epochResponses.push(
      { CacheKey: 'G0', Epoch: 'node-b' },
      { CacheKey: 'G0', Epoch: 'node-a' },
    );
  });
  await roundRobin.evaluate(() => window.JellyfinRefreshKit.get('EpochRoundRobin').checkNow());
  await roundRobin.evaluate(() => window.JellyfinRefreshKit.get('EpochRoundRobin').checkNow());
  const rotated = await roundRobin.evaluate((keys) => ({
    state: window.JellyfinRefreshKit.get('EpochRoundRobin').state(),
    epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
    announcements: window.__epochAnnouncements,
  }), storageKeys);
  assert.equal(rotated.state.updatePending, true);
  assert.equal(rotated.state.authorizedEpoch, 'node-a');
  assert.deepEqual(rotated.epochs, [['EpochRoundRobin', 'node-a']]);
  assert.equal(rotated.announcements, 1, 'epoch rotation is not a second release announcement');

  const volatile = await browser.newPage();
  await volatile.goto(`${origin}/#/home`);
  await volatile.evaluate((keys) => {
    sessionStorage.setItem(keys.left, JSON.stringify(['EpochVolatile|G0']));
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'G0',
        Epoch: `volatile-${window.__epochFetchCount}`,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'EpochVolatile',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
    };
  }, storageKeys);
  await injectRuntime(volatile, fastEpochRuntime());
  await waitForEpochFetches(volatile, 2);
  for (let i = 0; i < 30; i += 1) {
    await volatile.evaluate(() => window.JellyfinRefreshKit.get('EpochVolatile').checkNow());
  }
  await new Promise((resolve) => setTimeout(resolve, 50));
  const churned = await volatile.evaluate((keys) => ({
    state: window.JellyfinRefreshKit.get('EpochVolatile').state(),
    epochs: sessionStorage.getItem(keys.epochs),
  }), storageKeys);
  assert.equal(churned.state.updatePending, false);
  assert.equal(churned.state.authorizedEpoch, null);
  assert.equal(churned.state.candidateEpochEvidence.length, 24);
  assert.equal(churned.epochs, null);
});

test('corrupt, unreadable, unwritable, and saturated epoch storage fail closed', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const full = Array.from({ length: 48 }, (_, index) => [
    `FilledInstance${index}`,
    `filled-epoch-${index}`,
  ]);
  const cases = [
    { name: 'EpochCorruptStorage', epochRaw: '{not-json' },
    { name: 'EpochUnreadableStorage', storageMode: 'unreadable' },
    { name: 'EpochUnwritableStorage', storageMode: 'unwritable' },
    { name: 'EpochFullStorage', epochRecords: full },
  ];

  for (const scenario of cases) {
    const page = await browser.newPage();
    await configureMockEpochPage(page, origin, {
      ...scenario,
      responses: [{ CacheKey: 'G0', Epoch: 'must-not-authorize' }],
    });
    await injectRuntime(page, fastEpochRuntime());
    await waitForEpochFetches(page, 2);
    const state = await page.evaluate((name) => (
      window.JellyfinRefreshKit.get(name).state()
    ), scenario.name);
    assert.equal(state.updatePending, false, `${scenario.name} must refuse rollback`);
    assert.equal(state.authorizedEpoch, null, `${scenario.name} must not retain authorization`);
    if (scenario.name === 'EpochFullStorage') {
      assert.equal(await page.evaluate((keys) => (
        JSON.parse(sessionStorage.getItem(keys.epochs)).length
      ), storageKeys), 48, 'saturation must never FIFO-evict an older epoch');
    }
    await page.close();
  }
});

test('claimed rollback authorization survives thrown and ignored reload attempts', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);

  for (const behavior of ['throw', 'ignore']) {
    const name = behavior === 'throw' ? 'EpochReloadThrow' : 'EpochReloadIgnored';
    const page = await browser.newPage();
    await configureMockEpochPage(page, origin, {
      name,
      responses: [{ CacheKey: 'G0', Epoch: `${behavior}-epoch` }],
      idleSeconds: 0,
    });
    let source = fastEpochRuntime()
      .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
      .replace(
        'var RELOAD_SURVIVAL_WATCHDOG_MS = 3000;',
        'var RELOAD_SURVIVAL_WATCHDOG_MS = 75;',
      );
    const reloadReplacement = behavior === 'throw'
      ? 'window.__reloadAttempts = (window.__reloadAttempts || 0) + 1; throw new Error("blocked reload");'
      : 'window.__reloadAttempts = (window.__reloadAttempts || 0) + 1;';
    source = source.replace('location.reload();', reloadReplacement);
    assert.match(source, /__reloadAttempts/, 'test runtime intercepted reload');
    await injectRuntime(page, source);
    await page.waitForFunction(() => (window.__reloadAttempts || 0) === 1);
    await page.waitForFunction(() => (
      window.JellyfinRefreshKit.state().shared.reloadsSurvived === 1
    ));

    const recovered = await page.evaluate(({ instanceName, keys }) => ({
      state: window.JellyfinRefreshKit.get(instanceName).state(),
      epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
      reloadAttempts: window.__reloadAttempts,
    }), { instanceName: name, keys: storageKeys });
    assert.equal(recovered.reloadAttempts, 1);
    assert.equal(recovered.state.updatePending, true);
    assert.equal(recovered.state.authorizedEpoch, `${behavior}-epoch`);
    assert.ok(recovered.epochs.some((tuple) => (
      tuple[0] === name && tuple[1] === `${behavior}-epoch`
    )));
    await page.close();
  }
});

test('newest-wins handoffs preserve candidate evidence and claimed epoch authorization', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/#/home`);
  await page.evaluate((keys) => {
    sessionStorage.setItem(keys.left, JSON.stringify(['EpochHandoff|G0']));
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'G0', Epoch: 'handoff-epoch',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
  }, storageKeys);
  const attributes = {
    'data-name': 'EpochHandoff',
    'data-version-url': '/version',
    'data-version-json-field': 'CacheKey',
    'data-version-epoch-json-field': 'Epoch',
    'data-boot-version': 'G2',
    'data-mode': 'auto',
    'data-poll-seconds': '3600',
    'data-idle-seconds': '300',
  };

  await injectConfiguredRuntime(page, runtime, attributes);
  await waitForEpochFetches(page, 1);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('EpochHandoff').state().candidateEpochEvidence.length === 1
  ));
  await page.evaluate(() => {
    window.__retainedEpochHandle = window.JellyfinRefreshKit.get('EpochHandoff');
  });

  await injectConfiguredRuntime(page, runtimeAtVersion('2.4.7'), attributes);
  await page.waitForFunction(() => window.JellyfinRefreshKit.kitVersion === '2.4.7');
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('EpochHandoff').state().updatePending === true
  ));
  assert.equal(await page.evaluate(() => window.__epochFetchCount), 2,
    'handoff must re-arm the already-earned confirmation without checkNow()');
  let state = await page.evaluate(() => window.__retainedEpochHandle.state());
  assert.equal(state.updatePending, true);
  assert.equal(state.authorizedEpoch, 'handoff-epoch');
  assert.deepEqual(state.candidateEpochEvidence, [{ epoch: 'handoff-epoch', count: 2 }]);

  await injectConfiguredRuntime(page, runtimeAtVersion('2.4.8'), attributes);
  await page.waitForFunction(() => window.JellyfinRefreshKit.kitVersion === '2.4.8');
  state = await page.evaluate(() => window.__retainedEpochHandle.state());
  assert.equal(state.kitVersion, '2.4.8');
  assert.equal(state.restoredByHandoff, true);
  assert.equal(state.updatePending, true);
  assert.equal(state.authorizedEpoch, 'handoff-epoch');
  assert.deepEqual(await page.evaluate((keys) => (
    JSON.parse(sessionStorage.getItem(keys.epochs))
  ), storageKeys), [['EpochHandoff', 'handoff-epoch']]);
});

test('handoff replaces one held in-flight confirmation without waiting for pollSeconds', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/#/home`);
  await page.evaluate((keys) => {
    sessionStorage.setItem(keys.left, JSON.stringify(['EpochHeldHandoff|G0']));
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      if (window.__epochFetchCount === 2) {
        return new Promise((resolve) => {
          window.__releaseHeldConfirmation = () => resolve(new Response(JSON.stringify({
            CacheKey: 'G0', Epoch: 'held-handoff-epoch',
          }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        });
      }
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'G0', Epoch: 'held-handoff-epoch',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
  }, storageKeys);
  const attributes = {
    'data-name': 'EpochHeldHandoff',
    'data-version-url': '/version',
    'data-version-json-field': 'CacheKey',
    'data-version-epoch-json-field': 'Epoch',
    'data-boot-version': 'G2',
    'data-mode': 'auto',
    'data-poll-seconds': '3600',
    'data-idle-seconds': '300',
  };

  await injectConfiguredRuntime(page, fastEpochRuntime(), attributes);
  await waitForEpochFetches(page, 2);
  assert.equal(await page.evaluate(() => window.__epochFetchCount), 2,
    'the earned confirmation is held before handoff');
  assert.deepEqual(await page.evaluate(() => (
    window.JellyfinRefreshKit.get('EpochHeldHandoff').state().candidateEpochEvidence
  )), [{ epoch: 'held-handoff-epoch', count: 1 }]);

  await injectConfiguredRuntime(page, fastEpochRuntime(runtimeAtVersion('2.4.7')), attributes);
  await page.waitForFunction(() => window.JellyfinRefreshKit.kitVersion === '2.4.7');
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('EpochHeldHandoff').state().updatePending === true
  ));
  assert.equal(await page.evaluate(() => window.__epochFetchCount), 3,
    'the new manager automatically issues exactly one replacement observation');

  await page.evaluate(() => window.__releaseHeldConfirmation());
  await new Promise((resolve) => setTimeout(resolve, 75));
  const final = await page.evaluate((keys) => ({
    calls: window.__epochFetchCount,
    state: window.JellyfinRefreshKit.get('EpochHeldHandoff').state(),
    epochs: JSON.parse(sessionStorage.getItem(keys.epochs)),
  }), storageKeys);
  assert.equal(final.calls, 3, 'the retired response cannot trigger another observation');
  assert.equal(final.state.updatePending, true);
  assert.equal(final.state.authorizedEpoch, 'held-handoff-epoch');
  assert.deepEqual(final.epochs, [['EpochHeldHandoff', 'held-handoff-epoch']]);
});

test('fully covered fresh epochs support G0-G1-G2-G0-G2-G0 while consumed nodes remain one-use', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  const name = 'EpochLifecycle';
  let stageNumber = 0;

  async function loadStage({ boot, target, epoch, baselineEpoch = null, left, pending }) {
    stageNumber += 1;
    await page.goto(`${origin}/stage-${stageNumber}#/home`);
    await page.evaluate((payload) => {
      sessionStorage.setItem(
        payload.keys.left,
        JSON.stringify(payload.left.map((version) => `${payload.name}|${version}`)),
      );
      window.__epochFetchCount = 0;
      window.__epochResponses = payload.baselineEpoch && payload.boot !== payload.target
        ? [
          { CacheKey: payload.boot, Epoch: payload.baselineEpoch },
          { CacheKey: payload.target, Epoch: payload.epoch },
          { CacheKey: payload.target, Epoch: payload.epoch },
        ]
        : [{ CacheKey: payload.target, Epoch: payload.epoch }];
      window.__epochLastResponse = window.__epochResponses[window.__epochResponses.length - 1];
      window.fetch = () => {
        window.__epochFetchCount += 1;
        const response = window.__epochResponses.length
          ? window.__epochResponses.shift() : window.__epochLastResponse;
        return Promise.resolve(new Response(JSON.stringify(response), {
          status: 200, headers: { 'Content-Type': 'application/json' },
        }));
      };
      window.JellyfinRefreshKitConfig = {
        name: payload.name,
        mode: 'auto',
        bootVersion: payload.boot,
        versionUrl: '/version',
        versionJsonField: 'CacheKey',
        versionEpochJsonField: 'Epoch',
        pollSeconds: 3600,
        idleSeconds: 300,
      };
    }, { keys: storageKeys, name, boot, target, epoch, baselineEpoch, left });
    await injectRuntime(page, fastEpochRuntime());
    if (baselineEpoch && target !== boot) {
      await waitForEpochFetches(page, 1);
      await page.evaluate((instanceName) => (
        window.JellyfinRefreshKit.get(instanceName).checkNow()
      ), name);
      await page.evaluate((instanceName) => (
        window.JellyfinRefreshKit.get(instanceName).checkNow()
      ), name);
      await waitForEpochFetches(page, 3);
    } else {
      await waitForEpochFetches(page, target === boot ? 1 : 2);
    }
    await page.waitForFunction(({ instanceName, expectedEpoch }) => (
      window.JellyfinRefreshKit.get(instanceName).state().latestEpoch === expectedEpoch
    ), {}, { instanceName: name, expectedEpoch: epoch });
    const state = await page.evaluate((instanceName) => (
      window.JellyfinRefreshKit.get(instanceName).state()
    ), name);
    assert.equal(state.updatePending, pending, `${boot} -> ${target} at ${epoch}`);
    return state;
  }

  await loadStage({ boot: 'G0', target: 'G0', epoch: 'g0-original', left: [], pending: false });
  await loadStage({ boot: 'G1', target: 'G1', epoch: 'g1-original', left: ['G0'], pending: false });
  await loadStage({
    boot: 'G2', target: 'G2', epoch: 'g2-original', left: ['G0', 'G1'], pending: false,
  });
  let state = await loadStage({
    boot: 'G2', target: 'G0', baselineEpoch: 'g2-original', epoch: 'g0-rollback-1',
    left: ['G0', 'G1'], pending: true,
  });
  assert.equal(state.authorizedEpoch, 'g0-rollback-1');
  await loadStage({
    boot: 'G0', target: 'G0', epoch: 'g0-rollback-1', left: ['G0', 'G1', 'G2'], pending: false,
  });
  state = await loadStage({
    boot: 'G0', target: 'G2', baselineEpoch: 'g0-rollback-1', epoch: 'g2-return',
    left: ['G0', 'G1', 'G2'], pending: true,
  });
  assert.equal(state.authorizedEpoch, 'g2-return');
  await loadStage({
    boot: 'G2', target: 'G2', epoch: 'g2-return', left: ['G0', 'G1', 'G2'], pending: false,
  });
  state = await loadStage({
    boot: 'G2', target: 'G0', baselineEpoch: 'g2-return', epoch: 'g0-rollback-2',
    left: ['G0', 'G1', 'G2'], pending: true,
  });
  assert.equal(state.authorizedEpoch, 'g0-rollback-2');

  // A finite node identity cannot loop: a process epoch consumed on the first
  // rollback stays consumed even after later fresh incarnations were accepted.
  await loadStage({
    boot: 'G2', target: 'G2', epoch: 'g2-return', left: ['G0', 'G1', 'G2'], pending: false,
  });
  state = await loadStage({
    boot: 'G2', target: 'G0', baselineEpoch: 'g2-return', epoch: 'g0-rollback-1',
    left: ['G0', 'G1', 'G2'], pending: false,
  });
  assert.equal(state.authorizedEpoch, null);
  const epochs = await page.evaluate((keys) => JSON.parse(sessionStorage.getItem(keys.epochs)), storageKeys);
  assert.deepEqual(epochs, [
    [name, 'g0-original'],
    [name, 'g1-original'],
    [name, 'g2-original'],
    [name, 'g0-rollback-1'],
    [name, 'g2-return'],
    [name, 'g0-rollback-2'],
  ]);
});

test('boot-seed recovery retracts only on exact reconvergence and permanent mismatches stay bounded', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  const exact = JSON.stringify(['boot-seed', 'BootMarker', 'G2']);
  const otherBoot = JSON.stringify(['boot-seed', 'BootMarker', 'G1']);
  const otherInstance = JSON.stringify(['boot-seed', 'OtherInstance', 'G2']);

  await page.goto(`${origin}/reconverged#/home`);
  await page.evaluate(({ keys, markers }) => {
    sessionStorage.setItem(keys.recovery, JSON.stringify(markers));
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'G2', Epoch: 'reconverged-process',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'BootMarker',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
    };
  }, { keys: storageKeys, markers: [exact, otherBoot, otherInstance] });
  await injectRuntime(page);
  await waitForEpochFetches(page, 1);
  assert.deepEqual(await page.evaluate((keys) => (
    JSON.parse(sessionStorage.getItem(keys.recovery))
  ), storageKeys), [otherBoot, otherInstance], 'only the exact instance/boot marker is released');

  await page.goto(`${origin}/fresh-rollback#/home`);
  await page.evaluate((keys) => {
    sessionStorage.setItem(keys.left, JSON.stringify(['BootMarker|G0']));
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'G0', Epoch: 'fresh-after-reconvergence',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'BootMarker',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
    };
  }, storageKeys);
  await injectRuntime(page, fastEpochRuntime());
  await waitForEpochFetches(page, 2);
  assert.equal(await page.evaluate(() => (
    window.JellyfinRefreshKit.get('BootMarker').state().authorizedEpoch
  )), 'fresh-after-reconvergence');

  const permanent = JSON.stringify(['boot-seed', 'PermanentMismatch', 'StampedCache']);
  for (let attempt = 1; attempt <= 2; attempt += 1) {
    await page.goto(`${origin}/permanent-${attempt}#/home`);
    await page.evaluate(() => {
      window.__epochFetchCount = 0;
      window.fetch = () => {
        window.__epochFetchCount += 1;
        return Promise.resolve(new Response(JSON.stringify({
          CacheKey: 'BareVersion', Epoch: 'permanent-process',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      };
      window.JellyfinRefreshKitConfig = {
        name: 'PermanentMismatch',
        mode: 'auto',
        bootVersion: 'StampedCache',
        versionUrl: '/version',
        versionJsonField: 'CacheKey',
        versionEpochJsonField: 'Epoch',
        pollSeconds: 3600,
        idleSeconds: 300,
      };
    });
    await injectRuntime(page, fastEpochRuntime());
    await waitForEpochFetches(page, attempt === 1 ? 2 : 1);
    const bounded = await page.evaluate(({ keys, marker }) => ({
      calls: window.__epochFetchCount,
      pending: window.JellyfinRefreshKit.get('PermanentMismatch').state().updatePending,
      markerPresent: JSON.parse(sessionStorage.getItem(keys.recovery)).includes(marker),
    }), { keys: storageKeys, marker: permanent });
    assert.deepEqual(bounded, attempt === 1
      ? { calls: 2, pending: true, markerPresent: true }
      : { calls: 1, pending: false, markerPresent: true });
  }
});

test('boot recovery storage is strict, saturating, and never spent for a non-reload decision', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const full = Array.from({ length: 24 }, (_, index) => (
    JSON.stringify(['boot-seed', `Filled${index}`, `G${index}`])
  ));
  const cases = [
    { name: 'RecoverySilentNoop', behavior: 'noop', fetches: 2 },
    { name: 'RecoveryWriteThrow', behavior: 'throw', fetches: 2 },
    { name: 'RecoveryUnreadable', behavior: 'unreadable', fetches: 1 },
    { name: 'RecoveryCorrupt', behavior: 'corrupt', fetches: 1 },
    { name: 'RecoveryFull', behavior: 'full', fetches: 2 },
  ];

  for (const scenario of cases) {
    const page = await browser.newPage();
    await page.goto(`${origin}/${scenario.name}#/home`);
    await page.evaluate(({ keys, item, filled }) => {
      if (item.behavior === 'corrupt') sessionStorage.setItem(keys.recovery, '{not-json');
      if (item.behavior === 'full') sessionStorage.setItem(keys.recovery, JSON.stringify(filled));
      const nativeGet = Storage.prototype.getItem;
      const nativeSet = Storage.prototype.setItem;
      if (item.behavior === 'unreadable') {
        Storage.prototype.getItem = function getItem(key) {
          if (key === keys.recovery) throw new DOMException('blocked', 'SecurityError');
          return nativeGet.call(this, key);
        };
      }
      if (item.behavior === 'noop' || item.behavior === 'throw') {
        Storage.prototype.setItem = function setItem(key, value) {
          if (key === keys.recovery) {
            if (item.behavior === 'throw') throw new DOMException('blocked', 'QuotaExceededError');
            return undefined;
          }
          return nativeSet.call(this, key, value);
        };
      }
      window.__epochFetchCount = 0;
      window.__reloadAttempts = 0;
      window.fetch = () => {
        window.__epochFetchCount += 1;
        return Promise.resolve(new Response(JSON.stringify({
          CacheKey: 'EndpointGeneration', Epoch: 'endpoint-process',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      };
      window.JellyfinRefreshKitConfig = {
        name: item.name,
        mode: 'auto',
        bootVersion: 'StampedGeneration',
        versionUrl: '/version',
        versionJsonField: 'CacheKey',
        versionEpochJsonField: 'Epoch',
        pollSeconds: 3600,
        idleSeconds: 0,
      };
    }, { keys: storageKeys, item: scenario, filled: full });
    const source = fastEpochRuntime()
      .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
      .replace('location.reload();', 'window.__reloadAttempts += 1;');
    await injectRuntime(page, source);
    await waitForEpochFetches(page, scenario.fetches);
    await new Promise((resolve) => setTimeout(resolve, 75));
    const outcome = await page.evaluate((name) => ({
      state: window.JellyfinRefreshKit.get(name).state(),
      reloads: window.__reloadAttempts,
    }), scenario.name);
    assert.equal(outcome.state.updatePending, false, `${scenario.name} fails closed`);
    assert.equal(outcome.reloads, 0, `${scenario.name} must not attempt navigation`);
    if (scenario.behavior === 'full') {
      assert.deepEqual(await page.evaluate((keys) => (
        JSON.parse(sessionStorage.getItem(keys.recovery))
      ), storageKeys), full, 'full recovery history never evicts an older marker');
    }
    await page.close();
  }

  for (const decision of ['notify', 'flap']) {
    const page = await browser.newPage();
    await page.goto(`${origin}/${decision}#/home`);
    await page.evaluate(({ keys, kind }) => {
      if (kind === 'flap') {
        sessionStorage.setItem(keys.left, JSON.stringify(['RecoveryDecision|Historical']));
      }
      window.__epochFetchCount = 0;
      window.fetch = () => {
        window.__epochFetchCount += 1;
        return Promise.resolve(new Response(JSON.stringify({
          CacheKey: kind === 'flap' ? 'Historical' : 'NotifyTarget',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      };
      window.JellyfinRefreshKitConfig = {
        name: 'RecoveryDecision',
        mode: kind === 'notify' ? 'notify' : 'auto',
        bootVersion: 'StampedGeneration',
        versionUrl: '/version',
        versionJsonField: 'CacheKey',
        versionEpochJsonField: 'Epoch',
        pollSeconds: 3600,
        idleSeconds: 300,
      };
    }, { keys: storageKeys, kind: decision });
    await injectRuntime(page, fastEpochRuntime());
    await waitForEpochFetches(page, 2);
    assert.equal(await page.evaluate((keys) => sessionStorage.getItem(keys.recovery), storageKeys), null,
      `${decision} must not spend a recovery reload it will not perform`);
    await page.close();
  }
});

test('recovery saturation preserves all older instances and bounds the twenty-fifth mismatch', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  const source = fastEpochRuntime();

  for (let index = 0; index < 25; index += 1) {
    const name = `PermanentMismatch${index}`;
    await page.goto(`${origin}/mismatch-${index}#/home`);
    await page.evaluate((instanceName) => {
      window.__epochFetchCount = 0;
      window.fetch = () => {
        window.__epochFetchCount += 1;
        return Promise.resolve(new Response(JSON.stringify({
          CacheKey: 'EndpointGeneration', Epoch: 'stable-process',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      };
      window.JellyfinRefreshKitConfig = {
        name: instanceName,
        mode: 'auto',
        bootVersion: 'StampedGeneration',
        versionUrl: '/version',
        versionJsonField: 'CacheKey',
        versionEpochJsonField: 'Epoch',
        pollSeconds: 3600,
        idleSeconds: 300,
      };
    }, name);
    await injectRuntime(page, source);
    await waitForEpochFetches(page, 2);
    assert.equal(await page.evaluate((instanceName) => (
      window.JellyfinRefreshKit.get(instanceName).state().updatePending
    ), name), index < 24, `mismatch ${index} obeys the saturating recovery cap`);
  }

  const markers = await page.evaluate((keys) => (
    JSON.parse(sessionStorage.getItem(keys.recovery))
  ), storageKeys);
  assert.equal(markers.length, 24);
  assert.ok(markers.includes(JSON.stringify([
    'boot-seed', 'PermanentMismatch0', 'StampedGeneration',
  ])));
  assert.ok(!markers.includes(JSON.stringify([
    'boot-seed', 'PermanentMismatch24', 'StampedGeneration',
  ])));

  await page.goto(`${origin}/mismatch-revisit#/home`);
  await page.evaluate(() => {
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'EndpointGeneration', Epoch: 'stable-process',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'PermanentMismatch0',
      mode: 'auto',
      bootVersion: 'StampedGeneration',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
    };
  });
  await injectRuntime(page, source);
  await waitForEpochFetches(page, 1);
  assert.deepEqual(await page.evaluate(() => ({
    calls: window.__epochFetchCount,
    pending: window.JellyfinRefreshKit.get('PermanentMismatch0').state().updatePending,
  })), { calls: 1, pending: false }, 'the oldest marker was retained and rejects the revisit');
});

test('typed boot-recovery markers cannot collide through opaque delimiters', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  const exact = JSON.stringify(['boot-seed', 'a|b', 'c']);
  const collisionUnderOldEncoding = JSON.stringify(['boot-seed', 'a', 'b|c']);
  await page.goto(`${origin}/marker-delimiters#/home`);
  await page.evaluate(({ keys, markers }) => {
    sessionStorage.setItem(keys.recovery, JSON.stringify(markers));
    window.fetch = () => Promise.resolve(new Response(JSON.stringify({
      CacheKey: 'c', Epoch: 'delimiter-process',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    window.JellyfinRefreshKitConfig = {
      name: 'a|b',
      mode: 'auto',
      bootVersion: 'c',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
    };
  }, { keys: storageKeys, markers: [exact, collisionUnderOldEncoding] });
  await injectRuntime(page);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('a|b').state().latestEpoch === 'delimiter-process'
  ));
  assert.deepEqual(await page.evaluate((keys) => (
    JSON.parse(sessionStorage.getItem(keys.recovery))
  ), storageKeys), [collisionUnderOldEncoding], 'only the exact typed marker is released');
});

test('released legacy boot markers migrate in place without earning recovery or capacity', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  const legacy = 'boot-seed|LegacyUpgrade|G2';
  const tombstone = JSON.stringify(['legacy-recovery', legacy]);
  const records = [legacy].concat(Array.from({ length: 23 }, (_, index) => (
    JSON.stringify(['boot-seed', `Other${index}`, `G${index}`])
  )));
  await page.goto(`${origin}/legacy-marker-upgrade#/home`);
  await page.evaluate(({ keys, history }) => {
    sessionStorage.setItem(keys.recovery, JSON.stringify(history));
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'G0', Epoch: 'legacy-upgrade-process',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'LegacyUpgrade',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
    };
  }, { keys: storageKeys, history: records });
  await injectRuntime(page, fastEpochRuntime());
  await waitForEpochFetches(page, 1);
  const upgraded = await page.evaluate((keys) => ({
    calls: window.__epochFetchCount,
    pending: window.JellyfinRefreshKit.get('LegacyUpgrade').state().updatePending,
    records: JSON.parse(sessionStorage.getItem(keys.recovery)),
  }), storageKeys);
  assert.equal(upgraded.calls, 1, 'the legacy marker rejects the boot seed immediately');
  assert.equal(upgraded.pending, false);
  assert.equal(upgraded.records.length, 24, 'migration does not consume another bounded slot');
  assert.equal(upgraded.records[0], tombstone);
  assert.ok(!upgraded.records.includes(legacy), 'legacy capacity debris is removed');

  const collisionPage = await browser.newPage();
  const ambiguousLegacy = 'boot-seed|a|b|c';
  const ambiguousTombstone = JSON.stringify(['legacy-recovery', ambiguousLegacy]);
  await collisionPage.goto(`${origin}/legacy-collision-first#/home`);
  await collisionPage.evaluate(({ keys, marker }) => {
    sessionStorage.setItem(keys.recovery, JSON.stringify([marker]));
    window.fetch = () => Promise.resolve(new Response(JSON.stringify({
      CacheKey: 'c', Epoch: 'legacy-collision-first',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    window.JellyfinRefreshKitConfig = {
      name: 'a|b', mode: 'auto', bootVersion: 'c', versionUrl: '/version',
      versionJsonField: 'CacheKey', versionEpochJsonField: 'Epoch',
      pollSeconds: 3600, idleSeconds: 300,
    };
  }, { keys: storageKeys, marker: ambiguousLegacy });
  await injectRuntime(collisionPage);
  await collisionPage.waitForFunction(() => (
    window.JellyfinRefreshKit.get('a|b').state().latestEpoch === 'legacy-collision-first'
  ));
  assert.deepEqual(await collisionPage.evaluate((keys) => (
    JSON.parse(sessionStorage.getItem(keys.recovery))
  ), storageKeys), [ambiguousTombstone]);

  await collisionPage.goto(`${origin}/legacy-collision-second#/home`);
  await collisionPage.evaluate(() => {
    window.__epochFetchCount = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'D', Epoch: 'legacy-collision-second',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'a', mode: 'auto', bootVersion: 'b|c', versionUrl: '/version',
      versionJsonField: 'CacheKey', versionEpochJsonField: 'Epoch',
      pollSeconds: 3600, idleSeconds: 300,
    };
  });
  await injectRuntime(collisionPage, fastEpochRuntime());
  await waitForEpochFetches(collisionPage, 1);
  await new Promise((resolve) => setTimeout(resolve, 75));
  const collisionOutcome = await collisionPage.evaluate((keys) => ({
    calls: window.__epochFetchCount,
    pending: window.JellyfinRefreshKit.get('a').state().updatePending,
    records: JSON.parse(sessionStorage.getItem(keys.recovery)),
  }), storageKeys);
  assert.equal(collisionOutcome.calls, 1,
    'the colliding legacy tombstone rejects the boot seed before mismatch confirmation');
  assert.equal(collisionOutcome.pending, false,
    'both interpretations of released ambiguous evidence remain spent');
  assert.deepEqual(collisionOutcome.records, [ambiguousTombstone],
    'exact reconvergence by one interpretation cannot release the legacy tombstone');
});

test('strict LEFT history must verify the transition before any reload attempt', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const cases = [
    { name: 'LeftSilentNoop', behavior: 'noop' },
    { name: 'LeftWriteThrow', behavior: 'throw' },
    { name: 'LeftCorrupt', behavior: 'corrupt' },
    { name: 'LeftUnreadable', behavior: 'unreadable' },
    { name: 'LeftFull', behavior: 'full' },
  ];
  const full = Array.from({ length: 128 }, (_, index) => `Filled${index}|G${index}`);

  for (const scenario of cases) {
    const page = await browser.newPage();
    await page.goto(`${origin}/${scenario.name}#/home`);
    await page.evaluate(({ keys, item, filled }) => {
      if (item.behavior === 'corrupt') sessionStorage.setItem(keys.left, '{not-json');
      if (item.behavior === 'full') sessionStorage.setItem(keys.left, JSON.stringify(filled));
      const nativeGet = Storage.prototype.getItem;
      const nativeSet = Storage.prototype.setItem;
      if (item.behavior === 'unreadable') {
        Storage.prototype.getItem = function getItem(key) {
          if (key === keys.left) throw new DOMException('blocked', 'SecurityError');
          return nativeGet.call(this, key);
        };
      }
      if (item.behavior === 'noop' || item.behavior === 'throw') {
        Storage.prototype.setItem = function setItem(key, value) {
          if (key === keys.left) {
            if (item.behavior === 'throw') throw new DOMException('blocked', 'QuotaExceededError');
            return undefined;
          }
          return nativeSet.call(this, key, value);
        };
      }
      window.__epochFetchCount = 0;
      window.__reloadAttempts = 0;
      window.fetch = () => {
        window.__epochFetchCount += 1;
        return Promise.resolve(new Response(JSON.stringify({
          CacheKey: 'G3', Epoch: 'left-test-process',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      };
      window.JellyfinRefreshKitConfig = {
        name: item.name,
        mode: 'auto',
        bootVersion: 'G2',
        versionUrl: '/version',
        versionJsonField: 'CacheKey',
        versionEpochJsonField: 'Epoch',
        pollSeconds: 3600,
        idleSeconds: 0,
        reloadBudget: 1,
      };
    }, { keys: storageKeys, item: scenario, filled: full });
    const source = fastEpochRuntime()
      .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
      .replace('location.reload();', 'window.__reloadAttempts += 1;');
    await injectRuntime(page, source);
    await waitForEpochFetches(page, 2);
    await new Promise((resolve) => setTimeout(resolve, 75));
    const outcome = await page.evaluate((name) => ({
      state: window.JellyfinRefreshKit.get(name).state(),
      reloads: window.__reloadAttempts,
    }), scenario.name);
    assert.equal(outcome.reloads, 0, `${scenario.name} must not call reload`);
    if (scenario.behavior === 'noop' || scenario.behavior === 'throw' ||
        scenario.behavior === 'full') {
      assert.equal(outcome.state.updatePending, true, `${scenario.name} keeps the pending intent`);
      assert.equal(outcome.state.lastBlockReason, 'safety_history');
    } else {
      assert.equal(outcome.state.updatePending, false,
        `${scenario.name} refuses an update while history is unreadable`);
    }
    if (scenario.behavior === 'full') {
      const persisted = await page.evaluate((keys) => ({
        left: JSON.parse(sessionStorage.getItem(keys.left)),
        sessionBudget: sessionStorage.getItem(keys.budget),
        localBudget: localStorage.getItem(keys.budget),
      }), storageKeys);
      assert.deepEqual(persisted.left, full,
        'full LEFT history never FIFO-evicts older evidence');
      assert.equal(persisted.sessionBudget, null,
        'LEFT refusal happens before consuming a session reload slot');
      assert.equal(persisted.localBudget, null,
        'LEFT refusal happens before consuming a shared reload slot');
    }
    await page.close();
  }
});

test('a reload stamp made future-relative by a backward clock adjustment remains spent', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/backward-clock-budget#/home`);
  const futureStamp = await page.evaluate((keys) => {
    const stamp = Date.now() + 60_000;
    const serialized = JSON.stringify([stamp]);
    sessionStorage.setItem(keys.budget, serialized);
    localStorage.setItem(keys.budget, serialized);
    window.__reloadAttempts = 0;
    window.JellyfinRefreshKitConfig = {
      name: 'BackwardClockBudget',
      mode: 'auto',
      bootVersion: 'A',
      reloadBudget: 1,
      pollSeconds: 3600,
      idleSeconds: 0,
      getVersion: () => 'B',
    };
    return stamp;
  }, storageKeys);
  const source = fastEpochRuntime(runtime)
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  await injectRuntime(page, source);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('BackwardClockBudget')?.state().lastBlockReason
      === 'reload_budget'
  ));
  const result = await page.evaluate((keys) => ({
    reloads: window.__reloadAttempts,
    pending: window.JellyfinRefreshKit.get('BackwardClockBudget').state().updatePending,
    sessionBudget: JSON.parse(sessionStorage.getItem(keys.budget)),
    localBudget: JSON.parse(localStorage.getItem(keys.budget)),
  }), storageKeys);
  assert.equal(result.reloads, 0);
  assert.equal(result.pending, true);
  assert.deepEqual(result.sessionBudget, [futureStamp]);
  assert.deepEqual(result.localBudget, [futureStamp]);
});

test('a backward clock step cannot bank the post-playback idle relaxation', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/#/video`);
  await page.evaluate(() => {
    window.__clockNow = 10_000_000;
    Date.now = () => window.__clockNow;
    window.__reloadAttempts = 0;
    const dialog = document.createElement('dialog');
    dialog.id = 'clock-mask-dialog';
    dialog.setAttribute('open', '');
    dialog.textContent = 'hold the pending reload';
    document.body.appendChild(dialog);
    window.JellyfinRefreshKitConfig = {
      name: 'BackwardClockMask',
      mode: 'auto',
      bootVersion: 'A',
      pollSeconds: 3600,
      idleSeconds: 300,
      getVersion: () => 'B',
    };
  });
  const source = fastEpochRuntime(runtime)
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  await injectRuntime(page, source);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('BackwardClockMask')?.state().updatePending === true
  ));

  await page.evaluate(() => {
    location.hash = '#/home';
    document.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.maskedTransitionMsLeft > 0
  ));
  assert.equal(await page.evaluate(() => window.__reloadAttempts), 0);

  const rolledBack = await page.evaluate(() => {
    window.__clockNow -= 60 * 60 * 1000;
    document.querySelector('#clock-mask-dialog').remove();
    const state = window.JellyfinRefreshKit.state();
    return {
      mask: state.shared.maskedTransitionMsLeft,
      reason: state.shared.blockReason,
      reloads: window.__reloadAttempts,
    };
  });
  assert.deepEqual(rolledBack, {
    mask: 0,
    reason: 'not_idle',
    reloads: 0,
  });
});

test('budget refusal retracts only LEFT records newly preclaimed by that attempt', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/budget-left-transaction#/home`);
  await page.evaluate((keys) => {
    const fullBudget = JSON.stringify([Date.now()]);
    sessionStorage.setItem(keys.budget, fullBudget);
    localStorage.setItem(keys.budget, fullBudget);
    sessionStorage.setItem(keys.left, JSON.stringify(['OlderInstance|G0']));
    window.__epochFetchCount = 0;
    window.__reloadAttempts = 0;
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'G3', Epoch: 'budget-process',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'BudgetLeftTransaction',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 0,
      reloadBudget: 1,
    };
  }, storageKeys);
  const source = fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  await injectRuntime(page, source);
  await waitForEpochFetches(page, 2);
  await new Promise((resolve) => setTimeout(resolve, 75));
  const result = await page.evaluate((keys) => ({
    reloads: window.__reloadAttempts,
    left: JSON.parse(sessionStorage.getItem(keys.left)),
    state: window.JellyfinRefreshKit.get('BudgetLeftTransaction').state(),
  }), storageKeys);
  assert.equal(result.reloads, 0);
  assert.equal(result.state.updatePending, true);
  assert.equal(result.state.lastBlockReason, 'reload_budget');
  assert.deepEqual(result.left, ['OlderInstance|G0'],
    'the new baseline marker is retracted without touching older LEFT evidence');
});

test('committed reload preclaims every baseline and freezes a held late confirmation', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/late-confirmation#/home`);
  await page.evaluate(() => {
    window.__reloadAttempts = 0;
    window.__aCalls = 0;
    window.__bCalls = 0;
    window.fetch = (url) => {
      if (String(url).includes('/version-a')) {
        window.__aCalls += 1;
        if (window.__aCalls === 2) {
          return new Promise((resolve) => {
            window.__releaseAConfirmation = () => resolve(new Response(JSON.stringify({
              CacheKey: 'A1', Epoch: 'process-a1',
            }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
          });
        }
        return Promise.resolve(new Response(JSON.stringify({
          CacheKey: 'A1', Epoch: 'process-a1',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      }
      window.__bCalls += 1;
      if (window.__bCalls === 2) {
        return new Promise((resolve) => {
          window.__releaseBConfirmation = () => resolve(new Response(JSON.stringify({
            CacheKey: 'B1', Epoch: 'process-b1',
          }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        });
      }
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'B1', Epoch: 'process-b1',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
  });
  const source = fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace(
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 3000;',
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 150;',
    )
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  const common = {
    'data-version-json-field': 'CacheKey',
    'data-version-epoch-json-field': 'Epoch',
    'data-mode': 'auto',
    'data-poll-seconds': '3600',
    'data-idle-seconds': '0',
  };
  await injectConfiguredRuntime(page, source, {
    ...common,
    'data-name': 'CommitA',
    'data-boot-version': 'A0',
    'data-version-url': '/version-a',
  });
  await page.waitForFunction(() => window.__aCalls === 2);
  await injectConfiguredRuntime(page, source, {
    ...common,
    'data-name': 'CommitB',
    'data-boot-version': 'B0',
    'data-version-url': '/version-b',
  });
  await page.waitForFunction(() => window.__bCalls === 2);

  await page.evaluate(() => window.__releaseAConfirmation());
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  const preclaimed = await page.evaluate((keys) => ({
    left: JSON.parse(sessionStorage.getItem(keys.left)),
    gaps: JSON.parse(sessionStorage.getItem(keys.gaps)),
  }), storageKeys);
  assert.ok(preclaimed.left.includes('CommitA|A0'));
  assert.ok(preclaimed.left.includes('CommitB|B0'),
    'the non-triggering instance baseline was claimed before navigation');
  assert.deepEqual(preclaimed.gaps, [
    ['CommitA', 'A0'],
    ['CommitB', 'B0'],
  ], 'every unknown boot baseline gap is claimed in the same shared preflight');

  await page.evaluate((keys) => {
    const nativeSet = Storage.prototype.setItem;
    Storage.prototype.setItem = function setItem(key, value) {
      if (key === keys.left) throw new DOMException('late LEFT blocked', 'QuotaExceededError');
      return nativeSet.call(this, key, value);
    };
    window.__releaseBConfirmation();
  }, storageKeys);
  await new Promise((resolve) => setTimeout(resolve, 50));
  const frozen = await page.evaluate(() => ({
    reloads: window.__reloadAttempts,
    state: window.JellyfinRefreshKit.get('CommitB').state(),
  }));
  assert.equal(frozen.reloads, 1);
  assert.equal(frozen.state.updatePending, false,
    'the unload-window confirmation is frozen, not falsely disarmed as a ride-along');
  assert.deepEqual(frozen.state.candidateEpochEvidence, [{ epoch: 'process-b1', count: 1 }]);

  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.reloadsSurvived === 1
  ));
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.get('CommitB').state().updatePending === true
  ));
  const recovered = await page.evaluate((keys) => ({
    reloads: window.__reloadAttempts,
    aPending: window.JellyfinRefreshKit.get('CommitA').state().updatePending,
    bState: window.JellyfinRefreshKit.get('CommitB').state(),
    left: JSON.parse(sessionStorage.getItem(keys.left)),
  }), storageKeys);
  assert.equal(recovered.reloads, 1);
  assert.equal(recovered.aPending, true, 'the failed navigation rearms the real trigger');
  assert.equal(recovered.bState.updatePending, true,
    'the non-triggering sibling consumes its held confirmation during recovery');
  assert.deepEqual(recovered.bState.candidateEpochEvidence, [
    { epoch: 'process-b1', count: 2 },
  ]);
  assert.ok(recovered.left.includes('CommitB|B0'),
    'failed verified retraction remains conservative when LEFT writes are blocked');
});

test('watchdog forces baseline revalidation after an ignored reload', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/watchdog-reconcile#/home`);
  await page.evaluate(() => {
    window.__reloadAttempts = 0;
    window.__fetchCalls = 0;
    window.__fetchCallsAtReload = null;
    window.__response = { CacheKey: 'G3', Epoch: 'update-process' };
    window.fetch = () => {
      window.__fetchCalls += 1;
      return Promise.resolve(new Response(JSON.stringify(window.__response), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'WatchdogReconcile',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 0,
      reloadBudget: 3,
    };
  });
  const source = fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('var RETRY_MS = 1000;', 'var RETRY_MS = 25;')
    .replace(
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 3000;',
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 75;',
    )
    .replace(
      'location.reload();',
      'window.__fetchCallsAtReload = window.__fetchCalls; ' +
        'window.__response = { CacheKey: "G2", Epoch: "baseline-return" }; ' +
        'window.__reloadAttempts += 1;',
    );
  await injectRuntime(page, source);
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  assert.equal(await page.evaluate(() => window.__fetchCallsAtReload), 2,
    'there is no in-flight observation when reload commits');
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.reloadsSurvived === 1 &&
    window.__fetchCalls >= 3
  ));
  await new Promise((resolve) => setTimeout(resolve, 125));
  const reconciled = await page.evaluate(() => ({
    reloads: window.__reloadAttempts,
    state: window.JellyfinRefreshKit.get('WatchdogReconcile').state(),
  }));
  assert.equal(reconciled.reloads, 1,
    'the stale G3 intent is not retried after baseline reconvergence');
  assert.equal(reconciled.state.updatePending, false);
  assert.equal(reconciled.state.latestVersion, 'G2');
});

test('watchdog disarms a stale target when a replacement generation lands during reload', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/watchdog-replacement#/home`);
  await page.evaluate(() => {
    window.__reloadAttempts = 0;
    window.__fetchCalls = 0;
    window.__fetchCallsAtReload = null;
    window.__response = { CacheKey: 'G3', Epoch: 'original-process' };
    window.fetch = () => {
      window.__fetchCalls += 1;
      if (window.__fetchCalls >= 4) return new Promise(() => {});
      return Promise.resolve(new Response(JSON.stringify(window.__response), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'WatchdogReplacement',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 0,
      reloadBudget: 3,
    };
  });
  const source = fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('var RETRY_MS = 1000;', 'var RETRY_MS = 25;')
    .replace(
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 3000;',
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 75;',
    )
    .replace(
      'location.reload();',
      'window.__fetchCallsAtReload = window.__fetchCalls; ' +
        'window.__response = { CacheKey: "G4", Epoch: "replacement-process" }; ' +
        'window.__reloadAttempts += 1;',
    );
  await injectRuntime(page, source);
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  assert.equal(await page.evaluate(() => window.__fetchCallsAtReload), 2);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.reloadsSurvived === 1
  ));
  await page.waitForFunction(() => window.__fetchCalls === 4);
  await new Promise((resolve) => setTimeout(resolve, 100));
  const reconciled = await page.evaluate(() => ({
    reloads: window.__reloadAttempts,
    state: window.JellyfinRefreshKit.get('WatchdogReplacement').state(),
  }));
  assert.equal(reconciled.reloads, 1,
    'the stale G3 target is not retried while G4 earns confirmation');
  assert.equal(reconciled.state.updatePending, false);
  assert.equal(reconciled.state.latestVersion, 'G4');
  assert.deepEqual(reconciled.state.candidateEpochEvidence, [
    { epoch: 'replacement-process', count: 1 },
  ]);
});

test('handoff keeps the failed-reload barrier across every live instance poll', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/barrier-handoff#/home`);
  await page.evaluate(() => {
    window.__reloadAttempts = 0;
    window.__aCalls = 0;
    window.__bCalls = 0;
    window.__aResolvers = {};
    window.__bResolvers = {};
    window.fetch = (url) => {
      if (String(url).includes('/barrier-a')) {
        window.__aCalls += 1;
        const call = window.__aCalls;
        if (call === 2) {
          return new Promise((resolve) => { window.__aResolvers[call] = resolve; });
        }
        return Promise.resolve(new Response(JSON.stringify({
          CacheKey: 'A1', Epoch: 'barrier-a-process',
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      }
      window.__bCalls += 1;
      const call = window.__bCalls;
      if (call >= 2) {
        return new Promise((resolve) => { window.__bResolvers[call] = resolve; });
      }
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'B0', Epoch: 'barrier-b-process',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
  });
  const makeSource = (version) => fastEpochRuntime(runtimeAtVersion(version))
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('var RETRY_MS = 1000;', 'var RETRY_MS = 25;')
    .replace(
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 3000;',
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 75;',
    )
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  const common = {
    'data-version-json-field': 'CacheKey',
    'data-version-epoch-json-field': 'Epoch',
    'data-mode': 'auto',
    'data-poll-seconds': '3600',
    'data-idle-seconds': '0',
  };
  await injectConfiguredRuntime(page, makeSource('2.4.6'), {
    ...common,
    'data-name': 'BarrierA',
    'data-boot-version': 'A0',
    'data-version-url': '/barrier-a',
  });
  await page.waitForFunction(() => window.__aCalls === 2);
  await injectConfiguredRuntime(page, makeSource('2.4.6'), {
    ...common,
    'data-name': 'BarrierB',
    'data-boot-version': 'B0',
    'data-version-url': '/barrier-b',
  });
  await page.waitForFunction(() => window.__bCalls === 1);
  await page.evaluate(() => {
    window.__aResolvers[2](new Response(JSON.stringify({
      CacheKey: 'A1', Epoch: 'barrier-a-process',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  });
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.reloadRevalidationPending === true &&
    window.__bCalls === 2
  ));

  await injectConfiguredRuntime(page, makeSource('2.4.7'), {
    ...common,
    'data-name': 'BarrierA',
    'data-boot-version': 'A0',
    'data-version-url': '/barrier-a',
  });
  await page.waitForFunction(() => window.JellyfinRefreshKit.kitVersion === '2.4.7');
  await page.waitForFunction(() => window.__bCalls === 3);
  await new Promise((resolve) => setTimeout(resolve, 150));
  const held = await page.evaluate(() => ({
    reloads: window.__reloadAttempts,
    barrier: window.JellyfinRefreshKit.state().shared.reloadRevalidationPending,
    aPending: window.JellyfinRefreshKit.get('BarrierA').state().updatePending,
    bPending: window.JellyfinRefreshKit.get('BarrierB').state().updatePending,
  }));
  assert.deepEqual(held, {
    reloads: 1,
    barrier: true,
    aPending: true,
    bPending: false,
  }, 'A cannot retry while non-pending B still owes its replacement observation');

  await page.evaluate(() => {
    window.__bResolvers[3](new Response(JSON.stringify({
      CacheKey: 'B0', Epoch: 'barrier-b-process',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
  });
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.reloadRevalidationPending === false
  ));
  await page.waitForFunction(() => window.__reloadAttempts === 2);
});

test('failed-reload barrier completion rearms the hidden-settle path', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/barrier-hidden#/home`);
  await page.evaluate(() => {
    window.__reloadAttempts = 0;
    window.__fetchCalls = 0;
    window.__visibility = 'visible';
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get() { return window.__visibility; },
    });
    Object.defineProperty(document, 'hidden', {
      configurable: true,
      get() { return window.__visibility === 'hidden'; },
    });
    window.fetch = () => {
      window.__fetchCalls += 1;
      if (window.__fetchCalls === 3) {
        return new Promise((resolve) => {
          window.__releaseHiddenRevalidation = () => resolve(new Response(JSON.stringify({
            CacheKey: 'G3', Epoch: 'hidden-barrier-process',
          }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
        });
      }
      return Promise.resolve(new Response(JSON.stringify({
        CacheKey: 'G3', Epoch: 'hidden-barrier-process',
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'HiddenBarrier',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 0,
      reloadBudget: 3,
      hiddenReload: true,
      hiddenSettleSeconds: 0.1,
    };
  });
  const source = fastEpochRuntime()
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('var RETRY_MS = 1000;', 'var RETRY_MS = 25;')
    .replace(
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 3000;',
      'var RELOAD_SURVIVAL_WATCHDOG_MS = 75;',
    )
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  await injectRuntime(page, source);
  await page.waitForFunction(() => window.__reloadAttempts === 1);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.reloadRevalidationPending === true &&
    window.__fetchCalls === 3
  ));
  await page.evaluate(() => {
    window.__visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  await page.evaluate(() => window.__releaseHiddenRevalidation());
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.reloadRevalidationPending === false
  ));
  await page.waitForFunction(() => window.__reloadAttempts === 2, { timeout: 1000 });
  assert.equal(await page.evaluate(() => document.visibilityState), 'hidden',
    'the reload happens after hidden settle without a visibility wake');
});

test('baseline reconvergence resets notification watermarks and releases same-document recovery', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);

  const notify = await browser.newPage();
  await notify.goto(`${origin}/notify-reconvergence#/home`);
  await notify.evaluate(() => {
    window.__response = { CacheKey: 'G3', Epoch: 'notify-g3-a' };
    window.__announcements = 0;
    window.fetch = () => Promise.resolve(new Response(JSON.stringify(window.__response), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    window.JellyfinRefreshKitConfig = {
      name: 'NotifyReconvergence',
      mode: 'notify',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
      onUpdateAvailable() { window.__announcements += 1; },
    };
  });
  await injectRuntime(notify, fastEpochRuntime());
  await notify.waitForFunction(() => window.__announcements === 1);
  await notify.evaluate(() => {
    window.__response = { CacheKey: 'G2', Epoch: 'notify-g2' };
    return window.JellyfinRefreshKit.get('NotifyReconvergence').checkNow();
  });
  await notify.evaluate(() => {
    window.__response = { CacheKey: 'G3', Epoch: 'notify-g3-b' };
    return window.JellyfinRefreshKit.get('NotifyReconvergence').checkNow();
  });
  await notify.evaluate(() => window.JellyfinRefreshKit.get('NotifyReconvergence').checkNow());
  assert.equal(await notify.evaluate(() => window.__announcements), 2,
    'G2→G3→G2→G3 is announced once per distinct episode');

  const auto = await browser.newPage();
  await auto.goto(`${origin}/refused-reconvergence#/home`);
  await auto.evaluate((keys) => {
    sessionStorage.setItem(keys.left, JSON.stringify(['RefusedReconvergence|G0']));
    sessionStorage.setItem(keys.epochs, JSON.stringify([['RefusedReconvergence', 'old-epoch']]));
    window.__response = { CacheKey: 'G0', Epoch: 'old-epoch' };
    window.__announcements = 0;
    window.fetch = () => Promise.resolve(new Response(JSON.stringify(window.__response), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }));
    window.JellyfinRefreshKitConfig = {
      name: 'RefusedReconvergence',
      mode: 'auto',
      bootVersion: 'G2',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
      onUpdateAvailable() { window.__announcements += 1; },
    };
  }, storageKeys);
  await injectRuntime(auto, fastEpochRuntime());
  await auto.waitForFunction(() => window.__announcements === 1);
  await auto.evaluate(() => {
    window.__response = { CacheKey: 'G2', Epoch: 'baseline-process' };
    return window.JellyfinRefreshKit.get('RefusedReconvergence').checkNow();
  });
  await auto.evaluate(() => {
    window.__response = { CacheKey: 'G0', Epoch: 'fresh-epoch' };
    return window.JellyfinRefreshKit.get('RefusedReconvergence').checkNow();
  });
  await auto.evaluate(() => window.JellyfinRefreshKit.get('RefusedReconvergence').checkNow());
  const accepted = await auto.evaluate(() => ({
    announcements: window.__announcements,
    state: window.JellyfinRefreshKit.get('RefusedReconvergence').state(),
  }));
  assert.equal(accepted.announcements, 2);
  assert.equal(accepted.state.updatePending, true);
  assert.equal(accepted.state.authorizedEpoch, 'fresh-epoch');

  const sameDocument = await browser.newPage();
  await sameDocument.goto(`${origin}/same-document-recovery#/home`);
  await sameDocument.evaluate(() => {
    window.__epochFetchCount = 0;
    window.__response = { CacheKey: 'EndpointGeneration', Epoch: 'endpoint-process' };
    window.fetch = () => {
      window.__epochFetchCount += 1;
      return Promise.resolve(new Response(JSON.stringify(window.__response), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }));
    };
    window.JellyfinRefreshKitConfig = {
      name: 'SameDocumentRecovery',
      mode: 'auto',
      bootVersion: 'StampedGeneration',
      versionUrl: '/version',
      versionJsonField: 'CacheKey',
      versionEpochJsonField: 'Epoch',
      pollSeconds: 3600,
      idleSeconds: 300,
    };
  });
  await injectRuntime(sameDocument, fastEpochRuntime());
  await waitForEpochFetches(sameDocument, 2);
  assert.match(await sameDocument.evaluate((keys) => (
    sessionStorage.getItem(keys.recovery)
  ), storageKeys), /SameDocumentRecovery/);
  await sameDocument.evaluate(() => {
    window.__response = { CacheKey: 'StampedGeneration', Epoch: 'boot-process' };
    return window.JellyfinRefreshKit.get('SameDocumentRecovery').checkNow();
  });
  const reconverged = await sameDocument.evaluate((keys) => ({
    marker: sessionStorage.getItem(keys.recovery),
    state: window.JellyfinRefreshKit.get('SameDocumentRecovery').state(),
  }), storageKeys);
  assert.equal(reconverged.marker, '[]');
  assert.equal(reconverged.state.baselineFromBootSeed, false);
  assert.equal(reconverged.state.bootRecoveryAuthorized, false);
});

async function loadSafetyRuntime(page) {
  await page.goto('about:blank');
  await page.evaluate(() => {
    location.hash = '#/home';
    window.JellyfinRefreshKitConfig = {
      name: 'SafetyVisibilityTest',
      mode: 'auto',
      pollSeconds: 3600,
      idleSeconds: 0,
    };
  });
  await injectRuntime(page);
  await page.waitForFunction(() => window.JellyfinRefreshKit?.get('SafetyVisibilityTest'));
  // The safety engine deliberately floors idle at one second.
  await new Promise((resolve) => setTimeout(resolve, 1100));
}

async function blockReason(page) {
  return page.evaluate(() => (
    window.JellyfinRefreshKit.get('SafetyVisibilityTest').state().wouldBlockNow
  ));
}

test('srcObject-backed playback blocks reloads and source replacement resets its progress fingerprint', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/#/home`);
  await page.evaluate(() => {
    const media = document.createElement('video');
    media.id = 'object-backed-media';
    media.srcObject = new MediaStream();
    Object.defineProperty(media, 'paused', {
      configurable: true,
      get: () => false,
    });
    Object.defineProperty(media, 'currentTime', {
      configurable: true,
      get: () => 0,
    });
    document.body.appendChild(media);
    window.__objectBackedMedia = media;
    window.__reloadAttempts = 0;
    window.JellyfinRefreshKitConfig = {
      name: 'SrcObjectMediaTest',
      mode: 'auto',
      bootVersion: 'A',
      pollSeconds: 3600,
      idleSeconds: 0,
      getVersion: () => 'B',
    };
  });
  const source = fastEpochRuntime(runtime)
    .replace('var MIN_SETTLE_MS = 1000;', 'var MIN_SETTLE_MS = 0;')
    .replace('location.reload();', 'window.__reloadAttempts += 1;');
  await injectRuntime(page, source);
  await page.waitForFunction(() => {
    const state = window.JellyfinRefreshKit?.get('SrcObjectMediaTest')?.state();
    return state?.updatePending === true
      && state.wouldBlockNow === 'media_element'
      && window.JellyfinRefreshKit.state().shared.lastBlockReason === 'media_element';
  });
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit.state().shared.mediaBlockedForMs >= 150
  ));
  const beforeSwap = await page.evaluate(() => (
    window.JellyfinRefreshKit.state().shared.mediaBlockedForMs
  ));

  await page.evaluate(() => {
    window.__objectBackedMedia.srcObject = new MediaStream();
    return window.JellyfinRefreshKit.get('SrcObjectMediaTest').checkNow();
  });
  const afterSwap = await page.evaluate(() => ({
    blockedFor: window.JellyfinRefreshKit.state().shared.mediaBlockedForMs,
    reason: window.JellyfinRefreshKit.get('SrcObjectMediaTest').state().wouldBlockNow,
    reloads: window.__reloadAttempts,
  }));
  assert.equal(afterSwap.reason, 'media_element');
  assert.equal(afterSwap.reloads, 0);
  assert.ok(afterSwap.blockedFor < beforeSwap,
    `source replacement should reset the streak (${beforeSwap}ms -> ${afterSwap.blockedFor}ms)`);
});

test('retained hidden password fields do not permanently block authenticated pages', async (t) => {
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await loadSafetyRuntime(page);
  await page.evaluate(() => {
    const retainedLogin = document.createElement('div');
    retainedLogin.id = 'retained-login';
    retainedLogin.style.display = 'none';
    retainedLogin.innerHTML = '<input id="retained-password" type="password" value="submitted-secret">';
    document.body.appendChild(retainedLogin);
  });

  assert.equal(await blockReason(page), null);

  await page.evaluate(() => {
    document.querySelector('#retained-login').style.display = 'block';
  });
  assert.equal(await blockReason(page), 'password_entry');

  await page.evaluate(() => {
    document.querySelector('#retained-login').setAttribute('aria-hidden', 'TRUE');
  });
  assert.equal(await blockReason(page), null);
});

test('a visible populated password field remains protected', async (t) => {
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await loadSafetyRuntime(page);
  await page.evaluate(() => {
    const field = document.createElement('input');
    field.type = 'password';
    field.value = 'unfinished-secret';
    document.body.appendChild(field);
  });

  assert.equal(await blockReason(page), 'password_entry');

  await page.evaluate(() => {
    document.querySelector('input[type="password"]').disabled = true;
  });
  assert.equal(await blockReason(page), null);

  await page.evaluate(() => {
    const field = document.querySelector('input[type="password"]');
    field.disabled = false;
    const fieldset = document.createElement('fieldset');
    fieldset.disabled = true;
    field.replaceWith(fieldset);
    fieldset.appendChild(field);
  });
  assert.equal(await blockReason(page), null);

  await page.evaluate(() => {
    const fieldset = document.querySelector('fieldset');
    fieldset.disabled = false;
    fieldset.querySelector('input[type="password"]').focus();
    fieldset.setAttribute('inert', '');
  });
  assert.equal(await blockReason(page), null);

  await page.evaluate(() => {
    const fieldset = document.querySelector('fieldset');
    fieldset.removeAttribute('inert');
    const field = fieldset.querySelector('input[type="password"]');
    field.blur();
    field.setAttribute('aria-disabled', 'true');
  });
  assert.equal(await blockReason(page), 'password_entry');
});

test('only rendered dialogs block, with visibility probe failures failing safe', async (t) => {
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await loadSafetyRuntime(page);
  await page.evaluate(() => {
    const dialog = document.createElement('div');
    dialog.id = 'retained-dialog';
    dialog.setAttribute('role', 'dialog');
    dialog.textContent = 'Retained plugin dialog';
    dialog.style.display = 'none';
    document.body.appendChild(dialog);
  });

  assert.equal(await blockReason(page), null);

  await page.evaluate(() => {
    const dialog = document.querySelector('#retained-dialog');
    dialog.style.display = 'block';
  });
  assert.equal(await blockReason(page), 'dialog');

  await page.evaluate(() => {
    document.querySelector('#retained-dialog').setAttribute('aria-hidden', 'TRUE');
  });
  assert.equal(await blockReason(page), null);

  await page.evaluate(() => {
    document.querySelector('#retained-dialog').removeAttribute('aria-hidden');
  });
  assert.equal(await blockReason(page), 'dialog');

  await page.evaluate(() => {
    const dialog = document.querySelector('#retained-dialog');
    dialog.style.visibility = 'hidden';
  });
  assert.equal(await blockReason(page), null);

  await page.evaluate(() => {
    const dialog = document.querySelector('#retained-dialog');
    dialog.style.visibility = 'visible';
    dialog.style.display = 'contents';
  });
  assert.equal(await blockReason(page), 'dialog');

  await page.evaluate(() => {
    const dialog = document.querySelector('#retained-dialog');
    const wrapper = document.createElement('div');
    wrapper.id = 'hidden-dialog-wrapper';
    wrapper.style.display = 'none';
    dialog.replaceWith(wrapper);
    wrapper.appendChild(dialog);
  });
  assert.equal(await blockReason(page), null);

  await page.evaluate(() => {
    const wrapper = document.querySelector('#hidden-dialog-wrapper');
    wrapper.style.display = 'block';
    wrapper.style.contentVisibility = 'hidden';
  });
  assert.equal(await blockReason(page), null);

  await page.evaluate(() => {
    const dialog = document.querySelector('#retained-dialog');
    document.querySelector('#hidden-dialog-wrapper').style.contentVisibility = 'visible';
    dialog.style.display = 'block';
    dialog.getClientRects = () => { throw new Error('visibility probe failed'); };
  });
  assert.equal(await blockReason(page), 'dialog');
});

test('native open and modal dialog elements participate in the dialog safety gate', async (t) => {
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await loadSafetyRuntime(page);
  await page.evaluate(() => {
    const dialog = document.createElement('dialog');
    dialog.id = 'native-safety-dialog';
    dialog.textContent = 'Unsaved native dialog work';
    document.body.appendChild(dialog);
    dialog.showModal();
  });
  assert.equal(await blockReason(page), 'dialog');

  await page.evaluate(() => document.querySelector('#native-safety-dialog').close());
  assert.equal(await blockReason(page), null);

  await page.evaluate(() => document.querySelector('#native-safety-dialog').show());
  assert.equal(await blockReason(page), 'dialog');

  await page.evaluate(() => {
    document.querySelector('#native-safety-dialog').remove();
    const alert = document.createElement('div');
    alert.id = 'uppercase-alert-dialog';
    alert.setAttribute('role', 'ALERTDIALOG');
    alert.textContent = 'Uppercase ARIA role';
    document.body.appendChild(alert);
  });
  assert.equal(await blockReason(page), 'dialog');

  await page.evaluate(() => {
    document.querySelector('#uppercase-alert-dialog').remove();
    const modal = document.createElement('div');
    modal.id = 'uppercase-aria-modal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'TRUE');
    modal.textContent = 'Uppercase ARIA modal value';
    document.body.appendChild(modal);
  });
  assert.equal(await blockReason(page), 'dialog');

  await page.evaluate(() => {
    document.querySelector('#uppercase-aria-modal').remove();
    const fallback = document.createElement('div');
    fallback.id = 'fallback-role-dialog';
    fallback.setAttribute('role', 'unknown DIALOG');
    fallback.textContent = 'ARIA fallback role list';
    document.body.appendChild(fallback);
  });
  assert.equal(await blockReason(page), 'dialog');
});

test('forced checks join the in-flight version request', async (t) => {
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto('about:blank');
  await page.evaluate(() => {
    window.__versionCalls = 0;
    window.__versionResolvers = [];
    window.JellyfinRefreshKitConfig = {
      name: 'SingleFlightTest',
      mode: 'notify',
      pollSeconds: 3600,
      getVersion() {
        window.__versionCalls += 1;
        return new Promise((resolve) => window.__versionResolvers.push(resolve));
      },
    };
  });
  await injectRuntime(page);
  await page.waitForFunction(() => window.__versionCalls === 1);

  await page.evaluate(() => window.__versionResolvers.shift()('A'));
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('SingleFlightTest')?.version === 'A'
  ));

  const samePromise = await page.evaluate(() => {
    const handle = window.JellyfinRefreshKit.get('SingleFlightTest');
    const first = handle.checkNow();
    const second = handle.checkNow();
    window.__joinedChecks = Promise.all([first, second]);
    return first === second;
  });
  assert.equal(samePromise, true);
  await page.waitForFunction(() => window.__versionCalls === 2);
  assert.equal(await page.evaluate(() => window.__versionResolvers.length), 1);

  await page.evaluate(() => window.__versionResolvers.shift()('B'));
  await page.evaluate(() => window.__joinedChecks);
  const state = await page.evaluate(() => (
    window.JellyfinRefreshKit.get('SingleFlightTest').state()
  ));
  assert.equal(state.latestVersion, 'B');
  assert.equal(state.candidateVersion, 'B');
  assert.equal(await page.evaluate(() => window.__versionCalls), 2);
});

test('retained instance handles follow chained newest-wins handoffs', async (t) => {
  let currentVersion = 'A';
  let requestCount = 0;
  let heldFirstResponse;
  let markFirstRequest;
  const firstRequest = new Promise((resolve) => { markFirstRequest = resolve; });
  const origin = await startServer(t, (req, res) => {
    const pathname = new URL(req.url, 'http://runtime.test').pathname;
    if (pathname === '/version') {
      requestCount += 1;
      if (requestCount === 1) {
        heldFirstResponse = res;
        markFirstRequest();
        return;
      }
      res.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
      res.end(currentVersion);
      return;
    }
    serveHtml(res);
  });

  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  const attributes = {
    'data-name': 'RetainedHandoffTest',
    'data-version-url': `${origin}/version`,
    'data-mode': 'notify',
    'data-poll-seconds': '3600',
    'data-asset-patterns': '/adopter/',
  };

  await injectConfiguredRuntime(page, runtimeAtVersion('2.4.3'), attributes);
  await firstRequest;
  await page.evaluate(() => {
    const shape = (handle) => ({
      frozen: Object.isFrozen(handle),
      properties: Object.getOwnPropertyNames(handle).map((name) => {
        const descriptor = Object.getOwnPropertyDescriptor(handle, name);
        if (Object.prototype.hasOwnProperty.call(descriptor, 'value')) {
          return {
            name,
            kind: 'data',
            valueType: typeof descriptor.value,
            writable: descriptor.writable,
            enumerable: descriptor.enumerable,
            configurable: descriptor.configurable,
          };
        }
        return {
          name,
          kind: 'accessor',
          getType: typeof descriptor.get,
          setType: typeof descriptor.set,
          enumerable: descriptor.enumerable,
          configurable: descriptor.configurable,
        };
      }),
    });
    window.__retainedHandleShape = shape;
    window.__oldestHandle = window.JellyfinRefreshKit.get('RetainedHandoffTest');
    window.__oldestHandleShape = shape(window.__oldestHandle);
  });

  // The first copy is still waiting for headers. Its replacement must establish
  // the baseline itself; that leaves the retired closure at version=null and
  // makes getter/URL forwarding observable rather than merely identity-equal.
  await injectConfiguredRuntime(page, runtimeAtVersion('2.4.4'), attributes);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.kitVersion === '2.4.4'
      && window.JellyfinRefreshKit.get('RetainedHandoffTest')?.version === 'A'
  ));
  await page.evaluate(() => {
    window.__middleHandle = window.JellyfinRefreshKit.get('RetainedHandoffTest');
    window.__middleHandleShape = window.__retainedHandleShape(window.__middleHandle);
  });

  await injectConfiguredRuntime(page, runtime, attributes);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.kitVersion === '2.4.6'
      && window.JellyfinRefreshKit.get('RetainedHandoffTest')?.state().kitVersion === '2.4.6'
  ));

  const afterHandoffs = await page.evaluate(() => {
    const current = window.JellyfinRefreshKit.get('RetainedHandoffTest');
    const observe = (handle) => ({
      name: handle.name,
      version: handle.version,
      latestVersion: handle.latestVersion,
      versionedUrl: handle.versionedUrl('/adopter/plugin.js'),
      stateKitVersion: handle.state().kitVersion,
    });
    return {
      oldestIsCurrent: window.__oldestHandle === current,
      middleIsCurrent: window.__middleHandle === current,
      oldestIsMiddle: window.__oldestHandle === window.__middleHandle,
      oldest: observe(window.__oldestHandle),
      middle: observe(window.__middleHandle),
      current: observe(current),
      lineage: window.JellyfinRefreshKit.state().shared.managerLineage,
      handoffs: window.JellyfinRefreshKit.state().shared.managerHandoffs,
    };
  });
  assert.deepEqual(afterHandoffs, {
    oldestIsCurrent: false,
    middleIsCurrent: false,
    oldestIsMiddle: false,
    oldest: {
      name: 'RetainedHandoffTest',
      version: 'A',
      latestVersion: 'A',
      versionedUrl: '/adopter/plugin.js?v=A',
      stateKitVersion: '2.4.6',
    },
    middle: {
      name: 'RetainedHandoffTest',
      version: 'A',
      latestVersion: 'A',
      versionedUrl: '/adopter/plugin.js?v=A',
      stateKitVersion: '2.4.6',
    },
    current: {
      name: 'RetainedHandoffTest',
      version: 'A',
      latestVersion: 'A',
      versionedUrl: '/adopter/plugin.js?v=A',
      stateKitVersion: '2.4.6',
    },
    lineage: ['2.4.3', '2.4.4', '2.4.6'],
    handoffs: 2,
  });
  assert.equal(requestCount, 2, 'only the replacement may retry the interrupted baseline fetch');

  currentVersion = 'B';
  await page.evaluate(() => window.__oldestHandle.checkNow());
  assert.equal(requestCount, 3, 'the oldest handle must check through the current instance');
  assert.deepEqual(await page.evaluate(() => ({
    oldest: window.__oldestHandle.latestVersion,
    middle: window.__middleHandle.latestVersion,
    current: window.JellyfinRefreshKit.get('RetainedHandoffTest').latestVersion,
  })), { oldest: 'B', middle: 'B', current: 'B' });

  currentVersion = 'C';
  await page.evaluate(() => window.__middleHandle.checkNow());
  assert.equal(requestCount, 4, 'an intermediate handle must check through the second handoff');
  assert.deepEqual(await page.evaluate(() => ({
    oldest: window.__oldestHandle.latestVersion,
    middle: window.__middleHandle.latestVersion,
    current: window.JellyfinRefreshKit.get('RetainedHandoffTest').latestVersion,
  })), { oldest: 'C', middle: 'C', current: 'C' });

  const expectedShape = {
    frozen: true,
    properties: [
      {
        name: 'name', kind: 'data', valueType: 'string', writable: false,
        enumerable: true, configurable: false,
      },
      {
        name: 'version', kind: 'accessor', getType: 'function', setType: 'undefined',
        enumerable: true, configurable: false,
      },
      {
        name: 'latestVersion', kind: 'accessor', getType: 'function', setType: 'undefined',
        enumerable: true, configurable: false,
      },
      {
        name: 'versionedUrl', kind: 'data', valueType: 'function', writable: false,
        enumerable: true, configurable: false,
      },
      {
        name: 'checkNow', kind: 'data', valueType: 'function', writable: false,
        enumerable: true, configurable: false,
      },
      {
        name: 'state', kind: 'data', valueType: 'function', writable: false,
        enumerable: true, configurable: false,
      },
    ],
  };
  const shapesAfter = await page.evaluate(() => ({
    oldest: window.__retainedHandleShape(window.__oldestHandle),
    middle: window.__retainedHandleShape(window.__middleHandle),
  }));
  assert.deepEqual(shapesAfter.oldest, expectedShape);
  assert.deepEqual(shapesAfter.middle, expectedShape);
  assert.deepEqual(shapesAfter.oldest, await page.evaluate(() => window.__oldestHandleShape));
  assert.deepEqual(shapesAfter.middle, await page.evaluate(() => window.__middleHandleShape));

  // The request started by the 2.4.3 closure may finally finish, but its
  // deactivation latch must keep that stale response out of the live state.
  heldFirstResponse.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
  heldFirstResponse.end('STALE');
  heldFirstResponse = undefined;
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(await page.evaluate(() => window.__oldestHandle.latestVersion), 'C');
});

test('a 2.4.6+ createElement wrapper retained before handoff delegates to the newest manager', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  const attributes = {
    'data-name': 'CapturedCreateElementHandoff',
    'data-mode': 'off',
    'data-boot-version': 'CAPTURED',
    'data-asset-patterns': '/captured-assets/',
  };

  await injectConfiguredRuntime(page, runtime, attributes);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.kitVersion === '2.4.6'
      && window.JellyfinRefreshKit.state().interceptorInstalled === true
  ));
  await page.evaluate(() => { window.__retainedCreateElement = document.createElement; });

  await injectConfiguredRuntime(page, runtimeAtVersion('2.4.7'), attributes);
  await page.waitForFunction(() => window.JellyfinRefreshKit?.kitVersion === '2.4.7');
  await injectConfiguredRuntime(page, runtimeAtVersion('2.4.8'), attributes);
  await page.waitForFunction(() => window.JellyfinRefreshKit?.kitVersion === '2.4.8');

  const urls = await page.evaluate(() => {
    const retainedScript = window.__retainedCreateElement.call(document, 'script');
    retainedScript.src = '/captured-assets/from-retained.js';
    const retainedLink = window.__retainedCreateElement.call(document, 'link');
    retainedLink.setAttribute('href', '/captured-assets/from-retained.css');
    const currentScript = document.createElement('script');
    currentScript.src = '/captured-assets/from-current.js';
    return {
      retainedScript: retainedScript.getAttribute('src'),
      retainedLink: retainedLink.getAttribute('href'),
      currentScript: currentScript.getAttribute('src'),
    };
  });
  assert.deepEqual(urls, {
    retainedScript: '/captured-assets/from-retained.js?v=CAPTURED',
    retainedLink: '/captured-assets/from-retained.css?v=CAPTURED',
    currentScript: '/captured-assets/from-current.js?v=CAPTURED',
  });
});

test('the exact released 2.4.2 retained wrapper stays inert after a 2.4.6 handoff', async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  const attributes = {
    'data-name': 'HistoricalCapturedCreateElement',
    'data-mode': 'off',
    'data-boot-version': 'CAPTURED',
    'data-asset-patterns': '/captured-assets/',
  };

  await injectConfiguredRuntime(page, historicalRuntime242, attributes);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.kitVersion === '2.4.2'
      && window.JellyfinRefreshKit.state().interceptorInstalled === true
  ));
  await page.evaluate(() => {
    window.__historicalCreateElement = document.createElement;
    window.__historicalPreHandoff = document.createElement('script');
  });

  await injectConfiguredRuntime(page, runtime, attributes);
  await page.waitForFunction(() => window.JellyfinRefreshKit?.kitVersion === '2.4.6');

  const observed = await page.evaluate(() => {
    const retained = window.__historicalCreateElement.call(document, 'script');
    retained.src = '/captured-assets/from-retained-2.4.2.js';
    window.__historicalPreHandoff.src = '/captured-assets/from-pre-handoff.js';
    const current = document.createElement('script');
    current.src = '/captured-assets/from-current.js';
    return {
      retained: retained.getAttribute('src'),
      preHandoff: window.__historicalPreHandoff.getAttribute('src'),
      current: current.getAttribute('src'),
      lineage: window.JellyfinRefreshKit.state().shared.managerLineage,
    };
  });
  assert.deepEqual(observed, {
    retained: '/captured-assets/from-retained-2.4.2.js',
    preHandoff: '/captured-assets/from-pre-handoff.js?v=CAPTURED',
    current: '/captured-assets/from-current.js?v=CAPTURED',
    lineage: ['2.4.2', '2.4.6'],
  });
});

test('getVersion callback identities use the same bounds as HTTP identities', async (t) => {
  const browser = await openBrowser(t);

  async function runRejectedValue(name, value) {
    const page = await browser.newPage();
    await page.goto('about:blank');
    await page.evaluate(({ instanceName, callbackValue }) => {
      window.__getVersionReturned = false;
      window.JellyfinRefreshKitConfig = {
        name: instanceName,
        mode: 'off',
        getVersion() {
          window.__getVersionReturned = true;
          return callbackValue;
        },
      };
    }, { instanceName: name, callbackValue: value });
    await injectRuntime(page);
    await page.waitForFunction((instanceName) => (
      window.__getVersionReturned
      && window.JellyfinRefreshKit?.get(instanceName) !== undefined
    ), { timeout: 5000 }, name);
    await new Promise((resolve) => setTimeout(resolve, 50));
    const observed = await page.evaluate((instanceName) => {
      const api = window.JellyfinRefreshKit.get(instanceName);
      return {
        version: api.version,
        forcedUrl: api.versionedUrl('/audit.js', true),
      };
    }, name);
    assert.deepEqual(observed, { version: null, forcedUrl: '/audit.js' });
    await page.close();
  }

  async function runRejectedJsonValue(name, value) {
    const page = await browser.newPage();
    await page.goto('about:blank');
    await page.evaluate(({ instanceName, fieldValue }) => {
      window.__jsonVersionCalls = 0;
      window.fetch = () => {
        window.__jsonVersionCalls += 1;
        return Promise.resolve(new Response(JSON.stringify({ CacheKey: fieldValue }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        }));
      };
      window.JellyfinRefreshKitConfig = {
        name: instanceName,
        mode: 'off',
        versionUrl: '/version',
        versionJsonField: 'CacheKey',
        pollSeconds: 3600,
      };
    }, { instanceName: name, fieldValue: value });
    await injectRuntime(page);
    await page.waitForFunction((instanceName) => (
      window.__jsonVersionCalls > 0
      && window.JellyfinRefreshKit?.get(instanceName) !== undefined
    ), { timeout: 5000 }, name);
    await new Promise((resolve) => setTimeout(resolve, 50));
    assert.deepEqual(await page.evaluate((instanceName) => {
      const api = window.JellyfinRefreshKit.get(instanceName);
      return { version: api.version, forcedUrl: api.versionedUrl('/audit.js', true) };
    }, name), { version: null, forcedUrl: '/audit.js' });
    await page.close();
  }

  await runRejectedValue('CallbackLengthBoundTest', 'V'.repeat(2_000_000));
  await runRejectedValue('CallbackHtmlBoundTest', '<html>proxy error</html>');
  await runRejectedValue('CallbackObjectTypeTest', { version: 'OBJECT' });
  await runRejectedValue('CallbackNumberTypeTest', 2046);
  await runRejectedJsonValue('JsonObjectTypeTest', { version: 'OBJECT' });
  await runRejectedJsonValue('JsonNumberTypeTest', 2046);
});

test('version endpoint cache stamps remain in the query before fragments', async (t) => {
  const requests = [];
  const origin = await startServer(t, (req, res) => {
    requests.push(req.url);
    if (req.url.startsWith('/version')) {
      res.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
      res.end('A');
      return;
    }
    serveHtml(res);
  });
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  await page.evaluate((base) => {
    window.JellyfinRefreshKitConfig = {
      name: 'FragmentStampTest',
      mode: 'off',
      versionUrl: `${base}/version?seed=1#client-only`,
    };
  }, origin);
  await injectRuntime(page);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('FragmentStampTest')?.version === 'A'
  ));

  const versionRequest = requests.find((url) => url.startsWith('/version'));
  assert.ok(versionRequest, 'version endpoint was requested');
  const parsed = new URL(versionRequest, origin);
  assert.equal(parsed.searchParams.get('seed'), '1');
  assert.match(parsed.searchParams.get('_'), /^\d+$/);
  assert.equal(parsed.hash, '');

  const assetUrl = await page.evaluate(() => (
    window.JellyfinRefreshKit.get('FragmentStampTest')
      .versionedUrl('/asset.js?kind=entry#client-only', true)
  ));
  assert.equal(assetUrl, '/asset.js?kind=entry&v=A#client-only');
});

test('oversized chunked version responses are cancelled without unbounded buffering', async (t) => {
  let versionBytesSent = 0;
  let resolveVersionClosed;
  const versionClosed = new Promise((resolve) => { resolveVersionClosed = resolve; });
  const origin = await startServer(t, (req, res) => {
    if (!req.url.startsWith('/version')) {
      serveHtml(res);
      return;
    }

    res.writeHead(200, {
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
    });
    const chunk = Buffer.alloc(256, 'v');
    const timer = setInterval(() => {
      if (res.destroyed) return;
      versionBytesSent += chunk.length;
      res.write(chunk);
    }, 3);
    res.once('close', () => {
      clearInterval(timer);
      resolveVersionClosed();
    });
  });
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  await page.evaluate((base) => {
    window.JellyfinRefreshKitConfig = {
      name: 'BoundedVersionResponseTest',
      mode: 'off',
      versionUrl: `${base}/version`,
      pollSeconds: 3600,
    };
  }, origin);
  await injectRuntime(page);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('BoundedVersionResponseTest')
  ));

  await Promise.race([
    versionClosed,
    new Promise((_, reject) => setTimeout(
      () => reject(new Error('browser did not cancel the oversized version response')),
      5000,
    )),
  ]);

  const state = await page.evaluate(() => (
    window.JellyfinRefreshKit.get('BoundedVersionResponseTest').state()
  ));
  assert.equal(state.version, null);
  assert.ok(
    versionBytesSent < 32768,
    `oversized response was not stopped promptly (${versionBytesSent} bytes sent)`,
  );
});

test('streamed gzip versions are bounded by decoded bytes', async (t) => {
  const smallBody = zlib.gzipSync(Buffer.from('G-1'));
  const oversizedBody = zlib.gzipSync(Buffer.alloc(5000, 'x'));
  const origin = await startServer(t, (req, res) => {
    if (!req.url.startsWith('/version')) {
      serveHtml(res);
      return;
    }
    const body = req.url.includes('oversized') ? oversizedBody : smallBody;
    res.writeHead(200, {
      'Content-Type': 'text/plain; charset=utf-8',
      'Content-Encoding': 'gzip',
      'Content-Length': String(body.length),
      'Cache-Control': 'no-store',
    });
    res.end(body);
  });
  const browser = await openBrowser(t);

  const smallPage = await browser.newPage();
  await smallPage.goto(`${origin}/`);
  await smallPage.evaluate((base) => {
    window.JellyfinRefreshKitConfig = {
      name: 'SmallGzipVersionTest',
      mode: 'off',
      versionUrl: `${base}/version-small`,
    };
  }, origin);
  await injectRuntime(smallPage);
  await smallPage.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('SmallGzipVersionTest')?.version === 'G-1'
  ));

  const oversizedPage = await browser.newPage();
  await oversizedPage.goto(`${origin}/`);
  await oversizedPage.evaluate((base) => {
    window.JellyfinRefreshKitConfig = {
      name: 'OversizedGzipVersionTest',
      mode: 'off',
      versionUrl: `${base}/version-oversized`,
    };
  }, origin);
  await injectRuntime(oversizedPage);
  await oversizedPage.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('OversizedGzipVersionTest')?.state().lastFetchAt > 0
  ));
  await new Promise((resolve) => setTimeout(resolve, 100));
  assert.equal(await oversizedPage.evaluate(() => (
    window.JellyfinRefreshKit.get('OversizedGzipVersionTest').version
  )), null);
  assert.ok(oversizedBody.length < 4096, 'fixture must prove compressed length is not the cap');
});

test('non-success version responses are aborted without draining an endless body', async (t) => {
  let versionBytesSent = 0;
  let resolveVersionClosed;
  const versionClosed = new Promise((resolve) => { resolveVersionClosed = resolve; });
  const origin = await startServer(t, (req, res) => {
    if (!req.url.startsWith('/version')) {
      serveHtml(res);
      return;
    }

    res.writeHead(500, {
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
    });
    const chunk = Buffer.alloc(256, 'e');
    const timer = setInterval(() => {
      if (res.destroyed) return;
      versionBytesSent += chunk.length;
      res.write(chunk);
    }, 3);
    res.once('close', () => {
      clearInterval(timer);
      resolveVersionClosed();
    });
  });
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  await page.evaluate((base) => {
    window.JellyfinRefreshKitConfig = {
      name: 'ErrorVersionResponseTest',
      mode: 'off',
      versionUrl: `${base}/version`,
      pollSeconds: 3600,
    };
  }, origin);
  await injectRuntime(page);

  await Promise.race([
    versionClosed,
    new Promise((_, reject) => setTimeout(
      () => reject(new Error('browser did not cancel the non-success version response')),
      5000,
    )),
  ]);

  const state = await page.evaluate(() => (
    window.JellyfinRefreshKit.get('ErrorVersionResponseTest').state()
  ));
  assert.equal(state.version, null);
  assert.ok(
    versionBytesSent < 32768,
    `non-success response was not stopped promptly (${versionBytesSent} bytes sent)`,
  );
});

test('legacy response fallback refuses compressed bodies before calling text', async (t) => {
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto('about:blank');
  await page.evaluate(() => {
    window.__legacyTextCalled = false;
    window.__legacyFetchAborted = false;
    window.fetch = (_url, init) => {
      init.signal?.addEventListener('abort', () => {
        window.__legacyFetchAborted = true;
      }, { once: true });
      return Promise.resolve({
        ok: true,
        status: 200,
        body: null,
        headers: {
          get(name) {
            if (name.toLowerCase() === 'content-length') return '32';
            if (name.toLowerCase() === 'content-encoding') return 'gzip';
            return null;
          },
        },
        text() {
          window.__legacyTextCalled = true;
          return Promise.resolve('x'.repeat(1024 * 1024));
        },
      });
    };
    window.JellyfinRefreshKitConfig = {
      name: 'CompressedLegacyFallbackTest',
      mode: 'off',
      versionUrl: '/version',
      pollSeconds: 3600,
    };
  });
  await injectRuntime(page);
  await page.waitForFunction(() => window.__legacyFetchAborted === true);

  const observed = await page.evaluate(() => ({
    textCalled: window.__legacyTextCalled,
    version: window.JellyfinRefreshKit.get('CompressedLegacyFallbackTest').version,
  }));
  assert.equal(observed.textCalled, false);
  assert.equal(observed.version, null);
});

test('cancellable response bodies are released without AbortController', async (t) => {
  const browser = await openBrowser(t);
  async function runCase(name, status, declaredLength) {
    const page = await browser.newPage();
    await page.goto('about:blank');
    await page.evaluate(({ instanceName, responseStatus, contentLength }) => {
      Object.defineProperty(window, 'AbortController', {
        value: undefined,
        configurable: true,
      });
      window.__legacyBodyCancellations = 0;
      window.fetch = () => Promise.resolve({
        ok: responseStatus >= 200 && responseStatus < 300,
        status: responseStatus,
        body: {
          locked: false,
          cancel() {
            window.__legacyBodyCancellations += 1;
            return Promise.resolve();
          },
        },
        headers: {
          get(headerName) {
            return headerName.toLowerCase() === 'content-length' ? contentLength : null;
          },
        },
      });
      window.JellyfinRefreshKitConfig = {
        name: instanceName,
        mode: 'off',
        versionUrl: '/version',
      };
    }, { instanceName: name, responseStatus: status, contentLength: declaredLength });
    await injectRuntime(page);
    await page.waitForFunction(() => window.__legacyBodyCancellations === 1);
    assert.equal(await page.evaluate((instanceName) => (
      window.JellyfinRefreshKit.get(instanceName).version
    ), name), null);
  }

  await runCase('LegacyErrorCancellationTest', 500, null);
  await runCase('LegacyOversizedCancellationTest', 200, '4097');
});

test('legacy stream timeout cancels its locked reader without AbortController', async (t) => {
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto('about:blank');
  await page.evaluate(() => {
    Object.defineProperty(window, 'AbortController', {
      value: undefined,
      configurable: true,
    });
    window.__legacyReaderCancellations = 0;
    window.fetch = () => Promise.resolve({
      ok: true,
      status: 200,
      body: {
        getReader() {
          return {
            read() { return new Promise(() => {}); },
            cancel() {
              window.__legacyReaderCancellations += 1;
              return Promise.resolve();
            },
          };
        },
      },
      headers: { get() { return null; } },
    });
    window.JellyfinRefreshKitConfig = {
      name: 'LegacyTimeoutCancellationTest',
      mode: 'off',
      versionUrl: '/version',
    };
  });
  const fastRuntime = runtime.replace(
    'var VERSION_FETCH_TIMEOUT_MS = 10000;',
    'var VERSION_FETCH_TIMEOUT_MS = 75;',
  );
  await injectRuntime(page, fastRuntime);
  await page.waitForFunction(() => window.__legacyReaderCancellations === 1);
  assert.equal(await page.evaluate(() => (
    window.JellyfinRefreshKit.get('LegacyTimeoutCancellationTest').version
  )), null);
});

test('legacy timeout rejects late headers without starting a body reader', async (t) => {
  const browser = await openBrowser(t);
  const fastRuntime = runtime.replace(
    'var VERSION_FETCH_TIMEOUT_MS = 10000;',
    'var VERSION_FETCH_TIMEOUT_MS = 75;',
  );

  const streamPage = await browser.newPage();
  await streamPage.goto('about:blank');
  await streamPage.evaluate(() => {
    Object.defineProperty(window, 'AbortController', {
      value: undefined,
      configurable: true,
    });
    window.__lateHeaderBodyCancels = 0;
    window.__lateHeaderReads = 0;
    window.fetch = () => new Promise((resolve) => {
      setTimeout(() => resolve({
        ok: true,
        status: 200,
        body: {
          locked: false,
          cancel() {
            window.__lateHeaderBodyCancels += 1;
            return Promise.resolve();
          },
          getReader() {
            window.__lateHeaderReads += 1;
            return {
              read() { return new Promise(() => {}); },
              cancel() { return Promise.resolve(); },
            };
          },
        },
        headers: { get() { return null; } },
      }), 150);
    });
    window.JellyfinRefreshKitConfig = {
      name: 'LateHeaderStreamTest',
      mode: 'off',
      versionUrl: '/version',
    };
  });
  await injectRuntime(streamPage, fastRuntime);
  await streamPage.waitForFunction(() => window.__lateHeaderBodyCancels === 1);
  assert.equal(await streamPage.evaluate(() => window.__lateHeaderReads), 0);

  const textPage = await browser.newPage();
  await textPage.goto('about:blank');
  await textPage.evaluate(() => {
    Object.defineProperty(window, 'AbortController', {
      value: undefined,
      configurable: true,
    });
    window.__lateHeaderTextCalls = 0;
    window.__lateHeaderResolved = false;
    window.fetch = () => new Promise((resolve) => {
      setTimeout(() => {
        window.__lateHeaderResolved = true;
        resolve({
          ok: true,
          status: 200,
          body: null,
          headers: {
            get(name) {
              if (name.toLowerCase() === 'content-length') return '3';
              if (name.toLowerCase() === 'content-encoding') return 'identity';
              return null;
            },
          },
          text() {
            window.__lateHeaderTextCalls += 1;
            return Promise.resolve('A-1');
          },
        });
      }, 150);
    });
    window.JellyfinRefreshKitConfig = {
      name: 'LateHeaderTextTest',
      mode: 'off',
      versionUrl: '/version',
    };
  });
  await injectRuntime(textPage, fastRuntime);
  await textPage.waitForFunction(() => window.__lateHeaderResolved === true);
  await new Promise((resolve) => setTimeout(resolve, 50));
  assert.equal(await textPage.evaluate(() => window.__lateHeaderTextCalls), 0);
});

test('legacy bounded readers preserve successful identity and UTF-8 responses', async (t) => {
  const browser = await openBrowser(t);

  const textPage = await browser.newPage();
  await textPage.goto('about:blank');
  await textPage.evaluate(() => {
    window.fetch = () => Promise.resolve({
      ok: true,
      status: 200,
      body: null,
      headers: {
        get(name) {
          if (name.toLowerCase() === 'content-length') return '3';
          if (name.toLowerCase() === 'content-encoding') return 'identity';
          return null;
        },
      },
      text() { return Promise.resolve('A-1'); },
    });
    window.JellyfinRefreshKitConfig = {
      name: 'LegacyIdentityTextTest',
      mode: 'off',
      versionUrl: '/version',
    };
  });
  await injectRuntime(textPage);
  await textPage.waitForFunction(() => (
    window.JellyfinRefreshKit.get('LegacyIdentityTextTest').version === 'A-1'
  ));

  const bytesPage = await browser.newPage();
  await bytesPage.goto('about:blank');
  await bytesPage.evaluate(() => {
    Object.defineProperty(window, 'TextDecoder', {
      value: undefined,
      configurable: true,
    });
    const chunks = [new Uint8Array([0xc3]), new Uint8Array([0xa9, 0x2d, 0x41])];
    window.fetch = () => Promise.resolve({
      ok: true,
      status: 200,
      body: {
        getReader() {
          return {
            read() {
              return Promise.resolve(chunks.length
                ? { done: false, value: chunks.shift() }
                : { done: true, value: undefined });
            },
            cancel() { return Promise.resolve(); },
          };
        },
      },
      headers: { get() { return null; } },
    });
    window.JellyfinRefreshKitConfig = {
      name: 'LegacyUtf8BytesTest',
      mode: 'off',
      versionUrl: '/version',
    };
  });
  await injectRuntime(bytesPage);
  await bytesPage.waitForFunction(() => (
    window.JellyfinRefreshKit.get('LegacyUtf8BytesTest').version === '\u00e9-A'
  ));
});

test('invalid UTF-8 version streams are rejected and cancelled promptly', async (t) => {
  let resolveVersionClosed;
  const versionClosed = new Promise((resolve) => { resolveVersionClosed = resolve; });
  const origin = await startServer(t, (req, res) => {
    if (!req.url.startsWith('/version')) {
      serveHtml(res);
      return;
    }

    res.writeHead(200, {
      'Content-Type': 'text/plain; charset=utf-8',
      'Cache-Control': 'no-store',
    });
    res.write(Buffer.from([0xc3, 0x28]));
    const timer = setInterval(() => {
      if (!res.destroyed) res.write(Buffer.alloc(64, 'x'));
    }, 10);
    res.once('close', () => {
      clearInterval(timer);
      resolveVersionClosed();
    });
  });
  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  await page.evaluate((base) => {
    window.JellyfinRefreshKitConfig = {
      name: 'InvalidUtf8VersionTest',
      mode: 'off',
      versionUrl: `${base}/version`,
      pollSeconds: 3600,
    };
  }, origin);
  await injectRuntime(page);

  await Promise.race([
    versionClosed,
    new Promise((_, reject) => setTimeout(
      () => reject(new Error('browser did not cancel the invalid UTF-8 response')),
      5000,
    )),
  ]);
  assert.equal(await page.evaluate(() => (
    window.JellyfinRefreshKit.get('InvalidUtf8VersionTest').version
  )), null);
});

test('a same-origin opener clone gets fresh tab history, then preserves it on reload', {
  timeout: 15000,
}, async (t) => {
  const origin = await startServer(t, (req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const opener = await browser.newPage();
  await opener.goto(`${origin}/opener`);
  await opener.evaluate(() => {
    window.JellyfinRefreshKitConfig = { name: 'OpenerTest', mode: 'off' };
  });
  await injectRuntime(opener);
  await opener.waitForFunction(() => window.JellyfinRefreshKit?.get('OpenerTest'));

  const openerState = await opener.evaluate((keys) => {
    sessionStorage.setItem(keys.flips, JSON.stringify(['CloneTest|A>B']));
    sessionStorage.setItem(keys.left, JSON.stringify(['CloneTest|A']));
    sessionStorage.setItem(keys.epochs, JSON.stringify([['CloneTest', 'opener-epoch']]));
    sessionStorage.setItem(keys.gaps, JSON.stringify([['CloneTest', 'A']]));
    sessionStorage.setItem(keys.recovery, JSON.stringify(['boot-seed|CloneTest|A']));
    sessionStorage.setItem(keys.budget, JSON.stringify([111]));
    localStorage.setItem(keys.budget, JSON.stringify([222]));
    return {
      token: sessionStorage.getItem(keys.tab),
      left: sessionStorage.getItem(keys.left),
      epochs: sessionStorage.getItem(keys.epochs),
      gaps: sessionStorage.getItem(keys.gaps),
    };
  }, storageKeys);
  assert.ok(openerState.token);

  const popupPromise = new Promise((resolve) => opener.once('popup', resolve));
  await opener.evaluate((url) => { window.open(url, 'refresh-kit-clone-test'); }, `${origin}/child`);
  const child = await popupPromise;
  await child.waitForFunction(() => location.pathname === '/child');

  const inherited = await child.evaluate((keys) => ({
    token: sessionStorage.getItem(keys.tab),
    left: sessionStorage.getItem(keys.left),
    epochs: sessionStorage.getItem(keys.epochs),
    gaps: sessionStorage.getItem(keys.gaps),
  }), storageKeys);
  assert.equal(inherited.token, openerState.token);
  assert.equal(inherited.left, openerState.left);
  assert.equal(inherited.epochs, openerState.epochs);
  assert.equal(inherited.gaps, openerState.gaps);

  await child.evaluate(() => {
    window.JellyfinRefreshKitConfig = { name: 'CloneTest', mode: 'off' };
  });
  await injectRuntime(child);
  await child.waitForFunction(() => window.JellyfinRefreshKit?.get('CloneTest'));

  const freshChild = await child.evaluate((keys) => ({
    token: sessionStorage.getItem(keys.tab),
    flips: sessionStorage.getItem(keys.flips),
    left: sessionStorage.getItem(keys.left),
    epochs: sessionStorage.getItem(keys.epochs),
    gaps: sessionStorage.getItem(keys.gaps),
    recovery: sessionStorage.getItem(keys.recovery),
    sessionBudget: sessionStorage.getItem(keys.budget),
    sharedBudget: localStorage.getItem(keys.budget),
  }), storageKeys);
  assert.ok(freshChild.token);
  assert.notEqual(freshChild.token, openerState.token);
  assert.equal(freshChild.flips, null);
  assert.equal(freshChild.left, null);
  assert.equal(freshChild.epochs, null);
  assert.equal(freshChild.gaps, null);
  assert.equal(freshChild.recovery, null);
  assert.equal(freshChild.sessionBudget, null);
  assert.equal(freshChild.sharedBudget, JSON.stringify([222]));

  const childToken = freshChild.token;
  await child.evaluate((keys) => {
    sessionStorage.setItem(keys.left, JSON.stringify(['CloneTest|fresh']));
    sessionStorage.setItem(keys.epochs, JSON.stringify([['CloneTest', 'fresh-child-epoch']]));
    sessionStorage.setItem(keys.gaps, JSON.stringify([['CloneTest', 'fresh']]));
    sessionStorage.setItem(keys.budget, JSON.stringify([333]));
  }, storageKeys);
  await child.reload({ waitUntil: 'load' });
  assert.equal(await child.evaluate(() => Boolean(window.opener)), true);
  await child.evaluate(() => {
    window.JellyfinRefreshKitConfig = { name: 'CloneReloadTest', mode: 'off' };
  });
  await injectRuntime(child);
  await child.waitForFunction(() => window.JellyfinRefreshKit?.get('CloneReloadTest'));

  const reloadedChild = await child.evaluate((keys) => ({
    token: sessionStorage.getItem(keys.tab),
    left: sessionStorage.getItem(keys.left),
    epochs: sessionStorage.getItem(keys.epochs),
    gaps: sessionStorage.getItem(keys.gaps),
    sessionBudget: sessionStorage.getItem(keys.budget),
  }), storageKeys);
  assert.equal(reloadedChild.token, childToken);
  assert.equal(reloadedChild.left, JSON.stringify(['CloneTest|fresh']));
  assert.equal(
    reloadedChild.epochs,
    JSON.stringify([['CloneTest', 'fresh-child-epoch']]),
  );
  assert.equal(reloadedChild.gaps, JSON.stringify([['CloneTest', 'fresh']]));
  assert.equal(reloadedChild.sessionBudget, JSON.stringify([333]));
});

test('an opener clone keeps copied safety history unless its distinct tab ID write is verified', {
  timeout: 15000,
}, async (t) => {
  const origin = await startServer(t, (_req, res) => serveHtml(res));
  const browser = await openBrowser(t);
  const opener = await browser.newPage();
  await opener.goto(`${origin}/opener-noop`);
  await opener.evaluate(() => {
    window.JellyfinRefreshKitConfig = { name: 'OpenerNoopParent', mode: 'off' };
  });
  await injectRuntime(opener);
  await opener.waitForFunction(() => window.JellyfinRefreshKit?.get('OpenerNoopParent'));

  const parentState = await opener.evaluate((keys) => {
    const values = {
      flips: JSON.stringify(['Copied|A>B']),
      left: JSON.stringify(['Copied|A']),
      epochs: JSON.stringify([['Copied', 'copied-epoch']]),
      gaps: JSON.stringify([['Copied', 'A']]),
      recovery: JSON.stringify(['copied-recovery']),
      budget: JSON.stringify([123]),
    };
    sessionStorage.setItem(keys.flips, values.flips);
    sessionStorage.setItem(keys.left, values.left);
    sessionStorage.setItem(keys.epochs, values.epochs);
    sessionStorage.setItem(keys.gaps, values.gaps);
    sessionStorage.setItem(keys.recovery, values.recovery);
    sessionStorage.setItem(keys.budget, values.budget);
    return { token: sessionStorage.getItem(keys.tab), values };
  }, storageKeys);
  assert.ok(parentState.token);

  const popupPromise = new Promise((resolve) => opener.once('popup', resolve));
  await opener.evaluate((url) => { window.open(url, 'refresh-kit-clone-noop'); }, `${origin}/child-noop`);
  const child = await popupPromise;
  await child.waitForFunction(() => location.pathname === '/child-noop');
  await child.evaluate((keys) => {
    const nativeSet = Storage.prototype.setItem;
    Storage.prototype.setItem = function setItem(key, value) {
      if (this === sessionStorage && key === keys.tab) return undefined;
      return nativeSet.call(this, key, value);
    };
    window.JellyfinRefreshKitConfig = { name: 'OpenerNoopChild', mode: 'off' };
  }, storageKeys);
  await injectRuntime(child);
  await child.waitForFunction(() => window.JellyfinRefreshKit?.get('OpenerNoopChild'));

  const retained = await child.evaluate((keys) => ({
    token: sessionStorage.getItem(keys.tab),
    flips: sessionStorage.getItem(keys.flips),
    left: sessionStorage.getItem(keys.left),
    epochs: sessionStorage.getItem(keys.epochs),
    gaps: sessionStorage.getItem(keys.gaps),
    recovery: sessionStorage.getItem(keys.recovery),
    budget: sessionStorage.getItem(keys.budget),
  }), storageKeys);
  assert.equal(retained.token, parentState.token,
    'the shim left the copied opener identity in place');
  assert.deepEqual({
    flips: retained.flips,
    left: retained.left,
    epochs: retained.epochs,
    gaps: retained.gaps,
    recovery: retained.recovery,
    budget: retained.budget,
  }, parentState.values, 'no copied safety evidence is destroyed before ID verification');
});

test('a timed-out bootstrap entry stops later entries and reports a terminal failure', {
  timeout: 10000,
}, async (t) => {
  let hangingResponse;
  let hangingRequests = 0;
  let laterRequests = 0;
  const origin = await startServer(t, (req, res) => {
    const pathname = new URL(req.url, 'http://runtime.test').pathname;
    if (pathname === '/version') {
      res.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
      res.end('A');
      return;
    }
    if (pathname === '/hang.js') {
      hangingRequests += 1;
      hangingResponse = res;
      return;
    }
    if (pathname === '/later.js') {
      laterRequests += 1;
      res.writeHead(200, { 'Content-Type': 'text/javascript' });
      res.end('window.__entryOrder = (window.__entryOrder || []).concat("later");');
      return;
    }
    serveHtml(res);
  });

  const fastRuntime = runtime.replace(
    'var ENTRY_LOAD_TIMEOUT_MS = 30000;',
    'var ENTRY_LOAD_TIMEOUT_MS = 150;',
  );
  assert.notEqual(fastRuntime, runtime, 'test runtime timeout was accelerated');

  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  await page.evaluate((base) => {
    window.__entryOrder = [];
    window.JellyfinRefreshKitConfig = {
      name: 'EntryTimeoutTest',
      mode: 'off',
      versionUrl: `${base}/version`,
      entryScripts: [`${base}/hang.js`, `${base}/later.js`],
    };
  }, origin);
  await injectRuntime(page, fastRuntime);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('EntryTimeoutTest')?.state().entryTimedOut === true
  ));

  const timedOut = await page.evaluate(() => (
    window.JellyfinRefreshKit.get('EntryTimeoutTest').state()
  ));
  assert.equal(timedOut.entriesLoaded, true);
  assert.equal(timedOut.entryLoadTimeoutMs, 150);
  assert.equal(timedOut.entryTimedOutUrl, `${origin}/hang.js`);
  assert.deepEqual(timedOut.entryFailures, [
    { url: `${origin}/hang.js`, reason: 'timeout' },
  ]);
  assert.equal(hangingRequests, 1);
  assert.equal(laterRequests, 0);
  assert.deepEqual(await page.evaluate(() => window.__entryOrder), []);

  assert.ok(hangingResponse, 'the hanging entry reached the server');
  hangingResponse.writeHead(200, { 'Content-Type': 'text/javascript' });
  hangingResponse.end('window.__entryOrder = (window.__entryOrder || []).concat("late-hang");');
  await new Promise((resolve) => setTimeout(resolve, 250));

  const afterLateResponse = await page.evaluate(() => (
    window.JellyfinRefreshKit.get('EntryTimeoutTest').state()
  ));
  assert.deepEqual(afterLateResponse.entryFailures, timedOut.entryFailures);
  assert.equal(afterLateResponse.entryTimedOutUrl, timedOut.entryTimedOutUrl);
  assert.equal(laterRequests, 0);
  assert.equal((await page.evaluate(() => window.__entryOrder)).includes('later'), false);
});

test('an ordinary bootstrap load error remains fail-open for later entries', async (t) => {
  let laterRequests = 0;
  const origin = await startServer(t, (req, res) => {
    const pathname = new URL(req.url, 'http://runtime.test').pathname;
    if (pathname === '/version') {
      res.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
      res.end('A');
      return;
    }
    if (pathname === '/missing.js') {
      res.writeHead(404, { 'Content-Type': 'text/plain' });
      res.end('missing');
      return;
    }
    if (pathname === '/later.js') {
      laterRequests += 1;
      res.writeHead(200, { 'Content-Type': 'text/javascript' });
      res.end('window.__entryOrder = (window.__entryOrder || []).concat("later");');
      return;
    }
    serveHtml(res);
  });

  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  await page.evaluate((base) => {
    window.__entryOrder = [];
    window.JellyfinRefreshKitConfig = {
      name: 'EntryErrorTest',
      mode: 'off',
      versionUrl: `${base}/version`,
      entryScripts: [`${base}/missing.js`, `${base}/later.js`],
    };
  }, origin);
  await injectRuntime(page);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('EntryErrorTest')?.state().entriesLoaded === true
      && window.__entryOrder?.includes('later')
  ));

  const state = await page.evaluate(() => (
    window.JellyfinRefreshKit.get('EntryErrorTest').state()
  ));
  assert.equal(state.entryTimedOut, false);
  assert.equal(state.entryTimedOutUrl, null);
  assert.deepEqual(state.entryFailures, [
    { url: `${origin}/missing.js`, reason: 'load_error' },
  ]);
  assert.equal(laterRequests, 1);
  assert.deepEqual(await page.evaluate(() => window.__entryOrder), ['later']);
});

test('path-ending mjs entries load as modules with ordered synchronous graphs', async (t) => {
  const requests = [];
  const origin = await startServer(t, (req, res) => {
    const requestUrl = new URL(req.url, 'http://runtime.test');
    requests.push(`${requestUrl.pathname}${requestUrl.search}`);
    if (requestUrl.pathname === '/version') {
      res.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
      res.end('A');
      return;
    }
    if (requestUrl.pathname === '/main.MJS') {
      res.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'no-store' });
      res.end('import "./dependency.mjs"; window.__entryOrder.push("main");');
      return;
    }
    if (requestUrl.pathname === '/dependency.mjs') {
      res.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'no-store' });
      res.end('window.__entryOrder.push("dependency"); export const ready = true;');
      return;
    }
    if (requestUrl.pathname === '/after.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'no-store' });
      res.end('window.__entryOrder.push("after");');
      return;
    }
    if (requestUrl.pathname === '/classic-loader') {
      res.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'no-store' });
      const label = requestUrl.searchParams.get('file')?.endsWith('.css')
        ? 'query-css-classic' : 'query-classic';
      res.end(`window.__entryOrder.push(${JSON.stringify(label)});`);
      return;
    }
    serveHtml(res);
  });

  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  await page.evaluate((base) => {
    window.__entryOrder = [];
    window.JellyfinRefreshKitConfig = {
      name: 'ModuleEntryTest',
      mode: 'off',
      versionUrl: `${base}/version`,
      entryScripts: [
        `${base}/main.MJS?flavor=x#fragment`,
        `${base}/after.js`,
        `${base}/classic-loader?file=not-an-entry.mjs`,
        `${base}/classic-loader?file=not-an-entry.css`,
      ],
    };
  }, origin);
  await injectRuntime(page);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('ModuleEntryTest')?.state().entriesLoaded === true
      && window.__entryOrder?.includes('query-classic')
  ));

  const observed = await page.evaluate((base) => ({
    order: window.__entryOrder,
    state: window.JellyfinRefreshKit.get('ModuleEntryTest').state(),
    entries: [...document.querySelectorAll(`script[src^="${base}"]`)].map((entry) => ({
      src: entry.src,
      type: entry.type,
      async: entry.async,
    })),
  }), origin);
  assert.deepEqual(observed.order, [
    'dependency', 'main', 'after', 'query-classic', 'query-css-classic',
  ]);
  assert.deepEqual(observed.state.entryFailures, []);
  assert.equal(observed.entries[0].src, `${origin}/main.MJS?flavor=x&v=A#fragment`);
  assert.equal(observed.entries[0].type, 'module');
  assert.equal(observed.entries[0].async, false);
  assert.equal(observed.entries[1].type, '');
  assert.equal(observed.entries[2].type, '', 'a query value ending in .mjs must stay classic');
  assert.equal(observed.entries[3].type, '', 'a query value ending in .css must stay a script');
  assert.ok(requests.includes('/main.MJS?flavor=x&v=A'));
  assert.ok(requests.includes('/dependency.mjs'), 'native imports do not inherit the root version query');
  assert.equal(requests.some((url) => url.startsWith('/dependency.mjs?')), false);
});

test('a failed mjs dependency remains fail-open for the next entry', async (t) => {
  const origin = await startServer(t, (req, res) => {
    const pathname = new URL(req.url, 'http://runtime.test').pathname;
    if (pathname === '/version') {
      res.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
      res.end('A');
      return;
    }
    if (pathname === '/broken.mjs') {
      res.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'no-store' });
      res.end('import "./missing-dependency.mjs"; window.__entryOrder.push("broken");');
      return;
    }
    if (pathname === '/after.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'no-store' });
      res.end('window.__entryOrder.push("after");');
      return;
    }
    res.writeHead(404, { 'Content-Type': 'text/plain' });
    res.end('missing');
  });

  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  await page.evaluate((base) => {
    window.__entryOrder = [];
    window.JellyfinRefreshKitConfig = {
      name: 'ModuleFailureTest',
      mode: 'off',
      versionUrl: `${base}/version`,
      entryScripts: [`${base}/broken.mjs`, `${base}/after.js`],
    };
  }, origin);
  await injectRuntime(page);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('ModuleFailureTest')?.state().entriesLoaded === true
      && window.__entryOrder?.includes('after')
  ));

  const state = await page.evaluate(() => (
    window.JellyfinRefreshKit.get('ModuleFailureTest').state()
  ));
  assert.deepEqual(state.entryFailures, [
    { url: `${origin}/broken.mjs`, reason: 'load_error' },
  ]);
  assert.deepEqual(await page.evaluate(() => window.__entryOrder), ['after']);
});

test('mjs top-level await may continue after the ordered load chain settles', async (t) => {
  const origin = await startServer(t, (req, res) => {
    const pathname = new URL(req.url, 'http://runtime.test').pathname;
    if (pathname === '/version') {
      res.writeHead(200, { 'Content-Type': 'text/plain', 'Cache-Control': 'no-store' });
      res.end('A');
      return;
    }
    if (pathname === '/async-entry.mjs') {
      res.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'no-store' });
      res.end([
        'window.__entryOrder.push("module-start");',
        'await new Promise((resolve) => { window.__finishModuleEntry = resolve; });',
        'window.__entryOrder.push("module-finished");',
      ].join('\n'));
      return;
    }
    if (pathname === '/after.js') {
      res.writeHead(200, { 'Content-Type': 'application/javascript', 'Cache-Control': 'no-store' });
      res.end('window.__entryOrder.push("after");');
      return;
    }
    serveHtml(res);
  });

  const browser = await openBrowser(t);
  const page = await browser.newPage();
  await page.goto(`${origin}/`);
  await page.evaluate((base) => {
    window.__entryOrder = [];
    window.JellyfinRefreshKitConfig = {
      name: 'ModuleAwaitTest',
      mode: 'off',
      versionUrl: `${base}/version`,
      entryScripts: [`${base}/async-entry.mjs`, `${base}/after.js`],
    };
  }, origin);
  await injectRuntime(page);
  await page.waitForFunction(() => (
    window.JellyfinRefreshKit?.get('ModuleAwaitTest')?.state().entriesLoaded === true
      && window.__entryOrder?.includes('after')
  ));

  assert.deepEqual(await page.evaluate(() => window.__entryOrder), ['module-start', 'after']);
  await page.evaluate(() => window.__finishModuleEntry());
  await page.waitForFunction(() => window.__entryOrder?.includes('module-finished'));
  assert.deepEqual(await page.evaluate(() => window.__entryOrder), [
    'module-start', 'after', 'module-finished',
  ]);
});
