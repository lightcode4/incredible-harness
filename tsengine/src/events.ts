// Event bus: the engine pushes events, every open SSE connection receives them.
// Mirrors the Python EventHub (gui.py) — the UI consumes these by `kind`.

export type Event = { kind: string; [k: string]: unknown };
type Sink = (ev: Event) => void;

export class EventHub {
  private sinks = new Set<Sink>();

  subscribe(sink: Sink): () => void {
    this.sinks.add(sink);
    return () => this.sinks.delete(sink);
  }

  /** Broadcast an event to every connected client. */
  put(ev: Event): void {
    for (const sink of [...this.sinks]) {
      try {
        sink(ev);
      } catch {
        this.sinks.delete(sink);
      }
    }
  }

  get listeners(): number {
    return this.sinks.size;
  }
}
