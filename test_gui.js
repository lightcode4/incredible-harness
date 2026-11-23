const { chromium } = require('playwright');
(async () => {
  const url = process.argv[2]; const b = await chromium.launch();
  const pg = await b.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2, colorScheme: 'light' });
  await pg.goto(url); await pg.waitForFunction(() => document.getElementById('modelName').textContent !== 'model');
  await pg.screenshot({ path: 'shot-1-welcome.png' });
  await pg.fill('#input', 'Make total() handle an empty list'); await pg.keyboard.press('Enter');
  await pg.waitForSelector('.approve', { timeout: 15000 });
  await pg.waitForTimeout(300); await pg.screenshot({ path: 'shot-2-approval.png' });
  await pg.getByRole('button', { name: 'Allow', exact: true }).click();
  await pg.waitForSelector('.turnfoot', { timeout: 15000 });
  if (!(await pg.innerText('#log')).includes('Changes')) throw new Error('no reply');
  await pg.screenshot({ path: 'shot-3-done.png' });
  await pg.emulateMedia({ colorScheme: 'dark' }); await pg.screenshot({ path: 'shot-4-dark.png' });
  await pg.emulateMedia({ colorScheme: 'light' });
  await pg.click('#openSettings'); await pg.waitForSelector('dialog[open]'); await pg.click('#tabs button[data-p="perms"]');
  await pg.waitForTimeout(200); await pg.screenshot({ path: 'shot-5-settings.png' });
  await pg.click('#cancelSettings');
  await pg.click('.turnfoot button'); await pg.waitForTimeout(500);   // Undo
  if (!(await pg.innerText('#log')).includes('Undid changes')) throw new Error('undo failed');
  await b.close(); console.log('BROWSER OK');
})().catch(e => { console.error(String(e).slice(0, 400)); process.exit(1); });
