"""Shadow reviews by risk (#1788): chosen by what the PR changes, not sampled blind.

``shadow.always_paths``: a PR that changes one gets a shadow whatever ``shadow.every`` says.
``shadow.never_paths``: a PR that changes only these gets none. Anything else is sampled as
before, and with neither list set nothing is read from GitHub at all. The choice is made once
per PR head and kept, so the gate's several asks, a redelivery and ``explain`` agree.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import config, gate, gh, shadow_choice  # noqa: E402
from diaktoros import state as state_mod  # noqa: E402

RAW = {"id": "risky", "repo": "acme/widgets", "fixers": ["fixer"], "reviewers": ["reviewer"],
       "read_token": "reader",
       "seats": {"reviewer": {"profile": "rev", "route": "r"},
                 "fixer": {"profile": "fix", "route": "f"}}}
ALWAYS = ["diaktoros/broker*", "diaktoros/safe_push.py"]
NEVER = ["docs/*", "*.md", "tests/*"]


def files(*names, renamed=None):
    out = [{"filename": name, "status": "modified"} for name in names]
    if renamed:
        out.append({"filename": renamed[1], "previous_filename": renamed[0],
                    "status": "renamed"})
    return out


def an_unsampled_head(loop: dict, number: int) -> str:
    """A head sampling alone would not shadow; any head at ``shadow.every`` 1, which has none."""
    if config.shadow_every(loop) == 1:
        return "a" * 40
    return next(h for h in (f"{i:040x}" for i in range(1, 500))
                if not shadow_choice.sampled(loop, number, h))


class ByRisk(unittest.TestCase):
    def setUp(self):
        self.loop = self.make(every=1000)
        (state_mod.state_for(self.loop).dir / shadow_choice.FILE).unlink(missing_ok=True)

    def make(self, **shadow):
        block = {"profile": "grok", "always_paths": ALWAYS, "never_paths": NEVER, **shadow}
        return config.normalize({**RAW, "shadow": {k: v for k, v in block.items()
                                                   if v is not None}})

    def enqueue(self, loop, listing, number=8, head=None, seat="reviewer", turn_key=""):
        """The seats ``enqueue_isolated`` submitted, and the files reads it made."""
        head = head or an_unsampled_head(loop, number)
        sup = mock.Mock()
        sup.submit.return_value = "enqueued"
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup), \
                mock.patch.object(gh, "pr_files_read", return_value=listing) as read:
            gate.enqueue_isolated(loop, seat, number, head, turn_key=turn_key)
        return [c.args[4] for c in sup.submit.call_args_list], read.call_count

    def test_a_docs_only_pr_gets_no_shadow_even_at_every_1(self):
        loop = self.make(every=1)
        seats, _ = self.enqueue(loop, (files("docs/a.md", "README.md", "tests/test_x.py"), ""))
        self.assertEqual(seats, ["reviewer"])
        line = shadow_choice.explain_line(loop, 8, an_unsampled_head(loop, 8))
        self.assertIn("gets no shadow review: it changes only 3 path(s) matching "
                      "shadow.never_paths", line)

    def test_a_broker_change_gets_a_shadow_whatever_every_says(self):
        head = an_unsampled_head(self.loop, 9)                 # sampling alone: no shadow
        seats, _ = self.enqueue(self.loop, (files("docs/a.md", "diaktoros/broker_ipc.py"), ""),
                                number=9, head=head)
        self.assertEqual(seats, ["reviewer", "shadow"])
        self.assertEqual(shadow_choice.explain_line(self.loop, 9, head),
                         f"head {head[:7]} gets a shadow review: it changes "
                         "diaktoros/broker_ipc.py (shadow.always_paths)")

    def test_a_file_renamed_out_of_an_always_path_still_counts(self):
        seats, _ = self.enqueue(self.loop, (files(renamed=("diaktoros/safe_push.py",
                                                           "docs/old_push.md")), ""))
        self.assertEqual(seats, ["reviewer", "shadow"])

    def test_any_other_change_is_sampled_as_before(self):
        listing = (files("docs/a.md", "diaktoros/stats.py"), "")
        self.assertEqual(self.enqueue(self.loop, listing)[0], ["reviewer"])     # 1 in 1000
        head = next(h for h in (f"{i:040x}" for i in range(1, 50000))
                    if shadow_choice.sampled(self.loop, 10, h))
        self.assertEqual(self.enqueue(self.loop, listing, number=10, head=head)[0],
                         ["reviewer", "shadow"])

    def test_an_unread_listing_shadows_when_there_are_always_paths(self):
        # Review of #1852, F1: kept as "not sampled", a failed read would let a broker change
        # through unshadowed, and so unheld under shadow.escalate, for good.
        head = an_unsampled_head(self.loop, 11)
        listings = iter([(None, "HTTP 502"), (files("diaktoros/broker.py"), "")])
        sup = mock.Mock()
        sup.submit.return_value = "enqueued"
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup), \
                mock.patch.object(gh, "pr_files_read", side_effect=lambda *a: next(listings)):
            gate.enqueue_isolated(self.loop, "reviewer", 11, head)
            self.assertTrue(gate.shadow_sampled(self.loop, 11, head))      # kept, not re-read
        self.assertEqual([c.args[4] for c in sup.submit.call_args_list], ["reviewer", "shadow"])
        self.assertIn("could not be read (HTTP 502): shadowed, since it may change a "
                      "shadow.always_paths path", shadow_choice.explain_line(self.loop, 11, head))
        # With only a never list there is no guarantee to keep: unread samples, as before.
        never_only = self.make(every=1000, always_paths=None)
        seats, _ = self.enqueue(never_only, (None, "HTTP 502"), number=12)
        self.assertEqual(seats, ["reviewer"])

    def test_a_listing_cut_short_shadows_beside_always_paths_and_is_never_only_never_paths(self):
        # Review of #1852, F5: GitHub lists at most 3,000 files, so an always path may be among
        # the unlisted ones. On a head sampling alone skips, that would be no shadow, no hold.
        cut = files(*[f"docs/{i}.md" for i in range(gh.PR_FILES_LISTING_LIMIT)])
        head = an_unsampled_head(self.loop, 17)
        self.assertEqual(self.enqueue(self.loop, (cut, ""), number=17, head=head)[0],
                         ["reviewer", "shadow"])
        self.assertIn("GitHub lists only the first 3000 of its changed files: shadowed",
                      shadow_choice.explain_line(self.loop, 17, head))
        # With only a never list, the listed docs can't prove "only never paths": sampled.
        never_only = self.make(every=1000, always_paths=None)
        head = an_unsampled_head(never_only, 18)
        self.assertEqual(self.enqueue(never_only, (cut, ""), number=18, head=head)[0],
                         ["reviewer"])
        self.assertIn("not sampled", shadow_choice.explain_line(never_only, 18, head))
        every_1 = self.make(every=1, always_paths=None)
        self.assertEqual(self.enqueue(every_1, (cut, ""), number=19)[0], ["reviewer", "shadow"])

    def test_a_change_to_the_lists_or_the_rate_makes_the_choice_again(self):
        # Review of #1852, F2: a choice kept under the old lists would outlive the new ones.
        head = an_unsampled_head(self.loop, 16)
        broker = (files("diaktoros/broker.py"), "")
        old = self.make(every=1000, always_paths=["x/*"])
        with mock.patch.object(gh, "pr_files_read", return_value=broker) as read:
            self.assertFalse(gate.shadow_sampled(old, 16, head))
            self.assertTrue(gate.shadow_sampled(self.loop, 16, head))      # broker* added
            self.assertFalse(gate.shadow_sampled(old, 16, head))           # and taken away
            self.assertFalse(gate.shadow_sampled(old, 16, head))           # kept again
        self.assertEqual(read.call_count, 3)
        self.assertIsNone(shadow_choice.recorded(self.loop, 16, head))
        self.assertIn("no shadow choice made", shadow_choice.explain_line(self.loop, 16, head))

    def test_the_choice_is_made_once_per_head(self):
        head = an_unsampled_head(self.loop, 13)
        seats, reads = self.enqueue(self.loop, (files("diaktoros/broker.py"), ""), 13, head)
        self.assertEqual((seats, reads), (["reviewer", "shadow"], 1))   # one read, four asks
        # A redelivery whose listing now reads differently keeps the kept choice, and reads none.
        seats, reads = self.enqueue(self.loop, (files("docs/a.md"), ""), 13, head)
        self.assertEqual(reads, 0)
        self.assertTrue(gate.shadow_sampled(self.loop, 13, head))

    def test_no_lists_no_read_and_exactly_the_old_sampling(self):
        loop = self.make(every=3, always_paths=None, never_paths=None)
        heads = [f"{i:040x}" for i in range(60)]
        with mock.patch.object(gh, "pr_files_read") as read:
            picked = [h for h in heads if gate.shadow_sampled(loop, 8, h)]
        read.assert_not_called()
        self.assertEqual(picked, [h for h in heads if shadow_choice.sampled(loop, 8, h)])
        self.assertTrue(0 < len(picked) < len(heads))

    def test_a_fixer_turn_reads_nothing(self):
        seats, reads = self.enqueue(self.loop, (files("diaktoros/broker.py"), ""), seat="fixer")
        self.assertEqual((seats, reads), (["fixer"], 0))

    def test_a_new_turn_at_the_same_head_chooses_again_from_its_own_diff(self):
        # Review of #1852, F6: a same-head retarget changes the PR's file listing (its base) and
        # starts a fresh reviewer turn (transition.turn_key). Kept per head alone, its docs-only
        # "no shadow" would outlive a new diff that changes the broker.
        head = an_unsampled_head(self.loop, 20)
        retarget = "retarget:release:1700000000000"
        with mock.patch.object(gh, "pr_files_read", return_value=(files("docs/a.md"), "")):
            self.assertFalse(gate.shadow_sampled(self.loop, 20, head))
        with mock.patch.object(gh, "pr_files_read",
                               return_value=(files("diaktoros/broker.py"), "")) as read:
            self.assertTrue(gate.shadow_sampled(self.loop, 20, head, retarget))
            self.assertFalse(gate.shadow_sampled(self.loop, 20, head))      # each turn its own
            self.assertTrue(gate.shadow_sampled(self.loop, 20, head, retarget))
        self.assertEqual(read.call_count, 1)
        # Through the real enqueue: the retarget's fresh turn carries its key to every ask.
        head = an_unsampled_head(self.loop, 21)
        self.assertEqual(self.enqueue(self.loop, (files("docs/a.md"), ""), 21, head)[0],
                         ["reviewer"])
        seats, reads = self.enqueue(self.loop, (files("diaktoros/broker.py"), ""), 21, head,
                                    turn_key=retarget)
        self.assertEqual((seats, reads), (["reviewer", "shadow"], 1))
        self.assertIn("diaktoros/broker.py", shadow_choice.explain_line(self.loop, 21, head))
        lines = shadow_choice.status_lines(self.loop)
        self.assertTrue(any(line.startswith(f"PR #21 at {head[:7]}: shadowed") for line in lines),
                        lines)

    def test_status_says_how_and_lists_the_newest_choices(self):
        head = an_unsampled_head(self.loop, 14)
        self.enqueue(self.loop, (files("diaktoros/broker.py"), ""), 14, head)
        lines = shadow_choice.status_lines(self.loop)
        self.assertEqual(lines[0], "profile grok · 0/no budget tokens today · 0/no cap turns today")
        self.assertIn("by risk: always if a PR changes a shadow.always_paths glob (2)", lines[1])
        self.assertEqual(lines[2], f"PR #14 at {head[:7]}: shadowed — it changes "
                                   "diaktoros/broker.py (shadow.always_paths)")
        plain = self.make(every=5, always_paths=None, never_paths=None)
        self.assertEqual(shadow_choice.status_lines(plain)[1:], ["one head in 5, sampled "
                                                                 "(shadow.every)"])
        self.assertEqual(shadow_choice.status_lines(config.normalize(dict(RAW))), [])

    def test_explain_never_decides_by_paths_itself(self):
        with mock.patch.object(gh, "pr_files_read") as read:
            line = shadow_choice.explain_line(self.loop, 15, "f" * 40)
        read.assert_not_called()
        self.assertIn("no shadow choice made at fffffff under the current shadow paths", line)

    def test_explain_prints_the_choice_beside_the_hold_note(self):
        import contextlib
        import io
        from diaktoros.cli import operations
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            operations._print_shadow_note({"shadow": {"choice": "head abc gets a shadow review: x",
                                                      "note": "nothing to wait for"}}, "> ")
        self.assertEqual(out.getvalue(), "> head abc gets a shadow review: x\n"
                                         "> nothing to wait for\n")

    def test_only_the_newest_choices_are_kept(self):
        with mock.patch.object(shadow_choice, "KEEP", 3), \
                mock.patch.object(gh, "pr_files_read", return_value=(files("x.py"), "")):
            for n in range(5):
                shadow_choice.choose(self.loop, 100 + n, "e" * 40)
        kept = shadow_choice._memory(state_mod.state_for(self.loop))
        self.assertEqual(sorted(kept), [f"{100 + n}:{'e' * 40}:" for n in (2, 3, 4)])


class Setting(unittest.TestCase):
    def test_the_lists_are_checked(self):
        ok = config.normalize({**RAW, "shadow": {"profile": "g", "always_paths": [" a/* "],
                                                 "never_paths": []}})
        self.assertEqual(ok["shadow"], {"profile": "g", "always_paths": ["a/*"]})
        for bad in ("a/*", ["a/*", "a/*"], [""], [1], ["x"] * 51, ["a\nb"]):
            for key in ("always_paths", "never_paths"):
                with self.subTest(key=key, bad=bad), self.assertRaises(config.ConfigError):
                    config.normalize({**RAW, "shadow": {"profile": "g", key: bad}})

    def test_escalate_takes_always_paths_in_place_of_every_1(self):
        # With always paths, every head of such a PR is shadowed and held; the rest are sampled.
        # Blind sampling still can't escalate, and a never list alone is no guarantee either.
        ok = config.normalize({**RAW, "shadow": {"profile": "g", "every": 5, "escalate": True,
                                                 "always_paths": ["src/auth/*"]}})
        self.assertTrue(config.shadow_escalate(ok))
        for shadow in ({"every": 5}, {"every": 5, "never_paths": ["docs/*"]}, {}):
            with self.subTest(shadow=shadow), self.assertRaisesRegex(
                    config.ConfigError, "needs shadow.every 1 or shadow.always_paths"):
                config.normalize({**RAW, "shadow": {"profile": "g", "escalate": True, **shadow}})

    def test_set_keeps_the_escalate_rule_both_ways(self):
        from diaktoros.config import settings_form
        current = {"profile": "g", "every": 1, "escalate": True, "always_paths": ["a/*"]}
        loosened = settings_form.shadow_block(current, None, "5", "set")
        self.assertEqual(loosened["every"], 5)
        with self.assertRaises(config.ConfigError):        # the always list was what allowed it
            settings_form.shadow_block(loosened, None, None, "set", always_paths=[])


class Status(unittest.TestCase):
    """#1070: `hermes dk status` shows the shadow seat: profile, every-N, today's spend."""

    def status(self, loop: dict) -> str:
        import argparse
        import io
        import os
        import tempfile
        from contextlib import redirect_stdout
        from diaktoros import cli
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"DIAKTOROS_CONFIG_DIR": temp.name}), \
                mock.patch.object(config, "load_id", return_value=loop), \
                mock.patch("diaktoros.state.state_for") as state_for, redirect_stdout(out):
            state_for.return_value.dir = pathlib.Path(temp.name)
            for name in ("kv_get", "queue_items", "queue_all", "breach_all", "watch",
                         "late_triage", "pr_holds", "scope_holds", "_lock_ledger"):
                getattr(state_for.return_value, name).return_value = {}
            cli.cmd_status(argparse.Namespace(loop=loop["id"]))
        return out.getvalue()

    def test_status_prints_the_shadow_profile_cadence_and_spend(self):
        from diaktoros import pacing
        raw = {**RAW, "id": "shady", "shadow": {"profile": "grok", "every": 4},
               "seats": {**RAW["seats"], "shadow": {"daily_tokens": 50000, "daily_turns": 6}}}
        loop = config.normalize(raw)
        pacing.count_tokens("shady", "shadow", 12345)
        pacing.count_turn("shady", "shadow")
        out = self.status(loop)
        self.assertIn("  shadow:     profile grok · 12,345/50,000 tokens today · 1/6 turns today",
                      out)
        self.assertIn("  shadow:     one head in 4, sampled (shadow.every)", out)
        # The reviewer/fixer pacing line is unchanged by the shadow's budget.
        self.assertNotIn("pacing:     reviewer 0/no budget", out)

    def test_status_without_shadow_prints_no_shadow_line(self):
        self.assertNotIn("shadow:", self.status(config.normalize({**RAW, "id": "plain"})))


if __name__ == "__main__":
    unittest.main()
