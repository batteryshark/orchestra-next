// Optional browser regression: NODE_PATH=<directory containing playwright> node tests/check_ui_mobile.cjs
// Uses an isolated fixture server; never connects to or changes the operator's fleet.
const { webkit } = require('playwright');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { createServer } = require('node:http');
const { join } = require('node:path');

const long = 'long-project-name-'.repeat(8);
const profile = { id: 1, name: 'General worker', slug: 'worker', provider: 'local', model: long,
  tier: 2, enabled: true, revision: 1, max_concurrency: null };
const group = { group_id: 1, slug: 'general', name: 'General projects', default_cwd: '/projects/' + long, revision: 1 };
const requests = [];
const fixtures = {
  '/api/auth/me': { authenticated: true, kind: 'network', id: 'test' },
  '/api/events': [], '/api/runs': [], '/api/attention': [],
  '/api/profiles': [profile, { ...profile, id: 2, slug: 'disabled', name: 'Disabled worker', enabled: false }],
  '/api/groups': [group, { ...group, group_id: 2, slug: 'empty', name: 'No directory', default_cwd: null, archived: true }],
  '/api/settings': { settings: {}, revision: 1, scheduler: { paused: false } },
  '/api/models': { models: [{ provider: 'local', model: long, efforts: [] }] },
};
const server = createServer(async (req, res) => {
  const path = new URL(req.url, 'http://localhost').pathname;
  if (path.startsWith('/api/')) {
    let raw = '';
    for await (const chunk of req) raw += chunk;
    if (req.method !== 'GET') requests.push({ path, body: JSON.parse(raw) });
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ instance_id: 'mobile-fixture', board_revision: 1, data: fixtures[path] ?? {} }));
    return;
  }
  const file = { '/': 'index.html', '/app.css': 'app.css', '/app.js': 'app.js' }[path];
  if (!file) { res.writeHead(404).end(); return; }
  res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : file.endsWith('.js') ? 'text/javascript' : 'text/html');
  res.setHeader('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");
  res.end(readFileSync(join(__dirname, '../orchestra/ui', file)));
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await webkit.launch();
    for (const [width, height, mobile] of [[320, 568, true], [390, 844, true], [844, 390, true], [1280, 900, false]]) {
      const page = await browser.newPage({ viewport: { width, height }, isMobile: mobile, hasTouch: mobile });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(`http://127.0.0.1:${server.address().port}/#/config/profiles`);
      for (const tab of ['profiles', 'groups']) {
        await page.evaluate(tab => { location.hash = '#/config/' + tab; }, tab);
        const table = page.locator(`#config-${tab} table`);
        await table.waitFor({ state: 'visible' });
        assert.equal(await page.evaluate(() => scrollY), 0, `${tab}: new tab starts halfway down its records`);
        assert.equal(await table.getAttribute('role'), 'table');
        const metrics = await page.evaluate(selector => {
          const table = document.querySelector(selector);
          const rows = [...table.tBodies[0].rows];
          return {
            pageWidth: document.documentElement.clientWidth, pageScroll: document.documentElement.scrollWidth,
            tableDisplay: getComputedStyle(table).display,
            rows: rows.map(row => ({
              width: row.getBoundingClientRect().width,
              pathWidth: row.cells[2].getBoundingClientRect().width,
              actions: [...row.querySelectorAll('button')].map(button => {
                const rect = button.getBoundingClientRect();
                return { left: rect.left, right: rect.right, height: rect.height };
              }),
              labels: [...row.querySelectorAll('.record-label')].map(label => label.textContent),
            })),
          };
        }, `#config-${tab} table`);
        assert(metrics.pageScroll <= metrics.pageWidth + 1, `${tab}: page overflow`);
        if (mobile) {
          assert.equal(metrics.tableDisplay, 'block');
          for (const row of metrics.rows) {
            assert(row.pathWidth > 200, `${tab}: name/path squeezed into a narrow column`);
            assert(row.labels.includes(tab === 'profiles' ? 'Route' : 'Default cwd'));
            for (const action of row.actions) {
              assert(action.left >= 0 && action.right <= width, `${tab}: action needs horizontal scrolling`);
              assert(action.height >= 44, `${tab}: action too small to tap`);
            }
          }
        } else {
          assert.equal(metrics.tableDisplay, 'table', 'desktop must keep its table');
          assert.equal(await table.locator('.record-label').first().isVisible(), false);
        }
        if (tab === 'profiles') {
          await table.locator('[data-action=profile-edit]').first().click();
          const dialog = page.locator('#profile-dialog');
          await dialog.waitFor({ state: 'visible' });
          assert.equal(await dialog.locator('[name=name]').inputValue(), profile.name);
          await dialog.locator('[data-action=dialog-cancel]').click();
        } else {
          await table.locator('[data-action=group-cwd]').first().click();
          await page.locator('#confirm-input').fill('/projects/mobile');
          const request = page.waitForResponse(res => res.request().method() === 'PATCH');
          await page.locator('#confirm-ok').click();
          await request;
          assert.deepEqual(requests.at(-1), { path: '/api/groups/general', body: { expected_revision: 1, cwd: '/projects/mobile' } });
        }
      }
      assert.deepEqual(errors, []);
      // Screenshots are optional; WebKit's capture helper can log its own CSP messages.
      if (process.env.UI_SCREENSHOTS) {
        for (const tab of ['profiles', 'groups']) {
          await page.evaluate(tab => { location.hash = '#/config/' + tab; }, tab);
          await page.locator(`#config-${tab} table`).waitFor({ state: 'visible' });
          await page.screenshot({ path: join(process.env.UI_SCREENSHOTS, `${tab}-${width}.png`) });
        }
      }
      await page.close();
      console.log(`PASS mobile config ${width}×${height}`);
    }
  } finally {
    if (browser) await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
