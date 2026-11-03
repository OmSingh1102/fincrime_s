"""The one exception the engines raise at the route layer.

A route wraps every engine call in _run_action(), which turns this into a
flash and a redirect back to the page the analyst was on. Anything else that
escapes an engine is a bug and is allowed to surface as a 500.
"""


class WorkflowError(Exception):
    """The action is not allowed on this record in this state, or the form was incomplete."""
