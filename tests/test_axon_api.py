import unittest
import asyncio

from axon_controller.axon_api import AxonAPI
from axon_controller.axon_ble import CachedAxon
from axon_controller.axon_protocol import ChannelState


class FakeLight:
    def __init__(self, allow_control=False):
        self.allow_control = allow_control
        self.state = ChannelState((25, 50, 75, 100, 0, 0, 0, 0), True)

    async def status(self):
        return self.state

    async def refresh(self):
        return self.state

    async def set_power(self, enabled):
        if not self.allow_control:
            raise PermissionError("AXON control is disabled pending commissioning")
        self.state = ChannelState(self.state.channels, enabled)

    async def set_channels(self, channels, enabled=True):
        if not self.allow_control:
            raise PermissionError("AXON control is disabled pending commissioning")
        if len(channels) != 8 or any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100 for value in channels):
            raise ValueError("invalid channels")
        self.state = ChannelState(tuple(channels), enabled)
        return self.state


class FakeScheduler:
    def __init__(self, light):
        self.light = light
        self.active = False

    def status(self):
        return {"schedule_enabled": True, "override_active": self.active, "override_until": None, "scheduled_channels": [0] * 8}

    async def set_override(self, channels, duration):
        if not 60 <= duration <= 14400:
            raise ValueError("bad duration")
        self.active = True
        return await self.light.set_channels(channels, any(channels))

    async def resume(self):
        self.active = False
        return await self.light.set_channels((0,) * 8, False)

class AxonAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_health_reports_control_hold(self):
        light = FakeLight()
        light.refresh_count = 0
        original = light.refresh

        async def counted_refresh():
            light.refresh_count += 1
            return await original()

        light.refresh = counted_refresh
        status, body = await AxonAPI(light).dispatch({"operation": "health"})
        self.assertEqual(status, 200)
        self.assertFalse(body["control_enabled"])
        self.assertEqual(light.refresh_count, 0)
        self.assertIn("connectivity", body)

    async def test_status_maps_channels(self):
        status, body = await AxonAPI(FakeLight()).dispatch({"operation": "status"})
        self.assertEqual(status, 200)
        self.assertEqual(body["brightness"], 100)
        self.assertEqual(body["channels"][:4], [25, 50, 75, 100])
        self.assertIn("connectivity", body)

    async def test_live_status_requires_refresh(self):
        light = FakeLight()
        light.refresh_count = 0
        original = light.refresh

        async def counted_refresh():
            light.refresh_count += 1
            return await original()

        light.refresh = counted_refresh
        status, body = await AxonAPI(light).dispatch({"operation": "live_status"})
        self.assertEqual(status, 200)
        self.assertTrue(body["live"])
        self.assertEqual(light.refresh_count, 1)

    async def test_control_is_disabled_by_default(self):
        status, _ = await AxonAPI(FakeLight()).dispatch({"operation": "set_power", "enabled": False})
        self.assertEqual(status, 403)

    async def test_rejects_arbitrary_parameters(self):
        api = AxonAPI(FakeLight(True))
        self.assertEqual((await api.dispatch({"operation": "status", "extra": True}))[0], 400)
        self.assertEqual((await api.dispatch({"operation": "set_power", "enabled": 1}))[0], 400)

    async def test_sets_all_channels(self):
        api = AxonAPI(FakeLight(True))
        values = [10, 20, 30, 40, 0, 0, 0, 0]
        status, body = await api.dispatch({"operation": "set_channels", "channels": values})
        self.assertEqual(status, 200)
        self.assertEqual(body["channels"], values)
        self.assertTrue(body["enabled"])

    async def test_rejects_invalid_channels(self):
        api = AxonAPI(FakeLight(True))
        self.assertEqual((await api.dispatch({"operation": "set_channels", "channels": [1, 2]}))[0], 400)
        self.assertEqual((await api.dispatch({"operation": "set_channels", "channels": [True] + [0] * 7}))[0], 400)

    async def test_cache_avoids_repeated_transport_reads(self):
        transport = FakeLight(True)
        transport.reads = 0
        original = transport.status
        async def counted():
            transport.reads += 1
            return await original()
        transport.status = counted
        cached = CachedAxon(transport)
        await cached.refresh()
        await cached.status()
        await cached.status()
        self.assertEqual(transport.reads, 1)

    async def test_power_acknowledges_before_slow_transport(self):
        transport = FakeLight(True)
        gate = asyncio.Event()
        async def slow_power(enabled):
            await gate.wait()
        transport.set_power = slow_power
        cached = CachedAxon(transport)
        await cached.refresh()
        state = await asyncio.wait_for(cached.set_power(False), timeout=0.05)
        self.assertFalse(state.enabled)
        gate.set()
        await asyncio.gather(*cached._power_tasks)

    async def test_failed_command_invalidates_cache_for_reconnection(self):
        transport = FakeLight(True)
        attempts = 0

        async def fail_once(channels, enabled=True):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("Bluetooth disconnected")
            transport.state = ChannelState(tuple(channels), enabled)

        transport.set_channels = fail_once
        cached = CachedAxon(transport)
        await cached.refresh()
        await cached.set_channels((10, 10, 10, 10, 0, 0, 0, 0), True)
        await asyncio.gather(*cached._power_tasks)
        self.assertIsNone(cached._state)
        self.assertEqual(cached.last_error, "OSError")
        # The next read reconnects through the transport rather than trusting
        # the failed optimistic state.
        state = await cached.status()
        self.assertEqual(state, transport.state)
        await cached.set_channels((8, 8, 8, 8, 0, 0, 0, 0), True)
        await asyncio.gather(*cached._power_tasks)
        self.assertIsNone(cached.last_error)

    async def test_reconcile_repairs_state_with_one_transport_operation(self):
        transport = FakeLight(True)
        transport.state = ChannelState((0,) * 8, False)
        calls = []

        async def reconcile(channels, enabled=True):
            calls.append((channels, enabled))
            transport.state = ChannelState(tuple(channels), enabled)
            return transport.state

        transport.reconcile = reconcile
        cached = CachedAxon(transport)
        desired = (1, 0, 0, 0, 0, 0, 0, 0)
        state = await cached.reconcile(desired, True)
        self.assertEqual(state, ChannelState(desired, True))
        self.assertEqual(calls, [(desired, True)])
        # A matching cache avoids another BLE transaction; the background poll
        # remains responsible for discovering later external drift.
        await cached.reconcile(desired, True)
        self.assertEqual(len(calls), 1)

    async def test_timed_override_and_resume_api(self):
        light = FakeLight(True)
        scheduler = FakeScheduler(light)
        api = AxonAPI(light, scheduler)
        status, body = await api.dispatch({
            "operation": "override", "duration_seconds": 900,
            "channels": [5, 4, 3, 2, 0, 0, 0, 0],
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["automation"]["override_active"])
        status, body = await api.dispatch({"operation": "resume_schedule"})
        self.assertEqual(status, 200)
        self.assertFalse(body["automation"]["override_active"])
