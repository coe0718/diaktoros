"""One place resolves who a credential is (#1163 M1a): ``gh.principal``.

Every seat and reader identity check (the broker's authorizers, the review receipt, safe_push's
fixer identity, trusted_fetch) asks ``gh.principal``, each with its own transport. A GitHub App
identity cannot call ``GET /user`` with its installation token, so the App path (M1b) has one
function to change; a new ``/user`` call elsewhere would bypass it. The fixer's git identity
accepts a GitHub App bot login (``name[bot]``) and its noreply address.
"""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import ast
import itertools
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diaktoros import broker, gh, safe_push  # noqa: E402

# The only modules that may still name /user: the seam itself, and the read-only diagnostics
# that read a PAT's own response headers (expiry, scopes), which M1b makes App-aware.
USER_CALLERS = {"diaktoros/gh.py", "diaktoros/doctor/identity_checks.py", "diaktoros/selftest.py"}
# The Python that ships: the package, the gate/watchdog scripts, and the dashboard and desktop pages.
SCANNED = ("diaktoros", "scripts", "dashboard", "desktop")

# Two detectors read each file and ``user_calls`` reports a line either finds: ``user_reads`` reads
# one string expression whole, with the parts only run time knows (``f"{base}/user"``);
# ``user_paths`` follows names bound to literals through the file (``path = "/us"; path + "er"``).
_UNKNOWN = "\0"                       # a part of a string only known at run time
# An endpoint, not a sentence: one run of non-blank text, matched whole, that ends in ``/user`` and
# goes on only into a subpath, a query or a part run time fills. What comes before it is nothing, a
# ``scheme://host`` or bare host, or a run that holds a part only run time knows (``f"{base}/user"``,
# ``f"/{version}/user"``). So ``/user``, ``/user/emails``, ``/user?x`` and
# ``https://api.github.com/user`` are found; ``/users/x`` is not, and neither is prose that merely
# names it (``GET /user``, ``could not read /user``) or a file path (``/home/user``).
_USER_ENDPOINT = re.compile(r"(?:[^/\s]*://[^/\s]*|(?:[\w-]+\.)+[\w-]+(?::\d+)?|\S*\0\S*)?/user(?:[/?#\0]\S*)?")


_NOT_STATIC = object()                # an interpolated expression that is not a constant or a spelled string
_CONVERT = {-1: lambda value: value, ord("s"): str, ord("r"): repr, ord("a"): ascii}


def _text(node: ast.AST) -> str | None:
    """The text a string expression spells, ``_UNKNOWN`` standing for what is only known at run
    time; ``None`` when ``node`` is not a string expression. Reads a literal, an f-string (with
    its ``!s``/``!r``/``!a`` conversions and ``:spec`` applied to what is a constant), a ``+`` and
    a ``"/".join([...])``, so a path built in pieces reads as the path it makes."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.FormattedValue):
        return _formatted_value(node)
    if isinstance(node, ast.JoinedStr):
        return "".join(_piece(part) for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _text(node.left), _text(node.right)
        if left is None and right is None:
            return None
        return (_UNKNOWN if left is None else left) + (_UNKNOWN if right is None else right)
    if _is_join(node):
        separator = _text(node.func.value)
        if separator is not None:
            return separator.join(_piece(part) for part in node.args[0].elts)
    return None


def _piece(node: ast.AST) -> str:
    """One part of a built string: what it spells, or ``_UNKNOWN`` when only run time knows."""
    text = _text(node)
    return _UNKNOWN if text is None else text


def _is_join(node: ast.AST) -> bool:
    """``sep.join([...])`` or ``sep.join((...))``: a join of a list or tuple written in place."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join" and len(node.args) == 1
            and isinstance(node.args[0], ast.List | ast.Tuple))


def _static(node: ast.AST):
    """The value of an interpolated expression when a constant or a spelled string, else ``_NOT_STATIC``."""
    if isinstance(node, ast.Constant):
        return node.value
    text = _text(node)
    return _NOT_STATIC if text is None else text


def _formatted_value(node: ast.FormattedValue) -> str | None:
    """What one ``{value!conversion:spec}`` of an f-string spells: the value after its conversion
    and format spec, so ``{'us'!s}`` is ``us`` and ``{'user':s}`` is ``user``. ``None`` when the value
    or the spec is only known at run time; a part-known string is kept only when nothing would change it."""
    value = _static(node.value)
    spec = "" if node.format_spec is None else _text(node.format_spec)
    if value is _NOT_STATIC or spec is None or _UNKNOWN in spec:
        return None
    if isinstance(value, str) and _UNKNOWN in value:
        return value if node.conversion in (-1, ord("s")) and not spec else None
    try:
        return format(_CONVERT[node.conversion](value), spec)
    except (KeyError, TypeError, ValueError):
        return None


def _parts(node: ast.AST) -> list[ast.AST]:
    """The nodes ``_text`` reads into ``node``'s text, so a fragment of a spelled path is not
    read again as a path of its own (``f"/user{'s'}/alice"`` spells ``/users/alice``, not ``/user``)."""
    if isinstance(node, ast.JoinedStr):
        return list(node.values)
    if isinstance(node, ast.FormattedValue):
        return [node.value] + ([node.format_spec] if node.format_spec is not None else [])
    if isinstance(node, ast.BinOp):
        return [node.left, node.right]
    if _is_join(node):
        return [node.func, node.func.value, node.args[0], *node.args[0].elts]
    return []


def user_reads(source: str) -> list[int]:
    """The lines of ``source`` that spell a ``/user`` endpoint (``_USER_ENDPOINT``): a literal, or one
    built in an f-string, a concatenation or a join. The whole expression is read, never its
    fragments on their own; an interpolation or operand that is not a string (a call) is searched
    for its own paths. Message text that names the endpoint (``"GET /user"``, anything with a blank
    in it) is not an endpoint, and a bare string statement (a docstring) is prose, not a call. A
    path assembled from a variable or a call's result is out of its reach."""
    tree = ast.parse(source)
    prose = {id(node.value) for node in ast.walk(tree)
             if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)}
    lines, fragments, pending = set(), set(), [tree]
    while pending:                    # a parent is read before its children, so it claims its fragments
        node = pending.pop()
        if id(node) in prose:
            continue
        text = _text(node)
        if text is not None:
            if id(node) not in fragments and _USER_ENDPOINT.fullmatch(text):
                lines.add(node.lineno)
            fragments.update(id(part) for part in _parts(node))
        pending.extend(ast.iter_child_nodes(node))
    return sorted(lines)


# What the /user scan below reads as a call on the authenticated user: the path itself, with an
# optional sub-path or query after it (``/user/emails``) or a host before it
# (``https://api.github.com/user``); never ``/users/<login>`` or ``/userinfo``.
USER_PATH = re.compile(r"(?:https?://[^/]+)?/user(?:[/?#].*)?\Z", re.DOTALL)
CANDIDATES = 64     # most strings the scan tracks for one expression or name


def _combos(parts):
    """Every way to pick one string from each set in ``parts``: none when a part is unknown (an
    empty set) or the product is large."""
    size = 1
    for part in parts:
        size *= len(part)
    return itertools.product(*parts) if 0 < size <= CANDIDATES else ()


def _filled(template, args, keywords=None, percent=False):
    """``template`` with ``args`` (and ``keywords``) filled in, or ``None`` when they do not fit.

    Any failure means "the code cannot build this string from these candidates", never a
    failure of the scan. The name table merges every scope of a file, so a candidate can be a
    value the code never pairs with this template: ``"{x.foo}".format(x=x)`` tried with an
    unrelated ``x = "bar"`` raises ``AttributeError``, ``"{0[a]}"`` a ``TypeError``, and a huge
    width a ``MemoryError``. So the catch is deliberately broad."""
    try:
        return template % tuple(args) if percent else template.format(*args, **(keywords or {}))
    except Exception:  # noqa: BLE001  see the docstring: an unfit candidate is skipped
        return None


def _formatted(call, env):
    """``_strings`` of ``"...".format(*args, **keywords)`` over known strings."""
    names = [kw.arg for kw in call.keywords]
    values = call.args + [kw.value for kw in call.keywords]
    done = (_filled(chosen[0], chosen[1:len(call.args) + 1],
                    dict(zip(names, chosen[len(call.args) + 1:])))
            for chosen in _combos([_strings(call.func.value, env)]
                                  + [_strings(value, env) for value in values]))
    return {text for text in done if text is not None}


def _strings(node, env):
    """The strings ``node`` can statically be (an empty set when it cannot be known): a literal
    (adjacent literals are already one ``Constant`` to ``ast``), a name bound to such strings in
    ``env``, and ``+``, an f-string, ``%`` and ``.format`` over them."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.Name):
        return set(env.get(node.id, ()))
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {left + right for left, right in
                _combos([_strings(node.left, env), _strings(node.right, env)])}
    if isinstance(node, ast.JoinedStr):
        values = [part.value if isinstance(part, ast.FormattedValue) and part.conversion == -1
                  and part.format_spec is None else part for part in node.values]
        return {"".join(chosen) for chosen in _combos([_strings(v, env) for v in values])}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        args = node.right.elts if isinstance(node.right, ast.Tuple) else [node.right]
        done = (_filled(chosen[0], chosen[1:], percent=True) for chosen in _combos(
            [_strings(node.left, env)] + [_strings(arg, env) for arg in args]))
        return {text for text in done if text is not None}
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format" and all(kw.arg for kw in node.keywords)):
        return _formatted(node, env)
    return set()


def _bindings(tree):
    """``(name, expression)`` for each simple assignment in ``tree``: ``=`` (tuple unpacking
    too), ``:=``, an annotated ``=``, and ``name += value`` (as ``name + value``)."""
    pairs = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            targets, value = [node.target], node.value
        elif (isinstance(node, ast.AugAssign) and isinstance(node.op, ast.Add)
              and isinstance(node.target, ast.Name)):
            targets, value = [node.target], ast.BinOp(
                left=ast.Name(id=node.target.id), op=ast.Add(), right=node.value)
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                pairs.append((target.id, value))
            elif (isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple)
                  and len(target.elts) == len(value.elts)):
                pairs.extend((t.id, v) for t, v in zip(target.elts, value.elts)
                             if isinstance(t, ast.Name))
    return pairs


def _environment(tree):
    """Name -> every string it is bound to anywhere in ``tree``. One table for the whole file,
    not one per scope: a name reused for another value only adds candidates, never hides one.
    A name that keeps growing (``p += "x"`` in a loop) stops at ``CANDIDATES`` strings."""
    pairs, env, changed = _bindings(tree), {}, True
    while changed:
        changed = False
        for name, value in pairs:
            known = env.setdefault(name, set())
            new = sorted(_strings(value, env) - known)[:max(CANDIDATES - len(known), 0)]
            if new:
                known.update(new)
                changed = True
    return env


def user_paths(source: str) -> list[int]:
    """The line numbers in ``source`` that hold, or statically build, a ``/user`` path: the
    literal, adjacent literals (``"/us" "er"``), a ``+`` / f-string / ``%`` / ``.format`` of
    literals, and a name bound to any of those (``path = "/us"; api(loop, path + "er")``).

    This is a tripwire, not a gate (the broker authorizers check identities themselves). It
    cannot see a path it cannot compute from the file's own literals: one that arrives as an
    argument from another module, is read from data or config, or is built by a function call,
    a loop or a comprehension. A hit is the reviewer's cue; a miss is not proof."""
    tree = ast.parse(source)
    env = _environment(tree)
    return sorted({node.lineno for node in ast.walk(tree) if isinstance(node, ast.expr)
                   and not isinstance(getattr(node, "ctx", None), (ast.Store, ast.Del))
                   and any(USER_PATH.match(text) for text in _strings(node, env))})


def user_calls(root: Path, allowed=USER_CALLERS) -> list[str]:
    """``path:line: source`` for every ``/user`` path in the Python under ``root`` that ships
    (``SCANNED``), apart from the ``allowed`` files (relative paths): a line either detector,
    ``user_reads`` or ``user_paths``, finds."""
    found = []
    for top in SCANNED:
        for path in sorted((root / top).rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if rel in allowed or "__pycache__" in path.parts:
                continue
            source = path.read_text(encoding="utf-8")
            lines = source.splitlines()
            found += [f"{rel}:{number}: {lines[number - 1].strip()}"
                      for number in sorted({*user_reads(source), *user_paths(source)})]
    return found


class Seam(unittest.TestCase):
    def test_principal_returns_the_account_or_none(self):
        with mock.patch.object(gh, "api", return_value={"login": "fix", "id": 7}) as api:
            self.assertEqual(gh.principal({}, "fix"), {"login": "fix", "id": 7})
        api.assert_called_once_with({}, "/user", login="fix")
        for answer in (None, [], "Bad credentials"):
            with self.subTest(answer=answer), mock.patch.object(gh, "api", return_value=answer):
                self.assertIsNone(gh.principal({}, "fix"))

    def test_the_callers_transport_is_used_and_its_failure_kept(self):
        seen = []
        self.assertEqual(gh.principal({}, "fix", fetch=lambda path: seen.append(path) or {"id": 1}),
                         {"id": 1})
        self.assertEqual(seen, ["/user"])

        def raises(path):
            raise broker.BrokerDenied("GitHub read request failed")
        with self.assertRaisesRegex(broker.BrokerDenied, "GitHub read request failed"):
            gh.principal({}, "fix", fetch=raises)

    def test_no_identity_check_calls_user_directly(self):
        self.assertEqual(user_calls(ROOT), [], "resolve identities through gh.principal")


class UserReads(unittest.TestCase):
    """``user_reads``, and the walk over the whole tree that ships, must not be ones a new
    ``/user`` read can walk around."""

    FORMS = {
        "double-quoted literal": 'api(loop, "/user")',
        "single-quoted literal": "api(loop, '/user')",
        "a full URL": 'get("https://api.github.com/user")',
        "a /user subpath": 'api(loop, "/user/emails")',
        "a query string": 'api(loop, "/user?per_page=1")',
        "an f-string": 'api(loop, f"{base}/user")',
        "an f-string with a query": 'api(loop, f"/user?page={page}")',
        "an f-string with a suffix": 'api(loop, f"/user{suffix}")',
        "an f-string interpolating the segment": "api(loop, f\"{base}/{'user'}\")",
        "a concatenation": 'api(loop, base + "/user")',
        "a concatenation split inside the segment": 'api(loop, "/us" + "er")',
        "a join": 'api(loop, "/".join([base, "user"]))',
        "a join of a tuple": 'api(loop, "/".join((base, "user")))',
        "a conversion on a literal in an f-string": "api(loop, f\"/{'us'!s}er\")",
        "a format spec on a literal": "api(loop, f\"/{'user':s}\")",
        "a padding spec on a literal": "api(loop, f\"/{'use':r<4}\")",
        "a conversion and a spec on a literal": "api(loop, f\"/{'user'!s:>4}\")",
        "a concatenation interpolated into an f-string": "api(loop, f\"/{'us' + 'er'}\")",
        "a call inside the interpolation of a users path": "api(loop, f\"/users/{get('/user')}\")",
        "a call among the operands of a concatenation": 'api(loop, "/users/" + get("/user"))',
        "a scheme and a host built at run time": 'get(f"{scheme}://{host}/user")',
        "a base and a query": 'api(loop, f"{base}/user?page=1")',
        "a version segment before it": 'api(loop, f"/{version}/user")',
        "two run time segments before it": 'api(loop, f"{base}/{version}/user")',
        "a host with no scheme": 'get("api.github.com/user")',
    }
    CLEAN = {
        "the users endpoint": 'api(loop, "/users/someone")',
        "the users endpoint in an f-string": 'api(loop, f"/users/{login}")',
        "a field called user": 'pr.get("user")',
        "a longer segment": 'api(loop, "/user_agent")',
        "a docstring naming it": '"""GET /user answers who the token is."""',
        "a comment naming it": "x = 1  # GET /user",
        "a message naming it": 'say(f"{login}: GET /user failed")',
        "a message ending in it": 'say("GET /user")',
        "a message ending in it and a question mark": 'say("Use GET /user?")',
        "a message ending in it and a colon": 'say("could not read /user:")',
        "a bare message ending in it": 'say("read /user")',
        "an f-string message ending in it": 'say(f"{login} cannot call GET /user")',
        "a message with a blank after the query mark": 'say("GET /user? no")',
        "a message ending in a path of its own": 'say("see /home/user")',
        "a file path of its own": 'home = "/home/user"',
        "a filesystem join": 'os.path.join(home, "user")',
        "a users path whose s is interpolated": "api(loop, f\"/user{'s'}/alice\")",
        "a users path split by a concatenation": 'api(loop, "/user" + "s/alice")',
        "a users path split by a join": 'api(loop, "".join(["/user", "s", "/alice"]))',
        "a users path built from a literal conversion": "api(loop, f\"/{'users'!s}/alice\")",
        "a literal quoted by an ascii conversion": "api(loop, f\"/{'user'!a}\")",
        "a literal quoted by its conversion": "api(loop, f\"/{'user'!r}\")",
        "a number after it": 'api(loop, f"/user{1}")',
        "a spec that pads it": "api(loop, f\"/{'user':_<6}\")",
    }

    def test_every_form_of_a_user_read_is_caught(self):
        for name, source in self.FORMS.items():
            with self.subTest(name):
                self.assertEqual(user_reads(f"x = 1\n{source}\n"), [2])

    def test_prose_and_other_endpoints_are_not_mistaken_for_one(self):
        for name, source in self.CLEAN.items():
            with self.subTest(name):
                self.assertEqual(user_reads(source + "\n"), [])

    def test_a_docstring_that_names_it_is_prose_but_the_function_body_is_not(self):
        source = 'def f(base):\n    """GET /user."""\n    return base + "/user"\n'
        self.assertEqual(user_reads(source), [3])

    def test_the_whole_tree_that_ships_is_scanned_with_the_same_allowlist(self):
        read = 'def f(base):\n    return f"{base}/user"\n'
        flagged = ("diaktoros/new.py", "diaktoros/sub/deeper.py", "scripts/watchdog.py",
                   "dashboard/plugin_api.py", "desktop/page.py",
                   "scripts/gh.py")                    # the allowlist is by path, not by name
        quiet = ("diaktoros/gh.py", "diaktoros/doctor/identity_checks.py", "diaktoros/selftest.py",
                 "tests/test_x.py", "tools/spike.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in (*flagged, *quiet):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(read)
            found = {line.split(":")[0] for line in user_calls(root)}
        self.assertEqual(found, set(flagged))

    def test_a_finding_names_the_file_and_the_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "watchdog.py").write_text('x = 1\nurl = base + "/user"\n')
            self.assertEqual(user_calls(Path(tmp)), ['scripts/watchdog.py:2: url = base + "/user"'])

    def test_a_diagnostic_that_names_the_endpoint_is_not_a_finding_in_a_scanned_file(self):
        """The walk the principal test runs: message text ending in ``/user`` passes, a read beside it does not."""
        source = 'say("GET /user")\nsay("Use GET /user?")\nsay(f"{login} cannot call GET /user")\nurl = base + "/user"\n'
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "scripts").mkdir()
            (Path(tmp) / "scripts" / "watchdog.py").write_text(source)
            self.assertEqual(user_calls(Path(tmp)), ['scripts/watchdog.py:4: url = base + "/user"'])


class UserScan(unittest.TestCase):
    """The scan behind ``test_no_identity_check_calls_user_directly`` (#1175): a ``/user`` that
    is not written as one quoted literal on one line still counts."""

    FOUND = (
        'gh.api(loop, "/user", login=login)',
        "gh.api(loop, '/user', login=login)",
        'gh.api(loop, "/us" "er", login=login)',
        'gh.api(loop, ("/us"\n "er"), login=login)',
        'path = "/us"\ngh.api(loop, path + "er", login=login)',
        'path = "/us"\npath += "er"\ngh.api(loop, path, login=login)',
        'a, b = "/us", "er"\ngh.api(loop, a + b, login=login)',
        'path = "/user"\ngh.api(loop, path, login=login)',
        'kind = "user"\ngh.api(loop, f"/{kind}", login=login)',
        'gh.api(loop, f"/{\'user\'}", login=login)',
        'gh.api(loop, "/%s" % "user", login=login)',
        'gh.api(loop, "/{}".format("user"), login=login)',
        'gh.api(loop, "/{k}".format(k="user"), login=login)',
        'gh.api(loop, "/user/emails", login=login)',
        'gh.api(loop, "https://api.github.com/user", login=login)',
    )
    LEFT_ALONE = (
        'gh.api(loop, f"/users/{login}", login=login)',
        'gh.api(loop, "/users/" + login)',
        'gh.api(loop, "/userinfo")',
        'gh.api(loop, "/us" "ers")',
        'role = "user"\nmessages = [{"role": role}]',
        'path = "/us"\nprint(path)',
        '"""GET /user answers with the account."""',
        'gh.api(loop, path, login=login)',
        'gh.api(loop, build() + "er")',
        'gh.api(loop, "/{}".format(1, 2), "%s %s" % ("user",))',
    )

    def test_a_user_path_is_found_however_it_is_written(self):
        for source in self.FOUND:
            with self.subTest(source=source):
                self.assertTrue(user_paths(source), "a /user path went unseen")

    def test_other_paths_and_words_are_left_alone(self):
        for source in self.LEFT_ALONE:
            with self.subTest(source=source):
                self.assertEqual(user_paths(source), [])

    def test_the_line_of_the_use_is_reported(self):
        self.assertEqual(user_paths('x = 1\ny = "/us"\nz = y + "er"\n'), [3])
        self.assertEqual(user_paths('x = 1\ngh.api(loop, "/us"\n "er")\n'), [2])

    def test_a_name_that_keeps_growing_ends_the_scan(self):
        self.assertEqual(user_paths('p = "/x"\nfor _ in range(3):\n    p += "y"\n'), [])
        self.assertEqual(user_paths('p = "/us"\nfor _ in range(3):\n    p += "er"\nuse(p)\n'), [4])

    def test_a_candidate_that_does_not_fit_its_template_is_skipped(self):
        """The name table merges every scope, so ``x = "bar"`` in one function is also tried as
        the ``x`` that another function formats with ``"{x.foo}"``: that raises
        ``AttributeError`` (``.real`` on a str, ``[a]`` on a str: ``TypeError``), and the scan
        has to go on, still finding a real hit in the same file."""
        merged_scopes = (
            'class Thing:\n    foo = "ok"\n\n\ndef one():\n    x = "bar"\n    return x\n\n\n'
            'def two():\n    x = Thing()\n    return "{x.foo}".format(x=x)\n')
        for source in (merged_scopes, 'x = "a"\nprint("{0.real}".format(x))\n',
                       'x = "a"\nprint("{0[a]}".format(x))\n', 'print("%d" % ("a",))\n',
                       'x = "a"\nprint("{:99999999999}".format(x))\n'):
            with self.subTest(source=source):
                self.assertEqual(user_paths(source), [])
                self.assertEqual(user_paths(source + 'path = "/us"\ngh.api(path + "er")\n'),
                                 [source.count("\n") + 2])

    def run_seam(self, files):
        """Run ``Seam.test_no_identity_check_calls_user_directly`` itself, with ``ROOT`` pointing
        at a temporary tree whose ``diaktoros/`` holds ``files``, and return its result."""
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "diaktoros"
            package.mkdir()
            for name, text in files.items():
                (package / name).write_text(text)
            result = unittest.TestResult()
            with mock.patch.dict(globals(), ROOT=Path(raw)):
                Seam("test_no_identity_check_calls_user_directly").run(result)
        return result

    def test_the_seam_test_fails_on_an_assembled_path_it_used_to_miss(self):
        """The old seam test grepped each line for a quoted ``"/user"``. Each of these used to
        leave it green and must now turn it red, naming the file and the line."""
        for source, line in (
                ('def who(loop, login):\n    return gh.api(loop, "/us" "er", login=login)\n', 2),
                ('def who(loop, login):\n    path = "/us"\n'
                 '    return gh.api(loop, path + "er", login=login)\n', 3),
                ('PATH = "/user"\n\n\ndef who(loop, login):\n'
                 '    return gh.api(loop, PATH, login=login)\n', 5)):
            with self.subTest(source=source):
                result = self.run_seam({"evader.py": source, "gh.py": 'PROBE = "/user"\n'})
                self.assertEqual(result.errors, [])
                self.assertEqual(len(result.failures), 1, "the seam test stayed green")
                self.assertIn(f"diaktoros/evader.py:{line}:", result.failures[0][1])

    def test_the_seam_test_spares_its_own_callers_and_other_paths(self):
        """It stays green for the allowed callers and the look-alikes, and a module the merged
        name table cannot format does not turn it into an error."""
        result = self.run_seam({
            "gh.py": 'PROBE = "/user"\n', "selftest.py": 'path = "/us"\nuse(path + "er")\n',
            "fine.py": 'USERS = "/users/x"\nINFO = "/userinfo"\n',
            "scopes.py": 'x = "bar"\n\n\ndef two(x):\n    return "{x.foo}".format(x=x)\n'})
        self.assertEqual((result.errors, result.failures), ([], []))
        self.assertEqual(result.testsRun, 1)

    def test_the_package_scan_reports_an_evader_and_spares_the_seam(self):
        evader = 'diaktoros/evader.py:6: return gh.api(loop, path + "er", login=login)'
        with tempfile.TemporaryDirectory() as raw:
            package = Path(raw) / "diaktoros"
            package.mkdir()
            (package / "evader.py").write_text(
                'from . import gh\n\n\ndef who(loop, login):\n'
                '    path = "/us"\n    return gh.api(loop, path + "er", login=login)\n')
            (package / "gh.py").write_text('PROBE = "/user"\n')
            (package / "fine.py").write_text('USERS = "/users/x"\n')
            self.assertEqual(user_calls(Path(raw), allowed={"diaktoros/gh.py"}), [evader])
            self.assertEqual(user_calls(Path(raw), allowed=set()),
                             [evader, 'diaktoros/gh.py:1: PROBE = "/user"'])


class FixerIdentity(unittest.TestCase):
    def identity(self, account, login="fix", **kw):
        with mock.patch.object(safe_push, "_api", return_value=account):
            return safe_push._fixer_identity({}, login, **kw)

    def test_a_user_and_an_app_bot_get_their_noreply_address(self):
        self.assertEqual(self.identity({"login": "fix", "id": 9}),
                         {"name": "fix", "email": "9+fix@users.noreply.github.com"})
        self.assertEqual(self.identity({"login": "fix-bot[bot]", "id": 346015539},
                                       login="fix-bot[bot]"),
                         {"name": "fix-bot[bot]",
                          "email": "346015539+fix-bot[bot]@users.noreply.github.com"})

    def test_another_account_or_a_bad_id_is_refused(self):
        for account in ({"login": "other", "id": 9}, {"login": "fix", "id": 0},
                        {"login": "fix", "id": True}, {"login": "fix"}, {"id": 9}):
            with self.subTest(account=account), \
                    self.assertRaisesRegex(broker.BrokerDenied, "fixer identity changed"):
                self.identity(account)

    def test_a_login_git_would_not_take_is_refused_with_the_callers_reason(self):
        for name in ("fix bot", "fix[bot]x", "fix[robot]", "fix;rm"):
            with mock.patch.object(safe_push, "_api", return_value={"login": name, "id": 9}):
                with self.subTest(name=name), \
                        self.assertRaisesRegex(broker.BrokerDenied, "invalid fixer identity"):
                    safe_push._fixer_identity({}, name)
                with self.assertRaisesRegex(broker.BrokerDenied, "fixer identity changed"):
                    safe_push._fixer_identity({}, name, invalid="fixer identity changed")


if __name__ == "__main__":
    unittest.main()
