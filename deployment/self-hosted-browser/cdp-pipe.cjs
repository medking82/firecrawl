// Transport only. Docker exec attaches to CDP without a host port or writable bind.
const net = require('node:net');
const socket = net.connect({host:'127.0.0.1',port:9222});
socket.on('error',()=>process.exit(1));
process.stdin.pipe(socket).pipe(process.stdout);
process.stdin.on('end',()=>process.exit(0));
socket.on('close',()=>process.exit(0));
