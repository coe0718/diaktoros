#!/usr/bin/env python3
"""File and function sizes in the plugin's code, against a ratchet (#1349).

Agents read whole files and carry them in context for the rest of a turn: a 2,500-line file is
25–30K tokens, held on every step, and past the read cap it is paged or read in part. So code
files and functions are capped, and the cap only tightens.

**Size is what an agent reads, not how it is laid out:** the non-whitespace characters, counted
in line equivalents (``CHARS_PER_LINE``, the code's average per line, so the caps read as lines).
Joining two lines, or rewrapping an argument list, changes nothing; only less text does. It was
physical lines until a fix turn joined lines to pass the check, which made the code no simpler.

* A file not in the baseline may have a size of at most ``FILE_MAX``; a function, ``FUNCTION_MAX``.
* A baselined file may not grow past ``max(its baseline, FILE_CEILING)``: one already over the
  ceiling may not grow at all, and one under it may grow up to it while the splits happen. A
  baselined function may not grow past its baseline.
* When a baselined file (over the ceiling) or function shrinks, the baseline is lowered with it,
  and an entry that is back under the cap is removed: ``python3 tests/file_sizes.py --write``
  does both. It never raises an entry or adds one: those are a reviewed edit of the JSON.

Covers ``diaktoros/`` and ``scripts/`` (not ``tests/``). A function is named by its file and its
qualified name (``Class.method``, ``outer.inner``), and measured from its ``def`` line to its
last line, decorators excluded. ``tests/test_file_sizes.py`` runs the check.
"""
from __future__ import annotations

import ast
import json
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path(__file__).resolve().parent / "file_sizes_baseline.json"
SCANNED = ("diaktoros", "scripts")
FILE_MAX = 800
FILE_CEILING = 1500
FUNCTION_MAX = 80
CHARS_PER_LINE = 39          # the code's average non-whitespace characters per line (2026-10-29)
_WHITESPACE = re.compile(r"\s+")


def size(text: str) -> int:
    """Line equivalents: the non-whitespace characters of ``text`` over ``CHARS_PER_LINE``,
    rounded up, so layout (joined or wrapped lines, indentation) never moves it."""
    return math.ceil(len(_WHITESPACE.sub("", text)) / CHARS_PER_LINE)


def code_files(root: Path = ROOT) -> list[Path]:
    return sorted(path for top in SCANNED for path in (root / top).rglob("*.py")
                  if "__pycache__" not in path.parts)


def measure(root: Path = ROOT) -> tuple[dict[str, int], dict[str, int]]:
    """``(files, functions)``: the size of each code file and of each function
    (``path::qualname``)."""
    files, functions = {}, {}
    for path in code_files(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        files[rel] = size(text)
        lines = text.splitlines()
        seen: dict[str, int] = {}

        def visit(node, prefix=""):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    name = f"{prefix}{child.name}"
                    if not isinstance(child, ast.ClassDef):
                        key = f"{rel}::{name}"
                        seen[key] = seen.get(key, 0) + 1
                        if seen[key] > 1:          # a redefinition (a conditional def)
                            key = f"{key}#{seen[key]}"
                        functions[key] = size("\n".join(lines[child.lineno - 1:child.end_lineno]))
                    visit(child, f"{name}.")
                else:
                    visit(child, prefix)
        visit(ast.parse(text, filename=rel))
    return files, functions


def load_baseline(path: Path = BASELINE) -> dict:
    data = json.loads(path.read_text()) if path.exists() else {}
    return {"files": dict(data.get("files") or {}), "functions": dict(data.get("functions") or {})}


def problems(files: dict, functions: dict, baseline: dict) -> list[str]:
    """Every way the code breaks the ratchet, one line each, saying what to do."""
    out = []
    base_files, base_functions = baseline["files"], baseline["functions"]
    for rel, lines in sorted(files.items()):
        if rel in base_files:
            limit = max(base_files[rel], FILE_CEILING)
            if lines > limit:
                out.append(f"{rel}: size {lines}, over its limit {limit} — split it, never grow "
                           "a file already over the ceiling")
        elif lines > FILE_MAX:
            out.append(f"{rel}: size {lines}, over {FILE_MAX} for a new file — split it")
    for key, lines in sorted(functions.items()):
        if key in base_functions:
            if lines > base_functions[key]:
                out.append(f"{key}: size {lines}, grown past its baseline "
                           f"{base_functions[key]} — split it")
        elif lines > FUNCTION_MAX:
            out.append(f"{key}: size {lines}, over {FUNCTION_MAX} for a new function — split it")
    # The ratchet tightens: a shrink lowers the baseline, and a gone or small entry leaves it.
    for rel, base in sorted(base_files.items()):
        now = files.get(rel)
        if now is None or now <= FILE_MAX:
            out.append(f"{rel}: baselined at {base} but {'gone' if now is None else now} now — "
                       "remove it from the baseline (python3 tests/file_sizes.py --write)")
        elif base > FILE_CEILING and now < base:
            out.append(f"{rel}: size {now}, below its baseline {base} — lower the baseline "
                       "(python3 tests/file_sizes.py --write)")
    for key, base in sorted(base_functions.items()):
        now = functions.get(key)
        if now is None or now <= FUNCTION_MAX:
            out.append(f"{key}: baselined at {base} but {'gone' if now is None else now} now — "
                       "remove it from the baseline (python3 tests/file_sizes.py --write)")
        elif now < base:
            out.append(f"{key}: size {now}, below its baseline {base} — lower the baseline "
                       "(python3 tests/file_sizes.py --write)")
    return out


def tightened(files: dict, functions: dict, baseline: dict) -> dict:
    """The baseline lowered to today's sizes and cleared of entries back under the caps. Never
    raises an entry and never adds one."""
    out_files = {rel: min(base, files[rel]) if base > FILE_CEILING else base
                 for rel, base in baseline["files"].items()
                 if rel in files and files[rel] > FILE_MAX}
    out_functions = {key: min(base, functions[key]) for key, base in baseline["functions"].items()
                     if key in functions and functions[key] > FUNCTION_MAX}
    return {"files": dict(sorted(out_files.items())),
            "functions": dict(sorted(out_functions.items()))}


def snapshot(files: dict, functions: dict) -> dict:
    """A first baseline: every file over ``FILE_MAX`` and function over ``FUNCTION_MAX`` today."""
    return {"files": {rel: n for rel, n in sorted(files.items()) if n > FILE_MAX},
            "functions": {key: n for key, n in sorted(functions.items()) if n > FUNCTION_MAX}}


def write(data: dict, path: Path = BASELINE) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n")


def main(argv: list[str]) -> int:
    files, functions = measure()
    if argv == ["--write"]:
        write(tightened(files, functions, load_baseline()))
        print(f"{BASELINE.name}: lowered and cleared; nothing raised or added")
        return 0
    if argv == ["--snapshot"]:
        # Only for creating the first baseline (#1349); a reviewed change, never routine.
        write(snapshot(files, functions))
        print(f"{BASELINE.name}: written from today's sizes")
        return 0
    found = problems(files, functions, load_baseline())
    print("\n".join(found) if found else "every file and function is within the ratchet")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
