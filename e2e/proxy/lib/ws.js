// Websocket regression check through each proxy: does the Jellyfin socket still
// upgrade with the refresh kit's middleware in the pipeline?
const WebSocket = require('ws');
const fs = require('fs');
const tokenFile = process.argv[3] || process.env.RK_TOKEN_FILE;
if (!tokenFile) throw new Error('token file argument or RK_TOKEN_FILE is required');
const token = fs.readFileSync(tokenFile, 'utf8').trim();
const port = process.argv[2];
const prefix = process.argv[4] || '';
const url = `ws://127.0.0.1:${port}${prefix}/socket?api_key=${token}&deviceId=t`;
const ws = new WebSocket(url);
let done = false;
const finish = (ok, msg) => { if (done) return; done = true; console.log(`${ok ? 'PASS' : 'FAIL'} ws :${port}${prefix} ${msg}`); try { ws.close(); } catch (e) {} process.exit(ok ? 0 : 1); };
ws.on('open', () => { ws.send(JSON.stringify({ MessageType: 'KeepAlive' })); });
ws.on('message', (d) => {
  const s = d.toString();
  finish(true, `upgraded + received ${s.slice(0, 60)}`);
});
ws.on('error', (e) => finish(false, `error ${e.message}`));
setTimeout(() => finish(false, 'timeout, no message in 12s'), 12000);
