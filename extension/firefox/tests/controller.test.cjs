/* SPDX-License-Identifier: MIT */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const net = require("node:net");
const http = require("node:http");
const { once } = require("node:events");
const { webcrypto } = require("node:crypto");
if (!globalThis.crypto) globalThis.crypto = webcrypto;
require("../proxy.js");
const { createController } = require("../controller.js");
const { createRuntime } = require("../transports.js");
const core = require("../core.js");
const profile = overrides => ({ id: "a", name: "Node A", domain: "a.portmap", mode: "direct",
  host: "127.0.0.1", httpPort: 8080, httpsPort: null, enabled: true, ...overrides });
const config = connections => ({ schemaVersion: 3, revision: "seed", connections });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
function event() {
  const listeners = [];
  return { listeners, addListener(listener, ...args) { listeners.push({ listener, args }); },
    emit(...args) { return listeners.map(entry => entry.listener(...args)); } };
}

// A real loopback byte-stream adapter for exercising the shipped proxy/runtime.
// It replaces Firefox's socket service, not HTTP routing, authentication or framing.
function localNative() {
  let sequence = 0, endpoints = [];
  const sockets = new Map(), listeners = new Map();
  const adopt = socket => {
    const id = ++sequence;
    const state = { socket, chunks: [], pending: null, ended: false };
    sockets.set(id, state);
    socket.on("data", data => {
      for (let i = 0; i < data.length; i += 65536) {
        const bytes = data.subarray(i, i + 65536).toString("latin1");
        if (state.pending) { const pending = state.pending; state.pending = null; pending.resolve(bytes); }
        else state.chunks.push(bytes);
      }
    });
    const end = () => { state.ended = true; if (state.pending) { state.pending.resolve(""); state.pending = null; } };
    socket.on("end", end); socket.on("close", end); socket.on("error", end);
    return id;
  };
  async function connectSocket(host, port) {
    const socket = net.createConnection({ host, port });
    const id = adopt(socket);
    try { await once(socket, "connect"); return id; }
    catch (error) { sockets.delete(id); socket.destroy(); throw error; }
  }
  const api = {
    checks: 0, configured: [],
    async configure(items) {
      await api.shutdown(); endpoints = structuredClone(items); api.configured = endpoints;
    },
    async selfCheck() {
      api.checks++;
      let listener, a, b;
      try {
        listener = await api.listen();
        a = await connectSocket(listener.host, listener.port); b = await api.accept(listener.id);
        const sample = "\0\x80\xffcapability";
        await api.write(a, sample); assert.equal(await api.read(b), sample);
        await api.write(b, sample); assert.equal(await api.read(a), sample);
        return true;
      } finally { if (a) await api.close(a); if (b) await api.close(b); if (listener) await api.closeListener(listener.id); }
    },
    async connect(host, port) {
      if (!endpoints.some(entry => entry.host === host && entry.port === port)) throw new Error("Destination denied");
      return connectSocket(host, port);
    },
    async read(id) {
      const state = sockets.get(id);
      if (!state) throw new Error("Closed socket");
      if (state.chunks.length) return state.chunks.shift();
      if (state.ended) return "";
      if (state.pending) throw new Error("Concurrent read");
      const pending = deferred(); state.pending = pending; return pending.promise;
    },
    async write(id, data) {
      const state = sockets.get(id);
      if (!state || state.socket.destroyed) throw new Error("Closed socket");
      return new Promise((resolve, reject) => state.socket.write(Buffer.from(data, "latin1"), error => error ? reject(error) : resolve()));
    },
    async close(id) { const state = sockets.get(id); if (state) { state.socket.destroy(); sockets.delete(id); } },
    async listen() {
      const id = ++sequence;
      const listener = { queue: [], pending: null };
      const server = net.createServer(socket => {
        const socketId = adopt(socket);
        if (listener.pending) { const pending = listener.pending; listener.pending = null; pending.resolve(socketId); }
        else listener.queue.push(socketId);
      });
      listener.server = server;
      listeners.set(id, listener); server.listen(0, "127.0.0.1"); await once(server, "listening");
      return { id, host: "127.0.0.1", port: server.address().port };
    },
    async accept(id) {
      const listener = listeners.get(id);
      if (!listener) throw new Error("Listener closed");
      if (listener.queue.length) return listener.queue.shift();
      if (listener.pending) throw new Error("Concurrent accept");
      const pending = deferred(); listener.pending = pending; return pending.promise;
    },
    async closeListener(id) {
      const listener = listeners.get(id); if (!listener) return;
      listeners.delete(id);
      if (listener.pending) listener.pending.reject(new Error("Listener closed"));
      for (const socketId of listener.queue) await api.close(socketId);
      listener.server.close();
    },
    async shutdown() {
      for (const id of [...listeners.keys()]) await api.closeListener(id);
      for (const id of [...sockets.keys()]) await api.close(id);
    },
    counts: () => ({ listeners: listeners.size, sockets: sockets.size })
  };
  return api;
}
function fixture(t, initial = config([profile()]), gates = {}, native = localNative(), dependencies = {}) {
  let stored = initial, ids = 0;
  const api = {
    portmapTransport: native,
    proxy: { onRequest: event() },
    webRequest: { onBeforeRequest: event(), onBeforeSendHeaders: event(), onAuthRequired: event(), onCompleted: event(), onErrorOccurred: event() },
    storage: { onChanged: event(), local: {
      async get() { if (gates.read) await gates.read.promise; return { config: stored }; },
      async set(value) {
        if (gates.write) await gates.write.promise;
        const oldValue = stored; stored = value.config;
        api.storage.onChanged.emit({ config: { oldValue, newValue: stored } }, "local");
      }
    } },
    tabs: { opened: [], async create(value) { this.opened.push(value); } }
  };
  const runtime = createRuntime(api, dependencies);
  const controller = createController(api, () => `generated-${++ids}`, runtime);
  t.after(async () => { runtime.invalidate(); if (native?.shutdown) await native.shutdown(); });
  return { api, runtime, controller, native,
    state: () => controller.command({ action: "get" }), stored: () => stored,
    select: request => api.proxy.onRequest.emit(request)[0],
    block: request => api.webRequest.onBeforeRequest.emit(request)[0],
    auth: request => api.webRequest.onAuthRequired.emit(request)[0],
    external(value) { stored = value; api.storage.onChanged.emit({ config: { newValue: value } }, "local"); }
  };
}
const details = (url = "http://a.portmap/", requestId = "a") => ({ url, requestId });
function route(f, request = details()) {
  const selected = f.select(request);
  assert.ok(Array.isArray(selected)); assert.equal(selected[1], null);
  return { ...request, proxyInfo: selected[0] };
}
function blocked(f, request = details(), selected = f.select(request)) {
  assert.equal(selected[0].type, "http"); assert.equal(selected[0].host, "127.0.0.1");
  assert.equal(selected[0].port, 9); assert.equal(selected[1], null);
  assert.deepEqual(f.block({ ...request, proxyInfo: selected[0] }), { cancel: true });
}
function challenge(request, changes = {}) {
  return { ...request, isProxy: true, scheme: "basic", realm: "Portmap internal",
    challenger: { host: request.proxyInfo.host, port: request.proxyInfo.port }, ...changes };
}
async function origin(t) {
  const requests = [];
  const server = http.createServer(async (req, res) => {
    const chunks = []; for await (const chunk of req) chunks.push(chunk);
    const body = Buffer.concat(chunks);
    requests.push({ url: req.url, headers: req.headers, body });
    const payload = Buffer.concat([Buffer.from("origin:"), body]);
    res.writeHead(200, { "Content-Length": payload.length }); res.end(payload);
  });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  t.after(() => { server.closeAllConnections(); server.close(); });
  return { port: server.address().port, requests };
}
function exchange(proxyInfo, raw) {
  const socket = net.createConnection({ host: proxyInfo.host, port: proxyInfo.port });
  const chunks = [];
  socket.on("data", data => chunks.push(data));
  const finished = new Promise((resolve, reject) => { socket.on("end", () => resolve(Buffer.concat(chunks))); socket.on("error", reject); });
  socket.setTimeout(5000, () => socket.destroy(new Error("Test exchange deadline")));
  // Connection failure and response failure share one returned promise. Creating
  // a separate response promise before awaiting connect orphaned its rejection
  // when the deliberately revoked listener correctly refused the connection.
  socket.once("connect", () => socket.write(Buffer.from(raw, "latin1")));
  return finished;
}
function authorization(f, request) {
  const credentials = f.auth(challenge(request)).authCredentials;
  return "Basic " + Buffer.from(`${credentials.username}:${credentials.password}`).toString("base64");
}

test("synchronous listeners protect startup, first navigation waits bounded local capability and storage", async t => {
  const read = deferred(); const f = fixture(t, config([profile()]), { read });
  assert.deepEqual(f.api.webRequest.onBeforeRequest.listeners[0].args[1], ["blocking"]);
  assert.equal(f.api.webRequest.onAuthRequired.listeners.length, 1);
  const request = details(); const pending = f.select(request);
  assert.ok(pending instanceof Promise); assert.deepEqual(f.block(request), { cancel: true });
  assert.equal(f.select(details("https://ordinary.example/")), undefined);
  read.resolve(); const selected = await pending;
  assert.equal(selected[0].type, "http"); assert.notEqual(selected[0].port, 9);
  assert.deepEqual(f.block({ ...request, proxyInfo: selected[0] }), {});
  assert.equal((await f.state()).capability.available, true); assert.equal(f.native.checks, 1);
});

test("missing or failing capability disables both modes and reset cannot enable networking", async t => {
  for (const native of [undefined, { selfCheck: async () => true }, Object.assign(localNative(), { selfCheck: async () => { throw new Error("private failure"); } })]) {
    const f = fixture(t, config([profile()]), {}, native === undefined ? null : native);
    const state = await f.state(); assert.equal(state.phase, "unsupported"); assert.equal(state.capability.available, false);
    assert.match(state.error, /compatible Firefox/); assert.doesNotMatch(state.error, /private failure/);
    blocked(f);
    for (const action of ["save", "toggle", "open"]) await assert.rejects(f.controller.command({ action, connection: profile(), id: "a", scheme: "http" }));
    assert.equal((await f.controller.command({ action: "reset" })).phase, "unsupported"); blocked(f);
  }
});

test("old schema is preserved, explicit reset is the only rebuild path", async t => {
  const old = { schemaVersion: 2, revision: "old", connections: [{ proxyPort: 1080, password: "old-secret" }] };
  const f = fixture(t, old); const state = await f.state();
  assert.equal(state.phase, "error"); assert.match(state.error, /preserved/); assert.deepEqual(f.stored(), old); blocked(f);
  await f.controller.command({ action: "reset" }); assert.equal(f.stored().schemaVersion, 3); assert.deepEqual(f.stored().connections, []);
});

test("actual HTTP loopback ProxyInfo and exact current challenge are mandatory, origins never get proxy secrets", async t => {
  const f = fixture(t, config([profile(), profile({ id: "b", domain: "b.portmap" })])); await f.state();
  const a = route(f), b = route(f, details("http://b.portmap/", "b"));
  assert.notEqual(a.proxyInfo.port, b.proxyInfo.port);
  assert.deepEqual(f.block(a), {});
  for (const proxyInfo of [undefined, { type: "direct" }, { ...a.proxyInfo, port: b.proxyInfo.port }, { ...a.proxyInfo, type: "socks" }]) assert.deepEqual(f.block({ ...a, proxyInfo }), { cancel: true });
  assert.deepEqual(f.auth(challenge(a, { isProxy: false })), {});
  for (const changes of [{ realm: "foreign" }, { scheme: "digest" }, { challenger: { host: "localhost", port: a.proxyInfo.port } },
    { challenger: { host: "127.0.0.1", port: b.proxyInfo.port } }, { requestId: "unselected" }]) assert.deepEqual(f.auth(challenge(a, changes)), { cancel: true });
  assert.notEqual(authorization(f, a), authorization(f, b));
  f.api.webRequest.onCompleted.emit(a); assert.deepEqual(f.auth(challenge(a)), { cancel: true });
});

test("namespace and logical-port selection is fail closed, ordinary browser chain remains untouched", async t => {
  const f = fixture(t); await f.state();
  for (const url of ["http://unmapped.portmap/", "http://portmap/", "ftp://a.portmap/", "http://a.portmap:3000/", "https://a.portmap/"]) blocked(f, details(url));
  const child = route(f, details("ws://deep.a.portmap/", "child")); assert.deepEqual(f.block(child), {});
  assert.equal(f.select(details("http://a.portmap.evil/")), undefined);
});

test("shipped HTTP proxy forwards binary POST path and origin auth, strips internal auth and blocks pipelines", async t => {
  const upstream = await origin(t); const f = fixture(t, config([profile({ httpPort: upstream.port })])); await f.state();
  const selected = route(f); const auth = authorization(f, selected); const body = "\0\xffbinary";
  const response = await exchange(selected.proxyInfo, `POST http://a.portmap/a%2Fb?q=x%20y HTTP/1.1\r\nHost: a.portmap\r\nProxy-Authorization: ${auth}\r\nAuthorization: Bearer origin-token\r\nContent-Length: ${body.length}\r\n\r\n${body}GET http://a.portmap/forbidden-pipeline HTTP/1.1\r\nHost: a.portmap\r\n\r\n`);
  assert.match(response.toString("latin1"), /^HTTP\/1.1 200/); assert.ok(response.includes(Buffer.from("origin:" + body, "latin1")));
  assert.equal(upstream.requests.length, 1); assert.equal(upstream.requests[0].url, "/a%2Fb?q=x%20y");
  assert.equal(upstream.requests[0].headers.authorization, "Bearer origin-token"); assert.equal(upstream.requests[0].headers["proxy-authorization"], undefined);
  assert.deepEqual(upstream.requests[0].body, Buffer.from(body, "latin1"));
});

test("proxy enforces Basic authentication, independent namespace Host, duplicate framing and CONNECT bounds before any upstream opens", async t => {
  const upstream = await origin(t); const f = fixture(t, config([profile({ httpPort: upstream.port })])); await f.state();
  const selected = route(f); const auth = authorization(f, selected);
  const cases = [
    ["GET http://a.portmap/ HTTP/1.1\r\nHost: a.portmap\r\n\r\n", 407],
    [`GET http://foreign.portmap/ HTTP/1.1\r\nHost: foreign.portmap\r\nProxy-Authorization: ${auth}\r\n\r\n`, 403],
    [`GET http://a.portmap/ HTTP/1.1\r\nHost: child.a.portmap\r\nProxy-Authorization: ${auth}\r\n\r\n`, 403],
    [`POST http://a.portmap/ HTTP/1.1\r\nHost: a.portmap\r\nProxy-Authorization: ${auth}\r\nContent-Length: 0\r\nContent-Length: 0\r\n\r\n`, 400],
    [`CONNECT a.portmap:22 HTTP/1.1\r\nHost: a.portmap:22\r\nProxy-Authorization: ${auth}\r\n\r\n`, 403]
  ];
  for (const [raw, status] of cases) assert.match((await exchange(selected.proxyInfo, raw)).toString("latin1"), new RegExp(`^HTTP/1.1 ${status}`));
  assert.equal(upstream.requests.length, 0);
});

test("mutations revoke selection/auth synchronously, close listeners and derive allowlist only from enabled saved nodes", async t => {
  const f = fixture(t, config([profile(), profile({ id: "b", domain: "b.portmap", host: "192.0.2.10", enabled: false })])); await f.state();
  assert.deepEqual(f.native.configured, [{ host: "127.0.0.1", port: 8080 }]);
  const old = route(f); const oldAuth = authorization(f, old);
  f.external(config([profile({ httpPort: 9090 })]));
  assert.deepEqual(f.block(old), { cancel: true }); assert.deepEqual(f.auth(challenge(old)), { cancel: true }); blocked(f);
  await f.state(); const fresh = route(f, details("http://a.portmap/", "fresh"));
  assert.notEqual(fresh.proxyInfo.connectionIsolationKey, old.proxyInfo.connectionIsolationKey);
  assert.notEqual(authorization(f, fresh), oldAuth); assert.deepEqual(f.native.configured, [{ host: "127.0.0.1", port: 9090 }]);
  await assert.rejects(exchange(old.proxyInfo, "GET / HTTP/1.1\r\n\r\n"));
  await f.controller.command({ action: "toggle", id: "a" }); blocked(f); assert.deepEqual(f.native.configured, []);
});

test("startup generation cannot authorize a navigation waiting on replaced storage", async t => {
  const read = deferred(); const f = fixture(t, config([profile()]), { read });
  const request = details(); const pending = f.select(request); f.external(config([profile({ httpPort: 9090 })])); read.resolve();
  blocked(f, request, await pending); assert.equal((await f.state()).phase, "ready");
});

test("per-node lazy SSH is single flight and an offline node never disables another namespace", async t => {
  const upstream = await origin(t); const connecting = deferred(); let calls = 0, closes = 0, opens = 0;
  const ssh = { connect: async () => { calls++; return connecting.promise; } };
  const proxy = { start(native, open, options) {
    return globalThis.PortmapProxy.start(native, port => { opens++; return open(port); }, options);
  } };
  const sshEntry = profile({ id: "s", domain: "s.portmap", mode: "ssh", ssh: { port: 22, username: "u", targetHost: "127.0.0.1",
    hostKeySha256Hex: "ab".repeat(32), auth: { type: "password", password: "private-password" } } });
  const f = fixture(t, config([profile({ httpPort: upstream.port }), sshEntry]), {}, localNative(), { ssh, proxy }); await f.state();
  assert.equal(calls, 0); const s = route(f, details("http://s.portmap/", "s")); const sAuth = authorization(f, s);
  const one = exchange(s.proxyInfo, `GET http://s.portmap/ HTTP/1.1\r\nHost: s.portmap\r\nProxy-Authorization: ${sAuth}\r\n\r\n`);
  const two = exchange(s.proxyInfo, `GET http://s.portmap/ HTTP/1.1\r\nHost: s.portmap\r\nProxy-Authorization: ${sAuth}\r\n\r\n`);
  const deadline = Date.now() + 3000;
  while (opens < 2 && Date.now() < deadline) await new Promise(resolve => setImmediate(resolve));
  assert.equal(opens, 2); assert.equal(calls, 1);
  const a = route(f); const response = await exchange(a.proxyInfo, `GET http://a.portmap/ HTTP/1.1\r\nHost: a.portmap\r\nProxy-Authorization: ${authorization(f, a)}\r\n\r\n`);
  assert.match(response.toString(), /^HTTP\/1.1 200/);
  connecting.reject(new Error("private-password"));
  for (const response of await Promise.all([one, two])) assert.match(response.toString(), /^HTTP\/1.1 502/);
  const state = await f.state(); assert.equal(state.phase, "ready"); assert.equal(state.nodes.s.phase, "error"); assert.doesNotMatch(JSON.stringify(state.nodes), /private-password/);
  // Late authenticated sessions must close rather than attach to edited config.
  const gate = deferred();
  let open;
  const lateNative = localNative(); const late = createRuntime({ portmapTransport: lateNative }, { proxy: { async start(_native, upstreamOpen) { open = upstreamOpen; return { host: "127.0.0.1", port: 10002, close: async () => {}, credentials: {} }; } }, ssh: { connect: () => gate.promise } });
  await late.check(); await late.configure([core.normalizeConnection(sshEntry)], "late");
  const opening = open(80); await Promise.resolve(); late.invalidate(); gate.resolve({ close: async () => { closes++; }, open: async () => { throw new Error("Must not attach"); } });
  await assert.rejects(opening); assert.equal(closes, 1); await lateNative.shutdown();
});

test("queued edits preserve independent entries, failed writes fail closed, open uses only configured namespace", async t => {
  const f = fixture(t); await f.state();
  await Promise.all(["b", "c"].map(id => f.controller.command({ action: "save", connection: profile({ id, domain: `${id}.portmap` }) })));
  assert.equal((await f.state()).connections.length, 3);
  await assert.rejects(f.controller.command({ action: "save", connection: profile({ id: "overlap", domain: "child.a.portmap" }) }));
  await f.controller.command({ action: "open", id: "a", scheme: "http", url: "http://evil/" });
  assert.deepEqual(f.api.tabs.opened, [{ url: "http://a.portmap/" }]); await assert.rejects(f.controller.command({ action: "open", id: "a", scheme: "https" }));
  const write = deferred(); const failure = fixture(t, config([profile()]), { write }); await failure.state();
  const saved = failure.controller.command({ action: "toggle", id: "a" }); await Promise.resolve(); blocked(failure);
  write.reject(new Error("secret backend detail")); await assert.rejects(saved, /Saving local connections failed/);
  assert.equal((await failure.state()).phase, "error"); blocked(failure);
});

test("SSH verifies host key before password or RSA key auth and terminally frees failed or cancelled sessions", async () => {
  const vm = require("node:vm");
  const source = require("node:fs").readFileSync(require.resolve("../ssh/ssh.js"), "utf8");
  for (const authType of ["password", "key"]) {
    for (const pinMatches of [false, true]) {
      const pendingRead = deferred(); let allocations = 128, authCalls = 0, freed = 0, closed = 0;
      const module = {
        HEAPU8: new Uint8Array(65536),
        _probe_create: () => 1, _probe_handshake: () => 0, _probe_fingerprint: () => 32,
        _malloc: size => { const pointer = allocations; allocations += size; return pointer; },
        _free: () => {},
        _probe_password: () => { authCalls++; return 0; },
        _probe_key: () => { authCalls++; return 0; },
        _probe_abort_free: () => { freed++; return 0; }
      };
      const context = vm.createContext({ TextEncoder, Uint8Array, setTimeout, clearTimeout, queueMicrotask,
        browser: { runtime: { getURL: name => name } }, NightlySshWasm: async () => module });
      vm.runInContext(source, context);
      const native = { connect: async () => 1, read: () => pendingRead.promise,
        close: async () => { closed++; pendingRead.resolve(""); } };
      const input = { host: "127.0.0.1", port: 22, username: "user",
        hostKeySha256Hex: (pinMatches ? "00" : "ab").repeat(32), auth: authType === "password"
          ? { type: "password", password: "private-password" }
          : { type: "key", publicKey: "public-key", privateKey: "private-key", passphrase: "private-passphrase" } };
      if (pinMatches) {
        const session = await context.PortmapSSH.connect(input, native);
        assert.equal(authCalls, 1); assert.equal(session.closed, false);
        await session.close(); assert.equal(session.closed, true);
        assert.ok(module.HEAPU8.subarray(128, allocations).every(value => value === 0), "temporary auth memory must be wiped");
      } else {
        await assert.rejects(context.PortmapSSH.connect(input, native), /host key mismatch/);
        assert.equal(authCalls, 0, "pin mismatch must make every authentication method unreachable");
      }
      assert.equal(freed, 1); assert.equal(closed, 1);
    }
  }
  const cancelled = new AbortController(); cancelled.abort();
  let connects = 0;
  const context = vm.createContext({ TextEncoder, Uint8Array, setTimeout, clearTimeout, queueMicrotask,
    browser: { runtime: { getURL: name => name } }, NightlySshWasm: async () => ({}) });
  vm.runInContext(source, context);
  await assert.rejects(context.PortmapSSH.connect({ host: "127.0.0.1", port: 22, username: "user",
    hostKeySha256Hex: "00".repeat(32), auth: { type: "password", password: "private" } },
  { connect: async () => { connects++; } }, { signal: cancelled.signal }), /cancelled/);
  assert.equal(connects, 0);
});

test("editing an active direct node tears down owned client and upstream streams", { timeout: 5000 }, async t => {
  const seen = deferred(), ended = deferred();
  const server = http.createServer((req, res) => {
    req.socket.once("close", () => ended.resolve());
    res.writeHead(200, { "Content-Type": "application/octet-stream" });
    res.write("held-response");
    seen.resolve();
  });
  server.listen(0, "127.0.0.1"); await once(server, "listening");
  t.after(() => { server.closeAllConnections(); server.close(); });
  const f = fixture(t, config([profile({ httpPort: server.address().port })])); await f.state();
  const selected = route(f);
  const socket = net.createConnection({ host: selected.proxyInfo.host, port: selected.proxyInfo.port });
  socket.on("data", () => {});
  const clientClosed = once(socket, "close");
  await once(socket, "connect");
  socket.write(`GET http://a.portmap/hold HTTP/1.1\r\nHost: a.portmap\r\nProxy-Authorization: ${authorization(f, selected)}\r\n\r\n`);
  await seen.promise;
  await f.controller.command({ action: "save", connection: profile({ httpPort: server.address().port, name: "Edited" }) });
  await Promise.all([clientClosed, ended.promise]);
  assert.deepEqual(f.block(selected), { cancel: true });
  assert.deepEqual(f.auth(challenge(selected)), { cancel: true });
  assert.equal((await f.state()).phase, "ready");
});

test("native API grants handles only to background and exact configured destinations, then revokes on shutdown", async () => {
  const vm = require("node:vm");
  const source = require("node:fs").readFileSync(require.resolve("../transport/api.js"), "utf8");
  let nativeCalls = 0, shutdown;
  const context = vm.createContext({
    ChromeUtils: { importESModule: () => ({ ExtensionCommon: { ExtensionAPI: class {} } }) },
    Services: { tm: { currentThread: {} } },
    Cc: { "@mozilla.org/network/socket-transport-service;1": { getService() { nativeCalls++; throw new Error("Native service reached"); } } },
    Ci: {}, Cr: {}
  });
  vm.runInContext(source, context);
  const experiment = new context.portmapTransport();
  assert.equal(Object.keys(experiment.getAPI({ viewType: "tab" })).length, 0);
  const native = experiment.getAPI({ viewType: "background", callOnClose(value) { shutdown = value.close; } }).portmapTransport;
  await native.configure([{ host: "192.0.2.1", port: 8080 }]);
  for (const [host, port] of [["192.0.2.1", 8081], ["192.0.2.2", 8080], ["a.portmap", 8080]]) {
    await assert.rejects(native.connect(host, port), /not an enabled saved endpoint/);
  }
  assert.equal(nativeCalls, 0);
  await assert.rejects(native.connect("192.0.2.1", 8080), /Native service reached/);
  assert.equal(nativeCalls, 1);
  await native.configure([]);
  await assert.rejects(native.connect("192.0.2.1", 8080), /not an enabled saved endpoint/);
  for (const value of [[{ host: "host/path", port: 80 }], [{ host: "host", port: 0 }], Array(513).fill({ host: "host", port: 80 })]) {
    await assert.rejects(native.configure(value), /Invalid endpoint allowlist/);
  }
  shutdown();
  await assert.rejects(native.configure([]), /Invalid endpoint allowlist/);
  assert.equal(nativeCalls, 1);
});

test("shipped proxy canonicalizes trailing-dot root and descendant authorities without rewriting Host path or origin", async t => {
  const upstream = await origin(t);
  const f = fixture(t, config([profile({ httpPort: upstream.port })])); await f.state();
  const cases = [
    { target: "A.PORTMAP.", host: "a.portmap.", path: "/root%2Fpath?q=x%20y" },
    { target: "deep.APP.a.PORTMAP.", host: "DEEP.app.A.portmap", path: "/descendant%2fpath?key=%2B" }
  ];
  for (const [index, entry] of cases.entries()) {
    const selected = route(f, details(`http://${entry.target}${entry.path}`, `root-dot-${index}`));
    assert.deepEqual(f.block(selected), {});
    const originHeader = `http://${entry.target}`;
    const response = await exchange(selected.proxyInfo,
      `GET http://${entry.target}${entry.path} HTTP/1.1\r\nHost: ${entry.host}\r\nOrigin: ${originHeader}\r\nProxy-Authorization: ${authorization(f, selected)}\r\n\r\n`);
    assert.match(response.toString("latin1"), /^HTTP\/1.1 200/);
    assert.equal(upstream.requests[index].url, entry.path);
    assert.equal(upstream.requests[index].headers.host, entry.host);
    assert.equal(upstream.requests[index].headers.origin, originHeader);
  }
  const selected = route(f, details("http://a.portmap./", "root-dot-denial"));
  const auth = authorization(f, selected);
  for (const [target, host] of [["a.portmap.", "child.a.portmap."], ["foreign.portmap.", "foreign.portmap."]]) {
    const response = await exchange(selected.proxyInfo,
      `GET http://${target}/ HTTP/1.1\r\nHost: ${host}\r\nProxy-Authorization: ${auth}\r\n\r\n`);
    assert.match(response.toString("latin1"), /^HTTP\/1.1 403/);
  }
  assert.equal(upstream.requests.length, cases.length, "denied canonical authorities must not open upstream requests");
});
