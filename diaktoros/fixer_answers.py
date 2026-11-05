"""The fixer's answers comment as text (#52): its bound, its marker, its body and its parse.

Moved from ``broker`` (#1907), which keeps the POST (``broker.post_fixer_answers``). Nothing here
reads or writes GitHub.
"""
from __future__ import annotations

import re

from .wire import ANSWERS_MARKER, ANSWERS_MARKERS  # one home, shared with the sandbox client

# The fixer's answers to the findings: one bounded PR comment, posted by the host as the fixer
# identity after a confirmed push (#52). The hidden marker is how a later record tells these from
# any other comment: it is only trusted on a comment authored by the fixer seat's own login, and
# even then the text is the fixer model's words — data for the next seat, never instructions.
ANSWERS_MAX = 8 * 1024
_ANSWERS_MARKER = re.compile("(?:" + "|".join(map(re.escape, ANSWERS_MARKERS)) + ")"
                             + r" run=([A-Za-z0-9_.:-]{1,80}) "
                             r"head=([0-9a-f]{40}) base=([0-9a-f]{40}) -->\n")


def answers_valid(text: object) -> bool:
    """Non-empty text within the bound, with no NUL and no marker of its own."""
    return (isinstance(text, str) and bool(text.strip()) and "\x00" not in text
            and len(text.encode()) <= ANSWERS_MAX
            and not any(marker in text for marker in ANSWERS_MARKERS))


def answers_comment_body(text: str, *, head: str, base: str, run_id: str,
                         merge: dict | None = None) -> str:
    """The comment's text. A conflict turn's (``merge``, its scope's) is headed as the merge of
    the base it is: it answers no review finding, so it must not read as answers (#1907)."""
    title = (f"**Fixer's merge of `{merge.get('base_ref')}` at `{str(merge.get('base_sha'))[:12]}` "
             f"into `{base[:12]}`** — pushed as `{head[:12]}`. A merge only: it answers no review "
             "finding" if merge else
             f"**Fixer's answers to the review of `{base[:12]}`** — pushed as `{head[:12]}`")
    return (f"{ANSWERS_MARKER} run={run_id} head={head} base={base} -->\n"
            f"{title}\n\n"
            f"{text.strip()}\n\n"
            "_Posted by the Diaktoros host for the fixer seat; the fixer's own words, "
            "not verified by the loop._")


def parse_answers_comment(comment: object, loop: dict) -> dict | None:
    """``{run, head, base, body, created_at}`` for a fixer-answers comment, else ``None``.

    Both must hold: the author is the configured fixer seat login, and the body starts with the
    host's marker. A human (or anyone else) writing the marker is not the fixer seat; the fixer
    seat writing prose without it is not an answers record.
    """
    if not isinstance(comment, dict):
        return None
    login = ((loop.get("seats") or {}).get("fixer") or {}).get("login")
    user = comment.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    body = comment.get("body")
    if (not isinstance(login, str) or not login or not isinstance(author, str)
            or author.casefold() != login.casefold() or not isinstance(body, str)):
        return None
    match = _ANSWERS_MARKER.match(body)
    if not match:
        return None
    return {"run": match.group(1), "head": match.group(2), "base": match.group(3),
            "body": body[match.end():], "created_at": str(comment.get("created_at") or "")}
