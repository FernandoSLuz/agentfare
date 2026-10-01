"""`agentfare demo`: the whole product on invented data, in a few seconds, before you have a history of your own.

The demo copies the synthetic logs packaged with AgentFare (``agentfare/demo_data``: 45 sessions of Claude Code, Codex CLI and
Gemini CLI over 2026-09-21 to 2026-09-30, made by ``scripts/make_demo_data.py``) to a temporary directory, reads them into a
temporary database with the clock frozen at ``DEMO_NOW``, and runs the real ``summary`` and ``advise`` on them. Your own agent
logs and ``~/.agentfare`` are neither read nor written, and nothing leaves the machine.

The commands run in this process, through the same command line you type (``--config``, ``--home`` and ``--no-ingest`` in front
of the command), so what the demo prints is what those commands print. Its ``--json`` is built from their ``--json``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
import typer.main
from rich.console import Console
from rich.text import Text

from ..config import ConfigError
from ..render import JsonOpt, clean_errors, emit_json, plain
from ..safeio import make_private_dirs

DEMO_NOW = "2026-09-30T18:00:00Z"  # the clock of the demo: the last session is still going, 8 minutes before it
PACKAGED = Path(__file__).resolve().parent.parent / "demo_data"
TOP = 3  # pieces of advice shown
ADVICE_KEYS = ("short_id", "agent", "project", "day", "tier", "model", "recommended_model", "actual", "recommended", "saved", "ratio", "line")
TRY_ON_YOUR_OWN = (
    ("agentfare summary", "spend by model, project and day"),
    ("agentfare advise", "where a cheaper model would have done the job"),
    ('agentfare run --dry-run -- claude -p "fix the typo in the README"', "pick a model before a task starts"),
    ("agentfare report --format html -o week.html", "the weekly report, one self-contained page"),
)


# ---- running the real command line in this process -------------------------------------------------------------------
@contextlib.contextmanager
def _frozen_clock() -> Iterator[None]:
    """``AGENTFARE_NOW`` is the demo's clock while the demo runs, and what it was before afterwards."""
    before = os.environ.get("AGENTFARE_NOW")
    os.environ["AGENTFARE_NOW"] = DEMO_NOW
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("AGENTFARE_NOW", None)
        else:
            os.environ["AGENTFARE_NOW"] = before


def _invoke(argv: Sequence[str]) -> int:
    """Run ``agentfare <argv>`` and return its exit code. Its output goes where it would go if you had typed it."""
    from ..cli import app  # cli imports this module when it loads

    try:
        typer.main.get_command(app).main(args=list(argv), prog_name="agentfare")
    except SystemExit as exc:  # click's standalone mode always ends in sys.exit
        if isinstance(exc.__context__, BrokenPipeError):  # click turned a closed output pipe into exit 1: let cli._entry leave quietly
            raise BrokenPipeError from None
        return exc.code if isinstance(exc.code, int) else 0 if exc.code is None else 1
    return 0


def _capture(argv: Sequence[str]) -> tuple[int, str, str]:
    """Like ``_invoke``, but with stdout and stderr taken: (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = _invoke(argv)
    return code, out.getvalue(), err.getvalue()


def _json(argv: Sequence[str]) -> dict[str, Any]:
    code, out, err = _capture(argv)
    if code:
        raise ConfigError(f"the demo could not run `agentfare {shlex.join(argv)}`: {err.strip() or f'exit code {code}'}")
    return json.loads(out)  # type: ignore[no-any-return]


# ---- the places ------------------------------------------------------------------------------------------------------
def _target(keep: Path | None) -> Path | None:
    """The directory ``--keep`` names, made absolute, or None. It must be new or empty: the demo never writes into a directory
    that holds anything, so that nothing of yours can be overwritten."""
    if keep is None:
        return None
    path = Path(os.path.expanduser(keep)).absolute()
    if path.exists() and not path.is_dir():
        raise ConfigError(f"{path} is not a directory (--keep needs a new or empty one)")
    if path.is_dir() and any(path.iterdir()):
        raise ConfigError(f"{path} is not empty (--keep needs a new or empty directory, so that nothing of yours is overwritten)")
    return path


def _copy_data(root: Path) -> Path:
    if not (PACKAGED / "config.yaml").is_file():
        raise ConfigError(f"the demo data is missing from this installation ({PACKAGED}): reinstall agentfare")
    make_private_dirs(root)
    target = root / "demo_data"
    shutil.copytree(PACKAGED, target)
    return target


def _flags(config: Path, home: Path) -> str:
    return f"--config {shlex.quote(str(config))} --home {shlex.quote(str(home))}"


# ---- the output ------------------------------------------------------------------------------------------------------
def _emit(console: Console, text: str = "", style: str = "") -> None:
    """One line, verbatim and never wrapped, so that a command can be copied from it."""
    console.print(Text(plain(text, console), style=style), soft_wrap=True)


def _next_steps(console: Console, config: Path, kept: Path | None) -> None:
    _emit(console)
    _emit(console, "Next", "bold")
    if kept is None:
        _emit(console, "  On your own logs:", "bold")
        for command, what in TRY_ON_YOUR_OWN:
            _emit(console, f"    {command}")
            _emit(console, f"      {what}", "dim")
        _emit(console, "  To explore this demo, keep it in a directory (it prints the flags to run any command on it):", "bold")
        _emit(console, "    agentfare demo --keep ./agentfare-demo")
        _emit(console, "  Or run any command on the packaged demo data from here:", "bold")
        _emit(console, f"    export AGENTFARE_NOW={DEMO_NOW}")
        _emit(console, f'    agentfare --config {shlex.quote(str(config))} --home "$(mktemp -d)" report --format html -o demo-week.html')
        return
    flags = _flags(kept / "demo_data" / "config.yaml", kept / "home")
    _emit(console, f"  Kept in {kept}. Run any command on it with these flags in front of the command:", "bold")
    _emit(console, f"    export AGENTFARE_NOW={DEMO_NOW}")
    _emit(console, "      the demo's clock: --week and the report mean the demo week", "dim")
    for command, _ in (("summary --week", ""), ("advise --all", ""), ('run --dry-run -- claude -p "refactor the auth module and migrate the database"', ""),
                       ("report --format html -o demo-week.html", "")):
        _emit(console, f"    agentfare {flags} {command}")
    _emit(console, f"  Delete it with: rm -r {shlex.quote(str(kept))}", "dim")


def _payload(summary: dict[str, Any], advice: dict[str, Any], kept: Path | None, report: Path | None) -> dict[str, Any]:
    total, split = advice["total"], advice["saved_split"]
    return {
        "synthetic": True,
        "now": DEMO_NOW,
        "sessions": summary["sessions"],
        "projects": summary["projects"],
        "models": summary["models"],
        "tokens": summary["tokens"],
        "total": summary["total"],
        "excluded_tokens": summary["excluded_tokens"],
        "weeks": [{"week": w["week"], "sessions": w["sessions"], "total": w["cost"]} for w in summary["by_week"]],
        "savings": {
            "sessions": total["sessions"], "switched": total["switched"], "actual": total["actual"], "router": total["recommended"],
            "saved": total["saved"], "saved_bp": total["saved_bp"], "prompted": split["prompted"], "background": split["background"],
        },
        "top_advice": [{key: s[key] for key in ADVICE_KEYS} for s in advice["sessions"]],
        "warnings": summary["warnings"],
        "kept": None if kept is None else {"dir": str(kept), "config": str(kept / "demo_data" / "config.yaml"), "home": str(kept / "home")},
        "report": None if report is None else str(report),
    }


# ---- the command -----------------------------------------------------------------------------------------------------
HELP = """Run AgentFare on invented sessions: the spend, the advice, and what to try next.

The data is synthetic and packaged with AgentFare (45 sessions of Claude Code, Codex CLI and Gemini CLI over two weeks), so the demo works before you have a history of your own. Your own agent logs and ~/.agentfare are neither read nor written: the demo copies its data to a temporary directory, reads it into a temporary database with the clock frozen at 2026-09-30T18:00:00Z, and deletes both when it ends (--keep keeps them). --config and --home are ignored here. Nothing uses the network."""


def register(app: typer.Typer) -> None:
    @app.command("demo", help=HELP)
    def demo_cmd(
        ctx: typer.Context,
        keep: Annotated[Optional[Path], typer.Option("--keep", metavar="DIR", help="Keep the demo data and its database in this new or empty directory, and print the flags to run any command on them.")] = None,
        report: Annotated[Optional[Path], typer.Option("--report", metavar="FILE", help="Also write the HTML report of the latest demo week (2026-W40, still in progress) to FILE, with the invented project names shown.")] = None,
        as_json: JsonOpt = False,
    ) -> None:
        st = ctx.obj
        with clean_errors():
            kept = _target(keep)
            report_path = Path(os.path.expanduser(report)).absolute() if report is not None else None
            with contextlib.ExitStack() as stack:
                if kept is None:
                    root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="agentfare-demo-")))
                else:
                    root = kept
                data = _copy_data(root)
                config, home = data / "config.yaml", root / "home"
                stack.enter_context(_frozen_clock())
                front = ["--config", str(config), "--home", str(home)]
                ingested = _json([*front, "ingest", "--json"])  # reads the logs now and says nothing, so that no first-run note shows
                if ingested["errors"] or ingested["files_failed"]:
                    raise ConfigError(f"the demo data could not be read: {'; '.join(ingested['errors'][:3])}")
                if as_json:
                    summary, advice = _json([*front, "--no-ingest", "summary", "--json"]), _json([*front, "--no-ingest", "advise", "--limit", str(TOP), "--json"])
                else:
                    _emit(st.out, "Synthetic demo data: invented sessions, not yours. Your own logs are not read.", "bold yellow")
                    for argv, shown in ((["summary"], "summary"), (["advise", "--limit", str(TOP)], f"advise --limit {TOP}")):
                        _emit(st.out)
                        _emit(st.out, f"$ agentfare {shown}", "bold")
                        code = _invoke([*front, "--no-ingest", *argv])
                        if code:
                            raise typer.Exit(code)
                if report_path is not None:
                    code, _, err = _capture([*front, "--no-ingest", "report", "--format", "html", "--show-projects", "-o", str(report_path)])
                    if code:
                        typer.echo(err, err=True, nl=False)
                        raise typer.Exit(code)
            if as_json:
                emit_json(_payload(summary, advice, kept, report_path))
                return
            if report_path is not None:
                _emit(st.out)
                _emit(st.out, f"Wrote the report of the demo week to {report_path}")
            _next_steps(st.out, PACKAGED / "config.yaml", kept)
