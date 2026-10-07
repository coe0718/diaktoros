"""#425 stage 3 and #431: `migrate --rename-loop OLD=NEW`, and the pause that holds the install.

Offline, in a temporary home: the route registry, loop files, state, run ledger and pacing file
are real; GitHub (the hook listing, a hook PATCH, a ping) is faked at the CLI's own helpers.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fixer_gating as fg  # noqa: E402
from review_loop import (cli, config, gate_failures, gh, hook_ping, ledger, migrate,  # noqa: E402
                         pacing, route_intent, routes, run_supervisor)
from review_loop.run_supervisor import Supervisor  # noqa: E402
from scripts import watchdog  # noqa: E402

HOST = "https://hooks.example.com"
SECRETS = {"one-review": "s" * 64, "one-fix": "t" * 64}


def entry(script: str, secret: str) -> dict:
    return {"description": "x", "events": ["pull_request"], "secret": secret, "prompt": "p",
            "skills": [], "deliver": "log", "profile": "reviewer", "created_at": "t",
            "script": script}


class Marker(fg.Base):
    def test_hold_writes_and_clears_the_marker(self):
        self.assertIsNone(migrate.migrating())
        with migrate.hold() as left:
            self.assertIsNone(left)
            self.assertEqual(migrate.migrating()["pid"], os.getpid())
        self.assertIsNone(migrate.migrating())

    def test_a_live_migration_refuses_and_a_dead_one_is_picked_up(self):
        migrate.marker_path().parent.mkdir(parents=True, exist_ok=True)
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(live.kill)
        migrate.marker_path().write_text(json.dumps({"pid": live.pid, "started": 1}))
        with self.assertRaises(migrate.MigrationBusy):
            with migrate.hold():
                pass
        live.kill()
        live.wait()
        with migrate.hold() as left:
            self.assertEqual(left["pid"], live.pid)
        self.assertIsNone(migrate.migrating())
        self.assertIn("did not finish", migrate.describe({"pid": live.pid, "started": 1}))

    def test_an_unreadable_marker_still_pauses(self):
        migrate.marker_path().parent.mkdir(parents=True, exist_ok=True)
        migrate.marker_path().write_text("not json")
        self.assertEqual(migrate.migrating(), {"unreadable": True})


class Paused(fg.Base):
    def setUp(self):
        super().setUp()
        migrate.marker_path().parent.mkdir(parents=True, exist_ok=True)
        migrate.marker_path().write_text(json.dumps({"pid": os.getpid(), "started": 1}))

    def test_a_gate_defers_its_delivery_with_the_payload(self):
        acted = mock.Mock()
        payload = json.dumps({"action": "opened", "repository": {"full_name": fg.REPO},
                              "pull_request": {"number": 7, "head": {"sha": fg.HEAD}}})
        with mock.patch.object(gate_failures.sys, "stdin", io.StringIO(payload)), \
                contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as stop:
                gate_failures.run("gate_reviewer", acted)
        acted.assert_not_called()
        self.assertEqual(stop.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), "[SILENT]")
        [(key, recorded)] = gate_failures.fallback_ledger().entries().items()
        self.assertEqual((recorded["kind"], recorded["error_type"]),
                         (gate_failures.DEFERRED, "MigrationInProgress"))
        self.assertTrue((gate_failures.fallback_ledger().payload_dir / f"{key}.json").is_file())

    def test_the_worker_claims_nothing(self):
        settings = self.root / "runtime.json"
        settings.write_text("{}")
        settings.chmod(0o600)
        sup = Supervisor(config.home() / "state" / "diaktoros-runs.sqlite",
                         production_config=settings, hermes_home=self.root / "home")
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d", fg.REPO, 7, fg.HEAD, "reviewer")
        with mock.patch.object(gh, "api") as api:
            self.assertIsNone(sup._claim())
        api.assert_not_called()
        self.assertEqual(sup.get("d")["state"], "pending")

    def test_the_watchdog_sweeps_nothing(self):
        with mock.patch.object(config, "readable_loops") as loops, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            watchdog.run(argparse.Namespace(loop=None, drain=False, seat="reviewer"), 60)
        loops.assert_not_called()
        self.assertIn("paused", out.getvalue())


class DeferredRedrive(fg.Base):
    def test_a_redriven_deferral_that_completes_says_nothing(self):
        ledger_ = gate_failures.fallback_ledger()
        ledger_.record("k1", {"gate": "gate_reviewer", "kind": gate_failures.DEFERRED,
                              "repo": fg.REPO, "pr": 7, "head": fg.HEAD, "action": "opened",
                              "error_type": "MigrationInProgress", "error": "deferred",
                              "redrivable": True}, "{}")
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "gate_reviewer.py").write_text("")

        def completed(ledger_arg, key, gate, scripts_dir):
            ledger_arg.resolve(key, "re-driven")
            return "re-driven"
        with mock.patch.object(gate_failures, "redrive", side_effect=completed) as driven:
            lines = gate_failures.sweep(ledger_, "[one]", scripts, cooldown_s=0)
        driven.assert_called_once()
        self.assertEqual(lines, [])


class Helpers(fg.Base):
    def current(self, **extra):
        return {**config.load_id("one"), **extra}

    def test_route_renames_follow_the_id_and_keep_custom_names(self):
        loop = self.current()
        loop["observer"] = {"route": "one-observe", "urgent_route": "my-urgent"}
        self.assertEqual(migrate.route_renames(loop, "one", "two"),
                         {"one-review": "two-review", "one-fix": "two-fix",
                          "one-observe": "two-observe"})
        renamed = migrate.renamed_loop(loop, "two", migrate.route_renames(loop, "one", "two"))
        self.assertEqual(migrate.route_renames(renamed, "one", "two"),
                         {"one-review": "two-review", "one-fix": "two-fix",
                          "one-observe": "two-observe"})
        self.assertEqual(renamed["observer"]["urgent_route"], "my-urgent")
        self.assertEqual(renamed["observer"]["route"], "two-observe")
        self.assertEqual(renamed["seats"]["reviewer"]["route"], "two-review")

    def test_only_the_default_state_directory_moves(self):
        loop = self.current(state_dir=str(migrate.default_state_dir("one")))
        self.assertEqual(migrate.renamed_loop(loop, "two", {})["state_dir"],
                         str(migrate.default_state_dir("two")))
        custom = self.current(state_dir="/somewhere/else")
        self.assertEqual(migrate.renamed_loop(custom, "two", {})["state_dir"], "/somewhere/else")

    def test_pacing_counts_follow_the_id(self):
        pacing.count_turn("one", "reviewer")
        pacing.count_turn("one", "reviewer")
        pacing.count_turn("two", "reviewer")
        self.assertEqual(pacing.rename_loop("one", "two"), 1)
        self.assertEqual(pacing.turns_today("two", "reviewer"), 3)
        self.assertEqual(pacing.turns_today("one", "reviewer"), 0)


PROFILE = {"review": "reviewer", "fix": "fixer"}


def url(route: str) -> str:
    return f"{HOST}/p/{PROFILE[route.rsplit('-', 1)[1]]}/webhooks/{route}"


def hook(hook_id: int, route: str) -> dict:
    return {"id": hook_id, "active": True, "config": {"url": url(route), "insecure_ssl": "0"}}


class Rename(fg.Base):
    def setUp(self):
        super().setUp()
        path = config.config_dir() / "one.json"
        data = json.loads(path.read_text())
        data.update(host=HOST, state_dir=str(migrate.default_state_dir("one")))
        path.write_text(json.dumps(data))
        self.loop = config.load_id("one")
        state = Path(self.loop["state_dir"])
        state.mkdir(parents=True, exist_ok=True)
        (state / "marker.json").write_text("{}")
        routes.subs_path().write_text(json.dumps({
            "one-review": entry("gate_reviewer.py", SECRETS["one-review"]),
            "one-fix": entry("gate_fixer.py", SECRETS["one-fix"]),
            "elsewhere": entry("someone.py", "u" * 64)}))
        route_intent.record_live(self.loop, ["one-review", "one-fix"])
        self.hooks = [hook(11, "one-review"), hook(12, "one-fix")]
        self.patched: list = []

    def patch_hook(self, loop, hook_id, url, login=None, **kw):
        self.patched.append((hook_id, url))
        for item in self.hooks:
            if item["id"] == hook_id:
                item["config"]["url"] = url

    def migrate(self, ping=(hook_ping.OK, "✅ answered"), *, dry_run=False, extra=()):
        pings = ping if isinstance(ping, list) else None
        with mock.patch.object(cli, "_hook_listing", side_effect=lambda *a, **k: [
                    {**h, "config": dict(h["config"])} for h in self.hooks]), \
                mock.patch.object(cli, "_patch_hook_url", side_effect=self.patch_hook), \
                mock.patch.object(hook_ping, "ping", side_effect=(pings if pings else None),
                                  return_value=None if pings else ping), \
                mock.patch.object(migrate, "doctor_step", return_value=[]), \
                mock.patch.object(migrate, "shim_step", return_value=[]), \
                mock.patch.object(migrate, "repo_step", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_migrate(argparse.Namespace(dry_run=dry_run, rename_loop="one=two",
                                                    admin_token="admin", *extra))
        return rc, out.getvalue()

    def test_a_proven_rename_moves_everything_and_a_rerun_is_quiet(self):
        rc, out = self.migrate()
        self.assertEqual(rc, 0, out)
        live = routes.all_routes()
        self.assertEqual({k for k in live if k != "elsewhere"}, {"two-review", "two-fix"})
        self.assertEqual(live["two-review"]["secret"], SECRETS["one-review"])
        self.assertIn("elsewhere", live)
        self.assertEqual(sorted(self.patched), [(11, url("two-review")), (12, url("two-fix"))])
        self.assertFalse((config.config_dir() / "one.json").exists())
        new = config.load_id("two")
        self.assertEqual((new["seats"]["reviewer"]["route"], new["seats"]["fixer"]["route"]),
                         ("two-review", "two-fix"))
        self.assertEqual(new["state_dir"], str(migrate.default_state_dir("two")))
        self.assertTrue((migrate.default_state_dir("two") / "marker.json").is_file())
        self.assertFalse(migrate.default_state_dir("one").exists())
        self.assertEqual(set(route_intent.load(new)), {"two-review", "two-fix"})
        self.assertIsNone(migrate.migrating())
        patched = list(self.patched)
        rc, out = self.migrate()                      # a re-run finds it finished: no-op
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.patched, patched)       # no hook is moved twice
        self.assertEqual(config.load_id("two")["seats"]["reviewer"]["route"], "two-review")

    def test_a_rejected_ping_puts_the_hook_back_and_moves_nothing_else(self):
        rc, out = self.migrate(ping=(hook_ping.REJECTED, "❌ HTTP 401"))
        self.assertEqual(rc, 1)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.patched[-1], (11, url("one-review")))
        self.assertTrue((config.config_dir() / "one.json").exists())
        self.assertFalse((config.config_dir() / "two.json").exists())
        self.assertIn("one-review", routes.all_routes())
        self.assertTrue(migrate.default_state_dir("one").exists())

    def test_an_unproven_ping_keeps_the_old_route_until_a_rerun_proves_it(self):
        rc, out = self.migrate(ping=[(hook_ping.SILENT, "⚠️ no delivery"),
                                     (hook_ping.OK, "✅ answered")])
        self.assertEqual(rc, 1)
        self.assertIn("NOT FINISHED", out)
        live = routes.all_routes()
        self.assertIn("one-review", live)              # not proven: kept
        self.assertNotIn("one-fix", live)              # proven: gone
        self.assertTrue((config.config_dir() / "two.json").exists())
        rc, out = self.migrate()                        # only two.json exists now
        self.assertEqual(rc, 0, out)
        self.assertNotIn("one-review", routes.all_routes())

    def test_a_run_in_flight_refuses_before_anything_moves(self):
        sup = Supervisor(run_supervisor.production_ledger())
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue("d", self.loop["repo"], 7, fg.HEAD, "reviewer")
        with ledger.connect(run_supervisor.production_ledger()) as con:
            con.execute("UPDATE runs SET state='running'")
        rc, out = self.migrate()
        self.assertEqual(rc, 1)
        self.assertIn("in flight", out)
        self.assertEqual(self.patched, [])
        self.assertNotIn("two-review", routes.all_routes())

    def test_a_new_name_held_with_another_secret_refuses(self):
        live = routes.all_routes()
        live["two-review"] = entry("gate_reviewer.py", "z" * 64)
        routes.subs_path().write_text(json.dumps(live))
        rc, out = self.migrate()
        self.assertEqual(rc, 1)
        self.assertIn("already exists with another secret", out)
        self.assertEqual(self.patched, [])

    def test_dry_run_writes_nothing(self):
        before = routes.subs_path().read_text()
        rc, out = self.migrate(dry_run=True)
        self.assertEqual(rc, 0, out)
        self.assertIn("2 hook(s) to move and ping", out)
        self.assertEqual(routes.subs_path().read_text(), before)
        self.assertEqual(self.patched, [])
        self.assertTrue((config.config_dir() / "one.json").exists())
        self.assertTrue(migrate.default_state_dir("one").exists())
        self.assertFalse(migrate.default_state_dir("two").exists())

    def test_bad_ids_and_a_taken_name_refuse(self):
        for spec in ("one", "one=one", "one=../x", "=two", "one=a b"):
            with self.subTest(spec=spec):
                lines, refused = cli._rename_loop(spec, dry_run=True, admin=None)
                self.assertTrue(refused, lines)
                self.assertIn("give OLD=NEW", lines[0])
        other = json.loads((config.config_dir() / "one.json").read_text())
        other.update(id="two", repo="owner/other")
        (config.config_dir() / "two.json").write_text(json.dumps(other))
        lines, refused = cli._rename_loop("one=two", dry_run=True, admin=None)
        self.assertTrue(refused)
        self.assertIn("already exists", lines[0])


if __name__ == "__main__":
    unittest.main()
