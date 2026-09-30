"""The built-in price table: seeded prices, provenance, honesty about unverified values, round trip."""

from __future__ import annotations

import re
from decimal import Decimal
from urllib.parse import urlparse

import pytest
import yaml

from agentfare.config import DEFAULT_CONFIG
from agentfare.cost import usage_cost_ucents
from agentfare.models import Usage
from agentfare.pricefetch import check_page, extract_text
from agentfare.pricing import UNVERIFIED, PRICE_FIELDS, builtin_path, dump_pricing, load_pricing, parse_pricing

OFFICIAL_HOSTS = {
    "developers.openai.com",
    "learn.chatgpt.com",  # permanent (308) redirect target of developers.openai.com/codex/...
    "openai.com",
    "platform.claude.com",
    "docs.claude.com",
    "claude.com",
    "ai.google.dev",
    "docs.x.ai",
}
PROVIDERS = {"openai", "anthropic", "google", "xai"}

# Prices the product owner seeded (USD per 1M tokens, checked 2026-09-30): input, cached input, output.
SEEDED = {
    "gpt-6.1-sol": ("2.0", "0.10", "10.0"),
    "gpt-6-astra": ("10.0", "1.0", "50.0"),
    "gpt-6-sol": ("2.0", "0.20", "10.0"),
}

# Every model id the real Claude Code and Codex logs on the dev machine contained (dated Claude ids are
# covered by the alias test below), the Gemini CLI ids that are still served, and grok-4.7.
LOGGED_MODELS = [
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-fable-5-1",
    "claude-opus-4-8",
    "claude-haiku-4-5",
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "codex-auto-review",
    "grok-4.7",
    "gemini-3.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
]


@pytest.fixture(scope="module")
def raw() -> dict:
    return yaml.safe_load(builtin_path().read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def table():
    return load_pricing(path=builtin_path())


def test_builtin_table_loads(table):
    assert table.path == builtin_path()
    assert len(table.models) >= len(LOGGED_MODELS)


@pytest.mark.parametrize("model", sorted(SEEDED))
def test_seeded_prices_are_exact(table, model):
    inp, cached, out = SEEDED[model]
    p = table.models[model]
    assert (p.input, p.cached_input, p.output) == (Decimal(inp), Decimal(cached), Decimal(out))
    assert p.unverified_fields == ()
    assert p.provider == "openai"


# OpenAI models added after the seed, read on 2026-09-30 from developers.openai.com: input, cached input,
# output and cache writes (the 1.25x rule the model pages state).
OPENAI_5_6 = {
    "gpt-5.6-sol": ("4.0", "0.40", "20.0", "5.0"),  # promotional, at least through 2026-11-21
    "gpt-5.6-terra": ("2.0", "0.20", "12.0", "2.5"),
    "gpt-5.6-luna": ("0.20", "0.02", "1.20", "0.25"),
}


@pytest.mark.parametrize("model", sorted(OPENAI_5_6))
def test_gpt_5_6_family_is_priced_as_published(table, model):
    p = table.models[model]
    assert (p.input, p.cached_input, p.output, p.cache_write) == tuple(Decimal(v) for v in OPENAI_5_6[model])
    assert p.cache_write == p.input * Decimal("1.25")
    assert p.provider == "openai" and p.unverified_fields == () and p.cache_write_1h is None
    assert urlparse(p.source).hostname == "developers.openai.com" and p.checked == "2026-09-30"
    assert table.priced(model) is p


# The Standard table of the OpenAI pricing page, as its Markdown view lists it (read 2026-09-30). The HTML view draws
# only the first three rows and collapses the rest behind JavaScript, so `prices --refresh` can read the others only
# in this view (or on a model page, which shows no cache-write cell for the GPT-5.6 models).
OPENAI_PRICING_MD = "https://developers.openai.com/api/docs/pricing.md"
OPENAI_STANDARD_TABLE = """\
Prices per 1M tokens.

| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-6-astra | $10.00 | $1.00 | $12.50 | $50.00 | $20.00 | $2.00 | $25.00 | $75.00 |
| gpt-6.1-sol | $2.00 | $0.10 | $2.50 | $10.00 | $4.00 | $0.20 | $5.00 | $15.00 |
| gpt-6-luna | $0.10 | $0.01 | $0.125 | $0.50 | $0.20 | $0.02 | $0.25 | $0.75 |
| gpt-6-sol | $2.00 | $0.20 | $2.50 | $10.00 | $4.00 | $0.40 | $5.00 | $15.00 |
| gpt-5.6-sol | $4.00 | $0.40 | $5.00 | $20.00 | $8.00 | $0.80 | $10.00 | $30.00 |
| gpt-5.6-terra | $2.00 | $0.20 | $2.50 | $12.00 | $4.00 | $0.40 | $5.00 | $18.00 |
| gpt-5.6-luna | $0.20 | $0.02 | $0.25 | $1.20 | $0.40 | $0.04 | $0.50 | $1.80 |
"""
STANDARD_TABLE_MODELS = [row.split("|")[1].strip() for row in OPENAI_STANDARD_TABLE.splitlines() if row.startswith("| gpt-")]


def test_the_pricing_pages_standard_table_confirms_every_openai_row_it_lists(table):
    page = extract_text(OPENAI_STANDARD_TABLE, "text/markdown")
    checks = check_page(page, table, STANDARD_TABLE_MODELS)
    assert {model: check.status for model, check in checks.items()} == dict.fromkeys(STANDARD_TABLE_MODELS, "confirmed")


@pytest.mark.parametrize("model", sorted(OPENAI_5_6))
def test_gpt_5_6_rows_cite_the_page_view_that_shows_their_cache_writes(table, model):
    assert table.models[model].source == OPENAI_PRICING_MD


@pytest.mark.parametrize("model", STANDARD_TABLE_MODELS)
def test_notes_do_not_deny_a_row_the_pricing_page_lists(table, model):
    note = table.models[model].note
    assert "does not list" not in note and "lists only" not in note


def test_openai_long_context_rule_names_the_cache_rates(table):
    """The pricing page's long-context cells double the cached-input and cache-write rates too, not only input."""
    rows = {name: p for name, p in table.models.items() if p.provider == "openai" and "272K" in p.note}
    assert set(rows) == set(STANDARD_TABLE_MODELS)
    for name, p in rows.items():
        assert "2x input and cache rates and 1.5x output" in p.note, name


def test_gpt_5_6_ids_are_not_the_gpt_6_models(table):
    for family in ("sol", "luna"):
        assert table.resolve(f"gpt-5.6-{family}") == f"gpt-5.6-{family}"
        assert table.resolve(f"gpt-6-{family}") == f"gpt-6-{family}"
        assert table.models[f"gpt-5.6-{family}"].input != table.models[f"gpt-6-{family}"].input


def test_every_model_has_provenance(table):
    for name, p in table.models.items():
        assert p.provider in PROVIDERS, name
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.checked), f"{name}: checked must be an ISO date"
        parsed = urlparse(p.source)
        assert parsed.scheme == "https" and parsed.hostname in OFFICIAL_HOSTS, f"{name}: source {p.source!r}"


def test_prices_are_numbers_or_the_unverified_literal(raw, table):
    for name, entry in raw["models"].items():
        parsed = table.models[name.lower()]
        for field in PRICE_FIELDS:
            if field not in entry:
                continue
            value = entry[field]
            if isinstance(value, str):
                assert value == UNVERIFIED, f"{name}.{field}: a number must not be a string ({value!r})"
                assert field in parsed.unverified_fields and getattr(parsed, field) is None
            else:
                assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{name}.{field}"
                assert value >= 0, f"{name}.{field}"
                assert field not in parsed.unverified_fields and getattr(parsed, field) is not None


def test_every_model_declares_input_and_output(raw, table):
    for name, entry in raw["models"].items():
        for field in ("input", "output"):
            assert field in entry, f"{name} has no {field}: use a number or 'unverified'"
        priced = all(isinstance(entry[f], (int, float)) for f in ("input", "output"))
        assert table.models[name.lower()].priced is priced


def test_unverified_models_explain_why(table):
    for name in table.unverified_models():
        assert table.models[name].note, f"{name} has unverified fields but no note"


def test_unpriced_rows_hold_no_number_at_all(raw, table):
    unpriced = {name for name, p in table.models.items() if not p.priced}
    assert unpriced >= {"codex-auto-review", "gemini-3-flash", "gemini-3-pro-preview"}
    for name in unpriced:
        for field in PRICE_FIELDS:
            assert raw["models"][name].get(field, UNVERIFIED) == UNVERIFIED, f"{name}.{field} has a number on an unpriced row"
            assert getattr(table.models[name], field) is None, name


def test_codex_auto_review_stays_unverified_until_openai_publishes_a_price(table):
    p = table.models["codex-auto-review"]
    assert p.provider == "openai" and not p.priced
    assert set(p.unverified_fields) == {"input", "cached_input", "output"}
    assert (p.cache_write, p.cache_write_1h) == (None, None)
    assert "pricing.yaml" in p.note  # tells the reader how to price it themselves
    assert table.status("codex-auto-review") == UNVERIFIED
    assert "unverified" in table.warning_for("codex-auto-review")


def test_price_columns_are_sane(table):
    for name, p in table.models.items():
        if not p.priced:
            continue
        assert p.input > 0 and p.output >= p.input, f"{name}: output cheaper than input, columns swapped?"
        if p.cached_input is not None:
            assert p.cached_input <= p.input, name
        if p.cache_write is not None:
            assert p.cache_write >= p.input, name


# Anthropic prices cache writes at 1.25x input (5 minutes) or 2x (1 hour) and cache reads at 0.1x input,
# except these two models (platform.claude.com pricing page, "Prompt caching").
ANTHROPIC_CACHE_READ_MULTIPLIER = {"claude-fable-5-1": Decimal("0.025"), "claude-opus-5-5": Decimal("0.05")}


def test_anthropic_cache_columns_follow_the_published_multipliers(table):
    anthropic = {name: p for name, p in table.models.items() if p.provider == "anthropic"}
    assert anthropic
    for name, p in anthropic.items():
        assert p.cache_write == p.input * Decimal("1.25"), f"{name}: cache_write is not the 5-minute rate"
        assert p.cache_write_1h == p.input * 2, f"{name}: cache_write_1h is not the 1-hour rate (2x input)"
        assert p.cached_input == p.input * ANTHROPIC_CACHE_READ_MULTIPLIER.get(name, Decimal("0.1")), name
        assert p.cached_input < p.cache_write < p.cache_write_1h, name


def test_only_anthropic_has_a_1_hour_cache_write_rate(raw, table):
    for name, p in table.models.items():
        assert (p.cache_write_1h is not None) is (p.provider == "anthropic"), name
        assert ("cache_write_1h" in raw["models"][name]) is (p.provider == "anthropic"), name


def test_google_and_xai_publish_no_cache_write_rate(table):
    for name, p in table.models.items():
        if p.provider in ("google", "xai"):
            assert p.cache_write is None, f"{name}: {p.provider} has no cache-write price, so the input price applies"


def test_one_hour_writes_are_billed_at_their_own_rate_from_the_builtin_table(table):
    opus = table.models["claude-opus-5-5"]
    one_hour = usage_cost_ucents(Usage(cache_write_1h=1_000_000), opus)
    five_minute = usage_cost_ucents(Usage(cache_write=1_000_000), opus)
    assert one_hour == 8 * 100 * 1_000_000  # $8.00 per 1M tokens, in micro-cents
    assert five_minute == 5 * 100 * 1_000_000


# Rows whose price changes or whose model goes away on a known date carry that date in their note, so a
# refresh does not lose it (the schema has no field for it).
END_DATED = {
    "claude-haiku-4-5": "2026-10-15",
    "gpt-5.6-sol": "2026-11-21",
    "gemini-3.6-flash": "2026-12-31",
    "gemini-3.7-flash": "2026-12-31",
    "gemini-3.8-flash": "2026-12-31",
    "gemini-3.1-flash-lite": "2027-05-07",
}


@pytest.mark.parametrize(("model", "day"), sorted(END_DATED.items()))
def test_end_dated_rows_say_so_in_their_note(table, model, day):
    assert day in table.models[model].note


def test_openai_cache_writes_are_1_25x_input(table):
    with_cache_write = {name: p for name, p in table.models.items() if p.provider == "openai" and p.cache_write}
    assert with_cache_write
    for name, p in with_cache_write.items():
        assert p.cache_write == p.input * Decimal("1.25"), name


@pytest.mark.parametrize("model", LOGGED_MODELS)
def test_models_from_real_logs_are_in_the_table(table, model):
    assert table.get(model) is not None, f"{model} would be reported as not in the price table"


def test_only_the_codex_reviewer_alias_is_unpriced(table):
    unpriced = {m for m in LOGGED_MODELS if table.status(m) != "priced"}
    assert unpriced == {"codex-auto-review"}
    assert table.unverified_models() == ["codex-auto-review", "gemini-3-flash", "gemini-3-pro-preview"]


def test_router_models_from_default_config_are_priced(table):
    cfg = DEFAULT_CONFIG["router"]
    wanted = {m for m in cfg["recommend"].values() if m}
    for tiers in cfg["run"]["models"].values():
        wanted |= {m for m in tiers.values() if m}
    assert wanted
    for model in wanted:
        assert table.priced(model) is not None, f"default router model {model} is not priced"


def test_yaml_round_trips_without_changing_a_value(table, raw):
    header = "round trip header"
    again = parse_pricing(yaml.safe_load(dump_pricing(table, header)))
    assert again.models == table.models
    assert again.aliases == table.aliases
    assert set(again.models) == {k.lower() for k in raw["models"]}


def test_aliases_resolve_to_existing_models(table):
    assert table.aliases
    for alias, target in table.aliases.items():
        assert target in table.models, f"alias {alias} points to missing model {target}"
        assert table.resolve(alias) == target


@pytest.mark.parametrize(
    ("seen_in_a_log", "canonical"),
    [
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-opus-5[1m]", "claude-opus-5"),
        ("anthropic/claude-sonnet-5-5", "claude-sonnet-5-5"),
        ("anthropic.claude-opus-5-5", "claude-opus-5-5"),
        ("claude-haiku-4-5@20251001", "claude-haiku-4-5"),
        ("gemini-3.1-pro-preview-customtools", "gemini-3.1-pro-preview"),
        ("models/gemini-2.5-pro", "gemini-2.5-pro"),
        ("grok-4.7-latest", "grok-4.7"),
    ],
)
def test_vendor_spellings_resolve(table, seen_in_a_log, canonical):
    assert table.resolve(seen_in_a_log) == canonical
