import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {generateKeyPairSync, webcrypto} from 'node:crypto';
import net from 'node:net';
import vm from 'node:vm';

const source = readFileSync(new URL('../extension/firefox/ssh/ssh.js', import.meta.url), 'utf8');
const pin = 'ab'.repeat(32);
const config = {host: 'ssh-target', port: 2222, username: 'probe', hostKeySha256Hex: pin,
  auth: {type: 'password', password: 'fixture-only'}};
const checked = [];
function keyPair() {
  const pair = generateKeyPairSync('rsa', {modulusLength: 3072});
  const jwk = pair.publicKey.export({format: 'jwk'});
  const field = value => {
    const length = Buffer.alloc(4); length.writeUInt32BE(value.length);
    return Buffer.concat([length, value]);
  };
  const mpint = value => {
    const bytes = Buffer.from(value, 'base64url');
    return field(bytes[0] & 128 ? Buffer.concat([Buffer.from([0]), bytes]) : bytes);
  };
  return {privateKey: pair.privateKey.export({type: 'pkcs1', format: 'pem'}).toString(),
    publicKey: 'ssh-rsa ' + Buffer.concat([field(Buffer.from('ssh-rsa')), mpint(jwk.e), mpint(jwk.n)]).toString('base64') + " portmap fixture '$(not-a-command)'"};
}
function context(factory) {
  const world = vm.createContext({TextEncoder, Uint8Array, DataView, Date, Math, Map, Set, Promise,
    TypeError, Error, Object, String, Infinity, atob, btoa, setTimeout, clearTimeout, queueMicrotask,
    crypto: webcrypto, NightlySshWasm: factory,
    browser: {runtime: {getURL: name => name}}});
  vm.runInContext(source, world);
  return world.PortmapSSH;
}
function stub(settings = {}) {
  let next = 128, outputOffset = 0, errorOffset = 0;
  const events = {password: 0, key: 0, exec: 0, freed: 0, closed: 0, opened: 0};
  const heap = new Uint8Array(1048576);
  heap.fill(171, 32, 64);
  const readOutput = (stderr, pointer, length) => {
    const value = stderr ? settings.stderr || '' : settings.stdout ?? 'PORTMAP_KEY_INSTALLED\n';
    const offset = stderr ? errorOffset : outputOffset;
    const data = Buffer.from(value.slice(offset, offset + length));
    heap.set(data, pointer);
    if (stderr) errorOffset += data.length; else outputOffset += data.length;
    return data.length;
  };
  const module = {HEAPU8: heap, _malloc: length => {const result = next; next += length; return result;},
    _free: () => {}, _probe_create: () => 1,
    _probe_handshake: () => settings.handshakeAgain ? -37 : 0, _probe_fingerprint: () => 32,
    _probe_password: () => {events.password++; return 0;}, _probe_key: () => {events.key++; return 0;},
    _probe_open_session: () => {events.opened++; return settings.openDenied ? 0 : 2;},
    _probe_errno: () => -21,
    _probe_exec: (_channel, pointer) => {
      events.exec++;
      if (settings.execAgain) return -37;
      const end = heap.indexOf(0, pointer);
      events.command = new TextDecoder().decode(heap.subarray(pointer, end));
      return settings.execDenied ? -22 : 0;
    },
    _probe_send_eof: () => 0, _probe_read: (_c, p, n) => readOutput(false, p, n),
    _probe_read_stderr: (_c, p, n) => readOutput(true, p, n), _probe_read_cancel_safe: () => 1,
    _probe_eof: () => 1, _probe_close_channel: () => 0, _probe_free_channel: () => 0,
    _probe_exit_status: () => settings.status || 0, _probe_exit_signal: () => settings.signal || 0,
    _probe_abort_free: () => {events.freed++; return 0;}};
  let readWake;
  const native = {connect: async () => 7,
    read: () => new Promise(resolve => {readWake = resolve;}), write: async () => {},
    close: async () => {events.closed++; readWake?.('');}};
  return {ssh: context(async () => module), native, events, module};
}
async function unit() {
  const pair = keyPair();
  {
    const s = stub();
    const result = await s.ssh.inspectHost(config, s.native);
    assert.equal(result.hostKeySha256Hex, pin);
    assert.equal(s.events.password + s.events.key + s.events.exec, 0);
    assert.equal(s.events.closed, 1); assert.equal(s.events.freed, 1);
    checked.push('handshake-only inspection closes without authentication');
  }
  {
    const s = stub();
    await assert.rejects(s.ssh.connect({...config, hostKeySha256Hex: '00'.repeat(32)}, s.native), /host key mismatch/);
    assert.equal(s.events.password + s.events.key, 0);
    assert.equal(s.events.freed, 1);
    checked.push('host pin precedes all credential calls');
  }
  {
    const s = stub({stderr: 'bounded warning'}), session = await s.ssh.connect(config, s.native);
    assert.equal(session.exec, undefined);
    for (const invalid of [pair.publicKey + '\n', 'ssh-rsa AAAA', pair.publicKey.replace('ssh-rsa', 'ssh-ed25519'),
      pair.publicKey.replace(' ', '\t'), pair.publicKey + '\n$(touch /tmp/unsafe)', 'command="bad" ' + pair.publicKey]) {
      assert.throws(() => session.installPublicKey(invalid), /canonical single-line/);
    }
    assert.equal(s.events.exec, 0);
    assert.equal((await session.installPublicKey(pair.publicKey)).installed, true);
    assert(s.events.command.startsWith('python3 -I -c '));
    assert(s.events.command.includes('O_NOFOLLOW'));
    await session.close();
    assert.equal(s.events.freed, 1);
    checked.push('canonical RSA validation and fixed installer only');
  }
  for (const [settings, expected] of [[{openDenied: true}, /command sessions are unavailable/],
    [{execDenied: true}, /execution was rejected/], [{status: 64}, /symlink, hard link/],
    [{status: 127}, /Python 3 is required/], [{status: 1}, /exit 1/],
    [{signal: 1}, /terminated/], [{stdout: ''}, /not confirmed/],
    [{stderr: 'x'.repeat(17000)}, /safety bound/]]) {
    const s = stub(settings), session = await s.ssh.connect(config, s.native);
    await assert.rejects(session.installPublicKey(pair.publicKey), expected);
    await session.close(); assert.equal(s.events.closed, 1); assert.equal(s.events.freed, 1);
  }
  checked.push('exec rejection, unavailable utility, exit status, signal, and output limits are honest');
  for (const stage of ['handshakeAgain', 'execAgain']) {
    const s = stub({[stage]: true}), abort = new AbortController();
    let session;
    const task = stage === 'handshakeAgain' ? s.ssh.inspectHost(config, s.native, {signal: abort.signal}) :
      (session = await s.ssh.connect(config, s.native, {signal: abort.signal}), session.installPublicKey(pair.publicKey));
    setTimeout(() => abort.abort(), 10);
    await assert.rejects(task, /cancelled/);
    if (session) await session.close();
    assert.equal(s.events.closed, 1); assert.equal(s.events.freed, 1);
  }
  checked.push('EAGAIN handshake and authenticated exec cancellation quiesce and free');
  {
    const s = stub(), abort = new AbortController();
    let finish;
    s.native.connect = () => new Promise(resolve => {finish = resolve;});
    const opening = s.ssh.inspectHost(config, s.native, {signal: abort.signal});
    await new Promise(resolve => setTimeout(resolve, 5));
    abort.abort(); await assert.rejects(opening, /cancelled/);
    finish(7); await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(s.events.closed, 1); assert.equal(s.events.freed, 0);
    checked.push('cancelled pending native connect closes its late socket handle');
  }
}
function tcpNative() {
  const handles = new Map(); let next = 0, closed = 0;
  const api = {handles, pauseWrites: false, get closedCount() {return closed;},
    connect: (host, port) => new Promise((resolve, reject) => {
      const socket = net.connect({host, port}), handle = {socket, queue: [], waiter: null, ended: false, heldWrites: []};
      const id = ++next;
      socket.once('error', reject);
      socket.once('connect', () => {handles.set(id, handle); resolve(id);});
      socket.on('data', bytes => {
        for (let at = 0; at < bytes.length; at += 65536) handle.queue.push(bytes.subarray(at, at + 65536).toString('latin1'));
        handle.waiter?.();
      });
      socket.on('end', () => {handle.ended = true; handle.waiter?.();});
      socket.on('close', () => {handle.ended = true; handle.waiter?.();});
    }),
    read: async id => {
      const handle = handles.get(id);
      while (!handle.queue.length && !handle.ended) await new Promise(resolve => {handle.waiter = resolve;});
      handle.waiter = null;
      return handle.queue.shift() || '';
    },
    write: (id, data) => new Promise((resolve, reject) => {
      const handle = handles.get(id);
      if (api.pauseWrites) {handle.heldWrites.push(resolve); return;}
      handle.socket.write(Buffer.from(data, 'latin1'), error => error ? reject(error) : resolve());
    }),
    close: async id => {
      const handle = handles.get(id); if (!handle) return;
      handles.delete(id); closed++; handle.socket.destroy(); handle.ended = true;
      handle.waiter?.();
      for (const resolve of handle.heldWrites) resolve();
    },
  };
  return api;
}
async function real(input) {
  const contextReal = vm.createContext({TextEncoder, Uint8Array, DataView, Date, Math, Map, Set, Promise,
    TypeError, Error, Object, String, Infinity, atob, btoa, setTimeout, clearTimeout, queueMicrotask,
    crypto: webcrypto, console: {log() {}, warn() {}, error() {}},
    browser: {runtime: {getURL: name => name}}});
  vm.runInContext(readFileSync(new URL('../extension/firefox/ssh/libssh2.js', import.meta.url), 'utf8'), contextReal);
  const factory = contextReal.NightlySshWasm;
  let loaded;
  contextReal.NightlySshWasm = options => factory({...options,
    wasmBinary: readFileSync(new URL('../extension/firefox/ssh/libssh2.wasm', import.meta.url))})
    .then(module => {loaded = module; return module;});
  vm.runInContext(source, contextReal);
  const ssh = contextReal.PortmapSSH, native = tcpNative();
  const pair = {...input.candidate,
    publicKey: input.candidate.publicKey.split(' ').slice(0, 2).join(' ') + " portmap fixture '$(not-a-command)'"};
  const endpoint = {host: 'ssh-target', port: 2222};
  const pinned = {...endpoint, username: input.username, hostKeySha256Hex: input.hostKeySha256Hex,
    auth: {type: 'password', password: input.password}};
  try {
    assert.equal((await ssh.inspectHost(endpoint, native)).hostKeySha256Hex, input.hostKeySha256Hex);
    assert.equal(native.handles.size, 0);
    if (input.expected) {
      const session = await ssh.connect(pinned, native);
      try {await assert.rejects(session.installPublicKey(pair.publicKey), new RegExp(input.expected));}
      finally {await session.close();}
      checked.push('ordinary sshd rejects unsafe/unsupported install: ' + input.case);
    } else if (input.restrictedDuplicate) {
      const session = await ssh.connect(pinned, native);
      try {assert.equal((await session.installPublicKey(pair.publicKey)).installed, true);}
      finally {await session.close();}
      await assert.rejects(ssh.connect({...pinned, auth: {type: 'key', ...pair}}, native), /authentication failed/);
      checked.push('existing restricted key is not duplicated or weakened; new key-auth honestly fails');
    } else {
      await assert.rejects(ssh.connect({...pinned, hostKeySha256Hex: '00'.repeat(32)}, native), /host key mismatch/);
      const sessions = await Promise.all([ssh.connect(pinned, native), ssh.connect(pinned, native)]);
      try {
        const installed = await Promise.all(sessions.map(session => session.installPublicKey(pair.publicKey)));
        assert(installed.every(result => result.installed));
        assert.equal((await sessions[0].installPublicKey(pair.publicKey)).installed, true);
      } finally {await Promise.all(sessions.map(session => session.close()));}
      const fresh = await ssh.connect({...pinned, auth: {type: 'key', ...pair}}, native);
      assert.equal(fresh.metadata.authMethod, 'key');
      const stream = await fresh.open('127.0.0.1', 8080);
      await stream.write('GET /health HTTP/1.0\r\nHost: fixture\r\n\r\n');
      let reply = '';
      while (true) {const chunk = await stream.read(); if (!chunk) break; reply += chunk;}
      assert(reply.includes('200 OK')); await stream.close(); await fresh.close();
      checked.push('ordinary sshd concurrent/repeated installer then NEW pinned RSA key-auth and direct-tcpip');
      const abort = new AbortController();
      const cancelling = await ssh.connect(pinned, native, {signal: abort.signal});
      const originalExec = loaded._probe_exec;
      let sawExecAgain = false, abortAt = 0;
      const watchdog = setTimeout(() => abort.abort(), 5000);
      loaded._probe_exec = (...args) => {
        native.pauseWrites = true;
        const result = originalExec(...args);
        if (result === -37 && !sawExecAgain) {
          sawExecAgain = true;
          setTimeout(() => {abortAt = Date.now(); abort.abort();}, 20);
        }
        return result;
      };
      try {
        const installing = cancelling.installPublicKey(pair.publicKey);
        await assert.rejects(installing, /cancelled/);
        await cancelling.close();
        assert(sawExecAgain);
        assert(Date.now() - abortAt < 2000);
      } finally {clearTimeout(watchdog); loaded._probe_exec = originalExec; native.pauseWrites = false;}
      const afterAbort = await ssh.connect({...pinned, auth: {type: 'key', ...pair}}, native);
      await afterAbort.close();
      checked.push('actual WASM pending-exec EAGAIN abort closes/frees and leaves later pinned key-auth usable');
    }
    assert.equal(native.handles.size, 0);
  } finally {await Promise.all([...native.handles.keys()].map(id => native.close(id)));}
}
try {
  if (process.argv[2] === 'real') {
    let input = ''; for await (const chunk of process.stdin) input += chunk;
    await real(JSON.parse(input));
  } else await unit();
  console.log(JSON.stringify({checks: checked, count: checked.length}));
} catch (error) {
  // Never dump configuration, command source, public/private keys or frames.
  console.error(error.name + ': ' + error.message);
  process.exitCode = 1;
}
