const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  for (const scheme of ['light', 'dark']) {
    const pg = await b.newPage({ viewport: { width: 1200, height: 760 }, deviceScaleFactor: 2, colorScheme: scheme });
    await pg.goto(process.argv[2]); await pg.waitForFunction(() => document.getElementById('modelName').textContent !== 'model');
    if (scheme === 'light') await pg.screenshot({ path: 'shot-welcome.png' });
    await pg.fill('#input', 'Check that my setup works'); await pg.keyboard.press('Enter');
    await pg.getByRole('button', { name: 'Allow', exact: true }).click({ timeout: 10000 });
    await pg.waitForSelector('text=Anything else you want me to build?', { timeout: 10000 });
    if (!(await pg.innerText('#log')).includes('What I did')) throw new Error('no markdown');
    await pg.locator('.toolcard').first().evaluate(d => d.open = true);
    await pg.screenshot({ path: `shot-chat-${scheme}.png` }); await pg.close();
    if (scheme === 'light') { await fetch(process.argv[2].replace('/?token=', '/api/clear?token='), { method: 'POST', body: '{}' }); }
  }
  await b.close(); console.log('BROWSER OK');
})().catch(e => { console.error(String(e).slice(0, 300)); process.exit(1); });
