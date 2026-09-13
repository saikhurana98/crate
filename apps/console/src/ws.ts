// Live job-log client for /api/ws/jobs (ADR-0020).
//
// Connects over ws(s) on the same origin (the session cookie authenticates it),
// hands every events frame to the caller, remembers the cursor, and on a dropped
// connection reconnects with exponential backoff, resuming from that cursor so no
// line is lost or repeated. Auth/origin rejections (4401/4403) don't retry.
import type { JobEvent, JobSummary } from "./api";

export interface JobFrame {
  type: "events";
  events: JobEvent[];
  cursor: number;
  active: JobSummary[];
}

const NO_RETRY = new Set([4401, 4403]);

export function connectJobLog(
  onFrame: (frame: JobFrame) => void,
  opts: { since?: number | null; onStatus?: (live: boolean) => void } = {},
): () => void {
  let cursor: number | null = opts.since ?? null;
  let socket: WebSocket | null = null;
  let stopped = false;
  let delay = 1000;
  let timer: number | undefined;

  const open = (): void => {
    if (stopped) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const query = cursor != null ? `?since=${cursor}` : "";
    socket = new WebSocket(`${proto}//${location.host}/api/ws/jobs${query}`);
    socket.onopen = () => opts.onStatus?.(true);
    socket.onmessage = (ev: MessageEvent<string>) => {
      try {
        const msg = JSON.parse(ev.data) as { type: string; cursor?: number };
        if (typeof msg.cursor === "number") cursor = msg.cursor;
        if (msg.type === "events") onFrame(msg as JobFrame);
        delay = 1000; // healthy again -> reset backoff
      } catch {
        /* ignore malformed frames */
      }
    };
    socket.onclose = (ev: CloseEvent) => {
      socket = null;
      opts.onStatus?.(false);
      if (stopped || NO_RETRY.has(ev.code)) return;
      timer = window.setTimeout(open, delay);
      delay = Math.min(delay * 2, 30_000);
    };
  };

  open();
  return () => {
    stopped = true;
    window.clearTimeout(timer);
    socket?.close();
  };
}
