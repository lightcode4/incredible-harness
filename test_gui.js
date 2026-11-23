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
  // Terminal panel
  await pg.click('#termToggle'); await pg.waitForSelector('#termBox .xterm');
  await pg.waitForTimeout(1500); await pg.keyboard.type('echo hello from the herness terminal'); await pg.keyboard.press('Enter');
  await pg.waitForFunction(() => document.querySelector('#termBox').innerText.includes('hello from the herness terminal\n') || document.querySelector('#termBox .xterm-rows').innerText.split('hello from the herness terminal').length > 2, null, { timeout: 15000 });
  await pg.screenshot({ path: 'shot-6-terminal.png' });
  // Browser panel: navigate with the URL bar, click inside the page
  await pg.click('.ptabs .tab[data-v="browser"]'); await pg.fill('#burl', process.argv[3]); await pg.press('#burl', 'Enter');
  await pg.waitForSelector('#bimg[src^="data:"]', { timeout: 30000 }); await pg.waitForTimeout(800);
  const box = await pg.locator('#bimg').boundingBox();
  await pg.mouse.click(box.x + box.width * 0.1, box.y + box.height * 0.27); await pg.waitForTimeout(1500);
  await pg.screenshot({ path: 'shot-7-browser.png' });
  await pg.click('#closePanel');
  await pg.click('#openSettings'); await pg.waitForSelector('dialog[open]'); await pg.click('#tabs button[data-p="perms"]');
  await pg.waitForTimeout(200); await pg.screenshot({ path: 'shot-5-settings.png' });
  await pg.click('#cancelSettings');
  await pg.click('.turnfoot button'); await pg.waitForTimeout(500);   // Undo
  if (!(await pg.innerText('#log')).includes('Undid changes')) throw new Error('undo failed');
  await b.close(); console.log('BROWSER OK');
})().catch(e => { console.error(String(e).slice(0, 400)); process.exit(1); });
