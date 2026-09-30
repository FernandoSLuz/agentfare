"""`agentfare ingest`: scan agent logs into the local database."""

from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.markup import escape

from ..ingest import IngestReport, duplicates_warning
from ..models import AGENTS
from ..render import clean_errors, clean_text, emit_json, plural


def build_report(rep: IngestReport, database: str, database_sessions: int) -> dict[str, object]:
    """What the run did, as integers and strings: the lines of the text output and ``--json`` both come from this.

    ``sessions_read`` counts records (a session resumed in several files is read from each); ``sessions_stored`` and
    ``by_agent`` count distinct sessions; ``database.sessions`` is every distinct session the database holds."""
    return {
        "files_seen": rep.files_seen,
        "files_parsed": rep.files_parsed,
        "files_unchanged": rep.files_unchanged,
        "sessions_read": rep.sessions_read,
        "sessions_stored": rep.sessions,
        "by_agent": {agent: rep.by_agent.get(agent, 0) for agent in AGENTS},
        "database": {"path": database, "sessions": database_sessions},
        "missing_roots": rep.missing_roots,
        "errors": rep.errors,
        "files_failed": rep.files_failed,
        "duplicate_sessions": rep.duplicates,
    }


def register(app: typer.Typer) -> None:
    @app.command("ingest")
    def ingest_cmd(
        ctx: typer.Context,
        agent: Annotated[Optional[str], typer.Option("--agent", "-a", metavar="AGENT", help=f"Only this agent: {', '.join(AGENTS)}.")] = None,
        full: Annotated[bool, typer.Option("--full", help="Re-parse every file, even unchanged ones.")] = False,
        as_json: Annotated[bool, typer.Option("--json", help="Print what the run did as JSON: counts, problems and the database, stable keys.")] = False,
    ) -> None:
        """Scan Claude Code, Codex CLI and Gemini CLI logs into the local database (incremental)."""
        st = ctx.obj
        if agent and agent not in AGENTS:
            raise typer.BadParameter(f"expected one of: {', '.join(AGENTS)}")
        with clean_errors():
            store = st.store(ingest_first=False)
            rep = st.run_ingest(store, [agent] if agent else None, full=full)
            held = store.count()
            report = build_report(rep, str(st.cfg.db_path), held)
            for miss in rep.missing_roots:
                st.err.print(f"[dim]not found (skipped): {escape(clean_text(miss))}[/dim]", soft_wrap=True)
            for err in rep.errors:
                st.err.print(f"[yellow]warning:[/yellow] {escape(clean_text(err))}", soft_wrap=True)
            if rep.duplicates:
                st.err.print(f"[yellow]warning:[/yellow] {duplicates_warning(rep.duplicates)}", soft_wrap=True)
            if as_json:
                emit_json(report)
                return
            by_agent = ", ".join(f"{a}: {n}" for a, n in sorted(rep.by_agent.items())) or "none"
            # soft_wrap on everything: Rich never breaks an item such as a path (or this sentence) in two.
            st.out.print(f"read {plural(rep.sessions_read, 'session record')} from {plural(rep.files_seen, 'file')} "
                         f"({rep.files_parsed} parsed, {rep.files_unchanged} unchanged); "
                         f"the database holds {plural(held, 'distinct session')}", soft_wrap=True)
            st.out.print(f"{plural(rep.sessions, 'session')} stored ({by_agent})", soft_wrap=True)
            st.out.print(f"database: {escape(clean_text(str(st.cfg.db_path)))}", soft_wrap=True)
            if rep.files_failed:
                st.out.print(f"{plural(rep.files_failed, 'file')} could not be read (the warnings on stderr say which)", soft_wrap=True)
