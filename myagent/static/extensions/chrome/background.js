// LightCode⁴ for Chrome: asks the LightCode⁴ app on this computer for work, does it in Chrome, sends the result back.
// Pairing code (from LightCode⁴ > Settings > Tools > Chrome) = "<port>-<key>". Nothing leaves your computer.
let running = false;

async function pairing() {
  const { code } = await chrome.storage.local.get("code");
  const m = /^(\d+)-(\w+)$/.exec((code || "").trim());
  return m ? { base: `http://127.0.0.1:${m[1]}`, key: m[2] } : null;
}

async function post(p, path, body) {
  const r = await fetch(p.base + path, { method: "POST", headers: { "Content-Type": "application/json", "X-LC4-Key": p.key }, body: JSON.stringify(body) });
  if (r.status === 401) throw new Error("wrong pairing code");
  return r.json();
}

async function activeTab(id) {
  if (id) return chrome.tabs.get(id);
  const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!t) throw new Error("no active tab");
  return t;
}

async function inPage(tabId, func, args = []) {
  const [r] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return r?.result;
}

function waitLoad(tabId) {
  return new Promise(res => {
    const done = (id, info) => { if (id === tabId && info.status === "complete") { chrome.tabs.onUpdated.removeListener(done); res(); } };
    chrome.tabs.onUpdated.addListener(done); setTimeout(() => { chrome.tabs.onUpdated.removeListener(done); res(); }, 15000);
  });
}

const actions = {
  async tabs() {
    return (await chrome.tabs.query({})).map(t => ({ id: t.id, title: t.title, url: t.url, active: t.active }));
  },
  async read({ tab }) {
    const t = await activeTab(tab);
    return inPage(t.id, () => {
      const links = [...document.links].slice(0, 150).map(a => `- ${a.innerText.trim().slice(0, 80)} → ${a.href}`).filter(l => !l.startsWith("-  →"));
      const fields = [...document.querySelectorAll("input, textarea, select, button")].slice(0, 80)
        .map(e => `- <${e.tagName.toLowerCase()}${e.id ? " #" + e.id : ""}${e.name ? " name=" + e.name : ""}${e.type ? " type=" + e.type : ""}> ${(e.labels?.[0]?.innerText || e.placeholder || e.innerText || e.value || "").trim().slice(0, 60)}`);
      return { title: document.title, url: location.href,
               text: document.body.innerText.slice(0, 40000) + "\n\nLinks:\n" + links.join("\n") + "\n\nFields and buttons:\n" + fields.join("\n") };
    });
  },
  async screenshot() {
    const t = await activeTab();
    return { url: t.url, dataUrl: await chrome.tabs.captureVisibleTab(t.windowId, { format: "png" }) };
  },
  async open({ url, new_tab = true, tab }) {
    if (tab) { const t = await chrome.tabs.update(tab, { active: true }); await chrome.windows.update(t.windowId, { focused: true }); return { id: t.id, url: t.url }; }
    if (!/^https?:\/\//.test(url || "")) url = "https://" + url;
    const t = new_tab ? await chrome.tabs.create({ url }) : await chrome.tabs.update((await activeTab()).id, { url });
    await waitLoad(t.id);
    const now = await chrome.tabs.get(t.id);
    return { id: now.id, url: now.url };
  },
  async click({ text, selector, tab }) {
    const t = await activeTab(tab);
    const r = await inPage(t.id, (text, selector) => {
      let el = selector ? document.querySelector(selector) : null;
      if (!el && text) {
        const want = text.trim().toLowerCase();
        el = [...document.querySelectorAll("a, button, [role=button], input[type=submit], input[type=button], label, summary, [onclick]")]
          .find(e => (e.innerText || e.value || e.getAttribute("aria-label") || "").trim().toLowerCase().includes(want));
      }
      if (!el) return "Nothing matched to click.";
      el.scrollIntoView({ block: "center" }); el.click();
      return `Clicked ${el.tagName.toLowerCase()} “${(el.innerText || el.value || "").trim().slice(0, 60)}”.`;
    }, [text || "", selector || ""]);
    await new Promise(r => setTimeout(r, 800));
    return r;
  },
  async type({ text, selector, submit, tab }) {
    const t = await activeTab(tab);
    return inPage(t.id, (text, selector, submit) => {
      const el = selector ? document.querySelector(selector) : document.activeElement;
      if (!el || !("value" in el || el.isContentEditable)) return "No text field to type into (give a selector).";
      el.focus();
      if (el.isContentEditable) el.textContent = text;
      else { const set = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")?.set; set ? set.call(el, text) : (el.value = text); }
      el.dispatchEvent(new Event("input", { bubbles: true })); el.dispatchEvent(new Event("change", { bubbles: true }));
      if (submit) { if (el.form) el.form.requestSubmit ? el.form.requestSubmit() : el.form.submit();
        else el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true })); }
      return `Typed ${text.length} characters${submit ? " and submitted" : ""}.`;
    }, [text, selector || "", !!submit]);
  },
};

async function loop() {
  if (running) return;
  running = true;
  try {
    while (true) {
      const p = await pairing();
      if (!p) { await chrome.storage.local.set({ status: "Not paired yet" }); break; }
      let res;
      try { res = await post(p, "/chrome/next", { wait: 25, browser: navigator.userAgent.match(/Chrome\/[\d.]+/)?.[0] || "Chrome" }); }
      catch (e) { await chrome.storage.local.set({ status: e.message === "wrong pairing code" ? "Wrong pairing code" : "LightCode⁴ isn't running" }); break; }
      await chrome.storage.local.set({ status: "Connected" });
      const job = res.job;
      if (!job) continue;
      let ok = true, data;
      try { data = await actions[job.action](job.args || {}); if (data === undefined) data = null; }
      catch (e) { ok = false; data = String(e.message || e); }
      await post(p, "/chrome/result", { id: job.id, ok, data }).catch(() => {});
    }
  } finally { running = false; }
}

chrome.alarms.create("lc4", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => loop());
chrome.runtime.onStartup.addListener(() => loop());
chrome.runtime.onInstalled.addListener(() => loop());
chrome.storage.onChanged.addListener(ch => { if (ch.code) loop(); });
loop();
