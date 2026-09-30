"""Names from the logs are bounded, and a long one neither slows a command down nor crashes a legacy console.

The names (a project, a session id, a model id) come from files the agents wrote, so one of a hundred thousand characters is
possible. They are capped when a record is stored, and drawn through a cut that costs no more for a long name than for a short one.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from agentfare.cli import app
from agentfare.db import Store
from agentfare.models import AGENTS, MAX_NAME_CHARS, SessionRecord, Usage, bounded, cap_name
from agentfare.render import Col, print_table

NOW = "2026-09-30T12:00:00Z"
runner = CliRunner()
HUGE = 100_000


def record(agent: str = "claude", session_id: str = "s1", project: str = "proj", model: str = "claude-sonnet-5-5", **extra: Any) -> SessionRecord:
    return SessionRecord(
        agent=agent, session_id=session_id, source_path=f"/logs/{agent}-{session_id[:20]}.jsonl", project=project,
        started_at="2026-09-29T10:00:00Z", ended_at="2026-09-29T11:00:00Z", wall_seconds=3600, active_seconds=3000,
        usage={model: Usage(input=1000, output=500)}, prompts=2, **extra,
    )


def config_for(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"timezone: UTC\ndatabase: {tmp_path / 'long.db'}\nsources: {{claude: [], codex: [], gemini: []}}\n")
    return cfg


def cli(tmp_path: Path, *args: str, columns: int = 80) -> Any:
    env = {"COLUMNS": str(columns), "AGENTFARE_NOW": NOW, "FORCE_COLOR": None, "NO_COLOR": None}
    return runner.invoke(app, ["--config", str(config_for(tmp_path)), "--no-ingest", *args], env=env)


class TestNamesAreCappedWhenStored:
    @pytest.mark.parametrize("agent", AGENTS)
    def test_every_agent_stores_its_long_names_at_the_cap(self, tmp_path: Path, agent: str) -> None:
        rec = record(agent, "s" * HUGE, "p" * HUGE, "m" * HUGE)
        with Store(tmp_path / "x.db") as store:
            store.upsert_session(rec)
            [got] = store.sessions()
        assert len(got.project) == len(got.session_id) == MAX_NAME_CHARS
        [model] = got.usage
        assert len(model) == MAX_NAME_CHARS and got.primary_model == model
        assert got.project.startswith("p" * 100) and got.session_id.startswith("s" * 100) and model.startswith("m" * 100)
        assert got.key == f"{agent}:{got.session_id}"

    def test_the_slots_of_a_long_model_follow_its_capped_name(self, tmp_path: Path) -> None:
        rec = record(model="m" * HUGE, slots={("2026-09-29T10:00:00Z", "m" * HUGE): Usage(input=1000, output=500)})
        with Store(tmp_path / "x.db") as store:
            store.upsert_session(rec)
            [got] = store.sessions()
        [(slot, model)] = got.slots
        assert model in got.usage and len(model) == MAX_NAME_CHARS

    def test_a_name_within_the_cap_is_stored_as_it_is(self) -> None:
        exact = "x" * MAX_NAME_CHARS
        assert cap_name(exact) == exact
        rec = record(session_id=exact, project=exact, model=exact)
        assert bounded(rec) is rec

    def test_two_long_names_that_share_their_start_stay_two_names(self) -> None:
        one, other = "a" * 500 + "1", "a" * 500 + "2"
        assert cap_name(one) != cap_name(other) and len(cap_name(one)) == MAX_NAME_CHARS

    def test_the_cap_is_idempotent_and_keeps_the_start(self) -> None:
        name = "claude-" + "x" * 5000
        capped = cap_name(name)
        assert cap_name(capped) == capped and capped.startswith("claude-xxxx") and "..." in capped[-12:]

    def test_the_same_long_session_is_one_row_however_often_it_is_stored(self, tmp_path: Path) -> None:
        with Store(tmp_path / "x.db") as store:
            for _ in range(2):
                store.upsert_session(record(session_id="s" * 1000))
            assert store.count() == 1

    def test_ingest_compares_copies_of_a_long_session_by_its_stored_key(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from agentfare import ingest as ingest_module
        from agentfare.config import Config
        from agentfare.config import deep_merge, DEFAULT_CONFIG

        class Parser:
            def discover(self, root: Path) -> list[Path]:
                return [root / "a.jsonl"]

            def signature(self, path: Path) -> str:
                return "sig"

            def parse(self, path: Path, *, idle_gap_seconds: int) -> list[SessionRecord]:
                return [record(session_id="s" * HUGE, project="p" * HUGE, model="m" * HUGE)]

        root = tmp_path / "logs"
        root.mkdir()
        cfg = Config(home=tmp_path, data=deep_merge(DEFAULT_CONFIG, {"sources": {"claude": [str(root)], "codex": [], "gemini": []}}), base_dir=tmp_path)
        monkeypatch.setattr(ingest_module, "get_parser", lambda agent: Parser())
        with Store(tmp_path / "x.db") as store:
            first = ingest_module.ingest(cfg, store, ["claude"])
            again = ingest_module.ingest(cfg, store, ["claude"], full=True)
            [got] = store.sessions()
        assert first.sessions == again.sessions == 1 and first.duplicates == again.duplicates == 0
        assert len(got.session_id) == len(got.project) == MAX_NAME_CHARS


def legacy_database(path: Path, name_length: int) -> None:
    """A database written before the cap, by raw SQL: four sessions whose names are ``name_length`` characters long."""
    with Store(path) as store:
        for i in range(4):
            key = f"claude:{'s' * name_length}{i}"
            store.conn.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, "claude", "s" * name_length + str(i), "p" * name_length + str(i), "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z",
                 3600, 3000, 3, 1, 0, 4, 2, "m" * name_length + str(i), "/x.jsonl", "2026-09-29T11:00:00Z"),
            )
            store.conn.execute(
                "INSERT INTO session_usage (key, model, slot, input, cache_read, cache_write, output, cache_write_1h) VALUES (?,?,?,?,?,?,?,?)",
                (key, "m" * name_length + str(i), "2026-09-29T10:00:00Z", 1000, 0, 0, 500, 0),
            )
        store.conn.commit()


class TestLongNamesAreFast:
    @pytest.mark.parametrize("command", ["summary", "sessions", "models", "advise"])
    def test_a_hundred_thousand_character_name_finishes_quickly(self, tmp_path: Path, command: str) -> None:
        with Store(tmp_path / "long.db") as store:
            for i in range(4):
                store.upsert_session(record(session_id=f"{'s' * HUGE}{i}", project=f"{'p' * HUGE}{i}", model=f"{'m' * HUGE}{i}"))
        started = time.perf_counter()
        result = cli(tmp_path, command)
        took = time.perf_counter() - started
        assert result.exit_code == 0, result.output
        assert took < 1.0, f"{command} took {took:.1f} s"
        assert max(len(line) for line in result.output.splitlines()) <= MAX_NAME_CHARS + 60  # names that differ only at their end keep the width that tells them apart

    @pytest.mark.parametrize("command", ["summary", "sessions", "models", "advise"])
    def test_a_database_that_already_holds_huge_names_is_drawn_quickly_too(self, tmp_path: Path, command: str) -> None:
        legacy_database(tmp_path / "long.db", HUGE)
        started = time.perf_counter()
        result = cli(tmp_path, command)
        took = time.perf_counter() - started
        assert result.exit_code == 0, result.output
        assert took < 1.0, f"{command} took {took:.1f} s"

    @pytest.mark.parametrize("command", ["summary", "sessions", "models", "advise"])
    def test_names_that_differ_only_in_the_middle_do_not_make_the_fit_slow(self, tmp_path: Path, command: str) -> None:
        with Store(tmp_path / "long.db") as store:
            for i in range(30):
                store.upsert_session(record(session_id=f"s{i}", project=f"{'p' * 99}{i}{'q' * 99}", model=f"{'m' * 99}{i}{'n' * 99}"))
        started = time.perf_counter()
        result = cli(tmp_path, command)
        assert result.exit_code == 0, result.output
        assert time.perf_counter() - started < 1.5

    def test_print_table_cuts_a_huge_cell_in_one_pass(self) -> None:
        console = Console(file=io.StringIO(), width=80, force_terminal=False)
        rows = [["m" * HUGE + str(i), "1"] for i in range(20)]
        started = time.perf_counter()
        print_table(console, [Col("Model", min_width=18, middle=True), Col("Sess", True)], rows)
        assert time.perf_counter() - started < 0.5
        assert all(len(line) <= 80 for line in console.file.getvalue().splitlines())  # type: ignore[attr-defined]


ENTRY = "from agentfare.cli import _entry; _entry()"


def cli_process(tmp_path: Path, *args: str, encoding: str = "ascii") -> subprocess.CompletedProcess[str]:
    """The real command line in its own process with stdout in ``encoding``: what a pipe or an old terminal gives it."""
    env = {**os.environ, "PYTHONIOENCODING": encoding, "COLUMNS": "80", "AGENTFARE_NOW": NOW, "AGENTFARE_HOME": str(tmp_path / "home")}
    return subprocess.run([sys.executable, "-c", ENTRY, "--config", str(config_for(tmp_path)), "--no-ingest", *args],
                          capture_output=True, text=True, env=env, timeout=60, check=False)


class TestWarningsOnAnAsciiConsole:
    """Rich cuts a word that is wider than its column with U+2026 (its default), which an ASCII console cannot write."""

    MODEL = "x" * 75

    @pytest.fixture
    def seeded(self, tmp_path: Path) -> Path:
        with Store(tmp_path / "long.db") as store:
            store.upsert_session(record(model=self.MODEL))
            store.upsert_session(record(session_id="s2", model="claude-sonnet-5-5"))
        return tmp_path

    @pytest.mark.parametrize("command", ["summary", "sessions", "models", "advise"])
    def test_a_long_model_in_the_warnings_does_not_crash(self, seeded: Path, command: str) -> None:
        done = cli_process(seeded, command)
        assert done.returncode == 0, done.stderr
        assert "Traceback" not in done.stderr and "warning:" in done.stdout
        assert self.MODEL[:40] in done.stdout  # the name is folded over lines, not cut with an ellipsis
        assert all(ord(ch) < 128 for ch in done.stdout)

    @pytest.mark.parametrize("command", ["summary", "sessions", "models", "advise"])
    def test_a_long_model_keeps_its_ellipsis_on_a_utf8_console(self, seeded: Path, command: str) -> None:
        done = cli_process(seeded, command, encoding="utf-8")
        assert done.returncode == 0 and "Traceback" not in done.stderr and "warning:" in done.stdout

    def test_run_shows_a_long_forced_model_on_an_ascii_console(self, tmp_path: Path) -> None:
        done = cli_process(tmp_path, "run", "--dry-run", "--force-model", self.MODEL, "--", "claude", "-p", "hi")
        assert done.returncode == 0, done.stderr
        assert "Traceback" not in done.stderr and all(ord(ch) < 128 for ch in done.stdout)

    def test_a_long_model_on_a_very_narrow_console(self, seeded: Path) -> None:
        env = {**os.environ, "PYTHONIOENCODING": "ascii", "COLUMNS": "30", "AGENTFARE_NOW": NOW, "AGENTFARE_HOME": str(seeded / "home")}
        for command in ("summary", "sessions", "models", "advise"):
            done = subprocess.run([sys.executable, "-c", ENTRY, "--config", str(config_for(seeded)), "--no-ingest", command],
                                  capture_output=True, text=True, env=env, timeout=60, check=False)
            assert done.returncode == 0 and "Traceback" not in done.stderr, (command, done.stderr[-300:])



# ---- files the user names: regular, and not huge -----------------------------------------------------------------------
def agentfare(tmp_path: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """The real command line in its own process, with a 20 s timeout: a read that never ends fails the test instead of hanging it."""
    full = {**os.environ, "AGENTFARE_HOME": str(tmp_path / "home"), "AGENTFARE_NOW": NOW, "COLUMNS": "100", **(env or {})}
    try:
        return subprocess.run([sys.executable, "-c", ENTRY, *args], capture_output=True, text=True, env=full, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail(f"still running after 20 s: {args}")


def make_file(tmp_path: Path, kind: str, size: int = 0) -> Path:
    path = tmp_path / f"{kind}.yaml"
    if kind == "fifo":
        os.mkfifo(path)
    elif kind == "dir":
        path.mkdir()
    elif kind == "big":
        with open(path, "wb") as fh:
            fh.truncate(size)  # a sparse file: the size is what counts, and it costs no disk
    else:
        pytest.fail(kind)
    return path


def one_line_error(done: subprocess.CompletedProcess[str], *words: str) -> None:
    assert done.returncode == 2, (done.returncode, done.stderr[-400:])
    assert "Traceback" not in done.stderr and "MemoryError" not in done.stderr
    lines = done.stderr.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("error: "), lines
    assert all(word in lines[0] for word in words), lines[0]


class TestFilesTheUserNames:
    @pytest.mark.parametrize("kind", ["fifo", "dir"])
    def test_a_config_that_is_not_a_regular_file_is_refused_with_one_line(self, tmp_path: Path, kind: str) -> None:
        path = make_file(tmp_path, kind)
        one_line_error(agentfare(tmp_path, "--config", str(path), "config"), "cannot read", "not a regular file")

    def test_a_config_that_is_a_device_is_refused(self, tmp_path: Path) -> None:
        one_line_error(agentfare(tmp_path, "--config", "/dev/zero", "summary"), "cannot read /dev/zero", "not a regular file")

    def test_a_config_from_the_environment_is_checked_the_same_way(self, tmp_path: Path) -> None:
        one_line_error(agentfare(tmp_path, "config", env={"AGENTFARE_CONFIG": "/dev/zero"}), "not a regular file")
        one_line_error(agentfare(tmp_path, "config", env={"AGENTFARE_CONFIG": str(make_file(tmp_path, "fifo"))}), "not a regular file")

    def test_a_config_over_ten_megabytes_is_refused_without_being_read(self, tmp_path: Path) -> None:
        big = make_file(tmp_path, "big", 10 * 2**20 + 1)
        one_line_error(agentfare(tmp_path, "--config", str(big), "config"), "cannot read", "larger than 10 MB")

    @pytest.mark.parametrize("kind", ["fifo", "dir"])
    def test_a_pricing_file_named_in_the_config_is_checked(self, tmp_path: Path, kind: str) -> None:
        path = make_file(tmp_path, kind)
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"pricing_file: {path}\n")
        one_line_error(agentfare(tmp_path, "--config", str(cfg), "--no-ingest", "prices"), "cannot read", "not a regular file")

    def test_a_pricing_file_over_ten_megabytes_is_refused(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text(f"pricing_file: {make_file(tmp_path, 'big', 10 * 2**20 + 1)}\n")
        one_line_error(agentfare(tmp_path, "--config", str(cfg), "--no-ingest", "prices"), "larger than 10 MB")

    def test_the_implicit_price_table_in_the_data_directory_is_checked_too(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        os.mkfifo(home / "pricing.yaml")
        one_line_error(agentfare(tmp_path, "--no-ingest", "prices"), "cannot read", "not a regular file")

    @pytest.mark.parametrize("kind", ["fifo", "dir"])
    def test_a_prompt_file_that_is_not_a_regular_file_is_refused(self, tmp_path: Path, kind: str) -> None:
        one_line_error(agentfare(tmp_path, "--no-ingest", "run", "--dry-run", "--prompt-file", str(make_file(tmp_path, kind)), "--", "claude"),
                       "cannot read", "--prompt-file", "not a regular file")

    def test_a_prompt_file_that_is_a_device_is_refused(self, tmp_path: Path) -> None:
        one_line_error(agentfare(tmp_path, "--no-ingest", "run", "--dry-run", "--prompt-file", "/dev/zero", "--", "claude"), "not a regular file")

    def test_a_prompt_file_over_two_megabytes_is_refused_and_one_at_the_limit_is_read(self, tmp_path: Path) -> None:
        one_line_error(agentfare(tmp_path, "--no-ingest", "run", "--dry-run", "--prompt-file", str(make_file(tmp_path, "big", 2 * 2**20 + 1)), "--", "claude"),
                       "larger than 2 MB")
        exact = tmp_path / "exact.txt"
        exact.write_text("a" * (2 * 2**20))
        done = agentfare(tmp_path, "--no-ingest", "run", "--dry-run", "--json", "--prompt-file", str(exact), "--", "claude")
        assert done.returncode == 0, done.stderr

    def test_a_missing_prompt_file_is_the_same_kind_of_line(self, tmp_path: Path) -> None:
        one_line_error(agentfare(tmp_path, "--no-ingest", "run", "--dry-run", "--prompt-file", str(tmp_path / "nope.txt"), "--", "claude"),
                       "cannot read", "No such file")

    def test_the_reader_itself(self, tmp_path: Path) -> None:
        from agentfare.safeio import NotReadableError, read_text_limited

        path = tmp_path / "f.txt"
        path.write_text("héllo\r\nworld\n", encoding="utf-8")
        assert read_text_limited(path, 100) == "héllo\nworld\n"  # universal newlines, like Path.read_text
        path.write_text("a" * 100)
        assert read_text_limited(path, 100) == "a" * 100
        path.write_text("a" * 101)
        with pytest.raises(NotReadableError, match="larger than"):
            read_text_limited(path, 100)
        path.write_bytes(b"\xff\xfe")
        with pytest.raises(UnicodeDecodeError):
            read_text_limited(path, 100)
        assert read_text_limited(path, 100, errors="replace") == "��"
        with pytest.raises(FileNotFoundError):
            read_text_limited(tmp_path / "missing", 100)
        with pytest.raises(NotReadableError, match="not a regular file"):
            read_text_limited(tmp_path, 100)
        os.mkfifo(tmp_path / "pipe")
        with pytest.raises(NotReadableError, match="not a regular file"):
            read_text_limited(tmp_path / "pipe", 100)  # returns at once, though nobody writes to it

    def test_a_symbolic_link_to_a_regular_file_is_read(self, tmp_path: Path) -> None:
        real = tmp_path / "real.yaml"
        real.write_text("timezone: UTC\n")
        link = tmp_path / "link.yaml"
        link.symlink_to(real)
        done = agentfare(tmp_path, "--config", str(link), "--no-ingest", "config")
        assert done.returncode == 0, done.stderr


# ---- --no-ingest on a database nobody may write to ---------------------------------------------------------------------
class TestReadOnlyDatabase:
    @pytest.fixture
    def seeded(self, tmp_path: Path) -> Path:
        with Store(tmp_path / "long.db") as store:
            for i in range(3):
                store.upsert_session(record(session_id=f"s{i}"))
        return tmp_path

    @staticmethod
    def read_only_world(monkeypatch: pytest.MonkeyPatch) -> None:
        """What a read-only mount (or a file the user may not write) does to the normal opening: ``Store(path)`` fails."""
        import errno

        from agentfare import db

        def refuse(*args: Any, **kwargs: Any) -> None:
            raise OSError(errno.EROFS, "Read-only file system")

        monkeypatch.setattr(db, "touch_private", refuse)

    @staticmethod
    def untouched(path: Path) -> tuple[bytes, int]:
        return path.read_bytes(), path.stat().st_mtime_ns

    @pytest.mark.parametrize("command", ["summary", "sessions", "models", "advise", "report"])
    def test_no_ingest_reads_a_database_that_cannot_be_written(self, seeded: Path, monkeypatch: pytest.MonkeyPatch, command: str) -> None:
        db = seeded / "long.db"
        before = self.untouched(db)
        self.read_only_world(monkeypatch)
        result = cli(seeded, command, *(["-o", "-"] if command == "report" else []))
        assert result.exit_code == 0, result.output
        assert self.untouched(db) == before  # not a byte written, the version stamp included
        assert sorted(p.name for p in seeded.iterdir()) == ["config.yaml", "long.db"]  # no journal left behind either

    def test_the_numbers_are_the_same_as_from_a_writable_database(self, seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        normal = cli(seeded, "summary", "--json")
        self.read_only_world(monkeypatch)
        assert cli(seeded, "summary", "--json").stdout == normal.stdout and normal.exit_code == 0

    def test_a_real_read_only_file_in_a_read_only_directory_works_too(self, seeded: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root can write anywhere")
        db = seeded / "long.db"
        cfg = config_for(seeded)
        before = self.untouched(db)
        db.chmod(0o444)
        seeded.chmod(0o555)
        try:
            result = runner.invoke(app, ["--config", str(cfg), "--no-ingest", "summary", "--json"], env={"AGENTFARE_NOW": NOW})
        finally:
            seeded.chmod(0o755)
        assert result.exit_code == 0, result.output
        assert self.untouched(db) == before

    def test_without_no_ingest_the_normal_path_is_unchanged(self, seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.read_only_world(monkeypatch)
        result = runner.invoke(app, ["--config", str(config_for(seeded)), "summary"], env={"AGENTFARE_NOW": NOW})
        assert result.exit_code == 2 and "error: cannot use the database" in result.stderr

    def test_ingest_itself_still_opens_the_database_for_writing(self, seeded: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.read_only_world(monkeypatch)
        result = runner.invoke(app, ["--config", str(config_for(seeded)), "--no-ingest", "ingest"], env={"AGENTFARE_NOW": NOW})
        assert result.exit_code == 2 and "error: cannot use the database" in result.stderr

    def test_a_missing_database_is_still_created_by_the_normal_path(self, tmp_path: Path) -> None:
        result = cli(tmp_path, "summary")
        assert result.exit_code == 0 and (tmp_path / "long.db").is_file()

    def test_an_older_database_is_still_upgraded_by_the_normal_path(self, seeded: Path) -> None:
        import sqlite3

        db = seeded / "long.db"
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA user_version = 2")
        assert cli(seeded, "summary").exit_code == 0
        with sqlite3.connect(db) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3

    def test_a_newer_database_and_a_damaged_one_are_reported_as_before(self, seeded: Path) -> None:
        import sqlite3

        db = seeded / "long.db"
        with sqlite3.connect(db) as conn:
            conn.execute("PRAGMA user_version = 99")
        result = cli(seeded, "summary")
        assert result.exit_code == 2 and "written by a newer agentfare" in result.stderr
        db.write_bytes(b"this is not a database" * 100)
        result = cli(seeded, "summary")
        assert result.exit_code == 2 and "delete the file to start over" in result.stderr
