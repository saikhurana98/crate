-- 002_config: runtime cascade configuration for the console (Phase 2).
-- Layout (tier membership/order/strategy + disabled) is a single JSON row with a
-- version counter the serve process polls for hot-reload. Per-source settings and
-- encrypted credentials live in their own tables.

CREATE TABLE IF NOT EXISTS app_config (
  id         INTEGER PRIMARY KEY CHECK (id = 1),
  layout     TEXT NOT NULL,              -- JSON {tiers:[{tier,strategy,sources[]}], disabled:[]}
  version    INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_config (
  name       TEXT PRIMARY KEY,
  settings   TEXT NOT NULL DEFAULT '{}',  -- JSON of non-secret settings
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_secret (
  name       TEXT NOT NULL,
  env_key    TEXT NOT NULL,
  ciphertext BLOB NOT NULL,               -- AES-GCM nonce|tag|ct
  updated_at TEXT NOT NULL,
  PRIMARY KEY (name, env_key)
);
