from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import CAL, FakeClient, event, ics

from aw_watcher_ical.calendar import parse_calendar
from aw_watcher_ical.config import CalendarConfig, Config
from aw_watcher_ical.feed import Download, FeedError
from aw_watcher_ical.watcher import Watcher, backoff


def current_body():
    start = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
    return ics(
        event(
            start=start.strftime("%Y%m%dT%H%M%SZ"),
            end=(start + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"),
        )
    )


def config(calendars=(CAL,)):
    return Config(calendars, timezone=ZoneInfo("UTC"))


def test_offline_start_and_recovery(state, caplog):
    client = FakeClient()

    def offline(*args):
        raise FeedError("network or TLS failure")

    watcher = Watcher(config(), state, client, fetch=offline, parse=parse_calendar)
    assert watcher.run(once=True) == 1
    assert not client.events
    assert CAL.url not in caplog.text
    watcher.fetch = lambda *args: Download(current_body(), '"v1"')
    assert watcher.run(once=True) == 0
    assert len(client.events) == 1
    assert state.feed(CAL)["synced"]


def test_activitywatch_down_persists_and_recovers(state):
    client = FakeClient()
    client.offline = True
    watcher = Watcher(
        config(),
        state,
        client,
        fetch=lambda *args: Download(current_body()),
        parse=parse_calendar,
    )
    assert watcher.run(once=True) == 1
    assert state.feed(CAL)["body"]
    assert not state.feed(CAL)["synced"]
    client.offline = False
    assert watcher.run(once=True) == 0
    assert len(client.events) == 1


def test_bad_feed_and_internet_outage_keep_accepted_data(state):
    client = FakeClient()
    watcher = Watcher(
        config(),
        state,
        client,
        fetch=lambda *args: Download(current_body(), '"old"'),
        parse=parse_calendar,
    )
    assert watcher.run(once=True) == 0
    accepted = state.feed(CAL)["body"]
    watcher.fetch = lambda *args: Download(b"invalid", '"new"')
    assert watcher.run(once=True) == 1
    assert state.feed(CAL)["body"] == accepted
    assert state.feed(CAL)["etag"] == '"old"'
    assert len(client.events) == 1


def test_304_reexpands_cached_feed(state):
    client = FakeClient()
    watcher = Watcher(
        config(),
        state,
        client,
        fetch=lambda *args: Download(current_body(), '"tag"'),
        parse=parse_calendar,
    )
    assert watcher.run(once=True) == 0
    state.db.execute("DELETE FROM entries")
    state.db.commit()
    calls = []

    def unchanged(url, etag, modified):
        calls.append(etag)
        return Download(None, etag)

    watcher.fetch = unchanged
    assert watcher.run(once=True) == 0
    assert calls == ['"tag"']
    assert len(state.entries(CAL.id)) == 1
    assert len(client.events) == 1


def test_one_calendar_failure_does_not_block_other(state):
    client = FakeClient()
    second = CalendarConfig("work", "https://example.com/work")

    def fetch(url, *args):
        if url == CAL.url:
            raise FeedError("HTTP 429", 1200)
        return Download(current_body())

    watcher = Watcher(
        config((CAL, second)), state, client, fetch=fetch, parse=parse_calendar
    )
    assert watcher.run(once=True) == 1
    assert state.feed(second)
    assert watcher.schedules[CAL.id].fetch_failures == 1
    assert len(client.events) == 1


def test_capped_backoff_and_retry_after():
    assert 24 <= backoff(1, 900) <= 36
    assert backoff(100, 900) <= 900
    assert backoff(1, 900, 3600) == 3600
