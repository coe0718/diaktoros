"""The seat checks (#1349, split from the doctor core): each seat's Hermes profile exists
(``check_profile``, ``check_adjudicator_profile``), the triage seat and its settings
(``check_triage``), the model each seat runs (``check_seat_models``, ``runtime_settings``), and
the extras a seat's model needs in its venv (``check_seat_extras``). Read-only, like every doctor
check. Each reaches the core through ``doctor`` (``from .. import doctor``); callers use
``seat_checks.<name>``.
"""
from __future__ import annotations

import pathlib
import subprocess

from .. import config, doctor, gh
from ..config import credentials
from ..doctor import identity_checks


def _check_profile(name: str, seat: str) -> doctor.Check:
    if not name:
        return doctor.Check(f"profile:{seat}", doctor.ABSENT, "no profile named for this seat",
                            f"set {seat}_profile in the plugin settings to an existing profile, then run `hermes dk apply --loop <id>`")
    path = doctor.profile_dir(name)
    if path.is_dir():
        return doctor.Check(f"profile:{seat}", doctor.VERIFIED, f"{name} → {path}")
    return doctor.Check(f"profile:{seat}", doctor.ABSENT, f"no profile home at {path}",
                        f"`hermes profile create {name}`, or set {seat}_profile in the plugin settings to "
                        f"a profile that exists and run `hermes dk apply --loop <id>`: the run "
                        f"happens as this profile")


def check_profile(loop: dict, seat: str) -> doctor.Check:
    return _check_profile(str(loop["seats"][seat].get("profile") or ""), seat)


def check_triage(loop: dict) -> list[doctor.Check]:
    """Issue triage (#213): the profile its model runs as, and the token it labels with."""
    checks = [_check_profile(config.seat_profile(loop, "triage"), "triage")]
    login = config.triage_login(loop)
    path = gh.token_path(loop, login) if login else None
    from .. import github_app
    if path is None and login and github_app.is_app(loop, login):
        checks.append(identity_checks.check_app_identity(loop, login, "credential:triage"))
    elif path is None or not path.is_file() or path.stat().st_size == 0:
        checks.append(doctor.Check("credential:triage", doctor.ABSENT,
                                   f"no nonempty token file mapped for the triage login {login or '?'}",
                                   f"map one: `hermes dk set --loop {loop['id']} --token "
                                   f"{login or '<login>'}=/path/to/pat` (it needs issues: write)"))
    elif credentials.token_file_problem(str(path)):
        checks.append(doctor.Check("credential:triage", doctor.MISMATCH,
                                   f"{login} → {identity_checks._token_file_facts(path)}: "
                                   f"{credentials.token_file_problem(str(path))}", f"chmod 600 {path}"))
    else:
        triage = loop["triage"]
        checks.append(doctor.Check("credential:triage", doctor.VERIFIED,
                                   f"{login} → {identity_checks._token_file_facts(path)} · authors "
                                   f"{', '.join(triage['authors'])} · {len(triage['labels'])} labels"))
    # #1217: with no maintainers, no comment reaches the seats and no re-request starts a review.
    names = sorted(config.maintainers(loop))
    agents = config.operator_agents(loop)
    people = [n for n in names if n not in agents]       # #757: agents are named apart
    checks.append(doctor.Check("triage:maintainers", doctor.VERIFIED,
                               f"{', '.join(people) or 'none in person'}"
                               + (f" · operator agents: {', '.join(sorted(agents))}" if agents else "")
                               + " (their comments reach both seats)") if names else
                  doctor.Check("triage:maintainers", doctor.UNKNOWN,
                               "no maintainers: no one's PR or issue comments reach the seats, and no "
                               "re-request starts a fresh review",
                               f"run `hermes dk triage --loop {loop['id']} --maintainer <login>`"))
    return checks


def check_adjudicator_profile(loop: dict) -> doctor.Check:
    return _check_profile(str((loop.get("adjudicator") or {}).get("profile") or "default"),
                          "adjudicator")


def runtime_settings() -> tuple[dict | None, doctor.Check | None]:
    """The runtime file's settings for the model checks, or a check explaining why not."""
    from .. import seat_model
    path = config.host_path("runtime")
    if not path.exists():
        return None, None
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("must be a private (0600) regular file")
        return seat_model.load_runtime(path), None
    except (OSError, ValueError) as exc:
        return None, doctor.Check("runtime", doctor.MISMATCH, f"{path}: {exc}",
                                  f"repair {path}; `hermes dk selftest` shows each problem")


def check_seat_models(loop: dict) -> list[doctor.Check]:
    """Each seat's model as the worker will pick it — profile, provider, model; never the key.

    Read-only: the profile's ``config.yaml`` ``model`` block is read by the Hermes interpreter,
    without resolving (or refreshing) any credential. ``selftest`` resolves the credential and
    makes one tiny completion per seat.
    """
    from .. import seat_model
    settings, problem = runtime_settings()
    checks = [problem] if problem else []
    if settings is not None and seat_model.legacy_override(settings) is not None:
        checks.append(doctor.Check("runtime:legacy-model", doctor.UNKNOWN,
                                   "diaktoros-runtime.json still sets a top-level model/upstream/"
                                   "key_file: a LEGACY fallback used only for a seat whose profile "
                                   "cannot be resolved",
                                   "drop it once every seat's profile resolves, or move it under "
                                   "seats.<seat> as an explicit per-seat override"))
    status_of = {"ok": doctor.VERIFIED, "warn": doctor.UNKNOWN, "fail": doctor.ABSENT}
    for seat in seat_model.seats_for(loop):
        try:
            status, detail, fix = seat_model.describe_seat(loop, seat, settings)
        except Exception as exc:        # e.g. a malformed base_url: undecided, never a crash
            status, detail, fix = ("warn", f"could not describe this seat ({type(exc).__name__}: "
                                           f"{exc})", "run `hermes dk selftest`")
        checks.append(doctor.Check(f"model:{seat}", status_of[status], detail, fix))
    shadow = config.shadow_profile(loop)
    if shadow and shadow == config.seat_profile(loop, "reviewer"):
        # #218: the same profile is the same account and the same model. A shadow 429 would
        # hold the real reviewer's account, and its verdicts would compare a model with itself.
        checks.append(doctor.Check("model:shadow-independent", doctor.ABSENT,
                                   f"the shadow runs as {shadow!r}, the reviewer's own profile",
                                   "give shadow reviews a profile of their own (another model and "
                                   f"account): `hermes dk set --loop {loop['id']} --shadow-profile P`"))
    return checks


# Read-only: ``find_spec`` locates a module without importing (or installing) anything.
# ``-I`` keeps the answer the venv's own — no PYTHONPATH, user site-packages or cwd — which is
# what the sandbox's Hermes resolves against; a dotted name whose parent is missing is "absent".
_FIND_SPEC = ("import importlib.util, sys\n"
              "try:\n    found = importlib.util.find_spec(sys.argv[1]) is not None\n"
              "except (ImportError, ValueError):\n    found = False\n"
              "sys.exit(0 if found else 1)\n")


EXTRA_PROBE_TIMEOUT = 20


def venv_has_module(python: str, module: str) -> bool | None:
    """True/False: can ``python`` import ``module``; ``None`` when it could not say."""
    try:
        process = subprocess.run([python, "-I", "-c", _FIND_SPEC, module],
                                 env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                 stdin=subprocess.DEVNULL, capture_output=True,
                                 timeout=EXTRA_PROBE_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(process.returncode)


def check_seat_extras(loop: dict) -> list[doctor.Check]:
    """#118: each seat's provider's optional Hermes package, importable by the runtime's venv.

    A Claude-subscription (or any Messages-wire) seat needs Hermes's ``anthropic`` extra; an
    install without it looks healthy until the seat's first turn fails at model setup. The venv
    checked is the one the runtime file names — the one the sandbox mounts — and it is asked
    with ``find_spec`` (read-only, no network, bounded). The provider comes from the same
    read-only description as ``model:<seat>``: no credential is resolved. A seat whose provider
    could not be read, or a probe that could not answer, is ``unknown`` — never ``verified``.
    """
    from .. import seat_model
    runtime = config.host_path("runtime")
    settings, problem = runtime_settings()
    venv = str((settings or {}).get("venv") or "")
    probes: dict[str, bool | None] = {}
    checks = []
    for seat in seat_model.seats_for(loop):
        name = f"extras:{seat}"
        if not venv:
            why = f"{runtime} is not usable" if problem else f"no runtime file at {runtime}"
            checks.append(doctor.Check(name, doctor.UNKNOWN, f"{why}, so the Hermes venv the {seat} turn "
                                       "mounts is not known; its provider package was not checked",
                                       f"write {runtime} naming source/venv/runtime/rust"))
            continue
        needed = maybe = None
        explain: dict = {}
        try:
            status, _, _, wire = seat_model.describe_seat_wire(loop, seat, settings)
            if wire is not None and status != "fail":
                needed, maybe = seat_model.extras_for(
                    wire["provider"], wire["api_mode"], wire["base_url"],
                    model=wire.get("model") or "", configured=wire.get("configured") or "",
                    facts=wire.get("facts"), entry=wire.get("entry"), hermes=wire.get("hermes"),
                    entry_hint=bool(wire.get("entry_hint")), explain=explain)
        except Exception as exc:        # a malformed URL, a broken description: undecided
            status, wire, reason = "warn", None, f" ({type(exc).__name__}: {exc})"
        else:
            reason = ""
        if status == "fail":
            checks.append(doctor.Check(name, doctor.SKIPPED, f"skipped: model unresolved (see model:{seat})"))
            continue
        if wire is None:
            checks.append(doctor.Check(name, doctor.UNKNOWN, f"the {seat} seat's provider could not be read"
                                       f"{reason} (see model:{seat}), so whether it needs an optional "
                                       "Hermes package is unknown",
                                       f"fix model:{seat} first, then re-run doctor"))
            continue
        provider, model = wire["provider"], wire.get("model") or ""
        # The wire Hermes will use when it is decided (a named entry, nous native, ...), not
        # the profile's own spelling.
        mode = explain.get("wire") or wire["api_mode"]
        not_asked = str(wire.get("not_asked") or "")
        if not needed and not maybe:
            if not_asked:       # a "not needed" from doctor's own table is not a pass
                checks.append(doctor.Check(name, doctor.UNKNOWN, f"{provider} [{mode}] needs no optional Hermes "
                                           f"package by doctor's own table only — Hermes was not asked "
                                           f"({not_asked})", f"fix model:{seat} first, then re-run doctor"))
            else:
                checks.append(doctor.Check(name, doctor.VERIFIED, f"{provider} [{mode}] needs no optional "
                                           "Hermes package"))
            continue
        python = str(pathlib.Path(venv) / "bin" / "python")

        def probe(extra: str) -> bool | None:
            module = seat_model.HERMES_EXTRAS[extra]["module"]
            if module not in probes:
                probes[module] = venv_has_module(python, module)
            return probes[module]

        missing = [e for e in needed if probe(e) is False]
        undecided = [e for e in needed if probe(e) is None]
        maybe_missing = [e for e in maybe if probe(e) is not True]
        what = ", ".join(f"`{e}` (import {seat_model.HERMES_EXTRAS[e]['module']})"
                         for e in needed + maybe)
        switch = "; ".join(
            f"may need the {e} extra: {provider} can use Anthropic's native wire for {model} "
            f"{explain.get(e, '')}".rstrip()
            for e in maybe_missing)
        because = "; ".join(explain[e] for e in needed if explain.get(e))
        install = lambda extras: " and ".join(f"`hermes pm install --extra {e}`" for e in extras)
        where = (f"it installs into the venv Hermes selects, so if that is not {venv}, install the "
                 f"extra into {venv} or point `venv` in {runtime} at the venv that has it")
        if missing:
            checks.append(doctor.Check(
                name, doctor.ABSENT,
                f"{provider} [{mode}] needs the Hermes extra {what}"
                + (f" ({because})" if because else "")
                + f", which {python} cannot import; the {seat} turn would fail at model setup"
                + (f" ({switch})" if switch else ""),
                f"{install(missing + maybe_missing)} (Hermes's own command for a missing extra) "
                f"— {where}; then re-run doctor"))
        elif undecided or maybe_missing:
            if switch and not undecided:
                detail = (f"{switch}; {python} cannot import it (install: "
                          f"{install(maybe_missing)} — {where})")
            else:
                detail = (f"{provider} [{mode}] needs the Hermes extra {what}; {python} could not "
                          "answer whether it is importable (missing, not a python, or no answer "
                          "in time)")
            checks.append(doctor.Check(name, doctor.UNKNOWN, detail, f"check `venv` in {runtime}"))
        else:
            label = "may use" if maybe and not needed else "needs"
            checks.append(doctor.Check(name, doctor.VERIFIED,
                                       f"{provider} [{mode}] {label} {what}: importable by {python}"))
    return checks
