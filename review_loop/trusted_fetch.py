"""Bounded, credential-owning GitHub PR export into an unpublished checkout.

The caller must mount only the returned directory into a separately contained run.
A same-UID process is not isolated by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import tempfile
import ctypes
import urllib.error
import urllib.parse
import urllib.request

from . import gh

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_MAX_FILES = 10_000
_MAX_BYTES = 100 * 1024 * 1024
_MAX_TREE_RESPONSE = 16 * 1024 * 1024
_MAX_METADATA_RESPONSE = 256 * 1024
_MAX_PATH_BYTES = 4096
_CHUNK = 64 * 1024

# The tarball endpoint 302-redirects to codeload.github.com, whose URL already carries a
# short-lived token, so the reader's PAT must NOT travel there. The redirect target must be
# exactly this origin; a test points it at loopback to run the real urllib path.
_TARBALL_REDIRECT = ("https", "codeload.github.com")


class FetchDenied(Exception):
    """The requested head cannot safely be staged."""

def _publish_exclusive(private: pathlib.Path, root: pathlib.Path) -> None:
    """Atomically publish a staged directory without replacing an existing one."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise FetchDenied("exclusive sandbox publish unavailable")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                          ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(private), -100, os.fsencode(root), 1) != 0:
        raise FetchDenied(f"sandbox publish failed (errno={ctypes.get_errno()})")


def _request(loop: dict, path: str, login: str, limit: int, accept: str) -> bytes:
    """Read at most limit+1 bytes, including for chunked and dishonest responses."""
    from .config import guard_network
    guard_network(f"{gh.API}{path}")    # under the test guard: loopback fakes only
    try:
        credential = gh.token(loop, login)
        req = urllib.request.Request(
            f"{gh.API}{path}", headers={"Accept": accept,
                "Authorization": f"Bearer {credential}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hermes-review-loop"})
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status != 200:
                raise FetchDenied("GitHub response unavailable")
            if int(response.headers.get("Content-Length", "0")) > limit:
                raise FetchDenied("GitHub response exceeds bounds")
            chunks, remaining = [], limit + 1
            while remaining:
                chunk = response.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining == 0:
                raise FetchDenied("GitHub response exceeds bounds")
            return b"".join(chunks)
    except (OSError, ValueError, urllib.error.URLError, gh.GitHubError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()   # it holds the response open, and __cause__ would keep it alive
        raise FetchDenied("GitHub response unavailable") from exc


def _json(loop: dict, path: str, login: str, limit: int = _MAX_METADATA_RESPONSE):
    try:
        return json.loads(_request(loop, path, login, limit, "application/vnd.github+json"))
    except (ValueError, UnicodeError) as exc:
        raise FetchDenied("invalid GitHub response") from exc


def _identity(loop: dict, repo: str, number: int, head: str, ref: str, role: str) -> str:
    if not isinstance(repo, str) or not _REPO.fullmatch(repo) or repo != loop.get("repo"):
        raise FetchDenied("repository mismatch")
    if type(number) is not int or number <= 0 or not isinstance(head, str) or not _SHA.fullmatch(head):
        raise FetchDenied("invalid PR identity")
    # The adjudicator reads the same exact-head export; its mount is read-only (contained.py).
    if (role not in ("reviewer", "fixer", "adjudicator", "issue_fixer") or not isinstance(ref, str)
            or not ref or ref.startswith("-")):
        raise FetchDenied("invalid role or ref")
    seats = loop.get("seats") or {}
    reader = loop.get("read_token")
    reviewer = (seats.get("reviewer") or {}).get("login")
    fixer = (seats.get("fixer") or {}).get("login")
    if not all(isinstance(x, str) and x for x in (reader, reviewer, fixer)):
        raise FetchDenied("explicit, distinct read and seat identities required")
    assert isinstance(reader, str) and isinstance(reviewer, str) and isinstance(fixer, str)
    if len({reader.casefold(), reviewer.casefold(), fixer.casefold()}) != 3:
        raise FetchDenied("explicit, distinct read and seat identities required")
    paths = [gh.token_path(loop, x) for x in (reader, reviewer, fixer)]
    if any(p is None for p in paths) or len({p.resolve() for p in paths if p is not None}) != 3:
        raise FetchDenied("distinct token files required")
    # Distinct filenames are not distinct principals; verify every actual credential.
    for login in (reader, reviewer, fixer):
        assert isinstance(login, str)
        user = _json(loop, "/user", login)
        if not isinstance(user, dict) or not isinstance(user.get("login"), str) or user["login"].casefold() != login.casefold():
            raise FetchDenied("credential principal mismatch")
    assert isinstance(reader, str)
    return reader


def _live_head(loop: dict, repo: str, number: int, head: str, ref: str, reader: str) -> None:
    pr = _json(loop, f"/repos/{repo}/pulls/{number}", reader)
    if not isinstance(pr, dict) or pr.get("number") != number or pr.get("state") != "open":
        raise FetchDenied("PR unavailable or closed")
    base, current = pr.get("base"), pr.get("head")
    if not isinstance(base, dict) or not isinstance(current, dict):
        raise FetchDenied("invalid PR repository or ref")
    base_repo, current_repo = base.get("repo"), current.get("repo")
    if not isinstance(base_repo, dict) or not isinstance(current_repo, dict):
        raise FetchDenied("invalid PR repository or ref")
    if (base_repo.get("full_name") != repo or base.get("ref") != loop.get("base")
            or current_repo.get("full_name") != repo or current.get("ref") != ref):
        raise FetchDenied("PR repository, base or ref changed")
    if current.get("sha") != head:
        raise FetchDenied("stale PR head")


def _entries(tree: dict) -> list[tuple[str, str, int, bool]]:
    if not isinstance(tree, dict) or tree.get("truncated") is not False or not isinstance(tree.get("tree"), list):
        raise FetchDenied("truncated or invalid tree")
    entries, total, seen, files, parents = [], 0, set(), set(), set()
    for item in tree["tree"]:
        if not isinstance(item, dict):
            raise FetchDenied("unsafe tree entry")
        name, mode, kind, oid, length = (item.get(key) for key in ("path", "mode", "type", "sha", "size"))
        if not isinstance(name, str):
            raise FetchDenied("unsafe tree entry")
        parts = name.split("/")
        try:
            path_length = len(name.encode("utf-8"))
        except UnicodeError as exc:
            raise FetchDenied("unsafe tree entry") from exc
        if (path_length > _MAX_PATH_BYTES
                or any(not p or p in (".", "..") or p.casefold() in (".git", ".gitmodules")
                       or "\\" in p or any(ord(c) < 32 or ord(c) == 127 for c in p) for p in parts)
                or name in seen or (name in parents and kind != "tree")
                or any("/".join(parts[:index]) in files
                                          for index in range(1, len(parts)))):
            raise FetchDenied("unsafe tree entry")
        seen.add(name)
        if len(seen) > _MAX_FILES:
            raise FetchDenied("tree exceeds export bounds")
        parents.update("/".join(parts[:index]) for index in range(1, len(parts)))
        if kind == "tree" and mode == "040000":
            continue
        if (kind != "blob" or mode not in ("100644", "100755")
                or not isinstance(oid, str) or not _SHA.fullmatch(oid)
                or type(length) is not int or length < 0):
            raise FetchDenied("unsafe tree entry")
        total += length
        files.add(name)
        entries.append((name, oid, length, mode == "100755"))
        if total > _MAX_BYTES:
            raise FetchDenied("tree exceeds export bounds")
    return entries


def _fetch_tarball(loop: dict, repo: str, reader: str, head: str, limit: int) -> bytes:
    """One GitHub call for the whole head: the commit's tarball, bounded by ``limit``.

    This replaces the old one-GET-per-blob export (≈1,660 calls for attest, against the read
    token's 5,000/h budget). The credential stays host-side on the first request; GitHub's
    tarball endpoint answers 302 to codeload.github.com, and urllib delivers a declined
    redirect as an ``HTTPError`` (never as a 302 response). We take exactly one hop, only
    to ``_TARBALL_REDIRECT``, and drop ``Authorization`` on it (codeload authenticates by the
    token in its URL). Nothing credentialed reaches the export.
    """
    from .config import guard_network
    guard_network(f"{gh.API}/repos/{repo}/tarball/{head}")

    # First request: to the API with Authorization; do NOT follow redirects automatically.
    # A declined redirect is raised by urllib as HTTPError, caught below.
    class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        req = urllib.request.Request(
            f"{gh.API}/repos/{repo}/tarball/{head}",
            headers={"Accept": "application/vnd.github+json",
                     "Authorization": f"Bearer {gh.token(loop, reader)}",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "User-Agent": "hermes-review-loop"})
        with opener.open(req, timeout=30) as response:
            if response.status != 200:
                raise FetchDenied("GitHub response unavailable")
            return _read_bounded(response, limit)
    except urllib.error.HTTPError as exc:
        # urllib raises here for a declined redirect; the error IS the open response.
        if exc.code not in (301, 302, 303, 307, 308):
            exc.close()
            raise FetchDenied("GitHub response unavailable") from exc
        location = exc.headers.get("Location")   # safe after close(): a property over .hdrs
        exc.close()
        if not location:
            raise FetchDenied("GitHub response unavailable")
        return _fetch_tarball_hop(location, limit)
    except (OSError, ValueError, urllib.error.URLError, gh.GitHubError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        raise FetchDenied("GitHub response unavailable") from exc


def _fetch_tarball_hop(location: str, limit: int) -> bytes:
    """Take the one allowed redirect hop: validate the target, then fetch it with no credential."""
    from .config import guard_network
    parsed = urllib.parse.urlparse(location)
    allowed_scheme, allowed_host = _TARBALL_REDIRECT
    if parsed.scheme != allowed_scheme or (parsed.hostname or "").lower() != allowed_host:
        raise FetchDenied(f"tarball redirect to non-codeload host: {location}")
    guard_network(location)   # the second URL is judged too, not only the first

    # A second redirect is refused: this handler never follows one, so the hop lands or fails.
    class _NoRedirectHandler2(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirectHandler2)
    # No Authorization: codeload authenticates by the token already in the URL.
    req = urllib.request.Request(
        location,
        headers={"Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "hermes-review-loop"})
    try:
        with opener.open(req, timeout=30) as response:
            if response.status != 200:
                raise FetchDenied("GitHub response unavailable")
            return _read_bounded(response, limit)
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            exc.close()
            raise FetchDenied("GitHub response unavailable") from exc
        # A redirect from the redirect target is a second hop: refused.
        exc.close()
        raise FetchDenied("GitHub response unavailable")
    except (OSError, ValueError, urllib.error.URLError, gh.GitHubError) as exc:
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        raise FetchDenied("GitHub response unavailable") from exc


def _read_bounded(response, limit: int) -> bytes:
    """Read at most limit+1 bytes from response."""
    if int(response.headers.get("Content-Length", "0")) > limit:
        raise FetchDenied("GitHub response exceeds bounds")
    chunks, remaining = [], limit + 1
    while remaining:
        chunk = response.read(min(_CHUNK, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if remaining == 0:
        raise FetchDenied("GitHub response exceeds bounds")
    return b"".join(chunks)


def _extract(archive: bytes, directory: pathlib.Path,
             entries: list[tuple[str, str, int, bool]]) -> None:
    """Write the tarball's regular files into ``directory`` — nothing else.

    GitHub prefixes every member with ``{owner}-{repo}-{sha}/``; that one component is
    stripped, and the remainder must be a path in the verified tree (no ``..``, no absolute
    path, no symlink/device/hardlink, no duplicate, nothing extra). Each file's length must
    match the tree's declared size and its bytes must hash to the tree's own object id
    (``sha1("blob <len>\\0" + content)``), so a blob whose content does not match its tree
    SHA, a missing entry or an extra entry refuses the turn. Executable bits come from the
    tree, not the archive's metadata. Bytes written are bounded by ``_MAX_BYTES``.
    """
    import io
    import tarfile
    expected = {name: (oid, length, executable)
                for name, oid, length, executable in entries}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
        seen: set[str] = set()
        for member in tar:
            name = member.name
            # Drop the {owner}-{repo}-{sha} prefix (one component), keep the rest verbatim.
            name = name.split("/", 1)[1] if "/" in name else name
            parts = name.split("/")
            if member.isdir():
                # GitHub tarballs list directories; parent dirs are also created implicitly
                # by the files under them, so skip the entry. A directory named like a tree
                # file still cannot shadow one: `seen` only gains real files below.
                if any(not p or p in (".", "..") or "\\" in p for p in parts):
                    raise FetchDenied("unsafe tree entry")
                continue
            if (member.isdev() or member.issym() or member.islnk()
                    or any(not p or p in (".", "..") or "\\" in p for p in parts)
                    or name not in expected):
                raise FetchDenied("unsafe tree entry")
            if name in seen:
                raise FetchDenied("unsafe tree entry")
            seen.add(name)
            source = tar.extractfile(member)
            if source is None:
                raise FetchDenied("unsafe tree entry")
            oid, length, executable = expected[name]
            target = directory.joinpath(*parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Stream: hash is incremental (length is known from the tree), so no file is
            # buffered whole. A size mismatch or hash mismatch refuses before anything is
            # published — the whole export stays unpublished until every entry verifies.
            digest = hashlib.sha1(b"blob " + str(length).encode() + b"\0")
            size = 0
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output, source:
                while chunk := source.read(_CHUNK):
                    size += len(chunk)
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        raise FetchDenied("tree exceeds export bounds")
                    digest.update(chunk)
                    output.write(chunk)
            if size != length or digest.hexdigest() != oid:
                raise FetchDenied("blob size or hash mismatch")
            target.chmod(0o755 if executable else 0o644)
        if seen != set(expected):
            raise FetchDenied("blob size or hash mismatch")


def _base_head(loop: dict, repo: str, head: str, reader: str) -> None:
    """An issue fix's base commit (#214): ``head`` must still be on the loop's base branch."""
    compare = _json(loop, f"/repos/{repo}/compare/{head}...{loop.get('base')}", reader)
    if not isinstance(compare, dict) or compare.get("status") not in ("identical", "ahead"):
        raise FetchDenied("base commit is no longer on the base branch")


def _stage(loop: dict, *, repo: str, number: int, head: str, ref: str,
           role: str, sandbox_root: pathlib.Path) -> pathlib.Path:
    reader = _identity(loop, repo, number, head, ref, role)
    if role == "issue_fixer":
        def live(*_args) -> None:
            _base_head(loop, repo, head, reader)
    else:
        live = _live_head
    root = pathlib.Path(sandbox_root).absolute()
    token_paths = [path.resolve() for login in
                   (reader, loop["seats"]["reviewer"]["login"], loop["seats"]["fixer"]["login"])
                   if (path := gh.token_path(loop, login)) is not None]
    if (root.exists() or root.is_symlink() or root.parent.resolve() != root.parent
            or any(root == token or root in token.parents for token in token_paths)):
        raise FetchDenied("sandbox root exists, follows a symlink or overlaps credential")
    live(loop, repo, number, head, ref, reader)
    commit = _json(loop, f"/repos/{repo}/git/commits/{head}", reader)
    if not isinstance(commit, dict) or commit.get("sha") != head:
        raise FetchDenied("commit SHA mismatch")
    commit_tree = commit.get("tree")
    if not isinstance(commit_tree, dict):
        raise FetchDenied("invalid commit tree")
    tree_sha = commit_tree.get("sha")
    if not isinstance(tree_sha, str) or not _SHA.fullmatch(tree_sha):
        raise FetchDenied("invalid commit tree")
    tree = _json(loop, f"/repos/{repo}/git/trees/{tree_sha}?recursive=1", reader, _MAX_TREE_RESPONSE)
    if not isinstance(tree, dict) or tree.get("sha") != tree_sha:
        raise FetchDenied("tree SHA mismatch")
    entries = _entries(tree)
    live(loop, repo, number, head, ref, reader)
    # Sibling on the same filesystem: none of the partial export is visible at root.
    with tempfile.TemporaryDirectory(prefix=".review-trusted-", dir=root.parent) as temp:
        private = pathlib.Path(temp)
        private.chmod(0o700)
        archive = _fetch_tarball(loop, repo, reader, head, _MAX_BYTES)
        directory = private / "repo"
        directory.mkdir(mode=0o700)
        _extract(archive, directory, entries)
        live(loop, repo, number, head, ref, reader)
        if root.exists() or root.is_symlink():
            raise FetchDenied("sandbox root appeared during staging")
        _publish_exclusive(private, root)
    return root / "repo"


def stage(loop: dict, *, repo: str, number: int, head: str, ref: str,
          role: str, sandbox_root: pathlib.Path) -> pathlib.Path:
    """Stage an exact same-repository PR head; return a credentialless checkout path."""
    return _stage(loop, repo=repo, number=number, head=head, ref=ref, role=role,
                  sandbox_root=sandbox_root)
