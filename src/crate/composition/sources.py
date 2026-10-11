"""Composition root — wires swappable source modules into per-tier pools.

Each tier is a :class:`TierMux` (a pool of sources + a fan-out strategy), so a
tier can load-balance across several sources. Strategy is configurable per tier
via ``CRATE_MUX_TIER{1,2,3}`` = ``all`` (default, try every source) or
``uniform``/``distribute`` (round-robin one source per request).

  Tier 1  Qobuz (hi-res FLAC, direct)        opt-in CRATE_QOBUZ=1
          Monochrome (direct FLAC)          opt-in CRATE_MONOCHROME=1
          Antra (hosted lossless resolver)  opt-in CRATE_ANTRA=1
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
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from crate.adapters.sources import ytdlp
from crate.adapters.sources.monochrome import monochrome_download
from crate.application.auth import SessionStore, SpotifyProfile, User, UserRepository
from crate.application.cascade import MuxStrategy, SourceEntry, TierMux
from crate.domain.candidate import Candidate
from crate.domain.download import DownloadOutcome
from crate.domain.identity import TrackIdentity
from crate.domain.tiers import SourceRef, Tier
from crate.infrastructure import http

# Review-tab candidate source: (search-without-downloading, download-one-candidate).
SearchCandidatesFn = Callable[[str, "TrackIdentity | None"], list[Candidate]]
DownloadCandidateFn = Callable[[Candidate, Path, "TrackIdentity | None"], DownloadOutcome]
CandidateSource = tuple[SearchCandidatesFn, DownloadCandidateFn]


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _strategy(tier_num: int) -> MuxStrategy:
    value = os.environ.get(f"CRATE_MUX_TIER{tier_num}", "all").lower()
    return MuxStrategy.UNIFORM if value in ("uniform", "distribute") else MuxStrategy.ALL


@lru_cache(maxsize=4)
def _monochrome_client(base_url: str) -> Any:  # pragma: no cover - IO wiring
    """Reuse HTTP connections across downloads, review searches and tier rebuilds."""
    from crate.infrastructure.monochrome.client import MonochromeClient

    return MonochromeClient(base_url)


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
    from crate.infrastructure.monochrome.client import DEFAULT_BASE

    client = _monochrome_client(os.environ.get("CRATE_MONOCHROME_BASE", DEFAULT_BASE))

    def monochrome(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return monochrome_download(
            query,
            out_dir,
            want,
            search=client.search,
            fetch=client.fetch,
        )

    return SourceEntry(SourceRef("monochrome", Tier.ONE), monochrome, expected_lossless=True)


def _antra_client() -> Any:  # pragma: no cover - IO wiring
    from crate.infrastructure.antra.client import DEFAULT_BASE, AntraClient

    return AntraClient(
        os.environ["CRATE_ANTRA_USERNAME"],
        os.environ["CRATE_ANTRA_PASSWORD"],
        os.environ.get("CRATE_ANTRA_BASE", DEFAULT_BASE),
    )


def _antra_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.antra import antra_download

    client = _antra_client()

    def antra(query: str, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return antra_download(query, out_dir, want, download=client.download_track)

    return SourceEntry(SourceRef("antra", Tier.ONE), antra, expected_lossless=True)


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
    "antra": _antra_source,
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
    from crate.infrastructure.monochrome.client import DEFAULT_BASE

    client = _monochrome_client(os.environ.get("CRATE_MONOCHROME_BASE", DEFAULT_BASE))

    def search(query: str, want: TrackIdentity | None) -> list[Candidate]:
        return monochrome.search_candidates(query, want, search=client.search)

    def download(cand: Candidate, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return monochrome.download_candidate(cand, out_dir, want, fetch=client.fetch)

    return search, download


def _antra_candidate() -> CandidateSource:  # pragma: no cover - IO wiring
    from crate.adapters.sources import antra

    client = _antra_client()

    def search(query: str, want: TrackIdentity | None) -> list[Candidate]:
        return antra.search_candidates(query, want)

    def download(cand: Candidate, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return antra.download_candidate(cand, out_dir, want, download=client.download_track)

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


def _slskd_client() -> Any:  # pragma: no cover - IO wiring
    from crate.infrastructure.soulseek.slskd_client import SlskdClient

    return SlskdClient(
        os.environ["CRATE_SLSKD_URL"],
        os.environ["CRATE_SLSKD_API_KEY"],
        os.environ["CRATE_SLSKD_DOWNLOADS"],
    )


def _soulseek_candidate() -> CandidateSource:  # pragma: no cover - IO wiring
    """Soulseek as a review candidate source. Its ``search`` is the title-gated
    contract the other sources share; the console's review fan-out skips it (P2P
    search is slow) and offers the raw Nicotine+-style browser instead. The
    ``download`` half is what matters: fetch exactly the browsed file the user
    picked (ref = peer + remote path + size)."""
    from crate.adapters.sources import soulseek

    client = _slskd_client()

    def search(query: str, want: TrackIdentity | None) -> list[Candidate]:
        return soulseek.search_candidates(query, want, search=client.search)

    def download(cand: Candidate, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return soulseek.download_candidate(cand, out_dir, want, download=client.download)

    return search, download


def _ytdlp_candidate() -> CandidateSource:  # pragma: no cover - IO wiring
    def search(query: str, want: TrackIdentity | None) -> list[Candidate]:
        return ytdlp.search_candidates(query, want)

    def download(cand: Candidate, out_dir: Path, want: TrackIdentity | None) -> DownloadOutcome:
        return ytdlp.download_candidate(cand, out_dir, want)

    return search, download


# Candidate-capable factories only (search -> ref -> download-by-id). Telegram is
# intentionally absent (deferred, v1 non-goal). Soulseek is here for its
# download-by-ref half (the review browser); the generic fan-out skips its search.
_CANDIDATE_FACTORIES: dict[str, Callable[[], CandidateSource]] = {
    "qobuz": _qobuz_candidate,
    "monochrome": _monochrome_candidate,
    "antra": _antra_candidate,
    "deezer": _deezer_candidate,
    "arcod": _arcod_candidate,
    "ytdlp": _ytdlp_candidate,
    "soulseek": _soulseek_candidate,
}

# Manual searches are operator-initiated and do not participate in the automatic
# cascade. Credential-free providers can therefore remain searchable even when a
# host's stored tier layout has them in the disabled tray. This also keeps the
# provider Test action, Review search, and direct source search consistent.
_MANUAL_CANDIDATES_ALWAYS_AVAILABLE = frozenset({"monochrome"})


def _enabled_source_names() -> list[str]:  # pragma: no cover - env + IO wiring
    """Names of currently-enabled sources, honouring the same rules as build_tiers:
    the stored layout (CRATE_DB set) wins, else the env enable flags."""
    from crate.composition.catalog import SOURCES, default_layout, reconcile_layout

    config = _config_store_if_enabled()
    if config is not None:
        stored = config.get_layout()
        layout = reconcile_layout(stored) if stored else default_layout()
        return [name for t in layout["tiers"] for name in t["sources"]]
    names = [s.name for s in SOURCES if s.enable_flag is None or _enabled(s.enable_flag)]
    return names


def build_candidate_sources() -> dict[str, CandidateSource]:  # pragma: no cover - IO wiring
    """The review flow's candidate sources: ``{name: (search_fn, download_fn)}``.

    Enabled candidate-capable sources are wired, plus credential-free manual
    search providers such as Monochrome. Automatic cascade membership is still
    controlled by the stored layout. A factory that raises for missing creds is
    skipped. Telegram is excluded by construction (deferred)."""
    out: dict[str, CandidateSource] = {}
    names = dict.fromkeys([*_enabled_source_names(), *sorted(_MANUAL_CANDIDATES_ALWAYS_AVAILABLE)])
    for name in names:
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
    if _enabled("CRATE_ANTRA"):
        tier1.append(_antra_source())
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
    from crate.composition.catalog import default_layout, reconcile_layout

    config = _config_store_if_enabled()
    if config is None:
        return _tiers_from_env()
    _apply_stored_secrets(config)
    layout = config.get_layout()
    if layout is None:
        layout = default_layout()
        config.seed_layout(layout)
    else:
        layout = reconcile_layout(layout)
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
    from crate.application.review import MatchTooLow, handle_review_error, run_review_download
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
            except Exception as exc:
                # A source/network error (e.g. a transient Qobuz 401) must not nuke
                # the request: keep an existing upgrade file, stay retryable, and
                # fail with a clean, attributed message instead of "queue worker
                # error: <url>".
                handle_review_error(
                    claimed["id"],
                    candidate,
                    exc,
                    is_upgrade=bool(review["is_upgrade"]),
                    target_path=review["target_path"],
                    recorder=store,
                    exists=lambda p: p.exists(),
                )
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
    jobs = build_job_store()
    retention_days = _retention_days()
    interval = float(os.environ.get("CRATE_MAINTENANCE_INTERVAL", "86400"))

    def loop(stop: threading.Event) -> None:
        while not stop.wait(interval):  # first sleeps, then prunes (startup prune runs in serve)
            try:
                store.prune_attempts(retention_days)
                _prune_jobs(jobs)
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

_ACCESS_TTL_S = 3000.0  # Spotify access tokens live 3600 s; refresh comfortably early


class SpotifyUser:
    """Per-user Spotify reads — the "Listening" panel + library import (Phase 2).

    Trades each logged-in user's stored refresh token (vault key
    ``spotify:<user_id>``, minted by :class:`AuthProvider` at login) for
    short-lived access tokens, cached per user. There is no separate "connect"
    flow anymore: Login-with-Spotify is the single OAuth round-trip.
    """

    def __init__(
        self,
        client: _SpotifyClient,
        config: _ConfigStore,
        store: _RequestStore,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._config = config
        self._store = store
        self._now = now
        self._access: dict[str, tuple[str, float]] = {}  # user_id -> (token, expiry)

    @staticmethod
    def _key(user_id: str) -> tuple[str, str]:
        return (f"spotify:{user_id}", "refresh_token")

    def connected(self, user_id: str) -> bool:
        return self._config.get_secret(*self._key(user_id)) is not None

    def _bearer(self, user_id: str) -> str:
        cached = self._access.get(user_id)
        if cached is not None and self._now() < cached[1]:
            return cached[0]
        refresh = self._config.get_secret(*self._key(user_id))
        if refresh is None:
            raise RuntimeError("spotify not connected")
        tokens = self._client.refresh_tokens(refresh)
        vault = f"spotify:{user_id}"
        if tokens.refresh_token and tokens.refresh_token != refresh:
            # Spotify rotated it — persist, or the old one eventually stops working
            # (and the other process, which shares the vault, would be stranded).
            self._config.put_secret(vault, "refresh_token", tokens.refresh_token)
        if tokens.scope is not None:
            self._config.put_secret(vault, "scopes", tokens.scope)
        self._access[user_id] = (tokens.access_token, self._now() + _ACCESS_TTL_S)
        return tokens.access_token

    def missing_scopes(self, user_id: str) -> list[str]:
        """OAuth scopes this user must re-consent to (drives the re-login banner).
        Empty when not connected — there's no token to upgrade yet."""
        from crate.infrastructure.spotify.client import missing_scopes

        if not self.connected(user_id):
            return []
        return missing_scopes(self._config.get_secret(f"spotify:{user_id}", "scopes"))

    def _in_library(self, query: str) -> bool:
        return bool(self._store.list_requests(query=query, status="done", limit=1))

    def player(self, user_id: str) -> dict[str, Any]:
        token = self._bearer(user_id)
        now = self._client.currently_playing(token)
        recent = self._client.recently_played(token, limit=8)
        if now is not None:
            now["in_library"] = self._in_library(now["query"])
        for track in recent:
            track["in_library"] = self._in_library(track["query"])
        return {"now_playing": now, "recent": recent}

    # --- library import (SpotifyLibraryReader) ---
    def my_playlists(self, user_id: str) -> list[dict[str, Any]]:
        return self._client.current_user_playlists(self._bearer(user_id))

    def liked_songs(self, user_id: str, max_tracks: int = 2000) -> list[dict[str, Any]]:
        return self._client.saved_tracks(self._bearer(user_id), max_tracks)

    def playlist_tracks(
        self, user_id: str, playlist_id: str, max_tracks: int = 2000
    ) -> list[dict[str, Any]]:
        return self._client.user_playlist_tracks(self._bearer(user_id), playlist_id, max_tracks)

    def playlist_snapshot(self, user_id: str, playlist_id: str) -> str | None:
        """The playlist's current ``snapshot_id`` (for the sync watcher's cheap
        unchanged-playlist short-circuit)."""
        return self._client.user_playlist_snapshot(self._bearer(user_id), playlist_id)

    # --- Library Sync: listening ledger + tier reads ---
    def recent_plays_after(
        self, user_id: str, after_ms: int | None
    ) -> tuple[list[dict[str, Any]], int | None]:
        """This user's plays after ``after_ms`` + the next cursor (the ledger poll)."""
        return self._client.recently_played_after(self._bearer(user_id), after_ms)

    def top_tracks(self, user_id: str, time_range: str, limit: int = 100) -> list[dict[str, Any]]:
        """This user's top tracks for a time range (needs ``user-top-read``)."""
        return self._client.top_tracks(self._bearer(user_id), time_range, limit)


def build_spotify_user(
    config: _ConfigStore, store: _RequestStore
) -> SpotifyUser | None:  # pragma: no cover - IO wiring
    """Per-user Spotify reads (player + library) for the console, or ``None`` when
    the client creds are absent (the panel/import then hide). Uses the per-user
    tokens login already stores; needs no redirect URI of its own."""
    client_id = os.environ.get("CRATE_SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("CRATE_SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
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
    return SpotifyUser(client, config, store)


class AuthProvider:
    """Login-with-Spotify façade the console API calls (Phase 2, ADR-0018 v5).

    One OAuth round-trip authenticates AND links the account: exchange the code,
    read ``/v1/me`` for identity, upsert the user, persist their refresh token
    per-user in the config vault (key ``spotify:<user_id>``, for Phase B library
    reads), and mint a session. Constructed with injected collaborators so the
    orchestration is unit-tested with fakes.
    """

    def __init__(
        self,
        client: _SpotifyClient,
        redirect_uri: str,
        config: _ConfigStore,
        users: UserRepository,
        sessions: SessionStore,
        *,
        admin_spotify_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._client = client
        self._redirect = redirect_uri
        self._config = config
        self._users = users
        self._sessions = sessions
        self._admins = admin_spotify_ids

    def login_url(self, state: str) -> str:
        from crate.infrastructure.spotify.client import LOGIN_SCOPES

        return self._client.authorize_url(self._redirect, state, LOGIN_SCOPES)

    def complete_login(self, code: str) -> tuple[User, str]:
        tokens = self._client.exchange_code_tokens(code, self._redirect)
        me = self._client.me(tokens["access_token"])
        if not me.get("id"):
            raise RuntimeError("spotify profile missing id")
        user = self._users.upsert_spotify_user(
            SpotifyProfile(
                spotify_id=me["id"],
                display_name=me["display_name"],
                email=me.get("email"),
                avatar_url=me.get("avatar"),
            )
        )
        if self._admins:
            # CRATE_ADMIN_SPOTIFY_IDS is the source of truth when set: grant or
            # revoke at every login so the env always wins.
            want_admin = me["id"] in self._admins
            if user.is_admin != want_admin:
                user = self._users.set_admin(user.id, want_admin) or user
        self._config.put_secret(f"spotify:{user.id}", "refresh_token", tokens["refresh_token"])
        if tokens.get("scope"):
            self._config.put_secret(f"spotify:{user.id}", "scopes", tokens["scope"])
        raw = self._sessions.create(user.id)
        return user, raw

    def resolve_session(self, raw_token: str) -> User | None:
        user_id = self._sessions.resolve(raw_token)
        return self._users.get(user_id) if user_id else None

    def logout(self, raw_token: str) -> None:
        self._sessions.revoke(raw_token)


def build_auth(
    config: _ConfigStore, conn: Any
) -> AuthProvider | None:  # pragma: no cover - IO wiring
    """Login-with-Spotify provider, or ``None`` when the Spotify client creds /
    redirect URI are absent (the console then falls back to key-only auth).
    Shares the console's DB connection for the users/sessions tables."""
    client_id = os.environ.get("CRATE_SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("CRATE_SPOTIFY_CLIENT_SECRET")
    redirect_uri = os.environ.get("CRATE_SPOTIFY_REDIRECT_URI")
    if not client_id or not client_secret or not redirect_uri:
        return None
    import httpx

    from crate.infrastructure.spotify.client import SpotifyClient
    from crate.infrastructure.sqlite.user_store import SqliteSessionStore, SqliteUserRepository

    http_client = httpx.Client(timeout=8.0, follow_redirects=True)

    def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        resp = http_client.post(url, data=data, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        resp = http_client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    client = SpotifyClient(client_id, client_secret, post_form=post_form, get_json=get_json)
    admins = frozenset(
        s.strip() for s in os.environ.get("CRATE_ADMIN_SPOTIFY_IDS", "").split(",") if s.strip()
    )
    return AuthProvider(
        client,
        redirect_uri,
        config,
        SqliteUserRepository(conn),
        SqliteSessionStore(conn),
        admin_spotify_ids=admins,
    )


def build_sso(config: Any, conn: Any) -> Any | None:  # pragma: no cover - IO wiring
    """SSO sign-in (ADR-0023), or ``None`` when CRATE_OIDC_ISSUER is unset. A
    half-configured provider fails at startup rather than locking everyone out
    later. Spotify linking reuses the Login-with-Spotify client and callback."""
    issuer = os.environ.get("CRATE_OIDC_ISSUER", "").strip()
    if not issuer:
        return None
    client_id = os.environ.get("CRATE_OIDC_CLIENT_ID", "").strip()
    redirect_uri = os.environ.get("CRATE_OIDC_REDIRECT_URI", "").strip()
    if not client_id or not redirect_uri:
        raise RuntimeError(
            "CRATE_OIDC_ISSUER is set but CRATE_OIDC_CLIENT_ID or CRATE_OIDC_REDIRECT_URI is not"
        )
    import httpx

    from crate.application.oidc_auth import OidcAuthProvider
    from crate.infrastructure.oidc.client import DEFAULT_SCOPES, OidcClient
    from crate.infrastructure.spotify.client import LOGIN_SCOPES, SpotifyClient
    from crate.infrastructure.sqlite.user_store import SqliteSessionStore, SqliteUserRepository

    http_client = httpx.Client(timeout=10.0, follow_redirects=True)

    def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        resp = http_client.post(url, data=data, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def oidc_get(url: str, headers: Any) -> Any:
        resp = http_client.get(url, headers=dict(headers))
        resp.raise_for_status()
        return resp.json()

    def spotify_get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        resp = http_client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    oidc = OidcClient(
        issuer,
        client_id,
        os.environ.get("CRATE_OIDC_CLIENT_SECRET") or None,
        redirect_uri,
        get_json=oidc_get,
        post_form=post_form,
        scopes=os.environ.get("CRATE_OIDC_SCOPES") or DEFAULT_SCOPES,
    )
    sp_id = os.environ.get("CRATE_SPOTIFY_CLIENT_ID")
    sp_secret = os.environ.get("CRATE_SPOTIFY_CLIENT_SECRET")
    sp_redirect = os.environ.get("CRATE_SPOTIFY_REDIRECT_URI")
    spotify = (
        SpotifyClient(sp_id, sp_secret, post_form=post_form, get_json=spotify_get)
        if sp_id and sp_secret and sp_redirect
        else None
    )

    def _csv(name: str) -> frozenset[str]:
        return frozenset(s.strip() for s in os.environ.get(name, "").split(",") if s.strip())

    return OidcAuthProvider(
        oidc,
        SqliteUserRepository(conn),
        SqliteSessionStore(conn),
        config,
        label=os.environ.get("CRATE_OIDC_NAME", "").strip() or "SSO",
        spotify=spotify,
        spotify_redirect=sp_redirect,
        spotify_scopes=LOGIN_SCOPES,
        admin_groups=_csv("CRATE_OIDC_ADMIN_GROUPS"),
        groups_claim=os.environ.get("CRATE_OIDC_GROUPS_CLAIM", "").strip() or "groups",
        username_claim=os.environ.get("CRATE_OIDC_USERNAME_CLAIM", "").strip()
        or "preferred_username",
        admin_spotify_ids=_csv("CRATE_ADMIN_SPOTIFY_IDS"),
    )


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
    download) are wired: Qobuz (signed FLAC), Monochrome (direct FLAC), and
    yt-dlp (``-g``). Deezer plays its preview clip client-side; arcod has no URL."""
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
    from crate.infrastructure.monochrome.client import DEFAULT_BASE

    mono_base = os.environ.get("CRATE_MONOCHROME_BASE", DEFAULT_BASE).rstrip("/")
    streamers["monochrome"] = lambda ref: f"{mono_base}/track/{int(ref)}"
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

    conn = connect(db_path())
    store = RequestStore(conn, requested_via="console")
    config = build_config_store()
    static_env = os.environ.get("CRATE_CONSOLE_STATIC")
    static_dir = Path(static_env) if static_env else None
    spotify_user = build_spotify_user(config, store)
    sso = build_sso(config, conn)
    return create_app(
        store,
        api_key=os.environ.get("CRATE_OPS_API_KEY") or None,
        static_dir=static_dir,
        config=config,
        search=build_spotify_search(),
        spotify=spotify_user,
        library=spotify_user,
        auth=None if sso is not None else build_auth(config, conn),
        sso=sso,
        bulk_resolve=build_bulk_resolver(),
        candidate_sources=build_candidate_sources,
        identify_hint=build_identifier(),
        candidate_streamers=build_candidate_streamers,
        source_validator=build_source_validator(),
        soulseek_browse=build_soulseek_browse(),
        jobs=build_job_store(),
        manual_job_kinds=manual_job_kinds(),
        listening=_build_listening_store(),
        uploads_dir=uploads_dir(),
        sync=_build_sync_service(),
        sync_preview=_build_preview_resolver(),
        dedup=_build_dedup_store(),
        library_root=Path(os.environ.get("CRATE_LIBRARY_ROOT") or "/library"),
        **_build_navidrome(),
        sync_interval_s=float(os.environ.get("CRATE_LIBRARY_SYNC_INTERVAL", "7200")),
    )


def build_soulseek_browse() -> (
    Callable[[str], list[dict[str, Any]]] | None
):  # pragma: no cover - IO wiring
    """The review tab's raw Soulseek browser (Nicotine+-style): term -> every file
    the network returned, serialised with a ready-to-echo download candidate.
    ``None`` when Soulseek isn't enabled / slskd isn't configured, so the panel
    hides instead of erroring."""
    if "soulseek" not in _enabled_source_names():
        return None
    try:
        client = _slskd_client()
    except Exception:  # missing slskd creds -> feature off
        return None
    from crate.adapters.sources import soulseek

    return lambda term: soulseek.browse_rows(term, search=client.search)


# --- background jobs engine (Library Sync + Dedup) ---
def build_job_store() -> Any:  # pragma: no cover - IO wiring
    """A :class:`SqliteJobStore` on its own connection to the shared WAL DB."""
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.job_store import SqliteJobStore

    return SqliteJobStore(connect(db_path()))


def _job_handlers() -> dict[Any, Any]:  # pragma: no cover - IO wiring
    """``JobKind -> handler`` for every kind implemented so far. Features register
    here as they land (listen_poll, library_sync, dedup_scan, …). A kind with no
    handler is never scheduled and can't be triggered by hand."""
    from crate.domain.jobs import JobKind

    return {
        JobKind.LISTEN_POLL: _listen_poll_job,
        JobKind.HISTORY_IMPORT: _history_import_job,
        JobKind.LIBRARY_SYNC: _library_sync_job,
        JobKind.DEDUP_SCAN: _dedup_scan_job,
        JobKind.DEDUP_PURGE: _dedup_purge_job,
        JobKind.TRASH_RESTORE: _trash_restore_job,
        JobKind.TRASH_EMPTY: _trash_empty_job,
        JobKind.NAVIDROME_PUSH: _navidrome_push_job,
    }


def uploads_dir() -> Path:  # pragma: no cover - env read
    """Where history uploads land. It sits next to the DB, and both containers
    mount /data."""
    return db_path().parent / "uploads"


def _build_listening_store() -> Any:  # pragma: no cover - IO wiring
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.listening_store import SqliteListeningStore

    return SqliteListeningStore(connect(db_path()))


def _build_preview_resolver() -> Callable[[str], str | None] | None:  # pragma: no cover - IO
    """Spotify id -> a fresh Deezer preview URL, for the Sync page's play buttons.
    ``None`` without Spotify client creds (the id -> ISRC lookup needs them)."""
    client_id = os.environ.get("CRATE_SPOTIFY_CLIENT_ID")
    client_secret = os.environ.get("CRATE_SPOTIFY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    import httpx

    from crate.application.previews import resolve_preview
    from crate.infrastructure.deezer.public import DeezerPublic
    from crate.infrastructure.spotify.client import SpotifyClient
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.track_meta_store import SqliteTrackMetaStore

    http = httpx.Client(timeout=8.0, follow_redirects=True)

    def post_form(url: str, data: dict[str, str], headers: dict[str, str]) -> Any:
        resp = http.post(url, data=data, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        resp = http.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    spotify = SpotifyClient(client_id, client_secret, post_form=post_form, get_json=get_json)
    deezer = DeezerPublic(lambda url: http.get(url).json())
    cache = SqliteTrackMetaStore(connect(db_path()))

    def resolve(spotify_id: str) -> str | None:
        return resolve_preview(
            spotify_id, cache=cache, spotify_tracks=spotify.tracks_batch, deezer=deezer
        )

    return resolve


_SYNC_INDEX_TTL_S = 30.0  # the Sync page's library-index cache


def _build_sync_service() -> Any:  # pragma: no cover - IO wiring
    """The Sync page's reads + toggles (console). Its stores share one connection
    and lock; the library index (a scan of completed requests) is cached for 30 s."""
    from crate.application.library_sync import LibraryIndex, LibrarySyncService
    from crate.infrastructure.sqlite.affinity_store import SqliteAffinityStore
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.store import RequestStore
    from crate.infrastructure.sqlite.sync_store import (
        SqlitePlaylistSyncStore,
        SqliteSyncQueue,
    )

    conn = connect(db_path())
    lock = threading.Lock()
    requests = RequestStore(connect(db_path()), requested_via="console")
    cache: list[tuple[float, LibraryIndex]] = []

    def index() -> LibraryIndex:
        now = time.monotonic()
        if not cache or now - cache[0][0] > _SYNC_INDEX_TTL_S:
            isrcs, keys = requests.library_index()
            cache[:] = [(now, LibraryIndex(frozenset(isrcs), frozenset(keys)))]
        return cache[0][1]

    return LibrarySyncService(
        SqlitePlaylistSyncStore(conn, lock=lock),
        SqliteAffinityStore(conn, lock=lock),
        SqliteSyncQueue(conn, requests, lock=lock),
        index,
    )


def _history_import_job(job: Any, ctx: Any) -> dict[str, Any]:  # pragma: no cover - IO wiring
    """``history_import``: parse the uploaded export into the uploader's ledger
    (orchestration in ``application.history_import``). The upload is deleted
    only after a successful import, so a failed run can be retried."""
    from crate.application.auth import SERVICE_USER_ID
    from crate.application.history_import import run_history_import
    from crate.infrastructure.history.export_reader import open_export
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.listening_store import SqliteListeningStore

    name = Path(str(job.params.get("upload") or "")).name  # basename only, defensively
    path = uploads_dir() / name
    if not name or not path.is_file():
        raise RuntimeError("upload file is missing; upload the export again")
    if not job.user_id or job.user_id == SERVICE_USER_ID:
        raise RuntimeError("history import needs a logged-in user")
    conn = connect(db_path())
    try:
        summary = run_history_import(
            ctx, job.user_id, open_export(path), SqliteListeningStore(conn)
        )
    finally:
        conn.close()
    path.unlink(missing_ok=True)
    return summary


def _listen_poll_job(job: Any, ctx: Any) -> dict[str, Any]:  # pragma: no cover - IO wiring
    """``listen_poll``: append every linked user's recently-played plays to the
    ledger (orchestration in ``application.listening.run_listen_poll``). Builds
    its collaborators per run on one connection it closes afterwards."""
    from crate.application.listening import run_listen_poll
    from crate.infrastructure.crypto import SecretBox
    from crate.infrastructure.sqlite.config_store import ConfigStore
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.listening_store import SqliteListeningStore
    from crate.infrastructure.sqlite.store import RequestStore
    from crate.infrastructure.sqlite.user_store import SqliteUserRepository

    if not os.environ.get("CRATE_DB"):
        return {"skipped": "no CRATE_DB"}
    conn = connect(db_path())
    try:
        config = ConfigStore(conn, SecretBox.from_keyfile(_keyfile()))
        reader = build_spotify_user(config, RequestStore(conn))
        if reader is None:
            ctx.warn("Spotify client credentials not configured; skipping", code="no_spotify")
            return {"skipped": "spotify not configured"}
        users = SqliteUserRepository(conn).list_users()
        return run_listen_poll(ctx, users, reader, SqliteListeningStore(conn))
    finally:
        conn.close()


def _library_sync_job(job: Any, ctx: Any) -> dict[str, Any]:  # pragma: no cover - IO wiring
    """``library_sync`` (every 2 h + Run now): playlist sync-state + affinity tiers
    for every linked user (orchestration in ``application.library_sync``). It
    queues what auto-sync wants, at most ``CRATE_SYNC_MAX_ENQUEUE`` (default 300)
    per run; 0 makes it a dry run."""
    from datetime import UTC, datetime

    from crate.application.library_sync import LibraryIndex, SyncDeps, run_library_sync
    from crate.infrastructure.crypto import SecretBox
    from crate.infrastructure.sqlite.affinity_store import SqliteAffinityStore
    from crate.infrastructure.sqlite.config_store import ConfigStore
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.listening_store import SqliteListeningStore
    from crate.infrastructure.sqlite.store import RequestStore
    from crate.infrastructure.sqlite.sync_store import (
        SqlitePlaylistSyncStore,
        SqliteSyncQueue,
    )
    from crate.infrastructure.sqlite.user_store import SqliteUserRepository

    if not os.environ.get("CRATE_DB"):
        return {"skipped": "no CRATE_DB"}
    conn = connect(db_path())
    try:
        config = ConfigStore(conn, SecretBox.from_keyfile(_keyfile()))
        requests = RequestStore(conn, requested_via="sync")
        reader = build_spotify_user(config, requests)
        if reader is None:
            ctx.warn("Spotify client credentials not configured; skipping", code="no_spotify")
            return {"skipped": "spotify not configured"}
        isrcs, keys = requests.library_index()
        deps = SyncDeps(
            reader=reader,
            history=SqliteListeningStore(conn),
            tiers=SqliteAffinityStore(conn),
            playlists=SqlitePlaylistSyncStore(conn),
            index=LibraryIndex(frozenset(isrcs), frozenset(keys)),
            queue=SqliteSyncQueue(conn, requests),
        )
        return run_library_sync(
            ctx,
            SqliteUserRepository(conn).list_users(),
            deps,
            now=datetime.now(UTC),
            max_enqueue=int(os.environ.get("CRATE_SYNC_MAX_ENQUEUE", "300")),
        )
    finally:
        conn.close()


def _nice_runner(cmd: list[str]) -> Any:  # pragma: no cover - subprocess
    """Run an analysis tool at low CPU priority, so a full-library scan never starves
    serve's downloads or Navidrome, and with a bounded deadline."""
    import subprocess

    return subprocess.run(
        ["nice", "-n", "10", *cmd], capture_output=True, text=True, check=True, timeout=60
    )


def _analyze_file(path: Path) -> Any:  # pragma: no cover - IO wiring
    """probe + tags + embedded cover + one fpcalc run (decoded locally to raw)."""
    from crate.application.library_scan import FileAnalysis
    from crate.domain.audio import classify
    from crate.domain.chromaprint import decompress
    from crate.infrastructure.audio.fingerprint import fingerprint_compressed
    from crate.infrastructure.audio.probe import probe
    from crate.infrastructure.audio.tags import has_cover, read_tags

    info = probe(path, runner=_nice_runner)
    fp_duration, fp = fingerprint_compressed(path, runner=_nice_runner)
    return FileAnalysis(
        codec=info.codec,
        container=info.container,
        lossless=classify(info).lossless,
        bitrate=info.bitrate_bps,
        bit_depth=info.bit_depth,
        sample_rate=info.sample_rate_hz,
        channels=info.channels,
        duration_s=info.duration_s,
        tags=read_tags(path),
        has_cover=has_cover(path),
        fp_duration=fp_duration,
        fp_compressed=fp,
        fp_raw=decompress(fp).raw,
    )


def _sha256_file(path: Path) -> str:  # pragma: no cover - file IO
    """Content hash for exact-duplicate checks (only files whose sizes collide)."""
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _build_dedup_store() -> Any:  # pragma: no cover - IO wiring
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.dedup_store import SqliteDedupStore

    return SqliteDedupStore(connect(db_path()))


def _dedup_scan_job(job: Any, ctx: Any) -> dict[str, Any]:  # pragma: no cover - IO wiring
    """``dedup_scan`` (nightly + Run now + ``crate dedup scan``): index the library
    (orchestration in ``application.library_scan``). Needs CRATE_LIBRARY_ROOT and
    fpcalc; AcoustID lookups also need CRATE_ACOUSTID_KEY."""
    from crate.application.library_dedup import run_grouping
    from crate.application.library_scan import RateLimiter, run_dedup_scan
    from crate.infrastructure.audio.fingerprint import fpcalc_available
    from crate.infrastructure.fs.library_walk import walk_library
    from crate.infrastructure.sqlite.db import connect, now_iso
    from crate.infrastructure.sqlite.dedup_store import SqliteDedupStore
    from crate.infrastructure.sqlite.library_store import SqliteLibraryStore

    root = os.environ.get("CRATE_LIBRARY_ROOT")
    if not root:
        ctx.warn("CRATE_LIBRARY_ROOT is not set; nothing to scan", code="no_root")
        return {"skipped": "no library root"}
    if not fpcalc_available():
        ctx.warn("fpcalc is not installed; can't fingerprint", code="no_fpcalc")
        return {"skipped": "no fpcalc"}
    lookup = None
    key = os.environ.get("CRATE_ACOUSTID_KEY")
    if key:
        from crate.infrastructure.acoustid.client import AcoustidClient

        lookup = AcoustidClient(key).lookup
    rate = float(os.environ.get("CRATE_ACOUSTID_RATE", "2.5"))
    conn = connect(db_path())
    try:
        summary = run_dedup_scan(
            ctx,
            Path(root),
            walk=walk_library,
            store=SqliteLibraryStore(conn),
            analyze=_analyze_file,
            lookup=lookup,
            limiter=RateLimiter(rate),
        )
        summary["grouping"] = run_grouping(
            ctx,
            SqliteDedupStore(conn),
            root=Path(root),
            hash_file=_sha256_file,
            now=now_iso(),
        )
        return summary
    finally:
        conn.close()


def _navidrome_post(url: str, params: Any) -> Any:  # pragma: no cover - IO wiring
    """POST a Subsonic call as a form (Navidrome accepts POST; long songId lists
    would overflow a GET URL)."""
    import httpx

    resp = httpx.post(url, data=params, timeout=20.0)
    resp.raise_for_status()
    return resp.json()


def _navidrome_get_text(url: str, headers: dict[str, str]) -> str:  # pragma: no cover - IO
    import httpx

    resp = httpx.get(url, headers=headers, timeout=15.0)
    resp.raise_for_status()
    return resp.text


class _NavidromeSso:  # pragma: no cover - IO wiring (the pieces are tested in the client)
    """Navidrome behind SSO: a crate user's SSO username, and a trusted-header
    sign-in for it, checked with a Subsonic ping before it's kept."""

    def __init__(self, url: str, header: str, users: Any) -> None:
        self._url, self._header, self._users = url, header, users

    def username_of(self, user_id: str) -> str | None:
        user = self._users.get(user_id)
        return user.sso_username if user is not None and user.oidc_sub else None

    def login(self, username: str) -> Any:
        from crate.infrastructure.navidrome.client import SubsonicClient, sso_login

        login = sso_login(self._url, self._header, username, get=_navidrome_get_text)
        SubsonicClient.for_login(self._url, login, post=_navidrome_post).ping()
        return login


def _build_navidrome() -> dict[str, Any]:  # pragma: no cover - IO wiring
    """Console side of per-user Navidrome: the account store and a credential check.
    Empty (the feature is off) unless CRATE_NAVIDROME_URL is set. The browser
    link uses CRATE_NAVIDROME_PUBLIC_URL, since the API URL is usually internal."""
    url = os.environ.get("CRATE_NAVIDROME_URL")
    if not url:
        return {}
    from crate.infrastructure.crypto import SecretBox
    from crate.infrastructure.navidrome.client import SubsonicClient
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.navidrome_store import SqliteNavidromeStore

    def verify(user: str, password: str) -> None:
        SubsonicClient(url, user, password, post=_navidrome_post).ping()

    # Navidrome behind SSO: only when crate itself signs people in with SSO, so the
    # username it hands Navidrome was asserted by the identity provider.
    header = os.environ.get("CRATE_NAVIDROME_SSO_HEADER", "").strip()
    sso = None
    if header and os.environ.get("CRATE_OIDC_ISSUER", "").strip():
        from crate.infrastructure.sqlite.user_store import SqliteUserRepository

        sso = _NavidromeSso(url, header, SqliteUserRepository(connect(db_path())))
    return {
        "navidrome_sso": sso,
        "navidrome": SqliteNavidromeStore(connect(db_path()), SecretBox.from_keyfile(_keyfile())),
        "navidrome_verify": verify,
        "navidrome_public_url": os.environ.get("CRATE_NAVIDROME_PUBLIC_URL") or None,
    }


def _navidrome_push_job(job: Any, ctx: Any) -> dict[str, Any]:  # pragma: no cover - IO wiring
    """``navidrome_push`` (on import, on link, and every 30 min): bring every
    mirrored playlist up to date in its owner's Navidrome account (orchestration
    in ``application.navidrome_mirror``). A job from a user pushes only theirs."""
    from datetime import UTC, datetime

    from crate.application.auth import SERVICE_USER_ID
    from crate.application.library_sync import LIKED_ID, LibraryIndex
    from crate.application.navidrome_mirror import run_navidrome_push
    from crate.infrastructure.crypto import SecretBox
    from crate.infrastructure.navidrome.client import SubsonicClient
    from crate.infrastructure.sqlite.config_store import ConfigStore
    from crate.infrastructure.sqlite.db import connect
    from crate.infrastructure.sqlite.navidrome_store import SqliteNavidromeStore
    from crate.infrastructure.sqlite.store import RequestStore
    from crate.infrastructure.sqlite.sync_store import SqlitePlaylistSyncStore

    url = os.environ.get("CRATE_NAVIDROME_URL")
    if not os.environ.get("CRATE_DB") or not url:
        return {"skipped": "CRATE_NAVIDROME_URL not set"}
    conn = connect(db_path())
    try:
        box = SecretBox.from_keyfile(_keyfile())
        requests = RequestStore(conn)
        reader = build_spotify_user(ConfigStore(conn, box), requests)
        items = SqlitePlaylistSyncStore(conn)

        def tracks_for(user_id: str, spotify_id: str) -> list[dict[str, Any]]:
            stored = items.items(user_id, spotify_id)  # kept fresh by library_sync
            if stored or reader is None:
                return stored
            if spotify_id == LIKED_ID:
                return reader.liked_songs(user_id)
            return reader.playlist_tracks(user_id, spotify_id)

        isrcs, keys = requests.library_index()
        uid = job.user_id if job.user_id and job.user_id != SERVICE_USER_ID else None
        return run_navidrome_push(
            ctx,
            SqliteNavidromeStore(conn, box),
            client_for=lambda login: SubsonicClient.for_login(url, login, post=_navidrome_post),
            tracks_for=tracks_for,
            index=LibraryIndex(frozenset(isrcs), frozenset(keys)),
            now=datetime.now(UTC),
            user_id=uid,
            playlist_id=job.params.get("playlist_id") or None,
        )
    finally:
        conn.close()


def _navidrome_rescan() -> Callable[[], None] | None:  # pragma: no cover - IO wiring
    """Ask Navidrome to rescan after a purge or restore; ``None`` unless
    CRATE_NAVIDROME_URL / _USER / _PASS are all set."""
    url = os.environ.get("CRATE_NAVIDROME_URL")
    user = os.environ.get("CRATE_NAVIDROME_USER")
    password = os.environ.get("CRATE_NAVIDROME_PASS")
    if not (url and user and password):
        return None
    import httpx

    from crate.infrastructure.navidrome.client import start_scan

    def get(target: str, params: dict[str, str]) -> Any:
        resp = httpx.get(target, params=params, timeout=15.0)
        resp.raise_for_status()
        return resp.json()

    def rescan() -> None:
        if not start_scan(url, user, password, get=get):
            raise RuntimeError("Navidrome refused the scan request")

    return rescan


def _trash_parts(conn: Any, ctx: Any) -> tuple[Any, Any] | None:  # pragma: no cover - IO
    from crate.infrastructure.fs.library_fs import LibraryFs
    from crate.infrastructure.sqlite.trash_store import SqliteTrashStore

    root = os.environ.get("CRATE_LIBRARY_ROOT")
    if not root:
        ctx.warn("CRATE_LIBRARY_ROOT is not set; nothing to do", code="no_root")
        return None
    return SqliteTrashStore(conn, Path(root)), LibraryFs(Path(root))


def _dedup_purge_job(job: Any, ctx: Any) -> dict[str, Any]:  # pragma: no cover - IO wiring
    """``dedup_purge``: move the chosen duplicate copies into the library trash."""
    from datetime import UTC, datetime

    from crate.application.purge import run_purge
    from crate.domain.purge import PurgeItem
    from crate.infrastructure.sqlite.db import connect

    conn = connect(db_path())
    try:
        parts = _trash_parts(conn, ctx)
        if parts is None:
            return {"skipped": "no library root"}
        store, fs = parts
        items = [PurgeItem.from_dict(i) for i in job.params.get("items") or []]
        return run_purge(
            ctx,
            items,
            store=store,
            fs=fs,
            day=datetime.now(UTC).date().isoformat(),
            user_id=job.user_id,
            rescan=_navidrome_rescan(),
        )
    finally:
        conn.close()


def _trash_restore_job(job: Any, ctx: Any) -> dict[str, Any]:  # pragma: no cover - IO wiring
    from crate.application.purge import run_restore
    from crate.infrastructure.sqlite.db import connect

    conn = connect(db_path())
    try:
        parts = _trash_parts(conn, ctx)
        if parts is None:
            return {"skipped": "no library root"}
        store, fs = parts
        ids = [str(i) for i in job.params.get("ids") or []]
        return run_restore(ctx, ids, store=store, fs=fs, rescan=_navidrome_rescan())
    finally:
        conn.close()


def _trash_empty_job(job: Any, ctx: Any) -> dict[str, Any]:  # pragma: no cover - IO wiring
    """``trash_empty``: daily, delete trash older than CRATE_TRASH_RETENTION_DAYS
    (default 30); on request (admin, typed confirm), everything."""
    from datetime import UTC, datetime

    from crate.application.purge import run_empty
    from crate.infrastructure.sqlite.db import connect

    conn = connect(db_path())
    try:
        parts = _trash_parts(conn, ctx)
        if parts is None:
            return {"skipped": "no library root"}
        store, fs = parts
        return run_empty(
            ctx,
            store=store,
            fs=fs,
            everything=bool(job.params.get("all")),
            retention_days=int(os.environ.get("CRATE_TRASH_RETENTION_DAYS", "30")),
            now=datetime.now(UTC),
        )
    finally:
        conn.close()


def run_job_now(kind: Any, *, triggered_by: str = "cli") -> dict[str, Any]:  # pragma: no cover
    """Queue ``kind`` and run it in this process (the CLI's "run it now"). If one is
    already queued or running, report it instead of starting a second."""
    from crate.application.jobs import run_claimed
    from crate.domain.jobs import LANE_OF

    store = build_job_store()
    job_id, existing = store.enqueue(kind, triggered_by=triggered_by)
    if existing:
        return {"job": job_id, "existing": True}
    job = store.claim_next(LANE_OF[kind].value)
    if job is None:  # serve's runner claimed it first; it runs there
        return {"job": job_id, "claimed_by": "serve"}
    run_claimed(job, _job_handlers(), store)
    return dict(store.get(job.id) or {})


# Kinds a user may start with "Run now" (history import has its own upload flow;
# destructive dedup kinds go through their admin-gated endpoints).
_MANUAL_JOB_KINDS = ("library_sync", "dedup_scan")


def manual_job_kinds() -> frozenset[Any]:  # pragma: no cover - IO wiring
    from crate.domain.jobs import JobKind

    handlers = _job_handlers()
    return frozenset(JobKind(k) for k in _MANUAL_JOB_KINDS if JobKind(k) in handlers)


def _job_schedules(handlers: dict[Any, Any]) -> list[Any]:  # pragma: no cover - env read
    from crate.application.jobs import Schedule
    from crate.domain.jobs import JobKind

    intervals = {
        JobKind.LIBRARY_SYNC: float(os.environ.get("CRATE_LIBRARY_SYNC_INTERVAL", "7200")),
        JobKind.LISTEN_POLL: float(os.environ.get("CRATE_LISTEN_POLL_INTERVAL", "1800")),
        JobKind.DEDUP_SCAN: float(os.environ.get("CRATE_DEDUP_SCAN_INTERVAL", "86400")),
        JobKind.TRASH_EMPTY: float(os.environ.get("CRATE_TRASH_EMPTY_INTERVAL", "86400")),
    }
    if os.environ.get("CRATE_NAVIDROME_URL"):  # nothing to push to otherwise
        intervals[JobKind.NAVIDROME_PUSH] = float(
            os.environ.get("CRATE_NAVIDROME_PUSH_INTERVAL", "1800")
        )
    return [Schedule(k, v) for k, v in intervals.items() if k in handlers and v > 0]


def job_lanes() -> list[Any]:  # pragma: no cover - IO wiring
    """Lanes that have at least one registered handler (no idle runner threads)."""
    from crate.domain.jobs import LANE_OF

    lanes = {LANE_OF[k] for k in _job_handlers()}
    return sorted(lanes, key=lambda lane: lane.value)


def build_job_runner(lane: Any) -> Callable[[threading.Event], None]:  # pragma: no cover
    """One lane's runner loop: claim the oldest queued job in the lane and run it
    to completion. Never dies on a job or store error — it waits and retries."""
    from crate.application.jobs import run_claimed

    store = build_job_store()
    handlers = _job_handlers()
    poll = float(os.environ.get("CRATE_JOB_POLL", "2.0"))

    def loop(stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                job = store.claim_next(lane.value)
            except Exception:  # DB busy / transient — back off and retry
                stop.wait(poll)
                continue
            if job is None:
                stop.wait(poll)
                continue
            try:
                run_claimed(job, handlers, store)
            except Exception:  # a store error while finishing — keep the lane alive
                continue

    return loop


def build_job_scheduler() -> Callable[[threading.Event], None] | None:  # pragma: no cover
    """Every ``CRATE_SCHEDULER_TICK`` s (default 60), enqueue each scheduled kind
    that's due. Last-run times come from the DB, so restarts don't lose the
    schedule or burst. ``None`` when nothing is schedulable yet."""
    from datetime import UTC, datetime

    from crate.application.jobs import due_kinds

    handlers = _job_handlers()
    schedules = _job_schedules(handlers)
    if not schedules:
        return None
    store = build_job_store()
    tick = float(os.environ.get("CRATE_SCHEDULER_TICK", "60"))

    def loop(stop: threading.Event) -> None:
        while True:
            try:
                for kind in due_kinds(schedules, store.last_created, datetime.now(UTC)):
                    store.enqueue(kind, triggered_by="schedule")
            except Exception:  # never let one bad tick kill the scheduler
                pass
            if stop.wait(tick):
                return

    return loop


def reap_jobs() -> tuple[int, int]:  # pragma: no cover - IO wiring
    """Startup recovery for jobs a dead ``serve`` left running (see reap_running)."""
    failed, requeued = build_job_store().reap_running()
    return int(failed), int(requeued)


def _prune_jobs(store: Any) -> int:  # pragma: no cover - IO wiring
    """Retention: finished jobs + their logs after 30 days (listen_poll: 7)."""
    from crate.domain.jobs import JobKind

    days = int(os.environ.get("CRATE_JOB_RETENTION_DAYS", "30"))
    return int(store.prune(days, overrides={JobKind.LISTEN_POLL: min(days, 7)}))


def prune_jobs() -> int:  # pragma: no cover - IO wiring
    return _prune_jobs(build_job_store())
