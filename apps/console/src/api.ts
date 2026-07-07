// Typed client for the crate ops API. Same-origin in prod (Caddy injects the
// API key so the browser never holds it); dev proxies /api to :8788.

export interface Stats {
  total: number;
  done: number;
  failed: number;
  in_flight: number;
  queued: number;
  success_rate: number;
  lossless: number;
  lossless_rate: number;
  total_bytes: number;
  avg_elapsed_s: number;
  wins_by_source: Record<string, number>;
  wins_by_tier: Record<string, number>;
  attempts_by_source: Record<string, { ok: number; total: number }>;
}

export interface RequestRow {
  id: string;
  query: string;
  requested_via: string;
  status: string;
  tier_won: number | null;
  source_won: string | null;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  quality_label: string | null;
  size_bytes: number | null;
  lossless: number | null;
  elapsed_s: number | null;
  batch_id?: string | null;
  // structured track identity (Phase 1); null on legacy rows
  artist?: string | null;
  title?: string | null;
  isrc?: string | null;
  duration_s?: number | null;
}

export interface Attempt {
  id: number;
  source: string;
  tier: number;
  ok: number;
  detail: string;
  created_at: string;
}

export interface Download {
  path: string;
  size_bytes: number;
  codec: string;
  bitrate: number | null;
  bit_depth: number | null;
  sample_rate: number | null;
  duration_s: number | null;
  elapsed_s: number | null;
  lossless: number;
  quality_label: string;
}

export interface RequestDetail extends RequestRow {
  attempts: Attempt[];
  download: Download | null;
}

export interface Source {
  name: string;
  tier: number;
  enabled: boolean;
  lossless: boolean;
  credentials: string;
  missing_env: string[];
  required_env: string[];
}

export interface LiveEvent {
  stats: Stats;
  in_flight: RequestRow[];
}

export interface TierCfg {
  tier: number;
  strategy: "all" | "uniform";
  sources: string[];
}

export interface Layout {
  tiers: TierCfg[];
  disabled: string[];
  version: number;
}

export interface SourceUpdate {
  enabled?: boolean;
  settings?: Record<string, unknown>;
  credentials?: Record<string, string>;
}

export interface SpotifyTrack {
  id: string;
  title: string;
  artist: string;
  album: string;
  art: string | null;
  duration_ms: number;
  duration_s: number | null; // rounded seconds for the cascade's duration match
  isrc: string | null;
  year: string;
  url: string | null;
  query: string; // ready-to-cascade free-text query
}

export interface Enqueued {
  id: string;
  query: string;
  status: string;
}

// enqueue payload: a free-text query plus optional structured identity fields the
// cascade matches on (ISRC short-circuit, duration prefilter).
export interface EnqueuePayload {
  query: string;
  artist?: string | null;
  title?: string | null;
  isrc?: string | null;
  duration_s?: number | null;
}

export interface SpotifyStatus {
  configured: boolean;
  connected: boolean;
}

// now-playing / recently-played track (superset of SpotifyTrack)
export interface PlayerTrack extends SpotifyTrack {
  progress_ms?: number;
  is_playing?: boolean;
  played_at?: string | null;
  in_library?: boolean;
}

export interface PlayerState {
  now_playing: PlayerTrack | null;
  recent: PlayerTrack[];
}

export interface BatchCounts {
  done: number;
  failed: number;
  downloading: number;
  queued: number;
}

export interface Batch {
  id: string;
  name: string;
  origin: string; // spotify | youtube | paste
  owner: string;
  total: number;
  paused: boolean;
  cancelled_flag: boolean;
  counts: BatchCounts;
  created_at: string;
}

export interface BatchTrack {
  id: string;
  query: string;
  status: string;
  source_won: string | null;
  tier_won: number | null;
  error: string | null;
}

export interface BatchDetail extends Batch {
  tracks: BatchTrack[];
}

export interface BulkResult {
  id: string;
  name: string;
  origin: string;
  total: number;
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" }, signal });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
  return (await res.json()) as T;
}

async function send<T>(method: string, path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = (await res.json()) as { detail?: string };
      if (j.detail) detail = j.detail;
    } catch {
      /* keep status line */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const api = {
  stats: () => get<Stats>("/api/stats"),
  requests: (params: URLSearchParams) => get<RequestRow[]>(`/api/requests?${params}`),
  request: (id: string) => get<RequestDetail>(`/api/requests/${encodeURIComponent(id)}`),
  sources: () => get<Source[]>("/api/sources"),
  config: () => get<Layout>("/api/config"),
  putConfig: (layout: { tiers: TierCfg[]; disabled: string[] }) =>
    send<Layout>("PUT", "/api/config", layout),
  putSource: (name: string, body: SourceUpdate) =>
    send<Source>("PUT", `/api/sources/${encodeURIComponent(name)}`, body),
  search: (q: string, signal?: AbortSignal) =>
    get<SpotifyTrack[]>(`/api/search?q=${encodeURIComponent(q)}&limit=20`, signal),
  enqueue: (payload: EnqueuePayload) => send<Enqueued>("POST", "/api/download", payload),
  bulk: (text: string) => send<BulkResult>("POST", "/api/bulk", { text }),
  batches: () => get<Batch[]>("/api/batches"),
  batch: (id: string) => get<BatchDetail>(`/api/batches/${encodeURIComponent(id)}`),
  pauseBatch: (id: string, paused: boolean) =>
    send<{ id: string; paused: boolean }>("POST", `/api/batches/${encodeURIComponent(id)}/pause`, {
      paused,
    }),
  cancelBatch: (id: string) =>
    send<{ id: string }>("POST", `/api/batches/${encodeURIComponent(id)}/cancel`, {}),
  retry: (id: string) =>
    send<{ id: string }>("POST", `/api/requests/${encodeURIComponent(id)}/retry`, {}),
  spotifyStatus: () => get<SpotifyStatus>("/api/spotify/status"),
  spotifyPlayer: (signal?: AbortSignal) => get<PlayerState>("/api/spotify/player", signal),
  spotifyLoginUrl: "/api/spotify/login",
  eventsUrl: "/api/events",
};
