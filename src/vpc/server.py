"""The bridge: a Modbus TCP server that is also the plant's scan loop.

Everything else in this project was built so that this file could be small. The
protocol is pure functions in `modbus.py`, the plant is a state machine in
`cell.py`, and the address map is an enum in `process_image.py`. What was
missing was the thing that binds a port, and until it existed the PLC and the
plant could not exchange a single bit.

WHO IS THE MASTER. The PLC is the client and the plant is the server, which is
not an arbitrary choice: the PLC writes coils and reads discrete inputs and
input registers, and those directions are what the Modbus address spaces MEAN.
A remote IO drop is a server, and to the controller this simulation is a remote
IO drop that happens to think.

ONE THREAD, AND THAT IS THE WHOLE DESIGN. Sockets and the scan loop share the
process image, so the obvious build is a socket thread plus a scan thread plus a
lock, and it would be wrong. Not because of the race, which a lock does fix, but
because a lock still lets a coil write land in the MIDDLE of a scan, and the
entire point of a process image is that it cannot. So the server selects on its
sockets with a timeout that expires exactly at the next scan boundary. There is
one thread, no lock can be forgotten, and the ordering is deterministic, which
means a failure here reduces to a reproduction rather than to a rerun.

THE SCAN BOUNDARY, WHICH IS THE PROPERTY WORTH PROTECTING. Network writes land
in a STAGED image. The plant never sees the staged image until `tick()`, when it
becomes the input to one scan and the result becomes the new published image.
So a controller that writes CONVEYOR_RUN and then immediately writes it back off
within one scan period has, from the plant's point of view, done nothing at all.
That is exactly what happens on a real machine and it is the class of bug that a
callback based simulation makes invisible.

Reads are served from the staged image on purpose, so a master reading back a
coil sees what it just wrote, while the inputs it reads are the ones published
at the last completed scan. Those are two different freshness rules on one
image and they are both what a controller expects.
"""

from __future__ import annotations

import selectors
import socket
import time
from types import TracebackType

from vpc.cell import Cell
from vpc.modbus import MalformedFrame, handle, parse, take_frame
from vpc.process_image import ProcessImage

#: Seconds between scans. A real cell runs a task at a few milliseconds; this is
#: slower on purpose so a human watching a watch window can see the line move.
#: It is a simulation parameter and nothing in the control program may depend on
#: it, which is why the plant counts scans and never seconds.
DEFAULT_PERIOD_S = 0.05

#: Bytes to take from a socket at a time. Frames are at most 260 bytes, so this
#: takes several at once without ever needing a second pass for one frame.
READ_SIZE = 4096


class CellServer:
    """A Modbus TCP server in front of the plant, scanning on a fixed period.

    Constructed bound and listening, so a test can read `port` and connect
    without a sleep. Port 0 asks the OS for a free one, which is what makes the
    tests safe to run in parallel and on a machine that already has something on
    502.
    """

    def __init__(self, cell: Cell, host: str = "127.0.0.1", port: int = 0,
                 period_s: float = DEFAULT_PERIOD_S) -> None:
        self.cell = cell
        self.period_s = period_s
        #: What the last completed scan published. The plant's view.
        self.image = ProcessImage()
        #: What the network has written since. The controller's view.
        self.staged = self.image.copy()
        self.scans = 0

        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Without this, a restart inside the TIME_WAIT window fails to bind, and
        # restarting a simulation while debugging a control program is not an
        # unusual thing to want to do.
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, port))
        self._listener.listen()
        self._listener.setblocking(False)

        self._selector = selectors.DefaultSelector()
        self._selector.register(self._listener, selectors.EVENT_READ)
        self._streams: dict[socket.socket, bytes] = {}

    @property
    def port(self) -> int:
        """The port actually bound, which is the interesting one when it was 0."""
        return int(self._listener.getsockname()[1])

    # ---- the scan boundary --------------------------------------------------
    def tick(self) -> None:
        """Run one plant scan against everything the network has written.

        This is the only place the plant sees the staged image, and it is the
        reason the staged image exists. Re-staging from the result carries the
        coils forward, because a PLC output holds its value until the program
        changes it: outputs are not pulses and a coil that had to be rewritten
        every scan would be a very different machine.
        """
        self.image = self.cell.scan(self.staged)
        self.staged = self.image.copy()
        self.scans += 1

    # ---- the sockets --------------------------------------------------------
    def poll(self, timeout: float) -> None:
        """Service whatever is ready, for at most `timeout` seconds."""
        for key, _ in self._selector.select(timeout):
            sock = key.fileobj
            assert isinstance(sock, socket.socket)
            if sock is self._listener:
                self._accept()
            else:
                self._receive(sock)

    def _accept(self) -> None:
        connection, _ = self._listener.accept()
        connection.setblocking(False)
        self._selector.register(connection, selectors.EVENT_READ)
        self._streams[connection] = b""

    def _receive(self, connection: socket.socket) -> None:
        data = connection.recv(READ_SIZE)
        if not data:
            self._drop(connection)
            return

        stream = self._streams[connection] + data
        try:
            while True:
                frame, stream = take_frame(stream)
                if frame is None:
                    break
                connection.sendall(self._answer(frame))
        except MalformedFrame:
            # A Modbus exception response means "I understood you and the answer
            # is no". A malformed frame means this stream is not Modbus at all,
            # and answering it would be pretending to have understood something
            # that was never said. Hanging up is the honest reply, and it is
            # what a real device does.
            self._drop(connection)
            return
        self._streams[connection] = stream

    def _answer(self, frame: bytes) -> bytes:
        response, self.staged = handle(parse(frame), self.staged)
        return response

    def _drop(self, connection: socket.socket) -> None:
        self._selector.unregister(connection)
        del self._streams[connection]
        connection.close()

    # ---- the loop -----------------------------------------------------------
    def run(self, scans: int) -> None:
        """Serve the network and scan the plant, for `scans` scans.

        Finite on purpose. A loop that only ever runs forever cannot be tested,
        and a scan budget is also what a scenario run wants: play 500 scans of a
        changeover and report what came out. `__main__` below is the forever
        version, and it is three lines built out of this one.

        The deadline is monotonic rather than a sleep of one period, because a
        sleep would let every scan drift by however long the network work took,
        and a scan loop whose period depends on how chatty the master is would
        be the least useful possible property to give a simulation.
        """
        deadline = time.monotonic()
        for _ in range(scans):
            deadline += self.period_s
            remaining = deadline - time.monotonic()
            while remaining > 0:
                self.poll(remaining)
                remaining = deadline - time.monotonic()
            self.tick()

    # ---- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        for connection in list(self._streams):
            self._drop(connection)
        self._selector.close()
        self._listener.close()

    def __enter__(self) -> CellServer:
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        self.close()


if __name__ == "__main__":  # pragma: no cover
    with CellServer(Cell(), host="0.0.0.0", port=502) as server:  # noqa: S104
        print(f"plant listening on {server.port}, {server.period_s}s per scan")
        while True:
            server.run(scans=1000)
            print(f"scan {server.scans}: produced "
                  f"{server.cell.produced}, rejected {server.cell.rejected}")
