"""The site's "What the second opinion caught" section (#2253): docs/site/publish-stats.py and
docs/site/stats.js.

The publisher is fed the real ``hermes dk stats --public --json`` export, built from
test_stats_public's fixture ledger, so a change to the export the page cannot read fails here.
The page script runs under node against a small DOM stand-in.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_stats_public as tsp  # noqa: E402
from diaktoros import stats_public  # noqa: E402

SITE = Path(__file__).resolve().parents[1] / "docs" / "site"
spec = importlib.util.spec_from_file_location("publish_stats", SITE / "publish-stats.py")
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db = tsp.ledger(self.tmp / "ledger.db")
        self.raw = json.loads(json.dumps(stats_public.collect(
            {"repo": tsp.REPO}, db, 0, tsp.T + 1000, with_models=True, listing=tsp.listing)))
        self.out = self.tmp / "stats.json"


class Publish(Fixture):
    def test_the_real_export_publishes_and_drops_models_unless_asked(self):
        path, dropped = pub.publish(self.raw, out=self.out)
        data = json.loads(path.read_text())
        self.assertTrue(dropped)
        self.assertNotIn("models", data)
        self.assertEqual(data["stopped"], {"count": 1, "prs": [7]})
        self.assertEqual(data["overruled"], {"count": 1})
        _, dropped = pub.publish(self.raw, keep_models=True, out=self.out)
        self.assertFalse(dropped)
        self.assertEqual({row["model"] for row in json.loads(self.out.read_text())["models"]},
                         {"terra", "glm"})

    def test_an_unknown_schema_or_a_credential_refuses_with_nothing_written(self):
        for bad in ({**self.raw, "schema": 99},
                    {**self.raw, "extra": "token " + tsp.SECRET},          # even a dropped key
                    {**self.raw, "models": [{**self.raw["models"][0], "model": tsp.SECRET}]}):
            with self.assertRaises(pub.Refused):
                pub.publish(bad, keep_models=True, out=self.out)
            self.assertFalse(self.out.exists())

    def test_a_value_of_the_wrong_type_refuses_cleanly(self):
        for bad in ({**self.raw, "stopped": {"count": "10", "prs": []}},
                    {**self.raw, "stopped": {"count": True, "prs": []}},
                    {**self.raw, "repo": "https://evil.test/x"},
                    {**self.raw, "window": {**self.raw["window"], "first": "yesterday"}}):
            with self.assertRaises(pub.Refused):
                pub.publish(bad, out=self.out)
        self.assertFalse(self.out.exists())
        cycle = self.tmp / "in.json"
        cycle.write_text(json.dumps({**self.raw, "overruled": []}))
        proc = subprocess.run([sys.executable, str(SITE / "publish-stats.py"), str(cycle)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("refused: not a public stats export", proc.stderr)

    def test_unknown_keys_and_local_paths_never_reach_the_page(self):
        raw = {**self.raw, "note": "see /home/jeremy/x"}
        raw["models"] = [{**raw["models"][0], "model": "terra /home/jeremy/m", "login": "drey"}]
        pub.publish(raw, keep_models=True, out=self.out)
        text = self.out.read_text()
        for leak in ("note", "/home/", "login", "drey"):
            self.assertNotIn(leak, text)
        self.assertIn("terra [path]", text)


HARNESS = r"""
const fs = require('fs');
const [script, statsPath] = process.argv.slice(2);
function node(tag) {
  const n = { tag, children: [], hidden: false, className: '', _text: '',
    appendChild(c) { this.children.push(c); return c; },
    querySelector(sel) { return find(this, (c) => c.tag === sel); },
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); },
    set textContent(v) { this._text = String(v); this.children = []; } };
  return n;
}
function find(n, test) {
  for (const c of n.children) { if (test(c)) return c; const d = find(c, test); if (d) return d; }
  return null;
}
const ids = {};
for (const id of ['caught', 'caught-stopped', 'caught-overruled', 'caught-prs', 'caught-facts',
                  'caught-models', 'caught-window']) ids[id] = node('div');
ids['caught'].hidden = true;
ids['caught-models'].hidden = true;
ids['caught-models'].appendChild(node('tbody'));
global.document = { getElementById: (id) => ids[id] || null, createElement: node,
  createTextNode: (t) => { const n = node('#text'); n._text = String(t); return n; } };
global.window = global;
global.fetch = (url) => {
  if (url === 'stats.json' && statsPath)
    return Promise.resolve({ ok: true, json: () => Promise.resolve(
      JSON.parse(fs.readFileSync(statsPath, 'utf8'))) });
  return Promise.resolve({ ok: false, status: 404 });
};
eval(fs.readFileSync(script, 'utf8'));
Promise.resolve(window.DIAKTOROS_STATS).then(() => {
  const links = [];
  (function walk(n) { if (n.href) links.push(n.href); n.children.forEach(walk); })(ids['caught-prs']);
  const text = {};
  for (const id in ids) text[id] = ids[id].textContent;
  console.log(JSON.stringify({ hidden: ids['caught'].hidden,
    models: !ids['caught-models'].hidden, text, links }));
});
"""


@unittest.skipUnless(shutil.which("node"), "node unavailable")
class Page(Fixture):
    def page(self, data: dict | None) -> dict:
        (self.tmp / "s.js").write_text(HARNESS)
        args = ["node", str(self.tmp / "s.js"), str(SITE / "stats.js")]
        if data is not None:
            (self.tmp / "d.json").write_text(json.dumps(data))
            args.append(str(self.tmp / "d.json"))
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_no_stats_json_keeps_the_section_hidden(self):
        self.assertTrue(self.page(None)["hidden"])

    def test_published_stats_show_the_counts_and_link_each_stopped_pr(self):
        pub.publish(self.raw, out=self.out)
        shown = self.page(json.loads(self.out.read_text()))
        self.assertFalse(shown["hidden"])
        self.assertFalse(shown["models"])                      # no models section, no table
        self.assertEqual(shown["text"]["caught-stopped"], "1")
        self.assertEqual(shown["text"]["caught-overruled"], "1")
        self.assertEqual(shown["links"], [f"https://github.com/{tsp.REPO}/pull/7"])
        self.assertIn("Issues filed by the reviewer: 1, 1 closed as completed",
                      shown["text"]["caught-facts"])
        self.assertIn("2026-09-21 to 2026-09-21: 3 PRs, 9 turns", shown["text"]["caught-window"])

    def test_the_model_table_renders_only_when_present(self):
        pub.publish(self.raw, keep_models=True, out=self.out)
        shown = self.page(json.loads(self.out.read_text()))
        self.assertTrue(shown["models"])
        self.assertIn("terra", shown["text"]["caught-models"])
        self.assertIn("2 of 2 approvals", shown["text"]["caught-models"])

    def test_the_page_never_parses_data_as_html_and_keeps_the_wording(self):
        script = (SITE / "stats.js").read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, script)
        page = (SITE / "index.html").read_text()
        self.assertIn("Approved by the reviewer, stopped before merge", page)
        self.assertNotIn("would have merged", page)
        self.assertIn('id="caught" aria-labelledby="caught-title" hidden', page)


if __name__ == "__main__":
    unittest.main()
