"""AgentFare command line entry point (Typer + Rich)."""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Annotated, NoReturn, Optional

import typer
from typer.core import TyperGroup

from . import __version__
from .render import SETUP_ERRORS, print_error
from .state import AppState

# The options that belong before the command, with what each one is followed by in the example that says so.
GLOBAL_OPTIONS = {"--config": " FILE", "--home": " DIR", "--no-ingest": ""}


class _Group(TyperGroup):
    """The command group, which says where a global option goes when it is typed after the command."""

    def invoke(self, ctx: typer.Context) -> object:
        try:
            return super().invoke(ctx)
        except Exception as exc:  # typer's own click (typer._click) or a real one: the class is told apart by its name
            option = getattr(exc, "option_name", None)
            sub = getattr(getattr(exc, "ctx", None), "info_name", None)
            if type(exc).__name__ == "NoSuchOption" and option in GLOBAL_OPTIONS and sub and getattr(exc, "ctx", None) is not ctx:
                exc.message = (f"No such option: {option} (global options go before the command: "  # type: ignore[attr-defined]
                               f"agentfare {option}{GLOBAL_OPTIONS[option]} {sub})")
                exc.possibilities = None  # type: ignore[attr-defined]
            raise


app = typer.Typer(
    name="agentfare",
    cls=_Group,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Cost router for AI coding agents: price your Claude Code, Codex CLI and Gemini CLI sessions, "
    "and see where a cheaper model would have done the job. Local-first: no accounts, no cloud, no prompt storage.",
    no_args_is_help=True,
    add_completion=True,  # `agentfare --show-completion` prints the script; nothing is installed unless you run --install-completion
    pretty_exceptions_enable=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"agentfare {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[Optional[Path], typer.Option("--config", envvar="AGENTFARE_CONFIG", help="Config file (default: ~/.agentfare/config.yaml).")] = None,
    home: Annotated[Optional[Path], typer.Option("--home", help="Data directory (default: $AGENTFARE_HOME or ~/.agentfare).")] = None,
    no_ingest: Annotated[bool, typer.Option("--no-ingest", help="Do not scan agent logs first; use the database as it is.")] = False,
    version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version and exit.")] = False,
) -> None:
    ctx.obj = state = AppState(config_path=config, home=home, auto_ingest=not no_ingest)
    ctx.call_on_close(state.close)


def get_state(ctx: typer.Context) -> AppState:
    return ctx.obj


# Every command lives in its own module and registers itself here.
from .commands import advise, config as config_cmd, demo, doctor, ingest, init, models, prices, report, run, sessions, summary  # noqa: E402

for _mod in (init, ingest, config_cmd, summary, sessions, models, advise, run, report, prices, doctor, demo):
    _mod.register(app)


def _leave_quietly() -> NoReturn:
    """The reader of our stdout is gone (``agentfare advise --all | head -1``), which is no error, as with any Unix tool.

    What is left of stdout is pointed at /dev/null, so that the flush Python does when it exits cannot fail as well.
    """
    with contextlib.suppress(OSError, ValueError):  # a stdout without a file descriptor has nothing to redirect
        target = sys.stdout.fileno()
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, target)
        finally:
            os.close(devnull)
    raise SystemExit(0)


def _entry() -> None:
    """Console-script wrapper: setup problems (config, prices, database) are one-line errors, not tracebacks, and a closed
    output pipe ends the command quietly."""
    try:
        app()
    except SETUP_ERRORS as exc:
        print_error(exc)
        raise SystemExit(2)
    except BrokenPipeError:
        _leave_quietly()
    except SystemExit as exc:
        if isinstance(exc.__context__, BrokenPipeError):  # click catches EPIPE itself and exits 1: the pipe is its context
            _leave_quietly()
        raise


if __name__ == "__main__":  # pragma: no cover
    _entry()
