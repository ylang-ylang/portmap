/* SPDX-License-Identifier: MIT */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
if (!globalThis.crypto) globalThis.crypto = webcrypto;
const core = require("../core.js");
const { createStore, STORAGE_KEY } = require("../managed-identity.js");
const { createRuntime } = require("../transports.js");
const { createController } = require("../controller.js");
const pin = "ab".repeat(32);
const draft = overrides => ({ id: "", name: "Node", domain: "n.portmap", enabled: true, mode: "ssh",
  host: "192.0.2.4", httpPort: 80, httpsPort: 443,
  ssh: { port: 22, username: "user", targetHost: "127.0.0.1", hostKeySha256Hex: pin, auth: { type: "managed" } }, ...overrides });
const direct = () => ({ id: "d", name: "Direct", domain: "d.portmap", enabled: true, mode: "direct", host: "192.0.2.8", httpPort: 80, httpsPort: null });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function event() { const listeners = []; return { addListener(listener) { listeners.push(listener); }, emit(...args) { for (const listener of listeners) listener(...args); } }; }
let keyRecord;
async function record() {
  if (!keyRecord) keyRecord = (async () => {
    const values = {};
    const storage = { async get(key) { return { [key]: values[key] }; }, async set(value) { Object.assign(values, value); } };
    await createStore(storage, webcrypto).publicKey({ create: true });
    return values[STORAGE_KEY];
  })();
  return structuredClone(await keyRecord);
}
async function fixture(t, { connections = [], hooks = {}, missingKey = false } = {}) {
  let sequence = 0;
  const values = { config: { schemaVersion: 3, revision: "seed", connections: structuredClone(connections) } };
  if (!missingKey) values[STORAGE_KEY] = await record();
  const changed = event(), events = [], calls = [], opens = new Map();
  const storage = {
    async get(key) { return { [key]: structuredClone(values[key]) }; },
    async set(items) {
      await hooks.beforeWrite?.(items);
      const changes = {};
      for (const [key, value] of Object.entries(items)) {
        changes[key] = { oldValue: values[key], newValue: structuredClone(value) };
        values[key] = structuredClone(value);
      }
      changed.emit(changes, "local");
    },
    async remove(key) {
      await hooks.beforeRemove?.(key);
      const oldValue = values[key]; delete values[key]; changed.emit({ [key]: { oldValue } }, "local");
    }
  };
  const native = { allowed: [], configurations: [],
    async configure(items) { this.allowed = structuredClone(items); this.configurations.push(structuredClone(items)); events.push("configure"); },
    async selfCheck() { return true; },
    async connect(host, port) {
      assert.ok(this.allowed.some(entry => entry.host === host && entry.port === port), "native permission must admit only the exact requested endpoint");
      return ++sequence;
    },
    async read() {}, async write() {}, async close() {}, async listen() {}, async accept() {}, async closeListener() {}
  };
  const ssh = {
    async inspectHost(endpoint, api, options) {
      events.push("inspect"); await api.connect(endpoint.host, endpoint.port);
      assert.deepEqual(api.allowed, [endpoint]);
      await hooks.inspect?.(endpoint, options);
      return { hostKeySha256Hex: pin };
    },
    async connect(config, api, options) {
      assert.ok(["key", "password"].includes(config.auth.type), "managed auth must resolve only inside the background runtime");
      await api.connect(config.host, config.port);
      events.push(`connect:${config.auth.type}`); calls.push(structuredClone(config));
      await hooks.connect?.(config, options);
      let closed = false;
      return {
        async installPublicKey(publicKey) {
          assert.equal(config.auth.type, "password"); assert.match(publicKey, /^ssh-rsa /);
          events.push("install"); await hooks.install?.(publicKey, options); return { installed: true };
        },
        async close() {
          if (closed) return; closed = true;
          events.push(`close-start:${config.auth.type}`); await hooks.close?.(config.auth.type);
          events.push(`close-done:${config.auth.type}`);
        },
        async open() { return { async read() { return ""; }, async write() {}, async close() {} }; }
      };
    }
  };
  const proxy = { async start(_native, open, { domain }) {
    await hooks.proxy?.(domain); opens.set(domain, open);
    let closed = false;
    return { host: "127.0.0.1", port: ++sequence + 20000, credentials: { username: "route", password: "route-secret" }, realm: "Portmap internal",
      get closed() { return closed; }, async close() { closed = true; } };
  } };
  const api = { portmapTransport: native, storage: { local: storage, onChanged: changed },
    proxy: { onRequest: event() }, webRequest: { onBeforeRequest: event(), onBeforeSendHeaders: event(), onAuthRequired: event(), onCompleted: event(), onErrorOccurred: event() },
    tabs: { async create() {} } };
  const identity = createStore(storage, webcrypto);
  const runtime = createRuntime(api, { ssh, proxy, managedIdentity: identity });
  const controller = createController(api, () => `id-${++sequence}`, runtime, identity);
  const owner = controller.openEditor();
  t.after(() => { controller.cancelOwner(owner, true); runtime.invalidate(); });
  await controller.command({ action: "get" });
  return { api, values, identity, runtime, controller, owner, native, events, calls, opens,
    command(message, otherOwner = owner) { return controller.command(message, otherOwner); },
    state() { return controller.command({ action: "get" }); },
    async trust(endpoint = { host: "192.0.2.4", port: 22 }) {
      const inspected = (await controller.command({ action: "inspect-host", endpoint }, owner)).setup;
      await controller.command({ action: "confirm-host", endpoint, challenge: inspected.challenge, hostKeySha256Hex: inspected.hostKeySha256Hex }, owner);
      return inspected;
    }
  };
}

test("managed auth normalizes without secrets while existing schema3 password and key choices remain unchanged", () => {
  assert.deepEqual(core.normalizeConnection({ ...draft(), id: "n" }).ssh.auth, { type: "managed" });
  assert.throws(() => core.normalizeConnection({ ...draft(), id: "n", ssh: { ...draft().ssh, auth: { type: "managed", privateKey: "must-not-save" } } }), /must not contain/);
  const old = { ...draft(), id: "n", ssh: { ...draft().ssh, auth: { type: "password", password: "explicit-saved-password" } } };
  const stored = { schemaVersion: 3, revision: "old", connections: [old] };
  assert.deepEqual(core.readConfig(stored).connections[0].ssh.auth, old.ssh.auth);
  assert.deepEqual(stored.connections[0], old);
});

test("handshake admission sends no auth, requires explicit owner-bound endpoint confirmation, and restores saved permission", async t => {
  const f = await fixture(t, { connections: [direct()] });
  const endpoint = { host: "192.0.2.4", port: 22 };
  await assert.rejects(f.controller.command({ action: "inspect-host", endpoint }), /Open the connection editor/);
  const inspected = (await f.command({ action: "inspect-host", endpoint })).setup;
  assert.equal(f.calls.length, 0); assert.deepEqual(f.native.allowed, [{ host: "192.0.2.8", port: 80 }]);
  await assert.rejects(f.command({ action: "managed-install", connection: draft(), password: "one-time" }), /explicitly confirm/);
  const other = f.controller.openEditor();
  await assert.rejects(f.command({ action: "confirm-host", endpoint, challenge: inspected.challenge, hostKeySha256Hex: pin }, other), /expired or the endpoint changed/);
  await assert.rejects(f.command({ action: "confirm-host", endpoint: { ...endpoint, port: 2222 }, challenge: inspected.challenge, hostKeySha256Hex: pin }), /endpoint changed/);
  await f.command({ action: "confirm-host", endpoint, challenge: inspected.challenge, hostKeySha256Hex: pin });
  await assert.rejects(f.command({ action: "managed-install", connection: draft({ host: "192.0.2.5" }), password: "one-time" }), /explicitly confirm/);
  await assert.rejects(f.command({ action: "inspect-host", endpoint, expectedPin: "cd".repeat(32) }), /does not match/);
  assert.equal(f.calls.length, 0); assert.equal(f.values.config.connections.length, 1);
  f.controller.cancelOwner(other, true);
});

test("password installation closes before NEW pinned key login and saves only after verifier closes", async t => {
  const passwordClosing = deferred(), releasePassword = deferred(), keyClosing = deferred(), releaseKey = deferred();
  const f = await fixture(t, { hooks: { async close(type) {
    if (type === "password") { passwordClosing.resolve(); await releasePassword.promise; }
    else { keyClosing.resolve(); await releaseKey.promise; }
  } } });
  await f.trust();
  const message = { action: "managed-install", connection: draft(), password: "transient-only" };
  const saving = f.command(message);
  await passwordClosing.promise;
  assert.deepEqual(f.calls.map(call => call.auth.type), ["password"]);
  assert.equal(f.values.config.connections.length, 0);
  releasePassword.resolve(); await keyClosing.promise;
  assert.deepEqual(f.calls.map(call => call.auth.type), ["password", "key"]);
  assert.ok(f.events.indexOf("close-done:password") < f.events.indexOf("connect:key"));
  assert.equal(f.values.config.connections.length, 0);
  releaseKey.resolve(); const result = await saving;
  assert.deepEqual(result.connections[0].ssh.auth, { type: "managed" });
  assert.equal(message.password, undefined, "background drops the transient request credential on completion");
  assert.doesNotMatch(JSON.stringify({ state: result, config: f.values.config }), /transient-only|BEGIN RSA PRIVATE|"d":/);
  assert.equal(f.calls[0].hostKeySha256Hex, pin); assert.equal(f.calls[1].hostKeySha256Hex, pin);
  assert.deepEqual(f.native.allowed, [{ host: "192.0.2.4", port: 22 }]);
  const publicOnly = (await f.command({ action: "managed-public-key" })).setup;
  assert.deepEqual(Object.keys(publicOnly), ["publicKey"]);
  await f.opens.get("n.portmap")(80);
  assert.equal(f.calls.at(-1).auth.type, "key", "saved managed nodes resolve the profile key at runtime");
});

test("manual public-key setup uses a real key-auth check, never password fallback, and failed verification reports partial installation", async t => {
  const manual = await fixture(t); await manual.trust();
  await manual.command({ action: "managed-save", connection: draft() });
  assert.deepEqual(manual.calls.map(call => call.auth.type), ["key"]);
  const failed = await fixture(t, { connections: [direct()], hooks: { connect(config) { if (config.auth.type === "key") throw new Error("SSH key authentication failed"); } } });
  await failed.trust();
  await assert.rejects(failed.command({ action: "managed-install", connection: draft(), password: "one-time" }), failure => {
    assert.equal(failure.possiblyInstalled, true); assert.match(failure.message, /may already be installed/); return true;
  });
  assert.deepEqual(failed.values.config.connections, [direct()]);
  assert.deepEqual(failed.native.allowed, [{ host: "192.0.2.8", port: 80 }]);
  assert.deepEqual(failed.calls.map(call => call.auth.type), ["password", "key"]);
});

test("cancellation, disconnected editors, and external config changes cannot save a stale remote installation", async t => {
  for (const kind of ["cancel", "disconnect", "external"]) {
    const entered = deferred(), release = deferred();
    const f = await fixture(t, { hooks: { async install() { entered.resolve(); await release.promise; } } });
    await f.trust();
    const installing = f.command({ action: "managed-install", connection: draft(), password: "one-time" });
    const rejected = assert.rejects(installing, failure => { assert.match(failure.message, /may already be installed/); return true; });
    await entered.promise;
    if (kind === "cancel") await f.command({ action: "cancel-setup" });
    if (kind === "disconnect") f.controller.cancelOwner(f.owner, true);
    if (kind === "external") await f.api.storage.local.set({ config: { schemaVersion: 3, revision: "external", connections: [direct()] } });
    await rejected; release.resolve(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.values.config.connections.some(entry => entry.mode === "ssh"), false);
    assert.deepEqual(f.calls.map(call => call.auth.type), ["password"]);
    await f.state(); assert.deepEqual(f.native.allowed, kind === "external" ? [{ host: "192.0.2.8", port: 80 }] : []);
  }
});

test("closing options during storage or route publication rolls back only its pending managed save", async t => {
  for (const stage of ["storage", "publish"]) {
    const entered = deferred(), release = deferred();
    const f = await fixture(t, { hooks: {
      async beforeWrite(items) { if (stage === "storage" && items.config?.connections.some(entry => entry.mode === "ssh")) { entered.resolve(); await release.promise; } },
      async proxy(domain) { if (stage === "publish" && domain === "n.portmap") { entered.resolve(); await release.promise; } }
    } });
    await f.trust();
    const saving = f.command({ action: "managed-save", connection: draft() });
    const rejected = assert.rejects(saving, /cancelled/);
    await entered.promise; f.controller.cancelOwner(f.owner, true); release.resolve(); await rejected;
    assert.deepEqual(f.values.config.connections, []);
    assert.equal((await f.state()).phase, "ready"); assert.deepEqual(f.native.allowed, []);
  }
});

test("an external editor changing config during a pending write is preserved instead of overwritten by the stale managed draft", async t => {
  const entered = deferred(), release = deferred();
  const f = await fixture(t, { hooks: { async beforeWrite(items) {
    if (items.config?.connections.some(entry => entry.mode === "ssh")) { entered.resolve(); await release.promise; }
  } } });
  await f.trust();
  const saving = f.command({ action: "managed-save", connection: draft() });
  const rejected = assert.rejects(saving, /cancelled/);
  await entered.promise;
  await f.api.storage.local.set({ config: { schemaVersion: 3, revision: "other-editor", connections: [direct()] } });
  release.resolve(); await rejected;
  assert.equal(f.values.config.revision, "other-editor");
  assert.deepEqual(f.values.config.connections, [direct()]);
  assert.equal((await f.state()).phase, "ready");
  assert.deepEqual(f.native.allowed, [{ host: "192.0.2.8", port: 80 }]);
});

test("missing profile key never rotates for an existing managed node and reset reports undeleted key honestly", async t => {
  const existing = { ...draft(), id: "n" };
  const missing = await fixture(t, { connections: [existing], missingKey: true });
  await assert.rejects(missing.command({ action: "managed-public-key" }), /No replacement/);
  await assert.rejects(missing.opens.get("n.portmap")(80), /Node connection failed/);
  assert.equal(missing.calls.length, 0); assert.equal(missing.values[STORAGE_KEY], undefined);
  const f = await fixture(t, { connections: [existing], hooks: { beforeRemove() { throw new Error("disk unavailable"); } } });
  await assert.rejects(f.command({ action: "reset" }), /Connections were deleted.*key could not be deleted/);
  assert.deepEqual(f.values.config.connections, []); assert.ok(f.values[STORAGE_KEY]);
  f.api.storage.local.remove = async key => { delete f.values[key]; };
  await f.command({ action: "reset" });
  assert.equal(f.values[STORAGE_KEY], undefined); await assert.rejects(f.identity.resolve(), /No replacement/);
});
