"""The live issue an issue-seat turn acts on (#1349, split from the core): ``triage_issue`` and
``triage_blocker`` for triage (#213, #849), ``issue_fix_issue`` and ``issue_fix_dependencies`` for
an issue fix (#214, #775), each read as the reader and refused or retried, never guessed, plus
the limits on what triage may post. The core's names are reached through the package
(``run_supervisor.X``); callers use ``issue_inputs.<name>``.
"""
from __future__ import annotations

from .. import run_supervisor


TRIAGE_TITLE_MAX = 256


TRIAGE_BODY_MAX = 8000


TRIAGE_COMMENT_MAX = 1000


def triage_issue(loop: dict, number: int) -> dict:
    """The live issue a triage run may act on, or raise (#213).

    Read as the reader, right before launch and again by the broker before any write: an issue
    that closed, turned out to be a pull request, changed author, or that a person has since
    labelled from the triage list is no longer this run's to triage.
    """
    from .. import gh
    triage = loop.get('triage') or {}
    if not triage.get('route'):
        raise ValueError('issue triage is off for this loop')
    issue = gh.api(loop, f"/repos/{loop['repo']}/issues/{number}", login=loop['read_token'])
    if not isinstance(issue, dict) or issue.get('number') != number:
        raise run_supervisor.RetryableError('issue unreadable (GitHub read failed)')
    why = triage_blocker(loop, issue)
    if why:
        raise ValueError(why)
    return issue


def triage_blocker(loop: dict, issue: dict) -> str:
    """Why this issue is not (or no longer) triage's, or ``''`` when it is (#213, #849).

    The one rule for the run's launch, the broker's write and the watchdog's recovery sweep.
    """
    triage = loop.get('triage') or {}
    if 'pull_request' in issue:
        return 'not an issue (a pull request)'
    if issue.get('state') != 'open':
        return 'issue no longer open'
    author = str((issue.get('user') or {}).get('login') or '').lower()
    if author not in (triage.get('authors') or ()):
        return 'issue author is not in triage.authors'
    present = {str((label or {}).get('name') or '').casefold()
               for label in issue.get('labels') or [] if isinstance(label, dict)}
    if present & {name.casefold() for name in triage.get('labels') or []}:
        return 'issue already carries a triage label (a person labelled it)'
    return ''


def issue_fix_issue(loop: dict, number: int) -> dict:
    """The live issue an issue-fix run may act on, or raise (#214).

    Read as the reader at the gate, before launch and again by the broker before any write: it
    must still be an open issue (not a PR) by an allowlisted author, still carrying the fix
    label a maintainer applied.
    """
    from .. import config, gh
    triage = loop.get('triage') or {}
    if not triage.get('route') or not triage.get('fix_label'):
        raise ValueError('issue fixes are off for this loop')
    issue = gh.api(loop, f"/repos/{loop['repo']}/issues/{number}", login=loop['read_token'])
    if not isinstance(issue, dict) or issue.get('number') != number:
        raise run_supervisor.RetryableError('issue unreadable (GitHub read failed)')
    if 'pull_request' in issue:
        raise ValueError('not an issue (a pull request)')
    if issue.get('state') != 'open':
        raise run_supervisor.IssueIneligible('no longer eligible: the issue is no longer open')
    if str((issue.get('user') or {}).get('login') or '').lower() not in triage.get('authors', ()):
        raise run_supervisor.IssueIneligible('no longer eligible: the issue author is not in triage.authors')
    present = {str((label or {}).get('name') or '').casefold()
               for label in issue.get('labels') or [] if isinstance(label, dict)}
    # A maintainer's fix label, or (#232) a triage label the loop auto-offers (never with P0-P2).
    if (triage['fix_label'].casefold() not in present
            and not config.auto_fix_eligible(loop, present)):
        offered = [str(x) for x in triage.get('auto_fix_labels') or []]
        if offered and config.issue_fixes_enabled(loop):
            raise run_supervisor.IssueIneligible(
                f"no longer eligible: neither the fix label ({triage['fix_label']!r}) nor an "
                f"auto-offer label ({', '.join(offered)}) is on it (an auto-offer label never "
                "counts beside a P0-P2 label)")
        raise run_supervisor.IssueIneligible(f"no longer eligible: the {triage['fix_label']!r} label is "
                                             "no longer on the issue")
    return issue


def issue_fix_dependencies(loop: dict, number: int) -> None:
    """Raise ``RetryableError`` while a declared prerequisite of the issue is unmet or
    unreadable (#775): checked at launch and at the broker's publication boundary."""
    from .. import issue_deps
    why = issue_deps.hold_reason(loop, number)
    if why:
        raise run_supervisor.RetryableError(f'issue #{number} {why}')
