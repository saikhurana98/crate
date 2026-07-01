"""lbmd command-line entry point.

Bootstrap wiring only. Real commands (`download`, `serve` for the Explo slskd
shim) are added by their owning units. Kept thin: the CLI parses args and hands
off to application use-cases — no business logic here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from lbmd import __version__
from lbmd.application.cascade import CascadeResult, run_cascade
from lbmd.composition.sources import build_default_sources
from lbmd.infrastructure.audio.probe import probe

app = typer.Typer(
    name="lbmd",
    help="lbmd — load-balanced music downloader (tiered FLAC acquisition, Explo-compatible).",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """lbmd root. Present so commands stay subcommands (Typer collapses a
    single-command app into a bare root, which we don't want)."""


@app.command()
def version() -> None:
    """Print the lbmd version."""
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
    sources = build_default_sources()
    ok = 0
    for i, q in enumerate(queries, 1):
        res = run_cascade(q, out, sources, prober=probe)
        typer.echo(_fmt_result(i, res))
        ok += 1 if res.winner is not None else 0
    typer.echo(f"\n{ok}/{len(queries)} acquired into {out}/")


if __name__ == "__main__":  # pragma: no cover
    app()
