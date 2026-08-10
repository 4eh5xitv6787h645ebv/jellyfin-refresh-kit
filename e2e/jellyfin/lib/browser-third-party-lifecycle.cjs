'use strict';

// Exercise a genuine third-party plugin lifecycle while Refresh Kit remains
// active in three authenticated Chromium tabs. The fixture has independently
// compiled 1.0.0.0 and 2.0.0.0 assemblies plus distinct loose browser assets.

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { createHash } = require('node:crypto');
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
const refreshKitId = '515255fe333249b0b4710be58c8221d8';

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
assert.equal(metadata.repositoryUrl, `http://repository:8080/${target}/third-party/manifest.json`);
assert.equal(String(metadata.fixtureId || '').toLowerCase(), '8f42f34a-a7d1-4b6e-9b77-17ed99d7a216');
assert.equal(metadata.fixtureName, 'Refresh Kit Lifecycle Probe');
assert.equal(metadata.baselineVersion, '1.0.0.0');
assert.equal(metadata.candidateVersion, '2.0.0.0');
assert.match(metadata.refreshKitRuntimeVersion || '', /^\d+(?:\.\d+)+$/);
assert.match(metadata.refreshKitPackageVersion || '', /^\d+(?:\.\d+){3}$/);
assert.match(metadata.refreshKitSourceRevision || '', /^[0-9a-f]{40}$/);
assert.match(metadata.refreshKitSourceTreeSha256 || '', /^[0-9a-f]{64}$/);
assert.match(metadata.refreshKitSnapshot || '', /^[A-Za-z0-9._-]+$/);
for (const field of ['baselineAssemblySha256', 'baselineSha256', 'candidateAssemblySha256', 'candidateSha256']) {
  assert.match(metadata[field] || '', /^[0-9a-f]{64}$/i, `${field} is not a SHA-256 digest`);
}
for (const field of ['baselineMd5', 'candidateMd5']) {
  assert.match(metadata[field] || '', /^[0-9a-f]{32}$/i, `${field} is not an MD5 digest`);
}
assert.notEqual(metadata.baselineAssemblySha256, metadata.candidateAssemblySha256);
assert.notEqual(metadata.baselineSha256, metadata.candidateSha256);

const fixtureId = metadata.fixtureId.replaceAll('-', '').toLowerCase();
const fixtureName = metadata.fixtureName;
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

function value(object, name) {
  if (!object || typeof object !== 'object') return undefined;
  if (Object.hasOwn(object, name)) return object[name];
  const key = Object.keys(object).find((candidate) => candidate.toLowerCase() === name.toLowerCase());
  return key ? object[key] : undefined;
}

function statusName(status) {
  const numeric = new Map([[1, 'Restart'], [0, 'Active'], [-1, 'Disabled'], [-2, 'NotSupported'], [-3, 'Malfunctioned'], [-4, 'Superseded'], [-5, 'Deleted']]);
  if (numeric.has(status)) return numeric.get(status);
  if (typeof status === 'string' && /^-?\d+$/.test(status) && numeric.has(Number(status))) {
    return numeric.get(Number(status));
  }
  return String(status || '');
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

function pushBounded(list, item, capture) {
  if (list.length < 20000) list.push(item);
  else capture.truncated = true;
}

async function capturePage(page, name, startedMs) {
  const capture = { name, console: [], network: [], truncated: false };
  const stamp = () => ({ at: new Date().toISOString(), elapsedMs: Date.now() - startedMs });
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
    window.__rkThirdPartyDocumentId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  });
  return capture;
}

function authHeader() {
  return `MediaBrowser Client="RefreshKit Third-Party Lifecycle E2E", Device="Disposable Lab", DeviceId="rk-${project}", Version="1", Token="${token}"`;
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
    const kit = handle?.state?.() || null;
    const documentHtml = document.documentElement?.outerHTML || '';
    let authenticatedNow = false;
    try { authenticatedNow = Boolean(window.ApiClient?.accessToken?.()); } catch {}
    return {
      name: pageName,
      url: location.href,
      documentId: window.__rkThirdPartyDocumentId || null,
      authenticated: authenticatedNow,
      visibility: document.visibilityState,
      kit,
      scriptUrls: [...document.scripts]
        .map((script) => script.src)
        .filter((url) => /\/RefreshKit\/kit\.js/i.test(url)),
      epochPresentInDocument: Boolean(
        (kit?.baselineEpoch && documentHtml.includes(kit.baselineEpoch))
        || (kit?.latestEpoch && documentHtml.includes(kit.latestEpoch)),
      ),
      injectionTags: document.querySelectorAll('[plugin="Jellyfin Refresh Kit"], [data-name="RefreshKitPlugin"]').length,
    };
  }, name);
}

function validateActiveSnapshot(snapshot, server) {
  assert.equal(snapshot.authenticated, true, `${snapshot.name} is not authenticated`);
  assert.ok(snapshot.kit, `${snapshot.name} has no Refresh Kit handle`);
  assert.equal(snapshot.kit.kitVersion, metadata.refreshKitRuntimeVersion, `${snapshot.name} runs an unexpected kit runtime`);
  assert.equal(snapshot.kit.version, server.generation, `${snapshot.name} runs a stale generation`);
  assert.equal(snapshot.kit.latestVersion, server.generation, `${snapshot.name} sees a different latest generation`);
  assert.equal(snapshot.kit.baselineEpoch, server.epoch, `${snapshot.name} runs a stale process epoch`);
  assert.equal(snapshot.kit.latestEpoch, server.epoch, `${snapshot.name} sees a different latest process epoch`);
  assert.equal(snapshot.epochPresentInDocument, false, `${snapshot.name} exposes the process epoch in document HTML`);
  assert.equal(String(snapshot.kit.versionUrl || '').includes(server.epoch), false, `${snapshot.name} exposes the process epoch in its generation URL`);
  assert.equal(snapshot.injectionTags, 1, `${snapshot.name} does not have exactly one injected tag`);
  assert.equal(snapshot.scriptUrls.length, 1, `${snapshot.name} does not have exactly one kit.js resource`);
  assert.match(snapshot.scriptUrls[0], new RegExp(`[?&]v=${server.generation.replaceAll('-', '\\-')}(?:&|$)`));
  assert.equal(snapshot.scriptUrls[0].includes(server.epoch), false, `${snapshot.name} exposes the process epoch in an asset URL`);
}

function validateUnchangedPages(before, after, server, context) {
  for (const snapshot of after) {
    validateActiveSnapshot(snapshot, server);
    const prior = before.find((item) => item.name === snapshot.name);
    assert.ok(prior, `${context}: missing prior state for ${snapshot.name}`);
    assert.equal(snapshot.documentId, prior.documentId, `${context}: ${snapshot.name} reloaded before restart`);
    assert.deepEqual(snapshot.scriptUrls, prior.scriptUrls, `${context}: ${snapshot.name} kit URL changed before restart`);
    assert.equal(snapshot.injectionTags, prior.injectionTags, `${context}: ${snapshot.name} injection count changed`);
  }
}

async function preparePrimary(pages, server) {
  if (server) {
    for (const { page } of pages) {
      await page.bringToFront();
      await page.waitForFunction(() => document.visibilityState === 'visible', { timeout: 10000 });
      await waitPageGeneration(page, server, 90000);
    }
  }
  await pages[0].page.bringToFront();
  await pages[0].page.waitForFunction(() => document.visibilityState === 'visible', { timeout: 10000 });
  const snapshots = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
  assert.equal(snapshots[0].visibility, 'visible', 'primary tab is not visible before lifecycle change');
  if (server) snapshots.forEach((snapshot) => validateActiveSnapshot(snapshot, server));
  return snapshots;
}

async function waitPageGeneration(page, server, timeoutMs) {
  await page.waitForFunction((expectedGeneration, expectedEpoch) => {
    try {
      const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
      return state?.version === expectedGeneration && state?.latestVersion === expectedGeneration
        && state?.baselineEpoch === expectedEpoch && state?.latestEpoch === expectedEpoch
        && Boolean(window.ApiClient?.accessToken?.());
    } catch { return false; }
  }, { timeout: timeoutMs, polling: 500 }, server.generation, server.epoch);
}

async function attemptConvergence(pages, server, before, timeoutMs = 150000) {
  const outcomes = [];
  for (let index = 0; index < pages.length; index += 1) {
    const { page, name } = pages[index];
    if (index > 0) {
      await page.bringToFront();
      await page.waitForFunction(() => document.visibilityState === 'visible', { timeout: 10000 });
    }
    try {
      await waitPageGeneration(page, server, timeoutMs);
      outcomes.push({ name, converged: true });
    } catch (error) {
      outcomes.push({ name, converged: false, error: error.message });
    }
  }
  const after = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
  const converged = outcomes.every((outcome) => outcome.converged);
  if (converged) {
    for (const snapshot of after) {
      validateActiveSnapshot(snapshot, server);
      const prior = before.find((item) => item.name === snapshot.name);
      assert.notEqual(snapshot.documentId, prior?.documentId, `${snapshot.name} did not reload for generation change`);
    }
  }
  return { converged, outcomes, after };
}

async function fetchServerState() {
  const publicResponse = await fetch(`${origin}/System/Info/Public?_=${Date.now()}`, {
    cache: 'no-store', signal: AbortSignal.timeout(5000),
  });
  if (!publicResponse.ok) return null;
  const publicData = await publicResponse.json();
  const serverVersion = String(value(publicData, 'Version') || '');
  const generationUrl = `${origin}/RefreshKit/Generation?_=${Date.now()}`;
  const generationResponse = await fetch(generationUrl, {
    cache: 'no-store', signal: AbortSignal.timeout(5000),
  });
  const shellResponse = await fetch(`${origin}/web/index.html?rk-third-party=${Date.now()}`, {
    cache: 'no-store', signal: AbortSignal.timeout(10000),
  });
  if (!generationResponse.ok || !shellResponse.ok) return null;
  const generationData = await generationResponse.json();
  const generation = String(value(generationData, 'CacheKey') || '');
  const epoch = String(value(generationData, 'Epoch') || '');
  const shell = await shellResponse.text();
  assert.match(generation, /^g-[0-9a-f]{16}$/, 'Generation endpoint returned an invalid cache key');
  assert.match(epoch, /^[0-9a-f]{32}$/, 'Generation endpoint returned a missing or invalid process epoch');
  assert.notEqual(epoch, generation, 'process epoch must be independent of generation');
  assert.equal(generation.includes(epoch), false, 'generation must not embed the process epoch');
  assert.equal(generationUrl.includes(epoch), false, 'generation endpoint URL must not embed the process epoch');
  const kitScriptUrls = [...shell.matchAll(/\bsrc=["']([^"']*\/RefreshKit\/kit\.js[^"']*)["']/gi)]
    .map((match) => match[1]);
  assert.equal(kitScriptUrls.length, 1, 'transformed shell does not contain exactly one Refresh Kit asset URL');
  assert.match(kitScriptUrls[0], new RegExp(`[?&]v=${generation.replaceAll('-', '\\-')}(?:&|$)`));
  assert.equal(kitScriptUrls[0].includes(epoch), false, 'Refresh Kit asset URL must not embed the process epoch');
  assert.equal(shell.includes(epoch), false, 'transformed shell HTML must not embed the process epoch');
  return {
    generation,
    epoch,
    serverVersion,
    injected: true,
    generationUrl: `${origin}/RefreshKit/Generation`,
    kitScriptUrl: kitScriptUrls[0],
    shellSha256: createHash('sha256').update(shell).digest('hex'),
  };
}

async function waitForServerState() {
  return waitUntil(() => fetchServerState(), 180000, 1000);
}

async function pluginInventory(id = fixtureId) {
  const { data } = await requestApi('/Plugins');
  const records = Array.isArray(data) ? data : [];
  const expectedName = id === refreshKitId ? 'Jellyfin Refresh Kit' : fixtureName;
  return records
    .filter((record) => (
      String(value(record, 'Id') || '').replaceAll('-', '').toLowerCase() === id
      || String(value(record, 'Name') || '').toLowerCase() === expectedName.toLowerCase()
    ))
    .map((record) => ({
      id: String(value(record, 'Id') || '').replaceAll('-', '').toLowerCase(),
      name: String(value(record, 'Name') || ''),
      version: String(value(record, 'Version') || ''),
      status: statusName(value(record, 'Status')),
      canUninstall: value(record, 'CanUninstall'),
    }));
}

function hasSingleActiveVersion(inventory, version) {
  const active = inventory.filter((record) => record.status === 'Active');
  return active.length === 1
    && active[0].version === version
    && inventory.every((record) => (
      record === active[0] || ['Superseded', 'Disabled', 'Deleted'].includes(record.status)
    ));
}

function validateSingleActiveVersion(inventory, version, context, expectedId = fixtureId) {
  assert.equal(hasSingleActiveVersion(inventory, version), true, `${context}: plugin inventory is not uniquely active at ${version}`);
  const active = inventory.filter((record) => record.status === 'Active');
  assert.equal(active[0].id, expectedId, `${context}: active plugin ID does not match the expected assembly GUID`);
}

function validateFixtureCatalog(data, expectedVersions, context) {
  const packages = Array.isArray(data) ? data : [];
  const matches = packages.filter((record) => (
    String(value(record, 'Id') || value(record, 'Guid') || '').replaceAll('-', '').toLowerCase() === fixtureId
    || String(value(record, 'Name') || '').toLowerCase() === fixtureName.toLowerCase()
  ));
  assert.equal(matches.length, 1, `${context}: catalog did not contain exactly one fixture package`);
  const packageInfo = matches[0];
  assert.equal(String(value(packageInfo, 'Id') || value(packageInfo, 'Guid') || '').replaceAll('-', '').toLowerCase(), fixtureId);
  assert.equal(String(value(packageInfo, 'Name') || ''), fixtureName);
  const versions = Array.isArray(value(packageInfo, 'Versions')) ? value(packageInfo, 'Versions') : [];
  const normalized = versions.map((record) => ({
    version: String(value(record, 'Version') || ''),
    checksum: String(value(record, 'Checksum') || '').toLowerCase(),
    sourceUrl: String(value(record, 'SourceUrl') || ''),
  }));
  assert.deepEqual(
    normalized.map((record) => record.version).sort(),
    [...expectedVersions].sort(),
    `${context}: fixture catalog exposed an unexpected version set`,
  );
  for (const record of normalized) {
    const expectedChecksum = record.version === metadata.baselineVersion
      ? metadata.baselineMd5.toLowerCase()
      : metadata.candidateMd5.toLowerCase();
    assert.equal(record.checksum, expectedChecksum, `${context}: ${record.version} catalog checksum diverged`);
    const release = record.version === metadata.baselineVersion ? 'v1' : 'v2';
    assert.equal(
      record.sourceUrl,
      `http://repository:8080/${target}/third-party/${release}.zip`,
      `${context}: ${record.version} catalog source URL diverged`,
    );
  }
  return normalized;
}

async function waitForInventory(predicate, description) {
  return waitUntil(async () => {
    const inventory = await pluginInventory();
    return predicate(inventory) ? inventory : false;
  }, 45000, 500).catch((error) => {
    throw new Error(`${description}: ${error.message}`);
  });
}

async function diagnostics() {
  const { data } = await requestApi('/RefreshKit/Diagnostics');
  const plugins = Array.isArray(value(data, 'Plugins')) ? value(data, 'Plugins') : [];
  const fixtures = plugins.filter((record) => (
    String(value(record, 'Id') || '').replaceAll('-', '').toLowerCase() === fixtureId
  )).map((fixture) => ({
    version: String(value(fixture, 'Version') || ''),
    status: String(value(fixture, 'Status') || ''),
    loadedModuleIdentity: String(value(fixture, 'LoadedModuleIdentity') || ''),
    assetIdentity: String(value(fixture, 'AssetIdentity') || ''),
    assetFileCount: Number(value(fixture, 'AssetFileCount') || 0),
    assetBytesHashed: Number(value(fixture, 'AssetBytesHashed') || 0),
    assetScanTruncated: Boolean(value(fixture, 'AssetScanTruncated')),
    configurationIdentity: String(value(fixture, 'ConfigurationIdentity') || ''),
    configurationFileCount: Number(value(fixture, 'ConfigurationFileCount') || 0),
    configurationBytesHashed: Number(value(fixture, 'ConfigurationBytesHashed') || 0),
    configurationScanTruncated: Boolean(value(fixture, 'ConfigurationScanTruncated')),
    usingLastKnownPluginRecord: Boolean(value(fixture, 'UsingLastKnownPluginRecord')),
  }));
  return {
    generation: String(value(data, 'Generation') || ''),
    kitVersion: String(value(data, 'KitVersion') || ''),
    fixtures,
    fixture: fixtures[0] || null,
  };
}

function validateRefreshKitDiagnostics(snapshot, generation) {
  assert.equal(snapshot.generation, generation, 'Refresh Kit diagnostics and served generation diverged');
  assert.equal(snapshot.kitVersion, metadata.refreshKitRuntimeVersion, 'unexpected Refresh Kit browser runtime version');
}

function validateFixtureDiagnostics(snapshot, version, generation) {
  validateRefreshKitDiagnostics(snapshot, generation);
  assert.equal(snapshot.fixtures.length, 1, `Refresh Kit diagnostics has ${snapshot.fixtures.length} fixture rows`);
  assert.ok(snapshot.fixture, `Refresh Kit diagnostics omitted active fixture ${version}`);
  assert.equal(snapshot.fixture.version, version);
  assert.equal(snapshot.fixture.status, 'Active');
  assert.match(snapshot.fixture.loadedModuleIdentity, /^[0-9a-f]{32}$/i);
  assert.match(snapshot.fixture.assetIdentity, /^[0-9a-f]{32}$/i);
  assert.ok(snapshot.fixture.assetFileCount >= 3, 'fixture loose HTML/JS/CSS were not fingerprinted');
  assert.ok(snapshot.fixture.assetBytesHashed > 0, 'fixture browser assets contributed no bytes');
  assert.equal(snapshot.fixture.assetScanTruncated, false);
  assert.match(snapshot.fixture.configurationIdentity, /^[0-9a-f]{32}$/i);
  assert.equal(snapshot.fixture.configurationScanTruncated, false);
  assert.equal(snapshot.fixture.usingLastKnownPluginRecord, false);
}

function validateAbsentFixtureDiagnostics(snapshot, generation, context) {
  validateRefreshKitDiagnostics(snapshot, generation);
  assert.equal(snapshot.fixtures.length, 0, `${context}: inactive fixture remained in Refresh Kit diagnostics`);
}

function validateRetainedFixtureIdentity(actual, expected, context) {
  assert.equal(actual.fixtures.length, 1, `${context}: expected exactly one retained fixture diagnostics row`);
  for (const field of [
    'version',
    'loadedModuleIdentity',
    'assetIdentity',
    'assetFileCount',
    'assetBytesHashed',
    'configurationIdentity',
    'configurationFileCount',
    'configurationBytesHashed',
  ]) {
    assert.equal(actual.fixture[field], expected.fixture[field], `${context}: staged ${field} changed before restart`);
  }
}

async function assertStagedStateInvisible(pages, before, serverBefore, diagnosticsBefore, context) {
  const pollSeconds = before.map((snapshot) => Number(snapshot.kit?.pollSeconds));
  assert.equal(
    pollSeconds.every((seconds) => Number.isFinite(seconds) && seconds > 0),
    true,
    `${context}: could not derive every tab's live polling interval`,
  );
  const maximumPollSeconds = Math.max(...pollSeconds);
  const providerTtlMs = 5000;
  const twoPollIntervalsMs = maximumPollSeconds * 2 * 1000;
  const marginMs = 5000;
  const maximumHoldMs = 120000;
  const requestedHoldMs = Math.max(providerTtlMs, twoPollIntervalsMs) + marginMs;
  assert.ok(
    requestedHoldMs <= maximumHoldMs,
    `${context}: required staged hold ${requestedHoldMs}ms exceeds safe cap ${maximumHoldMs}ms`,
  );
  const holdStartedMs = Date.now();
  await sleep(requestedHoldMs);
  const actualHoldMs = Date.now() - holdStartedMs;
  assert.ok(actualHoldMs > providerTtlMs, `${context}: staged hold did not exceed provider TTL`);
  assert.ok(actualHoldMs > twoPollIntervalsMs, `${context}: staged hold did not cover two full browser polls`);
  const serverAfter = await waitForServerState();
  assert.equal(serverAfter.generation, serverBefore.generation, `${context}: staged operation changed generation before restart`);
  assert.equal(serverAfter.epoch, serverBefore.epoch, `${context}: process epoch changed without a restart`);
  assert.equal(serverAfter.serverVersion, serverBefore.serverVersion, `${context}: server identity changed before restart`);
  assert.equal(serverAfter.kitScriptUrl, serverBefore.kitScriptUrl, `${context}: staged operation changed the asset URL before restart`);
  assert.equal(serverAfter.shellSha256, serverBefore.shellSha256, `${context}: staged operation changed shell identity before restart`);
  const after = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
  validateUnchangedPages(before, after, serverBefore, context);
  const diagnosticAfter = await diagnostics();
  validateRefreshKitDiagnostics(diagnosticAfter, serverBefore.generation);
  if (diagnosticsBefore.fixtures.length === 0) {
    validateAbsentFixtureDiagnostics(diagnosticAfter, serverBefore.generation, context);
  } else {
    validateRetainedFixtureIdentity(diagnosticAfter, diagnosticsBefore, context);
  }
  return {
    timing: {
      pollSeconds,
      maximumPollSeconds,
      providerTtlMs,
      twoPollIntervalsMs,
      marginMs,
      maximumHoldMs,
      requestedHoldMs,
      actualHoldMs,
    },
    serverBefore,
    serverAfter,
    pagesBefore: before,
    pagesAfter: after,
    diagnosticsBefore,
    diagnosticsAfter: diagnosticAfter,
  };
}

async function installFixture(version) {
  const query = new URLSearchParams({
    assemblyGuid: metadata.fixtureId,
    version,
    repositoryUrl: metadata.repositoryUrl,
  });
  return requestApi(`/Packages/Installed/${encodeURIComponent(fixtureName)}?${query}`, {
    method: 'POST', expected: [204], timeoutMs: 120000,
  });
}

async function restartServer(result, phase, beforeServer, expectGenerationChange = true) {
  const started = Date.now();
  const restartWindow = {
    phase,
    startedUtc: new Date(started).toISOString(),
    startElapsedMs: started - result.startedMs,
    healthyElapsedMs: null,
    expectGenerationChange,
    before: beforeServer,
    after: null,
  };
  result.restartWindows.push(restartWindow);
  execFileSync('docker', ['restart', container], { stdio: ['ignore', 'ignore', 'inherit'] });
  await waitUntil(async () => {
    try {
      const response = await fetch(`${origin}/System/Info/Public?_=${Date.now()}`, {
        cache: 'no-store', signal: AbortSignal.timeout(3000),
      });
      return response.ok;
    } catch { return false; }
  }, 180000, 1000);
  const afterServer = await waitForServerState();
  assert.equal(afterServer.serverVersion, beforeServer.serverVersion, `${phase}: host version changed across restart`);
  assert.equal(afterServer.generationUrl, beforeServer.generationUrl, `${phase}: generation endpoint URL changed across restart`);
  assert.notEqual(afterServer.epoch, beforeServer.epoch, `${phase}: process epoch did not change across restart`);
  assert.equal(
    result.processEpochs.some((record) => record.epoch === afterServer.epoch),
    false,
    `${phase}: process epoch was reused by a later restart`,
  );
  if (expectGenerationChange) {
    assert.notEqual(afterServer.generation, beforeServer.generation, `${phase}: lifecycle restart did not move generation`);
    assert.notEqual(afterServer.kitScriptUrl, beforeServer.kitScriptUrl, `${phase}: lifecycle restart did not move asset URL`);
    assert.notEqual(afterServer.shellSha256, beforeServer.shellSha256, `${phase}: lifecycle restart did not move shell identity`);
  } else {
    assert.equal(afterServer.generation, beforeServer.generation, `${phase}: no-change restart moved generation`);
    assert.equal(afterServer.kitScriptUrl, beforeServer.kitScriptUrl, `${phase}: epoch leaked into asset identity`);
    assert.equal(afterServer.shellSha256, beforeServer.shellSha256, `${phase}: epoch leaked into HTML identity`);
  }
  result.processEpochs.push({
    phase,
    generation: afterServer.generation,
    epoch: afterServer.epoch,
    shellSha256: afterServer.shellSha256,
    kitScriptUrl: afterServer.kitScriptUrl,
  });
  restartWindow.healthyElapsedMs = Date.now() - result.startedMs;
  restartWindow.after = afterServer;
  return afterServer;
}

function validateHistoricalGenerationReturn(actual, historical, context) {
  assert.equal(actual.generation, historical.generation, `${context}: content-equivalent process did not restore historical generation`);
  assert.equal(actual.kitScriptUrl, historical.kitScriptUrl, `${context}: process epoch leaked into historical asset identity`);
  assert.equal(actual.shellSha256, historical.shellSha256, `${context}: process epoch leaked into historical HTML identity`);
  assert.notEqual(actual.epoch, historical.epoch, `${context}: historical generation reused a process epoch`);
}

function publishCandidateCatalog() {
  const targetDirectory = path.join(stateRoot, 'repository', target, 'third-party');
  const update = path.join(targetDirectory, 'manifest-update.json');
  const active = path.join(targetDirectory, 'manifest.json');
  assert.ok(fs.existsSync(update), 'third-party update catalog is missing');
  fs.copyFileSync(update, `${active}.part`);
  fs.renameSync(`${active}.part`, active);
}

function phaseRecord(result, name, details) {
  const entry = { name, at: new Date().toISOString(), elapsedMs: Date.now() - result.startedMs, ...details };
  result.phases.push(entry);
  console.log(`==> ${target}: third-party ${name}`);
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
  processEpochs: [],
  failures,
};
let browser;

(async () => {
  try {
    const refreshKitInventory = await pluginInventory(refreshKitId);
    validateSingleActiveVersion(
      refreshKitInventory,
      metadata.refreshKitPackageVersion,
      'completed Refresh Kit self-lifecycle handoff',
      refreshKitId,
    );
    const initialServer = await waitForServerState();
    result.processEpochs.push({
      phase: 'initial',
      generation: initialServer.generation,
      epoch: initialServer.epoch,
      shellSha256: initialServer.shellSha256,
      kitScriptUrl: initialServer.kitScriptUrl,
    });
    await requestApi('/Repositories', {
      method: 'POST',
      body: [{ Name: 'Refresh Kit genuine third-party lifecycle lab', Url: metadata.repositoryUrl, Enabled: true }],
      expected: [204],
    });
    const available = await requestApi('/Packages');
    const baselineCatalog = validateFixtureCatalog(
      available.data,
      [metadata.baselineVersion],
      'baseline repository',
    );

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
    const pristine = await preparePrimary(pages, initialServer);
    assert.deepEqual(await pluginInventory(), [], 'fixture was already installed before lifecycle start');
    const pristineDiagnostics = await diagnostics();
    validateAbsentFixtureDiagnostics(pristineDiagnostics, initialServer.generation, 'pristine server');
    phaseRecord(result, 'active-refresh-kit-tabs', {
      server: initialServer,
      pages: pristine,
      diagnostics: pristineDiagnostics,
      refreshKitInventory,
      baselineCatalog,
    });

    const beforeInstall = await preparePrimary(pages, initialServer);
    const installResponse = await installFixture(metadata.baselineVersion);
    const pendingInstall = await waitForInventory(
      (inventory) => inventory.some((record) => record.version === metadata.baselineVersion && record.status === 'Restart'),
      'fixture v1 did not enter restart-pending state',
    );
    const stagedInstall = await assertStagedStateInvisible(
      pages, beforeInstall, initialServer, pristineDiagnostics, 'install-v1 pending restart',
    );
    const installedServer = await restartServer(result, 'third-party-install-v1', initialServer);
    const activeV1 = await waitForInventory(
      (inventory) => hasSingleActiveVersion(inventory, metadata.baselineVersion),
      'fixture v1 did not become active',
    );
    validateSingleActiveVersion(activeV1, metadata.baselineVersion, 'post-install');
    const installedConvergence = await attemptConvergence(pages, installedServer, beforeInstall);
    assert.equal(installedConvergence.converged, true, 'open tabs did not converge after genuine third-party install');
    const v1Diagnostics = await diagnostics();
    validateFixtureDiagnostics(v1Diagnostics, metadata.baselineVersion, installedServer.generation);
    phaseRecord(result, 'install-v1-converged', {
      apiStatus: installResponse.status,
      pendingInventory: pendingInstall,
      stagedBeforeRestart: stagedInstall,
      activeInventory: activeV1,
      server: installedServer,
      diagnostics: v1Diagnostics,
      convergence: installedConvergence,
    });
    await pages[0].page.screenshot({ path: path.join(output, 'fixture-v1.png'), fullPage: false });

    const beforeUpdate = await preparePrimary(pages, installedServer);
    publishCandidateCatalog();
    const updatedCatalog = await requestApi('/Packages');
    const candidateCatalog = validateFixtureCatalog(
      updatedCatalog.data,
      [metadata.baselineVersion, metadata.candidateVersion],
      'candidate repository',
    );
    const updateResponse = await installFixture(metadata.candidateVersion);
    const pendingUpdate = await waitForInventory(
      (inventory) => inventory.some((record) => record.version === metadata.candidateVersion && record.status === 'Restart')
        && inventory.some((record) => record.status === 'Superseded'),
      'fixture v2 update did not expose restart/superseded state',
    );
    const stagedUpdate = await assertStagedStateInvisible(
      pages, beforeUpdate, installedServer, v1Diagnostics, 'update-v2 pending restart',
    );
    const updatedServer = await restartServer(result, 'third-party-update-v2', installedServer);
    assert.notEqual(updatedServer.generation, initialServer.generation, 'fixture v2 generation collapsed to pristine G0');
    const activeV2 = await waitForInventory(
      (inventory) => hasSingleActiveVersion(inventory, metadata.candidateVersion),
      'fixture v2 did not become active',
    );
    validateSingleActiveVersion(activeV2, metadata.candidateVersion, 'post-update');
    const updatedConvergence = await attemptConvergence(pages, updatedServer, beforeUpdate);
    assert.equal(updatedConvergence.converged, true, 'open tabs did not converge after genuine third-party update');
    const v2Diagnostics = await diagnostics();
    validateFixtureDiagnostics(v2Diagnostics, metadata.candidateVersion, updatedServer.generation);
    assert.notEqual(
      v2Diagnostics.fixture.loadedModuleIdentity,
      v1Diagnostics.fixture.loadedModuleIdentity,
      'v1 and v2 loaded assembly identities did not change',
    );
    assert.notEqual(
      v2Diagnostics.fixture.assetIdentity,
      v1Diagnostics.fixture.assetIdentity,
      'v1 and v2 loose browser asset identities did not change',
    );
    phaseRecord(result, 'update-v2-converged', {
      apiStatus: updateResponse.status,
      candidateCatalog,
      pendingInventory: pendingUpdate,
      stagedBeforeRestart: stagedUpdate,
      activeInventory: activeV2,
      server: updatedServer,
      diagnostics: v2Diagnostics,
      convergence: updatedConvergence,
    });
    await pages[0].page.screenshot({ path: path.join(output, 'fixture-v2.png'), fullPage: false });

    const beforeStableRestart = await preparePrimary(pages, updatedServer);
    const stableServer = await restartServer(
      result,
      'third-party-v2-no-change-control',
      updatedServer,
      false,
    );
    const stableInventory = await waitForInventory(
      (inventory) => hasSingleActiveVersion(inventory, metadata.candidateVersion),
      'fixture v2 was not uniquely active after no-change restart',
    );
    for (const { page } of pages) {
      await page.bringToFront();
      await page.waitForFunction(() => document.visibilityState === 'visible', { timeout: 10000 });
      await waitPageGeneration(page, stableServer, 90000);
    }
    const afterStableRestart = await Promise.all(pages.map(({ page, name }) => pageSnapshot(page, name)));
    validateUnchangedPages(beforeStableRestart, afterStableRestart, stableServer, 'no-change restart');
    const stableDiagnostics = await diagnostics();
    validateFixtureDiagnostics(stableDiagnostics, metadata.candidateVersion, stableServer.generation);
    validateRetainedFixtureIdentity(stableDiagnostics, v2Diagnostics, 'no-change restart');
    phaseRecord(result, 'no-change-v2-restart-in-place', {
      server: stableServer,
      inventory: stableInventory,
      pagesBefore: beforeStableRestart,
      pagesAfter: afterStableRestart,
      diagnostics: stableDiagnostics,
    });

    const beforeDisable = await preparePrimary(pages, stableServer);
    const disableResponse = await requestApi(
      `/Plugins/${metadata.fixtureId}/${metadata.candidateVersion}/Disable`,
      { method: 'POST', expected: [204] },
    );
    const additionalDisableResponses = [];
    for (const record of stableInventory) {
      if (record.version === metadata.candidateVersion || ['Deleted', 'Malfunctioned'].includes(record.status)) continue;
      const response = await requestApi(
        `/Plugins/${metadata.fixtureId}/${encodeURIComponent(record.version)}/Disable`,
        { method: 'POST', expected: [204] },
      );
      additionalDisableResponses.push({ version: record.version, status: response.status });
    }
    const pendingDisable = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Restart'),
      'fixture disable did not enter restart-pending state',
    );
    const stagedDisable = await assertStagedStateInvisible(
      pages, beforeDisable, stableServer, stableDiagnostics, 'disable-v2 pending restart',
    );
    const disabledServer = await restartServer(result, 'third-party-disable-v2', stableServer);
    validateHistoricalGenerationReturn(disabledServer, initialServer, 'post-disable G2 to G0');
    const disabledInventory = await waitForInventory(
      (inventory) => inventory.length >= 1
        && inventory.every((record) => ['Disabled', 'Deleted'].includes(record.status)),
      'all installed fixture versions did not remain disabled',
    );
    const disabledConvergence = await attemptConvergence(pages, disabledServer, beforeDisable);
    assert.equal(disabledConvergence.converged, true, 'open tabs did not converge after third-party disable');
    const disabledDiagnostics = await diagnostics();
    validateAbsentFixtureDiagnostics(disabledDiagnostics, disabledServer.generation, 'post-disable');
    phaseRecord(result, 'disable-v2-converged', {
      apiStatus: disableResponse.status,
      additionalApiStatuses: additionalDisableResponses,
      pendingInventory: pendingDisable,
      stagedBeforeRestart: stagedDisable,
      inventory: disabledInventory,
      server: disabledServer,
      diagnostics: disabledDiagnostics,
      convergence: disabledConvergence,
    });

    const beforeEnable = await preparePrimary(pages, disabledServer);
    const enableResponse = await requestApi(
      `/Plugins/${metadata.fixtureId}/${metadata.candidateVersion}/Enable`,
      { method: 'POST', expected: [204] },
    );
    const pendingEnable = await waitForInventory(
      (inventory) => inventory.some((record) => record.status === 'Restart'),
      'fixture enable did not enter restart-pending state',
    );
    const stagedEnable = await assertStagedStateInvisible(
      pages, beforeEnable, disabledServer, disabledDiagnostics, 'enable-v2 pending restart',
    );
    const enabledServer = await restartServer(result, 'third-party-enable-v2', disabledServer);
    validateHistoricalGenerationReturn(enabledServer, updatedServer, 'post-enable G0 to G2');
    const enabledInventory = await waitForInventory(
      (inventory) => hasSingleActiveVersion(inventory, metadata.candidateVersion),
      'fixture did not become active after enable',
    );
    validateSingleActiveVersion(enabledInventory, metadata.candidateVersion, 'post-enable');
    const enabledConvergence = await attemptConvergence(pages, enabledServer, beforeEnable);
    assert.equal(enabledConvergence.converged, true, 'open tabs did not converge automatically after third-party enable');
    const enabledDiagnostics = await diagnostics();
    validateFixtureDiagnostics(enabledDiagnostics, metadata.candidateVersion, enabledServer.generation);
    phaseRecord(result, 'enable-v2-converged', {
      apiStatus: enableResponse.status,
      pendingInventory: pendingEnable,
      stagedBeforeRestart: stagedEnable,
      activeInventory: enabledInventory,
      server: enabledServer,
      diagnostics: enabledDiagnostics,
      convergence: enabledConvergence,
    });

    const beforeUninstall = await preparePrimary(pages, enabledServer);
    const inventoryBeforeUninstall = await pluginInventory();
    const uninstallResponses = [];
    for (const version of [...new Set(inventoryBeforeUninstall.map((record) => record.version).filter(Boolean))]) {
      const response = await requestApi(
        `/Plugins/${metadata.fixtureId}/${encodeURIComponent(version)}`,
        { method: 'DELETE', expected: [204] },
      );
      uninstallResponses.push({ version, status: response.status });
    }
    const pendingUninstall = await waitForInventory(
      (inventory) => inventory.length === 0
        || inventory.every((record) => record.status === 'Deleted'),
      'fixture uninstall did not disappear or enter deleted state before restart',
    );
    const stagedUninstall = await assertStagedStateInvisible(
      pages, beforeUninstall, enabledServer, enabledDiagnostics, 'uninstall pending restart',
    );
    const uninstalledServer = await restartServer(result, 'third-party-uninstall', enabledServer);
    validateHistoricalGenerationReturn(uninstalledServer, initialServer, 'post-uninstall G2 to G0');
    validateHistoricalGenerationReturn(uninstalledServer, disabledServer, 'post-uninstall repeated G0');
    await waitForInventory((inventory) => inventory.length === 0, 'fixture remained installed after uninstall');
    const uninstallConvergence = await attemptConvergence(pages, uninstalledServer, beforeUninstall);
    assert.equal(uninstallConvergence.converged, true, 'open tabs did not converge automatically after third-party uninstall');
    const uninstalledDiagnostics = await diagnostics();
    validateAbsentFixtureDiagnostics(uninstalledDiagnostics, uninstalledServer.generation, 'post-uninstall');
    phaseRecord(result, 'uninstall-converged', {
      apiStatuses: uninstallResponses,
      inventoryBeforeUninstall,
      pendingInventory: pendingUninstall,
      stagedBeforeRestart: stagedUninstall,
      inventory: await pluginInventory(),
      server: uninstalledServer,
      diagnostics: uninstalledDiagnostics,
      convergence: uninstallConvergence,
    });
    await pages[0].page.screenshot({ path: path.join(output, 'fixture-uninstalled.png'), fullPage: false });
    result.completed = true;
  } catch (error) {
    failures.push(error.stack || error.message || String(error));
    console.error(`FAIL ${target} third-party lifecycle: ${failures.at(-1)}`);
  } finally {
    try {
      await requestApi('/Repositories', { method: 'POST', body: [], expected: [204], timeoutMs: 10000 });
      result.repositoryConfigurationRemoved = true;
    } catch (error) {
      result.repositoryConfigurationRemoved = false;
      result.repositoryCleanupError = redactText(error?.message || String(error));
      failures.push(`could not remove third-party repository configuration: ${result.repositoryCleanupError}`);
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
    result.versionFlapWarnings = consoleEvents.filter((event) => /version FLAP|auto-reload REFUSED/i.test(event.text || ''));
    if (result.versionFlapWarnings.length > 0) {
      failures.push(`${result.versionFlapWarnings.length} process-epoch/flap refusal warning(s)`);
    }
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
      failures.push(`${result.unexpectedRefreshKitBrowserErrors.length} unexpected Refresh Kit browser error(s)`);
    }
    fs.writeFileSync(path.join(output, 'console.json'), `${JSON.stringify(consoleEvents, null, 2)}\n`);
    fs.writeFileSync(path.join(output, 'network.json'), `${JSON.stringify(networkEvents, null, 2)}\n`);
    fs.writeFileSync(path.join(output, 'result.json'), `${JSON.stringify(result, null, 2)}\n`);
    if (browser) await browser.close();
  }

  if (failures.length > 0) {
    console.error(
      `RESULT ${target} third-party lifecycle: FAIL (${failures.length} failure(s)) — ${output}/result.json`,
    );
    process.exitCode = 1;
  } else {
    console.log(`RESULT ${target} third-party lifecycle: PASS — ${output}/result.json`);
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 2;
});
