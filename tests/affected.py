#!/usr/bin/env python3
"""``python3 tests/affected.py [--run] PATH…``: the tests that depend on a change (#362).

The command's entry point only. The selection is ``tools/affected_tests.py``, outside the test
paths the host's change proof copies from the head, so a base run tests the base's selector
(#1973).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from affected_tests import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
