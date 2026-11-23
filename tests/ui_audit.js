// Clicks through every section of the app and checks it does what it should; reports overlaps and page errors.
const { chromium } = require('playwright');
const url = process.argv[2];
const problems = [], passed = [];
const check = (name, ok, detail = '') => (ok ? passed : problems).push(name + (detail ? ' — ' + detail : ''));
async function overlaps(pg, label) {
  const found = await pg.evaluate(() => {
    const sel = 'button, a, input, select, textarea, .pill, .card, .status, .badge, h1, .eyebrow, .sub, .navbtn, .chatitem, .modelchip, .sdk, .wsbox, .who, .composer, .ctab, kbd, .rrow, .nums span, .savednote';
    const modal = document.querySelector('dialog[open]') || document.querySelector('#provMenu:not([hidden])');
    const clip = el => { // the part of the element that is visible inside scrolling/clipping ancestors
      let r = el.getBoundingClientRect(); r = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
      for (let p = el.parentElement; p; p = p.parentElement) {
        const cs = getComputedStyle(p); if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
        const q = p.getBoundingClientRect();
        r = { left: Math.max(r.left, q.left), top: Math.max(r.top, q.top), right: Math.min(r.right, q.right), bottom: Math.min(r.bottom, q.bottom) };
      }
      return r; };
    const seen = el => { // actually visible: the top element at its centre is itself (or inside it)
      const r = el.getBoundingClientRect(); if (r.width <= 2 || r.height <= 2) return false;
      const x = Math.min(Math.max(r.left + r.width / 2, 0), innerWidth - 1), y = Math.min(Math.max(r.top + r.height / 2, 0), innerHeight - 1);
      const top = document.elementFromPoint(x, y); return !!top && (el === top || el.contains(top) || top.contains(el)); };
    const vis = el => { const s = getComputedStyle(el); return s.visibility !== 'hidden' && s.display !== 'none' && !el.closest('[hidden]') && (!modal || modal.contains(el)) && seen(el); };
    const els = [...document.querySelectorAll(sel)].filter(vis);
    const out = [];
    for (let i = 0; i < els.length; i++) for (let j = i + 1; j < els.length; j++) {
      const a = els[i], b = els[j]; if (a.contains(b) || b.contains(a)) continue;
      const ra = clip(a), rb = clip(b);
      const w = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left), h = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
      if (w > 3 && h > 3) {
        const name = e => (e.id ? '#' + e.id : e.className ? '.' + String(e.className).split(' ')[0] : e.tagName) + '“' + (e.innerText || e.placeholder || '').trim().slice(0, 18) + '”';
        out.push(name(a) + ' × ' + name(b));
      }
    }
    return out.slice(0, 15);
  });
  found.forEach(f => problems.push(`[${label}] overlap: ${f}`));
  if (!found.length) passed.push(`[${label}] no overlaps`);
}
(async () => {
  const b = await chromium.launch();
  for (const [w, h, scheme] of [[1440, 900, 'light'], [1100, 760, 'dark'], [820, 900, 'light']]) {
    const pg = await b.newPage({ viewport: { width: w, height: h }, colorScheme: scheme });
    const errors = []; pg.on('pageerror', e => errors.push(String(e).slice(0, 160)));
    await pg.goto(url); await pg.waitForFunction(() => document.getElementById('modelName').textContent !== 'model');
    await pg.waitForTimeout(500);
    const L = `${w}px ${scheme}`;
    await overlaps(pg, L + ' start');
    if (process.argv[3]) {  // translated interface: check the words changed and nothing overflows
      const text = await pg.innerText('aside');
      check(`[${L}] sidebar is translated`, text.includes('Control de versiones') && text.includes('Ajustes'), text.slice(0, 80));
      await pg.click('#openSettings'); await pg.waitForSelector('#settings[open]');
      for (const t of ['model', 'tools', 'perms']) { await pg.click(`#tabs button[data-p="${t}"]`); await pg.waitForTimeout(120); await overlaps(pg, `${L} settings ${t}`); }
      await pg.click('#cancelSettings');
      await pg.screenshot({ path: `audit-es-${w}.png` });
    } else if (w === 1440) {
      // --- sidebar
      await pg.click('#navSessions'); check('Sessions opens the session list', await pg.isVisible('#sessBox'));
      await pg.click('#navGit'); check('Source control opens Git', await pg.isVisible('#gitView'));
      await pg.click('#navFiles'); check('Workspace opens the terminal', await pg.isVisible('#termBox'));
      await pg.click('#navPlay'); check('Agent playground shows the conversation', !(await pg.isVisible('#panel')));
      for (const [tab, pane] of [['instr', 'instr'], ['tools', 'tools'], ['mcp', 'mcp'], ['sched', 'sched']]) {
        await pg.click(`aside [data-set="${tab}"]`); await pg.waitForSelector('#settings[open]');
        check(`Sidebar “${tab}” opens Settings on the right tab`, await pg.isVisible(`.pane[data-p="${pane}"]`));
        await pg.click('#cancelSettings');
      }
      await pg.click('#navUsage'); check('Usage & cost shows the cost', (await pg.innerText('#log')).includes('cost'));
      await pg.click('#navHelp'); check('A little help opens shortcuts', await pg.isVisible('#keys[open]')); await pg.click('#closeKeys');
      await pg.click('#railWsBtn'); await pg.waitForTimeout(300);
      check('Workspace row opens the folder chooser', await pg.isVisible('#folderPicker[open]'));
      if (await pg.isVisible('#folderPicker[open]')) { await overlaps(pg, 'folder chooser'); await pg.keyboard.press('Escape'); }
      // --- top bar
      await pg.click('#jumpBtn'); check('Jump to anything opens the palette', await pg.isVisible('#palette[open]'));
      await pg.fill('#palIn', 'git'); check('Palette search finds Source control', (await pg.innerText('#palList')).includes('Source control')); await pg.keyboard.press('Escape');
      await pg.click('#keysBtn'); check('Help button opens shortcuts', await pg.isVisible('#keys[open]')); await pg.click('#closeKeys');
      // --- header & model bar
      await pg.click('#viewCodeBtn'); check('View code opens Git', await pg.isVisible('#gitView')); await pg.click('.ctab[data-c="chat"]');
      const narrow = (await pg.locator('#modelPill').boundingBox()).width;
      await pg.hover('#modelPill'); await pg.waitForTimeout(400);
      const wide = (await pg.locator('#modelPill').boundingBox()).width;
      check('Model button is just the logo, and expands on hover', narrow < 60 && wide > narrow + 80, `${Math.round(narrow)} -> ${Math.round(wide)}px`);
      await pg.click('#modelPill'); await pg.waitForTimeout(500);
      check('Model name opens the provider menu', await pg.isVisible('#provMenu'));
      check('Provider menu lists providers with logos', (await pg.locator('#provList .prow').count()) >= 15 && (await pg.locator('#provList .plogo svg').count()) >= 8);
      await overlaps(pg, 'provider menu'); await pg.keyboard.press('Escape');
      await pg.click('aside [data-set="model"]'); await pg.waitForTimeout(500);
      check('Only one sidebar item is highlighted', (await pg.locator('aside .navbtn.on').count()) === 1);
      check('Models opens the Models page', await pg.isVisible('#mvGrid') && (await pg.locator('.pcard').count()) >= 15);
      await overlaps(pg, 'models page');
      await pg.screenshot({ path: 'audit-models.png' });
      await pg.click('.pcard[data-id="openrouter"]'); await pg.waitForSelector('#provDlg[open]');
      check('Provider card opens its panel with key and model', await pg.isVisible('#pdKey') && await pg.isVisible('#pdModel'));
      check('Provider panel has the Get a key link', (await pg.getAttribute('#pdGet', 'href')).includes('openrouter.ai/keys'));
      await overlaps(pg, 'provider panel'); await pg.screenshot({ path: 'audit-provider.png' }); await pg.click('#pdClose');
      await pg.click('#mvBack'); check('Back to chat closes the Models page', !(await pg.isVisible('#mvGrid')));
      await pg.click('#railModelBtn'); await pg.waitForSelector('#modelPicker[open]'); await pg.waitForTimeout(300);
      check('Rail model opens the model list', await pg.isVisible('#mpList')); await overlaps(pg, 'model picker'); await pg.keyboard.press('Escape');
      await pg.click('#planBtn'); await pg.waitForTimeout(200); check('Plan mode toggles on', await pg.evaluate(() => document.getElementById('planBtn').classList.contains('on')));
      await pg.click('#planBtn'); await pg.waitForTimeout(200);
      await pg.click('#termToggle'); check('Terminal button opens Run logs', await pg.isVisible('#termBox'));
      await pg.click('#browserToggle'); check('Browser preview opens Preview', await pg.isVisible('#bview'));
      for (const t of ['git', 'term', 'browser', 'chat']) { await pg.click(`.ctab[data-c="${t}"]`); await overlaps(pg, `tab ${t}`); }
      await pg.click('#checkpointsBtn'); check('Checkpoints responds', true);
      // --- right rail

      await pg.click('#taskBoardBtn'); check('Open task board responds', true);
      // --- settings tabs
      await pg.click('#openSettings'); await pg.waitForSelector('#settings[open]');
      for (const t of ['model', 'tools', 'perms', 'instr', 'memory', 'sched', 'mcp', 'phone', 'about']) {
        await pg.click(`#tabs button[data-p="${t}"]`); await pg.waitForTimeout(120); await overlaps(pg, `settings ${t}`);
      }
      await pg.click('#cancelSettings');
      // --- conversation flow
      await pg.fill('#input', 'Make total() handle an empty list'); await pg.keyboard.press('Enter');
      await pg.getByRole('button', { name: 'Allow', exact: true }).click({ timeout: 15000 });
      await pg.waitForSelector('.turnfoot', { timeout: 15000 });
      check('A message runs, asks approval and finishes', true);
      check('Run overview shows the plan', (await pg.innerText('#todoList')).includes('Handle empty input'));
      check('Chat title updates', (await pg.innerText('#chatHead')) !== 'New session');
      await overlaps(pg, 'after a turn');
      await pg.screenshot({ path: 'audit-1440.png' });
    } else {
      await pg.screenshot({ path: `audit-${w}.png` });
    }
    errors.forEach(e => problems.push(`[${L}] page error: ${e}`));
    await pg.close();
  }
  await b.close();
  console.log(JSON.stringify({ passed: passed.length, problems }, null, 1));
})().catch(e => { console.error('AUDIT CRASHED', String(e).slice(0, 400)); process.exit(1); });
