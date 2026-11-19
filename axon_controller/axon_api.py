"""Strict local Unix-socket API for the AXON controller."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from .axon_ble import AxonBLE, CachedAxon

MAX_REQUEST = 4096


class AxonAPI:
    def __init__(self, light: AxonBLE | CachedAxon, scheduler: object | None = None) -> None:
        self.light = light
        self.scheduler = scheduler

    @staticmethod
    def _state(state: object) -> dict[str, object]:
        return {
            "enabled": state.enabled,
            "channels": list(state.channels),
            "brightness": max(state.channels[:4]),
        }

    async def dispatch(self, request: object) -> tuple[int, dict[str, object]]:
        if not isinstance(request, dict) or "operation" not in request:
            return 400, {"ok": False, "error": "operation is required"}
        operation = request.get("operation")
        if operation == "health" and set(request) == {"operation"}:
            automation = self.scheduler.status() if self.scheduler else {
                "schedule_enabled": False, "override_active": False,
                "override_until": None, "scheduled_channels": None,
            }
            return 200, {
                "ok": True,
                "service": "axon-light-controller",
                "control_enabled": self.light.allow_control,
                "automation": automation,
                "connectivity": {
                    "last_success_timestamp": getattr(self.light, "last_success_at", None),
                    "consecutive_failures": getattr(self.light, "consecutive_failures", 0),
                    "last_error": getattr(self.light, "last_error", None),
                },
            }
        if operation == "status" and set(request) == {"operation"}:
            try:
                automation = self.scheduler.status() if self.scheduler else {
                    "schedule_enabled": False, "override_active": False,
                    "override_until": None, "scheduled_channels": None,
                }
                state = await self.light.status()
                connectivity = {
                    "last_success_timestamp": getattr(self.light, "last_success_at", None),
                    "consecutive_failures": getattr(self.light, "consecutive_failures", 0),
                    "last_error": getattr(self.light, "last_error", None),
                }
                return 200, {
                    "ok": True, **self._state(state),
                    "automation": automation, "connectivity": connectivity,
                }
            except Exception:
                return 503, {"ok": False, "error": "light_unavailable"}
        if operation == "live_status" and set(request) == {"operation"}:
            try:
                refresh = getattr(self.light, "refresh", None)
                state = await refresh() if refresh is not None else await self.light.status()
                automation = self.scheduler.status() if self.scheduler else {"schedule_enabled": False}
                return 200, {"ok": True, **self._state(state), "automation": automation, "live": True}
            except Exception:
                return 503, {"ok": False, "error": "light_unavailable", "live": True}
        if operation == "set_power" and set(request) == {"operation", "enabled"} and isinstance(request.get("enabled"), bool):
            try:
                return 200, {"ok": True, **self._state(await self.light.set_power(request["enabled"]))}
            except PermissionError as exc:
                return 403, {"ok": False, "error": str(exc)}
            except Exception:
                return 503, {"ok": False, "error": "light_unavailable"}
        if operation == "set_channels" and set(request) == {"operation", "channels"}:
            channels = request.get("channels")
            if not isinstance(channels, list):
                return 400, {"ok": False, "error": "channels must be a list"}
            try:
                state = await self.light.set_channels(tuple(channels), any(channels))
                return 200, {"ok": True, **self._state(state)}
            except (TypeError, ValueError) as exc:
                return 400, {"ok": False, "error": str(exc)}
            except PermissionError as exc:
                return 403, {"ok": False, "error": str(exc)}
            except Exception:
                return 503, {"ok": False, "error": "light_unavailable"}
        if operation == "override" and set(request) == {"operation", "channels", "duration_seconds"}:
            if self.scheduler is None:
                return 503, {"ok": False, "error": "schedule_unavailable"}
            channels = request.get("channels")
            duration = request.get("duration_seconds")
            if not isinstance(channels, list) or isinstance(duration, bool) or not isinstance(duration, int):
                return 400, {"ok": False, "error": "invalid override parameters"}
            try:
                state = await self.scheduler.set_override(tuple(channels), duration)
                return 200, {"ok": True, **self._state(state), "automation": self.scheduler.status()}
            except (TypeError, ValueError) as exc:
                return 400, {"ok": False, "error": str(exc)}
            except PermissionError as exc:
                return 403, {"ok": False, "error": str(exc)}
            except Exception:
                return 503, {"ok": False, "error": "light_unavailable"}
        if operation == "resume_schedule" and set(request) == {"operation"}:
            if self.scheduler is None:
                return 503, {"ok": False, "error": "schedule_unavailable"}
            try:
                state = await self.scheduler.resume()
                return 200, {"ok": True, **self._state(state), "automation": self.scheduler.status()}
            except Exception:
                return 503, {"ok": False, "error": "light_unavailable"}
        if operation == "set_location" and set(request) == {
            "operation", "postal_code", "latitude", "longitude", "timezone",
        }:
            if self.scheduler is None:
                return 503, {"ok": False, "error": "schedule_unavailable"}
            try:
                state = await self.scheduler.set_location(
                    request["postal_code"], request["latitude"], request["longitude"], request["timezone"],
                )
                return 200, {"ok": True, **self._state(state), "automation": self.scheduler.status()}
            except (TypeError, ValueError) as exc:
                return 400, {"ok": False, "error": str(exc)}
            except Exception:
                return 503, {"ok": False, "error": "location_update_failed"}
        return 400, {"ok": False, "error": "invalid operation or parameters"}


async def _serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, api: AxonAPI) -> None:
    try:
        raw = await reader.readline()
        if len(raw) > MAX_REQUEST or not raw.endswith(b"\n"):
            status, body = 413, {"ok": False, "error": "request too large or incomplete"}
        else:
            try:
                status, body = await api.dispatch(json.loads(raw))
            except (json.JSONDecodeError, UnicodeDecodeError):
                status, body = 400, {"ok": False, "error": "invalid JSON"}
        writer.write(json.dumps({"status": status, **body}, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def create_axon_server(path: str, api: AxonAPI) -> asyncio.AbstractServer:
    socket = Path(path)
    socket.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if socket.exists():
        if not socket.is_socket():
            raise RuntimeError(f"refusing to replace non-socket path: {path}")
        socket.unlink()
    server = await asyncio.start_unix_server(lambda r, w: _serve(r, w, api), path)
    os.chmod(path, 0o660)
    return server
