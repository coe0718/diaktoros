"""Two loopback http.servers standing in for the GitHub API and codeload, so the tarball
redirect path runs through REAL urllib (an HTTPError(302), not a mocked 302 response).

Handlers record every request they see: (path, headers) — the tests assert the API hop
carried ``Authorization`` and the codeload hop did NOT.
"""
import http.server
import threading


class _Recorder:
    def __init__(self):
        self.requests = []          # [(path, dict(headers))]


def _serve(handler_cls, host="127.0.0.1"):
    server = http.server.ThreadingHTTPServer((host, 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class ApiServer:
    """The API side: answers 302 to a target URL, or 200 with ``direct`` body."""

    def __init__(self, location=None, direct=None, host="127.0.0.1"):
        self.recorder = _Recorder()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
                pass

            def do_GET(self):
                outer.recorder.requests.append((self.path, dict(self.headers)))
                if outer.location is not None:
                    self.send_response(302)
                    self.send_header("Location", outer.location)
                    self.end_headers()
                    return
                body = outer.direct or b""
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.location = location
        self.direct = direct
        self.server, self.thread = _serve(Handler, host)

    @property
    def port(self):
        return self.server.server_port

    @property
    def base(self):
        return f"http://{self.server.server_address[0]}:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class CodeloadServer:
    """The codeload side: answers 200 with ``body``, or 302 to ``redirect``."""

    def __init__(self, body=None, redirect=None, host="127.0.0.1"):
        self.recorder = _Recorder()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
                pass

            def do_GET(self):
                outer.recorder.requests.append((self.path, dict(self.headers)))
                if outer.redirect is not None:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect)
                    self.end_headers()
                    return
                data = outer.body or b""
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.body = body
        self.redirect = redirect
        self.server, self.thread = _serve(Handler, host)

    @property
    def port(self):
        return self.server.server_port

    @property
    def base(self):
        return f"http://{self.server.server_address[0]}:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
