"""Selftest step 5's hints, ``selftest_denials.DENIAL_FIX``, against what the broker raises.

Each key is a denial ``broker.authorize`` raises for the selftest's reviewer write in some state
(#1497), and each denial that write can meet has a key, not the generic hint (#2067). GitHub is
mocked by ``test_selftest.SelftestBase``.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_selftest import HEAD, ROOT, SelftestBase  # noqa: E402
from diaktoros import broker, gh, selftest_denials  # noqa: E402

# The broker's denials the selftest's call cannot meet: it passes the loop's repo and the number
# of the PR GitHub just answered for (an unreadable PR stops the step first), role "reviewer" and
# operation "review" without approve, so the fixer's checks and the conflict check never run. The
# head is whatever that answer says, so a malformed one meets the format check (#2704).
UNREACHABLE = {"wrong repository", "unsupported role or operation",
               "operation not permitted for role", "#{number} now conflicts with {loop.get('base')}",
               "PR author is not an authorized fixer", "cannot verify latest fixer verdict",
               "fixer verdict no longer current"}


def _denials() -> set[str]:
    """Every message ``authorize`` and the checks it runs raise, an f-string's fields as
    ``{expression}``, with the names ``authorize`` passes ``_verify_identities``."""
    tree = ast.parse((ROOT / "diaktoros" / "broker" / "__init__.py").read_text())
    found = set()
    for function in tree.body:
        if not (isinstance(function, ast.FunctionDef)
                and function.name in ("authorize", "_live_pr", "_verify_identities")):
            continue
        for node in ast.walk(function):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
                text = node.exc.args[0]
                parts = text.values if isinstance(text, ast.JoinedStr) else [text]
                found.add("".join(part.value if isinstance(part, ast.Constant)
                                  else "{" + ast.unparse(part.value) + "}" for part in parts))
    return {text.replace("{names}", "read, reviewer and fixer") for text in found}


class ReachableDenials(unittest.TestCase):
    def test_every_denial_the_reviewer_dry_run_can_meet_has_a_hint(self):
        denials = _denials()
        self.assertEqual(sorted(UNREACHABLE - denials), [])         # the list is not stale
        self.assertEqual(sorted(denials - UNREACHABLE - set(selftest_denials.DENIAL_FIX)), [])


class DenialFixKeys(SelftestBase):
    def test_every_denial_fix_key_is_a_denial_the_broker_raises(self):
        """#1497: each key is exactly what ``broker.authorize`` raises for a reviewer write in
        some state (a token denial's login as ``{identity}``), and ``fix_for`` finds its hint."""
        original = self.fx.fetch
        pr, loop = self.fx.pr, self.fx.loop
        (self.fx.root / "empty.pat").write_text("")

        def answer(override):
            def fetch(loop, path, method="GET", body=None, login=None):
                hit = override(path, login)
                return hit if hit is not None else original(loop, path, method, body, login)
            return fetch
        states = [
            lambda: pr.update(draft=True),
            lambda: pr["base"].update(ref="dev"),
            lambda: pr["base"].update(repo={"full_name": "other/widgets"}),
            lambda: pr["head"].update(repo={"full_name": "fork/widgets"}),
            lambda: pr["head"].update(sha="e" * 40),
            lambda: pr["head"].update(ref="other"),
            lambda: loop.update(read_token=""),
            lambda: loop["seats"]["fixer"].update(login="reviewer"),
            lambda: loop["tokens"].pop("fixer"),
            lambda: loop["tokens"].update(fixer=loop["tokens"]["reviewer"]),
            lambda: loop["tokens"].update(fixer=str(self.fx.root / "empty.pat")),
            lambda: loop["tokens"].update(fixer=str(self.fx.root / "gone.pat")),
            lambda: self.fx.ids.update(fixer=2),
            lambda: answer(lambda path, login: ({"login": "someone-else", "id": 2}, "")
                           if path == "/user" and login == "reviewer" else None),
            lambda: answer(lambda path, login: (None, "HTTP 404")
                           if path == "/repos/acme/widgets/pulls/7" else None),
        ]
        raised = set()
        for state in states:
            saved = json.loads(json.dumps([pr, loop])), dict(self.fx.ids)
            fetch = state()
            with mock.patch.object(gh, "fetch", side_effect=fetch or original):
                with self.assertRaises(broker.BrokerDenied) as caught:
                    broker.authorize(loop, repo="acme/widgets", number=7, head=HEAD,
                                     role="reviewer", branch="fix-7", operation="review")
            denial = str(caught.exception)
            self.assertNotEqual(selftest_denials.fix_for(denial), selftest_denials.GENERIC, denial)
            raised.add(denial.replace("token for fixer", "token for {identity}"))
            for live, before in zip((pr, loop), saved[0]):
                live.clear()
                live.update(before)
            self.fx.ids = saved[1]
        with self.assertRaises(broker.BrokerDenied) as caught:      # a malformed head (#2704)
            broker.authorize(loop, repo="acme/widgets", number=7, head="bad",
                             role="reviewer", branch="fix-7", operation="review")
        raised.add(str(caught.exception))
        self.assertEqual(raised, set(selftest_denials.DENIAL_FIX))

    def test_a_token_denial_gets_its_hint_through_the_selftest_run(self):
        """#2067: a denial naming a login is looked up with the login as ``{identity}``."""
        self.fx.loop["tokens"]["fixer"] = str(self.fx.root / "gone.pat")
        rc, text = self.run_selftest(pr=7, model=False)
        self.assertEqual(rc, 1)
        line = text.split("❌ broker:reviewer", 1)[1].splitlines()
        self.assertIn("missing token for fixer", line[0])
        self.assertIn("fix: see the identities step", line[1])

    def test_a_malformed_head_sha_gets_its_hint_through_the_selftest_run(self):
        """#2704: GitHub's answer for the PR carries no usable head SHA."""
        self.fx.pr["head"]["sha"] = "bad"
        rc, text = self.run_selftest(pr=7, model=False)
        self.assertEqual(rc, 1)
        line = text.split("❌ broker:reviewer", 1)[1].splitlines()
        self.assertIn("invalid PR number or head SHA", line[0])
        self.assertIn("fix: GitHub's answer for the PR had no 40-hex head SHA", line[1])


if __name__ == "__main__":
    unittest.main()
