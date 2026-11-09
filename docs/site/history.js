// Run viewer (#1749): review cycles published from `hermes dk history --pr N --format json`
// (#1657) by docs/site/publish-history.py. It shows only what was published: no cycles, no
// viewer, and the hand-written case study stands alone. Every value from the data is set with
// textContent, never parsed as HTML, and only links to GitHub are followed.
(function () {
  "use strict";
  var root = document.getElementById("history-viewer");
  if (!root || typeof fetch !== "function") return;
  var pick = document.getElementById("history-pick");
  var meta = document.getElementById("history-meta");
  var gaps = document.getElementById("history-gaps");
  var findings = document.getElementById("history-findings");
  var rounds = document.getElementById("history-rounds");
  var download = document.getElementById("history-md");
  var current = null;
  var SEATS = { fixer: "seat-fixer", reviewer: "seat-reviewer", adjudicator: "seat-adjudicator",
                shadow: "seat-shadow" };
  // The words history_render._end uses, so the page and the Markdown export agree.
  var OUTCOMES = { fixed: "fixed", withdrawn: "withdrawn", overruled: "overruled by a ruling",
                   upheld: "upheld by a ruling", respecified: "sent back for respecification",
                   disputed: "disputed by the fixer, no ruling yet", open: "still open" };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function json(url) {
    return fetch(url, { cache: "no-cache" }).then(function (r) {
      if (!r.ok) throw new Error(url + ": " + r.status);
      return r.json();
    });
  }

  function clock(at) {
    if (!at) return "--:--";
    var iso = new Date(at * 1000).toISOString();
    return iso.slice(0, 10) + " " + iso.slice(11, 16) + " UTC";
  }

  // The renderer's link forms made whole; anything not on GitHub is not linked.
  function whole(data, link) {
    link = String(link || "");
    if (!/^[\w.-]+\/[\w.-]+$/.test(data.repo)) return "";
    var base = "https://github.com/" + data.repo;
    if (/^#issuecomment-\d+$/.test(link)) return base + "/pull/" + data.pr + link;
    if (/^issue:\d+$/.test(link)) return base + "/issues/" + link.slice(6);
    return link.indexOf("https://github.com/") === 0 ? link : "";
  }

  function anchor(href, text) {
    var a = el("a", "", text);
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    return a;
  }

  function outcome(item) {
    var end = OUTCOMES[item.outcome] || String(item.outcome || "unknown");
    if (item.ruling) end += " " + item.ruling;
    if (item.outcome !== "open")
      end += item.settled ? " at round " + item.settled : " (round not on record)";
    return end;
  }

  function showMeta(data) {
    var merged = data.merged;
    var state = merged ? "Merged " + clock(merged.at) + " by " + (merged.by || "unknown")
      : data.missing && data.missing.length ? "Merge state not read (published offline)"
      : "Not merged (" + (data.state || "state unknown") + ")";
    meta.textContent = "";
    meta.appendChild(anchor("https://github.com/" + data.repo + "/pull/" + data.pr,
                            "PR #" + data.pr));
    meta.appendChild(document.createTextNode(" · " + data.rounds.length + " rounds · " + state +
      ". A person merges; the loop never does. Published " + (data.published || "") + "."));
    gaps.textContent = "";
    (data.gaps || []).concat((data.missing || []).map(function (m) {
      return "not read: " + m;
    })).forEach(function (gap) { gaps.appendChild(el("li", "", "Gap on record: " + gap)); });
  }

  function showFindings(data) {
    var body = findings.querySelector("tbody");
    body.textContent = "";
    findings.hidden = !(data.findings || []).length;
    (data.findings || []).forEach(function (item) {
      var row = el("tr");
      [item.id, item.severity || "—", item.category || "—",
       item.raised ? "round " + item.raised : "—", outcome(item)].forEach(function (cell) {
        row.appendChild(el("td", "", cell));
      });
      body.appendChild(row);
    });
  }

  function showEvent(data, event) {
    var li = el("li");
    var when = el("div", "case-when");
    var who = el("strong", SEATS[event.who] || "", event.who + (event.login ? " · " + event.login : ""));
    when.appendChild(who);
    when.appendChild(document.createTextNode(clock(event.at)));
    li.appendChild(when);
    var body = el("div");
    body.appendChild(el("p", "", event.text));
    if (event.withheld) body.appendChild(el("p", "history-note", "Text seen only on the loop's host is not published."));
    if (event.gap) body.appendChild(el("p", "history-gap", "Gap: " + event.gap));
    var href = whole(data, event.link);
    if (href) {
      var p = el("p");
      p.appendChild(anchor(href, "Open the evidence on GitHub"));
      body.appendChild(p);
    }
    li.appendChild(body);
    return li;
  }

  function showRounds(data) {
    rounds.textContent = "";
    data.rounds.forEach(function (rnd) {
      var head = el("h3", "history-round", "Round " + rnd.round + " @ ");
      if (/^[0-9a-f]{7,40}$/.test(rnd.head))
        head.appendChild(anchor("https://github.com/" + data.repo + "/commit/" + rnd.head,
                                rnd.head.slice(0, 7)));
      else head.appendChild(document.createTextNode("no head on record"));
      rounds.appendChild(head);
      var list = el("ol", "case-timeline");
      rnd.events.forEach(function (event) { list.appendChild(showEvent(data, event)); });
      rounds.appendChild(list);
    });
  }

  function show(pr) {
    return json("history/" + Number(pr) + ".json").then(function (data) {
      current = data;
      showMeta(data);
      showFindings(data);
      showRounds(data);
      download.hidden = !data.markdown;
    }).catch(function () {
      meta.textContent = "This cycle could not be loaded.";
    });
  }

  download.addEventListener("click", function () {
    if (!current || !current.markdown || typeof Blob === "undefined") return;
    var url = URL.createObjectURL(new Blob([current.markdown], { type: "text/markdown" }));
    var a = anchor(url, "");
    a.download = "diaktoros-pr-" + current.pr + "-history.md";
    a.target = "";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  });

  pick.addEventListener("change", function () { show(pick.value); });

  window.DIAKTOROS_HISTORY = json("history/index.json").then(function (index) {
    var cycles = (index && index.cycles) || [];
    if (!cycles.length) return;
    cycles.forEach(function (c) {
      var label = "PR #" + c.pr + " · " + (c.state || "?") + " · " + c.rounds + " rounds · " +
        c.findings + " findings" + (c.rulings ? " · " + c.rulings + " ruling" + (c.rulings > 1 ? "s" : "") : "");
      var option = el("option", "", label);
      option.value = String(c.pr);
      pick.appendChild(option);
    });
    root.hidden = false;
    return show(cycles[0].pr);
  }).catch(function () { /* no published cycles: the viewer stays hidden */ });
})();
