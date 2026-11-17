/* SPDX-License-Identifier: MIT */
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { webcrypto, createPrivateKey, createPublicKey, sign, verify } = require("node:crypto");
const { createStore, STORAGE_KEY, REFERENCE_KEY } = require("../managed-identity.js");
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
function memory(initial) {
  const values = initial ? { [STORAGE_KEY]: structuredClone(initial) } : {};
  return { values, writes: 0, async get(key) { return { [key]: structuredClone(values[key]) }; },
    async set(value) { this.writes++; Object.assign(values, structuredClone(value)); },
    async remove(key) { delete values[key]; } };
}
function countingCrypto() {
  let generations = 0;
  return { get generations() { return generations; },
    getRandomValues: webcrypto.getRandomValues.bind(webcrypto),
    subtle: new Proxy(webcrypto.subtle, { get(target, key) {
      if (key === "generateKey") return (...args) => { generations++; return target.generateKey(...args); };
      return typeof target[key] === "function" ? target[key].bind(target) : target[key];
    } }) };
}
let recordPromise;
function validRecord() {
  if (!recordPromise) recordPromise = (async () => {
    const storage = memory(); await createStore(storage, webcrypto).publicKey({ create: true });
    return storage.values[STORAGE_KEY];
  })();
  return recordPromise.then(structuredClone);
}
function decodeSshKey(publicKey) {
  const bytes = Buffer.from(publicKey.split(" ")[1], "base64");
  let offset = 0;
  const take = () => { const size = bytes.readUInt32BE(offset); offset += 4; const result = bytes.subarray(offset, offset + size); offset += size; return result; };
  const kind = take(), e = take(), n = take();
  assert.equal(offset, bytes.length, "no trailing data in the public-key blob");
  return { kind, e, n };
}

test("real WebCrypto RSA is persisted before concurrent consumers get matching PKCS1/OpenSSH keys", async () => {
  const storage = memory(), crypto = countingCrypto(), writing = deferred(), release = deferred();
  const persist = storage.set.bind(storage);
  storage.set = async value => { writing.resolve(); await release.promise; await persist(value); };
  const store = createStore(storage, crypto);
  let returned = 0;
  const publicResult = store.publicKey({ create: true }).then(value => { returned++; return value; });
  const privateResult = store.resolve({ create: true }).then(value => { returned++; return value; });
  await writing.promise;
  assert.equal(returned, 0); assert.equal(storage.values[STORAGE_KEY], undefined);
  release.resolve();
  const [publicOnly, auth] = await Promise.all([publicResult, privateResult]);
  assert.equal(crypto.generations, 1); assert.equal(storage.writes, 2);
  assert.deepEqual(Object.keys(publicOnly), ["publicKey"]);
  assert.equal(auth.publicKey, publicOnly.publicKey); assert.equal(auth.passphrase, "");
  const privateKey = createPrivateKey({ key: auth.privateKey, format: "pem", type: "pkcs1" });
  assert.ok(privateKey.asymmetricKeyDetails.modulusLength >= 3072);
  const jwk = createPublicKey(privateKey).export({ format: "jwk" });
  const decoded = decodeSshKey(auth.publicKey);
  assert.equal(decoded.kind.toString(), "ssh-rsa");
  assert.deepEqual(decoded.e, Buffer.from([1, 0, 1]));
  assert.equal(decoded.n[0], 0, "positive SSH mpint must sign-pad the RSA modulus");
  assert.deepEqual(decoded.n.subarray(1), Buffer.from(jwk.n, "base64url"));
  const payload = Buffer.from("genuine managed identity serialization test");
  assert.equal(verify("sha256", payload, createPublicKey(privateKey), sign("sha256", payload, privateKey)), true);
  assert.equal((await createStore(storage, webcrypto).resolve()).privateKey, auth.privateKey, "restart retains the exact identity");
});

test("corrupt, mismatched, weak, or disappearing stored keys fail closed without replacement", async () => {
  const record = await validRecord();
  const corruptions = [null, { ...record, schemaVersion: 2 }, { ...record, jwk: { ...record.jwk, kty: "EC" } },
    { ...record, jwk: { ...record.jwk, n: "AQAB" } },
    { ...record, jwk: { ...record.jwk, d: record.jwk.d + "=" } },
    { ...record, jwk: { ...record.jwk, qi: "AQAB" } }];
  for (const broken of corruptions) {
    const storage = memory(); storage.values[STORAGE_KEY] = broken;
    const crypto = countingCrypto(), store = createStore(storage, crypto);
    await assert.rejects(store.publicKey({ create: true }), /invalid or unreadable/);
    assert.equal(crypto.generations, 0); assert.equal(storage.writes, 0);
    assert.deepEqual(storage.values[STORAGE_KEY], broken);
  }
  const storage = memory(record), crypto = countingCrypto(), store = createStore(storage, crypto);
  await store.resolve(); delete storage.values[STORAGE_KEY];
  await assert.rejects(store.publicKey({ create: true }), /No replacement/);
  assert.equal(crypto.generations, 0);
  assert.ok(storage.values[REFERENCE_KEY], "the public no-rotation reference survives a missing private record");
  await assert.rejects(createStore(storage, crypto).publicKey({ create: true }), /No replacement/,
    "a fresh background must not rotate a manually copied key even without saved nodes");
  assert.equal(crypto.generations, 0);
});

test("storage failures disclose no unpersisted key and do not silently generate another", async () => {
  const crypto = countingCrypto(), storage = memory(), store = createStore(storage, crypto);
  storage.set = async () => { throw new Error("storage full"); };
  await assert.rejects(store.publicKey({ create: true }), /persist/);
  await assert.rejects(store.resolve({ create: true }), /No replacement/);
  assert.equal(crypto.generations, 1);
  const unreadable = memory(); unreadable.get = async () => { throw new Error("private disk error"); };
  await assert.rejects(createStore(unreadable, crypto).publicKey({ create: true }), /Cannot read/);
  assert.equal(crypto.generations, 1);
});

test("the public reference exposes no private fields and failure to persist it withholds all key use", async () => {
  const privateStorage = memory(await validRecord()), publicStorage = memory();
  publicStorage.set = async () => { throw new Error("quota"); };
  const crypto = countingCrypto(), store = createStore(privateStorage, crypto, publicStorage);
  await assert.rejects(store.publicKey({ create: true }), /persist.*reference/);
  assert.equal(crypto.generations, 0);
  publicStorage.set = async value => { Object.assign(publicStorage.values, structuredClone(value)); };
  const publicOnly = await store.publicKey({ create: true });
  assert.match(publicOnly.publicKey, /^ssh-rsa /);
  assert.deepEqual(Object.keys(publicStorage.values[REFERENCE_KEY]), ["schemaVersion", "n"]);
  assert.doesNotMatch(JSON.stringify(publicStorage.values), /"(?:d|p|q|dp|dq|qi)":|PRIVATE KEY/);
  publicStorage.values[REFERENCE_KEY].n = "A".repeat(512);
  await assert.rejects(createStore(privateStorage, crypto, publicStorage).publicKey({ create: true }), /changed unexpectedly/);
  assert.equal(crypto.generations, 0);
});

test("reset is explicit, serialized, checks deletion, and reports partial storage failure truthfully", async () => {
  const storage = memory(await validRecord()), store = createStore(storage, webcrypto);
  const before = await store.publicKey();
  storage.remove = async () => {};
  await assert.rejects(store.destroy(), /could not be deleted/);
  assert.deepEqual(await store.publicKey(), before);
  storage.remove = async key => { delete storage.values[key]; };
  await store.destroy(); assert.equal(storage.values[STORAGE_KEY], undefined);
  await assert.rejects(store.resolve(), /No replacement/);
  const after = await store.publicKey({ create: true });
  assert.notEqual(after.publicKey, before.publicKey);
});
