import io
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from rich.console import Console

from agentfare import state as state_module
from agentfare.config import ConfigError
from agentfare.ingest import IngestReport
from agentfare.db import Store
from agentfare.models import SessionRecord
from agentfare.pricing import builtin_path
from agentfare.state import SLOW_AFTER_SECONDS, AppState


class Consoles:
    def __init__(self) -> None:
        self.out_buf, self.err_buf = io.StringIO(), io.StringIO()
        self.out = Console(file=self.out_buf, width=200, force_terminal=False)
        self.err = Console(file=self.err_buf, width=200, force_terminal=False)


@pytest.fixture
def consoles() -> Consoles:
    return Consoles()


@pytest.fixture
def ingest_calls(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[IngestReport]]:
    """Replaces the real ingestion with one that records its calls and returns a canned report."""
    calls: list[IngestReport] = []

    def install(report: IngestReport) -> list[IngestReport]:
        def fake_ingest(cfg: object, store: object, *args: object, **kwargs: object) -> IngestReport:
            calls.append(report)
            return report

        monkeypatch.setattr(state_module, "ingest", fake_ingest)
        return calls

    return install


@pytest.fixture
def make_state(tmp_path: Path, consoles: Consoles) -> Iterator[Callable[..., AppState]]:
    made: list[AppState] = []

    def make(home: Path | None = None, **kw: object) -> AppState:
        st = AppState(home=home or tmp_path / "home", out=consoles.out, err=consoles.err, **kw)  # type: ignore[arg-type]
        made.append(st)
        return st

    yield make
    for st in made:
        if st._store is not None:
            st._store.close()


class TestLazyObjects:
    def test_config_is_loaded_once_from_the_given_home(self, tmp_path: Path, make_state: Callable[..., AppState]) -> None:
        st = make_state()
        assert st.cfg is st.cfg
        assert st.cfg.home == tmp_path / "home"

    def test_an_explicit_config_file_is_used(self, tmp_path: Path) -> None:
        cfg_file = tmp_path / "custom.yaml"
        cfg_file.write_text("database: elsewhere.db\n", encoding="utf-8")
        st = AppState(config_path=cfg_file, home=tmp_path / "home")
        assert st.cfg.db_path == tmp_path / "elsewhere.db"

    def test_a_missing_explicit_config_surfaces_when_first_used(self, tmp_path: Path) -> None:
        st = AppState(config_path=tmp_path / "nope.yaml")  # constructing is free
        with pytest.raises(ConfigError):
            st.cfg

    def test_price_table_is_loaded_once(self, make_state: Callable[..., AppState]) -> None:
        st = make_state()
        assert st.table is st.table
        assert st.table.path == builtin_path()

    def test_price_table_honours_the_override_next_to_the_data(self, tmp_path: Path, make_state: Callable[..., AppState]) -> None:
        (tmp_path / "home").mkdir()
        (tmp_path / "home" / "pricing.yaml").write_text("models:\n  mine: {input: 1, output: 2}\n", encoding="utf-8")
        assert list(make_state().table.models) == ["mine"]


class TestStore:
    def test_the_database_is_created_at_the_configured_path_and_cached(
        self, tmp_path: Path, make_state: Callable[..., AppState]
    ) -> None:
        st = make_state(auto_ingest=False)
        assert st.store() is st.store()
        assert (tmp_path / "home" / "agentfare.db").is_file()

    def test_new_logs_are_ingested_first_by_default(
        self, consoles: Consoles, make_state: Callable[..., AppState], ingest_calls
    ) -> None:
        calls = ingest_calls(IngestReport(sessions=3))
        st = make_state()
        st.store()
        st.store()
        assert len(calls) == 1  # only when the store is first opened
        assert st.last_ingest is calls[0]
        assert "ingested 3 new/updated sessions" in consoles.err_buf.getvalue()
        assert consoles.out_buf.getvalue() == ""  # progress goes to stderr, keeping stdout clean for data

    def test_nothing_new_prints_nothing(self, consoles: Consoles, make_state: Callable[..., AppState], ingest_calls) -> None:
        ingest_calls(IngestReport(sessions=0))
        make_state().store()
        assert consoles.err_buf.getvalue() == ""

    def test_no_ingest_flag(self, make_state: Callable[..., AppState], ingest_calls) -> None:
        calls = ingest_calls(IngestReport(sessions=1))
        st = make_state(auto_ingest=False)
        st.store()
        assert calls == [] and st.last_ingest is None

    def test_ingest_first_overrides_the_default_either_way(
        self, tmp_path: Path, make_state: Callable[..., AppState], ingest_calls
    ) -> None:
        calls = ingest_calls(IngestReport())
        make_state(auto_ingest=False).store(ingest_first=True)
        assert len(calls) == 1
        make_state(tmp_path / "other").store(ingest_first=False)
        assert len(calls) == 1

    def test_at_most_five_errors_are_shown_then_a_count(
        self, consoles: Consoles, make_state: Callable[..., AppState], ingest_calls
    ) -> None:
        ingest_calls(IngestReport(errors=[f"claude: /logs/{i}.jsonl: ValueError: bad" for i in range(7)]))
        make_state().store()
        text = consoles.err_buf.getvalue()
        assert text.count("could not parse") == 5
        assert "/logs/4.jsonl" in text and "/logs/5.jsonl" not in text
        assert "and 2 more unreadable files" in text

    def test_error_text_is_shown_literally_even_when_it_looks_like_markup(
        self, consoles: Consoles, make_state: Callable[..., AppState], ingest_calls
    ) -> None:
        nasty = "claude: /home/dev/notes [backup]/a.jsonl: ValueError: unexpected [/closing] and [bold]tags[/bold]"
        ingest_calls(IngestReport(errors=[nasty]))
        make_state().store()  # must not raise rich.errors.MarkupError
        assert nasty in consoles.err_buf.getvalue()


class TestClose:
    def test_the_database_the_state_opened_is_closed_and_asked_for_again_it_is_opened_anew(self, make_state: Callable[..., AppState]) -> None:
        st = make_state(auto_ingest=False)
        store = st.store()
        st.close()
        with pytest.raises(sqlite3.ProgrammingError):  # Cannot operate on a closed database
            store.count()
        assert st.store() is not store and st.store().count() == 0

    def test_closing_a_state_that_never_opened_the_database_is_fine(self, make_state: Callable[..., AppState]) -> None:
        st = make_state()
        st.close()
        st.close()
        assert st._store is None


class TestFailedOpen:
    def test_a_failed_first_ingest_does_not_leave_a_store_that_skips_the_ingest_next_time(
        self, tmp_path: Path, make_state: Callable[..., AppState]
    ) -> None:
        cfg_file = tmp_path / "bad.yaml"
        cfg_file.write_text("sources: [a, b]\n", encoding="utf-8")  # every agent needs its own list
        st = make_state(config_path=cfg_file)
        with pytest.raises(ConfigError):
            st.store()
        assert st._store is None
        with pytest.raises(ConfigError):  # tried again, not quietly served an un-ingested database
            st.store()


FIRST_RUN = "first run: reading your agent logs into a local database; this can take a minute"


@pytest.fixture
def scan(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Replaces ingestion with one that reports each of these (agent, path) files to ``on_file`` as it starts parsing it,
    while a fake clock moves ``seconds_per_file`` on at every file."""

    def install(files: list[tuple[str, str]], *, seconds_per_file: float = 0.0) -> None:
        now = [1_000.0]
        monkeypatch.setattr(state_module, "monotonic", lambda: now[0])

        def fake_ingest(cfg: object, store: object, *args: object, on_file: Callable[[str, str], None], **kwargs: object) -> IngestReport:
            for agent, path in files:
                on_file(agent, path)
                now[0] += seconds_per_file
            return IngestReport(sessions=len(files))

        monkeypatch.setattr(state_module, "ingest", fake_ingest)

    return install


class TestFirstRun:
    """A new, empty database that has logs to read says so once, before the slow part, on stderr and in plain text."""

    FILES = [("claude", "/logs/a.jsonl"), ("claude", "/logs/b.jsonl")]

    def test_the_first_run_says_what_it_is_doing_before_it_starts(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan(self.FILES)
        make_state().store()
        lines = consoles.err_buf.getvalue().splitlines()
        assert lines == [FIRST_RUN, "ingested 2 new/updated sessions"]  # said once, ahead of the result
        assert consoles.out_buf.getvalue() == ""

    def test_it_is_dim_text_with_no_escape_codes_when_output_is_not_a_terminal(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan(self.FILES)
        make_state().store()
        assert "\x1b" not in consoles.err_buf.getvalue()

    def test_it_is_dim_on_a_terminal(self, tmp_path: Path, scan: Callable[..., None]) -> None:
        scan(self.FILES)
        buffer = io.StringIO()
        st = AppState(home=tmp_path / "home", err=Console(file=buffer, width=200, force_terminal=True, color_system="standard"))
        try:
            st.store()
        finally:
            st.store().close()
        assert f"\x1b[2m{FIRST_RUN}\x1b[0m" in buffer.getvalue()

    def test_a_database_that_already_has_sessions_says_nothing_of_the_kind(
        self, tmp_path: Path, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None], make_record: Callable[..., SessionRecord]
    ) -> None:
        with Store(tmp_path / "home" / "agentfare.db") as existing:
            existing.upsert_session(make_record("old"))
        scan(self.FILES)
        make_state().store()
        assert FIRST_RUN not in consoles.err_buf.getvalue()

    def test_an_empty_database_with_nothing_to_read_says_nothing(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan([])
        make_state().store()
        assert consoles.err_buf.getvalue() == ""  # no logs at all: not a 'first run' worth announcing on every command

    def test_no_ingest_means_no_first_run_line(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan(self.FILES)
        make_state(auto_ingest=False).store()
        assert consoles.err_buf.getvalue() == ""

    def test_it_is_said_once_however_many_times_the_store_is_asked_for(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan(self.FILES)
        st = make_state()
        st.store(), st.store()
        assert consoles.err_buf.getvalue().count(FIRST_RUN) == 1


class TestSlowRunProgress:
    """An agent whose own scan has taken more than a couple of seconds gets one line: never more, never for a quick agent."""

    FILES = [("claude", f"/logs/c{i}") for i in range(4)] + [("codex", f"/logs/x{i}") for i in range(4)]

    def lines(self, consoles: Consoles) -> list[str]:
        return [line for line in consoles.err_buf.getvalue().splitlines() if "still reading" in line]

    def test_a_quick_run_has_no_progress_lines(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan(self.FILES, seconds_per_file=0.1)  # 0.8 seconds in all
        make_state().store()
        assert self.lines(consoles) == []

    def test_a_slow_run_says_one_line_per_agent(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan(self.FILES, seconds_per_file=1.0)
        make_state().store()
        assert self.lines(consoles) == ["still reading claude logs...", "still reading codex logs..."]

    def test_an_agent_is_named_when_the_slowness_shows_not_before(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        """The first three files of an agent start within its first two seconds: each agent is named at its fourth file."""
        scan(self.FILES, seconds_per_file=1.0)
        make_state().store()
        text = consoles.err_buf.getvalue()
        assert text.index(FIRST_RUN) < text.index("still reading claude") < text.index("still reading codex") < text.index("ingested")

    def test_an_agent_that_is_quick_is_not_named_however_long_the_agents_before_it_took(
        self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]
    ) -> None:
        """Deliberate: each agent has its own clock, which starts at its first file. The whole scan being slow says nothing about it."""
        scan([("claude", "/logs/c0"), ("claude", "/logs/c1"), ("gemini", "/logs/g0"), ("gemini", "/logs/g1")], seconds_per_file=SLOW_AFTER_SECONDS + 1)
        make_state().store()
        assert self.lines(consoles) == ["still reading claude logs...", "still reading gemini logs..."]  # each at its own second file

    def test_a_single_quick_file_of_a_late_agent_says_nothing(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan([("claude", "/logs/c0"), ("claude", "/logs/c1"), ("gemini", "/logs/g0")], seconds_per_file=SLOW_AFTER_SECONDS + 1)
        make_state().store()
        assert self.lines(consoles) == ["still reading claude logs..."]  # gemini has had no time of its own yet

    def test_no_escape_codes_and_nothing_on_stdout(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan(self.FILES, seconds_per_file=1.0)
        make_state().store()
        assert "\x1b" not in consoles.err_buf.getvalue() and consoles.out_buf.getvalue() == ""

    def test_the_explicit_ingest_reports_progress_the_same_way(self, consoles: Consoles, make_state: Callable[..., AppState], scan: Callable[..., None]) -> None:
        scan(self.FILES, seconds_per_file=1.0)
        st = make_state()
        st.run_ingest(st.store(ingest_first=False), ["claude"], full=True)
        assert FIRST_RUN in consoles.err_buf.getvalue() and len(self.lines(consoles)) == 2


APPROXIMATE = "database upgraded: day and week splits are approximate until agentfare ingest has re-read your logs"


class TestApproximateSplits:
    """``--no-ingest`` on a database whose logs no ingest has read since its upgrade says so once, on stderr."""

    def pending(self, tmp_path: Path, make_record: Callable[..., SessionRecord]) -> Path:
        log = tmp_path / "old.jsonl"
        log.write_text("{}\n")
        with Store(tmp_path / "home" / "agentfare.db") as existing:
            existing.upsert_session(make_record("old", source_path=str(log)))  # a session, and no ingested file: what the upgrade leaves
        return log

    def test_the_note_is_one_line_on_stderr(
        self, tmp_path: Path, consoles: Consoles, make_state: Callable[..., AppState], make_record: Callable[..., SessionRecord]
    ) -> None:
        self.pending(tmp_path, make_record)
        st = make_state(auto_ingest=False)
        st.store(), st.store(), st.store()  # asked for again and again: said once
        assert consoles.err_buf.getvalue() == APPROXIMATE + "\n"
        assert consoles.out_buf.getvalue() == "" and "\x1b" not in consoles.err_buf.getvalue()

    def test_a_normal_command_reads_the_logs_first_and_needs_no_note(
        self, tmp_path: Path, consoles: Consoles, make_state: Callable[..., AppState], make_record: Callable[..., SessionRecord], ingest_calls
    ) -> None:
        self.pending(tmp_path, make_record)
        ingest_calls(IngestReport())
        make_state().store()
        assert APPROXIMATE not in consoles.err_buf.getvalue()

    def test_the_ingest_command_needs_none_either(
        self, tmp_path: Path, consoles: Consoles, make_state: Callable[..., AppState], make_record: Callable[..., SessionRecord]
    ) -> None:
        self.pending(tmp_path, make_record)
        make_state(auto_ingest=False).store(ingest_first=False)
        assert consoles.err_buf.getvalue() == ""

    def test_once_the_file_the_session_came_from_is_read_there_is_nothing_to_say(
        self, tmp_path: Path, consoles: Consoles, make_state: Callable[..., AppState], make_record: Callable[..., SessionRecord]
    ) -> None:
        log = self.pending(tmp_path, make_record)
        with Store(tmp_path / "home" / "agentfare.db") as existing:
            existing.mark_file(str(log), "claude", "sig", 1)
        make_state(auto_ingest=False).store()
        assert consoles.err_buf.getvalue() == ""

    def test_one_agent_read_and_another_not_is_still_behind(
        self, tmp_path: Path, consoles: Consoles, make_state: Callable[..., AppState], make_record: Callable[..., SessionRecord]
    ) -> None:
        one, two = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
        one.write_text("{}\n"), two.write_text("{}\n")
        with Store(tmp_path / "home" / "agentfare.db") as existing:
            existing.upsert_session(make_record("a", source_path=str(one)))
            existing.upsert_session(make_record("b", agent="codex", source_path=str(two)))
            existing.mark_file(str(one), "claude", "sig", 1)  # `ingest --agent claude` after the upgrade
        make_state(auto_ingest=False).store()
        assert consoles.err_buf.getvalue() == APPROXIMATE + "\n"

    def test_an_empty_database_has_nothing_approximate(self, consoles: Consoles, make_state: Callable[..., AppState]) -> None:
        make_state(auto_ingest=False).store()
        assert consoles.err_buf.getvalue() == ""
