"""Configuration: ``~/.agentfare/config.yaml`` merged over the built-in defaults.

``DEFAULT_CONFIG_YAML`` is the single source of truth: it is what
``agentfare config --init`` writes and what is parsed to obtain the defaults, so
the documented file and the real defaults cannot drift apart.

Environment:
  AGENTFARE_HOME  data directory (default ``~/.agentfare``)
  AGENTFARE_NOW   freeze "now" (ISO-8601) for deterministic demos and tests
  CLAUDE_CONFIG_DIR, CODEX_HOME, GEMINI_CLI_HOME  where the agents keep their logs, used for every agent
                  whose ``sources`` entry is left at its default
"""

from __future__ import annotations

import copy
import difflib
import io
import os
import re
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import yaml

from .safeio import MAX_CONFIG_BYTES, PRIVATE_FILE, make_private_dirs, read_text_limited, write_text_atomic
from .safeyaml import UniqueKeyLoader, YamlBombError, check_expansion, shown as _shown

if TYPE_CHECKING:
    from .pricing import PriceTable

DEFAULT_CONFIG_YAML = """\
# AgentFare configuration. Everything here is optional; delete a key to get its default.
# Relative paths are resolved against the directory that holds this file.

timezone: local          # "local", "UTC" or an IANA name such as "America/Toronto"
idle_gap_minutes: 30     # a pause longer than this is not counted as active time; changing it re-reads the logs automatically
database: ~/.agentfare/agentfare.db
pricing_file: null       # null: ~/.agentfare/pricing.yaml when present, else the built-in table

# Where each agent keeps its session logs. Every path is scanned recursively. An agent left at its default follows the
# variable that agent uses to relocate its own data: CLAUDE_CONFIG_DIR (<dir>/projects), CODEX_HOME (<dir>/sessions and
# <dir>/archived_sessions) and GEMINI_CLI_HOME (<dir>/.gemini/tmp). Set a path here to use that one instead.
sources:
  claude: [~/.claude/projects]
  codex: [~/.codex/sessions, ~/.codex/archived_sessions]  # the archive holds threads archived in the app
  gemini: [~/.gemini/tmp]

# --- The router (docs/ROUTING.md, "How the router decides") --------------------------
router:
  # `advise`: each past session gets a complexity score from these transparent signals.
  # A signal scores 0 below `medium`, 1 from `medium`, 2 from `heavy`. The defaults are the rounded 33rd and
  # 67th percentiles of real sessions with prompts (docs/ROUTING.md); tune them to your own history.
  signals:
    input_tokens:   {medium: 2000000, heavy: 25000000}  # all input incl. cache reads/writes
    tool_calls:     {medium: 25,      heavy: 180}
    files_touched:  {medium: 1,       heavy: 6}
    error_retries:  {medium: 2,       heavy: 8}
    active_minutes: {medium: 10,      heavy: 80}
  # Total score (0-10) -> tier: light <= light_max < medium <= medium_max < heavy
  tiers: {light_max: 2, medium_max: 6}
  # Model to suggest per tier. null keeps whatever model you actually used.
  recommend: {light: gpt-6.1-sol, medium: gpt-6.1-sol, heavy: null}
  # Only suggest a switch when it is at least this many times cheaper.
  min_saving_ratio: 1.2
  # `advise` lists a session on its own only when a person drove it (at least 1 prompt) and it cost at least
  # this many cents. The rest (sub-agent and auto-review threads, tiny sessions) still count in the totals but
  # are summed on one line; `advise --all` lists them.
  advise: {min_cost_cents: 10}

  # `run`: quick pre-flight heuristics on the prompt and the repo.
  run:
    # Score (0-9 with keyword_points 2) -> tier, like router.tiers but for this smaller scale: 0 is light, 1 and 2 are medium,
    # 3 and up heavy. The score adds prompt length (0-2), repo size (0-2) and keywords (heavy +, light -, medium +1), never
    # below 0. One reason is enough for medium; heavy takes a third point: two heavy keywords, a heavy keyword plus a medium
    # keyword, a prompt of 400+ characters or a repo of 200+ files, or a big prompt or repo plus one more reason.
    tiers: {light_max: 0, medium_max: 2}
    prompt_chars:  {medium: 400, heavy: 1500}
    repo_files:    {medium: 200, heavy: 1200}   # files outside .git, node_modules, .venv, build, ...
    # A keyword matches every word that starts with it, in any case and with or without accents: refactor finds refactoring and
    # refactored, format does not find information, migr finds migrar. A verb that changes its ending is listed as a stem:
    # migrat finds migrate, migrating and migration. Keywords that match the same word count once (migrat and migr in migrate).
    # English and Portuguese stems; add the words you write. A Portuguese stem is written without accents and long enough to
    # leave other words alone: seguranc finds segurança and concorrenc finds concorrência, where segur would find seguro too.
    heavy_keywords: [refactor, architect, migrat, redesign, rewrit, overhaul, security, concurrency,
                     refator, arquitet, migr, reescrev, reescrit, seguranc, concorrenc]
    light_keywords: [typo, renam, docstring, comment, format, lint, bump, readme, changelog,
                     renomear, renome, comentar, comentario, formatar, erro de digitacao]
    # Ordinary feature and bug work: the family adds 1, once, however many of its keywords match.
    medium_keywords: [implement, feature, debug, bug, fix, test, optimi, perform, investigat, design, integrat, endpoint, api,
                      schema, upgrade, funcionalidade, depur, teste, otimiz, desempenho, investig, projeto, integr, corrig]
    # The first distinct keyword of a heavy or light family that matches scores this (heavy: +, light: -), each further one 1
    # more, up to 2 more. 0 turns every keyword family off.
    keyword_points: 2
    # Claude Code takes short aliases (--model opus) that are not in the price table: the estimate prices an alias as the model
    # named here. Map them to what you actually run; Claude Code resolves the alias itself, so the command is not changed.
    model_aliases: {opus: claude-opus-5-5, sonnet: claude-sonnet-5-5, haiku: claude-haiku-4-5, fable: claude-fable-5-1}
    # Model to run per agent and tier. Must exist in the price table to get a cost estimate.
    models:
      claude: {light: claude-sonnet-5-5, medium: claude-sonnet-5-5, heavy: claude-opus-5-5}
      codex:  {light: gpt-6.1-sol,       medium: gpt-6.1-sol,       heavy: gpt-6-astra}
      gemini: {light: gemini-3.1-flash-lite, medium: gemini-3-flash-preview, heavy: gemini-3.1-pro-preview}
    # Fallback token budget per tier for the cost estimate until there are 5 past sessions of the same agent
    # and tier to take the median from. Midpoint of the Claude and Codex medians of sessions with prompts.
    default_tokens:
      light:  {input: 30000,  cache_read: 600000,    cache_write: 25000,   output: 7000}
      medium: {input: 135000, cache_read: 8800000,   cache_write: 130000,  output: 65000}
      heavy:  {input: 770000, cache_read: 118000000, cache_write: 1500000, output: 480000}

# How `agentfare run` hands the chosen model to each agent CLI. Add your own agent here: its commands are matched by
# basename (/usr/local/bin/claude is `claude`), and it also needs router.run.models.<agent> entries to be routed, or it
# runs unchanged with a warning.
agents:
  claude: {commands: [claude], model_flag: "--model"}
  codex:  {commands: [codex],  model_flag: "--model"}
  gemini: {commands: [gemini], model_flag: "--model"}
"""

DEFAULT_CONFIG: dict[str, Any] = yaml.safe_load(DEFAULT_CONFIG_YAML)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``override`` laid over ``base``. A key left empty (YAML null) over a default mapping keeps the mapping: that is
    what ``router:`` becomes when all its children are commented out. Over a scalar or a list, null still sets null."""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        elif v is None and isinstance(out.get(k), dict):
            continue
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_home() -> Path:
    return Path(os.environ.get("AGENTFARE_HOME") or "~/.agentfare").expanduser().absolute()


class ConfigError(ValueError):
    pass


# Sanity limits for what a config file may hold: a real value is a few characters, so anything longer is a mistake or abuse.
MAX_PATH_CHARS = 4096
MAX_TIMEZONE_CHARS = 100
MAX_NUMBER_CHARS = 64
MAX_IDLE_GAP_MINUTES = 1_000_000  # about two years: a gap no session has


# ---- unknown keys ------------------------------------------------------------------
# The allowed keys are whatever DEFAULT_CONFIG holds, so a new default is known without touching this list. These mappings
# are the exceptions, because the user chooses their keys: the agent names under these, each checked against the keys
# the default agents have, and the aliases, which are free-form all the way down.
_NAMED_CHILDREN = ("router.run.models", "agents")
_FREE_FORM = ("router.run.model_aliases",)


def _children_shape(children: dict[str, Any]) -> dict[str, Any]:
    """The keys every entry of a mapping of named entries may have: those the default entries have."""
    return {key: value for child in children.values() if isinstance(child, dict) for key, value in child.items()}


def _lookup(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def unknown_keys(user: dict[str, Any], schema: dict[str, Any] | None = None, path: str = "") -> list[str]:
    """One message per key in the user's config that nothing reads, with a did-you-mean when a known key is close."""
    schema = DEFAULT_CONFIG if schema is None else schema
    found: list[str] = []
    for key, value in user.items():
        dotted = f"{path}.{key}" if path else str(key)
        if str(key) not in schema and dotted not in _FREE_FORM:
            close = difflib.get_close_matches(str(key), [str(k) for k in schema], n=1)
            found.append(f"unknown key {dotted}" + (f" (did you mean {close[0]}?)" if close else ""))
        elif dotted in _FREE_FORM or not isinstance(value, dict) or not isinstance(schema.get(key), dict):
            continue
        elif dotted in _NAMED_CHILDREN:
            shape = _children_shape(schema[key])
            for name, entry in value.items():
                if isinstance(entry, dict):
                    found += unknown_keys(entry, shape, f"{dotted}.{name}")
        else:
            found += unknown_keys(value, schema[key], dotted)
    return found


def validate(cfg: "Config", table: "PriceTable") -> list[str]:
    """The models the person pointed the router at that the price table cannot price, one message per target.

    A target left at its default is not reported: a price table that lacks it is the table's gap, which ``advise`` and
    ``run`` already report where they use it, and a person who never wrote the key has nothing to fix in config.yaml."""
    targets: list[tuple[str, Any, str]] = []
    recommend = cfg.get("router.recommend")
    for tier, model in (recommend.items() if isinstance(recommend, dict) else ()):
        targets.append((f"router.recommend.{tier}", model, "advise cannot use it"))
    by_agent = cfg.get("router.run.models")
    for agent, tiers in (by_agent.items() if isinstance(by_agent, dict) else ()):
        for tier, model in (tiers.items() if isinstance(tiers, dict) else ()):
            targets.append((f"router.run.models.{agent}.{tier}", model, "run cannot estimate its cost"))
    return [
        f"{where}: {model} has no verified price, so {consequence}"
        for where, model, consequence in targets
        if isinstance(model, str) and model.strip() and model != _lookup(DEFAULT_CONFIG, where) and table.priced(model) is None
    ]


_MISSING: Any = object()  # a setting that is not written in the file


def _router_number(value: Any, where: str, *, whole: bool = False) -> Decimal:
    """A non-negative finite number of the router settings, exactly as written (the messages are the router's own)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ConfigError(f"{where} must be a number, got {value!r}") from exc
    if not number.is_finite() or number < 0:
        raise ConfigError(f"{where} must be a number of at least 0, got {value!r}")
    if whole and number != number.to_integral_value():
        raise ConfigError(f"{where} must be a whole number, got {value!r}")
    return number


def _check_router_numbers(cfg: "Config") -> None:
    """The plain numbers under ``router``: what ``advise`` and ``run`` would reject when they get to read them. A node of the
    wrong shape is left to them (and to their own message); a key nobody reads is ``unknown_keys``' business."""

    def written(dotted: str) -> Any:
        """The value written at ``dotted``, or ``_MISSING`` when a level above it is not a mapping or the key is absent."""
        node: Any = cfg.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return _MISSING
            node = node[part]
        return node

    if (ratio := written("router.min_saving_ratio")) is not _MISSING and _router_number(ratio, "router.min_saving_ratio") < 1:
        raise ConfigError(f"router.min_saving_ratio must be at least 1, got {ratio!r}")
    if (cents := written("router.advise.min_cost_cents")) is not _MISSING:
        _router_number(cents, "router.advise.min_cost_cents")
    for node in ("router.tiers", "router.run.tiers"):
        if isinstance(table := written(node), dict):
            cuts = [_router_number(table[key], f"{node}.{key}", whole=True) for key in ("light_max", "medium_max") if key in table]
            if len(cuts) == 2 and cuts[0] > cuts[1]:
                raise ConfigError(f"{node}: light_max ({cuts[0]}) must not be above medium_max ({cuts[1]})")
    if (points := written("router.run.keyword_points")) is not _MISSING:
        _router_number(points, "router.run.keyword_points", whole=True)
    for node in [f"router.signals.{name}" for name in DEFAULT_CONFIG["router"]["signals"]] + ["router.run.prompt_chars", "router.run.repo_files"]:
        if isinstance(table := written(node), dict):
            limits = {key: _router_number(table[key], f"{node}.{key}") for key in ("medium", "heavy") if key in table}
            if len(limits) == 2 and limits["medium"] > limits["heavy"]:
                raise ConfigError(f"{node}: medium ({table['medium']}) must not be above heavy ({table['heavy']})")


# Where each agent looks for its data when told to relocate it: the variable, and the log directories below its value.
# Claude Code and Codex read their variable as the data directory itself; Gemini CLI reads GEMINI_CLI_HOME as the home
# directory it then adds ``.gemini`` to.
_RELOCATION = {
    "claude": ("CLAUDE_CONFIG_DIR", ("projects",)),
    "codex": ("CODEX_HOME", ("sessions", "archived_sessions")),
    "gemini": ("GEMINI_CLI_HOME", (".gemini/tmp",)),
}


def _relocated(agent: str) -> list[Path] | None:
    variable, subdirs = _RELOCATION.get(agent, ("", ()))
    value = os.environ.get(variable, "").strip() if variable else ""
    if not value:
        return None
    root = Path(os.path.expanduser(value)).absolute()
    return [root / sub for sub in subdirs]


@dataclass
class Config:
    home: Path
    data: dict[str, Any]
    config_path: Path | None = None
    base_dir: Path = field(default_factory=Path.cwd)
    warnings: list[str] = field(default_factory=list)  # what is wrong with the file but not wrong enough to stop: unknown keys
    init_path: Path | None = None  # only for ``for_init``: the file ``config --init`` was told to write

    # ---- loading -----------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path | None = None, home: str | Path | None = None) -> "Config":
        # Absolute, so relative paths keep meaning the same thing if the working directory changes later.
        home_dir = Path(home).expanduser().absolute() if home else default_home()
        cfg_path = Path(path).expanduser().absolute() if path else home_dir / "config.yaml"
        user: dict[str, Any] = {}
        found: Path | None = None
        if cfg_path.exists():
            try:
                text = read_text_limited(cfg_path, MAX_CONFIG_BYTES)  # a regular file of at most 10 MB, never /dev/zero or a pipe
            except (OSError, UnicodeDecodeError) as exc:
                raise ConfigError(f"cannot read {cfg_path}: {exc}") from exc
            try:
                loaded = yaml.load(text, Loader=UniqueKeyLoader) or {}  # safe, and a key written twice is an error
            except Exception as exc:  # not only YAMLError: PyYAML lets ValueError, KeyError and more escape for values like 2026-02-30
                raise ConfigError(f"{cfg_path}: invalid YAML: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"{cfg_path}: top level must be a mapping")
            try:
                check_expansion(loaded)  # an alias bomb loads instantly and blows up at the first str() or copy of it
            except YamlBombError as exc:
                raise ConfigError(f"{cfg_path}: {exc}") from exc
            user, found = loaded, cfg_path
        elif path:
            raise ConfigError(f"config file not found: {cfg_path}")
        base = cfg_path.parent if found else home_dir
        return cls(home=home_dir, data=deep_merge(DEFAULT_CONFIG, user), config_path=found, base_dir=base, warnings=unknown_keys(user))

    # ---- access ------------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        return _lookup(self.data, dotted, default)

    def _path(self, value: str | os.PathLike[str], key: str = "path") -> Path:
        if not isinstance(value, (str, os.PathLike)):
            raise ConfigError(f"{key} must be a path (text), got {_shown(value)}")
        text = os.fspath(value)
        if len(text) > MAX_PATH_CHARS:
            raise ConfigError(f"{key} is longer than {MAX_PATH_CHARS} characters")
        p = Path(os.path.expanduser(text))
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def db_path(self) -> Path:
        raw = self.data.get("database")
        # The default database follows AGENTFARE_HOME / --home so demos never touch ~/.agentfare.
        if raw in (None, DEFAULT_CONFIG["database"]):
            return self.home / "agentfare.db"
        return self._path(raw, "database")

    @property
    def pricing_override_path(self) -> Path:
        """File that `prices --refresh` writes and that overrides the built-in table."""
        raw = self.data.get("pricing_file")
        return self._path(raw, "pricing_file") if raw else self.home / "pricing.yaml"

    def sources(self, agent: str) -> list[Path]:
        table = self.data.get("sources") or {}
        if not isinstance(table, dict):
            raise ConfigError("'sources' must map each agent to a list of log directories")
        if table.get(agent) == DEFAULT_CONFIG["sources"].get(agent) and (moved := _relocated(agent)):
            return moved  # left at the default: follow the agent's own relocation variable, as the database follows AGENTFARE_HOME
        raw = table.get(agent) or []
        if isinstance(raw, (str, os.PathLike)):
            raw = [raw]
        if not isinstance(raw, list) or not all(isinstance(p, (str, os.PathLike)) for p in raw):
            raise ConfigError(f"sources.{agent} must be a list of directories")
        return [self._path(p, f"sources.{agent}") for p in raw]

    @property
    def idle_gap_seconds(self) -> int:
        raw = self.data.get("idle_gap_minutes")
        if raw is None:
            raw = DEFAULT_CONFIG["idle_gap_minutes"]
        # A plain number (or the text of one), checked before anything converts it: str() of a list built by aliases can be huge.
        if isinstance(raw, bool) or not isinstance(raw, (int, float, Decimal, str)) or (isinstance(raw, str) and len(raw) > MAX_NUMBER_CHARS):
            raise ConfigError(f"idle_gap_minutes must be a number of minutes, got {_shown(raw)}")
        try:
            minutes = Decimal(raw) if isinstance(raw, int) and raw.bit_length() > 64 else Decimal(str(raw))
            if not minutes.is_finite():
                raise ValueError(raw)
        except (ArithmeticError, ValueError) as exc:  # decimal.InvalidOperation and decimal.Overflow are ArithmeticErrors
            raise ConfigError(f"idle_gap_minutes must be a number of minutes, got {_shown(raw)}") from exc
        if minutes < 0:
            raise ConfigError(f"idle_gap_minutes must not be negative, got {_shown(raw)}")
        if minutes > MAX_IDLE_GAP_MINUTES:  # before the multiplication and int(): 1e5000000 would be a five-million-digit integer
            raise ConfigError(f"idle_gap_minutes is absurdly large (more than {MAX_IDLE_GAP_MINUTES:,} minutes), got {_shown(raw)}")
        return int(minutes * 60)  # Decimal: 1.15 minutes is exactly 69 seconds

    @property
    def tz(self) -> tzinfo:
        name = self.data.get("timezone") or "local"
        if not isinstance(name, str) or len(name) > MAX_TIMEZONE_CHARS:
            raise ConfigError(f"timezone must be \"local\", \"UTC\" or an IANA name, got {_shown(name)}")
        return resolve_tz(name)

    def check(self) -> None:
        """Look at every setting that a command would otherwise meet only when it gets to use it, so that all of them fail
        in the same clean way (``error: ...``, exit code 2) whether or not the logs are scanned first: the idle gap, the
        time zone, the paths, the log directories and the plain numbers of the router."""
        self.idle_gap_seconds
        self.tz
        self.db_path
        self.pricing_override_path
        for agent in _RELOCATION:
            self.sources(agent)
        _check_router_numbers(self)

    def local_tz_warning(self) -> str | None:
        """What to say when ``timezone: local`` could not be read as a named zone and is today's UTC offset instead."""
        name = self.data.get("timezone") or "local"
        if not isinstance(name, str) or name.lower() != "local":
            return None
        _, unread = _resolve_system_tz(os.environ.get("TZ", ""), _LOCALTIME)
        if not unread:
            return None
        return (f"timezone \"local\" could not be resolved to a named zone with DST rules ({unread}), so today's UTC offset is used "
                "all year and a day near a daylight-saving change can land on the wrong date; set timezone to an IANA name such as America/Toronto")

    @classmethod
    def for_init(cls, path: str | Path | None = None, home: str | Path | None = None) -> "Config":
        """A ``Config`` of the defaults that never reads a config file: what ``config --init`` needs to know where to write,
        so that a file which is broken (invalid YAML, not a mapping) can still be replaced with ``--force``.

        ``path`` is the file the command line named (``--config``), or None; ``default_init_target`` says where to write."""
        home_dir = Path(home).expanduser().absolute() if home else default_home()
        cfg = cls(home=home_dir, data=copy.deepcopy(DEFAULT_CONFIG), base_dir=home_dir)
        cfg.init_path = Path(path).expanduser().absolute() if path else None
        return cfg

    @property
    def default_init_target(self) -> Path:
        """Where ``config --init`` writes: the file named with ``--config`` (for a config made by ``for_init``), else
        ``<home>/config.yaml``."""
        return self.init_path or self.home / "config.yaml"

    def write_default(self, path: Path | None = None, overwrite: bool = False) -> Path:
        """Write the commented default file (mode 0600, in a directory made 0700 when it is new) to ``path``, else to
        ``<home>/config.yaml``. An existing file is left alone unless ``overwrite``, and is then never read, so a broken one can go."""
        target = path or (self.home / "config.yaml")
        if os.path.lexists(target) and not overwrite:
            raise ConfigError(f"{target} already exists (use --force to overwrite)")
        try:
            make_private_dirs(target.parent)
            if os.path.lexists(target):  # replaced through a new file, so a link is updated where it points and a mode is kept
                write_text_atomic(target.resolve(), DEFAULT_CONFIG_YAML)
            else:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, PRIVATE_FILE)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(DEFAULT_CONFIG_YAML)
        except OSError as exc:
            raise ConfigError(f"cannot write {target}: {exc.strerror or exc}") from exc
        return target


# ---- time helpers ------------------------------------------------------------------
_LOCALTIME = Path("/etc/localtime")


def _system_tz() -> tzinfo:
    return _load_system_tz(os.environ.get("TZ", ""), _LOCALTIME)


def _load_system_tz(tz_env: str, localtime: Path) -> tzinfo:
    """The machine's own zone *with its DST rules* (see ``_resolve_system_tz``)."""
    return _resolve_system_tz(tz_env, localtime)[0]


def _posix_zone(rule: str) -> ZoneInfo | None:
    """A POSIX ``TZ`` rule string such as ``EST5EDT,M3.2.0,M11.1.0`` as a zone with those DST rules, or None when it is not one.

    ``zoneinfo`` has a parser for exactly this text, the footer of a TZif file, but no way to call it on a bare string: the
    rule is wrapped in a TZif file that has one fixed type and no transitions, so the footer alone decides every instant."""
    if not rule or len(rule) > MAX_TIMEZONE_CHARS or not rule.isascii() or any(c in rule for c in "\0\n"):
        return None
    block = b"TZif2" + bytes(15) + struct.pack(">6l", 0, 0, 0, 0, 1, 1) + struct.pack(">lbb", 0, 0, 0) + b"\0"
    try:
        return ZoneInfo.from_file(io.BytesIO(block + block + b"\n" + rule.encode("ascii") + b"\n"))
    except (ValueError, OSError, KeyError):
        return None


@lru_cache(maxsize=8)  # commands read ``cfg.tz`` in loops; parsing the zone file every time would be wasteful
def _resolve_system_tz(tz_env: str, localtime: Path) -> tuple[tzinfo, str]:
    """(the machine's zone with its DST rules, what could not be read).

    The second item is empty when the zone was read, or when nothing names one (no ``TZ`` and no system zone file: a bare
    container, which is UTC). Otherwise the zone is today's UTC offset as a fixed zone, which would put sessions from the
    other half of the year on the wrong local day (``datetime.astimezone()`` reports nothing more), and the item says what
    could not be read: ``TZ='...'`` or the path of the zone file.
    """
    name = tz_env.lstrip(":")
    try:
        if name and not name.startswith("/"):
            try:
                return ZoneInfo(name), ""
            except (OSError, ValueError, KeyError):  # not a name in the tz database: maybe a POSIX rule with DST dates
                if (rule := _posix_zone(name)) is not None:
                    return rule, ""
                raise
        # An absolute TZ (``TZ=:/etc/localtime`` is common in images and login environments) names a zone file
        with (Path(name) if name else localtime).open("rb") as fh:
            return ZoneInfo.from_file(fh), ""
    except (OSError, ValueError, KeyError):  # an unreadable POSIX-style TZ string, no zone file, broken zone file
        unread = f"TZ={tz_env!r}" if tz_env.strip() else str(localtime) if localtime.exists() else ""
        try:
            fallback = datetime.now().astimezone().tzinfo or timezone.utc
        except (ValueError, OverflowError, OSError):  # a TZ the C library cannot parse can yield an offset datetime refuses (macOS: -30 hours)
            fallback = timezone.utc
        return fallback, unread


def resolve_tz(name: str) -> tzinfo:
    if name.lower() == "utc":
        return timezone.utc
    if name.lower() == "local":
        return _system_tz()
    try:
        return ZoneInfo(name)
    except Exception as exc:  # KeyError, ValueError or OSError depending on the platform and the name
        raise ConfigError(f"unknown timezone {name!r}") from exc


def parse_iso(ts: str) -> datetime:
    """Parse the ISO-8601 forms found in agent logs (trailing Z, offsets, fractional seconds) to aware UTC."""
    s = ts.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc)
    except OverflowError as exc:  # year 1 or 9999 with an offset: the UTC instant falls outside the datetime range
        raise ValueError(f"timestamp out of range: {ts!r}") from exc


def fmt_iso(dt: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ``; a naive datetime is read as UTC, like ``parse_iso`` does (never as machine-local time)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # isoformat, not strftime: ``%Y`` is not zero-padded for years below 1000 on every platform
    return dt.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds") + "Z"


def now_utc() -> datetime:
    frozen = os.environ.get("AGENTFARE_NOW")
    if not frozen:
        return datetime.now(timezone.utc)
    try:
        return parse_iso(frozen)
    except ValueError as exc:
        raise ConfigError(f"AGENTFARE_NOW must be an ISO-8601 timestamp, got {frozen!r}") from exc


def _local(ts: str, tz: tzinfo) -> datetime:
    """``ts`` on ``tz``'s calendar. An instant within a day of year 1 or 9999 cannot be shifted and stays on UTC's."""
    utc = parse_iso(ts)
    try:
        return utc.astimezone(tz)
    except OverflowError:
        return utc


def day_of(ts: str, tz: tzinfo) -> str:
    return _local(ts, tz).date().isoformat()


def iso_week_of(ts: str, tz: tzinfo) -> str:
    y, w, _ = _local(ts, tz).isocalendar()
    return f"{y:04d}-W{w:02d}"


_WEEK_LABEL = re.compile(r"(\d{4})-W(\d{2})")


def week_bounds(week: str | None, tz: tzinfo, now: datetime | None = None) -> tuple[str, str]:
    """(first_day, last_day) 'YYYY-MM-DD' of an ISO week label 'YYYY-Www'; None = the week containing ``now``."""
    if week:
        match = _WEEK_LABEL.fullmatch(week.strip().upper())
        if not match:
            raise ConfigError(f"invalid ISO week {week!r}: expected YYYY-Www, for example 2026-W39")
        try:
            monday = datetime.fromisocalendar(int(match[1]), int(match[2]), 1)
        except ValueError as exc:
            raise ConfigError(f"invalid ISO week {week!r}: {exc}") from exc
    else:
        local = (now or now_utc()).astimezone(tz)
        monday = (local - timedelta(days=local.isoweekday() - 1)).replace(tzinfo=None)
    try:
        sunday = monday + timedelta(days=6)
    except OverflowError as exc:  # the last ISO week of year 9999 ends in year 10000
        raise ConfigError(f"ISO week starting {monday.date().isoformat()} ends after the last supported date") from exc
    return monday.date().isoformat(), sunday.date().isoformat()
