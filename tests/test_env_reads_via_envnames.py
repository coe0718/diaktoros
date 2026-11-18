"""Every ``DIAKTOROS_``/``REVIEW_LOOP_`` environment read goes through ``envnames`` (#2300).

#542 was a variable read by its new name only, which missed installs still on the old
``REVIEW_LOOP_`` spelling. ``diaktoros/envnames.py`` reads both, so a direct
``os.environ.get(...)``, ``os.environ[...]`` (a read or an augmented assignment) or
``os.getenv(...)``, positional or by keyword, whose key is a literal with either prefix, an
``envnames.name(...)`` value, or a name bound to one (``X_ENV = envnames.name("X")``, also read
as ``module.X_ENV``), fails this scan anywhere in the repository's own Python (``_sources``:
``diaktoros/``, ``scripts/`` and the rest, #1489) but ``envnames.py`` itself. Python source held
in a string constant or an f-string (a script run in a child process) is scanned too; a fragment
that does not parse is searched by pattern rather than dropped. ``ALLOWED`` names the few reads
that must stay direct, each with its reason, by file, variable and exact count, and only inside
a generated script: an ordinary read of the same name in the same file still fails.

The scan is ``tools/env_reads.py`` (with ``tools/env_read_scripts.py``); see there why it is not
in this file. ``ALLOWED`` is here: it is what this test asserts of the repository.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# The scan lives in tools/env_reads.py and tools/env_read_scripts.py, outside tests/, so the
# host's base-failure proof runs the tests below against the base's scan, not this PR's (#1973,
# #2536 review F1).
sys.path.insert(0, str(ROOT / "tools"))

import _sources  # noqa: E402  every *.py the scans read (#1489)
import env_read_scripts  # noqa: E402
import env_reads  # noqa: E402
from env_reads import CODE, SCRIPT, direct_reads  # noqa: E402

# (file, variable) -> (reads, why the read stays direct). Each is inside a script generated as a
# string and run by a bare interpreter in a child process, which cannot import ``envnames``.
# Only reads of kind SCRIPT are allowed, and only exactly this many.
ALLOWED = {
    ("diaktoros/trace.py", "DIAKTOROS_GH_STUB"): (
        1, "_HARNESS runs in a child process (python -c); it reads both spellings by hand"),
    ("diaktoros/trace.py", "REVIEW_LOOP_GH_STUB"): (
        1, "_HARNESS runs in a child process (python -c); it reads both spellings by hand"),
    ("diaktoros/safe_push.py", "DIAKTOROS_TOKEN_FILE"): (
        1, "_ASKPASS is Git's credential prompt, a child-process script; safe_push itself sets "
           "this one spelling in that child's environment, so there is no old name to miss"),
}


def scan(root: Path = _sources.ROOT) -> dict[tuple[str, str, str], int]:
    """``env_reads.scan`` over the repository's own Python (``_sources``) under ``root``."""
    return env_reads.scan(root, _sources.python_sources(root))


def stray(hits) -> list:
    """The reads ``ALLOWED`` does not cover: any ordinary read, a script read not listed, or a
    listed one read more often than listed."""
    return sorted((entry, n) for entry, n in hits.items()
                  if entry[2] != SCRIPT or ALLOWED.get(entry[:2], (0,))[0] != n)


class EnvReadsViaEnvnames(unittest.TestCase):
    def test_only_allowlisted_direct_reads(self):
        self.assertEqual(stray(scan()), [], "read these through diaktoros.envnames.get (both "
                         "spellings, #542), or add them to ALLOWED with the reason they cannot")

    def test_allowlist_has_no_stale_entries(self):
        found = {entry[:2] for entry in scan() if entry[2] == SCRIPT}
        self.assertEqual(sorted(set(ALLOWED) - found), [],
                         "these ALLOWED reads are gone: drop them from the list")

    def test_every_read_shape_is_caught(self):
        cases = {
            'os.environ.get("DIAKTOROS_X")': "DIAKTOROS_X",
            "os.environ['REVIEW_LOOP_X']": "REVIEW_LOOP_X",
            'os.getenv("DIAKTOROS_X", "")': "DIAKTOROS_X",
            'os.getenv(key="DIAKTOROS_X")': "DIAKTOROS_X",
            'os.environ.get(key="DIAKTOROS_X")': "DIAKTOROS_X",
            'os.environ["DIAKTOROS_X"] += "x"': "DIAKTOROS_X",
            'os.environ.get(envnames.name("X"))': "DIAKTOROS_X",
            'os.environ[envnames.name("X")]': "DIAKTOROS_X",
            'X_ENV = envnames.name("X")\nos.environ.get(X_ENV)': "DIAKTOROS_X",
            # F1 (round 2): an annotated binding is a binding too.
            'X_ENV: str = envnames.name("X")\nos.environ.get(X_ENV)': "DIAKTOROS_X",
            'X_ENV: str = "DIAKTOROS_X"\nos.getenv(X_ENV)': "DIAKTOROS_X",
            'if (X_ENV := envnames.name("X")):\n    os.environ[X_ENV]': "DIAKTOROS_X",
            'os.environ.get(gate_failures.REDRIVE_ENV)': "DIAKTOROS_GATE_REDRIVE",
        }
        bound = {"REDRIVE_ENV": "DIAKTOROS_GATE_REDRIVE"}
        for source, key in cases.items():
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source, bound), [(key, CODE)])

    def test_reads_in_generated_scripts_are_caught(self):
        cases = {
            'SCRIPT = "import os\\nprint(os.environ[\'DIAKTOROS_X\'])\\n"': "DIAKTOROS_X",
            'SCRIPT = ("import os\\n"\n          "v = os.getenv(\'REVIEW_LOOP_X\')\\n")': "REVIEW_LOOP_X",
            # An interpolation that splits a statement: no fragment parses alone (F4).
            "S = f\"value = os.getenv('DIAKTOROS_X')\\nprint({42})\\n\"": "DIAKTOROS_X",
            "S = f\"v = [os.getenv('DIAKTOROS_X'), {n}\"": "DIAKTOROS_X",   # unparsable script
            # F4 (round 2): an interpolated name; ``0`` alone would not parse, and the pattern
            # search used to miss an envnames.name key.
            "S = f\"{prefix} = os.getenv(envnames.name('X'))\\n\"": "DIAKTOROS_X",
            "S = f\"v = os.environ[envnames.name('X')] + {s}\\n\"": "DIAKTOROS_X",
            "S = f\"v = [os.environ.get(envnames.name('X')), {n}\"": "DIAKTOROS_X",  # unparsable
            # #2376: an interpolated newline un-comments the read; every placeholder version
            # parses with the read inside a comment.
            "prefix = \"\\n\"\nS = f\"#{prefix}os.getenv('DIAKTOROS_X')\\n\"": "DIAKTOROS_X",
            # Found by both the parse and the pattern: still counted once.
            "S = f\"{indent}v = os.getenv('DIAKTOROS_X')\\n\"": "DIAKTOROS_X",
            # An indented script (dedented before parsing) with the #2376 shape.
            "S = f\"\"\"\n    import os\n    #{prefix}os.getenv('DIAKTOROS_X')\n\"\"\"": "DIAKTOROS_X",
        }
        for source, key in cases.items():
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source), [(key, SCRIPT)])

    def test_a_comment_no_interpolation_can_end_passes(self):
        # Round 1 F3: the interpolation is on an earlier line (or after the read), so the
        # example stays commented out whatever it holds; the generated script reads nothing.
        for source in ("x = 123\nS = f\"v = {x}\\n# os.getenv('DIAKTOROS_X')\\n\"",
                       "S = f\"v = {x}  # os.getenv('DIAKTOROS_X') {y}\\n\"",
                       "S = f'v = \"#{x}os.getenv(\\'DIAKTOROS_X\\')\"\\n'"):
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source), [])

    def test_reads_that_are_fine_pass(self):
        for source in ('envnames.get("X")', 'os.environ.get("HOME")',
                       '[os.environ.get(n) for n in envnames.both("X")]',
                       '{**os.environ, envnames.name("X"): "1"}',
                       'X_ENV = envnames.name("X")\nenv[X_ENV] = "1"',
                       'os.environ["DIAKTOROS_X"] = "1"', 'print("DIAKTOROS_X")'):
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source), [])

    def test_writes_in_generated_scripts_pass(self):
        # F4 (round 2): a generated script that only sets the variable reads nothing, whether
        # its interpolation sits where ``0`` would not parse or the script does not parse at all.
        for source in ("S = f\"{name} = 1\\nos.environ['DIAKTOROS_X'] = {value!r}\\n\"",
                       "S = f\"{name} = os.environ\\nos.environ['DIAKTOROS_X'] = '1'\\n\"",
                       "S = f\"v = [{n}\\nos.environ['DIAKTOROS_X'] = '1'\\n\""):
            with self.subTest(source=source):
                self.assertEqual(direct_reads(source), [])

    def test_an_annotated_bound_name_fails_the_scan(self):
        # F1 (round 2), through the scan the guard runs: ``X_ENV: str = envnames.name(...)``.
        hits = self.scan_tree({"diaktoros/a.py": 'import os\nfrom . import envnames\n'
                               'A_ENV: str = envnames.name("A")\nv = os.environ.get(A_ENV)\n'})
        self.assertEqual(stray(hits), [(("diaktoros/a.py", "DIAKTOROS_A", CODE), 1)])

    def scan_tree(self, files: dict[str, str]):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, text in {"diaktoros/envnames.py": 'os.environ.get("DIAKTOROS_X")\n',
                          **files}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        return scan(root)

    def test_a_read_uncommented_by_an_interpolation_fails_the_scan(self):
        # #2376 (and its duplicate #2371), through the scan the guard runs: every placeholder
        # parse of this f-string leaves the read in a comment, but the generated script runs it.
        hits = self.scan_tree({"scripts/gen.py": 'import os\nprefix = "\\n"\n'
                               'S = f"#{prefix}os.getenv(\'DIAKTOROS_X\')\\n"\n'})
        self.assertEqual(stray(hits), [(("scripts/gen.py", "DIAKTOROS_X", SCRIPT), 1)])

    def test_an_uncommented_read_beside_a_bound_name_read_counts_twice(self):
        # Round 1 F2: the parse counts ``os.getenv(X)`` through the bound ``X``; the read an
        # interpolated newline un-comments is a second read of the same variable, and must not
        # hide behind the allowlisted single read.
        source = ('import os\nprefix = "\\n"\n'
                  'S = f"X = \'DIAKTOROS_GH_STUB\'\\nos.getenv(X)\\n'
                  '#{prefix}os.getenv(\'DIAKTOROS_GH_STUB\')\\n"\n')
        hits = self.scan_tree({"diaktoros/trace.py": source})
        self.assertEqual(stray(hits), [(("diaktoros/trace.py", "DIAKTOROS_GH_STUB", SCRIPT), 2)])

    def test_a_comment_no_interpolation_can_end_passes_the_scan(self):
        # Round 1 F3, through the scan: a permanently commented-out example is not a read.
        hits = self.scan_tree({"scripts/gen.py": 'x = 123\n'
                               'S = f"v = {x}\\n# os.getenv(\'DIAKTOROS_X\')\\n"\n'})
        self.assertEqual(stray(hits), [])

    def test_a_read_a_hole_lets_out_of_a_string_fails_the_scan(self):
        # Round 2 F5: ``prefix`` closes the string and ``suffix`` comments out its leftover
        # quote, so the generated script runs the read; every placeholder parse keeps it inside
        # the string literal.
        for gen in ('prefix = \'"\\n\'\nsuffix = "\\n#"\n'
                    'S = f\'v = "#{prefix}os.getenv(\\\'DIAKTOROS_X\\\'){suffix}"\\n\'\n',
                    'S = f\'v = "{prefix}os.getenv(\\\'DIAKTOROS_X\\\')  # "\\n\'\n',
                    'S = f\'\'\'v = """{prefix}\nos.getenv("DIAKTOROS_X")\n"""\n\'\'\'\n'):
            with self.subTest(gen=gen):
                hits = self.scan_tree({"scripts/gen.py": gen})
                self.assertEqual(stray(hits), [(("scripts/gen.py", "DIAKTOROS_X", SCRIPT), 1)])

    def test_a_string_no_interpolation_can_open_passes_the_scan(self):
        # Round 2 F5, the other side: a string example stays a string when no interpolation sits
        # before the read inside it, or when closing it would leave an unterminated quote. (A
        # literal whose value is itself a read is a nested script and counts, as before: these
        # hold the read in the nested script's comment.)
        for gen in ('S = f\'{x}\\nv = "# os.getenv(\\\'DIAKTOROS_X\\\')"\\n\'\n',
                    'S = f\'v = "#{x}os.getenv(\\\'DIAKTOROS_X\\\')"\\n\'\n',
                    'S = f\'v = "# os.getenv(\\\'DIAKTOROS_X\\\')" + "{x}"\\n\'\n'):
            with self.subTest(gen=gen):
                self.assertEqual(stray(self.scan_tree({"scripts/gen.py": gen})), [])

    def test_another_objects_getenv_passes_the_scan(self):
        # Round 2 F4: only ``os.getenv`` / ``os.environ`` reads count, as in ``_read_key``;
        # the pattern used on un-commented text must not take ``other.getenv(...)`` for one.
        for gen in ('prefix = "\\n"\nS = f"#{prefix}other.getenv(\'DIAKTOROS_X\')\\n"\n',
                    'S = f"#{prefix}self.environ.get(\'DIAKTOROS_X\')\\n"\n',
                    'S = f"#{prefix}cfg.environ[\'DIAKTOROS_X\']\\n"\n',
                    'S = f"v = [other.getenv(\'DIAKTOROS_X\'), {n}"\n'):    # unparsable script
            with self.subTest(gen=gen):
                self.assertEqual(stray(self.scan_tree({"scripts/gen.py": gen})), [])
        hits = self.scan_tree({"scripts/gen.py": 'S = f"#{p}os.environ.get(\'DIAKTOROS_X\')\\n"\n'})
        self.assertEqual(stray(hits), [(("scripts/gen.py", "DIAKTOROS_X", SCRIPT), 1)])

    # #2420: another object's ``environ`` behind a dot then spaces, as generated scripts.
    SPACED = ('prefix = "\\n"\nS = f"#{prefix}self . environ.get(\'DIAKTOROS_X\')\\n"\n',
              'S = f"#{prefix}cfg.\\tenviron[\'DIAKTOROS_X\']\\n"\n',
              'S = "self . environ.get(\'DIAKTOROS_X\') ((("\n',      # unparsable script
              'S = f"v = [self. environ.get(\'DIAKTOROS_X\'), {n}"\n')  # unparsable

    def test_another_objects_spaced_environ_passes_the_scan(self):
        # #2420: spaces between the receiver's dot and ``environ`` (``self . environ``) hid the
        # dot from the pattern, which then took the read for ``os.environ``; the parse does not.
        for gen in self.SPACED:
            with self.subTest(gen=gen):
                self.assertEqual(stray(self.scan_tree({"scripts/gen.py": gen})), [])
        # A name before the spaces is not a receiver, and ``os . environ`` is still a read.
        for gen in ('S = f"#{p}os . environ.get(\'DIAKTOROS_X\')\\n"\n',
                    'S = "return  environ.get(\'DIAKTOROS_X\') ((("\n',
                    'S = "v = os . getenv(\'DIAKTOROS_X\') ((("\n'):
            with self.subTest(gen=gen):
                self.assertEqual(stray(self.scan_tree({"scripts/gen.py": gen})),
                                 [(("scripts/gen.py", "DIAKTOROS_X", SCRIPT), 1)])

    def test_the_spaced_dot_rule_is_needed(self):
        # #2536 review F1: undone, each ``SPACED`` script is a read. (The base proof runs the
        # base's scan from tools/ now; this pins what #2420's rule does.)
        for gen in self.SPACED:
            with self.subTest(gen=gen):
                files = {"scripts/gen.py": gen}
                self.assertEqual(stray(self.scan_tree(files)), [])
                with mock.patch.object(env_read_scripts, "_dot_across_spaces",
                                       lambda source, offset: False):
                    self.assertEqual(stray(self.scan_tree(files)),
                                     [(("scripts/gen.py", "DIAKTOROS_X", SCRIPT), 1)])

    def test_a_base_proof_tests_the_base_scan(self):
        # #2536 review F1: the scan lives off the test paths, so the host's base-failure proof
        # (change_proof.apply_tests) runs the head's tests here against the base's scan. It fails
        # if either module goes back into a tests/ module, where the scan was.
        import _base_proof
        for module in (env_reads, env_read_scripts):
            with self.subTest(module=module.__name__):
                _base_proof.assert_taken_from_the_base(self, module.__file__)

    def test_a_new_direct_read_fails_the_scan(self):
        hits = self.scan_tree({"scripts/new.py": 'import os\nos.environ.get("DIAKTOROS_X")\n'})
        self.assertEqual(hits, {("scripts/new.py", "DIAKTOROS_X", CODE): 1})
        self.assertTrue(stray(hits))

    def test_a_bound_name_read_from_another_module_fails_the_scan(self):
        hits = self.scan_tree({
            "diaktoros/a.py": 'from . import envnames\nA_ENV = envnames.name("A")\n',
            "diaktoros/b.py": 'import os\nfrom . import a\nv = os.environ.get(a.A_ENV)\n'})
        self.assertEqual(stray(hits), [(("diaktoros/b.py", "DIAKTOROS_A", CODE), 1)])

    def test_an_ordinary_read_of_an_allowlisted_name_fails_the_scan(self):
        # F2: the allowlisted script read stays allowed; a second, ordinary read does not.
        harness = "_HARNESS = '''\nimport os\nstub = os.environ.get(\"DIAKTOROS_GH_STUB\")\n'''\n"
        hits = self.scan_tree({"diaktoros/trace.py": harness})
        self.assertEqual(stray(hits), [])
        hits = self.scan_tree({"diaktoros/trace.py": harness
                               + 'import os\nx = os.environ.get("DIAKTOROS_GH_STUB")\n'})
        self.assertEqual(stray(hits), [(("diaktoros/trace.py", "DIAKTOROS_GH_STUB", CODE), 1)])
        hits = self.scan_tree({"diaktoros/trace.py": harness + "_MORE = '''\nimport os\n"
                               "y = os.environ.get(\"DIAKTOROS_GH_STUB\")\n'''\n"})
        self.assertEqual(stray(hits), [(("diaktoros/trace.py", "DIAKTOROS_GH_STUB", SCRIPT), 2)])


class OldSpellingIsRead(unittest.TestCase):
    """The reads F1 found by a bound name now go through ``envnames.get``: either spelling."""

    def env(self, **values):
        clean = {k: v for k, v in os.environ.items()
                 if not k.endswith(("_WORKER", "_GATE_REDRIVE"))}
        return mock.patch.dict(os.environ, {**clean, **values}, clear=True)

    def test_worker_mark_in_either_spelling(self):
        from diaktoros import hostdirs
        for name in ("DIAKTOROS_WORKER", "REVIEW_LOOP_WORKER"):
            with self.subTest(name=name), self.env(**{name: "1"}):
                self.assertTrue(hostdirs.in_worker())
        with self.env():
            self.assertFalse(hostdirs.in_worker())

    def test_redrive_mark_in_either_spelling(self):
        from diaktoros import app_ingress
        for name in ("DIAKTOROS_GATE_REDRIVE", "REVIEW_LOOP_GATE_REDRIVE"):
            with self.subTest(name=name), self.env(**{name: "k"}):
                self.assertTrue(app_ingress.redriven())
        with self.env():
            self.assertFalse(app_ingress.redriven())

    def run_gate(self, **values):
        """``gate_failures.run`` with a gate that completes: the ``redrive`` it settles with."""
        import io
        from diaktoros.gate import failures as gate_failures
        settled = []
        with self.env(**values), \
                mock.patch.object(gate_failures, "signal"), \
                mock.patch.object(sys, "stdin", io.StringIO("{}")), \
                mock.patch.object(gate_failures, "effective_timeout", return_value=(None, [])), \
                mock.patch.object(gate_failures, "_migration", return_value=None), \
                mock.patch.object(gate_failures.gh, "begin_gate"), \
                mock.patch.object(gate_failures.gh, "end_gate", return_value=[]), \
                mock.patch.object(gate_failures, "_resolve_completed",
                                  side_effect=lambda *a: settled.append(a[2])):
            with self.assertRaises(SystemExit) as done:
                gate_failures.run("gate_reviewer", lambda: None)
        self.assertEqual(done.exception.code, 0)
        (redrive,) = settled
        return redrive

    def test_a_completed_gate_sees_the_redrive_mark_in_either_spelling(self):
        # F5 (round 2): the mark reaches ``_resolve_completed`` through ``gate_failures.run``.
        for name in ("DIAKTOROS_GATE_REDRIVE", "REVIEW_LOOP_GATE_REDRIVE"):
            with self.subTest(name=name):
                self.assertEqual(self.run_gate(**{name: "k1"}), "k1")
        self.assertFalse(self.run_gate())


if __name__ == "__main__":
    unittest.main()
