'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const test = require('node:test');
const puppeteer = require('puppeteer');

const root = path.resolve(__dirname, '..', '..');
const runtime = fs.readFileSync(path.join(root, 'jellyfin-refresh-kit.js'), 'utf8');

const storageKeys = {
  budget: 'jellyfin-refresh-kit-budget-v1',
  flips: 'jellyfin-refresh-kit-flips-v1',
  left: 'jellyfin-refresh-kit-left-v1',
  recovery: 'jellyfin-refresh-kit-recovery-v1',
  tab: 'jellyfin-refresh-kit-tab-v1',
};

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
    document.querySelector('#retained-login').setAttribute('aria-hidden', 'true');
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
    sessionStorage.setItem(keys.recovery, JSON.stringify(['boot-seed|CloneTest|A']));
    sessionStorage.setItem(keys.budget, JSON.stringify([111]));
    localStorage.setItem(keys.budget, JSON.stringify([222]));
    return {
      token: sessionStorage.getItem(keys.tab),
      left: sessionStorage.getItem(keys.left),
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
  }), storageKeys);
  assert.equal(inherited.token, openerState.token);
  assert.equal(inherited.left, openerState.left);

  await child.evaluate(() => {
    window.JellyfinRefreshKitConfig = { name: 'CloneTest', mode: 'off' };
  });
  await injectRuntime(child);
  await child.waitForFunction(() => window.JellyfinRefreshKit?.get('CloneTest'));

  const freshChild = await child.evaluate((keys) => ({
    token: sessionStorage.getItem(keys.tab),
    flips: sessionStorage.getItem(keys.flips),
    left: sessionStorage.getItem(keys.left),
    recovery: sessionStorage.getItem(keys.recovery),
    sessionBudget: sessionStorage.getItem(keys.budget),
    sharedBudget: localStorage.getItem(keys.budget),
  }), storageKeys);
  assert.ok(freshChild.token);
  assert.notEqual(freshChild.token, openerState.token);
  assert.equal(freshChild.flips, null);
  assert.equal(freshChild.left, null);
  assert.equal(freshChild.recovery, null);
  assert.equal(freshChild.sessionBudget, null);
  assert.equal(freshChild.sharedBudget, JSON.stringify([222]));

  const childToken = freshChild.token;
  await child.evaluate((keys) => {
    sessionStorage.setItem(keys.left, JSON.stringify(['CloneTest|fresh']));
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
    sessionBudget: sessionStorage.getItem(keys.budget),
  }), storageKeys);
  assert.equal(reloadedChild.token, childToken);
  assert.equal(reloadedChild.left, JSON.stringify(['CloneTest|fresh']));
  assert.equal(reloadedChild.sessionBudget, JSON.stringify([333]));
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
