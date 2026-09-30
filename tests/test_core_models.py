from datetime import datetime, timedelta, timezone

import pytest

from agentfare.models import SLOT_MINUTES, SessionRecord, Usage, short_id, slot_of


class TestUsage:
    def test_the_first_four_fields_are_positional_and_1h_writes_come_last(self) -> None:
        u = Usage(1, 2, 3, 4)
        assert (u.input, u.cache_read, u.cache_write, u.output, u.cache_write_1h) == (1, 2, 3, 4, 0)
        assert Usage(1, 2, 3, 4, 5).cache_write_1h == 5

    def test_addition_covers_every_bucket(self) -> None:
        got = Usage(1, 2, 3, 4, 5) + Usage(10, 20, 30, 40, 50)
        assert got == Usage(11, 22, 33, 44, cache_write_1h=55)

    def test_total_input_and_total_count_both_kinds_of_cache_write(self) -> None:
        u = Usage(input=1, cache_read=2, cache_write=3, output=4, cache_write_1h=5)
        assert u.total_input == 1 + 2 + 3 + 5 == 11
        assert u.total == 15

    @pytest.mark.parametrize("bucket", ["input", "cache_read", "cache_write", "output", "cache_write_1h"])
    def test_a_single_non_zero_bucket_is_not_empty(self, bucket: str) -> None:
        assert Usage().is_empty()
        assert not Usage(**{bucket: 1}).is_empty()  # in particular a session that only wrote 1-hour cache is still spend

    def test_the_default_is_all_zero_and_usage_is_immutable(self) -> None:
        assert Usage() == Usage(0, 0, 0, 0, 0)
        with pytest.raises(AttributeError):
            Usage().input = 1  # type: ignore[misc]


class TestSessionRecord:
    def test_total_usage_sums_the_models_including_1h_writes(self) -> None:
        rec = SessionRecord(
            agent="claude", session_id="s", source_path="/x", project="p", started_at="2026-09-30T10:00:00Z",
            ended_at="2026-09-30T10:00:00Z", wall_seconds=0, active_seconds=0,
            usage={"a": Usage(1, 2, 3, 4, 5), "b": Usage(cache_write_1h=7)},
        )
        assert rec.total_usage == Usage(1, 2, 3, 4, 12)
        assert rec.primary_model == "a"


class TestSlotOf:
    @pytest.mark.parametrize(
        ("ts", "slot"),
        [
            ("2026-09-30T10:00:00", "2026-09-30T10:00:00Z"),
            ("2026-09-30T10:14:59.999999", "2026-09-30T10:00:00Z"),
            ("2026-09-30T10:15:00", "2026-09-30T10:15:00Z"),
            ("2026-09-30T10:44:59", "2026-09-30T10:30:00Z"),
            ("2026-09-30T10:59:59", "2026-09-30T10:45:00Z"),
            ("2026-09-30T23:59:59", "2026-09-30T23:45:00Z"),
            ("2026-10-01T00:00:00", "2026-10-01T00:00:00Z"),
        ],
    )
    def test_a_slot_is_the_utc_quarter_hour_that_holds_the_instant(self, ts: str, slot: str) -> None:
        assert SLOT_MINUTES == 15
        assert slot_of(datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)) == slot

    def test_a_naive_instant_is_read_as_utc(self) -> None:
        assert slot_of(datetime(2026, 9, 30, 10, 20)) == "2026-09-30T10:15:00Z"

    def test_an_instant_with_an_offset_is_converted_to_utc_first(self) -> None:
        assert slot_of(datetime(2026, 9, 30, 7, 20, tzinfo=timezone(timedelta(hours=-3)))) == "2026-09-30T10:15:00Z"
        assert slot_of(datetime(2026, 10, 1, 5, 45, tzinfo=timezone(timedelta(hours=5, minutes=45)))) == "2026-10-01T00:00:00Z"  # Kathmandu

    def test_every_minute_of_the_day_lands_in_a_slot_that_starts_no_later_and_less_than_a_quarter_hour_before(self) -> None:
        start = datetime(2026, 9, 30, tzinfo=timezone.utc)
        for minute in range(24 * 60):
            at = start + timedelta(minutes=minute, seconds=37)
            slot = datetime.fromisoformat(slot_of(at).replace("Z", "+00:00"))
            assert timedelta(0) <= at - slot < timedelta(minutes=SLOT_MINUTES)

    def test_a_slot_maps_to_one_local_day_in_every_real_offset(self) -> None:
        """Offsets are whole quarter hours, so a slot never straddles a local midnight."""
        slot = datetime.fromisoformat(slot_of(datetime(2026, 9, 27, 18, 29, 59, tzinfo=timezone.utc)).replace("Z", "+00:00"))
        for offset in (-12, -3.5, 0, 5.5, 5.75, 8.75, 12.75, 14):
            local = slot.astimezone(timezone(timedelta(hours=offset)))
            assert local.date() == (local + timedelta(minutes=SLOT_MINUTES - 1)).date()


class TestEffectiveSlots:
    def record(self, **kw: object) -> SessionRecord:
        return SessionRecord(
            agent="claude", session_id="s", source_path="/x", project="p", started_at="2026-09-30T10:37:12Z",
            ended_at="2026-09-30T11:00:00Z", wall_seconds=0, active_seconds=0, **kw,  # type: ignore[arg-type]
        )

    def test_recorded_slots_are_used_as_they_are(self) -> None:
        slots = {("2026-09-30T10:30:00Z", "m"): Usage(input=1), ("2026-09-30T11:00:00Z", "m"): Usage(input=2)}
        rec = self.record(usage={"m": Usage(input=3)}, slots=slots)
        assert rec.effective_slots() == slots

    def test_without_slots_all_of_the_usage_counts_at_the_slot_of_the_start(self) -> None:
        rec = self.record(usage={"m": Usage(input=3), "n": Usage(output=4)})
        assert rec.effective_slots() == {("2026-09-30T10:30:00Z", "m"): Usage(input=3), ("2026-09-30T10:30:00Z", "n"): Usage(output=4)}

    def test_a_start_with_an_offset_is_slotted_in_utc(self) -> None:
        rec = self.record(usage={"m": Usage(input=3)})
        rec.started_at = "2026-09-30T05:59:00-05:00"
        assert rec.effective_slots() == {("2026-09-30T10:45:00Z", "m"): Usage(input=3)}

    def test_no_usage_means_no_slots(self) -> None:
        assert self.record().effective_slots() == {}


class TestShortId:
    def test_it_is_the_tail_because_codex_ids_start_with_a_timestamp(self) -> None:
        assert short_id("01a00000-0000-7000-8000-000000000001") == "00000001"
        assert short_id("01a00000-0000-7000-8000-000000000002") != short_id("01a00000-0000-7000-8000-000000000001")  # same timestamp head

    def test_a_short_id_is_left_whole(self) -> None:
        assert short_id("abc") == "abc"
        assert short_id("") == ""
