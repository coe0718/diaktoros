"""Triage labels by kind (#967): a priority label (P0, P1, …) or a type label (bug, …)."""
from __future__ import annotations

import re

# A priority label (P0, P1, …); any other triage label is a type (bug, enhancement, …).
PRIORITY_LABEL = re.compile(r"P\d+\Z", re.IGNORECASE)


def type_labels(names) -> list[str]:
    """The labels in ``names`` that are a type rather than a priority (#967)."""
    return [x for x in names if not PRIORITY_LABEL.match(x)]


def missing_type(allowed, labels) -> str:
    """Why a filed issue's ``labels`` lack a type label, or ``""``. A maintainer selecting by
    type must find it (#967): one of the list's non-priority labels is required. A list with no
    such label cannot meet this, so it is not asked."""
    types = type_labels(allowed)
    if types and not type_labels(labels):
        return (f"a filed issue needs one type label as well as a priority "
                f"(one of {', '.join(types)}); nothing was filed")
    return ""
