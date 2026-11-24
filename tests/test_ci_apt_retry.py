"""CI's apt installs retry a bad mirror answer before failing the job (#2678). On 2026-11-24 two
jobs went red at "Install bubblewrap" with apt exit 100 before any test ran (#2674's verticals,
#2655's unit 3.14 2/3).

Every install goes through ``tools/apt_install.sh``, and that script is run here for real with a
stubbed ``sudo`` (failing the first N updates or installs with apt's exit 100) and a stubbed
``sleep`` (recording its delay), so retries, delay, warnings and exhaustion are all observed, with
no network and no wait. The workflows are read as text, like test_ci_timeouts.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / "tools" / "apt_install.sh"

SUDO = """#!/usr/bin/env bash
echo "$*" >> "$STUB_DIR/calls"
kind=$2
count="$STUB_DIR/$kind.count"
n=$(( $(cat "$count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$count"
limit_var="FAIL_${kind^^}"
[ "$n" -le "${!limit_var:-0}" ] && exit 100
exit 0
"""
SLEEP = """#!/usr/bin/env bash
echo "$1" >> "$STUB_DIR/sleeps"
"""


class Workflows(unittest.TestCase):
    def test_every_apt_install_goes_through_the_retrying_script(self):
        through, bare = 0, []
        for path in sorted(WORKFLOWS.glob("*.y*ml")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if "apt-get" in code:
                    bare.append(f"{path.name}:{n}: {line.strip()}")
                if "tools/apt_install.sh" in code:
                    through += 1
        self.assertEqual(bare, [], "an apt call outside tools/apt_install.sh fails on one bad answer")
        self.assertGreaterEqual(through, 4, "every bubblewrap install step runs the script")


class Script(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.stubs = pathlib.Path(temp.name)
        for name, body in (("sudo", SUDO), ("sleep", SLEEP)):
            (self.stubs / name).write_text(body)
            (self.stubs / name).chmod(0o755)

    def run_script(self, *packages, fail_update=0, fail_install=0):
        env = {**os.environ, "PATH": f"{self.stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
               "STUB_DIR": str(self.stubs), "FAIL_UPDATE": str(fail_update),
               "FAIL_INSTALL": str(fail_install)}
        done = subprocess.run(["bash", str(SCRIPT), *packages], env=env, capture_output=True,
                              text=True, timeout=30)

        def lines(name):
            path = self.stubs / name
            return path.read_text().splitlines() if path.exists() else []
        return done.returncode, done.stdout + done.stderr, lines("calls"), lines("sleeps")

    def test_a_first_success_installs_once_with_no_wait(self):
        rc, out, calls, sleeps = self.run_script("bubblewrap", "jq")
        self.assertEqual((rc, sleeps), (0, []))
        self.assertEqual(calls, ["apt-get update -q", "apt-get install -yq bubblewrap jq"])
        self.assertNotIn("::warning::", out)

    def test_two_failed_updates_are_retried_after_the_delay(self):
        rc, out, calls, sleeps = self.run_script("bubblewrap", fail_update=2)
        self.assertEqual((rc, sleeps), (0, ["15", "15"]))
        self.assertEqual(out.count("::warning::"), 2, out)
        self.assertEqual([c.split()[1] for c in calls], ["update", "update", "update", "install"])

    def test_two_failed_installs_are_retried_with_a_fresh_update_each_time(self):
        rc, out, calls, sleeps = self.run_script("bubblewrap", fail_install=2)
        self.assertEqual((rc, sleeps), (0, ["15", "15"]))
        self.assertEqual(out.count("::warning::"), 2, out)
        self.assertEqual([c.split()[1] for c in calls], ["update", "install"] * 3)

    def test_the_third_failure_fails_the_step_without_a_last_wait(self):
        for kind in ("update", "install"):
            with self.subTest(kind=kind):
                for name in ("calls", "sleeps", "update.count", "install.count"):
                    (self.stubs / name).unlink(missing_ok=True)
                rc, out, calls, sleeps = self.run_script("bubblewrap", **{f"fail_{kind}": 3})
                self.assertEqual((rc, sleeps), (1, ["15", "15"]))
                self.assertEqual(out.count("::warning::"), 2, out)
                self.assertIn("::error::apt failed 3 times", out)
                self.assertEqual(sum(c.split()[1] == kind for c in calls), 3)

    def test_no_package_is_a_usage_error_before_any_apt_call(self):
        rc, out, calls, _ = self.run_script()
        self.assertEqual((rc, calls), (2, []))
        self.assertIn("usage", out)


if __name__ == "__main__":
    unittest.main()
