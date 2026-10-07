"""An HTTP error response is closed where it is caught, not by the collector.

``urllib.error.HTTPError`` is also the open response. Dropping it unclosed leaves the socket to
garbage collection, and Python 3.14 warns "Implicitly cleaning up <HTTPError ...>" about it.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import io
import os
import unittest
from unittest import mock
import urllib.error

from diaktoros import config, gh, routes, trusted_fetch


def http_error(code: int = 500) -> urllib.error.HTTPError:
    return urllib.error.HTTPError('http://127.0.0.1/x', code, 'Internal Server Error', {},
                                  io.BytesIO(b'{"message": "boom"}'))


class HttpErrorsAreClosed(unittest.TestCase):
    def setUp(self):
        self.error = http_error()
        patcher = mock.patch('urllib.request.urlopen', side_effect=self.error)
        patcher.start()
        self.addCleanup(patcher.stop)
        # urlopen is the fake here, so nothing leaves the machine; the test guard's network
        # check (tests/_home_guard.py) would refuse the production URLs before reaching it.
        guard = mock.patch.object(config, 'guard_network', side_effect=lambda url: url)
        guard.start()
        self.addCleanup(guard.stop)
        env = mock.patch.dict(os.environ)   # restored after the test, stub or not
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('DIAKTOROS_GH_STUB', None)   # the real transport, not the stub

    def test_gh_request(self):
        with mock.patch.object(gh, 'token', return_value='t'):
            response = gh.request({}, '/user')
        self.assertEqual(response.status, 500)
        self.assertIn('boom', response.error)
        self.assertTrue(self.error.fp is None or self.error.fp.closed)

    def test_route_fire(self):
        with mock.patch.object(routes, 'target', return_value=('http://127.0.0.1/hook', b's')), \
                mock.patch.object(routes, 'log'):
            self.assertFalse(routes.fire('r', 'ping', {}, 'tag'))
        self.assertTrue(self.error.fp is None or self.error.fp.closed)

    def test_trusted_fetch(self):
        with mock.patch.object(gh, 'token', return_value='t'):
            with self.assertRaises(trusted_fetch.FetchDenied):
                trusted_fetch._request({}, '/x', 'login', 10, 'application/json')
        self.assertTrue(self.error.fp is None or self.error.fp.closed)


if __name__ == '__main__':
    unittest.main()
