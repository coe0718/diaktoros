#!/usr/bin/env python3
"""Required real-TanStack component lane; never rely on ambient desktop packages."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "tests/query-core"
QUERY_CORE_VERSION = "5.101.2"  # CI's HERMES_PINNED package-lock.json
EXPECTED_TESTS = 211  # 198 fixture cases + 13 real-TanStack cases
REQUIRED_REAL_TESTS = (
    *("health real TanStack fallback dedupes concurrent observers and remount; manual refresh recovers: " + reason
      for reason in ("caller", "unreachable")),
    "health real TanStack: automatic observers dedupe, switch loop, preserve freshness and recover manually",
    "health real TanStack mounted page: adapter follows selection, fallback and Retry",
    *("read Retry real REST endpoint: " + kind
      for kind in ("health", "stats", "settings", "records", "hooks", "overview")),
    "socket real TanStack: shared page and pane refresh once through REST, reconnect repairs missed changes",
    "socket real TanStack: OAuth no-op retains built-in polling",
    "socket real TanStack: disconnected retains built-in polling",
)


def validate_tap(output: str) -> None:
    """Reject an omitted block, skips/TODOs, truncated output and count drift."""
    if re.search(r"^(?:not ok\b|ok \d+\b[^\n]*#\s*(?:SKIP|TODO)\b|Bail out!)", output, re.M | re.I):
        raise ValueError("desktop suite contains failed, skipped or TODO tests")
    passed = re.findall(r"^ok (\d+) - (.+)$", output, re.M)
    if [int(number) for number, _ in passed] != list(range(1, EXPECTED_TESTS + 1)):
        raise ValueError(f"expected exactly {EXPECTED_TESTS} executed desktop tests")
    names = [name for _, name in passed]
    for name in REQUIRED_REAL_TESTS:
        if names.count(name) != 1:
            raise ValueError(f"required real-TanStack test missing or duplicated: {name}")
    if re.findall(r"^1\.\.(\d+)$", output, re.M) != [str(EXPECTED_TESTS)]:
        raise ValueError("desktop TAP plan is missing or disagrees with executed count")
    for label, expected in {"tests": EXPECTED_TESTS, "pass": EXPECTED_TESTS, "fail": 0,
                            "cancelled": 0, "skipped": 0, "todo": 0, "suites": 0}.items():
        if re.findall(rf"^# {label} (\d+)$", output, re.M) != [str(expected)]:
            raise ValueError(f"desktop TAP summary {label} must be {expected}")


def query_core_entry() -> Path:
    installed = PACKAGE / "node_modules/@tanstack/query-core"
    manifest = json.loads((PACKAGE / "package.json").read_text())
    lock = json.loads((PACKAGE / "package-lock.json").read_text())
    versions = (manifest["dependencies"]["@tanstack/query-core"],
                lock["packages"]["node_modules/@tanstack/query-core"]["version"],
                json.loads((installed / "package.json").read_text())["version"])
    if versions != (QUERY_CORE_VERSION,) * 3:
        raise ValueError(f"query-core manifest/lock/install must all pin {QUERY_CORE_VERSION}: {versions}")
    entry = installed / "build/modern/index.js"
    if not entry.is_file():
        raise ValueError(f"missing query-core ESM entry: {entry}")
    return entry.resolve()


def main() -> int:
    try:
        entry = query_core_entry()
        env = {key: value for key, value in os.environ.items()
               if key not in {"NODE_OPTIONS", "NODE_PATH", "DIAKTOROS_PLUGIN_SOURCE", "DIAKTOROS_QUERY_CORE"}}
        env["DIAKTOROS_QUERY_CORE"] = entry.as_uri()
        result = subprocess.run(
            ["node", "--experimental-vm-modules", "--test", "--test-reporter=tap",
             str(ROOT / "tests/desktop_behavior.mjs")],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
        )
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode:
            raise ValueError(f"desktop Node runner exited {result.returncode}")
        validate_tap(result.stdout)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"desktop real-TanStack acceptance FAILED: {error}", file=sys.stderr)
        return 1
    print(f"desktop real-TanStack acceptance: {EXPECTED_TESTS} passed, 0 skipped; query-core {QUERY_CORE_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
