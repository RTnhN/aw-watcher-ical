"""Normalize bounded iCalendar occurrences and explicit deletion evidence."""

import json
import multiprocessing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import recurring_ical_events
from icalendar import Calendar

from .feed import FeedError

MAX_OCCURRENCES = 50_000


@dataclass
class Snapshot:
    events: dict
    deletions: list


def identity(uid, recurrence_id):
    return json.dumps([uid, recurrence_id], separators=(",", ":"))


def aware(value, tz):
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=tz)
    if isinstance(value, date):
        return datetime.combine(value, time(), tzinfo=tz)
    raise ValueError("invalid calendar time")


def recurrence_identity(value, tz):
    if value is None:
        return None
    if isinstance(value, datetime):
        return aware(value, tz).astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise ValueError("invalid recurrence identity")


def parse_calendar(body, cid, start, stop, fallback_timezone):
    try:
        calendar = Calendar.from_ical(body)
        if calendar.name != "VCALENDAR" or calendar.errors:
            raise ValueError("invalid calendar")
        tz = fallback_timezone
        if calendar.get("X-WR-TIMEZONE"):
            tz = ZoneInfo(str(calendar["X-WR-TIMEZONE"]))
        name = str(calendar.get("X-WR-CALNAME", cid))
        deletions = []
        recurring = set()
        selected = {}
        for component in calendar.walk("VEVENT"):
            if component.errors or not str(component.get("UID", "")).strip():
                raise ValueError("invalid event")
            uid = str(component["UID"])
            # Resolve floating values before recurrence expansion, including EXDATE/RDATE.
            for field in ("DTSTART", "DTEND", "RECURRENCE-ID", "EXDATE", "RDATE"):
                values = component.get(field, [])
                if not isinstance(values, list):
                    values = [values]
                for prop in values:
                    for value in getattr(prop, "dts", [prop]):
                        dt = value.dt
                        if isinstance(dt, datetime) and dt.tzinfo is None:
                            zone = (
                                ZoneInfo(
                                    str(
                                        value.params.get(
                                            "TZID", prop.params.get("TZID")
                                        )
                                    )
                                )
                                if value.params.get("TZID", prop.params.get("TZID"))
                                else tz
                            )
                            value.dt = dt.replace(tzinfo=zone)
            rid = recurrence_identity(component.decoded("RECURRENCE-ID", None), tz)
            if "RRULE" in component or "RDATE" in component or rid is not None:
                recurring.add(uid)
            key = (uid, rid)
            sequence = int(component.get("SEQUENCE", 0))
            stamp = str(component.get("DTSTAMP", ""))
            if key not in selected or (sequence, stamp) > selected[key][0]:
                selected[key] = ((sequence, stamp), component)
        active = []
        range_shifts = {}
        for _, component in selected.values():
            uid = str(component["UID"])
            rid = recurrence_identity(component.decoded("RECURRENCE-ID", None), tz)
            if (
                str(component.get("STATUS", "")).upper() == "CANCELLED"
                or str(calendar.get("METHOD", "")).upper() == "CANCEL"
            ):
                future = bool(
                    component.get("RECURRENCE-ID")
                    and component["RECURRENCE-ID"].params.get("RANGE")
                    == "THISANDFUTURE"
                )
                deletions.append((uid, rid, future))
                continue
            if "DTSTART" not in component:
                raise ValueError("missing start")
            dtstart = component.decoded("DTSTART")
            if "DTEND" in component and "DURATION" in component:
                raise ValueError("ambiguous duration")
            dtend = component.decoded("DTEND", None)
            if dtend is None:
                duration = component.decoded("DURATION", None)
                if duration is None:
                    duration = (
                        timedelta(0)
                        if isinstance(dtstart, datetime)
                        else timedelta(days=1)
                    )
                dtend = dtstart + duration
                component.add("DTEND", dtend)
                component.pop("DURATION", None)
            if isinstance(dtstart, datetime) != isinstance(dtend, datetime) or aware(
                dtend, tz
            ) < aware(dtstart, tz):
                raise ValueError("invalid end")
            if (
                rid is not None
                and component["RECURRENCE-ID"].params.get("RANGE") == "THISANDFUTURE"
            ):
                original = component.decoded("RECURRENCE-ID")
                range_shifts[(uid, rid)] = dtstart - original
            excluded = component.get("EXDATE", [])
            if not isinstance(excluded, list):
                excluded = [excluded]
            for prop in excluded:
                for value in prop.dts:
                    deletions.append((uid, recurrence_identity(value.dt, tz), False))
            active.append(component)
        calendar.subcomponents = [
            c for c in calendar.subcomponents if c.name != "VEVENT"
        ] + active
        query = recurring_ical_events.of(calendar, keep_recurrence_attributes=True)
        events = {}
        count = 0
        # Iterate series generators rather than materializing an unbounded result list.
        for series in query.series:
            for occurrence in series.between(start, stop):
                count += 1
                if count > MAX_OCCURRENCES:
                    raise FeedError("calendar exceeds occurrence limit")
                event = occurrence.as_component(True)
                dtstart, dtend = event.decoded("DTSTART"), event.decoded("DTEND")
                begin = aware(dtstart, tz).astimezone(UTC)
                end = aware(dtend, tz).astimezone(UTC)
                if end < start or begin >= stop or (end == start and begin != end):
                    continue
                uid = str(event["UID"])
                rid = (
                    recurrence_identity(event.decoded("RECURRENCE-ID", None), tz)
                    if uid in recurring
                    else None
                )
                if (uid, rid) in range_shifts:
                    rid = recurrence_identity(dtstart - range_shifts[(uid, rid)], tz)
                key = identity(uid, rid)
                events[key] = {
                    "timestamp": begin.isoformat(),
                    "duration": (end - begin).total_seconds(),
                    "data": {
                        "calendar_id": cid,
                        "calendar_name": name,
                        "uid": uid,
                        "recurrence_id": rid,
                        "title": str(event.get("SUMMARY", "")),
                        "all_day": not isinstance(dtstart, datetime),
                    },
                }
        return Snapshot(events, deletions)
    except FeedError:
        raise
    except Exception:  # noqa: BLE001 - sanitize errors at external boundaries
        raise FeedError("invalid calendar or recurrence data") from None


def _parse_worker(connection, args):
    try:
        connection.send((True, parse_calendar(*args)))
    except FeedError as error:
        connection.send((False, str(error)))
    finally:
        connection.close()


def bounded_parse(*args):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_parse_worker, args=(child, args))
    process.start()
    child.close()
    try:
        if not parent.poll(30):
            raise FeedError("calendar processing deadline exceeded")
        try:
            success, result = parent.recv()
        except EOFError:
            raise FeedError("calendar processing worker failed") from None
        if not success:
            raise FeedError(result)
        return result
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join()
