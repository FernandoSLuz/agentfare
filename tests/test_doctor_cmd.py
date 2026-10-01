"""`agentfare doctor`: a checklist of the install, the config, the logs, the data and the prices, with a fix for every problem.

doctor is read-only by contract: it never opens a log file, never writes anything (not even the data directory) and works
on a machine where nothing is configured. These tests run it against the synthetic fixtures and against throwaway
directories under tmp_path, with a frozen clock where a date matters.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import typer.main
from typer.testing import CliRunner

import agentfare
from agentfare.cli import app
from agentfare.commands import doctor as doctor_mod
from agentfare.db import SCHEMA_VERSION, Store
from agentfare.pricing import PriceTable, builtin_path, dump_pricing, load_pricing

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_CONFIG = FIXTURES / "config.yaml"
NOW = "2026-09-30T12:00:00Z"
runner = CliRunner()
IDS = [
    "python", "package", "command", "config", "logs.claude", "logs.codex", "logs.gemini", "logs", "cli.claude", "cli.codex",
    "cli.gemini", "data_dir", "database", "price_table", "price_models", "environment", "terminal", "network",
]  # fmt: skip


def invoke(*args: str, home: Path | None = None, config: Path | None = FIXTURE_CONFIG, columns: int = 100,
           env: dict[str, str | None] | None = None, cli: CliRunner = runner) -> Any:
    environment: dict[str, str | None] = {"COLUMNS": str(columns), "AGENTFARE_NOW": None, "NO_COLOR": None, "FORCE_COLOR": None, **(env or {})}
    argv = [*(["--config", str(config)] if config else []), *(["--home", str(home)] if home else []), *args]
    return cli.invoke(app, argv, env=environment)


def checks(*args: str, **kw: Any) -> tuple[Any, dict[str, dict[str, Any]]]:
    """``doctor --json``: the result and the checks by id."""
    result = invoke("doctor", "--json", *args, **kw)
    assert result.stdout.strip(), result.stderr
    return result, {c["id"]: c for c in json.loads(result.stdout)["checks"]}


def flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def filled(data_dir: Path) -> Path:
    """A data directory that holds the database of the three fixture sessions."""
    result = invoke("ingest", home=data_dir, env={"AGENTFARE_NOW": NOW})
    assert result.exit_code == 0, result.output
    return data_dir


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    return path


def program(directory: Path, name: str) -> Path:
    path = directory / name
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def clean_env(tmp_path: Path, **extra: str | None) -> dict[str, str | None]:
    """An environment with an empty HOME, so the default log folders hold nothing."""
    return {"HOME": str(tmp_path / "empty"), "PATH": str(tmp_path / "nobin"), **extra}


# ---- the command ---------------------------------------------------------------------------------------------------
class TestCommand:
    def test_doctor_is_a_command_with_json(self) -> None:
        group: Any = typer.main.get_command(app)
        assert "doctor" in group.commands
        assert "--json" in {opt for p in group.commands["doctor"].params for opt in p.opts}

    def test_every_check_is_there_in_order(self, data_dir: Path) -> None:
        result, found = checks(home=data_dir)
        assert list(found) == IDS
        data = json.loads(result.stdout)
        assert list(data) == ["ok", "version", "summary", "checks"]
        assert data["version"] == agentfare.__version__
        for check in data["checks"]:
            assert list(check) == ["id", "title", "status", "detail", "fix", "notes", "data"]
            assert check["status"] in ("ok", "warn", "fail") and check["detail"]

    def test_every_line_that_is_not_ok_has_a_concrete_fix(self, data_dir: Path, tmp_path: Path) -> None:
        # a machine with a bit of everything wrong: no logs, no CLIs, a frozen clock, a narrow terminal, an old price date
        _, found = checks(home=data_dir, config=None, columns=60, env=clean_env(tmp_path, AGENTFARE_NOW="2027-06-01T00:00:00Z"))
        bad = [c for c in found.values() if c["status"] != "ok"]
        assert {"logs", "price_table", "environment", "terminal", "command"} <= {c["id"] for c in bad}
        for check in bad:
            assert check["fix"] and len(check["fix"]) > 20, check["id"]
        for check in found.values():
            assert (check["fix"] is None) == (check["status"] == "ok"), check["id"]

    def test_the_summary_counts_add_up(self, data_dir: Path) -> None:
        result, found = checks(home=data_dir)
        data = json.loads(result.stdout)
        counts = {s: sum(c["status"] == s for c in found.values()) for s in ("ok", "warn", "fail")}
        assert data["summary"] == counts and sum(counts.values()) == len(IDS)
        assert data["ok"] is (counts["fail"] == 0)

    def test_text_output_is_a_plain_checklist(self, data_dir: Path) -> None:
        result = invoke("doctor", home=data_dir)
        assert result.exit_code == 0, result.output
        lines = result.stdout.splitlines()
        assert lines[0] == "AgentFare doctor"
        assert "\x1b" not in result.stdout + result.stderr
        assert any(line.startswith("ok    Python") for line in lines)
        text = flat(result.stdout)
        assert "Result:" in text and "ok" in text.split("Result:")[1]
        assert "read-only" in text

    def test_a_fix_line_follows_every_warning_in_the_text(self, data_dir: Path, tmp_path: Path) -> None:
        text = invoke("doctor", home=data_dir, config=None, env=clean_env(tmp_path)).stdout
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith(("warn", "fail")):
                rest = lines[i + 1:]
                following = next((n for n in rest if n.startswith(("ok", "warn", "fail", "Result"))), "")
                block = rest[:rest.index(following)] if following in rest else rest
                assert any(b.strip().startswith("fix:") for b in block), line

    def test_long_details_wrap_at_the_terminal_width(self, data_dir: Path, tmp_path: Path) -> None:
        text = invoke("doctor", home=data_dir, config=None, columns=60, env=clean_env(tmp_path)).stdout
        lead = 6 + len("agentfare on PATH") + 2  # the status word and the widest title
        for line in text.splitlines():
            assert len(line) <= 60 or len(line[lead:].split()) == 1, line  # only one unbreakable word (a path) may run past the edge

    def test_it_names_the_network_rule(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir)
        assert found["network"]["status"] == "ok"
        assert "never uses the network" in found["network"]["detail"] and "prices --refresh" in found["network"]["detail"]


# ---- a machine with nothing on it --------------------------------------------------------------------------------
class TestBrandNewMachine:
    def test_it_works_and_writes_nothing(self, tmp_path: Path) -> None:
        home = tmp_path / "empty"
        agentfare_home = tmp_path / "never-made" / "agentfare"
        result, found = checks(config=None, env={"HOME": str(home), "AGENTFARE_HOME": str(agentfare_home), "PATH": str(tmp_path / "nobin")})
        assert result.exit_code == 0, result.output
        assert not home.exists() and not agentfare_home.exists() and not agentfare_home.parent.exists()
        assert found["config"]["status"] == "ok" and "built-in" in found["config"]["detail"]
        assert found["data_dir"]["status"] == "ok" and "not created yet" in found["data_dir"]["detail"]
        assert found["database"]["status"] == "ok" and "does not exist yet" in found["database"]["detail"]
        assert found["logs"]["status"] == "warn" and "no session files" in found["logs"]["detail"]
        assert all(found[f"logs.{a}"]["status"] == "ok" and "not used here" in found[f"logs.{a}"]["detail"] for a in ("claude", "codex", "gemini"))

    def test_the_fix_for_no_logs_names_the_two_ways_out(self, tmp_path: Path, data_dir: Path) -> None:
        _, found = checks(home=data_dir, config=None, env=clean_env(tmp_path))
        fix = found["logs"]["fix"]
        assert "sources.<agent>" in fix and "config.yaml" in fix and "agentfare init" in fix
        assert "agentfare demo" in fix

    def test_it_exits_zero_with_only_warnings(self, tmp_path: Path, data_dir: Path) -> None:
        result = invoke("doctor", home=data_dir, config=None, env=clean_env(tmp_path))
        assert result.exit_code == 0 and "warn" in result.stdout


# ---- exit codes ---------------------------------------------------------------------------------------------------
class TestExitCode:
    def test_a_failing_check_exits_one_and_the_rest_still_run(self, tmp_path: Path, data_dir: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("timezone: [unclosed\n")
        result, found = checks(home=data_dir, config=bad)
        assert result.exit_code == 1
        assert found["config"]["status"] == "fail" and "invalid YAML" in found["config"]["detail"]
        assert list(found) == IDS and found["python"]["status"] == "ok"  # everything else was still checked
        assert json.loads(result.stdout)["ok"] is False
        assert "error:" not in result.stderr and "Traceback" not in result.output

    def test_warnings_do_not_fail(self, tmp_path: Path, data_dir: Path) -> None:
        result, found = checks(home=data_dir, config=None, env=clean_env(tmp_path, AGENTFARE_NOW=NOW))
        assert result.exit_code == 0
        assert "warn" in {c["status"] for c in found.values()} and "fail" not in {c["status"] for c in found.values()}

    def test_text_mode_uses_the_same_exit_code(self, tmp_path: Path, data_dir: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("timezone: [unclosed\n")
        result = invoke("doctor", home=data_dir, config=bad)
        assert result.exit_code == 1 and "fail  Config file" in result.stdout and "fix:" in result.stdout
        assert "1 failed" in flat(result.stdout)


# ---- python and the install ---------------------------------------------------------------------------------------
class DistStub:
    """What ``importlib.metadata.distribution`` returns, with only what doctor reads."""

    def __init__(self, version: str, direct_url: str | None = None, installer: str = "pip") -> None:
        self.version = version
        self.files = {"direct_url.json": direct_url, "INSTALLER": installer}

    def read_text(self, name: str) -> str | None:
        return self.files.get(name)


class TestPython:
    def test_a_supported_python_is_ok(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir)
        assert found["python"]["status"] == "ok" and found["python"]["detail"].startswith(f"{sys.version_info[0]}.{sys.version_info[1]}.")

    def test_python_below_3_11_fails_with_a_way_out(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "version_info", (3, 10, 12, "final", 0))
        result, found = checks(home=data_dir)
        assert result.exit_code == 1 and found["python"]["status"] == "fail"
        assert "3.11" in found["python"]["fix"] and "uv" in found["python"]["fix"]


class TestPackage:
    def test_the_version_and_where_it_lives_are_shown(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir)
        assert found["package"]["status"] == "ok" and f"agentfare {agentfare.__version__}" in found["package"]["detail"]
        assert found["package"]["data"]["version"] == agentfare.__version__
        assert found["package"]["data"]["location"] == str(Path(agentfare.__file__).parent)

    def test_an_editable_install_is_recognised(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = DistStub(agentfare.__version__, json.dumps({"url": "file:///src", "dir_info": {"editable": True}}))
        monkeypatch.setattr(doctor_mod.metadata, "distribution", lambda name: stub)
        _, found = checks(home=data_dir)
        assert "editable install" in found["package"]["detail"] and found["package"]["data"]["editable"] is True

    def test_a_plain_install_is_not_editable(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = DistStub(agentfare.__version__, json.dumps({"url": "file:///wheel", "dir_info": {}}))
        monkeypatch.setattr(doctor_mod.metadata, "distribution", lambda name: stub)
        monkeypatch.setattr(doctor_mod, "_package_dir", lambda: Path("/venv/lib/python3.12/site-packages/agentfare"))
        _, found = checks(home=data_dir)
        assert found["package"]["data"]["editable"] is False and "editable" not in found["package"]["detail"]

    def test_metadata_that_disagrees_with_the_code_is_a_warning(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(doctor_mod.metadata, "distribution", lambda name: DistStub("0.0.1"))
        _, found = checks(home=data_dir)
        assert found["package"]["status"] == "warn" and "0.0.1" in found["package"]["detail"]
        assert "reinstall" in found["package"]["fix"]

    def test_running_from_a_source_tree_is_not_a_problem(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def missing(name: str) -> Any:
            raise doctor_mod.metadata.PackageNotFoundError(name)

        monkeypatch.setattr(doctor_mod.metadata, "distribution", missing)
        _, found = checks(home=data_dir)
        assert found["package"]["status"] == "ok" and "not installed" in found["package"]["detail"]

    @pytest.mark.parametrize(("prefix", "word"), [("/home/dev/.local/share/uv/tools/agentfare", "uv tool upgrade"), ("/home/dev/.local/pipx/venvs/agentfare", "pipx upgrade")])
    def test_the_upgrade_command_follows_the_tool_that_installed_it(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch, prefix: str, word: str) -> None:
        monkeypatch.setattr(sys, "prefix", prefix)
        monkeypatch.setattr(doctor_mod.metadata, "distribution", lambda name: DistStub(agentfare.__version__, json.dumps({"dir_info": {}})))
        monkeypatch.setattr(doctor_mod, "_package_dir", lambda: Path(prefix) / "lib" / "python3.12" / "site-packages" / "agentfare")
        _, found = checks(home=data_dir)
        assert any(word in note for note in found["package"]["notes"]), found["package"]


class TestCommandOnPath:
    def test_found_on_path(self, data_dir: Path, bin_dir: Path) -> None:
        program(bin_dir, "agentfare")
        _, found = checks(home=data_dir, env={"PATH": str(bin_dir)})
        assert found["command"]["status"] == "ok" and str(bin_dir / "agentfare") in found["command"]["detail"]

    def test_not_on_path_says_how_to_add_it(self, data_dir: Path, tmp_path: Path) -> None:
        _, found = checks(home=data_dir, env={"PATH": str(tmp_path / "nobin")})
        assert found["command"]["status"] == "warn"
        assert "python -m agentfare" in found["command"]["detail"] and "PATH" in found["command"]["fix"]

    def test_the_fix_names_uv_when_uv_installed_it(self, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "prefix", "/home/dev/.local/share/uv/tools/agentfare")
        _, found = checks(home=data_dir, env={"PATH": str(tmp_path / "nobin")})
        assert "uv tool update-shell" in found["command"]["fix"]

    @pytest.mark.parametrize("prefix", ["/home/dev/.local/share/uv/tools/agentfare", "/home/dev/.local/pipx/venvs/agentfare"])
    def test_a_tool_install_names_its_own_interpreter_not_the_python_on_path(self, data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
        """`python -m agentfare` only works for the interpreter that has agentfare: for uv and pipx that is the one inside the tool's own environment, never the `python` on PATH."""
        monkeypatch.setattr(sys, "prefix", prefix)
        monkeypatch.setattr(sys, "executable", f"{prefix}/bin/python")
        _, found = checks(home=data_dir, env={"PATH": str(tmp_path / "nobin")})
        detail = found["command"]["detail"]
        assert f"{prefix}/bin/python -m agentfare" in detail, detail
        assert "(python -m agentfare" not in detail, detail

    def test_another_agentfare_that_shadows_this_one_is_a_warning(self, data_dir: Path, bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        other = program(bin_dir, "agentfare")
        monkeypatch.setattr(sys, "argv", ["/venv/bin/agentfare", "doctor"])
        _, found = checks(home=data_dir, env={"PATH": str(bin_dir)})
        assert found["command"]["status"] == "warn" and str(other) in found["command"]["detail"] and "/venv/bin/agentfare" in found["command"]["detail"]


# ---- the config file -----------------------------------------------------------------------------------------------
class TestConfigFile:
    def test_no_file_is_fine(self, data_dir: Path, tmp_path: Path) -> None:
        _, found = checks(home=data_dir, config=None, env=clean_env(tmp_path))
        assert found["config"]["status"] == "ok" and "init --write-config" in found["config"]["detail"]

    def test_a_file_is_named(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir)
        assert found["config"]["status"] == "ok" and str(FIXTURE_CONFIG) in found["config"]["detail"]

    def test_a_config_named_but_missing_is_a_failure_with_a_fix(self, data_dir: Path, tmp_path: Path) -> None:
        result, found = checks(home=data_dir, config=tmp_path / "nope.yaml")
        assert result.exit_code == 1 and found["config"]["status"] == "fail"
        assert "not found" in found["config"]["detail"] and "init --write-config" in found["config"]["fix"]

    def test_bad_yaml_is_a_failure_that_names_the_file(self, data_dir: Path, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("timezone: [unclosed\n")
        _, found = checks(home=data_dir, config=bad)
        assert str(bad) in found["config"]["detail"] and "config --init --force" in found["config"]["fix"]

    def test_a_bad_setting_is_a_failure(self, data_dir: Path, tmp_path: Path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text("idle_gap_minutes: soon\n")
        result, found = checks(home=data_dir, config=cfg)
        assert result.exit_code == 1 and found["config"]["status"] == "fail" and "idle_gap_minutes" in found["config"]["detail"]
        assert "idle_gap_minutes" in found["config"]["fix"]

    def test_an_unknown_key_is_a_warning_with_the_did_you_mean(self, data_dir: Path, tmp_path: Path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text("timezon: UTC\nrouter:\n  min_saving_ratioo: 2\n")
        result, found = checks(home=data_dir, config=cfg)
        assert result.exit_code == 0 and found["config"]["status"] == "warn"
        assert "unknown key timezon (did you mean timezone?)" in found["config"]["notes"]
        assert "unknown key router.min_saving_ratioo (did you mean min_saving_ratio?)" in found["config"]["notes"]
        assert "config --default" in found["config"]["fix"]

    def test_a_model_the_router_cannot_price_is_a_warning(self, data_dir: Path, tmp_path: Path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text("router:\n  recommend: {light: not-a-real-model}\n")
        _, found = checks(home=data_dir, config=cfg)
        assert found["config"]["status"] == "warn"
        assert any("router.recommend.light: not-a-real-model has no verified price" in note for note in found["config"]["notes"])

    def test_a_broken_config_still_shows_the_default_places(self, data_dir: Path, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just\n- a list\n")
        _, found = checks(home=data_dir, config=bad, env=clean_env(tmp_path))
        assert found["config"]["status"] == "fail" and "top level must be a mapping" in found["config"]["detail"]
        assert "default" in found["logs.claude"]["detail"] or found["logs.claude"]["data"]["origin"] == "default"
        assert any("config could not be read" in n for n in found["logs"]["notes"])

    def test_a_config_is_never_written_or_changed(self, data_dir: Path, tmp_path: Path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text("timezone: UTC\n")
        before = (cfg.read_bytes(), cfg.stat().st_mtime_ns)
        invoke("doctor", home=data_dir, config=cfg)
        assert (cfg.read_bytes(), cfg.stat().st_mtime_ns) == before and not (data_dir / "config.yaml").exists()


# ---- agent logs and CLIs ----------------------------------------------------------------------------------------------
class TestAgentLogs:
    def test_the_fixture_logs_are_found_and_counted(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir)
        for agent in ("claude", "codex", "gemini"):
            check = found[f"logs.{agent}"]
            assert check["status"] == "ok" and "found 1 file," in check["detail"] and check["data"]["files"] == 1
            assert check["data"]["origin"] == "config"
        assert found["logs"]["status"] == "ok" and "3 session files" in found["logs"]["detail"]

    def test_a_folder_that_exists_but_is_empty_is_a_warning(self, tmp_path: Path, data_dir: Path) -> None:
        (tmp_path / "logs").mkdir()
        cfg = tmp_path / "c.yaml"
        cfg.write_text(f"sources:\n  claude: [{tmp_path / 'logs'}]\n")
        _, found = checks(home=data_dir, config=cfg, env=clean_env(tmp_path))
        assert found["logs.claude"]["status"] == "warn" and "no session files yet" in found["logs.claude"]["detail"]
        assert "sources.claude" in found["logs.claude"]["fix"]

    def test_a_missing_folder_is_fine_when_the_agent_is_not_there(self, tmp_path: Path, data_dir: Path) -> None:
        _, found = checks(home=data_dir, config=None, env=clean_env(tmp_path))
        assert found["logs.codex"]["status"] == "ok" and "not used here" in found["logs.codex"]["detail"]

    def test_a_missing_folder_is_a_warning_when_the_cli_is_installed(self, tmp_path: Path, data_dir: Path, bin_dir: Path) -> None:
        program(bin_dir, "codex")
        _, found = checks(home=data_dir, config=None, env=clean_env(tmp_path, PATH=str(bin_dir)))
        check = found["logs.codex"]
        assert check["status"] == "warn" and "codex" in check["detail"] and "sources.codex" in check["fix"] and "config.yaml" in check["fix"]
        assert found["cli.codex"]["status"] == "ok" and str(bin_dir / "codex") in found["cli.codex"]["detail"]

    def test_a_configured_folder_that_is_missing_is_a_warning(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text(f"sources:\n  gemini: [{tmp_path / 'typo' / 'tmp'}]\n")
        _, found = checks(home=data_dir, config=cfg, env=clean_env(tmp_path))
        assert found["logs.gemini"]["status"] == "warn" and "does not exist" in found["logs.gemini"]["detail"]
        assert "check the path in sources.gemini" in found["logs.gemini"]["fix"]

    def test_the_agents_own_variable_is_followed_and_named(self, tmp_path: Path, data_dir: Path) -> None:
        moved = tmp_path / "moved" / "projects" / "p"
        moved.mkdir(parents=True)
        (moved / "s.jsonl").write_text("{}\n")
        _, found = checks(home=data_dir, config=None, env=clean_env(tmp_path, CLAUDE_CONFIG_DIR=str(tmp_path / "moved")))
        assert found["logs.claude"]["status"] == "ok" and found["logs.claude"]["data"]["origin"] == "CLAUDE_CONFIG_DIR"
        assert found["environment"]["status"] == "ok" and "CLAUDE_CONFIG_DIR" in found["environment"]["detail"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
    def test_a_folder_that_cannot_be_read_is_a_failure_with_a_fix(self, tmp_path: Path, data_dir: Path) -> None:
        locked = tmp_path / "locked"
        (locked / "p").mkdir(parents=True)
        cfg = tmp_path / "c.yaml"
        cfg.write_text(f"sources:\n  claude: [{locked}]\n")
        locked.chmod(0)
        try:
            result, found = checks(home=data_dir, config=cfg, env=clean_env(tmp_path))
        finally:
            locked.chmod(0o755)
        assert result.exit_code == 1 and found["logs.claude"]["status"] == "fail"
        assert "cannot read" in found["logs.claude"]["detail"] and "chmod" in found["logs.claude"]["fix"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
    def test_it_never_opens_a_log_file(self, tmp_path: Path, data_dir: Path) -> None:
        copy = tmp_path / "copy"
        shutil.copytree(FIXTURES, copy, ignore=shutil.ignore_patterns("__pycache__"))
        files = [p for p in copy.rglob("*.jsonl")]
        for path in files:
            path.chmod(0)  # a doctor that opened one would fail on it
        try:
            _, found = checks(home=data_dir, config=copy / "config.yaml")
        finally:
            for path in files:
                path.chmod(0o644)
        assert found["logs"]["status"] == "ok" and "3 session files" in found["logs"]["detail"]

    def test_a_named_pipe_among_the_logs_cannot_hang_it(self, tmp_path: Path, data_dir: Path) -> None:
        root = tmp_path / "projects" / "p"
        root.mkdir(parents=True)
        os.mkfifo(root / "pipe.jsonl")
        (root / "real.jsonl").write_text("{}\n")
        cfg = tmp_path / "c.yaml"
        cfg.write_text(f"sources:\n  claude: [{tmp_path / 'projects'}]\n")
        _, found = checks(home=data_dir, config=cfg, env=clean_env(tmp_path))
        assert found["logs.claude"]["data"]["files"] == 1

    def test_the_cli_is_looked_up_but_never_run(self, tmp_path: Path, data_dir: Path, bin_dir: Path) -> None:
        marker = tmp_path / "ran"
        (bin_dir / "claude").write_text(f"#!/bin/sh\ntouch {marker}\n")
        (bin_dir / "claude").chmod(0o755)
        _, found = checks(home=data_dir, env={"PATH": str(bin_dir)})
        assert found["cli.claude"]["status"] == "ok" and str(bin_dir / "claude") in found["cli.claude"]["detail"]
        assert "not on PATH" in found["cli.codex"]["detail"] and found["cli.codex"]["status"] == "ok"
        assert not marker.exists()


# ---- the data directory and the database ----------------------------------------------------------------------------
class TestDataDirectory:
    def test_missing_is_fine(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir)
        assert found["data_dir"]["status"] == "ok" and str(data_dir) in found["data_dir"]["detail"] and not data_dir.exists()

    def test_a_private_directory_is_ok(self, filled: Path) -> None:
        _, found = checks(home=filled)
        assert found["data_dir"]["status"] == "ok" and "0700" in found["data_dir"]["detail"]

    def test_a_directory_others_can_read_is_a_warning(self, filled: Path) -> None:
        filled.chmod(0o755)
        _, found = checks(home=filled)
        check = found["data_dir"]
        assert check["status"] == "warn" and "0755" in check["detail"] and f"chmod 700 {filled}" in check["fix"]
        assert mode(filled) == 0o755  # doctor reports it and leaves it as it is

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
    def test_a_directory_you_cannot_write_is_a_failure(self, filled: Path) -> None:
        filled.chmod(0o500)
        try:
            result, found = checks(home=filled)
        finally:
            filled.chmod(0o700)
        assert result.exit_code == 1 and found["data_dir"]["status"] == "fail" and "not writable" in found["data_dir"]["detail"]
        assert "chmod" in found["data_dir"]["fix"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
    def test_a_directory_that_cannot_be_created_is_a_failure(self, tmp_path: Path) -> None:
        parent = tmp_path / "ro"
        parent.mkdir()
        parent.chmod(0o500)
        try:
            result, found = checks(home=parent / "agentfare")
        finally:
            parent.chmod(0o700)
        assert result.exit_code == 1 and found["data_dir"]["status"] == "fail" and "cannot be created" in found["data_dir"]["detail"]
        assert "AGENTFARE_HOME" in found["data_dir"]["fix"]

    def test_a_path_that_is_a_file_is_a_failure(self, tmp_path: Path) -> None:
        notdir = tmp_path / "file"
        notdir.write_text("x")
        result, found = checks(home=notdir)
        assert result.exit_code == 1 and found["data_dir"]["status"] == "fail" and "not a folder" in found["data_dir"]["detail"]


class TestDatabase:
    def test_a_filled_database_is_described(self, filled: Path) -> None:
        _, found = checks(home=filled, env={"AGENTFARE_NOW": "2026-10-02T12:00:00Z"})
        check = found["database"]
        assert check["status"] == "ok", check
        assert "3 sessions" in check["detail"] and "schema 3" in check["detail"] and "0600" in check["detail"]
        assert "2026-09-30T12:00:00Z" in check["detail"] and "2 days ago" in check["detail"]
        assert check["data"] == {"path": str(filled / "agentfare.db"), "exists": True, "mode": "0600", "writable": True, "schema": SCHEMA_VERSION,
                                 "sessions": 3, "last_stored": "2026-09-30T12:00:00Z", "reread_pending": False}

    def test_a_database_others_can_read_is_a_warning(self, filled: Path) -> None:
        (filled / "agentfare.db").chmod(0o644)
        _, found = checks(home=filled)
        check = found["database"]
        assert check["status"] == "warn" and "0644" in check["detail"] and f"chmod 600 {filled / 'agentfare.db'}" in check["fix"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
    def test_a_read_only_database_is_a_warning_that_names_no_ingest(self, filled: Path) -> None:
        (filled / "agentfare.db").chmod(0o400)
        try:
            result, found = checks(home=filled)
        finally:
            (filled / "agentfare.db").chmod(0o600)
        check = found["database"]
        assert result.exit_code == 0 and check["status"] == "warn" and "read-only" in check["detail"] and "--no-ingest" in check["fix"]

    def test_a_newer_database_is_a_failure(self, filled: Path) -> None:
        with sqlite3.connect(filled / "agentfare.db") as conn:
            conn.execute("PRAGMA user_version = 99")
        result, found = checks(home=filled)
        check = found["database"]
        assert result.exit_code == 1 and check["status"] == "fail" and "newer agentfare" in check["detail"] and "upgrade" in check["fix"]

    def test_an_older_database_is_a_warning_that_says_ingest_upgrades_it(self, filled: Path) -> None:
        with sqlite3.connect(filled / "agentfare.db") as conn:
            conn.execute("PRAGMA user_version = 2")
        result, found = checks(home=filled)
        check = found["database"]
        assert result.exit_code == 0 and check["status"] == "warn" and "schema 2" in check["detail"] and "agentfare ingest" in check["fix"]

    def test_a_file_that_is_not_a_database_is_a_failure(self, tmp_path: Path) -> None:
        home = tmp_path / "data"
        home.mkdir(mode=0o700)
        (home / "agentfare.db").write_bytes(b"this is not sqlite, " * 20)
        result, found = checks(home=home)
        check = found["database"]
        assert result.exit_code == 1 and check["status"] == "fail" and "not a database" in check["detail"]
        assert "delete" in check["fix"] and "rebuilt" in check["fix"]

    def test_an_empty_file_is_a_fresh_database(self, tmp_path: Path) -> None:
        home = tmp_path / "data"
        home.mkdir(mode=0o700)
        (home / "agentfare.db").touch(mode=0o600)
        _, found = checks(home=home)
        assert found["database"]["status"] == "ok" and "empty" in found["database"]["detail"]

    def test_a_database_that_still_has_to_re_read_the_logs_is_a_warning(self, filled: Path) -> None:
        with sqlite3.connect(filled / "agentfare.db") as conn:
            conn.execute("DELETE FROM ingested_files")  # what an upgrade does
        _, found = checks(home=filled)
        check = found["database"]
        assert check["status"] == "warn" and check["data"]["reread_pending"] is True
        assert "re-read" in check["detail"] and "agentfare ingest" in check["fix"]

    def test_no_sessions_while_logs_exist_is_a_warning(self, data_dir: Path) -> None:
        Store(data_dir / "agentfare.db").close()  # a database that was created and never filled
        _, found = checks(home=data_dir)
        assert found["database"]["status"] == "warn" and "no sessions" in found["database"]["detail"]
        assert "agentfare init" in found["database"]["fix"]

    def test_it_never_writes_to_the_database(self, filled: Path) -> None:
        db = filled / "agentfare.db"
        before = (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in filled.iterdir()))
        invoke("doctor", home=filled)
        invoke("doctor", "--json", home=filled)
        assert (db.read_bytes(), db.stat().st_mtime_ns, sorted(p.name for p in filled.iterdir())) == before

    def test_it_never_upgrades_an_old_database(self, filled: Path) -> None:
        with sqlite3.connect(filled / "agentfare.db") as conn:
            conn.execute("PRAGMA user_version = 2")
        db = filled / "agentfare.db"
        before = db.read_bytes()
        invoke("doctor", home=filled)
        assert db.read_bytes() == before

    def test_a_database_named_in_the_config_is_the_one_checked(self, tmp_path: Path, data_dir: Path) -> None:
        elsewhere = tmp_path / "elsewhere" / "my.db"
        cfg = tmp_path / "c.yaml"
        cfg.write_text(f"database: {elsewhere}\n")
        invoke("ingest", home=data_dir, config=cfg)
        _, found = checks(home=data_dir, config=cfg)
        assert found["database"]["data"]["path"] == str(elsewhere) and found["database"]["data"]["sessions"] == 0


# ---- the price table --------------------------------------------------------------------------------------------------
class TestPriceTable:
    def test_the_built_in_table_is_described(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir, env={"AGENTFARE_NOW": NOW})
        check = found["price_table"]
        table = load_pricing(path=builtin_path())
        assert check["status"] == "ok", check
        assert "built-in table" in check["detail"] and f"{len(table.models)} models" in check["detail"]
        assert check["data"]["models"] == len(table.models) and check["data"]["source"] == "builtin"
        assert check["data"]["oldest_checked"] == min(p.checked for p in table.models.values())

    def test_the_age_of_the_oldest_check_is_said_in_days(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir, env={"AGENTFARE_NOW": NOW})
        assert "oldest price check 2026-09-30 (today)" in found["price_table"]["detail"]
        _, found = checks(home=data_dir, env={"AGENTFARE_NOW": "2026-10-02T09:00:00Z"})
        assert "(2 days ago)" in found["price_table"]["detail"]

    def test_unverified_models_are_named(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir, env={"AGENTFARE_NOW": NOW})
        table = load_pricing(path=builtin_path())
        unverified = sorted(m for m, p in table.models.items() if not p.priced)
        assert unverified and found["price_table"]["data"]["unverified"] == unverified
        for name in unverified[:3]:
            assert name in found["price_table"]["detail"]

    def test_prices_older_than_30_days_are_a_warning_with_the_refresh_command(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir, env={"AGENTFARE_NOW": "2026-10-31T12:00:00Z"})  # 31 days after the check date
        check = found["price_table"]
        assert check["status"] == "warn" and "31 days" in check["detail"]
        assert "agentfare prices --refresh" in check["fix"] and "only command that uses the network" in check["fix"]

    def test_exactly_30_days_is_still_fresh(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir, env={"AGENTFARE_NOW": "2026-10-30T00:00:00Z"})
        assert found["price_table"]["status"] == "ok"

    def test_an_override_file_is_named_and_judged_by_its_own_dates(self, tmp_path: Path, data_dir: Path) -> None:
        table = load_pricing(path=builtin_path())
        old = {m: replace(p, checked="2026-01-15") for m, p in table.models.items()}
        data_dir.mkdir()
        (data_dir / "pricing.yaml").write_text(dump_pricing(PriceTable(models=old, aliases=dict(table.aliases))))
        _, found = checks(home=data_dir, env={"AGENTFARE_NOW": NOW})
        check = found["price_table"]
        assert check["data"]["source"] == "override" and "override file" in check["detail"] and "2026-01-15" in check["detail"]
        assert check["status"] == "warn" and f"{(date(2026, 9, 30) - date(2026, 1, 15)).days} days" in check["detail"]

    def test_a_model_with_no_check_date_is_a_warning(self, tmp_path: Path, data_dir: Path) -> None:
        table = load_pricing(path=builtin_path())
        models = dict(table.models)
        first = sorted(models)[0]
        models[first] = replace(models[first], checked="")
        data_dir.mkdir()
        (data_dir / "pricing.yaml").write_text(dump_pricing(PriceTable(models=models, aliases={})))
        _, found = checks(home=data_dir, env={"AGENTFARE_NOW": NOW})
        assert found["price_table"]["status"] == "warn" and first in found["price_table"]["detail"] and "no check date" in found["price_table"]["detail"]

    def test_a_broken_price_file_is_a_failure(self, data_dir: Path) -> None:
        data_dir.mkdir()
        (data_dir / "pricing.yaml").write_text("models: [oops\n")
        result, found = checks(home=data_dir)
        assert result.exit_code == 1 and found["price_table"]["status"] == "fail"
        assert "pricing.yaml" in found["price_table"]["detail"] and "built-in" in found["price_table"]["fix"]
        assert found["price_models"]["status"] == "ok"  # nothing to judge, and no crash

    def test_a_pricing_file_named_in_the_config_but_missing_is_a_failure(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text(f"pricing_file: {tmp_path / 'nope.yaml'}\n")
        result, found = checks(home=data_dir, config=cfg)
        assert result.exit_code == 1 and found["price_table"]["status"] == "fail" and "does not exist" in found["price_table"]["detail"]


class TestModelsInUse:
    def test_priced_models_are_ok(self, filled: Path) -> None:
        _, found = checks(home=filled)
        assert found["price_models"]["status"] == "ok" and "4 models" in found["price_models"]["detail"]

    def test_no_sessions_means_nothing_to_judge(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir)
        assert found["price_models"]["status"] == "ok" and "no sessions" in found["price_models"]["detail"]

    def test_an_unpriced_model_in_your_logs_is_named_with_a_fix(self, filled: Path) -> None:
        with sqlite3.connect(filled / "agentfare.db") as conn:
            conn.execute("UPDATE session_usage SET model = 'mystery-model-9' WHERE model = 'gpt-6-astra'")
        _, found = checks(home=filled)
        check = found["price_models"]
        assert check["status"] == "warn" and "mystery-model-9 (not in the table)" in check["detail"]
        assert "left out of every dollar total" in check["fix"] and "pricing.yaml" in check["fix"]
        assert check["data"]["without_price"] == ["mystery-model-9"]

    def test_an_unverified_model_in_your_logs_is_named(self, filled: Path) -> None:
        with sqlite3.connect(filled / "agentfare.db") as conn:
            conn.execute("UPDATE session_usage SET model = 'codex-auto-review' WHERE model = 'gpt-6-astra'")
        _, found = checks(home=filled)
        assert "codex-auto-review (unverified)" in found["price_models"]["detail"]


# ---- the environment and the terminal --------------------------------------------------------------------------------
class TestEnvironment:
    def test_nothing_set_is_ok(self, data_dir: Path, tmp_path: Path) -> None:
        _, found = checks(home=data_dir, config=None, env={"AGENTFARE_HOME": None, **clean_env(tmp_path)})
        assert found["environment"]["status"] == "ok" and "no AgentFare variable" in found["environment"]["detail"]

    def test_a_frozen_clock_is_a_warning(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir, env={"AGENTFARE_NOW": NOW})
        check = found["environment"]
        assert check["status"] == "warn" and "AGENTFARE_NOW" in check["detail"] and NOW in check["detail"]
        assert "unset AGENTFARE_NOW" in check["fix"]

    def test_a_bad_clock_value_is_a_failure(self, data_dir: Path) -> None:
        result, found = checks(home=data_dir, env={"AGENTFARE_NOW": "yesterday"})
        check = found["environment"]
        assert result.exit_code == 1 and check["status"] == "fail" and "ISO-8601" in check["detail"] and "unset AGENTFARE_NOW" in check["fix"]
        assert found["price_table"]["status"] in ("ok", "warn")  # the price age falls back to the real clock instead of crashing

    def test_the_variables_that_move_things_are_listed(self, data_dir: Path, tmp_path: Path) -> None:
        env = {"AGENTFARE_HOME": str(data_dir), "AGENTFARE_CONFIG": str(FIXTURE_CONFIG), "CODEX_HOME": str(tmp_path / "cx"), "GEMINI_CLI_HOME": str(tmp_path / "gm")}
        _, found = checks(config=None, env=env)
        detail = found["environment"]["detail"]
        for name in ("AGENTFARE_HOME", "AGENTFARE_CONFIG", "CODEX_HOME", "GEMINI_CLI_HOME"):
            assert name in detail, name
        assert found["environment"]["data"]["set"] == {k: v for k, v in env.items()}

    def test_a_variable_that_a_config_source_overrides_is_said_to_be_ignored(self, tmp_path: Path, data_dir: Path) -> None:
        _, found = checks(home=data_dir, env={"CODEX_HOME": str(tmp_path / "cx")})  # the fixture config sets sources.codex
        assert any("CODEX_HOME" in n and "ignored" in n for n in found["environment"]["notes"]) or "ignored" in found["environment"]["detail"]


class TestTerminal:
    def test_it_reports_the_encoding_the_width_and_the_colour(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir, columns=120)
        check = found["terminal"]
        assert check["status"] == "ok" and "utf-8" in check["detail"] and "120 columns" in check["detail"] and "plain text" in check["detail"]
        assert check["data"]["width"] == 120

    def test_a_narrow_terminal_is_a_warning(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir, columns=60)
        check = found["terminal"]
        assert check["status"] == "warn" and "60 columns" in check["detail"] and "--json" in check["fix"]

    def test_a_terminal_that_cannot_write_unicode_is_a_warning(self, data_dir: Path) -> None:
        ascii_runner = CliRunner(charset="ascii")
        result = invoke("doctor", "--json", home=data_dir, cli=ascii_runner)
        check = {c["id"]: c for c in json.loads(result.stdout)["checks"]}["terminal"]
        assert check["status"] == "warn" and "ascii" in check["detail"] and "UTF-8" in check["fix"]

    def test_no_color_is_noted(self, data_dir: Path) -> None:
        _, found = checks(home=data_dir, env={"NO_COLOR": "1"})
        assert "NO_COLOR" in found["terminal"]["detail"]


# ---- text safety -------------------------------------------------------------------------------------------------------
class TestSafety:
    def test_control_characters_in_a_path_never_reach_the_terminal(self, tmp_path: Path, data_dir: Path) -> None:
        cfg = tmp_path / "c.yaml"
        cfg.write_text('sources:\n  claude: ["/nope/\\u001b[31mred\\u202e"]\n')
        result = invoke("doctor", home=data_dir, config=cfg, env=clean_env(tmp_path))
        assert "\x1b" not in result.stdout and "‮" not in result.stdout
        assert "/nope/?[31mred?" in result.stdout

    def test_the_check_that_crashes_becomes_a_failed_line_not_a_traceback(self, data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("something odd")

        monkeypatch.setattr(doctor_mod.Doctor, "_terminal", boom)
        result, found = checks(home=data_dir)
        assert found["terminal"]["status"] == "fail" and "something odd" in found["terminal"]["detail"] and "issues" in found["terminal"]["fix"]
        assert "Traceback" not in result.output and list(found) == IDS


# ---- the fixes are real ---------------------------------------------------------------------------------------------------
class TestFixesAreRealCommands:
    def test_every_agentfare_command_a_fix_names_exists_with_the_flags_it_shows(self, tmp_path: Path, data_dir: Path, filled: Path) -> None:
        """A fix that tells a person to run a command or a flag that is not there is worse than no fix: the fixes of many
        different problems are collected and every ``agentfare COMMAND --flag`` in them is looked up in the real command tree."""
        group: Any = typer.main.get_command(app)
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("timezone: [unclosed\n")
        odd = tmp_path / "odd.yaml"
        odd.write_text("timezon: UTC\nrouter:\n  recommend: {light: not-a-model}\nsources:\n  claude: [/nope/x]\n")
        runs: list[dict[str, Any]] = [
            {"home": data_dir, "config": None, "env": clean_env(tmp_path, AGENTFARE_NOW="2027-06-01T00:00:00Z"), "columns": 60},
            {"home": data_dir, "config": bad_yaml},
            {"home": data_dir, "config": tmp_path / "missing.yaml"},
            {"home": data_dir, "config": odd, "env": clean_env(tmp_path)},
            {"home": filled, "env": {"AGENTFARE_NOW": "2026-12-01T00:00:00Z"}},
        ]
        fixes: list[str] = []
        for kw in runs:
            fixes += [c["fix"] for c in checks(**kw)[1].values() if c["fix"]]
        with sqlite3.connect(filled / "agentfare.db") as conn:
            conn.execute("DELETE FROM ingested_files")
            conn.execute("UPDATE session_usage SET model = 'mystery-model-9' WHERE model = 'gpt-6-astra'")
        fixes += [c["fix"] for c in checks(home=filled)[1].values() if c["fix"]]
        (filled / "agentfare.db").chmod(0o644)
        filled.chmod(0o755)
        fixes += [c["fix"] for c in checks(home=filled)[1].values() if c["fix"]]
        assert len(fixes) >= 12
        named = 0
        for fix in fixes:
            for match in re.finditer(r"(?<!-m )(?<!python -m )\bagentfare ([a-z]+)((?: --[a-z-]+)*)", fix):
                command, flags = match[1], match[2].split()
                if command not in group.commands:  # prose such as "agentfare rebuilds it" would show up here, so the texts avoid it
                    raise AssertionError(f"{match[0]!r} in {fix!r} names a command that does not exist")
                options = {opt for param in group.commands[command].params for opt in param.opts}
                assert all(flag in options for flag in flags), f"{match[0]!r}: {command} has no such option"
                named += 1
        assert named >= 8, "the fixes name too few commands for this check to mean anything"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__]))
