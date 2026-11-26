// Scheduled tasks (Automations): fire a prompt into a fresh session on an interval
// or at a time of day. TS port of the scheduler in gui.py.

import type { Store } from "./config";
import type { EventHub } from "./events";
import type { ChatManager } from "./chats";

type Task = { id: string; name?: string; prompt: string; cwd?: string; at?: string; every?: number; enabled?: boolean; last?: number };

export class Scheduler {
  private timer: ReturnType<typeof setInterval>;

  constructor(private store: Store, private chats: ChatManager, private events: EventHub) {
    this.timer = setInterval(() => this.tick(), 30_000);
  }

  stop(): void { clearInterval(this.timer); }

  private tick(): void {
    const now = Date.now() / 1000;
    for (const task of (this.store.config.schedules as Task[]) || []) {
      try { if (this.due(task, now)) this.run(task, now); } catch { /* keep going */ }
    }
  }

  private due(task: Task, now: number): boolean {
    if (!task.enabled || !task.prompt?.trim()) return false;
    const last = task.last || 0;
    if (task.every && task.every > 0) return now - last >= task.every * 60;
    if (task.at && /^\d{1,2}:\d{2}$/.test(task.at)) {
      const [h, m] = task.at.split(":").map(Number);
      const d = new Date(now * 1000);
      const fire = new Date(d); fire.setHours(h!, m!, 0, 0);
      const fireSec = fire.getTime() / 1000;
      return now >= fireSec && last < fireSec;
    }
    return false;
  }

  run(task: Task, now = Date.now() / 1000): void {
    task.last = now;
    this.store.save();
    this.events.put({ kind: "toast", text: `Running scheduled task: ${task.name || "Task"}` });
    this.chats.newChat(task.cwd || undefined, "code");
    void this.chats.current.send(task.prompt).catch(() => {});
  }

  runById(id: string): boolean {
    const task = ((this.store.config.schedules as Task[]) || []).find((t) => t.id === id);
    if (!task) return false;
    this.run(task);
    return true;
  }
}
