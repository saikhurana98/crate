"""CLI smoke tests. The real `download` / `serve` commands land in later units;
this pins the app wiring and the `version` command."""

from typer.testing import CliRunner

from lbmd import __version__
from lbmd.cli.main import app

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_the_app() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "lbmd" in result.stdout.lower()
