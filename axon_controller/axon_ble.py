"""Serialized BLE transport for the FZONE AXON light."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from .axon_protocol import (
    ChannelState,
    Frame,
    MESSAGE_POWER,
    MESSAGE_CHANNEL_STATE,
    all_state_query,
    manual_channel_command,
    manual_mode_command,
    channel_state_query,
    parse_channel_state,
    parse_frame,
    parse_power_state,
    power_command,
)

WRITE_UUID = "0000ae01-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ae02-0000-1000-8000-00805f9b34fb"
LOG = logging.getLogger(__name__)


class AxonBLE:
    def __init__(
        self,
        address: str,
        *,
        allow_control: bool = False,
        timeout: float = 8.0,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.address = address
        self.allow_control = allow_control
        self.timeout = timeout
        self._client_factory = client_factory
        self._lock = asyncio.Lock()
        self._connected_client: Any | None = None
        self._pending: dict[int, asyncio.Future[Frame]] = {}

    def _factory(self) -> Callable[..., Any]:
        if self._client_factory is not None:
            return self._client_factory
        try:
            from bleak import BleakClient
        except ImportError as exc:  # pragma: no cover - target dependency
            raise RuntimeError("Bleak is required for AXON Bluetooth access") from exc
        return BleakClient

    async def _new_client(self) -> Any:
        """Build a client for a freshly discovered fixture.

        BlueZ forgets unpaired LE device objects when bluetoothd restarts. A
        bare address can then time out even while the AXON is advertising, so
        production connections rediscover the device first. Injected test
        factories retain the deterministic address-only behavior.
        """
        factory = self._factory()
        target: Any = self.address
        if self._client_factory is None:
            from bleak import BleakScanner

            target = await BleakScanner.find_device_by_address(self.address, timeout=self.timeout)
            if target is None:
                raise TimeoutError(f"AXON {self.address} was not discovered")
        return factory(target, timeout=self.timeout)

    def _notified(self, _sender: object, data: bytearray) -> None:
        try:
            frame = parse_frame(bytes(data))
        except ValueError:
            return
        result = self._pending.get(frame.message_id)
        if result is not None and not result.done():
            result.set_result(frame)

    async def _ensure_client(self) -> Any:
        client = self._connected_client
        if client is not None and client.is_connected:
            return client
        client = await self._new_client()

        def disconnected(disconnected_client: Any) -> None:
            if self._connected_client is disconnected_client:
                self._connected_client = None

        if hasattr(client, "set_disconnected_callback"):
            client.set_disconnected_callback(disconnected)
        try:
            await client.connect()
            await client.start_notify(NOTIFY_UUID, self._notified)
        except Exception:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise
        self._connected_client = client
        return client

    async def _drop_client(self) -> None:
        client, self._connected_client = self._connected_client, None
        for result in self._pending.values():
            if not result.done():
                result.cancel()
        self._pending.clear()
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def _request(self, client: Any, command: bytes, expected_message: int) -> Frame:
        loop = asyncio.get_running_loop()
        result: asyncio.Future[Frame] = loop.create_future()
        self._pending[expected_message] = result
        try:
            await client.write_gatt_char(WRITE_UUID, command, response=False)
            return await asyncio.wait_for(result, timeout=self.timeout)
        finally:
            self._pending.pop(expected_message, None)

    async def _exchange(self, command: bytes, expected_message: int | None) -> Frame | None:
        async with self._lock:
            try:
                client = await self._ensure_client()
                if expected_message is None:
                    await client.write_gatt_char(WRITE_UUID, command, response=False)
                    return None
                return await self._request(client, command, expected_message)
            except Exception:
                await self._drop_client()
                raise

    async def status(self) -> ChannelState:
        async with self._lock:
            try:
                client = await self._ensure_client()
                return await self._status_with_client(client)
            except Exception:
                await self._drop_client()
                raise

    async def _status_with_client(self, client: Any) -> ChannelState:
        """Read power and channels over an already connected client."""
        power_frame = await self._request(client, all_state_query(), MESSAGE_POWER)
        channel_frame = await self._request(client, channel_state_query(), MESSAGE_CHANNEL_STATE)
        channels = parse_channel_state(channel_frame).channels
        return ChannelState(channels, parse_power_state(power_frame))

    async def reconcile(self, channels: tuple[int, ...], enabled: bool = True) -> ChannelState:
        """Read and, when needed, correct state in one BLE connection.

        The AXON is unreliable when a status connection is immediately followed
        by a second connection for the corrective write. Keeping both operations
        in one serialized session avoids that reconnect race without changing
        any commissioned protocol frames.
        """
        if not self.allow_control:
            raise PermissionError("AXON control is disabled pending commissioning")
        # Validate before opening Bluetooth.
        manual_channel_command(channels)
        async with self._lock:
            try:
                client = await self._ensure_client()
                current = await self._status_with_client(client)
                desired = ChannelState(tuple(channels), enabled and any(channels))
                if current != desired:
                    commands = (power_command(False),) if not desired.enabled else (
                        manual_channel_command(desired.channels), manual_mode_command(), power_command(True),
                    )
                    for command in commands:
                        await client.write_gatt_char(WRITE_UUID, command, response=False)
                        await asyncio.sleep(0.1)
                return desired if current != desired else current
            except Exception:
                await self._drop_client()
                raise

    async def set_power(self, enabled: bool) -> None:
        if not self.allow_control:
            raise PermissionError("AXON control is disabled pending commissioning")
        await self._exchange(power_command(enabled), None)

    async def set_channels(self, channels: tuple[int, ...], enabled: bool = True) -> None:
        if not self.allow_control:
            raise PermissionError("AXON control is disabled pending commissioning")
        if not enabled or not any(channels):
            await self._exchange(power_command(False), None)
            return
        await self._write_commands((manual_channel_command(channels), manual_mode_command(), power_command(True)))

    async def _write_commands(self, commands: tuple[bytes, ...]) -> None:
        async with self._lock:
            try:
                client = await self._ensure_client()
                for command in commands:
                    await client.write_gatt_char(WRITE_UUID, command, response=False)
                    await asyncio.sleep(0.1)
            except Exception:
                await self._drop_client()
                raise

    async def close(self) -> None:
        async with self._lock:
            await self._drop_client()


class CachedAxon:
    """Fast API facade backed by serialized background BLE polling."""

    def __init__(self, transport: AxonBLE, poll_seconds: float = 60.0) -> None:
        self.transport = transport
        self.allow_control = transport.allow_control
        self.poll_seconds = max(5.0, poll_seconds)
        self._state: ChannelState | None = None
        self._power_tasks: set[asyncio.Task[None]] = set()
        self.last_error: str | None = None
        self.last_success_at: float | None = None
        self.consecutive_failures = 0

    def _success(self) -> None:
        self.last_error = None
        self.last_success_at = time.time()
        self.consecutive_failures = 0

    def _failure(self, exc: Exception) -> None:
        self.last_error = type(exc).__name__
        self.consecutive_failures += 1

    async def refresh(self) -> ChannelState:
        try:
            self._state = await self.transport.status()
            self._success()
            return self._state
        except Exception as exc:
            self._failure(exc)
            raise

    async def status(self) -> ChannelState:
        if self._state is None:
            return await self.refresh()
        return self._state

    async def set_power(self, enabled: bool) -> ChannelState:
        channels = self._state.channels if self._state is not None else (0,) * 8
        self._state = ChannelState(channels, enabled)
        task = asyncio.create_task(self._apply_power(enabled))
        self._power_tasks.add(task)
        task.add_done_callback(self._power_tasks.discard)
        return self._state

    async def _apply_power(self, enabled: bool) -> None:
        try:
            await self.transport.set_power(enabled)
            self._success()
        except Exception as exc:
            self._state = None
            self._failure(exc)
            LOG.exception("AXON power command failed; cache invalidated for automatic reconciliation")

    async def set_channels(self, channels: tuple[int, ...], enabled: bool = True) -> ChannelState:
        # manual_channel_command performs strict validation before the
        # optimistic state is exposed to callers.
        command = manual_channel_command(channels)
        del command
        self._state = ChannelState(tuple(channels), enabled)
        task = asyncio.create_task(self._apply_channels(tuple(channels), enabled))
        self._power_tasks.add(task)
        task.add_done_callback(self._power_tasks.discard)
        return self._state

    async def reconcile(self, channels: tuple[int, ...], enabled: bool = True) -> ChannelState:
        """Synchronously repair automatic state, using one BLE session when supported."""
        manual_channel_command(channels)
        desired = ChannelState(tuple(channels), enabled and any(channels))
        if self._state == desired:
            return self._state
        try:
            reconcile = getattr(self.transport, "reconcile", None)
            if reconcile is not None:
                self._state = await reconcile(desired.channels, desired.enabled)
            else:  # Compatibility for alternate/mock transports.
                current = await self.transport.status()
                if current != desired:
                    await self.transport.set_channels(desired.channels, desired.enabled)
                self._state = desired
            self._success()
            return self._state
        except Exception as exc:
            self._state = None
            self._failure(exc)
            raise

    async def _apply_channels(self, channels: tuple[int, ...], enabled: bool) -> None:
        try:
            await self.transport.set_channels(channels, enabled)
            self._success()
        except Exception as exc:
            self._state = None
            self._failure(exc)
            LOG.exception("AXON channel command failed; cache invalidated for automatic reconciliation")

    async def poll_forever(self) -> None:
        while True:
            await asyncio.sleep(self.poll_seconds)
            try:
                await self.refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("AXON background status refresh failed")

    async def close(self) -> None:
        """Wait briefly for in-flight idempotent writes before clean shutdown."""
        tasks = tuple(self._power_tasks)
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
            except TimeoutError:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        close = getattr(self.transport, "close", None)
        if close is not None:
            await close()
