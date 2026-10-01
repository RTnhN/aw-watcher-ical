"""ActivityWatch adapter and idempotent reconciliation."""

import json

import requests
from aw_client import ActivityWatchClient
from aw_core.models import Event

from .calendar import identity
from .config import NAME


class TimedClient(ActivityWatchClient):
    # aw-client's synchronous requests have no timeout. Bound every request here.
    def _request(self, method, endpoint, **kwargs):
        response = requests.request(
            method, self._url(endpoint), timeout=(5, 10), **kwargs
        )
        response.raise_for_status()
        return response

    def _get(self, endpoint, params=None):
        return self._request("GET", endpoint, params=params)

    def _post(self, endpoint, data, params=None):
        return self._request("POST", endpoint, json=data, params=params)

    def _delete(self, endpoint, data=None):
        return self._request("DELETE", endpoint, json={} if data is None else data)


def bucket_id(client, cid):
    mode = "-testing" if client.testing else ""
    return f"{NAME}{mode}_{client.client_hostname}_{cid}"


def event_payload(event):
    return {
        "timestamp": event.timestamp.isoformat(),
        "duration": event.duration.total_seconds(),
        "data": event.data,
    }


def reconcile(client, state, cid):
    bucket = bucket_id(client, cid)
    metadata = client.get_buckets().get(bucket)
    if metadata is None:
        client.create_bucket(bucket, event_type="calendar_data")
    elif (
        metadata.get("client") != client.client_name
        or metadata.get("type") != "calendar_data"
    ):
        raise ValueError("bucket ownership mismatch")
    existing = {}
    for event in client.get_events(bucket, limit=-1):
        data = event.data
        if data.get("calendar_id") == cid and "uid" in data and "recurrence_id" in data:
            existing.setdefault(
                identity(data["uid"], data["recurrence_id"]), []
            ).append(event)
    counts = {"added": 0, "updated": 0, "removed": 0}
    for key, row in state.entries(cid).items():
        matches = sorted(existing.get(key, []), key=lambda e: e.id)
        if row["deleted"]:
            for event in matches:
                client.delete_event(bucket, event.id)
                counts["removed"] += 1
            state.applied(cid, key, None)
            continue
        desired = json.loads(row["payload"])
        if matches:
            current = matches[0]
            if event_payload(current) != desired:
                client.insert_event(bucket, Event(id=current.id, **desired))
                counts["updated"] += 1
            for duplicate in matches[1:]:
                client.delete_event(bucket, duplicate.id)
                counts["removed"] += 1
            state.applied(cid, key, current.id)
        else:
            # If this succeeds but the response is lost, the next pass reads the
            # bucket first and finds the occurrence instead of inserting it twice.
            client.insert_event(bucket, Event(**desired))
            counts["added"] += 1
            # aw-client 0.5.15 does not return the assigned ID; subsequent reads do.
            state.applied(cid, key, None)
    state.synced(cid)
    return counts
