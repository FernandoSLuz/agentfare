"""Config validation: unknown keys, model targets, relocated log directories, a missing pricing file, packaging metadata."""

import io
import json
import shutil
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from agentfare import cli
from agentfare.cli import app
from agentfare.config import DEFAULT_CONFIG, DEFAULT_CONFIG_YAML, Config, unknown_keys, validate
from agentfare.db import Store
from agentfare.ingest import ingest
from agentfare.pricing import PriceTable, PricingError, load_pricing
from agentfare.state import AppState

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "config.yaml"
ROOT = Path(__file__).parent.parent
RELOCATION_VARIABLES = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME")


def load(tmp_path: Path, body: str) -> Config:
    (tmp_path / "config.yaml").write_text(body, encoding="utf-8")
    return Config.load(tmp_path / "config.yaml", home=tmp_path)


# ---- unknown keys ----------------------------------------------------------------------------------------------------
class TestUnknownKeys:
    def test_the_documented_defaults_have_no_unknown_key(self, tmp_path: Path) -> None:
        assert load(tmp_path, DEFAULT_CONFIG_YAML).warnings == []
        assert Config.load(home=tmp_path).warnings == []
        assert unknown_keys(DEFAULT_CONFIG) == []

    def test_the_example_from_the_issue(self, tmp_path: Path) -> None:
        cfg = load(tmp_path, "router:\n  min_saving_ratioo: 1.3\n")
        assert cfg.warnings == ["unknown key router.min_saving_ratioo (did you mean min_saving_ratio?)"]

    @pytest.mark.parametrize(
        ("body", "warning"),
        [
            ("timezon: UTC\n", "unknown key timezon (did you mean timezone?)"),
            ("idle_gap_minute: 5\n", "unknown key idle_gap_minute (did you mean idle_gap_minutes?)"),
            ("router:\n  tier: {light_max: 1}\n", "unknown key router.tier (did you mean tiers?)"),
            ("router:\n  tiers: {light_maxx: 1}\n", "unknown key router.tiers.light_maxx (did you mean light_max?)"),
            ("router:\n  signals: {tool_call: {medium: 1}}\n", "unknown key router.signals.tool_call (did you mean tool_calls?)"),
            ("router:\n  signals:\n    tool_calls: {mediun: 1}\n", "unknown key router.signals.tool_calls.mediun (did you mean medium?)"),
            ("router:\n  recommend: {ligth: gpt-6.1-sol}\n", "unknown key router.recommend.ligth (did you mean light?)"),
            ("router:\n  run:\n    keyword_point: 3\n", "unknown key router.run.keyword_point (did you mean keyword_points?)"),
            ("router:\n  run:\n    prompt_chars: {mediun: 3}\n", "unknown key router.run.prompt_chars.mediun (did you mean medium?)"),
            ("router:\n  run:\n    default_tokens:\n      light: {inputs: 3}\n", "unknown key router.run.default_tokens.light.inputs (did you mean input?)"),
            ("router:\n  run:\n    models:\n      claude: {lite: x}\n", "unknown key router.run.models.claude.lite (did you mean light?)"),
            ("agents:\n  claude: {command: [claude]}\n", "unknown key agents.claude.command (did you mean commands?)"),
            ("agents:\n  mine: {commands: [m], modelflag: -m}\n", "unknown key agents.mine.modelflag (did you mean model_flag?)"),
            ("sources:\n  cluade: [logs]\n", "unknown key sources.cluade (did you mean claude?)"),
        ],
    )
    def test_a_misspelt_key_is_named_with_its_full_path_and_a_suggestion(self, tmp_path: Path, body: str, warning: str) -> None:
        assert load(tmp_path, body).warnings == [warning]

    def test_a_key_that_is_close_to_nothing_gets_no_suggestion(self, tmp_path: Path) -> None:
        assert load(tmp_path, "zzzzzz: 1\nrouter:\n  qqqq: 2\n").warnings == ["unknown key zzzzzz", "unknown key router.qqqq"]

    def test_every_unknown_key_is_reported_in_file_order(self, tmp_path: Path) -> None:
        cfg = load(tmp_path, "timezon: UTC\nrouter:\n  min_saving_ratioo: 1\n  tiers: {light_maxx: 1}\nagents:\n  claude: {model_flagg: -m}\n")
        assert [w.split(" (")[0] for w in cfg.warnings] == [
            "unknown key timezon",
            "unknown key router.min_saving_ratioo",
            "unknown key router.tiers.light_maxx",
            "unknown key agents.claude.model_flagg",
        ]

    def test_unknown_keys_never_stop_the_load_and_the_known_ones_still_apply(self, tmp_path: Path) -> None:
        cfg = load(tmp_path, "timezon: UTC\ntimezone: UTC\nrouter:\n  min_saving_ratio: 2\n  min_saving_ratioo: 9\n")
        assert cfg.get("timezone") == "UTC" and cfg.get("router.min_saving_ratio") == 2
        assert len(cfg.warnings) == 2

    @pytest.mark.parametrize(
        "body",
        [
            "router:\n  run:\n    model_aliases: {sonnet: claude-sonnet-5-5, my-own-name: gpt-6.1-sol}\n",
            "router:\n  run:\n    model_aliases: {}\n",
            "router:\n  run:\n    models:\n      aider: {light: a, medium: b, heavy: c}\n",
            "agents:\n  aider: {commands: [aider], model_flag: --model}\n  other: {commands: [other], model_flag: null}\n",
            "sources:\n  codex: [a, b]\n  claude: logs\n",
            "router:\n  run:\n    heavy_keywords: [anything, at, all]\n    light_keywords: []\n",
        ],
    )
    def test_keys_the_user_chooses_are_not_unknown(self, tmp_path: Path, body: str) -> None:
        assert load(tmp_path, body).warnings == []

    def test_a_key_is_known_as_soon_as_it_is_a_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(DEFAULT_CONFIG["router"]["run"], "a_new_default", {"width": 1})
        assert unknown_keys({"router": {"run": {"a_new_default": {"width": 2}}}}) == []
        assert unknown_keys({"router": {"run": {"a_new_default": {"widht": 2}}}}) == ["unknown key router.run.a_new_default.widht (did you mean width?)"]

    @pytest.mark.parametrize("body", ["router: 5\n", "router:\n  tiers: [1, 2]\n", "agents: [claude]\n", "sources: nope\n", "router:\n  run:\n    models: x\n"])
    def test_a_value_of_the_wrong_shape_is_left_to_the_accessors(self, tmp_path: Path, body: str) -> None:
        assert load(tmp_path, body).warnings == []

    def test_a_key_that_is_not_text_does_not_break_it(self, tmp_path: Path) -> None:
        assert load(tmp_path, "5: x\nrouter:\n  7: y\n").warnings == ["unknown key 5", "unknown key router.7"]

    def test_a_config_built_in_code_has_no_warnings(self, tmp_path: Path) -> None:
        assert Config(home=tmp_path, data=dict(DEFAULT_CONFIG)).warnings == []

    def test_a_key_left_empty_is_not_unknown(self, tmp_path: Path) -> None:
        assert load(tmp_path, "router:\n  tiers:\nsources:\nagents:\n").warnings == []


# ---- what the person sees --------------------------------------------------------------------------------------------
class Consoles:
    def __init__(self) -> None:
        self.out_buf, self.err_buf = io.StringIO(), io.StringIO()
        self.out = Console(file=self.out_buf, width=200, force_terminal=False)
        self.err = Console(file=self.err_buf, width=200, force_terminal=False)


def typo_home(tmp_path: Path, extra: str = "") -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(f"router:\n  min_saving_ratioo: 1.3\n{extra}", encoding="utf-8")
    return home


class TestWarningsOnStderr:
    def test_the_state_prints_them_once_on_stderr_in_one_line_each(self, tmp_path: Path) -> None:
        c = Consoles()
        st = AppState(home=typo_home(tmp_path), out=c.out, err=c.err)
        assert st.cfg is st.cfg
        _ = st.table
        # the file is named, as found in the data directory
        assert c.err_buf.getvalue() == f"warning: {tmp_path / 'home' / 'config.yaml'}: unknown key router.min_saving_ratioo (did you mean min_saving_ratio?)\n"
        assert c.out_buf.getvalue() == ""

    def test_a_long_message_is_not_wrapped_and_brackets_are_not_markup(self, tmp_path: Path) -> None:
        c = Consoles()
        c.err.width = 20
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text("router:\n  '[bold]': 1\n", encoding="utf-8")
        AppState(home=home, out=c.out, err=c.err).cfg
        assert c.err_buf.getvalue() == f"warning: {home / 'config.yaml'}: unknown key router.[bold]\n"

    def test_a_clean_config_says_nothing(self, tmp_path: Path) -> None:
        c = Consoles()
        st = AppState(home=tmp_path / "home", out=c.out, err=c.err)
        _ = st.table
        assert c.err_buf.getvalue() == ""

    def test_models_the_router_cannot_price_are_named_once_the_table_is_loaded(self, tmp_path: Path) -> None:
        c = Consoles()
        home = typo_home(tmp_path, "  recommend: {light: gpt-not-a-model}\n")
        st = AppState(home=home, out=c.out, err=c.err)
        st.cfg
        assert "gpt-not-a-model" not in c.err_buf.getvalue()  # needs the table
        st.table, st.table
        lines = c.err_buf.getvalue().splitlines()
        assert lines[-1] == f"warning: {home / 'config.yaml'}: router.recommend.light: gpt-not-a-model has no verified price, so advise cannot use it"
        assert len(lines) == 2

    def test_every_command_prints_them_and_the_stdout_of_json_stays_clean(self, tmp_path: Path) -> None:
        home = typo_home(tmp_path)
        for argv in (["summary", "--json"], ["sessions", "--json"], ["models", "--json"], ["prices", "--json"], ["config"], ["ingest"]):
            result = CliRunner().invoke(app, ["--config", str(home / "config.yaml"), "--home", str(home), "--no-ingest", *argv])
            assert result.exit_code == 0, (argv, result.output)
            assert result.stderr.count(f"warning: {home / 'config.yaml'}: unknown key router.min_saving_ratioo") == 1, argv
            if "--json" in argv:
                json.loads(result.stdout)
                assert "config.yaml" not in result.stdout


class TestTheFileIsNamed:
    def test_the_path_is_the_one_the_person_gave(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "typo.yaml").write_text("router:\n  min_saving_ratioo: 1.3\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        c = Consoles()
        AppState(config_path=Path("typo.yaml"), home=tmp_path / "home", out=c.out, err=c.err).cfg
        assert c.err_buf.getvalue() == "warning: typo.yaml: unknown key router.min_saving_ratioo (did you mean min_saving_ratio?)\n"

    def test_the_command_line_says_the_same(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "typo.yaml").write_text("timezon: UTC\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = CliRunner().invoke(app, ["--config", "typo.yaml", "--home", str(tmp_path / "home"), "--no-ingest", "config"])
        assert result.exit_code == 0 and "warning: typo.yaml: unknown key timezon (did you mean timezone?)" in result.stderr
        assert "config.yaml:" not in result.stderr

    def test_without_a_file_it_is_the_built_in_config(self, tmp_path: Path) -> None:
        c = Consoles()
        st = AppState(home=tmp_path / "home", out=c.out, err=c.err)
        assert st.config_name() == "built-in config"
        st._warn(["router.recommend.light: x has no verified price, so advise cannot use it"])
        assert c.err_buf.getvalue() == "warning: built-in config: router.recommend.light: x has no verified price, so advise cannot use it\n"


# ---- every setting is looked at when the config loads ------------------------------------------------------------------
def commands_fail_alike(tmp_path: Path, body: str, message: str) -> None:
    """With the logs scanned first (ingest) and without (--no-ingest) a bad setting is the same ``error:`` line and exit code 2."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text(body, encoding="utf-8")
    for argv in (["summary"], ["sessions"], ["models"], ["advise"], ["report"], ["ingest"], ["config"], ["summary", "--json"]):
        for globals_ in ([], ["--no-ingest"]):
            result = CliRunner().invoke(app, ["--config", str(home / "config.yaml"), "--home", str(home), *globals_, *argv])
            assert result.exit_code == 2, (globals_, argv, result.output)
            assert result.stderr.strip().splitlines()[-1] == f"error: {message}", (globals_, argv, result.stderr)
            assert result.stdout == "", (globals_, argv)
            assert "Traceback" not in result.output


class TestEverySettingIsCheckedAtLoad:
    @pytest.mark.parametrize(
        ("body", "message"),
        [
            ("idle_gap_minutes: soon\n", "idle_gap_minutes must be a number of minutes, got 'soon'"),
            ("idle_gap_minutes: -5\n", "idle_gap_minutes must not be negative, got -5"),
            ("idle_gap_minutes: [1, 2]\n", "idle_gap_minutes must be a number of minutes, got a list"),
            ("timezone: Nowhere/Land\n", "unknown timezone 'Nowhere/Land'"),
            ("timezone: [UTC]\n", 'timezone must be "local", "UTC" or an IANA name, got a list'),
            ("router:\n  min_saving_ratio: fast\n", "router.min_saving_ratio must be a number, got 'fast'"),
            ("router:\n  min_saving_ratio: 0.5\n", "router.min_saving_ratio must be at least 1, got 0.5"),
            ("router:\n  advise: {min_cost_cents: -1}\n", "router.advise.min_cost_cents must be a number of at least 0, got -1"),
            ("router:\n  tiers: {light_max: 7, medium_max: 3}\n", "router.tiers: light_max (7) must not be above medium_max (3)"),
            ("router:\n  tiers: {light_max: 1.5}\n", "router.tiers.light_max must be a whole number, got 1.5"),
            ("router:\n  signals:\n    tool_calls: {medium: 50, heavy: 10}\n", "router.signals.tool_calls: medium (50) must not be above heavy (10)"),
            ("router:\n  run:\n    keyword_points: many\n", "router.run.keyword_points must be a number, got 'many'"),
            ("router:\n  run:\n    prompt_chars: {medium: lots}\n", "router.run.prompt_chars.medium must be a number, got 'lots'"),
            ("database: 5\n", "database must be a path (text), got 5"),
            ("sources: {claude: 7}\n", "sources.claude must be a list of directories"),
        ],
    )
    def test_a_bad_value_stops_every_command_the_same_way_whether_or_not_the_logs_are_read(self, tmp_path: Path, body: str, message: str) -> None:
        commands_fail_alike(tmp_path, body, message)

    def test_the_defaults_and_the_documented_file_pass(self, tmp_path: Path) -> None:
        Config.load(home=tmp_path).check()
        load(tmp_path, DEFAULT_CONFIG_YAML).check()

    def test_a_value_the_router_reads_as_it_is_written_passes(self, tmp_path: Path) -> None:
        load(tmp_path, "router:\n  min_saving_ratio: 1\n  advise: {min_cost_cents: 0.5}\n  tiers: {light_max: 0, medium_max: 10}\n"
                       "  signals:\n    tool_calls: {medium: 25.5, heavy: 25.5}\n  run:\n    keyword_points: 0\n").check()

    def test_a_node_of_the_wrong_shape_is_left_to_the_accessors(self, tmp_path: Path) -> None:
        for body in ("router: 5\n", "router:\n  tiers: [1, 2]\n", "router:\n  signals: x\n", "router:\n  run: 3\n"):
            load(tmp_path, body).check()

    def test_config_init_still_replaces_a_config_that_is_broken(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text("idle_gap_minutes: soon\n", encoding="utf-8")
        result = CliRunner().invoke(app, ["--home", str(home), "config", "--init", "--force"])
        assert result.exit_code == 0, result.output
        Config.load(home=home).check()


# ---- the models the router is pointed at -----------------------------------------------------------------------------
@pytest.fixture
def table(make_table: Callable[..., PriceTable]) -> PriceTable:
    return make_table(
        {"good": {"input": 1, "output": 2}, "half": {"input": "unverified", "output": 2}},
        aliases={"nickname": "good"},
    )


def cfg_with(tmp_path: Path, recommend: Any = None, models: Any = None) -> Config:
    """Only these model targets: the defaults name models the test table does not have."""
    router = {"recommend": recommend, "run": {"models": models}}
    return Config(home=tmp_path, data={**DEFAULT_CONFIG, "router": router})


class TestValidate:
    def test_the_defaults_name_models_the_builtin_table_prices(self, tmp_path: Path) -> None:
        builtin = load_pricing(Config.load(home=tmp_path))
        router = DEFAULT_CONFIG["router"]
        targets = [m for m in router["recommend"].values() if m] + [m for tiers in router["run"]["models"].values() for m in tiers.values()]
        assert targets and all(builtin.priced(m) for m in targets)

    def test_a_target_left_at_its_default_is_not_reported_even_when_the_table_lacks_it(self, tmp_path: Path, table: PriceTable) -> None:
        assert validate(Config.load(home=tmp_path), table) == []
        cfg = Config(home=tmp_path, data={**DEFAULT_CONFIG, "router": {**DEFAULT_CONFIG["router"], "recommend": {"light": "gpt-6.1-sol", "medium": "nope"}}})
        assert validate(cfg, table) == ["router.recommend.medium: nope has no verified price, so advise cannot use it"]

    def test_a_recommendation_nobody_prices_is_named(self, tmp_path: Path, table: PriceTable) -> None:
        cfg = cfg_with(tmp_path, recommend={"light": "gpt-not-a-model", "medium": "good", "heavy": None})
        assert validate(cfg, table) == ["router.recommend.light: gpt-not-a-model has no verified price, so advise cannot use it"]

    def test_unverified_models_are_named_and_aliases_and_any_case_are_priced(self, tmp_path: Path, table: PriceTable) -> None:
        cfg = cfg_with(tmp_path, recommend={"light": "half", "medium": "nickname", "heavy": "GOOD"})
        assert validate(cfg, table) == ["router.recommend.light: half has no verified price, so advise cannot use it"]

    def test_the_models_run_would_pick_are_checked_per_agent_and_tier(self, tmp_path: Path, table: PriceTable) -> None:
        cfg = cfg_with(tmp_path, models={"claude": {"light": "good", "heavy": "nope"}, "aider": {"medium": "half"}})
        assert validate(cfg, table) == [
            "router.run.models.claude.heavy: nope has no verified price, so run cannot estimate its cost",
            "router.run.models.aider.medium: half has no verified price, so run cannot estimate its cost",
        ]

    def test_nulls_and_shapes_the_accessors_reject_are_left_alone(self, tmp_path: Path, table: PriceTable) -> None:
        for value in (None, "x", ["a"], {"light": None, "medium": "", "heavy": 5}):
            assert validate(cfg_with(tmp_path, recommend=value, models={"claude": value}), table) == []
        assert validate(cfg_with(tmp_path, models="x"), table) == []


# ---- log directories the agents can move -----------------------------------------------------------------------------
class TestRelocatedSources:
    def test_claude_config_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        cfg = Config.load(home=tmp_path)
        assert cfg.sources("claude") == [tmp_path / "cc" / "projects"]

    def test_codex_home_has_both_directories(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
        assert Config.load(home=tmp_path).sources("codex") == [tmp_path / "cx" / "sessions", tmp_path / "cx" / "archived_sessions"]

    def test_gemini_cli_home_is_the_home_that_gets_dot_gemini(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gm"))
        assert Config.load(home=tmp_path).sources("gemini") == [tmp_path / "gm" / ".gemini" / "tmp"]

    def test_each_agent_follows_its_own_variable_only(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
        cfg = Config.load(home=tmp_path)
        home = Path.home()
        assert cfg.sources("claude") == [home / ".claude" / "projects"]
        assert cfg.sources("gemini") == [home / ".gemini" / "tmp"]

    def test_a_source_the_user_set_wins_over_the_variable(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for variable in RELOCATION_VARIABLES:
            monkeypatch.setenv(variable, str(tmp_path / "moved"))
        cfg = load(tmp_path, "sources:\n  claude: [mine]\n  codex: []\n  gemini: null\n")
        assert cfg.sources("claude") == [tmp_path / "mine"]
        assert cfg.sources("codex") == [] and cfg.sources("gemini") == []

    def test_a_config_built_in_code_with_its_own_sources_is_not_redirected(self, make_config: Callable[..., Config], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "moved"))
        assert make_config(sources={"claude": [str(tmp_path / "mine")]}).sources("claude") == [tmp_path / "mine"]

    def test_tilde_and_relative_values_and_empty_ones(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~/elsewhere")
        assert Config.load(home=tmp_path).sources("claude") == [Path.home() / "elsewhere" / "projects"]
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "rel/dir")
        assert Config.load(home=tmp_path).sources("claude") == [Path.cwd() / "rel" / "dir" / "projects"]
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "  ")
        assert Config.load(home=tmp_path).sources("claude") == [Path.home() / ".claude" / "projects"]

    def test_the_variables_are_read_when_asked_not_when_the_config_loads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = Config.load(home=tmp_path)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "late"))
        assert cfg.sources("codex")[0] == tmp_path / "late" / "sessions"

    def test_the_documented_file_names_the_variables(self) -> None:
        for variable in RELOCATION_VARIABLES:
            assert variable in DEFAULT_CONFIG_YAML

    def test_the_ingest_reads_where_the_variable_points(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shutil.copytree(FIXTURE_CONFIG.parent / "claude" / "projects", tmp_path / "cc" / "projects")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
        with Store(tmp_path / "test.db") as store:
            report = ingest(Config.load(home=tmp_path / "home"), store, ["claude"])
        assert report.files_parsed == 1 and report.sessions == 1


# ---- the idle gap ----------------------------------------------------------------------------------------------------
class TestIdleGap:
    def test_the_documented_file_promises_a_re_read_and_the_ingest_keeps_it(self, tmp_path: Path) -> None:
        assert "changing it re-reads the logs automatically" in DEFAULT_CONFIG_YAML
        logs = FIXTURE_CONFIG.parent

        def ingest_with(minutes: int) -> tuple[int, int]:  # (files parsed, files unchanged)
            cfg = load(tmp_path, f"timezone: UTC\nidle_gap_minutes: {minutes}\nsources:\n  claude: [{logs}/claude/projects]\n")
            report = ingest(cfg, store)
            return report.files_parsed, report.files_unchanged

        with Store(tmp_path / "test.db") as store:
            assert ingest_with(30) == (1, 0)
            assert ingest_with(30) == (0, 1)
            assert ingest_with(5) == (1, 0)


# ---- a pricing file the user named -----------------------------------------------------------------------------------
class TestConfiguredPricingFile:
    def test_a_missing_file_is_an_error_naming_it(self, tmp_path: Path) -> None:
        cfg = load(tmp_path, "pricing_file: prices.yaml\n")
        with pytest.raises(PricingError) as err:
            load_pricing(cfg)
        assert str(err.value) == f"pricing_file {tmp_path / 'prices.yaml'} does not exist"

    def test_the_config_command_shows_the_missing_file_as_missing(self, tmp_path: Path) -> None:
        (tmp_path / "config.yaml").write_text("pricing_file: prices.yaml\n", encoding="utf-8")
        argv = ["--config", str(tmp_path / "config.yaml"), "--home", str(tmp_path), "--no-ingest", "config"]
        shown = CliRunner().invoke(app, argv).output
        assert f"pricing     : {tmp_path / 'prices.yaml'}  (missing)" in shown
        (tmp_path / "prices.yaml").write_text("models:\n  only: {input: 1, output: 2}\n", encoding="utf-8")
        assert f"pricing     : {tmp_path / 'prices.yaml'}\n" in CliRunner().invoke(app, argv).output
        (tmp_path / "config.yaml").write_text("pricing_file: null\n", encoding="utf-8")
        assert "pricing     : built-in table" in CliRunner().invoke(app, argv).output

    def test_the_implicit_file_stays_optional(self, tmp_path: Path) -> None:
        assert load_pricing(load(tmp_path, "pricing_file: null\n")).path is not None
        assert not (tmp_path / "pricing.yaml").exists()

    def test_an_existing_file_is_read(self, tmp_path: Path) -> None:
        (tmp_path / "prices.yaml").write_text("models:\n  only: {input: 1, output: 2}\n", encoding="utf-8")
        assert list(load_pricing(load(tmp_path, "pricing_file: prices.yaml\n")).models) == ["only"]

    def test_an_explicit_path_needs_no_configured_file(self, tmp_path: Path) -> None:
        explicit = tmp_path / "explicit.yaml"
        explicit.write_text("models:\n  m: {input: 1, output: 2}\n", encoding="utf-8")
        assert list(load_pricing(load(tmp_path, "pricing_file: missing.yaml\n"), explicit).models) == ["m"]

    def test_the_command_ends_with_exit_code_2_and_one_line(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        home = tmp_path / "home"
        home.mkdir()
        (home / "config.yaml").write_text("pricing_file: nowhere.yaml\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["agentfare", "--home", str(home), "--no-ingest", "prices"])
        with pytest.raises(SystemExit) as ended:
            cli._entry()
        captured = capsys.readouterr()
        assert (ended.value.code, captured.out) == (2, "")
        assert captured.err == f"error: pricing_file {home / 'nowhere.yaml'} does not exist\n"


# ---- packaging -------------------------------------------------------------------------------------------------------
class TestPackagingMetadata:
    @pytest.fixture
    def project(self) -> dict[str, Any]:
        return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    def test_the_license_is_an_spdx_expression_with_its_file(self, project: dict[str, Any]) -> None:
        meta = project["project"]
        assert meta["license"] == "MIT" and meta["license-files"] == ["LICENSE"] and (ROOT / "LICENSE").is_file()
        assert not [c for c in meta["classifiers"] if c.startswith("License ::")]

    def test_the_build_backend_understands_it(self, project: dict[str, Any]) -> None:
        assert "setuptools>=77" in project["build-system"]["requires"]

    def test_the_packaged_data_is_listed(self, project: dict[str, Any]) -> None:
        # the demo logs of `agentfare demo` ship with the package; a glob skips a dot file, so .project_root has a pattern of its own
        assert project["tool"]["setuptools"]["package-data"]["agentfare"] == [
            "data/*.yaml", "templates/*", "demo_data/**/*", "demo_data/gemini/tmp/*/.project_root"]
        assert yaml.safe_load((ROOT / "src" / "agentfare" / "data" / "pricing.yaml").read_text(encoding="utf-8"))["models"]
