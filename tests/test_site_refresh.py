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
    attribute, wherever that attribute sits among the element's others.

    It keeps a stack of open elements and closes them the way a browser's
    tree builder does in the cases a page can rely on: a void element never
    opens, `<x/>` on a non-void element still opens it, an end tag closes
    the nearest matching open element in scope (and everything opened
    inside it), an end tag with no match closes nothing, HTML's optional
    end tags (`li`, `dd`/`dt`, `p`, `option`, table rows and cells,
    headings) are implied by the start tags that imply them, and whatever
    is still open when the document ends is closed there. Not modelled:
    the adoption agency algorithm (misnested formatting tags such as
    `<b><i></b></i>`) and foster parenting of text misplaced in a table."""

    # Elements with no end tag; they never open an element.
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "source", "track", "wbr"}
    # The spec's "has an element in scope" boundaries.
    SCOPE = {"applet", "caption", "html", "table", "td", "th", "marquee",
             "object", "template"}
    # The spec's "special" elements: an li/dd/dt start tag stops looking
    # for an open li/dd/dt to close at any of these but address, div, p.
    SPECIAL = {"address", "applet", "area", "article", "aside", "base",
               "basefont", "bgsound", "blockquote", "body", "br", "button",
               "caption", "center", "col", "colgroup", "dd", "details",
               "dir", "div", "dl", "dt", "embed", "fieldset", "figcaption",
               "figure", "footer", "form", "frame", "frameset", "h1", "h2",
               "h3", "h4", "h5", "h6", "head", "header", "hgroup", "hr",
               "html", "iframe", "img", "input", "keygen", "li", "link",
               "listing", "main", "marquee", "menu", "meta", "nav",
               "noembed", "noframes", "noscript", "object", "ol", "p",
               "param", "plaintext", "pre", "script", "search", "section",
               "select", "source", "style", "summary", "table", "tbody",
               "td", "template", "textarea", "tfoot", "th", "thead",
               "title", "tr", "track", "ul", "wbr", "xmp"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    # Start tags that close an open <p> (in button scope).
    CLOSES_P = {"address", "article", "aside", "blockquote", "center",
                "details", "dialog", "dir", "div", "dl", "fieldset",
                "figcaption", "figure", "footer", "form", "header",
                "hgroup", "hr", "li", "dd", "dt", "listing", "main", "menu",
                "nav", "ol", "p", "pre", "search", "section", "summary",
                "table", "ul", "xmp", "plaintext"} | HEADINGS
    # Table start tags: the open elements each one closes, and the
    # elements at which the search for one stops. The outermost match
    # before the stop is closed, which closes everything inside it too:
    # a new row closes the open row (and its open cell), and a new
    # section closes the open section, or the open row if there is none.
    # Cells stay in the row and section sets for a cell with no open row
    # around it (`<table><td>1<tr>`): a browser inserts the missing row,
    # and the new row or section closes that row together with the cell.
    TABLE_CLOSES = {
        "td": ({"td", "th"}, {"tr", "table", "template", "html"}),
        "th": ({"td", "th"}, {"tr", "table", "template", "html"}),
        "tr": ({"tr", "td", "th"}, {"tbody", "thead", "tfoot", "table",
                                    "template", "html"}),
        "tbody": ({"tbody", "thead", "tfoot", "tr", "td", "th"},
                  {"table", "template", "html"}),
        "thead": ({"tbody", "thead", "tfoot", "tr", "td", "th"},
                  {"table", "template", "html"}),
        "tfoot": ({"tbody", "thead", "tfoot", "tr", "td", "th"},
                  {"table", "template", "html"}),
    }
    # End tags that close nothing here: content can follow them, and the
    # end of the document closes whatever is left.
    NO_CLOSE = {"body", "html"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pairs = []
        self._stack = []  # [tag, data-site key or None, text parts]

    def _pop_to(self, index):
        """Close the element at `index` and everything opened inside it,
        recording each data-site element's collected text."""
        while len(self._stack) > index:
            _, key, parts = self._stack.pop()
            if key is not None:
                self.pairs.append((key, "".join(parts)))

    def _find(self, wanted, stop, outermost=False):
        """Index of the innermost open element in `wanted`, searching
        outwards and giving up at an element in `stop`; None if absent.
        With `outermost`, the search runs on to the stop (or the bottom of
        the stack) and returns the outermost match it passed instead."""
        found = None
        for index in range(len(self._stack) - 1, -1, -1):
            tag = self._stack[index][0]
            if tag in wanted:
                found = index
                if not outermost:
                    return found
            elif tag in stop:
                return found
        return found

    def _close_found(self, wanted, stop, outermost=False):
        """Close the innermost (or, with `outermost`, the outermost) open
        element in `wanted` (see `_find`), and everything inside it."""
        index = self._find(wanted, stop, outermost)
        if index is not None:
            self._pop_to(index)

    def _close_implied(self, tag):
        """Close the elements whose end tag HTML lets this start tag imply."""
        if tag in self.CLOSES_P:
            self._close_found({"p"}, self.SCOPE | {"button"})
        if tag in ("li", "dd", "dt"):
            wanted = {"li"} if tag == "li" else {"dd", "dt"}
            self._close_found(wanted, self.SPECIAL - {"address", "div", "p"} - wanted)
        elif tag in self.HEADINGS:
            if self._stack and self._stack[-1][0] in self.HEADINGS:
                self._pop_to(len(self._stack) - 1)
        elif tag in ("option", "optgroup"):
            closes = {"option"} if tag == "option" else {"option", "optgroup"}
            while self._stack and self._stack[-1][0] in closes:
                self._pop_to(len(self._stack) - 1)
        elif tag in self.TABLE_CLOSES:
            self._close_found(*self.TABLE_CLOSES[tag], outermost=True)

    def handle_starttag(self, tag, attrs):
        key = dict(attrs).get("data-site")
        self._close_implied(tag)
        if tag in self.VOID:
            # A void element has no content, so it shows no fallback
            # text (None); its key still counts, as the page's selector
            # finds it and the snapshot must cover it.
            if key is not None:
                self.pairs.append((key, None))
            return
        self._stack.append([tag, key, []])

    def handle_startendtag(self, tag, attrs):
        """Treat `<x/>` as a start tag only. `<br/>` is one void tag, and
        HTML ignores the slash on a non-void `<span/>` (it still opens an
        element), so the default start-plus-end would close it at once."""
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        """Close the nearest open `tag` in scope. A void end tag (`</br>`)
        or one with no open match finds nothing and closes nothing."""
        if tag in self.NO_CLOSE:
            return
        self._close_found({tag}, self.SCOPE - {tag})

    def handle_data(self, data):
        for _, key, parts in self._stack:
            if key is not None:
                parts.append(data)

    def close(self):
        """The end of the document closes every element still open."""
        super().close()
        self._pop_to(0)


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
        anchor = f'data-site="stars">{data["stars"]:,}</div>'
        self.assertIn(anchor, html)
        mutated = html.replace(anchor, anchor + '<input data-site="ghost" title="x">', 1)
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

    def test_fallback_check_sees_elements_with_implied_end_tags(self):
        """HTML lets `</li>` (and `</p>`, `</td>`, `</dd>`, `</option>`)
        be omitted. A data-site element whose end tag is implied must
        still be found by both guards, with only its own text."""
        html = (SITE / "index.html").read_text()
        data = json.loads((SITE / "data.json").read_text())
        lists = ('<ul><li data-site="ghost" title="x">x<li>y<li>z<li>w</ul>'
                 '<ul><li data-site="stars" title="x">999<li>y<li>z<li>w</ul>')
        self.assertIn("</body>", html)
        mutated = html.replace("</body>", lists + "</body>", 1)
        found = hardcoded_fallbacks(mutated)
        self.assertIn(("ghost", "x"), found)
        self.assertIn(("stars", "999"), found)
        self.assertEqual(missing_snapshot_keys(mutated, data), {"ghost"})
        stale = stale_fallbacks(mutated, data)
        self.assertTrue(any(line.startswith("stars: html '999'") for line in stale), stale)
        for snippet, want in [
            ('<p data-site="k">1<p>2', ("k", "1")),
            ('<p data-site="k">1<div>2</div>', ("k", "1")),
            ('<table><tr><td data-site="k">1<td>2</table>', ("k", "1")),
            ('<table><tr><td data-site="k">1<tr><td>2</table>', ("k", "1")),
            ('<dl><dt>a<dd data-site="k">1<dt>b<dd>2</dl>', ("k", "1")),
            ('<select><option data-site="k">1<option>2</select>', ("k", "1")),
            ('<ul><li data-site="k">1<ul><li>2</ul>3<li>4</ul>', ("k", "123")),
            ('<div data-site="k">1', ("k", "1")),  # closed by end of document
        ]:
            with self.subTest(snippet=snippet):
                self.assertEqual(hardcoded_fallbacks(snippet), [want])

    def test_fallback_check_closes_row_and_section_with_implied_end_tags(self):
        """A `<tr>` or `<tbody>` start tag that arrives while a cell is open
        closes the open row (or section), not only the cell, as a browser
        does, so a data-site row or section holds only its own text (#2378)."""
        for snippet, want in [
            ('<table><tr data-site="k"><td>1<tr><td>2</table>', ("k", "1")),
            ('<table><tr data-site="k"><th>1<tr><td>2</table>', ("k", "1")),
            ('<table><tr data-site="k"><td>1<tbody><tr><td>2</table>', ("k", "1")),
            ('<table><tbody data-site="k"><tr><td>1<tbody><tr><td>2</table>', ("k", "1")),
            ('<table><thead data-site="k"><tr><td>1<tfoot><tr><td>2</table>', ("k", "1")),
            ('<table><tr data-site="k"><td>1</td><tr><td>2</table>', ("k", "1")),
            # A row inside a nested table closes only the nested row.
            ('<table><tr data-site="k"><td>1<table><tr><td>2<tr><td>3</table>4</table>',
             ("k", "1234")),
        ]:
            with self.subTest(snippet=snippet):
                self.assertEqual(hardcoded_fallbacks(snippet), [want])
        html = (SITE / "index.html").read_text()
        data = json.loads((SITE / "data.json").read_text())
        stars = f'{data["stars"]:,}'
        self.assertIn("</body>", html)
        # The row's own text is 999; the next row's text would make it
        # 999<stars>, and the section's would read as the snapshot value.
        for table in (f'<table><tr data-site="stars"><td>999<tr><td>{stars}</table>',
                      f'<table><tbody data-site="stars"><tr><td>999<tbody><tr><td>{stars}</table>'):
            with self.subTest(table=table):
                mutated = html.replace("</body>", table + "</body>", 1)
                stale = stale_fallbacks(mutated, data)
                self.assertIn(f"stars: html '999' != data.json {stars!r}", stale)

    def test_fallback_check_closes_cell_of_implied_row(self):
        """A cell with no open row around it sits in a row the browser
        inserts; a later `<tr>` or section start tag closes that row and
        the cell with it, so the cell holds only its own text."""
        for snippet in ('<table><td data-site="k">1<tr><td>2</table>',
                        '<table><th data-site="k">1<tr><td>2</table>',
                        '<table><td data-site="k">1<tbody><tr><td>2</table>',
                        '<table><td data-site="k">1<thead><tr><td>2</table>',
                        '<table><td data-site="k">1<tfoot><tr><td>2</table>'):
            with self.subTest(snippet=snippet):
                self.assertEqual(hardcoded_fallbacks(snippet), [("k", "1")])
        html = (SITE / "index.html").read_text()
        data = json.loads((SITE / "data.json").read_text())
        stars = f'{data["stars"]:,}'
        self.assertIn("</body>", html)
        # The cell's own text is 999; a cell that kept collecting would
        # read 999 plus the next row's snapshot value instead.
        for after in ("<tr>", "<tbody><tr>"):
            table = f'<table><td data-site="stars">999{after}<td>{stars}</table>'
            with self.subTest(table=table):
                mutated = html.replace("</body>", table + "</body>", 1)
                self.assertIn(f"stars: html '999' != data.json {stars!r}",
                              stale_fallbacks(mutated, data))

    def test_fallback_check_keeps_self_closed_non_void_open(self):
        """HTML ignores the slash on a non-void `<span/>`: the span stays
        open and the text after it is its content, so it must be checked."""
        self.assertEqual(hardcoded_fallbacks('<span data-site="stars"/>999</span>'),
                         [("stars", "999")])
        self.assertEqual(hardcoded_fallbacks('<div data-site="stars">11<span/>999</div>'),
                         [("stars", "11999")])

    def test_fallback_check_ignores_unmatched_end_tags(self):
        """A void end tag (`</br>`, `</img>`) or any end tag with no open
        element to match closes nothing, so it can't hide later text."""
        for stray in ("</br>", "</img>", "</input>", "</div>", "</p>"):
            with self.subTest(stray=stray):
                self.assertEqual(
                    hardcoded_fallbacks(f'<span data-site="stars">11{stray}999</span>'),
                    [("stars", "11999")])

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
