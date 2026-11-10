"""#1239 part 2: a loop on its App's webhook reads the App's own delivery log (``/app/hook/
deliveries``, with the App's JWT) where a repo-hooks loop reads its hooks' deliveries: ``trace
--delivery``, doctor's and selftest's secret evidence, ``selftest --ping``, and the hint for
redelivering a failed gate's event."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_app_cli as app_cli  # noqa: E402
import test_token_file_settings as tfs  # noqa: E402
from diaktoros import (  # noqa: E402
    app_deliveries, config, doctor, gate_failures, gh, github_app, hook_ping, routes, selftest,
    trace,
)
from diaktoros.doctor import hook_checks  # noqa: E402

READ_APP = "acme-reader[bot]"
REPO_ID, OTHER_ID = 42, 99
LOOP = {"id": "widgets", "repo": "acme/widgets", "read_token": "reader",
        "ingress": "app-webhook", "ingress_app": READ_APP,
        "seats": {"reviewer": {"route": "widgets-review"}, "fixer": {"route": "widgets-fix"}},
        "adjudicator": {}}


def delivery(n, event="pull_request", repo=REPO_ID, code=200, at="2026-11-10T10:00:00Z"):
    return {"id": n, "guid": f"guid-{n}", "event": event, "repository_id": repo,
            "status_code": code, "status": "OK" if code else "timed out", "delivered_at": at}


class FakeApp:
    """The App's webhook log as GitHub answers its JWT: newest first, ``per_page`` items a page,
    the next page named by a ``Link`` header; a redelivery adds a new item at the top."""

    def __init__(self, items, per_page=100, answer=200):
        self.items, self.per_page, self.answer = list(items), per_page, answer
        self.calls, self.error = [], ""

    def __call__(self, loop, login, path, method="GET", body=None):
        self.calls.append((method, path, login))
        if self.error:
            return gh.Response(None, self.error, 401, {})
        if path.startswith("/app/hook/deliveries?"):
            start = int(path.split("cursor=")[1]) if "cursor=" in path else 0
            page = self.items[start:start + self.per_page]
            more = start + self.per_page < len(self.items)
            link = (f'<{gh.API}/app/hook/deliveries?per_page=100&cursor={start + self.per_page}>; '
                    'rel="next"') if more else ""
            return gh.Response(page, "", 200, {"link": link} if link else {})
        if method == "POST" and path.endswith("/attempts"):
            self.items.insert(0, delivery(1000 + len(self.calls), "ping", None, self.answer,
                                          "2026-11-10T12:00:00Z"))
            return gh.Response({}, "", 202, {})
        number = int(path.rsplit("/", 1)[1])
        found = next((d for d in self.items if d["id"] == number), None)
        if not found:
            return gh.Response(None, "HTTP 404", 404, {})
        repo = "acme/widgets" if found["repository_id"] == REPO_ID else "acme/other"
        return gh.Response({**found, "request": {"payload": {
            "action": "opened", "number": 7, "repository": {"full_name": repo}}}}, "", 200, {})


class Base(unittest.TestCase):
    def app(self, items, **kwargs):
        fake = FakeApp(items, **kwargs)
        for patch in (mock.patch.object(github_app, "_as_app", side_effect=fake),
                      mock.patch.object(gh, "fetch", side_effect=self.repo)):
            patch.start()
            self.addCleanup(patch.stop)
        return fake

    def repo(self, loop, path, method="GET", body=None, login=None):
        self.assertEqual(path, "/repos/acme/widgets")
        return {"id": REPO_ID}, ""


class Latest(Base):
    def test_this_repositorys_newest_delivery_is_found_past_other_repositories_and_pages(self):
        # A busy App's log: its first page is another repository's, ours is on the second.
        fake = self.app([delivery(n, repo=OTHER_ID) for n in range(30, 27, -1)]
                        + [delivery(5, at="2026-11-10T09:00:00Z"),
                           delivery(6, at="2026-11-10T09:30:00Z")], per_page=3)
        found, error = app_deliveries.latest(LOOP)
        self.assertEqual((found["id"], error), (6, ""))
        self.assertEqual([p for _, p, _ in fake.calls],
                         ["/app/hook/deliveries?per_page=100",
                          "/app/hook/deliveries?per_page=100&cursor=3"])
        self.assertTrue(all(login == READ_APP for _, _, login in fake.calls))   # the App's JWT

    def test_a_link_off_the_api_is_not_followed(self):
        self.assertEqual(app_deliveries._next({"link": '<https://evil.example/x>; rel="next"'}), "")


class Doctor(Base):
    def check(self, items):
        self.app(items)
        return hook_checks.check_app_deliveries(LOOP)

    def test_the_latest_delivery_here_is_the_secrets_evidence(self):
        self.assertEqual(self.check([delivery(1)]), "latest delivery 200")
        rejected = self.check([delivery(2, code=401)])
        self.assertEqual(rejected.status, doctor.MISMATCH, rejected)
        self.assertIn("the App's webhook secret does not match the route's", rejected.detail)
        self.assertIn("hermes dk app-webhook --loop widgets --set", rejected.fix)
        silent = self.check([delivery(3, code=0)])
        self.assertEqual(silent.status, doctor.UNKNOWN, silent)
        self.assertIn("no HTTP response", silent.detail)

    def test_another_repositorys_deliveries_prove_nothing_here(self):
        found = self.check([delivery(1, repo=OTHER_ID, code=401)])
        self.assertTrue(found.startswith("no deliveries for this repository"), found)

    def test_an_unreadable_log_is_unknown(self):
        self.app([]).error = "HTTP 401 bad JWT"
        found = hook_checks.check_app_deliveries(LOOP)
        self.assertEqual(found.status, doctor.UNKNOWN)
        self.assertIn("HTTP 401 bad JWT", found.detail)

    def test_the_app_webhook_line_carries_it(self):
        state = {"url": "u", "want_url": "u", "content_type": "json", "error": "",
                 "events": ["pull_request"], "missing_events": []}
        self.app([delivery(1, code=401)])
        from diaktoros import app_ingress
        with mock.patch.object(app_ingress, "webhook_state", return_value=state):
            check = hook_checks.check_app_webhook(LOOP, offline=False)
        self.assertEqual(check.status, doctor.MISMATCH, check)
        self.app([delivery(1)])
        with mock.patch.object(app_ingress, "webhook_state", return_value=state):
            check = hook_checks.check_app_webhook(LOOP, offline=False)
        self.assertEqual(check.status, doctor.VERIFIED, check)
        self.assertIn("latest delivery 200", check.detail)


class Selftest(Base):
    def run_step(self, items, ping=False, **kwargs):
        fake = self.app(items, **kwargs)
        report = selftest.Report(out=io.StringIO())
        with mock.patch.object(selftest.gh, "hooks_read") as hooks, \
                mock.patch.object(hook_ping, "PING_WAIT", 0.0):
            selftest.check_hook_signatures(report, LOOP, ping=ping)
        hooks.assert_not_called()                    # no repo hooks on purpose
        (step, name, mark, detail, fix), = [tuple(r[:5]) for r in report.results]
        return fake, mark, detail, fix

    def test_read_only_it_judges_the_latest_delivery(self):
        _, mark, detail, _ = self.run_step([delivery(1)])
        self.assertEqual(mark, selftest.PASS, detail)
        self.assertIn("signature accepted", detail)
        _, mark, _, fix = self.run_step([])
        self.assertEqual(mark, selftest.WARN)
        self.assertIn("--ping", fix)

    def test_ping_redelivers_the_newest_ping_and_reads_the_answer(self):
        fake, mark, detail, _ = self.run_step(
            [delivery(1), delivery(2, "ping", None), delivery(3, "ping", None)], ping=True)
        self.assertEqual(mark, selftest.PASS, detail)
        posts = [p for m, p, _ in fake.calls if m == "POST"]
        self.assertEqual(posts, ["/app/hook/deliveries/2/attempts"])
        _, mark, detail, fix = self.run_step([delivery(2, "ping", None)], ping=True, answer=401)
        self.assertEqual(mark, selftest.FAIL, detail)
        self.assertIn("does not match the route's", detail)
        self.assertEqual(fix, "hermes dk app-webhook --loop widgets --set")

    def test_ping_sends_nothing_when_the_log_holds_no_ping(self):
        # Redelivering a real event would run a gate again: only a ping is ever redelivered.
        fake, mark, detail, _ = self.run_step([delivery(1), delivery(2)], ping=True)
        self.assertEqual(mark, selftest.WARN, detail)
        self.assertIn("no ping to redeliver", detail)
        self.assertFalse([p for m, p, _ in fake.calls if m == "POST"])


class Trace(Base):
    def test_a_guid_is_found_in_the_apps_log_and_reaches_the_apps_route(self):
        self.app([delivery(4), delivery(5)])
        with mock.patch.object(gh, "hooks_read") as hooks:
            payload, event, route = trace.fetch_delivery(LOOP, "guid-5", None)
        hooks.assert_not_called()
        self.assertEqual((payload["number"], event, route), (7, "pull_request",
                                                              "dk-app-acme-reader"))
        self.assertEqual(trace.role_for(LOOP, event, route), "app")
        self.assertEqual(trace.fetch_delivery(LOOP, "4", None)[2], "dk-app-acme-reader")

    def test_another_repositorys_or_an_unknown_delivery_is_refused(self):
        self.app([delivery(8, repo=OTHER_ID)])
        with self.assertRaises(trace.TraceError) as caught:
            trace.fetch_delivery(LOOP, "guid-8", None)
        self.assertIn("is for acme/other, not acme/widgets", str(caught.exception))
        with self.assertRaises(trace.TraceError) as caught:
            trace.fetch_delivery(LOOP, "guid-nope", None)
        self.assertIn("no delivery 'guid-nope' in acme-reader[bot]'s webhook log", str(caught.exception))

    def test_a_payload_without_a_route_goes_through_the_apps_dispatcher(self):
        self.assertEqual(trace.role_for(LOOP, "pull_request"), "app")
        self.assertEqual(trace.role_for(LOOP, "pull_request", "widgets-review"), "reviewer")
        repo_hooks = {k: v for k, v in LOOP.items() if k != "ingress"}
        self.assertEqual(trace.role_for(repo_hooks, "pull_request"), "reviewer")


class RedeliverHint(unittest.TestCase):
    def test_an_app_loops_failed_gate_points_at_the_apps_log(self):
        with mock.patch.object(gate_failures, "_loop_for", return_value=LOOP):
            route = gate_failures._route_of("gate_reviewer", {})
        self.assertEqual(route, "dk-app-acme-reader")
        hint = gate_failures.redeliver_hint({"gate": "gate_reviewer", "route": route,
                                             "action": "opened", "pr": 7})
        self.assertIn("the acme-reader App's settings → Advanced → Recent Deliveries", hint)
        self.assertNotIn("Settings → Webhooks", hint)
        repo_hook = gate_failures.redeliver_hint({"gate": "gate_reviewer",
                                                  "route": "widgets-review"})
        self.assertIn("Settings → Webhooks", repo_hook)


class TraceRun(app_cli.Base):
    """The real trace on a real App loop: the gate runs through the App's dispatcher, so it is
    not dropped as a leftover repo hook's delivery."""

    def setUp(self):
        super().setUp()
        rc, out = self.run_cli(self.app_argv(
            "--ingress", "app-webhook", "--ingress-app", READ_APP, reader=READ_APP,
            tokens=[(tfs.FIX, self.pats["fix"])],
            apps=[(app_cli.BOT_REV, 5416187, self.key("rev")), (READ_APP, 5416115, self.key("read"))]))
        self.assertEqual(rc, 0, out)
        self.addCleanup(lambda: routes.restore_entries({"dk-app-acme-reader": None}))

    def test_a_traced_payload_is_not_dropped_as_a_leftover_hooks(self):
        loop = config.load_id("widgets")
        payload = {"action": "opened", "number": 7, "pull_request": {
            "number": 7, "head": {"sha": "a" * 40}, "base": {"ref": "main"},
            "user": {"login": tfs.FIX}}, "repository": {"full_name": "acme/widgets"},
            "sender": {"login": tfs.FIX}}
        lines = []
        role = trace.role_for(loop, "pull_request")
        trace.run(loop, json.loads(json.dumps(payload)), "pull_request", role, out=lines.append)
        text = "\n".join(lines)
        self.assertIn("gate_app.py", text)
        self.assertNotIn("leftover repo hook", text)


if __name__ == "__main__":
    unittest.main()
