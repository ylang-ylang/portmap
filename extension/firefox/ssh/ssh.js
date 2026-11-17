/* global NightlySshWasm, browser */
/* Real libssh2 SSH over the Experiment's raw socket. No direct fallback. */
(() => {
  'use strict';
  const AGAIN = -37;
  const LIMIT = 262144;
  const CHUNK = 65536;
  const TIMEOUT = 30000;
  const sockets = new Map();
  let nextId = 1;
  let library;
  const text = new TextEncoder();

  function bytes(value) {
    if (typeof value !== 'string') throw new TypeError('Expected byte string');
    const data = new Uint8Array(value.length);
    for (let i = 0; i < value.length; i++) {
      const code = value.charCodeAt(i);
      if (code > 255) throw new TypeError('Non-byte string');
      data[i] = code;
    }
    return data;
  }
  function raw(data) {
    let result = '';
    for (let i = 0; i < data.length; i += 8192)
      result += String.fromCharCode(...data.subarray(i, i + 8192));
    return result;
  }
  function load() {
    if (!library) {
      if (typeof globalThis.NightlySshWasm !== 'function') throw new Error('SSH WASM artifact not preloaded');
      library = NightlySshWasm({
        locateFile: name => browser.runtime.getURL('ssh/' + name),
        print: () => {},
        printErr: () => {},
      }).then(module => {
        module.io = {
          send: (id, data) => sockets.get(id)?.send(data) ?? -1,
          recv: (id, data) => sockets.get(id)?.recv(data) ?? -1,
        };
        return module;
      });
    }
    return library;
  }
 
  function validString(value, name) {
    if (typeof value !== 'string' || value.includes('\0')) throw new TypeError('Invalid ' + name);
    return value;
  }
  function port(value) {
    if (!Number.isInteger(value) || value < 1 || value > 65535) throw new TypeError('Invalid port');
    return value;
  }

  class Connection {
    constructor(module, native, socket) {
      this.m = module;
      this.native = native;
      this.socket = socket;
      this.id = nextId++;
      this.input = [];
      this.inputSize = 0;
      this.inputOffset = 0;
      this.output = [];
      this.outputSize = 0;
      this.flushing = false;
      this.ended = false;
      this.closed = false;
      this.waiters = new Set();
      this.channels = new Set();
      this.serial = Promise.resolve();
      this.epoch = 0;
      sockets.set(this.id, this);
      this.session = module._probe_create(this.id);
      if (!this.session) {
        sockets.delete(this.id);
        throw new Error('SSH initialization failed');
      }
      this.readTask = this.receive();
    }
    pulse() {
      this.epoch++;
      for (const wake of [...this.waiters]) wake();
    }
    async wait(epoch, deadline) {
      if (this.closed) throw this.failure || new Error('SSH session closed');
      if (Date.now() >= deadline) throw new Error('SSH operation timed out');
      if (this.epoch !== epoch) return;
      await new Promise(resolve => {
        const wake = () => { clearTimeout(timer); this.waiters.delete(wake); resolve(); };
        // Also retry libssh2's already-decrypted cross-channel packet buffers.
        const timer = setTimeout(wake, Math.min(25, deadline - Date.now()));
        this.waiters.add(wake);
      });
      if (this.closed) throw this.failure || new Error('SSH session closed');
    }
    send(data) {
      if (this.closed) return -1;
      const count = Math.min(data.length, CHUNK, LIMIT - this.outputSize);
      if (!count) return -2;
      this.output.push(raw(data.subarray(0, count)));
      this.outputSize += count;
      // Never call an asynchronous native API from a WASM callback stack.
      queueMicrotask(() => this.flush());
      return count;
    }
    recv(target) {
      if (this.closed) return -1;
      if (!this.inputSize) return this.ended ? 0 : -2;
      let count = 0;
      while (count < target.length && this.input.length) {
        const first = this.input[0];
        const size = Math.min(first.length - this.inputOffset, target.length - count);
        target.set(first.subarray(this.inputOffset, this.inputOffset + size), count);
        count += size;
        this.inputOffset += size;
        this.inputSize -= size;
        if (this.inputOffset === first.length) { this.input.shift(); this.inputOffset = 0; }
      }
      this.pulse();
      return count;
    }
    async receive() {
      try {
        while (!this.closed && !this.ended) {
          while (this.inputSize > LIMIT - CHUNK && !this.closed)
            await this.wait(this.epoch, Infinity);
          if (this.closed) break;
          const data = await this.native.read(this.socket);
          if (this.closed) break;
          if (data === '') this.ended = true;
          else {
            if (data.length > CHUNK) throw new Error('Native read exceeded chunk bound');
            const buffer = bytes(data);
            this.input.push(buffer);
            this.inputSize += buffer.length;
          }
          this.pulse();
        }
      } catch (error) {
        if (!this.closed) await this.close(error);
      }
    }
    async flush() {
      if (this.flushing || this.closed) return;
      this.flushing = true;
      try {
        while (this.output.length && !this.closed) {
          const data = this.output[0];
          await this.native.write(this.socket, data);
          if (this.closed) break;
          this.output.shift();
          this.outputSize -= data.length;
          this.pulse();
        }
      } catch (error) {
        if (!this.closed) await this.close(error);
      } finally { this.flushing = false; }
    }
    alloc(data) {
      const pointer = this.m._malloc(data.length + 1);
      if (!pointer) throw new Error('SSH memory allocation failed');
      this.m.HEAPU8.set(data, pointer);
      this.m.HEAPU8[pointer + data.length] = 0;
      return {pointer, length: data.length + 1};
    }
    string(value) { return this.alloc(text.encode(value)); }
    release(item) {
      this.m.HEAPU8.fill(0, item.pointer, item.pointer + item.length);
      this.m._free(item.pointer);
    }
    async retry(step, deadline = Date.now() + TIMEOUT) {
      while (!this.closed) {
        const epoch = this.epoch;
        const result = step();
        if (result !== AGAIN) return result;
        try { await this.wait(epoch, deadline); }
        catch (error) {
          // An abandoned nonblocking operation can retain libssh2 retry state.
          // Make the whole session terminal before releasing its input buffer.
          if (!this.closed) void this.close(error);
          throw error;
        }
      }
      throw this.failure || new Error('SSH session closed');
    }
    exclusive(operation) {
      const result = this.serial.then(() => {
        if (this.closed) throw this.failure || new Error('SSH session closed');
        return operation();
      });
      // Failure must not poison the queue; connect/open callers still see it.
      this.serial = result.catch(() => {});
      return result;
    }
    async authenticate(config) {
      const m = this.m;
      const result = await this.retry(() => m._probe_handshake(this.session));
      if (this.closed) throw this.failure || new Error('SSH session closed');
      if (result) throw new Error('SSH handshake failed (' + result + ')');
      const digest = m._probe_fingerprint(this.session);
      if (!digest) throw new Error('SSH host SHA256 unavailable');
      const actual = [...m.HEAPU8.subarray(digest, digest + 32)].map(n => n.toString(16).padStart(2, '0')).join('');
      // Authentication is deliberately unreachable until the exact pin matches.
      if (actual !== config.hostKeySha256Hex.toLowerCase()) throw new Error('SSH host key mismatch');
      this.metadata = Object.freeze({hostKeySha256Hex: actual, authMethod: config.auth.type});
      const allocated = [];
      const hold = value => { const item = this.string(value); allocated.push(item); return item.pointer; };
      try {
        const user = hold(config.username);
        let auth;
        if (config.auth.type === 'password') {
          const password = hold(config.auth.password);
          auth = await this.retry(() => m._probe_password(this.session, user, password));
        } else {
          const pub = hold(config.auth.publicKey);
          const priv = hold(config.auth.privateKey);
          const pass = hold(config.auth.passphrase || '');
          auth = await this.retry(() => m._probe_key(this.session, user, pub, priv, pass));
        }
        if (auth) throw new Error('SSH authentication failed (' + auth + ')');
      } finally { for (const item of allocated) this.release(item); }
    }
    async open(host, targetPort) {
      validString(host, 'channel host'); port(targetPort);
      if (!host || host.length > 253) throw new TypeError('Invalid channel host');
      return this.exclusive(async () => {
        if (this.channels.size >= 32) throw new Error('SSH channel limit reached');
        const address = this.string(host);
        try {
          let channel = 0;
          const result = await this.retry(() => {
            channel = this.m._probe_open(this.session, address.pointer, targetPort);
            return channel ? 0 : this.m._probe_errno(this.session);
          });
          if (result || !channel) throw new Error('SSH direct-tcpip rejected (' + result + ')');
          const stream = new Stream(this, channel);
          this.channels.add(stream);
          this.pulse();
          return stream;
        } finally { this.release(address); }
      });
    }
    close(error) {
      if (this.closeTask) return this.closeTask;
      this.closed = true;
      this.failure = error;
      this.pulse();
      this.closeTask = (async () => {
        // Native close wakes I/O; it does NOT itself make libssh2_free safe:
        // an in-flight encrypted packet can reject a different CLOSE packet.
        try { await this.native.close(this.socket); } catch (_) {}
        await this.serial;
        for (const stream of this.channels) stream.dead = true;
        await Promise.all([...this.channels].map(stream => stream.readCompletion));
        // Never await receive(): its error handler can be awaiting this close.
        if (this.session) {
          const result = this.m._probe_abort_free(this.session);
          if (result !== 0) throw new Error('SSH terminal free failed (' + result + ')');
          this.session = 0;
        }
        this.channels.clear();
        sockets.delete(this.id);
        this.input.length = 0; this.output.length = 0;
        this.inputSize = this.outputSize = 0;
      })().catch(error => {
        // Keep the pointer/map/queues on unexpected failure, permitting an
        // explicit close retry rather than silently dropping owned resources.
        this.closeTask = null;
        throw error;
      });
      return this.closeTask;
    }
  }

  class Stream {
    constructor(connection, channel) {
      this.c = connection;
      this.channel = channel;
      this.dead = false;
      this.reading = false;
      this.readCompletion = Promise.resolve();
      this.writeQueue = Promise.resolve();
    }
    read() { return this.readInner(false); }
    async readInner(closing) {
      if (this.reading) throw new Error('Only one pending SSH channel read is allowed');
      if (this.dead && !closing) return '';
      if (this.c.closed) throw this.c.failure || new Error('SSH session closed');
      this.reading = true;
      let finished;
      this.readCompletion = new Promise(resolve => { finished = resolve; });
      let buffer;
      try {
        buffer = this.c.alloc(new Uint8Array(CHUNK));
        const result = await this.c.retry(() => {
          // A receive-only EAGAIN is cancellable; a pending WINDOW_ADJUST is
          // not. Resume that same libssh2 read until its send state drains.
          if (this.dead && this.c.m._probe_read_cancel_safe(this.channel)) return 0;
          const count = this.c.m._probe_read(this.channel, buffer.pointer, CHUNK);
          if (this.dead && count >= 0)
            return this.c.m._probe_read_cancel_safe(this.channel) ? 0 : AGAIN;
          if (count === 0 && !this.c.m._probe_eof(this.channel)) return AGAIN;
          return count;
        });
        if (result < 0) {
          const error = new Error('SSH channel read failed (' + result + ')');
          if (!this.c.closed) void this.c.close(error);
          throw error;
        }
        return raw(this.c.m.HEAPU8.subarray(buffer.pointer, buffer.pointer + result));
      } finally {
        if (buffer) this.c.release(buffer);
        this.reading = false;
        finished();
      }
    }
    write(value) {
      // Bound each call and snapshot before asynchronous scheduling.
      const data = bytes(value);
      if (data.length > LIMIT) return Promise.reject(new Error('SSH write exceeds 256KiB bound'));
      if (this.dead || this.c.closed) return Promise.reject(new Error('SSH channel closed'));
      if ((this.pendingBytes || 0) + data.length > LIMIT || (this.pendingWrites || 0) >= 16)
        return Promise.reject(new Error('SSH pending write queue limit reached'));
      this.pendingBytes = (this.pendingBytes || 0) + data.length;
      this.pendingWrites = (this.pendingWrites || 0) + 1;
      const result = this.writeQueue.then(() => this.c.exclusive(async () => {
        if (this.dead) throw new Error('SSH channel closed');
        // libssh2 requires the same pointer/length when retrying EAGAIN.
        const buffer = this.c.alloc(data);
        const deadline = Date.now() + TIMEOUT;
        let offset = 0;
        try {
          while (offset < data.length) {
            if (this.dead) throw new Error('SSH channel closed');
            const count = await this.c.retry(() => this.c.m._probe_write(this.channel, buffer.pointer + offset, data.length - offset), deadline);
            if (count <= 0) {
              const error = new Error('SSH channel write failed (' + count + ')');
              if (!this.c.closed) void this.c.close(error);
              throw error;
            }
            offset += count;
            this.c.pulse();
          }
        } finally { this.c.release(buffer); }
      })).finally(() => { this.pendingBytes -= data.length; this.pendingWrites--; });
      this.writeQueue = result.catch(() => {});
      return result;
    }
    close() {
      if (this.closeTask) return this.closeTask;
      this.dead = true;
      this.c.pulse();
      this.closeTask = (async () => {
        // Do not occupy the exclusive queue while a read drains a packet that
        // could belong to an earlier queued write/open operation.
        await this.readCompletion;
        // Even a completed buffered read may have dispatched transport work.
        // Drain it too before attempting a different channel CLOSE packet.
        if (!this.c.closed && !this.c.m._probe_read_cancel_safe(this.channel))
          await this.readInner(true);
        return this.c.exclusive(async () => {
          const result = await this.c.retry(() => this.c.m._probe_close_channel(this.channel));
          if (result < 0) throw new Error('SSH channel close failed (' + result + ')');
          const freed = await this.c.retry(() => this.c.m._probe_free_channel(this.channel));
          if (freed < 0) throw new Error('SSH channel free failed (' + freed + ')');
          this.c.channels.delete(this);
          this.c.pulse();
        });
      })().catch(async error => {
        if (!this.c.closed) await this.c.close(error);
        throw error;
      });
      return this.closeTask;
    }
  }

  globalThis.PortmapSSH = Object.freeze({
    async connect(config, nativeApi, { signal } = {}) {
      validString(config.host, 'host'); port(config.port);
      validString(config.username, 'username');
      if (!/^[a-f0-9]{64}$/i.test(config.hostKeySha256Hex)) throw new TypeError('Exact SHA256 host-key hex pin required');
      if (config.auth?.type === 'password') validString(config.auth.password, 'password');
      else if (config.auth?.type === 'key') {
        validString(config.auth.publicKey, 'public key'); validString(config.auth.privateKey, 'private key');
        validString(config.auth.passphrase || '', 'passphrase');
      } else throw new TypeError('Unsupported SSH authentication method');
      const m = await load();
      if (signal?.aborted) throw new Error('SSH connection cancelled');
      const socket = await nativeApi.connect(config.host, config.port);
      let connection;
      const abort = () => { if (connection) void connection.close(new Error('SSH connection cancelled')).catch(() => {}); };
      signal?.addEventListener('abort', abort, { once: true });
      try {
        if (signal?.aborted) throw new Error('SSH connection cancelled');
        connection = new Connection(m, nativeApi, socket);
        await connection.authenticate(config);
        if (signal?.aborted || connection.closed) throw new Error('SSH connection cancelled');
        return Object.freeze({
          get closed() { return connection.closed; },
          metadata: connection.metadata,
          open: (host, targetPort) => connection.open(host, targetPort),
          close: () => connection.close(),
        });
      } catch (error) {
        if (connection) await connection.close(error);
        else { try { await nativeApi.close(socket); } catch (_) {} }
        throw error;
      } finally { signal?.removeEventListener('abort', abort); }
    },
  });
})();
