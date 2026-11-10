// "What the second opinion caught" (#2253): the loop's own counts, from
// `hermes dk stats --public --json` published by docs/site/publish-stats.py. No stats.json, no
// section. Every value is set with textContent, never parsed as HTML, and only GitHub is linked.
(function () {
  "use strict";
  var root = document.getElementById("caught");
  if (!root || typeof fetch !== "function") return;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function set(id, text) {
    var node = document.getElementById(id);
    if (node) node.textContent = String(text);
    return node;
  }

  function hours(seconds) {
    if (typeof seconds !== "number") return "?";
    if (seconds < 3600) return Math.round(seconds / 60) + " min";
    if (seconds < 172800) return (Math.round(seconds / 360) / 10) + " h";
    return (Math.round(seconds / 8640) / 10) + " days";
  }

  function count(n) { return typeof n === "number" ? n.toLocaleString("en-US") : "?"; }

  function showPrs(data) {
    var box = document.getElementById("caught-prs");
    box.textContent = "";
    var prs = data.stopped.prs || [];
    if (!prs.length || !/^[\w.-]+\/[\w.-]+$/.test(data.repo)) return;
    box.appendChild(document.createTextNode("Stopped on: "));
    prs.forEach(function (pr, i) {
      if (i) box.appendChild(document.createTextNode(", "));
      var a = el("a", "", "#" + Number(pr));
      a.href = "https://github.com/" + data.repo + "/pull/" + Number(pr);
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      box.appendChild(a);
    });
  }

  function showFacts(data) {
    var list = document.getElementById("caught-facts");
    list.textContent = "";
    var f = data.findings || {};
    ["reviewer", "shadow", "arbiter"].forEach(function (who) {
      var row = f[who] || {};
      if (!row.filed) return;
      list.appendChild(el("li", "", "Issues filed by the " + who + ": " + count(row.filed) +
        (typeof row.completed === "number" ? ", " + count(row.completed) + " closed as completed" : "")));
    });
    var c = data.cycle || {};
    if (c.rounds) list.appendChild(el("li", "", "Review rounds per merged PR: median " +
      c.rounds.median + ", p75 " + c.rounds.p75 + " (" + count(c.rounds.n) + " PRs)"));
    if (c.open_to_merge) list.appendChild(el("li", "", "Open to merge: median " +
      hours(c.open_to_merge.median) + ", p75 " + hours(c.open_to_merge.p75)));
    if (c.change_proofs) list.appendChild(el("li", "", "Change proofs run: " + count(c.change_proofs)));
  }

  function showModels(data) {
    var table = document.getElementById("caught-models");
    var body = table.querySelector("tbody");
    body.textContent = "";
    var models = data.models || [];
    table.hidden = !models.length;
    models.forEach(function (m) {
      var row = el("tr");
      [m.model, count(m.verdicts), Math.round((m.approve_share || 0) * 100) + "%",
       count(m.shadow_disagreed) + " of " + count(m.shadowed_approvals) + " approvals",
       count(m.upheld) + " / " + count(m.overruled),
       (m.first || "?") + " to " + (m.last || "?")].forEach(function (cell) {
        row.appendChild(el("td", "", cell));
      });
      body.appendChild(row);
    });
  }

  window.DIAKTOROS_STATS = fetch("stats.json", { cache: "no-cache" }).then(function (r) {
    if (!r.ok) throw new Error("stats.json: " + r.status);
    return r.json();
  }).then(function (data) {
    if (!data || data.schema !== 1 || !data.stopped) return;
    set("caught-stopped", count(data.stopped.count));
    set("caught-overruled", count(data.overruled && data.overruled.count));
    showPrs(data);
    showFacts(data);
    showModels(data);
    var w = data.window || {};
    set("caught-window", "From the loop's run ledger, " + (w.first || "?") + " to " +
      (w.last || "?") + ": " + count(w.prs) + " PRs, " + count(w.turns) + " turns." +
      (data.partial ? " GitHub was only partly read, so some counts are a lower bound." : "") +
      (data.published ? " Published " + data.published + "." : ""));
    root.hidden = false;
  }).catch(function () { /* nothing published: the section stays hidden */ });
})();
