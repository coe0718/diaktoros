"""A gate that cannot finish must never look like a gate that chose silence (issue #75).

What the Hermes gateway does with a route script (``gateway/platforms/webhook_filters.py``,
``run_route_script``; ``gateway/platforms/webhook.py``, ``_handle_webhook``):

* it runs ``<python> <script>`` synchronously inside the request handler, with the webhook
  payload as JSON on stdin (no headers: the ``X-GitHub-Delivery`` id never reaches the script),
  ``cwd`` = the script's directory and the gateway's scrubbed subprocess environment;
* the timeout is platform-wide ``script_timeout_seconds`` (default **30s**); on expiry the
  child is killed, so a gate cannot record its own overrun after the fact;
* non-zero exit, a timeout, empty stdout and ``[SILENT]`` are all the same answer: HTTP
  **200** ``{"status": "ignored", "reason": "script"}``. No script outcome can produce a
  non-2xx, and GitHub does not automatically redeliver failed deliveries anyway — so
  "exit non-zero and let GitHub retry" is not available to the plugin. The loop has to
  remember and retry on its own.

So every gate runs under :func:`run`, which

1. budgets the gate well under the gateway's timeout (``REVIEW_LOOP_GATE_BUDGET_S``, default
   20s): every GitHub call is clipped to what is left, a spent budget raises
   :class:`gh.GateBudgetExceeded`, and a ``SIGALRM`` backstop a few seconds later interrupts
   anything else that hangs (a lock, a subprocess);
2. records a crash, a timeout, a stop (SIGTERM), or a silence that followed a failed GitHub
   read ("incomplete") durably in the loop's ``gate-failures.json`` — event fingerprint, repo, PR, head, action,
   gate, exception type and message, a bounded traceback — with the payload (up to 1 MiB) kept
   beside it so the watchdog can re-drive the event;
3. marks the fingerprint's entry resolved the next time the same event completes cleanly
   (a watchdog re-drive, or a manual redelivery from GitHub's UI).

A crash or timeout exits non-zero (the gateway logs ``code=`` with our stderr line); an
incomplete silence still prints ``[SILENT]`` and exits 0 — the answer to GitHub is the same
200 either way, and the ledger is what makes them different.

Re-driving is safe for the reviewer and fixer gates only: they print nothing but ``[SILENT]``
(runs are enqueued in the host run ledger, whose repo/PR/head/seat/turn index dedups a second
delivery) and re-read the live PR before acting, so a stale event silences. The adjudicator
gate's stdout *is* its dispatch, which a watchdog re-run could not hand to the gateway; its
failures are alerted, never re-driven.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import math
import os
import pathlib
import re
import signal
import sys
import time
import traceback
from collections.abc import Callable

from . import config, gh
from .util import iso_at, log

LEDGER = "gate-failures.json"
PAYLOADS = "gate-failures"
DEFAULT_BUDGET_S = 20.0
BACKSTOP_S = 3.0             # SIGALRM this long after the soft budget; 20 + 3 < the gateway's 30
MAX_REDRIVES = 3
MAX_ENTRIES = 200
MAX_PAYLOAD_BYTES = 1 << 20
RESOLVED_RETENTION_S = 7 * 86400
REDRIVABLE = frozenset({"gate_reviewer", "gate_fixer"})
REDRIVE_ENV = "REVIEW_LOOP_GATE_REDRIVE"
# A GitHub answer about the resource, not a failure to read it.
_FACT_ERRORS = re.compile(r"^HTTP (404|410|422)\b")
_SECRETS = re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def _bounded(text: str, limit: int) -> str:
    text = _SECRETS.sub("[redacted]", str(text))
    return text if len(text) <= limit else "…" + text[-(limit - 1):]


def budget_s() -> float:
    try:
        value = float(os.environ.get("REVIEW_LOOP_GATE_BUDGET_S", DEFAULT_BUDGET_S))
    except ValueError:
        value = DEFAULT_BUDGET_S
    return value if 0 < value < 600 else DEFAULT_BUDGET_S


# -- the gateway's script timeout ----------------------------------------------------------
#
# The gateway reads ``script_timeout_seconds`` from its webhook platform block (default 30) and
# kills a route script when it runs out. The gate cannot be told the value, so it reads the
# same files the gateway does and fits its own budget inside it; ``doctor`` reports the fit.
#
# *Which* gateway runs a loop route (read from hermes-agent, never run):
#
# * A ``/p/<profile>/webhooks/<route>`` URL on the **host** gateway (``multiplex_profiles``, the
#   default topology) is served by the default home's webhook adapter, so the timeout is the
#   host's; ``_profile_scope`` then sets the routed profile as the context's home override and
#   ``build_subprocess_env`` → ``_apply_profile_home`` hands the script
#   ``HERMES_HOME=<root>/profiles/<profile>``.
# * A profile that opts out of the multiplexer (``gateway.standalone: true`` in its own
#   config.yaml), or any profile on a host that is not multiplexing, runs its own gateway
#   (``hermes -p <profile> gateway`` sets ``HERMES_HOME`` to the profile home), whose adapter reads
#   the *profile's* config — and the script inherits that same ``HERMES_HOME``.
#
# From inside the script those two look identical (both hand over the profile home). A standalone
# profile is known from its own config; otherwise either gateway may be the one, and the gate
# fits the *smaller* of the two limits — never a budget the actual gateway would cut short.

GATEWAY_DEFAULT_TIMEOUT_S = 30   # gateway/platforms/webhook_filters.py DEFAULT_SCRIPT_TIMEOUT_SECONDS
KEY = "script_timeout_seconds"


def _profile_standalone(home: pathlib.Path) -> bool | None:
    """``gateway.standalone: true`` in a profile's own config.yaml; ``None`` when unreadable."""
    path = home / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    except (OSError, ValueError):     # unreadable or undecodable: unknown, so fit both hosts
        return None
    if "standalone" not in text:
        return False
    try:
        data = _read_yaml(text)
    except Exception:  # noqa: BLE001
        return None
    gw = data.get("gateway") if isinstance(data, dict) else None
    return isinstance(gw, dict) and gw.get("standalone") is True


def gateway_hosts(home: pathlib.Path | None = None) -> list[tuple[str, pathlib.Path]]:
    """``(label, home)`` of every gateway that may be running a route script under ``home``
    (default: the ``HERMES_HOME`` this process inherited from its gateway)."""
    home = pathlib.Path(home) if home is not None else config.home()
    if home.parent.name != "profiles":
        return [("default gateway", home)]
    name, root = home.name, home.parent.parent
    standalone = _profile_standalone(home)
    if standalone:
        return [(f"profile {name}'s standalone gateway", home)]
    return [(f"profile {name}'s own gateway (if it runs one)", home),
            (f"host gateway multiplexing profile {name}", root)]


def effective_timeout(home: pathlib.Path | None = None) -> tuple[int | None, list[tuple]]:
    """The limit to fit: the smallest readable one among the possible hosts, plus every
    host's ``(label, home, seconds|None, where)``. ``None`` when no host could be read."""
    rows = []
    for label, host in gateway_hosts(home):
        seconds, where = gateway_script_timeout(host)
        rows.append((label, host, seconds, where))
    known = [row[2] for row in rows if row[2] is not None]
    return (min(known) if known else None), rows


class _NoYamlReader(Exception):
    """This interpreter has no YAML parser and the text is not JSON: unknown, not malformed."""


def _read_yaml(raw: str):
    for name in ("yaml", "ruamel.yaml"):
        try:
            if name == "yaml":
                import yaml as module
                return module.safe_load(raw) or {}
            from ruamel.yaml import YAML
            return YAML(typ="safe").load(raw) or {}
        except ImportError:
            continue
    try:
        return json.loads(raw)       # a JSON config is valid YAML
    except ValueError as exc:
        raise _NoYamlReader() from exc


_ENV_REF = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")


def _managed_config() -> pathlib.Path | None:
    """The administrator's overlay (``hermes_cli.managed_scope``): ``$HERMES_MANAGED_DIR`` when
    it names a directory, else ``/etc/hermes`` when it exists."""
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    directory = pathlib.Path(override) if override else pathlib.Path("/etc/hermes")
    return directory / "config.yaml" if directory.is_dir() else None


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        out[key] = (_deep_merge(out[key], value)
                    if isinstance(value, dict) and isinstance(out.get(key), dict) else value)
    return out


def _leaf(data, *keys):
    for key in keys:
        data = data.get(key) if isinstance(data, dict) else None
    return data


def gateway_script_timeout(home: pathlib.Path | None = None) -> tuple[int | None, str]:
    """``(seconds, where)`` — what the gateway will allow a route script, as it computes it.

    The gateway's layers, read from hermes-agent (``gateway/config.py`` ``load_gateway_config``,
    ``gateway/config_loader.py``), later winning:

    1. legacy ``gateway.json`` ``platforms.webhook`` — malformed is ``{}`` plus a warning;
    2. ``config.yaml`` (the administrator's managed overlay deep-merged over it), merged per
       ``merge_platform_sections``: ``gateway.platforms.webhook``, then ``platforms.webhook``,
       then ``gateway.webhook`` — a block's plain keys and its ``extra:`` merged separately, so
       an ``extra`` value from an earlier layer survives a later plain key; a malformed
       ``config.yaml`` drops this whole layer (the loader falls back to ``gateway.json``);
    3. a **top-level** ``webhook:`` block, bridged into ``extra`` last (``_bridged_keys`` with
       ``root_block``) — its plain key, then its own ``extra``;
    4. ``PlatformConfig.from_dict``: ``extra`` beats the block's plain key; the adapter reads
       ``extra.get("script_timeout_seconds", 30)`` (``gateway/platforms/webhook.py``).

    ``(None, reason)`` when the deciding value cannot be known here.
    """
    home = pathlib.Path(home) if home is not None else config.home()
    notes: list[str] = []
    plain: tuple | None = None       # (value, where) of the merged block's plain key
    extra: tuple | None = None       # (value, where) of the merged block's extra

    def layer(block, where: str) -> None:
        nonlocal plain, extra
        if not isinstance(block, dict):
            return
        if KEY in block:
            plain = (block[KEY], where)
        more = block.get("extra")
        if isinstance(more, dict) and KEY in more:
            extra = (more[KEY], where + ".extra")

    legacy = home / "gateway.json"
    try:
        text = legacy.read_text(encoding="utf-8-sig") if legacy.is_file() else ""
        data = json.loads(text) if text.strip() else {}
        layer(_leaf(data, "platforms", "webhook"), f"{legacy} platforms.webhook")
    except (OSError, ValueError) as exc:
        notes.append(f"{legacy} unreadable, ignored as the gateway ignores it "
                     f"({type(exc).__name__})")

    path = home / "config.yaml"
    user: dict | None = {}
    try:
        # The gateway opens it the same way (utf-8-sig); a file it cannot read or decode makes
        # ``load_yaml_layer`` raise, and ``load_gateway_config`` then warns and carries on with
        # gateway.json alone. So a file this cannot decode drops the same layer here.
        text = path.read_text(encoding="utf-8-sig") if path.is_file() else ""
    except UnicodeDecodeError as exc:
        notes.append(f"{path} cannot be decoded as UTF-8 ({exc.reason} at byte {exc.start}), "
                     f"so the gateway drops it and falls back to gateway.json")
        text, user = "", None
    except OSError as exc:
        notes.append(f"{path} cannot be read ({type(exc).__name__}), so the gateway drops it "
                     f"and falls back to gateway.json")
        text, user = "", None
    if text.strip():
        try:
            parsed = _read_yaml(text)
            user = parsed if isinstance(parsed, dict) else {}
        except _NoYamlReader:
            return None, (f"{path} cannot be read by this interpreter ({sys.executable}): no "
                          f"YAML reader")
        except Exception as exc:  # noqa: BLE001 - the gateway drops the whole yaml layer too
            notes.append(f"{path} malformed, so the gateway falls back to gateway.json "
                         f"({type(exc).__name__})")
            user = None
    if user is not None:
        managed_path, managed = _managed_config(), {}
        if managed_path is not None:
            try:
                parsed = _read_yaml(managed_path.read_text(encoding="utf-8-sig"))
                managed = parsed if isinstance(parsed, dict) else {}
            except _NoYamlReader:
                return None, f"{managed_path} cannot be read by this interpreter: no YAML reader"
            except Exception as exc:  # noqa: BLE001 - fail-open, as managed_scope is
                notes.append(f"{managed_path} malformed, ignored as the gateway ignores it "
                             f"({type(exc).__name__})")
        merged = _deep_merge(user, managed)

        def label(*keys) -> str:
            return (f"{managed_path}" if managed and _leaf(managed, *keys) is not None
                    else f"{path}")

        for keys in (("gateway", "platforms", "webhook"), ("platforms", "webhook"),
                     ("gateway", "webhook")):
            block = _leaf(merged, *keys)
            if isinstance(block, dict):
                where_plain = f"{label(*keys, KEY)} {'.'.join(keys)}"
                where_extra = f"{label(*keys, 'extra', KEY)} {'.'.join(keys)}"
                if KEY in block:
                    plain = (block[KEY], where_plain)
                more = block.get("extra")
                if isinstance(more, dict) and KEY in more:
                    extra = (more[KEY], where_extra + ".extra")
        top = merged.get("webhook")
        if isinstance(top, dict):                      # bridged into extra, last
            if KEY in top:
                extra = (top[KEY], f"{label('webhook', KEY)} webhook")
            more = top.get("extra")
            if isinstance(more, dict) and KEY in more:
                extra = (more[KEY], f"{label('webhook', 'extra', KEY)} webhook.extra")

    decided = extra or plain
    note = f" ({'; '.join(notes)})" if notes else ""
    if decided is None:
        return GATEWAY_DEFAULT_TIMEOUT_S, "gateway default (not set)" + note
    value, where = decided
    if isinstance(value, str):
        ref = _ENV_REF.fullmatch(value.strip())
        if ref:
            if ref.group(1) not in os.environ:
                return None, f"{where}: {KEY} is {value!r}, which cannot be resolved here"
            value = os.environ[ref.group(1)]
    try:
        return max(1, int(value)), where + note
    except (TypeError, ValueError):
        return None, f"{where}: {KEY} is not a number ({str(value)[:40]!r})"


STARTUP_S = 1.0     # interpreter start-up and imports before ``run`` starts its clock (~0.1s idle)
RECORD_S = 3.0      # the most the bookkeeping after a failure may take (lock waits included)


def plan(timeout: float, base: float | None = None) -> tuple[float, float]:
    """``(budget, backstop)`` that fit inside the gateway's timeout with room to record.

    Start-up and the whole record phase are reserved first — ``STARTUP_S + budget + backstop
    + RECORD_S <= timeout`` holds for every timeout of at least ``MIN_RECORDABLE_S`` — and the
    rest is split: the backstop takes a quarter, at most ``BACKSTOP_S``. Below
    ``MIN_RECORDABLE_S`` nothing can guarantee a record; ``doctor`` says so."""
    base = budget_s() if base is None else base
    spare = max(float(timeout) - STARTUP_S - RECORD_S, 0.4)
    backstop = min(BACKSTOP_S, spare * 0.25)
    return min(base, spare - backstop), backstop


# The lowest gateway timeout at which the full default budget, its backstop, the bookkeeping
# after a failure and the interpreter's start-up all still fit.
MIN_TIMEOUT_S = next(t for t in range(1, 601)
                     if plan(t, DEFAULT_BUDGET_S) == (DEFAULT_BUDGET_S, BACKSTOP_S))
# The lowest at which a gate can still record its own failure (a second of work at most).
MIN_RECORDABLE_S = STARTUP_S + RECORD_S + 1.0


def fingerprint(gate: str, raw: str) -> str:
    """The event's identity. The gateway never hands a script the delivery id, but a GitHub
    redelivery is byte-for-byte the same payload, so its canonical form names the event."""
    try:
        canon = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
    except Exception:
        canon = raw
    return hashlib.sha256(f"{gate}\0{canon}".encode()).hexdigest()[:16]


def describe(payload) -> dict:
    """repo / PR / head / action out of whatever arrived — never raising on a bad shape."""
    def get(obj, *keys):
        for key in keys:
            obj = obj.get(key) if isinstance(obj, dict) else None
        return obj
    if not isinstance(payload, dict):
        return {"repo": "", "pr": None, "head": "", "action": ""}
    number = get(payload, "pull_request", "number") or payload.get("number")
    head = get(payload, "pull_request", "head", "sha") or get(payload, "review", "commit_id")
    repo = get(payload, "repository", "full_name")
    return {"repo": str(repo).lower() if isinstance(repo, str) else "",
            "pr": number if type(number) is int else None,
            "head": head if isinstance(head, str) else "",
            "action": str(payload.get("action") or "")[:40]}


# -- the ledger -----------------------------------------------------------------------------


UNREADABLE = "_unreadable"   # a read-only view's stand-in for an unreadable file; never written
CORRUPT = "ledger"           # ``kind`` of the entry standing for a ledger that was moved aside
CLAIM_LEASE_S = 120.0        # a re-drive runs at most 60s; a sweep that dies frees it after this


class LedgerBusy(Exception):
    """Another process held the ledger's lock past the caller's deadline."""


class LedgerUnreadable(Exception):
    """The ledger file is not a JSON object and could not be moved aside, so nothing is
    written: the operator's only copy of the recorded failures stays where it is."""


class Ledger:
    """``gate-failures.json`` plus one payload file per entry, under one flock.

    Every writer loads through :meth:`_load_for_write` under the lock. A file that exists but
    is not a ledger (unparseable, or the wrong shape) is never overwritten in place: its bytes
    are kept as ``gate-failures.json.corrupt-<when>`` (once), and the fresh ledger starts with
    one ``kind: ledger`` entry naming the copy — never pruned — which the watchdog alerts once
    and ``explain`` shows until the operator has salvaged and deleted it.
    """

    def __init__(self, directory: pathlib.Path):
        self.dir = pathlib.Path(directory)
        self.path = self.dir / LEDGER
        self.payload_dir = self.dir / PAYLOADS

    @contextlib.contextmanager
    def _locked(self, deadline: float | None = None):
        """The ledger's flock. With ``deadline`` (``time.monotonic()``), give up at it with
        :class:`LedgerBusy` rather than wait — the gate's record phase has a hard end."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "gate-failures.lock").open("a+") as lock:
            if deadline is None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            else:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise LedgerBusy(f"{self.path} is locked by another process")
                        time.sleep(0.02)
            yield

    def _read(self) -> tuple[dict | None, str]:
        """``(entries, "")``, or ``(None, why)`` when the file exists but is not a ledger.

        The shape is checked, not just the JSON: an object of entry objects under ordinary
        keys. A file that parses but holds anything else — the ``_unreadable`` stand-in an early
        build of this module could persist, a list, an entry that is not an object — is as
        unreadable as a torn write, so a writer moves it aside instead of failing on it forever.
        """
        try:
            if not self.path.exists():
                return {}, ""
            data = json.loads(self.path.read_text())
        except Exception as exc:  # noqa: BLE001 - any unreadable file is one answer
            return None, f"{type(exc).__name__}: {_bounded(exc, 160)}"
        if not isinstance(data, dict):
            return None, f"not a JSON object ({type(data).__name__})"
        for key, value in data.items():
            if not key or key.startswith("_"):
                return None, f"holds {key!r}, which is not a gate-failure entry"
            if not isinstance(value, dict):
                return None, f"entry {key[:40]!r} is not an object ({type(value).__name__})"
        return data, ""

    def movable(self) -> tuple[bool, str]:
        """Whether a writer could move this (unreadable) file aside: ``(yes, why not)``. A
        regular file in a writable directory can be hard-linked or copied and then replaced;
        anything else (a directory in its place, a read-only state directory) cannot."""
        if not self.path.is_file():
            return False, "it is not a regular file"
        if not os.access(self.dir, os.W_OK | os.X_OK):
            return False, f"{self.dir} is not writable"
        return True, ""

    def entries(self) -> dict:
        """A read-only view. An unreadable file shows as one synthetic entry, so ``explain``
        says so before any writer has moved it aside; no writer ever saves this view."""
        data, why = self._read()
        if data is None:
            movable, why_not = self.movable()
            return {UNREADABLE: {"id": UNREADABLE, "gate": "ledger", "kind": CORRUPT,
                                 "resolved": False, "error_type": "LedgerUnreadable",
                                 "error": why, "path": str(self.path), "attempts": 1,
                                 "movable": movable, "why_not": why_not}}
        return data

    def _load_for_write(self) -> dict:
        """The entries to modify — call with the lock held. An unreadable file is moved aside
        first (raising :class:`LedgerUnreadable`, and writing nothing, if it cannot be).

        Crash-safe order: the corrupt bytes are first given a second name (a hard link, or a
        fsynced copy), and only then is the fresh ledger published over the original path by an
        atomic replace. A crash before the replace leaves the original where it was, and the
        next writer finds the copy it already made (same bytes) and reuses it — one copy, never
        a lost file; after the replace the move is complete.
        """
        data, why = self._read()
        if data is not None:
            return data
        try:
            if not self.path.is_file():
                raise OSError(f"{self.path} is not a regular file, so it cannot be copied aside")
            try:
                raw: bytes | None = self.path.read_bytes()
            except OSError:
                raw = None                      # e.g. mode 000: a hard link keeps it all the same
            copy = (_existing_copy(self.dir, raw) if raw is not None
                    else _existing_link(self.dir, self.path))
            if copy is None:
                copy = _new_copy_name(self.dir)
                try:
                    os.link(self.path, copy)
                except OSError:
                    if raw is None:
                        raise
                    _write_copy(copy, raw)
                _fsync_dir(self.dir)
        except OSError as exc:
            raise LedgerUnreadable(f"{self.path} is unreadable ({why}) and could not be copied "
                                   f"aside: {type(exc).__name__}: {exc}") from exc
        now = time.time()
        key = "ledger-" + hashlib.sha256(str(copy).encode()).hexdigest()[:12]
        data = {key: {"id": key, "gate": "ledger", "kind": CORRUPT, "resolved": False,
                      "error_type": "LedgerUnreadable", "error": why, "path": str(self.path),
                      "corrupt_copy": str(copy), "attempts": 1, "redrives": 0,
                      "redrivable": False, "payload_kept": False,
                      "first_at": now, "last_at": now}}
        self._save(data)                  # atomic replace: the original is gone only now
        log(f"gate-failure ledger {self.path} was unreadable ({why}); moved aside to {copy}")
        return data

    def _save(self, data: dict) -> None:
        from .state import _atomic_write
        if UNREADABLE in data:
            raise LedgerUnreadable("refusing to write the unreadable-ledger stand-in")
        _atomic_write(self.path, data)

    def payload(self, key: str) -> str | None:
        try:
            return (self.payload_dir / f"{key}.json").read_text()
        except OSError:
            return None

    def snapshot(self) -> dict:
        """The entries as a writer sees them (moving an unreadable file aside first)."""
        with self._locked():
            return self._load_for_write()

    def record(self, key: str, entry: dict, raw: str, deadline: float | None = None) -> dict:
        now = time.time()
        pruned: list[tuple] = []
        try:
            return self._record(key, entry, raw, deadline, now, pruned)
        finally:
            for repo, stale in pruned:        # an entry that leaves gives up its read too
                _settle_github_read(repo, stale, "dropped by the ledger's 200-entry bound")

    def _record(self, key: str, entry: dict, raw: str, deadline, now: float,
                pruned: list) -> dict:
        with self._locked(deadline):
            data = self._load_for_write()
            prior = data.get(key) if isinstance(data.get(key), dict) else {}
            merged = {**prior, **entry, "id": key, "last_at": now,
                      "first_at": prior.get("first_at") or now,
                      "attempts": int(prior.get("attempts") or 0) + 1,
                      "redrives": int(prior.get("redrives") or 0), "resolved": False}
            merged.pop("resolution", None)
            data[key] = merged
            # Bounded: drop the oldest resolved entries first, then the oldest of all.
            if len(data) > MAX_ENTRIES:
                # The pointer to a corrupt copy is never pruned while unresolved: it is the
                # operator's only way to the failures recorded before the copy was made.
                order = sorted((k for k in data
                                if data[k].get("resolved") or data[k].get("kind") != CORRUPT),
                               key=lambda k: (not data[k].get("resolved"),
                                              data[k].get("last_at") or 0))
                for stale in order[:len(data) - MAX_ENTRIES]:
                    dropped = data.pop(stale, None)
                    if isinstance(dropped, dict) and not dropped.get("resolved"):
                        pruned.append((dropped.get("repo"), stale))
                    with contextlib.suppress(OSError):
                        (self.payload_dir / f"{stale}.json").unlink()
            if raw and len(raw.encode()) <= MAX_PAYLOAD_BYTES:
                self.payload_dir.mkdir(parents=True, exist_ok=True)
                target = self.payload_dir / f"{key}.json"
                fd = os.open(target, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as out:
                    out.write(raw)
                merged["payload_kept"] = True
            else:
                merged["payload_kept"] = False
            self._save(data)
            return merged

    def update(self, key: str, fields: dict, deadline: float | None = None) -> None:
        with self._locked(deadline):
            data = self._load_for_write()
            if isinstance(data.get(key), dict):
                data[key] = {**data[key], **fields}
                self._save(data)

    def resolve(self, key: str, how: str, settle: bool = True, duplicate_of: str = "") -> bool:
        with self._locked():
            data = self._load_for_write()
            entry = data.get(key)
            if not isinstance(entry, dict) or entry.get("resolved"):
                return False
            data[key] = {**entry, "resolved": True, "resolution": how, "resolved_at": time.time(),
                         **({"duplicate_of": duplicate_of} if duplicate_of else {})}
            now = time.time()
            # Retention ages resolved entries by their own ``resolved_at``. A hand-edited or
            # older-shape write can hold a junk value there; subtracting it would raise ``TypeError``
            # out of the whole resolve (the sweep-aborting class #80 closes). Only an age we can
            # actually compute may retire an entry — an unparseable one is kept, never guessed at.
            stale: list[str] = []
            for k, v in data.items():
                if not (isinstance(v, dict) and v.get("resolved")):
                    continue
                age = _resolved_age(v, now)
                if age is not None and age > RESOLVED_RETENTION_S:
                    stale.append(k)
            for k in stale:
                data.pop(k, None)
            self._save(data)
        with contextlib.suppress(OSError):
            (self.payload_dir / f"{key}.json").unlink()
        if settle:                                   # outside our lock
            _settle_github_read(entry.get("repo"), key, f"resolved — {how}")
        return True

    # -- one sweep owns an entry's alert and re-drive (the pattern of ``breach_deliver``) ------

    def claim(self, key: str, sweep: str, *, now: float, cooldown_s: float,
              may_redrive: bool, can_drive: Callable[[dict], bool]) -> tuple[dict, bool] | None:
        """Decide, under the lock, whether *this* sweep alerts on ``key`` and re-drives it.

        The decision is re-made against the entry as it is now, not as a sweep saw it earlier,
        and committed before anything runs as a ``claim`` leased to this sweep (plus, for a
        re-drive, the incremented ``redrives``, so the cap holds even if the sweep dies). An
        overlapping sweep finds the live claim and does nothing. The alert itself is marked
        only by :meth:`release`, after the line has been said: a sweep that dies before saying
        it leaves the claim to expire, and a later sweep says it (at least once, never lost).
        Returns ``(entry, drive)``, or ``None`` for "not yours".
        """
        with self._locked():
            data = self._load_for_write()
            entry = data.get(key)
            if not isinstance(entry, dict) or entry.get("resolved"):
                return None
            held = entry.get("claim")
            if (isinstance(held, dict) and held.get("sweep") != sweep
                    and _number(held.get("until")) > now):
                return None                      # another sweep owns it right now
            fresh = (now - _number(entry.get("alerted_at")) > cooldown_s
                     or (entry.get("kind") != CORRUPT
                         and entry.get("alerted_attempts") != entry.get("attempts")))
            drive = bool(entry.get("kind") != CORRUPT and may_redrive and entry.get("redrivable")
                         and entry.get("payload_kept")
                         and int(entry.get("redrives") or 0) < MAX_REDRIVES
                         and can_drive(entry))
            if not (fresh or drive):
                return None
            entry = {**entry, "claim": {"sweep": sweep, "until": now + CLAIM_LEASE_S}}
            if drive:
                entry.update(redrives=int(entry.get("redrives") or 0) + 1, last_redrive_at=now)
            data[key] = entry
            self._save(data)
            return dict(entry), drive

    def current(self, key: str) -> dict:
        entry = self.entries().get(key)
        return dict(entry) if isinstance(entry, dict) else {}

    def release(self, key: str, sweep: str, *, said: bool, attempts=None) -> dict:
        """End this sweep's claim. ``said``: the alert line has been emitted, so mark it with
        ``attempts`` — the count that line reported (including any a re-drive added). A
        delivery recorded after the line was made is not marked: the next sweep says it."""
        with self._locked():
            data = self._load_for_write()
            entry = data.get(key)
            if not isinstance(entry, dict):
                return {}
            if (entry.get("claim") or {}).get("sweep") == sweep:
                entry = {k: v for k, v in entry.items() if k != "claim"}
                if said:
                    entry.update(alerted_at=time.time(),
                                 alerted_attempts=entry.get("attempts") if attempts is None
                                 else attempts)
                data[key] = entry
                self._save(data)
            return dict(entry)

    def with_payload_state(self, key: str, entry: dict) -> dict:
        """``entry`` plus ``payload_missing`` when its kept payload file is no longer there."""
        missing = (bool(entry.get("payload_kept"))
                   and not (self.payload_dir / f"{key}.json").is_file())
        return {**entry, "payload_missing": True} if missing else entry

    def open_for(self, number: int) -> list[dict]:
        """Unresolved failures for one PR — plus the loop-level ones every PR's ``explain``
        must show: a failure whose payload named no PR, and an unreadable or moved-aside
        ledger, which may be hiding this PR's failures."""
        return [self.with_payload_state(str(e.get("id") or k), e)
                for k, e in self.entries().items()
                if isinstance(e, dict) and not e.get("resolved")
                and (e.get("pr") == number or e.get("pr") is None
                     or e.get("kind") == CORRUPT)]


def _existing_copy(directory: pathlib.Path, raw: bytes) -> pathlib.Path | None:
    """A ``.corrupt-*`` copy already holding exactly these bytes (a move-aside that crashed
    before publishing the fresh ledger), so a retry does not make a second one."""
    for candidate in sorted(directory.glob(f"{LEDGER}.corrupt-*")):
        try:
            if candidate.stat().st_size == len(raw) and candidate.read_bytes() == raw:
                return candidate
        except OSError:
            continue
    return None


def _existing_link(directory: pathlib.Path, path: pathlib.Path) -> pathlib.Path | None:
    """A ``.corrupt-*`` name already linked to ``path`` (same inode) — for bytes that cannot be
    read, the retry after a crash finds its copy this way instead of by content."""
    for candidate in sorted(directory.glob(f"{LEDGER}.corrupt-*")):
        try:
            if os.path.samefile(candidate, path):
                return candidate
        except OSError:
            continue
    return None


def _new_copy_name(directory: pathlib.Path) -> pathlib.Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    copy, n = directory / f"{LEDGER}.corrupt-{stamp}", 1
    while copy.exists():
        n += 1
        copy = directory / f"{LEDGER}.corrupt-{stamp}-{n}"
    return copy


def _write_copy(copy: pathlib.Path, raw: bytes) -> None:
    """A durable copy where no hard link can be made; a partial one is removed."""
    fd = os.open(copy, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            copy.unlink()
        raise


def _fsync_dir(directory: pathlib.Path) -> None:
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _resolved_age(entry: dict, now: float) -> float | None:
    """Seconds since a resolved entry's ``resolved_at``, or ``None`` when it cannot be aged.

    The reading rule ``state.mark_at`` applies to ``at`` (#80), applied to ``resolved_at``: only a
    finite plain number is a time. A string (ISO or numeric), ``null``, a bool or a list is junk,
    which retention skips instead of a ``TypeError`` out of the whole ``resolve`` — and it is
    *kept*, never retired on a guess, until a writer records a real timestamp.
    """
    at = entry.get("resolved_at")
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None
    return now - float(at)


def loop_ledger(loop: dict) -> Ledger:
    return Ledger(pathlib.Path(str(loop["state_dir"])).expanduser())


def open_for(loop: dict, number: int) -> list[dict]:
    """Unresolved failures for one PR: the loop's ledger, plus what the fallback ledger holds
    for this loop's repo (a failure it took when the loop's ledger was busy or unwritable)."""
    found = loop_ledger(loop).open_for(number) if loop.get("state_dir") else []
    have = {e.get("id") for e in found}
    try:
        spare = fallback_ledger()
        repo = str(loop.get("repo") or "").lower()
        for e in spare.open_for(number):
            if e.get("kind") != CORRUPT and e.get("repo") == repo and e.get("id") not in have:
                found.append({**e, "held_in": str(spare.path)})
    except Exception as err:  # noqa: BLE001 - explain reports what it can read
        log(f"fallback gate-failure ledger unreadable: {type(err).__name__}: {err}")
    return found


def _landed(ledger: "Ledger", key: str, since: float) -> dict | None:
    """``key``'s entry in ``ledger`` if it was written at or after ``since``; never raises."""
    try:
        data, _why = ledger._read()
        entry = (data or {}).get(key)
        return entry if isinstance(entry, dict) and _number(entry.get("last_at")) >= since else None
    except Exception:  # noqa: BLE001
        return None


def _home_ledger(entry: dict) -> "Ledger | None":
    """The ledger of the loop an entry's repo belongs to, if any."""
    repo = entry.get("repo") if isinstance(entry, dict) else None
    try:
        loop = _loop_for({"repository": {"full_name": repo}}) if repo else None
        return loop_ledger(loop) if loop and loop.get("state_dir") else None
    except Exception:  # noqa: BLE001
        return None


def owner_state(loop: dict, failure: dict) -> str:
    """Whether a gate-failure entry still backs a failed read marked ``owned_by``. The same
    event can be in two ledgers (a duplicate), so every ledger it could be in is asked — the one
    ``owned_in`` names, the loop's, and the no-loop one:

    * ``"open"`` — some ledger holds it unresolved: its alert line is (or will be) said;
    * ``"resolved"`` — held, and resolved everywhere it is held (the event completed); a copy
      resolved only as a duplicate of the owner does not count — it never owned the read;
    * ``"unreadable"`` — not held by any readable ledger, and one cannot be read, so no line
      about it can come from there: the health check reports the read itself;
    * ``"gone"`` — no ledger holds it (moved aside, pruned).

    ``""`` when the read is not owned. Read-only: ``explain`` calls it."""
    owner = str(failure.get("owned_by") or "")
    if not owner.startswith("gate-failures:"):
        return ""
    key = owner.removeprefix("gate-failures:")
    paths: list[pathlib.Path] = []
    try:
        if failure.get("owned_in"):
            paths.append(pathlib.Path(str(failure["owned_in"])).parent)
        if loop.get("state_dir"):
            paths.append(loop_ledger(loop).dir)
        paths.append(fallback_ledger().dir)
    except Exception:  # noqa: BLE001
        pass
    seen, resolved, unreadable = set(), False, False
    for directory in paths:
        if directory in seen:
            continue
        seen.add(directory)
        data, _why = Ledger(directory)._read()
        if data is None:
            unreadable = True
            continue
        entry = data.get(key)
        if isinstance(entry, dict):
            if not entry.get("resolved"):
                return "open"
            if not _is_duplicate(entry):     # a settled duplicate never owned the read
                resolved = True
    return "resolved" if resolved else "unreadable" if unreadable else "gone"


def _is_duplicate(entry: dict) -> bool:
    """An entry resolved as a duplicate of the same event held by another ledger. It never
    owned the read, so it says nothing about whether the owner resolved."""
    return bool(entry.get("duplicate_of")) or str(entry.get("resolution") or "").startswith(
        "duplicate of the same event in ")


def _settle_github_read(repo, key: str, how: str) -> None:
    """The failed read an entry owned (``owned_by``) is settled when the entry leaves: marked
    ``resolved_by`` instead, so ``explain`` stops promising a gate-failure line and the health
    sweep knows why it need not announce it. Only the read this very entry owned is touched."""
    if not repo:
        return
    try:
        loop = _loop_for({"repository": {"full_name": repo}})
        if not loop or not loop.get("state_dir"):
            return
        from . import state as state_mod
        st = state_mod.state_for(loop)
        failure = st.github_failure()
        if failure.get("owned_by") != f"gate-failures:{key}":
            return
        settled = {k: v for k, v in failure.items() if k not in ("owned_by", "owned_in")}
        st.github_failure_record({**settled, "resolved_by": f"gate-failures:{key} {how}"})
    except Exception as err:  # noqa: BLE001 - diagnostic only
        log(f"could not settle the owned failed read: {type(err).__name__}: {err}")


def fallback_ledger() -> Ledger:
    """For a failure before any loop could be named (a malformed payload, a broken config)."""
    return Ledger(config.home() / "state" / "review-loop-gate-failures")


def _loop_for(payload) -> dict | None:
    repo = describe(payload)["repo"]
    if not repo:
        return None
    try:
        return config.by_repo(repo)
    except Exception:
        return None


def _ledgers_for(payload) -> list[Ledger]:
    """Where to record: the loop's ledger, then the fallback. Never raises — it runs on the
    failure path, where an exception here would lose the very record it is looking for."""
    ledgers: list[Ledger] = []
    try:
        loop = _loop_for(payload)
        if loop:
            ledgers.append(loop_ledger(loop))
    except Exception as err:  # noqa: BLE001
        log(f"gate-failure ledger for the loop not found: {type(err).__name__}: {err}")
    try:
        ledgers.append(fallback_ledger())
    except Exception as err:  # noqa: BLE001
        log(f"fallback gate-failure ledger not found: {type(err).__name__}: {err}")
    return ledgers


# -- the guard ------------------------------------------------------------------------------


def _claim_github_read(payload, key: str, since: float, held_in: str = "") -> None:
    """One owner per failed GitHub read (#75 with #54). A read a gate made, and this ledger
    recorded as the event's failure, is alerted and re-driven from here; ``github-reads.json``
    keeps it only as the "last failed call" diagnostic (``explain``), marked ``owned_by`` so the
    watchdog's GitHub-health sweep does not announce the same read a second time."""
    loop = _loop_for(payload)
    if not loop or not loop.get("state_dir"):
        return
    try:
        from . import state as state_mod
        st = state_mod.state_for(loop)
        failure = st.github_failure()
        at = failure.get("at")
        if isinstance(at, (int, float)) and not isinstance(at, bool) and at >= since:
            st.github_failure_record({**failure, "owned_by": f"gate-failures:{key}",
                                      "owned_in": held_in})
    except Exception as err:  # noqa: BLE001 - the health sweep then reports it; never silent
        log(f"could not mark the failed read as owned: {type(err).__name__}: {err}")


class GateStopped(BaseException):
    """The gate was told to stop (SIGTERM: a gateway or systemd stop, an operator's kill).
    A ``BaseException`` so no gate's own ``except Exception`` can swallow it."""


def _stopped(signum, _frame):
    raise GateStopped(f"gate process received {signal.Signals(signum).name} while running "
                      f"(a gateway stop or restart, or a kill)")


def _backstop(_signum, _frame):
    raise gh.GateBudgetExceeded("gate exceeded its hard time budget (not in a GitHub read)")




class _RecordTimeout(Exception):
    """Raised into the post-failure bookkeeping when it overruns; every step there catches
    ``Exception`` and moves on, so the gate still exits before the gateway kills it."""


def _record_overrun(_signum, _frame):
    raise _RecordTimeout(f"post-failure bookkeeping exceeded {RECORD_S:g}s")


def run(gate: str, main: Callable[[], None]) -> None:
    """Run one gate's ``main`` with a budget, and never let a failure pass as ``[SILENT]``."""
    raw = sys.stdin.read()
    sys.stdin = io.StringIO(raw)
    started = time.monotonic()
    started_wall = time.time()
    try:
        limit, _rows = effective_timeout()
    except Exception:  # noqa: BLE001 - never let the fit check stop the gate
        limit = None
    budget, backstop = plan(limit or GATEWAY_DEFAULT_TIMEOUT_S)
    gh.begin_gate(started + budget)
    alarm = hasattr(signal, "setitimer")
    if alarm:
        signal.signal(signal.SIGALRM, _backstop)
        signal.setitimer(signal.ITIMER_REAL, budget + backstop)
    signal.signal(signal.SIGTERM, _stopped)
    kind, exc, code = "", None, 0
    try:
        try:
            main()
        finally:
            if alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
            # The bookkeeping below is short and bounded; a second stop must not cut it off.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except GateStopped as stop:
        kind, exc = "stopped", stop
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
        if code != 0:
            kind, exc = "crash", stop
    except gh.GateBudgetExceeded as over:
        kind, exc = "timeout", over
    except Exception as crash:  # noqa: BLE001 - the whole point: nothing escapes unrecorded
        kind, exc = "crash", crash
    elapsed = time.monotonic() - started
    read_errors = gh.end_gate()
    failed_reads = [e for e in read_errors if not _FACT_ERRORS.match(e[2])]
    if not kind and failed_reads:
        kind = "incomplete"
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None
    key = fingerprint(gate, raw)
    redrive = os.environ.get(REDRIVE_ENV, "")
    if not kind:
        how = (f"re-driven by the watchdog; completed at {iso_at(time.time())}" if redrive
               else f"event completed on a later delivery at {iso_at(time.time())}")
        for ledger in _ledgers_for(payload):
            try:
                ledger.resolve(key, how)
            except Exception as err:  # noqa: BLE001
                log(f"gate-failure ledger resolve failed: {type(err).__name__}: {err}")
        # The event completed: a read it owned is settled even if no ledger still held the
        # entry (a moved-aside ledger discards what it could not read).
        _settle_github_read(describe(payload)["repo"], key, f"resolved — {how}")
        raise SystemExit(code)

    if alarm:
        signal.signal(signal.SIGALRM, _record_overrun)
        signal.setitimer(signal.ITIMER_REAL, RECORD_S)
    record_start = time.monotonic()
    facts = describe(payload)
    if kind == "incomplete":
        error_type = "GitHubReadFailed"
        message = "; ".join(f"{m} {p}: {e}" for m, p, e in failed_reads[:4])
        trace = ""
    else:
        error_type = type(exc).__name__
        message = str(exc) if not isinstance(exc, SystemExit) else f"exit code {code}"
        trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    route = ""
    try:
        from .route_intent import routes_of
        loop = _loop_for(payload)
        route = routes_of(loop).get(gate.removeprefix("gate_"), "") if loop else ""
    except Exception:  # noqa: BLE001 - the hint then names the route generically
        pass
    entry = {"gate": gate, "kind": kind, **facts, "error_type": error_type, "route": route,
             "error": _bounded(message, 500), "traceback": _bounded(trace, 4000),
             "elapsed_s": round(elapsed, 2), "budget_s": budget,
             "redrivable": gate in REDRIVABLE, "released_claims": []}
    # The record comes first, and no lock may make it miss the gateway's kill: each ledger
    # gets an equal share of what is left of the record phase (the last 0.5s is kept for the
    # seat release and the exit), and a busy one hands over to the next — the fallback ledger,
    # which every watchdog run sweeps.
    recorded, kept, used = "", {}, None
    ledgers = _ledgers_for(payload)
    why_here = ""
    record_wall = time.time()
    for i, ledger in enumerate(ledgers):
        left = record_start + RECORD_S - 0.5 - time.monotonic()
        try:
            kept = ledger.record(key, {**entry, **({"recorded_here_because": why_here}
                                                   if why_here else {})},
                                 raw, deadline=time.monotonic() + max(0.05, left / (len(ledgers) - i)))
            recorded, used = str(ledger.path), ledger
            break
        except BaseException as err:  # noqa: BLE001 - refused, or took it and then raised?
            # ``record`` can raise after its write has landed (the directory fsync after the
            # atomic replace, or the record-phase alarm). Handing over then would put one event
            # in two ledgers — two alerts, two re-drive budgets — so look before moving on.
            landed = _landed(ledger, key, record_wall)
            if landed is not None:
                recorded, used, kept = str(ledger.path), ledger, landed
                log(f"gate-failure ledger {ledger.path} took the record, then "
                    f"{type(err).__name__}: {err} — kept there, not recorded twice")
                break
            if not isinstance(err, Exception):
                raise
            why_here = (f"{ledger.path} was busy or unwritable when this gate recorded "
                        f"({type(err).__name__}: {err})")
            log(f"gate-failure ledger write failed ({ledger.path}): {type(err).__name__}: {err}")
    freed: list[str] = []
    if kind in ("crash", "timeout", "stopped"):
        # A gate that claimed a seat and then died must not hold it until the TTL: the
        # re-drive (or the next event) has to find the seat free.
        from . import state as state_mod
        try:
            freed = state_mod.release_process_claims()
        except Exception as err:  # noqa: BLE001
            log(f"seat claim release failed: {type(err).__name__}: {err}")
    if used is not None and freed:
        try:
            used.update(key, {"released_claims": freed},
                        deadline=max(time.monotonic() + 0.05, record_start + RECORD_S - 0.2))
        except Exception as err:  # noqa: BLE001 - the claims are free; only the note is lost
            log(f"could not note the released claims: {type(err).__name__}: {err}")
    if recorded:
        _claim_github_read(payload, key, started_wall, recorded)
    where = f"#{facts['pr']}" if facts["pr"] else "an unnamed PR"
    then = ("nothing can alert on or re-drive it — " + redeliver_hint(entry) if not recorded
            else "the watchdog alerts and re-drives it"
            if kept.get("payload_kept") and gate in REDRIVABLE
            else "the watchdog alerts it; " + not_driven({**entry, **kept}))
    log(f"GATE FAILURE ({kind}) {gate} {facts['repo'] or '?'} {where}: {error_type}: "
        f"{_bounded(message, 200)} — recorded {key} in {recorded or 'NOWHERE (ledger unwritable)'}"
        f"; {then}")
    if alarm:
        signal.setitimer(signal.ITIMER_REAL, 0)
    if kind == "incomplete":
        raise SystemExit(0)   # the gate already printed its [SILENT]; the ledger tells them apart
    raise SystemExit(3 if kind == "timeout" else 143 if kind == "stopped" else 2)


# -- the watchdog's side --------------------------------------------------------------------


def redeliver_hint(entry: dict) -> str:
    """How the operator re-delivers this event from GitHub by hand, naming the gate's route."""
    seat = str(entry.get("gate") or "").removeprefix("gate_") or "gate"
    route = str(entry.get("route") or "") or f"<the loop's {seat} route>"
    what = " ".join(x for x in (str(entry.get("action") or ""),
                                f"#{entry['pr']}" if entry.get("pr") else "") if x)
    return (f"re-deliver it from GitHub: the repo's Settings → Webhooks → the hook ending in "
            f"/webhooks/{route} → Recent Deliveries → the {what or 'failed'} delivery → Redeliver")


SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def explain_status(entry: dict) -> str:
    """What happens next to an unresolved entry, for ``explain`` — the same tests the sweep
    applies, so it never promises a re-drive the sweep could not make."""
    redrives = int(entry.get("redrives") or 0)
    if (entry.get("redrivable") and entry.get("payload_kept") and not entry.get("payload_missing")
            and redrives < MAX_REDRIVES and (SCRIPTS_DIR / f"{entry.get('gate')}.py").is_file()):
        return f"{redrives} watchdog re-drive(s) so far; the next sweep retries it"
    return not_driven(entry, SCRIPTS_DIR)


def not_driven(entry: dict, scripts_dir: pathlib.Path | None = None, held: str = "") -> str:
    """Why an unresolved entry is not re-driven, as the operator should read it."""
    if not entry.get("redrivable"):
        return "not re-driven (its output is a dispatch) — " + redeliver_hint(entry)
    if not entry.get("payload_kept"):
        return (f"payload not kept (empty or over {MAX_PAYLOAD_BYTES >> 20} MiB), so it cannot be "
                f"re-driven — " + redeliver_hint(entry))
    if int(entry.get("redrives") or 0) >= MAX_REDRIVES:
        return f"gave up after {MAX_REDRIVES} re-drives — needs you"
    if entry.get("payload_missing"):
        return ("stored payload is missing (deleted from the gate-failures directory), so it "
                "cannot be re-driven — " + redeliver_hint(entry))
    if scripts_dir is not None and not (scripts_dir / f"{entry.get('gate')}.py").is_file():
        return f"{entry.get('gate')}.py not found — cannot re-drive"
    return held or "not re-driven this sweep (GitHub is not answering); a later sweep retries it"


def redrive(ledger: Ledger, key: str, gate: str, scripts_dir: pathlib.Path) -> str:
    """Re-run the gate on the stored payload, the way the gateway runs it; return an outcome.
    The caller has already claimed the re-drive (:meth:`Ledger.claim`); this only runs it."""
    import subprocess
    raw = ledger.payload(key)
    if raw is None:
        return not_driven({**(ledger.entries().get(key) or {}), "payload_missing": True})
    try:
        left = gh.remaining()
        proc = subprocess.run([sys.executable, str(scripts_dir / f"{gate}.py")], input=raw,
                              capture_output=True, text=True, cwd=str(scripts_dir),
                              timeout=60 if left is None else max(1.0, min(60.0, left)),
                              env={**os.environ, REDRIVE_ENV: key})
    except subprocess.TimeoutExpired:
        return "re-drive timed out"
    entry = ledger.entries().get(key) or {}
    if entry.get("resolved"):
        return "re-driven — completed" + (f" ({proc.stdout.strip()[:40]})" if proc.stdout.strip() else "")
    return f"re-driven — failed again (exit {proc.returncode})"


def _corrupt_line(header: str, entry: dict) -> str:
    return (f"⚠️ Review loop {header} — gate-failure ledger {entry.get('path')} was unreadable "
            f"({entry.get('error')}); it was moved aside to {entry.get('corrupt_copy')}, not "
            f"overwritten. Gate failures recorded before then are only in that copy: salvage what "
            f"you need from it, then delete it (this clears itself).")


def sweep(ledger: Ledger, header: str, scripts_dir: pathlib.Path, *, cooldown_s: float,
          may_redrive: bool = True, held: str = "",
          emit: Callable[[str], None] | None = None) -> list[str]:
    """Alert on unresolved gate failures (once per new failure, then per cooldown) and re-drive
    the re-drivable ones up to ``MAX_REDRIVES`` times.

    Overlapping sweeps are normal (one cron job per loop, each sweeping every loop), so each
    entry's decision is claimed under the ledger's lock first (:meth:`Ledger.claim`), acted on
    outside it, and its outcome recorded after (:meth:`Ledger.release`). ``emit`` says a line
    (the watchdog prints and flushes it); the alert is marked only after that returns. Without
    ``emit`` the lines are returned, and marked as they are collected. ``held``: why nothing is
    re-driven when ``may_redrive`` is false."""
    import uuid
    me = uuid.uuid4().hex
    lines: list[str] = []
    say = emit or lines.append
    snapshot = ledger.snapshot()

    def age(item) -> float:
        return _number(item[1].get("last_at")) if isinstance(item[1], dict) else 0.0
    for key, _ in sorted(snapshot.items(), key=age):
        seen = snapshot.get(key)
        if not isinstance(seen, dict) or seen.get("resolved"):
            continue
        if seen.get("kind") == CORRUPT and not pathlib.Path(str(seen.get("corrupt_copy"))).exists():
            ledger.resolve(key, f"corrupt copy removed by the operator; noticed at {iso_at(time.time())}")
            continue
        home = _home_ledger(seen)
        if home is not None and home.path != ledger.path and isinstance(home.entries().get(key), dict):
            # One event, two ledgers (a handover that raced a landed write): the loop's own
            # ledger owns it, so the alert and the re-drive budget are counted once, there.
            ledger.resolve(key, f"duplicate of the same event in {home.path}; handled there",
                           settle=False,             # the original there still owns its read
                           duplicate_of=str(home.path))
            continue
        claimed = ledger.claim(
            key, me, now=time.time(), cooldown_s=cooldown_s, may_redrive=may_redrive,
            can_drive=lambda e: ((scripts_dir / f"{e.get('gate')}.py").is_file()
                                 and (ledger.payload_dir / f"{key}.json").is_file()))
        if claimed is None:
            continue
        entry, drive = claimed
        if entry.get("kind") == CORRUPT:
            line = _corrupt_line(header, entry)
        else:
            if drive:
                try:
                    outcome = redrive(ledger, key, str(entry.get("gate")), scripts_dir)
                except BaseException:
                    ledger.release(key, me, said=False)
                    raise
                entry = ledger.current(key) or entry
            else:
                outcome = not_driven(ledger.with_payload_state(key, entry), scripts_dir,
                                     held if not may_redrive else "")
            pr = f"#{entry['pr']}" if entry.get("pr") else "no PR"
            head = str(entry.get("head") or "")[:7] or "?"
            line = (f"⚠️ Review loop {header} — gate failure {key}: {entry.get('gate')} "
                    f"{entry.get('kind')} on {pr} @ {head} ({entry.get('action') or '?'}), "
                    f"{entry.get('attempts')} attempt(s): {entry.get('error_type')}: "
                    f"{_bounded(entry.get('error') or '', 160)} — {outcome}")
        say(line)                          # a sweep killed here leaves the claim to expire
        ledger.release(key, me, said=True, attempts=entry.get("attempts"))
    return lines
