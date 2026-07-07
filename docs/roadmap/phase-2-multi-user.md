# Phase 2 — Multi-user foundation

Goal: N users cost ~O(1) DB pressure; identity, auth, and fairness exist.
SQLite stays (single node appliance).

## 1. DB layer: readers/writer split

- `infrastructure/sqlite/db.py`: `ReaderPool` — thread-local read-only
  connections (`PRAGMA query_only=ON`), WAL gives free concurrent readers.
- `RequestStore` splits internally: all read methods (`list_requests`,
  `get_request`, `stats`, `list_batches`, `get_batch`, `find_completed`) run on
  the calling thread's reader; writes keep the single locked writer connection.
  The lock now guards writes only.
- Console process (`ops_api`) uses readers exclusively except enqueue/requeue/
  batch mutations.

## 2. Users + sessions

- Migration `006_users.sql`: `users(id, name, pass_hash, is_admin, created_at)`
  + `sessions(token_hash, user_id, expires_at)`; `requests.owner` and
  `batches.owner` become FKs to `users.id` (nullable for legacy rows; the shim
  writes a fixed `explo` service user).
- Auth: stdlib only — `hashlib.scrypt` password hashes, 32-byte session tokens
  (stored hashed), HttpOnly cookie, 30-day expiry. No new dependency.
- `ops_api`: `/api/login`, `/api/logout`, `/api/me`; every mutating endpoint
  requires a session; reads require session unless `CRATE_OPS_API_KEY` matches
  (automation path). First-run bootstrap: `crate user add <name>` CLI command.
- Console UI: minimal login screen (same visual language), session persisted by
  cookie; header shows the user; per-user "my downloads" filter in History.

## 3. Fair scheduling

- `claim_next()` becomes owner-round-robin: pick the owner whose *least
  recently claimed* queued request is oldest — one query:
  order queued rows by `(owner_last_claim_at NULLS FIRST, created_at)` using a
  small `owner_claims(owner, last_claim_at)` upsert table maintained inside the
  claim transaction. Within an owner: FIFO. Paused/cancelled batch exclusion
  unchanged.
- Property test: two owners, one with 500 queued + one with 1 → the single
  request is claimed within the first 2 claims.

## 4. Per-user Spotify

- Config-store secrets namespaced `spotify:user:<id>` for refresh tokens;
  `SpotifyOAuthProvider` becomes per-user (constructed with user id); Listening
  panel + search quotas per user. Global client id/secret stay app-level.

## 5. SSE hub

- One background sampler thread in the console process builds the live frame on
  a 2 s cadence (reusing the Phase-0 stats cache); every `/events` client
  stream reads the latest frame from the hub (condition-variable broadcast) —
  no per-client DB access at all.

**Acceptance:** 2 concurrent SSE viewers + 4 workers show zero reader/writer
lock contention (writes only); unauthenticated mutation = 401; fairness
property test green; per-user Spotify tokens isolated.
