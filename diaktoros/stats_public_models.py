"""The opt-in reviewer-model comparison of ``hermes dk stats --public --models`` (#2253).

Per resolved reviewer model, over the window: its posted verdicts and approve share, how many of
its approvals a shadow review saw and how often the shadow asked for changes on one, and how the
arbiter ruled over those shadows (upheld: the shadow was right; overruled: the reviewer was).
Each row carries its sample size and its first and last dates, so a reader can tell a model with
four verdicts from one with four hundred. Counts and dates only, like the rest of the export.
"""
from __future__ import annotations

from collections import Counter

from . import stats_public as sp


def compare(rows: dict, since: float, until: float) -> list[dict]:
    """One row per reviewer model with a posted verdict in the window, busiest first."""
    latest = sp.approvals(rows)
    shadows: dict = {}
    for row in rows["shadows"]:
        verdict = sp._VERDICT.get(str(row["verdict"]), "other")
        shadows.setdefault((row["pr"], row["head"]), set()).add(verdict)
    per: dict[str, dict] = {}
    for row in rows["reviews"]:
        if not since <= row["created"] < until:
            continue
        name = str(row["model"] or "unknown")
        entry = per.setdefault(name, {"verdicts": Counter(), "stamps": [], "seen": set()})
        entry["verdicts"][sp._VERDICT.get(str(row["verdict"]), "other")] += 1
        entry["stamps"].append(row["created"])
        entry["seen"].add((row["pr"], row["head"]))
    rulings: dict[str, Counter] = {}
    for ruling, shadow in sp.over_shadow(rows, since, until):
        if shadow is None:
            continue
        verdict, model = latest.get((shadow["pr"], shadow["head"])) or ("", "unknown")
        if verdict != "approved":
            continue
        kind = {"REJECT": "upheld", "ACCEPT": "overruled"}.get(str(ruling["verdict"]), "other")
        rulings.setdefault(str(model), Counter())[kind] += 1
    out = []
    for name, entry in per.items():
        approved = [key for key in entry["seen"] if (latest.get(key) or ("",))[0] == "approved"]
        shadowed = [key for key in approved if key in shadows]
        disagreed = sum("changes" in shadows[key] for key in shadowed)
        total = sum(entry["verdicts"].values())
        ruled = rulings.get(name, Counter())
        out.append({"model": name, "verdicts": total,
                    "approved": entry["verdicts"]["approved"],
                    "changes": entry["verdicts"]["changes"],
                    "approve_share": round(entry["verdicts"]["approved"] / total, 3),
                    "shadowed_approvals": len(shadowed), "shadow_disagreed": disagreed,
                    "upheld": ruled["upheld"], "overruled": ruled["overruled"],
                    "first": sp.day(min(entry["stamps"])), "last": sp.day(max(entry["stamps"]))})
    return sorted(out, key=lambda row: (-row["verdicts"], row["model"]))
