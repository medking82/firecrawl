const {test}=require('node:test'), assert=require('node:assert/strict');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),crypto=require('node:crypto');
const {spawnSync}=require('node:child_process');
test('fixture root rejects production path, aliases and bad marker before starting', {skip:process.platform==='win32'},()=>{
 const id=crypto.randomBytes(16).toString('hex'), root=`/tmp/firecrawl-browser-fixture-${id}`;
 const manager=path.join(__dirname,'manager.cjs');
 const run=(dir)=>spawnSync(process.execPath,[manager],{env:{...process.env,BROWSER_FIXTURE_ID:id,BROWSER_FIXTURE_CDP_PORT:'30108',BROWSER_STATE_DIR:dir},encoding:'utf8',timeout:2000});
 assert.match(run('/home/marck/services/firecrawl-browser/shared').stderr,/invalid_fixture_root/);
 fs.mkdirSync(root,{mode:0o700});
 try {
   assert.notEqual(run(root).status,0);
   fs.writeFileSync(path.join(root,'.fixture-id'),id,{mode:0o600});
   const alias=root+'-alias';fs.symlinkSync(root,alias);
   try { assert.match(run(alias).stderr,/invalid_fixture_root/); } finally {fs.unlinkSync(alias);}
   const real=root+'-real';fs.renameSync(root,real);fs.symlinkSync(real,root);
   try { assert.match(run(root).stderr,/invalid_fixture_root/); }
   finally {fs.unlinkSync(root);fs.renameSync(real,root);}
   fs.chmodSync(path.join(root,'.fixture-id'),0o644);
   assert.match(run(root).stderr,/invalid_fixture_marker/);
 } finally {fs.rmSync(root,{recursive:true,force:true});}
});
