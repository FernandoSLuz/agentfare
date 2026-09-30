"""YAML and numbers from a file somebody else may have written: a clean error, quickly, never a memory or CPU blowup.

Every bomb runs in a child process with a 2 GiB address-space limit and a 5 s timeout, so a missing check fails the test
instead of taking the machine down.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentfare.config import Config, ConfigError
from agentfare.pricing import PricingError, load_pricing, parse_pricing

GUARD = """
import resource, sys
try:
    resource.setrlimit(resource.RLIMIT_AS, (2 << 30, 2 << 30))  # a MemoryError, not a frozen machine, when a bomb is expanded
except (ValueError, OSError):  # macOS does not let a process lower this limit; the 5 s timeout still catches a bomb
    pass
"""


def alias_bomb(levels: int = 10, width: int = 10, indent: str = "  ") -> list[str]:
    """``levels`` anchors, each a list of ``width`` references to the one before: 10 ** 10 values once expanded."""
    lines = [f"{indent}a0: &a0 [{', '.join(['x'] * width)}]"]
    lines += [f"{indent}a{i}: &a{i} [{', '.join([f'*a{i - 1}'] * width)}]" for i in range(1, levels)]
    return lines


def run_guarded(code: str, *args: str, timeout: float = 5) -> tuple[subprocess.CompletedProcess[str], float]:
    started = time.monotonic()
    try:
        done = subprocess.run([sys.executable, "-c", GUARD + code, *args], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail(f"still running after {timeout} s: the alias bomb was expanded")
    return done, time.monotonic() - started


CONFIG_CODE = """
from agentfare.config import Config, ConfigError
try:
    cfg = Config.load(sys.argv[1], home=sys.argv[2])
    cfg.idle_gap_seconds, cfg.tz, cfg.db_path, cfg.pricing_override_path
    [cfg.sources(a) for a in ("claude", "codex", "gemini")]
except ConfigError as exc:
    print("ConfigError", str(exc)[:300])
    sys.exit(0)
print("no error")
sys.exit(1)
"""


@pytest.mark.parametrize(
    "line",
    [
        "idle_gap_minutes: *a9",
        "timezone: *a9",
        "database: *a9",
        "pricing_file: *a9",
        "sources: {claude: *a9}",
        "sources: {claude: [*a9]}",
        "router: {min_saving_ratio: *a9}",
        "router: {signals: {tool_calls: {medium: *a9}}}",
        "agents: {claude: {commands: *a9}}",
    ],
)
def test_a_config_alias_bomb_is_a_config_error_not_a_blowup(tmp_path: Path, line: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("anchors:\n" + "\n".join(alias_bomb()) + f"\n{line}\n", encoding="utf-8")
    done, took = run_guarded(CONFIG_CODE, str(config), str(tmp_path))
    assert done.returncode == 0 and done.stdout.startswith("ConfigError"), (done.stdout, done.stderr[-500:])
    assert took < 5


def test_a_recursive_alias_is_a_config_error(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("idle_gap_minutes: &loop [*loop]\n", encoding="utf-8")
    done, _ = run_guarded(CONFIG_CODE, str(config), str(tmp_path))
    assert done.returncode == 0 and done.stdout.startswith("ConfigError"), (done.stdout, done.stderr[-500:])


def test_deep_nesting_is_a_config_error(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("anchors: " + "[" * 500 + "]" * 500 + "\n", encoding="utf-8")
    done, _ = run_guarded(CONFIG_CODE, str(config), str(tmp_path))
    assert done.returncode == 0 and done.stdout.startswith("ConfigError"), (done.stdout, done.stderr[-500:])


def test_an_ordinary_config_with_a_few_aliases_still_loads(tmp_path: Path) -> None:
    from agentfare.config import Config

    config = tmp_path / "config.yaml"
    config.write_text(
        "timezone: UTC\nrouter:\n  run:\n    models:\n      claude: &m {light: a, medium: a, heavy: b}\n      codex: *m\n      gemini: *m\n", encoding="utf-8"
    )
    cfg = Config.load(config, home=tmp_path)
    assert cfg.get("router.run.models.codex.heavy") == "b"


class TestConfigValues:
    @pytest.mark.parametrize("raw", ["[1, 2]", "{a: 1}", "true", "2026-09-30", "'x'", "'1e5000000'", "1.0e+400", "100000000", "'" + "9" * 100 + "'"])
    def test_idle_gap_must_be_a_number_of_a_sane_size(self, tmp_path: Path, raw: str) -> None:
        from agentfare.config import Config, ConfigError

        config = tmp_path / "c.yaml"
        config.write_text(f"idle_gap_minutes: {raw}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="idle_gap_minutes"):
            Config.load(config, home=tmp_path).idle_gap_seconds

    @pytest.mark.parametrize(("raw", "seconds"), [("30", 1800), ("1.15", 69), ("'7'", 420), ("0", 0), ("1000000", 60_000_000)])
    def test_idle_gap_still_takes_numbers_and_numeric_text(self, tmp_path: Path, raw: str, seconds: int) -> None:
        from agentfare.config import Config

        config = tmp_path / "c.yaml"
        config.write_text(f"idle_gap_minutes: {raw}\n", encoding="utf-8")
        assert Config.load(config, home=tmp_path).idle_gap_seconds == seconds

    @pytest.mark.parametrize("raw", ["[UTC]", "{a: 1}", "5", "true", "'" + "x" * 200 + "'"])
    def test_timezone_must_be_short_text(self, tmp_path: Path, raw: str) -> None:
        from agentfare.config import Config, ConfigError

        config = tmp_path / "c.yaml"
        config.write_text(f"timezone: {raw}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="timezone"):
            Config.load(config, home=tmp_path).tz

    @pytest.mark.parametrize("key", ["database", "pricing_file"])
    @pytest.mark.parametrize("raw", ["[a, b]", "{a: 1}", "5", "true", "'" + "x" * 5000 + "'"])
    def test_a_path_setting_must_be_text_of_a_sane_length(self, tmp_path: Path, key: str, raw: str) -> None:
        from agentfare.config import Config, ConfigError

        config = tmp_path / "c.yaml"
        config.write_text(f"{key}: {raw}\n", encoding="utf-8")
        cfg = Config.load(config, home=tmp_path)
        with pytest.raises(ConfigError, match=key):
            cfg.db_path if key == "database" else cfg.pricing_override_path

    def test_a_source_path_that_is_absurdly_long_is_a_config_error(self, tmp_path: Path) -> None:
        from agentfare.config import Config, ConfigError

        config = tmp_path / "c.yaml"
        config.write_text("sources:\n  claude: ['" + "x" * 5000 + "']\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="sources.claude"):
            Config.load(config, home=tmp_path).sources("claude")


# ---- the price table ----------------------------------------------------------------------------------
PRICING_CODE = """
from agentfare.pricing import PricingError, load_pricing
from pathlib import Path
try:
    load_pricing(path=Path(sys.argv[1]))
except PricingError as exc:
    print("PricingError", str(exc)[:300])
    sys.exit(0)
print("no error")
sys.exit(1)
"""


@pytest.mark.parametrize("field", ["input: *a9", "cached_input: *a9", "output: *a9", "provider: *a9", "source: *a9", "checked: *a9", "note: *a9"])
def test_a_price_table_alias_bomb_is_a_pricing_error_not_a_blowup(tmp_path: Path, field: str) -> None:
    table = tmp_path / "pricing.yaml"
    base = ["schema: 1", "anchors:", *alias_bomb(), "models:", "  m-1:", "    input: 1.0", "    output: 2.0", f"    {field}"]
    table.write_text("\n".join(base) + "\n", encoding="utf-8")
    done, took = run_guarded(PRICING_CODE, str(table))
    assert done.returncode == 0 and done.stdout.startswith("PricingError"), (done.stdout, done.stderr[-500:])
    assert took < 5


def test_an_alias_bomb_in_python_data_is_refused_before_any_conversion() -> None:
    code = """
from agentfare.pricing import PricingError, parse_pricing
bomb = ["x"] * 10
for _ in range(9):
    bomb = [bomb] * 10
for field in ("input", "cached_input", "output", "cache_write", "cache_write_1h", "provider", "source", "checked", "note"):
    entry = {"input": 1, "output": 2, field: bomb}
    try:
        parse_pricing({"models": {"m": entry}})
    except PricingError as exc:
        assert "m." + field in str(exc), str(exc)
    else:
        raise SystemExit("no error for " + field)
print("ok")
"""
    done, _ = run_guarded(code)
    assert done.returncode == 0 and done.stdout.strip() == "ok", (done.stdout, done.stderr[-500:])


def entry(**fields: Any) -> dict[str, Any]:
    return {"schema": 1, "models": {"m": {"input": 1, "output": 2, **fields}}}


ABSURD = [
    pytest.param(1_000_000.01, id="float-just-over"),
    pytest.param("1000000.01", id="text-just-over"),
    pytest.param(2_000_000, id="int-over"),
    pytest.param("1e7", id="1e7"),
    pytest.param("1e100000", id="huge-exponent"),
    pytest.param("1E+100000", id="huge-exponent-signed"),
    pytest.param(10**5000, id="5000-digit-int"),
    pytest.param("9" * 5000, id="5000-digit-text"),
    pytest.param(float("inf"), id="infinity"),
    pytest.param("-1", id="negative"),
    pytest.param("1" + "0" * 70, id="1e70"),
    pytest.param("0." + "0" * 150 + "1", id="151-decimals"),
    pytest.param("1." + "0" * 120, id="121-digits"),
    pytest.param("1e-100000", id="tiny-exponent"),
]


@pytest.mark.parametrize("value", ABSURD)
def test_an_absurd_price_is_a_pricing_error_naming_the_field(value: Any) -> None:
    for field in ("input", "cached_input", "output", "cache_write", "cache_write_1h"):
        with pytest.raises(PricingError, match=f"m.{field}"):
            parse_pricing(entry(**{field: value}))


def test_an_absurd_price_in_a_yaml_file_is_a_pricing_error(tmp_path: Path) -> None:
    from agentfare.pricing import load_pricing

    for text in ("1.0e+100000", "1.0e+999999999999", "99999999999999999999999999", "0." + "0" * 150 + "1"):
        path = tmp_path / "p.yaml"
        path.write_text(f"models:\n  m:\n    input: {text}\n    output: 2\n", encoding="utf-8")
        with pytest.raises(PricingError, match="m.input"):
            load_pricing(path=path)


@pytest.mark.parametrize("value", [1_000_000, "1000000", 0, 0.000001, "0.000001", "unverified", " Unverified ", None, 2.5, 10, "0.0049999999999999999999999999999999999999"])
def test_a_reasonable_price_is_fine(value: Any) -> None:
    parse_pricing(entry(cached_input=value))


@pytest.mark.parametrize("value", [[1], {"a": 1}, (1,), b"1", True, False, object()])
def test_a_price_must_be_a_number_or_unverified(value: Any) -> None:
    with pytest.raises(PricingError, match="m.input"):
        parse_pricing(entry(input=value))


@pytest.mark.parametrize(("field", "limit"), [("provider", 100), ("source", 2000), ("checked", 40), ("note", 5000)])
def test_text_fields_are_capped_and_must_be_scalars(field: str, limit: int) -> None:
    parse_pricing(entry(**{field: "x" * limit}))
    with pytest.raises(PricingError, match=f"m.{field}"):
        parse_pricing(entry(**{field: "x" * (limit + 1)}))
    for bad in ([1, 2], {"a": 1}, b"x", (1,)):
        with pytest.raises(PricingError, match=f"m.{field}"):
            parse_pricing(entry(**{field: bad}))


def test_a_dated_checked_field_and_a_number_as_text_still_work() -> None:
    import datetime

    table = parse_pricing(entry(checked=datetime.date(2026, 9, 30), note=12, provider="acme"))
    assert table.models["m"].checked == "2026-09-30" and table.models["m"].note == "12"


def test_model_ids_and_aliases_are_capped() -> None:
    with pytest.raises(PricingError, match="model id"):
        parse_pricing({"models": {"x" * 300: {"input": 1, "output": 2}}})
    with pytest.raises(PricingError, match="alias"):
        parse_pricing({"models": {"m": {"input": 1, "output": 2}}, "aliases": {"x" * 300: "m"}})
    with pytest.raises(PricingError, match="alias"):
        parse_pricing({"models": {"m": {"input": 1, "output": 2}}, "aliases": {"a": ["m"]}})


# ---- a key written twice in a mapping is an error --------------------------------------------------------------------------
class TestDuplicateKeys:
    """PyYAML keeps the last of two equal keys and says nothing, so a price or a setting can be overridden by accident."""

    def config(self, tmp_path: Path, text: str) -> Config:
        path = tmp_path / "config.yaml"
        path.write_text(text)
        return Config.load(path, home=tmp_path)

    def test_a_config_with_a_key_twice_names_the_key_and_the_line(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"invalid YAML: duplicate key timezone at line 5$"):
            self.config(tmp_path, "timezone: UTC\nrouter:\n  run:\n    x: 1\ntimezone: local\n")

    def test_a_nested_and_a_flow_mapping_are_checked_too(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="duplicate key idle_gap_minutes at line 4"):
            self.config(tmp_path, "agents:\n  claude: {commands: [claude]}\nidle_gap_minutes: 5\nidle_gap_minutes: 6\n")
        with pytest.raises(ConfigError, match="duplicate key commands at line 2"):
            self.config(tmp_path, "agents:\n  claude: {commands: [claude], commands: [cl]}\n")

    def test_the_first_line_of_the_document_and_a_very_long_key(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="duplicate key a at line 2"):
            self.config(tmp_path, "a: 1\na: 2\n")
        with pytest.raises(ConfigError, match=r"duplicate key x{37}\.\.\. at line 2"):
            self.config(tmp_path, f"{'x' * 500}: 1\n{'x' * 500}: 2\n")

    def test_a_number_written_two_ways_is_the_same_key(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="duplicate key 1 at line 2"):
            self.config(tmp_path, "1: a\n1: b\n")

    def test_the_same_key_in_different_mappings_and_an_override_through_a_merge_are_fine(self, tmp_path: Path) -> None:
        cfg = self.config(tmp_path, "base: &b {input: 1, output: 2}\nmore: {<<: *b, input: 3}\nother: {input: 4}\n")
        assert cfg.data["more"] == {"input": 3, "output": 2} and cfg.data["other"] == {"input": 4}

    def test_a_config_without_a_repeated_key_loads_as_before(self, tmp_path: Path) -> None:
        assert self.config(tmp_path, "timezone: UTC\nidle_gap_minutes: 7\n").data["idle_gap_minutes"] == 7

    def test_the_default_config_and_the_packaged_price_table_have_no_key_twice(self) -> None:
        from agentfare.pricing import builtin_path

        Config.load(home=Path("/nonexistent-home-for-defaults"))
        assert load_pricing(path=builtin_path()).models

    def test_a_price_table_with_a_field_twice_names_the_field_and_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.yaml"
        path.write_text("models:\n  m-1:\n    provider: a\n    input: 1\n    input: 2\n    output: 3\n")
        with pytest.raises(PricingError, match=r"cannot read .*pricing\.yaml: duplicate key input at line 5$"):
            load_pricing(path=path)

    def test_a_price_table_with_a_model_or_an_alias_twice_names_it(self, tmp_path: Path) -> None:
        path = tmp_path / "pricing.yaml"
        path.write_text("models:\n  m-1: {input: 1, output: 2}\n  m-1: {input: 3, output: 4}\n")
        with pytest.raises(PricingError, match="duplicate key m-1 at line 3"):
            load_pricing(path=path)
        path.write_text("models:\n  m-1: {input: 1, output: 2}\naliases:\n  old: m-1\n  old: m-1\n")
        with pytest.raises(PricingError, match="duplicate key old at line 5"):
            load_pricing(path=path)

    def test_the_command_line_reports_it_as_one_line(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.yaml"
        cfg.write_text("timezone: UTC\ntimezone: UTC\n")
        done = subprocess.run([sys.executable, "-c", "from agentfare.cli import _entry; _entry()", "--config", str(cfg), "config"],
                              capture_output=True, text=True, timeout=60, check=False)
        assert done.returncode == 2 and done.stderr.strip().splitlines() == [f"error: {cfg}: invalid YAML: duplicate key timezone at line 2"]
