"""Independent download and ActivityWatch retry schedules."""

import logging
import random
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .calendar import bounded_parse
from .feed import FeedError, bounded_fetch
from .sync import reconcile

logger = logging.getLogger("aw-watcher-ical")


def backoff(failures, poll_time, retry_after=0):
    cap = max(poll_time, 300)
    delay = min(30 * 2 ** min(failures - 1, 20), cap)
    return max(retry_after, min(cap, delay * random.uniform(0.8, 1.2)))


@dataclass
class Schedule:
    fetch_at: float = 0
    sync_at: float = 0
    fetch_failures: int = 0
    sync_failures: int = 0


class Watcher:
    def __init__(self, config, state, client, fetch=bounded_fetch, parse=bounded_parse):
        self.config, self.state, self.client = config, state, client
        self.fetch, self.parse = fetch, parse
        self.schedules = {calendar.id: Schedule() for calendar in config.calendars}

    def tick(self, once=False):
        healthy = True
        now = datetime.now(UTC)
        local_midnight = now.astimezone(self.config.timezone).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start = (local_midnight - timedelta(days=self.config.past_days)).astimezone(UTC)
        stop = (
            local_midnight + timedelta(days=self.config.future_days + 1)
        ).astimezone(UTC)
        for calendar in self.config.calendars:
            schedule = self.schedules[calendar.id]
            cached = self.state.feed(calendar)
            parsed = False
            if once or time.monotonic() >= schedule.fetch_at:
                try:
                    download = self.fetch(
                        calendar.url,
                        cached["etag"] if cached else None,
                        cached["modified"] if cached else None,
                    )
                    body = (
                        download.body
                        if download.body is not None
                        else cached["body"]
                        if cached
                        else None
                    )
                    if body is None:
                        raise FeedError("unchanged response without cached feed")
                    snapshot = self.parse(
                        body, calendar.id, start, stop, self.config.timezone
                    )
                    self.state.accept(
                        calendar,
                        body,
                        download.etag,
                        download.modified,
                        snapshot,
                        start,
                        stop,
                    )
                    parsed = True
                    schedule.sync_at = 0
                    schedule.fetch_failures = 0
                    schedule.fetch_at = time.monotonic() + self.config.poll_time
                except FeedError as error:
                    healthy = False
                    schedule.fetch_failures += 1
                    schedule.fetch_at = time.monotonic() + backoff(
                        schedule.fetch_failures,
                        self.config.poll_time,
                        error.retry_after,
                    )
                    logger.warning(
                        "calendar %s: %s; retaining accepted data",
                        calendar.id,
                        str(error),
                    )
            if once or time.monotonic() >= schedule.sync_at:
                if not parsed and cached:
                    try:
                        snapshot = self.parse(
                            cached["body"],
                            calendar.id,
                            start,
                            stop,
                            self.config.timezone,
                        )
                        with self.state.db:
                            self.state.expand(calendar.id, snapshot, start, stop)
                    except FeedError as error:
                        healthy = False
                        logger.warning(
                            "calendar %s: cached calendar processing failed (%s)",
                            calendar.id,
                            str(error),
                        )
                if self.state.feed(calendar) is None:
                    schedule.sync_at = schedule.fetch_at
                    continue
                try:
                    counts = reconcile(self.client, self.state, calendar.id)
                    logger.info(
                        "calendar %s: added=%s updated=%s removed=%s",
                        calendar.id,
                        *counts.values(),
                    )
                    schedule.sync_failures = 0
                    schedule.sync_at = time.monotonic() + self.config.poll_time
                except Exception:  # noqa: BLE001 - sanitize errors at external boundaries
                    healthy = False
                    schedule.sync_failures += 1
                    schedule.sync_at = time.monotonic() + backoff(
                        schedule.sync_failures, self.config.poll_time
                    )
                    logger.warning(
                        "calendar %s: ActivityWatch sync failed; retained for retry",
                        calendar.id,
                    )
        return healthy

    def run(self, once=False):
        while True:
            healthy = self.tick(once)
            if once:
                return 0 if healthy else 1
            next_due = min(min(s.fetch_at, s.sync_at) for s in self.schedules.values())
            time.sleep(max(0.1, min(1, next_due - time.monotonic())))
