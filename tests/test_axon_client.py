import unittest

from axon_controller.axon_client import _command


class AxonClientTests(unittest.TestCase):
    def test_allowlist(self):
        self.assertEqual(_command(["status"]), {"operation": "status"})
        self.assertEqual(_command(["live-status"]), {"operation": "live_status"})
        self.assertEqual(_command(["set-power", "on"]), {"operation": "set_power", "enabled": True})
        self.assertEqual(
            _command(["set-channels", "10,20,30,40,0,0,0,0"]),
            {"operation": "set_channels", "channels": [10, 20, 30, 40, 0, 0, 0, 0]},
        )
        self.assertEqual(
            _command(["override", "3600", "10,20,30,40,0,0,0,0"]),
            {"operation": "override", "duration_seconds": 3600, "channels": [10, 20, 30, 40, 0, 0, 0, 0]},
        )
        self.assertEqual(_command(["resume-solar"]), {"operation": "resume_schedule"})
        self.assertEqual(
            _command(["set-location", "90210", "34.0901", "-118.4065", "America/Los_Angeles"]),
            {
                "operation": "set_location", "postal_code": "90210",
                "latitude": 34.0901, "longitude": -118.4065, "timezone": "America/Los_Angeles",
            },
        )
        with self.assertRaises(ValueError):
            _command(["set-power", "maybe"])
        with self.assertRaises(ValueError):
            _command(["shell"])
