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
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[1]
SITE = ROOT / "docs" / "site"

HARNESS = r"""
const fs = require('fs');
const pageScript = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
// Stand-ins for the hardcoded snapshot values baked into the HTML.
const HARDCODED = { version: 'v0.0.0-test', loop_prs_merged: '0-test',
                    issues_closed: '0-test', generated: '1970-01-01-test',
                    prs_merged: '0-test', loop_share: '0-test',
                    prs_open: '0-test', issues_open: '0-test',
                    commits_year: '0-test', last_activity: '0-test',
                    stars: '0-test', forks: '0-test' };
const elements = {};
// Real DOM textContent coerces assigned values to strings; mirror that.
function makeEl(v) {
  let t = String(v);
  return { set textContent(x) { t = String(x); }, get textContent() { return t; } };
}
for (const [k, v] of Object.entries(HARDCODED)) elements[k] = [makeEl(v)];
const noteEl = makeEl('');
const staleBox = { added: [] };
const staleEl = { classList: { add: (c) => staleBox.added.push(c) } };
global.document = {
  querySelectorAll: (sel) => {
    const m = /\[data-site="([^"]+)"\]/.exec(sel);
    if (m) return (m && elements[m[1]]) || [];
    if (sel.indexOf('.stats-freshness') !== -1) return [noteEl];
    if (sel === '.stats, .strip-stats') return [staleEl];
    return [];
  },
};
global.fetch = (url) => {
  if (String(url).startsWith('https://api.github.com')) {
    if (scenario === 'live-ok' || scenario === 'live-partial' || scenario === 'live-mixed') {
      const u = String(url);
      if (u.includes('/stats/commit_activity')) {
        // In the partial scenario this one endpoint fails while the rest
        // succeed, proving one rejection can't abort the live refresh.
        if (scenario === 'live-partial')
          return Promise.resolve({ ok: false, status: 403 });
        return Promise.resolve({ ok: true, json: () => Promise.resolve([{ total: 10 }, { total: 20 }]) });
      }
      if (u.includes('/search/')) {
        // In the mixed scenario the loop-author query fails while the
        // rest succeed, so the strip mixes live and snapshot counts.
        if (scenario === 'live-mixed' && u.includes('author'))
          return Promise.resolve({ ok: false, status: 403 });
        // The open-PRs query is the only one with is:open; keep its count
        // small so the derived open-issues math stays positive.
        const total = u.includes('is%3Aopen') ? 7 : 4242;
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ total_count: total }) });
      }
      if (u.endsWith('/repos/coe0718/diaktoros'))
        return Promise.resolve({ ok: true, json: () => Promise.resolve({
          stargazers_count: 11, forks_count: 3, open_issues_count: 50,
          pushed_at: new Date(Date.now() - 5 * 60000).toISOString() }) });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ tag_name: 'v9.9.9-test' }) });
    }
    return Promise.resolve({ ok: false, status: 403 });
  }
  if (url === 'data.json') {
    if (scenario === 'live-fail-snapshot-ok' || scenario === 'live-mixed') {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        { version: 'v1.2.3-test', loop_prs_merged: 7, prs_merged: 21,
          issues_closed: 8, prs_open: 3, open_issues_count: 11,
          stars: 5, forks: 2, commits_year: 345,
          pushed_at: new Date(Date.now() - 90 * 60000).toISOString(),
          generated: '2026-01-02' }) });
    }
    return Promise.resolve({ ok: false, status: 404 });
  }
  return Promise.reject(new Error('unexpected fetch: ' + url));
};
eval(pageScript);
setTimeout(() => {
  const out = {};
  for (const [k, v] of Object.entries(elements)) out[k] = v[0].textContent;
  out['__note'] = noteEl.textContent;
  out['__stale'] = staleBox.added.indexOf('stats-stale') !== -1;
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
        self.assertEqual(out["loop_prs_merged"], "4,242")
        self.assertEqual(out["issues_closed"], "4,242")
        self.assertEqual(out["prs_merged"], "4,242")
        self.assertEqual(out["loop_share"], "100%")
        self.assertEqual(out["prs_open"], "7")
        self.assertEqual(out["issues_open"], "43")
        self.assertEqual(out["commits_year"], "30")
        self.assertEqual(out["stars"], "11")
        self.assertEqual(out["forks"], "3")
        self.assertEqual(out["last_activity"], "5m ago")
        # The harness stamps the UTC date; allow yesterday too so the test
        # cannot flake if the run straddles 00:00:00Z.
        today = datetime.now(timezone.utc).date()
        self.assertIn(out["generated"],
                      {today.isoformat(), (today - timedelta(days=1)).isoformat()})

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_api_failure_falls_back_to_snapshot(self):
        out = self.run_scenario("live-fail-snapshot-ok")
        self.assertEqual(out["version"], "v1.2.3-test")
        self.assertEqual(out["loop_prs_merged"], "7")
        self.assertEqual(out["issues_closed"], "8")
        self.assertEqual(out["generated"], "2026-01-02")
        # The added stat keys must also fall back to the snapshot,
        # including the derived ones (share, open issues, relative time).
        self.assertEqual(out["prs_merged"], "21")
        self.assertEqual(out["loop_share"], "33%")
        self.assertEqual(out["prs_open"], "3")
        self.assertEqual(out["issues_open"], "8")
        self.assertEqual(out["commits_year"], "345")
        self.assertEqual(out["stars"], "5")
        self.assertEqual(out["forks"], "2")
        self.assertEqual(out["last_activity"], "1h ago")

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_total_failure_preserves_hardcoded_values(self):
        out = self.run_scenario("all-fail")
        self.assertEqual(out["version"], "v0.0.0-test")
        self.assertEqual(out["loop_prs_merged"], "0-test")
        self.assertEqual(out["issues_closed"], "0-test")
        self.assertEqual(out["generated"], "1970-01-01-test")
        self.assertEqual(out["loop_share"], "0-test")
        self.assertEqual(out["stars"], "0-test")
        self.assertEqual(out["last_activity"], "0-test")

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_partial_endpoint_failure_does_not_abort_live_refresh(self):
        # One API endpoint fails while the rest succeed (and the snapshot
        # is unreachable): every other stat must still update from live.
        out = self.run_scenario("live-partial")
        self.assertEqual(out["version"], "v9.9.9-test")
        self.assertEqual(out["prs_merged"], "4,242")
        self.assertEqual(out["loop_prs_merged"], "4,242")
        self.assertEqual(out["loop_share"], "100%")
        self.assertEqual(out["issues_closed"], "4,242")
        self.assertEqual(out["prs_open"], "7")
        self.assertEqual(out["issues_open"], "43")
        self.assertEqual(out["stars"], "11")
        self.assertEqual(out["last_activity"], "5m ago")
        # The failed endpoint keeps its hardcoded value; nothing else
        # fills it in when the snapshot is unreachable too.
        self.assertEqual(out["commits_year"], "0-test")

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

    def test_freshness_note_present(self):
        """The proof strip's freshness note must exist in the HTML and
        be written by the refresh script, so a stat the page could not
        refresh never reads as current."""
        html = (SITE / "index.html").read_text()
        self.assertIn("stats-freshness", html)
        self.assertIn("stats-freshness", extract_refresh_script())

    def _note_today(self):
        today = datetime.now(timezone.utc).date()
        return {"Live from GitHub · refreshed " + today.isoformat(),
                "Live from GitHub · refreshed " + (today - timedelta(days=1)).isoformat()}

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_freshness_note_live(self):
        out = self.run_scenario("live-ok")
        self.assertIn(out["__note"], self._note_today())
        self.assertFalse(out["__stale"])

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_freshness_note_snapshot(self):
        out = self.run_scenario("live-fail-snapshot-ok")
        self.assertEqual(out["__note"], "Snapshot 2026-01-02 · live refresh unavailable")
        self.assertFalse(out["__stale"])

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_freshness_note_mixed(self):
        # Vex's repro, sharpened: every search endpoint answers except
        # the loop-author query, so the strip mixes live and snapshot
        # counts. The old any-endpoint-live rule called that "Live" and
        # stamped today; both assertions fail against it.
        out = self.run_scenario("live-mixed")
        self.assertEqual(out["prs_merged"], "4,242")
        self.assertEqual(out["loop_prs_merged"], "7")
        self.assertEqual(out["loop_share"], "0%")
        self.assertEqual(
            out["__note"],
            "Mixed sources: some counts are live from GitHub, others are from the 2026-01-02 snapshot")
        self.assertFalse(out["__stale"])
        self.assertEqual(out["generated"], "2026-01-02")

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_freshness_note_partial_live_is_mixed(self):
        # Shadow-filed #1756's evidence: commits_year stays hardcoded
        # while the rest is live, so the page-wide claim is mixed, not
        # live, and the stamp must not claim today either.
        out = self.run_scenario("live-partial")
        self.assertEqual(out["commits_year"], "0-test")
        self.assertEqual(
            out["__note"],
            "Mixed sources: some counts are live from GitHub, others are from last checked-in values")
        self.assertFalse(out["__stale"])
        self.assertEqual(out["generated"], "1970-01-01-test")

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_freshness_note_total_failure_dims(self):
        out = self.run_scenario("all-fail")
        self.assertEqual(out["__note"], "Statistics unavailable — showing last checked-in values")
        self.assertTrue(out["__stale"])


if __name__ == "__main__":
    unittest.main()
