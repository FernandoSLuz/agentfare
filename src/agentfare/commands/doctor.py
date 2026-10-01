"""`agentfare doctor`: a checklist of the install, the config, the logs, the data and the prices, with a fix for every problem.

doctor only looks. It never opens a log file (it counts the files a scan would read and asks the file system for their
size), never writes anything (the database is opened read-only and nothing is created), never runs an agent (a command
is looked up on PATH) and works on a machine where nothing is configured yet. A check that cannot run turns into a failed
line instead of a traceback, so the rest of the list is always there.
"""

from __future__ import annotations

import importlib.metadata as metadata
import json
import os
import shlex
import shutil
import sqlite3
import sys
import sysconfig
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import typer
from rich.text import Text

from .. import __version__
from ..config import Config, ConfigError, now_utc, validate
from ..db import SCHEMA_VERSION, Store, StoreError
from ..models import AGENTS
from ..pricing import PriceTable, PricingError, builtin_path, load_pricing
from ..render import emit_json, plain, plural
from ..setup_checks import (
    AgentScan, RELOCATION_VARIABLES, count_text, human_size, mode_text, nearest_existing, open_to_others, scan_agents, tilde,
    wrap_text,
)
from ..state import AppState

OK, WARN, FAIL = "ok", "warn", "fail"
RANK = {OK: 0, WARN: 1, FAIL: 2}
STYLE = {OK: "green", WARN: "yellow", FAIL: "bold red"}
STALE_DAYS = 30  # a price checked longer ago than this is worth checking again
INSTALL_SOURCE = "git+https://github.com/FernandoSLuz/agentfare"
ISSUES = "https://github.com/FernandoSLuz/agentfare/issues"
NETWORK = ("agentfare never uses the network, except agentfare prices --refresh (the only command that opens a connection). "
           "doctor itself opens no log file and writes nothing.")
HELP = """Check the install, the config, the log folders, the data and the prices, and say how to fix what is off.

Every line is ok, warn or fail, and every line that is not ok comes with a concrete fix. The exit code is 0 unless a \
check fails (warnings do not fail), and 1 otherwise. doctor only looks: it never opens a log file, writes nothing (not \
even the data directory), never starts an agent and works on a machine where nothing is configured yet."""


@dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str
    fix: str | None = None  # what to do about it: every check that is not ok has one
    notes: list[str] = field(default_factory=list)  # more about the problem, printed under it
    data: dict[str, Any] = field(default_factory=dict)  # the facts, for --json

    def worsen(self, status: str, fix: str, note: str = "") -> None:
        """Raise the status to ``status`` (never lower it) and add ``fix`` and ``note``."""
        if RANK[status] > RANK[self.status]:
            self.status = status
        self.fix = f"{self.fix} {fix}" if self.fix else fix
        if note:
            self.notes.append(note)

    def as_json(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail,
                "fix": None if self.status == OK else self.fix, "notes": self.notes, "data": self.data}


def _quote(path: str | os.PathLike[str]) -> str:
    return shlex.quote(os.fspath(path))


# ---- the install -----------------------------------------------------------------------------------------------------
def _package_dir() -> Path:
    return Path(__file__).parent.parent


def _installer() -> str:
    """``uv``, ``pipx`` or ``pip``: the tool whose environment this is, from where the interpreter lives."""
    prefix = sys.prefix.replace("\\", "/")
    return "uv" if "/uv/tools/" in prefix else "pipx" if "/pipx/venvs/" in prefix else "pip"


def _editable(dist: Any) -> bool:
    """Whether the installed copy points at a source folder (``pip install -e``): its ``direct_url.json`` says so."""
    try:
        info = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        return False
    return isinstance(info, dict) and isinstance(info.get("dir_info"), dict) and bool(info["dir_info"].get("editable"))


def _upgrade_hint(editable: bool) -> str:
    if editable:
        return "an editable install follows its folder: update the clone it points at (git pull)"
    return {"uv": "uv tool upgrade agentfare", "pipx": "pipx upgrade agentfare"}.get(
        _installer(), f"python -m pip install --upgrade {INSTALL_SOURCE}")


def _path_fix() -> str:
    installer = _installer()
    if installer == "uv":
        return "run uv tool update-shell and open a new terminal: it puts the folder uv installs tools into on PATH"
    if installer == "pipx":
        return "run pipx ensurepath and open a new terminal"
    scripts = sysconfig.get_path("scripts") or "the folder the install put the command in"
    return f"add {tilde(scripts)} (the folder the install put the command in) to PATH in your shell profile, or run it as python -m agentfare"


# ---- the checks -------------------------------------------------------------------------------------------------------
class Doctor:
    """One run of the checklist. Later checks use what earlier ones found (the config, the scans, the database, the table)."""

    def __init__(self, st: AppState) -> None:
        self.st = st
        self.cfg: Config = Config.for_init(st.config_path, st.home)  # the defaults, until the config file has been read
        self.loaded = False  # the config file was read and passed every check
        self.config_check: Check | None = None
        self.scans: list[AgentScan] = []
        self.models: list[str] = []  # the models the database holds
        self.table: PriceTable | None = None

    # -- running ---------------------------------------------------------------------------------------------------------
    def run(self) -> list[Check]:
        found: list[Check] = []
        found += self.one("python", "Python", self._python)
        found += self.one("package", "Package", self._package)
        found += self.one("command", "agentfare on PATH", self._command)
        found += self.one("config", "Config file", self._config)
        found += self.many([(f"logs.{a}", f"{a} logs") for a in AGENTS] + [("logs", "Session logs")], self._logs)
        found += self.many([(f"cli.{a}", f"{a} on PATH") for a in AGENTS], self._cli)
        found += self.one("data_dir", "Data directory", self._data_dir)
        found += self.one("database", "Database", self._database)
        found += self.one("price_table", "Price table", self._prices)
        found += self.one("price_models", "Models in use", self._price_models)
        found += self.one("environment", "Environment", self._environment)
        found += self.one("terminal", "Terminal", self._terminal)
        found += self.one("network", "Network", self._network)
        return found

    def one(self, ident: str, title: str, check: Callable[[], Check]) -> list[Check]:
        return self.many([(ident, title)], lambda: [check()])

    def many(self, wanted: list[tuple[str, str]], check: Callable[[], list[Check]]) -> list[Check]:
        """``check()``, or, when it breaks, one failed line per check it should have produced."""
        try:
            return check()
        except Exception as exc:  # a bug in a check, or something odd on this machine: said as a line, never a traceback
            what = f"{type(exc).__name__}: {exc}"
            return [Check(ident, title, FAIL, f"this check could not run ({what})",
                          f"this is a bug in agentfare doctor: please report it at {ISSUES}") for ident, title in wanted]

    def today(self) -> date:
        try:
            return now_utc().date()
        except ConfigError:  # AGENTFARE_NOW is not a timestamp: the environment line says so, the dates use the real clock
            return datetime.now(timezone.utc).date()

    # -- Python and the install ------------------------------------------------------------------------------------------
    def _python(self) -> Check:
        major, minor, micro = sys.version_info[0], sys.version_info[1], sys.version_info[2]
        version = f"{major}.{minor}.{micro}"
        detail = f"{version} ({tilde(sys.executable) if sys.executable else 'unknown path'})"
        data = {"version": version, "executable": sys.executable or ""}
        if (major, minor) < (3, 11):
            return Check("python", "Python", FAIL, f"{detail}: AgentFare needs Python 3.11 or newer",
                         f"install Python 3.11 or newer and install AgentFare with it, for example uv tool install --python 3.12 {INSTALL_SOURCE}", data=data)
        return Check("python", "Python", OK, detail, data=data)

    def _package(self) -> Check:
        location = _package_dir()
        try:
            dist: Any = metadata.distribution("agentfare")
        except metadata.PackageNotFoundError:
            dist = None
        data: dict[str, Any] = {"version": __version__, "location": str(location), "installed": dist is not None, "editable": False}
        if dist is None:
            return Check("package", "Package", OK, f"agentfare {__version__}, running from a source tree at {tilde(location)}, not installed", data=data)
        installed_copy = any(part in ("site-packages", "dist-packages") for part in location.parts)
        editable = _editable(dist) or not installed_copy
        data.update(editable=editable, installed_by=_installer())
        check = Check("package", "Package", OK, f"agentfare {__version__}, {'editable install' if editable else 'install'} at {tilde(location)}",
                      notes=[f"update with: {_upgrade_hint(editable)}"], data=data)
        if dist.version != __version__:
            check.detail += f"; the installed metadata says {dist.version}"
            check.worsen(WARN, "reinstall so that the metadata and the code agree: pip install --force-reinstall --no-deps . in the clone, "
                               f"or python -m pip install --force-reinstall --no-deps {INSTALL_SOURCE}")
        return check

    def _command(self) -> Check:
        found = shutil.which("agentfare")
        running = sys.argv[0] if sys.argv else ""
        data = {"path": found, "running": running if Path(running).name == "agentfare" else None}
        if not found:
            # `python -m agentfare` needs the interpreter that has agentfare: for uv and pipx that is the one inside the tool's own
            # environment, not the `python` on PATH, so it is named in full.
            runner = "python" if _installer() == "pip" else (tilde(sys.executable) if sys.executable else "python")
            return Check("command", "agentfare on PATH", WARN, f"the agentfare command is not on PATH ({runner} -m agentfare still works)", _path_fix(), data=data)
        if Path(running).name == "agentfare" and os.path.realpath(running) != os.path.realpath(found):
            return Check("command", "agentfare on PATH", WARN,
                         f"PATH finds {tilde(found)} first, but this is {tilde(running)}: another install shadows it",
                         "keep one of them (pip uninstall agentfare, or uv tool uninstall agentfare) or put the folder of the one you want first on PATH", data=data)
        return Check("command", "agentfare on PATH", OK, tilde(found), data=data)

    # -- the config file ---------------------------------------------------------------------------------------------------
    def _config_home(self) -> str:
        """Where the config file is, or would be."""
        return tilde(self.cfg.config_path or self.cfg.home / "config.yaml")

    def _config(self) -> Check:
        st = self.st
        target = self.cfg.default_init_target
        data: dict[str, Any] = {"path": str(target), "found": os.path.lexists(target)}
        problem = shown = ""  # what is wrong, and how it reads in the line (a message from Config.load already names the file)
        try:
            loaded = Config.load(st.config_path, st.home)
        except ConfigError as exc:
            problem = shown = str(exc)
        else:
            try:
                loaded.check()  # every setting a command would otherwise meet only when it gets to use it
            except ConfigError as exc:
                problem, shown = str(exc), f"{tilde(target)}: {exc}"
        if problem:
            if not os.path.lexists(target):
                fix = (f"create it with agentfare init --write-config (or agentfare config --init), or drop --config and AGENTFARE_CONFIG to use "
                       f"{tilde(self.cfg.home / 'config.yaml')} or the built-in defaults")
            else:
                fix = (f"correct what the message names in {tilde(target)} ({problem}). agentfare config --default prints every setting with "
                       "its default; agentfare config --init --force replaces the file with the defaults (keep a copy first)")
            self.config_check = Check("config", "Config file", FAIL, shown, fix, data=data)
            return self.config_check
        self.cfg, self.loaded = loaded, True
        where = tilde(loaded.config_path) if loaded.config_path else ""
        detail = where or "none: the built-in defaults are used (that is fine; agentfare init --write-config writes one to edit)"
        check = Check("config", "Config file", OK, detail, data={**data, "found": loaded.config_path is not None})
        if loaded.warnings:
            check.notes += loaded.warnings
            check.detail = f"{where}: {plural(len(loaded.warnings), 'setting')} nothing reads"
            check.worsen(WARN, "fix or remove the keys named above; agentfare config --default prints every valid key")
        if zone := loaded.local_tz_warning():
            check.notes.append(zone)
            check.worsen(WARN, "set timezone in the config to an IANA name such as America/Toronto")
        self.config_check = check
        return check

    # -- the agents' logs and commands ------------------------------------------------------------------------------------
    def _logs(self) -> list[Check]:
        self.scans = scans = scan_agents(self.cfg)
        checks = [self._agent_logs(scan) for scan in scans]
        total = sum(scan.files for scan in scans)
        summary = Check("logs", "Session logs", OK, f"{count_text(total, 'session file')} across {plural(sum(s.found for s in scans), 'agent')}",
                        data={"files": total, "bytes": sum(s.size for s in scans)})
        if not total:
            summary.detail = "no session files found for any agent"
            summary.worsen(WARN, f"use Claude Code, Codex CLI or Gemini CLI once, or, if their logs live somewhere else, set sources.<agent> in {self._config_home()} "
                                 "(agentfare init --write-config creates it); then run agentfare init. agentfare demo shows the tool working on sample sessions meanwhile")
        if not self.loaded:
            summary.notes.append("the config could not be read, so the default folders were checked (see the Config file line)")
        return [*checks, summary]

    def _agent_logs(self, scan: AgentScan) -> Check:
        agent = scan.agent
        folders = ", ".join(tilde(root.path) for root in scan.roots)
        data = {"origin": scan.origin, "files": scan.files, "bytes": scan.size, "truncated": scan.truncated,
                "folders": [root.as_json() for root in scan.roots]}
        ident, title = f"logs.{agent}", f"{agent} logs"
        if blocked := scan.unreadable:
            root = blocked[0]
            return Check(ident, title, FAIL, f"cannot read {tilde(root.path)}: {root.error}",
                         f"give your user read access (chmod -R u+rX {_quote(root.path)}) or point sources.{agent} at a copy you can read", data=data)
        if scan.found:
            more = "at least " if scan.truncated else ""
            return Check(ident, title, OK, f"found {more}{count_text(scan.files, 'file')}, {human_size(scan.size)} in {folders}", data=data)
        origin = f"sources.{agent} in {self._config_home()}" if scan.origin == "config" else scan.origin
        if any(root.exists for root in scan.roots):
            return Check(ident, title, WARN, f"{folders} exists but holds no session files yet",
                         f"use {agent} once so that it writes a session, or, if its logs live somewhere else, set sources.{agent} in {self._config_home()}", data=data)
        if scan.origin != "default":
            return Check(ident, title, WARN, f"{folders} does not exist (from {origin})",
                         f"check the path in {'sources.' + agent if scan.origin == 'config' else scan.origin}: it should be the folder {agent} writes its session logs to", data=data)
        if scan.cli_path:
            return Check(ident, title, WARN, f"no log folder yet ({folders}) although the {agent} command is installed",
                         f"run {agent} once so that it writes a session; if it keeps its logs somewhere else, set sources.{agent} in {self._config_home()}", data=data)
        return Check(ident, title, OK, "not used here (no log folder, no command on PATH)", data=data)

    def _cli(self) -> list[Check]:
        return [Check(f"cli.{scan.agent}", f"{scan.agent} on PATH", OK,
                      tilde(scan.cli_path) if scan.cli_path else "not on PATH (only agentfare run needs it)",
                      data={"commands": list(scan.cli_names), "path": scan.cli_path}) for scan in self.scans]

    # -- the data directory and the database -----------------------------------------------------------------------------
    def _data_dir(self) -> Check:
        home = self.cfg.home
        data: dict[str, Any] = {"path": str(home), "exists": os.path.lexists(home), "mode": None, "writable": None}
        outside = "point AGENTFARE_HOME (or --home) at a folder you own"
        if not data["exists"]:
            anchor = nearest_existing(home)
            if not anchor.is_dir() or not os.access(anchor, os.W_OK | os.X_OK):
                why = "is not a folder" if not anchor.is_dir() else "is not writable"
                return Check("data_dir", "Data directory", FAIL, f"{tilde(home)} cannot be created: {tilde(anchor)} {why}",
                             f"make {_quote(anchor)} writable, or {outside}", data=data)
            return Check("data_dir", "Data directory", OK, f"{tilde(home)} not created yet: AgentFare creates it (private, 0700) the first time it needs it", data=data)
        if not home.is_dir():
            return Check("data_dir", "Data directory", FAIL, f"{tilde(home)} exists but is not a folder",
                         f"move or delete that file, or {outside}", data=data)
        data.update(mode=mode_text(home), writable=os.access(home, os.W_OK | os.X_OK))
        if not data["writable"]:
            return Check("data_dir", "Data directory", FAIL, f"{tilde(home)} is not writable (permissions {data['mode']})",
                         f"chmod u+rwx {_quote(home)}, or {outside}", data=data)
        if open_to_others(data["mode"]):
            return Check("data_dir", "Data directory", WARN, f"{tilde(home)} has permissions {data['mode']}: other users can look inside it",
                         f"chmod 700 {_quote(home)} (it holds project names and token counts)", data=data)
        return Check("data_dir", "Data directory", OK, f"{tilde(home)} (permissions {data['mode']})", data=data)

    def _database(self) -> Check:
        path = self.cfg.db_path
        data: dict[str, Any] = {"path": str(path), "exists": os.path.lexists(path), "mode": None, "writable": None, "schema": None,
                                "sessions": None, "last_stored": None, "reread_pending": None}
        if not data["exists"]:
            return Check("database", "Database", OK, f"{tilde(path)} does not exist yet: agentfare init (or the first report) creates it", data=data)
        if not path.is_file():
            return Check("database", "Database", FAIL, f"{tilde(path)} is not a file", "move it away, or set database in the config to another path", data=data)
        data.update(mode=mode_text(path), writable=os.access(path, os.W_OK))
        try:
            facts = _inspect(path)
        except StoreError as exc:
            return Check("database", "Database", FAIL, str(exc), "run agentfare ingest, or move the file away and let AgentFare rebuild it", data=data)
        except sqlite3.DatabaseError as exc:
            if type(exc) is sqlite3.DatabaseError:  # not a database, or malformed: the one case where starting over is the answer
                return Check("database", "Database", FAIL, f"{tilde(path)} is not a database ({exc})",
                             f"delete {_quote(path)}: it is rebuilt from the agent logs that still exist (run agentfare init)", data=data)
            return Check("database", "Database", FAIL, f"cannot open {tilde(path)} read-only: {exc}",
                         "check that you may read it and that no other program holds it", data=data)
        version = facts["version"]
        data["schema"] = version
        if version > SCHEMA_VERSION:
            return Check("database", "Database", FAIL, f"{tilde(path)} was written by a newer agentfare (schema {version}, this one reads {SCHEMA_VERSION})",
                         f"upgrade agentfare ({_upgrade_hint(False)}), or move the file away and start over", data=data)
        if version == 0 and not facts["tables"]:
            return Check("database", "Database", OK, f"{tilde(path)} is an empty file: agentfare init fills it", data=data)
        if version < SCHEMA_VERSION:
            detail = f"{tilde(path)} has schema {version}, older than the {SCHEMA_VERSION} this agentfare uses: the next command that opens it upgrades it in place"
            return Check("database", "Database", WARN, detail, "run agentfare ingest: it upgrades the file and re-reads your logs", data=data)
        data.update(sessions=facts["sessions"], last_stored=facts["last"], reread_pending=facts["reread"])
        self.models = facts["models"]
        sessions = facts["sessions"]
        ago = _ago(facts["last"], self.today()) if facts["last"] else ""
        last = f", last stored {facts['last']}{f' ({ago})' if ago else ''}" if facts["last"] else ""
        check = Check("database", "Database", OK,
                      f"{tilde(path)}: {plural(sessions, 'session')}, schema {version}, permissions {data['mode']}{last}", data=data)
        if open_to_others(data["mode"]):
            check.worsen(WARN, f"chmod 600 {_quote(path)} (it holds project names and token counts)")
        if not data["writable"]:
            check.detail += "; it is read-only, so AgentFare cannot add new sessions"
            check.worsen(WARN, f"chmod u+w {_quote(path)} to let AgentFare update it, or use the global --no-ingest option to read it as it is")
        if facts["reread"]:
            check.detail += "; the logs still have to be re-read after an upgrade, so day and week splits are approximate until then"
            check.worsen(WARN, "run agentfare ingest")
        if not sessions and any(scan.found for scan in self.scans):
            check.detail += "; it holds no sessions although log files exist"
            check.worsen(WARN, "run agentfare init (or agentfare ingest) to read them")
        elif not sessions:
            check.detail = f"{tilde(path)}: no sessions yet, schema {version}, permissions {data['mode']}"
        return check

    # -- the prices --------------------------------------------------------------------------------------------------------
    def _prices(self) -> Check:
        override = self.cfg.pricing_override_path
        try:
            table = load_pricing(self.cfg)
        except PricingError as exc:
            fix = (f"fix the YAML in {tilde(override)} (the message names the problem), or delete it to go back to the built-in table"
                   if os.path.lexists(override) else
                   f"create {tilde(override)}, or remove pricing_file from the config to use the built-in table")
            return Check("price_table", "Price table", FAIL, str(exc), fix, data={"source": None, "path": str(override), "models": 0,
                                                                                    "unverified": [], "oldest_checked": None, "stale": 0})
        self.table = table
        builtin = table.path is None or table.path == builtin_path()
        models = table.models
        unverified = sorted(m for m, p in models.items() if not p.priced)
        dates: dict[str, date] = {}
        undated: list[str] = []
        for name, price in sorted(models.items()):
            try:
                dates[name] = date.fromisoformat(price.checked.strip())
            except ValueError:
                undated.append(name)
        today = self.today()
        oldest = min(dates.values()) if dates else None
        stale = sorted(m for m, d in dates.items() if (today - d).days > STALE_DAYS)
        source = "built-in table" if builtin else f"override file {tilde(table.path or override)} (it replaces the built-in table)"
        detail = f"{source}, {plural(len(models), 'model')}"
        if oldest:
            detail += f", oldest price check {oldest.isoformat()} ({_days_ago((today - oldest).days)})"
        if unverified:
            detail += f"; {len(unverified)} without a verified price, left out of dollar totals: {_names(unverified)}"
        data = {"source": "builtin" if builtin else "override", "path": str(table.path or builtin_path()), "models": len(models),
                "unverified": unverified, "oldest_checked": oldest.isoformat() if oldest else None, "stale": len(stale)}
        check = Check("price_table", "Price table", OK, detail, data=data)
        if stale:
            check.detail += f"; {len(stale)} of {len(models)} last checked more than {STALE_DAYS} days ago"
            check.worsen(WARN, "run agentfare prices --refresh to re-check them against the official pages (the only command that uses the network), "
                               "then add --write to record the date. A price that changed is never edited for you: correct it by hand")
        if undated:
            check.detail += f"; no check date for {_names(undated)}"
            check.worsen(WARN, f"add a checked: date to each of those models in {tilde(table.path or override)} once you have read its price on the provider's page")
        if self.config_check is not None and self.loaded:
            for message in validate(self.cfg, table):  # the router points at a model the table cannot price: the config's problem
                self.config_check.notes.append(message)
                self.config_check.worsen(WARN, "choose a model with a verified price in router.recommend and router.run.models, or verify its price")
        return check

    def _price_models(self) -> Check:
        data: dict[str, Any] = {"models": len(self.models), "without_price": []}
        if self.table is None:
            return Check("price_models", "Models in use", OK, "not checked: the price table could not be loaded (see the Price table line)", data=data)
        if not self.models:
            return Check("price_models", "Models in use", OK, "no sessions stored yet, so there are no models to check", data=data)
        bad = {m: self.table.status(m) for m in self.models if self.table.status(m) != "priced"}
        data["without_price"] = sorted(bad)
        if not bad:
            return Check("price_models", "Models in use", OK, f"{plural(len(self.models), 'model')} in your sessions, all with a verified price", data=data)
        label = {"unverified": "unverified", "unknown": "not in the table"}
        names = ", ".join(f"{m} ({label.get(status, status)})" for m, status in sorted(bad.items()))
        override = self.cfg.pricing_override_path
        where = (f"add each one to {tilde(override)}" if os.path.lexists(override) else
                 f"copy {tilde(builtin_path())} to {tilde(override)} (the file replaces the built-in table) and add each one there")
        return Check("price_models", "Models in use", WARN, f"{plural(len(bad), 'model')} in your sessions without a verified price: {names}",
                     f"their spend is left out of every dollar total. Read the price on the provider's page, then {where} "
                     "(CONTRIBUTING.md shows the format); agentfare prices --unverified lists the ones the table already marks", data=data)

    # -- the environment ---------------------------------------------------------------------------------------------------
    def _environment(self) -> Check:
        names = ("AGENTFARE_HOME", "AGENTFARE_CONFIG", "AGENTFARE_NOW", *RELOCATION_VARIABLES.values())
        present = {name: os.environ[name] for name in names if os.environ.get(name, "").strip()}
        data = {"set": present}
        if not present:
            return Check("environment", "Environment", OK, "no AgentFare variable is set (the defaults are in use)", data=data)
        check = Check("environment", "Environment", OK, "set: " + ", ".join(f"{name}={tilde(value)}" for name, value in present.items()), data=data)
        if "AGENTFARE_NOW" in present:
            try:
                now_utc()
            except ConfigError as exc:
                check.status, check.fix = FAIL, "unset AGENTFARE_NOW, or set it to a timestamp such as 2026-09-30T12:00:00Z (it is meant for tests and demos)"
                check.detail = f"{exc}; {check.detail}"
            else:
                check.detail += f"; AGENTFARE_NOW freezes the clock at {present['AGENTFARE_NOW']}"
                check.worsen(WARN, "unset AGENTFARE_NOW unless you are testing: every this-week view, the dates in a report and the age of the prices use that moment")
        for scan in self.scans:
            variable = RELOCATION_VARIABLES.get(scan.agent, "")
            if variable in present and scan.origin == "config":
                check.notes.append(f"{variable} is ignored for {scan.agent}: sources.{scan.agent} in the config takes over")
        return check

    def _terminal(self) -> Check:
        console = self.st.out
        encoding = (console.encoding or "unknown").lower()
        width = console.width
        colour = console.color_system
        how = ("colour off (NO_COLOR is set)" if os.environ.get("NO_COLOR") else "plain text (output is not a terminal)" if not console.is_terminal
               else f"colour ({colour})" if colour else "plain text")
        data = {"encoding": encoding, "width": width, "colour": bool(colour), "is_terminal": console.is_terminal}
        check = Check("terminal", "Terminal", OK, f"{encoding}, {width} columns, {how}", data=data)
        if not encoding.startswith("utf"):
            check.worsen(WARN, "set a UTF-8 locale, for example export LC_ALL=C.UTF-8 (or PYTHONIOENCODING=utf-8): until then the tables fall back to ASCII (~, #, ->)")
        if width < 80:
            check.worsen(WARN, "make the terminal at least 80 columns wide: narrower, AgentFare drops columns from its tables (--json always has everything)")
        return check

    def _network(self) -> Check:
        return Check("network", "Network", OK, NETWORK)


# ---- helpers ---------------------------------------------------------------------------------------------------------------
def _inspect(path: Path) -> dict[str, Any]:
    """What a database holds, read through a read-only connection: nothing is created, upgraded or stamped."""
    with closing(sqlite3.connect(f"file:{quote(str(path.absolute()))}?mode=ro", uri=True, timeout=1)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    facts: dict[str, Any] = {"version": version, "tables": tables, "sessions": None, "last": None, "models": [], "reread": False}
    if version == SCHEMA_VERSION:
        with Store.open_read_only(path) as store:
            facts["sessions"] = store.count()
            facts["last"] = store.conn.execute("SELECT MAX(ingested_at) FROM sessions").fetchone()[0]
            facts["models"] = [row[0] for row in store.conn.execute("SELECT DISTINCT model FROM session_usage ORDER BY model")]
            facts["reread"] = store.reread_pending()
    return facts


def _names(names: list[str], shown: int = 5) -> str:
    return ", ".join(names[:shown]) + (f" and {len(names) - shown} more" if len(names) > shown else "")


def _days_ago(days: int) -> str:
    """``today``, ``1 day ago``, ``31 days ago``."""
    return "today" if days <= 0 else plural(days, "day") + " ago"


def _ago(stamp: str, today: date) -> str:
    """``2 days ago`` for an ISO timestamp, in whole days of the calendar (``today`` if it was today)."""
    try:
        return _days_ago((today - date.fromisoformat(stamp[:10])).days)
    except ValueError:
        return ""


# ---- the output ------------------------------------------------------------------------------------------------------------------
def _summary(checks: list[Check]) -> dict[str, int]:
    return {status: sum(c.status == status for c in checks) for status in (OK, WARN, FAIL)}


def _print(st: AppState, checks: list[Check]) -> None:
    console = st.out
    width = max(40, min(console.width, 100))
    title = max(len(c.title) for c in checks)
    lead = 6 + title + 2
    pad = " " * lead

    def say(text: str, style: str = "") -> None:
        console.print(Text(plain(text, console), style=style), soft_wrap=True)

    say("AgentFare doctor", "bold")
    say("read-only: it opens no log file and writes nothing", "dim")
    console.print()
    for check in checks:
        first, *rest = wrap_text(check.detail, width - lead)
        row = Text()
        row.append(f"{check.status:<6}", style=STYLE[check.status])
        row.append(plain(f"{check.title:<{title}}  {first}", console))
        console.print(row, soft_wrap=True)
        for line in rest:
            say(pad + line)
        if check.status == OK:
            continue
        for note in check.notes:
            for line in wrap_text(note, width - lead):
                say(pad + line, "dim")
        for line in wrap_text("fix: " + (check.fix or ""), width - lead, later="     "):
            say(pad + line)
    counts = _summary(checks)
    console.print()
    say(f"Result: {counts[OK]} ok, {plural(counts[WARN], 'warning')}, {counts[FAIL]} failed", "bold" if counts[FAIL] else "")


def register(app: typer.Typer) -> None:
    @app.command("doctor", help=HELP)
    def doctor_cmd(
        ctx: typer.Context,
        as_json: Annotated[bool, typer.Option("--json", help="Print the checks as JSON: id, status, detail, fix and facts, stable keys.")] = False,
    ) -> None:
        st: AppState = ctx.obj
        checks = Doctor(st).run()
        counts = _summary(checks)
        if as_json:
            emit_json({"ok": not counts[FAIL], "version": __version__, "summary": counts, "checks": [c.as_json() for c in checks]})
        else:
            _print(st, checks)
        if counts[FAIL]:
            raise typer.Exit(1)
