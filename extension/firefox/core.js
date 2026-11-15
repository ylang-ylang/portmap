/* SPDX-License-Identifier: MIT */
"use strict";

(function (root) {
  const CONTROL = /[\u0000-\u001f\u007f]/;
  const LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
  const SCHEMES = new Set(["http:", "https:", "ws:", "wss:"]);
  const CONNECTION_CODE_PREFIX = "portmap-connect-v1:";
  const CONNECTION_CODE_FIELDS = ["name", "domain", "proxyHost", "proxyPort", "username", "password"];


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

  function normalizeProxyHost(value) {
    if (typeof value !== "string") throw new Error("Proxy host is required.");
    const host = value.trim().toLowerCase();
    if (host.startsWith("[")) {
      try {
        const url = new URL(`http://${host}/`);
        if (url.port || !host.endsWith("]") || !url.hostname.startsWith("[")) throw new Error();
        return url.hostname;
      } catch (_) {
        throw new Error("IPv6 proxy addresses must be valid and enclosed in brackets.");
      }
    }
    const name = dnsName(host, "Proxy host");
    if (isPortmapHost(name)) throw new Error("Proxy host must be independently reachable, not a .portmap name.");
    return name;
  }

  function requiredText(value, field, max) {
    if (typeof value !== "string" || !value.length || value.length > max || CONTROL.test(value)) {
      throw new Error(`${field} is required and must not contain control characters.`);
    }
    return value;
  }

  function credential(value, field) {
    requiredText(value, field, 255);
    const bytes = new TextEncoder().encode(value);
    if (bytes.length > 255 || new TextDecoder().decode(bytes) !== value) {
      throw new Error(`${field} must be valid UTF-8 text of 1 to 255 bytes.`);
    }
    return value;
  }

  function normalizeConnection(input) {
    if (!input || typeof input !== "object" || Array.isArray(input)) throw new Error("Connection must be an object.");
    const id = requiredText(input.id, "Connection ID", 128);
    if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new Error("Invalid connection ID.");
    const name = requiredText(input.name, "Name", 120).trim();
    if (!name) throw new Error("Name is required.");
    if (!Number.isInteger(input.proxyPort) || input.proxyPort < 1 || input.proxyPort > 65535) {
      throw new Error("Proxy port must be an integer from 1 to 65535.");
    }
    const username = credential(input.username, "Username");
    const password = credential(input.password, "Password");
    if (typeof input.enabled !== "boolean") throw new Error("Enabled must be true or false.");
    return Object.freeze({ id, name, domain: normalizeDomain(input.domain),
      proxyHost: normalizeProxyHost(input.proxyHost), proxyPort: input.proxyPort,
      username, password, enabled: input.enabled });
  }

  function decodeConnectionCode(value) {
    try {
      if (typeof value !== "string") throw new Error();
      const code = value.trim();
      if (code.length > 8192 || !code.startsWith(CONNECTION_CODE_PREFIX)) throw new Error();
      const encoded = code.slice(CONNECTION_CODE_PREFIX.length);
      if (!/^[A-Za-z0-9_-]+$/.test(encoded) || encoded.length % 4 === 1) throw new Error();
      const binary = atob(encoded.replace(/-/g, "+").replace(/_/g, "/"));
      // Reject noncanonical unused bits, padding and alternate base64 alphabets.
      if (btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "") !== encoded) throw new Error();
      const bytes = Uint8Array.from(binary, character => character.charCodeAt(0));
      const json = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes);
      const input = JSON.parse(json);
      if (!input || typeof input !== "object" || Array.isArray(input) ||
          Object.keys(input).length !== CONNECTION_CODE_FIELDS.length ||
          !CONNECTION_CODE_FIELDS.every(field => Object.hasOwn(input, field))) throw new Error();
      const normalized = normalizeConnection({ ...input, id: "import", enabled: true });
      // These fields are scalars. Count members outside strings so duplicate
      // keys cannot be silently discarded by JSON.parse.
      let quoted = false;
      let escaped = false;
      let members = 0;
      for (const character of json) {
        if (quoted) {
          if (escaped) escaped = false;
          else if (character === "\\") escaped = true;
          else if (character === "\"") quoted = false;
        } else if (character === "\"") quoted = true;
        else if (character === ":") members++;
      }
      if (members !== CONNECTION_CODE_FIELDS.length) throw new Error();
      return Object.freeze(Object.fromEntries(CONNECTION_CODE_FIELDS.map(field => [field, normalized[field]])));
    } catch (_) {
      // JSON/encoding exceptions can include fragments of the credential payload.
      throw new Error("Invalid connection code. Paste a complete portmap-connect-v1 code from your node (at most 8192 characters).");
    }
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
      return { host, portmap: isPortmapHost(host), supported: SCHEMES.has(url.protocol) };
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
    if (stored === undefined) return { schemaVersion: 2, revision: "empty", connections: [] };
    if (!stored || stored.schemaVersion !== 2 || typeof stored.revision !== "string") {
      throw new Error("Unsupported connection storage version. Routing is blocked; restore a compatible extension or reset connections.");
    }
    return { schemaVersion: 2, revision: stored.revision, connections: validateConnections(stored.connections) };
  }

  function compileConnections(connections, isolationKey) {
    return connections.filter(entry => entry.enabled).map(entry => ({
      connection: entry,
      proxy: Object.freeze({ type: "socks", host: entry.proxyHost, port: entry.proxyPort,
        proxyDNS: true, username: entry.username, password: entry.password,
        connectionIsolationKey: `${isolationKey}:${entry.id}`, failoverTimeout: 10 })
    }));
  }

  const api = Object.freeze({ normalizeConnection, normalizeDomain, normalizeProxyHost, decodeConnectionCode,
    matchesDomain, isPortmapHost, inspectUrl, validateConnections,
    readConfig, compileConnections });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PortmapCore = api;
})(globalThis);
