"""Run the plant and expose its PackTags over OPC UA, both at once.

    uv run python scripts/serve_opcua.py

Modbus TCP on 502 for the PLC, OPC UA on 4840 for the supervisor. That split is
the real one: the controller talks to the machine over fieldbus at scan rate,
and anything above it talks to the controller over OPC UA at human rate. A cell
that exposed one interface and called it both would be a demonstration rather
than an architecture.

Connect with UaExpert or any client using:

    endpoint  opc.tcp://<host>:4840/vpc/
    security  Basic256Sha256, Sign & Encrypt
    user      supervisor
    password  REDACTED-CREDENTIAL-PURGED

Anonymous is refused and there is no None policy offered, so a client that
cannot do certificates cannot connect. That is the point. Trust the server
certificate in `certs/` on first connection.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vpc.cell import Cell  # noqa: E402
from vpc.opcua import (  # noqa: E402
    DEFAULT_ENDPOINT,
    build_address_space,
    check_names_agree,
    make_server,
    publish,
)
from vpc.packtags import PackTags, StopReason  # noqa: E402
from vpc.process_image import Discrete  # noqa: E402
from vpc.scenario import ReferenceController  # noqa: E402
from vpc.server import DEFAULT_PERIOD_S, CellServer  # noqa: E402


def stop_reason(cell: Cell, controller: ReferenceController) -> StopReason:
    """Why the cell is not producing, as a supervisor needs it.

    Ordered by what a person would want told first. A guard open is the reason
    even though it also removed torque, because "somebody opened the door" is
    actionable and "torque is unavailable" is a symptom of it.
    """
    if not cell.guard_closed:
        return StopReason.GUARD_OPEN
    if not cell.torque_available:
        return StopReason.SAFETY_CHANNEL
    if cell.infeed_starved:
        return StopReason.STARVED
    if not controller.wants_to_run:
        return StopReason.OPERATOR
    return StopReason.NONE


async def main() -> int:
    check_names_agree()

    cell = Cell()
    plant = CellServer(cell, host="0.0.0.0", port=502)  # noqa: S104
    controller = ReferenceController()
    controller.wants_to_run = True
    tags = PackTags()

    server = await make_server(DEFAULT_ENDPOINT)
    nodes = await build_address_space(server, tags)

    print(f"plant   modbus tcp on 502, {plant.period_s}s per scan")
    print(f"tags    {DEFAULT_ENDPOINT} (Basic256Sha256 sign+encrypt, "
          f"user 'supervisor')")

    async with server:
        while True:
            # The plant runs in its own scan loop. The supervisory layer samples
            # it, and deliberately does NOT drive it: OPC UA is above the
            # control loop, not inside it, and a supervisor that could stall the
            # scan by being slow would be a supervisor that can stop a machine.
            plant.run(scans=int(1 / DEFAULT_PERIOD_S))
            image = plant.image
            tags.update(
                controller.machine.state,
                stop_reason(cell, controller),
                produced=cell.produced,
                rejected=cell.rejected,
                blocked=image.discretes[Discrete.CAPPER_BUSY],
                starved=cell.infeed_starved,
                speed=100 if cell.torque_available else 0,
            )
            await publish(nodes, tags)
            await asyncio.sleep(0)


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
