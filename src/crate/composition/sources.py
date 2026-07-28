"""Composition root — wires swappable source modules into per-tier pools.

Each tier is a :class:`TierMux` (a pool of sources + a fan-out strategy), so a
tier can load-balance across several sources. Strategy is configurable per tier
via ``CRATE_MUX_TIER{1,2,3}`` = ``all`` (default, try every source) or
``uniform``/``distribute`` (round-robin one source per request).

  Tier 1  Qobuz (hi-res FLAC, direct)        opt-in CRATE_QOBUZ=1
          Monochrome (Amazon 24-bit FLAC)   opt-in CRATE_MONOCHROME=1
  Tier 2  Deezer (16-bit FLAC via ARL)       opt-in CRATE_DEEZER=1
          arcod (Qobuz proxy, no auth)       opt-in CRATE_ARCOD=1
          Soulseek (via slskd)               opt-in CRATE_SOULSEEK=1
          Telegram music bot                 opt-in CRATE_TELEGRAM=1
  Tier 3  yt-dlp (lossy fallback)            always on

Only this layer binds concrete adapters + infrastructure.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from crate.adapters.sources import ytdlp
from crate.adapters.sources.monochrome import DEFAULT_TIDAL_BASE, monochrome_download
from crate.application.cascade import MuxStrategy, SourceEntry, TierMux
from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.tiers import SourceRef, Tier
from crate.infrastructure import http
from crate.infrastructure.audio.decrypt import mp4decrypt_cenc

# Review-tab candidate source: (search-without-downloading, download-one-candidate).
SearchCandidatesFn = Callable[[str, "TrackIdentity | None"], list[Candidate]]
DownloadCandidateFn = Callable[[Candidate, Path, "TrackIdentity | None"], DownloadOutcome]
CandidateSource = tuple[SearchCandidatesFn, DownloadCandidateFn]


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _strategy(tier_num: int) -> MuxStrategy:
    value = os.environ.get(f"CRATE_MUX_TIER{tier_num}", "all").lower()
    return MuxStrategy.UNIFORM if value in ("uniform", "distribute") else MuxStrategy.ALL


_amz_lock = threading.Lock()
_amz_holder: dict[str, Any] = {}  # holder (not a global rebind) for the singleton


def _shared_amz() -> Any:  # pragma: no cover - IO wiring
    """Process-wide singleton :class:`AmzSession` — one headed browser shared by
    every monochrome wiring (each of the N queue workers, the review-candidate
    path, and every config hot-reload rebuild). Constructing one per build leaked
    a ~2 GB Chromium each time, blowing the serve container past its memory limit;
    the session marshals all Playwright work to its own thread so sharing is safe."""
    with _amz_lock:
        session = _amz_holder.get("session")
        if session is None:
            from crate.infrastructure.browser.amz_session import AmzSession

            session = AmzSession()
            _amz_holder["session"] = session
        return session


def _qobuz_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.qobuz import qobuz_download
    from crate.infrastructure.qobuz.client import QobuzClient

    client = QobuzClient(
        os.environ["CRATE_QOBUZ_APP_ID"],
        os.environ["CRATE_QOBUZ_APP_SECRET"],
        os.environ["CRATE_QOBUZ_TOKEN"],
    )

    def qobuz(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return qobuz_download(
            query,
            out_dir,
            want,
            search=client.search,
            get_file_url=client.get_file_url,
            fetch=client.fetch,
        )

    return SourceEntry(SourceRef("qobuz", Tier.ONE), qobuz, expected_lossless=True)


def _monochrome_source() -> SourceEntry:  # pragma: no cover - IO wiring
    session = _shared_amz()

    def search_json(query: str) -> Any:
        return http.get_json(f"{DEFAULT_TIDAL_BASE}/search/", {"s": query})

    def fetch(url: str, dest: Path) -> None:
        http.stream_to(url, dest)  # discard the bytes-written count

    def monochrome(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return monochrome_download(
            query,
            out_dir,
            want,
            search_json=search_json,
            amz_fetch=session.fetch_amz,
            fetch=fetch,
            decrypt=mp4decrypt_cenc,
        )

    return SourceEntry(SourceRef("monochrome", Tier.ONE), monochrome, expected_lossless=True)


def _telegram_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.telegram_bot import telegram_download
    from crate.infrastructure.telegram.telethon_client import TelegramBotClient

    client = TelegramBotClient(
        api_id=int(os.environ["CRATE_TG_API_ID"]),
        api_hash=os.environ["CRATE_TG_API_HASH"],
        session=os.environ["CRATE_TG_SESSION"],
        bot=os.environ["CRATE_TG_BOT"],
    )

    def telegram(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return telegram_download(
            query, out_dir, want, request=client.request, download=client.download
        )

    # accepts whatever the bot serves (prefers lossless in selection)
    return SourceEntry(SourceRef("telegram", Tier.TWO), telegram, expected_lossless=False)


def _deezer_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.deezer import deezer_download
    from crate.infrastructure.deezer.client import DeezerClient

    client = DeezerClient(os.environ["CRATE_DEEZER_ARL"])

    def deezer(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return deezer_download(
            query,
            out_dir,
            want,
            search=client.search,
            resolve_url=client.resolve_url,
            fetch=client.fetch,
        )

    # Deezer always serves FLAC when available; if the account tier lacks it the
    # client raises and the cascade advances.
    return SourceEntry(SourceRef("deezer", Tier.TWO), deezer, expected_lossless=True)


def _arcod_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.arcod import arcod_download
    from crate.infrastructure.arcod.client import ArcodClient

    client = ArcodClient(os.environ.get("CRATE_ARCOD_BASE", "https://arcod.xyz"))

    def arcod(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return arcod_download(
            query,
            out_dir,
            want,
            search=client.search,
            create_download=client.create_download,
            poll=client.poll,
            fetch=client.fetch,
        )

    # arcod may deliver MP3 despite requesting FLAC (Qobuz proxy, quality not
    # guaranteed) -> NOT lossless-gated; the prober keeps whatever it serves.
    return SourceEntry(SourceRef("arcod", Tier.TWO), arcod, expected_lossless=False)


def _soulseek_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.soulseek import soulseek_download
    from crate.infrastructure.soulseek.slskd_client import SlskdClient

    client = SlskdClient(
        os.environ["CRATE_SLSKD_URL"],
        os.environ["CRATE_SLSKD_API_KEY"],
        os.environ["CRATE_SLSKD_DOWNLOADS"],
    )
    min_kbps = int(os.environ.get("CRATE_SLSKD_MIN_BITRATE", "0"))

    def soulseek(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return soulseek_download(
            query,
            out_dir,
            want,
            search=client.search,
            download=client.download,
            min_bitrate_kbps=min_kbps,
        )

    # prefers lossless in selection; a good Soulseek MP3 still beats Tier-3 YouTube
    return SourceEntry(SourceRef("soulseek", Tier.TWO), soulseek, expected_lossless=False)


def _ytdlp_source() -> SourceEntry:  # pragma: no cover - IO wiring
    return SourceEntry(SourceRef("ytdlp", Tier.THREE), ytdlp.download, expected_lossless=False)


# name -> factory. Each factory reads its creds from the environment (which the
# config store may have populated via _apply_stored_secrets) and raises if they
# are absent — a raising source is simply skipped from the cascade.
_FACTORIES: dict[str, Callable[[], SourceEntry]] = {
    "qobuz": _qobuz_source,
    "monochrome": _monochrome_source,
    "deezer": _deezer_source,
    "arcod": _arcod_source,
    "soulseek": _soulseek_source,
    "telegram": _telegram_source,
    "ytdlp": _ytdlp_source,
}


# --- Review-tab candidate sources (search without committing + download-by-ref) ---
# Only sources with a clean search -> ref -> download-by-id path. Soulseek +
# Telegram are deferred (P2P/bot listings are slow and awkward to enumerate).


def _qobuz_candidate() -> CandidateSource:  # pragma: no cover - IO wiring
    from crate.adapters.sources import qobuz
    from crate.infrastructure.qobuz.client import QobuzClient

    client = QobuzClient(
        os.environ["CRATE_QOBUZ_APP_ID"],
        os.environ["CRATE_QOBUZ_APP_SECRET"],
        os.environ["CRATE_QOBUZ_TOKEN"],
    )

    def search(query: str, want: TrackIdentity | None) -> list[Candidate]:
        return qobuz.search_candidates(query, want, search=client.search)

    def download(cand: Candidate, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return qobuz.download_candidate(
            cand, out_dir, want, get_file_url=client.get_file_url, fetch=client.fetch
        )

    return search, download


def _monochrome_candidate() -> CandidateSource:  # pragma: no cover - IO wiring
    from crate.adapters.sources import monochrome

    session = _shared_amz()

    def search_json(query: str) -> Any:
        return http.get_json(f"{DEFAULT_TIDAL_BASE}/search/", {"s": query})

    def fetch(url: str, dest: Path) -> None:
        http.stream_to(url, dest)

    def search(query: str, want: TrackIdentity | None) -> list[Candidate]:
        return monochrome.search_candidates(query, want, search_json=search_json)

    def download(cand: Candidate, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return monochrome.download_candidate(
            cand, out_dir, want, amz_fetch=session.fetch_amz, fetch=fetch, decrypt=mp4decrypt_cenc
        )

    return search, download


def _deezer_candidate() -> CandidateSource:  # pragma: no cover - IO wiring
    from crate.adapters.sources import deezer
    from crate.infrastructure.deezer.client import DeezerClient

    client = DeezerClient(os.environ["CRATE_DEEZER_ARL"])

    def search(query: str, want: TrackIdentity | None) -> list[Candidate]:
        return deezer.search_candidates(query, want, search=client.search)

    def download(cand: Candidate, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return deezer.download_candidate(
            cand, out_dir, want, resolve_url=client.resolve_url, fetch=client.fetch
        )

    return search, download


def _arcod_candidate() -> CandidateSource:  # pragma: no cover - IO wiring
    from crate.adapters.sources import arcod
    from crate.infrastructure.arcod.client import ArcodClient

    client = ArcodClient(os.environ.get("CRATE_ARCOD_BASE", "https://arcod.xyz"))

    def search(query: str, want: TrackIdentity | None) -> list[Candidate]:
        return arcod.search_candidates(query, want, search=client.search)

    def download(cand: Candidate, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return arcod.download_candidate(
            cand,
            out_dir,
            want,
            create_download=client.create_download,
            poll=client.poll,
            fetch=client.fetch,
        )

    return search, download


def _ytdlp_candidate() -> CandidateSource:  # pragma: no cover - IO wiring
    def search(query: str, want: TrackIdentity | None) -> list[Candidate]:
        return ytdlp.search_candidates(query, want)

    def download(cand: Candidate, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return ytdlp.download_candidate(cand, out_dir, want)

    return search, download


# Candidate-capable factories only (search -> ref -> download-by-id). Soulseek and
# Telegram are intentionally absent (deferred, v1 non-goal).
_CANDIDATE_FACTORIES: dict[str, Callable[[], CandidateSource]] = {
    "qobuz": _qobuz_candidate,
    "monochrome": _monochrome_candidate,
    "deezer": _deezer_candidate,
    "arcod": _arcod_candidate,
    "ytdlp": _ytdlp_candidate,
}


def _enabled_source_names() -> list[str]:  # pragma: no cover - env + IO wiring
    """Names of currently-enabled sources, honouring the same rules as build_tiers:
    the stored layout (CRATE_DB set) wins, else the env enable flags."""
    from crate.composition.catalog import SOURCES, default_layout

    config = _config_store_if_enabled()
    if config is not None:
        layout = config.get_layout() or default_layout()
        return [name for t in layout["tiers"] for name in t["sources"]]
    names = [s.name for s in SOURCES if s.enable_flag is None or _enabled(s.enable_flag)]
    return names


def build_candidate_sources() -> dict[str, CandidateSource]:  # pragma: no cover - IO wiring
    """The review flow's candidate sources: ``{name: (search_fn, download_fn)}``.

    Only enabled, candidate-capable sources are wired (same enable/stored-layout
    rules as ``build_tiers``); a factory that raises for missing creds is skipped.
    Soulseek/Telegram are excluded by construction (deferred)."""
    out: dict[str, CandidateSource] = {}
    for name in _enabled_source_names():
        factory = _CANDIDATE_FACTORIES.get(name)
        if factory is None:
            continue
        try:
            out[name] = factory()
        except Exception:  # missing creds / unavailable -> skip
            continue
    return out


def _keyfile() -> Path:  # pragma: no cover - path
    return db_path().parent / "config.key"


def build_config_store() -> Any:  # pragma: no cover - IO wiring
    from crate.infrastructure.crypto import SecretBox
    from crate.infrastructure.sqlite.config_store import ConfigStore
    from crate.infrastructure.sqlite.db import connect

    return ConfigStore(connect(db_path()), SecretBox.from_keyfile(_keyfile()))


def _config_store_if_enabled() -> Any:  # pragma: no cover - IO wiring
    # The console/serve set CRATE_DB; ad-hoc local `download` runs stay env-only.
    return build_config_store() if os.environ.get("CRATE_DB") else None


def _apply_stored_secrets(config: Any) -> None:  # pragma: no cover - IO wiring
    """Push console-stored (encrypted) credentials into the environment so the
    source factories pick them up. Stored values take precedence over env."""
    from crate.composition.catalog import SOURCES

    for spec in SOURCES:
        for env_key in spec.required_env:
            if env_key in config.secret_keys(spec.name):
                value = config.get_secret(spec.name, env_key)
                if value is not None:
                    os.environ[env_key] = value


def _tiers_from_layout(layout: dict[str, Any]) -> list[TierMux]:  # pragma: no cover - IO wiring
    tiers: list[TierMux] = []
    for t in sorted(layout["tiers"], key=lambda t: int(t["tier"])):
        entries: list[SourceEntry] = []
        for name in t["sources"]:
            factory = _FACTORIES.get(name)
            if factory is None:
                continue
            try:
                entries.append(factory())
            except Exception:  # missing creds / unavailable -> skip this source
                continue
        strategy = MuxStrategy.UNIFORM if t["strategy"] == "uniform" else MuxStrategy.ALL
        if entries:
            tiers.append(TierMux(Tier(int(t["tier"])), entries, strategy))
    return tiers


def _tiers_from_env() -> list[TierMux]:  # pragma: no cover - env + IO wiring
    """Legacy env-only wiring (no config store): CRATE_* flags + CRATE_MUX_TIER*."""
    tier1: list[SourceEntry] = []
    if _enabled("CRATE_QOBUZ"):
        tier1.append(_qobuz_source())
    if _enabled("CRATE_MONOCHROME"):
        tier1.append(_monochrome_source())
    tier2: list[SourceEntry] = []
    if _enabled("CRATE_DEEZER"):
        tier2.append(_deezer_source())
    if _enabled("CRATE_ARCOD"):
        tier2.append(_arcod_source())
    if _enabled("CRATE_SOULSEEK"):
        tier2.append(_soulseek_source())
    if _enabled("CRATE_TELEGRAM"):
        tier2.append(_telegram_source())
    tier3 = [_ytdlp_source()]

    tiers: list[TierMux] = []
    if tier1:
        tiers.append(TierMux(Tier.ONE, tier1, _strategy(1)))
    if tier2:
        tiers.append(TierMux(Tier.TWO, tier2, _strategy(2)))
    tiers.append(TierMux(Tier.THREE, tier3, _strategy(3)))
    return tiers


def build_tiers() -> list[TierMux]:  # pragma: no cover - env + IO wiring
    """Build the per-tier source pools. When a config store is active (CRATE_DB
    set), the stored layout (membership/order/strategy) and encrypted credentials
    drive it; otherwise fall back to env flags."""
    from crate.composition.catalog import default_layout

    config = _config_store_if_enabled()
    if config is None:
        return _tiers_from_env()
    _apply_stored_secrets(config)
    layout = config.get_layout()
    if layout is None:
        layout = default_layout()
        config.seed_layout(layout)
    return _tiers_from_layout(layout)


def build_forced_tiers(name: str) -> list[TierMux]:  # pragma: no cover - env + IO wiring
    """A one-source cascade: every query goes through ``name`` and nothing else.

    The diagnostic counterpart to :func:`build_tiers` — it answers "is *this*
    source broken?" without the cascade masking the answer by falling through to
    a lower tier. Deliberately ignores the stored layout's membership, so a
    disabled or tray-held source can still be exercised; stored credentials are
    still applied. Raises ``KeyError`` for an unknown source, and lets a missing-
    credential failure propagate rather than silently skipping (unlike the
    cascade, a source that can't be built *is* the answer here)."""
    if name not in _FACTORIES:
        raise KeyError(name)
    config = _config_store_if_enabled()
    if config is not None:
        _apply_stored_secrets(config)
    entry = _FACTORIES[name]()
    return [TierMux(entry.ref.tier, [entry], MuxStrategy.ALL)]


def db_path() -> Path:  # pragma: no cover - env read
    """Console/history SQLite path. Must be a local FS, never NFS (ADR-0018)."""
    return Path(os.environ.get("CRATE_DB", "data/crate.db"))


def build_recorder(requested_via: str) -> Any:  # pragma: no cover - IO wiring
    """A RequestStore bound to ``requested_via`` for writing history."""
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.store import RequestStore

    return RequestStore(connect(db_path()), requested_via=requested_via)


def _tiers_provider(config: Any) -> Callable[[], list[TierMux]]:  # pragma: no cover - IO wiring
    """A memoised ``() -> tiers`` that rebuilds only when the stored config
    version changes (hot-reload). Shared by the shim and the queue worker."""
    lock = threading.Lock()
    cache: dict[str, Any] = {"version": None, "tiers": None}

    def current_tiers() -> list[TierMux]:
        with lock:
            version = config.version() if config is not None else 0
            if cache["tiers"] is None or cache["version"] != version:
                cache["tiers"] = build_tiers()
                cache["version"] = version
            return cast("list[TierMux]", cache["tiers"])

    return current_tiers


def build_finalizer() -> Any:  # pragma: no cover - IO wiring
    """A per-track librarian (organize + tag + move into ``CRATE_LIBRARY_ROOT``),
    or ``None`` when the library root is unset — in which case downloads stay in
    the staging dir and the previous (batch) behaviour is preserved."""
    root = os.environ.get("CRATE_LIBRARY_ROOT")
    if not root:
        return None
    from crate.application.librarian import make_finalizer
    from crate.infrastructure.audio.tags import embed_cover, read_tags, write_tags

    return make_finalizer(
        Path(root),
        read_tags=read_tags,
        write_tags=write_tags,
        fetch_art=http.fetch_bytes,
        embed_cover=embed_cover,
    )


def _bound_run(prober: Any, finalizer: Any) -> Callable[..., Any]:  # pragma: no cover - IO wiring
    """A ``run(query, out_dir, tiers, *, request_id, recorder, want, finalizer)``
    with ``prober`` and the default ``finalizer`` pre-bound — the shape the de-dup
    use-case injects. Callers override ``finalizer=None`` for the in-place upgrade
    cascade (the winner replaces the existing library file, not a fresh path)."""
    from crate.application.cascade import run_cascade

    def run(
        query: str,
        out_dir: Path,
        tiers: Any,
        *,
        request_id: str | None = None,
        recorder: Any = None,
        want: Any = None,
        finalizer: Any = finalizer,
    ) -> Any:
        return run_cascade(
            query,
            out_dir,
            tiers,
            prober=prober,
            recorder=recorder,
            request_id=request_id,
            finalizer=finalizer,
            want=want,
        )

    return run


def _atomic_replace() -> Callable[[Path, Path], None]:  # pragma: no cover - IO wiring
    """Move a produced file over an existing library path (upgrade in place)."""
    from crate.application.librarian import _atomic_move

    return _atomic_move


def build_shim_download() -> Callable[[str, Path], Path]:  # pragma: no cover - IO wiring
    """A ``download(query, out_dir) -> path`` that runs the full cascade — used by
    the slskd shim (`crate serve`) so Explo pulls through the tiered stack. Records
    every request to the console DB, de-dups on identity (reusing lossless files
    and upgrading lossy ones), and — with a library root set — finalizes each
    winner into the library, returning the final location.

    The shim only knows the advertised free-text query, so identity is parsed
    best-effort from it (``Artist - Title`` -> artist/title)."""
    from crate.application.dedup import resolve_request
    from crate.domain.identity import from_query
    from crate.infrastructure.audio.probe import probe

    recorder = build_recorder("explo")
    current_tiers = _tiers_provider(_config_store_if_enabled())
    run = _bound_run(probe, build_finalizer())
    replace = _atomic_replace()

    def download(query: str, out_dir: Path) -> Path:
        identity = from_query(query)
        rid = recorder.open_request(query, identity)
        return resolve_request(
            query,
            out_dir,
            rid,
            identity=identity,
            tiers=current_tiers(),
            recorder=recorder,
            run=run,
            exists=lambda p: p.exists(),
            replace=replace,
        )

    return download


def reap_orphans() -> int:  # pragma: no cover - IO wiring
    """Requeue downloads a previous ``serve`` process left ``downloading`` when it
    died. Run once at startup before the workers spawn (crash/restart recovery)."""
    return int(build_recorder("serve").reap_orphans())


def _review_candidate(row: dict[str, Any]) -> Candidate:  # pragma: no cover - trivial mapping
    """Reconstruct the stored :class:`Candidate` from a ``review_candidates`` row."""
    return Candidate(
        source=str(row["source"]),
        tier=int(row["tier"]),
        ref=str(row["ref"]),
        title=str(row["title"]),
        artist=str(row["artist"]),
        album=row["album"],
        quality_label=str(row["quality_label"]),
        lossless=bool(row["lossless"]),
        duration_s=row["duration_s"],
        size_bytes=row["size_bytes"],
    )


def build_queue_worker() -> Callable[[threading.Event], None]:  # pragma: no cover - IO wiring
    """A loop that drains console-queued requests. Runs in the `serve` process
    (which owns the NAS mount + full source toolchain), so console downloads share
    the same downloader as Explo. Attaches to each pre-created ``queued`` row.

    Two kinds of work item: an ordinary request goes through the cascade + de-dup
    (reuse a lossless copy, upgrade a lossy one); a request armed with a review
    candidate (``review_candidates`` row) skips the cascade and downloads exactly
    that candidate, then upgrades in place (lossy) or fulfils (failed)."""
    from crate.application.dedup import resolve_request
    from crate.application.review import MatchTooLow, run_review_download
    from crate.domain.fingerprint import similarity as fp_similarity
    from crate.infrastructure.audio.fingerprint import fingerprint as fp_raw
    from crate.infrastructure.audio.fingerprint import fpcalc_available
    from crate.infrastructure.audio.probe import probe

    store = build_recorder("console")
    current_tiers = _tiers_provider(_config_store_if_enabled())
    finalizer = build_finalizer()
    run = _bound_run(probe, finalizer)
    replace = _atomic_replace()
    candidate_sources = build_candidate_sources()
    out_dir = Path(os.environ.get("CRATE_SERVE_DOWNLOADS", "downloads"))
    poll = float(os.environ.get("CRATE_QUEUE_POLL", "2.0"))
    # Fingerprint gate is opt-in: only armed when the fpcalc binary is present
    # (the core image may omit it), else review upgrades replace ungated as before.
    fingerprinter = fp_raw if fpcalc_available() else None

    def handle(claimed: dict[str, Any]) -> None:
        review = store.get_review_candidate(claimed["id"])
        if review is not None:
            candidate = _review_candidate(review)
            source = candidate_sources.get(candidate.source)
            if source is None:
                raise RuntimeError(f"candidate source {candidate.source!r} not available")
            try:
                run_review_download(
                    claimed["id"],
                    out_dir,
                    candidate,
                    want=claimed["identity"],
                    is_upgrade=bool(review["is_upgrade"]),
                    target_path=review["target_path"],
                    download=source[1],
                    prober=probe,
                    finalizer=finalizer,
                    replace=replace,
                    recorder=store,
                    fingerprinter=fingerprinter,
                    similarity=fp_similarity,
                    force=bool(review["force"]),
                )
            except MatchTooLow as low:
                # Gate refused the replace; park for the user instead of failing.
                store.mark_needs_confirm(claimed["id"], low.score)
            return
        resolve_request(
            claimed["query"],
            out_dir,
            claimed["id"],
            identity=claimed["identity"],
            tiers=current_tiers(),
            recorder=store,
            run=run,
            exists=lambda p: p.exists(),
            replace=replace,
        )

    def loop(stop: threading.Event) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        while not stop.is_set():
            claimed = store.claim_next()
            if claimed is None:
                stop.wait(poll)
                continue
            try:
                handle(claimed)
            except Exception as exc:  # never let one bad request kill the loop
                store.fail_request(claimed["id"], f"queue worker error: {exc}")

    return loop


def _retention_days() -> int:  # pragma: no cover - env read
    return int(os.environ.get("CRATE_ATTEMPTS_RETENTION_DAYS", "90"))


def prune_attempts() -> int:  # pragma: no cover - IO wiring
    """Drop attempt rows older than the retention window (default 90 d). Run once
    at ``serve`` startup and then daily by the maintenance thread; the ``attempts``
    table is append-only and otherwise grows without bound."""
    return int(build_recorder("serve").prune_attempts(_retention_days()))


def build_maintenance() -> Callable[[threading.Event], None]:  # pragma: no cover - IO wiring
    """A daily loop that prunes stale ``attempts`` rows. Runs in the ``serve``
    process alongside the queue workers and shares its ``stop`` Event. Mirrors
    ``build_queue_worker``: build the store once, then loop until stopped. The
    startup prune is done separately in ``serve`` (like ``reap_orphans``); this
    thread handles the recurring pass so a long-lived server keeps retention."""
    store = build_recorder("serve")
    retention_days = _retention_days()
    interval = float(os.environ.get("CRATE_MAINTENANCE_INTERVAL", "86400"))

    def loop(stop: threading.Event) -> None:
        while not stop.wait(interval):  # first sleeps, then prunes (startup prune runs in serve)
            try:
                store.prune_attempts(retention_days)
            except Exception:  # never let a maintenance hiccup kill the thread
                continue

    return loop


def build_spotify_search() -> Callable[[str, int], list[dict[str, Any]]] | None:
    """Spotify track search for the console, or ``None`` when creds are absent
    (the /api/search endpoint then reports 503 and the UI hides search)."""
    client_id = os.environ.get("CRATE_SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("CRATE_SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    import httpx  # pragma: no cover - IO wiring

    from crate.infrastructure.spotify.client import SpotifyClient

    http_client = httpx.Client(timeout=8.0, follow_redirects=True)  # pragma: no cover

    def post_form(
        url: str, data: dict[str, str], headers: dict[str, str]
    ) -> Any:  # pragma: no cover
        resp = http_client.post(url, data=data, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_json(
        url: str, params: dict[str, Any], headers: dict[str, str]
    ) -> Any:  # pragma: no cover
        resp = http_client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    client = SpotifyClient(client_id, client_secret, post_form=post_form, get_json=get_json)
    return client.search


def build_bulk_resolver() -> Callable[[str], Any]:  # pragma: no cover - IO wiring
    """Resolve pasted text into a batch: Spotify playlists (if creds present),
    YouTube playlists (yt-dlp), and plain pasted lists always work."""
    from crate.application.bulk import ResolvedBatch, resolve_bulk
    from crate.infrastructure.youtube import resolve_youtube_playlist

    spotify_playlist: Callable[[str], dict[str, Any]] | None = None
    client_id = os.environ.get("CRATE_SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("CRATE_SPOTIFY_CLIENT_SECRET")
    if client_id and client_secret:
        import httpx

        from crate.infrastructure.spotify.client import SpotifyClient

        http_client = httpx.Client(timeout=12.0, follow_redirects=True)

        def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
            resp = http_client.post(url, data=data, headers=headers)
            resp.raise_for_status()
            return resp.json()

        def get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
            resp = http_client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            return resp.json() if resp.content else None

        client = SpotifyClient(client_id, client_secret, post_form=post_form, get_json=get_json)
        spotify_playlist = client.playlist

    def resolve(text: str) -> ResolvedBatch:
        return resolve_bulk(
            text, spotify_playlist=spotify_playlist, youtube_playlist=resolve_youtube_playlist
        )

    return resolve


if TYPE_CHECKING:
    from crate.infrastructure.spotify.client import SpotifyClient as _SpotifyClient
    from crate.infrastructure.sqlite.config_store import ConfigStore as _ConfigStore
    from crate.infrastructure.sqlite.store import RequestStore as _RequestStore

_SPOTIFY_REFRESH = ("spotify", "refresh_token")
_ACCESS_TTL_S = 3000.0  # Spotify access tokens live 3600 s; refresh comfortably early


class SpotifyOAuthProvider:
    """User-scoped Spotify player integration for the console (ADR-0018 v4).

    Holds the operator's refresh token (encrypted in the config store) and trades
    it for short-lived access tokens to read their current + recent tracks. The
    client-credentials search flow cannot reach these endpoints — they need the
    user's own consent.
    """

    def __init__(
        self,
        client: _SpotifyClient,
        redirect_uri: str,
        config: _ConfigStore,
        store: _RequestStore,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._redirect = redirect_uri
        self._config = config
        self._store = store
        self._now = now
        self._access: str | None = None
        self._access_exp = 0.0

    def connected(self) -> bool:
        return self._config.get_secret(*_SPOTIFY_REFRESH) is not None

    def authorize_url(self, state: str) -> str:
        return self._client.authorize_url(self._redirect, state)

    def connect(self, code: str) -> None:
        refresh = self._client.exchange_code(code, self._redirect)
        self._config.put_secret(*_SPOTIFY_REFRESH, refresh)
        self._access = None  # force a fresh access token on the next player() call

    def _bearer(self) -> str:
        if self._access is not None and self._now() < self._access_exp:
            return self._access
        refresh = self._config.get_secret(*_SPOTIFY_REFRESH)
        if refresh is None:
            raise RuntimeError("spotify not connected")
        self._access = self._client.refresh_access_token(refresh)
        self._access_exp = self._now() + _ACCESS_TTL_S
        return self._access

    def _in_library(self, query: str) -> bool:
        return bool(self._store.list_requests(query=query, status="done", limit=1))

    def player(self) -> dict[str, Any]:
        token = self._bearer()
        now = self._client.currently_playing(token)
        recent = self._client.recently_played(token, limit=8)
        if now is not None:
            now["in_library"] = self._in_library(now["query"])
        for track in recent:
            track["in_library"] = self._in_library(track["query"])
        return {"now_playing": now, "recent": recent}


def build_spotify_oauth(
    config: _ConfigStore, store: _RequestStore
) -> SpotifyOAuthProvider | None:  # pragma: no cover - IO wiring
    """Provider for the console's Spotify "Listening" panel, or ``None`` when the
    client creds or redirect URI are absent (the panel then hides)."""
    client_id = os.environ.get("CRATE_SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("CRATE_SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("CRATE_SPOTIFY_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        return None
    import httpx

    from crate.infrastructure.spotify.client import SpotifyClient

    http_client = httpx.Client(timeout=8.0, follow_redirects=True)

    def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        resp = http_client.post(url, data=data, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        resp = http_client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else None  # 204 => nothing playing

    client = SpotifyClient(client_id, client_secret, post_form=post_form, get_json=get_json)
    return SpotifyOAuthProvider(client, redirect_uri, config, store)


def build_identifier() -> Any:  # pragma: no cover - IO wiring
    """Advisory AcoustID identifier for the review hint: ``path -> IdentityHint``.

    Returns None when the feature is off — no ``fpcalc`` binary or no
    ``CRATE_ACOUSTID_KEY`` — so the ``/review/{id}/identify`` endpoint returns
    ``{}`` and the UI falls back to metadata only (ADR-0019)."""
    from crate.infrastructure.audio.fingerprint import fingerprint_compressed, fpcalc_available

    key = os.environ.get("CRATE_ACOUSTID_KEY")
    if not key or not fpcalc_available():
        return None
    from pathlib import Path as _Path

    from crate.application.identify import identify
    from crate.domain.identity import IdentityHint
    from crate.infrastructure.acoustid.client import AcoustidClient

    client = AcoustidClient(key)

    def identifier(path: _Path) -> IdentityHint | None:
        return identify(path, fingerprint_compressed=fingerprint_compressed, lookup=client.lookup)

    return identifier


def build_candidate_streamers() -> dict[str, Callable[[str], str]]:  # pragma: no cover - IO wiring
    """Per-source resolvers ``ref -> direct streamable URL`` for the review A/B
    audition proxy. Only sources with a cheap direct URL (Range-able, no full
    download) are wired: Qobuz (signed FLAC url) and yt-dlp (``-g``). Deezer plays
    its static preview clip client-side; arcod (async job) / monochrome (DRM) have
    no such URL and are simply absent -> their rows show no play button."""
    import subprocess

    streamers: dict[str, Callable[[str], str]] = {}

    if _enabled("CRATE_QOBUZ"):
        from crate.infrastructure.qobuz.client import QobuzClient

        qobuz = QobuzClient(
            os.environ["CRATE_QOBUZ_APP_ID"],
            os.environ["CRATE_QOBUZ_APP_SECRET"],
            os.environ["CRATE_QOBUZ_TOKEN"],
        )
        streamers["qobuz"] = lambda ref: qobuz.get_file_url(int(ref))

    def ytdlp_stream(ref: str) -> str:
        cmd = ytdlp.build_stream_url_command(ref)  # validates the video id
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        return ytdlp.parse_stream_url(out.stdout)

    streamers["ytdlp"] = ytdlp_stream
    return streamers


def build_source_validator() -> Callable[
    [str, str | None], dict[str, Any]
]:  # pragma: no cover - IO wiring
    """``(name, query) -> {ok, detail, quality_label, elapsed_s}``: force a download
    through *one* source to prove it works.

    Prefers the review candidate halves (search + download-by-ref) because they
    reach the cred-gated resolve — a stale token fails there even though a bare
    search passes. Sources with no candidate path (soulseek/telegram resolve free
    text internally) fall back to the cascade's own download half, so every source
    is testable.

    ``query`` defaults to the well-known test track. A caller-supplied query is a
    free-text probe with no expected identity, so the artist/title gates stay off
    and the source is judged only on whether it can deliver *that* track."""
    import tempfile

    from crate.application.validate import validate_source, validate_source_direct
    from crate.infrastructure.audio.probe import probe

    out_dir = Path(tempfile.gettempdir()) / "crate-validate"
    out_dir.mkdir(parents=True, exist_ok=True)

    def fail(detail: str) -> dict[str, Any]:
        return {"ok": False, "detail": detail, "quality_label": None, "elapsed_s": None}

    def validate(name: str, query: str | None = None) -> dict[str, Any]:
        # a custom query carries no known identity; the default test track does
        kw: dict[str, Any] = {"query": query, "want": None} if query else {}
        # console-saved credentials live in the config store, not the environment;
        # push them out first or a source configured via the UI looks unconfigured
        config = _config_store_if_enabled()
        if config is not None:
            _apply_stored_secrets(config)
        entry = build_candidate_sources().get(name)
        if entry is not None:
            res = validate_source(
                search=entry[0], download=entry[1], prober=probe, out_dir=out_dir, **kw
            )
        else:
            factory = _FACTORIES.get(name)
            if factory is None:
                return fail("unknown source")
            try:
                download = factory().download
            except KeyError as exc:  # factories read creds via os.environ[...]
                return fail(f"not configured: {exc.args[0]} is missing")
            except Exception as exc:  # unreachable daemon, bad URL, ...
                return fail(f"could not initialise: {exc}"[:200])
            res = validate_source_direct(download=download, prober=probe, out_dir=out_dir, **kw)
        return {
            "ok": res.ok,
            "detail": res.detail,
            "quality_label": res.quality_label,
            "elapsed_s": res.elapsed_s,
        }

    return validate


def build_console_app() -> Any:  # pragma: no cover - IO wiring
    """FastAPI app for `crate console`: a read-only view over the history DB."""
    from crate.adapters.inbound.ops_api import create_app
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.store import RequestStore

    store = RequestStore(connect(db_path()), requested_via="console")
    config = build_config_store()
    static_env = os.environ.get("CRATE_CONSOLE_STATIC")
    static_dir = Path(static_env) if static_env else None
    return create_app(
        store,
        api_key=os.environ.get("CRATE_OPS_API_KEY") or None,
        static_dir=static_dir,
        config=config,
        search=build_spotify_search(),
        spotify=build_spotify_oauth(config, store),
        bulk_resolve=build_bulk_resolver(),
        candidate_sources=build_candidate_sources,
        identify_hint=build_identifier(),
        candidate_streamers=build_candidate_streamers,
        source_validator=build_source_validator(),
    )
