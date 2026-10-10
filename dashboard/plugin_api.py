"""Backend routes for the Desktop Diaktoros page (#369), mounted by Hermes at
``/api/plugins/diaktoros/``. Read-only: every route reads the loop config and the run
ledger, and none writes, calls GitHub or reads a token. The logic is ``diaktoros.desktop_api``.
"""
from __future__ import annotations

import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:          # Hermes imports this file by path, not as a package
    sys.path.insert(0, str(_ROOT))

from fastapi import APIRouter, HTTPException  # noqa: E402

from diaktoros import desktop_api  # noqa: E402

router = APIRouter()


def _answer(fn, *args):
    try:
        return fn(*args)
    except desktop_api.NotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
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
