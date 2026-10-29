// Uses actual TCP RST and the production upgrade handler in a disposable process.
// Only the Docker executable is replaced with a real child-process echo transport.
const assert=require('node:assert/strict');
const net=require('node:net');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const {test}=require('node:test');

const tokenA='a'.repeat(64),tokenB='b'.repeat(64);
const browserPath='/devtools/browser/00000000-0000-4000-8000-000000000001';
const echo=`let input='',upgraded=false;process.stdin.on('data',chunk=>{input+=chunk;
if(!upgraded){const end=input.indexOf('\\r\\n\\r\\n');if(end<0)return;input=input.slice(end+4);upgraded=true;process.stdout.write('HTTP/1.1 101 Switching Protocols\\r\\nConnection: Upgrade\\r\\nUpgrade: websocket\\r\\n\\r\\n');}
let end;while((end=input.indexOf('\\n'))>=0){const command=input.slice(0,end);input=input.slice(end+1);process.stdout.write(JSON.stringify({pid:process.pid,command})+'\\n');}});
process.stdin.on('end',()=>process.exit(0));process.stdout.on('error',()=>process.exit(0));`;

if(process.argv.includes('--fixture')) {
  const {createCdpServer}=require('./manager.cjs');
  const children=new Set();
  const sessions=new Map([['a',{name:'a',token:tokenA,status:'running'}],['b',{name:'b',token:tokenB,status:'running'}]]);
  const server=createCdpServer(sessions,(_file,args)=>{
    const child=spawn(process.execPath,['-e',echo],{stdio:['pipe','pipe','ignore'],windowsHide:true});
    children.add(child);process.send({type:'transport',session:args[2],pid:child.pid});
    child.on('close',()=>{children.delete(child);if(process.connected)process.send({type:'transport_closed',pid:child.pid});});
    return child;
  });
  server.listen(0,'127.0.0.1',()=>process.send({type:'ready',port:server.address().port}));
  process.on('exit',()=>{for(const child of children)child.kill();});
  process.on('message',message=>{if(message==='stop'){for(const child of children)child.kill();server.close();process.exit(0);}});
} else {
  test('activation admission gate survives process startup and rejects before allocating a session',()=>{
    const fs=require('node:fs'),path=require('node:path');
    const root=fs.mkdtempSync(path.join(require('node:os').tmpdir(),'cdp-admission-'));
    fs.mkdirSync(path.join(root,'sessions'));fs.writeFileSync(path.join(root,'admission-paused'),'reviewed-release');
    try{
      const code=`const assert=require('node:assert/strict'),fs=require('node:fs');const {create}=require(${JSON.stringify(require.resolve('./manager.cjs'))});(async()=>{
        await assert.rejects(create({owner:'test',max_lifetime_seconds:60,idle_timeout_seconds:30},'owned'),e=>e.status===503);
        assert.deepEqual(fs.readdirSync(process.env.BROWSER_STATE_DIR+'/sessions'),[]);
        fs.unlinkSync(process.env.BROWSER_STATE_DIR+'/admission-paused');
        await assert.rejects(create({},''),e=>e.status===400);
      })().catch(e=>{console.error(e);process.exitCode=1});`;
      const result=require('node:child_process').spawnSync(process.execPath,['-e',code],{env:{...process.env,BROWSER_STATE_DIR:root},encoding:'utf8',timeout:5000,windowsHide:true});
      assert.equal(result.status,0,result.stdout+result.stderr);
    }finally{
      const gate=path.join(root,'admission-paused');if(fs.existsSync(gate))fs.unlinkSync(gate);
      fs.rmdirSync(path.join(root,'sessions'));fs.rmdirSync(root);
    }
  });
  async function fixture(t) {
    const child=spawn(process.execPath,[__filename,'--fixture'],{stdio:['ignore','pipe','pipe','ipc'],windowsHide:true});
    const messages=[];let stderr='';child.stderr.on('data',x=>stderr+=x);
    child.on('message',message=>messages.push(message));
    const exit=once(child,'exit');
    t.after(async()=>{
      if(child.connected)child.send('stop');
      if(child.exitCode===null&&child.signalCode===null)await Promise.race([exit,new Promise(r=>setTimeout(r,1000))]);
      if(child.exitCode===null&&child.signalCode===null)child.kill();
    });
    const ready=await wait(()=>messages.find(m=>m.type==='ready'),'fixture start');
    return {child,messages,port:ready.port,stderr:()=>stderr};
  }
  async function wait(predicate,reason,timeout=4000) {
    const deadline=Date.now()+timeout;
    while(Date.now()<deadline){const value=predicate();if(value)return value;await new Promise(r=>setTimeout(r,10));}
    throw Error('Timed out: '+reason);
  }
  async function connect(port,token) {
    const socket=net.connect(port,'127.0.0.1');let text='';
    socket.on('data',x=>text+=x);socket.on('error',()=>{});
    await once(socket,'connect');
    socket.write(`GET /cdp/${token}${browserPath} HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n\r\n`);
    await wait(()=>text.includes('\r\n\r\n'),'HTTP upgrade');
    assert.match(text,/^HTTP\/1.1 101 /);
    let consumed=text.indexOf('\r\n\r\n')+4;
    return {socket,async command(value){socket.write(value+'\n');await wait(()=>text.indexOf('\n',consumed)>=0,'persistent transport reply');const end=text.indexOf('\n',consumed);const out=JSON.parse(text.slice(consumed,end));consumed=end+1;return out;}};
  }
  test('TCP RST closes one CDP transport while the same peer connection and session survive', {timeout:15000},async t=>{
    const manager=await fixture(t);
    const b=await connect(manager.port,tokenB);t.after(()=>b.socket.destroy());
    const before=await b.command('session-b-before');
    const a=await connect(manager.port,tokenA);t.after(()=>a.socket.destroy());
    const aReply=await a.command('session-a-before');
    a.socket.resetAndDestroy();
    await wait(()=>manager.messages.some(m=>m.type==='transport_closed'&&m.pid===aReply.pid)||manager.child.exitCode!==null,'reset transport cleanup');
    assert.equal(manager.child.exitCode,null,'manager exited after TCP RST: '+manager.stderr());
    assert.equal(manager.child.signalCode,null);
    const after=await b.command('session-b-after');
    assert.equal(after.pid,before.pid,'session B transport was replaced');
    assert.equal(after.command,'session-b-after');
    assert(manager.messages.some(m=>m.type==='transport_closed'&&m.pid===aReply.pid),'reset transport leaked');
    const reconnected=await connect(manager.port,tokenA);t.after(()=>reconnected.socket.destroy());
    assert.equal((await reconnected.command('session-a-reconnected')).command,'session-a-reconnected');
  });
  test('TCP RST during rejected upgrade cannot terminate the manager', {timeout:10000},async t=>{
    const manager=await fixture(t);
    const socket=net.connect(manager.port,'127.0.0.1');socket.on('error',()=>{});t.after(()=>socket.destroy());
    await once(socket,'connect');
    socket.write('GET /invalid HTTP/1.1\r\nHost: localhost\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n');
    socket.resetAndDestroy();
    await new Promise(r=>setTimeout(r,100));
    assert.equal(manager.child.exitCode,null,'manager exited on rejected upgrade: '+manager.stderr());
    const b=await connect(manager.port,tokenB);t.after(()=>b.socket.destroy());
    assert.equal((await b.command('alive')).command,'alive');
  });
}
