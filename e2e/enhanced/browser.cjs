'use strict';
const fs = require('node:fs');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const puppeteer = require('../../node_modules/puppeteer');
const [origin, out] = process.argv.slice(2);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const hash = x => crypto.createHash('sha256').update(x).digest('hex');
(async () => {
  const browser = await puppeteer.launch(
      {headless: true, args: ['--no-sandbox'], defaultViewport: {width: 1280, height: 900}});
  const watchdog = setTimeout(() => {
    void browser.close().catch(() => {});
  }, 180000);
  try {
    const p = await browser.newPage();
    const errors = [];
    p.on('pageerror', e => errors.push(e.message));
    const shell = await p.goto(origin + '/web/', {waitUntil: 'networkidle2'});
    const middlewareOrder = shell.headers()['x-rk-fixture-order'];
    assert(
        middlewareOrder?.includes('EnhancedAdoption') &&
        middlewareOrder.includes('SiblingRefresh'));
    await p.waitForSelector('input[type="password"]', {visible: true});
    await p.type('#txtManualName', 'rk_admin');
    await p.type('input[type="password"]', 'ReadinessLab42!');
    await p.click('button[type="submit"]');
    await p.waitForFunction(
        () => window.ApiClient?.getCurrentUserId() && window.JellyfinEnhanced?.currentSettings);
    await p.waitForFunction(() => window.JellyfinRefreshKit?.get('EnhancedAdoption'));
    // Let initial config files settle before the deliberate update below.
    await sleep(25000);
    await p.reload({waitUntil: 'networkidle2'});
    await p.waitForFunction(
        () => window.JellyfinEnhanced?.currentSettings &&
            window.JellyfinRefreshKit?.get('EnhancedAdoption'));
    const initial = await p.evaluate(
        () => ({
          kit: JellyfinRefreshKit.state(),
          entryTags:
              [...document.scripts].filter(s => s.src.includes('/JellyfinEnhanced/script')).length
        }));
    assert.equal(initial.entryTags, 1, 'Enhanced entry is present exactly once');
    assert.equal(initial.kit.instances.EnhancedAdoption.mode, 'auto');
    assert(initial.kit.instanceCount >= 2, 'embedded runtime coexists with another helper runtime');
    const [embedded, standalone] = await Promise.all(
        ['/EnhancedAdoption/kit.js', '/RefreshKit/kit.js'].map(u => fetch(origin + u).then(r => {
          assert(r.ok);
          return r.text()
        })));
    assert.equal(
        hash(embedded), hash(standalone),
        'fixture runtime must match immutable standalone candidate');
    const count = initial.kit.instanceCount;
    await p.evaluate(() => new Promise((resolve, reject) => {
                       const original =
                           document.querySelector('script[data-name="EnhancedAdoption"]');
                       const copy = document.createElement('script');
                       for (const attribute of original.attributes)
                         copy.setAttribute(attribute.name, attribute.value);
                       setTimeout(() => reject(Error('Duplicate runtime did not load')), 15000);
                       copy.onload = resolve;
                       copy.onerror = reject;
                       document.head.appendChild(copy);
                     }));
    assert.equal(
        await p.evaluate(() => JellyfinRefreshKit.state().instanceCount), count,
        'duplicate runtime does not create duplicate adopters');
    // Use the actual form builder from the verified installed Enhanced release;
    // no GPL source is copied into this MIT repository. Artwork/translations and
    // save completion are controlled; form events and navigation remain real.
    const source = await p.evaluate(async () => ApiClient.ajax({
      type: 'GET',
      url: ApiClient.getUrl('/JellyfinEnhanced/js/elsewhere/reviews.js'),
      dataType: 'text'
    }));
    const start = source.indexOf('function createReviewForm(');
    const end = source.indexOf('return form;', start);
    assert(start >= 0 && end > start);
    const formSource = source.slice(start, end + 'return form;'.length) + '\n}';
    await p.evaluate(code => {
      const make =
          new Function('JE', 'escapeHtml', 'starIconHtml', code + ';return createReviewForm;')(
              JellyfinEnhanced, s => s, () => '<span class="je-star-icon-fill">★</span>');
      let form;
      form = make(null, async content => {
        await new Promise((resolve, reject) => {
          window.finishSave = resolve;
          window.failSave = reject
        });
        sessionStorage.setItem('rk-saved-review', content);
        form.remove();
      }, () => form.remove());
      form.style.cssText =
          'position:fixed;inset:100px 100px auto;z-index:999999;background:#222;padding:20px;color:white';
      document.body.appendChild(form);
    }, formSource);
    await p.type('.je-review-textarea', 'My unsaved review must survive an update.');
    await p.click('.je-star-btn[data-value="4"]');
    const before = await p.evaluate(
        () => ({timeOrigin: performance.timeOrigin, kit: JellyfinRefreshKit.state()}));
    await p.evaluate(async () => {
      await ApiClient.ajax({type: 'POST', url: ApiClient.getUrl('/EnhancedAdoption/change')});
      await JellyfinRefreshKit.checkNow()
    });
    await p.waitForFunction(() => JellyfinRefreshKit.get('EnhancedAdoption').state().updatePending);
    await sleep(2500);
    assert.equal(await p.evaluate(() => performance.timeOrigin), before.timeOrigin);
    assert.equal(
        await p.evaluate(() => JellyfinRefreshKit.state().shared.blockReason), 'unsaved_work');
    await p.click('.je-review-submit-btn');
    await p.waitForFunction(() => !!window.finishSave);
    await sleep(1500);
    assert.equal(await p.evaluate(() => performance.timeOrigin), before.timeOrigin);
    await p.evaluate(() => failSave(Error('controlled save failure')));
    await p.waitForFunction(() => !document.querySelector('.je-review-submit-btn').disabled);
    assert.equal(
        await p.$eval('.je-review-textarea', e => e.value),
        'My unsaved review must survive an update.');
    await p.click('.je-review-submit-btn');
    await sleep(250);
    await p.evaluate(() => finishSave());
    await p.waitForFunction(t => performance.timeOrigin !== t, {timeout: 30000}, before.timeOrigin);
    await p.waitForFunction(
        () => window.JellyfinEnhanced?.currentSettings &&
            window.JellyfinRefreshKit?.get('EnhancedAdoption'));
    const after = await p.evaluate(
        () => ({
          saved: sessionStorage.getItem('rk-saved-review'),
          kit: JellyfinRefreshKit.state(),
          entryTags:
              [...document.scripts].filter(s => s.src.includes('/JellyfinEnhanced/script')).length
        }));
    assert.equal(after.saved, 'My unsaved review must survive an update.');
    assert.equal(after.entryTags, 1);
    assert.equal(after.kit.instances.EnhancedAdoption.updatePending, false);
    const injectionSwitch = await p.evaluate(async () => {
      const plugins =
          await ApiClient.ajax({type: 'GET', url: ApiClient.getUrl('/Plugins'), dataType: 'json'});
      const plugin = plugins.find(plugin => plugin.Name === 'Jellyfin Enhanced');
      if (!plugin) throw Error('Enhanced plugin identity missing');
      const url = ApiClient.getUrl('/Plugins/' + plugin.Id + '/Configuration');
      const config = await ApiClient.ajax({type: 'GET', url, dataType: 'json'});
      await ApiClient.ajax({
        type: 'POST',
        url,
        data: JSON.stringify({...config, DisableScriptInjectionMiddleware: true}),
        contentType: 'application/json'
      });
      return {url, config};
    });
    await p.reload({waitUntil: 'networkidle2'});
    assert.equal(
        await p.evaluate(
            () => [...document.scripts]
                      .filter(
                          s => s.src.includes('/JellyfinEnhanced/script') ||
                              s.dataset.name === 'EnhancedAdoption')
                      .length),
        0, 'injection kill switch removes Enhanced and its adopter');
    await p.waitForFunction(() => window.ApiClient?.getCurrentUserId());
    await p.evaluate(
        async ({url, config}) => ApiClient.ajax(
            {type: 'POST', url, data: JSON.stringify(config), contentType: 'application/json'}),
        injectionSwitch);
    await p.reload({waitUntil: 'networkidle2'});
    await p.waitForFunction(
        () => window.JellyfinEnhanced?.currentSettings &&
            window.JellyfinRefreshKit?.get('EnhancedAdoption'));
    assert.equal(
        await p.evaluate(
            () => [...document.scripts]
                      .filter(s => s.src.includes('/JellyfinEnhanced/script'))
                      .length),
        1);
    assert.deepEqual(errors, [], 'no unhandled browser exceptions');
    fs.writeFileSync(
        out,
        JSON.stringify(
            {
              middlewareOrder,
              runtimeSha256: hash(embedded),
              enhancedReviewSourceSha256: hash(source),
              initial,
              before,
              after,
              errors,
              checks: [
                'official Enhanced entry exactly once', 'embedded and standalone runtime identity',
                'multiple runtime instances', 'duplicate runtime deduplication',
                'actual Enhanced draft after blur', 'pending save blocks',
                'failed save retains draft', 'successful save resumes automatic reload',
                'Enhanced reinitializes after reload', 'injection disabled and reenabled'
              ]
            },
            null, 2));
  } finally {
    clearTimeout(watchdog);
    await browser.close()
  }
})().catch(e => {
  console.error(e);
  process.exitCode = 1
});
