'use strict';
// Embedded session/execute source is supplied in FIXTURE_* environment variables.
const fs=require('node:fs'),{spawn,spawnSync}=require('node:child_process'),assert=require('node:assert/strict');
const dir='/tmp/browser-fixture';fs.mkdirSync(dir,{recursive:true});
for(const n of ['session','execute']) fs.writeFileSync(`${dir}/${n}.cjs`,Buffer.from(process.env['FIXTURE_'+n.toUpperCase()],'base64'));
const session=spawn(process.execPath,[`${dir}/session.cjs`],{stdio:'ignore'});
async function main(){
 let version;
 for(let i=0;i<80;i++) { try {version=await (await fetch('http://127.0.0.1:9222/json/version')).json();break;} catch {await new Promise(r=>setTimeout(r,250));} }
 assert(version?.webSocketDebuggerUrl,'Chromium did not start');
 console.log('PASS isolated_chromium_create');
 function execute(code){const r=spawnSync(process.execPath,[`${dir}/execute.cjs`],{input:code,encoding:'utf8',timeout:20000});assert.equal(r.status,0,r.stderr);return r.stdout;}
 execute("await page.setContent('<title>Fixture</title><p id=proof>persisted</p>');");
 assert.match(execute("console.log(await page.title(),await page.locator('#proof').innerText())"),/Fixture persisted/);
 console.log('PASS isolated_execute_persistent_dom');
 const ws=new WebSocket(version.webSocketDebuggerUrl);
 await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;setTimeout(()=>reject(Error('CDP timeout')),5000).unref();});
 const response=new Promise((resolve,reject)=>{ws.onmessage=e=>resolve(JSON.parse(e.data));ws.onerror=reject;setTimeout(()=>reject(Error('CDP response timeout')),5000).unref();});
 ws.send(JSON.stringify({id:1,method:'Browser.getVersion'}));
 assert((await response).result.product);ws.close();console.log('PASS isolated_cdp_protocol');
}
main().catch(e=>{console.error(e);process.exitCode=1;}).finally(()=>{
 session.kill('SIGTERM');
 // Undici and WebSocket connections may otherwise keep the fixture process alive.
 setTimeout(()=>process.exit(process.exitCode || 0),100);
});
