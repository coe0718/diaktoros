"""The Desktop page (#369): a read-only backend over the run ledger, and a plugin.js that stays
inside Hermes's Desktop SDK surface.

Real SQLite ledgers; the FastAPI layer is exercised only when ``fastapi`` is importable, and
``node --check`` only when node is installed. Rendering in the Desktop app is not tested here.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import config, desktop_api, ledger, stats  # noqa: E402
from diaktoros.run_supervisor import Supervisor  # noqa: E402

REPO = "acme/widgets"
NOW = 1_800_000_000.0
SECRET = "/home/someone/.hermes/tokens/x.pat exploded"
PLUGIN_JS = ROOT / "desktop" / "plugin.js"


class Now(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.db = pathlib.Path(temp.name) / "runs.sqlite"
        self.sup = Supervisor(self.db)
        self.n = 0

    def row(self, state, updated, error=None, seat="reviewer", repo=REPO):
        self.n += 1
        with mock.patch.object(self.sup, "_spawn"):
            self.sup.enqueue(f"d{self.n}", repo, 300 + self.n, "a" * 40, seat)
        with ledger.connect(self.db) as con:
            con.execute("UPDATE runs SET state=?, updated=?, error=? WHERE delivery=?",
                        (state, updated, error, f"d{self.n}"))

    def test_live_runs_and_the_last_days_failures_without_their_text(self):
        self.row("running", NOW - 10)
        self.row("waiting", NOW - 20, "held: waiting for CI on aaaaaaa — 1 check(s) still running")
        self.row("waiting", NOW - 30, "turn exited with status 1")
        self.row("uncertain", NOW - 40, SECRET, seat="fixer")
        self.row("failed", NOW - 50, SECRET)
        self.row("failed", NOW - 2 * 86400, "old")                  # outside the day
        self.row("succeeded", NOW - 5)                              # finished: not "now"
        self.row("running", NOW - 5, repo="other/repo")
        view = stats.now(self.db, REPO, at=NOW)
        states = [run["state"] for run in view["runs"]]
        self.assertEqual(states, ["running", "waiting", "waiting", "uncertain", "failed"])
        whys = [run["why"] for run in view["runs"]]
        self.assertEqual(whys[1], "held: waiting for CI on aaaaaaa — 1 check(s) still running")
        self.assertEqual(whys[2:], ["waiting to retry", "needs an operator: may have written",
                                    "failed: see explain"])
        self.assertNotIn("exploded", json.dumps(view))
        self.assertEqual(view["counts"], {"running": 1, "held": 1, "waiting": 1, "uncertain": 1,
                                          "failed": 1})
        self.assertEqual(view["runs"][0]["head"], "aaaaaaa")

    def test_no_ledger(self):
        self.assertIsNone(stats.now(self.db.with_name("missing.sqlite"), REPO))


class Api(unittest.TestCase):
    LOOPS = [{"id": "widgets", "repo": REPO}, {"id": "gadgets", "repo": "acme/gadgets"}]

    def loops(self, found):
        return mock.patch.object(config, "readable_loops", return_value=(found, []))

    def test_loops_lists_only_ids_and_repos(self):
        with self.loops([{**self.LOOPS[0], "tokens": {"x": "/secret"}}]):
            self.assertEqual(desktop_api.loops(), [{"id": "widgets", "repo": REPO}])

    def test_a_loop_must_exist_and_one_is_the_default(self):
        with self.loops(self.LOOPS), self.assertRaises(desktop_api.NotFound):
            desktop_api.now_view(None)                              # two loops: name one
        with self.loops(self.LOOPS), self.assertRaises(desktop_api.NotFound):
            desktop_api.now_view("nope")
        with self.loops(self.LOOPS[:1]), \
             mock.patch.object(stats, "now", return_value=None) as now:
            self.assertEqual(desktop_api.now_view(None), {"repo": REPO, "runs": [], "counts": {}})
        self.assertEqual(now.call_args.args[1], REPO)

    def test_stats_reads_the_ledger_only(self):
        with self.loops(self.LOOPS[:1]), \
             mock.patch.object(stats, "collect", return_value={"ok": 1}) as collect:
            self.assertEqual(desktop_api.stats_view("widgets", "30d"), {"ok": 1})
        self.assertIs(collect.call_args.kwargs["with_github"], False)
        with self.loops(self.LOOPS[:1]), self.assertRaises(desktop_api.BadRequest):
            desktop_api.stats_view("widgets", "1y")


@unittest.skipUnless(importlib.util.find_spec("fastapi"), "fastapi is not installed here")
class Routes(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("hrl_plugin_api", ROOT / "dashboard" / "plugin_api.py")
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)

    def test_only_reads_and_maps_errors(self):
        from fastapi import HTTPException
        methods = {m for route in self.api.router.routes for m in route.methods}
        self.assertEqual(methods, {"GET"})
        with mock.patch.object(desktop_api, "now_view", side_effect=desktop_api.NotFound("no")), \
             self.assertRaises(HTTPException) as caught:
            self.api.now("x")
        self.assertEqual(caught.exception.status_code, 404)


class Package(unittest.TestCase):
    def test_the_manifest_mounts_the_backend_without_a_dashboard_tab(self):
        manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
        plugin_name = re.search(r"^name:\s*(\S+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
        self.assertEqual(manifest["name"], plugin_name)
        self.assertIs(manifest["tab"]["hidden"], True)
        self.assertEqual(manifest["api"], "plugin_api.py")
        self.assertTrue((ROOT / "dashboard" / manifest["api"]).is_file())
        self.assertTrue((ROOT / "dashboard" / manifest["entry"]).is_file())

    def test_plugin_js_names_the_package_and_ships_off(self):
        source = PLUGIN_JS.read_text()
        plugin_name = re.search(r"^name:\s*(\S+)", (ROOT / "plugin.yaml").read_text(), re.M).group(1)
        self.assertIn(f"const ID = '{plugin_name}'", source)
        self.assertIn("defaultEnabled: false", source)


# Hermes's catalog "desktop surface" rules (hermes_cli/plugin_validate_desktop.py), plus the
# loader's import allowlist and the SDK's theming rule (theme variables, never a literal color).
FORBIDDEN = (
    re.compile(r"\b[A-Za-z_$][\w$]*\.prototype\.[\w$]+\s*=[^=]"),
    re.compile(r"\bObject\.definePropert(?:y|ies)\(\s*[\w$.]+\.prototype\b"),
    re.compile(r"\b(?:Reflect|Object)\.setPrototypeOf\(|\.__proto__\s*="),
    re.compile(r"(?<![\w$.])eval\(|\bnew\s+Function\("),
    re.compile(r"\bimport\("),
    re.compile(r"createElement\(\s*['\"]script['\"]\s*\)|<script\b"),
    re.compile(r"\bdocument\."),
    re.compile(r"\binnerHTML\b|\bdangerouslySetInnerHTML\b"),
    re.compile(r"\bwindow\.(?:setInterval|setTimeout|addEventListener)\b"),
    re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\("),
)
ALLOWED_IMPORTS = {"@hermes/plugin-sdk", "react", "react/jsx-runtime"}


class Surface(unittest.TestCase):
    def test_stays_inside_the_sdk_surface(self):
        code = re.sub(r"(?<![:\w])//[^\n]*", "", PLUGIN_JS.read_text())
        for pattern in FORBIDDEN:
            with self.subTest(pattern=pattern.pattern):
                self.assertIsNone(pattern.search(code))
        imports = set(re.findall(r"^import\b[^'\"]*['\"]([^'\"]+)['\"]", code, re.M))
        self.assertEqual(imports - ALLOWED_IMPORTS, set())

    @unittest.skipUnless(shutil.which("node"), "node is not installed here")
    def test_it_parses_as_an_es_module(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            copy = pathlib.Path(tmp) / "plugin.mjs"
            copy.write_text(PLUGIN_JS.read_text())
            done = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
