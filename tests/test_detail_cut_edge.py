"""#2176: ``runs.detail`` never keeps a token's prefix-less tail from the margin window's edge.

``output_detail`` redacts a window ``DETAIL_MARGIN`` times what it stores, then cuts it (#2083).
When the tokens inside the window shrink enough on redaction, the redacted window falls below the
final cut and is kept whole, including a fragment of a secret whose ``ghp_`` prefix sat just
before the window: no pattern matches it. The window is redacted first, so a whole credential at
its edge is masked; then its edge drops its first word (any characters a ``Bearer`` value takes),
or a cut ``Bearer``/``token`` prefix's rest with the value after it, whether ``output_detail`` cut
the window or the caller's capture did.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from diaktoros import run_supervisor  # noqa: E402

LIMIT = run_supervisor.DETAIL_STDOUT_BYTES
WINDOW = run_supervisor.DETAIL_MARGIN * LIMIT
BODY = "A1b2C3d4" * 4                       # the tail of a ghp_ token
UNIT = "ghp_" + "Z" * 300 + " "             # 305 bytes, "[REDACTED] " (11) once redacted


def collapsing(prefix="ghp_"):
    """A stream whose last ``WINDOW`` bytes start with ``BODY``, its token prefix just before."""
    return prefix + BODY + (UNIT * 200)[:WINDOW - len(BODY)]


class CutEdge(unittest.TestCase):
    def test_a_window_that_collapses_on_redaction_keeps_no_token_fragment(self):
        detail = run_supervisor.output_detail(collapsing(), "")
        self.assertNotIn("A1b2C3d4", detail)
        self.assertNotIn("ZZZZ", detail)
        self.assertTrue(detail.startswith("stdout: […]"), detail[:40])
        self.assertIn("[REDACTED]", detail)

    def test_the_callers_capture_cut_counts_too(self):
        # The fixture's file tail and the production capture hand over exactly the window.
        for stream in (collapsing()[-WINDOW:], collapsing()[-WINDOW:].encode()):
            with self.subTest(kind=type(stream).__name__):
                detail = run_supervisor.output_detail(stream, "")
                self.assertNotIn("A1b2C3d4", detail)

    def test_stderr_gets_the_same_edge(self):
        limit = run_supervisor.DETAIL_MARGIN * run_supervisor.DETAIL_STDERR_BYTES
        stream = "ghp_" + BODY + (UNIT * 50)[:limit - len(BODY)]
        detail = run_supervisor.output_detail("", stream)
        self.assertNotIn("A1b2C3d4", detail)

    def test_a_window_that_is_one_run_is_kept(self):
        # No token is as long as the window: a whole window of token characters is output.
        detail = run_supervisor.output_detail("y" * (2 * WINDOW) + "END", "")
        self.assertTrue(detail.endswith("y" * 100 + "END"), detail[-20:])

    def test_output_inside_the_window_keeps_its_first_word(self):
        detail = run_supervisor.output_detail("Finished: 3 files changed\nok", "")
        self.assertEqual(detail, "stdout: Finished: 3 files changed\nok")

    def test_an_edge_on_whitespace_drops_the_word_after_it(self):
        # The word may be a ``Bearer`` value whose prefix sat just before the edge.
        for word in ("keep", "SECRETVALUE77"):
            with self.subTest(word=word):
                stream = "Bearer" + " " + word + " " + (UNIT * 200)[:WINDOW - len(word) - 2]
                detail = run_supervisor.output_detail(stream, "")
                self.assertNotIn(word, detail)
                self.assertTrue(detail.startswith("stdout: […] [REDACTED]"), detail[:40])

    def edge(self, head):
        """A stream whose last ``WINDOW`` bytes are ``head``, then tokens that shrink on redaction."""
        return head + (UNIT * 200)[:WINDOW - len(head)]

    def test_a_whole_credential_at_the_edge_is_redacted_before_the_edge_is_cut(self):
        # Vex on #2695: stripping the edge first took ``Bearer`` off an intact credential.
        for head in ("Bearer Secret1234567890 ", "token " + "S3cretTokenValue" * 2 + " "):
            with self.subTest(head=head[:6]):
                detail = run_supervisor.output_detail(self.edge(head), "")
                self.assertNotIn("Secret1234567890", detail)
                self.assertNotIn("S3cretTokenValue", detail)

    def test_a_fragment_of_any_bearer_character_is_dropped(self):
        detail = run_supervisor.output_detail("Bearer " + self.edge("%SECRETFRAGMENT123456 "), "")
        self.assertNotIn("SECRETFRAGMENT", detail)

    def test_a_cut_inside_a_prefix_drops_its_value_too(self):
        for cut in ("B", "Bear", "to", "tok"):
            rest = ("Bearer"[len(cut):] if cut[0] == "B" else "token"[len(cut):])
            with self.subTest(cut=cut):
                value = "SECRETVALUE99" + "x" * 10
                detail = run_supervisor.output_detail(cut + self.edge(f"{rest} {value} "), "")
                self.assertNotIn("SECRETVALUE99", detail)


if __name__ == "__main__":
    unittest.main()
