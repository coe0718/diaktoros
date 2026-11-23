"""How ``tests/_state_walk.py`` finds and calls a ``LoopState`` class's methods and the state
files they touch (#2303): the names its modules spell, and what a call reads, probes or creates."""
from __future__ import annotations

import ast
import builtins
import contextlib
import inspect
import os
from pathlib import Path
from unittest import mock

from diaktoros import state
from _state_walk_cases import ARGS, LOCK, METHOD_ARGS


def public_methods(cls=state.LoopState) -> list[str]:
    return sorted(name for name, _ in inspect.getmembers(cls, inspect.isfunction)
                  if not name.startswith("_"))


def source_files(cls) -> set[Path]:
    """The modules that define ``cls``'s MRO classes and every function bound on it."""
    found = set()
    for owner in [*cls.__mro__[:-1],
                  *(inspect.unwrap(f) for _, f in inspect.getmembers(cls, inspect.isfunction))]:
        with contextlib.suppress(TypeError, OSError):
            found.add(Path(inspect.getsourcefile(owner)))
    return found


def spelled_json(cls) -> set[str]:
    names = set()
    for path in source_files(cls):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names |= {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str) and n.value.endswith(".json")
                  and n.value != ".json" and "/" not in n.value and " " not in n.value}
    return names


@contextlib.contextmanager
def touches(folder: Path):
    """The ``*.json`` names in ``folder`` read, probed or created inside the block."""
    seen, before, root = set(), set(os.listdir(folder)), os.path.normpath(folder)

    def note(path):
        with contextlib.suppress(TypeError):
            head, name = os.path.split(os.fsdecode(path))
            if name.endswith(".json") and os.path.normpath(head) == root:
                seen.add(name)

    def spy(real):
        def wrapper(*args, **kwargs):
            note(args[0])
            return real(*args, **kwargs)
        return wrapper
    patches = [mock.patch.object(Path, name, spy(getattr(Path, name)))
               for name in ("exists", "is_file", "open", "read_text", "read_bytes")]
    patches += [mock.patch.object(os.path, name, spy(getattr(os.path, name)))
                for name in ("exists", "lexists", "isfile")]
    patches.append(mock.patch.object(builtins, "open", spy(builtins.open)))
    with contextlib.ExitStack() as stack:
        for patch in patches:
            stack.enter_context(patch)
        yield seen
    seen |= {n for n in set(os.listdir(folder)) - before if n.endswith(".json")}


def call(st, name: str):
    method = getattr(st, name)
    kwargs = {}
    for param in inspect.signature(method).parameters.values():
        if param.default is not param.empty or param.kind in (param.VAR_POSITIONAL,
                                                              param.VAR_KEYWORD):
            continue
        own = METHOD_ARGS.get(name, {})
        kwargs[param.name] = (own[param.name] if param.name in own
                              else st.dir / "kv.json" if param.name == "path"
                              else ARGS[param.name])
    result = method(**kwargs)
    if hasattr(result, "__enter__"):                 # a lock: take it and let it go
        with result:
            pass
        return LOCK
    return result
