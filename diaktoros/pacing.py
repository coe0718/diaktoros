"""Subscription-aware pacing (#219): a seat out of its usage window waits instead of failing.

Codex, a Claude subscription and the other OAuth seats share the operator's plan and its usage
window. When an upstream answers 429, the host inference proxy reads *when* the window reopens
(``parse_reset``) and the worker records a hold for that seat's account here. A turn that ended
on it waits until then without spending a retry (``run_supervisor``), and a new turn for the
same account is not launched into a closed window. ``seats.<seat>.daily_turns`` adds an optional
cap per loop and seat, counted here, that holds further turns until local midnight.

One small host file, ``$HERMES_HOME/state/diaktoros-pacing.json``, under a lock. It holds only
an account key (provider, endpoint and profile, never a credential), a time and a short reason,
plus per-day turn counts. A hold or count that cannot be read is treated as absent: pacing never
stops a turn on a guess.
"""

from __future__ import annotations

import contextlib
import email.utils
import fcntl
import json
import math
import re
import time

from . import config, hostdirs

# No provider's window is longer than a week; a reset beyond that is not believed.
MAX_WAIT_S = 7 * 24 * 3600
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?\Z")
_KEEP_DAYS = 3
# The counter key for reviewer turns on review-only PRs (``review_only_daily``), beside the seats'.
REVIEW_ONLY_SEAT = "review_only"


def path():
    return config.host_path("pacing")


def _seconds(text: str) -> float | None:
    """``"120"``, ``"1.5"``, ``"6m0s"``, ``"1h2m3s"``, ``"250ms"`` → seconds."""
    text = text.strip()
    try:
        return float(text)
    except ValueError:
        pass
    match = _DURATION.fullmatch(text)
    if not match or not any(match.groups()):
        return None
    hours, minutes, secs, millis = (float(g) if g else 0.0 for g in match.groups())
    return hours * 3600 + minutes * 60 + secs + millis / 1000


def _instant(text: str, now: float) -> float | None:
    """An absolute reset: epoch seconds (or milliseconds), RFC 3339, or an HTTP date."""
    text = text.strip()
    try:
        value = float(text)
    except ValueError:
        value = None
    if value is not None:
        if value > 1e12:            # epoch milliseconds
            value /= 1000
        return value if value > now - 60 else None
    try:
        from datetime import datetime
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        return parsed.timestamp() if parsed else None
    except (TypeError, ValueError):
        return None


def parse_reset(headers: dict, body: bytes, now: float | None = None) -> float | None:
    """When a 429's usage window reopens (epoch seconds), from what the provider said, or None.

    Reads ``Retry-After`` (seconds or an HTTP date), any ``…-reset…`` rate-limit header (a
    duration like ``6m0s``, seconds, epoch or RFC 3339; Anthropic's ``anthropic-ratelimit-*-reset``,
    OpenAI's ``x-ratelimit-reset-*``), and the JSON body's ``resets_in_seconds`` / ``resets_at``
    (the Codex usage-limit answer). The latest named time wins, bounded by a week.
    """
    now = time.time() if now is None else now
    found: list[float] = []
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    retry_after = lowered.get("retry-after")
    if retry_after:
        seconds = _seconds(retry_after)
        found.append(now + seconds if seconds is not None else (_instant(retry_after, now) or 0))
    for name, value in lowered.items():
        if not _reset_header(name):
            continue
        # A relative value (``6m0s``, ``120``) under an "after"/"in"/OpenAI-style name; an
        # absolute one (epoch, RFC 3339) otherwise, with a relative reading as the fallback.
        relative_first = name.startswith("x-ratelimit-reset") or name.endswith(
            ("-after", "-after-seconds", "-in"))
        seconds = _seconds(value)
        at = _instant(value, now)
        if relative_first and seconds is not None:
            found.append(now + seconds)
        elif at is not None:
            found.append(at)
        elif seconds is not None:
            found.append(now + seconds)
    try:
        data = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        data = None
    error = data.get("error") if isinstance(data, dict) else None
    for source in (error, data):
        if not isinstance(source, dict):
            continue
        seconds = source.get("resets_in_seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0:
            found.append(now + float(seconds))
        at = source.get("resets_at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            found.append(at / 1000 if at > 1e12 else float(at))
        elif isinstance(at, str):
            moment = _instant(at, now)
            if moment is not None:
                found.append(moment)
    found = [moment for moment in found if math.isfinite(moment) and moment > now]
    return min(max(found), now + MAX_WAIT_S) if found else None


def _reset_header(name: str) -> bool:
    """A provider's rate-limit reset header (Anthropic, OpenAI, Codex)."""
    if "reset" not in name:
        return False
    return "ratelimit" in name or "rate-limit" in name or name.startswith("x-codex")


def account_key(provider: str, upstream: str, profile: str) -> str:
    """Which usage window a seat draws on: provider, endpoint and profile — never a key."""
    return f"{provider}|{upstream}|{profile}"


@contextlib.contextmanager
def _locked():
    file = path()
    hostdirs.ensure(file.parent)
    with (file.parent / f".{file.stem}.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield file
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _load(file) -> dict:
    try:
        data = json.loads(file.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(file, data: dict) -> None:
    tmp = file.with_name(file.name + ".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    tmp.replace(file)


def hold(key: str, until: float, reason: str) -> None:
    """Record that ``key``'s window is closed until ``until`` (the later of any two holds)."""
    with _locked() as file:
        data = _load(file)
        holds = data.setdefault("holds", {})
        current = holds.get(key) if isinstance(holds.get(key), dict) else {}
        if not isinstance(current.get("until"), (int, float)) or current["until"] < until:
            holds[key] = {"until": float(until), "reason": str(reason)[:200]}
        _save(file, data)


def held(key: str, now: float | None = None) -> tuple[float, str] | None:
    """``(until, reason)`` while ``key``'s window is closed, else None."""
    now = time.time() if now is None else now
    entry = (_load(path()).get("holds") or {}).get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("until"), (int, float)):
        return None
    return (float(entry["until"]), str(entry.get("reason") or "")) if entry["until"] > now else None


def _today(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def next_midnight(now: float | None = None) -> float:
    now = time.time() if now is None else now
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def turns_today(loop_id: str, seat: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    day = (_load(path()).get("turns") or {}).get(_today(now)) or {}
    value = day.get(f"{loop_id}|{seat}") if isinstance(day, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def count_turn(loop_id: str, seat: str, now: float | None = None) -> int:
    """Count one launched turn for today; returns today's count. Old days are dropped."""
    now = time.time() if now is None else now
    with _locked() as file:
        data = _load(file)
        turns = data.get("turns") if isinstance(data.get("turns"), dict) else {}
        keep = sorted(turns)[-_KEEP_DAYS:]
        turns = {day: turns[day] for day in keep if isinstance(turns[day], dict)}
        day = turns.setdefault(_today(now), {})
        key = f"{loop_id}|{seat}"
        day[key] = (day.get(key) if isinstance(day.get(key), int) else 0) + 1
        data["turns"] = turns
        _save(file, data)
        return day[key]


def rename_loop(old_id: str, new_id: str) -> int:
    """Carry a renamed loop's turn counts to its new id (#425); returns the counts moved."""
    moved = 0
    with _locked() as file:
        data = _load(file)
        turns = data.get("turns") if isinstance(data.get("turns"), dict) else {}
        for day in turns.values():
            if not isinstance(day, dict):
                continue
            for key in [k for k in day if k.startswith(f"{old_id}|")]:
                count = day.pop(key)
                new_key = f"{new_id}|{key[len(old_id) + 1:]}"
                day[new_key] = ((day.get(new_key) if isinstance(day.get(new_key), int) else 0)
                                + (count if isinstance(count, int) else 0))
                moved += 1
        if moved:
            _save(file, data)
    return moved


def when(until: float) -> str:
    """A reset time an operator can read: local clock time, with the date when it is not today."""
    local = time.localtime(until)
    if time.strftime("%Y-%m-%d", local) == time.strftime("%Y-%m-%d"):
        return time.strftime("%H:%M", local)
    return time.strftime("%Y-%m-%d %H:%M", local)
