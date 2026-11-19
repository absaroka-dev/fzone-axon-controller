import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from axon_controller.axon_schedule import LightingSchedule, SchedulePoint, ScheduleRunner, SolarLightingSchedule


class FakeLight:
    def __init__(self):
        self.calls = []
        self.channels = (0,) * 8
        self.enabled = False

    async def status(self):
        from axon_controller.axon_protocol import ChannelState
        return ChannelState(self.channels, self.enabled)

    async def set_channels(self, channels, enabled):
        self.calls.append((channels, enabled))
        self.channels = channels
        self.enabled = enabled


class AxonScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.zone = ZoneInfo("America/Chicago")
        self.schedule = LightingSchedule(
            True,
            self.zone,
            (
                SchedulePoint(7 * 60, (0,) * 8),
                SchedulePoint(8 * 60, (40, 20, 10, 30, 0, 0, 0, 0)),
                SchedulePoint(17 * 60, (40, 20, 10, 30, 0, 0, 0, 0)),
                SchedulePoint(18 * 60, (0,) * 8),
            ),
        )

    @staticmethod
    def solar(**updates):
        payload = {
            "mode": "solar", "enabled": True, "timezone": "America/Chicago",
            "latitude": 48.4292, "longitude": -101.3208,
            "lighting": {
                "meaningful_light_hours": 6, "maximum_meaningful_light_hours": 7,
                "ramp_minutes": 60, "maximum_channel_percent": 35,
                "maximum_relative_output_hours": 1.35,
                "peak_wrgb": {"white": 30, "red": 20, "green": 25, "blue": 20},
                "twilight_wrgb": {"white": 1, "red": 0, "green": 0, "blue": 1},
            },
            "seasonal": {"anchor": "solar_noon", "outer_twilight": "civil"},
        }
        payload.update(updates)
        return SolarLightingSchedule.from_dict(payload)

    def test_interpolates_sunrise_and_sunset(self):
        morning = datetime(2026, 10, 30, 7, 30, tzinfo=self.zone)
        evening = datetime(2026, 10, 30, 17, 30, tzinfo=self.zone)
        self.assertEqual(self.schedule.channels_at(morning), (20, 10, 5, 15, 0, 0, 0, 0))
        self.assertEqual(self.schedule.channels_at(evening), (20, 10, 5, 15, 0, 0, 0, 0))

    def test_stays_off_across_midnight(self):
        self.assertEqual(self.schedule.channels_at(datetime(2026, 10, 30, 2, 0, tzinfo=self.zone)), (0,) * 8)

    async def test_runner_sends_only_changes(self):
        light = FakeLight()
        runner = ScheduleRunner(self.schedule, light)
        moment = datetime(2026, 10, 30, 12, 0, tzinfo=self.zone)
        await runner.apply(moment)
        await runner.apply(moment)
        self.assertEqual(light.calls, [((40, 20, 10, 30, 0, 0, 0, 0), True)])

    async def test_runner_repairs_external_drift(self):
        light = FakeLight()
        runner = ScheduleRunner(self.schedule, light)
        moment = datetime(2026, 10, 30, 12, 0, tzinfo=self.zone)
        await runner.apply(moment)
        light.channels = (1,) * 8
        await runner.apply(moment)
        self.assertEqual(len(light.calls), 2)

    async def test_timed_override_and_resume(self):
        light = FakeLight()
        runner = ScheduleRunner(self.schedule, light)
        await runner.set_override((5, 4, 3, 2, 0, 0, 0, 0), 900)
        self.assertTrue(runner.status()["override_active"])
        self.assertEqual(light.calls[-1][0], (5, 4, 3, 2, 0, 0, 0, 0))
        await runner.resume()
        self.assertFalse(runner.status()["override_active"])
        self.assertEqual(light.calls[-1][0], self.schedule.channels_at())

    async def test_override_duration_is_bounded(self):
        runner = ScheduleRunner(self.schedule, FakeLight())
        for duration in (59, 14401):
            with self.assertRaises(ValueError):
                await runner.set_override((1,) * 8, duration)

    async def test_solar_manual_override_is_intensity_and_exposure_bounded(self):
        runner = ScheduleRunner(self.solar(), FakeLight())
        with self.assertRaisesRegex(ValueError, "maximum_channel_percent"):
            await runner.set_override((36, 0, 0, 0, 0, 0, 0, 0), 900)
        with self.assertRaisesRegex(ValueError, "maximum_relative_output_hours"):
            await runner.set_override((35, 35, 35, 35, 0, 0, 0, 0), 14400)

    def test_loader_defaults_disabled_unless_explicitly_true(self):
        payload = {
            "timezone": "America/Chicago",
            "points": [
                {"time": "07:00", "channels": [0] * 8},
                {"time": "08:00", "channels": [10] * 8},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(LightingSchedule.load(str(path)).enabled)

    def test_rejects_duplicate_times(self):
        payload = {
            "enabled": True,
            "points": [
                {"time": "07:00", "channels": [0] * 8},
                {"time": "07:00", "channels": [10] * 8},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                LightingSchedule.load(str(path))

    def test_minot_solar_profile_tracks_events_and_limits_peak(self):
        solar = SolarLightingSchedule.from_dict({
            "mode": "solar",
            "enabled": True,
            "timezone": "America/Chicago",
            "latitude": 48.4292,
            "longitude": -101.3208,
            "productive_hours": 6,
            "ramp_minutes": 60,
            "peak_channels": [30, 20, 25, 20, 0, 0, 0, 0],
            "twilight_channels": [1, 0, 0, 1, 0, 0, 0, 0],
        })
        events = solar.events_for(datetime(2026, 10, 30).date())
        self.assertEqual(events.solar_noon.tzinfo, self.zone)
        self.assertLess(events.civil_dawn, events.sunrise)
        self.assertLess(events.sunrise, events.solar_noon)
        self.assertLess(events.solar_noon, events.sunset)
        self.assertLess(events.sunset, events.civil_dusk)
        self.assertEqual(solar.channels_at(events.civil_dawn + timedelta(minutes=5)), (1, 0, 0, 1, 0, 0, 0, 0))
        self.assertEqual(solar.channels_at(events.solar_noon), (30, 20, 25, 20, 0, 0, 0, 0))
        self.assertEqual(solar.channels_at(events.civil_dusk + timedelta(minutes=5)), (0,) * 8)

    def test_loads_solar_configuration(self):
        path = Path(__file__).parents[1] / "config" / "solar-schedule.example.json"
        schedule = LightingSchedule.load(str(path))
        self.assertIsInstance(schedule, SolarLightingSchedule)
        self.assertFalse(schedule.enabled)

    def test_summer_and_winter_keep_same_bounded_productive_window(self):
        schedule = self.solar()
        for day in (date(2026, 6, 21), date(2026, 12, 21)):
            start, end = schedule.productive_window(schedule.events_for(day))
            self.assertEqual((end - start).total_seconds(), 6 * 3600)
            self.assertLessEqual(schedule.relative_output_hours(day), schedule.max_relative_output_hours)
        summer = schedule.events_for(date(2026, 6, 21))
        winter = schedule.events_for(date(2026, 12, 21))
        self.assertGreater(summer.civil_dusk - summer.civil_dawn, winter.civil_dusk - winter.civil_dawn)

    def test_productive_window_can_shift_later_without_increasing_duration(self):
        base = self.solar()
        shifted_data = {
            "mode": "solar", "enabled": True, "timezone": "America/Chicago",
            "latitude": 48.4292, "longitude": -101.3208,
            "productive_hours": 6, "ramp_minutes": 60,
            "peak_channels": [30, 20, 25, 20, 0, 0, 0, 0],
            "twilight_channels": [1, 0, 0, 1, 0, 0, 0, 0],
            "seasonal": {"anchor": "solar_noon", "outer_twilight": "civil", "solar_noon_offset_minutes": 30},
        }
        shifted = SolarLightingSchedule.from_dict(shifted_data)
        day = date(2026, 11, 7)
        base_start, base_end = base.productive_window(base.events_for(day))
        shifted_start, shifted_end = shifted.productive_window(shifted.events_for(day))
        self.assertEqual(shifted_start - base_start, timedelta(minutes=30))
        self.assertEqual(shifted_end - base_end, timedelta(minutes=30))
        self.assertEqual(shifted_end - shifted_start, timedelta(hours=6))

    def test_dst_changes_offset_not_six_hour_duration(self):
        schedule = self.solar()
        before = schedule.events_for(date(2026, 3, 7))
        after = schedule.events_for(date(2026, 3, 9))
        self.assertNotEqual(before.solar_noon.utcoffset(), after.solar_noon.utcoffset())
        for events in (before, after):
            start, end = schedule.productive_window(events)
            self.assertEqual(end - start, timedelta(hours=6))

    def test_solar_schedule_is_dark_across_midnight_and_recovers_current_setpoint(self):
        schedule = self.solar()
        day = date(2026, 6, 21)
        self.assertEqual(schedule.channels_at(datetime.combine(day, datetime.min.time(), self.zone)), (0,) * 8)
        self.assertEqual(schedule.channels_at(schedule.events_for(day).solar_noon), schedule.peak_channels)

    def test_acclimation_is_restart_stable_and_gradual(self):
        schedule = self.solar(acclimation={
            "enabled": True, "start_date": "2026-10-30", "duration_days": 28, "start_percent": 60,
        })
        self.assertEqual(schedule.acclimation.factor_at(date(2026, 10, 30)), 0.6)
        self.assertAlmostEqual(schedule.acclimation.factor_at(date(2026, 11, 13)), 0.8)
        self.assertEqual(schedule.acclimation.factor_at(date(2026, 11, 27)), 1.0)
        self.assertEqual(schedule.effective_peak(date(2026, 10, 30))[:4], (18, 12, 15, 12))

    def test_rejects_intensity_and_relative_exposure_over_limits(self):
        with self.assertRaisesRegex(ValueError, "maximum_channel_percent"):
            self.solar(lighting={
                "meaningful_light_hours": 6, "maximum_meaningful_light_hours": 7,
                "ramp_minutes": 60, "maximum_channel_percent": 20,
                "maximum_relative_output_hours": 1.35,
                "peak_wrgb": {"white": 30, "red": 20, "green": 20, "blue": 20},
                "twilight_wrgb": {"white": 1, "red": 0, "green": 0, "blue": 1},
            })
        with self.assertRaisesRegex(ValueError, "relative_output"):
            self.solar(lighting={
                "meaningful_light_hours": 6, "maximum_meaningful_light_hours": 7,
                "ramp_minutes": 60, "maximum_channel_percent": 35,
                "maximum_relative_output_hours": 0.2,
                "peak_wrgb": {"white": 30, "red": 20, "green": 25, "blue": 20},
                "twilight_wrgb": {"white": 1, "red": 0, "green": 0, "blue": 1},
            })

    def test_status_exposes_advisory_co2_without_controlling_it(self):
        schedule = self.solar(co2_advisory={
            "enabled": True, "lead_minutes": 60, "stop_before_end_minutes": 60,
        })
        status = schedule.describe(datetime(2026, 10, 30, 12, tzinfo=self.zone))
        self.assertTrue(status["co2"]["advisory_only"])
        self.assertFalse(status["relative_output_is_par"])

    def test_fixed_predawn_viewing_is_dim_and_counted_in_exposure(self):
        base = self.solar()
        viewing = self.solar(predawn_viewing={
            "enabled": True, "start_local": "04:00",
            "wrgb": {"white": 1, "red": 0, "green": 0, "blue": 0},
        })
        day = date(2026, 10, 31)
        self.assertEqual(
            viewing.channels_at(datetime(2026, 10, 31, 3, 59, tzinfo=self.zone)), (0,) * 8,
        )
        self.assertEqual(
            viewing.channels_at(datetime(2026, 10, 31, 4, 0, tzinfo=self.zone)),
            (1, 0, 0, 0, 0, 0, 0, 0),
        )
        self.assertGreater(viewing.relative_output_hours(day), base.relative_output_hours(day))
        self.assertLess(viewing.relative_output_hours(day) - base.relative_output_hours(day), 0.02)
        self.assertEqual(viewing.describe(datetime(2026, 10, 31, 4, tzinfo=self.zone))["predawn_viewing"]["start_local"], "04:00")

    def test_predawn_viewing_rejects_non_dim_channels(self):
        with self.assertRaisesRegex(ValueError, "invalid solar schedule configuration"):
            self.solar(predawn_viewing={
                "enabled": True, "start_local": "04:00",
                "wrgb": {"white": 3, "red": 0, "green": 0, "blue": 0},
            })

    def test_warm_evening_viewing_ends_at_eight_and_is_counted(self):
        base = self.solar()
        viewing = self.solar(evening_viewing={
            "enabled": True, "end_local": "20:00", "fade_minutes": 10,
            "wrgb": {"white": 1, "red": 1, "green": 0, "blue": 0},
        })
        day = date(2026, 12, 21)
        main_end = viewing.productive_window(viewing.events_for(day))[1]
        self.assertEqual(viewing.channels_at(main_end + timedelta(minutes=1)), (1, 1, 0, 0, 0, 0, 0, 0))
        self.assertEqual(viewing.channels_at(datetime(2026, 12, 21, 20, 0, tzinfo=self.zone)), (0,) * 8)
        self.assertEqual(viewing.channels_at(datetime(2026, 6, 21, 20, 1, tzinfo=self.zone)), (0,) * 8)
        self.assertGreater(viewing.relative_output_hours(day), base.relative_output_hours(day))
        status = viewing.describe(datetime(2026, 12, 21, 19, tzinfo=self.zone))
        self.assertEqual(status["evening_viewing"]["end_local"], "20:00")
        self.assertEqual(status["evening_viewing"]["channels"][:4], [1, 1, 0, 0])

    def test_evening_viewing_rejects_bright_or_invalid_configuration(self):
        with self.assertRaisesRegex(ValueError, "invalid solar schedule configuration"):
            self.solar(evening_viewing={
                "enabled": True, "end_local": "20:00", "fade_minutes": 10,
                "wrgb": {"white": 3, "red": 1, "green": 0, "blue": 0},
            })
        with self.assertRaisesRegex(ValueError, "invalid solar schedule configuration"):
            self.solar(evening_viewing={
                "enabled": True, "end_local": "24:00", "fade_minutes": 10,
                "wrgb": {"white": 1, "red": 1, "green": 0, "blue": 0},
            })

    async def test_location_change_persists_and_survives_runner_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "location.json"
            light = FakeLight()
            runner = ScheduleRunner(self.solar(), light, str(path))
            await runner.set_location("90210", 34.0901, -118.4065, "America/Los_Angeles")
            self.assertTrue(path.exists())
            self.assertEqual(runner.status()["location"]["postal_code"], "90210")
            restarted = ScheduleRunner(self.solar(), FakeLight(), str(path))
            location = restarted.status()["location"]
            self.assertEqual(location["postal_code"], "90210")
            self.assertEqual(location["timezone"], "America/Los_Angeles")

    async def test_location_change_rejects_bad_zip_and_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = ScheduleRunner(self.solar(), FakeLight(), str(Path(directory) / "location.json"))
            with self.assertRaisesRegex(ValueError, "five-digit"):
                await runner.set_location("123", 40.0, -100.0, "America/Chicago")
            with self.assertRaisesRegex(ValueError, "unknown timezone"):
                await runner.set_location("12345", 40.0, -100.0, "Not/A_Zone")


if __name__ == "__main__":
    unittest.main()
