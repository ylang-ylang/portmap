/* SPDX-License-Identifier: MIT */
"use strict";
(function (root) {
  const core = typeof module !== "undefined" && module.exports ? require("./core.js") : root.PortmapCore;
  const transports = typeof module !== "undefined" && module.exports ? require("./transports.js") : root.PortmapTransports;
  // Non-direct literal admission sentinel prevents early DNS, including WS.
  // It never acquires a selection or credentials and both blocking hooks cancel.
  const BLOCKED_ADMISSION = Object.freeze([
    Object.freeze({ type: "http", host: "127.0.0.1", port: 9,
      connectionIsolationKey: "portmap-blocked-admission", failoverTimeout: 1 }), null
  ]);
  function createController(api, makeId = () => crypto.randomUUID(), runtime = transports.createRuntime(api)) {
    let config = core.readConfig(undefined);
    let routes = [];
    let phase = "loading";
    let error = "";
    let capability = { available: false, reason: "Checking required experimental networking capabilities." };
    let generation = 0;
    let pendingRevision = null;
    let queue = Promise.resolve();
    let initializing = true;
    const selections = new Map();
    function invalidate(nextPhase = "loading") {
      generation++;
      phase = nextPhase;
      routes = [];
      selections.clear();
      runtime.invalidate();
    }
    async function publish(nextConfig, epoch) {
      const nextRoutes = capability.available ? await runtime.configure(nextConfig.connections, makeId()) : [];
      if (epoch !== generation) return;
      config = nextConfig;
      routes = nextRoutes;
      phase = capability.available ? "ready" : "unsupported";
      error = capability.available ? "" : capability.reason;
    }
    function enqueue(operation) {
      const task = queue.then(operation);
      queue = task.catch(() => {});
      return task;
    }
    async function load() {
      const epoch = generation;
      try {
        const checked = await runtime.check();
        if (epoch !== generation) return;
        capability = checked;
        const stored = await api.storage.local.get("config").catch(() => { throw new Error("Local storage is unavailable."); });
        if (epoch === generation) await publish(core.readConfig(stored.config), epoch);
      } catch (failure) {
        if (epoch === generation) {
          invalidate(capability.available ? "error" : "unsupported");
          error = capability.available
            ? "Cannot load valid local connections. All .portmap traffic is blocked. " + failure.message
            : capability.reason;
        }
      }
    }
    function proxyRequest(details) {
      const target = core.inspectUrl(details.url);
      if (!target.portmap) return undefined;
      selections.delete(details.requestId);
      if (initializing) {
        const epoch = generation;
        return initialLoad.then(() => epoch === generation ? selectProxy(details, target) : BLOCKED_ADMISSION,
          () => BLOCKED_ADMISSION);
      }
      return selectProxy(details, target);
    }
    function selectProxy(details, target) {
      if (phase !== "ready" || !target.supported) return BLOCKED_ADMISSION;
      const route = routes.find(entry => core.matchesDomain(target.host, entry.connection.domain));
      if (!route || !runtime.isCurrent(route)) return BLOCKED_ADMISSION;
      if (new URL(details.url).protocol.match(/^(https|wss):$/) && !route.connection.httpsPort) return BLOCKED_ADMISSION;
      selections.set(details.requestId, { generation, host: target.host, route });
      return [{ ...route.proxy }, null];
    }
    function selection(details) {
      const target = core.inspectUrl(details.url);
      const selected = selections.get(details.requestId);
      if (phase !== "ready" || !target.supported || !selected || selected.generation !== generation ||
          selected.host !== target.host || !runtime.isCurrent(selected.route)) return null;
      return selected;
    }
    function actualProxy(actual, selected) {
      return actual && actual.type === "http" && actual.host === selected.route.proxy.host &&
        actual.port === selected.route.proxy.port;
    }
    function beforeRequest(details) {
      if (!core.inspectUrl(details.url).portmap) return {};
      const selected = selection(details);
      return selected && actualProxy(details.proxyInfo, selected) ? {} : { cancel: true };
    }
    function authenticate(details) {
      if (!details.isProxy) return {}; // Never supply proxy secrets to an origin challenge.
      if (!core.inspectUrl(details.url).portmap) return {};
      const selected = selection(details);
      const owner = selected && runtime.credentials(selected.route);
      if (!owner || details.challenger?.host !== owner.host || details.challenger?.port !== owner.port ||
          details.scheme?.toLowerCase() !== "basic" || details.realm !== owner.realm ||
          (details.proxyInfo && !actualProxy(details.proxyInfo, selected))) return { cancel: true };
      return { authCredentials: { ...owner.credentials } };
    }
    const completed = details => { selections.delete(details.requestId); };
    // These listeners MUST be registered synchronously, before async initialization.
    const filter = { urls: ["<all_urls>"] };
    api.webRequest.onBeforeRequest.addListener(beforeRequest, filter, ["blocking"]);
    api.proxy.onRequest.addListener(proxyRequest, filter);
    api.webRequest.onBeforeSendHeaders.addListener(beforeRequest, filter, ["blocking"]);
    api.webRequest.onAuthRequired.addListener(authenticate, filter, ["blocking"]);
    api.webRequest.onCompleted.addListener(completed, filter);
    api.webRequest.onErrorOccurred.addListener(completed, filter);
    api.storage.onChanged.addListener((changes, area) => {
      if (area !== "local" || !Object.hasOwn(changes, "config")) return;
      if (pendingRevision && changes.config.newValue?.revision === pendingRevision) return;
      invalidate();
      enqueue(load);
    });
    const initialLoad = enqueue(load).finally(() => { initializing = false; });
    function snapshot() {
      // Full config is returned only to authenticated internal options editor;
      // node status contains no endpoint, credentials, or backend error strings.
      return { phase, error, capability: { ...capability }, nodes: runtime.state(),
        connections: config.connections.map(entry => ({ ...entry })) };
    }
    async function persist(connections) {
      const validated = core.validateConnections(connections);
      const nextConfig = { schemaVersion: 3, revision: makeId(), connections: validated };
      invalidate();
      const epoch = generation;
      pendingRevision = nextConfig.revision;
      try {
        await api.storage.local.set({ config: nextConfig });
        if (epoch === generation) await publish(nextConfig, epoch);
      } catch (_) {
        if (epoch === generation) {
          invalidate(capability.available ? "error" : "unsupported");
          error = "Saving local connections failed. All .portmap traffic is blocked until storage is available.";
        }
        throw new Error("Saving local connections failed. All .portmap traffic is blocked until storage is available.");
      } finally { pendingRevision = null; }
      return snapshot();
    }
    function command(message) {
      return enqueue(async () => {
        if (message.action === "get") return snapshot();
        if (message.action === "reset") return persist([]);
        if (phase !== "ready") throw new Error(error || "Connections are loading. Try again shortly.");
        if (message.action === "save") {
          const input = { ...message.connection };
          if (!input.id) input.id = makeId();
          const connection = core.normalizeConnection(input);
          return persist([...config.connections.filter(entry => entry.id !== connection.id), connection]);
        }
        const existing = config.connections.find(entry => entry.id === message.id);
        if (!existing) throw new Error("Connection no longer exists. Refresh the list.");
        if (message.action === "delete") return persist(config.connections.filter(entry => entry.id !== existing.id));
        if (message.action === "toggle") return persist(config.connections.map(entry => entry.id === existing.id
          ? { ...entry, enabled: !entry.enabled } : entry));
        if (message.action === "open") {
          if (!existing.enabled) throw new Error("Enable this connection before opening its node.");
          if (!["http", "https"].includes(message.scheme)) throw new Error("Choose HTTP or HTTPS.");
          if (message.scheme === "https" && !existing.httpsPort) throw new Error("Configure an HTTPS ingress port first.");
          await api.tabs.create({ url: `${message.scheme}://${existing.domain}/` });
          return snapshot();
        }
        throw new Error("Unknown connection operation.");
      });
    }
    return Object.freeze({ command });
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { createController };
  else root.PortmapController = { createController };
})(globalThis);
