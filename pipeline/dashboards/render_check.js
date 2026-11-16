// Renders a dashboard page with a minimal stand-in for the `dash` API, using the real art/*.json files.
// usage: node render.js <page.html> <artdir> <out.png> [width]
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const [page_, artDir, out, width] = process.argv.slice(2);
const html = fs.readFileSync(page_, 'utf8');
const data = {};
for (const f of fs.readdirSync(artDir)) if (f.endsWith('.json') && f !== 'manifest.json') data[f.replace('.json', '')] = JSON.parse(fs.readFileSync(path.join(artDir, f))).rows;
const d3src = fs.readFileSync(path.join(__dirname, 'node_modules/d3/dist/d3.min.js'), 'utf8');
const shim = `
window.__DATA = ${JSON.stringify(data)};
const draws = {};
window.dash = {
  colors: ['#2a6fdb','#e0533d','#2ca58d','#b07cc6','#d9a21b','#7a8794','#9aa5b1','#5b8c3a'],
  data: (id) => (id in __DATA ? {status:'ok', data: __DATA[id], columns: Object.keys(__DATA[id][0]||{})} : {status:'missing', data: []}),
  draw: (name, fn) => { if (fn) { draws[name] = fn; } else if (draws[name]) document.querySelectorAll('[data-draw="'+name+'"]').forEach(el => draws[name](el)); },
  onData: (fn) => fn(), calc: () => {}, loader: () => {}, params: () => ({}), setParams: () => {}, setLink: () => {}, refresh: () => {},
};
window.__runDraws = () => { for (const [n, fn] of Object.entries(draws)) document.querySelectorAll('[data-draw="'+n+'"]').forEach(el => { try { fn(el); } catch (e) { console.error('draw '+n+': '+e.message); } }); };
`;
const doc = `<!doctype html><html><head><meta charset="utf-8"><style>
:root{--color-fg:#1d2228;--color-fg-muted:#5c6670;--color-bg:#fff;--color-panel:#fafbfc;--color-border-line:#e3e6ea;--color-ok:#2c8a4b;--color-warn:#b7791f;--color-bad:#c0392b;
--font-anthropic-sans:system-ui;--font-anthropic-serif:Georgia;--cds-font-size-caption:12px;--cds-font-size-body:14px;--cds-font-size-heading:18px;--cds-font-size-title:26px;--cds-font-weight-medium:600;
--cds-gap-xs:4px;--cds-gap-sm:8px;--cds-gap-md:12px;--cds-gap-lg:16px;--cds-gap-xl:28px;--cds-pad-sm:8px;--cds-pad-md:12px;--cds-pad-lg:16px;--cds-radius:8px;
--cds-chart-grid:#eef0f2;--cds-chart-axis:#c9ced4;--cds-chart-reference:#9aa5b1;--cds-chart-muted:#cfd6dd;--cds-text-secondary:#5c6670;--cds-surface-popover:#fff;--cds-border:#ddd;--cds-shadow-popover:0 2px 8px #0002}
body{margin:0;padding:16px;background:var(--color-bg)}</style>
<script>${d3src}</script><script>${shim}</script></head><body><div id="dash-root">${html.replace(/<script>/g, '<script>try{').replace(/<\/script>/g, '}catch(e){console.error("script: "+e.message)}</script>')}</div>
<script>__runDraws();</script></body></html>`;
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: +(width || 1200), height: 900 }, deviceScaleFactor: 1 });
  const errs = [];
  p.on('console', (m) => { if (m.type() === 'error') errs.push(m.text()); });
  p.on('pageerror', (e) => errs.push('pageerror: ' + e.message));
  await p.setContent(doc, { waitUntil: 'load' });
  await p.waitForTimeout(300);
  // exercise the controls
  for (const sel of await p.$$('select')) { const opts = await sel.$$eval('option', (o) => o.map((x) => x.value)); if (opts.length > 1) { await sel.selectOption(opts[1]); await p.waitForTimeout(100); } }
  const sw = await p.evaluate(() => document.documentElement.scrollWidth);
  await p.screenshot({ path: out, fullPage: true });
  console.log(JSON.stringify({ errors: errs, scrollWidth: sw, viewport: +(width || 1200) }));
  await b.close();
})();
