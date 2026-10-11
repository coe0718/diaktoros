"""Regression tests for the launch page's self-updating statistics.

docs/site/index.html fetches live repository stats from the GitHub API on
every page load, falling back to the checked-in data.json snapshot and then
to the hardcoded values. These tests run the page's own script under node
with mocked fetch/document across all three paths, so they fail if the
refresh behavior is removed or broken.
"""
import _home_guard  # noqa: F401  # first import: temp HOME/HERMES_HOME
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"

HARNESS = r"""
const fs = require('fs');
const pageScript = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
// Stand-ins for the hardcoded snapshot values baked into the HTML.
const HARDCODED = { version: 'v0.0.0-test', loop_prs_merged: '0-test',
                    issues_closed: '0-test', generated: '1970-01-01-test' };
const elements = {};
// Real DOM textContent coerces assigned values to strings; mirror that.
function makeEl(v) {
  let t = String(v);
  return { set textContent(x) { t = String(x); }, get textContent() { return t; } };
}
for (const [k, v] of Object.entries(HARDCODED)) elements[k] = [makeEl(v)];
global.document = {
  querySelectorAll: (sel) => {
    const m = /\[data-site="([^"]+)"\]/.exec(sel);
    return (m && elements[m[1]]) || [];
  },
};
global.fetch = (url) => {
  if (String(url).startsWith('https://api.github.com')) {
    if (scenario === 'live-ok') {
      const body = String(url).includes('/search/')
        ? { total_count: 4242 }
        : { tag_name: 'v9.9.9-test' };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    }
    return Promise.resolve({ ok: false, status: 403 });
  }
  if (url === 'data.json') {
    if (scenario === 'live-fail-snapshot-ok') {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        { version: 'v1.2.3-test', loop_prs_merged: 7,
          issues_closed: 8, generated: '2026-01-02' }) });
    }
    return Promise.resolve({ ok: false, status: 404 });
  }
  return Promise.reject(new Error('unexpected fetch: ' + url));
};
eval(pageScript);
setTimeout(() => {
  const out = {};
  for (const [k, v] of Object.entries(elements)) out[k] = v[0].textContent;
  console.log(JSON.stringify(out));
}, 200);
"""


def extract_refresh_script():
    html = (SITE / "index.html").read_text()
    marker = "// Live numbers"
    idx = html.find(marker)
    if idx == -1:
        raise AssertionError("live-refresh script marker not found in index.html")
    start = html.rfind("<script>", 0, idx) + len("<script>")
    end = html.find("</script>", idx)
    return html[start:end]


class SiteRefreshTests(unittest.TestCase):
    def run_scenario(self, scenario):
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, True)
        script_path = pathlib.Path(tmpdir) / "page.js"
        script_path.write_text(extract_refresh_script())
        harness_path = pathlib.Path(tmpdir) / "harness.js"
        harness_path.write_text(HARNESS)
        proc = subprocess.run(
            ["node", str(harness_path), str(script_path), scenario],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, f"node harness failed: {proc.stderr}")
        return json.loads(proc.stdout)

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_live_api_updates_stats(self):
        out = self.run_scenario("live-ok")
        self.assertEqual(out["version"], "v9.9.9-test")
        self.assertEqual(out["loop_prs_merged"], "4242")
        self.assertEqual(out["issues_closed"], "4242")
        self.assertEqual(out["generated"], datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_api_failure_falls_back_to_snapshot(self):
        out = self.run_scenario("live-fail-snapshot-ok")
        self.assertEqual(out["version"], "v1.2.3-test")
        self.assertEqual(out["loop_prs_merged"], "7")
        self.assertEqual(out["issues_closed"], "8")
        self.assertEqual(out["generated"], "2026-01-02")

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_total_failure_preserves_hardcoded_values(self):
        out = self.run_scenario("all-fail")
        self.assertEqual(out["version"], "v0.0.0-test")
        self.assertEqual(out["loop_prs_merged"], "0-test")
        self.assertEqual(out["issues_closed"], "0-test")
        self.assertEqual(out["generated"], "1970-01-01-test")

    def test_snapshot_covers_every_page_key(self):
        """Every data-site key in the HTML must exist in data.json,
        or the snapshot fallback silently misses it."""
        import re
        html = (SITE / "index.html").read_text()
        keys = set(re.findall(r'data-site="(\w+)"', html))
        self.assertTrue(keys, "no data-site keys found in index.html")
        data = json.loads((SITE / "data.json").read_text())
        missing = keys - set(data)
        self.assertFalse(missing, f"data.json missing keys used by the page: {missing}")


if __name__ == "__main__":
    unittest.main()
