// Self-check: quick health tests for the TS engine (Settings > Updates > Run self-check).
// TS-appropriate version — tests the engine's real moving parts, no API spend unless
// model_test is requested.

import { mkdtempSync, writeFileSync, rmSync, readFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import type { Store } from "./config";
import { testConnection } from "./models";

type Result = { name: string; ok: boolean; detail: string; secs: number };

async function check(name: string, fn: () => Promise<string> | string): Promise<Result> {
  const t0 = Date.now();
  try {
    const detail = (await fn()) || "ok";
    return { name, ok: true, detail: String(detail).slice(0, 300), secs: (Date.now() - t0) / 1000 };
  } catch (e) {
    return { name, ok: false, detail: `${(e as Error).name}: ${(e as Error).message}`.slice(0, 400), secs: (Date.now() - t0) / 1000 };
  }
}

export async function run(store: Store, cwd: string, modelTest = false): Promise<Result[]> {
  const out: Result[] = [];

  out.push(await check("Working folder", () => {
    const f = join(mkdtempSync(join(tmpdir(), "lc4-check-")), "probe.txt");
    writeFileSync(f, "ok"); const back = readFileSync(f, "utf8"); rmSync(f);
    if (back !== "ok") throw new Error("read-back mismatch");
    return "can create, write and read files";
  }));

  out.push(await check("Shell", async () => {
    const p = Bun.spawn(["bash", "-lc", "echo lc4-ok"], { cwd, stdout: "pipe" });
    const t = (await new Response(p.stdout).text()).trim(); await p.exited;
    if (!t.includes("lc4-ok")) throw new Error("shell did not run");
    return "ran a command and captured output";
  }));

  out.push(await check("Git", () => {
    const p = Bun.spawnSync(["git", "--version"]);
    if (p.exitCode !== 0) throw new Error("git not found");
    return p.stdout.toString().trim();
  }));

  out.push(await check("Runtime", () => `Bun ${Bun.version}`));

  out.push(await check("Provider key", () => {
    const cfg = store.config, keys = (cfg.keys as any) || {};
    if (!cfg.api_key && !keys[cfg.service as string] && !keys[cfg.provider as string]) throw new Error("no API key configured");
    return `provider ${cfg.provider}, model ${cfg.model}`;
  }));

  if (modelTest) {
    out.push(await check("Model reply", async () => {
      const reply = await testConnection(store.config);
      return `model replied: ${reply.slice(0, 40)}`;
    }));
  }

  return out;
}
