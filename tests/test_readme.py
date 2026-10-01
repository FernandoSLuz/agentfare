"""Drift guard for the public documentation: README.md, CONTRIBUTING.md, the community files and docs/.

Everything the README shows that can be computed is computed here, so a change to the output, the price table or the
config defaults fails a test until the README follows:

* Output blocks. A fenced block that has ``<!-- run: agentfare summary -->`` on the line above it holds the exact
  standard output of that command on the synthetic fixtures (tests/fixtures), with a fresh data directory, a frozen
  clock, ``COLUMNS=100`` and no colour. The test runs the command and compares line by line (the spaces at the end of a
  line and the final newline aside). Progress notes go to stderr and are not part of the block.
* Generated tables. Between ``<!-- generated: NAME -->`` and ``<!-- /generated -->`` the README holds a table that
  this file builds from ``pricing.yaml`` (prices), ``DEFAULT_CONFIG`` (signals, tiers, run rules, config keys) and a run
  of ``advise --json`` on the fixtures (the worked example).
* Figures in the prose. The worked example and the sentences that repeat its numbers are rebuilt from the parsed fixtures,
  the price table and ``advise --json``, so one edited occurrence fails.
* Commands. Every ``agentfare ...`` line in a ``sh`` block must be a real command with real flags, and the README must
  show every command the app registers.
* Links. Relative links in README.md, README.pt-BR.md, CONTRIBUTING.md, the community files and docs/*.md must point at files (and
  headings) that exist. The Portuguese README has its own structural test, tests/test_readme_pt.py.
* The demo script runs (also without `agentfare` on PATH), is deterministic, and prints each command before its output.
* No line of a document ends in spaces.

To bring the README back in line after a deliberate change:

    python tests/test_readme.py --write

which rewrites every generated table and every output block from the real thing, without the padding Rich puts at the
end of a line, so that the README stays clean for ``git diff --check``.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

import pytest
import typer.main
import yaml
from typer.testing import CliRunner

from agentfare import __version__
from agentfare.cli import app
from agentfare.config import DEFAULT_CONFIG
from agentfare.cost import usage_cost_ucents
from agentfare.db import Store
from agentfare.models import SessionRecord, Usage
from agentfare.money import format_usd_compact
from agentfare.parsers import get_parser
from agentfare.pricing import PRICE_FIELDS, ModelPrice, builtin_path, load_pricing
from agentfare.router import EXTRA_KEYWORDS, MEDIUM_POINTS

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
README_PT = ROOT / "README.pt-BR.md"
COMMUNITY_DOCS = [ROOT / "CHANGELOG.md", ROOT / "SECURITY.md", ROOT / "CODE_OF_CONDUCT.md"]
FIXTURES = ROOT / "tests" / "fixtures"
DEMO_SCRIPT = ROOT / "scripts" / "demo.sh"
DEMO_TAPE = ROOT / "demo.tape"
COMMAND_DOCS = [
    README, README_PT, ROOT / "CONTRIBUTING.md", *COMMUNITY_DOCS, ROOT / "docs" / "DEMO.md", ROOT / "docs" / "LOG-FORMATS.md", ROOT / "docs" / "ARCHITECTURE.md",
]  # fmt: skip
LINKED_DOCS = [README, README_PT, ROOT / "CONTRIBUTING.md", *COMMUNITY_DOCS, *sorted((ROOT / "docs").glob("*.md"))]
NOW = "2026-09-30T12:00:00Z"
COLUMNS = "100"


# ---- running the real thing -------------------------------------------------------------------------------------
def pinned_env(home: Path) -> dict[str, str]:
    """The environment every documented run uses: nothing of the developer's own leaks in."""
    env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "SYSTEMROOT") if key in os.environ}
    env.update(
        HOME=str(home), AGENTFARE_HOME=str(home / "agentfare"), AGENTFARE_NOW=NOW, AGENTFARE_CONFIG=str(FIXTURES / "config.yaml"),
        COLUMNS=COLUMNS, NO_COLOR="1", TERM="dumb", PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
    )  # fmt: skip
    return env


def agentfare(args: list[str], *, ingested: bool = False) -> subprocess.CompletedProcess[str]:
    """``agentfare ARGS`` on the fixtures in a throwaway data directory, from the small directory the run samples assume.

    ``run`` never ingests (it only reads a database that exists), so a sample of it is what you see after using AgentFare on your
    logs: with ``ingested`` the fixtures are read into the data directory first, as ``scripts/demo.sh`` does with its first step."""
    with tempfile.TemporaryDirectory() as tmp:
        env = pinned_env(Path(tmp))
        if ingested:
            first = subprocess.run([sys.executable, "-m", "agentfare", "ingest"], cwd=FIXTURES, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
            assert first.returncode == 0, first.stderr
        return subprocess.run(
            [sys.executable, "-m", "agentfare", *args], cwd=FIXTURES, env=env, capture_output=True,
            text=True, encoding="utf-8", timeout=120,
        )  # fmt: skip


def squash(text: str) -> str:
    """``text`` without the spaces at the end of each line. Rich pads its table cells, and an editor, a hook or
    ``git diff --check`` would strip them from the README, so they never count."""
    return "\n".join(line.rstrip() for line in text.split("\n"))


def output_of(command: str) -> str:
    """Standard output of a README marker command (``agentfare summary``), without its final newline or the padding of its lines."""
    argv = shlex.split(command)
    assert argv[0] == "agentfare", f"a run marker must start with agentfare: {command!r}"
    done = agentfare(argv[1:], ingested=argv[1] == "run")
    assert done.returncode == 0, f"`{command}` exited {done.returncode}:\n{done.stderr}"
    return squash(done.stdout[:-1] if done.stdout.endswith("\n") else done.stdout)


# ---- generated tables ---------------------------------------------------------------------------------------------
def table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def price_text(value: Decimal) -> str:
    """``10.00``, ``0.125``: at least two decimals, no trailing zeros beyond that (as ``agentfare prices`` prints them)."""
    whole, _, frac = format(value, "f").partition(".")
    return f"{whole}.{frac.rstrip('0').ljust(2, '0')}"


def gen_prices() -> str:
    prices = load_pricing(path=builtin_path())
    rows = []
    for p in sorted(prices.models.values(), key=lambda m: (m.provider, m.model)):
        cells = []
        for field in PRICE_FIELDS:
            value = getattr(p, field)
            cells.append("unverified" if field in p.unverified_fields else "-" if value is None else price_text(value))
        rows.append([f"`{p.model}`", *cells, f"<{p.source}>", p.checked])
    headers = ["Model", "Input", "Cached input", "Output", "Cache write (5 min)", "Cache write (1 h)", "Source", "Checked"]
    return table(headers, rows)


SIGNAL_TEXT = {
    "input_tokens": "Every input token of the session, all models added up: uncached, cache reads and cache writes. Output tokens are left out.",
    "tool_calls": "Tool invocations: reading, searching, editing and running commands.",
    "files_touched": "Distinct files written or edited. A count, never names.",
    "error_retries": "Tool calls that came back as an error.",
    "active_minutes": "Active seconds divided by 60. Pauses longer than `idle_gap_minutes` do not count.",
}  # fmt: skip


def number(value: Any) -> str:
    return f"{value:,}"


def span(first: int, last: int) -> str:
    return str(first) if first == last else f"{first} to {last}"


def gen_signals() -> str:
    signals = DEFAULT_CONFIG["router"]["signals"]
    rows = [
        [f"`router.signals.{name}`", SIGNAL_TEXT[name], number(limits["medium"]), number(limits["heavy"])]
        for name, limits in signals.items()
    ]  # fmt: skip
    return table(["Signal", "What it counts", "1 point from", "2 points from"], rows)


def gen_tiers() -> str:
    cuts = DEFAULT_CONFIG["router"]["tiers"]
    top = 2 * len(DEFAULT_CONFIG["router"]["signals"])
    light, medium = cuts["light_max"], cuts["medium_max"]
    rows = [[span(0, light), "light"], [span(light + 1, medium), "medium"], [span(medium + 1, top), "heavy"]]
    return table(["Score", "Tier"], rows) + f"\n\nThe cuts are `router.tiers`: `light_max: {light}` and `medium_max: {medium}`."


def gen_worked() -> str:
    done = agentfare(["advise", "--all", "--json"])
    assert done.returncode == 0, done.stderr
    session = next(s for s in json.loads(done.stdout)["sessions"] if s["agent"] == "codex")
    rows = []
    for name, limits in DEFAULT_CONFIG["router"]["signals"].items():
        value = session["signals"][name]
        shown = f"{value:.1f}" if isinstance(value, float) else number(value)
        rows.append([f"`{name}`", shown, number(limits["medium"]), number(limits["heavy"]), str(session["points"][name])])
    rows.append(["Score", "", "", "", f"{session['score']} of 10 ({session['tier']})"])
    return table(["Signal", "Value", "medium", "heavy", "Points"], rows)


def gen_run_rules() -> str:
    run = DEFAULT_CONFIG["router"]["run"]
    chars, files, points = run["prompt_chars"], run["repo_files"], run["keyword_points"]
    step = f"+{points} for the first distinct keyword, +1 for each further one, at most {EXTRA_KEYWORDS} more"
    rows = [
        ["Prompt length in characters", "+1 from `medium`, +2 from `heavy`", f"{number(chars['medium'])} / {number(chars['heavy'])}", "`router.run.prompt_chars`"],
        ["Files in the repository", "+1 from `medium`, +2 from `heavy`", f"{number(files['medium'])} / {number(files['heavy'])}", "`router.run.repo_files`"],
        ["Heavy keywords", step, ", ".join(run["heavy_keywords"]), "`router.run.heavy_keywords`"],
        ["Light keywords", "The same, subtracted", ", ".join(run["light_keywords"]), "`router.run.light_keywords`"],
        ["Medium keywords", f"+{MEDIUM_POINTS} once, however many of them match", ", ".join(run["medium_keywords"]), "`router.run.medium_keywords`"],
    ]  # fmt: skip
    cuts = run["tiers"]
    light, medium = cuts["light_max"], cuts["medium_max"]
    tiers = table(["Score", "Tier"], [[span(0, light), "light"], [span(light + 1, medium), "medium"], [f"{medium + 1} and up", "heavy"]])
    note = f"The cuts are `router.run.tiers`: `light_max: {light}` and `medium_max: {medium}`."
    return table(["Rule", "Points", "Default", "Config key"], rows) + "\n\n" + tiers + "\n\n" + note


CONFIG_TEXT = {
    "timezone": "Time zone that decides which day and ISO week a session belongs to: `local`, `UTC` or an IANA name such as `America/Toronto`.",
    "idle_gap_minutes": "A pause longer than this does not count as active session time. Changing it re-reads the logs automatically.",
    "database": "The SQLite file. The default follows `AGENTFARE_HOME` and `--home`.",
    "pricing_file": "Price table override. `null` means `~/.agentfare/pricing.yaml` when it exists, otherwise the built-in table. A path that does not exist is an error.",
    "sources": "Directories scanned recursively for each agent's session logs. The second Codex directory holds threads archived in the app. An agent left at its default follows `CLAUDE_CONFIG_DIR`, `CODEX_HOME` and `GEMINI_CLI_HOME`.",
    "router.signals.input_tokens": "Points for the total input tokens of a session (see the signals table above).",
    "router.signals.tool_calls": "Points for the number of tool calls.",
    "router.signals.files_touched": "Points for the number of distinct files written or edited.",
    "router.signals.error_retries": "Points for the number of tool calls that returned an error.",
    "router.signals.active_minutes": "Points for the active minutes of a session.",
    "router.tiers": "Total score (0 to 10) to tier: `light` up to `light_max`, `medium` up to `medium_max`, `heavy` above.",
    "router.recommend": "Model `advise` compares each tier against. `null` keeps the model you used.",
    "router.min_saving_ratio": "A switch is advised only when it is at least this many times cheaper. At least 1.",
    "router.advise.min_cost_cents": "A session with prompts gets its own line in `advise` only when it cost at least this many cents. The rest are summed on one line each.",
    "router.run.tiers": "Score of a `run` pick (0 to 9 with the defaults) to tier, like `router.tiers`.",
    "router.run.prompt_chars": "Prompt length that adds 1 point (`medium`) and 2 points (`heavy`).",
    "router.run.repo_files": "File count of the repository that adds 1 point (`medium`) and 2 points (`heavy`).",
    "router.run.heavy_keywords": "Word stems that add points. A stem matches every word that starts with it.",
    "router.run.light_keywords": "Word stems that subtract points, matched the same way.",
    "router.run.medium_keywords": "Word stems of ordinary feature and bug work: they add 1 point, once, however many of them match.",
    "router.run.keyword_points": "Points for the first matching keyword of a heavy or light family. `0` turns the keyword families off.",
    "router.run.model_aliases": "Short model names an agent CLI accepts (`--model opus`) that are not in the price table: the estimate prices an alias as the model named here. Your entries are merged over these.",
    "router.run.models.claude": "Model `run` picks for Claude Code, per tier.",
    "router.run.models.codex": "Model `run` picks for Codex CLI, per tier.",
    "router.run.models.gemini": "Model `run` picks for Gemini CLI, per tier.",
    "router.run.default_tokens.light": "Token budget behind the cost estimate of a light task, until there are 5 past sessions of the same agent and tier.",
    "router.run.default_tokens.medium": "The same for a medium task.",
    "router.run.default_tokens.heavy": "The same for a heavy task.",
    "agents.claude": "Commands that count as Claude Code (matched by basename) and the flag that takes a model.",
    "agents.codex": "Commands that count as Codex CLI, and its model flag.",
    "agents.gemini": "Commands that count as Gemini CLI, and its model flag.",
}  # fmt: skip


def config_leaves(node: Any, path: tuple[str, ...] = ()) -> Iterator[tuple[str, Any]]:
    """The keys the README documents: a mapping of mappings is walked, a mapping of plain values is one key."""
    if isinstance(node, dict) and (len(node) == 1 or any(isinstance(v, dict) for v in node.values())):
        for key, value in node.items():
            yield from config_leaves(value, (*path, key))
    else:
        yield ".".join(path), node


def flow(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return yaml.safe_dump(value, default_flow_style=True, sort_keys=False, width=10**6).strip()
    return str(value)


def gen_config() -> str:
    leaves = dict(config_leaves(DEFAULT_CONFIG))
    missing = sorted(set(leaves) - set(CONFIG_TEXT))
    stale = sorted(set(CONFIG_TEXT) - set(leaves))
    assert not missing, f"describe these config keys in CONFIG_TEXT (tests/test_readme.py): {missing}"
    assert not stale, f"CONFIG_TEXT describes keys DEFAULT_CONFIG no longer has: {stale}"
    rows = [[f"`{key}`", f"`{flow(value)}`", CONFIG_TEXT[key]] for key, value in leaves.items()]
    return table(["Key", "Default", "Meaning"], rows)


GENERATORS: dict[str, Callable[[], str]] = {
    "prices": gen_prices, "signals": gen_signals, "tiers": gen_tiers, "worked": gen_worked, "run-rules": gen_run_rules, "config": gen_config,
}  # fmt: skip


# ---- reading and rewriting the README -----------------------------------------------------------------------------
FENCE = re.compile(r"^```([\w-]*)[ \t]*\n(.*?)^```[ \t]*$", re.MULTILINE | re.DOTALL)
RUNNABLE = re.compile(r"<!-- run: (?P<cmd>[^\n]*?) -->\n```[ \t]*\n(?P<out>.*?)\n```[ \t]*$", re.MULTILINE | re.DOTALL)
GENERATED = re.compile(r"(?P<open><!-- generated: (?P<name>[a-z-]+) -->\n)(?P<body>.*?)(?P<close><!-- /generated -->)", re.DOTALL)


def block(text: str) -> str:
    """A generated block as it sits between its markers: blank lines around it keep the table a table."""
    return f"\n{text}\n\n"


def synced(text: str) -> str:
    """``text`` with every generated block and every output block rebuilt from the real thing."""
    text = GENERATED.sub(lambda m: m["open"] + block(GENERATORS[m["name"]]()) + m["close"], text)
    return RUNNABLE.sub(lambda m: f"<!-- run: {m['cmd']} -->\n```\n{output_of(m['cmd'])}\n```", text)


def readme_text() -> str:
    return README.read_text(encoding="utf-8")


def without_code(text: str) -> str:
    """Prose only: fenced blocks and inline code taken out."""
    text = FENCE.sub("", text)
    return re.sub(r"`[^`\n]*`", "", text)


RUN_BLOCKS = [(m["cmd"], m["out"]) for m in RUNNABLE.finditer(readme_text())]


# ---- (a) output blocks --------------------------------------------------------------------------------------------
def test_the_readme_has_output_blocks_for_the_main_commands() -> None:
    commands = [command for command, _ in RUN_BLOCKS]
    for wanted in ("agentfare summary", "agentfare advise", "agentfare sessions", "agentfare models"):
        assert wanted in commands, f"the README should show the output of `{wanted}`"
    assert any(c.startswith("agentfare run --dry-run") for c in commands), "the README should show a `run --dry-run` decision"


@pytest.mark.parametrize(("command", "expected"), RUN_BLOCKS, ids=[command for command, _ in RUN_BLOCKS])
def test_an_output_block_is_what_the_command_prints(command: str, expected: str) -> None:
    assert output_of(command) == squash(expected), "README output block is out of date: run `python tests/test_readme.py --write`"


# ---- (b) every documented command parses --------------------------------------------------------------------------
GROUP: Any = typer.main.get_command(app)
PUNCTUATION = set("();<>|&")


def shell_blocks(path: Path) -> list[tuple[int, str]]:
    """(line number, text) of every ``sh`` block of a markdown file."""
    text = path.read_text(encoding="utf-8")
    return [
        (text.count("\n", 0, m.start(2)) + 1, m.group(2))
        for m in FENCE.finditer(text)
        if m.group(1) in ("sh", "bash", "shell", "console")
    ]  # fmt: skip


def invocations(path: Path) -> list[tuple[str, list[str]]]:
    """(label, arguments after ``agentfare``) for each command a ``sh`` block starts with ``agentfare``."""
    found = []
    for line_no, body in shell_blocks(path):
        for offset, raw in enumerate(re.sub(r"\\\n[ \t]*", " ", body).splitlines()):
            if not raw.strip() or raw.lstrip().startswith("#"):
                continue
            segment: list[str] = []
            segments: list[list[str]] = []
            for token in shlex.shlex(raw, posix=True, punctuation_chars=True):
                if set(token) <= PUNCTUATION:
                    segments.append(segment)
                    segment = []
                else:
                    segment.append(token)
            segments.append(segment)
            for words in segments:
                while words and re.match(r"[A-Za-z_]\w*=", words[0]):
                    words = words[1:]  # VAR=value agentfare ...
                if words and words[0] == "agentfare":
                    found.append((f"{path.name}:{line_no + offset}", words[1:]))
    return found


def options_of(command: Any) -> dict[str, Any]:
    """Option spelling (``--json``, ``-n``) to its parameter. Typer 0.2x ships its own copy of click, so nothing here imports click."""
    return {name: p for p in command.params if p.param_type_name == "option" for name in (*p.opts, *p.secondary_opts)}


def takes_value(option: Any) -> bool:
    return option is not None and not option.is_flag and option.nargs == 1


def check_invocation(args: list[str]) -> tuple[list[str], list[tuple[str | None, str]]]:
    """(problems, [(command, flag)] used) for the arguments of one ``agentfare`` line."""
    problems: list[str] = []
    used: list[tuple[str | None, str]] = []
    group_options = options_of(GROUP)
    i = 0
    while i < len(args) and args[i].startswith("-"):
        name, has_value, _ = args[i].partition("=")
        used.append((None, name))
        if name != "--help" and name not in group_options:
            problems.append(f"unknown global option {name}")
        elif takes_value(group_options.get(name)) and not has_value:
            i += 1
        i += 1
    if i >= len(args):
        return problems, used
    command_name = args[i]
    command = GROUP.commands.get(command_name)
    if command is None:
        return [*problems, f"unknown command {command_name!r}"], used
    i += 1
    options = options_of(command)
    takes_arguments = any(p.param_type_name == "argument" for p in command.params)
    while i < len(args):
        token = args[i]
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            name, has_value, _ = token.partition("=")
            used.append((command_name, name))
            if name != "--help" and name not in options:
                problems.append(f"`{command_name}` has no option {name}")
            elif takes_value(options.get(name)) and not has_value:
                i += 1
        elif takes_arguments:
            break  # `run`: the agent's own command line starts here
        else:
            problems.append(f"`{command_name}` takes no argument {token!r}")
        i += 1
    return problems, used


def all_invocations() -> list[tuple[str, list[str]]]:
    return [item for path in COMMAND_DOCS if path.exists() for item in invocations(path)]


@functools.cache
def help_text(command: str | None) -> str:
    result = CliRunner().invoke(app, [command, "--help"] if command else ["--help"], env={"COLUMNS": "200", "NO_COLOR": "1"})
    assert result.exit_code == 0, result.output
    return result.output


INVOCATIONS = all_invocations()


def test_the_docs_show_enough_commands_to_check() -> None:
    assert len(INVOCATIONS) >= 15


@pytest.mark.parametrize(("label", "args"), INVOCATIONS, ids=[f"{label} {' '.join(args)[:50]}" for label, args in INVOCATIONS])
def test_a_documented_command_parses(label: str, args: list[str]) -> None:
    problems, used = check_invocation(args)
    assert not problems, f"{label}: `agentfare {' '.join(args)}`: {'; '.join(problems)}"
    for command, flag in used:  # and --help of that command lists each flag the docs use
        assert flag in help_text(command), f"{label}: `agentfare {command or ''} --help` does not list {flag}"


def test_the_readme_shows_every_command() -> None:
    shown = {args[0] for label, args in all_invocations() if label.startswith("README") and args and not args[0].startswith("-")}
    assert set(GROUP.commands) <= shown, f"README code blocks never use: {sorted(set(GROUP.commands) - shown)}"


# ---- (c) generated tables -----------------------------------------------------------------------------------------
def generated_blocks() -> dict[str, str]:
    return {m["name"]: m["body"] for m in GENERATED.finditer(readme_text())}


def test_the_readme_has_every_generated_block() -> None:
    assert set(generated_blocks()) == set(GENERATORS)


@pytest.mark.parametrize("name", sorted(GENERATORS))
def test_a_generated_block_is_in_sync(name: str) -> None:
    found = generated_blocks().get(name)
    assert found is not None, f"the README has no <!-- generated: {name} --> block"
    assert found == block(GENERATORS[name]()), f"the {name} table is out of date: run `python tests/test_readme.py --write`"


def test_every_config_key_is_described_and_none_is_stale() -> None:
    gen_config()  # asserts both directions


def test_the_price_table_lists_every_model_of_pricing_yaml() -> None:
    prices = load_pricing(path=builtin_path())
    body = generated_blocks()["prices"]
    assert sorted(re.findall(r"^\| `([^`]+)`", body, re.MULTILINE)) == sorted(prices.models)


def pricing_prose() -> str:
    """The text of the "Pricing sources" section without its generated table: where the README explains the prices. The
    section runs to the next heading of its own level."""
    text = readme_text()
    start = text.index("\n## Pricing sources\n")
    end = text.index("\n## ", start + 1)
    return GENERATED.sub("", text[start:end])


def test_the_readme_explains_the_unverified_models_and_the_dated_prices() -> None:
    """The prose of the pricing section (not the table, and not a mention elsewhere) names every model without a price and
    every date that a price note carries."""
    prices = load_pricing(path=builtin_path())
    prose = pricing_prose()
    for model, price in prices.models.items():
        if not price.priced:
            assert f"`{model}`" in prose, f"the README should say why {model} is unverified"
    dates = {d for price in prices.models.values() for d in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", price.note)}
    assert dates, "pricing.yaml no longer has a dated note: drop this check together with the README paragraph"
    for date in sorted(dates):
        assert date in prose, f"pricing.yaml has a note dated {date}; the README should mention it"


def test_the_worked_example_is_what_the_cost_engine_says() -> None:
    prices = load_pricing(path=builtin_path())
    astra, sol, luna = (prices.priced(m) for m in ("gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"))
    assert astra and sol and luna
    assert (astra.input, astra.cached_input, astra.output) == (Decimal(10), Decimal(1), Decimal(50))
    assert (sol.input, sol.cached_input, sol.output) == (Decimal(2), Decimal("0.10"), Decimal(10))
    assert (astra.input / luna.input, astra.output / luna.output) == (Decimal(100), Decimal(100))  # "up to 100x"
    session = Usage(input=40_000, cache_read=60_000, output=6_000)
    assert usage_cost_ucents(session, astra) == 76_000_000  # $0.76
    assert usage_cost_ucents(session, sol) == 14_600_000  # $0.146
    text = readme_text()
    for needle in ("40,000 x $10", "60,000 x $1 ", "6,000 x $50", "= $0.76", "= $0.146", "5.2x", "76,000,000"):
        assert needle in text, f"the worked example should contain {needle!r}"


def dollars(ucents: int) -> str:
    """``$0.76``, ``$0.146``: dollars with at least two decimals and no zero beyond them, as the README prose writes a small amount."""
    return "$" + price_text(Decimal(ucents) / Decimal(100_000_000))


def rate(price: Decimal) -> str:
    """``$10``, ``$0.10``: a price per million tokens, whole dollars without decimals."""
    return "$" + (str(int(price)) if price == price.to_integral_value() else price_text(price))


def tenths(part: int, whole: int) -> str:
    """``part / whole`` with one decimal, rounded half up, from exact decimals."""
    return str((Decimal(part) / Decimal(whole)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def parsed_fixture(agent: str) -> SessionRecord:
    parser = get_parser(agent)
    (path,) = list(parser.discover(FIXTURES / agent))
    (record,) = parser.parse(path)
    return record


def test_every_figure_of_the_worked_example_and_the_narrative_is_what_the_engine_computes() -> None:
    """The prose repeats the same few numbers in several places. Each sentence is rebuilt here from the parsed fixtures, the
    price table and the router's JSON, so that editing one occurrence (or the price table) fails this test."""
    prices = load_pricing(path=builtin_path())
    astra, sol, pro = (prices.priced(m) for m in ("gpt-6-astra", "gpt-6.1-sol", "gemini-2.5-pro"))
    assert astra and sol and pro
    codex, gemini = parsed_fixture("codex").total_usage, parsed_fixture("gemini").total_usage
    assert (codex.input, codex.cache_read, codex.output) == (40_000, 60_000, 6_000)
    done = agentfare(["advise", "--all", "--json"])
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    by_agent = {s["agent"]: s for s in report["sessions"]}
    total = report["total"]

    actual, cheaper = usage_cost_ucents(codex, astra), usage_cost_ucents(codex, sol)
    assert (actual, cheaper) == (by_agent["codex"]["actual"]["ucents"], by_agent["codex"]["recommended"]["ucents"])
    ratio, minimum = tenths(actual, cheaper) + "x", DEFAULT_CONFIG["router"]["min_saving_ratio"]

    def arithmetic(price: ModelPrice) -> tuple[str, str]:
        """``40,000 x $10 + 60,000 x $1 + 6,000 x $50`` and ``$0.40 + $0.06 + $0.30 = $0.76``."""
        terms = [(codex.input, price.input), (codex.cache_read, price.cached_input), (codex.output, price.output)]
        assert all(rate_ is not None for _, rate_ in terms)
        amounts = [int(Decimal(tokens) * rate_ * 100) for tokens, rate_ in terms]  # type: ignore[operator]
        shown = " + ".join(f"{tokens:,} x {rate(rate_)}" for tokens, rate_ in terms)  # type: ignore[arg-type]
        return shown, " + ".join(dollars(a) for a in amounts) + f" = {dollars(sum(amounts))}"

    (astra_terms, astra_sum), (sol_terms, sol_sum) = arithmetic(astra), arithmetic(sol)
    expected = [
        f"({codex.input:,} uncached input tokens, {codex.cache_read:,} cached and {codex.output:,} output on `gpt-6-astra`, at "
        f"{rate(astra.input)}, {rate(astra.cached_input)} and {rate(astra.output)} per million)",  # type: ignore[arg-type]
        f"{astra_terms} = {astra_sum} = {actual:,} micro-cents",
        f"gpt-6-astra {astra_terms} = {astra_sum}",
        f"gpt-6.1-sol {sol_terms} = {sol_sum}",
        f"{dollars(actual)} / {dollars(cheaper)} = {ratio}, which is above the {minimum}x minimum, so the switch is advised",
        f"The Codex session cost {dollars(actual)} on `gpt-6-astra`. The same tokens on `gpt-6.1-sol` cost {dollars(cheaper)}, which is {ratio} less",
        f"It cost {dollars(usage_cost_ucents(gemini, pro))} on `gemini-2.5-pro` and would cost {dollars(usage_cost_ucents(gemini, sol))} on `gpt-6.1-sol`",
        f"{dollars(total['saved']['ucents'])} of {format_usd_compact(total['actual']['ucents'])}, or {tenths(100 * total['saved']['ucents'], total['actual']['ucents'])}%",
        f"The Claude Code session scored {by_agent['claude']['score']} of 10 ({by_agent['claude']['tier']})",
        f"The Claude Code session scores {by_agent['claude']['score']} ({by_agent['claude']['tier']})",
    ]  # fmt: skip
    flat = "\n".join(" ".join(row.split()) for row in readme_text().splitlines())
    for sentence in expected:
        assert sentence in flat, f"the README should say: {sentence}"


# ---- facts the README states about the code --------------------------------------------------------------------
def test_the_readme_lists_the_database_columns_and_the_schema_version() -> None:
    text = readme_text()
    with Store(":memory:") as store:
        for name in ("sessions", "session_usage", "ingested_files"):
            assert f"`{name}`" in text
            for row in store.conn.execute(f"PRAGMA table_info({name})"):
                assert f"`{row['name']}`" in text, f"the README should list column {name}.{row['name']}"
        version = store.conn.execute("PRAGMA user_version").fetchone()[0]
    assert f"(currently {version})" in text


def test_the_readme_names_the_default_models_and_limits() -> None:
    prose = GENERATED.sub("", readme_text())
    router = DEFAULT_CONFIG["router"]
    models = {m for per_agent in router["run"]["models"].values() for m in per_agent.values()} | set(router["recommend"].values())
    for model in sorted(m for m in models if m):
        assert f"`{model}`" in prose, f"the README should name the default model {model}"
    assert f"({router['min_saving_ratio']})" in prose and f"{router['advise']['min_cost_cents']} cents" in prose


def test_the_readme_agrees_with_the_package_metadata() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    urls = project["urls"]
    assert urls["Homepage"] == urls["Source"] == "https://github.com/FernandoSLuz/agentfare"
    assert urls["Issues"] == urls["Homepage"] + "/issues"
    assert project["requires-python"] == ">=3.11"
    for doc in (README, ROOT / "CONTRIBUTING.md"):
        assert f"git clone {urls['Source']}" in doc.read_text(encoding="utf-8")
    text = readme_text()
    assert "Python 3.11 or newer" in text and f"The version is {__version__}" in text


# ---- (d) links ----------------------------------------------------------------------------------------------------
LINK = re.compile(r"!?\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")
LINK_DEFINITION = re.compile(r"^\[[^\]]+\]:\s*(?P<target>\S+)", re.MULTILINE)
HEADING = re.compile(r"^#{1,6}[ \t]+(?P<title>.+?)[ \t#]*$", re.MULTILINE)


def slugs(text: str) -> set[str]:
    """The anchors GitHub gives the headings of a markdown file."""
    seen: dict[str, int] = {}
    found = set()
    for m in HEADING.finditer(FENCE.sub("", text)):
        title = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m["title"]).replace("`", "").lower()
        slug = "".join(c for c in title if c.isalnum() or c in " -_").replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def relative_links(path: Path) -> list[str]:
    text = re.sub(r"`[^`\n]*`", "", FENCE.sub("", path.read_text(encoding="utf-8")))
    targets = [m["target"] for m in LINK.finditer(text)] + [m["target"] for m in LINK_DEFINITION.finditer(text)]
    return [t for t in targets if not re.match(r"[a-z][a-z0-9+.-]*:", t, re.IGNORECASE)]


LINKS = [(path, target) for path in LINKED_DOCS for target in relative_links(path)]


def test_the_docs_have_relative_links_to_check() -> None:
    assert len([1 for path, _ in LINKS if path == README]) >= 8


@pytest.mark.parametrize(("path", "target"), LINKS, ids=[f"{path.name} -> {target}" for path, target in LINKS])
def test_a_relative_link_resolves(path: Path, target: str) -> None:
    file_part, _, anchor = target.partition("#")
    destination = (path.parent / file_part).resolve() if file_part else path
    assert destination.exists(), f"{path.name} links to {target}, which does not exist"
    if anchor and destination.suffix == ".md":
        assert anchor in slugs(destination.read_text(encoding="utf-8")), f"{path.name} links to #{anchor}, which is not a heading of {destination.name}"


def test_the_readme_links_the_documents_it_defers_to() -> None:
    linked = {target.partition("#")[0] for path, target in LINKS if path == README}
    for wanted in (
        "docs/ROUTING.md", "docs/PRICING-SOURCES.md", "docs/LOG-FORMATS.md", "docs/DEMO.md", "docs/ARCHITECTURE.md", "CONTRIBUTING.md", "LICENSE",
    ):  # fmt: skip
        assert wanted in linked, f"the README should link {wanted}"


# ---- style --------------------------------------------------------------------------------------------------------
EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿⭐⬆✅❌]")


@pytest.mark.parametrize("path", [p for p in COMMAND_DOCS if p.exists()], ids=lambda p: p.name)
def test_the_prose_has_no_emoji_and_no_long_dashes(path: Path) -> None:
    prose = without_code(GENERATED.sub("", path.read_text(encoding="utf-8")))
    assert not EMOJI.search(prose), f"{path.name}: no emoji"
    assert "—" not in prose and "–" not in prose, f"{path.name}: use a comma, a colon or a full stop instead of a long dash"


@pytest.mark.parametrize("path", [p for p in (*COMMAND_DOCS, DEMO_SCRIPT, DEMO_TAPE, Path(__file__)) if p.exists()], ids=lambda p: p.name)
def test_no_line_ends_in_spaces(path: Path) -> None:
    lines = [n for n, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1) if line != line.rstrip()]
    assert not lines, f"{path.name}: spaces at the end of lines {lines[:5]}; `git diff --check` flags them (`python tests/test_readme.py --write` fixes the output blocks)"


# ---- the demo -----------------------------------------------------------------------------------------------------
def run_demo() -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as tmp:
        env = pinned_env(Path(tmp))
        for name in ("AGENTFARE_HOME", "AGENTFARE_NOW", "AGENTFARE_CONFIG"):
            env.pop(name)  # the script sets its own
        env.update(DEMO_PAUSE="0", DEMO_TYPE="0", DEMO_CLEAR="0", AGENTFARE_CMD=f"{sys.executable} -m agentfare")
        done = subprocess.run(["bash", str(DEMO_SCRIPT)], cwd=tmp, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
        assert not (Path(tmp) / ".agentfare").exists(), "the demo must not write to the real ~/.agentfare"
        return done


needs_bash = pytest.mark.skipif(shutil.which("bash") is None or " " in sys.executable, reason="needs bash and an interpreter path without spaces")


@needs_bash
def test_the_demo_script_runs_and_prints_each_command_before_its_output() -> None:
    done = run_demo()
    assert done.returncode == 0, done.stderr
    out = done.stdout
    steps = [
        "$ agentfare summary", "AgentFare summary: all time",
        "$ agentfare sessions", "AgentFare sessions: all time",
        "$ agentfare advise", "AgentFare advise: all time",
        '$ agentfare run --dry-run -- claude -p "fix the typo in the README"', "tier      light (score 0)",
        '$ agentfare run --dry-run -- claude -p "refactor the billing module and migrate it to the new API"', "tier      heavy (score 4)",
        "$ agentfare report --format md | sed -n 1,13p", "# AgentFare weekly report: 2026-W40",
    ]  # fmt: skip
    positions = [out.index(step) for step in steps]
    assert positions == sorted(positions), "each command must come before its own output"
    assert "$3.63" in out and "5.2x cheaper" in out


@needs_bash
def test_the_demo_script_falls_back_to_python_when_agentfare_is_not_on_the_path() -> None:
    """Without AGENTFARE_CMD and with no `agentfare` program on PATH the script runs `$PYTHON -m agentfare` (the shell function
    that shows the command must not be mistaken for the program)."""
    with tempfile.TemporaryDirectory() as tmp:
        env = pinned_env(Path(tmp))
        for name in ("AGENTFARE_HOME", "AGENTFARE_NOW", "AGENTFARE_CONFIG", "AGENTFARE_CMD"):
            env.pop(name, None)
        bare = [d for d in env["PATH"].split(os.pathsep) if d and not (Path(d) / "agentfare").exists()]
        env.update(PATH=os.pathsep.join(bare), PYTHON=sys.executable, DEMO_PAUSE="0", DEMO_TYPE="0", DEMO_CLEAR="0")
        assert shutil.which("agentfare", path=env["PATH"]) is None
        for tool in ("bash", "mktemp", "sed", "sleep"):
            if shutil.which(tool, path=env["PATH"]) is None:
                pytest.skip(f"{tool} lives in a directory that also holds agentfare")
        done = subprocess.run(["bash", str(DEMO_SCRIPT)], cwd=tmp, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert done.returncode == 0, done.stderr
    assert "total spend $3.63" in done.stdout


@needs_bash
def test_the_demo_script_is_deterministic_and_matches_the_readme() -> None:
    first, second = run_demo(), run_demo()
    assert first.returncode == 0 and second.returncode == 0
    assert first.stdout == second.stdout
    steps = re.findall(r"^step '(.*)'$", DEMO_SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    shared = [(command, expected) for command, expected in RUN_BLOCKS if command in steps]
    assert len(shared) >= 3, "the README and the demo should show the same summary, advise and run examples"
    for command, expected in shared:
        assert squash(expected) in squash(first.stdout), f"README block of `{command}` differs from what scripts/demo.sh prints"


def test_the_vhs_tape_records_the_demo_script() -> None:
    tape = DEMO_TAPE.read_text(encoding="utf-8")
    assert "Output docs/demo.gif" in tape and "bash scripts/demo.sh" in tape
    closing = re.search(r"Wait@\S+ /(.+)/", tape)
    assert closing, "the tape should wait for the end of the demo"
    assert re.search(closing[1], DEMO_SCRIPT.read_text(encoding="utf-8")), "the tape waits for text the demo never prints"
    assert os.access(DEMO_SCRIPT, os.X_OK), "scripts/demo.sh should be executable"


# ---- claims a first reader can check ---------------------------------------------------------------------------------
def test_what_the_readme_says_about_the_size_of_the_clone_is_true() -> None:
    """`run` counts the files under the directory it starts in, so the README tells people to run it from a small one: the clone has
    more files than the `repo_files` rule needs to add a point, and it must keep saying so only while that is true."""
    from agentfare.router import count_files

    medium = DEFAULT_CONFIG["router"]["run"]["repo_files"]["medium"]
    assert count_files(ROOT, medium) >= medium, f"the clone has fewer than {medium} files: the README (both languages) says it has more"
    for path in (README, README_PT):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"fewer than 200 files|menos de 200 arquivos", text), f"{path.name} says the clone is small enough for `run`"
        assert re.search(r"more than 200 files|mais de 200 arquivos", text), f"{path.name} should tell people to run `run` from a small directory"


@pytest.mark.parametrize("path", [README, README_PT, ROOT / "docs" / "PRICING-SOURCES.md"], ids=lambda p: p.name)
def test_no_recipe_needs_agentfare_to_be_importable_from_the_python_on_path(path: Path) -> None:
    """uv and pipx keep AgentFare in an environment of its own, so `python -c 'import agentfare...'` fails after the install the
    README recommends (and `python` does not exist on a stock macOS or Debian). A recipe asks the `agentfare` command instead."""
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"python3? -c ['\"]\s*import agentfare", text), f"{path.name}: use `agentfare prices --json` rather than importing the package"


# ---- maintenance entry point --------------------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    if argv[1:] != ["--write"]:
        print("usage: python tests/test_readme.py --write   (rebuilds the generated tables and output blocks of README.md)")
        return 2
    before = readme_text()
    after = synced(before)
    if after == before:
        print("README.md is up to date")
    else:
        README.write_text(after, encoding="utf-8")
        print("README.md updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
