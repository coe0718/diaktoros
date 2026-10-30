"""The checks ``normalize`` runs on a loop file, in order (#1349, split from the core): each step
fills its settings' defaults and refuses what would make a run ambiguous, reading what the steps
before it set, so their order is the order of the refusals. ``normalize`` itself stays in the
core and calls them as ``loop_checks.<name>``. Each step reaches the core through ``config``
(``from .. import config``), so a patch on a core rule reaches it too.
"""
from __future__ import annotations


from .. import config
from ..config import blocks, lists


def _check_lists_and_modes(loop: dict, where: str) -> None:
    """The list settings, ``ingress`` (and its App) and ``merge_check``."""
    loop["fixer_check"] = lists.check_fixer_check(loop["fixer_check"], where)
    loop["required_checks"] = lists.check_required_checks(loop["required_checks"], where)
    loop["human_paths"] = lists.check_human_paths(loop["human_paths"], where)
    loop["generated"] = lists.check_generated(loop["generated"], where)
    loop["ci_coverage"] = lists.check_ci_coverage(loop["ci_coverage"], where)
    if loop.get("ingress") not in config.INGRESS_MODES:
        raise config.ConfigError(f"{where}: 'ingress' must be one of {', '.join(config.INGRESS_MODES)}")
    loop["ingress_app"] = str(loop.get("ingress_app") or "").strip()
    if loop["ingress"] == "app-webhook":
        app = loop["ingress_app"].lower()
        if not config.APP_LOGIN.match(app) or app not in {str(k).lower() for k in loop.get("apps") or {}}:
            raise config.ConfigError(f"{where}: 'ingress' app-webhook needs 'ingress_app': the bot login "
                              "of a GitHub App mapped in 'apps' (its webhook delivers this loop's "
                              "events)")
    if loop.get("merge_check") not in config.MERGE_CHECK_MODES:
        raise config.ConfigError(f"{where}: 'merge_check' must be one of {', '.join(config.MERGE_CHECK_MODES)} "
                          "(auto: on when the reviewer is a GitHub App)")


def _check_identity(loop: dict, where: str) -> None:
    """``repo`` and ``id``, the fixers, reviewers and review-only authors, and the skill's name."""
    repo = str(loop.get("repo") or "").strip()
    if repo.count("/") != 1:
        raise config.ConfigError(f"{where}: 'repo' must be 'owner/name', got {repo!r}")

    loop["id"] = str(loop.get("id") or repo.split("/")[-1]).strip()
    loop["repo"] = repo.lower()
    loop["fixers"] = lists._check_logins(loop.get("fixers"), "fixers", where)
    loop["reviewers"] = lists._check_logins(loop.get("reviewers"), "reviewers", where)
    if not loop["fixers"]:
        raise config.ConfigError(f"{where}: 'fixers' must list at least one GitHub login")
    if not loop["reviewers"]:
        raise config.ConfigError(f"{where}: 'reviewers' must list at least one GitHub login")
    loop["review_only"] = lists.check_review_only(loop.get("review_only"), loop, where)
    # A plugin skill is qualified by the plugin's name, and the plugin was renamed (#425): a loop
    # written before still names the old one, which no longer exists. Read it as the new name.
    if isinstance(loop.get("skill"), str) and loop["skill"].startswith(config.OLD_SKILL_PREFIX):
        loop["skill"] = config.SKILL_PREFIX + loop["skill"][len(config.OLD_SKILL_PREFIX):]


def _check_seats(loop: dict, where: str) -> dict:
    """The reviewer and fixer seats, each with its own route; returns them (``loop['seats']``)."""
    seats = {}
    if not isinstance(loop.get("seats"), dict):
        raise config.ConfigError(f"{where}: 'seats' must be an object with reviewer and fixer")
    for seat in config.SEAT_KEYS:
        if not isinstance(loop["seats"].get(seat) or {}, dict):
            raise config.ConfigError(f"{where}: seats.{seat} must be an object")
        seat_cfg = dict(loop["seats"].get(seat) or {})
        if not seat_cfg.get("route"):
            raise config.ConfigError(f"{where}: seats.{seat}.route is required")
        if not seat_cfg.get("profile"):
            raise config.ConfigError(f"{where}: seats.{seat}.profile is required")
        seat_cfg.setdefault("login", loop["reviewers"][0] if seat == "reviewer"
                            else (loop["fixers"][0] if len(loop["fixers"]) == 1 else ""))
        seat_cfg.setdefault("agent", seat_cfg["profile"].capitalize())
        seats[seat] = seat_cfg
    loop["seats"] = seats
    if str(seats["reviewer"]["route"]) == str(seats["fixer"]["route"]):
        # One route for two seats: one hook would wake (and "arm") both, and the gate script
        # bound to it can only be one of the two. Refused on load, not only at init/apply.
        raise config.ConfigError(f"{where}: seats.reviewer.route and seats.fixer.route are both "
                          f"{seats['reviewer']['route']!r} — each seat needs its own route")
    return seats


def _check_reviewer_and_adjudicator(loop: dict, seats: dict, where: str) -> None:
    """``reviewer_seat``, and the ``adjudicator`` block (a route-less one is dropped)."""
    loop["reviewer_seat"] = str(loop.get("reviewer_seat")
                               or seats["reviewer"].get("login") or "").lower()
    if not loop["reviewer_seat"]:
        raise config.ConfigError(f"{where}: 'reviewer_seat' (or seats.reviewer.login) is required")

    adj = {k: v for k, v in (loop.get("adjudicator") or {}).items()}
    if adj and not adj.get("route") and set(adj) <= {"profile"}:
        # A route-less adjudicator carries no configuration at all — it is exactly what the
        # normalized form writes. Dropping it is what lets our own output be read back; anything
        # with more than a profile and still no route is a real mistake and raises below.
        adj = {}
    if adj and not adj.get("route"):
        raise config.ConfigError(f"{where}: adjudicator.route is required when adjudicator is set")
    if adj:
        adj.setdefault("profile", "default")
    loop["adjudicator"] = adj


def _check_limits(loop: dict, where: str) -> None:
    """``cap``, the review-only limits and ``ci_fix_cap``."""
    loop["cap"] = config._as_int(loop["cap"], "cap", where)
    if loop["cap"] < 2:
        raise config.ConfigError(f"{where}: 'cap' is the number of verdicts allowed; must be >= 2")

    for key in ("review_only_cap", "review_only_daily"):
        loop[key] = lists._check_review_only_limit(loop.get(key), key, where)
    loop["ci_fix_cap"] = lists.check_ci_fix_cap(loop.get("ci_fix_cap"), where)


def _check_tokens(loop: dict, where: str) -> None:
    """``tokens`` (absolute paths), ``apps``, and the named reader, ``read_token``."""
    loop["tokens"] = {k: str(v) for k, v in (loop.get("tokens") or {}).items()}
    for login, token_path in loop["tokens"].items():
        # A relative path would resolve from whatever the current directory is at use time.
        problem = config.relative_path_problem(token_path)
        if problem:
            raise config.ConfigError(f"{where}: tokens.{login}: {problem}")
    loop["apps"] = blocks.check_apps(loop.get("apps"), loop["tokens"], where)
    # The reader is named, never inferred: taking "the first token" would make whichever seat
    # happens to be listed first the account every gate reads GitHub as.
    loop["read_token"] = str(loop.get("read_token") or "").strip()
    if not loop["read_token"]:
        raise config.ConfigError(f"{where}: 'read_token' is not set, and the reader is never inferred "
                          "from 'tokens' — add \"read_token\": \"<login>\" naming the reader's "
                          "own account, with its own entry in 'tokens': `hermes dk set "
                          f"--loop {loop.get('id') or '<id>'} --read-token LOGIN --token "
                          f"LOGIN=/abs/path/to/pat` writes both; {config.FOUR_IDENTITY_RULE}")


def _add_optional_seats(loop: dict, seats: dict, raw_seats: dict, where: str) -> None:
    """The adjudicator, triage and shadow seats, with the ``triage`` and ``shadow`` blocks."""
    adjudicator_seat = blocks._adjudicator_seat(raw_seats.get("adjudicator"), loop, where)
    if adjudicator_seat:
        seats["adjudicator"] = adjudicator_seat
    loop["triage"] = blocks.normalize_triage(loop.get("triage"), loop, where)
    triage_seat = blocks._triage_seat(raw_seats.get("triage"), where)
    if triage_seat:
        seats["triage"] = triage_seat
    loop["shadow"] = blocks.normalize_shadow(loop.get("shadow"), where)
    shadow_seat = blocks._triage_seat(raw_seats.get("shadow"), where, "shadow")
    if shadow_seat:
        seats["shadow"] = shadow_seat


def _check_roots(loop: dict, where: str) -> None:
    """``roots``: none may be a directory cleanup must not delete children of."""
    loop["roots"] = [str(p) for p in (loop.get("roots") or [])]
    for root in loop["roots"]:
        reason = blocks.dangerous_root(root)
        if reason:
            raise config.ConfigError(f"{where}: root {root!r} is {reason}; cleanup deletes PR-named "
                              f"children of every root, so a root must be a dedicated directory")


def _check_run_concurrency(loop: dict, seats: dict, where: str) -> None:
    """``concurrency``, the loop's and each seat's."""
    # `or 1` here would swallow a literal 0 into "serialized", which is the worst kind of silent
    # correction: the operator asked for something invalid and got a loop that looks configured.
    raw_capacity = loop.get("concurrency")
    if raw_capacity is None or raw_capacity == "":
        raw_capacity = 1
    loop["concurrency"] = config._as_int(raw_capacity, "concurrency", where)
    if loop["concurrency"] < 1:
        raise config.ConfigError(f"{where}: 'concurrency' must be >= 1 (1 = serialized)")

    for seat in config.SEAT_KEYS:
        raw = seats[seat].get("concurrency")
        if raw is None or raw == "":
            continue
        seats[seat]["concurrency"] = config._as_int(raw, f"seats.{seat}.concurrency", where)
        if seats[seat]["concurrency"] < 1:
            raise config.ConfigError(f"{where}: seats.{seat}.concurrency must be >= 1 (1 = serialized)")


def _check_turn_limits(loop: dict, seats: dict, where: str) -> None:
    """``turn_budget_s``, and each seat's budget, daily turns and tokens, and step limit."""
    loop["turn_budget_s"] = config._check_budget(
        config.DEFAULT_TURN_BUDGET_S if loop.get("turn_budget_s") in (None, "") else loop["turn_budget_s"],
        "'turn_budget_s'", where)
    for seat in config.SEAT_KEYS:
        if seats[seat].get("turn_budget_s") not in (None, ""):
            seats[seat]["turn_budget_s"] = config._check_budget(
                seats[seat]["turn_budget_s"], f"seats.{seat}.turn_budget_s", where)
        if seats[seat].get("daily_turns") not in (None, ""):
            seats[seat]["daily_turns"] = config._check_daily_turns(
                seats[seat]["daily_turns"], f"seats.{seat}.daily_turns", where)
        if seats[seat].get("daily_tokens") not in (None, ""):
            seats[seat]["daily_tokens"] = config._check_daily_tokens(
                seats[seat]["daily_tokens"], f"seats.{seat}.daily_tokens", where)
        if seats[seat].get("max_steps") not in (None, ""):
            seats[seat]["max_steps"] = config._check_max_steps(
                seats[seat]["max_steps"], f"seats.{seat}.max_steps", where)


def _check_isolation(loop: dict, where: str) -> None:
    """More than one run at once needs isolation: a clone or the isolated worker."""
    if not loop.get("clone") and not config.isolated_runtime_configured():
        # Above one run at once, isolation is not a preference: with no clone to isolate from and
        # no isolated worker to launch the turns, two runs would share whatever checkout they
        # find, which is the wrong-verdict bug. Check the effective value per seat, so a
        # seat-level 2 is caught even when the loop default is 1.
        for seat in config.SEAT_KEYS:
            if config.seat_concurrency(loop, seat) > 1:
                raise config.ConfigError(
                    f"{where}: seats.{seat}.concurrency > 1 requires 'clone' "
                    f"(runs must be isolated), or the isolated worker's runtime file "
                    f"{config.host_path('runtime')} (`hermes dk setup` writes it)")
