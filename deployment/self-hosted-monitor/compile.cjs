const fs = require('node:fs');
const path = require('node:path');
const ts = require('/app/node_modules/typescript');
for (const relative of ['lib/local-artifacts', 'lib/gcs-jobs', 'lib/gcs-monitoring', 'lib/local-monitor-judge', 'services/monitoring/judgeChange', 'services/logging/log_job']) {
  const input = fs.readFileSync(`/tmp/monitor-src/${relative}.ts`, 'utf8');
  const result = ts.transpileModule(input, {
    fileName: `${relative}.ts`, reportDiagnostics: true,
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  });
  const errors = (result.diagnostics || []).filter(x => x.category === ts.DiagnosticCategory.Error);
  if (errors.length) throw new Error(ts.formatDiagnosticsWithColorAndContext(errors, {
    getCanonicalFileName: x => x, getCurrentDirectory: () => '/app', getNewLine: () => '\n',
  }));
  const output = `/app/dist/src/${relative}.js`;
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, result.outputText);
}
