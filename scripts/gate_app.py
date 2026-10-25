#!/usr/bin/env python3
"""App events gate (#1239): the route a GitHub App's own webhook delivers to.

One App's webhook serves every repository it is installed on, so this route is shared by every
loop whose ``ingress`` is ``app-webhook`` with this App (``config.app_route``). The Hermes gateway
runs a route script with the payload only, so the event is read from the payload's shape
(``app_ingress.gate_for``), then the matching gate runs in this process exactly as its own route
would run it: ``gate_failures.run`` under that gate's name, so its decisions, failures and redrives
name the real gate.

Before any gate: a loop that still uses repo hooks is not woken from here (its own hooks deliver
the same event), and a paused loop's delivery is dropped (GitHub cannot pause an App webhook for
one repository; ``arm --pause`` sets the loop's flag instead).

stdin : a GitHub webhook payload
stdout: ``[SILENT]``
"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from diaktoros import (  # noqa: E402
    app_ingress,
    config,
    gate_decisions,
    state as state_mod,
)
from diaktoros.util import log  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parent


def _quiet(reason: str) -> None:
    """Not a loop event, or not this route's to act on: nothing runs."""
    if reason:
        log(reason)
    print("[SILENT]")
    raise SystemExit(0)


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except ValueError:
        _quiet("app events: the payload is not JSON")
    name, why = app_ingress.gate_for(payload)
    if not name:
        _quiet(f"app events: {why}")
    full = str((payload.get("repository") or {}).get("full_name") or "")
    try:
        loop = config.loop_for_repo(full, warn=log)
    except config.ConfigError as exc:
        _quiet(f"app events: loop config refused for {full or 'an unknown repository'}: {exc}")
    if not loop:
        _quiet(f"app events: no loop configured for {full or 'an unknown repository'}")
    if not config.app_ingress(loop):
        # Its repo hooks deliver the same event: acting here too would run every gate twice.
        _quiet(f"app events: {loop['id']} uses repo hooks, not this App's webhook")
    st = state_mod.state_for(loop)
    if app_ingress.paused(st):
        reason = f"{loop['id']} is paused: the App's delivery is dropped (`hermes dk arm` resumes)"
        try:
            gate_decisions.record(loop, name, payload, "declined", reason)
        except Exception:
            pass
        _quiet(reason)
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"diaktoros_app_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The gate names itself from argv[0] (gate._record_decisions): it is that gate, not this one.
    sys.argv[0] = str(path)
    sys.stdin = io.StringIO(raw)
    from diaktoros import gate_failures
    gate_failures.run(name, module.main)


if __name__ == "__main__":
    main()
