"""`agentfare report`: the shareable weekly report, as markdown or as one self-contained HTML file.

The report is assembled in ``agentfare.report`` (a dict of integers, the very keys ``--json`` prints). Here that dict is
turned into display strings (``_view``) and drawn by a Jinja template. The HTML template has autoescape on and no
script, font, image or link that would need the network: it is meant to be screenshotted and posted, or attached.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from datetime import date
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

from ..aggregate import period_for
from ..config import ConfigError
from ..money import UCENTS_PER_CENT, UCENTS_PER_USD, format_usd, ratio_str
from ..render import Filters, JsonOpt, clean_errors, clean_text, div_round, empty_hints, percent, plural, share_cell, tokens, without_price
from ..report import as_dict, build_week_report, canonical_week
from ..safeio import write_chosen_file, write_new_file

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
STDOUT = Path("-")  # ``-o -``: print instead of writing a file
METHOD = "docs/ROUTING.md"
COMPARED_PARTLY = "Compared on the sessions that are fully priced; see the notes below."


class Format(str, Enum):
    md = "md"
    html = "html"


# ---- the view: display strings, drawn from the dict that --json prints ----------------------------
def _usd(ucents: int) -> str:
    """Two decimals from one cent up (a post reads ``$0.61``, not ``$0.6140``); below a cent ``format_usd`` keeps four."""
    return format_usd(ucents, 2 if abs(ucents) >= UCENTS_PER_CENT else None)


def _date(day: str, year: bool) -> str:
    y, m, d = (int(part) for part in day.split("-"))
    return f"{MONTHS[m - 1]} {d}, {y}" if year else f"{MONTHS[m - 1]} {d}"


def _range(first: str, last: str) -> str:
    """``Sep 28 to Oct 4, 2026``; both years when the week straddles New Year."""
    return f"{_date(first, first[:4] != last[:4])} to {_date(last, True)}"


def _short_usd(ucents: int) -> str:
    """The label over a day's bar, one format for every bar of the chart: three significant digits, at most five characters, so
    it fits over a column a seventh of a phone. Cents below $10 (``$0.76``), whole dollars below $1,000 (``$250``), then
    thousands and millions with one decimal below ten of them (``$1.2k``, ``$12k``). Below a cent it says so, because ``$0.00``
    would read as no spend."""
    cents = div_round(ucents, UCENTS_PER_CENT)
    if ucents > 0 and cents == 0:
        return "<$0.01"
    if cents < 1000:
        return f"${cents // 100}.{cents % 100:02d}"
    dollars = div_round(ucents, UCENTS_PER_USD)
    if dollars < 1000:
        return f"${dollars}"
    for step, suffix in ((1000, "k"), (1_000_000, "M")):
        tenths = div_round(ucents, step * UCENTS_PER_USD // 10)
        if tenths < 100:
            return f"${tenths // 10}.{tenths % 10}{suffix}"
        if (whole := div_round(ucents, step * UCENTS_PER_USD)) < 1000:
            return f"${whole}{suffix}"
    return f"${div_round(ucents, 10**9 * UCENTS_PER_USD)}B"


def _tenths(part: int, peak: int) -> int:
    """Length of a bar in tenths of a percent, from integers; anything above zero gets a visible sliver."""
    if peak <= 0 or part <= 0:
        return 0
    return min(1000, max(8, div_round(part * 1000, peak)))


def _pct(tenths: int) -> str:
    return f"{tenths // 10}.{tenths % 10}"


def _width(part: int, peak: int) -> str:
    return _pct(_tenths(part, peak))


def _cost(entry: dict[str, Any]) -> str:
    """``$1.23``; ``$1.23+`` when some of its tokens have no verified price; ``no verified price`` when none has."""
    if entry["cost"] is None:
        return "no verified price"
    return _usd(entry["cost"]["ucents"]) + ("+" if entry["unpriced_tokens"] else "")


def _peak(entries: list[dict[str, Any]]) -> int:
    return max((e["cost"]["ucents"] for e in entries if e["cost"]), default=0)


def _bars(entries: list[dict[str, Any]], name: str, spend: int) -> list[dict[str, str]]:
    """One row per entry with its bar length relative to the biggest of them."""
    peak = _peak(entries)
    return [
        {
            "label": e[name], "cost": _cost(e), "share": share_cell(e, spend),
            "count": str(e["sessions"]), "width": _width(e["cost"]["ucents"], peak) if e["cost"] else "0",
        }
        for e in entries
    ]


def _days(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    """One column per day: weekday, date, spend (full and short) and the bar as a height from the bottom."""
    peak = _peak(entries)
    rows = []
    for e in entries:
        day, cost = date.fromisoformat(e["day"]), e["cost"]
        short = "n/a" if cost is None else _short_usd(cost["ucents"]) + ("+" if e["unpriced_tokens"] else "")
        rows.append({
            "label": WEEKDAYS[day.weekday()], "date": f"{MONTHS[day.month - 1]} {day.day}", "month": MONTHS[day.month - 1], "dom": str(day.day),
            "count": str(e["sessions"]),
            "title": f"{WEEKDAYS[day.weekday()]} {MONTHS[day.month - 1]} {day.day}: {_cost(e) if e['sessions'] else 'no sessions'}",
            "cost": _cost(e) if e["sessions"] else "", "short": short if e["sessions"] else "",
            "height": _width(cost["ucents"], peak) if cost else "0",
            "top": _pct(1000 - _tenths(cost["ucents"], peak)) if cost else "100.0",
        })
    return rows


def _tier_cost(entry: dict[str, Any]) -> str:
    """The tier's priced spend, with ``+`` when some of its sessions also used an unpriced model; ``-`` for an empty tier."""
    spend = entry["cost"]["ucents"]
    if not entry["sessions"]:
        return "-"
    if entry["unpriced_sessions"] and not spend:
        return "no verified price"
    return _usd(spend) + ("+" if entry["unpriced_sessions"] else "")


def _tiers(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"label": e["tier"].capitalize(), "tier": e["tier"], "count": str(e["sessions"]),
         "sessions": plural(e["sessions"], "session"), "cost": _tier_cost(e)}
        for e in entries
    ]


def _segments(entries: list[dict[str, Any]], sessions: int) -> list[dict[str, str]]:
    """The tiers as consecutive stretches of one 100% bar: where each starts and how long it is, in tenths of a percent."""
    out, start = [], 0
    for e in entries:
        if e["sessions"]:
            length = div_round(e["sessions"] * 1000, sessions)
            out.append({"tier": e["tier"], "x": _pct(start), "width": _pct(length)})
            start += length
    return out


def _top(entries: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "id": e["short_id"], "agent": e["agent"], "project": e["project"], "model": e["model"],
            "to": e["recommended_model"], "tier": e["tier"], "actual": _usd(e["actual"]["ucents"]),
            "router": _usd(e["recommended"]["ucents"]), "saved": _usd(e["saved"]["ucents"]),
            "ratio": ratio_str(e["actual"]["ucents"], e["recommended"]["ucents"]) if e["recommended"]["ucents"] else "free",
        }
        for e in entries
    ]


def _saved_pct(router: dict[str, Any]) -> str:
    """The saving as a share of what the compared sessions cost, rounded once from the amounts; ``-`` without any spend."""
    return percent(router["saved"]["ucents"], router["actual"]["ucents"]) if router["actual"]["ucents"] else "-"


def _spend(data: dict[str, Any]) -> str:
    """Headline spend: ``$3.63``; ``$3.30+`` when tokens without a verified price are left out; ``n/a`` when nothing is priced."""
    excluded = data["excluded_tokens"]
    if excluded and excluded >= data["tokens"]["total"]:
        return "n/a (nothing priced)"
    return _usd(data["total"]["ucents"]) + ("+" if excluded else "")


def _change(data: dict[str, Any]) -> str | None:
    """Spend against the week before, for a week that is over; None while it runs (see ``_ongoing``) or without a priced week before."""
    previous = data["previous"]
    if data["in_progress"] or not previous or previous["change_bp"] is None:
        return None
    bp = previous["change_bp"]
    tenths = div_round(abs(bp), 10)  # not ``percent``: that one stops at 100%, and a week can more than double
    move = "unchanged" if tenths == 0 else f"{'up' if bp > 0 else 'down'} {tenths // 10:,}.{tenths % 10}%"
    return f"{move} from {previous['week']} ({_usd(previous['total']['ucents'])})"


def _ongoing(data: dict[str, Any]) -> bool:
    """True where the change against the week before would stand but the week is not over: a part of a week is not
    comparable with a whole one, so the page says so instead of printing a percentage."""
    previous = data["previous"]
    return data["in_progress"] and bool(previous) and previous["change_bp"] is not None


def _warning(warnings: list[dict[str, Any]], excluded: int) -> dict[str, Any] | None:
    """The visible price warning: what is left out of every dollar figure, and each model it concerns."""
    if not warnings:
        return None
    missing = without_price(warnings)
    if missing:
        head = f"{plural(len(missing), 'model')} without a verified price"
        head += f": {tokens(excluded)} tokens are not counted in any dollar figure" if excluded else ""
    else:
        head = "Some prices are only partly verified"
    return {
        "head": head,
        "lines": [
            {"tokens": tokens(w["excluded_tokens"]) if w["excluded_tokens"] else "", "model": w["model"], "rest": w["message"].removeprefix(w["model"])}
            for w in warnings  # every price message starts with the model id, which the markdown must set apart from the sentence
        ],
    }


def _when(data: dict[str, Any]) -> tuple[str, str]:
    """How the text names the week: ``this week`` (and ``this week's``) while it is the current one, else its label."""
    if data["in_progress"]:
        return "this week", "this week's"
    return f"in {data['week']}", f"{data['week']}'s"


def _partial(data: dict[str, Any]) -> bool:
    """True when the router compared less than was spent: some sessions use a model without a verified price, so the percentages
    have a base smaller than the spend next to them."""
    return data["router"]["actual"]["ucents"] != data["total"]["ucents"]


def _base(data: dict[str, Any], actual: int, whole: bool = False) -> str:
    """What a percentage is of. The sessions you prompted always name theirs; all sessions do only when it is not the spend shown."""
    if _partial(data):
        return f"of the {_usd(actual)} that could be compared"
    return "" if whole else f"of the {_usd(actual)} they cost"


def _figures(data: dict[str, Any]) -> dict[str, dict[str, str]]:
    """The two savings figures of a report that has one, in the order to quote them: first what a router would have saved in the
    sessions you prompted yourself (you chose their model), then across all sessions, including sub-agent and background threads
    (nobody chose theirs). Each has the sentence it leads with, the figure, its amount and its base, all as text.

    With nothing compared among the sessions you prompted, the all-sessions figure leads and says so."""
    router, prompted = data["router"], data["prompted"]
    saved, actual = router["saved"]["ucents"], router["actual"]["ucents"]
    everyone = {
        "saved": _usd(saved), "pct": _saved_pct(router), "tone": "good" if saved > 0 else "",
        "moved": f"by moving {router['switched']} of {plural(router['sessions'], 'session')} to a cheaper model",
    }
    if not prompted["actual"]["ucents"]:
        return {
            "first": {
                **everyone, "base": _base(data, actual, whole=True),
                "kicker": "A cost router would have saved, across all sessions, including sub-agent and background threads",
            },
            "second": {
                "kicker": "Sessions you prompted yourself", "pct": "n/a", "saved": "", "tone": "", "moved": "",
                "base": "no session you prompted yourself could be compared",
            },
        }
    mine = max(prompted["saved"]["ucents"], 0)  # a slice of a session can cost more on the advised model: that is no saving
    first = {
        "kicker": "In sessions you prompted yourself, a cost router would have saved", "pct": percent(mine, prompted["actual"]["ucents"]),
        "saved": _usd(mine), "tone": "good" if mine else "", "base": _base(data, prompted["actual"]["ucents"]), "moved": "",
    }
    return {
        "first": first,
        "second": {**everyone, "kicker": "Across all sessions, including sub-agent and background threads", "base": _base(data, actual, whole=True)},
    }


def _message(data: dict[str, Any], spend: str) -> str:
    """The one sentence the report leads with (what gets posted)."""
    state, router = data["state"], data["router"]
    when, whose = _when(data)
    if state == "empty":
        return f"No sessions were recorded in {data['week']} ({_range(data['first_day'], data['last_day'])}), so there is nothing to report."
    if state == "unpriced":
        return (
            f"None of {whose} models has a verified price, so there is no dollar total and no saving to show. "
            "The models are named below."
        )
    spent = f"I spent {spend} on AI coding agents {when}."
    if state == "none" and not router["sessions"]:
        return f"{spent} The router could not compare any session: each one used a model without a verified price."
    if state == "none":
        return f"{spent} The router found no session where a cheaper model was likely to do the job."
    figures = _figures(data)
    first, second = figures["first"], figures["second"]
    assert first is not None and second is not None
    everyone = "across all sessions, including sub-agent and background threads"
    if not data["prompted"]["actual"]["ucents"]:  # nothing you prompted could be compared: only the all-sessions figure exists
        base = f" {first['base']}," if first["base"] else ""
        return f"{spent} A cost router would have saved {first['pct']} ({first['saved']}){base} {everyone}; no session you prompted yourself could be compared."
    sentence = f"{spent} In sessions you prompted yourself, a cost router would have saved {first['pct']} ({first['saved']}) {first['base']}"
    return f"{sentence}; {everyone}: {second['pct']} ({second['saved']}){' ' + second['base'] if second['base'] else ''}."


def _check(data: dict[str, Any]) -> str:
    """The router's verdict when there is no saving to show, for the box beside the headline of the page."""
    if data["state"] == "unpriced":
        return f"No dollar total and no saving to show: none of {_when(data)[1]} models has a verified price."
    if not data["router"]["sessions"]:
        return "The router could not compare any session: each one used a model without a verified price."
    return "No session where a cheaper model was likely to do the job."


def _notes(data: dict[str, Any]) -> list[str]:
    router, background = data["router"], data["background"]
    notes = [
        "The saving is an estimate: it re-prices the same tokens on the suggested model. Tokenizers, caching and answer "
        f"quality are not measured, so read it as a hint, not a promise. Method: {METHOD}."
    ]
    if router["skipped"] and data["state"] != "unpriced":
        n = router["skipped"]
        notes.append(
            f"{plural(n, 'session')} using a model without a verified price {'is' if n == 1 else 'are'} left out of the savings "
            "comparison; any priced part is still in the total."
        )
    if background["sessions"]:
        n = background["sessions"]
        counted = f"{plural(n, 'sub-agent or background session')} (0 prompts) {'is' if n == 1 else 'are'} in the totals"
        share = f" and {'accounts' if n == 1 else 'account'} for {_usd(background['saved']['ucents'])} of the saving" if background["saved"]["ucents"] else ""
        notes.append(f"{counted}{share}; only sessions with prompts are listed.")
    if data["excluded_tokens"] and data["state"] != "unpriced":
        notes.append("A + after an amount means the figure leaves out tokens that have no verified price.")
    return notes


def _view(data: dict[str, Any], hints: Sequence[str] = ()) -> dict[str, Any]:
    """Everything a template prints, as strings; the templates do no arithmetic. ``hints`` are the lines an empty week adds."""
    router, sessions = data["router"], data["sessions"]
    spend = _spend(data)
    actual, recommended = router["actual"]["ucents"], router["recommended"]["ucents"]
    figures = _figures(data) if data["state"] == "savings" else {}
    return {
        "week": data["week"], "state": data["state"],
        "range": _range(data["first_day"], data["last_day"]) + (" (in progress)" if data["in_progress"] else ""),
        "generated": data["generated"], "version": data["version"],
        "counts": ", ".join([plural(sessions, "session"), plural(data["agents"], "agent"), plural(data["models"], "model"),
                             plural(data["projects"], "project")]),
        "spend": spend, "figure": " long" if len(spend) > 9 else "",  # a wide amount is set smaller so it fits a phone
        "message": _message(data, spend), "change": _change(data), "ongoing": _ongoing(data),
        "saved": _usd(router["saved"]["ucents"]), "saved_pct": _saved_pct(router),
        **({"first": figures["first"], "second": figures["second"]} if data["state"] == "savings" else {}),
        "switched": f"{router['switched']} of {plural(router['sessions'], 'session')}",
        "actual": _usd(actual), "router": _usd(recommended), "router_width": _width(recommended, actual),
        "compared": bool(router["sessions"]), "compared_partly": COMPARED_PARTLY if router["skipped"] else "",
        "check": _check(data),
        "warning": _warning(data["warnings"], data["excluded_tokens"]),
        "models": _bars(data["by_model"], "model", data["total"]["ucents"]),
        "agents": _bars(data["by_agent"], "agent", data["total"]["ucents"]),
        "days": _days(data["by_day"]), "tiers": _tiers(data["tiers"]), "segments": _segments(data["tiers"], sessions),
        "top": _top(data["top_sessions"]), "notes": _notes(data),
        "hints": [hint[:1].upper() + hint[1:] + "." for hint in hints],
    }


# ---- templates ------------------------------------------------------------------------------------
# Text GitHub shows exactly as written (``Project A``, ``gpt-6.1-sol``): it starts and ends with a letter or digit and in
# between holds only word characters, spaces and ``.+/-``.
_MD_PLAIN = re.compile(r"[^\W_](?:[\w .+/-]*[^\W_])?")
# What GitHub turns into a link on its own even in plain text: ``www.example.com``, ``https://...``, an e-mail address, and (as
# far as this code cares) a name with a dot followed by letters and a slash in it, which reads as a domain and a path. Each test
# is one linear scan, because a name can be long.
_WWW = re.compile(r"(?<!\w)www\.", re.I)
_DOMAIN = re.compile(r"\.[A-Za-z]")
_BACKTICKS = re.compile(r"`+")


def _autolinked(text: str) -> bool:
    return "@" in text or "://" in text or bool(_WWW.search(text)) or ("/" in text and bool(_DOMAIN.search(text)))


def md_text(value: object, *, cell: bool = False) -> str:
    """A name from the logs as literal markdown. Backslashes are not enough: GitHub turns ``@name`` into a mention, ``#12``
    into a link, ``:tada:`` into an emoji and ``$x$`` into math after the markdown is parsed, and ``www.example.com`` or
    ``example.com/x`` into a link. Only a code span is left alone, so any name that is not plain text goes in one. In a table
    cell a pipe is escaped, or it would start another cell."""
    text = str(value)
    if _MD_PLAIN.fullmatch(text) and not _autolinked(text):
        return text
    return _code_span(text, cell)


def md_code(value: object) -> str:
    """``value`` as a code span in a table cell, after ``clean_text``: a newline would end the table row or start a heading."""
    return _code_span(clean_text(str(value)), True)


def _code_span(text: str, cell: bool) -> str:
    """The fence is longer than any run of backticks inside, the text is padded when it starts or ends with one, and (in a
    table cell) a pipe is escaped, because it would start another cell even inside a code span."""
    fence = "`" * (1 + max(map(len, _BACKTICKS.findall(text)), default=0))
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    if cell:
        text = text.replace("|", "\\|")
    return f"{fence}{pad}{text}{pad}{fence}"


def _environment() -> Environment:
    env = Environment(
        loader=PackageLoader("agentfare", "templates"),
        autoescape=select_autoescape(enabled_extensions=("html.j2",), default=False),
        undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True,
    )
    env.filters["md"] = md_text
    env.filters["md_cell"] = partial(md_text, cell=True)
    env.filters["md_code"] = md_code
    return env


def render_report(data: dict[str, Any], fmt: Format, hints: Sequence[str] = ()) -> str:
    return _environment().get_template(f"report.{fmt.value}.j2").render(v=_view(data, hints))


# ---- the command ----------------------------------------------------------------------------------
def _echo(text: str) -> None:
    """Print ``text`` as is; a character the terminal's encoding lacks becomes ``?`` instead of a crash."""
    encoding = sys.stdout.encoding or "utf-8"
    typer.echo(text.encode(encoding, "replace").decode(encoding), nl=False)


def _save(text: str, path: Path, *, chosen: bool) -> None:
    """Write the report to ``path``, private (0600 when it is new). ``chosen``: the person named it with ``-o``, so a link
    to a file is followed and only a link to nothing is refused; otherwise the name is ours (``agentfare-report-<week>.html``
    in the working directory) and is created with O_EXCL and O_NOFOLLOW, so a link planted there cannot redirect the write."""
    try:
        (write_chosen_file if chosen else write_new_file)(path, text)
    except OSError as exc:
        typer.secho(clean_text(f"error: cannot write {path}: {exc.strerror or exc}"), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc
    typer.echo(str(path.resolve()))


def register(app: typer.Typer) -> None:
    @app.command("report")
    def report_cmd(
        ctx: typer.Context,
        fmt: Annotated[Format, typer.Option("--format", help="md: GitHub-flavoured markdown; html: one self-contained page.")] = Format.md,
        week_of: Annotated[Optional[str], typer.Option("--week-of", metavar="YYYY-Www", help="ISO week to report (default: the latest week with sessions).")] = None,
        output: Annotated[Optional[Path], typer.Option("--output", "-o", metavar="FILE", help="Write here, or to stdout with '-'. Default: md to stdout, html to agentfare-report-<week>.html.")] = None,
        show_projects: Annotated[bool, typer.Option("--show-projects", help="Name the projects. Hidden by default (Project A, B, ...) because reports get posted.")] = False,
        as_json: JsonOpt = False,
    ) -> None:
        """The weekly report to post: what you spent, what a cost router would have saved, and where.

        Every number comes from the same code as `summary` and `advise` for that week; amounts of a cent or more are rounded to whole cents.

        Models without a verified price stay out of the dollar figures and are named on the page. The HTML file has no script and no network reference.
        """
        if as_json and fmt is Format.html:
            raise typer.BadParameter("--json prints JSON, not a page: drop one of --json and --format html")
        st = ctx.obj
        with clean_errors():
            if week_of is not None and not week_of.strip():
                raise ConfigError("--week-of needs a value")
            week = canonical_week(week_of.strip(), st.cfg.tz) if week_of else None  # before the ingest: a typo should fail fast
            report = build_week_report(st.cfg, st.store(), st.table, week=week, show_projects=show_projects)
            data = as_dict(report)
            if as_json:
                text, default = json.dumps(data, indent=2) + "\n", None
            else:
                # an empty week says what the database does hold, as every other command does (render.empty_hints)
                hints = empty_hints(st, Filters(period_for(st.cfg, week=report.week))) if data["state"] == "empty" else []
                text = render_report(data, fmt, hints)
                default = Path(f"agentfare-report-{report.week}.html") if fmt is Format.html else None
            target = None if output == STDOUT else output or default
            if target:
                _save(text, target, chosen=output is not None)
            else:
                _echo(text)
