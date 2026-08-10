'use strict';

// Real-browser lifecycle exercise for one already-provisioned lab server:
// login (without clearing the retained password), dashboard/config navigation,
// three authenticated tabs with background visibility, an in-place server
// restart, websocket/API reconnection, and generation convergence.

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('puppeteer');

function parseArgs(argv) {
  const out = {};
  for (let i = 0; i < argv.length; i += 2) {
    const key = argv[i];
    const value = argv[i + 1];
    if (!key?.startsWith('--') || value === undefined) {
      throw new Error(`invalid argument sequence near ${key || '<end>'}`);
    }
    out[key.slice(2)] = value;
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
const target = args.target;
const origin = args.origin;
const container = args.container;
const project = args.project;
const outputRoot = path.resolve(__dirname, '..', 'artifacts');
const output = path.resolve(args.out || '');
const user = process.env.RK_LAB_USER || 'rk_admin';
const password = process.env.RK_LAB_PASSWORD || 'Test669Pw!x';

assert.match(target || '', /^jf(10|12)$/);
assert.match(origin || '', /^http:\/\/127\.0\.0\.1:\d+$/);
assert.match(container || '', /^[0-9a-f]{12,64}$/);
assert.match(project || '', /^rk-jellyfin-[a-z0-9][a-z0-9_-]*$/);
assert.ok(output.startsWith(`${outputRoot}${path.sep}`), 'output must remain under e2e/jellyfin/artifacts');
fs.rmSync(output, { recursive: true, force: true });
fs.mkdirSync(output, { recursive: true });

const labels = execFileSync('docker', [
  'inspect', '--format',
  '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}',
  container,
], { encoding: 'utf8' }).trim();
assert.equal(labels, `${project}|${target}`, 'refusing to restart a container outside this lab project/service');

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

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitUntil(probe, timeoutMs, intervalMs = 250) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const value = await probe();
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await sleep(intervalMs);
  }
  if (lastError) throw lastError;
  throw new Error(`condition did not become true within ${timeoutMs}ms`);
}

function redactUrl(raw) {
  try {
    const parsed = new URL(raw);
    for (const key of [...parsed.searchParams.keys()]) {
      if (/token|api.?key|authorization/i.test(key)) parsed.searchParams.set(key, '<redacted>');
    }
    return parsed.toString();
  } catch {
    return String(raw).replace(/([?&](?:token|api.?key|authorization)=)[^&]*/ig, '$1<redacted>');
  }
}

function redactText(raw) {
  return String(raw)
    .replace(/([?&](?:token|api.?key|authorization)=)[^&\s]*/ig, '$1<redacted>')
    .replace(/(Authorization:\s*MediaBrowser[^\r\n]*Token=")[^"]+/ig, '$1<redacted>');
}

function clipped(raw, limit = 8000) {
  const value = String(raw || '');
  return value.length > limit ? `${value.slice(0, limit)}…<truncated>` : value;
}

function guardedString(value) {
  try {
    return String(value);
  } catch {
    return '<String(value) threw>';
  }
}

function detailString(value) {
  if (value === undefined) return '';
  if (typeof value === 'string') return value;
  try {
    const encoded = JSON.stringify(value);
    return encoded === undefined ? guardedString(value) : encoded;
  } catch {
    return guardedString(value);
  }
}

function normalizePageError(value) {
  const message = typeof value?.message === 'string' ? value.message : '';
  const stack = typeof value?.stack === 'string' ? value.stack : '';
  const details = detailString(value?.details);
  const stringValue = guardedString(value);
  return {
    valueType: value === null ? 'null' : typeof value,
    name: clipped(typeof value?.name === 'string' ? value.name : ''),
    text: redactText(clipped(message || details || stringValue || '<empty page error>')),
    stack: redactText(clipped(stack)),
    details: redactText(clipped(details)),
    stringValue: redactText(clipped(stringValue)),
  };
}

function normalizeRuntimeException(details) {
  const remote = details?.exception || {};
  const remoteValue = typeof remote.description === 'string'
    ? remote.description
    : (typeof remote.unserializableValue === 'string'
      ? remote.unserializableValue
      : (Object.hasOwn(remote, 'value') ? guardedString(remote.value) : ''));
  const textParts = [details?.text, remoteValue].filter((value, index, all) => (
    typeof value === 'string' && value && all.indexOf(value) === index
  ));
  const frames = details?.stackTrace?.callFrames || [];
  const stack = frames.map((frame) => (
    `${frame.functionName || '<anonymous>'} (${redactUrl(frame.url || '')}:`
    + `${Number(frame.lineNumber || 0) + 1}:${Number(frame.columnNumber || 0) + 1})`
  )).join('\n');
  const firstFrame = frames[0];
  const sourceUrl = details?.url || firstFrame?.url || '';
  const line = Number(details?.lineNumber ?? firstFrame?.lineNumber ?? 0) + 1;
  const column = Number(details?.columnNumber ?? firstFrame?.columnNumber ?? 0) + 1;
  return {
    text: redactText(clipped(textParts.join(': ') || '<empty runtime exception>')),
    source: sourceUrl ? `${redactUrl(sourceUrl)}:${line}:${column}` : '',
    stack: redactText(clipped(stack)),
    exceptionType: typeof remote.type === 'string' ? remote.type : '',
    exceptionSubtype: typeof remote.subtype === 'string' ? remote.subtype : '',
  };
}

function pushBounded(list, value, state) {
  if (list.length < 20000) list.push(value);
  else state.truncated = true;
}

async function capturePage(page, name, suiteStarted) {
  const capture = {
    name,
    console: [],
    network: [],
    websocket: [],
    truncated: false,
    page,
  };
  const stamp = () => ({ at: new Date().toISOString(), elapsedMs: Date.now() - suiteStarted });

  page.on('console', (message) => {
    const location = message.location();
    pushBounded(capture.console, {
      ...stamp(),
      kind: 'console',
      type: message.type(),
      text: redactText(message.text()),
      source: location.url ? `${redactUrl(location.url)}:${location.lineNumber || 0}` : '',
    }, capture);
  });
  page.on('pageerror', (error) => {
    pushBounded(capture.console, {
      ...stamp(), kind: 'pageerror', type: 'error', ...normalizePageError(error),
    }, capture);
  });
  page.on('request', (request) => {
    pushBounded(capture.network, {
      ...stamp(), kind: 'request', method: request.method(), url: redactUrl(request.url()),
      resourceType: request.resourceType(), navigation: request.isNavigationRequest(),
    }, capture);
  });
  page.on('response', (response) => {
    pushBounded(capture.network, {
      ...stamp(), kind: 'response', status: response.status(), url: redactUrl(response.url()),
      fromCache: response.fromCache(), resourceType: response.request().resourceType(),
    }, capture);
  });
  page.on('requestfailed', (request) => {
    pushBounded(capture.network, {
      ...stamp(), kind: 'requestfailed', method: request.method(), url: redactUrl(request.url()),
      resourceType: request.resourceType(), error: request.failure()?.errorText || 'unknown',
    }, capture);
  });

  const cdp = await page.createCDPSession();
  await Promise.all([cdp.send('Network.enable'), cdp.send('Runtime.enable')]);
  cdp.on('Runtime.exceptionThrown', (event) => {
    pushBounded(capture.console, {
      ...stamp(), kind: 'runtimeexception', type: 'error',
      ...normalizeRuntimeException(event.exceptionDetails),
    }, capture);
  });
  cdp.on('Network.webSocketCreated', (event) => {
    pushBounded(capture.websocket, {
      ...stamp(), kind: 'created', requestId: event.requestId, url: redactUrl(event.url),
    }, capture);
  });
  cdp.on('Network.webSocketWillSendHandshakeRequest', (event) => {
    pushBounded(capture.websocket, {
      ...stamp(), kind: 'handshakeRequest', requestId: event.requestId,
      url: redactUrl(event.request?.url || ''),
    }, capture);
  });
  cdp.on('Network.webSocketHandshakeResponseReceived', (event) => {
    pushBounded(capture.websocket, {
      ...stamp(), kind: 'handshakeResponse', requestId: event.requestId,
      status: event.response?.status || 0, url: redactUrl(event.response?.url || ''),
    }, capture);
  });
  cdp.on('Network.webSocketClosed', (event) => {
    pushBounded(capture.websocket, {
      ...stamp(), kind: 'closed', requestId: event.requestId,
    }, capture);
  });

  await page.evaluateOnNewDocument(() => {
    window.__rkLabDocumentId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  });
  return capture;
}

function eventEvidence(event) {
  return {
    page: event.page,
    elapsedMs: event.elapsedMs,
    kind: event.kind,
    type: event.type || '',
    text: event.text || '',
    source: event.source || '',
    stack: event.stack || '',
    status: event.status || 0,
    method: event.method || '',
    url: event.url || '',
    error: event.error || '',
  };
}

function auditCapturedErrors(captures, restartWindow) {
  const buckets = Object.fromEntries([
    'restartTransportNoise',
    'allowlistedHostErrors',
    'unexpectedRefreshKitErrors',
    'otherHostOrUnattributedErrors',
  ].map((name) => [name, { count: 0, events: [], truncated: false }]));
  const add = (name, event) => {
    const bucket = buckets[name];
    bucket.count += 1;
    if (bucket.events.length < 250) bucket.events.push(eventEvidence(event));
    else bucket.truncated = true;
  };
  const errors = [];
  for (const capture of captures) {
    for (const event of capture.console) {
      if (event.type === 'error' || event.kind === 'pageerror' || event.kind === 'runtimeexception') {
        errors.push({ page: capture.name, ...event });
      }
    }
    for (const event of capture.network) {
      if ((event.kind === 'response' && event.status >= 500) || event.kind === 'requestfailed') {
        errors.push({ page: capture.name, ...event });
      }
    }
  }

  const start = Number(restartWindow?.startElapsedMs);
  const end = Number(restartWindow?.recoveryCompletedElapsedMs);
  // A restarted Jellyfin drops in-flight connections and serves warm-up errors
  // (503 Service Unavailable, connection resets, WebSocket retries) whose tail
  // outlasts the measured recovery window by an unpredictable amount on a slow
  // runner, so any fixed end margin still races. A same-origin TRANSPORT error
  // after the restart was initiated is an expected restart artifact, never a
  // RefreshKit logic defect — and a genuine failure to recover is caught
  // independently by the convergence, generation and WebSocket-reconnect
  // assertions above. So excuse transport noise from the restart start onward;
  // a bounded window is still used only to correlate non-transport page errors
  // with nearby real transport noise. The recorded restartWindow is unchanged,
  // and a genuine RefreshKit logic error (no transport signature) still fails.
  const CORRELATION_WINDOW_MARGIN_MS = 15000;
  const atOrAfterRestart = (event) => (
    Number.isFinite(start) && event.elapsedMs >= start
  );
  const inRestartWindow = (event) => (
    Number.isFinite(start) && Number.isFinite(end)
      && event.elapsedMs >= start && event.elapsedMs <= end + CORRELATION_WINDOW_MARGIN_MS
  );
  const haystack = (event) => [
    event.text, event.source, event.stack, event.details, event.url, event.error,
  ].filter(Boolean).join('\n');
  const isTransportNoise = (event) => (
    (event.kind === 'response' && event.status >= 500)
    // A requestfailed is by definition a network-level failure (the request did
    // not complete) — never a logic error — whatever the exact errorText, which
    // Chromium reports inconsistently (net::ERR_ABORTED, empty, etc.) when a
    // connection is dropped by a restart. Treat every requestfailed as
    // transport noise; it is only excused when it also occurs at/after the
    // restart (atOrAfterRestart) below.
    || event.kind === 'requestfailed'
    || /failed to load resource:.*(?:5\d\d|ERR_)|net::ERR_|service unavailable|websocket.*(?:failed|closed)|failed to fetch|networkerror|load failed|connection (?:reset|refused|closed|aborted)/i
      .test(haystack(event))
  );
  const isNotificationPermission = (event) => (
    /showNotification.*No notification permission has been granted/i.test(haystack(event))
  );
  const isRefreshKitAttributed = (event) => (
    /refresh[\s_-]*kit|\/RefreshKit\//i.test(haystack(event))
  );
  const transportTimesByPage = new Map();
  for (const event of errors) {
    if (!atOrAfterRestart(event) || !isTransportNoise(event)) continue;
    if (!transportTimesByPage.has(event.page)) transportTimesByPage.set(event.page, []);
    transportTimesByPage.get(event.page).push(event.elapsedMs);
  }
  for (const times of transportTimesByPage.values()) times.sort((a, b) => a - b);
  const correlatedRestartTransport = (event) => {
    const times = transportTimesByPage.get(event.page) || [];
    const lower = event.elapsedMs - 1000;
    let left = 0;
    let right = times.length;
    while (left < right) {
      const middle = Math.floor((left + right) / 2);
      if (times[middle] < lower) left = middle + 1;
      else right = middle;
    }
    return left < times.length && times[left] <= event.elapsedMs + 1000;
  };

  for (const event of errors) {
    if (isNotificationPermission(event)) {
      add('allowlistedHostErrors', event);
    } else if (atOrAfterRestart(event) && isTransportNoise(event)) {
      add('restartTransportNoise', event);
    } else if (isRefreshKitAttributed(event)) {
      add('unexpectedRefreshKitErrors', event);
    } else if (inRestartWindow(event)
      && (event.kind === 'pageerror' || event.kind === 'runtimeexception')
      && correlatedRestartTransport(event)) {
      add('restartTransportNoise', event);
    } else {
      add('otherHostOrUnattributedErrors', event);
    }
  }

  return {
    restartWindow,
    ...buckets,
  };
}

async function authenticated(page) {
  return page.evaluate(() => {
    try {
      return Boolean(window.ApiClient && typeof window.ApiClient.accessToken === 'function'
        && window.ApiClient.accessToken());
    } catch {
      return false;
    }
  });
}

async function clickLoginChoice(page) {
  return page.evaluate((wantedUser) => {
    const clickable = (node) => node?.closest?.('button, a, .cardBox, .emby-button') || node;
    const nodes = [...document.querySelectorAll('button, a, .cardBox, .emby-button, .listItem')];
    const named = nodes.find((node) => node.textContent?.trim() === wantedUser);
    if (named) {
      clickable(named)?.click();
      return 'named-user';
    }
    const manual = nodes.find((node) => /manual|other user|sign in/i.test(node.textContent || '')
      || node.matches?.('.btnManual, [data-id="manual"]'));
    if (manual) {
      clickable(manual)?.click();
      return 'manual';
    }
    return '';
  }, user);
}

async function login(page) {
  if (!page.url().startsWith(`${origin}/web`)) {
    await page.goto(`${origin}/web/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  }
  await sleep(1500);
  if (await authenticated(page)) return;

  await waitUntil(async () => {
    if (await authenticated(page)) return true;
    return page.evaluate(() => Boolean(
      document.querySelector('#txtManualName, #txtManualPassword, input[autocomplete="username"], input[autocomplete="current-password"], .cardBox, .btnManual'),
    ));
  }, 30000, 300);

  if (await authenticated(page)) return;
  let passwordSelector = await page.$('#txtManualPassword, input[autocomplete="current-password"], input[type="password"]');
  if (!passwordSelector) {
    await clickLoginChoice(page);
    await sleep(800);
  }

  const usernameSelector = '#txtManualName, input[autocomplete="username"], input[name="Username"]';
  const passwordSelectors = '#txtManualPassword, input[autocomplete="current-password"], input[type="password"]';
  const usernameField = await page.$(usernameSelector);
  if (usernameField) {
    await usernameField.click({ clickCount: 3 });
    await usernameField.type(user, { delay: 8 });
  }
  passwordSelector = await page.$(passwordSelectors);
  if (!passwordSelector) throw new Error('login password field did not appear');
  await passwordSelector.click({ clickCount: 3 });
  await passwordSelector.type(password, { delay: 8 });

  const submitted = await page.evaluate(() => {
    const passwordField = document.querySelector('#txtManualPassword, input[autocomplete="current-password"], input[type="password"]');
    const form = passwordField?.closest('form');
    const button = form?.querySelector('button[type="submit"], .btnSubmit, button.raised')
      || document.querySelector('button[type="submit"], .btnSubmit');
    if (button) {
      button.click();
      return true;
    }
    return false;
  });
  if (!submitted) await page.keyboard.press('Enter');

  await waitUntil(() => authenticated(page), 60000, 400);
  // Deliberately do not clear or blur the password field. Jellyfin 10.11 keeps
  // the completed login page hidden in this same document with its value intact.
  await sleep(2500);
}

async function waitForKit(page) {
  await page.waitForFunction(() => Boolean(
    window.JellyfinRefreshKit?.get?.('RefreshKitPlugin'),
  ), { timeout: 45000 });
}

async function navigateHash(page, hashes, probe, label) {
  for (const hash of hashes) {
    await page.evaluate((nextHash) => { location.hash = nextHash; }, hash);
    try {
      const result = await waitUntil(() => page.evaluate((kind, expectedHash) => {
        if (kind === 'config') {
          const root = document.querySelector('#RefreshKitConfigPage');
          return root ? { hash: location.hash, title: document.title, text: root.innerText.slice(0, 500) } : null;
        }
        if (kind === 'dashboard') {
          const text = document.body?.innerText || '';
          const selector = document.querySelector('#dashboardPage, .dashboardPage, [data-type="DashboardPage"]');
          return (selector || /\bDashboard\b/i.test(text))
            ? { hash: location.hash, title: document.title, text: text.slice(0, 500) } : null;
        }
        return location.hash === expectedHash ? { hash: location.hash, title: document.title } : null;
      }, probe, hash), 8000, 250);
      return { requested: hash, ...result };
    } catch {
      // Try the spelling used by the other Jellyfin generation.
    }
  }
  throw new Error(`${label} did not render for any route: ${hashes.join(', ')}`);
}

async function passwordAndKitState(page) {
  return page.evaluate(() => {
    const passwords = [...document.querySelectorAll('input[type="password"]')].map((field) => {
      let rendered = true;
      try {
        const style = getComputedStyle(field);
        rendered = field.isConnected
          && !field.closest('[hidden], [aria-hidden="true"], [inert]')
          && style.display !== 'none'
          && style.visibility !== 'hidden'
          && field.getClientRects().length > 0;
      } catch {
        rendered = true;
      }
      return {
        id: field.id || '',
        valueLength: typeof field.value === 'string' ? field.value.length : -1,
        rendered,
        disabled: field.matches?.(':disabled') || false,
      };
    });
    const handle = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin');
    return {
      passwords,
      populatedHiddenPasswords: passwords.filter((field) => field.valueLength > 0 && !field.rendered).length,
      kit: handle?.state?.() || null,
      documentId: window.__rkLabDocumentId || null,
      visibility: document.visibilityState,
      url: location.href,
    };
  });
}

async function pageSnapshot(page, name) {
  return page.evaluate((pageName) => {
    const handle = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin');
    let authenticatedNow = false;
    try { authenticatedNow = Boolean(window.ApiClient?.accessToken?.()); } catch {}
    return {
      name: pageName,
      url: location.href,
      hash: location.hash,
      title: document.title,
      visibility: document.visibilityState,
      documentId: window.__rkLabDocumentId || null,
      authenticated: authenticatedNow,
      kit: handle?.state?.() || null,
    };
  }, name);
}

async function generation() {
  const response = await fetch(`${origin}/RefreshKit/Generation?_=${Date.now()}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`generation endpoint returned ${response.status}`);
  const data = await response.json();
  return data.CacheKey || data.cacheKey;
}

async function waitForServer() {
  return waitUntil(async () => {
    const response = await fetch(`${origin}/System/Info/Public?_=${Date.now()}`, { cache: 'no-store' });
    return response.ok;
  }, 180000, 1000);
}

async function convergePage(page, expectedGeneration) {
  await page.bringToFront();
  await sleep(500);
  await page.waitForFunction(async (base, expected) => {
    try {
      const publicResponse = await fetch(`${base}/System/Info/Public?_=${Date.now()}`, { cache: 'no-store' });
      const generationResponse = await fetch(`${base}/RefreshKit/Generation?_=${Date.now()}`, { cache: 'no-store' });
      if (!publicResponse.ok || !generationResponse.ok) return false;
      const data = await generationResponse.json();
      const current = data.CacheKey || data.cacheKey;
      const handle = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin');
      if (!handle || current !== expected) return false;
      const state = handle.state();
      return state.version === expected && state.latestVersion === expected
        && Boolean(window.ApiClient?.accessToken?.());
    } catch {
      return false;
    }
  }, { timeout: 90000, polling: 500 }, origin, expectedGeneration);
}

const suiteStarted = Date.now();
const failures = [];
const result = {
  target,
  origin,
  startedUtc: new Date(suiteStarted).toISOString(),
  login: null,
  navigation: {},
  preRestart: [],
  postRestart: [],
  hiddenAtRestart: [],
  generationBefore: null,
  generationAfter: null,
  websocketReconnect: {},
  restartWindow: null,
  errorAudit: null,
  failures,
};
const captures = [];
let browser;

function recordFailure(message) {
  failures.push(message);
  console.error(`FAIL ${target}: ${message}`);
}

(async () => {
  try {
    browser = await puppeteer.launch({
      executablePath: browserExecutable(),
      headless: true,
      defaultViewport: { width: 1440, height: 1000 },
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });

    const primary = await browser.newPage();
    const primaryCapture = await capturePage(primary, 'primary', suiteStarted);
    captures.push(primaryCapture);
    await primary.goto(`${origin}/web/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await login(primary);
    await waitForKit(primary);
    await sleep(1300);

    result.login = await passwordAndKitState(primary);
    if (target === 'jf10' && result.login.populatedHiddenPasswords < 1) {
      recordFailure('Jellyfin 10 login did not retain a populated hidden password field');
    }
    if (result.login.kit?.wouldBlockNow === 'password_entry') {
      recordFailure('retained login password still blocks the runtime after authentication');
    }
    if (!result.login.kit) recordFailure('RefreshKitPlugin instance is absent after login');

    result.navigation.dashboard = await navigateHash(
      primary,
      ['#/dashboard', '#/dashboard.html', '#!/dashboard'],
      'dashboard',
      'dashboard',
    );
    await primary.screenshot({ path: path.join(output, 'dashboard.png'), fullPage: true });

    result.navigation.configuration = await navigateHash(
      primary,
      [
        '#/configurationpage?name=Jellyfin%20Refresh%20Kit',
        '#/configurationpage?name=Jellyfin+Refresh+Kit',
        '#!/configurationpage?name=Jellyfin%20Refresh%20Kit',
      ],
      'config',
      'Refresh Kit configuration page',
    );
    const configInitialized = await primary.waitForFunction(() => {
      const value = document.querySelector('#rkGeneration')?.textContent?.trim();
      return value && value !== '…' && value !== 'unavailable';
    }, { timeout: 10000 }).then(() => true, () => false);
    const configState = await primary.$eval('#RefreshKitConfigPage', (node) => ({
      controller: node.getAttribute('data-controller'),
      generation: document.querySelector('#rkGeneration')?.textContent?.trim() || '',
    }));
    const failedControllerResponses = primaryCapture.network.filter((event) => (
      event.kind === 'response'
      && event.status >= 400
      && /\/web\/configurationpage\?name=refreshkit(?:&|$)/i.test(event.url)
    )).map(({ status, url }) => ({ status, url }));
    const controllerImportErrors = primaryCapture.console.filter((event) => (
      event.kind === 'pageerror' && /failed to import:.*configurationpage\?name=refreshkit/i.test(event.text)
    )).map(({ type, text }) => ({ type, text }));
    result.navigation.configuration = {
      ...result.navigation.configuration,
      ...configState,
      initialized: configInitialized,
      failedControllerResponses,
      controllerImportErrors,
    };
    if (!configInitialized) {
      recordFailure(
        `configuration page did not initialize (generation ${JSON.stringify(configState.generation)}, `
        + `controller ${JSON.stringify(configState.controller)}`
        + `${failedControllerResponses[0] ? `, controller response ${failedControllerResponses[0].status}` : ''}`
        + `${controllerImportErrors[0] ? ', Failed to import was raised' : ''})`,
      );
    }
    await primary.screenshot({ path: path.join(output, 'configuration.png'), fullPage: true });

    await navigateHash(primary, ['#/home', '#!/home'], 'home', 'home');

    const secondary = await browser.newPage();
    const secondaryCapture = await capturePage(secondary, 'secondary', suiteStarted);
    captures.push(secondaryCapture);
    await secondary.goto(`${origin}/web/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await login(secondary);
    await waitForKit(secondary);
    await navigateHash(secondary, ['#/dashboard', '#/dashboard.html', '#!/dashboard'], 'dashboard', 'dashboard');

    const background = await browser.newPage();
    const backgroundCapture = await capturePage(background, 'background', suiteStarted);
    captures.push(backgroundCapture);
    await background.goto(`${origin}/web/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await login(background);
    await waitForKit(background);
    await navigateHash(background, ['#/home', '#!/home'], 'home', 'home');

    const pages = [
      { page: primary, name: 'primary', capture: primaryCapture },
      { page: secondary, name: 'secondary', capture: secondaryCapture },
      { page: background, name: 'background', capture: backgroundCapture },
    ];

    await primary.bringToFront();
    await sleep(750);
    result.preRestart = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
    result.hiddenAtRestart = result.preRestart.filter((item) => item.visibility === 'hidden').map((item) => item.name);
    if (result.hiddenAtRestart.length < 1) recordFailure('no Chromium tab was hidden at restart time');
    if (result.preRestart.some((item) => !item.authenticated || !item.kit)) {
      recordFailure('not every pre-restart tab is authenticated with a kit instance');
    }

    result.generationBefore = await generation();
    const restartAt = Date.now();
    result.restartUtc = new Date(restartAt).toISOString();
    result.restartWindow = {
      startElapsedMs: restartAt - suiteStarted,
      serverHealthyElapsedMs: null,
      recoveryCompletedElapsedMs: null,
      recoveryIncomplete: false,
    };
    console.log(`==> ${target}: restarting server with ${pages.length} tabs open (${result.hiddenAtRestart.join(', ')} hidden)`);
    execFileSync('docker', ['restart', container], { stdio: ['ignore', 'ignore', 'inherit'] });
    await waitForServer();
    result.restartWindow.serverHealthyElapsedMs = Date.now() - suiteStarted;
    result.generationAfter = await waitUntil(() => generation(), 60000, 500);
    if (result.generationAfter !== result.generationBefore) {
      recordFailure(`plain restart changed generation ${result.generationBefore} -> ${result.generationAfter}`);
    }

    for (const item of pages) {
      try {
        await convergePage(item.page, result.generationAfter);
      } catch (error) {
        recordFailure(`${item.name} did not reconnect/converge: ${error.message}`);
      }
      await item.page.screenshot({
        path: path.join(output, `post-restart-${item.name}.png`),
        fullPage: false,
      });
    }

    // Jellyfin Web's socket retry delay can approach a minute. Give all tabs
    // one shared window so a slow foreground retry cannot make later tabs wait
    // sequentially or get cut off at the deadline.
    await primary.bringToFront();
    await waitUntil(() => pages.every((item) => item.capture.websocket.some((event) => (
      event.kind === 'handshakeResponse'
      && event.elapsedMs >= restartAt - suiteStarted
      && event.status === 101
    ))), 120000, 500).catch(() => false);
    for (const item of pages) {
      const reconnected = item.capture.websocket.some((event) => (
        event.kind === 'handshakeResponse'
        && event.elapsedMs >= restartAt - suiteStarted
        && event.status === 101
      ));
      result.websocketReconnect[item.name] = reconnected;
      if (!reconnected) recordFailure(`${item.name} had no successful websocket handshake after restart`);
    }
    result.restartWindow.recoveryCompletedElapsedMs = Date.now() - suiteStarted;

    result.postRestart = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
    for (const before of result.preRestart) {
      const after = result.postRestart.find((item) => item.name === before.name);
      if (!after?.authenticated) recordFailure(`${before.name} lost authentication across restart`);
      if (after?.documentId !== before.documentId) recordFailure(`${before.name} reloaded instead of reconnecting in place`);
      if (after?.kit?.version !== result.generationAfter || after?.kit?.latestVersion !== result.generationAfter) {
        recordFailure(`${before.name} kit did not converge to ${result.generationAfter}`);
      }
    }

    const configGeneration = result.navigation.configuration.generation;
    if (result.navigation.configuration.initialized && configGeneration !== result.generationBefore) {
      recordFailure(`configuration page showed ${configGeneration}, endpoint reported ${result.generationBefore}`);
    }
  } catch (error) {
    recordFailure(error.stack || error.message);
  } finally {
    if (result.restartWindow && result.restartWindow.recoveryCompletedElapsedMs === null) {
      result.restartWindow.recoveryCompletedElapsedMs = Date.now() - suiteStarted;
      result.restartWindow.recoveryIncomplete = true;
    }
    result.errorAudit = auditCapturedErrors(captures, result.restartWindow);
    if (result.errorAudit.unexpectedRefreshKitErrors.count > 0) {
      // Print the offending events to the log so a CI failure is diagnosable
      // without the (unretained-on-failure) result.json artifact.
      for (const event of result.errorAudit.unexpectedRefreshKitErrors.events) {
        console.error(
          `  unexpected RK event: page=${event.page} kind=${event.kind}`
          + ` ms=${event.elapsedMs} status=${event.status ?? ''}`
          + ` text=${JSON.stringify(clipped(event.text || '', 200))}`
          + ` url=${JSON.stringify(redactUrl(event.url || event.source || ''))}`
          + ` error=${JSON.stringify(event.error || '')}`,
        );
      }
      recordFailure(
        `${result.errorAudit.unexpectedRefreshKitErrors.count} unexpected `
        + 'RefreshKit-attributed browser/network error(s); see result.json errorAudit',
      );
    }
    result.finishedUtc = new Date().toISOString();
    result.captureCounts = Object.fromEntries(captures.map((capture) => [capture.name, {
      console: capture.console.length,
      network: capture.network.length,
      websocket: capture.websocket.length,
      truncated: capture.truncated,
    }]));

    const consoleEvents = captures.flatMap((capture) => capture.console.map((event) => ({ page: capture.name, ...event })));
    const networkEvents = captures.flatMap((capture) => capture.network.map((event) => ({ page: capture.name, ...event })));
    const websocketEvents = captures.flatMap((capture) => capture.websocket.map((event) => ({ page: capture.name, ...event })));
    fs.writeFileSync(path.join(output, 'console.json'), `${JSON.stringify(consoleEvents, null, 2)}\n`);
    fs.writeFileSync(path.join(output, 'network.json'), `${JSON.stringify(networkEvents, null, 2)}\n`);
    fs.writeFileSync(path.join(output, 'websocket.json'), `${JSON.stringify(websocketEvents, null, 2)}\n`);
    fs.writeFileSync(path.join(output, 'result.json'), `${JSON.stringify(result, null, 2)}\n`);

    if (browser) await browser.close();
  }

  if (failures.length > 0) {
    console.error(`RESULT ${target}: FAIL (${failures.length}) — ${output}/result.json`);
    process.exitCode = 1;
  } else {
    console.log(`RESULT ${target}: PASS — ${output}/result.json`);
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 2;
});
