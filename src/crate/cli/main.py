"""crate command-line entry point.

Bootstrap wiring only. Real commands (`download`, `serve` for the Explo slskd
shim) are added by their owning units. Kept thin: the CLI parses args and hands
off to application use-cases — no business logic here.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from crate import __version__
from crate.application.cascade import CascadeResult, run_cascade
from crate.composition.sources import build_recorder, build_tiers
from crate.infrastructure.audio.probe import probe

app = typer.Typer(
    name="crate",
    help="crate — load-balanced music downloader (tiered FLAC acquisition, Explo-compatible).",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """crate root. Present so commands stay subcommands (Typer collapses a
    single-command app into a bare root, which we don't want)."""


@app.command()
def version() -> None:
    """Print the crate version."""
    typer.echo(__version__)


def _fmt_result(idx: int, res: CascadeResult) -> str:
    trail = (
        " -> ".join(f"T{a.tier}:{a.source}{'✓' if a.ok else '✗'}" for a in res.attempts)
        or "no sources"
    )
    w = res.winner
    if w is None or w.outcome is None or w.info is None or w.verdict is None:
        detail = res.attempts[-1].detail if res.attempts else "no sources registered"
        return f"{idx:>2}. FAIL  {res.query}\n      cascade: {trail}\n      last: {detail}"
    mb = w.info.size_bytes / 1_048_576
    tag = "LOSSLESS" if w.verdict.lossless else "lossy"
    return (
        f"{idx:>2}. OK    {w.outcome.path.name}\n"
        f"      cascade: {trail}\n"
        f"      won tier {w.outcome.tier} ({w.outcome.source}) | "
        f"{w.outcome.elapsed_s:.1f}s | {mb:.1f} MB | {w.info.duration_s:.0f}s | "
        f"{tag}: {w.verdict.label}"
    )


@app.command()
def download(
    queries: Annotated[list[str], typer.Argument(help="Track queries (free text or URL).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory.")] = Path("downloads"),
) -> None:
    """Download tracks via the tier cascade (Tier 1 Monochrome -> Tier 3 yt-dlp;
    Tier 2 skipped) and report which tier won plus the quality."""
    out.mkdir(parents=True, exist_ok=True)
    tiers = build_tiers()
    # record to the console DB only when one is configured (keeps ad-hoc local
    # downloads side-effect-free; the server sets CRATE_DB).
    recorder = build_recorder("cli") if os.environ.get("CRATE_DB") else None
    ok = 0
    for i, q in enumerate(queries, 1):
        res = run_cascade(q, out, tiers, prober=probe, recorder=recorder)
        typer.echo(_fmt_result(i, res))
        ok += 1 if res.winner is not None else 0
    typer.echo(f"\n{ok}/{len(queries)} acquired into {out}/")


@app.command()
def serve(  # pragma: no cover - network server loop
    port: Annotated[int, typer.Option("--port", "-p", help="Listen port.")] = 2416,
    host: Annotated[str, typer.Option(help="Listen address.")] = "0.0.0.0",
) -> None:
    """Run the slskd-compatible shim so Explo (SLSKD_URL) downloads via the tier
    cascade. Config: CRATE_SERVE_DOWNLOADS (output dir), CRATE_SERVE_API_KEY."""
    import os
    import threading

    from crate.adapters.inbound.slskd_shim import ShimState
    from crate.composition.sources import build_queue_worker, build_shim_download
    from crate.infrastructure.http_server.serve import run_server

    downloads = Path(os.environ.get("CRATE_SERVE_DOWNLOADS", "downloads"))
    downloads.mkdir(parents=True, exist_ok=True)
    shim = ShimState(
        build_shim_download(), downloads, api_key=os.environ.get("CRATE_SERVE_API_KEY") or None
    )
    # Drain console-queued downloads in the background (this process owns the NAS
    # mount + toolchain; the console only writes queue rows to the shared DB).
    stop = threading.Event()
    threading.Thread(target=build_queue_worker(), args=(stop,), daemon=True).start()
    typer.echo(f"crate slskd-shim listening on {host}:{port}  downloads -> {downloads}")
    try:
        run_server(shim, host, port)
    finally:
        stop.set()


@app.command()
def console(  # pragma: no cover - network server loop
    port: Annotated[int, typer.Option("--port", "-p", help="Listen port.")] = 8788,
    host: Annotated[str, typer.Option(help="Listen address.")] = "0.0.0.0",
) -> None:
    """Run the management console (read-only stats/history dashboard + API) over
    the history DB. Config: CRATE_DB (shared with `serve`), CRATE_OPS_API_KEY,
    CRATE_CONSOLE_STATIC (built UI dir)."""
    import uvicorn

    from crate.composition.sources import build_console_app

    typer.echo(f"crate console listening on {host}:{port}")
    uvicorn.run(build_console_app(), host=host, port=port, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    app()
