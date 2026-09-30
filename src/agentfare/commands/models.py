"""`agentfare models`: the cost-per-model leaderboard."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import typer
from rich.console import Console

from ..aggregate import Group, Row, group_rows
from ..money import usd_to_ucents
from ..pricing import PriceTable
from ..render import (
    MODEL_MIN, AgentOpt, Col, Filters, JsonOpt, ProjectOpt, SinceOpt, UntilOpt, WeekOfOpt, WeekOpt, clean_errors, cost_cells,
    div_round, emit_json, group_entry, leaderboard_key, load, money_json, plural, print_empty,
    print_period_note, print_plus_legend, print_table, print_warnings, resolve_filters, say, share_cell, spend_text, tokens, tokens_json,
    usd, warnings_payload,
)


def build_report(rows: Sequence[Row], table: PriceTable, filters: Filters) -> dict[str, Any]:
    """Models ranked by spend; models without a verified price come last, with no rank and no spend figure."""
    grand = (group_rows(rows, lambda r: "") or [Group(key="")])[0]
    models, rank = [], 0
    for g in sorted(group_rows(rows, lambda r: r.canonical), key=leaderboard_key):
        known = table.get(g.key)
        entry = group_entry(g, grand.ucents)
        priced = entry["cost"] is not None
        rank += priced
        models.append({
            "rank": rank if priced else None,
            "model": g.key,
            "provider": known.provider if known else "",
            "status": table.status(g.key),
            **entry,
            # Effective price: what the tokens actually cost per million, cache reads and writes included.
            "blended_per_mtok": money_json(div_round(g.ucents * 1_000_000, g.usage.total)) if priced else None,
            "list_price_per_mtok": (
                {"input": money_json(usd_to_ucents(known.input)), "output": money_json(usd_to_ucents(known.output))}
                if known and known.priced else None
            ),
        })
    return {
        **filters.as_json(),
        "sessions": grand.session_count,
        "tokens": tokens_json(grand.usage),
        "total": money_json(grand.ucents),
        "excluded_tokens": grand.unpriced_tokens,
        "models": models,
        "warnings": warnings_payload(rows, table),
    }


def print_models(console: Console, report: dict[str, Any], filters: Filters) -> None:
    models = report["models"]
    # On a narrow console the provider goes first (the model name says it), then the list prices, then the blended one, then
    # the session count and the tokens.
    cols = [Col("#", True), Col("Model", min_width=MODEL_MIN, cut_last=True), Col("Provider", optional=1), Col("Sess", True, optional=5),
            Col("Tokens", True, optional=6), Col("Spend", True), Col("Share", True), Col("Blended\n$/1M", True, optional=4),
            Col("List in\n$/1M", True, optional=3), Col("List out\n$/1M", True, optional=2)]
    rows = []
    for m, spend in zip(models, cost_cells(models)):
        price, blended = m["list_price_per_mtok"], m["blended_per_mtok"]
        rows.append([
            str(m["rank"]) if m["rank"] else "-", m["model"], m["provider"] or "-", str(m["sessions"]),
            tokens(m["tokens"]["total"]), spend, share_cell(m, report["total"]["ucents"]),
            usd(blended["ucents"]) if blended else "-",
            usd(price["input"]["ucents"]) if price else "-", usd(price["output"]["ucents"]) if price else "-",
        ])
    spend = spend_text(report["total"], report["excluded_tokens"], report["tokens"]["total"])
    say(console, f"AgentFare models: {filters.scope()}", "bold")
    print_period_note(console, filters)
    say(console, f"{plural(len(models), 'model')}, {plural(report['sessions'], 'session')}, total spend {spend}")
    console.print()
    print_table(console, cols, rows)
    print_plus_legend(console, models, total=bool(report["excluded_tokens"]))
    print_warnings(console, report["warnings"])


def register(app: typer.Typer) -> None:
    @app.command("models")
    def models_cmd(
        ctx: typer.Context,
        week: WeekOpt = False,
        week_of: WeekOfOpt = None,
        since: SinceOpt = None,
        until: UntilOpt = None,
        project: ProjectOpt = None,
        agent: AgentOpt = None,
        as_json: JsonOpt = False,
    ) -> None:
        """Models ranked by spend: share, effective $ per 1M tokens, and the list price to compare it with."""
        st = ctx.obj
        with clean_errors():
            filters = resolve_filters(st.cfg, week=week, week_of=week_of, since=since, until=until, project=project, agent=agent)
            rows = load(st, filters)
            report = build_report(rows, st.table, filters)
            if as_json:
                emit_json(report)
            elif not rows:
                print_empty(st, filters)
            else:
                print_models(st.out, report, filters)
