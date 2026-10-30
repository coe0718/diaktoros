"""The identity and credential checks (#1349, split from the doctor core): each seat's GitHub
identity, as a token file or a GitHub App (``check_credential``, ``check_token``,
``check_tokens``, ``check_app_identity``, ``check_app_live``), the reader's own account
(``check_read_token``), the adjudicator's (``check_adjudicator_identity``), and what each PAT's
own response headers say about its expiry and scopes (``check_token_expiry_scopes``). Read-only,
like every doctor check. Each reaches the core through ``doctor`` (``from .. import doctor``);
callers use ``identity_checks.<name>``.
"""
from __future__ import annotations

import pathlib
import time

from .. import config, doctor, gh, util
from ..config import credentials


def _token_file_facts(path: pathlib.Path) -> str:
    """``path (exists: yes, private: no)`` — metadata only; the file is never opened here."""
    exists = path.exists()
    private = exists and not credentials.token_file_problem(str(path))
    return (f"{path} (exists: {'yes' if exists else 'no'}, "
            f"private: {'yes' if private else 'no'})")


def check_app_identity(loop: dict, login: str, name: str) -> doctor.Check:
    """A GitHub App identity's credential (#1163): its private key file exists, is non-empty
    and private, and this host can sign an App JWT. Metadata only: the key is never opened. Its
    live principal and installation are checked by ``selftest`` and by the broker before each
    write, as a PAT's identity is."""
    from .. import github_app
    entry = github_app.entry(loop, login) or {}
    path = github_app.key_path(loop, login)
    app = f"GitHub App {entry.get('app_id')}"
    if path is None:
        return doctor.Check(name, doctor.ABSENT, f"{login}: {app} has no key_file",
                            f"run `hermes dk set --loop {loop['id']} --app {login}=APP_ID:/abs/key.pem` "
                            "(the reader or adjudicator), or `init --app` for a seat")
    if not path.is_file() or path.stat().st_size == 0:
        return doctor.Check(name, doctor.ABSENT, f"{login} → {app}, key {_token_file_facts(path)}: missing or empty",
                            f"save the App's private key (Generate a private key, on the App's page) "
                            f"to {path} and chmod 600 it")
    problem = credentials.token_file_problem(str(path))
    if problem:
        return doctor.Check(name, doctor.MISMATCH, f"{login} → {app}, key {_token_file_facts(path)}: {problem}",
                            f"chmod 600 {path} (and own it): an App key other users can read is the App")
    kind, _tool = github_app.signer()
    if not kind:
        return doctor.Check(name, doctor.ABSENT, f"{login} → {app}: no way to sign an App JWT on this host",
                            "install `cryptography` in the Hermes runtime venv, or the `openssl` command")
    return doctor.Check(name, doctor.VERIFIED, f"{login} → {app}, key {_token_file_facts(path)}, signed with "
                        f"{kind} (the App and its installation: app-live:{login})")


def check_app_live(loop: dict, login: str, offline: bool) -> doctor.Check:
    """Whether a GitHub App identity works against GitHub (#1181): its key signs as *that* App
    (``GET /app``: the id and the bot login match) and the App is installed on this repository
    (``GET /repos/<repo>/installation``). Both are reads made with the App's own JWT; no token is
    minted and nothing is written."""
    from .. import github_app
    name = f"app-live:{login}"
    if offline:
        return doctor.Check(name, doctor.UNKNOWN, "not probed (--offline) — the App and its installation not read")
    entry = github_app.entry(loop, login) or {}
    try:
        app = github_app._as_app(loop, login, "/app")
        where = github_app._as_app(loop, login, f"/repos/{loop['repo']}/installation")
    except github_app.AppError as exc:
        return doctor.Check(name, doctor.MISMATCH, f"{login}: {exc}", "check the key file is this App's private key")
    found = app.data if isinstance(app.data, dict) else {}
    if not found:
        return doctor.Check(name, doctor.MISMATCH, f"{login}: GET /app as the App failed ({app.error or 'no answer'})",
                            "check the key file is the App's private key and the App id is right")
    if found.get("id") != entry.get("app_id"):
        return doctor.Check(name, doctor.MISMATCH, f"{login}: the key signs as App {found.get('id')} "
                            f"({found.get('slug')}), not App {entry.get('app_id')}",
                            "put this App's own id and private key in apps")
    if f"{found.get('slug')}[bot]".casefold() != login.casefold():
        return doctor.Check(name, doctor.MISMATCH, f"{login}: App {entry.get('app_id')} is {found.get('slug')}[bot]",
                            f"name the identity {found.get('slug')}[bot]")
    installation = where.data.get("id") if isinstance(where.data, dict) else None
    if type(installation) is not int:
        return doctor.Check(name, doctor.ABSENT, f"{login}: App {found.get('slug')} is not installed on "
                            f"{loop['repo']} ({where.error or 'no installation'})",
                            f"install the App on {loop['repo']} (its page → Install App → only this "
                            "repository)")
    return doctor.Check(name, doctor.VERIFIED, f"{login} is App {entry.get('app_id')} ({found.get('slug')}), "
                        f"installed on {loop['repo']} (installation {installation})")


def check_credential(loop: dict, seat: str) -> doctor.Check:
    """The gates use gh.token_path, not the profile's GH_TOKEN environment variable."""
    login = str(loop["seats"][seat].get("login") or "")
    tokens = loop.get("tokens") or {}
    from .. import github_app
    if login and gh.token_path(loop, login) is None and github_app.is_app(loop, login):
        return check_app_identity(loop, login, f"credential:{seat}")
    if login:
        path = gh.token_path(loop, login)
        if path and path.is_file() and path.stat().st_size > 0:
            facts = _token_file_facts(path)
            if credentials.token_file_problem(str(path)):
                return doctor.Check(f"credential:{seat}", doctor.MISMATCH,
                                    f"{login} → {facts}: {credentials.token_file_problem(str(path))}",
                                    f"chmod 600 {path} (and own it): a token file other users can read "
                                    "is a shared credential")
            return doctor.Check(f"credential:{seat}", doctor.VERIFIED,
                                f"{login} → {facts}, nonempty (identity and API access not checked)")
    env = doctor.profile_dir(str(loop["seats"][seat].get("profile") or "")) / ".env"
    if "GH_TOKEN" in doctor._env_keys(env):
        return doctor.Check(f"credential:{seat}", doctor.ABSENT,
                            f"nonempty GH_TOKEN in {env}, but no mapped token file for "
                            f"{login or seat}; a profile environment alone does not provide the "
                            "gate's configured GitHub identity, and the sandboxed seat never sees it",
                            f"map a token file for {login or '<login>'}: run `hermes dk set --loop <id> "
                            f"--token {login or '<login>'}=/path/to/pat`, or set {seat}_token_file in the "
                            "plugin settings and run apply; then remove GH_TOKEN from that .env")
    who = login or f"the {seat} seat"
    mapped = gh.token_path(loop, login) if login else None
    if mapped is not None:
        return doctor.Check(f"credential:{seat}", doctor.ABSENT,
                            f"{login} → {_token_file_facts(mapped)}: missing or empty",
                            f"write the PAT for {login} to {mapped} (chmod 600)")
    # Seats write only through the host broker with a mapped token file; a GH_TOKEN in the
    # profile's .env is never used, so it is not offered as a fix.
    return doctor.Check(f"credential:{seat}", doctor.ABSENT,
                        f"no token file mapped for {who!r} (a GH_TOKEN in {env} would not be used)",
                        f"run `hermes dk set --loop <id> --token {login or '<login>'}=/path/to/pat`, or set "
                        f"{seat}_token_file in the plugin settings and run apply — without one the seat "
                        "cannot push or post a verdict")


def check_adjudicator_identity(loop: dict) -> doctor.Check | None:
    """The optional identity a ruling is also posted as; ``None`` when the loop has none.

    Without one, rulings go to the operator feed and the host ledger only — that is a valid
    configuration, not a failure, so nothing is reported. With one, it must be a fourth account:
    its own login and its own token file, never the reader's or a seat's. (Distinct *principals*
    need a network read; the broker verifies those via ``/user`` before every comment.)
    """
    login = config.adjudicator_login(loop)
    if not login:
        return None
    seats = loop.get("seats") or {}
    others = {"read_token": loop.get("read_token"),
              "reviewer": (seats.get("reviewer") or {}).get("login"),
              "fixer": (seats.get("fixer") or {}).get("login")}
    for role, other in others.items():
        if isinstance(other, str) and other and other.casefold() == login.casefold():
            return doctor.Check("credential:adjudicator", doctor.MISMATCH,
                                f"{login} is also the {role} — a ruling would post as that identity",
                                "set seats.adjudicator.login to its own account, or remove it for "
                                "operator-only rulings")
    path = gh.token_path(loop, login)
    from .. import github_app
    if path is None and github_app.is_app(loop, login):
        found = check_app_identity(loop, login, "credential:adjudicator")
        if found.status != doctor.VERIFIED:
            return found
        path = github_app.key_path(loop, login)
    if path is None:
        return doctor.Check("credential:adjudicator", doctor.ABSENT, f"no tokens or apps entry for {login!r}",
                            f"add --token {login}=/path/to/pat (or --app {login}=APP_ID:/abs/key.pem), or "
                            "remove seats.adjudicator.login for operator-only rulings")
    if not path.is_file() or path.stat().st_size == 0:
        return doctor.Check("credential:adjudicator", doctor.ABSENT,
                            f"{login} → {_token_file_facts(path)}: no nonempty token file",
                            f"write the PAT for {login} (chmod 600)")
    if credentials.token_file_problem(str(path)):
        return doctor.Check("credential:adjudicator", doctor.MISMATCH,
                            f"{login} → {_token_file_facts(path)}: "
                            f"{credentials.token_file_problem(str(path))}",
                            f"chmod 600 {path} (and own it): the ruling identity needs a private "
                            "credential")
    for role, other in others.items():
        theirs = gh.credential_path(loop, other) if isinstance(other, str) and other else None
        try:
            shared = theirs is not None and theirs.exists() and path.samefile(theirs)
        except OSError:
            shared = True
        if shared:
            return doctor.Check("credential:adjudicator", doctor.MISMATCH,
                                f"{login} reads the same token file as the {role}",
                                f"give {login} its own PAT file: a shared file is one account")
    return doctor.Check("credential:adjudicator", doctor.VERIFIED,
                        f"{login} → its own token file {_token_file_facts(path)}; rulings are also posted as a PR comment "
                        "(principal checked by the broker before each comment)")


def check_token(login: str, raw: str) -> doctor.Check:
    """One named credential file. The PAT's *bytes* are never printed, hashed or compared."""
    path = pathlib.Path(str(raw)).expanduser()
    if not path.exists():
        return doctor.Check(f"token:{login}", doctor.ABSENT, f"no file at {path}",
                            f"write the PAT for {login} to {path} (chmod 600), or run `hermes "
                            f"dk set --loop <id> --token {login}=<a path that exists>`")
    if not path.is_file():
        return doctor.Check(f"token:{login}", doctor.MISMATCH, f"{path} is not a file",
                            f"point --token {login} at a regular file holding the PAT")
    try:
        body = path.read_text()
    except Exception as exc:
        return doctor.Check(f"token:{login}", doctor.MISMATCH, f"{path} cannot be read ({exc})",
                            f"chmod 600 {path} so the user the gateway runs as can read it")
    if not body.strip():
        return doctor.Check(f"token:{login}", doctor.MISMATCH, f"{path} is empty",
                            f"write the PAT into {path}: an empty credential reads as an anonymous "
                            f"request, which GitHub answers with 404")
    problem = credentials.token_file_problem(str(path))
    if problem:
        return doctor.Check(f"token:{login}", doctor.MISMATCH, problem,
                            f"chmod 600 {path} (and own it): the same rule as every credential line")
    mode = path.stat().st_mode & 0o777
    return doctor.Check(f"token:{login}", doctor.VERIFIED, f"{path} (mode {mode:03o}, non-empty)")


def check_tokens(loop: dict) -> list[doctor.Check]:
    tokens = loop.get("tokens") or {}
    apps = loop.get("apps") or {}
    if not tokens and not apps:
        return [doctor.Check("tokens", doctor.ABSENT, "no token file is named for any login",
                             "run `hermes dk set --loop <id> --token <login>=/path/to/pat` (the gates read GitHub "
                             "through it; without one they go silent)")]
    return ([check_token(login, raw) for login, raw in sorted(tokens.items())]
            + [check_app_identity(loop, login, f"app:{login}") for login in sorted(apps)])


def check_read_token(loop: dict) -> doctor.Check:
    name = str(loop.get("read_token") or "")
    if not name:
        return doctor.Check("read_token", doctor.ABSENT, "no login is named as the reader",
                            "run `hermes dk set --loop <id> --read-token <login> --token <login>=/path/to/pat`")
    if util.login_entry(config.credentials_map(loop), name) is None:
        return doctor.Check("read_token", doctor.MISMATCH, f"read_token names {name!r}, which has no token file",
                            f"run `hermes dk set --loop <id> --token {name}=/path/to/pat` (or --app "
                            f"{name}=APP_ID:/abs/key.pem for a GitHub App reader): the gates read every "
                            "PR state as this login")
    problem = credentials.reader_problem(loop)
    if problem:
        # The broker refuses every write while the reader is a seat, so a loop in this shape
        # installs, "passes", and then never posts a review.
        return doctor.Check("read_token", doctor.MISMATCH, f"{problem} — {config.FOUR_IDENTITY_RULE}",
                            credentials.reader_fix(loop))
    where = "tokens" if util.login_entry(loop.get("tokens"), name) else "apps, a GitHub App"
    return doctor.Check("read_token", doctor.VERIFIED, f"{name} (mapped in {where}; its own account and file)")


EXPIRY_WARN_DAYS = 14


# The classic-token scopes each role needs (any one satisfies it). Fine-grained tokens send no
# ``x-oauth-scopes`` header, so their permissions cannot be read here.
ROLE_SCOPES = {"reviewer": ("repo",), "fixer": ("repo",), "triage": ("repo",),
               "adjudicator": ("repo",), "reader": ("repo", "read:repo_hook")}


def _token_roles(loop: dict) -> dict[str, list[str]]:
    """``{login: [roles]}`` for every identity with a nonempty token file."""
    roles: dict[str, list[str]] = {}
    seats = loop.get("seats") or {}
    named = [(str((seats.get(seat) or {}).get("login") or ""), seat) for seat in ("reviewer", "fixer")]
    named.append((str(loop.get("read_token") or ""), "reader"))
    if config.triage_enabled(loop):
        named.append((config.triage_login(loop) or "", "triage"))
    named.append((config.adjudicator_login(loop) or "", "adjudicator"))
    for login, role in named:
        path = gh.token_path(loop, login) if login else None
        if path is not None and path.is_file() and path.stat().st_size > 0:
            roles.setdefault(login, []).append(role)
    return roles


def check_token_expiry_scopes(loop: dict, offline: bool, now: float | None = None) -> list[doctor.Check]:
    """Per login: days to the token's expiry and the scopes its roles need (read-only GET /user)."""
    now = time.time() if now is None else now
    checks: list[doctor.Check] = []
    # A GitHub App identity (#1163) has neither: its installation tokens are minted per use and
    # its permissions are the App's, set on GitHub. Said, not silently skipped.
    for login in sorted(loop.get("apps") or {}):
        checks.append(doctor.Check(f"token-expiry:{login}", doctor.VERIFIED,
                                   "a GitHub App identity: installation tokens are minted per use, so "
                                   "there is no PAT expiry or scope to read (the App's permissions are "
                                   "set on GitHub)"))
    for login, roles in sorted(_token_roles(loop).items()):
        name = f"token-expiry:{login}"
        if offline:
            checks.append(doctor.Check(name, doctor.UNKNOWN, "not probed (--offline) — expiry and scopes not read"))
            continue
        response = gh.request(loop, "/user", login=login)
        if response.error:
            checks.append(doctor.Check(name, doctor.UNKNOWN,
                                       f"could not read GET /user as {login}: {gh.one_line(response.error)}",
                                       f"check the PAT for {login} (expired, revoked, or no network)"))
            continue
        headers = {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}
        raw = headers.get(gh.TOKEN_EXPIRY_HEADER)
        expires = gh.parse_token_expiry(raw) if raw else None
        fix = (f"rotate the PAT for {login} and rewrite {gh.token_path(loop, login)} "
               "before it lapses")
        if raw and expires is None:
            checks.append(doctor.Check(name, doctor.UNKNOWN, f"unreadable expiry header {raw!r}"))
        elif expires is None:
            checks.append(doctor.Check(name, doctor.VERIFIED, "no expiry reported"))
        else:
            days = (expires - now) / 86400
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expires))
            if days <= 0:
                checks.append(doctor.Check(name, doctor.MISMATCH, f"expired {when}", fix))
            elif days <= EXPIRY_WARN_DAYS:
                checks.append(doctor.Check(name, doctor.UNKNOWN, f"expires {when} — in {days:.1f} day(s)", fix))
            else:
                checks.append(doctor.Check(name, doctor.VERIFIED, f"expires {when} — in {days:.0f} day(s)"))
        scopes = f"token-scopes:{login}"
        if "x-oauth-scopes" not in headers:
            checks.append(doctor.Check(scopes, doctor.SKIPPED,
                                       "no x-oauth-scopes header (fine-grained token): permissions not readable"))
            continue
        have = {x.strip() for x in headers["x-oauth-scopes"].split(",") if x.strip()}
        short = [role for role in roles if not have & set(ROLE_SCOPES[role])]
        if short:
            needs = "; ".join(f"{r}: {' or '.join(ROLE_SCOPES[r])}" for r in short)
            checks.append(doctor.Check(scopes, doctor.MISMATCH,
                                       f"has [{', '.join(sorted(have)) or 'none'}] but needs {needs}",
                                       f"regenerate the PAT for {login} with the missing scope(s)"))
        else:
            checks.append(doctor.Check(scopes, doctor.VERIFIED, f"scopes cover {', '.join(roles)}"))
    return checks
