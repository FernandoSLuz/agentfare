"""Shared test setup.

Every test runs isolated from the real machine: HOME and AGENTFARE_HOME point to throwaway
directories and the AGENTFARE_* switches are cleared, so nothing can read or write the real
``~/.agentfare``, ``~/.claude``, ``~/.codex`` or ``~/.gemini`` (nor wherever CLAUDE_CONFIG_DIR, CODEX_HOME or
GEMINI_CLI_HOME point).
"""

from __future__ import annotations

import os

# Typer decides at import time to force colour (and its own width) when it runs inside GitHub Actions or when one of these
# variables is set, and the help and usage errors the tests compare as plain text would then carry ANSI codes. This runs
# before anything imports typer, so a test run looks the same on a laptop and in CI.
for _variable in ("GITHUB_ACTIONS", "FORCE_COLOR", "PY_COLORS", "TERMINAL_WIDTH"):
    os.environ.pop(_variable, None)

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agentfare.config import DEFAULT_CONFIG, Config, deep_merge, fmt_iso, parse_iso
from agentfare.models import SessionRecord, Usage
from agentfare.pricing import PriceTable, parse_pricing

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_machine(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    monkeypatch.setenv("AGENTFARE_HOME", str(tmp_path_factory.mktemp("agentfare-home")))
    monkeypatch.delenv("AGENTFARE_CONFIG", raising=False)
    monkeypatch.delenv("AGENTFARE_NOW", raising=False)
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME"):  # where the agents were told to keep their logs: not here
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], datetime]:
    """``frozen_now("2026-09-30T12:00:00Z")`` freezes AGENTFARE_NOW and returns that instant (aware UTC)."""

    def freeze(iso: str = "2026-09-30T12:00:00Z") -> datetime:
        monkeypatch.setenv("AGENTFARE_NOW", iso)
        return parse_iso(iso)

    return freeze


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., Config]:
    """``make_config(timezone="UTC", sources={...})``: defaults with these top-level keys overridden."""

    def make(**overrides: Any) -> Config:
        return Config(home=tmp_path / "data", data=deep_merge(DEFAULT_CONFIG, overrides), base_dir=tmp_path)

    return make


@pytest.fixture
def make_table() -> Callable[..., PriceTable]:
    """``make_table({"model": {"input": 2, "output": 10}}, aliases={...})`` through the real parser."""

    def make(models: dict[str, Any], aliases: dict[str, str] | None = None) -> PriceTable:
        return parse_pricing({"schema": 1, "models": models, "aliases": aliases or {}})

    return make


@pytest.fixture
def make_record() -> Callable[..., SessionRecord]:
    def make(
        session_id: str = "s1",
        *,
        agent: str = "claude",
        project: str = "proj",
        started_at: str = "2026-09-30T10:00:00Z",
        wall_seconds: int = 600,
        usage: dict[str, Usage] | None = None,
        **extra: Any,
    ) -> SessionRecord:
        ended = fmt_iso(parse_iso(started_at) + timedelta(seconds=wall_seconds))
        return SessionRecord(
            agent=agent,
            session_id=session_id,
            source_path=extra.pop("source_path", f"/logs/{session_id}.jsonl"),
            project=project,
            started_at=started_at,
            ended_at=ended,
            wall_seconds=wall_seconds,
            active_seconds=extra.pop("active_seconds", wall_seconds),
            usage={"claude-sonnet-5-5": Usage(input=1000, output=100)} if usage is None else usage,
            **extra,
        )

    return make
