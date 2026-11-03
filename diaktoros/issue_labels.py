"""Triage labels by kind (#967): a priority label (P0, P1, …) or a type label (bug, …)."""
from __future__ import annotations

import re

# A priority label (P0, P1, …); any other triage label is a type (bug, enhancement, …).
PRIORITY_LABEL = re.compile(r"P\d+\Z", re.IGNORECASE)


def type_labels(names) -> list[str]:
    """The labels in ``names`` that are a type rather than a priority (#967)."""
    return [x for x in names if not PRIORITY_LABEL.match(x)]


def max_labels(triage: dict | None) -> int:
    """The cap on labels per filed issue: ``triage.max_labels``, 3 when unset. The one place
    the broker's check and the reviewer's prompt both read it."""
    return int((triage or {}).get("max_labels") or 3)


def label_rule(allowed, most: int) -> str:
    """What a reviewer should send for this list and cap (#1403), or ``""`` for no list. Only
    the kinds the list really has are asked for, and a priority beside the type only when the
    cap leaves room for it."""
    allowed = list(allowed)
    types = type_labels(allowed)
    if not allowed:
        return ""
    if not types:
        return "send a priority label"
    if len(types) == len(allowed):
        return "send a type label"
    if most < 2:
        return "send the type label alone: a cap of 1 leaves no room for a priority"
    return "send a type label and a priority label"


def missing_type(allowed, labels, most: int = 3) -> str:
    """Why a filed issue's ``labels`` lack a type label, or ``""``. A maintainer selecting by
    type must find it (#967): one of the list's non-priority labels is required. A list with no
    such label cannot meet this, so it is not asked. The advice follows the list and the cap
    (#1403): with a cap of 1 the type label goes alone, it is not asked beside a priority."""
    types = type_labels(allowed)
    if types and not type_labels(labels):
        return (f"a filed issue needs a type label (one of {', '.join(types)}); "
                f"{label_rule(allowed, most)}; nothing was filed")
    return ""
