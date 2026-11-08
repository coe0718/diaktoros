"""#1526: a CI-fix turn that rightly does not push ends through the broker, not its summary.

The real RunBroker serves a real Unix socket (``test_fixer_answers``' harness: GitHub an in-memory
fake, the push a stub). A CI-fix turn declines with ``request_review --decline KIND``: one PR
comment, one ledger record, one urgent notice, and the broker's ``completed``, so the turn ends
succeeded and nothing retries it. Every other use is refused before anything is spent.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import dataclasses
import io
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_answers as base  # noqa: E402
from diaktoros import broker_client, ci_decline, explain, ledger, observer, prompts  # noqa: E402
from diaktoros import run_supervisor  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

HEAD, REPO = base.HEAD, base.REPO
WHY = ("`tests/file_sizes.py` fails: diaktoros/cli/apply_command.py::_apply is 352 units against "
       "its baseline 351. The only failure; the function grew by a docstring line.\n")


class Decline(base.Base):
    def setUp(self):
        super().setUp()
        self.scope = dataclasses.replace(self.scope, ci_fix=True)
        self.notices = []
        patch = mock.patch.object(observer, "notify",
                                  side_effect=lambda *a, **kw: self.notices.append((a, kw)))
        patch.start()
        self.addCleanup(patch.stop)

    def decline(self, server, kind="size-limit", body=WHY):
        return self.send(server, {"operation": "request_review", "body": body,
                                  "verdict": f"DECLINE:{kind}"})

    def declines(self):
        with ledger.connect(self.db) as con:
            return con.execute("SELECT run_id, pr, head, kind, body FROM declines").fetchall()

    def test_a_ci_fix_that_declines_posts_once_records_once_and_ends_the_turn_done(self):
        with self.serving() as server:
            result = self.decline(server)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["result"]["answers"], "posted")
            self.assertTrue(server.completed)          # run_turn returns 0: the run succeeds
            self.assertFalse(self.decline(server)["ok"])   # one write per turn
        posts = self.fake.posts()
        self.assertEqual([p.rsplit("/", 1)[1] for p, _, _ in posts], ["comments"])
        text = posts[0][1]["body"]
        self.assertIn("CI fix declined (size-limit): no push.", text)
        self.assertIn("_apply is 352 units", text)
        self.assertIn(ci_decline.KINDS["size-limit"], text)
        self.assertEqual([tuple(r) for r in self.declines()],
                         [(self.run_id, 7, HEAD, "size-limit", WHY)])
        with Supervisor(self.db)._connect() as con:   # a write: never replayed, nor re-armed
            self.assertIsNotNone(run_supervisor.write_records(con, self.run_id))
        (args, kw), = self.notices
        self.assertEqual((args[2], args[3], args[4]), ("ci_declined", 7, HEAD))
        self.assertEqual((kw["identity"], kw["next_turn"]), (self.run_id, "you"))
        self.assertIn("declined (size-limit): `tests/file_sizes.py` fails", kw["outcome"])

    def test_not_this_pr_names_its_own_next_move(self):
        with self.serving() as server:
            self.assertTrue(self.decline(server, "not-this-pr", "The runner was shut down.")["ok"])
        self.assertIn(ci_decline.KINDS["not-this-pr"], self.fake.posts()[0][1]["body"])
        self.assertIn(ci_decline.KINDS["not-this-pr"], self.notices[0][1]["outcome"])

    def test_every_other_decline_is_refused_before_anything_is_spent(self):
        review_fix = dataclasses.replace(self.scope, ci_fix=False)
        with mock.patch.object(self, "scope", review_fix), self.serving() as server:
            refused = self.decline(server)
            self.assertFalse(refused["ok"])
            self.assertIn("only a CI-fix turn", refused["error"])
            self.assertFalse(server.completed)
        with self.serving() as server:
            for kind, body in (("flaky", WHY), ("size-limit", ""), ("size-limit", "  \n")):
                with self.subTest(kind=kind, body=body[:5]):
                    self.assertFalse(self.decline(server, kind, body)["ok"])
            self.assertEqual((self.fake.posts(), self.declines(), self.notices), ([], [], []))
            self.assertTrue(self.decline(server)["ok"])    # still unspent
        self.assertEqual(len(self.declines()), 1)

    def test_a_decline_after_a_push_is_refused(self):
        with self.serving() as server:
            self.assertTrue(self.send(server, {"operation": "push",
                                               "manifest": base.manifest()})["ok"])
            refused = self.decline(server)
            self.assertFalse(refused["ok"])
            self.assertIn("pushed nothing", refused["error"])
        self.assertEqual(self.declines(), [])

    def test_a_turn_that_never_calls_the_broker_is_still_unpublished(self):
        with self.serving() as server:
            self.assertEqual((server.requests, server.completed), (0, False))


class Client(unittest.TestCase):
    def run_client(self, *argv):
        with tempfile.TemporaryDirectory() as tmp:
            why = Path(tmp) / "why.md"
            why.write_text(WHY)
            argv = [a.replace("WHY", str(why)) for a in argv]
            with mock.patch.object(broker_client, "call", return_value={"ok": True}) as call, \
                    mock.patch.object(sys, "argv", ["broker_client", *argv]), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                try:
                    broker_client.main()
                except SystemExit:
                    pass
            return call.call_args_list

    def test_the_client_sends_the_kind_as_the_verdict(self):
        self.assertEqual(self.run_client("request_review", "--answers-file", "WHY",
                                         "--decline", "size-limit"),
                         [mock.call("request_review", body=WHY, verdict="DECLINE:size-limit")])

    def test_the_client_refuses_a_decline_it_cannot_send(self):
        for argv in (("request_review", "--decline", "size-limit"),
                     ("request_review", "--answers-file", "WHY", "--dispute", "--decline",
                      "not-this-pr"),
                     ("request_review", "--answers-file", "WHY", "--decline", "flaky"),
                     ("review", "--answers-file", "WHY", "--decline", "size-limit")):
            with self.subTest(argv=argv):
                self.assertEqual(self.run_client(*argv), [])
        self.assertEqual(broker_client.DECLINE_KINDS, tuple(ci_decline.KINDS))


class Reports(unittest.TestCase):
    def test_explain_names_the_decline_at_its_head_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "runs.sqlite"
            sup = Supervisor(db)
            sup.enqueue("fix", REPO, 7, HEAD, "fixer")
            ci_decline.record(db, sup.get("fix")["id"], REPO, 7, HEAD, "size-limit", WHY)
            c = types.SimpleNamespace(pr={}, state="open", repo=REPO, number=7, head=HEAD,
                                      short=HEAD[:7], blockers=[])
            with mock.patch.object(explain.config, "host_path", return_value=db), \
                    mock.patch.multiple(explain, _read_blockers=mock.DEFAULT,
                                        _gate_blockers=mock.DEFAULT, _run_blockers=mock.DEFAULT):
                explain._blockers(c)
                [line] = c.blockers
                self.assertIn(f"the CI fix at head {HEAD[:7]} was declined (size-limit)", line)
                self.assertIn(ci_decline.KINDS["size-limit"], line)
                c.head, c.blockers = "b" * 40, []            # a new head: nothing declined there
                explain._blockers(c)
                self.assertEqual(c.blockers, [])
        self.assertIsNone(ci_decline.at_head(Path(tmp) / "gone.sqlite", REPO, 7, HEAD))

    def test_the_notice_is_urgent_and_labelled(self):
        self.assertIn("ci_declined", observer.EVENTS)
        self.assertTrue(observer.is_urgent("ci_declined"))
        self.assertIn("ci_declined", observer.LABEL)
        self.assertIn("ci_declined", observer.EMOJI)

    def test_the_ci_fix_prompt_says_how_to_decline_and_no_longer_to_just_say_so(self):
        text = prompts.ISOLATED_CI_FIX
        for kind in ci_decline.KINDS:
            self.assertIn(f"--decline {kind}`", " ".join(text.split()))
        self.assertNotIn("do not push: say so in your summary", text)
        self.assertNotIn("do not push: say in your summary", " ".join(text.split()))
        check = prompts.fixer_check_section({"fixer_check": "make check"}, ci_fix=True)
        self.assertIn("don't publish: decline it (step 2 above)", check)


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the borrowed harness's classes run in test_fixer_answers."""
    suite = unittest.TestSuite()
    for cls in (Decline, Client, Reports):
        for name in sorted(vars(cls)):
            if name.startswith("test_"):
                suite.addTest(cls(name))
    return suite


if __name__ == "__main__":
    unittest.main()
