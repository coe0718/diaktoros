"""Checks and arming (#676): ``doctor``, ``selftest``, ``corpus``, and ``arm``, which switches a
loop's GitHub hooks on or off and then verifies what it changed.

``cli`` re-exports every command here. The webhook plumbing ``arm`` shares with ``set`` and
``uninstall`` is ``cli_common``'s, called as ``cli_common.<name>``.
"""
from __future__ import annotations

from . import cli_common, config, doctor, gate_shims, gh, route_intent


def _hook_write_fix(token_login: str | None, transient: bool = False,
                    loop: dict | None = None) -> str:
    """What to do when a hook read or write failed with the token ``arm`` used.

    ``transient`` is for failures that were neither a refusal nor a write that did not stick (a
    timeout, a 5xx): those are worth a retry before blaming the token.
    """
    if transient:
        return ("retry `arm`; if it fails again, check the repo's webhooks on GitHub — "
                + _hook_write_fix(token_login, loop=loop))
    return cli_common.hook_write_need(loop or {}, token_login)


def _set_hooks(loop: dict, active: bool, token_login: str | None) -> tuple[list[str], bool]:
    """Flip this loop's repo hooks, then read each one back and report what GitHub now shows.

    Returns ``(lines, ok)``. ``ok`` is true only when every loop hook was observed in the asked-for
    state: a refused PATCH, a read-back that disagrees or cannot be made, an unreadable listing,
    a repo with no loop hooks, and a repo missing either seat's hook all make it false — ``arm``
    must never say "paused" about a hook GitHub still delivers to, nor "armed" about a loop that
    only one seat can hear.
    """
    names = cli_common._routes_of(loop)
    seats = [(role, names[role]) for role in config.hook_roles(loop) if names.get(role)]
    wanted = tuple(name for _, name in seats)
    word = "active" if active else "paused"
    login = token_login or loop.get("read_token")
    try:
        hooks = cli_common._hook_listing(loop, token_login, require_active=False)
    except config.ConfigError as exc:
        return [f"could not read the repo's hooks: {exc}",
                f"fix: {_hook_write_fix(token_login, loop=loop)}"], False
    try:
        # One matcher (doctor.split_route_hooks). Arming credits a hook only at the registry's
        # route URL bound to the seat's profile: another profile's URL (the gateway answers it
        # 404), another path, a retired gateway — or a route named inside another route's name —
        # is not that seat's hook. Pausing asks the ownership question instead: every hook this
        # install made is stopped, even once `uninstall` has removed the route it posted to.
        own, foreign = doctor.split_route_hooks(loop, hooks, wanted, ownership=not active)
    except config.ConfigError as exc:
        return [f"cannot tell this loop's hooks from anyone else's: {exc}",
                f"fix: hermes dk set --loop {loop.get('id')} --host "
                "https://your-gateway.example"], False
    out, ok = [], True
    failed: list[tuple[int, list[str]]] = []      # (hook id, the errors that decide its fix)
    targets = {name: doctor.seat_route_target(loop, name) for name in wanted}
    role_of = {name: role for role, name in seats}
    for hook in foreign:
        name = doctor.hook_route_name(hook)
        want, reason = targets[name]
        if want is None and active:
            out.append(f"hook {hook['id']} posts to route {name!r}; {reason} — not armed, "
                       "left as it is")
            continue
        why = doctor.hook_url_difference(str(hook["config"].get("url") or ""),
                                         want or (doctor.install_hook_urls(loop, name) or [""])[0])
        out.append(f"hook {hook['id']} posts to route {name!r} at {why} — not this seat's hook "
                   "(nothing this loop serves receives it), left as it is")
    matched: set[str] = set()
    miswired: list[int] = []
    for hook in own:
        name = doctor.hook_route_name(hook)
        matched.add(name)
        if active:
            # At the seat's URL but subscribed to the wrong event (or not JSON), or with a
            # trailing slash the gateway 404s: doctor calls each a MISMATCH, so arming it would
            # report a loop live that the seat never hears. (Found by same_hook_url, judged by
            # exact_hook_url — pausing still stops it.)
            want = targets[name][0] or ""
            problem = ("" if doctor.exact_hook_url(str(hook["config"].get("url") or ""), want)
                       else doctor.SLASH_404)
            problem = problem or doctor.hook_wake_problem(hook, role_of.get(name, ""))
            if problem:
                ok = False
                miswired.append(hook["id"])
                why = (problem if problem.endswith("never woken") else
                       f"{problem}, so it would not wake the {role_of.get(name, '?')} seat")
                out.append(f"hook {hook['id']} NOT armed: it {why} — left as it is")
                continue
        # Only a real bool is a state: a hook with no `active` (or a non-bool one) is flipped and
        # read back like any other, never taken as already there.
        if isinstance(hook.get("active"), bool) and hook["active"] == active:
            out.append(f"hook {hook['id']} already {word}")
            continue
        path = f"/repos/{loop['repo']}/hooks/{hook['id']}"
        _, error = gh.fetch(loop, path, method="PATCH", body={"active": active}, login=login)
        # A lost response is ambiguous and a stub or proxy can answer 200 without writing, so the
        # hook's state is whatever a fresh read says — never what was asked for.
        actual, read_error = gh.fetch(loop, path, login=login)
        if not isinstance(actual, dict) or not isinstance(actual.get("active"), bool):
            ok = False
            out.append(f"hook {hook['id']} NOT CONFIRMED {word}: "
                       + (f"PATCH failed ({error}); " if error else "")
                       + f"read-back failed ({read_error or 'no hook in the answer'})")
            failed.append((hook["id"], [e for e in (error, read_error) if e]))
            continue
        seen = "active" if actual["active"] else "paused"
        if actual["active"] == active:
            out.append(f"hook {hook['id']} → {seen} (read back)")
            continue
        ok = False
        out.append(f"hook {hook['id']} is still {seen}, not {word}"
                   + (f": PATCH failed ({error})" if error else ": GitHub accepted the PATCH "
                      "but the read-back disagrees"))
        # A PATCH GitHub accepted that did not stick is a refusal; a failed PATCH is judged by its
        # own code, so a timeout or a 5xx gets the retry advice, not the token-scope one.
        failed.append((hook["id"], [error or "refused"]))
    # Arming needs each seat's route bound in the registry; pausing only needs the hooks.
    unbound = [(role, name) for role, name in seats
               if active and targets[name][0] is None]
    if not matched and not unbound:
        return out + ["no loop hooks found at the routes' own URLs — run init --hooks first "
                      f"(looked for hooks posting to {', '.join(wanted) or 'a loop route'})"], False
    missing = [(role, name) for role, name in seats if name not in matched]
    for role, name in missing:
        reason = ((targets[name][1] if active else "") or
                  ("no repo hook posts to this route's URL, so the loop cannot be "
                   f"{'armed' if active else 'paused'} as a whole"))
        out.append(f"hook:{name} ABSENT ({role} seat) — {reason}")
    if miswired:
        out.append(f"fix: hook{'s' if len(miswired) > 1 else ''} {', '.join(map(str, miswired))}:"
                   f" `hermes dk doctor --loop {loop.get('id')}` names what each needs "
                   "(re-run init --hooks, or on GitHub set the event / content_type, or drop the "
                   f"URL's trailing slash — `hermes dk apply --hooks --loop {loop.get('id')}` "
                   "repoints a slashed hook), then run `arm` again")
    # The fix is per hook: one refused hook must not hide the retry advice another hook's 5xx
    # earned. Hooks that need the same fix share its line.
    advice: dict[str, list[int]] = {}
    for hook_id, errors in failed:
        # GitHub answers 404 to a token that may not see hooks, so 404 counts as a refusal too.
        refused = any(e == "refused" or any(f"HTTP {code}" in e for code in (401, 403, 404))
                      for e in errors)
        advice.setdefault(_hook_write_fix(token_login, transient=not refused, loop=loop),
                          []).append(hook_id)
    for text, ids in advice.items():
        out.append(f"fix: hook{'s' if len(ids) > 1 else ''} {', '.join(map(str, ids))}: {text}")
    if unbound:
        ok = False
        out.append(f"fix: `hermes dk doctor --loop {loop.get('id')}` names what each "
                   "route needs (its `route:` line and fix) — repair the route first, then run "
                   f"`arm{' --pause' if not active else ''}` again")
    if [pair for pair in missing if pair not in unbound]:
        ok = False
        out.append(f"fix: `hermes dk doctor --loop {loop.get('id')}` shows the hook each "
                   "seat needs; add the missing one (by hand with its route's URL and secret, or "
                   "via `init --hooks` for a loop being set up — hook write on "
                   f"{loop.get('repo')}, --admin-token <login>), then run "
                   f"`arm{' --pause' if not active else ''}` again")
    return out, ok


def cmd_doctor(args) -> int:
    """Read-only preflight: is this installation able to run the loop at all?

    Every check is in ``doctor.py`` — what each state means and, deliberately, everything this
    verb does *not* do (it writes nothing, and it never fires a route: a synthetic POST at a
    seat's route is a real agent run). This is the installation-level counterpart of the
    per-PR diagnosis in the watchdog, for the question `init` cannot answer about itself.
    """
    try:
        loops = [config.load_id(args.loop)] if args.loop else config.all_loops()
    except config.ConfigError as exc:
        print(f"cannot preflight loop: {doctor._safe_report_text(str(exc))}")
        return 2
    if not loops:
        print(f"no loops configured in {config.config_dir()} — run "
              "`hermes dk setup` to create one")
        return 0
    failed = 0
    for loop in loops:
        if getattr(args, "repair", False):
            # The one write doctor can make, and only when asked: put this loop's own routes back
            # from the plugin's intent record (same secret). Everything after it stays read-only.
            lines = route_intent.heal(loop) + gate_shims.heal(loop)
            print("\n".join(lines) if lines else
                  f"[{loop['id']}] repair: routes match the plugin's intent record — nothing restored")
        failed += doctor.report(loop, doctor.check_loop(loop, offline=args.offline),
                                strict=args.strict)
    return 1 if failed else 0


def cmd_selftest(args) -> int:
    """Verify the live isolated path for one loop; every step and its fix live in selftest.py.

    Unlike ``doctor`` this touches the real capabilities (bubblewrap, the model, GitHub reads, and
    with ``--live-turn`` one real reviewer turn) — but it never writes to GitHub: GitHub calls go
    through a GET-only guard and the live turn's broker runs in its host-only no-write mode.
    """
    from . import selftest
    if args.live_turn and args.pr is None:
        print("--live-turn needs --pr N: the turn reviews that pull request")
        return 2
    if args.live_turn and args.no_model:
        print("--live-turn needs the model; drop --no-model")
        return 2
    if getattr(args, "timeout", None) is not None and args.timeout < 1:
        print("--timeout must be positive")
        return 2
    try:
        loop = config.load_id(args.loop)
    except config.ConfigError as exc:
        print(f"cannot selftest loop: {doctor._safe_report_text(str(exc))}")
        return 2
    # Default to what production gives the reviewer seat, so a passing selftest proves the turn
    # fits the budget the unattended worker will actually enforce (#49).
    return selftest.run(loop, pr=args.pr, model=not args.no_model, live_turn=args.live_turn,
                        timeout=(args.timeout if getattr(args, "timeout", None) is not None
                                 else config.turn_budget(loop, "reviewer")),
                        ping=getattr(args, "ping", False),
                        ping_login=getattr(args, "admin_token", "") or None)


def cmd_corpus(args) -> int:
    """Replay the golden corpus through the reviewer (no-write) and record the scores (#491)."""
    from . import (
        corpus,
        prompts,
        seat_model,
        selftest,
    )
    try:
        loop = config.load_id(args.loop)
        directory = corpus.corpus_dir(loop, args.dir)
        cases = corpus.load(directory)
        scores = config.state_dir(loop) / corpus.SCORES_FILE
    except (config.ConfigError, corpus.CorpusError) as exc:
        print(f"cannot run corpus: {doctor._safe_report_text(str(exc))}")
        return 2
    if args.history:
        bad: list = []
        for entry in corpus.history(scores, bad):
            print(f"{entry['prompt_rev']}  {entry['model']}  caught {entry['caught']}  "
                  f"missed {entry['missed']}")
        if bad:
            print(f"skipped {len(bad)} unreadable line(s) in {scores} "
                  f"(line {', '.join(map(str, bad))})")
        return 0
    if not cases:
        print(f"no cases in {directory}")
        return 2
    try:
        settings = selftest.check_runtime(selftest.Report(None, selftest.Redactor()),
                                          selftest.runtime_path())
        if settings is None:
            raise corpus.CorpusError("no usable runtime config; run `hermes dk selftest` first")
        reviewer = seat_model.resolve_seat(loop, "reviewer", settings)
        review = corpus.live_review(loop, settings, reviewer,
                                    args.timeout or config.turn_budget(loop, "reviewer"))
    except Exception as exc:
        print(f"cannot run corpus: {doctor._safe_report_text(str(exc))}")
        return 2
    try:
        with selftest.github_read_only(), selftest.worker_tempdir():
            corpus.check_heads(loop, cases)
            results = corpus.replay(cases, review)
    except corpus.CorpusError as exc:
        print(f"cannot run corpus: {doctor._safe_report_text(str(exc))}")
        return 2
    for r in results:
        print(f"{r['case']}: caught {len(r['caught'])} missed {len(r['missed'])}"
              + (f" ({', '.join(r['missed'])})" if r["missed"] else "")
              + (f" [{r['error']}]" if r.get("error") else ""))
    entry = corpus.record(scores, prompts.revision("reviewer"), reviewer.model, results)
    print(f"caught {entry['caught']}, missed {entry['missed']} "
          f"(prompt {entry['prompt_rev']}, model {entry['model']}); recorded in {scores}")
    return 1 if entry["missed"] else 0


def cmd_arm(args) -> int:
    """Arm or pause loops by flipping their repo hooks; exit 1 unless GitHub confirms every one."""
    try:
        loops = [config.load_id(args.loop)] if args.loop else config.all_loops()
    except config.ConfigError as exc:
        print(f"cannot {'pause' if args.pause else 'arm'}: {exc}")
        return 2
    if not loops:
        print(f"no loops configured in {config.config_dir()} — nothing to "
              f"{'pause' if args.pause else 'arm'}")
        return 2
    failed = []
    for loop in loops:
        lines, ok = _set_hooks(loop, not args.pause, args.admin_token)
        for line in lines:
            print(f"[{loop['id']}] {line}")
        if not ok:
            failed.append(loop["id"])
        elif not args.pause and not cli_common._ping_loop_hooks(loop, args.admin_token):
            failed.append(loop["id"])
    if failed:
        print(f"{'pause' if args.pause else 'arm'} NOT confirmed for: {', '.join(failed)} "
              "— the lines above show what GitHub reports now")
        return 1
    return 0
