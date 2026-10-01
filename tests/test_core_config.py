import os
import shutil
import time
import zoneinfo
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
import yaml

from agentfare import config as config_module
from agentfare.config import (
    DEFAULT_CONFIG,
    DEFAULT_CONFIG_YAML,
    Config,
    ConfigError,
    day_of,
    deep_merge,
    default_home,
    fmt_iso,
    iso_week_of,
    now_utc,
    parse_iso,
    resolve_tz,
    week_bounds,
)

UTC = timezone.utc


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        pytest.skip(f"no time zone database for {name}")


def write_config(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


class TestDefaults:
    def test_default_config_is_the_parsed_default_yaml(self) -> None:
        assert DEFAULT_CONFIG == yaml.safe_load(DEFAULT_CONFIG_YAML)

    def test_defaults_without_a_config_file(self) -> None:
        cfg = Config.load()
        assert cfg.config_path is None
        assert cfg.data == DEFAULT_CONFIG
        assert cfg.idle_gap_seconds == 30 * 60
        assert cfg.get("router.tiers.light_max") == DEFAULT_CONFIG["router"]["tiers"]["light_max"]
        assert cfg.get("router.nope.deeper", "fallback") == "fallback"
        assert cfg.get("timezone") == "local"

    def test_default_sources_expand_the_tilde_against_home(self) -> None:
        home = Path(os.environ["HOME"])
        cfg = Config.load()
        assert cfg.sources("claude") == [home / ".claude" / "projects"]
        assert cfg.sources("codex") == [home / ".codex" / "sessions", home / ".codex" / "archived_sessions"]
        assert cfg.sources("gemini") == [home / ".gemini" / "tmp"]
        assert cfg.sources("unknown-agent") == []

    def test_loading_never_mutates_the_shared_defaults(self) -> None:
        cfg = Config.load()
        cfg.data["router"]["tiers"]["light_max"] = 99
        cfg.data["sources"]["claude"].append("/x")
        assert Config.load().data == DEFAULT_CONFIG
        assert DEFAULT_CONFIG == yaml.safe_load(DEFAULT_CONFIG_YAML)  # still what the documented file says


class TestDeepMerge:
    def test_nested_dicts_are_merged_key_by_key(self) -> None:
        base = {"a": {"x": 1, "y": {"p": 1, "q": 2}}, "b": 1}
        merged = deep_merge(base, {"a": {"y": {"q": 9}, "z": 3}})
        assert merged == {"a": {"x": 1, "y": {"p": 1, "q": 9}, "z": 3}, "b": 1}

    def test_lists_and_scalars_are_replaced_not_merged(self) -> None:
        assert deep_merge({"l": [1, 2], "s": 1}, {"l": [3], "s": None}) == {"l": [3], "s": None}

    def test_dict_replaces_a_scalar_and_vice_versa(self) -> None:
        assert deep_merge({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}
        assert deep_merge({"a": {"b": 2}}, {"a": 1}) == {"a": 1}

    def test_inputs_are_not_mutated_and_result_is_independent(self) -> None:
        base = {"a": {"x": [1]}}
        override = {"a": {"y": [2]}}
        merged = deep_merge(base, override)
        merged["a"]["x"].append(9)
        merged["a"]["y"].append(9)
        assert base == {"a": {"x": [1]}}
        assert override == {"a": {"y": [2]}}

    def test_a_key_left_empty_keeps_the_default_mapping(self) -> None:
        base = {"a": {"x": 1, "y": {"p": 1}}, "b": {"z": 2}}
        assert deep_merge(base, {"a": None}) == base
        assert deep_merge(base, {"a": {"y": None}}) == base  # also one level down
        assert deep_merge(base, {"a": None, "b": {"z": 3}}) == {"a": base["a"], "b": {"z": 3}}

    def test_null_still_sets_null_over_scalars_and_lists_and_new_keys(self) -> None:
        assert deep_merge({"l": [1], "s": "x", "n": None}, {"l": None, "s": None, "n": None, "new": None}) == {
            "l": None, "s": None, "n": None, "new": None,
        }

    def test_empty_or_none_override(self) -> None:
        assert deep_merge({"a": 1}, {}) == {"a": 1}
        assert deep_merge({"a": 1}, None) == {"a": 1}  # type: ignore[arg-type]

    def test_user_file_overrides_defaults_but_keeps_the_other_keys(self, tmp_path: Path) -> None:
        path = write_config(
            tmp_path / "config.yaml",
            "timezone: UTC\nrouter:\n  tiers: {light_max: 3}\nsources:\n  claude: [logs]\n",
        )
        cfg = Config.load(path, home=tmp_path)
        assert cfg.get("timezone") == "UTC"
        assert cfg.get("router.tiers") == {"light_max": 3, "medium_max": DEFAULT_CONFIG["router"]["tiers"]["medium_max"]}
        assert cfg.get("router.min_saving_ratio") == DEFAULT_CONFIG["router"]["min_saving_ratio"]
        assert cfg.sources("claude") == [tmp_path / "logs"]
        assert cfg.sources("codex") == [
            Path(os.environ["HOME"]) / ".codex" / "sessions",
            Path(os.environ["HOME"]) / ".codex" / "archived_sessions",
        ]


class TestEmptyKeysInTheUserFile:
    def test_sections_with_every_child_commented_out_keep_their_defaults(self, tmp_path: Path) -> None:
        body = "sources:\n  # claude: [logs]\nrouter:\n  # tiers: {light_max: 3}\n  signals:\n    # tool_calls: {medium: 1}\nagents:\n"
        cfg = Config.load(write_config(tmp_path / "config.yaml", body), home=tmp_path)
        assert cfg.data == DEFAULT_CONFIG
        home = Path(os.environ["HOME"])
        assert cfg.sources("claude") == [home / ".claude" / "projects"]

    def test_an_empty_section_does_not_hide_the_keys_the_user_did_set(self, tmp_path: Path) -> None:
        cfg = Config.load(write_config(tmp_path / "config.yaml", "router:\n  signals:\n  tiers: {light_max: 1}\n"), home=tmp_path)
        assert cfg.get("router.signals") == DEFAULT_CONFIG["router"]["signals"]
        assert cfg.get("router.tiers.light_max") == 1

    def test_scalars_that_may_be_null_keep_working(self, tmp_path: Path) -> None:
        body = "pricing_file:\ndatabase:\ntimezone:\nidle_gap_minutes:\n"
        cfg = Config.load(write_config(tmp_path / "config.yaml", body), home=tmp_path / "h")
        assert cfg.pricing_override_path == tmp_path / "h" / "pricing.yaml"
        assert cfg.db_path == tmp_path / "h" / "agentfare.db"
        assert cfg.idle_gap_seconds == 30 * 60
        assert cfg.tz == resolve_tz("local")

    def test_a_model_the_user_sets_to_null_stays_null(self, tmp_path: Path) -> None:
        body = "router:\n  recommend: {light: null}\n  run:\n    models:\n      claude: {heavy: null}\n"
        cfg = Config.load(write_config(tmp_path / "config.yaml", body), home=tmp_path)
        assert cfg.get("router.recommend") == {**DEFAULT_CONFIG["router"]["recommend"], "light": None}
        assert cfg.get("router.run.models.claude") == {**DEFAULT_CONFIG["router"]["run"]["models"]["claude"], "heavy": None}


class TestPaths:
    def test_relative_paths_resolve_against_the_config_directory_not_the_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cfgdir = tmp_path / "project" / "conf"
        path = write_config(
            cfgdir / "config.yaml",
            "database: data/fare.db\npricing_file: prices.yaml\nsources:\n  claude: [logs/claude, ../other]\n  codex: only/one\n",
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        cfg = Config.load(path, home=tmp_path / "home")
        assert cfg.config_path == path
        assert cfg.db_path == cfgdir / "data" / "fare.db"
        assert cfg.pricing_override_path == cfgdir / "prices.yaml"
        assert cfg.sources("claude") == [cfgdir / "logs" / "claude", cfgdir / ".." / "other"]
        assert cfg.sources("codex") == [cfgdir / "only" / "one"]  # a bare string is a one-item list

    def test_a_relative_config_path_keeps_working_after_the_cwd_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_config(tmp_path / "cfg" / "config.yaml", "database: fare.db\nsources: {claude: [logs]}\n")
        monkeypatch.chdir(tmp_path)
        cfg = Config.load("cfg/config.yaml", home="h")
        monkeypatch.chdir(tmp_path / "cfg")
        assert cfg.base_dir.is_absolute() and cfg.home.is_absolute()
        assert cfg.db_path == tmp_path / "cfg" / "fare.db"
        assert cfg.sources("claude") == [tmp_path / "cfg" / "logs"]

    def test_tilde_is_expanded_and_absolute_paths_are_kept(self, tmp_path: Path) -> None:
        home = Path(os.environ["HOME"])
        path = write_config(
            tmp_path / "config.yaml",
            "database: ~/elsewhere/fare.db\npricing_file: /abs/prices.yaml\nsources:\n  claude: ['~/logs', /abs/logs]\n",
        )
        cfg = Config.load(path, home=tmp_path)
        assert cfg.db_path == home / "elsewhere" / "fare.db"
        assert cfg.pricing_override_path == Path("/abs/prices.yaml")
        assert cfg.sources("claude") == [home / "logs", Path("/abs/logs")]

    def test_default_database_follows_agentfare_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTFARE_HOME", str(tmp_path / "custom-home"))
        assert default_home() == tmp_path / "custom-home"
        cfg = Config.load()
        assert cfg.home == tmp_path / "custom-home"
        assert cfg.db_path == tmp_path / "custom-home" / "agentfare.db"
        assert cfg.pricing_override_path == tmp_path / "custom-home" / "pricing.yaml"

    def test_the_config_file_is_looked_up_in_agentfare_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTFARE_HOME", str(tmp_path / "h"))
        write_config(tmp_path / "h" / "config.yaml", "idle_gap_minutes: 10\n")
        cfg = Config.load()
        assert cfg.config_path == tmp_path / "h" / "config.yaml"
        assert cfg.idle_gap_seconds == 600

    def test_explicit_home_beats_the_environment(self, tmp_path: Path) -> None:
        cfg = Config.load(home=tmp_path / "explicit")
        assert cfg.home == tmp_path / "explicit"
        assert cfg.db_path == tmp_path / "explicit" / "agentfare.db"

    def test_spelling_out_the_default_database_still_follows_home(self, tmp_path: Path) -> None:
        path = write_config(tmp_path / "config.yaml", "database: ~/.agentfare/agentfare.db\n")
        assert Config.load(path, home=tmp_path / "demo").db_path == tmp_path / "demo" / "agentfare.db"

    def test_a_custom_database_is_not_redirected(self, tmp_path: Path) -> None:
        path = write_config(tmp_path / "config.yaml", "database: mine.db\n")
        assert Config.load(path, home=tmp_path / "demo").db_path == tmp_path / "mine.db"

    def test_null_pricing_file_uses_home(self, tmp_path: Path) -> None:
        assert Config.load(home=tmp_path).pricing_override_path == tmp_path / "pricing.yaml"


class TestLoadErrors:
    def test_missing_explicit_config_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            Config.load(tmp_path / "nope.yaml", home=tmp_path)

    def test_missing_default_config_is_fine(self, tmp_path: Path) -> None:
        assert Config.load(home=tmp_path / "fresh").config_path is None

    def test_invalid_yaml_raises_config_error(self, tmp_path: Path) -> None:
        path = write_config(tmp_path / "config.yaml", "timezone: [unclosed\n")
        with pytest.raises(ConfigError, match="invalid YAML"):
            Config.load(path, home=tmp_path)

    @pytest.mark.parametrize(
        "body",
        [
            "checked: 2026-02-30\n",  # ValueError
            "a: !!timestamp 2026-99-99\n",  # ValueError
            "a: !!timestamp abc\n",  # AttributeError
            "a: !!bool maybe\n",  # KeyError
            "a: !!int 0xZZ\n",  # ValueError
            pytest.param("a: " + "[" * 5000 + "]" * 5000 + "\n", id="deeply nested"),  # RecursionError
        ],
    )
    def test_well_formed_yaml_with_an_impossible_value_is_a_config_error(self, tmp_path: Path, body: str) -> None:
        # PyYAML raises ValueError, KeyError and more instead of YAMLError for these; a mistyped date must not end in a traceback
        path = write_config(tmp_path / "config.yaml", body)
        with pytest.raises(ConfigError, match="invalid YAML"):
            Config.load(path, home=tmp_path)

    @pytest.mark.parametrize("body", ["- a\n- b\n", "just a string\n", "42\n"])
    def test_top_level_must_be_a_mapping(self, tmp_path: Path, body: str) -> None:
        path = write_config(tmp_path / "config.yaml", body)
        with pytest.raises(ConfigError, match="mapping"):
            Config.load(path, home=tmp_path)

    def test_empty_file_means_defaults(self, tmp_path: Path) -> None:
        path = write_config(tmp_path / "config.yaml", "# nothing but a comment\n")
        cfg = Config.load(path, home=tmp_path)
        assert cfg.data == DEFAULT_CONFIG
        assert cfg.config_path == path

    def test_unreadable_config_is_a_config_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="cannot read"):
            Config.load(tmp_path, home=tmp_path)  # a directory
        binary = tmp_path / "binary.yaml"
        binary.write_bytes(b"\xff\xfe\x00\x81")
        with pytest.raises(ConfigError, match="cannot read"):
            Config.load(binary, home=tmp_path)

    def test_write_default_creates_a_file_that_loads_back_as_the_defaults(self, tmp_path: Path) -> None:
        cfg = Config.load(home=tmp_path / "new" / "home")
        target = cfg.write_default()
        assert target == tmp_path / "new" / "home" / "config.yaml"
        assert target.read_text(encoding="utf-8") == DEFAULT_CONFIG_YAML
        loaded = Config.load(home=tmp_path / "new" / "home")
        assert loaded.config_path == target
        assert loaded.data == DEFAULT_CONFIG

    def test_write_default_refuses_to_overwrite(self, tmp_path: Path) -> None:
        cfg = Config.load(home=tmp_path)
        target = write_config(tmp_path / "config.yaml", "idle_gap_minutes: 5\n")
        with pytest.raises(ConfigError, match="already exists"):
            cfg.write_default()
        assert target.read_text(encoding="utf-8") == "idle_gap_minutes: 5\n"

    def test_write_default_into_a_place_that_cannot_be_written_is_a_config_error(self, tmp_path: Path) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("a file where the data directory should be")
        with pytest.raises(ConfigError, match=r"cannot write .*config\.yaml"):
            Config.load(home=blocker / "home").write_default()
        with pytest.raises(ConfigError, match="cannot write"):
            Config.load(home=tmp_path).write_default(tmp_path, overwrite=True)  # a directory is not a file to overwrite

    def test_write_default_can_overwrite_explicitly_and_take_a_path(self, tmp_path: Path) -> None:
        cfg = Config.load(home=tmp_path)
        target = write_config(tmp_path / "config.yaml", "idle_gap_minutes: 5\n")
        assert cfg.write_default(overwrite=True) == target
        assert target.read_text(encoding="utf-8") == DEFAULT_CONFIG_YAML
        other = cfg.write_default(tmp_path / "sub" / "other.yaml")
        assert other.read_text(encoding="utf-8") == DEFAULT_CONFIG_YAML


class TestValidatedAccessors:
    @pytest.mark.parametrize(
        ("value", "seconds"),
        [("30", 1800), ("0.5", 30), ("1.15", 69), ("90", 5400), ("0", 0)],
    )
    def test_idle_gap_is_exact(self, tmp_path: Path, value: str, seconds: int) -> None:
        path = write_config(tmp_path / "c.yaml", f"idle_gap_minutes: {value}\n")
        assert Config.load(path, home=tmp_path).idle_gap_seconds == seconds

    @pytest.mark.parametrize("value", ["abc", "-5", ".nan", ".inf", "true", "[1]", "1e99999999", "-1e99999999"])
    def test_invalid_idle_gap_is_a_config_error(self, tmp_path: Path, value: str) -> None:
        path = write_config(tmp_path / "c.yaml", f"idle_gap_minutes: {value}\n")
        with pytest.raises(ConfigError, match="idle_gap_minutes"):
            Config.load(path, home=tmp_path).idle_gap_seconds

    def test_null_idle_gap_and_timezone_mean_the_defaults(self, tmp_path: Path) -> None:
        path = write_config(tmp_path / "c.yaml", "idle_gap_minutes: null\ntimezone: null\n")
        cfg = Config.load(path, home=tmp_path)
        assert cfg.idle_gap_seconds == 1800
        assert cfg.tz is not None

    @pytest.mark.parametrize("body", ["sources: [a, b]\n", "sources: {claude: [1, 2]}\n", "sources: {claude: {a: b}}\n"])
    def test_malformed_sources_are_a_config_error(self, tmp_path: Path, body: str) -> None:
        cfg = Config.load(write_config(tmp_path / "c.yaml", body), home=tmp_path)
        with pytest.raises(ConfigError, match="sources"):
            cfg.sources("claude")

    def test_agent_without_sources_has_none(self, tmp_path: Path) -> None:
        cfg = Config.load(write_config(tmp_path / "c.yaml", "sources: {claude: null}\n"), home=tmp_path)
        assert cfg.sources("claude") == []


class TestResolveTz:
    def test_utc_any_case(self) -> None:
        assert resolve_tz("UTC") is UTC
        assert resolve_tz("utc") is UTC

    def test_named_zone(self) -> None:
        assert resolve_tz("America/Toronto") == zone("America/Toronto")

    @pytest.mark.parametrize("name", ["Mars/Olympus", "../etc/passwd", "None", ""])
    def test_unknown_zone_is_a_config_error(self, name: str) -> None:
        with pytest.raises(ConfigError, match="unknown timezone"):
            resolve_tz(name)

    def test_config_timezone_property(self, tmp_path: Path) -> None:
        path = write_config(tmp_path / "c.yaml", "timezone: UTC\n")
        assert Config.load(path, home=tmp_path).tz is UTC
        bad = write_config(tmp_path / "bad.yaml", "timezone: Nowhere/Land\n")
        with pytest.raises(ConfigError):
            Config.load(bad, home=tmp_path).tz

    def test_local_follows_dst_instead_of_using_todays_offset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        zone("America/Toronto")
        monkeypatch.setenv("TZ", "America/Toronto")
        tz = resolve_tz("local")
        assert day_of("2026-01-15T04:30:00Z", tz) == "2026-01-14"  # winter: UTC-5, 23:30 the day before
        assert day_of("2026-07-15T04:30:00Z", tz) == "2026-07-15"  # summer: UTC-4, 00:30 the same day
        assert tz.utcoffset(datetime(2026, 1, 15, 12)) == timedelta(hours=-5)
        assert tz.utcoffset(datetime(2026, 7, 15, 12)) == timedelta(hours=-4)

    def test_local_is_cached_per_setting_but_follows_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        zone("America/Toronto")
        zone("Asia/Tokyo")
        monkeypatch.setenv("TZ", "America/Toronto")
        assert resolve_tz("local") is resolve_tz("local")
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        assert resolve_tz("local") == ZoneInfo("Asia/Tokyo")
        monkeypatch.setenv("TZ", ":America/Toronto")  # the leading colon is a POSIX spelling of the same zone
        assert resolve_tz("local") == ZoneInfo("America/Toronto")

    def test_local_without_tz_env_reads_the_system_zone_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = next((Path(p) / "America" / "Toronto" for p in zoneinfo.TZPATH if (Path(p) / "America" / "Toronto").is_file()), None)
        if source is None:
            pytest.skip("no TZif file for America/Toronto")
        fake_localtime = tmp_path / "localtime"
        shutil.copy(source, fake_localtime)
        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(config_module, "_LOCALTIME", fake_localtime)
        tz = resolve_tz("local")
        assert day_of("2026-01-15T04:30:00Z", tz) == "2026-01-14"
        assert day_of("2026-07-15T04:30:00Z", tz) == "2026-07-15"

    @pytest.mark.parametrize("spelling", [":{}", "{}"])
    def test_tz_naming_a_zone_file_keeps_the_dst_rules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spelling: str
    ) -> None:
        # TZ=:/etc/localtime is how many distributions and container images spell "the system zone"
        source = next((Path(p) / "America" / "Toronto" for p in zoneinfo.TZPATH if (Path(p) / "America" / "Toronto").is_file()), None)
        if source is None:
            pytest.skip("no TZif file for America/Toronto")
        zone_file = tmp_path / "some-zone"
        shutil.copy(source, zone_file)
        monkeypatch.setenv("TZ", spelling.format(zone_file))
        tz = resolve_tz("local")
        assert day_of("2026-01-15T04:30:00Z", tz) == "2026-01-14"  # winter: UTC-5
        assert day_of("2026-07-15T04:30:00Z", tz) == "2026-07-15"  # summer: UTC-4

    def test_tz_naming_a_missing_zone_file_falls_back_instead_of_failing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TZ", f":{tmp_path / 'missing'}")
        assert resolve_tz("local").utcoffset(datetime(2026, 9, 30, 12)) is not None

    def test_a_tz_the_system_cannot_turn_into_an_offset_falls_back_to_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS reports an offset of -30 hours for a garbage TZ and ``astimezone()`` raises ValueError: the CLI must not crash."""

        class Broken:
            @staticmethod
            def now() -> "Broken":
                return Broken()

            def astimezone(self) -> None:
                raise ValueError("offset must be a timedelta strictly between -timedelta(hours=24) and timedelta(hours=24)")

        config_module._resolve_system_tz.cache_clear()
        monkeypatch.setattr(config_module, "datetime", Broken)
        try:
            zone, unread = config_module._resolve_system_tz("[bold]x", Path("/nonexistent/localtime"))
        finally:
            config_module._resolve_system_tz.cache_clear()
        assert zone is timezone.utc and "TZ=" in unread

    def test_local_falls_back_to_the_current_offset_when_nothing_is_known(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.setattr(config_module, "_LOCALTIME", tmp_path / "missing")
        tz = resolve_tz("local")
        assert tz.utcoffset(datetime(2026, 9, 30, 12)) is not None


class TestPosixTzRules:
    """``TZ`` as a POSIX rule string: its DST dates are read, not replaced by today's offset for the whole year."""

    @pytest.mark.parametrize(
        ("rule", "winter", "summer"),
        [
            ("EST5EDT,M3.2.0,M11.1.0", -5, -4),
            ("CET-1CEST,M3.5.0,M10.5.0/3", 1, 2),
            ("AEST-10AEDT,M10.1.0,M4.1.0/3", 11, 10),  # the southern summer is the northern winter
            ("<-03>3<-02>,M3.5.0/-2,M10.5.0/-1", -3, -2),  # a quoted name
            ("EST5EDT,J60,J300", -5, -4),  # Julian days
            ("JST-9", 9, 9),  # no DST at all
        ],
    )
    def test_the_dst_rules_are_followed(self, monkeypatch: pytest.MonkeyPatch, rule: str, winter: int, summer: int) -> None:
        monkeypatch.setenv("TZ", rule)
        tz = resolve_tz("local")
        offsets = [datetime(2026, month, 15, 12, tzinfo=UTC).astimezone(tz).utcoffset() for month in (1, 7)]
        assert offsets == [timedelta(hours=winter), timedelta(hours=summer)]

    def test_a_day_near_a_dst_change_lands_on_the_right_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "EST5EDT,M3.2.0,M11.1.0")
        tz = resolve_tz("local")
        assert day_of("2026-01-15T04:30:00Z", tz) == "2026-01-14"  # winter: 23:30 the day before
        assert day_of("2026-07-15T04:30:00Z", tz) == "2026-07-15"  # summer: 00:30 the same day
        assert iso_week_of("2026-03-08T06:30:00Z", tz) == "2026-W10"  # 01:30 on the Sunday that the clocks change

    def test_it_is_the_same_zone_as_the_named_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        toronto = zone("America/Toronto")
        monkeypatch.setenv("TZ", "EST5EDT,M3.2.0,M11.1.0")
        tz = resolve_tz("local")
        start = datetime(2026, 1, 1, tzinfo=UTC)
        for hours in range(0, 24 * 365, 6):
            instant = start + timedelta(hours=hours)
            assert instant.astimezone(tz).utcoffset() == instant.astimezone(toronto).utcoffset(), instant

    def test_a_leading_colon_is_no_different(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", ":EST5EDT,M3.2.0,M11.1.0")
        tz = resolve_tz("local")
        assert datetime(2026, 7, 15, 12, tzinfo=UTC).astimezone(tz).utcoffset() == timedelta(hours=-4)

    def test_a_rule_that_is_read_needs_no_warning(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "EST5EDT,M3.2.0,M11.1.0")
        assert Config.load(home=tmp_path).local_tz_warning() is None

    @pytest.mark.parametrize("garbage", ["garbage", "5EDT", "EST5EDT,M3.2.0", "EST5EDT,M13.2.0,M11.1.0", "x" * 500, "EST5EDT,M3.2.0,M11.1.0\nextra"])
    def test_a_string_that_is_no_rule_falls_back_and_says_so(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, garbage: str) -> None:
        monkeypatch.setenv("TZ", garbage)
        tz = resolve_tz("local")
        assert tz.utcoffset(datetime(2026, 9, 30, 12)) is not None  # still a usable zone
        warning = Config.load(home=tmp_path).local_tz_warning()
        assert warning is not None and 'timezone "local" could not be resolved to a named zone with DST rules' in warning
        assert "IANA name such as America/Toronto" in warning and "\n" not in warning

    def test_a_zone_file_that_is_missing_says_so_too(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", f":{tmp_path / 'missing'}")
        warning = Config.load(home=tmp_path).local_tz_warning()
        assert warning is not None and "TZ=" in warning and "missing" in warning

    def test_no_warning_for_a_configured_zone_nor_for_a_bare_machine(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "garbage")
        for zone_name in ("UTC", "America/Toronto"):
            cfg = Config.load(write_config(tmp_path / "c.yaml", f"timezone: {zone_name}\n"), home=tmp_path)
            assert cfg.local_tz_warning() is None, zone_name
        monkeypatch.delenv("TZ")
        monkeypatch.setattr(config_module, "_LOCALTIME", tmp_path / "missing")
        assert Config.load(home=tmp_path).local_tz_warning() is None  # nothing names a zone: UTC, not a problem

    def test_a_named_zone_in_tz_is_untouched(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TZ", "America/Toronto")
        assert resolve_tz("local") == zone("America/Toronto") and Config.load(home=tmp_path).local_tz_warning() is None

    def test_the_command_line_warns_once_on_stderr_and_not_in_the_json(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from agentfare.cli import app

        for argv in (["summary"], ["summary", "--json"], ["sessions"], ["--no-ingest", "models"]):
            result = CliRunner().invoke(app, ["--home", str(tmp_path), *argv], env={"TZ": "garbage"})
            assert result.exit_code == 0, (argv, result.output)
            assert result.stderr.count('warning: timezone "local" could not be resolved to a named zone with DST rules') == 1, argv
            assert "IANA" not in result.stdout
        quiet = CliRunner().invoke(app, ["--home", str(tmp_path), "summary"], env={"TZ": "EST5EDT,M3.2.0,M11.1.0"})
        assert quiet.exit_code == 0 and "timezone" not in quiet.stderr

    def test_the_day_split_of_a_summary_follows_the_rule(self, tmp_path: Path) -> None:
        """End to end: a session at 04:30 UTC on 15 July is the 15th under EST5EDT (00:30) and on 15 January the 14th (23:30)."""
        import json

        from typer.testing import CliRunner

        from agentfare.cli import app
        from agentfare.db import Store
        from agentfare.models import SessionRecord, Usage

        with Store(tmp_path / "agentfare.db") as store:
            for day in ("2026-01-15", "2026-07-15"):
                store.upsert_session(SessionRecord(
                    agent="claude", session_id=f"s-{day}", source_path=f"/x/{day}", project="p", started_at=f"{day}T04:30:00Z",
                    ended_at=f"{day}T04:40:00Z", wall_seconds=600, active_seconds=600, usage={"claude-sonnet-5-5": Usage(input=1000)}))
        result = CliRunner().invoke(app, ["--home", str(tmp_path), "--no-ingest", "summary", "--json"], env={"TZ": "EST5EDT,M3.2.0,M11.1.0"})
        assert [d["day"] for d in json.loads(result.stdout)["by_day"]] == ["2026-01-14", "2026-07-15"]


class TestParseIso:
    @pytest.mark.parametrize(
        "text",
        [
            "2026-09-30T10:00:00Z",
            "2026-09-30T10:00:00z",
            "2026-09-30T10:00:00+00:00",
            "2026-09-30T07:00:00-03:00",
            "2026-09-30T12:30:00+02:30",
            "2026-09-30T10:00:00",  # naive is read as UTC
            "2026-09-30 10:00:00",
            "  2026-09-30T10:00:00Z\n",
        ],
    )
    def test_forms_that_denote_the_same_instant(self, text: str) -> None:
        assert parse_iso(text) == datetime(2026, 9, 30, 10, 0, 0, tzinfo=UTC)

    def test_result_is_always_aware_utc(self) -> None:
        for text in ("2026-09-30T07:00:00-03:00", "2026-09-30T10:00:00", "2026-09-30T10:00:00Z"):
            assert parse_iso(text).utcoffset() == timedelta(0)

    @pytest.mark.parametrize(
        ("text", "micro"),
        [("2026-09-30T10:00:00.123Z", 123_000), ("2026-09-30T10:00:00.123456Z", 123_456), ("2026-09-30T10:00:00.5Z", 500_000)],
    )
    def test_fractional_seconds(self, text: str, micro: int) -> None:
        assert parse_iso(text) == datetime(2026, 9, 30, 10, 0, 0, micro, tzinfo=UTC)

    def test_fractional_seconds_with_an_offset(self) -> None:
        assert parse_iso("2026-09-30T07:00:00.250-03:00") == datetime(2026, 9, 30, 10, 0, 0, 250_000, tzinfo=UTC)

    @pytest.mark.parametrize("text", ["", "not a date", "2026-13-40T00:00:00Z"])
    def test_garbage_raises_value_error(self, text: str) -> None:
        with pytest.raises(ValueError):
            parse_iso(text)

    def test_fmt_iso_truncates_to_seconds_and_uses_z(self) -> None:
        assert fmt_iso(datetime(2026, 9, 30, 10, 0, 0, 999_999, tzinfo=UTC)) == "2026-09-30T10:00:00Z"
        assert fmt_iso(parse_iso("2026-09-30T07:00:00-03:00")) == "2026-09-30T10:00:00Z"

    def test_fmt_iso_reads_a_naive_datetime_as_utc_whatever_the_machine_zone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        zone("Asia/Tokyo")
        monkeypatch.setenv("TZ", "Asia/Tokyo")
        time.tzset()
        try:
            assert fmt_iso(datetime(2026, 9, 30, 10, 0, 0)) == "2026-09-30T10:00:00Z"
        finally:
            monkeypatch.undo()
            time.tzset()


class TestNow:
    def test_now_is_real_time_by_default(self) -> None:
        before = datetime.now(UTC)
        assert before <= now_utc() <= datetime.now(UTC)

    def test_agentfare_now_freezes_the_clock(self, frozen_now) -> None:
        expected = frozen_now("2026-09-30T12:00:00-03:00")
        assert now_utc() == expected == datetime(2026, 9, 30, 15, 0, tzinfo=UTC)

    def test_empty_agentfare_now_is_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTFARE_NOW", "")
        assert abs((now_utc() - datetime.now(UTC)).total_seconds()) < 5

    def test_garbage_agentfare_now_is_a_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTFARE_NOW", "yesterday")
        with pytest.raises(ConfigError, match="AGENTFARE_NOW"):
            now_utc()


class TestDayAndWeek:
    def test_day_of_uses_the_local_calendar(self) -> None:
        toronto = zone("America/Toronto")
        tokyo = zone("Asia/Tokyo")
        ts = "2026-09-30T02:30:00Z"
        assert day_of(ts, UTC) == "2026-09-30"
        assert day_of(ts, toronto) == "2026-09-29"  # 22:30 EDT the evening before
        assert day_of(ts, tokyo) == "2026-09-30"  # 11:30 JST
        assert day_of("2026-09-30T20:00:00Z", tokyo) == "2026-10-01"

    def test_day_of_around_a_dst_change(self) -> None:
        toronto = zone("America/Toronto")  # clocks go back on 2026-11-01 at 02:00 local
        assert day_of("2026-11-01T03:59:59Z", toronto) == "2026-10-31"  # 23:59:59 EDT
        assert day_of("2026-11-01T04:00:00Z", toronto) == "2026-11-01"  # 00:00 EDT
        assert day_of("2026-11-02T04:59:59Z", toronto) == "2026-11-01"  # 23:59:59 EST
        assert day_of("2026-11-02T05:00:00Z", toronto) == "2026-11-02"  # 00:00 EST

    def test_week_label_is_zero_padded(self) -> None:
        assert iso_week_of("2026-01-08T12:00:00Z", UTC) == "2026-W02"

    def test_a_timezone_can_move_a_session_to_the_previous_week(self) -> None:
        toronto = zone("America/Toronto")
        ts = "2026-09-28T02:00:00Z"  # Monday in UTC, still Sunday evening in Toronto
        assert iso_week_of(ts, UTC) == "2026-W40"
        assert iso_week_of(ts, toronto) == "2026-W39"

    def test_2026_has_an_iso_week_53(self) -> None:
        # 2026-01-01 is a Thursday, so the ISO year 2026 runs 2025-12-29 .. 2027-01-03.
        assert iso_week_of("2026-12-31T12:00:00Z", UTC) == "2026-W53"
        assert iso_week_of("2027-01-01T12:00:00Z", UTC) == "2026-W53"  # calendar 2027, ISO 2026
        assert iso_week_of("2027-01-03T23:59:59Z", UTC) == "2026-W53"
        assert iso_week_of("2027-01-04T00:00:00Z", UTC) == "2027-W01"
        assert iso_week_of("2025-12-28T23:59:59Z", UTC) == "2025-W52"
        assert iso_week_of("2025-12-29T00:00:00Z", UTC) == "2026-W01"  # calendar 2025, ISO 2026

    def test_iso_year_boundary_across_a_timezone(self) -> None:
        toronto = zone("America/Toronto")
        tokyo = zone("Asia/Tokyo")
        ts = "2027-01-04T03:00:00Z"
        assert iso_week_of(ts, UTC) == "2027-W01"
        assert iso_week_of(ts, toronto) == "2026-W53"  # Sunday 22:00 EST
        assert iso_week_of("2027-01-03T15:00:00Z", tokyo) == "2027-W01"  # already Monday 00:00 JST
        assert iso_week_of("2025-12-29T02:00:00Z", toronto) == "2025-W52"
        assert day_of("2025-12-29T02:00:00Z", toronto) == "2025-12-28"


class TestWeekBounds:
    def test_label_gives_monday_and_sunday(self) -> None:
        assert week_bounds("2026-W39", UTC) == ("2026-09-21", "2026-09-27")
        assert week_bounds("2026-W01", UTC) == ("2025-12-29", "2026-01-04")

    def test_week_53_and_the_next_week_1(self) -> None:
        assert week_bounds("2026-W53", UTC) == ("2026-12-28", "2027-01-03")
        assert week_bounds("2027-W01", UTC) == ("2027-01-04", "2027-01-10")

    def test_label_ignores_the_timezone(self) -> None:
        assert week_bounds("2026-W39", zone("Asia/Tokyo")) == week_bounds("2026-W39", UTC)

    @pytest.mark.parametrize("label", ["2026-39", "abc", "2026-W3", "26-W03", "2027-W53", "2026-W00", "2026-W54"])
    def test_invalid_labels_are_a_clear_error(self, label: str) -> None:
        with pytest.raises(ConfigError, match="ISO week"):
            week_bounds(label, UTC)

    def test_lowercase_w_is_accepted(self) -> None:
        assert week_bounds("2026-w39", UTC) == ("2026-09-21", "2026-09-27")

    def test_now_is_the_week_containing_the_instant(self) -> None:
        assert week_bounds(None, UTC, datetime(2026, 9, 30, 12, tzinfo=UTC)) == ("2026-09-28", "2026-10-04")
        assert week_bounds(None, UTC, datetime(2026, 9, 28, 0, 0, tzinfo=UTC)) == ("2026-09-28", "2026-10-04")  # Monday 00:00
        assert week_bounds(None, UTC, datetime(2026, 10, 4, 23, 59, 59, tzinfo=UTC)) == ("2026-09-28", "2026-10-04")
        assert week_bounds(None, UTC, datetime(2026, 10, 5, 0, 0, tzinfo=UTC)) == ("2026-10-05", "2026-10-11")

    def test_now_respects_the_timezone(self) -> None:
        toronto = zone("America/Toronto")
        tokyo = zone("Asia/Tokyo")
        monday_utc = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)  # Sunday evening in Toronto, Monday morning in Tokyo
        assert week_bounds(None, UTC, monday_utc) == ("2026-09-28", "2026-10-04")
        assert week_bounds(None, toronto, monday_utc) == ("2026-09-21", "2026-09-27")
        assert week_bounds(None, tokyo, datetime(2026, 9, 27, 20, 0, tzinfo=UTC)) == ("2026-09-28", "2026-10-04")

    def test_now_across_the_iso_year_boundary(self) -> None:
        assert week_bounds(None, UTC, datetime(2027, 1, 1, 8, tzinfo=UTC)) == ("2026-12-28", "2027-01-03")
        assert week_bounds(None, UTC, datetime(2025, 12, 30, 8, tzinfo=UTC)) == ("2025-12-29", "2026-01-04")

    def test_default_now_honours_agentfare_now(self, frozen_now) -> None:
        frozen_now("2026-09-30T12:00:00Z")
        assert week_bounds(None, UTC) == ("2026-09-28", "2026-10-04")


class TestTimestampsAtTheEdgeOfTime:
    """Year 1 and year 9999 turn up in logs (a zero-value date) and in typed input; nothing may escape as OverflowError."""

    @pytest.mark.parametrize("text", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"])
    def test_a_timestamp_that_cannot_be_shifted_to_utc_is_a_value_error(self, text: str) -> None:
        with pytest.raises(ValueError):  # OverflowError is not one, and parsers only catch ValueError
            parse_iso(text)

    def test_the_edges_themselves_are_valid_in_utc(self) -> None:
        assert parse_iso("0001-01-01T00:00:00Z") == datetime(1, 1, 1, tzinfo=UTC)
        assert parse_iso("9999-12-31T23:59:59Z") == datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)

    def test_a_garbage_agentfare_now_at_the_edge_is_a_config_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENTFARE_NOW", "9999-12-31T23:59:59-05:00")
        with pytest.raises(ConfigError, match="AGENTFARE_NOW"):
            now_utc()

    def test_days_and_weeks_are_zero_padded_years(self) -> None:
        assert fmt_iso(datetime(1, 1, 1, tzinfo=UTC)) == "0001-01-01T00:00:00Z"
        assert day_of("0001-01-01T00:00:00Z", UTC) == "0001-01-01"
        assert iso_week_of("0001-01-01T00:00:00Z", UTC) == "0001-W01"
        assert week_bounds("0001-W01", UTC) == ("0001-01-01", "0001-01-07")

    def test_an_instant_that_cannot_be_shifted_stays_on_the_utc_calendar(self) -> None:
        toronto, tokyo = zone("America/Toronto"), zone("Asia/Tokyo")
        assert day_of("0001-01-01T00:00:00Z", toronto) == "0001-01-01"  # local time would be before year 1
        assert iso_week_of("0001-01-01T00:00:00Z", toronto) == "0001-W01"
        assert day_of("9999-12-31T23:59:59Z", tokyo) == "9999-12-31"  # local time would be after year 9999
        assert iso_week_of("9999-12-31T23:59:59Z", tokyo) == "9999-W52"

    def test_a_week_that_ends_after_year_9999_is_a_config_error(self) -> None:
        with pytest.raises(ConfigError, match="ISO week"):
            week_bounds("9999-W52", UTC)
