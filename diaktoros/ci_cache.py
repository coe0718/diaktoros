"""On-disk answers about immutable GitHub objects, for the CI-fix prompts (#1074).

A completed Actions job's log and a commit's file list never change, yet the fixer's and the
reviewer's prompts re-read them every turn. A caller keeps what it read here, under the loop's
``state_dir`` in ``ci-cache/``, keyed by what names the object (repository, kind, ids). Nothing
here decides what is immutable: the caller stores only objects it knows are final (a job whose
``status`` is ``completed``, a commit sha), so an entry never expires.

Disposable and best effort: a missing, unreadable or malformed entry is a miss, and a failed
write is dropped. Files are mode 0600 and hold only GitHub data (a log tail is untrusted text and
is quoted again when it is shown), never a token. Nothing is written unless the loop's
``state_dir`` already exists, so a worker never creates host state (``hostdirs``).
"""
from __future__ import annotations

import hashlib
import json
import pathlib

from . import config
from .util import atomic_write

DIRNAME = "ci-cache"
MAX_BYTES = 200_000          # an entry bigger than this, as stored, is not kept


def _root(loop: dict) -> pathlib.Path | None:
    """The cache directory, or None when the loop has no ``state_dir``."""
    return config.state_dir(loop) / DIRNAME if loop.get("state_dir") else None


def _file(root: pathlib.Path, loop: dict, parts: tuple) -> pathlib.Path:
    name = json.dumps([str(loop.get("repo")), *map(str, parts)])
    return root / hashlib.sha256(name.encode()).hexdigest()


def load(loop: dict, *parts) -> dict | None:
    """The stored entry for ``parts`` (this loop's repository, then the key), or None."""
    root = _root(loop)
    if root is None:
        return None
    try:
        entry = json.loads(_file(root, loop, parts).read_text())
    except (OSError, ValueError, RecursionError):   # deeply nested JSON raises RecursionError
        return None
    return entry if isinstance(entry, dict) else None


def save(loop: dict, *parts, value: dict) -> None:
    """Keep ``value`` for ``parts``; best effort, never raises on a filesystem problem."""
    root = _root(loop)
    if root is None or not root.parent.is_dir():
        return                    # no state directory: the host never made it, so this does not
    try:
        data = json.dumps(value, ensure_ascii=False).encode()
        if len(data) > MAX_BYTES:
            return
        root.mkdir(mode=0o700, exist_ok=True)
        atomic_write(_file(root, loop, parts), data)
    except (OSError, TypeError, ValueError):
        return
