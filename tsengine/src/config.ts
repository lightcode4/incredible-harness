// Config: load/save the settings file and build the public_config the UI consumes.
// Mirrors gui.py's DEFAULTS, load(), save() and public_config().

import { homedir } from "os";
import { join } from "path";
import { mkdirSync } from "fs";

export const BUILD = Number(process.env.LC4_BUILD || 0);
export const VERSION = "0.1.0-ts";
export const DEFAULT_MODELS: Record<string, string> = {
  claude: "claude-opus-5-5",
  openai: "gpt-5",
  local: "llama3.1",
};

export const HOME = join(homedir(), ".incredible-herness");
export const CONFIG_FILE = join(HOME, "config.json");

export type Config = Record<string, unknown>;

// The persisted defaults — kept in sync with gui.py's self.config defaults.
export function defaults(): Config {
  return {
    provider: "claude",
    model: DEFAULT_MODELS.claude,
    effort: "high",
    base_url: "",
    api_key: "",
    cwd: homedir(),
    web: true,
    compaction: true,
    budget: 0.0,
    mcp: [],
    rules: {},
    hooks: {},
    github_token: "",
    diagnostics: true,
    computer: false,
    schedules: [],
    phone: false,
    voice_key: "",
    auto: false,
    plan: false,
    fast_model: "",
    mcp_local: [],
    sandbox: "off",
    language: "",
    chat_order: [],
    recent_folders: [],
    ui_language: "en",
    service: "",
    keys: {},
    service_models: {},
    projects: [],
    quick_hotkey: "<ctrl>+<alt>+<space>",
    pinned: [],
    titles: {},
    theme: "system",
    sound: false,
    onboarded: false,
    name: "",
    mode: "code",
    services: {},
    github_user: {},
    github_folder: "",
    style: "normal",
    style_custom: "",
    sync_chats: false,
    last_sync: {},
    bridge_key: "",
    teams: [],
    team_shares: {},
    auto_update: true,
    scene: "auto",
    cyber_mode: false,
    effects: "auto",
    last_chat: "",
  };
}

const HIDDEN = new Set([
  "api_key", "github_token", "rules", "hooks", "voice_key",
  "mcp_local", "keys", "services", "bridge_key",
]);

export class Store {
  config: Config;

  constructor() {
    this.config = defaults();
    this.load();
  }

  load(): void {
    try {
      const saved = JSON.parse(require("fs").readFileSync(CONFIG_FILE, "utf8")) as Config;
      for (const [k, v] of Object.entries(saved)) {
        if (k in this.config && k !== "auto" && k !== "plan") this.config[k] = v;
      }
    } catch {
      /* first run or unreadable — keep defaults */
    }
  }

  save(): void {
    try {
      mkdirSync(HOME, { recursive: true });
      require("fs").writeFileSync(CONFIG_FILE, JSON.stringify(this.config, null, 2));
    } catch {
      /* best effort */
    }
  }

  // What the UI receives — secrets are replaced with booleans, plus computed fields.
  publicConfig(extra: Partial<Record<string, unknown>> = {}): Config {
    const c: Config = {};
    for (const [k, v] of Object.entries(this.config)) if (!HIDDEN.has(k)) c[k] = v;
    c.api_key = Boolean(this.config.api_key);
    c.github_token = Boolean(this.config.github_token);
    c.voice_key = Boolean(this.config.voice_key);
    c.update_ready = null;
    c.rules = { allow: [], deny: [] };
    c.hooks = {};
    c.instructions = [];
    c.chat_cwd = this.config.cwd;
    c.chat_project = null;
    c.has_chats = false;
    c.services = { keys_set: [] };
    c.chat_kind = this.config.mode || "code";
    c.home_dir = homedir();
    c.build = BUILD;
    c.version = VERSION;
    c.phone_url = "";
    c.remote_url = "";
    c.context_window = (this.config as any).context_window || 0;
    c.commands = [];
    c.mcp_local = [];
    c.mcp = [];
    c.mcp_status = [];
    c.fast_model_default = "";
    c.keys_set = Object.entries((this.config.keys as Record<string, unknown>) || {})
      .filter(([, v]) => v).map(([k]) => k).sort();
    c.sandbox_available = false;
    return { ...c, ...extra };
  }
}
