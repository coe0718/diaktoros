"""The nested blocks ``normalize`` checks (#1349, split from the core): the adjudicator and
triage seats, ``triage``, ``shadow``, ``observer`` and ``apps``, the known-key check, and
the dangerous-root rule. Each reaches the core through ``config`` (``from .. import config``),
so a patch on a core rule reaches it too; callers use ``blocks.<name>``.
"""
from __future__ import annotations

import os
import pathlib
import re

from .. import config


def _adjudicator_seat(raw, loop: dict, where: str) -> dict:
    """Validate ``seats.adjudicator`` — only a login (with its own token) and a concurrency.

    The broker re-checks every one of these properties against live credentials before any
    write; this is the early, file-level refusal, so an unsafe shape never loads at all.
    """
    if raw is None or raw == {}:
        return {}
    if not isinstance(raw, dict) or not set(raw) <= {"login", "concurrency", "turn_budget_s",
                                                     "daily_turns", "daily_tokens", "max_steps"}:
        raise config.ConfigError(f"{where}: seats.adjudicator may only hold 'login', 'concurrency', "
                          "'turn_budget_s', 'daily_turns', 'daily_tokens' and 'max_steps'")
    seat: dict = {}
    if raw.get("max_steps") not in (None, ""):
        seat["max_steps"] = config._check_max_steps(raw["max_steps"], "seats.adjudicator.max_steps",
                                             where)
    if raw.get("daily_turns") not in (None, ""):
        seat["daily_turns"] = config._check_daily_turns(raw["daily_turns"],
                                                 "seats.adjudicator.daily_turns", where)
    if raw.get("daily_tokens") not in (None, ""):
        seat["daily_tokens"] = config._check_daily_tokens(raw["daily_tokens"],
                                                   "seats.adjudicator.daily_tokens", where)
    if raw.get("turn_budget_s") not in (None, ""):
        seat["turn_budget_s"] = config._check_budget(raw["turn_budget_s"],
                                              "seats.adjudicator.turn_budget_s", where)
    if raw.get("concurrency") not in (None, ""):
        seat["concurrency"] = config._as_int(raw["concurrency"], "seats.adjudicator.concurrency",
                                      where)
        if seat["concurrency"] < 1:
            raise config.ConfigError(f"{where}: seats.adjudicator.concurrency must be >= 1 (1 = serialized)")
    login = raw.get("login")
    if login in (None, ""):
        return seat
    if not isinstance(login, str) or not login.strip() or login != login.strip():
        raise config.ConfigError(f"{where}: seats.adjudicator.login must be a GitHub login")
    others = {str(x).casefold() for x in (
        loop.get("read_token"), loop.get("reviewer_seat"),
        (loop["seats"].get("reviewer") or {}).get("login"),
        (loop["seats"].get("fixer") or {}).get("login"),
        *loop["reviewers"], *loop["fixers"]) if x}
    if login.casefold() in others:
        raise config.ConfigError(f"{where}: seats.adjudicator.login {login!r} is also the reader, a seat "
                          f"or an allowlisted reviewer/fixer — {config.FOUR_IDENTITY_RULE}")
    tokens = config.credentials_map(loop)          # a PAT or an App key (#1163)
    if not tokens.get(login):
        raise config.ConfigError(f"{where}: seats.adjudicator.login {login!r} has no entry in 'tokens' "
                          "or 'apps' — an adjudicator identity without its own credential cannot "
                          "comment")
    mine = config._path(tokens[login])
    for other, raw_path in tokens.items():
        if other == login or not raw_path:
            continue
        theirs = config._path(raw_path)
        same = os.path.realpath(mine) == os.path.realpath(theirs)
        if not same and mine.exists() and theirs.exists():
            try:
                same = mine.samefile(theirs)
            except OSError as exc:
                raise config.ConfigError(f"{where}: cannot verify adjudicator token file identity: "
                                  f"{exc}") from exc
        if same:
            raise config.ConfigError(f"{where}: the adjudicator and {other!r} read the same token file "
                              f"— {config.FOUR_IDENTITY_RULE}")
    seat["login"] = login
    return seat


def normalize_shadow(raw, where: str) -> dict:
    """The ``shadow`` block (#218): ``profile`` (required to turn it on), ``every`` and
    ``escalate`` (#1004) and ``file_issues`` (#914), or ``{}``.

    A shadow review is a second reviewer model in no-write mode: it never posts, so it needs no
    route, login or token, only the profile its model and account come from. Its limits
    (``daily_turns``, ``daily_tokens``, ``turn_budget_s``, ``concurrency``) live under
    ``seats.shadow``, like every seat's.
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or not set(raw) <= {"profile", "every", "escalate", "file_issues"}:
        raise config.ConfigError(f"{where}: shadow may only hold 'profile', 'every', 'escalate' and "
                          "'file_issues'")
    profile = str(raw.get("profile") or "").strip()
    if not profile:
        raise config.ConfigError(f"{where}: shadow.profile is required (the Hermes profile shadow "
                          "reviews run as); remove the shadow block to turn them off")
    if not config._PROFILE_NAME.fullmatch(profile):
        raise config.ConfigError(f"{where}: shadow.profile {profile!r} is not a Hermes profile name")
    shadow = {"profile": profile}
    if raw.get("every") not in (None, ""):
        every = raw["every"]
        low, high = config.SHADOW_EVERY_RANGE
        if isinstance(every, bool) or not isinstance(every, int) or not low <= every <= high:
            raise config.ConfigError(f"{where}: shadow.every must be a whole number, {low}-{high} "
                              f"(1 = every reviewer turn), got {every!r}")
        shadow["every"] = every
    if raw.get("escalate") not in (None, ""):
        if not isinstance(raw["escalate"], bool):
            raise config.ConfigError(f"{where}: shadow.escalate must be true or false, "
                              f"got {raw['escalate']!r}")
        if raw["escalate"] and shadow.get("every", config.DEFAULT_SHADOW_EVERY) != 1:
            raise config.ConfigError(f"{where}: shadow.escalate needs shadow.every 1: a head the "
                              "sampling skips would have no second review to hold it")
        shadow["escalate"] = raw["escalate"]
    if raw.get("file_issues") not in (None, ""):
        if not isinstance(raw["file_issues"], bool):
            raise config.ConfigError(f"{where}: shadow.file_issues must be true or false, "
                              f"got {raw['file_issues']!r}")
        shadow["file_issues"] = raw["file_issues"]
    return shadow


def normalize_triage(raw, loop: dict, where: str) -> dict:
    """Validate the ``triage`` block (#213), or ``{}`` when the loop does not triage issues.

    Only issues from ``authors`` are ever shown to a model; its only output is labels from
    ``labels`` (at most ``max_labels`` of them) and, when ``comment`` is true, one short comment.
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or not set(raw) <= config.TRIAGE_KEYS:
        raise config.ConfigError(f"{where}: triage may only hold {', '.join(sorted(config.TRIAGE_KEYS))}")
    route = str(raw.get("route") or "").strip()
    profile = str(raw.get("profile") or "").strip()
    if not route or not profile:
        raise config.ConfigError(f"{where}: triage needs a route and a profile (the Hermes profile whose "
                          "model reads the issue)")
    authors = raw.get("authors")
    if (not isinstance(authors, list) or not authors
            or not all(isinstance(a, str) and a.strip() for a in authors)):
        raise config.ConfigError(f"{where}: triage.authors must list the GitHub logins whose issues are "
                          "triaged — anyone else's are ignored, before any model sees them")
    labels = raw.get("labels")
    if (not isinstance(labels, list) or not labels or len(labels) > config.TRIAGE_LABELS_MAX
            or not all(isinstance(x, str) and config.TRIAGE_LABEL.fullmatch(x) and x == x.strip()
                       for x in labels)
            or len({x.casefold() for x in labels}) != len(labels)):
        raise config.ConfigError(f"{where}: triage.labels must list 1-{config.TRIAGE_LABELS_MAX} distinct label "
                          "names (1-50 characters; no commas, braces, backticks or control characters) — the only "
                          "labels triage may apply")
    max_labels = raw.get("max_labels", 3)
    if (isinstance(max_labels, bool) or not isinstance(max_labels, int)
            or not 1 <= max_labels <= config.TRIAGE_MAX_LABELS):
        raise config.ConfigError(f"{where}: triage.max_labels must be 1-{config.TRIAGE_MAX_LABELS}")
    comment = raw.get("comment", False)
    if type(comment) is not bool:
        raise config.ConfigError(f"{where}: triage.comment must be true or false")
    out = {"route": route, "profile": profile,
           "authors": sorted({a.strip().lower() for a in authors}), "labels": list(labels),
           "max_labels": max_labels, "comment": comment}
    login = raw.get("login")
    if login not in (None, ""):
        if not isinstance(login, str) or login != login.strip() or not login:
            raise config.ConfigError(f"{where}: triage.login must be a GitHub login")
        out["login"] = login.lower()
    # The maintainers are trusted for more than issue fixes (#1217): their comments reach both
    # seats (#511), their re-request starts a fresh review (#375), they hold a PR (#477). So they
    # stand on their own, with or without a fix label, and turning fixes off keeps them.
    maintainers = raw.get("maintainers")
    if maintainers not in (None, []):
        if (not isinstance(maintainers, list)
                or not all(isinstance(m, str) and m.strip() for m in maintainers)):
            raise config.ConfigError(f"{where}: triage.maintainers must be a list of GitHub logins")
        out["maintainers"] = sorted({m.strip().lower() for m in maintainers})
    if raw.get("fix_daily_turns") not in (None, ""):
        out["fix_daily_turns"] = config._check_daily_turns(raw["fix_daily_turns"],
                                                    "triage.fix_daily_turns", where)
    fix_label = raw.get("fix_label")
    if fix_label not in (None, ""):
        if (not isinstance(fix_label, str) or not config.TRIAGE_LABEL.fullmatch(fix_label)
                or fix_label != fix_label.strip()):
            raise config.ConfigError(f"{where}: triage.fix_label must be one label name (no commas, "
                              "braces, backticks or control characters)")
        if fix_label.casefold() in {x.casefold() for x in out["labels"]}:
            raise config.ConfigError(f"{where}: triage.fix_label must not be one of triage.labels — the "
                              "triage seat could then hand issues to the fixer itself")
        if not out.get("maintainers"):
            raise config.ConfigError(f"{where}: triage.fix_label needs triage.maintainers: the logins "
                              "whose applying it hands an issue to the fixer")
        out["fix_label"] = fix_label
        auto = raw.get("auto_fix_labels")
        if auto not in (None, []):
            known = {x.casefold(): x for x in out["labels"]}
            if (not isinstance(auto, list) or not all(isinstance(x, str) for x in auto)
                    or any(x.casefold() not in known for x in auto)):
                raise config.ConfigError(f"{where}: triage.auto_fix_labels must list labels from "
                                  "triage.labels — the ones the triage seat applies")
            if any(config.AUTO_FIX_BLOCKED.match(x) for x in auto):
                raise config.ConfigError(f"{where}: triage.auto_fix_labels may not hold P0, P1 or P2: "
                                  "a person decides those")
            out["auto_fix_labels"] = list(dict.fromkeys(known[x.casefold()] for x in auto))
        if raw.get("auto_fix_daily") not in (None, ""):
            if not out.get("auto_fix_labels"):
                raise config.ConfigError(f"{where}: triage.auto_fix_daily needs triage.auto_fix_labels")
            out["auto_fix_daily"] = config._check_daily_turns(raw["auto_fix_daily"],
                                                       "triage.auto_fix_daily", where)
    elif (raw.get("auto_fix_labels") not in (None, [])
          or raw.get("auto_fix_daily") not in (None, "")):
        raise config.ConfigError(f"{where}: triage.auto_fix_labels and auto_fix_daily need "
                          "triage.fix_label — the automatic hand-off is the same path")
    who = out.get("login") or str((loop["seats"].get("reviewer") or {}).get("login") or "").lower()
    if not who:
        raise config.ConfigError(f"{where}: triage needs a login to label as (triage.login, or the "
                          "reviewer seat's)")
    if who == str(loop.get("read_token") or "").lower():
        raise config.ConfigError(f"{where}: triage would label as the reader {who!r} — the reader only "
                          f"reads; {config.FOUR_IDENTITY_RULE}")
    if not {str(k).lower() for k in config.credentials_map(loop)} & {who}:
        raise config.ConfigError(f"{where}: no token mapped for the triage login {who!r} — add --token "
                          f"{who}=/path/to/pat (it needs issues: write on the repository)")
    return out


def check_apps(value, tokens: dict, where: str) -> dict:
    """GitHub App identities (#1163): ``{"<slug>[bot]": {"app_id": N, "key_file": "/abs.pem"}}``.
    The login is the App's bot login; an identity has a PAT (``tokens``) or an App, never both,
    so which credential a seat uses is never a guess."""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise config.ConfigError(f"{where}: 'apps' must be an object of App bot logins")
    out = {}
    pats = {str(k).casefold() for k in tokens}
    for login, spec in value.items():
        name = str(login)
        if not config.APP_LOGIN.match(name.lower()):
            raise config.ConfigError(f"{where}: apps.{name}: an App identity is its bot login, "
                              "'<app-slug>[bot]'")
        if name.casefold() in pats:
            raise config.ConfigError(f"{where}: {name!r} is in both 'tokens' and 'apps' — an identity "
                              "uses one credential")
        if not isinstance(spec, dict) or set(spec) - {"app_id", "key_file"}:
            raise config.ConfigError(f"{where}: apps.{name} must be {{\"app_id\": N, \"key_file\": "
                              "\"/abs/path.pem\"}}")
        app_id = spec.get("app_id")
        if type(app_id) is not int or app_id <= 0:
            raise config.ConfigError(f"{where}: apps.{name}.app_id must be the App's numeric id")
        key = str(spec.get("key_file") or "")
        problem = config.relative_path_problem(key) if key else "no key_file given"
        if problem:
            raise config.ConfigError(f"{where}: apps.{name}.key_file: {problem}")
        out[name] = {"app_id": app_id, "key_file": key}
    return out


def _triage_seat(raw, where: str, name: str = "triage") -> dict:
    """``seats.triage`` (or ``seats.shadow``, #218): its turn budget, daily cap and concurrency —
    no login (see triage.login; a shadow never writes)."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict) or not set(raw) <= {"concurrency", "turn_budget_s", "daily_turns",
                                                     "daily_tokens", "max_steps"}:
        raise config.ConfigError(f"{where}: seats.{name} may only hold 'concurrency', 'turn_budget_s', "
                          "'daily_turns', 'daily_tokens' and 'max_steps'")
    seat: dict = {}
    if raw.get("max_steps") not in (None, ""):
        seat["max_steps"] = config._check_max_steps(raw["max_steps"], f"seats.{name}.max_steps", where)
    if raw.get("daily_turns") not in (None, ""):
        seat["daily_turns"] = config._check_daily_turns(raw["daily_turns"], f"seats.{name}.daily_turns",
                                                 where)
    if raw.get("daily_tokens") not in (None, ""):
        seat["daily_tokens"] = config._check_daily_tokens(raw["daily_tokens"],
                                                   f"seats.{name}.daily_tokens", where)
    if raw.get("turn_budget_s") not in (None, ""):
        seat["turn_budget_s"] = config._check_budget(raw["turn_budget_s"], f"seats.{name}.turn_budget_s",
                                              where)
    if raw.get("concurrency") not in (None, ""):
        seat["concurrency"] = config.check_concurrency(raw["concurrency"], f"seats.{name}.concurrency",
                                                where)
    return seat


def normalize_observer(raw) -> dict:
    """The observer feed's destination, or ``{}`` when this loop has no feed.

    Deliberately lenient where the seats are strict, and for one reason: an observer is
    read-only by construction, so a broken feed must never refuse a loop that can still turn.
    Anything unusable is dropped here and the *reason* is kept under ``misconfigured`` so
    ``hermes dk status`` says it out loud — silence is the failure mode this whole
    plugin exists to kill, and a feed that quietly delivers nothing would be a new one.

    ``events`` narrows the feed; absent or empty means every transition (``observer.EVENTS``).
    ``digest_min`` above zero batches transitions into one compact message flushed by the
    watchdog sweep instead of one notice per transition. ``weekly_report`` (``mon 09:00``, UTC)
    sends the week's report once a week (#658); an unreadable value misconfigures the feed.
    """
    if raw is None or raw == "" or raw == {}:
        return {}
    if not isinstance(raw, dict):
        return {"route": "", "misconfigured": "observer must be a JSON object"}
    route = str(raw.get("route") or "").strip()
    if not route:
        return {"route": "", "misconfigured": "observer.route is required to deliver anything"}
    observer = {"route": route,
                "profile": str(raw.get("profile") or "default").strip() or "default",
                "deliver": str(raw.get("deliver") or "telegram").strip() or "telegram",
                "mute": bool(raw.get("mute"))}
    events = raw.get("events")
    if isinstance(events, str):
        events = re.split(r"[,\s]+", events)
    if isinstance(events, (list, tuple)):
        wanted = sorted({str(e).strip().lower() for e in events if str(e).strip()})
        if wanted:
            observer["events"] = wanted
    try:
        digest = int(raw.get("digest_min") or 0)
    except (TypeError, ValueError):
        digest = 0
    if digest > 0:
        observer["digest_min"] = digest
    weekly = raw.get("weekly_report")
    if weekly not in (None, ""):
        try:
            observer["weekly_report"] = config.check_weekly_report(weekly)
        except ValueError as exc:
            return {**observer, "misconfigured": f"observer.weekly_report: {exc}"}
    # A second destination for urgent notices (observer.URGENT_EVENTS): its own delivery-only
    # route, optionally its own profile and platform. Unset: one feed gets everything.
    urgent = str(raw.get("urgent_route") or "").strip()
    if urgent:
        if urgent == route:
            return {**observer, "misconfigured": "observer.urgent_route must differ from observer.route"}
        observer["urgent_route"] = urgent
        for key in ("urgent_profile", "urgent_deliver"):
            value = str(raw.get(key) or "").strip()
            if value:
                observer[key] = value
    return observer


def dangerous_root(value: str) -> str:
    """Why a cleanup root is too broad to accept, or ``""`` when it is fine.

    Cleanup removes PR-named children of every root. ``/``, the home directory and anything
    above it hold the operator's own projects and dotfiles, which are never a loop's to delete,
    however they happen to be named.
    """
    path = config._path(value).resolve()
    home_dir = pathlib.Path.home().resolve()
    if path == pathlib.Path(path.anchor):
        return "the filesystem root"
    if path == home_dir:
        return "the home directory"
    if path in home_dir.parents:
        return "an ancestor of the home directory"
    return ""


# Keys a loop file may hold besides DEFAULTS' (identity and the state ``disable`` keeps).
LOOP_EXTRA_KEYS = frozenset({"id", "repo", "state_dir", "observer_disabled"})


# Every name ``normalize`` reads under ``seats``: the two working seats plus the optional
# adjudicator, triage and shadow seats. Anything else there is dropped on load, so it is refused.
FILE_SEAT_NAMES = frozenset({*config.SEAT_KEYS, "adjudicator", "triage", "shadow"})


# Keys of a reviewer/fixer seat.
SEAT_FILE_KEYS = frozenset({"route", "profile", "login", "agent", "concurrency", "turn_budget_s",
                            "daily_turns", "daily_tokens", "max_steps"})


def _unknown_keys(keys, known, prefix: str = "") -> list:
    import difflib
    out = []
    for key in keys:
        if key not in known:
            near = difflib.get_close_matches(str(key).strip().replace("-", "_"), sorted(known), 1)
            out.append(f"{prefix}{key!r}" + (f" (did you mean {near[0]!r}?)" if near else ""))
    return out


def check_known_keys(raw: dict, where: str) -> None:
    """Refuse a loop file key nothing reads: a typo'd setting would load and never apply."""
    problems = _unknown_keys(raw, set(config.DEFAULTS) | LOOP_EXTRA_KEYS)
    seats = raw.get("seats")
    if isinstance(seats, dict):
        problems += _unknown_keys(seats, FILE_SEAT_NAMES, "seats.")
        for seat in config.SEAT_KEYS:
            if isinstance(seats.get(seat), dict):
                problems += _unknown_keys(seats[seat], SEAT_FILE_KEYS, f"seats.{seat}.")
    if problems:
        raise config.ConfigError(f"{where}: unknown loop file key(s), which would load and never take "
                          f"effect: {', '.join(problems)}")
