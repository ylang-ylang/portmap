/* SPDX-License-Identifier: MIT */
"use strict";
(function (root) {
  const GUIDANCE = "Required experimental networking is unavailable. Enable extension experiments and use a compatible Firefox build, then reload Portmap. Both direct and SSH are disabled.";
  const METHODS = ["configure", "selfCheck", "connect", "read", "write", "close", "listen", "accept", "closeListener"];
  function createRuntime(api, dependencies = {}) {
    const native = api.portmapTransport;
    const proxyFactory = dependencies.proxy || root.PortmapProxy;
    const sshFactory = dependencies.ssh || root.PortmapSSH;
    const managedIdentity = dependencies.managedIdentity;
    let capability = { available: false, reason: GUIDANCE };
    let epoch = 0;
    let nodes = new Map();
    let disposal = Promise.resolve();
    let checking;
    let admission = Promise.resolve();
    let temporary = null;
    function serializeAdmission(operation) {
      const task = admission.then(operation);
      admission = task.catch(() => {});
      return task;
    }
    async function check() {
      if (!checking) checking = (async () => {
        if (!native || METHODS.some(method => typeof native[method] !== "function")) return capability;
        let timer;
        try {
          const ok = await Promise.race([native.selfCheck(), new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error("Capability deadline")), 5000);
          })]);
          if (ok === true) capability = { available: true, reason: "" };
        } catch (_) { /* Unsupported is distinct from remote node failures. */ }
        finally { clearTimeout(timer); }
        return capability;
      })();
      return checking;
    }
    function invalidate() {
      epoch++;
      temporary?.abort();
      const previous = nodes;
      nodes = new Map();
      const closing = [];
      for (const node of previous.values()) {
        node.stale = true;
        node.abort?.abort();
        // Close session and proxy concurrently: native close wakes channel reads.
        closing.push(Promise.allSettled([node.proxy?.close(), node.session?.close()]));
      }
      disposal = Promise.allSettled([disposal, ...closing]).then(() => {});
    }
    const current = node => !node.stale && nodes.get(node.connection.id) === node;
    function sessionFor(node) {
      if (node.session?.closed) node.session = null;
      if (node.session) return Promise.resolve(node.session);
      if (!node.opening) {
        node.state = { phase: "connecting", error: "" };
        const entry = node.connection;
        node.abort = new AbortController();
        node.opening = Promise.resolve().then(async () => {
          if (!current(node)) throw new Error("Stale connection");
          let auth = entry.ssh.auth;
          if (auth.type === "managed") {
            if (!managedIdentity) throw new Error("Browser SSH key unavailable");
            auth = await managedIdentity.resolve();
          }
          if (!current(node)) throw new Error("Stale connection");
          return sshFactory.connect({ host: entry.host, port: entry.ssh.port,
            username: entry.ssh.username, auth,
            hostKeySha256Hex: entry.ssh.hostKeySha256Hex }, native, { signal: node.abort.signal });
        }).then(async session => {
          if (!current(node)) { await session.close(); throw new Error("Stale connection"); }
          node.session = session;
          return session;
        }).finally(() => { node.opening = null; });
      }
      return node.opening;
    }
    async function open(node, logicalPort) {
      if (!current(node)) throw new Error("Stale connection");
      const entry = node.connection;
      const port = logicalPort === 80 ? entry.httpPort : logicalPort === 443 ? entry.httpsPort : null;
      if (!port) throw new Error("No ingress configured for this protocol");
      let stream;
      try {
        node.state = { phase: "connecting", error: "" };
        if (entry.mode === "ssh") stream = await (await sessionFor(node)).open(entry.ssh.targetHost, port);
        else {
          const id = await native.connect(entry.host, port);
          stream = { read: () => native.read(id), write: data => native.write(id, data), close: () => native.close(id) };
        }
        if (!current(node)) { await stream.close(); throw new Error("Stale connection"); }
        node.state = { phase: "ready", error: "" };
        return {
          read: async () => {
            try { return await stream.read(); }
            catch (_) { if (current(node)) node.state = { phase: "error", error: "Connection interrupted. Check this node's endpoint, SSH authentication and pinned host key." }; throw new Error("Upstream read failed"); }
          },
          write: async data => {
            try { await stream.write(data); }
            catch (_) { if (current(node)) node.state = { phase: "error", error: "Connection interrupted. Check this node's endpoint, SSH authentication and pinned host key." }; throw new Error("Upstream write failed"); }
          }, close: () => stream.close()
        };
      } catch (_) {
        if (current(node)) node.state = { phase: "error", error: "Cannot connect to this node. Check its endpoint, SSH authentication and pinned host key." };
        throw new Error("Node connection failed");
      }
    }
    function configure(connections, isolationKey) {
      invalidate();
      const generation = epoch;
      return serializeAdmission(async () => {
        await disposal;
        if (generation !== epoch) return [];
        const endpoints = [];
        for (const entry of connections.filter(entry => entry.enabled)) {
          for (const port of entry.mode === "ssh" ? [entry.ssh.port] : [entry.httpPort, entry.httpsPort].filter(Boolean)) {
            if (!endpoints.some(endpoint => endpoint.host === entry.host && endpoint.port === port)) endpoints.push({ host: entry.host, port });
          }
        }
        await native.configure(endpoints);
        if (generation !== epoch) return [];
        const active = connections.filter(entry => entry.enabled).map(connection => {
          const node = { connection, state: { phase: "idle", error: "" }, stale: false };
          nodes.set(connection.id, node);
          return node;
        });
        const routes = await Promise.all(active.map(async node => {
          try {
            const proxy = await proxyFactory.start(native, port => open(node, port), { domain: node.connection.domain });
            if (!current(node)) { await proxy.close(); return null; }
            node.proxy = proxy;
            return { connection: node.connection, node, proxy: Object.freeze({ type: "http", host: proxy.host,
              port: proxy.port, connectionIsolationKey: `${isolationKey}:${node.connection.id}`, failoverTimeout: 1 }) };
          } catch (_) {
            if (current(node)) node.state = { phase: "error", error: "Cannot create this node's private loopback listener." };
            return null;
          }
        }));
        return generation === epoch ? routes.filter(Boolean) : [];
      });
    }
    function atSshEndpoint(endpoint, operation, { signal } = {}) {
      invalidate();
      const generation = epoch;
      const abort = new AbortController();
      temporary = abort;
      const cancel = () => abort.abort();
      signal?.addEventListener("abort", cancel, { once: true });
      if (signal?.aborted) cancel();
      let timer;
      return serializeAdmission(async () => {
        await disposal;
        const guard = () => {
          if (abort.signal.aborted || generation !== epoch) throw new Error("SSH setup cancelled. No connection was saved.");
        };
        try {
          guard();
          // configure replaces permissions AND closes every old listener/socket.
          // During onboarding only this exact SSH endpoint is admitted, never a
          // union with another node's destination or a direct-transport fallback.
          await native.configure([endpoint]);
          guard();
          const cancelled = new Promise((_, reject) => {
            abort.signal.addEventListener("abort", () => reject(new Error("SSH setup cancelled or timed out. No connection was saved.")), { once: true });
          });
          timer = setTimeout(cancel, 90000);
          return await Promise.race([Promise.resolve().then(() => { guard(); return operation(abort.signal); }), cancelled]);
        } finally {
          clearTimeout(timer);
          signal?.removeEventListener("abort", cancel);
          if (temporary === abort) temporary = null;
          // Revoke temporary permission even if auth, cancellation, or cleanup
          // failed. Saved routes are rebuilt separately by the controller.
          await native.configure([]);
        }
      });
    }
    function inspectHost(endpoint, options) {
      return atSshEndpoint(endpoint, signal => sshFactory.inspectHost(endpoint, native, { signal }), options);
    }
    function authenticateManaged(connection, auth, { password, signal } = {}) {
      let installAttempted = false;
      return atSshEndpoint({ host: connection.host, port: connection.ssh.port }, async operationSignal => {
        async function useSession(credentials, operation) {
          const session = await sshFactory.connect({ host: connection.host, port: connection.ssh.port,
            username: connection.ssh.username, hostKeySha256Hex: connection.ssh.hostKeySha256Hex,
            auth: credentials }, native, { signal: operationSignal });
          const close = () => { void session.close().catch(() => {}); };
          operationSignal.addEventListener("abort", close, { once: true });
          try {
            if (operationSignal.aborted) throw new Error("SSH setup cancelled.");
            return await operation(session);
          } finally {
            operationSignal.removeEventListener("abort", close);
            await session.close();
          }
        }
        if (password !== undefined) {
          await useSession({ type: "password", password }, async session => {
            installAttempted = true;
            if ((await session.installPublicKey(auth.publicKey))?.installed !== true) throw new Error("The server did not confirm public-key installation.");
          });
        }
        // Never reuse the password-authenticated connection as verification.
        // Its close above must finish before this new pinned key-auth login.
        await useSession(auth, async () => {
          if (operationSignal.aborted) throw new Error("SSH setup cancelled.");
        });
        return { verified: true };
      }, { signal }).catch(failure => {
        if (installAttempted) {
          const error = new Error(`${failure.message} The public key may already be installed on the server. No connection was saved; you can retry using the browser key.`);
          error.possiblyInstalled = true;
          throw error;
        }
        throw failure;
      }).finally(() => { password = undefined; });
    }
    function state() {
      return Object.fromEntries([...nodes].map(([id, node]) => [id, node.proxy?.closed
        ? { phase: "error", error: "Private listener closed. Save this connection to reconnect." } : { ...node.state }]));
    }
    return Object.freeze({ check, invalidate, configure, state, inspectHost, authenticateManaged,
      isCurrent: route => current(route.node) && !route.node.proxy?.closed,
      credentials: route => current(route.node) && !route.node.proxy?.closed ? route.node.proxy : null });
  }
  const exported = Object.freeze({ createRuntime });
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else root.PortmapTransports = exported;
})(globalThis);
