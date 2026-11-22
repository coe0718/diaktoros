"""The Pages deploy refreshes the launch page's snapshot itself, on every push to main and every
published release, so a release never needs data.json edited by hand.

docs/site/build-data.py's docstring long said the deploy ran it while deploy-site.yml never did,
so the fallback version only moved when someone ran the script. Read as text: the tests are
stdlib-only, so no YAML parser.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import re
import unittest

from test_ci_timeouts import jobs

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy-site.yml"


def top(text: str, key: str) -> list[str]:
    """The lines under one top-level key, comments dropped."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            inside = line.split("#")[0].rstrip() == key + ":"
            continue
        if inside:
            out.append(line.split(" #")[0].rstrip())
    return out


def step(job: str, name: str) -> str:
    """One step of a job, from its ``- name:`` line to the next step."""
    out: list[str] = []
    for line in job.splitlines():
        if re.match(r"\s+- ", line):
            if out:
                break
            if line.strip() == "- name: " + name:
                out.append(line)
        elif out:
            out.append(line)
    return "\n".join(out)


REFRESH = "Refresh the launch page's release and statistics snapshot"


class DeploySite(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.jobs = {name: "\n".join(lines) for name, lines in jobs(self.text).items()}

    def test_every_job_is_one_this_reader_understands(self):
        self.assertEqual(sorted(self.jobs), ["build", "deploy", "redeploy"])

    def test_build_refreshes_the_snapshot_before_the_page_is_copied(self):
        build = self.jobs["build"]
        refresh = build.find("python3 docs/site/build-data.py")
        copy = build.find("cp -R docs/site/. site/")
        self.assertNotEqual(refresh, -1, "the build never runs build-data.py")
        self.assertNotEqual(copy, -1)
        self.assertLess(refresh, copy, "data.json must be refreshed before it is copied")
        # Authenticated, so the search counts do not hit the anonymous rate limit.
        self.assertIn("GITHUB_TOKEN: ${{ github.token }}", step(build, REFRESH))

    def test_a_published_release_redeploys_main(self):
        on = "\n".join(top(self.text, "on"))
        self.assertRegex(on, r"\n  release:\n    types: \[published\]")
        self.assertRegex(on, r"^  push:\n    branches: \[main\]")
        # The release run never builds from the tag: github-pages accepts main only.
        self.assertIn("if: github.event_name != 'release'", self.jobs["build"])
        self.assertIn("needs: build", self.jobs["deploy"])
        redeploy = self.jobs["redeploy"]
        self.assertIn("if: github.event_name == 'release'", redeploy)
        self.assertRegex(redeploy, r"permissions:\n\s+actions: write")
        self.assertRegex(redeploy, r'--ref "\$DEFAULT_BRANCH"')
        self.assertIn("DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}", redeploy)

    def test_the_refresh_is_bounded_and_never_fatal(self):
        # Vex, PR #2570 r1 F2: a hung refresh must not stall the deploy, and a timed-out or
        # crashed one must not fail the build.
        refresh = step(self.jobs["build"], REFRESH)
        self.assertRegex(refresh, r"\n        timeout-minutes: [1-9]\d?\n")
        self.assertRegex(refresh, r"\n        continue-on-error: true\n")

    def test_the_refresh_starts_from_the_published_snapshot(self):
        # Vex, PR #2570 r1 F1: the refresh is artifact-only, so an outage must fall back to
        # what the site last published, not the older checked-in file.
        refresh = step(self.jobs["build"], REFRESH)
        self.assertRegex(refresh, r'curl -fsS .*"\$PAGES_URL/data\.json" -o "\$RUNNER_TEMP/deployed-data\.json" \\\n\s+\|\| ')
        self.assertIn('build-data.py --previous "$RUNNER_TEMP/deployed-data.json"', refresh)
        self.assertIn("PAGES_URL: ${{ steps.pages.outputs.base_url }}", refresh)
        self.assertRegex(self.jobs["build"], r"- id: pages\n\s+uses: actions/configure-pages@")

    def test_the_last_snapshot_is_cached_for_the_next_deploy(self):
        # Vex, PR #2570 r3: when the published file cannot be fetched either, the build cache's
        # copy is what keeps an outage from publishing the older checked-in file.
        build = self.jobs["build"]
        key = r"key: site-data-\$\{\{ github\.run_id \}\}-\$\{\{ github\.run_attempt \}\}"
        restore = build.find("uses: actions/cache/restore@")
        save = build.find("uses: actions/cache/save@")
        refresh, keep = build.find("- name: " + REFRESH), build.find("- name: Keep this snapshot")
        self.assertTrue(-1 < restore < refresh < keep < save, (restore, refresh, keep, save))
        self.assertEqual(len(re.findall(key, build)), 2)
        self.assertRegex(build[restore:refresh], r"\n\s+restore-keys: site-data-\n")
        self.assertEqual(re.findall(r"path: (.*site-data)\n", build),
                         ["${{ runner.temp }}/site-data"] * 2)
        self.assertIn('--previous "$RUNNER_TEMP/site-data/data.json"', step(build, REFRESH))
        self.assertIn('cp docs/site/data.json "$RUNNER_TEMP/site-data/"',
                      step(build, "Keep this snapshot for the next deploy"))

    def test_the_dispatch_carries_a_token(self):
        # gh refuses to run without one; the release redeploy would quietly never happen.
        dispatch = step(self.jobs["redeploy"], "Deploy main again for the new release")
        self.assertIn("GH_TOKEN: ${{ github.token }}", dispatch)

    def test_redeploy_dispatches_this_very_workflow(self):
        named = re.findall(r"gh workflow run (\S+)", self.jobs["redeploy"])
        self.assertEqual(named, [WORKFLOW.name])

    def test_nothing_is_committed_back(self):
        # The refreshed data.json goes into the artifact only.
        self.assertNotRegex(self.text, r"git (commit|push)")
        self.assertNotIn("contents: write", self.text)
        self.assertIn("  contents: read", top(self.text, "permissions"))

    def test_one_deploy_at_a_time(self):
        self.assertEqual(top(self.text, "concurrency"),
                         ["  group: pages", "  cancel-in-progress: false"])


if __name__ == "__main__":
    unittest.main()
