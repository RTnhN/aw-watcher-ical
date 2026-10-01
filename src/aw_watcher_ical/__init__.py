"""Poll private iCalendar feeds and synchronize scheduled ActivityWatch events."""


def main():
    from .cli import main as run

    return run()
