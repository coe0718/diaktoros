#!/usr/bin/env python3
"""The flag tables in docs/commands.md, generated from the real ``hermes review-loop`` parser.

Each command's section in docs/commands.md holds a table between two markers:

    <!-- flags:init -->
    ...generated...
    <!-- /flags -->

Everything outside the markers is written by hand. ``python3 tests/commands_doc.py --write``
regenerates the tables after a flag changes; ``tests/test_commands_doc.py`` fails when a table is
stale or a command has no section, so the reference cannot drift from the CLI.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "commands.md"
MARKER = re.compile(r"<!-- flags:(?P<verb>[a-z-]+) -->\n.*?<!-- /flags -->", re.S)


def parser() -> argparse.ArgumentParser:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tests"))
    from harness.fixture import FakeCtx
    from review_loop import cli
    ctx = FakeCtx()
    # No settings form: every default shown is the schema's, the one a fresh install starts from.
    cli.register_cli(ctx, settings={})
    root = argparse.ArgumentParser(prog="hermes review-loop")
    ctx.setup(root)
    return root


def verbs(root: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def table(sub: argparse.ArgumentParser) -> str:
    rows = ["| flag | value | default | what it does |", "| --- | --- | --- | --- |"]
    groups = {id(a): g for g in sub._mutually_exclusive_groups for a in g._group_actions}
    for action in sub._actions:
        if isinstance(action, argparse._HelpAction) or not action.option_strings:
            continue
        flag = ", ".join(f"`{s}`" for s in action.option_strings)
        if action.nargs == 0:
            value = ""
        elif action.choices:
            value = " \\| ".join(f"`{c}`" for c in action.choices)
        else:
            value = f"`{(action.metavar or action.dest).upper()}`"
        if isinstance(action, argparse._AppendAction):
            value += " (repeatable)"
        default = action.default
        if action.required:
            shown = "**required**"
        elif default in (None, False, "", []) or action.nargs == 0:
            shown = ""
        else:
            shown = f"`{default}`"
        what = action.help or ""
        if id(action) in groups:
            what += (" " if what else "") + "(one of " + ", ".join(
                f"`{a.option_strings[0]}`" for a in groups[id(action)]._group_actions) + ")"
        rows.append(f"| {flag} | {value} | {shown} | {_cell(what)} |")
    if len(rows) == 2:
        return "No flags."
    return "\n".join(rows)


def render(text: str, root: argparse.ArgumentParser) -> str:
    subs = verbs(root)

    def replace(match: re.Match) -> str:
        verb = match["verb"]
        if verb not in subs:
            raise SystemExit(f"docs/commands.md has a table for unknown command {verb!r}")
        return f"<!-- flags:{verb} -->\n{table(subs[verb])}\n<!-- /flags -->"
    return MARKER.sub(replace, text)


def missing(text: str, root: argparse.ArgumentParser) -> list[str]:
    have = {m["verb"] for m in MARKER.finditer(text)}
    return sorted(set(verbs(root)) - have)


def main() -> int:
    write = "--write" in sys.argv[1:]
    root = parser()
    text = DOC.read_text()
    fresh = render(text, root)
    gaps = missing(text, root)
    if gaps:
        print("docs/commands.md has no section (flags marker) for: " + ", ".join(gaps))
    if write:
        DOC.write_text(fresh)
        print("docs/commands.md tables regenerated")
        return 1 if gaps else 0
    if fresh != text:
        print("docs/commands.md flag tables are stale: run `python3 tests/commands_doc.py --write`")
        return 1
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
