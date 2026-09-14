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

// A shared-feed row (Phase C): who requested a track and how fast it was served.
// ttf_playable_s = request -> first playable file (usually the lossy rip);
// ttf_final_s = request -> final FLAC. Either is null until its mark lands.
export interface FeedRow {
  id: string;
  query: string;
  status: string;
  artist: string | null;
  title: string | null;
  created_at: string;
  finished_at: string | null;
  first_playable_at: string | null;
  final_flac_at: string | null;
  tier_won: number | null;
  source_won: string | null;
  error: string | null;
  user_id: string | null;
  user_name: string | null;
  user_avatar: string | null;
  quality_label: string | null;
  lossless: number | null;
  ttf_playable_s: number | null;
  ttf_final_s: number | null;
}

export interface LiveEvent {
  stats: Stats;
  in_flight: RequestRow[];
  feed: FeedRow[];
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

// --- review tab (user-assisted retry / re-download) ---
export interface ReviewItem {
  id: string;
  query: string;
  requested_via: string;
  status: string; // failed | done | needs_confirm
  kind: "failed" | "lossy" | "needs_confirm";
  reason: string | null; // failure reason (failed items)
  source_won: string | null;
  title: string | null; // wanted title (for comparing against the fingerprint hint)
  duration_s: number | null; // wanted duration
  quality_label: string | null; // current file (lossy items)
  size_bytes: number | null;
  file_path: string | null;
  file_duration_s: number | null;
  lossless: number | null;
  has_audio: boolean; // a current file exists -> the /audio player is available
  // advisory fingerprint hint (ADR-0019); null until identified / when unavailable
  acoustid_artist: string | null;
  acoustid_title: string | null;
  acoustid_mbid: string | null;
  match_similarity: number | null; // 0..1 fingerprint match of the last re-download
}

export interface ReviewCandidate {
  id: string;
  source: string;
  tier: number;
  ref: string;
  title: string;
  artist: string;
  album: string | null;
  quality_label: string;
  lossless: boolean;
  duration_s: number | null;
  size_bytes: number | null;
  preview_url: string | null; // ~30s source clip for the A/B ear test (null = none)
  art: string | null; // album cover art (null = none)
  streamable: boolean; // playable via preview_url or the server stream proxy
  match: number;
}

// The subset of a candidate the download endpoint needs (what the server's
// _candidate_from_body reads). A full ReviewCandidate satisfies it structurally.
export type DownloadableCandidate = Pick<
  ReviewCandidate,
  | "source"
  | "tier"
  | "ref"
  | "title"
  | "artist"
  | "album"
  | "quality_label"
  | "lossless"
  | "duration_s"
  | "size_bytes"
>;

// One file from the Soulseek browser (the Nicotine+ columns) plus the candidate
// the download button echoes back to /review/{id}/download.
export interface SlskRow {
  username: string;
  filename: string; // remote path (often Windows-style)
  basename: string;
  ext: string;
  size_bytes: number;
  bitrate_kbps: number; // 0 for lossless (slskd reports no bitrate for FLAC)
  bit_depth: number | null;
  sample_rate: number | null;
  length_s: number | null;
  lossless: boolean;
  free_slot: boolean; // peer has a free upload slot right now
  upload_speed: number; // bytes/s as reported by the peer
  quality_label: string;
  candidate: DownloadableCandidate;
}

// The advisory hint POST /review/{id}/identify returns ({} -> all null here).
export interface ReviewHint {
  canonical_artist?: string | null;
  canonical_title?: string | null;
  mbid?: string | null;
}

export interface ReviewStats {
  needs_review: number;
  failed: number;
  lossy: number;
  needs_confirm: number;
  resolved: number;
}

export interface ReviewResponse {
  items: ReviewItem[];
  stats: ReviewStats;
}

// A mid-session 401 (expired/revoked cookie) reboots into the login gate. /api/me
// is exempt: the gate itself probes it and handles null without a redirect loop.
function on401(path: string): void {
  if (!path.startsWith("/api/me")) window.location.href = "/";
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { headers: { Accept: "application/json" }, signal });
  if (res.status === 401) on401(path);
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
  // No query -> the known-song credential test. A query forces that specific
  // track through this source, which is how you find the broken one.
  validateSource: (name: string, query?: string) =>
    send<{ ok: boolean; detail: string; quality_label: string | null; elapsed_s: number | null }>(
      "POST",
      `/api/sources/${encodeURIComponent(name)}/validate`,
      query ? { query } : {},
    ),
  search: (q: string, signal?: AbortSignal) =>
    get<SpotifyTrack[]>(`/api/search?q=${encodeURIComponent(q)}&limit=20`, signal),
  // Direct cross-source search (the "not on Spotify?" fallback) — fans the query
  // out across the linked download sources and returns scored candidates.
  searchSources: (q: string) => send<ReviewCandidate[]>("POST", "/api/search/sources", { q }),
  // Fire-and-forget: warm the backend cross-source cache while the user is still
  // typing, so opening the dialog returns instantly. Never throws.
  prewarmSources: (q: string): void => {
    void fetch("/api/search/sources/prewarm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ q }),
    }).catch(() => {});
  },
  downloadSourceCandidate: (query: string, candidate: ReviewCandidate) =>
    send<{ id: string; status: string; source: string }>(
      "POST",
      "/api/search/sources/download",
      { query, candidate },
    ),
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
  review: () => get<ReviewResponse>("/api/review"),
  reviewSearch: (id: string, q: string) =>
    send<ReviewCandidate[]>("POST", `/api/review/${encodeURIComponent(id)}/search`, { q }),
  reviewDownload: (id: string, candidate: DownloadableCandidate, force = false) =>
    send<{ id: string; status: string; source: string }>(
      "POST",
      `/api/review/${encodeURIComponent(id)}/download`,
      { candidate, force },
    ),
  reviewIdentify: (id: string) =>
    send<ReviewHint>("POST", `/api/review/${encodeURIComponent(id)}/identify`, {}),
  // Nicotine+-style raw Soulseek browse for a review item: custom terms -> every
  // file the network returned (no auto-selection). Slow (P2P search, up to ~30 s).
  reviewSoulseekSearch: (id: string, q: string) =>
    send<SlskRow[]>("POST", `/api/review/${encodeURIComponent(id)}/soulseek/search`, { q }),
  reviewConfirm: (id: string) =>
    send<{ id: string; status: string }>(
      "POST",
      `/api/review/${encodeURIComponent(id)}/confirm`,
      {},
    ),
  reviewAudioUrl: (id: string) => `/api/review/${encodeURIComponent(id)}/audio`,
  reviewCandidateStreamUrl: (source: string, ref: string) =>
    `/api/review/candidate-stream?source=${encodeURIComponent(source)}&ref=${encodeURIComponent(ref)}`,
  reviewDismiss: (id: string) =>
    send<{ id: string; dismissed: boolean }>(
      "POST",
      `/api/review/${encodeURIComponent(id)}/dismiss`,
      {},
    ),
  // Shared activity feed (Phase C) — everyone sees everything.
  feed: (limit = 50) => get<FeedRow[]>(`/api/feed?limit=${limit}`),
  spotifyStatus: () => get<SpotifyStatus>("/api/spotify/status"),
  spotifyPlayer: (signal?: AbortSignal) => get<PlayerState>("/api/spotify/player", signal),
  eventsUrl: "/api/events",
  // Background jobs: history, a job's log, "Run now", cancel.
  jobs: (kind?: string) =>
    get<JobSummary[]>(`/api/jobs${kind ? `?kind=${encodeURIComponent(kind)}` : ""}`),
  jobEvents: (id: string, after = 0) =>
    get<JobEvent[]>(`/api/jobs/${encodeURIComponent(id)}/events?after=${after}`),
  runJob: (kind: string) => send<{ id: string; existing: boolean }>("POST", "/api/jobs", { kind }),
  // Streams a Spotify streaming-history export (zip or json) as the raw request
  // body, queueing a history_import job for the logged-in user.
  uploadHistory: async (file: File): Promise<{ id: string; bytes: number }> => {
    const res = await fetch("/api/history/upload", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: file,
    });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        detail = ((await res.json()) as { detail?: string }).detail ?? detail;
      } catch {
        /* keep status line */
      }
      throw new Error(detail);
    }
    return (await res.json()) as { id: string; bytes: number };
  },
  historyStatus: () => get<HistoryStatus>("/api/history/status"),
  cancelJob: (id: string) =>
    send<{ id: string; status: string }>("POST", `/api/jobs/${encodeURIComponent(id)}/cancel`, {}),
  // Library Sync page: tiers, playlists, schedule; toggles; missing counts.
  syncOverview: () => get<SyncOverview>("/api/sync/overview"),
  // Only the songs not in the library yet (the server filters before paginating).
  syncMembers: (tier: number, limit = 200, offset = 0) =>
    get<TierMember[]>(`/api/sync/tiers/${tier}/members?limit=${limit}&offset=${offset}`),
  syncSetTier: (tier: number, auto_sync: boolean, backfill = false) =>
    send<SyncToggleResult>("PUT", `/api/sync/tiers/${tier}`, { auto_sync, backfill }),
  syncSetPlaylist: (id: string, mode: SyncMode, backfill = false) =>
    send<SyncToggleResult>("PUT", `/api/sync/playlists/${encodeURIComponent(id)}`, {
      mode,
      backfill,
    }),
  // 307 -> a fresh ~30 s Deezer clip for a tier song (usable as an <audio> src).
  syncPreviewUrl: (spotifyId: string) => `/api/sync/preview/${encodeURIComponent(spotifyId)}`,
  syncMissing: (scope: string) =>
    get<{ scope: string; missing: number }>(`/api/sync/missing?scope=${encodeURIComponent(scope)}`),
  // Deduplicate page: overview, ranked groups, ignore, and a file for the A/B test.
  dedupOverview: () => get<DedupOverview>("/api/dedup/overview"),
  dedupGroups: (variants: boolean, offset = 0, limit = 40) =>
    get<DupGroup[]>(`/api/dedup/groups?variants=${variants}&offset=${offset}&limit=${limit}`),
  dedupIgnore: (id: string) =>
    send<{ id: string; ignored_pairs: number }>(
      "POST",
      `/api/dedup/groups/${encodeURIComponent(id)}/ignore`,
      {},
    ),
  dedupAudioUrl: (fileId: string) => `/api/dedup/files/${encodeURIComponent(fileId)}/audio`,
  // Admin only: trash copies, clean up byte-identical copies, restore, empty.
  dedupPurge: (groupId: string, keep: string, trash: string[]) =>
    send<{ job: string; files: number; bytes: number }>(
      "POST",
      `/api/dedup/groups/${encodeURIComponent(groupId)}/purge`,
      { keep, trash },
    ),
  dedupResolveExact: () =>
    send<{ job: string; files: number; bytes: number }>("POST", "/api/dedup/resolve-exact", {}),
  dedupTrash: () => get<DedupTrash>("/api/dedup/trash"),
  dedupAuditTags: () => get<TagAudit>("/api/dedup/audit/tags"),
  dedupAuditOrphans: () => get<OrphanAudit>("/api/dedup/audit/orphans"),
  dedupAuditFolders: () => get<FolderDup[]>("/api/dedup/audit/folders"),
  dedupRestore: (ids: string[]) =>
    send<{ job: string; files: number }>("POST", "/api/dedup/trash/restore", { ids }),
  dedupEmpty: () =>
    send<{ job: string; existing: boolean }>("POST", "/api/dedup/trash/empty", { confirm: "EMPTY" }),
  // Per-user library import (Phase 2).
  libraryPlaylists: () => get<Playlist[]>("/api/library/playlists"),
  importLibrary: (body: { source: "liked" | "playlist"; playlist_id?: string; name?: string }) =>
    send<BulkResult>("POST", "/api/library/import", body),
  // Login with Spotify (Phase 2). me() resolves the current user, or null when
  // logged out (401) so the app can render the login gate.
  me: async (): Promise<Me | null> => {
    const res = await fetch("/api/me", { headers: { Accept: "application/json" } });
    if (res.status === 401) return null;
    if (!res.ok) throw new Error(`${res.status} ${res.statusText} — /api/me`);
    return (await res.json()) as Me;
  },
  loginUrl: "/api/auth/login",
  logout: async (): Promise<void> => {
    await fetch("/api/auth/logout", { method: "POST" });
    window.location.href = "/";
  },
};

// --- background jobs (Library Sync / Dedup engine) ---
export interface JobEvent {
  id: number; // the live-log cursor
  job_id: string;
  ts: string;
  level: "info" | "warn" | "error" | "status" | "progress";
  code: string | null;
  message: string;
  user_id: string | null;
  data: Record<string, unknown> | null;
}

export interface JobSummary {
  id: string;
  kind: string;
  lane: string;
  status: "queued" | "running" | "done" | "failed" | "cancelled";
  triggered_by: string;
  progress_done: number | null;
  progress_total: number | null;
  created_at: string;
  started_at: string | null;
  finished_at?: string | null;
  error?: string | null;
  summary?: Record<string, unknown> | null;
}

// A user's listening ledger (the Sync page's history panel).
export interface HistoryStatus {
  plays: number;
  export_plays: number;
  polled_plays: number;
  first_played_at: string | null;
  last_played_at: string | null;
  last_polled_at: string | null;
  export: { start: string; end: string; imported_at: string } | null;
}

export interface Me {
  id: string;
  spotify_id: string | null;
  display_name: string;
  email: string | null;
  avatar_url: string | null;
  is_admin: boolean;
  missing_scopes?: string[]; // OAuth scopes to re-consent to (drives the re-login banner)
}

export interface Playlist {
  id: string;
  name: string;
  count: number;
  image: string | null;
  snapshot_id?: string | null;
  owner_id?: string | null;
}


// --- Library Sync (the Sync page) ---
export type SyncMode = "inherit" | "on" | "off";

export interface SyncTierCard {
  tier: number; // 1 playlists & liked | 2 long-term constants | 3 short-term flings
  label: string;
  count: number;
  in_library: number;
  missing: number; // count - in_library: what this tier would still download
  approximate: boolean; // from top-tracks lists until the history export is in
  auto_sync: boolean;
  enabled_at: string | null;
}

export interface SyncPlaylist {
  spotify_id: string; // "liked" = Liked Songs
  name: string;
  owner_id: string | null;
  owned: boolean;
  generated: boolean; // Spotify-made (Discover Weekly, mixes)
  image: string | null;
  track_count: number;
  in_library_count: number;
  sync_mode: SyncMode;
  auto_sync: boolean; // effective (inherit resolved)
  last_checked_at: string | null;
  last_changed_at: string | null;
}

export interface SyncJobBrief {
  id: string;
  status: JobSummary["status"];
  triggered_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  summary: Record<string, unknown> | null;
  error: string | null;
  progress_done: number | null;
  progress_total: number | null;
}

export interface SyncOverview {
  tiers: SyncTierCard[];
  playlists: SyncPlaylist[];
  last_sync: SyncJobBrief | null;
  running: SyncJobBrief | null;
  next_run_at: string | null;
  interval_s: number;
}

export interface TierMember {
  merge_key: string;
  spotify_id: string | null;
  artist: string | null;
  title: string | null;
  score: number;
  approximate: boolean;
  first_seen_at: string;
  in_library: boolean;
  stats: {
    plays?: number;
    span?: string;
    burst_plays?: number;
    burst_end?: string;
    rank?: number;
    local?: boolean;
  };
}

export interface SyncToggleResult {
  auto_sync: boolean;
  missing: number;
  baselined: number;
}

// --- Deduplicate (the Dedup page) ---
export interface DedupOverview {
  files: number;
  analysed: number;
  identified: number;
  errors: number;
  bytes: number;
  groups: number;
  variant_groups: number;
  duplicate_files: number; // extra copies beyond each group's keeper (variants excluded)
  reclaimable_bytes: number;
  last_grouped_at: string | null;
  trash_count?: number;
  trash_bytes?: number;
  exact_groups?: number;
  exact_files?: number; // byte-identical extra copies (the one-click clean-up)
  exact_bytes?: number;
  mutating?: boolean; // a purge / restore / empty job is queued or running
  last_scan: {
    id: string;
    status: JobSummary["status"];
    created_at: string;
    finished_at: string | null;
    progress_done: number | null;
    progress_total: number | null;
    summary: Record<string, unknown> | null;
    error: string | null;
  } | null;
}

export interface DupMember {
  id: string;
  path: string; // relative to the library root
  size: number;
  codec: string | null;
  lossless: boolean;
  bitrate: number | null;
  bit_depth: number | null;
  sample_rate: number | null;
  duration_s: number | null;
  artist: string | null;
  album: string | null;
  title: string | null;
  has_cover: boolean;
  acoustid_id: string | null;
  acoustid_score: number | null;
  aid_artist: string | null;
  aid_title: string | null;
  mbid: string | null;
  rank: number; // 0 = keeper
  is_keeper: boolean;
  score_vs_keeper: number | null;
  mtime_ns?: number;
  reasons: string[]; // why the keeper beats this copy
  tag_score?: number;
  canonical?: boolean;
  referenced?: boolean;
}

export interface DupGroup {
  id: string;
  reason: "exact" | "acoustid" | "fp";
  confidence: number;
  variant: boolean;
  keeper_file_id: string;
  reclaimable_bytes: number;
  created_at: string;
  members: DupMember[];
}

export interface TrashItem {
  id: string;
  file_id: string;
  original_path: string;
  trash_path: string;
  size: number;
  trashed_at: string;
  user_id: string | null;
  group_id: string | null;
}

export interface DedupTrash {
  items: TrashItem[];
  count: number;
  bytes: number;
}

// --- Deduplicate audits ---
export interface TagAudit {
  count: number;
  items: {
    id: string;
    path: string;
    artist: string | null;
    title: string | null;
    aid_artist: string | null;
    aid_title: string | null;
    acoustid_score: number;
    disagrees: "title" | "artist" | "both";
  }[];
}

export interface OrphanAudit {
  records_missing: {
    count: number;
    items: {
      request_id: string;
      path: string;
      quality_label: string;
      query: string;
      artist: string | null;
      title: string | null;
      finished_at: string | null;
    }[];
  };
  files_unrecorded: { count: number; items: string[] };
}

export interface FolderDup {
  folder: string;
  other: string;
  shared: number;
  files: number;
  other_files: number;
  ratio: number;
}
