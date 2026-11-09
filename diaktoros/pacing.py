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

A provider that is *unavailable* without ever saying 429 (the DirectSDK subscription path
answers a closed window with empty streams: no status, no ``Retry-After``) is held the same way
(``hold_unavailable``): a short window that doubles on each repeat, cleared by the next turn that
succeeds on the account (``clear_unavailable``). The signature is read from the failed turn's
own output, so it is only as trustworthy as that output: a turn that prints it and exits
non-zero holds its account for one window, never longer than ``UNAVAILABLE_MAX_S``.
"""

from __future__ import annotations

import contextlib
import email.utils
import fcntl
import json
import math
import re
import time

from . import config, hostdirs, util

# No provider's window is longer than a week; a reset beyond that is not believed.
MAX_WAIT_S = 7 * 24 * 3600
_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?\Z")
_KEEP_DAYS = 3
# The counter key for reviewer turns on review-only PRs (``review_only_daily``), beside the seats'.
REVIEW_ONLY_SEAT = "review_only"
# A provider-unavailable hold: the first window, its cap, and how long a strike is remembered.
UNAVAILABLE = "provider unavailable"
# The words that mark a closed account's ``held:`` line (#2220): ``hold_line`` writes them, and
# ``closed_account`` (status, explain, the held notice) and ``retry --clear-hold``'s SQL
# (``closed_account_like``) match by them, so a rewording here moves all three together.
USAGE_WINDOW = "usage window"
CLOSED_ACCOUNT_MARKS = (f" {UNAVAILABLE}", f" {USAGE_WINDOW} (")
UNAVAILABLE_BASE_S = 15 * 60
UNAVAILABLE_MAX_S = 2 * 3600
_UNAVAILABLE_FORGET_S = 6 * 3600
# A turn the provider killed (#1155), as the inference proxy records it: an HTTP error status, or
# an upstream it could not reach. ``proxy refused: …`` is the proxy's own policy, never this.
_PROVIDER_FAILURE = re.compile(r"(?:HTTP [45]\d\d|upstream unreachable): ")
_UNAVAILABLE_SIGNS = (
    (re.compile(r"empty stream with no finish_reason", re.I), "empty stream with no finish_reason"),
    (re.compile(r"didn.t answer after \d+ attempts", re.I), "wire didn't answer"),
)


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
    if (at := util.iso_epoch(text)) is not None:
        return at
    try:
        parsed = email.utils.parsedate_to_datetime(text)
        return parsed.timestamp() if parsed else None
    except (TypeError, ValueError):
        return None


def parse_reset(headers: dict, body: bytes, now: float | None = None) -> float | None:
    """When a 429's usage window reopens (epoch seconds), from what the provider said, or None.

    Reads the JSON body's ``resets_in_seconds`` / ``resets_at`` (the Codex usage-limit answer),
    ``Retry-After`` (seconds or an HTTP date), and any ``…-reset…`` rate-limit header (a duration
    like ``6m0s``, seconds, epoch or RFC 3339; Anthropic's ``anthropic-ratelimit-*-reset``,
    OpenAI's ``x-ratelimit-reset-*``, Codex's ``x-codex-*-reset-*``). Bounded by a week.

    Providers report every window they track, not only the one that is full: Codex sends its
    5-hour and its weekly reset on every answer. So the time is the hit limit's, by authority:
    the error body's own reset first (it describes the limit that refused), then ``Retry-After``,
    then the reset of a window the headers show exhausted (``…-used-percent`` at 100, or
    ``…-remaining`` at 0). With no window shown exhausted, the earliest reset: a wait that is too
    short ends in one more 429 and a new hold, one that is too long silences a seat for days
    (live, 2026-10-10: a full 5-hour window held the reviewer until the weekly reset).
    """
    now = time.time() if now is None else now

    def kept(moments):
        return [m for m in moments if math.isfinite(m) and m > now]

    def bounded(moment):
        return min(moment, now + MAX_WAIT_S)

    body_found = kept(_body_resets(body, now))
    if body_found:
        return bounded(max(body_found))
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    retry_after = lowered.get("retry-after")
    if retry_after:
        seconds = _seconds(retry_after)
        moment = kept([now + seconds if seconds is not None else (_instant(retry_after, now) or 0)])
        if moment:
            return bounded(moment[0])
    windows = []                                   # (reset, exhausted: True / False / None)
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
            moment = now + seconds
        elif at is not None:
            moment = at
        elif seconds is not None:
            moment = now + seconds
        else:
            continue
        if kept([moment]):
            windows.append((moment, _exhausted(name, lowered)))
    full = [moment for moment, exhausted in windows if exhausted is True]
    if full:
        return bounded(max(full))
    unknown = [moment for moment, exhausted in windows if exhausted is None]
    pool = unknown or [moment for moment, _ in windows]
    return bounded(min(pool)) if pool else None


def _body_resets(body: bytes, now: float) -> list[float]:
    try:
        data = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        return []
    found: list[float] = []
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
    return found


def _number(text: str | None) -> float | None:
    try:
        value = float(str(text).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _exhausted(reset_name: str, headers: dict) -> bool | None:
    """Whether the window a reset header belongs to is full, from its sibling usage header:
    ``x-codex-primary-used-percent`` for ``x-codex-primary-reset-…``, ``…-remaining`` for an
    Anthropic ``…-reset``, ``x-ratelimit-remaining-X`` for OpenAI's ``x-ratelimit-reset-X``.
    None when the provider gives no usage for that window."""
    if reset_name.startswith("x-ratelimit-reset-"):
        siblings = {"remaining": "x-ratelimit-remaining-" + reset_name[len("x-ratelimit-reset-"):]}
    else:
        base = reset_name[:reset_name.index("-reset")] if "-reset" in reset_name else ""
        if not base:
            return None
        siblings = {"used": base + "-used-percent", "remaining": base + "-remaining"}
    used = _number(headers.get(siblings.get("used", "")))
    if used is not None:
        return used >= 100
    remaining = _number(headers.get(siblings["remaining"]))
    if remaining is not None:
        return remaining <= 0
    return None


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
    util.atomic_write(file, json.dumps(data, sort_keys=True))


def hold(key: str, until: float, reason: str) -> None:
    """Record that ``key``'s window is closed until ``until`` (the later of any two holds)."""
    with _locked() as file:
        data = _load(file)
        holds = data.setdefault("holds", {})
        current = holds.get(key) if isinstance(holds.get(key), dict) else {}
        if not isinstance(current.get("until"), (int, float)) or current["until"] < until:
            holds[key] = {"until": float(until), "reason": str(reason)[:200]}
        elif str(current.get("reason") or "").startswith(UNAVAILABLE) and until > time.time():
            # A provider's 429 reset that ends before our own unavailable window (#1325): keep
            # its deadline beside the entry, so clearing the unavailable hold cannot lose it.
            later = current.get("limited")
            if not isinstance(later, (int, float)) or later < until:
                current["limited"], current["limited_reason"] = float(until), str(reason)[:200]
        _save(file, data)


def held(key: str, now: float | None = None) -> tuple[float, str] | None:
    """``(until, reason)`` while ``key``'s window is closed, else None."""
    now = time.time() if now is None else now
    entry = (_load(path()).get("holds") or {}).get(key)
    if not isinstance(entry, dict) or not isinstance(entry.get("until"), (int, float)):
        return None
    return (float(entry["until"]), str(entry.get("reason") or "")) if entry["until"] > now else None


def unavailable_signature(*outputs: object) -> str | None:
    """What a failed turn's output says about an unavailable provider (the DirectSDK "didn't
    answer after N attempts" or "empty stream with no finish_reason"), or None."""
    text = "\n".join(str(out or "") for out in outputs)
    for pattern, detail in _UNAVAILABLE_SIGNS:
        if pattern.search(text):
            return detail
    return None


def hold_unavailable(key: str, detail: str, now: float | None = None) -> float:
    """Hold ``key`` because its provider is unavailable with no 429 to say when it is back;
    returns when the hold ends.

    The window is ``UNAVAILABLE_BASE_S``, doubled for each earlier strike (to
    ``UNAVAILABLE_MAX_S``); strikes older than ``_UNAVAILABLE_FORGET_S`` are forgotten. A failure
    while an unavailable hold is still live comes from a turn that launched before it, so it is
    no new strike: the window stays as it is. A live 429 hold is never shortened: an outage seen
    during a shorter one is still a strike and takes over the hold (the 429's deadline is kept
    beside it); during a longer one the 429 stands. The outage window of a strike is kept in the
    strike itself, so failures of turns already in flight while a longer 429 is displayed
    coalesce into that one strike too, whichever reason the hold shows.
    """
    now = time.time() if now is None else now
    with _locked() as file:
        data = _load(file)
        holds = data.setdefault("holds", {})
        strikes = data.setdefault("unavailable", {})
        current = holds.get(key) if isinstance(holds.get(key), dict) else {}
        live = isinstance(current.get("until"), (int, float)) and current["until"] > now
        if live and str(current.get("reason") or "").startswith(UNAVAILABLE):
            return float(current["until"])
        prev = strikes.get(key) if isinstance(strikes.get(key), dict) else {}
        if live and isinstance(prev.get("window"), (int, float)) and prev["window"] > now:
            return float(current["until"])      # in flight before this strike's window ended
        recent = isinstance(prev.get("last"), (int, float)) and now - prev["last"] < _UNAVAILABLE_FORGET_S
        count = (int(prev.get("count") or 0) if recent else 0) + 1
        until = now + min(UNAVAILABLE_BASE_S * 2 ** (count - 1), UNAVAILABLE_MAX_S)
        strikes[key] = {"count": count, "last": now, "window": until}
        if live and current["until"] >= until:
            # A 429 hold already outlasts our window: it stands, the outage is still a strike.
            until = float(current["until"])
        else:
            holds[key] = {"until": until, "reason": f"{UNAVAILABLE} ({detail})"[:200]}
            if live:
                # A shorter 429 deadline is kept beside the entry, so it survives a clear.
                holds[key]["limited"] = float(current["until"])
                holds[key]["limited_reason"] = str(current.get("reason") or "")[:200]
        _save(file, data)
    return until


def provider_failure(error: object) -> bool:
    """Whether a failed turn's last provider error (the proxy's ``last_error``) is the
    provider's own: an HTTP error status or an unreachable upstream (#1155)."""
    return bool(_PROVIDER_FAILURE.match(str(error or "")))


def count_provider_failure(key: str, error: str, limit: int,
                           now: float | None = None) -> float | None:
    """A turn on ``key`` died on a provider error (#1155): count it, and at ``limit`` in a row
    hold the account as an outage does (``hold_unavailable``). Returns when that hold ends, or
    None while the count is under ``limit``.

    The count stays at ``limit`` once reached, so the first provider failure after the hold
    lapses holds again at once, for a doubled window: that turn was the probe. A success
    (``clear_unavailable``) or any other failure (``reset_provider_failures``) ends the run, and
    a count untouched for ``_UNAVAILABLE_FORGET_S`` starts over.
    """
    now = time.time() if now is None else now
    with _locked() as file:
        data = _load(file)
        counts = data.setdefault("provider_failures", {})
        prev = counts.get(key) if isinstance(counts.get(key), dict) else {}
        recent = isinstance(prev.get("last"), (int, float)) and now - prev["last"] < _UNAVAILABLE_FORGET_S
        count = min((int(prev.get("count") or 0) if recent else 0) + 1, limit)
        counts[key] = {"count": count, "last": now}
        _save(file, data)
    if count < limit:
        return None
    return hold_unavailable(key, f"{limit} turns in a row: {error}", now)


def reset_provider_failures(key: str) -> bool:
    """A turn on ``key`` failed for another reason (#1155): the provider answered, so a run of
    provider failures is over. True if one was counted."""
    if key not in (_load(path()).get("provider_failures") or {}):
        return False                        # the usual case: no lock, no write
    with _locked() as file:
        data = _load(file)
        data.get("provider_failures", {}).pop(key, None)
        _save(file, data)
    return True


def clear_holds(profiles: set[str], now: float | None = None) -> list[str]:
    """``retry --clear-hold`` (#1155): drop every hold on an account of these seat profiles,
    a provider's own 429 window too (a turn that meets it again holds it again), with the
    outage strikes and provider failures counted there. Returns the accounts whose hold was
    still closed."""
    now = time.time() if now is None else now
    cleared = []
    with _locked() as file:
        data = _load(file)
        for section in ("holds", "unavailable", "provider_failures"):
            table = data.get(section) if isinstance(data.get(section), dict) else {}
            for key in [key for key in table if key.rsplit("|", 1)[-1] in profiles]:
                entry = table.pop(key)
                if (section == "holds" and isinstance(entry, dict)
                        and isinstance(entry.get("until"), (int, float)) and entry["until"] > now):
                    cleared.append(key)
        _save(file, data)
    return sorted(cleared)


def clear_unavailable(key: str) -> bool:
    """A turn succeeded on ``key``: its provider answers. Drop a provider-unavailable hold, the
    strikes behind it and any provider failures counted (#1155) (a 429 hold is the provider's
    own word and stays); True if any went."""
    data = _load(path())
    if not any(key in (data.get(section) or {}) for section in ("unavailable", "provider_failures")):
        return False                        # the usual case: no lock, no write
    with _locked() as file:
        data = _load(file)
        data.get("unavailable", {}).pop(key, None)
        data.get("provider_failures", {}).pop(key, None)
        holds = data.get("holds") if isinstance(data.get("holds"), dict) else {}
        entry = holds.get(key)
        if isinstance(entry, dict) and str(entry.get("reason") or "").startswith(UNAVAILABLE):
            limited = entry.get("limited")
            if isinstance(limited, (int, float)) and limited > time.time():
                # A 429's own reset, shorter than the window being cleared, still stands.
                holds[key] = {"until": float(limited),
                              "reason": str(entry.get("limited_reason") or "")}
            else:
                del holds[key]
        _save(file, data)
    return True


def hold_line(seat: str, provider: str, until: float, reason: str = "") -> str:
    """A held run's ``held:`` line for a closed account: "provider unavailable (…)" when that is
    why, else the usage window. Every path that holds an account for a run words it here."""
    what = reason if reason.startswith(UNAVAILABLE) else f"{USAGE_WINDOW} ({provider})"
    return f"held: {seat} {what} — resumes {when(until)}"


def closed_account(error: str) -> bool:
    """Whether a run's ``held:`` line is ``hold_line``'s for a closed account (a usage window or
    an unavailable provider), which ``retry --clear-hold`` reopens; a daily cap's is not."""
    return error.startswith("held: ") and any(mark in error for mark in CLOSED_ACCOUNT_MARKS)


def closed_account_like() -> tuple[str, ...]:
    """The SQL ``LIKE`` patterns of ``closed_account``, one per mark, for the run ledger."""
    return tuple(f"held: %{mark}%" for mark in CLOSED_ACCOUNT_MARKS)


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


class CapReached(Exception):
    """``count_turn`` found today's pool already at its ``cap``: nothing was counted."""

    def __init__(self, seat: str, cap: int):
        super().__init__(f"{seat} daily turn cap ({cap}) reached")
        self.seat, self.cap = seat, cap


def count_turn(loop_id: str, seat: str, now: float | None = None, cap: int | None = None) -> int:
    """Count one launched turn for today; returns today's count. Old days are dropped.

    With ``cap``, the check and the count are one step under the file lock: a pool already at
    ``cap`` raises :class:`CapReached` and counts nothing. A turn is counted when its sandbox
    starts (#1378), so two turns admitted together cannot both take the last slot."""
    now = time.time() if now is None else now
    with _locked() as file:
        data = _load(file)
        turns = data.get("turns") if isinstance(data.get("turns"), dict) else {}
        keep = sorted(turns)[-_KEEP_DAYS:]
        turns = {day: turns[day] for day in keep if isinstance(turns[day], dict)}
        day = turns.setdefault(_today(now), {})
        key = f"{loop_id}|{seat}"
        if cap is not None and isinstance(day.get(key), int) and day[key] >= cap:
            raise CapReached(seat, cap)
        day[key] = (day.get(key) if isinstance(day.get(key), int) else 0) + 1
        data["turns"] = turns
        _save(file, data)
        return day[key]


def tokens_today(loop_id: str, seat: str, now: float | None = None) -> int:
    """Tokens (in + out) the seat's finished turns spent today on this loop (#480)."""
    now = time.time() if now is None else now
    day = (_load(path()).get("tokens") or {}).get(_today(now)) or {}
    value = day.get(f"{loop_id}|{seat}") if isinstance(day, dict) else 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def count_tokens(loop_id: str, seat: str, tokens: int, now: float | None = None) -> int:
    """Add ``tokens`` to today's spend for the seat; returns today's total. Old days dropped."""
    now = time.time() if now is None else now
    with _locked() as file:
        data = _load(file)
        days = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
        keep = sorted(days)[-_KEEP_DAYS:]
        days = {day: days[day] for day in keep if isinstance(days[day], dict)}
        day = days.setdefault(_today(now), {})
        key = f"{loop_id}|{seat}"
        day[key] = (day.get(key) if isinstance(day.get(key), int) else 0) + max(0, int(tokens))
        data["tokens"] = days
        _save(file, data)
        return day[key]


def seats(loop: dict, now: float | None = None) -> dict:
    """What this loop's seats have spent today and which of their windows are closed (#219, #650).

    The live read: today's turns and tokens per seat against their caps, and every hold that
    belongs to one of this loop's seat profiles. A run's own ``held: ...`` error is a snapshot
    from the moment it was held, so it can still name a reset time that has long passed — this
    is the answer that expires with the window.

    ``review_only`` appears only for a loop that has review-only authors: a reviewer turn on one
    of their PRs is counted there (``run_supervisor``) once ``review_only_daily`` is set, capped by
    it, and the reviewer's own counter does not see it (#772). Unset, the one pool is the reviewer's.
    """
    now = time.time() if now is None else now
    names = ["reviewer", "fixer"]
    if config.review_only(loop):
        names.append(REVIEW_ONLY_SEAT)
    per_seat = {seat: {"turns": turns_today(loop["id"], seat, now),
                       "turns_cap": (config.review_only_daily(loop) if seat == REVIEW_ONLY_SEAT
                                     else config.seat_daily_turns(loop, seat)),
                       "tokens": tokens_today(loop["id"], seat, now),
                       "tokens_cap": config.seat_daily_tokens(loop, seat)}
                for seat in names}
    # The triage seat is paced on the same path (the supervisor holds its account on a 429), so
    # its profile belongs here too (#806); a loop without triage has none (""). A shadow review
    # turn (#218) is paced the same way under its own seat, so the shadow profile does too (#963).
    profiles = ({config.seat_profile(loop, seat) for seat in ("reviewer", "fixer", "triage", "shadow")}
                | {str((loop.get("adjudicator") or {}).get("profile") or "")}) - {""}
    holds = []
    for key, entry in sorted((_load(path()).get("holds") or {}).items()):
        until = entry.get("until") if isinstance(entry, dict) else None
        if not isinstance(until, (int, float)) or until <= now:
            continue                        # a window that has reopened is not news
        provider, profile = key.split("|", 1)[0], key.rsplit("|", 1)[-1]
        if profile not in profiles:
            continue                        # another loop's, or another profile's, window
        holds.append({"provider": provider, "profile": profile, "until": float(until),
                      "reason": str(entry.get("reason") or "usage window")})
    return {"now": now, "seats": per_seat, "holds": holds}


def rename_loop(old_id: str, new_id: str) -> int:
    """Carry a renamed loop's turn and token counts to its new id (#425, #764); returns the counts moved.

    Both families share ``loop_id|seat`` keys and move in one locked write; totals already
    under the new id are added to, and a re-run finds nothing left to move.
    """
    moved = 0
    with _locked() as file:
        data = _load(file)
        for family in ("turns", "tokens"):
            days = data.get(family) if isinstance(data.get(family), dict) else {}
            for day in days.values():
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
