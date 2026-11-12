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


def _app_unreachable(name: str, login: str, read: str, response: gh.Response) -> doctor.Check | None:
    """UNKNOWN when ``read`` (the App read just made, which yielded nothing usable) got no HTTP
    answer at all (a network outage, DNS, a timeout), or a 2xx answer whose body could not be
    read or did not hold what GitHub sends (a proxy or captive portal answering 200 with HTML,
    #1923), else ``None``. Neither says anything against the key or the installation, so neither
    is blamed: the PAT checks read a silent call the same way (``gh.failure_hint``), not as a
    credential to regenerate. Any other status is GitHub's answer and stays the caller's."""
    if response.status is None and response.data is None and response.error:
        return doctor.Check(name, doctor.UNKNOWN,
                            f"{login}: {read} as the App got no HTTP answer ({gh.one_line(response.error)}) — "
                            "the key and the installation were not read",
                            "check the host reaches api.github.com (network, DNS, proxy) and run doctor again; "
                            "the key file is not implicated")
    if response.status is not None and 200 <= response.status < 300:
        why = gh.one_line(response.error) if response.error else "not the expected JSON"
        return doctor.Check(name, doctor.UNKNOWN,
                            f"{login}: {read} as the App: GitHub answered {response.status} but the body "
                            f"could not be read ({why}) — a proxy, a captive portal, or GitHub returning "
                            "something unexpected; the key and the installation were not judged",
                            "check what answers for api.github.com from this host (proxy, captive portal) "
                            "and run doctor again; the key file is not implicated")
    return None


def check_app_live(loop: dict, login: str, offline: bool) -> doctor.Check:
    """Whether a GitHub App identity works against GitHub (#1181): its key signs as *that* App
    (``GET /app``: the id and the bot login match) and the App is installed on this repository
    (``GET /repos/<repo>/installation``). Both are reads made with the App's own JWT; no token is
    minted and nothing is written. A read with no HTTP answer (the network, not the key) is
    UNKNOWN, as for a PAT, and so is a 2xx whose body could not be read (a proxy or a captive
    portal, #1923); only an error answer from GitHub, or a key that cannot sign, is a MISMATCH."""
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
        silent = _app_unreachable(name, login, "GET /app", app)
        if silent:
            return silent
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
        silent = _app_unreachable(name, login, f"GET /repos/{loop['repo']}/installation", where)
        if silent:
            return silent
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
    env = config.profile_dir(str(loop["seats"][seat].get("profile") or "")) / ".env"
    if "GH_TOKEN" in doctor._env_keys(env):
        return doctor.Check(f"credential:{seat}", doctor.ABSENT,
                            f"nonempty GH_TOKEN in {env}, but no mapped token file for "
                            f"{login or seat}; a profile environment alone does not provide the "
                            "gate's configured GitHub identity, and the sandboxed seat never sees it",
                            f"map a credential for {login or '<login>'}: "
                            f"{credentials.seat_fix(loop, seat)}; then remove GH_TOKEN from that .env")
    who = login or f"the {seat} seat"
    mapped = gh.token_path(loop, login) if login else None
    if mapped is not None:
        return doctor.Check(f"credential:{seat}", doctor.ABSENT,
                            f"{login} → {_token_file_facts(mapped)}: missing or empty",
                            f"write the PAT for {login} to {mapped} (chmod 600)")
    # Seats write only through the host broker with a mapped token file; a GH_TOKEN in the
    # profile's .env is never used, so it is not offered as a fix.
    return doctor.Check(f"credential:{seat}", doctor.ABSENT,
                        f"no token file or GitHub App mapped for {who!r} (a GH_TOKEN in {env} would "
                        "not be used)",
                        f"{credentials.seat_fix(loop, seat)} — without one the seat cannot push or post "
                        "a verdict")


def check_adjudicator_identity(loop: dict) -> doctor.Check | None:
    """The optional identity a ruling is also posted as; ``None`` when the loop has none.

    Without one, rulings go to the operator feed and the host ledger only — that is a valid
    configuration, not a failure, so nothing is reported. With one, it must be a fourth account:
    its own login and its own token file, never the reader's or a seat's. (Distinct *principals*
    need a network read; the broker verifies those before every comment: ``/user`` for a PAT,
    ``/app`` and ``/users/<slug>[bot]`` for a GitHub App.)
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
    # An App adjudicator's credential is its private key, not a token file (#1687, #1675).
    file_kind = "token file"
    if path is None and github_app.is_app(loop, login):
        found = check_app_identity(loop, login, "credential:adjudicator")
        if found.status != doctor.VERIFIED:
            return found
        path = github_app.key_path(loop, login)
        file_kind = "GitHub App key"
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
                                f"{login} reads the same {file_kind} as the {role}",
                                f"give {login} its own GitHub App and key: a shared key is one identity"
                                if file_kind != "token file" else
                                f"give {login} its own PAT file: a shared file is one account")
    return doctor.Check("credential:adjudicator", doctor.VERIFIED,
                        f"{login} → its own {file_kind} {_token_file_facts(path)}; rulings are also posted as a PR comment "
                        "(principal checked by the broker before each comment)")


def check_token(login: str, raw: str) -> doctor.Check:
    """One named credential file. The PAT's *bytes* are never printed, hashed or compared."""
    path = pathlib.Path(str(raw)).expanduser()
    if not path.exists():
        return doctor.Check(f"token:{login}", doctor.ABSENT, f"no file at {path}",
                            f"write the PAT for {login} to {path} (chmod 600)")   # #2256
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
                            credentials.reader_fix(loop))
    if util.login_entry(config.credentials_map(loop), name) is None:
        return doctor.Check("read_token", doctor.MISMATCH, f"read_token names {name!r}, which has no token file",
                            # #2265: `set --token` alone refuses the current reader; with
                            # --read-token naming it, the mapping is the reader's own.
                            f"run {credentials.reader_fix(loop, name)}: the gates read every PR "
                            "state as this login")     # unquoted, as reader_fix's other callers (#2268)
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


def check_main_flake_rerun(loop: dict, offline: bool = False) -> doctor.Check | None:
    """The identity that reruns main's flaky jobs (#1784, ``main_flake_rerun``); ``None`` when
    the setting is off."""
    from .. import main_flake   # here, not module level: main_flake imports the CI readers
    if not config.main_flake_rerun(loop):
        return None
    lid = str(loop.get("id") or "<id>")
    off = f"`hermes dk set --loop {lid} --main-flake-rerun off`"
    login = main_flake.rerun_login(loop)
    if not login:
        return doctor.Check("main-flake-rerun", doctor.MISMATCH,
                            "on, but the loop holds no token for a triage maintainer who is not a "
                            "seat, so no flaky main is ever rerun",
                            f"name your login in triage.maintainers and its token file in tokens, "
                            f"or {off}")
    if not config.required_checks(loop):
        return doctor.Check("main-flake-rerun", doctor.MISMATCH,
                            "on, but the loop names no required_checks: their job logs are what "
                            "tells a flake, so nothing is ever rerun",
                            f"`hermes dk set --loop {lid} --required-check NAME` (repeat it), or {off}")
    if offline:
        return doctor.Check("main-flake-rerun", doctor.UNKNOWN,
                            f"on — {login} reruns main's flaky jobs; its access was not probed "
                            "(--offline)")
    repo = gh.api(loop, f"/repos/{loop['repo']}", login=login)
    perms = repo.get("permissions") if isinstance(repo, dict) else None
    if not isinstance(perms, dict) or perms.get("push") is not True:
        return doctor.Check("main-flake-rerun", doctor.MISMATCH,
                            f"on, but {login}'s token cannot write to {loop['repo']} (or could not "
                            "be read), so a rerun would be refused",
                            f"give {login}'s token write access (a fine-grained token: Actions "
                            f"read and write), or {off}")
    return doctor.Check("main-flake-rerun", doctor.VERIFIED,
                        f"on — a red main that looks like a flake is rerun once as {login} before "
                        "its notice (a fine-grained token also needs Actions: write; a refused "
                        "rerun is said in the main_red notice)")


def check_update_behind(loop: dict, offline: bool = False) -> doctor.Check | None:
    """The host's base merges into behind loop PRs (#1785, ``update_behind``); ``None`` when the
    setting is off. On, the fixer seat must push, and GitHub must report a PR ``behind``."""
    if not config.update_behind(loop):
        return None
    lid = str(loop.get("id") or "<id>")
    off = f"`hermes dk set --loop {lid} --update-behind off`"
    if not config.unattended_fixer_push_enabled(loop):
        return doctor.Check("update-behind", doctor.MISMATCH,
                            "on, but unattended fixer pushes are off, so the host pushes nothing",
                            f"`{config.fixer_push_enable_command(loop)}`, or {off}")
    login = config.seat_login(loop, "fixer")
    if offline:
        return doctor.Check("update-behind", doctor.UNKNOWN,
                            f"on — {login} merges {loop['base']} into behind loop PRs; its access "
                            "and the base's protection were not probed (--offline)")
    repo = gh.api(loop, f"/repos/{loop['repo']}", login=login)
    perms = repo.get("permissions") if isinstance(repo, dict) else None
    if not isinstance(perms, dict) or perms.get("push") is not True:
        return doctor.Check("update-behind", doctor.MISMATCH,
                            f"on, but {login}'s token cannot push to {loop['repo']} (or could not "
                            "be read), so a base merge would be refused",
                            f"give {login} write access to {loop['repo']}, or {off}")
    strict, unread = doctor.base_requires_up_to_date(loop)
    if strict is not True:
        why = (f"{loop['base']} does not require branches to be up to date" if strict is False
               else f"could not read whether {loop['base']} requires up-to-date branches ({unread})")
        return doctor.Check("update-behind", doctor.UNKNOWN,
                            f"on — {login} can push, but {why}: GitHub then never reports a PR "
                            "`behind`, so the host merges nothing",
                            f"require branches to be up to date before merging on {loop['base']} "
                            "(branch protection or a ruleset: 'strict required status checks')")
    return doctor.Check("update-behind", doctor.VERIFIED,
                        f"on — {login} merges {loop['base']} into a behind loop PR when the merge "
                        "is clean; an approved head's approval carries over only when the base's "
                        "new commits touch nothing the PR changed")
