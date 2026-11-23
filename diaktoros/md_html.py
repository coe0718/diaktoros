"""Shown HTML in a review's Requirements section (reviews F18-F22 on #2573), for ``md_fences``:
a tag's pattern, and the text a line of a shown HTML block (``<div>``, ``<ul>``, a lone tag)
displays, its tags, attributes and comments dropped."""
from __future__ import annotations

import re

_ATTR = r"""(?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:[^\s"'=<>`]+|'[^']*'|"[^"]*"))?)"""
# In shown HTML: a comment's start or end, or a tag (its attributes are not shown text).
TAG = re.compile(rf"<!--|-->|<[A-Za-z][A-Za-z0-9-]*{_ATTR}*\s*/?>|</[A-Za-z][A-Za-z0-9-]*\s*>")
LIST = re.compile(r"</?[uo]l\b", re.I)                 # an HTML list's tag
OPEN = re.compile(rf"<[A-Za-z][A-Za-z0-9-]*{_ATTR}*(?:\s+[\w:.-]+\s*=\s*(?:'[^']*|\"[^\"]*)?)?\s*$")


def shown(line: str, top: bool, state: list, literal) -> list[str]:
    """The lines a shown HTML block's ``line`` reads as: its displayed text, without tags (an
    attribute is not shown: twentieth review, F21) or comments (nor is a tag inside one: F20).
    When the block is in no Markdown item (``top``), each ``<li>`` no deeper than one HTML list
    starts a top-level item line (``- `` and its text, or the next text when it has none); the
    rest goes through ``literal`` (``md_fences.literal``). ``state`` is the block's ``[HTML list
    depth, an item still waiting for its text, inside a comment, a tag left open by the line
    before]`` (nineteenth review; twenty-second: a tag split across lines is read whole, so its
    attributes never part an item from its text, wherever the split falls: after its name, in an
    attribute's value, or between an attribute's ``=`` and its value, round 23; and a tag's quoted
    value is read with its tag, never as tags of its own, round 24)."""
    line, state[3] = state[3] + line, ""
    segs, text, waiting, at, pos, cut = [], [], state[1], 0, 0, None
    while True:              # left to right, so a quoted value is read with its tag (round 24)
        if state[2]:                                  # in a comment: only its end counts
            end = line.find("-->", pos)
            if end < 0:
                break
            state[2], at = False, end + 3
            pos = at
            continue
        if (lt := line.find("<", pos)) < 0:
            break
        if (tag := TAG.match(line, lt)) is None:
            if OPEN.match(line, lt):                   # left open: read whole with the next line
                cut = lt
                break
            pos = lt + 1
            continue
        word = tag.group()
        text.append(line[at:lt] + " ")               # a tag may end a word: keep it apart
        at = pos = tag.end()
        state[2] = word == "<!--"
        if LIST.match(word):
            state[0] = max(0, state[0] + (-1 if word[1] == "/" else 1))
        elif top and state[0] <= 1 and word[:3].lower() == "<li" and not word[3:4].isalnum():
            segs.append((waiting, "".join(text)))
            text, waiting = [], True
    if cut is not None:
        state[3] = line[cut:] + "\n"
    text.append("" if state[2] else line[at:cut])
    out = []
    for opens, part in segs + [(waiting, "".join(text))]:
        if part.strip():
            out.append("- " + part.strip() if opens else literal(part))
        state[1] = opens and not part.strip()
    return out
