"""Skip-or-fail for the tests that drive the real Hermes in bubblewrap.

Off a prepared host these tests skip. With ``DIAKTOROS_REQUIRE_HERMES_SOURCE=1`` (the CI
``verticals`` job) a missing prerequisite is a failure instead, so a broken setup cannot turn
the job green by quietly skipping the tests it exists to run.
"""
import functools
import os
import unittest

REQUIRED = os.environ.get("DIAKTOROS_REQUIRE_HERMES_SOURCE") == "1"


def _message(reason):
    return (f"DIAKTOROS_REQUIRE_HERMES_SOURCE=1 but {reason} — set HERMES_AGENT_SOURCE to a "
            "hermes-agent checkout with a venv, install bubblewrap and a stable Rust toolchain")


def needs(condition, reason):
    """``unittest.skipUnless``, except that it fails when the prerequisites are required."""
    if condition:
        return lambda item: item
    if not REQUIRED:
        return unittest.skip(reason)

    def fail(item):
        if isinstance(item, type):
            def setUpClass(cls):
                raise AssertionError(_message(reason))
            item.setUpClass = classmethod(setUpClass)
            return item

        @functools.wraps(item)
        def wrapper(*args, **kwargs):
            raise AssertionError(_message(reason))
        return wrapper
    return fail


def skip_or_fail(test, reason):
    """``test.skipTest(reason)``, except that it fails when the prerequisites are required."""
    if REQUIRED:
        test.fail(_message(reason))
    test.skipTest(reason)
