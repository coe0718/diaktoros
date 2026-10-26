"""``hermes dk update`` (#904): what an update picks up, and optionally the update itself.

Hermes's own ``plugins check-updates`` reports a pinned install as up to date. This reads GitHub's
compare of the installed commit with the target through ``doctor.compare_plugin`` — the reading
doctor's ``plugin-version`` check takes — and lists the commits and the merged PRs between them.
"""
from __future__ import annotations

import re
import shlex
import subprocess

from .. import doctor, gh
from . import common as cli_common
from .operations import _one_loop

PR_LIST_MAX = 20
_SQUASH = re.compile(r"\s*\(#(\d+)\)\s*$")
_MERGE = re.compile(r"^Merge pull request #(\d+)\b")


def merged_prs(commits) -> list[tuple[int, str]]:
    """``(number, title)`` of each PR the commits merged, newest first. A squash or rebase merge
    names it as ``title (#N)``; a merge commit as ``Merge pull request #N`` with the title below.
    A commit that names no PR (a direct push) is counted as a commit only."""
    found: dict[int, str] = {}
    for commit in reversed(list(commits)):
        message = str(((commit.get("commit") or {}).get("message")) or "")
        lines = message.splitlines()
        first = lines[0] if lines else ""
        merge = _MERGE.match(first)
        if merge:
            title = next((ln.strip() for ln in lines[1:] if ln.strip()), "")
            found.setdefault(int(merge.group(1)), title)
            continue
        squash = _SQUASH.search(first)
        if squash:
            found.setdefault(int(squash.group(1)), _SQUASH.sub("", first).strip())
    return list(found.items())


def install_line(sha: str) -> str:
    return f"hermes plugins install {doctor.UPSTREAM_REPO} --force --ref {shlex.quote(sha)}"


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
    if sha:
        print(f"install:   {install_line(sha)}")
    else:
        print(f"install:   {install_line(target)}   (the target was not resolved to a commit; "
              "pin it by hand to be exact)")
    return installed, sha, readable


def cmd_update(args) -> int:
    """Show what an update picks up; with ``--apply`` install it, then run doctor."""
    from .. import migrate
    loop = _one_loop(args)
    if loop is None:
        return 2
    target = args.ref or "main"
    _installed, sha, _readable = _report(loop, target)
    if not args.apply:
        return 0
    if not sha:
        print("not applied: the target could not be resolved to a commit, so there is nothing "
              "exact to install; run the install line above by hand, or retry online")
        return 2
    hermes = cli_common._hermes_bin()
    if not hermes:
        print("not applied: the `hermes` executable was not found; run the install line above")
        return 2
    try:
        # The marker pauses the gates and the worker for the install, and a lock a second
        # migrate or update cannot take: the same guard `restore` uses for swapping host files.
        with migrate.hold():
            busy = migrate.runs_in_flight()
            if busy:
                print(f"not applied: {busy} run(s) are in flight or uncertain; reinstalling now "
                      "would swap code under a live worker. Wait for them (or reconcile them), "
                      "then run update --apply again")
                return 2
            cmd = [hermes, "plugins", "install", doctor.UPSTREAM_REPO, "--force", "--ref", sha]
            print(f"running: {shlex.join(cmd)}")
            if subprocess.run(cmd).returncode != 0:
                print("update FAILED: the install command did not succeed; the installed plugin "
                      "is as the command left it — check `hermes plugins list`")
                return 1
    except (migrate.MigrationBusy, migrate.RunsUnknown) as exc:
        print(f"not applied: {exc}")
        return 2
    now = doctor.installed_commit()
    print(f"installed now: {now[:7] if now else 'unreadable'}"
          + ("" if now == sha else f" (expected {sha[:7]})"))
    # A fresh process: this one still holds the old code.
    doc = subprocess.run([hermes, "dk", "doctor", "--loop", loop["id"]])
    return 0 if now == sha and doc.returncode == 0 else 1


def add_update(sub) -> None:  # noqa: ANN001
    """The ``update`` subcommand's parser, registered by ``cli.register_cli`` (#904)."""
    update = sub.add_parser("update", help="Show how many commits and which merged PRs an update "
                                           "picks up; with --apply, install it and run doctor")
    update.add_argument("--loop", help="loop id whose read token queries GitHub (default: the "
                                       "only configured loop)")
    update.add_argument("--ref", default="", metavar="SHA|TAG",
                        help="the commit or tag to update to (default: main's head)")
    update.add_argument("--apply", action="store_true",
                        help="run the install line, then doctor, and report the new commit; "
                             "refused while a loop turn is running")
    update.set_defaults(func=cmd_update)
