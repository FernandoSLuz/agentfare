"""`agentfare prices`: show the price table, or re-check it against the official pages with --refresh."""

from __future__ import annotations

import json
import textwrap
from dataclasses import replace
from decimal import Decimal
from itertools import groupby
from typing import TYPE_CHECKING, Annotated, Any
from urllib.parse import SplitResult, urlsplit

import typer

from ..config import day_of, fmt_iso, now_utc
from ..money import to_cents, usd_to_ucents
from ..pricing import PRICE_FIELDS, ModelPrice, PriceTable, builtin_path, dump_pricing
from ..safeio import make_private_dirs, write_text_atomic
from ..state import AppState
from ..textsafe import clean_text

if TYPE_CHECKING:  # pricefetch is imported only by --refresh, so no other command ever loads the network code
    from ..pricefetch import ModelCheck, Refresh

HELP = """Show the price table, or re-check it against the official pages with --refresh.

Without --refresh nothing leaves your machine: every model with its price per 1M tokens, its verification status, \
the date it was checked and the host of its source page.

--refresh is the ONLY part of AgentFare that uses the network. It downloads each model's source page once (http or \
https, no cookies, at most 5 MB and 60 s per page), turns it into plain text, finds the first table that shows the \
model's name and ties each number in it to its label (its column header, its row label or the line above it). Each \
model ends up as confirmed (every verified price is the number the page labels as that price), changed? (a price \
differs or is missing: the table value and what the page shows are printed for you to compare), not-found (the name \
is not on the page, or has no price next to it) or error (the download failed). Numbers the page does not label are \
compared as a set: they confirm only when they are exactly the table's prices.

A source on this machine or its network is refused: a host that is loopback (127.0.0.1, ::1, localhost), private \
(10.x, 172.16-31.x, 192.168.x), link-local (169.254.x) or unspecified (0.0.0.0), or that resolves to one, is not \
fetched, and neither is a redirect to one. Pass --allow-private-hosts to fetch them anyway (for a mirror of the pages on \
your own network); the packaged table names only public hosts.

A price is never edited. --write only saves the table to the override file (~/.agentfare/pricing.yaml, created from \
the built-in table when absent) with today's date as 'checked' for confirmed models. The file is rewritten from the \
table, so comments and keys the table does not know are not kept. Once it exists it replaces the built-in table \
completely, so delete it to go back to the packaged prices.

--refresh cannot verify pages that draw their prices with JavaScript (they come back not-found), a price the page \
only states as a multiplier ("cache writes cost 1.25x input"), or the tier and long-context rules kept in each \
model's note: only the first table's standard numbers count, not Batch, Fast or long-context rates. A price counts \
only when the page ties it to a per-1M or per-1K token unit. A confirmed row means the numbers are on the page under \
their labels, not that the row is complete. The exit code is 1 when no model could be checked at all (every download \
failed), otherwise 0."""

_NAMES = {"input": "input", "cached_input": "cached input", "output": "output", "cache_write": "cache write", "cache_write_1h": "1h cache write"}
_HEADERS = {"input": "input", "cached_input": "cached", "output": "output", "cache_write": "write", "cache_write_1h": "1h"}
_SHOWN_STATUS = {"changed": "changed?", "not_found": "not-found"}
_HEADER_NOTE = (
    "AgentFare price table override, written by `agentfare prices --refresh --write`.\n"
    "It replaces the built-in table completely; delete it to go back to the packaged prices.\n"
    "Only the 'checked' dates are updated automatically: edit prices by hand, from the page named in 'source'."
)


# ---- shared helpers ------------------------------------------------------------------------------
def _status(p: ModelPrice) -> str:
    if not p.priced:
        return "unverified"
    return "partially unverified" if p.unverified_fields else "verified"


def _split(url: str) -> SplitResult:
    """urlsplit, except that a source which is not a URL (``http://[x``) reads as empty instead of raising."""
    try:
        return urlsplit(url)
    except ValueError:
        return urlsplit("")


def _host(url: str) -> str:
    return clean_text(_split(url).hostname or "-")


def _price_text(d: Decimal) -> str:
    whole, _, frac = format(d, "f").partition(".")
    return f"{whole}.{frac.rstrip('0').ljust(2, '0')}"


def _money(d: Decimal) -> dict[str, Any]:
    """A price per 1M tokens as exact USD text plus integer cents and micro-cents."""
    ucents = usd_to_ucents(d)
    return {"usd": _price_text(d), "cents": to_cents(ucents), "ucents": ucents}


def _select(table: PriceTable, unverified: bool) -> list[ModelPrice]:
    models = [table.models[m] for m in sorted(table.models)]
    return [p for p in models if _status(p) != "verified"] if unverified else models


def _pricing_json(table: PriceTable) -> dict[str, Any]:
    builtin = table.path is None or table.path == builtin_path()
    return {"source": "builtin" if builtin else "override", "path": str(table.path or builtin_path())}


def _warnings(table: PriceTable, models: list[ModelPrice]) -> list[dict[str, str]]:
    out = []
    for p in models:
        if _status(p) != "verified":
            text = table.warning_for(p.model) or f"{p.model}: some prices are unverified"
            out.append({"model": p.model, "message": f"{text} (source {_host(p.source)}, checked {p.checked or 'never'})"})
    return out


def _width(st: AppState) -> int:
    """Where prose is broken: 80 columns, or the console's when it is narrower."""
    return max(30, min(80, st.out.width))


def _say(st: AppState, line: str = "", style: str | None = None, *, wrap: bool = False) -> None:
    """Print ``line`` verbatim (no markup, no colour unless a terminal). ``wrap`` breaks prose at 80 columns (fewer on a narrower
    console), keeping paths whole.

    One call is one line: what a server or a file name put in it (an escape sequence, a newline) becomes ``?``."""
    line = clean_text(line)
    lines = textwrap.wrap(line, _width(st), subsequent_indent="    ", break_long_words=False, break_on_hyphens=False) if wrap else [line]
    for text in lines or [""]:
        st.out.print(text, style=style, markup=False, highlight=False, soft_wrap=True)


def _print_header(st: AppState, table: PriceTable) -> None:
    info = _pricing_json(table)
    if info["source"] == "builtin":
        _say(st, f"price table: built-in table ({info['path']})", wrap=True)
    else:
        _say(st, f"price table: override file {info['path']} (it replaces the built-in table)", wrap=True)


def _print_warnings(st: AppState, warnings: list[dict[str, str]]) -> None:
    if not warnings:
        return
    _say(st)
    _say(st, f"warning: {len(warnings)} model(s) without a fully verified price; spend on an unverified model is not counted:", "bold yellow", wrap=True)
    for w in warnings:
        _say(st, f"  {w['message']}", "yellow", wrap=True)


# ---- the offline table ----------------------------------------------------------------------------
def _cell(p: ModelPrice, field: str) -> str:
    if field in p.unverified_fields:
        return "?"
    value = getattr(p, field)
    return "-" if value is None else _price_text(value)


def _wrapped(text: str, width: int, indent: str = "", later: str = "") -> list[str]:
    """``text`` in lines of at most ``width`` columns (a word longer than that stays whole), the first after ``indent``."""
    return textwrap.wrap(text, width, initial_indent=indent, subsequent_indent=later, break_long_words=False, break_on_hyphens=False) or [indent]


def _table_lines(models: list[ModelPrice], width: int = 80) -> list[tuple[str, str | None]]:
    """Rows grouped by provider (a group header names its source hosts), in columns when they fit ``width``, else one block
    of lines per model: the name, its prices, then its status and date, every line wrapped to ``width``."""
    fields = [f for f in PRICE_FIELDS if any(getattr(p, f) is not None or f in p.unverified_fields for p in models)]
    widths = [max([len(_HEADERS.get(f, f))] + [len(_cell(p, f)) for p in models]) for f in fields]
    name_w = max([len("model")] + [len(p.model) for p in models])
    status_w = max([len("status")] + [len(_status(p).replace("partially unverified", "partial")) for p in models])
    head = (
        f"  {'model':<{name_w}}"
        + "".join(f" {_HEADERS.get(f, f):>{w}}" for f, w in zip(fields, widths))
        + f" {'status':<{status_w}} checked"
    )
    columns = len(head) + len(" 2026-09-30") - len(" checked") <= width  # a date is ten characters, 'checked' seven
    lines: list[tuple[str, str | None]] = [(head, "bold")] if columns else []
    for provider, group in groupby(sorted(models, key=lambda p: (p.provider or "other", p.model)), key=lambda p: p.provider or "other"):
        rows = list(group)
        title = f"{provider}  ({', '.join(sorted({_host(p.source) for p in rows}))})"
        lines += [(text, "bold") for text in _wrapped(title, width, later="    ")]
        for p in rows:
            status = _status(p).replace("partially unverified", "partial")
            style = None if status == "verified" else "yellow"
            if columns:
                cells = "".join(f" {_cell(p, f):>{w}}" for f, w in zip(fields, widths))
                lines.append((f"  {p.model:<{name_w}}{cells} {status:<{status_w}} {p.checked or '-'}", style))
                continue
            lines += [(text, style) for text in _wrapped(p.model, width, "  ", "      ")]
            prices = ", ".join(f"{_HEADERS.get(f, f)}\xa0{_cell(p, f)}" for f in fields if _cell(p, f) != "-")  # a label stays with its number
            lines += [(text.replace("\xa0", " "), style) for text in _wrapped(prices or "no price published", width, "      ", "      ")]
            lines += [(text, style) for text in _wrapped(f"{status}, checked {p.checked or '-'}", width, "      ", "      ")]
    legend = "USD per 1M tokens. ? = unverified, - = none published (the input price applies)."
    if any(_status(p) == "partially unverified" for p in models):
        legend += " partial = partially unverified."
    notes = [legend]
    if "cache_write" in fields:
        notes.append("write = cache write (Anthropic: 5-minute)" + (", 1h = 1-hour cache write." if "cache_write_1h" in fields else "."))
        if "cache_write_1h" in fields:
            notes.append("A - under 1h means the write price applies to 1-hour writes.")
    lines.append(("", None))
    lines += [(text, "dim") for note in notes for text in _wrapped(note, width)]
    return lines


def _model_json(table: PriceTable, p: ModelPrice) -> dict[str, Any]:
    return {
        "model": p.model,
        "provider": p.provider,
        "status": _status(p),
        "unverified_fields": list(p.unverified_fields),
        "prices_per_mtok": {f: None if getattr(p, f) is None else _money(getattr(p, f)) for f in PRICE_FIELDS},
        "source": p.source,
        "source_host": _host(p.source),
        "checked": p.checked,
        "note": p.note,
        "aliases": sorted(a for a, target in table.aliases.items() if target == p.model),
    }


def _list_prices(st: AppState, table: PriceTable, unverified: bool, as_json: bool) -> None:
    models = _select(table, unverified)
    warnings = _warnings(table, models)
    if as_json:
        payload = {
            "pricing": _pricing_json(table),
            "unit": "usd_per_1m_tokens",
            "models": [_model_json(table, p) for p in models],
            "warnings": warnings,
        }
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    _print_header(st, table)
    if models:
        _say(st)
        for line, style in _table_lines(models, _width(st)):
            _say(st, line, style)
    _print_warnings(st, warnings)
    if not warnings:
        _say(st)
        _say(st, "every model in the table has a verified price.")


# ---- --refresh ------------------------------------------------------------------------------------
def _detail(table: PriceTable, check: ModelCheck) -> str:
    p = table.models[check.model]
    if check.status in ("not_found", "error"):
        return check.detail
    parts = []
    off = [
        f"{_NAMES.get(c.field, c.field)} {_price_text(c.table)}" + ("" if c.page is None else f" (page shows {_price_text(c.page)})")
        for c in check.fields
        if not c.found
    ]
    if off:
        parts.append(f"not on the page: {', '.join(off)}")
    elif check.detail:
        parts.append(check.detail)
    elif not check.fields:
        parts.append("the table has no verified price to compare")
    if check.status == "changed":
        parts.append(f"numbers near the model: {', '.join(_price_text(n) for n in check.nearby)}")
    if p.unverified_fields:
        parts.append(f"unverified, not checked: {', '.join(_NAMES.get(f, f) for f in p.unverified_fields)}")
    return "; ".join(parts)


def _result_lines(table: PriceTable, checks: dict[str, ModelCheck]) -> list[tuple[str, str | None]]:
    name_w = max(len(m) for m in checks)
    prefix_w = name_w + 2 + 10 + 2
    lines: list[tuple[str, str | None]] = [(f"{'model':<{name_w}}  {'status':<10}  detail", "bold")]
    for model, check in checks.items():
        shown = _SHOWN_STATUS.get(check.status, check.status)
        first, *rest = textwrap.wrap(_detail(table, check), width=max(30, 80 - prefix_w)) or [""]
        style = {"confirmed": None, "error": "red"}.get(check.status, "yellow")
        lines.append((f"{model:<{name_w}}  {shown:<10}  {first}".rstrip(), style))
        lines += [(" " * prefix_w + more, style) for more in rest]
    return lines


def _check_json(table: PriceTable, check: ModelCheck) -> dict[str, Any]:
    p = table.models[check.model]
    return {
        "model": check.model,
        "status": check.status,
        "detail": _detail(table, check),
        "source_host": _host(p.source),
        "prices_per_mtok": {c.field: {**_money(c.table), "found_on_page": c.found, "page_price": None if c.page is None else _money(c.page)} for c in check.fields},
        "nearby_per_mtok": [_money(n) for n in check.nearby],
    }


def _write_override(st: AppState, table: PriceTable, confirmed: list[str], today: str) -> tuple[str, list[str], bool] | None:
    """Save the table to the override file with ``checked`` set to today for the confirmed models, and nothing else
    changed. Returns (path, models updated, file was created), or None when no date needed to move."""
    path = st.cfg.pricing_override_path
    stale = [m for m in confirmed if table.models[m].checked != today]
    if not stale:
        return None
    models = {m: replace(p, checked=today) if m in stale else p for m, p in table.models.items()}
    text = dump_pricing(PriceTable(models=models, aliases=dict(table.aliases)), header=_HEADER_NOTE)
    target = path.resolve()  # a symlinked override (dotfiles) is updated where it points, and stays a link
    created = not target.exists()
    try:
        make_private_dirs(target.parent)  # a data directory made here is 0700
        write_text_atomic(target, text)  # a fresh mkstemp file (0600, O_EXCL) and os.replace; an existing file keeps its mode
    except OSError as exc:
        st.err.print(clean_text(f"error: cannot write {path}: {exc.strerror or exc}"), markup=False, highlight=False, soft_wrap=True)
        raise typer.Exit(code=2) from exc
    return str(path), stale, created


def _refresh(st: AppState, table: PriceTable, unverified: bool, write: bool, as_json: bool, allow_private_hosts: bool = False) -> None:
    from .. import pricefetch  # the only place the network code is loaded

    models = _select(table, unverified)
    if models and not as_json:
        _print_header(st, table)
        sources = {p.source.split("#", 1)[0].strip() for p in models if p.source}
        _say(st, f"re-checking {len(models)} models against {len(sources)} source pages; no price will be changed.", wrap=True)

    def progress(label: str, outcome: pricefetch.PageOutcome | None) -> None:
        if outcome is None:
            st.err.print(f"fetching {clean_text(label)} ... ", end="", markup=False, highlight=False, soft_wrap=True)
        elif outcome.ok:
            st.err.print(f"ok ({max(1, outcome.size // 1024)} KB)", markup=False, highlight=False)
        else:
            st.err.print(f"failed: {clean_text(outcome.error)}", markup=False, highlight=False, soft_wrap=True)

    result = pricefetch.refresh(table, [p.model for p in models], progress, allow_private_hosts=allow_private_hosts)
    counts = {s: sum(c.status == s for c in result.checks.values()) for s in ("confirmed", "changed", "not_found", "error")}
    confirmed = [m for m, c in result.checks.items() if c.status == "confirmed"]
    written = _write_override(st, table, confirmed, day_of(fmt_iso(now_utc()), st.cfg.tz)) if write else None
    attention = [m for m, c in result.checks.items() if c.status != "confirmed"]
    if as_json:
        payload = {
            "pricing": _pricing_json(table),
            "pages": [{"host": _host(o.url), "path": clean_text(_split(o.url).path), "ok": o.ok, "error": o.error, "models": list(o.models)} for o in result.pages],
            "results": [_check_json(table, c) for c in result.checks.values()],
            "summary": counts,
            "written": None if not written else {"path": written[0], "checked_updated": written[1], "created": written[2]},
            "warnings": _warnings(table, models),
        }
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
    elif models:
        _print_refresh(st, table, result, counts, attention, written, write)
    else:
        _say(st, "no models to refresh.")
    if result.all_failed:
        raise typer.Exit(code=1)


def _print_refresh(
    st: AppState,
    table: PriceTable,
    result: Refresh,
    counts: dict[str, int],
    attention: list[str],
    written: tuple[str, list[str], bool] | None,
    write: bool,
) -> None:
    _say(st)
    for line, style in _result_lines(table, result.checks):
        _say(st, line, style)
    _say(st)
    _say(st, ", ".join(f"{n} {_SHOWN_STATUS.get(s, s)}" for s, n in counts.items()))
    if written:
        path, updated, created = written
        _say(st, f"'checked' set to today for {len(updated)} confirmed models in {path}; no price was changed.", wrap=True)
        if created:
            _say(st, "note: this file now replaces the built-in table for every model; delete it to go back to the packaged prices.", wrap=True)
    elif write:
        _say(st, "nothing written: no confirmed model needed a newer 'checked' date.")
    elif counts["confirmed"]:
        _say(st, f"add --write to record today's date for the confirmed models in {st.cfg.pricing_override_path}.", wrap=True)
    if attention:
        _say(st, f"needs a manual look: {', '.join(attention)}", "yellow", wrap=True)
        path = st.cfg.pricing_override_path
        where = f"in {path}" if path.exists() else f"in {path} (it does not exist yet: --write creates it from the built-in table)"
        _say(st, f"prices are never edited automatically: correct them by hand {where}.", wrap=True)
    _print_warnings(st, _warnings(table, [table.models[m] for m in result.checks]))


def register(app: typer.Typer) -> None:
    @app.command("prices", help=HELP)
    def prices_cmd(
        ctx: typer.Context,
        refresh: Annotated[bool, typer.Option("--refresh", help="Re-check the prices against the official pages named in each model's source. The only use of the network.")] = False,
        write: Annotated[bool, typer.Option("--write", help="With --refresh: save the table to the override file, setting 'checked' to today for confirmed models only.")] = False,
        allow_private_hosts: Annotated[bool, typer.Option("--allow-private-hosts", help="With --refresh: also fetch a source whose host is loopback, private, link-local or unspecified (refused by default, redirects included).")] = False,
        unverified: Annotated[bool, typer.Option("--unverified", help="Only models whose price is unverified or partially unverified (also limits --refresh to them).")] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output: prices as USD text plus integer cents and micro-cents per 1M tokens.")] = False,
    ) -> None:
        st: AppState = ctx.obj
        if write and not refresh:
            raise typer.BadParameter("--write needs --refresh")
        if allow_private_hosts and not refresh:
            raise typer.BadParameter("--allow-private-hosts needs --refresh")
        if refresh:
            _refresh(st, st.table, unverified, write, as_json, allow_private_hosts)
        else:
            _list_prices(st, st.table, unverified, as_json)
