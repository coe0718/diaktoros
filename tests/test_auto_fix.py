"""Issue #232: triage auto-offers P3/documentation issues to the fixer, host-side.

GitHub is mocked; the ledger and pacing file live in temporary directories.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_issue_fix import BASE, REPO, Base, issue  # noqa: E402

from diaktoros import config, fix_hold, gate, gh, pacing  # noqa: E402


class AutoFixConfig(Base):
    def loop_with(self, **triage):
        raw = {**self.raw, "triage": {**self.raw["triage"], "labels": ["bug", "P3", "documentation"],
                                      **triage}}
        return config.normalize(raw)

    def test_off_by_default(self):
        self.assertEqual(config.auto_fix_labels(self.loop), set())
        self.assertEqual(config.auto_fix_daily(self.loop), 25)

    def test_labels_and_cap_validate(self):
        loop = self.loop_with(auto_fix_labels=["p3", "documentation"], auto_fix_daily=5)
        self.assertEqual(loop["triage"]["auto_fix_labels"], ["P3", "documentation"])
        self.assertEqual(config.auto_fix_daily(loop), 5)
        for bad in ({"auto_fix_labels": ["P1"]}, {"auto_fix_labels": ["nope"]},
                    {"auto_fix_daily": 5}, {"auto_fix_labels": ["P3"], "auto_fix_daily": 0}):
            with self.subTest(bad), self.assertRaises(config.ConfigError):
                self.loop_with(**bad)

    def test_needs_fix_label(self):
        raw = {**self.raw["triage"], "labels": ["P3"], "auto_fix_labels": ["P3"]}
        raw.pop("fix_label"), raw.pop("maintainers")
        with self.assertRaises(config.ConfigError):
            config.normalize({**self.raw, "triage": raw})

    def test_p0_to_p2_block_an_eligible_issue(self):
        loop = self.loop_with(auto_fix_labels=["P3"])
        self.assertTrue(config.auto_fix_eligible(loop, ["P3"]))
        self.assertFalse(config.auto_fix_eligible(loop, ["P3", "p1"]))
        self.assertFalse(config.auto_fix_eligible(loop, ["bug"]))

    def test_schema_matches_plugin_yaml(self):
        text = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        for key in ("auto_fix_labels", "auto_fix_daily"):
            self.assertIn(key, config.SETTINGS_SCHEMA)
            self.assertIn(f"  {key}:", text)


class AutoOffer(Base):
    def setUp(self):
        super().setUp()
        self.raw["triage"].update(labels=["bug", "P3"], auto_fix_labels=["P3"], auto_fix_daily=2)
        self.loop = config.normalize(self.raw)
        self.live = issue(labels=("P3",))
        self.enqueued, self.open_prs, self.depth, self.origin = [], [], None, None
        self.origin_state = "merged"
        sup = mock.Mock()
        sup.filed_depth.side_effect = lambda repo, n: self.depth

        self.posted, self.bodies = [], {}

        def api(loop, path, method="GET", body=None, login=None):
            if method == "POST":
                self.posted.append(body["body"])
                return {"id": 1}
            if "/pulls/" in path:
                return {"number": int(path.rsplit("/", 1)[1]), "state": "open" if self.origin_state == "open"
                        else "closed", "merged": self.origin_state == "merged"}
            if "/git/ref/" in path:
                return {"object": {"sha": BASE}}
            return {**self.live, "number": int(path.rsplit("/", 1)[1])}
        for target, name, value in (
                (gate, "isolated_supervisor", lambda loop: sup),
                (fix_hold, "origin_pr", lambda loop, n: self.origin),
                (fix_hold, "_notice", lambda *a, **k: None),
                (fix_hold, "hold", lambda *a, **k: self.enqueued.append("held")),
                (gh, "open_prs_read", lambda loop: (self.open_prs, "")),
                (gh, "api", api),
                (pacing, "path", lambda: self.root / "pacing.json"),
                (gate, "enqueue_isolated",
                 lambda loop, seat, n, head, turn_key="": self.enqueued.append((seat, n)) or "enqueued")):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_eligible_issue_is_queued_once_counted(self):
        self.assertIn("auto-offered", fix_hold.auto_offer(self.loop, 12))
        self.assertEqual(self.enqueued, [("issue_fixer", 12)])
        self.assertEqual(pacing.turns_today(self.loop["id"], fix_hold.AUTO_FIX_SEAT), 1)

    def test_nothing_without_the_setting(self):
        self.raw["triage"].pop("auto_fix_labels"), self.raw["triage"].pop("auto_fix_daily")
        self.assertEqual(fix_hold.auto_offer(config.normalize(self.raw), 12), "")
        self.assertEqual(self.enqueued, [])

    def test_p1_label_or_other_label_is_never_offered(self):
        for labels in (("P3", "P1"), ("bug",)):
            self.live = issue(labels=labels)
            self.assertIn("not auto-offered", fix_hold.auto_offer(self.loop, 12))
        self.assertEqual(self.enqueued, [])

    def test_daily_cap_holds_the_handoff(self):
        for n in (1, 2):
            fix_hold.auto_offer(self.loop, n)
        self.assertIn("daily cap (2) reached", fix_hold.auto_offer(self.loop, 3))
        self.assertEqual(len(self.enqueued), 2)

    def test_existing_guards_apply(self):
        self.open_prs = [{"number": 9, "body": "Fixes #12"}]
        self.assertIn("PR #9 already fixes", fix_hold.auto_offer(self.loop, 12))
        self.open_prs = []
        self.depth = config.AUTO_FIX_MAX_DEPTH + 1
        self.assertIn("lineage depth", fix_hold.auto_offer(self.loop, 12))
        self.depth, self.origin, self.origin_state = 1, 7, "open"
        self.assertIn("held until PR #7", fix_hold.auto_offer(self.loop, 12))
        self.assertEqual(self.enqueued, ["held"])

    def test_same_comments_as_the_label_path(self):
        # already-fixed and closed-unmerged post the one comment the label path posts
        self.open_prs = [{"number": 9, "body": "Fixes #12"}]
        fix_hold.auto_offer(self.loop, 12)
        self.assertEqual(self.posted, ["PR #9 already fixes this; not handing it to the fixer."])
        self.posted.clear()
        self.open_prs, self.origin, self.origin_state = [], 7, "closed"
        self.assertIn("closed unmerged", fix_hold.auto_offer(self.loop, 12))
        self.assertEqual(len(self.posted), 1)
        self.assertIn("PR #7 closed without merging", self.posted[0])
        self.assertEqual(self.enqueued, [])

    def test_person_issue_naming_an_open_pr_is_held_then_released(self):
        from diaktoros import state as state_mod
        self.open_prs = [{"number": 20, "body": ""}]
        self.live = issue(labels=("P3",), body="Docs for the thing in #20.")
        self.origin_state = "open"
        self.assertIn("held until PR #20", fix_hold.auto_offer(self.loop, 12))
        self.assertEqual(self.enqueued, ["held"])
        st = state_mod.state_for(self.loop)
        st.fix_hold_set(12, 20)                       # (hold() is stubbed in this class)
        self.origin_state = "merged"
        fix_hold.sweep(self.loop, st)
        self.assertEqual(self.enqueued[-1], ("issue_fixer", 12))
        self.assertEqual(st.fix_holds(), {})
        self.assertEqual(pacing.turns_today(self.loop["id"], fix_hold.AUTO_FIX_SEAT), 1)

    def test_person_issue_naming_a_url_to_an_open_pr_is_held(self):
        self.open_prs = [{"number": 20, "body": ""}]
        self.live = issue(labels=("P3",), body=f"https://github.com/{REPO}/pull/20")
        self.origin_state = "open"
        self.assertIn("held until PR #20", fix_hold.auto_offer(self.loop, 12))

    def test_person_issue_naming_a_merged_pr_or_an_issue_is_not_held(self):
        self.open_prs = [{"number": 20, "body": ""}]   # 21 merged, 22 an issue: not in the list
        for body in ("see #21 and #22", f"see https://github.com/{REPO}/pull/21"):
            self.live = issue(labels=("P3",), body=body)
            self.assertIn("auto-offered at", fix_hold.auto_offer(self.loop, 12))
        self.assertNotIn("held", self.enqueued)

    def test_release_past_the_cap_waits_for_the_next_day(self):
        from diaktoros import state as state_mod
        for _ in range(2):
            pacing.count_turn(self.loop["id"], fix_hold.AUTO_FIX_SEAT)
        st = state_mod.state_for(self.loop)
        st.fix_hold_set(12, 20)
        self.origin_state = "merged"
        fix_hold.sweep(self.loop, st)
        self.assertEqual((self.enqueued, st.fix_holds()), ([], {12: 20}))
        with mock.patch.object(pacing, "_today", lambda now: "2999-01-01"):
            fix_hold.sweep(self.loop, st)
        self.assertEqual(self.enqueued, [("issue_fixer", 12)])
        self.assertEqual(st.fix_holds(), {})


class WriteTriageHandoff(Base):
    """_write_triage calls auto_offer only after the triage write is recorded as posted."""

    def setUp(self):
        super().setUp()
        self.raw["triage"].update(labels=["bug", "P3"], auto_fix_labels=["P3"])
        self.loop = config.normalize(self.raw)
        self.sup = mock.Mock()
        self.scope = mock.Mock(repo=REPO, number=12, run_id="r1")
        self.offers = []

    def run_write(self, labels=("P3",), post=None, auth=None):
        from diaktoros import broker, broker_ipc
        post = post or mock.Mock(return_value=77)
        patches = [
            mock.patch.object(config, "by_repo", lambda repo: self.loop),
            mock.patch.object(config, "triage_enabled", lambda loop: True),
            mock.patch.object(broker, "authorize_triage", auth or mock.Mock(return_value="bot")),
            mock.patch.object(broker, "post_triage", post),
            mock.patch.object(fix_hold, "auto_offer",
                              lambda loop, n: self.offers.append(n) or "ok"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        broker_ipc._write_triage(self.loop, self.scope, self.sup, list(labels), "body")

    def test_posted_write_invokes_offer(self):
        self.run_write()
        self.assertEqual(self.offers, [12])
        self.sup.triage_status.assert_called_with("r1", "posted", comment_id=77)

    def test_uncertain_write_does_not_offer(self):
        self.run_write(post=mock.Mock(side_effect=RuntimeError("boom")))
        self.assertEqual(self.offers, [])

    def test_denied_write_does_not_offer(self):
        from diaktoros import broker
        self.run_write(auth=mock.Mock(side_effect=broker.BrokerDenied("no")))
        self.assertEqual(self.offers, [])

    def test_no_labels_does_not_offer(self):
        self.run_write(labels=())
        self.assertEqual(self.offers, [])


if __name__ == "__main__":
    unittest.main()
