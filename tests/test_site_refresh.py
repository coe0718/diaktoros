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
from html.parser import HTMLParser

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
    if (scenario === 'live-ok' || scenario === 'live-partial' || scenario === 'live-mixed' || scenario === 'live-hang') {
      const u = String(url);
      if (u.includes('/stats/commit_activity')) {
        // In the partial scenario this one endpoint fails while the rest
        // succeed, proving one rejection can't abort the live refresh.
        if (scenario === 'live-partial')
          return Promise.resolve({ ok: false, status: 403 });
        // In the hang scenario the body never finishes: headers arrive,
        // json() never resolves. The whole-operation deadline must still
        // let fallback and the note run.
        if (scenario === 'live-hang')
          return Promise.resolve({ ok: true, json: () => new Promise(() => {}) });
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
    // Same snapshot with no generated key: the undated state the
    // builder now creates when the activity fetch never succeeded.
    // The page must read it as unknown, never as the baked-in date.
    if (scenario === 'snapshot-undated') {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(
        { version: 'v1.2.3-test', loop_prs_merged: 7, prs_merged: 21,
          issues_closed: 8, prs_open: 3, open_issues_count: 11,
          stars: 5, forks: 2, commits_year: 345,
          pushed_at: new Date(Date.now() - 90 * 60000).toISOString() }) });
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
  // The hang scenario's deadline is 10s; every other scenario settles
  // immediately, so only it waits.
}, scenario === 'live-hang' ? 11000 : 200);
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


class _FallbackParser(HTMLParser):
    """Collect (key, text) for every element carrying a data-site
    attribute, wherever that attribute sits among the element's others."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pairs = []
        self._open = []  # [key, depth, text parts] per open data-site element

    # Elements with no end tag; they must not deepen an open frame.
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "source", "track", "wbr"}

    def handle_starttag(self, tag, attrs):
        key = dict(attrs).get("data-site")
        if tag in self.VOID:
            # A void element has no content, so it shows no fallback
            # text (None); its key still counts, as the page's selector
            # finds it and the snapshot must cover it.
            if key is not None:
                self.pairs.append((key, None))
            return
        for frame in self._open:
            frame[1] += 1
        if key is not None:
            self._open.append([key, 1, []])

    def handle_startendtag(self, tag, attrs):
        """Treat `<x/>` as a start tag only. `<br/>` is one void tag, and
        HTML ignores the slash on a non-void `<span/>` (it still opens an
        element), so the default start-plus-end would close frames early."""
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return  # a stray `</br>` closes nothing
        for frame in self._open:
            frame[1] -= 1
        while self._open and self._open[-1][1] <= 0:
            key, _, parts = self._open.pop()
            self.pairs.append((key, "".join(parts)))

    def handle_data(self, data):
        for frame in self._open:
            frame[2].append(data)


def hardcoded_fallbacks(html):
    """Return [(data-site key, hardcoded text)] for every data-site
    element in the page, parsed as HTML so attribute order doesn't matter."""
    parser = _FallbackParser()
    parser.feed(html)
    parser.close()
    return parser.pairs


def missing_snapshot_keys(html, data):
    """Return the set of data-site keys the page uses (on any element,
    void ones included) that data.json does not provide."""
    return {key for key, _ in hardcoded_fallbacks(html)} - set(data)


def stale_fallbacks(html, data):
    """List every hardcoded data-site value that differs from data.json,
    formatted as the page's num() would show it."""
    stale = []
    for key, shown in hardcoded_fallbacks(html):
        if shown is None:
            continue  # a void element shows no text to go stale
        want = data.get(key)
        if isinstance(want, int) and not isinstance(want, bool):
            want = f"{want:,}"
        if shown != want:
            stale.append(f"{key}: html {shown!r} != data.json {want!r}")
    return stale


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
        html = (SITE / "index.html").read_text()
        keys = {key for key, _ in hardcoded_fallbacks(html)}
        self.assertTrue(keys, "no data-site keys found in index.html")
        data = json.loads((SITE / "data.json").read_text())
        missing = missing_snapshot_keys(html, data)
        self.assertFalse(missing, f"data.json missing keys used by the page: {missing}")

    def test_hardcoded_fallbacks_match_snapshot(self):
        """Every hardcoded data-site value in the HTML must equal the
        checked-in data.json value, formatted as the page's num() would
        show it, so a release stamp that regenerates data.json can't
        leave the last-resort tier a release behind (#2345)."""
        html = (SITE / "index.html").read_text()
        data = json.loads((SITE / "data.json").read_text())
        self.assertTrue(hardcoded_fallbacks(html), "no hardcoded data-site values found in index.html")
        stale = stale_fallbacks(html, data)
        self.assertFalse(stale, "hardcoded fallbacks disagree with data.json:\n" + "\n".join(stale))

    def test_fallback_check_sees_attributes_after_data_site(self):
        """An element whose data-site is followed by another attribute must
        still be checked, so a stale value there fails the check (#2349)."""
        html = (SITE / "index.html").read_text()
        data = json.loads((SITE / "data.json").read_text())
        original = f'data-site="stars">{data["stars"]:,}<'
        self.assertIn(original, html)
        mutated = html.replace(original, 'data-site="stars" title="stars">999<')
        self.assertIn(("stars", "999"), hardcoded_fallbacks(mutated))
        self.assertTrue(any(line.startswith("stars:") for line in stale_fallbacks(mutated, data)),
                        "stale stars value after a later attribute was not reported")

    def test_snapshot_key_check_sees_void_elements(self):
        """A data-site key on a void element (an input) is still found by
        the page's selector, so the key coverage check must see it."""
        html = (SITE / "index.html").read_text()
        data = json.loads((SITE / "data.json").read_text())
        anchor = f'data-site="stars">{data["stars"]:,}<'
        self.assertIn(anchor, html)
        mutated = html.replace(anchor, anchor + '/span><input data-site="ghost" title="x"><span>', 1)
        self.assertIn(("ghost", None), hardcoded_fallbacks(mutated))
        self.assertEqual(missing_snapshot_keys(mutated, data), {"ghost"})
        self.assertEqual(stale_fallbacks(mutated, data), stale_fallbacks(html, data))

    def test_fallback_check_reads_past_self_closing_void_tag(self):
        """A self-closing `<br/>` inside a data-site element must not end
        that element early and hide the text after it."""
        self.assertEqual(hardcoded_fallbacks('<span data-site="stars">11<br/>999</span>'),
                         [("stars", "11999")])
        html = (SITE / "index.html").read_text()
        data = json.loads((SITE / "data.json").read_text())
        original = f'data-site="stars">{data["stars"]:,}<'
        mutated = html.replace(original, f'data-site="stars" title="stars">{data["stars"]:,}<br/>999<')
        self.assertTrue(any(line.startswith("stars:") for line in stale_fallbacks(mutated, data)),
                        "text after a self-closing <br/> was not checked")

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
    def test_freshness_note_snapshot_undated(self):
        # Vex round 4 + shadow #1775: a snapshot with no generated key
        # must not render a blank date in the note, and the "Page
        # updated" stamp must not keep the date baked into the HTML.
        # Against the eb6a9ef page the note reads "Snapshot  · live
        # refresh unavailable" and the stamp stays hardcoded.
        out = self.run_scenario("snapshot-undated")
        self.assertEqual(out["prs_merged"], "21")
        self.assertEqual(out["commits_year"], "345")
        self.assertEqual(out["__note"], "Showing last checked-in values · live refresh unavailable")
        self.assertEqual(out["generated"], "unknown")
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

    @unittest.skipUnless(shutil.which("node"), "node unavailable")
    def test_freshness_note_hang_falls_back(self):
        # Vex round 2: a stalled body must not hang the page past
        # fallback. The commit-activity body never resolves; after the
        # 10s deadline the rest of the page must still be live, the
        # stuck figure hardcoded, and the note mixed — not blank.
        # Against the old header-cleared deadline the note never runs.
        out = self.run_scenario("live-hang")
        self.assertEqual(out["prs_merged"], "4,242")
        self.assertEqual(out["commits_year"], "0-test")
        self.assertEqual(
            out["__note"],
            "Mixed sources: some counts are live from GitHub, others are from last checked-in values")
        self.assertFalse(out["__stale"])
        self.assertEqual(out["generated"], "1970-01-01-test")


if __name__ == "__main__":
    unittest.main()
