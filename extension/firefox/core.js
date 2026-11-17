/* SPDX-License-Identifier: MIT */
"use strict";

(function (root) {
  const CONTROL = /[\u0000-\u001f\u007f]/;
  const LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
  const SCHEMES = new Set(["http:", "https:", "ws:", "wss:"]);

  function dnsName(value, field) {
    if (typeof value !== "string") throw new Error(`${field} must be a hostname.`);
    const name = value.trim().toLowerCase().replace(/\.$/, "");
    if (!name || name.length > 253 || !name.split(".").every(label => LABEL.test(label))) {
      throw new Error(`${field} must contain valid ASCII DNS labels (use punycode for international names).`);
    }
    return name;
  }

  function normalizeDomain(value) {
    const domain = dnsName(value, "Domain");
    if (!domain.endsWith(".portmap")) throw new Error("Domain must be a proper subdomain of portmap, such as node.portmap.");
    return domain;
  }

  function normalizeHost(value) {
    if (typeof value !== "string") throw new Error("Host is required.");
    const host = value.trim().toLowerCase().replace(/\.$/, "");
    if (host.includes(":")) {
      if (!/^(?:\[[0-9a-f:.]+\]|[0-9a-f:.]+)$/.test(host)) throw new Error("Host must be a valid IPv6 address.");
      try {
        const url = new URL(`http://${host.startsWith("[") ? host : `[${host}]`}/`);
        if (url.port || !url.hostname.startsWith("[")) throw new Error();
        return url.hostname.slice(1, -1);
      } catch (_) { throw new Error("Host must be a valid IPv6 address or hostname."); }
    }
    const name = dnsName(host, "Host");
    if (isPortmapHost(name)) throw new Error("Host must be independently reachable, not a .portmap name.");
    if (/^[0-9.]+$/.test(name) && (name.split(".").length !== 4 ||
        name.split(".").some(part => !/^(0|[1-9][0-9]{0,2})$/.test(part) || Number(part) > 255))) {
      throw new Error("Host must be a valid IPv4 address.");
    }
    return name;
  }

  function requiredText(value, field, max) {
    if (typeof value !== "string" || !value.length || value.length > max || CONTROL.test(value)) {
      throw new Error(`${field} is required and must not contain control characters.`);
    }
    return value;
  }

  function credential(value, field, max = 65536, multiline = false, empty = false) {
    if (typeof value !== "string" || (!empty && !value.length) || value.length > max ||
        (multiline ? /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/ : CONTROL).test(value) ||
        new TextDecoder().decode(new TextEncoder().encode(value)) !== value) {
      throw new Error(`${field} must contain valid text without unsupported control characters.`);
    }
    return value;
  }

  function normalizePort(value, field) {
    if (!Number.isInteger(value) || value < 1 || value > 65535) throw new Error(`${field} must be an integer from 1 to 65535.`);
    return value;
  }

  function normalizeFingerprint(value) {
    if (typeof value !== "string") throw new Error("An exact SSH SHA256 host-key fingerprint is required.");
    const pin = value.trim();
    if (/^[a-f0-9]{64}$/i.test(pin)) return pin.toLowerCase();
    if (/^SHA256:[A-Za-z0-9+/]{43}=?$/.test(pin)) {
      const bytes = atob(pin.slice(7).replace(/=$/, "") + "=");
      if (bytes.length === 32 && btoa(bytes).replace(/=$/, "") === pin.slice(7).replace(/=$/, "")) {
        return Array.from(bytes, byte => byte.charCodeAt(0).toString(16).padStart(2, "0")).join("");
      }
    }
    throw new Error("Use a verified 64-digit hex or OpenSSH SHA256 fingerprint; accepting any host key is not supported.");
  }

  function normalizeConnection(input) {
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("Connection must be an object.");
    const id = requiredText(input.id, "Connection ID", 128);
    if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error("Invalid connection ID.");
    const name = requiredText(input.name, "Name", 120).trim();
    if (!name) throw new Error("Name is required.");
    if (typeof input.enabled !== "boolean") throw new Error("Enabled must be true or false.");
    if (!["direct", "ssh"].includes(input.mode)) throw new Error("Choose direct or SSH transport.");
    const result = { id, name, domain: normalizeDomain(input.domain), enabled: input.enabled,
      mode: input.mode, host: normalizeHost(input.host), httpPort: normalizePort(input.httpPort, "HTTP port"),
      httpsPort: input.httpsPort == null ? null : normalizePort(input.httpsPort, "HTTPS port") };
    if (input.mode === "ssh") {
      const ssh = input.ssh;
      if (!ssh || typeof ssh !== "object") throw new Error("SSH settings are required.");
      let auth;
      if (ssh.auth?.type === "password") {
        auth = { type: "password", password: credential(ssh.auth.password, "SSH password", 4096) };
      } else if (ssh.auth?.type === "key") {
        const publicKey = credential(typeof ssh.auth.publicKey === "string" ? ssh.auth.publicKey.trim() : ssh.auth.publicKey,
          "SSH public key", 16384);
        const privateKey = credential(ssh.auth.privateKey, "SSH private key", 65536, true).trim();
        if (!/^ssh-rsa [A-Za-z0-9+/]+={0,2}(?: [^\r\n]*)?$/.test(publicKey) ||
            !/^-----BEGIN RSA PRIVATE KEY-----\r?\n[\s\S]+\r?\n-----END RSA PRIVATE KEY-----$/.test(privateKey)) {
          throw new Error("SSH key authentication requires an OpenSSH ssh-rsa public key and RSA PEM private key.");
        }
        auth = { type: "key", publicKey, privateKey,
          passphrase: credential(ssh.auth.passphrase ?? "", "Key passphrase", 4096, false, true) };
      } else throw new Error("Choose password or RSA PEM key authentication.");
      result.ssh = Object.freeze({ port: normalizePort(ssh.port ?? 22, "SSH port"),
        username: credential(ssh.username, "SSH username", 255), targetHost: normalizeHost(ssh.targetHost || "127.0.0.1"),
        hostKeySha256Hex: normalizeFingerprint(ssh.hostKeySha256Hex), auth: Object.freeze(auth) });
    }
    return Object.freeze(result);
  }

  function matchesDomain(host, domain) {
    return host === domain || host.endsWith(`.${domain}`);
  }

  function isPortmapHost(host) {
    return host === "portmap" || host.endsWith(".portmap");
  }

  function inspectUrl(value) {
    try {
      const url = new URL(value);
      const host = url.hostname.toLowerCase().replace(/\.+$/, "");
      const portAllowed = !url.port || url.port === (["https:", "wss:"].includes(url.protocol) ? "443" : "80");
      return { host, portmap: isPortmapHost(host), supported: SCHEMES.has(url.protocol) && portAllowed && !url.username && !url.password };
    } catch (_) {
      return { host: "", portmap: false, supported: false };
    }
  }

  function validateConnections(inputs) {
    if (!Array.isArray(inputs) || inputs.length > 256) throw new Error("Connections must be a list of at most 256 entries.");
    const connections = inputs.map(normalizeConnection);
    const ids = new Set();
    for (let i = 0; i < connections.length; i++) {
      const entry = connections[i];
      if (ids.has(entry.id)) throw new Error("Connection IDs must be unique.");
      ids.add(entry.id);
      // Even disabled claims must be unambiguous when enabled later.
      for (let j = 0; j < i; j++) {
        const previous = connections[j];
        if (matchesDomain(entry.domain, previous.domain) || matchesDomain(previous.domain, entry.domain)) {
          throw new Error("Connection domains overlap. Use separate, non-overlapping .portmap subdomains.");
        }
      }
    }
    return Object.freeze(connections);
  }

  function readConfig(stored) {
    if (stored === undefined) return { schemaVersion: 3, revision: "empty", connections: [] };
    if (!stored || stored.schemaVersion !== 3 || typeof stored.revision !== "string") {
      throw new Error("Incompatible connection storage. Old SOCKS settings are preserved and cannot be reinterpreted as direct/SSH settings. Reset connections explicitly to rebuild them.");
    }
    return { schemaVersion: 3, revision: stored.revision, connections: validateConnections(stored.connections) };
  }

  function findReleaseUpdate(feed, addonId, installedVersion) {
    const parseVersion = value => {
      if (typeof value !== "string" || !/^\d+\.\d+\.\d+$/.test(value)) throw new Error("Invalid release version.");
      const parts = value.split(".").map(Number);
      if (!parts.every(Number.isSafeInteger)) throw new Error("Invalid release version.");
      return parts;
    };
    const updates = feed?.addons?.[addonId]?.updates;
    if (!Array.isArray(updates)) throw new Error("Invalid extension update feed.");
    let highest = parseVersion(installedVersion);
    let selected = null;
    for (const entry of updates) {
      const parts = parseVersion(entry?.version);
      const url = `https://github.com/ylang-ylang/portmap/releases/download/v${entry.version}/portmap-firefox-experimental-${entry.version}.xpi`;
      if (entry.distribution !== "experimental-capability-only" || entry.signature_required !== false ||
          entry.capability_required !== true || entry.update_link !== url || typeof entry.update_hash !== "string" ||
          !/^sha256:[a-f0-9]{64}$/i.test(entry.update_hash)) {
        throw new Error("Invalid official release download.");
      }
      for (let index = 0; index < 3; index++) {
        if (parts[index] < highest[index]) break;
        if (parts[index] > highest[index]) {
          selected = Object.freeze({ version: entry.version, url });
          highest = parts;
          break;
        }
      }
    }
    return selected;
  }

  const api = Object.freeze({ normalizeConnection, normalizeDomain, normalizeHost, normalizeFingerprint,
    matchesDomain, isPortmapHost, inspectUrl, validateConnections, readConfig, findReleaseUpdate });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PortmapCore = api;
})(globalThis);
