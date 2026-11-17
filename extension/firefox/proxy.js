/* SPDX-License-Identifier: MIT */
// Addon-owned authenticated loopback HTTP proxy; no direct fallback.
// All strings on nativeApi/openUpstream streams are raw bytes (code units 0..255).
(() => {
  'use strict';
  const HEADER = 32768;
  const CHUNK = 65536;
  const BODY = 16 * 1024 * 1024;
  const TOKEN = /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/;
  class HttpError extends Error {
    constructor(status, message) { super(message); this.status = status; }
  }
  const bad = message => { throw new HttpError(400, message); };
  const denied = () => { throw new HttpError(403, 'Destination denied'); };
  const closeQuietly = async stream => { try { await stream?.close(); } catch (_) {} };
  const limit = async (promise, ms, expire) => {
    let timer;
    try {
      return await Promise.race([promise, new Promise((_, reject) => {
        timer = setTimeout(() => { expire(); reject(new Error('Proxy operation timeout')); }, ms);
      })]);
    } finally { clearTimeout(timer); }
  };
  class Reader {
    constructor(stream) { this.stream = stream; this.buffer = ''; this.eof = false; }
    async more() {
      const bytes = await this.stream.read();
      if (typeof bytes !== 'string' || bytes.length > CHUNK) throw new Error('Invalid upstream byte chunk');
      if (!bytes.length) this.eof = true;
      this.buffer += bytes;
    }
    async head() {
      for (;;) {
        const end = this.buffer.indexOf('\r\n\r\n');
        if (end >= 0) {
          if (end + 4 > HEADER) bad('Header too large');
          const head = this.buffer.slice(0, end + 4);
          this.buffer = this.buffer.slice(end + 4);
          return head;
        }
        if (this.buffer.length >= HEADER) bad('Header too large');
        if (this.eof) bad('Incomplete header');
        await this.more();
      }
    }
    async line(max = 8192) {
      for (;;) {
        const end = this.buffer.indexOf('\r\n');
        if (end >= 0) {
          if (end > max) bad('Line too large');
          const line = this.buffer.slice(0, end);
          this.buffer = this.buffer.slice(end + 2);
          return line;
        }
        if (this.buffer.length > max || this.eof) bad('Invalid body framing');
        await this.more();
      }
    }
    async take(count = CHUNK) {
      if (!this.buffer && !this.eof) await this.more();
      const bytes = this.buffer.slice(0, count);
      this.buffer = this.buffer.slice(bytes.length);
      return bytes;
    }
  }
  const parseHead = raw => {
    const lines = raw.slice(0, -4).split('\r\n');
    const first = lines.shift();
    const headers = lines.map(line => {
      const colon = line.indexOf(':');
      if (colon < 1 || !TOKEN.test(line.slice(0, colon)) || /[\x00-\x08\x0a-\x1f\x7f]/.test(line)) {
        bad('Invalid header');
      }
      return { name: line.slice(0, colon).toLowerCase(), value: line.slice(colon + 1).trim(), raw: line };
    });
    const values = name => headers.filter(h => h.name === name).map(h => h.value);
    const one = name => {
      const v = values(name);
      if (v.length > 1) bad('Duplicate ' + name);
      return v[0];
    };
    return { first, headers, values, one };
  };
  const names = value => {
    if (!value) return [];
    const result = value.split(',').map(s => s.trim().toLowerCase());
    if (result.some(s => !TOKEN.test(s))) bad('Invalid connection options');
    return result;
  };
  const authority = (text, defaultPort, allowed) => {
    const match = /^([a-zA-Z0-9.-]+)(?::([0-9]{1,5}))?$/.exec(text);
    if (!match) denied();
    const host = match[1].toLowerCase().replace(/\.+$/, "");
    const port = match[2] === undefined ? defaultPort : Number(match[2]);
    if (!allowed(host) || (port !== 80 && port !== 443)) denied();
    return { host, port };
  };
  const same = (a, b) => a.host === b.host && a.port === b.port;
  const request = (head, allowed, authorization, tunnel) => {
    const h = parseHead(head);
    const m = /^([A-Z]+) ([^\x00-\x20\x7f]+) HTTP\/(1\.[01])$/.exec(h.first);
    if (!m) bad('Invalid request line');
    const [_, method, target, version] = m;
    if (!TOKEN.test(method)) bad('Invalid method');
    if (!tunnel && h.one('proxy-authorization') !== authorization) {
      throw new HttpError(407, 'Proxy authentication required');
    }
    let destination;
    let path = target;
    if (method === 'CONNECT') {
      if (tunnel || !/:[0-9]+$/.test(target)) denied();
      destination = authority(target, 443, allowed);
    } else if (tunnel) {
      if (!target.startsWith('/') || target.startsWith('//') || target.includes('#') || target.includes('\\')) bad('Expected origin-form');
      destination = authority(h.one('host') || '', 80, allowed);
      if (!same(destination, tunnel)) denied();
    } else {
      // Do not use URL(), which normalizes paths/escapes before forwarding.
      const absolute = /^(http|ws):\/\/([^/?#]+)([^#]*)$/.exec(target);
      if (!absolute || absolute[3].includes('\\')) denied();
      destination = authority(absolute[2], 80, allowed);
      if (destination.port !== 80) denied();
      path = absolute[3] || '/';
      if (path.startsWith('?')) path = '/' + path;
    }
    const host = h.one('host');
    if (!host || !same(authority(host, destination.port, allowed), destination)) denied();
    const length = h.one('content-length');
    const transfer = h.one('transfer-encoding');
    if (length !== undefined && (!/^(0|[1-9][0-9]*)$/.test(length) || Number(length) > BODY)) bad('Invalid content length');
    if (transfer !== undefined && (transfer.toLowerCase() !== 'chunked' || length !== undefined)) bad('Invalid transfer encoding');
    const connection = names(h.values('connection').join(','));
    if (connection.some(n => ['host', 'authorization', 'content-length', 'transfer-encoding', 'trailer', 'proxy-authorization'].includes(n))) {
      bad('Unsafe connection options');
    }
    const upgrade = h.one('upgrade');
    const websocket = method === 'GET' && upgrade?.toLowerCase() === 'websocket' && connection.includes('upgrade');
    if ((upgrade || connection.includes('upgrade')) && !websocket) bad('Unsupported upgrade');
    if (websocket && (transfer || (length !== undefined && length !== '0'))) bad('Upgrade with body');
    if (method === 'CONNECT' && (transfer || (length !== undefined && length !== '0') || upgrade)) bad('CONNECT with body');
    const expect = h.one('expect');
    if (expect && expect.toLowerCase() !== '100-continue') throw new HttpError(417, 'Unsupported expectation');
    const remove = new Set(['proxy-authorization', 'proxy-connection', 'connection', 'keep-alive', 'expect', ...connection]);
    if (websocket) remove.delete('upgrade');
    const forwarded = `${method} ${path} HTTP/${version}\r\n` +
      h.headers.filter(row => !remove.has(row.name)).map(row => row.raw + '\r\n').join('') +
      (websocket ? 'Connection: Upgrade\r\n' : 'Connection: close\r\n') + '\r\n';
    return { method, destination, forwarded, websocket, expect, chunked: !!transfer, length: Number(length || 0) };
  };
  const copyExact = async (reader, output, length) => {
    while (length) {
      const bytes = await reader.take(Math.min(CHUNK, length));
      if (!bytes) bad('Truncated request body');
      await output.write(bytes);
      length -= bytes.length;
    }
  };
  const copyBody = async (reader, output, req) => {
    if (!req.chunked) { await copyExact(reader, output, req.length); return; }
    let total = 0;
    for (;;) {
      const line = await reader.line();
      // Preserve chunk extensions, but never permit control characters.
      const m = /^([0-9a-fA-F]{1,8})(;[\x20-\x7e]*)?$/.exec(line);
      if (!m) bad('Invalid chunk');
      const size = parseInt(m[1], 16);
      total += size;
      if (total > BODY) bad('Body too large');
      await output.write(line + '\r\n');
      if (!size) {
        let count = 0;
        for (;;) {
          const trailer = await reader.line();
          count += trailer.length + 2;
          if (count > HEADER) bad('Trailers too large');
          if (trailer) {
            const colon = trailer.indexOf(':');
            if (colon <= 0 || !TOKEN.test(trailer.slice(0, colon)) || /[\x00-\x1f\x7f]/.test(trailer) ||
                /^(host|authorization|proxy-authorization|proxy-connection|connection|upgrade|content-length|transfer-encoding|trailer)$/i.test(trailer.slice(0, colon))) {
              bad('Unsafe trailer');
            }
          }
          await output.write(trailer + '\r\n');
          if (!trailer) return;
        }
      }
      await copyExact(reader, output, size);
      if (await reader.line(0) !== '') bad('Invalid chunk terminator');
      await output.write('\r\n');
    }
  };
  const copy = async (reader, output) => {
    for (;;) {
      const bytes = await reader.take();
      if (!bytes) return;
      await output.write(bytes);
    }
  };
  globalThis.PortmapProxy = {
    async start(nativeApi, openUpstream, { domain } = {}) {
      if (typeof domain !== 'string' || domain !== domain.toLowerCase() || domain.length > 253 ||
          !domain.endsWith('.portmap') || domain.split('.').some(label => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) {
        throw new Error('Invalid private namespace');
      }
      const allowed = host => host.length <= 253 && (host === domain || host.endsWith('.' + domain)) &&
        host.split('.').every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label));
      const secret = Array.from(crypto.getRandomValues(new Uint8Array(32)), b => b.toString(16).padStart(2, '0')).join('');
      const authorization = 'Basic ' + btoa('portmap:' + secret);
      const listener = await nativeApi.listen();
      const clients = new Set();
      let stopped = false;
      const serve = async id => {
        let upstream;
        let closed = false;
        let responded = false;
        let closing;
        const finish = () => {
          if (closing) return closing;
          closed = true;
          closing = Promise.all([nativeApi.close(id).catch(() => {}), closeQuietly(upstream)]);
          return closing;
        };
        const state = { close: finish };
        clients.add(state);
        const wrap = stream => ({
          read: () => limit(stream.read(), 120000, () => { void finish(); }),
          write: async data => {
            for (let offset = 0; offset < data.length; offset += CHUNK) {
              await limit(stream.write(data.slice(offset, offset + CHUNK)), 30000, () => { void finish(); });
            }
          },
        });
        const client = wrap({ read: () => nativeApi.read(id), write: data => nativeApi.write(id, data) });
        const input = new Reader(client);
        const connect = async port => {
          const opening = Promise.resolve().then(() => openUpstream(port));
          opening.then(stream => { if (closed) void closeQuietly(stream); }, () => {});
          upstream = await limit(opening, 30000, () => { void finish(); });
          if (closed) { await closeQuietly(upstream); throw new Error('Proxy closed'); }
          return wrap(upstream);
        };
        const head = () => limit(input.head(), 15000, () => { void finish(); });
        try {
          const initialHead = await head();
          let req = request(initialHead, allowed, authorization, null);
          if (req.method === 'CONNECT') {
            if (req.destination.port === 443) {
              const output = await connect(443);
              responded = true;
              await client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
              // TLS is end-to-end; browser certificate checks remain enabled.
              await Promise.race([copy(input, output), copy(new Reader(output), client)]);
              return;
            }
            responded = true;
            await client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
            // Firefox uses CONNECT :80 for ws://. Gate the inner HTTP Host;
            // never provide an unchecked plaintext CONNECT/pipeline tunnel.
            const innerHead = await head();
            req = request(innerHead, allowed, authorization, req.destination);
          }
          const output = await connect(80);
          await output.write(req.forwarded);
          if (req.expect) await client.write('HTTP/1.1 100 Continue\r\n\r\n');
          const response = new Reader(output);
          // Run both directions concurrently: origin may reject a body early.
          const upload = copyBody(input, output, req);
          const download = (async () => {
            if (req.websocket) {
              let raw;
              let parsed;
              do {
                raw = await response.head();
                parsed = parseHead(raw);
                if (!/^HTTP\/1\.[01] [0-9]{3}(?: [^\r\n]*)?$/.test(parsed.first)) throw new Error('Invalid upgrade response');
                responded = true;
                await client.write(raw);
              } while (/^HTTP\/1\.[01] 1(?:00|02|03)(?: |$)/.test(parsed.first));
              if (/^HTTP\/1\.[01] 101(?: |$)/.test(parsed.first)) {
                if (parsed.one('upgrade')?.toLowerCase() !== 'websocket' ||
                    !names(parsed.values('connection').join(',')).includes('upgrade')) throw new Error('Invalid websocket response');
                await upload;
                await Promise.race([copy(input, output), copy(response, client)]);
                return;
              }
            }
            // Force a single request: only the declared body is uploaded;
            // bytes after its boundary are discarded, never upstream pipelines.
            for (;;) {
              const bytes = await response.take();
              if (!bytes) return;
              responded = true;
              await client.write(bytes);
            }
          })();
          // An upload error must cancel download; a successful upload must not.
          await Promise.race([download, upload.then(() => download)]);
        } catch (error) {
          if (!closed && !responded) {
            const status = error instanceof HttpError ? error.status : 502;
            const phrase = {400:'Bad Request',403:'Forbidden',407:'Proxy Authentication Required',417:'Expectation Failed',502:'Bad Gateway'}[status];
            const challenge = status === 407 ? 'Proxy-Authenticate: Basic realm="Portmap internal"\r\n' : '';
            try { await client.write(`HTTP/1.1 ${status} ${phrase}\r\n${challenge}Connection: close\r\nContent-Length: 0\r\n\r\n`); }
            catch (_) {}
          }
        } finally { await finish(); clients.delete(state); }
      };
      let closing;
      const close = () => {
        if (closing) return closing;
        stopped = true;
        closing = (async () => {
          try { await nativeApi.closeListener(listener.id); }
          finally { await Promise.all([...clients].map(client => client.close())); }
        })();
        return closing;
      };
      const accepting = (async () => {
        while (!stopped) {
          try {
            const id = await nativeApi.accept(listener.id);
            if (stopped || clients.size >= 64) await nativeApi.close(id);
            else void serve(id).catch(() => {});
          } catch (error) {
            if (stopped) return;
            if (String(error).includes('Listener accept timeout')) continue;
            await close();
            return;
          }
        }
      })();
      accepting.catch(() => { void close(); });
      return { host: listener.host, port: listener.port, realm: 'Portmap internal',
        get closed() { return stopped; },
        credentials: { username: 'portmap', password: secret }, close };
    },
  };
})();
