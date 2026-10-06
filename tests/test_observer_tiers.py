"""Observer tiers: urgent notices now, routine ones batched, optional urgent destination."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from review_loop import config, observer, routes
from review_loop.state import LoopState

import _ledger_guard  # noqa: E402

setUpModule, tearDownModule = _ledger_guard.module_home()

HEAD = "a" * 40


class ObserverTierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.subs = pathlib.Path(self.temp.name) / "subscriptions.json"
        env = patch.dict("os.environ", {"REVIEW_LOOP_SUBS": str(self.subs)})
        env.start()
        self.addCleanup(env.stop)
        self.loop = {"id": "feed", "repo": "owner/feed", "state_dir": self.temp.name,
                     "host": "https://owner.example",
                     "observer": {"route": "feed-observe", "profile": "default",
                                  "deliver": "telegram"}}
        self.state = LoopState(self.loop)

    def register(self, urgent=False):
        subs = {"feed-observe": {"host": "https://owner.example", "secret": "s",
                                 **observer.route_contract(self.loop)}}
        if urgent:
            subs["feed-urgent"] = {"host": "https://owner.example", "secret": "u",
                                   **observer.route_contract(self.loop, True)}
        self.subs.write_text(json.dumps(subs))

    def sent(self, fire):
        return [(c.args[0], c.args[2]["_observer"]["event"]) for c in fire.call_args_list]

    def test_tiers_are_fixed_and_cover_every_event(self):
        self.assertEqual(observer.URGENT_EVENTS | observer.ROUTINE_EVENTS, set(observer.EVENTS))
        self.assertFalse(observer.URGENT_EVENTS & observer.ROUTINE_EVENTS)
        for event in ("failed", "held", "escalation", "ruling"):
            self.assertTrue(observer.is_urgent(event))
        for event in ("opened", "handoff", "verdict", "approved", "triaged", "fixing",
                      "fixed", "closed"):
            self.assertFalse(observer.is_urgent(event))
        self.assertTrue(observer.is_urgent("fixed", "POST outcome uncertain"))

    def test_digest_queues_routine_and_sends_urgent_at_once(self):
        self.loop["observer"]["digest_min"] = 30
        self.register()
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertFalse(observer.notify(self.loop, self.state, "opened", 7, HEAD))
            self.assertFalse(observer.notify(self.loop, self.state, "approved", 7, HEAD,
                                             identity=1))
            self.assertEqual(fire.call_count, 0)
            for event in ("failed", "held", "escalation", "ruling"):
                self.assertTrue(observer.notify(self.loop, self.state, event, 7, HEAD,
                                                identity=event))
            self.assertEqual([e for _, e in self.sent(fire)],
                             ["failed", "held", "escalation", "ruling"])
            self.assertEqual(observer.owed(self.state).get("queued"), 2)
            self.assertTrue(observer.notify(self.loop, self.state, "fixed", 8, HEAD,
                                            identity="x", outcome="outcome uncertain"))
        self.assertEqual(observer.owed(self.state).get("queued"), 2)

    def test_no_digest_sends_everything_immediately_to_the_one_feed(self):
        self.register()
        with patch.object(routes, "fire", return_value=True) as fire:
            for event in ("opened", "failed", "approved"):
                self.assertTrue(observer.notify(self.loop, self.state, event, 7, HEAD,
                                                identity=event))
        self.assertEqual(self.sent(fire), [("feed-observe", "opened"), ("feed-observe", "failed"),
                                           ("feed-observe", "approved")])

    def test_digest_groups_by_pr_in_event_order(self):
        self.loop["observer"]["digest_min"] = 30
        self.register()
        n = observer.notify
        n(self.loop, self.state, "opened", 312, HEAD)
        n(self.loop, self.state, "verdict", 312, HEAD, identity=1, outcome="changes requested")
        n(self.loop, self.state, "opened", 316, HEAD)
        n(self.loop, self.state, "handoff", 312, HEAD, identity="fix")
        n(self.loop, self.state, "approved", 312, HEAD, identity=2)
        n(self.loop, self.state, "approved", 316, HEAD, identity=3)
        n(self.loop, self.state, "fixing", 318, "base", identity="b", issue=True)
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertTrue(observer.flush(self.loop, self.state))
        text = fire.call_args.args[2]["_observer"]["message"]
        lines = text.splitlines()
        self.assertEqual(lines[0], "🗂 [feed] last 30m — 2 PRs, 1 issue")
        self.assertTrue(lines[1].startswith(
            "#312 opened → reviewed (changes) → fixed → approved · "
            "https://github.com/owner/feed/pull/312"), lines[1])
        self.assertTrue(lines[2].startswith("#316 opened → approved · "), lines[2])
        self.assertTrue(lines[3].startswith("#318 (issue) handed to fixer · "), lines[3])
        self.assertIn("/issues/318", lines[3])

    def test_digest_is_bounded(self):
        entries = [{"event": "opened", "number": i, "url": f"u/{i}"} for i in range(40)]
        lines = observer.render_digest(self.loop, entries).splitlines()
        self.assertEqual(len(lines), 1 + observer.DIGEST_LIMIT + 1)
        self.assertEqual(lines[-1], "…and 15 more")

    def test_urgent_route_gets_only_urgent_events(self):
        self.loop["observer"].update(urgent_route="feed-urgent", digest_min=30)
        self.register(urgent=True)
        with patch.object(routes, "fire", return_value=True) as fire:
            observer.notify(self.loop, self.state, "opened", 7, HEAD)
            observer.notify(self.loop, self.state, "failed", 7, HEAD, identity="f")
            observer.notify(self.loop, self.state, "held", 7, HEAD, identity="h")
            self.assertEqual(self.sent(fire), [("feed-urgent", "failed"), ("feed-urgent", "held")])
            self.assertTrue(observer.flush(self.loop, self.state))
        self.assertEqual(self.sent(fire)[-1], ("feed-observe", "digest"))

    def test_urgent_route_without_digest_still_splits(self):
        self.loop["observer"]["urgent_route"] = "feed-urgent"
        self.register(urgent=True)
        with patch.object(routes, "fire", return_value=True) as fire:
            observer.notify(self.loop, self.state, "opened", 7, HEAD)
            observer.notify(self.loop, self.state, "escalation", 7, HEAD, identity="e")
        self.assertEqual(self.sent(fire), [("feed-observe", "opened"), ("feed-urgent", "escalation")])

    def test_urgent_route_must_pass_its_own_contract(self):
        self.loop["observer"]["urgent_route"] = "feed-urgent"
        self.register(urgent=True)
        subs = json.loads(self.subs.read_text())
        subs["feed-urgent"]["deliver_only"] = False
        self.subs.write_text(json.dumps(subs))
        with patch.object(routes, "fire", return_value=True) as fire:
            self.assertFalse(observer.notify(self.loop, self.state, "failed", 7, HEAD,
                                             identity="f"))
            fire.assert_not_called()

    def test_normalize_urgent_settings(self):
        raw = {"route": "a", "urgent_route": "b", "urgent_deliver": "discord"}
        obs = config.normalize_observer(raw)
        self.assertEqual((obs["urgent_route"], obs["urgent_deliver"]), ("b", "discord"))
        self.assertIn("misconfigured", config.normalize_observer({"route": "a", "urgent_route": "a"}))
        self.assertNotIn("urgent_route", config.normalize_observer({"route": "a"}))
        self.assertEqual(config.seat_profile({"observer": obs}, "observer_urgent"), "default")


if __name__ == "__main__":
    unittest.main()
