"""Generated finding lines: what ``findings.check`` accepts, ``findings.apply`` records (#2307).

A seeded ``random.Random`` writes review bodies from the grammar ``findings.py`` parses (ids,
``file:line`` refs, category tags, states with and without reasons, ``missed earlier``, list and
bold decorations) over a few review rounds. Each line carries what it was written to mean, so
the oracle does not reuse the parser. Asserted:

1. a body ``check`` accepts, ``apply`` records exactly as written: every new finding open, every
   stated finding in its state, nothing dropped and nothing else touched; an accepted APPROVE
   leaves nothing open;
2. a body ``check`` refuses because an answered finding was restated without dealing with the
   answer (#703) can be followed by a generated body it accepts.

#550 (a state for an unknown id accepted as a new finding, then dropped by ``apply``) fails 1.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
from pathlib import Path
import random
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import finding_answers, findings  # noqa: E402

SEEDS = (1, 7, 550, 2307)
CASES = 120                       # review sequences per seed
ROUNDS = 4
CHANGED = ("src/a.py", "src/b.py")
FILES = CHANGED + ("src/c.py", "lib/data.py")
TAGS = findings.CATEGORIES + ("weird",)
TEXTS = ("leaks the handle", "no test for the parser", "open file handle leaks",
         "Fixed-size buffer overflows", "withdrawn flag is never cleared")
REASONS = ("", " — still no test", ": the guard is gone", " (moved to src/b.py:4)",
           " — the answer misses the retry path", " — src/a.py: leaks the handle", ". done")
NOISE = ("Summary: two problems.", "Not verified: nothing", "", "- note: F-strings are fine")
VERDICTS = ("REQUEST_CHANGES", "APPROVE", "COMMENT")
LOOP = {"repo": "acme/w", "id": "l", "base": "main"}
ANSWERED = "was answered after you last stated it"


def _id(rng, fid):
    return rng.choice((fid, "- " + fid, "* " + fid, f"**{fid}**", f"- **{fid}**"))


def new_line(rng, fid, later):
    tag = f" [{rng.choice(TAGS)}]" if rng.random() < 0.5 else ""
    ref = rng.choice(FILES) + (f":{rng.randint(1, 99)}" if rng.random() < 0.7 else "")
    missed = findings.MISSED + ": " if later and rng.random() < 0.3 else ""
    text = (f"{ref}: {rng.choice(TEXTS)}" if rng.random() < 0.6
            else f"{rng.choice(TEXTS)} in {ref}")
    return f"{_id(rng, fid)}{tag}: {missed}{text}", ("new", fid, missed != "")


def state_line(rng, fid):
    word = rng.choice(findings.STATES)
    shown = word.upper() if rng.random() < 0.1 else word
    return f"{_id(rng, fid)}: {shown}{rng.choice(REASONS)}", ("state", fid, word)


def body_for(rng, entry, omit=0.1):
    """``(body, [meaning per finding line])`` for one review of ``entry``."""
    known = sorted(entry.get("findings", {}), key=lambda k: int(k[1:]))
    later = bool(entry.get("last_head"))
    lines = []
    for fid in known:
        if rng.random() < 0.04:
            lines.append(new_line(rng, fid, later))          # restated as new: to be refused
        elif rng.random() >= omit:
            lines.append(state_line(rng, fid))
    top = max((int(k[1:]) for k in known), default=0)
    for n in range(rng.randint(0, 3)):
        lines.append(new_line(rng, f"F{top + 1 + n + (rng.random() < 0.1)}", later))
    if rng.random() < 0.3:
        lines.append(state_line(rng, f"F{top + rng.randint(1, 9)}"))   # #550: an unknown id
    if lines and rng.random() < 0.05:
        lines.append(rng.choice(lines))                                 # a repeated line
    rng.shuffle(lines)
    text = [t for t, _ in lines] + [rng.choice(NOISE) for _ in range(rng.randint(0, 2))]
    rng.shuffle(text)
    return "\n".join(text) + "\n", [m for _, m in lines]


def check(entry, body, verdict, answers):
    with mock.patch.object(findings, "changed_files", return_value=set(CHANGED)), \
            mock.patch.object(finding_answers, "_read_answers", return_value=answers):
        return findings.check(LOOP, entry, 7, "h", body, verdict)


class GeneratedFindingLines(unittest.TestCase):
    def assert_recorded(self, where, entry, body, meaning, verdict, head, at):
        after = findings.apply(entry, head, body, at=at)
        before, got = entry.get("findings", {}), after["findings"]
        said = {m[1] for m in meaning}
        problems = []
        for kind, fid, extra in meaning:
            item = got.get(fid)
            if item is None:
                problems.append(f"{fid} was accepted but not recorded")
            elif kind == "new" and (item["state"], item["head"]) != ("open", head):
                problems.append(f"new {fid} recorded as {item['state']} at {item['head']}")
            elif kind == "new" and item["missed"] != (extra and bool(entry.get("last_head"))):
                problems.append(f"new {fid} recorded with missed={item['missed']}")
            elif kind == "state" and item["state"] != extra:
                problems.append(f"{fid} stated {extra} but recorded {item['state']}")
        if set(got) != set(before) | said:
            problems.append(f"recorded ids {sorted(got)} are not {sorted(set(before) | said)}")
        for fid in set(before) - said:
            if got[fid] != before[fid]:
                problems.append(f"{fid} was not in the body but changed")
        if verdict == "APPROVE" and findings.open_ids(after):
            problems.append(f"APPROVE accepted with {findings.open_ids(after)} open")
        if after["last_head"] != head:
            problems.append("last_head did not move")
        if problems:
            self.fail(f"{where}: check accepted, apply disagrees: {'; '.join(problems)}\n"
                      f"entry: {before}\nbody:\n{body}")
        return after

    def assert_answerable(self, where, rng, entry, answers):
        for _ in range(60):
            body, _ = body_for(rng, entry, omit=0)
            if not check(entry, body, "REQUEST_CHANGES", answers):
                return
        self.fail(f"{where}: refused for an answered finding and no generated body was "
                  f"accepted\nentry: {entry}")

    def run_seed(self, seed):
        rng = random.Random(seed)
        for case in range(CASES):
            entry = {}
            for rnd in range(ROUNDS):
                at = 1000.0 + 100 * rnd
                answers = ([{"at": at - 50, "stamp": "", "who": "maintainer boss",
                             "where": "the PR", "body": "That is by design.", "fixer": False}]
                           if entry and rng.random() < 0.4 else [])
                body, meaning = body_for(rng, entry)
                verdict = rng.choice(VERDICTS)
                where = f"seed {seed} case {case} round {rnd} verdict {verdict}"
                reason = check(entry, body, verdict, answers)
                if not reason:
                    entry = self.assert_recorded(where, entry, body, meaning, verdict,
                                                 f"h{rnd}", at)
                elif ANSWERED in reason:
                    self.assert_answerable(where, rng, entry, answers)

    def test_check_and_apply_agree_on_generated_bodies(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.run_seed(seed)

    def test_the_generator_reaches_both_properties(self):
        # Guard on the guard: the seeds produce accepted bodies, answered refusals and #550's
        # shape (a state for an unknown id), so neither property passes vacuously.
        rng, seen = random.Random(SEEDS[0]), {"accepted": 0, "answered": 0, "unknown": 0}
        for _ in range(CASES):
            entry = {}
            for rnd in range(ROUNDS):
                at = 1000.0 + 100 * rnd
                answers = ([{"at": at - 50, "stamp": "", "who": "m", "where": "the PR",
                             "body": "x", "fixer": False}] if entry else [])
                body, meaning = body_for(rng, entry)
                known = entry.get("findings", {})
                seen["unknown"] += any(k == "state" and f not in known for k, f, _ in meaning)
                reason = check(entry, body, "REQUEST_CHANGES", answers)
                seen["answered"] += ANSWERED in reason
                if not reason:
                    seen["accepted"] += 1
                    entry = findings.apply(entry, f"h{rnd}", body, at=at)
        self.assertTrue(all(n >= 5 for n in seen.values()), seen)


if __name__ == "__main__":
    unittest.main()
