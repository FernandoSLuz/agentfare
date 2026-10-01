"""`agentfare init` (the 60-second setup) and the detection helpers it shares with `agentfare doctor`.

Every run here uses the synthetic fixtures (tests/fixtures), a throwaway data directory and a frozen clock. Nothing reads
the real ~/.claude, ~/.codex or ~/.gemini: conftest points HOME at an empty directory, and the tests that need other
folders build them under tmp_path with invented content.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import typer.main
from typer.testing import CliRunner

from agentfare import setup_checks
from agentfare.cli import app
from agentfare.config import DEFAULT_CONFIG_YAML, Config
from agentfare.db import Store

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_CONFIG = FIXTURES / "config.yaml"
NOW = "2026-09-30T12:00:00Z"
runner = CliRunner()


def invoke(*args: str, home: Path | None = None, config: Path | None = FIXTURE_CONFIG, columns: int = 100,
           env: dict[str, str | None] | None = None, global_args: tuple[str, ...] = ()) -> Any:
    """``agentfare [--config FIXTURE] [--home HOME] ARGS`` as if piped: no terminal, no colour, a fixed width."""
    environment: dict[str, str | None] = {"COLUMNS": str(columns), "AGENTFARE_NOW": NOW, "NO_COLOR": None, "FORCE_COLOR": None, **(env or {})}
    argv = [*(["--config", str(config)] if config else []), *(["--home", str(home)] if home else []), *global_args, *args]
    return runner.invoke(app, argv, env=environment)


def flat(text: str) -> str:
    """``text`` on one line, runs of blanks squeezed: wrapping and alignment never matter to these checks."""
    return " ".join(text.split())


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"  # does not exist yet: init creates it


@pytest.fixture
def machine(tmp_path: Path) -> Callable[..., Path]:
    """A directory tree of fake agent logs: ``machine(claude=3, codex=1)`` makes that many session files per agent, and returns
    the root the sources should point at. The files hold no real log, only what a file needs to be found."""

    def build(**counts: int) -> Path:
        root = tmp_path / "machine"
        for n in range(counts.get("claude", 0)):
            (root / "claude" / "projects" / f"proj-{n}").mkdir(parents=True, exist_ok=True)
            (root / "claude" / "projects" / f"proj-{n}" / f"s{n}.jsonl").write_text('{"type":"x"}\n')
        for n in range(counts.get("codex", 0)):
            (root / "codex" / "sessions" / "2026" / "09").mkdir(parents=True, exist_ok=True)
            (root / "codex" / "sessions" / "2026" / "09" / f"rollout-2026-09-0{n}T10-00-00-abc.jsonl").write_text('{"type":"x"}\n')
        for n in range(counts.get("gemini", 0)):
            (root / "gemini" / "tmp" / f"proj{n}" / "chats").mkdir(parents=True, exist_ok=True)
            (root / "gemini" / "tmp" / f"proj{n}" / "chats" / f"session-2026-09-0{n}T10-00-abcdef01.jsonl").write_text('{"type":"x"}\n')
        return root

    return build


def assert_commands_exist(*texts: str) -> None:
    """Every ``agentfare COMMAND --flag`` written in ``texts`` names a registered command and options it has."""
    group: Any = typer.main.get_command(app)
    seen = 0
    for text in texts:
        for match in re.finditer(r"agentfare ((?:[a-z]+)(?: --[a-z-]+)*)", flat(text)):
            command, *flags = match[1].split()
            assert command in group.commands, f"{match[0]!r} names a command that does not exist"
            options = {opt for p in group.commands[command].params for opt in p.opts}
            assert all(flag in options for flag in flags if flag != "--help"), f"{match[0]!r}: {command} has no such option"
            seen += 1
    assert seen >= 3, "the hints name no command at all, so the check proves nothing"


def sources_config(tmp_path: Path, root: Path) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(
        f"timezone: UTC\nsources:\n  claude: [{root}/claude/projects]\n  codex: [{root}/codex/sessions]\n  gemini: [{root}/gemini/tmp]\n"
    )
    return path


# ---- the command exists ---------------------------------------------------------------------------------
class TestRegistration:
    def test_init_is_a_command_with_its_four_options(self) -> None:
        group: Any = typer.main.get_command(app)
        assert "init" in group.commands
        options = {opt for p in group.commands["init"].params for opt in p.opts}
        assert {"--dry-run", "--write-config", "--no-ingest", "--json"} <= options

    def test_help_says_what_it_does(self) -> None:
        text = flat(invoke("init", "--help").output)
        assert "--dry-run" in text and "--write-config" in text and "--no-ingest" in text and "--json" in text


# ---- detection --------------------------------------------------------------------------------------------
class TestDetectionHelpers:
    def test_human_size(self) -> None:
        cases = {0: "0 B", 999: "999 B", 1000: "1.0 KB", 1249: "1.2 KB", 9_949: "9.9 KB", 9_950: "10 KB", 12_345: "12 KB",
                 999_499: "999 KB", 999_999: "1.0 MB", 4_100_000: "4.1 MB", 340_000_000: "340 MB", 1_200_000_000: "1.2 GB",
                 12_345_678_901_234: "12 TB"}
        for size, text in cases.items():
            assert setup_checks.human_size(size) == text, size

    def test_counts_read_like_a_sentence(self) -> None:
        assert [setup_checks.count_text(n, "file") for n in (0, 1, 2, 999, 30_000)] == ["0 files", "1 file", "2 files", "999 files", "30,000 files"]

    def test_tilde_shortens_only_the_home_directory(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "h"))
        assert setup_checks.tilde(tmp_path / "h" / ".claude" / "projects") == "~/.claude/projects"
        assert setup_checks.tilde(tmp_path / "h") == "~"
        assert setup_checks.tilde(tmp_path / "hx" / "y") == str(tmp_path / "hx" / "y")  # a sibling that starts with the same letters
        assert setup_checks.tilde("/srv/code") == "/srv/code"

    def test_scan_counts_the_files_the_parsers_would_read_and_reads_none(self, tmp_path: Path) -> None:
        cfg = Config.load(FIXTURE_CONFIG, tmp_path / "home")
        scans = {s.agent: s for s in setup_checks.scan_agents(cfg)}
        assert {a: s.files for a, s in scans.items()} == {"claude": 1, "codex": 1, "gemini": 1}
        for scan in scans.values():
            assert scan.found and not scan.truncated and scan.size > 0
            assert scan.roots[0].exists and scan.roots[0].readable and scan.roots[0].error == ""
        claude = FIXTURES / "claude" / "projects" / "-home-dev-acme-shop" / "2c9f6d1e-7a4b-4e35-9b0d-58f3a1c7e402.jsonl"
        assert scans["claude"].size == claude.stat().st_size  # the size of the session file, not of its sub-agent files

    def test_the_count_is_the_number_of_files_an_ingest_would_see(self, tmp_path: Path) -> None:
        """One definition of a session file: the scan and the ingest must never disagree about how many there are."""
        from agentfare.ingest import ingest

        cfg = Config.load(FIXTURE_CONFIG, tmp_path / "home")
        seen = ingest(cfg, Store(tmp_path / "x.db")).files_seen
        assert sum(s.files for s in setup_checks.scan_agents(cfg)) == seen == 3

    def test_a_missing_folder_is_reported_not_raised(self, tmp_path: Path) -> None:
        cfg = Config.load(None, tmp_path / "home")
        scan = {s.agent: s for s in setup_checks.scan_agents(cfg)}["claude"]
        assert not scan.found and scan.files == 0 and not scan.roots[0].exists and scan.origin == "default"

    def test_the_walk_is_capped(self, machine: Callable[..., Path], tmp_path: Path) -> None:
        root = machine(claude=5)
        cfg = Config.load(sources_config(tmp_path, root), tmp_path / "home")
        scan = setup_checks.scan_agent(cfg, "claude", cap=3)
        assert scan.files == 3 and scan.truncated
        assert not setup_checks.scan_agent(cfg, "claude", cap=5).truncated  # exactly the cap is not more than the cap

    def test_the_walk_has_a_time_limit(self, machine: Callable[..., Path], tmp_path: Path) -> None:
        root = machine(claude=5)
        cfg = Config.load(sources_config(tmp_path, root), tmp_path / "home")
        scan = setup_checks.scan_agent(cfg, "claude", seconds=-1)  # already over time at the first file
        assert scan.truncated and scan.files < 5

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
    def test_a_folder_that_cannot_be_read_is_named_not_raised(self, machine: Callable[..., Path], tmp_path: Path) -> None:
        root = machine(claude=1)
        cfg = Config.load(sources_config(tmp_path, root), tmp_path / "home")
        locked = root / "claude" / "projects"
        locked.chmod(0)
        try:
            root_scan = setup_checks.scan_agent(cfg, "claude").roots[0]
        finally:
            locked.chmod(0o755)
        assert root_scan.exists and not root_scan.readable

    def test_origin_says_where_the_folders_came_from(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        assert setup_checks.scan_agent(Config.load(None, tmp_path / "home"), "codex").origin == "default"
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "moved"))
        moved = setup_checks.scan_agent(Config.load(None, tmp_path / "home"), "codex")
        assert moved.origin == "CODEX_HOME" and [r.path for r in moved.roots] == [tmp_path / "moved" / "sessions", tmp_path / "moved" / "archived_sessions"]
        assert setup_checks.scan_agent(Config.load(FIXTURE_CONFIG, tmp_path / "home"), "codex").origin == "config"  # sources.codex written in a file

    def test_the_relocation_variables_are_the_ones_config_follows(self) -> None:
        from agentfare.config import _RELOCATION

        assert {agent: var for agent, (var, _) in _RELOCATION.items()} == setup_checks.RELOCATION_VARIABLES

    def test_a_cli_is_looked_up_on_path_and_never_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "ran"
        program = bin_dir / "claude"
        program.write_text(f"#!/bin/sh\ntouch {marker}\n")
        program.chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        cfg = Config.load(None, tmp_path / "home")
        scans = {s.agent: s for s in setup_checks.scan_agents(cfg)}
        assert scans["claude"].cli_path == str(program) and scans["codex"].cli_path is None
        assert not marker.exists()

    def test_the_command_names_come_from_the_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "claude-work").write_text("#!/bin/sh\n")
        (bin_dir / "claude-work").chmod(0o755)
        monkeypatch.setenv("PATH", str(bin_dir))
        cfg_file = tmp_path / "c.yaml"
        cfg_file.write_text("agents:\n  claude: {commands: [claude-work]}\n")
        scan = {s.agent: s for s in setup_checks.scan_agents(Config.load(cfg_file, tmp_path / "home"))}["claude"]
        assert scan.cli_names == ("claude-work",) and scan.cli_path == str(bin_dir / "claude-work")

    def test_a_malformed_agents_section_falls_back_to_the_agent_name(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "c.yaml"
        cfg_file.write_text("agents:\n  claude: {commands: 7}\n")
        scan = {s.agent: s for s in setup_checks.scan_agents(Config.load(cfg_file, tmp_path / "home"))}["claude"]
        assert scan.cli_names == ("claude",)

    def test_json_shape_of_a_scan_is_stable(self, tmp_path: Path) -> None:
        scan = {s.agent: s for s in setup_checks.scan_agents(Config.load(FIXTURE_CONFIG, tmp_path / "home"))}["claude"]
        data = scan.as_json()
        assert list(data) == ["agent", "label", "origin", "found", "files", "bytes", "truncated", "folders", "cli"]
        assert list(data["folders"][0]) == ["path", "exists", "readable", "files", "bytes", "truncated", "error"]
        assert list(data["cli"]) == ["commands", "path"]
        json.dumps(data)


# ---- init --dry-run -------------------------------------------------------------------------------------------
class TestDryRun:
    def test_it_says_what_it_will_read_and_stores(self, data_dir: Path) -> None:
        result = invoke("init", "--dry-run", home=data_dir)
        assert result.exit_code == 0, result.output
        text = flat(result.stdout)
        assert "only metadata and token counts" in text and "never prompts, replies or file contents" in text
        for folder in ("claude/projects", "codex/sessions", "gemini/tmp"):
            assert str(FIXTURES / folder) in text, folder  # the exact folder, not a shortened or guessed one
        assert text.index("only metadata and token counts") < text.index("Claude Code")  # said before the table

    def test_it_counts_files_and_sizes_without_reading_them(self, data_dir: Path) -> None:
        text = flat(invoke("init", "--dry-run", home=data_dir).stdout)
        assert "Claude Code found 1 file," in text and "Codex CLI found 1 file," in text and "Gemini CLI found 1 file," in text

    def test_it_writes_and_reads_nothing(self, data_dir: Path) -> None:
        result = invoke("init", "--dry-run", "--write-config", home=data_dir)
        assert result.exit_code == 0
        assert not data_dir.exists()  # no data directory, no database, no config
        assert "Dry run" in result.stdout and "nothing was" in flat(result.stdout)
        assert "would" in result.stdout

    def test_it_closes_with_one_sentence_of_what_a_real_run_would_do(self, data_dir: Path, tmp_path: Path) -> None:
        text = flat(invoke("init", "--dry-run", home=data_dir).stdout)
        assert f"Without --dry-run, init would create {data_dir} (0700) and read 3 log files (" in text
        assert f") into {data_dir / 'agentfare.db'}." in text  # a config file is in use already, so there is nothing to say about one
        empty = flat(invoke("init", "--dry-run", "--write-config", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")}).stdout)
        assert f"init would create {data_dir} (0700), find no session files to read and write {data_dir / 'config.yaml'}." in empty
        plain_run = flat(invoke("init", "--dry-run", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")}).stdout)
        assert "and write no config file (add --write-config to write one)." in plain_run

    def test_a_missing_agent_says_how_to_point_at_its_logs(self, tmp_path: Path, data_dir: Path) -> None:
        text = flat(invoke("init", "--dry-run", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")}).stdout)
        assert "Gemini CLI not found" in text
        assert "If you use Gemini CLI from another folder, set sources.gemini in" in text and "config.yaml" in text
        assert "(missing)" in text

    def test_json_has_the_detection_and_no_result(self, data_dir: Path) -> None:
        result = invoke("init", "--dry-run", "--json", home=data_dir)
        assert result.exit_code == 0, result.stderr
        data = json.loads(result.stdout)
        assert list(data) == ["dry_run", "data_dir", "config", "agents", "ingest", "result", "next_steps"]
        assert data["dry_run"] is True and data["ingest"] is None and data["result"] is None
        assert [a["agent"] for a in data["agents"]] == ["claude", "codex", "gemini"]
        assert [a["files"] for a in data["agents"]] == [1, 1, 1] and all(a["found"] for a in data["agents"])
        assert data["data_dir"] == {"path": str(data_dir), "exists": False, "created": False}
        assert data["config"]["written"] is False and data["config"]["in_use"] is True
        assert not data_dir.exists()

    def test_a_broken_config_is_one_error_line_that_points_at_doctor(self, tmp_path: Path, data_dir: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("timezone: [unclosed\n")
        result = invoke("init", "--dry-run", home=data_dir, config=bad)
        assert result.exit_code == 2
        assert result.stderr.startswith("error: ") and "agentfare doctor" in result.stderr
        assert "Traceback" not in result.output and not data_dir.exists()


# ---- init -------------------------------------------------------------------------------------------------------
class TestInit:
    def test_the_first_run_sets_everything_up_and_shows_the_result(self, data_dir: Path) -> None:
        result = invoke("init", home=data_dir)
        assert result.exit_code == 0, result.output
        text = flat(result.stdout)
        assert "Found 3 sessions (claude 1, codex 1, gemini 1) from 2026-09-28 to 2026-09-30" in text
        assert "Spend so far: $3.63" in text
        assert "$0.6140" in text and "16.9%" in text
        assert "session 7c8d9e0f: you spent $0.76 on gpt-6-astra" in text and "5.2x cheaper" in text
        assert "first run: reading your agent logs into a local database" in result.stderr  # the ingest's own progress line

    def test_what_it_creates_is_private(self, data_dir: Path) -> None:
        invoke("init", home=data_dir)
        assert mode(data_dir) == 0o700 and mode(data_dir / "agentfare.db") == 0o600
        assert sorted(p.name for p in data_dir.iterdir()) == ["agentfare.db"]  # no config file unless --write-config

    def test_the_tool_works_without_a_config_and_says_so(self, data_dir: Path, tmp_path: Path) -> None:
        text = flat(invoke("init", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")}).stdout)
        assert "AgentFare works without a config file" in text and "init --write-config" in text
        assert not (data_dir / "config.yaml").exists()

    def test_write_config_writes_the_documented_defaults(self, data_dir: Path, tmp_path: Path) -> None:
        result = invoke("init", "--write-config", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")})
        assert result.exit_code == 0, result.output
        written = data_dir / "config.yaml"
        assert written.read_text() == DEFAULT_CONFIG_YAML and mode(written) == 0o600
        assert "wrote" in result.stdout and "config.yaml" in result.stdout

    def test_write_config_to_the_file_named_with_config(self, tmp_path: Path, data_dir: Path) -> None:
        target = tmp_path / "mine" / "agentfare.yaml"
        target.parent.mkdir()
        result = invoke("init", "--write-config", home=data_dir, config=target)  # does not exist yet: init creates it
        assert result.exit_code == 0, result.output
        assert target.read_text() == DEFAULT_CONFIG_YAML and not (data_dir / "config.yaml").exists()

    def test_an_existing_config_is_never_overwritten(self, data_dir: Path, tmp_path: Path) -> None:
        data_dir.mkdir()
        mine = data_dir / "config.yaml"
        mine.write_text("timezone: UTC\nidle_gap_minutes: 7\n")
        result = invoke("init", "--write-config", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")})
        assert result.exit_code == 0, result.output
        assert mine.read_text() == "timezone: UTC\nidle_gap_minutes: 7\n"
        assert "already exists" in flat(result.stdout)

    def test_it_is_safe_to_run_again(self, data_dir: Path) -> None:
        first = json.loads(invoke("init", "--json", home=data_dir).stdout)
        again = json.loads(invoke("init", "--json", home=data_dir).stdout)
        assert first["ingest"]["files_parsed"] == 3 and first["ingest"]["files_unchanged"] == 0
        assert again["ingest"]["files_parsed"] == 0 and again["ingest"]["files_unchanged"] == 3  # incremental
        assert again["result"] == first["result"]  # the same sessions and the same money
        assert again["data_dir"]["created"] is False and first["data_dir"]["created"] is True
        with Store(data_dir / "agentfare.db") as store:
            assert store.count() == 3

    def test_no_ingest_creates_the_directory_and_reads_nothing(self, data_dir: Path) -> None:
        result = invoke("init", "--no-ingest", home=data_dir)
        assert result.exit_code == 0, result.output
        assert mode(data_dir) == 0o700 and not (data_dir / "agentfare.db").exists()
        assert "agentfare ingest" in flat(result.stdout) and "Spend so far" not in result.stdout
        assert "first run" not in result.stderr

    def test_the_global_no_ingest_does_the_same(self, data_dir: Path) -> None:
        result = invoke("init", home=data_dir, global_args=("--no-ingest",))
        assert result.exit_code == 0 and not (data_dir / "agentfare.db").exists()

    def test_next_steps_are_the_commands_worth_running(self, data_dir: Path) -> None:
        text = flat(invoke("init", home=data_dir).stdout)
        assert "Next steps" in text
        steps = text[text.index("Next steps"):]
        for command in ("agentfare summary", "agentfare advise", "agentfare report --format html",
                        'agentfare run --dry-run -- claude -p "fix the typo in the README"', "agentfare doctor", "agentfare prices --refresh"):
            assert command in steps, command

    def test_every_next_step_is_a_real_command(self, data_dir: Path) -> None:
        """The suggestions are checked like the README's: a command that does not exist, or a flag it lacks, fails here."""
        group: Any = typer.main.get_command(app)
        for step in json.loads(invoke("init", "--json", home=data_dir).stdout)["next_steps"]:
            words = step["command"].split()
            assert words[0] == "agentfare" and words[1] in group.commands, step
            options = {opt for p in group.commands[words[1]].params for opt in p.opts}
            assert all(w in options for w in words[2:] if w.startswith("--") and w != "--"), step
            assert step["purpose"]

    def test_the_run_example_uses_a_detected_agent(self, tmp_path: Path, machine: Callable[..., Path], data_dir: Path) -> None:
        from agentfare.commands.init import _next_steps

        cfg = Config.load(sources_config(tmp_path, machine(codex=1)), data_dir)
        assert any(s["command"] == 'agentfare run --dry-run -- codex exec "fix the typo in the README"' for s in _next_steps("found", setup_checks.scan_agents(cfg)))
        none = Config.load(None, data_dir)  # nothing found and no command on PATH: the example is for Claude Code
        assert any(s["command"].startswith('agentfare run --dry-run -- claude -p') for s in _next_steps("found", setup_checks.scan_agents(none)))

    def test_nothing_from_the_logs_reaches_the_output_or_the_database(self, data_dir: Path) -> None:
        result = invoke("init", home=data_dir)
        assert "PRIVACY-CANARY" not in result.stdout + result.stderr
        assert b"PRIVACY-CANARY" not in (data_dir / "agentfare.db").read_bytes()

    def test_piped_output_has_no_colour(self, data_dir: Path) -> None:
        result = invoke("init", home=data_dir)
        assert "\x1b" not in result.stdout + result.stderr

    def test_it_never_touches_a_shell_startup_file(self, data_dir: Path) -> None:
        invoke("init", "--write-config", home=data_dir)
        home = Path(os.environ["HOME"])
        assert not any(p.name in (".bashrc", ".zshrc", ".bash_completions", ".config") for p in home.iterdir())

    def test_a_narrow_terminal_still_shows_the_whole_folder(self, data_dir: Path) -> None:
        lines = invoke("init", "--dry-run", home=data_dir, columns=50).stdout.splitlines()
        assert any(str(FIXTURES / "claude" / "projects") in line for line in lines)  # a path is never broken over two lines


# ---- the result -------------------------------------------------------------------------------------------------
class TestResult:
    def test_json_result_is_stable(self, data_dir: Path) -> None:
        data = json.loads(invoke("init", "--json", home=data_dir).stdout)
        assert list(data) == ["dry_run", "data_dir", "config", "agents", "ingest", "result", "next_steps"]
        result = data["result"]
        assert list(result) == ["sessions", "by_agent", "first_day", "last_day", "spend", "excluded_tokens", "warnings", "savings", "biggest"]
        assert result["sessions"] == 3 and result["by_agent"] == {"claude": 1, "codex": 1, "gemini": 1}
        assert (result["first_day"], result["last_day"]) == ("2026-09-28", "2026-09-30")
        assert result["spend"]["cents"] == 363 and result["excluded_tokens"] == 0 and result["warnings"] == []
        assert result["savings"]["saved"]["ucents"] == 61_400_000 and result["savings"]["switched"] == 1
        assert result["biggest"]["short_id"] == "7c8d9e0f" and result["biggest"]["recommended_model"] == "gpt-6.1-sol"
        assert data["ingest"]["database"] == {"path": str(data_dir / "agentfare.db"), "sessions": 3}

    def test_the_saving_matches_what_advise_prints(self, data_dir: Path) -> None:
        result = json.loads(invoke("init", "--json", home=data_dir).stdout)["result"]
        advise = json.loads(invoke("advise", "--json", home=data_dir).stdout)
        assert result["savings"]["saved"] == advise["total"]["saved"]
        assert result["biggest"]["saved"] == advise["sessions"][0]["saved"]
        assert result["biggest"]["line"] == advise["sessions"][0]["line"]

    def test_the_spend_matches_what_summary_prints(self, data_dir: Path) -> None:
        result = json.loads(invoke("init", "--json", home=data_dir).stdout)["result"]
        assert result["spend"] == json.loads(invoke("summary", "--json", home=data_dir).stdout)["total"]

    def test_a_model_without_a_verified_price_is_named_and_left_out(self, tmp_path: Path, data_dir: Path) -> None:
        from dataclasses import replace

        from agentfare.pricing import PriceTable, builtin_path, dump_pricing, load_pricing

        builtin = load_pricing(path=builtin_path())
        models = {name: replace(p, input=None, cached_input=None, output=None, unverified_fields=("input", "cached_input", "output"))
                  if name == "gpt-6-astra" else p for name, p in builtin.models.items()}
        (tmp_path / "prices.yaml").write_text(dump_pricing(PriceTable(models=models, aliases=dict(builtin.aliases))))
        config = tmp_path / "cfg.yaml"
        config.write_text(f"timezone: UTC\npricing_file: {tmp_path / 'prices.yaml'}\nsources:\n  claude: [{FIXTURES}/claude/projects]\n"
                          f"  codex: [{FIXTURES}/codex/sessions]\n  gemini: [{FIXTURES}/gemini/tmp]\n")
        result = invoke("init", home=data_dir, config=config)
        assert result.exit_code == 0, result.output
        text = flat(result.stdout)
        assert "Spend so far: $2.87+" in text  # the plus: something is left out of it
        assert "1 model without a verified price" in text and "gpt-6-astra" in text
        data = json.loads(invoke("init", "--json", home=data_dir, config=config).stdout)["result"]
        assert data["excluded_tokens"] == 106_000 and [w["model"] for w in data["warnings"]] == ["gpt-6-astra"]

    def test_the_warning_goes_to_stdout_only_as_text_never_into_json_noise(self, data_dir: Path) -> None:
        result = invoke("init", "--json", home=data_dir)
        json.loads(result.stdout)  # stdout is one JSON document and nothing else
        assert "first run" in result.stderr


# ---- nothing found ------------------------------------------------------------------------------------------------
class TestNothingFound:
    def run_empty(self, tmp_path: Path, data_dir: Path, *args: str) -> Any:
        return invoke("init", *args, home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")})

    def test_it_is_kind_and_points_to_doctor_and_sources(self, tmp_path: Path, data_dir: Path) -> None:
        result = self.run_empty(tmp_path, data_dir)
        assert result.exit_code == 0, result.output
        text = flat(result.stdout)
        assert "No sessions found yet" in text
        assert "agentfare doctor" in text and "sources.<agent>" in text and "config.yaml" in text
        assert "Traceback" not in result.output and "Spend so far" not in text

    def test_the_database_is_still_set_up(self, tmp_path: Path, data_dir: Path) -> None:
        self.run_empty(tmp_path, data_dir)
        assert mode(data_dir) == 0o700 and (data_dir / "agentfare.db").exists()

    def test_json_keeps_every_key(self, tmp_path: Path, data_dir: Path) -> None:
        data = json.loads(self.run_empty(tmp_path, data_dir, "--json").stdout)
        assert data["result"]["sessions"] == 0 and data["result"]["biggest"] is None and data["result"]["by_agent"] == {}
        assert data["result"]["spend"] == {"cents": 0, "ucents": 0}
        assert [a["found"] for a in data["agents"]] == [False, False, False]
        assert data["next_steps"][0]["command"] == "agentfare doctor"

    def test_it_offers_the_demo(self, tmp_path: Path, data_dir: Path) -> None:
        text = flat(self.run_empty(tmp_path, data_dir).stdout)
        assert "agentfare demo" in text
        steps = json.loads(self.run_empty(tmp_path, data_dir, "--json").stdout)["next_steps"]
        assert [s["command"] for s in steps] == ["agentfare doctor", "agentfare config", "agentfare demo"]


# ---- when something is wrong ------------------------------------------------------------------------------------------
class TestProblems:
    def test_a_database_from_a_newer_agentfare_is_one_error_line_that_points_at_doctor(self, data_dir: Path) -> None:
        data_dir.mkdir(mode=0o700)
        with sqlite3.connect(data_dir / "agentfare.db") as conn:
            conn.execute("PRAGMA user_version = 99")
        before = (data_dir / "agentfare.db").read_bytes()
        result = invoke("init", home=data_dir)
        assert result.exit_code == 2 and result.stderr.startswith("error: ") and "newer agentfare" in result.stderr
        assert "agentfare doctor" in result.stderr and "Traceback" not in result.output
        assert (data_dir / "agentfare.db").read_bytes() == before  # a newer file is never touched

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
    def test_an_unreadable_log_is_a_warning_and_the_rest_is_read(self, tmp_path: Path, data_dir: Path) -> None:
        copy = tmp_path / "copy"
        shutil.copytree(FIXTURES, copy, ignore=shutil.ignore_patterns("__pycache__"))
        locked = next((copy / "gemini").rglob("session-*.jsonl"))
        locked.chmod(0)
        try:
            result = invoke("init", home=data_dir, config=copy / "config.yaml")
            data = json.loads(invoke("init", "--json", home=data_dir, config=copy / "config.yaml").stdout)
        finally:
            locked.chmod(0o644)
        assert result.exit_code == 0, result.output
        assert "PermissionError" in result.stderr and "1 file could not be read" in flat(result.stdout)
        assert data["result"]["sessions"] == 2 and data["ingest"]["files_failed"] == 1
        assert "Traceback" not in result.output

    def test_a_config_that_is_not_valid_yaml_is_not_replaced_by_write_config(self, tmp_path: Path, data_dir: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("timezone: [unclosed\n")
        result = invoke("init", "--write-config", home=data_dir, config=bad)
        assert result.exit_code == 2 and bad.read_text() == "timezone: [unclosed\n"

    def test_an_unwritable_place_for_the_data_is_one_error_line(self, tmp_path: Path) -> None:
        blocker = tmp_path / "file"
        blocker.write_text("x")
        result = invoke("init", home=blocker / "data")
        assert result.exit_code == 2 and result.stderr.startswith("error: cannot create the data directory")
        assert isinstance(result.exception, SystemExit)  # an exit, not an exception that escaped


# ---- where it looks ----------------------------------------------------------------------------------------------
class TestSources:
    def test_the_agents_own_variables_are_followed(self, tmp_path: Path, data_dir: Path, machine: Callable[..., Path]) -> None:
        root = machine(claude=2, codex=1, gemini=1)
        env = {"CLAUDE_CONFIG_DIR": str(root / "claude"), "CODEX_HOME": str(root / "codex"), "GEMINI_CLI_HOME": str(tmp_path / "nohome"),
               "HOME": str(tmp_path / "empty")}
        data = json.loads(invoke("init", "--dry-run", "--json", home=data_dir, config=None, env=env).stdout)
        agents = {a["agent"]: a for a in data["agents"]}
        assert (agents["claude"]["files"], agents["claude"]["origin"]) == (2, "CLAUDE_CONFIG_DIR")
        assert (agents["codex"]["files"], agents["codex"]["origin"]) == (1, "CODEX_HOME")
        assert agents["gemini"]["files"] == 0 and agents["gemini"]["origin"] == "GEMINI_CLI_HOME"
        assert [f["path"] for f in agents["gemini"]["folders"]] == [str(tmp_path / "nohome" / ".gemini" / "tmp")]

    def test_the_text_says_where_the_folders_came_from(self, tmp_path: Path, data_dir: Path, machine: Callable[..., Path]) -> None:
        root = machine(claude=1)
        text = flat(invoke("init", "--dry-run", home=data_dir, config=None, env={"CLAUDE_CONFIG_DIR": str(root / "claude"), "HOME": str(tmp_path / "e")}).stdout)
        assert "from CLAUDE_CONFIG_DIR" in text
        text = flat(invoke("init", "--dry-run", home=data_dir).stdout)
        assert "from sources.claude in" in text

    def test_the_cli_is_reported_from_the_path(self, tmp_path: Path, data_dir: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "gemini").write_text("#!/bin/sh\n")
        (bin_dir / "gemini").chmod(0o755)
        data = json.loads(invoke("init", "--dry-run", "--json", home=data_dir, env={"PATH": str(bin_dir)}).stdout)
        agents = {a["agent"]: a for a in data["agents"]}
        assert agents["gemini"]["cli"]["path"] == str(bin_dir / "gemini") and agents["claude"]["cli"]["path"] is None
        text = flat(invoke("init", "--dry-run", home=data_dir, env={"PATH": str(bin_dir)}).stdout)
        assert f"CLI {bin_dir / 'gemini'}" in text and "CLI not on PATH" in text

    def test_home_is_written_as_a_tilde(self, tmp_path: Path, data_dir: Path) -> None:
        home = tmp_path / "userhome"
        (home / ".claude" / "projects" / "p").mkdir(parents=True)
        (home / ".claude" / "projects" / "p" / "s.jsonl").write_text("{}\n")
        text = flat(invoke("init", "--dry-run", home=data_dir, config=None, env={"HOME": str(home)}).stdout)
        assert "Claude Code found 1 file" in text and "~/.claude/projects" in text and str(home) not in text
        data = json.loads(invoke("init", "--dry-run", "--json", home=data_dir, config=None, env={"HOME": str(home)}).stdout)
        assert data["agents"][0]["folders"][0]["path"] == str(home / ".claude" / "projects")  # JSON keeps the full path


# ---- the empty-state hints ---------------------------------------------------------------------------------------
class TestEmptyHints:
    def test_an_empty_database_points_at_init_and_doctor(self, data_dir: Path, tmp_path: Path) -> None:
        result = invoke("summary", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")})
        text = flat(result.stdout)
        assert "the database has no sessions at all: run agentfare ingest, and check the log sources with agentfare config" in text
        assert "agentfare init" in text and "agentfare doctor" in text

    def test_the_demo_is_offered_too(self, data_dir: Path, tmp_path: Path) -> None:
        text = flat(invoke("summary", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")}).stdout)
        assert "agentfare demo" in text

    def test_every_command_a_hint_names_exists_with_the_flags_it_shows(self, data_dir: Path, tmp_path: Path) -> None:
        """A hint that points at a command or a flag that is not there fails here, not in a person's terminal."""
        texts = [invoke("summary", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")}).stdout,
                 invoke("init", home=data_dir, config=None, env={"HOME": str(tmp_path / "empty")}).stdout,
                 invoke("init", "--dry-run", home=data_dir).stdout, invoke("init", "--no-ingest", home=data_dir).stdout]
        assert_commands_exist(*texts)

    def test_a_database_with_data_has_no_setup_advice(self, data_dir: Path) -> None:
        text = flat(invoke("summary", "--week-of", "2026-W30", home=data_dir).stdout)
        assert "agentfare init" not in text and "agentfare doctor" not in text


# ---- tab completion -----------------------------------------------------------------------------------------------
# Typer detects the shell from the process that started it; this switch makes the shell a value of the option instead
# (``--show-completion zsh``), so that the scripts of all three shells can be checked on any machine.
NAMED_SHELL = {"_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION": "1"}


class TestCompletion:
    def test_show_completion_prints_the_script_without_installing_anything(self) -> None:
        done = self.agentfare("--show-completion", "bash")  # a named shell: with none given Typer detects the parent shell, which a CI runner may not have
        assert done.returncode == 0, done.stderr
        assert "_AGENTFARE_COMPLETE=complete_" in done.stdout and "agentfare" in done.stdout
        assert list(Path(os.environ["HOME"]).iterdir()) == []  # nothing written anywhere

    @staticmethod
    def agentfare(*args: str) -> subprocess.CompletedProcess[str]:
        """The real program in a child process: Typer reads the shell switch when the program starts."""
        env = {**os.environ, **NAMED_SHELL, "COLUMNS": "100"}
        code = "import sys; sys.argv[0] = 'agentfare'; from agentfare.cli import _entry; _entry()"  # what the installed script does
        return subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, env=env, timeout=60)

    @pytest.mark.parametrize(("shell", "marker"), [("bash", "complete -o default -F _agentfare_completion agentfare"),
                                                   ("zsh", "#compdef agentfare"), ("fish", "complete --command agentfare")])
    def test_each_shell_gets_its_own_script(self, shell: str, marker: str) -> None:
        done = self.agentfare("--show-completion", shell)
        assert done.returncode == 0, done.stderr
        assert marker in done.stdout and f"_AGENTFARE_COMPLETE=complete_{shell}" in done.stdout
        assert list(Path(os.environ["HOME"]).iterdir()) == []

    def test_install_completion_is_explicit_and_edits_only_the_users_own_files(self) -> None:
        home = Path(os.environ["HOME"])  # an empty directory made by conftest, never the real home
        done = self.agentfare("--install-completion", "bash")
        assert done.returncode == 0, done.stderr
        changed = sorted(str(p.relative_to(home)) for p in home.rglob("*") if p.is_file())
        assert changed and all(name.startswith((".bashrc", ".bash_completions")) for name in changed), changed

    def test_a_shell_that_asks_for_candidates_gets_them_and_nothing_is_written(self) -> None:
        """What a shell with the script installed runs on every tab: the commands and the options, with no side effect."""
        code = "import sys; sys.argv[0] = 'agentfare'; from agentfare.cli import _entry; _entry()"
        for words, cword, expected in (("agentfare i", "1", {"init", "ingest"}), ("agentfare init --", "2", {"--dry-run", "--write-config", "--no-ingest", "--json"}),
                                       ("agentfare doctor --", "2", {"--json"})):
            env = {**os.environ, "COMP_WORDS": words, "COMP_CWORD": cword, "_AGENTFARE_COMPLETE": "complete_bash"}
            done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
            assert done.returncode == 0 and expected <= set(done.stdout.split()), (words, done.stdout, done.stderr)
        assert list(Path(os.environ["HOME"]).iterdir()) == []

    def test_no_other_command_installs_it(self, data_dir: Path) -> None:
        for args in (("init",), ("init", "--write-config"), ("doctor",), ("summary",), ("config",), ("demo",)):
            invoke(*args, home=data_dir)
        assert list(Path(os.environ["HOME"]).iterdir()) == []

    def test_the_help_stays_short(self) -> None:
        """The two options cost two entries in --help and nothing else: the list of options stays under ten."""
        text = invoke("--help", config=None).output
        names = [w for line in text.splitlines() for w in line.split() if w.startswith("--")]
        assert {"--install-completion", "--show-completion"} <= set(names) and len(set(names)) <= 9


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__]))
