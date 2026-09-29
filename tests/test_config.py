"""
Config tests - the committed config.json is what runs.

Regression: the live bot polled every 60 s for ten days because a copy seeded
onto the volume by the SMA version overrode the committed 20 s.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import CONFIG_PATH, differences, load_config  # noqa: E402


def committed():
    with open(CONFIG_PATH) as fh:
        return json.load(fh)


class ConfigTests(unittest.TestCase):
    def test_committed_config_wins_over_a_volume_copy_which_is_retired(self):
        d = tempfile.mkdtemp()
        old = os.path.join(d, "config.json")
        with open(old, "w") as fh:
            json.dump({"symbol": "BTC/USD", "poll_seconds": 60, "fast_sma": 10}, fh)

        self.assertEqual(load_config(d), committed())
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(old + ".retired"))    # renamed, never deleted
        self.assertEqual(load_config(d), committed())         # and quiet from then on

    def test_no_volume_copy_is_fine(self):
        self.assertEqual(load_config(tempfile.mkdtemp()), committed())

    def test_differences_are_dotted_and_include_both_sides(self):
        diff = differences({"a": 1, "g": {"x": 1, "y": 2}}, {"a": 1, "g": {"x": 5}, "old": 3})
        self.assertEqual(diff, {
            "g.x": {"committed": 1, "volume": 5},
            "g.y": {"committed": 2, "volume": "<absent>"},
            "old": {"committed": "<absent>", "volume": 3},
        })

    def test_engine_keys_are_present(self):
        cfg = committed()
        for key in ("symbol", "poll_seconds", "fill_timeout_seconds", "cancel_timeout_seconds",
                    "grid", "risk", "exit", "bars"):
            self.assertIn(key, cfg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
