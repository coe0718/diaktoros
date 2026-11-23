"""List items and fenced code in a review's Requirements section, read as Markdown renders them
(reviews on #2573). ``issue_facts`` reads the section's items from what ``unfenced`` leaves.

A list line (``- x``, ``* x``, ``1. x``): its indent, its marker and the spaces after it say where
the item's content starts (``item_col``). A requirement item is a top-level one: a line indented
into an open item is a nested note (evidence), a line with no marker is prose, and a marker line
four or more columns past its container's content column is indented code (``code_line``).

A fence (three or more backticks or tildes) opens fenced code only up to three columns past the
content column of the list item it sits in (0 outside a list): more is indented code, or text
(eighth review). The block runs to a closing fence of its character at least as long, at
most three columns past that same column; or to a line that dedents out of its list item, which
ends the item and the block with it; or to the end. An HTML comment (``<!--`` opening a line, at
most three columns past that same column) is blanked the same way, through the line holding
``-->``, or to a line that dedents out of its list item, as a fence does (tenth and eleventh
reviews). A comment inside a line is hidden too: ``visible``
drops it from the line, and one it leaves open hides the lines after it up to its ``-->``, or to
the next blank line, item, fence or comment line (twelfth review: ``- x <!-- met -->`` read as
marked ``met``). One that never closes hides its words: they are never counted.
A ``<!--`` in a code span is text (thirteenth review). Inline comments and code spans are read
over a whole paragraph, not line by line: a code span may run across lines (fourteenth), and an
escaped backtick opens none (fifteenth). A marker line that cannot interrupt a paragraph is
the paragraph's text, not an item (``interrupts``: ``2. text`` under an item's text,
second F14): one that dedents past the item it continues is indented back into it. Nor is a
line that only looks like a fence or comment opener (sixteenth review) a break in the paragraph.
Every other HTML block (``html_end``, as CommonMark lists them) is raw HTML, not Markdown, so
no line in it is an item or heading (seventeenth review: a command beginning ``- `` in ``<pre>``
evidence read as an unmarked requirement). A ``<pre>``, ``<script>``, ``<style>`` or
``<textarea>`` block, and a ``<?``, ``<!X`` or CDATA one, is code or hidden: blanked as a comment
is, through its closing tag. Any other (``<div>``, ``<ul>``, a lone tag), up to a blank line, is
shown text: its displayed words are kept, so a status in them counts (eighteenth review), and a
top-level ``<li>`` is a requirement item (``shown``: nineteenth and twentieth). A lone tag
cannot interrupt a paragraph, so there it is the paragraph's text.
"""
from __future__ import annotations

import re

ITEM = re.compile(r"^([ \t]*)([-*+]|\d{1,9}[.)])(?:([ \t]+)\S|[ \t]*$)")
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_ATTR = r"""(?:\s+[A-Za-z_:][\w.:-]*(?:\s*=\s*(?:[^\s"'=<>`]+|'[^']*'|"[^"]*"))?)"""
_BLOCK_TAGS = ("address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|"
               "dd|details|dialog|dir|div|dl|dt|fieldset|figcaption|figure|footer|form|frame|"
               "frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|menuitem|"
               "nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|"
               "tfoot|th|thead|title|tr|track|ul")
# CommonMark's HTML block kinds: what opens one, and what ends it (None: the next blank line).
HTML = [(re.compile(start, re.I), end and re.compile(end, re.I)) for start, end in (
    (r"<(?:script|pre|style|textarea)(?:\s|>|$)", r"</(?:script|pre|style|textarea)>"),
    (r"<!--", r"-->"), (r"<\?", r"\?>"), (r"<![A-Za-z]", r">"), (r"<!\[CDATA\[", r"\]\]>"),
    (rf"</?(?:{_BLOCK_TAGS})(?:\s|/?>|$)", None),
    (rf"(?:<[A-Za-z][A-Za-z0-9-]*{_ATTR}*\s*/?>|</[A-Za-z][A-Za-z0-9-]*\s*>)\s*$", None))]
# An inline token: a backslash escape (ASCII punctuation), a backtick run or a comment opener.
INLINE = re.compile(r"\\[!-/:-@\[-`{-~]|`+|<!--")
# In shown HTML: a comment's start or end, or a tag (its attributes are not shown text).
TAG = re.compile(rf"<!--|-->|<[A-Za-z][A-Za-z0-9-]*{_ATTR}*\s*/?>|</[A-Za-z][A-Za-z0-9-]*\s*>")
LIST = re.compile(r"</?[uo]l\b", re.I)                 # an HTML list's tag


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


def kind(found) -> str:
    """A list marker's kind: its bullet, or an ordered marker's ``.`` or ``)``."""
    return found.group(2) if found.group(2) in "-*+" else found.group(2)[-1]


def interrupts(found, at: int, cols: list[int], kinds: list[str]) -> bool:
    """Whether a list-marker line ends the paragraph before it, as Markdown reads it: an item of
    a list already open does, and so does the first item of a new list, unless it is empty or
    numbered other than 1, which is the paragraph's text (second review of F14 on #2573)."""
    deeper = [k for c, k in zip(cols, kinds) if at < c]      # the outermost: the list it is in
    if deeper and deeper[0] == kind(found):
        return True
    return found.group(3) is not None and (kind(found) in "-*+" or int(found.group(2)[:-1]) == 1)


def html_end(text: str, para: list) -> tuple | None:
    """Whether ``text`` (a line, its indent stripped) opens an HTML block, and if so ``(end,)``:
    what ends it, or None for the next blank line. A lone tag cannot interrupt a paragraph."""
    for kind_at, (start, end) in enumerate(HTML):
        if start.match(text) and not (para and kind_at == len(HTML) - 1):
            return (end,)
    return None


def literal(line: str) -> str:
    """A shown line of a raw HTML block, never read as an item or heading: a leading list marker,
    ``#`` or ``*`` escaped (eighteenth review on #2573), the rest as it is."""
    body = line.lstrip()
    if ITEM.match(body) or body[:1] in "#*":
        return line[:len(line) - len(body)] + "\\" + body
    return line


def shown(line: str, top: bool, state: list) -> list[str]:
    """The lines a shown HTML block's ``line`` reads as: its displayed text, without tags (an
    attribute is not shown: twentieth review, F21) or comments (nor is a tag inside one: F20).
    When the block is in no Markdown item (``top``), each ``<li>`` no deeper than one HTML list
    starts a top-level item line (``- `` and its text, or the next text when it has none); the
    rest is ``literal``. ``state`` is the block's ``[HTML list depth, an item still waiting for
    its text, inside a comment]`` (nineteenth review)."""
    segs, text, waiting, at = [], [], state[1], 0
    for tag in TAG.finditer(line):
        word = tag.group()
        if state[2] or word == "-->":                   # in a comment; or plain text
            state[2], at = (False, tag.end()) if state[2] and word == "-->" else (state[2], at)
            continue
        text.append(line[at:tag.start()] + " ")      # a tag may end a word: keep it apart
        at, state[2] = tag.end(), word == "<!--"
        if LIST.match(word):
            state[0] = max(0, state[0] + (-1 if word[1] == "/" else 1))
        elif top and state[0] <= 1 and word[:3].lower() == "<li" and not word[3:4].isalnum():
            segs.append((waiting, "".join(text)))
            text, waiting = [], True
    text.append("" if state[2] else line[at:])
    out = []
    for opens, part in segs + [(waiting, "".join(text))]:
        if part.strip():
            out.append("- " + part.strip() if opens else literal(part))
        state[1] = opens and not part.strip()
    return out


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
    out, fence, cols, kinds, blank, html, para, raw = [], None, [], [], False, None, [], None
    state = [0, False, False]             # a shown HTML block's, for ``shown``

    def flush():                          # the paragraph so far, read inline as one text
        out[len(out) - len(para):] = visible("\n".join(para)).split("\n") if para else []
        para.clear()
    for line in lines:
        at, ended = indent(line), False
        if raw is not None and line.strip() and at < raw:
            raw, ended = None, True                # its list item ended, and the block with it
        elif raw is not None and line.strip():     # shown HTML: text, or a <li> item (F18-19)
            shown_ = shown(line, raw == 0, state)
            out += shown_
            para += shown_
            continue
        raw = None                                 # a blank line ends it, and is read as one
        if html is not None and line.strip() and at < html[0]:
            html, ended = None, True               # its list item ended, and the block with it
        elif html is not None:                     # inside an HTML block, through its end
            html = None if html[1].search(line) else html
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
        item, found, opener = ITEM.match(line), FENCE.match(line), html_end(line.lstrip(), para)
        item = None if code_line(item, at, cols) else item
        # A fence or comment line too deep for its item, or a backtick fence with a backtick
        # after it, is text: it ends no paragraph and closes no item (sixteenth review).
        deep = at - max((c for c in cols if c <= at), default=0) > 3
        found = None if deep or (found and found.group(1)[0] == "`" and "`" in found.group(2)) \
            else found
        opener = None if deep else opener
        if item and para and not interrupts(item, at, cols, kinds):   # the paragraph's text
            item, line = None, " " * max(at, cols[-1] if cols else 0) + line.lstrip()
        if item or found or opener or blank or ended:   # neither is a lazy continuation
            while cols and at < cols[-1]:
                cols.pop()
                kinds.pop()
        if item or found or opener or ended:   # a new block: its own paragraph
            flush()
        blank, col = False, cols[-1] if cols else 0
        if opener and opener[0] is None:           # shown HTML, to the next blank line
            raw, state = col, [0, False, False]
            shown_ = shown(line, raw == 0, state)
            out += shown_
            para += shown_
            continue
        elif opener:                               # code or hidden HTML, through its end
            html = None if opener[0].search(line) else (col, opener[0])
            out.append("")
            continue
        if found:
            fence = (found.group(1)[0], len(found.group(1)), col)
            out.append("")
            continue
        if item:
            cols.append(item_col(item))
            kinds.append(kind(item))
        out.append(line)
        para.append(line)
    flush()
    return out
