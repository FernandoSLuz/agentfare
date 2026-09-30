"""`agentfare summary`: spend by model, project, day (and week) for a period."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any

import typer
from rich.console import Console

from ..aggregate import Group, Row, group_rows
from ..config import ConfigError
from ..pricing import PriceTable
from ..render import (
    MODEL_MIN, AgentOpt, Col, Filters, JsonOpt, ProjectOpt, SinceOpt, UntilOpt, WeekOfOpt, WeekOpt, Whole, bar, clean_errors,
    cost_cells, emit_json, glyph, group_entry, leaderboard_key, load, money_json, plural, print_empty,
    print_period_note, print_plus_legend, print_table, print_title, print_warnings, resolve_filters, say, share_bp,
    share_cell, spend_text, tokens, usd, warnings_payload,
)

BAR_WIDTH = 10
DEFAULT_TOP = 10
TOKEN_KEYS = ("input", "cache_read", "cache_write", "output", "total")
# On a narrow console the bar goes first, then the columns whose loss says least (the "optional" order), the session count and the output tokens last.
_USAGE_COLS = [Col("Sess", True, optional=6), Col("Input", True, optional=5), Col("Cache\nread", True, optional=4),
               Col("Cache\nwrite", True, optional=2), Col("Output", True, optional=7), Col("Cost", True), Col("Share", True, optional=3)]


def build_report(rows: Sequence[Row], table: PriceTable, filters: Filters) -> dict[str, Any]:
    """Every figure of the summary as integers; the human tables and ``--json`` both come from this."""
    grand = (group_rows(rows, lambda r: "") or [Group(key="")])[0]
    spend = grand.ucents

    def entries(key: Callable[[Row], str], name: str, order: Callable[[Group], Any] | None = None) -> list[dict[str, Any]]:
        groups = group_rows(rows, key)
        return [{name: g.key, **group_entry(g, spend)} for g in (sorted(groups, key=order) if order else groups)]

    by_model = []
    for g in sorted(group_rows(rows, lambda r: f"{r.session.agent}/{r.canonical}"), key=leaderboard_key):
        agent, _, model = g.key.partition("/")
        known = table.get(model)
        by_model.append({"agent": agent, "model": model, "provider": known.provider if known else "",
                         "status": table.status(model), **group_entry(g, spend)})
    return {
        **filters.as_json(),
        "sessions": grand.session_count,
        "projects": len({r.session.project for r in rows}),
        "models": len({r.canonical for r in rows}),
        "tokens": group_entry(grand, spend)["tokens"],
        "total": money_json(spend),
        "excluded_tokens": grand.unpriced_tokens,
        "by_model": by_model,
        "by_project": entries(lambda r: r.session.project, "project"),
        "by_day": entries(lambda r: r.day, "day", order=lambda g: g.key),
        "by_week": entries(lambda r: r.week, "week", order=lambda g: g.key),
        "warnings": warnings_payload(rows, table),
    }


def _total_entry(report: dict[str, Any]) -> dict[str, Any]:
    """The report's grand total shaped like one breakdown row, so it prints through the same code."""
    priced = report["excluded_tokens"] < report["tokens"]["total"]
    return {
        "sessions": report["sessions"], "tokens": report["tokens"], "cost": report["total"] if priced else None,
        "share_bp": 10_000 if report["total"]["ucents"] else None, "unpriced_tokens": report["excluded_tokens"],
    }


def _rest(entries: Sequence[dict[str, Any]], spend: int) -> dict[str, Any]:
    """The rows left out by ``--top``, added up into one, so the table still cross-foots. ``label`` says how many and how much."""
    priced = [e["cost"]["ucents"] for e in entries if e["cost"]]
    unpriced = sum(e["unpriced_tokens"] for e in entries)
    merged = {
        "sessions": sum(e["sessions"] for e in entries),
        "tokens": {k: sum(e["tokens"][k] for e in entries) for k in TOKEN_KEYS},
        "cost": money_json(sum(priced)) if priced else None,
        "share_bp": share_bp(sum(priced), spend) if priced else None,
        "unpriced_tokens": unpriced,
    }
    worth = f" (total {usd(sum(priced))}{'+' if unpriced else ''})" if priced else ""
    return {**merged, "label": f"{len(entries)} more{worth}"}


def _top(entries: Sequence[dict[str, Any]], top: int, spend: int, *, newest: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The ``top`` first entries (the last ones with ``newest``) and the entry that stands for all the others, if any.

    Nothing is folded unless it hides at least two rows: one row in the place of one row would only hide it."""
    if not top or len(entries) <= top + 1:
        return list(entries), None
    shown, hidden = (entries[-top:], entries[:-top]) if newest else (entries[:top], entries[top:])
    return list(shown), _rest(hidden, spend)


def _breakdown(
    console: Console, title: str, lead: Sequence[Col], keys: Sequence[Sequence[str]], entries: Sequence[dict[str, Any]],
    total: dict[str, Any], *, bars: bool = False, rest: dict[str, Any] | None = None, note: str = "",
) -> None:
    """One table: key columns, the token buckets, cost and share, then a bold total row (and a bar per row).

    ``rest`` is the last row, the entries ``--top`` left out; ``note`` is a dim line under the table."""
    everything = [*entries, *([rest] if rest else [])]
    costs = cost_cells([*everything, total])
    peak = max((e["cost"]["ucents"] for e in entries if e["cost"]), default=0)
    spend = total["cost"]["ucents"] if total["cost"] else 0

    def cells(e: dict[str, Any], cost: str) -> list[str]:
        t = e["tokens"]
        return [str(e["sessions"]), *(tokens(t[k]) for k in ("input", "cache_read", "cache_write", "output")),
                cost, share_cell(e, spend)]

    def bar_cell(e: dict[str, Any]) -> list[str]:
        if not bars:
            return []
        return [bar(e["cost"]["ucents"] if e["cost"] and e is not rest else 0, peak, BAR_WIDTH, glyph(console, "█", "#"))]

    labels = [*keys, *([[Whole(rest["label"]), *[""] * (len(lead) - 1)]] if rest else [])]
    rows = [[*key, *cells(e, cost), *bar_cell(e)] for key, e, cost in zip(labels, everything, costs)]
    footer = ["Total", *[""] * (len(lead) - 1), *cells(total, costs[-1]), *([""] if bars else [])]
    print_title(console, title)
    print_table(console, [*lead, *_USAGE_COLS, *([Col("", optional=1, label="bar")] if bars else [])], rows, footer)
    if note:
        say(console, note, "dim")


def _case_note(entries: Sequence[dict[str, Any]]) -> str:
    """The note under the project table when names that differ only by case are listed apart (``--project`` ignores case)."""
    names: dict[str, list[str]] = {}
    for e in entries:
        names.setdefault(e["project"].casefold(), []).append(e["project"])
    twins = sorted(name for group in names.values() if len(group) > 1 for name in group)
    return f"names that differ only by case are listed apart ({', '.join(twins)}); --project ignores case and finds them all" if twins else ""


def _several(entries: Sequence[dict[str, Any]], total: dict[str, Any], what: str, per: str) -> str:
    """The note under a table whose rows count a session more than once, when they do: ``what`` spans several ``per``."""
    if sum(e["sessions"] for e in entries) <= total["sessions"]:
        return ""
    return f"a session that {what} counts once per {per}; the Total row counts it once"


def print_summary(console: Console, report: dict[str, Any], filters: Filters, top: int = DEFAULT_TOP) -> None:
    total = _total_entry(report)
    spend = report["total"]["ucents"]
    headline = spend_text(report["total"], report["excluded_tokens"], report["tokens"]["total"])
    say(console, f"AgentFare summary: {filters.scope()}", "bold")
    print_period_note(console, filters)
    say(console, ", ".join([plural(report["sessions"], "session"), plural(report["projects"], "project"),
                            plural(report["models"], "model"), f"total spend {headline}"]))
    by_model, by_project, by_day, by_week = (report[k] for k in ("by_model", "by_project", "by_day", "by_week"))
    projects, more_projects = _top(by_project, top, spend)
    days, more_days = _top(by_day, top, spend, newest=True)
    _breakdown(console, "Spend by model", [Col("Agent"), Col("Model", min_width=MODEL_MIN)],
               [[m["agent"], m["model"]] for m in by_model], by_model, total,
               note=_several(by_model, total, "used several models", "model"))
    _breakdown(console, f"Spend by project (top {top} by spend)" if more_projects else "Spend by project",
               [Col("Project", min_width=8)], [[p["project"]] for p in projects], projects, total, rest=more_projects,
               note=_case_note(by_project))
    _breakdown(console, f"Spend by day (latest {top} {'day' if top == 1 else 'days'})" if more_days else "Spend by day",
               [Col("Day")], [[d["day"]] for d in days], days, total, bars=True, rest=more_days,
               note=_several(by_day, total, "spans several days", "day"))
    if len(by_week) > 1:
        _breakdown(console, "Spend by ISO week", [Col("Week")], [[w["week"]] for w in by_week], by_week, total,
                   note=_several(by_week, total, "spans several weeks", "week"))
    print_plus_legend(console, [*by_project, *by_day, *by_week, total])
    print_warnings(console, report["warnings"])


def register(app: typer.Typer) -> None:
    @app.command("summary")
    def summary_cmd(
        ctx: typer.Context,
        week: WeekOpt = False,
        week_of: WeekOfOpt = None,
        since: SinceOpt = None,
        until: UntilOpt = None,
        project: ProjectOpt = None,
        agent: AgentOpt = None,
        top: Annotated[int, typer.Option("--top", metavar="N", help="Rows in the project table (top by spend) and the day table (latest days); the rest in one row when it would hide 2 or more (0: all). --json lists everything.")] = DEFAULT_TOP,
        as_json: JsonOpt = False,
    ) -> None:
        """Spend by model, project and day, with token totals. Unpriced models are excluded and named."""
        st = ctx.obj
        with clean_errors():
            if top < 0:
                raise ConfigError(f"--top must be 0 or more, got {top}")
            filters = resolve_filters(st.cfg, week=week, week_of=week_of, since=since, until=until, project=project, agent=agent)
            rows = load(st, filters)
            report = build_report(rows, st.table, filters)
            if as_json:
                emit_json(report)
            elif not rows:
                print_empty(st, filters)
            else:
                print_summary(st.out, report, filters, top)
