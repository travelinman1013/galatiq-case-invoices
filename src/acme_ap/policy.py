"""The policy floor. Pure functions; the model may tighten the floor, never loosen it."""

from __future__ import annotations

from decimal import Decimal

from acme_ap.models import Action, Finding

_RANK: dict[Action, int] = {"approve": 0, "escalate": 1, "reject": 2}
SCRUTINY_THRESHOLD = Decimal("10000")


def floor(findings: list[Finding], total: Decimal | None) -> tuple[Action, list[str]]:
    """Deterministic minimum outcome given the findings."""
    blocks = [f for f in findings if f.severity == "block"]
    if blocks:
        return "reject", [f"{f.code}: {f.message}" for f in blocks]
    warns = [f for f in findings if f.severity == "warn"]
    reasons = [f"{f.code}: {f.message}" for f in warns]
    if total is not None and total > SCRUTINY_THRESHOLD:
        reasons.append(f"OVER_10K: total {total} requires VP scrutiny")
    if reasons:
        return "escalate", reasons
    return "approve", ["All checks passed"]


def max_conservative(*actions: Action) -> Action:
    """approve < escalate < reject. The most conservative of the inputs wins."""
    return max(actions, key=lambda a: _RANK[a])


def is_at_least(action: Action, floor_action: Action) -> bool:
    return _RANK[action] >= _RANK[floor_action]
