"""A change to the base's protection, checked when GitHub reports it (#683).

GitHub sends ``branch_protection_rule`` when a classic protection rule is created, edited or
deleted, and ``repository_ruleset`` when a repository ruleset is. With ``protection_webhook`` on,
the reviewer's route and hook carry both (``config.hook_events``), or the App's webhook does, and
the reviewer gate hands them here. The delivery is a nudge, never the truth (#679): neither event
says which rules now apply, so the base's protection is read again (``protection.read``: its
rulesets and its classic protection, as ``doctor`` reads them) and held against what the loop
expects of it: every check in ``required_checks``, and ``diaktoros/review`` when the loop posts it.

An expected check the base does not require is named in one ``protection`` notice. The same
checks are not named twice: ``FILE`` keeps them until the base requires them all again. A created
or deleted rule that cannot cover the base (another branch's pattern, a tag or push ruleset) reads
nothing; any other change (an edit, an unknown or missing action) is always read, since the rule
may have covered the base before (#2030).
Organization rulesets may send no repository event at all, so ``doctor`` stays the check of record,
and the rest of what it checks (a merge check pinned to the wrong App, required approvals) is left
to it: this only runs the requirement check sooner.
"""
from __future__ import annotations

import fnmatch
import re
import time

from . import ci, config, observer, protection
from .util import log, silence

FILE = "protection-lost.json"     # the expected checks the last notice named, while still lost
# The only actions whose payload is the rule's whole history: a rule created or deleted never
# targeted anything but what it carries. Any other action (edited, unknown, missing) is read.
NO_HISTORY = ("created", "deleted")


def gate(loop: dict, st, payload: dict) -> None:
    """The reviewer gate's path for a protection change. Always ends in a silence."""
    event = "branch_protection_rule" if isinstance(payload.get("rule"), dict) else "repository_ruleset"
    if not config.protection_webhook(loop):
        silence(f"{event}: protection_webhook is off for {loop['id']}; `doctor` checks protection")
    base = str(loop.get("base") or "main")
    if not covers(payload, base):
        silence(f"{event}: the changed rule does not cover {base}")
    expected = set(config.required_checks(loop)) | (
        {ci.MERGE_CHECK} if config.merge_check_on(loop) else set())
    if not expected:
        silence(f"{event}: {loop['id']} expects no required check of {base}")
    found = protection.read(loop)
    if not found.known:
        silence(f"{event}: could not read {base}'s protection ({'; '.join(found.unread)}); "
                "`doctor` checks it")
    lost = sorted(expected - found.checks)
    with st.locked():
        said = st.kv_get(st.dir / FILE, [])
        if not lost:
            if said:
                st.kv_set(st.dir / FILE, [])
            silence(f"{event}: {base} requires every check the loop expects")
        if said == lost:
            silence(f"{event}: {base} still does not require {', '.join(lost)}; already said")
        st.kv_set(st.dir / FILE, lost)
    unread = f" (could not read {found.unread[0]}: it may still require them)" if found.unread else ""
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    by = f"{event} {payload.get('action') or 'change'}" + (
        f" by {sender['login']}" if isinstance(sender.get("login"), str) else "")
    observer.notify(loop, st, "protection", 0, "", identity=f"{','.join(lost)}@{time.time_ns()}",
                    outcome=f"{base} does not require {', '.join(lost)}{unread} ({by})"[:600],
                    next_turn="you")
    log(f"{event}: {base} does not require {', '.join(lost)}")
    silence(f"{event}: {base} does not require {', '.join(lost)}; notice sent", "accepted")


def covers(payload: dict, base: str) -> bool:
    """Whether the changed rule may cover ``base``. Unsure is yes: only a created or deleted rule
    that plainly targets something else (a pattern ``base`` misses, a tag or push ruleset, an
    excluded ``base``) is no. Anything else is unsure (#2030): an edit, since the payload carries
    the rule as it is now, not whole as it was, so a rule narrowed off ``base`` (a new pattern,
    ``include`` or ``exclude``) may have been what required its checks, whatever ``changes`` names;
    and an unknown or missing action, since the gates route by payload shape, not by action, and
    nothing then says the rule's targets are all it ever had."""
    if payload.get("action") not in NO_HISTORY or payload.get("changes"):
        return True
    repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    rule = payload.get("rule")
    if isinstance(rule, dict):               # classic: one fnmatch pattern for branch names
        name = rule.get("name")
        if not isinstance(name, str):
            return True
        return fnmatch.fnmatchcase(base, name)
    ruleset = payload.get("repository_ruleset")
    if not isinstance(ruleset, dict):
        return True
    if ruleset.get("target") in ("tag", "push"):
        return False
    refs = (ruleset.get("conditions") or {}).get("ref_name") \
        if isinstance(ruleset.get("conditions"), dict) else None
    if not isinstance(refs, dict):
        return True
    ref, default = f"refs/heads/{base}", repo.get("default_branch")

    def names(pattern, loose: bool) -> bool:
        if pattern == "~ALL":
            return True
        if pattern == "~DEFAULT_BRANCH":
            return base == default if isinstance(default, str) else loose
        if not isinstance(pattern, str):
            return loose
        return fnmatch.fnmatchcase(ref, pattern) if loose else _exact_glob(pattern, ref)

    return (any(names(p, True) for p in refs.get("include") or [])
            and not any(names(p, False) for p in refs.get("exclude") or []))


def _exact_glob(pattern: str, ref: str) -> bool:
    """A ruleset's exclude pattern as GitHub reads it: ``*`` stays within one path segment, ``**``
    crosses them. A character class is not read: then nothing is taken as excluded."""
    if "[" in pattern:
        return False
    parts = re.split(r"(\*\*|\*|\?)", pattern)
    rx = "".join({"**": ".*", "*": "[^/]*", "?": "[^/]"}.get(p, re.escape(p)) for p in parts)
    return re.fullmatch(rx, ref) is not None
