import json

import pytest
from conftest import CAL, START, STOP, FakeClient, event, ics, parse

from aw_watcher_ical.state import State
from aw_watcher_ical.sync import reconcile


def accept(state, body):
    state.accept(CAL, body, '"tag"', None, parse(body), START, STOP)


def test_unchanged_and_updated_event(state):
    client = FakeClient()
    accept(state, ics(event()))
    assert reconcile(client, state, CAL.id)["added"] == 1
    assert reconcile(client, state, CAL.id) == {"added": 0, "updated": 0, "removed": 0}
    accept(
        state,
        ics(event(title="Changed", start="20261001T110000Z", end="20261001T120000Z")),
    )
    assert reconcile(client, state, CAL.id)["updated"] == 1
    assert list(client.events) == [1]
    assert client.events[1].data["title"] == "Changed"


def test_lost_response_then_restart_no_duplicate(tmp_path):
    path = tmp_path / "persist.sqlite3"
    state = State(path)
    client = FakeClient()
    accept(state, ics(event(), event(uid="two")))
    client.lose_response = True
    with pytest.raises(TimeoutError):
        reconcile(client, state, CAL.id)
    state.close()
    state = State(path)
    try:
        assert reconcile(client, state, CAL.id)["added"] == 1
        assert len(client.events) == 2
        assert len(client.writes) == 2
    finally:
        state.close()


def test_disappearance_preserved_and_cancellation_deleted(state):
    client = FakeClient()
    accept(state, ics(event()))
    reconcile(client, state, CAL.id)
    accept(state, ics())
    assert reconcile(client, state, CAL.id)["removed"] == 0
    assert len(client.events) == 1
    accept(state, ics("UID:one\nSTATUS:CANCELLED"))
    assert reconcile(client, state, CAL.id)["removed"] == 1
    assert not client.events
    assert reconcile(client, state, CAL.id)["removed"] == 0


def test_recurring_move_and_exclusion(state):
    client = FakeClient()
    base = event(extra="RRULE:FREQ=DAILY;COUNT=3")
    accept(state, ics(base))
    reconcile(client, state, CAL.id)
    accept(
        state,
        ics(
            base + "\nEXDATE:20261002T090000Z",
            event(
                start="20261003T110000Z",
                end="20261003T120000Z",
                extra="RECURRENCE-ID:20261003T090000Z\nSEQUENCE:1",
            ),
        ),
    )
    assert reconcile(client, state, CAL.id) == {"added": 0, "updated": 1, "removed": 1}
    assert len(client.events) == 2


def test_url_change_retains_identity_clears_validators(state):
    from aw_watcher_ical.config import CalendarConfig

    accept(state, ics(event()))
    changed = CalendarConfig(CAL.id, "https://example.com/new-secret")
    cached = state.feed(changed)
    assert cached["etag"] is None
    assert cached["body"]
    assert len(state.entries(CAL.id)) == 1


def test_older_history_retained_outside_window(state):
    from datetime import timedelta

    accept(state, ics(event()))
    with state.db:
        state.expand(
            CAL.id,
            parse(ics("UID:one\nSTATUS:CANCELLED")),
            STOP,
            STOP + timedelta(days=30),
        )
    assert not next(iter(state.entries(CAL.id).values()))["deleted"]


def test_bucket_ownership_guard(state):
    from aw_watcher_ical.sync import bucket_id

    client = FakeClient()
    accept(state, ics(event()))
    client.buckets[bucket_id(client, CAL.id)] = {
        "client": "someone-else",
        "type": "calendar_data",
    }
    with pytest.raises(ValueError, match="ownership"):
        reconcile(client, state, CAL.id)
    assert not client.writes


def test_duplicate_recovery(state):
    from aw_core.models import Event

    client = FakeClient()
    accept(state, ics(event()))
    reconcile(client, state, CAL.id)
    payload = json.loads(next(iter(state.entries(CAL.id).values()))["payload"])
    client.events[10] = Event(id=10, **payload)
    assert reconcile(client, state, CAL.id)["removed"] == 1
    assert len(client.events) == 1
