"""Backend routes for the Desktop Diaktoros page (#369, #630, #371), mounted by Hermes at
``/api/plugins/diaktoros/``, behind the gateway's own auth.

Reads cover loop config, the run ledger, installation health and GitHub hook state.
``POST /retry`` re-arms a run via ``retry_action``; ``POST /hooks`` confirms a pause/resume
via the same ``arm_action`` operation as CLI ``arm``. Hooks use the existing host credential
resolution and return no credentials or paths. ``GET /actions`` previews host-owned eligibility;
``POST /actions`` performs confirmed escalation and reads back its exact marker, and a confirmed
cancel of the exact runs it previewed (#1148; the page's control is #1372). Review/drain currently refuse unsafe confirmations. ``/health?probe=1`` remains local-only.
The logic lives in ``diaktoros.desktop_api``; this module maps exceptions and validates body shape.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:          # Hermes imports this file by path, not as a package
    sys.path.insert(0, str(_ROOT))

from fastapi import APIRouter, Body, HTTPException, Request, WebSocket  # noqa: E402

from diaktoros import desktop_api, desktop_events  # noqa: E402

router = APIRouter()


@router.websocket("/events")
async def events(websocket: WebSocket):
    """Plugin-scoped SDK transport; host consent/scope plus explicit WS authentication."""
    await desktop_events.serve(websocket)



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
    except desktop_api.HookRequest as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except desktop_api.BadRequest as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


def _read_answer(fn, *args):
    """Read failures are unavailable, never a successful empty result or raw transport text.

    Keep the shared operation/refusal mapping, but bound unexpected store/runtime failures
    at the HTTP read boundary. Writes deliberately keep their existing exception semantics:
    an exception after a write cannot establish whether that write happened.
    """
    try:
        return _answer(fn, *args)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503,
                            detail='Desktop read unavailable; retry the read.') from None


@router.get("/loops")
def loops():
    return {"loops": _read_answer(desktop_api.loops)}


@router.get("/stats")
def stats(loop: str | None = None, since: str = "7d"):
    return _read_answer(desktop_api.stats_view, loop, since)


@router.get("/now")
def now(loop: str | None = None):
    return _read_answer(desktop_api.now_view, loop)


@router.get("/records")
def records(loop: str | None = None):
    """Read-only bounded local artifacts; deliberately no live CI or authorization probe."""
    return _read_answer(desktop_api.records_view, loop)


@router.get("/overview")
def overview():
    return _read_answer(desktop_api.overview_view)


@router.get("/pr")
def pr(pr: int, loop: str | None = None):
    return _read_answer(desktop_api.pr_view, loop, pr)


@router.get("/health")
def health(request: Request, loop: str | None = None, probe: bool = False):
    client = request.client
    # The whole header mapping goes down, not one value: presence is what refuses, and an empty
    # X-Forwarded-For is still a forwarding header (#648 review, F1).
    return _read_answer(desktop_api.health_view, loop, probe,
                   client.host if client else None, request.headers)


@router.get("/settings")
def settings():
    return _read_answer(desktop_api.settings_view)


@router.get("/hooks")
def hooks(loop: str | None = None):
    return _read_answer(desktop_api.hooks_view, loop)


@router.post("/hooks")
def hooks_action(payload: Any = Body(None)):
    """Only loop/action/opaque confirmation; credentials stay entirely on the host."""
    if not isinstance(payload, dict) or set(payload) != {'loop', 'action', 'confirmation'}:
        raise HTTPException(status_code=422, detail='Expected loop, action and confirmation.')
    return _answer(desktop_api.hooks_action_view, payload['loop'], payload['action'],
                   payload['confirmation'])


@router.get("/actions")
def actions(loop: str | None = None, action: str = '', pr: str | None = None,
            seat: str | None = None):
    # A plain string here lets this boundary return only bounded validation errors rather
    # than FastAPI's raw input-echoing integer diagnostics.
    if pr is not None:
        if not isinstance(pr, str) or not pr.isascii() or not pr.isdigit() or len(pr) > 12:
            raise HTTPException(status_code=422, detail='PR must be a positive integer.')
    number = int(pr) if pr is not None else None
    return _read_answer(desktop_api.action_view, loop, action, number, seat)


@router.post("/actions")
def actions_perform(payload: Any = Body(None)):
    if not isinstance(payload, dict) or set(payload) != {'loop', 'action', 'pr', 'seat', 'confirmation'}:
        raise HTTPException(status_code=422, detail='Expected loop, action, pr, seat and confirmation.')
    return _answer(desktop_api.action_perform_view, payload['loop'], payload['action'],
                   payload['pr'], payload['seat'], payload['confirmation'])


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
