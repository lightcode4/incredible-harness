// Clicks through every section of the app and checks it does what it should; reports overlaps and page errors.
const { chromium } = require('playwright');
const url = process.argv[2];
const problems = [], passed = [];
async function navTo(pg, sel) { if (!(await pg.isVisible(sel))) { await pg.hover('#moreBtn'); await pg.waitForTimeout(300); } await pg.click(sel); }
const check = (name, ok, detail = '') => (ok ? passed : problems).push(name + (detail ? ' — ' + detail : ''));
async function overlaps(pg, label) {
  const found = await pg.evaluate(() => {
    const sel = 'button, a, input, select, textarea, .pill, .card, .status, .badge, h1, .eyebrow, .sub, .navbtn, .chatitem, .modelchip, .sdk, .wsbox, .who, .composer, .ctab, kbd, .rrow, .nums span, .savednote';
    const modal = document.querySelector('dialog[open]') || document.querySelector('#provMenu:not([hidden])') || document.querySelector('#railDock.open #railPop') || document.querySelector('#activityPop.open');
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
    const pg = await b.newPage({ viewport: { width: w, height: h }, colorScheme: scheme });  globalThis.__pg = pg;
    const errors = []; pg.on('pageerror', e => errors.push(String(e).slice(0, 160)));
    await pg.goto(url); await pg.waitForFunction(() => document.getElementById('modelName').textContent !== 'model');
    await pg.waitForTimeout(500);
    const L = `${w}px ${scheme}`;
    await overlaps(pg, L + ' start');
    if (process.argv[3]) {  // translated interface: check the words changed and nothing overflows
      const text = await pg.innerText('aside');
      check(`[${L}] sidebar is translated`, text.includes('Más') && text.includes('Nueva sesión') && text.includes('Ajustes'), text.slice(0, 80));
      await pg.click('#openSettings'); await pg.waitForSelector('#settings[open]');
      for (const t of ['model', 'tools', 'perms']) { await pg.click(`#tabs button[data-p="${t}"]`); await pg.waitForTimeout(120); await overlaps(pg, `${L} settings ${t}`); }
      await pg.click('#cancelSettings');
      await pg.screenshot({ path: `audit-es-${w}.png` });
    } else if (w === 1440) {
      // --- sidebar
      await pg.click('#navSessions'); check('Sessions opens the session list', await pg.isVisible('#sessBox'));
      check('Sidebar shows only the essentials until More is opened', !(await pg.isVisible('#navGit')) && !(await pg.isVisible('#navUsage')) && await pg.isVisible('#moreBtn') && await pg.isVisible('#newchat'));
      await pg.hover('#moreBtn'); await pg.waitForTimeout(350);
      check('Pointing at More drops the rest down', await pg.isVisible('#navGit') && await pg.isVisible('#navHelp') && await pg.isVisible('aside [data-set="mcp"]'));
      await overlaps(pg, 'More open');
      await pg.mouse.move(900, 500); await pg.waitForTimeout(400);
      check('Moving away folds More back up', !(await pg.isVisible('#navGit')));
      await navTo(pg, '#navGit'); check('Source control opens Git', await pg.isVisible('#gitView'));
      await navTo(pg, '#navFiles'); check('Workspace opens the terminal', await pg.isVisible('#termBox'));
      await pg.click('#navPlay'); check('Agent playground shows the conversation', !(await pg.isVisible('#panel')));
      for (const [tab, pane] of [['instr', 'instr'], ['tools', 'tools'], ['mcp', 'mcp'], ['sched', 'sched']]) {
        await navTo(pg, `aside [data-set="${tab}"]`); await pg.waitForSelector('#settings[open]');
        check(`Sidebar “${tab}” opens Settings on the right tab`, await pg.isVisible(`.pane[data-p="${pane}"]`));
        await pg.click('#cancelSettings');
      }
      await navTo(pg, '#navUsage'); check('Usage & cost shows the cost', (await pg.innerText('#log')).includes('cost'));
      await navTo(pg, '#navHelp'); check('A little help opens shortcuts', await pg.isVisible('#keys[open]')); await pg.click('#closeKeys');
      await pg.click('#railFab'); await pg.waitForTimeout(250); await pg.click('#railWsBtn'); await pg.waitForTimeout(300);
      check('Workspace row opens the folder chooser', await pg.isVisible('#folderPicker[open]'));
      if (await pg.isVisible('#folderPicker[open]')) { await overlaps(pg, 'folder chooser'); await pg.keyboard.press('Escape'); }
      // --- top bar
      check('Jump box is gone from the top bar', !(await pg.isVisible('#jumpBtn')));
      await pg.keyboard.press('Control+k'); await pg.waitForTimeout(200); check('Ctrl/Cmd+K still opens the palette', await pg.isVisible('#palette[open]'));
      await pg.fill('#palIn', 'git'); check('Palette search finds Source control', (await pg.innerText('#palList')).includes('Source control')); await pg.keyboard.press('Escape');
      await pg.click('#keysBtn'); check('Help button opens shortcuts', await pg.isVisible('#keys[open]')); await pg.click('#closeKeys');
      // --- header & model bar
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
      check('Run overview is a small floating button', await pg.isVisible('#railFab') && !(await pg.isVisible('#railPop')));
      await pg.click('#railFab'); await pg.waitForTimeout(250);
      check('Clicking it pops the overview open', await pg.isVisible('#railModelBtn') && await pg.isVisible('#railCost'));
      await overlaps(pg, 'overview open');
      await pg.click('#railModelBtn'); await pg.waitForSelector('#modelPicker[open]'); await pg.waitForTimeout(300);
      check('Rail model opens the model list', await pg.isVisible('#mpList')); await overlaps(pg, 'model picker'); await pg.keyboard.press('Escape');
      await pg.click('#planBtn'); await pg.waitForTimeout(200); check('Plan mode toggles on', await pg.evaluate(() => document.getElementById('planBtn').classList.contains('on')));
      await pg.click('#planBtn'); await pg.waitForTimeout(200);
      await pg.click('#termToggle'); check('Terminal button opens the Terminal', await pg.isVisible('#termBox'));
      await pg.click('#browserToggle'); check('Browser button opens the Browser', await pg.isVisible('#bview'));
      for (const t of ['git', 'term', 'browser', 'chat']) { await pg.click(`.ctab[data-c="${t}"]`); await overlaps(pg, `tab ${t}`); }
      // --- right rail

      { const fb = await pg.locator('#railFab').boundingBox(); await pg.mouse.move(fb.x + 20, fb.y + 20); await pg.mouse.down(); await pg.mouse.move(fb.x - 300, fb.y + 200, { steps: 8 }); await pg.mouse.up(); await pg.waitForTimeout(200);
        const nb = await pg.locator('#railFab').boundingBox();
        check('Overview button can be dragged anywhere', Math.abs(nb.x - (fb.x - 320)) < 30 && Math.abs(nb.y - (fb.y + 180)) < 30 && !(await pg.isVisible('#railPop')), `${Math.round(fb.x)},${Math.round(fb.y)} -> ${Math.round(nb.x)},${Math.round(nb.y)}`);
        await pg.reload(); await pg.waitForTimeout(1200); const rb = await pg.locator('#railFab').boundingBox();
        check('It stays where you put it', Math.abs(rb.x - nb.x) < 3 && Math.abs(rb.y - nb.y) < 3);
        await pg.click('#railFab'); await pg.waitForTimeout(250); await pg.click('#taskBoardBtn'); check('Open task board responds', true);
        await pg.keyboard.press('Escape'); await pg.waitForTimeout(250); check('Escape closes the overview', !(await pg.isVisible('#railPop'))); 
        await pg.dblclick('#railFab'); await pg.waitForTimeout(300); const hb = await pg.locator('#railFab').boundingBox();
        check('Double-click puts it back in its corner', Math.abs(hb.x - fb.x) < 3 && Math.abs(hb.y - fb.y) < 3 && !(await pg.isVisible('#railPop'))); }
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
      check('Run overview shows the plan', (await pg.textContent('#todoList')).includes('Handle empty input'));
      check('Chat title updates', (await pg.innerText('#chatHead')) !== 'New session');
      // New session really starts a new one, and old sessions reopen
      const firstTitle = await pg.innerText('#chatHead');
      await pg.click('#side > .newchat'); await pg.waitForTimeout(800);
      check('New session starts an empty session', (await pg.innerText('#chatHead')) === 'New session' && !(await pg.isVisible('#scroll')) && await pg.isVisible('#welcome'));
      if (!(await pg.isVisible('#chatList'))) { await pg.click('#navSessions'); await pg.waitForTimeout(500); }
      await pg.locator('#chatList .chatitem', { hasText: firstTitle.slice(0, 20) }).first().click(); await pg.waitForTimeout(800);
      check('Opening a past session shows it again', (await pg.innerText('#chatHead')) === firstTitle && (await pg.innerText('#log')).includes('Done.'));
      // Chat / Code switch
      await pg.click('#modeCode'); await pg.waitForTimeout(300);
      check('Code switch shows changed files', await pg.isVisible('#gitView') && await pg.evaluate(() => document.getElementById('modeCode').classList.contains('on')));
      await pg.click('#modeChat'); await pg.waitForTimeout(300);
      check('Chat switch returns to the conversation', await pg.isVisible('#log') && await pg.evaluate(() => document.getElementById('modeChat').classList.contains('on')));
      // context meter, per-change review, /compact, fork, edit & resend
      check('Context meter shows how full the chat is', await pg.isVisible('#ctxMeter') && /\d+%/.test(await pg.innerText('#ctxText')));
      await pg.fill('#input', 'Tidy both functions'); await pg.keyboard.press('Enter');
      await pg.waitForSelector('.hunk', { timeout: 15000 });
      check('Approval lists each change separately', (await pg.locator('.hunk').count()) === 2);
      await overlaps(pg, 'change review');
      await pg.locator('.hunk input').nth(1).uncheck(); await pg.getByRole('button', { name: 'Apply selected' }).click();
      await pg.waitForFunction(() => document.querySelectorAll('.turnfoot').length >= 2, null, { timeout: 15000 });
      check('Apply selected applies only the chosen change', (await pg.innerText('#log')).includes('Applied 1 of 2'));
      check('Tabs are named Changes, Terminal, Browser, Files, Tasks', (await pg.innerText('.ctabs')).replace(/\s+/g, ' ').includes('Changes') && (await pg.innerText('.ctabs')).includes('Terminal') && (await pg.innerText('.ctabs')).includes('Browser') && (await pg.innerText('.ctabs')).includes('Files') && (await pg.innerText('.ctabs')).includes('Tasks'));
      await pg.click('.ctab[data-c="files"]'); await pg.waitForSelector('#fTree .fitem', { timeout: 8000 });
      check('Files tab lists the project folder', (await pg.innerText('#fTree')).includes('app.py'));
      await pg.locator('#fTree .fitem', { hasText: 'app.py' }).click(); await pg.waitForTimeout(400);
      check('Clicking a file shows its contents', (await pg.innerText('#fBody')).includes('def total'));
      await overlaps(pg, 'files tab');
      await pg.click('.ctab[data-c="tasks"]'); await pg.waitForTimeout(400);
      check('Tasks tab shows background tasks', await pg.isVisible('#jobList') && (await pg.innerText('#jobList')).includes('No background tasks'));
      await overlaps(pg, 'tasks tab');
      await pg.click('.ctab[data-c="git"]'); await pg.waitForTimeout(500);
      check('Changes tab shows changed files', (await pg.innerText('#gitFiles')).includes('app.py'));
      await pg.click('.ctab[data-c="chat"]'); await pg.waitForTimeout(200);
      // Activity panel and side panel button
      await pg.click('#activityBtn'); await pg.waitForTimeout(250);
      check('Activity panel lists what the agent did', await pg.isVisible('#activityPop') && (await pg.locator('#actList .arow').count()) >= 2, String(await pg.locator('#actList .arow').count()));
      await overlaps(pg, 'activity panel');
      await pg.locator('#actList .arow').first().click(); await pg.waitForTimeout(400);
      check('Clicking a step closes the panel and jumps to it', !(await pg.isVisible('#activityPop')));
      await pg.click('#sidePanelBtn'); await pg.waitForTimeout(250);
      check('Show side panel opens the overview', await pg.isVisible('#railPop') && await pg.evaluate(() => document.getElementById('sidePanelBtn').classList.contains('on')));
      await pg.click('#sidePanelBtn'); await pg.waitForTimeout(250);
      check('Clicking it again hides it', !(await pg.isVisible('#railPop')));
      await pg.keyboard.press('Control+.'); await pg.waitForTimeout(250);
      check('Ctrl/Cmd+. toggles the side panel', await pg.isVisible('#railPop')); await pg.keyboard.press('Escape'); await pg.waitForTimeout(250);
      await pg.locator('.umsg').first().hover(); await pg.waitForTimeout(250);
      check('Hovering a message shows Edit and Fork', await pg.locator('.umsg').first().getByRole('button', { name: 'Edit' }).isVisible());
      const before = await pg.innerText('#chatHead');
      await pg.locator('.umsg').first().getByRole('button', { name: 'Fork' }).click(); await pg.waitForTimeout(900);
      check('Fork opens a copy of the chat', (await pg.innerText('#chatHead')).startsWith('Fork:') && (await pg.innerText('#log')).includes('Forked from'), await pg.innerText('#chatHead'));
      await pg.locator('.umsg').first().hover(); await pg.locator('.umsg').first().getByRole('button', { name: 'Edit' }).click();
      check('Edit turns the message into an editor', await pg.isVisible('.uedit textarea'));
      await overlaps(pg, 'message editor');
      await pg.fill('.uedit textarea', 'Make total() handle None too'); await pg.getByRole('button', { name: 'Save & resend' }).click();
      await pg.waitForFunction(() => document.getElementById('log').innerText.includes('Edited answer.'), null, { timeout: 15000 });
      const log2 = await pg.innerText('#log');
      check('Save & resend replaces the message and continues', log2.includes('Make total() handle None too') && !log2.includes('handle an empty list'), log2.slice(0, 200));
      await pg.fill('#input', '/compact'); await pg.keyboard.press('Enter');
      await pg.waitForFunction(() => document.getElementById('log').innerText.includes('Conversation summarized'), null, { timeout: 15000 });
      check('/compact summarizes the chat', true);
      check('Older messages can no longer be rewound after a summary', (await pg.locator('.uacts').count()) === 0);
      check('Header has no Checkpoints / View code / New session', !(await pg.isVisible('#checkpointsBtn')) && !(await pg.isVisible('#viewCodeBtn')) && !(await pg.isVisible('#newSessionBtn')));
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
})().catch(async e => { console.error('AUDIT CRASHED', String(e).slice(0, 400)); try { console.error('LOG:', (await globalThis.__pg.innerText('#log')).slice(-1500)); } catch (_) {} process.exit(1); });
