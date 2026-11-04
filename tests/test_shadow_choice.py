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

    def enqueue(self, loop, listing, number=8, head=None, seat="reviewer"):
        """The seats ``enqueue_isolated`` submitted, and the files reads it made."""
        head = head or an_unsampled_head(loop, number)
        sup = mock.Mock()
        sup.submit.return_value = "enqueued"
        with mock.patch.object(gate, "isolated_supervisor", return_value=sup), \
                mock.patch.object(gh, "pr_files_read", return_value=listing) as read:
            gate.enqueue_isolated(loop, seat, number, head)
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

    def test_an_unread_or_cut_short_listing_falls_back_to_sampling(self):
        loop = self.make(every=1)
        seats, _ = self.enqueue(loop, (None, "HTTP 502"), number=11)
        self.assertEqual(seats, ["reviewer", "shadow"])        # not "only docs": unknown
        line = shadow_choice.explain_line(loop, 11, an_unsampled_head(loop, 11))
        self.assertIn("its changed files could not be read (HTTP 502)", line)
        cut = files(*[f"docs/{i}.md" for i in range(gh.PR_FILES_LISTING_LIMIT)])
        self.assertEqual(self.enqueue(loop, (cut, ""), number=12)[0], ["reviewer", "shadow"])

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

    def test_status_says_how_and_lists_the_newest_choices(self):
        head = an_unsampled_head(self.loop, 14)
        self.enqueue(self.loop, (files("diaktoros/broker.py"), ""), 14, head)
        lines = shadow_choice.status_lines(self.loop)
        self.assertIn("by risk: always if a PR changes a shadow.always_paths glob (2)", lines[0])
        self.assertEqual(lines[1], f"PR #14 at {head[:7]}: shadowed — it changes "
                                   "diaktoros/broker.py (shadow.always_paths)")
        plain = self.make(every=5, always_paths=None, never_paths=None)
        self.assertEqual(shadow_choice.status_lines(plain), ["one head in 5, sampled "
                                                             "(shadow.every)"])
        self.assertEqual(shadow_choice.status_lines(config.normalize(dict(RAW))), [])

    def test_explain_never_decides_by_paths_itself(self):
        with mock.patch.object(gh, "pr_files_read") as read:
            line = shadow_choice.explain_line(self.loop, 15, "f" * 40)
        read.assert_not_called()
        self.assertIn("no shadow choice made at fffffff yet", line)

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
        self.assertEqual(sorted(kept), [f"{100 + n}:{'e' * 40}" for n in (2, 3, 4)])


class Setting(unittest.TestCase):
    def test_the_lists_are_checked(self):
        ok = config.normalize({**RAW, "shadow": {"profile": "g", "always_paths": [" a/* "],
                                                 "never_paths": []}})
        self.assertEqual(ok["shadow"], {"profile": "g", "always_paths": ["a/*"]})
        for bad in ("a/*", ["a/*", "a/*"], [""], [1], ["x"] * 51, ["a\nb"]):
            for key in ("always_paths", "never_paths"):
                with self.subTest(key=key, bad=bad), self.assertRaises(config.ConfigError):
                    config.normalize({**RAW, "shadow": {"profile": "g", key: bad}})


if __name__ == "__main__":
    unittest.main()
