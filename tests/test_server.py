"""The bridge, tested over a real loopback socket.

Everything else in this project is tested without a network, deliberately. This
file is the exception and it has to be: the thing under test is precisely the
part that binds a port, and a server tested through its own internals would be
verifying the design rather than the wiring.

The property that earns this file is the SCAN BOUNDARY. A controller can write a
coil on and back off inside one scan period, and the plant must see neither. A
simulation that let that through would be a simulation in which a whole class of
real control bug cannot be reproduced, which is a worse failure than being slow
or ugly, because it is invisible.
"""

from __future__ import annotations

import socket
import struct
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from vpc.cell import Cell
from vpc.modbus import (
    COIL_OFF,
    COIL_ON,
    Function,
    build_read_request,
    build_write_single_coil,
)
from vpc.process_image import Coil, Discrete
from vpc.server import CellServer


@contextmanager
def running() -> Iterator[CellServer]:
    """A server bound to an OS chosen port, closed however the test ends."""
    with CellServer(Cell(), period_s=0) as server:
        yield server


@contextmanager
def connected(server: CellServer) -> Iterator[socket.socket]:
    """A client with the accept already serviced, so tests need no sleeps."""
    client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    server.poll(1.0)
    try:
        yield client
    finally:
        client.close()


def read_exactly(client: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = client.recv(count - len(data))
        assert chunk, "connection closed before the whole response arrived"
        data += chunk
    return data


def ask(server: CellServer, client: socket.socket, frame: bytes,
        expect: int) -> bytes:
    client.sendall(frame)
    server.poll(1.0)
    return read_exactly(client, expect)


#: A read of n bits answers with the MBAP header, function, byte count and data.
BITS_RESPONSE = 7 + 2 + 1
#: A coil write is echoed back verbatim.
COIL_ECHO = 7 + 5


# --- the scan boundary, which is why this server is single threaded ----------
@pytest.mark.verifies("SR-12")
def test_a_coil_written_and_withdrawn_inside_one_scan_never_reaches_the_plant() -> None:
    """The property the whole design exists to protect.

    A controller asks for the conveyor and changes its mind before the boundary.
    On a real machine the plant sees the output the program held AT the boundary
    and nothing in between, so the line must not have moved at all.
    """
    with running() as server, connected(server) as client:
        ask(server, client, build_write_single_coil(Coil.CONVEYOR_RUN, True), COIL_ECHO)
        ask(server, client, build_write_single_coil(Coil.CONVEYOR_RUN, False), COIL_ECHO)
        server.tick()

    assert server.cell.at_filler is None, "the line moved on a command that was withdrawn"
    assert server.cell.next_serial == 1


def test_a_coil_still_set_at_the_boundary_does_reach_the_plant() -> None:
    """The control case. Without it the test above passes on a broken server."""
    with running() as server, connected(server) as client:
        ask(server, client, build_write_single_coil(Coil.CONVEYOR_RUN, True), COIL_ECHO)
        server.tick()

    assert server.cell.at_filler is not None
    assert server.scans == 1


def test_an_output_holds_its_value_across_scans() -> None:
    """A PLC output is a level, not a pulse.

    If the staged image were rebuilt empty each scan, a program would have to
    rewrite every coil every scan to keep a motor turning, which is not how any
    controller on earth behaves.
    """
    with running() as server, connected(server) as client:
        ask(server, client, build_write_single_coil(Coil.CONVEYOR_RUN, True), COIL_ECHO)
        server.tick()
        server.tick()

    assert server.staged.coils[Coil.CONVEYOR_RUN] is True


def test_a_master_reads_back_a_coil_it_just_wrote_without_waiting_for_a_scan() -> None:
    """Two freshness rules on one image, and both are what a controller expects.

    Coils read back immediately because they are the master's own writes.
    Inputs are as of the last completed scan because that is when the plant
    published them.
    """
    with running() as server, connected(server) as client:
        ask(server, client, build_write_single_coil(Coil.FILLER_DOSE, True), COIL_ECHO)
        response = ask(server, client,
                       build_read_request(Function.READ_COILS, Coil.FILLER_DOSE, 1),
                       BITS_RESPONSE)

    assert response[-1] == 0b1


# --- the plant is visible through the socket ---------------------------------
def test_inputs_read_as_de_energised_before_the_first_scan() -> None:
    """The correct default, and worth pinning.

    Before a scan has run the plant has published nothing, so SAFETY_OK reads
    low and the controller sees a machine it may not move. A server that
    initialised its inputs to a cheerful default would hand a controller a
    healthy safety signal from a plant that had not yet run.
    """
    with running() as server, connected(server) as client:
        response = ask(server, client,
                       build_read_request(Function.READ_DISCRETE_INPUTS,
                                          Discrete.SAFETY_OK, 1),
                       BITS_RESPONSE)

    assert response[-1] == 0b0


def test_the_plant_publishes_its_state_at_the_scan_boundary() -> None:
    with running() as server, connected(server) as client:
        server.tick()
        response = ask(server, client,
                       build_read_request(Function.READ_DISCRETE_INPUTS,
                                          Discrete.SAFETY_OK, 1),
                       BITS_RESPONSE)

    assert response[-1] == 0b1


def test_the_safety_channel_still_outranks_the_network() -> None:
    """A guard open over the wire is not a thing the master can talk its way out of."""
    with running() as server, connected(server) as client:
        server.cell.open_guard()
        ask(server, client, build_write_single_coil(Coil.CONVEYOR_RUN, True), COIL_ECHO)
        server.tick()

    assert server.cell.at_filler is None, "the line moved with torque withheld"


# --- the stream, which is where a socket differs from a function -------------
def test_two_frames_in_one_read_are_both_answered() -> None:
    """TCP is a stream and a master may pipeline. One read, two frames, two replies."""
    with running() as server, connected(server) as client:
        client.sendall(build_write_single_coil(Coil.CONVEYOR_RUN, True)
                       + build_write_single_coil(Coil.FILLER_DOSE, True))
        server.poll(1.0)
        responses = read_exactly(client, COIL_ECHO * 2)

    assert struct.unpack(">H", responses[8:10])[0] == Coil.CONVEYOR_RUN
    assert struct.unpack(">H", responses[COIL_ECHO + 8:COIL_ECHO + 10])[0] == Coil.FILLER_DOSE
    assert server.staged.coils[Coil.FILLER_DOSE] is True


def test_a_frame_split_across_two_reads_is_answered_once_it_is_whole() -> None:
    """The half frame case, which is the one that only ever shows up in production."""
    frame = build_write_single_coil(Coil.CONVEYOR_RUN, True)
    with running() as server, connected(server) as client:
        client.sendall(frame[:5])
        server.poll(1.0)
        assert server.staged.coils[Coil.CONVEYOR_RUN] is False, "acted on half a frame"

        client.sendall(frame[5:])
        server.poll(1.0)
        assert read_exactly(client, COIL_ECHO)
        assert server.staged.coils[Coil.CONVEYOR_RUN] is True


def test_a_malformed_frame_closes_the_connection_rather_than_being_answered() -> None:
    """A Modbus exception says no. Silence says this was never Modbus.

    The frame below declares a length no Modbus frame can have. Answering it
    politely would be pretending to have understood it, and waiting for the
    bytes it promised would hang the connection open forever.
    """
    with running() as server, connected(server) as client:
        client.sendall(struct.pack(">HHH", 1, 0, 60000) + b"\x01")
        server.poll(1.0)

        assert server._streams == {}
        assert client.recv(16) == b""


def test_a_frame_that_is_not_modbus_at_all_closes_the_connection() -> None:
    """Right length, wrong protocol id. `parse` refuses it and the reply is a hangup."""
    with running() as server, connected(server) as client:
        client.sendall(struct.pack(">HHHB", 1, 99, 6, 1) + struct.pack(">BHH", 1, 0, 1))
        server.poll(1.0)

        assert server._streams == {}
        assert client.recv(16) == b""


def test_an_exception_response_keeps_the_connection_open() -> None:
    """The contrast that makes the two tests above mean something.

    A read past the end of the address space is a well formed question with the
    answer no. The device says so and stays on the line.
    """
    with running() as server, connected(server) as client:
        response = ask(server, client,
                       build_read_request(Function.READ_DISCRETE_INPUTS, 0, 999),
                       9)
        assert response[7] == Function.READ_DISCRETE_INPUTS | 0x80
        assert server._streams != {}


def test_a_client_that_hangs_up_is_forgotten() -> None:
    with running() as server:
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        server.poll(1.0)
        assert server._streams != {}

        client.close()
        server.poll(1.0)
        assert server._streams == {}


# --- the loop ----------------------------------------------------------------
def test_run_scans_the_plant_the_requested_number_of_times() -> None:
    with running() as server:
        server.run(scans=4)
    assert server.scans == 4
    assert server.cell.scans == 4


def test_run_paces_itself_on_a_monotonic_deadline() -> None:
    """Not a sleep per scan, which would let network work push the period out.

    Only the lower bound is asserted. An upper bound would be a test of how busy
    the machine running it happens to be.
    """
    with CellServer(Cell(), period_s=0.02) as server:
        started = time.monotonic()
        server.run(scans=3)
        elapsed = time.monotonic() - started

    assert server.scans == 3
    assert elapsed >= 0.04


def test_run_serves_the_network_between_scans() -> None:
    """The loop is a server first. A scan period is time to answer in, not to sleep in."""
    with CellServer(Cell(), period_s=0.05) as server:
        client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        client.sendall(build_write_single_coil(Coil.CONVEYOR_RUN, True))
        server.run(scans=2)
        client.close()

    assert server.cell.at_filler is not None, "a write during the period was not served"


# --- lifecycle ---------------------------------------------------------------
def test_the_port_is_the_one_the_os_chose() -> None:
    with running() as server:
        assert server.port > 0


def test_close_drops_live_connections() -> None:
    server = CellServer(Cell(), period_s=0)
    client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    server.poll(1.0)
    assert server._streams != {}

    server.close()
    assert server._streams == {}
    client.close()


def test_the_port_is_released_so_a_restart_can_rebind() -> None:
    """Restarting a simulation while debugging a control program is normal."""
    with running() as first:
        port = first.port
    with CellServer(Cell(), port=port, period_s=0) as second:
        assert second.port == port


@pytest.mark.parametrize("value", [COIL_ON, COIL_OFF])
def test_the_write_echo_is_the_value_the_master_sent(value: int) -> None:
    """Modbus echoes a coil write verbatim, and masters check it."""
    frame = struct.pack(">HHHB", 1, 0, 6, 1) + struct.pack(
        ">BHH", Function.WRITE_SINGLE_COIL, Coil.CONVEYOR_RUN, value)
    with running() as server, connected(server) as client:
        response = ask(server, client, frame, COIL_ECHO)

    assert struct.unpack(">H", response[-2:])[0] == value


# --- the peer going away rudely ----------------------------------------------
#: The `linger` struct is {u_short l_onoff, u_short l_linger} on Windows and
#: {int l_onoff, int l_linger} on Linux, so the OPTION is portable and the
#: PACKING is not. Getting this wrong gives EINVAL from setsockopt on the other
#: platform, which is how these two tests passed on Windows and failed in CI.
LINGER = struct.pack("hh" if sys.platform == "win32" else "ii", 1, 0)


def hard_reset_client(server: CellServer) -> socket.socket:
    """A client whose close sends RST rather than FIN.

    SO_LINGER with a zero timeout is how you make a close abrupt, which is what
    a rebooting controller or a pulled cable looks like from the other end. A
    graceful close is already covered; this is the case that killed the plant.
    """
    client = socket.create_connection(("127.0.0.1", server.port), timeout=5)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, LINGER)
    server.poll(1.0)
    return client


def test_a_connection_reset_drops_the_peer_and_does_not_kill_the_plant() -> None:
    """Found in production, not in review.

    A CODESYS runtime logged out after 101,000 scans of correct operation and
    reset the connection instead of closing it. The exception escaped `run` and
    took the whole plant with it. A simulated device that dies when its master
    disconnects cannot commission anything, because downloading repeatedly is
    the first thing anyone does with a controller.
    """
    with running() as server:
        client = hard_reset_client(server)
        client.sendall(build_write_single_coil(Coil.CONVEYOR_RUN, True))
        server.poll(1.0)
        client.close()          # sends RST, not FIN

        server.poll(1.0)        # must not raise
        server.tick()

        assert server._streams == {}, "the reset peer was not dropped"
        assert server.scans == 1, "the plant stopped scanning"


def test_the_plant_keeps_serving_after_a_peer_resets() -> None:
    """The property that actually matters: the next master still gets served."""
    with running() as server:
        first = hard_reset_client(server)
        first.close()
        server.poll(1.0)

        with connected(server) as second:
            response = ask(server, second,
                           build_write_single_coil(Coil.CONVEYOR_RUN, True),
                           COIL_ECHO)
            assert response
            assert server.staged.coils[Coil.CONVEYOR_RUN] is True
