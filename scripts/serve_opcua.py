"""Run the plant and expose its PackTags over OPC UA, both at once.

    uv run python scripts/serve_opcua.py

STANDALONE, and that word is load bearing. The reference controller drives the
plant here, so every tag published is live. It does NOT serve Modbus, because
two controllers cannot drive one plant and the first version of this script
quietly tried: it served Modbus for a real PLC, published the reference
controller's PackML state, and never scanned that controller. The state read
Aborted forever while the plant ran perfectly, which is the worst kind of wrong
because everything else on the screen was right.

With a real PLC the arrangement differs, and the difference is the point. Run
`python -m vpc.server` for Modbus on 502, attach CODESYS, and the PackML state
then lives IN THE PLC where the program is. A supervisory layer cannot invent it
from outside, which is exactly why real PackTags is implemented in the
controller and published by it. This script demonstrates the tag structure and
the security; a real line puts the same structure one layer down.

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
import threading
import time
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
from vpc.process_image import Discrete, ProcessImage  # noqa: E402
from vpc.scenario import ReferenceController  # noqa: E402


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
    controller = ReferenceController()
    controller.wants_to_run = True
    tags = PackTags()
    image = ProcessImage()

    server = await make_server(DEFAULT_ENDPOINT)
    nodes = await build_address_space(server, tags)

    print(f"tags    {DEFAULT_ENDPOINT} (Basic256Sha256 sign+encrypt, "
          f"user 'supervisor')")

    # The plant scans on its OWN thread. This is not tidiness, it is the same
    # principle stated twice and violated once: OPC UA sits ABOVE the control
    # loop, not inside it. The first version ran plant.run() in the event loop,
    # which starves it for a full second at a time, and the symptom was an OPC
    # UA client timing out during the handshake against a server that was
    # configured perfectly. A supervisor that can stall the scan is a supervisor
    # that can stop a machine, and a scan loop that can stall the supervisor is
    # the same fault pointing the other way.
    stop = threading.Event()

    def scan_forever() -> None:
        nonlocal image
        while not stop.is_set():
            for _ in range(20):
                image = cell.scan(controller.scan(image))
            time.sleep(0.02)

    threading.Thread(target=scan_forever, daemon=True).start()

    async with server:
        while True:
            # Reading counters while the plant thread advances them is a benign
            # race: each is a single integer and the supervisory view is a
            # sample, not a transaction. A supervisor that needed a consistent
            # snapshot across tags would need the plant to publish one, which is
            # what a real PackTags implementation does at the scan boundary.
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
            await asyncio.sleep(0.2)


if __name__ == "__main__":  # pragma: no cover
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
