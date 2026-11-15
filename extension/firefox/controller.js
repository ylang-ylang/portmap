/* SPDX-License-Identifier: MIT */
"use strict";

(function (root) {
  const core = typeof module !== "undefined" && module.exports ? require("./core.js") : root.PortmapCore;

  // Firefox can resolve admission DNS before webRequest cancellation (notably
  // for WebSockets). A non-direct literal keeps that early step off DNS.
  // This is never a route: no selection record or credentials are assigned,
  // and both mandatory blocking listeners cancel before any transport opens.
  const BLOCKED_ADMISSION = Object.freeze([
    Object.freeze({ type: "socks", host: "192.0.2.1", port: 65535, proxyDNS: true,
      connectionIsolationKey: "portmap-blocked-admission" }),
    null
  ]);

  function createController(api, makeId = () => crypto.randomUUID()) {
    let config = core.readConfig(undefined);
    let routes = [];
    let phase = "loading";
    let error = "";
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
    }

    function publish(nextConfig) {
      config = nextConfig;
      routes = core.compileConnections(config.connections, makeId());
      phase = "ready";
      error = "";
    }

    function enqueue(operation) {
      const task = queue.then(operation);
      queue = task.catch(() => {});
      return task;
    }

    async function load() {
      const epoch = generation;
      try {
        const stored = await api.storage.local.get("config");
        const nextConfig = core.readConfig(stored.config);
        if (epoch === generation) publish(nextConfig);
      } catch (failure) {
        if (epoch === generation) {
          invalidate("error");
          error = "Cannot load valid local connections. All .portmap traffic is blocked. " + failure.message;
        }
      }
    }

    function proxyRequest(details) {
      const target = core.inspectUrl(details.url);
      if (!target.portmap) return undefined; // Preserve the existing browser/default proxy chain.
      selections.delete(details.requestId);
      if (initializing) {
        const epoch = generation;
        // Proxy resolution precedes webRequest. Hold the first navigation until
        // storage is validated, without authorizing an obsolete configuration.
        return initialLoad.then(() => epoch === generation
          ? selectProxy(details, target) : BLOCKED_ADMISSION, () => BLOCKED_ADMISSION);
      }
      return selectProxy(details, target);
    }

    function selectProxy(details, target) {
      if (phase !== "ready" || !target.supported) return BLOCKED_ADMISSION;
      const route = routes.find(entry => core.matchesDomain(target.host, entry.connection.domain));
      if (!route) return BLOCKED_ADMISSION;
      selections.set(details.requestId, { generation, host: target.host, route });
      return [{ ...route.proxy }, null]; // Never fall back to a default proxy or direct connection.
    }

    function beforeRequest(details) {
      const target = core.inspectUrl(details.url);
      if (!target.portmap) return {};
      const selected = selections.get(details.requestId);
      const actual = details.proxyInfo;
      if (phase !== "ready" || !target.supported || !selected || selected.generation !== generation ||
          selected.host !== target.host || !actual || actual.type !== selected.route.proxy.type ||
          actual.host !== selected.route.proxy.host || actual.port !== selected.route.proxy.port ||
          actual.proxyDNS !== true) {
        return { cancel: true };
      }
      return {};
    }

    function completed(details) {
      selections.delete(details.requestId);
    }

    // Register synchronously, before the first asynchronous storage read. Firefox
    // wakes startup blocking listeners before matching network requests proceed.
    const filter = { urls: ["<all_urls>"] };
    api.webRequest.onBeforeRequest.addListener(beforeRequest, filter, ["blocking"]);
    api.proxy.onRequest.addListener(proxyRequest, filter);
    api.webRequest.onCompleted.addListener(completed, filter);
    api.webRequest.onErrorOccurred.addListener(completed, filter);
    api.webRequest.onBeforeSendHeaders.addListener(beforeRequest, filter, ["blocking"]);
    api.storage.onChanged.addListener((changes, area) => {
      if (area !== "local" || !Object.hasOwn(changes, "config")) return;
      if (pendingRevision && changes.config.newValue?.revision === pendingRevision) return;
      invalidate();
      enqueue(load);
    });
    const initialLoad = enqueue(load).finally(() => { initializing = false; });

    function snapshot() {
      return { phase, error, connections: config.connections.map(entry => ({ ...entry })) };
    }

    async function persist(connections) {
      const validated = core.validateConnections(connections);
      const nextConfig = { schemaVersion: 2, revision: makeId(), connections: validated };
      invalidate();
      const epoch = generation;
      pendingRevision = nextConfig.revision;
      try {
        await api.storage.local.set({ config: nextConfig });
        if (epoch === generation) publish(nextConfig);
      } catch (_) {
        phase = "error";
        error = "Saving local connections failed. All .portmap traffic is blocked until storage is available.";
        throw new Error(error);
      } finally {
        pendingRevision = null;
      }
      return snapshot();
    }

    function command(message) {
      return enqueue(async () => {
        if (message.action === "get") return snapshot();
        if (message.action === "reset") return persist([]);
        if (phase !== "ready") throw new Error(error || "Connections are loading. Try again shortly.");
        if (message.action === "preview-import" || message.action === "import") {
          const payload = core.decodeConnectionCode(message.code);
          const existing = config.connections.find(entry => entry.domain === payload.domain);
          if (message.action === "import" && (message.revision !== config.revision ||
              message.replaceId !== (existing?.id ?? null))) {
            throw new Error("Connections changed. Preview the connection code again before importing.");
          }
          const connection = core.normalizeConnection({ ...payload,
            id: existing?.id ?? makeId(), enabled: existing?.enabled ?? true });
          const connections = core.validateConnections([
            ...config.connections.filter(entry => entry.id !== existing?.id), connection
          ]);
          if (message.action === "import") return persist(connections);
          return { ...snapshot(), importPreview: {
            name: connection.name, domain: connection.domain,
            proxyHost: connection.proxyHost, proxyPort: connection.proxyPort,
            replaceId: existing?.id ?? null, enabled: connection.enabled, revision: config.revision
          } };
        }
        if (message.action === "save") {
          const input = { ...message.connection };
          if (!input.id) input.id = makeId();
          const connection = core.normalizeConnection(input);
          const previous = config.connections.filter(entry => entry.id !== connection.id);
          return persist([...previous, connection]);
        }
        const existing = config.connections.find(entry => entry.id === message.id);
        if (!existing) throw new Error("Connection no longer exists. Refresh the list.");
        if (message.action === "delete") return persist(config.connections.filter(entry => entry.id !== existing.id));
        if (message.action === "toggle") {
          return persist(config.connections.map(entry => entry.id === existing.id ? { ...entry, enabled: !entry.enabled } : entry));
        }
        if (message.action === "open") {
          if (!existing.enabled) throw new Error("Enable this connection before opening its node.");
          if (!["http", "https"].includes(message.scheme)) throw new Error("Choose HTTP or HTTPS.");
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
