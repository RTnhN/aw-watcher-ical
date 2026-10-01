import os
import subprocess
from pathlib import Path

import pytest
import tomlkit

from aw_watcher_ical.config import DEFAULT_CONFIG, parse_config

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"


def test_real_toml_types_and_webcal():
    section = tomlkit.parse(DEFAULT_CONFIG)["aw-watcher-ical"]
    section["calendars"] = [{"id": "test", "url": "webcal://example.com/private"}]
    config = parse_config(section)
    assert config.calendars[0].url == "https://example.com/private"
    assert config.past_days == 90


@pytest.mark.parametrize(
    "changes",
    [
        {"poll_time": 0},
        {"poll_time": True},
        {"poll_time": float("nan")},
        {"past_days": -1},
        {"past_days": True},
        {"past_days": 0, "future_days": 0},
        {"timezone": "Invalid/Zone"},
        {"calendars": [{"id": "bad/id", "url": "https://example.com"}]},
        {"calendars": [{"id": "a", "url": "http://example.com/secret"}]},
        {"calendars": [{"id": "a", "url": "https://user:secret@example.com"}]},
        {"calendars": [{"id": "a", "url": "https://example.com"}] * 2},
    ],
)
def test_invalid_config(changes):
    with pytest.raises((ValueError, TypeError)):
        parse_config(changes)


def cli(tmp_path, *args):
    env = dict(
        os.environ,
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
    )
    env.pop("AW_PROFILE", None)
    return subprocess.run(
        [str(PYTHON), "-m", "aw_watcher_ical", *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )


def test_cli_help_and_first_run(tmp_path):
    assert cli(tmp_path, "--help").returncode == 0
    first = cli(tmp_path, "--once")
    assert first.returncode == 0, first.stderr
    assert "No calendars configured" in first.stderr
    paths = list((tmp_path / "config").rglob("aw-watcher-ical.toml"))
    assert len(paths) == 1
    assert "poll_time" in paths[0].read_text()


def test_cli_bad_toml_redacted(tmp_path):
    cli(tmp_path, "--once")
    path = next((tmp_path / "config").rglob("aw-watcher-ical.toml"))
    path.write_text('[aw-watcher-ical]\nurl = "secret-token\n')
    result = cli(tmp_path, "--once", "--verbose")
    assert result.returncode == 2
    assert "secret-token" not in result.stderr


def test_testing_does_not_read_normal_calendars(tmp_path):
    cli(tmp_path, "--once")
    path = next((tmp_path / "config").rglob("aw-watcher-ical.toml"))
    path.write_text(
        '[[aw-watcher-ical.calendars]]\nid="normal"\nurl="https://example.com/private"\n'
    )
    result = cli(tmp_path, "--testing", "--once")
    assert result.returncode == 0
    assert "No calendars configured" in result.stderr
