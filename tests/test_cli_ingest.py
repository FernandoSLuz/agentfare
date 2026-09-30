"""`agentfare ingest`: what it prints, and that none of it is broken across lines in the middle of an item at 80 columns."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner, Result

from agentfare.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_CONFIG = FIXTURES / "config.yaml"
FIRST_RUN = "first run: reading your agent logs into a local database; this can take a minute"
READ_ALL = "read 3 session records from 3 files (3 parsed, 0 unchanged); the database holds 3 distinct sessions"
READ_NONE = "read 0 session records from 3 files (0 parsed, 3 unchanged); the database holds 3 distinct sessions"


def ingest(home: Path, *args: str, columns: int = 80, config: Path = FIXTURE_CONFIG) -> Result:
    return CliRunner().invoke(app, ["--config", str(config), "--home", str(home), "ingest", *args], env={"COLUMNS": str(columns)})


def lines(text: str) -> list[str]:
    return text.splitlines()


@pytest.fixture
def deep_home(tmp_path: Path) -> Path:
    """A data directory whose database path alone is longer than 80 columns."""
    return tmp_path / ("a-directory-with-a-long-name-" * 3) / "data"


def test_the_report_says_what_was_read_and_what_the_database_holds(tmp_path: Path) -> None:
    result = ingest(tmp_path)
    assert result.exit_code == 0, result.output
    assert lines(result.stdout) == [
        READ_ALL,
        "3 sessions stored (claude: 1, codex: 1, gemini: 1)",
        f"database: {tmp_path / 'agentfare.db'}",
    ]


def test_a_second_run_has_nothing_to_parse_and_the_database_still_holds_everything(tmp_path: Path) -> None:
    ingest(tmp_path)
    again = ingest(tmp_path)
    assert lines(again.stdout)[:2] == [READ_NONE, "0 sessions stored (none)"]


def test_one_of_each_reads_in_the_singular(tmp_path: Path) -> None:
    assert lines(ingest(tmp_path, "--agent", "codex").stdout)[:2] == [
        "read 1 session record from 1 file (1 parsed, 0 unchanged); the database holds 1 distinct session",
        "1 session stored (codex: 1)",
    ]


@pytest.fixture
def resumed(tmp_path: Path) -> Path:
    """A config whose two Claude directories hold the same session (a resumed or copied one): two records, one session."""
    log = FIXTURES / "claude" / "projects" / "-home-dev-acme-shop" / "2c9f6d1e-7a4b-4e35-9b0d-58f3a1c7e402.jsonl"
    for name in ("one", "two"):
        (tmp_path / name / "-proj").mkdir(parents=True)
        shutil.copy(log, tmp_path / name / "-proj" / log.name)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"sources": {"claude": [str(tmp_path / "one"), str(tmp_path / "two")], "codex": [], "gemini": []},
                                      "timezone": "UTC"}))
    return config


def test_records_read_and_distinct_sessions_are_told_apart(tmp_path: Path, resumed: Path) -> None:
    out = lines(ingest(tmp_path / "home", config=resumed).stdout)
    assert out[:2] == ["read 2 session records from 2 files (2 parsed, 0 unchanged); the database holds 1 distinct session",
                       "1 session stored (claude: 1)"]


def test_json_has_stable_keys_and_the_same_numbers(tmp_path: Path) -> None:
    result = ingest(tmp_path, "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert list(report) == ["files_seen", "files_parsed", "files_unchanged", "sessions_read", "sessions_stored", "by_agent",
                            "database", "missing_roots", "errors", "files_failed", "duplicate_sessions"]  # additive: the new keys come last
    assert (report["files_seen"], report["files_parsed"], report["files_unchanged"]) == (3, 3, 0)
    assert (report["sessions_read"], report["sessions_stored"]) == (3, 3)
    assert report["by_agent"] == {"claude": 1, "codex": 1, "gemini": 1}
    assert report["database"] == {"path": str(tmp_path / "agentfare.db"), "sessions": 3}
    assert report["missing_roots"] == report["errors"] == []
    assert (report["files_failed"], report["duplicate_sessions"]) == (0, 0)
    assert "\x1b" not in result.stdout
    again = json.loads(ingest(tmp_path, "--json").stdout)
    assert list(again) == list(report)  # the keys do not depend on what happened
    assert (again["files_parsed"], again["files_unchanged"], again["sessions_read"], again["sessions_stored"]) == (0, 3, 0, 0)
    assert again["by_agent"] == {"claude": 0, "codex": 0, "gemini": 0} and again["database"]["sessions"] == 3


def test_json_counts_records_and_distinct_sessions_apart(tmp_path: Path, resumed: Path) -> None:
    report = json.loads(ingest(tmp_path / "home", "--json", config=resumed).stdout)
    assert (report["sessions_read"], report["sessions_stored"], report["database"]["sessions"]) == (2, 1, 1)
    assert report["by_agent"] == {"claude": 1, "codex": 0, "gemini": 0}


def test_json_carries_the_problems_and_stdout_stays_json(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-directory"
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"sources": {"claude": [str(missing)], "codex": [], "gemini": []}, "timezone": "UTC"}))
    result = ingest(tmp_path / "home", "--json", config=config)
    assert json.loads(result.stdout)["missing_roots"] == [f"claude: {missing}"]
    assert f"not found (skipped): claude: {missing}" in lines(result.stderr)  # still said where a person sees it


def test_no_line_is_broken_in_the_middle_of_an_item_at_80_columns(deep_home: Path) -> None:
    result = ingest(deep_home, columns=80)
    out = lines(result.stdout)
    database = deep_home / "agentfare.db"
    assert len(str(database)) > 80  # the case this is about
    assert f"database: {database}" in out  # the path, whole, on one line
    assert all(len(line) <= 80 for line in out if not line.startswith(("database:", "read ")))
    assert READ_ALL in out  # the sentence is one line too: Rich does not break it, the terminal wraps it
    assert "3 sessions stored (claude: 1, codex: 1, gemini: 1)" in out


@pytest.mark.parametrize("columns", [40, 60, 80, 120])
def test_the_same_lines_at_every_width(tmp_path: Path, columns: int) -> None:
    assert lines(ingest(tmp_path / f"w{columns}", columns=columns).stdout)[:2] == [
        READ_ALL,
        "3 sessions stored (claude: 1, codex: 1, gemini: 1)",
    ]


def test_the_first_run_line_and_the_report_are_on_different_streams(tmp_path: Path) -> None:
    result = ingest(tmp_path)
    assert FIRST_RUN in result.stderr and FIRST_RUN not in result.stdout
    assert "\x1b" not in result.stderr  # piped: no colour, no cursor codes
    assert FIRST_RUN not in ingest(tmp_path).stderr  # the database is no longer empty


def test_a_path_in_a_warning_is_not_cut_either(tmp_path: Path) -> None:
    missing = tmp_path / ("no-such-directory-with-a-long-name-" * 3)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"sources": {"claude": [str(missing)], "codex": [], "gemini": []}, "timezone": "UTC"}))
    result = ingest(tmp_path / "home", config=config, columns=80)
    assert result.exit_code == 0, result.output
    assert f"not found (skipped): claude: {missing}" in lines(result.stderr)
    assert lines(result.stdout)[:2] == [
        "read 0 session records from 0 files (0 parsed, 0 unchanged); the database holds 0 distinct sessions", "0 sessions stored (none)"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
def test_a_log_that_cannot_be_read_is_a_warning_on_one_line(tmp_path: Path) -> None:
    root = tmp_path / ("logs-with-a-long-directory-name-" * 3)
    (root / "-proj").mkdir(parents=True)
    log = root / "-proj" / "s.jsonl"
    log.write_text("{}\n")
    log.chmod(0)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"sources": {"claude": [str(root)], "codex": [], "gemini": []}, "timezone": "UTC"}))
    try:
        result = ingest(tmp_path / "home", config=config, columns=80)
    finally:
        log.chmod(0o644)
    [warning] = [line for line in lines(result.stderr) if line.startswith("warning:")]
    assert f"claude: {log}: PermissionError" in warning  # the path and what went wrong with it, on the line the warning starts
    assert warning.count(str(log)) == 1 and warning.endswith("PermissionError: Permission denied")  # the OS error says it once


def test_a_damaged_database_is_error_2_here_as_everywhere(tmp_path: Path) -> None:
    (tmp_path / "agentfare.db").write_bytes(b"definitely not sqlite " * 100)
    result = ingest(tmp_path)
    assert result.exit_code == 2
    assert result.stderr.startswith("error: cannot use the database ") and "delete the file to start over" in result.stderr


def test_agent_and_full_options_still_work(tmp_path: Path) -> None:
    only = ingest(tmp_path, "--agent", "codex")
    assert lines(only.stdout)[1] == "1 session stored (codex: 1)"
    everything = ingest(tmp_path, "--full")
    assert lines(everything.stdout)[0] == READ_ALL
    assert ingest(tmp_path, "--agent", "nobody").exit_code == 2


@pytest.fixture
def unreadable(tmp_path: Path) -> Path:
    """A config whose Claude directory holds one log that cannot be read and one that can (the Claude fixture)."""
    root = tmp_path / "logs"
    (root / "-proj").mkdir(parents=True)
    bad = root / "-proj" / "bad.jsonl"
    bad.write_text("{}\n")
    bad.chmod(0)
    good = FIXTURES / "claude" / "projects" / "-home-dev-acme-shop" / "2c9f6d1e-7a4b-4e35-9b0d-58f3a1c7e402.jsonl"
    shutil.copy(good, root / "-proj" / good.name)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"sources": {"claude": [str(root)], "codex": [], "gemini": []}, "timezone": "UTC"}))
    yield config
    bad.chmod(0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read any file")
class TestFilesThatCannotBeRead:
    def test_the_last_line_says_how_many(self, tmp_path: Path, unreadable: Path) -> None:
        result = ingest(tmp_path / "home", config=unreadable)
        assert result.exit_code == 0, result.output  # one file the OS refuses is no reason to fail: the database is fine
        out = lines(result.stdout)
        assert out[-1] == "1 file could not be read (the warnings on stderr say which)"
        assert out[0].startswith("read 1 session record from 2 files (1 parsed, 0 unchanged)")
        assert sum(line.startswith("warning:") for line in lines(result.stderr)) == 1

    def test_two_files_in_the_plural(self, tmp_path: Path, unreadable: Path) -> None:
        (tmp_path / "logs" / "-proj" / "worse.jsonl").write_text("{}\n")
        (tmp_path / "logs" / "-proj" / "worse.jsonl").chmod(0)
        try:
            result = ingest(tmp_path / "home", config=unreadable)
        finally:
            (tmp_path / "logs" / "-proj" / "worse.jsonl").chmod(0o644)
        assert lines(result.stdout)[-1] == "2 files could not be read (the warnings on stderr say which)"
        assert "\n".join(lines(result.stderr)).count("PermissionError: Permission denied") == 2  # each once

    def test_a_clean_run_has_no_such_line(self, tmp_path: Path) -> None:
        assert not any("could not be read" in line for line in lines(ingest(tmp_path).stdout))

    def test_json_carries_the_errors_and_the_count(self, tmp_path: Path, unreadable: Path) -> None:
        result = ingest(tmp_path / "home", "--json", config=unreadable)
        assert result.exit_code == 0
        report = json.loads(result.stdout)
        [error] = report["errors"]
        assert error.endswith("PermissionError: Permission denied") and error.count("bad.jsonl") == 1
        assert report["files_failed"] == 1 and report["files_parsed"] == 1 and report["sessions_stored"] == 1

    def test_a_normal_command_names_the_file_once_as_well(self, tmp_path: Path, unreadable: Path) -> None:
        result = CliRunner().invoke(app, ["--config", str(unreadable), "--home", str(tmp_path / "home"), "summary", "--json"])
        assert result.exit_code == 0
        [warning] = [line for line in lines(result.stderr) if line.startswith("warning:")]
        assert warning.count("bad.jsonl") == 1 and "PermissionError: Permission denied" in warning


class TestTheSameSessionTwice:
    def test_the_count_is_said_on_stderr_and_in_the_json(self, tmp_path: Path, resumed: Path) -> None:
        result = ingest(tmp_path / "home", config=resumed)
        assert "warning: 1 session appears in more than one log file; the copy with the most tokens was kept" in lines(result.stderr)
        assert json.loads(ingest(tmp_path / "home2", "--json", config=resumed).stdout)["duplicate_sessions"] == 1

    def test_a_normal_command_says_it_too(self, tmp_path: Path, resumed: Path) -> None:
        result = CliRunner().invoke(app, ["--config", str(resumed), "--home", str(tmp_path / "home"), "summary", "--json"])
        assert "1 session appears in more than one log file" in result.stderr
        assert json.loads(result.stdout)["sessions"] == 1  # once in the numbers

    def test_nothing_to_say_without_a_copy(self, tmp_path: Path) -> None:
        assert "more than one log file" not in ingest(tmp_path).stderr


def test_the_agent_option_shows_its_value_and_the_json_help_is_not_about_money(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["ingest", "--help"], env={"COLUMNS": "100"})
    text = " ".join(result.stdout.split())
    assert "--agent -a AGENT" in text and "Only this agent: claude, codex, gemini." in text
    assert "integer cents" not in text and "TEXT" not in text
    assert "--json" in text and "Print what the run did as JSON" in text
