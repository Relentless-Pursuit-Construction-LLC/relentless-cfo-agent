"""AR Aging Digest — Keystone's first feature.

Runs Mon-Sat 6:30 AM MT. Pulls AR aging detail from QBO, joins customers to
assigned reps via the shared rep registry, generates:
  - Per-rep DM (their stuck customers, aged buckets, total at risk)
  - Matt summary DM (full picture, concentration flags, week-over-week delta)

STATUS: stub — to be implemented after sanity-pull validates QBO access.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def run_ar_aging_digest(as_of: date | None = None) -> dict[str, Any]:
    """Main entrypoint called by cron.

    Returns a summary dict for logging / observability.
    """
    raise NotImplementedError("AR aging digest — implement after sanity-pull")
