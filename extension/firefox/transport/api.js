var { ExtensionCommon } = ChromeUtils.importESModule('resource://gre/modules/ExtensionCommon.sys.mjs');

// Addon-private byte streams. Only validated saved endpoints are admitted.
this.portmapTransport = class extends ExtensionCommon.ExtensionAPI {
  getAPI(context) {
    // Firefox's parent extension context identifies the manifest background.
    // Options/content contexts must never configure destinations or own sockets.
    if (context.viewType !== 'background') return {};
    const connections = new Map();
    const listeners = new Map();
    const thread = Services.tm.currentThread;
    const CHUNK = 65536;
    const MAX_QUEUED = 1048576;
    let nextId = 1;
    let stopped = false;
    let endpoints = [];
    const isCode = (error, code) => error && error.result === code;
    const timer = (ms, callback) => {
      const t = Cc['@mozilla.org/timer;1'].createInstance(Ci.nsITimer);
      t.initWithCallback(callback, ms, Ci.nsITimer.TYPE_ONE_SHOT);
      return t;
    };
    const dispose = (c, error = new Error('Socket closed')) => {
      if (c.closed) return;
      c.closed = true;
      connections.delete(c.id);
      c.connecting?.(error);
      c.reading?.(error);
      for (const job of c.writes.splice(0)) {
        job.timer.cancel();
        try { job.source.close(); } catch (_) {}
        job.reject(error);
      }
      c.queued = 0;
      try { c.input?.asyncWait(null, 0, 0, thread); } catch (_) {}
      try { c.output?.asyncWait(null, 0, 0, thread); } catch (_) {}
      try { c.input?.close(); } catch (_) {}
      try { c.output?.close(); } catch (_) {}
      try { c.transport.close(Cr.NS_OK); } catch (_) {}
    };
    const adopt = transport => {
      if (stopped || connections.size >= 4096) {
        transport.close(Cr.NS_OK);
        throw new Error('Socket resource limit or shutdown');
      }
      const c = { id: nextId++, transport, closed: false, reading: null,
        connecting: null, writes: [], queued: 0, eof: false };
      connections.set(c.id, c);
      try {
        transport.setTimeout(Ci.nsISocketTransport.TIMEOUT_CONNECT, 15);
        transport.setTimeout(Ci.nsISocketTransport.TIMEOUT_READ_WRITE, 120);
        c.input = transport.openInputStream(0, CHUNK, 1).QueryInterface(Ci.nsIAsyncInputStream);
        c.output = transport.openOutputStream(Ci.nsITransport.OPEN_UNBUFFERED, 0, 0)
          .QueryInterface(Ci.nsIAsyncOutputStream);
        c.binary = Cc['@mozilla.org/binaryinputstream;1'].createInstance(Ci.nsIBinaryInputStream);
        c.binary.setInputStream(c.input);
        return c;
      } catch (error) { dispose(c, error); throw error; }
    };
    const get = id => {
      const c = connections.get(id);
      if (!c || c.closed) throw new Error('Unknown or closed socket');
      return c;
    };
    const pumpWrites = c => {
      if (c.closed || !c.writes.length) return;
      const job = c.writes[0];
      try {
        // setByteStringData is explicitly non-UTF8 and preserves embedded NUL.
        // writeFrom advances the source only by bytes actually accepted.
        const count = c.output.writeFrom(job.source, job.remaining);
        // ReadSegments suppresses writer errors. Distinguish a blocked socket
        // (status OK) from a terminal error before waiting again.
        if (!count) c.output.streamStatus();
        if (count > job.remaining) throw new Error('Invalid socket write count');
        job.remaining -= count;
        if (!job.remaining) {
          job.timer.cancel();
          job.source.close();
          c.writes.shift();
          c.queued -= job.length;
          job.resolve();
          if (!c.writes.length) return;
        }
        c.output.asyncWait({ onOutputStreamReady: () => pumpWrites(c) }, 0, 0, thread);
      } catch (error) {
        if (isCode(error, Cr.NS_BASE_STREAM_WOULD_BLOCK)) {
          try { c.output.asyncWait({ onOutputStreamReady: () => pumpWrites(c) }, 0, 0, thread); }
          catch (failure) { dispose(c, failure); }
        } else dispose(c, error);
      }
    };
    const closeListener = id => {
      const l = listeners.get(id);
      if (!l) return;
      listeners.delete(id);
      l.closed = true;
      if (l.pending) l.pending(new Error('Listener closed'));
      for (const c of l.queue.splice(0)) dispose(c);
      try { l.socket.close(); } catch (_) {}
    };
    const shutdown = () => {
      if (stopped) return;
      stopped = true;
      for (const id of [...listeners.keys()]) closeListener(id);
      for (const c of [...connections.values()]) dispose(c, new Error('Addon shutdown'));
    };
    context.callOnClose({ close: shutdown });
    this.shutdown = shutdown;
    const api = {
      configure: async items => {
        if (stopped || !Array.isArray(items) || items.length > 512 || items.some(e =>
          !e || typeof e.host !== 'string' || !e.host || e.host.length > 253 ||
          /[\s/@?#\\]/.test(e.host) || !Number.isInteger(e.port) || e.port < 1 || e.port > 65535)) {
          throw new Error('Invalid endpoint allowlist');
        }
        endpoints = items.map(e => ({ host: e.host, port: e.port }));
        // Configuration replacement revokes all old handles, including pending I/O.
        for (const id of [...listeners.keys()]) closeListener(id);
        for (const c of [...connections.values()]) dispose(c);
      },
      selfCheck: async () => {
        let listener, outgoing, incoming, deadline;
        const cleanup = () => {
          if (listener) closeListener(listener.id);
          if (outgoing) dispose(outgoing);
          if (incoming) { const c = connections.get(incoming); if (c) dispose(c); }
        };
        try {
          return await Promise.race([(async () => {
            listener = await api.listen();
            const service = Cc['@mozilla.org/network/socket-transport-service;1']
              .getService(Ci.nsISocketTransportService);
            // Only this internal probe may bypass saved endpoints: literal loopback,
            // the just-created private listener, never an addon-supplied destination.
            outgoing = adopt(service.createTransport([], '127.0.0.1', listener.port, null, null));
            incoming = await api.accept(listener.id);
            const readExact = async (id, expected) => {
              let data = '';
              while (data.length < expected.length) {
                const part = await api.read(id);
                if (!part || data.length + part.length > expected.length) throw new Error('Capability byte mismatch');
                data += part;
              }
              if (data !== expected) throw new Error('Capability byte mismatch');
            };
            const sample = '\x00\x80\xffPortmap\r\n';
            await api.write(outgoing.id, sample);
            await readExact(incoming, sample);
            await api.write(incoming, sample);
            await readExact(outgoing.id, sample);
            return true;
          })(), new Promise((_, reject) => {
            deadline = timer(3000, () => { cleanup(); reject(new Error('Local capability check timed out')); });
          })]);
        } finally { deadline?.cancel(); cleanup(); }
      },
      connect: async (host, port) => {
        const allowed = endpoints;
        if (stopped || !allowed.some(e => e.host === host && e.port === port)) {
          throw new Error('Destination is not an enabled saved endpoint');
        }
        const service = Cc['@mozilla.org/network/socket-transport-service;1']
          .getService(Ci.nsISocketTransportService);
        // Install readiness after opening streams: asyncWait also reports an
        // already-connected socket, unlike a status sink installed too late.
        const c = adopt(service.createTransport([], host, port, null, null));
        return new Promise((resolve, reject) => {
          const deadline = timer(15000, () => dispose(c, new Error('Socket connect timeout')));
          const finish = error => {
            if (!c.connecting) return;
            c.connecting = null;
            deadline.cancel();
            error ? reject(error) : resolve(c.id);
          };
          c.connecting = finish;
          try {
            c.output.asyncWait({ onOutputStreamReady() {
              if (c.closed) return;
              try { c.output.streamStatus(); finish(); }
              catch (error) { dispose(c, error); }
            } }, 0, 0, thread);
          } catch (error) { dispose(c, error); }
        });
      },
      read: async id => {
        const c = get(id);

        if (c.reading) throw new Error('Only one pending read is allowed');
        if (c.eof) return '';
        return new Promise((resolve, reject) => {
          const deadline = timer(120000, () => dispose(c, new Error('Socket read timeout')));
          const finish = (error, data) => {
            if (!c.reading) return;
            c.reading = null;
            deadline.cancel();
            error ? reject(error) : resolve(data);
          };
          c.reading = finish;
          const callback = { onInputStreamReady() {
            if (c.closed) return;

            try {
              const count = Math.min(c.input.available(), CHUNK);
              if (!count) { c.eof = true; finish(null, ''); }
              else {
                const bytes = c.binary.readBytes(count);

                finish(null, bytes);
              }
            } catch (error) {
              if (isCode(error, Cr.NS_BASE_STREAM_CLOSED)) { c.eof = true; finish(null, ''); }
              else if (isCode(error, Cr.NS_BASE_STREAM_WOULD_BLOCK)) {
                try { c.input.asyncWait(callback, 0, 0, thread); }
                catch (failure) { dispose(c, failure); }
              } else dispose(c, error);
            }
          } };
          try { c.input.asyncWait(callback, 0, 0, thread); }
          catch (error) { dispose(c, error); }
        });
      },
      write: async (id, data) => {
        const c = get(id);
        if (data.length > CHUNK || c.queued + data.length > MAX_QUEUED || c.writes.length >= 64) {
          throw new Error('Socket write queue limit');
        }
        for (let i = 0; i < data.length; i++) {
          if (data.charCodeAt(i) > 255) throw new Error('Expected a raw byte string');
        }
        if (!data.length) return;
        return new Promise((resolve, reject) => {
          const source = Cc['@mozilla.org/io/string-input-stream;1'].createInstance(Ci.nsIStringInputStream);
          source.setByteStringData(data);
          const job = { source, remaining: data.length, length: data.length, resolve, reject };
          job.timer = timer(30000, () => dispose(c, new Error('Socket write timeout')));
          c.queued += data.length;
          c.writes.push(job);
          if (c.writes.length === 1) pumpWrites(c);
        });
      },
      close: async id => { const c = connections.get(id); if (c) dispose(c); },
      listen: async () => {
        if (stopped || listeners.size >= 257) throw new Error('Listener limit or shutdown');
        const socket = Cc['@mozilla.org/network/server-socket;1'].createInstance(Ci.nsIServerSocket);
        const l = { id: nextId++, socket, closed: false, pending: null, queue: [] };
        try {
          socket.init(-1, true, 16); // IPv4 loopback only; never wildcard.
          if (socket.port < 1024) throw new Error('Expected unprivileged listener port');
          listeners.set(l.id, l);
          socket.asyncListen({
            onSocketAccepted(server, transport) {

              if (l.closed || l.queue.length >= 16) { transport.close(Cr.NS_OK); return; }
              try {
                const c = adopt(transport);

                if (l.pending) l.pending(null, c.id);
                else l.queue.push(c);
              } catch (error) {


                try { transport.close(Cr.NS_OK); } catch (_) {}
              }
            },
            onStopListening() { closeListener(l.id); },
          });
          return { id: l.id, host: '127.0.0.1', port: socket.port };
        } catch (error) {
          closeListener(l.id);
          try { socket.close(); } catch (_) {}
          throw error;
        }
      },
      accept: async id => {
        const l = listeners.get(id);
        if (!l || l.closed) throw new Error('Unknown or closed listener');
        if (l.pending) throw new Error('Only one pending accept is allowed');
        if (l.queue.length) return l.queue.shift().id;
        return new Promise((resolve, reject) => {
          const deadline = timer(60000, () => l.pending?.(new Error('Listener accept timeout')));
          l.pending = (error, connectionId) => {
            deadline.cancel();
            l.pending = null;
            error ? reject(error) : resolve(connectionId);
          };
        });
      },
      closeListener: async id => closeListener(id),
    };
    return { portmapTransport: api };
  }
  onShutdown() { this.shutdown?.(); }
};
