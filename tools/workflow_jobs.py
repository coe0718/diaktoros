"""The workflow time-limit guard (#1415, #1447), read as text: the tests are stdlib-only, so no
YAML parser. It lives outside tests/ so the host's base-failure proof sees a change to it.

``jobs`` walks the top-level ``jobs`` mapping line by line. A line reader can't follow every valid
YAML form (an indented root, flow style, a quoted or anchored ``jobs`` key), so ``unvouched``
cross-checks it: every ``runs-on`` key in the file must belong to a job the reader found. A
workflow with one it didn't is reported as unreadable, never passed as "no unbounded job"
(reviews of #2700, F1, F3 and F4). ``problems`` is the guard the tests run over every workflow.
"""
from __future__ import annotations

import pathlib
import re

# A job's header under ``jobs:``: two spaces, its id (bare, or single- or double-quoted), a
# colon, and at most a comment after it.
HEADER = re.compile(r"""^  (?:"([^"]+)"|'([^']+)'|([A-Za-z0-9_-]+))\s*:\s*(?:#.*)?$""")
TIMEOUT = re.compile(r"^    timeout-minutes:\s*\d+\s*(?:#.*)?$")
# The top-level ``jobs`` key, bare or with a comment after it.
JOBS = re.compile(r"^jobs:\s*(?:#.*)?$")
# A ``runs-on`` key anywhere: block or flow style, bare or quoted.
RUNS_ON = re.compile(r"""(?:^|[\s{,])["']?runs-on["']?\s*:""")
JOB_RUNS_ON = re.compile(r"^    runs-on\s*:")


def jobs(text: str) -> dict[str, list[str]]:
    """Each job under ``jobs:`` and its lines, read line by line."""
    found: dict[str, list[str]] = {}
    current = None
    inside = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue                       # blank or comment-only: never structure (#1415 F2)
        if not line.startswith(" "):
            inside, current = bool(JOBS.match(line.rstrip())), None    # a top-level key ends the jobs
            continue
        at_job_level = inside and line.startswith("  ") and not line.startswith("   ")
        match = HEADER.match(line) if at_job_level else None
        if match:
            current = next(name for name in match.groups() if name)
            found[current] = []
        elif at_job_level:
            # Fail closed (#1415 F3): a job-level line this reader does not understand is a job
            # it cannot vouch for, never a line of the job before it.
            current = f"(unrecognised: {line.strip()})"
            found[current] = []
        elif current is not None:
            found[current].append(line)
    return found


def missing(text: str) -> list[str]:
    """The jobs the reader found with no job-level ``timeout-minutes``."""
    return sorted(name for name, lines in jobs(text).items()
                  if not any(TIMEOUT.match(line) for line in lines))


def unvouched(text: str) -> bool:
    """Whether the file has a ``runs-on`` key outside every job the reader found: a job in a form
    the reader can't walk, so it can't say whether that job is bounded.

    The keys are counted in the raw text, comments and quoted strings included. Telling a comment
    from a ``#`` inside a quoted scalar needs a YAML parser, and cutting at the first ``#`` hid a
    flow-style job behind ``name: "build #1"`` (review of #2700, F4). Counting everything can only
    over-count: a comment or string that mentions the key fails the guard (reword it), never
    passes an unbounded job."""
    declared = sum(len(RUNS_ON.findall(line)) for line in text.splitlines())
    walked = sum(1 for lines in jobs(text).values() if any(JOB_RUNS_ON.match(x) for x in lines))
    return declared != walked


def problems(paths) -> list[str]:
    """The guard: every job without a time limit, and every workflow it can't read, one line each."""
    out = []
    for path in sorted(pathlib.Path(p) for p in paths):
        text = path.read_text(encoding="utf-8")
        out += [f"{path.name}: {name} has no timeout-minutes" for name in missing(text)]
        if unvouched(text):
            out.append(f"{path.name}: a job this guard cannot read")
    return out
