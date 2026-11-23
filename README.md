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
- **Web search & fetch**, **attachments** (images, PDFs, text files — drag, paste or 📎), **background jobs**
  (dev servers, long builds), **sub-agents** for research, **previews** of pages and local servers.
- **Project instructions:** put notes in `HERNESS.md` (or `CLAUDE.md` / `AGENTS.md`) in your folder.
- **Connectors:** remote MCP servers (Claude provider).
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
    pip install anthropic openai pywebview
    python run_app.py          # desktop window
    python -m myagent          # terminal version
    python test_loop.py        # offline tests: fake API server, no tokens used
    python test_gui.py         # interface test (needs Node + Playwright)
