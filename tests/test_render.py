"""The shared rendering helpers: percentages, the character sanitiser and the price-warning block."""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from datetime import timezone
from fractions import Fraction
from math import floor
from typing import Any

import pytest
from rich.console import Console

from agentfare import report as report_module
from agentfare.aggregate import priced_rows
from agentfare.config import Config
from agentfare.db import Store
from agentfare.models import SessionRecord, Usage
from agentfare.pricing import PriceTable
from agentfare.config import ConfigError
from agentfare.render import (
    Col, Whole, _cut, clean_text, percent, plain, print_table, print_warnings, price_warnings, resolve_filters, share_bp, spend_text,
)

UTC = timezone.utc
Make = Callable[..., SessionRecord]

PRICES = {
    "big": {"input": 10, "cached_input": 1, "output": 50},
    "small": {"input": 2, "cached_input": 0.10, "output": 10},
    "partial": {"input": 2, "cached_input": "unverified", "output": 8},
    "unver": {"input": "unverified", "output": 5},
}


@pytest.fixture
def table(make_table: Callable[..., PriceTable]) -> PriceTable:
    return make_table(PRICES)


@pytest.fixture
def store() -> Iterator[Store]:
    with Store(":memory:") as s:
        yield s


class TestPercent:
    """One decimal, half up, rounded once from the amounts: an already rounded share must not be rounded again."""

    @pytest.mark.parametrize(
        ("part", "whole", "shown"),
        [
            (49_499, 1_000_000, "4.9%"),  # 4.9499%: through basis points (495) it would read 5.0%
            (99_949, 100_000, "99.9%"),  # 99.949%: through basis points (9995) it would read 100%
            (49_500, 1_000_000, "5.0%"),  # exactly 4.95%... half of the last place goes up
            (4_949, 100_000, "4.9%"),
            (0, 5, "0.0%"),
            (1, 1_000_000, "<0.1%"),  # something, but less than the 0.05% that rounds to 0.1%
            (49, 100_000, "<0.1%"),
            (50, 100_000, "0.1%"),
            (7, 7, "100%"),
            (99_951, 100_000, "100%"),
            (381, 1_000, "38.1%"),
        ],
    )
    def test_the_amounts_are_rounded_once(self, part: int, whole: int, shown: str) -> None:
        assert percent(part, whole) == shown

    def test_every_share_of_small_amounts_is_the_exact_half_up_rounding(self) -> None:
        """Against exact fractions, ties included: a whole of 2,000 makes every odd part a tie between two tenths."""
        for whole in (*range(1, 121), 2_000, 4_000):
            for part in range(whole + 1):
                tenths = floor(Fraction(part * 1_000, whole) + Fraction(1, 2))
                want = "<0.1%" if tenths == 0 and part else "100%" if tenths >= 1_000 else f"{tenths // 10}.{tenths % 10}%"
                assert percent(part, whole) == want, (part, whole)

    def test_nothing_to_divide_by_reads_as_zero(self) -> None:
        assert percent(5, 0) == percent(0, 0) == "0.0%"

    def test_a_missing_share_is_a_dash(self) -> None:
        assert percent(None) == "-"

    @pytest.mark.parametrize(
        ("bp", "shown"),
        [(0, "0.0%"), (1, "<0.1%"), (4, "<0.1%"), (5, "0.1%"), (7002, "70.0%"), (9995, "100%"), (10_000, "100%")],
    )
    def test_a_share_in_basis_points_still_reads_as_before(self, bp: int, shown: str) -> None:
        assert percent(bp) == shown

    def test_share_bp_callers_keep_working(self) -> None:
        assert percent(share_bp(38_100, 100_000)) == percent(38_100, 100_000) == "38.1%"


class TestCleanText:
    def test_control_line_and_bidirectional_characters_become_question_marks(self) -> None:
        assert clean_text("a\x1b[31mb\nc\td‮e\x00f\x7fg h") == "a?[31mb?c?d?e?f?g?h"

    def test_ordinary_text_is_untouched(self) -> None:
        assert clean_text("plain ação — 日本語 [x]") == "plain ação — 日本語 [x]"

    def test_plain_uses_it_and_then_drops_what_the_console_cannot_write(self) -> None:
        ascii_console = Console(file=io.TextIOWrapper(io.BytesIO(), encoding="ascii"), force_terminal=False)
        assert plain("ok\x1b ação", ascii_console) == "ok? a??o"

    def test_the_weekly_report_has_no_private_copy_of_the_pattern(self) -> None:
        assert not hasattr(report_module, "_UNSAFE") and not hasattr(report_module, "_clean")


class TestPrintWarnings:
    def render(self, warnings: list[dict[str, Any]]) -> list[str]:
        buffer = io.StringIO()
        print_warnings(Console(file=buffer, width=100, force_terminal=False), warnings)
        return [line for line in buffer.getvalue().splitlines() if line]

    @staticmethod
    def entry(model: str, status: str, excluded: int = 0) -> dict[str, Any]:
        return {"model": model, "status": status, "excluded_tokens": excluded, "message": f"{model}: {status} message"}

    def test_nothing_to_say_prints_nothing(self) -> None:
        assert self.render([]) == []

    def test_models_with_tokens_left_out_say_how_many(self) -> None:
        out = self.render([self.entry("a", "unverified", 1_500_000), self.entry("b", "unknown", 500_000)])
        assert out[0] == "warning: 2 models without a verified price, 2.0M tokens are not counted in any dollar figure"

    def test_only_partial_prices_read_as_partly_verified(self) -> None:
        assert self.render([self.entry("p", "partial")])[0] == "warning: some prices are only partly verified"

    @pytest.mark.parametrize("status", ["unverified", "unknown"])
    def test_a_model_without_a_price_and_with_no_tokens_left_out_is_not_partly_verified(self, status: str) -> None:
        """A recommended model has no verified price at all: it has not lost any of your tokens, but it is not 'partly' priced."""
        out = self.render([self.entry("rec", status)])
        assert out[0] == "warning: 1 model without a verified price"
        assert "partly" not in " ".join(out)

    def test_a_partial_one_next_to_an_unpriced_one_does_not_call_the_unpriced_one_partly_verified(self) -> None:
        out = self.render([self.entry("p", "partial"), self.entry("rec", "unverified")])
        assert out[0] == "warning: 1 model without a verified price"

    def test_tokens_left_out_and_a_recommended_model_without_a_price_are_counted_together(self) -> None:
        out = self.render([self.entry("a", "unverified", 1_000_000), self.entry("rec", "unknown")])
        assert out[0] == "warning: 2 models without a verified price, 1.0M tokens are not counted in any dollar figure"

    def test_every_warning_is_listed_under_the_header(self) -> None:
        out = self.render([self.entry("a", "unverified", 1_000_000), self.entry("p", "partial")])
        assert any("a: unverified message" in line and "1.0M tokens" in line for line in out)
        assert any("p: partial message" in line for line in out)


class TestPriceWarnings:
    """The warnings behind a number on screen: the logged models, then the models the router recommends."""

    def cfg(self, make_config: Callable[..., Config], **recommend: Any) -> Config:
        return make_config(timezone="UTC", router={"recommend": {"light": None, "medium": None, "heavy": None, **recommend}})

    def rows(self, store: Store, make_record: Make, table: PriceTable, *models: str) -> list[Any]:
        store.upsert_session(make_record("s1", usage={m: Usage(input=1_000) for m in models}))
        return priced_rows(store.sessions(), table, UTC)

    def test_every_logged_price_problem_is_named_with_its_excluded_tokens(
        self, store: Store, make_record: Make, table: PriceTable, make_config: Callable[..., Config]
    ) -> None:
        rows = self.rows(store, make_record, table, "big", "unver", "partial", "mystery")
        found = price_warnings(rows, table, self.cfg(make_config))
        assert [(w["model"], w["status"], w["excluded_tokens"]) for w in found] == [
            ("mystery", "unknown", 1_000), ("partial", "partial", 0), ("unver", "unverified", 1_000),
        ]

    def test_a_recommended_model_without_a_verified_price_is_added_with_no_tokens_left_out(
        self, store: Store, make_record: Make, table: PriceTable, make_config: Callable[..., Config]
    ) -> None:
        rows = self.rows(store, make_record, table, "big")
        found = price_warnings(rows, table, self.cfg(make_config, light="unver", medium="ghost"))
        assert [(w["model"], w["status"], w["excluded_tokens"]) for w in found] == [
            ("ghost", "unknown", 0), ("unver", "unverified", 0),
        ]
        assert found[1]["message"] == table.warning_for("unver")

    def test_a_recommended_model_that_is_only_partly_priced_has_the_partial_status(
        self, store: Store, make_record: Make, table: PriceTable, make_config: Callable[..., Config]
    ) -> None:
        found = price_warnings(self.rows(store, make_record, table, "big"), table, self.cfg(make_config, light="partial"))
        assert [(w["model"], w["status"], w["excluded_tokens"]) for w in found] == [("partial", "partial", 0)]

    def test_a_recommended_model_that_is_also_logged_is_named_once(
        self, store: Store, make_record: Make, table: PriceTable, make_config: Callable[..., Config]
    ) -> None:
        rows = self.rows(store, make_record, table, "unver")
        found = price_warnings(rows, table, self.cfg(make_config, light="unver", medium="unver"))
        assert [(w["model"], w["excluded_tokens"]) for w in found] == [("unver", 1_000)]

    def test_verified_models_and_unset_recommendations_add_nothing(
        self, store: Store, make_record: Make, table: PriceTable, make_config: Callable[..., Config]
    ) -> None:
        rows = self.rows(store, make_record, table, "big")
        assert price_warnings(rows, table, self.cfg(make_config, light="small")) == []
        assert price_warnings([], table, self.cfg(make_config)) == []


class TestResolveFilters:
    """The period label and the spelling of a date."""

    def filters(self, make_config: Callable[..., Config], **options: Any) -> Any:
        given = {"week": False, "week_of": None, "since": None, "until": None, "project": None, "agent": None, **options}
        return resolve_filters(make_config(timezone="UTC"), **given)

    @pytest.mark.parametrize(
        ("options", "label"),
        [
            ({}, "all time"),
            ({"since": "2026-09-01"}, "from 2026-09-01"),
            ({"until": "2026-09-30"}, "up to 2026-09-30"),
            ({"since": " 2026-09-01", "until": "2026-09-30 "}, "2026-09-01 .. 2026-09-30"),
            ({"week_of": "2026-W39"}, "week 2026-09-21 .. 2026-09-27"),
        ],
    )
    def test_the_label_names_the_ends_that_are_given(self, make_config: Callable[..., Config], options: dict[str, str], label: str) -> None:
        found = self.filters(make_config, **options)
        assert found.period.label == label and found.scope() == label
        assert found.as_json()["period"]["label"] == label

    def test_the_label_does_not_touch_the_bounds(self, make_config: Callable[..., Config]) -> None:
        period = self.filters(make_config, since="2026-09-01").period
        assert (period.since, period.until) == ("2026-09-01T00:00:00Z", None)

    @pytest.mark.parametrize("day", ["2026-9-1", "2026-09-1", "2026-9-01", "26-09-01", "2026/09/01", "20260901", "2026-09-01T00", "２０２６-09-01", "today"])
    def test_a_date_has_one_spelling(self, make_config: Callable[..., Config], day: str) -> None:
        for option in ("since", "until"):
            with pytest.raises(ConfigError, match=f"invalid date {day!r}: expected YYYY-MM-DD"):
                self.filters(make_config, **{option: day})

    def test_a_date_that_does_not_exist_is_still_an_error(self, make_config: Callable[..., Config]) -> None:
        with pytest.raises(ConfigError, match="expected YYYY-MM-DD"):
            self.filters(make_config, until="2026-02-30")


class TestSpendText:
    TOTAL = {"cents": 330, "ucents": 330_000_000}

    def test_the_text_for_nothing_priced_can_be_chosen(self) -> None:
        assert spend_text(self.TOTAL, 100, 100) == "n/a (nothing priced)"
        assert spend_text(self.TOTAL, 100, 100, "n/a") == "n/a"

    def test_the_amount_carries_a_plus_when_tokens_are_left_out(self) -> None:
        assert spend_text(self.TOTAL, 0, 100) == "$3.30" and spend_text(self.TOTAL, 10, 100, "n/a") == "$3.30+"


class TestWholeCells:
    def draw(self, rows: list[list[str]], width: int) -> str:
        buffer = io.StringIO()
        print_table(Console(file=buffer, width=width, force_terminal=False, color_system=None),
                    [Col("Name", min_width=6), Col("Cost", True)], rows)
        return buffer.getvalue()

    def test_a_label_with_a_figure_is_not_cut_while_the_column_can_stay_as_wide(self) -> None:
        rows = [["a-very-long-project-name-indeed", "$1.00"], [Whole("3 more (total $1.20)"), "$1.20"]]
        for width in (40, 30, 27):
            text = self.draw(rows, width)
            assert "3 more (total $1.20)" in text and max(len(line) for line in text.splitlines()) <= width, width
        assert "a-very-long-project-name-indeed" not in self.draw(rows, 27)  # the ordinary name gave way instead
        assert "3 more (total $1.20)" in self.draw(rows, 22)  # below that the table runs wider than the console, never cuts the figure

    def test_an_ordinary_cell_of_the_same_text_is_cut_as_before(self) -> None:
        text = self.draw([["a-very-long-project-name-indeed", "$1.00"], ["3 more (total $1.20)", "$1.20"]], 22)
        assert "3 more (total $1.20)" not in text


class AsciiBuffer(io.StringIO):
    encoding = "ascii"  # what a console that cannot write an ellipsis reports


class TestCutCells:
    """A cell that is cut loses its end, one ellipsis style in the whole column, and two cells never come out the same."""

    MODELS = ["claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-4-5-20251101", "claude-haiku-4-5"]

    def draw(self, cells: list[str], width: int, col: Col, *, ascii_only: bool = False) -> list[str]:
        buffer = AsciiBuffer() if ascii_only else io.StringIO()
        console = Console(file=buffer, width=width, force_terminal=False, color_system=None, legacy_windows=False)
        print_table(console, [Col("Project", min_width=7), col, Col("Cost", True)], [["p", cell, "$1.00"] for cell in cells])
        return [line.split()[1].strip("|") for line in buffer.getvalue().splitlines()[2:] if line.strip()]  # an ASCII table has its bars

    @pytest.mark.parametrize("width", [20, 24, 28, 32, 36, 40, 50, 80])
    def test_every_label_is_the_start_of_one_name_and_they_are_all_different(self, width: int) -> None:
        shown = self.draw(self.MODELS, width, Col("Model", min_width=16, cut_last=True))
        assert len(set(shown)) == 4, (width, shown)  # however far the columns are crushed
        for label in shown:
            head = label.removesuffix("…")
            assert [m for m in self.MODELS if m.startswith(head)] and "…" not in head, (width, label)  # the start of a name, cut at its end

    def test_the_cut_falls_before_a_number_not_inside_it(self) -> None:
        assert _cut("claude-opus-4-5-20251101", 21, "…") == "claude-opus-4-5-…"  # not claude-opus-4-5-2025…
        assert _cut("gpt-6.1-sol", 7, "…") == "gpt-…"  # 6.1 is one number
        assert _cut("gpt-6.1-sol", 8, "…") == "gpt-6.1…"
        assert _cut("2026-09-30", 4, "…") == "202…"  # a cell that is one number has nothing before it to stop at
        assert _cut("whole", 5, "…") == "whole" and _cut("abcdef", 1, "…") == "…"

    def test_the_width_of_the_model_column_is_raised_until_the_labels_differ(self) -> None:
        names = ["acme-large-two-preview", "acme-large-two-lite", "acme-large-three-preview"]
        shown = self.draw(names, 40, Col("Model", min_width=10))
        assert len(set(shown)) == 3 and all(label.startswith("acme-large-t") for label in shown)

    def test_an_ascii_console_has_one_stand_in_for_the_ellipsis(self) -> None:
        shown = self.draw(self.MODELS, 36, Col("Model", min_width=16, cut_last=True), ascii_only=True)
        cut = [label for label in shown if label not in self.MODELS]
        assert cut and all(label.endswith("~") and label.count("~") == 1 for label in cut)
        assert "…" not in "".join(shown) and "..." not in "".join(shown)

    def test_a_column_marked_middle_still_keeps_the_start_and_the_tail(self) -> None:
        shown = self.draw(["claude-opus-4-5-20251101", "gpt-6.1-sol"], 28, Col("Model", min_width=12, middle=True))
        assert any("…" in label and not label.endswith("…") for label in shown)  # the ellipsis inside, as that column asked
