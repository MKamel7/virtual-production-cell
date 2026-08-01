"""Modbus framing, tested without a socket.

The PLC runtime is on the other side of a dual boot and cannot be reached from
here, so a wiring bug would not surface until integration, in the place where
everything looks broken at once. The defence is to keep the part that does the
wiring pure and check it properly: every function code, every exception path,
every boundary and every malformed frame.

The property that matters most is the LAST one in this file: that the PLC cannot
command the plant through an input. Modbus keeps bits and words in separate
address spaces, so an actuator and a sensor at the same number are different
things, and a device that blurred them would let a read look like a command.
"""

from __future__ import annotations

import struct

import pytest

from vpc.modbus import (
    COIL_ON,
    MAX_FRAME,
    MBAP_LENGTH,
    Exception_,
    Function,
    MalformedFrame,
    build_read_request,
    build_write_single_coil,
    handle,
    parse,
    take_frame,
)
from vpc.process_image import Coil, Discrete, InputRegister, ProcessImage


def answer(frame: bytes, image: ProcessImage) -> tuple[bytes, ProcessImage]:
    return handle(parse(frame), image)


def pdu(response: bytes) -> bytes:
    return response[7:]


# --- framing -----------------------------------------------------------------
def test_a_well_formed_request_round_trips() -> None:
    request = parse(build_read_request(Function.READ_DISCRETE_INPUTS, 0, 8,
                                       transaction_id=0x1234, unit_id=7))
    assert request.transaction_id == 0x1234
    assert request.unit_id == 7
    assert request.function == Function.READ_DISCRETE_INPUTS
    assert (request.address, request.quantity) == (0, 8)


def test_the_transaction_id_is_echoed_untouched() -> None:
    """A master matches responses to requests by it, so altering it loses replies."""
    frame = build_read_request(Function.READ_DISCRETE_INPUTS, 0, 1,
                               transaction_id=0xBEEF)
    response, _ = answer(frame, ProcessImage())
    assert struct.unpack(">H", response[:2])[0] == 0xBEEF


@pytest.mark.parametrize("frame,reason", [
    (b"", "too short"),
    (b"\x00\x01\x00\x00\x00", "too short"),
    (struct.pack(">HHHB", 1, 99, 6, 1) + b"\x02\x00\x00\x00\x01", "protocol id"),
    (struct.pack(">HHHB", 1, 0, 99, 1) + b"\x02\x00\x00\x00\x01", "header declares"),
    (struct.pack(">HHHB", 1, 0, 3, 1) + b"\x02\x00", "4 data bytes"),
])
def test_malformed_frames_are_rejected_rather_than_guessed(frame: bytes,
                                                           reason: str) -> None:
    """A malformed frame means the stream is not Modbus.

    Distinct from an exception response, which is a valid conversation in which
    the device says no. Answering an unparseable frame politely would be
    pretending to understand something that was not said.
    """
    with pytest.raises(MalformedFrame, match=reason):
        parse(frame)


def test_a_write_multiple_coils_frame_with_a_lying_byte_count_is_rejected() -> None:
    body = struct.pack(">HHB", 0, 8, 4) + b"\xff"          # claims 4, carries 1
    frame = (struct.pack(">HHHB", 1, 0, len(body) + 2, 1)
             + bytes([Function.WRITE_MULTIPLE_COILS]) + body)
    with pytest.raises(MalformedFrame, match="byte count"):
        parse(frame)


# --- reads -------------------------------------------------------------------
def test_reading_discrete_inputs_returns_the_plant_state() -> None:
    image = ProcessImage()
    image.discretes[Discrete.PRODUCT_AT_FILLER] = True
    image.discretes[Discrete.SAFETY_OK] = True

    response, _ = answer(build_read_request(Function.READ_DISCRETE_INPUTS, 0, 8), image)
    body = pdu(response)
    assert body[0] == Function.READ_DISCRETE_INPUTS
    assert body[1] == 1                       # one byte covers eight bits
    bits = body[2]
    assert bits & (1 << int(Discrete.PRODUCT_AT_FILLER))
    assert bits & (1 << int(Discrete.SAFETY_OK))
    assert not bits & (1 << int(Discrete.PRODUCT_AT_CAPPER))


def test_bits_are_packed_least_significant_first() -> None:
    """The Modbus convention, and getting it backwards inverts every signal."""
    image = ProcessImage()
    image.discretes[Discrete.PRODUCT_AT_FILLER] = True      # address 0
    response, _ = answer(build_read_request(Function.READ_DISCRETE_INPUTS, 0, 2), image)
    assert pdu(response)[2] == 0b01


def test_reading_input_registers_returns_counters_big_endian() -> None:
    image = ProcessImage()
    image.registers[InputRegister.PRODUCED] = 0x0102
    image.registers[InputRegister.REJECTED] = 3

    response, _ = answer(build_read_request(Function.READ_INPUT_REGISTERS, 0, 2), image)
    body = pdu(response)
    assert body[1] == 4
    assert struct.unpack(">HH", body[2:]) == (0x0102, 3)


def test_reading_coils_returns_what_the_plc_last_wrote() -> None:
    image = ProcessImage()
    image.coils[Coil.CONVEYOR_RUN] = True
    response, _ = answer(build_read_request(Function.READ_COILS, 0, 5), image)
    assert pdu(response)[2] & 1


# --- writes ------------------------------------------------------------------
def test_writing_a_single_coil_commands_the_plant_and_echoes() -> None:
    response, updated = answer(
        build_write_single_coil(int(Coil.CONVEYOR_RUN), True), ProcessImage())
    assert updated.coils[Coil.CONVEYOR_RUN]
    assert pdu(response) == struct.pack(">BHH", Function.WRITE_SINGLE_COIL,
                                        int(Coil.CONVEYOR_RUN), COIL_ON)


def test_writing_a_single_coil_off_works_too() -> None:
    image = ProcessImage()
    image.coils[Coil.CONVEYOR_RUN] = True
    _, updated = answer(build_write_single_coil(int(Coil.CONVEYOR_RUN), False), image)
    assert not updated.coils[Coil.CONVEYOR_RUN]


def test_a_coil_value_that_is_neither_on_nor_off_is_refused() -> None:
    """Modbus defines exactly two legal values, and a real device rejects others.

    Accepting 0x0001 as ON would make this simulation more permissive than the
    machine, so a frame that works here would fail on the bench.
    """
    pdu_bytes = struct.pack(">BHH", Function.WRITE_SINGLE_COIL, 0, 0x0001)
    frame = struct.pack(">HHHB", 1, 0, len(pdu_bytes) + 1, 1) + pdu_bytes
    response, updated = answer(frame, ProcessImage())
    assert pdu(response)[0] == Function.WRITE_SINGLE_COIL | 0x80
    assert pdu(response)[1] == Exception_.ILLEGAL_DATA_VALUE
    assert not updated.coils[Coil.CONVEYOR_RUN]


def test_writing_multiple_coils_applies_all_of_them() -> None:
    body = struct.pack(">HHB", 0, 4, 1) + bytes([0b0101])
    frame = (struct.pack(">HHHB", 1, 0, len(body) + 2, 1)
             + bytes([Function.WRITE_MULTIPLE_COILS]) + body)
    response, updated = answer(frame, ProcessImage())

    assert updated.coils[Coil.CONVEYOR_RUN]
    assert not updated.coils[Coil.FILLER_DOSE]
    assert updated.coils[Coil.CAPPER_ACTUATE]
    assert not updated.coils[Coil.REJECT_EJECT]
    assert pdu(response) == struct.pack(">BHH", Function.WRITE_MULTIPLE_COILS, 0, 4)


def test_a_write_leaves_the_original_image_untouched() -> None:
    """Either the whole frame lands or nothing does.

    The handler returns a new image rather than mutating, so a half applied
    write cannot exist. On a machine that would be a subset of the actuators
    moving, which is worse than none of them.
    """
    original = ProcessImage()
    _, updated = answer(build_write_single_coil(int(Coil.CONVEYOR_RUN), True), original)
    assert not original.coils[Coil.CONVEYOR_RUN]
    assert updated.coils[Coil.CONVEYOR_RUN]


# --- refusals ----------------------------------------------------------------
def test_an_address_beyond_the_map_is_refused() -> None:
    response, _ = answer(build_read_request(Function.READ_DISCRETE_INPUTS, 0, 99),
                         ProcessImage())
    assert pdu(response)[1] == Exception_.ILLEGAL_DATA_ADDRESS


def test_writing_a_coil_beyond_the_map_is_refused() -> None:
    response, updated = answer(build_write_single_coil(99, True), ProcessImage())
    assert pdu(response)[1] == Exception_.ILLEGAL_DATA_ADDRESS
    assert updated.coils == ProcessImage().coils


@pytest.mark.parametrize("quantity", [0, 3000])
def test_a_nonsense_quantity_is_refused(quantity: int) -> None:
    response, _ = answer(
        build_read_request(Function.READ_DISCRETE_INPUTS, 0, quantity), ProcessImage())
    assert pdu(response)[1] == Exception_.ILLEGAL_DATA_VALUE


def test_a_nonsense_register_quantity_is_refused() -> None:
    response, _ = answer(
        build_read_request(Function.READ_INPUT_REGISTERS, 0, 999), ProcessImage())
    assert pdu(response)[1] == Exception_.ILLEGAL_DATA_VALUE


def test_a_register_address_beyond_the_map_is_refused() -> None:
    response, _ = answer(
        build_read_request(Function.READ_INPUT_REGISTERS, 0, 50), ProcessImage())
    assert pdu(response)[1] == Exception_.ILLEGAL_DATA_ADDRESS


def test_writing_multiple_coils_beyond_the_map_is_refused() -> None:
    body = struct.pack(">HHB", 0, 99, 13) + bytes(13)
    frame = (struct.pack(">HHHB", 1, 0, len(body) + 2, 1)
             + bytes([Function.WRITE_MULTIPLE_COILS]) + body)
    response, updated = answer(frame, ProcessImage())
    assert pdu(response)[1] == Exception_.ILLEGAL_DATA_ADDRESS
    assert updated.coils == ProcessImage().coils


def test_a_nonsense_multiple_coil_quantity_is_refused() -> None:
    body = struct.pack(">HHB", 0, 0, 0)
    frame = (struct.pack(">HHHB", 1, 0, len(body) + 2, 1)
             + bytes([Function.WRITE_MULTIPLE_COILS]) + body)
    response, _ = answer(frame, ProcessImage())
    assert pdu(response)[1] == Exception_.ILLEGAL_DATA_VALUE


def test_an_unsupported_function_gets_an_exception_not_a_hangup() -> None:
    """A real device answers. Dropping the connection looks like a network fault."""
    pdu_bytes = struct.pack(">BHH", 0x63, 0, 1)
    frame = struct.pack(">HHHB", 1, 0, len(pdu_bytes) + 1, 1) + pdu_bytes
    response, _ = answer(frame, ProcessImage())
    assert pdu(response)[0] == 0x63 | 0x80
    assert pdu(response)[1] == Exception_.ILLEGAL_FUNCTION


# --- the property the address spaces exist to guarantee ----------------------
@pytest.mark.verifies("SR-04")
def test_the_same_address_is_a_different_object_in_each_space() -> None:
    """Modbus keeps bits and words in separate spaces, and that is load bearing.

    Address 2 is CAPPER_ACTUATE as a coil and PRODUCT_AT_QC as a discrete input.
    They are different objects. A device that blurred the two would let the
    controller assert a sensor, and the sensor it would most want to assert is
    the one saying it is safe to move.
    """
    image = ProcessImage()
    assert int(Coil.CAPPER_ACTUATE) == int(Discrete.PRODUCT_AT_QC) == 2

    _, updated = answer(build_write_single_coil(2, True), image)

    assert updated.coils[Coil.CAPPER_ACTUATE], "the write did not reach the coil"
    assert not updated.discretes[Discrete.PRODUCT_AT_QC], (
        "a coil write reached a discrete input, so the controller could assert "
        "a sensor reading"
    )


@pytest.mark.verifies("SR-04")
def test_there_is_no_way_to_write_a_safety_input_at_all() -> None:
    """SAFETY_OK is discrete input 6 and there is no coil 6.

    So the frame that would look like commanding it is refused for want of an
    address, which is the strongest form of the guarantee: not policy, arithmetic.
    """
    response, updated = answer(
        build_write_single_coil(int(Discrete.SAFETY_OK), True), ProcessImage())

    assert pdu(response)[1] == Exception_.ILLEGAL_DATA_ADDRESS
    assert not updated.discretes[Discrete.SAFETY_OK]
    assert max(int(m) for m in Coil) < int(Discrete.SAFETY_OK), (
        "a coil now exists at the safety input's address, so this guarantee has "
        "become policy rather than arithmetic"
    )


def test_a_truncated_write_single_coil_frame_is_rejected() -> None:
    body = struct.pack(">H", 0)                      # address only, no value
    frame = (struct.pack(">HHHB", 1, 0, len(body) + 2, 1)
             + bytes([Function.WRITE_SINGLE_COIL]) + body)
    with pytest.raises(MalformedFrame, match="write single coil"):
        parse(frame)


def test_a_truncated_write_multiple_coils_frame_is_rejected() -> None:
    body = struct.pack(">HH", 0, 4)                  # header only, no byte count
    frame = (struct.pack(">HHHB", 1, 0, len(body) + 2, 1)
             + bytes([Function.WRITE_MULTIPLE_COILS]) + body)
    with pytest.raises(MalformedFrame, match="at least 5"):
        parse(frame)


# --- splitting a stream into frames ------------------------------------------
# `parse` demands an exact frame, which is the right contract for it and useless
# against a socket, where a read delivers whatever happened to have arrived.
# `take_frame` is the seam between the two and it is pure, so every split point
# can be checked here rather than discovered during integration.
def test_a_stream_shorter_than_the_length_field_yields_nothing() -> None:
    frame, rest = take_frame(b"\x00\x01\x00")

    assert frame is None
    assert rest == b"\x00\x01\x00", "a partial header must be kept, not consumed"


def test_one_whole_frame_comes_off_the_front() -> None:
    whole = build_read_request(Function.READ_COILS, 0, 4)
    frame, rest = take_frame(whole)

    assert frame == whole
    assert rest == b""


def test_frames_come_off_one_at_a_time() -> None:
    first = build_read_request(Function.READ_COILS, 0, 4, transaction_id=1)
    second = build_write_single_coil(Coil.CONVEYOR_RUN, True, transaction_id=2)

    frame, rest = take_frame(first + second)
    assert frame == first

    frame, rest = take_frame(rest)
    assert frame == second
    assert rest == b""


def test_a_frame_arriving_one_byte_at_a_time_appears_exactly_when_it_is_whole() -> None:
    """The property that matters, checked at every split point rather than one.

    A server that answered early would be acting on a frame it had not finished
    reading; one that answered late would stall the master waiting for bytes it
    already had.
    """
    whole = build_write_single_coil(Coil.CONVEYOR_RUN, True)
    buffer = b""
    answers = []
    for byte in whole:
        buffer += bytes([byte])
        frame, buffer = take_frame(buffer)
        if frame is not None:
            answers.append(frame)

    assert answers == [whole]
    assert buffer == b""


def test_a_frame_declaring_an_impossible_length_is_malformed() -> None:
    """Not merely incomplete. The length is read before the body arrives, so a
    server that trusted it would wait forever for bytes no Modbus master will
    ever send."""
    with pytest.raises(MalformedFrame, match="maximum"):
        take_frame(struct.pack(">HHH", 1, 0, 60000) + b"\x01")


def test_the_largest_legal_frame_is_accepted() -> None:
    """The other side of the boundary, so the cap cannot quietly reject real traffic."""
    body = b"\x00" * (MAX_FRAME - MBAP_LENGTH)
    whole = struct.pack(">HHHB", 1, 0, len(body) + 1, 1) + body
    frame, rest = take_frame(whole)

    assert len(whole) == MAX_FRAME
    assert frame == whole
    assert rest == b""
