"""What ``agentfare init`` and ``agentfare doctor`` find out about a machine, shared so that the two always agree.

Nothing here opens a log file. A scan lists the files that the parsers would read (``parser.discover``, so the count is
the one an ingest sees) and asks the file system for their size. The walk is capped in files and in time, so that a
huge folder costs a second or two at most and the answer says it stopped early. The agent command lines are only looked
up on ``PATH`` (``shutil.which``) and never run.
"""

from __future__ import annotations

import os
import shutil
import stat
import textwrap
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from .config import _RELOCATION, DEFAULT_CONFIG, Config
from .models import AGENTS
from .parsers import get_parser
from .textsafe import clean_text

AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex CLI", "gemini": "Gemini CLI"}
RELOCATION_VARIABLES = {agent: variable for agent, (variable, _) in _RELOCATION.items()}
SCAN_FILE_CAP = 100_000  # a folder with more session files than this is counted up to it, and said to hold more
SCAN_SECONDS = 5.0  # and so is one that takes longer than this to walk


# ---- text -------------------------------------------------------------------------------------------------------
def tilde(path: str | os.PathLike[str]) -> str:
    """``path`` with the home directory written as ``~``: the way a person writes it, and it keeps pasted output private."""
    text = os.fspath(path)
    home = os.path.expanduser("~").rstrip("/")
    if len(home) > 1 and (text == home or text.startswith(home + "/")):
        return "~" + text[len(home):]
    return text


def human_size(n: int) -> str:
    """``940 B``, ``1.2 KB``, ``340 MB``, ``1.2 GB``: decimal units, one decimal below ten of a unit, integer arithmetic."""
    if n < 1000:
        return f"{n} B"
    units = ((10**3, "KB"), (10**6, "MB"), (10**9, "GB"), (10**12, "TB"))
    for i, (size, unit) in enumerate(units):
        whole = (n + size // 2) // size
        if whole < 1000 or i == len(units) - 1:
            tenths = (n * 10 + size // 2) // size
            return f"{tenths // 10}.{tenths % 10} {unit}" if tenths < 100 else f"{whole} {unit}"
    raise AssertionError("unreachable")  # pragma: no cover


def count_text(n: int, word: str) -> str:
    """``1 file``, ``204 files``, ``30,000 files``: a count with its noun, thousands separated."""
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def wrap_text(text: str, width: int, *, first: str = "", later: str = "") -> list[str]:
    """``text`` in lines of at most ``width`` characters, a word (a path) never broken: the first line starts with ``first``, the
    others with ``later``."""
    return textwrap.wrap(clean_text(text), max(width, 20), initial_indent=first, subsequent_indent=later,
                         break_long_words=False, break_on_hyphens=False) or [first.rstrip()]


def mode_text(path: str | os.PathLike[str]) -> str | None:
    """``0700``: the permission bits of ``path``, or None when it cannot be looked at."""
    try:
        return f"{stat.S_IMODE(os.stat(path).st_mode):04o}"
    except OSError:
        return None


def open_to_others(mode: str | None) -> bool:
    """True when ``mode`` (``0755``) gives anything to the group or to everybody: what AgentFare stores is for its owner."""
    return mode is not None and int(mode, 8) & 0o077 != 0


def nearest_existing(path: Path) -> Path:
    """``path``, or its closest parent that exists."""
    here = path
    while not os.path.lexists(here) and here.parent != here:
        here = here.parent
    return here


# ---- one folder, one agent --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class RootScan:
    """One log folder: whether it is there and can be read, and how many session files it holds."""

    path: Path
    exists: bool
    readable: bool
    files: int = 0
    size: int = 0
    truncated: bool = False  # the walk stopped at the cap or at the time limit, so there is more
    error: str = ""

    def as_json(self) -> dict[str, Any]:
        return {"path": str(self.path), "exists": self.exists, "readable": self.readable, "files": self.files,
                "bytes": self.size, "truncated": self.truncated, "error": self.error or None}


@dataclass(frozen=True)
class AgentScan:
    agent: str
    origin: str  # "default", the agent's own variable (CLAUDE_CONFIG_DIR), or "config" (sources.<agent> in a file)
    roots: tuple[RootScan, ...]
    cli_names: tuple[str, ...]
    cli_path: str | None

    @property
    def label(self) -> str:
        return AGENT_LABELS.get(self.agent, self.agent)

    @property
    def files(self) -> int:
        return sum(r.files for r in self.roots)

    @property
    def size(self) -> int:
        return sum(r.size for r in self.roots)

    @property
    def truncated(self) -> bool:
        return any(r.truncated for r in self.roots)

    @property
    def found(self) -> bool:
        return self.files > 0

    @property
    def unreadable(self) -> list[RootScan]:
        return [r for r in self.roots if r.exists and not r.readable]

    def as_json(self) -> dict[str, Any]:
        return {"agent": self.agent, "label": self.label, "origin": self.origin, "found": self.found, "files": self.files,
                "bytes": self.size, "truncated": self.truncated, "folders": [r.as_json() for r in self.roots],
                "cli": {"commands": list(self.cli_names), "path": self.cli_path}}


def _describe(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return f"{type(exc).__name__}: {exc.strerror}"
    return f"{type(exc).__name__}: {exc}"


def scan_root(agent: str, root: Path, *, cap: int = SCAN_FILE_CAP, seconds: float = SCAN_SECONDS) -> RootScan:
    """Count the session files of ``agent`` under ``root`` and add up their sizes, without opening one."""
    try:
        if not root.exists():
            return RootScan(root, exists=False, readable=False)
    except OSError as exc:  # before Python 3.14 a folder inside one nobody may search raises here
        return RootScan(root, exists=True, readable=False, error=clean_text(_describe(exc)))
    if not root.is_dir():
        return RootScan(root, exists=True, readable=False, error="not a folder")
    if not os.access(root, os.R_OK | os.X_OK):
        return RootScan(root, exists=True, readable=False, error="PermissionError: Permission denied")
    files = size = 0
    truncated = False
    error = ""
    began = monotonic()
    try:
        for path in get_parser(agent).discover(root):
            if files >= cap:  # one more file exists, so the cap really cut the count short
                truncated = True
                break
            files += 1
            try:
                size += os.stat(path).st_size
            except OSError:
                pass
            if monotonic() - began > seconds:
                truncated = True
                break
    except Exception as exc:  # a walk that fails halfway keeps what it found, like an ingest does
        error = clean_text(_describe(exc))
    return RootScan(root, exists=True, readable=True, files=files, size=size, truncated=truncated, error=error)


def source_origin(cfg: Config, agent: str) -> str:
    """Where the log folders of ``agent`` come from: ``config`` (``sources.<agent>`` written in a file), the variable the agent
    uses to move its own data, or ``default``. The order is the one ``Config.sources`` follows."""
    table = cfg.data.get("sources")
    configured = table.get(agent) if isinstance(table, dict) else None
    if configured != DEFAULT_CONFIG["sources"].get(agent):
        return "config"
    variable = RELOCATION_VARIABLES.get(agent, "")
    return variable if variable and os.environ.get(variable, "").strip() else "default"


def cli_names(cfg: Config, agent: str) -> tuple[str, ...]:
    """The command names ``agentfare run`` knows ``agent`` by (``agents.<agent>.commands``), else the agent's own name."""
    raw = cfg.get(f"agents.{agent}.commands")
    names = tuple(name for name in raw if isinstance(name, str) and name.strip()) if isinstance(raw, list) else ()
    return names or (agent,)


def find_cli(names: tuple[str, ...]) -> str | None:
    """The first of ``names`` that is an executable on ``PATH``. Only looked up: the program is never started."""
    for name in names:
        if found := shutil.which(name):
            return found
    return None


def scan_agent(cfg: Config, agent: str, *, cap: int = SCAN_FILE_CAP, seconds: float = SCAN_SECONDS) -> AgentScan:
    names = cli_names(cfg, agent)
    return AgentScan(agent=agent, origin=source_origin(cfg, agent),
                     roots=tuple(scan_root(agent, root, cap=cap, seconds=seconds) for root in cfg.sources(agent)),
                     cli_names=names, cli_path=find_cli(names))


def scan_agents(cfg: Config, *, cap: int = SCAN_FILE_CAP, seconds: float = SCAN_SECONDS) -> list[AgentScan]:
    return [scan_agent(cfg, agent, cap=cap, seconds=seconds) for agent in AGENTS]
