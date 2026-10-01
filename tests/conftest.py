from copy import deepcopy
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from aw_core.models import Event

from aw_watcher_ical.calendar import parse_calendar
from aw_watcher_ical.config import CalendarConfig
from aw_watcher_ical.state import State

START = datetime(2026, 9, 1, tzinfo=UTC)
STOP = datetime(2026, 12, 1, tzinfo=UTC)
CAL = CalendarConfig("personal", "https://example.com/secret-token")


def ics(*events, extra=""):
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Tests//EN\r\n"
        + extra
        + "".join(
            "BEGIN:VEVENT\r\n" + e.replace("\n", "\r\n") + "\r\nEND:VEVENT\r\n"
            for e in events
        )
        + "END:VCALENDAR\r\n"
    ).encode()


def event(
    uid="one",
    title="Meeting",
    start="20261001T090000Z",
    end="20261001T100000Z",
    extra="",
):
    return f"UID:{uid}\nSUMMARY:{title}\nDTSTART:{start}\nDTEND:{end}" + (
        "\n" + extra if extra else ""
    )


def parse(body):
    return parse_calendar(body, CAL.id, START, STOP, ZoneInfo("America/Chicago"))


@pytest.fixture
def state(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    yield state
    state.close()


class FakeClient:
    testing = False
    client_hostname = "test-host"
    client_name = "aw-watcher-ical"

    def __init__(self):
        self.buckets = {}
        self.events = {}
        self.next_id = 1
        self.writes = []
        self.offline = False
        self.lose_response = False

    def get_buckets(self):
        if self.offline:
            raise ConnectionError("offline")
        return self.buckets

    def create_bucket(self, bucket_id, event_type):
        self.buckets[bucket_id] = {"client": self.client_name, "type": event_type}

    def get_events(self, bucket_id, limit=-1):
        return list(self.events.values())

    def insert_event(self, bucket_id, event):
        if event.id is None:
            event.id = self.next_id
            self.next_id += 1
        self.events[event.id] = Event(**deepcopy(event.to_json_dict()))
        self.writes.append(("insert", event.id))
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("response lost")

    def delete_event(self, bucket_id, event_id):
        self.events.pop(event_id, None)
        self.writes.append(("delete", event_id))
