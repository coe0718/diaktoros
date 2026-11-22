"""The walk behind ``tests/test_state_junk_all.py`` (#2303): every public method of a
``LoopState`` class, against every junk shape in every JSON state file it reads or writes.

**Which files.** The union of (a) every ``*.json`` name spelled in a module that defines a class in
the class's MRO or a function bound on it (so a new mixin's module is scanned too), and (b) every
``*.json`` file in the state dir that a method touches when it is called on an empty state dir:
the walk records ``exists``/``open``/``read_text`` of such a path and any file a call creates, so a
name built at run time is found as well. ``observations.json`` and ``kv.json`` (the key-value
API's path) are always in it. Each junk shape is planted in all of them at once.

**What is checked** for each call on junk (``Walk.check``): it does not raise (except a
``RAISES`` entry's own exception); its result conforms to the method's declared return type (its
annotation, or its ``HINTS`` entry: a method that declares none fails, since what it returns on
junk could not be checked); on a top level no state file holds (null, a string, a number, a
bool) it returns exactly what it returns on a missing file, as #80's readers do; and a file it
rewrote is well-formed JSON of the top-level type the method's own clean call writes there (a
dict or a list when it writes none): healed, never junk kept. The cases are in
``tests/_state_walk_cases.py``.
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import inspect
import json
import math
import os
import re
from pathlib import Path
import types
import typing
from unittest import mock

from diaktoros import state
from _state_walk_cases import (  # noqa: F401 - re-exported for the tests
    ANY_SHAPE, ARGS, HEAD, HINTS, KEY, LOCK, METHOD_ARGS, NEVER, RAISES, SCALARS, SHAPES, SKIP,
    new_loop)


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
    seen, before = set(), set(os.listdir(folder))

    def note(path):
        with contextlib.suppress(TypeError):
            path = Path(os.fsdecode(path))
            if path.parent == folder and path.name.endswith(".json"):
                seen.add(path.name)

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


def conforms(value, hint) -> bool:
    """Whether ``value`` is of the return annotation ``hint`` (unions, generics, finite floats)."""
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if hint is None or hint is type(None):
        return value is None
    if origin in (typing.Union, types.UnionType):
        return any(conforms(value, arg) for arg in args)
    if hint is float:
        return type(value) in (int, float) and math.isfinite(value)
    if hint in (int, str):
        return type(value) is hint
    if origin is tuple:
        return (isinstance(value, tuple) and len(value) == len(args)
                and all(conforms(v, a) for v, a in zip(value, args)))
    if origin is list:
        return isinstance(value, list) and all(conforms(v, args[0]) for v in value)
    return isinstance(value, origin or hint)


def returns(cls, name: str):
    """The method's resolved return annotation (or its ``HINTS`` entry), else
    ``inspect.Signature.empty``."""
    if name in HINTS:
        return HINTS[name]
    try:
        return typing.get_type_hints(inspect.unwrap(getattr(cls, name))).get(
            "return", inspect.Signature.empty)
    except Exception:  # noqa: BLE001 - unresolvable: reported as no declared return type
        return inspect.Signature.empty


class Walk:
    """One class's walk: ``discover`` on an empty state dir, then ``run`` every shape."""

    def __init__(self, cls, root: Path):
        self.cls, self.root, self.clean, self.writes, self.used = cls, root, {}, {}, set()

    def fresh(self, name: str):
        st = self.cls(new_loop(self.root / name))
        st.dir.mkdir(parents=True, exist_ok=True)
        return st

    def discover(self) -> list[str]:
        for name in public_methods(self.cls):
            if name in SKIP:
                continue
            st = self.fresh(f"clean-{name}")
            with touches(st.dir) as seen:
                try:
                    self.clean[name] = ("ok", call(st, name))
                except Exception as exc:  # noqa: BLE001 - recorded: a clean raise is reported
                    self.clean[name] = ("raised", exc)
            self.used |= seen
            for file in seen & set(os.listdir(st.dir)):
                with contextlib.suppress(ValueError):
                    kind = type(json.loads((st.dir / file).read_text()))
                    self.writes.setdefault((name, file), set()).add(kind)
        self.files = sorted(self.used | spelled_json(self.cls)
                            | {state.OBSERVATIONS_FILE, "kv.json"})
        return self.files

    def check(self, name: str, shape, st, before: dict) -> list[str]:
        allowed = RAISES[name][0] if name in RAISES else ()
        try:
            result = call(st, name)
        except allowed:
            return []
        except Exception as exc:  # noqa: BLE001 - the failure is the finding
            return [f"raised {type(exc).__name__}: {exc}"]
        problems, hint = [], returns(self.cls, name)
        status, clean = self.clean.get(name, ("raised", None))
        if name in ANY_SHAPE or result is LOCK:
            status = "any"
        elif hint is inspect.Signature.empty:
            problems.append("declares no return type (annotate it, or add it to HINTS), so "
                            "what it returns on junk cannot be checked")
        elif not conforms(result, hint):
            problems.append(f"returned {result!r}, not {hint}")
        if status == "ok" and shape in SCALARS and _plain(result) != _plain(clean):
            problems.append(f"read junk as {result!r}, not as a missing file ({clean!r})")
        for path, was in before.items():
            now = path.read_bytes() if path.exists() else was
            if now == was:
                continue
            try:
                kind = type(json.loads(now))
            except ValueError:
                problems.append(f"left {path.name} unreadable")
                continue
            healed = self.writes.get((name, path.name)) or {dict, list}
            if kind not in healed:
                problems.append(f"rewrote {path.name} as a {kind.__name__}, not healed")
        return problems

    def run(self, only=None) -> list[str]:
        """Every ``method[shape]: problem``; empty when every method (or each one in ``only``)
        survives every shape."""
        files, failures = self.discover(), []
        for name in public_methods(self.cls):
            if name in SKIP or (only is not None and name not in only):
                continue
            for n, shape in enumerate(SHAPES):
                st = self.fresh(f"{name}-{n}")
                before = {}
                for file in files:
                    (st.dir / file).write_text(json.dumps(shape))
                    before[st.dir / file] = (st.dir / file).read_bytes()
                failures += [f"{name}[{shape!r:.30}]: {p}"
                             for p in self.check(name, shape, st, before)]
        return failures


def _plain(value):
    """``value`` with its timestamps masked, for comparing two calls a moment apart."""
    return re.sub(r"[0-9]+(?:[.][0-9]+)?", "#", json.dumps(value, sort_keys=True, default=repr))
