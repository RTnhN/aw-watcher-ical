"""Bounded feed retrieval. Errors deliberately never carry URLs or payloads."""

import multiprocessing
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests

MAX_BYTES = 10 * 1024 * 1024
DEADLINE = 30


class FeedError(Exception):
    def __init__(self, category, retry_after=0):
        super().__init__(category)
        self.retry_after = retry_after


@dataclass
class Download:
    body: bytes | None
    etag: str | None = None
    modified: str | None = None


def retry_seconds(value):
    try:
        return max(0, int(value))
    except TypeError, ValueError:
        try:
            return max(
                0, (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            )
        except TypeError, ValueError, OverflowError:
            return 0


def fetch(url, etag=None, modified=None):
    headers = {"User-Agent": "aw-watcher-ical/0.1", "Accept": "text/calendar"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified
    started = time.monotonic()
    try:
        with requests.Session() as session:
            for redirect in range(6):
                if time.monotonic() - started >= DEADLINE:
                    raise FeedError("download deadline exceeded")
                with session.get(
                    url,
                    headers=headers,
                    stream=True,
                    allow_redirects=False,
                    timeout=(5, 5),
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        target = urljoin(url, response.headers.get("Location", ""))
                        parts = urlsplit(target)
                        if (
                            redirect == 5
                            or not response.headers.get("Location")
                            or parts.scheme != "https"
                            or not parts.hostname
                            or parts.username
                            or parts.password
                        ):
                            raise FeedError("invalid or excessive redirects")
                        url = target
                        continue
                    if response.status_code == 304:
                        return Download(
                            None,
                            response.headers.get("ETag", etag),
                            response.headers.get("Last-Modified", modified),
                        )
                    if response.status_code != 200:
                        raise FeedError(
                            f"HTTP {response.status_code}",
                            retry_seconds(response.headers.get("Retry-After")),
                        )
                    body = bytearray()
                    for chunk in response.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise FeedError("feed exceeds size limit")
                        if time.monotonic() - started >= DEADLINE:
                            raise FeedError("download deadline exceeded")
                    return Download(
                        bytes(body),
                        response.headers.get("ETag"),
                        response.headers.get("Last-Modified"),
                    )
    except FeedError:
        raise
    except requests.RequestException, ValueError, OSError:
        raise FeedError("network or TLS failure") from None


def _fetch_worker(connection, args):
    try:
        connection.send((True, fetch(*args)))
    except FeedError as error:
        connection.send((False, (str(error), error.retry_after)))
    except Exception:  # noqa: BLE001 - sanitize errors at external boundaries
        connection.send((False, ("download failed", 0)))
    finally:
        connection.close()


def bounded_fetch(url, etag=None, modified=None):
    """Enforce a wall-clock deadline even during stalled DNS or trickle responses."""
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_fetch_worker, args=(child, (url, etag, modified)))
    process.start()
    child.close()
    try:
        if not parent.poll(DEADLINE):
            raise FeedError("download deadline exceeded")
        try:
            success, result = parent.recv()
        except EOFError:
            raise FeedError("download worker failed") from None
        if not success:
            raise FeedError(*result)
        return result
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join()
