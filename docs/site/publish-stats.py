#!/usr/bin/env python3
"""Publish the loop's public stats to the site's "What the second opinion caught" section (#2253).

The run ledger lives on the loop's host, so nothing reaches the public site unless someone runs
the export there and publishes its output with this script:

    hermes dk stats --loop <loop-id> --public --json > stats.json
    python3 docs/site/publish-stats.py stats.json             # or "-" to read stdin

It writes ``docs/site/stats.json``, whole or not at all. It refuses rather than guesses: an
unknown ``schema``, a value of the wrong type, or any string that looks like a credential stops
it with nothing written. The output is rebuilt from the keys this page reads, so a field the
export grows later is never published by accident. A local path in any text is replaced with
``[path]``; only GitHub links are kept. A ``models`` section (``--public --models``) is dropped,
with a line saying so, unless this script is also given ``--models``.
"""
import importlib.util
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "stats.json"
SCHEMAS = {1}                 # the export schemas this section was built against
_REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILERS = ("reviewer", "shadow", "arbiter")
_MODEL_COUNTS = ("verdicts", "approved", "changes", "shadowed_approvals", "shadow_disagreed",
                 "upheld", "overruled")

# The run viewer's refusals (#1749), shared so the two pages cannot drift.
_spec = importlib.util.spec_from_file_location("publish_history", HERE / "publish-history.py")
_history = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_history)
Refused = _history.Refused


def _count(value, optional=False):
    """A whole number of at least 0, never a bool or a string; else TypeError."""
    if optional and value is None:
        return None
    if type(value) is not int or value < 0:
        raise TypeError(f"expected a count, got {type(value).__name__} {value!r:.40}")
    return value


def _day(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _DAY.match(value):
        raise TypeError(f"expected a date, got {value!r:.40}")
    return value


def _spread(raw):
    """``None`` or ``{n, median, p75}`` with real numbers."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError(f"expected {{n, median, p75}}, got {raw!r:.60}")
    return {"n": _count(raw.get("n")), "median": _history._number(raw.get("median")),
            "p75": _history._number(raw.get("p75"))}


def _model(raw: dict) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("model"), str):
        raise TypeError(f"a model row is not an object with a model name (got {raw!r:.60})")
    row = {"model": raw["model"][:80], "first": _day(raw.get("first")),
           "last": _day(raw.get("last")),
           "approve_share": _history._number(raw.get("approve_share"))}
    row.update({key: _count(raw.get(key)) for key in _MODEL_COUNTS})
    return row


def sanitize(data: dict, keep_models: bool = False) -> dict:
    """The export, cut down to what the public page reads."""
    if not isinstance(data, dict) or data.get("schema") not in SCHEMAS:
        raise Refused(f"stats schema {data.get('schema') if isinstance(data, dict) else '?'} "
                      f"is not one this page reads ({sorted(SCHEMAS)})")
    try:
        repo = data["repo"]
        if not isinstance(repo, str) or not _REPO.match(repo):
            raise ValueError(f"repo {repo!r:.60} is not owner/name")
        window, stopped, cycle = data["window"], data["stopped"], data["cycle"]
        found = data["findings"]
        prs = stopped["prs"]
        if not isinstance(prs, list) or not isinstance(found, dict):
            raise TypeError("stopped.prs is not a list, or findings not an object")
        out = {"schema": data["schema"], "repo": repo,
               "window": {"first": _day(window.get("first")), "last": _day(window.get("last")),
                          "prs": _count(window.get("prs")), "turns": _count(window.get("turns"))},
               "stopped": {"count": _count(stopped["count"]),
                           "prs": [_count(pr) for pr in prs]},
               "overruled": {"count": _count(data["overruled"]["count"])},
               "findings": {who: {"filed": _count((found.get(who) or {}).get("filed")),
                                  "completed": _count((found.get(who) or {}).get("completed"),
                                                      optional=True)}
                            for who in _FILERS},
               "cycle": {"rounds": _spread(cycle.get("rounds")),
                         "open_to_merge": _spread(cycle.get("open_to_merge")),
                         "change_proofs": _count(cycle.get("change_proofs"))},
               "partial": data.get("partial") is True}
        if keep_models and data.get("models") is not None:
            if not isinstance(data["models"], list):
                raise TypeError("models is not a list")
            out["models"] = [_model(row) for row in data["models"]]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise Refused(f"not a public stats export ({type(exc).__name__}: {exc})") from None
    _history._unsafe(data)           # a credential anywhere in the input refuses, kept or not
    out = _history._scrub(out)
    _history._unsafe(out)
    return out


def publish(raw: dict, keep_models: bool = False, out: pathlib.Path = OUT) -> tuple:
    """Write the sanitized stats; ``(path, dropped)``, dropped saying a models section went."""
    data = sanitize(raw, keep_models)
    data["published"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dropped = isinstance(raw, dict) and raw.get("models") is not None and not keep_models
    sys.path.insert(0, str(ROOT))
    from diaktoros import util
    util.atomic_write(out, json.dumps(data, indent=1) + "\n", mode=0o644)
    return out, dropped


def main(argv: list) -> int:
    keep = "--models" in argv
    args = [arg for arg in argv if arg != "--models"]
    if len(args) != 1:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    source = sys.stdin.read() if args[0] == "-" else pathlib.Path(args[0]).read_text()
    try:
        path, dropped = publish(json.loads(source), keep)
    except (Refused, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    if dropped:
        print("dropped the models section (pass --models to publish it)")
    print(f"published {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
