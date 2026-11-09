"""The per-seat flags ``init``, ``setup`` and ``set`` share, declared once (#1484).

Each row is one seat's setting: its flag, its type, the help every command shows, and the
commands that have it. ``add`` loops over the table to add a command's flags, so the help cannot
drift between the three parsers and a new per-seat setting is one row, not three hand copies.

A row's ``lacks`` says why a command deliberately has no such flag; ``tests/test_seat_flags.py``
checks each parser against the table. ``init`` and ``setup`` append a row's ``install_note`` as
``(default: ...)``: what an unset flag means when a loop is first written. ``set`` adds no note:
there an unset flag leaves the loop's value as it is.

Left out on purpose: the route, profile and login flags of the adjudicator, the observer and the
shadow seat. Their meaning differs between the commands (``init --adjudicator-route`` turns
adjudication on, ``set --adjudicator-route`` only names its route; ``set --shadow-profile off``
turns shadows off and shares a group with ``--no-shadow``), so one help text would be wrong for
one of them.
"""
from __future__ import annotations

from typing import NamedTuple

INSTALL = ("init", "setup")
ALL = ("init", "setup", "set")


class SeatFlag(NamedTuple):
    """One per-seat setting's flag, as every command that has it declares it."""

    flag: str
    help: str
    commands: tuple = ALL
    type: type | None = int
    install_note: str = ""
    lacks: str = ""          # why a command not in ``commands`` has no such flag
    init_default: str = ""   # the settings-form key ``init`` takes its default from

    @property
    def dest(self) -> str:
        return self.flag[2:].replace("-", "_")


_NO_SETUP_CAPACITY = "setup leaves the working seats' capacity to the loop's --concurrency"
_SET_ONLY = "a new loop starts without it; add it with `set`"

TABLE: tuple[SeatFlag, ...] = (
    SeatFlag("--reviewer-profile", "Hermes profile for the reviewer seat", INSTALL, str,
             "the plugin setting", "moving a seat to another profile is `apply`'s, with "
             "the settings form", init_default="reviewer_profile"),
    SeatFlag("--fixer-profile", "Hermes profile for the fixer seat", INSTALL, str,
             "the plugin setting", "moving a seat to another profile is `apply`'s, with "
             "the settings form", init_default="fixer_profile"),
    SeatFlag("--reviewer-concurrency", "PRs the reviewer may work at once (overrides the loop's "
             "--concurrency)", ("init", "set"), install_note="the loop's, settings "
             "{reviewer_concurrency}", lacks=_NO_SETUP_CAPACITY),
    SeatFlag("--fixer-concurrency", "PRs the fixer may work at once (overrides the loop's "
             "--concurrency)", ("init", "set"), install_note="the loop's, settings "
             "{fixer_concurrency}", lacks=_NO_SETUP_CAPACITY),
    SeatFlag("--shadow-concurrency", "shadow reviews that may run at once, >= 1 (the shadow "
             "seat's own value: the loop's --concurrency does not reach it)",
             install_note="the plugin setting, else 1"),
    SeatFlag("--triage-concurrency", "issue triages that may run at once, >= 1 (the triage "
             "seat's own value: the loop's --concurrency does not reach it)",
             install_note="the plugin setting, else 1"),
    SeatFlag("--reviewer-turn-budget", "the reviewer seat's own turn budget in seconds "
             "(overrides the loop's --turn-budget)", ("init", "set"),
             lacks="setup writes the loop's turn budget only"),
    SeatFlag("--fixer-turn-budget", "the fixer seat's own turn budget in seconds (overrides "
             "the loop's --turn-budget)", ("init", "set"),
             lacks="setup writes the loop's turn budget only"),
    SeatFlag("--shadow-turn-budget", "a shadow review's own turn budget in seconds (0 = the "
             "loop's)", ("set",), lacks=_SET_ONLY),
    SeatFlag("--reviewer-daily-turns", "most reviewer turns per day on this loop; later ones "
             "wait for midnight (0 removes the cap)", ("set",), lacks=_SET_ONLY),
    SeatFlag("--fixer-daily-turns", "most fixer turns per day on this loop (0 removes the cap)",
             ("set",), lacks=_SET_ONLY),
    SeatFlag("--shadow-daily-turns", "most shadow reviews per day on this loop (0 removes the "
             "cap)", ("set",), lacks=_SET_ONLY),
    SeatFlag("--reviewer-daily-tokens", "most tokens (in + out) the reviewer may spend per day "
             "on this loop; once spent, the next turn waits for midnight (0 = none)",
             install_note="the plugin setting"),
    SeatFlag("--fixer-daily-tokens", "most tokens the fixer (and issue fixes) may spend per day "
             "on this loop (0 = none)", install_note="the plugin setting"),
    SeatFlag("--shadow-daily-tokens", "most tokens shadow reviews may spend per day on this "
             "loop; once spent, the next waits for midnight (0 = none)",
             install_note="the plugin setting"),
    SeatFlag("--reviewer-max-steps", "agent steps one reviewer turn may take, 8-200 (0 = "
             "default 60)", install_note="the plugin setting"),
    SeatFlag("--fixer-max-steps", "agent steps one fixer or issue-fix turn may take, 8-200 "
             "(0 = default 80)", install_note="the plugin setting"),
    SeatFlag("--shadow-max-steps", "agent steps one shadow review may take, 8-200 (0 = default "
             "100)", ("set",), lacks=_SET_ONLY),
)


def help_for(row: SeatFlag, command: str, d: dict | None = None) -> str:
    """The help ``command`` shows for ``row``: the shared text, and on ``init`` and ``setup``
    the row's install note (its ``{key}`` placeholders filled from the settings form ``d``)."""
    if command not in INSTALL or not row.install_note:
        return row.help
    return f"{row.help} (default: {row.install_note.format(**(d or {}))})"


def rows(command: str) -> tuple[SeatFlag, ...]:
    """The table's rows ``command`` has a flag for."""
    return tuple(row for row in TABLE if command in row.commands)


def add(parser, command: str, d: dict | None = None) -> None:  # noqa: ANN001
    """Add ``command``'s per-seat flags to ``parser``, from the table.

    Every flag's default is None (``""`` for a string), so an unset one is told apart from a
    given one; only ``init`` takes a row's ``init_default`` from the settings form ``d``.
    """
    for row in rows(command):
        default = None if row.type is int else ""
        if command == "init" and row.init_default:
            default = (d or {}).get(row.init_default, default)
        parser.add_argument(row.flag, type=row.type, default=default,
                            help=help_for(row, command, d))


def forwarded() -> tuple[str, ...]:
    """The ``dest`` of each numeric per-seat flag ``setup`` passes on to ``init`` as given."""
    return tuple(row.dest for row in rows("setup") if row.type is int and "init" in row.commands)
