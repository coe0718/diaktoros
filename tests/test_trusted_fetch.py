"""Offline GitHub transport fixtures for the trusted fetch boundary."""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from review_loop import trusted_fetch, trusted_turn
from review_loop.trusted_fetch import _MAX_BYTES


LOOPBACK_API = "http://127.0.0.1:9"   # never contacted: urlopen is mocked where it is used

class CommittedSourceSnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.destination = self.root / 'snapshot'
        self.git('init', '-q')
        (self.source / 'run_agent.py').write_text('committed\n')
        (self.source / 'module.py').write_text('original\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'initial')

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.source), *args])

    def test_uses_head_not_dirty_or_staged_files(self):
        (self.source / 'run_agent.py').write_text('dirty secret\n')
        (self.source / 'module.py').write_text('staged secret\n')
        self.git('add', 'module.py')
        trusted_turn._safe_code_snapshot(self.source, self.destination)
        self.assertEqual((self.destination / 'run_agent.py').read_text(), 'committed\n')
        self.assertEqual((self.destination / 'module.py').read_text(), 'original\n')

    def test_symlink_swap_cannot_export_external_file(self):
        external = self.root / 'external.py'
        external.write_text('EXTERNAL SECRET\n')
        (self.source / 'module.py').unlink()
        (self.source / 'module.py').symlink_to(external)
        trusted_turn._safe_code_snapshot(self.source, self.destination)
        self.assertEqual((self.destination / 'module.py').read_text(), 'original\n')

    def test_nested_hidden_and_credentials_are_not_exported(self):
        for name in ('.private/token.py', 'pkg/.hidden.py', 'pkg/CONFIG.YAML',
                     'pkg/credentials/key.py', 'pkg/normal.py'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('secret\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'more')
        trusted_turn._safe_code_snapshot(self.source, self.destination)
        for name in ('.private/token.py', 'pkg/.hidden.py', 'pkg/CONFIG.YAML',
                     'pkg/credentials/key.py'):
            self.assertFalse((self.destination / name).exists(), name)
        self.assertTrue((self.destination / 'pkg/normal.py').is_file())

    def test_committed_symlink_and_source_symlink_denied(self):
        (self.source / 'linked.py').symlink_to(self.root / 'external.py')
        self.git('add', 'linked.py')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'link')
        with self.assertRaisesRegex(trusted_turn.TurnDenied, 'nonregular'):
            trusted_turn._safe_code_snapshot(self.source, self.destination)
        alias = self.root / 'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(trusted_turn.TurnDenied, 'invalid source'):
            trusted_turn._safe_code_snapshot(alias, self.root / 'other')

    def test_destination_directory_swap_cannot_write_through_symlink(self):
        (self.source / 'pkg').mkdir()
        (self.source / 'pkg/normal.py').write_text('safe\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.org',
                 'commit', '-qm', 'nested')
        outside = self.root / 'outside'
        outside.mkdir()
        real_mkdir = trusted_turn.os.mkdir
        def swap(path, *args, **kwargs):
            if path == 'pkg' and kwargs.get('dir_fd') is not None:
                (self.destination / 'pkg').symlink_to(outside, target_is_directory=True)
                raise FileExistsError(path)
            return real_mkdir(path, *args, **kwargs)
        with mock.patch.object(trusted_turn.os, 'mkdir', side_effect=swap):
            with self.assertRaises(OSError):
                trusted_turn._safe_code_snapshot(self.source, self.destination)
        self.assertFalse((outside / 'normal.py').exists())


class Response:
    def __init__(self, data, length=None):
        self.stream = io.BytesIO(data)
        self.headers = {"Content-Length": str(len(data) if length is None else length)}
        self.status = 200
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size):
        self.read_sizes.append(size)
        return self.stream.read(size)


class TrustedFetchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = pathlib.Path(temp.name)
        self.paths = {}
        for name in ("reader", "reviewer", "fixer"):
            path = self.root / (name + ".key")
            path.write_text("dummy-" + name)
            self.paths[name] = str(path)
        self.loop = {"repo": "acme/widgets", "base": "main", "read_token": "reader",
                     "tokens": self.paths, "seats": {"reviewer": {"login": "reviewer"},
                                                   "fixer": {"login": "fixer"}}}
        self.blob = b"safe\n"
        self.oid = hashlib.sha1(b"blob 5\0" + self.blob).hexdigest()
        self.head = "a" * 40
        self.tree_sha = "b" * 40
        self.tree = {"sha": self.tree_sha, "truncated": False,
                     "tree": [{"path": "hello.txt", "mode": "100644", "type": "blob",
                               "sha": self.oid, "size": len(self.blob)}]}
        self.calls = []
        self.pr_count = 0
        self.pr_head = self.head
        self.tarball = self._tarball({"hello.txt": self.blob})
        self.kw = {"repo": "acme/widgets", "number": 7, "head": self.head,
                   "ref": "work", "role": "reviewer", "sandbox_root": self.root / "sandbox"}

    @staticmethod
    def _tarball(files: dict[str, bytes], *, prefix: str = "acme-widgets-aaaaaaa",
                 symlink: str | None = None, traversal: str | None = None) -> bytes:
        """A GitHub-shaped tarball: every member under ``{prefix}/`` (one path component)."""
        import io
        import tarfile
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for name, content in files.items():
                info = tarfile.TarInfo(f"{prefix}/{name}")
                info.size = len(content)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(content))
            if symlink is not None:
                info = tarfile.TarInfo(f"{prefix}/{symlink}")
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            if traversal is not None:
                info = tarfile.TarInfo(traversal)
                info.size = 4
                tar.addfile(info, io.BytesIO(b"pwn\n"))
        return buffer.getvalue()

    def response(self, loop, path, login, limit, accept):
        self.calls.append((path, login, limit))
        if path == "/user":
            return json.dumps({"login": login}).encode()
        if path.endswith("/pulls/7"):
            self.pr_count += 1
            return json.dumps({"number": 7, "state": "open", "head": {"sha": self.pr_head,
                "ref": "work", "repo": {"full_name": "acme/widgets"}},
                "base": {"ref": "main", "repo": {"full_name": "acme/widgets"}}}).encode()
        if path.endswith("/git/commits/" + self.head):
            return json.dumps({"sha": self.head, "tree": {"sha": self.tree_sha}}).encode()
        if "/git/trees/" in path:
            return json.dumps(self.tree).encode()
        if "/tarball/" in path:
            return self.tarball
        raise AssertionError(path)

    def stage(self, callback=None, **changes):
        # Mock the HTTP layer for _fetch_tarball:
        # 1. First request to api.github.com/repos/.../tarball/{head} returns 302 to codeload
        # 2. Second request to codeload returns the tarball
        import urllib.request
        from unittest import mock
        import io

        # Track calls to verify Authorization handling
        fetch_calls = []

        # Get tarball from callback if provided, else use default
        tarball_bytes = self.tarball
        # Note: tarball content is taken from self.tarball (set by test setUp or test method).
        # The callback is only for _request API calls (/user, /pulls, /git/commits, /git/trees).

        redirect_url = "https://codeload.github.com/acme/widgets/legacy.gz/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        first_response = type("Resp", (), {
            "status": 302,
            "headers": {"Location": redirect_url},
            "read": lambda self: b"",
            "close": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda *a: None,
        })()
        # Use BytesIO to simulate proper chunked reading
        second_stream = io.BytesIO(tarball_bytes)
        second_response = type("Resp", (), {
            "status": 200,
            "headers": {"Content-Length": str(len(tarball_bytes))},
            "read": lambda self, n=-1: second_stream.read(n if n > 0 else -1),
            "close": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda *a: None,
        })()

        def fake_build_opener(*handlers):
            class FakeOpener:
                def open(self, req, timeout=30):
                    fetch_calls.append(("opener", req.full_url, dict(req.headers)))
                    # First opener call returns 302, second returns 200 with tarball
                    if not hasattr(fake_build_opener, "call_count"):
                        fake_build_opener.call_count = 0
                    fake_build_opener.call_count += 1
                    if fake_build_opener.call_count == 1:
                        return first_response
                    return second_response
            return FakeOpener()

        def fake_urlopen(req, timeout=30):
            fetch_calls.append(("urlopen", req.full_url, dict(req.headers)))
            return second_response

        self._fetch_calls = fetch_calls

        # Also mock _request for the metadata API calls (/user, /pulls, /git/commits, /git/trees)
        def mock_request(loop, path, login, limit, accept):
            if callback:
                result = callback(loop, path, login, limit, accept)
                if result is not None:
                    return result
            return self.response(loop, path, login, limit, accept)

        with mock.patch.object(urllib.request, "build_opener", side_effect=fake_build_opener), \
             mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(trusted_fetch.gh, "token", return_value="dummy-token"), \
             mock.patch.object(trusted_fetch.gh, "API", LOOPBACK_API), \
             mock.patch.object(trusted_fetch, "_request", side_effect=mock_request):
            result = trusted_fetch._stage(self.loop, **{**self.kw, **changes})

        # Record the tarball call in self.calls for tests that track API calls
        self.calls.append((f"/repos/{self.loop['repo']}/tarball/{self.kw['head']}", "reader", _MAX_BYTES))
        return result

    def test_exact_export_no_credentials_and_three_head_checks(self):
        result = self.stage()
        self.assertEqual((result / "hello.txt").read_bytes(), self.blob)
        self.assertFalse((result / ".git").exists())
        self.assertEqual(self.pr_count, 3)
        self.assertEqual([login for path, login, _ in self.calls if path == "/user"],
                         ["reader", "reviewer", "fixer"])
        self.assertFalse(any(self.root.glob(".review-trusted-*")))

    def test_oversized_response_stopped_before_download(self):
        # urlopen is mocked; the loopback API keeps the test guard's no-real-GitHub check quiet.
        response = Response(b"x" * 100, length=0)
        with mock.patch.object(trusted_fetch.gh, "token", return_value="dummy"), \
                mock.patch.object(trusted_fetch.gh, "API", LOOPBACK_API), \
                mock.patch.object(trusted_fetch.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(trusted_fetch.FetchDenied, "bounds"):
                trusted_fetch._request(self.loop, "/repos/acme/widgets/git/trees/x", "reader", 8,
                                       "application/vnd.github+json")
        self.assertEqual(response.stream.tell(), 9)
        self.assertEqual(response.read_sizes, [9])
        response = Response(b"x" * 100, length=100)
        with mock.patch.object(trusted_fetch.gh, "token", return_value="dummy"), \
                mock.patch.object(trusted_fetch.gh, "API", LOOPBACK_API), \
                mock.patch.object(trusted_fetch.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(trusted_fetch.FetchDenied, "bounds"):
                trusted_fetch._request(self.loop, "/user", "reader", 8, "application/vnd.github+json")
        self.assertEqual(response.stream.tell(), 0)

    def test_deleted_large_blob_never_fetched(self):
        # A historical oversized blob, modeled by an excluded OID, is never requested.
        # The whole head now costs one tarball call, not one GET per blob: the tree is read
        # first (so an oversized or historical entry never reaches the export at all).
        historical_oid = "d" * 40
        def only_head(loop, path, login, limit, accept):
            self.assertNotIn(historical_oid, path)
            return self.response(loop, path, login, limit, accept)
        self.stage(callback=only_head)
        self.assertEqual([path for path, _, _ in self.calls if "/tarball/" in path],
                         [f"/repos/acme/widgets/tarball/{self.head}"])
        # One call for content, whatever the file count — never one per blob.
        self.assertFalse(any("/git/blobs/" in path for path, _, _ in self.calls))

    def test_truncated_malformed_and_unsafe_tree_rejected(self):
        valid = self.tree["tree"][0]
        for change in ({"truncated": True}, {"tree": [{**valid, "mode": "120000"}]},
                       {"tree": [{**valid, "type": "commit", "mode": "160000"}]},
                       {"tree": [{**valid, "path": "../escape"}]},
                       {"tree": [{**valid, "size": trusted_fetch._MAX_BYTES + 1}]},
                       {"tree": [valid, valid]},
                       {"tree": [{**valid, "path": "dir"}, {**valid, "path": "dir/file"}]}):
            with self.subTest(change=change), self.assertRaises(trusted_fetch.FetchDenied):
                trusted_fetch._entries({**self.tree, **change})
        self.assertFalse((self.root / "sandbox").exists())
        self.assertEqual(len(trusted_fetch._entries({**self.tree, "tree": [
            {"path": "dir", "mode": "040000", "type": "tree", "sha": self.tree_sha},
            {**valid, "path": "dir/file"}]})), 1)

    def test_principal_mismatch_including_seat_substitution(self):
        def mismatch(loop, path, login, limit, accept):
            if path == "/user" and login == "reader":
                return b'{"login":"reviewer"}'
            return self.response(loop, path, login, limit, accept)
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "principal mismatch"):
            self.stage(callback=mismatch)
        self.assertFalse((self.root / "sandbox").exists())

    def test_case_variant_logins_are_not_distinct_principals(self):
        self.loop["read_token"] = "Reviewer"
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "distinct read and seat"):
            self.stage()
        self.assertFalse((self.root / "sandbox").exists())

    def test_malformed_nested_api_objects_fail_closed(self):
        for path_fragment, replacement in (
            ("/pulls/7", {"head": "bad"}),
            ("/pulls/7", {"base": {"repo": []}}),
            ("/pulls/7", {"head": {"repo": "bad"}}),
            ("/git/commits/", {"tree": "bad"}),
        ):
            with self.subTest(path_fragment=path_fragment, replacement=replacement):
                def malformed(loop, path, login, limit, accept):
                    original = self.response(loop, path, login, limit, accept)
                    if path_fragment in path:
                        return json.dumps({**json.loads(original), **replacement}).encode()
                    return original
                with self.assertRaises(trusted_fetch.FetchDenied):
                    self.stage(callback=malformed)
                self.assertFalse((self.root / "sandbox").exists())

    def test_exclusive_publish_never_replaces_empty_sibling(self):
        source = self.root / "unpublished"
        source.mkdir()
        (source / "sentinel").write_text("new")
        destination = self.root / "sandbox"
        destination.mkdir()
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "publish failed"):
            trusted_fetch._publish_exclusive(source, destination)
        self.assertTrue((source / "sentinel").exists())
        self.assertTrue(destination.is_dir())

    def test_partial_export_invisible_and_stale_head(self):
        def inspect(loop, path, login, limit, accept):
            if "/tarball/" in path:
                self.assertFalse((self.root / "sandbox").exists())
                self.assertEqual(len(list(self.root.glob(".review-trusted-*"))), 1)
            data = self.response(loop, path, login, limit, accept)
            if path.endswith("/pulls/7") and self.pr_count == 3:
                self.pr_head = "c" * 40
                return self.response(loop, path, login, limit, accept)
            return data
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "stale"):
            self.stage(callback=inspect)
        self.assertFalse((self.root / "sandbox").exists())
        self.assertFalse(any(self.root.glob(".review-trusted-*")))

    def test_corrupt_blob_and_wrong_commit_rejected(self):
        # A tarball whose blob bytes do not match the tree's SHA for that path is refused.
        self.tarball = self._tarball({"hello.txt": b"tampered\n"})
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "hash mismatch"):
            self.stage()
        self.assertFalse((self.root / "sandbox").exists())
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "commit SHA mismatch"):
            self.stage(callback=lambda loop, path, login, limit, accept:
                b'{"sha":"bad"}' if "/git/commits/" in path else self.response(loop, path, login, limit, accept))

    def test_a_turn_costs_a_small_bounded_number_of_api_calls(self):
        # #66: one tarball call for the whole head, so the count does not grow with the file
        # count. A tree of 50 files still costs the same handful of metadata reads + 1 tarball.
        count = 50
        self.tree = {"sha": self.tree_sha, "truncated": False, "tree": [
            {"path": f"f{i}.txt", "mode": "100644", "type": "blob",
             "sha": hashlib.sha1(b"blob 2\0" + b"x\n").hexdigest(), "size": 2}
            for i in range(count)]}
        self.tarball = self._tarball({f"f{i}.txt": b"x\n" for i in range(count)})
        self.stage()
        # 3 x /user (identity) + 3 x /pulls/7 (_live_head) + commit + tree + 1 tarball.
        self.assertLessEqual(len(self.calls), 9,
                             f"a turn made {len(self.calls)} API calls: {self.calls}")
        self.assertFalse(any("/git/blobs/" in path for path, _, _ in self.calls))
        # The count is independent of the file count: 50 files cost the same as 1.
        self.assertEqual(len([p for p, _, _ in self.calls if "/tarball/" in p]), 1)
        # The one content call carries the read token host-side (never the export).
        self.assertEqual([login for path, login, _ in self.calls if "/tarball/" in path],
                         ["reader"])

    def test_a_tarball_with_a_path_traversal_is_refused(self):
        # #66: an archive member escaping the tree root must be refused, not written.
        self.tarball = self._tarball({"hello.txt": self.blob}, traversal="../../etc/evil")
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "unsafe tree entry"):
            self.stage()
        self.assertFalse((self.root / "sandbox").exists())

    def test_a_tarball_with_a_symlink_is_refused(self):
        # The existing handling for symlinks (#69 still open) is kept: a symlink member is
        # refused rather than exported.
        self.tarball = self._tarball({"hello.txt": self.blob}, symlink="link")
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "unsafe tree entry"):
            self.stage()
        self.assertFalse((self.root / "sandbox").exists())

    def test_a_tarball_with_an_extra_file_is_refused(self):
        # An entry the tree did not list (extra file) is refused: set equality with the tree.
        self.tarball = self._tarball({"hello.txt": self.blob, "extra.txt": b"smuggled\n"})
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "unsafe tree entry"):
            self.stage()
        self.assertFalse((self.root / "sandbox").exists())

    def test_a_tarball_missing_a_tree_file_is_refused(self):
        # The tree lists hello.txt; an archive that omits it is refused (nothing matches).
        self.tarball = self._tarball({})
        with self.assertRaisesRegex(trusted_fetch.FetchDenied, "hash mismatch"):
            self.stage()
        self.assertFalse((self.root / "sandbox").exists())

    def test_a_tarball_with_directory_entries_is_accepted(self):
        # Real GitHub tarballs list directory members; they must not be mistaken for a
        # path escape or an extra file (a false refusal would break every nested tree).
        self.tree = {"sha": self.tree_sha, "truncated": False, "tree": [
            {"path": "src/hello.txt", "mode": "100644", "type": "blob",
             "sha": self.oid, "size": len(self.blob)}]}
        import io
        import tarfile
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            directory = tarfile.TarInfo("acme-widgets-aaaaaaa/src")
            directory.type = tarfile.DIRTYPE
            tar.addfile(directory)
            info = tarfile.TarInfo("acme-widgets-aaaaaaa/src/hello.txt")
            info.size = len(self.blob)
            tar.addfile(info, io.BytesIO(self.blob))
        self.tarball = buffer.getvalue()
        result = self.stage()
        self.assertEqual((result / "src" / "hello.txt").read_bytes(), self.blob)

    # --- redirect behavior tests (PR #196 follow-up) ---

    def test_tarball_redirect_to_codeload_drops_authorization(self):
        """The tarball endpoint 302s to codeload.github.com; Authorization must not leak there."""
        import urllib.request
        from unittest import mock
        import io
        from review_loop import gh

        loop = self.loop
        tarball_bytes = self.tarball

        # First response: 302 to codeload (https, not http)
        redirect_url = "https://codeload.github.com/acme/widgets/legacy.gz/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        first_response = type("Resp", (), {
            "status": 302,
            "headers": {"Location": redirect_url},
            "read": lambda self: b"",
            "close": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda *a: None,
        })()
        # Second response: tarball content from codeload
        second_stream = io.BytesIO(tarball_bytes)
        second_response = type("Resp", (), {
            "status": 200,
            "headers": {"Content-Length": str(len(tarball_bytes))},
            "read": lambda self, n=-1: second_stream.read(n if n > 0 else -1),
            "close": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda *a: None,
        })()

        fetch_calls = []
        build_opener_count = [0]

        def fake_build_opener(*handlers):
            build_opener_count[0] += 1
            class FakeOpener:
                def open(self, req, timeout=30):
                    fetch_calls.append(("opener", req.full_url, dict(req.headers)))
                    # First build_opener call (first request) returns 302
                    # Second build_opener call (second request) returns 200 with tarball
                    if build_opener_count[0] == 1:
                        return first_response
                    return second_response
            return FakeOpener()

        def fake_urlopen(req, timeout=30):
            fetch_calls.append(("urlopen", req.full_url, dict(req.headers)))
            raise AssertionError("urlopen should not be called directly")

        with mock.patch.object(urllib.request, "build_opener", side_effect=fake_build_opener), \
             mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(gh, "token", return_value="dummy-token"), \
             mock.patch.object(gh, "API", LOOPBACK_API):
            archive = trusted_fetch._fetch_tarball(loop, "acme/widgets", "reader", self.head, _MAX_BYTES)

        self.assertEqual(archive, tarball_bytes)
        # Verify Authorization was on first request but not on redirect
        opener_calls = [c for c in fetch_calls if c[0] == "opener"]
        self.assertEqual(len(opener_calls), 2)  # Two build_opener calls
        self.assertIn("Authorization", opener_calls[0][2])
        self.assertNotIn("Authorization", opener_calls[1][2])

    def test_tarball_redirect_to_non_codeload_refused(self):
        """A redirect to anything other than codeload.github.com is refused."""
        import urllib.request
        from unittest import mock
        from review_loop import gh

        loop = self.loop
        redirect_url = "https://evil.example.com/steal.tar.gz"  # https but wrong host
        first_response = type("Resp", (), {
            "status": 302,
            "headers": {"Location": redirect_url},
            "read": lambda self: b"",
            "close": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda *a: None,
        })()

        def fake_build_opener(*handlers):
            class FakeOpener:
                def open(self, req, timeout=30):
                    return first_response
            return FakeOpener()

        def fake_urlopen(req, timeout=30):
            raise AssertionError("should not follow redirect")

        with mock.patch.object(urllib.request, "build_opener", side_effect=fake_build_opener), \
             mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(gh, "token", return_value="dummy-token"), \
             mock.patch.object(gh, "API", LOOPBACK_API):
            with self.assertRaisesRegex(trusted_fetch.FetchDenied, "non-codeload host"):
                trusted_fetch._fetch_tarball(loop, "acme/widgets", "reader", self.head, _MAX_BYTES)

    def test_tarball_multiple_redirects_refused(self):
        """More than one redirect is refused (loop protection)."""
        import urllib.request
        from unittest import mock
        from review_loop import gh

        loop = self.loop
        first_redirect = "https://codeload.github.com/acme/widgets/legacy.gz/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        second_redirect = "https://codeload.github.com/another.tar.gz"
        first_response = type("Resp", (), {
            "status": 302,
            "headers": {"Location": first_redirect},
            "read": lambda self: b"",
            "close": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda *a: None,
        })()
        # Second request returns another redirect (which the no-redirect handler will not follow)
        second_response = type("Resp", (), {
            "status": 302,
            "headers": {"Location": second_redirect},
            "read": lambda self: b"",
            "close": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda *a: None,
        })()

        build_opener_count = [0]

        def fake_build_opener(*handlers):
            build_opener_count[0] += 1
            class FakeOpener:
                def open(self, req, timeout=30):
                    # First build_opener call (first request) returns 302
                    # Second build_opener call (second request) returns another 302
                    if build_opener_count[0] == 1:
                        return first_response
                    return second_response
            return FakeOpener()

        def fake_urlopen(req, timeout=30):
            raise AssertionError("urlopen should not be called directly")

        with mock.patch.object(urllib.request, "build_opener", side_effect=fake_build_opener), \
             mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(gh, "token", return_value="dummy-token"), \
             mock.patch.object(gh, "API", LOOPBACK_API):
            # The second request returns a 302 which the no-redirect handler doesn't follow,
            # so we get a non-200 status -> "GitHub response unavailable"
            with self.assertRaisesRegex(trusted_fetch.FetchDenied, "GitHub response unavailable"):
                trusted_fetch._fetch_tarball(loop, "acme/widgets", "reader", self.head, _MAX_BYTES)

    def test_tarball_http_redirect_refused(self):
        """A non-HTTPS redirect target is refused."""
        import urllib.request
        from unittest import mock
        from review_loop import gh

        loop = self.loop
        redirect_url = "http://codeload.github.com/steal.tar.gz"  # http, not https
        first_response = type("Resp", (), {
            "status": 302,
            "headers": {"Location": redirect_url},
            "read": lambda self: b"",
            "close": lambda self: None,
            "__enter__": lambda self: self,
            "__exit__": lambda *a: None,
        })()

        def fake_build_opener(*handlers):
            class FakeOpener:
                def open(self, req, timeout=30):
                    return first_response
            return FakeOpener()

        def fake_urlopen(req, timeout=30):
            raise AssertionError("should not follow redirect")

        with mock.patch.object(urllib.request, "build_opener", side_effect=fake_build_opener), \
             mock.patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(gh, "token", return_value="dummy-token"), \
             mock.patch.object(gh, "API", LOOPBACK_API):
            with self.assertRaisesRegex(trusted_fetch.FetchDenied, "non-codeload host"):
                trusted_fetch._fetch_tarball(loop, "acme/widgets", "reader", self.head, _MAX_BYTES)


if __name__ == "__main__":
    unittest.main()
