import json

import requests
from aw_core.models import Event

from aw_watcher_ical.sync import TimedClient


def test_activitywatch_wire_requests_are_bounded_and_preserve_update_id(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b"[]"
        return response

    monkeypatch.setattr(requests, "request", request)
    client = TimedClient.__new__(TimedClient)
    client.server_address = "http://localhost:5600"
    event = Event(
        id=42,
        timestamp="2026-10-01T09:00:00+00:00",
        duration=3600,
        data={"title": "Changed"},
    )
    client.insert_event("calendar", event)
    client.get_events("calendar", limit=-1)
    client.delete_event("calendar", 42)
    assert [c[0] for c in calls] == ["POST", "GET", "DELETE"]
    assert calls[0][2]["json"][0]["id"] == 42
    assert json.loads(json.dumps(calls[0][2]["json"]))[0]["duration"] == 3600
    assert all(c[2]["timeout"] == (5, 10) for c in calls)
    assert calls[2][1].endswith("/calendar/events/42")
