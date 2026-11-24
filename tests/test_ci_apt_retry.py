"""Every apt install in CI retries before it fails the job (#2678). On 2026-11-24 two jobs went red
at "Install bubblewrap" with apt exit 100 before any test ran (#2674's verticals, #2655's unit
3.14 2/3): one bad mirror answer cost a CI-fix turn and a manual re-run. Read as text, like
test_ci_timeouts: the tests are stdlib-only, so no YAML parser."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import unittest

WORKFLOWS = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"


class AptRetries(unittest.TestCase):
    def test_every_apt_install_is_inside_a_bounded_retry(self):
        seen, bare = 0, []
        for path in sorted(WORKFLOWS.glob("*.y*ml")):
            lines = path.read_text(encoding="utf-8").splitlines()
            for n, line in enumerate(lines):
                if "apt-get install" not in line:
                    continue
                seen += 1
                loop = lines[n - 1].strip() if n else ""
                after = [x.strip() for x in lines[n + 1:n + 4]]
                if not (loop.startswith("for attempt in ") and line.rstrip().endswith("&& break")
                        and any(x.endswith("|| exit 1") for x in after) and "done" in after):
                    bare.append(f"{path.name}:{n + 1}: {line.strip()}")
        self.assertGreater(seen, 0, "no apt install found: the scan is reading the wrong files")
        self.assertEqual(bare, [], "an apt install that fails the job on one bad mirror answer")


if __name__ == "__main__":
    unittest.main()
