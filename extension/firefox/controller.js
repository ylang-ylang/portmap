/* SPDX-License-Identifier: MIT */
"use strict";
(function (root) {
  const core = typeof module !== "undefined" && module.exports ? require("./core.js") : root.PortmapCore;
  const transports = typeof module !== "undefined" && module.exports ? require("./transports.js") : root.PortmapTransports;
  const managed = typeof module !== "undefined" && module.exports ? require("./managed-identity.js") : root.PortmapManagedIdentity;
  // Non-direct literal admission sentinel prevents early DNS, including WS.
  // It never acquires a selection or credentials and both blocking hooks cancel.
  const BLOCKED_ADMISSION = Object.freeze([
    Object.freeze({ type: "http", host: "127.0.0.1", port: 9,
      connectionIsolationKey: "portmap-blocked-admission", failoverTimeout: 1 }), null
  ]);
  function createController(api, makeId = () => crypto.randomUUID(), runtime, identity = managed.createStore()) {
    runtime ??= transports.createRuntime(api, { managedIdentity: identity });
    let config = core.readConfig(undefined);
    let routes = [];
    let phase = "loading";
    let error = "";
    let capability = { available: false, reason: "Checking required experimental networking capabilities." };
    let generation = 0;
    let pendingRevision = null;
    let externalSerial = 0;
    let externalStoredConfig;
    let queue = Promise.resolve();
    let initializing = true;
    const selections = new Map();
    const owners = new Set();
    function openEditor() {
      const owner = { epoch: 0, proof: null, operation: null };
      owners.add(owner);
      return owner;
    }
    function cancelOwner(owner, disconnect = false) {
      if (!owners.has(owner)) return;
      owner.epoch++;
      owner.proof = null;
      owner.operation?.abort();
      if (disconnect) owners.delete(owner);
    }
    function cancelSetups() { for (const owner of owners) cancelOwner(owner); }
    function invalidate(nextPhase = "loading", cancel = true) {
      if (cancel) cancelSetups();
      generation++;
      phase = nextPhase;
      routes = [];
      selections.clear();
      runtime.invalidate();
    }
    async function publish(nextConfig, epoch, guard) {
      const nextRoutes = capability.available ? await runtime.configure(nextConfig.connections, makeId()) : [];
      guard?.();
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
      externalSerial++;
      externalStoredConfig = changes.config.newValue;
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
    async function persist(connections, guard) {
      const validated = core.validateConnections(connections);
      const previous = config;
      const externalAtStart = externalSerial;
      const nextConfig = { schemaVersion: 3, revision: makeId(), connections: validated };
      guard?.();
      invalidate("loading", !guard);
      const epoch = generation;
      pendingRevision = nextConfig.revision;
      try {
        guard?.();
        await api.storage.local.set({ config: nextConfig });
        guard?.();
        if (epoch === generation) await publish(nextConfig, epoch, guard);
      } catch (failure) {
        if (guard && failure.cancelledSetup) {
          try {
            const stored = await api.storage.local.get("config");
            if (stored.config?.revision === nextConfig.revision) {
              const restore = externalSerial === externalAtStart ? previous : externalStoredConfig;
              pendingRevision = restore?.revision;
              if (restore === undefined) await api.storage.local.remove("config");
              else await api.storage.local.set({ config: restore });
            }
            if (epoch === generation) {
              invalidate("loading", false);
              await publish(previous, generation);
            }
          } catch (_) {
            invalidate("error", false);
            error = "A cancelled draft could not be removed from local storage. Check saved connections or reset local data before continuing.";
            throw new Error(error);
          }
          throw failure;
        }
        if (epoch === generation) {
          invalidate(capability.available ? "error" : "unsupported", false);
          error = "Saving local connections failed. All .portmap traffic is blocked until storage is available.";
        }
        throw new Error("Saving local connections failed. All .portmap traffic is blocked until storage is available.");
      } finally { pendingRevision = null; }
      return snapshot();
    }
    function setupGuard(owner, lease, revision) {
      if (!owners.has(owner) || owner.epoch !== lease || config.revision !== revision) {
        const failure = new Error("SSH setup cancelled because the draft, page, or local connections changed. No connection was saved.");
        failure.cancelledSetup = true;
        throw failure;
      }
    }
    async function temporaryOperation(owner, lease, operation) {
      const revision = config.revision;
      const guard = () => setupGuard(owner, lease, revision);
      guard();
      const abort = new AbortController();
      owner.operation = abort;
      invalidate("checking", false);
      const epoch = generation;
      try {
        const value = await operation(abort.signal, guard);
        guard();
        return value;
      } finally {
        if (owner.operation === abort) owner.operation = null;
        if (epoch === generation) await publish(config, epoch);
      }
    }
    function trustedConnection(connection, owner) {
      const endpointMatches = candidate => candidate.host === connection.host &&
        candidate.port === connection.ssh.port && candidate.pin === connection.ssh.hostKeySha256Hex;
      const saved = config.connections.find(entry => entry.id === connection.id && entry.mode === "ssh");
      if (saved && endpointMatches({ host: saved.host, port: saved.ssh.port, pin: saved.ssh.hostKeySha256Hex })) return;
      const proof = owner.proof;
      if (!proof?.confirmed || proof.expires < Date.now() || proof.revision !== config.revision ||
          !endpointMatches(proof)) throw new Error("Check this server's fingerprint and explicitly confirm it before connecting.");
    }
    function mayCreateIdentity() {
      return !config.connections.some(entry => entry.mode === "ssh" && entry.ssh.auth.type === "managed");
    }
    async function setupCommand(message, owner, lease) {
      const revision = config.revision;
      const guard = () => setupGuard(owner, lease, revision);
      guard();
      if (message.action === "managed-public-key") {
        const publicKey = await identity.publicKey({ create: mayCreateIdentity() });
        guard();
        return { ...snapshot(), setup: publicKey };
      }
      if (message.action === "inspect-host") {
        const endpoint = core.normalizeSshEndpoint(message.endpoint);
        const expected = message.expectedPin ? core.normalizeFingerprint(message.expectedPin) : null;
        const detected = await temporaryOperation(owner, lease, signal => runtime.inspectHost(endpoint, { signal }));
        guard();
        const pin = core.normalizeFingerprint(detected.hostKeySha256Hex);
        if (expected && expected !== pin) throw new Error("This server's host key does not match the entered fingerprint. No credentials were sent.");
        owner.proof = { ...endpoint, pin, challenge: makeId(), confirmed: false, expires: Date.now() + 120000, revision };
        return { ...snapshot(), setup: { challenge: owner.proof.challenge, endpoint, hostKeySha256Hex: pin } };
      }
      if (message.action === "confirm-host") {
        const endpoint = core.normalizeSshEndpoint(message.endpoint);
        const pin = core.normalizeFingerprint(message.hostKeySha256Hex);
        const proof = owner.proof;
        if (!proof || proof.confirmed || proof.challenge !== message.challenge || proof.expires < Date.now() ||
            proof.revision !== revision || proof.host !== endpoint.host || proof.port !== endpoint.port || proof.pin !== pin) {
          throw new Error("The host check expired or the endpoint changed. Check the server fingerprint again.");
        }
        proof.confirmed = true;
        return { ...snapshot(), setup: { confirmed: true, endpoint, hostKeySha256Hex: pin } };
      }
      const input = { ...message.connection };
      if (!input.id) input.id = makeId();
      const connection = core.normalizeConnection(input);
      if (connection.mode !== "ssh" || connection.ssh.auth.type !== "managed") throw new Error("Choose browser-key authentication for this operation.");
      core.validateConnections([...config.connections.filter(entry => entry.id !== connection.id), connection]);
      trustedConnection(connection, owner);
      let password;
      if (message.action === "managed-install") {
        // Reuse credential validation, but never retain the password in config.
        password = core.normalizeConnection({ ...connection,
          ssh: { ...connection.ssh, auth: { type: "password", password: message.password } } }).ssh.auth.password;
      }
      const auth = await identity.resolve({ create: mayCreateIdentity() });
      guard();
      let possiblyInstalled = false;
      try {
        await temporaryOperation(owner, lease, async signal => {
          try { await runtime.authenticateManaged(connection, auth, { password, signal }); }
          catch (failure) { possiblyInstalled = failure.possiblyInstalled === true; throw failure; }
          possiblyInstalled = message.action === "managed-install";
        });
        guard();
        trustedConnection(connection, owner);
        return await persist([...config.connections.filter(entry => entry.id !== connection.id), connection], guard);
      } catch (failure) {
        if (possiblyInstalled && !failure.possiblyInstalled) {
          failure.message += " The public key may already be installed on the server. No connection was saved.";
          failure.possiblyInstalled = true;
        }
        throw failure;
      } finally { password = undefined; }
    }
    function command(message, owner) {
      const setupActions = ["managed-public-key", "inspect-host", "confirm-host", "managed-save", "managed-install", "cancel-setup"];
      const setup = setupActions.includes(message.action);
      if (setup && !owners.has(owner)) return Promise.reject(new Error("Open the connection editor to set up the browser SSH key."));
      if (message.action === "cancel-setup") {
        cancelOwner(owner);
        return Promise.resolve(snapshot());
      }
      if (["save", "reset", "delete", "toggle"].includes(message.action)) cancelSetups();
      const lease = owner?.epoch;
      return enqueue(async () => {
        if (message.action === "get") return snapshot();
        if (message.action === "reset") {
          const result = await persist([]);
          try { await identity.destroy(); }
          catch (failure) { throw new Error(`Connections were deleted. ${failure.message}`); }
          return result;
        }
        if (phase !== "ready") throw new Error(error || "Connections are loading. Try again shortly.");
        if (setup) return setupCommand(message, owner, lease);
        if (message.action === "save") {
          const input = { ...message.connection };
          if (!input.id) input.id = makeId();
          const connection = core.normalizeConnection(input);
          if (connection.mode === "ssh" && connection.ssh.auth.type === "managed") {
            throw new Error("Use Test and save to verify the browser SSH key before saving this connection.");
          }
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
      }).finally(() => { if (message.action === "managed-install") delete message.password; });
    }
    return Object.freeze({ command, openEditor, cancelOwner });
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { createController };
  else root.PortmapController = { createController };
})(globalThis);
