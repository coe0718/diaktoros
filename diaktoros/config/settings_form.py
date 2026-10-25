"""The plugin settings form (#676): its schema, the defaults it gives a new loop, and the push
``apply`` makes of it onto an existing one.

What the form holds is plugin-level and per Hermes profile; a loop's own file stays its effective
configuration. The rules a value must pass are the core's (``config.check_*``), called through
the package so a test that patches one reaches this module too.
"""
from __future__ import annotations

from .. import config


# Plugin-level settings. The desktop's Capabilities → Plugins form renders `config_schema` from
# plugin.yaml; this table mirrors it so the CLI can use the same defaults without a YAML parser
# (the package is stdlib-only on purpose). `tests/run_tests.py` asserts the two agree, because a
# form that writes keys nothing reads is worse than no form at all.
SETTINGS_SCHEMA: dict = {
    "cap": {"label": "Review cap (verdicts)", "type": "int", "default": 3,
            "description": "Verdicts before the loop stops and hands the PR to an adjudicator "
                           "(cap - 1 fixes)"},
    "reviewer_concurrency": {"label": "Reviews at once", "type": "int", "default": 1,
                             "description": "Reviews that may run at once; everything above this "
                                            "queues. Above 1 needs a clone path, so each run gets "
                                            "its own sandbox."},
    "fixer_concurrency": {"label": "Fixes at once", "type": "int", "default": 1,
                          "description": "Fixes that may run at once; everything above this queues"},
    "shadow_concurrency": {"label": "Shadow reviews at once", "type": "str", "default": "",
                           "description": "Shadow reviews (#218) that may run at once, a whole "
                                          "number >= 1; everything above this queues. Takes "
                                          "effect only while the loop has a shadow profile. Blank "
                                          "= not set here: the loop keeps its own value (default: "
                                          "1, serialized)"},
    "triage_concurrency": {"label": "Issue triages at once", "type": "str", "default": "",
                           "description": "Issue triages that may run at once, a whole number "
                                          ">= 1; everything above this queues. Takes effect only "
                                          "while the loop has issue triage on. Blank = not set "
                                          "here: the loop keeps its own value (default: 1, "
                                          "serialized)"},
    "clone": {"label": "Clone path (required above 1)", "type": "str", "default": "",
              "description": "Local clone the runs isolate from — required above 1, because a "
                             "parallel run in a shared checkout produces wrong verdicts"},
    "base": {"label": "Base branch", "type": "str", "default": "main",
             "description": "Base branch the loop watches"},
    "grace_min": {"label": "Watchdog grace (minutes)", "type": "int", "default": 35,
                  "description": "Minutes a quiet PR may sit before the watchdog speaks"},
    "ttl_min": {"label": "Seat slot TTL (minutes)", "type": "int", "default": 45,
                "description": "Minutes a seat slot survives — the backstop for a run that died "
                               "without a verdict"},
    "inflight_ttl_min": {"label": "In-flight mark TTL (minutes)", "type": "int", "default": 10,
                         "description": "Minutes an in-flight mark blocks a second run at the "
                                        "same head"},
    "turn_budget_s": {"label": "Turn budget (seconds)", "type": "int", "default": 900,
                      "description": "Wall-clock seconds one isolated seat turn may run: Hermes's "
                                     "--run-budget, and the sandbox is killed shortly after. A "
                                     "real review builds and tests, so keep it generous "
                                     "(60-14400)"},
    "host": {"label": "Webhook host", "type": "str", "default": "",
             "description": "Your gateway's public webhook origin (required to create GitHub hooks)"},
    "reviewer_profile": {"label": "Reviewer's Hermes profile", "type": "str", "default": "",
                         "description": "Profile the reviewer seat runs as. Blank = not set here: "
                                        "a new loop still needs one, and it must differ from the "
                                        "fixer's"},
    "fixer_profile": {"label": "Fixer's Hermes profile", "type": "str", "default": "",
                      "description": "Profile the fixer seat runs as. Blank = not set here: a new "
                                     "loop still needs one, and it must differ from the reviewer's"},
    "reviewer_login": {"label": "Reviewer's GitHub login", "type": "str", "default": "",
                       "description": "GitHub login the reviewer seat acts as — the login the "
                                      "review route serves. Must be in the loop's reviewers "
                                      "allowlist. Blank = not set here"},
    "fixer_login": {"label": "Fixer's GitHub login", "type": "str", "default": "",
                    "description": "GitHub login the fixer seat acts as. Must be in the loop's "
                                   "fixers allowlist. Blank = not set here"},
    "adjudicator_profile": {"label": "Adjudicator's Hermes profile (optional)", "type": "str",
                            "default": "",
                            "description": "Profile that rules when the verdict budget is spent. "
                                           "Blank = not set here: a loop with no adjudicator route "
                                           "is left exactly as it is"},
    # Token *paths* only. A token value typed into a settings form would be stored in plain text in
    # a config file this plugin never owns, so these keys hold where the PAT lives — never the PAT.
    "reviewer_token_file": {"label": "Reviewer's token file (path only)", "type": "str",
                            "default": "",
                            "description": "Absolute path (~ allowed) to the file holding the "
                                           "reviewer login's GitHub PAT — the path only, never the "
                                           "token itself. Must be your own mode-600 regular file. "
                                           "Blank = not set here"},
    "fixer_token_file": {"label": "Fixer's token file (path only)", "type": "str", "default": "",
                         "description": "Absolute path (~ allowed) to the file holding the fixer "
                                        "login's GitHub PAT — the path only, never the token "
                                        "itself. Must be your own mode-600 regular file. Blank = "
                                        "not set here"},
    "adjudicator_login": {"label": "Adjudicator's GitHub login (optional)", "type": "str",
                          "default": "",
                          "description": "Optional fourth account the ruling is also posted as, "
                                         "as a PR comment. Must differ from the reader and both "
                                         "seats, with its own credential (a PAT file, or a GitHub App). Lands only on a loop "
                                         "with an adjudicator route. Blank = not set here"},
    "adjudicator_token_file": {"label": "Adjudicator's token file (path only, optional)",
                               "type": "str", "default": "",
                               "description": "Absolute path (~ allowed) to the file holding the "
                                              "adjudicator login's GitHub PAT — the path only, "
                                              "never the token itself. Must be your own mode-600 "
                                              "regular file, not shared with any other login. "
                                              "Blank = not set here"},
    "reviewer_app_id": {"label": "Reviewer's GitHub App id",
                              "type": "str", "default": "",
                              "description": (
                                  "Positive ASCII decimal App id, paired with this seat's App key file and bot "
                                  'login. Blank pair = not set here. A complete pair selects App, never PAT.'
                              )},
    "reviewer_app_key_file": {"label": "Reviewer's App key file (path only)",
                              "type": "str", "default": "",
                              "description": (
                                  'Absolute path (~ allowed) to your mode-600 regular private-key file; the path '
                                  'only, never the key itself. Set with the App id. Blank pair = not set here.'
                              )},
    "fixer_app_id": {"label": "Fixer's GitHub App id",
                              "type": "str", "default": "",
                              "description": (
                                  "Positive ASCII decimal App id, paired with this seat's App key file and bot "
                                  'login. Blank pair = not set here. A complete pair selects App, never PAT.'
                              )},
    "fixer_app_key_file": {"label": "Fixer's App key file (path only)",
                              "type": "str", "default": "",
                              "description": (
                                  'Absolute path (~ allowed) to your mode-600 regular private-key file; the path '
                                  'only, never the key itself. Set with the App id. Blank pair = not set here.'
                              )},
    "adjudicator_app_id": {"label": "Adjudicator's GitHub App id",
                              "type": "str", "default": "",
                              "description": (
                                  "Positive ASCII decimal App id, paired with this seat's App key file and bot "
                                  'login. Blank pair = not set here. A complete pair selects App, never PAT.'
                              )},
    "adjudicator_app_key_file": {"label": "Adjudicator's App key file (path only)",
                              "type": "str", "default": "",
                              "description": (
                                  'Absolute path (~ allowed) to your mode-600 regular private-key file; the path '
                                  'only, never the key itself. Set with the App id. Blank pair = not set here.'
                              )},
    "reviewer_max_steps": {"label": "Reviewer's agent steps per turn", "type": "str", "default": "",
                           "description": "Agent steps one reviewer turn may take, 8-200. Blank = "
                                          "not set here: the loop keeps its own value or the "
                                          "role default"},
    "fixer_max_steps": {"label": "Fixer's agent steps per turn", "type": "str", "default": "",
                        "description": "Agent steps one fixer turn (and an issue fix) may take, "
                                       "8-200. Blank = not set here: the loop keeps its own value "
                                       "or the role default"},
    "reviewer_daily_tokens": {"label": "Reviewer's daily token budget", "type": "str", "default": "",
                              "description": "Tokens (in + out) the reviewer may spend per local "
                                             "day, a whole number. Once spent, the next turn "
                                             "waits for midnight; a turn in flight is not "
                                             "killed. Counted by the inference proxy from what "
                                             "the provider reports. Blank = not set here: the "
                                             "loop keeps its own value (default: no budget)"},
    "shadow_daily_tokens": {"label": "Shadow reviews' daily token budget", "type": "str",
                            "default": "",
                            "description": "Tokens (in + out) shadow reviews (#218) may spend per "
                                           "local day, a whole number. Once spent, the next "
                                           "shadow waits for midnight; a review in flight is not "
                                           "killed. Blank = not set here: the loop keeps its own "
                                           "value (default: no budget)"},
    "fixer_daily_tokens": {"label": "Fixer's daily token budget", "type": "str", "default": "",
                           "description": "Tokens (in + out) the fixer, and issue fixes, may "
                                          "spend per local day, a whole number. Once spent, the "
                                          "next turn waits for midnight; a turn in flight is not "
                                          "killed. Blank = not set here: the loop keeps its own "
                                          "value (default: no budget)"},
    "auto_fix_labels": {"label": "Auto-fix labels (comma-separated)", "type": "str", "default": "",
                        "description": "Triage labels (from triage.labels) that hand an issue to "
                                       "the fixer without a maintainer, e.g. P3, documentation. "
                                       "Needs the loop's triage.fix_label. P0-P2 are never "
                                       "auto-offered. Blank = not set here: the loop keeps its "
                                       "own value (empty by default: person-only trigger)"},
    "auto_fix_daily": {"label": "Auto-fix daily cap (issues)", "type": "str", "default": "",
                       "description": "Automatic issue fixes per loop per local day, 1-1000 "
                                      "(default 25). Needs the loop's triage.auto_fix_labels. "
                                      "Blank = not set here: the loop keeps its own value or "
                                      "the default"},
    "fix_daily_turns": {"label": "Issue-fix daily cap (turns)", "type": "str", "default": "",
                        "description": "Issue-fix turns per local day, 1-1000. Needs the loop's "
                                       "triage.fix_label. Blank = not set here: the loop keeps "
                                       "its own value or the default"},
    "review_only": {"label": "Review-only authors (comma-separated)", "type": "str",
                    "default": "",
                    "description": "GitHub logins whose PRs the reviewer reviews but the fixer "
                                   "never touches: a changes-requested verdict goes back to the "
                                   "author. Not a fixer or a reviewer. Blank = not set here"},
    "review_only_cap": {"label": "Review-only verdict cap", "type": "str", "default": "",
                        "description": "Verdicts the reviewer may give one review-only PR before it "
                                       "stops until a maintainer grants another round, a whole "
                                       "number 1-1000. Blank = not set here: the loop's own value, "
                                       "else the review cap"},
    "observer_weekly_report": {"label": "Weekly report (weekday and UTC time)", "type": "str",
                               "default": "",
                               "description": "When the observer feed gets the week's report: a "
                                              "weekday and a UTC time like 'mon 09:00'. Needs the "
                                              "loop's observer feed. 'off' turns it off. Blank = "
                                              "not set here: the loop's own value, else none"},
    "ci_fix_cap": {"label": "CI-fix turns per PR", "type": "str", "default": "",
                   "description": "CI-fix turns per PR (across its heads) before the reviewer "
                                  "reviews a red head, a whole number 1-10. Red required CI goes "
                                  "to the fixer first and spends no review verdict. Blank = not "
                                  "set here: the loop's own value, else 3"},
    "review_only_daily": {"label": "Review-only daily cap (turns)", "type": "str", "default": "",
                          "description": "Reviewer turns per local day on review-only PRs, a whole "
                                         "number 1-1000; once spent they wait until midnight. "
                                         "Blank = not set here: the loop's own value, else no cap"},
    "review_only_update": {"label": "Merge the base into review-only PRs", "type": "bool",
                           "default": False,
                           "description": "Off (default): a review-only PR that conflicts gets a "
                                          "notice naming the merged PRs, the files and the commands. "
                                          "On: the host also pushes a clean merge of the base into "
                                          "the author's same-repository branch, with a lease on the "
                                          "head it read (a push to a branch the loop does not own; "
                                          "needs unattended fixer pushes). Never resolves a real "
                                          "conflict or a workflow change. It pushes only a PR "
                                          "GitHub reports as behind, which it does only when "
                                          "the base requires branches to be up to date; "
                                          "without that setting only the notice applies"},
    "required_checks": {"label": "Required CI checks (comma-separated)", "type": "str",
                        "default": "",
                        "description": "The check runs or status contexts that gate an approval, "
                                       "named exactly as GitHub shows them, e.g. 'tests (3.11), "
                                       "lint'. Others are shown to the reviewer as optional and "
                                       "never block. Blank = not set here (an unset list means "
                                       "every check gates)"},
    "shadow_profile": {"label": "Shadow review profile", "type": "str", "default": "",
                       "description": "A second reviewer model in no-write mode (#218): the "
                                      "Hermes profile it runs as, never the reviewer's own. Its "
                                      "verdicts are recorded for comparison and never posted. "
                                      "'off' turns shadow reviews off. Blank = not set here: the "
                                      "loop keeps its own value (default: off)"},
    "shadow_every": {"label": "Shadow one in N reviews", "type": "str", "default": "",
                     "description": "How often a reviewer turn also gets a shadow review, 1-1000 "
                                    "(1 = every one). Blank = not set here: the loop keeps its "
                                    "own value (default 5)"},
    "shadow_escalate": {"label": "Shadow review holds the PR", "type": "str", "default": "",
                        "description": "on: a PR is not ready to merge while its shadow review "
                                       "is running, or after it requested changes (#1004). "
                                       "Needs shadow every = 1. Blank = not set here: the loop "
                                       "keeps its own value (default: off)"},
    "human_paths": {"label": "Human-only paths (comma-separated globs)", "type": "str",
                    "default": "",
                    "description": "Glob patterns, e.g. '.github/**, scripts/release*'. If a "
                                   "reviewed diff touches one, the loop never approves it: when "
                                   "the reviewer would approve, the approval is left to a person "
                                   "(the head is held, one notice per head); REQUEST_CHANGES "
                                   "still works. '*' also matches '/'. Blank = not set here (an "
                                   "unset list means no path is reserved)"},
    "generated": {"label": "Generated files (PATH[,PATH]=COMMAND, ';;' between entries)",
                  "type": "str", "default": "",
                  "description": "Generated files the host regenerates when a PR's only merge "
                                 "conflicts are in them, e.g. 'docs/commands.md=python3 "
                                 "tests/commands_doc.py --write'. The command runs only in the "
                                 "sandbox, on the merged tree, no network; its result is pushed "
                                 "only if no conflict marker remains, else a fixer turn resolves "
                                 "it. Takes effect only where a fixer pushes unattended. Blank = "
                                 "not set here (an unset list means the shortcut is off)"},
    "fixer_check": {"label": "Fixer's always-run check (command)", "type": "str", "default": "",
                    "description": "One command the fixer runs in the checkout before every "
                                   "push or issue-fix PR, besides the tests it touched: the "
                                   "checks CI always runs (chain several with &&). At most "
                                   "500 characters, one line. Blank = not set here"},
    "merge_check": {"label": "Post the diaktoros/review merge check", "type": "str", "default": "",
                    "description": "auto, on or off. The check mirrors whether the loop would "
                                   "merge the head (green only when approved and nothing holds "
                                   "it); make it required in branch protection for a block the "
                                   "merge button honours. auto: on when the reviewer is a GitHub "
                                   "App (a check run), off for a PAT reviewer (on: a commit "
                                   "status). Blank = not set here"},
    "review_after_ci": {"label": "Review after CI finishes", "type": "bool", "default": False,
                        "description": "On: a review waits while the head's checks are still "
                                       "running (up to an hour), then starts with their results. "
                                       "Off: it starts at once, and the reviewer sees whatever CI "
                                       "has finished so far."},
    "fix_ci": {"label": "Fix a failed CI check", "type": "bool", "default": False,
               "description": "On: a failed required check on a fixer's PR becomes one fixer "
                              "turn per head (needs unattended fixer pushes on). Off: a red "
                              "check is only reported."},
    "fixer_scope_lock": {"label": "Hold fixes outside the findings' files", "type": "bool",
                         "default": False,
                         "description": "On: a fix round that changes files the open findings do "
                                        "not name (other than their tests and docs) is held for "
                                        "a maintainer, with a notice listing the extra files; "
                                        "`hermes dk retry` lets it through. Only takes effect "
                                        "with unattended fixer pushes on. Off: no check."},
    "attribution": {"label": "Sign what the loop posts", "type": "bool", "default": True,
                    "description": "On: every review, comment and commit the loop itself "
                                   "posts ends with 'Automated by Diaktoros' and "
                                   "a link (commits get an Automated-By trailer). Off: "
                                   "nothing is added. Never touches what people or "
                                   "agents post by hand."},
}


def settings_defaults(settings: dict | None) -> dict:
    """Effective plugin-level defaults: what the settings form set, else the schema default.

    A value of the wrong type falls back to the default rather than propagating a string into a
    field the loop does arithmetic on. Nothing here raises: a bad setting must not break the CLI.
    """
    out: dict = {}
    for key, spec in SETTINGS_SCHEMA.items():
        value = (settings or {}).get(key)
        if value is None or value == "":
            value = spec["default"]
        if spec["type"] == "bool":
            value = _bool_setting(value, spec["default"])
        else:
            try:
                value = int(value) if spec["type"] == "int" else str(value)
            except (TypeError, ValueError):
                value = spec["default"]
        out[key] = value
    return out


def _bool_setting(value, default: bool) -> bool:
    """A form's boolean: a real bool, or the words a form or CLI may hand over; else ``default``."""
    if isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word in ("true", "on", "yes", "1"):
        return True
    if word in ("false", "off", "no", "0"):
        return False
    return default


def settings_loop_concurrency(d: dict) -> int:
    """The loop-level default a settings form implies: its seat value when both seats agree.

    When they disagree the loop default stays 1 and each seat that differs carries its own value.
    """
    if d["reviewer_concurrency"] == d["fixer_concurrency"]:
        return d["reviewer_concurrency"]
    return 1


# The form value the operator *actually* typed for one key — None/""/blank/whitespace is "not set
# here", never the schema default. ``settings_defaults`` deliberately substitutes the default so a
# fresh loop has real numbers to start from; that is right for ``init``, wrong for ``apply``, which
# must only move keys the operator named (#59).
def _form_value(settings: dict | None, key: str):
    value = (settings or {}).get(key)
    return None if value is None or str(value).strip() == "" else value


def _on_off_value(value, where: str, name: str):
    """``on``/``off`` (or a bool) as a bool; None when not given."""
    if value is None or isinstance(value, bool):
        return value
    word = str(value).strip().lower()
    if word in ("on", "true", "yes", "1"):
        return True
    if word in ("off", "false", "no", "0"):
        return False
    raise config.ConfigError(f"{where}: {name} must be on or off, got {value!r}")


def shadow_block(current, profile, every, where: str, escalate=None) -> dict | None:
    """The ``shadow`` block (#218) after a form, ``init``/``setup`` or ``set`` names a profile,
    ``every`` and/or ``escalate`` (#1004); None when none was named. ``off`` as the profile turns
    shadow reviews off. ``every`` and ``escalate on`` need a profile, already set or named with
    it; ``escalate off`` without one changes nothing."""
    escalate = _on_off_value(escalate, where, "shadow escalate")
    if profile is None and every is None and escalate is None:
        return None
    shadow = dict(current or {})
    if profile is not None:
        name = str(profile).strip()
        shadow = {} if name.lower() == "off" else {**shadow, "profile": name}
    if every is not None:
        if not shadow.get("profile"):
            raise config.ConfigError(f"{where}: a shadow review rate needs a shadow profile "
                                     "(name one with it)")
        shadow["every"] = config._form_int(every)
    if escalate is not None:
        if escalate and not shadow.get("profile"):
            raise config.ConfigError(f"{where}: shadow escalate needs a shadow profile "
                                     "(name one with it)")
        if escalate:
            shadow["escalate"] = True
        else:
            shadow.pop("escalate", None)
    return config.normalize_shadow(shadow, where)


def apply_settings(loop_raw: dict, settings: dict | None) -> dict:
    """The loop knobs the plugin settings own, overlaid on a raw (pre-``normalize``) loop dict.

    `apply` is deliberately a push, not a subscription: the form sets defaults, and a loop takes
    them when the operator says so. A running loop whose numbers changed under it would be a very
    confusing thing to debug at 2am.

    The push is also *only* over what the form named: a blank/absent field means "not set here",
    never "reset to the schema default" (#59). So ``cap``/``concurrency`` (and the other knobs)
    move only when the operator typed a value; an empty form leaves the loop's own numbers —
    including a non-default ``cap``/``concurrency`` — exactly as they were.
    """
    d = settings_defaults(settings)
    seats = {k: dict(v or {}) for k, v in (loop_raw.get("seats") or {}).items()}
    # The form's two seat numbers land as the loop default they agree on, and a seat carries its
    # own value only where it differs. Writing both seats every time would pin them, and a later
    # `set --concurrency` would move nothing (#76). The effective per-seat values are the form's —
    # but only for a seat the operator actually named: a blank form moves no seat at all (#59).
    seat_set = {seat: _form_value(settings, f"{seat}_concurrency") for seat in ("reviewer", "fixer")}
    if all(v is not None for v in seat_set.values()):
        # Both seats named: the form owns both, and they land as the loop default they agree on
        # (or each pinned where they differ) — the original, untouched semantics.
        loop_concurrency = settings_loop_concurrency(d)
        for seat in ("reviewer", "fixer"):
            if d[f"{seat}_concurrency"] != loop_concurrency:
                seats.setdefault(seat, {})["concurrency"] = d[f"{seat}_concurrency"]
            else:
                seats.setdefault(seat, {}).pop("concurrency", None)
    elif any(v is not None for v in seat_set.values()):
        # One seat named, the other blank: move only the named one; the loop default and the
        # other seat's pin are left alone.
        for seat, value in seat_set.items():
            if value is not None:
                seats.setdefault(seat, {})["concurrency"] = value
        loop_concurrency = loop_raw.get("concurrency")
    else:
        # No seat named: leave the seats and the loop default exactly as the loop has them.
        loop_concurrency = loop_raw.get("concurrency")
    # A blank clone in the form means "not set here", never "forget the clone this loop uses":
    # silently dropping it would quietly downgrade the cleanup, which prunes worktrees through it.
    clone = d["clone"] or str(loop_raw.get("clone") or "")
    # An unset form value cannot erase an existing loop's explicitly configured gateway.
    host = d["host"] or loop_raw.get("host") or ""
    overlaid = {**loop_raw, "clone": clone, "host": host, "seats": seats}
    # Only the knobs the operator actually set are written; a blank/absent field keeps the loop's
    # own value (None on a bare dict is the same "leave it alone", since `normalize` defaults it).
    for key in ("cap", "base", "grace_min", "ttl_min", "inflight_ttl_min", "turn_budget_s"):
        value = _form_value(settings, key)
        if value is not None:
            overlaid[key] = value
    if loop_concurrency is not None:
        overlaid["concurrency"] = loop_concurrency
    # Attribution (#197) moves only when the form names it, like every other knob.
    if _form_value(settings, "attribution") is not None:
        overlaid["attribution"] = d["attribution"]
    if _form_value(settings, "review_after_ci") is not None:
        overlaid["review_after_ci"] = d["review_after_ci"]
    if str(d.get("merge_check") or "").strip():
        overlaid["merge_check"] = str(d["merge_check"]).strip().lower()
    if _form_value(settings, "fix_ci") is not None:
        overlaid["fix_ci"] = d["fix_ci"]
    if _form_value(settings, "fixer_scope_lock") is not None:
        overlaid["fixer_scope_lock"] = d["fixer_scope_lock"]
    if _form_value(settings, "review_only_update") is not None:
        overlaid["review_only_update"] = d["review_only_update"]
    if _form_value(settings, "review_only") is not None:
        overlaid["review_only"] = [name.strip() for name in str(d["review_only"]).split(",")
                                   if name.strip()]
    if _form_value(settings, "ci_fix_cap") is not None:
        overlaid["ci_fix_cap"] = config.check_ci_fix_cap(config._form_int(d["ci_fix_cap"].strip()), "settings")
    for key in ("review_only_cap", "review_only_daily"):
        if _form_value(settings, key) is not None:
            overlaid[key] = config._check_review_only_limit(config._form_int(d[key].strip()), key, "settings")
    if _form_value(settings, "required_checks") is not None:
        overlaid["required_checks"] = config.check_required_checks(
            config.split_check_names(d["required_checks"]), "settings")
    if _form_value(settings, "human_paths") is not None:
        overlaid["human_paths"] = config.check_human_paths(
            config.split_human_paths(d["human_paths"]), "settings")
    if _form_value(settings, "generated") is not None:
        overlaid["generated"] = config.parse_generated(
            config.split_generated(d["generated"]), "settings")
    if _form_value(settings, "fixer_check") is not None:
        overlaid["fixer_check"] = config.check_fixer_check(d["fixer_check"], "settings")
    shadow = shadow_block(overlaid.get("shadow"), _form_value(settings, "shadow_profile"),
                          _form_value(settings, "shadow_every"), "settings",
                          escalate=_form_value(settings, "shadow_escalate"))
    if shadow is not None:
        overlaid["shadow"] = shadow
    # Turn knobs (#309): validated like the CLI/loop file, and only when the form names them.
    for seat in ("reviewer", "fixer"):
        raw_steps = _form_value(settings, f"{seat}_max_steps")
        if raw_steps is not None:
            seats.setdefault(seat, {})["max_steps"] = config._check_max_steps(
                config._form_int(raw_steps), f"seats.{seat}.max_steps", "settings")
    for seat in ("reviewer", "fixer", "shadow"):
        raw_tokens = _form_value(settings, f"{seat}_daily_tokens")
        if raw_tokens is not None:
            seats.setdefault(seat, {})["daily_tokens"] = config._check_daily_tokens(
                config._form_int(raw_tokens), f"seats.{seat}.daily_tokens", "settings")
    # The shadow and triage seats' own capacity: no loop default reaches them, so the form's value
    # is written as the seat's own, with the loader's rule (>= 1).
    for seat in ("shadow", "triage"):
        raw_capacity = _form_value(settings, f"{seat}_concurrency")
        if raw_capacity is not None:
            seats.setdefault(seat, {})["concurrency"] = config.check_concurrency(
                config._form_int(raw_capacity), f"seats.{seat}.concurrency", "settings")
    weekly = _form_value(settings, "observer_weekly_report")
    if weekly is not None and str(weekly).strip():
        observer = dict(loop_raw.get("observer") or {})
        if not observer.get("route"):
            raise config.ConfigError("settings: observer_weekly_report needs the loop's observer feed — "
                              "without one there is nowhere to send the report")
        if str(weekly).strip().lower() == "off":
            observer.pop("weekly_report", None)
        else:
            try:
                observer["weekly_report"] = config.check_weekly_report(weekly)
            except ValueError as exc:
                raise config.ConfigError(f"settings: observer_weekly_report: {exc}") from None
        overlaid["observer"] = observer
    raw_cap = _form_value(settings, "fix_daily_turns")
    if raw_cap is not None:
        triage = dict(loop_raw.get("triage") or {})
        if not triage.get("fix_label"):
            raise config.ConfigError("settings: fix_daily_turns needs the loop's triage.fix_label — "
                              "without it no issue is handed to the fixer, so there is nothing "
                              "to cap")
        triage["fix_daily_turns"] = config._check_daily_turns(
            config._form_int(raw_cap), "triage.fix_daily_turns", "settings")
        overlaid["triage"] = triage
    raw_auto = _form_value(settings, "auto_fix_labels")
    raw_auto_cap = _form_value(settings, "auto_fix_daily")
    if raw_auto is not None or raw_auto_cap is not None:
        triage = dict(overlaid.get("triage") or loop_raw.get("triage") or {})
        if not triage.get("fix_label"):
            raise config.ConfigError("settings: auto_fix_labels and auto_fix_daily need the loop's "
                              "triage.fix_label — the automatic hand-off is the same path")
        if raw_auto is not None:
            triage["auto_fix_labels"] = [x.strip() for x in str(raw_auto).split(",")
                                         if x.strip()]
        if raw_auto_cap is not None:
            triage["auto_fix_daily"] = config._check_daily_turns(
                config._form_int(raw_auto_cap), "triage.auto_fix_daily", "settings")
        overlaid["triage"] = triage
    # Seat identity rides the same push: the form names who serves each seat, and a blank field
    # stays blank rather than unsetting what the loop already answered for itself.
    return apply_seats(overlaid, settings)


def apply_seats(loop_raw: dict, settings: dict | None) -> dict:
    """Overlay the form's seat identities onto a raw loop, touching nothing it does not name.

    The reviewer's login and ``reviewer_seat`` move together: the reviewer route serves the login
    named there, and a form that changed one without the other would leave the route waking a
    login whose verdicts this loop does not count. An adjudicator profile only lands on a loop that
    already has an adjudicator *route* — a profile alone cannot wake anyone, and route names belong
    to the repository, not to the form.
    """
    mapping = config.seat_mapping(settings)
    if not mapping:
        return dict(loop_raw)
    raw = {**loop_raw}
    seats = {k: dict(v or {}) for k, v in (loop_raw.get("seats") or {}).items()}
    for seat in config.SEAT_KEYS:
        entry = mapping.get(seat) or {}
        profile = str(entry.get("profile") or "")
        if profile:
            seats.setdefault(seat, {})["profile"] = profile
            # The display name follows the profile unless the loop set its own: an agent name that
            # disagrees with the profile is a prompt telling the wrong agent it is speaking.
            if not str(seats[seat].get("agent") or "").strip():
                seats[seat]["agent"] = profile.capitalize()
        if str(entry.get("login") or ""):
            seats.setdefault(seat, {})["login"] = entry["login"]
    raw["seats"] = seats
    reviewer_login = str((mapping.get("reviewer") or {}).get("login") or "")
    if reviewer_login:
        raw["reviewer_seat"] = reviewer_login
    adj_entry = mapping.get("adjudicator") or {}
    adj_profile = str(adj_entry.get("profile") or "")
    adjudicator = dict(loop_raw.get("adjudicator") or {})
    if adj_profile and adjudicator.get("route"):
        adjudicator["profile"] = adj_profile
        raw["adjudicator"] = adjudicator
    # The adjudicator identity is inert until the loop has a route to rule through.
    adj_login = str(adj_entry.get("login") or "")
    if adjudicator.get("route") and adj_login:
        seats.setdefault("adjudicator", {})["login"] = adj_login
    tokens, apps, _selected, _ignored = select_credentials(raw, settings)
    if tokens != dict(loop_raw.get("tokens") or {}):
        raw["tokens"] = tokens
    if apps != dict(loop_raw.get("apps") or {}):
        raw["apps"] = apps
    return raw


def select_credentials(loop: dict, settings: dict | None) -> tuple[dict, dict, dict, dict]:
    """Select effective form credentials before checking paths; never open a credential.

    Return tokens, apps, selected setting paths, and ignored PAT settings. Blank pairs preserve
    the loop; partial App pairs refuse. An existing App never becomes a PAT implicitly. With
    an explicit App pair, a PAT path matching the existing mapping is stale (not a second
    choice); a new/different PAT path is conflicting intent and refuses. Without an App pair,
    a PAT field targeting an existing App is stale even if missing (#1197).
    """
    mapping = config.seat_mapping(settings)
    tokens = dict(loop.get("tokens") or {})
    apps = {k: dict(v) for k, v in (loop.get("apps") or {}).items()}
    selected, ignored = {}, {}
    for role, token_key in config.TOKEN_FILE_SETTINGS.items():
        if role == "adjudicator" and not (loop.get("adjudicator") or {}).get("route"):
            continue
        entry = mapping.get(role) or {}
        login = str(entry.get("login") or (config.adjudicator_login(loop) if role == "adjudicator"
                                           else config.seat_login(loop, role)))
        path = str(entry.get("token_file") or "")
        app_id = str(entry.get("app_id") or "")
        key_file = str(entry.get("app_key_file") or "")
        if bool(app_id) != bool(key_file):
            raise config.ConfigError(f"{role}_app_id and {role}_app_key_file must be set together")
        if not (path or app_id):
            continue
        if not login:
            raise config.ConfigError(f"{token_key} or {role}_app_id is set but no {role} login is "
                                     "available — set its login first (no adjudicator login "
                                     "means the ruling has no comment identity)")
        existing_pat = next((v for k, v in tokens.items() if k.lower() == login.lower()), "")
        existing_app = next((v for k, v in apps.items() if k.lower() == login.lower()), None)
        if app_id:
            if not app_id.isascii() or not app_id.isdecimal() or int(app_id) <= 0:
                raise config.ConfigError(f"{role}_app_id must be a positive ASCII decimal App id")
            requested_app = {"app_id": int(app_id), "key_file": str(config._path(key_file))}
            unchanged_app = existing_app == requested_app
            inherited_pat = existing_pat and config._path(path) == config._path(existing_pat)
            if path and not (inherited_pat or unchanged_app):
                raise config.ConfigError(f"{token_key} conflicts with {role}_app_id / "
                                         f"{role}_app_key_file — choose PAT or App, not both")
            if path:
                ignored[token_key] = login
            for key in list(tokens):
                if key.lower() == login.lower():
                    del tokens[key]
            for key in list(apps):
                if key.lower() == login.lower():
                    del apps[key]
            apps[login] = {"app_id": int(app_id), "key_file": str(config._path(key_file))}
            selected[f"{role}_app_key_file"] = key_file
        elif existing_app:
            ignored[token_key] = login
        else:
            _map_token(tokens, login, path)
            selected[token_key] = path
    return tokens, apps, selected, ignored


def _map_token(tokens: dict, login: str, path: str) -> None:
    """Point ``login`` at ``path``, dropping any case-variant key that would shadow it."""
    for key in [k for k in tokens if str(k).lower() == login.lower() and k != login]:
        del tokens[key]
    tokens[login] = str(config._path(path))
