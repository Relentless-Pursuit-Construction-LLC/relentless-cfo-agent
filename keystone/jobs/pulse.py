"""The Pulse — daily cash heartbeat.

Runs 6:30 AM MT after AR digest. Cash position, day-over-day change,
yesterday's revenue vs $12,500 target, one anomaly flag.

STATUS: stub — to be implemented after AR digest ships.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def run_pulse(as_of: date | None = None) -> dict[str, Any]:
    """Main entrypoint called by cron."""
    raise NotImplementedError("Daily Pulse — implement after AR digest ships")
