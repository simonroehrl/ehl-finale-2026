"""A longer follow-up when no revision is marked current.

The short gap rounds stay on priced fields. A specification with no governing revision
does not fit in 90s; this pass continues the same spec session. Missing evidence is
not a no_bid — only the resolver may decide no_bid (no matching material).
"""
from __future__ import annotations

from resolver import Gap

MARKING_PHRASE = "must be marked current"


def is_marking_gap(gap: Gap) -> bool:
    return gap.site == "customer-portal" and MARKING_PHRASE in gap.question


def marking_instruction() -> str:
    return (
        "Same browser session. You are already on this website. Do not start over. "
        "Log in again only if the session expired.\n"
        "Follow-up only. Open every specification revision, including ones already listed. "
        "On each revision, save_facts and copy the marking as the page writes it. "
        "Do not infer current from the date. Replace earlier records that have the same id. "
        "Do not finish while a revision you opened still has no marking."
    )


def marking_turns(seconds_left: float) -> int:
    """Enough turns to use the leftover gap time; one slow call must not end the pass."""
    return max(28, min(80, int(seconds_left / 12)))


def portal_answer(answer: dict | None, _gaps: list[Gap] | None = None,
                  _reference: str | None = None) -> dict | None:
    """Submit only what the resolver priced. Incomplete evidence is not converted to no_bid."""
    return answer
