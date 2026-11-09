#!/usr/bin/env python3
"""Publish one PR's review cycle to the site's run viewer (#1749).

The viewer on the launch page shows only cycles published here, from the real
``hermes dk history --pr N --format json`` output (#1657). The run ledger lives on the
loop's host, so nothing reaches the public site unless someone runs the command there and
publishes its output with this script:

    hermes dk history --loop <loop-id> --pr 1624 --format json > cycle.json
    python3 docs/site/publish-history.py cycle.json      # or "-" to read stdin

It writes ``docs/site/history/<pr>.json`` and lists it in ``docs/site/history/index.json``.
It refuses rather than guesses: an unknown ``schema``, or any string that looks like a
credential, stops it with nothing written. A local path in any text (a review that quotes
``~/.hermes/...``) is replaced with ``[path]``. What only the loop's host saw (a failed
turn's output tail, a shadow review's text, a dispute or decline body) is withheld; the event
stays, so the timeline never loses a step. GitHub links are kept; any other link is dropped.
The Markdown export is the command's own renderer, run on the published data.
"""
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = HERE / "history"
SCHEMAS = {1}                     # the history JSON schemas this viewer was built against
# Kinds whose text past the headline came from the host alone, never from a public GitHub page.
HOST_ONLY = {"turn", "shadow", "dispute", "decline"}
_HEADLINE = re.compile(r" — |: ")
_PATH = re.compile(r"(?<![\w/.:])(?:~|/(?:home|root|Users|var|tmp|etc|opt|srv|mnt))/[^\s`'\")\]]*"
                   r"|(?<![\w])[A-Za-z]:\\[^\s`'\")\]]*")
_SECRET = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|sk-[A-Za-z0-9_-]{20,}|xox[abprs]-\w+"
    r"|AKIA[0-9A-Z]{16})"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----")
_EVENT_KEYS = ("at", "kind", "who", "login", "text", "gap")


class Refused(Exception):
    """The input is not safe or not understood; nothing was written."""


def _scrub(value):
    """Every string in ``value`` with its local paths replaced by ``[path]``."""
    if isinstance(value, str):
        return _PATH.sub("[path]", value)
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def _unsafe(value, where="data"):
    """Every string in ``value`` checked; the first that looks like a credential refuses."""
    if isinstance(value, str):
        if _SECRET.search(value):
            raise Refused(f"{where} looks like a credential; not published")
    elif isinstance(value, dict):
        for key, item in value.items():
            _unsafe(item, f"{where}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _unsafe(item, f"{where}[{i}]")


def _link(raw: str) -> str:
    """Only links to GitHub, or the command's own short forms the renderer makes whole."""
    raw = str(raw or "")
    if raw.startswith(("https://github.com/", "#issuecomment-", "issue:")):
        return raw
    return ""


def _event(raw: dict) -> dict:
    event = {key: raw[key] for key in _EVENT_KEYS if raw.get(key) not in (None, "")}
    event.setdefault("text", "")
    event["link"] = _link(raw.get("link"))
    if event.get("kind") in HOST_ONLY and not event["link"]:
        head = _HEADLINE.split(event["text"], 1)
        if len(head) > 1:
            event["text"] = head[0]
            event["withheld"] = True
    return event


def sanitize(data: dict) -> dict:
    """The command's JSON, cut down to what the public page may carry."""
    if not isinstance(data, dict) or data.get("schema") not in SCHEMAS:
        raise Refused(f"history schema {data.get('schema') if isinstance(data, dict) else '?'} "
                      f"is not one this viewer reads ({sorted(SCHEMAS)})")
    try:
        pr = int(data["pr"])
        out = {"schema": data["schema"], "repo": str(data["repo"]), "loop": str(data["loop"]),
               "pr": pr, "state": str(data.get("state") or ""), "merged": data.get("merged"),
               "gaps": [str(g) for g in data.get("gaps") or []],
               "missing": [str(m) for m in data.get("missing") or []],
               "findings": list(data.get("findings") or []),
               "rounds": [{"round": int(r["round"]), "head": str(r.get("head") or ""),
                           "events": [_event(e) for e in r.get("events") or []]}
                          for r in data.get("rounds") or []]}
    except (KeyError, TypeError, ValueError) as exc:
        raise Refused(f"not a history export ({type(exc).__name__}: {exc})") from None
    out = _scrub(out)
    _unsafe(out)
    return out


def markdown(data: dict) -> str:
    sys.path.insert(0, str(ROOT))
    from diaktoros import history_render
    return history_render.markdown(data)


def entry(data: dict) -> dict:
    """The index line the viewer's picker shows."""
    rulings = sum(e["kind"] == "ruling" for r in data["rounds"] for e in r["events"])
    return {"pr": data["pr"], "repo": data["repo"], "state": data["state"],
            "merged_at": (data["merged"] or {}).get("at"), "rounds": len(data["rounds"]),
            "findings": len(data["findings"]), "rulings": rulings}


def publish(raw: dict, out: pathlib.Path = OUT) -> pathlib.Path:
    data = sanitize(raw)
    data["markdown"] = markdown(data)
    _unsafe(data["markdown"], "markdown")
    data["published"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {"cycles": []}
    cycles = [c for c in index.get("cycles") or [] if c.get("pr") != data["pr"]]
    cycles.append(entry(data))
    index = {"schema": 1, "cycles": sorted(cycles, key=lambda c: -c["pr"])}
    path = out / f"{data['pr']}.json"
    path.write_text(json.dumps(data, indent=1) + "\n")
    index_path.write_text(json.dumps(index, indent=1) + "\n")
    return path


def main(argv: list) -> int:
    if len(argv) != 1:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    source = sys.stdin.read() if argv[0] == "-" else pathlib.Path(argv[0]).read_text()
    try:
        path = publish(json.loads(source))
    except (Refused, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    print(f"published {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
