"""routes.serves_route_url ignores userinfo and the scheme's default port (#144)."""
from __future__ import annotations

import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from review_loop import routes  # noqa: E402

ROUTE = "https://gw.example/webhooks/widgets-review"


class ServesRouteUrl(unittest.TestCase):
    def test_equivalent_spellings_serve(self):
        for hook in ("https://gw.example:443/webhooks/widgets-review",
                     "https://user:pw@gw.example/webhooks/widgets-review",
                     "https://user@GW.example:443/webhooks/widgets-review?x=1",
                     ROUTE):
            self.assertTrue(routes.serves_route_url(hook, ROUTE), hook)
        self.assertTrue(routes.serves_route_url("http://gw:80/webhooks/a", "http://gw/webhooks/a"))
        self.assertTrue(routes.serves_route_url(ROUTE, "https://gw.example:443/webhooks/widgets-review"))

    def test_different_targets_do_not_serve(self):
        for hook in ("https://gw.example:8443/webhooks/widgets-review",
                     "http://gw.example:443/webhooks/widgets-review",
                     "https://gw.example:80/webhooks/widgets-review",
                     "https://other.example/webhooks/widgets-review",
                     "https://gw.example/webhooks/widgets-review/"):
            self.assertFalse(routes.serves_route_url(hook, ROUTE), hook)


if __name__ == "__main__":
    unittest.main()
