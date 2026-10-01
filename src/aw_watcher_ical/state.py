"""Durable desired state; remote writes are reconciled, never blindly replayed."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta


def now_iso():
    return datetime.now(UTC).isoformat()


def url_hash(url):
    return hashlib.sha256(url.encode()).hexdigest()


class State:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS feeds (
                cid TEXT PRIMARY KEY, url_hash TEXT, body BLOB, etag TEXT,
                modified TEXT, fetched TEXT, synced TEXT
            );
            CREATE TABLE IF NOT EXISTS entries (
                cid TEXT, key TEXT, payload TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                event_id INTEGER, PRIMARY KEY(cid, key)
            );
        """)

    def close(self):
        self.db.close()

    def feed(self, calendar):
        self.db.row_factory = sqlite3.Row
        row = self.db.execute(
            "SELECT * FROM feeds WHERE cid=?", (calendar.id,)
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        if result["url_hash"] != url_hash(calendar.url):
            result["etag"] = result["modified"] = None
        return result

    def entries(self, cid):
        return {
            row["key"]: dict(row)
            for row in self.db.execute("SELECT * FROM entries WHERE cid=?", (cid,))
        }

    def accept(self, calendar, body, etag, modified, snapshot, start, stop):
        with self.db:
            self.db.execute(
                """INSERT INTO feeds(cid,url_hash,body,etag,modified,fetched)
                VALUES(?,?,?,?,?,?) ON CONFLICT(cid) DO UPDATE SET
                url_hash=excluded.url_hash,body=excluded.body,etag=excluded.etag,
                modified=excluded.modified,fetched=excluded.fetched""",
                (calendar.id, url_hash(calendar.url), body, etag, modified, now_iso()),
            )
            self.expand(calendar.id, snapshot, start, stop)

    def expand(self, cid, snapshot, start, stop):
        """Caller commits expansion with feed acceptance, or separately for cached feeds."""
        for key, payload in snapshot.events.items():
            self.db.execute(
                """INSERT INTO entries(cid,key,payload) VALUES(?,?,?)
                ON CONFLICT(cid,key) DO UPDATE SET payload=excluded.payload,deleted=0""",
                (cid, key, json.dumps(payload, sort_keys=True)),
            )
        evidence = {}
        for uid, rid, future in snapshot.deletions:
            item = evidence.setdefault(
                uid, {"all": False, "exact": set(), "future": None}
            )
            if rid is None:
                item["all"] = True
            elif future:
                item["future"] = min(item["future"], rid) if item["future"] else rid
            else:
                item["exact"].add(rid)
        for key, row in self.entries(cid).items():
            payload = json.loads(row["payload"])
            begin = datetime.fromisoformat(payload["timestamp"])
            end = begin + timedelta(seconds=payload["duration"])
            if begin >= stop or end < start:
                continue
            data = payload["data"]
            item = evidence.get(data["uid"])
            rid = data["recurrence_id"]
            if item and (
                item["all"]
                or rid in item["exact"]
                or (
                    item["future"] is not None
                    and rid is not None
                    and rid >= item["future"]
                )
            ):
                self.db.execute(
                    "UPDATE entries SET deleted=1 WHERE cid=? AND key=?", (cid, key)
                )

    def applied(self, cid, key, event_id):
        with self.db:
            self.db.execute(
                "UPDATE entries SET event_id=? WHERE cid=? AND key=?",
                (event_id, cid, key),
            )

    def synced(self, cid):
        with self.db:
            self.db.execute("UPDATE feeds SET synced=? WHERE cid=?", (now_iso(), cid))
