"""Compatibility interface for the retired GoHighLevel integration.

GoHighLevel was cancelled. Retained credentials must never reactivate lookups.
Sightline sales reporting needs its own verified adapter; QBO is unchanged.
"""

from __future__ import annotations

from typing import Any


def is_configured() -> bool:
    """Retired regardless of any stale Railway credentials."""
    return False


def search_all_opportunities() -> list[dict[str, Any]]:
    """Never return empty sales as if a cancelled provider had zero sales."""
    raise RuntimeError("GoHighLevel is retired; use a verified Sightline sales source.")


def monetary_value(opp: dict[str, Any]) -> float:
    """Parse an opportunity's monetaryValue defensively."""
    v = opp.get("monetaryValue")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace(",", "").replace("$", ""))
        except ValueError:
            return 0.0
    return 0.0
