'use strict';

// Browser + HTTP side of the disposable in-place Jellyfin host-upgrade lab.
// The shell runner provisions one exact immutable Refresh Kit snapshot first;
// this process then keeps cache-enabled documents alive while replacing only
// the Compose service container and preserving its two named volumes.

const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const crypto = require('node:crypto');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const puppeteer = require('puppeteer');

const IMAGE = Object.freeze({
  jf101110: 'jellyfin/jellyfin:10.11.10@sha256:f66273e014b307e4ac46778845ebc1e9ee24b2e57c1fc17d5ec5ac3015649bfa',
  jf101111: 'jellyfin/jellyfin:10.11.11@sha256:aefb67e6a7ff1debdd154a78a7bbb780fd0c873d8639210a7f6a2016ad2b35db',
  jf12rc4: 'jellyfin/jellyfin:12.0-rc4@sha256:db1df1d111c27ba1f10bb8fce6630892f66eb66b12c2b24e79011453ac18b3db',
});

const SCENARIO = Object.freeze({
  jf10: Object.freeze({
    fromImage: IMAGE.jf101110,
    toImage: IMAGE.jf101111,
    fromVersion: '10.11.10',
    toVersion: '10.11.11',
    targetStage: 'net9',
  }),
  jf12: Object.freeze({
    fromImage: IMAGE.jf101111,
    toImage: IMAGE.jf12rc4,
    fromVersion: '10.11.11',
    toVersion: '12.0.0',
    targetStage: 'net10',
  }),
});

const PLUGIN_GUID = '515255fe-3332-49b0-b471-0be58c8221d8';
const GENERATION_PATTERN = /^g-[0-9a-f]{16}$/;
const EPOCH_PATTERN = /^[0-9a-f]{32}$/;
const POLL_CLIENT_COUNTS = Object.freeze([10, 50, 100]);
const MAX_CAPTURE_EVENTS = 20000;
const MAX_POLL_BODY_BYTES = 65536;
const POLL_REQUEST_LIMIT_MS = 20000;
const POLL_WAVE_LIMIT_MS = 30000;
const MEDIA_LIBRARY_NAME = 'Refresh Kit Host Upgrade Media';
const MEDIA_REMOTE_DIR = '/config/rk-host-upgrade-media';
const MEDIA_REMOTE_FILE = `${MEDIA_REMOTE_DIR}/Refresh Kit Host Upgrade Fixture.mp4`;
const MEDIA_FIXTURE_SECONDS = 120;
const PLAYBACK_GATE_REASONS = Object.freeze(['playback_route', 'media_element', 'fullscreen']);

function parseArgs(argv) {
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith('--') || value === undefined) {
      throw new Error(`invalid argument sequence near ${key || '<end>'}`);
    }
    result[key.slice(2)] = value;
  }
  return result;
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
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

function value(record, key) {
  return record?.[key] ?? record?.[`${key[0].toLowerCase()}${key.slice(1)}`];
}

function validateScenarioIdentity(scenario, fromImage, toImage) {
  const expected = SCENARIO[scenario];
  assert.ok(expected, `unsupported host-upgrade scenario ${scenario}`);
  assert.equal(fromImage, expected.fromImage, `${scenario} source image is not the exact pinned reference`);
  assert.equal(toImage, expected.toImage, `${scenario} target image is not the exact pinned reference`);
  assert.notEqual(fromImage, toImage, 'host-upgrade source and target images must differ');
  return expected;
}

function normalizedHeaders(headers) {
  const result = {};
  for (const [key, headerValue] of Object.entries(headers || {})) {
    result[key.toLowerCase()] = Array.isArray(headerValue) ? headerValue.join(', ') : String(headerValue || '');
  }
  return result;
}

function validateGenerationPayload(status, headers, rawBody, expectedEpoch = null) {
  assert.equal(status, 200, `generation response status was ${status}, expected 200`);
  const normalized = normalizedHeaders(headers);
  assert.match(normalized['cache-control'] || '', /(?:^|,)\s*no-store(?:\s*(?:,|$))/i,
    'generation response did not carry Cache-Control: no-store');
  assert.match(normalized['content-type'] || '', /^application\/json(?:\s*;|$)/i,
    'generation response was not JSON');
  assert.ok(Buffer.byteLength(rawBody) <= MAX_POLL_BODY_BYTES, 'generation response exceeded body cap');
  const data = JSON.parse(rawBody);
  const parsed = {
    version: String(value(data, 'Version') || ''),
    buildId: String(value(data, 'BuildId') || ''),
    generation: String(value(data, 'CacheKey') || ''),
    epoch: String(value(data, 'Epoch') || ''),
  };
  assert.match(parsed.version, /^\d+(?:\.\d+){3}$/, 'generation response Version is malformed');
  assert.match(parsed.buildId, /^[0-9a-f]{64}$/, 'generation response BuildId is malformed');
  assert.match(parsed.generation, GENERATION_PATTERN, 'generation response CacheKey is malformed');
  assert.match(parsed.epoch, EPOCH_PATTERN, 'generation response Epoch is malformed');
  if (expectedEpoch !== null) {
    assert.equal(parsed.epoch, expectedEpoch, 'process Epoch changed without a process replacement');
  }
  return parsed;
}

function assertFinalResult(result) {
  assert.equal(result.schemaVersion, 2, 'result schema is not the real-playback evidence schema');
  assert.equal(result.completed, true, 'result did not reach completion');
  assert.deepEqual(result.failures, [], 'result contains failures');
  assert.equal(result.unexpectedRefreshKitBrowserErrors.length, 0,
    'result contains unexpected Refresh Kit browser errors');
  assert.equal(result.versionFlapWarnings.length, 0, 'result contains version-flap/refusal warnings');
  assert.deepEqual(result.multiTab.tabCounts, [1, 2, 10], 'required tab-count sequence is incomplete');
  assert.equal(result.multiTab.finalRoles.length, 10, '10-tab role inventory is incomplete');
  const expectedFinalRoleCounts = {
    'admin-dashboard': 1,
    'admin-config-editor': 1,
    'admin-plugin-dialog': 1,
    'admin-background': 1,
    'viewer-home': 1,
    'viewer-background': 3,
    'viewer-playback': 1,
    'anonymous-login': 1,
  };
  for (const [role, count] of Object.entries(expectedFinalRoleCounts)) {
    assert.equal(result.multiTab.finalRoles.filter((entry) => entry.role === role).length, count,
      `10-tab role inventory count differs for ${role}`);
  }
  assert.ok(result.multiTab.finalRoles.every((entry) => Object.hasOwn(expectedFinalRoleCounts, entry.role)),
    '10-tab role inventory contains an unknown role');
  assert.ok(result.multiTab.finalRoles.some((entry) => entry.role === 'admin-dashboard'));
  assert.ok(result.multiTab.finalRoles.some((entry) => entry.role === 'admin-config-editor'));
  assert.ok(result.multiTab.finalRoles.some((entry) => entry.role === 'admin-plugin-dialog'));
  assert.ok(result.multiTab.finalRoles.some((entry) => entry.role === 'viewer-home'));
  assert.ok(result.multiTab.finalRoles.some((entry) => entry.role === 'viewer-playback'));
  assert.ok(result.multiTab.finalRoles.some((entry) => entry.role === 'anonymous-login'));
  assert.ok(result.multiTab.finalRoles.some((entry) => entry.hiddenAtCheckpoint === true));
  assert.equal(result.browserContexts.length, 3, 'admin, viewer and anonymous contexts were not all exercised');
  assert.deepEqual(result.browserContexts.map((entry) => entry.name), ['admin', 'viewer', 'anonymous'],
    'browser context role order differs');
  assert.notEqual(result.browserContexts[0].userId, result.browserContexts[1].userId,
    'admin and viewer contexts resolved to the same user');
  assert.equal(result.browserContexts[0].userId.replaceAll('-', '').toLowerCase(),
    result.metadata.users.admin.id.replaceAll('-', '').toLowerCase(), 'admin context resolved the wrong user');
  assert.equal(result.browserContexts[1].userId.replaceAll('-', '').toLowerCase(),
    result.metadata.users.viewer.id.replaceAll('-', '').toLowerCase(), 'viewer context resolved the wrong user');
  assert.equal(result.browserContexts[2].authenticated, false, 'anonymous context became authenticated');
  assert.equal(result.dialogSafety.realJellyfinDialog, true, 'real Jellyfin dialog was not proven');
  assert.equal(result.dialogSafety.blockReason, 'dialog', 'dialog did not engage the runtime safety gate');
  assert.equal(result.dialogSafety.cancelledWithoutUninstall, true, 'dialog cancellation was not proven safe');
  assert.equal(result.dialogSafety.inventoryBefore?.[0]?.canUninstall, true,
    'dialog did not target an uninstallable plugin');
  assert.equal(result.editorSafety.blockReason, 'text_entry', 'real configuration editor did not gate reload');
  assert.equal(result.playbackSafety.realMediaPlayback, true, 'real media playback was not proven');
  assert.ok(PLAYBACK_GATE_REASONS.includes(result.playbackSafety.blockReason),
    'playback did not engage a media safety gate');
  assert.ok(result.playbackSafety.progressSeconds >= 0.5, 'real media progress was not retained');
  assert.equal(result.playbackSafety.fixture.remoteFile, MEDIA_REMOTE_FILE,
    'playback fixture is not stored on the preserved /config volume');
  assert.equal(result.playbackSafety.fixture.sha256, result.playbackSafety.fixture.remoteSha256,
    'source playback fixture and preserved media bytes differ');
  assert.equal(result.playbackSafety.currentGenerationHeldWhileLatestAdvanced, true,
    'playback tab did not hold the current generation while observing the new generation');
  assert.equal(result.playbackSafety.documentIdPreservedWhilePlaying, true,
    'playback document changed while the media safety gate was active');
  assert.equal(result.playbackSafety.loadCountDeltaWhilePlaying, 0,
    'playback document reloaded while the media safety gate was active');
  assert.equal(result.playbackSafety.pausedWithoutReload, true,
    'paused playback document reloaded before leaving media');
  assert.equal(result.playbackSafety.exactOneReloadAfterLeave, true,
    'playback tab did not converge exactly once after leaving media');
  assert.deepEqual(result.pollStress.map((wave) => wave.clients), POLL_CLIENT_COUNTS);
  assert.ok(result.pollStress.every((wave) => wave.allResponsesExact === true));
  assert.ok(result.pollStress.every((wave) => wave.maximumInFlight === wave.clients
    && wave.independentSockets === wave.clients && wave.responses.length === wave.clients
    && wave.startBarrierSpreadMs <= 2000));
  assert.equal(
    result.pollStressRegressionLink.method,
    'ConcurrentGenerationReadsShareExactlyOneScanPerInvalidation(int readerCount)',
  );
  assert.equal(result.hostUpgrade.volumesPreserved, true, 'host-upgrade volumes were not preserved');
  assert.equal(result.hostUpgrade.generationChanged, true, 'host upgrade did not publish a new generation');
  assert.equal(result.hostUpgrade.epochChanged, true, 'host upgrade did not rotate the process epoch');
  assert.equal(result.hostUpgrade.configPreserved, true, 'plugin configuration was not preserved');
  assert.equal(result.hostUpgrade.usersPreserved, true, 'authenticated users were not preserved');
  assert.equal(result.hostUpgrade.mediaPreserved, true, 'indexed media was not preserved across host upgrade');
  assert.deepEqual(result.hostUpgrade.mediaBefore, result.playbackSafety.fixture,
    'host-upgrade media source differs from the real playback fixture');
  assert.equal(result.hostUpgrade.mediaAfter.remoteSha256, result.hostUpgrade.mediaBefore.sha256,
    'host-upgrade target media hash differs from the source fixture');
  assert.deepEqual(result.hostUpgrade.mediaAfter.library, result.hostUpgrade.mediaBefore.library,
    'host-upgrade target media library differs from the source library');
  assert.deepEqual(result.hostUpgrade.mediaAfter.item, result.hostUpgrade.mediaBefore.item,
    'host-upgrade target media item differs from the source item');
  assert.equal(result.hostUpgrade.mediaAfter.indexedForViewer, true,
    'host-upgrade target media is not indexed for the exact viewer');
}

function runSelfTest() {
  let assertions = 0;
  const check = (callback) => { callback(); assertions += 1; };
  const throws = (callback) => { assert.throws(callback); assertions += 1; };
  check(() => validateScenarioIdentity('jf10', IMAGE.jf101110, IMAGE.jf101111));
  check(() => validateScenarioIdentity('jf12', IMAGE.jf101111, IMAGE.jf12rc4));
  throws(() => validateScenarioIdentity('jf10', 'jellyfin/jellyfin:10.11.10', IMAGE.jf101111));
  throws(() => validateScenarioIdentity('jf12', IMAGE.jf101111, IMAGE.jf101111));
  const headers = { 'cache-control': 'no-store, no-cache', 'content-type': 'application/json; charset=utf-8' };
  const body = JSON.stringify({
    Version: '1.0.1.0',
    BuildId: 'b'.repeat(64),
    CacheKey: 'g-0123456789abcdef',
    Epoch: '0'.repeat(32),
  });
  check(() => validateGenerationPayload(200, headers, body, '0'.repeat(32)));
  throws(() => validateGenerationPayload(304, headers, body));
  throws(() => validateGenerationPayload(200, { 'content-type': 'application/json' }, body));
  throws(() => validateGenerationPayload(200, headers, body.replace('g-0123456789abcdef', 'bad')));
  throws(() => validateGenerationPayload(200, headers, body, '1'.repeat(32)));
  check(() => assert.equal(pluginStatusName(0), 'Active'));
  check(() => assert.equal(pluginStatusName('-1'), 'Disabled'));
  check(() => assert.equal(pluginStatusName('Restart'), 'Restart'));
  const indexedMedia = {
    Id: 'a'.repeat(32),
    Name: 'Refresh Kit Host Upgrade Fixture',
    Path: MEDIA_REMOTE_FILE,
    Type: 'Movie',
    MediaType: 'Video',
    RunTimeTicks: 1_200_000_000,
    MediaSources: [{ Id: 'source' }],
  };
  check(() => assert.equal(readyIndexedMediaItem({ ...indexedMedia, RunTimeTicks: null }), false));
  check(() => assert.equal(readyIndexedMediaItem({ ...indexedMedia, RunTimeTicks: 899_999_999 }), false));
  check(() => assert.equal(readyIndexedMediaItem({ ...indexedMedia, MediaSources: [] }), false));
  check(() => assert.equal(readyIndexedMediaItem(indexedMedia, 'b'.repeat(32)), false));
  check(() => assert.deepEqual(readyIndexedMediaItem(indexedMedia, 'a'.repeat(32)), {
    id: 'a'.repeat(32),
    name: 'Refresh Kit Host Upgrade Fixture',
    path: MEDIA_REMOTE_FILE,
    type: 'Movie',
    mediaType: 'Video',
    runTimeTicks: 1_200_000_000,
    mediaSourceCount: 1,
  }));
  check(() => {
    const audit = classifyErrors([{
      name: 'self-test',
      console: [{ kind: 'pageerror', type: 'error', text: '[RefreshKit] boom',
        source: 'http://127.0.0.1/RefreshKit/kit.js', stack: '', elapsedMs: 10 }],
      network: [
        { kind: 'requestfailed', url: 'http://127.0.0.1/RefreshKit/Generation',
          error: 'net::ERR_ABORTED', elapsedMs: 10 },
        { kind: 'requestfailed', url: 'http://127.0.0.1/RefreshKit/Generation',
          error: 'net::ERR_CONNECTION_REFUSED', elapsedMs: 10 },
      ],
    }], [{ startElapsedMs: 0, healthyElapsedMs: 20 }]);
    assert.equal(audit.benignNavigationAborts.length, 1);
    assert.equal(audit.expected.length, 1);
    assert.equal(audit.unexpected.length, 1);
  });
  const minimal = {
    schemaVersion: 2,
    completed: true,
    failures: [],
    unexpectedRefreshKitBrowserErrors: [],
    versionFlapWarnings: [],
    metadata: {
      users: {
        admin: { id: 'a'.repeat(32) },
        viewer: { id: 'b'.repeat(32) },
      },
    },
    multiTab: {
      tabCounts: [1, 2, 10],
      finalRoles: [
        { role: 'admin-dashboard', hiddenAtCheckpoint: false },
        { role: 'admin-config-editor', hiddenAtCheckpoint: true },
        { role: 'admin-plugin-dialog', hiddenAtCheckpoint: true },
        { role: 'admin-background', hiddenAtCheckpoint: true },
        { role: 'viewer-home', hiddenAtCheckpoint: true },
        { role: 'viewer-playback', hiddenAtCheckpoint: true },
        { role: 'anonymous-login', hiddenAtCheckpoint: true },
        ...Array.from({ length: 3 }, () => ({ role: 'viewer-background', hiddenAtCheckpoint: true })),
      ],
    },
    browserContexts: [
      { name: 'admin', userId: 'a'.repeat(32), authenticated: true },
      { name: 'viewer', userId: 'b'.repeat(32), authenticated: true },
      { name: 'anonymous', userId: null, authenticated: false },
    ],
    dialogSafety: {
      realJellyfinDialog: true,
      blockReason: 'dialog',
      cancelledWithoutUninstall: true,
      inventoryBefore: [{ canUninstall: true }],
    },
    editorSafety: { blockReason: 'text_entry' },
    playbackSafety: {
      realMediaPlayback: true,
      blockReason: 'media_element',
      progressSeconds: 1,
      fixture: {
        remoteFile: MEDIA_REMOTE_FILE,
        sha256: 'a'.repeat(64),
        remoteSha256: 'a'.repeat(64),
      },
      currentGenerationHeldWhileLatestAdvanced: true,
      documentIdPreservedWhilePlaying: true,
      loadCountDeltaWhilePlaying: 0,
      pausedWithoutReload: true,
      exactOneReloadAfterLeave: true,
    },
    pollStress: POLL_CLIENT_COUNTS.map((clients) => ({
      clients,
      allResponsesExact: true,
      maximumInFlight: clients,
      independentSockets: clients,
      responses: Array.from({ length: clients }, () => ({})),
      startBarrierSpreadMs: 1,
    })),
    pollStressRegressionLink: {
      method: 'ConcurrentGenerationReadsShareExactlyOneScanPerInvalidation(int readerCount)',
    },
    hostUpgrade: {
      volumesPreserved: true,
      generationChanged: true,
      epochChanged: true,
      configPreserved: true,
      usersPreserved: true,
      mediaPreserved: true,
      mediaBefore: {
        remoteFile: MEDIA_REMOTE_FILE,
        sha256: 'a'.repeat(64),
        remoteSha256: 'a'.repeat(64),
        library: { name: MEDIA_LIBRARY_NAME },
        item: { id: 'b'.repeat(32) },
      },
      mediaAfter: {
        remoteSha256: 'a'.repeat(64),
        library: { name: MEDIA_LIBRARY_NAME },
        item: { id: 'b'.repeat(32) },
        indexedForViewer: true,
      },
    },
  };
  minimal.playbackSafety.fixture = minimal.hostUpgrade.mediaBefore;
  check(() => assertFinalResult(minimal));
  throws(() => assertFinalResult({ ...minimal, pollStress: minimal.pollStress.slice(0, 2) }));
  throws(() => assertFinalResult({ ...minimal, hostUpgrade: { ...minimal.hostUpgrade, volumesPreserved: false } }));
  throws(() => assertFinalResult({
    ...minimal,
    playbackSafety: { ...minimal.playbackSafety, exactOneReloadAfterLeave: false },
  }));
  throws(() => assertFinalResult({
    ...minimal,
    hostUpgrade: {
      ...minimal.hostUpgrade,
      mediaAfter: { ...minimal.hostUpgrade.mediaAfter, indexedForViewer: false },
    },
  }));
  throws(() => assertFinalResult({ ...minimal, unexpectedRefreshKitBrowserErrors: [{}] }));
  console.log(`host-upgrade browser self-test: ${assertions}/${assertions} PASS`);
}

if (process.argv.length === 3 && process.argv[2] === '--self-test') {
  runSelfTest();
  process.exit(0);
}

const args = parseArgs(process.argv.slice(2));
const scenario = args.scenario;
const origin = args.origin;
const project = args.project;
const service = args.service;
const composeFile = path.resolve(args.compose || '');
const initialContainer = args.container;
const fromImage = args['from-image'];
const toImage = args['to-image'];
const adminTokenFile = path.resolve(args['admin-token-file'] || '');
const viewerTokenFile = path.resolve(args['viewer-token-file'] || '');
const metadataPath = path.resolve(args.metadata || '');
const net9Stage = path.resolve(args['net9-stage'] || '');
const net10Stage = path.resolve(args['net10-stage'] || '');
const output = path.resolve(args.out || '');
const jellyfinRoot = path.resolve(__dirname, '..');
const artifactRoot = path.join(jellyfinRoot, 'artifacts');
const repoRoot = path.resolve(jellyfinRoot, '..', '..');
const expectedPath = validateScenarioIdentity(scenario, fromImage, toImage);

assert.match(origin || '', /^http:\/\/127\.0\.0\.1:\d+$/);
assert.match(project || '', /^rk-jellyfin-[a-z0-9][a-z0-9_-]*$/);
assert.equal(service, 'host-upgrade');
assert.match(initialContainer || '', /^[0-9a-f]{12,64}$/);
assert.equal(composeFile, path.join(jellyfinRoot, 'docker-compose.yml'));
assert.ok(output.startsWith(`${artifactRoot}${path.sep}`), 'output must remain under e2e/jellyfin/artifacts');
assert.ok(metadataPath.startsWith(`${path.join(jellyfinRoot, '.state')}${path.sep}`),
  'metadata must remain under e2e/jellyfin/.state');
assert.ok(net9Stage.startsWith(`${path.join(repoRoot, 'plugin', '.builds')}${path.sep}`));
assert.ok(net10Stage.startsWith(`${path.join(repoRoot, 'plugin', '.builds')}${path.sep}`));
assert.ok(fs.existsSync(adminTokenFile) && fs.existsSync(viewerTokenFile), 'provisioned token files are missing');
assert.ok(fs.existsSync(metadataPath), 'scenario metadata is missing');
assert.ok(fs.existsSync(path.join(net9Stage, 'Jellyfin.Plugin.RefreshKit.dll')));
assert.ok(fs.existsSync(path.join(net10Stage, 'Jellyfin.Plugin.RefreshKit.dll')));
fs.mkdirSync(output, { recursive: true });

const metadata = JSON.parse(fs.readFileSync(metadataPath, 'utf8'));
assert.equal(metadata.scenario, scenario);
assert.equal(metadata.from.image, fromImage);
assert.equal(metadata.to.image, toImage);
assert.equal(metadata.from.serverVersion, expectedPath.fromVersion);
assert.equal(metadata.to.serverVersion, expectedPath.toVersion);
assert.match(metadata.pluginDirectory || '', /^\/config\/plugins\/Jellyfin Refresh Kit_\d+\.\d+\.\d+\.\d+$/);
assert.match(metadata.users?.admin?.id || '', /^[0-9a-f-]{32,36}$/i);
assert.match(metadata.users?.viewer?.id || '', /^[0-9a-f-]{32,36}$/i);
assert.notEqual(metadata.users.admin.id.toLowerCase(), metadata.users.viewer.id.toLowerCase());
assert.equal(typeof metadata.sourceIdentity?.dirty, 'boolean', 'host-upgrade source-dirty identity is missing');
assert.match(metadata.sourceIdentity?.revision || '', /^[0-9a-f]{40}$/);
assert.match(metadata.sourceIdentity?.treeSha256 || '', /^[0-9a-f]{64}$/);
for (const [stageName, framework] of [['net9', 'net9.0'], ['net10', 'net10.0']]) {
  const stage = metadata.stages?.[stageName];
  assert.equal(stage?.meta?.framework, framework);
  assert.equal(stage?.meta?.sourceDirty, metadata.sourceIdentity.dirty);
  assert.match(stage?.dllSha256 || '', /^[0-9a-f]{64}$/);
  assert.match(stage?.package?.sha256 || '', /^[0-9a-f]{64}$/);
  assert.match(stage?.package?.md5 || '', /^[0-9a-f]{32}$/);
  assert.ok(Number.isSafeInteger(stage?.package?.size) && stage.package.size > 0);
  assert.equal(stage.meta.sourceRevision, metadata.sourceIdentity.revision);
  assert.equal(stage.meta.sourceTreeSha256, metadata.sourceIdentity.treeSha256);
}

const adminToken = fs.readFileSync(adminTokenFile, 'utf8').trim();
const viewerToken = fs.readFileSync(viewerTokenFile, 'utf8').trim();
assert.ok(adminToken && viewerToken && adminToken !== viewerToken);
const adminUser = process.env.RK_LAB_USER || 'rk_admin';
const adminPassword = process.env.RK_LAB_PASSWORD || 'Test669Pw!x';
const viewerUser = process.env.RK_LAB_VIEWER_USER || 'rk_viewer';
const viewerPassword = process.env.RK_LAB_VIEWER_PASSWORD || 'Viewer669Pw!x';
assert.equal(metadata.users.admin.name, adminUser);
assert.equal(metadata.users.viewer.name, viewerUser);

function localFileIdentity(file) {
  const payload = fs.readFileSync(file);
  return {
    size: payload.length,
    sha256: crypto.createHash('sha256').update(payload).digest('hex'),
    md5: crypto.createHash('md5').update(payload).digest('hex'),
  };
}

const snapshotDirectory = path.dirname(net9Stage);
assert.equal(path.dirname(net10Stage), snapshotDirectory);
assert.equal(path.basename(net9Stage), 'stage');
assert.equal(path.basename(net10Stage), 'stage-jf12');
assert.equal(path.basename(snapshotDirectory), metadata.immutableSnapshot);
for (const [stageName, stageDirectory] of [['net9', net9Stage], ['net10', net10Stage]]) {
  const retained = metadata.stages[stageName];
  const dll = localFileIdentity(path.join(stageDirectory, 'Jellyfin.Plugin.RefreshKit.dll'));
  assert.equal(dll.sha256, retained.dllSha256, `${stageName} local DLL differs from metadata`);
  assert.equal(path.basename(retained.package.file), retained.package.file);
  const packagePath = path.join(snapshotDirectory, retained.package.file);
  assert.equal(path.dirname(packagePath), snapshotDirectory);
  const packageIdentity = localFileIdentity(packagePath);
  assert.deepEqual(packageIdentity, {
    size: retained.package.size,
    sha256: retained.package.sha256,
    md5: retained.package.md5,
  }, `${stageName} local package differs from metadata`);
}

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
  return String(raw || '')
    .replace(/([?&](?:token|api.?key|authorization)=)[^&\s]*/ig, '$1<redacted>')
    .replace(/(Token=")[^"]+/ig, '$1<redacted>')
    .slice(0, 8000);
}

function pushBounded(list, entry, capture) {
  if (list.length < MAX_CAPTURE_EVENTS) list.push(entry);
  else capture.truncated = true;
}

function capturePage(page, name, startedMs) {
  const capture = { name, console: [], network: [], truncated: false, page };
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
      ...stamp(), kind: 'pageerror', type: 'error', text: redactText(error?.message || error),
      stack: redactText(error?.stack || ''),
    }, capture);
  });
  page.on('response', (response) => {
    if (response.status() < 400 && !/\/RefreshKit\//i.test(response.url())) return;
    pushBounded(capture.network, {
      ...stamp(), kind: 'response', status: response.status(), url: redactUrl(response.url()),
      fromCache: response.fromCache(), resourceType: response.request().resourceType(),
    }, capture);
  });
  page.on('requestfailed', (request) => {
    pushBounded(capture.network, {
      ...stamp(), kind: 'requestfailed', method: request.method(), url: redactUrl(request.url()),
      error: request.failure()?.errorText || 'unknown', resourceType: request.resourceType(),
    }, capture);
  });
  return capture;
}

function run(command, commandArgs, options = {}) {
  return execFileSync(command, commandArgs, {
    encoding: 'utf8',
    maxBuffer: 16 * 1024 * 1024,
    timeout: options.timeout || 600000,
    stdio: options.stdio || ['ignore', 'pipe', 'pipe'],
    env: options.env || process.env,
  }).trim();
}

function dockerInspect(container) {
  return JSON.parse(run('docker', ['inspect', container]))[0];
}

function assertContainer(container, expectedImage) {
  assert.match(container || '', /^[0-9a-f]{12,64}$/);
  const inspected = dockerInspect(container);
  assert.equal(inspected.Config.Image, expectedImage, 'container is not configured from the exact pinned image');
  assert.equal(inspected.Config.Labels?.['com.docker.compose.project'], project);
  assert.equal(inspected.Config.Labels?.['com.docker.compose.service'], service);
  const expectedDigest = expectedImage.slice(expectedImage.indexOf('@sha256:') + 1);
  const imageInspect = JSON.parse(run('docker', ['image', 'inspect', expectedImage]))[0];
  assert.ok((imageInspect.RepoDigests || []).some((digest) => digest.endsWith(`@${expectedDigest}`)),
    `local image metadata does not contain ${expectedDigest}`);
  const mounts = Object.fromEntries(inspected.Mounts
    .filter((mount) => ['/config', '/cache'].includes(mount.Destination))
    .map((mount) => [mount.Destination, { name: mount.Name, type: mount.Type, rw: mount.RW }]));
  assert.deepEqual(Object.keys(mounts).sort(), ['/cache', '/config']);
  assert.ok(Object.values(mounts).every((mount) => mount.type === 'volume' && mount.rw === true
    && typeof mount.name === 'string' && mount.name.length > 0));
  return {
    containerId: inspected.Id,
    configuredImage: inspected.Config.Image,
    localImageId: inspected.Image,
    repoDigests: imageInspect.RepoDigests || [],
    mounts,
  };
}

function currentContainer(image) {
  const container = run('docker', [
    'compose', '--project-name', project, '-f', composeFile,
    '--profile', 'host-upgrade', 'ps', '-q', service,
  ], { env: { ...process.env, RK_HOST_UPGRADE_IMAGE: image } });
  assert.match(container, /^[0-9a-f]{12,64}$/);
  return container;
}

function composeUp(image) {
  run('docker', [
    'compose', '--project-name', project, '-f', composeFile,
    '--profile', 'host-upgrade', 'up', '-d', '--wait', '--pull', 'never',
    '--no-deps', '--force-recreate', service,
  ], { env: { ...process.env, RK_HOST_UPGRADE_IMAGE: image }, timeout: 900000 });
  const container = currentContainer(image);
  return { container, identity: assertContainer(container, image) };
}

function restartContainer(container) {
  run('docker', ['restart', container], { timeout: 300000 });
}

function remotePluginHash(container) {
  return run('docker', ['exec', container, 'sha256sum',
    `${metadata.pluginDirectory}/Jellyfin.Plugin.RefreshKit.dll`]).split(/\s+/)[0];
}

function retainContainerLog(container, filename) {
  let descriptor;
  try {
    descriptor = fs.openSync(path.join(output, filename), 'w', 0o600);
    execFileSync('docker', ['logs', container], {
      timeout: 120000,
      stdio: ['ignore', descriptor, descriptor],
    });
    return true;
  } catch (error) {
    fs.writeFileSync(path.join(output, `${filename}.error.txt`), `${redactText(error?.message || error)}\n`);
    return false;
  } finally {
    if (descriptor !== undefined) fs.closeSync(descriptor);
  }
}

function replacePluginStage(container, stage) {
  assert.ok([net9Stage, net10Stage].includes(stage), 'replacement stage is not an immutable selected stage');
  run('docker', ['exec', container, 'sh', '-c',
    'rm -rf -- "$1" && mkdir -p -- "$1"',
    'sh', metadata.pluginDirectory]);
  run('docker', ['cp', `${stage}/.`, `${container}:${metadata.pluginDirectory}/`]);
  const expectedHash = metadata.stages[stage === net9Stage ? 'net9' : 'net10'].dllSha256;
  assert.equal(remotePluginHash(container), expectedHash, 'replacement stage DLL checksum differs in container');
}

function authHeader(token) {
  return `MediaBrowser Client="RefreshKit Host Upgrade E2E", Device="Disposable Lab", DeviceId="rk-${project}", Version="1", Token="${token}"`;
}

async function apiRequest(route, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs || 30000);
  try {
    const response = await fetch(`${origin}${route}`, {
      method: options.method || 'GET',
      cache: 'no-store',
      signal: controller.signal,
      headers: {
        Authorization: authHeader(options.token || adminToken),
        ...(options.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const expected = options.expected || [200];
    const text = await response.text();
    assert.ok(expected.includes(response.status),
      `${options.method || 'GET'} ${route} returned ${response.status}, expected ${expected.join('/')}: ${text.slice(0, 500)}`);
    let data = null;
    if (text) data = JSON.parse(text);
    return { status: response.status, data, headers: Object.fromEntries(response.headers.entries()) };
  } finally {
    clearTimeout(timeout);
  }
}

async function generationFetch(expectedEpoch = null) {
  const response = await fetch(`${origin}/RefreshKit/Generation?_=${Date.now()}-${Math.random()}`, {
    cache: 'no-store',
  });
  const body = await response.text();
  return validateGenerationPayload(response.status, Object.fromEntries(response.headers.entries()), body, expectedEpoch);
}

async function publicInfo() {
  const response = await fetch(`${origin}/System/Info/Public?_=${Date.now()}`, { cache: 'no-store' });
  assert.equal(response.status, 200);
  return response.json();
}

async function waitPublicVersion(expectedVersion, timeoutMs = 600000) {
  return waitUntil(async () => {
    const response = await fetch(`${origin}/System/Info/Public?_=${Date.now()}`, { cache: 'no-store' });
    if (!response.ok) return false;
    const data = await response.json();
    return String(value(data, 'Version') || '') === expectedVersion ? data : false;
  }, timeoutMs, 1000);
}

async function waitGenerationAvailable(timeoutMs = 300000) {
  return waitUntil(async () => {
    try { return await generationFetch(); } catch { return false; }
  }, timeoutMs, 1000);
}

async function waitGenerationStatus(expectedStatus, timeoutMs = 180000) {
  return waitUntil(async () => {
    try {
      const response = await fetch(`${origin}/RefreshKit/Generation?_=${Date.now()}`, { cache: 'no-store' });
      return response.status === expectedStatus;
    } catch {
      return false;
    }
  }, timeoutMs, 1000);
}

function pluginStatusName(rawStatus) {
  const exact = String(rawStatus ?? '');
  const numericStatuses = new Map([
    ['1', 'Restart'],
    ['0', 'Active'],
    ['-1', 'Disabled'],
    ['-2', 'NotSupported'],
    ['-3', 'Malfunctioned'],
    ['-4', 'Superseded'],
    ['-5', 'Deleted'],
  ]);
  return numericStatuses.get(exact) || exact;
}

async function pluginInventory() {
  const { data } = await apiRequest('/Plugins');
  assert.ok(Array.isArray(data));
  return data.filter((plugin) => String(value(plugin, 'Id') || '').replaceAll('-', '').toLowerCase()
    === PLUGIN_GUID.replaceAll('-', '').toLowerCase()).map((plugin) => ({
    id: String(value(plugin, 'Id') || ''),
    name: String(value(plugin, 'Name') || ''),
    version: String(value(plugin, 'Version') || ''),
    status: pluginStatusName(value(plugin, 'Status')),
    canUninstall: Boolean(value(plugin, 'CanUninstall')),
  }));
}

async function pluginConfiguration() {
  const { data } = await apiRequest(`/Plugins/${PLUGIN_GUID}/Configuration`);
  return data;
}

function relevantConfig(config) {
  return {
    EnableInjection: Boolean(value(config, 'EnableInjection')),
    EnableThirdPartyStamping: Boolean(value(config, 'EnableThirdPartyStamping')),
    EnableAutoReload: Boolean(value(config, 'EnableAutoReload')),
    PollSeconds: Number(value(config, 'PollSeconds')),
    IdleSeconds: Number(value(config, 'IdleSeconds')),
    ReloadBudget: Number(value(config, 'ReloadBudget')),
    EnableConfigWatching: Boolean(value(config, 'EnableConfigWatching')),
    ConfigWatchExclusions: Array.isArray(value(config, 'ConfigWatchExclusions'))
      ? [...value(config, 'ConfigWatchExclusions')] : [],
    ConfigCooldownMinutes: Number(value(config, 'ConfigCooldownMinutes')),
    DevMode: Boolean(value(config, 'DevMode')),
  };
}

async function diagnostics() {
  const { data } = await apiRequest('/RefreshKit/Diagnostics');
  return data;
}

function remoteFileSha256(container, remotePath) {
  const digest = run('docker', ['exec', container, 'sha256sum', remotePath]).split(/\s+/)[0];
  assert.match(digest, /^[0-9a-f]{64}$/);
  return digest;
}

function normalizedMediaItem(item) {
  const mediaSources = value(item, 'MediaSources');
  return {
    id: String(value(item, 'Id') || ''),
    name: String(value(item, 'Name') || ''),
    path: String(value(item, 'Path') || ''),
    type: String(value(item, 'Type') || ''),
    mediaType: String(value(item, 'MediaType') || ''),
    runTimeTicks: Number(value(item, 'RunTimeTicks') || 0),
    mediaSourceCount: Array.isArray(mediaSources) ? mediaSources.length : 0,
  };
}

function readyIndexedMediaItem(item, expectedId = null) {
  const normalized = normalizedMediaItem(item);
  if (expectedId && normalized.id.replaceAll('-', '').toLowerCase()
    !== expectedId.replaceAll('-', '').toLowerCase()) return false;
  if (normalized.runTimeTicks < 900000000 || normalized.mediaSourceCount < 1) return false;
  return normalized;
}

async function waitIndexedMediaItem(expectedId = null, timeoutMs = 240000) {
  const query = new URLSearchParams({
    Recursive: 'true',
    IncludeItemTypes: 'Movie',
    Fields: 'Path,MediaSources,RunTimeTicks',
    SearchTerm: 'Refresh Kit Host Upgrade Fixture',
    Limit: '10',
  });
  return waitUntil(async () => {
    const { data } = await apiRequest(
      `/Users/${encodeURIComponent(metadata.users.viewer.id)}/Items?${query}`,
      { timeoutMs: 30000, token: viewerToken },
    );
    const items = value(data, 'Items');
    if (!Array.isArray(items)) return false;
    const found = items.find((item) => String(value(item, 'Path') || '') === MEDIA_REMOTE_FILE);
    if (!found) return false;
    return readyIndexedMediaItem(found, expectedId);
  }, timeoutMs, 2000);
}

async function mediaLibraryEvidence() {
  const { data } = await apiRequest('/Library/VirtualFolders');
  assert.ok(Array.isArray(data), 'virtual-folder response is not an array');
  const folder = data.find((entry) => String(value(entry, 'Name') || '') === MEDIA_LIBRARY_NAME);
  assert.ok(folder, 'real media library is absent');
  const locations = value(folder, 'Locations');
  assert.ok(Array.isArray(locations) && locations.includes(MEDIA_REMOTE_DIR),
    'real media library does not retain its /config media path');
  const itemId = String(value(folder, 'ItemId') || '');
  assert.match(itemId, /^[0-9a-f-]{32,36}$/i, 'real media library has no stable item id');
  return {
    name: MEDIA_LIBRARY_NAME,
    itemId,
    locations: [...locations].map(String).sort(),
  };
}

async function preparePlaybackFixture() {
  const mediaDirectory = path.join(jellyfinRoot, '.state', 'host-upgrade-media');
  const mediaFile = path.join(mediaDirectory, 'rk-host-upgrade-120s-v1.mp4');
  const partial = path.join(mediaDirectory, 'rk-host-upgrade-120s-v1.part.mp4');
  fs.mkdirSync(mediaDirectory, { recursive: true });
  if (!fs.existsSync(mediaFile) || fs.statSync(mediaFile).size < 100000) {
    fs.rmSync(partial, { force: true });
    run('ffmpeg', [
      '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
      '-f', 'lavfi', '-i', `testsrc2=duration=${MEDIA_FIXTURE_SECONDS}:size=640x360:rate=24`,
      '-f', 'lavfi', '-i', `sine=frequency=440:sample_rate=48000:duration=${MEDIA_FIXTURE_SECONDS}`,
      '-map_metadata', '-1', '-shortest',
      '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28', '-pix_fmt', 'yuv420p',
      '-threads', '1', '-c:a', 'aac', '-b:a', '96k',
      '-metadata', 'title=Refresh Kit Host Upgrade Fixture',
      '-movflags', '+faststart', partial,
    ], { timeout: 600000 });
    fs.renameSync(partial, mediaFile);
  }
  const localIdentity = localFileIdentity(mediaFile);
  assert.ok(localIdentity.size >= 100000, 'generated playback fixture is unexpectedly small');
  run('docker', ['exec', activeContainer, 'mkdir', '-p', MEDIA_REMOTE_DIR]);
  run('docker', ['cp', mediaFile, `${activeContainer}:${MEDIA_REMOTE_FILE}`]);
  run('docker', ['exec', activeContainer, 'chmod', '0644', MEDIA_REMOTE_FILE]);
  const remoteSha256 = remoteFileSha256(activeContainer, MEDIA_REMOTE_FILE);
  assert.equal(remoteSha256, localIdentity.sha256, 'preserved media copy differs from deterministic fixture');

  const libraryQuery = new URLSearchParams({
    name: MEDIA_LIBRARY_NAME,
    collectionType: 'movies',
    paths: MEDIA_REMOTE_DIR,
    refreshLibrary: 'true',
  });
  await apiRequest(`/Library/VirtualFolders?${libraryQuery}`, { method: 'POST', expected: [204], timeoutMs: 60000 });
  const library = await mediaLibraryEvidence();
  const item = await waitIndexedMediaItem();
  assert.match(item.id, /^[0-9a-f-]{32,36}$/i, 'indexed playback fixture has no usable item id');
  assert.equal(item.name, 'Refresh Kit Host Upgrade Fixture');
  assert.equal(item.path, MEDIA_REMOTE_FILE);
  assert.equal(item.type, 'Movie');
  assert.equal(item.mediaType, 'Video');
  assert.ok(item.runTimeTicks >= 900000000, 'indexed playback fixture duration is too short');
  assert.ok(item.mediaSourceCount >= 1, 'indexed playback fixture has no real media source');
  return {
    deterministicRecipe: 'lavfi-testsrc2+sine-120s-h264-aac-v1',
    localFile: path.basename(mediaFile),
    remoteDirectory: MEDIA_REMOTE_DIR,
    remoteFile: MEDIA_REMOTE_FILE,
    bytes: localIdentity.size,
    sha256: localIdentity.sha256,
    remoteSha256,
    durationSeconds: MEDIA_FIXTURE_SECONDS,
    library,
    item,
  };
}

async function verifyPlaybackFixture(fixture) {
  const remoteSha256 = remoteFileSha256(activeContainer, fixture.remoteFile);
  assert.equal(remoteSha256, fixture.sha256, 'preserved target media bytes differ');
  const library = await mediaLibraryEvidence();
  assert.deepEqual(library, fixture.library, 'preserved target media library identity changed');
  const item = await waitIndexedMediaItem(fixture.item.id);
  assert.deepEqual(item, fixture.item, 'indexed media identity changed across host upgrade');
  return { remoteSha256, library, item, indexedForViewer: true };
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

async function openPlaybackDetails(item, fixture) {
  await item.page.bringToFront();
  let playButton = null;
  for (const route of [`#/details?id=${fixture.item.id}`, `#!/details?id=${fixture.item.id}`]) {
    await item.page.evaluate((hash) => { location.hash = hash; }, route);
    try {
      playButton = await findVisibleElement(item.page, [
        '.btnPlay:not(.hide)', 'button.btnPlay', 'button[title="Play"]', 'button[aria-label="Play"]',
      ], 20000);
      if (playButton) break;
    } catch {
      // Try the alternate real Jellyfin route used by the other web generation.
    }
  }
  if (!playButton) throw new Error(`play button did not render for fixture ${fixture.item.id}`);
  const details = await item.page.evaluate((expectedId) => {
    const query = location.hash.includes('?') ? location.hash.slice(location.hash.indexOf('?') + 1) : '';
    const actualId = new URLSearchParams(query).get('id') || '';
    return { hash: location.hash, itemId: actualId, expectedId };
  }, fixture.item.id);
  assert.match(details.hash, /\/details(?:\.html)?(?:[?]|$)/i, 'fixture did not open a real details route');
  assert.equal(details.itemId.replaceAll('-', '').toLowerCase(), fixture.item.id.replaceAll('-', '').toLowerCase(),
    'real details route resolved the wrong media item');
  await playButton.dispose();
  return details;
}

async function beginRealPlayback(item, fixture) {
  const details = await openPlaybackDetails(item, fixture);
  const playButton = await findVisibleElement(item.page, [
    '.btnPlay:not(.hide)', 'button.btnPlay', 'button[title="Play"]', 'button[aria-label="Play"]',
  ], 30000);
  await playButton.click();
  await playButton.dispose();
  await item.page.waitForFunction(() => {
    const media = document.querySelector('video');
    return Boolean(media && !media.paused && media.currentTime > 0.5 && media.readyState >= 2);
  }, { timeout: 120000, polling: 250 });
  const currentTimeStart = await item.page.$eval('video', (media) => Number(media.currentTime));
  await sleep(1500);
  const currentTimeEnd = await item.page.$eval('video', (media) => Number(media.currentTime));
  assert.ok(currentTimeEnd >= currentTimeStart + 0.5, 'real media currentTime did not progress');
  const playing = await snapshot(item.page, item.name, item.role);
  assert.equal(playing.media?.paused, false, 'real media is not actively playing');
  assert.ok(playing.media?.readyState >= 2 && playing.media?.videoWidth > 0 && playing.media?.videoHeight > 0,
    'real media did not decode video frames');
  assert.ok(Number.isFinite(playing.media?.duration) && playing.media.duration >= 90 && playing.media.ended === false,
    'real media duration/end state is invalid');
  assert.ok(PLAYBACK_GATE_REASONS.includes(playing.kit?.wouldBlockNow),
    `real playback did not engage a media gate: ${playing.kit?.wouldBlockNow}`);
  return { details, currentTimeStart, currentTimeEnd, progressSeconds: currentTimeEnd - currentTimeStart, playing };
}

async function authenticated(page) {
  return page.evaluate(() => {
    try { return Boolean(window.ApiClient?.accessToken?.()); } catch { return false; }
  });
}

async function clickLoginChoice(page, username) {
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
  }, username);
}

async function login(page, username, password) {
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
    await clickLoginChoice(page, username);
    await sleep(800);
  }
  const usernameField = await page.$('#txtManualName, input[autocomplete="username"], input[name="Username"]');
  if (usernameField) {
    await usernameField.click({ clickCount: 3 });
    await usernameField.type(username, { delay: 8 });
  }
  passwordField = await page.$('#txtManualPassword, input[autocomplete="current-password"], input[type="password"]');
  if (!passwordField) throw new Error(`login password field did not appear for ${username}`);
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

async function snapshot(page, name, role) {
  return page.evaluate(async (pageName, pageRole) => {
    let token = '';
    let currentUser = null;
    try { token = window.ApiClient?.accessToken?.() || ''; } catch {}
    if (token) {
      let user = null;
      try {
        user = await window.ApiClient?.getCurrentUser?.();
      } catch {}
      if (!user) {
        try {
          const userId = await window.ApiClient?.getCurrentUserId?.();
          if (userId) user = await window.ApiClient?.getUser?.(userId);
        } catch {}
      }
      try {
        currentUser = user ? {
          id: String(user.Id ?? user.id ?? ''),
          name: String(user.Name ?? user.name ?? ''),
        } : null;
      } catch {}
    }
    const handle = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin');
    const media = document.querySelector('video');
    return {
      name: pageName,
      role: pageRole,
      url: location.href,
      hash: location.hash,
      visibility: document.visibilityState,
      authenticated: Boolean(token),
      user: currentUser,
      documentId: window.__rkHostUpgradeDocumentId || null,
      loadCount: Number(sessionStorage.getItem('__rkHostUpgradeLoadCount') || 0),
      kit: handle?.state?.() || null,
      media: media ? {
        paused: media.paused,
        currentTime: Number(media.currentTime),
        duration: Number(media.duration),
        readyState: Number(media.readyState),
        ended: media.ended,
        videoWidth: Number(media.videoWidth),
        videoHeight: Number(media.videoHeight),
      } : null,
      activeTag: document.activeElement?.tagName || '',
      activeId: document.activeElement?.id || '',
      renderedDialogs: [...document.querySelectorAll('[role="dialog"]')].filter((node) => {
        try {
          const style = getComputedStyle(node);
          return node.isConnected && style.display !== 'none' && style.visibility !== 'hidden'
            && node.getClientRects().length > 0;
        } catch { return true; }
      }).length,
    };
  }, name, role);
}

function assertSnapshotIdentity(found, expectedServer, expectedUser) {
  assert.ok(found.documentId, `${found.name} has no document id`);
  assert.ok(Number.isInteger(found.loadCount) && found.loadCount >= 1, `${found.name} has no load counter`);
  assert.ok(found.kit, `${found.name} has no Refresh Kit runtime`);
  assert.equal(found.kit.version, expectedServer.generation, `${found.name} current runtime generation is stale`);
  assert.equal(found.kit.latestVersion, expectedServer.generation, `${found.name} latest runtime generation is stale`);
  assert.equal(found.kit.baselineEpoch, expectedServer.epoch, `${found.name} baseline process epoch is stale`);
  assert.equal(found.kit.latestEpoch, expectedServer.epoch, `${found.name} latest process epoch is stale`);
  const roleRoutes = {
    'admin-dashboard': /\/dashboard(?:\.html)?(?:[/?]|$)/i,
    'admin-background': /\/dashboard(?:\.html)?(?:[/?]|$)/i,
    'admin-config-editor': /\/configurationpage(?:[?]|$)/i,
    'admin-plugin-dialog': /\/dashboard\/plugins(?:[/?]|$)/i,
    'viewer-home': /\/home(?:\.html)?(?:[/?]|$)/i,
    'viewer-background': /\/home(?:\.html)?(?:[/?]|$)/i,
    'viewer-playback': /\/(?:home|details|video)(?:\.html)?(?:[/?]|$)/i,
    'anonymous-login': /\/login(?:\.html)?(?:[?]|$)/i,
  };
  assert.match(found.hash, roleRoutes[found.role], `${found.name} is not on its claimed real Jellyfin route`);
  if (expectedUser === null) {
    assert.equal(found.authenticated, false, `${found.name} unexpectedly authenticated`);
    assert.equal(found.user, null, `${found.name} unexpectedly resolved a user`);
    assert.match(found.hash, /\/login(?:\.html)?(?:[?]|$)/i,
      `${found.name} is not on a Jellyfin login route`);
  } else {
    assert.equal(found.authenticated, true, `${found.name} is not authenticated`);
    assert.equal(found.user?.id?.replaceAll('-', '').toLowerCase(), expectedUser.id.replaceAll('-', '').toLowerCase(),
      `${found.name} authenticated as the wrong user id`);
    assert.equal(found.user?.name, expectedUser.name, `${found.name} authenticated as the wrong user name`);
  }
}

async function waitPageConvergence(item, expectedServer, expectedUser, before, exactReloadDelta = 1) {
  await item.page.bringToFront();
  await sleep(400);
  await item.page.waitForFunction(async (expectedGeneration, expectedEpoch, shouldAuthenticate) => {
    try {
      const handle = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin');
      const state = handle?.state?.();
      const token = Boolean(window.ApiClient?.accessToken?.());
      return state?.version === expectedGeneration && state?.latestVersion === expectedGeneration
        && state?.baselineEpoch === expectedEpoch && state?.latestEpoch === expectedEpoch
        && token === shouldAuthenticate;
    } catch { return false; }
  }, { timeout: 180000, polling: 500 }, expectedServer.generation, expectedServer.epoch, expectedUser !== null);
  const after = await snapshot(item.page, item.name, item.role);
  assertSnapshotIdentity(after, expectedServer, expectedUser);
  if (before) {
    assert.equal(after.loadCount, before.loadCount + exactReloadDelta,
      `${item.name} load count did not change by exactly ${exactReloadDelta}`);
    if (exactReloadDelta === 0) assert.equal(after.documentId, before.documentId);
    else assert.notEqual(after.documentId, before.documentId, `${item.name} document id did not rotate`);
  }
  return after;
}

async function newInstrumentedPage(context, name, role, captureList, startedMs) {
  const page = await context.newPage();
  await page.evaluateOnNewDocument(() => {
    try {
      const count = Number(sessionStorage.getItem('__rkHostUpgradeLoadCount') || 0) + 1;
      sessionStorage.setItem('__rkHostUpgradeLoadCount', String(count));
      window.__rkHostUpgradeDocumentId = typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random()}`;
    } catch {
      window.__rkHostUpgradeDocumentId = `${Date.now()}-${Math.random()}`;
    }
  });
  const item = { page, name, role, capture: capturePage(page, name, startedMs) };
  captureList.push(item.capture);
  return item;
}

async function navigate(item, hash, probe) {
  await item.page.goto(`${origin}/web/${hash}`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  if (probe) {
    await item.page.waitForFunction(probe, { timeout: 60000, polling: 300 });
  }
}

let activeContainer = initialContainer;
let mutationSequence = 0;
const generationsSeen = new Set();

function mutateProbe(label) {
  mutationSequence += 1;
  const content = `window.__rkHostUpgradeProbe = ${JSON.stringify(`${scenario}-${mutationSequence}-${label}`)};`;
  const remote = `${metadata.pluginDirectory}/rk-host-upgrade-live-probe.js`;
  run('docker', ['exec', activeContainer, 'sh', '-c',
    'umask 022; printf "%s\\n" "$2" > "$1"', 'sh', remote, content]);
  probeInstalled = true;
  return { sequence: mutationSequence, label, remote, contentBytes: Buffer.byteLength(content) + 1 };
}

function removeProbe() {
  try {
    run('docker', ['exec', activeContainer, 'rm', '-f',
      `${metadata.pluginDirectory}/rk-host-upgrade-live-probe.js`]);
    return true;
  } catch {
    return false;
  }
}

async function mutateAndWait(label, before) {
  const mutation = mutateProbe(label);
  const after = await waitUntil(async () => {
    const state = await generationFetch(before.epoch);
    return state.generation !== before.generation ? state : false;
  }, 120000, 500);
  assert.ok(!generationsSeen.has(after.generation), `${label} reused a generation within one process`);
  generationsSeen.add(after.generation);
  return { mutation, after };
}

function independentGenerationRequest(index, waveId, tracker) {
  return new Promise((resolve, reject) => {
    const started = process.hrtime.bigint();
    tracker.active += 1;
    tracker.maximumActive = Math.max(tracker.maximumActive, tracker.active);
    tracker.startOffsetsMs.push(Number(started - tracker.waveStarted) / 1e6);
    let finished = false;
    const finish = (callback, valueToReturn) => {
      if (finished) return;
      finished = true;
      tracker.active -= 1;
      callback(valueToReturn);
    };
    const url = new URL(`/RefreshKit/Generation?rk-wave=${encodeURIComponent(waveId)}&client=${index}`, origin);
    const request = http.get({
      protocol: url.protocol,
      hostname: url.hostname,
      port: url.port,
      path: `${url.pathname}${url.search}`,
      agent: false,
      headers: { Accept: 'application/json', 'Cache-Control': 'no-cache' },
    }, (response) => {
      const chunks = [];
      let bytes = 0;
      response.on('data', (chunk) => {
        bytes += chunk.length;
        if (bytes > MAX_POLL_BODY_BYTES) {
          request.destroy(new Error('generation response exceeded body cap'));
          return;
        }
        chunks.push(chunk);
      });
      response.on('error', (error) => finish(reject, error));
      response.on('aborted', () => finish(reject, new Error('generation response was aborted')));
      response.on('end', () => {
        const durationMs = Number(process.hrtime.bigint() - started) / 1e6;
        try {
          const body = Buffer.concat(chunks).toString('utf8');
          const parsed = validateGenerationPayload(response.statusCode, response.headers, body);
          finish(resolve, {
            client: index,
            status: response.statusCode,
            durationMs,
            cacheControl: response.headers['cache-control'] || '',
            contentType: response.headers['content-type'] || '',
            connectionReused: Boolean(request.reusedSocket),
            ...parsed,
          });
        } catch (error) {
          finish(reject, error);
        }
      });
    });
    request.setTimeout(POLL_REQUEST_LIMIT_MS, () => request.destroy(new Error('generation request timed out')));
    request.on('error', (error) => finish(reject, error));
  });
}

function percentile(sorted, percentileValue) {
  const index = Math.min(sorted.length - 1, Math.ceil(sorted.length * percentileValue) - 1);
  return sorted[index];
}

async function pollingWave(clients, before, expectedEpoch) {
  const mutation = mutateProbe(`poll-${clients}`);
  // Let the provider's five-second TTL expire before releasing the client
  // barrier. Background tabs intentionally remain realistic, so this live leg
  // claims exact HTTP coherence/timing, not an observed scan count; the linked
  // deterministic provider regression owns the one-scan assertion.
  await sleep(6000);
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const tracker = {
    active: 0,
    maximumActive: 0,
    startOffsetsMs: [],
    waveStarted: process.hrtime.bigint(),
  };
  const tasks = Array.from({ length: clients }, (_, index) => (async () => {
    await gate;
    return independentGenerationRequest(index, `${scenario}-${clients}-${mutation.sequence}`, tracker);
  })());
  const waveStarted = tracker.waveStarted;
  release();
  const responses = await Promise.all(tasks);
  const waveDurationMs = Number(process.hrtime.bigint() - waveStarted) / 1e6;
  assert.ok(waveDurationMs <= POLL_WAVE_LIMIT_MS,
    `${clients}-client polling wave took ${waveDurationMs.toFixed(1)}ms`);
  const signatures = new Set(responses.map((item) => (
    `${item.status}|${item.version}|${item.buildId}|${item.generation}|${item.epoch}`
  )));
  assert.equal(signatures.size, 1, `${clients}-client wave returned inconsistent identities`);
  assert.ok(responses.every((item) => item.epoch === expectedEpoch));
  assert.ok(responses.every((item) => item.status === 200));
  assert.ok(responses.every((item) => /no-store/i.test(item.cacheControl)));
  assert.ok(responses.every((item) => item.durationMs <= POLL_REQUEST_LIMIT_MS));
  assert.ok(responses.every((item) => item.connectionReused === false),
    `${clients}-client wave did not use independent sockets`);
  assert.equal(tracker.maximumActive, clients,
    `${clients}-client wave did not have every request simultaneously in flight`);
  assert.equal(tracker.active, 0, `${clients}-client wave leaked in-flight accounting`);
  const startSpreadMs = Math.max(...tracker.startOffsetsMs) - Math.min(...tracker.startOffsetsMs);
  assert.ok(startSpreadMs <= 2000,
    `${clients}-client start barrier spread ${startSpreadMs.toFixed(1)}ms exceeded 2s`);
  const generation = responses[0].generation;
  assert.notEqual(generation, before.generation, `${clients}-client wave did not publish the mutation`);
  assert.ok(!generationsSeen.has(generation), `${clients}-client wave reused a prior generation`);
  generationsSeen.add(generation);
  const durations = responses.map((item) => item.durationMs).sort((a, b) => a - b);
  return {
    clients,
    mutation,
    generationBefore: before.generation,
    generationAfter: generation,
    epoch: responses[0].epoch,
    allResponsesExact: true,
    independentSockets: responses.filter((item) => !item.connectionReused).length,
    maximumInFlight: tracker.maximumActive,
    startBarrierSpreadMs: startSpreadMs,
    waveDurationMs,
    responses,
    timingMs: {
      min: durations[0],
      p50: percentile(durations, 0.50),
      p95: percentile(durations, 0.95),
      max: durations.at(-1),
      perRequestLimit: POLL_REQUEST_LIMIT_MS,
      waveLimit: POLL_WAVE_LIMIT_MS,
    },
    responseIdentity: {
      status: responses[0].status,
      version: responses[0].version,
      buildId: responses[0].buildId,
      generation,
      epoch: responses[0].epoch,
      cacheControl: responses[0].cacheControl,
      contentType: responses[0].contentType,
    },
  };
}

async function openUninstallDialog(item, pluginRouteId) {
  assert.match(pluginRouteId || '', /^[0-9a-f-]{32,36}$/i);
  await navigate(item, `#/dashboard/plugins/${pluginRouteId}`, () => (
    /Jellyfin Refresh Kit/i.test(document.body?.innerText || '')
  ));
  await item.page.waitForFunction(() => [...document.querySelectorAll('button')].some((button) => {
    const style = getComputedStyle(button);
    return /^uninstall$/i.test(button.textContent?.trim() || '')
      && style.display !== 'none' && style.visibility !== 'hidden' && button.getClientRects().length > 0;
  }), { timeout: 60000, polling: 300 });
  const clicked = await item.page.evaluate(() => {
    const button = [...document.querySelectorAll('button')].find((candidate) => {
      const style = getComputedStyle(candidate);
      return /^uninstall$/i.test(candidate.textContent?.trim() || '')
        && style.display !== 'none' && style.visibility !== 'hidden' && candidate.getClientRects().length > 0;
    });
    button?.click();
    return Boolean(button);
  });
  assert.equal(clicked, true);
  await item.page.waitForFunction(() => [...document.querySelectorAll('[role="dialog"]')].some((dialog) => {
    const style = getComputedStyle(dialog);
    return /uninstall/i.test(dialog.textContent || '') && /Refresh Kit/i.test(dialog.textContent || '')
      && style.display !== 'none' && style.visibility !== 'hidden' && dialog.getClientRects().length > 0;
  }), { timeout: 30000, polling: 200 });
  const state = await snapshot(item.page, item.name, item.role);
  assert.equal(state.renderedDialogs, 1, 'expected one rendered Jellyfin confirmation dialog');
  assert.equal(state.kit?.wouldBlockNow, 'dialog', 'real Jellyfin dialog did not engage the dialog gate');
  return state;
}

async function cancelUninstallDialog(item) {
  const clicked = await item.page.evaluate(() => {
    const dialogs = [...document.querySelectorAll('[role="dialog"]')];
    const dialog = dialogs.find((candidate) => /uninstall/i.test(candidate.textContent || ''));
    if (!dialog) return false;
    const cancel = [...dialog.querySelectorAll('button')].find((button) => /cancel/i.test(button.textContent || ''));
    cancel?.click();
    return Boolean(cancel);
  });
  assert.equal(clicked, true, 'real Jellyfin uninstall dialog had no Cancel action');
  await item.page.waitForFunction(() => ![...document.querySelectorAll('[role="dialog"]')].some((dialog) => {
    try {
      const style = getComputedStyle(dialog);
      return /uninstall/i.test(dialog.textContent || '') && style.display !== 'none'
        && style.visibility !== 'hidden' && dialog.getClientRects().length > 0;
    } catch { return true; }
  }), { timeout: 30000, polling: 200 });
}

async function prepareConfigEditor(item) {
  await navigate(item, '#/configurationpage?name=Jellyfin%20Refresh%20Kit', () => Boolean(
    document.querySelector('#RefreshKitConfigPage #rkConfigExclusions'),
  ));
  const original = await item.page.$eval('#rkConfigExclusions', (field) => field.value);
  await item.page.focus('#rkConfigExclusions');
  await item.page.keyboard.type(' ');
  const found = await snapshot(item.page, item.name, item.role);
  assert.equal(found.activeId, 'rkConfigExclusions', 'real plugin configuration editor is not active');
  assert.equal(found.kit?.wouldBlockNow, 'text_entry', 'real plugin editor did not engage text-entry gate');
  return original;
}

async function releaseConfigEditor(item, original) {
  await item.page.$eval('#rkConfigExclusions', (field, initial) => {
    field.value = initial;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.blur();
  }, original);
}

async function usersStillExist() {
  const { data } = await apiRequest('/Users');
  const identities = new Map(data.map((user) => [
    String(value(user, 'Id') || '').replaceAll('-', '').toLowerCase(),
    String(value(user, 'Name') || ''),
  ]));
  for (const expected of [metadata.users.admin, metadata.users.viewer]) {
    assert.equal(identities.get(expected.id.replaceAll('-', '').toLowerCase()), expected.name,
      `migrated user ${expected.name} is absent or changed identity`);
  }
  return true;
}

async function performHostUpgrade(result, beforeIdentity, beforeServer, configBefore, playbackFixture) {
  const transition = {
    scenario,
    source: beforeIdentity,
    target: null,
    disable: null,
    replacement: null,
    windows: [],
  };
  let processWindowStart = Date.now() - result.startedMs;

  if (scenario === 'jf12') {
    const beforeInventory = await pluginInventory();
    assert.equal(beforeInventory.length, 1, 'cross-line source inventory is not a unique Refresh Kit install');
    const version = beforeInventory[0].version;
    const disableResponse = await apiRequest(
      `/Plugins/${PLUGIN_GUID}/${encodeURIComponent(version)}/Disable`,
      { method: 'POST', expected: [204] },
    );
    const pending = await waitUntil(async () => {
      const inventory = await pluginInventory();
      return inventory.some((plugin) => plugin.status === 'Restart') ? inventory : false;
    }, 60000, 500);
    restartContainer(activeContainer);
    await waitPublicVersion(expectedPath.fromVersion, 300000);
    await waitGenerationStatus(404, 180000);
    const disabledInventory = await waitUntil(async () => {
      const inventory = await pluginInventory();
      return inventory.length === 1 && inventory.every((plugin) => plugin.status === 'Disabled')
        ? inventory : false;
    }, 120000, 1000);
    const disabledAt = Date.now() - result.startedMs;
    const oldHash = remotePluginHash(activeContainer);
    assert.equal(oldHash, metadata.stages.net9.dllSha256);
    replacePluginStage(activeContainer, net10Stage);
    const newHash = remotePluginHash(activeContainer);
    assert.equal(newHash, metadata.stages.net10.dllSha256);
    assert.notEqual(oldHash, newHash, 'net9 and net10 DLLs unexpectedly share one byte identity');
    transition.disable = {
      apiStatus: disableResponse.status,
      generationStatusAfterRestart: 404,
      inventoryBefore: beforeInventory,
      pendingInventory: pending,
      disabledInventory,
    };
    transition.replacement = { fromSha256: oldHash, toSha256: newHash, targetFramework: 'net10.0' };
    transition.windows.push({ kind: 'disable-source-plugin', startElapsedMs: processWindowStart, healthyElapsedMs: disabledAt });
    processWindowStart = Date.now() - result.startedMs;
  }

  transition.sourceLogRetained = retainContainerLog(activeContainer, 'server-before-host-upgrade.log');
  assert.equal(transition.sourceLogRetained, true, 'could not retain source-server log before replacement');
  const upgraded = composeUp(toImage);
  activeContainer = upgraded.container;
  await waitPublicVersion(expectedPath.toVersion, 600000);

  if (scenario === 'jf12') {
    await waitGenerationStatus(404, 180000);
    const migratedDisabled = await waitUntil(async () => {
      const inventory = await pluginInventory();
      return inventory.length === 1 && inventory.every((plugin) => plugin.status === 'Disabled')
        ? inventory : false;
    }, 180000, 1000);
    const version = migratedDisabled[0].version;
    const enable = await apiRequest(
      `/Plugins/${PLUGIN_GUID}/${encodeURIComponent(version)}/Enable`,
      { method: 'POST', expected: [204] },
    );
    const pendingEnable = await waitUntil(async () => {
      const inventory = await pluginInventory();
      return inventory.some((plugin) => plugin.status === 'Restart') ? inventory : false;
    }, 60000, 500);
    restartContainer(activeContainer);
    await waitPublicVersion(expectedPath.toVersion, 300000);
    const enabledInventory = await waitUntil(async () => {
      const inventory = await pluginInventory();
      return inventory.length === 1 && inventory[0].status === 'Active' ? inventory : false;
    }, 180000, 1000);
    transition.enable = {
      migratedDisabledInventory: migratedDisabled,
      generationStatusBeforeEnable: 404,
      apiStatus: enable.status,
      pendingInventory: pendingEnable,
      activeInventory: enabledInventory,
    };
  }

  const afterServer = await waitGenerationAvailable(300000);
  const targetPublic = await publicInfo();
  assert.equal(String(value(targetPublic, 'Version') || ''), expectedPath.toVersion);
  assert.notEqual(afterServer.generation, beforeServer.generation, 'host upgrade did not change host generation');
  assert.notEqual(afterServer.epoch, beforeServer.epoch, 'host upgrade did not rotate process epoch');
  generationsSeen.add(afterServer.generation);
  const configAfter = relevantConfig(await pluginConfiguration());
  assert.deepEqual(configAfter, configBefore, 'Refresh Kit configuration changed during host upgrade');
  await usersStillExist();
  const finalInventory = await pluginInventory();
  assert.equal(finalInventory.length, 1, 'target inventory is not a unique Refresh Kit install');
  assert.equal(finalInventory[0].name, 'Jellyfin Refresh Kit');
  assert.equal(finalInventory[0].status, 'Active', 'Refresh Kit is not active after host upgrade');
  assert.equal(finalInventory[0].canUninstall, true, 'target Refresh Kit is not an uninstallable external plugin');
  assert.equal(finalInventory[0].version, metadata.stages[expectedPath.targetStage].meta.version,
    'target plugin inventory version differs from the immutable stage');
  transition.finalInventory = finalInventory;
  transition.target = upgraded.identity;
  transition.windows.push({
    kind: 'host-image-replacement',
    startElapsedMs: processWindowStart,
    healthyElapsedMs: Date.now() - result.startedMs,
  });
  assert.deepEqual(upgraded.identity.mounts, beforeIdentity.mounts,
    'Compose host upgrade did not preserve exact /config and /cache volume identities');
  const targetHash = remotePluginHash(activeContainer);
  assert.equal(targetHash, metadata.stages[expectedPath.targetStage].dllSha256,
    'active target plugin DLL differs from selected immutable stage');
  const mediaAfter = await verifyPlaybackFixture(playbackFixture);
  return {
    transition,
    afterServer,
    targetPublic,
    configAfter,
    targetHash,
    mediaAfter,
  };
}

function classifyErrors(captures, transitionWindows) {
  const consoleEvents = captures.flatMap((capture) => capture.console.map((event) => ({ page: capture.name, ...event })));
  const networkEvents = captures.flatMap((capture) => capture.network.map((event) => ({ page: capture.name, ...event })));
  const benignNavigationAborts = networkEvents.filter((event) => /\/RefreshKit\//i.test(event.url || '')
    && event.kind === 'requestfailed' && /(?:^|:)ERR_ABORTED$/i.test(event.error || ''));
  const refreshKitErrors = [
    ...consoleEvents.filter((event) => (event.type === 'error' || event.kind === 'pageerror'
      || /version check failed/i.test(event.text || ''))
      && /refresh[\s_-]*kit|\/RefreshKit\//i.test(`${event.text || ''}\n${event.source || ''}\n${event.stack || ''}`)),
    ...networkEvents.filter((event) => /\/RefreshKit\//i.test(event.url || '')
      && ((event.kind === 'requestfailed' && !benignNavigationAborts.includes(event))
        || Number(event.status) >= 400)),
  ];
  const inWindow = (event) => transitionWindows.some((window) => (
    Number.isFinite(window.startElapsedMs) && Number.isFinite(window.healthyElapsedMs)
      && event.elapsedMs >= window.startElapsedMs - 1000
      && event.elapsedMs <= window.healthyElapsedMs + 30000
  ));
  const expected = refreshKitErrors.filter((event) => inWindow(event) && (
    (/\/RefreshKit\/(?:Generation|kit\.js)/i.test(event.url || '')
      && (event.kind === 'requestfailed' || [404, 502, 503, 504].includes(Number(event.status))))
    || (/version check failed/i.test(event.text || '')
      && /HTTP (?:404|502|503|504)|Failed to fetch|Load failed|version fetch.*timed out/i.test(event.text || ''))
  ));
  const unexpected = refreshKitErrors.filter((event) => !expected.includes(event));
  const flapWarnings = consoleEvents.filter((event) => /version FLAP|auto-reload REFUSED/i.test(event.text || ''));
  return {
    consoleEvents, networkEvents, benignNavigationAborts,
    refreshKitErrors, expected, unexpected, flapWarnings,
  };
}

const startedMs = Date.now();
const failures = [];
const captures = [];
const result = {
  schemaVersion: 2,
  scenario,
  origin,
  startedMs,
  startedUtc: new Date(startedMs).toISOString(),
  metadata,
  completed: false,
  phases: [],
  browserContexts: [],
  multiTab: { tabCounts: [], finalRoles: [] },
  dialogSafety: null,
  editorSafety: null,
  playbackSafety: null,
  pollStress: [],
  pollStressRegressionLink: {
    layer: 'PluginGenerationProvider cold/invalidation coalescing',
    source: 'plugin/Jellyfin.Plugin.RefreshKit.Tests/ActivePluginGenerationTests.cs',
    class: 'Jellyfin.Plugin.RefreshKit.Tests.ActivePluginGenerationTests',
    method: 'ConcurrentGenerationReadsShareExactlyOneScanPerInvalidation(int readerCount)',
    cases: [10, 50, 100],
    complementaryMiddlewareSource: 'plugin/Jellyfin.Plugin.RefreshKit.Tests/RefreshKitMiddlewareCacheTests.cs',
    complementaryMiddlewareMethod: 'SameTargetConcurrentRequests_AreSingleFlightAndShareInjectedRepresentation()',
    scope: 'The provider regression proves exact scan/content-read coalescing; this live lab proves real HTTP response coherence and bounded completion. The middleware regression defends a separate transformed-shell representation gate.',
  },
  hostUpgrade: null,
  transitionWindows: [],
  failures,
};

let browser;
let probeInstalled = false;

(async () => {
  try {
    const sourceIdentity = assertContainer(initialContainer, fromImage);
    assert.equal(currentContainer(fromImage), initialContainer);
    const sourcePublic = await waitPublicVersion(expectedPath.fromVersion);
    const sourceConfig = relevantConfig(await pluginConfiguration());
    const sourceInventory = await pluginInventory();
    assert.equal(sourceInventory.length, 1);
    assert.equal(sourceInventory[0].name, 'Jellyfin Refresh Kit');
    assert.equal(sourceInventory[0].status, 'Active');
    assert.equal(sourceInventory[0].version, metadata.stages.net9.meta.version);
    assert.equal(sourceInventory[0].canUninstall, true);
    const sourcePluginHash = remotePluginHash(activeContainer);
    assert.equal(sourcePluginHash, metadata.stages.net9.dllSha256);
    const playbackFixture = await preparePlaybackFixture();
    const sourceServer = await waitGenerationAvailable();
    generationsSeen.add(sourceServer.generation);

    browser = await puppeteer.launch({
      executablePath: browserExecutable(),
      headless: true,
      defaultViewport: { width: 1440, height: 1000 },
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });
    const adminContext = browser.defaultBrowserContext();
    const viewerContext = await browser.createBrowserContext();
    const anonymousContext = await browser.createBrowserContext();
    const pages = [];

    const adminDashboard = await newInstrumentedPage(adminContext, 'admin-dashboard', 'admin-dashboard', captures, startedMs);
    pages.push(adminDashboard);
    await navigate(adminDashboard, '#/home');
    await login(adminDashboard.page, adminUser, adminPassword);
    await navigate(adminDashboard, '#/dashboard', () => /Dashboard/i.test(document.body?.innerText || ''));
    await adminDashboard.page.bringToFront();
    const oneBefore = await snapshot(adminDashboard.page, adminDashboard.name, adminDashboard.role);
    assertSnapshotIdentity(oneBefore, sourceServer, metadata.users.admin);
    result.multiTab.tabCounts.push(1);
    const oneMutation = await mutateAndWait('one-tab', sourceServer);
    const oneAfter = await waitPageConvergence(
      adminDashboard, oneMutation.after, metadata.users.admin, oneBefore,
    );
    result.phases.push({
      name: 'one-live-tab-converged', tabs: 1, serverBefore: sourceServer,
      mutation: oneMutation.mutation, serverAfter: oneMutation.after,
      pagesBefore: [oneBefore], pagesAfter: [oneAfter],
    });

    const viewerHome = await newInstrumentedPage(viewerContext, 'viewer-home', 'viewer-home', captures, startedMs);
    pages.push(viewerHome);
    await navigate(viewerHome, '#/home');
    await login(viewerHome.page, viewerUser, viewerPassword);
    await navigate(viewerHome, '#/home');
    await viewerHome.page.bringToFront();
    const twoBefore = await Promise.all(pages.map((item) => snapshot(item.page, item.name, item.role)));
    assertSnapshotIdentity(twoBefore[0], oneMutation.after, metadata.users.admin);
    assertSnapshotIdentity(twoBefore[1], oneMutation.after, metadata.users.viewer);
    result.multiTab.tabCounts.push(2);
    const twoMutation = await mutateAndWait('two-tabs-two-users', oneMutation.after);
    const twoAfter = [];
    twoAfter.push(await waitPageConvergence(adminDashboard, twoMutation.after,
      metadata.users.admin, twoBefore[0]));
    twoAfter.push(await waitPageConvergence(viewerHome, twoMutation.after,
      metadata.users.viewer, twoBefore[1]));
    result.phases.push({
      name: 'two-tabs-two-contexts-users-converged', tabs: 2,
      mutation: twoMutation.mutation, serverBefore: oneMutation.after,
      serverAfter: twoMutation.after, pagesBefore: twoBefore, pagesAfter: twoAfter,
    });

    const adminConfig = await newInstrumentedPage(adminContext, 'admin-config-editor', 'admin-config-editor', captures, startedMs);
    const adminDialog = await newInstrumentedPage(adminContext, 'admin-plugin-dialog', 'admin-plugin-dialog', captures, startedMs);
    const adminBackground = await newInstrumentedPage(adminContext, 'admin-background', 'admin-background', captures, startedMs);
    pages.push(adminConfig, adminDialog, adminBackground);
    await navigate(adminConfig, '#/home');
    await login(adminConfig.page, adminUser, adminPassword);
    await navigate(adminConfig, '#/configurationpage?name=Jellyfin%20Refresh%20Kit', () => Boolean(
      document.querySelector('#RefreshKitConfigPage'),
    ));
    await navigate(adminDialog, '#/home');
    await login(adminDialog.page, adminUser, adminPassword);
    await navigate(adminDialog, '#/dashboard/plugins');
    await navigate(adminBackground, '#/home');
    await login(adminBackground.page, adminUser, adminPassword);
    await navigate(adminBackground, '#/dashboard', () => /Dashboard/i.test(document.body?.innerText || ''));

    for (let index = 1; index <= 3; index += 1) {
      const item = await newInstrumentedPage(
        viewerContext, `viewer-background-${index}`, 'viewer-background', captures, startedMs,
      );
      pages.push(item);
      await navigate(item, '#/home');
      await login(item.page, viewerUser, viewerPassword);
      await navigate(item, '#/home');
    }
    const viewerPlayback = await newInstrumentedPage(
      viewerContext, 'viewer-playback', 'viewer-playback', captures, startedMs,
    );
    pages.push(viewerPlayback);
    await navigate(viewerPlayback, '#/home');
    await login(viewerPlayback.page, viewerUser, viewerPassword);
    await navigate(viewerPlayback, '#/home');
    const playbackDetails = await openPlaybackDetails(viewerPlayback, playbackFixture);
    const anonymousLogin = await newInstrumentedPage(
      anonymousContext, 'anonymous-login', 'anonymous-login', captures, startedMs,
    );
    pages.push(anonymousLogin);
    await navigate(anonymousLogin, '#/login');
    await anonymousLogin.page.waitForFunction(() => (
      /\/login(?:\.html)?(?:[?]|$)/i.test(location.hash)
      && Boolean(document.querySelector(
        '#txtManualName, #txtManualPassword, input[autocomplete="username"], input[autocomplete="current-password"], .cardBox, .btnManual',
      ))
    ), { timeout: 60000, polling: 300 });
    assert.equal(await authenticated(anonymousLogin.page), false);
    await anonymousLogin.page.waitForFunction(() => Boolean(
      window.JellyfinRefreshKit?.get?.('RefreshKitPlugin'),
    ), { timeout: 60000 });
    const anonymousRoute = await snapshot(anonymousLogin.page, anonymousLogin.name, anonymousLogin.role);
    assert.match(anonymousRoute.hash, /\/login(?:\.html)?(?:[?]|$)/i);
    result.multiTab.anonymousLoginRoute = anonymousRoute.hash;
    assert.equal(pages.length, 10);
    result.multiTab.tabCounts.push(10);

    // Establish exact identities before introducing the three real UI gates.
    for (const item of pages) {
      await item.page.bringToFront();
      await item.page.waitForFunction((expected) => {
        const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
        return state?.version === expected && state?.latestVersion === expected;
      }, { timeout: 120000, polling: 500 }, twoMutation.after.generation);
      const found = await snapshot(item.page, item.name, item.role);
      const expectedUser = item.role === 'anonymous-login'
        ? null : (item.name.startsWith('viewer-') ? metadata.users.viewer : metadata.users.admin);
      assertSnapshotIdentity(found, twoMutation.after, expectedUser);
    }

    const originalEditor = await prepareConfigEditor(adminConfig);
    await adminDialog.page.bringToFront();
    const inventoryBeforeDialog = await pluginInventory();
    assert.equal(inventoryBeforeDialog.length, 1);
    assert.equal(inventoryBeforeDialog[0].canUninstall, true,
      'installed Refresh Kit does not expose Jellyfin Web Uninstall');
    const dialogBefore = await openUninstallDialog(adminDialog, inventoryBeforeDialog[0].id);
    const editorBefore = await snapshot(adminConfig.page, adminConfig.name, adminConfig.role);
    const playbackStarted = await beginRealPlayback(viewerPlayback, playbackFixture);
    const tenBefore = await Promise.all(pages.map((item) => snapshot(item.page, item.name, item.role)));
    const playbackBefore = tenBefore.find((entry) => entry.name === viewerPlayback.name);
    assert.ok(playbackBefore?.media && playbackBefore.media.paused === false);
    assert.ok(PLAYBACK_GATE_REASONS.includes(playbackBefore.kit?.wouldBlockNow));
    const tenMutation = await mutateAndWait('ten-tabs-real-gates', twoMutation.after);

    await viewerPlayback.page.bringToFront();
    await viewerPlayback.page.waitForFunction((expectedOld, expectedNew, expectedEpoch, priorTime) => {
      const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
      const media = document.querySelector('video');
      return state?.version === expectedOld && state?.latestVersion === expectedNew
        && state?.baselineEpoch === expectedEpoch && state?.latestEpoch === expectedEpoch
        && media && !media.paused && media.readyState >= 2 && media.currentTime > priorTime;
    }, { timeout: 120000, polling: 250 }, twoMutation.after.generation,
    tenMutation.after.generation, tenMutation.after.epoch, playbackBefore.media.currentTime);
    const playbackGated = await snapshot(viewerPlayback.page, viewerPlayback.name, viewerPlayback.role);
    assert.equal(playbackGated.documentId, playbackBefore.documentId,
      'playing media document reloaded while gated');
    assert.equal(playbackGated.loadCount, playbackBefore.loadCount,
      'playing media load count changed while gated');
    assert.equal(playbackGated.kit?.version, twoMutation.after.generation);
    assert.equal(playbackGated.kit?.latestVersion, tenMutation.after.generation);
    assert.ok(PLAYBACK_GATE_REASONS.includes(playbackGated.kit?.wouldBlockNow),
      `playing media update was not gated: ${playbackGated.kit?.wouldBlockNow}`);
    assert.equal(playbackGated.media?.paused, false);
    assert.ok(playbackGated.media?.readyState >= 2
      && playbackGated.media?.videoWidth > 0 && playbackGated.media?.videoHeight > 0
      && Number.isFinite(playbackGated.media?.duration) && playbackGated.media.duration >= 90
      && playbackGated.media.ended === false, 'real media decode state was lost during the generation change');
    assert.ok(playbackGated.media.currentTime > playbackBefore.media.currentTime,
      'real media did not continue progressing during the generation change');

    await viewerPlayback.page.$eval('video', (media) => media.pause());
    await sleep(7000);
    const playbackPaused = await snapshot(viewerPlayback.page, viewerPlayback.name, viewerPlayback.role);
    assert.equal(playbackPaused.media?.paused, true, 'real media did not pause');
    assert.ok(playbackPaused.media?.readyState >= 2
      && playbackPaused.media?.videoWidth > 0 && playbackPaused.media?.videoHeight > 0
      && Number.isFinite(playbackPaused.media?.duration) && playbackPaused.media.duration >= 90
      && playbackPaused.media.ended === false, 'paused media lost its decoded real-video identity');
    assert.equal(playbackPaused.documentId, playbackBefore.documentId,
      'paused media document reloaded before leaving playback');
    assert.equal(playbackPaused.loadCount, playbackBefore.loadCount,
      'paused media load count changed before leaving playback');
    assert.equal(playbackPaused.kit?.version, twoMutation.after.generation);
    assert.equal(playbackPaused.kit?.latestVersion, tenMutation.after.generation);
    assert.ok(PLAYBACK_GATE_REASONS.includes(playbackPaused.kit?.wouldBlockNow),
      `paused media did not remain conservatively gated: ${playbackPaused.kit?.wouldBlockNow}`);
    await viewerPlayback.page.evaluate(() => { location.hash = '#/home'; });
    const playbackAfterLeave = await waitPageConvergence(
      viewerPlayback, tenMutation.after, metadata.users.viewer, playbackBefore,
    );
    assert.match(playbackAfterLeave.hash, /\/home(?:\.html)?(?:[/?]|$)/i,
      'playback tab did not leave media for the real home route before convergence');

    await adminDialog.page.bringToFront();
    await adminDialog.page.waitForFunction((expectedGeneration, expectedEpoch) => {
      const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
      return state?.latestVersion === expectedGeneration && state?.version !== expectedGeneration
        && state?.latestEpoch === expectedEpoch && state?.baselineEpoch === expectedEpoch;
    }, { timeout: 120000, polling: 500 }, tenMutation.after.generation, tenMutation.after.epoch);
    const dialogGated = await snapshot(adminDialog.page, adminDialog.name, adminDialog.role);
    assert.equal(dialogGated.documentId, dialogBefore.documentId);
    assert.equal(dialogGated.loadCount, dialogBefore.loadCount);
    assert.equal(dialogGated.kit?.wouldBlockNow, 'dialog');

    await adminConfig.page.focus('#rkConfigExclusions');
    await adminConfig.page.bringToFront();
    await adminConfig.page.waitForFunction((expectedGeneration, expectedEpoch) => {
      const state = window.JellyfinRefreshKit?.get?.('RefreshKitPlugin')?.state?.();
      return state?.latestVersion === expectedGeneration && state?.version !== expectedGeneration
        && state?.latestEpoch === expectedEpoch && state?.baselineEpoch === expectedEpoch;
    }, { timeout: 120000, polling: 500 }, tenMutation.after.generation, tenMutation.after.epoch);
    const editorGated = await snapshot(adminConfig.page, adminConfig.name, adminConfig.role);
    assert.equal(editorGated.documentId, editorBefore.documentId);
    assert.equal(editorGated.loadCount, editorBefore.loadCount);
    assert.equal(editorGated.kit?.wouldBlockNow, 'text_entry');

    const tenAfter = [playbackAfterLeave];
    for (const item of pages.filter((entry) => (
      entry !== adminDialog && entry !== adminConfig && entry !== viewerPlayback
    ))) {
      const before = tenBefore.find((entry) => entry.name === item.name);
      const expectedUser = item.role === 'anonymous-login'
        ? null : (item.name.startsWith('viewer-') ? metadata.users.viewer : metadata.users.admin);
      tenAfter.push(await waitPageConvergence(item, tenMutation.after, expectedUser, before));
    }
    await adminDialog.page.bringToFront();
    await cancelUninstallDialog(adminDialog);
    tenAfter.push(await waitPageConvergence(
      adminDialog, tenMutation.after, metadata.users.admin,
      tenBefore.find((entry) => entry.name === adminDialog.name),
    ));
    await adminConfig.page.bringToFront();
    await releaseConfigEditor(adminConfig, originalEditor);
    tenAfter.push(await waitPageConvergence(
      adminConfig, tenMutation.after, metadata.users.admin,
      tenBefore.find((entry) => entry.name === adminConfig.name),
    ));
    const inventoryAfterDialog = await pluginInventory();
    assert.deepEqual(inventoryAfterDialog, inventoryBeforeDialog,
      'cancelling the real uninstall dialog changed plugin inventory');
    result.dialogSafety = {
      realJellyfinDialog: true,
      source: 'Jellyfin Web plugin-detail Uninstall confirmation',
      role: 'dialog',
      blockReason: dialogGated.kit.wouldBlockNow,
      documentIdPreservedWhileOpen: dialogGated.documentId === dialogBefore.documentId,
      loadCountDeltaWhileOpen: dialogGated.loadCount - dialogBefore.loadCount,
      latestGenerationObserved: dialogGated.kit.latestVersion,
      cancelledWithoutUninstall: true,
      inventoryBefore: inventoryBeforeDialog,
      inventoryAfter: inventoryAfterDialog,
    };
    result.editorSafety = {
      source: 'Refresh Kit real Jellyfin configuration page textarea',
      blockReason: editorGated.kit.wouldBlockNow,
      documentIdPreservedWhileEditing: editorGated.documentId === editorBefore.documentId,
      loadCountDeltaWhileEditing: editorGated.loadCount - editorBefore.loadCount,
      convergedAfterBlur: true,
    };
    result.playbackSafety = {
      realMediaPlayback: true,
      source: 'viewer-owned indexed MP4 on a real Jellyfin details/playback route',
      fixture: playbackFixture,
      detailsRoute: playbackDetails,
      playbackDetails: playbackStarted.details,
      progressStartSeconds: playbackStarted.currentTimeStart,
      progressEndSeconds: playbackStarted.currentTimeEnd,
      progressSeconds: playbackStarted.progressSeconds,
      blockReason: playbackGated.kit.wouldBlockNow,
      playing: playbackBefore,
      gated: playbackGated,
      paused: playbackPaused,
      leftPlayback: playbackAfterLeave,
      documentIdPreservedWhilePlaying: playbackGated.documentId === playbackBefore.documentId,
      loadCountDeltaWhilePlaying: playbackGated.loadCount - playbackBefore.loadCount,
      currentGenerationHeldWhileLatestAdvanced: playbackGated.kit.version === twoMutation.after.generation
        && playbackGated.kit.latestVersion === tenMutation.after.generation,
      pausedWithoutReload: playbackPaused.documentId === playbackBefore.documentId
        && playbackPaused.loadCount === playbackBefore.loadCount,
      exactOneReloadAfterLeave: playbackAfterLeave.loadCount === playbackBefore.loadCount + 1
        && playbackAfterLeave.documentId !== playbackBefore.documentId,
    };
    result.phases.push({
      name: 'ten-live-tabs-role-mix-converged', tabs: 10,
      mutation: tenMutation.mutation, serverBefore: twoMutation.after,
      serverAfter: tenMutation.after, pagesBefore: tenBefore,
      gated: { dialog: dialogGated, editor: editorGated, playback: playbackGated },
      pagesAfter: tenAfter,
    });

    // Hide all ten Jellyfin documents behind an uninstrumented control target.
    // This makes the three server-only contention waves deterministic: every
    // tested Jellyfin document has exactly one catch-up reload afterward.
    const control = await browser.newPage();
    await control.goto('about:blank');
    await control.bringToFront();
    const beforeStress = await Promise.all(pages.map((item) => snapshot(item.page, item.name, item.role)));
    assert.ok(beforeStress.every((entry) => entry.visibility === 'hidden'));
    let waveBefore = tenMutation.after;
    for (const clients of POLL_CLIENT_COUNTS) {
      const wave = await pollingWave(clients, waveBefore, tenMutation.after.epoch);
      result.pollStress.push(wave);
      waveBefore = {
        ...waveBefore,
        generation: wave.generationAfter,
        epoch: wave.epoch,
      };
    }
    await control.close();
    const afterStress = [];
    for (const item of pages) {
      const expectedUser = item.role === 'anonymous-login'
        ? null : (item.name.startsWith('viewer-') ? metadata.users.viewer : metadata.users.admin);
      afterStress.push(await waitPageConvergence(
        item, waveBefore, expectedUser,
        beforeStress.find((entry) => entry.name === item.name),
      ));
    }
    const liveDiagnostics = await diagnostics();
    assert.equal(String(value(liveDiagnostics, 'Generation') || ''), waveBefore.generation);
    result.phases.push({
      name: 'generation-poll-stress-and-ten-tab-catch-up',
      waves: result.pollStress, pagesBefore: beforeStress, pagesAfter: afterStress,
      diagnosticsGeneration: String(value(liveDiagnostics, 'Generation') || ''),
    });

    const beforeUpgrade = await Promise.all(pages.map((item) => snapshot(item.page, item.name, item.role)));
    const upgradeControl = await browser.newPage();
    await upgradeControl.goto('about:blank');
    await upgradeControl.bringToFront();
    assert.ok((await Promise.all(pages.map((item) => snapshot(item.page, item.name, item.role))))
      .every((entry) => entry.visibility === 'hidden'));
    const upgrade = await performHostUpgrade(
      result, sourceIdentity, waveBefore, sourceConfig, playbackFixture,
    );
    result.transitionWindows.push(...upgrade.transition.windows);
    await upgradeControl.close();
    const afterUpgrade = [];
    for (const item of pages) {
      const expectedUser = item.role === 'anonymous-login'
        ? null : (item.name.startsWith('viewer-') ? metadata.users.viewer : metadata.users.admin);
      afterUpgrade.push(await waitPageConvergence(
        item, upgrade.afterServer, expectedUser,
        beforeUpgrade.find((entry) => entry.name === item.name),
      ));
    }
    const adminAfter = afterUpgrade.find((entry) => entry.name === 'admin-dashboard');
    const viewerAfter = afterUpgrade.find((entry) => entry.name === 'viewer-home');
    result.browserContexts = [
      { name: 'admin', userId: adminAfter.user.id, userName: adminAfter.user.name, authenticated: true },
      { name: 'viewer', userId: viewerAfter.user.id, userName: viewerAfter.user.name, authenticated: true },
      { name: 'anonymous', userId: null, userName: null, authenticated: false },
    ];
    const visibilityCheckpoint = await Promise.all(pages.map((item) => snapshot(item.page, item.name, item.role)));
    result.multiTab.finalRoles = visibilityCheckpoint.map((entry) => ({
      name: entry.name,
      role: entry.role,
      authenticated: entry.authenticated,
      userId: entry.user?.id || null,
      documentId: entry.documentId,
      loadCount: entry.loadCount,
      hiddenAtCheckpoint: entry.visibility === 'hidden',
    }));
    result.hostUpgrade = {
      path: `${expectedPath.fromVersion} -> ${expectedPath.toVersion}`,
      sourceImage: fromImage,
      targetImage: toImage,
      sourceServer: waveBefore,
      targetServer: upgrade.afterServer,
      sourcePublicVersion: String(value(sourcePublic, 'Version') || ''),
      targetPublicVersion: String(value(upgrade.targetPublic, 'Version') || ''),
      sourceIdentity,
      targetIdentity: upgrade.transition.target,
      transition: upgrade.transition,
      volumesPreserved: true,
      generationChanged: upgrade.afterServer.generation !== waveBefore.generation,
      epochChanged: upgrade.afterServer.epoch !== waveBefore.epoch,
      configBefore: sourceConfig,
      configAfter: upgrade.configAfter,
      configPreserved: JSON.stringify(sourceConfig) === JSON.stringify(upgrade.configAfter),
      usersPreserved: true,
      mediaBefore: playbackFixture,
      mediaAfter: upgrade.mediaAfter,
      mediaPreserved: upgrade.mediaAfter.remoteSha256 === playbackFixture.sha256
        && JSON.stringify(upgrade.mediaAfter.library) === JSON.stringify(playbackFixture.library)
        && JSON.stringify(upgrade.mediaAfter.item) === JSON.stringify(playbackFixture.item)
        && upgrade.mediaAfter.indexedForViewer === true,
      browserCacheEnabled: true,
      openDocumentsBefore: beforeUpgrade,
      openDocumentsAfter: afterUpgrade,
      exactOneReloadPerDocument: afterUpgrade.every((entry) => {
        const before = beforeUpgrade.find((candidate) => candidate.name === entry.name);
        return entry.loadCount === before.loadCount + 1 && entry.documentId !== before.documentId;
      }),
      sourcePluginDllSha256: sourcePluginHash,
      targetPluginDllSha256: upgrade.targetHash,
    };
    assert.equal(result.hostUpgrade.exactOneReloadPerDocument, true,
      'one or more documents did not reload exactly once for the host generation change');
    assert.equal(result.hostUpgrade.mediaPreserved, true,
      'indexed playback fixture did not survive the host replacement exactly');
    result.phases.push({
      name: 'in-place-host-upgrade-converged',
      hostUpgrade: result.hostUpgrade,
    });
    result.completed = true;
  } catch (error) {
    failures.push(error.stack || error.message || String(error));
    console.error(`FAIL ${scenario} host upgrade: ${failures.at(-1)}`);
  } finally {
    result.probeCleanupAttempted = probeInstalled;
    result.probeRemoved = probeInstalled ? removeProbe() : true;
    const errorAudit = classifyErrors(captures, result.transitionWindows);
    result.refreshKitAttributedBrowserErrors = errorAudit.refreshKitErrors;
    result.benignRefreshKitNavigationAborts = errorAudit.benignNavigationAborts;
    result.expectedRefreshKitTransitionErrors = errorAudit.expected;
    result.unexpectedRefreshKitBrowserErrors = errorAudit.unexpected;
    result.versionFlapWarnings = errorAudit.flapWarnings;
    if (result.unexpectedRefreshKitBrowserErrors.length > 0) {
      failures.push(`${result.unexpectedRefreshKitBrowserErrors.length} unexpected Refresh Kit browser error(s)`);
    }
    if (result.versionFlapWarnings.length > 0) {
      failures.push(`${result.versionFlapWarnings.length} version-flap/refusal warning(s)`);
    }
    if (captures.some((capture) => capture.truncated)) {
      failures.push('one or more browser captures reached the evidence cap');
    }
    if (!result.probeRemoved) failures.push('disposable generation probe could not be removed');
    result.captureCounts = Object.fromEntries(captures.map((capture) => [capture.name, {
      console: capture.console.length,
      network: capture.network.length,
      truncated: capture.truncated,
    }]));
    if (result.completed && failures.length === 0) {
      try {
        assertFinalResult(result);
      } catch (error) {
        failures.push(`final result validation failed: ${error.stack || error.message || String(error)}`);
      }
    }
    result.completed = result.completed && failures.length === 0;
    result.finishedUtc = new Date().toISOString();
    result.durationMs = Date.now() - startedMs;
    fs.writeFileSync(path.join(output, 'console.json'), `${JSON.stringify(errorAudit.consoleEvents, null, 2)}\n`);
    fs.writeFileSync(path.join(output, 'network.json'), `${JSON.stringify(errorAudit.networkEvents, null, 2)}\n`);
    fs.writeFileSync(path.join(output, 'result.json'), `${JSON.stringify(result, null, 2)}\n`);
    if (browser) await browser.close();
  }

  if (failures.length === 0) {
    console.log(`RESULT ${scenario} host upgrade: PASS — ${path.join(output, 'result.json')}`);
  } else {
    console.error(`RESULT ${scenario} host upgrade: FAIL (${failures.length}) — ${path.join(output, 'result.json')}`);
    process.exitCode = 1;
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 2;
});
