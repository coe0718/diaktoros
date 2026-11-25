"""What ``apply`` and ``set`` say when a change stops the host posting the merge check (#1987).

A ruleset ``protect`` wrote makes the base require ``diaktoros/review``, and nothing removes it
when posting turns off (``config.merge_check_on`` goes from true to false: ``merge_check: off``,
or ``auto`` once the reviewer is no GitHub App). Every PR then stays blocked. ``protect`` and
``doctor`` read the ruleset and say so (``protection.problems``); ``apply`` and ``set`` do not
read it, so they warn whenever their change turns posting off, whatever moved with it.
"""
from __future__ import annotations

from .. import ci, config


def why(loop: dict) -> str:
    """The setting that decides whether ``loop`` posts the check, as it really is (#1986): an
    explicit ``on``/``off`` is named as such, never as ``auto``."""
    mode = loop.get("merge_check") or "auto"
    if mode != "auto":
        return f"merge_check: {mode}"
    return ("merge_check: auto follows an App reviewer" if config.reviewer_is_app(loop)
            else "merge_check: auto, and the reviewer is no GitHub App")


def note(loop: dict, updated: dict, dry_run: bool) -> bool:
    """Print the warning when ``loop`` posted the check and ``updated`` does not; whether it
    did. No pin is named: the reviewer's App is not what changed here."""
    if not config.merge_check_on(loop) or config.merge_check_on(updated):
        return False
    lid = updated["id"]
    print(f"  the {ci.MERGE_CHECK} merge check "
          f"{'would stop being posted' if dry_run else 'is now off'} ({why(updated)}). If a "
          f"ruleset `protect` wrote still requires it, every PR stays blocked until you turn "
          f"posting back on (`hermes dk set --loop {lid} --merge-check on`) or remove the "
          f"ruleset (`hermes dk protect --loop {lid} --remove`)")
    return True
