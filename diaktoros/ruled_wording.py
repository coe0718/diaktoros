"""How the adjudicator's writes on an ACCEPT (``ruled_approval``, #905) name the ruling and the
reviews they act on: the ruling's citation, the tag that tells an approval's own write from
another's, and a review as the log and the ledger name it."""
from __future__ import annotations

from . import gate, gh
from .run_supervisor import production_ledger


def cite(loop: dict, number: int, ruling: dict) -> str:
    """Where the ruling can be read: its PR comment's link, or, with none, the host command that
    prints its full text from the run ledger."""
    if type(ruling.get("comment_id")) is int and ruling.get("comment") == "posted":
        return (f"https://github.com/{loop['repo']}/pull/{number}"
                f"#issuecomment-{ruling['comment_id']}")
    return (f"not posted as a comment (comment {ruling.get('comment') or 'unknown'}); its full text: "
            f"`python -m diaktoros.run_supervisor rulings {production_ledger()} "
            f"{ruling.get('run_id') or '?'}` on the host")


def ruling_tag(run: str) -> str:
    """How an approval on a ruling names it, so its own write can be told from another's."""
    return f"ruling `{run}`"


def named(review: dict) -> str:
    """A review as the log and the ledger name it."""
    return (f"{gate.reviewer_login(review) or '?'}'s {gh.review_state(review).lower()} "
            f"{review.get('id')} at {str(review.get('commit_id') or '?')[:7]}")
