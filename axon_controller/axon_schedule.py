"""Deterministic, restart-safe daily lighting schedule for the AXON light."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncio
import logging

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class SchedulePoint:
    minute: int
    channels: tuple[int, ...]


@dataclass(frozen=True)
class LightingSchedule:
    enabled: bool
    timezone: ZoneInfo
    points: tuple[SchedulePoint, ...]
    update_seconds: float = 30.0

    @classmethod
    def load(cls, path: str) -> "LightingSchedule | SolarLightingSchedule":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("mode") == "solar":
            return SolarLightingSchedule.from_dict(data)
        if not isinstance(data, dict) or set(data) - {"enabled", "timezone", "update_seconds", "points"}:
            raise ValueError("invalid schedule keys")
        try:
            timezone = ZoneInfo(data.get("timezone", "America/Chicago"))
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown schedule timezone") from exc
        raw_points = data.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            raise ValueError("schedule requires at least two points")
        points = tuple(sorted((_parse_point(point) for point in raw_points), key=lambda point: point.minute))
        if len({point.minute for point in points}) != len(points):
            raise ValueError("schedule times must be unique")
        update_seconds = float(data.get("update_seconds", 30))
        if not 5 <= update_seconds <= 300:
            raise ValueError("update_seconds must be between 5 and 300")
        return cls(data.get("enabled") is True, timezone, points, update_seconds)

    def channels_at(self, moment: datetime | None = None) -> tuple[int, ...]:
        now = (moment or datetime.now(self.timezone)).astimezone(self.timezone)
        position = now.hour * 60 + now.minute + now.second / 60
        points = self.points
        previous = points[-1]
        following = points[0]
        previous_minute = previous.minute - 1440
        following_minute = following.minute
        for index, point in enumerate(points):
            if point.minute <= position:
                previous = point
                previous_minute = point.minute
                following = points[(index + 1) % len(points)]
                following_minute = following.minute + (1440 if index == len(points) - 1 else 0)
            elif previous_minute < 0:
                break
        if position < points[0].minute:
            position += 1440
            previous_minute = points[-1].minute
            following_minute = points[0].minute + 1440
        span = following_minute - previous_minute
        ratio = 0.0 if span == 0 else (position - previous_minute) / span
        return tuple(round(start + (end - start) * ratio) for start, end in zip(previous.channels, following.channels))


@dataclass(frozen=True)
class SolarEvents:
    civil_dawn: datetime
    sunrise: datetime
    solar_noon: datetime
    sunset: datetime
    civil_dusk: datetime


@dataclass(frozen=True)
class AcclimationConfig:
    enabled: bool = False
    start_date: date | None = None
    duration_days: int = 28
    start_percent: float = 60.0

    def factor_at(self, day: date) -> float:
        if not self.enabled or self.start_date is None:
            return 1.0
        elapsed = (day - self.start_date).days
        if elapsed <= 0:
            return self.start_percent / 100
        if elapsed >= self.duration_days:
            return 1.0
        start = self.start_percent / 100
        return start + (1 - start) * elapsed / self.duration_days


@dataclass(frozen=True)
class Co2AdvisoryConfig:
    """Advisory timing only; this controller never switches life-support equipment."""

    enabled: bool = False
    lead_minutes: int = 60
    stop_before_end_minutes: int = 60


@dataclass(frozen=True)
class PredawnViewingConfig:
    enabled: bool = False
    start_local: time = time(4, 0)
    channels: tuple[int, ...] = (1, 0, 0, 0, 0, 0, 0, 0)


@dataclass(frozen=True)
class EveningViewingConfig:
    enabled: bool = False
    end_local: time = time(20, 0)
    fade_minutes: int = 10
    channels: tuple[int, ...] = (1, 1, 0, 0, 0, 0, 0, 0)


@dataclass(frozen=True)
class SolarLightingSchedule:
    """Solar-anchored aquarium cycle with a bounded productive photoperiod."""

    enabled: bool
    timezone: ZoneInfo
    latitude: float
    longitude: float
    peak_channels: tuple[int, ...]
    twilight_channels: tuple[int, ...]
    productive_hours: float = 6.0
    ramp_minutes: int = 60
    update_seconds: float = 30.0
    max_productive_hours: float = 7.0
    max_channel_percent: int = 35
    max_relative_output_hours: float = 1.35
    solar_noon_offset_minutes: int = 0
    acclimation: AcclimationConfig = AcclimationConfig()
    co2_advisory: Co2AdvisoryConfig = Co2AdvisoryConfig()
    postal_code: str | None = None
    predawn_viewing: PredawnViewingConfig = PredawnViewingConfig()
    evening_viewing: EveningViewingConfig = EveningViewingConfig()

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SolarLightingSchedule":
        allowed = {
            "mode", "enabled", "timezone", "latitude", "longitude", "peak_channels",
            "twilight_channels", "productive_hours", "ramp_minutes", "update_seconds",
            "schema_version", "lighting", "seasonal", "acclimation", "co2_advisory",
            "postal_code",
            "predawn_viewing",
            "evening_viewing",
        }
        if set(data) - allowed:
            raise ValueError("invalid solar schedule keys")
        try:
            zone = ZoneInfo(str(data.get("timezone", "America/Chicago")))
            latitude = float(data["latitude"])
            longitude = float(data["longitude"])
            lighting = _mapping(data.get("lighting", {}), "lighting")
            seasonal = _mapping(data.get("seasonal", {}), "seasonal")
            if set(lighting) - {
                "meaningful_light_hours", "maximum_meaningful_light_hours", "ramp_minutes",
                "maximum_channel_percent", "maximum_relative_output_hours", "peak_wrgb",
                "twilight_wrgb",
            }:
                raise ValueError("invalid lighting keys")
            if set(seasonal) - {"anchor", "outer_twilight", "solar_noon_offset_minutes"}:
                raise ValueError("invalid seasonal keys")
            if seasonal.get("anchor", "solar_noon") != "solar_noon":
                raise ValueError("seasonal anchor must be solar_noon")
            if seasonal.get("outer_twilight", "civil") != "civil":
                raise ValueError("outer_twilight must be civil")
            solar_noon_offset_minutes = int(seasonal.get("solar_noon_offset_minutes", 0))
            peak = _parse_named_or_legacy_channels(lighting.get("peak_wrgb", data.get("peak_channels")))
            twilight = _parse_named_or_legacy_channels(
                lighting.get("twilight_wrgb", data.get("twilight_channels", [1, 0, 0, 1, 0, 0, 0, 0]))
            )
            productive_hours = float(lighting.get("meaningful_light_hours", data.get("productive_hours", 6)))
            max_productive_hours = float(lighting.get("maximum_meaningful_light_hours", 7))
            ramp_minutes = int(lighting.get("ramp_minutes", data.get("ramp_minutes", 60)))
            max_channel_percent = int(lighting.get("maximum_channel_percent", 35))
            max_relative_output_hours = float(lighting.get("maximum_relative_output_hours", 1.35))
            update_seconds = float(data.get("update_seconds", 30))
            acclimation = _parse_acclimation(data.get("acclimation", {}))
            co2_advisory = _parse_co2_advisory(data.get("co2_advisory", {}))
            postal_code = data.get("postal_code")
            predawn_viewing = _parse_predawn_viewing(data.get("predawn_viewing", {}))
            evening_viewing = _parse_evening_viewing(data.get("evening_viewing", {}))
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("invalid solar schedule configuration") from exc
        if not -66 <= latitude <= 66 or not -180 <= longitude <= 180:
            raise ValueError("solar schedule coordinates are outside the supported range")
        if not 4 <= productive_hours <= max_productive_hours <= 10:
            raise ValueError("meaningful light hours must be between 4 and the configured maximum (at most 10)")
        if not 15 <= ramp_minutes <= 180:
            raise ValueError("ramp_minutes must be between 15 and 180")
        if ramp_minutes * 2 >= productive_hours * 60:
            raise ValueError("ramps must leave a positive daytime plateau")
        if not 5 <= update_seconds <= 300:
            raise ValueError("update_seconds must be between 5 and 300")
        if not 1 <= max_channel_percent <= 100 or max(peak) > max_channel_percent:
            raise ValueError("peak channels exceed maximum_channel_percent")
        if any(twilight[index] > peak[index] for index in range(8)):
            raise ValueError("twilight channels cannot exceed peak channels")
        if not 0.1 <= max_relative_output_hours <= 8:
            raise ValueError("maximum_relative_output_hours must be between 0.1 and 8")
        if not -120 <= solar_noon_offset_minutes <= 120:
            raise ValueError("solar_noon_offset_minutes must be between -120 and 120")
        if postal_code is not None and (not isinstance(postal_code, str) or re.fullmatch(r"\d{5}", postal_code) is None):
            raise ValueError("postal_code must be a five-digit US ZIP code")
        schedule = cls(
            data.get("enabled") is True, zone, latitude, longitude, peak, twilight,
            productive_hours, ramp_minutes, update_seconds, max_productive_hours,
            max_channel_percent, max_relative_output_hours, solar_noon_offset_minutes, acclimation, co2_advisory,
            postal_code, predawn_viewing, evening_viewing,
        )
        # Validate the worst seasonal outer-twilight day at full acclimation.
        summer = date(2028, 6, 20)
        if schedule.relative_output_hours(summer, acclimation_factor=1.0) > max_relative_output_hours:
            raise ValueError("configured curve exceeds maximum_relative_output_hours")
        return schedule

    def events_for(self, day: date) -> SolarEvents:
        noon_utc, sunrise_utc, sunset_utc = _solar_times(day, self.latitude, self.longitude, 90.833)
        _, dawn_utc, dusk_utc = _solar_times(day, self.latitude, self.longitude, 96.0)
        convert = lambda value: value.astimezone(self.timezone)
        return SolarEvents(convert(dawn_utc), convert(sunrise_utc), convert(noon_utc), convert(sunset_utc), convert(dusk_utc))

    def channels_at(self, moment: datetime | None = None) -> tuple[int, ...]:
        now = (moment or datetime.now(self.timezone)).astimezone(self.timezone)
        events = self.events_for(now.date())
        predawn_start = datetime.combine(now.date(), self.predawn_viewing.start_local, self.timezone)
        if now < events.civil_dawn:
            if self.predawn_viewing.enabled and predawn_start <= now:
                return self.predawn_viewing.channels
            return (0,) * 8
        main_start, main_end = self.productive_window(events)
        ramp = timedelta(minutes=self.ramp_minutes)
        peak = self.effective_peak(now.date())
        if now < main_start:
            return self.twilight_channels
        if now < main_start + ramp:
            return _interpolate(self.twilight_channels, peak, (now - main_start) / ramp)
        if now >= main_end - ramp:
            if now < main_end:
                return _interpolate(peak, self.twilight_channels, (now - (main_end - ramp)) / ramp)
            if self.evening_viewing.enabled:
                evening_end = datetime.combine(now.date(), self.evening_viewing.end_local, self.timezone)
                if main_end <= now < evening_end:
                    fade = timedelta(minutes=self.evening_viewing.fade_minutes)
                    fade_start = max(main_end, evening_end - fade)
                    if now >= fade_start:
                        return _interpolate(
                            self.evening_viewing.channels, (0,) * 8,
                            (now - fade_start) / (evening_end - fade_start),
                        )
                    return self.evening_viewing.channels
                return (0,) * 8
            if now < events.civil_dusk:
                return self.twilight_channels
            return (0,) * 8
        return peak

    def productive_window(self, events: SolarEvents) -> tuple[datetime, datetime]:
        half_window = timedelta(hours=self.productive_hours / 2)
        anchor = events.solar_noon + timedelta(minutes=self.solar_noon_offset_minutes)
        return anchor - half_window, anchor + half_window

    def effective_peak(self, day: date) -> tuple[int, ...]:
        factor = self.acclimation.factor_at(day)
        return _interpolate(self.twilight_channels, self.peak_channels, factor)

    def relative_output_hours(self, day: date, *, acclimation_factor: float | None = None) -> float:
        """Integral of mean WRGB command, not a PAR/DLI measurement."""
        events = self.events_for(day)
        factor = self.acclimation.factor_at(day) if acclimation_factor is None else acclimation_factor
        peak = tuple(first + (second - first) * factor for first, second in zip(self.twilight_channels, self.peak_channels))
        mean_peak = sum(peak[:4]) / 400
        mean_twilight = sum(self.twilight_channels[:4]) / 400
        ramp_hours = self.ramp_minutes / 60
        productive = mean_peak * (self.productive_hours - ramp_hours) + mean_twilight * ramp_hours
        main_start, main_end = self.productive_window(events)
        morning_twilight_hours = max(0.0, (main_start - events.civil_dawn).total_seconds() / 3600)
        outer_twilight = mean_twilight * morning_twilight_hours
        if self.evening_viewing.enabled:
            evening_end = datetime.combine(day, self.evening_viewing.end_local, self.timezone)
            evening_hours = max(0.0, (evening_end - main_end).total_seconds() / 3600)
            fade_hours = min(evening_hours, self.evening_viewing.fade_minutes / 60)
            mean_evening = sum(self.evening_viewing.channels[:4]) / 400
            outer_twilight += mean_evening * (evening_hours - fade_hours / 2)
        else:
            evening_twilight_hours = max(0.0, (events.civil_dusk - main_end).total_seconds() / 3600)
            outer_twilight += mean_twilight * evening_twilight_hours
        predawn = 0.0
        if self.predawn_viewing.enabled:
            predawn_start = datetime.combine(day, self.predawn_viewing.start_local, self.timezone)
            predawn_hours = max(0.0, (events.civil_dawn - predawn_start).total_seconds() / 3600)
            predawn = sum(self.predawn_viewing.channels[:4]) / 400 * predawn_hours
        return productive + outer_twilight + predawn

    def describe(self, moment: datetime | None = None) -> dict[str, object]:
        now = (moment or datetime.now(self.timezone)).astimezone(self.timezone)
        events = self.events_for(now.date())
        start, end = self.productive_window(events)
        factor = self.acclimation.factor_at(now.date())
        co2 = None
        if self.co2_advisory.enabled:
            co2 = {
                "advisory_only": True,
                "recommended_start": (start - timedelta(minutes=self.co2_advisory.lead_minutes)).isoformat(),
                "recommended_stop": (end - timedelta(minutes=self.co2_advisory.stop_before_end_minutes)).isoformat(),
            }
        return {
            "mode": "solar",
            "productive_start": start.isoformat(),
            "productive_end": end.isoformat(),
            "meaningful_light_hours": self.productive_hours,
            "maximum_meaningful_light_hours": self.max_productive_hours,
            "solar_noon_offset_minutes": self.solar_noon_offset_minutes,
            "effective_peak_channels": list(self.effective_peak(now.date())),
            "maximum_channel_percent": self.max_channel_percent,
            "relative_output_hours": round(self.relative_output_hours(now.date()), 3),
            "maximum_relative_output_hours": self.max_relative_output_hours,
            "relative_output_is_par": False,
            "acclimation": {
                "enabled": self.acclimation.enabled,
                "factor_percent": round(factor * 100, 1),
                "start_date": self.acclimation.start_date.isoformat() if self.acclimation.start_date else None,
                "duration_days": self.acclimation.duration_days,
            },
            "co2": co2,
            "location": {
                "postal_code": self.postal_code,
                "latitude": self.latitude,
                "longitude": self.longitude,
                "timezone": self.timezone.key,
            },
            "predawn_viewing": {
                "enabled": self.predawn_viewing.enabled,
                "start_local": self.predawn_viewing.start_local.strftime("%H:%M"),
                "channels": list(self.predawn_viewing.channels),
            },
            "evening_viewing": {
                "enabled": self.evening_viewing.enabled,
                "end_local": self.evening_viewing.end_local.strftime("%H:%M"),
                "fade_minutes": self.evening_viewing.fade_minutes,
                "channels": list(self.evening_viewing.channels),
            },
        }


def _solar_times(day: date, latitude: float, longitude: float, zenith: float) -> tuple[datetime, datetime, datetime]:
    """Approximate NOAA solar noon and symmetric zenith crossings in UTC."""
    days = 366 if _is_leap(day.year) else 365
    gamma = 2 * math.pi / days * (day.timetuple().tm_yday - 1)
    equation = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    latitude_radians = math.radians(latitude)
    cosine = (
        math.cos(math.radians(zenith)) / (math.cos(latitude_radians) * math.cos(declination))
        - math.tan(latitude_radians) * math.tan(declination)
    )
    if not -1 <= cosine <= 1:
        raise ValueError("solar event does not occur at this latitude and date")
    hour_angle = math.degrees(math.acos(cosine))
    noon_minutes = 720 - 4 * longitude - equation
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    noon = midnight + timedelta(minutes=noon_minutes)
    return noon, noon - timedelta(minutes=4 * hour_angle), noon + timedelta(minutes=4 * hour_angle)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _interpolate(start: tuple[int, ...], end: tuple[int, ...], ratio: float) -> tuple[int, ...]:
    return tuple(round(first + (second - first) * ratio) for first, second in zip(start, end))


def _parse_channels(values: object) -> tuple[int, ...]:
    if not isinstance(values, list) or len(values) != 8:
        raise ValueError("exactly eight channel intensities are required")
    if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100 for value in values):
        raise ValueError("channel intensities must be integers from 0 through 100")
    return tuple(values)


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _parse_named_or_legacy_channels(value: object) -> tuple[int, ...]:
    if isinstance(value, list):
        return _parse_channels(value)
    values = _mapping(value, "WRGB channels")
    if set(values) != {"white", "red", "green", "blue"}:
        raise ValueError("WRGB channels require white, red, green, and blue")
    return _parse_channels([values[name] for name in ("white", "red", "green", "blue")] + [0, 0, 0, 0])


def _parse_acclimation(value: object) -> AcclimationConfig:
    data = _mapping(value, "acclimation")
    if set(data) - {"enabled", "start_date", "duration_days", "start_percent"}:
        raise ValueError("invalid acclimation keys")
    enabled = data.get("enabled") is True
    start = data.get("start_date")
    if enabled and not isinstance(start, str):
        raise ValueError("enabled acclimation requires start_date")
    try:
        parsed_start = date.fromisoformat(start) if isinstance(start, str) else None
        duration = int(data.get("duration_days", 28))
        start_percent = float(data.get("start_percent", 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid acclimation configuration") from exc
    if not 7 <= duration <= 90:
        raise ValueError("acclimation duration_days must be between 7 and 90")
    if not 20 <= start_percent <= 100:
        raise ValueError("acclimation start_percent must be between 20 and 100")
    return AcclimationConfig(enabled, parsed_start, duration, start_percent)


def _parse_co2_advisory(value: object) -> Co2AdvisoryConfig:
    data = _mapping(value, "co2_advisory")
    if set(data) - {"enabled", "lead_minutes", "stop_before_end_minutes"}:
        raise ValueError("invalid co2_advisory keys")
    try:
        lead = int(data.get("lead_minutes", 60))
        stop = int(data.get("stop_before_end_minutes", 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid CO2 advisory configuration") from exc
    if not 0 <= lead <= 180 or not 0 <= stop <= 180:
        raise ValueError("CO2 advisory offsets must be between 0 and 180 minutes")
    return Co2AdvisoryConfig(data.get("enabled") is True, lead, stop)


def _parse_predawn_viewing(value: object) -> PredawnViewingConfig:
    data = _mapping(value, "predawn_viewing")
    if set(data) - {"enabled", "start_local", "wrgb"}:
        raise ValueError("invalid predawn_viewing keys")
    start_text = data.get("start_local", "04:00")
    if not isinstance(start_text, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_text) is None:
        raise ValueError("predawn start_local must use HH:MM")
    hour, minute = (int(part) for part in start_text.split(":"))
    channels = _parse_named_or_legacy_channels(
        data.get("wrgb", {"white": 1, "red": 0, "green": 0, "blue": 0})
    )
    if max(channels) > 2:
        raise ValueError("predawn viewing channels cannot exceed 2 percent")
    return PredawnViewingConfig(data.get("enabled") is True, time(hour, minute), channels)


def _parse_evening_viewing(value: object) -> EveningViewingConfig:
    data = _mapping(value, "evening_viewing")
    if set(data) - {"enabled", "end_local", "fade_minutes", "wrgb", "channels"}:
        raise ValueError("invalid evening_viewing keys")
    end_text = data.get("end_local", "20:00")
    if not isinstance(end_text, str) or re.fullmatch(r"\d{2}:\d{2}", end_text) is None:
        raise ValueError("evening_viewing end_local must use HH:MM")
    hour, minute = (int(part) for part in end_text.split(":"))
    if hour > 23 or minute > 59:
        raise ValueError("invalid evening_viewing end_local")
    fade_minutes = int(data.get("fade_minutes", 10))
    if not 1 <= fade_minutes <= 60:
        raise ValueError("evening_viewing fade_minutes must be between 1 and 60")
    channels = _parse_named_or_legacy_channels(
        data.get("wrgb", data.get("channels", {"white": 1, "red": 1, "green": 0, "blue": 0}))
    )
    if max(channels) > 2:
        raise ValueError("evening viewing channels cannot exceed 2 percent")
    return EveningViewingConfig(data.get("enabled") is True, time(hour, minute), fade_minutes, channels)


def _parse_point(data: object) -> SchedulePoint:
    if not isinstance(data, dict) or set(data) != {"time", "channels"}:
        raise ValueError("each schedule point requires only time and channels")
    try:
        hour_text, minute_text = data["time"].split(":")
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("schedule time must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or len(data["time"]) != 5:
        raise ValueError("schedule time must use HH:MM")
    return SchedulePoint(hour * 60 + minute, _parse_channels(data["channels"]))


class ScheduleRunner:
    """Applies only changed setpoints and derives state from wall-clock time."""

    def __init__(self, schedule: LightingSchedule, light: object, location_path: str | None = None) -> None:
        self.schedule = schedule
        self.light = light
        self.location_path = Path(location_path) if location_path else None
        self._last: tuple[int, ...] | None = None
        self._override_channels: tuple[int, ...] | None = None
        self._override_until: datetime | None = None
        self._lock = asyncio.Lock()
        self._load_location()

    def _load_location(self) -> None:
        if self.location_path is None or not self.location_path.exists():
            return
        if not isinstance(self.schedule, SolarLightingSchedule):
            LOG.warning("ignoring saved AXON location for non-solar schedule")
            return
        try:
            data = json.loads(self.location_path.read_text(encoding="utf-8"))
            self.schedule = self._updated_location(data)
            LOG.info("loaded persistent AXON solar location ZIP=%s", self.schedule.postal_code)
        except Exception:
            LOG.exception("invalid saved AXON location; retaining configured location")

    def _updated_location(self, data: object) -> SolarLightingSchedule:
        if not isinstance(self.schedule, SolarLightingSchedule) or not isinstance(data, dict):
            raise ValueError("solar location is unavailable")
        if set(data) != {"postal_code", "latitude", "longitude", "timezone"}:
            raise ValueError("invalid location keys")
        postal_code = data.get("postal_code")
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        timezone_name = data.get("timezone")
        if not isinstance(postal_code, str) or re.fullmatch(r"\d{5}", postal_code) is None:
            raise ValueError("postal_code must be a five-digit US ZIP code")
        if isinstance(latitude, bool) or not isinstance(latitude, (int, float)) or not -66 <= latitude <= 66:
            raise ValueError("latitude is outside the supported range")
        if isinstance(longitude, bool) or not isinstance(longitude, (int, float)) or not -180 <= longitude <= 180:
            raise ValueError("longitude is outside the supported range")
        if not isinstance(timezone_name, str) or len(timezone_name) > 64:
            raise ValueError("invalid timezone")
        try:
            zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return replace(
            self.schedule, postal_code=postal_code, latitude=float(latitude),
            longitude=float(longitude), timezone=zone,
        )

    async def set_location(
        self, postal_code: str, latitude: float, longitude: float, timezone_name: str,
    ) -> object:
        data = {
            "postal_code": postal_code, "latitude": latitude,
            "longitude": longitude, "timezone": timezone_name,
        }
        updated = self._updated_location(data)
        if self.location_path is None:
            raise RuntimeError("persistent location storage is not configured")
        async with self._lock:
            self.location_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
            temporary = self.location_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.location_path)
            self.schedule = updated
            self._last = None
            LOG.info("updated persistent AXON solar location ZIP=%s timezone=%s", postal_code, timezone_name)
            if self._override_channels is not None:
                return await self.light.status()
            channels = self.schedule.channels_at()
            return await self.light.set_channels(channels, any(channels))

    def status(self, moment: datetime | None = None) -> dict[str, object]:
        now = moment or datetime.now(self.schedule.timezone)
        active = self._override_until is not None and now < self._override_until
        result = {
            "schedule_enabled": self.schedule.enabled,
            "override_active": active,
            "override_until": self._override_until.isoformat() if active else None,
            "scheduled_channels": list(self.schedule.channels_at(now)) if self.schedule.enabled else None,
        }
        if isinstance(self.schedule, SolarLightingSchedule):
            result.update(self.schedule.describe(now))
        return result

    async def set_override(self, channels: tuple[int, ...], duration_seconds: int) -> object:
        if not 60 <= duration_seconds <= 14400:
            raise ValueError("override duration must be between 60 and 14400 seconds")
        # Validate before storing override state.
        values = _parse_channels(list(channels))
        if isinstance(self.schedule, SolarLightingSchedule):
            if max(values) > self.schedule.max_channel_percent:
                raise ValueError("override exceeds maximum_channel_percent")
            relative_output = sum(values[:4]) / 400 * duration_seconds / 3600
            if relative_output > self.schedule.max_relative_output_hours:
                raise ValueError("override exceeds maximum_relative_output_hours")
        async with self._lock:
            self._override_channels = values
            self._override_until = datetime.now(self.schedule.timezone) + timedelta(seconds=duration_seconds)
            return await self.light.set_channels(values, any(values))

    async def resume(self) -> object:
        async with self._lock:
            self._override_channels = None
            self._override_until = None
            self._last = None
            channels = self.schedule.channels_at() if self.schedule.enabled else (0,) * 8
            return await self.light.set_channels(channels, any(channels))

    async def apply(self, moment: datetime | None = None) -> tuple[int, ...] | None:
        async with self._lock:
            now = moment or datetime.now(self.schedule.timezone)
            if self._override_until is not None and now >= self._override_until:
                self._override_channels = None
                self._override_until = None
                self._last = None
                LOG.info("AXON manual override expired; resuming solar schedule")
            if self._override_channels is not None:
                channels = self._override_channels
                source = "override"
            elif self.schedule.enabled:
                channels = self.schedule.channels_at(now)
                source = "schedule"
            else:
                return None
            enabled = any(channels)
            reconcile = getattr(self.light, "reconcile", None)
            if reconcile is not None:
                current = await reconcile(channels, enabled)
                changed = channels != self._last
            else:
                current = await self.light.status()
                changed = channels != self._last or current.channels != channels or current.enabled != enabled
                if changed:
                    await self.light.set_channels(channels, enabled)
            if changed:
                self._last = channels
                LOG.info("applied AXON %s channels=%s", source, channels)
            return channels

    async def run_forever(self) -> None:
        while True:
            try:
                await self.apply()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("AXON schedule update failed")
            await asyncio.sleep(self.schedule.update_seconds)
