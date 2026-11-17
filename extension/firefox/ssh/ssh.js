/* global NightlySshWasm, browser */
/* Real libssh2 SSH over the Experiment's raw socket. No direct fallback. */
(() => {
  'use strict';
  const AGAIN = -37;
  const LIMIT = 262144;
  const CHUNK = 65536;
  const TIMEOUT = 30000;
  const INSTALL_OUTPUT_LIMIT = 16384;
  const INSTALL_OK = 'PORTMAP_KEY_INSTALLED\n';
  // Python's fd-relative, no-follow opens avoid shell redirection's symlink
  // race. No server configuration, alternate path, or command is caller-owned.
  const INSTALLER = String.raw`import base64, fcntl, os, pwd, shlex, stat, sys, time
def fail(code):
    raise SystemExit(code)
def main():
    os.umask(0o077)
    uid = os.getuid()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    home = pwd.getpwuid(uid).pw_dir
    if not home.startswith("/") or home == "/" or any(p in (".", "..") for p in home.split("/")):
        fail(62)
    directory = os.open("/", flags)
    try:
        parts = [p for p in home.split("/") if p]
        for index, part in enumerate(parts):
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            owners = (uid,) if index == len(parts) - 1 else (0, uid)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners or info.st_mode & 0o022:
                fail(62)
        try:
            os.mkdir(".ssh", 0o700, dir_fd=directory)
        except FileExistsError:
            pass
        try:
            ssh = os.open(".ssh", flags, dir_fd=directory)
        except OSError:
            fail(63)
        try:
            info = os.fstat(ssh)
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
                fail(63)
            file_flags = os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
            try:
                keyfile = os.open("authorized_keys", file_flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=ssh)
            except FileExistsError:
                try:
                    keyfile = os.open("authorized_keys", file_flags, dir_fd=ssh)
                except OSError:
                    fail(64)
            try:
                info = os.fstat(keyfile)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != uid or info.st_nlink != 1 or info.st_mode & 0o022:
                    fail(64)
                deadline = time.monotonic() + 5
                while True:
                    try:
                        fcntl.flock(keyfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            fail(65)
                        time.sleep(0.05)
                # Recheck the directory entry after acquiring the lock. Never
                # chmod or append through a replaced name or a hard link.
                named = os.stat("authorized_keys", dir_fd=ssh, follow_symlinks=False)
                current = os.fstat(keyfile)
                if (named.st_dev, named.st_ino) != (current.st_dev, current.st_ino) or current.st_nlink != 1:
                    fail(64)
                data = bytearray()
                while len(data) <= 8388608:
                    block = os.read(keyfile, min(65536, 8388609 - len(data)))
                    if not block:
                        break
                    data.extend(block)
                if len(data) > 8388608:
                    fail(67)
                public = sys.argv[1]
                kind, encoded = public.split(" ", 2)[:2]
                blob = base64.b64decode(encoded, validate=True)
                found = False
                for line in data.decode("utf-8", "surrogateescape").splitlines():
                    if not line.strip() or line.lstrip().startswith("#"):
                        continue
                    try:
                        fields = shlex.shlex(line, posix=True)
                        fields.whitespace_split = True
                        fields.commenters = ""
                        first = next(fields, None)
                        if first != kind and next(fields, None) != kind:
                            continue
                        # Comments are arbitrary bytes, not shell syntax. Stop
                        # lexing at the key blob, even if a comment has quotes.
                        existing = next(fields, None)
                    except ValueError:
                        continue
                    if not existing:
                        continue
                    try:
                        found = base64.b64decode(existing + "=" * (-len(existing) % 4), validate=True) == blob
                    except ValueError:
                        continue
                    if found:
                        break
                os.fchmod(ssh, 0o700)
                os.fchmod(keyfile, 0o600)
                if not found:
                    # Append only: preserve every existing byte, even a last
                    # comment/key without LF. O_APPEND + flock serialize us.
                    suffix = (b"\n" if data and data[-1] != 10 else b"") + public.encode("ascii") + b"\n"
                    while suffix:
                        count = os.write(keyfile, suffix)
                        if count <= 0:
                            fail(66)
                        suffix = suffix[count:]
                os.fsync(keyfile)
                os.fsync(ssh)
            finally:
                os.close(keyfile)
        finally:
            os.close(ssh)
    finally:
        os.close(directory)
    sys.stdout.write("PORTMAP_KEY_INSTALLED\n")
try:
    main()
except (AttributeError, ImportError, NotImplementedError):
    fail(61)
except (OSError, KeyError, ValueError):
    fail(66)
`;
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

  function installerCommand(publicKey) {
    const invalid = () => { throw new TypeError('A canonical single-line OpenSSH RSA public key is required'); };
    if (typeof publicKey !== 'string' || publicKey.length > 4096) invalid();
    const match = /^ssh-rsa ([A-Za-z0-9+/]+={0,2})(?: ([\x21-\x7e][\x20-\x7e]{0,127}))?$/.exec(publicKey);
    if (!match) invalid();
    let decoded;
    try { decoded = atob(match[1]); } catch (_) { invalid(); }
    if (btoa(decoded) !== match[1]) invalid();
    const blob = bytes(decoded);
    const view = new DataView(blob.buffer);
    let offset = 0;
    const field = () => {
      if (offset + 4 > blob.length) invalid();
      const length = view.getUint32(offset); offset += 4;
      if (!length || length > blob.length - offset) invalid();
      const item = blob.subarray(offset, offset + length); offset += length;
      return item;
    };
    if (raw(field()) !== 'ssh-rsa') invalid();
    const exponent = field(), modulus = field();
    const positive = value => !(value[0] & 128) && value[0] !== 0 ||
      value.length > 1 && value[0] === 0 && (value[1] & 128);
    if (offset !== blob.length || !positive(exponent) || !positive(modulus) ||
        exponent.length > 5 || modulus.length < 257 || modulus.length > 1025) invalid();
    let e = 0;
    for (const octet of exponent) e = e * 256 + octet;
    const significant = modulus[0] === 0 ? modulus.subarray(1) : modulus;
    const bits = significant.length * 8 - Math.clz32(significant[0]) + 24;
    if (e < 3 || e > 0xffffffff || !(e % 2) || bits < 2048 || bits > 8192) invalid();
    const quote = value => "'" + value.replace(/'/g, "'\\''") + "'";
    return 'python3 -I -c ' + quote(INSTALLER) + ' ' + quote(publicKey);
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
        if (Date.now() >= deadline) {
          const error = new Error('SSH operation timed out');
          void this.close(error).catch(() => {});
          throw error;
        }
        const epoch = this.epoch;
        const result = step();
        if (result !== AGAIN) return result;
        try { await this.wait(epoch, deadline); }
        catch (error) {
          // An abandoned nonblocking operation can retain libssh2 retry state.
          // Make the whole session terminal before releasing its input buffer.
          if (!this.closed) void this.close(error).catch(() => {});
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
    async handshake() {
      const m = this.m;
      const result = await this.retry(() => m._probe_handshake(this.session));
      if (this.closed) throw this.failure || new Error('SSH session closed');
      if (result) throw new Error('SSH handshake failed (' + result + ')');
      const digest = m._probe_fingerprint(this.session);
      if (!digest) throw new Error('SSH host SHA256 unavailable');
      return [...m.HEAPU8.subarray(digest, digest + 32)].map(n => n.toString(16).padStart(2, '0')).join('');
    }
    async authenticate(config) {
      const m = this.m;
      const actual = await this.handshake();
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
    installPublicKey(publicKey) {
      const command = installerCommand(publicKey);
      if (this.installPending) return Promise.reject(new Error('SSH key installation is already running'));
      this.installPending = true;
      return this.exclusive(async () => {
        const m = this.m, deadline = Date.now() + TIMEOUT;
        const allocated = [];
        const hold = data => { const item = this.alloc(data); allocated.push(item); return item.pointer; };
        try {
          if (this.channels.size >= 32) throw new Error('SSH channel limit reached');
          let channel = 0;
          const opened = await this.retry(() => {
            channel = m._probe_open_session(this.session);
            return channel ? 0 : m._probe_errno(this.session);
          }, deadline);
          if (opened || !channel)
            throw new Error('Remote SSH command sessions are unavailable (' + opened + '); install the public key manually');
          const argument = hold(text.encode(command));
          const started = await this.retry(() => m._probe_exec(channel, argument), deadline);
          if (started)
            throw new Error('Remote SSH command execution was rejected (' + started + '); install the public key manually');
          const sent = await this.retry(() => m._probe_send_eof(channel), deadline);
          if (sent) throw new Error('SSH installer input close failed (' + sent + ')');
          const buffer = hold(new Uint8Array(4096));
          let stdout = '', total = 0;
          while (true) {
            if (Date.now() >= deadline) throw new Error('SSH key installation timed out; the remote key may already have been appended');
            const epoch = this.epoch;
            let progress = false, empty = true;
            for (const read of [m._probe_read, m._probe_read_stderr]) {
              const step = () => read(channel, buffer, 4096);
              let count = step();
              // Only switch streams after a receive-only EAGAIN. A pending
              // WINDOW_ADJUST must resume the same call and input buffer.
              if (count === AGAIN && !m._probe_read_cancel_safe(channel))
                count = await this.retry(step, deadline);
              if (count === AGAIN) { empty = false; continue; }
              if (count < 0) throw new Error('SSH installer output read failed (' + count + ')');
              if (count) {
                progress = true; empty = false; total += count;
                if (total > INSTALL_OUTPUT_LIMIT) throw new Error('SSH installer output exceeded its safety bound');
                if (read === m._probe_read) stdout += raw(m.HEAPU8.subarray(buffer, buffer + count));
              }
            }
            if (empty && m._probe_eof(channel)) break;
            if (!progress) await this.wait(epoch, deadline);
          }
          const closed = await this.retry(() => m._probe_close_channel(channel), deadline);
          if (closed) throw new Error('SSH installer channel close failed (' + closed + ')');
          const status = m._probe_exit_status(channel), signal = m._probe_exit_signal(channel);
          const freed = await this.retry(() => m._probe_free_channel(channel), deadline);
          if (freed) throw new Error('SSH installer channel free failed (' + freed + ')');
          if (signal) throw new Error('The remote key installer was terminated; the key may already have been appended');
          const failures = {
            61: 'The server needs Python 3 with Unix no-follow file and locking support; install the public key manually',
            62: 'The remote home directory has unsafe ownership, permissions, or a symlink; fix it or install the public key manually',
            63: 'The remote .ssh directory is unsafe or not a real user-owned directory; fix it or install the public key manually',
            64: 'Remote authorized_keys is a symlink, hard link, nonregular file, foreign-owned, or group/other-writable; fix it manually',
            65: 'Remote authorized_keys is busy; try again after the other installation finishes',
            66: 'The remote key installer could not safely read, append, or sync authorized_keys; check permissions and available disk space',
            67: 'Remote authorized_keys exceeds the safe installation size; install the public key manually',
            126: 'Python 3 could not be executed on the server; install the public key manually',
            127: 'Python 3 is required on the server for safe installation; install the public key manually',
          };
          if (status !== 0) throw new Error((failures[status] || 'Remote key installer failed') + ' (exit ' + status + ')');
          if (stdout !== INSTALL_OK) throw new Error('Remote key installation was not confirmed; the key may already have been appended');
          return Object.freeze({installed: true});
        } catch (error) {
          // Abandoning exec/read state is terminal, never a reusable session.
          if (!this.closed) void this.close(error).catch(() => {});
          throw error;
        } finally { for (const item of allocated) this.release(item); }
      }).finally(() => { this.installPending = false; });
    }
    close(error) {
      if (this.closeTask) return this.closeTask;
      this.closed = true;
      this.failure = error;
      this.detachAbort?.();
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

  async function openConnection(endpoint, nativeApi, signal) {
    validString(endpoint.host, 'host'); port(endpoint.port);
    if (!endpoint.host || endpoint.host.length > 253) throw new TypeError('Invalid host');
    const m = await load();
    if (signal?.aborted) throw new Error('SSH connection cancelled');
    let rejectOpening, abandoned = false, socket, connection;
    const interrupted = new Promise((_, reject) => { rejectOpening = reject; });
    const cancelOpening = () => rejectOpening(new Error('SSH connection cancelled'));
    signal?.addEventListener('abort', cancelOpening, {once: true});
    const timer = setTimeout(() => rejectOpening(new Error('SSH socket connection timed out')), TIMEOUT);
    const pending = Promise.resolve().then(() => {
      if (signal?.aborted) throw new Error('SSH connection cancelled');
      return nativeApi.connect(endpoint.host, endpoint.port);
    });
    // A late native connect still owns a handle, even after cancellation.
    pending.then(handle => {
      if (abandoned) return nativeApi.close(handle);
    }).catch(() => {});
    try {
      socket = await Promise.race([pending, interrupted]);
      if (signal?.aborted) throw new Error('SSH connection cancelled');
      connection = new Connection(m, nativeApi, socket);
      const abort = () => { void connection.close(new Error('SSH connection cancelled')).catch(() => {}); };
      connection.detachAbort = () => signal?.removeEventListener('abort', abort);
      signal?.addEventListener('abort', abort, {once: true});
      if (signal?.aborted) {
        await connection.close(new Error('SSH connection cancelled'));
        throw new Error('SSH connection cancelled');
      }
      return connection;
    } catch (error) {
      abandoned = socket === undefined;
      if (connection) await connection.close(error);
      else if (socket !== undefined) { try { await nativeApi.close(socket); } catch (_) {} }
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', cancelOpening);
    }
  }

  globalThis.PortmapSSH = Object.freeze({
    async inspectHost(endpoint, nativeApi, {signal} = {}) {
      const connection = await openConnection(endpoint, nativeApi, signal);
      try {
        const hostKeySha256Hex = await connection.exclusive(() => connection.handshake());
        if (signal?.aborted || connection.closed) throw new Error('SSH connection cancelled');
        return Object.freeze({hostKeySha256Hex});
      } finally { await connection.close(); }
    },
    async connect(config, nativeApi, { signal } = {}) {
      validString(config.host, 'host'); port(config.port);
      validString(config.username, 'username');
      if (!/^[a-f0-9]{64}$/i.test(config.hostKeySha256Hex)) throw new TypeError('Exact SHA256 host-key hex pin required');
      if (config.auth?.type === 'password') validString(config.auth.password, 'password');
      else if (config.auth?.type === 'key') {
        validString(config.auth.publicKey, 'public key'); validString(config.auth.privateKey, 'private key');
        validString(config.auth.passphrase || '', 'passphrase');
      } else throw new TypeError('Unsupported SSH authentication method');
      const connection = await openConnection(config, nativeApi, signal);
      try {
        await connection.exclusive(() => connection.authenticate(config));
        if (signal?.aborted || connection.closed) throw new Error('SSH connection cancelled');
        return Object.freeze({
          get closed() { return connection.closed; },
          metadata: connection.metadata,
          open: (host, targetPort) => connection.open(host, targetPort),
          installPublicKey: publicKey => connection.installPublicKey(publicKey),
          close: () => connection.close(),
        });
      } catch (error) {
        await connection.close(error);
        throw error;
      }
    },
  });
})();
