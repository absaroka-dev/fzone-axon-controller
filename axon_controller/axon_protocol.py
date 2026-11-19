"""Pure protocol helpers for the FZONE AXON BLE light.

These helpers perform no Bluetooth I/O.  Frames were derived from My Fzone
2.0.2 and checked against a read-only response from the project's AXON light.
"""

from __future__ import annotations

from dataclasses import dataclass


REQUEST_HEADER = bytes((0x55, 0xAA, 0x06))
RESPONSE_HEADER = bytes((0x55, 0xAA, 0x07))
MESSAGE_POWER = 0x01
MESSAGE_MODE = 0x02
MESSAGE_MANUAL = 0x03
MESSAGE_QUERY = 0x0E
MESSAGE_CHANNEL_STATE = 0x11
QUERY_CHANNEL_STATE = 0x01
QUERY_ALL_STATE = 0x00


class FrameError(ValueError):
    """Raised when an AXON frame is malformed."""


@dataclass(frozen=True)
class Frame:
    header: bytes
    message_id: int
    payload: bytes


@dataclass(frozen=True)
class ChannelState:
    channels: tuple[int, ...]
    enabled: bool


def build_frame(message_id: int, payload: bytes = b"") -> bytes:
    if not 0 <= message_id <= 0xFF:
        raise ValueError("message_id must fit in one byte")
    body = bytes((message_id,)) + bytes(payload)
    if len(body) > 0xFFFF:
        raise ValueError("payload is too large")
    frame = REQUEST_HEADER + len(body).to_bytes(2, "big") + body
    return frame + bytes((sum(frame) & 0xFF,))


def parse_frame(data: bytes) -> Frame:
    raw = bytes(data)
    if len(raw) < 7:
        raise FrameError("frame is too short")
    if raw[:3] not in (REQUEST_HEADER, RESPONSE_HEADER):
        raise FrameError("invalid frame header")
    if int.from_bytes(raw[3:5], "big") != len(raw) - 6:
        raise FrameError("invalid frame length")
    if sum(raw[:-1]) & 0xFF != raw[-1]:
        raise FrameError("invalid frame checksum")
    return Frame(raw[:3], raw[5], raw[6:-1])


def channel_state_query() -> bytes:
    return build_frame(MESSAGE_QUERY, bytes((QUERY_CHANNEL_STATE,)))


def all_state_query() -> bytes:
    return build_frame(MESSAGE_QUERY, bytes((QUERY_ALL_STATE,)))


def power_command(enabled: bool) -> bytes:
    """Build the app's global power command; caller controls whether it is sent."""
    return build_frame(MESSAGE_POWER, bytes((int(enabled),)))


def channel_command(channels: tuple[int, ...] | list[int], enabled: bool = True) -> bytes:
    """Build a telemetry/preview frame from canonical W/R/G/B channels."""
    values = _to_device_order(_validate_channels(channels))
    return build_frame(MESSAGE_CHANNEL_STATE, bytes((*values, int(enabled))))


def manual_channel_command(channels: tuple[int, ...] | list[int], mode_id: int = 0) -> bytes:
    """Build a persistent profile from canonical W/R/G/B channels."""
    values = _to_device_order(_validate_channels(channels))
    if isinstance(mode_id, bool) or not isinstance(mode_id, int) or not 0 <= mode_id <= 255:
        raise ValueError("mode_id must be an integer from 0 through 255")
    # The last byte is the manual CO2 switch. This project does not control CO2
    # through the light, so it remains disabled.
    return build_frame(MESSAGE_MANUAL, bytes((mode_id, *values, 0)))


def manual_mode_command() -> bytes:
    return build_frame(MESSAGE_MODE, b"\x00")


def _validate_channels(channels: tuple[int, ...] | list[int]) -> tuple[int, ...]:
    values = tuple(channels)
    if len(values) != 8:
        raise ValueError("exactly eight channel intensities are required")
    if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100 for value in values):
        raise ValueError("channel intensities must be integers from 0 through 100")
    return values


def _to_device_order(channels: tuple[int, ...]) -> tuple[int, ...]:
    """Translate canonical W/R/G/B into the AXON's observed W/G/B/R order."""
    white, red, green, blue, *remaining = channels
    return (white, green, blue, red, *remaining)


def _from_device_order(channels: tuple[int, ...]) -> tuple[int, ...]:
    """Translate the AXON's observed W/G/B/R order into canonical W/R/G/B."""
    white, green, blue, red, *remaining = channels
    return (white, red, green, blue, *remaining)


def parse_power_state(frame: Frame) -> bool:
    if frame.message_id != MESSAGE_POWER or len(frame.payload) != 1 or frame.payload[0] not in (0, 1):
        raise FrameError("invalid power-state frame")
    return bool(frame.payload[0])


def parse_channel_state(frame: Frame) -> ChannelState:
    if frame.message_id != MESSAGE_CHANNEL_STATE:
        raise FrameError("not a channel-state frame")
    if len(frame.payload) != 9:
        raise FrameError("channel-state payload must contain eight channels and enabled")
    channels = tuple(frame.payload[:8])
    if any(value > 100 for value in channels):
        raise FrameError("channel intensity is outside 0-100")
    if frame.payload[8] not in (0, 1):
        raise FrameError("enabled flag is not boolean")
    return ChannelState(_from_device_order(channels), bool(frame.payload[8]))
