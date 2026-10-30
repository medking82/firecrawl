const fs=require('node:fs'),path=require('node:path'),ts=require('/app/node_modules/typescript');
const {adaptHangar}=require('/tmp/hangar-compat.cjs');
for(const relative of JSON.parse(fs.readFileSync('/tmp/browser-files.json','utf8'))) {
 let source=fs.readFileSync('/tmp/browser-src/'+relative+'.ts','utf8');
 if(relative==='lib/hangar') source=adaptHangar(source);
 const out=ts.transpileModule(source,{fileName:relative+'.ts',reportDiagnostics:true,
  compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,esModuleInterop:true}});
 if(out.diagnostics?.some(d=>d.category===ts.DiagnosticCategory.Error))throw new Error('TypeScript syntax failure: '+relative);
 const target='/app/dist/src/'+relative+'.js';fs.mkdirSync(path.dirname(target),{recursive:true});fs.writeFileSync(target,out.outputText);
}
