"""``hermes dk history --since DATE --out DIR``: every merged PR the loop worked on since a date,
one file each, from the same ``history.collect`` as ``--pr N``. For publishing several cycles at
once (the site's run viewer, #1749) instead of one PR at a time.

The PRs are the ones the run ledger has a turn for since the date. Each is collected whole (its
turns before the date included) and written only when GitHub says it merged; with ``--offline``
the merge cannot be read, so every PR with a turn is written and the listing says so. Nothing is
written outside ``DIR``; a file of the same name is replaced.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import history, history_render
from .run_supervisor import views

_SUFFIX = {"json": "json", "markdown": "md", "text": "txt"}


def prs_since(db: Path, repo: str, since: float) -> tuple[list[int], str]:
    """``(PR numbers with a turn since `since`, oldest first; a gap or "")``. Read-only."""
    try:
        con = views.read_only_connect(db)
    except sqlite3.Error as exc:
        return [], f"the run ledger is unreadable ({type(exc).__name__})"
    if con is None:
        return [], "no run ledger"
    try:
        rows = con.execute("SELECT pr, MIN(created) AS first FROM runs WHERE repo=? AND "
                           "created>=? AND pr IS NOT NULL GROUP BY pr ORDER BY first, pr",
                           (repo, since)).fetchall()
    except sqlite3.Error as exc:
        return [], f"the run ledger's runs are unreadable ({type(exc).__name__})"
    finally:
        con.close()
    return [int(row[0]) for row in rows], ""


def render(data: dict, fmt: str) -> str:
    """One cycle in ``fmt``, exactly as ``--pr N --format fmt`` prints it."""
    if fmt == "json":
        body = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False)
    else:
        body = history_render.markdown(data) if fmt == "markdown" else history_render.text(data)
    return body + "\n"         # the newline print adds, in every format (#2174)


def export(loop: dict, db: Path, since: float, out: Path, fmt: str, offline: bool,
           redact) -> dict:
    """Write each merged cycle to ``out/pr-N.<suffix>``. Returns ``{written, skipped, failed,
    gap}``: ``written`` as ``[{pr, file}]``, ``skipped`` as ``[{pr, why}]`` (not merged, its
    state), ``failed`` as ``{pr, file, why}`` when a write failed, which stops the export there
    with the files before it kept (#2200). A ``DIR`` that cannot be made raises ``OSError``."""
    numbers, gap = prs_since(db, loop["repo"], since)
    result: dict = {"written": [], "skipped": [], "failed": None, "gap": gap}
    if not numbers:
        return result
    out.mkdir(parents=True, exist_ok=True)
    for number in numbers:
        data = history.collect(loop, db, number, offline, redact)
        if not offline and not data["merged"]:
            result["skipped"].append({"pr": number, "why": data["state"] or "state unknown"})
            continue
        name = f"pr-{number}.{_SUFFIX[fmt]}"
        try:
            (out / name).write_text(render(data, fmt), encoding="utf-8")
        except OSError as exc:
            result["failed"] = {"pr": number, "file": name,
                                "why": exc.strerror or type(exc).__name__}
            break
        result["written"].append({"pr": number, "file": name})
    return result


def listing(result: dict, out: Path, offline: bool) -> str:
    """What was written and what was left out, for the terminal."""
    lines = [f"gap: {result['gap']}"] if result["gap"] else []
    for row in result["written"]:
        lines.append(f"wrote {row['file']} (#{row['pr']})")
    for row in result["skipped"]:
        lines.append(f"skipped #{row['pr']}: not merged ({row['why']})")
    failed = result.get("failed")
    if failed:
        lines.append(f"could not write {failed['file']} (#{failed['pr']}): {failed['why']}; "
                     "stopped there, so this export is partial (the files above were kept)")
    if offline and result["written"]:
        lines.append("--offline: merge state not read, so every PR with a turn was written")
    lines.append(f"{len(result['written'])} cycle(s) written to {out}" if result["written"]
                 else "no merged PR with a turn in that window: nothing written")
    return "\n".join(lines)
