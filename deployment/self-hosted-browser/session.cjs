// Runs ONLY in a disposable, network-none container. No host credentials/socket.
const net = require('node:net');
const fs = require('node:fs');
const { chromium } = require('/usr/src/app/node_modules/playwright');
function relay(socket, target) {
  const upstream = net.connect(target);
  socket.on('error', () => upstream.destroy());
  upstream.on('error', () => socket.destroy());
  socket.pipe(upstream).pipe(socket);
}
// Chromium understands TCP proxies; the only egress is a public-IP-only host proxy.
net.createServer(s => relay(s, '/run/egress/proxy.sock')).listen(3128, '127.0.0.1');
(async () => {
  await chromium.launchPersistentContext('/tmp/chrome', {
    headless: true, proxy: { server: 'http://127.0.0.1:3128' },
    args: ['--no-sandbox', '--remote-debugging-port=9222', '--remote-debugging-address=127.0.0.1',
      '--proxy-bypass-list=<-loopback>', '--disable-quic'],
  });
})().catch(() => process.exit(1));
