"""lbmd command-line entry point.

Bootstrap wiring only. Real commands (`download`, `serve` for the Explo slskd
shim) are added by their owning units. Kept thin: the CLI parses args and hands
off to application use-cases — no business logic here.
"""

from __future__ import annotations

import typer

from lbmd import __version__

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


if __name__ == "__main__":  # pragma: no cover
    app()
