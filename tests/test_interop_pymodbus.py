"""The plant, driven by a client that knows nothing about this repository.

Every other test here is written against the same understanding of Modbus that
the implementation has. If that understanding is wrong, they all agree with each
other and the device still fails the first time a real master talks to it. This
file exists to make that impossible to miss: `pymodbus` was written by other
people from the specification, and it either drives this plant or it does not.

The distinction is worth stating because it is easy to lose. A test suite proves
SELF CONSISTENCY. Interoperability is a different claim and needs a different
witness.

It earned itself before it was a test. Run by hand during bring-up, it confirmed
the wire format was right at a point when the CODESYS master was failing, which
localised the fault to the master's configuration rather than the plant. Without
it that afternoon would have been spent looking in the wrong place.
"""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager

import pytest

from vpc.cell import Cell
from vpc.process_image import Coil, Discrete, InputRegister
from vpc.server import CellServer

pymodbus_client = pytest.importorskip("pymodbus.client",
                                      reason="pymodbus is a dev dependency")
ModbusTcpClient = pymodbus_client.ModbusTcpClient

#: The unit id the plant answers on, matching the CODESYS slave configuration.
UNIT = 1


@contextmanager
def serving(period_s: float = 0.005) -> Iterator[CellServer]:
    """A plant on a background thread, stopped cleanly however the test ends.

    A thread rather than `run`, because the test needs to stop it on demand and
    `run` is deliberately finite rather than interruptible. The scan boundary
    property is unaffected: the loop still polls and ticks in one thread, which
    is the whole point of the design.
    """
    server = CellServer(Cell(), period_s=period_s)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            server.poll(0.01)
            server.tick()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        stop.set()
        thread.join(timeout=5)
        server.close()


@contextmanager
def master(server: CellServer) -> Iterator[object]:
    client = ModbusTcpClient("127.0.0.1", port=server.port, timeout=5)
    assert client.connect(), "pymodbus could not reach the plant"
    with closing(client):
        yield client


def device_kwarg(client: object) -> dict[str, int]:
    """pymodbus renamed this across versions: unit, then slave, then device_id.

    Discovered rather than pinned, so a dependency bump does not present as a
    protocol failure. That misdiagnosis is exactly what this file exists to
    prevent, and it would be embarrassing to cause it here.
    """
    import inspect
    parameters = inspect.signature(client.read_discrete_inputs).parameters
    for name in ("device_id", "slave", "unit"):
        if name in parameters:
            return {name: UNIT}
    return {}


def test_a_third_party_master_reads_the_plant_state() -> None:
    with serving() as server, master(server) as client:
        kwargs = device_kwarg(client)

        result = client.read_discrete_inputs(0, count=len(Discrete), **kwargs)

        assert not result.isError(), result
        assert len(result.bits) >= len(Discrete)


def test_a_third_party_master_commands_the_plant_and_reads_it_back() -> None:
    """Write then read, which is what a master does every scan."""
    with serving() as server, master(server) as client:
        kwargs = device_kwarg(client)

        written = client.write_coil(int(Coil.CONVEYOR_RUN), True, **kwargs)
        assert not written.isError(), written

        read = client.read_coils(0, count=len(Coil), **kwargs)
        assert not read.isError(), read
        assert read.bits[int(Coil.CONVEYOR_RUN)] is True


def test_a_third_party_master_can_write_every_coil_in_one_frame() -> None:
    """Function 15, which is how the real master is configured."""
    with serving() as server, master(server) as client:
        kwargs = device_kwarg(client)

        result = client.write_coils(0, [True] * len(Coil), **kwargs)

        assert not result.isError(), result
        assert all(server.staged.coils.values())


def test_the_plant_scans_while_a_third_party_master_is_connected() -> None:
    """SCAN_COUNT advancing is the check that separates a live plant from a
    stale snapshot, and it is the first thing to look at during bring-up."""
    with serving() as server, master(server) as client:
        kwargs = device_kwarg(client)

        first = client.read_input_registers(0, count=len(InputRegister), **kwargs)
        assert not first.isError(), first
        before = first.registers[int(InputRegister.SCAN_COUNT)]

        deadline = threading.Event()
        deadline.wait(0.3)

        second = client.read_input_registers(0, count=len(InputRegister), **kwargs)
        assert not second.isError(), second
        after = second.registers[int(InputRegister.SCAN_COUNT)]

    assert after > before, "the plant is not scanning"


def test_an_out_of_range_read_comes_back_as_an_exception_not_a_hangup() -> None:
    """A Modbus exception is a conversation in which the device says no.

    The connection surviving it is the half that matters: a device that hung up
    on every bad request would drop a master that briefly misconfigured one
    channel, and the symptom would look like a network fault.
    """
    with serving() as server, master(server) as client:
        kwargs = device_kwarg(client)

        refused = client.read_discrete_inputs(0, count=999, **kwargs)
        assert refused.isError(), "an out of range read was accepted"

        still_there = client.read_discrete_inputs(0, count=1, **kwargs)
        assert not still_there.isError(), (
            "the connection did not survive an exception response"
        )


def test_the_plant_survives_a_master_that_resets_the_connection() -> None:
    """The defect that killed the plant in use, reproduced with a real client.

    pymodbus closing normally is a graceful FIN. This forces the rude case that
    a CODESYS logout produced, and then checks a fresh master is still served.
    """
    with serving() as server:
        rude = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        rude.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        _linger())
        rude.close()

        with master(server) as client:
            kwargs = device_kwarg(client)
            result = client.read_discrete_inputs(0, count=1, **kwargs)

        assert not result.isError(), "the plant stopped serving after a reset"


def _linger() -> bytes:
    """Packed the way this platform's struct linger is laid out.

    Two u_shorts on Windows and two ints on Linux, which is how the original
    version of this passed locally and failed in CI.
    """
    import struct
    import sys
    return struct.pack("hh" if sys.platform == "win32" else "ii", 1, 0)
