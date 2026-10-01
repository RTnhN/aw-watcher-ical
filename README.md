# aw-watcher-ical

Periodically synchronize private iCalendar URLs into ActivityWatch. Entries describe
**scheduled activity**, not confirmed attendance. Each calendar gets a dedicated
`calendar_data` bucket, separate from live window/AFK activity and file imports.

## Setup

Requires Python 3.14 and a running ActivityWatch server. From this cloned
repository, install the watcher as a command-line tool so ActivityWatch can
discover its executable:

```sh
uv tool install .
uv tool update-shell
```

Open a new terminal after updating the shell, then verify the command and generate
the config:

```sh
aw-watcher-ical --help
aw-watcher-ical --once
```

`uv tool install .` creates a persistent tool environment and exposes the command
in uv's executable directory. `uv tool update-shell` adds that directory to your
shell's `PATH`; `uv tool dir --bin` prints its location. See the
[uv tools documentation](https://docs.astral.sh/uv/concepts/tools/).

The first run creates the normal ActivityWatch configuration file and prints its
location. On Linux this is normally:
`~/.config/activitywatch/aw-watcher-ical/aw-watcher-ical.toml`.
ActivityWatch handles the platform-specific directory and `XDG_CONFIG_HOME`.

Edit the file:

See [aw-watcher-ical.example.toml](aw-watcher-ical.example.toml) for commented-out
personal, work, and testing calendar examples. New generated configs also include
these examples; remove all leading comment markers to enable a block.

```toml
[aw-watcher-ical]
poll_time = 900          # seconds between successful polls
past_days = 90          # days
future_days = 30        # days
# timezone = "America/Chicago" # fallback for floating times and all-day dates

[[aw-watcher-ical.calendars]]
id = "personal"
url = "https://example.com/private-calendar.ics"

[[aw-watcher-ical.calendars]]
id = "work"
url = "webcal://example.com/work-calendar.ics"
```

Remove any uncommented `calendars = []` when adding calendar tables. Calendar IDs
must be unique and contain letters, numbers, underscores or hyphens. Keep IDs stable
when replacing URLs. `webcal://` is converted to verified HTTPS. URLs are stored
in the normal config file with normal permissions; they are never included in logs.

Start the watcher and leave it running:

```sh
aw-watcher-ical
```

Restart after config changes. `--host` and `--port` override the ActivityWatch server;
`--verbose` enables additional logging. `--once` attempts one fetch/sync per calendar
and exits: 0 for success (or no configured calendars), 1 for a transient failure,
2 for configuration/startup errors. Ctrl+C or SIGTERM stops cleanly.

## Synchronization

The default rolling window covers calendar dates from 90 days ago through 30 days
from today, inclusive. Events overlapping that range are imported at their full
duration. Older imported history is retained. Existing entries within the window
are updated when their titles, times or durations change. Recurring instances use
their original occurrence identities, including moved instances and “this and
future” edits. Polling an unchanged calendar does not create duplicates.

Explicit cancellations and recurrence exclusions remove matching imported entries
within the sync window. Unexplained disappearances are preserved because providers
may truncate their feeds. This also means shortening a recurrence rule or deleting
an event without an explicit cancellation may leave previously imported entries.
Changing a whole series' start time can change its occurrence identities; old
instances without cancellation evidence are preserved. Removing a calendar from
the config stops syncing it and retains its bucket.

Time zones come from event definitions, then `X-WR-TIMEZONE`, then the configured
fallback or system local zone. UTC timestamps are stored in ActivityWatch. All-day
entries are included with `all_day = true`; their exclusive end dates and DST are
respected. All-day durations can therefore be 23 or 25 hours around DST changes.
Descriptions, locations, attendees and meeting links are omitted. Stored fields
are `calendar_id`, `calendar_name`, `uid`, `recurrence_id`, `title`, and `all_day`.

## Offline behavior and limits

Accepted feed snapshots and desired entries are kept in SQLite under the standard
ActivityWatch data directory, normally
`~/.local/share/activitywatch/aw-watcher-ical/default/sync.sqlite3` on Linux.
Snapshots contain private calendar data; treat them like other ActivityWatch data.

Network failures, invalid calendars and HTTP errors preserve accepted data. Each
calendar retries independently with jittered exponential backoff beginning around
30 seconds, capped at the greater of the poll interval or five minutes. Longer
`Retry-After` values are honored. `ETag` and `Last-Modified` reduce unnecessary
transfers; cached feeds are re-expanded as the date window advances.

ActivityWatch outages retain pending desired state. Recovery compares actual bucket
records before writing, including after restarts and lost responses. Duplicate
watcher instances using the same state are blocked. The database retains last
successful fetch/sync timestamps for troubleshooting. Buckets are named
`aw-watcher-ical_<hostname>_<calendar-id>`.

Each download has a hard 30-second deadline, a 10 MiB decoded size limit, and at most
five redirects, all over HTTPS. Calendar processing has a separate 30-second
limit and a maximum of 50,000 expanded occurrences per window. A malformed required
event field or recurrence rejects that snapshot rather than partially importing it.
Calendar attachments and links are never fetched.

## Testing and autostart

`--testing` uses `[aw-watcher-ical-testing]` with its own `.calendars` tables,
isolated SQLite state under `testing/`, the ActivityWatch testing server, and
`aw-watcher-ical-testing_<hostname>_<calendar-id>` buckets. It does not use the normal
calendar list.

```sh
uv sync
uv run pytest
uv run ruff check src tests
```

ActivityWatch's launcher discovers installed `aw-*` commands on its `PATH`, as
shown in its [module discovery code](https://github.com/ActivityWatch/aw-qt/blob/master/aw_qt/manager.py).
After installing the tool, add `aw-watcher-ical` to the existing `autostart_modules`
list in `aw-qt.toml`, normally `~/.config/activitywatch/aw-qt/aw-qt.toml` on Linux.
Keep your existing server and watcher entries. For example, if your list currently
contains the standard server, AFK, and window watchers:

```toml
[aw-qt]
autostart_modules = ["aw-server-rust", "aw-watcher-afk", "aw-watcher-window", "aw-watcher-ical"]
```

Restart ActivityWatch after updating the config and `PATH`. Its launch environment
must include the directory printed by `uv tool dir --bin`. A desktop launcher may
have a different `PATH` from your terminal; if the watcher is missing from the tray
menu, add that directory to the desktop/login environment or start `aw-qt` from a
terminal where `aw-watcher-ical --help` works.

After changing this checkout, update the installed tool with
`uv tool install --reinstall .` and restart the watcher. For development without
tool installation, use `uv sync` and `uv run aw-watcher-ical`.
Alternatively, run the installed executable through your operating system's
login/startup service. No launcher settings are changed automatically.

This is read-only, one-way import. It does not change the source calendar, migrate
existing file-importer buckets, or add custom web UI panels.
