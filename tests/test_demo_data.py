"""The synthetic history behind `agentfare demo` (src/agentfare/demo_data, written by scripts/make_demo_data.py).

What is checked, and how:

* the committed files are exactly what the generator writes now (a drift guard), small, compact JSONL, relative config;
* the real parsers read every file without a single error, and every session comes out as the token table says
  (tool calls, files, errors, prompts, tokens per model);
* the money is worked out by hand here, from the generator's own token table (`PLANS`) and `pricing.yaml`, with `Decimal`
  and without importing the cost code of agentfare, and agrees with `summary` and `advise`: the spend, what a cheaper model
  would have saved, how that splits between what a person prompted and sub-agent threads, and the tokens left out because
  codex-auto-review has no price;
* privacy: every text field of every log carries the marker PRIVACY-CANARY-DEMO and the marker never reaches the database;
  no name, home directory or path of the machine that runs the test appears in the data, only the invented /home/demo directory.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import os
import re
import sqlite3
import sys
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from agentfare.cli import app
from agentfare.config import DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_demo_data.py"
DATA = ROOT / "src" / "agentfare" / "demo_data"
PRICING = ROOT / "src" / "agentfare" / "data" / "pricing.yaml"
NOW = "2026-09-30T18:00:00Z"
pytestmark = pytest.mark.skipif(not SCRIPT.is_file(), reason="needs a checkout with scripts/ (an installed copy has none)")


def load_generator() -> Any:
    spec = importlib.util.spec_from_file_location("make_demo_data", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["make_demo_data"] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


gen = load_generator()
PLANS = gen.PLANS
DOC = gen.DOCUMENTED
CANARY = gen.CANARY


# ---- the money, by hand --------------------------------------------------------------------------------------------------
PRICES: dict[str, Any] = yaml.safe_load(PRICING.read_text(encoding="utf-8"))["models"]


def dec(value: Any) -> Decimal:
    return Decimal(str(value))


def cost_ucents(model: str, tokens: tuple[int, int, int, int, int]) -> int | None:
    """tokens x price, in micro-cents (1 USD = 100 cents = 100,000,000 ucents, a price is USD per million tokens), or None when the
    model has no verified price. Every token count of the table is even, so every product is a whole number: no rounding."""
    price = PRICES[model]
    if price["input"] == "unverified":
        return None
    inp, read, write, write_1h, out = tokens
    p_in = dec(price["input"])
    p_read = dec(price.get("cached_input", price["input"]))
    p_write = dec(price.get("cache_write", price["input"]))
    p_write_1h = dec(price.get("cache_write_1h", price.get("cache_write", price["input"])))
    ucents = (inp * p_in + read * p_read + write * p_write + write_1h * p_write_1h + out * dec(price["output"])) * 100
    assert ucents == ucents.to_integral_value(), (model, tokens)
    return int(ucents)


def total_tokens(plan: Any) -> tuple[int, int, int, int, int]:
    return tuple(sum(part.tokens[i] for part in plan.parts) for i in range(5))  # type: ignore[return-value]


def tier_of(plan: Any, active_minutes: float) -> str:
    """The router's score and tier of a session, from the signals and thresholds of the default config."""
    signals = DEFAULT_CONFIG["router"]["signals"]
    tokens = total_tokens(plan)
    values = {"input_tokens": tokens[0] + tokens[1] + tokens[2] + tokens[3], "tool_calls": plan.tools, "files_touched": plan.files,
              "error_retries": plan.errors, "active_minutes": active_minutes}
    score = sum(2 if values[n] >= signals[n]["heavy"] else 1 if values[n] >= signals[n]["medium"] else 0 for n in values)
    cuts = DEFAULT_CONFIG["router"]["tiers"]
    return "light" if score <= cuts["light_max"] else "medium" if score <= cuts["medium_max"] else "heavy"


def tier(plan: Any) -> str:
    """No gap in a session is longer than the idle gap but its pause, so its active time is its minutes minus the pause, a little
    less when the person types after the silence (at most 10 minutes less): both ends must give one tier."""
    if not plan.pause:
        return tier_of(plan, plan.minutes)
    tiers = {tier_of(plan, plan.minutes - plan.pause[1]), tier_of(plan, plan.minutes - plan.pause[1] - 10)}
    assert len(tiers) == 1, (plan.key, tiers)
    return tiers.pop()


def priced(plan: Any) -> bool:
    return all(cost_ucents(part.model, part.tokens) is not None for part in plan.parts)


def advice(plan: Any) -> tuple[int, int] | None:
    """(actual, recommended) in micro-cents when the router would switch this session, else None (the rules of docs/ROUTING.md)."""
    target = DEFAULT_CONFIG["router"]["recommend"][tier(plan)]
    if target is None or not priced(plan) or {part.model for part in plan.parts} == {target}:
        return None
    actual = sum(cost_ucents(part.model, part.tokens) or 0 for part in plan.parts)
    cheaper = cost_ucents(target, total_tokens(plan))
    assert cheaper is not None
    minimum = dec(DEFAULT_CONFIG["router"]["min_saving_ratio"])
    if cheaper >= actual or actual < cheaper * minimum:
        return None
    return actual, cheaper


def hand_spend() -> int:
    return sum(c for plan in PLANS for part in plan.parts if (c := cost_ucents(part.model, part.tokens)) is not None)


def hand_savings() -> dict[str, int]:
    switched = [(plan, pair) for plan in PLANS if (pair := advice(plan))]
    saved = {plan.key: actual - cheaper for plan, (actual, cheaper) in switched}
    return {
        "switched": len(switched),
        "saved": sum(saved.values()),
        "prompted": sum(v for plan in PLANS if plan.prompts > 0 and (v := saved.get(plan.key))),
        "background": sum(v for plan in PLANS if plan.prompts == 0 and (v := saved.get(plan.key))),
    }


# ---- running the real thing ----------------------------------------------------------------------------------------------
CONFIG = DATA / "config.yaml"


def agentfare(home: Path, *args: str) -> Any:
    result = CliRunner().invoke(app, ["--config", str(CONFIG), "--home", str(home), *args], env={"AGENTFARE_NOW": NOW})
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout) if "--json" in args else result.stdout


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    yield tmp_path_factory.mktemp("demo-home")


@pytest.fixture(scope="module")
def ingested(home: Path) -> dict[str, Any]:
    return agentfare(home, "ingest", "--json")


@pytest.fixture(scope="module")
def summary(home: Path, ingested: dict[str, Any]) -> dict[str, Any]:
    return agentfare(home, "--no-ingest", "summary", "--json")


@pytest.fixture(scope="module")
def advise(home: Path, ingested: dict[str, Any]) -> dict[str, Any]:
    return agentfare(home, "--no-ingest", "advise", "--json")


# ---- the files -----------------------------------------------------------------------------------------------------------
class TestFiles:
    def test_the_committed_files_are_what_the_generator_writes_now(self) -> None:
        fresh, committed = gen.generate(), gen.on_disk()
        assert sorted(fresh) == sorted(committed), "files added or removed: run python scripts/make_demo_data.py"
        assert [path for path in fresh if fresh[path] != committed[path]] == [], "run python scripts/make_demo_data.py"

    def test_generating_twice_gives_the_same_bytes(self) -> None:
        assert gen.generate() == gen.generate()

    def test_the_data_is_small(self) -> None:
        sizes = {p: p.stat().st_size for p in DATA.rglob("*") if p.is_file()}
        assert sum(sizes.values()) < 1_500_000, sum(sizes.values())
        assert max(sizes.values()) < 150_000, max(sizes.items(), key=lambda kv: kv[1])

    def test_every_log_line_is_compact_json(self) -> None:
        for path in DATA.rglob("*.jsonl"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                record = json.loads(line)
                assert json.dumps(record, separators=(",", ":"), ensure_ascii=False) == line, f"{path.name}:{number}"

    def test_the_config_has_relative_sources_and_utc(self) -> None:
        config = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        assert config == {"timezone": "UTC", "sources": {"claude": ["claude/projects"], "codex": ["codex/sessions"], "gemini": ["gemini/tmp"]}}
        for agent, roots in config["sources"].items():
            assert (DATA / roots[0]).is_dir(), agent

    def test_the_layout_is_that_of_each_agent(self) -> None:
        claude = [p for p in (DATA / "claude/projects").rglob("*.jsonl") if "subagents" not in p.parts]
        assert all(re.fullmatch(r"[0-9a-f-]{36}\.jsonl", p.name) and p.parent.name.startswith("-home-demo-projects-") for p in claude)
        assert all(re.fullmatch(r"rollout-2026-09-\d\dT\d\d-\d\d-\d\d-[0-9a-f-]{36}\.jsonl", p.name) for p in (DATA / "codex/sessions").rglob("*.jsonl"))
        chats = [p for p in (DATA / "gemini/tmp").rglob("*.jsonl") if p.parent.name == "chats"]
        assert all(re.fullmatch(r"session-2026-09-\d\dT\d\d-\d\d-[0-9a-f]{8}\.jsonl", p.name) for p in chats)
        for root in (DATA / "gemini/tmp").iterdir():
            assert (root / ".project_root").read_text(encoding="utf-8").startswith("/home/demo/projects/")


# ---- what the parsers make of them ---------------------------------------------------------------------------------------
class TestIngest:
    def test_every_file_is_read_without_an_error(self, ingested: dict[str, Any]) -> None:
        assert ingested["errors"] == [] and ingested["files_failed"] == 0 and ingested["missing_roots"] == []
        assert ingested["duplicate_sessions"] == 0
        assert ingested["files_seen"] == ingested["files_parsed"] == DOC["sessions"]
        assert ingested["sessions_stored"] == ingested["database"]["sessions"] == DOC["sessions"] == len(PLANS)
        assert ingested["by_agent"] == DOC["by_agent"]

    def test_every_session_comes_out_as_the_table_says(self, home: Path, ingested: dict[str, Any]) -> None:
        db = sqlite3.connect(home / "agentfare.db")
        try:
            sessions = {(r[0], r[1]): r for r in db.execute(
                "select agent, started_at, tool_calls, files_touched, error_retries, prompts, project, key from sessions")}
            usage: dict[str, dict[str, list[int]]] = {}
            for key, model, *buckets in db.execute("select key, model, input, cache_read, cache_write, cache_write_1h, output from session_usage"):
                row = usage.setdefault(key, {}).setdefault(model, [0] * 5)
                for i, n in enumerate(buckets):
                    row[i] += n
        finally:
            db.close()
        assert len(sessions) == len(PLANS)
        for plan in PLANS:
            started = gen.iso(gen.epoch(plan.start))[:19] + "Z"
            _, _, tools, files, errors, prompts, project, key = sessions[(plan.agent, started)]
            assert (tools, files, errors, prompts, project) == (plan.tools, plan.files, plan.errors, plan.prompts, plan.project), plan.key
            expected: dict[str, list[int]] = {}
            for part in plan.parts:
                row = expected.setdefault(part.model, [0] * 5)
                for i, n in enumerate(part.tokens):
                    row[i] += n
            assert usage[key] == expected, plan.key


# ---- the money -----------------------------------------------------------------------------------------------------------
class TestMoney:
    def test_the_spend_worked_out_by_hand_is_what_the_generator_documents(self) -> None:
        assert hand_spend() == DOC["spend_ucents"]

    def test_summary_agrees(self, summary: dict[str, Any]) -> None:
        assert summary["total"]["ucents"] == hand_spend()
        assert summary["sessions"] == DOC["sessions"] and summary["projects"] == DOC["projects"] and summary["models"] == DOC["models"]
        assert {w["week"]: w["cost"]["ucents"] for w in summary["by_week"]} == DOC["weeks"]
        assert sum(DOC["weeks"].values()) == DOC["spend_ucents"]
        assert "$%d.%02d" % divmod(summary["total"]["cents"], 100) == "$193.96"  # the figure the README and the screenshots show

    def test_the_model_without_a_price_is_left_out_and_named(self, summary: dict[str, Any]) -> None:
        unpriced = {p.model: p for plan in PLANS for p in plan.parts if PRICES[p.model]["input"] == "unverified"}
        assert set(unpriced) == set(gen.UNPRICED) == {"codex-auto-review"}
        left_out = sum(sum(p.tokens) for plan in PLANS for p in plan.parts if p.model in unpriced)
        assert summary["excluded_tokens"] == left_out == DOC["excluded_tokens"]
        [row] = [m for m in summary["by_model"] if m["model"] == "codex-auto-review"]
        assert row["status"] == "unverified" and row["cost"] is None
        assert [(w["model"], w["status"], w["excluded_tokens"]) for w in summary["warnings"]] == [("codex-auto-review", "unverified", left_out)]

    def test_every_other_model_is_in_the_price_table(self) -> None:
        for plan in PLANS:
            for part in plan.parts:
                assert part.model in PRICES, part.model

    def test_what_a_cheaper_model_would_save_worked_out_by_hand(self, advise: dict[str, Any]) -> None:
        mine = hand_savings()
        assert mine["saved"] == DOC["saved_ucents"] and mine["switched"] == DOC["switched"]
        assert mine["prompted"] == DOC["saved_prompted_ucents"] and mine["background"] == DOC["saved_background_ucents"]
        assert hand_spend() - mine["saved"] == DOC["router_ucents"]
        total = advise["total"]
        assert total["sessions"] == DOC["priced_sessions"] and total["switched"] == DOC["switched"] and total["skipped"] == 2
        assert (total["actual"]["ucents"], total["recommended"]["ucents"], total["saved"]["ucents"]) == (
            DOC["spend_ucents"], DOC["router_ucents"], DOC["saved_ucents"])
        assert advise["saved_split"]["prompted"]["ucents"] == DOC["saved_prompted_ucents"]
        assert advise["saved_split"]["background"]["ucents"] == DOC["saved_background_ucents"]

    def test_the_router_saves_a_clear_share_of_the_spend(self) -> None:
        share = DOC["saved_ucents"] / DOC["spend_ucents"]
        assert 0.15 <= share <= 0.25, share
        assert 100_00 <= DOC["spend_ucents"] // 1_000_000 <= 999_00  # low hundreds of dollars, in cents

    def test_several_light_sessions_on_the_flagship_get_a_clear_saving(self, advise: dict[str, Any]) -> None:
        light_on_astra = [p for p in PLANS if p.parts[0].model == "gpt-6-astra" and tier(p) == "light" and p.prompts > 0]
        assert len(light_on_astra) >= 6
        assert all(advice(p) and advice(p)[0] > 4 * advice(p)[1] for p in light_on_astra)  # type: ignore[index]

    def test_the_week_in_progress_has_the_demo_clock_after_its_last_session(self, summary: dict[str, Any], home: Path) -> None:
        assert [w["week"] for w in summary["by_week"]] == ["2026-W39", "2026-W40"]
        assert [d["day"] for d in summary["by_day"]][0] == "2026-09-21" and [d["day"] for d in summary["by_day"]][-1] == "2026-09-30"
        db = sqlite3.connect(home / "agentfare.db")
        try:
            [last] = db.execute("select max(ended_at) from sessions").fetchone()
        finally:
            db.close()
        assert "2026-09-30T17:00:00Z" < last < NOW  # the last session is still going shortly before the clock


# ---- the story the numbers tell ------------------------------------------------------------------------------------------
class TestStory:
    def test_the_mix_of_sessions(self) -> None:
        by = {agent: [p for p in PLANS if p.agent == agent] for agent in ("claude", "codex", "gemini")}
        assert {a: len(v) for a, v in by.items()} == DOC["by_agent"]
        heavy_opus = [p for p in by["claude"] if p.parts[0].model == "claude-opus-5-5" and tier(p) == "heavy"]
        assert len(heavy_opus) >= 6 and all(p.tools >= 40 and p.files >= 7 and p.errors >= 3 for p in heavy_opus)
        assert any(p.parts[0].model == "claude-sonnet-5-5" and tier(p) == "light" for p in by["claude"])
        assert {p.parts[0].model for p in by["codex"]} >= {"gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol", "codex-auto-review"}
        assert {p.parts[0].model for p in by["gemini"]} == {"gemini-2.5-pro", "gemini-3.1-pro-preview"}
        assert len({p.project for p in PLANS}) == 5

    def test_sub_agents_and_threads_with_no_prompt(self) -> None:
        assert [p.key for p in PLANS if p.agent == "codex" and p.kind == "subagent"] == ["x06", "x13"]
        assert [p.key for p in PLANS if p.agent == "codex" and p.kind == "review"] == ["r01", "r02"]
        assert all(p.prompts == 0 for p in PLANS if p.kind != "main")
        assert any(len(p.parts) > 1 for p in PLANS if p.agent == "claude"), "a Claude session with sub-agent transcripts"

    def test_one_session_spans_midnight_and_one_leans_on_one_hour_cache_writes(self) -> None:
        spans = [p for p in PLANS if gen.iso(gen.epoch(p.start))[:10] != gen.iso(gen.epoch(p.start) + p.minutes * 60)[:10]]
        assert [p.key for p in spans] == ["c12"]
        one_hour = [p for p in PLANS if p.parts[0].tokens[3] > 1_000_000]
        assert [p.key for p in one_hour] == ["c11"]

    def test_the_midnight_session_is_split_between_both_days(self, home: Path, ingested: dict[str, Any]) -> None:
        by_day = {d["day"]: d["sessions"] for d in agentfare(home, "--no-ingest", "summary", "--json")["by_day"]}
        sessions_started = {}
        for plan in PLANS:
            sessions_started[gen.iso(gen.epoch(plan.start))[:10]] = sessions_started.get(gen.iso(gen.epoch(plan.start))[:10], 0) + 1
        assert by_day["2026-09-26"] == sessions_started["2026-09-26"] + 1  # the session that began on the 25th
        assert by_day["2026-09-25"] == sessions_started["2026-09-25"]


# ---- privacy -------------------------------------------------------------------------------------------------------------
TEXT_KEYS = {"text", "thinking", "command", "description", "old_string", "new_string", "prompt", "unified_diff", "subject", "objective"}
TEXT_WHEN_STRING = {"content", "output"}
HARNESS_HEADER = re.compile(r"Script (?:completed|failed)\nWall time ")  # the first part of a Codex tool output is the harness's, not text


def strings(value: Any, key: str = "") -> Iterator[tuple[str, str]]:
    """Every (key, text) of a JSON value whose key says it holds text a person or a model wrote."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from strings(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v, key)
    elif isinstance(value, str) and (key in TEXT_KEYS or key in TEXT_WHEN_STRING or (key == "pattern" and not value.startswith("**"))):
        if not HARNESS_HEADER.match(value):
            yield key, value


class TestPrivacy:
    def test_every_text_field_of_every_log_carries_the_marker(self) -> None:
        checked = 0
        for path in DATA.rglob("*.jsonl"):
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for key, text in strings(json.loads(line)):
                    assert CANARY in text, f"{path.name}:{number} {key}"
                    checked += 1
        assert checked > 2000
        for path in DATA.rglob("*.meta.json"):
            assert CANARY in path.read_text(encoding="utf-8")

    def test_the_marker_never_reaches_the_database(self, home: Path, ingested: dict[str, Any], summary: dict[str, Any], advise: dict[str, Any]) -> None:
        agentfare(home, "--no-ingest", "sessions", "--json")
        files = [p for p in home.iterdir() if p.is_file()]
        assert any(p.name == "agentfare.db" for p in files)
        for path in files:
            assert CANARY.encode() not in path.read_bytes(), path.name
        assert CANARY not in json.dumps([summary, advise])

    def test_the_logs_do_hold_the_marker(self) -> None:
        """So that the check above can fail: the marker is in the logs the database was built from."""
        assert all(CANARY in p.read_text(encoding="utf-8") for p in DATA.rglob("*.jsonl"))

    def test_no_name_or_path_of_this_machine_is_in_the_data(self) -> None:
        user = getpass.getuser()
        homes = {str(Path.home())}
        try:
            import pwd

            entry = pwd.getpwuid(os.getuid())
            user, homes = entry.pw_name, homes | {entry.pw_dir}
        except ImportError:  # no pwd on Windows
            pass
        for path in (p for p in DATA.rglob("*") if p.is_file()):
            text = path.read_text(encoding="utf-8")
            for home_dir in homes:
                assert len(home_dir) < 6 or home_dir not in text, path.name
            if len(user) >= 5:
                assert not re.search(rf"(?:/home/|/Users/|\\Users\\|~){re.escape(user)}(?![A-Za-z0-9_])", text), path.name
            assert set(re.findall(r"/home/([A-Za-z0-9_.-]+)", text)) <= {"demo"}, path.name
            assert "/Users/" not in text and "C:\\" not in text, path.name

    def test_only_invented_projects_and_paths_appear(self) -> None:
        paths = set()
        for path in DATA.rglob("*"):
            if path.is_file():
                paths |= set(re.findall(r"/home/demo/projects/([A-Za-z0-9_.-]+)", path.read_text(encoding="utf-8")))
        assert paths == {"storefront-api", "notes-app", "infra-terraform", "data-pipeline", "docs-site"}

    def test_the_ids_are_made_up(self) -> None:
        """Session ids come from a hash of an invented label, so they cannot be the ids of the logs the project was written on:
        the same label and seed always give the same id, and the label is not an id of any agent."""
        assert gen.fake_uuid("claude:c01") == gen.fake_uuid("claude:c01") != gen.fake_uuid("claude:c02")
        ids = [p.stem for p in (DATA / "claude/projects").rglob("*.jsonl") if "subagents" not in p.parts]
        assert len(set(ids)) == len(ids) == DOC["by_agent"]["claude"]


# ---- the page that describes them ----------------------------------------------------------------------------------------
class TestDocs:
    def test_the_demo_page_states_the_numbers_of_the_table(self) -> None:
        """docs/DEMO.md quotes the headline figures: they are rounded to whole cents from the documented micro-cents."""
        page = (ROOT / "docs" / "DEMO.md").read_text(encoding="utf-8")

        def usd(ucents: int) -> str:
            cents = (ucents + 500_000) // 1_000_000
            return f"${cents // 100:,}.{cents % 100:02d}"

        figures = [usd(DOC["spend_ucents"]), usd(DOC["router_ucents"]), usd(DOC["saved_ucents"]), usd(DOC["saved_prompted_ucents"]),
                   usd(DOC["saved_background_ucents"]), *(usd(v) for v in DOC["weeks"].values()),
                   f"{DOC['saved_ucents'] * 1000 // DOC['spend_ucents'] / 10}%", f"{DOC['switched']} advised switches",
                   f"{DOC['sessions']} invented sessions", f"{DOC['excluded_tokens'] // 1000}k tokens"]
        for figure in figures:
            assert figure in page, f"docs/DEMO.md does not say {figure}"
