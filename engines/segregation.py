"""Reviewer segregation.

Whoever worked a case at L1 does not see it in their L2 or EDD queue and
cannot decide it there; whoever worked it at L2 cannot decide it at EDD. The
check reads `reviewers_by_level`, which is append-only, so a case returned to
L1 and picked up by a second analyst still remembers the first.

Enforced twice on purpose: the queue hides the row, and the POST refuses the
action — a hidden row is a convenience, a refused action is the control.
"""
from fincrime.engines.errors import WorkflowError

_EARLIER = {"L1": (), "L2": ("L1",), "EDD": ("L1", "L2")}


def hidden_from(case, email, level):
    """True when `email` already touched this case at a level below `level`."""
    email = (email or "").lower()
    if not email:
        return False
    seen = case.get("reviewers_by_level") or {}
    for earlier in _EARLIER.get(level, ()):
        if email in [e.lower() for e in seen.get(earlier, [])]:
            return True
    return False


def assert_may_decide(case, email, level):
    if hidden_from(case, email, level):
        raise WorkflowError(f"You reviewed this case at an earlier level, so you cannot decide it at {level}. "
                            "Another analyst must take it.")
