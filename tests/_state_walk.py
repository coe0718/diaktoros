"""The walk behind ``tests/test_state_junk_all.py`` (#2303): every public method of a
``LoopState`` class, against every junk shape in every JSON state file it reads or writes.

**Which files.** The union of (a) every ``*.json`` name spelled in a module that defines a class in
the class's MRO or a function bound on it (so a new mixin's module is scanned too), and (b) every
``*.json`` file in the state dir that a method touches (``exists``/``open``/``read_text``, or
creates), so a name built at run time is found as well: first on an empty state dir, then, until
no new name turns up, on a dir where every file found holds what the methods wrote there, so a
file read only on a branch another file's data opens is found too. ``observations.json`` and
``kv.json`` (the key-value API's path) are always in it. Each junk shape is planted in all of
them at once, and then (#2589 F8) in each file a method touched alone, beside clean companions:
the other files it touched, in each combination of their folded samples and every whole value a
method wrote there (``tests/_state_walk_seeds.py``, failing closed past a cap), so a file read
only when others hold particular valid values together is reached with junk too. A file a call
touches on junk that was not planted (a branch only junk opens) is planted on a further pass,
and one still unplanted after ``ROUNDS`` passes fails the walk.

**What is checked** for each call on junk (``Walk.check``): it does not raise (except a
``RAISES`` entry's own exception); its result conforms to the method's declared return type (its
annotation, or its ``HINTS`` entry: a method that declares none fails, since what it returns on
junk could not be checked); on a shape that holds no valid entry (``EMPTY``: a scalar top level,
a list of junk, an object of junk scalars) it returns exactly what it returns on a missing file,
as #80's readers do, whatever the top-level type (a ``KEEPS`` entry names a shape that holds a
valid part for one method); and a file it rewrote holds every path the method's own clean call
writes there, of the same JSON type and with none of the planted junk strings
(``JUNK_STRINGS``) left at it (an object or a list, and not the junk as it was, when it writes
none): healed, never junk kept. Beside companions, "a missing file" and "its own clean call" are
the call on the companions alone. The cases are in ``tests/_state_walk_cases.py``.
"""
from __future__ import annotations

import contextlib
import inspect
import json
import os
from pathlib import Path

from diaktoros import state
from _state_walk_cases import (  # noqa: F401 - re-exported for the tests
    ANY_SHAPE, ARGS, EMPTY, FLAT, HEAD, HINTS, JUNK_STRINGS, KEEPS, KEY, LIST, LOCK, METHOD_ARGS,
    NEVER, RAISES, SCALARS, SHAPES, SKIP, new_loop)
from _state_walk_seeds import fold, seeds
from _state_walk_oracle import (  # noqa: F401 - re-exported for the tests
    conforms, healing, kind, plain, returns, strings)
from _state_walk_trace import (  # noqa: F401 - re-exported for the tests
    call, public_methods, source_files, spelled_json, touches)

ROUNDS = 4          # discovery rounds on seeded dirs, and junk passes, before failing closed
_MISSING = object()


class Walk:
    """One class's walk: ``discover`` the state files, then ``run`` every shape."""

    def __init__(self, cls, root: Path):
        self.cls, self.root, self.made = cls, root, 0
        self.clean, self.wrote, self.samples, self.strays, self.files = {}, {}, {}, set(), None
        self.touched, self.bases, self.alts, self.uncovered = {}, {}, {}, set()
        self.firsts = {}

    def methods(self, only=None) -> list[str]:
        return [n for n in public_methods(self.cls)
                if n not in SKIP and (only is None or n in only)]

    def trace(self, name: str, seed: dict):
        """Call ``name`` on a fresh state dir holding ``seed`` (file -> JSON):
        ``(outcome, touched, written)``, the call's ``("ok", result)`` or ``("raised", exc)``,
        the ``*.json`` names it touched, and each file it (re)wrote: its JSON, or ``_MISSING``
        when it is not JSON."""
        self.made += 1
        st = self.cls(new_loop(self.root / f"{self.made}-{name}"))
        st.dir.mkdir(parents=True, exist_ok=True)
        folder, before = os.fspath(st.dir), {}
        for file, value in seed.items():
            path = os.path.join(folder, file)
            with open(path, "wb") as out:
                out.write(text := json.dumps(value).encode())
            os.utime(path, ns=(0, 0))              # so any write, in place too, moves it
            before[file] = (text, _stamp(path))
        with touches(st.dir) as seen:
            try:
                outcome = ("ok", call(st, name))
            except Exception as exc:  # noqa: BLE001 - recorded: the caller decides
                outcome = ("raised", exc)
        written = {}
        for file in (f for f in os.listdir(folder) if f.endswith(".json")):
            path, (was, stamp) = os.path.join(folder, file), before.get(file, (None, None))
            if stamp is not None and stamp == _stamp(path):
                continue
            with open(path, "rb") as source:
                if was == (now := source.read()):
                    continue
            try:
                written[file] = json.loads(now)
            except ValueError:
                written[file] = _MISSING
        return outcome, seen, written

    def sample(self, file: str, value, name: str) -> bool:
        """Fold a value a call of ``name`` wrote into ``file``'s sample and alternatives."""
        return fold(self.samples, self.alts, file, value, name, self.firsts)

    def clean_call(self, name: str) -> set[str]:
        """Call ``name`` on an empty dir: record its result and writes; the names it touched."""
        self.clean[name], seen, written = self.trace(name, {})
        self.wrote = {key: value for key, value in self.wrote.items() if key[0] != name}
        for file, value in written.items():
            self.wrote[(name, file)] = value
            if value is not _MISSING:
                self.sample(file, value, name)
        return seen

    def discover(self) -> list[str]:
        """The state files: every name the class's modules spell, and every file a method
        touches: on an empty dir, then, round after round until no method touches a new name,
        with each file it touched holding what the methods wrote there (so a file read only
        when another holds data is found too). Records each clean call's result and writes.
        Done once per walk; ``run`` reuses it."""
        if self.files is not None:
            return self.files
        touched = self.touched = {name: self.clean_call(name) for name in self.methods()}
        todo = set(touched)
        for _ in range(ROUNDS):
            again, grown = set(), set()
            for name in sorted(todo):
                seed = {f: self.samples[f] for f in sorted(touched[name] & set(self.samples))}
                for one in seeds(seed, self.alts, self.uncovered, f"{name} (discovery)"):
                    _, seen, written = self.trace(name, one)
                    if seen - touched[name]:
                        touched[name] |= seen
                        again.add(name)
                    grown |= {f for f, value in written.items()
                              if value is not _MISSING and self.sample(f, value, name)}
            todo = again | {name for name, seen in touched.items() if seen & grown}
            if not todo:
                break
        else:                            # still finding state after ROUNDS: fail closed
            self.uncovered |= {f"{name} (discovery): still reaching new state after {ROUNDS} "
                               "rounds, so its files may not all be found" for name in todo}
        self.files = sorted(set().union(spelled_json(self.cls), *touched.values(),
                                        {state.OBSERVATIONS_FILE, "kv.json"}))
        return self.files

    def base(self, name: str, file: str) -> list:
        """Each ``(companions, outcome, writes)`` for junk in ``file`` alone: every combination
        (``seeds``) of the valid values of the other files ``name`` touched, and the call on
        those alone (none when no other file holds one: the all-junk pass covers it)."""
        seed = {f: self.samples[f] for f in sorted(self.touched.get(name, set()) - {file})
                if f in self.samples}
        bases = []
        for companions in seeds(seed, self.alts, self.uncovered, f"{name} beside {file}"):
            if (key := (name, file, repr(companions))) not in self.bases:
                outcome, _, written = self.trace(name, companions)
                self.bases[key] = companions, outcome, {(name, f): v for f, v in written.items()}
            bases.append(self.bases[key])
        return bases

    def check(self, name: str, shape, files, base=None) -> tuple[list[str], set[str]]:
        """The call's problems with ``shape`` planted in every one of ``files`` (beside
        ``base``'s companions, its outcome and writes standing for a missing file's), and the
        ``*.json`` names it touched."""
        companions, outcome, wrote = base or ({}, self.clean.get(name, ("raised", None)), None)
        (status, result), seen, written = self.trace(
            name, {**companions, **dict.fromkeys(files, shape)})
        if status == "raised":
            allowed = RAISES[name][0] if name in RAISES else ()
            problem = f"raised {type(result).__name__}: {result}"
            return ([] if isinstance(result, allowed) else [problem]), seen
        problems, hint = [], returns(self.cls, name)
        status, clean = outcome
        if name in ANY_SHAPE or result is LOCK:
            status = "any"
        elif hint is inspect.Signature.empty:
            problems.append("declares no return type (annotate it, or add it to HINTS), so "
                            "what it returns on junk cannot be checked")
        elif not conforms(result, hint):
            problems.append(f"returned {result!r}, not {hint}")
        keeps = KEEPS.get(name, ([], ""))[0]
        if (status == "ok" and shape in EMPTY and shape not in keeps
                and plain(result) != plain(clean)):
            problems.append(f"read junk as {result!r:.90}, not as a missing file ({clean!r:.40})")
        for file, now in sorted(written.items()):
            problems += ([f"left {file} unreadable"] if now is _MISSING
                         else self.healed(name, file, now, shape, wrote))
        return problems, seen

    def healed(self, name: str, file: str, now, shape, wrote=None) -> list[str]:
        """A rewritten file's problems: it carries what the method writes on a missing file,
        with none of the planted junk where the method writes (any object or list when the
        method writes nothing there on a missing file, but never the junk left as it was)."""
        clean = (self.wrote if wrote is None else wrote).get((name, file), _MISSING)
        if clean is _MISSING:
            if not isinstance(now, (dict, list)):
                return [f"rewrote {file} as a {type(now).__name__}, not healed"]
            return [f"rewrote {file} with the junk as it was"] if now == shape else []
        if plain(now) == plain(clean):
            return []
        junk = set(JUNK_STRINGS) - strings(clean)
        return [f"rewrote {file}, which {p}" for p in healing(now, clean, junk)]

    def run(self, only=None) -> list[str]:
        """Every ``method[shape]: problem``; empty when every method (or each one in ``only``)
        survives every shape. A file a call touched on junk that was not planted (read on a
        branch only junk opens) is planted on a further pass; one still unplanted after
        ``ROUNDS`` passes is a failure (closed, never a silent pass)."""
        known, files, self.strays = self.files, set(self.discover()), set()
        self.uncovered = {u for u in self.uncovered if "(discovery)" in u}   # this run's own
        if known is not None:            # again, so a method patched since discovery is its own
            for name in self.methods(only):
                files |= (seen := self.clean_call(name))
                self.touched[name] = self.touched.get(name, set()) | seen
        for _ in range(ROUNDS):
            failures, strays = [], set()
            for name in self.methods(only):
                for shape in SHAPES:
                    problems, seen = self.check(name, shape, sorted(files))
                    strays |= seen - files
                    failures += [f"{name}[{shape!r:.30}]: {p}" for p in problems]
                for file, base in ((f, b) for f in sorted(self.touched.get(name, set()) & files)
                                   for b in self.base(name, f)):
                    for shape in SHAPES:
                        problems, seen = self.check(name, shape, [file], base)
                        strays |= seen - files
                        failures += [f"{name}[{shape!r:.30} in {file}]: {p}" for p in problems]
            failures += sorted(u for u in self.uncovered if u.split()[0] in self.methods(only))
            if not strays:
                return failures
            self.strays |= strays
            files |= strays
        return failures + [f"{file}: touched on junk, and still not planted after {ROUNDS} "
                           "passes, so never fuzzed" for file in sorted(strays)]


def _stamp(path: str):
    """What tells a rewrite apart without reading the file: a replace makes a new inode, and
    any write moves the mtime off the epoch the seed was stamped with."""
    with contextlib.suppress(OSError):
        stat = os.stat(path)
        return stat.st_ino, stat.st_mtime_ns, stat.st_size
    return None
