from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentfare.config import Config
from agentfare.pricing import (
    PRICE_FIELDS,
    UNVERIFIED,
    ModelPrice,
    PriceTable,
    PricingError,
    builtin_path,
    dump_pricing,
    load_pricing,
    parse_pricing,
)

SAMPLE = """\
schema: 1
models:
  gpt-6.1-sol:
    provider: openai
    input: 2.0
    cached_input: 0.10
    output: 10.0
    source: https://example.test/pricing
    checked: 2026-09-30
    note: hand written
  claude-opus-5-5:
    provider: anthropic
    input: 5
    cached_input: 0.5
    cache_write: 6.25
    cache_write_1h: 10
    output: 25
  gemini-3-pro:
    provider: google
    input: unverified
    output: 12.0
  gpt-6-astra:
    input: 10
    cached_input: unverified
    output: 50
aliases:
  opus: claude-opus-5-5
  dangling: not-a-model
"""


@pytest.fixture
def table() -> PriceTable:
    return parse_pricing(yaml.safe_load(SAMPLE))


def bad_entry(**fields: Any) -> dict[str, Any]:
    return {"models": {"m": {"input": 1, "output": 1, **fields}}}


class TestParse:
    def test_numbers_become_exact_decimals(self, table: PriceTable) -> None:
        p = table.models["gpt-6.1-sol"]
        assert (p.input, p.cached_input, p.output) == (Decimal("2.0"), Decimal("0.10"), Decimal("10.0"))
        assert all(isinstance(v, Decimal) for v in (p.input, p.cached_input, p.output))
        assert p.cache_write is None
        assert (p.provider, p.source, p.checked, p.note) == ("openai", "https://example.test/pricing", "2026-09-30", "hand written")
        assert p.priced and p.unverified_fields == ()

    def test_yaml_floats_do_not_bring_binary_noise(self) -> None:
        t = parse_pricing(yaml.safe_load("models:\n  m: {input: 0.1, output: 1.0e-7, cached_input: '0.30'}\n"))
        p = t.models["m"]
        assert (p.input, p.output, p.cached_input) == (Decimal("0.1"), Decimal("0.0000001"), Decimal("0.30"))

    def test_integers_and_zero_are_valid_prices(self) -> None:
        p = parse_pricing({"models": {"free": {"input": 0, "output": 0}}}).models["free"]
        assert p.input == 0 and p.output == 0 and p.priced

    def test_unverified_marker(self, table: PriceTable) -> None:
        p = table.models["gemini-3-pro"]
        assert p.input is None and p.unverified_fields == ("input",)
        assert not p.priced
        assert p.output == Decimal("12.0")

    @pytest.mark.parametrize("marker", ["unverified", "UNVERIFIED", "  Unverified "])
    def test_marker_is_case_and_space_insensitive(self, marker: str) -> None:
        p = parse_pricing({"models": {"m": {"input": marker, "output": marker}}}).models["m"]
        assert p.unverified_fields == ("input", "output") and not p.priced

    def test_model_names_are_normalised_to_lowercase(self) -> None:
        t = parse_pricing({"models": {" GPT-Test ": {"input": 1, "output": 1}}, "aliases": {"Alias": "GPT-TEST"}})
        assert list(t.models) == ["gpt-test"] and t.models["gpt-test"].model == "gpt-test"
        assert t.aliases == {"alias": "gpt-test"}
        assert t.get("ALIAS") is t.models["gpt-test"]

    def test_absent_fields_are_none_not_unverified(self, table: PriceTable) -> None:
        p = table.models["gemini-3-pro"]
        assert p.cached_input is None and p.cache_write is None and p.source == "" and p.checked == ""

    def test_null_text_fields_are_empty_not_the_word_none(self) -> None:
        p = parse_pricing({"models": {"m": {"input": 1, "output": 1, "provider": None, "source": None, "note": None}}}).models["m"]
        assert (p.provider, p.source, p.checked, p.note) == ("", "", "", "")

    def test_fallback_rates(self, table: PriceTable) -> None:
        sol = table.models["gpt-6.1-sol"]
        assert sol.cached_rate == Decimal("0.10") and sol.cache_write_rate == Decimal("2.0")  # no cache_write: input rate
        opus = table.models["claude-opus-5-5"]
        assert opus.cache_write_rate == Decimal("6.25")
        astra = table.models["gpt-6-astra"]
        assert astra.cached_rate == Decimal("10")  # unverified cached price: input rate

    def test_one_hour_cache_write_price_is_an_optional_exact_decimal(self, table: PriceTable) -> None:
        assert table.models["claude-opus-5-5"].cache_write_1h == Decimal("10")
        assert table.models["gpt-6.1-sol"].cache_write_1h is None
        assert "cache_write_1h" in PRICE_FIELDS

    def test_one_hour_rate_falls_back_to_the_cache_write_rate_then_to_the_input_rate(self, table: PriceTable) -> None:
        assert table.models["claude-opus-5-5"].cache_write_1h_rate == Decimal("10")  # its own price
        only_5m = parse_pricing({"models": {"m": {"input": 3, "output": 15, "cache_write": 3.75}}}).models["m"]
        assert only_5m.cache_write_1h is None and only_5m.cache_write_1h_rate == Decimal("3.75")
        neither = table.models["gpt-6.1-sol"]
        assert neither.cache_write_1h_rate == Decimal("2.0")  # the input rate
        astra = table.models["gpt-6-astra"]  # cached price unverified: irrelevant to writes
        assert astra.cache_write_1h_rate == Decimal("10")

    def test_an_unverified_5_minute_price_leaves_the_1h_rate_at_the_input_rate(self) -> None:
        p = parse_pricing({"models": {"m": {"input": 3, "output": 15, "cache_write": "unverified"}}}).models["m"]
        assert p.cache_write_1h_rate == Decimal("3")

    def test_table_remembers_the_path(self, tmp_path: Path) -> None:
        assert parse_pricing({"models": {}}, tmp_path / "p.yaml").path == tmp_path / "p.yaml"


class TestRejected:
    @pytest.mark.parametrize("value", [-1, -0.01, "-3"])
    def test_negative_price(self, value: Any) -> None:
        with pytest.raises(PricingError, match=r"m\.input: negative"):
            parse_pricing(bad_entry(input=value))

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_price(self, value: bool) -> None:
        with pytest.raises(PricingError, match=r"m\.cached_input"):
            parse_pricing(bad_entry(cached_input=value))

    @pytest.mark.parametrize(("value", "message"), [(-1, "negative"), (True, "number or 'unverified'"), ("dear", "number or 'unverified'")])
    def test_one_hour_cache_write_price_is_validated_like_the_others(self, value: Any, message: str) -> None:
        with pytest.raises(PricingError, match=rf"m\.cache_write_1h: .*{message}"):
            parse_pricing(bad_entry(cache_write_1h=value))

    @pytest.mark.parametrize("value", ["cheap", "", [1], {"a": 1}, "1,5"])
    def test_non_numeric_price(self, value: Any) -> None:
        with pytest.raises(PricingError, match=r"m\.output"):
            parse_pricing(bad_entry(output=value))

    @pytest.mark.parametrize("text", [".nan", ".inf", "-.inf"])
    def test_non_finite_price(self, text: str) -> None:
        doc = yaml.safe_load(f"models:\n  m: {{input: 1, output: {text}}}\n")
        with pytest.raises(PricingError, match=r"m\.output"):
            parse_pricing(doc)

    @pytest.mark.parametrize("doc", [[], "text", None, {}, {"models": []}, {"models": "x"}, {"schema": 1}])
    def test_missing_models_mapping(self, doc: Any) -> None:
        with pytest.raises(PricingError, match="models"):
            parse_pricing(doc)

    @pytest.mark.parametrize("entry", [5, None, ["input"], "gpt"])
    def test_entry_must_be_a_mapping(self, entry: Any) -> None:
        with pytest.raises(PricingError, match="expected a mapping"):
            parse_pricing({"models": {"m": entry}})

    def test_aliases_must_be_a_mapping(self) -> None:
        with pytest.raises(PricingError, match="aliases"):
            parse_pricing({"models": {}, "aliases": ["a", "b"]})

    def test_missing_aliases_is_fine(self) -> None:
        assert parse_pricing({"models": {}, "aliases": None}).aliases == {}


class TestResolve:
    @pytest.mark.parametrize(
        "logged",
        [
            "claude-opus-5-5",
            "  Claude-Opus-5-5 ",
            "claude-opus-5-5-20260101",
            "claude-opus-5-5-2026-01-01",
            "claude-opus-5-5[1m]",
            "claude-opus-5-5-20260101[1m]",
            "claude-opus-5-5-latest",
            "anthropic/claude-opus-5-5",
            "anthropic/claude-opus-5-5-20260101[1m]",
            "opus",  # alias
            "anthropic/opus",
            "OPUS",
        ],
    )
    def test_spellings_that_reach_a_model(self, table: PriceTable, logged: str) -> None:
        assert table.resolve(logged) == "claude-opus-5-5"
        assert table.get(logged) is table.models["claude-opus-5-5"]

    @pytest.mark.parametrize(
        ("logged", "canonical"),
        [
            ("openai/gpt-6.1-sol", "gpt-6.1-sol"),
            ("gpt-6.1-sol-2026-09-30", "gpt-6.1-sol"),
            ("models/gemini-3-pro", "gemini-3-pro"),
            ("google/gemini-3-pro-latest", "gemini-3-pro"),
        ],
    )
    def test_other_providers(self, table: PriceTable, logged: str, canonical: str) -> None:
        assert table.resolve(logged) == canonical

    @pytest.mark.parametrize(
        "logged",
        ["nope", "", "claude-opus-5", "claude-opus-5-5-preview", "gpt-6.1", "claude-opus-5-5-2026", "dangling", "unknown/claude-opus-5-5"],
    )
    def test_no_fuzzy_matching(self, table: PriceTable, logged: str) -> None:
        assert table.resolve(logged) is None
        assert table.get(logged) is None and table.priced(logged) is None
        assert table.status(logged) == "unknown"

    def test_a_dated_entry_beats_the_undated_one(self) -> None:
        t = parse_pricing({"models": {"m-a": {"input": 1, "output": 1}, "m-a-20260101": {"input": 9, "output": 9}}})
        assert t.resolve("m-a-20260101") == "m-a-20260101"
        assert t.resolve("m-a-20260202") == "m-a"

    def test_priced_returns_only_models_with_dollar_math(self, table: PriceTable) -> None:
        assert table.priced("gpt-6.1-sol") is table.models["gpt-6.1-sol"]
        assert table.priced("gemini-3-pro") is None  # unverified input
        assert table.priced("gpt-6-astra") is table.models["gpt-6-astra"]  # only the cached price is unverified

    def test_status(self, table: PriceTable) -> None:
        assert table.status("opus") == "priced"
        assert table.status("gemini-3-pro") == UNVERIFIED
        assert table.status("gpt-6-astra") == "priced"
        assert table.status("mystery-model") == "unknown"


class TestWarnings:
    def test_clean_models_have_no_warning(self, table: PriceTable) -> None:
        assert table.warning_for("gpt-6.1-sol") is None
        assert table.warning_for("anthropic/opus") is None

    def test_unknown_model(self, table: PriceTable) -> None:
        msg = table.warning_for("mystery-model") or ""
        assert msg.startswith("mystery-model:") and "not in the price table" in msg and "not counted" in msg

    def test_unverified_model_is_named_and_excluded(self, table: PriceTable) -> None:
        msg = table.warning_for("gemini-3-pro-20260101") or ""
        assert msg.startswith("gemini-3-pro-20260101:") and "unverified" in msg and "not counted" in msg

    def test_unverified_output_is_also_unpriced(self) -> None:
        t = parse_pricing({"models": {"m": {"input": 1, "output": "unverified"}}})
        assert t.priced("m") is None and "unverified" in (t.warning_for("m") or "")

    def test_partially_unverified_cached_price_warns_but_stays_priced(self, table: PriceTable) -> None:
        msg = table.warning_for("gpt-6-astra") or ""
        assert "cached-input price is unverified" in msg and "input rate" in msg
        assert table.priced("gpt-6-astra") is not None

    def test_unverified_cache_write_price_warns_too(self) -> None:
        t = parse_pricing({"models": {"m": {"input": 3, "output": 15, "cache_write": "unverified"}}})
        msg = t.warning_for("m") or ""
        assert "cache-write price is unverified" in msg and "input rate" in msg
        assert t.priced("m") is not None and t.models["m"].cache_write_rate == Decimal("3")

    def test_both_cache_prices_unverified_in_one_message(self) -> None:
        t = parse_pricing({"models": {"m": {"input": 3, "output": 15, "cache_write": "unverified", "cached_input": "unverified"}}})
        msg = t.warning_for("m") or ""
        assert "cached-input" in msg and "cache-write" in msg

    def test_unverified_one_hour_price_warns_and_bills_1h_writes_at_the_5_minute_rate(self) -> None:
        t = parse_pricing({"models": {"m": {"input": 3, "output": 15, "cache_write": 3.75, "cache_write_1h": "unverified"}}})
        assert t.warning_for("m") == "m: 1-hour cache-write price is unverified, 1-hour cache writes are billed at the 5-minute cache-write rate"
        assert t.priced("m") is not None and t.models["m"].unverified_fields == ("cache_write_1h",)
        assert t.models["m"].cache_write_1h_rate == Decimal("3.75") and t.unverified_models() == ["m"]

    def test_unverified_one_hour_price_without_a_5_minute_price_falls_to_the_input_rate(self) -> None:
        t = parse_pricing({"models": {"m": {"input": 3, "output": 15, "cache_write_1h": "unverified"}}})
        assert t.warning_for("m") == "m: 1-hour cache-write price is unverified, 1-hour cache writes are billed at the input rate"

    def test_both_cache_write_prices_unverified_are_named_in_one_message(self) -> None:
        t = parse_pricing({"models": {"m": {"input": 3, "output": 15, "cache_write": "unverified", "cache_write_1h": "unverified"}}})
        assert t.warning_for("m") == (
            "m: cache-write and 1-hour cache-write prices are unverified, "
            "cache writes are billed at the input rate; 1-hour cache writes are billed at the input rate"
        )

    def test_all_three_optional_prices_unverified(self) -> None:
        odd = {"input": 3, "output": 15, "cached_input": "unverified", "cache_write": "unverified", "cache_write_1h": "unverified"}
        msg = parse_pricing({"models": {"m": odd}}).warning_for("m") or ""
        assert msg.startswith("m: cached-input, cache-write and 1-hour cache-write prices are unverified, cache reads and writes are billed at the input rate")

    def test_the_warning_names_the_model_as_logged(self) -> None:
        t = parse_pricing({"models": {"m": {"input": 3, "output": 15, "cache_write_1h": "unverified"}}})
        assert (t.warning_for("M-20260101") or "").startswith("M-20260101: 1-hour cache-write price")

    def test_unverified_models_lists_every_model_needing_attention(self, table: PriceTable) -> None:
        assert table.unverified_models() == ["gemini-3-pro", "gpt-6-astra"]

    def test_fully_verified_table_has_none(self) -> None:
        assert parse_pricing({"models": {"m": {"input": 1, "output": 1}}}).unverified_models() == []


class TestDump:
    def roundtrip(self, table: PriceTable) -> PriceTable:
        return parse_pricing(yaml.safe_load(dump_pricing(table)))

    def test_round_trip_preserves_values_and_unverified_markers(self, table: PriceTable) -> None:
        again = self.roundtrip(table)
        assert again.models == table.models
        assert again.aliases == table.aliases
        assert again.models["gemini-3-pro"].unverified_fields == ("input",)
        assert again.models["gpt-6-astra"].unverified_fields == ("cached_input",)
        assert again.models["gpt-6.1-sol"].cached_input == Decimal("0.10")

    def test_one_hour_price_and_its_marker_round_trip_after_the_5_minute_price(self) -> None:
        t = parse_pricing({"models": {
            "a": {"input": 5, "output": 25, "cache_write": 6.25, "cache_write_1h": 10},
            "b": {"input": 5, "output": 25, "cache_write_1h": "unverified"},
        }})
        text = dump_pricing(t)
        assert "    cache_write: 6.25\n    cache_write_1h: 10.0\n" in text
        assert "    cache_write_1h: unverified\n" in text
        again = self.roundtrip(t)
        assert again.models == t.models
        assert again.models["a"].cache_write_1h == Decimal("10") and again.models["b"].unverified_fields == ("cache_write_1h",)

    def test_dump_is_stable(self, table: PriceTable) -> None:
        once = dump_pricing(table)
        assert dump_pricing(self.roundtrip(table)) == once

    def test_dump_writes_the_marker_word_and_never_exponents(self) -> None:
        t = parse_pricing(yaml.safe_load("models:\n  m: {input: unverified, output: 1.0e-7, cache_write: 12}\n"))
        text = dump_pricing(t)
        assert "input: unverified" in text
        assert "output: 0.0000001" in text and "e-" not in text.lower()
        assert "cache_write: 12.0" in text
        assert self.roundtrip(t).models == t.models

    def test_each_model_gets_a_source_and_date_comment(self, table: PriceTable) -> None:
        text = dump_pricing(table)
        assert "# gpt-6.1-sol | source: https://example.test/pricing | checked: 2026-09-30" in text
        assert "# claude-opus-5-5 | source: n/a | checked: n/a" in text

    def test_header_becomes_comments(self) -> None:
        text = dump_pricing(parse_pricing({"models": {}}), header="line one\n\nline three")
        assert text.startswith("# line one\n#\n# line three\n\nschema: 1\nmodels:\n")

    def test_models_and_aliases_are_sorted(self, table: PriceTable) -> None:
        text = dump_pricing(table)
        names = [ln.strip()[:-1] for ln in text.splitlines() if ln.startswith("  ") and ln.endswith(":") and not ln.startswith("    ")]
        assert names == sorted(names) == ["claude-opus-5-5", "gemini-3-pro", "gpt-6-astra", "gpt-6.1-sol"]
        assert text.index("  dangling:") < text.index("  opus:")

    def test_awkward_free_text_survives_a_round_trip(self) -> None:
        nasty = {
            "provider": "open: ai",
            "source": "https://example.test/a?b=c#frag: draft",
            "checked": "2026-09-30",
            "note": "line one\nline two: still #note",
        }
        models = {
            "plain": {"input": 1, "output": 2, **nasty},
            "null": {"input": 1, "output": 2, "note": "yes"},
            "12345": {"input": 1, "output": 2, "note": "  padded  "},
            "ünï-cödé": {"input": 1, "output": 2, "note": "ção: é #"},
            "multi\nline": {"input": 1, "output": 2, "source": "a\nb"},
        }
        table = parse_pricing({"models": models, "aliases": {"true": "plain", "a: b": "null"}})
        again = self.roundtrip(table)
        assert again.models == table.models
        assert again.aliases == table.aliases
        assert again.models["plain"].note == "line one\nline two: still #note"
        assert again.models["null"].note == "yes"

    @pytest.mark.parametrize("char", ["\x07", "\x1b", "\x7f", "\x00", "\ufffe"])
    def test_characters_yaml_cannot_read_never_reach_the_file_raw(self, char: str) -> None:
        # the regenerated comment above each model repeats the name and source: it may not break the file
        models = {f"model{char}one": {"input": 1, "output": 2, "source": f"https://example.test/{char}", "note": f"n{char}"}}
        table = parse_pricing({"models": models, "aliases": {f"alias{char}": f"model{char}one"}})
        text = dump_pricing(table, header=f"refreshed{char}today")
        assert char not in text
        again = parse_pricing(yaml.safe_load(text))
        assert again.models == table.models
        assert again.aliases == table.aliases

    @pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
    def test_a_line_separator_inside_a_name_still_reads_back_as_a_key(self, separator: str) -> None:
        # a quoted key must fit on one line, and YAML counts these characters as line breaks
        models = {f"model{separator}one": {"input": 1, "output": 2, "note": f"a{separator}b"}}
        table = parse_pricing({"models": models, "aliases": {f"alias{separator}one": f"model{separator}one"}})
        again = self.roundtrip(table)
        assert again.models == table.models
        assert again.aliases == table.aliases

    @pytest.mark.parametrize("text", ["2026-02-30", "2026-13-01", "2026-09-31 10:00:00", "!!float abc", "!!timestamp abc", "!!bool maybe"])
    def test_text_that_looks_like_an_impossible_yaml_value_is_quoted_not_a_crash(self, text: str) -> None:
        # unquoted, these make PyYAML raise while reading (ValueError, KeyError...); a mistyped "checked" date is the likely way in
        table = parse_pricing({"models": {text: {"input": 1, "output": 2, "checked": text, "note": text}}, "aliases": {text: text}})
        again = self.roundtrip(table)
        assert again.models == table.models
        assert again.aliases == table.aliases

    def test_a_date_typed_checked_field_stays_a_plain_date(self) -> None:
        t = parse_pricing({"models": {"m": {"input": 1, "output": 1, "checked": date(2026, 9, 30)}}})
        assert t.models["m"].checked == "2026-09-30"
        assert "    checked: 2026-09-30\n" in dump_pricing(t)


class TestLoad:
    def test_explicit_path(self, tmp_path: Path) -> None:
        path = tmp_path / "mine.yaml"
        path.write_text(SAMPLE, encoding="utf-8")
        t = load_pricing(path=path)
        assert t.path == path and "opus" in t.aliases

    def test_builtin_table_is_the_default_and_round_trips(self) -> None:
        t = load_pricing()
        assert t.path == builtin_path() and builtin_path().is_file()
        assert t.models
        again = parse_pricing(yaml.safe_load(dump_pricing(t)))
        assert again.models == t.models and again.aliases == t.aliases

    def test_prices_keep_the_digits_they_were_written_with(self, tmp_path: Path) -> None:
        path = tmp_path / "p.yaml"
        path.write_text(
            "models:\n  m: {input: 0.1234567890123456789, cached_input: 0.10, output: 10.0, cache_write: 1_2.50}\n",
            encoding="utf-8",
        )
        p = load_pricing(path=path).models["m"]
        assert str(p.input) == "0.1234567890123456789"  # more digits than a binary float can hold
        assert str(p.cached_input) == "0.10" and str(p.output) == "10.0" and str(p.cache_write) == "12.50"
        text = dump_pricing(load_pricing(path=path))
        assert "    cached_input: 0.10\n" in text and "    input: 0.1234567890123456789\n" in text

    @pytest.mark.parametrize("value", [".inf", "-.inf", ".nan"])
    def test_non_finite_numbers_in_a_file_are_rejected(self, tmp_path: Path, value: str) -> None:
        path = tmp_path / "p.yaml"
        path.write_text(f"models:\n  m: {{input: 1, output: {value}}}\n", encoding="utf-8")
        with pytest.raises(PricingError, match=r"m\.output"):
            load_pricing(path=path)

    def test_user_override_wins_over_the_builtin_table(self, tmp_path: Path) -> None:
        (tmp_path / "pricing.yaml").write_text("models:\n  only-mine: {input: 1, output: 2}\n", encoding="utf-8")
        cfg = Config.load(home=tmp_path)
        t = load_pricing(cfg)
        assert list(t.models) == ["only-mine"] and t.path == tmp_path / "pricing.yaml"

    def test_configured_pricing_file_is_the_override(self, tmp_path: Path) -> None:
        (tmp_path / "custom.yaml").write_text("models:\n  custom: {input: 1, output: 2}\n", encoding="utf-8")
        (tmp_path / "config.yaml").write_text("pricing_file: custom.yaml\n", encoding="utf-8")
        cfg = Config.load(home=tmp_path)
        assert list(load_pricing(cfg).models) == ["custom"]

    def test_a_configured_pricing_file_that_is_missing_is_an_error(self, tmp_path: Path) -> None:
        (tmp_path / "pricing.yaml").write_text("models:\n  home-file: {input: 1, output: 2}\n", encoding="utf-8")
        (tmp_path / "config.yaml").write_text("pricing_file: typo.yaml\n", encoding="utf-8")
        with pytest.raises(PricingError, match=r"^pricing_file .*typo\.yaml does not exist$"):
            load_pricing(Config.load(home=tmp_path))  # the file in the home directory does not stand in for it

    def test_explicit_path_beats_the_override(self, tmp_path: Path) -> None:
        (tmp_path / "pricing.yaml").write_text("models:\n  from-home: {input: 1, output: 2}\n", encoding="utf-8")
        explicit = tmp_path / "explicit.yaml"
        explicit.write_text("models:\n  explicit: {input: 1, output: 2}\n", encoding="utf-8")
        assert list(load_pricing(Config.load(home=tmp_path), explicit).models) == ["explicit"]

    def test_no_override_file_falls_back_to_the_builtin_table(self, tmp_path: Path) -> None:
        assert load_pricing(Config.load(home=tmp_path)).path == builtin_path()

    @pytest.mark.parametrize("body", ["models: [unclosed\n", "- just\n- a list\n", "", "schema: 1\n", "models:\n  m: 5\n"])
    def test_malformed_file_raises_pricing_error(self, tmp_path: Path, body: str) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(PricingError):
            load_pricing(path=path)

    @pytest.mark.parametrize("value", ["2026-02-30", "2026-13-01", "!!float abc", "!!timestamp abc", "!!bool maybe"])
    def test_well_formed_yaml_with_an_impossible_value_raises_pricing_error(self, tmp_path: Path, value: str) -> None:
        # PyYAML raises ValueError, KeyError and more instead of YAMLError for these; a mistyped date must not end in a traceback
        path = tmp_path / "typo.yaml"
        path.write_text(f"models:\n  m: {{input: 1, output: 2, checked: {value}}}\n", encoding="utf-8")
        with pytest.raises(PricingError, match="cannot read"):
            load_pricing(path=path)

    def test_missing_file_raises_pricing_error(self, tmp_path: Path) -> None:
        with pytest.raises(PricingError, match="cannot read"):
            load_pricing(path=tmp_path / "missing.yaml")

    def test_undecodable_file_raises_pricing_error(self, tmp_path: Path) -> None:
        path = tmp_path / "binary.yaml"
        path.write_bytes(b"\xff\xfe\x00\x81")
        with pytest.raises(PricingError, match="cannot read"):
            load_pricing(path=path)


def test_model_price_is_hashable_and_frozen() -> None:
    p = ModelPrice("m", input=Decimal(1), output=Decimal(2))
    assert p in {p}
    with pytest.raises(AttributeError):
        p.input = Decimal(3)  # type: ignore[misc]


class TestCollisions:
    """Two entries that normalise to the same id must not silently replace each other's price."""

    def test_model_ids_that_differ_only_in_case_or_padding_are_refused(self) -> None:
        with pytest.raises(PricingError, match="duplicate.*gpt-x"):
            parse_pricing({"models": {"GPT-X": {"input": 1, "output": 1}, " gpt-x": {"input": 9, "output": 9}}})

    def test_alias_names_that_differ_only_in_case_are_refused(self) -> None:
        models = {"a": {"input": 1, "output": 1}, "b": {"input": 2, "output": 2}}
        with pytest.raises(PricingError, match="duplicate.*x"):
            parse_pricing({"models": models, "aliases": {"X": "a", "x": "b"}})

    def test_an_alias_that_names_a_model_is_dead_and_the_model_wins(self) -> None:
        t = parse_pricing({"models": {"a": {"input": 1, "output": 1}, "b": {"input": 2, "output": 2}}, "aliases": {"a": "b"}})
        assert t.resolve("a") == "a" and t.resolve("a-20260101") == "a"

    def test_an_alias_beats_the_date_stripped_model(self) -> None:
        t = parse_pricing({"models": {"m": {"input": 1, "output": 1}, "n": {"input": 2, "output": 2}}, "aliases": {"m-20260101": "n"}})
        assert t.resolve("m-20260101") == "n" and t.resolve("m-20260202") == "m"
