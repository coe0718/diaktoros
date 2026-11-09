"""The site's run viewer (#1749): docs/site/publish-history.py and docs/site/history.js.

The publisher is fed the real ``hermes dk history --format json`` output, built from
test_history's fixture ledger, so a change to the command's JSON that the viewer cannot read
fails here. The viewer runs under node against a small DOM stand-in.
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
import test_fixer_gating as fg  # noqa: E402
import test_history as th  # noqa: E402

SITE = Path(__file__).resolve().parents[1] / "docs" / "site"
spec = importlib.util.spec_from_file_location("publish_history", SITE / "publish-history.py")
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)
TAIL = "push said /home/jeremy/.hermes/state boom"


class Fixture(fg.Base):
    """test_history's fixture ledger and faked GitHub, without its tests."""
    run_row, insert, cycle, github, collect = (
        th.History.run_row, th.History.insert, th.History.cycle, th.History.github,
        th.History.collect)

    def setUp(self):
        super().setUp()
        self.db = th.run_supervisor.production_ledger()
        self.sup = th.run_supervisor.Supervisor(self.db)
        self.reads = []

    def out(self) -> Path:
        path = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def exported(self) -> dict:
        self.cycle()
        self.run_row("fixer", th.B, th.T + 200, state="failed", detail=TAIL)
        return json.loads(json.dumps(self.collect()))      # what --format json prints


class Publish(Fixture):
    def test_the_command_s_real_export_publishes_without_host_only_text(self):
        out = self.out()
        path = pub.publish(self.exported(), out)
        data = json.loads(path.read_text())
        index = json.loads((out / "index.json").read_text())
        self.assertEqual(index["cycles"], [{"pr": 7, "repo": fg.REPO, "state": "merged",
                                            "merged_at": th.T + 600, "rounds": 2,
                                            "findings": 2, "rulings": 1}])
        events = [e for r in data["rounds"] for e in r["events"]]
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("boom", text)                     # a failed turn's tail
        self.assertNotIn("F1 agreed", text)                # the shadow's own text
        self.assertNotIn("/home/", text)
        self.assertNotIn("example.test", text)             # links off GitHub are dropped
        self.assertNotIn("run", events[0])                 # ledger ids stay on the host
        failed = next(e for e in events if e["text"].startswith("turn failed"))
        shadow = next(e for e in events if e["kind"] == "shadow")
        self.assertTrue(failed["withheld"] and shadow["withheld"])
        self.assertIn("answers (posted): F1 fixed — loop bounded", text)   # posted on GitHub
        self.assertIn(f"https://github.com/{fg.REPO}/pull/7#issuecomment-55", data["markdown"])
        self.assertIn("## Round 2 @", data["markdown"])
        self.assertEqual([f["outcome"] for f in data["findings"]], ["fixed", "overruled"])

    def test_republishing_a_pr_replaces_its_index_line(self):
        out = self.out()
        export = self.exported()
        pub.publish(export, out)
        pub.publish(export, out)
        self.assertEqual(len(json.loads((out / "index.json").read_text())["cycles"]), 1)

    def test_an_unknown_schema_or_a_credential_refuses_with_nothing_written(self):
        out = self.out()
        export = self.exported()
        for bad in ({**export, "schema": 2},
                    {**export, "gaps": ["key ghp_" + "Q" * 30 + " leaked"]}, ["not", "a", "dict"]):
            with self.assertRaises(pub.Refused):
                pub.publish(bad, out)
        self.assertEqual(list(out.iterdir()), [])

    def test_a_local_path_in_public_text_is_replaced(self):
        export = self.exported()
        export["rounds"][0]["events"][0]["text"] = "see ~/.hermes/loops/x.json and C:\\Users\\j"
        export["gaps"] = ["broker-audit at /var/lib/hermes/a.jsonl is unreadable"]
        data = pub.sanitize(export)
        self.assertEqual(data["rounds"][0]["events"][0]["text"], "see [path] and [path]")
        self.assertEqual(data["gaps"], ["broker-audit at [path] is unreadable"])


HARNESS = r"""
const fs = require('fs');
const [script, scenario, cyclePath] = process.argv.slice(2);
function node(tag) {
  const n = { tag, children: [], attrs: {}, hidden: false, className: '', _text: '',
    listeners: {},
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(k, f) { this.listeners[k] = f; },
    querySelector(sel) { return find(this, (c) => c.tag === sel); },
    remove() {}, click() {},
    get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); },
    set textContent(v) { this._text = String(v); this.children = []; } };
  return n;
}
function find(n, test) {
  for (const c of n.children) { if (test(c)) return c; const d = find(c, test); if (d) return d; }
  return null;
}
const ids = {};
for (const id of ['history-viewer', 'history-pick', 'history-meta', 'history-gaps',
                  'history-findings', 'history-rounds', 'history-md']) ids[id] = node('div');
ids['history-viewer'].hidden = true;
ids['history-findings'].appendChild(node('tbody'));
global.document = { getElementById: (id) => ids[id] || null, createElement: node,
  createTextNode: (t) => { const n = node('#text'); n._text = String(t); return n; },
  body: node('body') };
global.window = global;
const cycle = cyclePath ? JSON.parse(fs.readFileSync(cyclePath, 'utf8')) : null;
global.fetch = (url) => {
  if (url === 'history/index.json') return Promise.resolve(scenario === 'empty'
    ? { ok: true, json: () => Promise.resolve({ schema: 1, cycles: [] }) }
    : { ok: true, json: () => Promise.resolve({ schema: 1, cycles: [{ pr: cycle.pr,
        state: cycle.state, rounds: cycle.rounds.length, findings: 2, rulings: 1 }] }) });
  if (url === 'history/' + (cycle && cycle.pr) + '.json')
    return Promise.resolve({ ok: true, json: () => Promise.resolve(cycle) });
  return Promise.resolve({ ok: false, status: 404 });
};
eval(fs.readFileSync(script, 'utf8'));
Promise.resolve(window.DIAKTOROS_HISTORY).then(() => {
  const links = [];
  (function walk(n) { if (n.href) links.push(n.href); n.children.forEach(walk); })(ids['history-rounds']);
  console.log(JSON.stringify({ hidden: ids['history-viewer'].hidden,
    pick: ids['history-pick'].textContent, meta: ids['history-meta'].textContent,
    findings: ids['history-findings'].textContent, rounds: ids['history-rounds'].textContent,
    md: !ids['history-md'].hidden, links }));
});
"""


@unittest.skipUnless(shutil.which("node"), "node unavailable")
class Viewer(Fixture):
    def page(self, scenario: str, cycle: dict | None = None) -> dict:
        tmp = self.out()
        (tmp / "h.js").write_text(HARNESS)
        args = ["node", str(tmp / "h.js"), str(SITE / "history.js"), scenario]
        if cycle is not None:
            (tmp / "c.json").write_text(json.dumps(cycle))
            args.append(str(tmp / "c.json"))
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_no_published_cycle_keeps_the_viewer_hidden(self):
        self.assertTrue(self.page("empty")["hidden"])

    def test_a_published_cycle_shows_its_rounds_findings_and_github_evidence(self):
        data = json.loads(pub.publish(self.exported(), self.out()).read_text())
        data["rounds"][0]["events"][0]["link"] = "https://evil.test/x"   # never followed
        shown = self.page("one", data)
        self.assertFalse(shown["hidden"])
        self.assertIn("PR #7 · merged · 2 rounds", shown["pick"])
        self.assertIn("Merged 2026-09-21 14:23 UTC by coe0718", shown["meta"])
        self.assertIn("overruled by a ruling ACCEPT (breach:3) at round 2", shown["findings"])
        for want in ("Round 1 @ aaaaaaa", "Round 2 @ bbbbbbb", "ruling ACCEPT (breach:3)",
                     "not published", "fixer · drey"):
            self.assertIn(want, shown["rounds"])
        self.assertIn(f"https://github.com/{fg.REPO}/pull/7#issuecomment-55", shown["links"])
        self.assertTrue(all(link.startswith("https://github.com/") for link in shown["links"]))
        self.assertTrue(shown["md"])

    def test_the_viewer_never_parses_data_as_html(self):
        script = (SITE / "history.js").read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(sink, script)


if __name__ == "__main__":
    unittest.main()
