from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
import requests

from aw_watcher_ical import feed
from aw_watcher_ical.feed import FeedError, fetch, retry_seconds


class Response:
    def __init__(self, status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks or [b"calendar"]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def iter_content(self, size):
        yield from self.chunks


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def session(monkeypatch, *responses):
    result = Session(responses)
    monkeypatch.setattr(feed.requests, "Session", lambda: result)
    return result


def test_conditional_request(monkeypatch):
    http = session(monkeypatch, Response(304))
    result = fetch("https://example.com/private", '"tag"', "yesterday")
    assert result.body is None
    assert result.etag == '"tag"'
    assert http.calls[0][1]["headers"]["If-None-Match"] == '"tag"'
    assert http.calls[0][1]["headers"]["If-Modified-Since"] == "yesterday"
    assert http.calls[0][1]["timeout"] == (5, 5)
    assert "verify" not in http.calls[0][1]  # requests verifies TLS by default


def test_download_and_https_redirect(monkeypatch):
    session(
        monkeypatch,
        Response(302, {"Location": "https://cdn.example.com/calendar"}),
        Response(headers={"ETag": '"new"'}, chunks=[b"one", b"two"]),
    )
    result = fetch("https://example.com/private")
    assert result.body == b"onetwo"
    assert result.etag == '"new"'


@pytest.mark.parametrize(
    "target",
    [
        "http://example.com/private",
        "https://user:secret@example.com/private",
        "file:///tmp/calendar",
    ],
)
def test_unsafe_redirects(monkeypatch, target):
    session(monkeypatch, Response(302, {"Location": target}))
    with pytest.raises(FeedError, match="redirects"):
        fetch("https://example.com/private")


def test_redirect_limit(monkeypatch):
    session(monkeypatch, *[Response(302, {"Location": "/next"}) for _ in range(6)])
    with pytest.raises(FeedError, match="redirects"):
        fetch("https://example.com/private")


def test_size_limit(monkeypatch):
    monkeypatch.setattr(feed, "MAX_BYTES", 4)
    session(monkeypatch, Response(chunks=[b"123", b"456"]))
    with pytest.raises(FeedError, match="size limit"):
        fetch("https://example.com/private")


def test_rate_limit_and_redacted_network_error(monkeypatch):
    session(monkeypatch, Response(429, {"Retry-After": "1200"}))
    with pytest.raises(FeedError) as caught:
        fetch("https://example.com/private")
    assert caught.value.retry_after == 1200
    session(monkeypatch, requests.ConnectionError("https://example.com/secret-token"))
    with pytest.raises(FeedError) as caught:
        fetch("https://example.com/secret-token")
    assert "secret-token" not in str(caught.value)


def test_retry_after_formats():
    assert retry_seconds("120") == 120
    assert retry_seconds("bad") == 0
    value = format_datetime(datetime.now(UTC) + timedelta(seconds=120))
    assert 118 <= retry_seconds(value) <= 120
