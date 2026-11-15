/* SPDX-License-Identifier: MIT */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { createController } = require("../controller.js");

function event() {
  const listeners = [];
  return { listeners, addListener(listener, ...args) { listeners.push({ listener, args }); },
    emit(...args) { return listeners.map(entry => entry.listener(...args)); } };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const profile = overrides => ({ id: "a", name: "Node A", domain: "a.portmap", proxyHost: "100.64.0.1",
  proxyPort: 8443, username: "user-a", password: "pass-a", enabled: true, ...overrides });
const config = connections => ({ schemaVersion: 2, revision: "seed", connections });
function connectionCode(overrides = {}) {
  const { id, enabled, ...payload } = profile(overrides);
  return `portmap-connect-v1:${Buffer.from(JSON.stringify(payload)).toString("base64url")}`;
}
async function preview(f, code = connectionCode()) {
  return (await f.controller.command({ action: "preview-import", code })).importPreview;
}
function importCode(f, code, confirmed) {
  return f.controller.command({ action: "import", code, revision: confirmed.revision, replaceId: confirmed.replaceId });
}

function fixture(initial = config([profile()]), gates = {}) {
  let stored = initial;
  let ids = 0;
  const api = {
    proxy: { onRequest: event() },
    webRequest: { onBeforeRequest: event(), onBeforeSendHeaders: event(), onAuthRequired: event(), onCompleted: event(), onErrorOccurred: event() },
    storage: { onChanged: event(), local: {
      writes: [],
      async get() { if (gates.read) await gates.read.promise; return { config: stored }; },
      async set(value) {
        if (gates.write) await gates.write.promise;
        api.storage.local.writes.push(value);
        const previous = stored;
        stored = value.config;
        api.storage.onChanged.emit({ config: { oldValue: previous, newValue: stored } }, "local");
      }
    } },
    tabs: { opened: [], async create(value) { this.opened.push(value); } }
  };
  const controller = createController(api, () => `generated-${++ids}`);
  return { api, controller,
    async state() { return controller.command({ action: "get" }); },
    select(details) { return api.proxy.onRequest.emit(details)[0]; },
    block(details) { return api.webRequest.onBeforeRequest.emit(details)[0]; },
    external(value, area = "local") {
      stored = value;
      api.storage.onChanged.emit({ config: { newValue: value } }, area);
    }
  };
}
function details(url = "http://a.portmap/path", requestId = "1") { return { url, requestId }; }
function routed(f, request = details()) {
  const result = f.select(request);
  assert.ok(Array.isArray(result));
  assert.equal(result.length, 2);
  assert.equal(result[1], null, "Firefox default proxy/direct chain must not become a failover");
  return { ...request, proxyInfo: result[0] };
}

function blocked(f, request = details(), result = f.select(request)) {
  assert.deepEqual(result, [{ type: "socks", host: "192.0.2.1", port: 65535,
    proxyDNS: true, connectionIsolationKey: "portmap-blocked-admission" }, null]);
  const resolved = { ...request, proxyInfo: result[0] };
  assert.deepEqual(f.block(resolved), { cancel: true }, "Admission sentinel must never authorize transport");
  assert.deepEqual(f.api.webRequest.onBeforeSendHeaders.emit(resolved)[0], { cancel: true });
  assert.equal(result[0].username, undefined, "Admission sentinel must never receive credentials");
  assert.equal(result[0].password, undefined);
}

test("first configured request awaits initial storage while listeners block unauthorized sends and normal traffic is untouched", async () => {
  const gate = deferred();
  const f = fixture(config([profile()]), { read: gate });
  assert.deepEqual(f.api.webRequest.onBeforeRequest.listeners[0].args[1], ["blocking"]);
  const request = details();
  const pending = f.select(request);
  assert.ok(pending instanceof Promise);
  let settled = false;
  pending.then(() => { settled = true; });
  await Promise.resolve();
  assert.equal(settled, false);
  assert.deepEqual(f.block(request), { cancel: true });
  assert.equal(f.select(details("https://ordinary.example/")), undefined);
  assert.deepEqual(f.block(details("https://ordinary.example/")), {});
  gate.resolve();
  const selected = await pending;
  assert.equal(selected[0].host, "100.64.0.1");
  assert.equal(selected[0].type, "socks");
  assert.equal(selected[0].proxyDNS, true);
  assert.equal(selected[1], null);
  assert.deepEqual(f.block({ ...request, proxyInfo: selected[0] }), {});
  assert.equal((await f.state()).phase, "ready");
});

test("first request rejects missing, disabled, invalid and unavailable initial configuration without default fallback", async () => {
  for (const initial of [undefined, config([]), config([profile({ enabled: false })]),
    { schemaVersion: 1, revision: "old", connections: [profile()] }]) {
    const gate = deferred();
    const f = fixture(initial, { read: gate });
    if (initial === undefined) f.external(undefined, "sync"); // Missing local config, without a local change event.
    const request = details();
    const pending = f.select(request);
    gate.resolve();
    blocked(f, request, await pending);
  }
  const gate = deferred();
  const f = fixture(config([profile()]), { read: gate });
  const request = details();
  const pending = f.select(request);
  gate.reject(new Error("storage unavailable"));
  blocked(f, request, await pending);
  assert.equal((await f.state()).phase, "error");
});

test("configuration invalidation revokes an initial waiting request instead of selecting its replacement node", async () => {
  const gate = deferred();
  const f = fixture(config([profile()]), { read: gate });
  const request = details();
  const pending = f.select(request);
  f.external(config([profile({ proxyHost: "100.64.0.9" })]));
  gate.resolve();
  blocked(f, request, await pending);
  assert.equal((await f.state()).phase, "ready");
  const next = routed(f, details("http://a.portmap/next", "next"));
  assert.equal(next.proxyInfo.host, "100.64.0.9");
  assert.deepEqual(f.block(next), {});
});

test("only exact and descendant claims route; unmapped, disabled, apex and lookalikes do not leak", async () => {
  const f = fixture(config([profile(), profile({ id: "b", domain: "b.portmap", enabled: false })]));
  await f.state();
  for (const url of ["http://a.portmap/", "http://deep.app.a.portmap/", "wss://a.portmap/socket", "http://A.PORTMAP./"]) {
    assert.deepEqual(f.block(routed(f, details(url))), {});
  }
  for (const url of ["http://unmapped.portmap/", "https://unmapped.portmap/", "ws://fresh-unmapped.portmap/ws", "wss://fresh-unmapped.portmap/ws", "http://unmapped.portmap./", "http://b.portmap/", "http://app.b.portmap/", "http://portmap/", "http://nota.portmap/", "ftp://a.portmap/file"]) {
    const request = details(url);
    blocked(f, request);
    assert.deepEqual(f.block(request), { cancel: true });
  }
  assert.equal(f.select(details("http://a.portmap.evil/")), undefined);
});

test("blocking checks actual selected endpoint and refuses missing selection, direct or foreign proxy", async () => {
  const f = fixture();
  await f.state();
  const request = routed(f);
  assert.deepEqual(f.block(request), {});
  for (const proxyInfo of [undefined, { type: "direct" }, { ...request.proxyInfo, host: "wrong.example" },
    { ...request.proxyInfo, port: 9999 }, { ...request.proxyInfo, type: "http" },
    { ...request.proxyInfo, proxyDNS: false }, { ...request.proxyInfo, proxyDNS: undefined }]) {
    assert.deepEqual(f.block({ ...request, proxyInfo }), { cancel: true });
  }
  assert.deepEqual(f.block({ ...request, requestId: "never-selected" }), { cancel: true });
  assert.deepEqual(f.block({ ...request, url: "http://different.a.portmap/" }), { cancel: true });
  f.api.webRequest.onCompleted.emit(request);
  assert.deepEqual(f.block(request), { cancel: true });
});

test("routing preserves original URL, method, origin auth and body; pre-header revocation blocks stale sends", async () => {
  const f = fixture();
  await f.state();
  const request = {
    url: "http://app.a.portmap:80/a%2Fb?q=hello%20there#fragment", requestId: "post",
    method: "POST", requestHeaders: [{ name: "Authorization", value: "Bearer origin-token" }],
    requestBody: { raw: [{ bytes: new Uint8Array([0, 1, 2, 255]).buffer }] }
  };
  const original = structuredClone(request);
  const selected = routed(f, request);
  assert.deepEqual(request, original);
  assert.deepEqual(f.api.webRequest.onBeforeSendHeaders.emit(selected)[0], {});
  await f.controller.command({ action: "toggle", id: "a" });
  assert.deepEqual(f.api.webRequest.onBeforeSendHeaders.emit(selected)[0], { cancel: true });
  assert.deepEqual(request, original);
  const ordinary = { ...request, url: "https://ordinary.example/" };
  assert.deepEqual(f.api.webRequest.onBeforeSendHeaders.emit(ordinary)[0], {});
});

test("configuration writes block pending and stale selections; disable is persisted across controller reload", async () => {
  const gate = deferred();
  const f = fixture(undefined, { write: gate });
  await f.state();
  const save = f.controller.command({ action: "save", connection: profile() });
  await Promise.resolve();
  blocked(f);
  assert.deepEqual(f.block(details()), { cancel: true });
  gate.resolve();
  await save;
  const previous = routed(f);
  await f.controller.command({ action: "toggle", id: "a" });
  assert.deepEqual(f.block(previous), { cancel: true });
  blocked(f);
  const snapshot = await f.state();
  const reloaded = fixture(config(snapshot.connections));
  await reloaded.state();
  blocked(reloaded);
});

test("queued concurrent edits preserve independent entries and reject overlap without corrupting valid routes", async () => {
  const f = fixture();
  await f.state();
  await Promise.all([
    f.controller.command({ action: "save", connection: profile({ id: "b", domain: "b.portmap", proxyHost: "100.64.0.2" }) }),
    f.controller.command({ action: "save", connection: profile({ id: "c", domain: "c.portmap", proxyHost: "100.64.0.3" }) })
  ]);
  assert.equal((await f.state()).connections.length, 3);
  await assert.rejects(f.controller.command({ action: "save", connection: profile({ id: "overlap", domain: "app.a.portmap" }) }));
  assert.equal(routed(f).proxyInfo.host, "100.64.0.1");
  assert.equal(routed(f, details("http://b.portmap/", "b")).proxyInfo.host, "100.64.0.2");
});

test("external storage edits revoke old generation synchronously, invalid schema blocks all .portmap", async () => {
  const f = fixture();
  await f.state();
  const old = routed(f);
  f.external(config([profile({ proxyHost: "100.64.0.9" })]));
  assert.deepEqual(f.block(old), { cancel: true });
  blocked(f);
  await f.state();
  assert.equal(routed(f).proxyInfo.host, "100.64.0.9");
  f.external({ schemaVersion: 3, revision: "future", connections: [] });
  assert.deepEqual(f.block(old), { cancel: true });
  assert.equal((await f.state()).phase, "error");
  blocked(f);
  await f.controller.command({ action: "reset" });
  assert.equal((await f.state()).phase, "ready");
  blocked(f);
});

test("storage read/write failure stays closed and secrets do not appear in write failure", async () => {
  const read = deferred();
  const f = fixture(config([profile()]), { read });
  read.reject(new Error("storage unavailable"));
  assert.equal((await f.state()).phase, "error");
  assert.deepEqual(f.block(details()), { cancel: true });
  const write = deferred();
  const writable = fixture(config([profile()]), { write });
  await writable.state();
  const action = writable.controller.command({ action: "toggle", id: "a" });
  await Promise.resolve();
  write.reject(new Error("secret backend details"));
  await assert.rejects(action, /Saving local connections failed/);
  assert.equal((await writable.state()).phase, "error");
  assert.deepEqual(writable.block(details()), { cancel: true });
});

test("usable navigation opens configured apex and never a proxy endpoint or arbitrary caller URL", async () => {
  const f = fixture();
  await f.state();
  await f.controller.command({ action: "open", id: "a", scheme: "http", url: "http://evil/" });
  await f.controller.command({ action: "open", id: "a", scheme: "https" });
  assert.deepEqual(f.api.tabs.opened, [{ url: "http://a.portmap/" }, { url: "https://a.portmap/" }]);
  await assert.rejects(f.controller.command({ action: "open", id: "a", scheme: "javascript" }));
  await f.controller.command({ action: "toggle", id: "a" });
  await assert.rejects(f.controller.command({ action: "open", id: "a", scheme: "http" }));
  await f.controller.command({ action: "delete", id: "a" });
  assert.deepEqual((await f.state()).connections, []);
});

test("SOCKS5 credentials are node scoped and never installed as HTTP auth handlers", async () => {
  const f = fixture(config([profile(), profile({ id: "b", domain: "b.portmap",
    proxyHost: "100.64.0.2", username: "user-b", password: "pass-b" })]));
  await f.state();
  const a = routed(f);
  const b = routed(f, details("http://b.portmap/", "b"));
  assert.equal(a.proxyInfo.type, "socks");
  assert.equal(a.proxyInfo.proxyDNS, true);
  assert.equal(a.proxyInfo.username, "user-a");
  assert.equal(a.proxyInfo.password, "pass-a");
  assert.equal(b.proxyInfo.username, "user-b");
  assert.equal(b.proxyInfo.password, "pass-b");
  assert.equal(f.api.webRequest.onAuthRequired.listeners.length, 0);
  assert.deepEqual(f.api.webRequest.onAuthRequired.emit({ ...a, isProxy: false, statusCode: 401 }), []);
  assert.equal(f.select(details("https://ordinary.example/")), undefined);
  assert.deepEqual(f.block(details("https://ordinary.example/")), {});
});

test("code preview is non-secret and read-only; confirmed new domain gets a local identity and enabled route", async () => {
  const f = fixture(config([]));
  await f.state();
  const code = connectionCode({ domain: " B.Portmap. ", proxyHost: "100.64.0.2",
    username: "pair-user", password: "pair-secret" });
  const confirmed = await preview(f, code);
  assert.deepEqual(Object.keys(confirmed).sort(),
    ["name", "domain", "proxyHost", "proxyPort", "replaceId", "enabled", "revision"].sort());
  assert.equal(confirmed.domain, "b.portmap");
  assert.equal(confirmed.replaceId, null);
  assert.equal(confirmed.enabled, true);
  assert.equal(JSON.stringify(confirmed).includes("pair-secret"), false);
  assert.equal(JSON.stringify(confirmed).includes("pair-user"), false);
  assert.equal(f.api.storage.local.writes.length, 0);
  assert.deepEqual((await f.state()).connections, []);
  const imported = await importCode(f, code, confirmed);
  const [saved] = imported.connections;
  assert.ok(saved.id);
  assert.equal(saved.enabled, true);
  assert.equal(saved.domain, "b.portmap");
  assert.equal(saved.username, "pair-user");
  assert.equal(saved.password, "pair-secret");
  assert.equal(f.api.storage.local.writes.length, 1);
  assert.equal(f.api.storage.local.writes[0].config.schemaVersion, 2);
  assert.equal(routed(f, details("https://b.portmap/")).proxyInfo.host, "100.64.0.2");
});

test("exact-domain import replaces rather than duplicates and preserves original identity and disabled state", async () => {
  for (const enabled of [true, false]) {
    const f = fixture(config([profile({ id: "original-local-id", enabled })]));
    await f.state();
    const code = connectionCode({ name: "Replacement", domain: " A.Portmap. ",
      proxyHost: "100.64.0.9", username: "replacement-user", password: "replacement-password" });
    const confirmed = await preview(f, code);
    assert.equal(confirmed.replaceId, "original-local-id");
    assert.equal(confirmed.enabled, enabled);
    const imported = await importCode(f, code, confirmed);
    assert.deepEqual(imported.connections, [profile({ id: "original-local-id", enabled,
      name: "Replacement", proxyHost: "100.64.0.9",
      username: "replacement-user", password: "replacement-password" })]);
    if (enabled) assert.equal(routed(f).proxyInfo.host, "100.64.0.9");
    else blocked(f);
    const repeated = await importCode(f, code, await preview(f, code));
    assert.equal(repeated.connections.length, 1);
    assert.equal(repeated.connections[0].id, "original-local-id");
    assert.equal(repeated.connections[0].enabled, enabled);
  }
});

test("invalid and overlapping imports do not write, revoke valid routes or change existing claims", async () => {
  const f = fixture(config([profile({ domain: "child.a.portmap" }),
    profile({ id: "disabled", domain: "b.portmap", enabled: false })]));
  const before = await f.state();
  const request = routed(f, details("http://child.a.portmap/"));
  const revision = "seed";
  for (const code of [connectionCode({ domain: "a.portmap" }),
    connectionCode({ domain: "deep.child.a.portmap" }), connectionCode({ domain: "app.b.portmap" }),
    "portmap-connect-v1:invalid", connectionCode({ password: "" })]) {
    await assert.rejects(preview(f, code));
    await assert.rejects(f.controller.command({ action: "import", code, revision, replaceId: null }));
    assert.deepEqual(await f.state(), before);
    assert.equal(f.api.storage.local.writes.length, 0);
    assert.deepEqual(f.block(request), {});
  }
});

test("import revalidates current serialized configuration and requires fresh explicit replacement confirmation", async () => {
  const f = fixture();
  await f.state();
  const code = connectionCode({ domain: "b.portmap" });
  const confirmed = await preview(f, code);
  await f.controller.command({ action: "save", connection: profile({ id: "b", domain: "b.portmap", enabled: false }) });
  const writes = f.api.storage.local.writes.length;
  await assert.rejects(importCode(f, code, confirmed));
  await assert.rejects(f.controller.command({ action: "import", code }));
  assert.equal(f.api.storage.local.writes.length, writes);
  const updated = await preview(f, code);
  assert.equal(updated.replaceId, "b");
  assert.equal(updated.enabled, false);
  await assert.rejects(importCode(f, code, { ...updated, replaceId: null }));
  assert.equal(f.api.storage.local.writes.length, writes);
  const imported = await importCode(f, code, updated);
  assert.equal(imported.connections.find(entry => entry.id === "b").enabled, false);
});

test("import runs through fail-closed persistence and invalidates old selections before storage settles", async () => {
  const gate = deferred();
  const f = fixture(config([profile()]), { write: gate });
  await f.state();
  const selected = routed(f);
  const code = connectionCode({ proxyHost: "100.64.0.9" });
  const imported = importCode(f, code, await preview(f, code));
  await Promise.resolve();
  blocked(f);
  assert.deepEqual(f.block(selected), { cancel: true });
  gate.resolve();
  await imported;
  assert.equal(routed(f).proxyInfo.host, "100.64.0.9");
  assert.equal((await f.state()).connections[0].id, "a");
});
