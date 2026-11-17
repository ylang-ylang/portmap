/* SPDX-License-Identifier: MIT */
"use strict";
(function (root) {
  const STORAGE_KEY = "managedSshIdentity";
  const REFERENCE_KEY = "managedSshIdentityReference";
  const FIELDS = ["n", "e", "d", "p", "q", "dp", "dq", "qi"];
  const ALGORITHM = { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" };
  const concat = (...parts) => {
    const bytes = new Uint8Array(parts.reduce((size, part) => size + part.length, 0));
    let offset = 0;
    for (const part of parts) { bytes.set(part, offset); offset += part.length; }
    return bytes;
  };
  function base64(bytes) {
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return btoa(binary);
  }
  function decode(value) {
    if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value) || value.length > 2048) {
      throw new Error("Invalid RSA key material.");
    }
    const binary = atob(value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - value.length % 4) % 4));
    const bytes = Uint8Array.from(binary, byte => byte.charCodeAt(0));
    if (!bytes.length || !bytes[0] || base64(bytes).replace(/=/g, "").replace(/\+/g, "-").replace(/\//g, "_") !== value) {
      throw new Error("Invalid RSA key material.");
    }
    return bytes;
  }
  function length(size) {
    if (size < 128) return Uint8Array.of(size);
    const bytes = [];
    while (size) { bytes.unshift(size & 255); size = Math.floor(size / 256); }
    return Uint8Array.of(128 | bytes.length, ...bytes);
  }
  const der = (tag, bytes) => concat(Uint8Array.of(tag), length(bytes.length), bytes);
  const positive = bytes => bytes[0] & 128 ? concat(Uint8Array.of(0), bytes) : bytes;
  function sshString(bytes) {
    const prefix = new Uint8Array(4);
    new DataView(prefix.buffer).setUint32(0, bytes.length);
    return concat(prefix, bytes);
  }
  function serialize(jwk) {
    const values = Object.fromEntries(FIELDS.map(field => [field, decode(jwk[field])]));
    const bits = values.n.length * 8 - Math.clz32(values.n[0]) + 24;
    if (jwk.kty !== "RSA" || bits < 3072 || bits > 8192 || jwk.e !== "AQAB") throw new Error("Unsupported browser RSA key.");
    // Check the CRT values too: an import/sign implementation may repair or
    // ignore a broken coefficient, but libssh2 receives these exact PEM bytes.
    const numbers = Object.fromEntries(FIELDS.map(field => [field,
      BigInt("0x" + Array.from(values[field], byte => byte.toString(16).padStart(2, "0")).join(""))]));
    const { n, e, d, p, q, dp, dq, qi } = numbers;
    if (p <= 2n || q <= 2n || p === q || p * q !== n || d >= n ||
        dp !== d % (p - 1n) || dq !== d % (q - 1n) || qi >= p || qi * q % p !== 1n) {
      throw new Error("Inconsistent RSA private key.");
    }
    let a = p - 1n, b = q - 1n;
    while (b) { const remainder = a % b; a = b; b = remainder; }
    if (e * d % ((p - 1n) / a * (q - 1n)) !== 1n) throw new Error("Inconsistent RSA private key.");
    const sequence = der(48, concat(der(2, Uint8Array.of(0)),
      ...FIELDS.map(field => der(2, positive(values[field])))));
    const pem = base64(sequence).match(/.{1,64}/g).join("\n");
    const publicKey = "ssh-rsa " + base64(concat(sshString(new TextEncoder().encode("ssh-rsa")),
      sshString(positive(values.e)), sshString(positive(values.n))));
    return Object.freeze({ type: "key", privateKey: `-----BEGIN RSA PRIVATE KEY-----\n${pem}\n-----END RSA PRIVATE KEY-----`,
      publicKey, passphrase: "" });
  }
  function backgroundStorage() {
    let opening = null;
    function database() {
      if (!opening) opening = new Promise((resolve, reject) => {
        const request = root.indexedDB.open("portmap-managed-ssh", 1);
        let abandoned = false;
        request.onupgradeneeded = () => { request.result.createObjectStore("identity"); };
        request.onerror = () => { opening = null; reject(new Error("Browser key database unavailable.")); };
        request.onblocked = () => {
          abandoned = true;
          opening = null;
          reject(new Error("Close other Portmap background contexts and retry."));
        };
        request.onsuccess = () => {
          const db = request.result;
          if (abandoned) { db.close(); return; }
          db.onversionchange = () => { db.close(); opening = null; };
          resolve(db);
        };
      });
      const pending = opening;
      return pending.catch(failure => { if (opening === pending) opening = null; throw failure; });
    }
    async function transaction(mode, operation) {
      const db = await database();
      return new Promise((resolve, reject) => {
        let tx, value;
        try {
          tx = mode === "readwrite" ? db.transaction("identity", mode, { durability: "strict" })
            : db.transaction("identity", mode);
          tx.oncomplete = () => resolve(value);
          tx.onabort = tx.onerror = () => reject(new Error("Browser key database transaction failed."));
          operation(tx.objectStore("identity"), result => { value = result; });
        } catch (failure) {
          opening = null;
          try { tx?.abort(); } catch (_) {}
          reject(failure);
        }
      });
    }
    // Unlike storage.local, IndexedDB does not broadcast private key bytes to
    // the options page through storage.onChanged. This adapter is instantiated
    // only by the persistent background and never exposed through messages.
    return Object.freeze({
      get: key => transaction("readonly", (store, done) => {
        const request = store.get(key);
        request.onsuccess = () => done({ [key]: request.result });
      }),
      set: values => transaction("readwrite", store => {
        for (const [key, value] of Object.entries(values)) store.put(value, key);
      }),
      remove: key => transaction("readwrite", store => { store.delete(key); })
    });
  }
  function createStore(storage = backgroundStorage(), cryptoApi = root.crypto,
      references = root.browser?.storage?.local || storage) {
    let queue = Promise.resolve();
    let failedCreation = false;
    let observedIdentity = false;
    let observedModulus = null;
    const enqueue = operation => {
      const task = queue.then(operation);
      queue = task.catch(() => {});
      return task;
    };
    async function readRecord() {
      try { return (await storage.get(STORAGE_KEY))[STORAGE_KEY]; }
      catch (_) { throw new Error("Cannot read the browser SSH key. Check local storage; no key was replaced."); }
    }
    async function readReference() {
      let reference;
      try { reference = (await references.get(REFERENCE_KEY))[REFERENCE_KEY]; }
      catch (_) { throw new Error("Cannot read the browser SSH key reference. No key was replaced."); }
      if (reference !== undefined && (!reference || reference.schemaVersion !== 1 ||
          typeof reference.n !== "string" || !/^[A-Za-z0-9_-]{512,1366}$/.test(reference.n))) {
        throw new Error("The browser SSH key reference is invalid. Restore this profile or explicitly reset local data.");
      }
      return reference;
    }
    async function persistReference(n) {
      try {
        await references.set({ [REFERENCE_KEY]: { schemaVersion: 1, n } });
        if ((await readReference())?.n !== n) throw new Error();
      } catch (_) { throw new Error("Could not persist the browser SSH key reference. No replacement key was used."); }
    }
    async function validate(record) {
      try {
        if (!record || record.schemaVersion !== 1 || !record.jwk || typeof record.jwk !== "object" ||
            Array.isArray(record.jwk) || record.jwk.kty !== "RSA") throw new Error();
        const jwk = { kty: "RSA", ...Object.fromEntries(FIELDS.map(field => [field, record.jwk[field]])) };
        const auth = serialize(jwk);
        const privateKey = await cryptoApi.subtle.importKey("jwk", jwk, ALGORITHM, true, ["sign"]);
        const exported = await cryptoApi.subtle.exportKey("jwk", privateKey);
        if (FIELDS.some(field => jwk[field] !== exported[field])) throw new Error();
        const publicKey = await cryptoApi.subtle.importKey("jwk", { kty: "RSA", n: jwk.n, e: jwk.e }, ALGORITHM, false, ["verify"]);
        const challenge = cryptoApi.getRandomValues(new Uint8Array(32));
        const signature = await cryptoApi.subtle.sign(ALGORITHM, privateKey, challenge);
        if (!await cryptoApi.subtle.verify(ALGORITHM, publicKey, signature, challenge)) throw new Error();
        return auth;
      } catch (_) {
        throw new Error("The browser SSH key is invalid or unreadable. It was not replaced. Restore this profile's key or explicitly reset local data.");
      }
    }
    async function resolveInternal(create) {
      let record = await readRecord();
      const reference = await readReference();
      if (record !== undefined) {
        observedIdentity = true;
        const auth = await validate(record);
        if ((observedModulus && record.jwk.n !== observedModulus) || (reference && reference.n !== record.jwk.n)) {
          throw new Error("The browser SSH key changed unexpectedly. It was not used. Restore this profile's key or explicitly reset local data.");
        }
        if (!reference) await persistReference(record.jwk.n);
        observedModulus = record.jwk.n;
        return auth;
      }
      if (!create || failedCreation || observedIdentity || reference) throw new Error("The browser SSH key is missing. No replacement was generated. Restore this profile's key or explicitly reset local data.");
      try {
        const keys = await cryptoApi.subtle.generateKey({ ...ALGORITHM, modulusLength: 3072,
          publicExponent: Uint8Array.of(1, 0, 1) }, true, ["sign", "verify"]);
        const exported = await cryptoApi.subtle.exportKey("jwk", keys.privateKey);
        const jwk = { kty: "RSA", ...Object.fromEntries(FIELDS.map(field => [field, exported[field]])) };
        record = { schemaVersion: 1, jwk };
        const auth = await validate(record);
        await storage.set({ [STORAGE_KEY]: record });
        const saved = await readRecord();
        if (!saved || saved.schemaVersion !== 1 || saved.jwk?.kty !== "RSA" ||
            FIELDS.some(field => saved.jwk?.[field] !== jwk[field])) throw new Error();
        observedIdentity = true;
        observedModulus = jwk.n;
        await persistReference(jwk.n);
        return auth;
      } catch (_) {
        failedCreation = true;
        throw new Error("Could not create and persist the browser SSH key. Nothing may use a new key until storage succeeds. Retry after checking storage, or explicitly reset local data.");
      }
    }
    return Object.freeze({
      resolve: ({ create = false } = {}) => enqueue(() => resolveInternal(create)),
      publicKey: ({ create = false } = {}) => enqueue(async () => ({ publicKey: (await resolveInternal(create)).publicKey })),
      destroy: () => enqueue(async () => {
        try {
          await storage.remove(STORAGE_KEY);
          if (await readRecord() !== undefined) throw new Error();
          await references.remove(REFERENCE_KEY);
          if (await readReference() !== undefined) throw new Error();
          failedCreation = false;
          observedIdentity = false;
          observedModulus = null;
        } catch (_) { throw new Error("The browser SSH key could not be deleted. Retry Reset local data; do not assume the key is gone."); }
      })
    });
  }
  const api = Object.freeze({ createStore, STORAGE_KEY, REFERENCE_KEY });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PortmapManagedIdentity = api;
})(globalThis);
