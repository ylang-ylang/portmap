import { defineConfig } from 'vite';
const node = process.env.PORTMAP_PROOF_NODE;
if (!node) throw new Error('PORTMAP_PROOF_NODE is required');
const event = (type, fields) => console.log(JSON.stringify({type, node, at: new Date().toISOString(), ...fields}));
export default defineConfig({
  define: {__PORTMAP_PROOF_NODE__: JSON.stringify(node)},
  server: {host: '0.0.0.0', port: 5173, strictPort: true, allowedHosts: ['.portmap'], hmr: {clientPort: 80}},
  plugins: [{
    name: 'portmap-realnode-development-proof',
    configureServer(server) {
      const handshakes = new WeakMap();
      server.ws.on('connection', (socket, request) => {
        const fields = {host: request.headers.host, path: (request.url || '').split('?')[0], peer: request.socket.remoteAddress, forwardedFor: request.headers['x-forwarded-for'] || null, forwardedHost: request.headers['x-forwarded-host'] || null};
        handshakes.set(socket, fields);
        event('vite-websocket-upgrade', fields);
      });
      server.ws.on('portmap:nonce', (data, client) => {
        const fields = handshakes.get(client.socket);
        if (!fields) throw new Error('Missing genuine Vite WebSocket handshake');
        const reply = {nonce: data.nonce, node, ...fields};
        event('vite-websocket-nonce', reply);
        client.send('portmap:ack', reply);
      });
      server.middlewares.use(async (request, response, next) => {
        const path = (request.url || '').split('?')[0];
        const fields = {method: request.method, host: request.headers.host, path, peer: request.socket.remoteAddress, forwardedFor: request.headers['x-forwarded-for'] || null, forwardedHost: request.headers['x-forwarded-host'] || null};
        event('vite-http-request', fields);
        if (path !== '/api/echo' || request.method !== 'POST') return next();
        const chunks = [];
        for await (const chunk of request) chunks.push(chunk);
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        const reply = {nonce: body.nonce, node, ...fields};
        event('vite-http-post-nonce', reply);
        response.setHeader('content-type', 'application/json');
        response.end(JSON.stringify(reply));
      });
    }
  }]
});
