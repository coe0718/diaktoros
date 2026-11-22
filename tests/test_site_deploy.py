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


class DeploySite(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.jobs = {name: "\n".join(lines) for name, lines in jobs(self.text).items()}

    def test_every_job_is_one_this_reader_understands(self):
        self.assertEqual(sorted(self.jobs), ["build", "deploy", "redeploy"])

    def test_build_refreshes_the_snapshot_before_the_page_is_copied(self):
        build = self.jobs["build"]
        refresh = build.find("run: python3 docs/site/build-data.py")
        copy = build.find("cp -R docs/site/. site/")
        self.assertNotEqual(refresh, -1, "the build never runs build-data.py")
        self.assertNotEqual(copy, -1)
        self.assertLess(refresh, copy, "data.json must be refreshed before it is copied")
        # Authenticated, so the search counts do not hit the anonymous rate limit.
        self.assertRegex(build, r"\n\s+GITHUB_TOKEN: \$\{\{ github\.token \}\}")

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
