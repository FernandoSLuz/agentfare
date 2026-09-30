"""The autouse fixture in conftest.py keeps every test away from the real machine's agent data."""

import os
from pathlib import Path

from agentfare.config import Config, default_home


def test_home_and_data_dir_are_throwaway_directories(tmp_path_factory) -> None:
    base = tmp_path_factory.getbasetemp()
    assert Path(os.environ["HOME"]).is_relative_to(base)
    assert Path(os.environ["AGENTFARE_HOME"]).is_relative_to(base)
    assert Path("~/.claude").expanduser().is_relative_to(base)
    assert default_home().is_relative_to(base)


def test_default_config_points_at_the_throwaway_home() -> None:
    cfg = Config.load()
    assert cfg.db_path == default_home() / "agentfare.db"
    for agent in ("claude", "codex", "gemini"):
        assert all(Path(os.environ["HOME"]) in p.parents for p in cfg.sources(agent))


def test_agentfare_switches_are_cleared(frozen_now) -> None:
    assert "AGENTFARE_CONFIG" not in os.environ and "AGENTFARE_NOW" not in os.environ
    frozen_now()
    assert os.environ["AGENTFARE_NOW"] == "2026-09-30T12:00:00Z"


def test_a_frozen_clock_does_not_leak_into_the_next_test() -> None:
    assert "AGENTFARE_NOW" not in os.environ


def test_fixtures_dir_points_at_tests_fixtures(fixtures_dir) -> None:
    assert fixtures_dir == Path(__file__).parent / "fixtures" and fixtures_dir.is_dir()
