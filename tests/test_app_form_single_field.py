#!/usr/bin/env python3
"""#1205: each App form field drives change detection by itself, for every seat.

``test_apply_every_form_key`` moves login, App id and key path together, so a comparison that
forgot one of the three fields would still see the change through the other two. Here the loop
begins with a complete App tuple on the reviewer, the fixer and the adjudicator, and ``apply``
is given a form that changes *only* the App id or *only* the key path of one seat. For each
case it asserts the printed diff, the effective credential written (``github_app``), the
busy/identity check the seat owes (the reviewer and fixer are in-flight checked, the
adjudicator's comment identity has no run of its own to strand) and that no other seat's
credential, nor the PAT reader, moved.

Mutation check (by hand, see the PR): blinding the comparison in ``route_sync._seat_diffs`` to
the ``app_id`` or the ``key_file`` of one seat makes that seat's matching cases (and only those)
fail. Leaving a field out of ``cli_common._credential_label`` instead is coarser: that label is
also the printed diff text, so the sibling field's expected diff line breaks too, and all six
one-field cases fail.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import itertools
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_token_file_settings as tfs  # noqa: E402
from test_app_cli import Base  # noqa: E402
from diaktoros import config, github_app  # noqa: E402
from diaktoros.cli import common as cli_common  # noqa: E402

ROLES = ("reviewer", "fixer", "adjudicator")
BOTS = {"reviewer": "acme-reviewer[bot]", "fixer": "acme-fixer[bot]",
        "adjudicator": "acme-arbiter[bot]"}
APP_IDS = {"reviewer": 21, "fixer": 22, "adjudicator": 23}
NEW_ID = 999
FIELDS = ("app_id", "app_key_file")
# The seats a busy check is owed to; the adjudicator's identity rebinds no route (#1197).
BUSY_CHECKED = {"reviewer", "fixer"}


class AppFormSingleField(Base):
    def setUp(self):
        super().setUp()
        self.role_keys = {role: self.key(role) for role in ROLES}
        argv = ["init", "--repo", "acme/widgets", "--reviewer", BOTS["reviewer"],
                "--fixer", BOTS["fixer"], "--reviewer-profile", "critic",
                "--fixer-profile", "coder", "--read-token", tfs.READER,
                "--host", "https://gateway.example",
                "--token", f"{tfs.READER}={self.pats['read']}",
                "--adjudicator-route", "widgets-breach", "--adjudicator-profile", "arbiter",
                "--adjudicator-login", BOTS["adjudicator"]]
        for role in ROLES:
            argv += ["--app", f"{BOTS[role]}={APP_IDS[role]}:{self.role_keys[role]}"]
        rc, out = self.run_cli(argv)
        self.assertEqual(rc, 0, out)

    def complete_form(self):
        """The form that restates every seat's complete App tuple exactly as the loop holds it."""
        form = {}
        for role in ROLES:
            form.update({f"{role}_login": BOTS[role], f"{role}_app_id": str(APP_IDS[role]),
                         f"{role}_app_key_file": str(self.role_keys[role])})
        return form

    def effective(self, role):
        """``(App id, key path)`` the broker would use for the seat's login, from the file."""
        loop = config.load_id("widgets")
        found = github_app.entry(loop, BOTS[role])
        return found["app_id"], str(github_app.key_path(loop, BOTS[role]))

    def all_effective(self):
        return {role: self.effective(role) for role in ROLES}

    def changed_form(self, role, field):
        """The complete form with only ``role``'s ``field`` changed, and the value it was given."""
        form = self.complete_form()
        new_value = str(NEW_ID) if field == "app_id" else str(self.key(f"{role}-rotated"))
        form[f"{role}_{field}"] = new_value
        return form, new_value

    def test_complete_form_over_a_complete_tuple_matches(self):
        # The baseline every case below departs from by exactly one field.
        rc, out = self.run_cli(["apply", "--loop", "widgets"], self.complete_form())
        self.assertEqual(rc, 0, out)
        self.assertIn("already matches", out)

    def test_one_field_alone_is_a_diff_a_write_and_the_right_busy_check(self):
        for role, field in itertools.product(ROLES, FIELDS):
            with self.subTest(role=role, field=field):
                before_all = self.all_effective()
                form, new_value = self.changed_form(role, field)
                old_id, old_key = before_all[role]
                new_id = NEW_ID if field == "app_id" else old_id
                new_key = new_value if field == "app_key_file" else old_key
                bytes_before = self.loop_file().read_bytes()

                # Dry run: the diff names this seat's credential, old tuple to new, and writes nothing.
                rc, out = self.run_cli(["apply", "--loop", "widgets", "--dry-run"], form)
                self.assertNotIn("already matches", out)
                self.assertIn(f"{role} credential ({BOTS[role]}): App {old_id}, key {old_key} → "
                              f"App {new_id}, key {new_key}", out)
                self.assertIn("dry run — nothing written", out)
                self.assertEqual(self.loop_file().read_bytes(), bytes_before)

                # The enforcing path: a seat in flight refuses; the adjudicator has no run.
                busy_lines = [f"{role} in flight"]
                with patch.object(cli_common, "_busy_seats", return_value=busy_lines) as busy:
                    rc, out = self.run_cli(["apply", "--loop", "widgets"], form)
                if role in BUSY_CHECKED:
                    self.assertEqual(rc, 2, out)
                    self.assertIn("refused: a seat is in flight", out)
                    self.assertIn(role, busy.call_args.args[1])
                    self.assertEqual(self.loop_file().read_bytes(), bytes_before)
                    with patch.object(cli_common, "_busy_seats", return_value=busy_lines):
                        rc, out = self.run_cli(["apply", "--loop", "widgets", "--while-busy"], form)
                else:
                    busy.assert_not_called()
                self.assertEqual(rc, 0, out)

                # Written: this seat's effective credential moved by that one field, no other did.
                after_all = self.all_effective()
                self.assertEqual(after_all[role], (new_id, new_key))
                for other in ROLES:
                    if other != role:
                        self.assertEqual(after_all[other], before_all[other], other)
                loop = self.loop_json()
                self.assertEqual(loop["tokens"], {tfs.READER: str(self.pats["read"])})
                self.assertEqual(loop["read_token"], tfs.READER)
                for other in ROLES:
                    self.assertEqual(loop["seats"][other]["login"], BOTS[other])
                self.assert_no_leak()

                # Put the loop back for the next case.
                restore = self.complete_form()
                with patch.object(cli_common, "_busy_seats", return_value=[]):
                    rc, out = self.run_cli(["apply", "--loop", "widgets"], restore)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.all_effective(), before_all)

    def test_a_form_pat_never_replaces_an_app_seat_by_a_single_field_change(self):
        # PAT regression: a different PAT beside a one-field App change is refused ("choose PAT
        # or App"), not applied. A form PAT is ignored only beside an unchanged App, or when it
        # is the seat's existing PAT.
        for role in ROLES:
            with self.subTest(role=role):
                form, _ = self.changed_form(role, "app_id")
                form[f"{role}_token_file"] = str(self.pats["rev2"])
                with patch.object(cli_common, "_busy_seats", return_value=[]):
                    self.assert_refused(["apply", "--loop", "widgets"], "choose PAT or App", form)
                self.assertEqual(self.effective(role)[0], APP_IDS[role])

    def test_one_field_change_still_needs_its_route(self):
        # Route gating: the seat's credential moves its route's profile binding checks too.
        for role, field in itertools.product(sorted(BUSY_CHECKED), FIELDS):
            with self.subTest(role=role, field=field):
                form, _ = self.changed_form(role, field)
                with patch.object(cli_common, "_verify_routes",
                                  side_effect=config.ConfigError("foreign route")) as verify:
                    self.assert_refused(["apply", "--loop", "widgets"], "foreign route", form)
                self.assertIn(role, verify.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
