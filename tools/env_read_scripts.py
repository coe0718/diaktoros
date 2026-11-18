"""The text side of the env-read scan (``tools/env_reads.py``): reads in a generated script.

Python held in a string and run in a child process is scanned too (#2300). When the script does
not parse, ``_fallback_matches`` searches it by pattern, with the receivers the parse accepts
(not another object's ``getenv``/``environ``, a dot across spaces included, #2420). When an
f-string script does parse, an interpolation inside a comment or a string literal can still end
it and let a read there run (#2376): ``_reads_a_hole_may_expose`` finds those, with
``_inert_spans`` for the comments and literals and ``_tail_may_close`` for a literal a hole can
close.

It sits here, outside ``tests/``, with the scan, so the host's base-failure proof (``change_proof``,
which copies a PR's test paths onto the base) runs a PR's tests against the base's rules and not
the PR's (#1973, #2509): inside the test file, every regression passed on the base too (#2536
review F1). The regression tests stay in ``tests/test_env_reads_via_envnames.py``. Standard
library only.
"""
from __future__ import annotations

import ast
import io
import re
import textwrap
import tokenize

# A read in a script that does not parse: ``os.environ.get(`` (or a bare ``environ.get(``),
# ``os.environ[`` (or ``environ[``) or ``os.getenv(``, then a prefixed literal or an
# ``envnames.name("X")`` key. A subscript followed by ``=`` is a write. The receivers are the
# ones ``_is_environ``/``_read_key`` accept: ``other.getenv(`` or ``self.environ[`` is not a read
# (``_fallback_matches`` drops a match glued to a name or a dot before it, or a dot then spaces).
_FALLBACK = re.compile(r"""(?:(?:os\s*\.\s*)?environ\s*(?:\.get\s*\(|(?P<sub>\[))"""
                       r"""|os\s*\.\s*getenv\s*\()\s*(?:key\s*=\s*)?"""
                       r"""(?:['"](?P<lit>(?:DIAKTOROS_|REVIEW_LOOP_)\w*)['"]"""
                       r"""|envnames\.name\(\s*['"](?P<short>\w*)['"]\s*\))""")
_WRITE = re.compile(r"""\s*\]\s*=(?!=)""")
# What an f-string interpolation becomes when its script is parsed: each is tried, since no one
# placeholder fits every position (``{indent}os...`` wants nothing, ``{name} = ...`` a name).
_PLACEHOLDERS = ("_", "", "0")


def _dot_across_spaces(source: str, offset: int) -> bool:
    """Whether spaces, then a dot, come right before ``offset``: ``self . environ`` is an
    attribute of ``self``, not ``os.environ`` (#2420)."""
    return (source[offset - 1:offset] in (" ", "\t")
            and source[:offset].rstrip(" \t").endswith("."))


def _fallback_matches(source: str, start: int = 0, end=None,
                      free=frozenset()) -> list[tuple[int, str]]:
    """``(offset, variable)`` for each covered read the pattern finds in ``source[start:end]``;
    writes are skipped, and so is a match glued to a name or a dot before it (``other.getenv``,
    ``xos.environ``) unless its offset is in ``free``: right after an interpolation, whose
    placeholder says nothing about what really precedes the read. A dot counts across spaces
    too (#2420: ``self . environ.get`` is an attribute of ``self``, as the parse reads it); a
    name does not (``return environ.get(...)`` is a read)."""
    found = []
    for m in _FALLBACK.finditer(source, start, len(source) if end is None else end):
        if m["sub"] and _WRITE.match(source, m.end()):
            continue
        before = source[m.start() - 1] if m.start() else ""
        if (before.isalnum() or before in "_.") and before and m.start() not in free:
            continue
        if _dot_across_spaces(source, m.start()):
            continue
        found.append((m.start(), m["lit"] or "DIAKTOROS_" + m["short"]))
    return found


def _fallback_reads(source: str) -> list[str]:
    """The covered reads a script that does not parse holds, by pattern; writes are skipped."""
    return [key for _, key in _fallback_matches(source)]


def _joined(parts: list, hole: str) -> tuple[str, list[int]]:
    """An f-string's script with ``hole`` for each interpolation (``_fstring_parts``), and the
    offset of each interpolation in it."""
    text, holes = "", []
    for part in parts:
        if part is None:
            holes.append(len(text))
            text += hole
        else:
            text += part
    return text, holes


def _reads_a_hole_may_expose(parts: list, hole: str, depth: int) -> list[str]:
    """The pattern reads that sit in a comment or a string literal of the f-string script (built
    with ``hole`` for each interpolation) after an interpolation inside that same comment or
    literal: the interpolation can end it (a newline ends a comment, a quote a string), so the
    read may run. A comment or literal with no interpolation before the read stays what it is
    whatever is interpolated, and is not counted. The parse counted none of them as code, so
    each is a distinct read (#2376), except one the nested scan of a string literal already
    counts (``_counted_by_nested_scan``)."""
    text, holes = _joined(parts, hole)
    found = []
    free = frozenset(h + len(hole) for h in holes)
    for begin, end, inner, kind in _inert_spans(text):
        matches = _fallback_matches(text, *inner, free=free)
        counted = (_counted_by_nested_scan(text[begin:end], len(matches), depth)
                   if kind == "string" else [False] * len(matches))
        for (offset, key), seen in zip(matches, counted):
            if seen:
                continue
            if any(begin < h <= offset for h in holes) and (
                    kind == "comment" or _tail_may_close(text, offset, end, holes)):
                found.append(key)
    return found


def _counted_by_nested_scan(token: str, count: int, depth: int) -> list[bool]:
    """For each of the ``count`` pattern reads in the string literal ``token`` of a generated
    script at ``depth``, in order: whether ``direct_reads`` already counts it by scanning the
    literal's value as a nested script (it does below depth 2: every read when the value does
    not parse, those in code when it does). When the reads cannot be paired (an f-string, a
    different number of reads), none is taken as counted: a read may be counted twice, never
    hidden."""
    unseen = [False] * count
    try:
        value = ast.literal_eval(token)
    except (ValueError, SyntaxError):
        return unseen
    if depth >= 2 or not isinstance(value, str):
        return unseen
    value = textwrap.dedent(value)
    matches = _fallback_matches(value)
    if len(matches) != count:
        return unseen
    try:
        ast.parse(value)
    except SyntaxError:
        return [True] * count
    inert = [inner for _, _, inner, _ in _inert_spans(value)]
    return [not any(a <= offset < b for a, b in inert) for offset, _ in matches]


def _tail_may_close(text: str, offset: int, end: int, holes: list[int]) -> bool:
    """Whether a read at ``offset`` inside a string literal that ends at ``end`` (after its
    closing quote) can run once an earlier interpolation has closed the literal. It cannot only
    when the leftover closing quote then opens a string that never ends: a single-quoted
    literal whose rest of line (from the read) holds no other interpolation, no ``#``, no
    second such quote and no line-continuing backslash. Anything else (a triple quote, which can
    pair with a later one; another interpolation; a ``#`` that comments out the leftover quote)
    may run the read, so it counts."""
    quote = text[end - 1]
    if text[end - 3:end] == quote * 3:
        return True
    line_end = text.find("\n", offset)
    line_end = len(text) if line_end < 0 else line_end
    tail = text[offset:line_end]
    return (any(offset < h <= line_end for h in holes) or "#" in tail
            or tail.count(quote) != 1 or tail.rstrip().endswith("\\"))


# Token types whose text the parse never reads as code: a comment, a string literal, and (3.12+)
# the literal text of an f-string or (3.14+) a t-string.
_INERT = {tokenize.COMMENT, tokenize.STRING}
_MIDDLES = {getattr(tokenize, n) for n in ("FSTRING_MIDDLE", "TSTRING_MIDDLE") if hasattr(tokenize, n)}
_OPENS = {getattr(tokenize, n) for n in ("FSTRING_START", "TSTRING_START") if hasattr(tokenize, n)}
_CLOSES = {getattr(tokenize, n) for n in ("FSTRING_END", "TSTRING_END") if hasattr(tokenize, n)}


def _inert_spans(text: str) -> list[tuple[int, int, tuple[int, int], str]]:
    """``(begin, end, (start, stop), kind)`` offsets in ``text`` for each comment (kind
    ``"comment"``) or string literal (``"string"``, or ``"fstring"`` for the literal pieces of
    an f-string on 3.12+): ``begin``/``end`` bound what an interpolation must sit inside to end
    it, ``start``/``stop`` the text that is not code. On 3.12+ an f-string is tokenized in
    pieces, and the whole outermost f-string is the bound (a quote in any piece ends it). A
    text that does not tokenize has none."""
    starts = [0]
    for line in text.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    spans, opened = [], []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            begin = starts[tok.start[0] - 1] + tok.start[1]
            end = starts[tok.end[0] - 1] + tok.end[1]
            if tok.type in _OPENS:
                opened.append(begin)
            elif tok.type in _CLOSES and opened:
                outer = opened.pop()
                if not opened:
                    spans = [(outer, end, inner, c) if b is None else (b, e, inner, c)
                             for b, e, inner, c in spans]
            elif tok.type in _MIDDLES:
                spans.append((None, None, (begin, end), "fstring"))
            elif tok.type in _INERT:
                spans.append((begin, end, (begin, end),
                              "comment" if tok.type == tokenize.COMMENT else "string"))
    except (tokenize.TokenError, SyntaxError):
        return []
    return [span for span in spans if span[0] is not None]
