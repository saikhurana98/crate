"""Composition root — wires swappable source modules into per-tier pools.

Each tier is a :class:`TierMux` (a pool of sources + a fan-out strategy), so a
tier can load-balance across several sources. Strategy is configurable per tier
via ``CRATE_MUX_TIER{1,2,3}`` = ``all`` (default, try every source) or
``uniform``/``distribute`` (round-robin one source per request).

  Tier 1  Qobuz (hi-res FLAC, direct)        opt-in CRATE_QOBUZ=1
          Monochrome (Amazon 24-bit FLAC)   opt-in CRATE_MONOCHROME=1
  Tier 2  Deezer (16-bit FLAC via ARL)       opt-in CRATE_DEEZER=1
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
from crate.domain.download import DownloadOutcome
from crate.domain.tiers import SourceRef, Tier
from crate.infrastructure import http
from crate.infrastructure.audio.decrypt import mp4decrypt_cenc


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


def _strategy(tier_num: int) -> MuxStrategy:
    value = os.environ.get(f"CRATE_MUX_TIER{tier_num}", "all").lower()
    return MuxStrategy.UNIFORM if value in ("uniform", "distribute") else MuxStrategy.ALL


def _qobuz_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.qobuz import qobuz_download
    from crate.infrastructure.qobuz.client import QobuzClient

    client = QobuzClient(
        os.environ["CRATE_QOBUZ_APP_ID"],
        os.environ["CRATE_QOBUZ_APP_SECRET"],
        os.environ["CRATE_QOBUZ_TOKEN"],
    )

    def qobuz(query: str, out_dir: Path) -> DownloadOutcome:
        return qobuz_download(
            query,
            out_dir,
            search=client.search,
            get_file_url=client.get_file_url,
            get_bytes=client.get_bytes,
        )

    return SourceEntry(SourceRef("qobuz", Tier.ONE), qobuz, expected_lossless=True)


def _monochrome_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.infrastructure.browser.amz_session import AmzSession

    session = AmzSession()

    def search_json(query: str) -> Any:
        return http.get_json(f"{DEFAULT_TIDAL_BASE}/search/", {"s": query})

    def monochrome(query: str, out_dir: Path) -> DownloadOutcome:
        return monochrome_download(
            query,
            out_dir,
            search_json=search_json,
            amz_fetch=session.fetch_amz,
            get_bytes=http.get_bytes,
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

    def telegram(query: str, out_dir: Path) -> DownloadOutcome:
        return telegram_download(query, out_dir, request=client.request, download=client.download)

    # accepts whatever the bot serves (prefers lossless in selection)
    return SourceEntry(SourceRef("telegram", Tier.TWO), telegram, expected_lossless=False)


def _deezer_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.deezer import deezer_download
    from crate.infrastructure.deezer.client import DeezerClient

    client = DeezerClient(os.environ["CRATE_DEEZER_ARL"])

    def deezer(query: str, out_dir: Path) -> DownloadOutcome:
        return deezer_download(
            query,
            out_dir,
            search=client.search,
            resolve_url=client.resolve_url,
            get_bytes=client.get_bytes,
        )

    # Deezer always serves FLAC when available; if the account tier lacks it the
    # client raises and the cascade advances.
    return SourceEntry(SourceRef("deezer", Tier.TWO), deezer, expected_lossless=True)


def _soulseek_source() -> SourceEntry:  # pragma: no cover - IO wiring
    from crate.adapters.sources.soulseek import soulseek_download
    from crate.infrastructure.soulseek.slskd_client import SlskdClient

    client = SlskdClient(
        os.environ["CRATE_SLSKD_URL"],
        os.environ["CRATE_SLSKD_API_KEY"],
        os.environ["CRATE_SLSKD_DOWNLOADS"],
    )
    min_kbps = int(os.environ.get("CRATE_SLSKD_MIN_BITRATE", "0"))

    def soulseek(query: str, out_dir: Path) -> DownloadOutcome:
        return soulseek_download(
            query,
            out_dir,
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
    "soulseek": _soulseek_source,
    "telegram": _telegram_source,
    "ytdlp": _ytdlp_source,
}


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


def build_shim_download() -> Callable[[str, Path], Path]:  # pragma: no cover - IO wiring
    """A ``download(query, out_dir) -> path`` that runs the full cascade — used by
    the slskd shim (`crate serve`) so Explo pulls through the tiered stack. Records
    every request to the console DB."""
    from crate.application.cascade import run_cascade
    from crate.infrastructure.audio.probe import probe

    recorder = build_recorder("explo")
    current_tiers = _tiers_provider(_config_store_if_enabled())

    def download(query: str, out_dir: Path) -> Path:
        result = run_cascade(query, out_dir, current_tiers(), prober=probe, recorder=recorder)
        if result.winner is None or result.winner.outcome is None:
            raise RuntimeError(f"no source produced a file for {query!r}")
        return result.winner.outcome.path

    return download


def build_queue_worker() -> Callable[[threading.Event], None]:  # pragma: no cover - IO wiring
    """A loop that drains console-queued requests through the cascade. Runs in
    the `serve` process (which owns the NAS mount + full source toolchain), so
    console downloads share the same downloader as Explo. Attaches to each
    pre-created ``queued`` row rather than opening a new request."""
    from crate.application.cascade import run_cascade
    from crate.infrastructure.audio.probe import probe

    store = build_recorder("console")
    current_tiers = _tiers_provider(_config_store_if_enabled())
    out_dir = Path(os.environ.get("CRATE_SERVE_DOWNLOADS", "downloads"))
    poll = float(os.environ.get("CRATE_QUEUE_POLL", "2.0"))

    def loop(stop: threading.Event) -> None:
        out_dir.mkdir(parents=True, exist_ok=True)
        while not stop.is_set():
            claimed = store.claim_next()
            if claimed is None:
                stop.wait(poll)
                continue
            try:
                run_cascade(
                    claimed["query"],
                    out_dir,
                    current_tiers(),
                    prober=probe,
                    recorder=store,
                    request_id=claimed["id"],
                )
            except Exception as exc:  # never let one bad request kill the loop
                store.fail_request(claimed["id"], f"queue worker error: {exc}")

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
    )
