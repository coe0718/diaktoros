"""Backend routes for the Desktop Diaktoros page (#369, #630, #371), mounted by Hermes at
``/api/plugins/diaktoros/``, behind the gateway's own auth.

Six routes read the loop config, the run ledger and (``/health``) the installation's own files.
**One writes:** ``POST /retry`` re-arms a run the CLI's ``retry`` would re-arm — the page's one
confirmed action, and the same host-owned code path (``diaktoros.retry_action`` over
``Supervisor.retry``). No route calls GitHub or reads a token except ``/health?probe=1``, which is
local-only. The logic is ``diaktoros.desktop_api``; this module maps its exceptions to status
codes and does nothing else.
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:          # Hermes imports this file by path, not as a package
    sys.path.insert(0, str(_ROOT))

from fastapi import APIRouter, HTTPException, Request  # noqa: E402

from diaktoros import desktop_api  # noqa: E402

router = APIRouter()


def _answer(fn, *args):
    try:
        return fn(*args)
    except desktop_api.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except desktop_api.Forbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except desktop_api.Conflict as exc:
        # A retry the ledger refuses: the operator sees the host's own reason, not a 500.
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except desktop_api.BadRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.get("/loops")
def loops():
    return {"loops": desktop_api.loops()}


@router.get("/stats")
def stats(loop: str | None = None, since: str = "7d"):
    return _answer(desktop_api.stats_view, loop, since)


@router.get("/now")
def now(loop: str | None = None):
    return _answer(desktop_api.now_view, loop)


@router.get("/overview")
def overview():
    return _answer(desktop_api.overview_view)


@router.get("/pr")
def pr(pr: int, loop: str | None = None):
    return _answer(desktop_api.pr_view, loop, pr)


@router.get("/health")
def health(request: Request, loop: str | None = None, probe: bool = False):
    client = request.client
    # The whole header mapping goes down, not one value: presence is what refuses, and an empty
    # X-Forwarded-For is still a forwarding header (#648 review, F1).
    return _answer(desktop_api.health_view, loop, probe,
                   client.host if client else None, request.headers)


@router.get("/settings")
def settings():
    return _answer(desktop_api.settings_view)


@router.post("/retry")
def retry(payload: dict):
    """Re-arm one run: ``{loop, pr, seat, head}`` — the head being what the operator confirmed.

    The body is read as a plain mapping and validated in ``desktop_api`` so every malformed
    selection answers 400 with a bounded reason (never a stack trace), and so the rules are
    testable without FastAPI. Nothing here trusts the client's view of the run: the ledger is
    re-read, and a confirmation whose head has moved is refused with 409.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return _answer(desktop_api.retry_view, payload.get("loop"), payload.get("pr"),
                   payload.get("seat"), payload.get("head"))
