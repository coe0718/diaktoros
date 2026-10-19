"""The host proves an issue fix's reproduction tests (#484): fail on the old head, pass on the fix.

``repro_proof`` is driven with real ``unittest`` runs in temporary trees (no sandbox needed); the
enforcing entry point, ``RunBroker`` ``open_pr``, is driven with a prover and the ledger; the
reviewer's prompt is built through ``turn_prompts.reproduction_section``.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from diaktoros import broker_ipc, repro_proof  # noqa: E402
from diaktoros.run_supervisor import turn_prompts  # noqa: E402
import test_issue_fix as tif  # noqa: E402

OLD_CODE = "def value():\n    return 1\n"
NEW_CODE = "def value():\n    return 2\n"
TEST = ("import unittest\nimport widget as c\n\n\nclass Case(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(c.value(), 2)\n")
WEAK_TEST = TEST.replace("assertEqual(c.value(), 2)", "assertTrue(c.value() > 0)")


def manifest(files):
    return {"base_head": tif.BASE, "message": "fix",
            "files": [{"path": p, "content_b64": base64.b64encode(d.encode()).decode(),
                       "sha256": hashlib.sha256(d.encode()).hexdigest()}
                      for p, d in files.items()]}


def run_unittest(tree, ids, pythonpath="."):
    import os
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run([sys.executable, "-m", "unittest", "-v", *ids], cwd=tree,
                          capture_output=True, text=True, env={**env, "PYTHONPATH": pythonpath})
    return done.stdout + done.stderr


class Proof(unittest.TestCase):
    def prove(self, test_text, ids=("tests.test_it.Case.test_value",)):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "tests").mkdir(parents=True)
            (base / "tests" / "__init__.py").write_text("")
            (base / "widget.py").write_text(OLD_CODE)
            fix = manifest({"widget.py": NEW_CODE, "tests/test_it.py": test_text})
            return repro_proof.prove(base, fix, list(ids), run_unittest)

    def test_a_test_failing_on_the_old_head_and_passing_on_the_fix_is_proven(self):
        proof = self.prove(TEST)
        self.assertEqual(proof["overall"], repro_proof.PROVEN)
        self.assertEqual(proof["ids"][0]["verdict"], "fail_then_pass")
        text = repro_proof.section(proof)
        self.assertIn("PROVEN", text)
        self.assertIn("original head fail, fix ok", text)

    def test_a_test_passing_on_the_old_head_is_flagged_weak(self):
        proof = self.prove(WEAK_TEST)
        self.assertEqual(proof["overall"], repro_proof.WEAK)
        self.assertEqual(proof["ids"][0]["verdict"], "passes_on_old_head")
        self.assertIn("does not cover the fix", repro_proof.section(proof))

    def test_a_test_that_fails_through_its_subtests_on_the_old_head_is_proven(self):
        # #996: unittest -v prints a failing subTest on its own indented line, with its
        # parameters, and leaves the test's own line without a result; the parser skipped those
        # lines, so a real fail-then-pass read "missing" and the proof said UNPROVEN.
        sub = TEST.replace("        self.assertEqual(c.value(), 2)\n",
                           "        for n in (1, 2):\n            with self.subTest(n=n):\n"
                           "                self.assertEqual(c.value(), 2)\n")
        proof = self.prove(sub)
        self.assertEqual(proof["overall"], repro_proof.PROVEN, proof)
        self.assertEqual((proof["ids"][0]["old"], proof["ids"][0]["new"]), ("fail", "ok"))

    def test_a_test_that_logs_while_it_runs_is_still_read(self):
        # #1005: the code under test logged to stderr, so unittest's "ok" landed alone on the
        # next line; the parser missed it and the proof told the reviewer the test FAILED with
        # the fix.
        loud = TEST.replace("        self.assertEqual(c.value(), 2)\n",
                            "        import sys\n        print('[diaktoros] did a thing', "
                            "file=sys.stderr)\n        self.assertEqual(c.value(), 2)\n")
        proof = self.prove(loud)
        self.assertEqual(proof["overall"], repro_proof.PROVEN, proof)

    def test_a_test_that_prints_ok_and_then_fails_is_never_read_as_ok(self):
        # Review on #1006: the parser took the first bare result after a test's line, so a test
        # printing "ok" before failing read as passing — a false green on the fix.
        fake = TEST.replace("        self.assertEqual(c.value(), 2)\n",
                            "        import sys\n        print('ok', file=sys.stderr)\n"
                            "        self.assertEqual(c.value(), 3)\n")
        proof = self.prove(fake)
        self.assertEqual(proof["ids"][0]["new"], "fail", proof)
        self.assertNotEqual(proof["overall"], repro_proof.PROVEN)

    def test_a_test_file_that_imports_what_the_fix_adds_says_so(self):
        # #1005: on the original head the test module did not import, so its tests ran nowhere;
        # say that, never a bare "missing".
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "tests").mkdir(parents=True)
            (base / "tests" / "__init__.py").write_text("")
            (base / "widget.py").write_text(OLD_CODE)
            test = TEST.replace("import widget as c\n", "import widget as c\nimport added\n")
            fix = manifest({"widget.py": NEW_CODE, "added.py": "X = 1\n",
                            "tests/test_it.py": test})
            proof = repro_proof.prove(base, fix, ["tests.test_it.Case.test_value"], run_unittest)
        self.assertEqual(proof["ids"][0]["old"], repro_proof.IMPORT_ERROR, proof)
        self.assertEqual(proof["overall"], repro_proof.UNPROVEN)
        self.assertIn("did not import on the original head", repro_proof.section(proof))

    # -- the hunk guard (#1043) ---------------------------------------------------------------
    def guard(self, new_code, test=TEST):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "tests").mkdir(parents=True)
            (base / "tests" / "__init__.py").write_text("")
            (base / "widget.py").write_text(OLD_CODE)
            fix = manifest({"widget.py": new_code, "tests/test_it.py": test})
            return repro_proof.prove(base, fix, ["tests.test_it.Case.test_value"], run_unittest)

    def test_a_hunk_the_tests_notice_is_guarded_and_one_they_do_not_is_unguarded(self):
        both = "def extra():\n    return 5\n\n\n" + NEW_CODE        # extra() is untested
        proof = self.guard(both)
        rows = {(h["path"], h["status"]) for h in proof["hunks"]}
        self.assertEqual(rows, {("widget.py", repro_proof.GUARDED),
                                ("widget.py", repro_proof.UNGUARDED)}, proof["hunks"])
        text = repro_proof.section(proof)
        self.assertIn("1 guarded, 1 unguarded, 0 not checked", text)
        self.assertIn("unguarded: \"widget.py\" lines 1-4", text)
        self.assertIn("is a missing test", text)

    def test_comment_only_hunks_are_not_checked_and_a_red_fix_reverts_nothing(self):
        # The comment sits apart from the code change (an unchanged `def` line between), so it
        # is its own hunk; adjacent, difflib folds it into the code hunk and nothing is tested
        # (shadow review of #1048, F4).
        commented = "# a note about value\n\n" + NEW_CODE
        self.assertEqual([h["status"] for h in self.guard(commented)["hunks"]],
                         [repro_proof.GUARDED])                 # the comment is not a hunk
        self.assertEqual(self.guard(NEW_CODE, TEST.replace("2)", "3)"))["hunks"], [])

    def test_the_cap_and_the_time_budget_leave_hunks_not_checked(self):
        both = "def extra():\n    return 5\n\n\n" + NEW_CODE
        with mock.patch.object(repro_proof, "HUNKS_MAX", 1):
            rows = self.guard(both)["hunks"]
        self.assertEqual([h["status"] for h in rows][1:], [repro_proof.NOT_CHECKED])
        self.assertIn("past the first 1 hunks", rows[1]["why"])
        with mock.patch.object(repro_proof, "HUNKS_BUDGET_S", -1.0):
            rows = self.guard(both)["hunks"]
        self.assertEqual({h["status"] for h in rows}, {repro_proof.NOT_CHECKED})
        self.assertIn("not checked", repro_proof.section(
            {"overall": repro_proof.PROVEN, "ids": [], "hunks": rows}))

    def test_a_revert_that_breaks_the_import_is_not_checked_never_guarded(self):
        # Review on #1048 (both seats): #1043 says a revert that doesn't import is "not checked"
        # and never counts as guarded: a broken partial revert proves nothing about coverage.
        adds = NEW_CODE + "\n\nLIMIT = 3\n"
        test = TEST.replace("import widget as c\n", "import widget as c\nfrom widget import LIMIT\n")
        rows = self.guard(adds, test)["hunks"]
        limit = [h for h in rows if h["status"] == repro_proof.NOT_CHECKED]
        self.assertTrue(limit, rows)
        self.assertIn("does not import", limit[0]["why"])
        self.assertNotIn(repro_proof.UNGUARDED, {h["status"] for h in rows})

    def trees(self, tmp, base_files, new_files):
        base, new = Path(tmp) / "base", Path(tmp) / "new"
        for root, files in ((base, base_files), (new, new_files)):
            (root / "tests").mkdir(parents=True)
            (root / "tests" / "__init__.py").write_text("")
            for name, text in files.items():
                (root / name).write_text(text)
        return base, new

    def test_a_deleted_source_file_and_a_deletion_hunk_are_checked_and_labelled(self):
        # Review on #1048: only the fix's tree was walked, so a deleted file was invisible; and a
        # pure deletion was labelled with a line the fix doesn't have.
        with tempfile.TemporaryDirectory() as tmp:
            base, new = self.trees(tmp, {"widget.py": "OLDER = 1\n\n\n" + OLD_CODE,
                                         "helper.py": "def h():\n    return 1\n"},
                                   {"widget.py": NEW_CODE, "tests/test_it.py": TEST})
            (base / "tests" / "test_it.py").write_text(TEST)
            ids = ["tests.test_it.Case.test_value"]
            out = run_unittest(new, ids)
            rows = repro_proof.hunk_guard(base, new, ids, ".", out, run_unittest)
            self.assertFalse((new / "helper.py").exists(), "the reverted deletion was undone")
        got = {(h["path"], h["lines"], h["status"]) for h in rows}
        self.assertIn(("helper.py", "original lines 1-2 (deleted)", repro_proof.UNGUARDED), got)
        self.assertIn(("widget.py", "original lines 1-3 (deleted)", repro_proof.UNGUARDED), got)
        self.assertIn(("widget.py", "lines 2-2", repro_proof.GUARDED), got)

    def test_a_same_size_revert_never_runs_the_fixs_stale_bytecode(self):
        # Found building #1048: `return 2` back to `return 1` is the same size, and Python reuses
        # a .pyc whose source has the same size and mtime, so the revert ran the fix's code and
        # read as unguarded. Pin the mtime on every run to make that certain, not timing luck.
        import os

        def run(tree, ids, pythonpath="."):
            os.utime(Path(tree) / "widget.py", (1_000_000_000, 1_000_000_000))
            return run_unittest(tree, ids, pythonpath)
        with tempfile.TemporaryDirectory() as tmp:
            base, new = self.trees(tmp, {"widget.py": OLD_CODE},
                                   {"widget.py": NEW_CODE, "tests/test_it.py": TEST})
            ids = ["tests.test_it.Case.test_value"]
            rows = repro_proof.hunk_guard(base, new, ids, ".", run(new, ids), run)
        self.assertEqual([h["status"] for h in rows], [repro_proof.GUARDED])

    def test_the_check_selects_every_non_test_python_file_and_nothing_else(self):
        # Review on #1048 (F3): the selection rule, pinned. Every changed .py outside test paths
        # and hidden directories is source, wherever it lives (the plugin reviews any repo, so no
        # directory of this one is hard-coded); tests, specs, hidden dirs and non-Python are not.
        change = ("def f():\n    return 1\n", "def f():\n    return 2\n")
        paths = {"pkg/mod.py": True, "tool.py": True, "docs/site/build.py": True,
                 "tests/test_x.py": False, "pkg/test_mod.py": False, "spec/a.py": False,
                 "pkg/a_test.py": False, ".github/scripts/s.py": False, "README.md": False,
                 "pkg/data.json": False}
        with tempfile.TemporaryDirectory() as tmp:
            base, new = Path(tmp) / "base", Path(tmp) / "new"
            for root, text in ((base, change[0]), (new, change[1])):
                for path in paths:
                    (root / path).parent.mkdir(parents=True, exist_ok=True)
                    (root / path).write_text(text)
            selected = {h["path"] for h in repro_proof.source_hunks(base, new)}
        self.assertEqual(selected, {p for p, source in paths.items() if source})

    def test_with_no_test_named_the_changed_test_files_are_used(self):
        # Shadow review of #1048: #1043 falls back to the test files the fix changed; prove()
        # returned "none named" before the hunk check, so such a fix got no check at all.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "tests").mkdir(parents=True)
            (base / "tests" / "__init__.py").write_text("")
            (base / "widget.py").write_text(OLD_CODE)
            both = "def extra():\n    return 5\n\n\n" + NEW_CODE
            proof = repro_proof.prove(base, manifest({"widget.py": both, "tests/test_it.py": TEST}),
                                      [], run_unittest)
        self.assertEqual(proof["overall"], repro_proof.NONE_NAMED)
        self.assertEqual(proof["hunk_tests"], ["tests.test_it"])
        self.assertEqual(sorted(h["status"] for h in proof["hunks"]),
                         [repro_proof.GUARDED, repro_proof.UNGUARDED])
        self.assertIn("against the test files the fix changed", repro_proof.section(proof))

    def test_with_no_test_every_source_hunk_is_listed_past_the_cap(self):
        # Review on #1092 (F2, P1): the no-test fallback cut the list at HUNKS_MAX, and the rest
        # vanished from the proof. Nothing runs there, so every hunk is listed; the section shows
        # the first ones by line and counts the rest.
        many = 25
        old = "".join(f"def f{n}():\n    return 1\n\n\n" for n in range(many))
        new = "".join(f"def f{n}():\n    return 2\n\n\n" for n in range(many))
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            base.mkdir()
            (base / "mod.py").write_text(old)
            proof = repro_proof.prove(base, manifest({"mod.py": new}), [],
                                      lambda *a: self.fail("nothing to run"))
        self.assertEqual(len(proof["hunks"]), many)
        self.assertEqual({h["status"] for h in proof["hunks"]}, {repro_proof.UNGUARDED})
        text = repro_proof.section(proof)
        self.assertIn(f"0 guarded, {many} unguarded, 0 not checked", text)
        self.assertIn(f"and {many - repro_proof.UNGUARDED_SHOWN} more unguarded hunk(s)", text)

    def test_the_fallback_takes_both_test_naming_conventions_and_no_helpers(self):
        # Review on #1092 (P1): `*_test.py` is a test file to TEST_PATH, but the fallback took
        # only `test*`, then said no test changed. A helper under tests/ runs no test and stays out.
        files = {"tests/test_a.py": "", "pkg/b_test.py": "", "tests/helpers.py": "",
                 "tests/conftest.py": "", "widget.py": NEW_CODE}
        self.assertEqual(repro_proof.changed_test_modules(manifest(files)),
                         ["pkg.b_test", "tests.test_a"])
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            base.mkdir()
            (base / "widget.py").write_text(OLD_CODE)
            proof = repro_proof.prove(base, manifest({"widget.py": NEW_CODE,
                                                      "widget_test.py": TEST}), [], run_unittest)
        self.assertEqual(proof["hunk_tests"], ["widget_test"])
        self.assertEqual([h["status"] for h in proof["hunks"]], [repro_proof.GUARDED])

    def fallback(self, test_text, new_code=None):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "tests").mkdir(parents=True)
            (base / "tests" / "__init__.py").write_text("")
            (base / "widget.py").write_text(OLD_CODE)
            code = new_code or ("def extra():\n    return 5\n\n\n" + NEW_CODE)
            return repro_proof.prove(base, manifest({"widget.py": code,
                                                     "tests/test_it.py": test_text}),
                                     [], run_unittest)

    def test_a_skipped_test_in_a_changed_module_does_not_void_the_fallback(self):
        # Shadow review of #1092 (F3): the module ran whole, one test was skipped, the module read
        # "skipped" not "ok", and the check silently ran nothing with a false reason.
        skipped = TEST + ("\n    @unittest.skip('not on this platform')\n"
                          "    def test_other(self):\n        pass\n")
        proof = self.fallback(skipped)
        self.assertEqual(sorted(h["status"] for h in proof["hunks"]),
                         [repro_proof.GUARDED, repro_proof.UNGUARDED], proof)
        self.assertNotIn("hunk_why", proof)

    def test_a_module_whose_tests_are_all_skipped_checks_nothing(self):
        # Nothing ran, so nothing could notice a revert: not "unguarded", and said so.
        all_skipped = TEST.replace("    def test_value(self):",
                                   "    @unittest.skip('off')\n    def test_value(self):")
        proof = self.fallback(all_skipped)
        self.assertEqual(proof["hunks"], [])
        self.assertIn("do not all pass", proof["hunk_why"])

    def test_the_fallback_wording_names_the_changed_tests_not_named_ones(self):
        text = repro_proof.section(self.fallback(TEST))
        self.assertIn("so no fail-then-pass proof was run", text)          # not "nothing was run"
        self.assertIn("ran the fix's changed tests", text)
        self.assertIn("fails none of the fix's changed tests", text)
        self.assertNotIn("the named tests", text)

    def test_a_diff_manifest_names_its_changed_test_files(self):
        # Shadow review of #1092 (I3): the patch_b64 manifest shape was untested.
        import base64 as b64
        import hashlib as hl
        patch = ("diff --git a/tests/test_p.py b/tests/test_p.py\n--- a/tests/test_p.py\n"
                 "+++ b/tests/test_p.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
                 "diff --git a/pkg/q_test.py b/pkg/q_test.py\n--- a/pkg/q_test.py\n"
                 "+++ b/pkg/q_test.py\n@@ -1 +1 @@\n-y = 1\n+y = 2\n"
                 "diff --git a/widget.py b/widget.py\n--- a/widget.py\n+++ b/widget.py\n"
                 "@@ -1 +1 @@\n-a\n+b\n").encode()
        diff = {"base_head": tif.BASE, "message": "fix",
                "patch_b64": b64.b64encode(patch).decode(), "sha256": hl.sha256(patch).hexdigest()}
        self.assertEqual(repro_proof.changed_test_modules(diff), ["pkg.q_test", "tests.test_p"])

    def test_with_no_test_named_or_changed_every_source_hunk_is_unguarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            base.mkdir()
            (base / "widget.py").write_text(OLD_CODE)
            proof = repro_proof.prove(base, manifest({"widget.py": NEW_CODE}), [],
                                      lambda *a: self.fail("nothing to run"))
        self.assertEqual([h["status"] for h in proof["hunks"]], [repro_proof.UNGUARDED])
        text = repro_proof.section(proof)
        self.assertIn("names no reproduction test and changes no test file", text)
        self.assertIn("1 unguarded", text)

    def test_a_hunk_run_that_raises_is_not_checked_and_keeps_the_proof(self):
        # Shadow review of #1048: a revert that hangs the tests (a guard or a loop bound) raised
        # out of prove(), and the broker showed the reviewer "NOT RUN" for the whole proof.
        calls = []

        def run(tree, ids, pythonpath="."):
            calls.append(1)
            if len(calls) > 2:                      # the old and new runs pass through
                raise subprocess.TimeoutExpired("unittest", 300)
            return run_unittest(tree, ids, pythonpath)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "tests").mkdir(parents=True)
            (base / "tests" / "__init__.py").write_text("")
            (base / "widget.py").write_text(OLD_CODE)
            fix = manifest({"widget.py": NEW_CODE, "tests/test_it.py": TEST})
            proof = repro_proof.prove(base, fix, ["tests.test_it.Case.test_value"], run)
        self.assertEqual(proof["overall"], repro_proof.PROVEN)
        self.assertEqual([(h["status"], h["why"]) for h in proof["hunks"]],
                         [(repro_proof.NOT_CHECKED,
                           "the tests did not finish with it reverted (TimeoutExpired)")])

    def test_a_test_failing_on_the_fix_or_missing_is_unproven(self):
        self.assertEqual(self.prove(TEST.replace("2)", "3)"))["overall"], repro_proof.UNPROVEN)
        self.assertEqual(self.prove(TEST, ("tests.test_it.Case.test_nope",))["overall"],
                         repro_proof.UNPROVEN)

    def prove_plain(self, ids):
        """#893: this repository's own layout. tests/ is a plain directory (no __init__.py) whose
        modules import a helper from it, so `python -m unittest tests.test_it...` cannot import,
        and -v prints `test_it.Case.test_value`. The fixer may name either spelling."""
        helper = "import os\n"
        plain = "import _helper  # noqa: F401\n" + TEST
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "tests").mkdir(parents=True)
            (base / "tests" / "_helper.py").write_text(helper)
            (base / "widget.py").write_text(OLD_CODE)
            fix = manifest({"widget.py": NEW_CODE, "tests/test_it.py": plain})
            return repro_proof.prove(base, fix, list(ids), run_unittest)

    def test_both_spellings_of_an_id_in_a_plain_tests_directory_are_proven(self):
        for wanted in ("tests.test_it.Case.test_value", "test_it.Case.test_value",
                       "tests.test_it.Case", "test_it"):
            with self.subTest(wanted=wanted):
                proof = self.prove_plain([wanted])
                self.assertEqual(proof["overall"], repro_proof.PROVEN, proof)
                self.assertEqual(proof["ids"][0]["id"], wanted)      # the fixer's words, kept
        self.assertIn('(ran as "test_it.Case.test_value")',
                      repro_proof.section(self.prove_plain(["tests.test_it.Case.test_value"])))

    def test_a_unique_bare_method_name_is_found_and_proven(self):
        # #893's "accept a bare method name when it is unique" (review on #898).
        proof = self.prove_plain(["test_value"])
        self.assertEqual(proof["overall"], repro_proof.PROVEN, proof)
        self.assertEqual(proof["ids"][0]["ran_as"], "test_it.Case.test_value")

    def test_a_bare_method_name_two_classes_define_is_not_guessed(self):
        twice = ("import _helper  # noqa: F401\n" + TEST + "\n\nclass Other(unittest.TestCase):\n"
                 "    def test_value(self):\n        self.assertEqual(c.value(), 2)\n")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "base"
            (base / "tests").mkdir(parents=True)
            (base / "tests" / "_helper.py").write_text("import os\n")
            (base / "widget.py").write_text(OLD_CODE)
            fix = manifest({"widget.py": NEW_CODE, "tests/test_it.py": twice})
            proof = repro_proof.prove(base, fix, ["test_value"], run_unittest)
        self.assertEqual(proof["overall"], repro_proof.UNRESOLVED)

    def test_an_id_matching_no_test_file_is_not_found_never_unproven(self):
        proof = self.prove_plain(["tests.test_nothing.Case.test_value"])
        self.assertEqual(proof["overall"], repro_proof.UNRESOLVED)
        self.assertEqual((proof["ids"][0]["old"], proof["ids"][0]["verdict"]),
                         (repro_proof.NOT_FOUND, "id_not_found"))
        self.assertIn("ID NOT FOUND", repro_proof.section(proof))
        self.assertNotIn("UNPROVEN", repro_proof.section(proof))
        # Beside a real failure it is still unproven: the real failure is what matters.
        both = self.prove_plain(["tests.test_nothing.C.t", "test_it.Case.test_nope"])
        self.assertEqual(both["overall"], repro_proof.UNPROVEN)

    def test_none_named_is_reported_as_such(self):
        proof = repro_proof.prove(Path("."), manifest({"a.py": "x"}), [], run_unittest)
        self.assertEqual(proof["overall"], repro_proof.NONE_NAMED)
        self.assertIn("NONE NAMED", repro_proof.section(proof))

    def test_ids_are_validated(self):
        self.assertEqual(repro_proof.valid_ids(None), [])
        for bad in ("x", ["a b"], ["--help"], ["a.b", "a.b"], ["a"] * 9, [1]):
            with self.assertRaises(ValueError):
                repro_proof.valid_ids(bad)

    def test_the_proof_is_stale_text_for_a_later_head(self):
        text = repro_proof.section(repro_proof.none_named(), "a" * 40, "b" * 40)
        self.assertIn("later commits are not covered", text)


class Broker(tif.Base):
    """``open_pr`` through ``RunBroker._dispatch``: the proof is run and recorded."""

    def setUp(self):
        super().setUp()
        self.sup, self.run_id = self.fix_row()
        self.pushed = []
        self.calls = []

        def api(loop, path, *, method="GET", body=None, login=None, **kw):
            self.calls.append((path, method, body, login))
            if method == "POST" and path.endswith("/pulls"):
                return {"number": 40, "head": {"ref": "diaktoros/issue-12"}}
            if method == "POST":
                return {"number": 40}
            if path == f"/repos/{tif.REPO}/issues/12":
                return tif.issue()
            return {}
        from diaktoros import broker, config, gh, safe_push
        for target, name, value in (
                (gh, "api", mock.Mock(side_effect=api)),
                (config, "by_repo", mock.Mock(side_effect=lambda r: self.loop)),
                (safe_push, "open_branch",
                 mock.Mock(side_effect=lambda loop, **kw: {"new_head": "d" * 40, "login": "fix"}))):
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.broker = broker
        self.scope = broker_ipc.RunScope(tif.REPO, 12, tif.BASE, "issue_fixer",
                                         "diaktoros/issue-12", self.run_id, str(self.db))

    def server(self, prover=None):
        return broker_ipc.RunBroker(self.loop, self.scope, self.root, prover=prover)

    def request(self, **extra):
        data = b"fixed\n"
        return json.dumps({"operation": "open_pr", "title": "Fix", "body": "Fixed.",
                           "manifest": {"base_head": tif.BASE, "message": "m", "files": [
                               {"path": "README.md", "content_b64": base64.b64encode(data).decode(),
                                "sha256": hashlib.sha256(data).hexdigest()}]}, **extra}).encode()

    def stored(self):
        return self.sup.repro_proof_for_pr(tif.REPO, 40)

    def test_the_named_tests_are_proven_and_the_result_is_kept(self):
        seen = []

        def prover(m, ids):
            seen.append(ids)
            return {"overall": "proven", "ids": [{"id": ids[0], "old": "fail", "new": "ok",
                                                  "verdict": "fail_then_pass"}]}
        self.server(prover)._dispatch(self.request(repro=["tests.test_x.Case.test_y"]))
        self.assertEqual(seen, [["tests.test_x.Case.test_y"]])
        proof, fix_head = self.stored()
        self.assertEqual((proof["overall"], fix_head), ("proven", "d" * 40))

    def test_a_weak_test_is_kept_as_weak_and_the_pr_still_opens(self):
        weak = {"overall": "weak", "ids": [{"id": "a.b", "old": "ok", "new": "ok",
                                            "verdict": "passes_on_old_head"}]}
        self.server(lambda m, ids: weak)._dispatch(self.request(repro=["a.b"]))
        self.assertEqual(self.stored()[0]["overall"], "weak")
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "requested")

    def test_none_named_still_runs_the_hunk_check_and_is_recorded(self):
        # #1043 (shadow review of #1048): with nothing named the prover still runs, with no ids,
        # so the hunk check falls back to the fix's changed test files.
        seen = []

        def prover(manifest, ids):
            seen.append(ids)
            return {"overall": "none_named", "ids": [], "hunks": [{"status": "unguarded"}]}
        self.server(prover)._dispatch(self.request())
        self.assertEqual(seen, [[]])
        self.assertEqual(self.stored()[0]["hunks"], [{"status": "unguarded"}])
        self.setUp()
        self.server(None)._dispatch(self.request())             # no sandbox: none named
        self.assertEqual(self.stored()[0]["overall"], "none_named")
        self.setUp()
        broken = mock.Mock(side_effect=RuntimeError("no bwrap"))
        self.server(broken)._dispatch(self.request())           # a broken one never blocks
        self.assertEqual(self.stored()[0]["overall"], "none_named")

    def test_without_a_sandbox_or_when_it_breaks_the_record_says_unavailable(self):
        self.server(None)._dispatch(self.request(repro=["a.b"]))
        self.assertEqual(self.stored()[0]["overall"], "unavailable")

    def test_a_prover_that_raises_never_blocks_the_pr(self):
        def broken(m, ids):
            raise RuntimeError("no bwrap")
        self.server(broken)._dispatch(self.request(repro=["a.b"]))
        proof = self.stored()[0]
        self.assertEqual(proof["overall"], "unavailable")
        self.assertIn("no bwrap", proof["why"])
        self.assertEqual(self.sup.issue_fix_result(self.run_id)["state"], "requested")

    def test_bad_ids_are_refused_before_the_write_is_spent(self):
        server = self.server(mock.Mock())
        with self.assertRaises(broker_ipc.ProtocolError):
            server._dispatch(self.request(repro=["--help"]))
        self.assertFalse(server._used)
        self.assertIsNone(self.stored())


class Prompt(tif.Base):
    def test_the_reviewer_is_shown_the_proof_for_the_pr(self):
        sup, run_id = self.fix_row()
        sup.record_issue_fix(run_id, tif.REPO, 12, tif.BASE, "pr", "diaktoros/issue-12")
        sup.issue_fix_status(run_id, "requested", new_head="d" * 40, pr_number=40)
        sup.record_repro_proof(run_id, tif.REPO, 12, tif.BASE, json.dumps(
            {"overall": "weak", "ids": [{"id": "a.b", "old": "ok", "new": "ok",
                                         "verdict": "passes_on_old_head"}]}))
        row = {"repo": tif.REPO, "pr": 40, "head": "d" * 40}
        with mock.patch("diaktoros.run_supervisor.production_ledger", return_value=self.db):
            text = turn_prompts.reproduction_section(self.loop, row)
            other = turn_prompts.reproduction_section(self.loop, {**row, "pr": 41})
        self.assertIn("WEAK", text)
        self.assertEqual(other, "")

    def test_the_fixer_is_told_to_name_its_reproduction(self):
        from diaktoros import prompts, trusted_turn
        self.assertIn("--repro", prompts.ISOLATED_ISSUE_FIX)
        self.assertIn("--repro", trusted_turn.tool_instructions("issue_fixer"))


if __name__ == "__main__":
    unittest.main()
