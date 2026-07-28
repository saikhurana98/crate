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
from crate.composition.sources import build_forced_tiers, build_recorder, build_tiers
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
    source: Annotated[
        str | None,
        typer.Option(
            "--source",
            "-s",
            help="Force one source (e.g. soulseek) instead of the cascade, to test that source.",
        ),
    ] = None,
) -> None:
    """Download tracks via the tier cascade and report which tier won plus the quality.

    With ``--source`` the cascade is replaced by that single source, so a failure
    is attributable to it instead of being masked by a lower tier taking over.
    The forced source runs even when it's disabled in the stored layout."""
    out.mkdir(parents=True, exist_ok=True)
    if source is not None:
        try:
            tiers = build_forced_tiers(source)
        except KeyError:
            raise typer.BadParameter(f"unknown source: {source}") from None
        except Exception as exc:  # missing creds -> that IS the diagnosis
            typer.echo(f"source {source!r} could not be built: {exc}")
            raise typer.Exit(code=1) from None
    else:
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
    from crate.composition.sources import (
        build_maintenance,
        build_queue_worker,
        build_shim_download,
        prune_attempts,
        reap_orphans,
    )
    from crate.infrastructure.http_server.serve import run_server

    downloads = Path(os.environ.get("CRATE_SERVE_DOWNLOADS", "downloads"))
    downloads.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    # Crash recovery: requeue anything a previous serve left mid-flight before the
    # workers start, else those rows hang 'downloading' forever (claim_next only
    # picks 'queued'). Only meaningful with a console DB (CRATE_DB).
    if os.environ.get("CRATE_DB"):
        reaped = reap_orphans()
        if reaped:
            typer.echo(f"reaped {reaped} orphaned download(s) from a previous run -> requeued")
        # Retention: prune stale attempt rows once now, then daily in the background
        # (the attempts table is append-only and would otherwise grow unbounded).
        pruned = prune_attempts()
        if pruned:
            typer.echo(f"pruned {pruned} attempt row(s) older than the retention window")
        threading.Thread(target=build_maintenance(), args=(stop,), daemon=True).start()
    shim = ShimState(
        build_shim_download(), downloads, api_key=os.environ.get("CRATE_SERVE_API_KEY") or None
    )
    # Drain console-queued downloads in the background (this process owns the NAS
    # mount + toolchain; the console only writes queue rows to the shared DB).
    # Multiple worker threads give bulk batches parallelism; claim_next is atomic
    # so they never double-claim a row.
    concurrency = max(1, int(os.environ.get("CRATE_BULK_CONCURRENCY", "4")))
    for _ in range(concurrency):
        threading.Thread(target=build_queue_worker(), args=(stop,), daemon=True).start()
    typer.echo(
        f"crate slskd-shim listening on {host}:{port}  downloads -> {downloads}  "
        f"(queue workers: {concurrency})"
    )
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
