"""The Desktop settings projection (#630): the plugin form's content and each loop's answers.

Two halves are worth pinning, and one of them is a safety property:

* **The schema is the whole form.** Every key in ``config.SETTINGS_SCHEMA`` must appear in
  ``schema``, ``values`` and ``sources``, or a field silently vanishes from the page.
* **A token is a path, never a value.** The projection names where a PAT is read from at use
  time; it must never carry a credential, whatever the store contains.
* **An unreadable store is unavailable, not a clean default.** Reporting schema defaults as if
  the operator had chosen them is the false-green this projection exists to prevent, so
  ``available`` and ``reason`` are asserted in both directions.

``hermes_cli`` is not importable inside this plugin's own CI, so the *unavailable* path is the
real one there and the *available* path is exercised through a stand-in module put into
``sys.modules``. Both directions are tested; neither is assumed.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import config, desktop_api  # noqa: E402

# A settings store carrying a credential-shaped value, to prove the projection never returns it.
LEAK = "github_pat_11ABCDEFG0abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP"


def _fake_settings_module(fields):
    """A stand-in for ``hermes_cli.plugins_settings`` exposing the form's own reader."""
    module = types.ModuleType("hermes_cli.plugins_settings")

    def plugin_settings_fields(plugin_id, plugin_dir):      # noqa: ANN001, ARG001
        return fields

    module.plugin_settings_fields = plugin_settings_fields
    package = types.ModuleType("hermes_cli")
    package.plugins_settings = module
    return package, module


# A loop file in the shape the loops themselves carry: seats under ``seats``, credentials mapped
# by login under ``tokens``, flags under the names the loop's own readers use. A hand-built dict
# with a top-level ``reviewer`` and a ``ci_fix`` flag passes while the page shows blanks — that is
# how this projection first shipped reading a shape no loop has, so the tests load files instead.
LOOP = {"repo": "acme/good", "base": "main", "cap": 3, "fix_ci": True, "review_after_ci": True,
        "fixer_check": "python3 tests/affected.py", "reviewers": ["vex-coe"],
        "fixers": ["drey-coe1"], "read_token": "reader",
        "tokens": {"reader": "/x/read.pat", "vex-coe": "/x/vex.pat", "drey-coe1": "/x/drey.pat",
                   "tuck-coe": "/x/tuck.pat"},
        "seats": {"reviewer": {"route": "r-review", "profile": "vex", "login": "vex-coe"},
                  "fixer": {"route": "r-fix", "profile": "drey", "login": "drey-coe1"},
                  "adjudicator": {"login": "tuck-coe"}},
        "adjudicator": {"profile": "default", "route": "r-breach"}}


class SettingsView(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.temp.cleanup)
        # Any pre-existing hermes_cli is put back afterwards: the unavailable path depends on it
        # being absent, and a later test must not inherit this one's stand-in.
        self.saved = {name: sys.modules.get(name) for name in ("hermes_cli",
                                                               "hermes_cli.plugins_settings")}
        self.addCleanup(self._restore_modules)

    def _restore_modules(self):
        for name, module in self.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _install(self, fields):
        package, module = _fake_settings_module(fields)
        sys.modules["hermes_cli"] = package
        sys.modules["hermes_cli.plugins_settings"] = module

    def _without_settings_surface(self):
        sys.modules.pop("hermes_cli", None)
        sys.modules.pop("hermes_cli.plugins_settings", None)

    # ---- the schema is the whole form ------------------------------------------------

    def test_every_schema_key_is_in_schema_values_and_sources(self):
        self._install([])
        view = desktop_api.settings_view()
        plugin = view["plugin"]
        declared = set(config.SETTINGS_SCHEMA)
        self.assertEqual(set(plugin["schema"]), declared)
        self.assertEqual(set(plugin["values"]), declared)
        self.assertEqual(set(plugin["sources"]), declared)

    def test_a_field_carries_what_a_form_renders(self):
        self._install([])
        field = desktop_api.settings_view()["plugin"]["schema"]["cap"]
        self.assertEqual(field["type"], "int")
        self.assertEqual(field["default"], 3)
        self.assertTrue(field["label"])
        self.assertTrue(field["description"])

    # ---- what was set, versus the schema default -------------------------------------

    def test_a_set_value_is_marked_set_and_a_blank_one_is_the_default(self):
        self._install([{"key": "cap", "value": 5}, {"key": "base", "value": ""}])
        plugin = desktop_api.settings_view()["plugin"]
        self.assertEqual(plugin["sources"]["cap"], "set")
        self.assertEqual(plugin["values"]["cap"], 5)
        self.assertEqual(plugin["sources"]["base"], "default")
        self.assertEqual(plugin["values"]["base"], config.SETTINGS_SCHEMA["base"]["default"])

    def test_a_wrong_typed_value_falls_back_rather_than_reaching_the_page(self):
        self._install([{"key": "grace_min", "value": "not a number"}])
        self.assertEqual(desktop_api.settings_view()["plugin"]["values"]["grace_min"],
                         config.SETTINGS_SCHEMA["grace_min"]["default"])

    # ---- the safety property ---------------------------------------------------------

    def test_a_token_value_never_appears_only_the_path_that_names_it(self):
        self._install([{"key": "reviewer_token_file", "value": "/home/x/.hermes/tokens/r.pat"},
                       {"key": "reviewer_login", "value": "reviewer-bot"},
                       {"key": "reviewer_profile", "value": "vex"}])
        view = desktop_api.settings_view()
        blob = json.dumps(view)
        self.assertNotIn(LEAK, blob)
        self.assertNotIn("github_pat", blob)
        for seat in view["plugin"]["seats"].values():
            self.assertIn(seat["token_file"], ("", "/home/x/.hermes/tokens/r.pat"))

    def test_a_seat_mapping_carries_the_path_and_never_the_credential_behind_it(self):
        self._install([{"key": "reviewer_token_file", "value": LEAK},
                       {"key": "reviewer_profile", "value": "vex"}])
        view = desktop_api.settings_view()["plugin"]
        # A store that (wrongly) held a credential in the path field: it is returned as written
        # for the form to show, and the projection itself reads no file and resolves no token.
        self.assertEqual(view["seats"]["reviewer"]["token_file"], LEAK)
        self.assertEqual(view["seats"]["reviewer"]["profile"], "vex")
        self.assertTrue(view["seats"]["reviewer"]["set"])
        # The seats the form does not name stay blank rather than inheriting another seat's answer.
        self.assertEqual(view["seats"]["fixer"]["token_file"], "")
        self.assertFalse(view["seats"]["fixer"]["set"])

    # ---- unavailable is not a clean default ------------------------------------------

    def test_an_unreadable_store_is_unavailable_with_a_reason_not_a_default(self):
        self._without_settings_surface()
        plugin = desktop_api.settings_view()["plugin"]
        self.assertFalse(plugin["available"])
        self.assertIn("unavailable", plugin["reason"])
        # The schema is still complete, so the page can render the form's shape and say why the
        # values are not the operator's.
        self.assertEqual(set(plugin["schema"]), set(config.SETTINGS_SCHEMA))

    def test_a_readable_but_empty_store_is_available_and_all_default(self):
        self._install([])
        plugin = desktop_api.settings_view()["plugin"]
        self.assertTrue(plugin["available"])
        self.assertEqual(plugin["reason"], "")
        self.assertTrue(all(source == "default" for source in plugin["sources"].values()))

    def test_a_store_that_raises_is_unavailable_and_does_not_raise(self):
        package, module = _fake_settings_module([])

        def boom(plugin_id, plugin_dir):            # noqa: ANN001, ARG001
            raise ValueError("malformed manifest")

        module.plugin_settings_fields = boom
        sys.modules["hermes_cli"] = package
        sys.modules["hermes_cli.plugins_settings"] = module
        plugin = desktop_api.settings_view()["plugin"]
        self.assertFalse(plugin["available"])
        self.assertTrue(plugin["reason"])

    # ---- per-loop answers ------------------------------------------------------------

    def write_loops(self, **files: str) -> pathlib.Path:
        """Loop files for this test, in the config dir the projection reads — removed afterwards.

        The config dir is shared state inside the guarded temp home: a file left behind is a
        configured loop for every later test in the same run, which is how a leak from one test
        file failed a test in another. Each write is undone, and the directory too when this test
        is the one that made it.
        """
        directory = config.config_dir()
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if created:
            self.addCleanup(lambda: directory.exists() and not any(directory.iterdir())
                            and directory.rmdir())
        for loop_id, text in files.items():
            path = directory / f"{loop_id}.json"
            path.write_text(text)
            self.addCleanup(path.unlink, missing_ok=True)
        return directory

    def write_loop(self, loop_id: str, **body) -> None:
        """One real loop file: ``LOOP`` as ``body`` overrides it, with the id its filename claims."""
        self.write_loops(**{loop_id: json.dumps({**LOOP, **body, "id": loop_id})})

    def test_each_loop_reports_its_own_effective_answers(self):
        self._install([])
        self.write_loop("good")
        loops = desktop_api.settings_view()["loops"]
        self.assertEqual([loop["id"] for loop in loops], ["good"])
        entry = loops[0]
        self.assertFalse(entry["error"])
        self.assertEqual(entry["base"], "main")
        self.assertEqual(set(entry["seats"]), set(config.ROUTE_ROLES))
        self.assertEqual(entry["seats"]["reviewer"]["profile"], "vex")
        self.assertEqual(entry["seats"]["reviewer"]["login"], "vex-coe")
        self.assertEqual(entry["seats"]["reviewer"]["route"], "r-review")
        self.assertEqual(entry["seats"]["adjudicator"]["profile"], "default")
        self.assertEqual(entry["seats"]["adjudicator"]["route"], "r-breach")
        for seat in entry["seats"].values():
            self.assertIsInstance(seat["turn_budget_s"], int)
            self.assertIsInstance(seat["max_steps"], int)
            self.assertTrue(seat["profile"])

    def test_a_seat_names_the_token_path_for_its_login(self):
        self._install([])
        self.write_loop("good")
        seats = desktop_api.settings_view()["loops"][0]["seats"]
        self.assertEqual(seats["reviewer"]["token_file"], "/x/vex.pat")
        self.assertEqual(seats["fixer"]["token_file"], "/x/drey.pat")
        self.assertEqual(seats["adjudicator"]["token_file"], "/x/tuck.pat")

    def test_a_loop_reports_the_flags_its_file_actually_sets(self):
        """Under the names a loop carries: ``fix_ci`` — a page reading ``ci_fix`` shows off forever."""
        self._install([])
        self.write_loop("good")
        self.write_loop("quiet", fix_ci=False, review_after_ci=False, fixer_check="")
        flags = {loop["id"]: loop["flags"] for loop in desktop_api.settings_view()["loops"]}
        self.assertTrue(flags["good"]["fix_ci"])
        self.assertTrue(flags["good"]["review_after_ci"])
        self.assertEqual(flags["good"]["fixer_check"], "python3 tests/affected.py")
        self.assertFalse(flags["quiet"]["fix_ci"])
        self.assertFalse(flags["quiet"]["review_after_ci"])
        self.assertEqual(flags["quiet"]["fixer_check"], "")

    def test_a_reader_raising_for_one_loop_is_only_that_loop_s_error(self):
        self._install([])
        self.write_loop("good")
        self.write_loop("broken")

        def budget(loop, seat):                   # only the broken loop fails to answer
            if loop["id"] == "broken":
                raise KeyError("no such loop")
            return 900

        with mock.patch.object(config, "turn_budget", budget):
            loops = desktop_api.settings_view()["loops"]
        by_id = {loop["id"]: loop for loop in loops}
        self.assertEqual(sorted(by_id), ["broken", "good"])
        self.assertTrue(by_id["broken"]["error"])
        self.assertEqual(by_id["broken"]["reason"], "KeyError")
        self.assertFalse(by_id["good"]["error"])
        self.assertEqual(by_id["good"]["seats"]["reviewer"]["profile"], "vex")

    def test_a_loop_file_that_does_not_parse_is_reported_not_dropped(self):
        """A loop whose config cannot be read at all still gets an entry, and the rest still load.

        Only a real unreadable file exercises ``readable_loops``'s ``skipped`` list. A loop that
        loads and then raises elsewhere is a *different* case, and covering that one alone is how a
        whole configured loop disappeared from this projection without a test noticing.
        """
        self._install([])
        self.write_loop("good")
        self.write_loops(broken="{ this is not json\n")
        by_id = {loop["id"]: loop for loop in desktop_api.settings_view()["loops"]}
        self.assertIn("broken", by_id, "an unreadable loop is reported, never dropped")
        self.assertTrue(by_id["broken"]["error"])
        self.assertTrue(by_id["broken"]["reason"])
        self.assertEqual(by_id["broken"]["seats"], {})
        self.assertFalse(by_id["good"]["error"])
        self.assertEqual(by_id["good"]["repo"], "acme/good")

    def test_every_entry_carries_the_same_keys_whatever_happened(self):
        self._install([])
        self.write_loop("good")
        self.write_loops(unreadable="{ nope\n")

        def budget(loop, seat):
            if loop["id"] == "good":
                raise KeyError("no such loop")
            return 900

        with mock.patch.object(config, "turn_budget", budget):
            loops = desktop_api.settings_view()["loops"]
        self.assertEqual(len(loops), 2)
        self.assertEqual(len({frozenset(loop) for loop in loops}), 1)

    def test_an_unreadable_reason_is_one_bounded_line(self):
        self._install([])
        with mock.patch.object(config, "readable_loops",
                               return_value=([], [("broken", "x" * 5000 + "\n\ttail")])):
            entry = desktop_api.settings_view()["loops"][0]
        self.assertTrue(entry["error"])
        self.assertEqual(entry["id"], "broken")
        self.assertLessEqual(len(entry["reason"]), 200)
        self.assertNotIn("\n", entry["reason"])

    # ---- read-only -------------------------------------------------------------------

    def test_the_view_creates_no_files_and_names_no_ledger(self):
        self._install([])
        before = {path for path in pathlib.Path(self.temp.name).rglob("*")}
        view = desktop_api.settings_view()
        after = {path for path in pathlib.Path(self.temp.name).rglob("*")}
        self.assertEqual(before, after)
        self.assertNotIn("ledger", json.dumps(view).lower())
        self.assertIsInstance(view["read_at"], float)

    def test_the_route_is_read_only_and_registered(self):
        """The route module exposes GET /settings and no write verb for it."""
        source = (ROOT / "dashboard" / "plugin_api.py").read_text()
        self.assertIn('@router.get("/settings")', source)
        self.assertNotIn('@router.post("/settings")', source)
        self.assertNotIn("@router.put", source)
        self.assertNotIn("@router.patch", source)
        self.assertNotIn("@router.delete", source)


if __name__ == "__main__":
    unittest.main()
