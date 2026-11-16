"""#2496: with ``review_only_rerequest_grants`` on, a maintainer's GitHub re-request of the
reviewer at a review-only PR's cap grants another round, as ``review --another-round`` does.

The gate runs end to end (GitHub, the worker spawn and the observer mocked, as in
``test_review_only.Gates``): one more verdict per head and verdict count, never for a head that
already has a verdict, only from a maintainer and only on ``review_requested``. Each grant is an
urgent ``granted`` notice, through the real observer: sent first, and the grant is written only
once the ledger records it delivered, so no review starts unannounced (a failed, uncertain or
unfinished POST grants nothing until a later pass finds it delivered, and a gate stopped after the
receipt leaves the grant to the next pass), never held for a digest, and no grant is made where
the feed can't send it. Off, the cap declines as before. Then the setting's every path:
the loop file, the form, ``init``, ``set``, ``setup``, ``show`` and ``doctor``.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import run_tests as t  # noqa: E402
import test_doctor_environment  # noqa: E402
import test_fixer_gating as fg  # noqa: E402
import test_review_only as tro  # noqa: E402
from diaktoros import config, doctor, observer, review_asks, routes, run_supervisor, show  # noqa: E402
from diaktoros.cli import setup_command as cli_setup  # noqa: E402
from diaktoros.config import settings_form  # noqa: E402
from diaktoros.doctor import hook_checks, identity_checks  # noqa: E402
from scripts import gate_reviewer  # noqa: E402

HEAD = fg.HEAD
FEED = {"route": "one-observe"}
OLD = "c" * 40
OWNER = tro.OWNER
MAINT = "maint-human"
KEY = "review_only_rerequest_grants"


def triage(*maintainers: str) -> dict:
    return {"route": "one-triage", "profile": "triage", "authors": ["someone"], "labels": ["bug"],
            "maintainers": list(maintainers)}


class Gate(fg.Base):
    """The reviewer gate at a review-only PR's cap, the setting on unless a test turns it off."""

    verdicts = tro.Gates.verdicts
    explain = tro.Gates.explain

    def setUp(self):
        super().setUp()
        # A maintainer, as config.maintainers reads one: an operator agent (#757) counts too.
        # And a feed for the grant's notice (#2501 review, F3), on a route ``feed`` registers (F5).
        self.write(review_only=[OWNER], operator_agents=[MAINT], observer=FEED, **{KEY: True})
        self.live = {**fg.LIVE, "user": {"login": OWNER}}
        self.cap = self.loop["cap"]
        self.feed()

    def feed(self, **observer_block):
        """An observer feed on a registered delivery-only route; returns the mocked POST."""
        subs = self.root / "subscriptions.json"
        env = mock.patch.dict(os.environ, {"DIAKTOROS_SUBS": str(subs)})
        env.start()
        self.addCleanup(env.stop)
        self.write(host="https://owner.example", observer={"route": "one-observe", **observer_block})
        subs.write_text(json.dumps({"one-observe": {"host": "https://owner.example", "secret": "s",
                                                    **observer.route_contract(self.loop)}}))
        fire = mock.patch.object(routes, "fire", return_value=True)
        self.addCleanup(fire.stop)
        return fire.start()

    @staticmethod
    def sent(fire) -> list:
        """(event, message) of each notice POSTed."""
        return [(c.args[2]["_observer"]["event"], c.args[2]["_observer"]["message"])
                for c in fire.call_args_list]

    def events(self, fire, only: str = "") -> list:
        """The events POSTed, or only those named ``only``."""
        return [event for event, _ in self.sent(fire) if not only or event == only]

    def write(self, **extra):
        path = config.config_dir() / "one.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), **extra}))
        self.loop = config.load_id("one")

    def at_head(self, count: int) -> list:
        """``count`` verdicts in all, the last of them at this head."""
        return self.verdicts(count - 1, OLD) + [fg.verdict(5 + count) | {"commit_id": HEAD}]

    def ask(self, sender: str, reviews=None, action: str = "review_requested", notes=None,
            enqueue: str = "", ends=SystemExit):
        """Run the gate on ``action`` from ``sender``; by default the cap's verdicts are all on an
        older head. Returns the mocked ``block_pr_agent`` (it ends the gate, as a claim would).
        With ``enqueue``, the real ``block_pr_agent`` runs and the ledger answers ``enqueue``,
        and with ``notes=False`` the real observer sends (``feed``). ``ends``: the exception the
        gate ends with (``silence``'s SystemExit, else a simulated termination)."""
        payload = {"action": action, "number": 7, "pull_request": self.live,
                   "repository": {"full_name": fg.REPO}, "sender": {"login": sender},
                   "requested_reviewer": {"login": "reviewer"}}
        reviews = self.verdicts(self.cap, OLD) if reviews is None else reviews
        with mock.patch.object(gate_reviewer.sys, "stdin", io.StringIO(json.dumps(payload))), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             mock.patch.object(gate_reviewer.gh, "pr", return_value=self.live), \
             mock.patch.object(gate_reviewer.gate, "fetch_reviews", return_value=reviews), \
             mock.patch.object(gate_reviewer.gate, "drain_seat"), \
             mock.patch.object(gate_reviewer.gate, "enqueue_isolated", return_value=enqueue), \
             mock.patch("diaktoros.merge_check.mark_pending", return_value=False), \
             (contextlib.nullcontext(mock.Mock()) if enqueue else mock.patch.object(
                 gate_reviewer.gate, "block_pr_agent",
                 side_effect=lambda *a, **kw: gate_reviewer.silence())) as block, \
             (contextlib.nullcontext() if notes is False else mock.patch.object(
                 gate_reviewer.observer, "notify",
                 side_effect=lambda *a, **kw: (notes if notes is not None
                                               else []).append((a[2], kw)))), \
             contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(ends):
                gate_reviewer.main()
        self.said = err.getvalue()
        if ends is SystemExit:
            self.assertEqual(out.getvalue().strip(), "[SILENT]")
        return block

    def handoff(self, block) -> mock.Mock:
        """The notice the queued review's ``on_queued`` sends."""
        with mock.patch.object(gate_reviewer.observer, "notify") as notify:
            block.call_args.kwargs["on_queued"]()
        return notify

    def test_a_maintainers_rerequest_at_the_cap_grants_one_review_and_says_who(self):
        # A digest is set: the grant still goes out at once, with its head and who granted it,
        # and only the ordinary hand-off waits for the digest (#2501, F2).
        fire = self.feed(digest_min=30)
        block = self.ask(MAINT, notes=False)
        block.assert_called_once()
        self.assertEqual(block.call_args.args[2:5], ("reviewer", 7, HEAD))
        self.assertTrue(self.st.review_cap_granted(7, HEAD, self.cap))
        [(event, text)] = self.sent(fire)
        self.assertEqual(event, "granted")
        self.assertIn(f"#7 `{HEAD[:7]}` another round granted — {MAINT}'s re-request on GitHub",
                      text)
        self.assertIn(f"one more verdict at this head, {self.cap} spent ({MAINT})", text)
        with contextlib.redirect_stderr(io.StringIO()):
            block.call_args.kwargs["on_queued"]()
        self.assertEqual(len(self.sent(fire)), 1)
        self.assertEqual(observer.owed(self.st).get("queued"), 1)
        # That review's verdict lands at this head: the head is reviewed, so nothing more.
        self.assertIsNone(self.ask(MAINT, self.at_head(self.cap + 1), notes=False).call_args)
        self.assertFalse(self.st.review_cap_granted(7, HEAD, self.cap + 1))
        self.assertEqual(len(self.sent(fire)), 1)

    def test_the_grant_is_announced_when_another_delivery_queues_its_review(self):
        # #2501 F1: two deliveries of the request interleave. The first writes the grant and its
        # enqueue finds the turn already there ("duplicate running"), so block_pr_agent drops
        # its on_queued; the second finds the grant and queues the review with an ordinary
        # hand-off. The grant's notice came from the first, before its enqueue.
        fire = self.feed()
        self.ask(MAINT, notes=False, enqueue="duplicate running")
        self.ask(MAINT, notes=False, enqueue="enqueued")
        self.assertEqual([event for event, _ in self.sent(fire)], ["granted", "handoff"])
        self.assertIn(f"another round granted — {MAINT}'s re-request", self.sent(fire)[0][1])
        self.assertNotIn("granted", self.sent(fire)[1][1])

    def test_a_repeated_rerequest_at_the_same_head_and_count_grants_nothing_more(self):
        fire = self.feed()
        self.ask(MAINT, notes=False)
        entry = self.st.review_cap_entry(7)
        block = self.ask(MAINT, notes=False)
        self.assertEqual(self.st.review_cap_entry(7), entry)
        # The first grant still covers this head's next verdict, as --another-round's would, but
        # this request granted nothing: still the one notice, and its hand-off grants nothing.
        block.assert_called_once()
        self.assertEqual([event for event, _ in self.sent(fire)], ["granted"])
        kw = self.handoff(block).call_args.kwargs
        self.assertEqual((kw["identity"], kw.get("outcome", "")), ("review_requested", ""))

    def test_a_gate_stopped_between_the_notice_and_the_grant_leaves_it_to_the_next_pass(self):
        # #2501 review, F4: the notice went out and the gate stopped before writing the grant.
        # The next pass (the redelivery, or any other event at the head) finds the notice
        # delivered and writes the grant without sending it again; a third sends nothing more.
        fire = self.feed()

        class Killed(BaseException):
            pass

        with mock.patch.object(self.st.__class__, "review_cap_grant", side_effect=Killed):
            self.ask(MAINT, notes=False, enqueue="enqueued", ends=Killed)
        self.assertFalse(self.st.review_cap_granted(7, HEAD, self.cap))
        self.assertEqual(self.events(fire), ["granted"])
        self.ask(OWNER, notes=False, enqueue="enqueued", action="ready_for_review")
        self.assertTrue(self.st.review_cap_granted(7, HEAD, self.cap))
        # Queued with that event's own hand-off (`hermes dk review`'s "opened"), not a second grant.
        self.assertEqual(self.events(fire), ["granted", "opened"])
        self.ask(MAINT, notes=False, enqueue="duplicate running")
        self.assertEqual(len(self.events(fire)), 2)

    def test_no_review_starts_until_the_grants_notice_is_delivered(self):
        # #2501 review, F5: a feed that passed the preflight can still fail at the POST. Nothing
        # is granted or queued then, and the gate's decision says what the notice is waiting on.
        fire = self.feed()
        # A definite failure (here the route gone by the POST): the sweep resends it, and the
        # re-request after that grants, with no second notice.
        with mock.patch.object(observer, "_post", return_value=(False, "route gone", False)):
            self.assertIsNone(self.ask(MAINT, notes=False).call_args)
        self.assertFalse(self.st.review_cap_granted(7, HEAD, self.cap))
        self.assertEqual(self.ledger_rows(), [])
        self.assertIn("waits for its granted notice, failed: the watchdog sweep resends it "
                      "(route gone); re-request again once it is delivered", self.said)
        self.assertIsNone(self.ask(MAINT, notes=False).call_args)     # still failed: still waits
        self.assertEqual(self.events(fire, "granted"), [])
        with contextlib.redirect_stderr(io.StringIO()):
            observer.retry(self.loop, self.st)
        self.assertEqual(self.events(fire, "granted"), ["granted"])
        self.ask(MAINT, notes=False).assert_called_once()
        self.assertTrue(self.st.review_cap_granted(7, HEAD, self.cap))
        self.assertEqual(len(self.events(fire, "granted")), 1)

    def test_an_uncertain_notice_is_never_replayed_and_grants_once_settled(self):
        fire = self.feed()
        key = observer.key_for(self.loop, 7, HEAD, "granted", f"rerequest:{self.cap}")
        fire.return_value = False                     # no 2xx: the POST may have landed
        self.assertIsNone(self.ask(MAINT, notes=False).call_args)
        self.assertEqual(observer.recorded(self.st, key)["status"], "uncertain")
        self.assertIn(f"granted notice, uncertain: check the chat, then `hermes dk observer --loop "
                      f"one --settle {key} --delivered`", self.said)
        fire.return_value = True
        with contextlib.redirect_stderr(io.StringIO()):
            observer.retry(self.loop, self.st)
        # Neither the sweep nor the gate replays it.
        self.assertIsNone(self.ask(MAINT, notes=False).call_args)
        self.assertEqual(len(self.events(fire, "granted")), 1)
        observer.settle(self.st, key, delivered=True)
        self.ask(MAINT, notes=False).assert_called_once()
        self.assertTrue(self.st.review_cap_granted(7, HEAD, self.cap))
        self.assertEqual(len(self.events(fire, "granted")), 1)

    def test_a_notice_still_being_sent_grants_nothing_yet(self):
        # Another delivery's POST of the notice is in flight (or its sender stopped mid-way).
        fire = self.feed()
        with mock.patch.object(observer, "_deliver", return_value=False):
            self.assertIsNone(self.ask(MAINT, notes=False).call_args)
        self.assertIsNone(self.ask(MAINT, notes=False).call_args)
        self.assertIn("granted notice, pending: it is being sent, or its sender stopped",
                      self.said)
        self.assertFalse(self.st.review_cap_granted(7, HEAD, self.cap))
        self.assertEqual(self.events(fire, "granted"), [])

    def test_another_rounds_grant_sends_no_rerequest_notice(self):
        # The terminal grant names no grantor: the operator ran it, and nothing claims a re-request.
        fire = self.feed()
        self.assertTrue(self.st.review_cap_grant(7, HEAD, self.cap))
        self.ask(OWNER, notes=False, enqueue="enqueued")
        self.assertEqual([event for event, _ in self.sent(fire)], ["handoff"])

    def test_no_grant_where_its_notice_could_not_be_sent(self):
        # #2501 review, F3: no feed, a muted one, or one whose events leave out the new
        # ``granted`` (an existing explicit list): the re-request grants nothing, and nothing runs.
        for name, block_ in (("no feed", None), ("muted", {**FEED, "mute": True}),
                             ("filtered", {**FEED, "events": ["held", "verdict"]}),
                             ("unroutable", {"route": "not-registered"})):  # F5
            with self.subTest(name):
                self.setUp()
                fire = self.feed(**(block_ or {}))
                if block_ is None:
                    self.write(observer={})
                self.assertIsNone(self.ask(MAINT, notes=False).call_args)
                self.assertFalse(self.st.review_cap_granted(7, HEAD, self.cap))
                self.assertNotIn("granted", [event for event, _ in self.sent(fire)])
                self.assertNotIn("re-request", self.explain()["next"]["action"])

    def test_off_a_maintainers_rerequest_at_the_cap_is_declined(self):
        self.write(**{KEY: False})
        self.assertIsNone(self.ask(MAINT).call_args)
        self.assertFalse(self.st.review_cap_granted(7, HEAD, self.cap))
        self.assertEqual(self.ledger_rows(), [])

    def test_only_a_maintainers_review_request_grants(self):
        for sender, action in ((OWNER, "review_requested"),     # the author: still no bypass
                               (MAINT, "ready_for_review"),     # `hermes dk review`'s event
                               (MAINT, "synchronize")):
            with self.subTest(sender=sender, action=action):
                self.assertIsNone(self.ask(sender, action=action).call_args)
                self.assertFalse(self.st.review_cap_granted(7, HEAD, self.cap))

    def test_an_author_who_is_also_the_maintainer_grants(self):
        self.feed()
        self.write(operator_agents=[OWNER])
        self.ask(OWNER, notes=False).assert_called_once()
        self.assertTrue(self.st.review_cap_granted(7, HEAD, self.cap))

    def test_a_head_that_already_has_a_verdict_gets_no_grant(self):
        self.assertIsNone(self.ask(MAINT, self.at_head(self.cap)).call_args)
        self.assertFalse(self.st.review_cap_granted(7, HEAD, self.cap))
        # Not even when a dispute at that head lets a maintainer's request past "already has a
        # reviewer's verdict" (#894) and on to the cap: a grant there would buy nothing.
        with mock.patch.object(run_supervisor.views, "disputed", return_value=1):
            self.assertIsNone(self.ask(MAINT, self.at_head(self.cap)).call_args)
        self.assertFalse(self.st.review_cap_granted(7, HEAD, self.cap))

    def test_the_cap_notice_and_explain_name_the_rerequest_only_when_on(self):
        notes = []
        self.ask(OWNER, notes=notes)
        held = [kw["outcome"] for _, kw in notes if "review cap reached" in kw.get("outcome", "")]
        self.assertEqual(len(held), 1, notes)
        self.assertIn("--another-round` or re-request the reviewer on GitHub", held[0])
        self.assertIn("re-requests the reviewer on GitHub", self.explain()["next"]["action"])
        self.assertIn("(or their re-request on GitHub)",
                      self.explain(self.cap - 1)["next"]["action"])
        self.write(**{KEY: False})
        self.assertNotIn("re-request", self.explain()["next"]["action"])

    def test_with_a_github_app_reviewer_neither_offers_a_rerequest(self):
        # GitHub can't request an App as a reviewer, so nothing offers what can't happen.
        notes = []
        with mock.patch.object(config, "reviewer_is_app", return_value=True):
            self.ask(OWNER, notes=notes)
            for action in (self.explain()["next"]["action"],
                           self.explain(self.cap - 1)["next"]["action"]):
                self.assertNotIn("re-request on GitHub", action)
                self.assertNotIn("re-requests the reviewer on GitHub", action)
        held = [kw["outcome"] for _, kw in notes if "review cap reached" in kw.get("outcome", "")]
        self.assertEqual(len(held), 1, notes)
        self.assertNotIn("re-request", held[0])


def routed(case: unittest.TestCase, loop: dict) -> dict:
    """``loop`` with a webhook host and its feed's route registered as a delivery-only one, so
    ``observer.unusable`` passes (#2501 review, F5)."""
    loop = {**loop, "host": "https://owner.example"}
    temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
    case.addCleanup(temp.cleanup)
    subs = pathlib.Path(temp.name) / "subscriptions.json"
    subs.write_text(json.dumps({"one-observe": {"host": loop["host"], "secret": "s",
                                                **observer.route_contract(loop)}}))
    env = mock.patch.dict(os.environ, {"DIAKTOROS_SUBS": str(subs)})
    env.start()
    case.addCleanup(env.stop)
    return loop


def loop_with(**extra) -> dict:
    return {"id": "w", "reviewer_seat": "rev", "review_only": [OWNER],
            "triage": triage(MAINT), "observer": FEED, KEY: True, **extra}


class Doctor(unittest.TestCase):
    def check(self, **extra):
        return identity_checks.check_review_only_rerequest_grants(routed(self, loop_with(**extra)))

    def test_off_says_nothing(self):
        self.assertIsNone(self.check(**{KEY: False}))
        self.assertIsNone(identity_checks.check_review_only_rerequest_grants({"id": "w"}))

    def test_on_names_who_can_grant_and_that_a_token_can_too(self):
        got = self.check(operator_agents=["docs-agent"])
        self.assertEqual(got.status, doctor.VERIFIED)
        self.assertIn(f"re-request of rev by docs-agent, {MAINT}", got.detail)
        self.assertIn("anything holding one of these logins' tokens", got.detail)

    def test_on_but_inert_says_why(self):
        for extra, said in (({"apps": {"rev": {"app_id": 1}}}, "is a GitHub App"),
                            ({"review_only": []}, "no review-only author"),
                            ({"triage": {}}, "no maintainer"),
                            ({"observer": {}}, "no observer configured"),
                            ({"observer": {**FEED, "mute": True}}, "muted"),
                            ({"observer": {"route": "elsewhere"}},
                             "missing from the gateway's subscriptions"),
                            ({"observer": {**FEED, "events": ["held"]}},
                             "observer.events leaves out granted")):
            with self.subTest(said):
                got = self.check(**extra)
                self.assertEqual(got.status, doctor.MISMATCH)
                self.assertIn(said, got.detail)
                self.assertIn("--review-only-rerequest-grants off", got.fix)


class Preflight(test_doctor_environment.Environment):
    """The check reaches ``check_loop``'s report (what ``hermes dk doctor`` prints)."""

    def test_the_loop_preflight_says_who_can_grant(self):
        self.assertNotIn("rerequest-grants", self.run_check())
        reach = mock.patch.object(hook_checks, "gateway_reachable", return_value=(True, ""))
        reach.start()
        self.addCleanup(reach.stop)
        self.loop.update(routed(self, {**self.loop, KEY: True, "review_only": [OWNER],
                                       "triage": triage(MAINT), "observer": FEED}))
        got = self.run_check()["rerequest-grants"]
        self.assertEqual(got.status, doctor.VERIFIED)
        self.assertIn(MAINT, got.detail)


class Setting(unittest.TestCase):
    def test_off_by_default_strict_bool_on_the_form_and_in_the_plugin_schema(self):
        self.assertIs(config.normalize(tro.raw())[KEY], False)
        self.assertFalse(review_asks.rerequest_grants(config.normalize(tro.raw())))
        self.assertTrue(review_asks.rerequest_grants(config.normalize(tro.raw(**{KEY: True}))))
        with self.assertRaises(config.ConfigError):
            config.normalize(tro.raw(**{KEY: "yes"}))
        self.assertNotIn(KEY, settings_form.apply_settings(tro.raw(), {}))
        self.assertIs(settings_form.apply_settings(tro.raw(), {KEY: True})[KEY], True)
        self.assertIn(KEY, settings_form.SETTINGS_SCHEMA)
        text = (pathlib.Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        self.assertIn(f"  {KEY}:", text)

    def test_show_lists_it_and_says_when_it_is_inert(self):
        def row(**extra):
            loop = routed(self, config.normalize(tro.raw(**extra)))
            rows = {r.name: r for r in show._rows(loop)}
            return rows[KEY]
        self.assertEqual(row(**{KEY: True}).note, "on, but inert: needs a maintainer "
                                                  "(triage.maintainers or operator_agents)")
        self.assertEqual(row(**{KEY: True}, operator_agents=[MAINT]).note,
                         "on, but inert: needs an observer feed that sends granted "
                         "(no observer configured)")
        self.assertEqual(row(**{KEY: True}, operator_agents=[MAINT],
                             observer={**FEED, "events": ["held"]}).note,
                         "on, but inert: needs an observer feed that sends granted "
                         "(observer.events leaves out granted)")
        self.assertEqual(row(**{KEY: True}, operator_agents=[MAINT], observer=FEED).note, "")
        self.assertIn("needs an observer feed that sends granted (route 'elsewhere' is missing",
                      row(**{KEY: True}, operator_agents=[MAINT],
                          observer={"route": "elsewhere"}).note)
        self.assertEqual(row().note, "")


LOOP_ID = "regrants"
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
        (t.LOOPS_DIR / "widgets.json").unlink(missing_ok=True)   # one loop per repo
        LOOP_FILE.unlink(missing_ok=True)
        self.addCleanup(LOOP_FILE.unlink, missing_ok=True)

    def cli(self, *argv, settings=None):
        return t.run_cli(t.parser_for(settings).parse_args(list(argv)))

    def init(self, *extra, settings=None):
        return self.cli("init", "--repo", t.REPO, "--id", LOOP_ID, "--host", t.HOST,
                        "--reviewer", t.REVIEWER, "--fixer", t.FIXER,
                        "--reviewer-profile", "reviewer-profile",
                        "--fixer-profile", "fixer-profile",
                        "--token", f"{t.REVIEWER}={t.SEAT_PATS[0]}",
                        "--token", f"{t.FIXER}={t.SEAT_PATS[1]}", *t.READER_ARGS, *extra,
                        settings=settings)

    def written(self):
        return json.loads(LOOP_FILE.read_text()).get(KEY)

    def test_init_and_set_move_it(self):
        self.assertEqual(self.init()[0], 0)
        self.assertIs(self.written(), False)
        for value, want in (("on", True), ("off", False)):
            rc, out = self.cli("set", "--loop", LOOP_ID, "--review-only-rerequest-grants", value)
            self.assertEqual(rc, 0, out)
            self.assertIs(self.written(), want)

    def test_init_takes_the_flag_else_the_plugin_setting(self):
        rc, out = self.init("--review-only-rerequest-grants", "on")
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written(), True)
        LOOP_FILE.unlink()
        rc, out = self.init(settings={KEY: True})
        self.assertEqual(rc, 0, out)
        self.assertIs(self.written(), True)

    def test_setup_hands_init_the_flag(self):
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO,
                                            "--review-only-rerequest-grants", "on"])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertIn("--review-only-rerequest-grants=on", argv)
        args = t.parser_for({}).parse_args(["setup", "--repo", t.REPO])
        argv, _ = cli_setup._setup_init_argv(args, t.REPO, LOOP_ID, interactive=False)
        self.assertFalse([a for a in argv if a.startswith("--review-only-rerequest-grants")])


if __name__ == "__main__":
    unittest.main()
