import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const page = fs.readFileSync(new URL('./index.html', import.meta.url), 'utf8');
const between = (start, end) => page.slice(page.indexOf(start), page.indexOf(end, page.indexOf(start)));

assert.match(page, /<img loading="lazy" decoding="async" alt="the screen">/,
  'shotFig builds the img without a src');
assert.doesNotMatch(between('function shotFig(', 'let flying'), /alt="the screen" src=/,
  'no inline src in the figure markup');

function fakeFig(h = 'abc123') {
  const img = {
    attrs: {},
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    removeAttribute(k) { delete this.attrs[k]; },
    get src() { return this.attrs.src || ''; },
    set src(v) { this.attrs.src = String(v); },
  };
  return { dataset: { h }, querySelector: () => img, img };
}

const figs = [fakeFig('one'), fakeFig('two')];
let onChange = null;
const ctx = vm.createContext({
  mobile: false,
  MOBILE: () => ctx.mobile,
  document: { querySelectorAll: () => figs },
  matchMedia: () => ({ addEventListener: (_ev, fn) => { onChange = fn; } }),
  encodeURIComponent,
});
vm.runInContext(between('function mountShot(f){', 'let flying'), ctx);

assert.equal(typeof onChange, 'function', 'listens for the breakpoint crossing, not every resize');

const f = fakeFig();
f.img.src = '/api/shot?h=abc123';
ctx.mountShot(f);
assert.equal(f.img.getAttribute('src'), null, 'a wide layout drops the src');

ctx.mobile = true;
ctx.mountShot(f);
assert.equal(f.img.getAttribute('src'), '/api/shot?h=abc123', 'a narrow layout loads the shot');

f.img.setAttribute('src', '/api/shot?h=abc123&kept');
ctx.mountShot(f);
assert.equal(f.img.getAttribute('src'), '/api/shot?h=abc123&kept', 'an already mounted shot is left alone');

ctx.mobile = true;
onChange();
assert.deepEqual(figs.map((x) => x.img.getAttribute('src')),
  ['/api/shot?h=one', '/api/shot?h=two'], 'narrow restores each card, in order');
ctx.mobile = false;
onChange();
assert.deepEqual(figs.map((x) => x.img.getAttribute('src')), [null, null], 'wide unloads them again');
ctx.mobile = true;
onChange();
assert.deepEqual(figs.map((x) => x.img.getAttribute('src')),
  ['/api/shot?h=one', '/api/shot?h=two'], 'and narrow brings them back once, not twice');

assert.match(page, /figure\.shot\{display:none;/, 'hidden by default');
assert.match(page, /@media\(max-width:900px\)\{\n\s*figure\.shot\{display:block\}/,
  'shown at the same breakpoint the phone shell uses');

console.log('screenshot cards: all checks pass');