"""Helpers several command groups share (#676): a loop's routes, its token mapping and credential
lines, and a seat's turn-budget line. Kept apart so a command module never imports ``cli``.
"""
from __future__ import annotations

from . import config, route_intent


def _routes_of(loop: dict) -> dict:
    """role → route name, from the loop's own config: the seats own the names, not this module.

    A hand-edited loop may route its reviewer anywhere; every path that fires, verifies or
    rebinds a route must read that answer rather than re-derive the convention.
    """
    return route_intent.routes_of(loop)


def _credential_lines(loop: dict) -> list[str]:
    """Where each seat's PAT is *referenced*. Never its value: the value lives in a 0600 file the
    seats read at use time, and this CLI has no business copying it into a terminal."""
    tokens = loop.get("tokens") or {}
    lines: list[str] = []
    seen: set[str] = set()
    for role in ("reviewer", "fixer"):
        login = config.seat_login(loop, role)
        if not login or login.lower() in seen:
            continue
        seen.add(login.lower())
        ref = tokens.get(login) or tokens.get(login.lower()) or ""
        lines.append(f"{role} {login} → "
                     f"{ref or '(no file mapped — falls back to read_token)'}")
    read_token = loop.get("read_token") or ""
    if read_token and read_token.lower() not in seen:
        ref = tokens.get(read_token) or tokens.get(read_token.lower()) or ""
        lines.append(f"read {read_token} → {ref or '(no file mapped)'}")
    adj = config.adjudicator_login(loop)
    if adj:
        lines.append(f"adjudicator {adj} → {_token_ref(loop, adj) or '(no file mapped)'}")
    return lines


def _token_ref(loop: dict, login: str) -> str:
    """The token *path* mapped for ``login`` (exact key first, then any case), or ``""``."""
    tokens = loop.get("tokens") or {}
    if not login:
        return ""
    if tokens.get(login):
        return str(tokens[login])
    return next((str(v) for k, v in tokens.items() if str(k).lower() == login.lower() and v), "")


def _budget_line(loop: dict) -> str:
    """Each seat's turn budget, the way status/doctor/set print it."""
    seats = ["reviewer", "fixer"] + (["adjudicator"] if (loop.get("adjudicator") or {}).get("route")
                                     else [])
    return " · ".join(f"{seat} {config.turn_budget(loop, seat)}s" for seat in seats)
