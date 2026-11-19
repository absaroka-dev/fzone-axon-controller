import unittest

from axon_controller.axon_ble import AxonBLE


class FakeClient:
    def __init__(self):
        self.is_connected = False
        self.connects = 0
        self.disconnects = 0
        self.writes = []
        self.callback = None

    def set_disconnected_callback(self, callback):
        self.callback = callback

    async def connect(self):
        self.connects += 1
        self.is_connected = True

    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False

    async def start_notify(self, _uuid, callback):
        self.callback = callback

    async def write_gatt_char(self, _uuid, value, response=False):
        self.writes.append((value, response))


class AxonBLEConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuses_one_connection_for_repeated_commands(self):
        client = FakeClient()
        transport = AxonBLE(
            "AA:BB:CC:DD:EE:FF",
            allow_control=True,
            client_factory=lambda *_args, **_kwargs: client,
        )

        await transport.set_power(True)
        await transport.set_power(False)

        self.assertEqual(client.connects, 1)
        self.assertEqual(len(client.writes), 2)
        self.assertEqual(client.disconnects, 0)

        await transport.close()
        self.assertEqual(client.disconnects, 1)


if __name__ == "__main__":
    unittest.main()
