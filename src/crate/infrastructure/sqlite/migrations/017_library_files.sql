-- Deduplicate: an index of every audio file under the library root.
-- The dedup_scan job fills it incrementally:
--   - unchanged files (same size + mtime) are skipped;
--   - a moved file (same inode + size, new path) keeps its id, so later ignores
--     and decisions survive a reorganise;
--   - a file that disappears gets missing_since instead of being deleted.
CREATE TABLE IF NOT EXISTS library_files (
  id             TEXT PRIMARY KEY,
  path           TEXT NOT NULL UNIQUE,     -- relative to the library root (POSIX)
  size           INTEGER NOT NULL,
  mtime_ns       INTEGER NOT NULL,
  inode          INTEGER,
  codec          TEXT,
  container      TEXT,
  lossless       INTEGER,
  bitrate        INTEGER,
  bit_depth      INTEGER,
  sample_rate    INTEGER,
  channels       INTEGER,
  duration_s     REAL,
  artist         TEXT,
  album          TEXT,
  title          TEXT,
  album_artist   TEXT,
  track_no       INTEGER,
  disc_no        INTEGER,
  tag_key        TEXT,                     -- merge key of the tagged artist + title
  has_cover      INTEGER,                  -- embedded artwork present
  fp_duration    INTEGER,
  fp_compressed  TEXT,                     -- what the AcoustID lookup takes
  fp_raw         BLOB,                     -- packed little-endian uint32 sub-fingerprints
  acoustid_id    TEXT,                     -- AcoustID track id (results[0].id)
  acoustid_score REAL,
  mbid           TEXT,
  aid_artist     TEXT,
  aid_title      TEXT,
  content_hash   TEXT,                     -- sha256; computed only for size collisions
  error          TEXT,                     -- the last analysis failure, if any
  first_seen_at  TEXT NOT NULL,
  scanned_at     TEXT NOT NULL,            -- last time the walker saw it
  analyzed_at    TEXT,                     -- NULL -> needs probe / tags / fingerprint
  looked_up_at   TEXT,                     -- NULL -> needs an AcoustID lookup
  missing_since  TEXT
);
CREATE INDEX IF NOT EXISTS idx_library_files_size ON library_files(size);
CREATE INDEX IF NOT EXISTS idx_library_files_acoustid
  ON library_files(acoustid_id) WHERE acoustid_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_library_files_tag_key ON library_files(tag_key);
CREATE INDEX IF NOT EXISTS idx_library_files_inode ON library_files(inode);
-- purge (later) repoints downloads rows by path
CREATE INDEX IF NOT EXISTS idx_downloads_path ON downloads(path);
