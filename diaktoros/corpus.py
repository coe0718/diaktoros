"""Golden corpus (#491): replay historical PRs through the reviewer and score what it catches.

A case is a JSON file ``{"id", "pr", "head", "findings": [{"id", "pattern"}]}``: a historical PR,
the head commit it was reviewed at (optional: default the PR's current head), and the P1 findings a
review of it must raise. A finding is caught only when the review the turn would submit is
REQUEST_CHANGES **and** the finding's regex (case-insensitive) matches inside a numbered finding
line (``F<n>: ...``, #475): a mention in an APPROVE, or in prose, is not a catch. The replay is a
first look: it is shown no earlier review or answer. Only a PR's final head can be replayed yet.
Replay is on demand (``hermes dk corpus``) and runs no-write, like ``selftest --live-turn``.
Scores are appended to ``<state>/corpus_scores.jsonl`` per prompt revision and model.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

SCORES_FILE = "corpus_scores.jsonl"


class CorpusError(ValueError):
    pass


class UnsupportedCase(CorpusError):
    """A case that cannot run; refused, never scored as missed."""


VERDICT = "REQUEST_CHANGES"


def corpus_dir(loop: dict, override: str | None = None) -> Path:
    from . import config
    return Path(override).expanduser() if override else config.state_dir(loop) / "corpus"


def load(directory: Path) -> list[dict]:
    """Every case in the directory, validated; a malformed case raises rather than being skipped
    (a skipped case would read as a pass)."""
    cases, seen = [], set()
    for path in sorted(Path(directory).glob("*.json")):
        try:
            case = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise CorpusError(f"{path.name}: unreadable ({exc})")
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            raise CorpusError(f"{path.name}: needs a string id")
        if case["id"] in seen:
            raise CorpusError(f"{path.name}: duplicate case id {case['id']}")
        seen.add(case["id"])
        if not isinstance(case.get("pr"), int) or isinstance(case.get("pr"), bool):
            raise CorpusError(f"{path.name}: needs an integer pr")
        if "head" in case and (not isinstance(case["head"], str) or not case["head"]):
            raise CorpusError(f"{path.name}: head must be a commit sha string")
        findings = case.get("findings")
        if not isinstance(findings, list) or not findings:
            raise CorpusError(f"{path.name}: needs at least one finding")
        for finding in findings:
            if (not isinstance(finding, dict) or not isinstance(finding.get("id"), str)
                    or not isinstance(finding.get("pattern"), str)
                    or not finding["id"] or not finding["pattern"]):
                raise CorpusError(f"{path.name}: each finding needs an id and a pattern")
            try:
                re.compile(finding["pattern"])
            except re.error as exc:
                raise CorpusError(f"{path.name}: finding {finding['id']}: bad pattern ({exc})")
        cases.append(case)
    return cases


def score(case: dict, review) -> dict:
    """Which of the case's findings the review caught and which it missed.

    ``review`` is ``{"verdict", "body"}`` (None has no verdict and catches nothing). A catch
    needs the verdict REQUEST_CHANGES and the pattern inside a numbered finding line."""
    from . import findings
    review = review if isinstance(review, dict) else {}
    verdict, text = review.get("verdict"), review.get("body") or ""
    lines = [rest for _, _, rest in findings.parse(text)] if verdict == VERDICT else []
    caught = [f["id"] for f in case["findings"]
              if any(re.search(f["pattern"], line, re.I) for line in lines)]
    return {"case": case["id"], "verdict": verdict, "caught": caught,
            "missed": [f["id"] for f in case["findings"] if f["id"] not in caught]}


def replay(cases: list[dict], review) -> list[dict]:
    """Score each case. ``review(case)`` returns ``{"verdict", "body"}``, or raises: a turn that
    gave no review misses everything and says why. An ``UnsupportedCase`` is not a miss: it
    propagates."""
    results = []
    for case in cases:
        try:
            result = score(case, review(case))
        except UnsupportedCase:
            raise
        except Exception as exc:
            result = score(case, None)
            result["error"] = f"{type(exc).__name__}: {exc}"
        results.append(result)
    return results


def record(path: Path, prompt_rev: str, model: str, results: list[dict], now=None) -> dict:
    """Append one run's scores, keyed by prompt revision and model."""
    entry = {"at": time.time() if now is None else now, "prompt_rev": prompt_rev, "model": model,
             "caught": sum(len(r["caught"]) for r in results),
             "missed": sum(len(r["missed"]) for r in results), "cases": results}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as out:
        out.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def history(path: Path, skipped: list | None = None) -> list[dict]:
    """Recorded runs. A torn or malformed line is skipped (unlike ``load``, a bad score has no
    integrity argument); each skipped line number is appended to ``skipped`` when given."""
    try:
        lines = Path(path).read_bytes().splitlines()
    except OSError:
        return []
    rows = []
    for number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw.decode("utf-8"))
        except ValueError:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
            entry = None
        if not (isinstance(entry, dict)
                and all(k in entry for k in ("prompt_rev", "model", "caught", "missed"))):
            if skipped is not None:
                skipped.append(number)
            continue
        rows.append(entry)
    return rows


def live_review(loop: dict, settings: dict, reviewer, timeout: int):
    """A ``review(case)`` that runs one isolated no-write reviewer turn on the case's PR."""
    from . import broker_ipc, gh as gh_mod, selftest, trusted_turn
    from .run_supervisor import isolated_prompt, pr_change

    def review(case: dict) -> str:
        number = case["pr"]
        pr, error = gh_mod.fetch(loop, f"/repos/{loop['repo']}/pulls/{number}",
                                 login=loop.get("read_token"))
        if error or not isinstance(pr, dict):
            raise CorpusError(f"PR #{number} unreadable ({error or 'no answer'})")
        head = case.get("head") or pr["head"]["sha"]
        if head != pr["head"]["sha"]:
            raise UnsupportedCase(f"{case['id']}: head {head[:12]} is not PR #{number}'s current "
                                  f"head {pr['head']['sha'][:12]}; only the PR's final head is "
                                  "supported yet")
        row = {"seat": "reviewer", "repo": loop["repo"], "pr": number, "head": head}
        change = pr_change(loop, row)
        # A first look: no earlier verdict or answer, so the prompt says round 1 and the replay
        # cannot "catch" a finding by repeating it.
        prompt = isolated_prompt(loop, row, [], change=change)
        scope = broker_ipc.RunScope(loop["repo"], number, head, "reviewer", pr["head"]["ref"])
        observed: dict = {}
        trusted_turn.run_turn(loop, scope, source=Path(settings["source"]),
                              venv=Path(settings["venv"]), runtime=Path(settings["runtime"]),
                              rust=Path(settings["rust"]), upstream=reviewer.upstream,
                              key=reviewer.key, model=reviewer.model, prompt=prompt,
                              review_diff=change.diff, api_mode=reviewer.api_mode,
                              credential=reviewer.credential_provider(),
                              proxy_model=reviewer.proxy_model,
                              client_identity=reviewer.client_identity, timeout=timeout,
                              work_root=selftest._work_root(loop), no_write=True,
                              observed=observed)
        submissions = observed.get("submissions") or []
        if not submissions:
            raise CorpusError("the turn submitted no review")
        last = submissions[-1]
        return {"verdict": last.get("verdict"), "body": last.get("body") or ""}
    return review


def check_heads(loop: dict, cases: list[dict]) -> None:
    """Refuse, before any replay, a case whose head is not its PR's current head."""
    from . import gh as gh_mod
    for case in cases:
        if not case.get("head"):
            continue
        pr, error = gh_mod.fetch(loop, f"/repos/{loop['repo']}/pulls/{case['pr']}",
                                 login=loop.get("read_token"))
        if error or not isinstance(pr, dict):
            raise CorpusError(f"PR #{case['pr']} unreadable ({error or 'no answer'})")
        if case["head"] != pr["head"]["sha"]:
            raise UnsupportedCase(f"{case['id']}: head {case['head'][:12]} is not PR "
                                  f"#{case['pr']}'s current head; only the PR's final head is "
                                  "supported yet")
