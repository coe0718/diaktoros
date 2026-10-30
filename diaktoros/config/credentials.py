"""Identity and credential checks (#676): the token files the loop is given, the reader account,
and the seats' profiles, verified before anything is written.

Every check reads the core's facts (``config.seat_login``, ``config.profile_dir``, ...) through
the package, so a test that patches one reaches these checks too.
"""
from __future__ import annotations

import os
import stat

from .. import config


def token_file_problem(value) -> str:
    """Why ``value`` cannot be a token file path, or ``""`` when it can.

    Only metadata is inspected — ``stat``, never ``open``: the PAT itself is not read, printed,
    hashed or copied. The rules: ``~`` expands, the result is absolute, it exists, it is a regular
    file owned by you, and group/other cannot read it (0600-style). A symlink is followed, as every
    other token reader in this plugin (``gh.token``, ``doctor``) follows it, and its *target* has
    to pass the same checks.
    """
    raw = str(value or "").strip()
    if not raw:
        return "no path given"
    problem = config.relative_path_problem(raw)
    if problem:
        return problem
    path = config._path(raw)
    try:
        info = path.stat()
    except FileNotFoundError:
        return f"{path} does not exist"
    except OSError as exc:
        return f"{path} cannot be inspected ({exc.strerror or exc})"
    if not stat.S_ISREG(info.st_mode):
        return f"{path} is not a regular file"
    if info.st_uid != os.getuid():
        return f"{path} is owned by uid {info.st_uid}, not you ({os.getuid()})"
    mode = info.st_mode & 0o777
    if mode & 0o077:
        return f"{path} is mode {mode:03o} — group/other can read it (chmod 600 {path})"
    return ""


def check_token_file(value, what: str) -> None:
    """Raise a named ``ConfigError`` when ``value`` is not a usable private token-file path."""
    problem = token_file_problem(value)
    if problem:
        raise config.ConfigError(f"{what}: {problem}")


def verify_adjudicator_token(loop: dict) -> None:
    """The adjudicator comment identity's token file must be a private absolute path.

    Checked when a CLI or settings push *writes* the identity; ``normalize`` already refused a
    missing mapping, a shared file and a login that is also the reader, a seat or an allowlisted
    account. The live ``/user`` principal check stays in the broker, before each comment.
    """
    login = config.adjudicator_login(loop)
    if login:
        check_token_file(config.credentials_map(loop).get(login),
                         f"credential file for the adjudicator login {login!r}")


def verify_token_settings(settings: dict | None, *, loop: dict | None = None) -> None:
    """Check only selected form paths when a loop is known (#1197); metadata, never contents."""
    from .settings_form import select_credentials, settings_defaults
    if loop is not None:
        _tokens, _apps, paths, _ignored = select_credentials(loop, settings)
    else:
        d = settings_defaults(settings)
        keys = [*config.TOKEN_FILE_SETTINGS.values(),
                *(f"{role}_app_key_file" for role in config.TOKEN_FILE_SETTINGS)]
        paths = {key: d[key] for key in keys if str(d.get(key) or "").strip()}
    for key, path in paths.items():
        check_token_file(path, key)


def _same_token_file(first, second) -> bool:
    """Whether two token-file references name one file. Metadata only; the PAT is never read."""
    mine, theirs = config._path(first), config._path(second)
    if os.path.realpath(mine) == os.path.realpath(theirs):
        return True
    try:
        return mine.exists() and theirs.exists() and mine.samefile(theirs)
    except OSError:
        return True                        # cannot tell them apart: treat as shared, never as safe


def case_clash(loop: dict) -> str:
    """Two credential keys that are one login spelled in two cases, or ``""``. GitHub logins
    ignore case and every lookup takes the exact key first, then any case (``util.login_entry``),
    so a map with ``Rev-Bot`` and ``rev-bot`` would let a lookup and a check pick different files
    (#1475 review): it is refused, never resolved."""
    seen = {}
    for table in ("tokens", "apps"):
        for key in loop.get(table) or {}:
            first = seen.setdefault(str(key).casefold(), f"{table}.{key}")
            if first != f"{table}.{key}":
                return (f"{first} and {table}.{key} are one login in two spellings — GitHub logins "
                        "ignore case; keep one")
    return ""


def reader_problem(loop: dict) -> str:
    """Why the reader is not its own identity, or ``""`` when it is (or no reader is named).

    The reader reads every PR state the gates act on; the broker refuses every write when it is
    also a seat, so a loop that installs that way passes init and fails at its first review. The
    offline part of the rule is checked here — distinct logins, distinct token files; distinct
    ``/user`` principals need the network, and ``selftest`` and the broker check those.
    """
    reader = str(loop.get("read_token") or "").strip()
    if not reader:
        return ""
    if clash := case_clash(loop):
        return clash
    others = [(f"{seat} seat", config.seat_login(loop, seat)) for seat in config.SEAT_KEYS]
    others.append(("adjudicator comment login", config.adjudicator_login(loop)))
    for role, login in others:
        if login and login.casefold() == reader.casefold():
            return f"the reader {reader!r} is also the {role}"
    tokens = {str(k).casefold(): v for k, v in config.credentials_map(loop).items()}
    mine = tokens.get(reader.casefold())
    if not mine:
        return ""
    for role, login in others:
        theirs = tokens.get(login.casefold()) if login else ""
        if theirs and _same_token_file(mine, theirs):
            return (f"the reader {reader!r} and the {role} {login!r} read the same token file — "
                    "one account wearing two hats")
    return ""


def reader_fix(loop: dict) -> str:
    """The command that moves a loop's reader onto its own account."""
    return (f"hermes dk set --loop {loop.get('id') or '<id>'} --read-token "
            "<its own login> --token <that login>=/path/to/pat")


def verify_reader(loop: dict) -> None:
    """Refuse a reader that is not its own account, naming the four-identity rule."""
    problem = reader_problem(loop)
    if problem:
        where = loop.get("id") or loop.get("repo") or "<inline>"
        raise config.ConfigError(f"{where}: {problem} — {config.FOUR_IDENTITY_RULE}")


def verify_credentials(loop: dict, roles: set[str] | None = None) -> None:
    """Check the loop's token *references* — never their values.

    Two rules, both learned the hard way. A mapping that names a file which is not there fails at
    the first API call, hours later, in a log nobody is reading; and a loop that maps tokens but
    leaves a seat's login unmapped quietly pushes as whatever ``read_token`` is, which is the wrong
    identity holding write access. Values are only ever read by ``gh.token()`` at use time, from a
    file the operator owns and this CLI does not print.
    """
    where = loop.get("id") or loop.get("repo") or "<inline>"
    if clash := case_clash(loop):
        raise config.ConfigError(f"{where}: {clash}")
    tokens = {str(k).lower(): str(v or "") for k, v in (loop.get("tokens") or {}).items()}
    for login, raw_path in sorted(tokens.items()):
        if not raw_path:
            raise config.ConfigError(f"{where}: tokens.{login} names no file")
        path = config._path(raw_path)
        if path.is_dir():
            raise config.ConfigError(f"{where}: token for {login!r} points at a directory ({path}), "
                              "not the file holding the PAT")
        if not path.is_file():
            raise config.ConfigError(f"{where}: token file for {login!r} is missing ({path}) — the seats "
                              "read that file at use time")
        try:
            empty = not path.read_text().strip()
        except OSError as exc:
            raise config.ConfigError(f"{where}: token file for {login!r} is unreadable ({path}): {exc}") from exc
        if empty:
            raise config.ConfigError(f"{where}: token file for {login!r} is empty ({path}) — an empty PAT "
                              "reads as an unauthenticated call, not as a loud failure")

    # A GitHub App identity (#1163) has a private key, not a PAT: the key file must be there,
    # private and non-empty; it is never read here.
    for login, spec in sorted((loop.get("apps") or {}).items()):
        raw_path = str((spec or {}).get("key_file") or "") if isinstance(spec, dict) else ""
        problem = token_file_problem(raw_path) if raw_path else "no key_file given"
        if not problem and config._path(raw_path).stat().st_size == 0:
            problem = "it is empty"
        if problem:
            raise config.ConfigError(f"{where}: App key for {login!r}: {problem}")
    tokens = {str(k).lower(): v for k, v in config.credentials_map(loop).items()}

    roles = set(roles or ())
    read_token = str(loop.get("read_token") or "").lower()
    if roles and read_token and read_token not in tokens:
        raise config.ConfigError(f"{where}: read_token {read_token!r} has no entry in 'tokens' "
                          "or 'apps' — the gates read GitHub as that login")

    for seat in config.SEAT_KEYS:
        if seat not in roles:
            continue                       # a loop that predates this check keeps loading
        login = config.seat_login(loop, seat).lower()
        if login and login not in tokens:
            raise config.ConfigError(f"{where}: no token mapped for the {seat} login {login!r} — add "
                              f"--token {login}=/path/to/pat (its own PAT file, never the "
                              "reader's)")
    if roles & {"read", "adjudicator", *config.SEAT_KEYS}:
        verify_reader(loop)
    if roles & set(config.SEAT_KEYS):
        # Distinct role credentials: two seats sharing one PAT is one account wearing two hats, and
        # the loop's whole point is that a different account reviews the fixer's work.
        reviewer, fixer = config.seat_login(loop, "reviewer").lower(), config.seat_login(loop, "fixer").lower()
        if reviewer in tokens and fixer in tokens and reviewer != fixer:
            try:
                same_file = config._path(tokens[reviewer]).samefile(config._path(tokens[fixer]))
            except OSError as exc:
                raise config.ConfigError(f"{where}: cannot verify reviewer/fixer token file identity: "
                                  f"{exc}") from exc
            if same_file:
                raise config.ConfigError(f"{where}: the reviewer and the fixer read the same token file "
                                  "— each seat needs its own credential")


def same_profile_home(first: str, second: str) -> bool:
    """Compare actual directory identity, not just names (also catches bind mounts).

    Legacy loops may mention a missing, untouched profile; their existence is checked only when
    a role is rebound. A missing home has no identity to compare, but other stat failures must
    not silently turn a potentially shared home into two independent seats.
    """
    if not first or not second:
        return False
    try:
        return config.profile_dir(first).samefile(config.profile_dir(second))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise config.ConfigError(f"cannot verify profile home identity for {first!r} and {second!r}: "
                          f"{exc}") from exc


def verify_seats(loop: dict, roles: set[str] | None = None) -> None:
    """Refuse a seat mapping that could not drive a run — before any file is written.

    Existence and allowlist membership are checked only for the roles this operation *writes*: a
    loop created before this validation existed may name a profile this machine never had, and
    rewriting its ``cap`` must not be the moment that surfaces. The combination checks (two seats
    on one profile, two seats on one login) always run against the *effective* loop, because the
    unsafe shape is the combination, not either half.

    Order matters for the error message, not for the outcome: profiles first, because a wrong
    profile name is the mistake an operator makes in a form.
    """
    roles = set(roles or ())
    where = loop.get("id") or loop.get("repo") or "<inline>"
    for seat in config.SEAT_KEYS:
        if seat not in roles:
            continue
        profile = config.seat_profile(loop, seat).strip()
        login = config.seat_login(loop, seat).strip().lower()
        if not profile:
            raise config.ConfigError(f"{where}: seats.{seat}.profile is required — the seat cannot run as "
                              "nobody")
        if not config.profile_exists(profile):
            raise config.ConfigError(
                f"{where}: no Hermes profile named {profile!r} for the {seat} seat (looked in "
                f"{config.profiles_root()}) — create the profile, or name one that exists")
        allowlist = [str(x).lower() for x in (loop.get("reviewers") if seat == "reviewer"
                                              else loop.get("fixers")) or []]
        if login and login not in set(allowlist):
            raise config.ConfigError(
                f"{where}: the {seat} login {login!r} is not in this loop's "
                f"{'reviewers' if seat == 'reviewer' else 'fixers'} allowlist "
                f"({', '.join(allowlist) or 'empty'}) — a seat may only act as a login the "
                "repository already trusts")

    reviewer_profile, fixer_profile = config.seat_profile(loop, "reviewer"), config.seat_profile(loop, "fixer")
    reviewer_login, fixer_login = config.seat_login(loop, "reviewer"), config.seat_login(loop, "fixer")
    if reviewer_profile and reviewer_profile == fixer_profile:
        raise config.ConfigError(f"{where}: reviewer and fixer both run as profile {reviewer_profile!r} — "
                          "one seat cannot review its own work")
    if same_profile_home(reviewer_profile, fixer_profile):
        raise config.ConfigError(f"{where}: reviewer and fixer use the same profile home "
                          f"({reviewer_profile!r}, {fixer_profile!r}) — one seat cannot review "
                          "its own work")
    if reviewer_login and reviewer_login.lower() == fixer_login.lower():
        raise config.ConfigError(f"{where}: reviewer and fixer both act as {reviewer_login!r} — the two "
                          "seats must be different accounts")

    adjudicator = loop.get("adjudicator") or {}
    # The adjudicator is judged against the seats whenever a seat *moves* too: pushing the reviewer
    # onto the profile that rules on it is the same mistake from the other direction, and the
    # effective loop is what runs.
    if adjudicator and roles & {"reviewer", "fixer", "adjudicator"}:
        adj_profile = str(adjudicator.get("profile") or "").strip() or "default"
        if "adjudicator" in roles and not adjudicator.get("route"):
            raise config.ConfigError(f"{where}: adjudicator.profile is set but adjudicator.route is not — "
                              "a profile alone cannot wake anyone")
        if "adjudicator" in roles and not config.profile_exists(adj_profile):
            raise config.ConfigError(f"{where}: no Hermes profile named {adj_profile!r} for the "
                              f"adjudicator seat (looked in {config.profiles_root()})")
        if adj_profile in (reviewer_profile, fixer_profile):
            raise config.ConfigError(f"{where}: the adjudicator runs as profile {adj_profile!r}, the same "
                              "as a seat it is meant to rule on")
        for other_profile in (reviewer_profile, fixer_profile):
            if same_profile_home(adj_profile, other_profile):
                raise config.ConfigError(f"{where}: the adjudicator and {other_profile!r} use the same "
                                  "profile home — a seat cannot rule on its own work")

    verify_credentials(loop, roles=roles)
