"""The base branch's protection, as the ``diaktoros/review`` merge check needs it (#1163 M2c).

GitHub never counts a GitHub App's review toward a branch's required approvals, but it does
enforce a required check pinned to an App (docs/github-app-support.md, 9). So App mode trades
required approvals for the required ``diaktoros/review`` check, pinned to the reviewer App.

``read`` answers what the base requires, from its rulesets (plain read access) and its classic
protection (admin access): every requirement of the check with the App it is pinned to, and the
most approvals any rule requires. ``require`` adds the requirement as the loop's own ruleset, named
after the check, beside whatever protection the base already has (it never edits another rule, so
``remove`` undoes it exactly). Turning required approvals off is left to the operator: that edits
their own rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ci, config, gh, util

RULESET_NAME = ci.MERGE_CHECK


@dataclass
class Protection:
    """What the base requires. ``sources``: one entry per requirement of the merge check, the App
    id it is pinned to, ``None`` for any source. ``approvals``: the most approving reviews any rule
    requires. ``unread``: the reads that did not answer (both unread: nothing is known).
    ``other_checks``: a read that answered requires some status check besides the merge check
    (#1240: then the unread half may well require the merge check too). ``checks``: every status
    check a read that answered requires, the merge check among them (#683)."""
    sources: list = field(default_factory=list)
    approvals: int = 0
    unread: list = field(default_factory=list)
    other_checks: bool = False
    checks: set = field(default_factory=set)

    @property
    def known(self) -> bool:
        return len(self.unread) < 2

    @property
    def required(self) -> bool:
        return bool(self.sources)


def reviewer_app_id(loop: dict) -> int | None:
    """The reviewer App's id, the check's pin; ``None`` for a PAT reviewer."""
    if not config.reviewer_is_app(loop):
        return None
    app = util.login_entry(loop.get("apps"), str(loop.get("reviewer_seat") or ""))
    return config.parse_app_id(app.get("app_id")) if isinstance(app, dict) else None


def unpinnable(loop: dict) -> str:
    """Why an App reviewer's check cannot be pinned (its stored id is not a valid App id), else
    ``""``. Such a loop must never get an any-source check as if it were a PAT reviewer's."""
    if config.reviewer_is_app(loop) and reviewer_app_id(loop) is None:
        return (f"the reviewer App {loop.get('reviewer_seat')} has no valid App id, so "
                f"{ci.MERGE_CHECK} cannot be pinned to it (fix 'apps' in the loop file)")
    return ""


def _count(value) -> int:
    return value if type(value) is int and value > 0 else 0


def read(loop: dict, login: str | None = None) -> Protection:
    """The base's requirements for the merge check and for approvals."""
    base = str(loop.get("base") or "main")
    found = Protection()
    rules, error = gh.branch_rules_read(loop, base, login=login)
    if rules is None:
        found.unread.append(f"rulesets ({gh.one_line(error)})")
    else:
        for rule in rules:
            params = rule.get("parameters") if isinstance(rule, dict) else None
            if not isinstance(params, dict):
                continue
            if rule.get("type") == "required_status_checks":
                listed = [c for c in params.get("required_status_checks") or []
                          if isinstance(c, dict)]
                found.sources += [c.get("integration_id") for c in listed
                                  if c.get("context") == ci.MERGE_CHECK]
                found.other_checks |= any(c.get("context") != ci.MERGE_CHECK for c in listed)
                found.checks |= {str(c["context"]) for c in listed if c.get("context")}
            elif rule.get("type") == "pull_request":
                found.approvals = max(found.approvals,
                                      _count(params.get("required_approving_review_count")))
    classic = gh.request(loop, f"/repos/{loop['repo']}/branches/{base}/protection", login=login)
    if classic.status != 404 and (classic.error or not isinstance(classic.data, dict)):
        found.unread.append(f"classic branch protection ({gh.one_line(classic.error or 'no answer')})")
    elif isinstance(classic.data, dict):
        checks = classic.data.get("required_status_checks")
        if isinstance(checks, dict):
            listed = [c for c in checks.get("checks") or [] if isinstance(c, dict)]
            found.sources += [c.get("app_id") for c in listed if c.get("context") == ci.MERGE_CHECK]
            contexts = [c for c in checks.get("contexts") or [] if isinstance(c, str)]
            if ci.MERGE_CHECK in contexts and not any(
                    c.get("context") == ci.MERGE_CHECK for c in listed):
                found.sources.append(None)
            found.other_checks |= any(c != ci.MERGE_CHECK for c in contexts) or any(
                c.get("context") != ci.MERGE_CHECK for c in listed)
            found.checks |= set(contexts) | {str(c["context"]) for c in listed if c.get("context")}
        reviews = classic.data.get("required_pull_request_reviews")
        if isinstance(reviews, dict):
            found.approvals = max(found.approvals,
                                  _count(reviews.get("required_approving_review_count")))
    found.sources = [s if type(s) is int and s > 0 else None for s in found.sources]
    return found


def ruleset_body(loop: dict) -> dict:
    """The loop's own ruleset: the base requires ``diaktoros/review``, pinned to the reviewer App
    (any source for a PAT reviewer's commit status, which cannot be pinned). No bypass: like
    *include administrators*, no one merges past it; ``remove`` takes it away."""
    why = unpinnable(loop)
    if why:
        raise config.ConfigError(why)       # fail closed: never an any-source check for an App
    check = {"context": ci.MERGE_CHECK}
    app_id = reviewer_app_id(loop)
    if app_id:
        check["integration_id"] = app_id
    return {"name": RULESET_NAME, "target": "branch", "enforcement": "active",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": [f"refs/heads/{loop.get('base') or 'main'}"],
                                        "exclude": []}},
            "rules": [{"type": "required_status_checks",
                       "parameters": {"strict_required_status_checks_policy": False,
                                      "do_not_enforce_on_create": False,
                                      "required_status_checks": [check]}}]}


def _own_ruleset(loop: dict, login: str | None) -> tuple[int | None, str]:
    """The id of the loop's ruleset (``None`` when there is none), or why it could not be read."""
    listed, error = gh.rulesets_read(loop, login=login)
    if listed is None:                    # never a prefix: a later page may hold ours (#2299)
        return None, gh.one_line(error)
    for ruleset in listed:
        if isinstance(ruleset, dict) and ruleset.get("name") == RULESET_NAME \
                and type(ruleset.get("id")) is int:
            return ruleset["id"], ""
    return None, ""


def require(loop: dict, login: str | None) -> tuple[bool, str]:
    """Create or update the loop's ruleset; ``(done, what happened)``."""
    ruleset_id, why = _own_ruleset(loop, login)
    if why:
        return False, f"could not read {loop['repo']}'s rulesets: {why}"
    try:
        body = ruleset_body(loop)
    except config.ConfigError as exc:
        return False, f"will not write the ruleset: {exc}"
    path = f"/repos/{loop['repo']}/rulesets" + (f"/{ruleset_id}" if ruleset_id else "")
    done = gh.request(loop, path, method="PUT" if ruleset_id else "POST", body=body, login=login)
    if done.error or not isinstance(done.data, dict):
        return False, (f"could not {'update' if ruleset_id else 'create'} the ruleset: "
                       f"{gh.one_line(done.error or 'no answer')} (the login needs admin access: "
                       "classic `repo`, or Administration: write)")
    app_id = reviewer_app_id(loop)
    pin = (f"pinned to the reviewer App ({app_id})" if app_id else
           "from any source (a commit status cannot be pinned)")
    return True, (f"{'updated' if ruleset_id else 'created'} ruleset {RULESET_NAME!r}: "
                  f"{loop.get('base') or 'main'} requires {ci.MERGE_CHECK}, {pin}")


def remove(loop: dict, login: str | None) -> tuple[bool, str]:
    """Delete the loop's ruleset; ``(done, what happened)``. Nothing else is touched."""
    ruleset_id, why = _own_ruleset(loop, login)
    if why:
        return False, f"could not read {loop['repo']}'s rulesets: {why}"
    if ruleset_id is None:
        return True, f"no ruleset {RULESET_NAME!r} on {loop['repo']}: nothing to remove"
    done = gh.request(loop, f"/repos/{loop['repo']}/rulesets/{ruleset_id}", method="DELETE",
                      login=login)
    if done.error:
        return False, f"could not delete the ruleset: {gh.one_line(done.error)}"
    return True, f"deleted ruleset {RULESET_NAME!r}: {loop.get('base') or 'main'} no longer requires {ci.MERGE_CHECK}"


def problems(loop: dict, found: Protection) -> tuple[list[str], list[str], list[str]]:
    """``(blocking, warnings, facts)`` about the base for this loop, in plain words."""
    base = str(loop.get("base") or "main")
    blocking, warnings, facts = [], [], []
    posting = config.merge_check_on(loop)
    if not posting:
        if found.required:
            blocking.append(f"{base} requires {ci.MERGE_CHECK} but this loop never posts it "
                            "(merge_check is off for it): every PR stays blocked")
        elif found.unread and found.other_checks:
            # #1240: silent when the half that answered requires no status check at all (most
            # PAT loops' readers lack the admin access classic protection needs, so a warning
            # there would be noise); a base that requires other checks may require this one in
            # the half that was not read.
            warnings.append(f"could not read {found.unread[0]}, and {base} requires other "
                            f"status checks: if it also requires {ci.MERGE_CHECK}, which this "
                            "loop never posts, every PR stays blocked")
        return blocking, warnings, facts
    app_id = reviewer_app_id(loop)
    if unpinnable(loop):
        blocking.append(unpinnable(loop))
        return blocking, warnings, facts
    if not found.required:
        where = "requires it nowhere doctor could read" if found.unread else "does not require it"
        warnings.append(f"{ci.MERGE_CHECK} is posted but {base} {where}: the merge "
                        "button ignores it")
    elif app_id:
        others = sorted({s for s in found.sources if s is not None and s != app_id})
        if others:
            blocking.append(f"{base} requires {ci.MERGE_CHECK} from App {', '.join(map(str, others))}, "
                            f"not the reviewer App ({app_id}): it can never be satisfied")
        elif app_id not in found.sources:
            warnings.append(f"{base} requires {ci.MERGE_CHECK} from any source: anyone with "
                            "write access can post a green status of that name")
        else:
            facts.append(f"{base} requires {ci.MERGE_CHECK} from the reviewer App ({app_id})")
    else:
        pinned = sorted({s for s in found.sources if s is not None})
        if pinned:
            # A commit status is never an App's check run, so a pin can never be met (shadow
            # review of #1237: a ruleset `protect` wrote while the reviewer was an App).
            blocking.append(f"{base} requires {ci.MERGE_CHECK} from App "
                            f"{', '.join(map(str, pinned))}, and the reviewer's PAT posts a "
                            "commit status, which never satisfies it: every PR stays blocked")
        else:
            facts.append(f"{base} requires {ci.MERGE_CHECK} (a commit status from the "
                         "reviewer's PAT: it cannot be pinned)")
    if app_id and found.approvals:
        warnings.append(f"{base} requires {found.approvals} approving review(s), and GitHub never "
                        "counts the reviewer App's: every PR also needs a person's approval")
    if found.unread:
        # #1238: half the base is unknown (required approvals among it), so never verified.
        warnings.append(f"could not read {found.unread[0]}: what it requires is unknown")
    return blocking, warnings, facts
