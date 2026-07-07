# Phase 4 — Deep verification & art (optional; revives ADR-0005)

Goal: verify *identity* and *authenticity* of files, and make the librarian
fetch cover art natively (retiring the external beets pass for new downloads).

## 1. Chromaprint identity

- `infrastructure/audio/fingerprint.py`: `fpcalc` subprocess wrapper (30 s
  timeout) → `(duration, fingerprint)`; binary presence detected at startup,
  feature silently off when absent (core image may exclude it; full image adds
  `libchromaprint-tools`).
- Used on the Phase-1 **upgrade path**: before replacing a lossy file with a
  lossless candidate, compare fingerprints (similarity ≥ 0.95 via offset-tolerant
  bit-error rate) — mismatch = failed attempt, keep the old file.
- Optional AcoustID lookup (`CRATE_ACOUSTID_KEY`) to attach MBIDs to library
  rows for future features.

## 2. Fake-FLAC detection

- `infrastructure/audio/spectrum.py`: ffmpeg-based rolloff probe
  (`astats`/`aspectralstats` over a 30 s middle window). A "24-bit FLAC" whose
  spectral rolloff sits at ~16 kHz is a transcoded MP3 → verdict `fake_flac`.
- Applied only to untrusted sources (soulseek, telegram): fake = failed
  attempt, cascade advances. Qobuz/Deezer/Monochrome skip (paid mirrors don't
  fake; saves 1 ffmpeg pass per track).

## 3. Cover art in the librarian

- `DownloadOutcome` gains `art_url: str | None`; qobuz (`album.image.large`),
  deezer (`album.cover_xl`), monochrome (TIDAL `album.cover` UUID → URL) fill it
  from data already in their search responses.
- Librarian `finalize`: stream art (≤ 2 MB cap) → embed via mutagen
  (front cover) + write `cover.jpg` alongside, matching the beets layout.
  Failures are logged, never fatal.
- Beets stays for back-catalog only; new downloads arrive art-complete.

**Acceptance:** upgrade replacing a file requires fingerprint match (test with
synthetic FLACs); soulseek MP3-in-FLAC fixture flagged fake; a qobuz download
lands with embedded art + cover.jpg with zero beets involvement.
