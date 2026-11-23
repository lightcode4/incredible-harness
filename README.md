# Incredible Herness

A desktop AI agent for macOS and Windows. Chat with it and it works in a folder you choose: runs commands,
reads and edits files, searches the web, and asks before every change.

## Features
- **Models:** Claude (default `claude-opus-5-5`), OpenAI or any OpenAI-compatible API (OpenRouter, Groq, …),
  or free local models (Ollama, LM Studio).
- **Approvals:** Allow / Always allow / Deny for every command and file change, with a diff of each edit first.
- **Permission rules** per folder: e.g. always allow `bash(npm test*)`, never allow `bash(git push*)`.
- **Undo:** every message that changed files has an Undo button that restores them.
- **Plan mode:** the agent researches and proposes a plan; nothing changes until you approve it.
- **Task list:** the agent tracks multi-step work in a live checklist.
- **Chat history:** chats are saved and can be reopened from the sidebar.
- **Cost meter and spending limit** per chat; prompt caching and automatic context compaction to keep costs down.
- **PDFs:** attach them, or let the agent read PDFs in your folder or from links (`read_pdf`). With Claude it sees
  the real pages — tables, charts and scanned pages; other providers get the extracted text.
- **Web search & fetch**, **attachments** (images, PDFs, text files — drag, paste or 📎), **background jobs**
  (dev servers, long builds), **sub-agents** for research, **previews** of pages and local servers.
- **Project instructions:** put notes in `HERNESS.md` (or `CLAUDE.md` / `AGENTS.md`) in your folder.
- **Terminal panel:** a real terminal in your working folder (zsh on Mac, PowerShell on Windows). Ctrl+` toggles it.
- **Built-in browser:** Herness's own Chromium, shown live in a side panel. The agent navigates, reads, clicks, types and
  takes screenshots to check its work; you can browse and click in it too. Clicks and typing by the agent ask first.
- **Git panel:** changed files with diffs, commit, branches, pull/push and pull requests (with the GitHub CLI).
- **Parallel chats:** several agents at once; a *Parallel chat (worktree)* works on its own branch in its own folder,
  then merges back with one click.
- **Code checks after every edit** (Python, JavaScript, TypeScript, JSON) and **hooks**: commands that run after each
  edit or before the agent finishes (e.g. `npm test`) — failures go back to the agent to fix.
- **Memory** across chats, **slash commands** (`/review`, `/init`, `/test`, `/commit`, `/fix`, `/explain`, plus your own
  in `.herness/commands/*.md` or `~/.incredible-herness/commands/*.md`), **Jupyter notebooks**, **export** (`/export`).
- **Scheduled tasks** that run while the app is open (only actions your rules allow).
- **Phone access** on your Wi-Fi (scan a QR code), **voice typing**, and optional **computer use** (screen control,
  every action asks first).
- **Connectors:** local MCP servers (programs on your computer: files, SQLite, Postgres, Slack, …) for every provider,
  plus remote MCP servers for Claude, with quick-connect presets. Every connector tool call asks for approval.
- **Command sandbox** (macOS, Linux): commands can only change the project folder; strict mode also blocks the internet.
- **Model routing:** sub-agents use a cheaper model (Claude Haiku 5.5 by default) while the main agent uses yours.
- **Share** a chat as a secret GitHub gist, open **several windows**, drag to **reorder chats**, **keyboard shortcuts**
  (Ctrl/⌘+/), the app in **English, Spanish, French, Portuguese or German**, a **reply language** setting, and **import/export of settings** between computers.
- **Updates install themselves:** "Install & restart" downloads the new version and relaunches.
- **Problem reports:** logs in `~/.incredible-herness/logs` (secrets removed) and a "Report a problem" button.
- **Update check** from Settings → Updates.

## Install
Download from **Releases**: `Incredible-Herness-mac.dmg` (Apple silicon) or `Incredible-Herness-Setup.exe`.
- Mac: drag to Applications. Unsigned builds: right-click → Open the first time.
- Windows: if SmartScreen appears, More info → Run anyway.

## Signed installers (optional)
Add these repository secrets and the build signs automatically:
- macOS: `MACOS_CERT_P12` (base64 Developer ID Application .p12), `MACOS_CERT_PASSWORD`, `MACOS_SIGN_IDENTITY`,
  and for notarization `APPLE_ID`, `APPLE_APP_PASSWORD`, `APPLE_TEAM_ID`.
- Windows: `WINDOWS_CERT_PFX` (base64 .pfx), `WINDOWS_CERT_PASSWORD`.

## Develop
    pip install anthropic openai pywebview playwright pypdf   # + pywinpty on Windows
    python -m playwright install chromium-headless-shell
    python run_app.py          # desktop window
    python -m myagent          # terminal version
    python test_loop.py        # offline tests: fake API server, no tokens used
    python test_gui.py         # interface test (needs Node + Playwright)

## License
MIT. See [LICENSE](LICENSE).

## Interface audit
    python tests/ui_audit.py   # clicks through every section at 3 window sizes, light+dark; reports overlaps and errors
