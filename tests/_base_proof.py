"""Whether the host's base-failure proof takes a helper from the base (#1973).

``change_proof.apply_tests`` lays every file under a test path (``ci_fix.TEST_PATH``) at the head
over the base before the PR's tests run there. A helper on such a path (the JS comment stripper
when it lived in tests/test_desktop.py, the test selector in tests/affected.py) is the head's in
the base run too, so a test of it could never fail on the base. ``assert_taken_from_the_base``
checks the real helper's file against the real classifier, then runs ``apply_tests`` on a
synthetic base and head that each hold a marked copy at that same path, plus a head test that
reads the helper, and asserts the head's test sees the base's copy.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = """import importlib.util, pathlib
ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("helper", ROOT / {rel!r})
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
print(helper.SIDE)
"""


def assert_taken_from_the_base(case, helper_file) -> None:
    from diaktoros import change_proof, ci_fix
    rel = Path(helper_file).resolve().relative_to(ROOT).as_posix()
    case.assertIsNone(ci_fix.TEST_PATH.search(rel),
                      f"{rel} is on a test path: a base proof would run the head's copy")
    with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as temp:
        base, head = Path(temp) / "base", Path(temp) / "head"
        for tree in (base, head):
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_text(f"SIDE = {tree.name!r}\n")
        (head / "tests").mkdir(exist_ok=True)
        (head / "tests" / "test_probe.py").write_text(PROBE.format(rel=rel))
        change_proof.apply_tests(base, head)
        done = subprocess.run([sys.executable, "tests/test_probe.py"], cwd=base,
                              capture_output=True, text=True, timeout=60)
    case.assertEqual(done.returncode, 0, done.stderr)
    case.assertEqual(done.stdout.strip(), "base",
                     f"the head's tests read the head's {rel} on the base")
