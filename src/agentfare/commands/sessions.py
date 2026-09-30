"""`agentfare sessions`: one line per session, newest (or dearest) first."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

import typer
from rich.console import Console

from ..aggregate import Group, Row, group_rows
from ..config import ConfigError
from ..models import short_id
from ..pricing import PriceTable
from ..render import (
    MODEL_MIN, AgentOpt, Col, Filters, JsonOpt, ProjectOpt, SinceOpt, UntilOpt, WeekOfOpt, WeekOpt, clean_errors, cost_cells,
    cost_json, duration, emit_json, leaderboard_key, load, money_json, plural, print_empty, print_plus_legend,
    print_table, print_warnings, resolve_filters, say, spend_text, tokens, tokens_json, warnings_payload,
)

DEFAULT_LIMIT = 30
SORTS = ("date", "cost")
COUNTERS = {"Prm": "prompts", "Tool": "tool calls", "File": "files touched", "Err": "tool errors"}


def _entry(rows: Sequence[Row]) -> dict[str, Any]:
    """One session from its rows (one per model and slot): what was spent in the view; the counters are the whole session's."""
    s = rows[0].session
    models = sorted(group_rows(rows, lambda r: r.canonical), key=leaderboard_key)
    whole = group_rows(rows, lambda r: "")[0]
    # the model that wrote the most, among those the view holds: a session cut at the period's edge may have spent the rest elsewhere
    primary = next((r.canonical for r in rows if r.model == s.primary_model), None)
    primary = primary or max(models, key=lambda g: (g.usage.output, g.usage.total, g.key)).key
    return {
        "date": min(r.day for r in rows),  # the first day of spend in the view, which is the start of a session that is not cut
        "started_at": s.started_at,
        "agent": s.agent,
        "session_id": s.session_id,
        "short_id": short_id(s.session_id),
        "project": s.project,
        "primary_model": primary,
        "other_models": len(models) - 1,
        "models": [{"model": g.key, "tokens": tokens_json(g.usage), "cost": cost_json(g)} for g in models],
        "active_seconds": s.active_seconds,
        "wall_seconds": s.wall_seconds,
        "prompts": s.prompts,
        "tool_calls": s.tool_calls,
        "files_touched": s.files_touched,
        "error_retries": s.error_retries,
        "turns": s.turns,
        "tokens": tokens_json(whole.usage),
        "cost": cost_json(whole),
        "unpriced_tokens": whole.unpriced_tokens,
        "clipped": s.clipped,
    }


def build_report(
    rows: Sequence[Row], table: PriceTable, filters: Filters, *, limit: int | None, sort: str,
) -> dict[str, Any]:
    """``count`` sessions match, ``limit`` of them (all when None) are listed in ``sessions``; ``tokens``, ``total``
    and ``excluded_tokens`` cover all of them."""
    grand = (group_rows(rows, lambda r: "") or [Group(key="")])[0]
    by_session: dict[str, list[Row]] = {}
    for r in rows:
        by_session.setdefault(r.session.key, []).append(r)
    entries = sorted((_entry(rs) for rs in by_session.values()),
                     key=lambda e: (e["started_at"], e["agent"], e["session_id"]), reverse=True)
    if sort == "cost":  # stable: sessions of equal cost stay newest first
        entries.sort(key=lambda e: -(e["cost"]["ucents"] if e["cost"] else 0))
    shown = entries[:limit]
    return {
        **filters.as_json(),
        "count": len(entries),
        "shown": len(shown),
        "tokens": tokens_json(grand.usage),
        "total": money_json(grand.ucents),
        "excluded_tokens": grand.unpriced_tokens,
        "sessions": shown,
        "warnings": warnings_payload(rows, table),
    }


def _footer(report: dict[str, Any]) -> str:
    """``showing 3 of 3 sessions, total $3.63``; when the list is cut, ``showing 30 of 337 sessions ($36.30 of $128.40)``."""
    shown, count = report["sessions"], plural(report["count"], "session")
    if report["shown"] == report["count"]:
        return f"showing {report['shown']} of {count}, total {spend_text(report['total'], report['excluded_tokens'], report['tokens']['total'])}"
    part = spend_text(money_json(sum(e["cost"]["ucents"] for e in shown if e["cost"])), sum(e["unpriced_tokens"] for e in shown),
                      sum(e["tokens"]["total"] for e in shown), "n/a")
    whole = spend_text(report["total"], report["excluded_tokens"], report["tokens"]["total"], "n/a")
    return f"showing {report['shown']} of {count} ({part} of {whole})"


def print_sessions(console: Console, report: dict[str, Any], filters: Filters, sort: str = "date") -> None:
    shown = report["sessions"]
    same_year = len({e["date"][:4] for e in shown}) == 1  # then the year is noise
    extra = [f"+{e['other_models']}" if e["other_models"] else "" for e in shown]
    clipped = any(e["clipped"] for e in shown)
    # On a narrow console the tokens go first, then the counters (the "optional" order), then the active time, then the id
    # (it is how a session named by `agentfare advise` is found here) and, only when the model names would otherwise run
    # together or past the edge, the project: the model has the width before the project does.
    cols = [Col("Date"), Col("Agent"), Col("ID", optional=7), Col("Project", min_width=7, optional=8),
            Col("Model", min_width=MODEL_MIN, cut_last=True), *([Col("")] if any(extra) else []),
            Col("Active", True, optional=6), Col("Prm", True, optional=5), Col("Tool", True, optional=4),
            Col("File", True, optional=3), Col("Err", True, optional=2), Col("Tokens", True, optional=1), Col("Cost", True)]
    costs = cost_cells(shown)
    if clipped:  # a star after the cost of a session that was cut at the edge of the period
        costs = [cost + ("*" if e["clipped"] else " ") for cost, e in zip(costs, shown)]
    rows = []
    for e, more, cost in zip(shown, extra, costs):
        rows.append([
            e["date"][5:] if same_year else e["date"], e["agent"], e["short_id"], e["project"], e["primary_model"],
            *([more] if any(extra) else []), duration(e["active_seconds"]), str(e["prompts"]), str(e["tool_calls"]),
            str(e["files_touched"]), str(e["error_retries"]), tokens(e["tokens"]["total"]), cost,
        ])
    order = "; sorted by cost, dearest first" if sort == "cost" else ""
    say(console, f"AgentFare sessions: {filters.scope()}{order}", "bold")
    console.print()
    drawn = print_table(console, cols, rows)
    if legend := ", ".join(f"{name} {meaning}" for name, meaning in COUNTERS.items() if name in drawn):
        say(console, legend, "dim")
    print_plus_legend(console, shown, total=bool(report["excluded_tokens"]))
    if clipped:
        say(console, "* sessions that span the period edge show only what was spent inside it", "dim")
    say(console, _footer(report), "bold")
    print_warnings(console, report["warnings"])


def register(app: typer.Typer) -> None:
    @app.command("sessions")
    def sessions_cmd(
        ctx: typer.Context,
        week: WeekOpt = False,
        week_of: WeekOfOpt = None,
        since: SinceOpt = None,
        until: UntilOpt = None,
        project: ProjectOpt = None,
        agent: AgentOpt = None,
        limit: Annotated[int, typer.Option("--limit", "-n", metavar="N", help="How many sessions to list.")] = DEFAULT_LIMIT,
        show_all: Annotated[bool, typer.Option("--all", help="List every session (overrides --limit).")] = False,
        sort: Annotated[str, typer.Option("--sort", metavar="date|cost", help="Newest first, or dearest first.")] = "date",
        as_json: JsonOpt = False,
    ) -> None:
        """Per-session breakdown: agent, project, model, time, activity, tokens and cost."""
        st = ctx.obj
        with clean_errors():
            if sort not in SORTS:
                raise ConfigError(f"unknown --sort {sort!r}: expected {' or '.join(SORTS)}")
            if limit < 1 and not show_all:
                raise ConfigError(f"--limit must be at least 1, got {limit}")
            filters = resolve_filters(st.cfg, week=week, week_of=week_of, since=since, until=until, project=project, agent=agent)
            rows = load(st, filters)
            report = build_report(rows, st.table, filters, limit=None if show_all else limit, sort=sort)
            if as_json:
                emit_json(report)
            elif not rows:
                print_empty(st, filters)
            else:
                print_sessions(st.out, report, filters, sort)
