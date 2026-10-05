#!/usr/bin/env python3
"""#197: what the loop posts is signed as the loop's — and only what it posts, and only when on.

Every write the plugin sends (the receipted review, ``broker.perform``, the fixer's answers
comment, the ruling comment, the fixer's commit) carries "Automated by hermes-review-loop": a body
footer, or an ``Automated-By`` commit trailer. It is added on the host, at the write, so a seat
cannot strip or pre-empt it; ``attribution: false`` turns both off. The loop config, the settings
form, ``init`` and ``set`` carry the switch.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
from review_loop import attribution, broker, config, gh, review_receipt, routes  # noqa: E402

HEAD = "a" * 40
BASE = "b" * 40
REPO = "acme/widgets"
LINK = "[hermes-review-loop](https://github.com/coe0718/hermes-review-loop)"
TRAILER = "Automated-By: hermes-review-loop (https://github.com/coe0718/hermes-review-loop)"


def loop(**extra) -> dict:
    return {"id": "widgets", "repo": REPO, "seats": {"reviewer": {"agent": "Critic"},
                                                     "fixer": {"agent": "Coder"}}, **extra}


class Footer(unittest.TestCase):
    def test_on_by_default_and_names_the_seat_its_agent_and_the_head(self):
        signed = attribution.stamp(loop(), "Looks good.", seat="reviewer", head=HEAD)
        self.assertEqual(signed, "Looks good.\n\n---\n<sub>🤖 Automated by " + LINK +
                         " · reviewer seat (Critic) · head `aaaaaaa`</sub>")
        fixer = attribution.stamp(loop(), "Done.", seat="fixer", head=HEAD)
        self.assertIn("fixer seat (Coder)", fixer)
        ruling = attribution.stamp(loop(), "Ruled.", seat="adjudicator", head=HEAD)
        self.assertIn("· adjudicator · head", ruling)

    def test_off_leaves_the_body_untouched(self):
        self.assertEqual(attribution.stamp(loop(attribution=False), "x", seat="reviewer",
                                           head=HEAD), "x")
        self.assertFalse(attribution.enabled(loop(attribution=False)))
        self.assertTrue(attribution.enabled(loop()))

    def test_a_seat_cannot_pre_empt_the_footer_with_its_own(self):
        # A body already ending in footer-shaped text still gets the real one: the host never
        # inspects the seat's text to decide whether to sign.
        fake = "ok\n\n---\n<sub>🤖 Automated by somebody else</sub>"
        signed = attribution.stamp(loop(), fake, seat="reviewer", head=HEAD)
        self.assertTrue(signed.startswith(fake))
        self.assertTrue(signed.endswith("· reviewer seat (Critic) · head `aaaaaaa`</sub>"))
        self.assertEqual(signed.count(LINK), 1)

    def test_an_agent_name_cannot_inject_markup_or_a_link(self):
        for name in ("Critic](https://evil.example)<script>`x`", "www.evil.example",
                     "Critic\n\n# heading", "a|b*c_d"):
            with self.subTest(name=name):
                hostile = loop(seats={"reviewer": {"agent": name}})
                footer = attribution.stamp(hostile, "ok", seat="reviewer",
                                           head=HEAD).rsplit("\n", 1)[1]
                self.assertEqual(footer.count("://"), 1)      # only the repository link
                self.assertEqual(footer.count("]("), 1)
                for bad in ("www.", "<script", "`x`", "\n", "#", "|", "*", "_d"):
                    self.assertNotIn(bad, footer)

    def test_a_body_that_would_exceed_githubs_limit_is_refused_not_cut(self):
        body = "x" * (attribution.GITHUB_BODY_MAX - 10)
        with self.assertRaises(attribution.AttributionError):
            attribution.stamp(loop(), body, seat="reviewer", head=HEAD)
        # Off sends what GitHub would accept anyway: nothing is added, nothing refused here.
        self.assertEqual(attribution.stamp(loop(attribution=False), body, seat="reviewer",
                                           head=HEAD), body)


class Unsign(unittest.TestCase):
    def test_the_history_a_seat_reads_drops_exactly_our_footer(self):
        signed = attribution.stamp(loop(), "Please fix X.", seat="reviewer", head=HEAD)
        self.assertEqual(attribution.unsign(signed), "Please fix X.")
        # Anything else is left alone: a look-alike, a footer mid-body, a non-string.
        for body in ("Please fix X.\n\n---\n<sub>🤖 Automated by somebody else</sub>",
                     signed + "\n\nand then more text", None):
            self.assertEqual(attribution.unsign(body), body)

    def test_the_review_record_shows_the_words_not_the_label(self):
        from review_loop import run_supervisor
        lp = {"reviewers": ["rev"], "fixers": ["fix"], "seats": {"fixer": {"login": "fix"}}}
        review = {"user": {"login": "rev"}, "state": "CHANGES_REQUESTED", "commit_id": BASE,
                  "submitted_at": "2026-10-01T00:00:00Z",
                  "body": attribution.stamp(loop(), "Fix the parser.", seat="reviewer",
                                            head=BASE)}
        answers = broker.answers_comment_body("Fixed it.", head=HEAD, base=BASE, run_id="r1")
        comment = {"user": {"login": "fix"}, "created_at": "2026-10-01T01:00:00Z",
                   "body": attribution.stamp(loop(), answers, seat="fixer", head=HEAD)}
        with mock.patch("review_loop.gate.is_reviewer", return_value=True):
            record = run_supervisor.pr_record(lp, None, [review], [comment])
        self.assertIn("Fix the parser.", record)
        self.assertIn("Fixed it.", record)
        self.assertNotIn("Automated by", record)


class CommitTrailer(unittest.TestCase):
    def test_a_plain_message_gets_a_trailer_block(self):
        self.assertEqual(attribution.sign_commit(loop(), "Fix the parser"),
                         "Fix the parser\n\n" + TRAILER)
        self.assertEqual(attribution.sign_commit(loop(), "Fix it\n\nWhy: the body.\n"),
                         "Fix it\n\nWhy: the body.\n" + TRAILER)

    def test_it_joins_an_existing_trailer_block(self):
        message = "Fix it\n\nCo-Authored-By: Coder <coder@example.com>"
        self.assertEqual(attribution.sign_commit(loop(), message), message + "\n" + TRAILER)

    def test_a_subject_line_alone_is_not_a_trailer_block(self):
        self.assertEqual(attribution.sign_commit(loop(), "Fix: the parser"),
                         "Fix: the parser\n\n" + TRAILER)

    def test_off_leaves_the_message_untouched(self):
        self.assertEqual(attribution.sign_commit(loop(attribution=False), "Fix it"), "Fix it")


class WritePaths(unittest.TestCase):
    """Each broker write is signed exactly once on the wire, and not at all when off."""

    def setUp(self):
        self.posted: list = []

        def api(lp, path, method="GET", body=None, login=None):
            if method == "POST":
                self.posted.append((path, body))
                return {"id": 9}
            return {}
        patcher = mock.patch.object(gh, "api", side_effect=api)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("_audit",):
            p = mock.patch.object(broker, name)
            p.start()
            self.addCleanup(p.stop)

    def body(self) -> str:
        self.assertEqual(len(self.posted), 1)
        return self.posted[0][1]["body"]

    def test_perform_review(self):
        with mock.patch.object(broker, "authorize", return_value="rev"):
            broker.perform(loop(), repo=REPO, number=7, head=HEAD, role="reviewer",
                           branch="fix-7", operation="review", verdict="APPROVE", body="LGTM")
        body = self.body()
        self.assertTrue(body.startswith("LGTM\n\n---\n"))
        self.assertEqual(body.count(LINK), 1)
        self.assertIn("reviewer seat (Critic)", body)

    def test_perform_request_review_has_no_body_to_sign(self):
        lp = {**loop(), "reviewer_seat": "rev", "seats": {"reviewer": {"login": "rev"}}}
        with mock.patch.object(broker, "authorize", return_value="fix"):
            broker.perform(lp, repo=REPO, number=7, head=HEAD, role="fixer", branch="fix-7",
                           operation="request_review")
        self.assertEqual(self.posted[0][1], {"reviewers": ["rev"]})

    def test_fixer_answers_comment(self):
        text = broker.answers_comment_body("Fixed both.", head=HEAD, base=BASE, run_id="r1")
        broker.post_fixer_answers(loop(), repo=REPO, number=7, head=HEAD, branch="fix-7",
                                  login="fix", text=text)
        body = self.body()
        self.assertTrue(body.startswith(text.rstrip()))
        self.assertIn("fixer seat (Coder)", body)
        self.assertEqual(body.count(LINK), 1)
        # The answers record still parses: the marker is first, the footer after the text.
        parsed = broker.parse_answers_comment(
            {"user": {"login": "fix"}, "body": body, "created_at": "2026-10-01T00:00:00Z"},
            {"seats": {"fixer": {"login": "fix"}}})
        self.assertEqual((parsed["run"], parsed["head"], parsed["base"]), ("r1", HEAD, BASE))

    def test_ruling_comment(self):
        text = broker.ruling_comment_body("merge", "It's fine.", head=HEAD, turn_key="breach:3",
                                          run_id="r2", cap=3)
        broker.post_ruling_comment(loop(), repo=REPO, number=7, head=HEAD, branch="fix-7",
                                   login="adj", text=text)
        body = self.body()
        self.assertIn("· adjudicator · head `aaaaaaa`", body)
        self.assertEqual(body.count(LINK), 1)

    def test_off_signs_none_of_them(self):
        off = loop(attribution=False)
        with mock.patch.object(broker, "authorize", return_value="rev"):
            broker.perform(off, repo=REPO, number=7, head=HEAD, role="reviewer",
                           branch="fix-7", operation="review", verdict="APPROVE", body="LGTM")
        broker.post_fixer_answers(off, repo=REPO, number=7, head=HEAD, branch="fix-7",
                                  login="fix", text="answers")
        broker.post_ruling_comment(off, repo=REPO, number=7, head=HEAD, branch="fix-7",
                                   login="adj", text="ruling")
        self.assertEqual([body["body"] for _, body in self.posted], ["LGTM", "answers", "ruling"])

    def test_an_unsignable_body_is_refused_before_anything_is_sent(self):
        with mock.patch.object(attribution, "GITHUB_BODY_MAX", 20), \
                self.assertRaises(broker.BrokerDenied):
            broker.post_ruling_comment(loop(), repo=REPO, number=7, head=HEAD, branch="fix-7",
                                       login="adj", text="a ruling that is long")
        self.assertEqual(self.posted, [])


class ReceiptedReview(unittest.TestCase):
    """The production reviewer write: signed before the durable claim; the readback still holds."""

    def setUp(self):
        self.scope = mock.Mock(repo=REPO, number=7, head=HEAD, branch="fix-7")
        self.ledger = mock.Mock(generation="g1")
        self.posted: list = []

        def api(lp, path, method="GET", body=None, login=None):
            if path == "/user":
                return {"id": 5, "login": "rev"}
            if method == "POST":
                self.posted.append(body)
                return {"id": 77}
            if path.endswith("/reviews/77"):
                return {"id": 77, "state": "APPROVED", "commit_id": HEAD,
                        "user": {"id": 5, "login": "rev"}}
            return {"number": 7}
        for target, kwargs in ((gh, {"side_effect": api}),):
            p = mock.patch.object(target, "api", **kwargs)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (("authorize", "rev"), ("_audit", None)):
            p = mock.patch.object(broker, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(review_receipt, "generation_for", return_value="g1")
        p.start()
        self.addCleanup(p.stop)

    def test_the_review_is_signed_and_its_readback_still_confirms(self):
        result = review_receipt.submit({**loop(), "read_token": "reader"}, self.scope,
                                       self.ledger, "APPROVE", "Ship it.")
        self.assertEqual(result, {"id": 77})
        self.assertTrue(self.posted[0]["body"].startswith("Ship it.\n\n---\n"))
        self.assertIn("reviewer seat (Critic)", self.posted[0]["body"])
        self.ledger.claim.assert_called_once()
        self.ledger.confirm.assert_called_once_with(77, "APPROVED", 5)

    def test_an_unsignable_review_is_never_claimed_or_sent(self):
        with mock.patch.object(attribution, "GITHUB_BODY_MAX", 20), \
                self.assertRaises(review_receipt.ReceiptDenied):
            review_receipt.submit({**loop(), "read_token": "reader"}, self.scope, self.ledger,
                                  "APPROVE", "a review that is long")
        self.ledger.claim.assert_not_called()
        self.assertEqual(self.posted, [])

    def test_off_posts_the_seats_words_only(self):
        review_receipt.submit({**loop(attribution=False), "read_token": "reader"}, self.scope,
                              self.ledger, "APPROVE", "Ship it.")
        self.assertEqual(self.posted[0]["body"], "Ship it.")


class Setting(unittest.TestCase):
    def raw(self, **extra) -> dict:
        return {"repo": REPO, "fixers": ["fix"], "reviewers": ["rev"], "read_token": "reader",
                "tokens": {}, "seats": {"reviewer": {"profile": "r", "login": "rev"},
                                        "fixer": {"profile": "f", "login": "fix"}}, **extra}

    def test_the_loop_default_is_on_and_only_a_boolean_is_accepted(self):
        self.assertIs(config.DEFAULTS["attribution"], True)
        for bad in ("off", 0, None, "false"):
            with self.subTest(value=bad), self.assertRaises(config.ConfigError) as caught:
                config.normalize(self.raw(attribution=bad))
            self.assertIn("'attribution' must be a JSON boolean", str(caught.exception))

    def test_the_form_moves_it_only_when_it_names_it(self):
        self.assertNotIn("attribution", config.apply_settings(self.raw(), {}))
        self.assertIs(config.apply_settings(self.raw(attribution=False), {})["attribution"], False)
        self.assertIs(config.apply_settings(self.raw(), {"attribution": False})["attribution"],
                      False)
        self.assertIs(config.apply_settings(self.raw(attribution=False),
                                            {"attribution": "true"})["attribution"], True)

    def test_form_words_and_junk(self):
        defaults = config.settings_defaults
        self.assertIs(defaults({})["attribution"], True)
        for word, want in (("off", False), ("no", False), ("0", False), (False, False),
                           ("on", True), ("yes", True), (True, True), ("garbage", True)):
            self.assertIs(defaults({"attribution": word})["attribution"], want, word)


LOOP_ID = "signed"
LOOP_FILE = t.LOOPS_DIR / f"{LOOP_ID}.json"


class Cli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        t.HOST = t.start_sink()
        t.DATA["host"] = t.HOST
        cls._env = dict(os.environ)
        os.environ.update(t.env())

    @classmethod
    def tearDownClass(cls):
        os.environ.clear()
        os.environ.update(cls._env)

    def setUp(self):
        t.reset(prs={})
        LOOP_FILE.unlink(missing_ok=True)
        self.addCleanup(LOOP_FILE.unlink, missing_ok=True)

    def cli(self, *argv, settings=None) -> tuple[int, str]:
        return t.run_cli(t.parser_for(settings).parse_args(list(argv)))

    def init(self, *extra, settings=None) -> tuple[int, str]:
        return self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile",
                        "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS, *extra,
                        settings=settings)

    def written(self) -> dict:
        return json.loads(LOOP_FILE.read_text())

    def test_init_signs_by_default_and_honours_off_and_the_form(self):
        rc, out = self.init()
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written()["attribution"], True)
        LOOP_FILE.unlink()
        rc, out = self.init("--attribution", "off")
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written()["attribution"], False)
        LOOP_FILE.unlink()
        rc, out = self.init(settings={"attribution": False})
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written()["attribution"], False)

    def test_set_turns_it_off_and_on_and_status_and_doctor_say_which(self):
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("set", "--loop", LOOP_ID, "--attribution", "off")
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written()["attribution"], False)
        rc, out = self.cli("status", "--loop", LOOP_ID)
        self.assertIn("signed:     off", out)
        rc, out = self.cli("doctor", "--loop", LOOP_ID, "--offline")
        self.assertRegex(out, r"✅ attribution +off")
        rc, out = self.cli("set", "--loop", LOOP_ID, "--attribution", "on")
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written()["attribution"], True)
        rc, out = self.cli("status", "--loop", LOOP_ID)
        self.assertIn("signed:     on", out)

    def test_apply_saves_a_form_change_to_attribution_alone(self):
        """The form's switch is a change apply must see, not "already matches"."""
        self.assertEqual(self.init()[0], 0)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"attribution": False})
        self.assertEqual(rc, 0, out)
        self.assertNotIn("already matches", out)
        self.assertIs(self.written()["attribution"], False)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"attribution": False})
        self.assertIn("already matches", out)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={"attribution": True})
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written()["attribution"], True)

    def test_apply_saves_each_turn_knob_alone_and_refuses_out_of_range(self):
        """#309: each new form knob alone is a change, then matches; bad values are refused."""
        self.assertEqual(self.init()[0], 0)
        (config.profiles_root() / "arbiter").mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: routes.restore_entries({f"{LOOP_ID}-triage": None}))
        rc, out = self.cli("triage", "--loop", LOOP_ID, "--enable", "--triage-profile", "arbiter",
                           "--author", "Owner", "--labels", "bug,docs", "--fix-label", "hermes-fix",
                           "--maintainer", "boss")
        self.assertEqual(rc, 0, out)
        for key, read, value in (
                ("reviewer_max_steps", lambda w: w["seats"]["reviewer"]["max_steps"], "120"),
                ("fixer_max_steps", lambda w: w["seats"]["fixer"]["max_steps"], 150),
                ("fix_daily_turns", lambda w: w["triage"]["fix_daily_turns"], "7")):
            rc, out = self.cli("apply", "--loop", LOOP_ID, settings={key: value})
            self.assertEqual(rc, 0, out)
            self.assertNotIn("already matches", out, key)
            self.assertEqual(read(self.written()), int(value), key)
            rc, out = self.cli("apply", "--loop", LOOP_ID, settings={key: value})
            self.assertIn("already matches", out, key)
        before = self.written()
        for key, bad, msg in (("reviewer_max_steps", "7", "8-200"),
                              ("fixer_max_steps", "201", "8-200"),
                              ("fixer_max_steps", "abc", "8-200"),
                              ("fix_daily_turns", "0", "1-1000"),
                              ("fix_daily_turns", "1001", "1-1000")):
            rc, out = self.cli("apply", "--loop", LOOP_ID, settings={key: bad})
            self.assertEqual(rc, 2, out)
            self.assertIn(msg, out)
        self.assertEqual(self.written(), before)
        rc, out = self.cli("apply", "--loop", LOOP_ID, settings={})   # blank: not set here
        self.assertIn("already matches", out)
        self.assertEqual(self.written(), before)


if __name__ == "__main__":
    unittest.main()
