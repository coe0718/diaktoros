"""Golden corpus (#491): replay historical PRs through the reviewer and score what it catches.

A case is a JSON file ``{"id", "pr", "head", "findings": [{"id", "pattern"}]}``: a historical PR,
the head commit it was reviewed at, and the P1 findings a review of it must raise. A finding is
caught when its regex (case-insensitive) matches the body of the review the turn would submit.
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


def score(case: dict, body: str | None) -> dict:
    """Which of the case's findings the review body caught and which it missed."""
    text = body or ""
    caught = [f["id"] for f in case["findings"] if re.search(f["pattern"], text, re.I)]
    return {"case": case["id"], "caught": caught,
            "missed": [f["id"] for f in case["findings"] if f["id"] not in caught]}


def replay(cases: list[dict], review) -> list[dict]:
    """Score each case. ``review(case)`` returns the review body, or raises: a turn that gave no
    review misses everything and says why."""
    results = []
    for case in cases:
        try:
            result = score(case, review(case))
        except Exception as exc:
            result = score(case, "")
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


def history(path: Path) -> list[dict]:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def live_review(loop: dict, settings: dict, reviewer, timeout: int):
    """A ``review(case)`` that runs one isolated no-write reviewer turn on the case's PR."""
    from . import broker_ipc, gh as gh_mod, selftest, trusted_turn
    from .run_supervisor import effective_reviews, isolated_prompt, pr_change

    def review(case: dict) -> str:
        number = case["pr"]
        pr, error = gh_mod.fetch(loop, f"/repos/{loop['repo']}/pulls/{number}",
                                 login=loop.get("read_token"))
        if error or not isinstance(pr, dict):
            raise CorpusError(f"PR #{number} unreadable ({error or 'no answer'})")
        head = case.get("head") or pr["head"]["sha"]
        row = {"seat": "reviewer", "repo": loop["repo"], "pr": number, "head": head}
        reviews = effective_reviews(loop, row, gh_mod.reviews(loop, number),
                                    str(selftest.ledger_path()))
        change = pr_change(loop, row)
        prompt = isolated_prompt(loop, row, reviews, change=change)
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
        return submissions[-1].get("body") or ""
    return review
