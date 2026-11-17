/* SPDX-License-Identifier: MIT */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

// These deterministic unit tests model lifecycle races, not real camera capture.
// The browser harness separately exercises getUserMedia and real PNG decoding.
class Element {
  constructor() {
    this.listeners = new Map();
    this.children = [];
    this.hidden = false;
    this.disabled = false;
    this.value = "";
    this.files = [];
    this.dataset = {};
    this.readyState = 2;
    this.videoWidth = 1920;
    this.videoHeight = 1080;
    this.srcObject = null;
  }
  addEventListener(name, callback) { this.listeners.set(name, [...this.listeners.get(name) || [], callback]); }
  removeEventListener(name, callback) { this.listeners.set(name, (this.listeners.get(name) || []).filter(item => item !== callback)); }
  async emit(name, event = {}) { await Promise.all((this.listeners.get(name) || []).map(callback => callback(event))); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  focus() {}
  pause() {}
  play() { return Promise.resolve(); }
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function media() {
  const track = new Element();
  track.stopped = 0;
  track.stop = () => { track.stopped++; };
  return { track, getTracks: () => [track] };
}
function png(width = 32, height = 32) {
  const data = new Uint8Array(33);
  const view = new DataView(data.buffer);
  view.setUint32(0, 0x89504e47);
  view.setUint32(4, 0x0d0a1a0a);
  view.setUint32(8, 13);
  view.setUint32(12, 0x49484452);
  view.setUint32(16, width);
  view.setUint32(20, height);
  return data.buffer;
}
function fixture() {
  const elements = new Map();
  const el = id => {
    if (!elements.has(id)) elements.set(id, new Element());
    return elements.get(id);
  };
  el("qr-panel").hidden = true;
  const document = new Element();
  document.hidden = false;
  document.getElementById = el;
  document.createElement = () => new Element();
  const context = {
    drawImage: () => {},
    getImageData: () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 })
  };
  el("qr-canvas").getContext = () => context;
  const requests = [];
  const navigator = { mediaDevices: { getUserMedia(constraints) {
    const pending = deferred();
    requests.push({ ...pending, constraints });
    return pending.promise;
  } } };
  const timers = new Map();
  let timerId = 0;
  const images = [];
  const revoked = [];
  const root = new Element();
  let result = null;
  const sandbox = {
    console: { log() { throw new Error("scanner must not log"); } },
    Blob, Uint8Array, Uint8ClampedArray, DataView,
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
    addEventListener: root.addEventListener.bind(root),
    jsQR: () => result,
    URL: { createObjectURL: () => "blob:owned-image", revokeObjectURL: url => revoked.push(url) },
    Image: class { constructor() { this.naturalWidth = 32; this.naturalHeight = 32; images.push(this); } }
  };
  vm.runInNewContext(fs.readFileSync(require.resolve("../scan.js"), "utf8"), sandbox);
  let available = true, busy = false, overlap = "";
  const decoded = [];
  const confirmed = [];
  const draft = { name: "Owned fixture", domain: "owned.portmap", mode: "direct", host: "192.0.2.1", httpPort: 8080, httpsPort: null };
  let apply = async item => { confirmed.push(item); return true; };
  const scanner = sandbox.PortmapScanner.create({ document, navigator,
    decode: text => { decoded.push(text); if (text === "invalid") throw new Error("safe"); return draft; },
    canUse: () => available, isBusy: () => busy, conflict: () => overlap,
    onConfirm: (...args) => apply(...args) });
  return { scanner, el, document, root, requests, timers, images, revoked, decoded, confirmed, draft,
    result: value => { result = value; }, available: value => { available = value; scanner.refresh(); },
    busy: value => { busy = value; scanner.refresh(); }, overlap: value => { overlap = value; scanner.refresh(); },
    apply: callback => { apply = callback; },
    async open() { await el("qr-open").emit("click"); },
    async tick(delay) {
      const entry = [...timers].find(([, value]) => value.delay === delay);
      assert.ok(entry, `expected ${delay}ms timer`);
      timers.delete(entry[0]);
      await entry[1].callback();
    } };
}

test("cancelled permission response stops only its own late stream", async () => {
  const f = fixture();
  await f.open();
  const old = f.el("qr-camera").emit("click");
  assert.equal(f.requests[0].constraints.audio, false);
  assert.equal(f.requests[0].constraints.video.facingMode.ideal, "environment");
  await f.el("qr-cancel").emit("click");
  await f.open();
  const next = f.el("qr-camera").emit("click");
  const active = media();
  f.requests[1].resolve(active);
  await next;
  const late = media();
  f.requests[0].resolve(late);
  await old;
  assert.equal(late.track.stopped, 1);
  assert.equal(active.track.stopped, 0);
  assert.equal(f.el("qr-video").srcObject, active);
  assert.equal(f.el("qr-status").dataset.state, "scanning");
});

test("successful camera decode stops capture before inert preview and explicit confirmation", async () => {
  const f = fixture();
  await f.open();
  f.result({ data: "public-wire" });
  const capture = f.el("qr-camera").emit("click");
  const stream = media();
  f.requests[0].resolve(stream);
  await capture;
  assert.equal(stream.track.stopped, 1);
  assert.equal(f.el("qr-video").srcObject, null);
  assert.deepEqual(f.decoded, ["public-wire"]);
  assert.equal(f.el("qr-preview").hidden, false);
  assert.equal(f.confirmed.length, 0);
  f.busy(true);
  assert.equal(f.el("qr-preview").hidden, false);
  f.busy(false);
  await f.el("qr-confirm").emit("click");
  assert.deepEqual(f.confirmed, [f.draft]);
  assert.equal(f.el("qr-panel").hidden, true);
});

test("camera denial and malformed QR preserve mutation boundary", async () => {
  const f = fixture();
  await f.open();
  const denied = f.el("qr-camera").emit("click");
  f.requests[0].reject(new Error("sensitive backend text"));
  await denied;
  assert.equal(f.el("qr-status").dataset.state, "error");
  assert.doesNotMatch(f.el("qr-status").textContent, /sensitive/);
  assert.equal(f.el("qr-image").disabled, false);
  f.result({ data: "invalid" });
  const capture = f.el("qr-camera").emit("click");
  const stream = media();
  f.requests[1].resolve(stream);
  await capture;
  assert.equal(stream.track.stopped, 1);
  assert.equal(f.el("qr-preview").hidden, true);
  assert.equal(f.confirmed.length, 0);
});

test("hidden document, pagehide, track termination and capability loss release capture", async () => {
  for (const terminal of ["hidden", "pagehide", "ended", "capability"]) {
    const f = fixture();
    await f.open();
    const capture = f.el("qr-camera").emit("click");
    const stream = media();
    f.requests[0].resolve(stream);
    await capture;
    if (terminal === "hidden") { f.document.hidden = true; await f.document.emit("visibilitychange"); }
    if (terminal === "pagehide") await f.root.emit("pagehide");
    if (terminal === "ended") await stream.track.emit("ended");
    if (terminal === "capability") f.available(false);
    assert.equal(stream.track.stopped, 1, terminal);
    assert.equal(f.el("qr-video").srcObject, null, terminal);
    assert.equal(f.timers.size, 0, terminal);
    assert.equal(f.confirmed.length, 0, terminal);
  }
});

test("permission and live scan deadlines stop late or active media", async () => {
  const f = fixture();
  await f.open();
  const pending = f.el("qr-camera").emit("click");
  await f.tick(30000);
  const late = media();
  f.requests[0].resolve(late);
  await pending;
  assert.equal(late.track.stopped, 1);
  const capture = f.el("qr-camera").emit("click");
  const stream = media();
  f.requests[1].resolve(stream);
  await capture;
  await f.tick(120000);
  assert.equal(stream.track.stopped, 1);
  assert.equal(f.timers.size, 0);
});

test("oversized, vector and decompression-sized image headers never reach browser decoder", async () => {
  for (const file of [
    { size: 8 * 1024 * 1024 + 1, arrayBuffer() { throw new Error("must not read"); } },
    { size: 20, arrayBuffer: async () => new TextEncoder().encode("<svg></svg>").buffer },
    { size: 33, arrayBuffer: async () => png(16000, 16000) }
  ]) {
    const f = fixture();
    await f.open();
    f.el("qr-image").files = [file];
    await f.el("qr-image").emit("change");
    assert.equal(f.images.length, 0);
    assert.equal(f.decoded.length, 0);
    assert.equal(f.confirmed.length, 0);
    assert.equal(f.el("qr-status").dataset.state, "error");
  }
});

test("cancelled late image read cannot create preview or change editor", async () => {
  const f = fixture();
  const bytes = deferred();
  await f.open();
  f.el("qr-image").files = [{ size: 33, arrayBuffer: () => bytes.promise }];
  const read = f.el("qr-image").emit("change");
  await f.el("qr-cancel").emit("click");
  bytes.resolve(png());
  await read;
  assert.equal(f.images.length, 0);
  assert.equal(f.el("qr-preview").hidden, true);
  assert.equal(f.confirmed.length, 0);
});

test("image decoding cancellation revokes local URL and resolves stale operation", async () => {
  const f = fixture();
  await f.open();
  f.el("qr-image").files = [{ size: 33, arrayBuffer: async () => png() }];
  const read = f.el("qr-image").emit("change");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.images.length, 1);
  await f.el("qr-cancel").emit("click");
  await read;
  assert.deepEqual(f.revoked, ["blob:owned-image"]);
  assert.equal(f.images[0].src, "");
  assert.equal(f.confirmed.length, 0);
});

test("changed domain conflict disables preview confirmation; own save refresh preserves failures", async () => {
  const f = fixture();
  await f.open();
  f.result({ data: "public-wire" });
  const capture = f.el("qr-camera").emit("click");
  f.requests[0].resolve(media());
  await capture;
  f.overlap("Existing domain overlaps. Use Edit separately.");
  await f.el("qr-confirm").emit("click");
  assert.equal(f.confirmed.length, 0);
  assert.equal(f.el("qr-confirm").disabled, true);
  f.overlap("");
  f.apply(async () => { f.busy(true); f.busy(false); return false; });
  await f.el("qr-confirm").emit("click");
  assert.equal(f.el("qr-status").dataset.state, "error");
  assert.equal(f.el("qr-panel").hidden, false);
});
