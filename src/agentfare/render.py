"""Shared rendering for the reporting commands (``summary``, ``sessions``, ``models``).

Each command first builds a plain dict of integers and strings (the very keys ``--json`` prints) and draws
its tables from that dict, so the two outputs cannot disagree. Money is JSON ``{"cents": .., "ucents": ..}``;
percentages are integer basis points; nothing here touches a float. Names from the logs are drawn literally, with
no Rich markup and no control characters (``plain``), and the tables degrade on a narrow or non-Unicode console
instead of cutting a number or crashing (``print_table``, ``glyph``).
"""

from __future__ import annotations

import difflib
import functools
import json
import re
from bisect import bisect_right
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Optional

import typer
from rich import box
from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .aggregate import Group, Period, Row, group_rows, load_rows, model_warnings, period_for, unpriced_models
from .config import Config, ConfigError, day_of
from .db import StoreError
from .models import AGENTS, MAX_NAME_CHARS, Usage
from .money import format_usd_compact, to_cents
from .pricing import PriceTable, PricingError
from .state import AppState
from .textsafe import clean_lines, clean_text  # noqa: F401 - re-exported: render.clean_text is what the other modules import

# ---- shared command-line options ------------------------------------------------------
WeekOpt = Annotated[bool, typer.Option("--week", help="Only the ISO week that contains today.")]
WeekOfOpt = Annotated[Optional[str], typer.Option("--week-of", metavar="YYYY-Www", help="Only this ISO week, e.g. 2026-W39.")]
SinceOpt = Annotated[Optional[str], typer.Option("--since", metavar="YYYY-MM-DD", help="First day, inclusive (configured timezone).")]
UntilOpt = Annotated[Optional[str], typer.Option("--until", metavar="YYYY-MM-DD", help="Last day, inclusive (configured timezone).")]
ProjectOpt = Annotated[Optional[str], typer.Option("--project", metavar="NAME", help="Only this project. The match ignores case: work finds both work and Work, which are listed apart.")]
AgentOpt = Annotated[Optional[str], typer.Option("--agent", "-a", metavar="AGENT", help=f"Only this agent: {', '.join(AGENTS)}.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Print JSON: money as integer cents and ucents, stable keys.")]


@dataclass(frozen=True)
class Filters:
    period: Period
    project: str | None = None
    agent: str | None = None

    def scope(self) -> str:
        """``week 2026-09-28 .. 2026-10-04, project acme, agent claude``: what the numbers cover."""
        parts = [self.period.label]
        if self.project:
            parts.append(f"project {self.project}")
        if self.agent:
            parts.append(f"agent {self.agent}")
        return ", ".join(parts)

    def as_json(self) -> dict[str, Any]:
        p = self.period
        return {"period": {"label": p.label, "since": p.since, "until": p.until},
                "filters": {"project": self.project, "agent": self.agent}}


_DAY = re.compile("[0-9]{4}-[0-9]{2}-[0-9]{2}")


def resolve_filters(
    cfg: Config, *, week: bool, week_of: str | None, since: str | None, until: str | None,
    project: str | None, agent: str | None,
) -> Filters:
    """Validate the common options; every problem is a ``ConfigError`` (the CLI prints it as ``error: ...``)."""
    for flag, value in (("--week-of", week_of), ("--since", since), ("--until", until), ("--project", project), ("--agent", agent)):
        if value is not None and not value.strip():
            raise ConfigError(f"{flag} needs a value")
    if week and week_of is not None:
        raise ConfigError("--week and --week-of cannot be combined")
    if (week or week_of is not None) and (since is not None or until is not None):
        raise ConfigError("--week and --week-of cannot be combined with --since or --until")
    since, until = (v.strip() if v is not None else None for v in (since, until))
    for day in (since, until):
        if day is not None and not _DAY.fullmatch(day):  # strptime would take 2026-9-1: a date has one spelling
            raise ConfigError(f"invalid date {day!r}: expected YYYY-MM-DD")
    period = period_for(cfg, week=week_of.strip() if week_of is not None else week, since=since, until=until)
    if agent is not None and agent.strip().lower() not in AGENTS:
        raise ConfigError(f"unknown agent {agent!r}: expected one of {', '.join(AGENTS)}")
    return Filters(period, project.strip() if project is not None else None, agent.strip().lower() if agent is not None else None)


def load(st: AppState, filters: Filters) -> list[Row]:
    return load_rows(st.cfg, st.store(), st.table, period=filters.period, project=filters.project, agent=filters.agent)


# Problems with the setup, not bugs: the configuration, the price table and the database. They are one line of
# ``error: ...`` and exit code 2 (here for a command, and in ``cli._entry`` for the ones no command caught).
SETUP_ERRORS = (ConfigError, PricingError, StoreError)


def print_error(exc: Exception) -> None:
    typer.secho(f"error: {clean_lines(str(exc))}", fg=typer.colors.RED, err=True)  # a message can quote a file name or a server


@contextmanager
def clean_errors() -> Iterator[None]:
    """Turn configuration, price-table and database problems into ``error: ...`` on stderr and exit code 2."""
    try:
        yield
    except SETUP_ERRORS as exc:
        print_error(exc)
        raise typer.Exit(2) from exc


def emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, indent=2))  # plain stdout: Rich would wrap long lines


def plain(text: str, console: Console) -> str:
    """``text`` as this console can show it: unsafe characters, and those its encoding lacks, become ``?``."""
    return clean_text(text).encode(console.encoding, "replace").decode(console.encoding)


def glyph(console: Console, preferred: str, fallback: str) -> str:
    """``preferred`` where the console's encoding can write it (a bar, an ellipsis), else ``fallback``."""
    return preferred if plain(preferred, console) == preferred else fallback


def say(console: Console, text: str, style: str = "") -> None:
    """One line of literal text: never Rich markup, never a raw control character."""
    console.print(Text(plain(text, console), style=style))


def _bounded(cell: str, limit: int, ellipsis: str) -> str:
    """``cell`` as it is, or, when it has more than ``limit`` characters, its first and last ``limit // 2`` around ``ellipsis``:
    one linear pass before any fitting, so a name of a hundred thousand characters costs no more than one of ``limit``."""
    if len(cell) <= limit:
        return cell
    half = limit // 2
    return cell[:half] + ellipsis + cell[-half:]


def _project_hint(st: AppState, project: str) -> str:
    """For a project name that matches nothing in the database: the names that start with it or contain it, then those that
    are spelt about like it, or else the biggest by spend."""
    spend = {g.key: g.ucents for g in group_rows(load_rows(st.cfg, st.store(), st.table), lambda r: r.session.project)}
    names = sorted(spend, key=lambda name: (-spend[name], name))
    key = project.casefold()
    starting = [n for n in names if n.casefold().startswith(key)]
    containing = [n for n in names if key in n.casefold() and n not in starting]
    folded: dict[str, list[str]] = {}
    for name in names:  # names that differ only by case are different projects: each is named as it is written
        folded.setdefault(name.casefold(), []).append(name)
    spelt = [name for close in difflib.get_close_matches(key, list(folded), n=3) for name in folded[close]]
    near = list(dict.fromkeys([*starting, *containing, *spelt]))[:5]
    if near:
        return f"closest names: {', '.join(near)}"
    return f"the biggest by spend: {', '.join(names[:5])}"


def empty_hints(st: AppState, filters: Filters) -> list[str]:
    """What to tell a person whose view of the data came out empty, the same for every command that has one.

    Setup advice (``agentfare ingest``, ``agentfare config``) only when the database holds no session at all. Otherwise the
    data is there and the view missed it: the dates it does cover, or, for a name that matches nothing, the names that do."""
    store = st.store()
    if store.count() == 0:
        return ["the database has no sessions at all: run agentfare ingest, and check the log sources with agentfare config"]
    if filters.project and store.usage_range(project=filters.project) is None:  # the name matched nothing, not the period
        return [f"no project is called {filters.project!r}; {_project_hint(st, filters.project)}"]
    if filters.agent and store.usage_range(agent=filters.agent) is None:
        return [f"the database has no {filters.agent} sessions"]
    found = store.usage_range(agent=filters.agent, project=filters.project)
    if found is None:
        return []
    tz = st.cfg.tz
    kept = ", ".join(f"{label} {value}" for label, value in (("project", filters.project), ("agent", filters.agent)) if value)
    return [f"data exists from {day_of(found[0], tz)} to {day_of(found[1], tz)}" + (f" ({kept})" if kept else "")]


def print_empty(st: AppState, filters: Filters) -> None:
    say(st.out, f"no sessions in this period ({filters.scope()})")
    for hint in empty_hints(st, filters):
        say(st.out, hint)


def print_period_note(console: Console, filters: Filters) -> None:
    """Under the header of a view of a period: what the money in it is."""
    if filters.period.since or filters.period.until:
        say(console, "spent inside the period only: a session that spans its edge counts in part", "dim")


# ---- numbers ---------------------------------------------------------------------------
def div_round(n: int, d: int) -> int:
    """``n / d`` rounded half up, for non-negative integers."""
    return (2 * n + d) // (2 * d)


usd = format_usd_compact


def money_json(ucents: int) -> dict[str, int]:
    return {"cents": to_cents(ucents), "ucents": ucents}


def tokens(n: int) -> str:
    """``950`` / ``12.3k`` / ``4.5M`` / ``1.2B``: one decimal below 100 of a unit, none above (integer math)."""
    if n < 1000:
        return str(n)
    for size, suffix in ((10**3, "k"), (10**6, "M"), (10**9, "B")):
        tenths = div_round(n * 10, size)
        if tenths < 1000:
            return f"{tenths // 10}.{tenths % 10}{suffix}"
        if (whole := div_round(n, size)) < 1000:
            return f"{whole}{suffix}"
    return f"{div_round(n, 10**9)}B"


def tokens_json(u: Usage) -> dict[str, int]:
    """The four price buckets and the total; ``cache_write`` holds every cache write, 5-minute and 1-hour."""
    return {"input": u.input, "cache_read": u.cache_read, "cache_write": u.total_input - u.input - u.cache_read,
            "output": u.output, "total": u.total}


def share_bp(part: int, whole: int) -> int:
    """Share in basis points (10000 = 100%), rounded half up."""
    return div_round(part * 10_000, whole) if whole > 0 else 0


def percent(part: int | None, whole: int = 10_000) -> str:
    """``part`` of ``whole`` as a percentage with one decimal, rounded half up once, from the exact integers.

    The default ``whole`` reads ``part`` as basis points (``share_bp``); pass the amounts themselves when they are at hand,
    because rounding a share to basis points first and to tenths after that would round twice. A share of something that
    is not zero never reads ``0.0%``, and ``None`` (no share) is ``-``.
    """
    if part is None:
        return "-"
    tenths = div_round(part * 1000, whole) if whole > 0 else 0
    if tenths == 0 and part > 0 and whole > 0:
        return "<0.1%"
    return "100%" if tenths >= 1000 else f"{tenths // 10}.{tenths % 10}%"


def duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def share_cell(entry: dict[str, Any], spend: int) -> str:
    """Share column: the row's cost of ``spend``, or ``-`` when it has no share. A row that spent something is never ``0.0%``."""
    return "-" if entry["share_bp"] is None else percent(entry["cost"]["ucents"], spend)


def spend_text(total: dict[str, int], excluded_tokens: int, all_tokens: int, nothing: str = "n/a (nothing priced)") -> str:
    """Headline spend: ``$3.63``; ``$3.30+`` when tokens without a verified price are left out of it; and when nothing
    could be priced no figure at all (``nothing``), because ``$0.00`` would read as free."""
    if excluded_tokens and excluded_tokens >= all_tokens:
        return nothing
    return usd(total["ucents"]) + ("+" if excluded_tokens else "")


def bar(part: int, peak: int, width: int, char: str = "█") -> str:
    """Proportional bar of at most ``width`` cells; any non-zero amount gets at least one."""
    return char * max(1, div_round(part * width, peak)) if peak > 0 and part > 0 else ""


def leaderboard_key(g: Group) -> tuple[bool, int, int, str]:
    """Most spend first; groups without a verified price last, biggest token count first."""
    return (g.unpriced_tokens > 0, -g.ucents, -g.usage.total, g.key)


def cost_json(g: Group) -> dict[str, int] | None:
    """The group's priced spend; None when none of its tokens has a price (0 would read as free)."""
    return money_json(g.ucents) if g.unpriced_tokens < g.usage.total else None


def group_entry(g: Group, spend: int) -> dict[str, Any]:
    """The keys every breakdown row shares."""
    cost = cost_json(g)
    return {
        "sessions": g.session_count,
        "tokens": tokens_json(g.usage),
        "cost": cost,
        "share_bp": share_bp(g.ucents, spend) if cost else None,
        "unpriced_tokens": g.unpriced_tokens,
    }


# ---- warnings --------------------------------------------------------------------------
def warnings_payload(rows: Sequence[Row], table: PriceTable) -> list[dict[str, Any]]:
    """One entry per model whose price is missing, unverified or partly unverified (``aggregate.unpriced_models``)."""
    excluded: dict[str, int] = {}
    for r in rows:
        if r.ucents is None:
            excluded[r.model] = excluded.get(r.model, 0) + r.usage.total
    return [
        {"model": model, "status": table.status(model) if model in excluded else "partial",
         "message": message, "excluded_tokens": excluded.get(model, 0)}
        for model, message in unpriced_models(rows, table).items()
    ]


def price_warnings(rows: Sequence[Row], table: PriceTable, cfg: Config) -> list[dict[str, Any]]:
    """``warnings_payload`` plus the models the router recommends (``router.recommend``): a saving computed on a model
    without a verified price is no saving, so it is named too, with no tokens left out."""
    found = warnings_payload(rows, table)
    named = {w["model"] for w in found}
    recommended = {m for m in (cfg.get("router.recommend") or {}).values() if isinstance(m, str)}
    for model, message in model_warnings(recommended - named, table).items():
        status = table.status(model)  # a warning on a model that is still "priced" means an unverified optional price
        found.append({"model": model, "status": "partial" if status == "priced" else status, "message": message, "excluded_tokens": 0})
    return found


def without_price(warnings: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The warnings about a model that has no verified price at all, as against one that is only partly verified."""
    return [w for w in warnings if w["status"] != "partial"]


def print_warnings(console: Console, warnings: Sequence[dict[str, Any]]) -> None:
    if not warnings:
        return
    lost = sum(w["excluded_tokens"] for w in warnings)
    missing = without_price(warnings)
    if missing:
        head = f"warning: {plural(len(missing), 'model')} without a verified price"
        head += f", {tokens(lost)} tokens are not counted in any dollar figure" if lost else ""
    else:
        head = "warning: some prices are only partly verified"
    console.print()
    say(console, head, "bold yellow")
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=1)
    grid.add_column(justify="right")
    grid.add_column(overflow="fold")  # the message wraps here, under itself; a word wider than the column folds (Rich's default cuts it with U+2026, which an ASCII console cannot write)
    for w in warnings:
        left_out = f"{tokens(w['excluded_tokens'])} tokens" if w["excluded_tokens"] else ""
        message = plain(_bounded(w["message"], max(4 * console.width, 2 * MAX_NAME_CHARS), "..."), console)
        grid.add_row(Text("!", style="bold yellow"), Text(left_out), Text(message))
    console.print(grid)


def cost_cells(entries: Sequence[dict[str, Any]]) -> list[str]:
    """Cost column: ``$1.23``; ``$1.23+`` when the row also holds unpriced tokens; nothing priced: its
    ``status`` (``unverified``, ``unknown``) or ``-``."""
    mixed = [e["cost"] is not None and e["unpriced_tokens"] > 0 for e in entries]
    return [
        e.get("status", "-") if e["cost"] is None else usd(e["cost"]["ucents"]) + ("+" if m else " " if any(mixed) else "")
        for e, m in zip(entries, mixed)
    ]


def print_plus_legend(console: Console, entries: Sequence[dict[str, Any]], *, total: bool = False) -> None:
    """The key to ``+``, when a row (or, with ``total``, the headline total) carries one."""
    if total or any(e["cost"] is not None and e["unpriced_tokens"] > 0 for e in entries):
        say(console, "+ also holds tokens without a verified price, which the cost leaves out", "dim")


# ---- tables ----------------------------------------------------------------------------
ELLIPSIS = "…"
MODEL_MIN = 18  # a model name is cut no narrower than this (``claude-sonnet-5-5`` has 17) before another column is left out


@dataclass(frozen=True)
class Col:
    header: str  # a "\n" stacks it on two lines
    right: bool = False
    min_width: int | None = None  # None: never shortened; an int: may be cut with an ellipsis down to this width
    cut_last: bool = False  # among the shortenable columns, this one is cut only when the others are at their floor
    optional: int = 0  # 0: always drawn; n: left out when the table does not fit even with the others cut, lowest n first
    label: str = ""  # names a column without a header in the "left out" note
    middle: bool = False  # a cut takes the middle and keeps the start and the tail: a model name says its version at the end


class Whole(str):
    """A cell that is not cut while its column can be as wide as it: a figure written into a label (``3 more (total $1.20)``)."""


_WORD_EDGES = "-_./:@ "
_NUMBER = re.compile("[0-9]+(?:[.][0-9]+)*")


@functools.lru_cache(maxsize=512)
def _cut_points(cell: str) -> tuple[list[int], tuple[tuple[list[int], list[int]], ...]]:
    """(cells before each character, the places ``_cut_middle`` may cut: between words first, then anywhere outside a number).

    Each place list comes with the cells before each of its places, which only grow: worked out once per cell, because
    ``_unique_width`` cuts the same cell at width after width."""
    wide = [0]
    for ch in cell:
        wide.append(wide[-1] + cell_len(ch))
    inside = {p for m in _NUMBER.finditer(cell) for p in range(m.start() + 1, m.end())}  # 2026, 5.1: never split
    ok = [p for p in range(1, len(cell)) if p not in inside]
    words = [p for p in ok if cell[p - 1] in _WORD_EDGES or cell[p] in _WORD_EDGES]
    return wide, tuple((edges, [wide[p] for p in edges]) for edges in (words, ok))


def _cut_middle(cell: str, width: int, ellipsis: str) -> str:
    """``cell`` cut to ``width`` cells in the middle, keeping the start and, a little longer, the tail.

    The cut falls between the words of the name when it can, and never inside a number: ``claude-opus-4-5-20251101``
    does not turn into ``claude-opus-4-5-2025…``. A cell that cannot be cut that way loses its end instead.

    Of every cut that fits, the one that keeps the most cells wins, then the one whose tail is nearest to three fifths of what
    is kept, then the later ones. For each place where the tail can begin the best start is the last place that still fits
    (one bisect, because the cells before a place only grow), so the search is ``n log n``, not ``n`` squared.
    """
    room = width - cell_len(ellipsis)
    wide, candidates = _cut_points(cell)
    for edges, edge_wide in candidates:
        best: tuple[int, int, int, int] | None = None  # (kept cells, closeness of the tail to 3/5, i, j) of the best cut so far
        for k, j in enumerate(edges):
            tail = wide[-1] - wide[j]
            fits = min(k, bisect_right(edge_wide, room - tail))  # the places before j that leave room for this tail
            if fits:
                i = edges[fits - 1]
                kept = wide[i] + tail
                score = (kept, -abs(5 * tail - 3 * kept), i, j)
                if best is None or score > best:
                    best = score
        if best:
            return cell[: best[2]] + ellipsis + cell[best[3]:]
    return set_cell_size(cell, width - 1) + ellipsis


def _cut_tail(cell: str, width: int, ellipsis: str) -> str:
    """``cell`` cut to ``width`` cells at its end, the ellipsis included, and never inside a number: ``claude-opus-4-5-20251101``
    becomes ``claude-opus-4-5-…``, not ``claude-opus-4-5-2025…``."""
    kept = set_cell_size(cell, width - cell_len(ellipsis)).rstrip(" ") if cell_len(ellipsis) < width else ""
    if kept and cell.startswith(kept):
        for match in _NUMBER.finditer(cell):
            if match.start() < len(kept) < match.end() and match.start() > 0:  # the cut falls inside this number: stop before it
                kept = cell[: match.start()]
                break
    return kept + ellipsis if kept else set_cell_size(cell, width - 1) + ellipsis


def _cut(cell: str, width: int, ellipsis: str, middle: bool = False) -> str:
    """``cell`` shortened to ``width`` cells, one of them the ellipsis (at the end, or in the middle)."""
    if cell_len(cell) <= width:
        return cell
    return _cut_middle(cell, width, ellipsis) if middle else _cut_tail(cell, width, ellipsis)


def _unique_width(col: Col, cells: set[str], natural: int, lowest: int) -> int:
    """The narrowest width from ``lowest`` up at which no two different cells are cut to the same text (``natural`` when there
    is none): ``claude-opus-...`` three times does not say which model is which."""
    for width in range(min(natural, lowest), natural):
        if len({_cut(cell, width, ELLIPSIS, col.middle) for cell in cells}) == len(cells):
            return width
    return natural


def _floor(col: Col, cells: set[str], natural: int) -> int:
    """The narrowest ``col`` may be. ``min_width``, or more when two different cells would then be cut to the same text."""
    if col.min_width is None:
        return natural
    return _unique_width(col, cells, natural, col.min_width)


def _fit(
    cols: Sequence[Col], natural: Sequence[int], floors: Sequence[int], heads: Sequence[int], available: int, *, crush: bool,
) -> list[int]:
    """Cut shortenable columns, the widest first, one cell at a time until the row fits.

    Order: the ordinary ones down to their floor, then the ``cut_last`` ones, then, with ``crush``, all of them
    down to the width of their header (or wider, where their cells would otherwise come out the same). A column without
    ``min_width`` is never touched.
    """
    widths = list(natural)
    excess = sum(widths) - available
    for last, limits in ((False, floors), (True, floors), (None, heads)):
        while excess > 0 and (limits is floors or crush):
            cuttable = [
                i for i, c in enumerate(cols)
                if c.min_width is not None and (last is None or c.cut_last == last) and widths[i] > limits[i]
            ]
            if not cuttable:
                break
            widths[max(cuttable, key=lambda i: widths[i])] -= 1
            excess -= 1
    return widths


def _layout(
    cols: Sequence[Col], natural: Sequence[int], floors: Sequence[int], heads: Sequence[int], width: int,
) -> tuple[list[int], list[int], int]:
    """(indices of the columns to draw, their widths, gap between columns) for a console ``width``.

    Every column at its natural width and two cells apart when that fits. Otherwise one cell apart, cutting
    shortenable columns down to their floors; if that does not fit either, optional columns are left out one by
    one (lowest ``optional`` first), each time trying the cuts again. As a last resort the shortenable columns are
    cut past their floors, down to their headers (never a number).
    """
    drops = sorted((i for i, c in enumerate(cols) if c.optional), key=lambda i: cols[i].optional)

    def fit(keep: list[int], gap: int, crush: bool) -> list[int]:
        return _fit([cols[i] for i in keep], [natural[i] for i in keep], [floors[i] for i in keep],
                    [heads[i] for i in keep], width - gap * (len(keep) - 1), crush=crush)

    for hidden in range(len(drops) + 1):
        keep = [i for i in range(len(cols)) if i not in drops[:hidden]]
        for gap in (2, 1):
            widths = fit(keep, gap, crush=False)
            if sum(widths) + gap * (len(keep) - 1) <= width and (gap == 1 or widths == [natural[i] for i in keep]):
                return keep, widths, gap
    return keep, fit(keep, 1, crush=True), 1


def print_table(
    console: Console, cols: Sequence[Col], rows: Sequence[Sequence[str]], footer: Sequence[str] | None = None,
) -> list[str]:
    """Draw ``rows`` under ``cols``, with an optional bold total row; returns the headers of the columns drawn.

    Widths are set here, not left to Rich, so that only the columns marked ``min_width`` ever lose characters:
    a number is never cut. See ``_layout`` for how a narrow console is handled. When even that is not enough the
    table is drawn wider than the console, which is better than a truncated figure.
    """
    held = [max((cell_len(plain(r[i], console)) for r in rows if isinstance(r[i], Whole)), default=0) for i in range(len(cols))]
    ellipsis = glyph(console, ELLIPSIS, "~")
    limit = max(2 * console.width, MAX_NAME_CHARS)  # stored names are cut at MAX_NAME_CHARS; this is for a database that still holds longer ones

    def tidy(cell: str) -> str:
        return plain(_bounded(cell, limit, ellipsis), console)

    rows = [[tidy(cell) for cell in row] for row in rows]
    footer = [tidy(cell) for cell in footer] if footer else None
    body = [*rows, footer] if footer else rows
    heads = [max(cell_len(line) for line in c.header.split("\n")) for c in cols]
    natural = [max([heads[i], *(cell_len(r[i]) for r in body)]) for i in range(len(cols))]
    cells = [{r[i] for r in body} for i in range(len(cols))]
    floors = [max(_floor(c, cells[i], natural[i]), held[i]) for i, c in enumerate(cols)]
    # Crushed past its floor a column goes down to its header and no further than where its cells still differ.
    crush = [max(heads[i], held[i], _unique_width(c, cells[i], natural[i], heads[i]) if c.min_width is not None else 0) for i, c in enumerate(cols)]
    keep, widths, gap = _layout(cols, natural, floors, crush, console.width)
    needed = sum(widths) + gap * (len(keep) - 1)
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, collapse_padding=True, padding=(0, gap - 1),
                  show_footer=footer is not None, header_style="bold", footer_style="bold",
                  width=needed if needed > console.width else None)
    for i, w in zip(keep, widths):
        table.add_column(cols[i].header, justify="right" if cols[i].right else "left", width=w, no_wrap=True,
                         overflow="crop", vertical="bottom",
                         footer=Text(_cut(footer[i], w, ellipsis, cols[i].middle)) if footer else "")
    for r in rows:
        table.add_row(*(Text(_cut(r[i], w, ellipsis, cols[i].middle)) for i, w in zip(keep, widths)))
    console.print(table, crop=False)
    names = [c.label or " ".join(c.header.split()) for c in cols]
    if len(keep) < len(cols):
        say(console, f"(left out to fit {console.width} columns: {', '.join(n for i, n in enumerate(names) if i not in keep)}; "
                     "--json has everything)", "dim")
    return [names[i] for i in keep]


def print_title(console: Console, title: str) -> None:
    console.print()
    say(console, title, "bold")
