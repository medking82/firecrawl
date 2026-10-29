const { chromium } = require('/usr/src/app/node_modules/playwright');
const fs = require('node:fs');
(async () => {
  const code = fs.readFileSync(0, 'utf8');
  const browser = await chromium.connectOverCDP('http://127.0.0.1:9222');
  const context = browser.contexts()[0];
  const page = context.pages().filter(p => !p.isClosed()).at(-1) || await context.newPage();
  page.setDefaultTimeout(15000);
  page.setDefaultNavigationTimeout(30000);
  const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
  const result = await new AsyncFunction('browser', 'context', 'page', 'require', code)(browser, context, page, require);
  if (result !== undefined) console.log(typeof result === 'string' ? result : JSON.stringify(result));
  // Disconnect this client; the persistent browser belongs to the session daemon.
  await browser.close();
})().then(() => process.exit(0)).catch(e => { console.error(e.message); process.exit(1); });
