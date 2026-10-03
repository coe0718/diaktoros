"""What the loop posts is signed as the loop's (#197): a footer on bodies, a trailer on commits.

Applied on the host, at the one place each write is sent — the receipted review
(``review_receipt.submit``), ``broker.perform``, the fixer's answers comment and the ruling comment
(``broker``), and the fixer's commit (``safe_push``) — never by the seat. So it marks only what this
plugin itself posts (a PR the fixer's operator opens by hand, or a human's review, is never touched),
and a seat can neither strip it nor make the loop skip it: a body that already carries footer-shaped
text gets the real footer appended all the same.

It is a label for readers, not evidence: anyone can paste the same text, so the loop never reads it
back as proof of its own write — receipts and the run ledger are that. ``attribution: false`` on a
loop turns both off.
"""

from __future__ import annotations

import re

REPO_URL = "https://github.com/coe0718/hermes-review-loop"
TRAILER = f"Automated-By: hermes-review-loop ({REPO_URL})"
# GitHub's limit on a review or comment body, in characters.
GITHUB_BODY_MAX = 65536

_SEAT_NAMES = {"reviewer": "reviewer seat", "fixer": "fixer seat", "adjudicator": "adjudicator",
               "triage": "issue triage"}
# One ``Key: value`` line of a Git trailer block (git interpret-trailers' shape).
_TRAILER_LINE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*: \S.*\Z")


class AttributionError(ValueError):
    """The signed body would exceed what GitHub accepts; the write must not be sent."""


def enabled(loop: dict) -> bool:
    """On unless the loop says ``attribution: false`` (the default is on)."""
    return not (isinstance(loop, dict) and loop.get("attribution") is False)


def _who(loop: dict, seat: str) -> str:
    """``reviewer seat (Critic)``: the seat, and its agent's display name when the loop has one."""
    name = _SEAT_NAMES.get(seat, seat)
    if seat in ("reviewer", "fixer"):
        agent = str((((loop.get("seats") or {}).get(seat)) or {}).get("agent") or "").strip()
    else:
        agent = ""
    # A display name is the operator's text, rendered as Markdown on GitHub: keep it to letters,
    # digits, spaces and ``-``, so it can never become a link (no ``:``/``/``, no ``www.``), markup
    # or a second line.
    agent = re.sub(r"[^A-Za-z0-9 \-]", "", agent)[:40].strip()
    return f"{name} ({agent})" if agent else name


def footer(loop: dict, *, seat: str, head: str) -> str:
    short = head[:7] if isinstance(head, str) else ""
    at = f" · head `{short}`" if short else ""
    return (f"<sub>🤖 Automated by [hermes-review-loop]({REPO_URL}) · "
            f"{_who(loop, seat)}{at}</sub>")


def stamp(loop: dict, body: str, *, seat: str, head: str) -> str:
    """``body`` with the loop's footer appended once — or unchanged when attribution is off.

    Raises :class:`AttributionError` when the signed body would exceed GitHub's limit: the write is
    refused rather than sent unsigned or cut mid-sentence. (Seat bodies are capped far below it.)
    """
    if not enabled(loop):
        return body
    signed = body.rstrip() + "\n\n---\n" + footer(loop, seat=seat, head=head)
    if len(signed) > GITHUB_BODY_MAX:
        raise AttributionError(f"signed body is {len(signed)} characters; GitHub accepts "
                               f"{GITHUB_BODY_MAX}")
    return signed


# The footer ``stamp`` appends, exactly: ``unsign`` removes this and nothing else.
_FOOTER_AT_END = re.compile(r"\n\n---\n<sub>🤖 Automated by \[hermes-review-loop\]\("
                            + re.escape(REPO_URL) + r"\) · [^\n]*</sub>\s*\Z")


def unsign(body: object) -> object:
    """``body`` without a trailing footer of ours, for the review history a seat reads.

    Display only: the seat sees the words, not the label, and the history's byte cap is not spent
    on it. Nothing here decides anything — a body is never trusted or distrusted for having one.
    """
    return _FOOTER_AT_END.sub("", body) if isinstance(body, str) else body


def sign_commit(loop: dict, message: str) -> str:
    """``message`` with the ``Automated-By`` trailer — or unchanged when attribution is off.

    It joins an existing trailer block (a last paragraph made only of ``Key: value`` lines, such as a
    seat's own ``Co-Authored-By``), else starts one after a blank line, as ``git interpret-trailers``
    would.
    """
    if not enabled(loop):
        return message
    text = message.rstrip()
    paragraphs = text.split("\n\n")
    last = paragraphs[-1].splitlines() if len(paragraphs) > 1 else []
    joins = bool(last) and all(_TRAILER_LINE.match(line) for line in last)
    return text + ("\n" if joins else "\n\n") + TRAILER
