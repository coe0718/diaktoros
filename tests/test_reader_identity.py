"""The reader is its own account: init, set and doctor hold the four-identity rule (#56).

Also runs README's own install command through ``init --dry-run`` — parsing it is not enough
(it parsed while every copy of it was refused), so the example is extracted from the file and
validated against a fixture home holding exactly the profiles and token files it names.

Stdlib only, disposable HOME/HERMES_HOME; the "tokens" are obviously-fake sentinels.
"""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_loop import cli, config, doctor

ROOT = Path(__file__).resolve().parent.parent
READER, REV, FIX, ADJ = "reader-acct", "rev-acct", "fix-acct", "adj-acct"
SENTINEL = "pat-fixture"


class _Ctx:
    def register_cli_command(self, name, summary, setup, **kwargs):
        self.setup = setup


def readme_install_argv() -> list[str]:
    """The argv of README's ``hermes review-loop init`` example, continuations joined."""
    lines = (ROOT / "README.md").read_text().splitlines()
    in_fence = False
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence and line.strip().startswith("hermes review-loop init"):
            command = line.strip()
            while command.endswith("\\"):
                index += 1
                command = command[:-1] + " " + lines[index].strip()
            return shlex.split(re.sub(r"^hermes\s+review-loop\s+", "", command), comments=True)
    raise AssertionError("README.md has no fenced `hermes review-loop init` example")


class _Home(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.home = self.root / "home"
        self.hermes = self.home / ".hermes"
        env = patch.dict(os.environ, {
            "HOME": str(self.home), "HERMES_HOME": str(self.hermes),
            "REVIEW_LOOP_CONFIG_DIR": str(self.hermes / "review-loops.d"),
            "REVIEW_LOOP_SUBS": str(self.hermes / "webhook_subscriptions.json")})
        env.start()
        self.addCleanup(env.stop)
        # No test here may reach GitHub: an unpatched call fails loudly instead of sending a
        # fixture token to the real API.
        offline = patch("urllib.request.urlopen",
                        side_effect=AssertionError("test reached the network"))
        offline.start()
        self.addCleanup(offline.stop)
        for profile in ("vex", "drey", "tuck"):
            (self.hermes / "profiles" / profile).mkdir(parents=True)
            (self.hermes / "profiles" / profile / "config.yaml").write_text("model: {}\n")
        self.keys = self.hermes / "keys"
        self.keys.mkdir(parents=True)
        self.addCleanup(setattr, cli, "_SETTINGS", getattr(cli, "_SETTINGS", {}))

    def pat(self, name, mode=0o600):
        path = self.keys / f"{name}-pat"
        path.write_text(f"{SENTINEL}-{name}\n")
        path.chmod(mode)
        return path

    def run_cli(self, argv, settings=None):
        ctx = _Ctx()
        cli.register_cli(ctx, settings=settings or {})
        parser = argparse.ArgumentParser(prog="hermes review-loop")
        ctx.setup(parser)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            args = parser.parse_args(argv)
            rc = args.func(args)
        text = out.getvalue()
        self.assertNotIn(SENTINEL, text)
        return rc, text


class _Loop(_Home):
    def init_argv(self, *extra, reader=READER, reader_file="read"):
        argv = ["init", "--repo", "acme/widgets", "--fixer", FIX, "--reviewer", REV,
                "--reviewer-profile", "vex", "--fixer-profile", "drey",
                "--host", "https://gateway.example",
                "--token", f"{REV}={self.pat('rev')}", "--token", f"{FIX}={self.pat('fix')}"]
        if reader:
            argv += ["--read-token", reader]
        if reader and reader_file:
            argv += ["--token", f"{reader}={self.pat(reader_file)}"]
        return argv + list(extra)

    def loop_file(self):
        return config.config_dir() / "widgets.json"

    def refused(self, argv, reason):
        rc, out = self.run_cli(argv)
        self.assertEqual(rc, 2, out)
        self.assertRegex(out, reason)
        return out


class ReadmeInstallTests(_Home):
    def test_readme_install_command_passes_init_dry_run(self):
        argv = readme_install_argv()
        self.assertEqual(argv[0], "init")
        # Every token file the example maps, created the way the docs say (mode 600).
        for value in [argv[i + 1] for i, arg in enumerate(argv) if arg == "--token"]:
            login, path = value.split("=", 1)
            target = Path(path).expanduser()
            self.assertEqual(target.parent, self.keys, f"{value}: expected under ~/.hermes/keys")
            self.pat(target.name[:-len("-pat")])
        rc, out = self.run_cli([*argv, "--dry-run"])
        self.assertEqual(rc, 0, out)
        self.assertIn("nothing written", out)
        self.assertFalse(config.config_dir().exists() and any(config.config_dir().iterdir()))

    def test_the_old_readme_shape_is_refused(self):
        """The shape README used to print (reader on the reviewer seat) stays refused."""
        self.pat("rev-bot")
        self.pat("dev-account")
        rc, out = self.run_cli(["init", "--repo", "owner/name", "--fixer", "dev-account",
                                "--reviewer", "rev-bot", "--fixer-profile", "drey",
                                "--reviewer-profile", "vex",
                                "--token", f"rev-bot={self.keys / 'rev-bot-pat'}",
                                "--token", f"dev-account={self.keys / 'dev-account-pat'}",
                                "--read-token", "rev-bot", "--host", "https://gw.example",
                                "--dry-run"])
        self.assertEqual(rc, 2, out)
        self.assertIn("also the reviewer seat", out)
        self.assertIn("four-identity rule", out)


class ReaderIdentityTests(_Loop):
    def test_init_refuses_a_reader_that_is_a_seat(self):
        for seat, login in (("reviewer", REV), ("fixer", FIX)):
            out = self.refused(self.init_argv(reader=login, reader_file=""),
                               rf"the reader {login!r} is also the {seat} seat")
            self.assertIn("four-identity rule", out)
            self.assertFalse(self.loop_file().exists())

    def test_init_no_longer_defaults_the_reader_to_the_reviewer_seat(self):
        out = self.refused(self.init_argv(reader=""), r"--read-token LOGIN names the account")
        self.assertIn("four-identity rule", out)
        self.assertFalse(self.loop_file().exists())

    def test_init_refuses_a_reader_sharing_a_seat_token_file(self):
        self.refused(self.init_argv(reader_file="rev"), r"read the same token file")

    def test_init_refuses_a_reader_that_is_the_adjudicator_login(self):
        out = self.refused(self.init_argv("--adjudicator-route", "widgets-breach",
                                          "--adjudicator-profile", "tuck",
                                          "--adjudicator-login", READER),
                           r"also the reader")
        self.assertIn("four-identity rule", out)

    def test_init_refuses_an_admin_token_with_no_file(self):
        self.refused(self.init_argv("--hooks", "--admin-token", "owner-acct", "--dry-run"),
                     r"--admin-token 'owner-acct' has no token file")

    def test_distinct_reader_installs(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(self.loop_file().read_text())["read_token"], READER)
        rc, out = self.run_cli(["doctor", "--loop", "widgets", "--offline"])
        self.assertIn(f"{READER} (mapped in tokens; its own account and file)", out)

    def install_legacy_shared_reader(self):
        """A loop written before this rule: the reader is the reviewer seat."""
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        data = json.loads(self.loop_file().read_text())
        data["read_token"] = REV
        self.loop_file().write_text(json.dumps(data))

    def test_doctor_flags_a_reader_on_a_seat(self):
        self.install_legacy_shared_reader()
        check = doctor.check_read_token(config.load_id("widgets"))
        self.assertEqual(check.status, doctor.MISMATCH)
        self.assertIn("also the reviewer seat", check.detail)
        self.assertIn("four-identity rule", check.detail)
        self.assertIn("set --loop widgets --read-token", check.fix)

    def test_set_read_token_moves_the_reader_off_the_seat(self):
        self.install_legacy_shared_reader()
        new = self.pat("new-reader")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--read-token", "new-reader",
                                "--token", f"new-reader={new}"])
        self.assertEqual(rc, 0, out)
        self.assertIn(f"read_token: {REV} → new-reader", out)
        data = json.loads(self.loop_file().read_text())
        self.assertEqual(data["read_token"], "new-reader")
        self.assertEqual(data["tokens"]["new-reader"], str(new))
        self.assertEqual(doctor.check_read_token(config.load_id("widgets")).status, doctor.VERIFIED)
        # An already-mapped login needs no --token.
        rc, out = self.run_cli(["set", "--loop", "widgets", "--read-token", READER])
        self.assertEqual(rc, 0, out)

    def settings_matching(self, **changes):
        """A settings form that matches the installed loop, plus ``changes``."""
        return {"reviewer_profile": "vex", "fixer_profile": "drey",
                "reviewer_login": REV, "fixer_login": FIX,
                "host": "https://gateway.example", **changes}

    def test_apply_refuses_a_legacy_reader_on_a_seat_even_when_no_identity_moves(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli(["apply", "--loop", "widgets"], self.settings_matching(cap=4))
        self.assertEqual(rc, 0, out)            # control: a clean loop takes the cap push
        self.assertIn("cap: 3 → 4", out)
        data = json.loads(self.loop_file().read_text())
        data["read_token"] = REV
        self.loop_file().write_text(json.dumps(data))
        before = self.loop_file().read_bytes()
        for settings in (self.settings_matching(cap=5), self.settings_matching(cap=4)):
            for dry in ([], ["--dry-run"]):
                rc, out = self.run_cli(["apply", "--loop", "widgets", *dry], settings)
                self.assertEqual(rc, 2, out)
                self.assertIn(f"the reader {REV!r} is also the reviewer seat", out)
                self.assertIn("four-identity rule", out)
                self.assertIn("fix: hermes review-loop set --loop widgets --read-token", out)
                self.assertNotIn("already matches", out)
                self.assertEqual(self.loop_file().read_bytes(), before)

    def test_status_flags_a_reader_on_a_seat(self):
        self.install_legacy_shared_reader()
        rc, out = self.run_cli(["status", "--loop", "widgets"])
        self.assertIn(f"reader:  the reader {REV!r} is also the reviewer seat", out)
        self.assertIn("four-identity rule", out)
        self.assertIn("fix:        hermes review-loop set --loop widgets --read-token", out)
        rc, out = self.run_cli(self.init_argv("--id", "clean", "--repo", "acme/clean"))
        rc, out = self.run_cli(["status", "--loop", "clean"])
        self.assertNotIn("reader:", out)

    def test_a_loop_file_without_a_reader_is_refused_never_inferred(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        data = json.loads(self.loop_file().read_text())
        del data["read_token"]                      # hand-edited; the first token is a seat's
        self.assertIn(next(iter(data["tokens"])), (REV, FIX))
        self.loop_file().write_text(json.dumps(data))
        with self.assertRaisesRegex(config.ConfigError,
                                    r"'read_token' is not set, and the reader is never inferred "
                                    r"from 'tokens' — add \"read_token\": \"<login>\""):
            config.load_id("widgets")
        # Every verb answers with that reason and an exit code — never a traceback. The verbs
        # come from the parser itself, so a verb added later cannot be forgotten here.
        ctx = _Ctx()
        cli.register_cli(ctx, settings={})
        parser = argparse.ArgumentParser(prog="hermes review-loop")
        ctx.setup(parser)
        verbs = next(a for a in parser._actions
                     if isinstance(a, argparse._SubParsersAction)).choices
        extra = {"explain": ["--pr", "1"], "fixer-push": ["--disable"],
                 "models": ["--seat", "reviewer"], "retry": ["--pr", "1"],
                 "init": ["--repo", "acme/other", "--dry-run"]}
        self.assertIn("uninstall", verbs)
        self.assertIn("cleanup", verbs)
        for verb, sub in verbs.items():
            loop_opt = next((a for a in sub._actions if "--loop" in a.option_strings), None)
            # With --loop where the verb takes one, and again without it where --loop is
            # optional (the verb then reads every loop file — `explain --pr 1`, `status`, …).
            argvs = [[verb, *(["--loop", "widgets"] if loop_opt else []), *extra.get(verb, [])]]
            if loop_opt is not None and not loop_opt.required:
                argvs.append([verb, *extra.get(verb, [])])
            for argv in argvs:
                try:
                    rc, out = self.run_cli(argv)
                except SystemExit as exc:      # argparse: a required option this list lacks
                    self.fail(f"{argv}: add its required options to `extra` ({exc})")
                except Exception as exc:       # noqa: BLE001 - the point of the test
                    self.fail(f"{argv} raised {type(exc).__name__}: {exc}")
                self.assertIsInstance(rc, int, argv)
                if loop_opt is not None or verb == "list":
                    self.assertEqual(rc, 2, (argv, out))
                    self.assertIn("'read_token' is not set", out, argv)
        # scripts/cleanup.py is also run directly (and by the cleanup verb): same answer.
        proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "cleanup.py"),
                               "--loop", "widgets", "--sweep", "--dry-run"],
                              capture_output=True, text=True, env=os.environ.copy(), timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("cannot clean up:", proc.stdout)
        self.assertIn("'read_token' is not set", proc.stdout)

    def test_set_read_token_repairs_a_file_with_no_reader(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        data = json.loads(self.loop_file().read_text())
        del data["read_token"]
        del data["tokens"][READER]
        self.loop_file().write_text(json.dumps(data))
        # The refusal names the repair, and the repair works on exactly this defect.
        rc, out = self.run_cli(["status", "--loop", "widgets"])
        self.assertIn("`hermes review-loop set --loop widgets --read-token LOGIN --token "
                      "LOGIN=/abs/path/to/pat` writes both", out)
        new = self.pat("fresh-reader")
        rc, out = self.run_cli(["set", "--loop", "widgets", "--read-token", "fresh-reader",
                                "--token", f"fresh-reader={new}"])
        self.assertEqual(rc, 0, out)
        self.assertIn("repairing widgets: it has no read_token", out)
        self.assertIn("read_token: (none) → fresh-reader", out)
        loop = config.load_id("widgets")
        self.assertEqual(loop["read_token"], "fresh-reader")
        self.assertEqual(doctor.check_read_token(loop).status, doctor.VERIFIED)
        # The repair still holds the four-identity rule, and repairs nothing else.
        del data["tokens"]
        data["tokens"] = {REV: str(self.keys / "rev-pat"), FIX: str(self.keys / "fix-pat")}
        self.loop_file().write_text(json.dumps(data))
        before = self.loop_file().read_bytes()
        self.refused(["set", "--loop", "widgets", "--read-token", REV], r"also the reviewer seat")
        self.assertEqual(self.loop_file().read_bytes(), before)
        data["cap"] = "many"
        self.loop_file().write_text(json.dumps(data))
        self.refused(["set", "--loop", "widgets", "--read-token", "fresh-reader",
                      "--token", f"fresh-reader={new}"], r"'cap' must be a whole number")
        # Without --read-token there is nothing to repair with: the ordinary refusal.
        data["cap"] = 3
        self.loop_file().write_text(json.dumps(data))
        self.refused(["set", "--loop", "widgets", "--cap", "4"], r"'read_token' is not set")

    def test_explain_without_loop_names_a_refused_file(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        data = json.loads(self.loop_file().read_text())
        del data["read_token"]
        self.loop_file().write_text(json.dumps(data))
        rc, out = self.run_cli(["explain", "--pr", "1"])
        self.assertEqual(rc, 2, out)
        self.assertIn("skipping widgets.json: ", out)
        # A healthy loop next to it: still ambiguous — the question may be about the bad one.
        (config.config_dir() / "clean.json").write_text(json.dumps(
            {**data, "id": "clean", "repo": "acme/clean", "read_token": READER,
             "seats": {"reviewer": {"profile": "vex", "route": "clean-review"},
                       "fixer": {"profile": "drey", "route": "clean-fix"}}}))
        rc, out = self.run_cli(["explain", "--pr", "1"])
        self.assertEqual(rc, 2, out)
        self.assertIn("2 loops are configured (clean, widgets) — name one with --loop", out)
        # The id comes from the file name, never re-parsed out of the printed line.
        self.loop_file().rename(config.config_dir() / "alpha:beta.json")
        rc, out = self.run_cli(["explain", "--pr", "1"])
        self.assertEqual(rc, 2, out)
        self.assertIn("skipping alpha:beta.json: ", out)
        self.assertIn("2 loops are configured (clean, alpha:beta) — name one with --loop", out)

    def test_explain_exit_2_is_one_closed_list_in_both_copies(self):
        # Every way explain exits 2, in its docstring and in docs/operations.md alike.
        rc, out = self.run_cli(["explain", "--pr", "5"])
        self.assertEqual(rc, 2, out)
        self.assertIn("no loops configured in", out)
        doc = " ".join(cli.cmd_explain.__doc__.split())
        ops = " ".join((ROOT / "docs" / "operations.md").read_text().split())
        for case in ("an unknown loop", "a loop file the loader refuses", "no `--loop`",
                     "no loop files at all"):
            self.assertIn(case.replace("`", "``"), doc, case)
            self.assertIn(case, ops, case)
        # and the formatter's docstring names only its real callers
        self.assertNotIn("``explain``):", cli._readable_loops.__doc__)
        self.assertIn("``explain`` calls ``config.readable_loops``", " ".join(
            cli._readable_loops.__doc__.split()))

    def test_apply_refuses_a_reader_with_no_token_file(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        data = json.loads(self.loop_file().read_text())
        data["read_token"] = "ghost-account"         # hand-edited; no tokens entry for it
        self.loop_file().write_text(json.dumps(data))
        before = self.loop_file().read_bytes()
        settings = {"reviewer_profile": "vex", "fixer_profile": "drey", "reviewer_login": REV,
                    "fixer_login": FIX, "host": "https://gateway.example", "cap": 5}
        rc, out = self.run_cli(["apply", "--loop", "widgets"], settings)
        self.assertEqual(rc, 2, out)
        self.assertIn("read_token 'ghost-account' has no entry in 'tokens'", out)
        self.assertIn("fix: hermes review-loop set --loop widgets --read-token ghost-account "
                      "--token ghost-account=", out)
        self.assertEqual(self.loop_file().read_bytes(), before)

    def test_set_read_token_refusals(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        before = self.loop_file().read_bytes()
        out = self.refused(["set", "--loop", "widgets", "--read-token", FIX],
                           r"also the fixer seat")
        self.assertIn("four-identity rule", out)
        self.refused(["set", "--loop", "widgets", "--read-token", "ghost"],
                     r"no token file mapped for the reader 'ghost'")
        self.refused(["set", "--loop", "widgets", "--read-token", "twin",
                      "--token", f"twin={self.keys / 'fix-pat'}"], r"read the same token file")
        loose = self.pat("loose", 0o644)
        self.refused(["set", "--loop", "widgets", "--read-token", "loose",
                      "--token", f"loose={loose}"], r"group/other can read it")
        self.refused(["set", "--loop", "widgets", "--token", f"{REV}={self.keys / 'rev-pat'}"],
                     r"only maps the token file of the login named by --read-token")
        self.assertEqual(self.loop_file().read_bytes(), before)


class HookWriteTests(_Loop):
    """Who edits the hooks, and what that login's file must carry. `arm`'s read-back and exit
    codes are tests/test_arm_verify.py's; these pin what init states and what the fix names."""

    def hook_fetch(self, patch_error):
        calls = []
        state = {1: False, 2: False}

        def fetch(loop, path, method="GET", body=None, login=None):
            calls.append((method, path, login))
            if path.endswith("/hooks?per_page=100"):
                # Each hook at its route's own URL (the seat's profile is part of it).
                return [{"id": n, "active": state[n], "events": [event], "config": {
                    "url": f"https://gateway.example/p/{profile}/webhooks/widgets-{r}",
                    "content_type": "json"}}
                    for n, r, profile, event in ((1, "review", "vex", "pull_request"),
                                                 (2, "fix", "drey", "pull_request_review"))], ""
            hook_id = int(path.rsplit("/", 1)[-1])
            if method == "PATCH":
                if patch_error:
                    return None, patch_error
                state[hook_id] = body["active"]
            return {"id": hook_id, "active": state[hook_id]}, ""
        return fetch, calls

    def test_dry_run_states_who_edits_the_hooks_and_what_it_needs(self):
        rc, out = self.run_cli(self.init_argv("--hooks", "--dry-run"))
        self.assertEqual(rc, 0, out)
        # Paused until `arm`: the line must not say "armed" under "paused until `arm`".
        self.assertIn(f"hooks would be created paused as {READER}, and `arm` / `arm --pause` "
                      f"edit them as {READER} too", out)
        self.assertNotIn("created and armed", out)
        self.assertIn("repository_hooks: write", out)
        self.assertIn("that is the reader's file", out)
        self.pat("owner")
        rc, out = self.run_cli(self.init_argv("--hooks", "--admin-token", "owner",
                                              "--token", f"owner={self.keys / 'owner-pat'}",
                                              "--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertIn("hooks would be created paused as owner", out)
        self.assertNotIn("that is the reader's file", out)
        rc, out = self.run_cli(self.init_argv("--hooks", "--arm", "--dry-run"))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hooks would be created armed (--arm) as {READER}", out)

    def test_a_successful_init_hooks_states_who_edits_them(self):
        # README: "`init --hooks` prints the login and this need" — on the real path, not only
        # in --dry-run or on failure.
        def api(loop, path, method="GET", body=None, login=None):
            if path.endswith("/hooks?per_page=100"):
                return []
            return {"id": 1 if body["events"] == ["pull_request"] else 2} if method == "POST" else None
        def fetch(loop, path, method="GET", body=None, login=None):
            # #57's stale-hook preflight reads the listing this way; nothing else may go out.
            self.assertEqual((method, path), ("GET", "/repos/acme/widgets/hooks?per_page=100"))
            return [], ""
        with patch("review_loop.gh.api", side_effect=api), \
                patch("review_loop.gh.fetch", side_effect=fetch):
            rc, out = self.run_cli(self.init_argv("--hooks"))
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hooks were created paused as {READER}, and `arm` / `arm --pause` edit "
                      f"them as {READER} too", out)
        self.assertIn("repository_hooks: write", out)
        self.assertIn("that is the reader's file", out)

    def test_next_step_arms_as_the_admin_login(self):
        self.pat("owner")
        created = []

        def api(loop, path, method="GET", body=None, login=None):
            if path.endswith("/hooks?per_page=100"):
                return []
            if method == "POST":
                created.append(login)
                return {"id": len(created)}
            return None
        listed = []

        def fetch(loop, path, method="GET", body=None, login=None):
            # init's stale-hook preflight (#57) reads the complete listing before writing
            # anything; any other call would reach the real API, so it fails the test instead.
            self.assertEqual((method, path), ("GET", "/repos/acme/widgets/hooks?per_page=100"))
            listed.append(login)
            return [], ""
        with patch("review_loop.gh.api", side_effect=api), \
                patch("review_loop.gh.fetch", side_effect=fetch):
            rc, out = self.run_cli(self.init_argv("--hooks", "--admin-token", "owner",
                                                  "--token", f"owner={self.keys / 'owner-pat'}"))
        self.assertEqual(rc, 0, out)
        self.assertEqual(created, ["owner", "owner"])
        self.assertEqual(listed, ["owner"])  # the preflight reads as the hook admin too
        self.assertIn("hermes review-loop arm --loop widgets --admin-token owner", out)

    def test_a_refused_arm_as_the_reader_names_the_scope_and_the_owner_case(self):
        rc, out = self.run_cli(self.init_argv())
        self.assertEqual(rc, 0, out)
        fetch, calls = self.hook_fetch("HTTP 403 Resource not accessible")
        with patch("review_loop.gh.fetch", side_effect=fetch):
            rc, out = self.run_cli(["arm", "--loop", "widgets"])
        self.assertEqual(rc, 1, out)
        self.assertIn("repository_hooks: write", out)
        self.assertIn("that is the reader's file", out)
        self.assertIn("only the owner can manage hooks", out)
        self.assertEqual({login for method, _, login in calls if method == "PATCH"}, {READER})


if __name__ == "__main__":
    unittest.main()
