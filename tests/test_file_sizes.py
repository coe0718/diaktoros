"""#1349: the size ratchet on code files and functions holds, and only tightens."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import file_sizes as fs  # noqa: E402


class TheCode(unittest.TestCase):
    def test_every_file_and_function_is_within_the_ratchet(self):
        files, functions = fs.measure()
        found = fs.problems(files, functions, fs.load_baseline())
        self.assertEqual(found, [], "\n" + "\n".join(found))


class Rules(unittest.TestCase):
    BASE = {"files": {"big.py": 2000, "mid.py": 900}, "functions": {"big.py::f": 120}}

    def check(self, files, functions, baseline=None):
        return fs.problems(files, functions, baseline or self.BASE)

    def test_new_files_and_functions_are_capped(self):
        found = self.check({"big.py": 2000, "mid.py": 900, "new.py": fs.FILE_MAX + 1},
                           {"big.py::f": 120, "new.py::g": fs.FUNCTION_MAX + 1})
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any(line.startswith("new.py: ") for line in found))
        self.assertTrue(any(line.startswith("new.py::g: ") for line in found))
        self.assertEqual(self.check({"big.py": 2000, "mid.py": 900, "new.py": fs.FILE_MAX},
                                    {"big.py::f": 120, "new.py::g": fs.FUNCTION_MAX}), [])

    def test_a_file_over_the_ceiling_may_not_grow_and_one_under_may_grow_to_it(self):
        self.assertIn("big.py: 2001 lines", self.check({"big.py": 2001, "mid.py": 900},
                                                        {"big.py::f": 120})[0])
        self.assertEqual(self.check({"big.py": 2000, "mid.py": fs.FILE_CEILING},
                                    {"big.py::f": 120}), [])
        self.assertIn("mid.py: 1501 lines", self.check({"big.py": 2000, "mid.py": 1501},
                                                        {"big.py::f": 120})[0])

    def test_a_baselined_function_may_not_grow(self):
        self.assertIn("big.py::f: 121 lines, grown past its baseline 120",
                      self.check({"big.py": 2000, "mid.py": 900}, {"big.py::f": 121})[0])

    def test_a_shrink_asks_for_the_baseline_to_be_lowered(self):
        found = self.check({"big.py": 1990, "mid.py": 900}, {"big.py::f": 100})
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all("lower the baseline" in line for line in found))
        # Under the ceiling, a file's limit is the ceiling: a shrink there changes nothing.
        self.assertEqual(self.check({"big.py": 2000, "mid.py": 850}, {"big.py::f": 120}), [])

    def test_an_entry_back_under_the_cap_or_gone_asks_to_be_removed(self):
        found = self.check({"big.py": 2000, "mid.py": fs.FILE_MAX}, {})
        self.assertEqual(len(found), 2, found)
        self.assertTrue(all("remove it from the baseline" in line for line in found))

    def test_write_only_lowers_and_removes(self):
        files = {"big.py": 1990, "mid.py": 1400, "new.py": 5000}
        functions = {"big.py::f": 150, "new.py::g": 500}
        out = fs.tightened(files, functions, self.BASE)
        # big.py lowered; mid.py (under the ceiling) kept; f never raised; nothing new added.
        self.assertEqual(out, {"files": {"big.py": 1990, "mid.py": 900},
                               "functions": {"big.py::f": 120}})
        out = fs.tightened({"big.py": 1990, "mid.py": 700}, {"big.py::f": 60}, self.BASE)
        self.assertEqual(out, {"files": {"big.py": 1990}, "functions": {}})


class Measuring(unittest.TestCase):
    def test_functions_are_named_by_qualified_name_without_decorators(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "diaktoros").mkdir()
            (root / "scripts").mkdir()
            (root / "diaktoros" / "m.py").write_text(textwrap.dedent('''\
                import functools


                @functools.cache
                def top():
                    def inner():
                        return 1
                    return inner


                class C:
                    async def method(self):
                        pass

                    def method(self):
                        return 2
                '''))
            files, functions = fs.measure(root)
        self.assertEqual(files, {"diaktoros/m.py": 16})
        self.assertEqual(functions, {"diaktoros/m.py::top": 4, "diaktoros/m.py::top.inner": 2,
                                     "diaktoros/m.py::C.method": 2,
                                     "diaktoros/m.py::C.method#2": 2})

    def test_tests_are_not_scanned(self):
        self.assertFalse(any(path.parts[-2] == "tests" for path in fs.code_files()))
        self.assertTrue(any(path.parts[-2] == "scripts" for path in fs.code_files()))


if __name__ == "__main__":
    unittest.main()
