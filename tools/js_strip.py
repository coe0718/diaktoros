"""The JS comment stripper the Desktop guards read ``desktop/plugin.js`` through (#1313, #1965).

``strip_js_comments`` returns the JS that runs, comments removed, for the first-render and
SDK-surface guards in ``tests/test_desktop.py``; its regression tests live there too. It sits here,
outside ``tests/``, so a change to it is an implementation change and not part of a test patch: a
base-failure proof that applies a PR's test paths to the merge base runs the new tests against the
base stripper, not the PR's (#1973). Standard library only; no I/O.
"""
from __future__ import annotations

import re

# One left-to-right scan over the JS tokens that can hold a comment marker without being a comment:
# string literals ('...', "..."), template literals (`...`, with each `${...}` scanned as code, so
# a template nested in one is a template too) and regex literals (/.../flags). They are kept
# verbatim, so a `/*`, `//` or quote inside one opens nothing and cannot hide the code after it
# (#1313), and a quote inside a regex literal cannot pair with one in a later comment (#1965).
# Only `//` line comments and `/* ... */` blocks are removed; whichever starts first wins, so a
# `//` inside a block comment (or a `/*` inside a line comment) is part of that comment.
# Not a full tokenizer: whether a `/` opens a regex is read from the token before it. After
# punctuation that cannot end an expression, or a keyword such as `return`, it is a regex; after a
# name, number, literal or `]`, a division. After `)`, `}`, `++` or `--` it can be either
# (`if (x) /re/` vs `(a) / b`, a block vs `{} / a`), so both readings are scanned: where they keep
# the same text the scan goes on, and where they differ it raises AmbiguousSlash rather than
# guess, so a guard built on it fails closed instead of passing on code it never saw (#1965).
# A quoted string or regex ends at a newline, and an unclosed one is kept as plain code.
_REGEX_AFTER = set("(,=:[!&|?{;+-*%<>~^")
_REGEX_AFTER_WORDS = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void",
                      "throw", "case", "do", "else", "yield", "await"}
_EITHER_AFTER = (")", "}", "++", "--")
_LAST_WORD = re.compile(r"[\w$]+$")


class AmbiguousSlash(ValueError):
    """A `/` that reads as a regex or as a division, where the two readings strip differently."""


def _kept_tail(out: list[str]) -> str:
    """The end of the code kept so far, trailing blanks dropped: enough to see the token before a
    `/`, however many one-character chunks and blank lines precede it."""
    tail = ""
    for chunk in reversed(out):
        tail = chunk + tail
        if len(tail.strip()) >= 16:
            break
    return tail.rstrip()


def _slash_reading(tail: str) -> str:
    """How a `/` after the kept code ``tail`` reads: ``"regex"``, ``"division"`` or ``"either"``
    (the token before it can end an expression or a statement)."""
    if tail.endswith(_EITHER_AFTER):
        return "either"
    if not tail or tail[-1] in _REGEX_AFTER:
        return "regex"
    word = _LAST_WORD.search(tail)
    return "regex" if word is not None and word.group() in _REGEX_AFTER_WORDS else "division"


def _literal_end(source: str, i: int) -> int:
    """The index just past the quoted string or regex literal opening at ``source[i]``, or -1 when
    it does not close before a newline or the end."""
    quote, in_class, j = source[i], False, i + 1
    while j < len(source):
        c = source[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return -1
        if quote == "/" and c in "[]":
            in_class = c == "["
        elif c == quote and not in_class:
            j += 1
            while quote == "/" and j < len(source) and (source[j].isalnum() or source[j] == "_"):
                j += 1   # the regex's flags
            return j
        j += 1
    return -1


def _template_end(source: str, i: int) -> int:
    """From inside a template literal at ``source[i]``: the index just past its closing backtick
    or past the `${` that opens an expression (the caller scans that as code), or the end."""
    while i < len(source):
        if source[i] == "\\":
            i += 2
        elif source[i] == "`":
            return i + 1
        elif source.startswith("${", i):
            return i + 2
        else:
            i += 1
    return len(source)


def _either_slash_end(source: str, i: int, depths: list[int], out: list[str]) -> int:
    """The index just past a `/` at ``source[i]`` that may open a regex or divide.

    With no regex closing on its line, it is a division (one character). Otherwise the text up to
    the regex's end is scanned again as division code: if that keeps it all, ends right there and
    leaves the `${` depths alone, the two readings agree and the regex span is kept. If not, the
    file's meaning depends on parsing this scanner does not do, so it raises AmbiguousSlash."""
    end = _literal_end(source, i)
    if end < 0:
        out.append("/")
        return i + 1
    division, divided_depths = [_kept_tail(out), "/"], list(depths)
    stop = _scan(source, i + 1, divided_depths, division, end)
    if stop != end or "".join(division[1:]) != source[i:end] or divided_depths != depths:
        line = source.count("\n", 0, i) + 1
        raise AmbiguousSlash(f"line {line}: `/` at {source[i:end]!r} reads as a regex or a "
                             "division, and the two strip differently")
    out.append(source[i:end])
    return end


def _scan(source: str, i: int, depths: list[int], out: list[str], stop: int) -> int:
    """Scan ``source`` from ``i`` as code, appending what is kept to ``out``, until ``stop`` or
    past it (a comment or literal that crosses it is taken whole); returns where it stopped.
    ``depths`` holds, per open `${`, the `{` depth inside it."""
    while i < stop:
        c = source[i]
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                out.append(source[i:])
                return len(source)
            i = end + 2
        elif source.startswith("//", i):
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
        elif c == "`" or (c == "}" and depths and depths[-1] == 0):
            if c == "}":
                depths.pop()                          # back in the template the `${` opened
            end = _template_end(source, i + 1)
            if source[end - 2:end] == "${":
                depths.append(0)
            out.append(source[i:end])
            i = end
        elif c == "/" and _slash_reading(_kept_tail(out)) == "either":
            i = _either_slash_end(source, i, depths, out)
        elif c in "'\"" or (c == "/" and _slash_reading(_kept_tail(out)) == "regex"):
            end = _literal_end(source, i)
            end = i + 1 if end < 0 else end
            out.append(source[i:end])
            i = end
        else:
            if depths and c in "{}":
                depths[-1] += 1 if c == "{" else -1
            out.append(c)
            i += 1
    return i


def strip_js_comments(source: str) -> str:
    """The JS that runs: `//` line comments and `/* ... */` / `/** ... */` blocks removed.

    String, template and regex literals are kept verbatim, comment markers inside them included.
    Raises AmbiguousSlash where a `/` may be a regex or a division and the readings differ."""
    out: list[str] = []
    _scan(source, 0, [], out, len(source))
    return "".join(out)
