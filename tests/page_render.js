/* Run the page's OWN renderers and report what they produced.
 *
 *     node tests/page_render.js <scenario.json>
 *
 * The script under test is read out of hub.html and evaluated as it ships, so
 * what these scenarios exercise is the renderer itself and not a second copy
 * of it that could be escaped correctly while the page is not.
 *
 * The DOM here is the smallest one that script will run against. It matters
 * that it does NOT parse markup: `innerHTML` keeps the exact string the page
 * assigned, which is the thing an escaping check has to look at, and
 * `textContent` keeps text as text. A real parser would hide the difference.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const HUB = path.join(__dirname, '..', 'hub.html');
const html = fs.readFileSync(HUB, 'utf8');
const script = html.slice(html.indexOf('<script>') + '<script>'.length,
                          html.lastIndexOf('</script>'));

class El {
  constructor(tag) {
    this.tagName = String(tag || 'div').toUpperCase();
    this.childNodes = [];
    this.attributes = {};
    this.dataset = {};
    this.style = {};
    this.listeners = {};
    this.parentElement = null;
    this._html = null;
    this._classes = new Set();
    const self = this;
    this.classList = {
      add: (...c) => c.forEach(x => self._classes.add(x)),
      remove: (...c) => c.forEach(x => self._classes.delete(x)),
      contains: c => self._classes.has(c),
      toggle: (c, on) => {
        const want = on === undefined ? !self._classes.has(c) : !!on;
        if (want) self._classes.add(c); else self._classes.delete(c);
        return want;
      },
    };
  }
  get className() { return [...this._classes].join(' '); }
  set className(v) {
    this._classes = new Set(String(v).split(/\s+/).filter(Boolean));
  }
  // The string the page assigned, kept verbatim. Nothing parses it.
  set innerHTML(v) { this._html = String(v); this.childNodes = []; }
  get innerHTML() { return this._html == null ? '' : this._html; }
  set textContent(v) {
    this._html = null;
    this.childNodes = String(v) === '' ? [] : [String(v)];
  }
  get textContent() {
    return this.childNodes
      .map(n => (typeof n === 'string' ? n : n.textContent)).join('');
  }
  append(...nodes) {
    this._html = null;
    for (const n of nodes) {
      if (n && n.tagName) n.parentElement = this;
      this.childNodes.push(n);
    }
  }
  appendChild(n) { this.append(n); return n; }
  remove() {
    const p = this.parentElement;
    if (p) p.childNodes = p.childNodes.filter(n => n !== this);
  }
  addEventListener(ev, fn) {
    (this.listeners[ev] = this.listeners[ev] || []).push(fn);
  }
  removeEventListener() {}
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  fire(ev) {
    for (const fn of this.listeners[ev] || []) fn({ type: ev, target: this });
    const inline = this['on' + ev];
    if (typeof inline === 'function') inline({ type: ev, target: this });
  }
}

const byId = new Map();
function pick(sel) {
  if (!byId.has(sel)) byId.set(sel, new El('div'));
  return byId.get(sel);
}

const scenario = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const routes = scenario.routes || {};
const calls = [];

function fetchShim(url, init) {
  calls.push({ url, body: init && init.body ? String(init.body) : null });
  const hit = Object.prototype.hasOwnProperty.call(routes, url)
    ? routes[url] : (scenario.fallback === undefined ? [] : scenario.fallback);
  return Promise.resolve({
    ok: true,
    json: async () => JSON.parse(JSON.stringify(hit)),
    text: async () => JSON.stringify(hit),
  });
}

class WebSocketShim {
  constructor(url) { this.url = url; WebSocketShim.last = this; }
  send() {}
  close() {}
}

const sandbox = {
  document: {
    createElement: t => new El(t),
    createTextNode: t => String(t),
    querySelector: sel => (sel.startsWith('#') ? pick(sel) : null),
    querySelectorAll: () => [],
    addEventListener() {},
    removeEventListener() {},
    body: new El('body'),
  },
  localStorage: {
    _d: {},
    getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
    setItem(k, v) { this._d[k] = String(v); },
  },
  location: { host: '127.0.0.1:8080', origin: 'http://127.0.0.1:8080' },
  fetch: fetchShim,
  WebSocket: WebSocketShim,
  // Timers are inert. A renderer is being read, not a page that lives.
  setInterval: () => 0,
  clearInterval: () => {},
  setTimeout: () => 0,
  clearTimeout: () => {},
  confirm: () => true,
  URL: { createObjectURL: () => 'blob:none', revokeObjectURL() {} },
  Blob: class { constructor(p) { this.parts = p; } },
  // The browser's own, for counting a name in the bytes the air counts.
  TextEncoder,
  console,
};

vm.createContext(sandbox);
vm.runInContext('globalThis.window=globalThis;', sandbox);
vm.runInContext(script, sandbox, { filename: 'hub.html' });

const tick = () => new Promise(r => setImmediate(r));

function describe(el) {
  return {
    tag: el.tagName,
    text: el.textContent,
    html: el.innerHTML,
    className: el.className,
    attributes: el.attributes,
    inline_handlers: Object.keys(el).filter(k => /^on[a-z]+$/.test(k)
                                            && typeof el[k] === 'function'),
    listeners: Object.keys(el.listeners),
  };
}

async function main() {
  // Let the page's own start-up finish: it fetches its experiments, formats
  // and analyses before anything is rendered.
  for (let i = 0; i < 8; i++) await tick();

  const out = {};
  const what = scenario.scenario;

  if (what === 'captures') {
    await sandbox.loadCaps();
    for (let i = 0; i < 4; i++) await tick();
    out.caps = describe(pick('#caps'));
  } else if (what === 'error_message') {
    sandbox.log(scenario.text, 'err');
    const t = sandbox.toast('Run failed', scenario.text, 'err');
    out.log = describe(pick('#log'));
    out.log_children = pick('#log').childNodes
      .map(n => (typeof n === 'string' ? { tag: '#text', text: n } : describe(n)));
    out.toast = describe(t);
    out.toast_children = t.childNodes
      .map(n => (typeof n === 'string' ? { tag: '#text', text: n } : describe(n)));
  } else if (what === 'markers') {
    WebSocketShim.last.onmessage(
      { data: JSON.stringify({ type: 'markers', names: scenario.names }) });
    const box = pick('#markers');
    out.markers = describe(box);
    out.buttons = box.childNodes.map(b => describe(b));
    out.clicked = [];
    for (const b of box.childNodes) {
      const before = calls.length;
      b.fire('click');
      out.clicked.push(calls.slice(before));
    }
  } else if (what === 'params') {
    pick('#exp').value = scenario.experiment;
    sandbox.applyTier('advanced');
    for (let i = 0; i < 4; i++) await tick();
    out.params = describe(pick('#params'));
    out.marker_edit = describe(pick('#markerEdit'));
  } else if (what === 'picker') {
    // A page opened with nothing connected: it lists what is on the air.
    sandbox.onStatus(scenario.status);
    for (let i = 0; i < 4; i++) await tick();
    out.picker_open = pick('#picker').classList.contains('show');
    out.link = describe(pick('#linkBtn'));
    out.list = describe(pick('#pickList'));
    out.state = describe(pick('#pickState'));
    // A person clicks the second device listed.
    const before = calls.length;
    await pick('#pickList').onclick({ target: { closest: () => ({ dataset: { a: scenario.click } }) } });
    out.clicked = calls.slice(before);
  } else if (what === 'device_status') {
    // A status as the server sends it, then the model panel it loads.
    sandbox.onStatus(scenario.status);
    for (let i = 0; i < 6; i++) await tick();
    out.name_col_hidden = !!pick('#nameCol').hidden;
    out.name_in = pick('#nameIn').value;
    out.adj_in = pick('#adjIn').value;
    out.preview = describe(pick('#namePreview'));
    out.source = describe(pick('#srcNote'));
    out.devinfo = describe(pick('#devinfo'));
    out.model = describe(pick('#modelBody'));
    out.predict = describe(pick('#predict'));
    if (scenario.typed) {
      // What a person types, one pair at a time, as the page sees it.
      out.typed = [];
      for (const [name, adjective] of scenario.typed) {
        pick('#nameIn').value = name;
        pick('#adjIn').value = adjective;
        pick('#nameIn').fire('input');
        const before = calls.length;
        const disabled = !!pick('#nameApply').disabled;
        if (!disabled) await pick('#nameApply').onclick();
        out.typed.push({ preview: describe(pick('#namePreview')), disabled,
                         fetches: calls.slice(before) });
      }
    }
  } else {
    throw new Error('unknown scenario: ' + what);
  }
  out.fetches = calls;
  return out;
}

main().then(o => { process.stdout.write(JSON.stringify(o)); })
  .catch(e => {
    process.stdout.write(JSON.stringify({ harness_error: String(e && e.stack || e) }));
    process.exitCode = 1;
  });
