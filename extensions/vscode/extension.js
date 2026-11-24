// LightCode⁴ for VS Code: shows the LightCode⁴ app beside your code and sends it what you select.
// It talks only to the LightCode⁴ app running on this computer (address and key from ~/.incredible-herness/running.json).
const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");
const { exec } = require("child_process");

const RUNNING = path.join(os.homedir(), ".incredible-herness", "running.json");
let panel = null;

function readRunning() { try { return JSON.parse(fs.readFileSync(RUNNING, "utf8")); } catch { return null; } }

function alive(info) {
  return new Promise(res => {
    if (!info) return res(false);
    const req = http.get(`${info.url}/api/ping?token=${info.token}`, r => { r.resume(); res(r.statusCode < 500); });
    req.on("error", () => res(false)); req.setTimeout(1500, () => { req.destroy(); res(false); });
  });
}

async function ensureApp() {
  let info = readRunning();
  if (await alive(info)) return info;
  const cmd = process.platform === "darwin" ? "open -a LightCode4" : process.platform === "win32" ? 'start "" "lightcode4://open"' : "lightcode4";
  exec(cmd);
  return vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: "Starting LightCode⁴…" }, async () => {
    for (let i = 0; i < 40; i++) { await new Promise(r => setTimeout(r, 500)); info = readRunning(); if (await alive(info)) return info; }
    throw new Error("LightCode⁴ isn't running. Open the LightCode⁴ app, then try again.");
  });
}

function post(info, route, body) {
  return new Promise((resolve, reject) => {
    const data = Buffer.from(JSON.stringify(body));
    const u = new URL(info.url + route);
    const req = http.request({ hostname: u.hostname, port: u.port, path: u.pathname, method: "POST",
      headers: { "Content-Type": "application/json", "Content-Length": data.length, "X-Token": info.token } }, r => {
      let out = ""; r.on("data", c => out += c); r.on("end", () => { try { const j = JSON.parse(out || "{}"); j.error ? reject(new Error(j.error)) : resolve(j); } catch (e) { reject(e); } });
    });
    req.on("error", reject); req.end(data);
  });
}

function folderOf(uri) {
  const ws = uri ? vscode.workspace.getWorkspaceFolder(uri) : vscode.workspace.workspaceFolders?.[0];
  return ws ? ws.uri.fsPath : (uri ? path.dirname(uri.fsPath) : os.homedir());
}

async function showPanel(info) {
  if (panel) { panel.reveal(vscode.ViewColumn.Beside, true); return; }
  panel = vscode.window.createWebviewPanel("lightcode4", "LightCode⁴", { viewColumn: vscode.ViewColumn.Beside, preserveFocus: true }, { enableScripts: true, retainContextWhenHidden: true });
  panel.iconPath = vscode.Uri.file(path.join(__dirname, "icon.png"));
  const src = (await vscode.env.asExternalUri(vscode.Uri.parse(`${info.url}/?token=${info.token}`))).toString();
  panel.webview.html = `<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src 'none'; frame-src http://127.0.0.1:* https:; style-src 'unsafe-inline'">
    <style>html,body,iframe{margin:0;padding:0;border:0;width:100%;height:100vh;background:#1b1610}</style>
    <iframe src="${src}" allow="clipboard-read; clipboard-write"></iframe>`;
  panel.onDidDispose(() => { panel = null; });
}

async function run(fn) { try { await fn(); } catch (e) { vscode.window.showErrorMessage(`LightCode⁴: ${e.message}`); } }

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("lightcode4.open", () => run(async () => {
      const info = await ensureApp(); await post(info, "/api/editor/open", { cwd: folderOf(vscode.window.activeTextEditor?.document.uri) }); await showPanel(info); })),
    vscode.commands.registerCommand("lightcode4.folder", (uri) => run(async () => {
      const info = await ensureApp(); await post(info, "/api/editor/open", { cwd: uri?.fsPath || folderOf() }); await showPanel(info); })),
    vscode.commands.registerCommand("lightcode4.ask", () => run(async () => {
      const ed = vscode.window.activeTextEditor; if (!ed) return;
      const sel = ed.selection, text = ed.document.getText(sel) || ed.document.lineAt(sel.active.line).text;
      const cwd = folderOf(ed.document.uri), file = path.relative(cwd, ed.document.uri.fsPath) || path.basename(ed.document.uri.fsPath);
      const info = await ensureApp();
      await post(info, "/api/editor/ask", { cwd, file, text, lang: ed.document.languageId, start: sel.start.line + 1, end: sel.end.line + 1 });
      await showPanel(info); })),
  );
}

module.exports = { activate, deactivate() {} };
