"""Credentialed, checkout-free, exact-head Git push for one scoped PR head.

The sandbox supplies only bounded regular-file bytes, or a bounded unified diff against the
scoped head (#64: a one-line fix to a large file, a deletion); only the trusted broker uses
Git in a fresh bare repository, with isolated configuration and an exact-head lease.
"""
from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import quote

from . import attribution, broker, config, gh, util

MAX_FILES = 24
MAX_CONTENT = 128 * 1024
MAX_FILE = 64 * 1024
MAX_MESSAGE = 240
# A diff manifest (#64): its decoded size, and how many paths it may change. A diff costs what the
# change costs, so a large file is no longer out of reach; every changed path still passes the
# same path rules as a whole file, and only regular files may be added, changed or deleted.
MAX_PATCH = 512 * 1024
MAX_PATCH_PATHS = 64
# GitHub moves a PR's head a few seconds after its branch (#351: 4 s), so the PR read right after
# a confirmed push can still show the old head. Re-read on "stale PR head" only, after each delay.
PR_HEAD_LAG_DELAYS = (1, 2, 4, 8, 8)
_REGULAR = ("100644", "100755")
SHA = re.compile(r"[0-9a-f]{40}\Z")
SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


class PushFailure(broker.BrokerDenied):
    """A failed push with a durable attempt boundary; never infer this from text.

    Once attempt journaling begins, even a failed journal or an unchanged ref
    needs a host hold: the write/verification path was entered but did not finish.
    """

    def __init__(self, outcome: str):
        self.outcome = outcome
        super().__init__(f"Git ref update not confirmed ({outcome})")


def _sha(value: object) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise broker.BrokerDenied("invalid Git object SHA")
    return value


# Files that act on the repository rather than live in it. A workflow runs with the
# repository's Actions secrets, so writing one would hand a credentialless fixer those
# secrets through CI; the others change checkout, review ownership or submodule sources.
CONTROL_FILES = {".gitmodules", ".gitattributes"}
CONTROL_PATHS = {"codeowners", "docs/codeowners"}


def _path(value: object) -> str:
    if (not isinstance(value, str) or len(value) > 512 or not value
            or any(not SEGMENT.fullmatch(part) or part in (".", "..")
                   or part.lower() == ".git"
                   for part in value.split("/"))):
        raise broker.BrokerDenied("unsafe file path")
    parts = value.lower().split("/")
    if parts[0] == ".github" or CONTROL_FILES.intersection(parts) or value.lower() in CONTROL_PATHS:
        raise broker.BrokerDenied("repository control file")
    return value


def _manifest(manifest: object) -> tuple[str, list[tuple[str, bytes]], bytes | None]:
    """Validate a push manifest: ``(base, files, patch)``.

    Two shapes. Whole files, ``{base_head, message, files}``, each file's new bytes; or a diff,
    ``{base_head, message, patch_b64, sha256}``, a unified diff against ``base_head`` that the
    host applies to that exact tree (``_git_cas``), where every path it changes is checked.
    """
    if not isinstance(manifest, dict) or set(manifest) not in (
            {"base_head", "message", "files"}, {"base_head", "message", "patch_b64", "sha256"}):
        raise broker.BrokerDenied("invalid push manifest")
    base = _sha(manifest["base_head"])
    message = manifest["message"]
    if (not isinstance(message, str) or not message.strip() or "\x00" in message
            or len(message.encode("utf-8")) > MAX_MESSAGE):
        raise broker.BrokerDenied("invalid push message or file count")
    if "patch_b64" in manifest:
        encoded, digest = manifest["patch_b64"], manifest["sha256"]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_PATCH + 2) // 3):
            raise broker.BrokerDenied("patch too large")
        try:
            patch = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise broker.BrokerDenied("invalid base64 content") from None
        if (not patch or len(patch) > MAX_PATCH or not isinstance(digest, str)
                or digest != hashlib.sha256(patch).hexdigest()):
            raise broker.BrokerDenied("patch content mismatch")
        return base, [], patch
    files = manifest["files"]
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise broker.BrokerDenied("invalid push message or file count")
    parsed = []
    total = 0
    seen = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "content_b64", "sha256"}:
            raise broker.BrokerDenied("invalid file entry")
        path = _path(entry["path"])
        if path in seen:
            raise broker.BrokerDenied("duplicate file path")
        seen.add(path)
        encoded, digest = entry["content_b64"], entry["sha256"]
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_FILE + 2) // 3):
            raise broker.BrokerDenied("file too large")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise broker.BrokerDenied("invalid base64 content") from None
        if len(data) > MAX_FILE or not isinstance(digest, str) or digest != hashlib.sha256(data).hexdigest():
            raise broker.BrokerDenied("file content mismatch")
        total += len(data)
        if total > MAX_CONTENT:
            raise broker.BrokerDenied("manifest too large")
        parsed.append((path, data))
    for path in seen:
        parts = path.split("/")
        if any("/".join(parts[:n]) in seen for n in range(1, len(parts))):
            raise broker.BrokerDenied("file and directory conflict")
    return base, parsed, None


def _changed_paths(raw: bytes) -> list[str]:
    """Check every path a diff changed (``git diff-index --cached --raw -z`` output).

    Only regular files may be added, changed or deleted — never a symlink, a submodule or any
    other mode, on either side, and no mode change — and every path passes ``_path``: no repository control file,
    nothing under ``.github``. Returns the changed paths, in order."""
    fields = raw.split(b"\0")
    paths = []
    for index in range(0, len(fields) - 1, 2):
        meta, path = fields[index].decode("ascii", "replace"), fields[index + 1]
        if not meta.startswith(":"):
            raise broker.BrokerDenied("unexpected diff record")
        old_mode, new_mode = meta[1:].split()[:2]
        if (old_mode not in _REGULAR + ("000000",) or new_mode not in _REGULAR + ("000000",)
                or old_mode == new_mode == "000000"):
            raise broker.BrokerDenied("patch changes a nonregular file")
        if "000000" not in (old_mode, new_mode) and old_mode != new_mode:
            # A whole-file push keeps a file's mode; so does a diff (no executable bit flipped).
            raise broker.BrokerDenied("patch changes a file mode")
        try:
            paths.append(_path(path.decode("utf-8", errors="strict")))
        except UnicodeDecodeError:
            raise broker.BrokerDenied("unsafe file path") from None
    if not paths:
        raise broker.BrokerDenied("push has no changes")
    if len(paths) > MAX_PATCH_PATHS:
        raise broker.BrokerDenied("patch changes too many files")
    return paths


def _api(loop: dict, path: str, *, login: str, method: str = "GET", body=None) -> dict:
    result = gh.api(loop, path, method=method, body=body, login=login)
    if not isinstance(result, dict) or "message" in result and "documentation_url" in result:
        raise broker.BrokerDenied("GitHub read request failed")
    return result


# GitHub does not always serve a just-written ref at once (#522: a landed issue-fix branch read
# back as 404, so the push was "unknown" and its PR never opened; the fixer's PR head lagged ~4s,
# #360). A read that shows no ref, or the ref still at its old commit, is read again before the
# outcome is called. A different commit ends the wait at once: that is another writer, not lag.
CONFIRM_WAITS = (1, 2, 4, 8)


def _read_ref(loop: dict, ref_path: str, branch: str, login: str) -> str | None:
    """The commit ``refs/heads/<branch>`` points at, or None when GitHub does not show it."""
    try:
        got = _api(loop, ref_path, login=login)
        if got.get("ref") != f"refs/heads/{branch}":
            return None
        return _sha((got.get("object") or {}).get("sha"))
    except Exception:
        return None


def _confirm_ref(loop: dict, ref_path: str, branch: str, login: str, new_head: str | None,
                 prior: str | None, *, push_failed: bool) -> str | None:
    """Read the ref back after a push attempt, waiting out GitHub's lag before giving up.

    Re-reads (``CONFIRM_WAITS``) while the ref is missing, or still at ``prior`` after a push that
    Git reported as done. A push Git refused that left the ref at ``prior`` is read once: that is
    the refusal, not lag."""
    observed = _read_ref(loop, ref_path, branch, login)
    if new_head is None:
        return observed
    for wait in CONFIRM_WAITS:
        if observed == new_head:
            break
        if observed is not None and (observed != prior or push_failed):
            break
        time.sleep(wait)
        observed = _read_ref(loop, ref_path, branch, login)
    return observed


def _audit(loop: dict, record: dict) -> None:
    """Durable metadata journal; failure aborts the write."""
    audit = config.state_dir(loop) / "broker-audit.jsonl"
    if not audit.parent.is_dir() or audit.parent.is_symlink():
        raise broker.BrokerDenied("durable audit directory unavailable")
    dirfd = os.open(audit.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fd = os.open(audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            line = (json.dumps({**record, "at": time.time()}, sort_keys=True) + "\n").encode()
            if os.write(fd, line) != len(line):
                raise OSError("short audit write")
            os.fsync(fd)
            # A new audit file's directory entry must survive a crash too.
            os.fsync(dirfd)
        finally:
            os.close(fd)
    finally:
        os.close(dirfd)


# Git's credential prompt: the login for "Username", the token file's contents for "Password".
_ASKPASS = ("import os,sys\nfrom pathlib import Path\n"
            "print('x-access-token' if 'Username' in sys.argv[1] "
            "else Path(os.environ['DIAKTOROS_TOKEN_FILE']).read_text().strip())\n")


class _Isolated:
    """A private bare repository and the only way to run Git against it: isolated config, no
    hooks, no credential helper, one allowed transport, and stderr never surfaced (it can carry
    URLs and server-controlled text). ``run`` raises on a non-zero exit; ``run_rc`` returns it."""

    def __init__(self, root: Path, env: dict, protocol: str):
        self.root, self.env, self.protocol = root, env, protocol
        self.bare = root / "objects.git"

    def _cmd(self, args):
        return ["/usr/bin/git", "-c", "credential.helper=", "-c", "core.hooksPath=/dev/null",
                "-c", "commit.gpgsign=false", "-c", "protocol.allow=never",
                "-c", f"protocol.{self.protocol}.allow=always", *args]

    def run_rc(self, *args: str, input: bytes | None = None) -> tuple[int, bytes]:
        try:
            result = subprocess.run(self._cmd(args), cwd=self.root, env=self.env, input=input,
                                    capture_output=True, timeout=90, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise broker.BrokerDenied("isolated Git transport failed") from exc
        return result.returncode, result.stdout

    def run(self, *args: str, input: bytes | None = None) -> bytes:
        code, out = self.run_rc(*args, input=input)
        if code:
            raise broker.BrokerDenied("isolated Git transport rejected operation")
        return out.strip()


@contextlib.contextmanager
def _isolated(loop: dict, login: str, identity: dict, remote: str | None):
    url = config.guard_network(remote if remote is not None else
                               f"https://github.com/{loop['repo']}.git")
    with tempfile.TemporaryDirectory(prefix="review-loop-git-") as temp:
        root = Path(temp)
        os.chmod(root, 0o700)
        askpass = root / "askpass.py"
        askpass.write_text("#!/usr/bin/python3\n" + util.leak_guard_code(_ASKPASS))
        askpass.chmod(0o700)
        env = {"PATH": "/usr/bin:/bin", "HOME": temp, "XDG_CONFIG_HOME": temp,
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(askpass),
               "GIT_OPTIONAL_LOCKS": "0", "GIT_INDEX_FILE": str(root / "manifest.index"),
               "DIAKTOROS_TOKEN_FILE": str(gh.token_path(loop, login)), "LC_ALL": "C",
               "GIT_AUTHOR_NAME": identity["name"], "GIT_AUTHOR_EMAIL": identity["email"],
               "GIT_COMMITTER_NAME": identity["name"], "GIT_COMMITTER_EMAIL": identity["email"]}
        util.leak_guard_env(env, pythonpath=False)   # the askpass loads it by path
        git = _Isolated(root, env, "file" if remote is not None else "https")
        git.run("init", "--bare", "--template", str(root / "empty-template"), str(git.bare))
        yield git, url


# A conflict block's opening or closing line, as Git writes them: 7 of '<' or '>' then a space
# or the line's end. (A lone line of 7 '=' is also a Markdown/RST underline: never a marker on its
# own.) A merge push that leaves one in a conflicted file is refused (#303).
CONFLICT_MARKER = re.compile(rb"^(?:<{7}|>{7})(?: |$)", re.M)
# The bounds of a merge export: conflicted paths reported back, and the tree's size in entries.
MERGE_CONFLICTS_MAX = 64
MERGE_ENTRIES_MAX = 20000
MERGE_BYTES_MAX = 100 * 1024 * 1024          # trusted_fetch's export bound
# What each side changed in the conflicted files, for the resolving turn's prompt: bounded.
MERGE_SIDES_MAX = 48 * 1024


class NeedsPerson(broker.BrokerDenied):
    """A conflict the loop does not resolve unattended: a whole-file one (#303)."""


def _merge(git: "_Isolated", url: str, branch: str, head: str, base_ref: str,
           base_sha: str) -> tuple[str, list[str]]:
    """Fetch the PR branch (it must be at ``head``) and the base branch (``base_sha`` must be on
    it), then merge ``base_sha`` into ``head`` without a worktree: the merged tree, with conflict
    markers in the conflicted files, and those files' paths.

    Only *content* conflicts are left to a resolving turn: both sides changed the file and Git
    wrote marker blocks into it. A whole-file conflict — modify/delete, add/delete, a rename, a
    binary file — leaves no markers (``merge-tree`` keeps one side's file whole), so a turn that
    never touched it would silently drop the other side. Those raise ``NeedsPerson``."""
    code, _ = git.run_rc("check-ref-format", "--branch", base_ref)
    if code:
        raise broker.BrokerDenied("invalid base branch name")
    bare = str(git.bare)
    git.run("--git-dir", bare, "fetch", "--no-tags", "--no-recurse-submodules", url,
            f"refs/heads/{branch}:refs/heads/snapshot", f"refs/heads/{base_ref}:refs/heads/base")
    if _sha(git.run("--git-dir", bare, "rev-parse", "refs/heads/snapshot").decode()) != head:
        raise broker.BrokerDenied("fetched PR branch moved")
    code, _ = git.run_rc("--git-dir", bare, "merge-base", "--is-ancestor", base_sha, "refs/heads/base")
    if code:
        raise broker.BrokerDenied("base commit is not on the base branch")
    code, out = git.run_rc("--git-dir", bare, "merge-tree", "--write-tree", "--no-messages",
                           "-z", head, base_sha)
    if code not in (0, 1):
        raise broker.BrokerDenied("merge of the base could not be computed")
    fields = [field for field in out.split(b"\0") if field]
    if not fields:
        raise broker.BrokerDenied("merge of the base could not be computed")
    tree = _sha(fields[0].decode())
    stages: dict[str, set[str]] = {}
    objects: dict[str, dict[str, str]] = {}
    for record in fields[1:]:
        # "<mode> <object> <stage>\t<path>" for each conflicted index entry.
        meta, _, path = record.partition(b"\t")
        parts = meta.decode("ascii", "replace").split()
        if len(parts) != 3 or not path:
            raise broker.BrokerDenied("merge of the base could not be computed")
        name = path.decode("utf-8", "surrogateescape")
        stages.setdefault(name, set()).add(parts[2])
        objects.setdefault(name, {})[parts[2]] = parts[1]
    conflicted = sorted(stages)
    if code == 1 and not conflicted:
        raise broker.BrokerDenied("merge of the base could not be computed")
    if len(conflicted) > MERGE_CONFLICTS_MAX:
        raise broker.BrokerDenied("merge conflicts in too many files")
    whole = []
    for path in conflicted:
        if not {"2", "3"} <= stages[path]:
            whole.append(path)                 # one side deleted or renamed it
            continue
        code, merged_oid = git.run_rc("--git-dir", bare, "rev-parse", "--verify", "-q",
                                      f"{tree}:{path}")
        if code or merged_oid.strip().decode() in (objects[path].get("2"), objects[path].get("3")):
            # #409: Git kept one side's file whole instead of writing a merged one — a binary
            # conflict, whatever its bytes look like (a kept side may hold a marker-shaped line).
            whole.append(path)
            continue
        code, blob = git.run_rc("--git-dir", bare, "cat-file", "blob", f"{tree}:{path}")
        if code or not CONFLICT_MARKER.search(blob):
            whole.append(path)                 # no marker block written: not a content conflict
    if whole:
        raise NeedsPerson(f"merge conflicts a person must resolve: {len(whole)} whole-file "
                          "conflict(s) (a modify/delete, a rename or a binary file)")
    return tree, conflicted


def merged_tree(loop: dict, *, branch: str, head: str, base_ref: str, base_sha: str,
                login: str, remote: str | None = None) -> dict:
    """The PR head with ``base_sha`` merged in, for a conflict-resolution turn (#303): read-only.

    Returns the merged tree's id, its conflicted paths, and the tree as a verified archive:
    ``entries`` (path, blob id, size, executable) for every regular file, ``skipped`` for the
    paths an export never carries (symlinks, submodules), and ``archive`` (a tar whose members
    sit under one top-level directory), so ``trusted_fetch._extract`` writes and verifies it
    exactly like a PR export."""
    _sha(head), _sha(base_sha)
    identity = {"name": "review-loop", "email": "review-loop@localhost"}
    with _isolated(loop, login, identity, remote) as (git, url):
        tree, conflicted = _merge(git, url, branch, head, base_ref, base_sha)
        bare = str(git.bare)
        entries, skipped, total = [], [], 0
        for record in git.run("--git-dir", bare, "ls-tree", "-r", "-l", "-z", tree).split(b"\0"):
            if not record:
                continue
            meta, path = record.split(b"\t", 1)
            mode, kind, oid, size = meta.decode().split()
            name = path.decode("utf-8", "surrogateescape")
            if kind == "blob" and mode in _REGULAR:
                entries.append((name, _sha(oid), int(size), mode == "100755"))
                total += int(size)
            else:
                skipped.append(name)
            # Bounded before anything is archived: entries and bytes alike.
            if len(entries) > MERGE_ENTRIES_MAX or total > MERGE_BYTES_MAX:
                raise broker.BrokerDenied("merged tree too large")
        archive = git.run_rc("--git-dir", bare, "archive", "--format=tar", "--prefix=merge/",
                             tree)
        if archive[0]:
            raise broker.BrokerDenied("merged tree could not be archived")
        # The base brings workflow changes into the branch: GitHub refuses that push from a token
        # without the `workflow` scope, which the loop never asks for. Part B reads this flag and
        # leaves such a PR to a person instead of spending a turn on a push that cannot land.
        workflows = bool(git.run("--git-dir", bare, "diff-tree", "-r", "--name-only", head, tree,
                                 "--", ".github/workflows"))
        return {"tree": tree, "conflicted": conflicted, "entries": entries,
                "skipped": skipped, "archive": archive[1], "workflows": workflows,
                "sides": _sides(git, head, base_sha, conflicted)}


def _sides(git: "_Isolated", head: str, base_sha: str, conflicted: list[str]) -> str:
    """What each side changed in every conflicted file since they split: the PR's diff and the
    base's, from their merge base, bounded at MERGE_SIDES_MAX (and said so when clipped)."""
    if not conflicted:
        return ""
    bare = str(git.bare)
    split = _sha(git.run("--git-dir", bare, "merge-base", head, base_sha).decode())
    parts = []
    for label, tip in (("the PR", head), ("the base", base_sha)):
        code, diff = git.run_rc("--git-dir", bare, "diff", "--no-color", "--no-ext-diff",
                                split, tip, "--", *conflicted)
        parts.append(f"### What {label} changed in these files\n\n```diff\n"
                     + (diff.decode("utf-8", "replace") if not code else "(unreadable)")
                     + "\n```")
    text = "\n\n".join(parts)
    if len(text.encode()) > MERGE_SIDES_MAX:
        text = (text.encode()[:MERGE_SIDES_MAX].decode("utf-8", "ignore")
                + f"\n\n(clipped at {MERGE_SIDES_MAX // 1024} KiB: read the files in /work)")
    return text


def _git_cas(loop: dict, repo: str, branch: str, head: str,
             files: list[tuple[str, bytes]], message: str, login: str,
             identity: dict, *, before_push=None, remote: str | None = None,
             from_branch: str | None = None, patch: bytes | None = None,
             changed: list | None = None, merge: dict | None = None) -> str:
    """Fetch the advertised branch, construct local objects, and exact-lease push.

    With ``from_branch`` (an issue fix, #214) the commit is built on ``head`` as found in that
    advertised branch (``head`` must be one of its commits) and pushed to ``branch``, which must
    not exist yet: the lease is "absent", so an existing branch is never overwritten.

    With ``patch`` (#64) the commit is the base tree with that unified diff applied by Git
    (``apply --cached``: index only, no worktree, no hooks, nothing outside the tree), and every
    path it changed is then checked like a whole file's (``_changed_paths``); ``changed``, when
    given, receives those paths for the audit record before ``before_push`` runs.

    With ``merge`` (#303: ``{base_ref, base_sha, tree}``, host-owned, never from the sandbox)
    the commit is a merge: it starts from ``base_sha`` merged into ``head`` — which must still be
    exactly ``tree``, the tree the resolving turn was given — applies the seat's resolution to
    that, refuses any conflict marker left in a conflicted file, and has the parents
    ``(head, base_sha)``. The lease is still the exact head.

    `remote` is a private local-fixture seam, never sourced from IPC or config.
    Only validated manifest paths/bytes reach Git's temporary private bare repo.
    """
    if merge is not None and (from_branch or set(merge) != {"base_ref", "base_sha", "tree"}):
        raise broker.BrokerDenied("invalid merge scope")
    with _isolated({**loop, "repo": repo}, login, identity, remote) as (git, url):
        run, bare = git.run, git.bare
        ref = f"refs/heads/{branch}"
        conflicted: list[str] = []
        if merge is not None:
            start, conflicted = _merge(git, url, branch, head, merge["base_ref"],
                                       _sha(merge["base_sha"]))
            if start != _sha(merge["tree"]):
                raise broker.BrokerDenied("the base merge changed since the turn was staged")
        else:
            # Fetch an ADVERTISED ref, never a dangling object ID. Verify the exact
            # snapshot before constructing anything or attempting a ref mutation.
            source = f"refs/heads/{from_branch}" if from_branch else ref
            run("--git-dir", str(bare), "fetch", "--no-tags", "--no-recurse-submodules",
                url, f"{source}:refs/heads/snapshot")
            snapshot = _sha(run("--git-dir", str(bare), "rev-parse", "refs/heads/snapshot").decode())
            if from_branch:
                try:
                    run("--git-dir", str(bare), "merge-base", "--is-ancestor", head, snapshot)
                except broker.BrokerDenied:
                    raise broker.BrokerDenied("base commit is not on the base branch") from None
            elif snapshot != head:
                raise broker.BrokerDenied("fetched PR branch moved")
            start = head
        parents = run("--git-dir", str(bare), "rev-list", "--parents", "-n", "1", head).decode().split()
        if not parents or parents[0] != head:
            raise broker.BrokerDenied("invalid fetched head")
        run("--git-dir", str(bare), "read-tree", start)
        existing = {}
        for entry in run("--git-dir", str(bare), "ls-files", "--stage", "-z").split(b"\0"):
            if entry:
                metadata, path = entry.split(b"\t", 1)
                mode, _, stage = metadata.decode().split()
                if stage != "0":
                    raise broker.BrokerDenied("unmerged base tree")
                existing[path.decode("utf-8", errors="surrogateescape")] = mode
        for path, data in files:
            parts = path.split("/")
            if any("/".join(parts[:n]) in existing for n in range(1, len(parts))):
                raise broker.BrokerDenied("manifest traverses tracked file or symlink")
            if any(item.startswith(path + "/") for item in existing):
                raise broker.BrokerDenied("manifest replaces tracked directory")
            if path in existing and existing[path] not in ("100644", "100755"):
                raise broker.BrokerDenied("manifest replaces nonregular file")
            blob = _sha(run("--git-dir", str(bare), "hash-object", "-w", "--stdin",
                            input=data).decode())
            # Keep an edited script executable; a new file is a plain file.
            mode = existing.get(path, "100644")
            run("--git-dir", str(bare), "update-index", "--add", "--cacheinfo",
                f"{mode},{blob},{path}")
        if patch is not None:
            # Applied to the index of the exact base tree: Git refuses a hunk that does not fit,
            # a path that escapes the tree and a path beyond a symlink; what it did change is
            # then checked path by path, so the rules are the whole-file push's.
            try:
                run("--git-dir", str(bare), "apply", "--cached", "--check", "-", input=patch)
                run("--git-dir", str(bare), "apply", "--cached", "-", input=patch)
            except broker.BrokerDenied:
                raise broker.BrokerDenied("patch does not apply to the scoped head") from None
            paths = _changed_paths(run("--git-dir", str(bare), "diff-index", "--cached", "--raw",
                                       "-z", "--no-renames", start))
            if changed is not None:
                changed.extend(paths)
        if conflicted:
            # A resolution must leave no conflict marker in any file Git could not merge.
            staged = {}
            for entry in run("--git-dir", str(bare), "ls-files", "--stage", "-z").split(b"\0"):
                if entry:
                    metadata, name = entry.split(b"\t", 1)
                    staged[name.decode("utf-8", "surrogateescape")] = metadata.decode().split()[1]
            left = [path for path in conflicted if path in staged and CONFLICT_MARKER.search(
                run("--git-dir", str(bare), "cat-file", "blob", staged[path]))]
            if left:
                raise broker.BrokerDenied(f"conflict markers left in {len(left)} file(s)")
            # Belt and braces: every conflicted file is one the seat explicitly resolved.
            touched = set(run("--git-dir", str(bare), "diff-index", "--cached", "--name-only",
                              "-z", "--no-renames", start).decode("utf-8", "surrogateescape")
                          .split("\0"))
            if not set(conflicted) <= touched:
                raise broker.BrokerDenied(
                    f"{len(set(conflicted) - touched)} conflicted file(s) left unresolved")
        tree = _sha(run("--git-dir", str(bare), "write-tree").decode())
        base_tree = _sha(run("--git-dir", str(bare), "rev-parse", f"{head}^{{tree}}").decode())
        if tree == base_tree:
            raise broker.BrokerDenied("push has no changes")
        extra_parents = ["-p", merge["base_sha"]] if merge is not None else []
        new_head = _sha(run("--git-dir", str(bare), "commit-tree", tree, "-p", head,
                            *extra_parents, input=message.encode("utf-8") + b"\n").decode())
        created = run("--git-dir", str(bare), "rev-list", "--parents", "-n", "1", new_head).decode().split()
        if created != [new_head, head] + ([merge["base_sha"]] if merge is not None else []):
            raise broker.BrokerDenied("local commit does not have exact expected parent")
        if before_push is not None:
            before_push(new_head)
        lease = f"{ref}:" if from_branch else f"{ref}:{head}"
        run("--git-dir", str(bare), "push", "--porcelain",
            f"--force-with-lease={lease}", url, f"{new_head}:{ref}")
        return new_head

def push(loop: dict, *, repo: str, number: int, head: str, role: str,
         branch: str, manifest: object, merge: dict | None = None,
         ci_fix: bool = False) -> dict:
    """Create Git objects and lease-advance only the gate-scoped PR branch.

    The trusted caller supplies scope from the gate, never from the manifest. All
    repository/ref API paths are derived from this scope after live authorization.
    GitHub API calls here are read-only; the only remote write is a leased Git push.
    The lease protects the branch SHA, not PR metadata: a close/retarget/draft
    transition after the final PR read and before receive-pack remains possible.
    """
    base, files, patch = _manifest(manifest)
    assert isinstance(manifest, dict)  # _manifest rejects any other shape
    if not config.unattended_fixer_push_enabled(loop):
        raise broker.BrokerDenied("unattended fixer push disabled")
    if base != head:
        raise broker.BrokerDenied("manifest base differs from scoped PR head")
    # A conflict turn (#303) runs on an approved or unreviewed PR, so it has no changes-requested
    # verdict to answer. ``merge`` is host-built (RunScope), never the seat's: an ordinary push
    # still needs the live verdict.
    verdict = merge is None and not ci_fix   # a CI-fix turn (#306) answers no verdict either
    login = broker.authorize(loop, repo=repo, number=number, head=head,
                             role=role, branch=branch, operation="push", require_verdict=verdict)
    if not isinstance(branch, str) or len(branch) > 200 or not all(
            SEGMENT.fullmatch(part) and not part.startswith(".") and not part.endswith(".")
            and ".." not in part and not part.endswith(".lock")
            for part in branch.split("/")):
        raise broker.BrokerDenied("unsafe branch ref")
    root = f"/repos/{repo}/git"
    ref_path = f"{root}/ref/heads/{quote(branch, safe='/')}"

    def check_ref() -> None:
        ref = _api(loop, ref_path, login=login)
        if ref.get("ref") != f"refs/heads/{branch}" or _sha((ref.get("object") or {}).get("sha")) != head:
            raise broker.BrokerDenied("PR branch moved")

    check_ref()
    # Resolve the authenticated fixer before attributing the local commit.
    seat = _api(loop, "/user", login=login)
    if (not isinstance(seat.get("login"), str) or seat["login"].casefold() != login.casefold()
            or type(seat.get("id")) is not int or seat["id"] <= 0):
        raise broker.BrokerDenied("fixer identity changed")
    identity = {"name": seat["login"],
                "email": f"{seat['id']}+{seat['login']}@users.noreply.github.com"}
    if not re.fullmatch(r"[A-Za-z0-9-]+", identity["name"]):
        raise broker.BrokerDenied("invalid fixer identity")
    # Refresh BOTH PR identity and branch immediately before ref mutation.
    broker.authorize(loop, repo=repo, number=number, head=head,
                     role=role, branch=branch, operation="push", require_verdict=verdict)
    check_ref()
    # The SHA is known after local construction, before the only remote mutation.
    receipt = {"repo": repo, "pr": number, "old_head": head,
               "branch": branch, "role": role, "login": login,
               "paths": [path for path, _ in files], "operation": "push"}
    if merge is not None:
        # #303: a conflict resolution — a merge commit of this base into the head.
        receipt["merge_base"] = merge.get("base_sha")
    # A diff's paths are known once Git has applied it; they reach the receipt before the push.
    extra = {"patch": patch, "changed": receipt["paths"]} if patch is not None else {}
    error = None
    new_head = None
    attempt_started = False
    def before_push(created: str) -> None:
        nonlocal new_head, attempt_started
        # Object construction/fetch may take time; the initial PR check cannot
        # authorize a later write. Recheck as close to the Git push as possible.
        broker.authorize(loop, repo=repo, number=number, head=head,
                         role=role, branch=branch, operation="push", require_verdict=verdict)
        check_ref()
        attempt_started = True
        _audit(loop, {**receipt, "new_head": created, "phase": "attempt"})
        new_head = created
    try:
        # The trailer (#197) is added here, on the host, after the seat's message was validated.
        _git_cas(loop, repo, branch, head, files, attribution.sign_commit(loop, manifest["message"]),
                 login, identity, before_push=before_push,
                 **({"merge": merge} if merge is not None else {}), **extra)
    except Exception as exc:
        error = exc
    outcome = "unknown"
    try:
        # Read back independently even on timeout, rejection, or lost response.
        observed = _confirm_ref(loop, ref_path, branch, login, new_head, head,
                                push_failed=error is not None)
        outcome = "published" if new_head is not None and observed == new_head else ("unchanged" if observed == head else "unknown")
        if outcome == "published" and new_head is not None:
            # Git's lease verifies only the ref. The PR may have closed during receive-pack
            # without changing that ref; never acknowledge it as a successful authorized push in
            # that case. A PR that still shows the old head is GitHub catching up: wait for it.
            if not _pr_follows(loop, repo, number, new_head, role, branch):
                outcome = "published_pr_unverified"
        _audit(loop, {**receipt, "new_head": new_head, "phase": "reconciled",
                      "outcome": outcome, "observed_head": observed})
    except Exception as exc:
        if attempt_started:
            raise PushFailure(outcome) from exc
        raise
    if error is not None or outcome != "published":
        failure = PushFailure(outcome) if attempt_started else broker.BrokerDenied(
            f"Git ref update not confirmed ({outcome})")
        raise failure from error
    return {**receipt, "new_head": new_head, "outcome": outcome}


def _pr_follows(loop: dict, repo: str, number: int, new_head: str, role: str, branch: str,
                sleep=None) -> bool:
    """Whether the PR, re-authorized live, is at ``new_head``. Only "stale PR head" is re-read
    (the PR lagging its branch); any other refusal is final at once."""
    for delay in (0, *PR_HEAD_LAG_DELAYS):
        if delay:
            (sleep or time.sleep)(delay)
        try:
            broker.authorize(loop, repo=repo, number=number, head=new_head, role=role,
                             branch=branch, operation="push", require_verdict=False)
            return True
        except broker.BrokerDenied as exc:
            if str(exc) != "stale PR head":
                return False
        except Exception:
            return False
    return False


def open_branch(loop: dict, *, repo: str, number: int, base: str, branch: str,
                manifest: object) -> dict:
    """Push an issue fix (#214) as one commit on ``base`` to the new branch ``branch``.

    Authorized against the live issue (``broker.authorize_issue_fix``) before construction and
    again just before the push. An existing branch (a second fix for the same issue) is refused,
    never overwritten, by two guards with different jobs:

    * a pre-write existence read (``GET .../git/ref/heads/<branch>``) whose 404 lets the run
      proceed; a 200, any other read failure, or a non-dict payload denies before anything is
      built or journaled. This turns a knowable second attempt into a pre-write denial
      (recorded ``denied``, not ``uncertain``).
    * the push's lease, which requires the branch to be absent. It covers the window between
      that read and the push, i.e. a race.

    Deleting the branch alone does not make the run retryable (the ``issue_fixes`` row counts as
    write evidence); see docs/issues.md for recovery. Opening the PR is the caller's next step,
    after this returns a confirmed ref.
    """
    base_head, files, patch = _manifest(manifest)
    assert isinstance(manifest, dict)
    if not config.issue_fixes_enabled(loop):
        raise broker.BrokerDenied("issue fixes disabled")
    if base_head != base:
        raise broker.BrokerDenied("manifest base differs from the scoped base commit")
    if branch != config.ISSUE_FIX_BRANCH.format(number=number):
        raise broker.BrokerDenied("unsafe branch ref")
    login = broker.authorize_issue_fix(loop, repo=repo, number=number)
    seat = _api(loop, "/user", login=login)
    if (not isinstance(seat.get("login"), str) or seat["login"].casefold() != login.casefold()
            or type(seat.get("id")) is not int or seat["id"] <= 0):
        raise broker.BrokerDenied("fixer identity changed")
    identity = {"name": seat["login"],
                "email": f"{seat['id']}+{seat['login']}@users.noreply.github.com"}
    if not re.fullmatch(r"[A-Za-z0-9-]+", identity["name"]):
        raise broker.BrokerDenied("invalid fixer identity")
    ref_path = f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"
    receipt = {"repo": repo, "pr": number, "old_head": base, "branch": branch,
               "role": "issue_fixer", "login": login, "paths": [path for path, _ in files],
               "operation": "issue_branch"}
    extra = {"patch": patch, "changed": receipt["paths"]} if patch is not None else {}
    # Pre-write: an existing branch (a second fix attempt) is a knowable denial, not an
    # uncertain push. The absent-branch lease below stays the guard against a race.
    existing, read_error = gh.fetch(loop, ref_path, login=login)
    if gh.status_of(read_error) == 404:
        pass                                   # absent: proceed
    elif read_error or not isinstance(existing, dict):
        raise broker.BrokerDenied("could not check whether the fix branch exists")
    else:
        raise broker.BrokerDenied(f"{branch} already exists — inspect it, and see docs/issues.md "
                                  "(uncertain/post-write recovery) before any new attempt")
    new_head = None
    attempt_started = False

    def before_push(created: str) -> None:
        nonlocal new_head, attempt_started
        broker.authorize_issue_fix(loop, repo=repo, number=number)
        attempt_started = True
        _audit(loop, {**receipt, "new_head": created, "phase": "attempt"})
        new_head = created
    error = None
    try:
        _git_cas(loop, repo, branch, base, files, attribution.sign_commit(loop, manifest["message"]),
                 login, identity, before_push=before_push, from_branch=loop["base"], **extra)
    except Exception as exc:
        error = exc
    observed = _confirm_ref(loop, ref_path, branch, login, new_head, None,
                            push_failed=error is not None)
    outcome = "published" if new_head is not None and observed == new_head else "unknown"
    if attempt_started:
        _audit(loop, {**receipt, "new_head": new_head, "phase": "reconciled",
                      "outcome": outcome, "observed_head": observed})
    if error is not None or outcome != "published":
        failure = PushFailure(outcome) if attempt_started else broker.BrokerDenied(
            "Git ref update not confirmed (unchanged)")
        raise failure from error
    return {**receipt, "new_head": new_head, "outcome": outcome}


# -- the base merged into a review-only author's PR (opt-in, ``review_only_update``) -----------

NEW_COMMITS_MAX = 200


class NotClean(broker.BrokerDenied):
    """The base does not merge into the head cleanly, or brings what the loop never pushes."""


def _fetch_pair(git: "_Isolated", url: str, branch: str, head: str, base_ref: str) -> str:
    """Fetch the PR branch (it must be at ``head``) and the base branch; return the base tip."""
    code, _ = git.run_rc("check-ref-format", "--branch", base_ref)
    if code:
        raise broker.BrokerDenied("invalid base branch name")
    bare = str(git.bare)
    git.run("--git-dir", bare, "fetch", "--no-tags", "--no-recurse-submodules", url,
            f"refs/heads/{branch}:refs/heads/snapshot", f"refs/heads/{base_ref}:refs/heads/base")
    if _sha(git.run("--git-dir", bare, "rev-parse", "refs/heads/snapshot").decode()) != head:
        raise broker.BrokerDenied("fetched PR branch moved")
    return _sha(git.run("--git-dir", bare, "rev-parse", "refs/heads/base").decode())


def _dry(git: "_Isolated", head: str, tip: str) -> dict:
    """The merge of ``tip`` into ``head`` without a worktree: clean or not, the tree when clean,
    the conflicted paths, whether the base brought workflow changes, and the base's new commits
    (``[(sha, subject)]``) since the two split."""
    bare = str(git.bare)
    code, out = git.run_rc("--git-dir", bare, "merge-tree", "--write-tree", "--no-messages",
                           "-z", head, tip)
    if code not in (0, 1):
        raise broker.BrokerDenied("merge of the base could not be computed")
    fields = [field for field in out.split(b"\0") if field]
    if not fields:
        raise broker.BrokerDenied("merge of the base could not be computed")
    tree = _sha(fields[0].decode())
    conflicted: set[str] = set()
    if code == 1:
        for record in fields[1:]:
            meta, _, path = record.partition(b"\t")
            if path and len(meta.split()) == 3:
                conflicted.add(path.decode("utf-8", "surrogateescape"))
        if not conflicted:
            raise broker.BrokerDenied("merge of the base could not be computed")
    split = _sha(git.run("--git-dir", bare, "merge-base", head, tip).decode())
    workflows = bool(git.run("--git-dir", bare, "diff", "--name-only", split, tip, "--",
                             ".github/workflows"))
    log = git.run("--git-dir", bare, "log", f"-n{NEW_COMMITS_MAX}", "--format=%H%x09%s",
                  f"{split}..{tip}").decode("utf-8", "replace")
    commits = [tuple(line.split("\t", 1)) for line in log.splitlines() if "\t" in line]
    return {"clean": code == 0, "tree": tree, "conflicted": sorted(conflicted),
            "workflows": workflows, "base": tip, "split": split, "commits": commits}


def dry_merge(loop: dict, *, branch: str, head: str, base_ref: str, login: str,
              remote: str | None = None) -> dict:
    """A dry merge of the base's tip into the PR head, in a private bare repository: nothing is
    pushed, nothing is written outside it (the same isolated merge the conflict turn uses)."""
    _sha(head)
    identity = {"name": "review-loop", "email": "review-loop@localhost"}
    with _isolated(loop, login, identity, remote) as (git, url):
        return _dry(git, head, _fetch_pair(git, url, branch, head, base_ref))


def update_branch(loop: dict, *, repo: str, number: int, head: str, branch: str,
                  remote: str | None = None) -> dict:
    """Merge the base into a review-only author's same-repository PR branch and push the merge
    commit with a lease on ``head``. Only a clean merge that carries no workflow change is pushed
    (``NotClean`` otherwise); the author pushing meanwhile makes the lease fail, overwriting nothing.

    Pushes as the account that makes the host's merge pushes (the fixer seat), within the push
    policy: ``review_only_update`` and unattended fixer pushes must both be on."""
    _sha(head)
    if not config.review_only_update(loop):
        raise broker.BrokerDenied("review-only updates disabled")
    if not config.unattended_fixer_push_enabled(loop):
        raise broker.BrokerDenied("unattended fixer push disabled")
    login = config.seat_login(loop, "fixer")
    reader = loop.get("read_token")
    if (not login or not reader or not gh.token_path(loop, login) or not gh.token_path(loop, reader)
            or login.casefold() == str(reader).casefold()):
        raise broker.BrokerDenied("explicit seat and read token mappings required")
    if not isinstance(branch, str) or len(branch) > 200 or not all(
            SEGMENT.fullmatch(part) and not part.startswith(".") and not part.endswith(".")
            and ".." not in part and not part.endswith(".lock") for part in branch.split("/")):
        raise broker.BrokerDenied("unsafe branch ref")

    def authorize() -> None:
        current = gh.api(loop, f"/repos/{repo}/pulls/{number}", login=reader)
        if not isinstance(current, dict):
            raise broker.BrokerDenied("cannot verify live PR")
        pr_head, pr_base = current.get("head") or {}, current.get("base") or {}
        author = str((current.get("user") or {}).get("login") or "").lower()
        if (current.get("number") != number or current.get("state") != "open"
                or current.get("draft") is not False):
            raise broker.BrokerDenied("PR identity, state or draft status changed")
        if (pr_base.get("repo") or {}).get("full_name") != repo or pr_base.get("ref") != loop.get("base"):
            raise broker.BrokerDenied("PR base branch mismatch")
        if pr_head.get("sha") != head:
            raise broker.BrokerDenied("stale PR head")
        if pr_head.get("ref") != branch or (pr_head.get("repo") or {}).get("full_name") != repo:
            raise broker.BrokerDenied("fork head not permitted for credentialed writes")
        if author not in config.review_only(loop):
            raise broker.BrokerDenied("PR author is not review-only")

    authorize()
    seat = _api(loop, "/user", login=login)
    if (not isinstance(seat.get("login"), str) or seat["login"].casefold() != login.casefold()
            or type(seat.get("id")) is not int or seat["id"] <= 0
            or not re.fullmatch(r"[A-Za-z0-9-]+", seat["login"])):
        raise broker.BrokerDenied("fixer identity changed")
    identity = {"name": seat["login"],
                "email": f"{seat['id']}+{seat['login']}@users.noreply.github.com"}
    ref = f"refs/heads/{branch}"
    ref_path = f"/repos/{repo}/git/ref/heads/{quote(branch, safe='/')}"
    receipt = {"repo": repo, "pr": number, "old_head": head, "branch": branch,
               "role": "review_only_update", "login": login, "paths": [],
               "operation": "update_branch"}
    new_head = None
    attempt_started = False
    error = None
    with _isolated({**loop, "repo": repo}, login, identity, remote) as (git, url):
        bare = str(git.bare)
        tip = _fetch_pair(git, url, branch, head, loop["base"])
        merged = _dry(git, head, tip)
        if not merged["clean"]:
            raise NotClean(f"{len(merged['conflicted'])} file(s) conflict: "
                           + ", ".join(merged["conflicted"][:5]))
        if merged["workflows"]:
            raise NotClean(f"{loop['base']} changed workflow files, which a push without the "
                           "`workflow` scope cannot carry")
        message = attribution.sign_commit(loop, f"Merge {loop['base']} into {branch}")
        new_head = _sha(git.run("--git-dir", bare, "commit-tree", merged["tree"], "-p", head,
                                "-p", tip, input=message.encode("utf-8") + b"\n").decode())
        receipt["merge_base"] = tip
        authorize()
        attempt_started = True
        _audit(loop, {**receipt, "new_head": new_head, "phase": "attempt"})
        try:
            git.run("--git-dir", bare, "push", "--porcelain",
                    f"--force-with-lease={ref}:{head}", url, f"{new_head}:{ref}")
        except Exception as exc:
            error = exc
    observed = _confirm_ref(loop, ref_path, branch, login, new_head, head,
                            push_failed=error is not None)
    outcome = ("published" if observed == new_head
               else "unchanged" if observed == head else "unknown")
    _audit(loop, {**receipt, "new_head": new_head, "phase": "reconciled",
                  "outcome": outcome, "observed_head": observed})
    if error is not None or outcome != "published":
        raise PushFailure(outcome) from error
    return {**receipt, "new_head": new_head, "base": tip, "outcome": outcome}
