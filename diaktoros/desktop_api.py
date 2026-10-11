"""The Desktop page's backend logic (#369): read-only views over one loop, for
``dashboard/plugin_api.py``. Kept free of FastAPI so it is tested without it.

Nothing here writes, calls GitHub or reads a token: ``stats`` is the run ledger's half of
``hermes dk stats`` (totals and timings only), and ``now`` lists live runs with
host-written reasons, never a turn's output.
"""
from __future__ import annotations

from . import config, run_supervisor, stats

SINCE_CHOICES = ("24h", "7d", "30d")


class NotFound(LookupError):
    pass


class BadRequest(ValueError):
    pass


def loops() -> list[dict]:
    """The configured loops the page can show: id and repository only."""
    found, _ = config.readable_loops()
    return [{"id": loop["id"], "repo": loop["repo"]} for loop in found]


def _loop(loop_id: str | None) -> dict:
    found, _ = config.readable_loops()
    if loop_id:
        for loop in found:
            if loop["id"] == loop_id:
                return loop
        raise NotFound("no such loop")
    if len(found) != 1:
        raise NotFound("name a loop: " + (", ".join(loop["id"] for loop in found) or "none"))
    return found[0]


def stats_view(loop_id: str | None, since: str = "7d") -> dict:
    if since not in SINCE_CHOICES:
        raise BadRequest(f"since must be one of {', '.join(SINCE_CHOICES)}")
    loop = _loop(loop_id)
    return stats.collect(loop, run_supervisor.production_ledger(), stats.parse_since(since),
                         with_github=False)


def now_view(loop_id: str | None) -> dict:
    loop = _loop(loop_id)
    view = stats.now(run_supervisor.production_ledger(), loop["repo"])
    return view if view is not None else {"repo": loop["repo"], "runs": [], "counts": {}}
