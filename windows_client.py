"""Stable, package-external entry point for the frozen Windows client."""

from meeting_notes.client.app import run


if __name__ == "__main__":
    raise SystemExit(run())
