"""`agentfare run`: the routing wrapper, driven against fake agent scripts.

The fakes are tiny Python scripts named ``claude``, ``codex``, ``gemini`` and ``custom`` on a throwaway PATH. Each one
records the argv and the AGENTFARE_* variables it received, and exits with the status the test asks for, so every claim
about "what the agent got" is read back from a real child process. No real agent is started, nothing touches the
network, and prices are pinned in the throwaway home so the estimates below are hand-computable:

  light task on claude-sonnet-5-5: 30000 x 2.0 + 600000 x 0.20 + 25000 x 2.5 + 7000 x 10.0 = 312500 -> $0.3125
  light task on claude-opus-5-5  : 30000 x 4.0 + 600000 x 0.20 + 25000 x 5.0 + 7000 x 20.0 = 505000 -> $0.5050
  (dollars per 1M tokens, with the default token budgets of ``router.run.default_tokens``)
"""

from __future__ import annotations

import json
import os
import pty
import random
import re
import select
import signal
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from agentfare.cli import app
from agentfare.commands import run as run_module
from agentfare.config import DEFAULT_CONFIG, ConfigError
from agentfare.db import Store
from agentfare.models import Usage
from agentfare.runwrap import Invocation, analyze, child_env, resolve_agent

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_CONFIG = FIXTURES / "config.yaml"

PRICES = """\
schema: 1
models:
  claude-opus-5-5:        {provider: anthropic, input: 4.0, cached_input: 0.20, cache_write: 5.0, output: 20.0}
  claude-sonnet-5-5:      {provider: anthropic, input: 2.0, cached_input: 0.20, cache_write: 2.5, output: 10.0}
  claude-haiku-4-5:       {provider: anthropic, input: 1.0, cached_input: 0.10, cache_write: 1.25, output: 5.0}
  gpt-6-astra:            {provider: openai, input: 10.0, cached_input: 1.0, cache_write: 12.5, output: 50.0}
  gpt-6.1-sol:            {provider: openai, input: 2.0, cached_input: 0.10, cache_write: 2.5, output: 10.0}
  gemini-3.1-pro-preview: {provider: google, input: 2.0, cached_input: 0.20, output: 12.0}
  gemini-3-flash-preview: {provider: google, input: 0.5, cached_input: 0.05, output: 3.0}
  gemini-3.1-flash-lite:  {provider: google, input: 0.25, cached_input: 0.025, output: 1.5}
  shaky-model:            {provider: nobody, input: unverified, output: 10.0}
"""

FAKE_AGENT = """\
#!{python}
import json, os, signal, sys, time

with open(os.environ["FAKE_AGENT_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{
        "name": os.path.basename(sys.argv[0]),
        "argv": sys.argv[1:],
        "tier": os.environ.get("AGENTFARE_TIER"),
        "model": os.environ.get("AGENTFARE_MODEL"),
        "time": time.time(),
    }}) + "\\n")
print("FAKE-AGENT-RAN", flush=True)
if os.environ.get("FAKE_AGENT_SIGNAL"):
    os.kill(os.getpid(), int(os.environ["FAKE_AGENT_SIGNAL"]))
    time.sleep(5)
sys.exit(int(os.environ.get("FAKE_AGENT_EXIT", "0")))
"""

MARKER = "PRIVACY-CANARY-RUN-9c1f"


@pytest.fixture(autouse=True)
def pinned_prices(isolated_machine: None) -> None:
    (Path(os.environ["AGENTFARE_HOME"]) / "pricing.yaml").write_text(PRICES)


@dataclass
class World:
    bin: Path
    log: Path
    repo: Path

    def calls(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def only_call(self) -> dict[str, Any]:
        [call] = self.calls()
        return call


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    """Fake agents first on PATH, a small repo as the working directory, and a log every fake appends to."""
    bin_dir, repo = tmp_path / "bin", tmp_path / "repo"
    bin_dir.mkdir()
    repo.mkdir()
    for name in ("README.md", "app.py", "notes.txt"):
        (repo / name).write_text("x")
    for name in ("claude", "codex", "gemini", "custom"):
        script = bin_dir / name
        script.write_text(FAKE_AGENT.format(python=sys.executable))
        script.chmod(0o755)
    broken = bin_dir / "broken"  # executable, but no shebang and not a binary: the kernel refuses to start it
    broken.write_text("echo never\n")
    broken.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_AGENT_LOG", str(log))
    monkeypatch.setenv("AGENTFARE_NOW", "2026-09-30T12:00:00Z")
    monkeypatch.chdir(repo)
    return World(bin_dir, log, repo)


def write_config(tmp_path: Path, **overrides: Any) -> Path:
    """The fixture config (sources, UTC) plus these top-level overrides, merged the way the real loader merges them."""
    data = yaml.safe_load(FIXTURE_CONFIG.read_text())
    data["sources"] = {"claude": [str(FIXTURES / "claude/projects")], "codex": [str(FIXTURES / "codex/sessions")], "gemini": [str(FIXTURES / "gemini/tmp")]}
    for key, value in overrides.items():
        data[key] = {**data.get(key, {}), **value} if isinstance(value, dict) and isinstance(data.get(key), dict) else value
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def run(*args: str, config: Path = FIXTURE_CONFIG, tty: bool = False, input: str | None = None, env: dict[str, str] | None = None, cli: tuple[str, ...] = ()) -> Result:
    """``agentfare run <args>`` as if piped (no colour, 80 columns); ``tty=True`` makes stdin look like a terminal."""
    argv = ["--config", str(config), "--no-ingest", *cli, "run", *args]
    return CliRunner().invoke(app, argv, input=input, env={"COLUMNS": "80", "FORCE_COLOR": None, "NO_COLOR": None, **(env or {})})  # type: ignore[dict-item]


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_module, "_stdin_is_tty", lambda: True)


def flat(text: str) -> str:
    """The panel as one line of words: box borders removed and the 80-column wrapping undone."""
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


def panel(result: Result) -> str:
    return flat(result.stdout + result.stderr)


# ---- reading command lines (pure) ----------------------------------------------------------------------------
def read(agent: str | None, *argv: str, flag: str | None = "--model") -> Invocation:
    return analyze(argv, agent, flag)


@pytest.mark.parametrize(
    ("agent", "argv", "prompt"),
    [
        ("claude", ("claude", "-p", "fix it"), "fix it"),
        ("claude", ("claude", "fix it"), "fix it"),
        ("claude", ("claude", "-p", "fix it", "--output-format", "json"), "fix it"),
        ("claude", ("claude", "--output-format", "json", "-p", "fix it"), "fix it"),
        ("claude", ("claude", "--model", "opus", "--effort", "high", "-p", "fix it"), "fix it"),
        ("claude", ("claude", "--allowedTools", "Edit", "Bash", "-p", "fix it"), "fix it"),
        ("claude", ("claude", "--resume", "-p", "fix it"), "fix it"),  # optional value, next argument is an option
        ("claude", ("claude", "-c", "-p", "fix it"), "fix it"),
        ("claude", ("claude", "-p", "--", "--not-a-flag"), "--not-a-flag"),
        ("claude", ("claude", "--append-system-prompt", "be brief", "do it"), "do it"),
        ("claude", ("claude",), None),  # interactive: the prompt is typed later
        ("claude", ("claude", "-p"), None),  # print mode reading stdin
        ("claude", ("claude", "--allowedTools", "Edit", "fix it"), None),  # the variadic option swallows it, as claude does
        ("codex", ("codex", "exec", "fix it"), "fix it"),
        ("codex", ("codex", "e", "fix it"), "fix it"),
        ("codex", ("codex", "fix it"), "fix it"),
        ("codex", ("codex", "exec", "--json", "-m", "gpt-6", "-C", "/tmp", "fix it"), "fix it"),
        ("codex", ("codex", "exec", "-"), None),  # "-" reads the prompt from stdin
        ("codex", ("codex", "exec", "resume", "--last", "fix it"), None),  # session id or prompt: not guessed
        ("codex", ("codex", "-c", "sandbox=x", "exec", "fix it"), "fix it"),
        ("gemini", ("gemini", "-p", "fix it"), "fix it"),
        ("gemini", ("gemini", "--prompt", "fix it"), "fix it"),
        ("gemini", ("gemini", "--prompt=fix it"), "fix it"),
        ("gemini", ("gemini", "fix", "the", "typo"), "fix the typo"),
        ("gemini", ("gemini", "-m", "gemini-3-flash-preview", "-y", "fix it"), "fix it"),
        ("gemini", ("gemini", "-i", "fix it"), "fix it"),
        ("gemini", ("gemini", "--include-directories", "a", "b", "-p", "fix it"), "fix it"),
        ("gemini", ("gemini",), None),
        ("custom", ("custom", "--foo", "fix it"), None),  # only the three built-in CLIs are read for a prompt
        (None, ("mystery", "fix it"), None),
    ],
)
def test_the_prompt_is_read_the_way_each_cli_reads_it(agent: str | None, argv: tuple[str, ...], prompt: str | None) -> None:
    assert read(agent, *argv).prompt == prompt


@pytest.mark.parametrize(
    ("agent", "argv", "model"),
    [
        ("claude", ("claude", "--model", "opus", "-p", "x"), "opus"),
        ("claude", ("claude", "--model=claude-haiku-4-5", "x"), "claude-haiku-4-5"),
        ("claude", ("claude", "-p", "x", "--fallback-model", "opus"), None),  # a different option
        ("claude", ("claude", "-p", "--", "--model", "opus"), None),  # after -- it is text
        ("claude", ("claude", "-m", "opus", "x"), None),  # claude has no -m
        ("codex", ("codex", "exec", "-m", "gpt-6", "x"), "gpt-6"),
        ("codex", ("codex", "exec", "-mgpt-6", "x"), "gpt-6"),
        ("codex", ("codex", "exec", "-m=gpt-6", "x"), "gpt-6"),
        ("codex", ("codex", "exec", "--model", "gpt-6", "x"), "gpt-6"),
        ("codex", ("codex", "-c", 'model="o3"', "exec", "x"), "o3"),
        ("codex", ("codex", "--config", "model='o3'", "x"), "o3"),
        ("codex", ("codex", "-c", "model_reasoning_effort=high", "x"), None),
        ("gemini", ("gemini", "-m", "gemini-3-flash-preview", "x"), "gemini-3-flash-preview"),
        ("gemini", ("gemini", "--model=gemini-3-flash-preview", "x"), "gemini-3-flash-preview"),
        ("custom", ("custom", "--llm", "m1", "x"), None),  # with --model as the flag it is not the flag
        ("claude", ("claude", "--model", "a", "--model", "b", "x"), "b"),  # the last one wins, as in the CLI
    ],
)
def test_a_model_the_command_already_selects_is_found(agent: str, argv: tuple[str, ...], model: str | None) -> None:
    assert read(agent, *argv).model == model


def test_a_custom_agents_own_model_flag_is_found() -> None:
    assert read("custom", "custom", "--llm", "m1", "x", flag="--llm").model == "m1"
    assert read("custom", "custom", "--llm=m1", "x", flag="--llm").model == "m1"
    assert read("custom", "custom", "-M", "m1", "x", flag="-M").model == "m1"


@pytest.mark.parametrize(
    ("agent", "argv", "expected"),
    [
        ("claude", ("claude", "-p", "fix it"), ("claude", "--model", "M", "-p", "fix it")),
        ("claude", ("claude", "-p", "--", "--x"), ("claude", "--model", "M", "-p", "--", "--x")),
        ("claude", ("claude",), ("claude", "--model", "M")),
        ("codex", ("codex", "exec", "fix it"), ("codex", "exec", "--model", "M", "fix it")),
        ("codex", ("codex", "exec", "--json", "-", "--"), ("codex", "exec", "--model", "M", "--json", "-", "--")),
        ("codex", ("codex", "-c", "a=b", "exec", "fix it"), ("codex", "-c", "a=b", "exec", "--model", "M", "fix it")),
        ("codex", ("codex", "exec", "resume", "--last"), ("codex", "exec", "resume", "--model", "M", "--last")),
        ("codex", ("codex", "exec", "review", "--uncommitted"), ("codex", "exec", "review", "--model", "M", "--uncommitted")),
        ("codex", ("codex", "resume", "--last"), ("codex", "resume", "--model", "M", "--last")),
        ("codex", ("codex", "fix it"), ("codex", "--model", "M", "fix it")),
        ("gemini", ("gemini", "-p", "fix it"), ("gemini", "--model", "M", "-p", "fix it")),
        ("gemini", ("gemini", "fix it"), ("gemini", "--model", "M", "fix it")),
        (None, ("mystery", "sub", "--", "x"), ("mystery", "--model", "M", "sub", "--", "x")),
    ],
)
def test_the_model_flag_goes_after_the_command_and_before_every_option_marker_or_prompt(agent: str | None, argv: tuple[str, ...], expected: tuple[str, ...]) -> None:
    assert read(agent, *argv).with_model("--model", "M").argv == expected


@pytest.mark.parametrize("argv", [("codex", "--", "exec"), ("codex", "--json", "--", "resume", "--last"), ("codex", "-C", "/x", "--", "fork", "fix it")])
def test_a_subcommand_name_after_a_double_dash_never_moves_the_model_flag_past_it(argv: tuple[str, ...]) -> None:
    """clap reads everything after -- as prompt text, so a flag put there would be part of the prompt and pick nothing."""
    new = read("codex", *argv).with_model("--model", "M").argv
    assert new.index("--model") < new.index("--")


WORDS = {
    "claude": ["-p", "--output-format", "json", "--allowedTools", "Edit", "--add-dir", "/x", "--resume", "--", "--model", "opus", "mcp", "update", "fix it", "-c", "-", "-w", "--effort", "high"],
    "codex": ["exec", "e", "resume", "review", "fork", "-m", "gpt", "-c", 'model="x"', "sandbox=x", "-C", "/x", "-i", "a.png", "--json", "--", "-", "fix it", "login", "--last", "-p", "prof"],
    "gemini": ["-p", "--prompt", "fix", "the", "typo", "-m", "g", "--model=g", "-y", "-i", "-r", "--", "mcp", "--include-directories", "a", "b", "-e", "x", "--prompt=hi there"],
}


@pytest.mark.parametrize("agent", list(WORDS))
def test_a_model_flag_injected_into_any_command_line_is_read_back_and_changes_nothing_else(agent: str) -> None:
    rng = random.Random(agent)
    checked = 0
    for _ in range(3000):
        argv = (agent, *(rng.choice(WORDS[agent]) for _ in range(rng.randint(0, 7))))
        inv = read(agent, *argv)
        if not inv.session or inv.model:
            continue
        new = inv.with_model("--model", "M")
        back = read(agent, *new.argv)
        at = inv.insert_at
        assert (*new.argv[:at], *new.argv[at + 2:]) == argv
        assert new.argv[at:at + 2] == ("--model", "M")
        assert (back.model, back.prompt, back.words, back.session) == ("M", inv.prompt, inv.words, True), argv
        checked += 1
    assert checked > 500


@pytest.mark.parametrize(
    ("agent", "argv", "words"),
    [
        ("claude", ("claude", "mcp", "list"), ("claude", "mcp")),
        ("claude", ("claude", "update"), ("claude", "update")),
        ("codex", ("codex", "login"), ("codex", "login")),
        ("codex", ("codex", "review", "--uncommitted"), ("codex", "review")),  # codex review has no --model option
        ("gemini", ("gemini", "extensions", "list"), ("gemini", "extensions")),
    ],
)
def test_commands_that_never_talk_to_a_model_are_not_routed(agent: str, argv: tuple[str, ...], words: tuple[str, ...]) -> None:
    inv = read(agent, *argv)
    assert (inv.session, inv.words, inv.prompt) == (False, words, None)


def test_a_prompt_that_looks_like_a_subcommand_only_is_one_when_it_is_the_whole_argument() -> None:
    assert read("claude", "claude", "-p", "update the docs").session
    assert not read("claude", "claude", "update").session


def test_the_display_shows_the_prompt_as_its_length_and_quotes_for_a_shell() -> None:
    inv = read("claude", "claude", "-p", "it's a \"secret\" $HOME", "--add-dir", "a;b").with_model("--model", "m")
    shown = inv.display()
    assert "secret" not in shown and "HOME" not in shown
    assert shown == "claude --model m -p '<prompt: 21 chars>' --add-dir '<arg: 3 chars>'"  # a flag keeps its name, its value is only a length
    assert read("gemini", "gemini", "--prompt=hello there").display() == "gemini '--prompt=<prompt: 11 chars>'"
    assert read("gemini", "gemini", "-p").display() == "gemini -p"


def test_the_display_hides_the_prompt_and_nothing_else_even_when_it_repeats_part_of_its_own_flag() -> None:
    assert read("gemini", "gemini", "--prompt=prompt").display() == "gemini '--prompt=<prompt: 6 chars>'"
    assert read("gemini", "gemini", "-pp").display() == "gemini '-p<prompt: 1 char>'"


def test_agents_are_found_by_the_basename_of_the_command() -> None:
    agents = DEFAULT_CONFIG["agents"]
    assert resolve_agent(agents, "claude") is not None and resolve_agent(agents, "claude").model_flag == "--model"  # type: ignore[union-attr]
    assert resolve_agent(agents, "/usr/local/bin/codex").name == "codex"  # type: ignore[union-attr]
    assert resolve_agent(agents, "./gemini").name == "gemini"  # type: ignore[union-attr]
    assert resolve_agent(agents, "claude-code") is None
    custom = {**agents, "mine": {"commands": ["mine", "/opt/bin/mine-cli"], "model_flag": "-M"}, "bare": {"commands": "bare", "model_flag": None}}
    assert resolve_agent(custom, "mine-cli").model_flag == "-M"  # type: ignore[union-attr]
    assert resolve_agent(custom, "bare").model_flag is None  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "agents",
    [[], {"a": 3}, {"a": {"model_flag": "--model"}}, {"a": {"commands": []}}, {"a": {"commands": [1]}}, {"a": {"commands": ["a"], "model_flag": 5}}, {"a": {"commands": ["a"], "model_flag": " "}}, {"a": {"commands": ["a"], "model_flag": "model"}}, {"a": {"commands": ["a"], "model_flag": "--model="}}, {"a": {"commands": ["a"], "model_flag": "--a b"}}],
)
def test_a_broken_agents_table_is_a_config_error(agents: Any) -> None:
    with pytest.raises(ConfigError, match="agents"):
        resolve_agent(agents, "a")


def test_the_child_environment_carries_the_choice_and_drops_a_stale_one() -> None:
    assert child_env({"A": "1"}, "heavy", "m") == {"A": "1", "AGENTFARE_TIER": "heavy", "AGENTFARE_MODEL": "m"}
    stale = {"AGENTFARE_TIER": "light", "AGENTFARE_MODEL": "old", "A": "1"}
    assert child_env(stale, "medium", None) == {"A": "1", "AGENTFARE_TIER": "medium"}
    assert child_env(stale, None, None) == {"A": "1"}
    assert "AGENTFARE_MODEL" in stale  # the caller's mapping is not touched


# ---- --dry-run ---------------------------------------------------------------------------------------------
def test_dry_run_prints_the_decision_and_spawns_nothing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*args: object, **kwargs: object) -> None:
        raise AssertionError("--dry-run started a process")

    monkeypatch.setattr(run_module.subprocess, "Popen", never)
    result = run("--dry-run", "--", "claude", "-p", "fix the typo in the README")
    assert result.exit_code == 0, result.output
    assert not world.log.exists()
    text = flat(result.stdout)
    assert "agentfare run (dry run)" in text
    assert "agent claude" in text
    assert "tier light (score 0)" in text
    assert "- light keywords typo, readme: -3" in text  # 2 for the first distinct keyword, 1 more for the second: the score stays at 0
    assert "model claude-sonnet-5-5 (router pick)" in text
    assert "estimate ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet)" in text
    assert "baseline ~$0.5050 on claude-opus-5-5" in text
    assert "saving ~$0.1925 (38.1% less than the baseline)" in text
    assert "command claude --model claude-sonnet-5-5 -p '<prompt: 26 chars>'" in text


def test_dry_run_needs_no_terminal_and_no_confirmation(world: World) -> None:
    result = run("--dry-run", "--", "claude", "-p", "x", input="")
    assert result.exit_code == 0
    assert "Run this?" not in result.output and not world.log.exists()


def test_dry_run_wins_over_yes(world: World) -> None:
    assert run("--dry-run", "--yes", "--", "claude", "-p", "x").exit_code == 0
    assert not world.log.exists()


def test_the_decision_fits_80_columns_and_has_no_colour_codes(world: World) -> None:
    result = run("--dry-run", "--", "claude", "-p", "refactor everything", "--add-dir", "/a/very/long/path/that/goes/on/and/on/and/on/forever/and/ever")
    lines = result.stdout.splitlines()
    assert lines and max(len(line) for line in lines) <= 80
    assert "\x1b" not in result.output


# ---- printing first, then asking -------------------------------------------------------------------------------
def test_without_a_terminal_and_without_yes_it_refuses(world: World) -> None:
    result = run("--", "claude", "-p", "fix the typo")
    assert result.exit_code == 3
    assert not world.log.exists()
    assert "--yes" in result.stderr and "not a terminal" in result.stderr
    assert "model claude-sonnet-5-5" in flat(result.stderr)  # the pick was shown before refusing


def test_yes_runs_the_agent_with_the_injected_flag_and_the_choice_in_its_environment(world: World) -> None:
    result = run("--yes", "--", "claude", "-p", "fix the typo")
    assert result.exit_code == 0, result.output
    call = world.only_call()
    assert call["argv"] == ["--model", "claude-sonnet-5-5", "-p", "fix the typo"]
    assert (call["tier"], call["model"]) == ("light", "claude-sonnet-5-5")
    assert "model claude-sonnet-5-5 (router pick)" in flat(result.stderr)


def test_the_decision_goes_to_stderr_so_the_agents_output_stays_clean(world: World, tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "agentfare", "--config", str(FIXTURE_CONFIG), "--no-ingest", "run", "-y", "--", "claude", "-p", "fix the typo"],
        capture_output=True, text=True, timeout=120, env={**os.environ, "COLUMNS": "80"},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "FAKE-AGENT-RAN\n"
    assert "agentfare run" in done.stderr and "claude-sonnet-5-5" in done.stderr


def test_the_pick_is_printed_before_the_agent_starts(world: World) -> None:
    """One pipe for both streams shows the real order in which the two processes wrote."""
    done = subprocess.run(
        [sys.executable, "-m", "agentfare", "--config", str(FIXTURE_CONFIG), "--no-ingest", "run", "-y", "--", "claude", "-p", "fix the typo"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120, env={**os.environ, "COLUMNS": "80"},
    )
    assert done.returncode == 0
    command = re.search(r"command\s+claude --model", done.stdout)
    assert command and 0 <= done.stdout.index("╭─ agentfare run") < command.start() < done.stdout.index("FAKE-AGENT-RAN")


def test_the_pick_is_printed_before_the_question_and_before_the_agent(world: World, tty: None) -> None:
    result = run("--", "claude", "-p", "fix the typo", input="y\n")
    assert result.exit_code == 0, result.output
    text = result.stderr
    assert text.index("agentfare run") < re.search(r"command\s+claude --model", text).start() < text.index("Run this?")  # type: ignore[union-attr]
    assert "[Y/n]" in text
    assert world.only_call()["argv"][:2] == ["--model", "claude-sonnet-5-5"]


@pytest.mark.parametrize(("answer", "runs"), [("y\n", True), ("Y\n", True), ("\n", True), ("n\n", False), ("no\n", False), ("", False)])
def test_on_a_terminal_it_asks_and_yes_is_the_default(world: World, tty: None, answer: str, runs: bool) -> None:
    result = run("--", "claude", "-p", "x", input=answer)
    assert bool(world.calls()) is runs
    assert result.exit_code == (0 if runs else 1)


def test_a_closed_stdin_is_not_a_terminal_it_is_the_refusal_not_a_crash(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """`agentfare run -- claude <&-` leaves sys.stdin as None."""
    monkeypatch.setattr(sys, "stdin", None)
    assert run_module._stdin_is_tty() is False


def test_yes_on_a_terminal_does_not_ask(world: World, tty: None) -> None:
    result = run("-y", "--", "claude", "-p", "x")
    assert result.exit_code == 0 and "Run this?" not in result.output
    assert len(world.calls()) == 1


# ---- the agent's command line reaches it untouched -------------------------------------------------------------
PROMPTS = [
    "plain words",
    "she said \"hi\" and 'bye'",
    "line one\nline two\n\ttabbed\r\nwindows",
    "$(touch PWNED) `touch PWNED` ${HOME} $HOME; touch PWNED && echo x | cat > /dev/null &",
    "  leading and trailing spaces  ",
    "ünïcødé ✓ 日本語 🚀",
    "*.py [abc] ~ {a,b} !history #comment",
    "back\\slash \\n literal \\\\ two",
    "",
]


@pytest.mark.parametrize("prompt", PROMPTS)
@pytest.mark.parametrize(
    "command",
    [("claude", "-p"), ("codex", "exec"), ("gemini", "-p"), ("gemini",), ("codex",), ("claude",)],
    ids=["claude -p", "codex exec", "gemini -p", "gemini", "codex", "claude"],
)
def test_prompts_arrive_byte_identical(world: World, command: tuple[str, ...], prompt: str) -> None:
    result = run("--yes", "--", *command, prompt)
    assert result.exit_code == 0, result.output
    assert not (world.repo / "PWNED").exists()  # no shell ever saw `$(touch PWNED)`
    argv = world.only_call()["argv"]
    assert argv[-1] == prompt
    assert argv.count("--model") == 1


@pytest.mark.parametrize("prompt", ["-x", "--model=evil", "--model", "--yes", "--dry-run", "-", "--", "-p", "--prompt=x"])
@pytest.mark.parametrize("command", [("claude", "-p", "--"), ("codex", "exec", "--"), ("codex", "--"), ("gemini", "--")], ids=["claude -p --", "codex exec --", "codex --", "gemini --"])
def test_a_prompt_that_looks_like_an_option_arrives_after_the_double_dash_untouched(world: World, command: tuple[str, ...], prompt: str) -> None:
    result = run("--yes", "--", *command, prompt)
    assert result.exit_code == 0, result.output
    argv = world.only_call()["argv"]
    assert argv[-2:] == ["--", prompt]
    assert argv.index("--model") < argv.index("--")  # the injected flag is before the marker, so it is a flag
    assert argv.count("--model") == 1 + (prompt == "--model")


def test_a_prompt_after_the_agents_own_double_dash_stays_after_it(world: World) -> None:
    result = run("--yes", "--", "claude", "-p", "--", "--model", "not-a-model")
    assert result.exit_code == 0, result.output
    assert world.only_call()["argv"] == ["--model", "claude-sonnet-5-5", "-p", "--", "--model", "not-a-model"]


def test_the_agents_own_flags_pass_through_untouched(world: World) -> None:
    args = ["--output-format", "json", "--allowedTools", "Edit", "Bash", "--add-dir", "../x", "-p", "fix the typo", "--verbose"]
    assert run("--yes", "--", "claude", *args).exit_code == 0
    assert world.only_call()["argv"] == ["--model", "claude-sonnet-5-5", *args]


def test_agentfares_own_flag_names_after_the_command_belong_to_the_agent(world: World) -> None:
    """Options of agentfare end where the agent command starts: `--prompt`, `--yes` and `--dry-run` after it are the agent's."""
    args = ["--prompt", "fix the typo", "--yes", "--dry-run"]
    assert run("-y", "--", "gemini", *args).exit_code == 0
    assert world.only_call()["argv"] == ["--model", "gemini-3.1-flash-lite", *args]


def test_the_double_dash_is_optional_when_the_command_starts_with_a_word(world: World) -> None:
    assert run("-y", "claude", "-p", "fix the typo", "--model", "claude-haiku-4-5").exit_code == 0
    assert world.only_call()["argv"] == ["-p", "fix the typo", "--model", "claude-haiku-4-5"]


def test_the_agent_is_started_without_a_shell_and_with_the_terminals_stdio(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    real, seen = subprocess.Popen, []

    def spy(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        seen.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(run_module.subprocess, "Popen", spy)
    assert run("-y", "--", "claude", "-p", "a; b").exit_code == 0
    [(args, kwargs)] = seen
    assert isinstance(args[0], list) and args[0][-1] == "a; b"
    assert not kwargs.get("shell") and not {"stdin", "stdout", "stderr"} & kwargs.keys()


@pytest.mark.parametrize("code", [0, 1, 2, 3, 42, 126, 130, 255])
def test_the_agents_exit_code_is_forwarded(world: World, code: int) -> None:
    result = run("-y", "--", "claude", "-p", "x", env={"FAKE_AGENT_EXIT": str(code)})
    assert result.exit_code == code


def test_an_agent_killed_by_a_signal_reports_128_plus_the_signal(world: World) -> None:
    assert run("-y", "--", "claude", "-p", "x", env={"FAKE_AGENT_SIGNAL": "15"}).exit_code == 143


def test_the_agent_reads_the_stdin_of_the_wrapper_untouched(world: World) -> None:
    """Nothing in the wrapper may read stdin first: `cat prompt.txt | agentfare run -y -- claude -p` feeds the agent."""
    reader = world.bin / "reader"
    reader.write_text(f"#!{sys.executable}\nimport sys\nprint(repr(sys.stdin.read()))\n")
    reader.chmod(0o755)
    done = subprocess.run(
        [sys.executable, "-m", "agentfare", "--config", str(FIXTURE_CONFIG), "--no-ingest", "run", "-y", "--", "reader"],
        input="first line\nsecond line\n", capture_output=True, text=True, timeout=120, env={**os.environ, "COLUMNS": "80"},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "'first line\\nsecond line\\n'\n"


def test_a_command_that_is_not_installed_exits_127_after_the_decision(world: World) -> None:
    result = run("-y", "--", "agentfare-no-such-agent", "--flag", "x")
    assert result.exit_code == 127
    assert "command not found: agentfare-no-such-agent" in result.stderr
    assert result.stderr.index("agentfare run") < result.stderr.index("error:")
    assert "command not found on PATH" in flat(result.stderr)
    assert not world.log.exists()


def test_dry_run_of_a_missing_command_still_shows_the_decision(world: World) -> None:
    result = run("--dry-run", "--", "claude-not-here", "-p", "fix the typo")
    assert result.exit_code == 0
    assert "command not found on PATH: claude-not-here" in panel(result)


def test_a_command_the_system_cannot_execute_exits_126(world: World) -> None:
    result = run("-y", "--", "broken", "-p", "x")
    assert result.exit_code == 126
    assert "cannot run broken" in result.stderr


def test_a_file_that_is_not_executable_is_a_permission_problem_not_a_missing_command(world: World) -> None:
    """The shell says 126 for it; 127 (not found) would send the user looking for an installation that is there."""
    (world.bin / "noexec").write_text("x\n")
    result = run("-y", "--", "noexec")
    assert result.exit_code == 126 and "cannot run noexec: Permission denied" in result.stderr
    assert "not found" not in result.stderr


def test_without_a_command_it_is_a_usage_error(world: World) -> None:
    result = run("--dry-run")
    assert result.exit_code == 2 and not world.log.exists()


# ---- the model the command already selects ------------------------------------------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ("claude", "-p", "fix the typo", "--model", "claude-haiku-4-5"),
        ("claude", "--model=claude-haiku-4-5", "-p", "fix the typo"),
        ("codex", "exec", "-m", "claude-haiku-4-5", "fix the typo"),
        ("codex", "exec", "-mclaude-haiku-4-5", "fix the typo"),
        ("codex", "-c", 'model="claude-haiku-4-5"', "exec", "fix the typo"),
        ("gemini", "-m", "claude-haiku-4-5", "-p", "fix the typo"),
    ],
)
def test_a_model_flag_the_user_wrote_is_authoritative_and_no_second_one_is_added(world: World, argv: tuple[str, ...]) -> None:
    result = run("-y", "--", *argv)
    assert result.exit_code == 0, result.output
    call = world.only_call()
    assert call["argv"] == list(argv[1:])  # not one argument added
    assert call["model"] == "claude-haiku-4-5"
    text = flat(result.stderr)
    assert "model claude-haiku-4-5 (the command already selects it, the router pick below is advisory)" in text
    assert "router would pick" in text
    assert "estimate ~$0.1563 on claude-haiku-4-5" in text  # 30000 x 1.0 + 600000 x 0.10 + 25000 x 1.25 + 7000 x 5.0 = 156250


def test_a_router_that_agrees_with_the_model_in_the_command_says_so_instead_of_offering_it_again(world: World) -> None:
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo", "--model", "claude-sonnet-5-5").stdout)
    assert "model claude-sonnet-5-5 (the command already selects it; the router agrees)" in text
    assert "router would pick" not in text and "advisory" not in text
    assert "estimate ~$0.3125 on claude-sonnet-5-5" in text


def test_the_router_pick_is_shown_as_advice_next_to_the_users_own_model(world: World) -> None:
    result = run("--dry-run", "--", "claude", "-p", "refactor the auth module", "--model", "claude-haiku-4-5")
    text = flat(result.stdout)
    assert "tier medium (score 2)" in text
    assert "router would pick claude-sonnet-5-5 instead: ~$3.01 on claude-sonnet-5-5" in text


def test_a_model_the_price_table_does_not_know_still_runs_with_a_warning(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 1)  # some history, so that the only warning is the one about the price
    result = run("-y", "--", "claude", "-p", "fix the typo", "--model", "mystery")
    assert result.exit_code == 0
    assert world.only_call()["argv"] == ["-p", "fix the typo", "--model", "mystery"]
    text = flat(result.stderr)
    assert "estimate unavailable: mystery has no verified price" in text
    assert "warning no verified price for mystery: cost estimate unavailable" in text


# ---- Claude Code's own model aliases ------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("alias", "target", "estimate"),
    [("opus", "claude-opus-5-5", "0.5050"), ("sonnet", "claude-sonnet-5-5", "0.3125"), ("haiku", "claude-haiku-4-5", "0.1563")],
)
def test_a_claude_alias_is_priced_as_the_model_it_stands_for_and_passed_on_as_written(world: World, alias: str, target: str, estimate: str) -> None:
    result = run("-y", "--", "claude", "-p", "fix the typo", "--model", alias)
    assert result.exit_code == 0, result.output
    assert world.only_call()["argv"] == ["-p", "fix the typo", "--model", alias]  # Claude Code resolves the alias itself
    text = flat(result.stderr)
    assert f"estimate ~${estimate} on {alias} (alias for {target}), typical light task (no history yet)" in text
    assert f"model {alias} (the command already selects it" in text and "no verified price" not in text


def test_an_alias_the_router_would_also_pick_is_agreed_with(world: World) -> None:
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo", "--model", "sonnet").stdout)
    assert "model sonnet (the command already selects it; the router agrees)" in text
    assert "router would pick" not in text and "advisory" not in text


def test_an_alias_of_another_model_than_the_routers_is_advice_next_to_it(world: World) -> None:
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo", "--model", "opus").stdout)
    assert "router would pick claude-sonnet-5-5 instead: ~$0.3125 on claude-sonnet-5-5" in text
    assert "the router pick below is advisory" in text


def test_force_model_takes_an_alias_too(world: World) -> None:
    text = flat(run("--dry-run", "--force-model", "haiku", "--", "claude", "-p", "fix the typo").stdout)
    assert "model haiku (--force-model)" in text and "estimate ~$0.1563 on haiku (alias for claude-haiku-4-5)" in text


def test_the_aliases_are_the_ones_of_the_config(world: World, tmp_path: Path) -> None:
    config = write_config(tmp_path, router={"run": {"model_aliases": {"opus": "claude-haiku-4-5", "quick": "claude-haiku-4-5"}}})
    assert "estimate ~$0.1563 on opus (alias for claude-haiku-4-5)" in flat(run("--dry-run", "--", "claude", "-p", "x", "--model", "opus", config=config).stdout)
    assert "estimate ~$0.1563 on quick (alias for claude-haiku-4-5)" in flat(run("--dry-run", "--", "claude", "-p", "x", "--model", "quick", config=config).stdout)


def test_an_alias_whose_target_has_no_price_warns_about_the_target(world: World, tmp_path: Path, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 1)
    config = write_config(tmp_path, router={"run": {"model_aliases": {"opus": "shaky-model"}}})
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo", "--model", "opus", config=config).stdout)
    assert "estimate unavailable: shaky-model has no verified price" in text
    assert "warning no verified price for shaky-model: cost estimate unavailable" in text


# ---- what the estimate is based on ---------------------------------------------------------------------------------
def store_sessions(make_record: Callable[..., Any], count: int, **kw: Any) -> None:
    """``count`` light Claude sessions (a person drove them, nothing scores) of 1M input tokens on claude-sonnet-5-5."""
    tag = kw.pop("tag", "")  # tells two calls for the same agent apart
    with Store(Path(os.environ["AGENTFARE_HOME"]) / "agentfare.db") as store:
        for i in range(count):
            fields: dict[str, Any] = {"prompts": 1, "active_seconds": 0, "usage": {"claude-sonnet-5-5": Usage(input=1_000_000)}}
            store.upsert_session(make_record(f"past-{kw.get('agent', 'claude')}{tag}-{i}", **(fields | kw)))


def test_without_enough_history_the_estimate_says_it_is_a_typical_task(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 4)  # one short of the five that replace the default budget
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "estimate ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet)" in text
    assert "median" not in text


def test_with_history_the_estimate_names_how_many_sessions_it_is_the_median_of(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 7)
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "estimate ~$2.00 on claude-sonnet-5-5, median of 7 past claude/light sessions" in text  # 1M input tokens at $2.00/M
    assert "typical" not in text and "no usage history yet" not in text


def test_the_baseline_is_the_model_you_use_most_and_the_panel_says_so(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 6, usage={"claude-sonnet-5-5": Usage(input=1_000_000), "claude-opus-5-5": Usage(input=100_000)})
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "baseline ~$2.20 on claude-sonnet-5-5 (your most used claude model in the last 30 days, since 2026-08-31)" in text  # the median budget: 1M + 100k input tokens
    assert "saving" not in text  # the router runs the very model you use most: nothing to save (it used to say "same cost")


def test_a_pick_dearer_than_the_model_you_use_most_is_shown_as_more_than_the_baseline(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 2, usage={"claude-haiku-4-5": Usage(output=1_000_000)})
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "baseline ~$0.1563 on claude-haiku-4-5 (your most used claude model in the last 30 days, since 2026-08-31)" in text
    assert "saving none: ~$0.1563 MORE than the baseline" in text


AT = "started_at"


def test_the_baseline_is_what_you_run_now_not_what_you_ran_most_ever(world: World, make_record: Callable[..., Any]) -> None:
    """Much more was spent on opus in the summer than on sonnet in the last month: the model you use today is the baseline."""
    store_sessions(make_record, 3, usage={"claude-opus-5-5": Usage(output=5_000_000)}, **{AT: "2026-07-01T10:00:00Z"})
    store_sessions(make_record, 1, usage={"claude-haiku-4-5": Usage(output=1_000_000)}, **{AT: "2026-09-20T10:00:00Z"}, tag="-recent")
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "baseline ~$0.1563 on claude-haiku-4-5 (your most used claude model in the last 30 days, since 2026-08-21)" in text  # 30 days before Sep 20
    data = decision("--", "claude", "-p", "fix the typo")
    assert (data["baseline_model"], data["baseline_basis"], data["baseline_since"]) == ("claude-haiku-4-5", "recent", "2026-08-21")


def test_the_last_30_days_are_counted_from_the_newest_session_in_the_database_not_from_the_clock(world: World, make_record: Callable[..., Any]) -> None:
    """The clock says 2026-09-30 (AGENTFARE_NOW), the newest session is from July: the window is June 15 to July 15, whatever today is."""
    store_sessions(make_record, 1, usage={"claude-opus-5-5": Usage(output=5_000_000)}, **{AT: "2026-06-10T10:00:00Z"})  # before the window
    store_sessions(make_record, 1, usage={"claude-haiku-4-5": Usage(output=1_000_000)}, **{AT: "2026-07-15T10:00:00Z"}, tag="-newest")
    data = decision("--", "claude", "-p", "fix the typo")
    assert (data["baseline_model"], data["baseline_basis"], data["baseline_since"]) == ("claude-haiku-4-5", "recent", "2026-06-15")
    assert "since 2026-06-15" in flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)


def test_the_newest_session_of_any_agent_sets_the_window(world: World, make_record: Callable[..., Any]) -> None:
    """Claude was last used in July, codex yesterday: 'the last 30 days' is the last 30 days of your history, so no claude model is recent
    and the baseline falls back to the claude model with the most spend overall, which the panel says."""
    store_sessions(make_record, 1, usage={"claude-opus-5-5": Usage(output=1_000_000)}, **{AT: "2026-07-15T10:00:00Z"})
    store_sessions(make_record, 1, agent="codex", **{AT: "2026-09-29T10:00:00Z"})
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "baseline ~$0.5050 on claude-opus-5-5 (your most used claude model overall, none in the last 30 days)" in text
    data = decision("--", "claude", "-p", "fix the typo")
    assert (data["baseline_model"], data["baseline_basis"], data["baseline_since"]) == ("claude-opus-5-5", "all_time", None)


def test_without_priced_history_the_json_baseline_is_the_configured_one(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 2, usage={"shaky-model": Usage(output=1_000_000)})
    data = decision("--", "claude", "-p", "fix the typo")
    assert (data["baseline_model"], data["baseline_basis"], data["baseline_since"]) == ("claude-opus-5-5", "configured", None)


def test_no_saving_line_when_the_router_runs_the_baseline_even_under_an_alias(world: World, make_record: Callable[..., Any]) -> None:
    """``opus`` is an alias of the baseline ``claude-opus-5-5``, so a --force-model opus saves nothing; a different model still shows its line."""
    store_sessions(make_record, 2, usage={"claude-opus-5-5": Usage(output=100_000)})
    assert "saving" not in flat(run("--dry-run", "--force-model", "opus", "--", "claude", "-p", "fix the typo").stdout)
    assert "saving ~$" in flat(run("--dry-run", "--force-model", "claude-haiku-4-5", "--", "claude", "-p", "fix the typo").stdout)


def test_without_priced_history_the_baseline_is_the_heavy_model_of_the_agent(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 1, agent="codex")  # the history of another agent does not name this agent's baseline
    store_sessions(make_record, 2, usage={"shaky-model": Usage(output=1_000_000)})
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "baseline ~$0.5050 on claude-opus-5-5 (what you would run without the router)" in text and "most used" not in text


def test_the_history_named_is_that_of_the_agent_and_the_tier_that_were_picked(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 6, agent="codex")
    store_sessions(make_record, 6, tool_calls=180, files_touched=6, error_retries=8)  # heavy Claude sessions
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "typical light task (no history yet)" in text  # neither the other agent's sessions nor the other tier's count
    assert "median of 6 past codex/light sessions" in flat(run("--dry-run", "--", "codex", "exec", "fix the typo").stdout)


def test_an_advisory_pick_next_to_the_users_own_model_says_the_same_about_its_basis(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 5)
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo", "--model", "claude-haiku-4-5").stdout)
    assert "estimate ~$1.00 on claude-haiku-4-5, median of 5 past claude/light sessions" in text
    assert "router would pick claude-sonnet-5-5 instead: ~$2.00 on claude-sonnet-5-5, median of 5 past claude/light sessions" in text


# ---- --force-model ------------------------------------------------------------------------------------------------
def test_force_model_overrides_the_router_and_is_injected_the_same_way(world: World) -> None:
    result = run("-y", "--force-model", "claude-opus-5-5", "--", "claude", "-p", "fix the typo")
    assert result.exit_code == 0, result.output
    call = world.only_call()
    assert call["argv"] == ["--model", "claude-opus-5-5", "-p", "fix the typo"]
    assert call["model"] == "claude-opus-5-5" and call["tier"] == "light"
    text = flat(result.stderr)
    assert "model claude-opus-5-5 (--force-model)" in text
    assert "tier light (score 0)" in text  # the score is still computed and shown
    assert "estimate ~$0.5050 on claude-opus-5-5" in text
    assert "saving" not in text  # opus is the agent's baseline: the router is not saving anything


def test_force_model_on_codex_exec_goes_after_the_subcommand(world: World) -> None:
    assert run("-y", "--force-model", "gpt-6-astra", "--", "codex", "exec", "--json", "fix it").exit_code == 0
    assert world.only_call()["argv"] == ["exec", "--model", "gpt-6-astra", "--json", "fix it"]


def test_a_forced_model_is_compared_with_the_baseline_whichever_way_it_falls(world: World) -> None:
    same = flat(run("--dry-run", "--force-model", "gpt-6-astra", "--", "codex", "exec", "fix the typo").stdout)  # codex's own heavy model
    assert "estimate ~$" in same and "saving" not in same  # the baseline itself: no saving line
    # 30000 x 2.0 + 600000 x 0.20 + 25000 x 2.0 + 7000 x 12.0 = 314000 for gemini's baseline, 505000 for opus
    dearer = flat(run("--dry-run", "--force-model", "claude-opus-5-5", "--", "gemini", "-p", "fix the typo").stdout)
    assert "estimate ~$0.5050 on claude-opus-5-5" in dearer
    assert "baseline ~$0.3140 on gemini-3.1-pro-preview" in dearer
    assert "saving none: ~$0.1910 MORE than the baseline" in dearer


def test_force_model_against_a_different_model_in_the_command_is_refused(world: World) -> None:
    result = run("-y", "--force-model", "claude-opus-5-5", "--", "claude", "--model", "claude-haiku-4-5", "-p", "x")
    assert result.exit_code == 2 and not world.log.exists()
    assert "already selects model claude-haiku-4-5" in flat(result.output)
    same = run("-y", "--force-model", "claude-haiku-4-5", "--", "claude", "--model", "claude-haiku-4-5", "-p", "x")
    assert same.exit_code == 0 and world.only_call()["argv"] == ["--model", "claude-haiku-4-5", "-p", "x"]


def test_force_model_needs_a_value(world: World) -> None:
    assert run("--dry-run", "--force-model", " ", "--", "claude").exit_code == 2


@pytest.mark.parametrize("args", [("--force-model", "--yes"), ("--force-model=-x",), ("--force-model", "--dangerously-skip-permissions")])
def test_force_model_does_not_take_an_option_for_its_value(world: World, args: tuple[str, ...]) -> None:
    """`--force-model --yes` is a forgotten value: it must not become the model id put on the agent's command line."""
    result = run("-y", *args, "--", "claude", "-p", "x")
    assert result.exit_code == 2 and not world.log.exists()
    assert "--force-model" in result.output and "needs a model id" in flat(result.output)


@pytest.mark.parametrize("flag", ["model", "mcp", "", " ", "--model=", "--a b", "-", "--"])
def test_model_flag_must_be_an_option_name(world: World, flag: str) -> None:
    """A flag that is not an option would go onto the agent's command line as a word: `--model-flag mcp` would run `claude mcp`."""
    result = run("--dry-run", "--model-flag", flag, "--", "claude", "-p", "x")
    assert result.exit_code == 2 and not world.log.exists()
    assert "--model-flag" in result.output


# ---- unpriced and unverified models -------------------------------------------------------------------------------
def test_a_forced_model_without_a_verified_price_runs_with_a_visible_warning(world: World) -> None:
    for name in ("shaky-model", "mystery-model"):  # unverified in the table / not in the table at all
        world.log.unlink(missing_ok=True)
        result = run("-y", "--force-model", name, "--", "claude", "-p", "fix the typo")
        assert result.exit_code == 0, result.output
        assert world.only_call()["argv"] == ["--model", name, "-p", "fix the typo"]
        text = flat(result.stderr)
        assert f"estimate unavailable: {name} has no verified price" in text
        assert f"no verified price for {name}: cost estimate unavailable" in text
        assert "saving" not in text


def test_a_warning_is_one_row_of_its_own_and_is_never_cut_at_a_semicolon(world: World, make_record: Callable[..., Any]) -> None:
    """The router hands over a list of warnings; a model id that holds '; ' must not be read as two of them."""
    store_sessions(make_record, 1)
    out = run("--dry-run", "--force-model", "a; b", "--", "claude", "-p", "fix the typo").stdout
    rows = [line for line in out.splitlines() if "no verified price for a; b: cost estimate unavailable" in line]
    assert len(rows) == 1 and "warning" in rows[0]


def test_every_warning_gets_its_own_row(world: World) -> None:
    damage_the_database("not a database")
    out = run("--dry-run", "--force-model", "a; b", "--", "claude", "-p", "fix the typo").stdout
    rows = [line for line in out.splitlines() if "warning" in line or "no verified price for a; b" in line or "usage history unavailable" in line]
    assert len(rows) == 2
    assert "warning" in rows[0] and "usage history unavailable" in rows[0]
    assert "warning" not in rows[1] and "no verified price for a; b: cost estimate unavailable" in rows[1]  # the label is on the first row only


def test_a_long_database_path_in_a_warning_is_kept_whole(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel cut it with ``…`` ('.../scrat…'): a warning that holds a word wider than the frame goes under it, unbroken."""
    home = tmp_path / ("a-rather-long-directory-name-for-the-data-of-agentfare-" * 2)
    home.mkdir()
    monkeypatch.setenv("AGENTFARE_HOME", str(home))
    damage_the_database("not a database")
    out = run("--dry-run", "--", "claude", "-p", "fix the typo").stdout
    assert "…" not in out
    assert f"{home / 'agentfare.db'}:" in out.split()  # one word on one line, however long
    assert max(len(line) for line in out.splitlines() if str(home) not in line) <= 80  # only the path is wider than the console
    assert out.index("╯") < out.index("warning: usage history unavailable")  # the frame is intact and the warning is under it


def test_a_priced_model_with_an_unverified_cache_price_is_flagged(world: World) -> None:
    (Path(os.environ["AGENTFARE_HOME"]) / "pricing.yaml").write_text(PRICES.replace("claude-sonnet-5-5:      {provider: anthropic, input: 2.0, cached_input: 0.20,", "claude-sonnet-5-5:      {provider: anthropic, input: 2.0, cached_input: unverified,"))
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "claude-sonnet-5-5: cached-input price is unverified, cache reads are billed at the input rate" in text


def test_an_unverified_baseline_is_named_too(world: World) -> None:
    (Path(os.environ["AGENTFARE_HOME"]) / "pricing.yaml").write_text(PRICES.replace("claude-opus-5-5:        {provider: anthropic, input: 4.0,", "claude-opus-5-5:        {provider: anthropic, input: unverified,"))
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "baseline unavailable: claude-opus-5-5 has no verified price" in text
    assert "no verified price for claude-opus-5-5: baseline cost unavailable" in text
    assert "saving" not in text and "estimate ~$0.3125 on claude-sonnet-5-5" in text


# ---- custom agents and unknown commands ---------------------------------------------------------------------------
CUSTOM_AGENT = {"agents": {"custom": {"commands": ["custom"], "model_flag": "--llm"}}}
CUSTOM_MODELS = {"router": {"run": {"models": {"custom": {"light": "gpt-6.1-sol", "medium": "gpt-6.1-sol", "heavy": "gpt-6-astra"}}}}}


def test_a_custom_agent_gets_its_own_model_flag(world: World, tmp_path: Path) -> None:
    cfg = write_config(tmp_path, **CUSTOM_AGENT, **CUSTOM_MODELS)
    result = run("-y", "--prompt", "fix the typo", "--", "custom", "--foo", "bar", "task words", config=cfg)
    assert result.exit_code == 0, result.output
    call = world.only_call()
    assert call["argv"] == ["--llm", "gpt-6.1-sol", "--foo", "bar", "task words"]
    assert (call["name"], call["model"], call["tier"]) == ("custom", "gpt-6.1-sol", "light")
    text = flat(result.stderr)
    assert "agent custom" in text and "not in your agents config" not in text
    assert "prompt 12 chars, from --prompt / --prompt-file" in text


def test_a_custom_agents_prompt_is_never_guessed_from_its_arguments(world: World, tmp_path: Path) -> None:
    cfg = write_config(tmp_path, **CUSTOM_AGENT, **CUSTOM_MODELS)
    result = run("--dry-run", "--", "custom", "refactor the world", config=cfg)
    text = flat(result.stdout)
    assert "prompt not on the command line: prompt rules (length, keywords) skipped" in text
    assert "heavy keyword" not in text


def test_a_custom_agents_own_model_flag_in_the_command_is_respected(world: World, tmp_path: Path) -> None:
    cfg = write_config(tmp_path, **CUSTOM_AGENT, **CUSTOM_MODELS)
    assert run("-y", "--", "custom", "--llm=gpt-6-astra", "task", config=cfg).exit_code == 0
    assert world.only_call()["argv"] == ["--llm=gpt-6-astra", "task"]
    assert world.only_call()["model"] == "gpt-6-astra"


def test_a_users_own_model_on_an_agent_the_router_has_no_models_for_is_still_respected(world: World, tmp_path: Path) -> None:
    cfg = write_config(tmp_path, **CUSTOM_AGENT)
    result = run("-y", "--", "custom", "--llm", "gpt-6.1-sol", "task", config=cfg)
    assert result.exit_code == 0
    assert world.only_call()["argv"] == ["--llm", "gpt-6.1-sol", "task"]
    text = flat(result.stderr)
    assert "model gpt-6.1-sol (the command already selects it" in text and "estimate ~$0.3125 on gpt-6.1-sol" not in text
    assert "router would pick" not in text
    assert "no model configured for agent 'custom' (router.run.models)" in text


def test_a_custom_agent_without_a_model_table_runs_unchanged_with_a_warning(world: World, tmp_path: Path) -> None:
    cfg = write_config(tmp_path, **CUSTOM_AGENT)
    result = run("-y", "--", "custom", "task", config=cfg)
    assert result.exit_code == 0
    call = world.only_call()
    assert call["argv"] == ["task"] and call["model"] is None and call["tier"] == "light"
    text = flat(result.stderr)
    assert "no model configured for agent 'custom' (router.run.models): run it with its own default" in text
    assert "model none: the agent runs with its own default model" in text


def test_a_custom_agent_that_names_no_model_flag_cannot_be_given_a_model(world: World, tmp_path: Path) -> None:
    cfg = write_config(tmp_path, agents={"custom": {"commands": ["custom"], "model_flag": None}}, **CUSTOM_MODELS)
    result = run("-y", "--", "custom", "task", config=cfg)
    assert result.exit_code == 0
    assert world.only_call()["argv"] == ["task"] and world.only_call()["model"] is None
    assert "no model flag is known for custom, so the command runs unchanged" in flat(result.stderr)


def test_a_model_that_cannot_be_passed_on_is_shown_neither_as_the_choice_nor_as_a_saving(world: World, tmp_path: Path) -> None:
    """With no model flag the agent runs on its own default: an estimate or a saving for the router's model would be false."""
    cfg = write_config(tmp_path, agents={"custom": {"commands": ["custom"], "model_flag": None}}, **CUSTOM_MODELS)
    text = flat(run("--dry-run", "--", "custom", "task", config=cfg).stdout)
    assert "model none: the agent runs with its own default model (the router pick gpt-6.1-sol cannot be passed on)" in text
    assert "no model flag is known for custom" in text
    forced = flat(run("--dry-run", "--force-model", "gpt-6.1-sol", "--", "custom", "task").stdout)
    assert "model none: the agent runs with its own default model (--force-model gpt-6.1-sol cannot be passed on)" in forced
    for shown in (text, forced):
        assert not {"estimate", "baseline", "saving"} & set(shown.split())


def test_an_unknown_command_runs_unchanged_after_the_decision_is_shown(world: World) -> None:
    result = run("-y", "--", "custom", "--foo", "refactor everything")
    assert result.exit_code == 0, result.output
    call = world.only_call()
    assert call["argv"] == ["--foo", "refactor everything"] and call["model"] is None
    assert call["tier"] == "light"  # the tier is still computed and exported
    text = flat(result.stderr)
    assert "agent custom (not in your agents config: no model mapping)" in text
    assert "no model configured for agent 'custom' (router.run.models)" in text
    assert "prompt:" not in text and "refactor everything" not in text  # nothing guessed about which argument is a prompt, and text is never shown
    assert "command custom --foo '<arg: 19 chars>'" in text


def test_an_unknown_command_can_be_given_a_model_with_force_model_and_model_flag(world: World) -> None:
    result = run("-y", "--force-model", "gpt-6.1-sol", "--model-flag", "-M", "--", "custom", "task")
    assert result.exit_code == 0, result.output
    call = world.only_call()
    assert call["argv"] == ["-M", "gpt-6.1-sol", "task"] and call["model"] == "gpt-6.1-sol"
    assert "estimate ~$0.3125" not in flat(result.stderr)  # gpt-6.1-sol prices differently from sonnet
    assert "model gpt-6.1-sol (--force-model)" in flat(result.stderr)


def test_force_model_without_any_way_to_pass_it_says_so_and_runs_unchanged(world: World) -> None:
    result = run("-y", "--force-model", "gpt-6.1-sol", "--", "custom", "task")
    assert result.exit_code == 0
    assert world.only_call()["argv"] == ["task"] and world.only_call()["model"] is None
    assert "no model flag is known for custom" in flat(result.stderr) and "--model-flag" in flat(result.stderr)


def test_model_flag_overrides_the_configured_one(world: World) -> None:
    assert run("-y", "--model-flag", "--brain", "--", "claude", "-p", "fix the typo").exit_code == 0
    assert world.only_call()["argv"] == ["--brain", "claude-sonnet-5-5", "-p", "fix the typo"]


def test_a_stale_model_from_an_outer_run_never_reaches_a_command_that_gets_none(world: World) -> None:
    result = run("-y", "--", "custom", "task", env={"AGENTFARE_MODEL": "outer-model", "AGENTFARE_TIER": "heavy"})
    assert result.exit_code == 0
    call = world.only_call()
    assert call["model"] is None and call["tier"] == "light"


def test_a_configured_command_alias_reaches_its_agent(world: World, tmp_path: Path) -> None:
    cfg = write_config(tmp_path, agents={"claude": {"commands": ["claude", "custom"]}})
    assert run("-y", "--", "custom", "-p", "fix the typo", config=cfg).exit_code == 0
    assert world.only_call()["argv"] == ["--model", "claude-sonnet-5-5", "-p", "fix the typo"]


def test_a_broken_agents_table_is_reported_and_nothing_runs(world: World, tmp_path: Path) -> None:
    cfg = write_config(tmp_path, agents={"claude": 5})
    result = run("-y", "--", "claude", "-p", "x", config=cfg)
    assert result.exit_code == 2 and not world.log.exists()
    assert "agents.claude must be a mapping" in result.stderr


def test_a_working_directory_that_no_longer_exists_is_a_clean_error_and_nothing_runs(world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    gone.rmdir()
    result = run("-y", "--", "claude", "-p", "x")
    assert result.exit_code == 2 and "current directory" in result.stderr and not world.log.exists()


def damage_the_database(how: str) -> None:
    db = Path(os.environ["AGENTFARE_HOME"]) / "agentfare.db"
    if how == "not a database":
        db.write_text("this is not sqlite\n")
        return
    Store(db).close()
    data = bytearray(db.read_bytes())
    data[4096:] = b"\xab" * (len(data) - 4096)  # the header and the schema page survive, so the file still opens
    db.write_bytes(bytes(data))


@pytest.mark.parametrize("how", ["not a database", "damaged pages"])
def test_a_history_that_cannot_be_read_costs_the_estimate_its_precision_not_the_run(world: World, how: str) -> None:
    """The database is only a cache of the logs. A wrapper in front of the agent must not stop it, or show a traceback, over it."""
    damage_the_database(how)
    result = run("-y", "--", "claude", "-p", "fix the typo")
    assert result.exit_code == 0, result.output
    assert world.only_call()["argv"] == ["--model", "claude-sonnet-5-5", "-p", "fix the typo"]
    text = flat(result.stderr)
    assert "usage history unavailable, so estimates use the default token budgets" in text
    assert "estimate ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet)" in text  # the default budgets, as with no history at all
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("how", ["not a database", "damaged pages"])
def test_a_database_that_breaks_while_the_logs_are_read_into_it_is_a_lost_history_too(world: World, tmp_path: Path, how: str) -> None:
    """Not ``--no-ingest`` this time: the ingest before the decision hits the damage, which is one error for the whole
    database and not a 'could not parse' for every log file."""
    damage_the_database(how)
    argv = ["--config", str(write_config(tmp_path)), "run", "--dry-run", "--", "claude", "-p", "fix the typo"]
    result = CliRunner().invoke(app, argv, env={"COLUMNS": "80"})
    assert result.exit_code == 0, result.output
    text = flat(result.stdout)
    assert "usage history unavailable, so estimates use the default token budgets" in text
    assert "estimate ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet)" in text
    assert "could not parse" not in result.stderr and "Traceback" not in result.output


def test_a_data_directory_with_odd_characters_is_opened_as_it_is(world: World, make_record: Callable[..., Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The database is opened through a URI: a space, a ``#``, a ``?`` or a ``%`` in the path must mean themselves."""
    odd = tmp_path / "data #1 ?x%20y"
    odd.mkdir()
    (odd / "pricing.yaml").write_text(PRICES)
    monkeypatch.setenv("AGENTFARE_HOME", str(odd))
    store_sessions(make_record, 6)
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "median of 6 past claude/light sessions" in text and "warning" not in text


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_database_that_cannot_be_written_is_still_read(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 6)
    home = database_file().parent
    database_file().chmod(0o444)
    home.chmod(0o555)
    try:
        result = run("--dry-run", "--", "claude", "-p", "fix the typo")
    finally:
        home.chmod(0o755)
        database_file().chmod(0o644)
    assert result.exit_code == 0, result.output
    text = flat(result.stdout)
    assert "estimate ~$2.00 on claude-sonnet-5-5, median of 6 past claude/light sessions" in text and "warning" not in text


def test_a_database_of_an_older_version_is_not_upgraded_by_a_run_and_does_not_stop_it(world: World) -> None:
    """Only ``ingest`` and the reporting commands upgrade the file; the run falls back to the default budgets and says so."""
    with sqlite3.connect(database_file()) as db:
        db.executescript(
            "CREATE TABLE sessions (key TEXT PRIMARY KEY, agent TEXT, session_id TEXT, project TEXT, started_at TEXT, ended_at TEXT,"
            " wall_seconds INTEGER, active_seconds INTEGER, tool_calls INTEGER, files_touched INTEGER, error_retries INTEGER,"
            " turns INTEGER, prompts INTEGER, primary_model TEXT, source_path TEXT, ingested_at TEXT);"
            "INSERT INTO sessions VALUES ('claude:a', 'claude', 'a', 'p', '2026-09-01T10:00:00Z', '2026-09-01T11:00:00Z', 1, 1, 1, 1, 1, 1, 1, 'm', '/x', 'now');"
            "CREATE TABLE session_usage (key TEXT, model TEXT, input INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER, cache_write_1h INTEGER);"
            "PRAGMA user_version = 2;"
        )
    before = database_file().read_bytes()
    result = run("--dry-run", "--", "claude", "-p", "fix the typo")
    assert result.exit_code == 0, result.output
    text = flat(result.stdout)
    assert "usage history unavailable, so estimates use the default token budgets" in text
    assert "estimate ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet)" in text
    assert database_file().read_bytes() == before


# ---- --dry-run --json --------------------------------------------------------------------------------------------
def decision(*args: str, **kw: Any) -> dict[str, Any]:
    result = run("--dry-run", "--json", *args, **kw)
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    return json.loads(result.stdout)  # type: ignore[no-any-return]


DECISION_KEYS = [
    "agent", "tier", "score", "reasons", "model", "forced", "estimate", "baseline", "baseline_model", "baseline_basis", "baseline_since",
    "history_sessions", "warnings", "argv",
]


def test_the_decision_as_json(world: World) -> None:
    data = decision("--", "claude", "-p", "fix a typo in the README")
    assert list(data) == DECISION_KEYS
    assert data == {
        "agent": "claude", "tier": "light", "score": 0,
        "reasons": ["light keywords typo, readme: -3", "medium keyword fix: +1"],
        "model": "claude-sonnet-5-5", "forced": False,
        "estimate": {"cents": 31, "ucents": 31_250_000}, "baseline": {"cents": 51, "ucents": 50_500_000},
        "baseline_model": "claude-opus-5-5", "baseline_basis": "configured", "baseline_since": None, "history_sessions": 0,
        "warnings": ["no usage history yet (run agentfare ingest to personalize estimates)"],
        "argv": ["claude", "--model", "claude-sonnet-5-5", "-p", "<prompt: 24 chars>"],
    }
    assert not world.log.exists()  # nothing ran


def test_the_json_decision_follows_the_history_the_routes_and_the_model_the_command_selects(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 6)
    data = decision("--", "claude", "-p", "fix the typo", "--model", "opus")
    assert (data["tier"], data["model"], data["forced"], data["history_sessions"], data["warnings"]) == ("light", "opus", False, 6, [])
    assert data["estimate"] == {"cents": 400, "ucents": 400_000_000}  # the median budget (6 sessions of 1M input tokens) on claude-opus-5-5 at $4/M
    assert (data["baseline"], data["baseline_model"]) == ({"cents": 200, "ucents": 200_000_000}, "claude-sonnet-5-5")  # the model you use most
    assert (data["baseline_basis"], data["baseline_since"]) == ("recent", "2026-08-31")
    assert data["argv"] == ["claude", "-p", "<prompt: 12 chars>", "--model", "opus"]


def test_the_json_decision_of_a_forced_model_and_of_one_that_cannot_be_priced(world: World) -> None:
    data = decision("--force-model", "gpt-6.1-sol", "--", "claude", "-p", "fix the typo")
    assert (data["forced"], data["model"], data["estimate"]["ucents"]) == (True, "gpt-6.1-sol", 25_250_000)
    data = decision("--force-model", "mystery", "--", "claude", "-p", "fix the typo")
    assert (data["model"], data["estimate"]) == ("mystery", None) and data["baseline"]["ucents"] == 50_500_000
    assert "no verified price for mystery: cost estimate unavailable" in data["warnings"]


def test_the_json_decision_has_no_estimate_for_a_model_that_cannot_be_passed_on(world: World, tmp_path: Path) -> None:
    config = write_config(tmp_path, agents={"custom": {"commands": ["custom"], "model_flag": None}}, **CUSTOM_MODELS)
    data = decision("--", "custom", "fix the typo", config=config)
    assert (data["model"], data["estimate"], data["baseline"], data["baseline_model"]) == (None, None, None, "gpt-6-astra")
    assert any("no model flag is known for custom" in w for w in data["warnings"])
    assert data["argv"] == ["custom", "<arg: 12 chars>"]


def test_the_json_decision_of_a_command_that_talks_to_no_model(world: World) -> None:
    data = decision("--", "claude", "mcp", "list")
    assert list(data) == DECISION_KEYS
    assert (data["tier"], data["score"], data["reasons"], data["model"], data["estimate"], data["baseline"], data["baseline_model"]) == (None, None, [], None, None, None, None)
    assert data["argv"] == ["claude", "mcp", "<arg: 4 chars>"]  # mcp is a subcommand it knows; what follows it is not shown


def test_the_json_decision_never_holds_prompt_text(world: World, tmp_path: Path) -> None:
    secret = "PRIVACY-CANARY-RUN-JSON-7e2a"
    prompt, note = f"refactor {secret}", f"note={secret} with spaces"
    data = decision("--", "claude", "-p", prompt)
    assert secret not in json.dumps(data) and data["argv"][-1] == f"<prompt: {len(prompt)} chars>"
    data = decision("--", "codex", "exec", f"fix {secret}", "--config", note)
    assert secret not in json.dumps(data) and data["argv"][-2:] == ["--config", f"<arg: {len(note)} chars>"]
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text(f"migrate {secret}")
    assert secret not in json.dumps(decision("--prompt-file", str(prompt_file), "--", "claude"))


def test_json_needs_dry_run(world: World) -> None:
    result = run("--json", "-y", "--", "claude", "-p", "fix the typo")
    assert result.exit_code == 2 and not world.log.exists()
    assert "--json" in result.output and "--dry-run" in result.output


def test_the_json_decision_is_deterministic_and_on_stdout_only(world: World) -> None:
    first, second = (run("--dry-run", "--json", "--", "claude", "-p", "fix the typo") for _ in range(2))
    assert first.stdout == second.stdout and first.stderr == second.stderr == ""
    assert "\x1b" not in first.stdout


# ---- commands that are not model sessions ---------------------------------------------------------------------------
def test_a_subcommand_that_never_talks_to_a_model_is_run_as_it_is(world: World) -> None:
    result = run("-y", "--", "claude", "mcp", "list")
    assert result.exit_code == 0, result.output
    call = world.only_call()
    assert call["argv"] == ["mcp", "list"] and call["model"] is None and call["tier"] is None
    text = flat(result.stderr)
    assert "'claude mcp' does not talk to a model: nothing to route" in text
    assert "tier" not in text and "estimate" not in text


def test_force_model_on_a_command_that_is_not_a_model_session_is_ignored_loudly(world: World) -> None:
    result = run("-y", "--force-model", "claude-opus-5-5", "--", "claude", "mcp", "list")
    assert result.exit_code == 0 and world.only_call()["argv"] == ["mcp", "list"]
    assert "--force-model ignored: 'claude mcp' does not talk to a model" in flat(result.stderr)


def test_codex_review_takes_no_model_option_so_none_is_injected(world: World) -> None:
    assert run("-y", "--", "codex", "review", "--uncommitted").exit_code == 0
    assert world.only_call()["argv"] == ["review", "--uncommitted"]


# ---- the prompt, and what the heuristics make of it -----------------------------------------------------------------
def tier_of(result: Result) -> str:
    return re.search(r"tier (\w+) \(score", flat(result.stdout + result.stderr)).group(1)  # type: ignore[union-attr]


TUNED = {"router": {"run": {"tiers": {"light_max": 0, "medium_max": 1}}}}  # one keyword (2 points) is enough for heavy
AGENT_LINES: dict[str, Callable[[str], list[str]]] = {
    "claude": lambda p: ["claude", "-p", p],
    "codex": lambda p: ["codex", "exec", p],
    "gemini": lambda p: ["gemini", "-p", p],
}
LIGHT_HEAVY = {"claude": ("claude-sonnet-5-5", "claude-opus-5-5"), "codex": ("gpt-6.1-sol", "gpt-6-astra"), "gemini": ("gemini-3.1-flash-lite", "gemini-3.1-pro-preview")}


@pytest.mark.parametrize("agent", ["claude", "codex", "gemini"])
@pytest.mark.parametrize(("word", "keyword"), [("refactor", "refactor"), ("migrate", "migrat"), ("architect", "architect")])
def test_a_heavy_keyword_end_to_end_picks_the_heavy_model(world: World, tmp_path: Path, agent: str, word: str, keyword: str) -> None:
    result = run("-y", "--", *AGENT_LINES[agent](f"please {word} the auth module"), config=write_config(tmp_path, **TUNED))
    assert result.exit_code == 0, result.output
    call = world.only_call()
    assert (call["tier"], call["model"]) == ("heavy", LIGHT_HEAVY[agent][1])
    assert f"heavy keyword {keyword}: +2" in flat(result.stderr)


@pytest.mark.parametrize("agent", ["claude", "codex", "gemini"])
def test_a_light_keyword_end_to_end_picks_the_light_model(world: World, tmp_path: Path, agent: str) -> None:
    result = run("-y", "--", *AGENT_LINES[agent]("fix a typo in the docs"), config=write_config(tmp_path, **TUNED))
    call = world.only_call()
    assert (call["tier"], call["model"]) == ("light", LIGHT_HEAVY[agent][0])
    assert "light keyword typo: -2" in flat(result.stderr)


def test_with_the_default_thresholds_one_keyword_alone_is_medium(world: World) -> None:
    result = run("--dry-run", "--", "claude", "-p", "refactor the auth module")
    assert tier_of(result) == "medium"
    assert "heavy keyword refactor: +2" in flat(result.stdout)


def test_keyword_prompt_length_and_repo_size_add_up_to_heavy(world: World) -> None:
    for i in range(200):
        (world.repo / f"file{i}.py").write_text("x")
    prompt = "migrate the billing module. " + "Keep the public API stable. " * 15  # 28 + 420 = 448 chars
    result = run("-y", "--", "claude", "-p", prompt)
    assert result.exit_code == 0, result.output
    text = flat(result.stderr)
    assert "prompt is 448 chars (>= 400): +1" in text
    assert "repo has 203 files (>= 200): +1" in text
    assert "heavy keyword migrat: +2" in text
    assert "medium keyword api: +1" in text  # "the public API"
    assert "tier heavy (score 5)" in text
    assert "saving" not in text  # the heavy pick is the baseline
    assert world.only_call()["argv"][:2] == ["--model", "claude-opus-5-5"]


def test_the_repo_that_counts_is_the_current_directory_and_dependency_folders_do_not(world: World) -> None:
    (world.repo / "node_modules").mkdir()
    for i in range(300):
        (world.repo / "node_modules" / f"dep{i}.js").write_text("x")
    assert "repo has" not in flat(run("--dry-run", "--", "claude", "-p", "x").stdout)


def test_a_prompt_typed_later_is_scored_from_prompt_or_prompt_file_and_the_skip_is_announced(world: World, tmp_path: Path) -> None:
    text = flat(run("--dry-run", "--", "claude").stdout)
    assert "prompt not on the command line: prompt rules (length, keywords) skipped; --prompt or --prompt-file adds them" in text
    assert "no rule fired: score 0" in text
    text = flat(run("--dry-run", "--prompt", "refactor the auth module", "--", "claude").stdout)
    assert "prompt 24 chars, from --prompt / --prompt-file" in text and "heavy keyword refactor: +2" in text
    file = tmp_path / "task.txt"
    file.write_text("please refactor the whole thing\n")
    text = flat(run("--dry-run", "--prompt-file", str(file), "--", "claude").stdout)
    assert "prompt 32 chars, from --prompt / --prompt-file" in text and "heavy keyword refactor: +2" in text


def test_prompt_option_beats_the_prompt_in_the_command(world: World) -> None:
    text = flat(run("--dry-run", "--prompt", "refactor it", "--", "claude", "-p", "fix a typo").stdout)
    assert "heavy keyword refactor: +2" in text and "light keyword" not in text


def test_prompt_and_prompt_file_together_are_refused(world: World, tmp_path: Path) -> None:
    file = tmp_path / "p.txt"
    file.write_text("x")
    assert run("--dry-run", "--prompt", "a", "--prompt-file", str(file), "--", "claude").exit_code == 2


def test_an_unreadable_prompt_file_is_a_clear_error_and_nothing_runs(world: World, tmp_path: Path) -> None:
    result = run("-y", "--prompt-file", str(tmp_path / "missing.txt"), "--", "claude", "-p", "x")
    assert result.exit_code == 2 and not world.log.exists()
    assert "cannot read" in result.output.replace("\n", " ")


# ---- privacy -------------------------------------------------------------------------------------------------------
def test_prompt_text_is_never_printed(world: World, tmp_path: Path) -> None:
    secret = f"{MARKER} refactor the vault"
    long_secret = f"{secret} and then some more words so that it reads as free text of a fair length"
    file = tmp_path / "secret.txt"
    file.write_text(secret)
    cases = [
        ("--dry-run", "--", "claude", "-p", secret),
        ("-y", "--", "claude", "-p", secret),
        ("--dry-run", "--", "codex", "exec", secret),
        ("--dry-run", "--", "gemini", "--prompt=" + secret),
        ("--dry-run", "--", "gemini", *secret.split()),
        ("--dry-run", "--prompt", secret, "--", "claude"),
        ("--dry-run", "--prompt-file", str(file), "--", "claude"),
        ("--dry-run", "--", "custom", "--anything", long_secret),  # a prompt the wrapper cannot identify: long free text is still hidden
        ("--dry-run", "--", "claude", "--allowedTools", "Edit", long_secret),  # swallowed by the variadic option, so not identified either
    ]
    for args in cases:
        result = run(*args)
        assert result.exit_code == 0, (args, result.output)
        assert MARKER not in result.stdout + result.stderr and "vault" not in result.stdout + result.stderr, args


def test_every_argument_but_the_program_the_known_subcommands_the_model_flag_and_flag_names_is_shown_as_its_length() -> None:
    """Changed on purpose (it showed every argument without whitespace as it was): a flag can hold a secret (--api-key sk-...)."""
    inv = read(None, "custom", "--flag", "two words", "x" * 100, "a b " * 20, "--path", "/some dir/x", "line\nbreak")
    assert inv.display() == "custom --flag '<arg: 9 chars>' '<arg: 100 chars>' '<arg: 80 chars>' --path '<arg: 11 chars>' '<arg: 10 chars>'"


@pytest.mark.parametrize(
    "argv",
    [
        ("codex", "exec", "resume", "--last", f"{MARKER} vault plan"),  # session id or prompt: not guessed, so not shown
        ("codex", "resume", "abc", f"{MARKER} vault plan"),
        ("codex", "review", f"{MARKER} vault plan"),
        ("claude", "--append-system-prompt", f"{MARKER} vault plan", "-p", "x"),  # a system prompt is prompt text too
        ("claude", "--system-prompt", f"{MARKER} vault plan", "-p", "x"),
        ("custom", "--anything", f"{MARKER} vault plan"),
        ("custom", f"--flag={MARKER} vault plan"),
    ],
)
def test_no_argument_that_reads_as_text_is_ever_printed_however_short(world: World, argv: tuple[str, ...]) -> None:
    result = run("--dry-run", "--", *argv)
    assert result.exit_code == 0, result.output
    assert MARKER not in result.stdout + result.stderr and "vault" not in result.stdout + result.stderr


def test_running_writes_nothing_to_the_database(world: World) -> None:
    assert run("-y", "--", "claude", "-p", "fix the typo").exit_code == 0
    result = CliRunner().invoke(app, ["--config", str(FIXTURE_CONFIG), "--no-ingest", "sessions", "--json"])
    assert json.loads(result.stdout)["sessions"] == []


def database_file() -> Path:
    return Path(os.environ["AGENTFARE_HOME"]) / "agentfare.db"


@pytest.mark.parametrize("args", [("--dry-run",), ("-y",)])
def test_run_never_reads_the_logs_and_never_creates_the_database(world: World, tmp_path: Path, args: tuple[str, ...]) -> None:
    """The decision must appear at once, even on a first run: no scan of the logs, no database file."""
    argv = ["--config", str(write_config(tmp_path)), "run", *args, "--", "claude", "-p", "fix the typo"]  # auto-ingest is on, the logs are there
    result = CliRunner().invoke(app, argv, env={"COLUMNS": "80"})
    assert result.exit_code == 0, result.output
    assert not database_file().exists()
    assert "first run" not in result.stderr and "ingested" not in result.stderr and "reading" not in result.stderr
    text = flat(result.stdout + result.stderr)
    assert "model claude-sonnet-5-5 (router pick)" in text
    assert "warning no usage history yet (run agentfare ingest to personalize estimates)" in text
    assert "estimate ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet)" in text  # the default token budget


def test_run_reads_the_history_that_ingest_left_but_does_not_update_it(world: World, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    assert CliRunner().invoke(app, ["--config", str(config), "ingest"]).exit_code == 0
    before = database_file().read_bytes()
    result = CliRunner().invoke(app, ["--config", str(config), "run", "--dry-run", "--", "claude", "-p", "fix the typo"], env={"COLUMNS": "80"})
    assert result.exit_code == 0 and result.stderr == ""
    assert "no usage history yet" not in flat(result.stdout)
    assert "your most used claude model" in flat(result.stdout)  # the fixtures' Claude session: its most expensive model
    assert database_file().read_bytes() == before


def test_a_database_with_no_sessions_says_there_is_no_history_yet(world: World) -> None:
    Store(database_file()).close()
    text = flat(run("--dry-run", "--", "claude", "-p", "fix the typo").stdout)
    assert "warning no usage history yet (run agentfare ingest to personalize estimates)" in text


def test_a_locked_database_falls_back_with_a_warning(world: World, make_record: Callable[..., Any]) -> None:
    store_sessions(make_record, 6)
    locker = sqlite3.connect(database_file(), timeout=0)
    locker.execute("BEGIN EXCLUSIVE")
    try:
        result = run("--dry-run", "--", "claude", "-p", "fix the typo")
    finally:
        locker.close()
    assert result.exit_code == 0, result.output
    text = flat(result.stdout)
    assert "usage history unavailable, so estimates use the default token budgets" in text and "locked" in text
    assert "estimate ~$0.3125 on claude-sonnet-5-5, typical light task (no history yet)" in text


# ---- signals ---------------------------------------------------------------------------------------------------------
SLEEPY = """\
#!{python}
import os, signal, sys, time

def say(text):  # not print(): the test signals us the moment READY appears, while print may still be finishing, and it is not re-entrant
    os.write(1, text.encode())

def handler(signum, frame):
    say("GOT " + signal.Signals(signum).name + "\\n")
    sys.exit(40 + signum)  # not 128 + signum, so a wrapper that dies on the signal itself cannot pass for one that waits

for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
    signal.signal(s, handler)
say("READY\\n")
time.sleep(30)
"""

PROBE = """\
#!{python}
import signal

print(sorted(s.name for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGHUP, signal.SIGTERM) if signal.getsignal(s) == signal.SIG_IGN))
"""

INTERRUPTED = """\
#!{python}
import os, signal

signal.signal(signal.SIGINT, signal.SIG_DFL)
os.kill(os.getpid(), signal.SIGINT)  # what an agent does when Ctrl-C stops it
"""


def install(world: World, name: str, script: str) -> None:
    path = world.bin / name
    path.write_text(script.format(python=sys.executable))
    path.chmod(0o755)


def wrapper_command(agent: str, *, terminal: bool = False, keep_ignored: bool = False) -> list[str]:
    """``agentfare run -y -- <agent>`` as its own process. A process started with SIGINT ignored (a job in a background shell,
    a CI runner) keeps ignoring it, which would hide a wrapper that fails to, so the entry point restores Python's usual
    Ctrl-C handling first. ``terminal=True`` says a terminal leads the process group (as ``_terminal_leads`` would) without
    needing one, so a plain ``killpg`` can stand in for the keys. ``keep_ignored=True`` leaves SIGINT as it was inherited."""
    lead = "from agentfare.commands import run; run._terminal_leads = lambda: True; " if terminal else ""
    restore = "" if keep_ignored else "import signal; signal.signal(signal.SIGINT, signal.default_int_handler); "
    entry = f"{restore}{lead}from agentfare.cli import _entry; _entry()"
    return [sys.executable, "-c", entry, "--config", str(FIXTURE_CONFIG), "--no-ingest", "run", "-y", "--", agent]


def start_sleepy(world: World, *, terminal: bool = False) -> subprocess.Popen[str]:
    install(world, "sleepy", SLEEPY)
    proc = subprocess.Popen(
        wrapper_command("sleepy", terminal=terminal),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, env={**os.environ, "COLUMNS": "80"},
    )
    assert proc.stdout and proc.stdout.readline().strip() == "READY"
    return proc


def finish(proc: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        return proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        pytest.fail("the wrapper and its agent were still running ten seconds after the signal")


def test_ctrl_c_reaches_the_agent_once_and_the_wrapper_waits_for_it(world: World) -> None:
    """A terminal signals the whole foreground group: the wrapper must neither die on it nor pass a second copy on."""
    proc = start_sleepy(world, terminal=True)
    os.killpg(proc.pid, signal.SIGINT)
    out, err = finish(proc)
    assert out.splitlines() == ["GOT SIGINT"]  # once: a second Ctrl-C would be a second line, or a dead child
    assert proc.returncode == 42  # the agent's own exit code, not an abort of the wrapper
    assert "Traceback" not in err and "KeyboardInterrupt" not in err


def test_ctrl_backslash_reaches_the_agent_once_and_the_wrapper_waits_for_it(world: World) -> None:
    """SIGQUIT kills a Python process on the spot: a wrapper that did not ignore it would vanish and leave the agent behind."""
    proc = start_sleepy(world, terminal=True)
    os.killpg(proc.pid, signal.SIGQUIT)
    out, _ = finish(proc)
    assert out.splitlines() == ["GOT SIGQUIT"]
    assert proc.returncode == 43


def test_an_interrupt_sent_to_the_wrapper_alone_is_passed_on_to_the_agent(world: World) -> None:
    """Without a terminal nobody else delivers it: a supervisor that signals only the pid it started must still stop the agent."""
    proc = start_sleepy(world)
    proc.send_signal(signal.SIGINT)
    out, err = finish(proc)
    assert out.splitlines() == ["GOT SIGINT"]
    assert proc.returncode == 42
    assert "Traceback" not in err


def test_a_terminate_sent_to_the_wrapper_alone_is_passed_on_to_the_agent(world: World) -> None:
    proc = start_sleepy(world)
    proc.send_signal(signal.SIGTERM)
    out, err = finish(proc)
    assert out.splitlines() == ["GOT SIGTERM"]
    assert proc.returncode == 55


def test_a_signal_that_was_ignored_on_entry_stays_ignored_for_the_agent(world: World) -> None:
    """`nohup agentfare run ...` and a background job in a script start us with SIGHUP, or SIGINT and SIGQUIT, ignored. A
    handler here would reset them to the default in the agent, so closing the terminal would kill what nohup protects."""
    install(world, "probe", PROBE)
    done = subprocess.run(
        ["sh", "-c", 'trap "" INT QUIT HUP; exec "$@"', "sh", *wrapper_command("probe", keep_ignored=True)],
        capture_output=True, text=True, timeout=60, env={**os.environ, "COLUMNS": "80"},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "['SIGHUP', 'SIGINT', 'SIGQUIT']"  # SIGTERM was not ignored on entry, and is not now


def read_terminal(master: int, until: bytes | None) -> bytes:
    """What a pseudo-terminal shows until ``until`` has appeared, or (``None``) until every process on it has gone."""
    seen, end = b"", time.monotonic() + 15
    while until is None or until not in seen:
        if not select.select([master], [], [], max(0.0, end - time.monotonic()))[0]:
            pytest.fail(f"the terminal showed {seen!r} and then nothing for 15 seconds")
        try:
            chunk = os.read(master, 4096)
        except OSError:  # Linux: EIO once nothing holds the other side any more
            break
        if not chunk:
            break
        seen += chunk
    return seen


@pytest.mark.parametrize(("key", "name", "code"), [(b"\x03", "SIGINT", 42), (b"\x1c", "SIGQUIT", 43)], ids=["Ctrl-C", "Ctrl-backslash"])
def test_the_keys_of_a_real_terminal_reach_the_agent_once_and_the_wrapper_waits(world: World, key: bytes, name: str, code: int) -> None:
    """The wrapper started the way a shell starts a foreground job: its own session, a pseudo-terminal as controlling terminal."""
    try:
        master, slave = pty.openpty()
    except OSError:
        pytest.skip("no pseudo-terminals here")
    install(world, "sleepy", SLEEPY)
    launcher = "import fcntl, os, sys, termios; os.setsid(); fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execv(sys.executable, [sys.executable, '-c', sys.argv[1], *sys.argv[2:]])"
    proc = subprocess.Popen([sys.executable, "-c", launcher, *wrapper_command("sleepy")[2:]], stdin=slave, stdout=slave, stderr=slave, env={**os.environ, "COLUMNS": "80"})
    os.close(slave)
    try:
        read_terminal(master, b"READY")
        os.write(master, key)
        seen = read_terminal(master, None).decode(errors="replace")
        status = proc.wait(timeout=15)
    finally:
        os.close(master)
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # nothing should be left; if something is, it must not outlive the test
        except ProcessLookupError:
            pass
    assert seen.count("GOT ") == 1 and f"GOT {name}" in seen
    assert status == code


def test_an_agent_stopped_by_ctrl_c_makes_the_wrapper_end_the_same_way(world: World) -> None:
    """A calling script stops on a child that died of SIGINT, but reads a plain exit status of 130 as an ordinary failure and goes on."""
    install(world, "interrupted", INTERRUPTED)
    done = subprocess.run(wrapper_command("interrupted"), capture_output=True, text=True, timeout=60, start_new_session=True, env={**os.environ, "COLUMNS": "80"})
    assert done.returncode == -signal.SIGINT, done.stderr


# ---- staying local ---------------------------------------------------------------------------------------------------
def test_the_wrapper_needs_no_network(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network used")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    assert run("-y", "--", "claude", "-p", "fix the typo").exit_code == 0
    assert run("--dry-run", "--", "codex", "exec", "x").exit_code == 0


# ---- a secret in a flag is never shown --------------------------------------------------------------------------------
SECRET = "sk-PRIVACY-CANARY-FLAG-4d2e"
HIDDEN = f"<arg: {len(SECRET)} chars>"


@pytest.mark.parametrize(
    ("agent", "argv", "expected"),
    [
        ("claude", ("claude", "--api-key", SECRET, "-p", "fix it"), ["claude", "--api-key", HIDDEN, "-p", "<prompt: 6 chars>"]),
        ("claude", ("claude", "--api-key", SECRET, "--", "fix it"), ["claude", "--api-key", HIDDEN, "--", "<prompt: 6 chars>"]),
        ("claude", ("claude", f"--api-key={SECRET}", "-p", "fix it"), ["claude", f"--api-key={HIDDEN}", "-p", "<prompt: 6 chars>"]),
        ("claude", ("claude", f"-k{SECRET}", "-p", "fix it"), ["claude", f"-k<arg: {len(SECRET)} chars>", "-p", "<prompt: 6 chars>"]),
        ("claude", ("claude", "--header", f"Authorization: Bearer {SECRET}", "-p", "fix it"), ["claude", "--header", f"<arg: {len(SECRET) + 22} chars>", "-p", "<prompt: 6 chars>"]),
        ("claude", ("claude", "--system-prompt", SECRET, "--model", "opus", "-p", "fix it"), ["claude", "--system-prompt", HIDDEN, "--model", "opus", "-p", "<prompt: 6 chars>"]),
        ("claude", ("claude", "--model=opus", "-p", "fix it"), ["claude", "--model=opus", "-p", "<prompt: 6 chars>"]),
        ("claude", ("claude", "mcp", "add", "--env", f"KEY={SECRET}", "server"), ["claude", "mcp", "<arg: 3 chars>", "--env", f"<arg: {len(SECRET) + 4} chars>", "<arg: 6 chars>"]),
        ("codex", ("codex", "exec", "-c", f"api_key={SECRET}", "fix it"), ["codex", "exec", "-c", f"<arg: {len(SECRET) + 8} chars>", "<prompt: 6 chars>"]),
        ("codex", ("codex", "exec", "-c", 'model="gpt-6.1-sol"', "-c", f"token={SECRET}", "fix it"), ["codex", "exec", "-c", 'model="gpt-6.1-sol"', "-c", f"<arg: {len(SECRET) + 6} chars>", "<prompt: 6 chars>"]),
        ("codex", ("codex", "resume", SECRET), ["codex", "resume", HIDDEN]),
        ("gemini", ("gemini", "-p", "fix it", "--session-id", SECRET), ["gemini", "-p", "<prompt: 6 chars>", "--session-id", HIDDEN]),
        (None, ("custom", "--token", SECRET, "task"), ["custom", "--token", HIDDEN, "<arg: 4 chars>"]),
        (None, ("custom", f"--token={SECRET}", "-dy", "-"), ["custom", f"--token={HIDDEN}", "-d<arg: 1 char>", "-"]),
        (None, ("./bin/custom", "--flag"), ["./bin/custom", "--flag"]),
    ],
)
def test_only_the_program_known_subcommands_the_model_flag_and_flag_names_are_shown(agent: str | None, argv: tuple[str, ...], expected: list[str]) -> None:
    inv = read(agent, *argv)
    assert inv.shown_argv() == expected
    assert SECRET not in inv.display() and inv.argv == argv  # what the agent gets is untouched


def test_the_model_flag_the_router_adds_is_shown_with_its_value_and_the_rest_stays_hidden() -> None:
    inv = read("claude", "claude", "--api-key", SECRET, "-p", "fix it").with_model("--model", "claude-sonnet-5-5")
    assert inv.shown_argv() == ["claude", "--model", "claude-sonnet-5-5", "--api-key", HIDDEN, "-p", "<prompt: 6 chars>"]
    assert inv.argv == ("claude", "--model", "claude-sonnet-5-5", "--api-key", SECRET, "-p", "fix it")
    inv = read("codex", "codex", "exec", "-c", f"token={SECRET}", "fix it").with_model("-m", "gpt-6.1-sol")
    assert inv.shown_argv() == ["codex", "exec", "-m", "gpt-6.1-sol", "-c", f"<arg: {len(SECRET) + 6} chars>", "<prompt: 6 chars>"]


def test_a_secret_in_a_flag_reaches_neither_the_panel_nor_the_json_but_the_agent_gets_it(world: World) -> None:
    args = ("claude", "--api-key", SECRET, f"--header=X-Token: {SECRET}", "-p", "fix the typo")
    dry = run("--dry-run", "--", *args)
    assert dry.exit_code == 0 and SECRET not in dry.stdout + dry.stderr
    assert f"--api-key '{HIDDEN}'" in flat(dry.stdout)
    data = decision("--", *args)
    assert SECRET not in json.dumps(data)
    assert data["argv"] == ["claude", "--model", "claude-sonnet-5-5", "--api-key", HIDDEN, f"--header=<arg: {len(SECRET) + 9} chars>", "-p", "<prompt: 12 chars>"]
    ran = run("-y", "--", *args)
    assert ran.exit_code == 0, ran.output
    assert SECRET not in ran.stdout.replace("FAKE-AGENT-RAN", "") + ran.stderr  # the decision went to stderr without it
    assert world.only_call()["argv"] == ["--model", "claude-sonnet-5-5", "--api-key", SECRET, f"--header=X-Token: {SECRET}", "-p", "fix the typo"]


def test_the_run_help_says_which_arguments_are_shown() -> None:
    out = CliRunner().invoke(app, ["run", "--help"], env={"COLUMNS": "120"})
    text = " ".join(out.stdout.split())
    assert "<arg: N chars>" in text and "--api-key" in text and "subcommand words" in text and "model flag" in text
