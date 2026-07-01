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
from lbmd.application.download import DownloadReport, acquire_many

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


def _fmt_report(idx: int, r: DownloadReport) -> str:
    if not r.ok or r.outcome is None or r.info is None or r.verdict is None:
        return f"{idx:>2}. FAIL  {r.query}\n      error: {r.error}"
    mb = r.info.size_bytes / 1_048_576
    lossless = "LOSSLESS" if r.verdict.lossless else "lossy"
    return (
        f"{idx:>2}. OK    {r.outcome.path.name}\n"
        f"      tier {r.outcome.tier} ({r.outcome.source}) | "
        f"{r.outcome.elapsed_s:.1f}s | {mb:.1f} MB | {r.info.duration_s:.0f}s | "
        f"{lossless}: {r.verdict.label}"
    )


@app.command()
def download(
    queries: Annotated[list[str], typer.Argument(help="Track queries (free text or URL).")],
    out: Annotated[Path, typer.Option("--out", "-o", help="Output directory.")] = Path("downloads"),
) -> None:
    """Download one or more tracks (Tier-3 yt-dlp today) and report quality."""
    out.mkdir(parents=True, exist_ok=True)
    reports = acquire_many(queries, out)
    for i, r in enumerate(reports, 1):
        typer.echo(_fmt_report(i, r))
    ok = sum(1 for r in reports if r.ok)
    typer.echo(f"\n{ok}/{len(reports)} downloaded into {out}/")


if __name__ == "__main__":  # pragma: no cover
    app()
