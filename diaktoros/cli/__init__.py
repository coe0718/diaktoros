"""``hermes dk`` — install, inspect and drive the loop from the CLI.

The plugin does not try to own the gateway. Everything the loop needs beyond its own scripts —
webhook routes, GitHub hooks, a cron entry — is written through the same config surfaces the
operator would touch by hand, so `hermes dk uninstall` is really just the inverse of
`init` and nothing lives in a place you cannot see.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile

from ..ledger import LOCK_WAIT_S
from ..config import settings_form
from .. import (
    attribution,
    config,
    doctor,
    envnames,
    gate,
    gate_shims,
    gh,
    observer,
    pr_hold,
    prompts,
    route_intent,
    routes,
    state as state_mod,
)
from . import checks as cli_checks
from . import install as cli_install
from . import lifecycle as cli_lifecycle
from . import operations as cli_operations
from . import reports as cli_reports
from . import settings as cli_settings
from ..util import logged
# Every command lives in its own module with its parser (#676): register_cli below lists them in
# help order. `cli.cmd_*` stays usable through these re-exports; the parser dispatches to the
# module's own function, so patch a command there, never here.
from . import common as cli_common
# Constants, re-exported here for doctor, backup and gate_shims, which read them as cli.<NAME>.
from .common import (  # noqa: F401
    BRANCH_PUSH_REFUSAL,
    GATE_SCRIPT,
    SHARED_JOB_NAME,
    SHARED_JOB_NAMES,
    SHIM,
    SHIM_NAME,
    _ROUTE_PROMPT,
    _hook_route_name,
)
from .checks import (
    cmd_app_webhook,
    cmd_arm,
    cmd_protect,
    cmd_corpus,
    cmd_doctor,
    cmd_selftest,
)  # noqa: F401
from .install import cmd_init, cmd_setup  # noqa: F401
from .lifecycle import (
    cmd_backup,
    cmd_migrate,
    cmd_restore,
    cmd_uninstall,
)  # noqa: F401
from .operations import (
    cmd_cleanup,
    cmd_depends,
    cmd_drain,
    cmd_observer,
    cmd_continue,
    cmd_escalate,
    cmd_explain,
    cmd_list,
    cmd_ready,
    cmd_regression,
    cmd_retry,
    cmd_review,
    cmd_status,
)
from .reports import (
    cmd_models,
    cmd_report,
    cmd_stats,
    cmd_trace,
)  # noqa: F401
from .settings import (
    cmd_apply,
    cmd_fixer_push,
    cmd_identity,
    cmd_set,
    cmd_settings,
    cmd_show,
    cmd_triage,
)

# -- verbs ----------------------------------------------------------------------


RENAMED_NOTE = ("note: `hermes review-loop` is now `hermes dk` (short for `hermes diaktoros`); "
                "the old name will be removed in a later release")


def _renamed(func):
    """``func`` run through the old command name: the rename note on stderr, then the verb."""
    def run(args):  # noqa: ANN001
        print(RENAMED_NOTE, file=sys.stderr)
        return func(args)
    return run


def register_cli(ctx, settings: dict | None = None) -> None:
    """Wire ``hermes diaktoros``, its short name ``hermes dk``, and the old ``hermes review-loop``.

    ``settings`` is the plugin-level settings form. It supplies the defaults a new loop starts
    from, and what ``apply`` pushes onto an existing loop; it never rewrites a loop behind the
    operator's back.
    """
    cli_common._SETTINGS = dict(settings or {})
    cli_common._CTX = ctx
    d = settings_form.settings_defaults(settings)
    # Both seats agreeing is the only case where a loop-level default says anything useful: when
    # they disagree the differing seat carries its value explicitly (see _init_seat_concurrency).
    both = settings_form.settings_loop_concurrency(d)

    def setup(parser) -> None:  # noqa: ANN001
        """Build the command's argparse tree.

        The framework hands this the parser for ``hermes dk`` itself — the subcommands are
        ours to create. (Claiming a subparsers action here compiles, loads, validates, and then
        quietly offers zero subcommands: ``hermes dk list`` is "unrecognized arguments".)
        """
        sub = parser.add_subparsers(dest="command", metavar="<command>")
        cli_common._PARSER = parser

        def _usage(_args) -> int:      # bare `hermes dk` prints the commands, not an error
            parser.print_help()
            return 0

        parser.set_defaults(func=_usage)
        cli_operations.add_list(sub)
        cli_install.add_init(sub, d, both)
        cli_install.add_setup(sub)
        cli_operations.add_status(sub)
        cli_reports.add_stats(sub)
        cli_reports.add_report(sub)
        cli_operations.add_review(sub)
        cli_operations.add_escalate(sub)
        cli_operations.add_depends(sub)
        cli_operations.add_regression(sub)
        cli_operations.add_explain(sub)
        cli_operations.add_ready(sub)
        cli_reports.add_trace(sub)
        cli_lifecycle.add_migrate(sub)
        cli_lifecycle.add_backup(sub)
        cli_lifecycle.add_restore(sub)
        cli_checks.add_doctor(sub)
        cli_checks.add_selftest(sub)
        cli_checks.add_corpus(sub)
        cli_reports.add_models(sub)
        cli_settings.add_set(sub)
        cli_settings.add_identity(sub)
        cli_settings.add_apply(sub)
        cli_settings.add_settings(sub)
        cli_settings.add_show(sub)
        cli_checks.add_arm(sub)
        cli_checks.add_protect(sub)
        cli_checks.add_app_webhook(sub)
        cli_settings.add_triage(sub)
        cli_settings.add_fixer_push(sub)
        cli_operations.add_retry(sub)
        cli_operations.add_continue(sub)
        cli_operations.add_drain(sub)
        cli_operations.add_observer(sub)
        cli_operations.add_cleanup(sub)
        cli_lifecycle.add_uninstall(sub)

    summary = "Diaktoros — bounded, autonomous software maintenance: review, fix, triage"
    description = ("Configure, inspect and drive Diaktoros loops. Each loop is one JSON file under "
                   "~/.hermes/diaktoros.d/, and it drives two webhook routes, two GitHub hooks "
                   "and (optionally) one cron watchdog job.")

    def deprecated(parser) -> None:  # noqa: ANN001
        """``hermes review-loop`` (#425): the same commands, each saying once that it was renamed.

        It never becomes ``_PARSER``: verbs one command runs for another go through the real name.
        """
        kept = cli_common._PARSER
        setup(parser)
        cli_common._PARSER = kept if kept is not None else parser
        if parser.get_default("func") is not None:          # the bare command prints its usage
            parser.set_defaults(func=_renamed(parser.get_default("func")))
        for action in parser._subparsers._group_actions if parser._subparsers else ():
            for verb in action.choices.values():
                func = verb.get_default("func")
                if func is not None:
                    verb.set_defaults(func=_renamed(func))

    # The old name first and the full name last: a host that keeps one registration keeps that one.
    ctx.register_cli_command("review-loop", "Renamed: use `hermes dk` (Diaktoros)", deprecated,
                             description=description)
    ctx.register_cli_command("dk", summary + " (short for `hermes diaktoros`)", setup,
                             description=description)
    ctx.register_cli_command("diaktoros", summary, setup, description=description)
