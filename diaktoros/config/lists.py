"""The list and limit settings ``normalize`` checks (#1349, split from the core): required
checks, CI coverage, human paths, generated files and the fixer check, with the parsers and
readers of the first four, and the login lists and per-PR limits (``review_only``,
``operator_agents``, ``review_only_cap`` / ``review_only_daily``, ``ci_fix_cap``). Each reaches
the core through ``config`` (``from .. import config``), so a patch on a core rule reaches it
too; callers use ``lists.<name>``.
"""
from __future__ import annotations

import re

from .. import config


FIXER_CHECK_MAX = 500


REQUIRED_CHECKS_MAX = 50


REQUIRED_CHECK_NAME_MAX = 100


MERGE_CHECK_NAME = "diaktoros/review"     # ci.MERGE_CHECK (config imports nothing from ci)


def check_required_checks(value, where: str) -> list[str]:
    """The operator's required checks (#368): unique, printable, one-line names, exactly as GitHub
    shows them; [] means every check gates. Host-owned: never read from a PR."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise config.ConfigError(f"{where}: 'required_checks' must be a list of check names")
    names = [name.strip() for name in value]
    if (len(names) > REQUIRED_CHECKS_MAX or len(set(names)) != len(names)
            or not all(0 < len(name) <= REQUIRED_CHECK_NAME_MAX and name.isprintable()
                       for name in names)):
        raise config.ConfigError(f"{where}: 'required_checks' must be at most {REQUIRED_CHECKS_MAX} "
                          f"unique names, each one printable line of 1-{REQUIRED_CHECK_NAME_MAX} "
                          "characters")
    if any(name.lower() == MERGE_CHECK_NAME for name in names):
        # #1233: the loop never reads its own check as CI, so requiring it refuses every approval.
        raise config.ConfigError(f"{where}: 'required_checks' cannot name {MERGE_CHECK_NAME}, the loop's "
                          "own merge check; require it in the base branch's protection instead")
    return names


CI_COVERAGE_MAX = 50


CI_COVERS_MAX = 200


def check_ci_coverage(value, where: str) -> dict[str, str]:
    """The operator's map of check name -> what that check covers (#756), e.g. ``{"tests (3.14)":
    "full unit suite and harness"}``; {} = the reviewer is told nothing about it. Names are as
    GitHub shows them (no ``=`` or ``;;``, which the one-line forms use), the text one printable
    line. Host-owned: never read from a PR."""
    if value is None:
        return {}
    bad = f"{where}: 'ci_coverage' must map check names to a line saying what each covers"
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
        raise config.ConfigError(bad)
    out = {name.strip(): covers.strip() for name, covers in value.items()}
    if (len(value) > CI_COVERAGE_MAX or len(out) != len(value)
            or not all(0 < len(n) <= REQUIRED_CHECK_NAME_MAX and n.isprintable()
                       and "=" not in n and ";;" not in n for n in out)
            or not all(0 < len(c) <= CI_COVERS_MAX and c.isprintable() and ";;" not in c
                       for c in out.values())):
        raise config.ConfigError(f"{bad}: at most {CI_COVERAGE_MAX} unique names (1-"
                          f"{REQUIRED_CHECK_NAME_MAX} printable characters, no '=' or ';;'), "
                          f"each text 1-{CI_COVERS_MAX} printable characters, no ';;'")
    if any(name.lower() == MERGE_CHECK_NAME for name in out):
        raise config.ConfigError(f"{where}: 'ci_coverage' cannot name {MERGE_CHECK_NAME}, the loop's "
                          "own merge check, which the reviewer never reads as CI")
    return out


def parse_ci_coverage(entries, where: str) -> dict[str, str]:
    """``NAME=COVERS`` strings (the CLI and the form's spelling) -> ``ci_coverage``."""
    parsed: dict[str, str] = {}
    for text in entries:
        name, sep, covers = str(text).partition("=")
        if not sep:
            raise config.ConfigError(f"{where}: 'ci_coverage' entry {text!r} must be NAME=COVERS")
        if name.strip() in parsed:
            raise config.ConfigError(f"{where}: 'ci_coverage' names {name.strip()!r} twice")
        parsed[name.strip()] = covers
    return check_ci_coverage(parsed, where)


def split_ci_coverage(text) -> list[str]:
    """The settings form's one line: entries separated by ``;;``."""
    return [part.strip() for part in str(text or "").split(";;") if part.strip()]


def ci_coverage_text(coverage) -> str:
    """The inverse of ``split_ci_coverage`` (for ``show``)."""
    return " ;; ".join(f"{name}={covers}" for name, covers in (coverage or {}).items())


def ci_coverage(loop: dict) -> dict[str, str]:
    """The check name -> what it covers map; {} = off."""
    value = loop.get("ci_coverage")
    return dict(value) if isinstance(value, dict) else {}


HUMAN_PATHS_MAX = 50


HUMAN_PATH_MAX = 200


def check_human_paths(value, where: str) -> list[str]:
    """Glob patterns (#478) for paths only a human may approve: unique, printable, one line each.
    [] reserves nothing. Host-owned: never read from a PR."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise config.ConfigError(f"{where}: 'human_paths' must be a list of glob patterns")
    names = [name.strip() for name in value]
    if (len(names) > HUMAN_PATHS_MAX or len(set(names)) != len(names)
            or not all(0 < len(name) <= HUMAN_PATH_MAX and name.isprintable() for name in names)):
        raise config.ConfigError(f"{where}: 'human_paths' must be at most {HUMAN_PATHS_MAX} unique "
                          f"patterns, each one printable line of 1-{HUMAN_PATH_MAX} characters")
    return names


GENERATED_MAX = 20           # entries, and paths per entry


GENERATED_PATH_MAX = 200


GENERATED_COMMAND_MAX = 500


def check_generated(value, where: str) -> list[dict]:
    """The loop's generated files (#551): ``[{"paths": [...], "command": "..."}]``, [] = off.

    Host-owned (the loop file), never read from a PR. Paths are repo-relative, with no ``..``,
    ``.git`` or empty segment and no comma or ``=`` (the one-line forms use both); a path may
    belong to one entry only. The command is one printable line."""
    if value is None:
        return []
    bad = f"{where}: 'generated' must be a list of {{\"paths\": [...], \"command\": \"...\"}}"
    if not isinstance(value, list) or len(value) > GENERATED_MAX:
        raise config.ConfigError(f"{bad}, at most {GENERATED_MAX}")
    out, seen = [], set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"paths", "command"}:
            raise config.ConfigError(bad)
        paths, command = entry["paths"], entry["command"]
        if (not isinstance(paths, list) or not 1 <= len(paths) <= GENERATED_MAX
                or not all(isinstance(p, str) for p in paths) or not isinstance(command, str)):
            raise config.ConfigError(bad)
        paths = [p.strip() for p in paths]
        command = command.strip()
        for path in paths:
            parts = path.split("/")
            if (not 0 < len(path) <= GENERATED_PATH_MAX or not path.isprintable()
                    or path.startswith("/") or "\\" in path or "," in path or "=" in path
                    or any(part in ("", ".", "..") or part.lower() == ".git" for part in parts)):
                raise config.ConfigError(f"{where}: 'generated' path {path!r} must be repo-relative "
                                  "(no '..', '.git', empty segment, comma or '=')")
            if path in seen:
                raise config.ConfigError(f"{where}: 'generated' path {path!r} is listed twice")
            seen.add(path)
        if not command or len(command) > GENERATED_COMMAND_MAX or not command.isprintable():
            raise config.ConfigError(f"{where}: a 'generated' command must be one printable line of "
                              f"1-{GENERATED_COMMAND_MAX} characters")
        out.append({"paths": paths, "command": command})
    return out


def parse_generated(entries, where: str) -> list[dict]:
    """``PATH[,PATH...]=COMMAND`` strings (the CLI and the form's spelling) -> ``generated``."""
    parsed = []
    for text in entries:
        left, sep, command = str(text).partition("=")
        if not sep:
            raise config.ConfigError(f"{where}: 'generated' entry {text!r} must be PATH[,PATH...]=COMMAND")
        parsed.append({"paths": [p.strip() for p in left.split(",") if p.strip()],
                       "command": command})
    return check_generated(parsed, where)


def split_generated(text) -> list[str]:
    """The settings form's one line: entries separated by ``;;``."""
    return [part.strip() for part in str(text or "").split(";;") if part.strip()]


def generated_text(entries) -> str:
    """The inverse of ``split_generated`` (for ``show``)."""
    return " ;; ".join(f"{','.join(e['paths'])}={e['command']}" for e in entries or [])


def generated(loop: dict) -> list[dict]:
    """The generated-file entries; [] = the conflict shortcut is off."""
    value = loop.get("generated")
    return list(value) if isinstance(value, list) else []


def split_human_paths(text) -> list[str]:
    """A one-line list of glob patterns, comma-separated."""
    return [name.strip() for name in str(text or "").split(",") if name.strip()]


def split_check_names(text) -> list[str]:
    """A one-line list of check names: commas separate them, except inside parentheses, which
    GitHub matrix names use (``test (ubuntu-24.04, 3.11)``)."""
    names, depth, current = [], 0, []
    for ch in str(text or ""):
        if ch == "," and depth == 0:
            names.append("".join(current))
            current = []
            continue
        depth += {"(": 1, ")": -1}.get(ch, 0)
        depth = max(depth, 0)
        current.append(ch)
    names.append("".join(current))
    return [name.strip() for name in names if name.strip()]


REVIEW_ONLY_MAX = 50


_LOGIN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38})\Z")


def _check_logins(value, key: str, where: str) -> list[str]:
    """``fixers`` / ``reviewers``: a list of logins, lowercased. A bare string would be read one
    character at a time (``"rev-bot"`` -> ``r``, ``e``, ...), so any non-list is refused."""
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise config.ConfigError(f"{where}: {key!r} must be a list of GitHub logins")
    return [name.lower() for name in value]


def check_review_only(value, loop: dict, where: str) -> list[str]:
    """Review-only authors (#191): GitHub logins, lowercased, unique, and neither a fixer nor a
    reviewer — a fixer's PR is fixed, and a reviewer never reviews its own."""
    names = _check_login_list(value, "review_only", where)
    clash = sorted(set(names) & (set(loop.get("fixers") or []) | set(loop.get("reviewers") or [])))
    if clash:
        raise config.ConfigError(f"{where}: {', '.join(clash)} cannot be review-only and also a fixer "
                          "or a reviewer")
    return names


def check_operator_agents(value, loop: dict, where: str) -> list[str]:
    """``operator_agents`` (#757): logins of agents that post as the operator's own account.
    Lowercased, unique, and none of them a seat's account: a seat speaks for the loop, not for
    the operator, and one login can't be both."""
    names = _check_login_list(value, "operator_agents", where)
    clash = sorted(set(names) & config.seat_accounts(loop))
    if clash:
        raise config.ConfigError(f"{where}: {', '.join(clash)} cannot be an operator agent and also a "
                          "fixer, a reviewer or another seat's account")
    return names


def _check_login_list(value, key: str, where: str) -> list[str]:
    """A list of GitHub logins, lowercased, unique, at most ``REVIEW_ONLY_MAX`` (``review_only``
    and ``operator_agents``)."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise config.ConfigError(f"{where}: {key!r} must be a list of GitHub logins")
    # A login never contains a comma, so "a,b" is two logins on every path: the settings form
    # always split them, and `--review-only a,b` now does too (it used to fail as one login).
    parts = [part.strip().lower() for name in value for part in name.split(",")]
    names = [part for part in parts if part]
    bad = [name for name in names if not _LOGIN.match(name)]
    twice = sorted({name for name in names if names.count(name) > 1})
    problems = (["an empty entry (a blank login or a stray comma)"] if len(names) < len(parts)
                else []) + \
        ([f"not a GitHub login: {', '.join(repr(n) for n in bad)}"] if bad else []) + \
        ([f"listed twice: {', '.join(twice)}"] if twice else []) + \
        ([f"{len(names)} logins, more than {REVIEW_ONLY_MAX}"] if len(names) > REVIEW_ONLY_MAX
         else [])
    if problems:
        raise config.ConfigError(f"{where}: {key!r} must be at most {REVIEW_ONLY_MAX} unique "
                          f"GitHub logins ({'; '.join(problems)})")
    return names


def _check_review_only_limit(value, key: str, where: str) -> int | None:
    """``review_only_cap`` / ``review_only_daily``: a positive whole number, or empty (unset)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        value = config._form_int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= config.DAILY_TURNS_MAX:
        raise config.ConfigError(f"{where}: {key} must be a whole number, 1-{config.DAILY_TURNS_MAX} "
                          f"(leave it empty for the default), got {value!r}")
    return value


def check_ci_fix_cap(value, where: str) -> int | None:
    """``ci_fix_cap`` (#539): a whole number 1-10, or empty (unset: the default, 3)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, str):
        value = config._form_int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= config.CI_FIX_CAP_MAX:
        raise config.ConfigError(f"{where}: ci_fix_cap must be a whole number, 1-{config.CI_FIX_CAP_MAX} "
                          f"(leave it empty for {config.CI_FIX_CAP_DEFAULT}), got {value!r}")
    return value


def check_fixer_check(value, where: str) -> str:
    """The loop's always-run check: one printable line, at most ``FIXER_CHECK_MAX`` characters,
    or "" for none. The operator's own text; it reaches the fixer as an instruction."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise config.ConfigError(f"{where}: 'fixer_check' must be a string (one command, or \"\")")
    value = value.strip()
    if len(value) > FIXER_CHECK_MAX or not all(ch.isprintable() for ch in value):
        raise config.ConfigError(f"{where}: 'fixer_check' must be one printable line of at most "
                          f"{FIXER_CHECK_MAX} characters (chain commands with &&)")
    return value
