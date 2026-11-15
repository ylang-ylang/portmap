/* SPDX-License-Identifier: MIT */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../core.js");

const connection = overrides => ({ id: "node-a", name: "Node A", domain: "a.portmap", proxyHost: "100.64.0.1",
  proxyPort: 8443, username: "user", password: "secret", enabled: true, ...overrides });

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

const addonId = "firefox@portmap.ylang-ylang.github.io";
const release = version => ({
  version,
  update_link: `https://github.com/ylang-ylang/portmap/releases/download/v${version}/portmap-firefox-${version}.xpi`,
  update_hash: "sha256:" + "a".repeat(64)
});
const updateFeed = updates => ({ addons: { [addonId]: { updates } } });

test("release checks choose the newest numeric version without downgrades or reinstall offers", () => {
  const feed = updateFeed([release("0.9.9"), release("0.10.0"), release("0.9.10")]);
  assert.equal(core.findReleaseUpdate(feed, addonId, "0.9.9").version, "0.10.0");
  assert.equal(core.findReleaseUpdate(feed, addonId, "0.10.0"), null);
  assert.equal(core.findReleaseUpdate(feed, addonId, "1.0.0"), null);
  assert.equal(core.findReleaseUpdate(updateFeed([]), addonId, "0.10.0"), null);
  assert.equal(core.findReleaseUpdate(updateFeed([release("0.99.99"), release("1.0.0")]), addonId, "0.10.0").version, "1.0.0");
});

test("release checks reject foreign identities, malformed metadata and untrusted download targets", () => {
  for (const feed of [null, {}, updateFeed(null), { addons: { other: { updates: [release("1.0.0")] } } }]) {
    assert.throws(() => core.findReleaseUpdate(feed, addonId, "0.1.0"));
  }
  for (const invalid of [
    { ...release("1.0.0"), update_link: "https://evil.example/update.xpi" },
    { ...release("1.0.0"), update_link: release("2.0.0").update_link },
    { ...release("1.0.0"), update_link: "javascript:alert(1)" },
    { ...release("1.0.0"), update_hash: "sha256:truncated" },
    { ...release("1.0.0"), version: "1.0.0-beta" },
    { ...release("1.0.0"), version: "999999999999999999999.0.0" },
    { ...release("1.0.0"), version: 1 }, null
  ]) assert.throws(() => core.findReleaseUpdate(updateFeed([invalid]), addonId, "0.1.0"));
});
