"""Modbus TCP, implemented rather than imported.

Two reasons for writing this instead of taking a library.

The first is testability, which is the constraint the whole project is shaped
by. The PLC runtime is on the other side of a dual boot and cannot be reached
from here, so the only defence against a wiring bug is to make the part that
does the wiring PURE and test it exhaustively. Everything in this module is a
function from bytes to bytes with no socket anywhere, so every frame, every
exception path and every boundary can be checked without a network.

The second is that Modbus TCP is genuinely small, and a dependency whose API
moves between minor versions is a worse bet than 200 lines that do not.

FRAME LAYOUT. A request is an MBAP header followed by a PDU:

    transaction id   2 bytes, echoed back untouched
    protocol id      2 bytes, always zero for Modbus TCP
    length           2 bytes, bytes following this field
    unit id          1 byte
    function code    1 byte
    data             function specific

ADDRESSING. Modbus has four separate spaces and they do not overlap: coils and
discrete inputs are bits, holding and input registers are words. The PLC writes
COILS, and reads DISCRETE INPUTS and INPUT REGISTERS. That direction is not a
convention this project invented, it is what the spaces mean, and it is why the
plant can never accidentally be commanded through an input.
"""

from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum

from vpc.process_image import Coil, Discrete, InputRegister, ProcessImage

MBAP_LENGTH = 7
PROTOCOL_ID = 0


class Function:
    """The function codes this device supports."""

    READ_COILS = 0x01
    READ_DISCRETE_INPUTS = 0x02
    READ_INPUT_REGISTERS = 0x04
    WRITE_SINGLE_COIL = 0x05
    WRITE_MULTIPLE_COILS = 0x0F


class Exception_(int):
    """Modbus exception codes. Named to avoid shadowing the builtin."""

    ILLEGAL_FUNCTION = 0x01
    ILLEGAL_DATA_ADDRESS = 0x02
    ILLEGAL_DATA_VALUE = 0x03


#: A coil write of this value means ON. Modbus defines exactly two legal values
#: for a single coil write and anything else is a data error, which matters:
#: silently treating 0x0001 as ON would accept a frame a real device rejects,
#: so the simulation would be more permissive than the machine.
COIL_ON = 0xFF00
COIL_OFF = 0x0000


class MalformedFrame(ValueError):
    """A frame that cannot be parsed at all, as opposed to one that is refused.

    Distinct from an exception response on purpose. A Modbus exception is a
    valid conversation in which the device says no; a malformed frame means the
    stream is not Modbus, and answering it politely would be pretending to
    understand something that was not said.
    """


@dataclass(frozen=True)
class Request:
    transaction_id: int
    unit_id: int
    function: int
    address: int
    quantity: int
    #: Present only for writes.
    payload: bytes = b""


def parse(frame: bytes) -> Request:
    """Parse a request frame, or raise MalformedFrame."""
    if len(frame) < MBAP_LENGTH + 1:
        raise MalformedFrame(f"frame is {len(frame)} bytes, too short for MBAP plus a function")

    transaction_id, protocol_id, length, unit_id = struct.unpack(">HHHB", frame[:MBAP_LENGTH])
    if protocol_id != PROTOCOL_ID:
        raise MalformedFrame(f"protocol id {protocol_id} is not Modbus TCP")

    # `length` counts the unit id and everything after it.
    expected = MBAP_LENGTH - 1 + length
    if len(frame) != expected:
        raise MalformedFrame(
            f"header declares {expected} bytes, frame is {len(frame)}")

    function = frame[MBAP_LENGTH]
    body = frame[MBAP_LENGTH + 1:]

    if function in (Function.READ_COILS, Function.READ_DISCRETE_INPUTS,
                    Function.READ_INPUT_REGISTERS):
        if len(body) != 4:
            raise MalformedFrame(f"read function {function} needs 4 data bytes")
        address, quantity = struct.unpack(">HH", body)
        return Request(transaction_id, unit_id, function, address, quantity)

    if function == Function.WRITE_SINGLE_COIL:
        if len(body) != 4:
            raise MalformedFrame("write single coil needs 4 data bytes")
        address, value = struct.unpack(">HH", body)
        return Request(transaction_id, unit_id, function, address, 1,
                       struct.pack(">H", value))

    if function == Function.WRITE_MULTIPLE_COILS:
        if len(body) < 5:
            raise MalformedFrame("write multiple coils needs at least 5 data bytes")
        address, quantity, byte_count = struct.unpack(">HHB", body[:5])
        if len(body) != 5 + byte_count:
            raise MalformedFrame(
                f"byte count {byte_count} does not match {len(body) - 5} bytes of data")
        return Request(transaction_id, unit_id, function, address, quantity,
                       body[5:])

    # An unknown function is still a well formed frame. The device answers with
    # an exception rather than hanging up, which is what a real one does.
    return Request(transaction_id, unit_id, function, 0, 0)


def _exception(request: Request, code: int) -> bytes:
    pdu = struct.pack(">BB", request.function | 0x80, code)
    return _wrap(request, pdu)


def _wrap(request: Request, pdu: bytes) -> bytes:
    header = struct.pack(">HHHB", request.transaction_id, PROTOCOL_ID,
                         len(pdu) + 1, request.unit_id)
    return header + pdu


def _pack_bits(values: list[bool]) -> bytes:
    """Modbus packs bits least significant first within each byte."""
    out = bytearray((len(values) + 7) // 8)
    for index, value in enumerate(values):
        if value:
            out[index // 8] |= 1 << (index % 8)
    return bytes(out)


def _unpack_bits(data: bytes, quantity: int) -> list[bool]:
    return [bool(data[i // 8] & (1 << (i % 8))) for i in range(quantity)]


def handle(request: Request, image: ProcessImage) -> tuple[bytes, ProcessImage]:
    """Answer a request against the process image.

    Returns the response frame and the image, which is REPLACED rather than
    mutated when a write lands. Keeping it immutable here means a half applied
    write cannot exist: either the whole frame is accepted and a new image is
    returned, or nothing changed and an exception goes back.
    """
    if request.function == Function.READ_DISCRETE_INPUTS:
        source = {int(k): v for k, v in image.discretes.items()}
        return _read_bits(request, source, list(Discrete)), image
    if request.function == Function.READ_COILS:
        source = {int(k): v for k, v in image.coils.items()}
        return _read_bits(request, source, list(Coil)), image
    if request.function == Function.READ_INPUT_REGISTERS:
        return _read_registers(request, image), image
    if request.function == Function.WRITE_SINGLE_COIL:
        return _write_single_coil(request, image)
    if request.function == Function.WRITE_MULTIPLE_COILS:
        return _write_multiple_coils(request, image)
    return _exception(request, Exception_.ILLEGAL_FUNCTION), image


def _read_bits(request: Request, source: Mapping[int, bool],
               members: Sequence[IntEnum]) -> bytes:
    """Members are passed rather than the enum class.

    Mypy cannot see an enum class as iterable, and the alternative was an
    ignore comment. A list of members is also honest about what this needs:
    the addresses, not the type.
    """
    if not 1 <= request.quantity <= 2000:
        return _exception(request, Exception_.ILLEGAL_DATA_VALUE)
    highest = max(int(m) for m in members)
    if request.address + request.quantity - 1 > highest:
        return _exception(request, Exception_.ILLEGAL_DATA_ADDRESS)

    values = [bool(source[request.address + offset])
              for offset in range(request.quantity)]
    data = _pack_bits(values)
    return _wrap(request, struct.pack(">BB", request.function, len(data)) + data)


def _read_registers(request: Request, image: ProcessImage) -> bytes:
    if not 1 <= request.quantity <= 125:
        return _exception(request, Exception_.ILLEGAL_DATA_VALUE)
    highest = max(int(m) for m in InputRegister)
    if request.address + request.quantity - 1 > highest:
        return _exception(request, Exception_.ILLEGAL_DATA_ADDRESS)

    words = b"".join(
        struct.pack(">H", image.registers[InputRegister(request.address + offset)] & 0xFFFF)
        for offset in range(request.quantity))
    return _wrap(request, struct.pack(">BB", request.function, len(words)) + words)


def _write_single_coil(request: Request,
                       image: ProcessImage) -> tuple[bytes, ProcessImage]:
    (value,) = struct.unpack(">H", request.payload)
    if value not in (COIL_ON, COIL_OFF):
        # Modbus defines exactly two legal values here. Accepting anything else
        # would make this device more permissive than a real one.
        return _exception(request, Exception_.ILLEGAL_DATA_VALUE), image
    if request.address > max(int(m) for m in Coil):
        return _exception(request, Exception_.ILLEGAL_DATA_ADDRESS), image

    updated = image.copy()
    updated.coils[Coil(request.address)] = value == COIL_ON
    echo = struct.pack(">BHH", request.function, request.address, value)
    return _wrap(request, echo), updated


def _write_multiple_coils(request: Request,
                          image: ProcessImage) -> tuple[bytes, ProcessImage]:
    if not 1 <= request.quantity <= 1968:
        return _exception(request, Exception_.ILLEGAL_DATA_VALUE), image
    if request.address + request.quantity - 1 > max(int(m) for m in Coil):
        return _exception(request, Exception_.ILLEGAL_DATA_ADDRESS), image

    values = _unpack_bits(request.payload, request.quantity)
    updated = image.copy()
    for offset, value in enumerate(values):
        updated.coils[Coil(request.address + offset)] = value
    echo = struct.pack(">BHH", request.function, request.address, request.quantity)
    return _wrap(request, echo), updated


def build_read_request(function: int, address: int, quantity: int,
                       transaction_id: int = 1, unit_id: int = 1) -> bytes:
    """Build a read request. Used by the tests and by any client side tooling."""
    pdu = struct.pack(">BHH", function, address, quantity)
    header = struct.pack(">HHHB", transaction_id, PROTOCOL_ID, len(pdu) + 1, unit_id)
    return header + pdu


def build_write_single_coil(address: int, value: bool, transaction_id: int = 1,
                            unit_id: int = 1) -> bytes:
    pdu = struct.pack(">BHH", Function.WRITE_SINGLE_COIL, address,
                      COIL_ON if value else COIL_OFF)
    header = struct.pack(">HHHB", transaction_id, PROTOCOL_ID, len(pdu) + 1, unit_id)
    return header + pdu
