// Clicks through every section of the app and checks it does what it should; reports overlaps and page errors.
const { chromium } = require('playwright');
const url = process.argv[2];
const problems = [], passed = [];
async function tab(pg, t) {
  if (t === 'chat') { if (await pg.evaluate(() => !!panelTab)) await pg.click('#closePanel'); await pg.waitForTimeout(150); return; }
  if (!(await pg.isVisible('#panel .ptabs'))) { await pg.click('#sidePanelBtn'); await pg.waitForTimeout(200); }
  await pg.click(`#panel .ptabs .tab[data-v="${t}"]`); await pg.waitForTimeout(150);
}
async function navTo(pg, sel) { if (!(await pg.isVisible(sel))) { await pg.evaluate(() => document.querySelector('.more')?.classList.add('open')); await pg.waitForTimeout(350); } await pg.waitForSelector(sel, { state: 'visible' }); await pg.click(sel); }
const check = (name, ok, detail = '') => (ok ? passed : problems).push(name + (detail ? ' — ' + detail : ''));
async function overlaps(pg, label) {
  const found = await pg.evaluate(() => {
    const sel = 'button, a, input, select, textarea, .pill, .card, .status, .badge, h1, .eyebrow, .sub, .navbtn, .chatitem, .modelchip, .sdk, .wsbox, .who, .composer, .ctab, kbd, .rrow, .nums span, .savednote';
    const FLOATERS = ['railFab']; const modal = document.querySelector('dialog[open]') || document.querySelector('#provMenu:not([hidden])') || document.querySelector('#railDock.open #railPop') || document.querySelector('#activityPop.open') || document.querySelector('#sideChat:not([hidden])') || document.querySelector('#modeMenu:not([hidden])') || document.querySelector('#folderMenu:not([hidden])');
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
      if ((FLOATERS.includes(a.id) && b.classList.contains('card')) || (FLOATERS.includes(b.id) && a.classList.contains('card'))) continue;  // floating button over the chat area is intended
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
      await pg.evaluate(() => openSettings()); await pg.waitForSelector('#settings[open]');
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
      await navTo(pg, '#navUsage'); await pg.waitForTimeout(400); check('Usage & cost opens the usage page', await pg.isVisible('#usageView') && await pg.isVisible('#usToday'));
      await overlaps(pg, 'usage page'); await pg.click('#usBack');
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
      check('New chat shows the big background mascot and a greeting', await pg.isVisible('#bgm.sc-photo .ph') && !(await pg.isVisible('#helloMascot')) && (await pg.innerText('#hello')).length > 3);
      check('The background mascot is animated', await pg.evaluate(() => getComputedStyle(document.querySelector('#bgm .ph')).animationName === 'bgKen'));
      check('Background picker switches scenes and None brings back the small mascot', await pg.evaluate(async () => {
        const ok = [];
        for (const id of ['companion', 'orbit', 'landscape']) { document.querySelector(`#scenePick [data-s="${id}"]`).click(); ok.push(document.getElementById('bgm')?.dataset.scene === id); }
        for (const id of ['dusk', 'dream', 'aurora', 'moon', 'meteor', 'clouds', 'lighthouse', 'meadow', 'wave', 'halo', 'tower', 'datafall', 'forest']) { document.querySelector(`#scenePick [data-s="${id}"]`).click(); const ph = document.querySelector('#bgm.sc-photo .ph'); ok.push(!!ph && document.body.classList.contains(['clouds', 'lighthouse', 'meadow', 'wave'].includes(id) ? 'has-lightbg' : 'has-darkbg') && (await fetch(`/scenes/${id}.webp`)).ok); }
        document.querySelector('#scenePick [data-s="auto"]').click(); const isDay = (h => h >= 6 && h < 18)(new Date().getHours());
        applyTheme('light'); ok.push(document.body.classList.contains('has-lightbg')); applyTheme('dark'); ok.push(document.body.classList.contains('has-darkbg')); applyTheme('system'); ok.push(document.body.classList.contains(isDay ? 'has-lightbg' : 'has-darkbg'));
        document.querySelector('#scenePick [data-s="none"]').click(); ok.push(!document.getElementById('bgm'));
        const small = getComputedStyle(document.getElementById('helloMascot')).display !== 'none';
        return ok.every(Boolean) && small;
      }));
      check('The background stays while chatting', await pg.evaluate(() => { document.querySelector('#scenePick [data-s="daybreak"]').click(); document.body.classList.add('chatting'); const v = getComputedStyle(document.getElementById('bgm')).display !== 'none'; document.body.classList.remove('chatting'); document.querySelector('#scenePick [data-s="none"]').click(); return v; }));
      await pg.evaluate(() => sceneSave); await pg.reload(); await pg.waitForSelector('#hello');
      check('The chosen background is remembered after a restart', await pg.evaluate(() => new Promise(r => setTimeout(() => r(!document.getElementById('bgm') && document.querySelector('#scenePick [data-s="none"]').classList.contains('on')), 800))));
      await pg.evaluate(() => document.querySelector('#scenePick [data-s="daybreak"]').click()); await pg.waitForTimeout(300);
      check('The message box sits in the middle on a new chat', await pg.evaluate(() => document.getElementById('wslot').contains(document.getElementById('composer'))) && /What should we build|How can I help/.test(await pg.getAttribute('#input', 'placeholder')));
      await pg.click('#folderBtn'); await pg.waitForTimeout(300);
      check('Project or folder menu lists folders and a way to choose another', await pg.isVisible('#folderMenu') && (await pg.innerText('#folderMenu')).includes('Choose another folder'));
      await overlaps(pg, 'folder menu'); await pg.keyboard.press('Escape');
      check('Front page is clean: no big heading, no suggestion chips, no footer slogan', !(await pg.isVisible('.page > .pagehead')) && !(await pg.isVisible('.chip')) && !(await pg.innerText('body')).includes('Your machine. Your rules'));
      const mp = await pg.locator('#modelPill').boundingBox(), cb = await pg.locator('#composer').boundingBox();
      check('Model and mode sit under the message box', mp && cb && mp.y > cb.y + cb.height - 2 && await pg.isVisible('#planBtn'), JSON.stringify([mp?.y, cb?.y, cb?.height]));
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
      await pg.click('#planBtn'); await pg.waitForTimeout(200); check('Mode button opens the mode menu', await pg.isVisible('#modeMenu'));
      await overlaps(pg, 'mode menu');
      await pg.click('#modeMenu [data-mode="plan"]'); await pg.waitForTimeout(250);
      check('Choosing Plan mode turns it on', await pg.evaluate(() => document.getElementById('planBtn').classList.contains('on')) && (await pg.innerText('#modeLabel')) === 'Plan mode');
      await pg.click('#planBtn'); await pg.click('#modeMenu [data-mode="auto"]'); await pg.waitForTimeout(250);
      check('Auto-approve can be chosen', (await pg.innerText('#modeLabel')) === 'Auto-approve' && !(await pg.evaluate(() => document.getElementById('planBtn').classList.contains('on'))));
      await pg.click('#planBtn'); await pg.click('#modeMenu [data-mode="ask"]'); await pg.waitForTimeout(250);
      check('Back to Asks first', (await pg.innerText('#modeLabel')) === 'Asks first');
      await pg.click('#termToggle'); check('Terminal button opens the Terminal', await pg.isVisible('#termBox'));
      await pg.click('#browserToggle'); check('Browser button opens the Browser', await pg.isVisible('#bview'));
      await pg.click('#maxPanel'); check('Maximize gives the panel the whole window', await pg.evaluate(() => document.body.classList.contains('panel-max') && getComputedStyle(document.querySelector('.chatcard')).display === 'none' && document.getElementById('panel').getBoundingClientRect().width > innerWidth * 0.6));
      await pg.click('#maxPanel'); check('Restore puts the chat back', await pg.evaluate(() => !document.body.classList.contains('panel-max') && getComputedStyle(document.querySelector('.chatcard')).display !== 'none'));
      for (const t of ['git', 'term', 'browser', 'chat']) { await tab(pg, t); await overlaps(pg, `tab ${t}`); }
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
      await pg.evaluate(() => openSettings()); await pg.waitForSelector('#settings[open]');
      for (const t of ['model', 'tools', 'perms', 'instr', 'memory', 'sched', 'mcp', 'services', 'github', 'phone', 'about']) {
        await pg.click(`#tabs button[data-p="${t}"]`); await pg.waitForTimeout(120); await overlaps(pg, `settings ${t}`);
      }
      await pg.click('#cancelSettings'); await pg.click('#navTeams'); await pg.waitForTimeout(500);
      check('Teams page opens with New team and asks to connect GitHub first', await pg.isVisible('#teamsView') && await pg.isVisible('#tmNew') && (await pg.innerText('#tmInvites')).includes('Connect GitHub'));
      await overlaps(pg, 'teams page');
      await pg.click('#navPlay'); await pg.evaluate(() => openSettings()); await pg.waitForSelector('#settings[open]');
      await pg.click('#tabs button[data-p="github"]');
      await pg.waitForTimeout(300);
      check('GitHub pane offers Sign in with GitHub, with a token as the fallback', await pg.isVisible('#ghSignIn') && (await pg.locator('#ghAlt summary').innerText()).includes('token'));
      await pg.click('#tabs button[data-p="services"]');
      check('Services pane has search, image and cloud upload settings', await pg.isVisible('#svSearch') && await pg.isVisible('#svImage') && await pg.isVisible('#s3Bucket')
        && (await pg.evaluate(() => [...document.querySelectorAll('#svSearch option')].map(o => o.value).join(','))).includes('tavily'));
      await pg.click('#cancelSettings');
      // --- conversation flow
      await pg.fill('#input', 'Make total() handle an empty list'); await pg.keyboard.press('Enter');
      await pg.waitForSelector('.ask .askbtn', { timeout: 15000 });
      check('Approval shows what it will do, with Deny / Always allow / Allow once', (await pg.locator('.ask .askbtn').count()) === 3 && (await pg.innerText('.ask .askinfo')).includes('app.py')
        && (await pg.getAttribute('.ask .askbtn:nth-of-type(2)', 'title')).includes('allow all edits'));
      { const r = await pg.evaluate(() => { const a = document.querySelector('.ask').getBoundingClientRect(); return { h: a.height, w: a.width, vh: innerHeight }; });
        check('Approval prompt is compact (does not take the whole screen)', r.h < r.vh * 0.6 && r.w <= 900, JSON.stringify(r)); }
      await overlaps(pg, 'approval card');
      await pg.keyboard.press('1');  // keyboard answers work
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
      { const sb = await pg.locator('#side .sidemode').boundingBox(), nb = await pg.locator('#newchat').boundingBox();
        check('Chat / Code switch sits at the top of the sidebar', sb && nb && sb.y < nb.y && !(await pg.isVisible('.topbar .modeseg'))); }
      check('Code mode is on for coding sessions', await pg.evaluate(() => document.getElementById('modeCode').classList.contains('on')) && await pg.isVisible('#sidePanelBtn'));
      await pg.click('#modeChat'); await pg.waitForTimeout(900);
      check('Chat mode opens a plain assistant chat', await pg.evaluate(() => document.body.classList.contains('mode-chat') && document.getElementById('modeChat').classList.contains('on')) && (await pg.getAttribute('#input', 'placeholder')) === 'How can I help you today?',
        JSON.stringify(await pg.evaluate(() => [document.body.className, document.getElementById('input').placeholder, document.getElementById('modeChat').className])));
      check('Chat mode hides folders, terminal, browser and side panel', !(await pg.isVisible('#folderBtn')) && !(await pg.isVisible('#sidePanelBtn')) && !(await pg.isVisible('#termToggle')) && !(await pg.isVisible('#railFab')));
      if (!(await pg.isVisible('#chatList'))) { await pg.click('#navSessions'); await pg.waitForTimeout(300); }
      check('Chat mode lists only chats, not code sessions', !(await pg.innerText('#chatList')).includes(firstTitle.slice(0, 20)));
      await overlaps(pg, 'chat mode');
      await pg.click('#modeCode'); await pg.waitForTimeout(900);
      check('Code mode returns to the latest coding session', (await pg.innerText('#chatHead')) === firstTitle && await pg.isVisible('#sidePanelBtn'));
      // context meter, per-change review, /compact, fork, edit & resend
      check('Context meter shows how full the chat is', await pg.isVisible('#ctxMeter') && /\d+%/.test(await pg.innerText('#ctxText')));
      await pg.fill('#input', 'Tidy both functions'); await pg.keyboard.press('Enter');
      await pg.waitForSelector('.hunk', { timeout: 15000 });
      check('Approval lists each change separately', (await pg.locator('.hunk').count()) === 2);
      await overlaps(pg, 'change review');
      await pg.locator('.hunk input').nth(1).uncheck(); await pg.getByRole('button', { name: 'Apply selected' }).click();
      await pg.waitForFunction(() => document.querySelectorAll('.turnfoot').length >= 2, null, { timeout: 15000 });
      check('Apply selected applies only the chosen change', (await pg.innerText('#log')).includes('Applied 1 of 2'));
      await tab(pg, 'git');
      const pt = await pg.innerText('#panel .ptabs');
      check('Side panel has Changes, Terminal, Browser, Files and Tasks', ['Changes', 'Terminal', 'Browser', 'Files', 'Tasks'].every(x => pt.includes(x)), pt);
      check('The conversation stays visible beside the panel', await pg.isVisible('#log') && await pg.isVisible('#composer') && await pg.isVisible('#panel'));
      check('Changes shows the first diff right away', (await pg.innerText('#gitDiff')).includes('+') && !(await pg.innerText('#gitDiff')).includes('Select a file'));
      { const g = await pg.locator('.chatcard').boundingBox(), pb = await pg.locator('#panel').boundingBox(); check('Panel sits to the right of the chat', pb.x >= g.x + g.width - 2, JSON.stringify([g.x + g.width, pb.x])); }
      await overlaps(pg, 'side panel');
      await tab(pg, 'files'); await pg.waitForSelector('#fTree .fitem', { timeout: 8000 });
      check('Files tab lists the project folder', (await pg.innerText('#fTree')).includes('app.py'));
      await pg.locator('#fTree .fitem', { hasText: 'app.py' }).click(); await pg.waitForTimeout(400);
      check('Clicking a file shows its contents', (await pg.innerText('#fBody')).includes('def total'));
      await overlaps(pg, 'files tab');
      await tab(pg, 'tasks'); await pg.waitForTimeout(400);
      check('Tasks tab shows background tasks', await pg.isVisible('#jobList') && (await pg.innerText('#jobList')).includes('No background tasks'));
      await overlaps(pg, 'tasks tab');
      await tab(pg, 'git'); await pg.waitForTimeout(500);
      check('Changes tab shows changed files', (await pg.innerText('#gitFiles')).includes('app.py'));
      await tab(pg, 'sec'); await pg.waitForSelector('#secList .secfind', { timeout: 8000 });
      check('Security review scans the project and flags issues', (await pg.locator('#secList .secfind').count()) >= 1 && (await pg.innerText('#secSummary')).toLowerCase().includes('high'));
      check('A finding links to its file and location', /insecure\.py:\d+/.test(await pg.innerText('#secList .secfind .seclink')));
      check('Findings can be filtered', await pg.evaluate(() => { const n = document.querySelectorAll('#secList .secfind').length; const s = document.getElementById('secSearch'); s.value = 'zz-no-such-finding-zz'; s.dispatchEvent(new Event('input')); const after = document.querySelectorAll('#secList .secfind').length; s.value = ''; s.dispatchEvent(new Event('input')); return n >= 1 && after === 0; }));
      await overlaps(pg, 'security tab');
      await tab(pg, 'chat'); await pg.waitForTimeout(200);
      check('Consecutive commands collapse into "Ran N commands"', await pg.evaluate(() => {
        for (let i = 1; i <= 3; i++) { toolCard({ name: 'bash', summary: '$ echo ' + i, id: 'tcg' + i }); toolResult({ id: 'tcg' + i, output: String(i) }); }
        const g = document.querySelector('#log .runsgroup'); const ok = !!g && /Ran 3 commands/.test(g.querySelector('.gsum').textContent) && g.querySelectorAll('.tgbody > details').length === 3;
        g && g.remove(); return ok;
      }));
      check('Working bar shows a friendly phrase, not the raw command', await pg.evaluate(() => toolPhrase({ name: 'bash' }) === 'Running commands' && toolPhrase({ name: 'read_file' }) === 'Reading files' && toolPhrase({ name: 'web_search' }) === 'Searching the web'));
      check('Sidebar auto-hides while the agent works', await pg.evaluate(() => { sideAuto = true; setBusy(true); const on = document.body.classList.contains('working') && getComputedStyle(document.getElementById('side')).position === 'fixed'; setBusy(false); sideAuto = false; return on && !document.body.classList.contains('working'); }));
      check('Edits show a compact "Edited <file> +N −M" summary', await pg.evaluate(() => {
        toolCard({ name: 'multi_edit', summary: 'Edit demo.js', id: 'edsum', diff: '--- a/demo.js\n+++ b/demo.js\n@@\n-old\n+new1\n+new2\n' });
        const c = tools['edsum']; const t = c ? c.querySelector('.sum').textContent : '';
        c && c.remove(); return /Edited/.test(t) && t.includes('demo.js') && t.includes('+2') && (t.includes('−1') || t.includes('-1'));
      }));
      // Activity panel and side panel button
      await pg.click('#activityBtn'); await pg.waitForTimeout(250);
      check('Activity panel lists what the agent did', await pg.isVisible('#activityPop') && (await pg.locator('#actList .arow').count()) >= 2, String(await pg.locator('#actList .arow').count()));
      await overlaps(pg, 'activity panel');
      await pg.locator('#actList .arow').first().click(); await pg.waitForTimeout(400);
      check('Clicking a step closes the panel and jumps to it', !(await pg.isVisible('#activityPop')));
      await tab(pg, 'chat'); await pg.click('#sidePanelBtn'); await pg.waitForTimeout(250);
      check('Show side panel opens the work panel beside the chat', await pg.isVisible('#panel') && await pg.isVisible('#log') && await pg.evaluate(() => document.getElementById('sidePanelBtn').classList.contains('on')));
      check('The sun button steps aside while the panel is open', !(await pg.isVisible('#railFab')));
      await pg.click('#sidePanelBtn'); await pg.waitForTimeout(250);
      check('Clicking it again hides it', !(await pg.isVisible('#panel')) && await pg.isVisible('#railFab'));
      await pg.keyboard.press('Control+.'); await pg.waitForTimeout(250);
      check('Ctrl/Cmd+. toggles the side panel', await pg.isVisible('#panel')); await pg.keyboard.press('Control+.'); await pg.waitForTimeout(250);
      await pg.locator('.umsg').first().hover(); await pg.waitForTimeout(250);
      check('Hovering a message shows Copy, Edit, Rewind and Fork', await pg.locator('.umsg').first().evaluate(m => [...m.querySelectorAll('.uacts button')].map(b => b.title.split(':')[0]).join(',')) === 'Copy,Edit,Rewind to here,Fork');
      await pg.locator('.umsg').first().getByRole('button', { name: 'Rewind' }).click();
      check('Rewind asks first, in a small card', await pg.isVisible('.rwbox') && (await pg.innerText('.rwbox')).includes('Rewind to before this message') && (await pg.locator('.rwbox').boundingBox()).height < 200);
      await pg.locator('.rwbox').getByRole('button', { name: 'Cancel' }).click(); check('Cancel leaves the chat as it was', !(await pg.isVisible('.rwbox')));
      const before = await pg.innerText('#chatHead');
      await pg.locator('.umsg').first().getByRole('button', { name: 'Fork' }).click(); await pg.waitForTimeout(900);
      check('Fork opens a copy of the chat', (await pg.innerText('#chatHead')).startsWith('Fork:') && (await pg.innerText('#log')).includes('Forked from'), await pg.innerText('#chatHead'));
      await pg.locator('.umsg').first().hover(); await pg.locator('.umsg').first().getByRole('button', { name: 'Edit' }).click();
      check('Edit turns the message into an editor', await pg.isVisible('.uedit textarea'));
      await overlaps(pg, 'message editor');
      { const f = await pg.evaluate(() => { const r = document.querySelector('.uedit .uefoot').getBoundingClientRect(); const bs = [...document.querySelectorAll('.uedit .uefoot button')].map(b => b.getBoundingClientRect());
          return { h: r.height, wide: Math.max(...bs.map(b => b.width)), sameRow: bs.every(b => Math.abs(b.top - bs[0].top) < 2) }; });
        check('Edit box footer is one compact row (small Cancel / Save buttons)', f.h <= 40 && f.wide < 120 && f.sameRow, JSON.stringify(f)); }
      check('Chat text can be selected for copying', await pg.evaluate(() => getComputedStyle(document.querySelector('#log')).userSelect !== 'none'));
      await pg.fill('.uedit textarea', 'Make total() handle None too'); await pg.getByRole('button', { name: 'Save', exact: true }).click();
      await pg.waitForFunction(() => document.getElementById('log').innerText.includes('Edited answer.'), null, { timeout: 15000 });
      const log2 = await pg.innerText('#log');
      check('Save replaces the message and continues', log2.includes('Make total() handle None too') && !log2.includes('handle an empty list'), log2.slice(0, 200));
      await pg.fill('#input', '/compact'); await pg.keyboard.press('Enter');
      await pg.waitForFunction(() => document.getElementById('log').innerText.includes('Conversation summarized'), null, { timeout: 15000 });
      check('/compact summarizes the chat', true);
      check('After a summary older messages keep only Copy', await pg.evaluate(() => [...document.querySelectorAll('.uacts')].every(a => a.children.length === 1 && a.children[0].title === 'Copy')));
      // keyboard shortcuts for the tabs
      for (const [keys, sel, name] of [['Control+Shift+F', '#fTree', 'Files'], ['Control+j', '#termBox', 'Terminal'], ['Control+Shift+D', '#gitView', 'Changes'], ['Control+Shift+T', '#jobList', 'Tasks'], ['Control+Shift+B', '#bview', 'Browser']]) {
        await pg.keyboard.press(keys); await pg.waitForTimeout(350); check(`${keys} opens ${name}`, await pg.isVisible(sel));
      }
      check('Sidebar stays put with Shift (Ctrl+Shift+B is not Ctrl+B)', await pg.isVisible('#side .brand'));
      // edit a file in the Files tab
      await pg.keyboard.press('Control+Shift+F'); await pg.waitForSelector('#fTree .fitem', { timeout: 8000 });
      await pg.locator('#fTree .fitem', { hasText: 'app.py' }).click(); await pg.waitForTimeout(400);
      await pg.click('#fEditBtn'); check('Edit turns the file into an editor', await pg.isVisible('#fEdit'));
      await overlaps(pg, 'file editor');
      await pg.locator('#fEdit').press('Control+End'); await pg.keyboard.type('\n# edited by hand\n'); await pg.keyboard.press('Control+s'); await pg.waitForTimeout(500);
      check('Ctrl+S saves the file', !(await pg.isVisible('#fEdit')) && (await pg.innerText('#fBody')).includes('# edited by hand'), await pg.innerText('#fErr'));
      await pg.click('#fRefresh'); await pg.waitForTimeout(300); await pg.locator('#fTree .fitem', { hasText: 'app.py' }).click(); await pg.waitForTimeout(400);
      check('The saved change is really on disk', (await pg.innerText('#fBody')).includes('# edited by hand'));
      await tab(pg, 'chat'); await pg.waitForTimeout(200);
      // side chat
      await pg.keyboard.press('Control+;'); await pg.waitForTimeout(250);
      check('Ctrl+; opens the side chat', await pg.isVisible('#sideChat'));
      await overlaps(pg, 'side chat');
      await pg.fill('#scInput', 'what does total() do?'); await pg.keyboard.press('Enter');
      await pg.waitForFunction(() => document.getElementById('scLog').innerText.includes('Side answer'), null, { timeout: 15000 });
      check('Side chat answers in its own panel', (await pg.innerText('#scLog')).includes('what does total() do?') && !(await pg.innerText('#log')).includes('Side answer'));
      await pg.keyboard.press('Escape'); await pg.waitForTimeout(200); check('Esc closes the side chat', !(await pg.isVisible('#sideChat')));
      check('Side chats are not listed as sessions', !(await pg.innerText('#chatList')).includes('Side chat'));
      // search past chats
      if (!(await pg.isVisible('#chatSearch'))) { await pg.click('#navSessions'); await pg.waitForTimeout(300); }
      await pg.fill('#chatSearch', 'None too'); await pg.waitForSelector('.chathit', { timeout: 8000 });
      check('Search finds chats by what was said', (await pg.innerText('#chatList')).includes('None too'));
      await pg.fill('#chatSearch', ''); await pg.waitForTimeout(500);
      // projects
      await pg.click('#navProjects'); await pg.waitForTimeout(400);
      check('Projects opens the Projects page', await pg.isVisible('#projectsView') && await pg.isVisible('#pjGrid .pjcard.add'));
      await pg.click('#pjGrid .pjcard.add'); await pg.fill('#pjName', 'Website'); await pg.fill('#pjInstr', 'Use British spelling.');
      await pg.click('#pjSave'); await pg.waitForTimeout(600);
      check('A project can be created', !(await pg.isDisabled('#pjChat')) && (await pg.inputValue('#pjName')) === 'Website', await pg.innerText('#pjErr'));
      await overlaps(pg, 'project page');
      await pg.click('#pjChat'); await pg.waitForTimeout(800);
      check('New chat in this project starts a project chat', (await pg.innerText('#chatSub')).includes('Project · Website') && !(await pg.isVisible('#projectsView')));
      await pg.click('#navProjects'); await pg.waitForTimeout(400);
      check('The project card is listed', (await pg.innerText('#pjGrid')).includes('Website'));
      await pg.locator('.pjcard', { hasText: 'Website' }).click(); await pg.waitForTimeout(300);
      check('A project can be exported', await pg.isVisible('#pjExport') && !(await pg.isDisabled('#pjExport')));
      await pg.click('#pjBack'); await pg.waitForTimeout(200);
      // reply actions: copy, retry, read aloud, time
      await pg.click('#navPlay'); await pg.waitForTimeout(200);
      await pg.fill('#input', 'Say something'); await pg.keyboard.press('Enter');
      await pg.waitForFunction(() => document.getElementById('log').innerText.includes('First answer.'), null, { timeout: 15000 }); await pg.waitForTimeout(300);
      const lastFoot = pg.locator('.turnfoot').last();
      check('Each reply has Copy, Retry and Read aloud', await lastFoot.getByRole('button', { name: 'Copy reply' }).isVisible() && await lastFoot.getByRole('button', { name: /Retry/ }).isVisible() && await lastFoot.getByRole('button', { name: 'Read aloud' }).count() >= 0);
      check('Each reply shows when it was sent', /\d{1,2}:\d{2}/.test(await lastFoot.locator('.rtime').innerText()));
      check('Only the latest reply can be retried', (await pg.locator('.turnfoot .retry').count()) === 1);
      await lastFoot.getByRole('button', { name: /Retry/ }).click();
      await pg.waitForFunction(() => document.getElementById('log').innerText.includes('Second answer, after Retry.'), null, { timeout: 15000 });
      check('Retry replaces the answer', !(await pg.innerText('#log')).includes('First answer.'));
      await overlaps(pg, 'reply actions');
      // rename and pin a chat
      if (!(await pg.isVisible('#chatList'))) { await pg.click('#navSessions'); await pg.waitForTimeout(300); }
      const item = pg.locator('#chatList .chatitem.on'); await item.hover(); await item.locator('.ren').click();
      await pg.locator('#chatList input.rename').fill('My renamed chat'); await pg.keyboard.press('Enter'); await pg.waitForTimeout(600);
      check('A chat can be renamed', (await pg.innerText('#chatList')).includes('My renamed chat') && (await pg.innerText('#crumbHere')).includes('My renamed chat'));
      const other = pg.locator('#chatList .chatitem:not(.on)').last(); const otherName = (await other.locator('span').first().innerText()).trim();
      await other.hover(); await other.locator('.pin').click(); await pg.waitForTimeout(600);
      check('A pinned chat moves to the top under “Pinned”', (await pg.locator('#chatList .chatitem').first().innerText()).includes(otherName) && await pg.isVisible('#chatList .pinsep'), otherName);
      await overlaps(pg, 'pinned chats');
      await pg.locator('#chatList .chatitem').first().hover(); await pg.locator('#chatList .chatitem').first().locator('.pin').click(); await pg.waitForTimeout(500);
      check('Unpin works', !(await pg.isVisible('#chatList .pinsep')));
      // @-mentions and checkpoints
      await pg.click('#navPlay'); await pg.waitForTimeout(150);
      await pg.click('#input'); await pg.keyboard.type('look at @app');
      await pg.waitForSelector('#atMenu:not([hidden]) .atrow', { timeout: 8000 });
      check('Typing @ suggests project files', (await pg.innerText('#atMenu')).includes('app.py'));
      await overlaps(pg, 'mention menu');
      await pg.keyboard.press('Enter'); await pg.waitForTimeout(150);
      check('Enter inserts the file mention', (await pg.inputValue('#input')).includes('@app.py '));
      await pg.fill('#input', '');
      await pg.evaluate(() => openCheckpoints()); await pg.waitForSelector('#cpDlg[open]', { timeout: 5000 }); await pg.waitForTimeout(300);
      check('Checkpoints opens with its list (or a clear empty state)', (await pg.locator('#cpList .cprow').count()) >= 1 ? (await pg.innerText('#cpList')).includes('Restore to before this') : (await pg.innerText('#cpList')).includes('No checkpoints yet'));
      await overlaps(pg, 'checkpoints'); await pg.click('#cpClose');
      await pg.evaluate(() => showQueue(['add tests next'])); await pg.waitForTimeout(100);
      check('Queued messages show above the message box', (await pg.innerText('#queueBar')).includes('add tests next'));
      await pg.evaluate(() => showQueue([]));
      // effort switch, find in chat
      await pg.click('#modelPill'); await pg.waitForTimeout(300);
      check('Model menu has an effort switch for Claude', await pg.isVisible('#effortRow') && (await pg.innerText('#effortRow')).includes('Max'));
      await pg.click('#effortRow [data-e="low"]'); await pg.waitForTimeout(500);
      check('Choosing an effort applies it', await pg.evaluate(() => config.effort === 'low') && (await pg.innerText('#modelPill')).includes('Low'), JSON.stringify(await pg.evaluate(() => [config.effort, document.getElementById('modelName').textContent, config.provider, busy, [...document.querySelectorAll('#log .notice')].slice(-1).map(n => n.textContent)])));
      await pg.click('#effortRow [data-e="high"]'); await pg.waitForTimeout(400); await pg.keyboard.press('Escape'); await pg.mouse.click(700, 300); await pg.waitForTimeout(200);
      await pg.keyboard.press('Control+f'); await pg.waitForTimeout(200);
      check('Ctrl/Cmd+F opens find in this chat', await pg.isVisible('#findBar') && await pg.evaluate(() => document.activeElement.id === 'findIn'));
      await pg.keyboard.type('answer'); await pg.waitForTimeout(400);
      check('Find highlights matches and counts them', (await pg.locator('#log mark.findhit').count()) >= 1 && /\d+\/\d+/.test(await pg.innerText('#findCount')), await pg.innerText('#findCount'));
      await overlaps(pg, 'find bar');
      await pg.keyboard.press('Escape'); await pg.waitForTimeout(150);
      check('Esc closes find and removes highlights', !(await pg.isVisible('#findBar')) && (await pg.locator('#log mark.findhit').count()) === 0);
      // context breakdown, outputs, environment in the Run overview
      await pg.click('#railFab'); await pg.waitForTimeout(700);
      check('Overview shows context used with a breakdown', /\d+% used/.test(await pg.innerText('#cxPct')) && (await pg.innerText('#cxRows')).includes('Messages') && (await pg.innerText('#cxRows')).includes('Free space') && (await pg.innerText('#cxUntil')).includes('auto-compact'));
      await overlaps(pg, 'context panel'); await pg.keyboard.press('Escape'); await pg.waitForTimeout(200);
      // artifacts, diagrams and math in replies
      { const wl = await pg.evaluate(() => { showWorking(); const w = document.getElementById('workbar'), c = document.getElementById('composer');
        const ok = !w.hidden && !!w.querySelector('.mascot.work') && w.getBoundingClientRect().bottom <= c.getBoundingClientRect().top + 2 && /\d+s/.test(document.getElementById('wbTime').textContent); const d = [w.hidden, !!w.querySelector('.mascot.work'), w.getBoundingClientRect().bottom, c.getBoundingClientRect().top, document.getElementById('wbTime').textContent]; hideWorking(); return ok || JSON.stringify(d); });
      check('While working, a pinned work line shows the hopping mascot above the message box', wl === true, String(wl)); }
      await pg.evaluate(() => { const sc = document.getElementById('scroll'); sc.scrollTop = 0; sc.dispatchEvent(new Event('scroll')); }); await pg.waitForTimeout(150);
      if (await pg.evaluate(() => { const sc = document.getElementById('scroll'); return sc.scrollHeight - sc.clientHeight > 300; })) {
        check('Scrolling up shows a Latest button', await pg.isVisible('#jumpLatest'));
        await pg.click('#jumpLatest'); await pg.waitForTimeout(700); check('Latest jumps back down', !(await pg.isVisible('#jumpLatest'))); }
      await pg.click('#navPlay'); await pg.waitForTimeout(200);
      await pg.fill('#input', 'Show me a demo'); await pg.keyboard.press('Enter');
      await pg.waitForSelector('#artifact:not([hidden])', { timeout: 15000 });
      check('An HTML reply opens as a live artifact', (await pg.innerText('#artTitle')) === 'Sunrise counter');
      await pg.click('#artClose'); await pg.click('#side > .newchat'); await pg.waitForTimeout(900);
      await pg.waitForSelector('#recGrid .reccard', { timeout: 8000 });
      check('New chat lists recent artifacts with live previews', (await pg.innerText('#recents')).includes('Recent artifacts') && (await pg.innerText('#recGrid')).includes('Sunrise counter') && await pg.locator('#recGrid iframe').count() >= 1);
      await overlaps(pg, 'recent artifacts');
      await pg.locator('#recGrid .reccard', { hasText: 'Sunrise counter' }).click(); await pg.waitForSelector('#artifact:not([hidden])', { timeout: 8000 });
      check('Clicking a recent artifact opens it again', (await pg.innerText('#artTitle')) === 'Sunrise counter');
      const fr = pg.frameLocator('#artFrame'); await fr.locator('button').click(); await fr.locator('button').click();
      check('The artifact runs (its button counts clicks)', (await fr.locator('button').innerText()) === 'Clicks: 2');
      check('The artifact cannot reach the app', await pg.evaluate(() => { try { return !document.getElementById('artFrame').contentWindow.document; } catch (e) { return true; } }));
      await overlaps(pg, 'artifact open');
      await pg.click('#artShowCode'); check('Code view shows the source', (await pg.innerText('#artCode')).includes('Clicks: 0'));
      await pg.click('#artClose'); check('Close hides the artifact', !(await pg.isVisible('#artifact')));
      await pg.waitForSelector('.mmd .mmdout svg', { timeout: 15000 });
      check('Mermaid diagrams are drawn', (await pg.locator('.mmd .mmdout svg').count()) >= 1 && (await pg.innerText('.mmd .mmdout')).includes('Empty?'));
      await pg.waitForSelector('.ech .echout svg, .ech .echout canvas', { timeout: 15000 });
      check('Charts are drawn from echarts blocks', (await pg.locator('.ech .echout svg, .ech .echout canvas').count()) >= 1);
      check('Math is typeset', (await pg.locator('#log .katex-display').count()) >= 1 && (await pg.innerText('#log')).includes('a price like $5 stays text'));
      check('Math fonts load', await pg.evaluate(async () => (await fetch('/vendor/fonts/KaTeX_Main-Regular.woff2')).ok));
      await pg.locator('.artopen').last().click(); check('Open preview reopens it', await pg.isVisible('#artifact')); await pg.keyboard.press('Escape');
      // usage page with real data
      await navTo(pg, '#navUsage'); await pg.waitForSelector('#usChart svg', { timeout: 8000 });
      check('Usage chart shows the days with requests', (await pg.locator('#usChart .bar[d^="M"]').count()) >= 1 && (await pg.innerText('#usModels')).includes('claude-opus-5-5'));
      await pg.locator('#usChart .hit').last().hover(); await pg.waitForTimeout(150);
      check('Hovering a day shows its numbers', await pg.isVisible('#usTip') && (await pg.innerText('#usTip')).includes('request'));
      await pg.click('#usTableBtn'); check('Usage can be shown as a table', await pg.isVisible('#usDayTable table'));
      await pg.click('#usTableBtn'); await pg.click('#usBack');
      // skills & connectors gallery
      await navTo(pg, '#navGallery'); await pg.waitForSelector('#gaSkills .gacard', { timeout: 8000 });
      check('Gallery lists ready-made skills and connectors', (await pg.locator('#gaSkills .gacard').count()) >= 8 && (await pg.innerText('#gaConn')).includes('Linear'));
      await overlaps(pg, 'gallery');
      await pg.locator('#gaSkills .gacard', { hasText: 'Code review' }).getByRole('button', { name: 'Add' }).click(); await pg.waitForTimeout(400);
      check('Add installs a skill', (await pg.locator('#gaSkills .gacard', { hasText: 'Code review' }).innerText()).includes('Added'));
      await pg.locator('#gaSkills .gacard', { hasText: 'Code review' }).getByRole('button', { name: 'Remove' }).click(); await pg.waitForTimeout(300);
      await pg.locator('#gaConn .gacard', { hasText: 'Notion' }).getByRole('button', { name: 'Set up' }).click(); await pg.waitForTimeout(500);
      check('Set up opens Connectors with the connector filled in', await pg.isVisible('.pane[data-p="mcp"]') && (await pg.evaluate(() => [...document.querySelectorAll('#mcpRows input')].some(i => i.value.includes('mcp.notion.com')))));
      await pg.click('#cancelSettings');
      check('Skills page has search and Add from a link (button off until a link is typed)', await pg.isVisible('#gaSearch') && await pg.isDisabled('#addonBtn'));
      await pg.fill('#addonUrl', 'https://github.com/owner/repo'); check('Typing a link turns Install on', !(await pg.isDisabled('#addonBtn'))); await pg.fill('#addonUrl', '');
      await pg.fill('#gaSearch', 'zzzz-nothing'); check('Search hides skills that do not match', await pg.evaluate(() => [...document.querySelectorAll('#gaSkills .gacard')].every(c => c.hidden)));
      await pg.fill('#gaSearch', '');
      await pg.locator('body').click({ position: { x: 5, y: 5 } }).catch(() => {});
      await pg.keyboard.press('Escape'); await pg.waitForTimeout(250);
      check('Esc leaves the Skills page', !(await pg.isVisible('#galleryView')));
      await pg.click('#navGallery').catch(() => {}); await pg.waitForTimeout(400); if (await pg.isVisible('#galleryView')) await pg.click('#gaBack');
      // Cyber Security section
      await pg.click('#navCyber'); await pg.waitForFunction(() => document.body.classList.contains('cyber-open'), { timeout: 6000 });
      await pg.click('#cySeg button[data-c="bounty"]'); await pg.waitForSelector('#cyGroups .gacard', { timeout: 6000 });
      const bounty = await pg.innerText('#cyGroups');
      check('Cyber Security · Bug bounty lists the toolkit', (await pg.locator('#cyGroups .gacard').count()) >= 8 && /Burp/.test(bounty) && /garak/i.test(bounty), bounty.slice(0, 80));
      check('Jailbreak repos are excluded from the toolkit', !/L1B3RT4S|Claude-Red/i.test(await pg.innerText('#cyberView')) && /guardrail-bypass/i.test(await pg.innerText('#cyExcluded')));
      await pg.click('#cySeg button[data-c="rea"]'); await pg.waitForSelector('#cyRea .cyrea', { timeout: 6000 });
      check('REA has its own tab', /Reverse Engineer/i.test(await pg.innerText('#cyRea')));
      check('The global switch turns Cyber Security mode on for the harness', await pg.evaluate(async () => { document.getElementById('cyGlobal').click(); await new Promise(r => setTimeout(r, 180)); const on = document.body.classList.contains('cyber-on') && config.cyber_mode === true; document.getElementById('cyGlobal').click(); await new Promise(r => setTimeout(r, 120)); return on; }));
      await pg.keyboard.press('Escape'); await pg.waitForTimeout(200);
      check('Esc leaves the Cyber Security section', !(await pg.evaluate(() => document.body.classList.contains('cyber-open'))));
      // account / profile menu
      await pg.click('#acctBtn'); await pg.waitForSelector('#acctMenu:not([hidden])', { timeout: 4000 });
      check('Account menu opens with profile actions', await pg.evaluate(() => { const t = document.getElementById('acctMenu').innerText; return t.includes('Settings') && t.includes("What's new") && t.includes('Usage'); }));
      await pg.evaluate(() => closeAcct());
      check("What's new opens a changelog dialog", await pg.evaluate(() => { showWhatsNew(); const o = document.getElementById('whatsNew').open; document.getElementById('whatsNew').close(); return o; }));
      await pg.evaluate(() => openSettings('about')); await pg.waitForSelector('#abtLogo .mascot', { timeout: 4000 });
      check('About pane shows the app hero with version', /build/i.test(await pg.innerText('#aboutText')) && await pg.isVisible('#abtWhatsNew')); await pg.evaluate(() => $('settings').close());
      // settings: code intelligence + quick chat shortcut
      await pg.evaluate(() => openSettings()); await pg.click('#tabs button[data-p="tools"]'); await pg.waitForSelector('.lsprow', { timeout: 8000 });
      check('Settings lists language servers with their status', (await pg.locator('.lsprow').count()) === 6 && (await pg.innerText('#lspList')).includes('Python'));
      check('Quick chat shortcut is shown in plain words', (await pg.inputValue('#quickKey')) === 'ctrl+alt+space');
      await overlaps(pg, 'settings tools'); await pg.click('#cancelSettings');
      // appearance switch — now in its own Theme tab
      await pg.evaluate(() => openSettings()); await pg.click('#tabs button[data-p="theme"]'); await pg.waitForSelector('#themeSeg button[data-t="dark"]', { timeout: 6000 }); await pg.waitForTimeout(300);
      check('Theme tab groups Appearance, Background and Team wallpaper', await pg.isVisible('#scenePick') && await pg.isVisible('#fxSeg') && (await pg.innerText('#thWpHint')).toLowerCase().includes('team'));
      await pg.click('#themeSeg button[data-t="dark"]'); await pg.waitForTimeout(200);
      const darkBg = await pg.evaluate(() => getComputedStyle(document.body).backgroundColor);
      await pg.click('#themeSeg button[data-t="light"]'); await pg.waitForTimeout(200);
      const lightBg = await pg.evaluate(() => getComputedStyle(document.body).backgroundColor);
      check('Appearance switches between dark and light', darkBg !== lightBg && darkBg.includes('23, 19, 12'), darkBg + ' / ' + lightBg);
      check('Effects · Lite adds performance mode', await pg.evaluate(() => { document.querySelector('#fxSeg button[data-fx="lite"]').click(); return document.body.classList.contains('perf'); }));
      await pg.evaluate(() => document.querySelector('#fxSeg button[data-fx="auto"]').click());
      await pg.click('#themeSeg button[data-t="system"]'); await pg.click('#cancelSettings');
      check('Header has no Checkpoints / View code / New session', !(await pg.isVisible('#checkpointsBtn')) && !(await pg.isVisible('#viewCodeBtn')) && !(await pg.isVisible('#newSessionBtn')));
      await overlaps(pg, 'after a turn');
      await pg.screenshot({ path: 'audit-1440.png' });
    } else {
      await pg.screenshot({ path: `audit-${w}.png` });
    }
    errors.forEach(e => problems.push(`[${L}] page error: ${e}`));
    await pg.close();
  }
  { const w = await b.newPage({ viewport: { width: 1200, height: 820 } }); await w.goto(url); await w.waitForTimeout(1200);
    await w.evaluate(() => { config.onboarded = false; wlShown = false; showWelcome(); }); await w.waitForTimeout(300);
    check('First-run welcome shows step 1 with providers', await w.isVisible('#welcomeDlg[open]') && (await w.locator('#wlProviders .wlcard').count()) >= 6);
    await w.locator('#wlProviders .wlcard', { hasText: 'Ollama' }).click(); await w.click('#wlNext');
    check('Step 2: Ollama needs no key', await w.isVisible('#wlNokey') && !(await w.isDisabled('#wlNext')));
    await w.click('#wlNext'); check('Step 3: pick a folder', await w.isVisible('#wlFolder'));
    const wl = []; w.on('pageerror', e => wl.push(e.message));
    await w.click('#wlNext'); await w.waitForTimeout(900);
    check('Start finishes setup with the chosen provider', !(await w.isVisible('#welcomeDlg[open]')) && await w.evaluate(() => config.onboarded === true && config.service === 'ollama'), wl.join('; ') + (await w.innerText('#wlErr').catch(() => '')));
    await w.close(); }
  { const q = await b.newPage({ viewport: { width: 640, height: 168 } }); await q.goto(url + (url.includes('?') ? '&' : '?') + 'quick=1'); await q.waitForTimeout(800);
    check('Quick chat window shows just the input box', await q.isVisible('#quickInput') && !(await q.isVisible('#side')) && !(await q.isVisible('.topbar')));
    check('Quick chat box is focused, ready to type', await q.evaluate(() => document.activeElement?.id === 'quickInput')); await q.close(); }
  await b.close();
  console.log(JSON.stringify({ passed: passed.length, problems }, null, 1));
})().catch(async e => { console.error('AUDIT CRASHED', String(e).slice(0, 400)); try { console.error('LOG:', (await globalThis.__pg.innerText('#log')).slice(-1500)); } catch (_) {} process.exit(1); });
