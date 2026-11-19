import unittest

from axon_controller.axon_protocol import (
    FrameError,
    all_state_query,
    build_frame,
    channel_command,
    manual_channel_command,
    manual_mode_command,
    channel_state_query,
    parse_channel_state,
    parse_frame,
    parse_power_state,
    power_command,
)


class AxonProtocolTests(unittest.TestCase):
    def test_verified_channel_query(self):
        self.assertEqual(channel_state_query(), bytes.fromhex("55 AA 06 00 02 0E 01 16"))
        self.assertEqual(all_state_query(), bytes.fromhex("55 AA 06 00 02 0E 00 15"))

    def test_parse_observed_power_response(self):
        observed = bytes.fromhex("55 AA 07 00 02 01 01 0A")
        self.assertTrue(parse_power_state(parse_frame(observed)))

    def test_parse_observed_channel_response(self):
        observed = bytes.fromhex("55 AA 07 00 0A 11 64 64 64 64 00 00 00 00 00 B1")
        state = parse_channel_state(parse_frame(observed))
        self.assertEqual(state.channels, (100, 100, 100, 100, 0, 0, 0, 0))
        self.assertFalse(state.enabled)

    def test_power_frames_match_app_algorithm(self):
        self.assertEqual(power_command(False), bytes.fromhex("55 AA 06 00 02 01 00 08"))
        self.assertEqual(power_command(True), bytes.fromhex("55 AA 06 00 02 01 01 09"))

    def test_channel_frame_matches_official_app_algorithm(self):
        command = channel_command((100, 80, 60, 40, 0, 0, 0, 0), True)
        self.assertEqual(command, bytes.fromhex("55 AA 06 00 0A 11 64 3C 28 50 00 00 00 00 01 39"))

    def test_channel_command_is_strict(self):
        for invalid in ((1,) * 7, (1,) * 9, (101,) + (0,) * 7, (True,) + (0,) * 7):
            with self.assertRaises(ValueError):
                channel_command(invalid)

    def test_persistent_manual_profile_matches_official_app(self):
        channels = (30, 20, 25, 20, 0, 0, 0, 0)
        self.assertEqual(
            manual_channel_command(channels),
            bytes.fromhex("55 AA 06 00 0B 03 00 1E 19 14 14 00 00 00 00 00 72"),
        )
        self.assertEqual(manual_mode_command(), bytes.fromhex("55 AA 06 00 02 02 00 09"))

    def test_rejects_corrupt_frames(self):
        good = build_frame(1, b"\x01")
        for corrupt in (good[:-1], bytes((0,)) + good[1:], good[:-1] + bytes((good[-1] ^ 1,))):
            with self.assertRaises(FrameError):
                parse_frame(corrupt)

    def test_rejects_invalid_channel_payload(self):
        with self.assertRaises(FrameError):
            parse_channel_state(parse_frame(build_frame(0x11, bytes((101,)) + bytes(8))))

    def test_translates_observed_device_wgbr_order_to_canonical_wrgb(self):
        payload = bytes((10, 20, 30, 40, 0, 0, 0, 0, 1))
        state = parse_channel_state(parse_frame(build_frame(0x11, payload)))
        self.assertEqual(state.channels, (10, 40, 20, 30, 0, 0, 0, 0))
        self.assertTrue(state.enabled)


if __name__ == "__main__":
    unittest.main()
