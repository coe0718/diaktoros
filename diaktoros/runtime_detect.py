"""Find the four host paths ``diaktoros-runtime.json`` names, and write the file (#215).

``setup`` uses this so a first install never asks the operator to hand-write the runtime file:

* ``venv``: the virtualenv Hermes runs from — this interpreter's own ``sys.prefix`` when it holds
  ``bin/hermes`` (the plugin runs inside Hermes), else the one ``hermes`` on ``PATH`` lives in,
  else the checkout's own ``venv/`` or ``.venv/``. A packaged install runs the ``hermes`` command
  on a bundled Python outside any venv, so the checkout is where its venv is found;
* ``source``: the hermes-agent Git checkout — where ``hermes_cli`` is imported from, else the
  venv's parent — holding ``run_agent.py`` and ``.git``;
* ``runtime``: the Python installation the venv's interpreter links into. The sandbox binds it at
  its own path and nothing else, so it must hold *both* the link's literal target and where that
  resolves (a uv ``cpython-3.11-…`` alias and the ``cpython-3.11.13-…`` directory it points at
  share one parent, which is the answer then);
* ``rust``: a toolchain directory with ``bin/cargo`` — rustup's default toolchain, else a
  ``stable-*`` one, else a system cargo's prefix (never the rustup proxy in ``~/.cargo/bin``);
* ``node`` (optional, #1149): the Node install root a seat's sandbox mounts at ``/opt/node``
  (``node_root``), found here in the operator's environment. The worker never looks it up: its
  clean ``PATH`` and ``HOME`` hide it. Absent or ``""`` is none; a ``/usr`` Node needs no mount.

Every candidate is checked the way ``selftest`` checks the file (``problems``), so a detected
path that would fail there is reported, not written. Nothing here reads a credential.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys

from . import config, util
from .util import atomic_write

HOST_KEYS = ("source", "venv", "runtime", "rust")
NODE_KEY = "node"   # an optional host path: absent or "" is none (#1149)
# Keys of the runtime file that are not host paths, kept as they are when setup rewrites it.
_KEPT_KEYS = ("model", "upstream", "key_file", "seats")


def path() -> Path:
    return config.host_path("runtime")


def _real_home() -> Path:
    """The operator's own HOME: a gateway-run profile may have HOME pointed at the profile."""
    return Path(os.environ.get("HERMES_REAL_HOME") or Path.home())


def _is_hermes_venv(path: Path) -> bool:
    return not venv_missing(path)


def _venv(source: Path | None = None) -> Path | None:
    candidates = [Path(sys.prefix)]
    found = shutil.which("hermes")
    if found:
        candidates.append(Path(found).resolve().parent.parent)
    if source is not None:
        # Hermes's installer makes ``venv``; ``.venv`` is a developer's. Prefer the installer's.
        candidates += [source / "venv", source / ".venv"]
    return next((c for c in candidates if _is_hermes_venv(c)), None)


def _source(venv: Path | None) -> Path | None:
    candidates = []
    try:
        spec = importlib.util.find_spec("hermes_cli")
    except (ImportError, ValueError):
        spec = None
    if spec is not None and spec.origin:
        candidates.append(Path(spec.origin).resolve().parents[1])
    if venv is not None:
        candidates.append(venv.parent)
    for candidate in candidates:
        if (candidate / "run_agent.py").is_file() and (candidate / ".git").exists():
            return candidate
    return None


def runtime_for(venv: Path) -> Path | None:
    """The directory holding both the venv interpreter's link target and where it resolves."""
    link = venv / "bin" / "python"
    if not link.exists():
        return None
    literal = Path(os.path.normpath(link.parent / os.readlink(link))) if link.is_symlink() else link
    resolved = Path(os.path.realpath(link))
    # Each interpreter sits in <install>/bin/; the root must hold both installs.
    roots = [str(p.parent.parent) for p in (literal, resolved)]
    root = Path(os.path.commonpath(roots))
    return None if root == Path("/") else root


def _rust() -> Path | None:
    rustup = Path(os.environ.get("RUSTUP_HOME") or _real_home() / ".rustup")
    toolchains = rustup / "toolchains"
    if toolchains.is_dir():
        have = sorted(p for p in toolchains.iterdir() if (p / "bin" / "cargo").is_file())
        default = ""
        try:
            match = re.search(r'^default_toolchain\s*=\s*"([^"]+)"',
                              (rustup / "settings.toml").read_text(), re.M)
            default = match.group(1) if match else ""
        except OSError:
            pass
        for wanted in ([lambda p: p.name == default, lambda p: p.name.startswith(default + "-")]
                       if default else []) + [lambda p: p.name.startswith("stable-"),
                                              lambda p: True]:
            chosen = next((p for p in have if wanted(p)), None)
            if chosen is not None:
                return chosen
    found = shutil.which("cargo")
    if found:
        cargo = Path(found).resolve()
        if ".cargo" not in cargo.parts and cargo.parent.name == "bin":
            return cargo.parent.parent
    return None


# Install roots whose ``bin/node`` a seat already runs: the sandbox's PATH holds only /usr/bin and
# /bin (``contained``), so nothing to mount. Any other root (``/usr/local``, a ``/usr/lib`` tree
# included) is mounted at /opt/node, or the seat has no ``node`` command (#1149).
_NODE_VISIBLE = (Path("/"), Path("/usr"))


# Directories under the home that hold credentials or state, never a Node prefix. A Node tree
# *inside* one (``~/.hermes/node``, ``~/.local/share/fnm/...``) is fine; the directory itself is not.
_NODE_UNSAFE_UNDER_HOME = (".local", ".hermes", ".config", ".cache", ".ssh", ".gnupg", ".aws",
                           ".local/share", ".local/state")


def _node_root_is_unsafe(root: Path) -> bool:
    """True for a root that is a home directory (or above it) or a credential store in it (#779).

    ``<root>/bin/node`` placed at ``~/bin/node`` or ``~/.local/bin/node`` would otherwise stage
    the whole home or ``~/.local`` read-only into the seat.
    """
    for raw in _homes():
        for home in {raw, raw.resolve()}:
            if root == home or (root in home.parents and root not in _NODE_VISIBLE):
                return True
            if any(root == home / sub for sub in _NODE_UNSAFE_UNDER_HOME):
                return True
    return False


def _homes() -> set[Path]:
    """The operator's home however it is found: ``HERMES_REAL_HOME``, ``HOME``, and the account's
    own entry, which the worker's ``HOME`` (the Hermes home) does not hide (#1149)."""
    homes = {_real_home(), Path.home()}
    try:
        import pwd
        homes.add(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (ImportError, KeyError, OSError):
        pass
    return homes


def _node_install() -> Path | None:
    """The host Node install root wherever it lives (``/usr`` included), or ``None``.

    Looked for without trusting the caller's PATH alone: Hermes's own ``~/.hermes/node`` (the
    ``~/.local/bin/node`` link points into it), then ``node`` on PATH. A candidate counts only
    when it resolves to ``<root>/bin/node`` that is an executable regular file whose root is a
    Node prefix: never a home directory, ``~/.local``, ``~/.hermes`` or another credential store.
    """
    candidates = [_real_home() / ".hermes" / "node" / "bin" / "node",
                  _real_home() / ".local" / "bin" / "node"]
    # Every ``node`` on PATH, not only the first: a rejected one must not hide a real install.
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        found = shutil.which("node", path=entry) if entry else None
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        try:
            binary = candidate.resolve(strict=True)
        except (OSError, RuntimeError):     # RuntimeError: a link loop before Python 3.13
            continue
        if (binary.is_file() and os.access(binary, os.X_OK) and binary.parent.name == "bin"
                and not _node_root_is_unsafe(binary.parent.parent)):
            return binary.parent.parent
    return None


def node_mounted(root: Path) -> bool:
    """False when a seat already runs this install's ``node`` from its own PATH (/usr/bin, /bin)."""
    return root not in _NODE_VISIBLE


def node_root() -> Path | None:
    """The host Node install root to stage read-only at ``/opt/node`` (#685), or ``None``.

    ``None`` both when there is no Node and when it is a system install the sandbox already sees
    (``/usr``): nothing to mount. ``node_version`` tells those apart. Nothing is fetched; the
    choice is the host's and never an argument of a seat.
    """
    root = _node_install()
    return root if root is not None and node_mounted(root) else None


def node_problem(value: object) -> str:
    """Why a recorded ``node`` would not give seats Node; ``""`` when it would, or none is
    recorded (absent or ``""``). The rules ``_node_install`` applies to its candidates, on the
    canonical root (``node_canonical``): a link to a home or credential store is that directory."""
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        return "not a string path"
    root = node_canonical(value)
    if root is None:
        return f"{value} does not exist"
    if _node_root_is_unsafe(root):
        return f"{root} is a home directory or credential store, never a Node root"
    target = _seat_binary(root)
    if target is None or not (target.is_file() and os.access(target, os.X_OK)):
        return (f"{root / 'bin' / 'node'} is not an executable file inside {root}, reached by "
                "relative links only (a seat sees the root alone, at /opt/node)")
    return ""


def _seat_binary(root: Path) -> Path | None:
    """``root``'s ``bin/node`` as a seat resolves it with ``root`` bound at ``/opt/node``:
    each link followed here, one step at a time, inside ``root``. ``None`` for a link that is
    absolute (its host path is not in the sandbox), climbs out of ``root``, or loops (#2478
    review 2, F5 and F6); a missing file is the path that is not there."""
    here, parts, links = root, ["bin", "node"], 0
    while parts:
        part = parts.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if here == root:
                return None
            here = here.parent
            continue
        step = here / part
        if not step.is_symlink():
            here = step
            continue
        links += 1
        try:
            target = os.readlink(step)
        except OSError:
            return None
        if links > 40 or os.path.isabs(target):
            return None
        parts[:0] = Path(target).parts
    return here


def node_canonical(value: str) -> Path | None:
    """The real directory a recorded ``node`` names (every link resolved), or ``None``. What is
    checked and what is mounted are this one path, never the alias the file holds (#1149)."""
    try:
        return Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None


def recorded_node(settings: dict) -> Path | None:
    """The Node root a turn's sandbox mounts at ``/opt/node``: the runtime file's ``node``, as
    ``setup`` found it on the host (#1149). ``None`` when none is recorded, when the sandbox
    already sees it (``/usr``), or when it is no longer usable: Node is optional, so that is a
    logged note and a turn without Node, never a failed turn."""
    value = settings.get(NODE_KEY) or ""
    why = node_problem(value)
    if why:
        util.log(f"the runtime file's node is not mounted: {why}; re-run `hermes dk setup`")
        return None
    root = node_canonical(value) if value else None
    return root if root is not None and node_mounted(root) else None


def turn_paths(settings: dict) -> dict:
    """``trusted_turn.run_turn``'s host paths from the runtime file: the four ``HOST_KEYS`` and
    the recorded Node (``recorded_node``), so every caller mounts what the worker does."""
    return {**{key: Path(settings[key]) for key in HOST_KEYS}, NODE_KEY: recorded_node(settings)}


def node_version(root: Path | None = None) -> tuple[Path | None, str]:
    """``(root, version)`` of the host Node, e.g. ``v22.1.0``; ``(None, why)`` when unusable.

    Unlike ``node_root`` the root is reported for system installs too; ``node_mounted(root)``
    says whether it is staged at ``/opt/node``.
    """
    import subprocess
    root = root if root is not None else _node_install()
    if root is None:
        return None, "no Node runtime found on this host"
    try:
        done = subprocess.run([str(root / "bin" / "node"), "--version"], capture_output=True,
                              text=True, timeout=15, env={"PATH": "/usr/bin:/bin"})
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{root}/bin/node could not run ({type(exc).__name__})"
    version = done.stdout.strip()
    if done.returncode != 0 or not re.fullmatch(r"v\d+\.\d+\.\d+\S{0,40}", version):
        return None, f"{root}/bin/node --version failed"
    return root, version


_VISIBLE = "system install, already visible in the sandbox"
SETUP_NODE = "re-run `hermes dk setup` (`--node PATH` to choose one, `--node \"\"` for none)"


def node_report(settings: dict) -> tuple[str, str, str]:
    """What seats get for Node, read the way the worker reads it (``recorded_node``), for
    ``doctor`` and ``selftest`` (#1149): ``(state, detail, fix)``. ``state`` is ``"ok"`` (mounted,
    or a ``/usr`` install the sandbox sees), ``"warn"`` (a recorded Node that is unusable, or a
    host Node the runtime file does not record, so seats don't get it) or ``"none"``."""
    value = settings.get(NODE_KEY) or ""
    if value:
        why = node_problem(value)
        root, version = (None, why) if why else node_version(node_canonical(value))
        if root is None:
            return "warn", f"the runtime file's node {value}: {version}; seats get no Node", SETUP_NODE
        where = "mounted at /opt/node" if node_mounted(root) else _VISIBLE
        return "ok", f"node {version} at {root}, {where}", ""
    root, version = node_version()          # the operator's environment, not the worker's
    if root is None:
        return ("none", f"{version}; seats cannot run `node` and JavaScript test suites stay "
                "unverified", "install Node under ~/.hermes/node or on PATH, then " + SETUP_NODE)
    if not node_mounted(root):
        return "ok", f"node {version} at {root}, {_VISIBLE}", ""
    # Recorded as none (``""``) or not at all, a host Node seats don't get is a warning either
    # way: a plugin update leaves the file as it was (#1149, the maintainer's decision).
    recorded = "records no Node" if NODE_KEY in settings else "does not record it"
    return ("warn", f"node {version} at {root} is not in the runtime file (it {recorded}), so "
            "seats don't get it", SETUP_NODE)


def detect() -> dict[str, str]:
    """The host paths found on this machine; a key that could not be found is left out."""
    venv = _venv()
    source = _source(venv)
    if venv is None:
        venv = _venv(source)
    found = {"source": source, "venv": venv,
             "runtime": runtime_for(venv) if venv else None, "rust": _rust(),
             NODE_KEY: node_root()}
    return {key: str(value) for key, value in found.items() if value is not None}


def venv_missing(venv: Path) -> list[str]:
    """The files a venv must hold for a turn to start (``bin/hermes`` is what the seat runs)."""
    return [n for n in ("bin/python", "bin/hermes") if not (venv / n).exists()]


def source_head_problem(source: Path) -> str:
    """Why a turn's source snapshot of ``source`` would lack ``run_agent.py``; ``""`` when fine.

    ``trusted_turn`` exports only the HEAD tree (never the working tree) and denies the turn when
    the export has no ``run_agent.py``. So a ``.git`` that is a stale pointer, a repository with no
    commit, or a ``run_agent.py`` that is untracked all fail every turn while a plain
    ``exists()`` check passes. This asks Git the same question with the same hardened
    environment and binary the snapshot uses.
    """
    import subprocess
    env = {"PATH": "/usr/bin:/bin", "HOME": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1",
           "GIT_OPTIONAL_LOCKS": "0"}
    try:
        result = subprocess.run(["/usr/bin/git", "-C", str(source), "ls-tree", "-z",
                                 "--full-tree", "HEAD", "--", "run_agent.py"],
                                capture_output=True, env=env, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Git could not read it ({type(exc).__name__})"
    if result.returncode != 0:
        return "it has no readable committed HEAD (stale .git, or no commit yet)"
    meta = result.stdout.split(b"\t", 1)[0].split()
    if len(meta) != 3 or meta[1] != b"blob" or meta[0] not in (b"100644", b"100755"):
        return "run_agent.py is not committed in HEAD (untracked, or not a regular file)"
    return ""


def problems(settings: dict) -> dict[str, str]:
    """Why each host path would fail ``selftest``'s runtime step; an empty dict when none would."""
    from .selftest import _interpreter_outside
    out: dict[str, str] = {}
    for key in HOST_KEYS:
        if not isinstance(settings.get(key), str) or not settings[key]:
            out[key] = "not set"
    if "source" not in out:
        source = Path(settings["source"])
        if not ((source / "run_agent.py").is_file() and (source / ".git").exists()):
            out["source"] = f"{source} is not a hermes-agent Git checkout"
    if "venv" not in out:
        venv = Path(settings["venv"])
        missing = venv_missing(venv)
        if missing:
            out["venv"] = f"{venv} lacks {', '.join(missing)}"
    if "runtime" not in out:
        runtime = Path(settings["runtime"])
        if not runtime.is_dir():
            out["runtime"] = f"no directory at {runtime}"
        elif "venv" not in out:
            outside = _interpreter_outside(Path(settings["venv"]) / "bin" / "python", runtime)
            if outside:
                out["runtime"] = f"the venv interpreter points to {outside}, outside {runtime}"
    if "rust" not in out and not (Path(settings["rust"]) / "bin" / "cargo").is_file():
        out["rust"] = f"{settings['rust']}/bin/cargo does not exist"
    return out


def read() -> tuple[dict | None, str]:
    """The current runtime file as JSON (shape unchecked), or ``(None, why)``."""
    file = path()
    if not file.exists() and not file.is_symlink():
        return None, "absent"
    if file.is_symlink() or not file.is_file():
        return None, f"{file} is not a regular file"
    try:
        data = json.loads(file.read_text())
    except (OSError, ValueError) as exc:
        return None, f"{file} is not readable JSON ({type(exc).__name__})"
    return (data, "") if isinstance(data, dict) else (None, f"{file} is not a JSON object")


def write(settings: dict) -> Path:
    """Write the runtime file private (0600) and atomically; return its path."""
    file = path()
    file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(file, json.dumps(settings, indent=2, sort_keys=True) + "\n")
    return file


def merged(current: dict | None, chosen: dict[str, str]) -> dict:
    """``chosen`` host paths over the current file, keeping its model overrides."""
    out = {key: current[key] for key in _KEPT_KEYS if isinstance(current, dict) and key in current}
    out.update(chosen)
    return out
