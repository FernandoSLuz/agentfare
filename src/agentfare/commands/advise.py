"""`agentfare advise`: where a cheaper model would have done the job, session by session and week by week."""

from __future__ import annotations

import textwrap
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.text import Text

from ..aggregate import priced_rows
from ..config import ConfigError, day_of
from ..models import short_id
from ..money import UCENTS_PER_CENT, format_usd
from ..pricing import PriceTable
from ..render import (
    AgentOpt, Col, Filters, JsonOpt, ProjectOpt, SinceOpt, UntilOpt, WeekOfOpt, WeekOpt, clean_errors, div_round,
    emit_json, money_json, percent, plain, plural, price_warnings, print_empty, print_table, print_title, print_warnings,
    resolve_filters, share_bp, usd,
)
from ..router import (
    Advice, WeekSavings, advise_sessions, format_advice_line, min_cost_ucents, savings_split, total_savings, weekly_savings,
)
from ..state import AppState

DEFAULT_LIMIT = 15
CAVEAT = (
    "Each estimate re-prices the same token counts on the recommended model. Tokenizers and caching differ and "
    "answer quality is not measured, so read it as a hint, not a promise."
)
LEGEND = "Sess fully priced sessions, Switched sessions with an advised switch, Router the same spend with every advised switch applied"


class Folded:
    """Sessions that are counted in the totals but not listed one by one."""

    def __init__(self, members: list[Advice]):
        self.sessions = len(members)
        self.actual_ucents = sum(a.actual_ucents for a in members if a.actual_complete)
        self.switchable = sum(a.switch for a in members)
        self.saved_ucents = sum(a.saved_ucents for a in members)


class View:
    """What the default view lists (top sessions with an advised switch) and what it folds into summary lines."""

    def __init__(self, listed: list[Advice], candidates: int, background: Folded, small: Folded, no_switch: Folded):
        self.listed, self.candidates = listed, candidates
        self.background, self.small, self.no_switch = background, small, no_switch

    @classmethod
    def build(cls, advice: list[Advice], min_cost: int, everything: bool, limit: int) -> "View":
        """``everything`` lists every session; otherwise only the sessions a person drove, that cost at least
        ``min_cost`` and have an advised switch, and the rest is summarised."""
        background = [a for a in advice if a.session.prompts == 0]
        small = [a for a in advice if a.session.prompts > 0 and a.actual_complete and a.actual_ucents < min_cost]
        regular = [a for a in advice if a.session.prompts > 0 and not (a.actual_complete and a.actual_ucents < min_cost)]
        pool = advice if everything else [a for a in regular if a.switch]
        pool = sorted(pool, key=lambda a: (-a.saved_ucents, -a.actual_ucents, a.session.key))
        if everything:
            return cls(pool, len(pool), Folded([]), Folded([]), Folded([]))
        return cls(pool[:limit], len(pool), Folded(background), Folded(small), Folded([a for a in regular if not a.switch]))


# ---- the report: one dict that both the text and --json are drawn from ---------------------------
def _other_provider(a: Advice, table: PriceTable) -> bool:
    """The advised model is another provider's than the session's (providers come from the price table): the comparison
    is about prices, the agent that ran the session cannot run it."""
    target = table.get(a.recommended_model) if a.switch and a.recommended_model else None
    used = {price.provider for model in a.session.usage if (price := table.get(model)) and price.provider}
    return bool(target and target.provider and used and target.provider not in used)


def _session_entry(a: Advice, st: AppState, encoding: str) -> dict[str, Any]:
    s = a.session
    ratio = div_round(a.actual_ucents * 100, a.recommended_ucents) / 100 if a.switch and a.recommended_ucents else None
    return {
        "key": s.key, "agent": s.agent, "session_id": s.session_id, "short_id": short_id(s.session_id),
        "project": s.project, "started_at": s.started_at, "day": day_of(s.started_at, st.cfg.tz), "prompts": s.prompts,
        "clipped": s.clipped, "model": a.model, "models": sorted(s.usage),
        "tier": a.scored.tier, "score": a.scored.score, "points": a.scored.points, "signals": a.scored.signals,
        "explanation": a.scored.explain(),
        "actual_complete": a.actual_complete, "actual": money_json(a.actual_ucents),
        "switch": a.switch, "recommended_model": a.recommended_model,
        "recommended": None if a.recommended_ucents is None else money_json(a.recommended_ucents),
        "saved": money_json(a.saved_ucents), "ratio": ratio, "note": a.note,
        "other_provider": _other_provider(a, st.table), "line": format_advice_line(a, encoding), "other_models": a.other_models,
    }


def _week_entry(w: WeekSavings) -> dict[str, Any]:
    return {
        "week": w.week, "sessions": w.sessions, "switched": w.switched, "skipped": w.skipped,
        "actual": money_json(w.actual_ucents), "recommended": money_json(w.recommended_ucents),
        "saved": money_json(w.saved_ucents), "saved_bp": share_bp(w.saved_ucents, w.actual_ucents) if w.actual_ucents else None,
    }


def _folded_entry(g: Folded) -> dict[str, Any]:
    return {"sessions": g.sessions, "switchable": g.switchable, "actual": money_json(g.actual_ucents), "saved": money_json(g.saved_ucents)}


def build_report(
    st: AppState, filters: Filters, advice: list[Advice], view: View, min_cost: int, encoding: str = "utf-8",
) -> dict[str, Any]:
    """The report both outputs are drawn from. ``encoding`` is the one of the console the text goes to, which decides how
    the signature lines draw their arrow; ``--json`` escapes it, so it always has the ``→``."""
    weeks = weekly_savings(advice, st.cfg)
    prompted, background = savings_split(advice)
    return {
        **filters.as_json(),
        "counts": {"sessions": len(advice), "listed": len(view.listed), "candidates": view.candidates},
        "sessions": [_session_entry(a, st, encoding) for a in view.listed],
        "not_listed": {
            "background": _folded_entry(view.background),
            "small": {**_folded_entry(view.small), "below": money_json(min_cost)},
            "no_switch": _folded_entry(view.no_switch),
        },
        "weeks": [_week_entry(w) for w in weeks],
        "total": _week_entry(total_savings(weeks)),
        "saved_split": {"prompted": money_json(prompted), "background": money_json(background)},
        "warnings": price_warnings(priced_rows([a.session for a in advice], st.table, st.cfg.tz), st.table, st.cfg),
        "caveat": CAVEAT,
    }


# ---- text -----------------------------------------------------------------------------
def _emit(console: Console, text: str, style: str = "") -> None:
    """One line, verbatim: Rich neither wraps, pads nor reads markup in it, and a character the console cannot write
    (a project name from the logs) shows as ``?`` instead of stopping the command."""
    console.print(Text(plain(text, console), style=style), soft_wrap=True)


def _wrapped(console: Console, text: str, style: str = "", indent: str = "") -> None:
    """``text`` wrapped to the console width with a hanging ``indent`` (whole words: ``gpt-6.1-sol`` never splits)."""
    for line in textwrap.wrap(text, max(console.width, 40), subsequent_indent=indent, break_on_hyphens=False):
        _emit(console, line, style)


def _folded_lines(report: dict[str, Any]) -> list[str]:
    background, small, no_switch = (report["not_listed"][k] for k in ("background", "small", "no_switch"))
    tail = "included in the totals, --all lists them."
    lines = []
    if background["sessions"]:
        what = plural(background["sessions"], "sub-agent or background session")
        lines.append(
            f"{what} with 0 prompts: {usd(background['actual']['ucents'])} spent, "
            f"{background['switchable']} switchable for {usd(background['saved']['ucents'])}; {tail}"
        )
    if small["sessions"]:
        lines.append(
            f"{plural(small['sessions'], 'session')} under {usd(small['below']['ucents'])}: {usd(small['actual']['ucents'])} spent, "
            f"{small['switchable']} switchable for {usd(small['saved']['ucents'])}; {tail}"
        )
    if no_switch["sessions"]:
        lines.append(
            f"{plural(no_switch['sessions'], 'more session')} where no switch is advised (heavy, already cheaper, no verified price): "
            f"{usd(no_switch['actual']['ucents'])} spent; --all lists them."
        )
    return lines


# On a terminal too narrow for the table the counts go first: a dollar amount is never cut.
_COLS = [Col("Week"), *(Col(name, True, optional=True) for name in ("Sess", "Switched")), *(Col(name, True) for name in ("Actual", "Router", "Saved", "Saved %"))]


def _week_cells(w: dict[str, Any]) -> list[str]:
    """One precision per row: four decimals on all three amounts when any of them is not a whole cent, else the compact one."""
    amounts = [w[key]["ucents"] for key in ("actual", "recommended", "saved")]
    precise = any(amount % UCENTS_PER_CENT for amount in amounts)
    money = [format_usd(amount, 4) if precise else usd(amount) for amount in amounts]
    return [w["week"], str(w["sessions"]), str(w["switched"]), *money,
            percent(w["saved"]["ucents"], w["actual"]["ucents"]) if w["actual"]["ucents"] else "-"]


def _detail(s: dict[str, Any]) -> str:
    """The dim line under a listed session: where it ran, why it scored as it did, and what the comparison is."""
    part = " (the part inside this period)" if s["clipped"] else ""
    text = f"{s['agent']}, {s['project']}, {s['day']}{part}: {s['explanation']}"
    if s["other_provider"]:
        text += f". {s['recommended_model']} is another provider's model: a price comparison, not something this agent can run"
    return text


def _split_line(report: dict[str, Any]) -> str | None:
    """How much of the saving comes from sessions a person prompted and how much from sub-agent and background threads."""
    prompted, background = (report["saved_split"][k]["ucents"] for k in ("prompted", "background"))
    whole = prompted + background
    if not whole:
        return None
    return (
        f"Of the saving, {usd(prompted)} ({percent(prompted, whole)}) comes from sessions with prompts and "
        f"{usd(background)} ({percent(background, whole)}) from sub-agent or background threads with 0 prompts."
    )


def print_advice(console: Console, report: dict[str, Any], filters: Filters) -> None:
    total = report["total"]
    _emit(console, f"AgentFare advise: {filters.scope()}", "bold")
    _emit(console, f"{plural(total['sessions'] + total['skipped'], 'session')}, {total['switched']} with a switch advised")
    _emit(console, "")
    if not report["sessions"]:
        _emit(console, "No session with an advised switch in this view.")
    digits = len(str(len(report["sessions"])))
    lead = " " * (digits + 2)
    for rank, s in enumerate(report["sessions"], 1):
        _wrapped(console, f"{rank:>{digits}}. {s['line']}", indent=lead)
        _wrapped(console, f"{lead}{_detail(s)}", "dim", lead)
    counts = report["counts"]
    if counts["candidates"] > counts["listed"]:
        _wrapped(console, f"Showing {counts['listed']} of {counts['candidates']}; --limit N shows more, --all lists everything.")
    for line in _folded_lines(report):
        _wrapped(console, line)
    print_title(console, "Router savings (per ISO week)")
    print_table(console, _COLS, [_week_cells(w) for w in report["weeks"]], _week_cells({**total, "week": "Total"}))
    if split := _split_line(report):
        _wrapped(console, split)
    _wrapped(console, LEGEND, "dim")
    if total["skipped"]:
        _wrapped(console, f"{plural(total['skipped'], 'session')} with an unverified price left out of these totals and got no advice.")
    print_warnings(console, report["warnings"])
    _emit(console, "")
    _wrapped(console, f"Note: {report['caveat']}", "dim")


def register(app: typer.Typer) -> None:
    @app.command("advise")
    def advise_cmd(
        ctx: typer.Context,
        week: WeekOpt = False,
        week_of: WeekOfOpt = None,
        since: SinceOpt = None,
        until: UntilOpt = None,
        project: ProjectOpt = None,
        agent: AgentOpt = None,
        limit: Annotated[int, typer.Option("--limit", "-n", metavar="N", help="How many sessions to list.")] = DEFAULT_LIMIT,
        show_all: Annotated[bool, typer.Option("--all", help="List every session, folded ones too (overrides --limit).")] = False,
        as_json: JsonOpt = False,
    ) -> None:
        """Where a cheaper model would likely have done the job, with the reason for each call.

        Each session is scored from five visible signals (docs/ROUTING.md); its tokens are re-priced on the model recommended for its tier.

        Sessions with an unverified price stay out of the totals.
        """
        st: AppState = ctx.obj
        with clean_errors():
            if limit < 1 and not show_all:
                raise ConfigError(f"--limit must be at least 1, got {limit}")
            filters = resolve_filters(st.cfg, week=week, week_of=week_of, since=since, until=until, project=project, agent=agent)
            period = filters.period
            store = st.store()
            sessions = store.sessions(agent=filters.agent, project=filters.project, since=period.since, until=period.until)
            min_cost = min_cost_ucents(st.cfg)
            advice = advise_sessions(sessions, st.cfg, st.table, store.whole_usage(s.key for s in sessions if s.clipped))
            view = View.build(advice, min_cost, show_all, limit)
            report = build_report(st, filters, advice, view, min_cost, "utf-8" if as_json else st.out.encoding)
            if as_json:
                emit_json(report)
            elif not sessions:
                print_empty(st, filters)
            else:
                print_advice(st.out, report, filters)
