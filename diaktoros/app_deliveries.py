"""The ingress App's own delivery log (#1239, part 2): for a loop on ``ingress app-webhook``, what a
repo hook's ``/hooks/{id}/deliveries`` is for a repo-hooks loop.

One App's webhook delivers for every repository the App is installed on, so its log
(``/app/hook/deliveries``, read with the App's JWT, never an installation token) mixes them: each
item carries its ``repository_id``, and this loop's are the ones with its repository's id. The log
is newest first, paged by cursor (GitHub's ``Link`` header), and GitHub keeps recent deliveries
only.

Three readers: ``trace --delivery`` finds one delivery's payload (``find``); doctor and selftest
judge the gateway's answer to this repository's latest delivery (``latest``), the only evidence
that the App's secret is the route's, as ``check_deliveries`` judges a repo hook's; and
``selftest --ping`` redelivers the App's newest ping (``ping``). An App's webhook has no ping
endpoint, and a ping is the one delivery the gateway answers without waking a gate.
"""
from __future__ import annotations

import re
import time

from . import gh

MAX_PAGES = 3            # 300 deliveries, as ``trace`` searches each repo hook
PAGE = 100
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def _call(loop: dict, path: str, method: str = "GET") -> gh.Response:
    from . import github_app
    return github_app._as_app(loop, str(loop.get("ingress_app") or ""), path, method)


def _next(headers) -> str:
    """The next page's path from GitHub's ``Link`` header; "" on the last page, or for a link
    that leaves the API (the JWT goes to GitHub's API only)."""
    found = _NEXT.search(str((headers or {}).get("link") or ""))
    if not found or not found.group(1).startswith(gh.API + "/"):
        return ""
    return found.group(1)[len(gh.API):]


def pages(loop: dict, max_pages: int = MAX_PAGES):
    """The App's log a page at a time, newest first: ``(items, "")`` per page, or one final
    ``(None, why)`` when a page cannot be read."""
    path = f"/app/hook/deliveries?per_page={PAGE}"
    for _ in range(max_pages):
        found = _call(loop, path)
        items = found.data
        if found.error or not isinstance(items, list) or not all(
                isinstance(item, dict) for item in items):
            yield None, found.error or "no delivery list returned"
            return
        yield items, ""
        path = _next(found.headers)
        if not items or not path:
            return


def repo_id(loop: dict) -> tuple[int | None, str]:
    """The loop's repository's numeric id, which the App's log names deliveries by."""
    data, error = gh.fetch(loop, f"/repos/{loop['repo']}")
    found = data.get("id") if isinstance(data, dict) else None
    if error or type(found) is not int:
        return None, f"the repository's id could not be read ({error or 'no id returned'})"
    return found, ""


def latest(loop: dict) -> tuple[dict | None, str]:
    """This repository's newest delivery in the App's log: ``(delivery, "")``, ``(None, "")``
    when the log holds none for it, or ``(None, why)`` when it cannot be read."""
    rid, error = repo_id(loop)
    if error:
        return None, error
    for items, error in pages(loop):
        if error:
            return None, f"the App's deliveries could not be read ({error})"
        ours = [item for item in items if item.get("repository_id") == rid
                and isinstance(item.get("delivered_at"), str)]
        if ours:
            return max(ours, key=lambda item: item["delivered_at"]), ""
    return None, ""


def find(loop: dict, delivery: str) -> tuple[dict | None, str, str]:
    """``(payload, event, "")`` of one delivery in the App's log, by GitHub's numeric id or its
    ``X-GitHub-Delivery`` GUID, or ``(None, "", why)``. Another repository's delivery is refused:
    the App's log holds every repository it serves."""
    wanted = delivery.strip()
    number = wanted if wanted.isdigit() else None
    if number is None:
        for items, error in pages(loop):
            if error:
                return None, "", f"the App's deliveries could not be read ({error})"
            match = next((item for item in items if str(item.get("guid") or "") == wanted), None)
            if match:
                number = str(match.get("id"))
                break
    if number is None:
        return None, "", (f"no delivery {wanted!r} in {loop.get('ingress_app')}'s webhook log "
                          f"(the last {MAX_PAGES * PAGE} were searched)")
    found = _call(loop, f"/app/hook/deliveries/{number}")
    request = found.data.get("request") if isinstance(found.data, dict) else None
    payload = request.get("payload") if isinstance(request, dict) else None
    if found.error or not isinstance(payload, dict):
        return None, "", f"delivery {number} could not be read ({found.error or 'no payload'})"
    full = str((payload.get("repository") or {}).get("full_name") or "")
    if full.lower() != str(loop["repo"]).lower():
        return None, "", (f"delivery {number} is for {full or 'no repository'}, not "
                          f"{loop['repo']}: {loop.get('ingress_app')}'s webhook serves every "
                          "repository the App is installed on")
    return payload, str(found.data.get("event") or ""), ""


def ping(loop: dict, *, wait: float | None = None, sleep=time.sleep,
         clock=time.monotonic) -> tuple[str, str]:
    """Redeliver the App's newest ping and wait (bounded) for the gateway's answer: ``(status,
    line)`` with ``hook_ping``'s statuses. ``silent`` when the log holds no ping to redeliver."""
    from . import hook_ping
    wait = hook_ping.PING_WAIT if wait is None else wait
    app = loop.get("ingress_app")
    known, newest = None, None
    for items, error in pages(loop):
        if error:
            return hook_ping.ERROR, (f"⚠️ {app}'s webhook: its deliveries cannot be read ({error}), "
                                     "so a ping could not be observed — not sent")
        known = {item.get("id") for item in items} if known is None else known
        newest = next((item for item in items if item.get("event") == "ping"), None)
        if newest:
            break
    if newest is None:
        return hook_ping.SILENT, (f"⚠️ {app}'s webhook log holds no ping to redeliver (GitHub keeps "
                                  "recent deliveries only, and an App's webhook has no ping "
                                  "endpoint) — nothing proven yet; the next real delivery is the "
                                  f"evidence: `hermes dk doctor --loop {loop['id']}`")
    sent = _call(loop, f"/app/hook/deliveries/{newest['id']}/attempts", "POST")
    if sent.error:
        return hook_ping.ERROR, f"❌ {app}'s webhook: ping not redelivered ({sent.error})"
    deadline = clock() + wait
    while True:
        items = next(pages(loop, 1))[0] or []
        fresh = [item for item in items if item.get("id") not in known
                 and item.get("event") == "ping" and isinstance(item.get("status_code"), int)]
        if fresh:
            code = max(fresh, key=lambda item: str(item.get("delivered_at") or ""))["status_code"]
            if 200 <= code < 300:
                return hook_ping.OK, (f"✅ {app}'s webhook: ping answered HTTP {code} — "
                                      "signature accepted")
            return hook_ping.REJECTED, f"❌ {app}'s webhook: ping answered HTTP {code} — {rejected(code)}"
        if clock() >= deadline:
            return hook_ping.SILENT, (f"⚠️ {app}'s webhook: no redelivered ping seen within "
                                      f"{wait:g}s — nothing proven yet; look again with "
                                      f"`hermes dk doctor --loop {loop['id']}`")
        sleep(hook_ping.POLL)


def rejected(code: int) -> str:
    """What the gateway's non-2xx answer to an App delivery means (``hook_checks.REJECTED``)."""
    if code == 401:
        return "signature rejected — the App's webhook secret does not match the route's"
    if code == 403:
        return "refused — the route is disabled or holds no secret"
    return "the gateway errored" if code >= 500 else "the gateway did not accept it"


def fix(loop: dict) -> str:
    """The fix for a rejected App delivery: write the route's secret onto the App's webhook."""
    return f"hermes dk app-webhook --loop {loop['id']} --set"
