"""Log parsers, one per agent. Each ``agentfare.parsers.<agent>`` module exposes ``PARSER``."""

from __future__ import annotations

from importlib import import_module

from ..models import AGENTS
from .base import LogParser

# Part of every stored file signature (see ``ingest``): bump it whenever any parser's output changes for the same log
# (a new field, a fixed count, a different slot), so the next run re-reads the logs without asking for --full.
PARSERS_VERSION = 3


def get_parser(agent: str) -> LogParser:
    if agent not in AGENTS:
        raise KeyError(f"unknown agent {agent!r}; expected one of {', '.join(AGENTS)}")
    return import_module(f"{__name__}.{agent}").PARSER


__all__ = ["get_parser", "LogParser", "PARSERS_VERSION"]
