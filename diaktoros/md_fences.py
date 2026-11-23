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
ends the item and the block with it; or to the end. An HTML comment (``<!--`` opening a line, at
most three columns past that same column) is blanked the same way, through the line holding
``-->``, or to a line that dedents out of its list item, as a fence does (tenth and eleventh
reviews: a commented-out draft item read as an unmarked requirement; a comment left open in an
item blanked the top-level items after it). A comment inside a line is hidden too: ``visible``
drops it from the line, and one it leaves open hides the lines after it up to its ``-->``, or to
the next blank line, item, fence or comment line (twelfth review: ``- x <!-- met -->`` read as
marked ``met``). One that never closes shows as text, so its words are dropped, never counted.
A ``<!--`` inside a code span is text, not a comment (thirteenth review). Inline comments and
code spans are read over a whole paragraph, as Markdown reads them, not line by line: a code span
may run across lines (fourteenth review), and a backslash escapes the character after it, so an
escaped backtick opens no code span (fifteenth review).
"""
from __future__ import annotations

import re

ITEM = re.compile(r"^([ \t]*)([-*+]|\d{1,9}[.)])(?:([ \t]+)\S|[ \t]*$)")
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
COMMENT = re.compile(r"^[ \t]*<!--(.*)$")
# An inline token: a backslash escape (ASCII punctuation), a backtick run or a comment opener.
INLINE = re.compile(r"\\[!-/:-@\[-`{-~]|`+|<!--")


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


def visible(text: str) -> str:
    """``text``, one paragraph's lines joined by newlines, without its inline HTML comments
    (twelfth review on #2573); each line break they held is kept, so the lines still line up.
    A code span is text, so a ``<!--`` inside one opens nothing (thirteenth review), and it may
    run across lines (fourteenth); an unmatched backtick run is text. A backslash escapes the
    character after it: an escaped backtick opens no span, an escaped ``<`` no comment (fifteenth).
    A comment that never closes hides the rest of the paragraph."""
    out, at = [], 0
    while (token := INLINE.search(text, at)) is not None:
        start, stop = token.span()
        if token.group() == "<!--":                     # ``<!-->`` closes at once
            end = text.find("-->", start + 2)
            out.append(text[at:start])
            at = len(text) if end < 0 else end + 3
            out.append("\n" * text.count("\n", start, at))
            continue
        if token.group()[0] == "`":                     # a code span: kept as it is
            close = re.compile(rf"(?<!`){token.group()}(?!`)").search(text, stop)
            stop = close.end() if close else stop
        out.append(text[at:stop])
        at = stop
    return "".join(out) + text[at:]


def unfenced(lines: list[str]) -> list[str]:
    """``lines`` with each fenced code block (its fences too) and HTML comment blanked: code or
    hidden text, not items, statuses or headings (seventh review on #2573: a ``diff`` given as
    evidence read as unmarked items). The lines left are read for inline comments a paragraph
    at a time (``visible``): one runs to a blank line, item, fence, comment line or dedent."""
    out, fence, cols, blank, comment, para = [], None, [], False, None, []

    def flush():                          # the paragraph so far, read inline as one text
        out[len(out) - len(para):] = visible("\n".join(para)).split("\n") if para else []
        para.clear()
    for line in lines:
        at, ended = indent(line), False
        if comment is not None and line.strip() and at < comment:
            comment, ended = None, True            # its list item ended, and the comment with it
        elif comment is not None:                  # inside an HTML comment, through its "-->"
            comment = None if "-->" in line else comment
            out.append("")
            continue
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
            flush()
            out.append(line)
            blank = True
            continue
        item, found, opener = ITEM.match(line), FENCE.match(line), COMMENT.match(line)
        item = None if code_line(item, at, cols) else item
        if item or found or opener or blank or ended:   # neither is a lazy continuation
            while cols and at < cols[-1]:
                cols.pop()
        if item or found or opener or ended:   # a new block: its own paragraph
            flush()
        blank, col = False, cols[-1] if cols else 0
        if opener and at - col <= 3:
            comment = None if "-->" in opener.group(1) else col
            out.append("")
            continue
        if found and at - col <= 3 and not (found.group(1)[0] == "`" and "`" in found.group(2)):
            fence = (found.group(1)[0], len(found.group(1)), col)
            out.append("")
            continue
        if item:
            cols.append(item_col(item))
        out.append(line)
        para.append(line)
    flush()
    return out
