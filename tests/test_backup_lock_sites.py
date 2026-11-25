"""#2618: a ``LockInventory.RUNTIME`` entry covers one runtime-built lock site, not every site
that shares its module, innermost scope name and expression text.

The entries were keyed ``(module, innermost scope, expression)`` and every occurrence of a key
merged into one, so a same-named method in another class, or the same expression a second time
in one scope, passed under an existing entry. These drive the enforcing test itself on a copy of
the package with each probe appended (``LockInventory.probed``), so loosening the key or dropping
the one-occurrence rule turns them red. (``test_backup`` is imported as a module, not its class,
so this module does not run the inventory a second time.)
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import unittest

import test_backup

TEST = "test_every_runtime_built_lock_name_is_accounted_for"


class OneEntryOneSite(unittest.TestCase):
    def probed(self, module, text):
        return test_backup.LockInventory(TEST).probed(module, text, TEST)

    def test_a_same_named_method_in_another_class_is_not_covered(self):
        # The issue's probe: observer's allowance is ("diaktoros.observer", "lock_path", ...).
        self.assertTrue(self.probed(
            "diaktoros/observer.py",
            "\n\nclass ReviewProbe:\n    def lock_path(self, path):\n"
            "        return path.parent / (path.name + \".lock\")\n"))

    def test_a_second_function_of_the_same_name_is_not_covered(self):
        self.assertTrue(self.probed(
            "diaktoros/observer.py",
            "\n\ndef lock_path(path):\n    return path.parent / (path.name + \".lock\")\n"))

    def test_the_same_expression_twice_in_one_scope_is_not_covered(self):
        # route_intent's allowance is at module level: a second identical site there.
        self.assertTrue(self.probed("diaktoros/route_intent.py",
                                    "\nOTHER_LOCK = INTENT_FILE + \".lock\"\n"))


if __name__ == "__main__":
    unittest.main()
