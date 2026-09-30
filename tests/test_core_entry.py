"""``agentfare._entry``: setup problems are one line on stderr and exit code 2, never a traceback."""

from __future__ import annotations

import errno
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentfare import cli
from agentfare.cli import app
from agentfare.config import ConfigError
from agentfare.db import Store, StoreError
from agentfare.pricing import PricingError


def run_entry(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    monkeypatch.setattr(sys, "argv", ["agentfare", *argv])
    with pytest.raises(SystemExit) as exit_info:
        cli._entry()
    captured = capsys.readouterr()
    return exit_info.value.code, captured.out, captured.err


@pytest.mark.parametrize("error", [ConfigError("bad config"), PricingError("bad prices"), StoreError("bad database")], ids=lambda error: type(error).__name__)
def test_setup_errors_are_printed_as_one_line_with_exit_code_2(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], error: ValueError) -> None:
    def raising() -> None:
        raise error

    monkeypatch.setattr(cli, "app", raising)
    code, out, err = run_entry(monkeypatch, capsys)
    assert (code, out, err) == (2, "", f"error: {error}\n")


def test_other_exceptions_are_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def raising() -> None:
        raise RuntimeError("a real bug")

    monkeypatch.setattr(cli, "app", raising)
    with pytest.raises(RuntimeError, match="a real bug"):
        cli._entry()


def test_a_home_that_cannot_be_created_ends_in_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the data directory should be")
    code, _, err = run_entry(monkeypatch, capsys, "--home", str(blocker / "data"), "ingest")
    assert code == 2
    assert err.startswith("error: cannot use the database ") and err.count("\n") == 1
    assert "Traceback" not in err and "\x1b" not in err


def test_a_corrupt_database_ends_in_a_clear_error_and_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "agentfare.db").write_bytes(b"definitely not sqlite " * 100)
    code, _, err = run_entry(monkeypatch, capsys, "--home", str(tmp_path), "ingest")
    assert code == 2
    assert err.startswith("error: cannot use the database ") and "delete the file" in err and "Traceback" not in err
    assert (tmp_path / "agentfare.db").read_bytes() == b"definitely not sqlite " * 100


def test_a_missing_config_is_still_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run_entry(monkeypatch, capsys, "--config", str(tmp_path / "nope.yaml"), "--home", str(tmp_path), "ingest")
    assert code == 2 and err.startswith("error: config file not found")


def test_a_healthy_run_is_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    fixtures = Path(__file__).parent / "fixtures"
    code, out, _ = run_entry(monkeypatch, capsys, "--config", str(fixtures / "config.yaml"), "--home", str(tmp_path), "ingest")
    assert code == 0 and "sessions stored" in out


# ---- a damaged database is one clean error, whichever way it surfaces ------------------------------------------------
FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "config.yaml"


def damaged_home(home: Path) -> None:
    """A database that opens (header and schema page intact) but whose data pages are garbage."""
    with Store(home / "agentfare.db") as store:
        store.mark_file("/logs/a", "claude", "sig", 1)
    db = home / "agentfare.db"
    data = bytearray(db.read_bytes())
    data[4096:] = b"\xab" * (len(data) - 4096)
    db.write_bytes(bytes(data))


@pytest.mark.parametrize("command", ["summary", "sessions", "models", "advise", "report", "ingest"])
def test_a_database_that_is_damaged_after_it_opens_is_error_2_for_every_command(tmp_path: Path, command: str) -> None:
    damaged_home(tmp_path)
    result = CliRunner().invoke(app, ["--config", str(FIXTURE_CONFIG), "--home", str(tmp_path), *([] if command == "ingest" else ["--no-ingest"]), command])
    assert result.exit_code == 2, result.output
    assert result.stderr.startswith("error: cannot use the database ") and "delete the file to start over" in result.stderr
    assert result.stderr.count("\n") == 1 and "Traceback" not in result.output


def test_the_first_ingest_of_a_damaged_database_is_one_error_not_one_warning_per_log_file(tmp_path: Path) -> None:
    damaged_home(tmp_path)
    result = CliRunner().invoke(app, ["--config", str(FIXTURE_CONFIG), "--home", str(tmp_path), "summary"])
    assert result.exit_code == 2
    assert result.stderr.startswith("error: cannot use the database ") and "could not parse" not in result.stderr


def test_both_ways_of_ending_present_a_store_error_alike(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """``clean_errors`` (inside a command) and ``_entry`` (for what no command caught) say the same thing and exit 2."""
    damaged_home(tmp_path)
    code, out, err = run_entry(monkeypatch, capsys, "--home", str(tmp_path), "--no-ingest", "summary")  # a command with clean_errors
    assert (code, out) == (2, "") and err.startswith("error: cannot use the database ") and err.count("\n") == 1

    def raising() -> None:
        raise StoreError(err.removeprefix("error: ").rstrip("\n"))

    monkeypatch.setattr(cli, "app", raising)
    assert run_entry(monkeypatch, capsys) == (2, "", err)  # ... and one that reached _entry


# ---- a reader that stops early is not an error --------------------------------------------------------------------
class FakeStdout:
    def fileno(self) -> int:
        return 99

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        pass


class FakeOs:
    """The three calls that point a descriptor at /dev/null, recorded instead of done (they would redirect pytest's own output)."""

    devnull = "/dev/null"
    O_WRONLY = 1

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def open(self, path: str, flags: int) -> int:
        self.calls.append(("open", path))
        return 7

    def dup2(self, source: int, target: int) -> None:
        self.calls.append(("dup2", source, target))

    def close(self, fd: int) -> None:
        self.calls.append(("close", fd))


@pytest.fixture
def fake_os(monkeypatch: pytest.MonkeyPatch) -> FakeOs:
    """``cli`` sees the recording fake in place of ``os``."""
    fake = FakeOs()
    monkeypatch.setattr(cli, "os", fake)
    return fake


def stdout_with_descriptor_99(monkeypatch: pytest.MonkeyPatch) -> None:
    """Called inside the test: capsys puts its own stdout back when the test body starts, over one set by a fixture."""
    monkeypatch.setattr(sys, "stdout", FakeStdout())


DEVNULL_OVER_STDOUT = [("open", "/dev/null"), ("dup2", 7, 99), ("close", 7)]


def test_a_broken_pipe_ends_quietly_with_exit_code_0_and_points_stdout_at_devnull(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_os: FakeOs
) -> None:
    def raising() -> None:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    stdout_with_descriptor_99(monkeypatch)
    monkeypatch.setattr(cli, "app", raising)
    with pytest.raises(SystemExit) as exit_info:
        cli._entry()
    assert exit_info.value.code == 0
    assert fake_os.calls == DEVNULL_OVER_STDOUT  # what is left of stdout goes nowhere, so the flush at interpreter exit cannot fail either
    assert capsys.readouterr().err == ""


def test_the_broken_pipe_click_has_already_turned_into_exit_1_is_exit_0_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fake_os: FakeOs
) -> None:
    """Click catches EPIPE itself and calls ``sys.exit(1)``: the exit carries the BrokenPipeError as its context."""

    def like_click() -> None:
        try:
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
        except OSError:
            sys.exit(1)

    stdout_with_descriptor_99(monkeypatch)
    monkeypatch.setattr(cli, "app", like_click)
    with pytest.raises(SystemExit) as exit_info:
        cli._entry()
    assert exit_info.value.code == 0 and fake_os.calls == DEVNULL_OVER_STDOUT
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_other_exits_keep_their_codes(monkeypatch: pytest.MonkeyPatch, fake_os: FakeOs, code: int) -> None:
    def leaving() -> None:
        raise SystemExit(code)

    stdout_with_descriptor_99(monkeypatch)
    monkeypatch.setattr(cli, "app", leaving)
    with pytest.raises(SystemExit) as exit_info:
        cli._entry()
    assert exit_info.value.code == code and fake_os.calls == []


def test_an_exit_after_some_other_os_error_is_not_taken_for_a_closed_pipe(monkeypatch: pytest.MonkeyPatch, fake_os: FakeOs) -> None:
    def failing() -> None:
        try:
            raise PermissionError(errno.EACCES, "denied")
        except OSError:
            sys.exit(1)

    stdout_with_descriptor_99(monkeypatch)
    monkeypatch.setattr(cli, "app", failing)
    with pytest.raises(SystemExit) as exit_info:
        cli._entry()
    assert exit_info.value.code == 1 and fake_os.calls == []


def test_stdout_without_a_file_descriptor_is_no_obstacle(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def raising() -> None:
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    monkeypatch.setattr(cli, "app", raising)
    with pytest.raises(SystemExit) as exit_info:  # capsys' stdout has no fileno()
        cli._entry()
    assert exit_info.value.code == 0


PIPE_SCRIPT = "import sys\nsys.stdin.readline()\nfrom agentfare.cli import _entry\n_entry()\n"


@pytest.mark.parametrize(
    "command",
    [
        ["summary"], ["summary", "--json"], ["sessions"], ["sessions", "--json"], ["models"], ["advise"], ["advise", "--json"],
        ["report"], ["report", "--json"], ["config"], ["prices"], ["prices", "--json"], ["ingest"],
        ["run", "--dry-run", "--", "claude", "-p", "fix a typo"],
    ],
    ids=lambda command: " ".join(command[:3]),
)
def test_a_reader_that_stops_early_ends_every_command_quietly_with_0(tmp_path: Path, command: list[str]) -> None:
    """``agentfare advise --all | head -1``, for real: the pipe is closed before the command writes anything."""
    argv = [sys.executable, "-c", PIPE_SCRIPT, "--config", str(FIXTURE_CONFIG), "--home", str(tmp_path), "--no-ingest", *command]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp_path)
    assert proc.stdout is not None
    proc.stdout.close()  # nobody is reading any more
    try:
        _, error = proc.communicate(b"go\n", timeout=60)  # the command starts only now, into a pipe with no reader
    finally:
        proc.kill()
    assert proc.returncode == 0
    assert error.decode() in ("", "first run: reading your agent logs into a local database; this can take a minute\n")  # `ingest` says that much


@pytest.mark.parametrize(
    "command",
    [
        ["summary"], ["sessions"], ["models"], ["advise"], ["report"], ["ingest"], ["--no-ingest", "summary"],
        ["run", "--dry-run", "--", "claude", "-p", "fix a typo"],
    ],
    ids=lambda command: " ".join(command[:3]),
)
def test_the_database_is_closed_before_the_process_ends(tmp_path: Path, command: list[str]) -> None:
    """A connection left to the garbage collector is a ResourceWarning, which Python's development mode (``-X dev``) prints."""
    argv = [sys.executable, "-X", "dev", "-m", "agentfare", "--config", str(FIXTURE_CONFIG), "--home", str(tmp_path), *command]
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60, cwd=tmp_path, stdin=subprocess.DEVNULL)
    assert done.returncode == 0
    assert "ResourceWarning" not in done.stderr


# ---- options that belong before the command, and -h ------------------------------------------------------------------
def flat(text: str) -> str:
    """The text of a Rich error panel as one line: the frame and the line breaks taken out."""
    return " ".join("".join(" " if ch in "│╭╮╰╯─" else ch for ch in text).split())


@pytest.mark.parametrize("command", ["summary", "sessions", "models", "advise", "report", "ingest", "prices", "config"])
def test_a_global_option_after_the_command_says_where_it_goes(command: str, tmp_path: Path) -> None:
    result = CliRunner().invoke(app, [command, "--no-ingest"], env={"COLUMNS": "100"})
    assert result.exit_code == 2
    assert f"No such option: --no-ingest (global options go before the command: agentfare --no-ingest {command})" in flat(result.stderr)
    assert "Possible options" not in result.stderr and "Traceback" not in result.output


def test_the_other_global_options_are_told_the_same(tmp_path: Path) -> None:
    for option, example in (("--home", "--home DIR summary"), ("--config", "--config FILE summary")):
        result = CliRunner().invoke(app, ["summary", option, str(tmp_path)], env={"COLUMNS": "100"})
        assert result.exit_code == 2
        assert f"No such option: {option} (global options go before the command: agentfare {example})" in flat(result.stderr)


def test_the_advice_is_not_given_for_an_option_nobody_has() -> None:
    result = CliRunner().invoke(app, ["summary", "--nope"], env={"COLUMNS": "100"})
    assert result.exit_code == 2 and "No such option: --nope" in result.stderr and "global options" not in result.stderr
    before = CliRunner().invoke(app, ["--nope", "summary"], env={"COLUMNS": "100"})
    assert before.exit_code == 2 and "global options" not in before.stderr


def test_the_suggested_spelling_works(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--home", str(tmp_path), "--no-ingest", "summary"])
    assert result.exit_code == 0 and result.stdout.startswith("no sessions in this period")


@pytest.mark.parametrize("command", [[], ["summary"], ["sessions"], ["models"], ["advise"], ["report"], ["ingest"], ["prices"], ["config"], ["run"]])
def test_h_is_short_for_help(command: list[str]) -> None:
    short = CliRunner().invoke(app, [*command, "-h"], env={"COLUMNS": "100"})
    long = CliRunner().invoke(app, [*command, "--help"], env={"COLUMNS": "100"})
    assert short.exit_code == long.exit_code == 0
    assert short.stdout == long.stdout and "Usage: agentfare" in short.stdout
    assert re.search(r"--help\s+-h\s", flat(short.stdout))  # and the help says so
