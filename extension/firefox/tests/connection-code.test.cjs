/* SPDX-License-Identifier: MIT */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const codec = require("../connection-code.js");
const core = require("../core.js");
const PREFIX = "portmap-connect:v1:";
const ERROR = "Invalid or unsupported Portmap connection QR code.";
const direct = { name: "Node A", domain: "a.portmap", mode: "direct", host: "192.0.2.10", httpPort: 8080, httpsPort: null };
const directJson = '{"domain":"a.portmap","host":"192.0.2.10","httpPort":8080,"httpsPort":null,"mode":"direct","name":"Node A"}';
const directWire = "portmap-connect:v1:eyJkb21haW4iOiJhLnBvcnRtYXAiLCJob3N0IjoiMTkyLjAuMi4xMCIsImh0dHBQb3J0Ijo4MDgwLCJodHRwc1BvcnQiOm51bGwsIm1vZGUiOiJkaXJlY3QiLCJuYW1lIjoiTm9kZSBBIn0";
const ssh = { name: "节点 🛰", domain: "xn--fsqu00a.portmap", mode: "ssh", host: "2001:db8::1", httpPort: 1, httpsPort: 65535,
  ssh: { port: 2222, username: "", targetHost: "::ffff:c000:201", hostKeySha256Hex: "ab".repeat(32) } };
const sshWire = "portmap-connect:v1:eyJkb21haW4iOiJ4bi0tZnNxdTAwYS5wb3J0bWFwIiwiaG9zdCI6IjIwMDE6ZGI4OjoxIiwiaHR0cFBvcnQiOjEsImh0dHBzUG9ydCI6NjU1MzUsIm1vZGUiOiJzc2giLCJuYW1lIjoi6IqC54K5IPCfm7AiLCJzc2giOnsiaG9zdEtleVNoYTI1NkhleCI6ImFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWJhYmFiYWIiLCJwb3J0IjoyMjIyLCJ0YXJnZXRIb3N0IjoiOjpmZmZmOmMwMDA6MjAxIiwidXNlcm5hbWUiOiIifX0";
const wireJson = json => PREFIX + Buffer.from(json, "utf8").toString("base64url");
const canonical = value => value && typeof value === "object" ? `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}` : JSON.stringify(value);
const rejects = callback => assert.throws(callback, error => error instanceof Error && error.message === ERROR);

test("fixed Python/JS public-wire vectors roundtrip without mutation", () => {
  for (const [draft, wire] of [[direct, directWire], [ssh, sshWire]]) {
    const before = structuredClone(draft);
    assert.equal(codec.encode(draft), wire);
    assert.deepEqual(codec.decode(wire), draft);
    assert.deepEqual(draft, before);
    assert.ok(Object.isFrozen(codec.decode(wire)));
  }
  assert.ok(Object.isFrozen(codec.decode(sshWire).ssh));
  assert.equal(codec.MAX_WIRE_BYTES, 2048);
});

test("producer uses core normalization and never invents SSH auth/user", () => {
  const draft = { ...ssh, name: "\ufeff节点 🛰\u00a0", domain: " XN--FSQU00A.PORTMAP. ", host: "[2001:0DB8:0:0::1]",
    ssh: { ...ssh.ssh, targetHost: "::ffff:192.0.2.1", hostKeySha256Hex: "SHA256:" + Buffer.alloc(32, 171).toString("base64").replace(/=$/, "") } };
  assert.equal(codec.encode(draft), sshWire);
  const decoded = codec.decode(sshWire);
  assert.equal(decoded.ssh.username, "");
  assert.equal(Object.hasOwn(decoded.ssh, "auth"), false);
  assert.throws(() => core.normalizeConnection({ ...decoded, id: "new", enabled: true }));
  const ready = core.normalizeConnection({ ...decoded, id: "new", enabled: true,
    ssh: { ...decoded.ssh, username: "owned-user", auth: { type: "password", password: "owned-secret" } } });
  assert.equal(ready.host, decoded.host);
  assert.equal(ready.ssh.username, "owned-user");
});

test("every required key is mandatory; direct cannot carry SSH", () => {
  for (const key of Object.keys(direct)) {
    const missing = { ...direct };
    delete missing[key];
    rejects(() => codec.encode(missing));
  }
  for (const key of Object.keys(ssh.ssh)) {
    const missing = { ...ssh, ssh: { ...ssh.ssh } };
    delete missing.ssh[key];
    rejects(() => codec.encode(missing));
  }
  rejects(() => codec.encode({ ...direct, ssh: ssh.ssh }));
});

test("public schema rejects secrets and identity/control fields at every allowed object", () => {
  for (const field of ["auth", "password", "privateKey", "passphrase", "token", "id", "enabled", "update", "url", "credentials", "__proto__"]) {
    for (const value of [{ ...direct, [field]: "do-not-echo" }, { ...ssh, ssh: { ...ssh.ssh, [field]: "do-not-echo" } }]) {
      rejects(() => codec.encode(value));
      rejects(() => codec.decode(wireJson(canonical(value))));
    }
  }
});

test("invalid names domains hosts ports and fingerprints fail safely", () => {
  for (const change of [{ name: "" }, { name: " " }, { name: "x".repeat(121) }, { name: "🛰".repeat(61) },
    { name: "x\x1by" }, { name: "\ud800" }, { domain: "portmap" }, { domain: "a..portmap" }, { domain: "例子.portmap" },
    { host: "a.portmap" }, { host: "http://example.test" }, { host: "user@example.test" }, { host: "host:80" },
    { host: "256.1.2.3" }, { host: "127.1" }, { host: "010.0.0.1" }, { host: "[::1]:80" }, { host: "fe80::1%eth0" },
    { httpPort: 0 }, { httpPort: 65536 }, { httpPort: true }, { httpPort: "80" }, { httpPort: 1.5 },
    { httpsPort: 0 }, { httpsPort: undefined }, { mode: "socks" }]) rejects(() => codec.encode({ ...direct, ...change }));
  for (const change of [{ username: null }, { username: "x\ny" }, { username: "x".repeat(256) },
    { hostKeySha256Hex: "*" }, { hostKeySha256Hex: "SHA256:" + "A".repeat(42) + "B" }, { targetHost: "a.portmap" }, { port: false }]) {
    rejects(() => codec.encode({ ...ssh, ssh: { ...ssh.ssh, ...change } }));
  }
});

test("strict canonical decode rejects duplicate keys, aliases, floats, escaping and reordering", () => {
  for (const json of [directJson.replace('"name":"Node A"', '"name":"stolen","name":"Node A"'),
    canonical(ssh).replace('"username":""', '"username":"stolen","username":""'),
    " " + directJson, directJson + "\n", JSON.stringify(direct), directJson.replace("Node A", "Node \\u0041"),
    directJson.replace("8080", "8080.0"), directJson.replace("8080", "8.08e3"),
    directJson.replace("192.0.2.10", "192.0.2.10."), directJson.replace("a.portmap", "A.PORTMAP"),
    "\ufeff" + directJson]) rejects(() => codec.decode(wireJson(json)));
});

test("malformed UTF8 base64 versions foreign URLs and oversized wires never escape safe error", () => {
  for (const wire of ["", "https://example.test/pair", "portmap-connect:v2:AA", directWire + "=", directWire + "\n",
    PREFIX + "A", PREFIX + "+/", PREFIX + "_w", PREFIX + "eyJ4Ijoi7aCAIn0", PREFIX + "A".repeat(2048),
    directWire.slice(0, -1) + "1", PREFIX + "null", null, 42]) rejects(() => codec.decode(wire));
});

test("Unicode boundaries and large payload limit match UTF16/core semantics", () => {
  const name = "🛰".repeat(60);
  assert.equal(codec.decode(codec.encode({ ...direct, name })).name, name);
  const separatorName = "\u0085visible\u2028separator\u2029";
  const normalizedName = core.normalizeConnection({ ...direct, id: "owned", enabled: true, name: separatorName }).name;
  assert.equal(codec.decode(codec.encode({ ...direct, name: separatorName })).name, normalizedName);
  const longHost = ["h".repeat(63), "h".repeat(63), "h".repeat(63), "h".repeat(61)].join(".");
  const maximal = { ...ssh, name, host: longHost, ssh: { ...ssh.ssh, targetHost: longHost, username: "汉".repeat(181) } };
  assert.equal(codec.encode(maximal).length, 2047);
  assert.deepEqual(codec.decode(codec.encode(maximal)), maximal);
  maximal.ssh.username += "x";
  assert.equal(wireJson(canonical(maximal)).length, 2049);
  rejects(() => codec.encode(maximal));
  for (const port of [1, 65535]) assert.equal(codec.decode(codec.encode({ ...direct, httpPort: port, httpsPort: port })).httpPort, port);
});

test("browser-global codec is inert and cannot fetch navigate or persist a code", () => {
  const forbidden = () => { throw new Error("unexpected external action"); };
  const context = vm.createContext({ TextEncoder, TextDecoder, atob, btoa, URL,
    fetch: forbidden, eval: forbidden, browser: { storage: { local: { set: forbidden } } }, location: { assign: forbidden } });
  vm.runInContext(fs.readFileSync(require.resolve("../core.js"), "utf8"), context);
  vm.runInContext(fs.readFileSync(require.resolve("../connection-code.js"), "utf8"), context);
  assert.equal(context.PortmapConnectionCode.encode(direct), directWire);
  assert.equal(context.PortmapConnectionCode.decode(directWire).domain, "a.portmap");
  assert.throws(() => context.PortmapConnectionCode.decode("https://attacker.test/not-a-node"), error => error.message === ERROR);
});
