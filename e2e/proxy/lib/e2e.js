// Browser E2E through a reverse proxy:
//   login -> kit instances register -> zero kit-attributed console errors ->
//   bump the generation -> EXACTLY ONE smart reload -> stamped URLs updated.
//
// usage: node e2e.js <port> [prefix]
const puppeteer = require('puppeteer');
const { execFileSync } = require('child_process');

const PORT = process.argv[2];
const PREFIX = process.argv[3] || '';
const BASE = `http://127.0.0.1:${PORT}${PREFIX}`;
const USER = process.env.RK_USER || 'rk_admin';
const PASS = process.env.RK_PASS || 'Test669Pw!x';
const CONTAINER = process.env.RK_CONTAINER;
if (!/^[/a-zA-Z0-9_.-]+$/.test(CONTAINER || '')) {
  throw new Error('RK_CONTAINER must identify the verified project-scoped origin');
}
// A loose client asset whose CONTENT is replaced to move the generation. This
// models a live JS/CSS deployment and remains meaningful when binary identity is
// correctly tied to the assembly loaded in the current server process (merely
// touching or staging a DLL must not publish an update before restart).
const JE_VERSION = process.env.RK_JE_VERSION;
if (!/^\d+(?:\.\d+){3}$/.test(JE_VERSION || '')) {
  throw new Error('RK_JE_VERSION must be the four-part ecosystem-locked Jellyfin Enhanced version exported by run.sh');
}
const BUMP = process.env.RK_BUMP_FILE || `/config/plugins/Jellyfin Enhanced_${JE_VERSION}/rk-e2e-generation.js`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.log(...a);

function bumpGeneration() {
  const marker = `// refresh-kit E2E generation ${Date.now()}-${process.pid}\n`;
  execFileSync('docker', ['exec', '-i', CONTAINER, 'tee', BUMP], {
    input: marker,
    stdio: ['pipe', 'ignore', 'inherit'],
  });
}
function originGeneration() {
  return execFileSync('curl', ['-s', `${BASE}/RefreshKit/Generation.txt`]).toString().trim();
}

(async () => {
  let failures = [];
  const browser = await puppeteer.launch({ headless: 'new', args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 900 });

  const consoleErrors = [];
  page.on('console', (m) => {
    if (m.type() === 'error' || m.type() === 'warning') consoleErrors.push(`[${m.type()}] ${m.text()}`);
  });
  page.on('pageerror', (e) => consoleErrors.push(`[pageerror] ${e.message}`));

  // Count main-frame document loads so a reload is unambiguous.
  let loads = 0;
  await page.evaluateOnNewDocument(() => { window.__rkLoadMark = Date.now(); });
  page.on('load', () => { loads++; });

  log(`--- ${BASE} ---`);
  await page.goto(`${BASE}/web/`, { waitUntil: 'networkidle2', timeout: 60000 });

  // ---- login ----
  try {
    await page.waitForSelector('#txtManualName, .cardBox, button.btnManual', { timeout: 30000 });
    if (!(await page.$('#txtManualName'))) {
      const manual = await page.$('button.btnManual, .btnManual');
      if (manual) await manual.click();
      else {
        const card = await page.$('.cardBox');
        if (card) await card.click();
      }
      await page.waitForSelector('#txtManualName', { timeout: 15000 });
    }
    await page.type('#txtManualName', USER, { delay: 10 });
    await page.type('#txtManualPassword', PASS, { delay: 10 });
    await Promise.all([
      page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 45000 }).catch(() => {}),
      page.keyboard.press('Enter'),
    ]);
    await sleep(4000);
  } catch (e) {
    failures.push(`login flow: ${e.message}`);
  }

  const loggedIn = await page.evaluate(() => !!(window.ApiClient && window.ApiClient.accessToken && window.ApiClient.accessToken()));
  log(loggedIn ? 'PASS login through proxy' : 'FAIL login through proxy');
  if (!loggedIn) failures.push('login');

  // Move focus out of the completed login form. Jellyfin retains that form in
  // the DOM after authentication, including its populated password field; the
  // regression under test is that a retained HIDDEN form must not block every
  // future update. Deliberately do not clear the password here.
  await page.evaluate(() => {
    try { document.activeElement && document.activeElement.blur(); } catch (e) {}
  });
  await page.mouse.click(5, 5).catch(() => {});
  await sleep(1500);

  // ---- kit registered? ----
  await page.waitForFunction(() => !!window.JellyfinRefreshKit, { timeout: 30000 }).catch(() => {});
  const kit = await page.evaluate(() => {
    const k = window.JellyfinRefreshKit;
    if (!k) return null;
    const st = k.state();
    return { version: st.kitVersion, names: Object.keys(st.instances || {}), mode: st.mode, poll: st.pollSeconds };
  });
  if (kit && kit.names && kit.names.length) log(`PASS kit manager present (kit v${kit.version}, mode ${kit.mode}, poll ${kit.poll}s) instances=${JSON.stringify(kit.names)}`);
  else { log(`FAIL kit manager/instances missing: ${JSON.stringify(kit)}`); failures.push('kit registration'); }

  // ---- stamped URLs before ----
  const before = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('script[src], link[rel="stylesheet"][href]').forEach((n) => {
      const u = n.src || n.href;
      if (/[?&](rkv|v)=/.test(u)) out.push(u.replace(location.origin, ''));
    });
    return out;
  });
  log(`stamped URLs before (${before.length}): ${before.slice(0, 4).join(' | ')}`);

  const genBefore = originGeneration();
  log(`generation before: ${genBefore}`);

  // ---- the money test: bump -> exactly one smart reload ----
  loads = 0;
  bumpGeneration();
  log('changed a loose plugin client asset; waiting for the smart reload...');
  let reloaded = false;
  let lastBlock = null;
  for (let i = 0; i < 50; i++) {
    await sleep(1000);
    if (loads >= 1) { reloaded = true; break; }
    if (i % 5 === 0) {
      lastBlock = await page.evaluate(() => {
        try { const s = window.JellyfinRefreshKit.state(); return { pending: s.shared.pendingInstances, block: s.shared.blockReason }; }
        catch (e) { return null; }
      }).catch(() => null);
    }
  }
  if (!reloaded) log(`  (no reload; last gate state ${JSON.stringify(lastBlock)})`);
  await sleep(10000); // give any *extra* reload a chance to show up

  const genAfter = originGeneration();
  log(`generation after: ${genAfter} (${genAfter !== genBefore ? 'changed' : 'UNCHANGED'})`);

  if (reloaded && loads === 1) log(`PASS exactly one smart reload through the proxy (loads=${loads})`);
  else { log(`FAIL reload count = ${loads} (expected exactly 1)`); failures.push(`reload count ${loads}`); }

  const after = await page.evaluate(() => {
    const out = [];
    document.querySelectorAll('script[src], link[rel="stylesheet"][href]').forEach((n) => {
      const u = n.src || n.href;
      if (/[?&](rkv|v)=/.test(u)) out.push(u.replace(location.origin, ''));
    });
    return out;
  });
  const kitTagAfter = after.find((u) => u.includes('/RefreshKit/kit.js')) || '';
  if (kitTagAfter.includes(genAfter)) log(`PASS stamped URLs updated to the new generation (${kitTagAfter})`);
  else { log(`FAIL stamped URL not updated: ${kitTagAfter} vs ${genAfter}`); failures.push('stamp update'); }

  // ---- kit-attributed console noise ----
  const kitErrors = consoleErrors.filter((l) => /refresh.?kit|RefreshKit|rkv=/i.test(l));
  if (kitErrors.length === 0) log('PASS zero kit-attributed console errors/warnings');
  else { log(`FAIL kit-attributed console output:\n  ${kitErrors.join('\n  ')}`); failures.push('kit console errors'); }
  log(`(total page console errors/warnings, all sources: ${consoleErrors.length})`);

  await browser.close();
  log(failures.length === 0 ? `RESULT :${PORT}${PREFIX} E2E PASS` : `RESULT :${PORT}${PREFIX} E2E FAIL -> ${failures.join(', ')}`);
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.log('FATAL', e); process.exit(2); });
