"""`agentfare init`: the 60-second setup. Find the agent logs, read them once, show what was spent and what to run next."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.text import Text

from ..aggregate import Group, group_rows, priced_rows
from ..config import Config, ConfigError, day_of
from ..ingest import IngestReport, duplicates_warning
from ..models import AGENTS, short_id
from ..money import UCENTS_PER_CENT, format_usd
from ..render import (
    SETUP_ERRORS, clean_text, emit_json, money_json, percent, plain, plural, price_warnings, print_error, print_warnings, say,
    share_bp, spend_text, usd,
)
from ..router import Advice, advise_sessions, format_advice_line, min_cost_ucents, total_savings, weekly_savings
from ..safeio import make_private_dirs
from ..setup_checks import AGENT_LABELS, AgentScan, count_text, human_size, scan_agents, tilde, wrap_text
from ..state import AppState
from .advise import View
from .ingest import build_report

HELP = """Set AgentFare up in about a minute: find your agent logs, read them once, and show what you spent.

Only metadata and token counts are stored, never prompts, replies or file contents. init looks at the log folders of Claude \
Code, Codex CLI and Gemini CLI (following CLAUDE_CONFIG_DIR, CODEX_HOME, GEMINI_CLI_HOME and any sources in your config), \
creates the data directory (private to you), reads the logs into the local database and prints the total, the biggest \
saving advise sees and what to run next. A config file is written only with --write-config: AgentFare works without one. \
It is safe to run again: only new or changed logs are read, and an existing config file is never overwritten."""

RUN_EXAMPLES = {
    "claude": 'claude -p "fix the typo in the README"',
    "codex": 'codex exec "fix the typo in the README"',
    "gemini": 'gemini -p "fix the typo in the README"',
}
LABEL_WIDTH = 14  # "Data directory"


@dataclass
class Outcome:
    """What the ingest and the figures after it came to: the payload ``--json`` prints and the lines of the text."""

    payload: dict[str, Any]
    ingest: dict[str, Any] | None
    lines: list[str] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)


def _amount(ucents: int) -> str:
    """A saving at the precision ``advise`` prints it: four decimals unless it is a whole number of cents."""
    return format_usd(ucents, 4) if ucents % UCENTS_PER_CENT else usd(ucents)


def _biggest(advice: Advice, encoding: str) -> dict[str, Any]:
    s = advice.session
    return {
        "key": s.key, "agent": s.agent, "session_id": s.session_id, "project": s.project, "short_id": short_id(s.session_id),
        "model": advice.model, "recommended_model": advice.recommended_model, "actual": money_json(advice.actual_ucents),
        "recommended": money_json(advice.recommended_ucents or 0), "saved": money_json(advice.saved_ucents),
        "line": format_advice_line(advice, encoding),
    }


def _run_agent(scans: list[AgentScan]) -> str:
    """The agent the ``run`` example uses: the first one with logs, else the first one whose command is on PATH, else Claude Code."""
    for wanted in (lambda s: s.found, lambda s: s.cli_path):
        for scan in scans:
            if wanted(scan):
                return scan.agent
    return "claude"


def _next_steps(mode: str, scans: list[AgentScan]) -> list[dict[str, str]]:
    """The commands to suggest after ``mode``: ``dry-run``, ``skipped`` (no ingest), ``found`` sessions or ``empty``."""
    run = f"agentfare run --dry-run -- {RUN_EXAMPLES[_run_agent(scans)]}"
    steps = {
        "init": ("agentfare init", "do the setup for real: it reads the logs into the local database"),
        "ingest": ("agentfare ingest", "read the logs into the local database now"),
        "summary": ("agentfare summary", "spend by model, project and day"),
        "advise": ("agentfare advise", "where a cheaper model would have done the job"),
        "report": ("agentfare report --format html", "a weekly report as one HTML page in the current folder, to share"),
        "run": (run, "see which model the router picks for a task before you start it, and what it would cost"),
        "doctor": ("agentfare doctor", "check the setup and get a fix for anything that is off"),
        "prices": ("agentfare prices --refresh", "re-check the prices against the official pages (the only command that uses the network)"),
        "config": ("agentfare config", "list the log folders AgentFare looks in and mark the missing ones"),
        "demo": ("agentfare demo", "see AgentFare work on invented sample sessions"),
    }
    order = {
        "dry-run": ["init", "doctor"],
        "skipped": ["ingest", "summary", "advise", "doctor"],
        "found": ["summary", "advise", "report", "run", "doctor", "prices"],
        "empty": ["doctor", "config", "demo"],
    }[mode]
    return [{"command": steps[key][0], "purpose": steps[key][1]} for key in order]


# ---- the figures after the ingest ---------------------------------------------------------------------------------
def _outcome(st: AppState, cfg: Config, rep: IngestReport, store_count: int, *, utf8: bool) -> Outcome:
    store = st.store()
    table = st.table
    sessions = store.sessions()
    rows = priced_rows(sessions, table, cfg.tz)
    grand = (group_rows(rows, lambda r: "") or [Group(key="")])[0]
    nothing_priced = bool(grand.unpriced_tokens) and grand.unpriced_tokens >= grand.usage.total
    advice = advise_sessions(sessions, cfg, table)
    weeks = weekly_savings(advice, cfg)
    total = total_savings(weeks)
    best = View.build(advice, min_cost_ucents(cfg), False, 1).listed
    encoding = "utf-8" if utf8 else st.out.encoding
    span = store.usage_range()
    by_agent = {a: n for a in AGENTS if (n := sum(s.agent == a for s in sessions))}
    warnings = price_warnings(rows, table, cfg)
    payload = {
        "sessions": store_count, "by_agent": by_agent,
        "first_day": day_of(span[0], cfg.tz) if span else None, "last_day": day_of(span[1], cfg.tz) if span else None,
        "spend": None if nothing_priced else money_json(grand.ucents), "excluded_tokens": grand.unpriced_tokens,
        "warnings": warnings,
        "savings": {"compared": total.sessions, "switched": total.switched, "saved": money_json(total.saved_ucents),
                    "actual": money_json(total.actual_ucents),
                    "saved_bp": share_bp(total.saved_ucents, total.actual_ucents) if total.actual_ucents else None},
        "biggest": _biggest(best[0], encoding) if best else None,
    }
    lines: list[str] = []
    if store_count:
        who = ", ".join(f"{agent} {n}" for agent, n in by_agent.items())
        first, last = payload["first_day"], payload["last_day"]
        lines.append(f"Found {plural(store_count, 'session')} ({who}) " + (f"on {first}." if first == last else f"from {first} to {last}."))
        lines.append("Spend so far: " + spend_text(money_json(grand.ucents), grand.unpriced_tokens, grand.usage.total) + ".")
        if total.switched:
            lines.append(f"advise sees a cheaper model for {plural(total.switched, 'session')}, about {_amount(total.saved_ucents)} less "
                         f"({percent(total.saved_ucents, total.actual_ucents)} of the spend it compared).")
        else:
            lines.append("advise sees no session where a cheaper model would clearly have done the job.")
        if best:
            lines.append("Biggest: " + format_advice_line(best[0], encoding))
    return Outcome(payload, build_report(rep, str(cfg.db_path), store_count), lines, warnings)


def _empty_payload() -> dict[str, Any]:
    return {"sessions": 0, "by_agent": {}, "first_day": None, "last_day": None, "spend": money_json(0), "excluded_tokens": 0,
            "warnings": [], "savings": {"compared": 0, "switched": 0, "saved": money_json(0), "actual": money_json(0), "saved_bp": None},
            "biggest": None}


# ---- the text ---------------------------------------------------------------------------------------------------------
class Printer:
    """Wrapped lines on the console, in the way the other commands print them: literal text, a path never broken."""

    def __init__(self, console: Console) -> None:
        self.console = console
        self.width = max(40, min(console.width, 100))

    def line(self, text: str = "", style: str = "", *, first: str = "", later: str | None = None) -> None:
        for piece in wrap_text(text, self.width, first=first, later=first if later is None else later):
            self.console.print(Text(plain(piece, self.console), style=style), soft_wrap=True)  # wrapped already: Rich would fold a long path

    def labelled(self, label: str, text: str, style: str = "") -> None:
        pad = " " * (LABEL_WIDTH + 2)
        self.line(text, style, first=f"{label:<{LABEL_WIDTH}}  ", later=pad)


def _config_hint(cfg: Config) -> str:
    return tilde(cfg.config_path or cfg.home / "config.yaml")


def _print_agent(out: Printer, cfg: Config, scan: AgentScan) -> None:
    pad = " " * (len(max(AGENT_LABELS.values(), key=len)) + 2)
    unit = f"{count_text(scan.files, 'file')}, {human_size(scan.size)}"
    status = f"found {'at least ' if scan.truncated else ''}{unit}" if scan.found else "not found"
    out.line(status, first=f"{scan.label:<{len(pad) - 2}}  ", later=pad)
    for root in scan.roots:
        note = "" if root.exists and root.readable else " (missing)" if not root.exists else f" (cannot read: {root.error})"
        out.line(f"{tilde(root.path)}{note}", first=pad, later=pad)
    if scan.origin == "config":
        out.line(f"folders from sources.{scan.agent} in {_config_hint(cfg)}", "dim", first=pad, later=pad)
    elif scan.origin != "default":
        out.line(f"folders from {scan.origin}", "dim", first=pad, later=pad)
    out.line(f"CLI {tilde(scan.cli_path)}" if scan.cli_path else "CLI not on PATH", "dim", first=pad, later=pad)
    if not scan.found:
        out.line(f"If you use {scan.label} from another folder, set sources.{scan.agent} in {_config_hint(cfg)}.", first=pad, later=pad)


def _print_head(out: Printer, cfg: Config, scans: list[AgentScan]) -> None:
    say(out.console, "AgentFare setup", "bold")
    out.line()
    out.line("AgentFare will read the session logs in the folders below. It stores only metadata and token counts in "
             f"{tilde(cfg.db_path)}, never prompts, replies or file contents, and it changes nothing in those folders.")
    out.line()
    for scan in scans:
        _print_agent(out, cfg, scan)
    out.line()


def _print_next_steps(out: Printer, steps: list[dict[str, str]]) -> None:
    out.line()
    say(out.console, "Next steps", "bold")
    for step in steps:
        out.line(step["command"], first="  ", later="      ")
        out.line(step["purpose"], "dim", first="      ", later="      ")


def _print_empty(out: Printer, cfg: Config) -> None:
    out.line()
    say(out.console, "No sessions found yet", "bold")
    out.line("AgentFare did not find any session logs in the folders above. That is fine on a machine where the agents have not run yet.")
    out.line(f"If you keep the logs somewhere else, set sources.<agent> in {_config_hint(cfg)} (agentfare init --write-config creates it) "
             "and run agentfare init again.")
    out.line("agentfare doctor shows what was checked and gives a fix for anything that is off, and agentfare demo shows the tool "
             "working on sample sessions.")


# ---- the command ------------------------------------------------------------------------------------------------------
def _print_ingest_notes(st: AppState, rep: IngestReport) -> None:
    """The ingest's own warnings, on stderr as every command prints them (a missing folder is already in the table)."""
    for msg in rep.errors[:5]:
        st.err.print(f"[yellow]warning:[/yellow] could not parse {escape(clean_text(msg))}", soft_wrap=True)
    if len(rep.errors) > 5:
        st.err.print(f"[yellow]warning:[/yellow] ... and {len(rep.errors) - 5} more unreadable files")
    if rep.duplicates:
        st.err.print(f"[yellow]warning:[/yellow] {duplicates_warning(rep.duplicates)}", soft_wrap=True)


def _setup(st: AppState, *, dry_run: bool, write_config: bool, skip_ingest: bool, as_json: bool) -> None:
    fresh = Config.for_init(st.config_path, st.home)  # knows where things go without reading the config file
    target = fresh.default_init_target
    data_existed, config_existed = os.path.exists(fresh.home), os.path.lexists(target)  # os.path: False, not an error, for a folder nobody may search
    config_written = False
    if write_config and not config_existed and not dry_run:
        fresh.write_default(target)  # before the config is loaded, so that `--config new.yaml --write-config` works in one step
        config_written = True
    cfg = st.cfg
    scans = scan_agents(cfg)
    out = Printer(st.out)
    if not as_json:
        _print_head(out, cfg, scans)

    in_use = cfg.config_path is not None
    payload: dict[str, Any] = {
        "dry_run": dry_run,
        "data_dir": {"path": str(cfg.home), "exists": data_existed, "created": False},
        "config": {"path": str(target), "exists": config_existed, "in_use": in_use, "written": config_written},
        "agents": [scan.as_json() for scan in scans], "ingest": None, "result": None, "next_steps": [],
    }
    where = tilde(target)
    if not dry_run:
        try:
            make_private_dirs(cfg.home)
        except OSError as exc:
            raise ConfigError(f"cannot create the data directory {cfg.home}: {exc.strerror or exc}") from exc
        payload["data_dir"]["created"] = not data_existed
    if not as_json:
        data_text = ("exists" if data_existed else "would be created, private to you (0700)" if dry_run
                     else "created, private to you (0700)")
        out.labelled("Data directory", f"{tilde(cfg.home)} {data_text}")
        if config_written:
            out.labelled("Config file", f"wrote {where} with every default; edit it to change a setting")
        elif config_existed and write_config:
            out.labelled("Config file", f"{where} already exists, left as it is")
        elif in_use:
            out.labelled("Config file", f"{tilde(cfg.config_path)} (in use)")
        elif write_config:
            out.labelled("Config file", f"would write {where} with every default")
        else:
            out.labelled("Config file", f"none. AgentFare works without a config file: the defaults are built in. "
                                        f"agentfare init --write-config writes {where} with every setting to edit.")

    if dry_run:
        payload["next_steps"] = _next_steps("dry-run", scans)
        if as_json:
            emit_json(payload)
            return
        files = sum(s.files for s in scans)
        actions = [] if data_existed else [f"create {tilde(cfg.home)} (0700)"]
        actions.append(f"read {count_text(files, 'log file')} ({human_size(sum(s.size for s in scans))}) into {tilde(cfg.db_path)}" if files
                       else "find no session files to read")
        if not (config_existed or in_use):
            actions.append(f"write {where}" if write_config else "write no config file (add --write-config to write one)")
        out.line()
        out.line("Dry run: nothing was created, written or read into the database.")
        out.line("Without --dry-run, init would " + ", ".join(actions[:-1]) + (" and " if len(actions) > 1 else "") + actions[-1] + ".")
        _print_next_steps(out, payload["next_steps"])
        return

    if skip_ingest:
        payload["next_steps"] = _next_steps("skipped", scans)
        if as_json:
            emit_json(payload)
            return
        out.line()
        out.line("The logs were not read (--no-ingest). Run agentfare ingest when you are ready.")
        _print_next_steps(out, payload["next_steps"])
        return

    store = st.store(ingest_first=False)
    rep = st.run_ingest(store)
    _print_ingest_notes(st, rep)
    held = store.count()
    outcome = _outcome(st, cfg, rep, held, utf8=as_json)
    payload["ingest"] = outcome.ingest
    payload["result"] = outcome.payload if held else _empty_payload()
    payload["next_steps"] = _next_steps("found" if held else "empty", scans)
    if as_json:
        emit_json(payload)
        return
    out.line()
    if rep.files_seen:
        out.line(f"Read {count_text(rep.files_seen, 'log file')}: {rep.files_parsed:,} new or changed, {rep.files_unchanged:,} already up to date.")
    if rep.files_failed:
        out.line(f"{plural(rep.files_failed, 'file')} could not be read (the warnings above say which).", "yellow")
    if not held:
        _print_empty(out, cfg)
    else:
        for line in outcome.lines:
            out.line(line)
        print_warnings(st.out, outcome.warnings)
    _print_next_steps(out, payload["next_steps"])


def register(app: typer.Typer) -> None:
    @app.command("init", help=HELP)
    def init_cmd(
        ctx: typer.Context,
        dry_run: Annotated[bool, typer.Option("--dry-run", help="Only look: list the folders and what is in them, change nothing.")] = False,
        write_config: Annotated[bool, typer.Option("--write-config", help="Also write ~/.agentfare/config.yaml with every default (an existing file is never overwritten).")] = False,
        no_ingest: Annotated[bool, typer.Option("--no-ingest", help="Create the data directory but do not read the logs yet.")] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Print the detection and the result as JSON, stable keys.")] = False,
    ) -> None:
        st: AppState = ctx.obj
        try:
            _setup(st, dry_run=dry_run, write_config=write_config, skip_ingest=no_ingest or not st.auto_ingest, as_json=as_json)
        except SETUP_ERRORS as exc:
            print_error(exc)
            st.err.print("[dim]agentfare doctor checks the whole setup and says how to fix what is wrong[/dim]")
            raise typer.Exit(2) from exc
