// A chat session: holds the transcript and runs the agent turn loop, emitting
// the same events the Python Chat emits (user / text / tool / tool_result / turn_end / status / usage).

import type { EventHub } from "./events";
import type { Store } from "./config";
import { streamTurn, type Msg } from "./providers";
import { TOOL_SPECS, runTool } from "./tools";

const CYBER_PREAMBLE =
  "Cyber Security mode is ON. The user is authorized to test systems and apps they own, " +
  "in a lab they control. Help with security review, bug hunting and defensive analysis. " +
  "Refuse anything that targets third parties or enables real-world harm.\n\n";

export class Session {
  transcript: Msg[] = [];
  busy = false;
  private stopFlag = false;

  constructor(private store: Store, private events: EventHub) {}

  private system(): string {
    const cfg = this.store.config;
    const cyber = cfg.cyber_mode ? CYBER_PREAMBLE : "";
    return (
      cyber +
      `You are LightCode⁴, a coding agent working in the folder ${cfg.cwd}. ` +
      `Be concise and act directly. Use the provided tools to read and change files and run commands.`
    );
  }

  replayEvent() {
    return {
      kind: "replay",
      transcript: this.transcript.map((m) => ({ role: m.role, content: m.content })),
    };
  }

  stop(): void {
    this.stopFlag = true;
  }

  async send(text: string): Promise<void> {
    if (this.busy) throw new Error("The agent is already working. Stop it first.");
    const cfg = this.store.config;
    if (!cfg.api_key && !(cfg.keys as any)?.[cfg.service as string] && !(cfg.keys as any)?.[cfg.provider as string]) {
      this.events.put({ kind: "setup", text: "Add a provider API key in Settings first." });
      throw new Error("No API key configured.");
    }

    this.stopFlag = false;
    this.busy = true;
    this.events.put({ kind: "user", text });
    this.events.put({ kind: "status", busy: true });
    this.transcript.push({ role: "user", content: text });

    try {
      for (let step = 0; step < 24 && !this.stopFlag; step++) {
        const result = await streamTurn(
          cfg, this.system(), this.transcript, TOOL_SPECS,
          { onText: (d) => this.events.put({ kind: "text", text: d }),
            onThinking: (d) => this.events.put({ kind: "thinking", text: d }) },
        );

        if (result.text) this.transcript.push({ role: "assistant", content: result.text });
        this.events.put({ kind: "end_block" });
        this.events.put({
          kind: "usage",
          cost: 0, priced: false,
          tokens: result.usage.input + result.usage.output,
          context: result.usage.input, window: 0,
        });

        if (!result.toolCalls.length || this.stopFlag) break;

        const outs: string[] = [];
        for (const tc of result.toolCalls) {
          if (this.stopFlag) break;
          this.events.put({ kind: "tool", id: tc.id, name: tc.name, input: tc.args, summary: tc.name });
          let out: string;
          try { out = await runTool(String(cfg.cwd), tc.name, tc.args); }
          catch (e) { out = `Error: ${(e as Error).message}`; }
          this.events.put({ kind: "tool_result", id: tc.id, output: out });
          outs.push(`[${tc.name}] ${out}`);
        }
        this.transcript.push({ role: "user", content: "Tool results:\n" + outs.join("\n\n") });
      }
    } catch (e) {
      this.events.put({ kind: "notice", text: (e as Error).message });
    } finally {
      this.busy = false;
      this.events.put({ kind: "turn_end" });
      this.events.put({ kind: "status", busy: false });
    }
  }
}
