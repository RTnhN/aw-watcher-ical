import argparse
import logging
import signal
from pathlib import Path

from aw_core import dirs
from filelock import FileLock, Timeout

from .config import NAME, load_config
from .state import State
from .sync import TimedClient
from .watcher import Watcher


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Synchronize private iCalendar feeds into ActivityWatch."
    )
    parser.add_argument("--host", help="ActivityWatch server host")
    parser.add_argument("--port", type=int, help="ActivityWatch server port")
    parser.add_argument(
        "--testing",
        action="store_true",
        help="Use isolated testing config, data and buckets",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Attempt one sync and exit (1 on transient failure)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    # HTTP library debug messages may contain private URL paths.
    for name in ("urllib3", "requests", "aw_client"):
        logging.getLogger(name).setLevel(logging.ERROR)
    logger = logging.getLogger(NAME)
    try:
        config = load_config(args.testing)
    except Exception:  # noqa: BLE001 - config errors may contain private URLs
        logger.error(
            "Invalid watcher configuration. Check field types, calendar IDs, HTTPS URLs, timezone and polling settings."
        )
        return 2
    if not config.calendars:
        logger.info(
            "No calendars configured. Add [[%s.calendars]] tables with id and url to %s, then restart.",
            NAME + ("-testing" if args.testing else ""),
            Path(dirs.get_config_dir(NAME)) / (NAME + ".toml"),
        )
        return 0
    directory = Path(dirs.get_data_dir(NAME)) / (
        "testing" if args.testing else "default"
    )
    directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(directory / "watcher.lock")
    state = None
    old_term = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise KeyboardInterrupt

    try:
        with lock.acquire(timeout=0):
            state = State(directory / "sync.sqlite3")
            client = TimedClient(
                NAME + ("-testing" if args.testing else ""),
                testing=args.testing,
                host=args.host,
                port=args.port,
            )
            signal.signal(signal.SIGTERM, interrupted)
            return Watcher(config, state, client).run(args.once)
    except Timeout:
        logger.error("Another watcher is using this sync state.")
        return 2
    except KeyboardInterrupt:
        logger.info("Watcher stopped.")
        return 0
    except Exception:  # noqa: BLE001 - sanitize errors at external boundaries
        logger.error(
            "Watcher could not start or persist sync state. Check local config and data directories."
        )
        return 2
    finally:
        signal.signal(signal.SIGTERM, old_term)
        if state is not None:
            state.close()
