"""Issue #51: host-side dependency prefetch, read-only offline cache, and what the seat is told."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from review_loop import contained, deps  # noqa: E402

CRATES = "registry+https://github.com/rust-lang/crates.io-index"
LOCK = f'''version = 4

[[package]]
name = "itoa"
version = "1.0.18"
source = "{CRATES}"
checksum = "8f42a60cbdf9a97f5d2305f08a87dc4e09308d1276d28c869c684d7777685682"

[[package]]
name = "tiny"
version = "0.1.0"
dependencies = ["itoa"]
'''
MANIFEST = '[package]\nname = "tiny"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\nitoa = "1"\n'


def _package(name, version, source):
    return f'[[package]]\nname = "{name}"\nversion = "{version}"\nsource = "{source}"\n'


def _toolchain() -> Path:
    if os.environ.get("REVIEW_LOOP_TEST_RUST"):
        return Path(os.environ["REVIEW_LOOP_TEST_RUST"])
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".rustup/toolchains/stable-x86_64-unknown-linux-gnu"


def _crates_io_reachable() -> bool:
    try:
        socket.create_connection(("index.crates.io", 443), timeout=3).close()
        return True
    except OSError:
        return False


def _bwrap_works() -> bool:
    if not shutil.which("bwrap"):
        return False
    try:
        return subprocess.run(["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr", "--ro-bind",
                               "/bin", "/bin", "--ro-bind", "/lib", "/lib", "--ro-bind-try",
                               "/lib64", "/lib64", "--", "/usr/bin/true"],
                              capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.checkout = self.root / "export"
        (self.checkout / "src").mkdir(parents=True)
        (self.checkout / "src/lib.rs").write_text("")
        self.cache_parent = self.root / "deps"
        self.cache_parent.mkdir(mode=0o700)

    def write(self, lock=LOCK, manifest=MANIFEST):
        if manifest is not None:
            (self.checkout / "Cargo.toml").write_text(manifest)
        if lock is not None:
            (self.checkout / "Cargo.lock").write_text(lock)


class LockfileTests(Base):
    def test_only_crates_io_pairs_are_taken_and_path_packages_skipped(self):
        self.write(LOCK + _package("ryu", "1.0.0-rc.1+b", "sparse+https://index.crates.io/"))
        self.assertEqual(deps.locked_crates(self.checkout / "Cargo.lock"),
                         [("itoa", "1.0.18"), ("ryu", "1.0.0-rc.1+b")])

    def test_git_and_other_registries_are_refused(self):
        for source in ("git+https://evil.example/x.git#abc", "git+ssh://git@host/x#abc",
                       "registry+https://evil.example/index", "sparse+https://evil.example/"):
            with self.subTest(source=source):
                self.write(LOCK + _package("x", "1.0.0", source))
                with self.assertRaisesRegex(ValueError, "non-crates.io"):
                    deps.locked_crates(self.checkout / "Cargo.lock")

    def test_names_and_versions_cannot_inject_into_the_synthetic_manifest(self):
        for name, version in (('a"b', "1.0.0"), ("a", '1.0.0", path = "/etc'), ("a b", "1.0.0"),
                              ("a", "1.0"), ("a", "^1.0.0"), ("-a", "1.0.0")):
            with self.subTest(name=name, version=version):
                (self.checkout / "Cargo.lock").write_text(
                    "[[package]]\nname = " + json.dumps(name) + "\nversion = " + json.dumps(version)
                    + f'\nsource = "{CRATES}"\n')
                with self.assertRaises(ValueError):
                    deps.locked_crates(self.checkout / "Cargo.lock")

    def test_bounds_and_malformed(self):
        (self.checkout / "Cargo.lock").write_text("not = [toml")
        with self.assertRaises(ValueError):
            deps.locked_crates(self.checkout / "Cargo.lock")
        many = "".join(_package(f"c{i}", "1.0.0", CRATES) for i in range(deps.MAX_PACKAGES + 1))
        (self.checkout / "Cargo.lock").write_text(many)
        with self.assertRaisesRegex(ValueError, "more than"):
            deps.locked_crates(self.checkout / "Cargo.lock")
        (self.checkout / "Cargo.lock").unlink()
        (self.checkout / "Cargo.lock").symlink_to("/etc/hostname")
        with self.assertRaises(ValueError):
            deps.locked_crates(self.checkout / "Cargo.lock")

    def test_synthetic_manifest_pins_exact_versions(self):
        text = deps.synthetic_manifest([("itoa", "1.0.18"), ("ryu", "1.0.0")])
        self.assertIn('d0 = { package = "itoa", version = "=1.0.18", default-features = false }', text)
        self.assertIn('d1 = { package = "ryu", version = "=1.0.0"', text)
        self.assertNotIn("path", text)
        self.assertNotIn("git", text)


FAKE_CARGO = r'''#!/usr/bin/env python3
import json, os, sys, pathlib
home = pathlib.Path(os.environ["CARGO_HOME"])
record = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": dict(os.environ),
          "manifest": pathlib.Path("Cargo.toml").read_text(),
          "config_in_ancestry": [str(p) for p in pathlib.Path.cwd().parents
                                 if (p / ".cargo").exists()]}
(home / "record.json").write_text(json.dumps(record))
mode = os.environ.get("FAKE_MODE") or pathlib.Path(__file__).with_name("mode").read_text()
if mode == "ok":
    d = home / "registry/cache/index.crates.io-1949cf8c6b5b557f"
    d.mkdir(parents=True, exist_ok=True)
    (d / "itoa-1.0.18.crate").write_bytes(b"x")
elif mode == "fail":
    print("error: failed to download from index.crates.io")
    sys.exit(101)
elif mode == "sleep":
    import time; time.sleep(30)
elif mode in ("flood", "some"):
    # A lockfile whose crates are large: 1 MiB unpacked per step, as cargo would land them.
    import time
    d = home / "registry/cache/index.crates.io-1949cf8c6b5b557f"
    d.mkdir(parents=True, exist_ok=True)
    (d / "itoa-1.0.18.crate").write_bytes(b"x")
    src = home / "registry/src/index.crates.io-1949cf8c6b5b557f"
    for step in range(64 if mode == "flood" else 2):
        crate = src / f"big-{step}"
        crate.mkdir(parents=True, exist_ok=True)
        (crate / "lib.rs").write_bytes(b"y" * (1024 * 1024))
        time.sleep(0.05)
    (home / "finished").write_text("the fetch ran to completion")
'''


class FakeCargo(Base):
    def setUp(self):
        super().setUp()
        self.rust = self.root / "rust"
        (self.rust / "bin").mkdir(parents=True)
        cargo = self.rust / "bin/cargo"
        cargo.write_text(FAKE_CARGO)
        cargo.chmod(0o755)
        (self.rust / "bin/rustc").write_text("")
        self.mode("ok")

    def mode(self, value):
        (self.rust / "bin/mode").write_text(value)

    def record(self):
        return json.loads((self.cache_parent / "cargo/record.json").read_text())


class PrefetchTests(FakeCargo):
    def test_not_a_rust_head(self):
        self.assertIsNone(deps.prefetch_rust(self.checkout, self.cache_parent, self.rust))
        self.assertEqual(deps.prepare(self.checkout, self.cache_parent, self.rust), [])

    def test_manifest_without_lockfile_is_unavailable(self):
        self.write(lock=None)
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertEqual((result.status, result.cache), (deps.UNAVAILABLE, None))
        self.assertIn("no Cargo.lock", result.reason)

    def test_git_source_never_runs_cargo(self):
        self.write(LOCK + _package("x", "1.0.0", "git+https://evil.example/x#a"))
        with mock.patch.object(deps, "bounded_run", side_effect=AssertionError("ran cargo")):
            result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("non-crates.io", result.reason)

    def test_no_registry_crates_is_ready_without_cargo(self):
        self.write('version = 4\n[[package]]\nname = "tiny"\nversion = "0.1.0"\n')
        with mock.patch.object(deps, "bounded_run", side_effect=AssertionError("ran cargo")):
            result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertTrue(result.ready)
        self.assertTrue((result.cache / "registry").is_dir())

    def test_fetch_runs_on_a_synthetic_manifest_with_a_scratch_environment(self):
        self.write()
        # Everything the PR controls that cargo would otherwise honour.
        (self.checkout / ".cargo").mkdir()
        (self.checkout / ".cargo/config.toml").write_text('[build]\nrustc-wrapper = "/bin/false"\n')
        (self.checkout / "build.rs").write_text("fn main() {}")
        (self.checkout / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "nightly"\n')
        parent_env = {"GH_TOKEN": "ghp_dummyparenttoken000000000", "GITHUB_TOKEN": "x",
                      "CARGO_REGISTRY_TOKEN": "x", "SSH_AUTH_SOCK": "/tmp/agent",
                      "HTTPS_PROXY": "http://proxy.example", "RUSTC_WRAPPER": "/bin/false",
                      "CARGO_HOME": "/somewhere/else", "RUSTUP_TOOLCHAIN": "nightly"}
        with mock.patch.dict(os.environ, parent_env):
            result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertTrue(result.ready, result)
        self.assertEqual(result.cache, self.cache_parent / "cargo")
        record = self.record()
        self.assertEqual(record["argv"], ["fetch"])
        self.assertEqual(record["manifest"], deps.synthetic_manifest([("itoa", "1.0.18")]))
        cwd = Path(record["cwd"])
        self.assertNotEqual(cwd, self.checkout)
        self.assertNotIn(self.checkout, cwd.parents)
        self.assertEqual(record["config_in_ancestry"], [])
        env = record["env"]
        for name in parent_env:
            if name != "CARGO_HOME":
                self.assertNotIn(name, env)
        self.assertEqual(env["CARGO_HOME"], str(self.cache_parent / "cargo"))
        self.assertEqual(env["RUSTC"], str(self.rust / "bin/rustc"))
        self.assertNotEqual(env["HOME"], os.environ.get("HOME"))
        self.assertFalse(Path(env["HOME"]).exists())       # a throwaway, removed afterwards
        self.assertFalse(cwd.exists())

    def test_failed_or_incomplete_fetch_is_unavailable_with_detail(self):
        self.write()
        self.mode("fail")
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("failed", result.reason)
        self.assertIn("failed to download", result.detail)
        self.mode("noop")                               # rc 0 but nothing downloaded
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("1 of 1 crates missing", result.reason)

    def test_timeout_kills_the_fetch(self):
        self.write()
        self.mode("sleep")
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust, timeout=1)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("timed out", result.reason)

    def test_output_is_bounded(self):
        rc, tail = deps.bounded_run([sys.executable, "-c", "print('x' * 1000000)"],
                                    env={"PATH": "/usr/bin:/bin"}, cwd=self.root, timeout=20,
                                    limit=1000)
        self.assertEqual(rc, 0)
        self.assertLessEqual(len(tail), 1000)

    def test_a_crashing_prefetcher_is_unavailable_not_raised(self):
        self.write()
        with mock.patch.dict(deps.ECOSYSTEMS, {"rust": mock.Mock(side_effect=RuntimeError("x"))}):
            [result] = deps.prepare(self.checkout, self.cache_parent, self.rust)
        self.assertEqual((result.ecosystem, result.status), ("rust", deps.UNAVAILABLE))

    def test_unusable_cache_is_unavailable_without_cargo(self):
        self.write()
        with mock.patch.object(deps, "bounded_run", side_effect=AssertionError("ran cargo")):
            [result] = deps.prepare(self.checkout, None, self.rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("cache is unusable", result.reason)

    def test_cache_root_is_private(self):
        loop = {"state_dir": str(self.root / "state")}
        self.assertEqual(deps.cache_root(loop), self.root / "state/deps")
        self.assertEqual((self.root / "state/deps").stat().st_mode & 0o777, 0o700)
        (self.root / "state/deps").chmod(0o755)
        with self.assertRaises(PermissionError):
            deps.cache_root(loop)


MiB = 1024 * 1024


def _inventory(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")} if root.exists() else set()


class ByteCapTests(FakeCargo):
    """The lockfile is PR-controlled: bounded by package count *and* by bytes, during the fetch."""

    def seed(self, megabytes: int) -> set[str]:
        """An existing cache from earlier PRs: crates this lockfile does not pin."""
        cache = self.cache_parent / "cargo"
        old = cache / "registry/src/index.crates.io-1949cf8c6b5b557f/old-1.0.0"
        old.mkdir(parents=True)
        for index in range(megabytes):
            (old / f"f{index}").write_bytes(b"o" * MiB)
        return _inventory(cache)

    def test_a_fetch_past_the_cap_is_stopped_and_leaves_no_partial_cache(self):
        self.write()
        self.mode("flood")                              # would land 64 MiB
        before = self.seed(1)
        started = time.monotonic()
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust, cap=4 * MiB)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(result.status, deps.UNAVAILABLE, result)
        self.assertIn("would exceed 4 MiB", result.reason)
        self.assertIsNone(result.cache)
        cache = self.cache_parent / "cargo"
        self.assertFalse((cache / "finished").exists())            # stopped, not checked after
        self.assertEqual(_inventory(cache), before)                # nothing this fetch landed
        self.assertEqual(sorted(p.name for p in self.cache_parent.iterdir()
                                if p.name.startswith("cargo.")), [])
        note = deps.seat_note([result], "reviewer")
        self.assertIn("would exceed 4 MiB", note)
        self.assertIn("not the PR", note)

    def test_a_first_fetch_past_the_cap_leaves_no_cache_at_all(self):
        self.write()
        self.mode("flood")
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust, cap=3 * MiB)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertEqual([p for p in (self.cache_parent / "cargo").rglob("*")
                          if p.is_file() and p.stat().st_size], [])

    def test_within_the_cap_is_ready(self):
        self.write()
        self.mode("some")                               # 2 MiB
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust, cap=8 * MiB)
        self.assertTrue(result.ready, result)
        self.assertTrue((result.cache / "finished").exists())
        deps.release([result])

    def test_an_accumulated_cache_is_retired_and_refetched_not_blamed_on_the_pr(self):
        self.write()
        self.mode("some")                               # 2 MiB of its own
        self.seed(3)                                    # 3 MiB other PRs left behind
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust, cap=4 * MiB)
        self.assertTrue(result.ready, result)
        self.assertFalse(any("old-1.0.0" in p for p in _inventory(result.cache)))
        self.assertEqual([p.name for p in self.cache_parent.iterdir()
                          if p.name.startswith("cargo.retired")], [])   # nobody held it: gone
        deps.release([result])

    def test_a_retired_cache_a_running_turn_holds_is_kept_until_released(self):
        self.write()
        self.mode("some")
        self.seed(3)
        running = deps.hold(self.cache_parent / "cargo")    # a turn is building from it
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust, cap=4 * MiB)
        self.assertTrue(result.ready, result)
        [retired] = [p for p in self.cache_parent.iterdir() if p.name.startswith("cargo.retired")]
        self.assertTrue((retired / "registry/src/index.crates.io-1949cf8c6b5b557f/old-1.0.0/f0")
                        .exists())                          # still readable by that turn
        deps.release([result])
        # A second retirement while the first is still held: refused, never a third copy.
        self.seed(3)
        blocked = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust, cap=4 * MiB)
        self.assertEqual(blocked.status, deps.UNAVAILABLE)
        self.assertIn("would exceed 4 MiB", blocked.reason)
        self.assertIn("still in use", blocked.reason)
        os.close(running)
        swept = deps.prefetch_rust(self.checkout, self.cache_parent, self.rust, cap=4 * MiB)
        self.assertTrue(swept.ready, swept)
        self.assertFalse(retired.exists())
        deps.release([swept])

    def test_a_ready_cache_is_held_for_the_turn(self):
        self.write()
        [result] = deps.prepare(self.checkout, self.cache_parent, self.rust)
        self.assertTrue(result.ready)
        probe = os.open(result.cache / deps.IN_USE, os.O_RDONLY)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            deps.release([result])
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

    def test_cap_default_and_override(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(deps.CAP_ENV, None)
            self.assertEqual(deps.cache_cap(), 2 * 1024 * MiB)
            self.assertEqual(deps.refused_cap_override(), None)
            os.environ[deps.CAP_ENV] = "5"
            self.assertEqual(deps.cache_cap(), 5 * 1024 * MiB)
            for bad in ("abc", "0", "2000", "1.5"):
                os.environ[deps.CAP_ENV] = bad
                self.assertEqual(deps.cache_cap(), 2 * 1024 * MiB)
                self.assertIn(bad, deps.refused_cap_override())
        self.assertEqual(deps.human_bytes(2 * 1024 * MiB), "2 GiB")
        self.assertEqual(deps.human_bytes(4 * MiB), "4 MiB")

    def test_human_bytes_reads_right_at_every_size(self):
        for value, text in ((0, "0 bytes"), (1, "1 byte"), (512, "512 bytes"),
                            (16 * 1024, "16 KiB"), (1536, "1.5 KiB"), (1023 * 1024, "1023 KiB"),
                            (MiB, "1 MiB"), (int(1.25 * MiB), "1.25 MiB"),
                            (276 * 1000 ** 2, "263.21 MiB"), (1024 * MiB, "1 GiB"),
                            (int(2.5 * 1024 * MiB), "2.5 GiB"), (3 * 1024 ** 4, "3072 GiB")):
            with self.subTest(value):
                self.assertEqual(deps.human_bytes(value), text)

    def test_prepare_applies_the_configured_cap(self):
        self.write()
        self.mode("flood")
        with mock.patch.dict(os.environ, {deps.CAP_ENV: "1"}), \
             mock.patch.object(deps, "GIB", MiB):          # 1 "GiB" = 1 MiB for the test
            [result] = deps.prepare(self.checkout, self.cache_parent, self.rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("would exceed", result.reason)


class GitBoundaryTests(Base):
    def test_git_dependency_note_names_the_boundary_and_its_reason(self):
        self.write(LOCK + _package("x", "1.0.0", "git+https://github.com/o/x#a"))
        rust = self.root / "rust"
        result = deps.prefetch_rust(self.checkout, self.cache_parent, rust)
        self.assertEqual(result.status, deps.UNAVAILABLE)
        self.assertIn("git", result.reason)
        self.assertTrue(result.boundary)
        for role in ("reviewer", "fixer", "adjudicator"):
            note = deps.seat_note([result], role)
            self.assertIn("deliberate", note)
            self.assertIn("the host fetches nothing it cannot name", note)
            self.assertIn("URL the PR chose", note)
            self.assertIn("not a defect of the PR", note)
        # A crate name is PR-controlled: it never reaches the seat's host note.
        self.assertNotIn('"x"', deps.seat_note([result], "reviewer"))

    def test_other_registry_is_the_same_boundary(self):
        self.write(LOCK + _package("x", "1.0.0", "registry+https://evil.example/index"))
        result = deps.prefetch_rust(self.checkout, self.cache_parent, self.root / "rust")
        self.assertTrue(result.boundary)
        self.assertIn("registry other than crates.io", result.reason)


class LedgerTextTests(unittest.TestCase):
    def test_outcomes_are_one_bounded_line(self):
        ready = deps.Prefetch("rust", deps.READY, "224 crates.io crates from Cargo.lock",
                              Path("/c"), seconds=2.44)
        self.assertEqual(deps.ledger_text([ready]),
                         "rust: ready — 224 crates.io crates from Cargo.lock (2.4s)")
        gone = deps.Prefetch("rust", deps.UNAVAILABLE, "x" * 5000, detail="SECRET tool output")
        text = deps.ledger_text([gone])
        self.assertTrue(text.startswith("rust: unavailable — "))
        self.assertLessEqual(len(text), deps.LEDGER_MAX)
        self.assertNotIn("SECRET", text)                  # tool output never reaches the ledger
        self.assertEqual(deps.ledger_text([]), "none — no dependency lockfile at the head's root")
        self.assertNotIn("\n", deps.ledger_text([deps.Prefetch("rust", deps.UNAVAILABLE, "a\nb")]))


class SeatNoteTests(unittest.TestCase):
    def test_unavailable_tells_the_reviewer_to_judge_by_reading(self):
        note = deps.seat_note([deps.Prefetch("rust", deps.UNAVAILABLE, "no network")], "reviewer")
        self.assertIn("NOT available", note)
        self.assertIn("no network", note)
        self.assertIn("Judge by reading", note)
        self.assertIn("not by itself a reason to request changes", note)
        fixer = deps.seat_note([deps.Prefetch("rust", deps.UNAVAILABLE, "x")], "fixer")
        self.assertIn("unbuilt", fixer)
        self.assertIn("answers", fixer)  # the summary is not published; the answers are (#52)
        adjudicator = deps.seat_note([deps.Prefetch("rust", deps.UNAVAILABLE, "x")], "adjudicator")
        self.assertIn("not evidence either way", adjudicator)

    def test_ready_and_absent(self):
        note = deps.seat_note([deps.Prefetch("rust", deps.READY, "3 crates", Path("/c"))], "reviewer")
        self.assertIn("available offline", note)
        self.assertNotIn("NOT", note)
        self.assertEqual(deps.seat_note([], "reviewer"), "")


class ContainedMountTests(Base):
    def layout(self):
        base = Path(tempfile.mkdtemp(dir=self.root))
        paths = {name: base / name for name in
                 ("code", "venv", "runtime", "home", "checkout", "rust", "query")}
        for directory in paths.values():
            directory.mkdir()
        return paths

    def test_cache_is_mounted_read_only_inside_the_tmpfs_and_offline_is_always_set(self):
        cache = self.cache_parent / "cargo"
        (cache / "registry").mkdir(parents=True)
        argv = contained.command(**self.layout(), entry=["true"],
                                 dependency_caches={"rust": cache})
        bind = argv.index(str(cache / "registry"))
        self.assertEqual(argv[bind - 1:bind + 2],
                         ["--ro-bind", str(cache / "registry"), "/tmp/cargo/registry"])
        self.assertLess(argv.index("/tmp"), bind)          # after --tmpfs /tmp, not shadowed by it
        self.assertEqual(argv[argv.index("CARGO_HOME") + 1], "/tmp/cargo")
        self.assertEqual(argv[argv.index("CARGO_NET_OFFLINE") + 1], "true")
        bare = contained.command(**self.layout(), entry=["true"])
        self.assertEqual(bare[bare.index("CARGO_NET_OFFLINE") + 1], "true")
        self.assertNotIn("/tmp/cargo/registry", bare)      # no cache, no mount

    def test_cache_and_review_diff_mount_together(self):
        # #50 and #51 in one turn: the reviewer's diff and the crate cache are both read-only.
        cache = self.cache_parent / "cargo"
        (cache / "registry").mkdir(parents=True)
        review = Path(tempfile.mkdtemp(dir=self.root))
        (review / "pr.diff").write_text("diff --git a/x b/x\n")
        argv = contained.command(**self.layout(), entry=["true"],
                                 dependency_caches={"rust": cache}, review_dir=review)
        bind = argv.index(str(cache / "registry"))
        self.assertEqual(argv[bind - 1:bind + 2],
                         ["--ro-bind", str(cache / "registry"), "/tmp/cargo/registry"])
        diff = argv.index(str(review))
        self.assertEqual(argv[diff - 1:diff + 2], ["--ro-bind", str(review), "/opt/review"])

    def test_unknown_ecosystem_or_missing_cache_is_refused(self):
        with self.assertRaises(ValueError):
            contained.command(**self.layout(), entry=["true"],
                              dependency_caches={"npm": self.cache_parent})
        with self.assertRaises(FileNotFoundError):
            contained.command(**self.layout(), entry=["true"],
                              dependency_caches={"rust": self.cache_parent / "absent"})


REPO, HEAD = "acme/widgets", "a" * 40


class TurnPrefetchTests(unittest.TestCase):
    """#51 with #49: a slow prefetch is visible, and the sandbox's clock starts after it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.root.chmod(0o700)
        tokens = {}
        for login in ("read", "review", "fix", "adj"):
            path = self.root / f"{login}.pat"
            path.write_text("DUMMY_" + login)
            path.chmod(0o600)
            tokens[login] = str(path)
        self.loop = {"id": "widgets", "repo": REPO, "base": "main", "cap": 3,
                     "state_dir": str(self.root / "state"), "fixers": ["fix"],
                     "reviewers": ["review"], "tokens": tokens, "read_token": "read",
                     "reviewer_seat": "review", "adjudicator": {"route": "widgets-breach"},
                     "seats": {"reviewer": {"login": "review"}, "fixer": {"login": "fix"}}}

    def test_prefetch_is_recorded_before_and_after_and_the_sandbox_gets_its_whole_budget(self):
        from review_loop import broker_ipc, trusted_turn
        events = []

        def stage(_loop, **kw):
            kw["sandbox_root"].mkdir()
            return kw["sandbox_root"]

        def prepare(checkout, cache, rust, timeout):
            events.append(("prepare", time.monotonic()))
            time.sleep(0.4)                     # a slow fetch, before any sandbox exists
            return [deps.Prefetch("rust", deps.READY, "1 crates.io crates from Cargo.lock",
                                  cache / "cargo", seconds=0.4)]

        def run(**kw):
            events.append(("sandbox", time.monotonic(), kw["timeout"]))
            return subprocess.CompletedProcess([], 0, "", "")

        class Inference:
            def __init__(self, directory, *a, **k):
                self.directory = directory

            def __enter__(self):
                self.directory.mkdir()
                return self

            def __exit__(self, *a):
                return False
        for name in ("venv", "runtime", "rust"):
            (self.root / name).mkdir()
        progress = []
        scope = broker_ipc.RunScope(REPO, 7, HEAD, "adjudicator", "fix-7", "rid",
                                    str(self.root / "runs.sqlite"))
        with mock.patch.object(trusted_turn, "_safe_code_snapshot",
                               side_effect=lambda src, dst: dst.mkdir()), \
             mock.patch.object(trusted_turn.trusted_fetch, "stage", side_effect=stage), \
             mock.patch.object(trusted_turn.inference_proxy, "InferenceCapability", Inference), \
             mock.patch.object(deps, "prepare", side_effect=prepare), \
             mock.patch.object(contained, "run", side_effect=run), \
             self.assertRaises(trusted_turn.TurnDenied):
            trusted_turn.run_turn(self.loop, scope, source=self.root, venv=self.root / "venv",
                                  runtime=self.root / "runtime", rust=self.root / "rust",
                                  upstream="https://model.invalid", key="k", model="m",
                                  prompt="RULE", timeout=5, work_root=self.root / "work",
                                  progress=progress.append)
        [(_, fetched_at), (_, sandbox_at, budget)] = events
        self.assertEqual(budget, 5)                       # the prefetch took none of it
        self.assertGreaterEqual(sandbox_at - fetched_at, 0.4)
        self.assertEqual(len(progress), 2, progress)
        self.assertTrue(progress[0].startswith("fetching"), progress)
        self.assertIn(f"bounded at {deps.FETCH_TIMEOUT}s", progress[0])
        self.assertEqual(progress[1], "rust: ready — 1 crates.io crates from Cargo.lock (0.4s)")

    def test_a_failing_progress_sink_never_fails_the_turn(self):
        from review_loop import trusted_turn
        calls = []

        def sink(text):
            calls.append(text)
            raise OSError("ledger busy")
        trusted_turn._report(sink, "x")
        trusted_turn._report(None, "x")
        self.assertEqual(calls, ["x"])


class LedgerTests(unittest.TestCase):
    """Prefetch outcomes live in the run ledger, so status/explain show them after the fact."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.root.chmod(0o700)
        self.runtime = self.root / "runtime.json"
        self.runtime.write_text("{}")
        self.runtime.chmod(0o600)

    def row(self, sup, state="running", owner="w"):
        with mock.patch.object(sup, "_spawn"):
            sup.enqueue(f"d{time.monotonic_ns()}", REPO, 7, HEAD, "reviewer")
        with sqlite3.connect(sup.db) as con:
            run_id = con.execute("SELECT id FROM runs ORDER BY created DESC").fetchone()[0]
            con.execute("UPDATE runs SET state=?, owner=?, generation='g', lease=? WHERE id=?",
                        (state, owner, time.time() + 60, run_id))
        return run_id

    def test_only_the_owner_of_a_live_run_records_and_it_is_bounded(self):
        from review_loop.run_supervisor import Supervisor, dependency_view
        sup = Supervisor(self.root / "ledger.sqlite", production_config=self.runtime,
                         hermes_home=self.root)
        run_id = self.row(sup)
        sup.record_dependencies(run_id, "w", "rust: unavailable — " + "z" * 5000 + "\x1b[31m")
        sup.record_dependencies(run_id, "intruder", "rust: ready — forged")
        [view] = dependency_view(sup.db, REPO, 7)
        self.assertTrue(view["deps"].startswith("rust: unavailable — zzz"))
        self.assertLessEqual(len(view["deps"]), deps.LEDGER_MAX)
        self.assertNotIn("\x1b", view["deps"])
        self.assertEqual((view["seat"], view["pr"], view["state"]), ("reviewer", 7, "running"))
        self.assertEqual(dependency_view(sup.db, REPO, 8), [])
        self.assertIsNone(dependency_view(self.root / "absent.sqlite", REPO))
        # status JSON (python -m review_loop.run_supervisor status) carries it for failed runs.
        with sqlite3.connect(sup.db) as con:
            con.execute("UPDATE runs SET state='failed' WHERE id=?", (run_id,))
        [failed] = sup.status()
        self.assertTrue(failed["deps"].startswith("rust: unavailable"))

    def test_the_worker_records_every_phase_the_turn_reports(self):
        from types import SimpleNamespace
        from review_loop import config, gh, run_supervisor, seat_model, trusted_turn
        from review_loop.run_supervisor import Supervisor, dependency_view, describe_dependencies
        sup = Supervisor(self.root / "ledger.sqlite", production_config=self.runtime,
                         hermes_home=self.root)
        run_id = self.row(sup, state="launching")
        seen = []

        def run_turn(_loop, _scope, **kw):
            kw["progress"]("fetching — started 12:00:00Z, bounded at 300s")
            seen.append(dependency_view(sup.db, REPO, 7)[0]["deps"])
            kw["progress"]("rust: unavailable — Cargo.lock pins a git dependency")
            return 0
        loop = {"id": "widgets", "repo": REPO, "read_token": "read",
                "state_dir": str(self.root / "state")}
        settings = {k: str(self.root) for k in ("source", "venv", "runtime", "rust")}
        with mock.patch.object(seat_model, "load_runtime", return_value=settings), \
             mock.patch.object(seat_model, "resolve_seat"), \
             mock.patch.object(config, "by_repo", return_value=loop), \
             mock.patch.object(gh, "api", return_value={"head": {"sha": HEAD, "ref": "b"}}), \
             mock.patch.object(gh, "reviews", return_value=[]), \
             mock.patch.object(run_supervisor, "effective_reviews", return_value=[]), \
             mock.patch.object(run_supervisor, "pr_change",
                               return_value=SimpleNamespace(diff="d", record="r", partial="")), \
             mock.patch.object(run_supervisor, "isolated_prompt", return_value="P"), \
             mock.patch.object(trusted_turn, "run_turn", side_effect=run_turn), \
             mock.patch.object(sup, "recover"):
            sup._run_production(run_id, "w")
        self.assertEqual(seen, ["fetching — started 12:00:00Z, bounded at 300s"])
        [row] = dependency_view(sup.db, REPO, 7)
        self.assertEqual(row["deps"], "rust: unavailable — Cargo.lock pins a git dependency")
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(describe_dependencies(row),
                         "reviewer #7 @ aaaaaaa succeeded — rust: unavailable — Cargo.lock pins "
                         "a git dependency")

    def test_a_slow_prefetch_keeps_its_lease_and_is_never_taken_for_a_lost_worker(self):
        from review_loop.run_supervisor import Supervisor
        # child_timeout matters: _run_one's launching lease is child_timeout + lease_seconds, and
        # with the 120 s default no sweep inside this test could ever reclaim the run, heartbeat
        # or not. The lease (5 s) is five sweep periods and three heartbeat periods (5/3 s), so a
        # live heartbeat has ~3.3 s of scheduling slack: what varies is whether it runs at all,
        # never how promptly. The pre-sandbox phase lasts over two lease lengths (11 s), so a
        # run nobody renews is reclaimed by the sweeps in it, deterministically.
        sup = Supervisor(self.root / "ledger.sqlite", production_config=self.runtime,
                         hermes_home=self.root, lease_seconds=5.0, child_timeout=5.0)
        run_id = self.row(sup, state="claimed")
        seen = []

        def lease(rid):
            with sqlite3.connect(sup.db) as con:
                return con.execute("SELECT state, lease FROM runs WHERE id=?", (rid,)).fetchone()

        def slow_turn(rid, owner):
            # What _run_production does before any GitHub read: running, on a one-lease lease.
            with sqlite3.connect(sup.db) as con:
                con.execute("UPDATE runs SET state='running', lease=? WHERE id=?",
                            (time.time() + sup.lease_seconds, rid))
            started = lease(rid)[1]
            for _ in range(11):                 # 11 s of prefetch: over two lease lengths
                time.sleep(1.0)
                Supervisor(sup.db).recover()    # a concurrent sweep, as the watchdog runs one
                state, until = lease(rid)
                seen.append((state, until, time.time()))
            seen.insert(0, ("start", started, None))
        with mock.patch.object(sup, "_claim", return_value=(run_id, "w")), \
             mock.patch.object(sup, "_run_production", side_effect=slow_turn):
            sup._run_one()
        start, *sweeps = seen
        self.assertEqual({state for state, _, _ in sweeps}, {"running"}, seen)
        # The lease was renewed during the pre-sandbox phase: it never went backwards, it was
        # always in the future when a sweep looked, and by the end it had moved past the
        # start's lease by more than a lease length (the start's own lease had expired).
        leases = [start[1]] + [until for _, until, _ in sweeps]
        self.assertEqual(leases, sorted(leases), seen)
        self.assertTrue(all(until > now for _, until, now in sweeps), seen)
        self.assertGreater(leases[-1], start[1] + sup.lease_seconds, seen)

    def test_status_and_explain_lines_come_from_the_ledger(self):
        from review_loop import cli, config
        from review_loop.run_supervisor import Supervisor
        (self.root / "state").mkdir()
        sup = Supervisor(self.root / "state/review-loop-runs.sqlite",
                         production_config=self.runtime, hermes_home=self.root)
        run_id = self.row(sup)
        loop = {"repo": REPO}
        with mock.patch.object(config, "home", return_value=self.root):
            self.assertEqual(cli._dependency_lines(loop), [])
            sup.record_dependencies(run_id, "w", "rust: ready — 224 crates.io crates (2.4s)")
            self.assertEqual(cli._dependency_lines(loop, 7),
                             ["reviewer #7 @ aaaaaaa running — rust: ready — 224 crates.io "
                              "crates (2.4s)"])
            self.assertEqual(cli._dependency_lines({"repo": "other/repo"}), [])
        with mock.patch.object(config, "home", return_value=self.root / "absent"):
            self.assertEqual(cli._dependency_lines(loop), [])

    def test_the_cap_override_reaches_the_worker(self):
        from review_loop import run_supervisor
        from review_loop.run_supervisor import Supervisor
        sup = Supervisor(self.root / "ledger.sqlite", production_config=self.runtime,
                         hermes_home=self.root)
        with mock.patch.dict(os.environ, {deps.CAP_ENV: "6", "GH_TOKEN": "ghp_x"}), \
             mock.patch.object(run_supervisor.subprocess, "Popen") as popen:
            sup._spawn()
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env[deps.CAP_ENV], "6")
        self.assertNotIn("GH_TOKEN", env)


@unittest.skipUnless((_toolchain() / "bin/cargo").exists(), "stable Rust toolchain unavailable")
@unittest.skipUnless(_bwrap_works(), "unprivileged bubblewrap unavailable")
@unittest.skipUnless(_crates_io_reachable(), "crates.io unreachable (no network)")
class RealPrefetchAndOfflineBuild(Base):
    """The whole point: real cargo fetch on the host, real offline build in the real sandbox."""

    def test_seat_builds_offline_with_the_cache_and_cannot_without_it(self):
        self.write()
        rust = _toolchain()
        [result] = deps.prepare(self.checkout, self.cache_parent, rust, timeout=240)
        self.assertTrue(result.ready, (result.reason, result.detail))
        layout = {name: self.root / name for name in ("code", "venv", "runtime", "home")}
        for directory in layout.values():
            directory.mkdir()
        query = self.root / "query"
        query.write_text("x")
        script = ("touch /tmp/cargo/registry/poison 2>/dev/null && echo CACHE-WRITABLE; "
                  "cargo metadata --format-version 1 --locked >/dev/null && echo METADATA-OK; "
                  "cargo build --offline --locked -j 2 && echo BUILD-OK")

        def build(caches):
            shutil.rmtree(self.checkout / "target", ignore_errors=True)
            return contained.run(**layout, checkout=self.checkout, rust=rust, query=query,
                                 entry=["/bin/sh", "-c", script], timeout=240,
                                 dependency_caches=caches)

        good = build({"rust": result.cache})
        self.assertEqual(good.returncode, 0, good.stderr[-2000:])
        self.assertIn("METADATA-OK", good.stdout)
        self.assertIn("BUILD-OK", good.stdout)
        self.assertNotIn("CACHE-WRITABLE", good.stdout)
        self.assertIn("Compiling itoa", good.stderr)
        bare = build({})
        self.assertNotEqual(bare.returncode, 0)
        self.assertNotIn("BUILD-OK", bare.stdout)
        self.assertIn("itoa", bare.stderr)


if __name__ == "__main__":
    unittest.main()
