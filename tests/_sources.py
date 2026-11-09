"""The Python the source-scan guard tests read: every ``*.py`` in the repository but the suite.

The "one home" scans (the read-only ledger URI, ``util.iso_epoch``, ``util.atomic_write``) each
listed their roots by hand, ``diaktoros/`` and ``scripts/``, so a forbidden spelling in
``dashboard/`` (runtime code), ``tools/``, ``docs/site/`` or the top-level ``__init__.py`` passed
them (#1489). They read this list instead; a file a rule exempts on purpose is named in that
test's own exceptions, never left out of the walk.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Not the repository's own code: the suite itself (top-level ``tests/``), and what a checkout or
# a local run leaves behind (all in ``.gitignore``).
SKIPPED_TOP = frozenset({"tests"})
SKIPPED_ANYWHERE = frozenset({"__pycache__", ".git", ".venv", "venv"})


def python_sources(root: Path = ROOT) -> list[Path]:
    """Every ``*.py`` under ``root``, sorted, apart from ``tests/`` and the skipped directories."""
    return sorted(path for path in root.rglob("*.py")
                  if path.relative_to(root).parts[0] not in SKIPPED_TOP
                  and not SKIPPED_ANYWHERE & set(path.relative_to(root).parts))
