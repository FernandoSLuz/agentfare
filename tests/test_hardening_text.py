"""Text that comes from outside (a log file's name, an exception's message, a session id) must be harmless where it lands:
no control character on the terminal, no extra row or heading in a markdown report."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentfare import ingest as ingest_module
from agentfare.cli import app
from agentfare.db import Store
from agentfare.models import SessionRecord, Usage

NOW = "2026-09-30T12:00:00Z"
UNSAFE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")  # everything but newline and tab
HOSTILE = "evil\x1b]0;pwned\x07\x1b[31m\x9b‮name"


class ExplodingParser:
    """Finds one log file with a hostile name and fails to read it with a hostile message."""

    agent = "claude"

    def __init__(self, root: Path) -> None:
        self.file = root / f"{HOSTILE}.jsonl"
        self.file.write_text("{}\n", encoding="utf-8")

    def discover(self, root: Path) -> Iterator[Path]:
        yield self.file

    def signature(self, path: Path) -> str:
        return "sig"

    def parse(self, path: Path, *, idle_gap_seconds: int = 1800) -> list[SessionRecord]:
        raise RuntimeError(f"cannot read {HOSTILE}")


@pytest.fixture
def hostile_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """Global options for a run whose Claude root holds a hostile file, whose Codex root is missing under a hostile name."""
    monkeypatch.setenv("AGENTFARE_NOW", NOW)
    root = tmp_path / "logs"
    root.mkdir()
    parser = ExplodingParser(root)
    real = ingest_module.get_parser
    monkeypatch.setattr(ingest_module, "get_parser", lambda agent: parser if agent == "claude" else real(agent))
    home = tmp_path / "home"
    home.mkdir()
    import yaml

    (home / "config.yaml").write_text(
        yaml.safe_dump({"timezone": "UTC", "sources": {"claude": [str(root)], "codex": [str(tmp_path / f"missing-{HOSTILE}")], "gemini": []}}),
        encoding="utf-8",
    )
    return ("--home", str(home))


class TestIngestOutput:
    def test_ingest_warnings_and_missing_roots_are_clean(self, hostile_logs: tuple[str, ...]) -> None:
        result = CliRunner().invoke(app, [*hostile_logs, "ingest"])
        assert result.exit_code == 0, result.stderr
        assert "warning:" in result.stderr and "not found (skipped)" in result.stderr  # both lines are there...
        assert not UNSAFE.search(result.stderr) and not UNSAFE.search(result.stdout)  # ...and harmless
        assert "evil?]0;pwned?" in result.stderr

    def test_the_warnings_of_an_automatic_ingest_are_clean(self, hostile_logs: tuple[str, ...]) -> None:
        result = CliRunner().invoke(app, [*hostile_logs, "summary"])
        assert "could not parse" in result.stderr
        assert not UNSAFE.search(result.stderr) and not UNSAFE.search(result.stdout)

    def test_a_warning_cannot_forge_a_line_with_a_newline(self, hostile_logs: tuple[str, ...], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ExplodingParser, "parse", lambda self, path, *, idle_gap_seconds=1800: (_ for _ in ()).throw(RuntimeError("x\nforged: all good")))
        result = CliRunner().invoke(app, [*hostile_logs, "ingest"])
        assert not [ln for ln in result.stderr.splitlines() if ln.startswith("forged")]

    def test_the_json_keeps_the_text_but_no_raw_control_character(self, hostile_logs: tuple[str, ...]) -> None:
        result = CliRunner().invoke(app, [*hostile_logs, "ingest", "--json"])
        assert result.exit_code == 0 and not UNSAFE.search(result.stdout)


class TestConfigWarnings:
    def test_an_unknown_key_with_control_characters_is_shown_harmlessly(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text('"bad\\ekey\\x9b": 1\nsources: {claude: [], codex: [], gemini: []}\n', encoding="utf-8")
        result = CliRunner().invoke(app, ["--home", str(home), "--no-ingest", "summary"])
        assert "unknown key" in result.stderr and not UNSAFE.search(result.stderr)


# ---- the report -----------------------------------------------------------------------------------------
@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., tuple[str, ...]]:
    monkeypatch.setenv("AGENTFARE_NOW", NOW)

    def build(*records: SessionRecord) -> tuple[str, ...]:
        home = tmp_path / "world"
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text("timezone: UTC\nsources: {claude: [], codex: [], gemini: []}\n", encoding="utf-8")
        with Store(home / "agentfare.db") as store:
            for record in records:
                store.upsert_session(record)
        return ("--home", str(home), "--no-ingest")

    return build


def session(session_id: str) -> SessionRecord:
    return SessionRecord(
        agent="codex", session_id=session_id, source_path=f"/logs/{len(session_id)}.jsonl", project="shop",
        started_at="2026-09-29T10:00:00Z", ended_at="2026-09-29T10:10:00Z", wall_seconds=600, active_seconds=600,
        usage={"gpt-6-astra": Usage(input=100_000)}, prompts=1,
    )


def report(prefix: tuple[str, ...], *args: str) -> str:
    result = CliRunner().invoke(app, [*prefix, "report", *args])
    assert result.exit_code == 0, result.stderr
    return result.stdout


def table_rows(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    start = lines.index("## Biggest savings")
    rows = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("|"):
            rows.append(line)
    return rows


def cells(row: str) -> list[str]:
    """The cells of a GitHub table row: a pipe splits them unless a backslash escapes it."""
    return re.split(r"(?<!\\)\|", row.strip())[1:-1]


class TestReportSessionIds:
    def test_a_session_file_name_with_a_newline_and_a_heading_cannot_split_the_table_or_start_a_heading(self, world: Callable[..., tuple[str, ...]]) -> None:
        prefix = world(session("dddddd\n# H"))  # the id of a file named 'dddddd<newline># H.jsonl'
        text = report(prefix)
        headings = [ln for ln in text.splitlines() if ln.startswith("#")]
        assert "# H" not in headings and not [h for h in headings if h.startswith("# H")]
        rows = table_rows(text)
        assert len(rows) == 3 and all(len(cells(row)) == 8 for row in rows), rows  # header, rule and the one session
        assert "\n# H" not in text

    @pytest.mark.parametrize("session_id", ["ab|cd`e|f", "a`b``c`d12", "x|y|z|w|v|u", "<b>&amp;\"'12"])
    def test_a_pipe_or_a_backtick_in_the_id_stays_inside_its_own_cell(self, world: Callable[..., tuple[str, ...]], session_id: str) -> None:
        text = report(world(session(session_id)))
        rows = table_rows(text)
        assert len(rows) == 3 and all(len(cells(row)) == 8 for row in rows), rows

    def test_control_characters_in_the_id_are_replaced_in_every_format(self, world: Callable[..., tuple[str, ...]], tmp_path: Path) -> None:
        prefix = world(session("x\x1b[31m‮\x9bid123"))
        page = tmp_path / "r.html"
        report(prefix, "--format", "html", "-o", str(page))
        for text in (report(prefix), page.read_text(encoding="utf-8"), report(prefix, "--json")):
            assert not UNSAFE.search(text)
            assert "id123" in text  # the letters stay, the escape and the override do not

    def test_the_id_in_the_html_is_one_code_element(self, world: Callable[..., tuple[str, ...]], tmp_path: Path) -> None:
        prefix = world(session("dddddd\n# H<script>"))
        page = tmp_path / "r.html"
        report(prefix, "--format", "html", "-o", str(page))
        body = page.read_text(encoding="utf-8")
        match: Any = re.search(r'data-label="Session"><code>([^<]*)</code>', body)
        assert match and "\n" not in match[1] and "<script" not in body.lower()

    def test_an_ordinary_id_is_still_a_plain_code_span(self, world: Callable[..., tuple[str, ...]]) -> None:
        text = report(world(session("01a00000-0000-7000-8000-000000000001")))
        assert "| `00000001` |" in text


class TestErrorLines:
    def test_an_error_that_quotes_a_hostile_name_is_clean_but_keeps_its_lines(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["--config", str(tmp_path / "x\x1b]0;pwned\x07\x9b.yaml"), "--home", str(tmp_path), "config"])
        assert result.exit_code == 2, result.stdout + result.stderr
        assert "config file not found" in result.stderr and not UNSAFE.search(result.stderr)
        assert "x?]0;pwned??.yaml" in result.stderr

    def test_a_yaml_error_keeps_the_line_breaks_of_its_message(self, tmp_path: Path) -> None:
        bad = tmp_path / "c.yaml"
        bad.write_text("timezone: [unclosed\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["--config", str(bad), "--home", str(tmp_path), "config"])
        assert result.exit_code == 2 and "invalid YAML" in result.stderr and len(result.stderr.splitlines()) > 1
        assert not UNSAFE.search(result.stderr)


# ---- the new lines that quote a name ------------------------------------------------------------------------
class TestHintsAndNotes:
    NAMES = [HOSTILE, "[bold red]Work[/bold red]", "work", "Work"]

    @pytest.fixture
    def named(self, world: Callable[..., tuple[str, ...]]) -> tuple[str, ...]:
        records = []
        for i, name in enumerate(self.NAMES):
            rec = session(f"n{i}-xxxx")
            rec.project = name
            records.append(rec)
        return world(*records)

    def test_the_hints_for_a_name_that_matches_nothing_are_harmless(self, named: tuple[str, ...]) -> None:
        for wanted in ("evil", "bold", "wor", "zzzz"):
            for command in ("summary", "sessions", "models"):
                result = CliRunner().invoke(app, [*named, command, "--project", wanted])
                assert result.exit_code == 0, result.stderr
                assert not UNSAFE.search(result.stdout), (command, wanted)
        listed = CliRunner().invoke(app, [*named, "summary", "--project", "bold"]).stdout
        assert "closest names: [bold red]Work[/bold red]" in listed  # shown as written, not as markup

    def test_the_note_about_names_that_differ_only_by_case_is_harmless(self, named: tuple[str, ...]) -> None:
        result = CliRunner().invoke(app, [*named, "summary"], env={"COLUMNS": "200"})
        assert result.exit_code == 0 and not UNSAFE.search(result.stdout)
        assert "names that differ only by case are listed apart (Work, work)" in result.stdout

    def test_the_path_of_the_config_in_a_warning_is_harmless(self, tmp_path: Path) -> None:
        bad = tmp_path / f"{HOSTILE}[x].yaml"
        bad.write_text("timezon: UTC\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["--config", str(bad), "--home", str(tmp_path / "home"), "--no-ingest", "config"])
        assert result.exit_code == 0 and "unknown key timezon" in result.stderr
        assert not UNSAFE.search(result.stderr) and "evil?]0;pwned?" in result.stderr and "[x].yaml" in result.stderr

    def test_the_tz_string_in_the_warning_is_harmless(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(app, ["--home", str(tmp_path), "--no-ingest", "summary"], env={"TZ": "[bold]\x1b[31mx"})
        assert result.exit_code == 0 and "could not be resolved to a named zone" in result.stderr
        assert not UNSAFE.search(result.stderr) and "\x1b" not in result.stderr
