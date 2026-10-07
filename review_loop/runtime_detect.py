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
  ``stable-*`` one, else a system cargo's prefix (never the rustup proxy in ``~/.cargo/bin``).

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

from . import config

HOST_KEYS = ("source", "venv", "runtime", "rust")
# Keys of the runtime file that are not host paths, kept as they are when setup rewrites it.
_KEPT_KEYS = ("model", "upstream", "key_file", "seats")


def path() -> Path:
    return config.host_path("runtime")


def _real_home() -> Path:
    """The operator's own HOME: a gateway-run profile may have HOME pointed at the profile."""
    return Path(os.environ.get("HERMES_REAL_HOME") or Path.home())


def _is_hermes_venv(path: Path) -> bool:
    return (path / "bin" / "hermes").exists() and (path / "bin" / "python").exists()


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


def detect() -> dict[str, str]:
    """The host paths found on this machine; a key that could not be found is left out."""
    venv = _venv()
    source = _source(venv)
    if venv is None:
        venv = _venv(source)
    found = {"source": source, "venv": venv,
             "runtime": runtime_for(venv) if venv else None, "rust": _rust()}
    return {key: str(value) for key, value in found.items() if value is not None}


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
        missing = [n for n in ("bin/python", "bin/hermes") if not (venv / n).exists()]
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
    tmp = file.with_name(file.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(settings, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, file)
    return file


def merged(current: dict | None, chosen: dict[str, str]) -> dict:
    """``chosen`` host paths over the current file, keeping its model overrides."""
    out = {key: current[key] for key in _KEPT_KEYS if isinstance(current, dict) and key in current}
    out.update(chosen)
    return out
