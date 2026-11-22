"""List items and fenced code in a review's Requirements section, read as Markdown renders them
(reviews on #2573). ``issue_facts`` reads the section's items from what ``unfenced`` leaves.

A list line (``- x``, ``* x``, ``1. x``): its indent, its marker and the spaces after it say where
the item's content starts (``item_col``). A requirement item is a top-level one: a line indented
into an open item is a nested note (evidence), a line with no marker is prose, and a marker line
four or more columns past its container's content column is indented code (``code_line``).

A fence (three or more backticks or tildes) opens fenced code only up to three columns past the
content column of the list item it sits in (0 outside a list): more is indented code, or text,
not a fence (eighth review: a backtick line indented four columns after prose blanked every
requirement after it). The block runs to a closing fence of its character at least as long, at
most three columns past that same column; or to a line that dedents out of its list item, which
ends the item and the block with it; or to the end.
"""
from __future__ import annotations

import re

ITEM = re.compile(r"^([ \t]*)([-*+]|\d{1,9}[.)])(?:([ \t]+)\S|[ \t]*$)")
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")


def width(text: str) -> int:
    return len(text.expandtabs(4))


def indent(line: str) -> int:
    return width(line[:len(line) - len(line.lstrip())])


def item_col(found) -> int:
    """The column an item's content starts at: past its marker and the spaces after it (a tab in
    the gap reaches the next stop from the marker's real column: fifth review); a gap past four
    columns is one space, the rest indented code."""
    start = width(found.group(1)) + len(found.group(2))
    gap = width(found.group(1) + found.group(2) + (found.group(3) or " ")) - start
    return start + (gap if gap <= 4 else 1)


def code_line(found, at: int, cols: list[int]) -> bool:
    """Whether a list-marker line at column ``at`` is indented code (or text), not an item: four
    or more columns past the content column of the deepest open item it sits in, 0 outside a list
    (ninth review on #2573: ``    - command --help`` after prose read as an unmarked item)."""
    return bool(found) and at - max((c for c in cols if c <= at), default=0) >= 4


def unfenced(lines: list[str]) -> list[str]:
    """``lines`` with each fenced code block (its fences too) blanked: code, not items, statuses
    or headings (seventh review on #2573: a ``diff`` given as evidence read as unmarked items)."""
    out, fence, cols, blank = [], None, [], False
    for line in lines:
        at, ended = indent(line), False
        if fence is not None:
            char, size, col = fence
            if line.strip() and at < col:
                fence, ended = None, True          # its list item ended, and the block with it
            else:
                found = FENCE.match(line)
                if found and at - col <= 3 and found.group(1)[0] == char \
                        and len(found.group(1)) >= size and not found.group(2).strip():
                    fence = None
                out.append("")
                continue
        if not line.strip():
            out.append(line)
            blank = True
            continue
        item, found = ITEM.match(line), FENCE.match(line)
        item = None if code_line(item, at, cols) else item
        if item or found or blank or ended:        # a fence is never a lazy continuation
            while cols and at < cols[-1]:
                cols.pop()
        blank, col = False, cols[-1] if cols else 0
        if found and at - col <= 3 and not (found.group(1)[0] == "`" and "`" in found.group(2)):
            fence = (found.group(1)[0], len(found.group(1)), col)
            out.append("")
            continue
        if item:
            cols.append(item_col(item))
        out.append(line)
    return out
