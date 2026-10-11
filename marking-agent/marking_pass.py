"""A separate pass for the one gap that otherwise ends the run with no answer.

The short gap rounds cap each site at 90s. A specification with no revision marked current does not fit in
that, and leaving it open used to return before submit. This pass runs until the gap cutoff, and if the
marking is still missing the known reference is entered as no_bid.
"""
from __future__ import annotations

import pricing
from resolver import Gap

MARKING_PHRASE = "must be marked current"


def is_marking_gap(gap: Gap) -> bool:
    return gap.site == "customer-portal" and MARKING_PHRASE in gap.question


def marking_instruction() -> str:
    return (
        "Follow-up only. Open every specification revision, including ones already listed. "
        "On each revision, record_page with its id and marking set to exactly one of "
        "current, superseded, unknown. Use current only when that revision itself is marked "
        "current. Do not infer it from the date. Replace earlier records that have the same id. "
        "Do not finish while a revision you opened still has no marking."
    )


def marking_turns(seconds_left: float) -> int:
    """Enough turns to use the leftover gap time; one slow call must not end the pass."""
    return max(28, min(80, int(seconds_left / 12)))


def answer_or_no_bid(answer: dict | None, gaps: list[Gap], reference: str) -> dict | None:
    """The priced answer, or a no_bid when the only reason to stop is a missing current revision."""
    if answer is not None:
        return answer
    if reference and reference != "UNKNOWN" and any(is_marking_gap(gap) for gap in gaps):
        return pricing.no_bid(reference)
    return None
