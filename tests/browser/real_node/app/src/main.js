import './style.css';
// portmap-real-vite-js-8.0.16
const node = __PORTMAP_PROOF_NODE__;
const button = document.querySelector('#counter');
let count = 0;
button.addEventListener('click', () => { button.textContent = `Count ${++count}`; });
document.querySelector('#node-identity').textContent = `Independent actual node: ${node}`;
const pending = new Map();
if (!import.meta.hot) throw new Error('This proof requires a real Vite development server');
import.meta.hot.on('portmap:ack', data => {
  const entry = pending.get(data.nonce);
  if (!entry) return;
  clearTimeout(entry.timeout);
  pending.delete(data.nonce);
  entry.resolve(data);
});
window.realPortmapProof = {
  ready: true,
  node,
  viteVersion: '8.0.16',
  async exercise(nonce) {
    const websocketPromise = new Promise((resolve, reject) => {
      const timeout = setTimeout(() => { pending.delete(nonce); reject(new Error('Genuine Vite WS acknowledgement timed out')); }, 15000);
      pending.set(nonce, {resolve, reject, timeout});
      import.meta.hot.send('portmap:nonce', {nonce});
    });
    const response = await fetch('/api/echo', {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify({nonce})});
    if (!response.ok) throw new Error(`Echo POST failed: ${response.status}`);
    const post = await response.json();
    const websocket = await websocketPromise;
    const staticResponse = await fetch('/proof.txt');
    const image = document.querySelector('#badge');
    if (!image.complete) await image.decode();
    const result = {
      nonce, node, websocket, post,
      cssLoaded: getComputedStyle(document.querySelector('#asset-proof')).color === 'rgb(1, 2, 3)',
      jsBehavior: count > 0 && button.textContent === `Count ${count}`,
      staticLoaded: staticResponse.ok && (await staticResponse.text()).includes('portmap-realnode-vite-8.0.16-static-resource') && image.naturalWidth === 160
    };
    document.querySelector('#proof-result').textContent = JSON.stringify(result, null, 2);
    return result;
  }
};
