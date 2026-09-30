"""`agentfare run`: pick a model for an agent command, show the decision first, then run the command."""

from __future__ import annotations

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..aggregate import model_warnings
from ..config import ConfigError
from ..db import Store, StoreError
from ..render import clean_errors, emit_json, money_json, percent, plain, plural, usd
from ..router import BASELINE_DAYS, RunPick, pick_for_run
from ..runwrap import Invocation, analyze, child_env, is_option_name, resolve_agent
from ..safeio import MAX_PROMPT_BYTES, read_text_limited
from ..state import AppState

EXIT_DECLINED, EXIT_REFUSED, EXIT_NOT_RUNNABLE, EXIT_NOT_FOUND = 1, 3, 126, 127
NO_HISTORY = "no usage history yet (run agentfare ingest to personalize estimates)"


@dataclass(frozen=True)
class Plan:
    """Everything decided before the agent starts."""

    inv: Invocation  # the command exactly as it will run, model flag included
    agent: str  # the configured agent, or the command's own name when it is not configured
    configured: bool
    pick: RunPick | None  # None when the command starts no model session
    advisory: RunPick | None  # what the router alone would pick, when the command chose its own model
    prompt_note: str
    warnings: list[str]
    found: bool  # the command is on PATH

    @property
    def model(self) -> str | None:
        """The model the agent will run with; None when it keeps its own default."""
        return self.inv.model if self.pick and self.inv.model else None

    @property
    def unapplied(self) -> bool:
        """The router chose a model but the command has no way to receive it (no model flag is known)."""
        return bool(self.pick and self.pick.model and not self.inv.model)

    @property
    def agrees(self) -> bool:
        """The router alone would pick the model the command already selects, or what its alias stands for."""
        chosen = {self.inv.model, self.pick.alias_for if self.pick else None} - {None}
        return bool(self.advisory and self.advisory.model in chosen)


# ---- deciding ---------------------------------------------------------------------------------
def _prompt(text: str | None, file: Path | None) -> str | None:
    if file is None:
        return text
    try:
        return read_text_limited(file, MAX_PROMPT_BYTES, errors="replace")  # a regular file of at most 2 MB, never /dev/zero or a pipe
    except OSError as exc:
        raise ConfigError(f"cannot read {file} (--prompt-file): {exc.strerror or exc}") from exc


def _prompt_note(given: str | None, found: str | None) -> str:
    if given is not None:
        return f"{plural(len(given), 'char')}, from --prompt / --prompt-file"
    if found is not None:
        return f"{plural(len(found), 'char')}, from the command line"
    return "not on the command line: prompt rules (length, keywords) skipped; --prompt or --prompt-file adds them"


def _price_warnings(st: AppState, picks: Sequence[RunPick]) -> list[str]:
    """The router's warnings, plus a note for every priced model that has an unverified optional price (cache rates)."""
    out = [w for p in picks for w in p.warnings]
    models = {m for p in picks for m in (p.priced_model, p.baseline_model) if m}
    out += [w for m, w in model_warnings(models, st.table).items() if st.table.status(m) == "priced"]  # the router names the unpriced ones
    return list(dict.fromkeys(out))


def _open_history(st: AppState) -> Store | None:
    """The database for the estimate, or None when there is none: a run never creates it and never reads the logs into it
    (that is ``ingest``), so the decision is on screen at once, even on a first run."""
    path = st.cfg.db_path
    return Store.open_read_only(path) if path.is_file() else None


def _route(st: AppState, agent: str, prompt: str, repo: Path, chosen: str | None) -> tuple[RunPick, RunPick, list[str]]:
    """(what the router picks, what will run, notes and warnings). The database only caches the logs: a history that cannot be
    read costs the estimate its precision (the default token budgets stand in), not the run."""

    def pick(store: Store | None, force: str | None) -> RunPick:
        return pick_for_run(st.cfg, st.table, store, agent=agent, prompt=prompt, repo_path=repo, force_model=force)

    store: Store | None = None
    try:
        store = _open_history(st)
        notes = [] if store is not None and store.count() else [NO_HISTORY]
        router = pick(store, None)
        return router, pick(store, chosen) if chosen else router, notes
    except (StoreError, sqlite3.Error, OSError) as exc:
        detail = str(exc) if isinstance(exc, StoreError) else f"cannot read the database {st.cfg.db_path}: {exc}"
        router = pick(None, None)
        return router, pick(None, chosen) if chosen else router, [f"usage history unavailable, so estimates use the default token budgets ({detail})"]
    finally:
        if store is not None:
            store.close()


def build_plan(
    st: AppState, argv: Sequence[str], *, force_model: str | None, model_flag: str | None, prompt: str | None,
) -> Plan:
    cfg = st.cfg
    agent = resolve_agent(cfg.get("agents"), argv[0])
    name = agent.name if agent else os.path.basename(argv[0])
    flag = model_flag or (agent.model_flag if agent else None)
    inv = analyze(argv, agent.name if agent else None, flag)
    found = shutil.which(argv[0], mode=os.F_OK) is not None  # a file that cannot be executed is spawn's error to report (126)
    missing = [] if found else [f"command not found on PATH: {argv[0]}"]
    if not inv.session:
        ignored = [f"--force-model ignored: '{' '.join(inv.words)}' does not talk to a model"] if force_model else []
        return Plan(inv, name, agent is not None, None, None, "", [*missing, *ignored], found)
    existing = inv.model
    if existing and force_model and existing != force_model:
        raise typer.BadParameter(
            f"the command already selects model {existing}; drop that flag or drop --force-model", param_hint="--force-model")
    text = prompt if prompt is not None else inv.prompt
    chosen = existing or force_model
    try:
        repo = Path.cwd()
    except OSError as exc:
        raise _fail(f"cannot read the current directory: {exc.strerror or exc}", 2) from exc
    router, pick, history = _route(st, name, text or "", repo, chosen)
    warnings = [*history, *_price_warnings(st, [pick, router] if existing else [pick])]
    if pick.model and not existing:
        if flag:
            inv = inv.with_model(flag, pick.model)
        else:
            warnings.append(
                f"no model flag is known for {name}, so the command runs unchanged "
                f"(set agents.{name}.model_flag in the config, or pass --model-flag)")
    return Plan(inv, name, agent is not None, pick, router if existing else None, _prompt_note(prompt, inv.prompt), [*missing, *warnings], found)


# ---- showing the decision ---------------------------------------------------------------------
def _basis(pick: RunPick) -> str:
    """Where the token budget of the estimate comes from."""
    if pick.history_sessions:
        return f"median of {pick.history_sessions} past {pick.agent}/{pick.tier} sessions"
    return f"typical {pick.tier} task (no history yet)"


def _label(pick: RunPick) -> str:
    """The model as the estimate prices it: ``opus (alias for claude-opus-5-5)`` for a Claude Code alias."""
    return f"{pick.model} (alias for {pick.alias_for})" if pick.alias_for else str(pick.model)


def _estimate(pick: RunPick) -> str:
    if pick.model is None:
        return "none: no model is set, so the agent runs with its own default"
    if pick.est_ucents is None:
        return f"unavailable: {pick.priced_model} has no verified price"
    return f"~{usd(pick.est_ucents)} on {_label(pick)}, {_basis(pick)}"


def _meaning(pick: RunPick) -> str:
    """What the baseline is, in words: where its model comes from and, for the recent one, the day the window starts."""
    if pick.baseline_basis == "recent":
        return f"your most used {pick.agent} model in the last {BASELINE_DAYS} days, since {pick.baseline_since}"
    if pick.baseline_basis == "all_time":
        return f"your most used {pick.agent} model overall, none in the last {BASELINE_DAYS} days"
    return "what you would run without the router"


def _saving(pick: RunPick) -> str | None:
    """The saving against the baseline; nothing when the router runs the baseline itself, because there is nothing to save."""
    if pick.est_ucents is None or pick.baseline_ucents is None or pick.same_as_baseline:
        return None
    diff = pick.baseline_ucents - pick.est_ucents
    if diff > 0:
        return f"~{usd(diff)} ({percent(diff, pick.baseline_ucents)} less than the baseline)"
    if diff < 0:
        return f"none: ~{usd(-diff)} MORE than the baseline"
    return "none: same cost as the baseline"


def _model_text(plan: Plan, pick: RunPick) -> str:
    if plan.advisory:
        note = "; the router agrees" if plan.agrees else ", the router pick below is advisory" if plan.advisory.model else ""
        return f"{plan.inv.model} (the command already selects it{note})"
    if not pick.model:
        return "none: the agent runs with its own default model"
    if plan.unapplied:
        chosen = f"--force-model {pick.model}" if pick.forced else f"the router pick {pick.model}"
        return f"none: the agent runs with its own default model ({chosen} cannot be passed on)"
    return f"{pick.model} ({'--force-model' if pick.forced else 'router pick'})"


def _rows(plan: Plan) -> list[tuple[str, str, str]]:
    """(label, text, style) for each line of the panel."""
    rows = [("agent", plan.agent if plan.configured else f"{plan.agent} (not in your agents config: no model mapping)", "")]
    pick = plan.pick
    if pick is None:
        rows.append(("note", f"'{' '.join(plan.inv.words)}' does not talk to a model: nothing to route", ""))
    else:
        rows += [("prompt", plan.prompt_note, ""), ("tier", f"{pick.tier} (score {pick.score})", "")]
        rows += [("", f"- {reason}", "dim") for reason in pick.reasons]
        rows.append(("model", _model_text(plan, pick), "bold"))
        if plan.advisory and plan.advisory.model and not plan.agrees:
            rows.append(("router", f"would pick {plan.advisory.model} instead: {_estimate(plan.advisory)}", ""))
        if not plan.unapplied:  # an estimate or a saving for a model that will not run would be false
            rows.append(("estimate", _estimate(pick), ""))
            if pick.baseline_model:
                base = f"~{usd(pick.baseline_ucents)} on {pick.baseline_model}" if pick.baseline_ucents is not None else f"unavailable: {pick.baseline_model} has no verified price"
                rows.append(("baseline", f"{base} ({_meaning(pick)})", ""))
            if saving := _saving(pick):
                rows.append(("saving", saving, "bold green" if saving.startswith("~") else ""))
    rows.append(("command", plan.inv.display(), "bold"))
    rows += [("warning" if i == 0 else "", w, "yellow") for i, w in enumerate(plan.warnings)]
    return rows


def decision_json(plan: Plan) -> dict[str, Any]:
    """The decision as ``--dry-run --json`` prints it: what the panel shows, as data. Money is ``{cents, ucents}`` or null when
    there is no estimate (or, as in the panel, when the model cannot be passed on); ``argv`` is what the agent would get,
    redacted (``Invocation.shown_argv``): the prompt and every argument but the program, the known subcommand words, the model
    flag with its value and the names of the flags are replaced by their length."""
    pick = plan.pick
    priced = pick is not None and not plan.unapplied
    estimate = pick.est_ucents if pick and priced else None
    baseline = pick.baseline_ucents if pick and priced else None
    return {
        "agent": plan.agent,
        "tier": pick.tier if pick else None,
        "score": pick.score if pick else None,
        "reasons": pick.reasons if pick else [],
        "model": plan.model,
        "forced": bool(pick and pick.forced and not plan.advisory),
        "estimate": None if estimate is None else money_json(estimate),
        "baseline": None if baseline is None else money_json(baseline),
        "baseline_model": pick.baseline_model if pick else None,
        "baseline_basis": pick.baseline_basis if pick else None,
        "baseline_since": pick.baseline_since if pick else None,
        "history_sessions": pick.history_sessions if pick else 0,
        "warnings": plan.warnings,
        "argv": plan.inv.shown_argv(),
    }


def show(console: Console, plan: Plan, *, dry_run: bool) -> None:
    """The panel, and under it every line group (a label and the rows that continue it) that holds a word too long for it: a
    file path in a warning is printed whole, wrapped between words by the console, instead of being cut with ``…`` in the frame."""
    groups: list[list[tuple[str, str, str]]] = []
    for label, text, style in _rows(plan):
        row = (label, plain(text, console), style)
        if label or not groups:
            groups.append([row])
        else:
            groups[-1].append(row)
    room = console.width - 6 - max(len(g[0][0]) for g in groups)  # the frame, its padding and the label column
    wide = [g for g in groups if any(max(map(len, text.split()), default=0) > room for _, text, _ in g)]
    grid = Table.grid(padding=(0, 2))
    grid.add_column(no_wrap=True, style="bold")
    grid.add_column(overflow="fold")
    for group in (g for g in groups if g not in wide):
        for label, text, style in group:
            grid.add_row(label, Text(text, style=style))
    console.print(Panel(grid, title="agentfare run (dry run)" if dry_run else "agentfare run", title_align="left"))
    for group in wide:
        for i, (label, text, style) in enumerate(group):
            for line in textwrap.wrap(f"{label}: {text}" if i == 0 else f"  {text}", console.width, subsequent_indent="  ", break_long_words=False, break_on_hyphens=False):
                console.print(Text(line, style=style), soft_wrap=True)  # only the long word is wider than the console


# ---- running ----------------------------------------------------------------------------------
def _stdin_is_tty() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()  # None when stdin is closed


def _terminal_leads() -> bool:
    """True when this process group is the foreground group of its terminal, which then delivers Ctrl-C and Ctrl-\\ to every
    process in it, the agent included."""
    try:
        fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
    except OSError:  # no terminal at all
        return False
    try:
        return os.tcgetpgrp(fd) == os.getpgrp()
    except OSError:
        return False
    finally:
        os.close(fd)


def spawn(argv: Sequence[str], env: dict[str, str]) -> int:
    """Run ``argv`` (never through a shell) with stdin, stdout and stderr shared; return its wait status: the exit code,
    or minus the number of the signal that killed it.

    Ctrl-C (SIGINT) and Ctrl-\\ (SIGQUIT) from the terminal already reach the agent, and a second copy would count as a
    second Ctrl-C, so this process only waits for it then. Sent from anywhere else (a supervisor that signals the one pid it
    started, a background job) they would never reach the agent, so they are passed on, like SIGTERM and SIGHUP. A signal
    that was ignored on entry (``nohup``, a background job in a script) is left ignored, and so stays ignored in the agent.
    """
    child: subprocess.Popen[bytes] | None = None
    early: list[int] = []

    def relay(signum: int, _frame: object) -> None:
        if child:
            child.send_signal(signum)
        else:  # the agent is not started yet: pass the signal on as soon as it is
            early.append(signum)

    def relay_unless_terminal(signum: int, frame: object) -> None:
        if not _terminal_leads():
            relay(signum, frame)

    handlers = {signal.SIGINT: relay_unless_terminal, signal.SIGQUIT: relay_unless_terminal, signal.SIGTERM: relay, signal.SIGHUP: relay}
    previous = {
        signum: signal.signal(signum, handler)  # a handler, unlike SIG_IGN, is reset to the default in the agent
        for signum, handler in handlers.items()
        if signal.getsignal(signum) != signal.SIG_IGN
    }
    try:
        child = subprocess.Popen(list(argv), env=env)
        for signum in early:
            child.send_signal(signum)
        return child.wait()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _end_like_the_agent(status: int) -> typer.Exit:
    """The exit for a wait status: the agent's code, or 128 + N for a kill by signal N, as a shell reports it.

    An agent stopped by SIGINT ends this process by SIGINT too: a calling script stops on a child killed that way, but
    takes a plain exit code of 130 for an ordinary failure and carries on with its next command.
    """
    if status == -signal.SIGINT:
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGINT)  # does not return, unless SIGINT is blocked
    return typer.Exit(status if status >= 0 else 128 - status)


def _fail(message: str, code: int) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def register(app: typer.Typer) -> None:
    @app.command("run", context_settings={"allow_interspersed_args": False})
    def run_cmd(
        ctx: typer.Context,
        command: Annotated[list[str], typer.Argument(metavar="COMMAND...", help="The agent command; put -- before it so its own flags are left alone.")],
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the decision and the command, run nothing.")] = False,
        force_model: Annotated[Optional[str], typer.Option("--force-model", metavar="MODEL", help="Use this model instead of the router's pick.")] = None,
        model_flag: Annotated[Optional[str], typer.Option("--model-flag", metavar="FLAG", help="How the agent takes a model, when the config does not say (e.g. --model).")] = None,
        prompt: Annotated[Optional[str], typer.Option("--prompt", metavar="TEXT", help="Score this prompt when the command line has none (an interactive session).")] = None,
        prompt_file: Annotated[Optional[Path], typer.Option("--prompt-file", metavar="FILE", help="Like --prompt, read from a file.")] = None,
        yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask; needed when stdin is not a terminal.")] = False,
        as_json: Annotated[bool, typer.Option("--json", help="With --dry-run: print the decision as JSON (money as integer cents and ucents) instead of the panel.")] = False,
    ) -> None:
        """Pick a model for an agent command, show why, then run the command with it.

        Everything after -- is the agent command, passed through untouched except for one added model flag, for example: agentfare run -- claude -p "fix the typo in the README"

        The decision (tier and its reasons, model, estimated cost against the baseline, the command) is printed before anything runs. It goes to stderr so the agent's own output stays clean; --dry-run prints it on stdout and stops, and --dry-run --json prints it as JSON. The prompt is shown as its length, never as text. So is every other argument, because a flag can hold a secret (--api-key sk-...): only the program, the subcommand words it knows (exec, resume, mcp, ...), the model flag with its value and the names of the flags are shown, and the rest reads <arg: N chars>. The agent still gets every argument untouched.

        The estimate is read from the usage history already in the database: run never reads your logs and never creates the database (agentfare ingest does), so the decision appears at once, even on a first run.

        A model flag already in the command is respected: the router's pick is then only advisory. The exit code is the agent's own, except: 1 you answered no, 2 usage or configuration error, 3 stdin is not a terminal and there is no --yes, 126 the command cannot be run, 127 it is not found.
        """
        st: AppState = ctx.obj
        if as_json and not dry_run:
            raise typer.BadParameter("only works with --dry-run: a run shows its decision to a person, not to a script", param_hint="--json")
        if prompt is not None and prompt_file is not None:
            raise typer.BadParameter("give --prompt or --prompt-file, not both")
        if force_model is not None and (not force_model.strip() or force_model.strip().startswith("-")):
            raise typer.BadParameter("needs a model id, and a value that starts with - is an option", param_hint="--force-model")
        if model_flag is not None and not is_option_name(model_flag.strip()):
            raise typer.BadParameter("needs an option name such as --model or -m", param_hint="--model-flag")
        with clean_errors():
            plan = build_plan(
                st, command, force_model=force_model.strip() if force_model else None,
                model_flag=model_flag.strip() if model_flag else None, prompt=_prompt(prompt, prompt_file),
            )
        if as_json:
            emit_json(decision_json(plan))
            return
        show(st.out if dry_run else st.err, plan, dry_run=dry_run)
        if dry_run:
            return
        if not plan.found:
            raise _fail(f"command not found: {command[0]}", EXIT_NOT_FOUND)
        if not yes:
            if not _stdin_is_tty():
                raise _fail("not running: stdin is not a terminal, so there is nobody to ask. Add --yes to run without asking.", EXIT_REFUSED)
            if not typer.confirm("Run this?", default=True, err=True):
                raise typer.Exit(EXIT_DECLINED)
        env = child_env(os.environ, plan.pick.tier if plan.pick else None, plan.model)
        try:
            status = spawn(plan.inv.argv, env)
        except FileNotFoundError as exc:
            raise _fail(f"command not found: {command[0]}", EXIT_NOT_FOUND) from exc
        except OSError as exc:
            raise _fail(f"cannot run {command[0]}: {exc.strerror or exc}", EXIT_NOT_RUNNABLE) from exc
        raise _end_like_the_agent(status)
