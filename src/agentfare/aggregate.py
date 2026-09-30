"""Aggregation: sessions -> priced rows -> groups by model / project / day / week."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo

from .config import Config, ConfigError, day_of, fmt_iso, iso_week_of, now_utc, parse_iso, week_bounds
from .cost import usage_cost_ucents
from .db import StoredSession, Store
from .models import Usage
from .pricing import ModelPrice, PriceTable


@dataclass(frozen=True)
class Row:
    """One (session, model, slot) usage line with its price: what was spent on ``model`` in that 15-minute slot."""

    session: StoredSession
    model: str  # as logged
    canonical: str  # canonical model id in the price table, or the logged id when unknown
    usage: Usage
    ucents: int | None  # None = unpriced (unverified or unknown model)
    day: str  # the local day of the slot
    week: str  # the local ISO week of the slot
    slot: str = ""  # UTC slot 'YYYY-MM-DDTHH:MM:00Z' (models.slot_of)


@dataclass
class Group:
    key: str
    sessions: set[str] = field(default_factory=set)
    usage: Usage = field(default_factory=Usage)
    ucents: int = 0
    unpriced_tokens: int = 0  # tokens that could not be priced

    @property
    def session_count(self) -> int:
        return len(self.sessions)


@dataclass(frozen=True)
class Period:
    since: str | None  # ISO-8601 UTC, inclusive
    until: str | None  # ISO-8601 UTC, exclusive
    label: str = "all time"


def priced_rows(sessions: Iterable[StoredSession], table: PriceTable, tz: tzinfo) -> list[Row]:
    """One row per (session, model, slot), so money lands on the local day and week it was spent, not the session's start.

    A session without recorded slots counts at its start (``StoredSession.effective_slots``)."""
    rows: list[Row] = []
    by_model: dict[str, tuple[ModelPrice | None, str]] = {}  # logged id -> (verified price, canonical id)
    local: dict[str, tuple[str, str]] = {}  # slot -> (day, week): sessions share slots, and converting is the slow part
    for s in sessions:
        for (slot, model), usage in sorted(s.effective_slots().items(), key=lambda item: (item[0][1], item[0][0])):
            if model not in by_model:
                price = table.priced(model)
                by_model[model] = (price, price.model if price else (table.resolve(model) or model))
            if slot not in local:
                local[slot] = (day_of(slot, tz), iso_week_of(slot, tz))
            price, canonical = by_model[model]
            day, week = local[slot]
            rows.append(
                Row(
                    session=s, model=model, canonical=canonical, usage=usage,
                    ucents=usage_cost_ucents(usage, price) if price else None, day=day, week=week, slot=slot,
                )
            )
    return rows


def group_rows(rows: Iterable[Row], key: Callable[[Row], str]) -> list[Group]:
    groups: dict[str, Group] = {}
    for r in rows:
        g = groups.setdefault(key(r), Group(key=key(r)))
        g.sessions.add(r.session.key)
        g.usage = g.usage + r.usage
        if r.ucents is None:
            g.unpriced_tokens += r.usage.total
        else:
            g.ucents += r.ucents
    return sorted(groups.values(), key=lambda g: (-g.ucents, g.key))


def total_ucents(rows: Iterable[Row]) -> int:
    return sum(r.ucents for r in rows if r.ucents is not None)


def model_warnings(models: Iterable[str], table: PriceTable) -> dict[str, str]:
    """Model id -> warning text, sorted by id, for every model that has no verified price or only a partly verified one."""
    return {m: w for m in sorted(set(models)) if (w := table.warning_for(m))}


def unpriced_models(rows: Iterable[Row], table: PriceTable) -> dict[str, str]:
    """Logged model id -> warning text, for every model in ``rows`` whose spend could not be counted or is approximate."""
    return model_warnings((r.model for r in rows), table)


def period_for(
    cfg: Config, *, week: bool | str = False, since: str | None = None, until: str | None = None
) -> Period:
    """Build a UTC period. ``week=True`` = the ISO week containing now; ``week='2026-W39'`` = that week.
    ``since``/``until`` are local calendar days (``YYYY-MM-DD``), both inclusive.

    A period holds exactly the instants whose ``day_of``/``iso_week_of`` is the one asked for; the tz database has no
    exception to that from 2011 on. Some older transitions, such as a DST end at 00:01 in the 1990s, make one local day
    come back in two separate stretches, which no half-open interval can express: there the period follows the first
    midnight."""
    tz = cfg.tz
    if week:
        first, last = week_bounds(None if week is True else week, tz)
        label = f"week {first} .. {last}"
        since, until = first, last
    else:
        label = f"{since} .. {until}" if since and until else f"from {since}" if since else f"up to {until}" if until else "all time"
    start, end = _local_day_start_utc(since, tz), _local_day_start_utc(until, tz, plus_days=1)
    if start and end and start >= end:  # a period that would end where (or before) it starts
        raise ConfigError(f"since {since} is later than until {until}")
    return Period(start, end, label)


def _local_day_start_utc(day: str | None, tz: tzinfo, plus_days: int = 0) -> str | None:
    if not day:
        return None
    try:
        d = datetime.strptime(day, "%Y-%m-%d")
    except ValueError as exc:
        raise ConfigError(f"invalid date {day!r}: expected YYYY-MM-DD") from exc
    try:
        return fmt_iso((d + timedelta(days=plus_days)).replace(tzinfo=tz))
    except OverflowError as exc:  # too close to year 1 or 9999 to express in UTC
        raise ConfigError(f"date {day!r} is outside the supported range") from exc


def load_rows(
    cfg: Config,
    store: Store,
    table: PriceTable,
    *,
    period: Period | None = None,
    project: str | None = None,
    agent: str | None = None,
) -> list[Row]:
    p = period or Period(None, None)
    sessions = store.sessions(agent=agent, project=project, since=p.since, until=p.until)
    return priced_rows(sessions, table, cfg.tz)


__all__ = [
    "Row", "Group", "Period", "priced_rows", "group_rows", "total_ucents", "model_warnings", "unpriced_models",
    "period_for", "load_rows", "now_utc", "parse_iso",
]
