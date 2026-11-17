/* SPDX-License-Identifier: MIT */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../core.js");

const connection = overrides => ({ id: "node-a", name: "Node A", domain: "a.portmap",
  mode: "direct", host: "192.0.2.10", httpPort: 8080, httpsPort: null, enabled: true, ...overrides });
const ssh = overrides => ({ port: 22, username: "user", targetHost: "127.0.0.1",
  hostKeySha256Hex: "ab".repeat(32), auth: { type: "password", password: "secret" }, ...overrides });

test("namespace claims normalize and match only whole labels", () => {
  const profile = core.normalizeConnection(connection({ domain: " A.Portmap. " }));
  assert.equal(profile.domain, "a.portmap");
  for (const host of ["a.portmap", "x.a.portmap"]) assert.ok(core.matchesDomain(host, profile.domain));
  for (const host of ["nota.portmap", "a.portmap.evil", "portmap"]) assert.equal(core.matchesDomain(host, profile.domain), false);
});

test("physical hosts normalize IPv4 IPv6 DNS but never accept virtual names or URL syntax", () => {
  for (const [input, output] of [["192.0.2.1", "192.0.2.1"], ["[2001:db8::1]", "2001:db8::1"],
    ["2001:0db8::1", "2001:db8::1"], [" Example.TEST. ", "example.test"]]) assert.equal(core.normalizeHost(input), output);
  for (const host of ["a.portmap", "portmap", "256.0.0.1", "127.1", "http://host", "[::1]/path",
    "[::1]:80", "host/path", "user@host", "host:80", "host\\nname"]) assert.throws(() => core.normalizeHost(host));
});

test("virtual requests allow only HTTP WS HTTPS WSS on logical 80 and 443, without userinfo", () => {
  assert.deepEqual(core.inspectUrl("https://APP.A.PORTMAP.:443/a%2Fb?q=a%20b#fragment"),
    { host: "app.a.portmap", portmap: true, supported: true });
  for (const url of ["http://a.portmap:3000/", "https://a.portmap:80/", "ftp://a.portmap/", "http://user:pass@a.portmap/"]) {
    assert.equal(core.inspectUrl(url).portmap, true);
    assert.equal(core.inspectUrl(url).supported, false);
  }
  for (const url of ["ws://a.portmap:80/", "wss://a.portmap:443/"]) assert.equal(core.inspectUrl(url).supported, true);
  for (const url of ["http://a.portmap.evil/", "http://a.portmap@evil/", "not a URL"]) assert.equal(core.inspectUrl(url).portmap, false);
});

test("direct and SSH schema are exact normalized independent records, no old proxy fields survive", () => {
  const direct = core.normalizeConnection(connection({ proxyPort: 9999, password: "obsolete" }));
  assert.deepEqual(Object.keys(direct).sort(), ["id", "name", "domain", "enabled", "mode", "host", "httpPort", "httpsPort"].sort());
  const secure = core.normalizeConnection(connection({ mode: "ssh", ssh: ssh({ port: undefined, targetHost: undefined }) }));
  assert.equal(secure.ssh.port, 22);
  assert.equal(secure.ssh.targetHost, "127.0.0.1");
  assert.equal(secure.ssh.auth.password, "secret");
  assert.ok(Object.isFrozen(secure.ssh.auth));
});

test("invalid and overlapping configuration rejects disabled claims too", () => {
  for (const overrides of [{ domain: "portmap" }, { domain: "-x.portmap" }, { host: "a.portmap" },
    { httpPort: "8080" }, { httpPort: 0 }, { httpsPort: 65536 }, { mode: "socks" }, { enabled: 1 },
    { mode: "ssh" }, { id: "has space" }]) assert.throws(() => core.normalizeConnection(connection(overrides)));
  for (const domain of ["a.portmap", "child.a.portmap"]) assert.throws(() => core.validateConnections([
    connection(), connection({ id: "b", domain, enabled: false })]));
  assert.throws(() => core.validateConnections(Array.from({ length: 257 }, (_, i) => connection({ id: `n${i}`, domain: `n${i}.portmap` }))));
  assert.equal(core.validateConnections([connection(), connection({ id: "b", domain: "b.portmap" })]).length, 2);
});

test("SSH requires verified pins and truthful RSA PEM authentication inputs", () => {
  const digest = Buffer.alloc(32, 171);
  assert.equal(core.normalizeFingerprint(`SHA256:${digest.toString("base64").replace(/=$/, "")}`), digest.toString("hex"));
  assert.equal(core.normalizeFingerprint("AB".repeat(32)), "ab".repeat(32));
  for (const pin of ["", "*", "SHA256:invalid", "a".repeat(63)]) assert.throws(() => core.normalizeFingerprint(pin));
  for (const override of [{ username: "x\0y" }, { hostKeySha256Hex: "*" },
    { auth: { type: "password", password: "" } }, { auth: { type: "key", publicKey: "ssh-ed25519 AAAA", privateKey: "not PEM" } }]) {
    assert.throws(() => core.normalizeConnection(connection({ mode: "ssh", ssh: ssh(override) })));
  }
  const key = core.normalizeConnection(connection({ mode: "ssh", ssh: ssh({ auth: { type: "key",
    publicKey: "ssh-rsa AAAA user", privateKey: "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----", passphrase: "" } }) }));
  assert.equal(key.ssh.auth.type, "key");
});

test("SSH public key files canonicalize surrounding whitespace but reject embedded controls and multiple keys", () => {
  const publicKey = "ssh-rsa AAAA generated-by-ssh-keygen";
  const privateKey = "-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----";
  const normalizeKey = value => core.normalizeConnection(connection({ mode: "ssh",
    ssh: ssh({ auth: { type: "key", publicKey: value, privateKey, passphrase: "" } }) })).ssh.auth.publicKey;
  for (const value of [publicKey, publicKey + "\n", publicKey + "\r\n", " \t" + publicKey + "\r\n \t"]) {
    assert.equal(normalizeKey(value), publicKey);
  }
  for (const value of [publicKey + "\nssh-rsa BBBB another-key", publicKey + "\r\nssh-rsa BBBB another-key",
    "ssh-rsa AAAA comment\ncontinued", "ssh-rsa AAAA\tcomment", "ssh-rsa AAAA\0comment", "\n \t", null]) {
    assert.throws(() => normalizeKey(value));
  }
});

test("schema2 remains incompatible and untouched; fresh install has no invented endpoints", () => {
  const old = { schemaVersion: 2, revision: "old", connections: [{ proxyHost: "old", proxyPort: 1080 }] };
  const before = structuredClone(old);
  assert.throws(() => core.readConfig(old), /preserved.*Reset/);
  assert.deepEqual(old, before);
  assert.deepEqual(core.readConfig(undefined), { schemaVersion: 3, revision: "empty", connections: [] });
  assert.equal(core.readConfig({ schemaVersion: 3, revision: "new", connections: [connection()] }).connections.length, 1);
});

const addonId = "firefox@portmap.ylang-ylang.github.io";
const release = version => ({
  version,
  distribution: "experimental-capability-only", signature_required: false, capability_required: true,
  update_link: `https://github.com/ylang-ylang/portmap/releases/download/v${version}/portmap-firefox-experimental-${version}.xpi`,
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
    { ...release("1.0.0"), distribution: undefined },
    { ...release("1.0.0"), signature_required: true },
    { ...release("1.0.0"), capability_required: false },
    { ...release("1.0.0"), update_link: `https://github.com/ylang-ylang/portmap/releases/download/v1.0.0/portmap-firefox-1.0.0.xpi` },
    { ...release("1.0.0"), update_link: "https://evil.example/update.xpi" },
    { ...release("1.0.0"), update_link: release("2.0.0").update_link },
    { ...release("1.0.0"), update_link: "javascript:alert(1)" },
    { ...release("1.0.0"), update_hash: "sha256:truncated" },
    { ...release("1.0.0"), version: "1.0.0-beta" },
    { ...release("1.0.0"), version: "999999999999999999999.0.0" },
    { ...release("1.0.0"), version: 1 }, null
  ]) assert.throws(() => core.findReleaseUpdate(updateFeed([invalid]), addonId, "0.1.0"));
});
