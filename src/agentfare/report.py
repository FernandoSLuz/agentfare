"""Weekly report data: one ISO week of sessions, priced, then run through the router. No rendering here.

The week holds the money spent inside it (``Store.sessions`` clips each session to the week's slots), so a session that
spans two weeks shows up in both with only its own share. Every figure comes from the same code as ``summary`` (spend:
``aggregate.priced_rows``) and ``advise`` (savings: ``router.advise_sessions`` and ``weekly_savings``), so the report
cannot disagree with them. Project names never leave this module unless the caller asks for them: they are replaced by
``Project A``, ``Project B``, ... here, so no renderer can leak one by accident.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta, tzinfo
from typing import Any

from . import __version__
from .aggregate import Group, Row, group_rows, period_for, priced_rows, total_ucents
from .config import Config, ConfigError, day_of, fmt_iso, iso_week_of, now_utc, week_bounds
from .db import Store, StoredSession
from .models import Usage, short_id
from .pricing import PriceTable
from .render import clean_text, div_round, leaderboard_key, money_json, price_warnings, share_bp, tokens_json
from .router import TIERS, Advice, WeekSavings, advise_sessions, savings_split, weekly_savings

TOP_SESSIONS = 5  # sessions listed under "biggest savings"


@dataclass(frozen=True)
class Slice:
    """One bar of a breakdown: what a model, an agent or a day cost."""

    key: str
    sessions: int
    usage: Usage
    ucents: int
    unpriced_tokens: int  # tokens of this slice without a verified price; ``ucents`` leaves them out

    @property
    def priced(self) -> bool:
        """False only when tokens were left out and none was priced: a day without sessions costs a true zero."""
        return not (self.unpriced_tokens and self.unpriced_tokens >= self.usage.total)


@dataclass(frozen=True)
class TierMix:
    tier: str
    sessions: int
    ucents: int  # priced part of what these sessions cost
    unpriced_sessions: int  # sessions of the tier that also used a model without a verified price


@dataclass(frozen=True)
class Opportunity:
    """A session the router would have moved to a cheaper model."""

    short_id: str
    agent: str
    project: str  # already an alias unless the report shows project names
    model: str
    recommended_model: str
    tier: str
    actual_ucents: int
    router_ucents: int

    @property
    def saved_ucents(self) -> int:
        return self.actual_ucents - self.router_ucents


@dataclass(frozen=True)
class Previous:
    """The week before, for the week-over-week line: only its priced spend matters."""

    week: str
    sessions: int
    ucents: int


@dataclass(frozen=True)
class WeekReport:
    week: str  # ISO week label "2026-W40"
    first_day: str
    last_day: str
    generated: str  # local date the report was made
    show_projects: bool
    sessions: int
    projects: int
    agents: int
    models: int
    usage: Usage
    spend_ucents: int  # priced spend, exactly what ``summary`` totals for this week
    excluded_tokens: int  # tokens without a verified price, left out of every dollar figure
    savings: WeekSavings  # exactly what ``advise`` prints for this week
    background_sessions: int  # sessions nobody prompted (sub-agent and auto-review threads): counted, not listed
    background_saved_ucents: int
    prompted_sessions: int  # sessions with at least one prompt: the ones the person can route by hand
    prompted_actual_ucents: int  # what the fully priced ones of them cost, so the saving has a denominator of its own
    prompted_saved_ucents: int
    by_model: list[Slice]
    by_agent: list[Slice]
    by_day: list[Slice]
    tiers: list[TierMix]
    top: list[Opportunity]
    previous: Previous | None
    warnings: list[dict[str, Any]]

    @property
    def in_progress(self) -> bool:
        """True while the week is still running: its spend is not comparable with a whole week's."""
        return self.first_day <= self.generated <= self.last_day

    @property
    def nothing_priced(self) -> bool:
        return self.excluded_tokens > 0 and self.excluded_tokens >= self.usage.total

    @property
    def state(self) -> str:
        """``empty`` (no sessions), ``unpriced`` (no dollar total), ``none`` (no advised switch) or ``savings``."""
        if not self.sessions:
            return "empty"
        if self.nothing_priced:
            return "unpriced"
        return "savings" if self.savings.saved_ucents > 0 else "none"

    @property
    def change_bp(self) -> int | None:
        """Spend against the week before in signed basis points; None without a priced week before."""
        if self.previous is None or self.previous.ucents <= 0 or self.nothing_priced:
            return None
        delta = self.spend_ucents - self.previous.ucents
        bp = div_round(abs(delta) * 10_000, self.previous.ucents)
        return -bp if delta < 0 else bp


# ---- weeks ----------------------------------------------------------------------------
def canonical_week(week: str, tz: tzinfo) -> str:
    """``2026-w40`` -> ``2026-W40``; an invalid label is a ConfigError."""
    year, number, _ = date.fromisoformat(week_bounds(week, tz)[0]).isocalendar()
    return f"{year:04d}-W{number:02d}"


def shift_week(week: str, weeks: int, tz: tzinfo) -> str:
    """The ISO week ``weeks`` before (negative) or after ``week``."""
    try:
        monday = date.fromisoformat(week_bounds(week, tz)[0]) + timedelta(weeks=weeks)
    except OverflowError as exc:
        raise ConfigError(f"no ISO week {abs(weeks)} week(s) from {week}") from exc
    year, number, _ = monday.isocalendar()
    return f"{year:04d}-W{number:02d}"


def latest_week(store: Store, cfg: Config) -> str:
    """The most recent ISO week with spend at or before the current one; the current week when there is none.

    The week is the one of the latest slot with usage, not of a session's start: a session that began last week and is
    still spending belongs to this one."""
    current = iso_week_of(fmt_iso(now_utc()), cfg.tz)
    until = period_for(cfg, week=current).until
    slots = [slot for s in store.sessions(until=until) if s.usage for slot, _model in s.effective_slots()]
    return iso_week_of(max(slots), cfg.tz) if slots else current


def _week_sessions(cfg: Config, store: Store, week: str) -> list[StoredSession]:
    period = period_for(cfg, week=week)
    return [s for s in store.sessions(since=period.since, until=period.until) if s.usage]


# ---- assembly -------------------------------------------------------------------------
# Every name from the logs goes through ``clean_text`` before it reaches a page.
def _slice(g: Group) -> Slice:
    return Slice(clean_text(g.key), g.session_count, g.usage, g.ucents, g.unpriced_tokens)


def _slices(rows: list[Row], key: Callable[[Row], str], order: Callable[[Group], Any] | None = None) -> list[Slice]:
    groups = group_rows(rows, key)
    return [_slice(g) for g in (sorted(groups, key=order) if order else groups)]


def _letters(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA: spreadsheet-style, so there is never a shortage of aliases."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


def _project_labels(rows: list[Row], show: bool) -> dict[str, str]:
    """Project name -> what the report says. The biggest spender is Project A, so an alias does not depend on run order."""
    names = [g.key for g in group_rows(rows, lambda r: r.session.project)]
    return {name: clean_text(name) if show else f"Project {_letters(i)}" for i, name in enumerate(names)}


def _tier_mix(advice: list[Advice]) -> list[TierMix]:
    return [
        TierMix(
            tier, len(mine := [a for a in advice if a.scored.tier == tier]), sum(a.actual_ucents for a in mine),
            sum(not a.actual_complete for a in mine),
        )
        for tier in TIERS
    ]


def _opportunity(a: Advice, project: str) -> Opportunity:
    s = a.session
    return Opportunity(
        clean_text(short_id(s.session_id)), s.agent, project, clean_text(a.model), clean_text(a.recommended_model or ""), a.scored.tier,
        a.actual_ucents, a.actual_ucents - a.saved_ucents,
    )


def _warnings(cfg: Config, table: PriceTable, rows: list[Row]) -> list[dict[str, Any]]:
    """Every price problem behind a number on the page: the logged models and the models the router recommends."""
    return [{**w, "model": clean_text(w["model"]), "message": clean_text(w["message"])} for w in price_warnings(rows, table, cfg)]


def _previous(cfg: Config, store: Store, table: PriceTable, week: str) -> Previous | None:
    label = shift_week(week, -1, cfg.tz)
    rows = priced_rows(_week_sessions(cfg, store, label), table, cfg.tz)
    if not rows:
        return None
    return Previous(label, len({r.session.key for r in rows}), total_ucents(rows))


def build_week_report(
    cfg: Config, store: Store, table: PriceTable, *, week: str | None = None, show_projects: bool = False
) -> WeekReport:
    """The report for ``week`` (``YYYY-Www``), or for the latest week that has sessions."""
    tz = cfg.tz
    label = canonical_week(week, tz) if week else latest_week(store, cfg)
    first_day, last_day = week_bounds(label, tz)
    sessions = _week_sessions(cfg, store, label)
    rows = priced_rows(sessions, table, tz)
    advice = advise_sessions(sessions, cfg, table, store.whole_usage(s.key for s in sessions if s.clipped))  # tiers are whole-session, as in `advise`
    labels = _project_labels(rows, show_projects)
    grand = (group_rows(rows, lambda r: "") or [Group(key="")])[0]
    by_day = {g.key: _slice(g) for g in group_rows(rows, lambda r: r.day)}
    days = [(date.fromisoformat(first_day) + timedelta(days=i)).isoformat() for i in range(7)]
    background = [a for a in advice if a.session.prompts == 0]
    prompted = [a for a in advice if a.session.prompts > 0]
    prompted_saved, background_saved = savings_split(advice)
    # a slice of a session that cost less than the advised model would have is not a saving to list (the decision was the whole session's)
    listed = sorted((a for a in prompted if a.switch and a.saved_ucents > 0), key=lambda a: (-a.saved_ucents, -a.actual_ucents, a.session.key))
    return WeekReport(
        week=label, first_day=first_day, last_day=last_day, generated=day_of(fmt_iso(now_utc()), tz),
        show_projects=show_projects, sessions=grand.session_count, projects=len(labels),
        agents=len({r.session.agent for r in rows}), models=len({r.canonical for r in rows}),
        usage=grand.usage, spend_ucents=grand.ucents, excluded_tokens=grand.unpriced_tokens,
        savings=next((w for w in weekly_savings(advice, cfg) if w.week == label), WeekSavings(label, 0, 0, 0, 0)),
        background_sessions=len(background), background_saved_ucents=background_saved,
        prompted_sessions=len(prompted), prompted_actual_ucents=sum(a.actual_ucents for a in prompted if a.actual_complete),
        prompted_saved_ucents=prompted_saved,
        by_model=_slices(rows, lambda r: r.canonical, leaderboard_key),
        by_agent=_slices(rows, lambda r: r.session.agent),
        by_day=[by_day.get(d) or Slice(d, 0, Usage(), 0, 0) for d in days],
        tiers=_tier_mix(advice),
        top=[_opportunity(a, labels[a.session.project]) for a in listed[:TOP_SESSIONS]],
        previous=_previous(cfg, store, table, label), warnings=_warnings(cfg, table, rows),
    )


# ---- --json ---------------------------------------------------------------------------
def _slice_json(name: str, s: Slice, spend: int) -> dict[str, Any]:
    cost = money_json(s.ucents) if s.priced else None
    return {
        name: s.key, "sessions": s.sessions, "tokens": tokens_json(s.usage), "cost": cost,
        "share_bp": share_bp(s.ucents, spend) if cost else None, "unpriced_tokens": s.unpriced_tokens,
    }


def _router_json(w: WeekSavings) -> dict[str, Any]:
    """The keys of one ``weeks[]`` entry of ``advise --json``."""
    return {
        "week": w.week, "sessions": w.sessions, "switched": w.switched, "skipped": w.skipped,
        "actual": money_json(w.actual_ucents), "recommended": money_json(w.recommended_ucents),
        "saved": money_json(w.saved_ucents), "saved_bp": share_bp(w.saved_ucents, w.actual_ucents) if w.actual_ucents else None,
    }


def as_dict(r: WeekReport) -> dict[str, Any]:
    """Every figure of the report, money as integer cents and ucents: the HTML and markdown are drawn from this."""
    previous = None
    if r.previous:
        previous = {
            "week": r.previous.week, "sessions": r.previous.sessions, "total": money_json(r.previous.ucents),
            "change_bp": r.change_bp,
        }
    return {
        "week": r.week, "first_day": r.first_day, "last_day": r.last_day, "generated": r.generated,
        "version": __version__, "state": r.state, "in_progress": r.in_progress, "show_projects": r.show_projects,
        "sessions": r.sessions, "projects": r.projects, "agents": r.agents, "models": r.models,
        "tokens": tokens_json(r.usage), "total": money_json(r.spend_ucents), "excluded_tokens": r.excluded_tokens,
        "router": _router_json(r.savings),
        "background": {"sessions": r.background_sessions, "saved": money_json(r.background_saved_ucents)},
        "prompted": {
            "sessions": r.prompted_sessions, "actual": money_json(r.prompted_actual_ucents),
            "saved": money_json(r.prompted_saved_ucents),
            "saved_bp": share_bp(r.prompted_saved_ucents, r.prompted_actual_ucents) if r.prompted_actual_ucents else None,
        },
        "previous": previous,
        "by_model": [_slice_json("model", s, r.spend_ucents) for s in r.by_model],
        "by_agent": [_slice_json("agent", s, r.spend_ucents) for s in r.by_agent],
        "by_day": [_slice_json("day", s, r.spend_ucents) for s in r.by_day],
        "tiers": [
            {"tier": t.tier, "sessions": t.sessions, "cost": money_json(t.ucents), "unpriced_sessions": t.unpriced_sessions}
            for t in r.tiers
        ],
        "top_sessions": [
            {
                "short_id": o.short_id, "agent": o.agent, "project": o.project, "model": o.model,
                "recommended_model": o.recommended_model, "tier": o.tier, "actual": money_json(o.actual_ucents),
                "recommended": money_json(o.router_ucents), "saved": money_json(o.saved_ucents),
            }
            for o in r.top
        ],
        "warnings": r.warnings,
    }
