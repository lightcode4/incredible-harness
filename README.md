# Incredible Herness

A desktop AI agent for macOS and Windows. Chat with it, and it runs commands and reads, writes and edits
files in a folder you choose. It asks before every command or file change (Allow / Always allow / Deny).

Works with:
- **Claude** (default model `claude-opus-5-5`), via the official Anthropic SDK
- **OpenAI** or any OpenAI-compatible API (OpenRouter, Groq, Together, ...) via Base URL
- **Local models** via Ollama (`http://localhost:11434/v1`) or LM Studio (`http://localhost:1234/v1`): free

## Get the installers
GitHub → Actions → **Incredible Herness build** → latest run → Artifacts:
- `Incredible-Herness-mac` → `Incredible-Herness-mac.dmg` (Apple silicon)
- `Incredible-Herness-windows` → `Incredible-Herness-Setup.exe`

The apps are not code-signed:
- **Mac:** open the .dmg, drag the app to Applications. First launch: right-click → Open
  (or System Settings → Privacy & Security → Open Anyway).
- **Windows:** if SmartScreen appears, click More info → Run anyway. Needs the Edge WebView2 runtime
  (built into Windows 10/11).

## Setup in the app
Pick a provider, paste your API key, choose a folder, press **Apply**. Settings are saved in
`~/.incredible-herness/settings.json` (readable only by your user account).

## Saving tokens
- Nothing runs on its own: tokens are only used when you send a message.
- Lower **Effort** (low/medium) or use a cheaper model for simple tasks.
- **New chat** when you change topic: the whole conversation is sent with each request.
- The Local provider costs nothing.

## Develop
    pip install anthropic openai pywebview
    python run_app.py            # desktop window
    python -m myagent            # terminal version
    python test_loop.py          # offline tests (fake servers, no API calls)
