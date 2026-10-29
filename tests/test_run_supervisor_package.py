"""``run_supervisor`` is a package (#717): ``python -m diaktoros.run_supervisor`` still runs the
worker and the operator commands, and the worker's PYTHONPATH still finds the plugin.

The loop spawns every worker with ``-m diaktoros.run_supervisor`` and prints the same command in
its reconcile and retry advice, so a missing ``__main__.py`` (or a PYTHONPATH one level short)
would fail only when a turn launched.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from diaktoros import run_supervisor  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


class Entry(unittest.TestCase):
    def test_python_m_runs_the_operator_commands(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            db = pathlib.Path(tmp) / "runs.sqlite"
            run_supervisor.Supervisor(db)  # status reads a ledger that exists (#798)
            done = subprocess.run([sys.executable, "-m", "diaktoros.run_supervisor", "status",
                                   str(db)],
                                  cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")

    def test_the_worker_pythonpath_is_the_plugin_root(self):
        # _spawn sets PYTHONPATH, and the worker's cwd (#1373), to Path(__file__).resolve()
        # .parents[2]: from diaktoros/run_supervisor/workers.py (where _spawn lives, #1349)
        # that is the checkout holding diaktoros/.
        from diaktoros.run_supervisor import workers
        root = pathlib.Path(workers.__file__).resolve().parents[2]
        self.assertEqual(root, ROOT)
        self.assertTrue((root / "diaktoros" / "run_supervisor" / "__main__.py").is_file())
        source = pathlib.Path(workers.__file__).read_text()
        self.assertIn('plugin_root = Path(__file__).resolve().parents[2]', source)
        self.assertIn('"PYTHONPATH": str(plugin_root)', source)
        self.assertIn('cwd=str(plugin_root)', source)


class WriteRecordsDoc(unittest.TestCase):
    def test_docstring_lists_every_table_the_function_checks(self):
        # Drift guard (#808): a reader deciding whether a run may be re-armed goes by the
        # docstring, so each table the function queries must be named in it.
        import inspect
        import re
        source = inspect.getsource(run_supervisor.write_records)
        tables = set(re.findall(r"\bFROM\s+(\w+)", source))
        self.assertGreaterEqual(len(tables), 7, tables)  # runs + the six record tables
        doc = run_supervisor.write_records.__doc__
        for table in sorted(tables):
            self.assertIn(f"``{table}``", doc, f"docstring omits the {table} table")


if __name__ == "__main__":
    unittest.main()
