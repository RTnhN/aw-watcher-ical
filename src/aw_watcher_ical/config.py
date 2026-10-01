"""ActivityWatch-standard configuration with stable calendar identities."""

import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from aw_core.config import load_config_toml
from tzlocal import get_localzone

NAME = "aw-watcher-ical"
DEFAULT_CONFIG = """
[aw-watcher-ical]
poll_time = 900 # seconds
past_days = 90 # days
future_days = 30 # days
# timezone = "America/Chicago"
calendars = []

# To enable a calendar, uncomment its entire example block and replace the URL.
# Remove calendars = [] above if you have uncommented that setting.
# [[aw-watcher-ical.calendars]]
# id = "personal"
# url = "https://example.com/private-calendar.ics"
#
# [[aw-watcher-ical.calendars]]
# id = "work"
# url = "webcal://example.com/work-calendar.ics"

[aw-watcher-ical-testing]
poll_time = 900 # seconds
past_days = 90 # days
future_days = 30 # days
calendars = []

# [[aw-watcher-ical-testing.calendars]]
# id = "test-calendar"
# url = "https://example.com/test-calendar.ics"
""".strip()


@dataclass(frozen=True)
class CalendarConfig:
    id: str
    url: str


@dataclass(frozen=True)
class Config:
    calendars: tuple[CalendarConfig, ...]
    poll_time: float = 900
    past_days: int = 90
    future_days: int = 30
    timezone: object = None


def parse_config(section):
    poll = section.get("poll_time", 900)
    if (
        isinstance(poll, bool)
        or not isinstance(poll, (int, float))
        or not math.isfinite(poll)
        or poll <= 0
    ):
        raise ValueError("poll_time must be a positive finite number of seconds")
    days = [section.get("past_days", 90), section.get("future_days", 30)]
    if any(
        isinstance(d, bool) or not isinstance(d, int) or d < 0 for d in days
    ) or not any(days):
        raise ValueError(
            "date windows must be nonnegative integers with a nonempty range"
        )
    try:
        tz = ZoneInfo(section["timezone"]) if "timezone" in section else get_localzone()
    except KeyError, ValueError, TypeError:
        raise ValueError("timezone must be a valid IANA timezone") from None
    calendars = []
    seen = set()
    items = section.get("calendars", [])
    if not isinstance(items, list):
        raise TypeError("calendars must be a list of tables")
    for item in items:
        if not isinstance(item, dict):
            raise TypeError("each calendar must be a table")
        cid, url = item.get("id"), item.get("url")
        if (
            not isinstance(cid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", cid)
            or cid in seen
        ):
            raise ValueError(
                "calendar IDs must be unique and contain only letters, numbers, underscores or hyphens"
            )
        if not isinstance(url, str):
            raise TypeError(f"calendar {cid}: URL must be a string")
        if url.startswith("webcal://"):
            url = "https://" + url[9:]
        try:
            parsed = urlsplit(url)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and parsed.port != 0
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid or any(c.isspace() for c in url):
            raise ValueError(
                f"calendar {cid}: URL must be an HTTPS URL without user information or fragments"
            )
        seen.add(cid)
        calendars.append(CalendarConfig(cid, url))
    return Config(tuple(calendars), float(poll), *(int(d) for d in days), tz)


def load_config(testing=False):
    sections = load_config_toml(NAME, DEFAULT_CONFIG)
    return parse_config(sections[NAME + ("-testing" if testing else "")])
