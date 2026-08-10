'use strict';

// Exercise Jellyfin's real package/plugin APIs while authenticated Chromium
// documents remain open: install, update, disable, enable, uninstall,
// reinstall, and a final in-place restart.

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('puppeteer');

function parseArgs(argv) {
  const parsed = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith('--') || value === undefined) {
      throw new Error(`invalid argument sequence near ${key || '<end>'}`);
    }
    parsed[key.slice(2)] = value;
  }
  return parsed;
}

const args = parseArgs(process.argv.slice(2));
const target = args.target;
const origin = args.origin;
const container = args.container;
const project = args.project;
const outputRoot = path.resolve(__dirname, '..', 'artifacts');
const stateRoot = path.resolve(__dirname, '..', '.state');
const output = path.resolve(args.out || '');
const metadataPath = path.resolve(args.metadata || '');
const user = process.env.RK_LAB_USER || 'rk_admin';
const password = process.env.RK_LAB_PASSWORD || 'Test669Pw!x';
const token = process.env.RK_LAB_TOKEN || '';
const pluginId = '515255fe-3332-49b0-b471-0be58c8221d8';
const pluginName = 'Jellyfin Refresh Kit';

assert.match(target || '', /^jf(10|12)$/);
assert.match(origin || '', /^http:\/\/127\.0\.0\.1:\d+$/);
assert.match(container || '', /^[0-9a-f]{12,64}$/);
assert.match(project || '', /^rk-jellyfin-[a-z0-9][a-z0-9_-]*$/);
assert.ok(output.startsWith(`${outputRoot}${path.sep}`), 'output must remain below e2e/jellyfin/artifacts');
assert.ok(metadataPath.startsWith(`${stateRoot}${path.sep}`), 'metadata must remain below e2e/jellyfin/.state');
fs.rmSync(output, { recursive: true, force: true });
fs.mkdirSync(output, { recursive: true });
assert.ok(token.length >= 16, 'RK_LAB_TOKEN is missing or implausibly short');

const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
assert.equal(metadata.target, target);
assert.match(metadata.repositoryUrl || '', /^http:\/\/repository:8080\/jf(10|12)\/manifest\.json$/);
for (const version of [metadata.baselineVersion, metadata.candidateVersion, metadata.embeddedCandidateVersion]) {
  assert.match(version || '', /^\d+(?:\.\d+){3}$/);
}

const labels = execFileSync('docker', [
  'inspect', '--format',
  '{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}',
  container,
], { encoding: 'utf8' }).trim();
assert.equal(labels, `${project}|${target}`, 'refusing to restart a container outside this lab project/service');
const configuredImage = execFileSync('docker', [
  'inspect', '--format', '{{.Config.Image}}', container,
], { encoding: 'utf8' }).trim();

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

async function waitUntil(probe, timeoutMs, intervalMs = 300) {
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

function pushBounded(list, value, capture) {
  if (list.length < 20000) list.push(value);
  else capture.truncated = true;
}

async function capturePage(page, name, started) {
  const capture = { name, console: [], network: [], truncated: false };
  const stamp = () => ({ at: new Date().toISOString(), elapsedMs: Date.now() - started });
  page.on('console', (message) => {
    const location = message.location();
    pushBounded(capture.console, {
      ...stamp(), kind: 'console', type: message.type(), text: redactText(message.text()),
      source: location.url ? `${redactUrl(location.url)}:${location.lineNumber || 0}` : '',
    }, capture);
  });
  page.on('pageerror', (error) => {
    pushBounded(capture.console, {
      ...stamp(), kind: 'pageerror', type: 'error', text: redactText(error?.message || String(error)),
      stack: redactText(error?.stack || ''),
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
  await page.evaluateOnNewDocument(() => {
    window.__rkLifecycleDocumentId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  });
  return capture;
}

function authHeader() {
  return `MediaBrowser Client="RefreshKit Lifecycle E2E", Device="Disposable Lab", DeviceId="rk-${project}", Version="1", Token="${token}"`;
}

async function requestApi(route, options = {}) {
  const response = await fetch(`${origin}${route}`, {
    method: options.method || 'GET',
    headers: {
      Authorization: authHeader(),
      ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: AbortSignal.timeout(options.timeoutMs || 60000),
    cache: 'no-store',
  });
  const text = await response.text();
  const expected = options.expected || [200];
  if (!expected.includes(response.status)) {
    throw new Error(`${options.method || 'GET'} ${route} returned ${response.status}: ${text.slice(0, 500)}`);
  }
  if (!text) return { status: response.status, data: null };
  try {
    return { status: response.status, data: JSON.parse(text) };
  } catch {
    return { status: response.status, data: text };
  }
}

async function preparePlaybackFixture() {
  const mediaDir = path.join(stateRoot, 'media', target);
  const mediaFile = path.join(mediaDir, 'Refresh Kit Lifecycle.mp4');
  fs.mkdirSync(mediaDir, { recursive: true });
  if (!fs.existsSync(mediaFile) || fs.statSync(mediaFile).size < 100000) {
    const partial = path.join(mediaDir, 'Refresh Kit Lifecycle.part.mp4');
    fs.rmSync(partial, { force: true });
    execFileSync('ffmpeg', [
      '-hide_banner', '-loglevel', 'error', '-y',
      '-f', 'lavfi', '-i', 'testsrc2=duration=60:size=640x360:rate=24',
      '-f', 'lavfi', '-i', 'sine=frequency=440:duration=60',
      '-shortest', '-c:v', 'libx264', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p',
      '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', partial,
    ], { stdio: ['ignore', 'ignore', 'inherit'] });
    fs.renameSync(partial, mediaFile);
  }
  execFileSync('docker', ['exec', container, 'mkdir', '-p', '/media/RefreshKitLifecycle'], {
    stdio: ['ignore', 'ignore', 'inherit'],
  });
  execFileSync('docker', ['cp', mediaFile, `${container}:/media/RefreshKitLifecycle/Refresh Kit Lifecycle.mp4`], {
    stdio: ['ignore', 'ignore', 'inherit'],
  });
  const libraryQuery = new URLSearchParams({
    name: 'Refresh Kit Lifecycle',
    collectionType: 'movies',
    paths: '/media/RefreshKitLifecycle',
    refreshLibrary: 'true',
  });
  await requestApi(`/Library/VirtualFolders?${libraryQuery}`, { method: 'POST', expected: [204] });
  const { data: me } = await requestApi('/Users/Me');
  const userId = String(value(me, 'Id') || '');
  assert.match(userId, /^[0-9a-f-]{32,36}$/i, 'authenticated user has no usable id');
  const itemQuery = new URLSearchParams({
    Recursive: 'true',
    IncludeItemTypes: 'Movie',
    Fields: 'Path,MediaSources',
    SearchTerm: 'Refresh Kit Lifecycle',
    Limit: '10',
  });
  const item = await waitUntil(async () => {
    const { data } = await requestApi(`/Users/${encodeURIComponent(userId)}/Items?${itemQuery}`);
    const items = value(data, 'Items');
    if (!Array.isArray(items)) return false;
    return items.find((entry) => String(value(entry, 'Name') || '').includes('Refresh Kit Lifecycle')) || false;
  }, 180000, 2000);
  const itemId = String(value(item, 'Id') || '');
  assert.match(itemId, /^[0-9a-f-]{32,36}$/i, 'playback fixture has no usable item id');
  return {
    itemId,
    name: String(value(item, 'Name') || ''),
    path: String(value(item, 'Path') || ''),
    bytes: fs.statSync(mediaFile).size,
  };
}

function publishCandidateCatalog() {
  const repositoryDir = path.join(stateRoot, 'repository', target);
  const updateCatalog = path.join(repositoryDir, 'manifest-update.json');
  const activeCatalog = path.join(repositoryDir, 'manifest.json');
  const partialCatalog = path.join(repositoryDir, 'manifest.json.part');
  assert.ok(fs.existsSync(updateCatalog), 'candidate repository catalog is missing');
  fs.copyFileSync(updateCatalog, partialCatalog);
  fs.renameSync(partialCatalog, activeCatalog);
}

function value(record, key) {
  return record?.[key] ?? record?.[`${key.slice(0, 1).toLowerCase()}${key.slice(1)}`];
}

function statusName(status) {
  const numeric = new Map([[1, 'Restart'], [0, 'Active'], [-1, 'Disabled'], [-2, 'NotSupported'], [-3, 'Malfunctioned'], [-4, 'Superseded'], [-5, 'Deleted']]);
  if (numeric.has(status)) return numeric.get(status);
  return String(status || '');
}

async function pluginInventory() {
  const { data } = await requestApi('/Plugins');
  assert.ok(Array.isArray(data), 'plugin inventory is not an array');
  return data.filter((record) => {
    const id = String(value(record, 'Id') || '').replaceAll('-', '').toLowerCase();
    const name = String(value(record, 'Name') || '').toLowerCase();
    return id === pluginId.replaceAll('-', '') || name === pluginName.toLowerCase();
  }).map((record) => ({
    id: String(value(record, 'Id') || ''),
    name: String(value(record, 'Name') || ''),
    version: String(value(record, 'Version') || ''),
    status: statusName(value(record, 'Status')),
    canUninstall: value(record, 'CanUninstall'),
  }));
}

async function waitForInventory(predicate, description) {
  return waitUntil(async () => {
    const inventory = await pluginInventory();
    return predicate(inventory) ? inventory : false;
  }, 30000, 500).catch((error) => {
    throw new Error(`${description}: ${error.message}`);
  });
}

async function authenticated(page) {
  return page.evaluate(() => {
    try { return Boolean(window.ApiClient?.accessToken?.()); } catch { return false; }
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
  await sleep(1200);
  if (await authenticated(page)) return;
  await waitUntil(async () => {
    if (await authenticated(page)) return true;
    return page.evaluate(() => Boolean(document.querySelector(
      '#txtManualName, #txtManualPassword, input[autocomplete="username"], input[autocomplete="current-password"], .cardBox, .btnManual',
    )));
  }, 30000, 300);
  if (await authenticated(page)) return;
  let passwordField = await page.$('#txtManualPassword, input[autocomplete="current-password"], input[type="password"]');
  if (!passwordField) {
    await clickLoginChoice(page);
    await sleep(800);
  }
  const usernameField = await page.$('#txtManualName, input[autocomplete="username"], input[name="Username"]');
  if (usernameField) {
    await usernameField.click({ clickCount: 3 });
    await usernameField.type(user, { delay: 8 });
  }
  passwordField = await page.$('#txtManualPassword, input[autocomplete="current-password"], input[type="password"]');
  if (!passwordField) throw new Error('login password field did not appear');
  await passwordField.click({ clickCount: 3 });
  await passwordField.type(password, { delay: 8 });
  const submitted = await page.evaluate(() => {
    const field = document.querySelector('#txtManualPassword, input[autocomplete="current-password"], input[type="password"]');
    const button = field?.closest('form')?.querySelector('button[type="submit"], .btnSubmit, button.raised')
      || document.querySelector('button[type="submit"], .btnSubmit');
    if (!button) return false;
    button.click();
    return true;
  });
  if (!submitted) await page.keyboard.press('Enter');
  await waitUntil(() => authenticated(page), 60000, 400);
  await sleep(1500);
}

async function pageSnapshot(page, name) {
  return page.evaluate((pageName) => {
    const handle = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin');
    let authenticatedNow = false;
    try { authenticatedNow = Boolean(window.ApiClient?.accessToken?.()); } catch {}
    const scriptUrls = [...document.scripts]
      .map((script) => script.src)
      .filter((url) => /\/RefreshKit\/kit\.js/i.test(url));
    const resourceUrls = performance.getEntriesByType('resource')
      .map((entry) => entry.name)
      .filter((url) => /\/RefreshKit\/kit\.js/i.test(url));
    return {
      name: pageName,
      url: location.href,
      documentId: window.__rkLifecycleDocumentId || null,
      authenticated: authenticatedNow,
      visibility: document.visibilityState,
      kit: handle?.state?.() || null,
      scriptUrls,
      resourceUrls,
      injectionTags: document.querySelectorAll('[plugin="Jellyfin Refresh Kit"], [data-name="RefreshKitPlugin"]').length,
    };
  }, name);
}

function versionFromKitUrl(raw) {
  try { return new URL(raw).searchParams.get('v'); } catch { return null; }
}

function validateActiveSnapshot(snapshot, generation) {
  assert.equal(snapshot.authenticated, true, `${snapshot.name} is not authenticated`);
  assert.ok(snapshot.kit, `${snapshot.name} has no Refresh Kit handle`);
  assert.equal(snapshot.kit.version, generation, `${snapshot.name} runtime version is stale`);
  assert.equal(snapshot.kit.latestVersion, generation, `${snapshot.name} latest version is stale`);
  assert.ok(snapshot.injectionTags >= 1, `${snapshot.name} has no injected runtime tag`);
  const kitUrls = [...snapshot.scriptUrls, ...snapshot.resourceUrls];
  assert.ok(kitUrls.length >= 1, `${snapshot.name} loaded no versioned kit.js resource`);
  const stale = kitUrls.filter((url) => versionFromKitUrl(url) !== generation);
  assert.deepEqual(stale, [], `${snapshot.name} retained stale kit.js URL(s)`);
}

function validateInactiveSnapshot(snapshot) {
  assert.equal(snapshot.authenticated, true, `${snapshot.name} is not authenticated`);
  assert.equal(snapshot.kit, null, `${snapshot.name} retained a Refresh Kit handle`);
  assert.equal(snapshot.injectionTags, 0, `${snapshot.name} retained an injected runtime tag`);
  assert.deepEqual(snapshot.scriptUrls, [], `${snapshot.name} retained a kit.js script`);
  assert.deepEqual(snapshot.resourceUrls, [], `${snapshot.name} loaded kit.js from browser cache`);
}

async function reloadPages(pages, active, generation) {
  const before = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
  for (const { page } of pages) {
    await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
    await login(page);
    if (active) {
      await page.waitForFunction((expected) => {
        try {
          const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
          return state?.version === expected && state?.latestVersion === expected
            && Boolean(window.ApiClient?.accessToken?.());
        } catch { return false; }
      }, { timeout: 90000, polling: 500 }, generation);
    } else {
      await page.waitForFunction(() => {
        try {
          return Boolean(window.ApiClient?.accessToken?.())
            && !window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')
            && !document.querySelector('[plugin="Jellyfin Refresh Kit"], [data-name="RefreshKitPlugin"]');
        } catch { return false; }
      }, { timeout: 60000, polling: 400 });
    }
  }
  const after = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
  for (const snapshot of after) {
    if (active) validateActiveSnapshot(snapshot, generation);
    else validateInactiveSnapshot(snapshot);
    const prior = before.find((item) => item.name === snapshot.name);
    assert.notEqual(snapshot.documentId, prior?.documentId, `${snapshot.name} did not create a fresh document on reload`);
  }
  return { before, after };
}

async function convergeUpdatedPages(pages, generation, before) {
  const waitForGeneration = async (page) => {
    await page.waitForFunction((expected) => {
      try {
        const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
        return state?.version === expected && state?.latestVersion === expected
          && Boolean(window.ApiClient?.accessToken?.());
      } catch { return false; }
    }, { timeout: 150000, polling: 500 }, generation);
  };

  // A real browser deliberately suspends Refresh Kit polling in background tabs.
  // Prove the already-visible tab converges without intervention, then visit each
  // still-open authenticated tab and prove it converges as soon as it is resumed.
  await waitForGeneration(pages[0].page);
  for (const { page } of pages.slice(1)) {
    await page.bringToFront();
    await page.waitForFunction(() => document.visibilityState === 'visible', { timeout: 10000 });
    await waitForGeneration(page);
  }
  const after = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
  for (const snapshot of after) {
    validateActiveSnapshot(snapshot, generation);
    const prior = before.find((item) => item.name === snapshot.name);
    assert.notEqual(snapshot.documentId, prior?.documentId, `${snapshot.name} did not auto-reload for the package update`);
  }
  return after;
}

function pluginConfiguration(enableThirdPartyStamping, pollSeconds = 5) {
  return {
    EnableInjection: true,
    EnableThirdPartyStamping: enableThirdPartyStamping,
    EnableAutoReload: true,
    PollSeconds: pollSeconds,
    IdleSeconds: 0,
    ReloadBudget: 10,
    EnableConfigWatching: true,
    ConfigWatchExclusions: [],
    ConfigCooldownMinutes: 0,
    DevMode: false,
  };
}

async function savePluginConfiguration(enableThirdPartyStamping, pollSeconds = 5) {
  return requestApi(`/Plugins/${pluginId}/Configuration`, {
    method: 'POST', expected: [204], body: pluginConfiguration(enableThirdPartyStamping, pollSeconds),
  });
}

async function findVisibleElement(page, selectors, timeoutMs = 30000) {
  return waitUntil(async () => {
    for (const selector of selectors) {
      const handles = await page.$$(selector);
      for (const handle of handles) {
        const box = await handle.boundingBox();
        if (box && box.width > 0 && box.height > 0) return handle;
        await handle.dispose();
      }
    }
    return false;
  }, timeoutMs, 300);
}

async function beginPlayback(page, fixture) {
  await page.bringToFront();
  await page.waitForFunction(() => document.visibilityState === 'visible', { timeout: 10000 });
  let playButton;
  for (const route of [`#/details?id=${fixture.itemId}`, `#!/details?id=${fixture.itemId}`]) {
    await page.evaluate((hash) => { location.hash = hash; }, route);
    try {
      playButton = await findVisibleElement(page, [
        '.btnPlay:not(.hide)',
        'button.btnPlay',
        'button[title="Play"]',
        'button[aria-label="Play"]',
      ], 15000);
      if (playButton) break;
    } catch {
      // Try the alternate hash spelling used by the other web generation.
    }
  }
  if (!playButton) throw new Error(`play button did not render for fixture ${fixture.itemId}`);
  await playButton.click();
  await playButton.dispose();
  await page.waitForFunction(() => {
    const media = document.querySelector('video');
    return Boolean(media && !media.paused && media.currentTime > 0.25 && media.readyState >= 2);
  }, { timeout: 90000, polling: 250 });
  return page.evaluate(() => {
    const media = document.querySelector('video');
    const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.() || null;
    return {
      documentId: window.__rkLifecycleDocumentId || null,
      hash: location.hash,
      paused: media?.paused ?? null,
      currentTime: media?.currentTime ?? null,
      readyState: media?.readyState ?? null,
      wouldBlockNow: state?.wouldBlockNow || null,
      version: state?.version || null,
      latestVersion: state?.latestVersion || null,
    };
  });
}

async function exercisePlaybackGate(pages, fixture, generationBefore) {
  const primary = pages[0];
  const playing = await beginPlayback(primary.page, fixture);
  const beforeFirstConvergence = await Promise.all(
    pages.map(({ page, name }) => pageSnapshot(page, name)),
  );
  assert.equal(
    beforeFirstConvergence[0].visibility,
    'visible',
    'primary playback tab was not visible before the gated generation change',
  );
  assert.ok(
    ['playback_route', 'media_element', 'fullscreen'].includes(playing.wouldBlockNow),
    `live playback did not engage a reload gate: ${playing.wouldBlockNow}`,
  );
  await savePluginConfiguration(false);
  const changedServer = await waitUntil(async () => {
    const state = await fetchServerState(true);
    return state?.generation && state.generation !== generationBefore ? state : false;
  }, 120000, 1000);
  await primary.page.waitForFunction((expectedOld, expectedNew, expectedDocument) => {
    try {
      const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
      return state?.version === expectedOld && state?.latestVersion === expectedNew
        && window.__rkLifecycleDocumentId === expectedDocument;
    } catch { return false; }
  }, { timeout: 90000, polling: 500 }, generationBefore, changedServer.generation, playing.documentId);
  const gatedWhilePlaying = await pageSnapshot(primary.page, primary.name);
  assert.equal(gatedWhilePlaying.documentId, playing.documentId, 'playing document reloaded despite its safety gate');
  assert.equal(gatedWhilePlaying.kit?.version, generationBefore);
  assert.equal(gatedWhilePlaying.kit?.latestVersion, changedServer.generation);
  assert.ok(
    ['playback_route', 'media_element', 'fullscreen'].includes(gatedWhilePlaying.kit?.wouldBlockNow),
    `playing update was not blocked: ${gatedWhilePlaying.kit?.wouldBlockNow}`,
  );

  await primary.page.$eval('video', (media) => media.pause());
  await sleep(7000);
  const paused = await pageSnapshot(primary.page, primary.name);
  const pausedMedia = await primary.page.$eval('video', (media) => ({
    paused: media.paused,
    currentTime: media.currentTime,
    readyState: media.readyState,
  }));
  assert.equal(pausedMedia.paused, true, 'playback fixture did not pause');
  assert.equal(paused.documentId, playing.documentId, 'paused playback document reloaded before leaving playback');
  assert.equal(paused.kit?.version, generationBefore);
  assert.equal(paused.kit?.latestVersion, changedServer.generation);
  assert.ok(
    ['playback_route', 'media_element', 'fullscreen'].includes(paused.kit?.wouldBlockNow),
    `paused session did not remain conservatively gated: ${paused.kit?.wouldBlockNow}`,
  );

  await primary.page.evaluate(() => { location.hash = '#/home'; });
  await primary.page.waitForFunction((expected, oldDocument) => {
    try {
      const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
      return state?.version === expected && state?.latestVersion === expected
        && window.__rkLifecycleDocumentId !== oldDocument
        && Boolean(window.ApiClient?.accessToken?.());
    } catch { return false; }
  }, { timeout: 120000, polling: 500 }, changedServer.generation, playing.documentId);
  const afterLeavingPlayback = await convergeUpdatedPages(
    pages,
    changedServer.generation,
    beforeFirstConvergence,
  );

  await primary.page.bringToFront();
  await primary.page.waitForFunction(() => document.visibilityState === 'visible', { timeout: 10000 });
  const beforeRestore = await Promise.all(
    pages.map(({ page, name }) => pageSnapshot(page, name)),
  );
  assert.equal(beforeRestore[0].visibility, 'visible', 'primary tab was not visible before configuration restore');
  // Restore stamping with a fresh, semantically equivalent polling interval so
  // the cache identity remains monotonic instead of reverting to generationBefore.
  // Re-serving an identity the tab already left is correctly rejected as a flap.
  await savePluginConfiguration(true, 6);
  const restoredServer = await waitUntil(async () => {
    const state = await fetchServerState(true);
    return state?.generation && state.generation !== changedServer.generation ? state : false;
  }, 120000, 1000);
  assert.notEqual(
    restoredServer.generation,
    generationBefore,
    'configuration restore reused an already-departed cache generation',
  );
  const afterRestore = await convergeUpdatedPages(pages, restoredServer.generation, beforeRestore);
  return {
    fixture,
    generationBefore,
    playing,
    gatedWhilePlaying,
    changedServer,
    paused: { page: paused, media: pausedMedia },
    beforeFirstConvergence,
    afterLeavingPlayback,
    beforeRestore,
    restoredServer,
    afterRestore,
  };
}

async function exerciseLogout(page, name, generation) {
  await page.bringToFront();
  const before = await pageSnapshot(page, name);
  let logoutItem;
  try {
    logoutItem = await findVisibleElement(page, ['.btnLogout'], 1500);
  } catch {
    try {
      const drawerButton = await findVisibleElement(page, [
        'button.mainDrawerButton',
        '.mainDrawerButton',
        'button[title="Menu"]',
      ], 5000);
      await drawerButton.evaluate((node) => node.click());
      await drawerButton.dispose();
      logoutItem = await findVisibleElement(page, ['.btnLogout'], 5000);
    } catch {
      const menuButton = await findVisibleElement(page, [
        'button[aria-controls="app-user-menu"]',
        'button[aria-label*="User menu" i]',
        'button[title*="User menu" i]',
        'button.headerUserButton',
        '.headerUserButton',
      ], 30000);
      await menuButton.evaluate((node) => node.click());
      await menuButton.dispose();
    }
  }
  if (!logoutItem) {
    logoutItem = await waitUntil(async () => {
      const handles = await page.$$('#app-user-menu [role="menuitem"], .btnLogout, button, a');
      for (const handle of handles) {
        const [text, box] = await Promise.all([
          handle.evaluate((node) => node.textContent || ''),
          handle.boundingBox(),
        ]);
        if (box && box.width > 0 && box.height > 0 && /sign\s*out|log\s*out/i.test(text)) return handle;
        await handle.dispose();
      }
      return false;
    }, 30000, 300);
  }
  await logoutItem.evaluate((node) => node.click());
  await logoutItem.dispose();
  await waitUntil(async () => !(await authenticated(page)), 60000, 300);
  const loggedOut = await pageSnapshot(page, name);
  assert.equal(loggedOut.authenticated, false, 'UI logout left the page authenticated');
  assert.ok(loggedOut.kit, 'Refresh Kit disappeared from the active plugin shell after logout');
  await login(page);
  await page.waitForFunction((expected) => {
    try {
      const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
      return state?.version === expected && state?.latestVersion === expected
        && Boolean(window.ApiClient?.accessToken?.());
    } catch { return false; }
  }, { timeout: 90000, polling: 500 }, generation);
  const relogged = await pageSnapshot(page, name);
  validateActiveSnapshot(relogged, generation);
  return { before, loggedOut, relogged };
}

async function fetchServerState(expectActive) {
  const publicResponse = await fetch(`${origin}/System/Info/Public?_=${Date.now()}`, {
    cache: 'no-store', signal: AbortSignal.timeout(5000),
  });
  if (!publicResponse.ok) return null;
  const publicData = await publicResponse.json();
  const serverVersion = String(value(publicData, 'Version') || '');
  const generationResponse = await fetch(`${origin}/RefreshKit/Generation?_=${Date.now()}`, {
    cache: 'no-store', signal: AbortSignal.timeout(5000),
  });
  const shellResponse = await fetch(`${origin}/web/index.html?rk-lifecycle=${Date.now()}`, {
    cache: 'no-store', signal: AbortSignal.timeout(10000),
  });
  if (!shellResponse.ok) return null;
  const shell = await shellResponse.text();
  const injected = shell.includes('plugin="Jellyfin Refresh Kit"')
    && shell.includes('data-name="RefreshKitPlugin"');
  if (!expectActive) {
    if (generationResponse.status === 404 && !injected && !shell.includes('/RefreshKit/kit.js')) {
      return { active: false, generationStatus: 404, injected: false, serverVersion };
    }
    return null;
  }
  if (!generationResponse.ok || !injected) return null;
  const generationData = await generationResponse.json();
  const generation = value(generationData, 'CacheKey');
  if (!generation || !shell.includes(`/RefreshKit/kit.js?v=${generation}`)) return null;
  const kitResponse = await fetch(`${origin}/RefreshKit/kit.js?v=${encodeURIComponent(generation)}`, {
    cache: 'no-store', signal: AbortSignal.timeout(10000),
  });
  if (!kitResponse.ok || !/immutable/i.test(kitResponse.headers.get('cache-control') || '')) return null;
  const kit = await kitResponse.text();
  if (!/KIT_VERSION\s*=\s*'[^']+'/.test(kit)) return null;
  return { active: true, generationStatus: generationResponse.status, injected, generation, serverVersion };
}

async function waitForServerState(expectActive) {
  return waitUntil(() => fetchServerState(expectActive), 180000, 1000);
}

async function settleGeneration() {
  let last = null;
  let stable = 0;
  return waitUntil(async () => {
    const state = await fetchServerState(true);
    if (!state) return false;
    if (state.generation === last) stable += 1;
    else {
      last = state.generation;
      stable = 0;
    }
    return stable >= 3 ? state : false;
  }, 90000, 3000);
}

async function diagnostics() {
  const { data } = await requestApi('/RefreshKit/Diagnostics');
  return {
    generation: String(value(data, 'Generation') || ''),
    kitVersion: String(value(data, 'KitVersion') || ''),
    pluginCount: Array.isArray(value(data, 'Plugins')) ? value(data, 'Plugins').length : null,
  };
}

async function installPackage(version) {
  const query = new URLSearchParams({
    assemblyGuid: pluginId,
    version,
    repositoryUrl: metadata.repositoryUrl,
  });
  return requestApi(`/Packages/Installed/${encodeURIComponent(pluginName)}?${query}`, {
    method: 'POST', expected: [204], timeoutMs: 120000,
  });
}

async function restartServer(result, phase) {
  const started = Date.now();
  execFileSync('docker', ['restart', container], { stdio: ['ignore', 'ignore', 'inherit'] });
  result.restartWindows.push({
    phase,
    startedUtc: new Date(started).toISOString(),
    startElapsedMs: started - result.startedMs,
    healthyElapsedMs: null,
  });
  await waitUntil(async () => {
    try {
      const response = await fetch(`${origin}/System/Info/Public?_=${Date.now()}`, {
        cache: 'no-store', signal: AbortSignal.timeout(3000),
      });
      return response.ok;
    } catch { return false; }
  }, 180000, 1000);
  result.restartWindows.at(-1).healthyElapsedMs = Date.now() - result.startedMs;
}

function phaseRecord(result, name, details) {
  const entry = { name, at: new Date().toISOString(), elapsedMs: Date.now() - result.startedMs, ...details };
  result.phases.push(entry);
  console.log(`==> ${target}: ${name}`);
  return entry;
}

const startedMs = Date.now();
const failures = [];
const captures = [];
const result = {
  target,
  origin,
  configuredImage,
  startedMs,
  startedUtc: new Date(startedMs).toISOString(),
  metadata,
  phases: [],
  restartWindows: [],
  failures,
};
let browser;

(async () => {
  try {
    await requestApi('/Repositories', {
      method: 'POST',
      body: [{ Name: 'Refresh Kit lifecycle lab', Url: metadata.repositoryUrl, Enabled: true }],
      expected: [204],
    });
    const available = await requestApi('/Packages');
    const availableText = JSON.stringify(available.data);
    assert.match(availableText, new RegExp(metadata.baselineVersion.replaceAll('.', '\\.')));
    assert.doesNotMatch(availableText, new RegExp(metadata.candidateVersion.replaceAll('.', '\\.')));
    const playbackFixture = await preparePlaybackFixture();
    phaseRecord(result, 'playback-fixture-indexed', { fixture: playbackFixture });

    browser = await puppeteer.launch({
      executablePath: browserExecutable(),
      headless: true,
      defaultViewport: { width: 1440, height: 1000 },
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });
    const pages = [];
    for (const name of ['primary', 'secondary', 'background']) {
      const page = await browser.newPage();
      captures.push(await capturePage(page, name, startedMs));
      await page.goto(`${origin}/web/`, { waitUntil: 'domcontentloaded', timeout: 60000 });
      await login(page);
      pages.push({ page, name });
    }
    await pages[0].page.bringToFront();
    await sleep(500);
    const pristine = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
    pristine.forEach(validateInactiveSnapshot);
    phaseRecord(result, 'pristine-authenticated-tabs', { pages: pristine });

    const installResponse = await installPackage(metadata.baselineVersion);
    const pendingInstall = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Restart'),
      'baseline install did not enter restart-pending state',
    );
    await restartServer(result, 'install-baseline');
    let baselineServer = await waitForServerState(true);
    const activeBaselineInventory = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Active'
        && record.version === metadata.baselineVersion),
      'baseline did not become active after restart',
    );
    await savePluginConfiguration(true);
    baselineServer = await settleGeneration();
    const baselineReload = await reloadPages(pages, true, baselineServer.generation);
    const baselineDiagnostics = await diagnostics();
    phaseRecord(result, 'install-baseline-active', {
      apiStatus: installResponse.status,
      pendingInventory: pendingInstall,
      activeInventory: activeBaselineInventory,
      server: baselineServer,
      diagnostics: baselineDiagnostics,
      pages: baselineReload.after,
    });
    await pages[0].page.screenshot({ path: path.join(output, 'baseline-active.png'), fullPage: false });

    await pages[0].page.bringToFront();
    await pages[0].page.waitForFunction(() => document.visibilityState === 'visible', { timeout: 10000 });
    const beforeUpdate = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
    assert.equal(beforeUpdate[0].visibility, 'visible', 'primary tab was not visible before candidate publication');
    publishCandidateCatalog();
    const updateCatalog = await requestApi('/Packages');
    assert.match(
      JSON.stringify(updateCatalog.data),
      new RegExp(metadata.candidateVersion.replaceAll('.', '\\.')),
      'candidate did not appear after atomic repository publication',
    );
    const updateResponse = await installPackage(metadata.candidateVersion);
    const pendingUpdate = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Restart')
        && inventory.some((record) => record.status === 'Superseded'),
      'candidate update did not expose restart/superseded inventory',
    );
    await restartServer(result, 'update-candidate');
    const candidateServer = await waitForServerState(true);
    const activeCandidateInventory = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Active'
        && record.version === metadata.candidateVersion)
        && inventory.every((record) => record.status !== 'Restart'),
      'candidate did not become active after restart',
    );
    assert.notEqual(candidateServer.generation, baselineServer.generation, 'package update did not change the cache generation');
    const afterUpdate = await convergeUpdatedPages(pages, candidateServer.generation, beforeUpdate);
    const candidateDiagnostics = await diagnostics();
    phaseRecord(result, 'update-candidate-converged', {
      apiStatus: updateResponse.status,
      pendingInventory: pendingUpdate,
      activeInventory: activeCandidateInventory,
      server: candidateServer,
      diagnostics: candidateDiagnostics,
      pagesBefore: beforeUpdate,
      pagesAfter: afterUpdate,
      automaticReload: true,
    });
    await pages[0].page.screenshot({ path: path.join(output, 'candidate-active.png'), fullPage: false });

    const disableResponse = await requestApi(
      `/Plugins/${pluginId}/${metadata.candidateVersion}/Disable`,
      { method: 'POST', expected: [204] },
    );
    const additionalDisableResponses = [];
    for (const record of activeCandidateInventory) {
      if (record.version === metadata.candidateVersion || ['Deleted', 'Malfunctioned'].includes(record.status)) continue;
      const response = await requestApi(
        `/Plugins/${pluginId}/${encodeURIComponent(record.version)}/Disable`,
        { method: 'POST', expected: [204] },
      );
      additionalDisableResponses.push({ version: record.version, status: response.status });
    }
    const pendingDisable = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Restart'),
      'disable did not enter restart-pending state',
    );
    await restartServer(result, 'disable-candidate');
    const disabledServer = await waitForServerState(false);
    const disabledInventory = await waitForInventory(
      (inventory) => inventory.length >= 1
        && inventory.every((record) => ['Disabled', 'Deleted'].includes(record.status)),
      'all installed Refresh Kit versions did not remain disabled after restart',
    );
    const staleDocumentsBeforeReload = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
    const disabledReload = await reloadPages(pages, false);
    phaseRecord(result, 'disable-clean-shell', {
      apiStatus: disableResponse.status,
      additionalApiStatuses: additionalDisableResponses,
      pendingInventory: pendingDisable,
      disabledInventory,
      server: disabledServer,
      openDocumentsBeforeNormalReload: staleDocumentsBeforeReload,
      pagesAfterNormalReload: disabledReload.after,
    });
    await pages[0].page.screenshot({ path: path.join(output, 'disabled-clean-shell.png'), fullPage: false });

    const enableResponse = await requestApi(
      `/Plugins/${pluginId}/${metadata.candidateVersion}/Enable`,
      { method: 'POST', expected: [204] },
    );
    const pendingEnable = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Restart'),
      'enable did not enter restart-pending state',
    );
    await restartServer(result, 'enable-candidate');
    const enabledServer = await waitForServerState(true);
    const enabledInventory = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Active'),
      'candidate did not become active after enable restart',
    );
    const enabledReload = await reloadPages(pages, true, enabledServer.generation);
    phaseRecord(result, 'enable-active', {
      apiStatus: enableResponse.status,
      pendingInventory: pendingEnable,
      activeInventory: enabledInventory,
      server: enabledServer,
      pages: enabledReload.after,
    });

    const inventoryBeforeUninstall = await pluginInventory();
    const uninstallVersions = [...new Set([
      metadata.candidateVersion,
      ...inventoryBeforeUninstall.map((record) => record.version).filter(Boolean),
    ])];
    const uninstallResponses = [];
    for (const version of uninstallVersions) {
      const response = await requestApi(
        `/Plugins/${pluginId}/${encodeURIComponent(version)}`,
        { method: 'DELETE', expected: [204] },
      );
      uninstallResponses.push({ version, status: response.status });
    }
    await restartServer(result, 'uninstall-all-versions');
    const uninstalledServer = await waitForServerState(false);
    const uninstallReload = await reloadPages(pages, false);
    const postUninstallInventory = await pluginInventory();
    assert.deepEqual(postUninstallInventory, [], 'uninstall left a Refresh Kit version in inventory');
    phaseRecord(result, 'uninstall-clean-shell', {
      apiStatuses: uninstallResponses,
      inventoryBeforeUninstall,
      server: uninstalledServer,
      inventory: postUninstallInventory,
      pages: uninstallReload.after,
    });

    const reinstallResponse = await installPackage(metadata.candidateVersion);
    const pendingReinstall = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Restart'),
      'reinstall did not enter restart-pending state',
    );
    await restartServer(result, 'reinstall-candidate');
    const reinstalledServer = await waitForServerState(true);
    const reinstalledInventory = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Active'
        && record.version === metadata.candidateVersion),
      'candidate did not become active after reinstall restart',
    );
    const reinstallReload = await reloadPages(pages, true, reinstalledServer.generation);
    const reinstalledDiagnostics = await diagnostics();
    phaseRecord(result, 'reinstall-active', {
      apiStatus: reinstallResponse.status,
      pendingInventory: pendingReinstall,
      activeInventory: reinstalledInventory,
      server: reinstalledServer,
      diagnostics: reinstalledDiagnostics,
      pages: reinstallReload.after,
    });

    const beforePlainRestart = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
    await restartServer(result, 'plain-restart');
    const finalServer = await waitForServerState(true);
    assert.equal(finalServer.generation, reinstalledServer.generation, 'plain restart changed cache generation');
    await Promise.all(pages.map(async ({ page }) => {
      await page.waitForFunction((expected) => {
        try {
          const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
          return state?.version === expected && state?.latestVersion === expected
            && Boolean(window.ApiClient?.accessToken?.());
        } catch { return false; }
      }, { timeout: 120000, polling: 500 }, finalServer.generation);
    }));
    const afterPlainRestart = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
    for (const snapshot of afterPlainRestart) {
      validateActiveSnapshot(snapshot, finalServer.generation);
      assert.equal(
        snapshot.documentId,
        beforePlainRestart.find((item) => item.name === snapshot.name)?.documentId,
        `${snapshot.name} reloaded during a generation-stable restart`,
      );
    }
    phaseRecord(result, 'plain-restart-in-place', {
      server: finalServer,
      pagesBefore: beforePlainRestart,
      pagesAfter: afterPlainRestart,
    });
    await pages[0].page.screenshot({ path: path.join(output, 'final-active.png'), fullPage: false });

    const playbackGate = await exercisePlaybackGate(pages, playbackFixture, finalServer.generation);
    phaseRecord(result, 'playback-pause-safety-gate', playbackGate);
    await pages[0].page.screenshot({ path: path.join(output, 'post-playback-converged.png'), fullPage: false });

    const logout = await exerciseLogout(
      pages[1].page,
      pages[1].name,
      playbackGate.restoredServer.generation,
    );
    phaseRecord(result, 'ui-logout-and-relogin', logout);
    result.completed = true;

  } catch (error) {
    failures.push(error.stack || error.message || String(error));
    console.error(`FAIL ${target}: ${failures.at(-1)}`);
  } finally {
    try {
      await requestApi('/Repositories', { method: 'POST', body: [], expected: [204], timeoutMs: 10000 });
      result.repositoryConfigurationRemoved = true;
    } catch (error) {
      result.repositoryConfigurationRemoved = false;
      result.repositoryCleanupError = redactText(error?.message || String(error));
      if (failures.length === 0) failures.push(`could not remove lifecycle repository configuration: ${result.repositoryCleanupError}`);
    }
    result.finishedUtc = new Date().toISOString();
    result.durationMs = Date.now() - startedMs;
    result.captureCounts = Object.fromEntries(captures.map((capture) => [capture.name, {
      console: capture.console.length,
      network: capture.network.length,
      truncated: capture.truncated,
    }]));
    const consoleEvents = captures.flatMap((capture) => capture.console.map((event) => ({ page: capture.name, ...event })));
    const networkEvents = captures.flatMap((capture) => capture.network.map((event) => ({ page: capture.name, ...event })));
    result.refreshKitAttributedBrowserErrors = consoleEvents.filter((event) => (
      (event.type === 'error' || event.kind === 'pageerror')
      && /refresh[\s_-]*kit|\/RefreshKit\//i.test(`${event.text || ''}\n${event.source || ''}\n${event.stack || ''}`)
    ));
    // No upper bound on purpose: a restart's warm-up transport tail outlasts
    // healthyElapsedMs by an unpredictable amount on a slow runner, so any fixed
    // margin still races. A transport-signature error on a /RefreshKit/ URL at
    // or after a restart began is an expected restart artifact; a genuine
    // failure to recover is caught by the convergence assertions, and a
    // RefreshKit logic error carries no transport signature so it is still
    // counted as unexpected below.
    const insideRestartWindow = (event) => result.restartWindows.some((window) => (
      Number.isFinite(window.startElapsedMs)
      && event.elapsedMs >= window.startElapsedMs - 1000
    ));
    // A restart drops in-flight connections and briefly serves warm-up errors,
    // so a poll of a same-origin /RefreshKit/ endpoint inside the restart
    // window can surface either an HTTP 4xx/5xx OR a connection-level transient
    // (net::ERR_CONNECTION_RESET/REFUSED, "Failed to fetch"). Both are expected
    // restart transitions, not RefreshKit defects. A genuine RefreshKit logic
    // error carries no transport signature and is still counted as unexpected.
    result.expectedRefreshKitTransitionErrors = result.refreshKitAttributedBrowserErrors.filter((event) => (
      /\/RefreshKit\//i.test(`${event.source || ''}\n${event.text || ''}`)
      && /status of (?:4\d\d|5\d\d)|net::ERR_|ERR_CONNECTION|connection (?:reset|refused|closed|aborted)|failed to (?:load resource|fetch)|load failed|networkerror/i
        .test(`${event.text || ''}\n${event.source || ''}\n${event.stack || ''}`)
      && insideRestartWindow(event)
    ));
    result.unexpectedRefreshKitBrowserErrors = result.refreshKitAttributedBrowserErrors.filter(
      (event) => !result.expectedRefreshKitTransitionErrors.includes(event),
    );
    if (result.unexpectedRefreshKitBrowserErrors.length > 0) {
      failures.push(
        `${result.unexpectedRefreshKitBrowserErrors.length} unexpected Refresh Kit-attributed browser error(s)`,
      );
    }
    fs.writeFileSync(path.join(output, 'console.json'), `${JSON.stringify(consoleEvents, null, 2)}\n`);
    fs.writeFileSync(path.join(output, 'network.json'), `${JSON.stringify(networkEvents, null, 2)}\n`);
    fs.writeFileSync(path.join(output, 'result.json'), `${JSON.stringify(result, null, 2)}\n`);
    if (browser) await browser.close();
  }

  if (failures.length > 0) {
    console.error(`RESULT ${target} lifecycle: FAIL — ${output}/result.json`);
    process.exitCode = 1;
  } else {
    console.log(`RESULT ${target} lifecycle: PASS — ${output}/result.json`);
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 2;
});
