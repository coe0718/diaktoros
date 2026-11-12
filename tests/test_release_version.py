"""The release version, in every place it is written (review of #2372).

A release PR bumps ``plugin.yaml``, ``diaktoros.__version__`` and the catalog copy together, and
``RELEASE`` here with them: a bump lost from any one of the three, or never made, fails here.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import diaktoros  # noqa: E402

RELEASE = "0.6.1"


def _field(path: pathlib.Path) -> str:
    found = re.search(r'^version:\s*"?([^"\s]+)"?\s*$', path.read_text(encoding="utf-8"), re.M)
    return found.group(1) if found else ""


class ReleaseVersion(unittest.TestCase):
    def test_every_place_names_the_release(self):
        self.assertEqual(diaktoros.__version__, RELEASE)
        self.assertEqual(_field(ROOT / "plugin.yaml"), RELEASE)
        self.assertEqual(_field(ROOT / "catalog" / "diaktoros.yaml"), RELEASE)


if __name__ == "__main__":
    unittest.main()
