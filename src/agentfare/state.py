"""Per-invocation state shared by all commands (config, price table, store, consoles)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from rich.console import Console
from rich.markup import escape

from .config import Config, validate
from .db import Store, StoreError
from .ingest import IngestReport, duplicates_warning, ingest
from .pricing import PriceTable, load_pricing
from .textsafe import clean_text


SLOW_AFTER_SECONDS = 2.0  # an agent whose own scan has taken longer than this starts to say so
APPROXIMATE_NOTE = "database upgraded: day and week splits are approximate until agentfare ingest has re-read your logs"


class IngestProgress:
    """What an ingest says on stderr while it works, in plain dim lines (no spinner, so a pipe or a log file stays clean).

    One line before the first log file is parsed into an empty database (``first_run``), and one line for an agent whose own
    scan (from its first file on) has taken longer than ``SLOW_AFTER_SECONDS``, when its next file starts: a quick agent
    says nothing, however long the agents before it took.
    """

    def __init__(self, console: Console, *, first_run: bool) -> None:
        self.console, self.first_run = console, first_run
        self.began: dict[str, float] = {}  # agent -> when its first file started
        self.named: set[str] = set()

    def __call__(self, agent: str, path: str) -> None:
        now = monotonic()
        if self.first_run:
            self.first_run = False
            self.console.print("[dim]first run: reading your agent logs into a local database; this can take a minute[/dim]")
        began = self.began.setdefault(agent, now)
        if agent not in self.named and now - began > SLOW_AFTER_SECONDS:
            self.named.add(agent)
            self.console.print(f"[dim]still reading {agent} logs...[/dim]")


@dataclass
class AppState:
    config_path: Path | None = None
    home: Path | None = None
    auto_ingest: bool = True
    out: Console = field(default_factory=Console)
    err: Console = field(default_factory=lambda: Console(stderr=True))
    _cfg: Config | None = None
    _table: PriceTable | None = None
    _store: Store | None = None
    last_ingest: IngestReport | None = None
    _warned: bool = False  # the warnings about the config file have been printed

    @property
    def cfg(self) -> Config:
        if self._cfg is None:
            cfg = Config.load(self.config_path, self.home)
            if not self._warned:
                self._warned = True
                self._warn(cfg.warnings, self.config_name(cfg))
            cfg.check()  # every setting, now: a bad one stops the commands that never ingest exactly like the ones that do
            if zone := cfg.local_tz_warning():
                self.err.print(f"[yellow]warning:[/yellow] {escape(clean_text(zone))}", soft_wrap=True)
            self._cfg = cfg
        return self._cfg

    def config_name(self, cfg: Config | None = None) -> str:
        """The config file as the warnings name it: the path as it was given, else where it was found, else the built-in one."""
        if self.config_path is not None:
            return str(self.config_path)
        cfg = cfg or self._cfg
        return str(cfg.config_path) if cfg is not None and cfg.config_path else "built-in config"

    @property
    def table(self) -> PriceTable:
        if self._table is None:
            table = load_pricing(self.cfg)
            self._warn(validate(self.cfg, table))
            self._table = table
        return self._table

    def _warn(self, messages: list[str], source: str | None = None) -> None:
        """Problems with the config file, once per run, on stderr (never in the stdout of ``--json``), naming the file."""
        for message in messages:
            self.err.print(f"[yellow]warning:[/yellow] {escape(clean_text(source or self.config_name()))}: {escape(clean_text(message))}",
                           soft_wrap=True)

    def store(self, *, ingest_first: bool | None = None) -> Store:
        """Open the database; incrementally ingest new logs first unless ``--no-ingest`` was given."""
        if self._store is None:
            store = self._open(ingest_first)
            try:
                if self.auto_ingest if ingest_first is None else ingest_first:
                    self._ingest(store)
                elif ingest_first is None and store.reread_pending():  # --no-ingest, on a database no ingest has read since its upgrade
                    self.err.print(f"[dim]{APPROXIMATE_NOTE}[/dim]", soft_wrap=True)
            except BaseException:
                store.close()  # never keep a store that skipped its ingest: the next call would hand it out silently
                raise
            self._store = store
        return self._store

    def _open(self, ingest_first: bool | None) -> Store:
        """``--no-ingest`` on a database that exists and is of this version: opened read-only and left exactly as it is, so a file
        nobody may write to (a shared one, a read-only mount) still answers. Nothing is created, upgraded or stamped. Every other
        case is the normal ``Store(path)``: it creates the database, upgrades an older one and gives a newer one's error."""
        path = self.cfg.db_path
        if ingest_first is None and not self.auto_ingest and path.is_file():
            try:
                return Store.open_read_only(path)
            except StoreError:  # an older or damaged file: the normal path upgrades it, or says what is wrong with it
                pass
        return Store(path)

    def close(self) -> None:
        """Close the database if a command opened it (the CLI calls this when the command ends)."""
        if self._store is not None:
            self._store.close()
            self._store = None

    def run_ingest(self, store: Store, agents: list[str] | None = None, *, full: bool = False) -> IngestReport:
        """Ingest the agent logs into ``store``, telling the person on stderr what a slow run is doing."""
        progress = IngestProgress(self.err, first_run=store.count() == 0)
        return ingest(self.cfg, store, agents, full=full, on_file=progress)

    def _ingest(self, store: Store) -> None:
        self.last_ingest = self.run_ingest(store)
        for msg in self.last_ingest.errors[:5]:
            self.err.print(f"[yellow]warning:[/yellow] could not parse {escape(clean_text(msg))}", soft_wrap=True)
        if len(self.last_ingest.errors) > 5:
            self.err.print(f"[yellow]warning:[/yellow] ... and {len(self.last_ingest.errors) - 5} more unreadable files")
        if self.last_ingest.duplicates:
            self.err.print(f"[yellow]warning:[/yellow] {duplicates_warning(self.last_ingest.duplicates)}", soft_wrap=True)
        if self.last_ingest.sessions:
            self.err.print(f"[dim]ingested {self.last_ingest.sessions} new/updated sessions[/dim]")
