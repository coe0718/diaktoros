"""``hermes dk update`` (#904): what an update picks up. It only reads.

Hermes's own ``plugins check-updates`` reports a pinned install as up to date. This reads GitHub's
compare of the installed commit with the target through ``doctor.compare_plugin`` — the reading
doctor's ``plugin-version`` check takes — and lists the commits and the merged PRs between them.

It installs nothing (the catalog review of v0.6.0): a catalog install moves only to a commit the
catalog has reviewed, through its re-pinned ``sha`` and ``hermes plugins update diaktoros``, so the
plugin carries no updater of its own. The report names that command.
"""
from __future__ import annotations

import re

from .. import doctor, gh
from .operations import _one_loop

PR_LIST_MAX = 20
_SQUASH = re.compile(r"\s*\(#(\d+)\)\s*$")
_MERGE = re.compile(r"^Merge pull request #(\d+)\b")
_INNER_REF = re.compile(r"\s*\(#\d+\)")


def merged_prs(commits) -> list[tuple[int, str]]:
    """``(number, title)`` of each PR the commits merged, newest first. A squash or rebase merge
    names it as ``title (#N)``; a merge commit as ``Merge pull request #N`` with the title below.
    A commit that names no PR is counted as a commit only. The subject alone decides which PR a
    commit is, so a direct push whose subject ends in ``(#N)`` counts as PR #N, issue or not. The
    shown title is the subject for a squash commit, but for a merge commit it is the first nonblank
    body line, or "" when the body has none. A ``(#N)`` inside the title (a revert of a squash
    commit) is dropped from it."""
    found: dict[int, str] = {}
    for commit in reversed(list(commits)):
        message = str(((commit.get("commit") or {}).get("message")) or "")
        lines = message.splitlines()
        first = lines[0] if lines else ""
        merge = _MERGE.match(first)
        if merge:
            title = next((ln.strip() for ln in lines[1:] if ln.strip()), "")
            found.setdefault(int(merge.group(1)), _INNER_REF.sub("", title).strip())
            continue
        squash = _SQUASH.search(first)
        if squash:
            title = _INNER_REF.sub("", _SQUASH.sub("", first)).strip()
            found.setdefault(int(squash.group(1)), title)
    return list(found.items())


# How to update: through Hermes, which moves a catalog install to the commit the catalog reviewed.
INSTALL_LINE = "hermes plugins update diaktoros"
INSTALL_NOTE = "(a catalog install moves to the commit the catalog has reviewed, which may be older)"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _target_sha(loop: dict, target: str, comparison) -> str:
    """The commit ``target`` names now: GitHub's answer, else the compare's last commit when the
    compare listed every commit. Empty when neither can be read."""
    resp = gh.request(loop, f"/repos/{doctor.UPSTREAM_REPO}/commits/{target}")
    sha = resp.data.get("sha") if isinstance(resp.data, dict) else None
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha):
        return sha
    if comparison.commits and comparison.ahead_by == len(comparison.commits):
        last = comparison.commits[-1].get("sha")
        if isinstance(last, str) and re.fullmatch(r"[0-9a-f]{40}", last):
            return last
    return ""


def _report(loop: dict, target: str) -> tuple[str, str, bool]:
    """Print what the update picks up. Returns ``(installed, target sha, readable)``: readable is
    true only when the count was read, never guessed."""
    installed = doctor.installed_commit()
    if not installed:
        print("installed: unknown (the plugin is not a git checkout, so there is nothing to compare)")
        comparison = doctor.PluginComparison()
    else:
        print(f"installed: {installed[:7]}")
        comparison = doctor.compare_plugin(loop, installed, target)
    sha = _target_sha(loop, target, comparison)
    print(f"target:    {target}" + (f" = {sha[:7]}" if sha else " (not resolved)"))
    behind = comparison.ahead_by
    readable = bool(installed) and not comparison.error and type(behind) is int
    if not readable:
        why = ("no installed commit to compare" if not installed else
               f"could not read GitHub's compare ({gh.one_line(comparison.error or 'no comparison')})")
        print(f"picks up:  unknown — {why}; no count is guessed")
    elif behind == 0:
        print("picks up:  nothing — the installed commit has everything the target has"
              + (f" (the target is {comparison.status})" if comparison.status == "behind" else ""))
    else:
        prs = merged_prs(comparison.commits)
        partial = len(comparison.commits) < behind
        head = f"picks up:  {_plural(behind, 'commit')}"
        if prs or not partial:
            head += f", {_plural(len(prs), 'PR')}"
        if prs:
            shown = "; ".join(f"#{n} {t}".rstrip() for n, t in prs[:PR_LIST_MAX])
            more = f"; … and {len(prs) - PR_LIST_MAX} more" if len(prs) > PR_LIST_MAX else ""
            head += f": {shown}{more}"
        print(head)
        if partial:
            print(f"           (GitHub's compare listed {len(comparison.commits)} of the {behind} "
                  "commits, so the PR list is partial)")
        if comparison.status == "diverged":
            print("           (the installed commit is not on the target's history: the target "
                  "also drops commits the install has)")
    print(f"install:   {INSTALL_LINE}   {INSTALL_NOTE}")
    return installed, sha, readable


def cmd_update(args) -> int:
    """Show what an update picks up, and the command that updates."""
    loop = _one_loop(args)
    if loop is None:
        return 2
    _report(loop, args.ref or "main")
    return 0


def add_update(sub) -> None:  # noqa: ANN001
    """The ``update`` subcommand's parser, registered by ``cli.register_cli`` (#904)."""
    update = sub.add_parser("update", help="Show how many commits and which merged PRs an update "
                                           "picks up (read-only)")
    update.add_argument("--loop", help="loop id whose read token queries GitHub (default: the "
                                       "only configured loop)")
    update.add_argument("--ref", default="", metavar="SHA|TAG",
                        help="the commit or tag to update to (default: main's head)")
    update.set_defaults(func=cmd_update)
