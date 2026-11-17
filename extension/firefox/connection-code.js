/* SPDX-License-Identifier: MIT */
"use strict";

(function (root) {
  const core = typeof module !== "undefined" && module.exports ? require("./core.js") : root.PortmapCore;
  const PREFIX = "portmap-connect:v1:";
  const MAX_WIRE_BYTES = 2048;
  const ERROR = "Invalid or unsupported Portmap connection QR code.";
  const COMMON = ["name", "domain", "mode", "host", "httpPort", "httpsPort"];
  const SSH = ["port", "username", "targetHost", "hostKeySha256Hex"];
  const encoder = new TextEncoder();
  const decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });

  function keys(value, expected) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        Object.keys(value).length !== expected.length || !expected.every(key => Object.hasOwn(value, key))) throw new Error(ERROR);
  }
  function text(value, max, empty = false) {
    if (typeof value !== "string" || (!empty && !value.length) || value.length > max ||
        /[\u0000-\u001f\u007f]/.test(value) || decoder.decode(encoder.encode(value)) !== value) throw new Error(ERROR);
    return value;
  }
  function port(value) {
    if (!Number.isInteger(value) || value < 1 || value > 65535) throw new Error(ERROR);
    return value;
  }
  function normalize(value) {
    keys(value, value?.mode === "ssh" ? [...COMMON, "ssh"] : COMMON);
    if (!["direct", "ssh"].includes(value.mode)) throw new Error(ERROR);
    const name = text(value.name, 120).trim();
    if (!name) throw new Error(ERROR);
    const result = { name, domain: core.normalizeDomain(value.domain), mode: value.mode,
      host: core.normalizeHost(value.host), httpPort: port(value.httpPort),
      httpsPort: value.httpsPort === null ? null : port(value.httpsPort) };
    if (value.mode === "ssh") {
      keys(value.ssh, SSH);
      result.ssh = Object.freeze({ port: port(value.ssh.port), username: text(value.ssh.username, 255, true),
        targetHost: core.normalizeHost(value.ssh.targetHost), hostKeySha256Hex: core.normalizeFingerprint(value.ssh.hostKeySha256Hex) });
    }
    return Object.freeze(result);
  }
  function canonical(value) {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(",")}}`;
    }
    return JSON.stringify(value);
  }
  function encoded(profile) {
    const bytes = encoder.encode(canonical(profile));
    const wire = PREFIX + btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    if (wire.length > MAX_WIRE_BYTES) throw new Error(ERROR);
    return wire;
  }
  function encode(profile) {
    try { return encoded(normalize(profile)); } catch (_) { throw new Error(ERROR); }
  }
  function decode(wire) {
    try {
      if (typeof wire !== "string" || wire.length > MAX_WIRE_BYTES || !wire.startsWith(PREFIX)) throw new Error(ERROR);
      const payload = wire.slice(PREFIX.length);
      if (!/^[A-Za-z0-9_-]+$/.test(payload) || payload.length % 4 === 1) throw new Error(ERROR);
      const binary = atob(payload.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - payload.length % 4) % 4));
      const json = decoder.decode(Uint8Array.from(binary, byte => byte.charCodeAt(0)));
      const profile = normalize(JSON.parse(json));
      // Exact re-encoding rejects duplicate keys, escaping/order/spacing variants,
      // noncanonical base64 bits and non-normalized endpoint representations.
      if (encoded(profile) !== wire) throw new Error(ERROR);
      return profile;
    } catch (_) { throw new Error(ERROR); }
  }
  const api = Object.freeze({ encode, decode, MAX_WIRE_BYTES });
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PortmapConnectionCode = api;
})(globalThis);
