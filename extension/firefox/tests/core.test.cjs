/* SPDX-License-Identifier: MIT */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../core.js");

const connection = overrides => ({ id: "node-a", name: "Node A", domain: "a.portmap", proxyHost: "100.64.0.1",
  proxyPort: 8443, username: "user", password: "secret", enabled: true, ...overrides });
const codePayload = overrides => ({ name: "Node A", domain: "a.portmap", proxyHost: "100.64.0.1",
  proxyPort: 8443, username: "user", password: "secret", ...overrides });
const encodeCode = value => `portmap-connect-v1:${Buffer.from(value).toString("base64url")}`;
const connectionCode = payload => encodeCode(JSON.stringify(payload));

test("claims normalize ASCII names and match whole labels, not suffix lookalikes", () => {
  const profile = core.normalizeConnection(connection({ domain: " A.Portmap. " }));
  assert.equal(profile.domain, "a.portmap");
  for (const host of ["a.portmap", "x.a.portmap", "deep.x.a.portmap"]) assert.ok(core.matchesDomain(host, profile.domain));
  for (const host of ["nota.portmap", "a.portmap.evil", "a-portmap", "portmap"]) assert.equal(core.matchesDomain(host, profile.domain), false);
});

test("URL inspection recognizes portmap aliases without rewriting path, query, port or fragment", () => {
  const original = "https://APP.A.PORTMAP.:443/a%2Fb?q=a%20b#fragment";
  assert.deepEqual(core.inspectUrl(original), { host: "app.a.portmap", portmap: true, supported: true });
  for (const url of ["http://portmap/", "ws://a.portmap:3000/socket", "wss://a.portmap/", "http://a.portmap../"]) {
    assert.equal(core.inspectUrl(url).portmap, true);
  }
  for (const url of ["http://a.portmap.evil/", "http://evil/?next=a.portmap", "http://a.portmap@evil/", "not a URL"]) {
    assert.equal(core.inspectUrl(url).portmap, false);
  }
  assert.equal(core.inspectUrl("ftp://a.portmap/file").supported, false);
});

test("invalid and ambiguous configuration fails as a whole, even for disabled entries", () => {
  for (const overrides of [
    { domain: "portmap" }, { domain: "x.portmap.evil" }, { domain: "-x.portmap" }, { domain: "x..portmap" },
    { domain: "https://a.portmap" }, { domain: "a.portmap/path" }, { proxyHost: "a.portmap" },
    { proxyHost: "https://proxy:443" }, { proxyHost: "proxy/path" }, { proxyHost: "proxy@evil" },
    { proxyPort: "8443" }, { proxyPort: 0 }, { proxyPort: 65536 }, { proxyPort: 1.5 },
    { enabled: 1 }, { username: "a\r\nb" }, { username: "é".repeat(128) },
    { username: "\ud800" }, { password: "x".repeat(256) }, { password: "" },
    { password: "abc\u0000def" }, { id: "has space" }
  ]) assert.throws(() => core.normalizeConnection(connection(overrides)));
  for (const domain of ["a.portmap", "child.a.portmap", "portmap"]) {
    assert.throws(() => core.validateConnections([connection(), connection({ id: "b", domain, enabled: false })]));
  }
  assert.throws(() => core.validateConnections([connection(), connection({ domain: "b.portmap" })]));
  assert.equal(core.validateConnections([connection(), connection({ id: "b", domain: "b.portmap" })]).length, 2);
});

test("SOCKS5 uses authenticated remote DNS without HTTP credential headers", () => {
  const route = core.compileConnections([core.normalizeConnection(connection({ username: "用户:name", password: "päss:word" }))], "epoch")[0];
  assert.equal(route.proxy.type, "socks");
  assert.equal(route.proxy.proxyDNS, true);
  assert.equal(route.proxy.username, "用户:name");
  assert.equal(route.proxy.password, "päss:word");
  assert.deepEqual(Object.keys(route.proxy).sort(), ["connectionIsolationKey", "failoverTimeout", "host", "port", "username", "password", "proxyDNS", "type"].sort());
  assert.notEqual(core.compileConnections([connection()], "new")[0].proxy.connectionIsolationKey, route.proxy.connectionIsolationKey);
});

test("SOCKS5 credential limits count valid UTF-8 bytes, not UTF-16 characters", () => {
  assert.equal(core.normalizeConnection(connection({ username: "a".repeat(255) })).username.length, 255);
  assert.equal(core.normalizeConnection(connection({ password: "é".repeat(127) + "a" })).password.length, 128);
  for (const overrides of [{ username: "a".repeat(256) }, { password: "é".repeat(128) },
    { username: "\udfff" }, { password: "\ud800" }]) {
    assert.throws(() => core.normalizeConnection(connection(overrides)));
  }
});

test("storage schema is strict and first installation has no implicit connection", () => {
  assert.deepEqual(core.readConfig(undefined).connections, []);
  for (const stored of [null, {}, { schemaVersion: 1, revision: "x", connections: [] },
    { schemaVersion: 3, revision: "x", connections: [] },
    { schemaVersion: 2, revision: "x", connections: [connection({ domain: "evil" })] }]) {
    assert.throws(() => core.readConfig(stored));
  }
});

test("connection codes decode six normalized fields without granting a stored identity or enabled flag", () => {
  const payload = codePayload({ name: "  节点 A  ", domain: " A.Portmap. ", proxyHost: "[2001:0db8::1]",
    username: "用户:name", password: "päss\\\"word:with:colons" });
  const decoded = core.decodeConnectionCode(` \n${connectionCode(payload)}\t `);
  assert.deepEqual(decoded, { ...payload, name: "节点 A", domain: "a.portmap", proxyHost: "[2001:db8::1]" });
  assert.equal(Object.isFrozen(decoded), true);
  assert.equal(Object.hasOwn(decoded, "id"), false);
  assert.equal(Object.hasOwn(decoded, "enabled"), false);
});

test("connection code parser rejects malformed framing, noncanonical base64url and invalid UTF-8", () => {
  const valid = connectionCode(codePayload());
  let paddedJson = JSON.stringify(codePayload());
  while (Buffer.byteLength(paddedJson) % 3 !== 1) paddedJson += " ";
  const canonical = encodeCode(paddedJson);
  // With one trailing input byte, only the first two bits of the final symbol
  // carry data; changing an unused bit must not yield another accepted code.
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  const last = alphabet.indexOf(canonical.at(-1));
  const noncanonical = canonical.slice(0, -1) + alphabet[last + 1];
  for (const value of [null, {}, "", valid.replace("v1:", "v2:"), valid.slice(1),
    `${valid}=`, `${valid.slice(0, 30)}\n${valid.slice(30)}`,
    "portmap-connect-v1:A", "portmap-connect-v1:+/", noncanonical,
    encodeCode("{"), encodeCode("null"), encodeCode("[]"),
    encodeCode(Buffer.from([0xff])), encodeCode(Buffer.from([0xc0, 0xaf])),
    encodeCode(Buffer.from([0xe2, 0x82])), encodeCode(Buffer.from([0xed, 0xa0, 0x80])),
    encodeCode(Buffer.concat([Buffer.from([0xef, 0xbb, 0xbf]), Buffer.from(JSON.stringify(codePayload()))]))]) {
    assert.throws(() => core.decodeConnectionCode(value));
  }
  const [beforeName, afterName] = JSON.stringify(codePayload()).split("Node A");
  for (const invalidBytes of [[0xff], [0xc0, 0xaf], [0xe2, 0x82], [0xed, 0xa0, 0x80]]) {
    // A lossy decoder would turn these bytes into an otherwise valid name.
    const bytes = Buffer.concat([Buffer.from(beforeName), Buffer.from(invalidBytes), Buffer.from(afterName)]);
    assert.throws(() => core.decodeConnectionCode(encodeCode(bytes)));
  }
});

test("connection codes require exactly the six fields and reuse existing field validators", () => {
  const payload = codePayload();
  for (const key of Object.keys(payload)) {
    const missing = { ...payload };
    delete missing[key];
    assert.throws(() => core.decodeConnectionCode(connectionCode(missing)));
  }
  for (const extra of ["id", "enabled", "version", "__proto__", "unexpected"]) {
    assert.throws(() => core.decodeConnectionCode(connectionCode({ ...payload, [extra]: "injected" })));
  }
  for (const overrides of [{ name: " " }, { name: "x".repeat(121) }, { domain: "portmap" },
    { domain: "https://a.portmap" }, { proxyHost: "a.portmap" }, { proxyHost: "http://proxy:8443" },
    { proxyPort: "8443" }, { proxyPort: 0 }, { proxyPort: 65536 }, { proxyPort: 1.5 },
    { username: "" }, { username: "é".repeat(128) }, { password: "\ud800" }, { password: "secret\n" }]) {
    assert.throws(() => core.decodeConnectionCode(connectionCode(codePayload(overrides))));
  }
  const duplicate = `{"name":"discarded",${JSON.stringify(payload).slice(1)}`;
  assert.throws(() => core.decodeConnectionCode(encodeCode(duplicate)));
});

test("connection code size is bounded after trimming and rejection errors never expose credentials", () => {
  const payload = codePayload({ password: "DO-NOT-ECHO-THIS-SECRET" });
  let json = JSON.stringify(payload);
  while (encodeCode(json + " ").length <= 8192) json += " ";
  assert.ok(encodeCode(json).length >= 8190);
  assert.deepEqual(core.decodeConnectionCode(`\n${encodeCode(json)}\n`), payload);
  for (const invalid of [encodeCode(json + " "), encodeCode('{"password":"DO-NOT-ECHO-THIS-SECRET",broken'),
    connectionCode({ ...payload, unexpected: "DO-NOT-ECHO-THIS-SECRET" })]) {
    assert.throws(() => core.decodeConnectionCode(invalid), error => {
      assert.equal(error.message.includes(payload.password), false);
      assert.equal(error.message.includes(invalid), false);
      return true;
    });
  }
});
