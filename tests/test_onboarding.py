"""#1598: the onboarding record: per-stage evidence whose fingerprint goes stale.

Offline: a real temporary state directory, runtime file and token files; the seat resolver, the
PR head read and the hook-state read are injected.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diaktoros import onboarding, prompts, seat_model  # noqa: E402

PAT = "ghp_" + "S" * 36
APP_KEY = "-----BEGIN RSA PRIVATE KEY-----APPKEYBYTES0123456789-----END RSA PRIVATE KEY-----"
MODEL_KEY = "sk-MODELKEYBYTES-0123456789abcdef"
HEAD = "a" * 40
STAGES = onboarding.STAGES


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, text in (("reader-pat", PAT), ("app.pem", APP_KEY), ("model-key", MODEL_KEY)):
            (self.root / name).write_text(text)
        self.runtime = self.root / "runtime.json"
        self.write_runtime()
        self.loop = {
            "id": "widgets", "repo": "acme/widgets", "state_dir": str(self.root / "state"),
            "read_token": "reader-bot", "tokens": {"reader-bot": str(self.root / "reader-pat")},
            "apps": {"critic[bot]": {"app_id": 7, "key_file": str(self.root / "app.pem")}},
            "seats": {"reviewer": {"login": "critic[bot]", "profile": "rev"},
                      "fixer": {"login": "coder", "profile": "fix"}}}
        self.models = {"reviewer": "model-a", "fixer": "model-b"}
        self.head = HEAD
        self.hooks = "armed"

    def write_runtime(self, **extra):
        body = {"source": "/s", "venv": "/v", "runtime": "/r", "rust": "/c", **extra}
        self.runtime.write_text(json.dumps(body))

    def resolver(self, profile, seat, settings):
        return seat_model.SeatInference(seat, profile, "profile", "prov", self.models[seat],
                                        "https://api.example/v1/chat/completions", MODEL_KEY)

    def record(self, stage, status="passed", summary="ok", **kw):
        kw.setdefault("runtime_file", self.runtime)
        kw.setdefault("resolver", self.resolver)
        if stage == "live_turn":
            kw.setdefault("pr", (12, self.head))
        if stage == "armed":
            kw.setdefault("hook_state", lambda loop: self.hooks)
        return onboarding.record(self.loop, stage, status, f"hermes dk {stage}", summary, **kw)

    def read(self):
        return onboarding.read(self.loop, runtime_file=self.runtime, resolver=self.resolver,
                               pr_head=lambda loop, number: self.head,
                               hook_state=lambda loop: self.hooks)

    def record_all(self):
        for stage in STAGES:
            self.record(stage)


class Reading(Fixture):
    def test_a_missing_record_reads_all_not_attempted(self):
        self.assertFalse(onboarding.path(self.loop).exists())
        self.assertEqual(self.read(), {stage: "not_attempted" for stage in STAGES})

    def test_a_malformed_record_reads_all_not_attempted(self):
        onboarding.path(self.loop).parent.mkdir(parents=True)
        onboarding.path(self.loop).write_text("{not json")
        self.assertEqual(set(self.read().values()), {"not_attempted"})

    def test_recorded_stages_read_back_as_recorded(self):
        self.record("identities", "passed")
        self.record("loop", "failed")
        self.record("model", "skipped")
        self.assertEqual(self.read(), {"identities": "passed", "loop": "failed",
                                       "model": "skipped", "live_turn": "not_attempted",
                                       "armed": "not_attempted"})
        self.record_all()
        self.assertEqual(set(self.read().values()), {"passed"})

    def test_the_entry_holds_status_time_command_summary_and_fingerprint(self):
        entry = self.record("loop", "passed", "all good")
        stored = json.loads(onboarding.path(self.loop).read_text())["stages"]["loop"]
        self.assertEqual(stored, entry)
        self.assertEqual(entry["status"], "passed")
        self.assertEqual(entry["command"], "hermes dk loop")
        self.assertEqual(entry["summary"], "all good")
        self.assertRegex(entry["at"], r"^\d{4}-\d\d-\d\dT")
        self.assertRegex(entry["fingerprint"], r"^[0-9a-f]{64}$")

    def test_an_unknown_stage_or_status_is_refused(self):
        with self.assertRaises(ValueError):
            self.record("bogus")
        with self.assertRaises(ValueError):
            self.record("loop", "stale")

    def test_a_skip_or_warn_in_a_required_check_is_never_passed(self):
        every = {name: "pass" for name in onboarding.REQUIRED_CHECKS["model"]}
        self.models["fixer"] = "model-a"       # one shared model: one completion to expect
        self.assertEqual(self.record("model", checks=every)["status"], "passed")
        self.assertEqual(self.record("model", checks={**every, "model:completion": "skip"})
                         ["status"], "skipped")
        self.assertEqual(self.record("model", checks={**every, "seat:fixer": "warn"})["status"],
                         "failed")
        self.assertEqual(self.record("model", checks={**every, "seat:fixer": "fail"})["status"],
                         "failed")
        missing = {k: v for k, v in every.items() if k != "model:completion"}
        self.assertEqual(self.record("model", checks=missing)["status"], "skipped")
        self.assertEqual(self.read()["model"], "skipped")
        doctors = {name: "verified" for name in onboarding.REQUIRED_CHECKS["model"]}
        self.assertEqual(self.record("model", checks=doctors)["status"], "passed")
        self.assertEqual(self.record("model", checks={**doctors, "seat:fixer": "unknown"})
                         ["status"], "failed")


class Staleness(Fixture):
    def setUp(self):
        super().setUp()
        self.record_all()
        self.assertEqual(set(self.read().values()), {"passed"})

    def assert_stale_from(self, stage):
        got = self.read()
        index = STAGES.index(stage)
        self.assertEqual(got, {s: ("passed" if i < index else "stale")
                               for i, s in enumerate(STAGES)})

    def test_changing_a_token_mapping_makes_identities_and_all_after_it_stale(self):
        (self.root / "other-pat").write_text("x")
        self.loop["tokens"] = {"reader-bot": str(self.root / "other-pat")}
        self.assert_stale_from("identities")

    def test_changing_an_app_mapping_makes_identities_stale(self):
        self.loop["apps"]["critic[bot]"]["app_id"] = 8
        self.assert_stale_from("identities")

    def test_the_token_file_contents_are_not_part_of_the_fingerprint(self):
        (self.root / "reader-pat").write_text("ghp_" + "Z" * 36)     # a rotated token, same mapping
        self.assertEqual(set(self.read().values()), {"passed"})

    def test_changing_the_runtime_file_makes_loop_and_later_stages_stale(self):
        self.write_runtime(rust="/other")
        self.assert_stale_from("loop")

    def test_changing_a_seat_profile_makes_loop_stale(self):
        self.loop["seats"]["fixer"]["profile"] = "fix2"
        self.assert_stale_from("loop")

    def test_changing_a_seats_model_makes_model_and_later_stages_stale(self):
        self.models["fixer"] = "model-c"
        self.assert_stale_from("model")

    def test_changing_the_pr_head_makes_live_turn_and_armed_stale(self):
        self.head = "b" * 40
        self.assert_stale_from("live_turn")

    def test_an_unreadable_pr_head_is_stale_not_passed(self):
        def boom(loop, number):
            raise OSError("down")
        got = onboarding.read(self.loop, runtime_file=self.runtime, resolver=self.resolver,
                              pr_head=boom, hook_state=lambda loop: self.hooks)
        self.assertEqual(got["live_turn"], "stale")

    def test_changed_hook_state_makes_armed_stale(self):
        self.hooks = "paused"
        self.assert_stale_from("armed")

    def test_a_change_before_a_stage_leaves_an_unrecorded_one_not_attempted(self):
        onboarding.path(self.loop).unlink()
        self.record("identities")
        self.record("model")
        self.models["reviewer"] = "model-z"
        self.assertEqual(self.read(), {"identities": "passed", "loop": "not_attempted",
                                       "model": "stale", "live_turn": "not_attempted",
                                       "armed": "not_attempted"})

    def test_re_recording_a_stale_stage_makes_it_current_again(self):
        self.models["fixer"] = "model-c"
        self.record("model")
        self.record("live_turn")
        self.record("armed")
        self.assertEqual(set(self.read().values()), {"passed"})

    def test_the_live_turn_fingerprint_follows_the_reviewer_prompt(self):
        before = onboarding.fingerprint(self.loop, "live_turn", pr=(12, HEAD))
        real = prompts.revision
        prompts.revision = lambda role: "changed"
        try:
            after = onboarding.fingerprint(self.loop, "live_turn", pr=(12, HEAD))
        finally:
            prompts.revision = real
        self.assertNotEqual(before, after)


class OnDisk(Fixture):
    def test_the_file_is_0600_and_holds_no_token_or_key_bytes(self):
        self.record_all()
        file = onboarding.path(self.loop)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        text = file.read_text()
        for secret in (PAT, APP_KEY, MODEL_KEY, "APPKEYBYTES", "MODELKEYBYTES"):
            self.assertNotIn(secret, text)
        self.assertNotIn(seat_model.SeatInference(
            "reviewer", "rev", "profile", "p", "m", "u", MODEL_KEY).identity()[3], text)

    def test_a_rewrite_keeps_0600(self):
        self.record("loop")
        onboarding.path(self.loop).chmod(0o644)
        self.record("loop")
        self.assertEqual(stat.S_IMODE(onboarding.path(self.loop).stat().st_mode), 0o600)

    def test_a_summary_is_redacted_and_one_line(self):
        entry = self.record("identities", summary=f"reader token {PAT}\nand key {MODEL_KEY} ok")
        self.assertNotIn("\n", entry["summary"])
        self.assertNotIn(PAT, entry["summary"])
        self.assertNotIn(MODEL_KEY, entry["summary"])
        stored = onboarding.path(self.loop).read_text()
        self.assertNotIn(PAT, stored)
        self.assertNotIn(MODEL_KEY, stored)
        # A secret configured on this loop is redacted even when it has no token shape.
        (self.root / "reader-pat").write_text("plainsecretvalue")
        entry = self.record("identities", summary="the pat is plainsecretvalue")
        self.assertNotIn("plainsecretvalue", entry["summary"])

    def test_recording_one_stage_keeps_the_others(self):
        self.record("identities")
        self.record("loop", "failed")
        self.assertEqual(self.read()["identities"], "passed")
        self.assertEqual(self.read()["loop"], "failed")


PROFILE_KEY = "hunter2-profile-key-no-token-shape"
LOOP_OK = [(name, "pass") for name in onboarding.REQUIRED_CHECKS["loop"]]


class Review1(Fixture):
    """Vex's round-1 findings on PR #1606."""

    def test_re_recording_only_an_earlier_stale_stage_does_not_revive_later_ones(self):    # F1
        self.record_all()
        self.models["fixer"] = "model-c"
        self.assertEqual(self.read()["model"], "stale")
        self.record("model")
        self.assertEqual(self.read(), {"identities": "passed", "loop": "passed",
                                       "model": "passed", "live_turn": "stale",
                                       "armed": "stale"})
        # Recording them again is what makes them current.
        self.record("live_turn")
        self.record("armed")
        self.assertEqual(set(self.read().values()), {"passed"})

    def test_re_recording_unchanged_inputs_leaves_later_stages_alone(self):                # F1
        self.record_all()
        self.record("model")
        self.assertEqual(set(self.read().values()), {"passed"})

    def test_a_profile_key_with_no_token_shape_is_redacted_from_command_and_summary(self):   # F2
        def resolver(profile, seat, settings):
            return seat_model.SeatInference(seat, profile, "profile", "prov", "m",
                                            "https://api.example/v1/chat/completions",
                                            PROFILE_KEY)
        entry = onboarding.record(
            self.loop, "model", "passed", f"dk model --key {PROFILE_KEY}",
            f"fixer answered with {PROFILE_KEY}", runtime_file=self.runtime, resolver=resolver)
        self.assertNotIn(PROFILE_KEY, json.dumps(entry))
        self.assertNotIn(PROFILE_KEY, onboarding.path(self.loop).read_text())
        self.assertIn("[REDACTED]", entry["summary"])
        self.assertIn("[REDACTED]", entry["command"])

    def test_an_unknown_hook_state_is_never_passed_and_never_current(self):               # F3
        entry = self.record("armed", hook_state=lambda loop: "unknown")
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["fingerprint"], "")
        self.assertEqual(self.record("armed", "skipped", hook_state=lambda loop: "unknown")
                         ["status"], "skipped")
        self.hooks = "unknown"
        self.assertEqual(self.read()["armed"], "stale")

    def test_the_real_hook_adapter_answering_unknown_reads_stale(self):                    # F3
        from diaktoros import arm_action
        real = arm_action.plan
        self.addCleanup(setattr, arm_action, "plan", real)
        arm_action.plan = lambda loop, token_login=None: {"state": "unknown", "eligible": False}
        entry = onboarding.record(self.loop, "armed", "passed", "dk arm", "ok",
                                  runtime_file=self.runtime, resolver=self.resolver)
        self.assertEqual(entry["status"], "failed")
        got = onboarding.read(self.loop, runtime_file=self.runtime, resolver=self.resolver)
        self.assertEqual(got["armed"], "stale")
        arm_action.plan = lambda loop, token_login=None: {"state": "armed"}
        self.assertEqual(onboarding.fingerprint(self.loop, "armed"),
                         onboarding.fingerprint(self.loop, "armed", hook_state=lambda l: "armed"))

    def test_the_names_selftest_emits_cover_every_required_check(self):                    # F4
        import re
        from diaktoros import selftest
        source = Path(selftest.__file__).read_text()
        emitted = set(re.findall(r'report\.add\([^,]+,\s*(?:f?"([^"{]+)")', source))
        for stage, names in onboarding.REQUIRED_CHECKS.items():
            for name in names:
                # f-string names (github:{role}, seat:{seat}) and the arm_action placeholder
                if name != "arm:hooks" and not name.startswith(("github:r", "github:f", "seat:")):
                    self.assertIn(name, emitted, f"{stage}: selftest never emits {name}")

    def test_a_healthy_loop_report_passes_and_containment_trouble_does_not(self):          # F4
        self.assertEqual(onboarding.stage_status("loop", "passed", LOOP_OK, loop=self.loop),
                         "passed")
        for status, want in (("skip", "skipped"), ("fail", "failed")):
            got = onboarding.stage_status(
                "loop", "passed", LOOP_OK + [("sandbox:containment", status)], loop=self.loop)
            self.assertEqual(got, want)
        broken = [(n, "fail" if n == "sandbox:network" else s) for n, s in LOOP_OK]
        self.assertEqual(onboarding.stage_status("loop", "passed", broken, loop=self.loop),
                         "failed")
        self.assertEqual(onboarding.stage_status("loop", "passed", LOOP_OK[:-1], loop=self.loop),
                         "skipped")

    def test_identity_safety_failures_outside_the_required_list_block_passed(self):        # F5
        ok = [(name, "pass") for name in onboarding.REQUIRED_CHECKS["identities"]]
        self.assertEqual(self.record("identities", checks=ok)["status"], "passed")
        for extra in ("github:distinct-files", "github:adjudicator"):
            entry = self.record("identities", checks=ok + [(extra, "fail")])
            self.assertEqual(entry["status"], "failed", extra)

    def test_a_configured_adjudicator_is_required_and_an_unconfigured_one_is_not(self):    # F5
        ok = [(name, "pass") for name in onboarding.REQUIRED_CHECKS["identities"]]
        self.assertEqual(onboarding.stage_status("identities", "passed", ok, loop=self.loop),
                         "passed")
        self.loop["seats"]["adjudicator"] = {"login": "judge"}
        self.assertEqual(onboarding.stage_status("identities", "passed", ok, loop=self.loop),
                         "skipped")
        self.assertEqual(onboarding.stage_status(
            "identities", "passed", ok + [("github:adjudicator", "pass")], loop=self.loop),
            "passed")
        self.assertEqual(onboarding.stage_status(
            "identities", "passed", ok + [("github:adjudicator", "fail")], loop=self.loop),
            "failed")

    def _model_report(self, outcomes):
        """selftest.check_model's real grouping, with each completion's outcome stubbed."""
        from diaktoros import selftest
        seats = {seat: self.resolver("p", seat, {}) for seat in ("reviewer", "fixer")}
        results = iter(outcomes)
        real = selftest._one_completion
        self.addCleanup(setattr, selftest, "_one_completion", real)
        selftest._one_completion = lambda report, names, inference: report.add(
            "inference", "model:completion", next(results), names)
        report = selftest.Report(out=io.StringIO())
        selftest.check_model(report, seats)
        seat_checks = [(f"seat:{seat}", "pass") for seat in seats]
        return seat_checks + onboarding.report_checks(
            [r for r in report.results if r[1] == "model:completion"])

    def test_one_failing_model_completion_fails_the_model_stage_in_either_order(self):     # F6
        for order in (("fail", "pass"), ("pass", "fail")):
            checks = self._model_report(order)
            self.assertEqual(len([c for c in checks if c[0] == "model:completion"]), 2)
            self.assertEqual(self.record("model", checks=checks)["status"], "failed", order)
        self.assertEqual(self.record("model", checks=self._model_report(("pass", "pass")))
                         ["status"], "passed")
        self.assertEqual(self.record("model", checks=self._model_report(("pass", "skip")))
                         ["status"], "skipped")

    def test_a_shared_model_needs_one_completion_and_distinct_models_need_each(self):      # F6
        checks = [("seat:reviewer", "pass"), ("seat:fixer", "pass"), ("model:completion", "pass")]
        self.assertEqual(self.record("model", checks=checks)["status"], "skipped")   # 2 models
        self.models["fixer"] = "model-a"
        self.assertEqual(self.record("model", checks=checks)["status"], "passed")    # shared


class Review2(Fixture):
    """Vex's round-2 findings on PR #1606."""

    def _keyed(self, key, fail_after=None):
        """A resolver returning ``key``; after ``fail_after`` calls it raises, as a profile whose
        Hermes read fails the second time."""
        calls = []

        def resolver(profile, seat, settings):
            calls.append(seat)
            if fail_after is not None and len(calls) > fail_after:
                raise seat_model.SeatModelError("profile unreadable")
            return seat_model.SeatInference(seat, profile, "profile", "prov", "m",
                                            "https://api.example/v1/chat/completions", key)
        return resolver

    def test_a_profile_key_shorter_than_four_characters_is_not_written(self):              # F2
        key = "xyz"
        entry = onboarding.record(
            self.loop, "model", "passed", f"run {key}", f"key {key}",
            runtime_file=self.runtime, resolver=self._keyed(key))
        self.assertNotIn(key, entry["command"])
        self.assertNotIn(key, entry["summary"])
        self.assertIn("withheld", entry["summary"])
        self.assertNotIn(f"key {key}", onboarding.path(self.loop).read_text())

    def test_a_failed_resolution_withholds_a_model_stage_text(self):                       # F2
        # The check saw the key through a first resolution; recording resolves again and fails.
        key = "profile-key-seen-by-the-check"
        for stage, extra in (("model", {}), ("live_turn", {"pr": (12, HEAD)})):
            entry = onboarding.record(
                self.loop, stage, "failed", f"dk {stage} --key {key}", f"rejected {key}",
                runtime_file=self.runtime, resolver=self._keyed(key, fail_after=0), **extra)
            self.assertNotIn(key, json.dumps(entry), stage)
        self.assertNotIn(key, onboarding.path(self.loop).read_text())

    def test_secrets_the_caller_saw_are_redacted_and_short_ones_withhold(self):            # F2
        key = "caller-saw-this-profile-key"
        entry = onboarding.record(
            self.loop, "model", "failed", f"dk model {key}", f"bad {key}",
            runtime_file=self.runtime, resolver=self.resolver, secrets=[key])
        self.assertNotIn(key, json.dumps(entry))
        self.assertIn("[REDACTED]", entry["summary"])
        entry = onboarding.record(
            self.loop, "model", "failed", "dk model ab", "bad ab",
            runtime_file=self.runtime, resolver=self.resolver, secrets=["ab"])
        self.assertIn("withheld", entry["command"])

    def test_text_is_kept_when_every_credential_is_redactable(self):                       # F2
        entry = self.record("model", summary="both seats answered")
        self.assertEqual(entry["summary"], "both seats answered")

    def test_a_live_turn_needs_a_head_to_record(self):                                     # F7
        for head in ("", "  ", None):
            with self.assertRaises(ValueError):
                self.record("live_turn", pr=(12, head))
        self.assertFalse(onboarding.path(self.loop).exists())

    def test_an_empty_live_head_reads_stale_never_passed(self):                            # F7
        self.record("live_turn")
        for lookup in ("", "  ", None):
            got = onboarding.read(self.loop, runtime_file=self.runtime, resolver=self.resolver,
                                  pr_head=lambda loop, number, v=lookup: v,
                                  hook_state=lambda loop: self.hooks)
            self.assertEqual(got["live_turn"], "stale", repr(lookup))

    def test_an_entry_stored_with_an_empty_head_never_reads_passed(self):                  # F7
        # A record written before the guard (or by hand) holds the hash of an empty head.
        entry = self.record("live_turn")
        stored = json.loads(onboarding.path(self.loop).read_text())
        stored["stages"]["live_turn"]["fingerprint"] = onboarding._digest(
            ["live_turn", {"pr": 12, "head": "", "prompt": prompts.revision("reviewer")}])
        onboarding.path(self.loop).write_text(json.dumps(stored))
        got = onboarding.read(self.loop, runtime_file=self.runtime, resolver=self.resolver,
                              pr_head=lambda loop, number: "", hook_state=lambda l: self.hooks)
        self.assertEqual(entry["status"], "passed")
        self.assertEqual(got["live_turn"], "stale")


class UnresolvedSeats(Fixture):
    """F8 of the ACCEPT ruling on #1606: an unknown input never matches another unknown."""

    def _failing(self, profile, seat, settings):
        raise seat_model.SeatModelError("profile unreadable")

    def test_the_model_fingerprint_raises_when_a_seat_does_not_resolve(self):
        with self.assertRaises(onboarding.SeatUnresolved):
            onboarding.fingerprint(self.loop, "model", runtime_file=self.runtime,
                                   resolver=self._failing)

    def test_record_never_stores_model_passed_when_no_seat_resolves(self):
        entry = onboarding.record(
            self.loop, "model", "passed", "dk selftest", "ok", runtime_file=self.runtime,
            resolver=self._failing)
        self.assertNotEqual(entry["status"], "passed")
        self.assertEqual(entry["fingerprint"], "")
        self.assertNotEqual(self.read()["model"], "passed")

    def test_a_passed_model_check_does_not_survive_resolution_failing_later(self):
        entry = self.record("model")
        self.assertEqual(entry["status"], "passed")
        self.assertEqual(self.read()["model"], "passed")
        # Resolution now fails for every seat: "unresolved then" must not match "unresolved now".
        for _ in range(2):
            got = onboarding.read(self.loop, runtime_file=self.runtime, resolver=self._failing,
                                  pr_head=lambda loop, number: self.head,
                                  hook_state=lambda loop: self.hooks)
            self.assertEqual(got["model"], "stale")
        onboarding.record(self.loop, "model", "passed", "dk selftest", "ok",
                          runtime_file=self.runtime, resolver=self._failing)
        got = onboarding.read(self.loop, runtime_file=self.runtime, resolver=self._failing)
        self.assertNotIn("passed", (got["model"], got["live_turn"], got["armed"]))

    def test_one_unresolved_seat_is_enough(self):
        def half(profile, seat, settings):
            if seat == "fixer":
                raise seat_model.SeatModelError("profile unreadable")
            return self.resolver(profile, seat, settings)
        entry = onboarding.record(self.loop, "model", "passed", "dk selftest", "ok",
                                  runtime_file=self.runtime, resolver=half)
        self.assertEqual((entry["status"], entry["fingerprint"]), ("failed", ""))


if __name__ == "__main__":
    unittest.main()
