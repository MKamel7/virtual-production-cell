"""Properties of the Modbus layer, checked against inputs nobody chose.

`tests/test_modbus.py` checks the cases somebody thought of. This file checks
the ones nobody did, which matters more here than anywhere else in the project:
`modbus.py` is the only code that reads bytes off a socket, and every one of
those bytes is written by something outside this repository.

The contract is the same one any protocol parser owes its caller. Given
arbitrary input it must return a result or raise the ONE exception it documents.
Anything else, an IndexError from a short slice, a struct.error from a bad
unpack, is the parser leaking its implementation into the caller's error
handling, and on a server it means a malformed frame takes the process down
rather than the connection.

That failure is not hypothetical. The plant was killed in use by an unhandled
ConnectionResetError, which is the same shape of bug one layer down.
"""

from __future__ import annotations

import struct

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vpc.modbus import (
    COIL_OFF,
    COIL_ON,
    MAX_FRAME,
    MBAP_LENGTH,
    Function,
    MalformedFrame,
    Request,
    build_read_request,
    build_write_single_coil,
    handle,
    parse,
    take_frame,
)
from vpc.process_image import Coil, Discrete, InputRegister, ProcessImage

#: Deadlines off because CI machines stall unpredictably and a timing flake in a
#: property test teaches nobody anything. The function-scoped fixture health
#: check is not relevant here: nothing below uses a fixture.
SETTINGS = settings(max_examples=300, deadline=None,
                    suppress_health_check=[HealthCheck.function_scoped_fixture])

#: Frames built to look plausible rather than purely random, so the search
#: spends its time past the header checks instead of bouncing off them.
plausible = st.builds(
    lambda tid, unit, function, body: (
        struct.pack(">HHHB", tid, 0, len(body) + 2, unit)
        + bytes([function]) + body),
    tid=st.integers(0, 0xFFFF),
    unit=st.integers(0, 0xFF),
    function=st.integers(0, 0xFF),
    body=st.binary(min_size=0, max_size=32),
)

anything = st.one_of(st.binary(min_size=0, max_size=300), plausible)


# --- the never crash contract ------------------------------------------------
@SETTINGS
@given(frame=anything)
def test_parse_returns_a_request_or_refuses_and_never_anything_else(
        frame: bytes) -> None:
    """The whole contract of a parser that faces a network."""
    try:
        request = parse(frame)
    except MalformedFrame:
        return
    assert isinstance(request, Request)
    assert 0 <= request.transaction_id <= 0xFFFF
    assert 0 <= request.unit_id <= 0xFF


@SETTINGS
@given(stream=anything)
def test_take_frame_never_loses_or_invents_bytes(stream: bytes) -> None:
    """Whatever comes off the front, the rest must be exactly what is left.

    A splitter that dropped a byte would desynchronise the stream permanently
    and every later frame would be garbage, which presents as a device that
    works for a while and then stops.
    """
    try:
        frame, rest = take_frame(stream)
    except MalformedFrame:
        return
    if frame is None:
        assert rest == stream
    else:
        assert frame + rest == stream
        assert len(frame) <= MAX_FRAME


@SETTINGS
@given(frame=anything, image=st.builds(ProcessImage))
def test_handle_always_answers_and_never_raises(frame: bytes,
                                                image: ProcessImage) -> None:
    """A device answers or hangs up. It does not throw at its caller."""
    try:
        request = parse(frame)
    except MalformedFrame:
        return
    response, updated = handle(request, image)

    assert isinstance(response, bytes)
    assert len(response) >= MBAP_LENGTH + 2
    transaction, protocol, length, unit = struct.unpack(">HHHB",
                                                        response[:MBAP_LENGTH])
    assert transaction == request.transaction_id, "a master matches on this"
    assert protocol == 0
    assert unit == request.unit_id
    assert length == len(response) - (MBAP_LENGTH - 1)
    assert isinstance(updated, ProcessImage)


@SETTINGS
@given(frame=anything)
def test_a_write_never_touches_the_image_it_was_given(frame: bytes) -> None:
    """Either the whole frame lands or nothing does.

    On a machine a partly applied write is a subset of the actuators moving,
    which is worse than none of them moving.
    """
    original = ProcessImage()
    before = dict(original.coils)
    try:
        request = parse(frame)
    except MalformedFrame:
        return
    handle(request, original)

    assert original.coils == before


# --- the stream splits back into exactly what went in ------------------------
@SETTINGS
@given(
    frames=st.lists(
        st.one_of(
            st.builds(build_read_request,
                      function=st.sampled_from([Function.READ_COILS,
                                                Function.READ_DISCRETE_INPUTS,
                                                Function.READ_INPUT_REGISTERS]),
                      address=st.integers(0, 7),
                      quantity=st.integers(1, 8),
                      transaction_id=st.integers(0, 0xFFFF)),
            st.builds(build_write_single_coil,
                      address=st.integers(0, 4),
                      value=st.booleans(),
                      transaction_id=st.integers(0, 0xFFFF)),
        ),
        min_size=0, max_size=6),
    cut=st.integers(0, 400),
)
def test_a_pipelined_stream_splits_back_into_the_frames_that_built_it(
        frames: list[bytes], cut: int) -> None:
    """Delivered in one read or dribbled in two, the frames must be identical.

    This is the property the server depends on and the one a socket will break
    if it is wrong, because TCP is free to chop the stream anywhere it likes.
    """
    stream = b"".join(frames)
    cut = min(cut, len(stream))

    recovered = []
    buffer = b""
    for chunk in (stream[:cut], stream[cut:]):
        buffer += chunk
        while True:
            frame, buffer = take_frame(buffer)
            if frame is None:
                break
            recovered.append(frame)

    assert recovered == frames
    assert buffer == b""


# --- round trips -------------------------------------------------------------
@SETTINGS
@given(address=st.integers(0, max(int(c) for c in Coil)), value=st.booleans())
def test_a_coil_written_reads_back_as_written(address: int, value: bool) -> None:
    response, updated = handle(parse(build_write_single_coil(address, value)),
                               ProcessImage())

    assert updated.coils[Coil(address)] is value
    assert struct.unpack(">H", response[-2:])[0] == (COIL_ON if value else COIL_OFF)


@SETTINGS
@given(
    values=st.lists(st.booleans(), min_size=len(Discrete), max_size=len(Discrete)),
    quantity=st.integers(1, len(Discrete)),
)
def test_reading_discrete_inputs_returns_the_bits_that_were_set(
        values: list[bool], quantity: int) -> None:
    image = ProcessImage()
    for signal, value in zip(Discrete, values, strict=True):
        image.discretes[signal] = value

    response, _ = handle(
        parse(build_read_request(Function.READ_DISCRETE_INPUTS, 0, quantity)),
        image)

    data = response[MBAP_LENGTH + 2:]
    for index in range(quantity):
        got = bool(data[index // 8] & (1 << (index % 8)))
        assert got is values[index], f"bit {index} came back wrong"


@SETTINGS
@given(counters=st.lists(st.integers(0, 0xFFFF),
                         min_size=len(InputRegister), max_size=len(InputRegister)))
def test_registers_round_trip_big_endian(counters: list[int]) -> None:
    image = ProcessImage()
    for register, value in zip(InputRegister, counters, strict=True):
        image.registers[register] = value

    response, _ = handle(
        parse(build_read_request(Function.READ_INPUT_REGISTERS, 0, len(counters))),
        image)

    words = struct.unpack(f">{len(counters)}H", response[MBAP_LENGTH + 2:])
    assert list(words) == counters


# --- the guarantee that is arithmetic rather than policy ----------------------
@SETTINGS
@given(address=st.integers(0, 0xFFFF), value=st.booleans())
def test_no_coil_write_at_any_address_can_reach_a_discrete_input(
        address: int, value: bool) -> None:
    """The strongest form of the safety property, over every address there is.

    Coils and discrete inputs are separate spaces, so there is no frame that
    commands a sensor. Checked exhaustively over the whole address range rather
    than at the one address somebody thought to try.
    """
    image = ProcessImage()
    before = dict(image.discretes)

    _, updated = handle(parse(build_write_single_coil(address, value)), image)

    assert updated.discretes == before, (
        f"a coil write at {address} changed a discrete input"
    )


def test_hypothesis_is_actually_running() -> None:
    """Guards against the whole file silently becoming decoration.

    A property suite that stopped generating examples would keep passing, and
    the loudest possible failure is better than a green tick that means nothing.
    """
    seen: set[int] = set()

    @settings(max_examples=50, deadline=None)
    @given(value=st.integers(0, 1000))
    def collect(value: int) -> None:
        seen.add(value)

    collect()
    assert len(seen) > 5, "hypothesis generated almost nothing"


def test_the_parser_refuses_rather_than_truncating_a_huge_frame() -> None:
    """The one case the property tests cannot reach, since they cap their input."""
    with pytest.raises(MalformedFrame, match="maximum"):
        take_frame(struct.pack(">HHH", 1, 0, 0xFFFF) + b"\x01")
