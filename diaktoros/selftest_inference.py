"""``selftest``'s step 3, inference (split from ``selftest`` at its size limit): each seat's model
as the worker resolves it (``check_seats``), then one tiny completion per distinct seat
resolution (``check_model``) in the seat's wire format, through the host inference capability.
It reaches the core through ``selftest`` (``from . import selftest``); callers use
``selftest_inference.<name>``.
"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile

from . import selftest


def check_seats(report: selftest.Report, loop: dict, settings: dict | None, resolver=None) -> dict:
    """Step 1 (cont.): each seat's model exactly as the worker resolves it. Returns seat → it."""
    from . import seat_model
    step = "runtime"
    resolved: dict = {}
    for seat in seat_model.seats_for(loop):
        name = f"seat:{seat}"
        if settings is None:
            report.add(step, name, selftest.SKIP, "needs a usable runtime config")
            continue
        try:
            inference = seat_model.resolve_seat(loop, seat, settings, resolver=resolver)
        except seat_model.SeatModelError as exc:
            report.add(step, name, selftest.FAIL, f"{exc}; the {seat} turn would be held",
                       f"`hermes dk models --seat {seat} --loop {loop['id']}` lists what "
                       "its profile's provider offers")
            continue
        report.redact.add(inference.key)
        resolved[seat] = inference
        if inference.origin == "legacy":
            report.add(step, name, selftest.WARN, f"{inference.describe()} — {inference.warning}",
                       f"make profile {inference.profile or '<name>'} resolve, or add an explicit "
                       f"seats.{seat} override")
        else:
            report.add(step, name, selftest.PASS, inference.describe())
    return resolved


def check_model(report: selftest.Report, seats: dict | None) -> None:
    """Step 3: one tiny completion per distinct seat resolution (keys never leave the host)."""
    if not seats:
        report.add("inference", "model:completion", selftest.SKIP,
                   "needs at least one resolved seat model (step 1)")
        return
    groups: dict = {}
    for seat, inference in seats.items():
        groups.setdefault(inference.identity(), []).append((seat, inference))
    for members in groups.values():
        names = "+".join(seat for seat, _ in members)
        _one_completion(report, names, members[0][1])


_MODEL_PROMPT = "Reply with the single word OK."


def probe_body(api_mode: str, client_identity: str = "") -> bytes:
    """The smallest request of each wire format (the proxy forces model and output cap)."""
    if api_mode == "codex_responses":
        body = {"instructions": "You are a connectivity probe.", "store": False, "stream": True,
                "input": [{"role": "user",
                           "content": [{"type": "input_text", "text": _MODEL_PROMPT}]}]}
    elif api_mode == "anthropic_messages":
        body = {"max_tokens": 16, "messages": [{"role": "user", "content": _MODEL_PROMPT}]}
        if client_identity == "claude_code":
            # What Hermes sends first on a subscription token (see its anthropic adapter).
            body["system"] = [{"type": "text",
                               "text": "You are Claude Code, Anthropic's official CLI for Claude."}]
    else:
        body = {"messages": [{"role": "user", "content": _MODEL_PROMPT}], "max_tokens": 16}
    return json.dumps(body).encode()


def empty_reply_reason(api_mode: str) -> str:
    """Why an empty probe reply may be, naming only the output cap the probe really sent."""
    if api_mode == "codex_responses":
        # probe_body names no cap here: the proxy supplies its own default (or none at all).
        return ("the model may have spent its output budget on reasoning before any visible text "
                "(the probe sets no small output limit in this mode)")
    return ("the probe's 16-token limit was probably used up by the model's reasoning "
            "before any visible text")


def _reasoning_tokens(usage) -> int:
    """Reasoning tokens a ``usage`` object reports, in the shapes providers use."""
    if not isinstance(usage, dict):
        return 0
    found = [usage.get("reasoning_tokens")]
    for key in ("completion_tokens_details", "output_tokens_details"):
        details = usage.get(key)
        found.append(details.get("reasoning_tokens") if isinstance(details, dict) else None)
    return sum(n for n in found if isinstance(n, int) and not isinstance(n, bool))


def _message_reasoning(message: dict) -> bool:
    """Whether a chat_completions message (or stream delta) carries reasoning text."""
    return any(isinstance(message.get(key), str) and message[key].strip()
               for key in ("reasoning_content", "reasoning"))


def probe_reply(api_mode: str, content_type: str, data: bytes) -> tuple[str, bool] | None:
    """``(visible text, whether the reply also carries reasoning)``, or ``None`` if not an answer."""
    try:
        # Judged by the body as well as the label: the proxy relays an event stream an upstream
        # labelled JSON as a stream now, but a probe should not depend on that.
        if (content_type.startswith("text/event-stream")
                or data.lstrip().startswith((b"event:", b"data:", b":"))):
            text, done, reasoned = [], False, False
            for line in data.decode("utf-8", "replace").splitlines():
                if not line.startswith("data:") or line[5:].strip() in ("", "[DONE]"):
                    continue
                event = json.loads(line[5:])
                kind = event.get("type", "")
                usage = event.get("usage") or (event.get("response") or {}).get("usage")
                reasoned = reasoned or _reasoning_tokens(usage) > 0
                if kind in ("response.output_text.delta",):
                    text.append(str(event.get("delta") or ""))
                elif kind.startswith("response.reasoning") and kind.endswith(".delta"):
                    reasoned = reasoned or bool(str(event.get("delta") or "").strip())
                elif kind == "content_block_start":
                    reasoned = reasoned or (event.get("content_block") or {}).get("type") in (
                        "thinking", "redacted_thinking")
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    text.append(str(delta.get("text") or ""))
                    reasoned = reasoned or delta.get("type") == "thinking_delta"
                elif "choices" in event:
                    delta = (event["choices"] or [{}])[0].get("delta") or {}
                    text.append(str(delta.get("content") or ""))
                    reasoned = reasoned or _message_reasoning(delta)
                done = done or kind in ("response.completed", "message_stop") or "choices" in event
            return ("".join(text), reasoned) if done else None
        answer = json.loads(data)
        reasoned = _reasoning_tokens(answer.get("usage")) > 0
        if api_mode == "anthropic_messages":
            blocks = answer["content"]
            reasoned = reasoned or any(b.get("type") in ("thinking", "redacted_thinking")
                                       for b in blocks)
            return "".join(b.get("text", "") for b in blocks if b.get("type") == "text"), reasoned
        if api_mode == "codex_responses":
            reasoned = reasoned or any(item.get("type") == "reasoning" for item in answer["output"])
            return ("".join(c.get("text", "") for item in answer["output"]
                            if item.get("type") == "message" for c in item.get("content", [])),
                    reasoned)
        message = answer["choices"][0]["message"]
        return message.get("content") or "", reasoned or _message_reasoning(message)
    except Exception:
        return None


def probe_answer(api_mode: str, content_type: str, data: bytes) -> str | None:
    """The reply text of a probe answer in ``api_mode``'s shape, or ``None`` if it is not one."""
    reply = probe_reply(api_mode, content_type, data)
    return None if reply is None else reply[0]


def _one_completion(report: selftest.Report, seats: str, inference) -> None:
    from . import inference_proxy
    step, name = "inference", "model:completion"
    report.redact.add(inference.key)
    host = inference.host
    mode = getattr(inference, "api_mode", "chat_completions")
    identity = getattr(inference, "client_identity", "")
    body = probe_body(mode, identity)
    try:
        with tempfile.TemporaryDirectory(prefix="rl-st-") as sockets:
            with inference_proxy.InferenceCapability(
                    Path(sockets) / "i", inference.upstream, model=inference.proxy_model,
                    quota=1, api_mode=mode, credential=inference.credential_provider()) as capability:
                conn = inference_proxy._UnixHTTP(str(capability.socket_path))
                try:
                    conn.request("POST", capability.contract.local_path, body=body,
                                 headers={"Content-Type": "application/json"})
                    response = conn.getresponse()
                    status, data = response.status, response.read(inference_proxy.MAX_RESPONSE)
                    content_type = response.getheader("Content-Type", "")
                finally:
                    conn.close()
    except (OSError, http.client.HTTPException, ValueError) as exc:
        report.add(step, name, selftest.FAIL,
                   f"{seats}: the host capability did not answer ({type(exc).__name__})",
                   "check that the upstream host is reachable from this machine")
        return
    where = (f"profile {inference.profile}'s credential" if inference.origin == "profile"
             else "the override's key file")
    oauth = getattr(inference, "auth", "") == "oauth"
    process_backend = getattr(inference, "auth", "") == "external_process"
    fixes = {401: (f"{where} was rejected even after a host refresh — log in again with "
                   f"`hermes -p {inference.profile} auth`" if oauth
                   else f"{where} was rejected — replace it"),
             403: f"{where} may not use model {inference.model!r}",
             404: f"the provider does not know model {inference.model!r} at this URL — "
                  "`hermes dk models` lists what it offers",
             429: ("the subscription's rate limit is spent — it is shared with your own use of "
                   "this account" if oauth else
                   "the provider rate-limited or the account is out of credit"),
             502: f"the proxy could not complete an HTTPS call to {host} (DNS, TLS, network, or a "
                  "non-JSON answer) — try `curl -sS https://" + str(host) + "` from this host"}
    if process_backend:
        fixes[502] = (f"the host DirectSDK process failed; check the installed provider, native "
                      f"Claude CLI and profile {inference.profile}'s native login/config directory; "
                      "the host launch lockdown refuses missing/unsafe flags or an unreviewed MCP inventory server")
    what = f"{seats}: {inference.describe()}"
    if status != 200:
        report.add(step, name, selftest.FAIL, f"HTTP {status} for {what}",
                   fixes.get(status, "check the upstream URL, the model name and the key"))
        return
    reply = probe_reply(mode, content_type, data)
    if reply is None:
        report.add(step, name, selftest.FAIL,
                   f"{seats}: HTTP 200 via {host}, but not a {mode} answer",
                   f"upstream must speak {mode} at {inference.upstream}")
        return
    answer, reasoned = reply
    if not answer.strip() and reasoned:
        # A reasoning model can spend the probe's small cap thinking; it did answer.
        report.add(step, name, selftest.PASS,
                   f"HTTP 200 — {what}, reply was reasoning only (no visible text within "
                   "the probe's output limit)")
        return
    if not answer.strip():
        report.add(step, name, selftest.WARN,
                   f"{what}: HTTP 200 but the reply is empty — {empty_reply_reason(mode)}",
                   "the endpoint answers, but this probe cannot show the model produces text; "
                   "try a non-reasoning model or check the provider's reasoning settings")
        return
    report.add(step, name, selftest.PASS, f"HTTP 200 — {what}, reply {answer.strip()[:40]!r}")
