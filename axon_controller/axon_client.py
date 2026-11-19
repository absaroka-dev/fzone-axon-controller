from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex


async def request(path: str, body: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(path)
    try:
        writer.write(json.dumps(body, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        raw = await reader.readline()
        if not raw or len(raw) > 4096:
            raise RuntimeError("invalid AXON controller response")
        return json.loads(raw)
    finally:
        writer.close()
        await writer.wait_closed()


def _command(argv: list[str]) -> dict[str, object]:
    if argv == ["health"] or argv == ["status"] or argv == ["live-status"]:
        return {"operation": "live_status" if argv[0] == "live-status" else argv[0]}
    if argv == ["set-power", "on"]:
        return {"operation": "set_power", "enabled": True}
    if argv == ["set-power", "off"]:
        return {"operation": "set_power", "enabled": False}
    if len(argv) == 2 and argv[0] == "set-channels":
        try:
            channels = [int(value) for value in argv[1].split(",")]
        except ValueError as exc:
            raise ValueError("channels must be comma-separated integers") from exc
        return {"operation": "set_channels", "channels": channels}
    if len(argv) == 3 and argv[0] == "override":
        try:
            duration = int(argv[1])
            channels = [int(value) for value in argv[2].split(",")]
        except ValueError as exc:
            raise ValueError("override requires integer seconds and comma-separated channels") from exc
        return {"operation": "override", "duration_seconds": duration, "channels": channels}
    if argv == ["resume-solar"]:
        return {"operation": "resume_schedule"}
    if len(argv) == 5 and argv[0] == "set-location":
        try:
            latitude, longitude = float(argv[2]), float(argv[3])
        except ValueError as exc:
            raise ValueError("set-location coordinates must be numeric") from exc
        return {
            "operation": "set_location", "postal_code": argv[1],
            "latitude": latitude, "longitude": longitude, "timezone": argv[4],
        }
    raise ValueError("allowed commands: health, status, live-status, set-power on|off, set-channels C1,...,C8, override SECONDS C1,...,C8, resume-solar, set-location ZIP LAT LON TIMEZONE")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default=os.getenv("AXON_SOCKET", "/run/axon-light/controller.sock"))
    parser.add_argument("command", nargs="*")
    args = parser.parse_args()
    command = args.command
    if not command and os.getenv("SSH_ORIGINAL_COMMAND"):
        command = shlex.split(os.environ["SSH_ORIGINAL_COMMAND"])
    try:
        result = asyncio.run(request(args.socket, _command(command)))
    except Exception as exc:
        result = {"status": 500, "ok": False, "error": str(exc)}
    print(json.dumps(result, separators=(",", ":")))
    raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
