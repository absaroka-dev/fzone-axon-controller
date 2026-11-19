from __future__ import annotations

import argparse
import asyncio
import logging
import os

from .axon_api import AxonAPI, create_axon_server
from .axon_ble import AxonBLE, CachedAxon
from .axon_schedule import LightingSchedule, ScheduleRunner


async def run(address: str, socket_path: str, allow_control: bool, schedule_path: str | None = None) -> None:
    light = CachedAxon(AxonBLE(address, allow_control=allow_control))
    try:
        await light.refresh()
    except Exception:
        logging.exception("initial AXON status read failed; controller will retry in background")
    poller = asyncio.create_task(light.poll_forever())
    scheduler = None
    runner = None
    if schedule_path:
        schedule = LightingSchedule.load(schedule_path)
        runner = ScheduleRunner(schedule, light, os.getenv("AXON_LOCATION_STATE"))
        scheduler = asyncio.create_task(runner.run_forever())
        logging.info("AXON schedule loaded from %s (enabled=%s)", schedule_path, schedule.enabled)
    server = await create_axon_server(socket_path, AxonAPI(light, runner))
    logging.info("AXON controller listening on %s (control=%s)", socket_path, allow_control)
    try:
        async with server:
            await server.serve_forever()
    finally:
        poller.cancel()
        if scheduler:
            scheduler.cancel()
        await asyncio.gather(*(task for task in (poller, scheduler) if task), return_exceptions=True)
        await light.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default=os.getenv("AXON_BLE_ADDRESS"), required=os.getenv("AXON_BLE_ADDRESS") is None)
    parser.add_argument("--socket", default=os.getenv("AXON_SOCKET", "/run/axon-light/controller.sock"))
    parser.add_argument("--allow-control", action="store_true", default=os.getenv("AXON_ALLOW_CONTROL") == "1")
    parser.add_argument("--schedule", default=os.getenv("AXON_SCHEDULE"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(args.address, args.socket, args.allow_control, args.schedule))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
