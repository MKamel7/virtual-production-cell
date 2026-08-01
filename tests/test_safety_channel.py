"""The safety channel under the four communication faults it exists to survive.

The protection layer itself is not tested here. It was built and attacked in the
fault injection harness against a catalogue of twenty seven faults, and it is
imported at a pinned tag rather than copied. What is tested here is the only
thing this project adds: **that anything the layer refuses reads as UNSAFE.**

Corruption, repetition, loss and delay all collapse to the same answer, and that
is deliberate. A controller reacting differently to a bad checksum than to a
stale counter would be making a safety decision out of a diagnosis, and the
diagnosis is for the person.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from vpc.cell import Cell
from vpc.safety_channel import STATUS, Responder, SafetyLink, SafetyState


@dataclass
class Wire:
    """A transport that can be told to misbehave, sitting ON the protected frame.

    The position matters more than the faults. This wraps the PeerEndpoint, so
    it sees the frame after the checksum and counter have been applied, which is
    where a communication fault actually happens. Wrapping the responder instead
    would corrupt the payload before it was protected, and every frame would
    arrive valid.
    """

    inner: object
    corrupt: bool = False
    repeat: bool = False
    drop: bool = False
    _last: str = ""

    def request(self, line: str) -> str:
        if self.drop:
            raise TimeoutError("no answer from the safety channel")
        answer: str = self.inner.request(line)  # type: ignore[attr-defined]
        if self.repeat and self._last:
            return self._last
        self._last = answer
        if self.corrupt:
            # Flip a character in the protected frame, so the checksum is what
            # catches it rather than the parser.
            return answer.replace("=1", "=0", 1) if "=1" in answer else answer
        return answer


def link_over(cell: Cell) -> tuple[SafetyLink, Wire]:
    held: dict[str, Wire] = {}

    def wire(peer: object) -> Wire:
        held["wire"] = Wire(peer)
        return held["wire"]

    link = SafetyLink(cell, wire=wire)
    return link, held["wire"]


# --- the healthy case, which the rest is measured against ---------------------
@pytest.mark.verifies("SR-13")
def test_a_healthy_channel_reports_the_plant_truthfully() -> None:
    cell = Cell()
    link, _ = link_over(cell)

    state = link.read()

    assert state.trusted
    assert state.guard_closed and state.torque_available


@pytest.mark.verifies("SR-13")
def test_the_channel_reports_a_guard_opening() -> None:
    cell = Cell()
    link, _ = link_over(cell)
    cell.open_guard()

    state = link.read()

    assert state.trusted
    assert not state.guard_closed
    assert not state.torque_available


# --- the four faults, all collapsing to the same answer -----------------------
@pytest.mark.verifies("SR-14")
def test_a_corrupted_message_reads_as_unsafe() -> None:
    """Not as an error to log, not as a value to retry, not as last known good."""
    cell = Cell()
    link, wire = link_over(cell)
    assert link.read().trusted

    wire.corrupt = True
    state = link.read()

    assert not state.trusted
    assert not state.torque_available and not state.guard_closed


@pytest.mark.verifies("SR-14")
def test_a_repeated_message_reads_as_unsafe() -> None:
    """A stuck sender repeating a healthy frame is the dangerous one.

    Every byte of it is valid. Only the counter shows that nothing new has been
    said, which is exactly what a sequence number is for and exactly what a
    plain Modbus bit cannot express.
    """
    cell = Cell()
    link, wire = link_over(cell)
    assert link.read().trusted

    wire.repeat = True
    state = link.read()

    assert not state.trusted
    assert not state.torque_available


@pytest.mark.verifies("SR-14")
def test_a_lost_message_reads_as_unsafe() -> None:
    cell = Cell()
    link, wire = link_over(cell)
    assert link.read().trusted

    wire.drop = True
    state = link.read()

    assert not state.trusted
    assert not state.torque_available


@pytest.mark.verifies("SR-14")
def test_a_frame_that_passes_the_checksum_and_makes_no_sense_reads_as_unsafe() -> None:
    """Valid framing is not the same as a message this channel can act on."""

    class Nonsense:
        def request(self, line: str) -> str:
            return "THE MACHINE IS FINE, HONESTLY"

    link = SafetyLink(Cell(), responder=Nonsense())

    state = link.read()

    assert not state.trusted
    assert state.refusal == "MALFORMED"


# --- the properties of the unsafe state itself --------------------------------
@pytest.mark.verifies("SR-14")
def test_an_unsafe_state_never_carries_the_last_known_good_values() -> None:
    """The exact defect the Modbus channel had with 'keep last value'.

    A refused message means the channel cannot prove what it is saying. Handing
    back what it said last time is the controller believing something nobody
    asserted.
    """
    state = SafetyState.unsafe("CRC")

    assert not state.guard_closed
    assert not state.torque_available
    assert not state.trusted


def test_the_refusal_reason_is_carried_rather_than_only_raised() -> None:
    """So a stop can be diagnosed without opening the machine.

    The reaction is the same for every fault; the REPORT is not, and that
    difference is what a person needs.
    """
    cell = Cell()
    link, wire = link_over(cell)
    link.read()
    wire.corrupt = True

    assert link.read().refusal


def test_the_channel_refuses_a_request_it_does_not_understand() -> None:
    """A safety link with a rich vocabulary has more ways to be wrong."""
    with pytest.raises(ValueError, match="STATUS"):
        Responder(Cell()).request("PLEASE START THE MACHINE")


def test_the_default_wire_talks_straight_to_the_plant() -> None:
    """The production path, with no test double anywhere in it."""
    cell = Cell()
    link = SafetyLink(cell)

    assert link.read().trusted
    assert Responder(cell).request(STATUS) == "GUARD=1,TORQUE=1"
