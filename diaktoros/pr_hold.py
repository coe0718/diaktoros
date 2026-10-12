"""Hold one PR: a maintainer's label or comment pauses the loop on it (#477).

``diaktoros:hold`` applied by a maintainer, or a maintainer's ``/loop hold`` comment, holds the
PR; ``/loop release`` or removing the label lifts it. The latest valid event wins. Only
``triage.maintainers`` count: anyone else's label or comment is ignored and logged. A loop with
no ``triage.maintainers`` (they need ``triage.fix_label``) has nobody who can hold, so the hold
can never fire there.

While held, no turn is claimed (``Supervisor._claim``): queued turns stay pending and start
after release (the watchdog sweep re-arms the worker). A turn already running is not
interrupted. The watchdog sweep reports no stall and queues no conflict turn for a held PR, and
the queue drain leaves its entry queued.
"""

from __future__ import annotations

from . import config, gh
from .util import epoch, iso_at, log

LABEL = "diaktoros:hold"
HOLD, RELEASE = "/loop hold", "/loop release"


def _login(entry, key: str) -> str:
    user = entry.get(key) if isinstance(entry, dict) else None
    return str(user.get("login") or "").lower() if isinstance(user, dict) else ""


def decide(loop: dict, number: int, events: list, comments: list) -> dict | None:
    """The hold in force (``{"by", "at", "how"}``), or ``None``. Pure apart from one log line
    per ignored non-maintainer."""
    allowed = config.maintainers(loop)
    timeline = []
    for event in events:
        if not isinstance(event, dict) or event.get("event") not in ("labeled", "unlabeled"):
            continue
        name = (event.get("label") or {}).get("name")
        if str(name or "").casefold() == LABEL:
            timeline.append((event.get("created_at"),
                             "label" if event["event"] == "labeled" else "unlabel",
                             _login(event, "actor")))
    for comment in comments:
        body = str(comment.get("body") or "").strip().lower() if isinstance(comment, dict) else ""
        if body in (HOLD, RELEASE):
            timeline.append((comment.get("created_at"), "hold" if body == HOLD else "release",
                             _login(comment, "user")))
    held = None
    for at, kind, who in sorted(timeline, key=lambda item: epoch(item[0])):
        if who not in allowed:
            if kind != "unlabel":      # anyone may remove their own mistake; it lifts nothing
                log(f"#{number}: ignored {kind} by {who or '?'} — not in triage.maintainers")
            continue
        if kind == "release":
            held = None
        elif kind == "unlabel":
            held = None if held and held["how"] == "label" else held
        else:
            held = {"by": who, "at": str(at or ""),
                    "how": "label" if kind == "label" else "comment"}
    return held


def read(loop: dict, number: int) -> tuple[dict | None, str]:
    """``(hold, error)``. No maintainers means no read: nobody could hold. A failed read is an
    error, never "not held"."""
    if not config.maintainers(loop):
        return None, ""
    events, error = gh.issue_events_read(loop, number)
    if events is None:
        return None, error
    comments, error = gh.issue_comments_read(loop, number)
    if comments is None:
        return None, error
    return decide(loop, number, events, comments), ""


def line(hold: dict) -> str:
    """The one sentence explain, status and the watchdog all use."""
    how = "the diaktoros:hold label" if hold.get("how") == "label" else "/loop hold"
    when = iso_at(epoch(hold.get("at"))) or "an unknown time"
    return (f"held by {hold.get('by') or '?'} via {how} since {when}: no review, fix, CI-fix or "
            f"conflict turn starts until `/loop release` or the label is removed")
