// Typed client for the lbmd ops API. Same-origin in prod (Caddy injects the
// API key so the browser never holds it); dev proxies /api to :8788.

export interface Stats {
  total: number;
  done: number;
  failed: number;
  in_flight: number;
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

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`);
  return (await res.json()) as T;
}

export const api = {
  stats: () => get<Stats>("/api/stats"),
  requests: (params: URLSearchParams) => get<RequestRow[]>(`/api/requests?${params}`),
  request: (id: string) => get<RequestDetail>(`/api/requests/${encodeURIComponent(id)}`),
  sources: () => get<Source[]>("/api/sources"),
  eventsUrl: "/api/events",
};
