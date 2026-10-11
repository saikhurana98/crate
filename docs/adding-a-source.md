# Adding a music source

This is the contract for adding a new download source to crate. Follow it and a
new source drops into the tier cascade, the console, and the per-track library
finalizer with no changes to the core. Qobuz ([`src/crate/adapters/sources/qobuz.py`](../src/crate/adapters/sources/qobuz.py))
is the canonical reference — read it alongside this doc.

## Mental model

A **source** is one place crate can fetch a track from (Qobuz, Deezer, Soulseek,
yt-dlp, …). Sources are pooled into **tiers**; the cascade tries Tier 1, then 2,
then 3, and keeps the first file that verifies. Within a tier, a `MuxStrategy`
(`all` = try every source, `uniform` = round-robin one per request) decides
fan-out. See [`application/multiplex.py`](../src/crate/application/multiplex.py)
and [`application/cascade.py`](../src/crate/application/cascade.py).

Tiers (by convention — see [`domain/tiers.py`](../src/crate/domain/tiers.py)):

| Tier | Meaning | Examples |
|---|---|---|
| 1 | lossless FLAC, direct or resolved | qobuz, monochrome, antra |
| 2 | lossless (16-bit FLAC) or good P2P | deezer, soulseek, telegram |
| 3 | lossy fallback, **always on** | ytdlp |

## The contract

Every source is a callable with this exact shape (the "downloader"):

```python
download(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome
```

- **`query`** — free text ("Artist Title") or a URL. You search for it.
- **`out_dir`** — where to write the file. Write the final audio file here.
- **`want`** — optional structured artist, title, ISRC and duration. Use it to
  reject wrong recordings when present.
- **Returns** a [`DownloadOutcome`](../src/crate/domain/download.py). On **any**
  failure (not found, auth, transport, wrong quality) **raise** — the cascade
  catches it, records a failed `CascadeAttempt`, and advances. Never return a
  partial/blank outcome. Prefer `LookupError` (no match) / `RuntimeError` (fetch
  failed).

`DownloadOutcome` fields you must set:

```python
DownloadOutcome(
    path=dest,            # the file you just wrote, under out_dir
    tier=1,               # your tier number
    source="qobuz",       # your source name (matches the catalog)
    elapsed_s=clock()-start,
    query=query,
    # --- metadata: fill these so the librarian files it correctly ---
    title=track.title,
    artist=track.artist,
    album=track.album or None,
    track_no=track.track_no,   # optional
    disc_no=track.disc_no,     # optional
)
```

### Metadata is not optional in practice

The per-track finalizer ([`application/librarian.py`](../src/crate/application/librarian.py))
files each track as `<Artist>/<Album>/<NN Title>.<ext>` and writes those tags
onto the file for Navidrome. It builds the tags by **merging source metadata
over the file's own tags** (source wins, file fills gaps, `query` is the
last-resort title). So:

- If your source hands back **untagged** audio (Qobuz/Deezer do),
  you **must** populate `title/artist/album` on the `DownloadOutcome`, or every
  track lands in `Unknown Artist/Unknown Album/`.
- If your source's files are **already tagged** (Soulseek/Telegram/yt-dlp
  usually are), you may leave the metadata `None` — the librarian reads the
  file's own tags. Setting them anyway (when you have them) is better.

## Layering (enforced by lint — don't fight it)

Hexagonal boundaries are enforced by ruff `flake8-tidy-imports`
([`pyproject.toml`](../pyproject.toml)):

- **`adapters/sources/<name>.py`** — the source logic. **Pure and testable**:
  parsing, selection, orchestration. Take every I/O collaborator (search, HTTP
  `get_bytes`, decrypt, clock) as an **injected keyword callable** — do not
  import concrete HTTP/DB clients here. This is why adapters have wide
  keyword-only signatures (`max-args = 9`).
- **`infrastructure/<name>/client.py`** — the concrete I/O: credentials, HTTP,
  protocol, crypto, session. Holds secrets, talks to the network.
- **`composition/sources.py`** — the only place that wires a concrete client
  into an adapter and reads env. Everything else stays decoupled.

Optional heavy deps (such as telethon) are **lazy-imported inside the method
that needs them**, never at module top.

## Step-by-step: add a source named `foo`

### 1. Infrastructure client — `src/crate/infrastructure/foo/client.py`
Holds creds + does I/O. Expose small methods the adapter will inject, e.g.
`search(query) -> json`, `get_file_url(id) -> url`, `get_bytes(url) -> bytes`.
Constructor reads what it needs and stores it; **raise if a required cred is
missing** (a raising source is skipped, not fatal).

### 2. Adapter — `src/crate/adapters/sources/foo.py`
Pattern (mirror qobuz):
- A frozen `FooTrack` dataclass with `title/artist/album` (+ id, isrc, …).
- `parse_search(json) -> list[FooTrack]` — pure.
- `select_track(tracks, query, want=None) -> FooTrack` — pick the best; use
  [`domain.matching.title_matches`](../src/crate/domain/matching.py) and
  `artist_matches` so you don't accept a wrong song. Prefer an exact ISRC
  when available. Raise `LookupError` if nothing matches.
- `foo_download(query, out_dir, want=None, *, search, fetch, ..., clock=time.monotonic)
  -> DownloadOutcome` — orchestrate: search → select → fetch → write file →
  return `DownloadOutcome` **with metadata** (see above).
- Filename convention: `"{artist} - {title} [{id}].{ext}"` run through the
  `_UNSAFE` sanitiser.

### 3. Composition factory — `src/crate/composition/sources.py`
Add a `_foo_source()` that builds the client from env and returns a
`SourceEntry`:

```python
def _foo_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.foo import foo_download
    from crate.infrastructure.foo.client import FooClient

    client = FooClient(os.environ["CRATE_FOO_TOKEN"])  # raises if unset -> skipped

    def foo(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return foo_download(query, out_dir, want, search=client.search, fetch=client.fetch)

    return SourceEntry(SourceRef("foo", Tier.TWO), foo, expected_lossless=True)
```

Then register it:
- Add `"foo": _foo_source` to the `_FACTORIES` dict.
- Add an enable block to `_tiers_from_env()` (the env-only path):
  `if _enabled("CRATE_FOO"): tierN.append(_foo_source())`.

`expected_lossless=True` makes the mux **reject** a non-lossless result from
this source and advance — set it only if the source truly delivers lossless.

### 4. Catalog entry — `src/crate/composition/catalog.py`
Add a `SourceSpec` to `SOURCES` (drives console display + credential status +
the drag-drop layout):

```python
SourceSpec("foo", 2, "CRATE_FOO", ("CRATE_FOO_TOKEN",), lossless=True),
```
`enable_flag=None` means always-on (only ytdlp). Never invent a new tier number
— tiers are fixed 1/2/3.

Registering in `_FACTORIES` also makes the source **testable in isolation** for
free — `crate download --source foo` and the console's Sources → Test button
both force a download through just this source, falling back to your `download`
callable when the source has no candidate/search-by-ref path. Nothing extra to
implement.

For Review search and re-download, also implement `search_candidates` and
`download_candidate` in the adapter and register a factory in
`_CANDIDATE_FACTORIES`. The latter must fetch the selected ref without
re-running search. A direct Range-able stream can also be registered in
`build_candidate_streamers()` for the Review A/B player.

### 5. Env template — `.crate.env.example`
Document the enable flag + every `required_env` var with a one-line "how to get
it". Prefix everything `CRATE_`.

### 6. Tests — `tests/adapters/test_foo.py`
Unit-test the adapter with **fakes** for the injected callables (no network):
- `parse_search` on a captured JSON blob,
- `select_track` picks the right track / raises on no match,
- `foo_download` writes the file and returns a `DownloadOutcome` **with the
  metadata populated** (assert `outcome.artist/album/title`).
Live integration tests go behind `CRATE_TEST_LIVE_SOURCES` and are skipped in
CI. The gate is `uv run pytest` (coverage ≥ 90%), `ruff check`, `ruff format`,
`mypy --strict` — all must pass.

## Files to touch (summary)

| File | What |
|---|---|
| `infrastructure/foo/client.py` | creds + I/O client (new) |
| `adapters/sources/foo.py` | pure adapter + `foo_download` (new) |
| `composition/sources.py` | `_foo_source()` factory, `_FACTORIES`, `_tiers_from_env()` |
| `composition/catalog.py` | `SourceSpec` in `SOURCES` |
| `.crate.env.example` | enable flag + creds, documented |
| `tests/adapters/test_foo.py` | fake-backed unit tests (new) |

## Gotchas

- **Raise, don't return-empty.** A failing source must raise so the cascade
  moves on; returning a bad outcome corrupts the pipeline.
- **Untagged audio → set metadata.** Otherwise the librarian files it under
  `Unknown Artist/Unknown Album`.
- **Don't import infrastructure/adapters from domain or ports** — lint fails.
- **Missing creds should raise in the factory**, not crash the process — the
  source is simply left out of the cascade.
- **Pin nothing but ytdlp to a tier.** Users re-arrange tiers at runtime via the
  console layout ([`catalog.validate_layout`](../src/crate/composition/catalog.py));
  only `ytdlp` is `PINNED` to Tier 3.
- **Same-filesystem writes.** Files download into the staging dir
  (`CRATE_SERVE_DOWNLOADS`) and are atomically renamed into `CRATE_LIBRARY_ROOT`
  — keep both on one mount (ADR-0008).
