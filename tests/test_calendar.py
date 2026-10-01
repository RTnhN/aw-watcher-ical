from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from conftest import event, ics, parse

from aw_watcher_ical.calendar import parse_calendar
from aw_watcher_ical.feed import FeedError


def test_basic_and_private_fields():
    snapshot = parse(
        ics(
            event(
                extra="DESCRIPTION:Secret\nLOCATION:Secret\nATTENDEE:mailto:secret@example.com"
            )
        )
    )
    payload = next(iter(snapshot.events.values()))
    assert payload["duration"] == 3600
    assert payload["data"]["recurrence_id"] is None
    assert "Secret" not in str(snapshot)


def test_recurrences_exdates_rdates_and_moved_override():
    body = ics(
        event(
            extra="RRULE:FREQ=DAILY;COUNT=3\nEXDATE:20261002T090000Z\nRDATE:20261005T090000Z"
        ),
        event(
            start="20261003T110000Z",
            end="20261003T120000Z",
            extra="RECURRENCE-ID:20261003T090000Z\nSEQUENCE:1",
        ),
    )
    snapshot = parse(body)
    data = list(snapshot.events.values())
    assert len(data) == 3
    moved = next(
        e for e in data if e["data"]["recurrence_id"] == "2026-10-03T09:00:00+00:00"
    )
    assert moved["timestamp"] == "2026-10-03T11:00:00+00:00"
    assert ("one", "2026-10-02T09:00:00+00:00", False) in snapshot.deletions


def test_dst_recurring_floating_times():
    snapshot = parse(
        ics(
            event(
                start="20261031T090000",
                end="20261031T100000",
                extra="RRULE:FREQ=DAILY;COUNT=3",
            )
        )
    )
    stamps = [v["timestamp"] for v in snapshot.events.values()]
    assert stamps == [
        "2026-10-31T14:00:00+00:00",
        "2026-11-01T15:00:00+00:00",
        "2026-11-02T15:00:00+00:00",
    ]


def test_calendar_timezone_and_explicit_timezone():
    snapshot = parse(
        ics(
            "UID:a\nDTSTART:20261001T090000\nDURATION:PT1H",
            "UID:b\nDTSTART;TZID=America/Chicago:20261001T090000\nDURATION:PT1H",
            extra="X-WR-TIMEZONE:Europe/London\r\n",
        )
    )
    by_uid = {p["data"]["uid"]: p for p in snapshot.events.values()}
    assert by_uid["a"]["timestamp"] == "2026-10-01T08:00:00+00:00"
    assert by_uid["b"]["timestamp"] == "2026-10-01T14:00:00+00:00"


def test_all_day_exclusive_end_and_dst():
    snapshot = parse(
        ics("UID:a\nDTSTART;VALUE=DATE:20261101\nDTEND;VALUE=DATE:20261103")
    )
    payload = next(iter(snapshot.events.values()))
    assert payload["data"]["all_day"] is True
    assert payload["duration"] == 49 * 3600


@pytest.mark.parametrize(
    "fields,duration",
    [
        ("DTSTART:20261001T090000Z", 0),
        ("DTSTART;VALUE=DATE:20261001", 86400),
        ("DTSTART:20261001T090000Z\nDURATION:PT30M", 1800),
    ],
)
def test_missing_end_and_duration(fields, duration):
    assert (
        next(iter(parse(ics("UID:a\n" + fields)).events.values()))["duration"]
        == duration
    )


def test_explicit_cancellation_without_start():
    snapshot = parse(ics("UID:one\nSTATUS:CANCELLED"))
    assert not snapshot.events
    assert snapshot.deletions == [("one", None, False)]


def test_cancelled_recurrence():
    snapshot = parse(
        ics(
            event(extra="RRULE:FREQ=DAILY;COUNT=3"),
            "UID:one\nRECURRENCE-ID:20261002T090000Z\nSTATUS:CANCELLED",
        )
    )
    assert ("one", "2026-10-02T09:00:00+00:00", False) in snapshot.deletions


@pytest.mark.parametrize(
    "body",
    [
        b"not a calendar",
        ics("DTSTART:20261001T090000Z"),
        ics("UID:a"),
        ics(event(end="20261001T080000Z")),
        ics(event(extra="RRULE:FREQ=INVALID")),
        ics(event(extra="DURATION:PT1H")),
    ],
)
def test_invalid_snapshot_rejected(body):
    with pytest.raises(FeedError):
        parse(body)


def test_occurrence_limit(monkeypatch):
    monkeypatch.setattr("aw_watcher_ical.calendar.MAX_OCCURRENCES", 2)
    with pytest.raises(FeedError, match="occurrence limit"):
        parse(ics(event(extra="RRULE:FREQ=DAILY;COUNT=3")))


def test_window_overlap():
    snapshot = parse_calendar(
        ics(
            event(start="20260930T230000Z", end="20261001T010000Z"),
            event(uid="later", start="20261002T000000Z", end="20261002T010000Z"),
        ),
        "personal",
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 10, 2, tzinfo=UTC),
        ZoneInfo("UTC"),
    )
    assert [p["data"]["uid"] for p in snapshot.events.values()] == ["one"]


def test_bounded_parse_worker():
    from conftest import CAL, START, STOP

    from aw_watcher_ical.calendar import bounded_parse

    snapshot = bounded_parse(ics(event()), CAL.id, START, STOP, ZoneInfo("UTC"))
    assert len(snapshot.events) == 1
    with pytest.raises(FeedError, match="invalid calendar"):
        bounded_parse(b"invalid", CAL.id, START, STOP, ZoneInfo("UTC"))


def test_series_cancellation_and_future_cancellation():
    body = ics(
        event(extra="RRULE:FREQ=DAILY;COUNT=3"),
        "UID:one\nRECURRENCE-ID;RANGE=THISANDFUTURE:20261002T090000Z\nSTATUS:CANCELLED",
    )
    assert ("one", "2026-10-02T09:00:00+00:00", True) in parse(body).deletions


def test_highest_sequence_wins():
    snapshot = parse(
        ics(
            event(title="Old", extra="SEQUENCE:0"),
            event(title="New", extra="SEQUENCE:1"),
        )
    )
    assert len(snapshot.events) == 1
    assert next(iter(snapshot.events.values()))["data"]["title"] == "New"


def test_this_and_future_edit_preserves_distinct_original_identities():
    snapshot = parse(
        ics(
            event(extra="RRULE:FREQ=DAILY;COUNT=4"),
            event(
                start="20261002T110000Z",
                end="20261002T120000Z",
                extra="RECURRENCE-ID;RANGE=THISANDFUTURE:20261002T090000Z\nSEQUENCE:1",
            ),
        )
    )
    assert len(snapshot.events) == 4
    instances = {
        p["data"]["recurrence_id"]: p["timestamp"] for p in snapshot.events.values()
    }
    assert instances["2026-10-03T09:00:00+00:00"] == "2026-10-03T11:00:00+00:00"
    assert instances["2026-10-04T09:00:00+00:00"] == "2026-10-04T11:00:00+00:00"
