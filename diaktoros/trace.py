"""``trace``: run one webhook through its gate as a dry run, and say what it would have done (#216).

The gateway logs nothing when a gate declines (a gate answers ``[SILENT]`` and exits 0, #209),
so "why did that delivery start nothing?" has no answer on disk. This one runs the **real,
unmodified gate script** on the delivery's payload, in a child process whose Hermes home is a
temporary **copy** of this loop's: its config, state and a snapshot of the run ledger. The gate's
local writes (seat claims, marks, ledger rows) land in the copy, so its logic runs exactly as it
would live, and a harness in the child turns everything that would leave the machine into a
record instead: any non-GET request (GitHub writes, gateway route POSTs, observer notices), and
any process launch (the isolated worker). GitHub *reads* are real, so the gate judges the PR as
it is now. The copy is deleted afterwards.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap

from .ledger import LOCK_WAIT_S
from . import config, gh, route_intent, routes, util
from . import envnames
from .util import logged

PLUGIN = pathlib.Path(__file__).resolve().parents[1]
# The events a loop's repo hooks deliver, and the seat route each one feeds (``issues`` only
# when the loop has a triage route: see ``role_for``).
EVENT_ROLE = {"pull_request": "reviewer", "pull_request_review": "fixer", "issues": "triage"}
MAX_DELIVERY_PAGES = 3
# Not copied: the crate cache and isolated run trees are large and no gate reads them; the ledger
# is copied by sqlite's backup API rather than byte for byte.
_SKIP = shutil.ignore_patterns("deps", "isolated-runs", "*.sqlite", "*.sqlite-wal",
                               "*.sqlite-shm", "*.sqlite-journal")

_HARNESS = textwrap.dedent('''
    import atexit, json, os, runpy, subprocess, sys, urllib.error, urllib.parse, urllib.request
    script, plugin, report = sys.argv[1], sys.argv[2], sys.argv[3]
    sys.path.insert(0, plugin)
    effects = []
    def save():
        with open(report, "w") as out:
            out.write(json.dumps(effects))
    atexit.register(save)
    stub = os.environ.get("DIAKTOROS_GH_STUB") or os.environ.get("REVIEW_LOOP_GH_STUB") or ""

    _urlopen = urllib.request.urlopen
    def urlopen(req, *args, **kwargs):
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        if method != "GET":
            url = urllib.parse.urlsplit(getattr(req, "full_url", str(req)))
            effects.append(["send", method + " " + (url.hostname or "") + url.path])
            raise urllib.error.URLError("trace: dry run, not sent")
        return _urlopen(req, *args, **kwargs)
    urllib.request.urlopen = urlopen

    from diaktoros import gh
    _request = gh._request
    def request(loop, path, method, body, login, timeout):
        if str(method).upper() != "GET":
            effects.append(["github", str(method).upper() + " " + path.split("?")[0]])
            return gh.Response(None, "trace: dry run, not sent", None, {})
        return _request(loop, path, method, body, login, timeout)
    gh._request = request

    _popen = subprocess.Popen
    def popen(argv, *args, **kwargs):
        first = argv[0] if isinstance(argv, (list, tuple)) and argv else argv
        if stub and str(first) == stub:          # the test suite's GitHub stub: a read
            return _popen(argv, *args, **kwargs)
        shown = argv if isinstance(argv, (list, tuple)) else [argv]
        effects.append(["process", " ".join(os.path.basename(str(part)) if i == 0 else str(part)
                                            for i, part in enumerate(shown[:4]))])
        raise OSError("trace: dry run, process not started")
    subprocess.Popen = popen

    sys.argv[0] = script
    os.chdir(os.path.dirname(script))
    runpy.run_path(script, run_name="__main__")
''')


class TraceError(Exception):
    """The trace could not be set up (unknown delivery, unreadable payload, no such route)."""


def _rows(db: pathlib.Path) -> list[tuple]:
    if not db.exists():
        return []
    with contextlib.closing(sqlite3.connect(db, timeout=LOCK_WAIT_S)) as con:
        try:
            return con.execute("SELECT id, seat, pr, head, state FROM runs").fetchall()
        except sqlite3.Error:
            return []


def _copy_home(loop: dict, dst: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
    """A disposable Hermes home holding what the gates read.

    Returns the copied ledger and a ``copy path → real path`` map, so what the gate logs about
    the copy is shown with the paths the operator knows.
    """
    root = config.home()
    real = {str(dst): str(root)}
    dst.mkdir(mode=0o700)
    # The copy keeps each host file's name as it is here (old or new), so the gate run in the
    # copy resolves it exactly as the live install does.
    runtime = config.host_path("runtime", root)
    if runtime.is_file():
        shutil.copy2(runtime, dst / runtime.relative_to(root))
    if routes.subs_path().is_file():      # the route registry, wherever this host keeps it
        shutil.copy2(routes.subs_path(), dst / "webhook_subscriptions.json")
    if (root / "state").is_dir():
        shutil.copytree(root / "state", dst / "state", ignore=_SKIP, symlinks=True)
    source = config.host_path("ledger", root)
    ledger = dst / source.relative_to(root)
    if source.is_file():
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True,
                                                   timeout=LOCK_WAIT_S)) as src, \
                contextlib.closing(sqlite3.connect(ledger)) as out:  # private new file
            src.backup(out)
    configs = dst / config.host_path("config_dir", root).relative_to(root)
    configs.mkdir()
    for path in sorted(config.config_dir().glob("*.json")):
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        state_dir = pathlib.Path(str(raw.get("state_dir") or "")).expanduser()
        if state_dir.is_absolute():
            try:
                raw["state_dir"] = str(dst / state_dir.relative_to(root))
            except ValueError:      # a custom state_dir outside the home: copy it beside the rest
                copy = dst / "external" / path.stem
                if state_dir.is_dir():
                    shutil.copytree(state_dir, copy, ignore=_SKIP, symlinks=True)
                raw["state_dir"] = str(copy)
                real[str(copy)] = str(state_dir)
        (configs / path.name).write_text(json.dumps(raw))
    real[str(configs)] = str(config.config_dir())
    return ledger, real


def _script_for(loop: dict, role: str) -> pathlib.Path:
    name = route_intent.GATE_SCRIPT.get(role)
    if not name:
        raise TraceError(f"no gate for role {role!r}")
    return PLUGIN / "scripts" / name


def infer_event(payload: dict) -> str:
    """The event a bare payload file was, from its own shape (``--payload`` without ``--event``)."""
    if isinstance(payload.get("issue"), dict) and not isinstance(payload.get("pull_request"), dict):
        return "issues"
    if isinstance(payload.get("review"), dict):
        return "pull_request_review"
    return "pull_request"


def role_for(loop: dict, event: str, route: str | None = None) -> str:
    """Which seat's gate a delivery reaches: the route it was sent to, else its event."""
    if route:
        for role, name in route_intent.routes_of(loop).items():
            if name == route and role in route_intent.GATE_SCRIPT:
                return role
        raise TraceError(f"route {route!r} is not one of loop {loop['id']}'s routes")
    if event not in EVENT_ROLE:
        raise TraceError(f"event {event!r} is not one the loop's repo hooks deliver "
                         f"({', '.join(EVENT_ROLE)})")
    role = EVENT_ROLE[event]
    if role == "triage" and "triage" not in route_intent.routes_of(loop):
        raise TraceError(f"loop {loop['id']} has no triage route (issue triage is off), so an "
                         f"{event!r} delivery reaches no gate")
    return role


def fetch_delivery(loop: dict, delivery: str, login: str | None) -> tuple[dict, str, str]:
    """``(payload, event, route)`` of one recorded delivery to this loop's hooks.

    ``delivery`` is GitHub's numeric delivery id or the ``X-GitHub-Delivery`` GUID the webhook
    page shows. Reading deliveries needs a token with hook read access (``admin:repo_hook`` or
    ``repo``): the loop's hook admin.
    """
    hooks, error = gh.hooks_read(loop, login)
    if hooks is None:
        raise TraceError(f"cannot read the repo's hooks ({error}) — pass --admin-token LOGIN, a "
                         "login whose token can read hooks")
    names = set(route_intent.routes_of(loop).values())
    ours = [hook for hook in hooks
            if routes.route_name_of(str((hook.get("config") or {}).get("url") or "")) in names]
    wanted = delivery.strip()
    for hook in ours:
        base = f"/repos/{loop['repo']}/hooks/{hook['id']}/deliveries"
        number = wanted if wanted.isdigit() else None
        if number is None:
            for page in range(1, MAX_DELIVERY_PAGES + 1):
                listing = gh.api(loop, f"{base}?per_page=100&page={page}", login=login)
                if not isinstance(listing, list) or not listing:
                    break
                match = next((d for d in listing if isinstance(d, dict)
                              and str(d.get("guid") or "") == wanted), None)
                if match:
                    number = str(match.get("id"))
                    break
        if number is None:
            continue
        found = gh.api(loop, f"{base}/{number}", login=login)
        request = (found or {}).get("request") if isinstance(found, dict) else None
        if isinstance(request, dict) and isinstance(request.get("payload"), dict):
            route = routes.route_name_of(str((hook.get("config") or {}).get("url") or ""))
            return request["payload"], str(found.get("event") or ""), route
    raise TraceError(f"no delivery {wanted!r} on this loop's repo hooks (the last "
                     f"{MAX_DELIVERY_PAGES * 100} per hook were searched)")


def facts(event: str, payload: dict) -> list[str]:
    """The delivery as the gate will judge it, in one or two lines."""
    pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}

    def who(value) -> str:
        return (value.get("login") if isinstance(value, dict) else None) or "?"

    if event == "issues":
        issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
        parts = [f"{event}/{payload.get('action') or '?'}", f"issue #{issue.get('number') or '?'}",
                 f"sender {who(payload.get('sender'))}", f"author {who(issue.get('user'))}"]
        if isinstance(payload.get("label"), dict):
            parts.append(f"label {payload['label'].get('name') or '?'}")
        names = [str(lb.get("name")) for lb in issue.get("labels") or []
                 if isinstance(lb, dict) and lb.get("name")]
        if names:
            parts.append(f"labels {', '.join(names)}")
        return [" · ".join(parts)]
    number = pr.get("number") or payload.get("number") or "?"
    parts = [f"{event}/{payload.get('action') or '?'}", f"PR #{number}",
             f"sender {who(payload.get('sender'))}", f"author {who(pr.get('user'))}",
             f"head {str((pr.get('head') or {}).get('sha') or '?')[:7]}",
             f"base {(pr.get('base') or {}).get('ref') or '?'}",
             "draft" if pr.get("draft") else "not draft"]
    if payload.get("requested_reviewer"):
        parts.append(f"requested {who(payload.get('requested_reviewer'))}")
    review = payload.get("review") if isinstance(payload.get("review"), dict) else None
    if review:
        parts.append(f"review {str(review.get('state') or '?').lower()} by {who(review.get('user'))}")
    return [" · ".join(parts)]


def outcome(logs: list[str], effects: list, queued: list) -> str:
    """The gate's decision in one line: a run it would start, a hold, or why it declined."""
    worker = [what for kind, what in effects if kind == "process" and "run_supervisor" in what]
    if worker or queued:
        seat = queued[0][1] if queued else "isolated"
        return f"would start a {seat} run" if worker else f"would queue a {seat} run"
    said = logged(logs)
    held = next((line for line in said if " held: " in line or line.startswith("held")), "")
    if held:
        return f"held — {held}"
    # The decision is the last thing the gate said that is not the observer's own bookkeeping.
    reason = next((line for line in reversed(said)
                   if not line.startswith(("observer:", "fired ", "could not fire"))), "")
    return f"declined — {reason}" if reason else "declined (no reason logged)"


def run(loop: dict, payload: dict, event: str, role: str, out=print) -> int:
    """Dry-run ``payload`` through ``role``'s gate and print what it did and would have done."""
    script = _script_for(loop, role)
    out(f"[{loop['id']}] trace: {role} gate ({script.name}) — dry run: the gate runs for real on a "
        "temporary copy of this loop's state; nothing is posted, no run is started")
    for line in facts(event, payload):
        out(f"  delivery:  {line}")
    with tempfile.TemporaryDirectory(prefix="review-loop-trace-") as tmp:
        home = pathlib.Path(tmp) / "hermes"
        ledger, real = _copy_home(loop, home)
        before = set(_rows(ledger))
        report = pathlib.Path(tmp) / "effects.json"
        env = {key: value for key, value in os.environ.items()
               if key not in (*envnames.both("CONFIG_DIR"), *envnames.both("SUBS"))}
        env["HERMES_HOME"] = str(home)
        env = util.leak_guard_env(env)
        proc = subprocess.run([sys.executable, "-c", util.leak_guard_code(_HARNESS), str(script),
                               str(PLUGIN), str(report)],
                              input=json.dumps(payload), capture_output=True, text=True,
                              env=env, timeout=120, check=False)
        try:
            effects = json.loads(report.read_text())
        except (OSError, ValueError):
            effects = []
        queued = [row for row in _rows(ledger) if row not in before]
    def shown(text: str) -> str:
        for copy, path in sorted(real.items(), key=lambda item: -len(item[0])):
            text = text.replace(copy, path)
        return text

    # A launch the harness refused is listed under "would start"; the gate's own complaint about
    # it ("drain fixer failed: trace: …") is an artifact of the dry run, not something it said.
    logs = [shown(line) for line in proc.stderr.splitlines()
            if line.strip() and "trace: dry run" not in line]
    out("  gate log:" if logs else "  gate log:  (nothing logged)")
    for line in logs[-40:]:
        out(f"    {line}")
    for kind, what in effects:
        label = {"github": "would write to GitHub", "send": "would send",
                 "process": "would start"}.get(kind, kind)
        out(f"  {label}: {shown(what)}")
    for _id, seat, number, head, state in queued:
        out(f"  would queue: {seat} run for #{number} at {str(head)[:7]} ({state})")
    verdict = outcome(logs, effects, queued)
    if proc.returncode != 0:
        verdict += f" (gate exited {proc.returncode})"
    out(f"  outcome:   {verdict}")
    out(f"  answer:    {proc.stdout.strip() or '(empty)'} — what the gateway would have received")
    return 0
