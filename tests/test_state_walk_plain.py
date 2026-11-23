"""#2640 review (F2): how the state walk's oracle (``tests/_state_walk_oracle.py``'s ``plain``)
reads numbers. Only a float within ``CLOCK_S`` of now is a clock and is masked; every integer,
however large, and every other float stays, so two results that differ in an ordinary number are
never read as equal (#2638). The boundary cases sit a minute either side of ``CLOCK_S``.
"""
import _home_guard  # noqa: F401  first import: temp HOME/HERMES_HOME (tests/_home_guard.py)
import time
import unittest

import _state_walk_oracle as oracle


class PlainNumbers(unittest.TestCase):
    def test_a_clock_float_is_masked(self):
        now = time.time()
        for value in (now, now - 3600.25, now + 0.5, now - (oracle.CLOCK_S - 60)):
            with self.subTest(value=value):
                self.assertEqual(oracle.plain(value), "#")

    def test_a_float_far_from_now_stays(self):
        far = time.time() - (oracle.CLOCK_S + 60)
        for value in (2.5, 3.5, -0.75, 1e8 + 0.5, far, 1e12 + 0.5):
            with self.subTest(value=value):
                self.assertEqual(oracle.plain(value), value)

    def test_two_fractional_results_stay_distinct(self):
        self.assertNotEqual(oracle.plain({"ratio": 2.5}), oracle.plain({"ratio": 3.5}))
        self.assertNotEqual(oracle.plain([1e9 + 0.25]), oracle.plain([1e9 + 0.75]))

    def test_every_integer_stays(self):
        for value in (0, 7, 100000001, 100000002, int(time.time()), 10**15):
            with self.subTest(value=value):
                self.assertEqual(oracle.plain(value), value)

    def test_iso_times_and_named_clocks_stay_masked(self):
        self.assertEqual(oracle.plain("at 2026-11-23T19:20:00Z"), "at #")
        key = next(iter(oracle.FRESH))
        self.assertEqual(oracle.plain({key: 5}), {key: "#"})


if __name__ == "__main__":
    unittest.main()
