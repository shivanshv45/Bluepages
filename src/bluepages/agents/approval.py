"""Whether a procurement clears on its own, or waits on a person.

Default is off: a department's auto_approve column is 0 unless someone has
set it, so a fresh production auto-approves nothing. Setting a limit is
itself an act of approval, the way a coordinator's petty-cash ceiling is
agreed once rather than re-approved per purchase.

Bluepages never places an order either way. Auto-approval only changes who
signs off on the decision row; Finance still completes the payment on its
own schedule, per agents/consumers.py.
"""

from __future__ import annotations


def should_auto_approve(
    total: float,
    limit: float,
    remaining: float,
    grounded: bool,
) -> bool:
    """True if a procurement this size clears without a person.

    All four conditions hold together:
    - the department has a limit set (limit > 0)
    - the total is at or under that limit
    - the total is at or under what remains of the allocation, so a small
      limit cannot drain a department that is already spent
    - the price is grounded: an unsourced or guessed price never auto-clears
    """
    if limit <= 0:
        return False
    if total <= 0:
        return False
    if not grounded:
        return False
    if total > limit:
        return False
    if total > remaining:
        return False
    return True
